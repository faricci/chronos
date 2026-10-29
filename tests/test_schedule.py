"""The daily clock of the dry run: a job fires once a day, the learner is a supervised low-priority child, and nothing
it does (fail, hang, not start) reaches the app."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from modules.dryrun import schedule as schedule_module
from modules.dryrun.schedule import DailyJob, LearnerLauncher, LearnerResult, run_learner_process

D0 = datetime(2026, 11, 2, tzinfo=UTC)


def at(hh: int, mm: int, ss: int = 0, day: int = 0) -> datetime:
    return D0 + timedelta(days=day, hours=hh, minutes=mm, seconds=ss)


# ============================================================================================== the daily job


def test_a_job_fires_once_a_day_when_the_clock_passes_its_time() -> None:
    job = DailyJob("learner", "00:15", at(0, 10))
    assert not job.due(at(0, 14, 59))
    assert job.due(at(0, 15)) and job.due(at(0, 15, 1))
    job.mark(at(0, 15))
    assert not job.due(at(0, 15, 1)) and not job.due(at(23, 59))  # not again today
    assert not job.due(at(0, 14, 59, day=1))
    assert job.due(at(0, 15, day=1))  # tomorrow it is due again


def test_a_job_whose_time_has_passed_when_the_process_starts_waits_for_tomorrow() -> None:
    job = DailyJob("learner", "00:15", at(8, 0))  # a restart at 08:00 does not run the learner of 00:15
    assert not job.due(at(8, 1)) and not job.due(at(23, 59))
    assert job.due(at(0, 15, day=1))
    exact = DailyJob("reload", "00:30", at(0, 30))  # started at the very minute: that occurrence is gone
    assert not exact.due(at(0, 30, 30)) and exact.due(at(0, 30, day=1))


def test_the_time_of_a_job_is_utc_whatever_the_clock_zone() -> None:
    from datetime import timezone

    plus2 = timezone(timedelta(hours=2))
    job = DailyJob("learner", "00:15", at(23, 0))
    assert not job.due(datetime(2026, 11, 3, 2, 14, tzinfo=plus2))  # 00:14 UTC
    assert job.due(datetime(2026, 11, 3, 2, 15, tzinfo=plus2))  # 00:15 UTC


# =========================================================================================== the launcher


class Events:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self, event: str, severity: str = "info", **data: Any) -> None:
        self.items.append((event, severity, data))

    def names(self) -> list[str]:
        return [e for e, _, _ in self.items]


def runner_returning(result: LearnerResult | Exception, calls: list[Any] | None = None) -> Any:
    async def run(argv: Any, timeout_s: float) -> LearnerResult:
        if calls is not None:
            calls.append((tuple(argv), timeout_s))
        if isinstance(result, Exception):
            raise result
        return result

    return run


async def test_a_learner_that_succeeds_is_an_event_and_nothing_else() -> None:
    ev, calls = Events(), []
    launcher = LearnerLauncher(
        runner=runner_returning(LearnerResult(0, False, 12.3, "ok"), calls), emit=ev, argv=["--config", "c.yml"],
        timeout_s=99.0,
    )  # fmt: skip
    assert launcher.launch(at(0, 15)) is True
    await launcher.wait()
    assert ev.names() == ["learner_started", "learner_finished"] and ev.items[1][1] == "info"
    assert calls == [(("--config", "c.yml"), 99.0)]
    assert launcher.last is not None and launcher.last["outcome"] == "ok" and not launcher.running


async def test_a_failing_learner_is_an_error_event_with_its_output() -> None:
    ev = Events()
    launcher = LearnerLauncher(runner=runner_returning(LearnerResult(1, False, 3.0, "Traceback: boom")), emit=ev)
    launcher.launch(at(0, 15))
    await launcher.wait()
    (_, severity, data) = ev.items[-1]
    assert ev.names()[-1] == "learner_failed" and severity == "error"
    assert data["exit_code"] == 1 and "boom" in data["tail"]
    assert launcher.last is not None and launcher.last["outcome"] == "failed"


async def test_a_learner_that_times_out_is_an_error_event() -> None:
    ev = Events()
    launcher = LearnerLauncher(runner=runner_returning(LearnerResult(None, True, 21600.0, "")), emit=ev, timeout_s=5)
    launcher.launch(at(0, 15))
    await launcher.wait()
    assert ev.names()[-1] == "learner_timeout" and ev.items[-1][1] == "error" and ev.items[-1][2]["timeout_s"] == 5
    assert launcher.last is not None and launcher.last["outcome"] == "timeout"


async def test_a_learner_that_cannot_even_start_is_an_event_not_an_exception() -> None:
    ev = Events()
    launcher = LearnerLauncher(runner=runner_returning(FileNotFoundError("no python")), emit=ev)
    launcher.launch(at(0, 15))
    await launcher.wait()  # does not raise
    assert ev.names()[-1] == "learner_error" and "FileNotFoundError" in ev.items[-1][2]["error"]
    assert launcher.launch(at(0, 16)) is True  # and the next day (or minute) it can be launched again


async def test_only_one_learner_runs_at_a_time() -> None:
    ev, release, calls = Events(), asyncio.Event(), []

    async def slow(argv: Any, timeout_s: float) -> LearnerResult:
        calls.append(1)
        await release.wait()
        return LearnerResult(0, False, 1.0, "")

    launcher = LearnerLauncher(runner=slow, emit=ev)
    assert launcher.launch(at(0, 15)) is True
    await asyncio.sleep(0)
    assert launcher.running
    assert launcher.launch(at(0, 16)) is False  # the last one is still running: no second learner
    assert ev.items[-1][0] == "learner_skipped" and ev.items[-1][1] == "warning"
    assert ev.items[-1][2]["reason"] == "previous_learner_still_running"
    release.set()
    await launcher.wait()
    assert calls == [1] and launcher.launches == 1
    assert launcher.launch(at(0, 17)) is True  # finished: the next launch is allowed
    await launcher.wait()
    assert launcher.launches == 2


async def test_cancelling_the_launcher_stops_the_child_and_says_so() -> None:
    ev, started = Events(), asyncio.Event()
    killed: list[bool] = []

    async def hang(argv: Any, timeout_s: float) -> LearnerResult:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            killed.append(True)  # what run_learner_process does: kill the child
            raise
        return LearnerResult(0, False, 0.0, "")

    launcher = LearnerLauncher(runner=hang, emit=ev)
    launcher.launch(at(0, 15))
    await started.wait()
    await launcher.cancel()
    assert killed == [True] and not launcher.running and ev.names()[-1] == "learner_cancelled"
    await launcher.cancel()  # idempotent


# ================================================================================================ the child


class FakeProc:
    def __init__(self, *, hang: bool = False, code: int = 0, out: bytes = b"") -> None:
        self.hang, self.returncode, self.out = hang, code, out
        self.killed = False

    async def communicate(self) -> tuple[bytes, None]:
        if self.hang:
            await asyncio.Event().wait()
        return self.out, None

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


def patch_spawn(monkeypatch: pytest.MonkeyPatch, proc: FakeProc) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    async def spawn(*args: Any, **kwargs: Any) -> FakeProc:
        seen["args"], seen["kwargs"] = args, kwargs
        return proc

    monkeypatch.setattr(schedule_module.asyncio, "create_subprocess_exec", spawn)
    return seen


async def test_the_child_is_the_learner_cli_once_at_low_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = patch_spawn(monkeypatch, FakeProc(out=b"x" * 5000 + b"tail"))
    result = await run_learner_process(["--config", "c.yml"], 60.0)
    assert seen["args"] == (sys.executable, "-m", "modules.dryrun.learner", "--once", "--config", "c.yml")
    kwargs = seen["kwargs"]
    if sys.platform == "win32":
        import subprocess

        assert kwargs["creationflags"] & subprocess.BELOW_NORMAL_PRIORITY_CLASS
    else:
        assert callable(kwargs["preexec_fn"])
    assert kwargs["stdin"] == asyncio.subprocess.DEVNULL  # a child that waits for input would hang to the timeout
    assert result.exit_code == 0 and not result.timed_out and result.tail.endswith("tail")
    assert len(result.tail) == schedule_module.TAIL_CHARS


async def test_a_child_that_exits_with_an_error_reports_its_code(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_spawn(monkeypatch, FakeProc(code=2, out=b"configuration error"))
    result = await run_learner_process([], 60.0)
    assert result.exit_code == 2 and "configuration error" in result.tail and not result.timed_out


async def test_a_child_past_its_timeout_is_killed(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = FakeProc(hang=True)
    patch_spawn(monkeypatch, proc)
    result = await run_learner_process([], 0.05)
    assert result.timed_out and result.exit_code is None and proc.killed


async def test_cancelling_the_runner_kills_the_child(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = FakeProc(hang=True)
    patch_spawn(monkeypatch, proc)
    task = asyncio.create_task(run_learner_process([], 60.0))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert proc.killed


async def test_a_real_child_runs_and_a_bad_config_is_a_failed_exit_not_a_crash(tmp_path: Path) -> None:
    """The real thing, once: the module exists, starts, rejects a config that is not there and exits."""
    result = await run_learner_process(["--config", str(tmp_path / "missing.yml")], 120.0)
    assert result.exit_code not in (None, 0) and not result.timed_out
