"""LiveExecutionGateway on a fake exchange: the whole order life cycle, with the spot-coverage circuit (C11) in front.

Proves: every order passes ``BalanceGuard.authorize`` and reaches the exchange only as a ticket; a coverage refusal is a
typed result and never touches the exchange; entry -> fill -> STANDALONE bracket -> exit by TP / SL / decision; the
cancel of the residual (T13) and of the bracket (T15) is confirmed BEFORE the next order; dust merges into the next
entry (T14); an ambiguous outcome keeps the reservation and is never resent; the live gateway cannot be built by
accident.
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from decimal import Decimal
from pathlib import Path

import pytest

from modules.config import ExecutionConfig
from modules.execution import gateway as gateway_module
from modules.execution.balance_guard import BalanceGuard, InsufficientCoverage
from modules.execution.exchange import ExchangeAuthError, OrderRejected, OutcomeUnknown
from modules.execution.gateway import (
    EntryResult,
    ExecutionGateway,
    ExitResult,
    LiveExecutionGateway,
    LiveModeNotConfirmed,
)
from modules.execution.ids import client_order_id
from modules.execution.orders import OrderSpec
from modules.execution.rounding import floor_to
from modules.risk.risk_manager import ExitIntent, OrderIntent
from tests.coverage_fixtures import T0
from tests.gateway_fixtures import BOOK, FEE_RATE, LIVE_ENV, PRODUCT, LiveEnv, make_live
from tests.test_risk_manager import plan

D = Decimal
GATEWAY_PY = Path(gateway_module.__file__)
ENTRY = client_order_id("dec-1", "entry")
BRACKET = client_order_id("dec-1", "bracket")
TP, SL = D("108.96"), D("83.57")


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


def intent(decision: str = "dec-1", size: str | None = None) -> OrderIntent:
    i = plan(decision_id=decision, intent_id=f"int-{decision}")
    assert isinstance(i, OrderIntent)  # SOL-EUR: BUY 0.023 @ 92.03 (maker, 1800 s), TP 108.96, SL 83.57
    return i if size is None else i.model_copy(update={"base_size": D(size)})


def exit_intent(bracket: str | None = BRACKET, decision: str = "dec-x", reason: str = "SELL") -> ExitIntent:
    return ExitIntent(
        intent_id="xi-1", decision_id=decision, product_id=PRODUCT, book=BOOK, base_size=D("0.023"),
        cancel_bracket_order_id=bracket, reason=reason, created_at=T0,
    )  # fmt: skip


def cost(price: str, size: str) -> Decimal:
    return D(price) * D(size) * (1 + FEE_RATE)


async def open_position(env: LiveEnv, size: str = "0.023") -> EntryResult:
    env.venue.plan_fills(ENTRY, (60, size, "92.03"))
    result = await env.gateway.submit_entry_with_bracket(intent(size=size))
    assert result.status == "filled" and result.bracket == "placed", result
    return result


def bracket_order(env: LiveEnv) -> str:
    return env.venue.order_of(BRACKET).order_id


# ------------------------------------------------------------------------------- the live gate


@pytest.mark.parametrize(
    ("mode", "env", "ok"),
    [
        ("live", LIVE_ENV, True),
        ("dry", LIVE_ENV, False),  # the confirmation alone is not enough
        ("live", {}, False),  # the config alone is not enough
        ("live", {"CHRONOS_LIVE_CONFIRM": "yes"}, False),
        ("live", {"CHRONOS_LIVE_CONFIRM": "i_accept_real_orders"}, False),
        ("live", {"CHRONOS_LIVE_CONFIRM": ""}, False),
    ],
)
async def test_live_gateway_needs_both_acts_of_the_spec(
    make: Callable[..., Awaitable[LiveEnv]], mode: str, env: dict[str, str], ok: bool
) -> None:
    if ok:
        await make(execution=ExecutionConfig(mode=mode), env=env)  # type: ignore[arg-type]
    else:
        with pytest.raises(LiveModeNotConfirmed):
            await make(execution=ExecutionConfig(mode=mode), env=env)  # type: ignore[arg-type]


async def test_the_process_environment_is_what_counts_by_default(
    make: Callable[..., Awaitable[LiveEnv]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CHRONOS_LIVE_CONFIRM", raising=False)
    with pytest.raises(LiveModeNotConfirmed):
        await make(env=None)  # no variable in the real environment: not instantiable
    monkeypatch.setenv("CHRONOS_LIVE_CONFIRM", "I_ACCEPT_REAL_ORDERS")
    await make(env=None)


async def test_live_gateway_refuses_a_read_only_exchange_a_dry_ledger_and_a_virtual_book(
    make: Callable[..., Awaitable[LiveEnv]],
) -> None:
    env = await make()
    parts = (env.guard, env.venue, env.ledger, env.gateway._registry, env.clock)
    kw = {"book": BOOK, "balances": env.balances, "execution": ExecutionConfig(mode="live"), "env": LIVE_ENV}
    env.venue.read_only = True
    with pytest.raises(LiveModeNotConfirmed, match="read-only"):
        LiveExecutionGateway(*parts, **kw)  # type: ignore[arg-type]
    env.venue.read_only = False
    from modules.execution.balance import BalanceBook
    from modules.ledger.positions import PositionLedger

    dry = await PositionLedger.open(":memory:", "dry")
    try:
        with pytest.raises(LiveModeNotConfirmed, match="live ledger"):
            LiveExecutionGateway(parts[0], parts[1], dry, parts[3], parts[4], **kw)  # type: ignore[arg-type]
    finally:
        await dry.close()
    virtual = BalanceBook(BOOK, "virtual", env.clock, {"EUR": D(100)})
    with pytest.raises(ValueError, match="exchange-sourced"):
        LiveExecutionGateway(*parts, **{**kw, "balances": virtual})  # type: ignore[arg-type]


async def test_a_gateway_cannot_be_built_without_a_real_guard(make: Callable[..., Awaitable[LiveEnv]]) -> None:
    env = await make()
    parts = (env.venue, env.ledger, env.gateway._registry, env.clock)
    kw = {"book": BOOK, "balances": env.balances, "execution": ExecutionConfig(mode="live"), "env": LIVE_ENV}
    with pytest.raises(TypeError):
        LiveExecutionGateway(*parts, **kw)  # type: ignore[call-arg, arg-type]  # no guard at all
    for fake in (None, object(), env.guard.authorize):
        with pytest.raises(TypeError, match="BalanceGuard"):
            LiveExecutionGateway(fake, *parts, **kw)  # type: ignore[arg-type]


def test_the_interface_is_the_one_of_the_spec() -> None:
    assert {"submit_entry_with_bracket", "submit_exit", "emergency_exit", "cancel"} <= set(dir(ExecutionGateway))
    for name in ("submit_entry_with_bracket", "submit_exit", "emergency_exit", "cancel"):
        assert hasattr(LiveExecutionGateway, name)


def test_every_order_leaves_through_one_door_after_authorize() -> None:
    """Structural: ``create_order`` is called only in ``_send``, and ``_send`` authorizes before it; the file never
    names the ticket type nor builds a body."""
    tree = ast.parse(GATEWAY_PY.read_text(encoding="utf-8"))
    callers: dict[str, list[tuple[str, int]]] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            for n in ast.walk(fn):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                    callers.setdefault(fn.name, []).append((n.func.attr, n.lineno))
    sending = {name for name, calls in callers.items() if any(a == "create_order" for a, _ in calls)}
    assert sending == {"_send"}
    send = dict(reversed(callers["_send"]))  # first line of each call name
    assert send["authorize"] < send["create_order"]
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
    }
    assert not names & {"CoverageTicket", "build_order_body", "_reserve"}


# ------------------------------------------------------------------------------ the whole flow


async def test_entry_fill_then_standalone_bracket(make: Callable[..., Awaitable[LiveEnv]]) -> None:
    env = await make()
    spec_log: list[OrderSpec] = []
    original = BalanceGuard.authorize

    def spy(self: BalanceGuard, spec: OrderSpec):  # type: ignore[no-untyped-def]
        spec_log.append(spec)
        return original(self, spec)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(BalanceGuard, "authorize", spy)
        result = await open_position(env)
    assert result.filled_size == D("0.023") and result.avg_price == D("92.03") and not result.dust_merged
    # exactly two orders, both authorized by the guard BEFORE they were sent, in this order
    assert [(s.side, s.kind) for s in spec_log] == [("BUY", "limit"), ("SELL", "bracket")]
    assert env.venue.creates() == spec_log
    entry, bracket = env.venue.creates()
    assert (entry.client_order_id, entry.post_only, entry.limit_price) == (ENTRY, True, D("92.03"))
    assert (bracket.client_order_id, bracket.base_size, bracket.limit_price, bracket.stop_trigger_price) == (
        BRACKET, D("0.023"), TP, SL,
    )  # fmt: skip
    pos = env.ledger.view(BOOK, PRODUCT)
    assert (pos.side, pos.base_qty, pos.bracket_order_id, pos.tp, pos.sl) == ("LONG", D("0.023"), BRACKET, TP, SL)
    # reservations: the entry's is gone (terminal), the bracket holds the base; the quote left is exactly the cost
    b = env.balances
    assert b.reserved("EUR") == 0 and b.reserved("SOL") == D("0.023") and b.free("SOL") == 0
    assert b.free("EUR") == D(100) - cost("92.03", "0.023")
    open_orders = {o.client_order_id: o.status for o in await env.ledger.open_orders(BOOK)}
    assert open_orders == {BRACKET: "OPEN"}
    assert [f.size for f in await env.ledger.fills_for(ENTRY)] == [D("0.023")]


async def test_tp_hit_is_booked_by_sync_order(make: Callable[..., Awaitable[LiveEnv]]) -> None:
    env = await make()
    await open_position(env)
    env.venue.trigger(bracket_order(env), TP)
    info = await env.gateway.sync_order(BRACKET)
    assert info is not None and info.status == "FILLED"
    assert await env.gateway.sync_order(BRACKET) is None  # idempotent: nothing left to sync
    assert env.ledger.view(BOOK, PRODUCT).side == "FLAT" and not await env.ledger.open_orders(BOOK)
    b = env.balances
    assert b.reserved("SOL") == 0 and b.free("SOL") == 0 and b.reserved("EUR") == 0
    assert b.free("EUR") == D(100) - cost("92.03", "0.023") + TP * D("0.023") * (1 - FEE_RATE)


async def test_sl_hit_is_booked_by_sync_order(make: Callable[..., Awaitable[LiveEnv]]) -> None:
    env = await make()
    await open_position(env)
    env.venue.trigger(bracket_order(env), D("83.40"))  # the stop executes below its trigger
    await env.gateway.sync_order(BRACKET)
    assert env.ledger.view(BOOK, PRODUCT).side == "FLAT"
    loss = D(100) - env.balances.free("EUR")
    assert 0 < loss < D("0.023") * (D("92.03") - D("83.40")) + D("0.02") + D("0.0075")
    assert env.balances.total("SOL") == 0


async def test_decision_exit_cancels_the_bracket_then_sells(make: Callable[..., Awaitable[LiveEnv]]) -> None:
    env = await make()
    await open_position(env)
    res = await env.gateway.submit_exit(exit_intent())
    assert (res.status, res.size, res.reason) == ("closed", D("0.023"), "SELL")
    sells = env.venue.creates("SELL")
    assert [(s.kind, s.base_size) for s in sells] == [("bracket", D("0.023")), ("limit", D("0.023"))]
    assert sells[1].limit_price == D("92.03") and not sells[1].post_only  # a marketable limit at the best bid
    pos = env.ledger.view(BOOK, PRODUCT)
    assert pos.side == "FLAT" and pos.bracket_order_id is None
    b = env.balances
    assert (b.reserved("SOL"), b.total("SOL"), b.reserved("EUR")) == (0, 0, 0)
    assert not await env.ledger.open_orders(BOOK)


async def test_exit_falls_back_to_market_when_the_limit_does_not_fill(make: Callable[..., Awaitable[LiveEnv]]) -> None:
    env = await make()
    await open_position(env)
    env.venue.auto_sell = False  # the marketable limit just rests
    original_create = env.venue.create_order

    async def create(ticket):  # type: ignore[no-untyped-def]
        result = await original_create(ticket)
        if ticket.spec.kind == "market":
            env.venue.auto_sell = True
            env.venue._fill(env.venue.orders[result.order_id], ticket.spec.base_size, env.venue.bid)
        return result

    env.venue.create_order = create  # type: ignore[method-assign]
    res = await env.gateway.submit_exit(exit_intent())
    assert res.status == "closed"
    kinds = [s.kind for s in env.venue.creates("SELL")]
    assert kinds == ["bracket", "limit", "market"]
    limit_oid = env.venue.order_of(client_order_id("dec-x", "exit")).order_id
    # the unfilled limit was cancelled and confirmed before the market order was authorized
    cancel_limit = next(i for i, e in enumerate(env.venue.log) if e[0] == "cancel" and e[1] == (limit_oid,))
    assert cancel_limit < env.venue.index("create", kind="market")
    assert env.venue.orders[limit_oid].status == "CANCELLED"
    assert env.balances.reserved("SOL") == 0 and env.ledger.view(BOOK, PRODUCT).side == "FLAT"


async def test_emergency_exit_cancels_the_bracket_and_goes_straight_to_market(
    make: Callable[..., Awaitable[LiveEnv]],
) -> None:
    env = await make()
    await open_position(env)
    res = await env.gateway.emergency_exit(PRODUCT)
    assert (res.status, res.reason) == ("closed", "WATCHDOG")
    assert [s.kind for s in env.venue.creates("SELL")] == ["bracket", "market"]
    assert env.venue.index("cancel") < env.venue.index("create", kind="market")
    assert env.ledger.view(BOOK, PRODUCT).side == "FLAT" and env.balances.reserved("SOL") == 0


async def test_exit_on_a_flat_book_does_nothing(make: Callable[..., Awaitable[LiveEnv]]) -> None:
    env = await make()
    res = await env.gateway.submit_exit(exit_intent(None))
    assert res.status == "nothing_to_do" and env.venue.log == []
    assert (await env.gateway.emergency_exit(PRODUCT)).status == "nothing_to_do"


# ------------------------------------------------------------------- T13: partial fill and timeout


async def test_partial_fill_timeout_cancels_before_bracket(make: Callable[..., Awaitable[LiveEnv]]) -> None:  # T13
    env = await make()
    env.venue.plan_fills(ENTRY, (60, "0.012", "92.03"))  # 0.012 SOL = 1.10 EUR: sellable
    result = await env.gateway.submit_entry_with_bracket(intent())
    assert (result.status, result.bracket, result.filled_size) == ("filled", "placed", D("0.012"))
    v = env.venue
    entry_oid = v.order_of(ENTRY).order_id
    cancel_at = v.index("cancel")
    terminal_at = next(i for i, e in enumerate(v.log) if e[0] == "get_order" and e[1] == entry_oid and e[2] != "OPEN")
    bracket_at = v.index("create", kind="bracket")
    assert cancel_at < terminal_at < bracket_at  # cancel -> confirmed terminal -> only then the bracket
    assert not [e for e in v.log[:terminal_at] if e[0] == "create" and e[1].side == "SELL"]  # no SELL meanwhile
    assert v.log[cancel_at][1] == (entry_oid,)
    bracket = v.creates("SELL")[0]
    assert bracket.base_size == floor_to(D("0.012"), D("0.001")) == D("0.012")  # what was really filled
    assert env.clock() >= T0.replace(microsecond=0) and (env.clock() - T0).total_seconds() >= 1800
    b = env.balances
    assert b.reserved("EUR") == 0 and b.reserved("SOL") == D("0.012")  # the unused quote went back to free
    assert b.free("EUR") == D(100) - cost("92.03", "0.012")
    assert env.ledger.view(BOOK, PRODUCT).base_qty == D("0.012")


async def test_the_first_fill_already_makes_the_position_long(make: Callable[..., Awaitable[LiveEnv]]) -> None:
    env = await make()
    env.venue.plan_fills(ENTRY, (60, "0.010", "92.03"), (600, "0.013", "92.02"))
    seen: list[tuple[str, Decimal]] = []

    def watch() -> None:
        pos = env.ledger.view(BOOK, PRODUCT)
        if pos.is_long and (pos.side, pos.base_qty) not in seen:
            seen.append((pos.side, pos.base_qty))

    env.venue.on_get_order = watch
    result = await env.gateway.submit_entry_with_bracket(intent())
    assert result.bracket == "placed"
    assert seen == [("LONG", D("0.010"))]  # LONG from the first fill, while the order is still open
    pos = env.ledger.view(BOOK, PRODUCT)
    assert pos.base_qty == D("0.023")
    assert pos.avg_entry == (D("92.03") * D("0.010") + D("92.02") * D("0.013")) / D("0.023")


async def test_no_fill_at_all_releases_everything(make: Callable[..., Awaitable[LiveEnv]]) -> None:
    env = await make()
    result = await env.gateway.submit_entry_with_bracket(intent())
    assert (result.status, result.reason) == ("no_fill", "cancelled")
    assert env.venue.creates("SELL") == [] and env.balances.reserved("EUR") == 0
    assert env.balances.free("EUR") == D(100) and env.ledger.view(BOOK, PRODUCT).side == "FLAT"


async def test_unconfirmed_cancel_keeps_the_reservation(make: Callable[..., Awaitable[LiveEnv]]) -> None:
    env = await make(max_cancel_rounds=2)
    env.venue.plan_fills(ENTRY, (60, "0.012", "92.03"))
    env.venue.stuck.add("ex-1")  # the exchange never confirms the cancel
    result = await env.gateway.submit_entry_with_bracket(intent())
    assert result.status == "unconfirmed" and result.filled_size == D("0.012")
    assert env.venue.creates("SELL") == []  # no bracket without a terminal entry
    assert env.balances.reservation(ENTRY) is not None  # nothing released optimistically


# ---------------------------------------------------------------------------------- T14: dust


async def test_dust_merges_into_next_entry(make: Callable[..., Awaitable[LiveEnv]]) -> None:  # T14
    env = await make()
    env.venue.plan_fills(ENTRY, (60, "0.010", "92.03"))  # 0.010 SOL = 0.92 EUR < quote_min_size (1 EUR)
    first = await env.gateway.submit_entry_with_bracket(intent())
    assert (first.status, first.bracket) == ("filled", "dust")
    assert env.venue.creates("SELL") == []  # nothing sellable: no bracket
    pos = env.ledger.view(BOOK, PRODUCT)
    dust_cost = cost("92.03", "0.010")
    assert (pos.side, pos.base_qty, pos.dust_qty, pos.dust_cost) == ("FLAT", 0, D("0.010"), dust_cost)
    assert env.balances.free("SOL") == D("0.010") and env.balances.reserved("EUR") == 0

    env.refresh_snapshot()  # the first entry took 1800 s: the snapshot is old
    second_cid = client_order_id("dec-2", "entry")
    env.venue.plan_fills(second_cid, (60, "0.023", "92.03"))
    second = await env.gateway.submit_entry_with_bracket(intent("dec-2"))
    assert (second.status, second.bracket, second.dust_merged) == ("filled", "placed", True)
    bracket = env.venue.creates("SELL")[0]
    assert bracket.base_size == D("0.033")  # the dust joins the new bracket
    pos = env.ledger.view(BOOK, PRODUCT)
    assert (pos.side, pos.base_qty, pos.dust_qty, pos.dust_cost) == ("LONG", D("0.033"), 0, 0)
    assert pos.avg_entry == (D("92.03") * D("0.023") + dust_cost) / D("0.033")
    assert env.balances.reserved("SOL") == D("0.033")


# --------------------------------------------------------------------- T15: exit and the cancel


async def test_exit_waits_for_cancel_confirmation(make: Callable[..., Awaitable[LiveEnv]]) -> None:  # T15
    env = await make()
    await open_position(env)
    env.venue.cancel_lag = 3  # CANCELLED shows only after three polls following the request
    res = await env.gateway.submit_exit(exit_intent())
    assert res.status == "closed"
    v = env.venue
    boid = bracket_order(env)
    cancel_at = v.index("cancel")
    states = [e[2] for e in v.log[cancel_at:] if e[0] == "get_order" and e[1] == boid]
    assert states[:3] == ["OPEN", "OPEN", "OPEN"] and states[3] == "CANCELLED"
    confirmed_at = next(i for i, e in enumerate(v.log) if i > cancel_at and e[0] == "get_order" and e[2] == "CANCELLED")
    sell_at = v.index("create", kind="limit", side="SELL")
    assert cancel_at < confirmed_at < sell_at
    assert not [e for e in v.log[cancel_at:sell_at] if e[0] == "create"]  # not a single order while unconfirmed


async def test_exit_with_an_unconfirmed_cancel_sends_no_sell(make: Callable[..., Awaitable[LiveEnv]]) -> None:  # T15
    env = await make(max_cancel_rounds=2)
    await open_position(env)
    env.venue.stuck.add(bracket_order(env))
    res = await env.gateway.submit_exit(exit_intent())
    assert res.status == "unconfirmed"
    assert [s.kind for s in env.venue.creates("SELL")] == ["bracket"]  # still only the bracket
    assert env.balances.reserved("SOL") == D("0.023")  # the base stays reserved
    assert env.ledger.view(BOOK, PRODUCT).side == "LONG"
    # and even asking the guard directly cannot sell base that a live bracket holds
    emergency = await env.gateway.emergency_exit(PRODUCT)
    assert emergency.status == "unconfirmed" and len(env.venue.creates("SELL")) == 1


async def test_bracket_filled_while_cancelling_means_no_sell(make: Callable[..., Awaitable[LiveEnv]]) -> None:  # T15
    env = await make()
    await open_position(env)
    env.venue.trigger(bracket_order(env), TP)  # TP executed before the cancel arrives
    res = await env.gateway.submit_exit(exit_intent())
    assert res.status == "closed_by_bracket"
    assert [s.kind for s in env.venue.creates("SELL")] == ["bracket"]  # nothing sold on top of the TP
    assert env.ledger.view(BOOK, PRODUCT).side == "FLAT"
    assert env.balances.total("SOL") == 0 and env.balances.reserved("SOL") == 0
    assert env.balances.free("EUR") == D(100) - cost("92.03", "0.023") + TP * D("0.023") * (1 - FEE_RATE)


# ------------------------------------------------------------------------------ coverage refusals


async def test_insufficient_balance_never_reaches_the_exchange(make: Callable[..., Awaitable[LiveEnv]]) -> None:
    env = await make(eur="1")
    result = await env.gateway.submit_entry_with_bracket(intent())
    assert (result.status, result.reason) == ("refused", "insufficient_balance")
    assert env.venue.log == []  # not a single call
    assert await env.ledger.open_orders(BOOK) == [] and env.balances.open_reservations() == []
    assert env.balances.free("EUR") == D(1)
    assert env.gateway.has_pending_entry(PRODUCT) is False


async def test_stale_snapshot_and_coverage_breach_are_typed_refusals(make: Callable[..., Awaitable[LiveEnv]]) -> None:
    env = await make()
    env.clock.advance(121)  # the balance view is older than 120 s
    stale = await env.gateway.submit_entry_with_bracket(intent())
    assert (stale.status, stale.reason) == ("refused", "balance_stale")
    env.refresh_snapshot()
    env.kill.breached.add(BOOK)
    breach = await env.gateway.submit_entry_with_bracket(intent("dec-2"))
    assert (breach.status, breach.reason) == ("refused", "kill_switch")
    assert env.venue.log == []


async def test_exit_on_a_closed_position_sends_nothing(make: Callable[..., Awaitable[LiveEnv]]) -> None:
    env = await make()
    await open_position(env)
    env.venue.trigger(bracket_order(env), TP)
    await env.gateway.sync_order(BRACKET)
    # the book is FLAT: a stale ledger view cannot make the gateway sell what it does not own
    res = await env.gateway.submit_exit(exit_intent(None))
    assert res.status == "nothing_to_do" and len(env.venue.creates()) == 2


async def test_a_bracket_refused_by_the_guard_leaves_a_long_position_without_bracket(
    make: Callable[..., Awaitable[LiveEnv]],
) -> None:
    env = await make()
    env.venue.plan_fills(ENTRY, (60, "0.023", "92.03"))
    original = env.guard.authorize

    def authorize(spec: OrderSpec):  # type: ignore[no-untyped-def]
        if spec.kind == "bracket":  # as if the base were not sellable (e.g. reserved elsewhere)
            raise InsufficientCoverage("SOL", spec.base_size, D(0))
        return original(spec)

    env.guard.authorize = authorize  # type: ignore[method-assign]
    result = await env.gateway.submit_entry_with_bracket(intent())
    assert (result.status, result.bracket) == ("filled", "refused")
    assert env.ledger.view(BOOK, PRODUCT).side == "LONG" and env.ledger.view(BOOK, PRODUCT).bracket_order_id is None
    assert [s.kind for s in env.venue.creates()] == ["limit"]


# ---------------------------------------------------------------------------- ambiguous outcomes


async def test_outcome_unknown_keeps_the_reservation_and_never_resends(make: Callable[..., Awaitable[LiveEnv]]) -> None:
    env = await make()
    env.venue.errors = [OutcomeUnknown("create_order", ENTRY, "read timeout")]
    result = await env.gateway.submit_entry_with_bracket(intent())
    assert (result.status, result.reason) == ("unknown", "outcome_unknown")
    assert len(env.venue.creates()) == 1
    assert env.balances.reservation(ENTRY) is not None and env.balances.reserved("EUR") > 0  # the money stays set aside
    assert {o.client_order_id: o.status for o in await env.ledger.open_orders(BOOK)} == {ENTRY: "UNKNOWN"}
    # asking again for the same decision cannot create a second order
    again = await env.gateway.submit_entry_with_bracket(intent())
    assert (again.status, again.reason) == ("blocked", "order_already_known")
    assert len(env.venue.creates()) == 1
    assert env.gateway.has_pending_entry(PRODUCT) is False


async def test_cancelled_while_sending_is_treated_as_unknown(make: Callable[..., Awaitable[LiveEnv]]) -> None:
    env = await make()
    env.venue.errors = [asyncio.CancelledError()]
    with pytest.raises(asyncio.CancelledError):
        await env.gateway.submit_entry_with_bracket(intent())
    assert env.balances.reservation(ENTRY) is not None  # the request may be in flight: reservation stays
    assert {o.client_order_id: o.status for o in await env.ledger.open_orders(BOOK)} == {ENTRY: "UNKNOWN"}
    assert env.gateway.has_pending_entry(PRODUCT) is False


async def test_unknown_bracket_blocks_every_sell_until_reconciled(make: Callable[..., Awaitable[LiveEnv]]) -> None:
    env = await make()
    env.venue.plan_fills(ENTRY, (60, "0.023", "92.03"))
    env.venue.errors = [None, OutcomeUnknown("create_order", BRACKET, "502")]
    result = await env.gateway.submit_entry_with_bracket(intent())
    assert (result.status, result.bracket) == ("filled", "unknown")
    assert env.balances.reservation(BRACKET) is not None
    assert env.ledger.view(BOOK, PRODUCT).side == "LONG"
    sent = len(env.venue.creates())
    res = await env.gateway.submit_exit(exit_intent())
    assert res.status == "unknown"  # the exit cannot cancel an order it cannot identify
    emergency = await env.gateway.emergency_exit(PRODUCT)
    assert emergency.status == "refused"  # the guard: the base is reserved by the unknown bracket
    assert len(env.venue.creates()) == sent  # no SELL was ever sent on top of the unknown bracket


async def test_a_rejected_entry_releases_the_reservation(make: Callable[..., Awaitable[LiveEnv]]) -> None:
    env = await make()
    env.venue.errors = [OrderRejected("INVALID_LIMIT_PRICE_POST_ONLY", "would cross")]
    result = await env.gateway.submit_entry_with_bracket(intent())
    assert (result.status, result.reason) == ("rejected", "INVALID_LIMIT_PRICE_POST_ONLY")
    assert env.balances.reservation(ENTRY) is None and env.balances.free("EUR") == D(100)
    assert await env.ledger.open_orders(BOOK) == []  # REJECTED is terminal


async def test_a_rejected_bracket_leaves_a_long_position_without_bracket(
    make: Callable[..., Awaitable[LiveEnv]],
) -> None:
    env = await make()
    env.venue.plan_fills(ENTRY, (60, "0.023", "92.03"))
    env.venue.errors = [None, OrderRejected("INVALID_SIZE", "no")]
    result = await env.gateway.submit_entry_with_bracket(intent())
    assert (result.status, result.bracket) == ("filled", "rejected")
    pos = env.ledger.view(BOOK, PRODUCT)
    assert pos.side == "LONG" and pos.bracket_order_id is None
    assert env.balances.reservation(BRACKET) is None and env.balances.free("SOL") == D("0.023")


async def test_an_auth_failure_is_fatal_but_releases_the_unsent_reservation(
    make: Callable[..., Awaitable[LiveEnv]],
) -> None:
    env = await make()
    env.venue.errors = [ExchangeAuthError("401")]
    with pytest.raises(ExchangeAuthError):
        await env.gateway.submit_entry_with_bracket(intent())
    assert env.balances.reservation(ENTRY) is None and env.balances.free("EUR") == D(100)


# ------------------------------------------------------------------------------ pending and cancel


async def test_one_pending_entry_per_product_and_cancel(make: Callable[..., Awaitable[LiveEnv]]) -> None:
    env = await make()
    first = asyncio.create_task(env.gateway.submit_entry_with_bracket(intent()))
    await asyncio.sleep(0)
    assert env.gateway.has_pending_entry(PRODUCT)
    second = await env.gateway.submit_entry_with_bracket(intent("dec-2"))
    assert (second.status, second.reason) == ("blocked", "entry_already_pending")
    outcome = await env.gateway.cancel(PRODUCT)
    assert outcome.status == "done" and outcome.entry is not None and outcome.entry.status == "no_fill"
    assert (await first).status == "no_fill"
    assert (await env.gateway.cancel(PRODUCT)).status == "nothing_pending"
    assert env.balances.reserved("EUR") == 0
    assert len(env.venue.creates()) == 1  # the blocked second intent sent nothing


async def test_cancel_with_a_partial_fill_follows_the_normal_path(make: Callable[..., Awaitable[LiveEnv]]) -> None:
    env = await make()
    env.venue.plan_fills(ENTRY, (20, "0.012", "92.03"))
    cancels: list[asyncio.Task[object]] = []

    def trigger() -> None:
        if not cancels and (env.clock() - T0).total_seconds() >= 40:
            cancels.append(asyncio.get_running_loop().create_task(env.gateway.cancel(PRODUCT)))

    env.venue.on_get_order = trigger
    result = await env.gateway.submit_entry_with_bracket(intent())
    assert cancels and (await cancels[0]).status == "done"  # type: ignore[attr-defined]
    assert (result.status, result.bracket, result.filled_size) == ("filled", "placed", D("0.012"))
    assert (env.clock() - T0).total_seconds() < 300  # cancelled early, not at the 1800 s timeout


async def test_emergency_exit_during_a_partial_entry_cancels_it_and_skips_the_bracket(
    make: Callable[..., Awaitable[LiveEnv]],
) -> None:
    env = await make()
    env.venue.plan_fills(ENTRY, (20, "0.012", "92.03"))
    tasks: list[asyncio.Task[ExitResult]] = []

    def trigger() -> None:
        if not tasks and (env.clock() - T0).total_seconds() >= 40:
            tasks.append(asyncio.get_running_loop().create_task(env.gateway.emergency_exit(PRODUCT)))

    env.venue.on_get_order = trigger
    entry = await env.gateway.submit_entry_with_bracket(intent())
    assert (entry.status, entry.bracket) == ("filled", "skipped")  # no bracket for a position being dumped
    res = await tasks[0]
    assert res.status == "closed" and res.size == D("0.012")
    assert [s.kind for s in env.venue.creates()] == ["limit", "market"]  # BUY, then the market SELL: no bracket ever
    assert env.ledger.view(BOOK, PRODUCT).side == "FLAT" and env.balances.total("SOL") == 0


async def test_intent_of_another_book_is_a_programming_error(make: Callable[..., Awaitable[LiveEnv]]) -> None:
    env = await make()
    with pytest.raises(ValueError, match="book"):
        await env.gateway.submit_entry_with_bracket(intent().model_copy(update={"book": "other"}))
    with pytest.raises(ValueError, match="book"):
        await env.gateway.submit_exit(exit_intent().model_copy(update={"book": "other"}))
    assert env.venue.log == []


async def test_the_exit_reason_is_passed_through_untouched(make: Callable[..., Awaitable[LiveEnv]]) -> None:
    """MANUAL exits (sez. 6.4) will go through the same call: the reason is just a label for the gateway."""
    env = await make()
    await open_position(env)
    res = await env.gateway.submit_exit(exit_intent(reason="MANUAL"))
    assert (res.status, res.reason) == ("closed", "MANUAL")
