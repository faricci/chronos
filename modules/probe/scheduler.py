"""Slots of the probe (plan sez. 1.6, 2): at most one entry per slot, decided deterministically, never repeated.

A slot is a UTC instant of ``entry_slots_utc`` on a Monday-to-Friday. It is served during ``SLOT_WINDOW_S`` after its
time and never later (a process started after the window skips it). In the window the planner is asked every ``poll_s``:

* an intent -> the entry is started and the slot is closed (``entered``);
* a transient skip (stale quote, wide spread, ...) -> asked again until the window closes, then the slot is closed with
  the last reason;
* any other skip -> the slot is closed at once with its reason (``probe_skip`` event).

The memory of the closed slots is the ledger (``meta`` rows, and the order itself via its deterministic
``client_order_id``): after a restart the same slot is never served twice. The slot is closed BEFORE the entry is
started, so a crash in between costs a slot, never a duplicate.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final, Protocol

import structlog

from modules.config import ExecProbeConfig
from modules.execution.balance import Clock, utc_now
from modules.execution.ids import client_order_id
from modules.ledger.positions import OrderRecord
from modules.probe import limits
from modules.probe.planner import PlanFacts, ProbePlanner, ProbeSkip, decision_id
from modules.risk.risk_manager import OrderIntent

log = structlog.get_logger(__name__)

DEFAULT_POLL_S: Final = 5.0
META_PREFIX: Final = "probe.slot."

EventSink = Callable[[str, dict[str, Any]], None]
Gather = Callable[[datetime, datetime], Awaitable[PlanFacts]]
StartEntry = Callable[[OrderIntent], None]
FinishFn = Callable[[str, str], None]


class SlotLedger(Protocol):
    async def meta_get(self, key: str) -> str | None: ...
    async def meta_set(self, key: str, value: str) -> None: ...
    async def get_order(self, client_order_id: str, book: str | None = None) -> OrderRecord | None: ...


def slot_times(day: date, cfg: ExecProbeConfig) -> list[datetime]:
    """The slots of one UTC day (none on Saturday and Sunday), in time order."""
    if day.weekday() not in limits.ENTRY_WEEKDAYS:
        return []
    out: list[datetime] = []
    for label in sorted(cfg.entry_slots_utc):
        hour, minute = (int(x) for x in label.split(":"))
        out.append(datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC))
    return out


def due_slot(now: datetime, cfg: ExecProbeConfig) -> datetime | None:
    """The slot being served at ``now`` (its time <= now < its time + window), or ``None``."""
    now = now.astimezone(UTC)
    window = timedelta(seconds=limits.SLOT_WINDOW_S)
    for slot in slot_times(now.date(), cfg):
        if slot <= now < slot + window:
            return slot
    return None


class ProbeScheduler:
    def __init__(
        self,
        *,
        cfg: ExecProbeConfig,
        product_id: str,
        ledger: SlotLedger,
        planner: ProbePlanner,
        gather: Gather,
        start_entry: StartEntry,
        on_event: EventSink,
        finish: FinishFn,
        clock: Clock = utc_now,
        poll_s: float = DEFAULT_POLL_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._cfg = cfg
        self._product = product_id
        self._ledger = ledger
        self._planner = planner
        self._gather = gather
        self._start = start_entry
        self._event = on_event
        self._finish = finish
        self._clock = clock
        self._poll_s = poll_s
        self._sleep = sleep
        self._last_reason: dict[datetime, str] = {}
        self._closed: set[datetime] = set()  # slots closed in this process (the ledger remembers the rest)

    async def _is_closed(self, slot: datetime) -> bool:
        if slot in self._closed:
            return True
        did = decision_id(self._product, slot)
        if await self._ledger.meta_get(META_PREFIX + did) is not None:
            self._closed.add(slot)
            return True
        if await self._ledger.get_order(client_order_id(did, "entry")) is not None:  # the order itself is proof enough
            self._closed.add(slot)
            return True
        return False

    async def _close(self, slot: datetime, outcome: str) -> None:
        await self._ledger.meta_set(META_PREFIX + decision_id(self._product, slot), outcome)
        self._closed.add(slot)

    async def tick(self) -> str | None:
        """One look at the clock. Returns the outcome when a slot was closed now, else ``None``."""
        now = self._clock()
        await self._close_missed(now)
        slot = due_slot(now, self._cfg)
        if slot is None or await self._is_closed(slot):
            return None
        try:
            facts = await self._gather(slot, now)
            result = self._planner.plan(facts)
        except Exception as exc:  # the facts could not be read: that is a transient skip, not a crash
            log.error("probe_gather_failed", error=f"{type(exc).__name__}: {exc}")
            result = ProbeSkip("gather_failed", f"{type(exc).__name__}: {exc}")
        if isinstance(result, ProbeSkip):
            if result.transient or result.reason == "gather_failed":
                if self._last_reason.get(slot) != result.reason:
                    self._last_reason[slot] = result.reason
                    self._event(
                        "probe_skip_pending",
                        {"slot": slot.isoformat(), "reason": result.reason, "detail": result.detail},
                    )
                return None
            return await self._skip(slot, result)
        await self._close(slot, "entered")
        self._event(
            "probe_entry",
            {"slot": slot.isoformat(), "decision_id": result.decision_id, "limit_price": str(result.entry.limit_price),
             "base_size": str(result.base_size), "tp": str(result.bracket.take_profit),
             "sl": str(result.bracket.stop_loss), "end_after_s": result.entry.time_in_force_s},
        )  # fmt: skip
        self._start(result)
        return "entered"

    async def _skip(self, slot: datetime, skip: ProbeSkip) -> str:
        outcome = f"skip:{skip.reason}"
        await self._close(slot, outcome)
        self._event("probe_skip", {"slot": slot.isoformat(), "reason": skip.reason, "detail": skip.detail})
        log.info("probe_skip", slot=slot.isoformat(), reason=skip.reason, detail=skip.detail)
        if skip.ends_the_probe:
            self._finish(skip.reason, skip.detail)
        return outcome

    async def _close_missed(self, now: datetime) -> None:
        """Slots whose window ended with no decision: transient skips that never cleared, or a process that was down."""
        window = timedelta(seconds=limits.SLOT_WINDOW_S)
        for slot in slot_times(now.astimezone(UTC).date(), self._cfg):
            if slot + window <= now and not await self._is_closed(slot):
                last = self._last_reason.get(slot)
                outcome = f"skip:slot_expired:{last}" if last else "missed"
                await self._close(slot, outcome)
                self._event("probe_skip", {"slot": slot.isoformat(), "reason": "slot_expired" if last else "missed",
                                           "detail": last or "the process was not running in the window"})  # fmt: skip

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as exc:  # a failed tick must not end the scheduler: the slot window is short
                log.error("probe_scheduler_tick_failed", error=f"{type(exc).__name__}: {exc}")
            await self._sleep(self._poll_s)
