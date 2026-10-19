"""Execution gateway: the life cycle of the real orders (spec sez. 4.1, 4.1.6, 4.3 punto 7; vincolo C11).

``GatewayContract`` (exported also as ``ExecutionGateway``) is the interface of both gateways; ``LiveExecutionGateway``
implements it on Coinbase and ``modules/dryrun/shadow_gateway.py`` implements it on simulated fills. Everything that
does not depend on the venue (order specs, sellable size, ledger bookkeeping of fills and dust) is defined once here
and used by both, so live and dry cannot drift apart.

Constraint C11, as wired in this file:

* every order, without exception, is born from ``guard.authorize(spec)``, which verifies the real spot balance and
  reserves it in one synchronous step; the resulting ticket is the only thing ``exchange.create_order`` accepts;
* a refusal of the guard is a typed result (``refused`` + reason), never an unhandled exception and never retried;
* nothing is released optimistically: a reservation returns to ``free`` only when the exchange confirms a terminal
  state (filled, cancelled, expired, failed) or definitively rejects the order;
* ``OutcomeUnknown`` (ambiguous write): the reservation STAYS, the order is marked ``UNKNOWN`` in the ledger and
  nothing is re-sent; the reconciler finds the order by its deterministic ``client_order_id`` and decides;
* the bracket is always STANDALONE, placed after the entry is terminal, sized on what was really filled and owned
  (never an attached bracket, sez. 4.1.6); a fill below the minimum sellable size becomes dust;
* an exit cancels the bracket, waits for the terminal state of the cancel, and only then sells.

``LiveExecutionGateway`` cannot be built by accident: the constructor repeats the mode check of the spec
(``resolve_mode``: ``execution.mode == "live"`` AND ``CHRONOS_LIVE_CONFIRM=I_ACCEPT_REAL_ORDERS``) and refuses a
read-only exchange, a virtual balance book and a dry ledger.

``bracket_order_id`` of a position holds the ``client_order_id`` of its bracket (as in the backtest gateway); the
exchange id is found in the ``orders`` table of the ledger.

Entry context. The first fill of an entry can reach the ledger from the user stream before this gateway sees it, and
the position it opens must already carry the bracket levels (otherwise the watchdog finds a position "without bracket
and without SL"). So the plan of every entry (decision id, tp, sl, k_tp, k_sl, atr) is remembered under the entry's
``client_order_id`` BEFORE the order is sent (``entry_context``, the ``EntryContextSource`` of the stream) and is
written in the ``payload`` of the order record, so that ``restore_entry_contexts`` rebuilds it after a restart
(call it at startup, before the stream and the reconciler start). Only entries still open are restored.

The reconciler never creates orders: ``place_missing_bracket`` is its ``BracketPlacer``, so a bracket recreated at
startup is authorized by the guard exactly like any other order.

A fill and the position it changes are written in ONE ledger transaction (``LedgerBooking.book_fill``).
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

import structlog

from modules.config import ExecutionConfig, resolve_mode
from modules.execution.balance import BalanceBook, Clock, CoverageAlarm
from modules.execution.balance_guard import BalanceGuard, CoverageRefused
from modules.execution.exchange import (
    CoinbaseExchange,
    DryRunViolation,
    ExchangeAuthError,
    ExchangeError,
    FillInfo,
    OrderInfo,
    OrderRejected,
    OutcomeUnknown,
)
from modules.execution.ids import client_order_id
from modules.execution.orders import OrderSpec, ProductMeta, Side
from modules.execution.rounding import floor_to
from modules.execution.spot import SpotRegistry, SpotViolation
from modules.execution.user_stream import EntryContext
from modules.ledger.positions import EntryPlan, FillRecord, OrderRecord, PositionLedger, PositionView
from modules.risk.risk_manager import ExitIntent, OrderIntent

if TYPE_CHECKING:
    from modules.execution.reconciler import BracketRequest

__all__ = [
    "AlarmSink",
    "BracketNotPlaced",
    "BracketStatus",
    "CancelOutcome",
    "EntryContexts",
    "EntryResult",
    "EntryStatus",
    "ExecutionGateway",
    "ExitResult",
    "ExitStatus",
    "FillEvent",
    "GatewayContract",
    "LedgerBooking",
    "LiveExecutionGateway",
    "LiveModeNotConfirmed",
    "bracket_spec",
    "entry_spec",
    "exit_spec",
    "is_sellable",
    "sellable_base",
]

log = structlog.get_logger(__name__)

ZERO: Final = Decimal(0)

EntryStatus = Literal[
    "filled",  # filled > 0 (a partial fill included): the position exists, see ``bracket``
    "no_fill",  # terminal without any fill
    "refused",  # the BalanceGuard refused (reason: insufficient_balance | balance_stale | kill_switch)
    "rejected",  # the exchange definitively refused the order (nothing is open)
    "unknown",  # ambiguous write: reservation kept, the reconciler decides
    "unconfirmed",  # the cancel of the residual was not confirmed in time: reservation kept
    "blocked",  # an entry is already pending for the product, or the book already holds a position
]
BracketStatus = Literal["none", "placed", "dust", "skipped", "refused", "rejected", "unknown"]
ExitStatus = Literal[
    "closed",  # the position is FLAT
    "closed_by_bracket",  # the bracket filled while it was being cancelled: nothing was sold
    "partial",  # some base is still held (market sell not fully filled)
    "nothing_to_do",  # no LONG position
    "refused",  # the BalanceGuard refused a sell
    "rejected",  # the exchange definitively refused the sell
    "unknown",  # ambiguous write: reservation kept, the reconciler decides
    "unconfirmed",  # a cancel or a sell is not confirmed terminal yet: reservation kept, retry later
]


class LiveModeNotConfirmed(Exception):
    """A ``LiveExecutionGateway`` was requested without the two acts the spec requires (sez. 5.1.1 punto 4)."""


class BracketNotPlaced(Exception):
    """``place_missing_bracket`` could not get a bracket on the exchange. ``status`` is ``refused`` (the guard),
    ``rejected`` (the exchange), ``unknown`` (ambiguous write: the reservation stays, the reconciler decides)."""

    def __init__(self, status: str, reason: str, detail: str = "") -> None:
        super().__init__(f"{status}: {reason} {detail}".strip())
        self.status = status
        self.reason = reason


# ------------------------------------------------------------------------------------------------ results


@dataclass(frozen=True, slots=True)
class EntryResult:
    status: EntryStatus
    client_order_id: str
    reason: str = ""
    detail: str = ""
    filled_size: Decimal = ZERO
    avg_price: Decimal | None = None
    fees: Decimal = ZERO
    bracket: BracketStatus = "none"
    bracket_client_order_id: str | None = None
    dust_merged: bool = False


@dataclass(frozen=True, slots=True)
class ExitResult:
    status: ExitStatus
    reason: str = ""  # why the exit was asked (SELL, HORIZON, WATCHDOG, ...): passed through untouched
    detail: str = ""
    size: Decimal = ZERO
    avg_price: Decimal | None = None
    fees: Decimal = ZERO


@dataclass(frozen=True, slots=True)
class CancelOutcome:
    status: Literal["nothing_pending", "done"]
    entry: EntryResult | None = None


class GatewayContract(Protocol):
    """What the rest of the system may ask of an execution gateway (spec sez. 4.3 punto 7).

    Both calls return when the sequence is over (entry terminal and bracket placed or dust; exit confirmed), so the
    caller runs them as tasks. Nothing here sleeps in the shadow implementation: it is driven by market events.
    """

    async def submit_entry_with_bracket(self, intent: OrderIntent) -> EntryResult: ...

    async def submit_exit(self, intent: ExitIntent) -> ExitResult: ...

    async def emergency_exit(self, product_id: str) -> ExitResult: ...

    async def cancel(self, product_id: str) -> CancelOutcome: ...


# The tripwire of tests/test_c11_wiring.py (T18) inspects every class whose name ends with ``ExecutionGateway`` and
# wants a mandatory ``guard`` and a call to ``authorize``: that is the rule for concrete gateways. The interface has
# no constructor, so it carries a neutral class name and the public name is an alias.
ExecutionGateway = GatewayContract


# --------------------------------------------------------------------------------------- shared helpers


def entry_spec(intent: OrderIntent) -> OrderSpec:
    return OrderSpec(
        client_order_id=client_order_id(intent.decision_id, "entry"),
        book=intent.book,
        product_id=intent.product_id,
        side="BUY",
        kind="limit",
        base_size=intent.base_size,
        limit_price=intent.entry.limit_price,
        post_only=intent.entry.type == "limit_post_only",
    )


def bracket_spec(intent: OrderIntent, base_size: Decimal) -> OrderSpec:
    return OrderSpec(
        client_order_id=client_order_id(intent.decision_id, "bracket"),
        book=intent.book,
        product_id=intent.product_id,
        side="SELL",
        kind="bracket",
        base_size=base_size,
        limit_price=intent.bracket.take_profit,
        stop_trigger_price=intent.bracket.stop_loss,
    )


def exit_spec(
    cid: str,
    book: str,
    product_id: str,
    base_size: Decimal,
    *,
    kind: Literal["limit", "market"],
    limit_price: Decimal | None = None,
) -> OrderSpec:
    """A SELL of base the book owns: a marketable limit (``limit_price`` = best bid) or a market IOC."""
    return OrderSpec(
        client_order_id=cid,
        book=book,
        product_id=product_id,
        side="SELL",
        kind=kind,
        base_size=base_size,
        limit_price=limit_price,
    )


def sellable_base(position: PositionView, balances: BalanceBook, meta: ProductMeta) -> Decimal:
    """Base the book may sell now: the guard's formula, floored to the increment. Zero when nothing is sellable."""
    owned = position.base_qty - balances.reserved_sell_base(meta.product_id)
    return max(floor_to(min(owned, balances.free(meta.base_currency)), meta.base_increment), ZERO)


