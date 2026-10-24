"""Read-only soak of the Coinbase ``user`` channel (spec sez. 4.1.5 and 4.3 punti 8-9).

::

    python -m script.user_channel_soak --minutes 45 --out <folder> [--inject abort@12 --inject silence@24:12 ...]

The ``user`` channel was implemented from the documentation and never tried with a real key. A View key with no orders
receives no order messages, so this soak can validate: authentication and subscription, the acks, the heartbeat
cadence, the continuity of ``sequence_num``, what the stream does on a reconnect and on a silent feed, the gap
detection with its reconciliation request, and the real ``Reconciler.on_startup()`` read against an empty ledger. It
can NOT validate the format of order and fill messages (see docs/decisions/2026-10-20-canale-user-p4.md).

It drives the SAME classes as the application (``CoinbaseUserStream`` over ``CoinbaseUserTransport``, the real parser,
the real ``Reconciler``), not a parallel client. The only additions are taps: a transport wrapper that timestamps and
records every raw message, and a read-only proxy in front of the exchange. Rules:

* the key must be View only: it refuses to start (exit 4) if it can trade or transfer;
* nothing is written outside ``--out``; the reconciler runs against an in-memory ledger;
* nothing here can send, change or cancel an order: the exchange the reconciler sees is an allowlist of reads;
* secrets never reach a file: every raw message and log line goes through ``redact`` (JWTs, the key name, the PEM
  key); the report holds counts and currency codes, never amounts.

``--inject KIND@MINUTE[:SECONDS]`` forces the faults a quiet account never produces, to watch the real code react:
``close`` (the SDK socket is closed and calls ``on_close``), ``abort`` (the socket dies without any callback, like a
server-side drop), ``silence`` (messages are received but not handed to the stream for SECONDS) and ``refuse``
(abort, then every new connection fails for SECONDS). They are labelled in the report.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import gzip
import json
import logging
import math
import os
import re
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, Protocol

import structlog

from modules.config import CoinbaseConfig, FallbackFees, MarketConfig
from modules.execution.balance import AccountSnapshot, BalanceBook, CoverageAlarm
from modules.execution.exchange import BestBidAsk, FillInfo, OrderInfo, build_exchange
from modules.execution.reconciler import Reconciler, ReconcileReport
from modules.execution.spot import SPOT_EXIT_CODE, SpotRegistry
from modules.execution.user_stream import (
    CoinbaseUserStream,
    CoinbaseUserTransport,
    OrderEventSink,
    TransportFactory,
    UserTransport,
    parse_user_message,
)
from modules.ledger.positions import PositionLedger
from modules.market.ws_feed import ErrorEvent, Heartbeat, SequenceMonitor, Subscribed
from modules.risk.costs import FeeProvider

EXIT_OK: Final = 0
EXIT_CHECKS_FAILED: Final = 1
EXIT_CONFIG: Final = 2
EXIT_WRITE_KEY: Final = SPOT_EXIT_CODE  # a key that can trade or transfer: same code as the application

BOOK: Final = "soak"
REPORT_JSON: Final = "user_channel_soak.json"
REPORT_MD: Final = "user_channel_soak.md"
RAW_NAME: Final = "user_channel_raw.jsonl.gz"
LOG_NAME: Final = "soak.log"
REDACTED: Final = "[REDACTED]"
FLUSH_EVERY_S: Final = 5.0
SAMPLE_LIMIT: Final = 20

NOT_VALIDATED: Final = (
    "format of order messages after an order is placed (new, open, partial fill, filled, cancelled, expired, failed)",
    "format and timing of fills, and their relation to REST get_fills",
    "behaviour of the sink on real fills, and of the BalanceBook when a fill arrives from the stream",
    "bracket (trigger) orders: fields stop_price / trigger_status, and what the channel says when one triggers",
    "client_order_id echo on order messages (Chronos' own orders versus manual ones)",
)

FaultKind = Literal["close", "abort", "silence", "refuse"]


# --------------------------------------------------------------------------------------- redaction

_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}(?:\.[A-Za-z0-9_-]*)?")
_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)
_KEY_NAME = re.compile(r"organizations/[\w-]+/api[Kk]eys/[\w-]+")
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")
_SECRET_FIELD = re.compile(
    r'("(?:jwt|token|access_token|id_token|signature|api_key|api_secret|private_key|secret|retail_portfolio_id)"'
    r'\s*:\s*")[^"]*(")',
    re.I,
)
# Amounts and ids that structlog fields of the repo's modules may carry: never written to the soak log
_LOG_OMIT = frozenset(
    {"detail", "stored", "reported", "qty", "size", "limit_price", "leaves", "tp", "sl", "released", "balances"}
)


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Remove every credential-looking string from ``text``: known secrets first, then JWTs, PEM blocks, key names,
    bearer tokens and credential fields of a JSON object."""
    for secret in sorted(set(secrets), key=len, reverse=True):
        if len(secret) >= 8:
            text = text.replace(secret, REDACTED)
    text = _PEM.sub(REDACTED, text)
    text = _JWT.sub(REDACTED, text)
    text = _KEY_NAME.sub(REDACTED, text)
    text = _BEARER.sub(r"\1" + REDACTED, text)
    return _SECRET_FIELD.sub(r"\1" + REDACTED + r"\2", text)


def secrets_of(cfg: CoinbaseConfig) -> list[str]:
    """Every form in which the credentials could show up in a message or a log line."""
    out: list[str] = []
    if cfg.api_key is not None:
        out.append(cfg.api_key.get_secret_value())
    if cfg.api_secret is not None:
        pem = cfg.api_secret.get_secret_value()
        out += [pem, pem.replace("\n", "\\n"), pem.replace("\n", "\\\\n")]
        out += [line.strip() for line in pem.splitlines() if len(line.strip()) >= 20]
    return out


# ------------------------------------------------------------------------------------------ stats


