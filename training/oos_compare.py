"""Paired comparison of two models on their common out-of-sample walk-forward (rule L5, P2 and S3; spec 5.1.6).

``training.evaluate`` writes, next to ``evaluation.json`` and BEFORE the model is registered, the OOS trades GE was
computed on (``oos_trades.jsonl``, one per row, with the E1 weight) and their test spans (``oos_spans.json``). Both
become part of the immutable registered version. This module reads them for two versions and answers one question: is
the challenger better than the champion by more than the noise?

* the common OOS is the INTERSECTION of the test spans of the two models; a trade counts if its ``decision_ts`` falls in
  it (for both models, the same rule);
* the statistic is ``D = PnL_challenger - PnL_champion`` (EUR, same capital) over a circular block bootstrap with the
  SAME blocks for the two models (the same random block starts in every replicate), through the one block definition
  of E2 (``training.evaluate.BlockGrid`` / ``BlockedValues``): all products share the blocks, a trade belongs to the
  block of its ``decision_ts``. Condition of rule L5: the ``q`` quantile of ``D`` is above zero;
* Sharpe and drawdown of each model on the common OOS come from the daily PnL series (days without exits are 0) over
  ``starting_quote``, through ``training.backtest.equity_sharpe_and_drawdown``: the definition of the backtest, not a
  second one.

A champion that is not GE = GO has no paired part: only the challenger side is computed (S3 needs it).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from modules.dryrun.registry import OosEvidence
from training.backtest import equity_sharpe_and_drawdown
from training.evaluate import (
    OOS_SPANS,
    OOS_SPANS_SCHEMA,
    OOS_TRADE_FIELDS,
    OOS_TRADES,
    BlockedValues,
    block_grid,
    draw_block_starts,
)

__all__ = [
    "OosArtifacts",
    "OosMetrics",
    "PairedOos",
    "common_spans",
    "compare_models",
    "load_oos_artifacts",
    "oos_metrics",
    "paired_oos_comparison",
]

Span = tuple[pd.Timestamp, pd.Timestamp]
DAY = pd.Timedelta(days=1)


@dataclass(frozen=True)
class OosArtifacts:
    trades: pd.DataFrame  # OOS_TRADE_FIELDS, timestamps tz-aware UTC
    spans: list[Span]  # test span [start, end) of every fold
    label_config_sha256: str
    block_days: int


def _utc(value: object) -> pd.Timestamp:
    t = pd.Timestamp(str(value))
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def load_oos_artifacts(model_dir: Path) -> OosArtifacts | None:
    """The OOS artifacts of a registered version, or None if they are missing or unreadable (a version evaluated
    before they existed has none: L5 then says ``oos_unavailable``, it never guesses)."""
    try:
        doc = json.loads((model_dir / OOS_SPANS).read_text(encoding="utf-8"))
        if doc["v"] != OOS_SPANS_SCHEMA:
            return None
        spans = [(_utc(s["start"]), _utc(s["end"])) for s in doc["spans"]]
        rows = [
            json.loads(line)
            for line in (model_dir / OOS_TRADES).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        trades = pd.DataFrame(rows, columns=list(OOS_TRADE_FIELDS))
        for col in ("decision_ts", "fill_ts", "exit_ts"):
            trades[col] = pd.to_datetime(trades[col], utc=True)
        return OosArtifacts(trades, spans, str(doc["label_config_sha256"]), int(doc["block_days"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def common_spans(a: Sequence[Span], b: Sequence[Span]) -> list[Span]:
    """Intersection of two lists of ``[start, end)`` intervals (each list disjoint), sorted, empty pieces dropped."""
    out: list[Span] = []
    for s1, e1 in a:
        for s2, e2 in b:
            lo, hi = max(s1, s2), min(e1, e2)
            if lo < hi:
                out.append((lo, hi))
    return sorted(out)


def _in_spans(ts: pd.Series, spans: Sequence[Span]) -> np.ndarray:
    keep = np.zeros(len(ts), dtype=bool)
    for lo, hi in spans:
        keep |= ((ts >= lo) & (ts < hi)).to_numpy()
    return keep


@dataclass(frozen=True)
class OosMetrics:
    n_trades: int
    pnl_eur: float
    sharpe: float
    max_dd_frac: float


def oos_metrics(trades: pd.DataFrame, spans: Sequence[Span], starting_quote: float) -> OosMetrics:
    """Sharpe and drawdown of a model on the OOS ``spans``: the daily PnL (by day of exit; days without exits are 0)
    accumulated over ``starting_quote``, measured by the backtest's own function."""
    if not spans:
        return OosMetrics(0, 0.0, 0.0, 0.0)
    kept = trades[_in_spans(trades["decision_ts"], spans)]
    first = min(s for s, _ in spans).floor("D")
    last = max((max(e for _, e in spans) - pd.Timedelta(nanoseconds=1)).floor("D"), first)
    if len(kept):
        last = max(last, kept["exit_ts"].max().floor("D"))
    days = pd.date_range(first, last, freq="D", tz="UTC")
    pnl = kept.groupby(kept["exit_ts"].dt.floor("D"))["pnl_eur"].sum().reindex(days, fill_value=0.0)
    equity = starting_quote + pnl.cumsum()
    equity = pd.concat([pd.Series([starting_quote], index=[first - DAY]), equity])
    sharpe, dd = equity_sharpe_and_drawdown(equity) if len(equity) > 2 else (0.0, 0.0)
    return OosMetrics(len(kept), float(kept["pnl_eur"].sum()), sharpe, dd)


