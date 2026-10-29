"""The daily clock of the dry run (spec sez. 5.1.5, 5.1.6): the nightly learner and the model reload.

* **00:15 UTC (``learner.run_at_utc``)**: ``python -m modules.dryrun.learner --once`` as a CHILD PROCESS at low
  priority (``BELOW_NORMAL_PRIORITY_CLASS`` on Windows, ``nice`` elsewhere), outside the critical path. At most one
  learner at a time: a launch while the previous child still runs is refused and recorded. A child that fails, hangs
  past its timeout or cannot even be started is an EVENT; it never stops the app, never raises into it.
* **00:30 UTC (``learner.reload_at_utc``)**: the app re-resolves the registry aliases of its books
  (``Runtime.reload_models``).

``DailyJob`` fires once per UTC day, when the clock passes its time; a job whose time has already passed when the
process starts waits for the next day (a restart at 08:00 does not launch the learner of 00:15). Everything is driven
by the clock the app was given, so a test (or a replay) walks through 00:15 and 00:30 in simulated time.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Final

import structlog

__all__ = [
    "DEFAULT_LEARNER_TIMEOUT_S",
    "DailyJob",
    "LearnerLauncher",
    "LearnerResult",
    "LearnerRunner",
    "run_learner_process",
]

log = structlog.get_logger(__name__)

DEFAULT_LEARNER_TIMEOUT_S: Final = 6 * 3600.0  # a monthly refit trains three models and tunes them: hours, not days
TAIL_CHARS: Final = 2000
NICE_INCREMENT: Final = 10


class DailyJob:
    """Fires once per UTC day, at ``at_utc`` (``HH:MM``), the first time ``due`` is asked after that instant."""

    def __init__(self, name: str, at_utc: str, now: datetime) -> None:
        hh, mm = (int(x) for x in at_utc.split(":"))
        self.name, self.hour, self.minute = name, hh, mm
        self.last_fired: date | None = now.astimezone(UTC).date() if self._passed(now) else None

    def _passed(self, now: datetime) -> bool:
        t = now.astimezone(UTC)
        return (t.hour, t.minute) >= (self.hour, self.minute)

    def due(self, now: datetime) -> bool:
        return self._passed(now) and self.last_fired != now.astimezone(UTC).date()

    def mark(self, now: datetime) -> None:
        self.last_fired = now.astimezone(UTC).date()


# ------------------------------------------------------------------------------------------------ the child


@dataclass(frozen=True)
class LearnerResult:
    exit_code: int | None  # None: the child did not finish (timeout)
    timed_out: bool
    duration_s: float
    tail: str  # the last characters of its output


LearnerRunner = Callable[[Sequence[str], float], Awaitable[LearnerResult]]  # (extra argv, timeout in seconds)


def _spawn_options() -> dict[str, Any]:
    """Low priority from the first instant of the child (the learner lowers its own priority too, as a belt)."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW}
    return {"preexec_fn": lambda: os.nice(NICE_INCREMENT)}


async def run_learner_process(argv: Sequence[str], timeout_s: float) -> LearnerResult:
    """Run ``python -m modules.dryrun.learner --once <argv>``; wait at most ``timeout_s``; kill the child on a timeout
    or when this coroutine is cancelled. Raises ``OSError`` if the child cannot be started."""
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "modules.dryrun.learner", "--once", *argv,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        **_spawn_options(),
    )  # fmt: skip

    async def kill() -> None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()

    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout_s)
    except TimeoutError:
        await kill()
        return LearnerResult(None, True, time.monotonic() - started, "")
    except asyncio.CancelledError:
        await asyncio.shield(kill())
        raise
    tail = (out or b"").decode("utf-8", "replace")[-TAIL_CHARS:]
    return LearnerResult(proc.returncode, False, time.monotonic() - started, tail)


# ---------------------------------------------------------------------------------------------- the launcher

Emit = Callable[..., None]  # ``Runtime.emit(event, severity, **data)``


class LearnerLauncher:
    """One learner child at a time, supervised. ``launch`` returns at once; the outcome is an event."""

    def __init__(
        self, *, runner: LearnerRunner, emit: Emit, argv: Sequence[str] = (),
        timeout_s: float = DEFAULT_LEARNER_TIMEOUT_S,
    ) -> None:  # fmt: skip
        self._runner, self._emit, self._argv, self._timeout = runner, emit, tuple(argv), timeout_s
        self._task: asyncio.Task[None] | None = None
        self.launches = 0
        self.last: dict[str, Any] | None = None  # the last outcome, for the status

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def launch(self, now: datetime) -> bool:
        """Start the child unless the previous one still runs. Returns whether it was started."""
        if self.running:
            self._emit("learner_skipped", "warning", reason="previous_learner_still_running", at=now.isoformat())
            return False
        self.launches += 1
        self._task = asyncio.create_task(self._supervise(now), name="learner_child")
        return True

    async def wait(self) -> None:
        """Wait for the running child (tests, shutdown after a grace period)."""
        if self._task is not None:
            await asyncio.wait({self._task})

    async def cancel(self) -> None:
        """Shutdown: the child is killed (the learner resumes a started date on its next run)."""
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task})

    async def _supervise(self, started_at: datetime) -> None:
        self._emit("learner_started", "info", argv=list(self._argv), timeout_s=self._timeout)
        try:
            result = await self._runner(self._argv, self._timeout)
        except asyncio.CancelledError:
            self.last = {"outcome": "cancelled", "at": started_at.isoformat()}
            self._emit("learner_cancelled", "warning", reason="shutdown")
            raise
        except Exception as exc:  # cannot be started, or a bug of the runner: an event, never the app's problem
            self.last = {"outcome": "error", "at": started_at.isoformat(), "error": f"{type(exc).__name__}: {exc}"}
            self._emit("learner_error", "error", error=f"{type(exc).__name__}: {exc}")
            return
        if result.timed_out:
            self.last = {"outcome": "timeout", "at": started_at.isoformat(), "duration_s": result.duration_s}
            self._emit("learner_timeout", "error", timeout_s=self._timeout, duration_s=round(result.duration_s, 1))
        elif result.exit_code == 0:
            self.last = {"outcome": "ok", "at": started_at.isoformat(), "duration_s": result.duration_s}
            self._emit("learner_finished", "info", exit_code=0, duration_s=round(result.duration_s, 1))
        else:
            self.last = {"outcome": "failed", "at": started_at.isoformat(), "exit_code": result.exit_code}
            self._emit(
                "learner_failed", "error", exit_code=result.exit_code, duration_s=round(result.duration_s, 1),
                tail=result.tail,
            )
