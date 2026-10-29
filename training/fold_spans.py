"""Time spans of validation, test and holdout for the backtests (decision O2.7).

They are derived from the SAME splitter as the dataset (``purged_walk_forward`` / ``final_fit_split``), so the
backtest of fold ``k`` decides entries exactly where the model of fold ``k`` has never trained:

- test of fold ``k``: ``[test_start_k, test_end_k)``; ``data_limit`` is ``holdout_start - embargo`` for the last fold
  only (a trade of an earlier fold may close after ``test_end``, never inside the holdout);
- validation of fold ``k`` (thresholds): from its first sample to ``test_start_k``, with
  ``data_limit = test_start_k - embargo``;
- final validation: from its first sample to ``holdout_start``, with ``data_limit = holdout_start - embargo``;
- holdout: ``[holdout_start, last bar]`` with every label window inside the data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import pandas as pd

from modules.risk.label_config import LoadedLabelConfig
from training.backtest import Span
from training.config import TrainConfig
from training.splits import final_fit_split, purged_walk_forward

__all__ = ["FoldSpans", "SpanSet", "compute_spans", "span_rows"]


@dataclass(frozen=True)
class FoldSpans:
    index: int
    val: Span
    test: Span


@dataclass(frozen=True)
class SpanSet:
    folds: list[FoldSpans]
    final_val: Span
    holdout: Span
    holdout_start: pd.Timestamp
    embargo: pd.Timedelta


def _utc(ts: pd.Series | pd.DatetimeIndex) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(ts).tz_localize("UTC")


def compute_spans(
    samples: pd.DataFrame, label: LoadedLabelConfig, cfg: TrainConfig, last_close: pd.Timestamp
) -> SpanSet:
    """``samples``: the dataset table (any rows; only the FLAT ones define the splits, like in the dataset builder)."""
    flat = samples[samples["position_side"] == "FLAT"].sort_values(["t", "product"], kind="stable")
    ts, t1 = _utc(flat["t"]), _utc(flat["label_window_end"])
    embargo = timedelta(days=cfg.embargo_days)
    hs = label.holdout_start
    splits = purged_walk_forward(ts, t1, n_folds=cfg.n_folds, embargo=embargo, val_frac=cfg.val_frac, holdout_start=hs)
    emb = pd.Timedelta(embargo)
    folds: list[FoldSpans] = []
    for i, f in enumerate(splits.folds):
        last = i == len(splits.folds) - 1
        val_start = ts[f.val].min() if len(f.val) else f.test_start
        val = Span(val_start, f.test_start, f.test_start - emb, f.index)
        test = Span(f.test_start, f.test_end, hs - emb if last else None, f.index)
        folds.append(FoldSpans(f.index, val, test))
    _, val_idx = final_fit_split(ts, t1, hs, cfg.val_frac, embargo)
    final_start = ts[val_idx].min() if len(val_idx) else hs
    end = last_close + pd.Timedelta(nanoseconds=1)
    return SpanSet(
        folds,
        Span(final_start, hs, hs - emb, -3),
        Span(hs, end, end, -1),
        hs,
        emb,
    )


def span_rows(spans: SpanSet) -> list[Span]:
    return [s for f in spans.folds for s in (f.val, f.test)] + [spans.final_val, spans.holdout]
