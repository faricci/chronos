"""SentimentAggregator (spec sez. 1.1.3, 1.3 punto 7): formulas, decay, point-in-time (C7)."""

from __future__ import annotations

import math
from datetime import timedelta

import pytest

from modules.config import AggregationConfig, HalfLife
from modules.sentiment.aggregator import SentimentAggregator
from tests.sentiment_helpers import T0, event

MIN = timedelta(minutes=1)


def agg(**kw: object) -> SentimentAggregator:
    return SentimentAggregator(AggregationConfig(**kw))  # type: ignore[arg-type]


def test_empty_snapshot_is_neutral_and_flagged_by_none() -> None:
    s = agg().snapshot(T0, "SOL")
    assert (s.asset.ewm, s.asset.mass, s.asset.disp, s.asset.shock_15m, s.asset.n_24h) == (0.0, 0.0, 0.0, 0.0, 0)
    assert s.asset.last_choice is None and s.asset.last_age_min is None and s.asset.last_conf is None
    assert s.market.n_1h == 0 and s.fear_greed is None


def test_single_event_matches_the_formulas() -> None:
    a = agg()
    e = event(probs=(0.06, 0.21, 0.73), confidence=0.41, relevance=1.0, ingested_at=T0)
    a.add(e, source_count=1)
    s = a.snapshot(T0 + 3 * MIN, "SOL").asset
    w = 0.41 * 1.0 * math.log(2.0) * 2.0 ** (-3 / 90)
    assert s.mass == pytest.approx(w, abs=1e-12)
    assert s.mass_slow == pytest.approx(0.41 * math.log(2.0) * 2.0 ** (-3 / 1440), abs=1e-12)
    assert s.ewm == pytest.approx(0.67, abs=1e-9)  # one event: ewm = its score
    assert s.disp == 0.0
    assert s.shock_15m == pytest.approx(0.67 * 0.41)
    assert (s.n_1h, s.n_24h, s.last_choice, s.last_age_min) == (1, 1, 2, 3)
    assert s.last_conf == 0.41


def test_decay_halves_the_weight_after_one_half_life() -> None:
    a = agg()
    a.add(event(ingested_at=T0, confidence=0.8, relevance=0.6), source_count=3)
    m0 = a.asset_snapshot("SOL", T0).mass
    assert m0 == pytest.approx(0.8 * 0.6 * math.log(4.0), abs=1e-12)
    for hl_min, field in ((90, "mass"), (1440, "mass_slow")):
        at_hl = a.asset_snapshot("SOL", T0 + hl_min * MIN)
        assert getattr(at_hl, field) == pytest.approx(getattr(a.asset_snapshot("SOL", T0), field) / 2, abs=1e-9)
    assert a.asset_snapshot("SOL", T0 + 180 * MIN).mass == pytest.approx(m0 / 4, abs=1e-9)


def test_market_key_uses_the_market_half_lives() -> None:
    a = agg(half_life_min=HalfLife(asset=90, market=240), half_life_slow_min=HalfLife(asset=1440, market=2880))
    a.add(event(key="MARKET", ingested_at=T0, confidence=1.0, relevance=1.0), source_count=1)
    m0 = a.market_snapshot(T0)
    assert a.market_snapshot(T0 + 240 * MIN).mass == pytest.approx(m0.mass / 2, abs=1e-9)
    assert a.market_snapshot(T0 + 2880 * MIN).mass_slow == pytest.approx(m0.mass_slow / 2, abs=1e-9)


def test_ewm_is_the_weighted_mean_and_stays_in_range() -> None:
    a = agg()
    a.add(event(probs=(0.0, 0.0, 1.0), confidence=1.0, news_id="a", ingested_at=T0), 1)  # s = +1
    a.add(event(probs=(1.0, 0.0, 0.0), confidence=0.5, news_id="b", ingested_at=T0), 1)  # s = -1, half the weight
    s = a.asset_snapshot("SOL", T0)
    assert s.ewm == pytest.approx((1.0 * 1.0 - 1.0 * 0.5) / 1.5, abs=1e-9)
    assert -1.0 <= s.ewm <= 1.0
    # weighted standard deviation around the mean
    mean = 1 / 3
    var = (1.0 * (1 - mean) ** 2 + 0.5 * (-1 - mean) ** 2) / 1.5
    assert s.disp == pytest.approx(math.sqrt(var), abs=1e-9)


def test_source_count_raises_the_weight_by_log1p() -> None:
    one, many = agg(), agg()
    one.add(event(news_id="n"), source_count=1)
    many.add(event(news_id="n"), source_count=5)
    assert many.asset_snapshot("SOL", T0).mass / one.asset_snapshot("SOL", T0).mass == pytest.approx(
        math.log(6.0) / math.log(2.0)
    )


def test_reposts_count_only_from_their_own_time() -> None:
    a = agg()
    a.add(event(news_id="n", ingested_at=T0), source_count=1)
    a.register_repost("n", T0 + 10 * MIN)
    base = math.log(2.0)
    before = a.asset_snapshot("SOL", T0 + 5 * MIN).mass / (0.5 * 2.0 ** (-5 / 90))
    after = a.asset_snapshot("SOL", T0 + 10 * MIN).mass / (0.5 * 2.0 ** (-10 / 90))
    assert before == pytest.approx(base)
    assert after == pytest.approx(math.log(3.0))


# ----------------------------------------------------------------------- point-in-time (C7)


