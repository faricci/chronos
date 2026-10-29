"""Reconciliation of the ledger and the ``BalanceBook`` with the exchange (spec sez. 4.1.5, 4.3 punto 9; C11).

The reconciler READS the exchange and CORRECTS Chronos' own records. It never creates, edits or cancels an order:

* its view of the exchange (:class:`ReconcileExchange`) declares reads only (accounts, open orders, order history,
  best bid/ask) and the fills reach it through the :class:`~modules.execution.user_stream.OrderEventSink`;
* a position found without a bracket is handed to an injected :class:`BracketPlacer` (the gateway), which sends the
  order through ``BalanceGuard`` like any other; the reconciler itself holds nothing that can send an order;
* orders it does not recognise are left alone: they may be the user's own, and the spec asks for no cancellation.

``on_startup`` (before the streams start) rebuilds the memory of the process (spec 4.1.5, "Avvio"):

1. read accounts and open orders, and the history of every local order that is not open any more;
2. in one synchronous step: restore the reservation of every open Chronos order (``BUY``: ``required_quote(limit,
   leaves)``; ``SELL``: ``leaves``) with ``restore_reservation``, then ``apply_snapshot`` with the exchange balances.
   If anything touched the book during the reads the step is repeated: a snapshot is never applied over a race;
3. replay the order states through the sink: fills traded before the snapshot go to the ledger only (the snapshot
   already holds them), orders that finished while the process was down are finalized, positions are updated;
4. relink a bracket that exists on the exchange but not in the ledger; give a position that has none to the placer
   (or turn it into dust when it is below the minimum sellable size).

``reconcile_once`` (every ``reconcile_interval_s`` and on request, e.g. after a gap of the user stream) repeats step 3
and the cross-checks. An order recorded locally and unknown to the exchange (``OutcomeUnknown`` after a timeout) is
looked up by its ``client_order_id`` in the order history; when it is absent for ``UNKNOWN_CYCLES`` consecutive cycles
its reservation is released and the record is closed (spec 4.1.5, "Idempotenza").

Alarms (``reservation_overdraw``, ``hold_exceeds_reservation``, ``balance_drift``) go through :class:`CoverageMonitor`,
which trips ``coverage_breach`` on the first overdraw or excess hold and on the third drift within 24 hours.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final, Protocol

import structlog

from modules.config import FallbackFees
from modules.execution.balance import AccountSnapshot, BalanceBook, Clock, CoverageAlarm, Reservation, utc_now
from modules.execution.balance_guard import FeeSource
from modules.execution.exchange import BestBidAsk, OrderInfo
from modules.execution.rounding import floor_to
from modules.execution.spot import SpotRegistry
from modules.execution.user_stream import AlarmHandler, OrderEventSink
from modules.ledger.positions import TERMINAL_STATUSES, OrderRecord, PositionLedger, PositionView
from modules.risk.costs import coverage_fee_rate, required_quote

log = structlog.get_logger(__name__)

ZERO: Final = Decimal(0)
UNKNOWN_CYCLES: Final = 2  # cycles without a trace of an order before its reservation is released (spec 4.1.5)
DRIFT_ALARMS_TO_TRIP: Final = 3  # balance_drift alarms within DRIFT_WINDOW that trip coverage_breach (spec 4.1.4)
DRIFT_WINDOW: Final = timedelta(hours=24)
SNAPSHOT_ATTEMPTS: Final = 3
HISTORY_SLACK: Final = timedelta(minutes=1)


# ------------------------------------------------------------------------------------------ alarms


class CoverageMonitor:
    """Turns coverage alarms into ``coverage_breach`` (spec 4.1.4, "Coverage breach"; 4.1.6 rule 7).

    ``reservation_overdraw`` and ``hold_exceeds_reservation`` trip at once. ``balance_drift`` trips at the
    ``DRIFT_ALARMS_TO_TRIP``-th occurrence within ``DRIFT_WINDOW``. ``trip(reason)`` is
    ``AccountGuards.trip_coverage_breach`` of the book. The handler is the one given to the balance feed, the sink and
    the reconciler, so the count sees every source.
    """

    def __init__(self, trip: Callable[[str], None], clock: Clock = utc_now) -> None:
        self._trip = trip
        self._clock = clock
        self._drifts: deque[datetime] = deque()

    def drift_count_24h(self) -> int:
        self._prune(self._clock())
        return len(self._drifts)

    def _prune(self, now: datetime) -> None:
        while self._drifts and now - self._drifts[0] >= DRIFT_WINDOW:
            self._drifts.popleft()

    def on_alarm(self, alarm: CoverageAlarm) -> None:
        reason = f"{alarm.kind}:{alarm.currency}:{alarm.detail}"
        if alarm.kind == "balance_drift":
            now = self._clock()
            self._drifts.append(now)
            self._prune(now)
            if len(self._drifts) < DRIFT_ALARMS_TO_TRIP:
                return
            reason = f"{len(self._drifts)} balance_drift in 24h, last: {alarm.currency}:{alarm.detail}"
        self._trip(reason)


# ------------------------------------------------------------------------------------- collaborators


class ReconcileExchange(Protocol):
    """What the reconciler may ask the exchange: reads. ``CoinbaseExchange`` satisfies it structurally."""

    async def get_accounts(self) -> AccountSnapshot: ...
    async def list_open_orders(self) -> list[OrderInfo]: ...
    async def list_orders(
        self,
        *,
        statuses: Sequence[str] | None = None,
        product_ids: Sequence[str] | None = None,
        order_ids: Sequence[str] | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[OrderInfo]: ...
    async def get_best_bid_ask(self, product_id: str) -> BestBidAsk: ...


@dataclass(frozen=True, slots=True)
class BracketRequest:
    """A LONG position without an active bracket, with the size the bracket must cover (``floor_to(min(base_qty,
    sellable))``) and the levels the ledger remembers."""

    book: str
    product_id: str
    base_size: Decimal
    take_profit: Decimal
    stop_loss: Decimal


class BracketPlacer(Protocol):
    """Implemented by the gateway: authorizes (``BalanceGuard``) and sends the standalone ``trigger_bracket_gtc``."""

    async def place_missing_bracket(self, request: BracketRequest) -> None: ...


Adopter = Callable[[OrderInfo], OrderRecord | None]


@dataclass
class ReconcileReport:
    reason: str
    startup: bool
    started_at: datetime
    snapshot_applied: bool = False
    restored_reservations: int = 0
    synced_orders: int = 0
    new_fills: int = 0
    finalized_orders: int = 0
    incomplete_orders: int = 0
    unresolved_orders: int = 0
    released_unknown: int = 0
    foreign_orders: int = 0
    adopted_orders: int = 0
    inconsistent_orders: int = 0
    relinked_brackets: int = 0
    bracket_requests: int = 0
    unprotected_positions: int = 0
    dust_conversions: int = 0
    drift_alarms: int = 0
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Reading:
    epoch: int
    accounts: AccountSnapshot | None
    read_at: datetime
    exchange_open: list[OrderInfo]
    local_open: list[OrderRecord]
    history: dict[str, OrderInfo]  # client_order_id -> order, for local orders that are not open on the exchange
    ours: dict[str, tuple[OrderRecord, OrderInfo]]  # open on the exchange AND recorded (or adopted) locally
    foreign: list[OrderInfo]
    adopted: int


# ------------------------------------------------------------------------------------------ reconciler


class Reconciler:
    def __init__(
        self,
        *,
        book: str,
        exchange: ReconcileExchange,
        ledger: PositionLedger,
        balances: BalanceBook,
        registry: SpotRegistry,
        sink: OrderEventSink,
        fees: FeeSource,
        fallback: FallbackFees,
        interval_s: float,
        on_alarm: AlarmHandler | None = None,
        placer: BracketPlacer | None = None,
        adopt: Adopter | None = None,
        clock: Clock = utc_now,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be positive")
        if balances.source != "exchange":
            raise ValueError("only an exchange book is reconciled; a virtual book is its own source of truth")
        if balances.book != book or sink.book != book:
            raise ValueError("book, balances and sink must belong to the same book")
        self.book = book
        self._exchange = exchange
        self._ledger = ledger
        self._balances = balances
        self._registry = registry
        self._sink = sink
        self._fees = fees
        self._fallback = fallback
        self._interval_s = interval_s
        self._on_alarm = on_alarm
        self._placer = placer
        self._adopt = adopt
        self._clock = clock
        self._lock = asyncio.Lock()
        self._wakeup = asyncio.Event()
        self._reasons: list[str] = []
        self._misses: dict[str, int] = {}
        self._increments = _increments(registry)
        self.last_report: ReconcileReport | None = None
        self.consecutive_failures = 0
        self.last_error: str | None = None

    # ---------------------------------------------------------------------------------------- public

    def request(self, reason: str) -> None:
        """Ask for a reconciliation now (a gap in the user stream, a reconnect). Requests collapse into one cycle."""
        if reason not in self._reasons:
            self._reasons.append(reason)
        self._wakeup.set()

    async def on_startup(self) -> ReconcileReport:
        """Rebuild the reservations, the balances and the ledger from the exchange. Call it once, before the user
        stream and the balance feed start. An exchange error propagates: the process cannot trade blind."""
        async with self._lock:
            return await self._cycle("startup", startup=True)

    async def reconcile_once(self, reason: str = "periodic") -> ReconcileReport:
        async with self._lock:
            return await self._cycle(reason, startup=False)

    async def run(self) -> None:
        """Reconcile every ``interval_s`` or on request, forever (a task of the app's ``TaskGroup``)."""
        while True:
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=self._interval_s)
            except TimeoutError:
                pass
            reasons, self._reasons = self._reasons, []
            self._wakeup.clear()
            try:
                await self.reconcile_once(",".join(reasons) or "periodic")
                self.consecutive_failures, self.last_error = 0, None
            except Exception as exc:  # a failed cycle changes nothing; the next one retries
                self.consecutive_failures += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.error("reconcile_failed", error=self.last_error, failures=self.consecutive_failures)

    # ------------------------------------------------------------------------------------------ cycle

    async def _cycle(self, reason: str, *, startup: bool) -> ReconcileReport:
        report = ReconcileReport(reason=reason, startup=startup, started_at=self._clock())
        reading: _Reading | None = None
        if startup:
            for _ in range(SNAPSHOT_ATTEMPTS):
                reading = await self._read(startup=True)
                if self._rebuild_book(reading, report):
                    break
                report.notes.append("book changed during the reads: repeated")
            else:
                report.notes.append("snapshot not applied: the book kept changing")
                log.error("startup_snapshot_not_applied", attempts=SNAPSHOT_ATTEMPTS)
            assert reading is not None
        else:
            reading = await self._read(startup=False)
            for rec, _ in reading.ours.values():
                if self._balances.reservation(rec.client_order_id) is None:  # reported, never invented at runtime
                    report.notes.append(f"{rec.client_order_id}: open order without a reservation")
                    log.error("open_order_without_reservation", client_order_id=rec.client_order_id)
        report.foreign_orders = len(reading.foreign)
        report.adopted_orders = reading.adopted
        if startup:
            self._sink.set_book_cutoff(reading.read_at)
        await self._replay_orders(reading, report)
        await self._relink_brackets(reading, report, startup=startup)
        await self._cross_check(reading, report)
        self.last_report = report
        log.info("reconciled", **{k: v for k, v in vars(report).items() if k != "notes"})
        return report

    async def _read(self, *, startup: bool) -> _Reading:
        epoch = self._balances.epoch
        local_open = await self._ledger.open_orders(self.book)  # first: an order recorded after this is not judged
        accounts = await self._exchange.get_accounts() if startup else None
        exchange_open = await self._exchange.list_open_orders()
        open_cids = {o.client_order_id for o in exchange_open}
        missing = [r for r in local_open if r.client_order_id not in open_cids]
        history: dict[str, OrderInfo] = {}
        if missing:
            since = min(r.created_at for r in missing) - HISTORY_SLACK
            history = {o.client_order_id: o for o in await self._exchange.list_orders(start_date=since.isoformat())}
        ours: dict[str, tuple[OrderRecord, OrderInfo]] = {}
        foreign: list[OrderInfo] = []
        adopted = 0
        for info in exchange_open:
            rec = await self._sink.local_order(info.client_order_id)
            if rec is None and self._adopt is not None:
                rec = self._adopt(info)
                if rec is not None:
                    await self._ledger.record_order(rec)
                    adopted += 1
                    log.warning("order_adopted", client_order_id=rec.client_order_id, product_id=rec.product_id)
            if rec is None:
                foreign.append(info)
                log.info("foreign_open_order", order_id=info.order_id, product_id=info.product_id, side=info.side)
            elif rec.status in TERMINAL_STATUSES:
                log.warning("order_open_on_exchange_closed_locally", client_order_id=rec.client_order_id,
                            local=rec.status)  # fmt: skip
                foreign.append(info)  # no reservation: the balance snapshot will not credit its hold
            else:
                ours[info.client_order_id] = (rec, info)
        read_at = self._clock()  # after every read: fills traded before this instant are in the snapshot (startup)
        return _Reading(epoch, accounts, read_at, exchange_open, local_open, history, ours, foreign, adopted)

    # ------------------------------------------------------------------------------ startup: balances

    def _rebuild_book(self, reading: _Reading, report: ReconcileReport) -> bool:
        """Restore the reservations of the open Chronos orders, then apply the balance snapshot. NO await in here:
        the book cannot change between the epoch check and the snapshot. False if it changed during the reads."""
        if self._balances.epoch != reading.epoch or reading.accounts is None:
            return False
        for _, info in reading.ours.values():
            if self._balances.reservation(info.client_order_id) is None and self._restore(info):
                report.restored_reservations += 1
        snapshot = AccountSnapshot(
            balances=reading.accounts.balances,
            foreign_order_currencies=self._foreign_currencies(reading.foreign),
            taken_at=reading.accounts.taken_at,
        )
        _, alarms = self._balances.apply_snapshot(snapshot, self._balances.epoch, self._increments)
        report.snapshot_applied = True
        for alarm in alarms:
            self._raise(alarm)
        return True

    def _restore(self, info: OrderInfo) -> bool:
        reservation = self._reservation_of(info)
        if reservation is None:
            log.error("reservation_not_restorable", client_order_id=info.client_order_id, side=info.side,
                      limit_price=str(info.limit_price), leaves=str(info.leaves_size))  # fmt: skip
            return False
        self._balances.restore_reservation(reservation)
        return True

    def _reservation_of(self, info: OrderInfo) -> Reservation | None:
        meta = self._registry.meta(info.product_id)
        leaves = info.leaves_size
        if leaves is None or leaves <= 0:
            return None
        if info.side == "BUY":
            if info.limit_price is None:
                return None
            fee_rate = coverage_fee_rate(self._fees.current, self._fallback)
            amount = required_quote(info.limit_price, leaves, fee_rate, meta.quote_increment)
            return Reservation(client_order_id=info.client_order_id, product_id=info.product_id, side="BUY",
                               currency=meta.quote_currency, amount=amount)  # fmt: skip
        if info.side == "SELL":
            return Reservation(client_order_id=info.client_order_id, product_id=info.product_id, side="SELL",
                               currency=meta.base_currency, amount=leaves)  # fmt: skip
        return None

    def _currencies_of(self, product_id: str) -> tuple[str, str]:
        if product_id in self._registry.product_ids:
            meta = self._registry.meta(product_id)
            return meta.base_currency, meta.quote_currency
        base, _, quote = product_id.partition("-")
        return base, quote or base

    def _foreign_currencies(self, foreign: Sequence[OrderInfo]) -> frozenset[str]:
        """Currencies on hold because of open orders Chronos holds no reservation for (never credited)."""
        out: set[str] = set()
        for info in foreign:
            base, quote = self._currencies_of(info.product_id)
            if info.side == "BUY":
                out.add(quote)
            elif info.side == "SELL":
                out.add(base)
            else:
                out.update((base, quote))
        return frozenset(out)

    # ---------------------------------------------------------------------------------- order replay

    async def _replay_orders(self, reading: _Reading, report: ReconcileReport) -> None:
        by_cid = {i.client_order_id: i for i in reading.exchange_open}
        source = "startup" if report.startup else "poll"
        local_cids = {r.client_order_id for r in reading.local_open}
        for rec in reading.local_open:
            cid = rec.client_order_id
            info = by_cid.get(cid) or reading.history.get(cid)
            if info is None:
                await self._unresolved(rec, report)
                continue
            self._misses.pop(cid, None)
            await self._sync(info, source, report)
        for cid, (_, info) in reading.ours.items():
            if cid not in local_cids:  # adopted in this cycle
                await self._sync(info, "adopt", report)

    async def _sync(self, info: OrderInfo, source: str, report: ReconcileReport) -> None:
        result = await self._sink.sync_order(info, source=source)
        report.synced_orders += 1
        report.new_fills += result.new_fills
        report.finalized_orders += int(result.finalized)
        report.incomplete_orders += int(result.status == "incomplete")

    async def _unresolved(self, rec: OrderRecord, report: ReconcileReport) -> None:
        """A local order the exchange does not know. After ``UNKNOWN_CYCLES`` cycles it never existed: release."""
        cid = rec.client_order_id
        self._misses[cid] = self._misses.get(cid, 0) + 1
        report.unresolved_orders += 1
        if self._misses[cid] < UNKNOWN_CYCLES:
            log.warning("order_unknown_on_exchange", client_order_id=cid, cycles=self._misses[cid])
            return
        released = self._balances.release(cid)
        await self._ledger.update_order(cid, "FAILED", self._clock())
        self._misses.pop(cid, None)
        report.released_unknown += 1
        log.warning("unknown_order_closed", client_order_id=cid, released=str(released))

    # ------------------------------------------------------------------------------------ positions

    async def _relink_brackets(self, reading: _Reading, report: ReconcileReport, *, startup: bool) -> None:
        open_now = await self._ledger.open_orders(self.book)
        for pos in self._ledger.long_positions(self.book):
            sells = [r for r in open_now if r.side == "SELL" and r.product_id == pos.product_id]
            linked = [r for r in sells if _is_bracket(r, reading.ours.get(r.client_order_id))]
            ids = {r.client_order_id for r in linked} | {r.exchange_order_id for r in linked}
            if pos.bracket_order_id is not None and pos.bracket_order_id in ids:
                continue
            if linked:
                rec = linked[0]
                pair = reading.ours.get(rec.client_order_id)
                await self._ledger.set_bracket(
                    self.book, pos.product_id, rec.client_order_id,
                    None if pair is None else pair[1].limit_price,
                    None if pair is None else pair[1].stop_trigger_price,
                )  # fmt: skip
                report.relinked_brackets += 1
                continue
            report.unprotected_positions += 1
            if startup:
                await self._protect(pos, open_now, report)

    async def _protect(self, pos: PositionView, open_orders: Sequence[OrderRecord], report: ReconcileReport) -> None:
        """Startup only: nobody is waiting for an entry any more, so a position without a bracket gets one (or
        becomes dust). A position whose entry order is still open is left to that order's life cycle."""
        if any(r.side == "BUY" and r.product_id == pos.product_id for r in open_orders):
            report.notes.append(f"{pos.product_id}: entry still open, no bracket requested")
            return
        meta = self._registry.meta(pos.product_id)
        try:
            bid = (await self._exchange.get_best_bid_ask(pos.product_id)).bid
        except Exception as exc:  # no price, no judgement on the minimum size: leave it to the watchdog
            report.notes.append(f"{pos.product_id}: best bid unavailable ({type(exc).__name__})")
            return
        held = floor_to(pos.base_qty, meta.base_increment)
        if held < meta.base_min_size or held * bid < meta.quote_min_size:  # the position itself is below the minimum
            await self._to_dust(pos, report)
            return
        sellable = min(pos.base_qty - self._balances.reserved_sell_base(pos.product_id),
                       self._balances.free(meta.base_currency))  # fmt: skip
        size = floor_to(max(sellable, ZERO), meta.base_increment)
        if size < meta.base_min_size or size * bid < meta.quote_min_size:
            report.notes.append(f"{pos.product_id}: {size} sellable of {pos.base_qty} held, no bracket requested")
            return
        if pos.tp is None or pos.sl is None or self._placer is None:
            report.notes.append(f"{pos.product_id}: bracket needed, cannot be placed (tp/sl or placer missing)")
            log.error("bracket_missing_not_repaired", product_id=pos.product_id, tp=str(pos.tp), sl=str(pos.sl),
                      placer=self._placer is not None)  # fmt: skip
            return
        try:
            await self._placer.place_missing_bracket(BracketRequest(self.book, pos.product_id, size, pos.tp, pos.sl))
            report.bracket_requests += 1
        except Exception as exc:  # the watchdog covers the position meanwhile
            report.notes.append(f"{pos.product_id}: bracket request failed ({type(exc).__name__})")
            log.error("bracket_request_failed", product_id=pos.product_id, error=f"{type(exc).__name__}: {exc}")

    async def _to_dust(self, pos: PositionView, report: ReconcileReport) -> None:
        cost = (pos.avg_entry or ZERO) * pos.base_qty
        await self._ledger.apply_exit_fill(self.book, pos.product_id, pos.base_qty)
        await self._ledger.set_dust(self.book, pos.product_id, pos.base_qty, cost)
        report.dust_conversions += 1
        log.warning("position_became_dust", product_id=pos.product_id, qty=str(pos.base_qty))

    # ---------------------------------------------------------------------------------- cross-check

    async def _cross_check(self, reading: _Reading, report: ReconcileReport) -> None:
        """The ledger cannot hold more base than the account does (the sink's lock keeps a fill from being half applied
        while we look). Currencies with foreign orders on hold are skipped: their hold is not part of our totals."""
        if self._balances.snapshot_at is None:
            return
        foreign = self._foreign_currencies(reading.foreign)
        async with self._sink.lock:
            owned: dict[str, Decimal] = {}
            for pos in self._ledger.long_positions(self.book):
                base = self._currencies_of(pos.product_id)[0]
                owned[base] = owned.get(base, ZERO) + pos.base_qty
            for currency, qty in sorted(owned.items()):
                if currency in foreign:
                    continue
                tolerance = 2 * self._increments.get(currency, ZERO)
                if qty > self._balances.total(currency) + tolerance:
                    report.drift_alarms += 1
                    self._raise(
                        CoverageAlarm(kind="balance_drift", currency=currency,
                                      detail=f"ledger holds {qty} but the book has {self._balances.total(currency)}")
                    )  # fmt: skip

    def _raise(self, alarm: CoverageAlarm) -> None:
        log.error("coverage_alarm", kind=alarm.kind, currency=alarm.currency, detail=alarm.detail)
        if self._on_alarm is None:
            return
        try:
            self._on_alarm(alarm)
        except Exception as exc:  # a broken handler must not stop the reconciliation
            log.error("coverage_alarm_handler_failed", error=f"{type(exc).__name__}: {exc}")


def _is_bracket(rec: OrderRecord, open_pair: tuple[OrderRecord, OrderInfo] | None) -> bool:
    """A SELL that is the bracket of a position: recorded as such, or a trigger order on the exchange."""
    return rec.leg == "bracket" or (open_pair is not None and open_pair[1].stop_trigger_price is not None)


def _increments(registry: SpotRegistry) -> Mapping[str, Decimal]:
    """Smallest increment per currency over the registry (the smaller, the more sensitive the drift alarms)."""
    out: dict[str, Decimal] = {}
    for product_id in registry.product_ids:
        meta = registry.meta(product_id)
        for currency, inc in ((meta.base_currency, meta.base_increment), (meta.quote_currency, meta.quote_increment)):
            out[currency] = min(out.get(currency, inc), inc)
    return out