@dataclass(frozen=True)
class PairedOos:
    common_days: float
    challenger: OosMetrics
    champion: OosMetrics
    d_pnl_mean: float  # mean of D over the replicates (EUR)
    d_pnl_quantile: float  # the ``q`` quantile of D (EUR)


def paired_oos_comparison(
    trades_ch: pd.DataFrame, trades_cp: pd.DataFrame, spans_ch: Sequence[Span], spans_cp: Sequence[Span],
    block_days: int, reps: int, seed: int, q: float, starting_quote: float,
) -> PairedOos:  # fmt: skip
    """Challenger against champion on the common OOS (see the module docstring). ``block_days``: block length of the
    bootstrap (the longer of the two ``label_config`` blocks); ``reps``, ``seed``: the constants of GE."""
    common = common_spans(spans_ch, spans_cp)
    if not common:
        raise ValueError("the two models have no out-of-sample span in common")
    ch = trades_ch[_in_spans(trades_ch["decision_ts"], common)]
    cp = trades_cp[_in_spans(trades_cp["decision_ts"], common)]
    lo, hi = min(s for s, _ in common), max(e for _, e in common)
    grid = block_grid(lo, hi, pd.Timedelta(days=block_days))
    starts = draw_block_starts(grid, reps, np.random.default_rng(seed))  # ONE draw: the same blocks for both models
    _, sum_ch = BlockedValues.place(grid, ch["decision_ts"], ch["pnl_eur"].to_numpy()).window_sums(starts)
    _, sum_cp = BlockedValues.place(grid, cp["decision_ts"], cp["pnl_eur"].to_numpy()).window_sums(starts)
    d = sum_ch - sum_cp
    days = sum((e - s) / DAY for s, e in common)
    return PairedOos(
        days, oos_metrics(trades_ch, common, starting_quote), oos_metrics(trades_cp, common, starting_quote),
        float(d.mean()), float(np.quantile(d, q)),
    )  # fmt: skip


def compare_models(
    challenger_dir: Path, champion_dir: Path, *, champion_go: bool, reps: int, seed: int, q: float,
    starting_quote: float,
) -> OosEvidence | None:  # fmt: skip
    """The ``OosEvidence`` of rule L5 for two registered versions. None: the challenger has no OOS artifacts. A
    champion that is not GO (or has no artifacts) leaves the paired fields None: P2 does not apply to it, or L5 says
    ``paired_oos_unavailable``."""
    ch = load_oos_artifacts(challenger_dir)
    if ch is None:
        return None
    own = oos_metrics(ch.trades, ch.spans, starting_quote)
    days = sum((e - s) / DAY for s, e in ch.spans)
    cp = load_oos_artifacts(champion_dir) if champion_go else None
    if cp is None or not common_spans(ch.spans, cp.spans):
        return OosEvidence(days, own.n_trades, own.max_dd_frac, None, None, own.sharpe, None, None)
    paired = paired_oos_comparison(
        ch.trades, cp.trades, ch.spans, cp.spans, max(ch.block_days, cp.block_days), reps, seed, q, starting_quote
    )
    return OosEvidence(
        paired.common_days, paired.challenger.n_trades, paired.challenger.max_dd_frac, paired.champion.n_trades,
        paired.d_pnl_quantile, paired.challenger.sharpe, paired.champion.sharpe, paired.champion.max_dd_frac,
    )  # fmt: skip
