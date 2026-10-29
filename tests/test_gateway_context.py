"""The gateway's duties towards the user stream and the reconciler: the entry context (the plan of an entry, so that
the first fill seen by the stream already carries tp / sl), the bracket the reconciler asks for at startup (every order
through the guard), and fills written together with their position."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from decimal import Decimal

import pytest

from modules.execution.balance_guard import BalanceGuard, InsufficientCoverage
from modules.execution.exchange import OrderRejected, OutcomeUnknown
from modules.execution.gateway import (
    BracketNotPlaced,
    EntryContexts,
    LedgerBooking,
    LiveExecutionGateway,
    context_from_payload,
)
from modules.execution.ids import client_order_id
from modules.execution.orders import OrderSpec
from modules.execution.reconciler import BracketRequest
from modules.execution.user_stream import EntryContext, OrderEventSink
from modules.ledger.positions import EntryPlan
from tests.gateway_fixtures import BOOK, LIVE_ENV, PRODUCT, LiveEnv, make_live
from tests.test_gateway import BRACKET, ENTRY, SL, TP, cost, intent, open_position

D = Decimal
PLAN = EntryPlan(tp=TP, sl=SL, k_tp=4.0, k_sl=2.0, atr=D("4.2334"))


@pytest.fixture
async def make() -> AsyncIterator[Callable[..., Awaitable[LiveEnv]]]:
    envs: list[LiveEnv] = []

    async def factory(**kwargs: object) -> LiveEnv:
        env = await make_live(**kwargs)  # type: ignore[arg-type]
        envs.append(env)
        return env

    yield factory
    for env in envs:
        await env.ledger.close()


def sink_for(env: LiveEnv, *, with_contexts: bool = True) -> OrderEventSink:
    return OrderEventSink(
        book=BOOK, ledger=env.ledger, balances=env.balances, registry=env.gateway._registry,  # noqa: SLF001
        fills=env.venue, contexts=env.gateway if with_contexts else None,
    )  # type: ignore[arg-type]  # fmt: skip


def request(size: str = "0.023") -> BracketRequest:
    return BracketRequest(BOOK, PRODUCT, D(size), TP, SL)


async def long_without_bracket(env: LiveEnv) -> None:
    """A filled entry whose bracket was rejected: LONG, with tp / sl from the plan, no bracket."""
    env.venue.plan_fills(ENTRY, (60, "0.023", "92.03"))
    env.venue.errors = [None, OrderRejected("INVALID_SIZE", "no")]
    result = await env.gateway.submit_entry_with_bracket(intent())
    assert (result.status, result.bracket) == ("filled", "rejected")


# ---------------------------------------------------------------------------------- entry context


async def test_the_entry_plan_is_known_before_the_order_reaches_the_exchange(
    make: Callable[..., Awaitable[LiveEnv]],
) -> None:
    env = await make()
    seen: list[EntryContext | None] = []
    env.venue.on_create = lambda: seen.append(env.gateway.entry_context(ENTRY))
    env.venue.plan_fills(ENTRY, (60, "0.023", "92.03"))
    await env.gateway.submit_entry_with_bracket(intent())
    assert seen[0] == EntryContext("dec-1", PLAN)
    assert env.gateway.entry_context("unknown-order") is None


async def test_the_entry_plan_is_persisted_in_the_order_record(make: Callable[..., Awaitable[LiveEnv]]) -> None:
    env = await make()
    env.venue.plan_fills(ENTRY, (60, "0.023", "92.03"))
    await env.gateway.submit_entry_with_bracket(intent())
    rec = await env.ledger.get_order(ENTRY)
    assert rec is not None and rec.payload is not None
    data = json.loads(rec.payload)
    assert data["client_order_id"] == ENTRY and data["post_only"] is True  # the spec is still there
    assert data["entry_plan"] == {
        "decision_id": "dec-1", "tp": "108.96", "sl": "83.57", "k_tp": 4.0, "k_sl": 2.0, "atr": "4.2334"
    }  # fmt: skip
    assert context_from_payload(rec.payload) == EntryContext("dec-1", PLAN)
    bracket = await env.ledger.get_order(BRACKET)
    assert bracket is not None and context_from_payload(bracket.payload) is None  # only entries carry a plan


@pytest.mark.parametrize("payload", [None, "", "not json", "[]", '{"entry_plan": 3}', '{"entry_plan": {"tp": "1"}}'])
def test_an_unreadable_payload_gives_no_context(payload: str | None) -> None:
    assert context_from_payload(payload) is None


async def test_a_gateway_after_a_restart_gets_the_plans_of_the_open_entries_back(
    make: Callable[..., Awaitable[LiveEnv]],
) -> None:
    env = await make()
    env.venue.errors = [OutcomeUnknown("create_order", ENTRY, "timeout")]
    await env.gateway.submit_entry_with_bracket(intent())  # the entry stays open (UNKNOWN) in the ledger
    registry, cfg = env.gateway._registry, env.gateway._cfg  # noqa: SLF001
    reborn = LiveExecutionGateway(
        env.guard, env.venue, env.ledger, registry, env.clock,  # type: ignore[arg-type]
        book=BOOK, balances=env.balances, execution=cfg, env=LIVE_ENV,
    )  # fmt: skip
    assert reborn.entry_context(ENTRY) is None  # a new process knows nothing yet
    assert await reborn.restore_entry_contexts() == 1
    assert reborn.entry_context(ENTRY) == EntryContext("dec-1", PLAN)


async def test_only_open_buy_entries_are_restored(make: Callable[..., Awaitable[LiveEnv]]) -> None:
    env = await make()
    await open_position(env)  # the entry is FILLED (terminal), the open order is the SELL bracket
    assert await env.gateway.restore_entry_contexts() == 0
    contexts = EntryContexts()
    assert await contexts.restore(env.ledger, BOOK) == 0


async def test_the_first_fill_seen_by_the_stream_already_carries_tp_and_sl(
    make: Callable[..., Awaitable[LiveEnv]],
) -> None:
    """The stream applies the fill before the gateway polls: with the gateway as ``EntryContextSource`` the position
    opens with the levels; without it the position would have none (the case the watchdog cannot protect)."""
    env = await make()
    env.venue.create_then_fail = True  # the order exists, but the answer is lost: nobody has polled it
    env.venue.errors = [OutcomeUnknown("create_order", ENTRY, "timeout")]
    env.venue.plan_fills(ENTRY, (0, "0.023", "92.03"))
    assert (await env.gateway.submit_entry_with_bracket(intent())).status == "unknown"
    order = env.venue.orders["ex-1"]
    env.venue._advance(order)  # noqa: SLF001  (the fill happens on the exchange)
    result = await sink_for(env).sync_order(env.venue._info(order))  # noqa: SLF001  (the WS delivers it)
    assert result.new_fills == 1
    pos = env.ledger.view(BOOK, PRODUCT)
    assert (pos.side, pos.base_qty, pos.tp, pos.sl, pos.k_tp, pos.k_sl) == ("LONG", D("0.023"), TP, SL, 4.0, 2.0)
    assert pos.atr_at_entry == D("4.2334") and pos.entry_decision_id == "dec-1"


async def test_without_the_gateway_the_stream_opens_a_position_with_no_levels(
    make: Callable[..., Awaitable[LiveEnv]],
) -> None:
    env = await make()
    env.venue.create_then_fail = True
    env.venue.errors = [OutcomeUnknown("create_order", ENTRY, "timeout")]
    env.venue.plan_fills(ENTRY, (0, "0.023", "92.03"))
    await env.gateway.submit_entry_with_bracket(intent())
    order = env.venue.orders["ex-1"]
    env.venue._advance(order)  # noqa: SLF001
    await sink_for(env, with_contexts=False).sync_order(env.venue._info(order))  # noqa: SLF001
    assert env.ledger.view(BOOK, PRODUCT).tp is None  # the control: this is the bug the context fixes


async def test_a_fill_seen_by_both_the_stream_and_the_gateway_counts_once(
    make: Callable[..., Awaitable[LiveEnv]],
) -> None:
    env = await make()
    await open_position(env)
    entry_order = env.venue.order_of(ENTRY)
    again = await sink_for(env).sync_order(env.venue._info(entry_order))  # noqa: SLF001  (the WS, late)
    assert again.new_fills == 0
    assert env.ledger.view(BOOK, PRODUCT).base_qty == D("0.023")
    assert env.balances.free("EUR") == D(100) - cost("92.03", "0.023")


# --------------------------------------------------------------------------- place_missing_bracket


async def test_the_missing_bracket_goes_through_the_guard_and_is_linked(
    make: Callable[..., Awaitable[LiveEnv]],
) -> None:
    env = await make()
    await long_without_bracket(env)
    seen: list[OrderSpec] = []
    original = BalanceGuard.authorize

    def spy(self: BalanceGuard, spec: OrderSpec):  # type: ignore[no-untyped-def]
        seen.append(spec)
        return original(self, spec)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(BalanceGuard, "authorize", spy)
        await env.gateway.place_missing_bracket(request())
    [spec] = seen
    assert (spec.side, spec.kind, spec.base_size, spec.limit_price, spec.stop_trigger_price) == (
        "SELL", "bracket", D("0.023"), TP, SL
    )  # fmt: skip
    assert env.venue.creates() == [env.venue.creates()[0], env.venue.creates()[1], spec]  # entry, rejected, new
    new_cid = client_order_id("dec-1", "bracket-r1")  # the rejected first bracket id is not reused
    pos = env.ledger.view(BOOK, PRODUCT)
    assert spec.client_order_id == new_cid != BRACKET and pos.bracket_order_id == new_cid
    assert env.balances.reservation(new_cid) is not None and env.balances.reserved("SOL") == D("0.023")
    assert [o.client_order_id for o in await env.ledger.open_orders(BOOK)] == [new_cid]


async def test_the_missing_bracket_is_sized_on_what_is_sellable(make: Callable[..., Awaitable[LiveEnv]]) -> None:
    env = await make()
    await long_without_bracket(env)
    await env.gateway.place_missing_bracket(request("0.500"))  # asks for more than is held
    assert env.venue.creates("SELL")[-1].base_size == D("0.023")


async def test_a_refusal_of_the_guard_places_nothing(make: Callable[..., Awaitable[LiveEnv]]) -> None:
    env = await make()
    await long_without_bracket(env)
    sent = len(env.venue.creates())
    original = env.guard.authorize

    def refuse(spec: OrderSpec):  # type: ignore[no-untyped-def]
        raise InsufficientCoverage("SOL", spec.base_size, D(0))

    env.guard.authorize = refuse  # type: ignore[method-assign]
    with pytest.raises(BracketNotPlaced) as err:
        await env.gateway.place_missing_bracket(request())
    assert err.value.status == "refused" and err.value.reason == "insufficient_balance"
    assert len(env.venue.creates()) == sent and env.ledger.view(BOOK, PRODUCT).bracket_order_id is None
    env.guard.authorize = original  # type: ignore[method-assign]


async def test_nothing_sellable_places_nothing(make: Callable[..., Awaitable[LiveEnv]]) -> None:
    env = await make()
    await long_without_bracket(env)
    env.balances._free["SOL"] = D(0)  # noqa: SLF001  (the exchange's account shows no free base)
    sent = len(env.venue.creates())
    with pytest.raises(BracketNotPlaced, match="insufficient_balance"):
        await env.gateway.place_missing_bracket(request())
    assert len(env.venue.creates()) == sent


async def test_an_ambiguous_bracket_keeps_its_reservation(make: Callable[..., Awaitable[LiveEnv]]) -> None:
    env = await make()
    await long_without_bracket(env)
    env.venue.errors = [OutcomeUnknown("create_order", "x", "502")]
    with pytest.raises(BracketNotPlaced) as err:
        await env.gateway.place_missing_bracket(request())
    assert err.value.status == "unknown"
    assert env.balances.reserved("SOL") == D("0.023") and env.ledger.view(BOOK, PRODUCT).bracket_order_id is None
    sent = len(env.venue.creates())
    with pytest.raises(BracketNotPlaced):  # asking again cannot stack a second bracket on the unknown one
        await env.gateway.place_missing_bracket(request())
    assert len(env.venue.creates()) == sent


async def test_a_bracket_is_not_placed_twice_nor_on_a_flat_book_nor_during_an_entry(
    make: Callable[..., Awaitable[LiveEnv]],
) -> None:
    env = await make()
    await env.gateway.place_missing_bracket(request())  # FLAT: nothing to protect
    assert env.venue.log == []
    await open_position(env)  # LONG and already protected
    sent = len(env.venue.creates())
    await env.gateway.place_missing_bracket(request())
    assert len(env.venue.creates()) == sent
    with pytest.raises(ValueError, match="book"):
        await env.gateway.place_missing_bracket(BracketRequest("other", PRODUCT, D("0.023"), TP, SL))


async def test_no_bracket_is_placed_while_an_entry_is_pending(make: Callable[..., Awaitable[LiveEnv]]) -> None:
    env = await make()
    task = asyncio.create_task(env.gateway.submit_entry_with_bracket(intent()))
    await asyncio.sleep(0)
    assert env.gateway.has_pending_entry(PRODUCT)
    await env.gateway.place_missing_bracket(request())  # the entry owns the position's life cycle
    assert [s.kind for s in env.venue.creates()] in ([], ["limit"])
    await env.gateway.cancel(PRODUCT)
    await task
    assert env.venue.creates("SELL") == []


# ---------------------------------------------------------------------------- one transaction


async def test_booking_a_fill_cannot_leave_the_fill_without_the_position(
    make: Callable[..., Awaitable[LiveEnv]], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = await make()
    env.venue.plan_fills(ENTRY, (60, "0.023", "92.03"))

    async def broken(_view: object) -> None:
        raise RuntimeError("crash")

    monkeypatch.setattr(env.ledger, "_write_position", broken)
    with pytest.raises(RuntimeError):
        await env.gateway.submit_entry_with_bracket(intent())
    assert await env.ledger.fills_for(ENTRY) == []  # the next sync will find it missing and apply it again
    assert env.ledger.view(BOOK, PRODUCT).side == "FLAT"


async def test_a_sell_larger_than_the_position_raises_an_alarm_and_books_what_is_held(
    make: Callable[..., Awaitable[LiveEnv]],
) -> None:
    env = await make()
    await open_position(env)
    alarms: list[str] = []
    booking = LedgerBooking(BOOK, env.ledger, env.clock, lambda a: alarms.append(a.kind))
    from modules.execution.gateway import FillEvent

    big = FillEvent("t-big", "c-x", PRODUCT, "SELL", D("100"), D("0.030"), D("0.01"), "TAKER", env.clock())
    assert await booking.book_fill(big, lambda _f: [], decision_id="", plan=None) is True
    assert alarms == ["reservation_overdraw"]
    assert env.ledger.view(BOOK, PRODUCT).side == "FLAT"  # never negative, never short


async def test_an_entry_that_never_opened_leaves_no_context_behind(make: Callable[..., Awaitable[LiveEnv]]) -> None:
    refused = await make(eur="1")
    await refused.gateway.submit_entry_with_bracket(intent())
    assert refused.gateway.entry_context(ENTRY) is None
    rejected = await make()
    rejected.venue.errors = [OrderRejected("INVALID_LIMIT_PRICE_POST_ONLY", "would cross")]
    await rejected.gateway.submit_entry_with_bracket(intent())
    assert rejected.gateway.entry_context(ENTRY) is None
    unknown = await make()
    unknown.venue.errors = [OutcomeUnknown("create_order", ENTRY, "timeout")]
    await unknown.gateway.submit_entry_with_bracket(intent())
    assert unknown.gateway.entry_context(ENTRY) is not None  # it may exist on the exchange: the stream may need it
