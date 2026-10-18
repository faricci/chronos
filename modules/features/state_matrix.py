"""Vectorised flat feature matrix of FLAT states (used by the backtest and by batch scoring).

``StateBuilder.build`` + ``flatten_state`` cost ~0.3 ms per state: millions of minute-by-minute states would take
the better part of an hour. This module produces the SAME numbers (``tests/test_state_matrix.py`` asserts exact
equality with the scalar path) with numpy, so the backtest scores the model on exactly the features the runtime
would feed it:

- integer fields: ``numpy.rint`` (half to even, exactly like Python's ``round(x)``) then the clamps of the builder;
- two-decimal fields: Python's ``round(x, 2)`` element by element (``numpy.round`` can differ on ties);
- FLAT position block, no sentiment, ``costs`` from the label_config, BTC context from the BTC rows.

Only FLAT states are covered; a LONG state depends on the position and is built row by row with ``StateBuilder``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from modules.features.serializer import FLAT_COLUMNS
from modules.market.indicator_defs import SEQ_1D_COLUMNS, SEQ_1H_COLUMNS
from modules.risk.costs import CostModel, round_bps
from modules.risk.label_config import LoadedLabelConfig

__all__ = ["flat_matrix", "long_matrix"]

BPS_CLAMP = 5000
_COL = {c: i for i, c in enumerate(FLAT_COLUMNS)}


def _int(x: np.ndarray, lo: float = -BPS_CLAMP, hi: float = BPS_CLAMP) -> np.ndarray:
    return np.asarray(np.clip(np.rint(x), lo, hi))  # NaN stays NaN


def _dec2(x: np.ndarray, lo: float | None = None, hi: float | None = None) -> np.ndarray:
    v = x if lo is None else np.clip(x, lo, hi)
    out = np.full(v.shape, np.nan)
    ok = np.flatnonzero(~np.isnan(v))
    out[ok] = [round(float(a), 2) for a in v[ok].tolist()]
    return out


def flat_matrix(
    ind: pd.DataFrame,
    rows: np.ndarray,
    product: str,
    label: LoadedLabelConfig,
    btc_ind: pd.DataFrame | None = None,
    btc_rows: np.ndarray | None = None,
) -> np.ndarray:
    """Rows of ``FLAT_COLUMNS`` (float64, NaN = missing) for the FLAT states at bar indices ``rows`` of ``ind``.

    ``btc_rows``: row of ``btc_ind`` aligned to each sample (-1 where BTC has no bar); ignored for BTC products.
    """
    n = len(rows)
    out = np.full((n, len(FLAT_COLUMNS)), np.nan)
    t = pd.DatetimeIndex(ind.index[rows]) + pd.Timedelta(minutes=1)
    cfg = label.cfg

    def col(name: str) -> np.ndarray:
        return np.asarray(ind[name].to_numpy(dtype=np.float64)[rows])

    out[:, _COL["clock_hour_utc"]] = t.hour
    out[:, _COL["clock_dow"]] = t.weekday
    out[:, _COL["pos_is_long"]] = 0.0
    out[:, _COL["pos_hours_held"]] = 0.0
    for name in ("15m", "1h", "6h", "1d", "3d", "7d"):
        out[:, _COL[f"ret_{name}_bps"]] = _int(col(f"ret_{name}_bps"))
    for cols in (SEQ_1H_COLUMNS, SEQ_1D_COLUMNS):  # a sequence with any missing value is missing entirely
        block = np.column_stack([col(c) for c in cols])
        partial = np.isnan(block).any(axis=1)
        block = _int(block)
        block[partial] = np.nan
        for j, c in enumerate(cols):
            out[:, _COL[c]] = block[:, j]
    out[:, _COL["dist_vwap_1d_bps"]] = _int(col("dist_vwap_1d_bps"))
    out[:, _COL["bb_pos_1d"]] = _dec2(col("bb_pos_1d"), -1.0, 2.0)
    out[:, _COL["hh_ll_pos_30d"]] = _dec2(col("hh_ll_pos_30d"))
    for c in ("rsi_14_1h", "rsi_14_6h", "rsi_14_1d"):
        out[:, _COL[c]] = _int(col(c), 0, 100)
    out[:, _COL["macd_hist_1d_bps"]] = _int(col("macd_hist_1d_bps"))
    for c in ("atr_14_1h_bps", "atr_14_6h_bps", "atr_14_1d_bps", "rv_1d_bps"):
        out[:, _COL[c]] = _int(col(c), 0, 10**6)
    out[:, _COL["rv_ratio_1d_30d"]] = _dec2(col("rv_ratio_1d_30d"))
    out[:, _COL["atr_1d_pctile_365d"]] = _int(col("atr_1d_pctile_365d"), 0, 100)
    out[:, _COL["vol_z_1d"]] = _dec2(col("vol_z_1d"), -6.0, 6.0)

    if btc_ind is not None and btc_rows is not None and not product.startswith("BTC-"):
        have = btc_rows >= 0
        bret = np.full(n, np.nan)
        brv = np.full(n, np.nan)
        bret[have] = btc_ind["ret_1d_bps"].to_numpy(dtype=np.float64)[btc_rows[have]]
        brv[have] = btc_ind["rv_ratio_1d_30d"].to_numpy(dtype=np.float64)[btc_rows[have]]
        out[:, _COL["btc_ret_1d_bps"]] = _int(bret)
        out[:, _COL["btc_rv_ratio_1d_30d"]] = _dec2(brv)

    # costs: fees, slippage and spread of the label_config (identical to StateBuilder._costs). The spread may one day
    # depend on the hour of the day, so the cost model is built once per distinct spread value.
    by_hour = {int(h): label.spread_for(product, t[t.hour == h][0].to_pydatetime()) for h in np.unique(t.hour)}
    spread = np.array([by_hour[int(h)] for h in t.hour], dtype=np.float64)
    out[:, _COL["cost_is_taker"]] = 1.0 if cfg.entry_mode == "taker" else 0.0
    atr_bps = col(f"atr_14_{cfg.atr_tf}_bps")
    p_be = np.full(n, np.nan)
    for value in np.unique(spread):
        m = spread == value
        model = CostModel(fees=label.fees, entry_mode=cfg.entry_mode, spread_bps=float(value), params=label.costs)
        out[m, _COL["cost_tp_bps"]] = round_bps(model.cost_tp)
        out[m, _COL["cost_sl_bps"]] = round_bps(model.cost_sl)
        win = cfg.k_tp * atr_bps[m] - model.cost_tp
        loss = cfg.k_sl * atr_bps[m] + model.cost_sl
        with np.errstate(divide="ignore", invalid="ignore"):
            p_be[m] = np.where(win > 0, loss / (win + loss), np.nan)
    out[:, _COL["p_breakeven"]] = _dec2(p_be)
    return out


def long_matrix(
    base: np.ndarray,
    ts_close: pd.DatetimeIndex,
    close: np.ndarray,
    *,
    entry_px: float,
    stop_loss: float,
    take_profit: float,
    atr_entry: float,
    opened_at: pd.Timestamp,
) -> np.ndarray:
    """Turn FLAT rows into the LONG states of ONE open position: only the ``pos_*`` block changes.

    ``base``: ``flat_matrix`` rows for the decision bars; ``ts_close`` / ``close``: their decision instants and the
    close of the bar ending there; the position numbers are the ones ``PositionInput.from_view`` gives (floats).
    Same formulas as ``StateBuilder._position`` (pinned by ``tests/test_state_matrix.py``).
    """
    out = np.array(base, copy=True)
    now = ts_close.tz_convert(None) if ts_close.tz is not None else ts_close  # compare as naive UTC
    since = opened_at.tz_convert(None) if opened_at.tzinfo is not None else opened_at
    hours = np.maximum(0.0, np.floor((now - since).total_seconds().to_numpy() / 3600.0))
    out[:, _COL["pos_is_long"]] = 1.0
    out[:, _COL["pos_hours_held"]] = hours
    out[:, _COL["pos_unreal_bps"]] = _int(1e4 * (close / entry_px - 1.0))
    out[:, _COL["pos_dist_sl_atr"]] = _dec2((close - stop_loss) / atr_entry, -20.0, 20.0)
    out[:, _COL["pos_dist_tp_atr"]] = _dec2((take_profit - close) / atr_entry, -20.0, 20.0)
    return out
