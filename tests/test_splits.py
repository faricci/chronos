"""Purged walk-forward splits: no label window of an earlier set reaches into a later set (spec sez. 3.1.4)."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from training.splits import final_fit_split, holdout_start_from, purged_walk_forward

H = pd.Timedelta(days=15, minutes=30)


def make(n_days: int = 900, step_h: int = 1) -> tuple[pd.DatetimeIndex, pd.DatetimeIndex]:
    ts = pd.date_range("2023-10-01", periods=n_days * 24 // step_h, freq=f"{step_h}h", tz="UTC")
    return ts, ts + H


def assert_no_leak(
    ts: pd.DatetimeIndex, t1: pd.DatetimeIndex, earlier: np.ndarray, later: np.ndarray, embargo: pd.Timedelta
) -> None:
    if earlier.size == 0 or later.size == 0:
        return
    assert t1[earlier].max() + embargo <= ts[later].min() + pd.Timedelta(0), "label window reaches into the next set"
    assert not set(earlier.tolist()) & set(later.tolist())


def test_no_overlap_between_sets_of_any_fold() -> None:
    ts, t1 = make()
    emb = pd.Timedelta(days=2)
    sp = purged_walk_forward(ts, t1, n_folds=5, embargo=timedelta(days=2))
    assert len(sp.folds) == 5
    for f in sp.folds:
        assert_no_leak(ts, t1, f.train, f.val, emb)
        assert_no_leak(ts, t1, f.val, f.test, emb)
        assert_no_leak(ts, t1, f.train, f.test, emb)
    dev_all = np.concatenate([f.test for f in sp.folds])
    assert_no_leak(ts, t1, dev_all, sp.holdout, emb)


def test_folds_are_expanding_and_tests_are_disjoint_and_ordered() -> None:
    ts, t1 = make()
    sp = purged_walk_forward(ts, t1)
    for a, b in zip(sp.folds, sp.folds[1:], strict=False):
        assert a.test_end == b.test_start
        assert ts[a.test].max() < ts[b.test].min()
        assert len(b.train) + len(b.val) >= len(a.train) + len(a.val)
    assert all(len(f.train) > 0 and len(f.val) > 0 and len(f.test) > 0 for f in sp.folds)


def test_holdout_is_the_last_120_days_and_no_fold_touches_it() -> None:
    ts, t1 = make()
    sp = purged_walk_forward(ts, t1, holdout_days=120)
    assert ts[sp.holdout].min() >= ts[-1] - pd.Timedelta(days=120)
    used = np.concatenate([np.concatenate([f.train, f.val, f.test]) for f in sp.folds])
    assert not set(used.tolist()) & set(sp.holdout.tolist())
    assert (ts[used] < sp.holdout_start).all()


def test_samples_close_to_a_boundary_are_purged() -> None:
    ts, t1 = make()
    sp = purged_walk_forward(ts, t1)
    f = sp.folds[2]
    just_before_test = np.flatnonzero((ts < f.test_start) & (ts > f.test_start - pd.Timedelta(days=10)))
    assert not set(just_before_test.tolist()) & set(f.val.tolist())  # their label window reaches the test period
    assert not set(just_before_test.tolist()) & set(f.train.tolist())


def test_embargo_removes_more_samples() -> None:
    ts, t1 = make()
    short = purged_walk_forward(ts, t1, embargo=timedelta(hours=1))
    long = purged_walk_forward(ts, t1, embargo=timedelta(days=10))
    pairs = list(zip(short.folds, long.folds, strict=True))
    assert all(len(a.train) >= len(b.train) and len(a.val) >= len(b.val) for a, b in pairs)
    assert any(len(a.val) > len(b.val) for a, b in pairs)


def test_deterministic() -> None:
    ts, t1 = make()
    a, b = purged_walk_forward(ts, t1), purged_walk_forward(ts, t1)
    for fa, fb in zip(a.folds, b.folds, strict=True):
        np.testing.assert_array_equal(fa.train, fb.train)
        np.testing.assert_array_equal(fa.test, fb.test)


def test_validates_inputs() -> None:
    ts, t1 = make(100)
    with pytest.raises(ValueError):
        purged_walk_forward(ts, t1[:-1])
    with pytest.raises(ValueError):
        purged_walk_forward(ts, ts - pd.Timedelta(hours=1))
    with pytest.raises(ValueError):
        purged_walk_forward(ts[::-1], t1[::-1])
    with pytest.raises(ValueError):
        purged_walk_forward(ts, t1, n_folds=0)
    with pytest.raises(ValueError):
        purged_walk_forward(ts, t1, holdout_days=5000)


@settings(max_examples=40, deadline=None)
@given(
    n_days=st.integers(min_value=400, max_value=1200),
    step_h=st.sampled_from([1, 3, 6]),
    horizon_days=st.integers(min_value=1, max_value=20),
    embargo_days=st.integers(min_value=0, max_value=5),
    n_folds=st.integers(min_value=2, max_value=6),
)
def test_property_no_leakage_for_any_geometry(
    n_days: int, step_h: int, horizon_days: int, embargo_days: int, n_folds: int
) -> None:
    ts = pd.date_range("2023-01-01", periods=n_days * 24 // step_h, freq=f"{step_h}h", tz="UTC")
    t1 = ts + pd.Timedelta(days=horizon_days)
    emb = pd.Timedelta(days=embargo_days)
    sp = purged_walk_forward(ts, t1, n_folds=n_folds, embargo=timedelta(days=embargo_days))
    for f in sp.folds:
        assert_no_leak(ts, t1, f.train, f.val, emb)
        assert_no_leak(ts, t1, f.val, f.test, emb)
    assert_no_leak(ts, t1, np.concatenate([f.test for f in sp.folds]), sp.holdout, emb)


# ---- frozen holdout start (B9)


def test_holdout_start_from_is_the_midnight_before_the_last_close_minus_the_holdout() -> None:
    last = pd.Timestamp("2026-10-18 06:08", tz="UTC")
    assert holdout_start_from(last) == pd.Timestamp("2026-06-20", tz="UTC")
    assert holdout_start_from(pd.Timestamp("2026-10-18 00:00"), 10) == pd.Timestamp("2026-10-08", tz="UTC")


def test_explicit_holdout_start_defines_the_holdout_exactly() -> None:
    ts, t1 = make()
    hs = pd.Timestamp("2025-12-01", tz="UTC")
    sp = purged_walk_forward(ts, t1, holdout_start=hs)
    assert sp.holdout_start == hs
    assert (ts[sp.holdout] >= hs).all() and len(sp.holdout) == int((ts >= hs).sum())
    dev = np.concatenate([f.train for f in sp.folds] + [f.val for f in sp.folds] + [f.test for f in sp.folds])
    assert (t1[dev] < hs - pd.Timedelta(days=2)).all()  # dev is purged against the frozen holdout


def test_rerunning_later_cannot_move_the_holdout_into_data_already_used() -> None:
    ts, t1 = make(900)
    hs = ts[300 * 24].floor("D")  # frozen by an earlier run on the first 400 days
    early = purged_walk_forward(ts[: 400 * 24], t1[: 400 * 24], holdout_start=hs)
    late = purged_walk_forward(ts, t1, holdout_start=hs)
    assert early.holdout_start == late.holdout_start == hs
    assert (ts[late.holdout] >= hs).all() and len(late.holdout) > len(early.holdout)  # it grows with the history


@pytest.mark.parametrize("hs", ["2020-01-01", "2030-01-01"])
def test_holdout_start_outside_the_sample_range_is_rejected(hs: str) -> None:
    ts, t1 = make(200)
    with pytest.raises(ValueError):
        purged_walk_forward(ts, t1, holdout_start=pd.Timestamp(hs, tz="UTC"))


# ---- final model split (O3.6)


def test_final_fit_split_has_no_leak_and_ends_before_the_holdout() -> None:
    ts, t1 = make()
    hs = pd.Timestamp("2026-01-01", tz="UTC")
    emb = pd.Timedelta(days=2)
    train, val = final_fit_split(ts, t1, hs)
    assert len(train) > 0 and len(val) > 0
    assert t1[train].max() + emb <= ts[val].min()  # purge + embargo between train and validation
    assert t1[val].max() < hs - emb and ts[val].max() < hs
    assert not set(train.tolist()) & set(val.tolist())
    span = hs - ts[0]
    assert ts[val].min() >= ts[0] + span * 0.84  # the most recent ~15% of the development period


def test_final_fit_split_rejects_bad_input() -> None:
    ts, t1 = make(100)
    with pytest.raises(ValueError):
        final_fit_split(ts, t1, pd.Timestamp("2020-01-01", tz="UTC"))
    with pytest.raises(ValueError):
        final_fit_split(ts, t1, pd.Timestamp("2024-01-01", tz="UTC"), val_frac=0.9)
