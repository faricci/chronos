"""Evidence gate GE: effective trades, block bootstrap, permutation test, concentration and the three-state verdict."""

from __future__ import annotations

import math
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from tests.label_fixtures import loaded_label
from training.backtest import Span
from training.evaluate import (
    Boot,
    Candidates,
    _ge,
    always_invested,
    block_bootstrap,
    buy_and_hold,
    concentration,
    edge_status,
    effective_trades,
    holdout_consistency,
    permutation_pvalue,
)
from training.fold_spans import FoldSpans, SpanSet

T0 = pd.Timestamp("2025-01-01", tz="UTC")


def trades(rows: list[tuple[str, float, float]]) -> pd.DataFrame:
    """(product, fill hour offset, exit hour offset)."""
    return pd.DataFrame(
        {
            "product": [r[0] for r in rows],
            "fill_ts": [T0 + pd.Timedelta(hours=r[1]) for r in rows],
            "exit_ts": [T0 + pd.Timedelta(hours=r[2]) for r in rows],
        }
    )


# -------------------------------------------------------------------------------- effective trades


def test_disjoint_trades_count_one_each() -> None:
    t = trades([("A", 0, 5), ("B", 10, 15), ("C", 20, 25), ("A", 30, 35)])
    assert effective_trades(t, rho=0.6) == pytest.approx(4.0)


def test_three_overlapping_products_count_three_over_one_plus_two_rho() -> None:
    t = trades([("A", 0, 10), ("B", 2, 12), ("C", 4, 14)])
    assert effective_trades(t, rho=0.5) == pytest.approx(3 / (1 + 2 * 0.5))
    assert effective_trades(t, rho=0.0) == pytest.approx(3.0)
    assert effective_trades(t, rho=1.0) == pytest.approx(1.0)


def test_overlap_of_the_same_product_is_ignored_and_empty_is_zero() -> None:
    t = trades([("A", 0, 10), ("A", 5, 15)])
    assert effective_trades(t, rho=1.0) == pytest.approx(2.0)
    assert effective_trades(trades([]), rho=0.5) == 0.0


def test_partial_overlap_counts_only_the_trades_that_touch() -> None:
    t = trades([("A", 0, 10), ("B", 8, 20), ("C", 30, 40)])
    assert effective_trades(t, rho=1.0) == pytest.approx(0.5 + 0.5 + 1.0)


# ------------------------------------------------------------------------------------- bootstrap


def test_constant_values_have_zero_se_and_infinite_t() -> None:
    ts = np.array([T0 + pd.Timedelta(days=d) for d in range(0, 200, 2)], dtype="datetime64[ns]")
    b = block_bootstrap(np.full(len(ts), 25.0), ts, T0, T0 + pd.Timedelta(days=200), pd.Timedelta(days=16), 2000, 1)
    assert b.mean == 25.0 and b.se == 0.0 and b.t == math.inf and b.q025 == 25.0 == b.q975


def test_bootstrap_is_deterministic_at_fixed_seed() -> None:
    rng = np.random.default_rng(0)
    ts = np.array([T0 + pd.Timedelta(hours=int(h)) for h in rng.integers(0, 24 * 300, 400)], dtype="datetime64[ns]")
    v = rng.normal(20, 100, 400)
    args = (v, ts, T0, T0 + pd.Timedelta(days=300), pd.Timedelta(days=16), 3000)
    a, b = block_bootstrap(*args, 7), block_bootstrap(*args, 7)
    assert a == b and block_bootstrap(*args, 8).q025 != a.q025


def test_bootstrap_interval_covers_the_true_mean_and_excludes_zero_for_a_real_edge() -> None:
    rng = np.random.default_rng(1)
    ts = np.array([T0 + pd.Timedelta(hours=int(h)) for h in rng.integers(0, 24 * 600, 1500)], dtype="datetime64[ns]")
    edge = block_bootstrap(
        rng.normal(60, 200, 1500), ts, T0, T0 + pd.Timedelta(days=600), pd.Timedelta(days=16), 4000, 3
    )
    null = block_bootstrap(
        rng.normal(0, 200, 1500), ts, T0, T0 + pd.Timedelta(days=600), pd.Timedelta(days=16), 4000, 3
    )
    assert edge.q025 > 0 and edge.t > 2
    assert null.q025 < 0 < null.q975


