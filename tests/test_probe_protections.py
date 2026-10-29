"""The blocking protections of the probe (plan sez. 1.4): bracket coverage, the stop that does not execute, MAKER."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from modules.config import ExecProbeConfig
from modules.execution.ids import client_order_id
from modules.ledger.positions import ClosedTrade, EntryPlan, FillRecord, OrderRecord, PositionLedger
from modules.probe import limits
from modules.probe.protections import ProtectionMonitor

D = Decimal
BOOK, P = limits.BOOK, "BTC-EUR"
T0 = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
DECISION = "probe:BTC-EUR:20261028T0900Z"
ENTRY = client_order_id(DECISION, "entry")
BRACKET = client_order_id(DECISION, "bracket")
SL, TP = D("79520.00"), D("80480.00")


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Gateway:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail = False

    async def emergency_exit(self, product_id: str) -> object:
        self.calls.append(product_id)
        if self.fail:
            raise RuntimeError("exit failed")
        return "closed"


class Rig:
    def __init__(self, ledger: PositionLedger, clock: Clock, cfg: ExecProbeConfig | None = None) -> None:
        self.ledger, self.clock = ledger, clock
        self.gateway = Gateway()
        self.bid: D | None = D("80000")
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.finished: list[tuple[str, str]] = []
        self.stops = 0
        self.monitor = ProtectionMonitor(
            cfg=cfg or ExecProbeConfig(), book=BOOK, product_id=P, ledger=ledger, gateway=gateway_of(self),
            bid=self._bid,
            on_event=lambda k, d: self.events.append((k, d)), finish=lambda r, d: self.finished.append((r, d)),
            request_stop=self._stop, clock=clock,
        )  # fmt: skip

    async def _bid(self) -> Decimal | None:
        return self.bid

    def _stop(self) -> None:
        self.stops += 1

    def kinds(self) -> list[str]:
        return [k for k, _ in self.events]


def gateway_of(rig: Rig) -> Gateway:
    return rig.gateway


@pytest.fixture
async def ledger() -> AsyncIterator[PositionLedger]:
    led = await PositionLedger.open(":memory:", "live")
    yield led
    await led.close()


@pytest.fixture
def clock() -> Clock:
    return Clock()


async def open_long(led: PositionLedger, *, entry_status: str = "FILLED", bracket: bool = False) -> None:
    plan = EntryPlan(TP, SL, 0.0, 0.0, D(200))
    await led.apply_entry_fill(BOOK, P, D("0.00003700"), D("80000"), DECISION, T0, plan)
    await led.record_order(OrderRecord(ENTRY, BOOK, P, "BUY", "entry", entry_status, T0, T0, "ex-1", DECISION))
    if bracket:
        await add_bracket(led, T0 + timedelta(seconds=2))


async def add_bracket(led: PositionLedger, at: datetime) -> None:
    await led.record_order(OrderRecord(BRACKET, BOOK, P, "SELL", "bracket", "OPEN", at, at, "ex-2", DECISION))
    await led.set_bracket(BOOK, P, BRACKET, TP, SL)


async def test_nothing_to_protect_on_a_flat_book(ledger: PositionLedger, clock: Clock) -> None:
    rig = Rig(ledger, clock)
    await rig.monitor.check()
    assert rig.events == [] and rig.gateway.calls == []


async def test_a_bracket_open_in_time_is_measured_and_not_alarmed(ledger: PositionLedger, clock: Clock) -> None:
    await open_long(ledger, bracket=True)
    rig = Rig(ledger, clock)
    clock.advance(5)
    await rig.monitor.check()
    await rig.monitor.check()
    assert rig.kinds() == ["bracket_open_latency"] and rig.events[0][1]["seconds"] == 2.0  # once only
    assert rig.gateway.calls == []


async def test_a_slow_bracket_raises_the_alarm_and_a_missing_one_is_sold_after_thirty_seconds(
    ledger: PositionLedger, clock: Clock
) -> None:
    await open_long(ledger)  # entry terminal at T0, no bracket
    rig = Rig(ledger, clock)
    clock.advance(9)
    await rig.monitor.check()
    assert rig.events == []
    clock.advance(2)  # 11 s
    await rig.monitor.check()
    await rig.monitor.check()
    assert rig.kinds() == ["bracket_open_alarm"] and rig.gateway.calls == []  # an alarm, once
    clock.advance(19)  # 30 s: not yet beyond the maximum
    await rig.monitor.check()
    assert rig.gateway.calls == []
    clock.advance(1)  # 31 s
    await rig.monitor.check()
    assert rig.gateway.calls == [P]
    assert rig.finished and rig.finished[0][0] == "watchdog_exit" and "bracket_not_open" in rig.finished[0][1]
    assert "protection_exit" in rig.kinds() and "protection_exit_done" in rig.kinds()


async def test_a_bracket_that_opens_late_but_before_the_maximum_saves_the_position(
    ledger: PositionLedger, clock: Clock
) -> None:
    await open_long(ledger)
    rig = Rig(ledger, clock)
    clock.advance(12)
    await rig.monitor.check()
    await add_bracket(ledger, clock())
    clock.advance(10)
    await rig.monitor.check()
    assert rig.gateway.calls == [] and "bracket_open_latency" in rig.kinds() and rig.finished == []


async def test_an_entry_still_alive_is_left_to_its_own_sequence(ledger: PositionLedger, clock: Clock) -> None:
    await open_long(ledger, entry_status="OPEN")  # a partial fill; the 30 minutes of the entry are not over
    rig = Rig(ledger, clock)
    clock.advance(600)
    await rig.monitor.check()
    assert rig.gateway.calls == [] and rig.events == []


async def test_a_failed_exit_is_retried_after_the_back_off_not_every_second(
    ledger: PositionLedger, clock: Clock
) -> None:
    await open_long(ledger)
    rig = Rig(ledger, clock)
    rig.gateway.fail = True
    clock.advance(31)
    await rig.monitor.check()
    clock.advance(1)
    await rig.monitor.check()
    assert len(rig.gateway.calls) == 1 and "protection_exit_failed" in rig.kinds()
    clock.advance(5)
    await rig.monitor.check()
    assert len(rig.gateway.calls) == 2


async def test_the_bid_under_the_stop_floor_sells_at_once(ledger: PositionLedger, clock: Clock) -> None:
    await open_long(ledger, bracket=True)
    rig = Rig(ledger, clock)
    rig.bid = SL * D("0.96") - D("0.01")
    await rig.monitor.check()
    assert rig.gateway.calls == [P] and "sl_floor_breach" in rig.finished[0][1]


async def test_the_stop_that_does_not_execute_is_sold_after_sixty_seconds(ledger: PositionLedger, clock: Clock) -> None:
    await open_long(ledger, bracket=True)
    rig = Rig(ledger, clock)
    rig.bid = SL - D("10")  # the stop triggered; the position is still open
    await rig.monitor.check()
    assert rig.kinds() == ["bracket_open_latency", "sl_touched"]
    clock.advance(59)
    await rig.monitor.check()
    assert rig.gateway.calls == []
    clock.advance(1)
    await rig.monitor.check()
    assert rig.gateway.calls == [P] and "sl_not_executed" in rig.finished[0][1]


async def test_a_bounce_above_the_stop_resets_the_clock(ledger: PositionLedger, clock: Clock) -> None:
    await open_long(ledger, bracket=True)
    rig = Rig(ledger, clock)
    rig.bid = SL
    await rig.monitor.check()
    clock.advance(50)
    rig.bid = SL + D("5")
    await rig.monitor.check()
    clock.advance(50)
    rig.bid = SL
    await rig.monitor.check()
    clock.advance(59)
    await rig.monitor.check()
    assert rig.gateway.calls == []


async def test_no_bid_no_judgement(ledger: PositionLedger, clock: Clock) -> None:
    await open_long(ledger, bracket=True)
    rig = Rig(ledger, clock)
    rig.bid = None
    clock.advance(500)
    await rig.monitor.check()
    assert rig.gateway.calls == []


async def test_the_state_resets_for_a_new_position(ledger: PositionLedger, clock: Clock) -> None:
    await open_long(ledger, bracket=True)
    rig = Rig(ledger, clock)
    rig.bid = SL
    await rig.monitor.check()
    clock.advance(40)
    await rig.monitor.check()
    await ledger.apply_exit_fill(BOOK, P, D("0.00003700"))  # closed
    await rig.monitor.check()
    assert rig.monitor._below_since is None  # noqa: SLF001


# ------------------------------------------------------------------------------------- after a close


def trade(sl: str | None, exit_avg: str, reason: str = "SL") -> ClosedTrade:
    return ClosedTrade(
        trade_id="x1", book=BOOK, product_id=P, entry_decision_id=DECISION, entry_avg_price=D("80000"),
        entry_size=D("0.000037"), entry_fees=D(0), entry_at=T0, tp=TP, sl=None if sl is None else D(sl), k_tp=0.0,
        k_sl=0.0, exit_reason=reason, exit_avg_price=D(exit_avg), exit_size=D("0.000037"), exit_fees=D(0),
        exit_at=T0 + timedelta(hours=1), gross_quote=D(0), fees_quote=D(0), net_quote=D("-0.02"), net_bps=-60,
        r_multiple=-1.0, held_min=60,
    )  # fmt: skip


@pytest.mark.parametrize(
    ("exit_avg", "alarm", "severe", "stops"),
    [("79519.00", False, False, 0), ("79400.00", True, False, 0), ("79100.00", False, True, 1)],
)
async def test_the_slippage_of_a_stop_is_measured(
    ledger: PositionLedger, clock: Clock, exit_avg: str, alarm: bool, severe: bool, stops: int
) -> None:
    rig = Rig(ledger, clock)
    await rig.monitor.on_closed(trade("79520.00", exit_avg))
    kinds = rig.kinds()
    assert (
        "sl_slippage" in kinds and ("sl_slippage_alarm" in kinds) is alarm and ("sl_slippage_severe" in kinds) is severe
    )
    assert rig.stops == stops  # a severe slip stops the entries until the operator has looked


async def test_other_exits_are_only_recorded(ledger: PositionLedger, clock: Clock) -> None:
    rig = Rig(ledger, clock)
    await rig.monitor.on_closed(trade("79520.00", "80480.00", reason="TP"))
    assert rig.kinds() == ["trade_closed"] and rig.stops == 0 and rig.finished == []


async def test_a_maker_fill_on_a_stop_is_a_conflict(ledger: PositionLedger, clock: Clock) -> None:
    await open_long(ledger, bracket=True)
    rig = Rig(ledger, clock)
    await ledger.apply_exit_fill_recorded(
        FillRecord(
            "t-sl", BOOK, BRACKET, P, "SELL", D("79519"), D("0.000037"), D("0.01"), "MAKER", T0 + timedelta(hours=1)
        )
    )
    await rig.monitor.on_closed(trade("79520.00", "79519.00"))
    assert rig.finished and rig.finished[0][0] == "sl_fill_maker"


async def test_a_taker_fill_on_a_stop_is_the_expected_case(ledger: PositionLedger, clock: Clock) -> None:
    await open_long(ledger, bracket=True)
    rig = Rig(ledger, clock)
    await ledger.apply_exit_fill_recorded(
        FillRecord(
            "t-sl", BOOK, BRACKET, P, "SELL", D("79519"), D("0.000037"), D("0.01"), "TAKER", T0 + timedelta(hours=1)
        )
    )
    await rig.monitor.on_closed(trade("79520.00", "79519.00"))
    assert rig.finished == []
