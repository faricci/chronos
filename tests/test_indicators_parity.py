"""Batch/incremental parity and point-in-time rules of the market features (spec sez. 2.2.1.b, test T1-T7)."""

from __future__ import annotations

import math
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from modules.market.backfill import BarStore, load_bars
from modules.market.indicator_defs import FEATURE_COLUMNS
from modules.market.indicators import (
    EmaState,
    IndicatorEngine,
    IndicatorSnapshot,
    PrevPercentile,
    PrevZScore,
    RollingExtreme,
    RollingSum,
    WilderState,
)
from modules.market.indicators_batch import (
    compute_indicators,
    ema_sma_seed,
    htf_asof,
    macd_hist,
    pctile_prev,
    rv_per_day_bps,
    true_range,
    wilder,
    wilder_atr,
    wilder_rsi,
    zscore_prev,
)
from modules.market.timeframes import Bar, BarGapError
from tests.indicator_fixtures import assert_parity, constant_bars, stream_rows, synthetic_bars

REAL_DATA = Path(__file__).resolve().parent.parent / "data" / "bars" / "1m"
PRODUCTS = ("BTC-EUR", "ETH-EUR", "SOL-EUR")


@pytest.fixture(scope="module")
def bars() -> pd.DataFrame:
    return synthetic_bars()


@pytest.fixture(scope="module")
def batch(bars: pd.DataFrame) -> pd.DataFrame:
    return compute_indicators(bars)


@pytest.fixture(scope="module")
def streamed(bars: pd.DataFrame) -> pd.DataFrame:
    return stream_rows(IndicatorEngine("TEST-EUR"), bars)


# --------------------------------------------------------------------------------------- T1


def test_parity_synthetic(batch: pd.DataFrame, streamed: pd.DataFrame) -> None:
    """Every row, every column, from the very first bar (200-day style series with quiet stretches, a day without
    volume and a stretch of constant price)."""
    assert_parity(streamed, batch)
    # the series is long enough for the slowest features to exist
    assert batch.iloc[-1].notna().all()
    assert batch["atr_1d_pctile_365d"].notna().sum() > 0 and batch["vol_z_1d"].notna().sum() > 0


# --------------------------------------------------------------------------------------- T7


def test_warmup_equals_stream(bars: pd.DataFrame, streamed: pd.DataFrame) -> None:
    split = 100 * 1440 + 777  # an arbitrary point in the middle of a day, an hour and a 6h bucket
    engine = IndicatorEngine("TEST-EUR")
    engine.warm_up(bars.iloc[:split])
    tail = stream_rows(engine, bars.iloc[split : split + 5000])
    assert_parity(tail, streamed.iloc[split : split + 5000])


def test_warmup_alone_leaves_the_engine_ready(bars: pd.DataFrame, batch: pd.DataFrame) -> None:
    engine = IndicatorEngine("TEST-EUR")
    engine.warm_up(bars.iloc[:50_000])
    row = engine.snapshot().as_row()
    assert_parity(pd.DataFrame([row])[list(FEATURE_COLUMNS)], batch.iloc[[49_999]].reset_index(drop=True))


# --------------------------------------------------------------------------------------- T3


def _cut_points() -> list[int]:
    """Rows around every boundary that matters: 23:58, 23:59, 00:00, 05:59, 06:00 and ordinary xx:59 minutes."""
    base = 110 * 1440
    minutes = [23 * 60 + 58, 23 * 60 + 59, 24 * 60, 5 * 60 + 59, 6 * 60, 12 * 60 - 1, 12 * 60, 13 * 60 - 1, 7 * 60 + 31]
    return [base + m for m in minutes] + [105 * 1440 + 1439, 120 * 1440 + 3]


@pytest.mark.parametrize("i", _cut_points())
def test_no_lookahead_truncation(bars: pd.DataFrame, batch: pd.DataFrame, i: int) -> None:
    """Features of row ``i`` computed on the history cut at ``i`` equal the ones computed with the future present."""
    truncated = compute_indicators(bars.iloc[: i + 1]).iloc[[-1]]
    assert_parity(truncated.reset_index(drop=True), batch.iloc[[i]].reset_index(drop=True))


# --------------------------------------------------------------------------------------- T4


