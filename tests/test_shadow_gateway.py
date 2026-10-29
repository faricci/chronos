"""ShadowExecutionGateway: the dry-run twin of the live gateway, driven by market bars and a simulated clock.

Proves: the same coverage circuit as live (every simulated order is authorized by ``BalanceGuard.authorize`` and a
refusal is the same typed result); the bar-level fill rules (latency, queue haircut, post-only, timeout, TP / SL /
same-bar-is-SL, fill-bar rule); the order life cycle (partial fills, dust, bracket, exit by decision, emergency,
cancel); and that it can never write anywhere but the dry ledger and the virtual account.
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from modules.config import FallbackFees
from modules.dryrun import shadow_gateway as shadow_module
from modules.dryrun.fill_model import BarFillModel
from modules.dryrun.shadow_gateway import BarEvent, ShadowExecutionGateway
from modules.dryrun.virtual_account import VirtualAccount
from modules.execution.balance_guard import BalanceGuard
from modules.execution.gateway import EntryResult, ExecutionGateway, ExitResult, context_from_payload, entry_spec
from modules.execution.ids import client_order_id
from modules.execution.orders import OrderSpec
from modules.execution.rounding import floor_to
from modules.execution.spot import SpotRegistry
from modules.ledger.positions import LedgerModeMismatch, PositionLedger
from modules.risk.label_config import LabelConfig
from modules.risk.risk_manager import ExitIntent, MarketQuote, OrderIntent
from tests.coverage_fixtures import SOL, T0, Clock, KillSwitch
from tests.gateway_fixtures import BOOK, PRODUCT, FeeSourceStub, make_live
from tests.test_gateway import intent as live_intent
from tests.test_risk_manager import manager, plan

D = Decimal
SHADOW_PY = Path(shadow_module.__file__)
ENTRY = client_order_id("dec-1", "entry")
BRACKET = client_order_id("dec-1", "bracket")
TP, SL = D("108.96"), D("83.57")
MAKER_BPS, TAKER_BPS = D("0.006"), D("0.012")  # the assumed (fallback) tier: 60 / 120 bps


@dataclass
class ShadowEnv:
    gateway: ShadowExecutionGateway
    account: VirtualAccount
    ledger: PositionLedger
    guard: BalanceGuard
    clock: Clock
    kill: KillSwitch
    registry: SpotRegistry
    fees: FeeSourceStub

    async def bar(self, minute: float, **kw: float) -> list[ExitResult]:
        return await self.gateway.on_bar(bar(minute, **kw))


def bar(
    minute: float,
    o: float = 92.1,
    h: float = 92.2,
    lo: float = 92.0,
    c: float = 92.1,
    v: float = 1000.0,
    spread: float = 2.0,
) -> BarEvent:
    return BarEvent(PRODUCT, T0 + timedelta(minutes=minute), o, h, lo, c, v, spread)


async def build(eur: str = "100") -> ShadowEnv:
    clock = Clock()
    ledger = await PositionLedger.open(":memory:", "dry")
    account = VirtualAccount(BOOK, D(eur), clock)
    registry = SpotRegistry({PRODUCT: SOL})
    kill = KillSwitch()
    fees = FeeSourceStub(clock)
    guard = BalanceGuard(account.balances, ledger.book_view(BOOK), registry, fees, FallbackFees(), kill)
    gateway = ShadowExecutionGateway(BOOK, account, BarFillModel(), ledger, clock, guard, registry, fees)
    return ShadowEnv(gateway, account, ledger, guard, clock, kill, registry, fees)


@pytest.fixture
async def make() -> AsyncIterator[Callable[..., Awaitable[ShadowEnv]]]:
    envs: list[ShadowEnv] = []

    async def factory(**kwargs: str) -> ShadowEnv:
        env = await build(**kwargs)
        envs.append(env)
        return env

    yield factory
    for env in envs:
        await env.ledger.close()


def intent(decision: str = "dec-1", size: str | None = None) -> OrderIntent:
    i = plan(decision_id=decision, intent_id=f"int-{decision}")
    assert isinstance(i, OrderIntent)
    return i if size is None else i.model_copy(update={"base_size": D(size)})


def taker_intent() -> OrderIntent:
    rm = manager(label=LabelConfig(atr_tf="1d", k_tp=4.0, k_sl=2.0, entry_mode="taker", horizon_min=14400))
    i = plan(rm, quote=MarketQuote(D("92.03"), D("92.064")))
    assert isinstance(i, OrderIntent) and i.entry.type == "limit_marketable"
    return i


def exit_intent(reason: str = "SELL") -> ExitIntent:
    return ExitIntent(
        intent_id="xi-1", decision_id="dec-x", product_id=PRODUCT, book=BOOK, base_size=D("0.023"),
        cancel_bracket_order_id=BRACKET, reason=reason, created_at=T0,
    )  # fmt: skip


async def start_entry(env: ShadowEnv, i: OrderIntent) -> asyncio.Task[EntryResult]:
    """Run the entry as a task and wait until it is armed (authorized, recorded) or already over."""
    task = asyncio.create_task(env.gateway.submit_entry_with_bracket(i))
    while not env.gateway.has_pending_entry(PRODUCT) and not task.done():
        await asyncio.sleep(0.002)
    return task


async def start_exit(env: ShadowEnv, coro: Awaitable[ExitResult]) -> asyncio.Task[ExitResult]:
    task = asyncio.ensure_future(coro)
    while not env.gateway.has_pending_exit(PRODUCT) and not task.done():
        await asyncio.sleep(0.002)
    return task


async def open_position(env: ShadowEnv, **bar_kw: float) -> EntryResult:
    task = await start_entry(env, intent())
    await env.bar(1, **bar_kw)
    result = await task
    assert result.status == "filled" and result.bracket == "placed", result
    return result


def fee(price: str | Decimal, size: str | Decimal, rate: Decimal) -> Decimal:
    return D(price) * D(size) * rate


# ------------------------------------------------------------------------------------------ the gate


def test_the_interface_is_the_one_of_the_spec() -> None:
    for name in ("submit_entry_with_bracket", "submit_exit", "emergency_exit", "cancel"):
        assert hasattr(ShadowExecutionGateway, name) and hasattr(ExecutionGateway, name)


async def test_a_shadow_gateway_cannot_be_built_without_a_real_guard(make: Callable[..., Awaitable[ShadowEnv]]) -> None:
    env = await make()
    rest = (BOOK, env.account, BarFillModel(), env.ledger, env.clock)
    with pytest.raises(TypeError):
        ShadowExecutionGateway(*rest, registry=env.registry, fees=env.fees)  # type: ignore[call-arg]
    for fake in (None, object(), env.guard.authorize):
        with pytest.raises(TypeError, match="BalanceGuard"):
            ShadowExecutionGateway(*rest, fake, env.registry, env.fees)  # type: ignore[arg-type]


async def test_the_shadow_gateway_only_writes_to_the_dry_ledger(make: Callable[..., Awaitable[ShadowEnv]]) -> None:
    env = await make()
    live_ledger = await PositionLedger.open(":memory:", "live")
    try:
        with pytest.raises(LedgerModeMismatch):
            ShadowExecutionGateway(
                BOOK, env.account, BarFillModel(), live_ledger, env.clock, env.guard, env.registry, env.fees
            )
    finally:
        await live_ledger.close()
    other = VirtualAccount("random_entry", D(100), env.clock)
    with pytest.raises(ValueError, match="own book"):
        ShadowExecutionGateway(BOOK, other, BarFillModel(), env.ledger, env.clock, env.guard, env.registry, env.fees)


def test_shadow_gateway_never_writes_to_an_exchange() -> None:
    """Structural: the module has no exchange, no SDK and never names an order endpoint."""
    tree = ast.parse(SHADOW_PY.read_text(encoding="utf-8"))
    imported = {
        (n.module or "") if isinstance(n, ast.ImportFrom) else a.name
        for n in ast.walk(tree)
        for a in (n.names if isinstance(n, ast.Import | ast.ImportFrom) else [])
    } | {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not {m for m in imported if m.startswith("coinbase") or m.endswith("exchange")}
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
    }
    assert not names & {
        "create_order",
        "cancel_orders",
        "preview_order",
        "CoinbaseExchange",
        "RESTClient",
        "build_order_body",
    }
    assert "authorize" in names and "consume" in names  # the circuit is what "sends" the simulated orders


# ------------------------------------------------------------------------------- entry and bracket


async def test_maker_entry_fills_then_a_standalone_bracket_exists(make: Callable[..., Awaitable[ShadowEnv]]) -> None:
    env = await make()
    task = await start_entry(env, intent())
    assert not task.done() and env.account.balances.reserved("EUR") > 0  # authorized and reserved at once
    assert await env.bar(0, lo=80.0) == []  # opens before the order reaches the market (300 ms): ignored
    assert not task.done() and env.ledger.view(BOOK, PRODUCT).side == "FLAT"
    assert await env.bar(1) == []  # low 92.0 < 92.03: filled; the take profit does not exist in the fill bar
    result = await task
    assert (result.status, result.bracket, result.filled_size, result.avg_price) == (
        "filled", "placed", D("0.023"), D("92.03")
    )  # fmt: skip
    assert result.bracket_client_order_id == BRACKET
    pos = env.ledger.view(BOOK, PRODUCT)
    assert (pos.side, pos.base_qty, pos.bracket_order_id, pos.tp, pos.sl) == ("LONG", D("0.023"), BRACKET, TP, SL)
    b = env.account.balances
    entry_fee = fee("92.03", "0.023", MAKER_BPS)
    assert b.reserved("EUR") == 0 and b.reserved("SOL") == D("0.023") and b.free("SOL") == 0
    assert b.free("EUR") == D(100) - D("92.03") * D("0.023") - entry_fee
    assert env.account.fees_paid == entry_fee
    assert {o.client_order_id: o.status for o in await env.ledger.open_orders(BOOK)} == {BRACKET: "OPEN"}
    assert [f.trade_id for f in await env.ledger.fills_for(ENTRY)] == [f"sim-{ENTRY}-1"]  # simulated: sim- prefix


async def test_the_queue_haircut_makes_a_maker_fill_partial(make: Callable[..., Awaitable[ShadowEnv]]) -> None:
    env = await make()
    task = await start_entry(env, intent())
    await env.bar(1, v=0.04)  # 25% of 0.04 = 0.010 per bar
    assert not task.done()
    pos = env.ledger.view(BOOK, PRODUCT)
    assert (pos.side, pos.base_qty, pos.bracket_order_id) == ("LONG", D("0.010"), None)  # LONG from the first fill
    await env.bar(2, v=0.04)
    assert env.ledger.view(BOOK, PRODUCT).base_qty == D("0.020") and not task.done()
    await env.bar(3, v=0.04)
    result = await task
    assert (result.status, result.bracket, result.filled_size) == ("filled", "placed", D("0.023"))
    assert env.account.balances.reserved("SOL") == D("0.023")


async def test_maker_entry_expires_at_the_timeout_and_releases_the_quote(
    make: Callable[..., Awaitable[ShadowEnv]],
) -> None:
    env = await make()
    task = await start_entry(env, intent())
    await env.bar(1, o=92.6, h=92.7, lo=92.5)  # never below the limit
    assert not task.done()
    await env.bar(31, o=92.6, h=92.7, lo=80.0)  # opens after the 1800 s deadline: too late even if it trades through
    result = await task
    assert (result.status, result.reason) == ("no_fill", "expired")
    assert env.account.balances.free("EUR") == D(100) and env.account.balances.open_reservations() == []
    assert await env.ledger.open_orders(BOOK) == [] and env.ledger.view(BOOK, PRODUCT).side == "FLAT"


async def test_a_post_only_limit_that_would_cross_is_rejected(make: Callable[..., Awaitable[ShadowEnv]]) -> None:
    env = await make()
    task = await start_entry(env, intent())
    await env.bar(1, o=90.0, h=90.1, lo=89.9, c=90.0)  # the market is now below our buy limit
    result = await task
    assert (result.status, result.reason) == ("rejected", "post_only_would_cross")
    assert env.account.balances.free("EUR") == D(100) and env.account.balances.open_reservations() == []


async def test_taker_entry_pays_the_spread_and_the_taker_fee(make: Callable[..., Awaitable[ShadowEnv]]) -> None:
    env = await make()
    i = taker_intent()
    task = await start_entry(env, i)
    await env.bar(1, o=92.0)  # 92.0 x (1 + (1 + 5) bps) = 92.0552 -> 92.06, within the 92.07 limit
    result = await task
    assert (result.status, result.avg_price, result.bracket) == ("filled", D("92.06"), "placed")
    assert env.account.fees_paid == fee("92.06", i.base_size, TAKER_BPS)


async def test_taker_entry_beyond_the_limit_expires(make: Callable[..., Awaitable[ShadowEnv]]) -> None:
    env = await make()
    task = await start_entry(env, taker_intent())
    await env.bar(1, o=92.5)  # the market ran away from the 92.07 limit
    assert (await task).status == "no_fill"
    assert env.account.balances.free("EUR") == D(100)


# --------------------------------------------------------------------------------- TP, SL, same bar


async def test_take_profit_fills_at_the_target_with_the_maker_fee(make: Callable[..., Awaitable[ShadowEnv]]) -> None:
    env = await make()
    await open_position(env)
    assert await env.bar(2, h=108.9) == []  # high must be strictly above the target
    [closed] = await env.bar(3, h=109.0)
    assert (closed.status, closed.reason, closed.size, closed.avg_price) == ("closed", "TP", D("0.023"), TP)
    assert closed.fees == fee(TP, "0.023", MAKER_BPS)
    assert env.ledger.view(BOOK, PRODUCT).side == "FLAT" and not await env.ledger.open_orders(BOOK)
    a = env.account
    assert a.balances.reserved("SOL") == 0 and a.balances.total("SOL") == 0 and a.balances.reserved("EUR") == 0
    assert a.realized_pnl == (TP - D("92.03")) * D("0.023")
    assert a.balances.total("EUR") == D(100) + a.realized_pnl - a.fees_paid  # the accounting identity


async def test_stop_loss_executes_below_the_stop_with_the_taker_fee(make: Callable[..., Awaitable[ShadowEnv]]) -> None:
    env = await make()
    await open_position(env)
    [closed] = await env.bar(2, o=90.0, lo=83.0)
    expected = floor_to(D(repr(BarFillModel().sl_exit(83.57, 90.0, 2.0))), D("0.01"))
    assert (closed.reason, closed.avg_price) == ("SL", expected) and expected < SL
    assert closed.fees == fee(expected, "0.023", TAKER_BPS)
    assert env.ledger.view(BOOK, PRODUCT).side == "FLAT" and env.account.balances.total("SOL") == 0


async def test_same_event_tp_and_sl_is_sl(make: Callable[..., Awaitable[ShadowEnv]]) -> None:
    env = await make()
    await open_position(env)
    [closed] = await env.bar(2, h=110.0, lo=83.0)
    assert closed.reason == "SL"


async def test_in_the_fill_bar_only_the_stop_acts(make: Callable[..., Awaitable[ShadowEnv]]) -> None:
    env = await make()
    task = await start_entry(env, intent())
    closed = await env.bar(1, h=110.0, lo=83.0)  # fills the entry AND trades through both barriers
    assert [c.reason for c in closed] == ["SL"]  # the take profit did not exist yet
    assert (await task).bracket == "placed" and env.ledger.view(BOOK, PRODUCT).side == "FLAT"


async def test_a_stop_in_a_later_bar_than_the_fill_does_not_need_a_pending_exit(
    make: Callable[..., Awaitable[ShadowEnv]],
) -> None:
    env = await make()
    await open_position(env)
    assert await env.bar(2, lo=84.0) == []  # above the stop: nothing
    assert env.ledger.view(BOOK, PRODUCT).side == "LONG"


# ----------------------------------------------------------------------------------------- dust


async def test_dust_merges_into_next_entry(make: Callable[..., Awaitable[ShadowEnv]]) -> None:
    env = await make()
    task = await start_entry(env, intent())
    await env.bar(1, v=0.04)  # 0.010 SOL = 0.92 EUR: below the 1 EUR minimum
    await env.bar(31, o=92.6, h=92.7, lo=92.5)  # timeout: the rest is cancelled
    first = await task
    assert (first.status, first.bracket) == ("filled", "dust")
    pos = env.ledger.view(BOOK, PRODUCT)
    dust_cost = D("92.03") * D("0.010") + fee("92.03", "0.010", MAKER_BPS)
    assert (pos.side, pos.dust_qty, pos.dust_cost) == ("FLAT", D("0.010"), dust_cost)
    assert env.account.balances.free("SOL") == D("0.010")

    env.clock.advance(31 * 60)  # the clock follows the market
    second_task = await start_entry(env, intent("dec-2"))
    await env.bar(32, o=92.6, h=92.7, lo=92.0)
    second = await second_task
    assert (second.bracket, second.dust_merged) == ("placed", True)
    pos = env.ledger.view(BOOK, PRODUCT)
    assert (pos.side, pos.base_qty, pos.dust_qty) == ("LONG", D("0.033"), 0)
    assert env.account.balances.reserved("SOL") == D("0.033")  # the bracket covers the dust too


# --------------------------------------------------------------------------------- exits and cancel


async def test_exit_by_decision_cancels_the_bracket_then_sells_at_a_later_bar(
    make: Callable[..., Awaitable[ShadowEnv]],
) -> None:
    env = await make()
    await open_position(env)
    env.clock.advance(600)
    task = await start_exit(env, env.gateway.submit_exit(exit_intent()))
    b = env.account.balances
    assert b.reservation(BRACKET) is None and b.reserved("SOL") == D("0.023")  # the exit order holds the base
    assert env.ledger.view(BOOK, PRODUCT).bracket_order_id is None
    assert await env.bar(5, o=80.0, lo=70.0) == []  # a bar that opened before the exit reached the market
    assert not task.done()
    await env.bar(11, o=92.0)
    res = await task
    expected = floor_to(D(repr(BarFillModel().market_exit(92.0, 2.0))), D("0.01"))
    assert (res.status, res.reason, res.size, res.avg_price) == ("closed", "SELL", D("0.023"), expected)
    assert res.fees == fee(expected, "0.023", TAKER_BPS)
    assert env.ledger.view(BOOK, PRODUCT).side == "FLAT" and b.reserved("SOL") == 0 and b.total("SOL") == 0
    assert not await env.ledger.open_orders(BOOK)


async def test_exit_on_a_flat_book_does_nothing(make: Callable[..., Awaitable[ShadowEnv]]) -> None:
    env = await make()
    assert (await env.gateway.submit_exit(exit_intent())).status == "nothing_to_do"
    assert (await env.gateway.emergency_exit(PRODUCT)).status == "nothing_to_do"
    assert await env.ledger.open_orders(BOOK) == []


async def test_emergency_exit_during_a_partial_entry_cancels_it_and_skips_the_bracket(
    make: Callable[..., Awaitable[ShadowEnv]],
) -> None:
    env = await make()
    entry = await start_entry(env, intent())
    await env.bar(1, v=0.048)  # 0.012 filled: LONG without a bracket yet
    assert env.ledger.view(BOOK, PRODUCT).base_qty == D("0.012")
    emergency = await start_exit(env, env.gateway.emergency_exit(PRODUCT))
    result = await entry  # the entry was cancelled (confirmed at once in a simulation) and got no bracket
    assert (result.status, result.bracket) == ("filled", "skipped")
    assert not env.gateway.has_pending_entry(PRODUCT) and env.account.balances.reserved("EUR") == 0
    await env.bar(2, o=92.0)
    res = await emergency
    assert (res.status, res.reason, res.size) == ("closed", "WATCHDOG", D("0.012"))
    assert env.ledger.view(BOOK, PRODUCT).side == "FLAT" and env.account.balances.total("SOL") == 0


async def test_cancel_stops_a_pending_entry(make: Callable[..., Awaitable[ShadowEnv]]) -> None:
    env = await make()
    task = await start_entry(env, intent())
    assert (await env.gateway.cancel("ETH-EUR")).status == "nothing_pending"
    outcome = await env.gateway.cancel(PRODUCT)
    assert outcome.status == "done" and outcome.entry is not None and outcome.entry.reason == "cancelled"
    assert (await task).status == "no_fill" and env.account.balances.free("EUR") == D(100)
    assert await env.bar(1, o=92.6, h=92.7, lo=80.0) == []  # the late bar finds nothing to fill
    assert env.ledger.view(BOOK, PRODUCT).side == "FLAT" and await env.ledger.open_orders(BOOK) == []


async def test_one_pending_entry_per_product(make: Callable[..., Awaitable[ShadowEnv]]) -> None:
    env = await make()
    await start_entry(env, intent())
    second = await env.gateway.submit_entry_with_bracket(intent("dec-2"))
    assert (second.status, second.reason) == ("blocked", "entry_already_pending")
    assert len(await env.ledger.open_orders(BOOK)) == 1
    await env.gateway.cancel(PRODUCT)  # unblock the first task so the fixture can close cleanly


# ------------------------------------------------------------------------------ coverage parity


async def test_insufficient_balance_is_refused_without_touching_the_ledger(
    make: Callable[..., Awaitable[ShadowEnv]],
) -> None:
    env = await make(eur="1")
    result = await env.gateway.submit_entry_with_bracket(intent())
    assert (result.status, result.reason) == ("refused", "insufficient_balance")
    assert await env.ledger.open_orders(BOOK) == [] and env.account.balances.open_reservations() == []
    assert env.account.balances.free("EUR") == D(1) and not env.gateway.has_pending_entry(PRODUCT)
    env.kill.breached.add(BOOK)
    env.account.balances._free["EUR"] = D(100)  # noqa: SLF001  (enough money, but the book is in coverage breach)
    breach = await env.gateway.submit_entry_with_bracket(intent("dec-2"))
    assert (breach.status, breach.reason) == ("refused", "kill_switch")


async def test_the_same_decision_cannot_create_two_simulated_orders(make: Callable[..., Awaitable[ShadowEnv]]) -> None:
    env = await make()
    spec = entry_spec(intent())
    env.guard.authorize(spec)  # an order of this id already holds its reservation
    sent = await env.gateway._send(spec, leg="entry", intent_id=None)  # noqa: SLF001
    assert (sent.status, sent.reason) == ("duplicate", "order_already_known")
    assert len(env.account.balances.open_reservations()) == 1 and await env.ledger.open_orders(BOOK) == []


async def test_live_and_shadow_gateways_authorize_the_same_orders(make: Callable[..., Awaitable[ShadowEnv]]) -> None:
    """The same intent, once on the live gateway (fake exchange) and once on the shadow gateway: the guard sees the
    same specs in the same order, and the outcome of a refusal is identical (T17 at gateway level)."""
    original = BalanceGuard.authorize

    def recording(sink: list[tuple[Any, ...]]) -> Callable[[BalanceGuard, OrderSpec], Any]:
        def spy(self: BalanceGuard, spec: OrderSpec) -> Any:
            sink.append(
                (spec.side, spec.kind, spec.base_size, spec.limit_price, spec.stop_trigger_price, spec.post_only)
            )
            return original(self, spec)

        return spy

    live = await make_live()
    poor_live = await make_live(eur="1")
    try:
        live.venue.plan_fills(ENTRY, (60, "0.023", "92.03"))
        live_calls: list[tuple[Any, ...]] = []
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(BalanceGuard, "authorize", recording(live_calls))
            live_result = await live.gateway.submit_entry_with_bracket(live_intent())
        shadow = await make()
        shadow_calls: list[tuple[Any, ...]] = []
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(BalanceGuard, "authorize", recording(shadow_calls))
            task = await start_entry(shadow, intent())
            await shadow.bar(1)
            shadow_result = await task
        assert live_calls == shadow_calls == [
            ("BUY", "limit", D("0.023"), D("92.03"), None, True),
            ("SELL", "bracket", D("0.023"), TP, SL, False),
        ]  # fmt: skip
        assert (live_result.status, live_result.bracket) == (shadow_result.status, shadow_result.bracket)
        assert live.balances.reserved("SOL") == shadow.account.balances.reserved("SOL") == D("0.023")
        assert live.balances.reserved("EUR") == shadow.account.balances.reserved("EUR") == 0
        # and the same refusal
        poor_shadow = await make(eur="1")
        a = await poor_live.gateway.submit_entry_with_bracket(live_intent())
        b = await poor_shadow.gateway.submit_entry_with_bracket(intent())
        assert (a.status, a.reason) == (b.status, b.reason) == ("refused", "insufficient_balance")
        assert poor_live.venue.log == [] and await poor_shadow.ledger.open_orders(BOOK) == []
    finally:
        await live.ledger.close()
        await poor_live.ledger.close()


async def test_the_shadow_gateway_keeps_the_entry_plan_like_the_live_one(
    make: Callable[..., Awaitable[ShadowEnv]],
) -> None:
    env = await make()
    task = await start_entry(env, intent())
    ctx = env.gateway.entry_context(ENTRY)
    assert ctx is not None and ctx.decision_id == "dec-1" and ctx.plan is not None
    assert (ctx.plan.tp, ctx.plan.sl, ctx.plan.k_tp, ctx.plan.k_sl, ctx.plan.atr) == (TP, SL, 4.0, 2.0, D("4.2334"))
    rec = await env.ledger.get_order(ENTRY)
    assert rec is not None and context_from_payload(rec.payload) == ctx  # persisted in the dry ledger
    reborn = ShadowExecutionGateway(
        BOOK, env.account, BarFillModel(), env.ledger, env.clock, env.guard, env.registry, env.fees
    )
    assert reborn.entry_context(ENTRY) is None and await reborn.restore_entry_contexts() == 1
    assert reborn.entry_context(ENTRY) == ctx
    await env.gateway.cancel(PRODUCT)
    await task
