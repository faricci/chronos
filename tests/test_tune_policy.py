"""Policy tuning: the grid is anchored on the validation answers, every combination goes through the full backtest."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from modules.decision.baseline import AnswerArrays
from tests.indicator_fixtures import synthetic_bars
from tests.test_backtest import LABEL, SPAN, StubDecider, config, registry
from training.backtest import FlatProbCache, LongProbCache, build_market
from training.tune_policy import C_MIN_GRID, TAU_SELL_GRID, grid, probe_registry, tune


class VariedStub(StubDecider):
    """Like the stub but with a spread of BUY probabilities, so that thresholds actually select something."""

    degenerate_regime = True

    def answers_batch(self, x: np.ndarray) -> AnswerArrays:
        base = super().answers_batch(x)
        wobble = 0.5 + 0.45 * np.sin(np.arange(len(x)) / 37.0)  # in [0.05, 0.95]
        pa = np.column_stack([wobble * 0.9, np.full(len(x), 0.02), 1 - wobble * 0.9 - 0.02])
        return AnswerArrays(pa, 0.3 + 0.6 * wobble, base.p_regime)


@pytest.fixture(scope="module")
def markets() -> dict[str, object]:
    btc = synthetic_bars(days=70, seed=61, zero_volume_day=None, constant_days=None)
    sol = synthetic_bars(days=70, seed=62, zero_volume_day=None, constant_days=None)
    bm = build_market("BTC-EUR", btc, LABEL)
    return {"BTC-EUR": bm, "SOL-EUR": build_market("SOL-EUR", sol, LABEL, bm.ind)}


def test_the_grid_is_anchored_on_the_validation_answers(markets) -> None:  # type: ignore[no-untyped-def]
    combos = grid(VariedStub(), markets, LABEL, SPAN, FlatProbCache(), 15, p_edge_full=0.60)
    assert combos
    taus = sorted({c["tau_buy"] for c in combos})
    assert 2 <= len(taus) <= 5 and taus[0] >= 0.0 and taus[-1] <= 1.0
    assert {c["c_min"] for c in combos} == set(C_MIN_GRID) and {c["tau_sell"] for c in combos} == set(TAU_SELL_GRID)
    assert all(c["allowed_regimes_for_entry"] == (2,) for c in combos)  # a degenerate regime head: only "normal"


def test_p_edge_min_never_reaches_p_edge_full(markets) -> None:  # type: ignore[no-untyped-def]
    combos = grid(VariedStub(), markets, LABEL, SPAN, FlatProbCache(), 15, p_edge_full=0.60)
    assert max(c["p_edge_min"] for c in combos) <= 0.60 - 0.01 + 1e-12  # sizing needs p_edge_min < p_edge_full


async def test_tune_picks_the_best_pnl_among_the_combinations_with_enough_trades(markets) -> None:  # type: ignore[no-untyped-def]
    res = await tune(VariedStub(), markets, LABEL, SPAN, config(), registry(), FlatProbCache(), LongProbCache(), "m@1",
                     "val_fold_0", min_trades=3)  # fmt: skip
    assert res.reason == "ok" and res.policy is not None
    ok = res.table[res.table["n_trades"] >= 3]
    assert len(ok) > 0 and res.policy.tuned_on["n_trades"] == int(ok.loc[ok["pnl_eur"].idxmax(), "n_trades"])
    assert res.policy.label_config_sha256 == LABEL.sha256 and res.policy.tuned_on["min_trades"] == 3
    assert abs(res.policy.tau_buy - ok.loc[ok["pnl_eur"].idxmax(), "tau_buy"]) < 1e-12


async def test_a_fold_without_enough_trades_is_untunable(markets) -> None:  # type: ignore[no-untyped-def]
    res = await tune(VariedStub(), markets, LABEL, SPAN, config(), registry(), FlatProbCache(), LongProbCache(), "m@1",
                     "val_fold_0", min_trades=10_000)  # fmt: skip
    assert res.reason == "untunable" and res.policy is None and len(res.table) > 0


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


def test_tuning_cadence_is_15_minutes(markets) -> None:  # type: ignore[no-untyped-def]
    assert pd.DatetimeIndex(markets["BTC-EUR"].bars.ts_close).minute.isin(range(60)).all()
    assert config().decision_every_min == 15
