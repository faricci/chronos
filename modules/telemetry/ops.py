"""``OpsRecorder``: the operational numbers of each UTC day, in ``data/reports/ops/DAY.json`` (``chronos.ops.v1``).

This is the file ``modules/dryrun/report.py`` reads (``OpsDay``) to judge G-DRY-OPS. Without it uptime, crashes, bar
lags, spread and the kill-switch trips are ``unmeasured`` and the gate cannot pass. What it records, and how:

* **uptime** = seconds the process was up / seconds of the day since the agent was first seen. A tick every
  ``telemetry.ops_interval_s`` adds the elapsed time as up; a gap longer than ``MAX_GAP_S`` between two ticks (a frozen
  process, a suspended PC) and the time between a previous run's last sample and this start are DOWN time. A day the
  agent did not exist yet is not counted.
* **crashes**: a previous run that left ``running: true`` in the newest file never stopped cleanly: +1 on its day.
* **bars** per product: how many REST / flat / flat-unproven bars were published, the publication lag p50 / p99, the
  bars that arrived out of order or twice (seen here, independently of the engines), fetch failures.
* **kill switch**: every transition of "entries blocked" from no reason to a reason, per book (or ``*`` for the system
  guards), with ``bug`` set when the cause is a defect (a coverage breach, a dead task) and not the market or the feed.
* **median spread** per product from the live order book at each bar, the **maker fill rate** of the entries, the fee
  rates and their source, ``ws_reconnects``, ``decider_fallbacks``, ``skipped_late``, ``dry_run_violations``.

The file also keeps ``state`` (the samples the percentiles come from, the trips already seen) so a restart on the same
day goes on from where it was. Everything is called from the event loop; the file write runs in a thread. No method
raises: a failure is counted in ``TelemetryErrors`` (sez. 6.3.5).
"""

from __future__ import annotations

import asyncio
import json
import statistics
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Final

import numpy as np

from modules.market.bars import BarClosed
from modules.telemetry.files import TelemetryErrors, iso_z, write_json_atomic

__all__ = ["MAX_GAP_S", "OPS_V", "OpsRecorder"]

OPS_V: Final = "chronos.ops.v1"
MAX_GAP_S: Final = 300.0  # between two ticks: more than this is a frozen process, not an up process
SAMPLES_MAX: Final = 3000
COUNTERS: Final = ("ws_reconnects", "decider_fallbacks", "skipped_late")

Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _midnight(d: date) -> datetime:
    return datetime.combine(d, time(0), tzinfo=UTC)