def test_a_trade_near_the_end_of_a_circular_span_is_drawn() -> None:
    """Blocks wrap around: a lone trade on the last day is picked up by blocks starting just before the end."""
    ts = np.array([T0 + pd.Timedelta(days=99, hours=23)], dtype="datetime64[ns]")
    b = block_bootstrap(np.array([5.0]), ts, T0, T0 + pd.Timedelta(days=100), pd.Timedelta(days=16), 500, 2)
    assert b.n == 1 and b.mean == 5.0 and b.se == 0.0  # every non-empty replicate holds that one trade


def test_bootstrap_without_trades_is_nan() -> None:
    b = block_bootstrap(
        np.empty(0), np.empty(0, dtype="datetime64[ns]"), T0, T0 + pd.Timedelta(days=10), pd.Timedelta(days=2), 10, 1
    )
    assert b.n == 0 and math.isnan(b.mean)


# ----------------------------------------------------------------------------------- permutation


def cands(n: int, label: float, spacing: int = 100, width: int = 10) -> Candidates:
    t = np.arange(n, dtype=np.int64) * spacing
    return Candidates(t, t + width, np.full(n, label))


def test_a_model_that_picked_the_best_entries_gets_the_smallest_possible_p() -> None:
    labels = np.concatenate([np.full(50, 500.0), np.full(450, -100.0)])
    t = np.arange(500, dtype=np.int64) * 100
    c = {(0, "A"): Candidates(t, t + 10, labels)}
    res = permutation_pvalue(np.full(10, 500.0), {(0, "A"): 10}, c, 2000, 5)
    assert res.p <= 1 / 1500  # no random draw of 10 entries is all of the 50 best


def test_a_model_that_picked_at_random_gets_a_p_near_one_half() -> None:
    rng = np.random.default_rng(2)
    labels = rng.normal(0, 100, 600)
    t = np.arange(600, dtype=np.int64) * 100
    c = {(0, "A"): Candidates(t, t + 10, labels)}
    ps = []
    for k in range(20):
        pick = np.random.default_rng(100 + k).choice(600, size=30, replace=False)
        ps.append(permutation_pvalue(labels[pick], {(0, "A"): 30}, c, 600, 9).p)
    assert 0.35 < float(np.mean(ps)) < 0.65


def test_random_entries_never_overlap_and_a_shortfall_is_reported_not_fatal() -> None:
    c = {(0, "A"): Candidates(np.array([0, 5, 10], dtype=np.int64), np.array([100, 100, 100], dtype=np.int64),
                              np.array([1.0, 2.0, 3.0]))}  # fmt: skip
    res = permutation_pvalue(np.array([2.0, 2.0]), {(0, "A"): 2}, c, 200, 4)
    assert res.shortfall > 0 and not math.isnan(res.p)  # only one non-overlapping entry can be placed


def test_permutation_uses_the_fold_and_product_of_each_trade() -> None:
    cs = {(0, "A"): cands(50, 10.0), (1, "A"): cands(50, -10.0)}
    res = permutation_pvalue(np.array([10.0, -10.0]), {(0, "A"): 1, (1, "A"): 1}, cs, 500, 3)
    assert res.p == pytest.approx(1.0)  # the model took exactly the label of its fold: no null draw can differ
    assert math.isnan(permutation_pvalue(np.empty(0), {}, cs, 10, 1).p)


# ------------------------------------------------------------------------- concentration / z


def test_concentration() -> None:
    assert concentration({0: 10.0, 1: 10.0, 2: 10.0, 3: 10.0, 4: 10.0}) == (0.2, 5)
    share, n_pos = concentration({0: 80.0, 1: 10.0, 2: 10.0, 3: -5.0})
    assert share == pytest.approx(80 / 95) and n_pos == 3
    assert concentration({0: 5.0, 1: -10.0}) == (math.inf, 1)


