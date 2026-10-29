"""Vectorized market features for training (spec sez. 2.2.1.b, docs/decisions/2026-10-17-indicatori.md).

``compute_indicators`` is the training-side twin of ``indicators.IndicatorEngine``. Both must give the same value
on every row (tolerance ``|a-b| <= 1e-9 * max(1, |b|)``, identical NaN masks). To make that true by construction:

- recurrences (Wilder, EMA) are plain left-to-right float loops with SMA seeds, never ``pandas.ewm``;
- the scalar formulas shared by both sides (``rsi_value``, ``rvd_value``, ``bb_stats``, ``zscore_value``...) live here;
- higher-timeframe series use only CLOSED bars (``aggregate_bars``), made visible on the 1m row of their last
  minute (``htf_asof``): nothing provisional, no lookahead (vincolo C7);
- the anchor is the first row of the 1m frame. Always pass the product's whole history (``load_bars`` without
  ``start``), never a slice: Wilder/EMA depend on where they start.

Output is in bps (not fractions) and unrounded: rounding and clamping happen only in the StateBuilder.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
import pandas as pd

from modules.market.indicator_defs import (
    ATR_N,
    BB_K,
    BB_N,
    FEATURE_COLUMNS,
    HHLL_WIN_MIN,
    HTF_TIMEFRAMES,
    MACD_FAST,
    MACD_SIGNAL,
    MACD_SLOW,
    PCTILE_DAYS,
    PCTILE_MIN_N,
    RET_WINDOWS_MIN,
    RSI_N,
    RV_1D_BARS,
    RV_30D_BARS,
    SEQ_1D_LEN,
    SEQ_1H_LEN,
    VOLZ_DAYS,
    VWAP_WIN_MIN,
)
from modules.market.timeframes import TIMEFRAME_SECONDS, BarGapError, aggregate_bars

__all__ = [
    "BarGapError",
    "bb_pos",
    "bb_stats",
    "compute_indicators",
    "ema_sma_seed",
    "htf_asof",
    "macd_hist",
    "pctile_prev",
    "rsi_value",
    "rv_per_day_bps",
    "rvd_value",
    "true_range",
    "wilder",
    "wilder_atr",
    "wilder_rsi",
    "zscore_prev",
    "zscore_value",
]

NAN = float("nan")


# ------------------------------------------------------------------------------ scalar formulas


def rsi_value(avg_gain: float, avg_loss: float) -> float:
    """RSI from the two Wilder averages. ``avgP == 0`` is the standard convention, not a division by zero."""
    if math.isnan(avg_gain) or math.isnan(avg_loss):
        return NAN
    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0.0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def rvd_value(r2_window: Sequence[float], m: int) -> float:
    """``RVd(m, k)``: daily volatility in bps from the last ``m`` squared hourly log-returns."""
    return 1e4 * math.sqrt((24.0 / m) * math.fsum(r2_window))


def bb_stats(closes: Sequence[float]) -> tuple[float, float]:
    """Mean and population standard deviation (ddof=0) of the window, two passes."""
    n = len(closes)
    m = math.fsum(closes) / n
    sd = math.sqrt(math.fsum([(x - m) * (x - m) for x in closes]) / n)
    return m, sd


def bb_pos(c: float, m: float, sd: float) -> float:
    """Position of the 1m close in the daily Bollinger bands; NaN if the bands have zero width. Not clamped."""
    if math.isnan(m) or math.isnan(sd) or sd == 0.0:
        return NAN
    return (c - (m - BB_K * sd)) / (2.0 * BB_K * sd)


def zscore_value(prev: Sequence[float], y: float) -> float:
    """z-score of ``y`` against ``prev`` (ddof=1, two passes); NaN if the deviation is zero."""
    n = len(prev)
    m = math.fsum(prev) / n
    s = math.sqrt(math.fsum([(x - m) * (x - m) for x in prev]) / (n - 1))
    if s == 0.0:
        return NAN
    return (y - m) / s


# ------------------------------------------------------------------------------------ recurrences


def wilder(u: np.ndarray, n: int) -> np.ndarray:
    """Wilder smoothing: first value (index ``n-1``) is the mean of the first ``n`` inputs, then
    ``x_k = (x_{k-1} * (n-1) + u_k) / n``. Inputs must not contain NaN."""
    out = np.full(len(u), np.nan)
    if len(u) < n:
        return out
    acc = 0.0
    for i in range(n):
        acc += float(u[i])
    x = acc / n
    out[n - 1] = x
    for k in range(n, len(u)):
        x = (x * (n - 1) + float(u[k])) / n
        out[k] = x
    return out


def ema_sma_seed(x: np.ndarray, n: int) -> np.ndarray:
    """EMA(n) seeded with the SMA of the first ``n`` non-NaN inputs (leading NaN allowed), alpha = 2/(n+1)."""
    out = np.full(len(x), np.nan)
    valid = np.flatnonzero(~np.isnan(x))
    if len(valid) < n:
        return out
    s0 = int(valid[0])
    acc = 0.0
    for i in range(s0, s0 + n):
        acc += float(x[i])
    e = acc / n
    out[s0 + n - 1] = e
    alpha = 2.0 / (n + 1)
    for k in range(s0 + n, len(x)):
        e = alpha * float(x[k]) + (1.0 - alpha) * e
        out[k] = e
    return out


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    """``TR_0 = H-L``; ``TR_k = max(H-L, |H-C_{k-1}|, |L-C_{k-1}|)``."""
    n = len(high)
    tr = np.full(n, np.nan)
    if n == 0:
        return tr
    tr[0] = float(high[0]) - float(low[0])
    for k in range(1, n):
        h, lo, pc = float(high[k]), float(low[k]), float(close[k - 1])
        tr[k] = max(h - lo, abs(h - pc), abs(lo - pc))
    return tr


def wilder_atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int = ATR_N) -> np.ndarray:
    return wilder(true_range(high, low, close), n)


def wilder_rsi(close: np.ndarray, n: int = RSI_N) -> np.ndarray:
    out = np.full(len(close), np.nan)
    if len(close) < 2:
        return out
    gains, losses = [], []
    for k in range(1, len(close)):
        d = float(close[k]) - float(close[k - 1])
        gains.append(d if d > 0 else 0.0)
        losses.append(-d if d < 0 else 0.0)
    avg_g, avg_p = wilder(np.asarray(gains), n), wilder(np.asarray(losses), n)
    for k in range(1, len(close)):
        out[k] = rsi_value(float(avg_g[k - 1]), float(avg_p[k - 1]))
    return out


def macd_hist(close: np.ndarray, fast: int = MACD_FAST, slow: int = MACD_SLOW, signal: int = MACD_SIGNAL) -> np.ndarray:
    macd = ema_sma_seed(close, fast) - ema_sma_seed(close, slow)
    hist: np.ndarray = macd - ema_sma_seed(macd, signal)
    return hist


def rv_per_day_bps(close_1h: np.ndarray, m: int) -> np.ndarray:
    """``RVd(m, k)`` for every hourly bar ``k`` (NaN for ``k < m``). The single realized-volatility function of
    the system: used by the state (m=24, 720) and by the ``vol_regime`` label (m=H/60)."""
    n = len(close_1h)
    out = np.full(n, np.nan)
    r2 = [NAN] * n
    for k in range(1, n):
        r = math.log(float(close_1h[k]) / float(close_1h[k - 1]))
        r2[k] = r * r
    for k in range(m, n):
        out[k] = rvd_value(r2[k - m + 1 : k + 1], m)
    return out


def pctile_prev(x: np.ndarray, window: int = PCTILE_DAYS, min_n: int = PCTILE_MIN_N) -> np.ndarray:
    """Percentile (0..100) of ``x[k]`` among the previous ``window`` available values, current excluded,
    ties counted with ``<=``; NaN with fewer than ``min_n`` previous values."""
    out = np.full(len(x), np.nan)
    for k in range(len(x)):
        if math.isnan(x[k]):
            continue
        prev = x[max(0, k - window) : k]
        prev = prev[~np.isnan(prev)]
        n = len(prev)
        if n < min_n:
            continue
        out[k] = 100.0 * int(np.count_nonzero(prev <= x[k])) / n
    return out


def zscore_prev(y: np.ndarray, window: int = VOLZ_DAYS) -> np.ndarray:
    out = np.full(len(y), np.nan)
    for k in range(window, len(y)):
        out[k] = zscore_value([float(v) for v in y[k - window : k]], float(y[k]))
    return out


# ------------------------------------------------------------------------------------ alignment


def htf_asof(htf: pd.DataFrame, tf: str, index_1m: pd.DatetimeIndex) -> pd.DataFrame:
    """Point-in-time join: the value of the HTF bar starting at ``B`` appears on the 1m row ``B + tf - 1min`` (the
    row of its last minute, whose decision time is ``B + tf``) and is carried forward. Earlier rows are NaN."""
    n = len(index_1m)
    out = pd.DataFrame(np.nan, index=index_1m, columns=htf.columns)
    if htf.empty or n == 0:
        return out
    visible_at = htf.index + pd.Timedelta(seconds=TIMEFRAME_SECONDS[tf] - 60)
    rows = index_1m.get_indexer(visible_at)
    if (rows < 0).any():
        raise BarGapError("a completed higher-timeframe bar has no matching 1m row")
    src = np.full(n, -1, dtype=np.int64)
    src[rows] = np.arange(len(htf))
    src = np.maximum.accumulate(src)
    have = src >= 0
    for col in htf.columns:
        values = htf[col].to_numpy(dtype=np.float64)
        arr = np.full(n, np.nan)
        arr[have] = values[src[have]]
        out[col] = arr
    return out


# --------------------------------------------------------------------------------- main entry point


def _all_or_nothing(cols: dict[str, np.ndarray], names: list[str]) -> None:
    """A sequence is whole or ``null``, never partial (spec 2.2.1.b): NaN everywhere unless every element exists."""
    complete = np.logical_and.reduce([~np.isnan(cols[name]) for name in names])
    for name in names:
        cols[name] = np.where(complete, cols[name], np.nan)


def _htf_frame(htf: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Per-bar features of one timeframe, indexed by bar start."""
    h, lo, c = (htf[k].to_numpy(dtype=np.float64) for k in ("high", "low", "close"))
    atr = wilder_atr(h, lo, c)
    cols: dict[str, np.ndarray] = {
        f"atr_14_{tf}": atr,
        f"atr_14_{tf}_bps": 1e4 * atr / c if len(c) else atr,
        f"rsi_14_{tf}": wilder_rsi(c),
    }
    ret1 = np.full(len(c), np.nan)
    if len(c) > 1:
        ret1[1:] = 1e4 * (c[1:] / c[:-1] - 1.0)
    if tf == "1h":
        rv1d = rv_per_day_bps(c, RV_1D_BARS)
        rv30d = rv_per_day_bps(c, RV_30D_BARS)
        with np.errstate(divide="ignore", invalid="ignore"):
            ratio = np.where(rv30d != 0.0, rv1d / rv30d, np.nan)
        cols |= {"rv_1d_bps": rv1d, "rv_30d_bps": rv30d, "rv_ratio_1d_30d": ratio}
        for j in range(SEQ_1H_LEN):
            shift = SEQ_1H_LEN - 1 - j
            col = np.full(len(c), np.nan)
            if shift < len(c):
                col[shift:] = ret1[: len(c) - shift]
            cols[f"seq_1h_bps_{j:02d}"] = col
        _all_or_nothing(cols, [f"seq_1h_bps_{j:02d}" for j in range(SEQ_1H_LEN)])
    if tf == "1d":
        hist = macd_hist(c)
        cols["macd_hist_1d_bps"] = 1e4 * hist / c if len(c) else hist
        cols["atr_1d_pctile_365d"] = pctile_prev(cols["atr_14_1d_bps"])
        bb_m, bb_sd = np.full(len(c), np.nan), np.full(len(c), np.nan)
        for k in range(BB_N - 1, len(c)):
            bb_m[k], bb_sd[k] = bb_stats([float(x) for x in c[k - BB_N + 1 : k + 1]])
        cols["_bb_m"], cols["_bb_sd"] = bb_m, bb_sd
        for j in range(SEQ_1D_LEN):
            shift = SEQ_1D_LEN - 1 - j
            col = np.full(len(c), np.nan)
            if shift < len(c):
                col[shift:] = ret1[: len(c) - shift]
            cols[f"seq_1d_bps_{j}"] = col
        _all_or_nothing(cols, [f"seq_1d_bps_{j}" for j in range(SEQ_1D_LEN)])
    return pd.DataFrame(cols, index=htf.index)


