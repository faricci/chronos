"""The raw ``user`` channel of the probe: recorded redacted while it flows, read back to check the field table B of
``docs/decisions/2026-10-20-canale-user-p4.md`` against REST (plan sez. 4.3, 5).

* ``tee_factory`` wraps the transport factory of the user stream: every raw message goes through ``redact`` into the
  :class:`RawLog` and then, untouched, to the stream. A recording error never reaches the stream.
* ``TimedFills`` wraps the fill source of the sink and tells when ``get_fills`` returned how much, for the latency
  from the WS message to the REST fill.
* ``order_messages`` / ``compare_order`` read the recording back and compare the channel's fields with ``get_order``.
"""

from __future__ import annotations

import gzip
import json
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final

import structlog

from modules.execution.exchange import FillInfo, OrderInfo
from modules.execution.raw_log import RawLog, redact
from modules.execution.user_stream import FillSource, TransportFactory, UserTransport

log = structlog.get_logger(__name__)

ZERO: Final = Decimal(0)
FEE_TOLERANCE: Final = Decimal("0.00000001")


def tee_factory(
    inner: TransportFactory, raw_log: RawLog, secrets: Sequence[str], clock: Callable[[], float] = time.time
) -> TransportFactory:
    """The user transport factory, with every raw message recorded (redacted) on its way to the stream."""
    lock = threading.Lock()  # one SDK thread per connection, but a reconnect can overlap the old one's last message

    def make(on_message: Callable[[str], None], on_close: Callable[[], None]) -> UserTransport:
        def tapped(raw: str) -> None:
            try:
                with lock:
                    raw_log.write({"t": clock(), "raw": redact(raw, secrets)})
            except Exception as exc:  # a full disk or a closed log must not stop the order events
                log.error("probe_raw_record_failed", error=type(exc).__name__)
            on_message(raw)

        return inner(tapped, on_close)

    return make


class TimedFills:
    """A ``FillSource`` that remembers what each ``get_fills`` answered and when (``events``)."""

    def __init__(
        self,
        inner: FillSource,
        clock: Callable[[], float] = time.time,
        on_read: Callable[[float, str, Decimal, int], None] | None = None,
    ) -> None:
        self._inner = inner
        self._clock = clock
        self._on_read = on_read
        self.events: list[tuple[float, str, Decimal, int]] = []  # (t, order_id, total size listed, n fills)

    async def get_fills(self, order_id: str) -> list[FillInfo]:
        fills = await self._inner.get_fills(order_id)
        event = (self._clock(), order_id, sum((f.size for f in fills), ZERO), len(fills))
        self.events.append(event)
        if self._on_read is not None:
            self._on_read(*event)
        return fills


# --------------------------------------------------------------------------------------- reading back


def read_raw(path: Path) -> Iterator[dict[str, Any]]:
    """The records of a raw log (``{"t": epoch, "raw": <redacted JSON>}``). A damaged tail is not an error: a killed
    process leaves a gzip that ends in the middle, and everything up to there is still good."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    return
                if isinstance(rec, dict):
                    yield rec
    except (EOFError, OSError):
        return


def order_messages(path: Path) -> list[tuple[float, dict[str, Any]]]:
    """Every ``events[].orders[]`` element of the ``user`` channel in the recording, with the receive time."""
    out: list[tuple[float, dict[str, Any]]] = []
    for rec in read_raw(path):
        try:
            data = json.loads(rec["raw"])
        except (ValueError, KeyError, TypeError):
            continue
        if not isinstance(data, dict) or data.get("channel") != "user":
            continue
        for event in data.get("events", []) or []:
            for order in event.get("orders", []) or []:
                if isinstance(order, dict):
                    out.append((float(rec.get("t", 0.0)), order))
    return out


def _dec(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def compare_order(msg: dict[str, Any], info: OrderInfo, n_fills: int | None = None) -> list[str]:
    """Differences between one channel message and the REST order, field by field (table B). Empty = they agree.
    Only the fields present in the message are judged; a ``0`` price means "not applicable" on the channel."""
    diffs: list[str] = []

    def check(name: str, got: object, want: object) -> None:
        if got != want:
            diffs.append(f"{name}: channel {got!r} vs REST {want!r}")

    check("order_id", msg.get("order_id"), info.order_id)
    if msg.get("client_order_id") is not None:
        check("client_order_id", msg.get("client_order_id"), info.client_order_id)
    check("product_id", msg.get("product_id"), info.product_id)
    if "order_side" in msg:
        check("order_side", msg.get("order_side"), info.side)
    check("status", msg.get("status"), info.status)
    cumulative, leaves = _dec(msg.get("cumulative_quantity")), _dec(msg.get("leaves_quantity"))
    if cumulative is not None:
        check("cumulative_quantity", cumulative, info.filled_size)
        if leaves is not None and info.base_size is not None:
            check("cumulative+leaves", cumulative + leaves, info.base_size)
    for field, rest in (("limit_price", info.limit_price), ("stop_price", info.stop_trigger_price)):
        got = _dec(msg.get(field))
        if got is not None and (got or None) != rest:
            diffs.append(f"{field}: channel {got} vs REST {rest}")
    avg = _dec(msg.get("avg_price"))
    if avg is not None and (avg or None) != info.average_filled_price:
        diffs.append(f"avg_price: channel {avg} vs REST {info.average_filled_price}")
    fees = _dec(msg.get("total_fees"))
    if fees is not None and abs(fees - info.total_fees) > FEE_TOLERANCE:
        diffs.append(f"total_fees: channel {fees} vs REST {info.total_fees}")
    n = _dec(msg.get("number_of_fills"))
    if n is not None and n_fills is not None and int(n) != n_fills:
        diffs.append(f"number_of_fills: channel {int(n)} vs get_fills {n_fills}")
    return diffs


def compare_order_open(msg: dict[str, Any], info: OrderInfo) -> list[str]:
    """The OPEN message of an order that is terminal now: identity and size must agree, the status obviously not."""
    diffs = compare_order(msg, info)
    return [d for d in diffs if not d.startswith(("status:", "cumulative_quantity", "avg_price", "total_fees"))]


def last_message_by_status(
    messages: list[tuple[float, dict[str, Any]]], client_order_id: str
) -> dict[str, dict[str, Any]]:
    """The last channel message of an order for each status it showed (``OPEN``, ``CANCELLED``, ``EXPIRED``...)."""
    out: dict[str, dict[str, Any]] = {}
    for _, order in messages:
        if order.get("client_order_id") == client_order_id:
            out[str(order.get("status"))] = order
    return out


def fill_latencies(
    messages: list[tuple[float, dict[str, Any]]], timed: Sequence[tuple[float, str, Decimal, int]]
) -> list[float]:
    """Seconds from a channel message whose ``cumulative_quantity`` rose to the first ``get_fills`` that listed that
    much. One value per rise; a rise never listed gives no value (the report counts it as a missing fill)."""
    out: list[float] = []
    seen: dict[str, Decimal] = {}
    for t_msg, order in messages:
        oid = str(order.get("order_id", ""))
        cumulative = _dec(order.get("cumulative_quantity")) or ZERO
        if cumulative <= seen.get(oid, ZERO):
            continue
        seen[oid] = cumulative
        for t_fill, fill_oid, total, _ in timed:
            if fill_oid == oid and t_fill >= t_msg and total >= cumulative:
                out.append(t_fill - t_msg)
                break
    return out
