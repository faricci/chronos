from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from modules.market.backfill import BarStore, load_bars
from modules.market.timeframes import (
    OHLCV,
    Bar,
    BarGapError,
    TimeframeAggregator,
    aggregate_bars,
    bucket_start,
)

REAL_DATA = Path(__file__).resolve().parent.parent / "data" / "bars" / "1m"


def synthetic(start: datetime, n: int, seed: int = 7) -> pd.DataFrame:
    """Contiguous random-walk 1m bars with some flat (zero-volume) minutes."""
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.0005, n)))
    open_ = np.concatenate([[close[0]], close[:-1]])
    spread = np.abs(rng.normal(0, 0.0003, n)) * close
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    volume = rng.exponential(5.0, n)
    volume[rng.random(n) < 0.1] = 0.0
    idx = pd.date_range(start, periods=n, freq="1min", name="ts")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx)


def to_bars(df: pd.DataFrame) -> list[Bar]:
    return [Bar(t.to_pydatetime(), *map(float, row)) for t, row in zip(df.index, df[OHLCV].to_numpy(), strict=True)]


def run_incremental(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    agg = TimeframeAggregator(tf)
    done = [b for b in (agg.update(x) for x in to_bars(df)) if b is not None]
    out = pd.DataFrame(
        [(b.ts, b.open, b.high, b.low, b.close, b.volume) for b in done], columns=["ts", *OHLCV]
    ).set_index("ts")
    out.index = pd.DatetimeIndex(out.index, tz="UTC", name="ts")
    return out


def assert_same(batch: pd.DataFrame, inc: pd.DataFrame) -> None:
    assert list(batch.index) == list(inc.index)
    for col in ("open", "high", "low", "close"):
        assert (batch[col].to_numpy() == inc[col].to_numpy()).all(), col  # no arithmetic involved: exact
    np.testing.assert_allclose(batch["volume"].to_numpy(), inc["volume"].to_numpy(), rtol=1e-12, atol=0)


@pytest.mark.parametrize("tf", ["15m", "1h", "6h", "1d"])
@pytest.mark.parametrize("start_offset_min", [0, 37])  # aligned and cold-start in the middle of a bucket
def test_batch_and_incremental_agree(tf: str, start_offset_min: int) -> None:
    df = synthetic(datetime(2026, 1, 1, 0, 0, tzinfo=UTC) + timedelta(minutes=start_offset_min), 5000)
    batch, inc = aggregate_bars(df, tf), run_incremental(df, tf)
    assert len(batch) > 0
    assert_same(batch, inc)


def test_buckets_are_utc_epoch_aligned() -> None:
    df = synthetic(datetime(2026, 3, 1, 0, 0, tzinfo=UTC), 2 * 1440)
    assert [t.hour for t in aggregate_bars(df, "6h").index[:5]] == [0, 6, 12, 18, 0]
    daily = aggregate_bars(df, "1d")
    assert list(daily.index) == [pd.Timestamp("2026-03-01", tz="UTC"), pd.Timestamp("2026-03-02", tz="UTC")]
    assert daily.iloc[0]["open"] == df.iloc[0]["open"] and daily.iloc[0]["close"] == df.iloc[1439]["close"]
    assert daily.iloc[0]["high"] == df.iloc[:1440]["high"].max()
    assert daily.iloc[0]["volume"] == pytest.approx(df.iloc[:1440]["volume"].sum())


def test_partial_buckets_are_never_exposed() -> None:
    df = synthetic(datetime(2026, 3, 1, 5, 30, tzinfo=UTC), 150)  # 05:30 -> 08:00 (exclusive)
    hourly = aggregate_bars(df, "1h")
    assert [t.hour for t in hourly.index] == [6, 7]  # 05:00 started late, 08:00 not yet closed
    assert aggregate_bars(df, "6h").empty


def test_incremental_emits_on_the_last_minute_only() -> None:
    agg = TimeframeAggregator("1h")
    df = synthetic(datetime(2026, 3, 1, 10, 0, tzinfo=UTC), 61)
    emitted = [agg.update(b) for b in to_bars(df)]
    assert [i for i, e in enumerate(emitted) if e is not None] == [59]
    assert emitted[59] is not None and emitted[59].ts == datetime(2026, 3, 1, 10, 0, tzinfo=UTC)


def test_gaps_are_rejected_by_both_implementations() -> None:
    df = synthetic(datetime(2026, 3, 1, 0, 0, tzinfo=UTC), 100).drop(index=pd.Timestamp("2026-03-01 00:40", tz="UTC"))
    with pytest.raises(BarGapError):
        aggregate_bars(df, "1h")
    agg = TimeframeAggregator("1h")
    with pytest.raises(BarGapError):
        for b in to_bars(df):
            agg.update(b)


def test_duplicate_or_out_of_order_minutes_are_rejected() -> None:
    agg = TimeframeAggregator("1h")
    bars = to_bars(synthetic(datetime(2026, 3, 1, 0, 0, tzinfo=UTC), 3))
    agg.update(bars[0])
    with pytest.raises(BarGapError):
        agg.update(bars[0])


def test_unknown_timeframe_and_empty_input() -> None:
    df = synthetic(datetime(2026, 3, 1, tzinfo=UTC), 10)
    with pytest.raises(ValueError, match="unknown timeframe"):
        aggregate_bars(df, "2h")
    with pytest.raises(ValueError, match="unknown timeframe"):
        TimeframeAggregator("3d")
    assert aggregate_bars(df.iloc[:0], "1h").empty


def test_bucket_start() -> None:
    t = datetime(2026, 3, 1, 13, 47, 12, tzinfo=UTC)
    assert bucket_start(t, 3600) == datetime(2026, 3, 1, 13, 0, tzinfo=UTC)
    assert bucket_start(t, 21600) == datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    assert bucket_start(t, 86400) == datetime(2026, 3, 1, 0, 0, tzinfo=UTC)


@pytest.mark.skipif(not (REAL_DATA / "product=BTC-EUR").exists(), reason="run the backfill first (sez. 0.5)")
@pytest.mark.parametrize("tf", ["1h", "6h", "1d"])
def test_batch_and_incremental_agree_on_real_history(tf: str) -> None:
    store = BarStore(REAL_DATA)
    months = store.months("BTC-EUR")
    first = store.read_month("BTC-EUR", *months[0])["ts"].min().to_pydatetime()
    bars = load_bars(REAL_DATA, "BTC-EUR", start=first, end=first + timedelta(days=12))  # clamped to stored data
    if len(bars) < 5000:
        pytest.skip("not enough backfilled history yet")
    assert_same(aggregate_bars(bars, tf), run_incremental(bars, tf))