def percentile(sorted_values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile of an ASCENDING sequence."""
    rank = max(math.ceil(q * len(sorted_values)) - 1, 0)
    return sorted_values[min(rank, len(sorted_values) - 1)]


def summarize(values: Sequence[float]) -> dict[str, float | int] | None:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    median = s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2
    return {
        "n": len(s), "min": round(s[0], 4), "median": round(median, 4), "p99": round(percentile(s, 0.99), 4),
        "max": round(s[-1], 4),
    }  # fmt: skip


def iso(epoch: float | None) -> str | None:
    return None if epoch is None else datetime.fromtimestamp(epoch, UTC).isoformat(timespec="milliseconds")


# ------------------------------------------------------------------------------------- raw record


class RawLog:
    """Gzip JSON lines in the output folder. Flushed (sync flush) every few seconds, so a killed process leaves a
    readable file up to the last flush. A second run in the same folder opens a ``.partN`` file."""

    def __init__(self, folder: Path, clock: Callable[[], float] = time.time) -> None:
        path = folder / RAW_NAME
        part = 1
        while path.exists():
            part += 1
            path = folder / f"{RAW_NAME}.part{part}"
        self.path = path
        self._clock = clock
        self._file = gzip.open(path, "wt", encoding="utf-8")
        self._last_flush = clock()

    def write(self, entry: Mapping[str, Any]) -> None:
        if self._file.closed:  # a straggler from the SDK thread after the run ended
            return
        self._file.write(json.dumps(entry, separators=(",", ":")) + "\n")
        now = self._clock()
        if now - self._last_flush >= FLUSH_EVERY_S:
            self._file.flush()
            self._last_flush = now

    def close(self) -> None:
        self._file.close()


# --------------------------------------------------------------------------------- the recorder


@dataclass
class Connection:
    """What happened on one connection attempt (a call of the transport factory)."""

    index: int
    started_t: float
    monitor: SequenceMonitor = field(default_factory=SequenceMonitor)
    opened_t: float | None = None
    subscribed_t: float | None = None
    ack_t: float | None = None
    first_message_t: float | None = None
    last_message_t: float | None = None
    close_called_t: float | None = None
    on_close_t: float | None = None
    open_error: str | None = None
    messages: int = 0
    first_seq: int | None = None
    last_seq: int | None = None
    auth: str = "pending"
    acks: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    heartbeat_times: list[float] = field(default_factory=list)
    heartbeat_counters: list[int] = field(default_factory=list)
    seq_gaps: list[tuple[int, int]] = field(default_factory=list)  # (last, got)
    seq_resets: int = 0
    snapshot_order_ids: set[str] = field(default_factory=set)
    snapshot_statuses: Counter[str] = field(default_factory=Counter)
    lags: list[float] = field(default_factory=list)

    def public(self, t0: float) -> dict[str, Any]:
        def rel(t: float | None) -> float | None:
            return None if t is None else round(t - t0, 3)

        def span(a: float | None, b: float | None) -> float | None:
            return None if a is None or b is None else round(b - a, 3)

        return {
            "index": self.index,
            "started_at_s": rel(self.started_t),
            "open_s": span(self.started_t, self.opened_t),
            "subscribe_to_ack_s": span(self.subscribed_t, self.ack_t),
            "first_message_after_start_s": span(self.started_t, self.first_message_t),
            "last_message_at_s": rel(self.last_message_t),
            "close_called_at_s": rel(self.close_called_t),
            "on_close_callback_at_s": rel(self.on_close_t),
            "open_error": self.open_error,
            "messages": self.messages,
            "first_sequence_num": self.first_seq,
            "last_sequence_num": self.last_seq,
            "authentication": self.auth,
            "subscriptions_confirmed": sorted(set(self.acks)),
            "errors": self.errors[:SAMPLE_LIMIT],
            "sequence_gaps": [{"after": a, "got": b} for a, b in self.seq_gaps],
            "sequence_resets": self.seq_resets,
            "heartbeats": len(self.heartbeat_times),
            "snapshot_orders": len(self.snapshot_order_ids),
        }


class SoakRecorder:
    """Thread-safe: ``on_message`` runs in the SDK's thread. Everything it keeps is counts and timings."""

    def __init__(
        self, *, secrets: Sequence[str] = (), raw_log: RawLog | None = None, clock: Callable[[], float] = time.time
    ) -> None:
        self._lock = threading.Lock()
        self._secrets = tuple(secrets)
        self._raw = raw_log
        self.clock = clock
        self.connections: list[Connection] = []
        self.type_counts: Counter[str] = Counter()
        self.parse_errors = 0
        self.parse_error_samples: list[str] = []
        self.dropped_by_injection = 0
        self.messages = 0

    # -- lifecycle of a connection
    def new_connection(self) -> Connection:
        with self._lock:
            conn = Connection(len(self.connections) + 1, self.clock())
            self.connections.append(conn)
            return conn

    def opened(self, conn: Connection) -> None:
        with self._lock:
            conn.opened_t = self.clock()

    def open_failed(self, conn: Connection, error: str) -> None:
        with self._lock:
            conn.open_error = redact(error, self._secrets)[:300]

    def subscribe_sent(self, conn: Connection) -> None:
        with self._lock:
            conn.subscribed_t = self.clock()

    def close_called(self, conn: Connection) -> None:
        with self._lock:
            conn.close_called_t = conn.close_called_t or self.clock()

    def on_close_callback(self, conn: Connection) -> None:
        with self._lock:
            conn.on_close_t = self.clock()

    # -- messages
    def on_message(self, conn: Connection, t: float, raw: str, *, delivered: bool) -> None:
        with self._lock:
            self.messages += 1
            conn.messages += 1
            conn.first_message_t = conn.first_message_t or t
            conn.last_message_t = t
            if not delivered:
                self.dropped_by_injection += 1
            clean = redact(raw, self._secrets)
            if self._raw is not None:
                self._raw.write({"t": round(t, 6), "conn": conn.index, "delivered": delivered, **self._body(clean)})
            self._classify(clean)
            self._parse(conn, t, raw, clean)

    @staticmethod
    def _body(clean: str) -> dict[str, Any]:
        try:
            return {"msg": json.loads(clean)}
        except ValueError:
            return {"raw": clean[:2000]}

    def _classify(self, clean: str) -> None:
        try:
            data = json.loads(clean)
        except ValueError:
            self.type_counts["not_json"] += 1
            return
        if not isinstance(data, Mapping):
            self.type_counts["not_an_object"] += 1
        elif data.get("type") == "error":
            self.type_counts["error"] += 1
        elif data.get("channel") == "user":
            events = data.get("events") or []
            kinds = [str(e.get("type")) for e in events if isinstance(e, Mapping)] or ["empty"]
            for kind in kinds:
                self.type_counts[f"user/{kind}"] += 1
        else:
            self.type_counts[str(data.get("channel"))] += 1

    def _parse(self, conn: Connection, t: float, raw: str, clean: str) -> None:
        try:
            env, updates = parse_user_message(raw)
        except (ValueError, KeyError, TypeError) as exc:
            self.parse_errors += 1
            if len(self.parse_error_samples) < SAMPLE_LIMIT:
                self.parse_error_samples.append(f"{type(exc).__name__}: {exc} :: {clean[:300]}")
            return
        if env.sequence_num is not None:
            if conn.first_seq is None:
                conn.first_seq = env.sequence_num
            verdict = conn.monitor.check(env.sequence_num)
            if verdict == "gap":
                conn.seq_gaps.append((conn.last_seq if conn.last_seq is not None else -1, env.sequence_num))
            elif verdict == "reset":
                conn.seq_resets += 1
            conn.last_seq = env.sequence_num
        if env.ts is not None:
            conn.lags.append(t - env.ts.timestamp())
        for event in env.events:
            if isinstance(event, Heartbeat):
                conn.heartbeat_times.append(t)
                counter = self._heartbeat_counter(clean)
                if counter is not None:
                    conn.heartbeat_counters.append(counter)
            elif isinstance(event, Subscribed):
                conn.acks.extend(event.channels)
                if "user" in event.channels and conn.ack_t is None:
                    conn.ack_t = t
                    conn.auth = "ok" if not conn.errors else conn.auth
            elif isinstance(event, ErrorEvent):
                conn.errors.append(redact(event.message, self._secrets)[:300])
                if conn.auth == "pending":
                    conn.auth = f"error: {conn.errors[-1]}"
        for update in updates:
            if update.snapshot:
                conn.snapshot_order_ids.add(update.order.order_id)
                conn.snapshot_statuses[update.order.status] += 1

    @staticmethod
    def _heartbeat_counter(clean: str) -> int | None:
        try:
            events = json.loads(clean).get("events") or []
            return int(events[0]["heartbeat_counter"])
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            return None

    # -- read back
    def counts(self) -> dict[str, int]:
        with self._lock:
            return {
                "connections": len(self.connections),
                "messages": self.messages,
                "heartbeats": sum(len(c.heartbeat_times) for c in self.connections),
                "sequence_gaps": sum(len(c.seq_gaps) for c in self.connections),
                "parse_errors": self.parse_errors,
            }

    def snapshot(self) -> tuple[list[Connection], Counter[str], int, list[str], int]:
        with self._lock:
            return (
                list(self.connections), Counter(self.type_counts), self.parse_errors,
                list(self.parse_error_samples), self.dropped_by_injection,
            )  # fmt: skip


# ----------------------------------------------------------------------------------- injected faults


@dataclass(frozen=True)
class Fault:
    kind: FaultKind
    at_s: float
    duration_s: float = 0.0


def parse_fault(spec: str) -> Fault:
    """``KIND@MINUTES[:SECONDS]``, e.g. ``abort@12`` or ``silence@24:12``."""
    kind, sep, rest = spec.partition("@")
    if not sep or kind not in ("close", "abort", "silence", "refuse"):
        raise ValueError(f"fault {spec!r}: expected close|abort|silence|refuse@MINUTES[:SECONDS]")
    minutes, _, seconds = rest.partition(":")
    duration = float(seconds) if seconds else 0.0
    if kind in ("silence", "refuse") and duration <= 0:
        raise ValueError(f"fault {spec!r}: {kind} needs a duration, e.g. {kind}@{minutes or '10'}:15")
    return Fault(kind, float(minutes) * 60.0, duration)  # type: ignore[arg-type]


class FaultState:
    """The switches the transport wrapper consults: a connection is silenced (its messages are received and recorded
    but not handed to the stream) or new connections are refused (the factory raises). A silence belongs to the
    connection that was current when it began: the one that replaces it is healthy."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._silent: dict[int, float] = {}  # connection index -> end of the silence
        self._refuse_until = 0.0

    def silence_for(self, seconds: float, connection: int) -> None:
        self._silent[connection] = self._clock() + seconds

    def refuse_for(self, seconds: float) -> None:
        self._refuse_until = self._clock() + seconds

    def silenced(self, connection: int) -> bool:
        return self._clock() < self._silent.get(connection, 0.0)

    @property
    def refusing(self) -> bool:
        return self._clock() < self._refuse_until


# ------------------------------------------------------------------------------------- the taps


class SdkTransport(CoinbaseUserTransport):
    """The production transport plus ``abort``: kill the socket the way a server-side drop does (no ``on_close``
    callback, the SDK handler just ends). Used only by the fault injector."""

    def abort(self) -> None:
        sdk = self._ws
        loop, websocket = sdk.loop, sdk.websocket
        if loop is None or websocket is None:
            raise RuntimeError("the websocket is not open")
        future = asyncio.run_coroutine_threadsafe(websocket.close(), loop)
        with contextlib.suppress(TimeoutError):  # the close handshake may take up to 10 s; the socket is gone anyway
            future.result(1)


class ObservedTransport:
    """Wraps a ``UserTransport``: same interface for the stream, timings for the recorder."""

    def __init__(self, inner: UserTransport, conn: Connection, recorder: SoakRecorder) -> None:
        self.inner = inner
        self.conn = conn
        self._recorder = recorder

    def open(self) -> None:
        try:
            self.inner.open()
        except Exception as exc:
            self._recorder.open_failed(self.conn, f"{type(exc).__name__}: {exc}")
            raise
        self._recorder.opened(self.conn)

    def subscribe(self, product_ids: Sequence[str], channels: Sequence[str]) -> None:
        self._recorder.subscribe_sent(self.conn)
        self.inner.subscribe(product_ids, channels)

    def close(self) -> None:
        self._recorder.close_called(self.conn)
        self.inner.close()

    def abort(self) -> None:
        abort = getattr(self.inner, "abort", None)
        if abort is None:
            self.inner.close()  # a transport that cannot die silently: the closest thing
            return
        abort()


class SoakTransportFactory:
    """A ``TransportFactory`` for ``CoinbaseUserStream``: every connection gets a ``Connection`` record; messages are
    timestamped on arrival (SDK thread) before the stream sees them."""

    def __init__(
        self,
        inner: TransportFactory,
        recorder: SoakRecorder,
        faults: FaultState,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._inner = inner
        self._recorder = recorder
        self._faults = faults
        self._clock = clock
        self.current: ObservedTransport | None = None

    def __call__(self, on_message: Callable[[str], None], on_close: Callable[[], None]) -> UserTransport:
        conn = self._recorder.new_connection()
        if self._faults.refusing:
            self._recorder.open_failed(conn, "ConnectionError: injected refusal")
            raise ConnectionError("injected refusal")

        def message(raw: str) -> None:
            delivered = not self._faults.silenced(conn.index)
            self._recorder.on_message(conn, self._clock(), raw, delivered=delivered)
            if delivered:
                on_message(raw)

        def closed() -> None:
            self._recorder.on_close_callback(conn)
            on_close()

        try:
            inner = self._inner(message, closed)
        except Exception as exc:
            self._recorder.open_failed(conn, f"{type(exc).__name__}: {exc}")
            raise
        self.current = ObservedTransport(inner, conn, self._recorder)
        return self.current


class ReadTap:
    """What the reconciler and the sink may ask the exchange: an allowlist of READS, with a summary of what came
    back (counts, currency codes, product ids; never amounts). Anything else raises."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.account_reads: list[dict[str, Any]] = []
        self.open_order_reads: list[dict[str, Any]] = []
        self.first_snapshot: AccountSnapshot | None = None
        self.open_order_ids: list[set[str]] = []

    async def get_accounts(self) -> AccountSnapshot:
        snap: AccountSnapshot = await self._inner.get_accounts()
        nonzero = sorted(c for c, (a, h) in snap.balances.items() if a > 0 or h > 0)
        self.account_reads.append(
            {
                "accounts": len(snap.balances), "currencies_with_balance": nonzero,
                "currencies_with_hold": sorted(c for c, (_, h) in snap.balances.items() if h > 0),
            }  # fmt: skip
        )
        if self.first_snapshot is None:
            self.first_snapshot = snap
        return snap

    async def list_open_orders(self) -> list[OrderInfo]:
        orders: list[OrderInfo] = await self._inner.list_open_orders()
        self.open_order_ids.append({o.order_id for o in orders})
        self.open_order_reads.append(
            {
                "count": len(orders), "by_product": dict(Counter(o.product_id for o in orders)),
                "by_side": dict(Counter(o.side for o in orders)), "by_status": dict(Counter(o.status for o in orders)),
            }  # fmt: skip
        )
        return orders

    async def list_orders(self, **kwargs: Any) -> list[OrderInfo]:
        result: list[OrderInfo] = await self._inner.list_orders(**kwargs)
        return result

    async def get_best_bid_ask(self, product_id: str) -> BestBidAsk:
        result: BestBidAsk = await self._inner.get_best_bid_ask(product_id)
        return result

    async def get_fills(self, order_id: str) -> list[FillInfo]:
        result: list[FillInfo] = await self._inner.get_fills(order_id)
        return result

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        raise AssertionError(f"the soak asked the exchange for {name!r}: it may only read")


# ----------------------------------------------------------------------------------- the run


class SoakExchange(Protocol):
    """The real ``CoinbaseExchange`` (read only) or a fake."""

    async def get_api_key_permissions(self) -> Any: ...
    async def get_product_raw(self, product_id: str) -> Mapping[str, Any]: ...
    async def get_accounts(self) -> AccountSnapshot: ...
    async def list_open_orders(self) -> list[OrderInfo]: ...
    async def list_orders(self, **kwargs: Any) -> list[OrderInfo]: ...
    async def get_best_bid_ask(self, product_id: str) -> BestBidAsk: ...
    async def get_fills(self, order_id: str) -> list[FillInfo]: ...


@dataclass(frozen=True)
class SoakOptions:
    out: Path
    duration_s: float
    products: tuple[str, ...]
    faults: tuple[Fault, ...] = ()
    reconcile_interval_s: float = 300.0
    silence_s: float = 5.0
    progress_s: float = 30.0
    heartbeat_s: float = 1.0  # the cadence the exchange documents; the check accepts half to double of it
    overwrite: bool = False


@dataclass
class StartupResult:
    error: str | None = None
    elapsed_s: float | None = None
    report: dict[str, Any] | None = None
    alarms: list[dict[str, str]] = field(default_factory=list)
    comparison: dict[str, Any] | None = None


def _report_counts(report: ReconcileReport) -> dict[str, Any]:
    return {k: v for k, v in vars(report).items() if isinstance(v, int | bool) and not isinstance(v, datetime)} | {
        "notes": len(report.notes)
    }


def build_reconciler(
    tap: ReadTap, registry: SpotRegistry, ledger: PositionLedger, book: BalanceBook, alarms: list[CoverageAlarm],
    interval_s: float,
) -> tuple[Reconciler, OrderEventSink]:
    """The application's reconciler and sink over an EMPTY in-memory ledger and the read-only tap."""
    fallback = FallbackFees()
    sink = OrderEventSink(
        book=BOOK, ledger=ledger, balances=book, registry=registry, fills=tap, on_alarm=alarms.append
    )  # fmt: skip
    reconciler = Reconciler(
        book=BOOK, exchange=tap, ledger=ledger, balances=book, registry=registry, sink=sink,
        fees=FeeProvider(fallback), fallback=fallback, interval_s=interval_s, on_alarm=alarms.append,
    )  # fmt: skip
    return reconciler, sink


async def reconcile_startup(
    reconciler: Reconciler, tap: ReadTap, book: BalanceBook, alarms: list[CoverageAlarm]
) -> StartupResult:
    """The real ``Reconciler.on_startup()`` against the real account and an empty ledger."""
    result = StartupResult()
    started = time.monotonic()
    try:
        report = await reconciler.on_startup()
    except Exception as exc:  # the soak goes on: the failure IS a finding
        result.error = f"{type(exc).__name__}: {exc}"
    else:
        result.report = _report_counts(report)
        result.comparison = _compare_book(tap, book)
    result.elapsed_s = round(time.monotonic() - started, 3)
    result.alarms = [{"kind": a.kind, "currency": a.currency} for a in alarms]
    return result


def _compare_book(tap: ReadTap, book: BalanceBook) -> dict[str, Any]:
    """Real balances and open orders against the empty ledger. Counts and currency codes only."""
    snap = tap.first_snapshot
    differs = (
        []
        if snap is None
        else sorted(c for c, (available, _) in snap.balances.items() if book.free(c) != available)
    )  # fmt: skip
    first_orders = tap.open_order_reads[0] if tap.open_order_reads else None
    return {
        "exchange_open_orders": None if first_orders is None else first_orders["count"],
        "open_orders_by_product": None if first_orders is None else first_orders["by_product"],
        "open_orders_by_side": None if first_orders is None else first_orders["by_side"],
        "ledger_orders": 0,
        "currencies_with_balance": [] if not tap.account_reads else tap.account_reads[0]["currencies_with_balance"],
        "currencies_with_hold": [] if not tap.account_reads else tap.account_reads[0]["currencies_with_hold"],
        "accounts_read": 0 if not tap.account_reads else tap.account_reads[0]["accounts"],
        "book_free_differs_from_available": differs,
        "book_reservations": 0,
    }


def _heartbeats(conns: Sequence[Connection]) -> dict[str, Any]:
    deltas = [b - a for c in conns for a, b in zip(c.heartbeat_times, c.heartbeat_times[1:], strict=False)]
    jumps = sum(
        1 for c in conns for a, b in zip(c.heartbeat_counters, c.heartbeat_counters[1:], strict=False) if b != a + 1
    )
    return {
        "count": sum(len(c.heartbeat_times) for c in conns),
        "interval_s": summarize(deltas),
        "counter_jumps": jumps,
        "counter_seen": any(c.heartbeat_counters for c in conns),
    }


def _recoveries(conns: Sequence[Connection]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    previous: Connection | None = None
    for conn in conns:
        if conn.open_error is not None and conn.first_message_t is None:
            out.append({"connection": conn.index, "outcome": "failed", "error": conn.open_error})
            continue
        if previous is not None and previous.last_message_t is not None and conn.first_message_t is not None:
            out.append(
                {
                    "connection": conn.index, "outcome": "recovered",
                    "outage_s": round(conn.first_message_t - previous.last_message_t, 3),
                    "connect_to_ack_s": None if conn.ack_t is None else round(conn.ack_t - conn.started_t, 3),
                }  # fmt: skip
            )
        elif previous is not None:
            out.append({"connection": conn.index, "outcome": "no_message"})
        previous = conn
    return out


def build_report(
    *,
    opts: SoakOptions,
    started: float,
    ended: float,
    ended_reason: str,
    perms: Mapping[str, Any],
    recorder: SoakRecorder,
    stream: CoinbaseUserStream | None,
    startup: StartupResult,
    reconcile_requests: Sequence[tuple[float, str]],
    reconciler: Reconciler,
    tap: ReadTap,
    applied_faults: Sequence[Mapping[str, Any]],
    alarms: Sequence[CoverageAlarm],
    threads_start: int = 0,
) -> dict[str, Any]:
    conns, types, parse_errors, samples, dropped = recorder.snapshot()
    delivered_conns = [c for c in conns if c.messages]
    confirmed = sorted({ch for c in conns for ch in c.acks})
    auths = [c.auth for c in delivered_conns]
    heartbeats = _heartbeats(conns)
    gaps = [{"connection": c.index, "after": a, "got": b} for c in conns for a, b in c.seq_gaps]
    recoveries = _recoveries(conns)
    stats = None if stream is None else stream.stats
    snapshot_ids = [c.snapshot_order_ids for c in conns if c.snapshot_order_ids or c.ack_t]
    rest_ids = tap.open_order_ids[0] if tap.open_order_ids else None
    first_ws = snapshot_ids[0] if snapshot_ids else None
    lags = [lag for c in conns for lag in c.lags]
    expected = sum(1 for f in applied_faults if str(f.get("result")).startswith("applied"))
    reconnects = 0 if stats is None else stats.reconnects
    first_stream_msg = next((c for c in conns if c.first_message_t is not None), None)
    report: dict[str, Any] = {
        "schema": 1,
        "started_at": iso(started), "ended_at": iso(ended), "ended_reason": ended_reason,
        "planned_s": opts.duration_s, "duration_s": round(ended - started, 1),
        "products": list(opts.products),
        "key": {k: perms.get(k) for k in ("can_view", "can_trade", "can_transfer", "portfolio_type")},
        "authentication": {"per_connection": auths, "ok": "ok" in auths},
        "connect_time_s": None if first_stream_msg is None else first_stream_msg.public(started)["open_s"],
        "subscriptions_confirmed": confirmed,
        "connections": [c.public(started) for c in conns],
        "messages": {
            "total": recorder.messages, "by_type": dict(sorted(types.items())),
            "dropped_by_injection": dropped,
        },  # fmt: skip
        "heartbeats": heartbeats,
        "feed_lag_s": summarize(lags),
        "sequence": {
            "gaps": gaps, "gap_count": len(gaps), "resets": sum(c.seq_resets for c in conns),
            "first_sequence_num_per_connection": [c.first_seq for c in delivered_conns],
        },  # fmt: skip
        "reconnects": {
            "count": reconnects, "expected_from_injection": expected, "recoveries": recoveries,
            "unexpected": max(reconnects - expected, 0),
        },  # fmt: skip
        "parse": {
            "errors": parse_errors, "samples": samples, "stream_errors": None if stats is None else stats.parse_errors,
        },  # fmt: skip
        "stream_stats": None if stats is None else {
            k: v for k, v in vars(stats).items() if k != "errors"
        } | {"error_messages": [redact(m)[:300] for m in stats.errors[:SAMPLE_LIMIT]]},  # fmt: skip
        "reconcile_requests": [{"at_s": round(t - started, 3), "reason": r} for t, r in reconcile_requests],
        "reconciler_startup": {
            "error": startup.error, "elapsed_s": startup.elapsed_s, "report": startup.report,
            "comparison": startup.comparison,
        },  # fmt: skip
        "ws_snapshot_vs_rest": {
            "ws_snapshot_orders": None if first_ws is None else len(first_ws),
            "rest_open_orders": None if rest_ids is None else len(rest_ids),
            "ws_not_in_rest": None if first_ws is None or rest_ids is None else len(first_ws - rest_ids),
            "rest_not_in_ws": None if first_ws is None or rest_ids is None else len(rest_ids - first_ws),
            "ws_snapshot_statuses": dict(sum((c.snapshot_statuses for c in conns), Counter())),
        },  # fmt: skip
        "reconciler_runtime": {
            "consecutive_failures": reconciler.consecutive_failures, "last_error": reconciler.last_error,
            "last_reason": None if reconciler.last_report is None else reconciler.last_report.reason,
            "reads": {"accounts": len(tap.account_reads), "open_orders": len(tap.open_order_reads)},
        },  # fmt: skip
        "coverage_alarms": [{"kind": a.kind, "currency": a.currency} for a in alarms],
        "injected_faults": list(applied_faults),
        "process": {"threads_start": threads_start, "threads_end": threading.active_count()},
        "not_validated": list(NOT_VALIDATED),
    }
    report["checks"] = _checks(report, opts)
    return report


def _checks(report: Mapping[str, Any], opts: SoakOptions) -> dict[str, dict[str, Any]]:
    hb = report["heartbeats"]["interval_s"]
    startup = report["reconciler_startup"]
    outcomes = [r["outcome"] for r in report["reconnects"]["recoveries"]]
    checks: dict[str, tuple[bool, str]] = {
        "key_is_read_only": (
            not report["key"]["can_trade"] and not report["key"]["can_transfer"],
            f"can_trade={report['key']['can_trade']} can_transfer={report['key']['can_transfer']}",
        ),
        "authenticated": (report["authentication"]["ok"], str(report["authentication"]["per_connection"])),
        "user_subscription_confirmed": (
            "user" in report["subscriptions_confirmed"], str(report["subscriptions_confirmed"])
        ),  # fmt: skip
        "heartbeats_subscription_confirmed": (
            "heartbeats" in report["subscriptions_confirmed"] and report["heartbeats"]["count"] > 0,
            f"{report['heartbeats']['count']} heartbeats",
        ),
        "heartbeat_cadence_about_1s": (
            hb is not None and 0.5 * opts.heartbeat_s <= hb["median"] <= 2.0 * opts.heartbeat_s,
            "no heartbeat" if hb is None else f"median {hb['median']} s",
        ),
        "no_parse_errors": (
            report["parse"]["errors"] == 0 and not report["parse"]["stream_errors"],
            f"recorder {report['parse']['errors']}, stream {report['parse']['stream_errors']}",
        ),
        "sequence_continuous": (report["sequence"]["gap_count"] == 0, f"{report['sequence']['gap_count']} gaps"),
        "no_unexpected_reconnects": (
            report["reconnects"]["unexpected"] == 0,
            f"{report['reconnects']['count']} reconnects, {report['reconnects']['expected_from_injection']} injected",
        ),
        "every_outage_recovered": (
            # refused attempts are allowed (they are retried); a connection that stays mute, or an outage that never
            # ends, is not
            "no_message" not in outcomes and (not outcomes or outcomes[-1] == "recovered"),
            f"{outcomes.count('recovered')} recovered, {outcomes.count('failed')} refused attempts, "
            f"{outcomes.count('no_message')} mute",
        ),
        "every_reconnect_asked_for_a_reconciliation": (
            sum(1 for r in report["reconcile_requests"] if r["reason"].startswith("ws_"))
            >= report["reconnects"]["count"],
            f"{len(report['reconcile_requests'])} requests",
        ),
        "reconciler_startup_ok": (startup["error"] is None, startup["error"] or "ok"),
        "reconciler_book_matches_exchange": (
            bool(startup["comparison"]) and not startup["comparison"]["book_free_differs_from_available"],
            "no comparison"
            if not startup["comparison"]
            else str(startup["comparison"]["book_free_differs_from_available"]),
        ),
        "reconciler_found_all_orders_foreign": (
            bool(startup["report"])
            and startup["comparison"] is not None
            and startup["report"]["foreign_orders"] == startup["comparison"]["exchange_open_orders"],
            "n/a" if not startup["report"] else f"foreign {startup['report']['foreign_orders']}",
        ),
        "no_coverage_alarms": (not report["coverage_alarms"], f"{len(report['coverage_alarms'])} alarms"),
        "ran_the_planned_time": (
            report["ended_reason"] == "completed" and report["duration_s"] >= opts.duration_s - 2,
            f"{report['ended_reason']}, {report['duration_s']} s of {opts.duration_s}",
        ),
    }
    return {name: {"pass": ok, "detail": detail} for name, (ok, detail) in checks.items()}


def render_markdown(report: Mapping[str, Any]) -> str:
    def row(*cells: Any) -> str:
        return "| " + " | ".join(str(c) for c in cells) + " |"

    msgs, hb, seq, rec = report["messages"], report["heartbeats"], report["sequence"], report["reconnects"]
    interval = hb["interval_s"]
    lines = [
        "# Soak del canale `user` (sola lettura)", "",
        f"- Inizio {report['started_at']}, fine {report['ended_at']} ({report['ended_reason']}), "
        f"durata {report['duration_s']} s su {report['planned_s']} s previsti",
        f"- Chiave: {report['key']} (nessun ordine inviato, nessun permesso di scrittura)",
        f"- Prodotti: {', '.join(report['products'])}",
        f"- Autenticazione: {'OK' if report['authentication']['ok'] else 'FALLITA'} "
        f"{report['authentication']['per_connection']}",
        f"- Tempo di connessione (open): {report['connect_time_s']} s; "
        f"sottoscrizioni confermate: {report['subscriptions_confirmed']}",
        "", "## Esito dei controlli", "", row("controllo", "esito", "dettaglio"), row("---", "---", "---"),
    ]  # fmt: skip
    for name, c in report["checks"].items():
        lines.append(row(name, "PASS" if c["pass"] else "FAIL", c["detail"]))
    lines += ["", "## Messaggi", "", row("tipo", "numero"), row("---", "---")]
    lines += [row(t, n) for t, n in msgs["by_type"].items()]
    lines += [row("totale", msgs["total"]), row("scartati dall'iniezione (silenzio)", msgs["dropped_by_injection"])]
    lines += ["", "## Heartbeat e sequenza", ""]
    lines.append(
        "- Intervallo tra heartbeat (s): "
        + (
            "nessuno"
            if interval is None
            else f"mediana {interval['median']}, p99 {interval['p99']}, max {interval['max']} (n={interval['n']})"
        )
    )
    lines.append(f"- Salti di `heartbeat_counter`: {hb['counter_jumps']} (campo visto: {hb['counter_seen']})")
    lines.append(f"- Ritardo del feed (arrivo - timestamp del messaggio, s): {report['feed_lag_s']}")
    lines.append(
        f"- Buchi di `sequence_num`: {seq['gap_count']} {seq['gaps']}; reset: {seq['resets']}; "
        f"primo numero per connessione: {seq['first_sequence_num_per_connection']}"
    )
    lines += ["", "## Connessioni e riconnessioni", ""]
    lines.append(
        f"- Riconnessioni del flusso: {rec['count']} (iniettate: {rec['expected_from_injection']}, "
        f"inattese: {rec['unexpected']})"
    )
    for r in rec["recoveries"]:
        lines.append(f"  - connessione {r['connection']}: {r}")
    lines.append(f"- Richieste di riconciliazione: {[(r['at_s'], r['reason']) for r in report['reconcile_requests']]}")
    for f in report["injected_faults"]:
        lines.append(f"- Guasto INIETTATO: {f}")
    lines += ["", "## Riconciliatore in lettura (contro un ledger vuoto)", ""]
    su = report["reconciler_startup"]
    lines.append(f"- on_startup: {'errore ' + su['error'] if su['error'] else 'ok'} in {su['elapsed_s']} s")
    if su["report"]:
        lines.append(f"- Rapporto: {su['report']}")
    if su["comparison"]:
        lines.append(f"- Confronto (solo conteggi e valute): {su['comparison']}")
    lines.append(f"- Snapshot WS contro REST: {report['ws_snapshot_vs_rest']}")
    lines.append(f"- Allarmi di copertura: {report['coverage_alarms']}")
    lines.append(f"- Thread del processo (inizio, fine): {report['process']}")
    lines.append(f"- Riconciliatore a regime: {report['reconciler_runtime']}")
    lines += ["", "## Che cosa NON e' provato", ""]
    lines += [f"- {item}" for item in report["not_validated"]]
    return "\n".join(lines) + "\n"


def write_reports(folder: Path, report: Mapping[str, Any]) -> None:
    """Atomic: a reader (or a killed run) never sees half a file."""
    for name, text in (
        (REPORT_JSON, json.dumps(report, indent=2, default=str) + "\n"),
        (REPORT_MD, render_markdown(report)),
    ):
        tmp = folder / (name + ".tmp")
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, folder / name)


def _configure_logging(path: Path, secrets: Sequence[str]) -> Any:
    handle = open(path, "a", encoding="utf-8", newline="\n")  # noqa: SIM115 - lives as long as the process

    def scrub(_: Any, __: str, event: dict[str, Any]) -> dict[str, Any]:
        clean: dict[str, Any] = {}
        for key, value in event.items():
            if key in _LOG_OMIT:
                clean[key] = "<omitted>"
            else:
                clean[key] = redact(value, secrets) if isinstance(value, str) else value
        return clean

    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            scrub,
            structlog.processors.JSONRenderer(default=str),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.WriteLoggerFactory(file=handle),
    )
    return handle


