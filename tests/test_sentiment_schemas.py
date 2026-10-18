"""Sentiment schemas (spec sez. 1.2): UTC timestamps with Z, frozen models, consistent events."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from modules.sentiment.schemas import NewsItem, SentimentEvent, parse_iso, to_iso
from tests.sentiment_helpers import T0, event, item


def test_to_iso_is_fixed_width_utc_with_z() -> None:
    assert to_iso(datetime(2026, 10, 17, 14, 0, 12, 384_000, tzinfo=UTC)) == "2026-10-17T14:00:12.384Z"
    assert to_iso(datetime(2026, 10, 17, 13, 58, 41, tzinfo=UTC)) == "2026-10-17T13:58:41.000Z"
    # a non-UTC aware datetime is converted
    assert to_iso(datetime(2026, 10, 17, 16, 0, tzinfo=timezone(timedelta(hours=2)))) == "2026-10-17T14:00:00.000Z"


def test_fixed_width_text_sorts_like_time() -> None:
    a = datetime(2026, 10, 17, 14, 0, 12, 384_000, tzinfo=UTC)
    b = datetime(2026, 10, 17, 14, 0, 12, tzinfo=UTC)  # earlier than a, but "...12Z" > "...12.384Z" without fixed width
    assert (to_iso(b) < to_iso(a)) == (b < a)


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(ValidationError, match="naive"):
        NewsItem(news_id="x", source="s", title="t", ingested_at=datetime(2026, 1, 1))  # noqa: DTZ001


def test_microseconds_are_truncated_so_roundtrip_is_exact() -> None:
    n = item(ingested_at=datetime(2026, 10, 17, 14, 0, 12, 384_567, tzinfo=UTC))
    assert n.ingested_at.microsecond == 384_000
    assert parse_iso(to_iso(n.ingested_at)) == n.ingested_at


def test_news_item_json_matches_the_spec_shape() -> None:
    d = item(assets=("SOL",)).model_dump(mode="json")
    assert set(d) == {
        "news_id",
        "source",
        "url",
        "title",
        "summary",
        "published_at",
        "ingested_at",
        "source_count",
        "assets",
        "is_market_wide",
    }
    assert d["ingested_at"].endswith("Z") and d["assets"] == ["SOL"]


def test_models_are_frozen_and_reject_unknown_keys() -> None:
    n = item()
    with pytest.raises(ValidationError):
        n.title = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        NewsItem.model_validate({**n.model_dump(), "surprise": 1})


def test_keys_of_a_story() -> None:
    assert item(assets=("BTC", "ETH")).keys == ("BTC", "ETH")
    assert item(is_market_wide=True).keys == ("MARKET",)


def test_event_score_and_json() -> None:
    e = event(probs=(0.06, 0.21, 0.73))
    assert e.choice == 2 and e.label == "BULLISH"
    assert e.score == pytest.approx(0.67)
    d = e.model_dump(mode="json")
    assert d["probabilities"] == {"BEARISH": 0.06, "NEUTRAL": 0.21, "BULLISH": 0.73}
    assert d["ingested_at"] == to_iso(T0)


def test_event_rejects_label_choice_mismatch_and_bad_probabilities() -> None:
    good = event().model_dump()
    with pytest.raises(ValidationError, match="does not match"):
        SentimentEvent.model_validate({**good, "label": "BEARISH"})
    with pytest.raises(ValidationError, match="sum to 1"):
        SentimentEvent.model_validate({**good, "probabilities": {"BEARISH": 0.5, "NEUTRAL": 0.5, "BULLISH": 0.5}})
    with pytest.raises(ValidationError, match="keys"):
        SentimentEvent.model_validate({**good, "probabilities": {"BEARISH": 1.0}})
