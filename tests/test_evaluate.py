"""Evidence gate GE: effective trades, block bootstrap, permutation test, concentration and the three-state verdict."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from training.evaluate import (
    Boot,
    Candidates,
    block_bootstrap,
    concentration,
    edge_status,
    effective_trades,
    holdout_consistency,
    permutation_pvalue,
)

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
