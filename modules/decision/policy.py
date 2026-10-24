"""DecisionPolicy: answers -> Decision (spec sez. 2.1.2). Pure function, no I/O.

Order of the rules:
1. mask the actions the position state forbids (C1: FLAT cannot SELL, LONG cannot BUY: no pyramiding) and renormalize;
2. thresholds of the model's ``policy.json``: BUY (FLAT) needs ``p(BUY) >= tau_buy`` and ``confidence >= c_min``;
   SELL (LONG) needs ``p(SELL) >= tau_sell``; otherwise HOLD, with the reason that stopped it;
3. regime = the state token ``atr_1d_pctile_365d`` cut at fixed points (``REGIME_EDGES_PCT``); the entry veto on the
   regime exists ONLY here and never reads the model's ``vol_regime`` answer (diagnostics only). Entries are allowed in
   ``ENTRY_REGIMES``; an unavailable token means no entry.
The economic floor on ``p_edge`` is not a threshold of the policy: it is the live break-even of the single decision plus
``p_be_buffer`` and lives in the RiskManager (EV gate).
The confidence is recomputed here from the backend's action probabilities (``1 - H/ln K``, K = 3) so that all
backends are comparable; a confidence sent by the backend is ignored. A decider failure is not decided here: the engine
builds ``Decision.unavailable()``.
"""

from __future__ import annotations

import json
import math
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from modules.decision.protocol import DecisionAnswers, confidence_from_probs

__all__ = [
    "ENTRY_REGIMES",
    "REGIME_EDGES_PCT",
    "REGIME_FEATURE",
    "REGIME_RULE",
    "Decision",
    "DecisionPolicy",
    "PolicyConfig",
    "PolicyMismatchError",
    "load_policy",
    "masked_buy_prob",
    "masked_sell_prob",
    "regime_from_pctile",
    "regimes_from_pctile",
]

Action = Literal["BUY", "SELL", "HOLD"]
Side = Literal["FLAT", "LONG"]

REGIME_FEATURE: Final = "atr_1d_pctile_365d"  # column of FLAT_COLUMNS = state.volatility.atr_1d_pctile_365d (0..100)
REGIME_EDGES_PCT: Final = (5.0, 35.0, 65.0, 95.0)  # fixed before tuning, never optimised
REGIME_RULE: Final = "ATR1D_PCTILE365:5/35/65/95"
ENTRY_REGIMES: Final = (1, 2, 3)
_LEGACY_KEYS: Final = ("p_edge_min", "allowed_regimes_for_entry")


def regime_from_pctile(pctile: float | None) -> int | None:
    """Regime 0..4 of an ATR percentile (0..100); ``None`` when the token is missing or not finite."""
    if pctile is None or not math.isfinite(pctile):
        return None
    return bisect_right(REGIME_EDGES_PCT, pctile)


def regimes_from_pctile(pctile: np.ndarray) -> np.ndarray:
    """Vector twin of ``regime_from_pctile``: int64, -1 where the percentile is NaN."""
    x = np.asarray(pctile, dtype=np.float64)
    out = np.searchsorted(np.asarray(REGIME_EDGES_PCT), x, side="right").astype(np.int64)
    return np.where(np.isfinite(x), out, -1)


class PolicyMismatchError(Exception):
    """The policy.json was tuned for a different label_config than the model's (sez. 2.2.5)."""


class PolicyConfig(BaseModel):
    """``policy.json``: thresholds of ONE model version, produced by ``training/tune_policy.py``."""

    model_config = ConfigDict(frozen=True, extra="allow")

    model: str
    tau_buy: float = Field(ge=0, le=1)
    tau_sell: float = Field(ge=0, le=1)
    c_min: float = Field(ge=0, le=1)
    p_be_buffer: float = Field(ge=0, le=0.20)
    regime_rule: Literal["ATR1D_PCTILE365:5/35/65/95"]
    label_config_sha256: str

    @model_validator(mode="before")
    @classmethod
    def _refuse_legacy_keys(cls, data: object) -> object:
        """A policy.json from before the regime rule (or edited by hand) does not load: fail closed."""
        if isinstance(data, dict):
            old = [k for k in _LEGACY_KEYS if k in data]
            if old:
                raise ValueError(f"legacy policy keys {old} are no longer valid; retune the policy")
        return data


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

    def resolve(self, answers: DecisionAnswers, side: Side, *, vol_pctile: float | None) -> Decision:
        """``vol_pctile`` is the state token ``atr_1d_pctile_365d``; ignored while LONG but always required."""
        cfg = self.config
        raw = answers.action.probabilities
        confidence = confidence_from_probs(raw)
        p_edge = answers.edge.noul
        regime = regime_from_pctile(vol_pctile)

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
        if regime is None:
            return hold("regime_unavailable")
        if regime not in ENTRY_REGIMES:
            return hold("regime_veto")
        return Decision("BUY", "thresholds_met", p_edge, regime, confidence, masked)

    # ------------------------------------------------------------------------------ vectorised twins

    def buy_mask(self, p_action: np.ndarray, vol_pctile: np.ndarray) -> np.ndarray:
        """``resolve(...) == BUY`` for many FLAT states at once (the backtest scores millions of minutes).

        Same rules as ``resolve`` and pinned to it by ``tests/test_decision_policy.py``: masked P(BUY) over BUY+HOLD,
        confidence from the raw action probabilities, regime from the state token. It receives neither the edge nor
        the model's regime answer: neither decides an entry here.
        """
        cfg = self.config
        p = np.asarray(p_action, dtype=np.float64)
        total = p[:, 0] + p[:, 2]  # BUY, HOLD
        masked_buy = masked_buy_prob(p)
        with np.errstate(divide="ignore", invalid="ignore"):
            h = -np.where(p > 0, p * np.log(np.where(p > 0, p, 1.0)), 0.0).sum(axis=1)
        confidence = np.clip(1.0 - h / math.log(p.shape[1]), 0.0, 1.0)
        allowed = np.isin(regimes_from_pctile(vol_pctile), np.array(ENTRY_REGIMES))
        return np.asarray((total > 0) & (masked_buy >= cfg.tau_buy) & (confidence >= cfg.c_min) & allowed)

    def sell_mask(self, p_action: np.ndarray) -> np.ndarray:
        """``resolve(...) == SELL`` for many LONG states: masked P(SELL) over SELL+HOLD against ``tau_sell``."""
        p = np.asarray(p_action, dtype=np.float64)
        total = p[:, 1] + p[:, 2]  # SELL, HOLD
        return np.asarray((total > 0) & (masked_sell_prob(p) >= self.config.tau_sell))


def masked_buy_prob(p_action: np.ndarray) -> np.ndarray:
    """P(BUY) over BUY+HOLD for many FLAT states (0 where both are 0): the masked probability ``resolve`` compares."""
    p = np.asarray(p_action, dtype=np.float64)
    total = p[:, 0] + p[:, 2]  # BUY, HOLD
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.asarray(np.where(total > 0, p[:, 0] / total, 0.0))


def masked_sell_prob(p_action: np.ndarray) -> np.ndarray:
    """P(SELL) over SELL+HOLD for many LONG states (0 where both are 0)."""
    p = np.asarray(p_action, dtype=np.float64)
    total = p[:, 1] + p[:, 2]  # SELL, HOLD
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.asarray(np.where(total > 0, p[:, 1] / total, 0.0))