def test_no_lookahead_an_event_after_as_of_does_not_change_the_snapshot() -> None:
    a = agg()
    a.add(event(news_id="a", ingested_at=T0, probs=(0.1, 0.2, 0.7)), 1)
    a.add(event(news_id="m", key="MARKET", ingested_at=T0, probs=(0.7, 0.2, 0.1)), 1)
    as_of = T0 + 5 * MIN
    before = a.snapshot(as_of, "SOL")
    a.add(
        event(news_id="future", ingested_at=as_of + timedelta(milliseconds=1), probs=(0.0, 0.0, 1.0), confidence=1), 1
    )
    a.add(event(news_id="future-m", key="MARKET", ingested_at=as_of + 10 * MIN, probs=(1.0, 0.0, 0.0)), 1)
    assert a.snapshot(as_of, "SOL") == before
    # ...and it does show up once as_of reaches it
    assert a.snapshot(as_of + 10 * MIN, "SOL") != before


def test_event_exactly_at_as_of_is_visible() -> None:
    a = agg()
    a.add(event(ingested_at=T0), 1)
    assert a.asset_snapshot("SOL", T0).n_1h == 1
    assert a.asset_snapshot("SOL", T0 - timedelta(milliseconds=1)).n_1h == 0


def test_snapshot_does_not_mutate_and_is_repeatable() -> None:
    a = agg()
    for i in range(5):
        a.add(event(news_id=f"n{i}", ingested_at=T0 + i * MIN), 1)
    first = a.snapshot(T0 + 10 * MIN, "SOL")
    a.snapshot(T0 + 1000 * MIN, "SOL")  # a far-future query must not prune or alter anything
    assert a.snapshot(T0 + 10 * MIN, "SOL") == first
    assert a.n_events("SOL") == 5


def test_insertion_order_does_not_matter() -> None:
    evs = [
        event(news_id=f"n{i}", ingested_at=T0 + i * MIN, probs=((i % 3) / 4, 0.1, 0.9 - (i % 3) / 4)) for i in range(8)
    ]
    fwd, rev = agg(), agg()
    for e in evs:
        fwd.add(e, 1)
    for e in reversed(evs):
        rev.add(e, 1)
    assert fwd.snapshot(T0 + 20 * MIN, "SOL") == rev.snapshot(T0 + 20 * MIN, "SOL")


def test_ingested_at_and_not_published_at_is_what_counts() -> None:
    # the event carries only ingested_at; a story published long ago but ingested now is as fresh as any other
    a = agg()
    a.add(event(ingested_at=T0), 1)
    assert a.asset_snapshot("SOL", T0 + 1 * MIN).last_age_min == 1


# --------------------------------------------------------------------------- windows


def test_shock_window_and_counts() -> None:
    a = agg()
    a.add(event(news_id="old", ingested_at=T0, probs=(0.0, 0.0, 1.0), confidence=1.0), 1)  # |s|*conf = 1
    a.add(event(news_id="mid", ingested_at=T0 + 20 * MIN, probs=(0.0, 0.5, 0.5), confidence=0.4), 1)  # 0.5*0.4 = 0.2
    s = a.asset_snapshot("SOL", T0 + 30 * MIN)
    assert s.shock_15m == pytest.approx(0.2)  # the first event is 30 min old: outside the 15 min window
    assert s.n_1h == 2 and s.n_24h == 2
    s2 = a.asset_snapshot("SOL", T0 + 61 * MIN)
    assert (s2.n_1h, s2.n_24h) == (1, 2)
    s3 = a.asset_snapshot("SOL", T0 + 25 * 60 * MIN)
    assert (s3.n_1h, s3.n_24h) == (0, 0)
    assert s3.shock_15m == 0.0 and s3.last_choice is not None  # still inside retention: "last" is known


def test_retention_prunes_and_ignores_old_events() -> None:
    a = agg(retention_days=7)
    a.add(event(news_id="old", ingested_at=T0), 1)
    a.add(event(news_id="new", ingested_at=T0 + timedelta(days=8)), 1)
    assert a.n_events("SOL") == 1  # pruned at add
    assert a.asset_snapshot("SOL", T0 + timedelta(days=8)).last_age_min == 0
    # an event older than retention is invisible even if still held
    b = agg(retention_days=7)
    b.add(event(ingested_at=T0), 1)
    assert b.asset_snapshot("SOL", T0 + timedelta(days=7, minutes=1)).last_choice is None


def test_duplicate_event_is_counted_once() -> None:
    a = agg()
    e = event(news_id="n")
    assert a.add(e, 1) is True
    assert a.add(e, 1) is False
    assert a.n_events() == 1


def test_one_story_for_two_keys_counts_in_each() -> None:
    a = agg()
    a.add(event(news_id="n", key="SOL"), 1)
    a.add(event(news_id="n", key="BTC", relevance=0.6), 1)
    assert a.asset_snapshot("SOL", T0).n_1h == 1 and a.asset_snapshot("BTC", T0).n_1h == 1
    assert a.asset_snapshot("BTC", T0).mass == pytest.approx(0.6 * a.asset_snapshot("SOL", T0).mass)


def test_load_from_store_rows() -> None:
    a = agg()
    rows = [(event(news_id=f"n{i}", ingested_at=T0 + i * MIN), 2) for i in range(3)]
    assert a.load(rows) == 3
    assert a.asset_snapshot("SOL", T0 + 5 * MIN).n_1h == 3