def test_htf_visibility_boundary(batch: pd.DataFrame) -> None:
    """A higher-timeframe value changes exactly on the 1m row of its last minute, not before."""

    def at(day: int, hh: int, mm: int) -> int:
        return day * 1440 + hh * 60 + mm

    d = 100
    # 1d: unchanged through D 23:58, new on D 23:59
    assert batch["atr_14_1d"].iloc[at(d, 23, 58)] == batch["atr_14_1d"].iloc[at(d - 1, 23, 59)]
    assert batch["atr_14_1d"].iloc[at(d, 23, 59)] != batch["atr_14_1d"].iloc[at(d, 23, 58)]
    assert batch["atr_14_1d"].iloc[at(d + 1, 0, 0)] == batch["atr_14_1d"].iloc[at(d, 23, 59)]
    # 1h: new on xx:59
    assert batch["atr_14_1h"].iloc[at(d, 9, 58)] == batch["atr_14_1h"].iloc[at(d, 8, 59)]
    assert batch["atr_14_1h"].iloc[at(d, 9, 59)] != batch["atr_14_1h"].iloc[at(d, 9, 58)]
    # 6h: new on 05:59 (and not at 06:00 or in between)
    assert batch["atr_14_6h"].iloc[at(d, 5, 58)] == batch["atr_14_6h"].iloc[at(d - 1, 23, 59)]
    assert batch["atr_14_6h"].iloc[at(d, 5, 59)] != batch["atr_14_6h"].iloc[at(d, 5, 58)]
    assert batch["atr_14_6h"].iloc[at(d, 6, 0)] == batch["atr_14_6h"].iloc[at(d, 5, 59)]
    # the 1m price features, by contrast, move minute by minute (quiet stretches excepted)
    hour = batch["ret_15m_bps"].iloc[at(d, 9, 0) : at(d, 10, 0)]
    assert hour.nunique() > 20


def test_htf_asof_places_values_on_the_last_minute_of_each_bar() -> None:
    idx = pd.date_range("2024-01-01", periods=180, freq="1min", tz="UTC")
    htf = pd.DataFrame({"x": [1.0, 2.0]}, index=pd.DatetimeIndex(["2024-01-01 00:00", "2024-01-01 01:00"], tz="UTC"))
    out = htf_asof(htf, "1h", idx)["x"]
    assert out.iloc[:59].isna().all() and out.iloc[59] == 1.0 and out.iloc[118] == 1.0
    assert out.iloc[119] == 2.0 and out.iloc[179] == 2.0


# --------------------------------------------------------------------------------------- T5


def test_atr_reference_values() -> None:
    # TR_0 = H-L; TR_k = max(H-L, |H-C_{k-1}|, |L-C_{k-1}|)
    high = np.array([12.0, 13.0, 14.5, 14.0, 15.0, 16.0, 15.5, 16.5, 17.0, 18.0, 17.5, 18.5, 19.0, 20.0, 21.0, 20.0])
    low = np.array([10.0, 11.0, 12.0, 12.5, 13.0, 14.5, 14.0, 15.0, 15.5, 16.0, 16.5, 17.0, 17.5, 18.0, 19.0, 18.5])
    close = np.array([11.0, 12.5, 13.0, 13.5, 14.5, 15.0, 15.0, 16.0, 16.5, 17.0, 17.0, 18.0, 18.5, 19.5, 20.0, 19.0])
    tr = [high[0] - low[0]] + [
        max(high[k] - low[k], abs(high[k] - close[k - 1]), abs(low[k] - close[k - 1])) for k in range(1, 16)
    ]
    assert tr[:3] == [2.0, 2.0, 2.5]  # TR_2 = max(2.5, |14.5-12.5|, |12-12.5|)
    np.testing.assert_array_equal(true_range(high, low, close), tr)
    atr = wilder_atr(high, low, close)
    assert np.isnan(atr[:13]).all()
    first = sum(tr[:14]) / 14
    assert atr[13] == pytest.approx(first, abs=0, rel=1e-15)
    assert atr[14] == pytest.approx((first * 13 + tr[14]) / 14, rel=1e-15)
    assert atr[15] == pytest.approx((atr[14] * 13 + tr[15]) / 14, rel=1e-15)


