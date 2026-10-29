"""Deterministic synthetic 1m history for the indicator parity tests (no network, no files)."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd

from modules.market.indicator_defs import FEATURE_COLUMNS
from modules.market.indicators import IndicatorEngine
from modules.market.timeframes import Bar

START = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
DAY = 1440


def synthetic_bars(
    days: int = 130,
    seed: int = 11,
    *,
    zero_volume_day: int | None = 40,
    constant_days: tuple[int, int] | None = (90, 92),
    start: datetime = START,
) -> pd.DataFrame:
    """Random walk with volatility clusters, quiet stretches (flat bars, volume 0), an optional whole day with no
    volume and an optional stretch of constant price with volume (zero true range). Contiguous, like ``load_bars``."""
    n = days * DAY
    rng = np.random.default_rng(seed)
    vol_regime = np.repeat(np.exp(rng.normal(0.0, 0.5, days + 1)), DAY)[:n]
    r = rng.normal(0.0, 4e-4, n) * vol_regime
    spread = np.abs(rng.normal(0.0, 3e-4, n)) * vol_regime
    volume = rng.exponential(5.0, n) * (1.0 + vol_regime)

    quiet = np.zeros(n, dtype=bool)
    for s in rng.integers(0, n - 400, size=days):  # about one quiet stretch per day
        quiet[s : s + int(rng.integers(20, 300))] = True
    if zero_volume_day is not None:
        quiet[zero_volume_day * DAY : (zero_volume_day + 1) * DAY] = True
    r[quiet], spread[quiet], volume[quiet] = 0.0, 0.0, 0.0

    if constant_days is not None:
        lo, hi = constant_days[0] * DAY, constant_days[1] * DAY
        r[lo:hi], spread[lo:hi] = 0.0, 0.0  # constant price, volume kept

    close = 100.0 * np.exp(np.cumsum(r))
    open_ = np.concatenate([[100.0], close[:-1]])
    high = np.maximum(open_, close) + spread * close
    low = np.minimum(open_, close) - spread * close
    index = pd.date_range(start, periods=n, freq="1min", name="ts")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=index)


def constant_bars(days: int, volume: float = 1.0, price: float = 50.0) -> pd.DataFrame:
    n = days * DAY
    index = pd.date_range(START, periods=n, freq="1min", name="ts")
    v = np.full(n, volume)
    p = np.full(n, price)
    return pd.DataFrame({"open": p, "high": p, "low": p, "close": p, "volume": v}, index=index)


def stream_rows(engine: IndicatorEngine, bars: pd.DataFrame) -> pd.DataFrame:
    """Feed ``bars`` one by one through ``update`` and collect the feature rows."""
    rows = []
    cols = bars[["open", "high", "low", "close", "volume"]].to_numpy().tolist()
    for ts, (o, h, lo, c, v) in zip(bars.index.to_pydatetime(), cols, strict=True):
        rows.append(engine.update(Bar(ts, o, h, lo, c, v)).as_row())
    return pd.DataFrame(rows, index=bars.index)[list(FEATURE_COLUMNS)]


def assert_parity(inc: pd.DataFrame, ref: pd.DataFrame, rtol: float = 1e-9) -> None:
    """``|a-b| <= rtol * max(1, |b|)`` on every cell and identical NaN masks (spec Q6)."""
    assert list(inc.columns) == list(ref.columns)
    assert len(inc) == len(ref)
    a, b = inc.to_numpy(dtype=np.float64), ref.to_numpy(dtype=np.float64)
    nan_a, nan_b = np.isnan(a), np.isnan(b)
    if (nan_a != nan_b).any():
        rows, cols = np.nonzero(nan_a != nan_b)
        raise AssertionError(
            f"NaN masks differ in {len(rows)} cells, first: row {rows[0]} column {ref.columns[cols[0]]} "
            f"(incremental={a[rows[0], cols[0]]}, batch={b[rows[0], cols[0]]})"
        )
    ok = ~nan_a
    err = np.abs(a - b)[ok] - rtol * np.maximum(1.0, np.abs(b))[ok]
    if (err > 0).any():
        flat = np.flatnonzero(ok)[int(np.argmax(err))]
        r_, c_ = divmod(flat, a.shape[1])
        raise AssertionError(f"parity broken at row {r_} column {ref.columns[c_]}: {a[r_, c_]!r} vs {b[r_, c_]!r}")
