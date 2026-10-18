"""``LabelConfig``: the barrier parameters a model was trained for (spec sez. 0.4, 3.1.3).

Shared contract between training (``training/labeling.py``) and the runtime (``RiskManager``, question texts): the
runtime must use exactly the barriers ``p_edge`` was calibrated on, and must not import the training package (numba).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from modules.risk.costs import CostParams, FeeRates

__all__ = ["LabelConfig", "LoadedLabelConfig", "load_label_config"]


class LabelConfig(BaseModel):
    """Barrier parameters of the label (sez. 0.4 defaults). They must equal the RiskManager's (sez. 3.1.3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    atr_tf: Literal["1h", "6h", "1d"] = "1d"
    k_tp: float = Field(default=4.0, gt=0)
    k_sl: float = Field(default=2.0, gt=0)
    entry_mode: Literal["maker", "taker"] = "maker"
    horizon_min: int = Field(default=14_400, ge=1)
    entry_timeout_min: int = Field(default=30, ge=1)
    # First bar, counted from the sample's bar t, in which the entry order exists: the decision is known at the end of
    # bar t and the runtime computes it at ts_bar_close + S, so F = 1 + ceil(S / 60) = 3 with S = 120 s. Same for
    # maker and taker. F = 1 reproduces the semantics of spec 2.6.1 (tests only).
    entry_first_bar: int = Field(default=3, ge=1, le=6)

    @property
    def atr_column(self) -> str:
        return f"atr_14_{self.atr_tf}"

    def sha256(self, fees: FeeRates, costs: CostParams, spread_bps: Mapping[str, float]) -> str:
        """Hash of everything that changes a label: barriers, delay, fees, slippage and the spread per product.

        Nothing that only serves the choice of the parameters (detrend, selection window) is part of it (C7).
        """
        label = self.model_dump()
        label["k_tp"], label["k_sl"] = float(self.k_tp), float(self.k_sl)  # 3 and 3.0 must hash the same
        payload = {
            "label": label,
            "fees_bps": [float(fees.maker_bps), float(fees.taker_bps)],
            "slip_bps": float(costs.slippage_buffer_bps),
            "stop_extra_slip_bps": float(costs.stop_extra_slip_bps),
            "spread_bps": {k: float(spread_bps[k]) for k in sorted(spread_bps)},
            "cost_model": "COST_V1",
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class LoadedLabelConfig:
    """``label_config.json`` after validation: everything a consumer needs, with the hash re-checked."""

    cfg: LabelConfig
    fees: FeeRates
    costs: CostParams
    spread_bps: dict[str, float]
    holdout_start: pd.Timestamp
    raw: dict[str, Any]

    @property
    def sha256(self) -> str:
        return str(self.raw["sha256"])

    def spread_for(self, product: str, ts_bar_close: datetime) -> float:
        """The ONE access to the label spread (bps). Today a constant per product; the hourly table of the recorder
        will arrive in a new label_config (and a new model version) without changing any caller."""
        try:
            return self.spread_bps[product]
        except KeyError:
            raise ValueError(f"no label spread for {product}") from None


def load_label_config(path: Path) -> LoadedLabelConfig:
    """Read ``label_config.json`` and rebuild the objects; a file whose ``sha256`` does not match is refused."""
    raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    try:
        cfg = LabelConfig(
            atr_tf=raw["atr_tf"], k_tp=raw["k_tp"], k_sl=raw["k_sl"], entry_mode=raw["entry_mode"],
            horizon_min=raw["horizon_min"], entry_timeout_min=raw["entry_timeout_min"],
            entry_first_bar=raw["entry_first_bar"],
        )  # fmt: skip
        f = raw["fees_bps"]
        fees = FeeRates(maker_bps=f["maker"], taker_bps=f["taker"], assumed=f["source"] == "assumed")
        costs = CostParams(slippage_buffer_bps=raw["slip_bps"], stop_extra_slip_bps=raw["stop_extra_slip_bps"])
        spreads = {str(k): float(v) for k, v in raw["spread_bps"].items()}
        holdout_start = pd.Timestamp(raw["selection"]["holdout_start"])
        expected = raw["sha256"]
    except (KeyError, TypeError) as e:
        raise ValueError(f"{path}: malformed label_config ({type(e).__name__}: {e})") from e
    if cfg.sha256(fees, costs, spreads) != expected:
        raise ValueError(f"{path}: sha256 does not match its content (edited by hand or another version)")
    return LoadedLabelConfig(cfg, fees, costs, spreads, holdout_start, raw)
