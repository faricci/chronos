"""CostModel against the worked reference case of spec sez. 0.3 / 4.1.2 and the lift table of sez. 0.4."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from modules.config import AppConfig, FallbackFees
from modules.risk.costs import (
    CostModel,
    CostParams,
    FeeProvider,
    FeeRates,
    net_pnl_quote,
    net_return,
    round_bps,
)

ASSUMED = FeeRates(maker_bps=60, taker_bps=120, assumed=True)


def model(spread: float, entry_mode: str = "maker", fees: FeeRates = ASSUMED) -> CostModel:
    return CostModel(fees=fees, entry_mode=entry_mode, spread_bps=spread)  # type: ignore[arg-type]


# ------------------------------------------------------------------------- reference example, sez. 0.3

# SOL-EUR, best_bid 92.03, s = 3 bps, ATR_14_1d = 460 bps (4.2334 EUR), maker entry, k_tp = 4, k_sl = 2
REF = model(spread=3)
REF_TP, REF_SL = 4 * 460.0, 2 * 460.0


def test_reference_legs() -> None:
    assert REF.c_in == 60
    assert REF.c_tp_out == 60
    assert REF.c_sl_out == pytest.approx(120 + 1.5 + 5 + 10)
    assert REF.c_mkt_out == pytest.approx(120 + 1.5 + 5)


def test_reference_round_trips() -> None:
    assert REF.cost_tp == 120
    assert REF.cost_sl == pytest.approx(196.5)
    assert round_bps(REF.cost_sl) == 197  # value shown in the spec and in the OrderIntent example
    assert REF.cost_mkt == pytest.approx(186.5)


def test_reference_fee_floor_and_breakeven() -> None:
    assert REF_TP == 1840 and REF_SL == 920
    assert REF.tp_net_bps(REF_TP) == 1720
    assert REF.passes_fee_floor(REF_TP, min_net_edge_bps=30)
    assert REF.sl_loss_bps(REF_SL) == pytest.approx(1116.5)  # spec rounds to 1117
    p_be = REF.p_breakeven(REF_TP, REF_SL)
    assert p_be == pytest.approx(1116.5 / (1720 + 1116.5))
    assert round(p_be or 0, 2) == 0.39


def test_reference_ev() -> None:
    ev = REF.ev_bps(0.52, REF_TP, REF_SL)
    assert ev == pytest.approx(0.52 * 1720 - 0.48 * 1116.5)
    assert round_bps(ev) == 358


def test_reference_sizing_loss_per_unit() -> None:
    """sez. 4.1.2: loss_per_unit = (entry - SL) + entry * cost_sl / 1e4 = 8.46 + 1.81 = 10.27 EUR/SOL."""
    entry, sl = 92.03, 83.57
    loss_per_unit = (entry - sl) + entry * REF.cost_sl / 1e4
    assert round(loss_per_unit, 2) == 10.27
    risk_eur = 100 * 0.005 * 0.5 * 1.0  # equity * risk_per_trade * scale_p * m_reg
    assert risk_eur == 0.25
    assert risk_eur / loss_per_unit == pytest.approx(0.0243, abs=1e-4)  # floors to 0.024 SOL


# --------------------------------------------------------------------------- taker entry and the 0.4 table


def test_taker_entry_pays_spread_and_slippage() -> None:
    m = model(spread=3, entry_mode="taker")
    assert m.c_in == pytest.approx(120 + 1.5 + 5)
    assert m.cost_tp == pytest.approx(126.5 + 60)
    assert m.cost_sl == pytest.approx(126.5 + 136.5)


@pytest.mark.parametrize(
    ("entry_mode", "atr_bps", "spread", "expected_lift"),
    [
        # sez. 0.4, k_tp/k_sl = 4/2, columns BTC, ETH, SOL. None = the TP does not cover its costs.
        ("maker", 30, 2.9, None),
        ("maker", 48, 2.0, 0.47),
        ("maker", 61, 3.3, 0.39),  # 1h
        ("taker", 30, 2.9, None),
        ("taker", 48, 2.0, 0.65),
        ("taker", 61, 3.3, 0.54),  # 1h
        ("maker", 120, 2.9, 0.21),
        ("maker", 187, 2.0, 0.14),
        ("maker", 222, 3.3, 0.12),  # 6h
        ("taker", 120, 2.9, 0.30),
        ("taker", 187, 2.0, 0.20),
        ("taker", 222, 3.3, 0.17),  # 6h
        ("maker", 253, 2.9, 0.11),
        ("maker", 375, 2.0, 0.07),
        ("maker", 460, 3.3, 0.06),  # 1d
        ("taker", 253, 2.9, 0.15),
        ("taker", 375, 2.0, 0.10),
        ("taker", 460, 3.3, 0.08),  # 1d
    ],
)
def test_required_lift_table(entry_mode: str, atr_bps: float, spread: float, expected_lift: float | None) -> None:
    m = model(spread=spread, entry_mode=entry_mode)
    lift = m.required_lift(4 * atr_bps, 2 * atr_bps)
    if expected_lift is None:
        assert lift is None
    else:
        assert lift is not None
        assert round(lift, 2) == expected_lift


def test_eth_hourly_taker_is_below_the_fee_floor() -> None:
    assert not model(2.0, "taker").passes_fee_floor(4 * 48, 30)  # tp_net = 192 - 186 = 6 < 30


def test_hourly_atr_does_not_cover_costs() -> None:
    """sez. 0.4: with ATR 1h = 30 bps (BTC) the TP at 4 ATR does not pay its own cost."""
    m = model(spread=2.9)
    assert m.tp_net_bps(4 * 30) <= 0
    assert m.p_breakeven(4 * 30, 2 * 30) is None
    assert m.required_lift(4 * 30, 2 * 30) is None
    assert not m.passes_fee_floor(4 * 30, min_net_edge_bps=30)
    assert m.ev_bps(0.9, 4 * 30, 2 * 30) < 0  # even a 90% hit rate loses money


def test_stricter_fee_floor_blocks_marginal_trades() -> None:
    assert not REF.passes_fee_floor(tp_bps=140, min_net_edge_bps=30)  # 140 - 120 = 20 < 30
    assert REF.passes_fee_floor(tp_bps=150, min_net_edge_bps=30)  # exactly 30


def test_params_follow_config() -> None:
    p = CostParams.from_config(AppConfig())
    assert p.slippage_buffer_bps == 5 and p.stop_extra_slip_bps == 10
    m = CostModel(fees=ASSUMED, entry_mode="maker", spread_bps=3, params=CostParams(stop_extra_slip_bps=0))
    assert m.c_sl_out == pytest.approx(126.5)


def test_ev_rejects_invalid_probability() -> None:
    with pytest.raises(ValueError, match="p_edge"):
        REF.ev_bps(1.2, REF_TP, REF_SL)


def test_maker_above_taker_is_rejected() -> None:
    with pytest.raises(ValueError, match="above taker"):
        FeeRates(maker_bps=130, taker_bps=120, assumed=False)


@given(
    maker=st.floats(0, 100),
    extra=st.floats(0, 100),
    spread=st.floats(0, 50),
    atr=st.floats(50, 2000),
    k_tp=st.floats(1, 6),
    k_sl=st.floats(0.5, 4),
    mode=st.sampled_from(["maker", "taker"]),
)
def test_ev_is_zero_at_breakeven_probability(
    maker: float, extra: float, spread: float, atr: float, k_tp: float, k_sl: float, mode: str
) -> None:
    m = model(spread, mode, FeeRates(maker_bps=maker, taker_bps=maker + extra, assumed=True))
    tp, sl = k_tp * atr, k_sl * atr
    p_be = m.p_breakeven(tp, sl)
    if p_be is None:
        assert m.tp_net_bps(tp) <= 0
        return
    assert 0.0 < p_be < 1.0
    assert m.ev_bps(p_be, tp, sl) == pytest.approx(0.0, abs=1e-6)
    assert m.ev_bps(min(1.0, p_be + 0.05), tp, sl) >= m.ev_bps(p_be, tp, sl)  # EV grows with p


# ---------------------------------------------------------------------------------------------- rounding


@pytest.mark.parametrize(
    ("x", "expected"), [(196.5, 197), (0.5, 1), (1.5, 2), (2.5, 3), (358.2, 358), (-1.5, -2), (120.0, 120)]
)
def test_round_bps_is_half_up(x: float, expected: int) -> None:
    assert round_bps(x) == expected


# --------------------------------------------------------------------------------------- exact pnl


def test_net_return_exact_for_reference_tp_trade() -> None:
    """Maker entry at 92.03, maker exit at TP 108.96, size 0.024: spec 4.1.2 position."""
    entry, tp, size = Decimal("92.03"), Decimal("108.96"), Decimal("0.024")
    assert net_pnl_quote(entry, tp, size, 60, 60) == Decimal("0.37737744")
    assert net_return(entry, tp, 60, 60) == pytest.approx(Decimal("0.169838947"), abs=Decimal("1e-9"))
    # the approximation used by the gates is close to the exact value: tp_bps - cost_tp = 1720 vs exact 1698
    exact_bps = float(net_return(entry, tp, 60, 60)) * 1e4
    assert abs(exact_bps - REF.tp_net_bps(REF_TP)) < 40


def test_net_return_for_a_stopped_trade_is_negative() -> None:
    entry, stop_fill = Decimal("92.03"), Decimal("83.40")  # stop 83.57 minus extra slippage
    assert net_return(entry, stop_fill, 60, 120) < Decimal("-0.09")


def test_net_return_with_zero_fees_is_plain_return() -> None:
    assert net_return(Decimal("100"), Decimal("110"), 0, 0) == Decimal("0.1")


@given(
    entry=st.decimals(min_value="0.01", max_value="100000", places=4),
    ret=st.decimals(min_value="-0.5", max_value="2", places=4),
    size=st.decimals(min_value="0.0001", max_value="1000", places=4),
    f_in=st.floats(0, 150),
    f_out=st.floats(0, 150),
)
def test_pnl_quote_is_size_times_return(entry: Decimal, ret: Decimal, size: Decimal, f_in: float, f_out: float) -> None:
    exit_px = entry * (1 + ret)
    pnl = net_pnl_quote(entry, exit_px, size, f_in, f_out)
    cost = entry * size * (1 + Decimal(str(f_in)) / 10000)
    expected = cost * net_return(entry, exit_px, f_in, f_out)
    assert pnl == pytest.approx(expected, rel=Decimal("1e-20"), abs=Decimal("1e-20"))


# ------------------------------------------------------------------------------------------ FeeProvider


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeFetcher:
    def __init__(self, *results: tuple[float, float] | Exception) -> None:
        self.results = list(results)
        self.calls = 0

    async def __call__(self) -> tuple[float, float]:
        self.calls += 1
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


FALLBACK = FallbackFees(maker_bps=60, taker_bps=120)


async def test_without_fetcher_uses_assumed_fallback() -> None:
    p = FeeProvider(FALLBACK)
    assert p.current == ASSUMED
    fees = await p.refresh()
    assert fees.assumed and (fees.maker_bps, fees.taker_bps) == (60, 120)


async def test_fetched_fees_replace_fallback_and_are_cached_for_an_hour() -> None:
    clock, fetch = FakeClock(), FakeFetcher((35.0, 75.0), (25.0, 60.0))
    p = FeeProvider(FALLBACK, fetch, clock=clock)
    first = await p.refresh()
    assert not first.assumed and (first.maker_bps, first.taker_bps) == (35, 75)
    assert first.as_of == clock.now
    clock.advance(3599)
    assert await p.refresh() == first
    assert fetch.calls == 1
    clock.advance(2)
    second = await p.refresh()
    assert (second.maker_bps, second.taker_bps) == (25, 60)
    assert fetch.calls == 2


async def test_fetch_failure_falls_back_to_assumed_and_retries_soon() -> None:
    clock, fetch = FakeClock(), FakeFetcher(RuntimeError("boom"), (35.0, 75.0))
    p = FeeProvider(FALLBACK, fetch, clock=clock, retry_s=60)
    failed = await p.refresh()
    assert failed.assumed and failed.taker_bps == 120
    clock.advance(30)
    assert (await p.refresh()).assumed and fetch.calls == 1  # still inside the retry window
    clock.advance(31)
    recovered = await p.refresh()
    assert not recovered.assumed and recovered.taker_bps == 75


async def test_malformed_fees_from_the_exchange_count_as_failure() -> None:
    p = FeeProvider(FALLBACK, FakeFetcher((90.0, 75.0)), clock=FakeClock())  # maker > taker
    assert (await p.refresh()).assumed


async def test_failure_after_success_goes_back_to_fallback() -> None:
    clock, fetch = FakeClock(), FakeFetcher((35.0, 75.0), RuntimeError("down"))
    p = FeeProvider(FALLBACK, fetch, clock=clock)
    assert not (await p.refresh()).assumed
    clock.advance(3601)
    assert (await p.refresh()).assumed  # sez. 5.1.3: fall back to the (higher) configured fees


@given(
    maker=st.floats(0, 100),
    extra=st.floats(0, 100),
    spread=st.floats(0, 50),
    atr=st.floats(50, 2000),
    k_tp=st.floats(1, 6),
    k_sl=st.floats(0.5, 4),
    mode=st.sampled_from(["maker", "taker"]),
    u=st.floats(0, 1, exclude_min=True),
)
def test_vertical_exit_is_never_worse_than_the_modelled_stop(
    maker: float, extra: float, spread: float, atr: float, k_tp: float, k_sl: float, mode: str, u: float
) -> None:
    """Invariant behind the two-outcome EV gate (sez. 0.3): valuing VERTICAL as a stop is conservative."""
    m = model(spread, mode, FeeRates(maker_bps=maker, taker_bps=maker + extra, assumed=True))
    tp, sl = k_tp * atr, k_sl * atr
    r_bps = -sl + u * (tp + sl)  # vertical close strictly above lower, not above upper
    assert m.c_mkt_out <= m.c_sl_out
    assert m.cost_mkt - r_bps < m.sl_loss_bps(sl) + 1e-9


def test_round_bps_accepts_numpy_scalars() -> None:
    import numpy as np

    assert round_bps(np.float64(196.5)) == 197  # type: ignore[arg-type]