def is_sellable(size: Decimal, price: Decimal, meta: ProductMeta) -> bool:
    """``min_sell_size`` of sez. 4.1.6: ``size >= base_min_size`` and ``size * price >= quote_min_size``."""
    return size > 0 and size >= meta.base_min_size and size * price >= meta.quote_min_size


@dataclass(frozen=True, slots=True)
class FillEvent:
    """One fill of one order, from the exchange (live) or from the fill model (shadow)."""

    trade_id: str
    client_order_id: str
    product_id: str
    side: Side
    price: Decimal
    size: Decimal
    fee: Decimal
    liquidity: str
    time: datetime


BalanceApplier = Callable[[FillEvent], list[CoverageAlarm]]
AlarmSink = Callable[[CoverageAlarm], None]


def _log_alarm(alarm: CoverageAlarm) -> None:
    log.error("coverage_alarm", kind=alarm.kind, currency=alarm.currency, detail=alarm.detail)


def entry_payload(spec: OrderSpec, context: EntryContext | None) -> str:
    """The ``payload`` column of an order: the spec as JSON and, for an entry, its plan (see the module docstring)."""
    data: dict[str, Any] = spec.model_dump(mode="json")
    if context is not None:
        plan = context.plan
        data["entry_plan"] = {"decision_id": context.decision_id} | (
            {} if plan is None else
            {"tp": str(plan.tp), "sl": str(plan.sl), "k_tp": plan.k_tp, "k_sl": plan.k_sl, "atr": str(plan.atr)}
        )  # fmt: skip
    return json.dumps(data, sort_keys=True)


