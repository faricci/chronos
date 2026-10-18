"""DecisionPolicy: answers -> Decision (spec sez. 2.1.2). Pure function, no I/O.

Order of the rules:
1. mask the actions the position state forbids (C1: FLAT cannot SELL, LONG cannot BUY: no pyramiding) and renormalize;
2. regime = argmax of ``vol_regime.probabilities``; the entry veto on the regime exists ONLY here;
3. thresholds of the model's ``policy.json``:
   BUY  (FLAT)  if ``p(BUY) >= tau_buy`` and ``confidence >= c_min`` and ``p_edge >= p_edge_min`` and
                ``regime in allowed_regimes_for_entry``;
   SELL (LONG)  if ``p(SELL) >= tau_sell``;
   otherwise HOLD, with the reason that stopped it.
The confidence is recomputed here from the backend's action probabilities (``1 - H/ln K``, K = 3) so that all
backends are comparable; a confidence sent by the backend is ignored. A decider failure is not decided here: the engine
builds ``Decision.unavailable()``.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from modules.decision.protocol import DecisionAnswers, confidence_from_probs, regime_from_probs

__all__ = ["Decision", "DecisionPolicy", "PolicyConfig", "PolicyMismatchError", "load_policy"]

Action = Literal["BUY", "SELL", "HOLD"]
Side = Literal["FLAT", "LONG"]


class PolicyMismatchError(Exception):
    """The policy.json was tuned for a different label_config than the model's (sez. 2.2.5)."""


class PolicyConfig(BaseModel):
    """``policy.json``: thresholds of ONE model version, produced by ``training/tune_policy.py``."""

    model_config = ConfigDict(frozen=True, extra="allow")

    model: str
    tau_buy: float = Field(ge=0, le=1)
    tau_sell: float = Field(ge=0, le=1)
    c_min: float = Field(ge=0, le=1)
    p_edge_min: float = Field(ge=0, le=1)
    allowed_regimes_for_entry: tuple[int, ...]
    label_config_sha256: str


def load_policy(path: Path, label_config_sha256: str) -> PolicyConfig:
    """Load ``policy.json`` and refuse it when it does not belong to the model's label_config."""
    policy = PolicyConfig.model_validate(json.loads(path.read_text(encoding="utf-8")))
    if policy.label_config_sha256 != label_config_sha256:
        raise PolicyMismatchError(
            f"{path}: label_config_sha256 {policy.label_config_sha256[:12]} != model card {label_config_sha256[:12]}"
        )
    return policy


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str
    p_edge: float | None = None
    regime: int | None = None
    confidence: float | None = None
    masked_probs: dict[str, float] = field(default_factory=dict)

    @classmethod
    def unavailable(cls) -> Decision:
        return cls(action="HOLD", reason="decider_unavailable")


class DecisionPolicy:
    def __init__(self, config: PolicyConfig) -> None:
        self.config = config

    def resolve(self, answers: DecisionAnswers, side: Side) -> Decision:
        cfg = self.config
        raw = answers.action.probabilities
        confidence = confidence_from_probs(raw)
        p_edge = answers.edge.noul
        regime = regime_from_probs(answers.vol_regime.probabilities)

        valid = ("BUY", "HOLD") if side == "FLAT" else ("SELL", "HOLD")
        total = sum(raw[a] for a in valid)
        if total <= 0.0:
            return Decision("HOLD", "no_valid_action_mass", p_edge, regime, confidence)
        masked = {a: raw[a] / total for a in valid}

        def hold(reason: str) -> Decision:
            return Decision("HOLD", reason, p_edge, regime, confidence, masked)

        if side == "LONG":
            if masked["SELL"] >= cfg.tau_sell:
                return Decision("SELL", "thresholds_met", p_edge, regime, confidence, masked)
            return hold("below_threshold")

        if masked["BUY"] < cfg.tau_buy or confidence < cfg.c_min:
            return hold("below_threshold")
        if p_edge < cfg.p_edge_min:
            return hold("edge_veto")
        if regime not in cfg.allowed_regimes_for_entry:
            return hold("regime_veto")
        return Decision("BUY", "thresholds_met", p_edge, regime, confidence, masked)

    # ------------------------------------------------------------------------------ vectorised twins

    def buy_mask(self, p_action: np.ndarray, p_edge: np.ndarray, p_regime: np.ndarray) -> np.ndarray:
        """``resolve(...) == BUY`` for many FLAT states at once (the backtest scores millions of minutes).

        Same rules as ``resolve`` and pinned to it by ``tests/test_decision_policy.py``: masked P(BUY) over BUY+HOLD,
        confidence from the raw action probabilities, edge and regime gates.
        """
        cfg = self.config
        p = np.asarray(p_action, dtype=np.float64)
        total = p[:, 0] + p[:, 2]  # BUY, HOLD
        with np.errstate(divide="ignore", invalid="ignore"):
            masked_buy = np.where(total > 0, p[:, 0] / total, 0.0)
            h = -np.where(p > 0, p * np.log(np.where(p > 0, p, 1.0)), 0.0).sum(axis=1)
        confidence = np.clip(1.0 - h / math.log(p.shape[1]), 0.0, 1.0)
        regime = np.argmax(np.asarray(p_regime, dtype=np.float64), axis=1)
        allowed = np.isin(regime, np.array(cfg.allowed_regimes_for_entry))
        return np.asarray(
            (total > 0) & (masked_buy >= cfg.tau_buy) & (confidence >= cfg.c_min)
            & (np.asarray(p_edge) >= cfg.p_edge_min) & allowed
        )  # fmt: skip

    def sell_mask(self, p_action: np.ndarray) -> np.ndarray:
        """``resolve(...) == SELL`` for many LONG states: masked P(SELL) over SELL+HOLD against ``tau_sell``."""
        p = np.asarray(p_action, dtype=np.float64)
        total = p[:, 1] + p[:, 2]  # SELL, HOLD
        with np.errstate(divide="ignore", invalid="ignore"):
            masked_sell = np.where(total > 0, p[:, 1] / total, 0.0)
        return np.asarray((total > 0) & (masked_sell >= self.config.tau_sell))