FAULT_CALL_TIMEOUT_S: Final = 5.0


async def _call(fn: Callable[[], None]) -> str:
    """Run an SDK call in a thread, but do not wait for it forever: a ``close`` that races with the stream's own
    ``close`` on the same SDK client may never return (seen with the real feed), and that must not stop the injector."""
    try:
        await asyncio.wait_for(asyncio.to_thread(fn), FAULT_CALL_TIMEOUT_S)
    except TimeoutError:
        return f"applied (call still pending after {FAULT_CALL_TIMEOUT_S:g} s)"
    return "applied"


async def _inject(
    faults: Sequence[Fault], factory: SoakTransportFactory, state: FaultState, t0: float, applied: list[dict[str, Any]],
    clock: Callable[[], float],
) -> None:
    for fault in sorted(faults, key=lambda f: f.at_s):
        await asyncio.sleep(max(t0 + fault.at_s - clock(), 0.0))
        current = factory.current
        entry: dict[str, Any] = {
            "kind": fault.kind, "planned_at_s": fault.at_s, "duration_s": fault.duration_s,
            "applied_at_s": round(clock() - t0, 3), "connection": None if current is None else current.conn.index,
        }  # fmt: skip
        try:
            if fault.kind == "silence":
                if current is None:
                    raise RuntimeError("no connection to silence")
                state.silence_for(fault.duration_s, current.conn.index)
            elif fault.kind == "refuse":
                state.refuse_for(fault.duration_s)
                if current is not None:
                    entry["result"] = await _call(current.abort)
            elif current is not None:
                entry["result"] = await _call(current.close if fault.kind == "close" else current.abort)
            entry.setdefault("result", "applied")
        except Exception as exc:  # recorded, the soak goes on
            entry["result"] = f"failed: {type(exc).__name__}: {exc}"[:300]
        applied.append(entry)


