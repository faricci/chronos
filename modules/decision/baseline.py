"""BaselineDecider: three LightGBM models behind the System One contract (spec sez. 2.1.1, 2.3 C.3; decision O3).

The CPU backend of the local phase. It answers the three questions of the protocol from the flat numeric view of the
state (``flatten_state``), with calibrated probabilities:

- ``action``: multiclass over ``ACTIONS`` (BUY, SELL, HOLD) on all ``FLAT_COLUMNS``. The HOLD class is subsampled in
  training, so the raw score of HOLD is corrected by ``ln(keep_rate)`` before the temperature;
- ``edge``: binary, Platt scaling ``sigmoid(a * z + b)`` on the raw score ``z``; features without the ``pos_*`` block;
- ``vol_regime``: multiclass over ``REGIME_LEGEND``, temperature scaling; features without the ``pos_*`` block. A fold
  whose training data is too small has no regime model: the answer is the constant "normal" (marked ``degenerate``).

Training and serving share this file: ``Calibration`` is applied here by both ``training/baseline_lgbm.py`` and
``BaselineDecider``, so the numbers that were validated are the numbers that are served. The confidence of the
answers is left at 0.0 on purpose: the client/policy recompute it (sez. 2.1.2).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import lightgbm as lgb
import numpy as np
from pydantic import BaseModel, ConfigDict

from modules.decision.protocol import (
    ACTIONS,
    REGIME_LEGEND,
    ChoiceQuestion,
    DeciderBadResponse,
    DecisionAnswers,
    NoulQuestion,
    ScoreQuestion,
)
from modules.decision.questions import QuestionSet
from modules.features.schemas import DecisionState
from modules.features.serializer import FLAT_COLUMNS, flatten_state, state_sha256
from modules.risk.label_config import LoadedLabelConfig

__all__ = [
    "ACTION_FEATURES",
    "AnswerArrays",
    "BaselineDecider",
    "Calibration",
    "MARKET_FEATURES",
    "flat_columns_sha256",
    "softmax",
]

ACTION_FEATURES: Final[tuple[str, ...]] = tuple(FLAT_COLUMNS)
MARKET_FEATURES: Final[tuple[str, ...]] = tuple(c for c in FLAT_COLUMNS if not c.startswith("pos_"))
_MARKET_IDX: Final = np.array([FLAT_COLUMNS.index(c) for c in MARKET_FEATURES], dtype=np.int64)
HOLD: Final = ACTIONS.index("HOLD")
NORMAL_REGIME: Final = REGIME_LEGEND.index("normal")


def flat_columns_sha256() -> str:
    return state_sha256(",".join(FLAT_COLUMNS))


def softmax(z: np.ndarray) -> np.ndarray:
    z = z - z.max(axis=1, keepdims=True)
    e = np.exp(z)
    return np.asarray(e / e.sum(axis=1, keepdims=True))


class PlattParams(BaseModel):
    model_config = ConfigDict(frozen=True)

    a: float = 1.0
    b: float = 0.0


class Calibration(BaseModel):
    """``calibration.json``: everything applied to the raw LightGBM scores (decision O3.4)."""

    model_config = ConfigDict(frozen=True, extra="allow")

    platt_edge: PlattParams = PlattParams()
    T_action: float = 1.0
    T_vol_regime: float = 1.0
    action_hold_log_prior: float = 0.0

    def action_probs(self, raw: np.ndarray) -> np.ndarray:
        z = np.array(raw, dtype=np.float64, copy=True)
        z[:, HOLD] += self.action_hold_log_prior  # undo the HOLD subsampling of the training set
        return softmax(z / self.T_action)

    def edge_probs(self, raw: np.ndarray) -> np.ndarray:
        z = self.platt_edge.a * np.asarray(raw, dtype=np.float64) + self.platt_edge.b
        return np.asarray(1.0 / (1.0 + np.exp(-z)))

    def regime_probs(self, raw: np.ndarray) -> np.ndarray:
        return softmax(np.asarray(raw, dtype=np.float64) / self.T_vol_regime)


@dataclass(frozen=True)
class AnswerArrays:
    p_action: np.ndarray  # (n, 3) in ACTIONS order
    p_edge: np.ndarray  # (n,)
    p_regime: np.ndarray  # (n, 5) in REGIME_LEGEND order


class BaselineDecider:
    def __init__(
        self,
        action: lgb.Booster,
        edge: lgb.Booster,
        regime: lgb.Booster | None,
        calibration: Calibration,
        questions: QuestionSet,
        degenerate_regime: bool,
    ) -> None:
        self._action, self._edge, self._regime = action, edge, regime
        self.calibration = calibration
        self._questions = questions
        self.degenerate_regime = degenerate_regime
        self.model_key = ""  # sha256 of the model files, set by ``load``: keys the backtest caches

    @classmethod
    def load(
        cls,
        model_dir: Path,
        *,
        label: LoadedLabelConfig,
        questions: QuestionSet,
        subset: str = "final",
        allow_degenerate: bool = False,
    ) -> BaselineDecider:
        """``model_dir``: version directory (``models/baseline/<version>``); ``subset``: ``final`` or ``fold_<k>``.

        Refuses (ValueError) a model built for another label_config, other questions or another state layout, and a
        model without regime head unless ``allow_degenerate`` (only the walk-forward folds may be degenerate).
        """
        card = json.loads((model_dir / "model_card.json").read_text(encoding="utf-8"))
        if card["label_config"]["sha256"] != label.sha256:
            raise ValueError("model was trained for another label_config")
        if card["questions_version"] != questions.version_tag:
            raise ValueError("model was trained with other questions")
        if card["flat_columns_sha256"] != flat_columns_sha256():
            raise ValueError("model was trained with another state layout")
        degenerate = bool(card["degenerate"].get(subset, {}).get("vol_regime", False))
        if degenerate and not allow_degenerate:
            raise ValueError(
                f"{subset} has no regime head (degenerate); pass allow_degenerate=True for walk-forward folds"
            )
        sub = model_dir / subset
        calibration = Calibration.model_validate(json.loads((sub / "calibration.json").read_text(encoding="utf-8")))
        regime = None if degenerate else lgb.Booster(model_file=str(sub / "vol_regime.txt"))
        decider = cls(
            lgb.Booster(model_file=str(sub / "action.txt")),
            lgb.Booster(model_file=str(sub / "edge.txt")),
            regime,
            calibration,
            questions,
            degenerate,
        )
        digest = hashlib.sha256()
        for name in ("action.txt", "edge.txt", "vol_regime.txt", "calibration.json"):
            if (sub / name).exists():
                digest.update((sub / name).read_bytes())
        decider.model_key = digest.hexdigest()
        return decider

    # ------------------------------------------------------------------------------------ batch

    def answers_batch(self, x: np.ndarray) -> AnswerArrays:
        """``x``: rows in ``FLAT_COLUMNS`` order (NaN = missing)."""
        x = np.ascontiguousarray(x, dtype=np.float64)
        if x.ndim != 2 or x.shape[1] != len(FLAT_COLUMNS):
            raise ValueError(f"expected {len(FLAT_COLUMNS)} columns in FLAT_COLUMNS order, got shape {x.shape}")
        raw_action = np.asarray(self._action.predict(x, raw_score=True)).reshape(len(x), len(ACTIONS))
        xm = x[:, _MARKET_IDX]
        raw_edge = np.asarray(self._edge.predict(xm, raw_score=True)).reshape(len(x))
        if self._regime is None:
            p_regime = np.zeros((len(x), len(REGIME_LEGEND)))
            p_regime[:, NORMAL_REGIME] = 1.0
        else:
            raw_reg = np.asarray(self._regime.predict(xm, raw_score=True)).reshape(len(x), len(REGIME_LEGEND))
            p_regime = self.calibration.regime_probs(raw_reg)
        return AnswerArrays(self.calibration.action_probs(raw_action), self.calibration.edge_probs(raw_edge), p_regime)

    # ------------------------------------------------------------------------------ protocol

    async def decide(
        self, state: str, questions: Mapping[str, ChoiceQuestion | NoulQuestion | ScoreQuestion]
    ) -> DecisionAnswers:
        """Same signature as ``SystemOneClient.decide``'s inputs; no await inside: one row is microseconds of work."""
        if dict(questions) != dict(self._questions.questions):
            raise DeciderBadResponse("the baseline answers only the questions it was trained for")
        flat = flatten_state(DecisionState.model_validate_json(state))
        a = self.answers_batch(np.array([[flat[c] for c in FLAT_COLUMNS]]))
        return self._answers(a, 0)

    @staticmethod
    def _answers(a: AnswerArrays, i: int) -> DecisionAnswers:
        pa = [float(v) for v in a.p_action[i]]
        pr = [float(v) for v in a.p_regime[i]]
        probs: dict[str, Any] = dict(zip(ACTIONS, pa, strict=True))
        choice = ACTIONS[int(np.argmax(pa))]
        return DecisionAnswers.model_validate(
            {
                "action": {"choice": choice, "probabilities": probs, "confidence": 0.0},
                "edge": {"noul": float(min(max(a.p_edge[i], 0.0), 1.0))},
                "vol_regime": {
                    "score": float(sum(k * p for k, p in enumerate(pr))),
                    "legend": {str(k): name for k, name in enumerate(REGIME_LEGEND)},
                    "probabilities": dict(zip(REGIME_LEGEND, pr, strict=True)),
                    "confidence": 0.0,
                },
            }
        )
