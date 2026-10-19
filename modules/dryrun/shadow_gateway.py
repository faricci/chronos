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
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final, Literal

import structlog

from modules.dryrun.fill_model import BarFillModel
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
from modules.risk.risk_manager import ExitIntent, OrderIntent

__all__ = ["BarEvent", "RestoreReport", "ShadowExecutionGateway"]

log = structlog.get_logger(__name__)

ZERO: Final = Decimal(0)
BPS: Final = Decimal(10_000)


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
    ) -> Decimal:
        """Apply one simulated fill (VirtualAccount balances, ledger via LedgerBooking). Returns the fee."""
        fee = self._fee(price, size, liquidity)

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
        self._brackets[meta.product_id] = _Bracket(
            spec.client_order_id, meta, size, intent.bracket.take_profit, intent.bracket.stop_loss, bar.ts_open
        )
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
                spec = OrderSpec.model_validate_json(alive.payload or "{}")
                self._brackets[product] = _Bracket(
                    alive.client_order_id, meta, spec.base_size, pos.tp, pos.sl, pos.opened_at or self._clock()
                )  # fmt: skip
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
        self._brackets[product] = _Bracket(cid, meta, size, pos.tp, pos.sl, now)
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
        pending = self._entries.get(product_id)
        if pending is not None:  # a half-filled entry is cancelled and gets no bracket
            pending.abort_bracket = True
            await self._cancel_entry(pending)
        position = self._ledger.view(self._book, product_id)
        if not position.is_long:
            return ExitResult("nothing_to_do", reason)
        # X1-X3: cancel the bracket; in a simulation the confirmation is immediate, and only then is its base free
        bracket = self._brackets.pop(product_id, None)
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
        fee = await self._book_fill(
            b.cid,
            meta,
            "SELL",
            1,
            price,
            b.size,
            liquidity,
            bar.ts_open,
            decision_id="",
            plan=None,
        )
        self._account.balances.release(b.cid)
        await self._booking.set_status(b.cid, "FILLED")
        self._brackets.pop(meta.product_id, None)
        left = self._ledger.view(self._book, meta.product_id)
        return ExitResult("partial" if left.is_long else "closed", reason, "", b.size, price, fee)

    # ------------------------------------------------------------------------------------ market

    async def on_bar(self, bar: BarEvent) -> list[ExitResult]:
        """Feed one closed bar. Entries, brackets and exits of the product act in that order; the bracket exits
        (TP, SL) are returned, the entry and the model exits resolve their pending ``submit_*`` call. Callers feed
        bars one at a time (await each call)."""
        product = bar.product_id
        self._last_bar[product] = bar
        closed: list[ExitResult] = []
        entry = self._entries.get(product)
        if entry is not None:
            await self._entry_step(entry, bar)
        bracket = self._brackets.get(product)
        if bracket is not None and (done := await self._bracket_step(bracket, bar)) is not None:
            closed.append(done)
        exit_ = self._exits.get(product)
        if exit_ is not None:
            await self._exit_step(exit_, bar)
        return closed
