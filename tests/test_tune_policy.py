"""Policy tuning: a small grid anchored on the validation answers, every combination through the full backtest."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from modules.decision.baseline import AnswerArrays
from modules.decision.policy import REGIME_RULE
from modules.features.serializer import FLAT_COLUMNS
from tests.indicator_fixtures import synthetic_bars
from tests.test_backtest import LABEL, SPAN, StubDecider, config, registry
from training.backtest import FlatProbCache, LongProbCache, Span, build_market
from training.baseline_lgbm import CALIBRATION_SCHEME
from training.tune_policy import (
    C_MIN_GRID,
    MAX_COMBINATIONS,
    NO_CANDIDATES_REGIME,
    OK,
    P_BE_BUFFER_GRID,
    TAU_SELL,
    UNTUNABLE_CALIBRATION,
    UNTUNABLE_EV,
    UNTUNABLE_PNL,
    ModelSchemeError,
    TuneResult,
    check_scheme,
    classify,
    grid,
    probe_registry,
    tune,
    val_diagnostics,
)

EARLY = Span(pd.Timestamp("2024-02-01", tz="UTC"), pd.Timestamp("2024-02-20", tz="UTC"), None, 0)  # no regime token yet


class VariedStub(StubDecider):
    """Like the stub but with a spread of BUY probabilities, so that thresholds actually select something."""

    def answers_batch(self, x: np.ndarray) -> AnswerArrays:
        base = super().answers_batch(x)
        wobble = 0.5 + 0.45 * np.sin(np.arange(len(x)) / 37.0)  # in [0.05, 0.95]
        pa = np.column_stack([wobble * 0.9, np.full(len(x), 0.02), 1 - wobble * 0.9 - 0.02])
        return AnswerArrays(pa, 0.3 + 0.6 * wobble, base.p_regime)


class FixedEdge(StubDecider):
    def __init__(self, p: float) -> None:
        super().__init__()
        self.p = p

    def answers_batch(self, x: np.ndarray) -> AnswerArrays:
        a = super().answers_batch(x)
        return AnswerArrays(a.p_action, np.full(len(x), self.p), a.p_regime)


@pytest.fixture(scope="module")
def markets() -> dict[str, object]:
    btc = synthetic_bars(days=150, seed=61, zero_volume_day=None, constant_days=None)
    sol = synthetic_bars(days=150, seed=62, zero_volume_day=None, constant_days=None)
    bm = build_market("BTC-EUR", btc, LABEL)
    return {"BTC-EUR": bm, "SOL-EUR": build_market("SOL-EUR", sol, LABEL, bm.ind)}


@pytest.fixture(scope="module")
async def tuned(markets):  # type: ignore[no-untyped-def]
    return await tune(VariedStub(), markets, LABEL, SPAN, config(), registry(), FlatProbCache(), LongProbCache(),
                      "m@1", "val_fold_0", min_trades=3)  # fmt: skip


# ----------------------------------------------------------------------------------------------- grid


def test_grid_is_at_most_20_without_regime_dimension(markets) -> None:  # type: ignore[no-untyped-def]
    combos = grid(VariedStub(), markets, LABEL, SPAN, FlatProbCache(), 15)
    assert 0 < len(combos) <= MAX_COMBINATIONS == 20
    for c in combos:
        assert set(c) == {"tau_buy", "c_min", "p_be_buffer", "tau_sell", "regime_rule"}  # no p_edge_min, no regimes
    taus = sorted({c["tau_buy"] for c in combos})
    assert 1 <= len(taus) <= 2 and taus[0] >= 0.0 and taus[-1] <= 1.0


def test_buffer_grid_and_fixed_tau_sell(markets) -> None:  # type: ignore[no-untyped-def]
    combos = grid(VariedStub(), markets, LABEL, SPAN, FlatProbCache(), 15)
    assert {c["p_be_buffer"] for c in combos} == set(P_BE_BUFFER_GRID) == {0.0, 0.02, 0.04, 0.06, 0.08}
    assert {c["c_min"] for c in combos} == set(C_MIN_GRID)
    assert {c["tau_sell"] for c in combos} == {TAU_SELL} == {0.5}
    assert {c["regime_rule"] for c in combos} == {REGIME_RULE}


def test_the_grid_is_empty_when_no_decision_allows_an_entry(markets) -> None:  # type: ignore[no-untyped-def]
    assert grid(VariedStub(), markets, LABEL, EARLY, FlatProbCache(), 15) == []  # the regime token does not exist yet


# ------------------------------------------------------------------------------------------ selection


async def test_selection_requires_positive_pnl_and_enough_trades(tuned) -> None:  # type: ignore[no-untyped-def]
    ok = tuned.table[(tuned.table["n_trades"] >= 3) & (tuned.table["pnl_eur"] > 0)]
    if ok.empty:
        assert tuned.policy is None and tuned.reason == UNTUNABLE_PNL
        return
    assert tuned.reason == OK and tuned.policy is not None
    best = ok["pnl_eur"].max()
    assert tuned.policy.tuned_on["pnl_eur"] == pytest.approx(best) and tuned.policy.tuned_on["pnl_eur"] > 0
    assert tuned.policy.label_config_sha256 == LABEL.sha256 and tuned.policy.tuned_on["min_trades"] == 3
    assert tuned.policy.tau_sell == 0.5 and tuned.policy.regime_rule == REGIME_RULE


async def test_ties_go_to_the_most_prudent_combination(tuned) -> None:  # type: ignore[no-untyped-def]
    ok = tuned.table[(tuned.table["n_trades"] >= 3) & (tuned.table["pnl_eur"] > 0)]
    if ok.empty:
        pytest.skip("no profitable combination on the synthetic data")
    top = ok[np.isclose(ok["pnl_eur"], ok["pnl_eur"].max())].sort_values(
        ["p_be_buffer", "tau_buy", "c_min"], ascending=False
    )
    row, pol = top.iloc[0], tuned.policy
    assert (pol.p_be_buffer, pol.tau_buy, pol.c_min) == (row["p_be_buffer"], row["tau_buy"], row["c_min"])


def _fake_backtest(monkeypatch, rule):  # type: ignore[no-untyped-def]
    """Replace the backtest by a function of the thresholds: ``rule(policy) -> (n_trades, total_pnl)``."""
    import types

    async def fake(markets, decider, pol, *args):  # type: ignore[no-untyped-def]
        n, pnl = rule(pol)
        trades = pd.DataFrame({"pnl_eur": [pnl / n] * n}) if n else pd.DataFrame({"pnl_eur": []})
        return types.SimpleNamespace(trades=trades)

    monkeypatch.setattr("training.tune_policy.run_backtest", fake)


async def _tune_with(markets, **kw):  # type: ignore[no-untyped-def]
    return await tune(VariedStub(), markets, LABEL, SPAN, config(), registry(), FlatProbCache(), LongProbCache(),
                      "m@1", "val_fold_0", **kw)  # fmt: skip


async def test_selection_logic_with_known_pnls(markets, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # only the combinations with buffer 0.04 have 40 trades and a profit; buffer 0.08 has the best profit but 10 trades
    def rule(p):  # type: ignore[no-untyped-def]
        if p.p_be_buffer == 0.08:
            return 10, 9.0
        return (40, 2.0 + p.tau_buy) if p.p_be_buffer == 0.04 else (40, -1.0)

    _fake_backtest(monkeypatch, rule)
    res = await _tune_with(markets)
    assert res.reason == OK and res.policy is not None and res.policy.p_be_buffer == 0.04
    assert res.policy.tau_buy == res.table["tau_buy"].max()  # the best of the eligible, never the 10-trade one
    assert res.policy.tuned_on["n_trades"] == 40
    assert res.policy.tuned_on["pnl_eur"] == pytest.approx(2.0 + res.policy.tau_buy)


async def test_a_loss_making_validation_leaves_the_fold_flat(markets, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _fake_backtest(monkeypatch, lambda p: (80, -0.01))  # plenty of trades, all slightly negative
    res = await _tune_with(markets)
    assert res.policy is None and res.reason == UNTUNABLE_PNL and (res.table["n_trades"] == 80).all()
    _fake_backtest(monkeypatch, lambda p: (80, 0.0))  # a zero PnL is not a profit either
    assert (await _tune_with(markets)).policy is None


async def test_ties_are_broken_by_buffer_then_tau_then_c_min(markets, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _fake_backtest(monkeypatch, lambda p: (40, 3.0))  # every combination ties
    res = await _tune_with(markets)
    top = res.table.sort_values(["p_be_buffer", "tau_buy", "c_min"], ascending=False).iloc[0]
    assert res.policy is not None
    got = (res.policy.p_be_buffer, res.policy.tau_buy, res.policy.c_min)
    assert got == (top["p_be_buffer"], top["tau_buy"], top["c_min"])
    assert res.policy.p_be_buffer == 0.08 and res.policy.c_min == 0.10


async def test_a_fold_without_enough_trades_is_untunable(markets) -> None:  # type: ignore[no-untyped-def]
    res = await tune(VariedStub(), markets, LABEL, SPAN, config(), registry(), FlatProbCache(), LongProbCache(), "m@1",
                     "val_fold_0", min_trades=10_000)  # fmt: skip
    assert res.reason == UNTUNABLE_PNL and res.policy is None and len(res.table) > 0


async def test_no_decision_with_an_allowed_regime_is_reported(markets) -> None:  # type: ignore[no-untyped-def]
    res = await tune(VariedStub(), markets, LABEL, EARLY, config(), registry(), FlatProbCache(), LongProbCache(),
                     "m@1", "val_fold_0")  # fmt: skip
    assert res.reason == NO_CANDIDATES_REGIME and res.policy is None


# ----------------------------------------------------------------------------- reasons and diagnostics


def test_untunable_reasons_order() -> None:
    empty = pd.DataFrame(columns=["n_trades", "pnl_eur"])
    failed = TuneResult(None, empty, UNTUNABLE_PNL)
    chosen = TuneResult(None, empty, OK)
    assert classify(chosen, {"ev_headroom_q95": -1.0}) == OK  # a chosen policy is never re-labelled
    assert classify(failed, {"n_rows_regime_ok": 0, "ev_headroom_q95": -1.0}) == NO_CANDIDATES_REGIME  # 3 before 4
    assert classify(failed, {"n_rows_regime_ok": 50, "ev_headroom_q95": -0.01}) == UNTUNABLE_EV  # 4 before 5
    assert classify(failed, {"n_rows_regime_ok": 50, "ev_headroom_q95": 0.02}) == UNTUNABLE_PNL
    assert classify(failed, {"n_rows_regime_ok": 50, "ev_headroom_q95": None}) == UNTUNABLE_PNL
    assert classify(TuneResult(None, empty, UNTUNABLE_CALIBRATION), {}) == UNTUNABLE_CALIBRATION


def test_old_calibration_scheme_is_refused() -> None:
    check_scheme({"calibration_scheme": CALIBRATION_SCHEME})
    with pytest.raises(ModelSchemeError):
        check_scheme({"calibration_scheme": "PLATT_ON_VALIDATION"})
    with pytest.raises(ModelSchemeError):
        check_scheme({})  # a model card from before the scheme existed


def _samples(n: int, p_be: float) -> tuple[pd.DataFrame, np.ndarray]:
    rng = np.random.default_rng(5)
    x = rng.normal(size=(n, len(FLAT_COLUMNS)))
    x[:, FLAT_COLUMNS.index("atr_1d_pctile_365d")] = rng.uniform(0, 100, n)
    x[:, FLAT_COLUMNS.index("p_breakeven")] = p_be
    df = pd.DataFrame(x, columns=list(FLAT_COLUMNS))
    df["position_side"] = "FLAT"
    df["edge_target"] = (rng.uniform(size=n) < 0.30).astype(float)
    df["w_edge"] = 1.0
    return df, np.full(n, "val", dtype=object)


def test_val_diagnostics_separate_a_bias_from_a_missing_edge() -> None:
    df, member = _samples(4000, p_be=0.40)
    d = val_diagnostics(FixedEdge(0.22), df, member, rho=0.5, n_products=3)  # type: ignore[arg-type]
    assert d["n_rows"] == 4000 and 0.85 < d["n_rows_regime_ok"] / 4000 < 0.95  # entries allowed in the 5..95 band
    assert d["mean_p_edge"] == pytest.approx(0.22) and d["p_hist_obs"] == pytest.approx(0.30, abs=0.03)
    assert d["bias_z"] < -2  # the model is below what happened: a calibration problem, not a missing edge
    assert d["ev_headroom_q95"] == pytest.approx(0.22 - 0.40)  # even the best row is below the break-even
    assert d["n_eff"] == pytest.approx(4000 / (1 + 2 * 0.5))
    assert sum(v for k, v in d["regime_share"].items() if k != "missing") == pytest.approx(1.0)


def test_val_diagnostics_ignore_other_members_and_empty_sets() -> None:
    df, member = _samples(100, p_be=0.40)
    member[:60] = "train"
    assert val_diagnostics(FixedEdge(0.5), df, member, 0.5, 3)["n_rows"] == 40  # type: ignore[arg-type]
    assert val_diagnostics(FixedEdge(0.5), df, np.full(100, "", dtype=object), 0.5, 3) == {"n_rows": 0}  # type: ignore[arg-type]


# ------------------------------------------------------------------------------------------- the rest


def test_probe_registry_reads_the_product_facts_of_the_probe(tmp_path: Path) -> None:
    meta = {"product_type": "SPOT", "product_venue": "CBE", "base_currency_id": "SOL", "quote_currency_id": "EUR",
            "base_increment": "0.001", "quote_increment": "0.01", "base_min_size": "0.001", "quote_min_size": "1",
            "trading_disabled": False, "cancel_only": False, "view_only": False,
            "future_product_details": None}  # fmt: skip
    path = tmp_path / "probe.json"
    path.write_text(json.dumps({"products": {"SOL-EUR": {"meta": meta}}}))
    reg = probe_registry(path, ["SOL-EUR"])
    m = reg.meta("SOL-EUR")
    assert (m.base_currency, m.quote_increment, m.base_min_size) == ("SOL", Decimal("0.01"), Decimal("0.001"))


def test_the_real_probe_is_a_spot_registry() -> None:
    real = Path("data/probe/coinbase_probe.json")
    if not real.exists():
        pytest.skip("no probe file")
    reg = probe_registry(real, ["BTC-EUR", "ETH-EUR", "SOL-EUR"])
    assert reg.product_ids == frozenset({"BTC-EUR", "ETH-EUR", "SOL-EUR"})


def test_tuning_cadence_is_15_minutes() -> None:
    assert config().decision_every_min == 15
