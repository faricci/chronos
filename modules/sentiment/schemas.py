"""Data models of the sentiment pipeline (spec sez. 1.2): NewsItem, SentimentEvent, SentimentSnapshot.

Rules:
- every model is a frozen pydantic model that rejects unknown keys;
- every timestamp is a timezone-aware UTC ``datetime`` truncated to the millisecond and serialised as ISO-8601 with a
  ``Z`` suffix and ALWAYS three fractional digits (``2026-10-17T13:58:41.000Z``). The fixed width makes the text
  columns of SQLite sortable, so ``ingested_at <= ?`` in SQL is the same comparison as in Python (constraint C7), and
  the millisecond truncation makes serialise -> parse an identity.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Final, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, PlainSerializer, model_validator

MARKET_KEY: Final = "MARKET"
LABELS: Final = ("BEARISH", "NEUTRAL", "BULLISH")  # index = Choice token {0: BEARISH, 1: NEUTRAL, 2: BULLISH}
Label = Literal["BEARISH", "NEUTRAL", "BULLISH"]
PROB_TOLERANCE: Final = 1e-6


def _to_utc_ms(v: datetime) -> datetime:
    if v.tzinfo is None or v.utcoffset() is None:
        raise ValueError("naive datetime: sentiment timestamps must be timezone-aware")
    return v.astimezone(UTC).replace(microsecond=v.microsecond // 1000 * 1000)


def to_iso(v: datetime) -> str:
    """``2026-10-17T14:00:12.384Z``: fixed width, UTC, millisecond precision."""
    return _to_utc_ms(v).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_iso(s: str) -> datetime:
    """Inverse of ``to_iso`` (also accepts ISO strings without fractional digits or with an offset)."""
    return _to_utc_ms(datetime.fromisoformat(s))


def utc_now() -> datetime:
    """The ingestion clock: UTC, millisecond precision (so that what is stored equals what is held in memory)."""
    return _to_utc_ms(datetime.now(UTC))


UtcDatetime = Annotated[datetime, AfterValidator(_to_utc_ms), PlainSerializer(to_iso, return_type=str)]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ----------------------------------------------------------------------------------------------- news


class NewsItem(_Model):
    """A headline as it leaves a ``Source`` (and, once tagged, the ``AssetTagger``)."""

    news_id: str  # sha1(normalize_title(title)), see dedup.news_id_for
    source: str
    url: str | None = None
    title: str = Field(min_length=1)
    summary: str = ""  # first 512 characters of the description, HTML stripped
    published_at: UtcDatetime | None = None  # as declared by the publisher: NEVER used for point-in-time logic
    ingested_at: UtcDatetime  # when WE first saw it: the only timestamp the aggregator uses (C7)
    source_count: int = Field(default=1, ge=1)  # number of outlets that published the story
    assets: tuple[str, ...] = ()
    is_market_wide: bool = False

    @property
    def keys(self) -> tuple[str, ...]:
        """The aggregation keys of the story: its assets, or ``MARKET`` for a macro story without assets."""
        return self.assets if self.assets else (MARKET_KEY,)


class FearGreed(_Model):
    """One reading of the Alternative.me Fear & Greed index (a macro feature, not a headline)."""

    value: int = Field(ge=0, le=100)
    classification: str
    timestamp: UtcDatetime  # the instant the value refers to (the index is daily)
    fetched_at: UtcDatetime  # when we read it: the reading is visible from this instant on (C7)


# ------------------------------------------------------------------------------------------- events


class SentimentEvent(_Model):
    """The classification of one story for one aggregation key (one row of ``sentiment_events``).

    ``classified_at`` and ``latency_ms`` are not persisted (the DDL of sez. 1.2 has no column for them): events
    reloaded from SQLite carry ``None`` there.
    """

    news_id: str
    asset_key: str
    ingested_at: UtcDatetime
    classified_at: UtcDatetime | None = None
    model: str
    choice: Literal[0, 1, 2]
    label: Label
    probabilities: dict[Label, float]
    confidence: float = Field(ge=0.0, le=1.0)
    relevance: float = Field(ge=0.0, le=1.0)
    latency_ms: float | None = Field(default=None, ge=0.0)

    @model_validator(mode="after")
    def _consistent(self) -> SentimentEvent:
        if self.label != LABELS[self.choice]:
            raise ValueError(f"label {self.label!r} does not match choice {self.choice}")
        if set(self.probabilities) != set(LABELS):
            raise ValueError(f"probabilities must have exactly the keys {LABELS}")
        if abs(sum(self.probabilities.values()) - 1.0) > PROB_TOLERANCE:
            raise ValueError("probabilities must sum to 1")
        if any(p < 0.0 for p in self.probabilities.values()):
            raise ValueError("probabilities must be >= 0")
        return self

    @property
    def score(self) -> float:
        """``s = p_bull - p_bear`` in [-1, 1] (sez. 1.1.3)."""
        return self.probabilities["BULLISH"] - self.probabilities["BEARISH"]


# ----------------------------------------------------------------------------------------- snapshots


class AssetSentiment(_Model):
    key: str
    ewm: float
    ewm_slow: float
    mass: float
    mass_slow: float
    disp: float
    shock_15m: float
    n_1h: int
    n_24h: int
    last_choice: Literal[0, 1, 2] | None  # None: no event in the retention window
    last_conf: float | None
    last_age_min: int | None


class MarketSentiment(_Model):
    ewm: float
    ewm_slow: float
    mass: float
    mass_slow: float
    disp: float
    shock_15m: float
    n_1h: int


class FearGreedSnapshot(_Model):
    value: int
    age_h: float


class SentimentSnapshot(_Model):
    """What the ``StateBuilder`` reads at minute ``as_of``: a function of the events with ``ingested_at <= as_of``."""

    as_of: UtcDatetime
    asset: AssetSentiment
    market: MarketSentiment
    fear_greed: FearGreedSnapshot | None


def minutes_between(later: datetime, earlier: datetime) -> float:
    return (later - earlier) / timedelta(minutes=1)
