"""Shadow execution gateway: the dry-run counterpart of ``LiveExecutionGateway`` (spec sez. 5.1.1, 5.1.2, 5.3 punto 6).

Same interface (``GatewayContract``), same order life cycle, same coverage circuit. What changes is the venue:

* every order, simulated or not, is authorized by ``BalanceGuard.authorize`` (the very code of the live gateway) and its
  ticket is consumed when the order is "sent"; a refusal is the same typed result as in live. The reservation lives in
  the ``BalanceBook`` of the ``VirtualAccount`` and is released only at the simulated terminal state;
* fills come from the ``BarFillModel`` (the tick face is a later phase), one market bar at a time through ``on_bar``.
  Nothing sleeps: the gateway is driven by the bars and by the injected ``clock`` (simulated in replay and in tests);
* the position, the orders and the fills go to the DRY ledger with the same code as live (``LedgerBooking``);
* it has no exchange: it cannot send an order to Coinbase, and it refuses a ledger that is not the dry one.

Like the live gateway it remembers the plan of every entry (``entry_context``, persisted in the order's payload and
rebuilt by ``restore_entry_contexts``) and writes each fill together with its position in one ledger transaction.

Bar-level rules (a deliberately pessimistic reading of sez. 5.1.3; the same kernels as ``SimulatedExchange``):

* an order reaches the market at ``clock() + latency_ms``; only bars that OPEN at or after that instant act on it;
* maker entry: rests at its limit and fills on a bar with ``low < limit``, at most ``queue_haircut x volume`` of the
  bar per bar (partial fills carry over), maker fee; a post-only limit that would cross the estimated ask is rejected;
  at ``entry_timeout`` (first bar opening after the deadline) the residual is cancelled;
* taker entry: fills at the first eligible bar open plus half spread and slippage if that is within the limit, else
  expires; taker fee;
* the bracket exists from the terminal state of the entry. Stop: ``low <= stop`` (acts in the fill bar too), price
  ``sl_exit`` rounded down, taker fee. Take profit: ``high > tp`` from the bar AFTER the fill bar, price = tp, maker
  fee. Same bar: the stop wins;
* a model exit or an emergency exit cancels the bracket (confirmed at once, it is a simulation) and sells at
  ``market_exit`` of the first eligible bar open, taker fee.

Limits of the bar face, to be lifted by the tick face: the virtual stop of a position with a partial entry and no
bracket yet is checked only at the bar where the entry ends; the watchdog covers that window in live and dry.

Tick fill mode (``tick=TickRig(...)``, ``dry_run.fill_model.mode: tick``). The entry, the model exit and the watchdog
exit stay bar-driven (above). What changes is who resolves the bracket (take profit, stop) of a position:

* the ``BracketSimulator`` follows it trade by trade (``on_trades``), retries a stop that waits for liquidity on every
  book update (``on_books``) and closes a data gap with the candles (``on_damage``, ``resolve_gaps``). It only PRODUCES
  ``BracketFill``: it books nothing;
* the fills wait in a queue and ``settle`` books them, one at a time, through ``_book_fill`` (the very code of the bar
  mode: ``LedgerBooking`` for the ledger, ``VirtualAccount.record_fill`` against the reservation the guard made when
  the bracket was authorized). A take profit or a stop can fill in parts: the reservation shrinks with each part and is
  released with the last; the ``closed_trades`` row and its exit reason (price against the take profit) are the
  ledger's, as in the bar mode. ``on_bar`` and every exit settle the queue first, so a fill that the market produced
  before the bar or the exit is booked before it;
* a bracket is armed from the position: the trades of the minutes between the fill and the moment the bar was
  published (``TradeTape``) are checked first. A print at or below the stop in that window closes the position at the
  bar-level price of the stop (``sl_exit`` from the trigger print: the book of that moment is gone); the take profit
  exists from the bar AFTER the fill bar, as in the bar mode. Then the simulator follows the live trades;
* a bar acts on a bracket only in a gap catch-up (``on_bar(bar, gap=True)``: the bars of a downtime), when no trade was
  seen. Everything else is the bar mode.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final, Literal

import structlog

from modules.dryrun.bracket_sim import BracketFill, BracketSimulator
from modules.dryrun.fill_model import BarFillModel, TickFillModel
from modules.dryrun.restore import BookRestore
from modules.dryrun.virtual_account import VirtualAccount
from modules.execution.balance import Clock, CoverageAlarm
from modules.execution.balance_guard import BalanceGuard, CoverageRefused, FeeSource
from modules.execution.gateway import (
    AlarmSink,
    BracketStatus,
    CancelOutcome,
    EntryContexts,
    EntryResult,
    ExitResult,
    ExitStatus,
    FillEvent,
    LedgerBooking,
    bracket_spec,
    entry_spec,
    exit_spec,
    is_sellable,
    sellable_base,
)
from modules.execution.ids import client_order_id
from modules.execution.orders import OrderSpec, ProductMeta
from modules.execution.rounding import ceil_to, floor_to
from modules.execution.spot import SpotRegistry
from modules.execution.user_stream import EntryContext
from modules.ledger.positions import EntryPlan, LedgerModeMismatch, PositionLedger, PositionView
from modules.market.orderbook import LocalOrderBook
from modules.market.ws_feed import Trade
from modules.risk.risk_manager import ExitIntent, OrderIntent

__all__ = ["BarEvent", "RestoreReport", "ShadowExecutionGateway", "TickRig", "TradeTape"]

log = structlog.get_logger(__name__)

ZERO: Final = Decimal(0)
BPS: Final = Decimal(10_000)
MINUTE: Final = timedelta(minutes=1)
TAPE_KEEP: Final = timedelta(minutes=15)  # a bar is published about 3 minutes after its minute: a wide margin


@dataclass(frozen=True, slots=True)
class BarEvent:
    """One closed market bar of one product (OHLCV in float, as the bar store has them) and its spread estimate."""

    product_id: str
    ts_open: datetime
    o: float
    h: float
    lo: float
    c: float
    v: float
    spread_bps: float


@dataclass(frozen=True, slots=True)
class RestoreReport:
    """What ``ShadowExecutionGateway.restore`` did after a restart (``catch_up_from``: per product, the open time of
    the position whose bracket was re-registered; the bars from then on are fed to ``on_bar`` to resolve the gap)."""

    cancelled: tuple[str, ...] = ()  # client_order_ids of the open orders that could not be resumed
    restored: tuple[str, ...] = ()  # products whose open bracket is simulated again
    placed: tuple[str, ...] = ()  # products that got a NEW bracket (the old one was gone)
    unprotected: tuple[str, ...] = ()  # LONG positions left without a bracket (the watchdog covers them)
    catch_up_from: dict[str, datetime] = field(default_factory=dict)


class TradeTape:
    """The trades of the last minutes per product, shared by the books of a manager (tick fill mode).

    A bar is published about three minutes after its minute, so a position filled "at" a bar is armed when its first
    minutes are already history: the tape is where the bracket finds the trades it missed. Old trades are dropped
    against the newest trade of the product, never against a clock."""

    def __init__(self, keep: timedelta = TAPE_KEEP) -> None:
        self._keep = keep
        self._trades: dict[str, deque[Trade]] = {}

    def add(self, trades: Iterable[Trade]) -> None:
        for tr in trades:
            tape = self._trades.setdefault(tr.product_id, deque())
            tape.append(tr)
            while tape[0].ts < tr.ts - self._keep:
                tape.popleft()

    def since(self, product_id: str, t: datetime) -> list[Trade]:
        """The trades of ``product_id`` at or after ``t``, in the order they arrived."""
        return [tr for tr in self._trades.get(product_id, ()) if tr.ts >= t]


@dataclass(frozen=True, slots=True)
class TickRig:
    """What the tick fill mode needs: the simulator (one per gateway), the shared tape, the order books of the stream
    and the tick face of the fill model (the stop rule the tape check shares with the simulator)."""

    sim: BracketSimulator
    tape: TradeTape
    books: Mapping[str, LocalOrderBook]
    model: TickFillModel


@dataclass(slots=True)
class _Entry:
    intent: OrderIntent
    spec: OrderSpec
    meta: ProductMeta
    plan: EntryPlan
    arrival: datetime
    deadline: datetime
    maker: bool
    prev_dust_qty: Decimal
    prev_dust_cost: Decimal
    remaining: Decimal
    armed: bool = False  # the order is authorized and recorded: bars may act on it
    closing: bool = False  # the end of the entry has started (a cancel and a bar can race)
    first_bar_seen: bool = False
    fills: int = 0
    filled: Decimal = ZERO
    notional: Decimal = ZERO
    fees: Decimal = ZERO
    cancel_requested: bool = False
    abort_bracket: bool = False
    result: EntryResult | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def cost(self) -> Decimal:
        return self.notional + self.fees

    @property
    def avg_price(self) -> Decimal | None:
        return None if self.filled <= 0 else self.notional / self.filled


@dataclass(slots=True)
class _Exit:
    spec: OrderSpec
    meta: ProductMeta
    arrival: datetime
    reason: str
    result: ExitResult | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(slots=True)
class _Bracket:
    cid: str
    meta: ProductMeta
    size: Decimal
    tp: Decimal
    sl: Decimal
    placed_bar_open: datetime  # the take profit does not exist in the bar where the bracket is placed
    opened_at: datetime | None = None  # when the position was filled: the trades before it are not its own
    fills: int = 0  # simulated fills booked so far (tick mode: a bracket can fill in parts); numbers the trade ids


@dataclass(frozen=True, slots=True)
class _Sent:
    status: Literal["open", "refused", "duplicate"]
    reason: str = ""
    detail: str = ""


def _price(x: float, inc: Decimal, *, up: bool) -> Decimal:
    d = Decimal(repr(float(x)))
    return ceil_to(d, inc) if up else floor_to(d, inc)


class ShadowExecutionGateway:
    def __init__(
        self,
        book: str,
        account: VirtualAccount,
        fill_model: BarFillModel,
        ledger: PositionLedger,
        clock: Clock,
        guard: BalanceGuard,
        registry: SpotRegistry,
        fees: FeeSource,
        *,
        on_alarm: AlarmSink | None = None,
        tick: TickRig | None = None,
    ) -> None:
        if not isinstance(guard, BalanceGuard):  # a look-alike or None is not the coverage circuit
            raise TypeError("ShadowExecutionGateway needs a BalanceGuard")
        if ledger.mode != "dry":
            raise LedgerModeMismatch(f"a shadow gateway writes the dry ledger only, got the {ledger.mode!r} one")
        if account.balances.source != "virtual" or account.book != book:
            raise ValueError("a shadow gateway needs the virtual account of its own book")
        self._book = book
        self._account = account
        self._fill = fill_model
        self._ledger = ledger
        self._clock = clock
        self._guard = guard  # mandatory and typed: the same circuit as live, no gateway without it
        self._registry = registry
        self._fees = fees
        self._booking = LedgerBooking(book, ledger, clock, on_alarm)
        self._latency = timedelta(milliseconds=fill_model.params.latency_ms)
        self._entries: dict[str, _Entry] = {}
        self._exits: dict[str, _Exit] = {}
        self._brackets: dict[str, _Bracket] = {}
        self._last_bar: dict[str, BarEvent] = {}
        self._exit_locks: dict[str, asyncio.Lock] = {}
        self._contexts = EntryContexts()
        self._tick = tick
        self._queue: deque[BracketFill] = deque()  # fills the simulator produced and nobody booked yet
        self._settle_lock = asyncio.Lock()
        self._reported: list[tuple[str, ExitResult]] = []  # tick exits booked and not yet handed to the caller
        self.tick_dropped = 0  # queued fills whose bracket was gone (an exit cancelled it first): nothing was sold
        self.tick_errors = 0  # queued fills that could not be booked

    # ---------------------------------------------------------------------------- order plumbing

    async def _send(
        self, spec: OrderSpec, *, leg: str, intent_id: str | None, context: EntryContext | None = None
    ) -> _Sent:
        """authorize -> record -> consume the ticket ("send"). The venue is the fill model: nothing leaves."""
        cid = spec.client_order_id
        if self._account.balances.reservation(cid) is not None:  # same id still unresolved: never a second order
            return _Sent("duplicate", "order_already_known", f"{cid} still holds a reservation")
        try:
            ticket = self._guard.authorize(spec)
        except CoverageRefused as exc:
            log.info("coverage_refused", client_order_id=cid, side=spec.side, kind=spec.kind, reason=exc.reason)
            return _Sent("refused", exc.reason, str(exc))
        try:
            await self._booking.open_order(spec, leg=leg, intent_id=intent_id, context=context)
        except BaseException:
            self._account.balances.release(cid)  # nothing was "sent"
            raise
        ticket.consume()
        await self._booking.set_status(cid, "OPEN", f"sim-{cid}")
        return _Sent("open")

    def _fee(self, price: Decimal, size: Decimal, liquidity: Literal["maker", "taker"]) -> Decimal:
        rates = self._fees.current
        bps = Decimal(str(rates.maker_bps if liquidity == "maker" else rates.taker_bps))
        return price * size * bps / BPS

    async def _book_fill(
        self,
        cid: str,
        meta: ProductMeta,
        side: Literal["BUY", "SELL"],
        n: int,
        price: Decimal,
        size: Decimal,
        liquidity: Literal["maker", "taker"],
        when: datetime,
        *,
        decision_id: str,
        plan: EntryPlan | None,
        fee: Decimal | None = None,
    ) -> Decimal:
        """Apply one simulated fill (VirtualAccount balances, ledger via LedgerBooking). Returns the fee (``fee``
        when the fill model already priced it, else the current fee rates)."""
        fee = self._fee(price, size, liquidity) if fee is None else fee

        def apply(f: FillEvent) -> list[CoverageAlarm]:
            return self._account.record_fill(
                f.client_order_id,
                product_id=f.product_id,
                base_currency=meta.base_currency,
                price=f.price,
                size=f.size,
                fee=f.fee,
                trade_id=f.trade_id,
            )

        await self._booking.book_fill(
            FillEvent(f"sim-{cid}-{n}", cid, meta.product_id, side, price, size, fee, liquidity, when),
            apply,
            decision_id=decision_id,
            plan=plan,
        )
        return fee

    # ------------------------------------------------------------------------------------- entry

    async def submit_entry_with_bracket(self, intent: OrderIntent) -> EntryResult:
        if intent.book != self._book:
            raise ValueError(f"intent for book {intent.book!r} sent to the gateway of {self._book!r}")
        spec = entry_spec(intent)
        cid, product = spec.client_order_id, intent.product_id
        meta = self._registry.meta(product)
        position = self._ledger.view(self._book, product)
        if product in self._entries:
            return EntryResult("blocked", cid, reason="entry_already_pending")
        if position.is_long:
            return EntryResult("blocked", cid, reason="position_open")
        now = self._clock()
        plan = EntryPlan(
            intent.bracket.take_profit, intent.bracket.stop_loss, intent.bracket.k_tp, intent.bracket.k_sl,
            intent.bracket.atr,
        )  # fmt: skip
        e = _Entry(
            intent=intent, spec=spec, meta=meta, plan=plan, arrival=now + self._latency,
            deadline=now + timedelta(seconds=intent.entry.time_in_force_s),
            maker=intent.entry.type == "limit_post_only", prev_dust_qty=position.dust_qty,
            prev_dust_cost=position.dust_cost, remaining=intent.base_size,
        )  # fmt: skip
        self._entries[product] = e  # registered before any await: one pending entry per (book, product)
        try:
            context = EntryContext(intent.decision_id, plan)
            self._contexts.remember(cid, context)  # same as live: the plan is known before the order exists
            sent = await self._send(spec, leg="entry", intent_id=intent.intent_id, context=context)
        except BaseException:
            self._entries.pop(product, None)
            e.done.set()
            raise
        if sent.status != "open":
            self._entries.pop(product, None)
            self._contexts.forget(cid)  # nothing was opened under this id
            e.done.set()
            return EntryResult("blocked" if sent.status == "duplicate" else sent.status, cid, sent.reason, sent.detail)
        e.armed = True
        if e.cancel_requested:  # a cancel arrived while the order was being authorized
            await self._cancel_entry(e)
        await e.done.wait()  # resolved by on_bar
        assert e.result is not None
        return e.result

    async def _entry_step(self, e: _Entry, bar: BarEvent) -> None:
        if not e.armed or e.closing or bar.ts_open < e.arrival:
            return
        inc = e.meta.quote_increment
        limit = e.intent.entry.limit_price
        first = not e.first_bar_seen
        e.first_bar_seen = True
        # a taker entry (20 s timeout) lives for the first bar after its arrival, whatever the bar's own timestamp
        if e.cancel_requested or (e.maker and bar.ts_open >= e.deadline):
            await self._finish_entry(e, bar, "cancelled" if e.cancel_requested else "expired")
            return
        if e.maker:
            if first and limit >= _price(bar.o * (1 + bar.spread_bps / 2 / 1e4), inc, up=False):
                await self._finish_entry(e, bar, "post_only_would_cross", rejected=True)
                return
            if self._fill.maker_filled(bar.lo, float(limit)):
                cap = floor_to(Decimal(repr(self._fill.maker_fill_qty_cap(bar.v))), e.meta.base_increment)
                qty = min(e.remaining, cap)
                if qty > 0:
                    await self._fill_entry(e, bar, qty, limit, "maker")
        else:  # a taker entry has a 20 s timeout: one bar is the whole window
            px = _price(self._fill.taker_entry(bar.o, bar.spread_bps), inc, up=True)
            if px <= limit:
                await self._fill_entry(e, bar, e.remaining, px, "taker")
            else:
                await self._finish_entry(e, bar, "expired")
                return
        if e.remaining <= 0:
            await self._finish_entry(e, bar, "filled")
        elif not e.maker:
            await self._finish_entry(e, bar, "expired")

    async def _fill_entry(
        self, e: _Entry, bar: BarEvent, qty: Decimal, price: Decimal, liquidity: Literal["maker", "taker"]
    ) -> None:
        e.fills += 1
        cid = e.spec.client_order_id
        fee = await self._book_fill(
            cid,
            e.meta,
            "BUY",
            e.fills,
            price,
            qty,
            liquidity,
            bar.ts_open,
            decision_id=e.intent.decision_id,
            plan=e.plan,
        )
        e.remaining -= qty
        e.filled += qty
        e.notional += price * qty
        e.fees += fee

    async def _finish_entry(self, e: _Entry, bar: BarEvent | None, why: str, *, rejected: bool = False) -> None:
        """The entry is terminal (filled, expired, cancelled, rejected): release the residual, then bracket or dust."""
        e.closing = True
        cid, product = e.spec.client_order_id, e.meta.product_id
        self._account.balances.release(cid)
        if rejected:
            await self._booking.set_status(cid, "REJECTED")
            e.result = EntryResult("rejected", cid, reason=why)
        elif e.filled <= 0:
            await self._booking.set_status(cid, "EXPIRED" if why == "expired" else "CANCELLED")
            e.result = EntryResult("no_fill", cid, reason=why)
        else:
            await self._booking.set_status(cid, "FILLED" if e.remaining <= 0 else "CANCELLED")
            e.result = await self._protect(e, bar)
        self._entries.pop(product, None)
        e.done.set()

    async def _protect(self, e: _Entry, bar: BarEvent | None) -> EntryResult:
        intent, meta = e.intent, e.meta
        cid = e.spec.client_order_id

        def filled(
            bracket: BracketStatus, reason: str = "", detail: str = "", bracket_cid: str | None = None
        ) -> EntryResult:
            return EntryResult(
                "filled", cid, reason, detail, e.filled, e.avg_price, e.fees, bracket, bracket_cid, e.prev_dust_qty > 0
            )

        if e.abort_bracket:
            return filled("skipped")
        assert bar is not None  # fills come from bars, so a bar has been seen
        position = self._ledger.view(self._book, meta.product_id)
        if not position.is_long:
            return filled("none", "position_not_recorded")
        size = sellable_base(position, self._account.balances, meta)
        bid_estimate = Decimal(repr(bar.c * (1 - bar.spread_bps / 2 / 1e4)))
        if not is_sellable(size, bid_estimate, meta):
            await self._booking.to_dust(meta.product_id, position.base_qty, e.cost + e.prev_dust_cost)
            return filled("dust")
        spec = bracket_spec(intent, size)
        sent = await self._send(spec, leg="bracket", intent_id=intent.intent_id)
        if sent.status != "open":
            log.error("bracket_not_placed", client_order_id=spec.client_order_id, reason=sent.reason)
            return filled(
                "unknown" if sent.status == "duplicate" else sent.status, sent.reason, sent.detail, spec.client_order_id
            )
        await self._ledger.set_bracket(
            self._book, meta.product_id, spec.client_order_id, intent.bracket.take_profit, intent.bracket.stop_loss
        )
        bracket = _Bracket(
            spec.client_order_id, meta, size, intent.bracket.take_profit, intent.bracket.stop_loss, bar.ts_open
        )
        self._brackets[meta.product_id] = bracket
        self._arm_ticks(bracket, position.opened_at or bar.ts_open, bar.ts_open + MINUTE, bar.spread_bps)
        return filled("placed", bracket_cid=spec.client_order_id)

    # -------------------------------------------------------------------------------------- cancel

    def entry_context(self, client_order_id: str) -> EntryContext | None:
        """The plan of an entry we sent (same interface as the live gateway)."""
        return self._contexts.get(client_order_id)

    async def restore_entry_contexts(self) -> int:
        """Rebuild the plans of the entries still open in the dry ledger. Returns how many were restored."""
        return await self._contexts.restore(self._ledger, self._book)

    async def restore(self, state: BookRestore) -> RestoreReport:
        """Continue after a restart from the dry ledger (``state`` was read before the account was rebuilt).

        The orders that were open and cannot be resumed (an entry waiting for bars, an exit, an orphan bracket) are
        cancelled in the ledger: the rebuilt account holds no reservation for them. The brackets that were open on a
        LONG position are simulated again with the very levels they were sent with. A LONG position left without a
        bracket (it was between an exit and its fill, or its entry was still working) gets a new one through the guard
        like any other order. Call it once, before the first bar, with the account already restored from the same
        ``state``.
        """
        kept = {rec.product_id: rec for rec in state.bracket_orders}
        cancelled: list[str] = []
        for rec in state.open_orders:
            if rec in state.bracket_orders:
                continue
            await self._booking.set_status(rec.client_order_id, "CANCELLED")
            self._contexts.forget(rec.client_order_id)
            cancelled.append(rec.client_order_id)
            log.warning("restart_open_order_cancelled", book=self._book, client_order_id=rec.client_order_id,
                        leg=rec.leg, product=rec.product_id)  # fmt: skip
        restored: list[str] = []
        placed: list[str] = []
        unprotected: list[str] = []
        catch_up: dict[str, datetime] = {}
        for pos in self._ledger.long_positions(self._book):
            product, meta = pos.product_id, self._registry.meta(pos.product_id)
            alive = kept.get(product)
            if alive is not None and pos.tp is not None and pos.sl is not None:
                # what is left of the bracket: the reservation the account was rebuilt with (a tick bracket may have
                # sold a part); the next fill continues the numbering of the trade ids of this order
                left = next(r.amount for r in state.reservations if r.client_order_id == alive.client_order_id)
                opened = pos.opened_at or self._clock()
                bracket = _Bracket(alive.client_order_id, meta, left, pos.tp, pos.sl, opened)
                bracket.fills = len(await self._ledger.fills_for(alive.client_order_id))
                self._brackets[product] = bracket
                self._arm_ticks(bracket, opened, opened.replace(second=0, microsecond=0) + MINUTE, 0.0)
                restored.append(product)
                if pos.opened_at is not None:
                    catch_up[product] = pos.opened_at
                continue
            if await self._replace_bracket(pos):
                placed.append(product)
            else:
                unprotected.append(product)
        return RestoreReport(tuple(cancelled), tuple(restored), tuple(placed), tuple(unprotected), catch_up)

    async def _replace_bracket(self, pos: PositionView) -> bool:
        """A new standalone bracket for a LONG position that has none (restart). ``False``: not possible."""
        product, meta = pos.product_id, self._registry.meta(pos.product_id)
        if pos.tp is None or pos.sl is None:
            log.error("restart_position_without_levels", book=self._book, product=product)
            return False
        size = sellable_base(pos, self._account.balances, meta)
        if not is_sellable(size, pos.avg_entry or pos.sl, meta):
            log.error("restart_position_not_sellable", book=self._book, product=product, size=str(size))
            return False
        now = self._clock()
        cid = client_order_id(f"restart:{pos.entry_decision_id or product}:{int(now.timestamp())}", "bracket")
        spec = OrderSpec(
            client_order_id=cid, book=self._book, product_id=product, side="SELL", kind="bracket", base_size=size,
            limit_price=pos.tp, stop_trigger_price=pos.sl,
        )  # fmt: skip
        sent = await self._send(spec, leg="bracket", intent_id=None)
        if sent.status != "open":
            log.error("restart_bracket_not_placed", book=self._book, product=product, reason=sent.reason)
            return False
        await self._ledger.set_bracket(self._book, product, cid, pos.tp, pos.sl)
        bracket = _Bracket(cid, meta, size, pos.tp, pos.sl, now)
        self._brackets[product] = bracket
        self._arm_ticks(bracket, pos.opened_at or now, now, 0.0)
        return True

    def has_pending_entry(self, product_id: str) -> bool:
        """An entry order is authorized and waits for market bars."""
        e = self._entries.get(product_id)
        return e is not None and e.armed

    def has_pending_exit(self, product_id: str) -> bool:
        """An exit order is authorized and waits for a market bar."""
        return product_id in self._exits

    async def _cancel_entry(self, e: _Entry) -> None:
        """Cancel the residual of an entry. A simulated cancel is confirmed at once; what was filled follows the
        normal path (bracket or dust)."""
        e.cancel_requested = True
        if e.armed and not e.closing:
            await self._finish_entry(e, self._last_bar.get(e.meta.product_id), "cancelled")
        await e.done.wait()

    async def cancel(self, product_id: str) -> CancelOutcome:
        e = self._entries.get(product_id)
        if e is None:
            return CancelOutcome("nothing_pending")
        await self._cancel_entry(e)
        return CancelOutcome("done", e.result)

    # --------------------------------------------------------------------------------------- exit

    async def submit_exit(self, intent: ExitIntent) -> ExitResult:
        if intent.book != self._book:
            raise ValueError(f"exit for book {intent.book!r} sent to the gateway of {self._book!r}")
        return await self._exit(intent.product_id, intent.decision_id, intent.intent_id, intent.reason)

    async def emergency_exit(self, product_id: str) -> ExitResult:
        position = self._ledger.view(self._book, product_id)
        stamp = int(self._clock().timestamp() * 1000)
        decision_id = f"emergency:{position.entry_decision_id or product_id}:{stamp}"
        return await self._exit(product_id, decision_id, None, "WATCHDOG")

    async def _exit(self, product_id: str, decision_id: str, intent_id: str | None, reason: str) -> ExitResult:
        async with self._exit_locks.setdefault(product_id, asyncio.Lock()):
            return await self._exit_locked(product_id, decision_id, intent_id, reason)

    async def _exit_locked(self, product_id: str, decision_id: str, intent_id: str | None, reason: str) -> ExitResult:
        meta = self._registry.meta(product_id)
        await self._book_queue()  # what the market already did to the bracket is booked before the position is sold
        pending = self._entries.get(product_id)
        if pending is not None:  # a half-filled entry is cancelled and gets no bracket
            pending.abort_bracket = True
            await self._cancel_entry(pending)
        position = self._ledger.view(self._book, product_id)
        if not position.is_long:
            return ExitResult("nothing_to_do", reason)
        # X1-X3: cancel the bracket; in a simulation the confirmation is immediate, and only then is its base free
        bracket = self._drop_bracket(product_id)
        active = bracket.cid if bracket is not None else position.bracket_order_id
        if active is not None:
            self._account.balances.release(active)
            await self._booking.set_status(active, "CANCELLED")
            await self._ledger.set_bracket(self._book, product_id, None, None, None)
            position = self._ledger.view(self._book, product_id)
        size = sellable_base(position, self._account.balances, meta) or position.base_qty
        spec = exit_spec(client_order_id(decision_id, "exit"), self._book, product_id, size, kind="market")
        sent = await self._send(spec, leg="exit", intent_id=intent_id)
        if sent.status != "open":
            return ExitResult("refused", reason, f"{sent.reason}: {sent.detail}")
        x = _Exit(spec, meta, self._clock() + self._latency, reason)
        self._exits[product_id] = x
        await x.done.wait()  # resolved by on_bar
        assert x.result is not None
        return x.result

    async def _exit_step(self, x: _Exit, bar: BarEvent) -> ExitResult | None:
        if bar.ts_open < x.arrival:
            return None
        meta, spec = x.meta, x.spec
        price = _price(self._fill.market_exit(bar.o, bar.spread_bps), meta.quote_increment, up=False)
        cid = spec.client_order_id
        fee = await self._book_fill(
            cid, meta, "SELL", 1, price, spec.base_size, "taker", bar.ts_open, decision_id="", plan=None
        )
        self._account.balances.release(cid)
        await self._booking.set_status(cid, "FILLED")
        left = self._ledger.view(self._book, meta.product_id)
        status: ExitStatus = "partial" if left.is_long else "closed"
        x.result = ExitResult(status, x.reason, "", spec.base_size, price, fee)
        self._exits.pop(meta.product_id, None)
        x.done.set()
        return x.result

    # --------------------------------------------------------------------------------- bracket

    async def _bracket_step(self, b: _Bracket, bar: BarEvent) -> ExitResult | None:
        sl_hit = bar.lo <= float(b.sl)
        tp_hit = bar.ts_open > b.placed_bar_open and self._fill.tp_touched(bar.h, float(b.tp))
        if not sl_hit and not tp_hit:
            return None
        meta = b.meta
        liquidity: Literal["maker", "taker"]
        if sl_hit:  # the stop wins when both are inside the bar
            price = _price(self._fill.sl_exit(float(b.sl), bar.o, bar.spread_bps), meta.quote_increment, up=False)
            reason, liquidity = "SL", "taker"
        else:
            price, reason, liquidity = b.tp, "TP", "maker"
        b.fills += 1  # a bracket the tick mode already sold a part of continues the numbering of its trade ids
        fee = await self._book_fill(
            b.cid,
            meta,
            "SELL",
            b.fills,
            price,
            b.size,
            liquidity,
            bar.ts_open,
            decision_id="",
            plan=None,
        )
        self._account.balances.release(b.cid)
        await self._booking.set_status(b.cid, "FILLED")
        self._drop_bracket(meta.product_id)
        left = self._ledger.view(self._book, meta.product_id)
        return ExitResult("partial" if left.is_long else "closed", reason, "", b.size, price, fee)

    def _drop_bracket(self, product_id: str) -> _Bracket | None:
        """Forget the simulated bracket of a product: here and in the tick simulator (a fill it has not produced yet
        never will, one already queued finds no bracket and is dropped by ``settle``)."""
        bracket = self._brackets.pop(product_id, None)
        if bracket is not None and self._tick is not None:
            self._tick.sim.untrack(self._tick.sim.key_of(self._book, product_id))
        return bracket

    # ------------------------------------------------------------------------------------ tick mode

    @property
    def tick_mode(self) -> bool:
        return self._tick is not None

    @property
    def queued_fills(self) -> int:
        return len(self._queue)

    @property
    def tick_stats(self) -> dict[str, int]:
        """Counters of the tick fill mode (all zero in the bar mode)."""
        sim = None if self._tick is None else self._tick.sim
        return {
            "tp_fills": 0 if sim is None else sim.stats.tp_fills, "sl_fills": 0 if sim is None else sim.stats.sl_fills,
            "gaps_resolved": 0 if sim is None else sim.stats.gaps_resolved,
            "gap_fetch_failures": 0 if sim is None else sim.stats.gap_fetch_failures,
            "dropped": self.tick_dropped, "book_errors": self.tick_errors, "queued": len(self._queue),
        }  # fmt: skip

    def _rig(self) -> TickRig:
        if self._tick is None:
            raise RuntimeError(f"book {self._book!r} runs in the bar fill mode: it has no tick feed")
        return self._tick

    def on_trades(self, trades: Sequence[Trade]) -> None:
        """One event of the trade feed (synchronous, in-memory); the fills it produces wait for ``settle``."""
        fresh = [tr for tr in trades if not self._precedes_position(tr)]
        self._queue.extend(self._rig().sim.on_trades(fresh))

    def _precedes_position(self, tr: Trade) -> bool:
        """A trade older than the position it would stop: the channel resends its recent trades as a snapshot at every
        (re)connection, and a restored position must not be stopped by what happened before it was filled."""
        b = self._brackets.get(tr.product_id)
        return b is not None and b.opened_at is not None and tr.ts < b.opened_at

    def on_books(self) -> None:
        """A book update: the stops that wait for liquidity try again."""
        rig = self._rig()
        for book in rig.books.values():
            self._queue.extend(rig.sim.on_book(book))

    def on_damage(self, start: datetime, end: datetime | None) -> None:
        """Trades in ``[start, end]`` may be missing (the callback of the market stream)."""
        self._rig().sim.on_damage(start, end)

    async def resolve_gaps(self) -> None:
        """Close the data gaps whose candles are final; the stops they cause are queued."""
        self._queue.extend(await self._rig().sim.resolve_gaps())

    def _arm_ticks(self, b: _Bracket, opened_at: datetime, bracket_at: datetime, spread_bps: float) -> None:
        """Hand a bracket that was just placed (or restored) to the simulator.

        The trades since ``opened_at`` that the tape has are checked first for the stop: the position was filled at a
        bar that is published minutes later, and the simulator would never see those trades. A print at or below the
        stop closes the position at once at the bar-level stop price from that print (the book of that moment is
        gone). The take profit counts from ``bracket_at`` (the bar after the fill bar, as in the bar mode), which the
        simulator applies itself when the same trades are replayed to it."""
        b.opened_at = opened_at
        if self._tick is None:
            return
        rig, product = self._tick, b.meta.product_id
        key = rig.sim.key_of(self._book, product)
        tape = rig.tape.since(product, opened_at)
        for tr in tape:
            if rig.model.stop_triggered(Decimal(repr(tr.price)), b.sl):
                inc = b.meta.quote_increment
                price = floor_to(Decimal(repr(self._fill.sl_exit(float(b.sl), tr.price, spread_bps))), inc)
                fee = self._fee(price, b.size, "taker")
                self._queue.append(
                    BracketFill(key, self._book, product, "SL", price, b.size, fee, "taker", tr.ts, ZERO)
                )
                return
        rig.sim.track(
            book=self._book, product_id=product, size=b.size, tp=b.tp, sl=b.sl, opened_at=opened_at,
            bracket_at=bracket_at,
        )  # fmt: skip
        if tape:
            self._queue.extend(rig.sim.on_trades(tape))

    async def settle(self) -> list[ExitResult]:
        """Book the fills the simulator produced, in order, and return the bracket exits they are (TP, SL)."""
        return [result for _, result in await self.settle_closed()]

    async def settle_closed(self) -> list[tuple[str, ExitResult]]:
        """``settle`` with the product of every exit, including the ones a bar or an exit booked on the way and that
        nobody has been told about yet (each exit is reported once)."""
        await self._book_queue()
        reported, self._reported = self._reported, []
        return reported

    async def _book_queue(self) -> None:
        """Book everything queued; the bracket exits that result are kept in ``_reported``."""
        if self._tick is None:
            return
        async with self._settle_lock:
            while self._queue:
                fill = self._queue.popleft()
                try:
                    result = await self._book_tick_fill(fill)
                except Exception as exc:  # a ledger failure: counted and logged, the horizon watchdog still covers it
                    self.tick_errors += 1
                    log.error("tick_fill_not_booked", book=self._book, product=fill.product_id, reason=fill.reason,
                              error=f"{type(exc).__name__}: {exc}")  # fmt: skip
                    continue
                if result is not None:
                    self._reported.append((fill.product_id, result))

    async def _book_tick_fill(self, fill: BracketFill) -> ExitResult | None:
        """One ``BracketFill`` into the ledger and the account, through the path of the bar mode (``_book_fill``)."""
        b = self._brackets.get(fill.product_id)
        if b is None:  # an exit (or a bar of a gap catch-up) took the bracket away first: nothing is left to sell
            self.tick_dropped += 1
            log.warning("tick_fill_without_bracket", book=self._book, product=fill.product_id, reason=fill.reason)
            return None
        b.fills += 1
        # the trade of a gap closed by candles says so in its row (the flag is the ledger's, as in a restart catch-up)
        previous = self._ledger.gap_resolution
        self._ledger.gap_resolution = previous or fill.gap_resolved
        try:
            await self._book_fill(
                b.cid, b.meta, "SELL", b.fills, fill.price, fill.size, fill.liquidity, fill.at, decision_id="",
                plan=None, fee=fill.fee,
            )  # fmt: skip
        finally:
            self._ledger.gap_resolution = previous
        b.size -= fill.size
        if fill.closed or b.size <= 0:
            self._account.balances.release(b.cid)
            await self._booking.set_status(b.cid, "FILLED")
            self._drop_bracket(fill.product_id)
        left = self._ledger.view(self._book, fill.product_id)
        return ExitResult("partial" if left.is_long else "closed", fill.reason, "", fill.size, fill.price, fill.fee)

    # ------------------------------------------------------------------------------------ market

    async def on_bar(self, bar: BarEvent, *, gap: bool = False) -> list[ExitResult]:
        """Feed one closed bar. Entries, brackets and exits of the product act in that order; the bracket exits
        (TP, SL) are returned, the entry and the model exits resolve their pending ``submit_*`` call. Callers feed
        bars one at a time (await each call).

        In the tick fill mode the bracket is not looked at by bars, the market trades did it (their fills are booked
        here first and again at the end, ``settle``) unless ``gap``: the bars of a downtime, when no trade was seen."""
        product = bar.product_id
        self._last_bar[product] = bar
        await self._book_queue()
        closed: list[ExitResult] = []
        entry = self._entries.get(product)
        if entry is not None:
            await self._entry_step(entry, bar)
        bracket = self._brackets.get(product)
        if bracket is not None and (self._tick is None or gap):
            done = await self._bracket_step(bracket, bar)
            if done is not None:
                closed.append(done)
        exit_ = self._exits.get(product)
        if exit_ is not None:
            await self._exit_step(exit_, bar)
        await self._book_queue()
        mine = [r for p, r in self._reported if p == product]  # the exits of the other products wait for their caller
        self._reported = [(p, r) for p, r in self._reported if p != product]
        return mine + closed
