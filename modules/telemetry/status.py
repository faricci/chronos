"""``StatusWriter`` and ``EquityWriter`` (spec sez. 6.3.1, 6.3.3): the present and the equity history of every book.

* ``telemetry/status.json`` (``chronos.status.v1``): the in-memory state, rewritten atomically every
  ``telemetry.status_interval_s``. The content comes from a ``snapshot()`` callable that reads memory only.
* ``data/equity/YYYY-MM-DD.jsonl`` (``chronos.equity.v1``): one row per book every ``telemetry.equity_interval_s``, the
  values read from the ``VirtualAccount`` / ``BalanceBook`` (never recomputed here).

Isolation (sez. 6.3.5): ``run`` loops forever; a failing cycle is counted in ``TelemetryErrors`` and the next one goes
on. Only ``asyncio.CancelledError`` (shutdown) propagates.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from modules.telemetry.files import TelemetryErrors, dumps_line, iso_z, write_json_atomic

__all__ = ["EQUITY_V", "STATUS_V", "EquityWriter", "StatusWriter"]

STATUS_V: Final = "chronos.status.v1"
EQUITY_V: Final = "chronos.equity.v1"
STATUS_FILE: Final = "status.json"

Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class StatusWriter:
    def __init__(
        self,
        directory: Path,
        snapshot: Callable[[], Mapping[str, Any]],
        *,
        interval_s: float = 10.0,
        clock: Clock = _utc_now,
        sleep: Sleep = asyncio.sleep,
        errors: TelemetryErrors | None = None,
    ) -> None:
        self.path = Path(directory) / STATUS_FILE
        self._snapshot = snapshot
        self._interval = interval_s
        self._clock = clock
        self._sleep = sleep
        self._errors = errors or TelemetryErrors()
        self.writes = 0

    async def write_once(self) -> bool:
        """One cycle. ``False`` (and an error counted) when it failed; never raises."""
        try:
            doc = {"v": STATUS_V, "written_at": iso_z(self._clock(), "milliseconds"), **self._snapshot()}
            await asyncio.to_thread(write_json_atomic, self.path, doc)
            self.writes += 1
            return True
        except Exception as exc:
            self._errors.record("status_write", exc)
            return False

    async def run(self) -> None:
        while True:
            await self.write_once()
            await self._sleep(self._interval)


EquityRows = Callable[[], Awaitable[Sequence[Mapping[str, Any]]]]


class EquityWriter:
    def __init__(
        self,
        root: Path,
        rows: EquityRows,
        *,
        interval_s: float = 60.0,
        clock: Clock = _utc_now,
        sleep: Sleep = asyncio.sleep,
        errors: TelemetryErrors | None = None,
    ) -> None:
        self._root = Path(root)
        self._rows = rows
        self._interval = interval_s
        self._clock = clock
        self._sleep = sleep
        self._errors = errors or TelemetryErrors()
        self.rows_written = 0

    async def write_once(self) -> int:
        """Append one row per book. Returns how many were written (0 on a failure); never raises."""
        try:
            now = self._clock()
            lines = [
                dumps_line({"v": EQUITY_V, "ts": iso_z(now), **row}) for row in await self._rows()
            ]
            if not lines:
                return 0
            await asyncio.to_thread(self._append, now.astimezone(UTC).date().isoformat(), lines)
            self.rows_written += len(lines)
            return len(lines)
        except Exception as exc:
            self._errors.record("equity_write", exc)
            return 0

    def _append(self, day: str, lines: Sequence[str]) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        with open(self._root / f"{day}.jsonl", "a", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")

    async def run(self) -> None:
        while True:
            await self.write_once()
            await self._sleep(self._interval)
