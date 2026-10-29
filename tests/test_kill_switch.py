"""KillSwitch: account guards (per book), system guards (global), persistence (sez. 0.6, 4.1.4, O2.6)."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from modules.config import FallbackFees, KillSwitchConfig, RiskConfig
from modules.execution.balance import BalanceBook
from modules.execution.balance_guard import BalanceGuard, CoverageBreached
from modules.execution.spot import SpotRegistry
from modules.ledger.positions import PositionLedger
from modules.risk.costs import FeeProvider
from modules.risk.kill_switch import AccountGuards, KillSwitch, SystemGuards
from tests.coverage_fixtures import SOL, T0, Clock, Positions, buy, sell

D = Decimal
ROOT = Path(__file__).resolve().parent.parent
CFG = KillSwitchConfig()
RISK = RiskConfig()
B = "champion"


def at(hours: float = 0.0, *, minutes: float = 0.0, seconds: float = 0.0) -> datetime:
    """An instant relative to T0 = 2026-10-17 12:00 UTC."""
    return T0 + timedelta(hours=hours, minutes=minutes, seconds=seconds)


def account(book: str = B, clock: Clock | None = None, cfg: KillSwitchConfig = CFG) -> AccountGuards:
    return AccountGuards(book, cfg, RISK, clock or Clock())


def switch(
    *books: str, cfg: KillSwitchConfig = CFG, clock: Clock | None = None
) -> tuple[KillSwitch, SystemGuards, dict[str, AccountGuards], Clock]:
    clock = clock or Clock()
    system = SystemGuards(cfg, clock)
    accounts = {b: AccountGuards(b, cfg, RISK, clock) for b in (books or (B,))}
    return KillSwitch(system, accounts, clock), system, accounts, clock


# ----------------------------------------------------------------------------------------- daily loss


def test_daily_loss_trips_at_the_threshold() -> None:
    g = account()
    g.observe_equity(at(0), D("100.00"), D(0))
    g.observe_equity(at(1), D("98.01"), D(0))  # -1.99%: below the 2% limit
    assert g.blocked_reason(at(1)) is None
    g.observe_equity(at(2), D("98.00"), D(0))  # exactly -2.00%: trips (<=)
    assert g.blocked_reason(at(2)) == "daily_loss"


def test_daily_loss_does_not_trip_under_the_threshold() -> None:
    g = account()
    g.observe_equity(at(0), D("100.00"), D(0))
    for hours, equity in enumerate(["99.90", "99.00", "98.50", "98.01", "100.50"], start=1):
        g.observe_equity(at(hours), D(equity), D(0))
        assert g.blocked_reason(at(hours)) is None


def test_the_opening_equity_is_the_first_observation_of_the_utc_day() -> None:
    g = account()
    g.observe_equity(at(0), D("100.00"), D(0))  # fixes the opening of 2026-10-17
    g.observe_equity(at(1), D("99.00"), D(0))  # a later, lower value does not move the opening
    g.observe_equity(at(2), D("97.99"), D(0))  # -2.01% of 100, but only -1.02% of 99
    assert g.blocked_reason(at(2)) == "daily_loss"


def test_daily_loss_unblocks_at_midnight_utc_and_the_new_day_opens_fresh() -> None:
    g = account()
    g.observe_equity(at(0), D("100.00"), D(0))
    g.observe_equity(at(3), D("97.00"), D(0))  # 15:00
    midnight = datetime(2026, 10, 18, 0, 0, tzinfo=UTC)
    assert g.blocked_reason(midnight - timedelta(seconds=1)) == "daily_loss"
    assert g.blocked_reason(midnight) is None
    g.observe_equity(midnight + timedelta(minutes=1), D("97.00"), D(0))  # the new day opens at 97.00
    assert g.blocked_reason(midnight + timedelta(minutes=1)) is None
    g.observe_equity(midnight + timedelta(hours=1), D("95.06"), D(0))  # -2.0% of 97 (95.06 <= 95.06)
    assert g.blocked_reason(midnight + timedelta(hours=1)) == "daily_loss"


def test_daily_loss_stays_latched_when_equity_recovers() -> None:
    g = account()
    g.observe_equity(at(0), D("100.00"), D(0))
    g.observe_equity(at(1), D("97.00"), D(0))
    g.observe_equity(at(2), D("105.00"), D(0))
    assert g.blocked_reason(at(2)) == "daily_loss"


def test_an_observation_from_a_past_day_is_ignored() -> None:
    g = account()
    g.observe_equity(datetime(2026, 10, 18, 1, 0, tzinfo=UTC), D("100.00"), D(0))
    g.observe_equity(datetime(2026, 10, 17, 23, 0, tzinfo=UTC), D("1.00"), D("90"))  # late, out of order
    assert g.blocked_reason(datetime(2026, 10, 17, 23, 30, tzinfo=UTC)) is None
    assert g.blocked_reason(datetime(2026, 10, 18, 2, 0, tzinfo=UTC)) is None


def test_naive_datetimes_are_rejected() -> None:
    g = account()
    with pytest.raises(ValueError, match="timezone-aware"):
        g.observe_equity(datetime(2026, 10, 17, 12, 0), D("100"), D(0))


# ----------------------------------------------------------------------------------------- loss streak


def test_four_stops_in_a_row_pause_the_book_for_48_hours() -> None:
    g = account()
    for i in range(3):
        g.observe_trade_closed(at(i), D("-0.5"), "SL")
        assert g.blocked_reason(at(i)) is None
    g.observe_trade_closed(at(3), D("-0.5"), "SL")
    assert g.blocked_reason(at(3)) == "loss_streak"
    assert g.blocked_reason(at(3 + 48) - timedelta(seconds=1)) == "loss_streak"
    assert g.blocked_reason(at(3 + 48)) is None


def test_a_take_profit_in_the_middle_resets_the_streak() -> None:
    g = account()
    for _ in range(3):
        g.observe_trade_closed(at(0), D("-0.5"), "SL")
    g.observe_trade_closed(at(1), D("1.0"), "TP")
    for _ in range(3):
        g.observe_trade_closed(at(2), D("-0.5"), "SL")
    assert g.blocked_reason(at(2)) is None  # 3 + 3 stops, never 4 in a row
    g.observe_trade_closed(at(3), D("-0.5"), "SL")
    assert g.blocked_reason(at(3)) == "loss_streak"


@pytest.mark.parametrize("reason", ["TP", "SELL", "HORIZON"])
def test_other_exits_reset_the_streak(reason: str) -> None:
    g = account()
    for _ in range(3):
        g.observe_trade_closed(at(0), D("-0.5"), "SL")
    g.observe_trade_closed(at(1), D("-0.1"), reason)  # type: ignore[arg-type]
    g.observe_trade_closed(at(2), D("-0.5"), "SL")
    assert g.blocked_reason(at(2)) is None


def test_watchdog_exits_count_as_stops() -> None:
    g = account()
    for reason in ("SL", "WATCHDOG", "SL", "WATCHDOG"):
        g.observe_trade_closed(at(0), D("-0.5"), reason)  # type: ignore[arg-type]
    assert g.blocked_reason(at(0)) == "loss_streak"


def test_after_the_pause_a_whole_new_streak_is_needed() -> None:
    g = account()
    for _ in range(4):
        g.observe_trade_closed(at(0), D("-0.5"), "SL")
    later = at(49)
    for _ in range(3):
        g.observe_trade_closed(later, D("-0.5"), "SL")
    assert g.blocked_reason(later) is None
    g.observe_trade_closed(later, D("-0.5"), "SL")
    assert g.blocked_reason(later) == "loss_streak"


def test_the_threshold_and_the_pause_come_from_the_config() -> None:
    g = account(cfg=KillSwitchConfig(max_consecutive_losses=2, loss_streak_pause_h=1))
    g.observe_trade_closed(at(0), D("-1"), "SL")
    g.observe_trade_closed(at(0), D("-1"), "SL")
    assert g.blocked_reason(at(0, minutes=59)) == "loss_streak"
    assert g.blocked_reason(at(1)) is None


@given(st.lists(st.sampled_from(["TP", "SL", "SELL", "HORIZON", "WATCHDOG"]), max_size=40))
def test_property_the_pause_starts_exactly_when_four_stops_come_in_a_row(exits: list[str]) -> None:
    g = account()
    for i, reason in enumerate(exits):
        g.observe_trade_closed(at(0), D(0), reason)  # type: ignore[arg-type]
        paused = run_started(exits[: i + 1])
        assert (g.blocked_reason(at(0)) == "loss_streak") == paused
        if paused:
            return  # after the first pause the reference model (which does not reset the count) no longer applies


def run_started(exits: list[str]) -> bool:
    """Reference model: did four consecutive stops ever occur?"""
    run = 0
    for reason in exits:
        run = run + 1 if reason in ("SL", "WATCHDOG") else 0
        if run >= 4:
            return True
    return False


# ----------------------------------------------------------------------------------------- exposure


def test_exposure_blocks_while_the_last_observation_is_above_the_limit() -> None:
    g = account()
    g.observe_equity(at(0), D("100"), D("50"))  # exactly 50%: not above
    assert g.blocked_reason(at(0)) is None
    g.observe_equity(at(1), D("100"), D("50.01"))
    assert g.blocked_reason(at(1)) == "exposure"
    g.observe_equity(at(2), D("100"), D("30"))  # not latched: it follows the last observation
    assert g.blocked_reason(at(2)) is None


def test_zero_or_negative_equity_blocks() -> None:
    g = account()
    g.observe_equity(at(0), D("100"), D(0))
    g.observe_equity(at(1), D("0"), D(0))
    assert g.blocked_reason(at(1)) is not None
    # still blocked after the daily-loss latch expired at midnight, until a new observation says otherwise
    assert g.blocked_reason(datetime(2026, 10, 18, 0, 1, tzinfo=UTC)) == "exposure"


# ----------------------------------------------------------------------------------- coverage breach


def test_coverage_breach_blocks_until_a_manual_reset() -> None:
    ks, _, accounts, clock = switch()
    assert not ks.coverage_breach_active(B) and ks.entry_blocked(B) is None
    accounts[B].trip_coverage_breach("reservation_overdraw")
    assert ks.coverage_breach_active(B) and ks.entry_blocked(B) == "coverage_breach"
    clock.advance(30 * 24 * 3600)  # no timeout: only a reset lifts it
    assert ks.coverage_breach_active(B)
    accounts[B].reset_coverage_breach()
    assert not ks.coverage_breach_active(B) and ks.entry_blocked(B) is None


def test_coverage_breach_is_stamped_by_the_injected_clock_and_is_idempotent() -> None:
    clock = Clock()
    g = account(clock=clock)
    g.trip_coverage_breach("first")
    clock.advance(3600)
    g.trip_coverage_breach("second")
    (row,) = g.state_rows()
    assert (row.guard, row.tripped_at, row.until, row.reason) == ("coverage_breach", T0, None, "first")


def test_coverage_breach_blocks_buys() -> None:
    """With a real BalanceGuard: after the breach a BUY is refused, a SELL still works (sez. 4.1.4, 4.1.6)."""
    ks, _, accounts, clock = switch()
    book = BalanceBook(B, "virtual", clock, {"EUR": D("100.00"), "SOL": D("0.024")})
    fees = FallbackFees()
    guard = BalanceGuard(
        book, Positions(SOL_EUR="0.024"), SpotRegistry({"SOL-EUR": SOL}), FeeProvider(fees, clock=clock), fees, ks
    )
    guard.authorize(buy("b0"))  # works before the breach
    accounts[B].trip_coverage_breach("hold_exceeds_reservation")
    with pytest.raises(CoverageBreached) as exc:
        guard.authorize(buy("b1"))
    assert exc.value.reason == "kill_switch"
    ticket = guard.authorize(sell("s1", "0.024", kind="market"))  # the exit is not blocked
    assert ticket.spec.side == "SELL"
    accounts[B].reset_coverage_breach()
    guard.authorize(buy("b2"))


# ----------------------------------------------------------------------------------- scope: book vs system


def test_a_guard_of_one_book_does_not_block_another_book() -> None:
    ks, _, accounts, _ = switch("champion", "challenger")
    accounts["champion"].observe_equity(at(0), D("100"), D(0))
    accounts["champion"].observe_equity(at(1), D("90"), D(0))
    for _ in range(4):
        accounts["challenger"].observe_trade_closed(at(1), D("-1"), "SL")
    assert ks.entry_blocked("champion") == "daily_loss"
    assert ks.entry_blocked("challenger") == "loss_streak"
    accounts["champion"].trip_coverage_breach("x")
    assert ks.coverage_breach_active("champion") and not ks.coverage_breach_active("challenger")


def test_account_guards_of_another_book_stay_clear() -> None:
    ks, _, accounts, _ = switch("champion", "random_entry")
    accounts["champion"].observe_equity(at(0), D("100"), D("80"))
    assert ks.entry_blocked("champion") == "exposure"
    assert ks.entry_blocked("random_entry") is None


def test_an_unknown_book_is_blocked_fail_closed() -> None:
    ks, *_ = switch()
    assert ks.entry_blocked("ghost") == "unknown_book:ghost"
    assert ks.coverage_breach_active("ghost")


# ----------------------------------------------------------------------------------------- system guards


def test_no_stale_trip_with_a_90s_bar_lag() -> None:
    """The normal publication lag (a 1m bar is published S = 120 s after its close, up to ~90 s more when REST is
    slow) must not trip anything: stale_bar_s is 270 s."""
    ks, system, *_ = switch("champion", "challenger")
    close = at(0)
    system.observe_bar_published("SOL-EUR", close, close + timedelta(seconds=90))
    system.observe_ticker("SOL-EUR", close + timedelta(seconds=85))
    now = close + timedelta(seconds=90)
    assert system.blocked_reason(now) is None
    assert system.bar_lag_s("SOL-EUR") == 90.0
    for book in ("champion", "challenger"):
        assert ks.entry_blocked(book) is None
    assert system.blocked_reason(now + timedelta(seconds=60)) is None  # bar 150 s, ticker 65 s old


def test_stale_bar_trips_beyond_180s() -> None:
    """The trip is beyond the configured ``stale_bar_s`` (default 270 s), NOT beyond 180 s."""
    system = SystemGuards(CFG)
    assert CFG.stale_bar_s == 270
    close = at(0)
    system.observe_bar_published("SOL-EUR", close, close + timedelta(seconds=120))
    assert system.blocked_reason(close + timedelta(seconds=180)) is None
    assert system.blocked_reason(close + timedelta(seconds=270)) is None  # exactly the limit: not beyond it
    assert system.blocked_reason(close + timedelta(seconds=271)) == "stale_bar:SOL-EUR"
    # the same code with a 180 s limit trips beyond 180 s: the threshold is the configured one
    tight = SystemGuards(KillSwitchConfig(stale_bar_s=180))
    tight.observe_bar_published("SOL-EUR", close, close + timedelta(seconds=120))
    assert tight.blocked_reason(close + timedelta(seconds=180)) is None
    assert tight.blocked_reason(close + timedelta(seconds=181)) == "stale_bar:SOL-EUR"


def test_a_fresher_bar_clears_the_stale_state() -> None:
    system = SystemGuards(CFG)
    system.observe_bar_published("SOL-EUR", at(0), at(0, seconds=120))
    now = at(0, seconds=400)
    assert system.blocked_reason(now) is not None
    system.observe_bar_published("SOL-EUR", at(0, seconds=360), at(0, seconds=480))
    assert system.blocked_reason(now) is None
    system.observe_bar_published("SOL-EUR", at(0), at(0, seconds=120))  # an older bar never rewinds
    assert system.blocked_reason(now) is None


def test_stale_ticker_trips_beyond_stale_data_s() -> None:
    system = SystemGuards(CFG)
    assert CFG.stale_data_s == 90
    system.observe_ticker("BTC-EUR", at(0))
    assert system.blocked_reason(at(0, seconds=90)) is None
    assert system.blocked_reason(at(0, seconds=91)) == "stale_data:BTC-EUR"
    system.observe_ticker("BTC-EUR", at(0, seconds=100))
    assert system.blocked_reason(at(0, seconds=150)) is None


def test_one_stale_product_blocks_every_book() -> None:
    ks, system, _, clock = switch("champion", "challenger")
    system.observe_bar_published("BTC-EUR", at(0, seconds=-600), at(0))  # BTC feed died
    system.observe_bar_published("SOL-EUR", at(0, seconds=-60), at(0, seconds=60))
    assert ks.entry_blocked("champion") == "stale_bar:BTC-EUR"
    assert ks.entry_blocked("challenger") == "stale_bar:BTC-EUR"
    assert clock() == T0


def test_decider_health_and_drift_block_every_book_until_cleared() -> None:
    ks, system, *_ = switch("champion", "challenger")
    system.set_decider_health(False, reason="fallback ratio 0.35")
    assert ks.entry_blocked("champion") == "decider_health" == ks.entry_blocked("challenger")
    system.set_decider_health(True)
    assert ks.entry_blocked("champion") is None
    system.set_drift(True, reason="psi 0.31")
    assert ks.entry_blocked("champion") == "drift" == ks.entry_blocked("challenger")
    system.set_drift(False)
    assert ks.entry_blocked("challenger") is None


def test_system_guards_come_before_account_guards() -> None:
    ks, system, accounts, _ = switch()
    accounts[B].trip_coverage_breach("x")
    system.set_drift(True)
    assert ks.entry_blocked(B) == "drift"


# ----------------------------------------------------------------------------------------- exits


def test_no_guard_can_block_an_exit() -> None:
    ks, system, accounts, clock = switch("champion", "challenger")
    system.observe_bar_published("SOL-EUR", at(0, seconds=-3600), at(0))
    system.observe_ticker("SOL-EUR", at(0, seconds=-3600))
    system.set_decider_health(False)
    system.set_drift(True)
    for g in accounts.values():
        g.observe_equity(at(0), D("100"), D("100"))
        g.observe_equity(at(1), D("1"), D("100"))
        g.trip_coverage_breach("x")
        for _ in range(4):
            g.observe_trade_closed(at(1), D("-1"), "SL")
    for book in ("champion", "challenger", "ghost"):
        assert ks.entry_blocked(book) is not None
        assert ks.exit_allowed(book) is True


def test_the_balance_guard_still_sells_with_every_guard_tripped() -> None:
    ks, system, accounts, clock = switch()
    system.set_drift(True)
    accounts[B].trip_coverage_breach("x")
    book = BalanceBook(B, "virtual", clock, {"EUR": D("100.00"), "SOL": D("0.024")})
    fees = FallbackFees()
    guard = BalanceGuard(
        book, Positions(SOL_EUR="0.024"), SpotRegistry({"SOL-EUR": SOL}), FeeProvider(fees, clock=clock), fees, ks
    )
    assert guard.authorize(sell("s1", "0.024", kind="market")).spec.client_order_id == "s1"


# ----------------------------------------------------------------------------------------- injected clock


def test_the_clock_is_injected_not_read_from_the_wall() -> None:
    for name in ("modules/risk/kill_switch.py", "modules/dryrun/virtual_account.py"):
        source = (ROOT / name).read_text(encoding="utf-8")
        assert not re.search(r"datetime\.(now|utcnow|today)\(|time\.time\(|date\.today\(", source), name
    ks, system, accounts, clock = switch()
    accounts[B].observe_trade_closed(T0, D(-1), "SL")
    for _ in range(3):
        accounts[B].observe_trade_closed(T0, D(-1), "SL")
    assert ks.entry_blocked(B) == "loss_streak"
    clock.advance(48 * 3600 - 1)
    assert ks.entry_blocked(B) == "loss_streak"
    clock.advance(1)
    assert ks.entry_blocked(B) is None  # time moved only because the injected clock moved


# ----------------------------------------------------------------------------------------- persistence


@pytest.fixture
async def ledger() -> AsyncIterator[PositionLedger]:
    lg = await PositionLedger.open(":memory:", "dry")
    yield lg
    await lg.close()


async def rows(lg: PositionLedger) -> set[tuple[str, str]]:
    async with lg.connection.execute("SELECT book, guard FROM kill_switch_state") as cur:
        return {(r[0], r[1]) for r in await cur.fetchall()}


async def test_save_and_load_round_trip(ledger: PositionLedger) -> None:
    ks, system, accounts, clock = switch("champion", "challenger")
    system.set_drift(True, reason="psi 0.31")
    system.set_decider_health(False, reason="fallback")
    accounts["champion"].trip_coverage_breach("balance_drift x3")
    accounts["champion"].observe_equity(at(0), D("100"), D(0))
    accounts["champion"].observe_equity(at(1), D("97"), D(0))  # daily loss
    for _ in range(4):
        accounts["challenger"].observe_trade_closed(at(1), D("-1"), "SL")  # pause
    accounts["challenger"].observe_equity(at(0), D("200"), D(0))
    await ks.save(ledger.connection)

    ks2, system2, accounts2, _ = switch("champion", "challenger")
    await ks2.load(ledger.connection)
    assert ks2.coverage_breach_active("champion") and not ks2.coverage_breach_active("challenger")
    system2.set_drift(False)
    system2.set_decider_health(True)
    assert ks2.entry_blocked("champion") == "coverage_breach"
    assert ks2.entry_blocked("challenger") == "loss_streak"
    # the global flags came back too
    ks3, *_ = switch("champion", "challenger")
    await ks3.load(ledger.connection)
    assert ks3.entry_blocked("challenger") == "decider_health"
    assert ("*", "drift") in await rows(ledger) and ("*", "decider_health") in await rows(ledger)
    assert clock() == T0


async def test_load_restores_the_opening_equity_and_the_partial_streak(ledger: PositionLedger) -> None:
    ks, _, accounts, _ = switch()
    accounts[B].observe_equity(at(0), D("100"), D(0))
    for _ in range(3):
        accounts[B].observe_trade_closed(at(0), D("-1"), "SL")
    await ks.save(ledger.connection)

    ks2, _, accounts2, _ = switch()
    await ks2.load(ledger.connection)
    accounts2[B].observe_equity(at(2), D("97.50"), D(0))  # vs the restored opening 100, not vs 97.50
    assert ks2.entry_blocked(B) == "daily_loss"
    accounts2[B].observe_trade_closed(at(2), D("-1"), "SL")  # the 4th stop of the restored streak
    assert accounts2[B].blocked_reason(at(2)) == "daily_loss"
    assert any(r.guard == "loss_streak" for r in accounts2[B].state_rows(at(2)))


async def test_expired_trips_are_not_restored(ledger: PositionLedger) -> None:
    ks, _, accounts, clock = switch()
    accounts[B].observe_equity(at(0), D("100"), D(0))
    accounts[B].observe_equity(at(1), D("90"), D(0))
    await ks.save(ledger.connection)
    clock.now = datetime(2026, 10, 18, 0, 0, 1, tzinfo=UTC)
    ks2, *_ = switch(clock=clock)  # same clock, now past midnight UTC
    await ks2.load(ledger.connection)
    assert ks2.entry_blocked(B) is None


async def test_save_replaces_the_rows_of_its_books_and_keeps_the_others(ledger: PositionLedger) -> None:
    await ledger.connection.execute(
        "INSERT INTO kill_switch_state (book, guard, tripped_at, until, reason)"
        " VALUES ('other', 'coverage_breach', NULL, NULL, 'x')"
    )
    ks, _, accounts, _ = switch()
    accounts[B].trip_coverage_breach("x")
    await ks.save(ledger.connection)
    assert (B, "coverage_breach") in await rows(ledger)
    accounts[B].reset_coverage_breach()
    await ks.save(ledger.connection)
    left = await rows(ledger)
    assert (B, "coverage_breach") not in left and ("other", "coverage_breach") in left


async def test_save_waits_for_a_transaction_of_the_ledger(ledger: PositionLedger) -> None:
    ks, _, accounts, _ = switch()
    accounts[B].trip_coverage_breach("x")
    db = ledger.connection
    await db.execute("BEGIN IMMEDIATE")

    async def finish_ledger_transaction() -> None:
        await asyncio.sleep(0.05)
        await db.execute("COMMIT")

    await asyncio.gather(finish_ledger_transaction(), ks.save(db))
    assert (B, "coverage_breach") in await rows(ledger)


async def test_a_failed_save_rolls_back(ledger: PositionLedger) -> None:
    ks, _, accounts, _ = switch()
    accounts[B].trip_coverage_breach("x")
    await ks.save(ledger.connection)
    await ledger.connection.execute("DROP TABLE kill_switch_state")
    await ledger.connection.execute("CREATE TABLE kill_switch_state (book TEXT, guard TEXT)")  # wrong shape
    with pytest.raises(Exception):  # noqa: B017
        await ks.save(ledger.connection)
    assert not ledger.connection.in_transaction
