"""WebSocket ``user`` channel -> orders, fills, positions and ``BalanceBook`` (spec sez. 4.1.5, 4.3 punto 8; C11).

This module REPORTS what the exchange did; it never asks the exchange to do anything. Nothing here can create, edit or
cancel an order, and the type system and the tests say so:

* the only collaborators that talk to the exchange are :class:`FillSource` (one read, ``get_fills``) and the WebSocket
  transport (which only subscribes to channels);
* it does not know ``BalanceGuard``, tickets, order bodies or the order-capable methods of ``CoinbaseExchange``
  (``tests/test_user_stream.py`` scans the source for those names and runs every scenario against a fake that fails
  on any call outside the read allowlist).

Two layers, so that the logic can be tested without a network:

* :class:`OrderEventSink` is the one place where an order state reported by the exchange (from this stream or from the
  reconciler's polling) becomes ledger and balance changes. Fills are applied once per ``trade_id``: the ledger table
  ``fills`` is the durable memory (the ``*_recorded`` ledger methods store a fill together with its position in one
  transaction and return nothing for a trade already seen) and
  ``BalanceBook.apply_fill`` is idempotent too, so the same fill arriving from the WS and from the polling changes
  nothing twice.
* :class:`CoinbaseUserStream` is the asyncio shell: it bridges the SDK thread, checks the sequence, and reconnects.

The ``user`` channel carries cumulative quantities, not trades. A fill is therefore always read with ``get_fills``
(real ``trade_id``s, the same ids the polling sees); a stream update only tells the sink that the order moved.

Rules the sink applies, in this order, for each order state ``S``:

1. an order whose ``client_order_id`` has no local record is not Chronos' (a manual order on the same account): ignored;
2. if ``S.filled_size`` is above the fills already stored, read the fills and apply the new ones;
3. if the stored fills still do not add up to ``S.filled_size`` (the exchange publishes fills a little late) the
   order is NOT finalized: a terminal status with missing fills would release the reservation before the fills
   consume it;
4. the status is stored, never moving a terminal order back to a non-terminal one;
5. at a terminal status the unused reservation returns to ``free`` (no optimistic release: this is the confirmation).

A sequence gap or a reconnect cannot be repaired here: the stream asks for a reconciliation (``request_reconcile``).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import partial
from typing import Any, Final, Literal, Protocol

import structlog

from modules.config import CoinbaseConfig
from modules.execution.balance import BalanceBook, Clock, CoverageAlarm, utc_now
from modules.execution.exchange import FillInfo, OrderInfo
from modules.execution.spot import SpotRegistry
from modules.ledger.positions import (
    TERMINAL_STATUSES,
    EntryPlan,
    FillRecord,
    OrderRecord,
    PositionLedger,
    PositionView,
)
from modules.market.ws_feed import Envelope, ErrorEvent, Heartbeat, SequenceMonitor, parse_message, parse_ts

log = structlog.get_logger(__name__)

ZERO: Final = Decimal(0)
CHANNELS: Final = ("user", "heartbeats")
SILENCE_S: Final = 5.0  # heartbeats arrive every second
SUPERVISE_S: Final = 1.0  # how often the silence check runs
BACKOFF_MIN_S: Final = 1.0
BACKOFF_MAX_S: Final = 30.0
CONNECT_TIMEOUT_S: Final = 30.0  # open + subscribe: the SDK has no timeout of its own by default
CLOSE_TIMEOUT_S: Final = 10.0  # a close that never returns must not freeze the stream (seen with a real feed)

AlarmHandler = Callable[[CoverageAlarm], None]
ResyncRequest = Callable[[str], None]


# --------------------------------------------------------------------------------------------- messages


@dataclass(frozen=True, slots=True)
class OrderUpdate:
    """One order as the ``user`` channel reports it. ``snapshot`` is True for the initial state sent at subscription."""

    order: OrderInfo
    snapshot: bool
    ts: datetime


def _opt(raw: Mapping[str, Any], key: str) -> Decimal | None:
    value = raw.get(key)
    if value in (None, ""):
        return None
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"{key}: {value!r} is not a number") from None
    if not d.is_finite():
        raise ValueError(f"{key}: {value!r} is not finite")
    return d


def _req(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"missing field {key!r}")
    return value


def parse_order_message(raw: Mapping[str, Any]) -> OrderInfo:
    """One element of ``events[].orders[]`` of the ``user`` channel -> ``OrderInfo`` (the REST shape).

    ``base_size`` is rebuilt as ``cumulative + leaves``. A price of ``0`` means "not applicable" on this channel.
    """
    cumulative = _opt(raw, "cumulative_quantity") or ZERO
    leaves = _opt(raw, "leaves_quantity")
    return OrderInfo(
        order_id=_req(raw, "order_id"),
        client_order_id=str(raw.get("client_order_id") or ""),
        product_id=_req(raw, "product_id"),
        side=str(raw.get("order_side") or ""),
        status=_req(raw, "status"),
        base_size=None if leaves is None else cumulative + leaves,
        limit_price=_opt(raw, "limit_price") or None,
        stop_trigger_price=_opt(raw, "stop_price") or None,
        filled_size=cumulative,
        average_filled_price=_opt(raw, "avg_price") or None,
        total_fees=_opt(raw, "total_fees") or ZERO,
    )


def parse_user_message(raw: str) -> tuple[Envelope, tuple[OrderUpdate, ...]]:
    """Raw JSON text -> envelope plus the order updates it carries.

    Heartbeats, subscription acks and error messages are parsed by the market feed's parser. Raises ``ValueError``,
    ``KeyError`` or ``TypeError`` on a malformed message.
    """
    data = json.loads(raw)
    if not isinstance(data, Mapping) or data.get("channel") != "user":
        return parse_message(raw), ()
    ts = parse_ts(data["timestamp"])
    updates: list[OrderUpdate] = []
    for event in data.get("events", []):
        snapshot = event.get("type") == "snapshot"
        updates.extend(OrderUpdate(parse_order_message(o), snapshot, ts) for o in event.get("orders", []))
    return Envelope("user", int(data["sequence_num"]), ts, ()), tuple(updates)


# ------------------------------------------------------------------------------------------------- sink


class FillSource(Protocol):
    """The one thing the sink asks the exchange for: the fills of an order (a read)."""

    async def get_fills(self, order_id: str) -> list[FillInfo]: ...


@dataclass(frozen=True, slots=True)
class EntryContext:
    """What the entry's author knew when it sent the order: the decision and the bracket plan of the position."""

    decision_id: str
    plan: EntryPlan | None