def context_from_payload(payload: str | None) -> EntryContext | None:
    """Inverse of ``entry_payload``; ``None`` if the payload carries no (readable) entry plan."""
    if not payload:
        return None
    try:
        entry = json.loads(payload).get("entry_plan")
        if not isinstance(entry, dict):
            return None
        decision_id = str(entry["decision_id"])
        if "tp" not in entry:
            return EntryContext(decision_id, None)
        plan = EntryPlan(
            Decimal(entry["tp"]),
            Decimal(entry["sl"]),
            float(entry["k_tp"]),
            float(entry["k_sl"]),
            Decimal(entry["atr"]),
        )
    except (ValueError, KeyError, TypeError, AttributeError, InvalidOperation):
        log.error("entry_plan_unreadable")
        return None
    return EntryContext(decision_id, plan)


class EntryContexts:
    """``client_order_id`` -> :class:`EntryContext`, in memory (the stream asks synchronously); persisted too."""

    def __init__(self) -> None:
        self._by_cid: dict[str, EntryContext] = {}

    def remember(self, client_order_id: str, context: EntryContext) -> None:
        self._by_cid[client_order_id] = context

    def forget(self, client_order_id: str) -> None:
        self._by_cid.pop(client_order_id, None)

    def get(self, client_order_id: str) -> EntryContext | None:
        return self._by_cid.get(client_order_id)

    async def restore(self, ledger: PositionLedger, book: str) -> int:
        """Reload the plans of the entries still open in the ledger (after a restart). Returns how many."""
        n = 0
        for rec in await ledger.open_orders(book):
            ctx = context_from_payload(rec.payload) if rec.side == "BUY" else None
            if ctx is not None:
                self._by_cid[rec.client_order_id] = ctx
                n += 1
        return n


class LedgerBooking:
    """The ledger side of an order's life, identical in live and shadow.

    A fill is stored together with the position it changes in ONE transaction (a crash cannot leave one without the
    other), then applied to the balances through ``apply_balances`` (the live ``BalanceBook`` or the
    ``VirtualAccount``). A fill already booked by another path (WS and polling both deliver it) changes nothing.
    """

    def __init__(self, book: str, ledger: PositionLedger, clock: Clock, on_alarm: AlarmSink | None = None) -> None:
        self._book = book
        self._ledger = ledger
        self._clock = clock
        self._alarm = on_alarm or _log_alarm

    async def open_order(
        self,
        spec: OrderSpec,
        *,
        leg: str,
        intent_id: str | None,
        status: str = "NEW",
        context: EntryContext | None = None,
    ) -> None:
        now = self._clock()
        await self._ledger.record_order(
            OrderRecord(
                client_order_id=spec.client_order_id,
                book=self._book,
                product_id=spec.product_id,
                side=spec.side,
                leg=leg,
                status=status,
                created_at=now,
                updated_at=now,
                intent_id=intent_id,
                payload=entry_payload(spec, context),
            )
        )

    async def set_status(self, cid: str, status: str, exchange_order_id: str | None = None) -> None:
        await self._ledger.update_order(cid, status, self._clock(), exchange_order_id)

    async def book_fill(
        self, fill: FillEvent, apply_balances: BalanceApplier, *, decision_id: str, plan: EntryPlan | None
    ) -> bool:
        """Apply one fill. Returns False if it had already been booked."""
        record = FillRecord(
            trade_id=fill.trade_id,
            book=self._book,
            client_order_id=fill.client_order_id,
            product_id=fill.product_id,
            side=fill.side,
            price=fill.price,
            size=fill.size,
            fee=fill.fee,
            liquidity=fill.liquidity,
            trade_time=fill.time,
        )
        if fill.side == "BUY":
            stored = await self._ledger.apply_entry_fill_recorded(record, decision_id, plan) is not None
        else:  # a SELL never takes the position below zero: the excess is an alarm, not a short
            qty = min(fill.size, self._ledger.view(self._book, fill.product_id).base_qty)
            if qty > 0:
                stored = await self._ledger.apply_exit_fill_recorded(record, qty) is not None
            else:
                stored = await self._ledger.record_fill(record)
            if stored and qty < fill.size:
                detail = f"SELL fill {fill.trade_id} of {fill.size} > ledger position {qty}"
                self._alarm(
                    CoverageAlarm(kind="reservation_overdraw", currency=fill.product_id.split("-")[0], detail=detail)
                )
        if not stored:
            return False
        for alarm in apply_balances(fill):
            self._alarm(alarm)
        return True

    async def to_dust(self, product_id: str, qty: Decimal, cost: Decimal) -> None:
        """A fill below the minimum sellable size: the book goes back to FLAT and remembers the base as dust.

        Not a sale: the base stays in the account (and in the balances); ``cost`` is the quote spent, fees included."""
        await self._ledger.apply_exit_fill(self._book, product_id, qty)
        await self._ledger.set_dust(self._book, product_id, qty, cost)