async def run_soak(
    opts: SoakOptions,
    exchange: SoakExchange,
    inner_factory: TransportFactory,
    *,
    secrets: Sequence[str] = (),
    clock: Callable[[], float] = time.time,
) -> int:
    """Run the soak and write the reports. Returns the exit code (0, 1 failed checks, 4 key can write)."""
    perms_raw = await exchange.get_api_key_permissions()
    perms = {k: getattr(perms_raw, k, None) for k in ("can_view", "can_trade", "can_transfer", "portfolio_type")}
    if perms["can_trade"] or perms["can_transfer"]:
        print(
            "REFUSED: the API key can trade or transfer (this soak needs a View-only key); nothing was connected.",
            file=sys.stderr,
        )
        return EXIT_WRITE_KEY
    folder = opts.out
    if (folder / REPORT_JSON).exists() and not opts.overwrite:
        print(f"REFUSED: {folder / REPORT_JSON} already exists (use another --out or --overwrite)", file=sys.stderr)
        return EXIT_CONFIG
    folder.mkdir(parents=True, exist_ok=True)

    registry = await SpotRegistry.load(exchange, opts.products)
    tap = ReadTap(exchange)
    ledger = await PositionLedger.open(":memory:", "live")
    book = BalanceBook(BOOK, "exchange")
    alarms: list[CoverageAlarm] = []
    started = clock()
    threads_start = threading.active_count()
    raw_log = RawLog(folder, clock)
    recorder = SoakRecorder(secrets=secrets, raw_log=raw_log, clock=clock)
    faults = FaultState(clock)
    factory = SoakTransportFactory(inner_factory, recorder, faults, clock)
    requests: list[tuple[float, str]] = []
    applied: list[dict[str, Any]] = []
    reconciler, sink = build_reconciler(tap, registry, ledger, book, alarms, opts.reconcile_interval_s)
    stream: CoinbaseUserStream | None = None
    startup = StartupResult(error="not run")
    ended_reason = "completed"
    tasks: list[asyncio.Task[None]] = []

    def build() -> dict[str, Any]:
        return build_report(
            opts=opts, started=started, ended=clock(), ended_reason=ended_reason, perms=perms, recorder=recorder,
            stream=stream, startup=startup, reconcile_requests=requests, reconciler=reconciler, tap=tap,
            applied_faults=applied, alarms=alarms, threads_start=threads_start,
        )  # fmt: skip

    try:
        # 1. the reconciler against the real account, BEFORE the stream (spec 4.1.5, "Avvio")
        startup = await reconcile_startup(reconciler, tap, book, alarms)

        def on_request(reason: str) -> None:
            requests.append((clock(), reason))
            reconciler.request(reason)

        # 2. the real stream over the real transport (wrapped by the recorder)
        stream = CoinbaseUserStream(
            opts.products, sink, transport_factory=factory, request_reconcile=on_request, silence_s=opts.silence_s,
        )  # fmt: skip
        tasks = [
            asyncio.create_task(stream.run(), name="user_stream"),
            asyncio.create_task(reconciler.run(), name="reconciler"),
            asyncio.create_task(_inject(opts.faults, factory, faults, started, applied, clock), name="faults"),
        ]
        deadline = started + opts.duration_s
        next_progress = started
        while clock() < deadline:
            await asyncio.sleep(min(1.0, max(deadline - clock(), 0.0)))
            crashed = [t for t in tasks if t.done() and t.get_name() != "faults" and not t.cancelled()]
            if crashed:
                exc = crashed[0].exception()
                ended_reason = f"crashed: {crashed[0].get_name()}: {type(exc).__name__}: {exc}"[:300]
                break
            if clock() >= next_progress:
                next_progress = clock() + opts.progress_s
                write_reports(folder, build())
                c = recorder.counts()
                print(f"[{clock() - started:7.0f}s] " + " ".join(f"{k}={v}" for k, v in c.items()), flush=True)
    except (asyncio.CancelledError, KeyboardInterrupt):
        ended_reason = "interrupted"
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # the stream's finally closed the transport; give the SDK thread a moment before closing the files
        await asyncio.sleep(0)
        raw_log.close()
        report = build()
        write_reports(folder, report)
        await ledger.close()
    failed = [name for name, c in report["checks"].items() if not c["pass"]]
    print(f"done: {ended_reason}; checks failed: {failed or 'none'}; reports in {folder}", flush=True)
    return EXIT_OK if not failed else EXIT_CHECKS_FAILED


