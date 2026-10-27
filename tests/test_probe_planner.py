"""ProbePlanner: the deterministic entry, its nine conditions in order, and the ATR1h helper (plan sez. 2)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from modules.config import ExecProbeConfig, FallbackFees
from modules.execution.ids import client_order_id
from modules.execution.orders import ProductMeta
from modules.probe import limits
from modules.probe.budget import BudgetState
from modules.probe.planner import PlanFacts, ProbePlanner, ProbeSkip, atr_wilder, decision_id
from modules.risk.costs import FeeRates, coverage_fee_rate, required_quote
from modules.risk.risk_manager import MarketQuote, OrderIntent

D = Decimal
P = "BTC-EUR"
SLOT = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
NOW = SLOT + timedelta(seconds=3)
META = ProductMeta(
    product_id=P, base_currency="BTC", quote_currency="EUR", base_increment=D("0.00000001"),
    quote_increment=D("0.01"), base_min_size=D("0.00000001"), quote_min_size=D("1"),
)  # fmt: skip
CFG = ExecProbeConfig()


def budget(entry_cap: str = "3.00", dust: str = "0") -> BudgetState:
    return BudgetState(D(20), D(0), D(3), D(dust), D(dust), D(entry_cap))


def facts(**kw: Any) -> PlanFacts:
    base = PlanFacts(
        now=NOW, slot=SLOT, product_id=P, meta=META, fees=FeeRates(maker_bps=25, taker_bps=50, assumed=False),
        fallback=FallbackFees(), manual_stop=False, entry_blocked=None, limit_blocked=None, position_flat=True,
        open_orders=0, balance_age_s=10.0, free_quote=D("5.00"), quote=MarketQuote(D("80000.00"), D("80001.00")),
        quote_age_s=1.0, atr1h=D("200"), last_close=D("80000"), budget=budget(),
    )  # fmt: skip
    return replace(base, **kw)


def intent(f: PlanFacts | None = None) -> OrderIntent:
    out = ProbePlanner(CFG).plan(f or facts())
    assert isinstance(out, OrderIntent), out
    return out


def skip(f: PlanFacts) -> ProbeSkip:
    out = ProbePlanner(CFG).plan(f)
    assert isinstance(out, ProbeSkip), out
    return out


# ------------------------------------------------------------------------------------------ the intent


def test_the_intent_of_a_normal_slot() -> None:
    i = intent()
    assert i.decision_id == i.intent_id == "probe:BTC-EUR:20261028T0900Z"
    assert (i.book, i.product_id, i.side) == ("probe", P, "BUY")
    assert (i.entry.type, i.entry.limit_price, i.entry.time_in_force_s) == ("limit_post_only", D("80000.00"), 1800)
    assert i.entry.expires_on_exchange is True  # the exchange expires it: GTD
    assert i.bracket.take_profit == D("80480.00") and i.bracket.stop_loss == D("79520.00")
    assert (i.bracket.k_tp, i.bracket.k_sl, i.bracket.atr_tf, i.bracket.atr) == (0.0, 0.0, "probe", D("200"))
    assert i.base_size == D("0.00003693")
    r = i.risk
    assert (r.p_edge, r.scale_p, r.regime, r.m_reg, r.ev_bps, r.p_breakeven, r.p_be_buffer) == (
        0.0,
        0.0,
        0,
        0.0,
        0,
        None,
        0.0,
    )
    assert i.created_at == NOW


def test_the_client_order_id_comes_from_the_slot_and_never_changes() -> None:
    a, b = intent(), intent(facts(now=NOW + timedelta(seconds=40)))
    assert client_order_id(a.decision_id, "entry") == client_order_id(b.decision_id, "entry")
    other = intent(facts(slot=SLOT + timedelta(hours=2)))
    assert other.decision_id == "probe:BTC-EUR:20261028T1100Z"
    assert client_order_id(other.decision_id, "entry") != client_order_id(a.decision_id, "entry")
    assert decision_id(P, SLOT) == a.decision_id


def test_the_entry_is_always_a_post_only_maker_at_or_below_the_bid() -> None:
    for bid in ("80000.00", "79999.99", "81234.56", "100000.01"):
        b = D(bid)
        i = intent(facts(quote=MarketQuote(b, b + D("1.00")), last_close=b, budget=budget()))
        assert i.entry.type == "limit_post_only" and i.entry.limit_price <= b
        assert i.bracket.stop_loss < i.entry.limit_price < i.bracket.take_profit
        assert i.entry.limit_price == D(bid)  # the bid is on the tick already


def test_levels_are_rounded_against_the_probe() -> None:
    i = intent(facts(quote=MarketQuote(D("79999.999"), D("80001.00"))))
    assert i.entry.limit_price == D("79999.99")  # floor: a post-only BUY must not cross
    assert i.bracket.take_profit == D("80479.98") and i.bracket.stop_loss == D("79520.00")  # floor TP, ceil SL


def test_the_required_quote_never_exceeds_the_cap_nor_q_max() -> None:
    fee = coverage_fee_rate(FeeRates(maker_bps=25, taker_bps=50, assumed=False), FallbackFees())
    assert fee == D("0.012")  # never below the 120 bps floor
    for cap in ("3.00", "2.50", "1.50", "1.05", "2.99"):
        for price in ("80000.00", "79123.45", "91000.07"):
            p = D(price)
            out = ProbePlanner(CFG).plan(facts(quote=MarketQuote(p, p + 1), last_close=p, budget=budget(cap)))
            assert isinstance(out, OrderIntent), (cap, price, out)
            need = required_quote(out.entry.limit_price, out.base_size, fee, META.quote_increment)
            assert need <= min(D(cap), CFG.max_entry_quote) and need <= D("3.00")


def test_the_cap_is_q_max_even_when_the_budget_and_the_account_are_larger() -> None:
    out = intent(facts(budget=BudgetState(D(20), D(0), D("30"), D(0), D(0), D("3.00")), free_quote=D("500")))
    fee = D("0.012")
    assert required_quote(out.entry.limit_price, out.base_size, fee, D("0.01")) <= D("3.00")


def test_the_dust_is_taken_off_the_cap() -> None:
    with_dust = intent(facts(budget=budget("2.20", dust="0.80")))
    without = intent()
    assert with_dust.base_size < without.base_size
    need = required_quote(with_dust.entry.limit_price, with_dust.base_size, D("0.012"), D("0.01"))
    assert need + D("0.80") <= D("3.00")


def test_the_account_balance_limits_the_size_too() -> None:
    out = intent(facts(free_quote=D("2.00")))
    assert required_quote(out.entry.limit_price, out.base_size, D("0.012"), D("0.01")) <= D("2.00")


# ------------------------------------------------------------------------------------------- the skips


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"manual_stop": True}, "manual_stop"),
        ({"entry_blocked": "daily_loss"}, "entry_blocked"),
        ({"limit_blocked": "max_entries_per_day"}, "max_entries_per_day"),
        ({"position_flat": False}, "position_open"),
        ({"open_orders": 1}, "open_orders"),
        ({"balance_age_s": None}, "balance_stale"),
        ({"balance_age_s": 120.5}, "balance_stale"),
        ({"quote": None}, "quote_unavailable"),
        ({"quote_age_s": 5.5}, "quote_stale"),
        ({"quote": MarketQuote(D("80001"), D("80000"))}, "quote_invalid"),
        ({"quote": MarketQuote(D("80000"), D("80100"))}, "spread_wide"),  # 12.5 bps
        ({"atr1h": None}, "atr_unavailable"),
        ({"atr1h": D("500")}, "atr_high"),  # 62.5 bps
        ({"budget": None}, "budget_unavailable"),
        ({"budget": budget("1.00")}, "budget_exhausted"),
        ({"free_quote": D("1.00")}, "insufficient_balance"),
    ],
)
def test_each_condition_gives_its_own_skip(change: dict[str, Any], reason: str) -> None:
    assert skip(facts(**change)).reason == reason


def test_the_first_false_condition_wins() -> None:
    both = facts(manual_stop=True, entry_blocked="daily_loss", position_flat=False, quote=None, atr1h=None)
    assert skip(both).reason == "manual_stop"
    assert skip(replace(both, manual_stop=False)).reason == "entry_blocked"
    assert skip(replace(both, manual_stop=False, entry_blocked=None)).reason == "position_open"
    assert skip(replace(both, manual_stop=False, entry_blocked=None, position_flat=True)).reason == "quote_unavailable"


def test_atr_at_the_threshold_passes_and_just_above_fails() -> None:
    assert isinstance(ProbePlanner(CFG).plan(facts(atr1h=D("480"))), OrderIntent)  # exactly 60 bps
    assert skip(facts(atr1h=D("480.01"))).reason == "atr_high"


def test_a_spread_at_the_threshold_passes() -> None:
    assert isinstance(
        ProbePlanner(CFG).plan(facts(quote=MarketQuote(D("80000.00"), D("80080.00")))), OrderIntent
    )  # 10 bps


def test_only_the_budget_exhaustion_ends_the_probe_and_the_transient_skips_are_marked() -> None:
    assert skip(facts(budget=budget("1.03"))).ends_the_probe
    assert not skip(facts(free_quote=D("1"))).ends_the_probe
    assert skip(facts(quote_age_s=9)).transient and skip(facts(balance_age_s=500.0)).transient
    assert not skip(facts(manual_stop=True)).transient and not skip(facts(position_flat=False)).transient


def test_a_budget_just_above_the_smallest_protectable_entry_still_trades() -> None:
    out = ProbePlanner(CFG).plan(facts(budget=budget("1.10")))
    assert isinstance(out, OrderIntent) and out.base_size * out.entry.limit_price >= limits.n_br(D(1), D("0.006"))


def test_a_budget_that_the_cushion_and_rounding_eat_is_exhausted_too() -> None:
    # 1.04 passes the plan's formula (N_br * 1.012 = 1.0385) but one tick of cushion and the floor leave 1.0176 < N_br
    out = skip(facts(budget=budget("1.04")))
    assert out.reason == "budget_exhausted" and out.ends_the_probe


def test_a_lowered_config_changes_the_planner() -> None:
    low = ExecProbeConfig(sl_frac=D("0.004"), tp_frac=D("0.004"), max_entry_quote=D("2.00"))
    out = ProbePlanner(low).plan(facts())
    assert isinstance(out, OrderIntent)
    assert out.bracket.stop_loss == D("79680.00") and required_quote(
        out.entry.limit_price, out.base_size, D("0.012"), D("0.01")
    ) <= D("2.00")  # fmt: skip


# ------------------------------------------------------------------------------------------------ ATR


def candle(start: int, high: str, low: str, close: str) -> dict[str, str]:
    return {"start": str(start), "high": high, "low": low, "close": close, "open": close, "volume": "1"}


def hourly(n: int, *, tr: str = "100", base: str = "80000") -> list[dict[str, str]]:
    t0 = int(datetime(2026, 10, 27, 0, 0, tzinfo=UTC).timestamp())
    b = D(base)
    return [candle(t0 + i * 3600, str(b + D(tr) / 2), str(b - D(tr) / 2), str(b)) for i in range(n)]


def test_the_atr_of_a_constant_range_is_that_range() -> None:
    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    atr, last = atr_wilder(hourly(30), now) or (None, None)
    assert atr == D(100) and last == D(80000)


def test_the_forming_candle_is_ignored() -> None:
    candles = hourly(24)
    t_last = int(candles[-1]["start"])
    forming = candle(t_last + 3600, "90000", "70000", "75000")  # an hour that has not ended
    now = datetime.fromtimestamp(t_last + 3600 + 600, UTC)
    atr, last = atr_wilder([*candles, forming], now) or (None, None)
    assert atr == D(100) and last == D(80000)


def test_wilder_smoothing_follows_the_last_ranges() -> None:
    candles = hourly(20)
    candles.append(candle(int(candles[-1]["start"]) + 3600, "80350", "80000", "80000"))  # a 350 range after 100s
    now = datetime(2026, 10, 29, 0, 0, tzinfo=UTC)
    atr, _ = atr_wilder(candles, now) or (None, None)
    assert atr == (D(100) * 13 + D(350)) / 14


def test_too_few_or_broken_candles_give_none() -> None:
    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    assert atr_wilder(hourly(14), now) is None  # 14 candles = 13 ranges
    assert atr_wilder([{"start": "x"}], now) is None
    assert atr_wilder([], now) is None
