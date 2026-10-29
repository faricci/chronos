"""SimulatedExchange: the bar-level venue rules of sez. 5.1.3 (maker fill with haircut, stop rules, horizon)."""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pytest

from modules.dryrun.fill_model import BarFillModel, FillModelParams
from modules.dryrun.sim_exchange import MarketBars, SimulatedExchange
from modules.risk.costs import FeeRates

D = Decimal
FEES = FeeRates(maker_bps=25, taker_bps=50, assumed=False)
QINC = {"SOL-EUR": D("0.01")}
F, T_IN, H = 3, 30, 100


def make_exchange(haircut: float = 0.25) -> SimulatedExchange:
    return SimulatedExchange(BarFillModel(FillModelParams(queue_haircut=haircut)), FEES, QINC, F, T_IN, H)


def market(n: int = 400, price: float = 100.0, volume: float = 1000.0) -> MarketBars:
    ts = (np.datetime64("2026-01-01T00:01") + np.arange(n) * np.timedelta64(1, "m")).astype("datetime64[ns]")
    full = np.full(n, price)
    return MarketBars("SOL-EUR", ts, full.copy(), full.copy(), full.copy(), full.copy(), np.full(n, volume))


# ------------------------------------------------------------------------------------ maker entry


def test_maker_fills_on_the_first_allowed_bar_below_the_limit() -> None:
    m = market()
    m.lo[13] = 99.0  # decision bar 10 + F = 13
    out = make_exchange().entry_maker(m, 10, D("99.5"), D("2"))
    assert (out.status, out.bar, out.price, out.haircut_blocked_bars) == ("FILLED", 13, D("99.5"), 0)


def test_maker_cannot_fill_before_the_order_exists() -> None:
    m = market()
    m.lo[11] = m.lo[12] = 50.0
    assert make_exchange().entry_maker(m, 10, D("99.5"), D("2")).status == "EXPIRED"


def test_a_touch_is_not_a_fill() -> None:
    m = market()
    m.lo[20] = 99.5
    assert make_exchange().entry_maker(m, 10, D("99.5"), D("2")).status == "EXPIRED"


def test_thin_bars_block_the_fill_and_are_counted() -> None:
    """test_haircut_blocks_fill_on_thin_bar: the quantity must fit in haircut x the bar volume (all or nothing)."""
    m = market(volume=1.0)  # haircut 25% of 1.0 = 0.25 base per bar
    m.lo[14] = m.lo[15] = 99.0
    m.v[16] = 100.0
    m.lo[16] = 99.0
    out = make_exchange().entry_maker(m, 10, D("99.5"), D("2"))
    assert (out.status, out.bar, out.haircut_blocked_bars) == ("FILLED", 16, 2)
    m2 = market(volume=1.0)
    m2.lo[14] = 99.0
    expired = make_exchange().entry_maker(m2, 10, D("99.5"), D("2"))
    assert expired.status == "EXPIRED" and expired.haircut_blocked_bars == 1


def test_the_window_is_exactly_t_in_bars() -> None:
    last = 10 + F + T_IN - 1
    m = market()
    m.lo[last] = 99.0
    assert make_exchange().entry_maker(m, 10, D("99.5"), D("2")).bar == last
    m2 = market()
    m2.lo[last + 1] = 99.0
    out = make_exchange().entry_maker(m2, 10, D("99.5"), D("2"))
    assert out.status == "EXPIRED" and out.bar == last


def test_a_decision_too_close_to_the_end_of_the_data_expires() -> None:
    m = market(n=12)
    assert make_exchange().entry_maker(m, 10, D("99.5"), D("1")).status == "EXPIRED"


# ------------------------------------------------------------------------------------ taker entry


