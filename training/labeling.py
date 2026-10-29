"""Triple-barrier labels, net of fees (spec sez. 3.1.3).

``triple_barrier`` follows the algorithm of sez. 3.1.3 exactly. All price rules come from
``modules.dryrun.fill_model`` (``BarFillModel`` kernels) and all costs from ``modules.risk.costs``
(``FeeRates``, ``CostParams``): this file contains the scan, not a copy of the fill or cost formulas.

Index convention: the sample ``t`` is the index of the last CLOSED 1m bar (``ts_bar_close = ts[t] + 1 min``,
sez. 2.2.1.b). The runtime computes the decision at ``ts_bar_close + S`` and the order exists from bar
``t + F`` on (``F = entry_first_bar = 1 + ceil(S / 60) = 3``): bars ``t+1 .. t+F-1`` have already elapsed. A maker
order is priced at ``close[t+F-1]`` and rests on bars ``[t+F, t+F+T_in-1]``; a taker order enters at ``open[t+F]``.
In the fill bar only the stop acts (no bracket exists yet); the take-profit counts from the next bar.

Outcome codes (``int8``):

| code | name       | meaning                                                                  |
|------|------------|--------------------------------------------------------------------------|
| 0    | NO_FILL    | maker entry never filled within ``entry_timeout_min`` (no trade)         |
| 1    | UPPER      | take-profit touched first                                                |
| 2    | LOWER      | stop touched first (also when both barriers fall in the same bar)        |
| 3    | VERTICAL   | horizon reached strictly inside the barriers                             |
| 4    | UNRESOLVED | the window runs past the last bar, or the ATR is missing: not a label    |
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

import numba
import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict

from modules.dryrun.fill_model import (
    maker_entry_filled,
    maker_entry_limit,
    market_exit_px,
    sl_exit_px,
    taker_entry_px,
    tp_touched,
)
from modules.market.indicators_batch import rv_per_day_bps
from modules.risk.costs import CostParams, FeeRates
from modules.risk.label_config import LabelConfig, LoadedLabelConfig, load_label_config

__all__ = [
    "LOWER",
    "NO_FILL",
    "UNRESOLVED",
    "UPPER",
    "VERTICAL",
    "LabelConfig",
    "LoadedLabelConfig",
    "LongSamples",
    "TripleBarrierResult",
    "forward_rv_bucket",
    "label_window_end",
    "load_label_config",
    "synthesize_long_samples",
    "triple_barrier",
]

NO_FILL: Final = 0
UPPER: Final = 1
LOWER: Final = 2
VERTICAL: Final = 3
UNRESOLVED: Final = 4

_BPS = 1e4
VOL_REF_DAYS: Final = 365


class TripleBarrierResult(BaseModel):
    """Columnar result, one entry per sample (numpy arrays)."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    outcome: np.ndarray  # int8
    fill_idx: np.ndarray  # int32, -1 without a fill
    exit_idx: np.ndarray  # int32, -1 without an exit
    net_ret: np.ndarray  # float64, NaN for NO_FILL / UNRESOLVED
    entry_px: np.ndarray  # float64, NaN without a fill
    exit_px: np.ndarray  # float64, NaN without an exit (includes the fees' price effects, not the fees)


# ------------------------------------------------------------------------------------------- kernel