class EntryContextSource(Protocol):
    """Implemented by whoever sends entries (the gateway): in memory, keyed by ``client_order_id``."""

    def entry_context(self, client_order_id: str) -> EntryContext | None: ...


@dataclass(frozen=True, slots=True)
class PositionClosed:
    """A SELL fill took the position to zero. ``position`` is the state BEFORE the fill, ``order`` the SELL's record.

    Whoever owns the loss streak and ``closed_trades`` maps the leg and the prices to an exit reason."""

    position: PositionView
    order: OrderRecord
    price: Decimal
    size: Decimal
    ts: datetime


ClosedHandler = Callable[[PositionClosed], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class SyncResult:
    status: Literal["foreign", "synced", "incomplete"]
    new_fills: int = 0
    finalized: bool = False


class OrderEventSink:
    """Applies exchange order states to the ledger and to the ``BalanceBook`` of one book. See the module docstring."""

    def __init__(
        self,
        *,
        book: str,
        ledger: PositionLedger,
        balances: BalanceBook,
        registry: SpotRegistry,
        fills: FillSource,
        contexts: EntryContextSource | None = None,
        on_alarm: AlarmHandler | None = None,
        on_closed: ClosedHandler | None = None,
        clock: Clock = utc_now,
    ) -> None:
        if balances.book != book:
            raise ValueError(f"balance book {balances.book!r} is not the book {book!r}")
        self.book = book
        self._ledger = ledger
        self._balances = balances
        self._registry = registry
        self._fills = fills
        self._contexts = contexts
        self._on_alarm = on_alarm
        self._on_closed = on_closed
        self._clock = clock
        self._cutoff: datetime | None = None
        self.lock = asyncio.Lock()  # one order state at a time; the reconciler takes it for its cross-checks

    def set_book_cutoff(self, cutoff: datetime | None) -> None:
        """Fills traded at or before ``cutoff`` are stored in the ledger but do NOT touch the ``BalanceBook``: the
        balance snapshot taken at startup already contains them (applying them again would double count)."""
        self._cutoff = cutoff

    # ----------------------------------------------------------------------------------------- reads

    async def local_order(self, client_order_id: str) -> OrderRecord | None:
        """The ledger's record of an order (any status), or ``None`` if Chronos never recorded it."""
        if not client_order_id:
            return None
        return await self._ledger.get_order(client_order_id, self.book)

    # --------------------------------------------------------------------------------------- apply

    async def sync_order(self, info: OrderInfo, *, source: str = "ws") -> SyncResult:
        async with self.lock:
            return await self._sync(info, source)

    async def _sync(self, info: OrderInfo, source: str) -> SyncResult:
        rec = await self.local_order(info.client_order_id)
        if rec is None:
            log.debug("order_event_foreign", order_id=info.order_id, client_order_id=info.client_order_id)
            return SyncResult("foreign")
        stored = await self._ledger.fills_for(rec.client_order_id)
        have = sum((f.size for f in stored), ZERO)
        new = 0
        if info.filled_size > have:
            seen = {f.trade_id for f in stored}
            exchange_fills = await self._fills.get_fills(info.order_id)
            for f in sorted(exchange_fills, key=lambda x: (x.trade_time, x.trade_id)):
                if f.trade_id not in seen and await self._apply_fill(rec, f):
                    new += 1
            have = sum((f.size for f in await self._ledger.fills_for(rec.client_order_id)), ZERO)
        if have < info.filled_size:
            log.warning(
                "order_fills_incomplete", client_order_id=rec.client_order_id, stored=str(have),
                reported=str(info.filled_size), source=source,
            )  # fmt: skip
            return SyncResult("incomplete", new)
        await self._store_status(rec, info)
        finalized = info.is_terminal
        if finalized:
            self._balances.release(rec.client_order_id)
        return SyncResult("synced", new, finalized)

    async def _store_status(self, rec: OrderRecord, info: OrderInfo) -> None:
        keep = rec.status in TERMINAL_STATUSES and info.status not in TERMINAL_STATUSES
        status = rec.status if keep else info.status
        if keep:
            log.warning("order_status_regression_ignored", client_order_id=rec.client_order_id, stored=rec.status,
                        reported=info.status)  # fmt: skip
        if status == rec.status and rec.exchange_order_id == info.order_id:
            return
        await self._ledger.update_order(rec.client_order_id, status, self._clock(), exchange_order_id=info.order_id)

    async def _apply_fill(self, rec: OrderRecord, f: FillInfo) -> bool:
        """Apply one fill once. False if it was already stored or is unusable.

        The fill and the position it changes are written in ONE ledger transaction (a crash cannot leave the fill
        stored without its position, or the reverse); the ``BalanceBook`` follows (it is memory, rebuilt by the next
        snapshot)."""
        if f.size <= 0 or f.price < 0 or f.fee < 0:
            log.error("fill_unusable", trade_id=f.trade_id, size=str(f.size), price=str(f.price), fee=str(f.fee))
            return False
        record = FillRecord(
            f.trade_id, self.book, rec.client_order_id, rec.product_id, rec.side, f.price, f.size, f.fee, f.liquidity,
            f.trade_time,
        )  # fmt: skip
        meta = self._registry.meta(rec.product_id)
        alarms: list[CoverageAlarm] = []
        closed: PositionClosed | None = None
        if rec.side == "BUY":
            if not await self._apply_entry(rec, f, record):
                return False  # already applied: the same trade arrived from the WS and from the polling
        else:
            stored, alarms, closed = await self._apply_exit(rec, f, record, meta.base_currency)
            if not stored:
                return False
        if self._cutoff is None or f.trade_time > self._cutoff:
            for alarm in self._balances.apply_fill(
                rec.client_order_id, base_currency=meta.base_currency, quote_currency=meta.quote_currency,
                price=f.price, size=f.size, fee=f.fee, trade_id=f.trade_id,
            ):  # fmt: skip
                self._raise(alarm)
        for alarm in alarms:
            self._raise(alarm)
        if closed is not None and self._on_closed is not None:
            try:
                await self._on_closed(closed)
            except Exception as exc:  # a broken observer must not stop the bookkeeping
                log.error("position_closed_handler_failed", error=f"{type(exc).__name__}: {exc}")
        return True

    async def _apply_entry(self, rec: OrderRecord, f: FillInfo, record: FillRecord) -> bool:
        ctx = None if self._contexts is None else self._contexts.entry_context(rec.client_order_id)
        decision_id = ctx.decision_id if ctx is not None else (rec.intent_id or rec.client_order_id)
        stored = await self._ledger.apply_entry_fill_recorded(record, decision_id, None if ctx is None else ctx.plan)
        if stored is not None and (ctx is None or ctx.plan is None):
            log.error("entry_fill_without_plan", client_order_id=rec.client_order_id, product_id=rec.product_id)
        return stored is not None

    async def _apply_exit(
        self, rec: OrderRecord, f: FillInfo, record: FillRecord, base_currency: str
    ) -> tuple[bool, list[CoverageAlarm], PositionClosed | None]:
        """A SELL never takes the position below zero: the excess is an alarm (``reservation_overdraw``, also for a
        SELL on a flat ledger position), not a short. Returns ``(stored, alarms, closed event)``."""
        before = self._ledger.view(self.book, rec.product_id)
        held = before.base_qty if before.is_long else ZERO
        size = min(f.size, held)
        alarms: list[CoverageAlarm] = []
        after: PositionView | None = None
        if size > 0:
            after = await self._ledger.apply_exit_fill_recorded(record, size)
            stored = after is not None
        else:
            stored = await self._ledger.record_fill(record)  # nothing to take off the position: keep the fill only
        if not stored:
            return False, alarms, None
        if not before.is_long:
            detail = f"SELL fill {f.trade_id} of {f.size} on a flat ledger position"
            alarms.append(CoverageAlarm(kind="reservation_overdraw", currency=base_currency, detail=detail))
        elif size < f.size:
            detail = f"SELL fill {f.trade_id} of {f.size} > ledger position {before.base_qty}"
            alarms.append(CoverageAlarm(kind="reservation_overdraw", currency=base_currency, detail=detail))
        closed = None
        if after is not None and not after.is_long:
            closed = PositionClosed(before, rec, f.price, size, f.trade_time)
        return True, alarms, closed

    def _raise(self, alarm: CoverageAlarm) -> None:
        log.error("coverage_alarm", kind=alarm.kind, currency=alarm.currency, detail=alarm.detail)
        if self._on_alarm is None:
            return
        try:
            self._on_alarm(alarm)
        except Exception as exc:  # a broken handler must not stop the bookkeeping
            log.error("coverage_alarm_handler_failed", error=f"{type(exc).__name__}: {exc}")


# ----------------------------------------------------------------------------------------- transport


class UserTransport(Protocol):
    def open(self) -> None: ...
    def close(self) -> None: ...
    def subscribe(self, product_ids: Sequence[str], channels: Sequence[str]) -> None: ...


TransportFactory = Callable[[Callable[[str], None], Callable[[], None]], UserTransport]


class CoinbaseUserTransport:
    """The SDK's ``WSUserClient`` (authenticated endpoint). It runs its own thread and calls back into it. ``retry`` is
    off: reconnection is supervised by :class:`CoinbaseUserStream`, which also asks for a reconciliation."""

    def __init__(
        self,
        on_message: Callable[[str], None],
        on_close: Callable[[], None],
        *,
        api_key: str | None,
        api_secret: str | None,
    ) -> None:
        from coinbase.websocket import WSUserClient

        self._ws = WSUserClient(
            api_key=api_key, api_secret=api_secret, on_message=on_message, on_close=on_close, retry=False
        )

    def open(self) -> None:
        self._ws.open()

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            self._stop_sdk_thread()  # a dead socket makes the SDK raise BEFORE it stops its loop: one thread per drop
            raise

    def _stop_sdk_thread(self) -> None:
        loop, thread = getattr(self._ws, "loop", None), getattr(self._ws, "thread", None)
        if loop is None or loop.is_closed():
            return
        if loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5.0)
        if not loop.is_running():
            loop.close()

    def subscribe(self, product_ids: Sequence[str], channels: Sequence[str]) -> None:
        self._ws.subscribe(product_ids=list(product_ids), channels=list(channels))


