"""The only place where a ``DecisionState`` becomes text (spec sez. 2.3 B.3).

Training (``training/build_dataset.py``) and the runtime import ``serialize_state`` from here: a different string
for the same state would be train/serve skew. ``flatten_state`` is the numeric view used by the LightGBM baseline
and by the drift monitor; its column list is derived from the schema, so it cannot disagree with it.
"""

from __future__ import annotations

import hashlib
import math
from typing import Final

from modules.features.schemas import DecisionState
from modules.market.indicator_defs import SEQ_1D_LEN, SEQ_1H_LEN

__all__ = ["FLAT_COLUMNS", "flatten_state", "serialize_state", "state_sha256"]


def serialize_state(state: DecisionState) -> str:
    """Canonical one-line JSON: declaration order of the schema, no spaces, aliases of the spec."""
    return state.model_dump_json(by_alias=True)


def state_sha256(serialized: str) -> str:
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


_NAN: Final = math.nan

_RET_KEYS: Final = ("15m", "1h", "6h", "1d", "3d", "7d")
_ASSET_KEYS: Final = (
    "ewm", "ewm_slow", "mass", "mass_slow", "disp", "shock_15m", "n_1h", "n_24h", "last_choice", "last_age_min",
)  # fmt: skip
_MARKET_KEYS: Final = ("ewm", "ewm_slow", "mass", "mass_slow", "disp", "shock_15m", "n_1h")

FLAT_COLUMNS: Final[tuple[str, ...]] = (
    "clock_hour_utc", "clock_dow",
    "pos_is_long", "pos_hours_held", "pos_unreal_bps", "pos_dist_sl_atr", "pos_dist_tp_atr",
    *(f"ret_{k}_bps" for k in _RET_KEYS),
    *(f"seq_1h_bps_{j:02d}" for j in range(SEQ_1H_LEN)),
    *(f"seq_1d_bps_{j}" for j in range(SEQ_1D_LEN)),
    "dist_vwap_1d_bps", "bb_pos_1d", "hh_ll_pos_30d",
    "rsi_14_1h", "rsi_14_6h", "rsi_14_1d", "macd_hist_1d_bps",
    "atr_14_1h_bps", "atr_14_6h_bps", "atr_14_1d_bps", "rv_1d_bps", "rv_ratio_1d_30d", "atr_1d_pctile_365d",
    "vol_z_1d",
    *(f"sent_asset_{k}" for k in _ASSET_KEYS),
    *(f"sent_market_{k}" for k in _MARKET_KEYS),
    "sent_fear_greed",
    "btc_ret_1d_bps", "btc_rv_ratio_1d_30d",
    "cost_is_taker", "cost_tp_bps", "cost_sl_bps", "p_breakeven",
)  # fmt: skip


def _f(x: float | int | None) -> float:
    return _NAN if x is None else float(x)


def flatten_state(state: DecisionState) -> dict[str, float]:
    """Numeric view of a state, in ``FLAT_COLUMNS`` order; missing values are NaN (LightGBM handles them natively)."""
    pos, price, mom, vol, liq = state.position, state.price, state.momentum, state.volatility, state.liquidity
    r = price.ret_bps
    ret = {"15m": r.m15, "1h": r.h1, "6h": r.h6, "1d": r.d1, "3d": r.d3, "7d": r.d7}
    seq1h: list[int | None] = list(price.seq_1h_bps) if price.seq_1h_bps is not None else [None] * SEQ_1H_LEN
    seq1d: list[int | None] = list(price.seq_1d_bps) if price.seq_1d_bps is not None else [None] * SEQ_1D_LEN
    sent, btc, costs = state.sentiment, state.btc_context, state.costs
    a = sent.asset.model_dump() if sent is not None and sent.asset is not None else {}
    m = sent.market.model_dump() if sent is not None and sent.market is not None else {}
    values: dict[str, float] = {
        "clock_hour_utc": float(state.clock.hour_utc),
        "clock_dow": float(state.clock.dow),
        "pos_is_long": 1.0 if pos.side == "LONG" else 0.0,
        "pos_hours_held": float(pos.hours_held),
        "pos_unreal_bps": _f(pos.unreal_bps),
        "pos_dist_sl_atr": _f(pos.dist_sl_atr),
        "pos_dist_tp_atr": _f(pos.dist_tp_atr),
        **{f"ret_{k}_bps": _f(v) for k, v in ret.items()},
        **{f"seq_1h_bps_{j:02d}": _f(v) for j, v in enumerate(seq1h)},
        **{f"seq_1d_bps_{j}": _f(v) for j, v in enumerate(seq1d)},
        "dist_vwap_1d_bps": _f(price.dist_vwap_1d_bps),
        "bb_pos_1d": _f(price.bb_pos_1d),
        "hh_ll_pos_30d": _f(price.hh_ll_pos_30d),
        "rsi_14_1h": _f(mom.rsi_14_1h),
        "rsi_14_6h": _f(mom.rsi_14_6h),
        "rsi_14_1d": _f(mom.rsi_14_1d),
        "macd_hist_1d_bps": _f(mom.macd_hist_1d_bps),
        "atr_14_1h_bps": _f(vol.atr_14_1h_bps),
        "atr_14_6h_bps": _f(vol.atr_14_6h_bps),
        "atr_14_1d_bps": _f(vol.atr_14_1d_bps),
        "rv_1d_bps": _f(vol.rv_1d_bps),
        "rv_ratio_1d_30d": _f(vol.rv_ratio_1d_30d),
        "atr_1d_pctile_365d": _f(vol.atr_1d_pctile_365d),
        "vol_z_1d": _f(liq.vol_z_1d),
        **{f"sent_asset_{k}": _f(a.get(k)) for k in _ASSET_KEYS},
        **{f"sent_market_{k}": _f(m.get(k)) for k in _MARKET_KEYS},
        "sent_fear_greed": _f(sent.fear_greed) if sent is not None else _NAN,
        "btc_ret_1d_bps": _f(btc.ret_1d_bps) if btc is not None else _NAN,
        "btc_rv_ratio_1d_30d": _f(btc.rv_ratio_1d_30d) if btc is not None else _NAN,
        "cost_is_taker": 1.0 if costs.entry_mode == "taker" else 0.0,
        "cost_tp_bps": float(costs.cost_tp_bps),
        "cost_sl_bps": float(costs.cost_sl_bps),
        "p_breakeven": _f(costs.p_breakeven),
    }
    return {c: values[c] for c in FLAT_COLUMNS}