@numba.njit(parallel=True)  # no cache=True: numba does not track the fill_model.py kernels it calls (stale labels)
def _scan(  # pragma: no cover - compiled; exercised through triple_barrier
    o: np.ndarray,
    h: np.ndarray,
    low: np.ndarray,
    c: np.ndarray,
    atr: np.ndarray,
    t_idx: np.ndarray,
    spread: np.ndarray,
    maker: bool,
    k_tp: float,
    k_sl: float,
    horizon: int,
    timeout: int,
    first_bar: int,
    f_maker: float,
    f_taker: float,
    slip: float,
    x: float,
    out_outcome: np.ndarray,
    out_fill: np.ndarray,
    out_exit: np.ndarray,
    out_net: np.ndarray,
    out_entry: np.ndarray,
    out_exitpx: np.ndarray,
) -> None:
    n = c.shape[0]
    for i in numba.prange(t_idx.shape[0]):
        t = t_idx[i]
        s = spread[i]
        a = atr[t]
        out_outcome[i] = 4
        out_fill[i] = -1
        out_exit[i] = -1
        out_net[i] = np.nan
        out_entry[i] = np.nan
        out_exitpx[i] = np.nan
        if not (a > 0.0) or t + first_bar > n - 1:
            continue
        # ---- entry
        if maker:
            limit = maker_entry_limit(c[t + first_bar - 1], s)  # best bid estimated when the order is sent
            fill = -1
            window_end = t + first_bar + timeout - 1  # the order rests exactly `timeout` bars
            last = min(window_end, n - 1)
            for k in range(t + first_bar, last + 1):
                if maker_entry_filled(low[k], limit):
                    fill = k
                    break
            if fill < 0:
                if window_end <= n - 1:
                    out_outcome[i] = 0  # NO_FILL: the whole entry window was observed
                continue
            entry = limit
            f_in = f_maker
        else:
            fill = t + first_bar
            entry = taker_entry_px(o[fill], s, slip)
            f_in = f_taker
        vertical = fill + horizon
        if vertical > n - 1:
            continue  # the horizon is not fully observed yet
        out_fill[i] = fill
        out_entry[i] = entry
        upper = entry + k_tp * a
        lower = entry - k_sl * a
        # ---- barriers on bars [fill, vertical]; in the fill bar only the stop acts (the bracket does not exist yet:
        # it is placed after the terminal state of the entry, and the watchdog covers the unprotected position)
        outcome = 3
        exit_px = 0.0
        f_out = f_taker
        exit_idx = vertical
        for j in range(fill, vertical + 1):
            if low[j] <= lower:  # also when the same bar touches the upper barrier: conservative LOWER
                outcome = 2
                exit_idx = j
                exit_px = sl_exit_px(lower, o[j], s, slip, x)
                break
            if j > fill and tp_touched(h[j], upper):
                outcome = 1
                exit_idx = j
                exit_px = upper
                f_out = f_maker
                break
        if outcome == 3:
            exit_px = market_exit_px(c[vertical], s, slip)
        out_outcome[i] = outcome
        out_exit[i] = exit_idx
        out_exitpx[i] = exit_px
        out_net[i] = exit_px * (1.0 - f_out / 1e4) / (entry * (1.0 + f_in / 1e4)) - 1.0


def _as_f64(name: str, a: np.ndarray, n: int | None = None) -> np.ndarray:
    arr = np.ascontiguousarray(a, dtype=np.float64)
    if arr.ndim != 1 or (n is not None and arr.shape[0] != n):
        raise ValueError(f"{name} must be a 1-D array of length {n}")
    return arr


def triple_barrier(
    open_: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    atr: np.ndarray,
    t_idx: np.ndarray,
    label_config: LabelConfig,
    fees: FeeRates,
    costs: CostParams,
    spread_bps: float | np.ndarray,
) -> TripleBarrierResult:
    """Label every sample ``t_idx`` (indices of closed 1m bars). ``atr`` is ``atr_14_<atr_tf>`` in PRICE units
    aligned to the bars. ``spread_bps`` is a scalar or one value per sample."""
    n = len(close)
    o, h, lo, c = (_as_f64(nm, a, n) for nm, a in (("open", open_), ("high", high), ("low", low), ("close", close)))
    a_arr = _as_f64("atr", atr, n)
    t = np.ascontiguousarray(t_idx, dtype=np.int64)
    if t.ndim != 1 or (t.size and (t.min() < 0 or t.max() >= n)):
        raise ValueError("t_idx must be 1-D indices inside the bar arrays")
    if np.ndim(spread_bps) == 0:
        spread = np.full(t.shape[0], float(spread_bps))
    else:
        spread = _as_f64("spread_bps", np.asarray(spread_bps), t.shape[0])
    if (spread < 0).any() or np.isnan(spread).any():
        raise ValueError("spread_bps must be finite and >= 0")
    m = t.shape[0]
    out_outcome = np.empty(m, dtype=np.int8)
    out_fill = np.empty(m, dtype=np.int32)
    out_exit = np.empty(m, dtype=np.int32)
    out_net = np.empty(m, dtype=np.float64)
    out_entry = np.empty(m, dtype=np.float64)
    out_exitpx = np.empty(m, dtype=np.float64)
    _scan(
        o, h, lo, c, a_arr, t, spread,
        label_config.entry_mode == "maker",
        label_config.k_tp, label_config.k_sl,
        label_config.horizon_min, label_config.entry_timeout_min, label_config.entry_first_bar,
        fees.maker_bps, fees.taker_bps, costs.slippage_buffer_bps, costs.stop_extra_slip_bps,
        out_outcome, out_fill, out_exit, out_net, out_entry, out_exitpx,
    )  # fmt: skip
    return TripleBarrierResult(outcome=out_outcome, fill_idx=out_fill, exit_idx=out_exit, net_ret=out_net,
                               entry_px=out_entry, exit_px=out_exitpx)  # fmt: skip


