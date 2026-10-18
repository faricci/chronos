"""Position sizing (spec sez. 4.1.2): the reference example, the risk bound and the coverage cap."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from modules.config import FallbackFees
from modules.execution.balance_guard import InsufficientCoverage
from modules.risk.costs import FeeRates
from modules.risk.sizing import SizeSkip, SizingResult, compute_size
from tests.coverage_fixtures import SOL, buy, make_guard

D = Decimal
ASSUMED = FeeRates(maker_bps=60, taker_bps=120, assumed=True)
FALLBACK = FallbackFees()


def size_of(**kw: object) -> SizingResult | SizeSkip:
    base: dict[str, object] = {
        "equity": D("100"), "free_quote": D("100"), "entry": D("92.03"), "stop_loss": D("83.57"),
        "cost_sl_bps": 197.0, "p_edge": 0.52, "p_edge_min": 0.44, "p_edge_full": 0.60, "m_reg": 1.0,
        "risk_frac": 0.005, "max_position_frac": 0.20, "max_order_quote": D("50"), "meta": SOL, "fees": ASSUMED,
        "fallback_fees": FALLBACK,
    }  # fmt: skip
    return compute_size(**{**base, **kw})  # type: ignore[arg-type]


def test_reference_example_of_the_spec() -> None:
    r = size_of()
    assert isinstance(r, SizingResult)
    assert r.scale_p == D("0.5") and r.risk_eur == D("0.25")
    assert r.loss_per_unit == pytest.approx(D("10.27"), abs=D("0.01"))  # 8.46 + 1.81 EUR per SOL
    assert r.base_size == D("0.024") and not r.min_size_bump
    assert r.base_size * D("92.03") == pytest.approx(D("2.21"), abs=D("0.01"))


def test_scale_is_clipped_to_0_1() -> None:
    top = size_of(p_edge=0.90)
    assert isinstance(top, SizingResult) and top.scale_p == 1 and top.risk_eur == D("0.5")
    low = size_of(p_edge=0.30)  # below p_edge_min: zero conviction, the min-size bump still fits the budget
    assert isinstance(low, SizingResult) and low.scale_p == 0 and low.risk_eur == 0 and low.min_size_bump


def test_regime_multiplier_scales_the_risk() -> None:
    normal, high = size_of(p_edge=0.60), size_of(p_edge=0.60, m_reg=0.6)
    assert isinstance(normal, SizingResult) and isinstance(high, SizingResult)
    assert high.risk_eur == normal.risk_eur * D("0.6") and high.base_size < normal.base_size


def test_notional_cap_binds_before_the_risk() -> None:
    r = size_of(p_edge=0.60, risk_frac=0.1, max_position_frac=0.05, equity=D("1000"), free_quote=D("1000"))
    assert isinstance(r, SizingResult)
    assert r.base_size * D("92.03") <= D("50.01")  # equity * 5% = 50
    assert r.base_size == r.size_cap


def test_absolute_order_cap_binds() -> None:
    r = size_of(p_edge=0.60, risk_frac=0.1, max_position_frac=0.2, equity=D("100000"), free_quote=D("100000"))
    assert isinstance(r, SizingResult) and r.base_size * D("92.03") <= D("50")


def test_coverage_cap_binds_when_the_free_quote_is_small() -> None:
    r = size_of(p_edge=0.60, risk_frac=0.1, equity=D("100"), free_quote=D("3"))
    assert isinstance(r, SizingResult) and r.base_size * D("92.03") <= D("3")


def test_min_size_bump_only_inside_the_risk_budget() -> None:
    # tiny conviction and tiny equity: min size (1 EUR) loses more than the per-trade budget -> skip
    s = size_of(equity=D("20"), free_quote=D("20"), p_edge=0.46)
    assert isinstance(s, SizeSkip) and s.reason == "below_min_size"
    ok = size_of(equity=D("1000"), free_quote=D("1000"), p_edge=0.46)
    assert isinstance(ok, SizingResult) and ok.base_size > 0


def test_not_enough_free_quote_is_reported_as_insufficient_balance() -> None:
    s = size_of(free_quote=D("0.5"))
    assert isinstance(s, SizeSkip) and s.reason == "insufficient_balance"
    assert isinstance(size_of(free_quote=D("0")), SizeSkip)


def test_invalid_levels_are_programming_errors() -> None:
    with pytest.raises(ValueError):
        size_of(stop_loss=D("92.03"))
    with pytest.raises(ValueError):
        size_of(p_edge_full=0.44)


GEN = dict(
    equity=st.decimals(min_value="10", max_value="5000", places=2),
    free_frac=st.floats(min_value=0.0, max_value=1.5),
    entry=st.decimals(min_value="5", max_value="500", places=2),
    sl_frac=st.floats(min_value=0.02, max_value=0.4),
    cost_sl=st.floats(min_value=20, max_value=300),
    p_edge=st.floats(min_value=0.0, max_value=1.0),
    m_reg=st.sampled_from([0.6, 0.75, 1.0]),
    risk_frac=st.floats(min_value=0.001, max_value=0.02),
)


@settings(max_examples=300, deadline=None)
@given(**GEN)
def test_loss_at_stop_never_exceeds_the_risk(equity, free_frac, entry, sl_frac, cost_sl, p_edge, m_reg, risk_frac):  # type: ignore[no-untyped-def]
    stop = (entry * D(str(1 - sl_frac))).quantize(D("0.01"))
    if not 0 < stop < entry:
        return
    r = size_of(equity=equity, free_quote=equity * D(str(free_frac)), entry=entry, stop_loss=stop, cost_sl_bps=cost_sl,
                p_edge=p_edge, m_reg=m_reg, risk_frac=risk_frac)  # fmt: skip
    if isinstance(r, SizeSkip):
        return
    loss = r.base_size * r.loss_per_unit
    budget = equity * D(str(risk_frac))
    # floor_to(size) only ever lowers the risk; the bump is allowed up to the per-trade budget
    assert loss <= (budget if r.min_size_bump else r.risk_eur) + D("1e-9")
    assert r.base_size % SOL.base_increment == 0 and r.base_size >= SOL.base_min_size


@settings(max_examples=300, deadline=None)
@given(**GEN)
def test_sizing_never_refused_by_guard(equity, free_frac, entry, sl_frac, cost_sl, p_edge, m_reg, risk_frac):  # type: ignore[no-untyped-def]
    """T4: whatever the sizing returns, the guard accepts it for coverage reasons (it is the final authority)."""
    stop = (entry * D(str(1 - sl_frac))).quantize(D("0.01"))
    if not 0 < stop < entry:
        return
    free = (equity * D(str(free_frac))).quantize(D("0.01"))
    r = size_of(equity=equity, free_quote=free, entry=entry, stop_loss=stop, cost_sl_bps=cost_sl, p_edge=p_edge,
                m_reg=m_reg, risk_frac=risk_frac)  # fmt: skip
    if isinstance(r, SizeSkip):
        return
    guard, _book, *_ = make_guard(quote=str(free))
    spec = buy(size=str(r.base_size), price=str(entry))
    try:
        ticket = guard.authorize(spec)
    except InsufficientCoverage as e:  # pragma: no cover - the failure this test exists to catch
        pytest.fail(f"guard refused a size the sizing allowed: {e}")
    assert ticket is not None
