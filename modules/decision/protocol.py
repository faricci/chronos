"""System One protocol: request/response schemas shared by the client, the baseline and the decider server
(spec sez. 2.2.2, 2.2.3, 3.5 punto 12).

Probabilities travel already calibrated (temperature / Platt of the backend's model card). The confidence is NEVER taken
from the backend: it is recomputed here as ``1 - H(p) / ln K`` so that every backend is comparable (sez. 2.1.2).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, model_validator

__all__ = [
    "ACTIONS",
    "REGIME_LEGEND",
    "ChoiceAnswer",
    "ChoiceQuestion",
    "DecisionAnswers",
    "DeciderBadResponse",
    "DeciderError",
    "DeciderTimeout",
    "DeciderUnavailable",
    "NoulAnswer",
    "NoulQuestion",
    "ScoreAnswer",
    "ScoreQuestion",
    "SystemOneRequest",
    "SystemOneResponse",
    "confidence_from_probs",
    "regime_from_probs",
]

ACTIONS: Final = ("BUY", "SELL", "HOLD")
REGIME_LEGEND: Final = ("very_low", "low", "normal", "high", "extreme")
PROB_TOL: Final = 1e-3


class DeciderError(Exception):
    """Base of the typed decider failures; the engine turns any of them into HOLD / baseline fallback."""


class DeciderTimeout(DeciderError):
    pass


class DeciderUnavailable(DeciderError):
    pass


class DeciderBadResponse(DeciderError):
    pass


def confidence_from_probs(probs: Mapping[str, float] | list[float]) -> float:
    """``1 - H(p)/ln K`` in [0, 1] (0 = uniform, 1 = one-hot). ``K`` is the number of options."""
    values = list(probs.values()) if isinstance(probs, Mapping) else list(probs)
    k = len(values)
    if k < 2:
        raise ValueError("confidence needs at least two options")
    h = -sum(p * math.log(p) for p in values if p > 0.0)
    return max(0.0, min(1.0, 1.0 - h / math.log(k)))


def regime_from_probs(probs: Mapping[str, float]) -> int:
    """Volatility regime = argmax of the probabilities, in legend order (ties go to the lower index; sez. 2.1.2)."""
    ordered = [probs[name] for name in REGIME_LEGEND]
    return max(range(len(ordered)), key=lambda i: (ordered[i], -i))


# ---------------------------------------------------------------------------------------- questions


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ChoiceQuestion(_Frozen):
    type: Literal["choice"] = "choice"
    instructions: str
    criteria: dict[str, str]


class NoulQuestion(_Frozen):
    type: Literal["noul"] = "noul"
    instructions: str


class ScoreQuestion(_Frozen):
    type: Literal["score"] = "score"
    instructions: str
    criteria: list[str]


class SystemOneRequest(_Frozen):
    model: str
    state: str
    questions: dict[str, ChoiceQuestion | NoulQuestion | ScoreQuestion]


# ----------------------------------------------------------------------------------------- answers


def _check_distribution(probs: Mapping[str, float], keys: tuple[str, ...], what: str) -> None:
    if set(probs) != set(keys):
        raise ValueError(f"{what}: expected options {sorted(keys)}, got {sorted(probs)}")
    if any(not 0.0 <= p <= 1.0 for p in probs.values()):
        raise ValueError(f"{what}: probabilities must be in [0, 1]")
    if abs(sum(probs.values()) - 1.0) > PROB_TOL:
        raise ValueError(f"{what}: probabilities sum to {sum(probs.values()):.6f}, not 1")


class ChoiceAnswer(_Frozen):
    choice: str
    probabilities: dict[str, float]
    confidence: float = 0.0  # overwritten by the client; never trusted from the backend

    @model_validator(mode="after")
    def _valid(self) -> ChoiceAnswer:
        _check_distribution(self.probabilities, ACTIONS, "action")
        if self.choice not in ACTIONS:
            raise ValueError(f"action: unknown choice {self.choice!r}")
        return self

    def with_recomputed_confidence(self) -> ChoiceAnswer:
        return self.model_copy(update={"confidence": confidence_from_probs(self.probabilities)})


class NoulAnswer(_Frozen):
    noul: float

    @model_validator(mode="after")
    def _valid(self) -> NoulAnswer:
        if not 0.0 <= self.noul <= 1.0:
            raise ValueError("edge: noul must be in [0, 1]")
        return self


class ScoreAnswer(_Frozen):
    score: float
    legend: dict[str, str]
    probabilities: dict[str, float]
    confidence: float = 0.0

    @model_validator(mode="after")
    def _valid(self) -> ScoreAnswer:
        _check_distribution(self.probabilities, REGIME_LEGEND, "vol_regime")
        if not 0.0 <= self.score <= len(REGIME_LEGEND) - 1:
            raise ValueError("vol_regime: score out of range")
        return self

    def with_recomputed_confidence(self) -> ScoreAnswer:
        return self.model_copy(update={"confidence": confidence_from_probs(self.probabilities)})


class DecisionAnswers(_Frozen):
    action: ChoiceAnswer
    edge: NoulAnswer
    vol_regime: ScoreAnswer

    def with_recomputed_confidence(self) -> DecisionAnswers:
        return DecisionAnswers(
            action=self.action.with_recomputed_confidence(),
            edge=self.edge,
            vol_regime=self.vol_regime.with_recomputed_confidence(),
        )


class Usage(_Frozen):
    input_tokens: int = 0
    output_tokens: int = 0


class SystemOneResponse(_Frozen):
    model: str
    answers: DecisionAnswers
    usage: Usage = Usage()