# ----------------------------------------------------------------------------------- vol_regime label


def forward_rv_bucket(close_1h: pd.Series, t: pd.DatetimeIndex, horizon_min: int) -> np.ndarray:
    """``vol_regime`` bucket 0..4 per sample, -1 where there is no label (sez. 3.1.3).

    ``close_1h``: closes of the CLOSED hourly bars, indexed by bar start (UTC), contiguous (``aggregate_bars``).
    ``t``: decision instants (``ts_bar_close``). With ``R = RVd(H/60)`` (``indicators_batch.rv_per_day_bps``, the
    same function as the state), ``R(E)`` is the value known at instant ``E`` (hourly bar starting at ``E - 1h``)::

        fwd(t) = R(E_t + H),  E_t = ceil_to_hour(t),  F_t = floor_to_hour(t)
        ref(t) = {R(E) : F_t - 365 d < E <= F_t}
        bucket = searchsorted(quantile(ref, [.2, .4, .6, .8]), fwd, side="right")

    Quantiles use only values at or before ``F_t``. No label if ``fwd`` or any value of ``ref`` is NaN.
    """
    if horizon_min % 60 != 0:
        raise ValueError("horizon_min must be a whole number of hours")
    m = horizon_min // 60
    idx = pd.DatetimeIndex(close_1h.index)
    if len(idx) and not (idx[1:] - idx[:-1] == pd.Timedelta(hours=1)).all():
        raise ValueError("close_1h must be a contiguous hourly series")
    rv = rv_per_day_bps(close_1h.to_numpy(dtype=np.float64), m)  # rv[k]: known at idx[k] + 1h
    out = np.full(len(t), -1, dtype=np.int8)
    if len(idx) == 0:
        return out
    first_known = idx[0] + pd.Timedelta(hours=1)  # instant E of rv[0]
    ref_len = VOL_REF_DAYS * 24
    t_arr = pd.DatetimeIndex(t)
    floor_h = t_arr.floor("h")
    ceil_h = t_arr.ceil("h")
    hours = pd.Timedelta(hours=1)
    cache: dict[pd.Timestamp, np.ndarray | None] = {}
    for i in range(len(t_arr)):
        e_t = ceil_h[i] + pd.Timedelta(minutes=horizon_min)
        k_fwd = int((e_t - first_known) / hours)
        if k_fwd < 0 or k_fwd >= len(rv) or math.isnan(rv[k_fwd]):
            continue
        f_t = floor_h[i]
        if f_t not in cache:
            k_hi = int((f_t - first_known) / hours)  # R(F_t)
            k_lo = k_hi - ref_len + 1  # R(F_t - 365 d + 1h)
            if k_lo < 0 or k_hi >= len(rv):
                cache[f_t] = None
            else:
                ref = rv[k_lo : k_hi + 1]
                cache[f_t] = None if np.isnan(ref).any() else np.quantile(ref, [0.2, 0.4, 0.6, 0.8], method="linear")
        q = cache[f_t]
        if q is None:
            continue
        out[i] = int(np.searchsorted(q, rv[k_fwd], side="right"))
    return out


