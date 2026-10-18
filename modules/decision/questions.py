"""The three questions of the System One protocol, frozen and versioned (spec sez. 2.2.2).

The text must be byte-identical in the training dataset and at serving time: changing a question is a new model
version. The numbers in the text (horizon, barriers) come from the ``label_config`` of the model, resolved once when the
set is built, never per call.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Final

from modules.decision.protocol import REGIME_LEGEND, ChoiceQuestion, NoulQuestion, ScoreQuestion

__all__ = ["QUESTIONS_VERSION", "QuestionSet", "build_questions"]

QUESTIONS_VERSION: Final = "QUESTIONS_V1"


@dataclass(frozen=True)
class QuestionSet:
    questions: dict[str, ChoiceQuestion | NoulQuestion | ScoreQuestion]
    sha256: str

    @property
    def version_tag(self) -> str:
        """``QUESTIONS_V1:<8 hex>``: written in every DecisionRecord and in the model card."""
        return f"{QUESTIONS_VERSION}:{self.sha256[:8]}"


def build_questions(horizon_min: int, k_tp: float, k_sl: float, atr_tf: str) -> QuestionSet:
    days = horizon_min / 1440
    d = f"{days:g}"
    action = ChoiceQuestion(
        instructions=(
            "Spot long-only crypto account. Given the state, which action maximizes expected net return after fees "
            f"over the next {d} days?"
        ),
        criteria={
            "BUY": "open a long position now (only valid when position.side is FLAT)",
            "SELL": "close the existing long now (only valid when position.side is LONG)",
            "HOLD": "do nothing this minute",
        },
    )
    edge = NoulQuestion(
        instructions=(
            f"If a long were opened now with take-profit at +{k_tp:.1f} ATR({atr_tf}) and stop-loss at "
            f"-{k_sl:.1f} ATR({atr_tf}), would the take-profit be hit first within {d} days?"
        )
    )
    regime = ScoreQuestion(
        instructions=f"What will realized volatility be over the next {d} days relative to the last 365 days?",
        criteria=list(REGIME_LEGEND),
    )
    questions: dict[str, ChoiceQuestion | NoulQuestion | ScoreQuestion] = {
        "action": action,
        "edge": edge,
        "vol_regime": regime,
    }
    canonical = json.dumps({k: v.model_dump() for k, v in questions.items()}, separators=(",", ":"), sort_keys=False)
    return QuestionSet(questions, hashlib.sha256(canonical.encode("utf-8")).hexdigest())
