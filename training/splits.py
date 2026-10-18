"""Purged walk-forward splits with embargo (spec sez. 3.1.4, 3.5 punto 3).

Labels look ``T_in + H`` forward, so neighbouring samples share outcomes. A random split would leak, and even a plain
time split leaks near the boundary. Rules implemented here:

- the last ``holdout_days`` of the history are the final holdout, untouched by every fold;
- the rest is cut into ``n_folds + 1`` equal time blocks; fold ``i`` tests on block ``i + 1`` and trains (expanding
  window) on everything before it; the last ``val_frac`` of that training time span is the validation set used for
  calibration and for the policy thresholds (sez. 3.1.6);
- PURGE + EMBARGO: a sample of an earlier set is dropped when its label window ``[t, t1]`` ends later than
  ``start_of_next_set - embargo``. The same rule separates dev data from the holdout.

Everything is expressed in positional indices into the arrays given by the caller; nothing is shuffled.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import numpy as np
import pandas as pd

__all__ = ["Fold", "Splits", "final_fit_split", "holdout_start_from", "purged_walk_forward"]

DEFAULT_EMBARGO = timedelta(days=2)  # sez. 3.1.4


@dataclass(frozen=True)
class Fold:
    index: int
    train: np.ndarray
    val: np.ndarray
    test: np.ndarray
    test_start: pd.Timestamp
    test_end: pd.Timestamp  # exclusive


@dataclass(frozen=True)
class Splits:
    folds: list[Fold]
    holdout: np.ndarray
    holdout_start: pd.Timestamp


def _ns(x: pd.DatetimeIndex | np.ndarray) -> np.ndarray:
    idx = pd.DatetimeIndex(x)
    if idx.tz is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    return np.asarray(idx.to_numpy(dtype="datetime64[ns]"))


def holdout_start_from(last_ts_bar_close: pd.Timestamp, holdout_days: int = 120) -> pd.Timestamp:
    """The ONE definition of where the final holdout starts: ``floor_to_day_utc(last close) - holdout_days``.

    The feasibility study freezes it in ``label_config.json`` (``selection.holdout_start``) and ``build_dataset`` passes
    it back to ``purged_walk_forward``, so data seen by the selection can never fall inside the holdout.
    """
    ts = pd.Timestamp(last_ts_bar_close)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return ts.floor("D") - pd.Timedelta(days=holdout_days)


def _keep_before(idx: np.ndarray, t1: np.ndarray, limit: np.datetime64) -> np.ndarray:
    """Indices of ``idx`` whose label window ends strictly before ``limit``."""
    return np.asarray(idx[t1[idx] < limit])


def purged_walk_forward(
    timestamps: pd.DatetimeIndex,
    t1: pd.DatetimeIndex,
    n_folds: int = 5,
    embargo: timedelta = DEFAULT_EMBARGO,
    holdout_days: int = 120,
    val_frac: float = 0.15,
    holdout_start: pd.Timestamp | None = None,
) -> Splits:
    """Splits for samples at ``timestamps`` (sorted) whose label windows end at ``t1`` (>= timestamps)."""
    if n_folds < 1 or not 0.0 < val_frac < 0.5 or holdout_days < 0:
        raise ValueError("invalid split parameters")
    t0 = _ns(pd.DatetimeIndex(timestamps))
    t1n = _ns(pd.DatetimeIndex(t1))
    if t0.shape != t1n.shape or t0.ndim != 1:
        raise ValueError("timestamps and t1 must have the same length")
    if t0.size == 0:
        raise ValueError("no samples")
    if (t1n < t0).any():
        raise ValueError("t1 must not precede its timestamp")
    if (np.diff(t0) < np.timedelta64(0, "ns")).any():
        raise ValueError("timestamps must be sorted")
    emb = np.timedelta64(int(embargo.total_seconds() * 1e9), "ns")
    all_idx = np.arange(t0.size)

    end = t0[-1] + np.timedelta64(1, "ns")
    if holdout_start is not None:
        hold_start = _ns(pd.DatetimeIndex([holdout_start]))[0]
        if hold_start > t0[-1] or hold_start <= t0[0]:
            raise ValueError("holdout_start must fall inside the sample range")
    else:
        hold_start = end - np.timedelta64(holdout_days, "D") if holdout_days else end
    holdout = all_idx[t0 >= hold_start] if (holdout_days or holdout_start is not None) else np.empty(0, dtype=np.int64)
    dev = _keep_before(all_idx[t0 < hold_start], t1n, hold_start - emb)
    if dev.size == 0:
        raise ValueError("no development samples before the holdout")

    start = t0[dev[0]]
    block = (hold_start - start) / (n_folds + 1)
    folds: list[Fold] = []
    for i in range(n_folds):
        test_start = start + block * (i + 1)
        test_end = hold_start if i == n_folds - 1 else start + block * (i + 2)
        test = dev[(t0[dev] >= test_start) & (t0[dev] < test_end)]
        pre = dev[t0[dev] < test_start]
        val_start = start + (test_start - start) * (1.0 - val_frac)
        val = _keep_before(pre[t0[pre] >= val_start], t1n, test_start - emb)
        train = _keep_before(pre[t0[pre] < val_start], t1n, val_start - emb)
        folds.append(Fold(i, train, val, test, pd.Timestamp(test_start, tz="UTC"), pd.Timestamp(test_end, tz="UTC")))
    return Splits(folds, holdout, pd.Timestamp(hold_start, tz="UTC"))


def final_fit_split(
    timestamps: pd.DatetimeIndex,
    t1: pd.DatetimeIndex,
    holdout_start: pd.Timestamp,
    val_frac: float = 0.15,
    embargo: timedelta = DEFAULT_EMBARGO,
) -> tuple[np.ndarray, np.ndarray]:
    """``(train, val)`` of the FINAL model (decision O3.6): the most recent ``val_frac`` of the development period is
    the validation set used for calibration and for the production policy; the model is not refit on it.

    ``dev`` = samples with ``t < holdout_start`` whose label window ends before ``holdout_start - embargo``; the train
    set is purged against the start of the validation set in the same way.
    """
    if not 0.0 < val_frac < 0.5:
        raise ValueError("invalid val_frac")
    t0, t1n = _ns(pd.DatetimeIndex(timestamps)), _ns(pd.DatetimeIndex(t1))
    if t0.shape != t1n.shape:
        raise ValueError("timestamps and t1 must have the same length")
    hold = _ns(pd.DatetimeIndex([holdout_start]))[0]
    emb = np.timedelta64(int(embargo.total_seconds() * 1e9), "ns")
    all_idx = np.arange(t0.size)
    dev = all_idx[(t0 < hold) & (t1n < hold - emb)]
    if dev.size == 0:
        raise ValueError("no development samples before the holdout")
    val_start = t0[dev[0]] + (hold - t0[dev[0]]) * (1.0 - val_frac)
    val = dev[t0[dev] >= val_start]
    train = dev[(t0[dev] < val_start) & (t1n[dev] < val_start - emb)]
    return train, val