def test_rsi_reference_values() -> None:
    # 14 differences: 10 of +1 and 4 of -1 -> avgG = 10/14, avgP = 4/14 -> RSI = 100 - 100/(1 + 10/4)
    deltas = [1, 1, -1, 1, 1, 1, -1, 1, 1, -1, 1, 1, -1, 1]
    close = np.concatenate([[100.0], 100.0 + np.cumsum(deltas)])
    rsi = wilder_rsi(close)
    assert np.isnan(rsi[:14]).all() and not np.isnan(rsi[14])
    assert rsi[14] == pytest.approx(100.0 - 100.0 / 3.5, rel=1e-12)
    assert rsi[14] == pytest.approx(71.42857142857143, rel=1e-12)
    # next step: one more -1 -> avgG = 10/14*13/14, avgP = (4/14*13 + 1)/14
    close2 = np.append(close, close[-1] - 1.0)
    ag, ap = (10 / 14) * 13 / 14, ((4 / 14) * 13 + 1) / 14
    assert wilder_rsi(close2)[15] == pytest.approx(100.0 - 100.0 / (1 + ag / ap), rel=1e-12)


def test_ema_seed_and_first_macd_histogram_index() -> None:
    x = np.arange(1.0, 31.0)
    ema = ema_sma_seed(x, 10)
    alpha = 2 / 11
    assert np.isnan(ema[:9]).all() and ema[9] == 5.5
    assert ema[10] == pytest.approx(alpha * 11 + (1 - alpha) * 5.5, rel=1e-15)
    # leading NaN is allowed: the seed is the mean of the first n valid inputs
    shifted = ema_sma_seed(np.concatenate([[np.nan] * 3, x]), 10)
    assert np.isnan(shifted[:12]).all() and shifted[12] == 5.5
    close = 100 + np.sin(np.arange(60) / 5.0) * 5
    hist = macd_hist(close)
    assert np.isnan(hist[:33]).all() and not np.isnan(hist[33])  # MACD from k=25, signal seeded 9 values later


def test_percentile_excludes_the_current_value_and_counts_ties() -> None:
    x = np.array([5.0, 3.0, 4.0, 4.0, 9.0, 1.0])
    out = pctile_prev(x, window=5, min_n=3)
    assert np.isnan(out[:3]).all()
    assert out[3] == pytest.approx(100.0 * 2 / 3)  # prev [5,3,4]: 3 and 4 are <= 4 (the 4 at k=3 excluded)
    assert out[4] == 100.0  # prev [5,3,4,4]: all <= 9
    assert out[5] == 0.0  # prev [3,4,4,9] (window 5 keeps the last 5 = [3,4,4,9] plus 5): none <= 1


def test_percentile_window_is_by_bars_and_ignores_nan() -> None:
    x = np.array([np.nan, np.nan, 1.0, 2.0, 3.0, 2.5])
    out = pctile_prev(x, window=4, min_n=2)
    # k=4: positions 0..3 -> prev available [1,2] (2 values) -> 3.0 is above both
    assert out[4] == 100.0
    # k=5: positions 1..4 -> prev available [1,2,3] -> 2.5 >= 1 and 2 -> 2/3
    assert out[5] == pytest.approx(100.0 * 2 / 3)


def test_zscore_uses_ddof_1_and_excludes_the_current_value() -> None:
    out = zscore_prev(np.array([1.0, 2.0, 3.0, 4.0, 10.0]), window=3)
    assert np.isnan(out[:3]).all()
    assert out[3] == pytest.approx((4 - 2) / 1.0)  # prev [1,2,3]: mean 2, sd 1
    prev = np.array([2.0, 3.0, 4.0])
    assert out[4] == pytest.approx((10 - 3) / prev.std(ddof=1))


def test_rvd_of_constant_growth() -> None:
    close = 100.0 * 1.001 ** np.arange(60)
    rv = rv_per_day_bps(close, 24)
    assert np.isnan(rv[:24]).all()
    assert rv[24] == pytest.approx(1e4 * math.sqrt(24) * math.log(1.001), rel=1e-12)  # sqrt(sum of 24 r^2)
    half = rv_per_day_bps(close, 12)
    assert half[12] == pytest.approx(1e4 * math.sqrt(24 / 12 * 12) * math.log(1.001), rel=1e-12)  # same daily scale


