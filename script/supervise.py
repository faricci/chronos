"""Keep ``python -m modules.app`` alive: restart it when it dies or when its heartbeat goes stale.

::

    python -m script.supervise --config <config.yml> --heartbeat <data/heartbeat> --log <supervisor.jsonl> \\
        [--stdout <app.log> --stderr <app.err.log>] [--stale-s 240] [--startup-grace-s 420]

Why: a dry run that ran for days died twice without a trace (a silent process death at 2026-10-23 08:23, a market feed
that never reconnected on 2026-10-20) and nobody knew for hours. The app writes ``data/heartbeat`` every 10 s; this
supervisor treats a missing or old file as a dead app, stops it gracefully (CTRL_BREAK on Windows, SIGTERM elsewhere,
then a kill after ``--kill-after-s``), and starts it again with a backoff. Every start, exit and stale kill is one JSON
line in ``--log`` with the exit code, so the next death is explained. A crash loop (more than
``--max-restarts-per-hour``) stops the supervisor with exit code 1 instead of hammering the exchange API.

It never touches orders: the app restores its books from its own ledger at every start.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, Final, Protocol

POLL_S: Final = 5.0
BACKOFF_MIN_S: Final = 5.0
BACKOFF_MAX_S: Final = 120.0
STABLE_AFTER_S: Final = 600.0  # a run this long resets the backoff
# Exit codes of the app that are DECISIONS, not failures: restarting would override them.
#   0 = asked to stop; 2 = configuration error (a restart cannot fix it); 3 = DryRunViolation; 4 = SpotViolation.
NO_RESTART_CODES: Final = frozenset({0, 2, 3, 4})


class Child(Protocol):
    pid: int

    def poll(self) -> int | None: ...
    def stop(self, kill_after_s: float) -> int | None: ...


@dataclass
class Policy:
    stale_s: float = 240.0
    startup_grace_s: float = 420.0  # warm-up of the indicators and the restore take about two minutes
    kill_after_s: float = 45.0
    max_restarts_per_hour: int = 8


@dataclass
class Supervisor:
    spawn: Callable[[], Child]
    heartbeat: Path
    log: Callable[[dict[str, Any]], None]
    policy: Policy = field(default_factory=Policy)
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    child: Child | None = None
    started_at: float = 0.0
    backoff_s: float = BACKOFF_MIN_S
    restarts: list[float] = field(default_factory=list)
    stopping: bool = False

    # ---------------------------------------------------------------------------------------- decisions (pure)

    def heartbeat_age_s(self, now: float) -> float | None:
        try:
            return now - self.heartbeat.stat().st_mtime
        except OSError:
            return None

    def verdict(self, now: float) -> str | None:
        """Why the running child must be replaced, or ``None``."""
        assert self.child is not None
        code = self.child.poll()
        if code is not None:
            return f"exited:{code}"
        if now - self.started_at < self.policy.startup_grace_s:
            return None
        age = self.heartbeat_age_s(now)
        if age is None:
            return "heartbeat_missing"
        return f"heartbeat_stale:{age:.0f}s" if age > self.policy.stale_s else None

    def crash_loop(self, now: float) -> bool:
        self.restarts = [t for t in self.restarts if now - t < 3600]
        return len(self.restarts) >= self.policy.max_restarts_per_hour

    # --------------------------------------------------------------------------------------------- the loop

    def start(self) -> None:
        now = self.clock()
        self.child = self.spawn()
        self.started_at = now
        self.restarts.append(now)
        self.log({"event": "start", "pid": self.child.pid, "restarts_last_hour": len(self.restarts) - 1})

    def stop_child(self, reason: str) -> int | None:
        assert self.child is not None
        code = self.child.stop(self.policy.kill_after_s)
        self.log({"event": "stopped", "pid": self.child.pid, "reason": reason, "exit_code": code})
        return code

    def step(self) -> int | None:
        """One poll. Returns an exit code for the supervisor itself when it must stop, else ``None``."""
        now = self.clock()
        if self.child is None:
            self.start()
            return None
        reason = self.verdict(now)
        if reason is None:
            if now - self.started_at > STABLE_AFTER_S:
                self.backoff_s = BACKOFF_MIN_S
            return None
        if not reason.startswith("exited:"):
            self.stop_child(reason)
        else:
            code = self.child.poll()
            self.log({"event": "exited", "pid": self.child.pid, "exit_code": code, "reason": reason})
            if code in NO_RESTART_CODES:  # the app decided: honour it
                self.log({"event": "not_restarting", "exit_code": code})
                self.child = None
                return code
        self.child = None
        if self.crash_loop(now):
            self.log({"event": "crash_loop", "restarts_last_hour": len(self.restarts), "action": "supervisor_stops"})
            return 1
        self.log({"event": "restart_in", "seconds": self.backoff_s, "reason": reason})
        self.sleep(self.backoff_s)
        self.backoff_s = min(self.backoff_s * 2, BACKOFF_MAX_S)
        return None

    def run(self) -> int:
        while not self.stopping:
            code = self.step()
            if code is not None:
                return code
            self.sleep(POLL_S)
        if self.child is not None:
            self.stop_child("supervisor_stopping")
        return 0


# ------------------------------------------------------------------------------------------------ real process


class AppProcess:
    """``python -m modules.app`` in its own process group, so that CTRL_BREAK reaches only it."""

    def __init__(self, cmd: Sequence[str], cwd: Path, stdout: IO[bytes], stderr: IO[bytes]) -> None:
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        self._p = subprocess.Popen(list(cmd), cwd=cwd, stdout=stdout, stderr=stderr, creationflags=flags)  # noqa: S603
        self.pid = self._p.pid

    def poll(self) -> int | None:
        return self._p.poll()

    def stop(self, kill_after_s: float) -> int | None:
        if self._p.poll() is None:
            try:
                self._p.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
            except OSError:
                pass
            try:
                self._p.wait(timeout=kill_after_s)
            except subprocess.TimeoutExpired:
                self._p.kill()
                self._p.wait(timeout=30)
        return self._p.poll()


def _jsonl(path: Path) -> Callable[[dict[str, Any]], None]:
    path.parent.mkdir(parents=True, exist_ok=True)

    def write(record: dict[str, Any]) -> None:
        line = json.dumps({"ts": datetime.now(UTC).isoformat(timespec="seconds"), **record}, default=str)
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")

    return write


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Restart modules.app when it dies or its heartbeat goes stale")
    ap.add_argument("--config", required=True)
    ap.add_argument("--heartbeat", required=True, type=Path)
    ap.add_argument("--log", required=True, type=Path)
    ap.add_argument("--stdout", type=Path, default=None)
    ap.add_argument("--stderr", type=Path, default=None)
    ap.add_argument("--cwd", type=Path, default=Path.cwd(), help="directory the app runs in (it reads .env there)")
    ap.add_argument("--stale-s", type=float, default=Policy.stale_s)
    ap.add_argument("--startup-grace-s", type=float, default=Policy.startup_grace_s)
    ap.add_argument("--kill-after-s", type=float, default=Policy.kill_after_s)
    ap.add_argument("--max-restarts-per-hour", type=int, default=Policy.max_restarts_per_hour)
    args = ap.parse_args(argv)
    out = open(args.stdout or os.devnull, "ab")  # noqa: SIM115
    err = open(args.stderr or os.devnull, "ab")  # noqa: SIM115
    cmd = [sys.executable, "-m", "modules.app", "--config", args.config]
    sup = Supervisor(
        spawn=lambda: AppProcess(cmd, args.cwd, out, err),
        heartbeat=args.heartbeat,
        log=_jsonl(args.log),
        policy=Policy(args.stale_s, args.startup_grace_s, args.kill_after_s, args.max_restarts_per_hour),
    )

    def request_stop(_signum: int, _frame: Any) -> None:
        sup.stopping = True

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), request_stop)
    sup.log({"event": "supervisor_start", "config": args.config, "heartbeat": str(args.heartbeat)})
    code = sup.run()
    sup.log({"event": "supervisor_exit", "exit_code": code})
    return code


if __name__ == "__main__":
    sys.exit(main())