def user_transport_factory(cfg: CoinbaseConfig) -> TransportFactory:
    """The production wiring: credentials from the environment (``CoinbaseConfig``), never from the YAML."""
    key = cfg.api_key.get_secret_value() if cfg.api_key else None
    secret = cfg.api_secret.get_secret_value() if cfg.api_secret else None

    def make(on_message: Callable[[str], None], on_close: Callable[[], None]) -> UserTransport:
        return CoinbaseUserTransport(on_message, on_close, api_key=key, api_secret=secret)

    return make


# --------------------------------------------------------------------------------------------- stream


@dataclass
class UserStreamStats:
    messages: int = 0
    updates: int = 0
    foreign: int = 0
    incomplete: int = 0
    gaps: int = 0
    resets: int = 0
    reconnects: int = 0
    parse_errors: int = 0
    sink_errors: int = 0
    heartbeats: int = 0
    stale_dropped: int = 0  # items of a transport that was already replaced
    connect_failures: int = 0
    errors: list[str] = field(default_factory=list)


class CoinbaseUserStream:
    def __init__(
        self,
        products: Sequence[str],
        sink: OrderEventSink,
        *,
        transport_factory: TransportFactory,
        request_reconcile: ResyncRequest | None = None,
        clock: Clock = utc_now,
        silence_s: float = SILENCE_S,
    ) -> None:
        self.products = tuple(products)
        self.stats = UserStreamStats()
        self.last_message_at: datetime | None = None
        self._sink = sink
        self._factory = transport_factory
        self._request = request_reconcile
        self._clock = clock
        self._silence_s = silence_s
        self._seq = SequenceMonitor()
        self._transport: UserTransport | None = None
        self._queue: asyncio.Queue[tuple[int, str | None]] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # Every transport gets a generation; what an older one says (its messages, and the ``on_close`` that its own
        # ``close()`` makes the SDK call) is not about the connection we hold now.
        self._generation = 0
        self._connected_at: datetime | None = None
        self._reconnecting = False
        self._silent = False
        self._resync_reason: str | None = None
        self._backoff = BACKOFF_MIN_S

    # ----------------------------------------------------------------------------------- dispatch

    def _resync(self, reason: str) -> None:
        log.warning("user_stream_resync", reason=reason)
        if self._request is not None:
            try:
                self._request(reason)
            except Exception as exc:  # a broken callback must not stop the stream
                log.error("resync_request_failed", error=f"{type(exc).__name__}: {exc}")

    async def handle_raw(self, raw: str) -> None:
        """Process one raw message: parse, check the sequence, apply the order updates. The unit under test."""
        self.stats.messages += 1
        now = self._clock()
        try:
            env, updates = parse_user_message(raw)
        except (ValueError, KeyError, TypeError) as exc:
            self.stats.parse_errors += 1
            log.warning("user_ws_parse_error", error=f"{type(exc).__name__}: {exc}", raw=raw[:200])
            self._resync("parse_error")  # the message may have carried a fill
            return
        if env.sequence_num is not None:
            verdict = self._seq.check(env.sequence_num)
            if verdict == "gap":
                self.stats.gaps += 1
                self._resync("sequence_gap")
            elif verdict == "reset":
                self.stats.resets += 1
                self._resync("sequence_reset")
        self.last_message_at = now
        if self._silent or self._resync_reason is not None:  # first message after an outage
            reason, self._silent, self._resync_reason = self._resync_reason or "ws_silence_recovered", False, None
            self._resync(reason)
        for event in env.events:
            if isinstance(event, Heartbeat):
                self.stats.heartbeats += 1
            elif isinstance(event, ErrorEvent):
                self.stats.errors.append(event.message)
                log.error("user_ws_error_message", message=event.message)
        for update in updates:
            await self._apply(update)

    async def _apply(self, update: OrderUpdate) -> None:
        self.stats.updates += 1
        try:
            result = await self._sink.sync_order(update.order, source="ws")
        except Exception as exc:  # network on get_fills, ledger: never kill the stream; the polling will redo it
            self.stats.sink_errors += 1
            log.error("order_update_failed", order_id=update.order.order_id, error=f"{type(exc).__name__}: {exc}")
            self._resync("sink_error")
            return
        if result.status == "foreign":
            self.stats.foreign += 1
        elif result.status == "incomplete":
            self.stats.incomplete += 1
            self._resync("incomplete_fills")

    # ------------------------------------------------------------------------------ supervision

    def check_silence(self, now: datetime) -> bool:
        """True (once per silence) if nothing has arrived for longer than ``silence_s``, counted from the last message
        or from the connection, whichever is later (a connection that opens and says nothing is an outage too)."""
        reference = self.last_message_at
        if self._connected_at is not None and (reference is None or self._connected_at > reference):
            reference = self._connected_at
        if reference is None or self._silent:
            return False
        if (now - reference).total_seconds() <= self._silence_s:
            return False
        self._silent = True
        log.warning("user_ws_silent", since=reference)
        return True

    # --------------------------------------------------------------------------------- asyncio

    def _on_message_thread(self, generation: int, raw: str) -> None:
        """Called by the SDK thread: hand the message to the event loop."""
        if self._loop is not None and self._queue is not None:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, (generation, raw))

    def _on_close_thread(self, generation: int) -> None:
        if self._loop is not None and self._queue is not None:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, (generation, None))

    async def _connect(self) -> None:
        self._seq.reset()
        self._generation += 1
        generation = self._generation
        self._transport = self._factory(
            partial(self._on_message_thread, generation), partial(self._on_close_thread, generation)
        )
        async with asyncio.timeout(CONNECT_TIMEOUT_S):
            await asyncio.to_thread(self._transport.open)
            await asyncio.to_thread(self._transport.subscribe, self.products, CHANNELS)
        self._connected_at = self._clock()
        self._silent = False  # a new connection: a new silence can be detected

    async def _disconnect(self) -> None:
        self._generation += 1  # from now on the old transport is not heard, ``on_close`` of our own close included
        transport, self._transport = self._transport, None
        if transport is not None:
            try:
                async with asyncio.timeout(CLOSE_TIMEOUT_S):
                    await asyncio.to_thread(transport.close)
            except Exception as exc:  # closing a dead socket may raise (or hang): nothing more to do about it
                log.debug("user_ws_close_failed", error=f"{type(exc).__name__}: {exc}")

    async def _reconnect(self, reason: str) -> None:
        """Drop the transport and connect again, retrying with a back-off until it works. One at a time."""
        if self._reconnecting:
            return
        self._reconnecting = True
        try:
            self.stats.reconnects += 1
            log.warning("user_ws_reconnecting", reason=reason, backoff_s=self._backoff)
            self._resync_reason = f"ws_{reason.replace(' ', '_')}"  # events may be lost: reconcile at the first message
            await self._disconnect()
            while True:
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, BACKOFF_MAX_S)
                try:
                    await self._connect()
                    return
                except Exception as exc:
                    self.stats.connect_failures += 1
                    log.error("user_ws_connect_failed", error=f"{type(exc).__name__}: {exc}", retry_in_s=self._backoff)
                    await self._disconnect()  # a transport that opened but could not subscribe must not stay open
        finally:
            self._reconnecting = False

    async def run(self) -> None:
        """Connect and keep the stream alive until cancelled."""
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        while True:  # the very first connection may fail too: keep trying with backoff
            try:
                await self._connect()
                break
            except Exception as exc:
                self.stats.connect_failures += 1
                log.error("user_ws_connect_failed", error=f"{type(exc).__name__}: {exc}", retry_in_s=self._backoff)
                await self._disconnect()
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, BACKOFF_MAX_S)
        self._resync_reason = "ws_connected"  # orders may have moved while we were not listening
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._consume())
                tg.create_task(self._supervise())
        finally:
            await self._disconnect()

    async def _consume(self) -> None:
        assert self._queue is not None
        while True:
            generation, raw = await self._queue.get()
            if generation != self._generation:  # a transport we already replaced
                self.stats.stale_dropped += 1
                continue
            if raw is None:
                await self._reconnect("connection closed")
                continue
            self._backoff = BACKOFF_MIN_S  # a message arrived: the connection works
            await self.handle_raw(raw)

    async def _supervise(self) -> None:
        while True:
            await asyncio.sleep(SUPERVISE_S)
            if not self._reconnecting and self.check_silence(self._clock()):
                await self._reconnect("feed silent")
