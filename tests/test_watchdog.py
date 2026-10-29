"""Watchdog: stop backup (with and without a bracket), the exit at the horizon, retries and isolation.

The gateway is a fake that records the two calls the watchdog is allowed to make; the ledger is the real one.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
import pytest

from modules.config import WatchdogConfig
from modules.execution.watchdog import Verdict, Watchdog, horizon_deadline
from modules.ledger.positions import PositionLedger
from modules.market.ws_feed import Quote
from modules.risk.risk_manager import ExitIntent
from tests.coverage_fixtures import T0
from tests.execution_fakes import PLAN, B, P, World, bracket_spec, new_world_parts

D = Decimal
SL = PLAN.sl  # 83.57
ATR = D("2.0")
HORIZON_MIN = 60


class FakeGateway:
    """The ExitGateway: records the calls, can fail or block."""

    def __init__(self) -> None:
        self.emergency: list[str] = []
        self.exits: list[ExitIntent] = []
        self.fail: Exception | None = None
        self.gate: asyncio.Event | None = None

    async def emergency_exit(self, product_id: str) -> object:
        self.emergency.append(product_id)
        return await self._finish()

    async def submit_exit(self, intent: ExitIntent) -> object:
        self.exits.append(intent)
        return await self._finish()

    async def _finish(self) -> str:
        if self.gate is not None:
            await self.gate.wait()
        if self.fail is not None:
            raise self.fail
        return "closed"


@pytest.fixture
async def world() -> AsyncIterator[World]:
    ledger = await PositionLedger.open(":memory:", "live")
    w = new_world_parts(ledger)
    yield w
    await ledger.close()


def make(
    w: World,
    gateway: FakeGateway,
    *,
    atr: Decimal | None = ATR,
    cfg: WatchdogConfig | None = None,
    retry_s: float = 5.0,
    horizon_min: int = HORIZON_MIN,
    check_s: float = 5.0,
) -> Watchdog:
    return Watchdog(
        book=B, ledger=w.ledger, gateway=gateway, cfg=cfg or WatchdogConfig(), horizon_min=horizon_min,
        atr_1h=lambda product: atr, clock=w.clock, retry_s=retry_s, horizon_check_s=check_s,
    )  # fmt: skip


async def protect(
    w: World, *, status: str = "OPEN", side: str = "SELL", set_id: str | None = "br1", cid: str = "br1"
) -> None:
    """A bracket order in the ledger and linked to the position."""
    spec = bracket_spec(cid)
    if side == "BUY":
        spec = spec.model_copy(update={"side": "BUY"})
    await w.record(spec, leg="bracket", status=status, exchange_order_id=f"ex-{cid}")
    if set_id is not None:
        await w.ledger.set_bracket(B, P, set_id, PLAN.tp, PLAN.sl)


# -------------------------------------------------------------------- position without a bracket


async def test_watchdog_without_bracket_triggers_at_sl(world: World) -> None:
    await world.open_position()  # the window between the first fill and the bracket: SL is known, no bracket
    gw = FakeGateway()
    wd = make(world, gw)
    above = await wd.on_ticker(P, SL + D("0.01"))
    assert above.kind == "ok" and gw.emergency == []
    hit = await wd.on_ticker(P, SL)  # <= SL, inclusive, no grace period
    await wd.drain()
    assert hit.kind == "exit_started" and hit.cause == "WATCHDOG" and gw.emergency == [P] and gw.exits == []


@pytest.mark.parametrize(("price", "exits"), [("83.58", 0), ("83.57", 1), ("83.56", 1), ("1", 1), ("200", 0)])
async def test_without_a_bracket_the_stop_level_is_the_trigger(world: World, price: str, exits: int) -> None:
    await world.open_position()
    gw = FakeGateway()
    wd = make(world, gw)
    await wd.on_ticker(P, D(price))
    await wd.drain()
    assert len(gw.emergency) == exits


@pytest.mark.parametrize("status", ["CANCELLED", "REJECTED", "FAILED", "EXPIRED"])
async def test_a_bracket_that_is_no_longer_alive_counts_as_no_bracket(world: World, status: str) -> None:
    await world.open_position()
    await protect(world, status=status)
    gw = FakeGateway()
    wd = make(world, gw)
    await wd.on_ticker(P, SL)
    await wd.drain()
    assert gw.emergency == [P]


async def test_a_bracket_id_with_no_order_behind_it_counts_as_no_bracket(world: World) -> None:
    await world.open_position()
    await world.ledger.set_bracket(B, P, "ghost", PLAN.tp, PLAN.sl)
    gw = FakeGateway()
    wd = make(world, gw)
    await wd.on_ticker(P, SL)
    await wd.drain()
    assert gw.emergency == [P]


async def test_an_open_order_that_is_not_a_sell_bracket_of_this_product_does_not_protect(world: World) -> None:
    await world.open_position()
    await protect(world, side="BUY")  # same id, but a BUY
    gw = FakeGateway()
    wd = make(world, gw)
    await wd.on_ticker(P, SL)
    await wd.drain()
    assert gw.emergency == [P]


async def test_a_bracket_is_recognised_by_its_exchange_order_id_too(world: World) -> None:
    await world.open_position()
    await protect(world, set_id=None)
    await world.ledger.set_bracket(B, P, "ex-br1", PLAN.tp, PLAN.sl)
    gw = FakeGateway()
    wd = make(world, gw)
    assert (await wd.on_ticker(P, SL - D("3"))).kind == "breach_pending"  # protected: the grace period applies


async def test_no_bracket_and_no_stop_level_is_exited_whatever_the_price(world: World) -> None:
    await world.open_position(plan=None)
    gw = FakeGateway()
    wd = make(world, gw)
    verdict = await wd.on_ticker(P, D("500"))
    await wd.drain()
    assert verdict.kind == "exit_started" and gw.emergency == [P]


# ----------------------------------------------------------------------- position with a bracket


async def test_with_a_bracket_the_trigger_is_sl_minus_one_atr_and_waits_for_the_grace_period(world: World) -> None:
    await world.open_position()
    await protect(world)
    gw = FakeGateway()
    wd = make(world, gw, atr=D("2.0"))  # trigger = 83.57 - 1.0 x 2.0 = 81.57
    assert (await wd.on_ticker(P, D("82.00"))).kind == "ok"  # below SL, above the trigger: the bracket's job
    assert (await wd.on_ticker(P, D("81.58"))).kind == "ok"
    first = await wd.on_ticker(P, D("81.57"))  # the trigger itself breaches; the timer starts
    assert first.kind == "breach_pending"
    world.clock.advance(9.999)
    assert (await wd.on_ticker(P, D("70"))).kind == "breach_pending"
    world.clock.advance(0.001)
    last = await wd.on_ticker(P, D("70"))  # 10 s after the first breach and still open
    await wd.drain()
    assert last.kind == "exit_started" and last.cause == "WATCHDOG" and gw.emergency == [P]


async def test_a_recovery_above_the_trigger_resets_the_grace_timer(world: World) -> None:
    await world.open_position()
    await protect(world)
    gw = FakeGateway()
    wd = make(world, gw)
    assert (await wd.on_ticker(P, D("80"))).kind == "breach_pending"
    world.clock.advance(8)
    assert (await wd.on_ticker(P, D("82"))).kind == "ok"  # back above SL - ATR
    world.clock.advance(8)
    assert (await wd.on_ticker(P, D("80"))).kind == "breach_pending"  # a new breach starts a new timer
    world.clock.advance(9.9)
    assert (await wd.on_ticker(P, D("80"))).kind == "breach_pending"
    assert gw.emergency == []


async def test_the_atr_multiple_comes_from_the_config(world: World) -> None:
    await world.open_position()
    await protect(world)
    gw = FakeGateway()
    wd = make(world, gw, atr=D("2.0"), cfg=WatchdogConfig(sl_breach_atr_1h=2.5, grace_s=0))
    assert (await wd.on_ticker(P, D("78.58"))).kind == "ok"  # 83.57 - 5.0 = 78.57
    assert (await wd.on_ticker(P, D("78.57"))).kind == "exit_started"


@pytest.mark.parametrize("atr", [None, D("0")])
async def test_without_an_atr_the_trigger_falls_back_to_the_stop_level(world: World, atr: Decimal | None) -> None:
    await world.open_position()
    await protect(world)
    gw = FakeGateway()
    wd = make(world, gw, atr=atr, cfg=WatchdogConfig(grace_s=0))
    assert (await wd.on_ticker(P, SL + D("0.01"))).kind == "ok"
    assert (await wd.on_ticker(P, SL)).kind == "exit_started"


async def test_a_zero_grace_period_exits_on_the_first_breach(world: World) -> None:
    await world.open_position()
    await protect(world)
    gw = FakeGateway()
    wd = make(world, gw, cfg=WatchdogConfig(grace_s=0))
    assert (await wd.on_ticker(P, D("81"))).kind == "exit_started"


async def test_a_bracket_without_a_known_stop_level_cannot_be_judged_and_is_not_sold_blindly(world: World) -> None:
    await world.open_position(plan=None)
    await protect(world, set_id=None)
    await world.ledger.set_bracket(B, P, "br1", None, None)
    gw = FakeGateway()
    wd = make(world, gw)
    verdict = await wd.on_ticker(P, D("1"))
    await wd.drain()
    assert verdict.kind == "blind" and gw.emergency == [] and gw.exits == []


# ----------------------------------------------------------------------------------------- horizon


def test_the_horizon_deadline_is_the_close_of_the_bar_h_minutes_after_the_fill_bar() -> None:
    opened = datetime(2026, 10, 17, 12, 0, 30, 500_000, tzinfo=UTC)
    assert horizon_deadline(opened, 60) == datetime(2026, 10, 17, 13, 1, tzinfo=UTC)  # 12:00 bar + 60 bars + 1 min
    assert horizon_deadline(datetime(2026, 10, 17, 12, 0, tzinfo=UTC), 1) == datetime(2026, 10, 17, 12, 2, tzinfo=UTC)
    assert horizon_deadline(opened.astimezone(timezone_plus_two()), 60) == datetime(2026, 10, 17, 13, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="timezone"):
        horizon_deadline(datetime(2026, 10, 17, 12, 0), 60)
    with pytest.raises(ValueError, match="at least 1"):
        horizon_deadline(opened, 0)


def timezone_plus_two() -> object:
    from datetime import timezone

    return timezone(timedelta(hours=2))


def test_horizon_deadline_is_the_close_of_the_vertical_bar_of_the_label_and_of_the_backtest() -> None:
    """The label exits at close[fill + H]; the runtime must exit when that bar closes (decision O2.4)."""
    from modules.dryrun.fill_model import BarFillModel
    from modules.dryrun.sim_exchange import MarketBars, SimulatedExchange
    from modules.risk.costs import CostParams, FeeRates
    from modules.risk.label_config import LabelConfig
    from training.labeling import VERTICAL, triple_barrier

    n, h_min, decision_bar = 200, 10, 20
    ts_open = np.datetime64("2026-10-17T00:00", "ns") + np.arange(n) * np.timedelta64(1, "m")
    ts_close = ts_open + np.timedelta64(1, "m")
    flat = np.full(n, 100.0)
    fees, costs = FeeRates(maker_bps=25, taker_bps=50, assumed=False), CostParams(slippage_buffer_bps=5,
                                                                                  stop_extra_slip_bps=10)  # fmt: skip
    cfg = LabelConfig(atr_tf="1h", k_tp=4.0, k_sl=2.0, entry_mode="taker", horizon_min=h_min, entry_timeout_min=30,
                      entry_first_bar=3)  # fmt: skip
    res = triple_barrier(flat, flat, flat, flat, np.full(n, 1.0), np.array([decision_bar]), cfg, fees, costs, 0.0)
    fill_idx, exit_idx = int(res.fill_idx[0]), int(res.exit_idx[0])
    assert res.outcome[0] == VERTICAL and exit_idx == fill_idx + h_min

    def to_dt(x: np.datetime64) -> datetime:
        return datetime.fromisoformat(str(x.astype("datetime64[s]"))).replace(tzinfo=UTC)

    assert horizon_deadline(to_dt(ts_open[fill_idx]), h_min) == to_dt(ts_close[exit_idx])
    # the same bar in the bar-level venue of the backtest
    bars = MarketBars(P, ts_close, flat, flat, flat, flat, np.full(n, 1e6))
    venue = SimulatedExchange(BarFillModel(), fees, {P: D("0.01")}, 3, 30, h_min)
    out = venue.bracket(bars, fill_idx, D("200"), D("1"), 0.0)
    assert out is not None and out.reason == "HORIZON" and out.bar == exit_idx
    # and a fill inside the minute (12:00:41) belongs to the same bar as 12:00:00
    inside = to_dt(ts_open[fill_idx]) + timedelta(seconds=41, microseconds=123)
    assert horizon_deadline(inside, h_min) == to_dt(ts_close[exit_idx])


async def test_a_position_at_its_horizon_is_sold_through_the_normal_exit_path(world: World) -> None:
    await world.open_position()  # opened_at = T0 (12:00:00)
    await protect(world)
    gw = FakeGateway()
    wd = make(world, gw, horizon_min=HORIZON_MIN)
    due = horizon_deadline(T0, HORIZON_MIN)  # 13:01:00
    world.clock.now = due - timedelta(microseconds=1)
    assert (await wd.on_ticker(P, D("100"))).kind == "ok"
    world.clock.now = due
    verdict = await wd.on_ticker(P, D("100"))
    await wd.drain()
    assert verdict.kind == "exit_started" and verdict.cause == "HORIZON"
    assert gw.emergency == [] and len(gw.exits) == 1
    intent = gw.exits[0]
    assert (intent.reason, intent.book, intent.product_id) == ("HORIZON", B, P)
    assert intent.base_size == D("0.024") and intent.cancel_bracket_order_id == "br1"
    assert intent.decision_id.startswith("horizon:dec1:") and intent.created_at == due


async def test_the_horizon_comes_from_the_label_config_not_from_a_default(world: World) -> None:
    await world.open_position()
    gw = FakeGateway()
    wd = make(world, gw, horizon_min=15 * 1440)
    world.clock.now = T0 + timedelta(days=15)  # 15 days after the fill is still inside the last bar
    assert (await wd.on_ticker(P, D("100"))).kind == "ok"
    world.clock.now = T0 + timedelta(days=15, minutes=1)
    assert (await wd.on_ticker(P, D("100"))).kind == "exit_started"


async def test_the_horizon_check_needs_no_ticker(world: World) -> None:
    await world.open_position()
    gw = FakeGateway()
    wd = make(world, gw)
    assert [v.kind for v in await wd.check_horizons()] == ["ok"]
    world.clock.now = horizon_deadline(T0, HORIZON_MIN)
    assert [v.kind for v in await wd.check_horizons()] == ["exit_started"]
    await wd.drain()
    assert len(gw.exits) == 1


async def test_the_run_loop_closes_a_position_past_its_horizon_even_when_the_market_is_silent(world: World) -> None:
    await world.open_position()
    world.clock.now = T0 + timedelta(days=3)  # the process was down: catch up at the first check
    gw = FakeGateway()
    wd = make(world, gw, check_s=0.01)
    task = asyncio.create_task(wd.run())
    try:
        async with asyncio.timeout(3):
            while not gw.exits:
                await asyncio.sleep(0.005)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert gw.exits[0].reason == "HORIZON"


async def test_a_price_breach_wins_over_the_horizon(world: World) -> None:
    await world.open_position()
    gw = FakeGateway()
    wd = make(world, gw)
    world.clock.now = horizon_deadline(T0, HORIZON_MIN) + timedelta(minutes=5)
    verdict = await wd.on_ticker(P, SL - D("1"))
    await wd.drain()
    assert verdict.cause == "WATCHDOG" and gw.emergency == [P] and gw.exits == []


async def test_a_flat_position_has_nothing_to_watch(world: World) -> None:
    gw = FakeGateway()
    wd = make(world, gw)
    world.clock.now = T0 + timedelta(days=400)
    assert (await wd.on_ticker(P, D("1"))).kind == "flat"
    assert await wd.check_horizons() == []
    assert gw.emergency == [] and gw.exits == []


# ----------------------------------------------------------------- retries, isolation, plumbing


async def test_an_exit_in_flight_is_not_started_twice(world: World) -> None:
    await world.open_position()
    gw = FakeGateway()
    gw.gate = asyncio.Event()
    wd = make(world, gw)
    assert (await wd.on_ticker(P, SL)).kind == "exit_started"
    await asyncio.sleep(0)
    for _ in range(3):
        assert (await wd.on_ticker(P, SL - D("5"))).kind == "exit_in_flight"
    gw.gate.set()
    await wd.drain()
    assert gw.emergency == [P] and wd.exits_started == 1


async def test_a_failed_exit_is_retried_only_after_the_back_off(world: World) -> None:
    await world.open_position()
    gw = FakeGateway()
    gw.fail = RuntimeError("exchange down")
    wd = make(world, gw, retry_s=5.0)
    await wd.on_ticker(P, SL)
    await wd.drain()
    assert (gw.emergency, wd.exits_failed) == ([P], 1)
    world.clock.advance(4.999)
    assert (await wd.on_ticker(P, SL)).kind == "exit_backoff"
    world.clock.advance(0.001)
    gw.fail = None
    assert (await wd.on_ticker(P, SL)).kind == "exit_started"
    await wd.drain()
    assert gw.emergency == [P, P]


async def test_one_slow_exit_does_not_hold_back_the_other_products(world: World) -> None:
    await world.open_position()
    await world.ledger.apply_entry_fill(B, "ETH-EUR", D("0.5"), D("3000"), "dec2", T0, PLAN)
    gw = FakeGateway()
    gw.gate = asyncio.Event()
    wd = make(world, gw)
    await wd.on_ticker(P, SL)
    verdict = await asyncio.wait_for(wd.on_ticker("ETH-EUR", PLAN.sl), timeout=1)  # returns while SOL is still exiting
    assert verdict.kind == "exit_started"
    gw.gate.set()
    await wd.drain()
    assert sorted(gw.emergency) == ["ETH-EUR", P]


async def test_after_the_position_closes_the_state_is_forgotten(world: World) -> None:
    await world.open_position()
    await protect(world)
    gw = FakeGateway()
    wd = make(world, gw)
    await wd.on_ticker(P, D("80"))  # breach pending
    await world.ledger.apply_exit_fill(B, P, D("0.024"))
    assert (await wd.on_ticker(P, D("80"))).kind == "flat"
    await world.ledger.apply_entry_fill(B, P, D("0.024"), D("92.03"), "dec3", world.clock(), PLAN)
    await protect(world, cid="br2", set_id="br2")
    assert (await wd.on_ticker(P, D("80"))).kind == "breach_pending"  # a fresh timer for the new position
    world.clock.advance(9)
    assert (await wd.on_ticker(P, D("80"))).kind == "breach_pending"


async def test_on_quote_converts_the_float_price_through_its_string(world: World) -> None:
    await world.open_position()
    gw = FakeGateway()
    wd = make(world, gw)
    quote = Quote(P, 83.57, 83.5, 83.6, 1.0, 1.0, T0)
    verdict = await wd.on_quote(quote)
    await wd.drain()
    assert verdict.kind == "exit_started" and gw.emergency == [P]  # 83.57 -> Decimal("83.57"), exactly the stop


def test_the_watchdog_validates_its_parameters() -> None:
    gw = FakeGateway()
    common = {"book": B, "ledger": None, "gateway": gw, "cfg": WatchdogConfig(), "atr_1h": lambda p: None}
    with pytest.raises(ValueError, match="horizon_min"):
        Watchdog(**common, horizon_min=0)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="positive"):
        Watchdog(**common, horizon_min=10, retry_s=0)  # type: ignore[arg-type]


def test_a_verdict_is_a_plain_value() -> None:
    assert Verdict("ok", P) == Verdict("ok", P) and Verdict("ok", P).cause is None
