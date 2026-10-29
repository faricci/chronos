"""RiskManager: the reference example of the spec, every gate, and the contract with the policy (sez. 4.1)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from modules.config import EntryTimeout, FallbackFees, RiskConfig
from modules.decision.policy import Decision
from modules.ledger.positions import PositionView
from modules.risk.costs import FeeRates
from modules.risk.label_config import LabelConfig
from modules.risk.risk_manager import MarketQuote, OrderIntent, PolicyContractError, RiskManager, Skip
from tests.coverage_fixtures import SOL

D = Decimal
NOW = datetime(2026, 10, 17, 14, 3, 1, tzinfo=UTC)
ASSUMED = FeeRates(maker_bps=60, taker_bps=120, assumed=True)
REAL = FeeRates(maker_bps=25, taker_bps=50, assumed=False)
LABEL_1D = LabelConfig(atr_tf="1d", k_tp=4.0, k_sl=2.0, entry_mode="maker", horizon_min=14400)
GOLDEN = Path(__file__).parent / "golden"
FLAT = PositionView(book="champion", product_id="SOL-EUR")


class Gate:
    def __init__(self, reason: str | None = None) -> None:
        self.reason = reason

    def entry_blocked(self, book: str) -> str | None:
        return self.reason


def manager(label: LabelConfig = LABEL_1D, gate: Gate | None = None, **risk: object) -> RiskManager:
    return RiskManager("champion", RiskConfig(**risk), label, FallbackFees(), EntryTimeout(), gate)  # type: ignore[arg-type]


def buy(p_edge: float = 0.52, regime: int = 2) -> Decision:
    return Decision("BUY", "thresholds_met", p_edge=p_edge, regime=regime, confidence=0.4)


def plan(rm: RiskManager | None = None, **kw: object) -> OrderIntent | Skip:
    args: dict[str, object] = {
        "decision": buy(), "decision_id": "dec-1", "intent_id": "int-1", "now": NOW, "product_id": "SOL-EUR",
        "quote": MarketQuote(D("92.03"), D("92.06")), "atr": D("4.2334"), "fees": ASSUMED, "meta": SOL,
        "equity": D("100"), "free_quote": D("100"), "p_be_buffer": 0.05, "position": FLAT,
    }  # fmt: skip
    return (rm or manager()).plan_entry(**{**args, **kw})  # type: ignore[arg-type]


def test_reference_example_of_the_spec() -> None:
    i = plan()
    assert isinstance(i, OrderIntent)
    assert i.entry.limit_price == D("92.03") and i.entry.type == "limit_post_only" and i.entry.time_in_force_s == 1800
    assert i.bracket.take_profit == D("108.96") and i.bracket.stop_loss == D("83.57")
    assert i.base_size == D("0.023") and not i.risk.min_size_bump
    assert i.risk.cost_tp_bps == 120 and i.risk.cost_sl_bps == 197  # 60+60 and 60+120+1.6+5+10
    assert i.risk.ev_bps == pytest.approx(358, abs=3) and i.risk.p_breakeven == pytest.approx(0.39, abs=0.005)
    # the floor of the decision is the live break-even plus the buffer (0.05): scale_p = (0.52 - 0.4435) / 0.1565
    assert (i.risk.regime, i.risk.m_reg, i.risk.p_be_buffer) == (2, 1.0, 0.05)
    assert i.risk.scale_p == pytest.approx(0.4887, abs=1e-3)
    assert i.risk.risk_eur == pytest.approx(D("0.2443"), abs=D("0.001"))
    assert i.bracket.atr == D("4.2334") and i.bracket.atr_tf == "1d" and i.side == "BUY"


def test_intent_is_deterministic_and_keeps_the_ids() -> None:
    a, b = plan(), plan()
    assert (
        a == b and isinstance(a, OrderIntent) and (a.intent_id, a.decision_id, a.created_at) == ("int-1", "dec-1", NOW)
    )


def test_levels_are_rounded_for_safety() -> None:
    i = plan(quote=MarketQuote(D("92.035"), D("92.07")))
    assert isinstance(i, OrderIntent)
    assert i.entry.limit_price == D("92.03")  # floor: a post-only buy must not cross
    assert i.bracket.stop_loss >= D("92.03") - 2 * D("4.2334")  # ceil: the stop is never further than planned
    assert i.bracket.take_profit <= D("92.03") + 4 * D("4.2334")  # floor: the target is never beyond the plan
    assert i.bracket.stop_loss < i.entry.limit_price < i.bracket.take_profit


def test_taker_entry_is_marketable_and_rounded_up() -> None:
    rm = manager(label=LabelConfig(atr_tf="1d", k_tp=4.0, k_sl=2.0, entry_mode="taker", horizon_min=14400))
    i = plan(rm, quote=MarketQuote(D("92.03"), D("92.064")))
    assert isinstance(i, OrderIntent)
    assert i.entry.type == "limit_marketable" and i.entry.limit_price == D("92.07") and i.entry.time_in_force_s == 20


def test_barriers_come_from_the_label_config() -> None:
    lab = LabelConfig(atr_tf="6h", k_tp=3.0, k_sl=2.0, entry_mode="maker", horizon_min=21600)
    i = plan(manager(label=lab), atr=D("1.10"), fees=REAL, decision=buy(p_edge=0.55), p_be_buffer=0.0)
    assert isinstance(i, OrderIntent)
    assert (i.bracket.k_tp, i.bracket.k_sl, i.bracket.atr_tf) == (3.0, 2.0, "6h")
    assert i.bracket.take_profit == D("95.33") and i.bracket.stop_loss == D("89.83")


# ------------------------------------------------------------------------------------------ gates


def test_fee_floor_skip() -> None:
    s = plan(atr=D("0.15"))  # TP ~ 65 bps, below cost_tp + 30
    assert isinstance(s, Skip) and s.reason == "fee_floor"


def test_ev_gate_skip_below_breakeven() -> None:
    s = plan(decision=buy(p_edge=0.35))
    assert isinstance(s, Skip) and s.reason == "ev_negative"
    assert isinstance(plan(decision=buy(p_edge=0.45)), OrderIntent)  # p_breakeven is ~0.39 here


def test_ev_gate_is_strict() -> None:
    be = 0.0
    i = plan()
    assert isinstance(i, OrderIntent) and i.risk.p_breakeven is not None
    be = i.risk.p_breakeven
    s = plan(decision=buy(p_edge=be - 0.001))
    assert isinstance(s, Skip) and s.reason == "ev_negative"


def test_real_fees_make_the_same_trade_cheaper() -> None:
    cheap, dear = plan(fees=REAL), plan(fees=ASSUMED)
    assert isinstance(cheap, OrderIntent) and isinstance(dear, OrderIntent)
    assert cheap.risk.cost_tp_bps == 50 and cheap.risk.ev_bps > dear.risk.ev_bps


def test_kill_switch_skip_and_pass() -> None:
    s = plan(manager(gate=Gate("daily_loss")))
    assert isinstance(s, Skip) and s.reason == "kill_switch" and s.detail == "daily_loss"
    assert isinstance(plan(manager(gate=Gate(None))), OrderIntent)


def test_insufficient_balance_and_below_min_size() -> None:
    s = plan(free_quote=D("0.5"))
    assert isinstance(s, Skip) and s.reason == "insufficient_balance"
    t = plan(equity=D("20"), free_quote=D("20"), decision=buy(p_edge=0.45))
    assert isinstance(t, Skip) and t.reason == "below_min_size"


def test_invalid_quote_and_levels_are_skips() -> None:
    assert plan(quote=MarketQuote(D("92.10"), D("92.00"))) == Skip(  # crossed book
        reason="no_quote", detail="bid=92.10 ask=92.00 atr=4.2334"
    )
    s = plan(atr=D("50"))  # the stop would be below zero
    assert isinstance(s, Skip) and s.reason == "invalid_levels"
    z = plan(atr=D("0"))
    assert isinstance(z, Skip) and z.reason == "no_quote"


# --------------------------------------------------------------------------------------- contract


@pytest.mark.parametrize("regime", [0, 4])
def test_a_regime_the_policy_should_have_vetoed_is_a_programming_error(regime: int) -> None:
    with pytest.raises(PolicyContractError):
        plan(decision=buy(regime=regime))


def test_only_buy_on_flat_reaches_plan_entry() -> None:
    with pytest.raises(PolicyContractError):
        plan(decision=Decision("HOLD", "below_threshold", p_edge=0.5, regime=2))
    with pytest.raises(PolicyContractError):
        plan(position=PositionView(book="champion", product_id="SOL-EUR", side="LONG", base_qty=D("1")))
    with pytest.raises(PolicyContractError):
        plan(decision=Decision("BUY", "x", p_edge=None, regime=None))


def test_regime_multiplier_changes_only_the_size() -> None:
    rm = manager(regime_multipliers={1: 0.75, 2: 1.0, 3: 0.6})  # the default is 1.0 for the three entry regimes
    normal, high = plan(rm, decision=buy(p_edge=0.60, regime=2)), plan(rm, decision=buy(p_edge=0.60, regime=3))
    assert isinstance(normal, OrderIntent) and isinstance(high, OrderIntent)
    assert high.base_size < normal.base_size
    assert (high.bracket.take_profit, high.bracket.stop_loss) == (normal.bracket.take_profit, normal.bracket.stop_loss)


# ----------------------------------------------------------------------------------------- exit


def test_plan_exit_cancels_the_bracket_and_sells_the_position() -> None:
    pos = PositionView(book="champion", product_id="SOL-EUR", side="LONG", base_qty=D("0.024"), bracket_order_id="b-9")
    e = manager().plan_exit(
        decision=Decision("SELL", "thresholds_met"), decision_id="dec-2", intent_id="int-2", now=NOW, position=pos
    )
    assert (e.base_size, e.cancel_bracket_order_id, e.product_id, e.reason) == (
        D("0.024"), "b-9", "SOL-EUR", "thresholds_met",
    )  # fmt: skip
    with pytest.raises(PolicyContractError):
        manager().plan_exit(decision=Decision("SELL", "x"), decision_id="d", intent_id="i", now=NOW, position=FLAT)


# ----------------------------------------------------------------------------------------- snapshot


def test_intent_json_snapshot() -> None:
    i = plan()
    assert isinstance(i, OrderIntent)
    path = GOLDEN / "intent_sol_reference.json"
    assert i.model_dump_json() == path.read_text(encoding="utf-8")


# ----------------------------------------------------------------------------------- p_be_buffer (P1c)


def break_even() -> float:
    i = plan(p_be_buffer=0.0)
    assert isinstance(i, OrderIntent) and i.risk.p_breakeven is not None
    return i.risk.p_breakeven


def test_ev_buffer_gate() -> None:
    be, buf = break_even(), 0.04
    s = plan(decision=buy(p_edge=be + buf - 0.001), p_be_buffer=buf)
    assert isinstance(s, Skip) and s.reason == "ev_buffer"
    assert isinstance(plan(decision=buy(p_edge=be + buf + 0.001), p_be_buffer=buf), OrderIntent)
    # buffer 0: the EV gate of before (strict at the break-even)
    assert isinstance(plan(decision=buy(p_edge=be + 0.001), p_be_buffer=0.0), OrderIntent)
    s0 = plan(decision=buy(p_edge=be - 0.001), p_be_buffer=0.0)
    assert isinstance(s0, Skip) and s0.reason == "ev_negative"


def test_negative_ev_is_reported_before_the_buffer() -> None:
    s = plan(decision=buy(p_edge=0.30), p_be_buffer=0.08)
    assert isinstance(s, Skip) and s.reason == "ev_negative"


def test_scale_p_uses_the_decision_floor() -> None:
    be = break_even()
    lo, hi = plan(decision=buy(p_edge=be + 0.05 + 0.02), p_be_buffer=0.05), plan(
        decision=buy(p_edge=be + 0.05 + 0.08), p_be_buffer=0.05
    )
    assert isinstance(lo, OrderIntent) and isinstance(hi, OrderIntent)
    full = 0.60 - (be + 0.05)
    assert lo.risk.scale_p == pytest.approx(0.02 / full, abs=2e-3)
    assert hi.risk.scale_p == pytest.approx(0.08 / full, abs=2e-3) and hi.risk.scale_p > lo.risk.scale_p


def test_floor_above_p_edge_full_gives_full_scale() -> None:
    be = break_even()
    i = plan(manager(p_edge_full=0.45), decision=buy(p_edge=0.70), p_be_buffer=0.10)  # floor = be + 0.10 > 0.45
    assert be + 0.10 > 0.45 and isinstance(i, OrderIntent) and i.risk.scale_p == 1.0


def test_entry_regimes_need_multipliers() -> None:
    with pytest.raises(ValueError, match="entry regimes"):
        manager(regime_multipliers={1: 1.0, 2: 1.0})
    assert manager(regime_multipliers={1: 1.0, 2: 1.0, 3: 1.0})
