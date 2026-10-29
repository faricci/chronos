"""Shared constants and column names of the market features (spec sez. 2.2.1.b).

No logic here: the formulas live in ``indicators_batch`` (vectorized, training) and ``indicators``
(incremental, runtime), which must agree row by row.
"""

from __future__ import annotations

from typing import Final

RET_WINDOWS_MIN: Final[dict[str, int]] = {"15m": 15, "1h": 60, "6h": 360, "1d": 1440, "3d": 4320, "7d": 10080}
ATR_N: Final = 14
RSI_N: Final = 14
MACD_FAST: Final = 12
MACD_SLOW: Final = 26
MACD_SIGNAL: Final = 9
BB_N: Final = 20
BB_K: Final = 2.0
VWAP_WIN_MIN: Final = 1440
HHLL_WIN_MIN: Final = 43200
RV_1D_BARS: Final = 24
RV_30D_BARS: Final = 720
VOLZ_DAYS: Final = 30
PCTILE_DAYS: Final = 365
PCTILE_MIN_N: Final = 90
SEQ_1H_LEN: Final = 12
SEQ_1D_LEN: Final = 7

HTF_TIMEFRAMES: Final = ("1h", "6h", "1d")

FEATURE_COLUMNS: Final[tuple[str, ...]] = (
    *(f"ret_{name}_bps" for name in RET_WINDOWS_MIN),
    *(f"seq_1h_bps_{j:02d}" for j in range(SEQ_1H_LEN)),
    *(f"seq_1d_bps_{j}" for j in range(SEQ_1D_LEN)),
    "dist_vwap_1d_bps",
    "bb_pos_1d",
    "hh_ll_pos_30d",
    "rsi_14_1h",
    "rsi_14_6h",
    "rsi_14_1d",
    "macd_hist_1d_bps",
    "atr_14_1h",
    "atr_14_6h",
    "atr_14_1d",
    "atr_14_1h_bps",
    "atr_14_6h_bps",
    "atr_14_1d_bps",
    "rv_1d_bps",
    "rv_30d_bps",
    "rv_ratio_1d_30d",
    "atr_1d_pctile_365d",
    "vol_z_1d",
)

SEQ_1H_COLUMNS: Final = tuple(c for c in FEATURE_COLUMNS if c.startswith("seq_1h_bps_"))
SEQ_1D_COLUMNS: Final = tuple(c for c in FEATURE_COLUMNS if c.startswith("seq_1d_bps_"))
SCALAR_COLUMNS: Final = tuple(c for c in FEATURE_COLUMNS if not c.startswith("seq_"))
