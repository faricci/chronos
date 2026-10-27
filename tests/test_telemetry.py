"""Telemetry (spec sez. 6.3): status.json, the event log, the equity rows and the ops of the day. None of it may stop
trading (6.3.5), and the ops file is exactly what ``modules/dryrun/report.py`` reads to judge G-DRY-OPS."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import structlog

from modules.config import GateOpsConfig
from modules.dryrun.report import JsonOpsSource, OpsDay, evaluate_gate_ops
from modules.market.bars import BarClosed
from modules.market.timeframes import Bar
from modules.telemetry.events import EventLog, LogForwarder
from modules.telemetry.files import TelemetryErrors, dumps_line, write_json_atomic
from modules.telemetry.ops import MAX_GAP_S, OpsRecorder
from modules.telemetry.status import EquityWriter, StatusWriter

T0 = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


def lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


# ------------------------------------------------------------------------------------------ files


def test_write_json_atomic_replaces_the_file_and_leaves_no_temporary(tmp_path: Path) -> None:
    target = tmp_path / "t" / "status.json"
    write_json_atomic(target, {"a": 1})
    write_json_atomic(target, {"a": 2, "nan": float("nan"), "inf": float("inf")})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 2, "nan": None, "inf": None}
    assert [p.name for p in target.parent.iterdir()] == ["status.json"]


def test_dumps_line_is_one_strict_json_line() -> None:
    from decimal import Decimal

    out = dumps_line({"m": Decimal("1.50"), "t": T0, "s": "a\nb", "n": float("nan")})
    assert "\n" not in out and json.loads(out) == {"m": "1.50", "t": str(T0), "s": "a\nb", "n": None}


def test_telemetry_errors_are_counted_logged_once_a_minute_and_never_raise(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    t = [0.0]
    errors = TelemetryErrors(lambda: t[0], log_every_s=60)
    for _ in range(5):
        errors.record("disk", OSError("full"))
    t[0] = 61.0
    errors.record("disk", OSError("full"))
    errors.record("other", ValueError("x"))
    assert errors.total == 7 and errors.errors_1h() == 7
    t[0] = 3700.0
    assert errors.errors_1h() == 0 and errors.total == 7


# -------------------------------------------------------------------------------------------- events


def test_events_are_written_one_per_line_in_the_file_of_their_day(tmp_path: Path) -> None:
    clock = Clock(datetime(2026, 10, 19, 23, 59, 59, tzinfo=UTC))
    log = EventLog(tmp_path / "events", clock=clock, flush_s=0.01)
    log.start()
    log.emit("agent_started", "info", data={"mode": "dry"})
    clock.advance(seconds=2)
    log.emit("coverage_refused", "warning", book="champion", product="SOL-EUR", data={"reason": "insufficient"})
    log.stop()
    first = lines(tmp_path / "events" / "2026-10-19.jsonl")
    second = lines(tmp_path / "events" / "2026-10-20.jsonl")
    assert [e["type"] for e in first + second] == ["agent_started", "coverage_refused"]
    e = second[0]
    assert e["v"] == "chronos.event.v1" and e["severity"] == "warning" and e["ts"] == "2026-10-20T00:00:01.000Z"
    assert (e["book"], e["product"], e["data"]) == ("champion", "SOL-EUR", {"reason": "insufficient"})
    assert len(e["event_id"]) == 26 and first[0]["event_id"] != e["event_id"]


def test_a_full_queue_drops_counts_and_reports_without_blocking(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events", queue_max=3, flush_s=0.01)  # the thread is not started: the queue fills
    for i in range(10):
        log.emit("x", data={"i": i})
    assert log.dropped_total == 7
    log.start()
    log.emit("late", data={})  # may or may not fit; the writer then reports the drops
    log.stop()
    events = [e for p in (tmp_path / "events").glob("*.jsonl") for e in lines(p)]
    dropped = [e for e in events if e["type"] == "telemetry_dropped"]
    assert [e["data"]["i"] for e in events if e["type"] == "x"] == [0, 1, 2]
    assert dropped and sum(e["data"]["count"] for e in dropped) == log.dropped_total


def test_the_redaction_runs_before_anything_is_stored(tmp_path: Path) -> None:
    def scrub(record: Any) -> Any:
        return json.loads(json.dumps(record).replace("SECRET", "***"))

    log = EventLog(tmp_path / "events", redact=scrub, flush_s=0.01)
    log.start()
    log.emit("x", data={"k": "has SECRET inside"})
    log.stop()
    (event,) = [e for p in (tmp_path / "events").glob("*.jsonl") for e in lines(p)]
    assert event["data"]["k"] == "has *** inside"


def test_emit_never_raises_and_a_dead_disk_is_a_counted_error(tmp_path: Path) -> None:
    blocker = tmp_path / "events"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")
    errors = TelemetryErrors()
    log = EventLog(blocker, errors=errors, flush_s=0.01)
    log.start()
    log.emit("x")
    log.stop()
    assert errors.total >= 1 and log.written_total == 0

    def bad(_r: Any) -> Any:
        raise RuntimeError("redactor bug")

    log2 = EventLog(tmp_path / "ev2", redact=bad, errors=errors)
    log2.emit("x")  # must not raise
    assert errors.total >= 2


def test_old_daily_files_are_purged_at_start(tmp_path: Path) -> None:
    root = tmp_path / "events"
    root.mkdir()
    for name in ("2026-01-01.jsonl", "2026-10-18.jsonl", "notes.jsonl"):
        (root / name).write_text("{}\n", encoding="utf-8")
    log = EventLog(root, clock=Clock(), retention_days=30)
    log.start()
    log.stop()
    assert sorted(p.name for p in root.iterdir()) == ["2026-10-18.jsonl", "notes.jsonl"]


def test_the_structlog_processor_forwards_errors_and_typed_facts(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events", flush_s=0.01)
    forwarder = LogForwarder()
    forwarder({}, "info", {"event": "x", "level": "error"})  # no target yet: nothing happens, nothing raises
    forwarder.target = log
    log.start()
    forwarder({}, "info", {"event": "plain_info", "level": "info"})
    forwarder({}, "error", {"event": "task_failed", "level": "error", "task": "engine", "error": "boom"})
    forwarder({}, "error", {"event": "telemetry_error", "level": "error"})  # a telemetry failure never feeds itself
    refused = {"event": "coverage_refused", "level": "info", "side": "BUY", "reason": "insufficient_balance"}
    forwarder({}, "info", refused)
    forwarder({}, "error", {"event": "x", "level": "error", "blob": "y" * 5000})
    exit_log = {
        "event": "watchdog_exit", "level": "error", "book": "random_entry", "product_id": "SOL-EUR", "cause": "HORIZON",
    }
    forwarder({}, "error", exit_log)  # routine, logged at error level by the watchdog: one typed warning
    forwarder({}, "error", {"event": "watchdog_exit_done", "level": "error", "result": "..."})  # not an error at all
    log.stop()
    events = [e for p in (tmp_path / "events").glob("*.jsonl") for e in lines(p)]
    assert [e["type"] for e in events] == ["log_error", "coverage_refused", "log_error", "watchdog_exit"]
    assert (events[3]["severity"], events[3]["book"], events[3]["product"]) == ("warning", "random_entry", "SOL-EUR")
    assert "task=engine" in events[0]["data"]["message"]
    assert events[1]["data"] == {"side": "BUY", "reason": "insufficient_balance"}
    assert len(events[2]["data"]["message"]) <= 2048


def test_the_forwarder_works_inside_a_structlog_chain(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events", flush_s=0.01)
    forwarder = LogForwarder()
    forwarder.target = log
    log.start()
    structlog.configure(
        processors=[structlog.processors.add_log_level, forwarder, structlog.processors.KeyValueRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.ReturnLoggerFactory(),
        cache_logger_on_first_use=False,
    )
    structlog.get_logger("t").error("something_broke", why="test")
    log.stop()
    (event,) = [e for p in (tmp_path / "events").glob("*.jsonl") for e in lines(p)]
    assert event["type"] == "log_error" and event["data"]["event"] == "something_broke"
    structlog.reset_defaults()


# -------------------------------------------------------------------------------- status and equity


async def test_status_is_written_atomically_with_its_envelope(tmp_path: Path) -> None:
    clock = Clock()
    writer = StatusWriter(tmp_path / "telemetry", lambda: {"agent": {"mode": "dry"}}, clock=clock)
    assert await writer.write_once()
    doc = json.loads((tmp_path / "telemetry" / "status.json").read_text(encoding="utf-8"))
    assert doc == {"v": "chronos.status.v1", "written_at": "2026-10-19T10:00:00.000Z", "agent": {"mode": "dry"}}
    assert [p.name for p in (tmp_path / "telemetry").iterdir()] == ["status.json"]


async def test_a_failing_snapshot_is_counted_and_the_loop_survives(tmp_path: Path) -> None:
    calls = {"n": 0}

    def snapshot() -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("a bug in the snapshot")
        return {"n": calls["n"]}

    errors = TelemetryErrors()
    sleeps: list[float] = []

    async def sleep(s: float) -> None:
        sleeps.append(s)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError  # the shutdown: the only thing that leaves the loop
        await asyncio.sleep(0)

    writer = StatusWriter(tmp_path, snapshot, interval_s=10, sleep=sleep, errors=errors)
    with pytest.raises(asyncio.CancelledError):
        await writer.run()
    assert calls["n"] == 3 and errors.total == 1 and writer.writes == 2
    assert json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))["n"] == 3
    assert sleeps == [10, 10, 10]


async def test_status_on_a_dead_directory_does_not_raise(tmp_path: Path) -> None:
    blocker = tmp_path / "telemetry"
    blocker.write_text("file", encoding="utf-8")
    errors = TelemetryErrors()
    assert not await StatusWriter(blocker, lambda: {}, errors=errors).write_once()
    assert errors.total == 1


async def test_equity_rows_are_appended_to_the_file_of_the_day(tmp_path: Path) -> None:
    clock = Clock()

    async def rows() -> list[dict[str, Any]]:
        return [{"book": "champion", "mode": "dry", "equity": "100.31"}, {"book": "random_entry", "equity": "99.90"}]

    writer = EquityWriter(tmp_path / "equity", rows, clock=clock)
    assert await writer.write_once() == 2
    clock.advance(minutes=1)
    assert await writer.write_once() == 2
    got = lines(tmp_path / "equity" / "2026-10-19.jsonl")
    assert [(r["ts"], r["book"]) for r in got] == [
        ("2026-10-19T10:00:00Z", "champion"), ("2026-10-19T10:00:00Z", "random_entry"),
        ("2026-10-19T10:01:00Z", "champion"), ("2026-10-19T10:01:00Z", "random_entry"),
    ]  # fmt: skip
    assert got[0]["v"] == "chronos.equity.v1" and got[0]["equity"] == "100.31"


async def test_an_equity_provider_that_raises_is_a_counted_error(tmp_path: Path) -> None:
    async def rows() -> list[dict[str, Any]]:
        raise RuntimeError("account bug")

    errors = TelemetryErrors()
    assert await EquityWriter(tmp_path, rows, errors=errors).write_once() == 0 and errors.total == 1


# ---------------------------------------------------------------------------------------------- ops


def bar_event(product: str, minute: datetime, *, lag: float = 120.0, source: str = "rest") -> BarClosed:
    return BarClosed(
        product, Bar(minute, 1.0, 1.0, 1.0, 1.0, 1.0), source,  # type: ignore[arg-type]
        minute + timedelta(seconds=60 + lag),
    )  # fmt: skip


def recorder(tmp_path: Path, clock: Clock, **kw: Any) -> OpsRecorder:
    return OpsRecorder(tmp_path / "ops", clock=clock, **kw)


async def test_uptime_is_up_time_over_observed_time_and_the_report_reads_the_file(tmp_path: Path) -> None:
    clock = Clock()
    ops = recorder(tmp_path, clock)
    ops.start()
    for _ in range(10):
        clock.advance(seconds=60)
        ops.tick()
    ops.stop()
    await ops.flush()
    doc = json.loads((tmp_path / "ops" / "2026-10-19.json").read_text(encoding="utf-8"))
    assert doc["v"] == "chronos.ops.v1" and doc["uptime"] == 1.0 and doc["up_s"] == 600.0 and not doc["running"]
    assert doc["crashes"] == 0 and doc["dry_run_violations"] == 0
    day = JsonOpsSource(tmp_path / "ops").day(date(2026, 10, 19))
    assert isinstance(day, OpsDay) and day.uptime == 1.0 and day.crashes == 0


async def test_a_frozen_process_and_the_downtime_between_runs_are_not_uptime(tmp_path: Path) -> None:
    clock = Clock()
    ops = recorder(tmp_path, clock)
    ops.start()
    clock.advance(seconds=60)
    ops.tick()  # 60 s up
    clock.advance(seconds=MAX_GAP_S + 1)
    ops.tick()  # a gap too long to be a running process: down
    ops.stop()
    await ops.flush()
    clock.advance(seconds=1000)  # nobody runs for 1000 s
    again = recorder(tmp_path, clock)
    again.start()
    clock.advance(seconds=60)
    again.tick()
    doc = again.today()
    assert doc["up_s"] == 120.0 and doc["observed_s"] == pytest.approx(60 + MAX_GAP_S + 1 + 1000 + 60, abs=1)
    assert 0 < doc["uptime"] < 0.2 and doc["crashes"] == 0


async def test_a_run_that_never_stopped_is_a_crash_of_its_day(tmp_path: Path) -> None:
    clock = Clock()
    first = recorder(tmp_path, clock)
    first.start()
    clock.advance(seconds=30)
    first.tick()
    await first.flush()  # ... and the process dies here: running stays true
    clock.advance(seconds=100)
    second = recorder(tmp_path, clock)
    second.start()
    assert second.today()["crashes"] == 1
    second.stop()
    await second.flush()
    third = recorder(tmp_path, clock)
    third.start()
    assert third.today()["crashes"] == 1  # a clean stop adds none, the crash is not counted twice


async def test_the_time_of_a_downtime_is_split_across_the_days_it_covers(tmp_path: Path) -> None:
    clock = Clock(datetime(2026, 10, 19, 23, 0, tzinfo=UTC))
    first = recorder(tmp_path, clock)
    first.start()
    first.stop()
    await first.flush()
    clock.now = datetime(2026, 10, 20, 1, 0, tzinfo=UTC)  # two hours of nobody running over midnight
    second = recorder(tmp_path, clock)
    second.start()
    await second.flush()
    d19 = json.loads((tmp_path / "ops" / "2026-10-19.json").read_text(encoding="utf-8"))
    d20 = json.loads((tmp_path / "ops" / "2026-10-20.json").read_text(encoding="utf-8"))
    assert d19["observed_s"] == 3600.0 and d19["uptime"] == 0.0
    assert d20["observed_s"] == 3600.0 and d20["uptime"] == 0.0


async def test_bars_lags_order_and_duplicates(tmp_path: Path) -> None:
    clock = Clock()
    ops = recorder(tmp_path, clock)
    ops.start()
    t = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
    for i, lag in enumerate((120.0, 121.0, 122.0, 130.0)):
        ops.observe_bar(bar_event("SOL-EUR", t + timedelta(minutes=i), lag=lag))
    ops.observe_bar(bar_event("SOL-EUR", t + timedelta(minutes=3), lag=125.0))  # twice
    ops.observe_bar(bar_event("SOL-EUR", t + timedelta(minutes=1), lag=125.0))  # late and out of order
    ops.observe_bar(bar_event("SOL-EUR", t + timedelta(minutes=4), source="flat"))
    ops.observe_fetch_failures("SOL-EUR", 2)
    ops.observe_fetch_failures("SOL-EUR", 5)
    b = ops.today()["bars"]["SOL-EUR"]
    assert (b["rest"], b["flat"], b["flat_unproven"]) == (6, 1, 0) and (b["duplicates"], b["out_of_order"]) == (1, 1)
    assert b["fetch_failures"] == 5 and 120.0 <= b["lag_s_p50"] <= 125.0 and b["lag_s_p99"] <= 130.0


async def test_spread_fill_rate_fees_counters_and_violations(tmp_path: Path) -> None:
    clock = Clock()
    ops = recorder(tmp_path, clock)
    ops.start()
    for bps in (2.0, 4.0, 3.0, None):
        ops.observe_spread("BTC-EUR", bps)
    for filled in (True, False, True, True):
        ops.observe_entry(maker=True, filled=filled)
    ops.observe_entry(maker=False, filled=False)  # a taker entry says nothing about the maker queue
    ops.observe_counter("ws_reconnects", 2)
    ops.observe_counter("ws_reconnects", 5)
    ops.observe_counter("ws_reconnects", 1)  # a new process: starts again
    ops.observe_counter("decider_fallbacks", 4)
    ops.set_fees(25, 50, "account")
    ops.violation()
    doc = ops.today()
    assert doc["median_spread_bps"] == {"BTC-EUR": 3.0} and doc["maker_fill_rate"] == 0.75
    assert doc["ws_reconnects"] == 2 + 3 + 1 and doc["decider_fallbacks"] == 4 and doc["dry_run_violations"] == 1
    assert (doc["fee_maker_bps"], doc["fee_taker_bps"], doc["fee_source"]) == (25, 50, "account")


async def test_kill_switch_trips_are_transitions_with_a_bug_flag(tmp_path: Path) -> None:
    clock = Clock()
    ops = recorder(tmp_path, clock)
    ops.start()
    assert ops.observe_guard("*", None) == (None, None)
    assert ops.observe_guard("*", "stale_bar:BTC-EUR") == ("stale_bar", None)
    assert ops.observe_guard("*", "stale_bar:BTC-EUR") == (None, None)  # still the same trip
    clock.advance(minutes=5)
    assert ops.observe_guard("*", None) == (None, "stale_bar")
    clock.advance(minutes=5)
    ops.observe_guard("champion", "coverage_breach")
    ops.observe_guard("random_entry", "decider_health", bug=True)
    ops.observe_guard("buy_and_hold", "loss_streak")
    trips = ops.today()["kill_switch_trips"]
    assert [(t["book"], t["guard"], t["bug"]) for t in trips] == [
        ("*", "stale_bar", False), ("champion", "coverage_breach", True),
        ("random_entry", "decider_health", True), ("buy_and_hold", "loss_streak", False),
    ]  # fmt: skip


async def test_a_restart_on_the_same_day_continues_the_counts(tmp_path: Path) -> None:
    clock = Clock()
    first = recorder(tmp_path, clock)
    first.start()
    t = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
    for i in range(3):
        first.observe_bar(bar_event("ETH-EUR", t + timedelta(minutes=i)))
    first.observe_spread("ETH-EUR", 2.0)
    first.observe_entry(maker=True, filled=True)
    first.observe_guard("*", "drift")
    first.stop()
    await first.flush()
    clock.advance(seconds=30)
    second = recorder(tmp_path, clock)
    second.start()
    second.observe_bar(bar_event("ETH-EUR", t + timedelta(minutes=3)))
    second.observe_bar(bar_event("ETH-EUR", t + timedelta(minutes=2)))  # older than the last one of the first run
    second.observe_guard("*", "drift")
    doc = second.today()
    assert doc["bars"]["ETH-EUR"]["rest"] == 5 and doc["bars"]["ETH-EUR"]["out_of_order"] == 1
    assert doc["median_spread_bps"] == {"ETH-EUR": 2.0} and doc["maker_fill_rate"] == 1.0
    assert len(doc["kill_switch_trips"]) == 2  # the same guard tripping again in the new run is a new trip


async def test_a_restart_with_a_flat_tail_is_not_an_order_anomaly(tmp_path: Path) -> None:
    """The order check is per process: after a restart the engine republishes the flat minutes the file of the day
    already holds (older than its last bar). That is not a duplicate and not out of order; a real one still is."""
    clock = Clock()
    first = recorder(tmp_path, clock)
    first.start()
    t = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
    for i in range(4):
        first.observe_bar(bar_event("SOL-EUR", t + timedelta(minutes=i)))
    first.stop()
    await first.flush()
    clock.advance(seconds=30)
    second = recorder(tmp_path, clock)
    second.start()
    for i in (1, 2, 3, 4):  # the tail of the first run is published again, flat, then the new minute
        second.observe_bar(bar_event("SOL-EUR", t + timedelta(minutes=i), source="flat"))
    b = second.today()["bars"]["SOL-EUR"]
    assert (b["duplicates"], b["out_of_order"]) == (0, 0) and b["rest"] == 4 and b["flat"] == 4
    second.observe_bar(bar_event("SOL-EUR", t + timedelta(minutes=4)))  # inside the same process: a duplicate
    second.observe_bar(bar_event("SOL-EUR", t + timedelta(minutes=2)))  # and an old one
    b = second.today()["bars"]["SOL-EUR"]
    assert (b["duplicates"], b["out_of_order"]) == (1, 1)


async def test_a_failing_write_is_retried_at_the_next_flush(tmp_path: Path) -> None:
    clock = Clock()
    blocker = tmp_path / "ops"
    blocker.write_text("not a directory", encoding="utf-8")
    errors = TelemetryErrors()
    ops = OpsRecorder(blocker, clock=clock, errors=errors)
    ops.start()
    ops.tick()
    assert await ops.flush() == 0 and errors.total >= 1
    blocker.unlink()
    assert await ops.flush() >= 1 and (blocker / "2026-10-19.json").exists()


async def test_the_gate_can_pass_the_operational_criteria_it_could_not_measure_before(tmp_path: Path) -> None:
    """With the ops files of a week the report no longer says ``unmeasured`` for uptime, crashes and violations."""
    clock = Clock(datetime(2026, 10, 12, 0, 0, tzinfo=UTC))
    ops = recorder(tmp_path, clock)
    ops.start()
    for i in range(7 * 24 * 12):  # a tick every 5 minutes, the longest gap that still counts as running
        clock.advance(minutes=5)
        ops.tick()
        if i % 12 == 0:
            await ops.flush()
    ops.stop()
    await ops.flush()
    src = JsonOpsSource(tmp_path / "ops")
    days = []
    for d in range(12, 19):
        o = src.day(date(2026, 10, d))
        assert o is not None and o.uptime is not None
        days.append({"ops": {"uptime": o.uptime, "crashes": o.crashes, "dry_run_violations": o.dry_run_violations}})
    gate = evaluate_gate_ops(days, GateOpsConfig(), "champion")
    status = {c["name"]: c["status"] for c in gate["criteria"]}
    assert [status[k] for k in ("uptime", "zero_crashes", "zero_dry_run_violations")] == ["pass"] * 3
