"""The supervisor of the app: restart on death or stale heartbeat, honour the app's own decisions, stop crash loops."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from script.supervise import BACKOFF_MIN_S, NO_RESTART_CODES, AppProcess, Policy, Supervisor, _jsonl


class FakeChild:
    count = 0

    def __init__(self) -> None:
        FakeChild.count += 1
        self.pid = 1000 + FakeChild.count
        self.code: int | None = None
        self.stopped_with: float | None = None

    def poll(self) -> int | None:
        return self.code

    def stop(self, kill_after_s: float) -> int | None:
        self.stopped_with = kill_after_s
        self.code = -15 if self.code is None else self.code
        return self.code


class Rig:
    def __init__(self, tmp: Path, policy: Policy | None = None) -> None:
        FakeChild.count = 0
        self.now = 10_000.0
        self.sleeps: list[float] = []
        self.logs: list[dict[str, object]] = []
        self.children: list[FakeChild] = []
        self.heartbeat = tmp / "heartbeat"
        self.sup = Supervisor(
            spawn=self.spawn, heartbeat=self.heartbeat, log=self.logs.append,
            policy=policy or Policy(stale_s=240, startup_grace_s=420, kill_after_s=45, max_restarts_per_hour=8),
            clock=lambda: self.now, sleep=self.sleep,
        )  # fmt: skip

    def spawn(self) -> FakeChild:
        c = FakeChild()
        self.children.append(c)
        return c

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s

    def beat(self, age: float = 0.0) -> None:
        self.heartbeat.write_text("{}")
        os.utime(self.heartbeat, (self.now - age, self.now - age))

    def events(self) -> list[object]:
        return [e["event"] for e in self.logs]


def test_the_first_step_starts_the_app(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    assert r.sup.step() is None and len(r.children) == 1 and r.events() == ["start"]


def test_a_healthy_app_is_left_alone(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    r.sup.step()
    for _ in range(100):
        r.now += 20
        r.beat()
        assert r.sup.step() is None
    assert len(r.children) == 1 and r.events() == ["start"]


@pytest.mark.parametrize("code", [1, -9, 137, 5])
def test_an_app_that_dies_is_restarted_with_a_growing_backoff(tmp_path: Path, code: int) -> None:
    r = Rig(tmp_path)
    r.sup.step()
    r.children[-1].code = code
    assert r.sup.step() is None
    assert r.sleeps == [BACKOFF_MIN_S] and r.logs[-1]["event"] == "restart_in"
    r.sup.step()  # starts the second child
    r.children[-1].code = code
    r.sup.step()
    assert r.sleeps == [BACKOFF_MIN_S, 2 * BACKOFF_MIN_S] and len(r.children) == 2
    exited = [e for e in r.logs if e["event"] == "exited"]
    assert [e["exit_code"] for e in exited] == [code, code]  # the next death is explained


@pytest.mark.parametrize("code", sorted(NO_RESTART_CODES))
def test_the_decisions_of_the_app_are_honoured(tmp_path: Path, code: int) -> None:
    """0 = asked to stop, 2 = configuration, 3 = DryRunViolation, 4 = SpotViolation: a restart would override them."""
    r = Rig(tmp_path)
    r.sup.step()
    r.children[-1].code = code
    assert r.sup.step() == code
    assert len(r.children) == 1 and r.sleeps == [] and r.events()[-1] == "not_restarting"


def test_a_missing_heartbeat_is_tolerated_during_the_startup_grace_and_not_after(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    r.sup.step()
    r.now += 300  # inside the 420 s grace: the app is still warming up
    assert r.sup.step() is None and len(r.children) == 1
    r.now += 200  # past it, and still no heartbeat file
    r.sup.step()
    assert r.children[0].stopped_with == 45 and "heartbeat_missing" in str(r.logs[1]["reason"])
    r.sup.step()
    assert len(r.children) == 2


def test_a_stale_heartbeat_replaces_the_app_and_a_fresh_one_does_not(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    r.sup.step()
    r.now += 500
    r.beat(age=100)  # fresh enough (limit 240 s)
    assert r.sup.step() is None and len(r.children) == 1
    r.beat(age=400)  # the app froze 400 s ago
    r.sup.step()
    stop = [e for e in r.logs if e["event"] == "stopped"]
    assert len(stop) == 1 and str(stop[0]["reason"]).startswith("heartbeat_stale")
    assert r.children[0].stopped_with == 45  # graceful first, a kill only after this long
    r.sup.step()
    assert len(r.children) == 2


def test_a_crash_loop_stops_the_supervisor_instead_of_hammering_the_exchange(tmp_path: Path) -> None:
    r = Rig(tmp_path, Policy(max_restarts_per_hour=3))
    code: int | None = None
    for _ in range(10):
        r.sup.step()  # (re)start
        if r.children[-1].poll() is None:
            r.children[-1].code = 1
        code = r.sup.step()
        if code is not None:
            break
    assert code == 1 and len(r.children) == 3 and r.events()[-1] == "crash_loop"


def test_old_restarts_do_not_count_toward_the_crash_loop(tmp_path: Path) -> None:
    r = Rig(tmp_path, Policy(max_restarts_per_hour=3))
    for _ in range(2):
        r.sup.step()
        r.children[-1].code = 1
        r.sup.step()
    r.now += 4000  # more than an hour later
    r.sup.step()
    r.children[-1].code = 1
    assert r.sup.step() is None  # the oldest restarts have expired: no crash loop


def test_a_long_stable_run_resets_the_backoff(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    r.sup.step()
    r.children[-1].code = 1
    r.sup.step()  # backoff doubles to 10
    r.sup.step()
    r.now += 700
    r.beat()
    r.sup.step()  # stable for more than 600 s
    assert r.sup.backoff_s == BACKOFF_MIN_S


def test_stopping_the_supervisor_stops_the_app_gracefully(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    r.sup.step()
    r.sup.stopping = True
    assert r.sup.run() == 0
    assert r.children[0].stopped_with == 45 and r.events()[-1] == "stopped"


def test_the_log_is_one_json_object_per_line(tmp_path: Path) -> None:
    write = _jsonl(tmp_path / "sub" / "sup.jsonl")
    write({"event": "start", "pid": 1})
    write({"event": "exited", "exit_code": 1})
    rows = [json.loads(x) for x in (tmp_path / "sub" / "sup.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["event"] for r in rows] == ["start", "exited"] and all("ts" in r for r in rows)


def test_a_real_process_is_stopped_gracefully_and_reports_its_exit(tmp_path: Path) -> None:
    sleeper = [sys.executable, "-c", "import time; time.sleep(120)"]
    with open(os.devnull, "ab") as out:
        p = AppProcess(sleeper, tmp_path, out, out)
        assert p.poll() is None
        t0 = time.monotonic()
        code = p.stop(kill_after_s=10)
        assert code is not None and p.poll() is not None and time.monotonic() - t0 < 12


def test_a_real_process_that_ignores_the_signal_is_killed_after_the_grace(tmp_path: Path) -> None:
    stubborn = [
        sys.executable, "-c",
        "import signal, time\nfor n in ('SIGBREAK','SIGTERM','SIGINT'):\n"
        "    s = getattr(signal, n, None)\n    s and signal.signal(s, signal.SIG_IGN)\ntime.sleep(120)",
    ]  # fmt: skip
    with open(os.devnull, "ab") as out:
        p = AppProcess(stubborn, tmp_path, out, out)
        time.sleep(1.0)  # let the interpreter install its handlers
        code = p.stop(kill_after_s=2)
        assert code is not None and p.poll() is not None
