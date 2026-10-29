"""The tick fill mode of the ShadowExecutionGateway: the brackets of the virtual positions follow the market trades and
the order book (``BracketSimulator``) instead of the 1m bars, and every fill is booked through the very path of the bar
mode (same ``BalanceGuard`` reservation, same ``VirtualAccount``, same ledger, same ``closed_trades`` row).

Proves: the outcome is the same as the bar mode where it is unambiguous (a take profit, the size, the reason, the
accounting); the deliberate divergences (the stop is a book walk, not ``min(stop, open)``; the same event is a stop; a
data gap is closed by candles); partial fills keep ``free + reserved`` exact and balances never negative; a model exit
cannot sell what a queued fill already sold; the stop of the minutes between the fill and the bar's publication is not
lost; and a restart in the middle of a partly filled bracket goes on without a duplicated fill.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from modules.config import FallbackFees
from modules.dryrun.bracket_sim import BracketFill, BracketSimulator
from modules.dryrun.fill_model import BarFillModel, TickFillModel
from modules.dryrun.restore import load_book_restore
from modules.dryrun.shadow_gateway import ShadowExecutionGateway, TickRig, TradeTape
from modules.dryrun.virtual_account import VirtualAccount
from modules.execution.balance_guard import BalanceGuard
from modules.execution.rounding import floor_to
from modules.execution.spot import SpotRegistry
from modules.ledger.positions import PositionLedger
from modules.market.orderbook import LevelUpdate, LocalOrderBook
from modules.market.ws_feed import Trade
from tests.coverage_fixtures import SOL, T0, Clock, KillSwitch
from tests.gateway_fixtures import BOOK, PRODUCT, FeeSourceStub
from tests.test_bracket_sim import Candles
from tests.test_shadow_gateway import MAKER_BPS, SL, TAKER_BPS, TP, bar, fee, intent, start_entry, start_exit

D = Decimal
MIN = timedelta(minutes=1)


def trade(price: float, size: float = 1.0, at: float = 2.0, tid: str = "t") -> Trade:
    return Trade(PRODUCT, tid, T0 + at * MIN, price, size)


def deep_book() -> LocalOrderBook:
    book = LocalOrderBook(PRODUCT)
    book.apply_snapshot(
        [LevelUpdate("bid", D("83.00"), D("1.0")), LevelUpdate("bid", D("82.00"), D("10")),
         LevelUpdate("offer", D("120"), D("5"))]  # fmt: skip
    )
    return book


@dataclass
class Env:
    gateway: ShadowExecutionGateway
    account: VirtualAccount
    ledger: PositionLedger
    guard: BalanceGuard
    clock: Clock
    registry: SpotRegistry
    fees: FeeSourceStub
    tape: TradeTape
    book: LocalOrderBook
    sim: BracketSimulator | None
    candles: Candles

    async def bar(self, minute: float, **kw: float) -> Any:
        return await self.gateway.on_bar(bar(minute, **kw))

    async def feed(self, *trades: Trade) -> list[Any]:
        """The market stream tells the books (tape first, as the manager does), then the fills are booked."""
        self.tape.add(trades)
        self.gateway.on_trades(trades)
        return await self.gateway.settle()


async def build(tick: bool, *, existing: PositionLedger | None = None) -> Env:
    clock = Clock()
    ledger = existing or await PositionLedger.open(":memory:", "dry")
    account = VirtualAccount(BOOK, D(100), clock)
    registry = SpotRegistry({PRODUCT: SOL})
    fees = FeeSourceStub(clock)
    guard = BalanceGuard(account.balances, ledger.book_view(BOOK), registry, fees, FallbackFees(), KillSwitch())
    return _gateway(tick, clock, ledger, account, registry, fees, guard)


def _gateway(
    tick: bool, clock: Clock, ledger: PositionLedger, account: VirtualAccount, registry: SpotRegistry,
    fees: FeeSourceStub, guard: BalanceGuard,
) -> Env:  # fmt: skip
    book, tape, candles = deep_book(), TradeTape(), Candles()
    rig: TickRig | None = None
    sim: BracketSimulator | None = None
    if tick:
        model = TickFillModel()
        sim = BracketSimulator(registry, fees, clock, model=model, candles=candles, books={PRODUCT: book})
        rig = TickRig(sim, tape, {PRODUCT: book}, model)
    gateway = ShadowExecutionGateway(BOOK, account, BarFillModel(), ledger, clock, guard, registry, fees, tick=rig)
    return Env(gateway, account, ledger, guard, clock, registry, fees, tape, book, sim, candles)


@pytest.fixture
async def envs() -> AsyncIterator[list[Env]]:
    made: list[Env] = []
    yield made
    for env in made:
        await env.ledger.close()


async def make(envs: list[Env], tick: bool = True) -> Env:
    env = await build(tick)
    envs.append(env)
    return env


async def open_position(env: Env, **bar_kw: float) -> None:
    task = await start_entry(env, intent())  # type: ignore[arg-type]
    await env.bar(1, **bar_kw)
    result = await task
    assert result.status == "filled" and result.bracket == "placed", result
    env.clock.now = T0 + 4 * MIN  # the bar of minute 1 is published at minute 1 + 1 + S (120 s)


def conserved(env: Env) -> None:
    """free + reserved is the total, nothing is negative, and the accounting identity holds."""
    b = env.account.balances
    for currency in ("EUR", "SOL"):
        assert b.free(currency) >= 0 and b.reserved(currency) >= 0
        assert b.total(currency) == b.free(currency) + b.reserved(currency)
    assert b.total("SOL") == env.account.position_qty(PRODUCT)
    mark = D("100")  # the identity of VirtualAccount: quote + base x mark + fees == capital + realized + unrealized
    unrealized = env.account.unrealized_pnl({PRODUCT: mark})
    lhs = b.total("EUR") + b.total("SOL") * mark + env.account.fees_paid
    assert lhs == D(100) + env.account.realized_pnl + unrealized


# ===================================================================== same outcome where it is unambiguous


async def test_a_take_profit_through_a_trade_equals_the_take_profit_through_a_bar(envs: list[Env]) -> None:
    bar_env, tick_env = await make(envs, tick=False), await make(envs)
    for env in (bar_env, tick_env):
        await open_position(env)
    [closed_bar] = await bar_env.bar(2, o=100.0, h=109.5, lo=99.0)
    [closed_tick] = await tick_env.feed(trade(109.5, 1.0, at=2.0))
    assert (closed_bar.reason, closed_tick.reason) == ("TP", "TP")
    assert (closed_bar.status, closed_bar.size, closed_bar.avg_price, closed_bar.fees) == (
        closed_tick.status, closed_tick.size, closed_tick.avg_price, closed_tick.fees
    )  # fmt: skip
    (row_bar,), (row_tick,) = await bar_env.ledger.closed_trades(BOOK), await tick_env.ledger.closed_trades(BOOK)
    assert row_bar == row_tick  # the very same closed_trades row: trade id, prices, fees, PnL, reason, time
    for name in ("fees_paid", "realized_pnl"):
        assert getattr(bar_env.account, name) == getattr(tick_env.account, name)
    for currency in ("EUR", "SOL"):
        assert bar_env.account.balances.total(currency) == tick_env.account.balances.total(currency)
        assert bar_env.account.balances.reserved(currency) == tick_env.account.balances.reserved(currency) == 0
    assert tick_env.ledger.view(BOOK, PRODUCT).side == "FLAT" and not await tick_env.ledger.open_orders(BOOK)
    conserved(tick_env)


async def test_a_stop_by_trade_and_by_bar_agree_on_everything_but_the_price(envs: list[Env]) -> None:
    bar_env, tick_env = await make(envs, tick=False), await make(envs)
    for env in (bar_env, tick_env):
        await open_position(env)
    [closed_bar] = await bar_env.bar(2, o=90.0, lo=83.0)
    [closed_tick] = await tick_env.feed(trade(83.5, 0.5, at=2.0))
    assert (closed_bar.reason, closed_tick.reason) == ("SL", "SL")
    assert (closed_bar.status, closed_bar.size) == (closed_tick.status, closed_tick.size) == ("closed", D("0.023"))
    # the bar says min(stop, open) less spread, slippage and the extra slip; the tick mode sells into the bids
    assert closed_bar.avg_price == floor_to(D(repr(BarFillModel().sl_exit(83.57, 90.0, 2.0))), D("0.01")) == D("83.43")
    assert closed_tick.avg_price == D("82.91") and closed_tick.fees == fee("82.91", "0.023", TAKER_BPS)
    (row_bar,), (row_tick,) = await bar_env.ledger.closed_trades(BOOK), await tick_env.ledger.closed_trades(BOOK)
    assert (row_tick.exit_reason, row_tick.exit_size, row_tick.entry_size) == ("SL", D("0.023"), D("0.023"))
    assert row_tick.net_quote < 0 and row_tick.exit_avg_price < row_bar.exit_avg_price
    assert tick_env.account.balances.reserved("SOL") == 0 and tick_env.account.balances.total("SOL") == 0
    conserved(tick_env)


async def test_the_bracket_exit_of_a_tick_stop_is_not_a_model_exit(envs: list[Env]) -> None:
    env = await make(envs)
    await open_position(env)
    await env.feed(trade(83.5, 0.5, at=2.0))
    (row,) = await env.ledger.closed_trades(BOOK)
    assert row.exit_reason == "SL" and row.gap_resolved is False and row.partial_entry is False


# ============================================================================================ deliberate divergences


async def test_the_same_event_that_touches_both_barriers_is_a_stop(envs: list[Env]) -> None:
    env = await make(envs)
    await open_position(env)
    [closed] = await env.feed(trade(109.5, 1.0, at=2.0, tid="up"), trade(83.0, 1.0, at=2.0, tid="down"))
    assert closed.reason == "SL"
    (row,) = await env.ledger.closed_trades(BOOK)
    assert row.exit_reason == "SL" and row.exit_avg_price < SL


async def test_two_events_are_two_events_the_take_profit_comes_first(envs: list[Env]) -> None:
    env = await make(envs)
    await open_position(env)
    [closed] = await env.feed(trade(109.5, 1.0, at=2.0, tid="up"))
    assert closed.reason == "TP"
    assert await env.feed(trade(83.0, 1.0, at=2.5, tid="down")) == []  # the position is gone: nothing to stop


async def test_a_trade_at_the_take_profit_price_does_not_fill_it(envs: list[Env]) -> None:
    env = await make(envs)
    await open_position(env)
    assert await env.feed(trade(float(TP), 5.0, at=2.0)) == []  # strictly above, as in the bar mode
    assert env.ledger.view(BOOK, PRODUCT).side == "LONG"
    assert [r.amount for r in env.account.balances.open_reservations()] == [D("0.023")]


async def test_the_take_profit_does_not_exist_in_the_fill_bar(envs: list[Env]) -> None:
    env = await make(envs)
    await open_position(env)  # filled at the bar of minute 1
    assert await env.feed(trade(109.0, 5.0, at=1.5)) == []  # a print inside the fill minute: no take profit yet
    assert env.ledger.view(BOOK, PRODUCT).side == "LONG"


# ============================================================ the minutes between the fill and the publication


async def test_a_stop_printed_after_the_fill_and_before_the_bar_was_published_is_not_lost(envs: list[Env]) -> None:
    env = await make(envs)
    task = await start_entry(env, intent())  # type: ignore[arg-type]
    env.tape.add([trade(83.0, 1.0, at=1.5, tid="early")])  # the market went through the stop 30 s after the fill
    env.tape.add([trade(100.0, 1.0, at=3.0, tid="later")])  # ...and recovered before the bar of minute 1 came out
    closed = await env.bar(1)
    assert (await task).bracket == "placed" and [c.reason for c in closed] == ["SL"]
    (row,) = await env.ledger.closed_trades(BOOK)
    expected = floor_to(D(repr(BarFillModel().sl_exit(83.57, 83.0, 2.0))), D("0.01"))  # the bar formula from the print
    assert (row.exit_reason, row.exit_avg_price, row.exit_at) == ("SL", expected, T0 + 1.5 * MIN)
    assert env.ledger.view(BOOK, PRODUCT).side == "FLAT" and env.sim is not None and env.sim.open_keys() == []
    conserved(env)


async def test_a_take_profit_printed_after_the_fill_bar_and_before_the_publication_is_credited(envs: list[Env]) -> None:
    env = await make(envs)
    task = await start_entry(env, intent())  # type: ignore[arg-type]
    env.tape.add([trade(109.0, 5.0, at=1.4, tid="in-the-fill-bar"), trade(109.0, 5.0, at=2.5, tid="after")])
    closed = await env.bar(1)
    await task
    assert [c.reason for c in closed] == ["TP"]
    (row,) = await env.ledger.closed_trades(BOOK)
    assert (row.exit_reason, row.exit_avg_price, row.exit_at) == ("TP", TP, T0 + 2.5 * MIN)


async def test_a_bar_of_the_tick_mode_does_not_resolve_the_bracket(envs: list[Env]) -> None:
    env = await make(envs)
    await open_position(env)
    assert await env.bar(2, o=90.0, lo=80.0, h=120.0) == []  # the trades decide; none was seen
    assert env.ledger.view(BOOK, PRODUCT).side == "LONG"


# ================================================================== partial fills, thin and stale books


async def test_a_take_profit_in_parts_keeps_the_balances_exact_and_closes_once(envs: list[Env]) -> None:
    env = await make(envs)
    await open_position(env)
    remaining = D("0.023")
    for k, qty in enumerate(("0.010", "0.010", "0.003"), start=1):
        [res] = await env.feed(trade(109.0, 0.04 if k < 3 else 0.012, at=2.0 + k, tid=f"p{k}"))
        remaining -= D(qty)
        assert (res.reason, res.size, res.avg_price) == ("TP", D(qty), TP)
        assert res.status == ("closed" if remaining == 0 else "partial")
        assert env.account.balances.reserved("SOL") == remaining == env.ledger.view(BOOK, PRODUCT).base_qty
        assert env.account.balances.free("SOL") == 0  # what is left is still reserved by the bracket
        conserved(env)
    assert env.account.balances.open_reservations() == [] and not await env.ledger.open_orders(BOOK)
    (row,) = await env.ledger.closed_trades(BOOK)  # ONE trade, whatever the number of fills
    assert (row.exit_reason, row.exit_size, row.exit_avg_price, row.entry_size) == ("TP", D("0.023"), TP, D("0.023"))
    fills = [f for f in await env.ledger.book_fills(BOOK) if f.side == "SELL"]
    assert len({f.trade_id for f in fills}) == 3 and all(f.liquidity == "maker" for f in fills)
    assert env.account.fees_paid == fee("92.03", "0.023", MAKER_BPS) + fee(TP, "0.023", MAKER_BPS)


async def test_a_stop_into_a_thin_book_sells_what_the_book_has_and_the_rest_on_the_next_book(envs: list[Env]) -> None:
    env = await make(envs)
    await open_position(env)
    env.book.apply_snapshot([LevelUpdate("bid", D("83.00"), D("0.010")), LevelUpdate("offer", D("120"), D("5"))])
    [first] = await env.feed(trade(83.0, 1.0, at=2.0))
    assert (first.status, first.reason, first.size, first.avg_price) == ("partial", "SL", D("0.010"), D("82.91"))
    assert env.ledger.view(BOOK, PRODUCT).base_qty == D("0.013") and env.account.balances.reserved("SOL") == D("0.013")
    conserved(env)
    env.book.apply_update([LevelUpdate("bid", D("83.00"), D(0)), LevelUpdate("bid", D("82.50"), D("5"))])
    env.gateway.on_books()  # the update of the book: the stop retries
    [second] = await env.gateway.settle()
    assert (second.status, second.size, second.avg_price) == ("closed", D("0.013"), D("82.41"))
    (row,) = await env.ledger.closed_trades(BOOK)
    assert row.exit_size == D("0.023") and row.exit_reason == "SL"
    assert row.exit_avg_price == (D("82.91") * D("0.010") + D("82.41") * D("0.013")) / D("0.023")
    conserved(env)


async def test_a_stop_waits_for_a_usable_book_and_never_invents_a_price(envs: list[Env]) -> None:
    env = await make(envs)
    await open_position(env)
    env.book.mark_stale("sequence gap")
    assert await env.feed(trade(83.0, 1.0, at=2.0)) == []  # triggered, nothing to sell into
    assert env.ledger.view(BOOK, PRODUCT).side == "LONG" and env.account.balances.reserved("SOL") == D("0.023")
    env.gateway.on_books()
    assert await env.gateway.settle() == []  # still stale
    env.book.apply_snapshot([LevelUpdate("bid", D("81.00"), D("2")), LevelUpdate("offer", D("120"), D("5"))])
    env.gateway.on_books()
    [closed] = await env.gateway.settle()
    assert (closed.reason, closed.avg_price) == ("SL", D("80.91")) and env.ledger.view(BOOK, PRODUCT).side == "FLAT"
    conserved(env)


# ======================================================================================= races with other exits


async def test_a_model_exit_does_not_sell_what_a_queued_take_profit_already_sold(envs: list[Env]) -> None:
    env = await make(envs)
    await open_position(env)
    env.tape.add([trade(109.5, 1.0)])
    env.gateway.on_trades([trade(109.5, 1.0)])  # the simulator filled the take profit; nobody booked it yet
    assert env.gateway.queued_fills == 1
    result = await env.gateway.emergency_exit(PRODUCT)
    assert result.status == "nothing_to_do"
    (row,) = await env.ledger.closed_trades(BOOK)
    assert row.exit_reason == "TP" and env.account.balances.total("SOL") == 0
    conserved(env)


async def test_an_exit_takes_the_bracket_away_from_the_simulator(envs: list[Env]) -> None:
    env = await make(envs)
    await open_position(env)
    assert env.sim is not None and env.sim.open_keys() == [f"{BOOK}|{PRODUCT}"]
    task = await start_exit(env, env.gateway.emergency_exit(PRODUCT))  # type: ignore[arg-type]
    assert env.sim.open_keys() == []
    assert await env.feed(trade(109.5, 1.0, at=2.0)) == []  # no bracket any more: no take profit
    await env.bar(5, o=100.0)  # the market exit fills at the next bar open
    assert (await task).status == "closed"
    (row,) = await env.ledger.closed_trades(BOOK)
    assert row.exit_reason == "SELL"  # not a bracket: the hint of the reason is the app's (_ObservedGateway)
    conserved(env)


async def test_a_fill_queued_for_a_bracket_that_is_gone_is_dropped_not_booked(envs: list[Env]) -> None:
    env = await make(envs)
    await open_position(env)
    task = await start_exit(env, env.gateway.emergency_exit(PRODUCT))  # type: ignore[arg-type]
    env.gateway._queue.append(_a_fill())  # noqa: SLF001  (a fill the simulator made before the exit took the bracket)
    assert await env.gateway.settle() == [] and env.gateway.tick_dropped == 1
    assert env.ledger.view(BOOK, PRODUCT).base_qty == D("0.023")  # nothing was sold by it
    await env.bar(5, o=100.0)
    assert (await task).status == "closed"
    assert env.account.balances.total("SOL") == 0 and env.account.balances.reserved("SOL") == 0
    conserved(env)


def _a_fill() -> BracketFill:
    return BracketFill(f"{BOOK}|{PRODUCT}", BOOK, PRODUCT, "TP", TP, D("0.023"), D(0), "maker", T0, D(0))


# ========================================================================================== gaps and catch-up


async def test_a_gap_is_closed_with_the_candles_and_the_trade_says_so(envs: list[Env]) -> None:
    env = await make(envs)
    await open_position(env)
    env.gateway.on_damage(T0 + 2 * MIN, T0 + 3 * MIN)
    assert await env.feed(trade(109.5, 1.0, at=3.5)) == []  # held: it would be judged against a hole
    env.candles.rows[T0 + 2 * MIN] = (90.0, 91.0, 80.0, 85.0)  # the stop was crossed during the hole
    env.clock.now = T0 + 10 * MIN
    await env.gateway.resolve_gaps()
    [closed] = await env.gateway.settle()
    assert (closed.reason, closed.avg_price) == ("SL", D("83.43"))
    (row,) = await env.ledger.closed_trades(BOOK)
    assert row.gap_resolved is True and row.exit_reason == "SL"
    assert env.ledger.gap_resolution is False  # the flag is the ledger's, put back
    assert env.gateway.tick_dropped == 0  # the held take profit print was not credited
    conserved(env)


async def test_the_bars_of_a_downtime_resolve_the_bracket_when_asked_to(envs: list[Env]) -> None:
    env = await make(envs)
    await open_position(env)
    assert await env.gateway.on_bar(bar(2, lo=80.0)) == []
    [closed] = await env.gateway.on_bar(bar(3, lo=80.0), gap=True)
    assert closed.reason == "SL" and env.sim is not None and env.sim.open_keys() == []
    assert env.ledger.view(BOOK, PRODUCT).side == "FLAT"
    conserved(env)


# ==================================================================================================== the plumbing


async def test_the_bar_mode_has_no_tick_feed(envs: list[Env]) -> None:
    env = await make(envs, tick=False)
    assert not env.gateway.tick_mode and env.gateway.queued_fills == 0 and await env.gateway.settle() == []
    with pytest.raises(RuntimeError, match="bar fill mode"):
        env.gateway.on_trades([trade(100.0)])
    assert env.gateway.tick_stats["queued"] == 0


def test_the_tape_keeps_the_last_minutes_of_each_product() -> None:
    tape = TradeTape(timedelta(minutes=2))
    other = Trade("BTC-EUR", "x", T0, 1.0, 1.0)
    tape.add([trade(1.0, at=0.0, tid="a"), trade(2.0, at=1.0, tid="b"), trade(3.0, at=2.5, tid="c"), other])
    assert [t.trade_id for t in tape.since(PRODUCT, T0)] == ["b", "c"]  # "a" is older than 2 minutes before "c"
    assert [t.trade_id for t in tape.since(PRODUCT, T0 + 2 * MIN)] == ["c"]
    assert [t.trade_id for t in tape.since("BTC-EUR", T0)] == ["x"] and tape.since("ETH-EUR", T0) == []


# ============================================================================================== restart in the middle


async def test_a_restart_in_the_middle_of_a_partly_filled_bracket_goes_on_without_a_duplicate(envs: list[Env]) -> None:
    first = await make(envs)
    await open_position(first)
    [part] = await first.feed(trade(109.0, 0.04, at=2.0, tid="a"))  # sells 0.010 of 0.023
    assert part.status == "partial"
    before = {c: (first.account.balances.free(c), first.account.balances.reserved(c)) for c in ("EUR", "SOL")}
    bracket_id = first.ledger.view(BOOK, PRODUCT).bracket_order_id
    assert bracket_id is not None
    # --- the process dies; a new one rebuilds the account and the gateway from the same ledger
    state = await load_book_restore(first.ledger, BOOK, first.registry, D(100))
    assert [r.amount for r in state.reservations] == [D("0.013")]  # sent with 0.023, 0.010 already sold
    clock = Clock()
    account = VirtualAccount(BOOK, state.starting_quote, clock)
    account.restore(state.fills, {PRODUCT: "SOL"}, state.reservations)
    guard = BalanceGuard(account.balances, first.ledger.book_view(BOOK), first.registry, first.fees, FallbackFees(),
                         KillSwitch())  # fmt: skip
    second = _gateway(True, clock, first.ledger, account, first.registry, first.fees, guard)
    report = await second.gateway.restore(state)
    assert report.restored == (PRODUCT,) and not report.placed and not report.cancelled
    after = {c: (account.balances.free(c), account.balances.reserved(c)) for c in ("EUR", "SOL")}
    assert after == before  # free and reserved, per currency, exactly as they were
    # the next take profit print is the SECOND fill of the order: a new trade id, booked
    [again] = await second.feed(trade(109.0, 0.04, at=3.0, tid="b"))
    assert (again.status, again.size) == ("partial", D("0.010"))
    ids = sorted(f.trade_id for f in await first.ledger.fills_for(bracket_id))
    assert ids == [f"sim-{bracket_id}-1", f"sim-{bracket_id}-2"]
    assert second.sim is not None and second.sim.remaining(f"{BOOK}|{PRODUCT}") == D("0.003")
    # ... and the stop still protects what is left, through the book
    [last] = await second.feed(trade(83.0, 1.0, at=4.0, tid="c"))
    assert (last.reason, last.size, last.status) == ("SL", D("0.003"), "closed")
    (row,) = await first.ledger.closed_trades(BOOK)
    assert row.exit_size == D("0.023") and row.entry_size == D("0.023")
    for b in (account.balances,):
        assert b.reserved("SOL") == 0 and b.total("SOL") == 0 and b.free("EUR") >= 0 and b.reserved("EUR") == 0


async def test_a_restart_without_a_fill_restores_the_bracket_in_the_simulator(envs: list[Env]) -> None:
    first = await make(envs)
    await open_position(first)
    state = await load_book_restore(first.ledger, BOOK, first.registry, D(100))
    clock = Clock()
    account = VirtualAccount(BOOK, state.starting_quote, clock)
    account.restore(state.fills, {PRODUCT: "SOL"}, state.reservations)
    guard = BalanceGuard(account.balances, first.ledger.book_view(BOOK), first.registry, first.fees, FallbackFees(),
                         KillSwitch())  # fmt: skip
    second = _gateway(True, clock, first.ledger, account, first.registry, first.fees, guard)
    report = await second.gateway.restore(state)
    assert report.restored == (PRODUCT,) and PRODUCT in report.catch_up_from
    assert second.sim is not None and second.sim.open_keys() == [f"{BOOK}|{PRODUCT}"]
    [closed] = await second.feed(trade(109.5, 1.0, at=5.0))
    assert closed.reason == "TP" and closed.avg_price == TP
    assert account.balances.total("SOL") == 0 and SL < TP


# ============================================================================== bar and tick on synthetic paths


async def _run_path(bar_env: Env, tick_env: Env, path: list[str]) -> tuple[str | None, str | None]:
    for env in (bar_env, tick_env):
        await open_position(env)
    bar_reason = tick_reason = None
    for i, kind in enumerate(path):
        minute = 2 + i
        hi, lo = {"in": (105.0, 95.0), "up": (110.0, 95.0), "down": (105.0, 80.0)}[kind]
        for closed in await bar_env.bar(minute, o=100.0, h=hi, lo=lo, c=100.0):
            bar_reason = bar_reason or closed.reason
        for k, price in enumerate((100.0, lo, hi, 100.0)):  # the trades the bar is made of, one event each
            for closed in await tick_env.feed(trade(price, 100.0, at=minute + 0.1 * k, tid=f"{i}-{k}")):
                tick_reason = tick_reason or closed.reason
    return bar_reason, tick_reason


@given(st.lists(st.sampled_from(["in", "up", "down"]), min_size=1, max_size=6))
@settings(max_examples=40, deadline=None)
def test_bar_and_tick_agree_on_every_path_whose_outcome_is_unambiguous(path: list[str]) -> None:
    """No bar touches both barriers, so the bar mode and the tick mode must close for the same reason, with the same
    size, and leave the same state; the prices of a stop differ by the rule (book walk against ``min(stop, open)``)."""

    async def scenario() -> None:
        bar_env, tick_env = await build(False), await build(True)
        try:
            await check(bar_env, tick_env)
        finally:
            await bar_env.ledger.close()
            await tick_env.ledger.close()

    async def check(bar_env: Env, tick_env: Env) -> None:
        bar_reason, tick_reason = await _run_path(bar_env, tick_env, path)
        first = next((k for k in path if k != "in"), None)
        assert bar_reason == tick_reason == {None: None, "up": "TP", "down": "SL"}[first]
        for currency in ("EUR", "SOL"):
            for env in (bar_env, tick_env):
                b = env.account.balances
                assert b.free(currency) >= 0 and b.total(currency) == b.free(currency) + b.reserved(currency)
        assert bar_env.ledger.view(BOOK, PRODUCT).side == tick_env.ledger.view(BOOK, PRODUCT).side
        assert bar_env.account.balances.total("SOL") == tick_env.account.balances.total("SOL")
        assert len(bar_env.account.balances.open_reservations()) == len(tick_env.account.balances.open_reservations())
        if first == "up":  # a take profit is the same fill in both: same price, size, fee, row, account
            assert bar_env.account.fees_paid == tick_env.account.fees_paid
            assert bar_env.account.balances.total("EUR") == tick_env.account.balances.total("EUR")
        if first == "down":
            rows = [await e.ledger.closed_trades(BOOK) for e in (bar_env, tick_env)]
            assert [len(r) for r in rows] == [1, 1] and rows[0][0].exit_size == rows[1][0].exit_size
            assert rows[0][0].net_quote < 0 and rows[1][0].net_quote < 0

    asyncio.run(scenario())


async def test_a_trade_older_than_the_position_does_not_stop_it(envs: list[Env]) -> None:
    """The channel resends its recent trades at every (re)connection: a position filled at minute 1 is not stopped by
    a print of minute 0.5 (and its closed trade would end before it began)."""
    env = await make(envs)
    await open_position(env)
    assert await env.feed(trade(80.0, 5.0, at=0.5, tid="old")) == []
    assert env.ledger.view(BOOK, PRODUCT).side == "LONG" and env.sim is not None and env.sim.remaining(
        f"{BOOK}|{PRODUCT}") == D("0.023")  # fmt: skip
    [closed] = await env.feed(trade(80.0, 5.0, at=1.0, tid="own"))  # a print at the very minute of the fill counts
    assert closed.reason == "SL"
    (row,) = await env.ledger.closed_trades(BOOK)
    assert row.exit_at >= row.entry_at


async def test_the_bars_of_a_downtime_sell_only_what_is_left_of_a_partly_filled_bracket(envs: list[Env]) -> None:
    env = await make(envs)
    await open_position(env)
    [part] = await env.feed(trade(109.0, 0.04, at=2.0))  # 0.010 sold by the take profit
    assert part.status == "partial"
    [closed] = await env.gateway.on_bar(bar(3, lo=80.0), gap=True)
    assert (closed.reason, closed.size, closed.status) == ("SL", D("0.013"), "closed")
    assert env.account.balances.total("SOL") == 0 and env.account.balances.reserved("SOL") == 0
    assert env.gateway.tick_errors == 0
    conserved(env)