# ---------------------------------------------------------------------------------------------- cli


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Read-only soak of the Coinbase user channel.")
    ap.add_argument("--minutes", type=float, default=45.0)
    ap.add_argument("--out", type=Path, required=True, help="folder for the reports and the raw record")
    ap.add_argument("--products", nargs="+", default=list(MarketConfig().products))
    ap.add_argument("--inject", action="append", default=[], metavar="KIND@MIN[:SEC]",
                    help="force a fault: close|abort|silence|refuse (see the module docstring)")  # fmt: skip
    ap.add_argument("--reconcile-interval", type=float, default=300.0, help="seconds between periodic reconciliations")
    ap.add_argument("--overwrite", action="store_true")
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        faults = tuple(parse_fault(s) for s in args.inject)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_CONFIG
    cfg = CoinbaseConfig()  # COINBASE_API_KEY / COINBASE_API_SECRET from the environment or .env, never printed
    if not cfg.has_credentials:
        print("no Coinbase credentials: set COINBASE_API_KEY and COINBASE_API_SECRET (env or .env)", file=sys.stderr)
        return EXIT_CONFIG
    secrets = secrets_of(cfg)
    args.out.mkdir(parents=True, exist_ok=True)
    log_handle = _configure_logging(args.out / LOG_NAME, secrets)
    key = cfg.api_key.get_secret_value() if cfg.api_key else None
    secret = cfg.api_secret.get_secret_value() if cfg.api_secret else None

    def make(on_message: Callable[[str], None], on_close: Callable[[], None]) -> UserTransport:
        return SdkTransport(on_message, on_close, api_key=key, api_secret=secret)

    opts = SoakOptions(
        out=args.out, duration_s=args.minutes * 60.0, products=tuple(args.products), faults=faults,
        reconcile_interval_s=args.reconcile_interval, overwrite=args.overwrite,
    )  # fmt: skip
    exchange = build_exchange(cfg, read_only=True)
    try:
        return asyncio.run(run_soak(opts, exchange, make, secrets=secrets))
    except KeyboardInterrupt:
        return EXIT_OK
    finally:
        with contextlib.suppress(Exception):
            log_handle.close()


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)  # an abandoned SDK call (a hung close) is a non-daemon worker thread: do not wait for it at exit