def test_holdout_consistency_z() -> None:
    h, o = Boot(30.0, 20.0, 0, 0, 0, 10), Boot(50.0, 15.0, 0, 0, 0, 100)
    assert holdout_consistency(h, o) == pytest.approx(-20 / 25)
    assert holdout_consistency(Boot(1.0, 0.0, 0, 0, 0, 1), Boot(1.0, 0.0, 0, 0, 0, 1)) == math.inf


# ----------------------------------------------------------------------------------- verdict


def chk(**kw: bool | None) -> dict[str, bool | None]:
    base: dict[str, bool | None] = {"E1": True, "E2": True, "E3": True, "E4": True, "E5": True}
    base.update(kw)
    return base


def test_the_six_ordered_rules() -> None:
    assert edge_status(False, chk(), 50.0, 3.0).status == "NO-GO"  # 1. regime change beats everything
    assert edge_status(True, chk(E1=False), 5.0, 0.5).status == "INCONCLUSO"  # 2. too few trades, positive mean
    assert edge_status(True, chk(E1=False), -5.0, -0.5).status == "NO-GO"  # 2. ... negative mean
    assert edge_status(True, chk(), 50.0, 3.0).status == "GO"  # 3.
    assert edge_status(True, chk(E2=False), 20.0, 1.5).status == "INCONCLUSO"  # 4. significance short, t >= 1
    assert edge_status(True, chk(E2=False, E5=None), 20.0, 1.5).status == "INCONCLUSO"  # 4. holdout not evaluable
    assert edge_status(True, chk(E5=None), 50.0, 3.0).status == "INCONCLUSO"  # 5.
    assert edge_status(True, chk(E2=False), 20.0, 0.5).status == "NO-GO"  # 6. t below 1
    assert edge_status(True, chk(E3=False), 50.0, 3.0).status == "NO-GO"  # selection no better than random
    assert edge_status(True, chk(E4=False), 50.0, 3.0).status == "NO-GO"  # one fold carries the result
    assert edge_status(True, chk(E5=False), 50.0, 3.0).status == "NO-GO"  # the holdout contradicts
    assert edge_status(True, chk(E2=False, E5=False), 20.0, 1.5).status == "NO-GO"


def test_reasons_name_the_failed_checks() -> None:
    v = edge_status(True, chk(E3=False, E4=False), 50.0, 3.0)
    assert v.status == "NO-GO" and v.reasons == ["E3", "E4"]
    assert edge_status(False, chk(), 0, 0).reasons == ["regime_change"]


# ------------------------------------------------------------------------ P1c: flat folds, E5, baselines

LABEL = loaded_label({"A": 3.0})


def span_set() -> SpanSet:
    def sp(a: int, b: int, fold: int) -> Span:
        return Span(T0 + pd.Timedelta(days=a), T0 + pd.Timedelta(days=b), None, fold)

    folds = [FoldSpans(i, sp(0, 1, i), sp(60 * i, 60 * i + 60, i)) for i in range(5)]
    return SpanSet(folds, sp(0, 1, -2), sp(300, 330, -1), T0 + pd.Timedelta(days=300), pd.Timedelta(days=2))


def book(folds: list[int], per_fold: int, ret_bps: float = 100.0, pnl: float = 1.0) -> pd.DataFrame:
    """Trades of product A, ``per_fold`` per listed fold, one per day, two hours long."""
    rows = []
    for f in folds:
        for i in range(per_fold):
            fill = T0 + pd.Timedelta(days=60 * f + i, hours=3)
            rows.append({"product": "A", "fold": f, "fill_ts": fill, "exit_ts": fill + pd.Timedelta(hours=2),
                         "net_ret_bps": ret_bps, "label_net_bps": ret_bps, "pnl_eur": pnl})  # fmt: skip
    return pd.DataFrame(rows)


def candidates_everywhere() -> dict[tuple[int, str], Candidates]:
    t = np.arange(400, dtype=np.int64) * 10
    return {(f, "A"): Candidates(t, t + 5, np.full(400, -100.0)) for f in range(5)}


