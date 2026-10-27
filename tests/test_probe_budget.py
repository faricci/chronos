"""ProbeBudget, ProbeLimiter, ProbeStreaks: money and counts come from the ledger of the probe (plan sez. 1.1, 1.3)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from modules.config import ExecProbeConfig
from modules.ledger.positions import ClosedTrade, FillRecord, OrderRecord, PositionLedger
from modules.probe import limits
from modules.probe.budget import ProbeBudget, ProbeLimiter, ProbeStreaks, next_midnight_utc

D = Decimal
BOOK = limits.BOOK
P = "BTC-EUR"
T0 = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)  # a Wednesday
CFG = ExecProbeConfig()


@pytest.fixture
async def ledger() -> AsyncIterator[PositionLedger]:
    led = await PositionLedger.open(":memory:", "live")
    yield led
    await led.close()


_n = 0


async def buy(led: PositionLedger, size: str, price: str, fee: str, at: datetime = T0) -> None:
    global _n
    _n += 1
    rec = FillRecord(f"t{_n}", BOOK, f"c{_n}", P, "BUY", D(price), D(size), D(fee), "MAKER", at)
    await led.apply_entry_fill_recorded(rec, f"probe:{_n}", None)


async def sell(led: PositionLedger, size: str, price: str, fee: str, at: datetime) -> None:
    global _n
    _n += 1
    rec = FillRecord(f"t{_n}", BOOK, f"c{_n}", P, "SELL", D(price), D(size), D(fee), "TAKER", at)
    await led.apply_exit_fill_recorded(rec)


# ------------------------------------------------------------------------------------------------ budget


async def test_a_fresh_probe_may_lose_the_whole_total_and_enter_with_q_max(ledger: PositionLedger) -> None:
    s = await ProbeBudget(CFG, ledger).state(P, D("80000"))
    assert (s.equity, s.cumulative_loss, s.l_rem, s.dust_mark, s.exposure) == (D(20), 0, D(3), 0, 0)
    assert s.entry_cap == D("3.00")


async def test_an_open_position_is_counted_at_the_bid_and_the_fees_are_a_loss(ledger: PositionLedger) -> None:
    await buy(ledger, "0.00003700", "80000", "0.0074")  # 2.96 EUR + fee
    s = await ProbeBudget(CFG, ledger).state(P, D("80000"))
    assert s.equity == D(20) - D("0.0074") and s.l_rem == D(3) - D("0.0074")
    down = await ProbeBudget(CFG, ledger).state(P, D("79000"))  # the mark falls: the budget shrinks with it
    assert down.equity == D(20) - D("0.0074") - D("0.037")
    assert down.exposure == D("0.00003700") * D("79000")


async def test_a_closed_trade_moves_the_budget_by_its_net_result(ledger: PositionLedger) -> None:
    await buy(ledger, "0.00003700", "80000", "0.0074")
    await sell(ledger, "0.00003700", "79500", "0.0147", T0 + timedelta(hours=1))  # a stop: -0.0185 - fees
    s = await ProbeBudget(CFG, ledger).state(P, D("79500"))
    assert s.exposure == 0
    assert s.equity == D(20) + D("0.00003700") * (D("79500") - D("80000")) - D("0.0074") - D("0.0147")
    assert s.entry_cap == min(D("3.00"), s.l_rem) == s.l_rem


async def test_dust_is_counted_once_and_taken_off_the_entry_cap(ledger: PositionLedger) -> None:
    await buy(ledger, "0.00001000", "80000", "0.002")  # 0.80 EUR of base...
    await ledger.apply_exit_fill(BOOK, P, D("0.00001000"))
    await ledger.set_dust(BOOK, P, D("0.00001000"), D("0.802"))  # ...turned into dust (not sellable)
    s = await ProbeBudget(CFG, ledger).state(P, D("80000"))
    assert s.dust_mark == D("0.8000") and s.exposure == D("0.8000")
    assert s.equity == D(20) - D("0.002")  # the 0.80 EUR is still worth 0.80: only the fee is lost
    assert s.entry_cap == min(D("3.00"), s.l_rem - s.dust_mark) == D("3") - D("0.002") - D("0.8")


async def test_the_entry_cap_never_lets_the_total_loss_pass_l_tot(ledger: PositionLedger) -> None:
    """Worst case: every position loses ALL its value. Entries sized at the cap (cost = cap) can never lose more
    than L_tot in total, however many there are."""
    lost = D(0)
    for _ in range(8):
        s = await ProbeBudget(CFG, ledger).state(P, D("80000"))
        cap = s.entry_cap
        if cap <= 0:
            break
        size = (cap / D("80000")).quantize(D("0.00000001"), rounding="ROUND_DOWN")
        if size <= 0:
            break
        await buy(ledger, str(size), "80000", "0")
        await sell(ledger, str(size), "0.01", "0", T0 + timedelta(hours=1))  # the price collapses
        lost = D(20) - (await ProbeBudget(CFG, ledger).state(P, D("80000"))).equity
    assert lost <= D(3)
    assert (await ProbeBudget(CFG, ledger).state(P, D("80000"))).entry_cap <= D("0.01")


async def test_exhaustion_is_when_the_smallest_protectable_entry_does_not_fit(ledger: PositionLedger) -> None:
    s = await ProbeBudget(CFG, ledger).state(P, D("80000"))
    protectable = limits.n_br(D(1), CFG.sl_frac) * (1 + D("0.012"))  # N_br with the coverage fee
    assert not s.exhausted(protectable)
    await buy(ledger, "0.00003500", "80000", "0")
    await sell(ledger, "0.00003500", "1", "0", T0 + timedelta(hours=1))  # lose 2.80 EUR
    assert (await ProbeBudget(CFG, ledger).state(P, D("80000"))).exhausted(protectable)  # 0.20 left < 1.04


# ----------------------------------------------------------------------------------------------- limiter


async def order(led: PositionLedger, cid: str, intent: str | None, at: datetime, leg: str = "entry") -> None:
    await led.record_order(
        OrderRecord(cid, BOOK, P, "BUY" if leg == "entry" else "SELL", leg, "FILLED", at, at, None, intent)
    )


async def test_entries_are_counted_from_the_ledger_and_the_stage_orders_do_not_count(ledger: PositionLedger) -> None:
    lim = ProbeLimiter(CFG, ledger)
    assert await lim.blocked(T0) is None
    await order(ledger, "t1", "probe-test:place-cancel:1", T0)  # a stage order: never an entry of the probe
    await order(ledger, "e1", "probe:BTC-EUR:20261028T0900Z", T0)
    c = await lim.counts(T0)
    assert (c.entries_today, c.entries_total, c.orders_today) == (1, 1, 2)
    await order(ledger, "b1", None, T0, leg="bracket")
    assert (await lim.counts(T0)).orders_today == 3 and (await lim.counts(T0)).entries_today == 1
    await order(ledger, "e2", "probe:BTC-EUR:20261028T1100Z", T0 + timedelta(hours=2))
    assert await lim.blocked(T0 + timedelta(hours=2)) == "max_entries_per_day"
    assert await lim.blocked(T0 + timedelta(days=1)) is None  # the day count restarts at 00:00 UTC


async def test_the_total_of_entries_is_five_across_days(ledger: PositionLedger) -> None:
    lim = ProbeLimiter(CFG, ledger)
    for i in range(5):
        await order(ledger, f"e{i}", f"probe:{i}", T0 + timedelta(days=i))
    assert await lim.blocked(T0 + timedelta(days=5)) == "max_entries_total"


async def test_the_counts_survive_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "probe.db"
    led = await PositionLedger.open(path, "live")
    await order(led, "e1", "probe:1", T0)
    await order(led, "e2", "probe:2", T0 + timedelta(hours=2))
    await led.close()
    again = await PositionLedger.open(path, "live")
    try:
        assert await ProbeLimiter(CFG, again).blocked(T0 + timedelta(hours=3)) == "max_entries_per_day"
    finally:
        await again.close()


async def test_the_cap_on_orders_blocks_entries_only(ledger: PositionLedger) -> None:
    cfg = ExecProbeConfig(max_orders_per_day=3)
    lim = ProbeLimiter(cfg, ledger)
    for i in range(3):
        await order(ledger, f"x{i}", None, T0, leg="bracket")
    assert await lim.blocked(T0) == "max_orders_per_day"
    # the limiter has no authority over brackets and exits: it is only consulted by the planner (no hook in the gateway)
    import inspect

    from modules.execution import gateway

    assert "ProbeLimiter" not in inspect.getsource(gateway)


# ----------------------------------------------------------------------------------------------- streaks


def trade(reason: str, at: datetime) -> ClosedTrade:
    return ClosedTrade(
        trade_id=f"x{at.timestamp()}{reason}", book=BOOK, product_id=P, entry_decision_id="d", entry_avg_price=D(1),
        entry_size=D(1), entry_fees=D(0), entry_at=at - timedelta(hours=1), tp=None, sl=None, k_tp=None, k_sl=None,
        exit_reason=reason, exit_avg_price=D(1), exit_size=D(1), exit_fees=D(0), exit_at=at, gross_quote=D(0),
        fees_quote=D(0), net_quote=D(0), net_bps=0, r_multiple=None, held_min=60,
    )  # fmt: skip


def feed(reasons: list[str]) -> ProbeStreaks:
    s = ProbeStreaks(CFG)
    for i, r in enumerate(reasons):
        s.observe_exit(r, T0 + timedelta(hours=i))
    return s


def test_two_stops_in_a_row_pause_the_entries_until_midnight_of_the_next_day() -> None:
    s = feed(["SL", "SL"])
    assert s.pause_until == datetime(2026, 10, 29, 0, 0, tzinfo=UTC)  # the second SL was at 10:00 of the 28th
    assert s.blocked_reason(T0 + timedelta(hours=5)) == "sl_streak_pause"
    assert s.blocked_reason(datetime(2026, 10, 29, 0, 0, tzinfo=UTC)) is None
    assert not s.finished and s.sl_streak == 0  # a new series is needed after the pause


def test_a_take_profit_between_two_stops_breaks_the_series() -> None:
    s = feed(["SL", "TP", "SL"])
    assert s.pause_until is None and s.blocked_reason(T0 + timedelta(hours=9)) is None and s.sl_streak == 1


@pytest.mark.parametrize("breaker", ["TP", "HORIZON", "SELL", "MANUAL"])
def test_every_close_that_is_not_a_stop_resets_the_series(breaker: str) -> None:
    assert feed(["SL", breaker, "SL"]).pause_until is None


def test_one_watchdog_exit_ends_the_probe_and_is_not_a_stop() -> None:
    s = feed(["WATCHDOG"])
    assert s.finished and s.sl_streak == 0 and s.blocked_reason(T0) == "finished:watchdog_exit"
    # and it does not touch the stop series either way
    s2 = ProbeStreaks(ExecProbeConfig())
    s2.observe_exit("SL", T0)
    s2.observe_exit("WATCHDOG", T0 + timedelta(hours=1))
    assert s2.sl_streak == 1 and s2.watchdog_count == 1
    s2.observe_exit("SL", T0 + timedelta(hours=2))
    assert s2.pause_until is not None  # SL, WATCHDOG, SL: the two stops count together


def test_the_replay_of_the_closed_trades_gives_the_live_state() -> None:
    reasons = ["SL", "TP", "SL", "SL", "HORIZON", "SL"]
    live = feed(reasons)
    rebuilt = ProbeStreaks(CFG)
    rebuilt.replay([trade(r, T0 + timedelta(hours=i)) for i, r in enumerate(reasons)])
    assert (rebuilt.sl_streak, rebuilt.watchdog_count, rebuilt.pause_until) == (
        live.sl_streak, live.watchdog_count, live.pause_until,
    )  # fmt: skip
    again = ProbeStreaks(CFG)
    again.replay(
        reversed([trade(r, T0 + timedelta(hours=i)) for i, r in enumerate(reasons)])
    )  # order of arrival is irrelevant
    assert again.pause_until == live.pause_until


def test_the_accountguards_single_series_is_neutralised_for_the_probe() -> None:
    assert CFG.max_entries_total + 1 == 6  # the value the runner gives to AccountGuards.max_consecutive_losses


def test_midnight_helper() -> None:
    assert next_midnight_utc(datetime(2026, 10, 28, 23, 59, tzinfo=UTC)) == datetime(2026, 10, 29, tzinfo=UTC)