def test_minimum_history_of_every_feature(batch: pd.DataFrame) -> None:
    """First row index (0-based) at which each column becomes available, against the table of sez. 2.2.1.b."""
    first = {c: int(batch[c].notna().to_numpy().argmax()) if batch[c].notna().any() else None for c in batch.columns}
    assert first["ret_15m_bps"] == 15 and first["ret_7d_bps"] == 10080
    assert first["dist_vwap_1d_bps"] == 1439 and first["hh_ll_pos_30d"] == 43199
    # the first complete 1h bar ends at row 59: 14 bars for ATR, 15 for RSI, 721 for the ratio
    assert first["atr_14_1h"] == 14 * 60 - 1 and first["rsi_14_1h"] == 15 * 60 - 1
    assert first["rv_1d_bps"] == 25 * 60 - 1 and first["rv_ratio_1d_30d"] == 721 * 60 - 1
    assert first["seq_1h_bps_00"] == 13 * 60 - 1 and first["seq_1d_bps_0"] == 8 * 1440 - 1
    assert first["atr_14_6h"] == 14 * 360 - 1 and first["rsi_14_6h"] == 15 * 360 - 1
    assert first["atr_14_1d"] == 14 * 1440 - 1 and first["rsi_14_1d"] == 15 * 1440 - 1
    assert first["bb_pos_1d"] == 20 * 1440 - 1 and first["macd_hist_1d_bps"] == 34 * 1440 - 1
    assert first["vol_z_1d"] == 31 * 1440 - 1 and first["atr_1d_pctile_365d"] == 104 * 1440 - 1


def _same(a: float, b: float) -> bool:
    return (math.isnan(a) and math.isnan(b)) or a == b


def test_state_classes_match_their_batch_functions_on_data_full_of_ties() -> None:
    """The synthetic series has continuous values, so ties are tested here, directly, class by class."""
    rng = np.random.default_rng(5)
    x = rng.integers(0, 6, 700).astype(float)
    x[:30] = np.nan  # NaN prefix, like the ATR before its seed
    window, min_n = 50, 10
    pct = PrevPercentile(window, min_n)
    assert all(_same(pct.update(float(v)), float(e)) for v, e in zip(x, pctile_prev(x, window, min_n), strict=True))

    y = rng.integers(0, 4, 300).astype(float)
    z = PrevZScore(20)
    assert all(_same(z.update(float(v)), float(e)) for v, e in zip(y, zscore_prev(y, 20), strict=True))

    u = rng.normal(0.0, 1.0, 400)
    w = WilderState(14)
    assert all(_same(w.update(float(v)), float(e)) for v, e in zip(u, wilder(u, 14), strict=True))
    ema = EmaState(12)
    assert all(_same(ema.update(float(v)), float(e)) for v, e in zip(u, ema_sma_seed(u, 12), strict=True))


def test_rolling_extreme_and_sum_match_pandas() -> None:
    rng = np.random.default_rng(8)
    x = rng.integers(0, 20, 500).astype(float)  # many equal values
    hi, lo, total = RollingExtreme(37, "max"), RollingExtreme(37, "min"), RollingSum(37)
    got_hi = [hi.update(i, float(v)) for i, v in enumerate(x)]
    got_lo = [lo.update(i, float(v)) for i, v in enumerate(x)]
    for v in x:
        total.update(float(v))
    ref = pd.Series(x).rolling(37, min_periods=37)
    np.testing.assert_array_equal(got_hi, ref.max().to_numpy())
    np.testing.assert_array_equal(got_lo, ref.min().to_numpy())
    assert total.value == ref.sum().iloc[-1]
    assert math.isnan(RollingSum(5).value)  # not full yet


# --------------------------------------------------------------------------------------- T6


def test_degenerate_cases_give_nan_not_conventions() -> None:
    bars = constant_bars(days=45, volume=1.0)  # constant price and volume
    out = compute_indicators(bars)
    last = out.iloc[-1]
    assert last["rsi_14_1h"] == 50.0 and last["rsi_14_1d"] == 50.0  # no gains, no losses: the RSI convention
    assert math.isnan(last["bb_pos_1d"])  # zero band width
    assert math.isnan(last["hh_ll_pos_30d"])  # HH == LL
    assert math.isnan(last["rv_ratio_1d_30d"])  # zero denominator
    assert math.isnan(last["vol_z_1d"])  # zero standard deviation
    assert last["atr_14_1d"] == 0.0 and last["atr_14_1d_bps"] == 0.0  # zero true range is a valid ATR
    assert last["dist_vwap_1d_bps"] == pytest.approx(0.0, abs=1e-9)
    inc = stream_rows(IndicatorEngine("X"), bars.iloc[-5000:]) if False else None  # parity covered by T1 on rich data
    assert inc is None
    engine = IndicatorEngine("X")
    assert_parity(stream_rows(engine, bars), out)


