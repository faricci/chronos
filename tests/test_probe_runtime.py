"""The runtime of the probe on the fake venue: wiring, a whole cycle, the protections, the hand switches, a restart.

Everything runs through ``build_runtime`` (the same wiring the real run uses) with the exchange replaced by ``Venue``.
No network, no key, no WebSocket (``Deps.streams = False``): the polling path of the gateway and the reconciler book the
fills, exactly as they do when the stream is silent.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from modules.config import AppConfig, CoinbaseConfig, ExecProbeConfig, PathsConfig
from modules.execution.exchange import OrderRejected, OutcomeUnknown
from modules.execution.ids import client_order_id
from modules.probe import limits
from modules.probe.control import EventFile
from modules.probe.planner import decision_id
from modules.probe.runner import Deps, ProbeRuntime, build_runtime
from tests.probe_fakes import Clock, P, Venue

D = Decimal
SLOT9 = datetime(2026, 10, 28, 9, 0, tzinfo=Clock().now.tzinfo)
SLOT11 = SLOT9.replace(hour=11)
ENTRY9 = client_order_id(decision_id(P, SLOT9), "entry")
BRACKET9 = client_order_id(decision_id(P, SLOT9), "bracket")
LIVE_ENV = {"CHRONOS_LIVE_CONFIRM": "I_ACCEPT_REAL_ORDERS", "CHRONOS_EXEC_PROBE_CONFIRM": "BTC-EUR:3.00"}


def make_cfg(tmp_path: Path, **probe: Any) -> AppConfig:
    return AppConfig(
        paths=PathsConfig(state_dir=str(tmp_path / "state")),
        coinbase=CoinbaseConfig(_env_file=None),  # type: ignore[call-arg]
        exec_probe=ExecProbeConfig(
            enabled=True, db_path=str(tmp_path / "state" / "probe.db"), out_dir=str(tmp_path / "out"), **probe
        ),
    )


def make_deps(venue: Venue, clock: Clock, tmp_path: Path, **kw: Any) -> Deps:
    return Deps(
        read_exchange=lambda c: venue,  # type: ignore[arg-type,return-value]
        write_exchange=lambda c: venue,  # type: ignore[arg-type,return-value]
        env=dict(LIVE_ENV), clock=clock, sleep=clock.sleep, repo_root=tmp_path / "repo", configure_logs=False,
        install_signals=False, streams=False, **kw,
    )  # fmt: skip


@dataclass
class Env:
    rt: ProbeRuntime
    venue: Venue
    clock: Clock
    cfg: AppConfig
    deps: Deps
    tmp_path: Path

    def events(self) -> list[dict[str, Any]]:
        return EventFile(self.rt.out_dir, self.clock).read()

    def kinds(self) -> list[str]:
        return [e["kind"] for e in self.events()]

    async def settle(self, timeout_s: float = 10.0) -> None:
        """Wait until every background task of the runtime (an entry, an exit) has ended."""
        waited = 0.0
        while self.rt._tasks_active() and waited < timeout_s:  # noqa: SLF001
            await asyncio.sleep(0.01)
            waited += 0.01
        assert not self.rt._tasks_active(), "a background task did not end"  # noqa: SLF001

    async def at(self, hour: int, minute: int = 0, second: int = 3) -> None:
        self.clock.now = self.clock.now.replace(hour=hour, minute=minute, second=second)

    async def enter_at_nine(self, size: str = "1", fill_after_s: float = 60) -> None:
        """Serve the 09:00 slot with an entry that fills in full after ``fill_after_s`` seconds."""
        self.venue.plan_fills(ENTRY9, (fill_after_s, size, "80000.00"))
        await self.at(9)
        assert await self.rt.scheduler.tick() == "entered"
        await self.settle()


async def build(tmp_path: Path, *, venue_kw: dict[str, Any] | None = None, **probe: Any) -> Env:
    clock = Clock()
    venue = Venue(clock, **(venue_kw or {}))
    cfg = make_cfg(tmp_path, **probe)
    deps = make_deps(venue, clock, tmp_path)
    rt = await build_runtime(cfg, deps, CoinbaseConfig(_env_file=None), [], "gtd")  # type: ignore[call-arg]
    return Env(rt, venue, clock, cfg, deps, tmp_path)


async def close(env: Env) -> None:
    for task in list(env.rt._tasks):  # noqa: SLF001
        task.cancel()
    await asyncio.gather(*list(env.rt._tasks), return_exceptions=True)  # noqa: SLF001
    env.rt.raw_log.close()
    await env.rt.ledger.close()


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    e = await build(tmp_path)
    yield e
    await close(e)


# -------------------------------------------------------------------------------------------- wiring


async def test_the_runtime_is_wired_as_the_plan_says(env: Env) -> None:
    rt = env.rt
    assert rt.ledger.mode == "live" and str(rt.cfg.exec_probe.db_path).endswith("probe.db")
    assert rt.cfg.exec_probe.db_path != rt.cfg.paths.live_db  # the probe never touches the live ledger
    assert (rt.balances.book, rt.balances.source) == (limits.BOOK, "exchange")
    assert rt.balances.snapshot_age_s() is not None  # the startup reconciliation applied a snapshot
    assert rt.registry.product_ids == {P}
    assert rt.account._max_losses == 6 and rt.account._max_daily_loss == D("0.015")  # noqa: SLF001  neutralised / 0.30 over C
    assert rt.pcfg.confirm_value == "BTC-EUR:3.00"
    assert rt.atr.value() is not None and rt.fees.current.assumed is False
    assert rt.entries_blocked(env.clock()) is None and not rt.manual_stop()


async def test_the_gateway_only_ever_sees_the_probe_book(env: Env) -> None:
    from modules.execution.gateway import entry_spec
    from tests.test_probe_planner import intent as planner_intent

    wrong = planner_intent().model_copy(update={"book": "champion"})
    with pytest.raises(ValueError, match="book"):
        await env.rt.gateway.submit_entry_with_bracket(wrong)
    assert entry_spec(planner_intent()).book == limits.BOOK


# ------------------------------------------------------------------------------------- a whole cycle


async def test_a_cycle_to_take_profit(env: Env) -> None:
    await env.enter_at_nine()
    pos = env.rt.ledger.view(limits.BOOK, P)
    assert pos.is_long and pos.bracket_order_id == BRACKET9 and pos.base_qty == D("0.00003693")
    entry, bracket = env.venue.creates()
    assert (entry.post_only, entry.kind, entry.limit_price) == (True, "limit", D("80000.00"))
    assert entry.end_time == SLOT9 + timedelta(seconds=3 + 1800)  # GTD: the exchange expires it, 30 min after the send
    assert (bracket.kind, bracket.limit_price, bracket.stop_trigger_price) == ("bracket", D("80480.00"), D("79520.00"))
    assert bracket.base_size == pos.base_qty
    assert "probe_entry" in env.kinds() and "probe_entry_result" in env.kinds()
    assert env.rt.balances.reservation(BRACKET9) is not None  # the base is set aside for the bracket

    env.venue.set_market("80500.00")  # the take profit fills
    await env.rt.reconciler.reconcile_once()
    assert not env.rt.ledger.view(limits.BOOK, P).is_long
    (trade,) = await env.rt.ledger.closed_trades(limits.BOOK)
    assert trade.exit_reason == "TP" and trade.net_quote > 0
    assert env.rt.streaks.sl_streak == 0 and env.rt.streaks.watchdog_count == 0
    await env.settle()
    assert "trade_closed" in env.kinds()


async def test_the_slot_is_used_once_and_the_second_slot_follows_the_position(env: Env) -> None:
    await env.enter_at_nine()
    env.venue.set_market("80000.00")
    await env.at(9, 4, 59)
    assert await env.rt.scheduler.tick() is None  # same slot, already closed
    await env.at(11)
    await env.rt.atr.refresh()
    assert await env.rt.scheduler.tick() == "skip:position_open"  # the 09:00 position is still open
    assert len(env.venue.creates("BUY")) == 1


async def test_an_entry_that_never_fills_is_expired_by_the_exchange_and_the_next_slot_can_go(tmp_path: Path) -> None:
    e = await build(tmp_path)
    try:
        await e.at(9)
        assert await e.rt.scheduler.tick() == "entered"
        await e.settle(timeout_s=30)
        (result,) = [x for x in e.events() if x["kind"] == "probe_entry_result"]
        assert (result["status"], result["reason"]) == ("no_fill", "expired")
        assert [x for x in e.venue.log if x[0] == "cancel"] == []  # the exchange ended it, the bot cancelled nothing
        assert (await e.rt.ledger.get_order(ENTRY9)).status == "EXPIRED"  # type: ignore[union-attr]
        assert not e.rt.ledger.view(limits.BOOK, P).is_long and e.rt.balances.reservation(ENTRY9) is None
        e.venue.plan_fills(client_order_id(decision_id(P, SLOT11), "entry"), (30, "1", "80000.00"))
        e.clock.now = SLOT11.replace(second=3)
        await e.rt.atr.refresh()
        e.rt.feed and await e.rt.feed.refresh_once()
        assert await e.rt.scheduler.tick() == "entered"  # the 11:00 slot trades after the 09:00 one expired
    finally:
        await e.settle()
        await close(e)


async def test_a_partial_fill_then_expiry_still_gets_its_bracket(env: Env) -> None:
    env.venue.plan_fills(ENTRY9, (60, "0.00001300", "80000.00"))  # 1.04 EUR: above the protectable notional
    await env.at(9)
    await env.rt.scheduler.tick()
    await env.settle(timeout_s=30)
    pos = env.rt.ledger.view(limits.BOOK, P)
    assert pos.is_long and pos.base_qty == D("0.00001300") and pos.bracket_order_id is not None
    (result,) = [x for x in env.events() if x["kind"] == "probe_entry_result"]
    assert (result["status"], result["bracket"]) == ("filled", "placed")


# ------------------------------------------------------------------------------------- protections


async def test_a_bracket_the_exchange_refuses_ends_the_probe_with_an_exit(env: Env) -> None:
    env.venue.plan_fills(ENTRY9, (60, "1", "80000.00"))
    env.venue.errors = [None, OrderRejected("INVALID_SIZE", "bracket refused")]  # the entry goes, the bracket does not
    await env.at(9)
    await env.rt.scheduler.tick()
    await env.settle(timeout_s=30)
    assert not env.rt.ledger.view(limits.BOOK, P).is_long  # sold at once, at market
    (trade,) = await env.rt.ledger.closed_trades(limits.BOOK)
    assert trade.exit_reason == "WATCHDOG"
    finished = env.rt.marker.read()
    assert finished is not None and finished.reason == "watchdog_exit" and "bracket" in finished.detail
    assert env.rt.streaks.finished and env.rt.streaks.blocked_reason(env.clock()) == "finished:watchdog_exit"
    sells = env.venue.creates("SELL")
    assert [s.kind for s in sells] == ["bracket", "market"]  # the refused bracket, then market: allowed in this case
    assert [o.spec.kind for o in env.venue.orders.values() if o.spec.side == "SELL"] == ["market"]
    # the end is persistent: the planner refuses every later slot
    await env.at(11)
    await env.rt.atr.refresh()
    assert await env.rt.scheduler.tick() == "skip:entry_blocked"


async def test_the_protection_monitor_sells_a_position_whose_bracket_never_opens(env: Env) -> None:
    env.venue.plan_fills(ENTRY9, (60, "1", "80000.00"))
    env.venue.errors = [None, OutcomeUnknown("create_order", BRACKET9, "502")]  # the bracket outcome is unknown
    await env.at(9)
    await env.rt.scheduler.tick()
    await env.settle(timeout_s=30)
    pos = env.rt.ledger.view(limits.BOOK, P)
    assert (
        pos.is_long and pos.bracket_order_id is None
    )  # LONG, no bracket recorded, the base reserved by the unknown order
    env.clock.advance(31)
    await env.rt.protections.check()
    assert env.rt.protections.exits_requested == 1 and env.rt.marker.read() is not None


async def test_a_stop_that_does_not_execute_is_sold_by_the_monitor(env: Env) -> None:
    await env.enter_at_nine()
    env.venue.stop_stuck = True
    env.venue.set_market("79500.00")  # under the stop, which triggered and was not executed
    await env.rt.protections.check()
    env.clock.advance(61)
    await env.rt.protections.check()
    assert env.rt.protections.exits_requested == 1
    await env.settle()
    (trade,) = await env.rt.ledger.closed_trades(limits.BOOK)
    assert trade.exit_reason == "WATCHDOG" and env.rt.streaks.finished


# ------------------------------------------------------------------------------- hand switches


async def test_stop_cancels_the_pending_entry_with_confirmation_and_blocks_the_slots(env: Env) -> None:
    await env.at(9)
    assert await env.rt.scheduler.tick() == "entered"
    for _ in range(100):  # let the entry reach the exchange
        if env.rt.gateway.has_pending_entry(P) and env.venue.orders:
            break
        await asyncio.sleep(0.01)
    env.rt.switches.request_stop()
    await env.rt.switches_once()
    await env.settle(timeout_s=30)
    assert [x for x in env.venue.log if x[0] == "cancel"]
    assert (await env.rt.ledger.get_order(ENTRY9)).status == "CANCELLED"  # type: ignore[union-attr]
    assert env.rt.balances.reservation(ENTRY9) is None and "probe_pending_entry_cancelled" in env.kinds()
    await env.at(11)
    await env.rt.atr.refresh()
    assert await env.rt.scheduler.tick() == "skip:manual_stop"


async def test_stop_leaves_the_bracket_and_the_position_alone(env: Env) -> None:
    await env.enter_at_nine()
    env.rt.switches.request_stop()
    await env.rt.switches_once()
    await env.settle()
    pos = env.rt.ledger.view(limits.BOOK, P)
    assert pos.is_long and pos.bracket_order_id == BRACKET9
    assert [x for x in env.venue.log if x[0] == "cancel"] == []


async def test_flatten_sells_the_position_as_manual(env: Env) -> None:
    await env.enter_at_nine()
    env.rt.switches.request_flatten()
    await env.rt.switches_once()
    await env.settle(timeout_s=30)
    assert not env.rt.ledger.view(limits.BOOK, P).is_long
    (trade,) = await env.rt.ledger.closed_trades(limits.BOOK)
    assert trade.exit_reason == "MANUAL"
    assert env.rt.streaks.sl_streak == 0 and not env.rt.streaks.finished  # a manual exit is neutral
    assert (await env.rt.ledger.get_order(BRACKET9)).status == "CANCELLED"  # type: ignore[union-attr]
    exits = [o for o in await env.rt.ledger.book_orders(limits.BOOK) if o.leg.startswith("exit")]
    assert exits and all(o.intent_id and o.intent_id.startswith("probe-exit:MANUAL:") for o in exits)


# ---------------------------------------------------------------- the failed horizon exit and its bracket


async def test_a_failed_exit_at_the_horizon_puts_the_bracket_back_at_once(env: Env) -> None:
    await env.enter_at_nine()
    env.venue.auto_sell = False  # the exit orders will not fill
    pos = env.rt.ledger.view(limits.BOOK, P)
    env.clock.now = pos.opened_at + timedelta(minutes=481, seconds=30)  # type: ignore[operator]
    await env.rt.watchdog.check_horizons()
    await env.rt.watchdog.drain()
    after = env.rt.ledger.view(limits.BOOK, P)
    assert after.is_long and after.bracket_order_id is not None and after.bracket_order_id != BRACKET9
    brackets = [s for s in env.venue.creates("SELL") if s.kind == "bracket"]
    assert len(brackets) == 2 and brackets[1].base_size == after.base_qty  # the protection came back
    assert "probe_bracket_restored" in env.kinds()
    assert (await env.rt.ledger.get_order(BRACKET9)).status == "CANCELLED"  # type: ignore[union-attr]


async def test_an_exit_that_works_does_not_restore_anything(env: Env) -> None:
    await env.enter_at_nine()
    pos = env.rt.ledger.view(limits.BOOK, P)
    env.clock.now = pos.opened_at + timedelta(minutes=481, seconds=30)  # type: ignore[operator]
    await env.rt.watchdog.check_horizons()
    await env.rt.watchdog.drain()
    assert not env.rt.ledger.view(limits.BOOK, P).is_long
    (trade,) = await env.rt.ledger.closed_trades(limits.BOOK)
    assert trade.exit_reason == "HORIZON"
    assert len([s for s in env.venue.creates("SELL") if s.kind == "bracket"]) == 1


# ------------------------------------------------------------------------------------- the guards


async def test_a_daily_loss_blocks_the_entries_until_midnight(env: Env) -> None:
    await env.enter_at_nine()
    env.venue.set_market("79520.00")  # the stop executes: a loss, but a small one
    await env.rt.reconciler.reconcile_once()
    await env.rt.equity_once()
    assert env.rt.entries_blocked(env.clock()) is None  # about 0.05 EUR: inside the day's 0.30
    # a loss of 0.40 EUR on a later position (booked straight in the ledger) trips the daily guard
    from modules.ledger.positions import FillRecord

    t = env.clock()
    await env.rt.ledger.apply_entry_fill_recorded(
        FillRecord("x-b", limits.BOOK, "cx-b", P, "BUY", D("80000"), D("0.00005"), D(0), "MAKER", t), "probe:x", None
    )
    await env.rt.ledger.apply_exit_fill_recorded(
        FillRecord("x-s", limits.BOOK, "cx-s", P, "SELL", D("72000"), D("0.00005"), D(0), "TAKER", t)
    )
    await env.rt.equity_once()
    assert env.rt.entries_blocked(env.clock()) == "daily_loss"
    env.clock.now = env.clock.now.replace(day=29, hour=0, minute=1)
    assert env.rt.entries_blocked(env.clock()) is None and env.rt.marker.read() is None  # a new day, not the end


async def test_an_anomaly_loss_ends_the_probe(env: Env) -> None:
    from modules.ledger.positions import FillRecord

    t = env.clock()
    await env.rt.ledger.apply_entry_fill_recorded(
        FillRecord("a-b", limits.BOOK, "ca-b", P, "BUY", D("80000"), D("0.00005"), D(0), "MAKER", t), "probe:a", None
    )
    await env.rt.ledger.apply_exit_fill_recorded(
        FillRecord("a-s", limits.BOOK, "ca-s", P, "SELL", D("68000"), D("0.00005"), D(0), "TAKER", t)
    )
    await env.rt.equity_once()  # a loss of 0.60 EUR > 0.50
    finished = env.rt.marker.read()
    assert finished is not None and finished.reason == "anomaly_loss"
    assert env.rt.entries_blocked(env.clock()) == "finished:anomaly_loss"


async def test_two_stops_in_a_row_pause_until_the_next_day(env: Env) -> None:
    env.rt.streaks.observe_exit("SL", env.clock())
    env.rt.streaks.observe_exit("SL", env.clock())
    assert env.rt.entries_blocked(env.clock()) == "sl_streak_pause"
    await env.at(11)
    await env.rt.atr.refresh()
    assert await env.rt.scheduler.tick() == "skip:entry_blocked"
    assert not env.rt.marker.read()  # a pause is not the end of the probe


async def test_a_stale_balance_blocks_the_slot_until_a_snapshot_arrives(env: Env) -> None:
    env.rt.balances.snapshot_at = env.clock() - timedelta(minutes=10)  # the feed is not running: the snapshot is old
    env.clock.now = env.clock.now.replace(hour=9, minute=0, second=3)
    assert await env.rt.scheduler.tick() is None and "probe_skip_pending" in env.kinds()
    await env.rt.feed.refresh_once()
    env.clock.advance(5)
    assert await env.rt.scheduler.tick() == "entered"


async def test_outcome_unknown_at_the_entry_is_never_resent_and_the_reconciler_closes_it(env: Env) -> None:
    env.venue.errors = [OutcomeUnknown("create_order", ENTRY9, "read timeout")]
    await env.at(9)
    await env.rt.scheduler.tick()
    await env.settle()
    assert len(env.venue.creates("BUY")) == 1
    assert (await env.rt.ledger.get_order(ENTRY9)).status == "UNKNOWN"  # type: ignore[union-attr]
    assert await env.rt.scheduler.tick() is None and len(env.venue.creates("BUY")) == 1  # the slot is closed
    assert env.rt.balances.reservation(ENTRY9) is not None  # the money stays set aside
    await env.rt.reconciler.reconcile_once()
    await env.rt.reconciler.reconcile_once()  # two cycles without a trace of the order on the exchange
    assert env.rt.balances.reservation(ENTRY9) is None
    assert len(env.venue.creates("BUY")) == 1


# --------------------------------------------------------------------------------------- the restart


async def test_a_restart_with_an_open_position_resumes_it_and_never_repeats_the_slot(tmp_path: Path) -> None:
    first = await build(tmp_path)
    await first.enter_at_nine()
    venue, clock = first.venue, first.clock
    await close(first)
    cfg = make_cfg(tmp_path)
    deps = make_deps(venue, clock, tmp_path)
    clock.now = SLOT9.replace(minute=2)
    rt = await build_runtime(cfg, deps, CoinbaseConfig(_env_file=None), [], "gtd")  # type: ignore[call-arg]
    try:
        pos = rt.ledger.view(limits.BOOK, P)
        assert pos.is_long and pos.bracket_order_id == BRACKET9 and rt.balances.reservation(BRACKET9) is not None
        assert await rt.scheduler.tick() is None  # the 09:00 slot is already served
        assert (await rt.limiter.counts(clock())).entries_today == 1
        assert len(venue.creates("BUY")) == 1
    finally:
        rt.raw_log.close()
        await rt.ledger.close()


async def test_a_restart_after_the_end_of_the_probe_keeps_it_ended(tmp_path: Path) -> None:
    first = await build(tmp_path)
    first.rt.finish("anomaly_loss", "test")
    venue, clock = first.venue, first.clock
    await close(first)
    rt = await build_runtime(
        make_cfg(tmp_path), make_deps(venue, clock, tmp_path), CoinbaseConfig(_env_file=None), [], "gtd"
    )  # type: ignore[call-arg]
    try:
        assert rt.entries_blocked(clock()) == "finished:anomaly_loss"
    finally:
        rt.raw_log.close()
        await rt.ledger.close()


async def test_the_status_says_why_the_entries_are_blocked(env: Env) -> None:
    status = await env.rt.status()
    assert status["entries_blocked"] is None and status["position"]["side"] == "FLAT"
    assert status["counts"] == {"entries_today": 0, "entries_total": 0, "orders_today": 0}
    env.rt.switches.request_stop()
    assert (await env.rt.status())["entries_blocked"] == "manual_stop"


async def test_saving_the_guards_never_interleaves_with_a_ledger_transaction(env: Env) -> None:
    """The kill switch writes on the ledger's connection. Under ``exclusive`` its transaction and the ledger's own
    (fills, orders, positions) take turns instead of colliding with 'cannot start a transaction within a
    transaction'."""
    from modules.ledger.positions import FillRecord

    t = env.clock()

    async def saves() -> None:
        for _ in range(25):
            async with env.rt.ledger.exclusive():
                await env.rt.kill.save(env.rt.ledger.connection)

    async def fills() -> None:
        for i in range(25):
            rec = FillRecord(f"s-{i}", limits.BOOK, f"cs-{i}", P, "BUY", D("80000"), D("0.00001"), D(0), "MAKER", t)
            await env.rt.ledger.apply_entry_fill_recorded(rec, f"probe:s{i}", None)

    results = await asyncio.gather(saves(), fills(), saves(), return_exceptions=True)
    assert all(r is None for r in results), results