# ----------------------------------------------------------------------------------------------- live


@dataclass(slots=True)
class _PendingEntry:
    intent: OrderIntent
    spec: OrderSpec
    meta: ProductMeta
    plan: EntryPlan
    prev_dust_qty: Decimal
    prev_dust_cost: Decimal
    order_id: str | None = None
    fills: dict[str, FillInfo] = field(default_factory=dict)
    cancel_requested: bool = False
    abort_bracket: bool = False
    result: EntryResult | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def filled(self) -> Decimal:
        return sum((f.size for f in self.fills.values()), ZERO)

    @property
    def cost(self) -> Decimal:
        return sum((f.price * f.size + f.fee for f in self.fills.values()), ZERO)

    @property
    def fees(self) -> Decimal:
        return sum((f.fee for f in self.fills.values()), ZERO)

    @property
    def avg_price(self) -> Decimal | None:
        filled = self.filled
        return None if filled <= 0 else sum((f.price * f.size for f in self.fills.values()), ZERO) / filled


@dataclass(frozen=True, slots=True)
class _Sent:
    status: Literal["open", "refused", "rejected", "unknown", "duplicate"]
    order_id: str | None = None
    reason: str = ""
    detail: str = ""


@dataclass(slots=True)
class _Tally:
    size: Decimal = ZERO
    gross: Decimal = ZERO
    fees: Decimal = ZERO

    def add(self, fills: list[FillInfo]) -> None:
        for f in fills:
            self.size += f.size
            self.gross += f.price * f.size
            self.fees += f.fee

    @property
    def avg_price(self) -> Decimal | None:
        return None if self.size <= 0 else self.gross / self.size


