"""Triple-barrier labels and vol_regime bucket: hand-built series, one case per outcome (spec sez. 3.1.3)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from modules.dryrun.fill_model import BarFillModel
from modules.market.indicators_batch import rv_per_day_bps
from modules.risk.costs import CostParams, FeeRates, net_return
from training.labeling import (
    LOWER,
    NO_FILL,
    UNRESOLVED,
    UPPER,
    VERTICAL,
    LabelConfig,
    forward_rv_bucket,
    label_window_end,
    load_label_config,
    triple_barrier,
)

FEES = FeeRates(maker_bps=60, taker_bps=120, assumed=True)
COSTS = CostParams()  # slip 5, x 10
SPREAD = 4.0
ATR = 2.0  # price units
N = 400


@dataclass
class Bars:
    o: np.ndarray
    h: np.ndarray
    lo: np.ndarray
    c: np.ndarray
    atr: np.ndarray

    @classmethod
    def flat(cls, n: int = N, px: float = 100.0) -> Bars:
        full = np.full(n, px)
        return cls(full.copy(), full.copy(), full.copy(), full.copy(), np.full(n, ATR))

    def label(self, cfg: LabelConfig, t: int = 10, spread: float = SPREAD):
        return triple_barrier(self.o, self.h, self.lo, self.c, self.atr, np.array([t]), cfg, FEES, COSTS, spread)


def taker_cfg(**kw: object) -> LabelConfig:
    base = {"entry_mode": "taker", "horizon_min": 100, "k_tp": 4.0, "k_sl": 2.0}
    return LabelConfig(**{**base, **kw})  # type: ignore[arg-type]


def maker_cfg(**kw: object) -> LabelConfig:
    base = {"entry_mode": "maker", "horizon_min": 100, "entry_timeout_min": 30}
    return LabelConfig(**{**base, **kw})  # type: ignore[arg-type]


# taker entry on flat 100: 100 * (1 + (2 + 5)/1e4)
TAKER_ENTRY = 100.0 * (1 + (SPREAD / 2 + 5) / 1e4)


def test_taker_upper_exact_net_return() -> None:
    b = Bars.flat()
    b.h[50] = TAKER_ENTRY + 4 * ATR + 0.5  # strictly above the upper barrier
    r = b.label(taker_cfg())
    assert r.outcome[0] == UPPER and r.fill_idx[0] == 13 and r.exit_idx[0] == 50
    upper = TAKER_ENTRY + 4 * ATR
    expected = net_return(Decimal(repr(TAKER_ENTRY)), Decimal(repr(upper)), 120, 60)
    assert r.net_ret[0] == pytest.approx(float(expected), abs=1e-12)


def test_high_equal_to_upper_is_not_a_take_profit() -> None:
    b = Bars.flat()
    b.h[50] = TAKER_ENTRY + 4 * ATR  # touches but does not trade above: the resting sell does not fill
    assert b.label(taker_cfg()).outcome[0] == VERTICAL


def test_taker_lower_with_costs() -> None:
    b = Bars.flat()
    lower = TAKER_ENTRY - 2 * ATR
    b.lo[60] = lower - 0.1
    r = b.label(taker_cfg())
    assert r.outcome[0] == LOWER and r.exit_idx[0] == 60
    exit_px = lower * (1 - (SPREAD / 2 + 5 + 10) / 1e4)
    expected = net_return(Decimal(repr(TAKER_ENTRY)), Decimal(repr(exit_px)), 120, 120)
    assert r.net_ret[0] == pytest.approx(float(expected), abs=1e-12)


def test_lower_gap_exits_at_the_open_below_the_stop() -> None:
    b = Bars.flat()
    lower = TAKER_ENTRY - 2 * ATR
    gap_open = lower - 3.0
    b.o[60], b.lo[60], b.h[60], b.c[60] = gap_open, gap_open - 1, gap_open + 0.2, gap_open
    r = b.label(taker_cfg())
    assert r.outcome[0] == LOWER
    exit_px = gap_open * (1 - (SPREAD / 2 + 5 + 10) / 1e4)
    expected = net_return(Decimal(repr(TAKER_ENTRY)), Decimal(repr(exit_px)), 120, 120)
    assert r.net_ret[0] == pytest.approx(float(expected), abs=1e-12)


def test_same_bar_both_barriers_is_lower() -> None:
    b = Bars.flat()
    b.h[70] = TAKER_ENTRY + 4 * ATR + 5
    b.lo[70] = TAKER_ENTRY - 2 * ATR - 5
    assert b.label(taker_cfg()).outcome[0] == LOWER


def test_vertical_exit_uses_market_costs_at_the_horizon_close() -> None:
    b = Bars.flat()
    b.c[13 + 100] = 100.7  # fill at 13 (t + F), vertical at 113
    r = b.label(taker_cfg())
    assert r.outcome[0] == VERTICAL and r.exit_idx[0] == 113
    exit_px = 100.7 * (1 - (SPREAD / 2 + 5) / 1e4)
    expected = net_return(Decimal(repr(TAKER_ENTRY)), Decimal(repr(exit_px)), 120, 120)
    assert r.net_ret[0] == pytest.approx(float(expected), abs=1e-12)


def test_in_the_fill_bar_only_the_stop_acts() -> None:
    b = Bars.flat()
    lower = TAKER_ENTRY - 2 * ATR
    b.lo[13] = lower - 1.0  # fill bar (t + F): the stop is live because the unprotected position is watched
    r = b.label(taker_cfg())
    assert r.outcome[0] == LOWER and r.exit_idx[0] == 13 and r.fill_idx[0] == 13
    c = Bars.flat()
    c.h[13] = TAKER_ENTRY + 4 * ATR + 5  # a take-profit cannot fill before the bracket exists
    assert c.label(taker_cfg()).outcome[0] == VERTICAL
    c.h[14] = TAKER_ENTRY + 4 * ATR + 5  # from the next bar it can
    assert c.label(taker_cfg()).outcome[0] == UPPER


def test_taker_enters_at_the_open_of_bar_t_plus_f_and_f_one_reproduces_the_old_timing() -> None:
    b = Bars.flat()
    b.o[13] = 101.0
    b.h[13] = 101.0
    b.lo[13] = 101.0
    b.c[13] = 101.0
    r = b.label(taker_cfg())
    assert r.fill_idx[0] == 13 and r.entry_px[0] == pytest.approx(101.0 * (1 + (SPREAD / 2 + 5) / 1e4), abs=1e-12)
    old = b.label(taker_cfg(entry_first_bar=1))
    assert old.fill_idx[0] == 11 and old.entry_px[0] == pytest.approx(TAKER_ENTRY, abs=1e-12)


# ---- maker entry


def test_maker_entry_fills_on_first_allowed_bar_and_pays_maker_fees() -> None:
    b = Bars.flat()
    limit = 100.0 * (1 - SPREAD / 2 / 1e4)
    b.lo[13] = limit - 0.01  # bar t+3 = t + F: the first bar the order can fill
    b.h[60] = limit + 4 * ATR + 1
    r = b.label(maker_cfg())
    assert r.fill_idx[0] == 13 and r.outcome[0] == UPPER
    assert r.entry_px[0] == pytest.approx(limit, abs=1e-12)
    expected = net_return(Decimal(repr(limit)), Decimal(repr(limit + 4 * ATR)), 60, 60)
    assert r.net_ret[0] == pytest.approx(float(expected), abs=1e-12)


def test_maker_limit_is_priced_on_the_close_of_the_bar_before_the_order_exists() -> None:
    b = Bars.flat()
    b.c[12] = 110.0  # close[t + F - 1]
    b.lo[13] = 109.0
    r = b.label(maker_cfg())
    assert r.entry_px[0] == pytest.approx(110.0 * (1 - SPREAD / 2 / 1e4), abs=1e-12) and r.fill_idx[0] == 13


@pytest.mark.parametrize("bar", [11, 12])
def test_maker_order_cannot_fill_before_it_exists(bar: int) -> None:
    b = Bars.flat()
    b.lo[bar] = 90.0  # bars t+1 and t+2 elapsed before the order reached the book
    assert b.label(maker_cfg()).outcome[0] == NO_FILL


def test_maker_fill_must_trade_strictly_below_the_limit() -> None:
    b = Bars.flat()
    limit = 100.0 * (1 - SPREAD / 2 / 1e4)
    b.lo[15] = limit  # touching is not trading through
    assert b.label(maker_cfg()).outcome[0] == NO_FILL
    b.lo[15] = np.nextafter(limit, 0)
    assert b.label(maker_cfg()).outcome[0] != NO_FILL


def test_maker_window_is_exactly_t_in_bars_long() -> None:
    last = 10 + 3 + 30 - 1  # t + F + T_in - 1
    b = Bars.flat()
    b.lo[last] = 50.0
    assert b.label(maker_cfg()).fill_idx[0] == last
    c = Bars.flat()
    c.lo[last + 1] = 50.0  # one bar too late
    assert c.label(maker_cfg()).outcome[0] == NO_FILL


def test_maker_no_fill_has_no_net_return() -> None:
    r = Bars.flat().label(maker_cfg())
    assert r.outcome[0] == NO_FILL and r.fill_idx[0] == -1 and np.isnan(r.net_ret[0])


# ---- unresolved


def test_window_past_the_last_bar_is_unresolved() -> None:
    b = Bars.flat(n=60)
    assert b.label(taker_cfg()).outcome[0] == UNRESOLVED  # fill 11 + 100 > 59


def test_maker_entry_window_not_fully_observed_is_unresolved_not_no_fill() -> None:
    b = Bars.flat(n=25)
    assert b.label(maker_cfg(), t=10).outcome[0] == UNRESOLVED  # window ends at 42 > 24 and no fill yet
    edge = Bars.flat(n=43)  # last index 42 == t + F + T_in - 1: the whole window is observed
    assert edge.label(maker_cfg(), t=10).outcome[0] == NO_FILL
    assert Bars.flat(n=42).label(maker_cfg(), t=10).outcome[0] == UNRESOLVED


def test_missing_atr_is_unresolved() -> None:
    b = Bars.flat()
    b.atr[10] = np.nan
    assert b.label(taker_cfg()).outcome[0] == UNRESOLVED


def test_last_bar_sample_is_unresolved() -> None:
    b = Bars.flat()
    assert b.label(taker_cfg(), t=N - 1).outcome[0] == UNRESOLVED


def test_vectorised_samples_match_single_calls() -> None:
    rng = np.random.default_rng(0)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.002, 3000)))
    o = np.concatenate([[100.0], close[:-1]])
    h = np.maximum(o, close) * (1 + np.abs(rng.normal(0, 0.001, 3000)))
    lo = np.minimum(o, close) * (1 - np.abs(rng.normal(0, 0.001, 3000)))
    atr = np.full(3000, 0.5)
    cfg = taker_cfg(horizon_min=500)
    idx = np.arange(0, 2000, 37)
    spreads = rng.uniform(1, 6, len(idx))
    full = triple_barrier(o, h, lo, close, atr, idx, cfg, FEES, COSTS, spreads)
    for n, t in enumerate(idx):
        one = triple_barrier(o, h, lo, close, atr, np.array([t]), cfg, FEES, COSTS, float(spreads[n]))
        assert one.outcome[0] == full.outcome[n]
        np.testing.assert_array_equal(one.net_ret, full.net_ret[n : n + 1])


def test_label_config_hash_changes_with_every_parameter() -> None:
    base = LabelConfig()
    sp = {"BTC-EUR": 3.0, "ETH-EUR": 3.0}
    h0 = base.sha256(FEES, COSTS, sp)
    assert h0 == LabelConfig().sha256(FEES, COSTS, dict(reversed(sp.items())))  # key order is irrelevant
    assert LabelConfig(k_tp=3.0).sha256(FEES, COSTS, sp) != h0
    assert LabelConfig(entry_first_bar=2).sha256(FEES, COSTS, sp) != h0
    assert base.sha256(FeeRates(maker_bps=25, taker_bps=50, assumed=False), COSTS, sp) != h0
    assert base.sha256(FEES, CostParams(slippage_buffer_bps=6), sp) != h0
    assert base.sha256(FEES, COSTS, {**sp, "BTC-EUR": 3.5}) != h0  # the spread changes the labels


def test_fill_model_object_matches_kernels() -> None:
    m = BarFillModel()
    assert m.taker_entry(100.0, 4.0) == pytest.approx(TAKER_ENTRY, abs=1e-12)
    assert m.maker_limit(100.0, 4.0) == pytest.approx(99.98, abs=1e-12)
    assert m.maker_filled(99.9, 99.98) and not m.maker_filled(99.98, 99.98)
    assert m.tp_touched(101.0, 100.5) and not m.tp_touched(100.5, 100.5)
    assert m.sl_exit(98.0, 97.0, 4.0) == pytest.approx(97.0 * (1 - 17 / 1e4), abs=1e-12)
    assert m.maker_fill_qty_cap(10.0) == pytest.approx(2.5)


# ---- vol_regime


def _hourly(n_hours: int, seed: int = 1) -> pd.Series:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2023-01-01", periods=n_hours, freq="1h", tz="UTC")
    return pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.005, n_hours))), index=idx)


H_MIN = 24 * 60 * 2  # 2 days, keeps the test fast


def test_forward_rv_bucket_matches_a_manual_computation() -> None:
    close = _hourly(24 * 520)
    m = H_MIN // 60
    rv = rv_per_day_bps(close.to_numpy(), m)
    t = pd.DatetimeIndex([close.index[24 * 400 + 5] + pd.Timedelta(minutes=17 + 60)])  # inside an hour
    got = forward_rv_bucket(close, t, H_MIN)[0]
    e_t = t[0].ceil("h")
    f_t = t[0].floor("h")
    first_known = close.index[0] + pd.Timedelta(hours=1)
    k = lambda ts: int((ts - first_known) / pd.Timedelta(hours=1))  # noqa: E731
    ref = rv[k(f_t) - 365 * 24 + 1 : k(f_t) + 1]
    assert len(ref) == 365 * 24 and not np.isnan(ref).any()
    q = np.quantile(ref, [0.2, 0.4, 0.6, 0.8], method="linear")
    fwd = rv[k(e_t + pd.Timedelta(minutes=H_MIN))]
    assert got == np.searchsorted(q, fwd, side="right")


def test_forward_rv_bucket_has_no_label_before_the_reference_year() -> None:
    close = _hourly(24 * 520)
    t = pd.DatetimeIndex([close.index[24 * 100], close.index[24 * 300], close.index[24 * 400]])
    got = forward_rv_bucket(close, t, H_MIN)
    assert got[0] == -1 and got[1] == -1 and got[2] >= 0


def test_forward_rv_bucket_has_no_label_past_the_available_data() -> None:
    close = _hourly(24 * 520)
    t = pd.DatetimeIndex([close.index[-1] - pd.Timedelta(hours=10)])
    assert forward_rv_bucket(close, t, H_MIN)[0] == -1


def test_forward_rv_bucket_reference_ignores_everything_after_the_sample() -> None:
    close = _hourly(24 * 520)
    t = pd.DatetimeIndex([close.index[24 * 400]])
    a = forward_rv_bucket(close, t, H_MIN)[0]
    changed = close.copy()
    changed.iloc[24 * 400 + 2 * 24 + 5 :] *= 1.5  # after E_t + H: cannot matter
    assert forward_rv_bucket(changed, t, H_MIN)[0] == a


def test_forward_rv_bucket_rejects_gaps_and_odd_horizons() -> None:
    close = _hourly(100)
    with pytest.raises(ValueError):
        forward_rv_bucket(close.drop(close.index[50]), pd.DatetimeIndex([close.index[10]]), H_MIN)
    with pytest.raises(ValueError):
        forward_rv_bucket(close, pd.DatetimeIndex([close.index[10]]), 90)


# ---- obligations of the indicator resolution (T8, T9)


def test_label_atr_is_snapshot_atr() -> None:
    """T8: the barrier ATR of a label is the ATR the runtime state carries at the same row."""
    from modules.market.indicators import IndicatorEngine
    from modules.market.indicators_batch import compute_indicators
    from modules.market.timeframes import Bar
    from tests.indicator_fixtures import synthetic_bars

    bars = synthetic_bars(days=40, zero_volume_day=None, constant_days=None)
    batch = compute_indicators(bars)
    engine = IndicatorEngine("T8-EUR")
    live_1d, live_6h = [], []
    for ts, (o, h, lo, c, v) in zip(
        bars.index.to_pydatetime(), bars[["open", "high", "low", "close", "volume"]].to_numpy().tolist(), strict=True
    ):
        snap = engine.update(Bar(ts, o, h, lo, c, v))
        live_1d.append(snap.atr_14_1d)
        live_6h.append(snap.atr_14_6h)
    t = np.arange(20 * 1440, 28 * 1440, 97)
    cfg = taker_cfg(horizon_min=600, atr_tf="1d")
    args = [bars[k].to_numpy() for k in ("open", "high", "low", "close")]
    from_batch = triple_barrier(*args, batch["atr_14_1d"].to_numpy(), t, cfg, FEES, COSTS, 2.0)
    from_engine = triple_barrier(*args, np.array(live_1d), t, cfg, FEES, COSTS, 2.0)
    np.testing.assert_array_equal(from_batch.outcome, from_engine.outcome)
    np.testing.assert_allclose(from_batch.net_ret, from_engine.net_ret, rtol=1e-9, equal_nan=True)
    np.testing.assert_allclose(np.array(live_6h)[t], batch["atr_14_6h"].to_numpy()[t], rtol=1e-9)
    assert (np.array(live_1d)[t] > 0).all() and cfg.atr_column == "atr_14_1d"


def test_vol_regime_point_in_time() -> None:
    """T9: ``ref(t)`` uses only data up to ``F_t``; ``fwd(t)`` is ``rv_per_day_bps`` at the bar ending ``E_t + H``."""
    close = _hourly(24 * 520, seed=5)
    m = H_MIN // 60
    rv = rv_per_day_bps(close.to_numpy(), m)
    t = pd.DatetimeIndex([close.index[24 * 410 + 3] + pd.Timedelta(minutes=25)])
    full = forward_rv_bucket(close, t, H_MIN)[0]
    # keep only the bars that exist at E_t + H: nothing later may change the label
    e_end = t[0].ceil("h") + pd.Timedelta(minutes=H_MIN)
    cut = close[close.index < e_end]
    assert forward_rv_bucket(cut, t, H_MIN)[0] == full
    # and fwd is exactly the last RVd of the truncated series
    assert rv_per_day_bps(cut.to_numpy(), m)[-1] == pytest.approx(rv[len(cut) - 1], rel=1e-12)


def test_low_exactly_at_the_stop_triggers_it() -> None:
    b = Bars.flat()
    b.lo[60] = TAKER_ENTRY - 2 * ATR  # a stop triggers on "<=" (sez. 3.1.3), unlike the take-profit
    assert b.label(taker_cfg()).outcome[0] == LOWER


# ---- label_window_end (B5) and label_config.json (B8)


def test_label_window_end_for_an_hourly_sample() -> None:
    cfg = LabelConfig(atr_tf="6h", k_tp=3.0, k_sl=2.0, horizon_min=15 * 1440)
    ts = pd.DatetimeIndex(["2025-03-01 12:00"], tz="UTC")
    end = label_window_end(ts, cfg)
    assert end[0] == ts[0] + pd.Timedelta(minutes=cfg.entry_first_bar + cfg.entry_timeout_min - 1 + cfg.horizon_min)


def test_label_window_end_for_a_cusum_sample_uses_the_vol_regime_forward_data() -> None:
    cfg = LabelConfig(atr_tf="6h", k_tp=3.0, k_sl=2.0, horizon_min=15 * 1440)
    ts = pd.DatetimeIndex(["2025-03-01 12:05", "2025-03-01 12:50"], tz="UTC")
    end = label_window_end(ts, cfg)
    assert end[0] == pd.Timestamp("2025-03-01 13:00", tz="UTC") + pd.Timedelta(
        minutes=cfg.horizon_min
    )  # ceil_hour wins
    assert end[1] == ts[1] + pd.Timedelta(minutes=cfg.entry_first_bar + cfg.entry_timeout_min - 1 + cfg.horizon_min)


def test_label_window_end_covers_every_bar_a_label_reads() -> None:
    cfg = taker_cfg(horizon_min=100, entry_timeout_min=30)
    ts = pd.date_range("2025-03-01", periods=1, freq="1h", tz="UTC")
    m_cfg = maker_cfg(horizon_min=100)
    last_bar_read = 10 + m_cfg.entry_first_bar + m_cfg.entry_timeout_min - 1 + m_cfg.horizon_min  # t + F + T_in - 1 + H
    assert (label_window_end(ts, m_cfg)[0] - ts[0]) / pd.Timedelta(minutes=1) == last_bar_read - 10
    assert label_window_end(ts, cfg)[0] >= ts[0]


def write_label_config(tmp_path: Path, **override: object) -> Path:
    cfg = LabelConfig(atr_tf="6h", k_tp=3.0, k_sl=2.0, horizon_min=15 * 1440)
    spreads = {"BTC-EUR": 3.0, "ETH-EUR": 3.0}
    raw = {
        "k_tp": 3.0, "k_sl": 2.0, "horizon_min": 15 * 1440, "atr_tf": "6h", "entry_mode": "maker",
        "entry_timeout_min": 30, "entry_first_bar": 3,
        "fees_bps": {"maker": 25.0, "taker": 50.0, "source": "probe"}, "slip_bps": 5.0, "stop_extra_slip_bps": 10.0,
        "spread_bps": spreads, "selection": {"holdout_start": "2026-06-18T00:00:00+00:00"},
        "sha256": cfg.sha256(FeeRates(maker_bps=25, taker_bps=50, assumed=False), COSTS, spreads),
    }  # fmt: skip
    raw.update(override)
    path = tmp_path / "label_config.json"
    path.write_text(json.dumps(raw))
    return path


def test_load_label_config_round_trips(tmp_path: Path) -> None:
    lc = load_label_config(write_label_config(tmp_path))
    assert lc.cfg.atr_tf == "6h" and lc.cfg.entry_first_bar == 3 and lc.fees.assumed is False
    assert lc.spread_bps == {"BTC-EUR": 3.0, "ETH-EUR": 3.0}
    assert lc.holdout_start == pd.Timestamp("2026-06-18", tz="UTC") and len(lc.sha256) == 64


def test_load_label_config_refuses_a_hand_edited_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="sha256"):
        load_label_config(write_label_config(tmp_path, k_tp=4.0))
    with pytest.raises(ValueError, match="sha256"):
        load_label_config(write_label_config(tmp_path, spread_bps={"BTC-EUR": 1.0, "ETH-EUR": 3.0}))


def test_load_label_config_ignores_what_only_serves_the_selection(tmp_path: Path) -> None:
    path = write_label_config(tmp_path, selection={"holdout_start": "2026-06-18T00:00:00+00:00", "cut": "x"},
                              feasibility={"mu_year": 0.4})  # fmt: skip
    assert load_label_config(path).cfg.k_tp == 3.0  # mu / selection are not part of the hash (C7)


def test_load_label_config_rejects_malformed_files(tmp_path: Path) -> None:
    path = write_label_config(tmp_path)
    raw = json.loads(path.read_text())
    del raw["entry_first_bar"]
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="malformed"):
        load_label_config(path)


def test_upper_exit_pays_maker_fee() -> None:
    """The take-profit is a resting limit sell: maker fee on the exit leg (F0 of the P1b resolution)."""
    b = Bars.flat()
    b.h[50] = TAKER_ENTRY + 4 * ATR + 0.5
    r = b.label(taker_cfg())
    upper = TAKER_ENTRY + 4 * ATR
    expected = upper * (1 - FEES.maker_bps / 1e4) / (TAKER_ENTRY * (1 + FEES.taker_bps / 1e4)) - 1
    assert r.outcome[0] == UPPER and r.net_ret[0] == pytest.approx(expected, abs=1e-12)
    wrong = upper * (1 - FEES.taker_bps / 1e4) / (TAKER_ENTRY * (1 + FEES.taker_bps / 1e4)) - 1
    assert abs(r.net_ret[0] - wrong) > 1e-3


def test_spread_for_is_the_single_access_to_the_label_spread(tmp_path: Path) -> None:
    lc = load_label_config(write_label_config(tmp_path))
    ts = pd.Timestamp("2026-01-01 12:00", tz="UTC").to_pydatetime()
    assert lc.spread_for("BTC-EUR", ts) == 3.0
    with pytest.raises(ValueError, match="XRP-EUR"):
        lc.spread_for("XRP-EUR", ts)


# ---- synthetic LONG samples (O3.3)


def long_bars(n: int = 3000) -> Bars:
    return Bars.flat(n=n)


def test_long_sample_sells_when_the_bracket_is_about_to_lose() -> None:
    from training.labeling import synthesize_long_samples

    b = long_bars()
    cfg = taker_cfg(horizon_min=500)
    # the fictitious entry fills at t_e + 3; the stop hits long after t: V_br < V_now only if the price already fell
    t = 1000
    b.c[t:] = 98.0
    b.o[t:] = 98.0
    b.h[t:] = 98.0
    b.lo[t:] = 98.0
    b.lo[t + 120] = 90.0  # the bracket will hit the stop well after the decision
    rng = np.random.default_rng(1)
    ls = synthesize_long_samples(b.o, b.h, b.lo, b.c, b.atr, np.array([t]), cfg, FEES, COSTS, np.array([SPREAD]),
                                 lambda idx: np.full(len(idx), SPREAD), rng)  # fmt: skip
    if len(ls.keep) == 1:  # the draw may discard the sample; with a fixed seed it must be deterministic
        assert ls.fill_idx[0] < t <= ls.exit_idx_br[0] - cfg.entry_first_bar
        assert ls.sell[0] == (
            BarFillModel().market_exit(98.0, SPREAD) * (1 - FEES.taker_bps / 1e4)
            > ls.exit_px_br[0] * (1 - (FEES.maker_bps if ls.outcome_br[0] == UPPER else FEES.taker_bps) / 1e4)
        )


def test_long_samples_are_deterministic_and_respect_the_discard_rules() -> None:
    from training.labeling import synthesize_long_samples

    rng_bars = np.random.default_rng(5)
    close = 100 * np.exp(np.cumsum(rng_bars.normal(0, 0.001, 6000)))
    o = np.concatenate([[100.0], close[:-1]])
    h, lo = np.maximum(o, close) * 1.0005, np.minimum(o, close) * 0.9995
    atr = np.full(6000, 1.5)  # wide barriers: brackets live long enough to straddle the sample
    cfg = taker_cfg(horizon_min=800, k_tp=3.0, k_sl=1.5)
    t = np.arange(1500, 4500, 53)
    sp = np.full(len(t), SPREAD)
    a = synthesize_long_samples(
        o, h, lo, close, atr, t, cfg, FEES, COSTS, sp, lambda idx: np.full(len(idx), SPREAD), np.random.default_rng(7)
    )
    b = synthesize_long_samples(
        o, h, lo, close, atr, t, cfg, FEES, COSTS, sp, lambda idx: np.full(len(idx), SPREAD), np.random.default_rng(7)
    )
    np.testing.assert_array_equal(a.keep, b.keep)
    np.testing.assert_array_equal(a.sell, b.sell)
    assert len(a.keep) > 10
    kt = t[a.keep]
    assert (a.fill_idx < kt).all() and (a.exit_idx_br >= kt + cfg.entry_first_bar).all()
    assert (kt - a.t_e >= 60).all() and (kt - a.t_e <= cfg.horizon_min).all()
    assert np.isin(a.outcome_br, (UPPER, LOWER, VERTICAL)).all()
    assert a.sell.dtype == bool


def test_triple_barrier_reports_the_exit_price() -> None:
    b = Bars.flat()
    b.h[50] = TAKER_ENTRY + 4 * ATR + 0.5
    r = b.label(taker_cfg())
    assert r.exit_px[0] == pytest.approx(TAKER_ENTRY + 4 * ATR, abs=1e-12)
    lo = Bars.flat()
    lower = TAKER_ENTRY - 2 * ATR
    lo.lo[60] = lower - 0.1
    assert lo.label(taker_cfg()).exit_px[0] == pytest.approx(lower * (1 - (SPREAD / 2 + 5 + 10) / 1e4), abs=1e-12)
    assert np.isnan(Bars.flat().label(maker_cfg()).exit_px[0])  # NO_FILL
