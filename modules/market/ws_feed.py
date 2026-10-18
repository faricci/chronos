"""Public market data over WebSocket: trades, ticker, level2, heartbeats (spec sez. 2.3 A.1).

No authentication is needed for any of these channels (verified for ``level2`` on 2026-10-17). The 1m candles are
built from the trades, never from the ``candles`` channel (5 minute granularity).

Layers, so that the logic can be tested without a network:
- ``parse_message``: pure, raw JSON -> typed events (formats captured from the real feed, see tests/fixtures);
- ``SequenceMonitor``: ``sequence_num`` is one counter per connection across ALL channels, so any jump means lost
  messages and a restart from 0 means a new connection;
- ``CoinbaseMarketStream.handle_raw``: synchronous dispatch of trades (``on_trades``) and level2 (order books);
- ``CoinbaseMarketStream.run``: asyncio shell that bridges the SDK's callback thread, supervises the connection
  and reconnects with backoff when the feed goes silent.

What a problem does (never a silent hole in the data):
- sequence gap or reconnect  -> books stale until a new snapshot (level2 is resubscribed) and ``on_damage(start, end)``
  tells the consumers of trades (fill and bracket simulators) which interval may be incomplete;
- silence (heartbeats stop)  -> same with an open interval ``on_damage(start, None)``, plus a forced reconnect.

The stream builds NO bars: 1m bars are the REST candles read by ``BarService`` (spec invariant I1), because the
exchange assigns trades to candle minutes by a rule that cannot be rebuilt from the trade ``time`` field.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final, Literal, Protocol

import structlog

from modules.market.orderbook import BookSide, LevelUpdate, LocalOrderBook

log = structlog.get_logger(__name__)

CHANNELS: Final = ("market_trades", "ticker", "level2", "heartbeats")
SILENCE_S: Final = 5.0  # heartbeats arrive every second
BACKOFF_MIN_S: Final = 1.0
BACKOFF_MAX_S: Final = 30.0

SEEN_TRADES_MAX: Final = 20_000

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------------------------- events


@dataclass(frozen=True, slots=True)
class Trade:
    product_id: str
    trade_id: str
    ts: datetime
    price: float
    size: float


@dataclass(frozen=True, slots=True)
class Quote:
    product_id: str
    price: float
    best_bid: float
    best_ask: float
    best_bid_size: float
    best_ask_size: float
    ts: datetime


@dataclass(frozen=True, slots=True)
class TradesEvent:
    trades: tuple[Trade, ...]
    snapshot: bool


@dataclass(frozen=True, slots=True)
class BookEvent:
    product_id: str
    levels: tuple[LevelUpdate, ...]
    snapshot: bool


@dataclass(frozen=True, slots=True)
class Heartbeat:
    ts: datetime


@dataclass(frozen=True, slots=True)
class ErrorEvent:
    message: str


@dataclass(frozen=True, slots=True)
class Subscribed:
    channels: tuple[str, ...]


Event = Quote | TradesEvent | BookEvent | Heartbeat | ErrorEvent | Subscribed


@dataclass(frozen=True, slots=True)
class Envelope:
    channel: str
    sequence_num: int | None  # None for error messages, which carry none
    ts: datetime | None
    events: tuple[Event, ...]


def parse_ts(value: str) -> datetime:
    """ISO-8601 with up to nanoseconds and a trailing Z (extra digits are truncated)."""
    return datetime.fromisoformat(value)


def _side(value: str) -> BookSide:
    if value == "bid":
        return "bid"
    if value == "offer":
        return "offer"
    raise ValueError(f"unknown book side {value!r}")


def parse_message(raw: str) -> Envelope:
    """Raw JSON text -> ``Envelope``. Raises ``ValueError``/``KeyError`` on a malformed message."""
    d: Mapping[str, Any] = json.loads(raw)
    if d.get("type") == "error":
        return Envelope("error", None, None, (ErrorEvent(str(d.get("message", ""))),))
    channel = str(d["channel"])
    ts = parse_ts(d["timestamp"])
    events: list[Event] = []
    for ev in d.get("events", []):
        if channel == "l2_data":
            levels = tuple(
                LevelUpdate(_side(u["side"]), Decimal(u["price_level"]), Decimal(u["new_quantity"]))
                for u in ev["updates"]
            )
            events.append(BookEvent(str(ev["product_id"]), levels, ev["type"] == "snapshot"))
        elif channel == "market_trades":
            trades = tuple(
                Trade(t["product_id"], str(t["trade_id"]), parse_ts(t["time"]), float(t["price"]), float(t["size"]))
                for t in ev["trades"]
            )
            events.append(TradesEvent(trades, ev["type"] == "snapshot"))
        elif channel == "ticker":
            for t in ev["tickers"]:
                events.append(
                    Quote(
                        t["product_id"],
                        float(t["price"]),
                        float(t["best_bid"]),
                        float(t["best_ask"]),
                        float(t["best_bid_quantity"]),
                        float(t["best_ask_quantity"]),
                        ts,
                    )  # fmt: skip
                )
        elif channel == "heartbeats":
            events.append(Heartbeat(ts))
        elif channel == "subscriptions":
            events.append(Subscribed(tuple(ev.get("subscriptions", {}))))
    return Envelope(channel, int(d["sequence_num"]), ts, tuple(events))


# ------------------------------------------------------------------------------------ sequence


class SequenceMonitor:
    """``sequence_num`` is contiguous per connection and restarts at 0 on a new one."""

    def __init__(self) -> None:
        self.last: int | None = None

    def check(self, seq: int) -> Literal["ok", "gap", "reset"]:
        last, self.last = self.last, seq
        if last is None or seq == last + 1:
            return "ok"
        return "reset" if seq <= last else "gap"

    def reset(self) -> None:
        self.last = None


# ------------------------------------------------------------------------------------ transport


class MarketTransport(Protocol):
    def open(self) -> None: ...
    def close(self) -> None: ...
    def subscribe(self, product_ids: Sequence[str], channels: Sequence[str]) -> None: ...
    def unsubscribe(self, product_ids: Sequence[str], channels: Sequence[str]) -> None: ...


TransportFactory = Callable[[Callable[[str], None], Callable[[], None]], MarketTransport]


class CoinbaseTransport:
    """The SDK's ``WSClient``: it runs its own thread and calls back into it. ``retry`` is off: reconnection is
    supervised by ``CoinbaseMarketStream`` so that every outage is also accounted for in the bars."""

    def __init__(self, on_message: Callable[[str], None], on_close: Callable[[], None]) -> None:
        from coinbase.websocket import WSClient

        self._ws = WSClient(on_message=on_message, on_close=on_close, retry=False)

    def open(self) -> None:
        self._ws.open()

    def close(self) -> None:
        self._ws.close()

    def subscribe(self, product_ids: Sequence[str], channels: Sequence[str]) -> None:
        self._ws.subscribe(product_ids=list(product_ids), channels=list(channels))

    def unsubscribe(self, product_ids: Sequence[str], channels: Sequence[str]) -> None:
        self._ws.unsubscribe(product_ids=list(product_ids), channels=list(channels))


# --------------------------------------------------------------------------------------- stream


@dataclass
class StreamStats:
    messages: int = 0
    gaps: int = 0
    resets: int = 0
    reconnects: int = 0
    parse_errors: int = 0
    resubscribes: int = 0
    heartbeats: int = 0
    trades: int = 0
    errors: list[str] = field(default_factory=list)


class CoinbaseMarketStream:
    def __init__(
        self,
        products: Sequence[str],
        transport_factory: TransportFactory = CoinbaseTransport,
        *,
        on_trades: Callable[[Sequence[Trade]], None] | None = None,
        on_damage: Callable[[datetime, datetime | None], None] | None = None,
        clock: Clock = utc_now,
        raw_tap: Callable[[float, str], None] | None = None,
        silence_s: float = SILENCE_S,
    ) -> None:
        self.products = tuple(products)
        self.books = {p: LocalOrderBook(p) for p in self.products}
        self.quotes: dict[str, Quote] = {}
        self.stats = StreamStats()
        self.last_message_at: datetime | None = None
        self._factory = transport_factory
        self._on_trades = on_trades
        self._on_damage = on_damage
        self._seen: set[str] = set()
        self._seen_order: deque[str] = deque()
        self._clock = clock
        self._raw_tap = raw_tap
        self._silence_s = silence_s
        self._seq = SequenceMonitor()
        self._transport: MarketTransport | None = None
        self._queue: asyncio.Queue[tuple[float, str | None]] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._resubscribe: set[str] = set()
        self._silent = False
        self._backoff = BACKOFF_MIN_S

    # ----------------------------------------------------------------------------- reads

    def spread_bps(self, product_id: str) -> float | None:
        """Spread from the live book if it is trustworthy, else from the last ticker, else None."""
        book = self.books[product_id]
        if not book.stale:
            return book.spread_bps()
        q = self.quotes.get(product_id)
        if q is None or q.best_ask <= q.best_bid:
            return None
        return (q.best_ask - q.best_bid) / ((q.best_ask + q.best_bid) / 2) * 10_000

    # ------------------------------------------------------------------------- dispatch

    def handle_raw(self, recv_ts: float, raw: str) -> None:
        """Process one raw message. Synchronous and free of I/O: the unit under test."""
        if self._raw_tap is not None:
            self._raw_tap(recv_ts, raw)
        self.stats.messages += 1
        now = self._clock()
        try:
            env = parse_message(raw)
        except (ValueError, KeyError, TypeError) as exc:
            self.stats.parse_errors += 1
            log.warning("ws_parse_error", error=f"{type(exc).__name__}: {exc}", raw=raw[:200])
            return
        if env.sequence_num is not None:
            verdict = self._seq.check(env.sequence_num)
            if verdict != "ok":
                self._on_discontinuity(verdict, now)
        previous = self.last_message_at
        self.last_message_at = now
        if self._silent:  # the first message after a silence closes the damaged period
            self._silent = False
            self._close_damage(previous, now)
        for event in env.events:
            self._dispatch(event, now)

    def _dispatch(self, event: Event, now: datetime) -> None:
        if isinstance(event, TradesEvent):
            fresh = self._unseen(event.trades)
            self.stats.trades += len(fresh)
            if fresh and self._on_trades is not None:
                self._on_trades(fresh)
        elif isinstance(event, BookEvent):
            book = self.books.get(event.product_id)
            if book is None:
                return
            if event.snapshot:
                book.apply_snapshot(event.levels, now)
                self._resubscribe.discard(event.product_id)
            elif not book.apply_update(event.levels, now) and book.stale:
                if event.product_id not in self._resubscribe and book.stale_reason != "no snapshot yet":
                    self._resubscribe.add(event.product_id)
                    log.warning("order_book_stale", product=event.product_id, reason=book.stale_reason)
        elif isinstance(event, Quote):
            self.quotes[event.product_id] = event
        elif isinstance(event, Heartbeat):
            self.stats.heartbeats += 1
        elif isinstance(event, ErrorEvent):
            self.stats.errors.append(event.message)
            log.error("ws_error_message", message=event.message)

    def _unseen(self, trades: Sequence[Trade]) -> list[Trade]:
        """Trades never forwarded before: the channel resends its recent trades as a snapshot at every reconnect."""
        fresh = []
        for t in trades:
            key = f"{t.product_id}:{t.trade_id}"
            if key in self._seen:
                continue
            self._seen.add(key)
            self._seen_order.append(key)
            if len(self._seen_order) > SEEN_TRADES_MAX:
                self._seen.discard(self._seen_order.popleft())
            fresh.append(t)
        return fresh

    def _on_discontinuity(self, verdict: str, now: datetime) -> None:
        if verdict == "gap":
            self.stats.gaps += 1
        else:
            self.stats.resets += 1
        log.warning("ws_discontinuity", kind=verdict, last_message_at=self.last_message_at)
        for p, book in self.books.items():
            book.mark_stale(f"{verdict} in the message sequence")
            self._resubscribe.add(p)
        self._close_damage(self.last_message_at, now)

    def _close_damage(self, last_good: datetime | None, now: datetime) -> None:
        """Trades between the last good message and now may be missing: tell the consumers (closed interval)."""
        if self._on_damage is not None and last_good is not None:
            self._on_damage(last_good, now)

    # -------------------------------------------------------------------------- supervision

    def check_silence(self, now: datetime) -> bool:
        """True (once per silence) if the feed has been quiet for longer than ``silence_s``."""
        if self.last_message_at is None or self._silent:
            return False
        if (now - self.last_message_at).total_seconds() <= self._silence_s:
            return False
        self._enter_trouble("feed silent")
        return True

    def _enter_trouble(self, reason: str) -> None:
        """The live data cannot be trusted from the last good message on, until the next message arrives."""
        if self._silent:
            return
        self._silent = True
        log.warning("ws_trouble", reason=reason, since=self.last_message_at)
        for book in self.books.values():
            book.mark_stale(reason)
        if self._on_damage is not None and self.last_message_at is not None:
            self._on_damage(self.last_message_at, None)  # open interval: closed at the first message after it

    # ------------------------------------------------------------------------------ asyncio

    def _on_message_thread(self, raw: str) -> None:
        """Called by the SDK thread: hand the message to the event loop together with its arrival time."""
        if self._loop is not None and self._queue is not None:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, (time.time(), raw))

    def _on_close_thread(self) -> None:
        if self._loop is not None and self._queue is not None:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, (time.time(), None))

    async def _connect(self) -> None:
        self._transport = self._factory(self._on_message_thread, self._on_close_thread)
        await asyncio.to_thread(self._transport.open)
        await asyncio.to_thread(self._transport.subscribe, self.products, CHANNELS)

    async def _disconnect(self) -> None:
        transport, self._transport = self._transport, None
        if transport is not None:
            try:
                await asyncio.to_thread(transport.close)
            except Exception as exc:  # closing a dead socket may raise: nothing more to do about it
                log.debug("ws_close_failed", error=str(exc))

    async def _reconnect(self, reason: str) -> None:
        self.stats.reconnects += 1
        log.warning("ws_reconnecting", reason=reason, backoff_s=self._backoff)
        self._enter_trouble(reason)
        await self._disconnect()
        await asyncio.sleep(self._backoff)
        self._backoff = min(self._backoff * 2, BACKOFF_MAX_S)
        try:
            await self._connect()
        except Exception as exc:
            log.error("ws_connect_failed", error=f"{type(exc).__name__}: {exc}")

    async def _flush_resubscribes(self) -> None:
        if not self._resubscribe or self._transport is None:
            return
        products, self._resubscribe = sorted(self._resubscribe), set()
        self.stats.resubscribes += len(products)
        try:
            await asyncio.to_thread(self._transport.unsubscribe, products, ["level2"])
            await asyncio.to_thread(self._transport.subscribe, products, ["level2"])
        except Exception as exc:
            log.error("ws_resubscribe_failed", error=f"{type(exc).__name__}: {exc}")
            self._resubscribe.update(products)

    async def run(self) -> None:
        """Connect and keep the feed alive until cancelled."""
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        while True:  # the very first connection may fail too: keep trying with backoff
            try:
                await self._connect()
                break
            except Exception as exc:
                log.error("ws_connect_failed", error=f"{type(exc).__name__}: {exc}", retry_in_s=self._backoff)
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, BACKOFF_MAX_S)
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._consume())
                tg.create_task(self._watchdog())
        finally:
            await self._disconnect()

    async def _consume(self) -> None:
        assert self._queue is not None
        while True:
            recv_ts, raw = await self._queue.get()
            if raw is None:
                await self._reconnect("connection closed")
                continue
            self._backoff = BACKOFF_MIN_S  # a message arrived: the connection works
            self.handle_raw(recv_ts, raw)
            await self._flush_resubscribes()

    async def _watchdog(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            if self.check_silence(self._clock()):
                await self._reconnect("feed silent")