def _parse(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


@dataclass
class _Bars:
    published: dict[str, int] = field(default_factory=lambda: {"rest": 0, "flat": 0, "flat_unproven": 0})
    lags: list[float] = field(default_factory=list)
    out_of_order: int = 0
    duplicates: int = 0
    fetch_failures: int = 0
    last_ts: str | None = None  # ``ts`` of the newest bar seen (ISO), for the order check across restarts


@dataclass
class _Day:
    date: date
    up_s: float = 0.0
    observed_s: float = 0.0
    crashes: int = 0
    running: bool = False
    first_sample_at: str | None = None
    last_sample_at: str | None = None
    counters: dict[str, int] = field(default_factory=lambda: dict.fromkeys(COUNTERS, 0))
    dry_run_violations: int = 0
    trips: list[dict[str, Any]] = field(default_factory=list)
    bars: dict[str, _Bars] = field(default_factory=dict)
    spreads: dict[str, list[float]] = field(default_factory=dict)
    maker_attempts: int = 0
    maker_fills: int = 0
    fees: dict[str, Any] = field(default_factory=dict)

    def bar(self, product: str) -> _Bars:
        return self.bars.setdefault(product, _Bars())

    # ----------------------------------------------------------------------------------- JSON

    def to_json(self) -> dict[str, Any]:
        bars: dict[str, Any] = {}
        for product, b in sorted(self.bars.items()):
            p50, p99 = (
                (float(np.percentile(b.lags, 50)), float(np.percentile(b.lags, 99))) if b.lags else (None, None)
            )
            bars[product] = {
                "rest": b.published["rest"], "flat": b.published["flat"],
                "flat_unproven": b.published["flat_unproven"], "lag_s_p50": p50, "lag_s_p99": p99,
                "fetch_failures": b.fetch_failures, "late": None, "out_of_order": b.out_of_order,
                "duplicates": b.duplicates,
            }  # fmt: skip
        return {
            "v": OPS_V,
            "date": self.date.isoformat(),
            "uptime": round(self.up_s / self.observed_s, 6) if self.observed_s > 0 else None,
            "up_s": round(self.up_s, 1),
            "observed_s": round(self.observed_s, 1),
            "crashes": self.crashes,
            "ws_reconnects": self.counters["ws_reconnects"],
            "decider_fallbacks": self.counters["decider_fallbacks"],
            "skipped_late": self.counters["skipped_late"],
            "dry_run_violations": self.dry_run_violations,
            "kill_switch_trips": [
                {"book": t["book"], "guard": t["guard"], "bug": t["bug"], "tripped_at": t["tripped_at"]}
                for t in self.trips
            ],
            "bars": bars,
            "median_spread_bps": {p: statistics.median(v) for p, v in sorted(self.spreads.items()) if v},
            "maker_fill_rate": self.maker_fills / self.maker_attempts if self.maker_attempts else None,
            "latency_ms_p75": None,  # REST round trip: not measured by this build
            "fee_maker_bps": self.fees.get("maker_bps"),
            "fee_taker_bps": self.fees.get("taker_bps"),
            "fee_source": self.fees.get("source"),
            "running": self.running,
            "first_sample_at": self.first_sample_at,
            "last_sample_at": self.last_sample_at,
            "state": {
                "lags": {p: b.lags[-SAMPLES_MAX:] for p, b in self.bars.items()},
                "last_bar_ts": {p: b.last_ts for p, b in self.bars.items()},
                "spreads": {p: v[-SAMPLES_MAX:] for p, v in self.spreads.items()},
                "maker": [self.maker_attempts, self.maker_fills],
            },
        }  # fmt: skip

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> _Day:
        day = cls(date.fromisoformat(str(raw["date"])))
        day.up_s, day.observed_s = float(raw.get("up_s", 0.0)), float(raw.get("observed_s", 0.0))
        day.crashes, day.running = int(raw.get("crashes", 0)), bool(raw.get("running", False))
        day.first_sample_at, day.last_sample_at = raw.get("first_sample_at"), raw.get("last_sample_at")
        for name in COUNTERS:
            day.counters[name] = int(raw.get(name) or 0)
        day.dry_run_violations = int(raw.get("dry_run_violations") or 0)
        day.trips = [
            {"book": t["book"], "guard": t["guard"], "bug": bool(t["bug"]), "tripped_at": t.get("tripped_at")}
            for t in raw.get("kill_switch_trips", [])
        ]
        for product, b in (raw.get("bars") or {}).items():
            acc = day.bar(product)
            acc.published = {k: int(b.get(k) or 0) for k in ("rest", "flat", "flat_unproven")}
            acc.out_of_order, acc.duplicates = int(b.get("out_of_order") or 0), int(b.get("duplicates") or 0)
            acc.fetch_failures = int(b.get("fetch_failures") or 0)
        state = raw.get("state") or {}
        for product, lags in (state.get("lags") or {}).items():
            day.bar(product).lags = [float(x) for x in lags]
        for product, ts in (state.get("last_bar_ts") or {}).items():
            day.bar(product).last_ts = ts
        day.spreads = {p: [float(x) for x in v] for p, v in (state.get("spreads") or {}).items()}
        day.maker_attempts, day.maker_fills = (int(x) for x in (state.get("maker") or [0, 0]))
        day.fees = {
            "maker_bps": raw.get("fee_maker_bps"), "taker_bps": raw.get("fee_taker_bps"),
            "source": raw.get("fee_source"),
        }  # fmt: skip
        return day


class OpsRecorder:
    def __init__(
        self,
        directory: Path,
        *,
        clock: Clock = _utc_now,
        sleep: Sleep = asyncio.sleep,
        interval_s: float = 60.0,
        errors: TelemetryErrors | None = None,
    ) -> None:
        self._dir = Path(directory)
        self._clock = clock
        self._sleep = sleep
        self._interval = interval_s
        self._errors = errors or TelemetryErrors()
        self._days: dict[date, _Day] = {}
        self._dirty: set[date] = set()
        self._last_tick: datetime | None = None
        self._counter_last: dict[str, int] = {}
        self._guard_state: dict[str, str | None] = {}
        self.started = False

    # --------------------------------------------------------------------------------------- days

    def _path(self, d: date) -> Path:
        return self._dir / f"{d.isoformat()}.json"

    def _day(self, d: date) -> _Day:
        day = self._days.get(d)
        if day is None:
            day = _load_day(self._path(d)) or _Day(d)
            self._days[d] = day
        return day

    def _add_interval(self, t0: datetime, t1: datetime, *, up: bool) -> None:
        """Add the time from ``t0`` to ``t1`` to the observed time of each UTC day it covers (and to the up time)."""
        cursor = t0
        while cursor < t1:
            d = cursor.date()
            end = min(t1, _midnight(d + timedelta(days=1)))
            day = self._day(d)
            seconds = (end - cursor).total_seconds()
            day.observed_s += seconds
            day.up_s += seconds if up else 0.0
            if day.first_sample_at is None:
                day.first_sample_at = iso_z(cursor)
            self._dirty.add(d)
            cursor = end

    # -------------------------------------------------------------------------------- life cycle

    def start(self) -> None:
        """Account for the previous run (a crash, the downtime) and open today. Never raises."""
        try:
            now = self._clock()
            self._dir.mkdir(parents=True, exist_ok=True)
            previous = _newest(self._dir)
            if previous is not None:
                self._days[previous.date] = previous
                if previous.running:  # it never wrote its clean stop
                    previous.crashes += 1
                    previous.running = False
                    self._dirty.add(previous.date)
                last = _parse(previous.last_sample_at)
                if last is not None and now > last:
                    self._add_interval(last, now, up=False)  # the time nobody was running
            today = self._day(now.date())
            # the order check is per process: a restart republishes the flat tail of the minutes the file of the day
            # already holds, which is neither a duplicate nor out of order. The counts of the day stay.
            for loaded in (previous, today):
                for acc in () if loaded is None else loaded.bars.values():
                    acc.last_ts = None
            today.running = True
            today.last_sample_at = iso_z(now)
            if today.first_sample_at is None:
                today.first_sample_at = iso_z(now)
            self._dirty.add(today.date)
            self._last_tick = now
            self.started = True
        except Exception as exc:
            self._errors.record("ops_start", exc)

    def tick(self) -> None:
        """Time passes: the process was up since the last tick (unless it was frozen)."""
        try:
            now = self._clock()
            last = self._last_tick
            if last is not None and now > last:
                if (now - last).total_seconds() <= MAX_GAP_S:
                    self._add_interval(last, now, up=True)
                else:
                    self._add_interval(last, now, up=False)
            self._last_tick = now
            today = self._day(now.date())
            today.running = True
            today.last_sample_at = iso_z(now)
            self._dirty.add(today.date)
        except Exception as exc:
            self._errors.record("ops_tick", exc)

    def stop(self) -> None:
        """Clean stop: the last interval is up time, ``running`` goes false. Call ``flush`` after it."""
        try:
            self.tick()
            self._day(self._clock().date()).running = False
            self._dirty.add(self._clock().date())
        except Exception as exc:
            self._errors.record("ops_stop", exc)

    # ---------------------------------------------------------------------------------- observers

    def observe_bar(self, event: BarClosed) -> None:
        try:
            day = self._day(self._clock().date())
            b = day.bar(event.product_id)
            ts = event.bar.ts.astimezone(UTC).isoformat()
            if b.last_ts is not None:
                prev = _parse(b.last_ts)
                if prev is not None and event.bar.ts == prev:
                    b.duplicates += 1
                elif prev is not None and event.bar.ts < prev:
                    b.out_of_order += 1
            if b.last_ts is None or (_parse(b.last_ts) or event.bar.ts) <= event.bar.ts:
                b.last_ts = ts
            b.published[event.source] = b.published.get(event.source, 0) + 1
            b.lags.append(round(event.lag_s, 3))
            del b.lags[:-SAMPLES_MAX]
            self._dirty.add(day.date)
        except Exception as exc:
            self._errors.record("ops_bar", exc)

    def observe_spread(self, product: str, bps: float | None) -> None:
        try:
            if bps is None:
                return
            day = self._day(self._clock().date())
            samples = day.spreads.setdefault(product, [])
            samples.append(round(float(bps), 4))
            del samples[:-SAMPLES_MAX]
            self._dirty.add(day.date)
        except Exception as exc:
            self._errors.record("ops_spread", exc)

    def observe_entry(self, *, maker: bool, filled: bool) -> None:
        """An entry order reached its end: ``filled`` or not (a maker entry that expired is a failed attempt)."""
        try:
            if not maker:
                return
            day = self._day(self._clock().date())
            day.maker_attempts += 1
            day.maker_fills += int(filled)
            self._dirty.add(day.date)
        except Exception as exc:
            self._errors.record("ops_entry", exc)

    def observe_counter(self, name: str, value: int) -> None:
        """A cumulative counter of this process (``ws_reconnects``...): the day gets the increase since the last call;
        a counter that went down (new process) starts again from its value."""
        try:
            last = self._counter_last.get(name)
            delta = value if last is None or value < last else value - last
            self._counter_last[name] = value
            if delta:
                day = self._day(self._clock().date())
                if name in day.counters:
                    day.counters[name] += delta
                self._dirty.add(day.date)
        except Exception as exc:
            self._errors.record("ops_counter", exc)

    def observe_fetch_failures(self, product: str, value: int) -> None:
        try:
            key = f"fetch_failures:{product}"
            last = self._counter_last.get(key)
            delta = value if last is None or value < last else value - last
            self._counter_last[key] = value
            if delta:
                day = self._day(self._clock().date())
                day.bar(product).fetch_failures += delta
                self._dirty.add(day.date)
        except Exception as exc:
            self._errors.record("ops_fetch_failures", exc)

    def observe_guard(self, scope: str, reason: str | None, *, bug: bool = False) -> tuple[str | None, str | None]:
        """The entry-blocking reason of a book (or ``*`` for the system) as of now. A change from "not blocked" to a
        reason is a trip and is written; returns ``(tripped, cleared)`` guard names so the caller can emit events."""
        tripped = cleared = None
        try:
            before = self._guard_state.get(scope)
            self._guard_state[scope] = reason
            if reason is not None and before is None:
                guard = reason.split(":")[0]
                now = self._clock()
                day = self._day(now.date())
                entry = {
                    "book": scope, "guard": guard, "bug": bool(bug or guard == "coverage_breach"),
                    "tripped_at": iso_z(now),
                }  # fmt: skip
                if not any(
                    t["book"] == scope and t["guard"] == guard and t["tripped_at"] == entry["tripped_at"]
                    for t in day.trips
                ):
                    day.trips.append(entry)
                self._dirty.add(day.date)
                tripped = guard
            elif reason is None and before is not None:
                cleared = before.split(":")[0]
        except Exception as exc:
            self._errors.record("ops_guard", exc)
        return tripped, cleared

    def violation(self) -> None:
        """A ``DryRunViolation`` was raised: the gate wants zero of them."""
        try:
            day = self._day(self._clock().date())
            day.dry_run_violations += 1
            self._dirty.add(day.date)
        except Exception as exc:
            self._errors.record("ops_violation", exc)

    def set_fees(self, maker_bps: float, taker_bps: float, source: str) -> None:
        try:
            day = self._day(self._clock().date())
            day.fees = {"maker_bps": maker_bps, "taker_bps": taker_bps, "source": source}
            self._dirty.add(day.date)
        except Exception as exc:
            self._errors.record("ops_fees", exc)

    # ------------------------------------------------------------------------------------ output

    def today(self) -> dict[str, Any]:
        """The document of the current day as it would be written now (tests and ``status``)."""
        return self._day(self._clock().date()).to_json()

    async def flush(self) -> int:
        """Write every day that changed. Returns the number of files written; never raises."""
        written = 0
        try:
            dirty, self._dirty = sorted(self._dirty), set()
            for d in dirty:
                try:
                    await asyncio.to_thread(write_json_atomic, self._path(d), self._day(d).to_json())
                    written += 1
                except Exception as exc:
                    self._dirty.add(d)  # try again at the next flush
                    self._errors.record("ops_write", exc)
        except Exception as exc:
            self._errors.record("ops_flush", exc)
        return written

    async def run(self) -> None:
        """Tick and flush every ``interval_s`` (the supervised task of the app)."""
        if not self.started:
            self.start()
        while True:
            self.tick()
            await self.flush()
            await self._sleep(self._interval)


def _load_day(path: Path) -> _Day | None:
    try:
        return _Day.from_json(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _newest(directory: Path) -> _Day | None:
    for path in sorted(directory.glob("????-??-??.json"), reverse=True):
        day = _load_day(path)
        if day is not None:
            return day
    return None
