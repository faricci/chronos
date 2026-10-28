"""Pure signal rules of hypotheses C (excess-drop reversion) and D (volatility squeeze + breakout).

Specification: docs/decisions/2026-10-28-ipotesi-strategie-laboratorio.md sections 3.C, 3.D and 10 (E5, E6), and
docs/decisions/2026-10-28-book-laboratorio-spec.md section 13 (Q3, Q4). Only the SIGNAL is defined here: no fill, no
exit, no cost, no return. The backtest and the dry-run drivers reuse these functions (no second copy).

Conventions: a frame of bars is indexed by the UTC OPEN time of each bar on a REGULAR grid; a missing bar is a row
of NaN (``resample_bars`` builds it). The value at index ``t`` is the decision taken at the close of bar ``t`` and
uses only rows <= ``t``: every window is a trailing ``rolling`` window with ``min_periods`` equal to the window, so a
hole in the window gives NaN and therefore no signal (E5, E6). No function looks at a row after ``t``.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

_AGG = {"open": "first", "high": "max", "low": "min", "close": "last"}


@dataclass(frozen=True)
class RevertParams:
    """Hypothesis C: drop of ``lookback_bars`` hours beyond ``k_sigma4h`` x sigma4h, sigma4h = 2 x sigma1h."""

    sigma_window: int = 720
    lookback_bars: int = 4
    k_sigma4h: float = 3.0
    dedup_hours: int = 24


@dataclass(frozen=True)
class SqueezeParams:
    """Hypothesis D on 4h bars: Q = sd(short returns) / sd(long returns) <= q20 and breakout of the channel."""

    short_window: int = 30
    long_window: int = 180
    channel_in: int = 30
    quantile: float = 0.2
    max_hold_bars: int = 60


REVERT = RevertParams()
SQUEEZE = SqueezeParams()


def resample_bars(frame: pd.DataFrame, rule: str) -> pd.DataFrame:
    """OHLC bars of length ``rule`` from finer bars (``ts`` column or UTC index), regular grid.

    A target bar exists when at least one finer bar falls in it; otherwise the row is all NaN (a hole).
    The caller cuts the data on a bar boundary: the last target bar must be complete.
    """
    data = frame.set_index("ts") if "ts" in frame.columns else frame
    grouped = data.resample(rule, label="left", closed="left")
    out = grouped.agg(_AGG)
    out["volume"] = grouped["volume"].sum(min_count=1)
    out.index.name = "ts"
    return out


def log_returns(close: pd.Series) -> pd.Series:
    """ln(C_t / C_(t-1)); NaN when either bar is missing."""
    return pd.Series(np.log(close / close.shift(1)), index=close.index)


def revert_events_raw(bars_1h: pd.DataFrame, p: RevertParams = REVERT) -> pd.DataFrame:
    """Hours t where ln(C_t / C_(t-4)) <= -k x sigma4h(t), BEFORE the 24 h deduplication.

    sigma1h(t) = sd (ddof 1) of the 720 hourly log returns r_(t-719) .. r_t (all must exist); sigma4h = 2 x sigma1h.
    Columns: ``z`` = ln(C_t / C_(t-4)) / sigma4h(t), ``sigma1h``, ``sigma4h``.
    """
    close = bars_1h["close"]
    sigma1h = log_returns(close).rolling(p.sigma_window).std(ddof=1)
    sigma4h = 2.0 * sigma1h
    drop = np.log(close / close.shift(p.lookback_bars))
    hit = (sigma4h > 0) & (drop <= -p.k_sigma4h * sigma4h)
    frame = pd.DataFrame({"z": drop / sigma4h, "sigma1h": sigma1h, "sigma4h": sigma4h})
    return frame[hit.to_numpy()]


def thin_events(times: pd.DatetimeIndex, min_gap: pd.Timedelta) -> pd.DatetimeIndex:
    """Greedy thinning in time order: keep an event only if >= ``min_gap`` after the last KEPT one.

    Used for the 24 h deduplication of C (E6: every detected event opens the window, traded or not, so the
    definition is a pure function of the prices) and for the independence units (anchored clusters).
    """
    keep: list[int] = []
    last: pd.Timestamp | None = None
    for i, t in enumerate(times):
        if last is None or t - last >= min_gap:
            keep.append(i)
            last = t
    return times[keep]


def revert_events(bars_1h: pd.DataFrame, p: RevertParams = REVERT) -> pd.DataFrame:
    """Detected events of C: raw condition + at most one event per 24 h (no detected event in (t - 24 h, t))."""
    raw = revert_events_raw(bars_1h, p)
    kept = thin_events(pd.DatetimeIndex(raw.index), pd.Timedelta(hours=p.dedup_hours))
    return raw.loc[kept]


def squeeze_q(bars_4h: pd.DataFrame, p: SqueezeParams = SQUEEZE) -> pd.Series:
    """Q(t) = sd of the last 30 log returns 4h / sd of the last 180 (ddof 1); needs 181 complete bars."""
    r = log_returns(bars_4h["close"])
    return r.rolling(p.short_window).std(ddof=1) / r.rolling(p.long_window).std(ddof=1)


def squeeze_threshold(bars_4h_training: pd.DataFrame, p: SqueezeParams = SQUEEZE) -> float:
    """q20 = 20th percentile (``numpy.quantile``, linear) of Q over the TRAINING bars passed in (E5)."""
    q = squeeze_q(bars_4h_training, p).dropna()
    if q.empty:
        raise ValueError("no Q value in the training bars: cannot estimate q20")
    return float(np.quantile(q.to_numpy(), p.quantile, method="linear"))


def squeeze_signals(bars_4h: pd.DataFrame, q20: float, p: SqueezeParams = SQUEEZE) -> pd.Series:
    """Bool at t: Q(t-1) <= q20 and C_t > max(H_(t-30) .. H_(t-1)). Entry would be at O_(t+1); not simulated here."""
    q_prev = squeeze_q(bars_4h, p).shift(1)
    channel_high = bars_4h["high"].shift(1).rolling(p.channel_in).max()
    return (q_prev <= q20) & (bars_4h["close"] > channel_high)
