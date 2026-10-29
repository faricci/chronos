"""``EventLog``: the append-only history of what happened (spec sez. 6.3.2, ``chronos.event.v1``).

``data/events/YYYY-MM-DD.jsonl``, one JSON line per event. ``emit`` is synchronous, O(1) and never blocks: the record is
put in a bounded ``queue.Queue`` (``put_nowait``) and a writer thread drains it, flushing at least every second. With
the queue full the event is dropped and counted (``dropped_total``, and a ``telemetry_dropped`` event follows when there
is room again). Nothing here raises into the caller; a failing disk degrades the telemetry, never the trading.

``LogForwarder`` is the ``structlog`` processor of D5: every log at error level or above becomes a ``log_error`` event
(message cut at 2 KB), and a few log events that are facts of the spec table become their typed event
(``coverage_refused``, ``coverage_alarm``). It runs after the redaction of the secrets, so an event never carries one.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable, Mapping, MutableMapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import IO, Any, Final

from modules.decision.engine import new_ulid
from modules.telemetry.files import TelemetryErrors, dumps_line, iso_z

__all__ = ["EVENT_V", "EventLog", "LogForwarder"]

EVENT_V: Final = "chronos.event.v1"
SEVERITIES: Final = frozenset({"debug", "info", "warning", "error", "critical"})
MESSAGE_MAX: Final = 2048
_STOP: Final = object()

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class EventLog:
    def __init__(
        self,
        root: Path,
        *,
        queue_max: int = 10_000,
        clock: Clock = _utc_now,
        errors: TelemetryErrors | None = None,
        redact: Callable[[Any], Any] | None = None,
        retention_days: int = 180,
        flush_s: float = 1.0,
    ) -> None:
        self._root = Path(root)
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=queue_max)
        self._clock = clock
        self._errors = errors or TelemetryErrors()
        self._redact = redact
        self._retention = timedelta(days=retention_days)
        self._flush_s = flush_s
        self._thread: threading.Thread | None = None
        self._fh: IO[str] | None = None
        self._day: date | None = None
        self.dropped_total = 0
        self._dropped_reported = 0
        self.written_total = 0

    # ------------------------------------------------------------------------------------------ api

    def start(self) -> None:
        if self._thread is not None:
            return
        self._purge()
        self._thread = threading.Thread(target=self._run, name="event-log", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Write what is queued, close the file, join the thread. Safe to call twice."""
        thread, self._thread = self._thread, None
        if thread is None:
            return
        try:
            self._queue.put(_STOP, timeout=timeout)
        except queue.Full:
            pass  # the thread is stuck on a dead disk: it is a daemon, the process can still end
        thread.join(timeout)

    def emit(
        self,
        type: str,
        severity: str = "info",
        *,
        book: str | None = None,
        product: str | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        """Queue one event. Never blocks, never raises."""
        try:
            now = self._clock()
            record: dict[str, Any] = {
                "v": EVENT_V,
                "event_id": new_ulid(now),
                "ts": iso_z(now, "milliseconds"),
                "type": type,
                "severity": severity if severity in SEVERITIES else "info",
                "book": book,
                "product": product,
                "data": dict(data or {}),
            }
            if self._redact is not None:
                record = self._redact(record)
            self._queue.put_nowait(record)
        except queue.Full:
            self.dropped_total += 1
        except Exception as exc:
            self._errors.record("event_emit", exc)

    # ------------------------------------------------------------------------------------- writer

    def _run(self) -> None:
        stop = False
        while not stop:
            batch: list[Any] = []
            try:
                batch.append(self._queue.get(timeout=self._flush_s))
                while len(batch) < 1000:
                    batch.append(self._queue.get_nowait())
            except queue.Empty:
                pass
            for item in batch:
                if item is _STOP:
                    stop = True
                    continue
                self._write(item)
            self._report_drops()
            self._flush()
        self._close()

    def _report_drops(self) -> None:
        new = self.dropped_total - self._dropped_reported
        if new > 0 and not self._queue.full():
            self._dropped_reported += new
            self.emit("telemetry_dropped", "warning", data={"count": new, "total": self.dropped_total})

    def _write(self, record: Mapping[str, Any]) -> None:
        try:
            day = datetime.fromisoformat(str(record["ts"]).replace("Z", "+00:00")).astimezone(UTC).date()
            if self._fh is None or self._day != day:
                self._close()
                self._root.mkdir(parents=True, exist_ok=True)
                self._fh = open(self._root / f"{day.isoformat()}.jsonl", "a", encoding="utf-8", newline="\n")  # noqa: SIM115
                self._day = day
            self._fh.write(dumps_line(record) + "\n")
            self.written_total += 1
        except Exception as exc:
            self._errors.record("event_write", exc)
            self._close()  # reopen at the next event (a replaced or locked file)

    def _flush(self) -> None:
        try:
            if self._fh is not None:
                self._fh.flush()
        except Exception as exc:
            self._errors.record("event_flush", exc)
            self._close()

    def _close(self) -> None:
        fh, self._fh = self._fh, None
        if fh is not None:
            try:
                fh.close()
            except Exception as exc:
                self._errors.record("event_close", exc)

    def _purge(self) -> None:
        """Delete the daily files older than the retention (by their name)."""
        try:
            cutoff = self._clock().date() - self._retention
            for path in self._root.glob("*.jsonl"):
                try:
                    if date.fromisoformat(path.stem) < cutoff:
                        path.unlink()
                except ValueError:
                    continue  # not one of ours
        except Exception as exc:
            self._errors.record("event_purge", exc)


# ----------------------------------------------------------------------------------------- structlog

# log event name -> (event type, severity, data fields taken from the log record)
TYPED_LOGS: Final[dict[str, tuple[str, str, tuple[str, ...]]]] = {
    "coverage_refused": ("coverage_refused", "warning", ("client_order_id", "side", "kind", "reason")),
    "book_coverage_alarm": ("coverage_alarm", "error", ("book", "kind", "currency", "detail")),
    # the watchdog logs its exits at error level, the HORIZON one too (a routine close): a typed warning, not noise
    "watchdog_exit": ("watchdog_exit", "warning", ("book", "product_id", "cause", "detail")),
}
_NOT_ERRORS: Final = frozenset({"telemetry_error", "watchdog_exit_done"})  # reported by their typed event or not at all
_ERROR_LEVELS: Final = frozenset({"error", "critical"})


class LogForwarder:
    """``structlog`` processor: logs at error level and the typed facts go to the event log (set ``target``)."""

    def __init__(self) -> None:
        self.target: EventLog | None = None

    def __call__(
        self, _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        target = self.target
        if target is None:
            return event_dict
        try:
            event = str(event_dict.get("event", ""))
            typed = TYPED_LOGS.get(event)
            if typed is not None:
                etype, severity, fields = typed
                data = {k: event_dict[k] for k in fields if k in event_dict}
                book, product = event_dict.get("book"), event_dict.get("product_id", event_dict.get("product"))
                target.emit(etype, severity, book=book, product=product, data=data)
            elif event_dict.get("level") in _ERROR_LEVELS and event not in _NOT_ERRORS:
                target.emit(
                    "log_error", "error",
                    data={"event": event, "message": _message(event_dict)[:MESSAGE_MAX]},
                )  # fmt: skip
        except Exception:  # a log line is never worth an exception
            return event_dict
        return event_dict


def _message(event_dict: Mapping[str, Any]) -> str:
    parts = [f"{k}={v}" for k, v in event_dict.items() if k not in ("event", "level", "timestamp", "exception")]
    exc = event_dict.get("exception")
    return " ".join(parts) + ("" if not exc else f" exception={str(exc).splitlines()[-1]}")
