"""Protocol schemas, frozen questions and DecisionPolicy (spec sez. 2.1.2, 2.2.2, 2.2.3, 2.2.5)."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from pydantic import ValidationError

from modules.decision.policy import Decision, DecisionPolicy, PolicyConfig, PolicyMismatchError, load_policy
from modules.decision.protocol import (
    REGIME_LEGEND,
    ChoiceAnswer,
    DecisionAnswers,
    NoulAnswer,
    ScoreAnswer,
    confidence_from_probs,
    regime_from_probs,
)
from modules.decision.questions import build_questions

POLICY = PolicyConfig(
    model="t@1", tau_buy=0.55, tau_sell=0.50, c_min=0.10, p_edge_min=0.44, allowed_regimes_for_entry=(1, 2, 3),
    label_config_sha256="a" * 64,
)  # fmt: skip


def answers(
    buy: float = 0.71, sell: float = 0.02, hold: float = 0.27, edge: float = 0.52, regime: int = 2
) -> DecisionAnswers:
    reg = [0.02] * 5
    reg[regime] = 0.92
    return DecisionAnswers(
        action=ChoiceAnswer(choice="BUY", probabilities={"BUY": buy, "SELL": sell, "HOLD": hold}),
        edge=NoulAnswer(noul=edge),
        vol_regime=ScoreAnswer(
            score=float(regime),
            legend={str(i): n for i, n in enumerate(REGIME_LEGEND)},
            probabilities=dict(zip(REGIME_LEGEND, reg, strict=True)),
        ),  # fmt: skip
    )


# ------------------------------------------------------------------------------------- protocol


def test_confidence_bounds_and_known_values() -> None:
    assert confidence_from_probs([1 / 3] * 3) == pytest.approx(0.0, abs=1e-12)
    assert confidence_from_probs([1.0, 0.0, 0.0]) == pytest.approx(1.0)
    assert confidence_from_probs({"BUY": 0.71, "SELL": 0.02, "HOLD": 0.27}) == pytest.approx(
        1 + (0.71 * math.log(0.71) + 0.02 * math.log(0.02) + 0.27 * math.log(0.27)) / math.log(3)
    )
    with pytest.raises(ValueError):
        confidence_from_probs([1.0])


def test_the_confidence_of_the_backend_is_overwritten() -> None:
    a = ChoiceAnswer(choice="BUY", probabilities={"BUY": 0.9, "SELL": 0.05, "HOLD": 0.05}, confidence=0.99)
    assert a.with_recomputed_confidence().confidence == pytest.approx(confidence_from_probs(a.probabilities))
    assert a.with_recomputed_confidence().confidence != 0.99


@pytest.mark.parametrize(
    "probs",
    [
        {"BUY": 0.5, "SELL": 0.2, "HOLD": 0.2},  # sums to 0.9
        {"BUY": 1.2, "SELL": -0.1, "HOLD": -0.1},  # out of [0, 1]
        {"BUY": 0.5, "HOLD": 0.5},  # missing option
    ],
)
def test_invalid_choice_distributions_are_rejected(probs: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        ChoiceAnswer(choice="BUY", probabilities=probs)


def test_probabilities_summing_to_one_within_tolerance_are_accepted() -> None:
    ChoiceAnswer(choice="BUY", probabilities={"BUY": 0.7005, "SELL": 0.0, "HOLD": 0.3})
    with pytest.raises(ValidationError):
        ChoiceAnswer(choice="BUY", probabilities={"BUY": 0.702, "SELL": 0.0, "HOLD": 0.3})


def test_regime_is_the_argmax_with_ties_to_the_lower_index() -> None:
    p = dict.fromkeys(REGIME_LEGEND, 0.0)
    p.update(normal=0.4, high=0.4, low=0.2)
    assert regime_from_probs(p) == 2
    p.update(normal=0.1, high=0.7, low=0.2)
    assert regime_from_probs(p) == 3


def test_noul_range() -> None:
    with pytest.raises(ValidationError):
        NoulAnswer(noul=1.2)


# -------------------------------------------------------------------------------------- questions


def test_questions_text_follows_the_label_config() -> None:
    q = build_questions(horizon_min=15 * 1440, k_tp=3.0, k_sl=2.0, atr_tf="6h")
    assert "over the next 15 days" in q.questions["action"].instructions
    assert "take-profit at +3.0 ATR(6h) and stop-loss at -2.0 ATR(6h)" in q.questions["edge"].instructions
    assert "hit first within 15 days" in q.questions["edge"].instructions
    assert "over the next 15 days relative to the last 365 days" in q.questions["vol_regime"].instructions
    assert list(q.questions) == ["action", "edge", "vol_regime"]


def test_questions_reproduce_the_example_of_the_spec() -> None:
    q = build_questions(horizon_min=10 * 1440, k_tp=4.0, k_sl=2.0, atr_tf="1d")
    assert q.questions["edge"].instructions == (
        "If a long were opened now with take-profit at +4.0 ATR(1d) and stop-loss at -2.0 ATR(1d), "
        "would the take-profit be hit first within 10 days?"
    )


def test_question_version_hash_changes_with_any_parameter() -> None:
    base = build_questions(14400, 4.0, 2.0, "1d")
    assert base.sha256 == build_questions(14400, 4.0, 2.0, "1d").sha256
    assert base.version_tag == f"QUESTIONS_V1:{base.sha256[:8]}"
    for other in (build_questions(7200, 4.0, 2.0, "1d"), build_questions(14400, 3.0, 2.0, "1d"),
                  build_questions(14400, 4.0, 2.0, "6h")):  # fmt: skip
        assert other.sha256 != base.sha256


# ---------------------------------------------------------------------------------------- policy


@pytest.mark.parametrize(
    ("side", "kw", "expected", "reason"),
    [
        ("FLAT", {}, "BUY", "thresholds_met"),
        ("FLAT", {"buy": 0.50, "hold": 0.48}, "HOLD", "below_threshold"),  # masked p(BUY) 0.51 < tau_buy
        ("FLAT", {"edge": 0.40}, "HOLD", "edge_veto"),
        ("FLAT", {"regime": 4}, "HOLD", "regime_veto"),
        ("FLAT", {"regime": 0}, "HOLD", "regime_veto"),
        ("FLAT", {"buy": 0.36, "sell": 0.30, "hold": 0.34}, "HOLD", "below_threshold"),
        ("FLAT", {"buy": 0.0, "sell": 1.0, "hold": 0.0}, "HOLD", "no_valid_action_mass"),
        ("FLAT", {"buy": 0.05, "sell": 0.90, "hold": 0.05}, "HOLD", "below_threshold"),  # SELL masked out
        ("LONG", {"buy": 0.02, "sell": 0.71, "hold": 0.27}, "SELL", "thresholds_met"),
        ("LONG", {"buy": 0.80, "sell": 0.08, "hold": 0.12}, "HOLD", "below_threshold"),  # BUY masked out, SELL 0.4
        ("LONG", {"buy": 0.80, "sell": 0.10, "hold": 0.10}, "SELL", "thresholds_met"),  # renormalized 0.5 >= tau_sell
        ("LONG", {"buy": 0.0, "sell": 0.4, "hold": 0.6}, "HOLD", "below_threshold"),
        ("LONG", {"buy": 0.0, "sell": 0.4, "hold": 0.6, "regime": 4, "edge": 0.1}, "HOLD", "below_threshold"),
        ("LONG", {"buy": 0.0, "sell": 0.7, "hold": 0.3, "regime": 4, "edge": 0.1}, "SELL", "thresholds_met"),
    ],
)
def test_policy_table(side: str, kw: dict[str, float], expected: str, reason: str) -> None:
    d = DecisionPolicy(POLICY).resolve(answers(**kw), side)  # type: ignore[arg-type]
    assert (d.action, d.reason) == (expected, reason)


def test_masking_renormalizes_over_the_valid_actions() -> None:
    d = DecisionPolicy(POLICY).resolve(answers(buy=0.60, sell=0.30, hold=0.10), "FLAT")
    assert set(d.masked_probs) == {"BUY", "HOLD"}
    assert d.masked_probs["BUY"] == pytest.approx(0.60 / 0.70)
    assert d.action == "BUY"
    long = DecisionPolicy(POLICY).resolve(answers(buy=0.60, sell=0.30, hold=0.10), "LONG")
    assert set(long.masked_probs) == {"SELL", "HOLD"} and long.masked_probs["SELL"] == pytest.approx(0.75)


def test_buy_is_never_returned_in_a_long_and_sell_never_when_flat() -> None:
    policy = DecisionPolicy(POLICY)
    for buy, sell, hold in [(0.9, 0.05, 0.05), (0.05, 0.9, 0.05), (0.05, 0.05, 0.9), (0.34, 0.33, 0.33)]:
        a = answers(buy=buy, sell=sell, hold=hold)
        assert policy.resolve(a, "FLAT").action != "SELL"
        assert policy.resolve(a, "LONG").action != "BUY"


def test_confidence_is_recomputed_not_read_from_the_backend() -> None:
    a = answers()
    liar = a.model_copy(update={"action": a.action.model_copy(update={"confidence": 0.999})})
    d = DecisionPolicy(POLICY).resolve(liar, "FLAT")
    assert d.confidence == pytest.approx(confidence_from_probs(a.action.probabilities))
    strict = PolicyConfig(**{**POLICY.model_dump(), "c_min": 0.9})
    assert DecisionPolicy(strict).resolve(liar, "FLAT").reason == "below_threshold"


def test_decision_carries_edge_and_regime_for_the_risk_manager() -> None:
    d = DecisionPolicy(POLICY).resolve(answers(edge=0.61, regime=3), "FLAT")
    assert (d.p_edge, d.regime) == (0.61, 3)


def test_decider_unavailable_is_a_hold() -> None:
    d = Decision.unavailable()
    assert d.action == "HOLD" and d.reason == "decider_unavailable"


def test_thresholds_are_inclusive() -> None:
    a = answers(buy=0.55, sell=0.0, hold=0.45, edge=0.44)
    # masked p(BUY) is 0.55 exactly and p_edge equals p_edge_min: both gates are ">="
    assert DecisionPolicy(POLICY).resolve(a, "FLAT").action == "BUY"


# ------------------------------------------------------------------------------------ policy.json


def write_policy(tmp_path: Path, sha: str) -> Path:
    p = tmp_path / "policy.json"
    p.write_text(json.dumps({**POLICY.model_dump(), "label_config_sha256": sha, "tuned_on": {"split": "val"}}))
    return p


def test_policy_json_loads_when_the_label_config_matches(tmp_path: Path) -> None:
    cfg = load_policy(write_policy(tmp_path, "a" * 64), "a" * 64)
    assert cfg.tau_buy == 0.55 and cfg.allowed_regimes_for_entry == (1, 2, 3)


def test_policy_json_of_another_label_config_is_refused(tmp_path: Path) -> None:
    with pytest.raises(PolicyMismatchError):
        load_policy(write_policy(tmp_path, "b" * 64), "a" * 64)


# ---- vectorised twins (pinned to resolve)


ACTIONS3 = ("BUY", "SELL", "HOLD")


def test_masks_equal_resolve_on_random_answers() -> None:
    import numpy as np

    rng = np.random.default_rng(11)
    n = 4000
    raw = rng.dirichlet(np.array([0.4, 0.3, 1.0]), size=n)
    edge = rng.uniform(0.2, 0.7, n)
    reg = rng.dirichlet(np.ones(5) * 0.5, size=n)
    policy = DecisionPolicy(POLICY)
    buy = policy.buy_mask(raw, edge, reg)
    sell = policy.sell_mask(raw)
    assert 0 < buy.sum() < n and 0 < sell.sum() < n
    for i in range(n):
        a = DecisionAnswers(
            action=ChoiceAnswer(choice="HOLD", probabilities=dict(zip(ACTIONS3, map(float, raw[i]), strict=True))),
            edge=NoulAnswer(noul=float(edge[i])),
            vol_regime=ScoreAnswer(score=0.0, legend={str(k): v for k, v in enumerate(REGIME_LEGEND)},
                                   probabilities=dict(zip(REGIME_LEGEND, map(float, reg[i]), strict=True))),
        )  # fmt: skip
        assert (policy.resolve(a, "FLAT").action == "BUY") == bool(buy[i]), i
        assert (policy.resolve(a, "LONG").action == "SELL") == bool(sell[i]), i


def test_masks_handle_degenerate_probability_rows() -> None:
    import numpy as np

    policy = DecisionPolicy(POLICY)
    raw = np.array([[0.0, 1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])
    reg = np.tile([0.0, 0.0, 1.0, 0.0, 0.0], (3, 1))
    assert policy.buy_mask(raw, np.full(3, 0.6), reg).tolist() == [False, True, False]
    assert policy.sell_mask(raw).tolist() == [True, False, False]
