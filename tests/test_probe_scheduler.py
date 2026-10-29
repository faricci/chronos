"""Slots of the probe: one decision per slot, served in a short window, never twice (plan sez. 1.6, 2)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from modules.config import ExecProbeConfig, FallbackFees
from modules.execution.ids import client_order_id
from modules.execution.orders import ProductMeta
from modules.ledger.positions import OrderRecord, PositionLedger
from modules.probe import limits
from modules.probe.budget import BudgetState
from modules.probe.control import FinishMarker, Switches
from modules.probe.planner import PlanFacts, ProbePlanner, decision_id
from modules.probe.scheduler import ProbeScheduler, due_slot, slot_times
from modules.risk.costs import FeeRates
from modules.risk.risk_manager import MarketQuote, OrderIntent

D = Decimal
P = "BTC-EUR"
CFG = ExecProbeConfig()
WED = datetime(2026, 10, 28, 0, 0, tzinfo=UTC)
META = ProductMeta(
    product_id=P, base_currency="BTC", quote_currency="EUR", base_increment=D("0.00000001"),
    quote_increment=D("0.01"), base_min_size=D("0.00000001"), quote_min_size=D("1"),
)  # fmt: skip


def at(h: int, m: int = 0, s: int = 0, day: datetime = WED) -> datetime:
    return day.replace(hour=h, minute=m, second=s)


def good_facts(now: datetime, slot: datetime) -> PlanFacts:
    return PlanFacts(
        now=now, slot=slot, product_id=P, meta=META, fees=FeeRates(maker_bps=25, taker_bps=50, assumed=False),
        fallback=FallbackFees(), manual_stop=False, entry_blocked=None, limit_blocked=None, position_flat=True,
        open_orders=0, balance_age_s=10.0, free_quote=D("5.00"), quote=MarketQuote(D("80000.00"), D("80001.00")),
        quote_age_s=1.0, atr1h=D("200"), last_close=D("80000"),
        budget=BudgetState(D(20), D(0), D(3), D(0), D(0), D("3.00")),
    )  # fmt: skip


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class Rig:
    def __init__(self, ledger: PositionLedger, now: datetime, cfg: ExecProbeConfig = CFG) -> None:
        self.ledger, self.clock = ledger, Clock(now)
        self.started: list[OrderIntent] = []
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.finished: list[tuple[str, str]] = []
        self.change: dict[str, Any] = {}
        self.fail = False
        self.gathers = 0
        self.scheduler = ProbeScheduler(
            cfg=cfg, product_id=P, ledger=ledger, planner=ProbePlanner(cfg), gather=self.gather,
            start_entry=self.started.append, on_event=lambda k, d: self.events.append((k, d)),
            finish=lambda r, d: self.finished.append((r, d)), clock=self.clock,
        )  # fmt: skip

    async def gather(self, slot: datetime, now: datetime) -> PlanFacts:
        self.gathers += 1
        if self.fail:
            raise RuntimeError("REST down")
        return replace(good_facts(now, slot), **self.change)

    def kinds(self) -> list[str]:
        return [k for k, _ in self.events]


@pytest.fixture
async def ledger() -> AsyncIterator[PositionLedger]:
    led = await PositionLedger.open(":memory:", "live")
    yield led
    await led.close()


# ------------------------------------------------------------------------------------------- the clock


def test_slots_exist_only_from_monday_to_friday() -> None:
    assert [s.strftime("%a %H:%M") for s in slot_times(WED.date(), CFG)] == ["Wed 09:00", "Wed 11:00"]
    sat, sun = datetime(2026, 10, 31, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC)
    assert slot_times(sat.date(), CFG) == [] and slot_times(sun.date(), CFG) == []
    mon = datetime(2026, 10, 26, tzinfo=UTC)
    assert (
        len(slot_times(mon.date(), CFG)) == 2 and len(slot_times(datetime(2026, 10, 30, tzinfo=UTC).date(), CFG)) == 2
    )


def test_a_lowered_slot_list_is_followed() -> None:
    assert [s.hour for s in slot_times(WED.date(), ExecProbeConfig(entry_slots_utc=("11:00",)))] == [11]


def test_the_slot_is_served_for_five_minutes_and_not_a_second_longer() -> None:
    assert due_slot(at(8, 59, 59), CFG) is None
    assert due_slot(at(9, 0, 0), CFG) == at(9)
    assert due_slot(at(9, 4, 59), CFG) == at(9)
    assert due_slot(at(9, 5, 0), CFG) is None
    assert due_slot(at(11, 0, 1), CFG) == at(11)
    assert due_slot(at(12, 0), CFG) is None
    assert due_slot(at(9, 1, day=datetime(2026, 10, 31, tzinfo=UTC)), CFG) is None  # Saturday


def test_the_slot_window_is_a_hard_constant() -> None:
    assert limits.SLOT_WINDOW_S == 300


# --------------------------------------------------------------------------------------------- serving


async def test_a_good_slot_starts_one_entry_and_is_closed(ledger: PositionLedger) -> None:
    rig = Rig(ledger, at(9, 0, 3))
    assert await rig.scheduler.tick() == "entered"
    assert [i.decision_id for i in rig.started] == [decision_id(P, at(9))]
    assert rig.kinds() == ["probe_entry"] and rig.events[0][1]["decision_id"] == "probe:BTC-EUR:20261028T0900Z"
    assert await ledger.meta_get("probe.slot.probe:BTC-EUR:20261028T0900Z") == "entered"
    rig.clock.now = at(9, 0, 10)
    assert await rig.scheduler.tick() is None and len(rig.started) == 1 and rig.gathers == 1  # not even asked again


async def test_the_slot_is_not_served_again_after_a_restart(ledger: PositionLedger) -> None:
    first = Rig(ledger, at(9, 0, 3))
    await first.scheduler.tick()
    second = Rig(ledger, at(9, 1, 0))  # a new process, same ledger
    assert await second.scheduler.tick() is None and second.started == [] and second.gathers == 0


async def test_an_order_already_in_the_ledger_closes_the_slot_even_without_a_memory_row(ledger: PositionLedger) -> None:
    did = decision_id(P, at(9))
    now = at(9, 0, 5)
    await ledger.record_order(
        OrderRecord(client_order_id(did, "entry"), limits.BOOK, P, "BUY", "entry", "OPEN", now, now, None, did)
    )
    rig = Rig(ledger, at(9, 1, 0))
    assert await rig.scheduler.tick() is None and rig.started == [] and rig.gathers == 0


async def test_the_second_slot_is_independent_and_follows_the_position_fact(ledger: PositionLedger) -> None:
    rig = Rig(ledger, at(9, 0, 3))
    await rig.scheduler.tick()
    rig.clock.now = at(11, 0, 2)
    rig.change = {"position_flat": False}  # the 09:00 position is still open
    assert await rig.scheduler.tick() == "skip:position_open" and len(rig.started) == 1
    assert "probe_skip" in rig.kinds()
    rig2 = Rig(ledger, at(11, 0, 2))
    assert await rig2.scheduler.tick() is None  # already closed by the first rig's skip
    rig3 = Rig(ledger, at(11, 0, 2), ExecProbeConfig(entry_slots_utc=("09:00", "11:00")))
    assert rig3.started == []


async def test_nothing_happens_outside_the_windows(ledger: PositionLedger) -> None:
    rig = Rig(ledger, at(10, 0, 0))
    assert await rig.scheduler.tick() is None and rig.gathers == 0 and rig.started == []


async def test_a_transient_skip_is_asked_again_and_the_slot_can_still_be_served(ledger: PositionLedger) -> None:
    rig = Rig(ledger, at(9, 0, 2))
    rig.change = {"quote_age_s": 9.0}
    assert await rig.scheduler.tick() is None and rig.started == []
    assert rig.kinds() == ["probe_skip_pending"]
    rig.clock.now = at(9, 0, 8)
    assert await rig.scheduler.tick() is None and rig.kinds() == ["probe_skip_pending"]  # the same reason, logged once
    rig.change = {}  # the quote is fresh now
    rig.clock.now = at(9, 1, 0)
    assert await rig.scheduler.tick() == "entered" and len(rig.started) == 1


async def test_a_transient_skip_that_never_clears_closes_the_slot_when_the_window_ends(ledger: PositionLedger) -> None:
    rig = Rig(ledger, at(9, 0, 2))
    rig.change = {"quote": MarketQuote(D("80000"), D("80100"))}  # 12.5 bps
    await rig.scheduler.tick()
    rig.clock.now = at(9, 5, 1)
    await rig.scheduler.tick()
    assert rig.started == []
    assert await ledger.meta_get("probe.slot.probe:BTC-EUR:20261028T0900Z") == "skip:slot_expired:spread_wide"
    skip = [d for k, d in rig.events if k == "probe_skip"]
    assert skip and skip[0]["reason"] == "slot_expired"


async def test_a_permanent_skip_closes_the_slot_at_once(ledger: PositionLedger) -> None:
    rig = Rig(ledger, at(9, 0, 2))
    rig.change = {"manual_stop": True}
    assert await rig.scheduler.tick() == "skip:manual_stop"
    rig.change = {}
    rig.clock.now = at(9, 0, 30)
    assert await rig.scheduler.tick() is None and rig.started == []  # STOP removed a moment later: the slot is gone


async def test_an_exhausted_budget_ends_the_probe(ledger: PositionLedger) -> None:
    rig = Rig(ledger, at(9, 0, 2))
    rig.change = {"budget": BudgetState(D(20), D("2.5"), D("0.5"), D(0), D(0), D("0.50"))}
    assert await rig.scheduler.tick() == "skip:budget_exhausted"
    assert rig.finished and rig.finished[0][0] == "budget_exhausted"


async def test_a_gather_failure_is_transient_not_a_crash(ledger: PositionLedger) -> None:
    rig = Rig(ledger, at(9, 0, 2))
    rig.fail = True
    assert await rig.scheduler.tick() is None and rig.started == [] and rig.kinds() == ["probe_skip_pending"]
    rig.fail = False
    rig.clock.now = at(9, 0, 9)
    assert await rig.scheduler.tick() == "entered"


async def test_a_process_started_after_the_window_never_serves_the_slot_late(ledger: PositionLedger) -> None:
    rig = Rig(ledger, at(9, 7, 0))
    assert await rig.scheduler.tick() is None and rig.started == [] and rig.gathers == 0
    assert await ledger.meta_get("probe.slot.probe:BTC-EUR:20261028T0900Z") == "missed"
    skip = [d for k, d in rig.events if k == "probe_skip"]
    assert skip and skip[0]["reason"] == "missed"


async def test_the_intent_is_the_planners_not_the_schedulers(ledger: PositionLedger) -> None:
    rig = Rig(ledger, at(9, 0, 2))
    await rig.scheduler.tick()
    (intent,) = rig.started
    assert intent.entry.type == "limit_post_only" and intent.entry.expires_on_exchange and intent.book == limits.BOOK


# -------------------------------------------------------------------------------- hand controls (control.py)


def test_stop_and_flatten_are_files_and_flatten_includes_stop(tmp_path: Path) -> None:
    sw = Switches(tmp_path / "state" / "exec_probe")
    assert not sw.stop_active() and not sw.flatten_active()
    sw.request_stop()
    assert sw.stop_active() and not sw.flatten_active() and sw.stop_path.name == "STOP"
    sw2 = Switches(tmp_path / "other")
    sw2.request_flatten()
    assert sw2.stop_active() and sw2.flatten_active() and sw2.flatten_path.name == "FLATTEN"


def test_the_switches_can_only_be_removed_by_hand() -> None:
    import inspect

    from modules.probe import control

    src = inspect.getsource(control)
    assert "unlink" not in src and "remove(" not in src and "rmtree" not in src


def test_the_finish_marker_keeps_the_first_reason(tmp_path: Path) -> None:
    marker = FinishMarker(tmp_path)
    assert marker.read() is None
    assert marker.write("watchdog_exit", "bracket missing", at(10)) is True
    assert marker.write("anomaly_loss", "later", at(11)) is False
    got = marker.read()
    assert got is not None and (got.reason, got.detail) == ("watchdog_exit", "bracket missing")


def test_an_unreadable_finish_marker_still_means_finished(tmp_path: Path) -> None:
    (tmp_path / "finished.json").write_text("{broken")
    got = FinishMarker(tmp_path).read()
    assert got is not None and got.reason == "unreadable_marker"
