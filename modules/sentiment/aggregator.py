"""Temporal aggregation of the sentiment events (spec sez. 1.1.3, sez. 1.3 punto 7).

For each key ``k`` (an asset or ``MARKET``) and each event ``i`` with ``s_i = p_bull - p_bear``:

    w_i(t)   = conf_i * relevance_i * log(1 + source_count_i) * 2^(-(t - ingested_at_i) / half_life)
    ewm(t)   = sum(w_i s_i) / (sum(w_i) + eps)         in [-1, 1]
    mass(t)  = sum(w_i)
    disp(t)  = sqrt(weighted variance of s_i with weights w_i(t))
    shock(t) = max |s_i| * conf_i over the events of the last ``shock_window_min`` minutes

``ewm``/``mass`` are computed at two speeds (fast and slow half-life); ``disp`` and ``shock`` at the fast one only.

Point-in-time (constraint C7): ``snapshot(as_of)`` reads ONLY the events with ``ingested_at <= as_of`` (and not older
than ``retention_days``), where ``ingested_at`` is when we first saw the story, never ``published_at``. It is a pure
function of the state and of ``as_of``: it does not mutate anything, and the same state and ``as_of`` give the same
output. Pruning happens in ``add`` (relative to the newest event), so a snapshot taken far in the past of the newest
event can miss events that retention has already dropped: to replay history, build the aggregator with a retention that
covers it.

``source_count`` of the weight is the number of outlets that had published the story AT ``as_of``: the republications
are registered with their own ingestion time (``register_repost``) and counted only if ``<= as_of``.
Events reloaded from SQLite carry the final ``source_count`` of the database, which is exact for the live use (every
republication is already in the past of ``now``) and is the only information the schema keeps.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final, Literal

from modules.config import AggregationConfig
from modules.sentiment.schemas import (
    MARKET_KEY,
    AssetSentiment,
    FearGreedSnapshot,
    MarketSentiment,
    SentimentEvent,
    SentimentSnapshot,
    minutes_between,
)

EPS: Final = 1e-12
HOUR_MIN: Final = 60.0
DAY_MIN: Final = 1440.0


@dataclass(slots=True)
class _Rec:
    ts: datetime
    news_id: str
    s: float
    conf: float
    relevance: float
    base_count: int
    choice: Literal[0, 1, 2]


@dataclass(frozen=True, slots=True)
class FlowStats:
    """The aggregates of one key at one instant (fast + slow)."""

    ewm: float
    ewm_slow: float
    mass: float
    mass_slow: float
    disp: float
    shock: float
    n_1h: int
    n_24h: int
    last: _Rec | None


@dataclass(slots=True)
class _KeyState:
    recs: deque[_Rec] = field(default_factory=deque)  # sorted by ts


class SentimentAggregator:
    def __init__(self, cfg: AggregationConfig | None = None) -> None:
        self._cfg = AggregationConfig() if cfg is None else cfg
        self._keys: dict[str, _KeyState] = {}
        self._known: set[tuple[str, str]] = set()  # (news_id, key): an event is counted once
        self._reposts: dict[str, list[datetime]] = {}  # news_id -> ingestion time of each republication
        self._newest: datetime | None = None

    # ------------------------------------------------------------------------------- feeding

    def add(self, event: SentimentEvent, source_count: int = 1) -> bool:
        """Insert an event (kept sorted by ``ingested_at``). False if (news, key) was already counted."""
        ident = (event.news_id, event.asset_key)
        if ident in self._known:
            return False
        self._known.add(ident)
        rec = _Rec(
            ts=event.ingested_at,
            news_id=event.news_id,
            s=event.score,
            conf=event.confidence,
            relevance=event.relevance,
            base_count=max(source_count, 1),
            choice=event.choice,
        )
        recs = self._keys.setdefault(event.asset_key, _KeyState()).recs
        if not recs or recs[-1].ts <= rec.ts:
            recs.append(rec)
        else:  # classified out of order (micro-batches, backlog): keep the deque sorted
            recs.insert(bisect_right(recs, rec.ts, key=lambda r: r.ts), rec)
        if self._newest is None or rec.ts > self._newest:
            self._newest = rec.ts
            self._prune()
        return True

    def load(self, events: Iterable[tuple[SentimentEvent, int]]) -> int:
        """Warm-up from the store: ``(event, final source_count)`` pairs. Returns how many were added."""
        return sum(self.add(e, c) for e, c in events)

    def register_repost(self, news_id: str, seen_at: datetime) -> None:
        """Another outlet published the story at ``seen_at``: its weight grows from that instant on."""
        self._reposts.setdefault(news_id, []).append(seen_at)

    @property
    def retention(self) -> timedelta:
        return timedelta(days=self._cfg.retention_days)

    def _prune(self) -> None:
        if self._newest is None:
            return
        cutoff = self._newest - self.retention
        for key, state in self._keys.items():
            recs = state.recs
            while recs and recs[0].ts < cutoff:
                old = recs.popleft()
                self._known.discard((old.news_id, key))
        for news_id in [n for n, times in self._reposts.items() if max(times) < cutoff]:
            del self._reposts[news_id]

    # ----------------------------------------------------------------------------- reading

    def n_events(self, key: str | None = None) -> int:
        if key is not None:
            return len(self._keys[key].recs) if key in self._keys else 0
        return sum(len(s.recs) for s in self._keys.values())

    def _count_at(self, rec: _Rec, as_of: datetime) -> int:
        return rec.base_count + sum(1 for t in self._reposts.get(rec.news_id, ()) if t <= as_of)

    def flow(self, key: str, as_of: datetime) -> FlowStats:
        """Aggregates of ``key`` at ``as_of`` from the events with ``ingested_at <= as_of`` (C7)."""
        market = key == MARKET_KEY
        hl = self._cfg.half_life_min.market if market else self._cfg.half_life_min.asset
        hl_slow = self._cfg.half_life_slow_min.market if market else self._cfg.half_life_slow_min.asset
        horizon = as_of - self.retention
        recs = self._keys[key].recs if key in self._keys else deque[_Rec]()

        sw = sws = sw_slow = sws_slow = 0.0
        visible: list[tuple[float, float]] = []  # (s, fast weight) for the dispersion
        shock = 0.0
        n_1h = n_24h = 0
        last: _Rec | None = None
        for r in recs:
            if r.ts > as_of:
                break  # sorted: nothing after this point was known at as_of
            if r.ts < horizon:
                continue
            age = minutes_between(as_of, r.ts)
            base = r.conf * r.relevance * math.log1p(self._count_at(r, as_of))
            w, w_slow = base * 2.0 ** (-age / hl), base * 2.0 ** (-age / hl_slow)
            sw, sws, sw_slow, sws_slow = sw + w, sws + w * r.s, sw_slow + w_slow, sws_slow + w_slow * r.s
            visible.append((r.s, w))
            if age <= self._cfg.shock_window_min:
                shock = max(shock, abs(r.s) * r.conf)
            n_1h += age <= HOUR_MIN
            n_24h += age <= DAY_MIN
            last = r  # sorted by ts: the last visible one is the most recent
        disp = 0.0
        if sw > 0.0:
            mean = sws / sw
            disp = math.sqrt(max(sum(w * (s - mean) ** 2 for s, w in visible) / sw, 0.0))
        return FlowStats(sws / (sw + EPS), sws_slow / (sw_slow + EPS), sw, sw_slow, disp, shock, n_1h, n_24h, last)

    def asset_snapshot(self, key: str, as_of: datetime) -> AssetSentiment:
        f = self.flow(key, as_of)
        last = f.last
        return AssetSentiment(
            key=key,
            ewm=f.ewm,
            ewm_slow=f.ewm_slow,
            mass=f.mass,
            mass_slow=f.mass_slow,
            disp=f.disp,
            shock_15m=f.shock,
            n_1h=f.n_1h,
            n_24h=f.n_24h,
            last_choice=None if last is None else last.choice,
            last_conf=None if last is None else last.conf,
            last_age_min=None if last is None else int(minutes_between(as_of, last.ts)),
        )

    def market_snapshot(self, as_of: datetime) -> MarketSentiment:
        f = self.flow(MARKET_KEY, as_of)
        return MarketSentiment(
            ewm=f.ewm,
            ewm_slow=f.ewm_slow,
            mass=f.mass,
            mass_slow=f.mass_slow,
            disp=f.disp,
            shock_15m=f.shock,
            n_1h=f.n_1h,
        )

    def snapshot(
        self, as_of: datetime, asset_key: str, fear_greed: FearGreedSnapshot | None = None
    ) -> SentimentSnapshot:
        """Immutable snapshot for one asset plus the market. ``asset_key`` is the one extra argument with respect to
        the signature in the spec, which cannot say which asset it is about."""
        return SentimentSnapshot(
            as_of=as_of,
            asset=self.asset_snapshot(asset_key, as_of),
            market=self.market_snapshot(as_of),
            fear_greed=fear_greed,
        )