def test_taker_enters_at_the_open_of_bar_t_plus_f_with_slippage_rounded_up() -> None:
    m = market()
    m.o[13] = 100.0
    out = make_exchange().entry_taker(m, 10, D("100.20"), 4.0, "SOL-EUR")
    # 100 * (1 + (2 + 5) / 1e4) = 100.07
    assert (out.status, out.bar, out.price) == ("FILLED", 13, D("100.07"))
    assert make_exchange().entry_taker(m, 10, D("100.05"), 4.0, "SOL-EUR").status == "EXPIRED"  # limit below the price


# ----------------------------------------------------------------------------------------- bracket


def test_take_profit_needs_a_trade_strictly_above_and_fills_at_the_target() -> None:
    m = market()
    m.h[60] = 110.0
    out = make_exchange().bracket(m, 20, D("109"), D("90"), 4.0)
    assert (out.reason, out.bar, out.price) == ("TP", 60, D("109"))
    m2 = market()
    m2.h[60] = 109.0  # touching is not enough
    assert make_exchange().bracket(m2, 20, D("109"), D("90"), 4.0).reason == "HORIZON"


def test_stop_price_has_gap_awareness_slippage_and_rounds_down() -> None:
    m = market()
    m.lo[50] = 89.0
    m.o[50] = 100.0
    out = make_exchange().bracket(m, 20, D("109"), D("90"), 4.0)
    # min(90, 100) * (1 - (2 + 5 + 10) / 1e4) = 89.847
    assert (out.reason, out.bar, out.price) == ("SL", 50, D("89.84"))
    gap = market()
    gap.o[50], gap.lo[50] = 85.0, 84.0
    out = make_exchange().bracket(gap, 20, D("109"), D("90"), 4.0)
    assert out.price == D("84.85")  # 85 * (1 - 17/1e4) = 84.8555 rounded down


def test_same_bar_stop_wins() -> None:
    m = market()
    m.lo[40], m.h[40] = 89.0, 120.0
    assert make_exchange().bracket(m, 20, D("109"), D("90"), 4.0).reason == "SL"


def test_only_the_stop_acts_in_the_fill_bar() -> None:
    m = market()
    m.h[20] = 150.0  # a target in the fill bar does not exist yet
    assert make_exchange().bracket(m, 20, D("109"), D("90"), 4.0).reason == "HORIZON"
    m.lo[20] = 80.0
    out = make_exchange().bracket(m, 20, D("109"), D("90"), 4.0)
    assert (out.reason, out.bar) == ("SL", 20)


def test_horizon_exits_at_the_vertical_close_with_taker_costs() -> None:
    m = market()
    m.c[120] = 101.0  # fill 20 + H 100
    out = make_exchange().bracket(m, 20, D("109"), D("90"), 4.0)
    assert (out.reason, out.bar, out.price) == (
        "HORIZON",
        120,
        D("100.92"),
    )  # 101 * (1 - 7/1e4) = 100.9293, rounded down


def test_a_bracket_that_cannot_resolve_yet_is_none() -> None:
    m = market(n=60)
    assert make_exchange().bracket(m, 20, D("109"), D("90"), 4.0) is None


def test_model_sell_executes_at_the_open_of_bar_t_plus_f() -> None:
    m = market()
    m.o[33] = 102.0
    out = make_exchange().model_sell(m, 30, 4.0)
    assert (out.reason, out.bar) == ("SELL", 33) and out.price == D("101.92")  # 102 * (1 - 7/1e4) = 101.92860 down
    assert make_exchange().model_sell(market(n=40), 38, 4.0) is None


def test_fee_quote() -> None:
    ex = make_exchange()
    assert ex.fee_quote(D("100"), D("2"), "maker") == D("0.5") and ex.fee_quote(D("100"), D("2"), "taker") == D("1")


@pytest.mark.parametrize("haircut", [0.0, 0.25, 1.0])
def test_a_zero_haircut_never_fills(haircut: float) -> None:
    m = market()
    m.lo[14] = 99.0
    out = make_exchange(haircut).entry_maker(m, 10, D("99.5"), D("2"))
    assert (out.status == "FILLED") == (haircut > 0)