def holdout(n: int) -> pd.DataFrame:
    return book([5], n).assign(fold=-1).assign(fill_ts=lambda d: d["fill_ts"] + pd.Timedelta(days=0))


def test_flat_fold_is_zero_and_not_positive() -> None:
    """Two positive folds and three flat ones: the five folds are listed, 0.0 is not positive, E4 fails."""
    out = _ge(book([0, 1], 80), holdout(10), candidates_everywhere(), LABEL, span_set(), True, 0.5)
    assert out["pnl_by_fold"] == {0: 80.0, 1: 80.0, 2: 0.0, 3: 0.0, 4: 0.0}
    assert out["n_positive_folds"] == 2 and out["checks"]["E4"] is False


def test_all_five_positive_folds_pass_the_concentration_check() -> None:
    out = _ge(book(range(5), 40), holdout(10), candidates_everywhere(), LABEL, span_set(), True, 0.5)  # type: ignore[arg-type]
    assert out["n_positive_folds"] == 5 and out["checks"]["E4"] is True and out["max_fold_share"] == pytest.approx(0.2)


def test_bootstrap_uses_only_real_trades() -> None:
    trades_ = book([0, 2], 90, ret_bps=40.0)  # fold 1, 3, 4 have none: no fictitious zeros enter the mean
    out = _ge(trades_, holdout(10), candidates_everywhere(), LABEL, span_set(), True, 0.5)
    assert out["bootstrap"]["mean_bps"] == pytest.approx(40.0)
    assert out["n_oos_trades"] == 180


def test_e5_false_without_holdout_trades() -> None:
    out = _ge(book(range(5), 40), pd.DataFrame(), candidates_everywhere(), LABEL, span_set(), True, 0.5)  # type: ignore[arg-type]
    assert out["checks"]["E5"] is False
    assert out["status"] == "NO-GO" and "holdout_no_trades" in out["reasons"]


def test_e5_none_with_one_to_four_trades() -> None:
    for n in (1, 4):
        out = _ge(book(range(5), 40), holdout(n), candidates_everywhere(), LABEL, span_set(), True, 0.5)  # type: ignore[arg-type]
        assert out["checks"]["E5"] is None and out["status"] == "INCONCLUSO"


def test_no_trades_at_all_is_no_go_with_five_flat_folds() -> None:
    out = _ge(pd.DataFrame(), pd.DataFrame(), {}, LABEL, span_set(), True, 0.5)
    assert out["status"] == "NO-GO" and out["pnl_by_fold"] == dict.fromkeys(range(5), 0.0)


def test_always_invested_is_a_greedy_chain_per_fold_and_product() -> None:
    c = {(0, "A"): Candidates(np.array([0, 5, 10, 30]), np.array([8, 12, 20, 40]), np.array([10.0, -50.0, 30.0, 5.0]))}
    r = always_invested(c)
    assert r["n"] == 3 and r["mean_label_bps"] == pytest.approx((10.0 + 30.0 + 5.0) / 3)  # 5 starts before 8: skipped
    assert always_invested({}) == {"n": 0, "mean_label_bps": None}


def test_buy_and_hold_pays_the_round_trip_and_reports_the_drawdown() -> None:
    n = 3 * 1440
    ts = (np.datetime64("2025-01-01T00:01") + np.arange(n).astype("timedelta64[m]")).astype("datetime64[ns]")
    pm = SimpleNamespace(bars=SimpleNamespace(ts_close=ts, c=np.full(n, 100.0)))
    span = Span(T0, T0 + pd.Timedelta(days=3), None, 0)
    r = buy_and_hold({"A": pm}, LABEL, span)  # type: ignore[dict-item]
    cost = 25 + 50 + 3.0 / 2 + 5  # maker in, taker out, half the spread, slippage buffer (bps)
    assert r["n_products"] == 1 and r["return_bps"] == pytest.approx(-cost, abs=1.0) and r["max_drawdown"] == 0.0
    assert r["bps_per_day"] == pytest.approx(r["return_bps"] / 3.0, rel=1e-3)
    assert buy_and_hold({}, LABEL, span) == {"n_products": 0}
