"""The engine side of a model swap (spec 5.1.6): the model parts change together, between two bars, or not at all; the
SELL of a model does not apply to a position opened under another label_config; an entry is stamped with the model
that decided it."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from modules.decision.engine import KEEP_BACKEND, LABEL_CONFIG_MISMATCH
from modules.decision.questions import build_questions
from modules.ledger.positions import EntryStamp
from modules.risk.costs import FallbackFees
from modules.risk.risk_manager import ExitIntent, OrderIntent, RiskManager
from tests.label_fixtures import loaded_label
from tests.test_backtest import LABEL, StubDecider
from tests.test_decision_engine import (
    BOOK,
    DAY,
    QUESTIONS,
    StubBaseline,
    World,
    long_view,
    make_world,
    policy_with,
)

OTHER_LABEL = loaded_label({"BTC-EUR": 3.0, "SOL-EUR": 3.0}, horizon_min=2 * DAY, k_tp=2.0)


def other_parts(world: World, *, policy: Any = None, label: Any = OTHER_LABEL) -> dict[str, Any]:
    questions = build_questions(label.cfg.horizon_min, label.cfg.k_tp, label.cfg.k_sl, label.cfg.atr_tf)
    chosen = policy or policy_with(model="stub-2", label_config_sha256=label.sha256)
    from modules.config import EntryTimeout, RiskConfig

    risk = RiskManager(BOOK, RiskConfig(), label.cfg, FallbackFees(), EntryTimeout(), None, label.costs)
    return {"version": "stub-2", "label": label, "policy": chosen, "questions": questions, "risk": risk}


# ================================================================================ the label_config rule


async def test_the_sell_of_a_model_does_not_apply_to_a_position_of_another_label_config() -> None:
    w = make_world(inner=StubDecider(sell_after_h=2))
    i = w.cursor["BTC-EUR"]
    w.positions.views["BTC-EUR"] = dataclasses.replace(long_view("BTC-EUR", i), label_sha256="sha-of-another-label")
    p = await w.next("BTC-EUR")
    assert p.decision is not None and p.decision.action == "HOLD" and p.decision.reason == LABEL_CONFIG_MISMATCH
    assert p.intent is None and w.gateway.exits == []
    assert p.record["policy"]["final"] == "HOLD" and p.record["policy"]["reason"] == LABEL_CONFIG_MISMATCH
    assert p.record["risk"]["outcome"] == "none"  # nothing was planned: no exit intent exists to be refused later


async def test_the_sell_applies_when_the_label_config_is_the_same_or_unrecorded() -> None:
    for sha in (LABEL.sha256, None):
        w = make_world(inner=StubDecider(sell_after_h=2))
        i = w.cursor["BTC-EUR"]
        w.positions.views["BTC-EUR"] = dataclasses.replace(long_view("BTC-EUR", i), label_sha256=sha)
        p = await w.next("BTC-EUR")
        assert p.decision is not None and p.decision.action == "SELL" and isinstance(p.intent, ExitIntent), sha
        assert len(w.gateway.exits) == 1


async def test_a_mismatched_position_still_lets_the_model_buy_other_products() -> None:
    w = make_world(inner=StubDecider(sell_after_h=2))
    w.positions.views["BTC-EUR"] = dataclasses.replace(
        long_view("BTC-EUR", w.cursor["BTC-EUR"]), label_sha256="sha-of-another-label"
    )
    held = await w.next("BTC-EUR")
    free = await w.next("SOL-EUR")
    assert held.decision is not None and held.decision.reason == LABEL_CONFIG_MISMATCH
    assert free.decision is not None and free.decision.action == "BUY"  # the rule is about the position, not the book


# ================================================================================================ the stamp


async def test_an_entry_is_stamped_with_the_model_that_decided_it_before_the_order_goes() -> None:
    w = make_world()
    seen: list[tuple[str, EntryStamp, int]] = []
    w.engine._stamp_entry = lambda decision_id, stamp: seen.append((decision_id, stamp, len(w.gateway.entries)))  # noqa: SLF001
    p = await w.next("BTC-EUR")
    assert isinstance(p.intent, OrderIntent)
    ((decision_id, stamp, orders_before),) = seen
    assert decision_id == p.decision_id and stamp == EntryStamp("stub", LABEL.sha256, LABEL.cfg.horizon_min)
    assert orders_before == 0  # the stamp is announced BEFORE the gateway sees the intent


async def test_an_exit_is_not_stamped_and_a_failing_stamp_never_stops_the_entry() -> None:
    sell = make_world(inner=StubDecider(sell_after_h=2))
    seen: list[Any] = []
    sell.engine._stamp_entry = lambda decision_id, stamp: seen.append(stamp)  # noqa: SLF001
    sell.positions.views["BTC-EUR"] = long_view("BTC-EUR", sell.cursor["BTC-EUR"])
    await sell.next("BTC-EUR")
    assert seen == [] and len(sell.gateway.exits) == 1

    def boom(decision_id: str, stamp: EntryStamp) -> None:
        raise OSError("ledger")

    w = make_world()
    w.engine._stamp_entry = boom  # noqa: SLF001
    p = await w.next("BTC-EUR")
    assert isinstance(p.intent, OrderIntent) and len(w.gateway.entries) == 1  # the position falls back to the book's


# ================================================================================================ the swap


async def test_a_swap_changes_every_model_part_together_between_two_bars() -> None:
    w = make_world()
    first = await w.next("BTC-EUR")
    assert first.record["policy"]["policy_version"] == "stub"
    assert first.record["questions_version"] == QUESTIONS.version_tag
    await w.engine.drain()
    swap = w.engine.build_swap(**other_parts(w))
    assert w.engine.model_version == "stub"  # building installs nothing
    w.engine.apply_swap(swap)
    assert w.engine.model_version == "stub-2" and w.engine.label is OTHER_LABEL
    assert w.engine._risk is swap.risk  # noqa: SLF001  (the entry is planned with the barriers of the NEW label)
    second = await w.next("BTC-EUR")
    assert second.record["policy"]["policy_version"] == "stub-2"
    assert second.record["questions_version"] == swap.questions.version_tag != QUESTIONS.version_tag
    assert second.record["risk"]["outcome"] in ("intent", "skip")
    intents = w.gateway.entries
    assert intents and all(i.book == BOOK for i in intents)


async def test_the_new_policy_decides_from_the_next_bar_and_the_new_risk_manager_sizes_it() -> None:
    w = make_world()
    assert (await w.next("BTC-EUR")).decision is not None
    strict = policy_with(model="strict", tau_buy=0.99, label_config_sha256=LABEL.sha256)
    parts = other_parts(w, policy=strict, label=LABEL)
    w.engine.apply_swap(w.engine.build_swap(**parts))
    p = await w.next("BTC-EUR")
    assert p.decision is not None and p.decision.action == "HOLD" and p.decision.reason == "below_threshold"
    assert p.record["policy"]["policy_version"] == "strict" and p.record["policy"]["tau_buy"] == 0.99


async def test_a_swap_that_cannot_be_built_changes_nothing() -> None:
    w = make_world()
    parts = other_parts(w, policy=policy_with(model="bad", label_config_sha256="0" * 64))
    with pytest.raises(ValueError, match="another label_config"):
        w.engine.build_swap(**parts)
    assert w.engine.model_version == "stub" and w.engine.label is LABEL
    p = await w.next("BTC-EUR")
    assert p.record["policy"]["policy_version"] == "stub"


async def test_the_backend_is_kept_unless_a_new_one_is_given() -> None:
    w = make_world()
    keep = w.engine.build_swap(**other_parts(w))
    assert keep.baseline is w.baseline and keep.remote is None
    new = StubBaseline(StubDecider(buy=0.01))
    swapped = w.engine.build_swap(**other_parts(w), baseline=new)
    assert swapped.baseline is new
    w.engine.apply_swap(swapped)
    p = await w.next("BTC-EUR")  # a model that never wants to buy
    assert p.decision is not None and p.decision.action == "HOLD"
    assert KEEP_BACKEND is not None


async def test_a_gateway_call_started_before_the_swap_keeps_its_intent() -> None:
    import asyncio

    w = make_world()
    w.gateway.hold = asyncio.Event()
    first = await w.next("BTC-EUR")
    assert isinstance(first.intent, OrderIntent) and w.engine.pending("BTC-EUR") == "entry"
    w.engine.apply_swap(w.engine.build_swap(**other_parts(w)))
    assert w.gateway.entries == [first.intent]  # what the old model decided is what the venue holds
    w.gateway.hold.set()
    await w.engine.drain()
    assert w.engine.pending("BTC-EUR") is None