def test_no_volume_window_has_no_vwap() -> None:
    bars = synthetic_bars(days=4, zero_volume_day=None, constant_days=None)
    bars.loc[bars.index[: 2 * 1440], "volume"] = 0.0
    out = compute_indicators(bars)
    assert out["dist_vwap_1d_bps"].iloc[1439 : 2 * 1440 - 1].isna().all()  # a full window without any traded minute
    assert out["dist_vwap_1d_bps"].iloc[3 * 1440 + 10 :].notna().all()
    assert_parity(stream_rows(IndicatorEngine("X"), bars), out)


def test_non_contiguous_input_is_rejected() -> None:
    bars = synthetic_bars(days=2, zero_volume_day=None, constant_days=None).drop(
        index=pd.Timestamp("2024-01-01 10:00", tz="UTC")
    )
    with pytest.raises(BarGapError):
        compute_indicators(bars)
    engine = IndicatorEngine("X")
    with pytest.raises(BarGapError):
        stream_rows(engine, bars)


def test_empty_and_short_inputs() -> None:
    empty = compute_indicators(
        pd.DataFrame(
            columns=["open", "high", "low", "close", "volume"], index=pd.DatetimeIndex([], tz="UTC", name="ts")
        )
    )
    assert list(empty.columns) == list(FEATURE_COLUMNS) and empty.empty
    short = compute_indicators(synthetic_bars(days=1, zero_volume_day=None, constant_days=None).iloc[:50])
    assert short.isna().all().all() or short.drop(columns=["ret_15m_bps"]).isna().all().all()
    with pytest.raises(ValueError, match="no bar"):
        IndicatorEngine("X").snapshot()


# ------------------------------------------------------------------------------ snapshot plumbing


def test_snapshot_round_trip_and_decision_instant(bars: pd.DataFrame, batch: pd.DataFrame) -> None:
    engine = IndicatorEngine("TEST-EUR")
    engine.warm_up(bars.iloc[:60_000])
    snap = engine.update(Bar(bars.index[60_000].to_pydatetime(), *bars.iloc[60_000].tolist()))
    assert snap.t == snap.ts + timedelta(minutes=1)  # the decision instant is the close of the bar
    row = snap.as_row()
    assert list(row) == list(FEATURE_COLUMNS)
    again = IndicatorSnapshot.from_row("TEST-EUR", snap.ts, row)
    # training (from_row) and serving (update) build the same object; NaN == NaN here
    np.testing.assert_equal(again.as_row(), snap.as_row())
    assert (again.seq_1h_bps, again.seq_1d_bps, again.t) == (snap.seq_1h_bps, snap.seq_1d_bps, snap.t)
    assert snap.seq_1h_bps is not None and len(snap.seq_1h_bps) == 12
    assert snap.seq_1d_bps is not None and len(snap.seq_1d_bps) == 7
    early = IndicatorEngine("TEST-EUR").update(Bar(bars.index[0].to_pydatetime(), *bars.iloc[0].tolist()))
    assert early.seq_1h_bps is None and early.seq_1d_bps is None  # never partial lists
    assert math.isnan(early.atr_14_1d)


def test_running_sums_do_not_drift_over_a_long_run() -> None:
    """The daily exact resum keeps the VWAP sums equal to a fresh fsum after months of add/subtract."""
    bars = synthetic_bars(days=60, zero_volume_day=None, constant_days=None, seed=3)
    engine = IndicatorEngine("X")
    engine.warm_up(bars)
    pv = ((bars["high"] + bars["low"] + bars["close"]) / 3.0 * bars["volume"]).to_numpy()[-1440:]
    # resummed at the 23:59 close of the last day: identical to the exact sum
    assert engine._pv.value == math.fsum(pv)


# ------------------------------------------------------------------------ T2: the real history


@pytest.mark.data
@pytest.mark.parametrize("product", PRODUCTS)
def test_parity_history(product: str) -> None:
    """P0 exit criterion: warm up on the whole stored history except the last N rows, stream those N, compare."""
    if not (REAL_DATA / f"product={product}").exists() or not BarStore(REAL_DATA).months(product):
        pytest.skip("run the backfill first (sez. 0.5)")
    history = load_bars(REAL_DATA, product)
    n = 20_000
    if len(history) < 60_000:
        pytest.skip(f"only {len(history)} rows of {product} stored")
    ref = compute_indicators(history).iloc[-n:]
    engine = IndicatorEngine(product)
    engine.warm_up(history.iloc[:-n])
    assert_parity(stream_rows(engine, history.iloc[-n:]), ref)
    full = int(ref.notna().all(axis=1).sum())
    assert full >= 5000 or len(history) < 150 * 1440, f"{product}: only {full} rows with every column available"
