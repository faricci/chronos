"""A LoadedLabelConfig equal to the real one (spec 2.7 outcome) without reading the git-ignored data file."""

from __future__ import annotations

import pandas as pd

from modules.risk.costs import CostParams, FeeRates
from modules.risk.label_config import LabelConfig, LoadedLabelConfig

FEES_REAL = FeeRates(maker_bps=25, taker_bps=50, assumed=False)
COSTS = CostParams()  # slip 5, stop extra 10
SPREADS = {"BTC-EUR": 3.0, "ETH-EUR": 3.0, "SOL-EUR": 3.0}
HOLDOUT_START = pd.Timestamp("2026-06-20", tz="UTC")


def loaded_label(
    spreads: dict[str, float] | None = None, holdout_start: pd.Timestamp = HOLDOUT_START, **cfg_kw: object
) -> LoadedLabelConfig:
    kw: dict[str, object] = {"atr_tf": "6h", "k_tp": 3.0, "k_sl": 1.5, "entry_mode": "maker", "horizon_min": 15 * 1440}
    kw.update(cfg_kw)
    cfg = LabelConfig(**kw)  # type: ignore[arg-type]
    sp = dict(SPREADS if spreads is None else spreads)
    raw = {"sha256": cfg.sha256(FEES_REAL, COSTS, sp), "selection": {"holdout_start": holdout_start.isoformat()}}
    return LoadedLabelConfig(cfg, FEES_REAL, COSTS, sp, holdout_start, raw)
