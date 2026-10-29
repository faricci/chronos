"""File helpers of the telemetry (stdlib only, spec sez. 6.3): atomic JSON, one-line JSON, error counting."""

from __future__ import annotations

import json
import math
import os
import time
from collections import deque
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

import structlog

__all__ = ["TelemetryErrors", "dumps_line", "iso_z", "write_json_atomic"]

log = structlog.get_logger(__name__)

REPLACE_ATTEMPTS: Final = 50


def iso_z(t: datetime, spec: Literal["seconds", "milliseconds"] = "seconds") -> str:
    return t.astimezone(UTC).isoformat(timespec=spec).replace("+00:00", "Z")


def _clean(obj: Any) -> Any:
    if isinstance(obj, float) and not math.isfinite(obj):
        return None  # JSON has no NaN: a missing number is null
    if isinstance(obj, Mapping):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple | set | frozenset):
        return [_clean(v) for v in obj]
    return obj


def dumps_line(obj: Any) -> str:
    """One JSON document on one line (``\\n`` never appears inside), strict JSON, Decimal and datetimes as strings."""
    return json.dumps(_clean(obj), separators=(",", ":"), ensure_ascii=False, default=str, allow_nan=False)


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    """Write a temporary file in the same directory, then ``os.replace`` it over ``path``: a reader sees the old
    document or the new one, never half of one. On Windows the replace is retried while a reader holds the file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(dumps_line(payload), encoding="utf-8")
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.01)


class TelemetryErrors:
    """The place where a telemetry failure goes (spec sez. 6.3.5): counted, logged at most once a minute per kind, and
    NEVER raised. ``errors_1h`` is what ``status.telemetry`` shows. Logs at ``warning`` on purpose: errors are
    forwarded to the event log, and a failing event log must not feed itself."""

    def __init__(self, monotonic: Callable[[], float] = time.monotonic, log_every_s: float = 60.0) -> None:
        self._now = monotonic
        self._every = log_every_s
        self._last_logged: dict[str, float] = {}
        self._times: deque[float] = deque()
        self.total = 0

    def record(self, kind: str, exc: BaseException) -> None:
        try:
            self.total += 1
            now = self._now()
            self._times.append(now)
            if now - self._last_logged.get(kind, -math.inf) >= self._every:
                self._last_logged[kind] = now
                log.warning("telemetry_error", kind=kind, error=f"{type(exc).__name__}: {exc}", total=self.total)
        except Exception:  # nothing in here may escape
            return

    def errors_1h(self) -> int:
        now = self._now()
        while self._times and now - self._times[0] > 3600.0:
            self._times.popleft()
        return len(self._times)
