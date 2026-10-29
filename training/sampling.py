"""Sample selection and uniqueness weights (spec sez. 3.1.4).

Not every minute is a sample (extreme autocorrelation, near-duplicate labels): the sample set is the union of an
hourly grid and the events of a symmetric CUSUM filter on the 1m log-returns. With a multi-day horizon labels
overlap heavily, so the average-uniqueness weights (Lopez de Prado) are mandatory.

Index convention as in ``labeling``: a sample is the index ``t`` of the last closed 1m bar.
"""

from __future__ import annotations

import numba
import numpy as np
import pandas as pd

__all__ = ["cusum_events", "hourly_grid", "sample_indices", "uniqueness_weights"]


@numba.njit(cache=True)
def _cusum(log_ret: np.ndarray, h: np.ndarray) -> np.ndarray:  # pragma: no cover - compiled
    n = log_ret.shape[0]
    out = np.empty(n, dtype=np.int64)
    count = 0
    s_pos = 0.0
    s_neg = 0.0
    for i in range(n):
        r = log_ret[i]
        thr = h[i]
        if np.isnan(r) or np.isnan(thr) or thr <= 0.0:
            continue  # no information: the accumulators neither move nor reset
        s_pos = max(0.0, s_pos + r)
        s_neg = min(0.0, s_neg + r)
        if s_neg < -thr:
            s_neg = 0.0
            out[count] = i
            count += 1
        elif s_pos > thr:
            s_pos = 0.0
            out[count] = i
            count += 1
    return out[:count]


def cusum_events(log_ret: np.ndarray, h: np.ndarray | float) -> np.ndarray:
    """Indices where the symmetric CUSUM of ``log_ret`` crosses the threshold ``h`` (scalar or per bar).

    ``s+ = max(0, s+ + r)``, ``s- = min(0, s- + r)``; an event fires when ``s- < -h`` or ``s+ > h`` and resets the
    accumulator that fired. NaN returns or thresholds are skipped.
    """
    r = np.ascontiguousarray(log_ret, dtype=np.float64)
    thr = np.full(r.shape[0], float(h)) if np.ndim(h) == 0 else np.ascontiguousarray(h, dtype=np.float64)
    if thr.shape != r.shape:
        raise ValueError("h must be a scalar or have the same length as log_ret")
    return _cusum(r, thr)


def hourly_grid(index: pd.DatetimeIndex) -> np.ndarray:
    """Indices of the bars whose close falls on a whole hour (``ts_bar_close = ts + 1 min``)."""
    close_ts = pd.DatetimeIndex(index) + pd.Timedelta(minutes=1)
    return np.flatnonzero(close_ts.minute == 0)


def sample_indices(index: pd.DatetimeIndex, log_ret: np.ndarray, h: np.ndarray | float) -> np.ndarray:
    """Sorted union of the hourly grid and the CUSUM events."""
    return np.union1d(hourly_grid(index), cusum_events(log_ret, h))


def uniqueness_weights(t0: np.ndarray, t1: np.ndarray, n_bars: int | None = None) -> np.ndarray:
    """Average uniqueness of each label over its own span ``[t0_i, t1_i]`` (bar indices, inclusive).

    ``c_t`` = number of labels whose span contains bar ``t``; ``w_i = mean_{t in span_i} 1 / c_t``. Weights are in
    ``(0, 1]``; ``w.sum()`` is the effective number of independent samples (``n_eff``).
    """
    a = np.asarray(t0, dtype=np.int64)
    b = np.asarray(t1, dtype=np.int64)
    if a.shape != b.shape or a.ndim != 1:
        raise ValueError("t0 and t1 must be 1-D arrays of the same length")
    if a.size == 0:
        return np.empty(0, dtype=np.float64)
    if (b < a).any() or (a < 0).any():
        raise ValueError("each span must satisfy 0 <= t0 <= t1")
    size = int(b.max()) + 2 if n_bars is None else max(n_bars, int(b.max()) + 1) + 1
    delta = np.zeros(size + 1, dtype=np.int64)
    np.add.at(delta, a, 1)
    np.add.at(delta, b + 1, -1)
    conc = np.cumsum(delta)[:size]
    inv = np.zeros(size, dtype=np.float64)
    live = conc > 0
    inv[live] = 1.0 / conc[live]
    prefix = np.concatenate([[0.0], np.cumsum(inv)])
    return np.asarray((prefix[b + 1] - prefix[a]) / (b - a + 1), dtype=np.float64)