class LiveExecutionGateway:
    def __init__(
        self,
        guard: BalanceGuard,
        exchange: CoinbaseExchange,
        ledger: PositionLedger,
        registry: SpotRegistry,
        clock: Clock,
        *,
        book: str,
        balances: BalanceBook,
        execution: ExecutionConfig,
        env: Mapping[str, str] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_alarm: AlarmSink | None = None,
        confirm_poll_s: float = 1.0,
        max_cancel_rounds: int = 6,
        settle_attempts: int = 3,
    ) -> None:
        if not isinstance(guard, BalanceGuard):  # a look-alike or None is not the coverage circuit
            raise TypeError("LiveExecutionGateway needs a BalanceGuard")
        # the mode check of the spec: both acts, or no live gateway
        if resolve_mode(execution, os.environ if env is None else env) != "live":
            raise LiveModeNotConfirmed(
                "LiveExecutionGateway needs execution.mode == 'live' AND CHRONOS_LIVE_CONFIRM=I_ACCEPT_REAL_ORDERS"
            )
        if exchange.read_only:
            raise LiveModeNotConfirmed("a live gateway cannot run on a read-only exchange")
        if ledger.mode != "live":
            raise LiveModeNotConfirmed(f"a live gateway needs the live ledger, got the {ledger.mode!r} one")
        if balances.source != "exchange" or balances.book != book:
            raise ValueError("a live gateway needs the exchange-sourced BalanceBook of its own book")
        self._guard = guard  # mandatory and typed: there is no gateway without the coverage circuit
        self._exchange = exchange
        self._ledger = ledger
        self._registry = registry
        self._clock = clock
        self._book = book
        self._balances = balances
        self._cfg = execution
        self._sleep = sleep
        self._booking = LedgerBooking(book, ledger, clock, on_alarm)
        self._confirm_poll_s = confirm_poll_s
        self._max_cancel_rounds = max_cancel_rounds
        self._settle_attempts = settle_attempts
        self._pending: dict[str, _PendingEntry] = {}
        self._exit_locks: dict[str, asyncio.Lock] = {}
        self._contexts = EntryContexts()

    # ---------------------------------------------------------------------------- order plumbing

    async def _send(
        self, spec: OrderSpec, *, leg: str, intent_id: str | None, context: EntryContext | None = None
    ) -> _Sent:
        """authorize -> record -> create_order. Every order of this gateway goes through here."""
        cid = spec.client_order_id
        if self._balances.reservation(cid) is not None:  # same id still unresolved: never a second order
            return _Sent("duplicate", None, "order_already_known", f"{cid} still holds a reservation")
        try:
            ticket = self._guard.authorize(spec)
        except CoverageRefused as exc:
            log.info("coverage_refused", client_order_id=cid, side=spec.side, kind=spec.kind, reason=exc.reason)
            return _Sent("refused", None, exc.reason, str(exc))
        try:
            await self._booking.open_order(spec, leg=leg, intent_id=intent_id, context=context)
        except BaseException:
            self._balances.release(cid)  # nothing was sent
            raise
        try:
            created = await self._exchange.create_order(ticket)
        except OrderRejected as exc:  # definitive: nothing is open
            self._balances.release(cid)
            await self._booking.set_status(cid, "REJECTED")
            return _Sent("rejected", None, exc.reason, exc.message)
        except OutcomeUnknown as exc:  # ambiguous: keep the reservation, never resend
            await self._booking.set_status(cid, "UNKNOWN")
            return _Sent("unknown", None, "outcome_unknown", str(exc))
        except (ExchangeAuthError, DryRunViolation, SpotViolation):  # refused before anything could open
            self._balances.release(cid)
            await self._booking.set_status(cid, "FAILED")
            raise
        except asyncio.CancelledError:  # the request may be in flight
            await self._booking.set_status(cid, "UNKNOWN")
            raise
        except Exception as exc:  # unexpected: treat as ambiguous, the order may exist
            log.exception("create_order_unexpected_failure", client_order_id=cid)
            await self._booking.set_status(cid, "UNKNOWN")
            return _Sent("unknown", None, "unexpected_error", repr(exc))
        await self._booking.set_status(cid, "OPEN", created.order_id)
        return _Sent("open", created.order_id)

    async def _poll_order(self, order_id: str) -> OrderInfo | None:
        try:
            return await self._exchange.get_order(order_id)
        except ExchangeAuthError:
            raise
        except ExchangeError as exc:  # not found yet, unavailable, odd answer: ask again at the next poll
            log.warning("get_order_failed", order_id=order_id, error=str(exc))
            return None

    async def _wait_terminal(
        self,
        order_id: str,
        *,
        timeout_s: float,
        stop: Callable[[], bool] = lambda: False,
        on_info: Callable[[OrderInfo], Awaitable[None]] | None = None,
    ) -> OrderInfo | None:
        """Poll until terminal. Returns the last info seen (terminal or not) when time is up or ``stop`` is true."""
        deadline = self._clock() + timedelta(seconds=timeout_s)
        last: OrderInfo | None = None
        while True:
            info = await self._poll_order(order_id)
            if info is not None:
                last = info
                if on_info is not None:
                    await on_info(info)
                if info.is_terminal:
                    return info
            if stop() or self._clock() >= deadline:
                return last
            await self._sleep(self._cfg.entry_poll_s)

    async def _cancel_order(self, order_id: str) -> OrderInfo | None:
        """Cancel and wait for the terminal state. ``None`` = not confirmed: the reservation must stay."""
        for attempt in range(1, self._max_cancel_rounds + 1):
            try:
                await self._exchange.cancel_orders([order_id])
            except OutcomeUnknown as exc:  # the poll below tells what happened
                log.warning("cancel_outcome_unknown", order_id=order_id, error=str(exc))
            deadline = self._clock() + timedelta(seconds=self._cfg.cancel_confirm_timeout_s)
            while True:
                info = await self._poll_order(order_id)
                if info is not None and info.is_terminal:
                    return info
                if self._clock() >= deadline:
                    break
                await self._sleep(self._confirm_poll_s)
            if attempt == 3:
                log.error("cancel_not_confirmed", order_id=order_id, attempts=attempt)
        return None

    def _applier(self, meta: ProductMeta) -> BalanceApplier:
        def apply(fill: FillEvent) -> list[CoverageAlarm]:
            return self._balances.apply_fill(
                fill.client_order_id,
                base_currency=meta.base_currency,
                quote_currency=meta.quote_currency,
                price=fill.price,
                size=fill.size,
                fee=fill.fee,
                trade_id=fill.trade_id,
            )

        return apply

    async def _sync_fills(
        self, cid: str, order_id: str, side: Side, meta: ProductMeta, *, decision_id: str, plan: EntryPlan | None
    ) -> list[FillInfo]:
        """Book every fill of the order not booked yet; returns ALL the fills the exchange lists for it."""
        fills = sorted(await self._exchange.get_fills(order_id), key=lambda f: f.trade_time)
        for f in fills:
            await self._booking.book_fill(
                FillEvent(f.trade_id, cid, f.product_id, side, f.price, f.size, f.fee, f.liquidity, f.trade_time),
                self._applier(meta),
                decision_id=decision_id,
                plan=plan,
            )
        return fills

    async def _best_bid(self, product_id: str) -> Decimal | None:
        try:
            return (await self._exchange.get_best_bid_ask(product_id)).bid
        except ExchangeAuthError:
            raise
        except ExchangeError as exc:
            log.warning("best_bid_unavailable", product_id=product_id, error=str(exc))
            return None

    # ------------------------------------------------------------------------------------- entry

    async def submit_entry_with_bracket(self, intent: OrderIntent) -> EntryResult:
        if intent.book != self._book:
            raise ValueError(f"intent for book {intent.book!r} sent to the gateway of {self._book!r}")
        spec = entry_spec(intent)
        cid, product = spec.client_order_id, intent.product_id
        meta = self._registry.meta(product)
        position = self._ledger.view(self._book, product)
        if product in self._pending:
            return EntryResult("blocked", cid, reason="entry_already_pending")
        if position.is_long:
            return EntryResult("blocked", cid, reason="position_open")
        plan = EntryPlan(
            intent.bracket.take_profit, intent.bracket.stop_loss, intent.bracket.k_tp, intent.bracket.k_sl,
            intent.bracket.atr,
        )  # fmt: skip
        p = _PendingEntry(intent, spec, meta, plan, position.dust_qty, position.dust_cost)
        self._pending[product] = p  # registered before any await: one pending entry per (book, product)
        # the plan is known to the stream BEFORE the order exists: its first fill may arrive before we look
        self._contexts.remember(cid, EntryContext(intent.decision_id, plan))
        try:
            p.result = await self._run_entry(p)
            if p.result.status in ("refused", "rejected", "blocked"):
                self._contexts.forget(cid)  # nothing was opened under this id
            return p.result
        finally:
            self._pending.pop(product, None)
            p.done.set()

    async def _run_entry(self, p: _PendingEntry) -> EntryResult:
        cid = p.spec.client_order_id
        context = EntryContext(p.intent.decision_id, p.plan)
        sent = await self._send(p.spec, leg="entry", intent_id=p.intent.intent_id, context=context)
        if sent.status != "open":
            return EntryResult("blocked" if sent.status == "duplicate" else sent.status, cid, sent.reason, sent.detail)
        assert sent.order_id is not None
        p.order_id = sent.order_id
        timeout_s = float(p.intent.entry.time_in_force_s)

        async def on_info(info: OrderInfo) -> None:
            if info.filled_size > p.filled:  # the first fill already makes the position LONG
                await self._sync_entry_fills(p)

        info = await self._wait_terminal(
            sent.order_id, timeout_s=timeout_s, stop=lambda: p.cancel_requested, on_info=on_info
        )
        if info is None or not info.is_terminal:  # timeout or cancel request: cancel the residual, confirm
            info = await self._cancel_order(sent.order_id)
        if info is None:
            log.error("entry_cancel_unconfirmed", client_order_id=cid)
            return EntryResult("unconfirmed", cid, reason="cancel_unconfirmed", filled_size=p.filled)
        return await self._conclude_entry(p, info)

    async def _sync_entry_fills(self, p: _PendingEntry) -> None:
        assert p.order_id is not None
        fills = await self._sync_fills(
            p.spec.client_order_id, p.order_id, "BUY", p.meta, decision_id=p.intent.decision_id, plan=p.plan
        )
        p.fills = {f.trade_id: f for f in fills}

    async def _conclude_entry(self, p: _PendingEntry, info: OrderInfo) -> EntryResult:
        """The entry is terminal: settle the fills, release the residual reservation, then bracket or dust."""
        cid = p.spec.client_order_id
        for attempt in range(self._settle_attempts):  # the fills can lag behind the order status
            try:
                await self._sync_entry_fills(p)
            except ExchangeAuthError:
                raise
            except ExchangeError as exc:
                log.warning("entry_fills_unavailable", client_order_id=cid, error=str(exc))
            if p.filled >= info.filled_size:
                break
            if attempt + 1 < self._settle_attempts:
                await self._sleep(self._confirm_poll_s)
        else:
            log.error("entry_fills_incomplete", client_order_id=cid, listed=str(p.filled), order=str(info.filled_size))
        self._balances.release(cid)  # only now: the exchange confirmed the terminal state
        await self._booking.set_status(cid, info.status)
        if p.filled <= 0:
            return EntryResult("no_fill", cid, reason=info.status.lower())
        return await self._protect(p)

    async def _protect(self, p: _PendingEntry) -> EntryResult:
        """E4 of sez. 4.1: standalone bracket on what was filled and is owned, or dust."""
        intent, meta = p.intent, p.meta
        cid = p.spec.client_order_id

        def filled(
            bracket: BracketStatus, reason: str = "", detail: str = "", bracket_cid: str | None = None
        ) -> EntryResult:
            return EntryResult(
                "filled", cid, reason, detail, p.filled, p.avg_price, p.fees, bracket, bracket_cid, p.prev_dust_qty > 0
            )

        if p.abort_bracket:
            return filled("skipped")
        position = self._ledger.view(self._book, intent.product_id)
        if not position.is_long:  # the fills are on the exchange but the ledger does not show them: reconciler
            log.error("entry_filled_but_position_not_recorded", client_order_id=cid)
            return filled("none", "position_not_recorded")
        size = sellable_base(position, self._balances, meta)
        reference = await self._best_bid(intent.product_id) or p.avg_price or intent.entry.limit_price
        if not is_sellable(size, reference, meta):
            await self._booking.to_dust(intent.product_id, position.base_qty, p.cost + p.prev_dust_cost)
            log.info("entry_below_min_sell_size_is_dust", client_order_id=cid, qty=str(position.base_qty))
            return filled("dust")
        spec = bracket_spec(intent, size)
        sent = await self._send(spec, leg="bracket", intent_id=intent.intent_id)
        bracket_cid = spec.client_order_id
        if sent.status == "open":
            await self._ledger.set_bracket(
                self._book, intent.product_id, bracket_cid, intent.bracket.take_profit, intent.bracket.stop_loss
            )
            return filled("placed", bracket_cid=bracket_cid)
        # refused / rejected / unknown: the position stays LONG without a bracket; the watchdog stops it at the SL
        log.error("bracket_not_placed", client_order_id=bracket_cid, outcome=sent.status, reason=sent.reason)
        return filled("unknown" if sent.status == "duplicate" else sent.status, sent.reason, sent.detail, bracket_cid)

    # -------------------------------------------------------------------------------------- cancel

    def entry_context(self, client_order_id: str) -> EntryContext | None:
        """The plan of an entry we sent (``EntryContextSource`` of the user stream)."""
        return self._contexts.get(client_order_id)

    async def restore_entry_contexts(self) -> int:
        """Startup: rebuild the plans of the entries still open from the ledger. Returns how many were restored."""
        return await self._contexts.restore(self._ledger, self._book)

    async def place_missing_bracket(self, request: BracketRequest) -> None:
        """``BracketPlacer`` of the reconciler: a standalone bracket for a LONG position found without one.

        Same circuit as every order: guard, record, ticket, exchange. Raises :class:`BracketNotPlaced` if no bracket
        is on the exchange afterwards (the reconciler logs it and the watchdog covers the position)."""
        if request.book != self._book:
            raise ValueError(f"bracket request for book {request.book!r} sent to the gateway of {self._book!r}")
        product = request.product_id
        meta = self._registry.meta(product)
        async with self._exit_locks.setdefault(product, asyncio.Lock()):  # not while an exit is under way
            position = self._ledger.view(self._book, product)
            if product in self._pending or not position.is_long or position.bracket_order_id is not None:
                return  # an entry owns the position's life cycle, or there is nothing to protect / already protected
            size = min(floor_to(request.base_size, meta.base_increment), sellable_base(position, self._balances, meta))
            if size <= 0:
                raise BracketNotPlaced("refused", "insufficient_balance", "no base is sellable")
            decision_id = position.entry_decision_id or product
            n = 0
            while True:  # a bracket id used before (cancelled, rejected) is never reused
                cid = client_order_id(decision_id, "bracket" if n == 0 else f"bracket-r{n}")
                if await self._ledger.get_order(cid) is None:
                    break
                n += 1
            spec = OrderSpec(
                client_order_id=cid, book=self._book, product_id=product, side="SELL", kind="bracket", base_size=size,
                limit_price=request.take_profit, stop_trigger_price=request.stop_loss,
            )  # fmt: skip
            sent = await self._send(spec, leg="bracket", intent_id=None)
            if sent.status != "open":
                raise BracketNotPlaced(sent.status, sent.reason, sent.detail)
            await self._ledger.set_bracket(self._book, product, cid, request.take_profit, request.stop_loss)

    def has_pending_entry(self, product_id: str) -> bool:
        return product_id in self._pending

    async def cancel(self, product_id: str) -> CancelOutcome:
        """Cancel the pending entry of the product. The entry sequence itself cancels the residual, waits for the
        terminal state, and then follows the normal path for a partial fill (bracket or dust)."""
        p = self._pending.get(product_id)
        if p is None:
            return CancelOutcome("nothing_pending")
        p.cancel_requested = True
        await p.done.wait()
        return CancelOutcome("done", p.result)

    async def sync_order(self, client_order_id: str) -> OrderInfo | None:
        """Book what the exchange did with an open SELL order of this book (a bracket that hit TP or SL, an exit
        order): its new fills and, if it is terminal, the release of its reservation. Idempotent; the entry order has
        its own sequence. Returns the order as the exchange reports it, or ``None`` if there is nothing to sync."""
        record = next(
            (r for r in await self._ledger.open_orders(self._book) if r.client_order_id == client_order_id), None
        )
        if record is None or record.side != "SELL" or record.exchange_order_id is None:
            return None
        info = await self._poll_order(record.exchange_order_id)
        if info is None:
            return None
        meta = self._registry.meta(record.product_id)
        if info.filled_size > 0:
            await self._sync_fills(
                client_order_id, record.exchange_order_id, "SELL", meta, decision_id=record.intent_id or "", plan=None
            )
        if info.is_terminal:
            self._balances.release(client_order_id)
            await self._booking.set_status(client_order_id, info.status)
            if self._ledger.view(self._book, meta.product_id).bracket_order_id == client_order_id:
                await self._ledger.set_bracket(self._book, meta.product_id, None, None, None)
        return info

    # --------------------------------------------------------------------------------------- exit

    async def submit_exit(self, intent: ExitIntent) -> ExitResult:
        if intent.book != self._book:
            raise ValueError(f"exit for book {intent.book!r} sent to the gateway of {self._book!r}")
        return await self._exit(
            intent.product_id,
            decision_id=intent.decision_id,
            intent_id=intent.intent_id,
            reason=intent.reason,
            bracket_cid=intent.cancel_bracket_order_id,
            emergency=False,
        )

    async def emergency_exit(self, product_id: str) -> ExitResult:
        """Watchdog exit: cancel the pending entry and the bracket (confirmed), then ``market_market_ioc``."""
        position = self._ledger.view(self._book, product_id)
        stamp = int(self._clock().timestamp() * 1000)
        decision_id = f"emergency:{position.entry_decision_id or product_id}:{stamp}"
        return await self._exit(
            product_id, decision_id=decision_id, intent_id=None, reason="WATCHDOG", bracket_cid=None, emergency=True
        )

    async def _exit(
        self,
        product_id: str,
        *,
        decision_id: str,
        intent_id: str | None,
        reason: str,
        bracket_cid: str | None,
        emergency: bool,
    ) -> ExitResult:
        async with self._exit_locks.setdefault(product_id, asyncio.Lock()):
            return await self._exit_locked(product_id, decision_id, intent_id, reason, bracket_cid, emergency=emergency)

    async def _exit_locked(
        self,
        product_id: str,
        decision_id: str,
        intent_id: str | None,
        reason: str,
        bracket_cid: str | None,
        *,
        emergency: bool,
    ) -> ExitResult:
        meta = self._registry.meta(product_id)
        pending = self._pending.get(product_id)
        if pending is not None:  # a half-filled entry is cancelled (confirmed) and gets no bracket
            pending.cancel_requested = True
            pending.abort_bracket = True
            await pending.done.wait()
        position = self._ledger.view(self._book, product_id)
        if not position.is_long:
            return ExitResult("nothing_to_do", reason)
        tally = _Tally()

        def result(status: ExitStatus, detail: str = "") -> ExitResult:
            return ExitResult(status, reason, detail, tally.size, tally.avg_price, tally.fees)

        # X1-X3: cancel the bracket and wait for the terminal state; only then is its base free
        active = bracket_cid or position.bracket_order_id
        if active is not None:
            step = await self._cancel_bracket(active, meta)
            if step == "unconfirmed":
                return result("unconfirmed", "bracket cancel not confirmed")
            if step == "unknown":
                return result("unknown", "bracket outcome unknown")
            position = self._ledger.view(self._book, product_id)
            if not position.is_long:
                return result("closed_by_bracket")

        # X4-X5: a marketable limit at the best bid (not in an emergency), then market IOC for what is left
        bid = await self._best_bid(product_id)
        reference = bid or position.avg_entry or ZERO
        if bid is not None and not emergency:
            outcome = await self._sell(
                meta, decision_id, intent_id, leg="exit", kind="limit", price=floor_to(bid, meta.quote_increment),
                wait_s=self._cfg.exit_timeout_s, tally=tally, reference=reference,
            )  # fmt: skip
            if outcome == "refused" or outcome == "unknown" or outcome == "unconfirmed":
                return result(outcome)
        if self._ledger.view(self._book, product_id).is_long:
            outcome = await self._sell(
                meta, decision_id, intent_id, leg="exit-mkt", kind="market", price=None,
                wait_s=self._cfg.cancel_confirm_timeout_s, tally=tally, reference=reference,
            )  # fmt: skip
            if outcome != "done":
                return result(outcome)
        return result("partial" if self._ledger.view(self._book, product_id).is_long else "closed")

    async def _cancel_bracket(
        self, bracket_cid: str, meta: ProductMeta
    ) -> Literal["absent", "cancelled", "filled", "unconfirmed", "unknown"]:
        record = next((r for r in await self._ledger.open_orders(self._book) if r.client_order_id == bracket_cid), None)
        if record is None:
            return "absent"  # already terminal in the ledger: nothing reserved for it any more
        if record.exchange_order_id is None:
            return "unknown"  # the bracket was sent with an ambiguous outcome: its reservation blocks any sell
        info = await self._cancel_order(record.exchange_order_id)
        if info is None:
            return "unconfirmed"
        await self._sync_fills(
            bracket_cid, record.exchange_order_id, "SELL", meta, decision_id=record.intent_id or "", plan=None
        )  # a TP/SL filled during the cancel is booked here: the position may already be FLAT
        self._balances.release(bracket_cid)  # X3: only now is the base free again
        await self._booking.set_status(bracket_cid, info.status)
        if self._ledger.view(self._book, meta.product_id).is_long:
            await self._ledger.set_bracket(self._book, meta.product_id, None, None, None)
        return "filled" if info.status == "FILLED" else "cancelled"

    async def _sell(
        self,
        meta: ProductMeta,
        decision_id: str,
        intent_id: str | None,
        *,
        leg: str,
        kind: Literal["limit", "market"],
        price: Decimal | None,
        wait_s: float,
        tally: _Tally,
        reference: Decimal,
    ) -> Literal["done", "refused", "rejected", "unknown", "unconfirmed"]:
        """One sell through the guard: send, wait for the terminal state (cancel on timeout), book, release."""
        position = self._ledger.view(self._book, meta.product_id)
        # a size of zero is sent as the whole position: the guard is the authority and its refusal is typed
        size = sellable_base(position, self._balances, meta) or position.base_qty
        if size >= position.base_qty and not is_sellable(size, reference, meta):
            # what is left of the position is below the minimum sellable size: it is dust, not an error
            cost = (position.avg_entry or reference) * position.base_qty
            await self._booking.to_dust(meta.product_id, position.base_qty, cost)
            return "done"
        cid = client_order_id(decision_id, leg)
        spec = exit_spec(cid, self._book, meta.product_id, size, kind=kind, limit_price=price)
        sent = await self._send(spec, leg=leg, intent_id=intent_id)
        if sent.status != "open":
            return "refused" if sent.status == "duplicate" else sent.status
        assert sent.order_id is not None
        info = await self._wait_terminal(sent.order_id, timeout_s=wait_s)
        if info is None or not info.is_terminal:
            info = await self._cancel_order(sent.order_id)
        if info is None:
            return "unconfirmed"
        tally.add(await self._sync_fills(cid, sent.order_id, "SELL", meta, decision_id=decision_id, plan=None))
        self._balances.release(cid)
        await self._booking.set_status(cid, info.status)
        return "done"