async def test_the_watchdog_still_protects_when_the_atr_is_unknown(env: Env) -> None:
    """ATR1h unknown (no value for two hours): no entry can start, but a position already open must still be covered.
    The standard watchdog then judges the bracket at the plain SL (distance 0) for its grace period."""
    await env.enter_at_nine()
    env.rt.atr._value = None  # noqa: SLF001
    env.rt.atr._fetched_at = None  # noqa: SLF001
    assert env.rt.atr.atr_1h(P) is None
    sl = env.rt.ledger.view(limits.BOOK, P).sl
    assert sl is not None
    env.venue.stop_stuck = True  # the stop does not execute
    price = sl - D("1")
    assert (await env.rt.watchdog.on_ticker(P, price)).kind == "breach_pending"
    env.clock.advance(11)  # grace_s is 10
    verdict = await env.rt.watchdog.on_ticker(P, price)
    assert verdict.kind == "exit_started" and verdict.cause == "WATCHDOG"
    await env.rt.watchdog.drain()
    (trade,) = await env.rt.ledger.closed_trades(limits.BOOK)
    assert trade.exit_reason == "WATCHDOG" and env.rt.streaks.finished
    # and with no ATR the planner refuses a new entry
    await env.at(11)
    assert await env.rt.scheduler.tick() in ("skip:entry_blocked", None)


async def test_the_horizon_exit_carries_its_reason_in_the_decision_id(env: Env) -> None:
    await env.enter_at_nine()
    pos = env.rt.ledger.view(limits.BOOK, P)
    env.clock.now = pos.opened_at + timedelta(minutes=481, seconds=30)  # type: ignore[operator]
    await env.rt.watchdog.check_horizons()
    await env.rt.watchdog.drain()
    exits = [o for o in await env.rt.ledger.book_orders(limits.BOOK) if o.leg.startswith("exit")]
    assert exits and all((o.intent_id or "").startswith("probe-exit:HORIZON:") for o in exits)