def compute_indicators(bars_1m: pd.DataFrame) -> pd.DataFrame:
    """All ``FEATURE_COLUMNS`` for every row of a contiguous 1m frame (``load_bars`` output).

    Raises ``BarGapError`` if the frame is not contiguous. NaN = not available.
    """
    n = len(bars_1m)
    idx = pd.DatetimeIndex(bars_1m.index)
    if n == 0:
        return pd.DataFrame({c: np.array([], dtype=np.float64) for c in FEATURE_COLUMNS}, index=idx)
    htf = {tf: aggregate_bars(bars_1m, tf) for tf in HTF_TIMEFRAMES}  # also checks contiguity
    o = bars_1m
    c, h, lo, v = (o[k].to_numpy(dtype=np.float64) for k in ("close", "high", "low", "volume"))

    out = pd.DataFrame(np.nan, index=idx, columns=list(FEATURE_COLUMNS))

    # --- returns over fixed 1m windows
    for name, w in RET_WINDOWS_MIN.items():
        prev = np.full(n, np.nan)
        if w < n:
            prev[w:] = c[: n - w]
        out[f"ret_{name}_bps"] = 1e4 * (c / prev - 1.0)

    # --- rolling windows on 1m rows
    pv = (h + lo + c) / 3.0 * v
    sum_pv = pd.Series(pv).rolling(VWAP_WIN_MIN, min_periods=VWAP_WIN_MIN).sum().to_numpy()
    sum_v = pd.Series(v).rolling(VWAP_WIN_MIN, min_periods=VWAP_WIN_MIN).sum().to_numpy()
    traded = (v > 0.0).astype(np.float64)  # integer count of minutes with volume, never a float sum compared to 0
    n_traded = pd.Series(traded).rolling(VWAP_WIN_MIN, min_periods=VWAP_WIN_MIN).sum().to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        vwap = np.where(n_traded > 0.0, sum_pv / sum_v, np.nan)
        out["dist_vwap_1d_bps"] = 1e4 * (c / vwap - 1.0)
    hh = pd.Series(h).rolling(HHLL_WIN_MIN, min_periods=HHLL_WIN_MIN).max().to_numpy()
    ll = pd.Series(lo).rolling(HHLL_WIN_MIN, min_periods=HHLL_WIN_MIN).min().to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        out["hh_ll_pos_30d"] = np.where(hh != ll, (c - ll) / (hh - ll), np.nan)

    # --- higher timeframes, closed bars only
    bb_m = bb_sd = np.full(n, np.nan)
    for tf in HTF_TIMEFRAMES:
        frame = _htf_frame(htf[tf], tf)
        if tf == "1d" and not frame.empty:
            day_rows = idx.get_indexer(htf[tf].index)
            qv = [math.fsum(pv[p : p + 1440]) for p in day_rows]  # quote volume of each complete UTC day
            frame["vol_z_1d"] = zscore_prev(np.array([math.log(1.0 + q) for q in qv]))
        aligned = htf_asof(frame, tf, idx)
        for col in aligned.columns:
            if col in out.columns:
                out[col] = aligned[col].to_numpy()
        if tf == "1d":
            bb_m, bb_sd = aligned["_bb_m"].to_numpy(), aligned["_bb_sd"].to_numpy()

    # Bollinger position: bands from the last closed daily bars, numerator from the 1m close
    with np.errstate(divide="ignore", invalid="ignore"):
        pos = (c - (bb_m - BB_K * bb_sd)) / (2.0 * BB_K * bb_sd)
    out["bb_pos_1d"] = np.where((bb_sd > 0.0) & ~np.isnan(bb_m), pos, np.nan)
    return out[list(FEATURE_COLUMNS)].astype(np.float64)