def label_window_end(ts_bar_close: pd.DatetimeIndex, cfg: LabelConfig) -> pd.DatetimeIndex:
    """Last instant whose data a label of each sample can use (purge, selection window, LabelResolver, backtest).

    ``max(ts + (F + T_in - 1) min, ceil_to_hour(ts)) + H min``: the first term is the close of the vertical bar in the
    worst case (fill on the last bar of the entry window), the second the data read by ``forward_rv_bucket``
    (``E_t + H``).
    """
    ts = pd.DatetimeIndex(ts_bar_close)
    entry_end = ts + pd.Timedelta(minutes=cfg.entry_first_bar + cfg.entry_timeout_min - 1)
    ceil_h = ts.ceil("h")
    first = entry_end.where(entry_end >= ceil_h, ceil_h)
    return first + pd.Timedelta(minutes=cfg.horizon_min)


# ------------------------------------------------------------------------------ synthetic LONG samples


@dataclass(frozen=True)
class LongSamples:
    """Synthetic LONG states for the ``action`` question (sez. 3.1.3 and decision O3.3), aligned arrays.

    ``keep`` indexes the input samples that produced a usable LONG state; every other array has one entry per kept
    sample. The fictitious entry is the order the runtime would really have sent when deciding at ``t_e = t - U``.
    """

    keep: np.ndarray  # positions into the input t_idx
    t_e: np.ndarray  # bar index of the fictitious entry decision
    fill_idx: np.ndarray
    entry_px: np.ndarray
    outcome_br: np.ndarray  # outcome of the fictitious bracket (UPPER / LOWER / VERTICAL)
    exit_idx_br: np.ndarray
    exit_px_br: np.ndarray
    sell: np.ndarray  # bool: target SELL (True) or HOLD (False)


def synthesize_long_samples(
    open_: np.ndarray, high: np.ndarray, low: np.ndarray, close: np.ndarray, atr: np.ndarray, t_idx: np.ndarray,
    cfg: LabelConfig, fees: FeeRates, costs: CostParams, spread_t: np.ndarray,
    spread_at: Callable[[np.ndarray], np.ndarray], rng: np.random.Generator,
) -> LongSamples:  # fmt: skip
    """One fictitious LONG per sample ``t`` (``U ~ Uniform{60..H}`` minutes), discarded when it would not exist.

    Discarded: entry never filled or window unobserved, not yet filled at ``t`` (``fill >= t``), or the bracket closes
    before a SELL decided at ``t`` could reach the market (``exit < t + F``: the decision would have no effect).
    Target: SELL iff selling now at ``open[t+F]`` (taker costs) beats what the bracket gives::

        V_now = market_exit_px(open[t+F], s_t, slip) * (1 - f_t)
        V_br  = exit_px_br * (1 - f_out_br),  f_out_br = f_m if UPPER else f_t
    """
    t = np.ascontiguousarray(t_idx, dtype=np.int64)
    n = len(close)
    u = rng.integers(60, cfg.horizon_min + 1, size=t.shape[0])
    t_e = t - u
    ok = t_e >= 0
    pos = np.flatnonzero(ok)
    res = triple_barrier(
        open_, high, low, close, atr, t_e[pos], cfg, fees, costs, np.asarray(spread_at(t_e[pos]), dtype=np.float64)
    )
    resolved = np.isin(res.outcome, (UPPER, LOWER, VERTICAL))
    fill, exit_ = res.fill_idx.astype(np.int64), res.exit_idx.astype(np.int64)
    tt = t[pos]
    usable = resolved & (fill < tt) & (exit_ >= tt + cfg.entry_first_bar) & (tt + cfg.entry_first_bar <= n - 1)
    sel = np.flatnonzero(usable)
    keep = pos[sel]
    kt = t[keep]
    s_now = np.asarray(spread_t, dtype=np.float64)[keep]
    v_now = np.array([market_exit_px(float(open_[x + cfg.entry_first_bar]), float(sp), costs.slippage_buffer_bps)
                      for x, sp in zip(kt, s_now, strict=True)])  # fmt: skip
    v_now = v_now * (1.0 - fees.taker_bps / 1e4)
    outcome = res.outcome[sel]
    f_out = np.where(outcome == UPPER, fees.maker_bps, fees.taker_bps)
    v_br = res.exit_px[sel] * (1.0 - f_out / 1e4)
    return LongSamples(
        keep, t_e[keep], fill[sel], res.entry_px[sel], outcome, exit_[sel], res.exit_px[sel], v_now > v_br
    )
