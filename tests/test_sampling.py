"""Sampling and uniqueness weights (spec sez. 3.1.4)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from training.sampling import cusum_events, hourly_grid, sample_indices, uniqueness_weights


def test_cusum_fires_on_cumulative_drift_and_resets() -> None:
    r = np.full(10, 0.004)
    ev = cusum_events(r, 0.01)
    # 0.004, 0.008, 0.012 > 0.01 -> event at index 2, reset, again at 5 and 8
    assert ev.tolist() == [2, 5, 8]


def test_cusum_is_symmetric() -> None:
    assert cusum_events(np.full(10, -0.004), 0.01).tolist() == [2, 5, 8]


def test_cusum_noise_inside_the_band_never_fires() -> None:
    r = np.tile([0.004, -0.004], 50)
    assert cusum_events(r, 0.01).size == 0


def test_cusum_opposite_move_resets_the_other_side_only_through_the_max() -> None:
    r = np.array([0.006, -0.012, 0.006, 0.006])
    # s+: .006, 0, .006, .012 (> .01 at 3); s-: 0, -.012 -> event at 1 (s- < -.01) then reset
    assert cusum_events(r, 0.01).tolist() == [1, 3]


def test_cusum_skips_nan_without_resetting() -> None:
    r = np.array([0.006, np.nan, 0.006])
    assert cusum_events(r, 0.01).tolist() == [2]
    h = np.array([0.01, 0.01, np.nan])
    assert cusum_events(np.array([0.006, 0.006, 0.006]), h).tolist() == [1]


def test_cusum_rejects_mismatched_threshold() -> None:
    with pytest.raises(ValueError):
        cusum_events(np.zeros(5), np.ones(4))


def test_hourly_grid_selects_bars_closing_on_the_hour() -> None:
    idx = pd.date_range("2025-01-01 00:00", periods=180, freq="1min", tz="UTC")
    g = hourly_grid(idx)
    assert [idx[i].strftime("%H:%M") for i in g] == ["00:59", "01:59", "02:59"]


def test_sample_indices_is_the_sorted_union() -> None:
    idx = pd.date_range("2025-01-01 00:00", periods=180, freq="1min", tz="UTC")
    r = np.zeros(180)
    r[[10, 59]] = 1.0  # events at 10 and 59 (59 is also on the grid)
    got = sample_indices(idx, r, 0.5)
    assert got.tolist() == [10, 59, 119, 179]


def test_uniqueness_non_overlapping_labels_have_weight_one() -> None:
    w = uniqueness_weights(np.array([0, 10, 20]), np.array([4, 14, 24]))
    np.testing.assert_allclose(w, 1.0)


def test_uniqueness_identical_labels_share_the_weight() -> None:
    w = uniqueness_weights(np.array([0, 0]), np.array([9, 9]))
    np.testing.assert_allclose(w, 0.5)


def test_uniqueness_partial_overlap_by_hand() -> None:
    # A = [0,3], B = [2,5]; bars 0,1: c=1; bars 2,3: c=2; bars 4,5: c=1
    w = uniqueness_weights(np.array([0, 2]), np.array([3, 5]))
    np.testing.assert_allclose(w, [(1 + 1 + 0.5 + 0.5) / 4, (0.5 + 0.5 + 1 + 1) / 4])


def test_uniqueness_matches_brute_force() -> None:
    rng = np.random.default_rng(3)
    t0 = np.sort(rng.integers(0, 200, 40))
    t1 = t0 + rng.integers(0, 60, 40)
    fast = uniqueness_weights(t0, t1)
    conc = np.zeros(300)
    for a, b in zip(t0, t1, strict=True):
        conc[a : b + 1] += 1
    slow = np.array([np.mean(1.0 / conc[a : b + 1]) for a, b in zip(t0, t1, strict=True)])
    np.testing.assert_allclose(fast, slow, atol=1e-12)
    assert ((fast > 0) & (fast <= 1)).all()


def test_uniqueness_effective_sample_size_of_full_overlap() -> None:
    n = 50
    w = uniqueness_weights(np.zeros(n, dtype=int), np.full(n, 99))
    assert w.sum() == pytest.approx(1.0)  # 50 identical labels carry one sample of information


def test_uniqueness_validates_spans() -> None:
    with pytest.raises(ValueError):
        uniqueness_weights(np.array([5]), np.array([4]))
    assert uniqueness_weights(np.array([], dtype=int), np.array([], dtype=int)).size == 0
