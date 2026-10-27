"""Hand controls and persistent flags of the probe: the ``STOP`` / ``FLATTEN`` files, the end-of-probe marker and the
status file (plan sez. 1.6).

* ``<state_dir>/exec_probe/STOP``: no new entry, and the pending entry is cancelled (with confirmation). The bracket and
  the position stay. ``FLATTEN``: ``STOP`` plus the exit of the position (reason ``MANUAL``).
* They are created by ``python -m modules.probe stop|flatten``, read every second and before every entry, and
  removed ONLY by hand: this code has no function that deletes them.
* ``<out_dir>/finished.json``: the probe is over (a watchdog exit, the anomaly loss, the exhausted budget, a conflict).
  It survives a restart: the runner refuses to place entries while it exists, and the operator removes it after the
  analysis the plan asks for.
* ``<out_dir>/status.json``: what ``python -m modules.probe status`` prints; written by the running process.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

STOP_NAME: Final = "STOP"
FLATTEN_NAME: Final = "FLATTEN"
FINISHED_NAME: Final = "finished.json"
STATUS_NAME: Final = "status.json"
EVENTS_NAME: Final = "events.jsonl"
MANUAL_STOP: Final = "manual_stop"


class Switches:
    """The two files. ``stop_active`` is true for ``STOP`` and for ``FLATTEN`` (which includes it)."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    @property
    def stop_path(self) -> Path:
        return self.directory / STOP_NAME

    @property
    def flatten_path(self) -> Path:
        return self.directory / FLATTEN_NAME

    def stop_active(self) -> bool:
        return self.stop_path.exists() or self.flatten_path.exists()

    def flatten_active(self) -> bool:
        return self.flatten_path.exists()

    def request_stop(self) -> Path:
        return self._touch(self.stop_path)

    def request_flatten(self) -> Path:
        return self._touch(self.flatten_path)

    def _touch(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(datetime.now(UTC).isoformat() + "\n", encoding="utf-8")
        return path


@dataclass(frozen=True, slots=True)
class Finished:
    reason: str
    detail: str
    at: datetime


class FinishMarker:
    def __init__(self, out_dir: Path) -> None:
        self.path = out_dir / FINISHED_NAME

    def read(self) -> Finished | None:
        if not self.path.is_file():
            return None
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            return Finished(str(d["reason"]), str(d.get("detail", "")), datetime.fromisoformat(d["at"]))
        except (ValueError, KeyError, TypeError):
            # an unreadable marker still means "finished": fail closed
            return Finished("unreadable_marker", "", datetime.now(UTC))

    def write(self, reason: str, detail: str, at: datetime) -> bool:
        """Write the marker once; the first reason is the one that counts. Returns True if it was written now."""
        if self.path.exists():
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"reason": reason, "detail": detail, "at": at.astimezone(UTC).isoformat()}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        return True


def write_status(out_dir: Path, status: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / STATUS_NAME
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(status, indent=2, sort_keys=True, default=str), encoding="utf-8")
    tmp.replace(path)


def read_status(out_dir: Path) -> dict[str, Any] | None:
    path = out_dir / STATUS_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


class EventFile:
    """``<out_dir>/events.jsonl``: one JSON line per fact worth keeping (skips, entries, exits, alarms, protections).
    The report reads it back. Amounts are small and the file holds no secret (events carry ids, reasons and numbers)."""

    def __init__(self, out_dir: Path, clock: Callable[[], datetime]) -> None:
        self.path = out_dir / EVENTS_NAME
        self._clock = clock

    def emit(self, kind: str, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"t": self._clock().astimezone(UTC).isoformat(), "kind": kind, **data}, default=str)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def read(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        out: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
        return out
