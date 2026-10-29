"""Synthetic tests of the C/D signal rules and of the counting script (no real data, no holdout)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from modules.lab.signals_cd import (
    resample_bars,
    revert_events,
    revert_events_raw,
    squeeze_q,
    squeeze_signals,
    squeeze_threshold,
    thin_events,
)
from script import count_signals_cd as cs

T0 = pd.Timestamp("2020-01-01", tz="UTC")


def hourly(n: int, jumps: dict[int, float]) -> pd.DataFrame:
    """Deterministic 1h bars: log returns alternate +-0.001 (a 4h change is 0), plus the given log jumps."""
    r = np.where(np.arange(n) % 2 == 0, 0.001, -0.001).astype(float)
    for i, j in jumps.items():
        r[i] += j
    close = 100.0 * np.exp(np.cumsum(r))
    idx = pd.date_range(T0, periods=n, freq="1h")
    return pd.DataFrame({"open": close, "high": close, "low": close, "close": close, "volume": 1.0}, index=idx)


# ------------------------------------------------------------------------------------------ C: dedup 24 h


def test_thin_events_boundary_is_24h_inclusive() -> None:
    t = pd.DatetimeIndex([T0, T0 + pd.Timedelta(hours=23), T0 + pd.Timedelta(hours=24)])
    kept = thin_events(t, pd.Timedelta(hours=24))
    assert list(kept) == [T0, T0 + pd.Timedelta(hours=24)]


def test_revert_dedup_window_opens_on_detected_events_only() -> None:
    bars = hourly(900, {800: -0.04, 820: -0.04, 830: -0.04})
    raw = revert_events_raw(bars)
    assert {800, 801, 802, 803}.issubset({bars.index.get_loc(t) for t in raw.index})  # the drop persists 4 bars
    kept = [bars.index.get_loc(t) for t in revert_events(bars).index]
    # 820 is inside the window of 800 (suppressed, so it does NOT open a window): 830 is 30 h after 800 -> detected
    assert kept == [800, 830]


def test_revert_needs_complete_window() -> None:
    bars = hourly(900, {800: -0.04})
    bars.loc[bars.index[100], "close"] = np.nan  # hole inside the 720 h window of bar 800
    assert revert_events(bars).empty
    assert [bars.index.get_loc(t) for t in revert_events(hourly(900, {800: -0.04})).index] == [800]


def test_revert_no_look_ahead() -> None:
    bars = hourly(900, {800: -0.04, 830: -0.04})
    cut = 810
    future_changed = bars.copy()
    future_changed.iloc[cut + 1 :, :4] = 1.0  # garbage after the cut
    full = revert_events_raw(future_changed).loc[: bars.index[cut]]
    prefix = revert_events_raw(bars.iloc[: cut + 1])
    pd.testing.assert_frame_equal(full, prefix, check_freq=False)
    assert not prefix.empty and prefix["sigma1h"].notna().all()  # sigma/z of the event at 800 equal too


# ------------------------------------------------------------------------------------------ D: Q, q20, breakout


def four_hour(n_wide: int = 220, n_calm: int = 40) -> pd.DataFrame:
    r = np.concatenate([np.where(np.arange(n_wide) % 2 == 0, 0.02, -0.02), np.full(n_calm, 0.0)])
    r[n_wide:] = np.where(np.arange(n_calm) % 2 == 0, 0.001, -0.001)
    close = 100.0 * np.exp(np.cumsum(r))
    idx = pd.date_range(T0, periods=len(close), freq="4h")
    return pd.DataFrame({"open": close, "high": close * 1.001, "low": close * 0.999, "close": close}, index=idx)


def with_breakout(bars: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    out = bars.copy()
    t = len(out)
    c = float(out["close"].iloc[-1]) * 1.05
    out.loc[out.index[-1] + pd.Timedelta(hours=4)] = [c, c * 1.001, c * 0.999, c]
    return out, t


def test_squeeze_signal_uses_previous_q_and_channel() -> None:
    bars, t = with_breakout(four_hour())
    q = squeeze_q(bars)
    assert q.iloc[t - 1] < 0.2 < q.iloc[t]  # Q(t) already contains the jump: only Q(t-1) can fire
    assert bool(squeeze_signals(bars, 0.2).iloc[t])
    assert not bool(squeeze_signals(bars, 0.01).iloc[t])  # not compressed enough
    assert not squeeze_signals(bars, 0.2).iloc[:t].any()  # no breakout before


def test_squeeze_hole_in_window_gives_no_signal() -> None:
    bars, t = with_breakout(four_hour())
    bars.loc[bars.index[t - 100], "close"] = np.nan  # hole in the 180 return window of Q(t-1)
    assert not bool(squeeze_signals(bars, 0.2).iloc[t])


def test_squeeze_no_look_ahead_in_q_and_signal() -> None:
    bars, t = with_breakout(four_hour())
    changed = bars.copy()
    changed.iloc[t:, :] = 1.0e6  # everything from the breakout bar on is garbage
    before = bars.iloc[:t]
    pd.testing.assert_series_equal(squeeze_q(before), squeeze_q(changed).iloc[:t])
    pd.testing.assert_series_equal(squeeze_signals(before, 0.2), squeeze_signals(changed, 0.2).iloc[:t])


def test_q20_is_training_only_and_linear_quantile() -> None:
    bars, _ = with_breakout(four_hour())
    train = bars.iloc[:200]
    expected = float(np.quantile(squeeze_q(train).dropna().to_numpy(), 0.2, method="linear"))
    assert squeeze_threshold(train) == expected
    # bars after the training end can only change the threshold if they are passed in: the function sees what it gets
    longer = squeeze_threshold(bars)
    assert squeeze_threshold(bars.iloc[:200]) == expected and longer != expected


# ------------------------------------------------------------------------------------------ resample


def test_resample_marks_hole_as_nan_row() -> None:
    idx = pd.date_range(T0, periods=12, freq="15min")  # 3 hours
    base = pd.DataFrame({"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1.0}, index=idx)
    base = base.drop(index=idx[4:8])  # the whole second hour is missing
    out = resample_bars(base.rename_axis("ts").reset_index(), "1h")
    assert len(out) == 3
    assert out.iloc[1].isna().all()
    assert out.iloc[0]["volume"] == 4.0 and out.iloc[0]["high"] == 2.0 and out.iloc[0]["low"] == 0.5


# ------------------------------------------------------------------------------------------ script: cut, units


def usd_file(path: Path, days: int = 200, start: str = "2020-08-01") -> pd.DataFrame:
    idx = pd.date_range(start, periods=days * 96, freq="15min", tz="UTC")
    rng = np.random.default_rng(7)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.004, len(idx))))
    frame = pd.DataFrame(
        {"ts": idx, "open": close, "high": close * 1.002, "low": close * 0.998, "close": close, "volume": 1.0}
    )
    frame.to_parquet(path, index=False)
    return frame


def test_reader_never_returns_rows_at_or_after_the_cut(tmp_path: Path) -> None:
    frame = usd_file(tmp_path / "X-USD_15m.parquet")
    assert frame["ts"].max() > cs.USD_CUT  # the file does contain "holdout" rows
    out = cs.read_usd_training(tmp_path / "X-USD_15m.parquet")
    assert out["ts"].max() == cs.USD_CUT - pd.Timedelta(minutes=15)
    assert (out["ts"] < cs.USD_CUT).all()


def test_counts_do_not_depend_on_rows_after_the_cut(tmp_path: Path) -> None:
    usd_file(tmp_path / "A-USD_15m.parquet")
    clean = tmp_path / "B-USD_15m.parquet"
    frame = pd.read_parquet(tmp_path / "A-USD_15m.parquet")
    late = frame["ts"] >= cs.USD_CUT
    frame.loc[late, ["open", "high", "low", "close"]] *= 1000.0  # absurd holdout values
    frame.to_parquet(clean, index=False)
    a = cs.build_counts({"BTC-USD": cs.coin_bars("U", cs.read_usd_training(tmp_path / "A-USD_15m.parquet"))})
    b = cs.build_counts({"BTC-USD": cs.coin_bars("U", cs.read_usd_training(clean))})
    for r in (a, b):
        r.pop("created_utc")
    assert a == b
    assert a["D"]["q20"] == b["D"]["q20"]


def test_units_group_coins_within_24h_and_classify() -> None:
    btc = pd.DatetimeIndex([T0, T0 + pd.Timedelta(hours=30)])
    eth = pd.DatetimeIndex([T0 + pd.Timedelta(hours=10)])
    merged = cs._union([btc, eth])
    assert len(cs.units(merged)) == 2  # BTC t0 absorbs ETH t0+10h; BTC t0+30h opens a second unit
    assert cs.by_year(merged) == {"2020": 3}
    assert cs.classify(150, 150, 150) == "sopra"
    assert cs.classify(73, 125, 150) == "sotto"
    assert cs.classify(73, 200, 150) == "ambiguo"
