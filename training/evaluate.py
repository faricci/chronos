"""Evaluation gates G0, G1, G2, GE, G3, G4 (spec sez. 3.4, decision D of docs/decisions/2026-10-18-fattibilita.md).

::

    python -m training.evaluate --config config/train.yml --model models/baseline/<version> [--dataset <run>]

Every gate is computed and printed; nothing stops at the first failure. Exit code 0: everything passes and GE = GO;
5: everything passes and GE = INCONCLUSO; 1 otherwise.

The absolute-evidence gate GE answers "is there an edge at all?" with the trades of the walk-forward backtests (every
fold with its own model and its own thresholds, concatenated) and of the holdout:

- E1 sample size: >= 150 OOS trades and >= 100 effective trades (trades of different products overlapping in time are
  correlated: weight ``1 / (1 + rho * c)``);
- E2 significance: 2.5th percentile of a circular block bootstrap (blocks of ``ceil_days(F + T_in - 1 + H)``, all
  products in the same time block) of the mean net return per trade > 0, and the OOS PnL in EUR > 0;
- E3 selection: permutation test against random entries of the same fold and product (with fill and fee floor, one
  position per product, same number of trades): ``p <= 0.05``; random entries inherit the drift of the period, so
  only the ability to pick entries passes;
- E4 concentration: no fold above 50% of the OOS PnL and at least 3 of 5 folds with PnL > 0;
- E5 holdout: >= 5 trades, PnL > 0 and consistent with the OOS (``z > -1.96``); a holdout WITHOUT trades is
  E5 = False (``holdout_no_trades``), with 1..4 trades it is not evaluable.

A fold (or the holdout) whose policy is not tunable runs as "no entry": zero trades, PnL 0.0, and 0.0 is not
positive for E4 (decision P1c: a flat fold is never treated as neutral). ``pnl_by_fold`` always lists the five
folds. E1..E3 use only the trades that exist (no fictitious zeros). The trivial baselines (B0 buy and hold,
B2 always invested with the same bracket) are report-only: they change no gate and no verdict.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Literal

import numba
import numpy as np
import pandas as pd

from modules.config import EXIT_CONFIG_ERROR
from modules.decision.baseline import MARKET_FEATURES, BaselineDecider, Calibration
from modules.decision.policy import PolicyConfig
from modules.decision.questions import build_questions
from modules.risk.costs import CostModel, net_return
from modules.risk.label_config import LabelConfig, LoadedLabelConfig, load_label_config
from training import feasibility as fz
from training.backtest import (
    EVAL_DECISION_EVERY_MIN,
    TUNE_DECISION_EVERY_MIN,
    BacktestResult,
    FlatProbCache,
    LongProbCache,
    ProductMarket,
    Span,
    decision_rows,
    run_backtest,
)
from training.baseline_lgbm import FINAL, _edge_frame, _select, ece, latest_run, logloss_binary, set_membership
from training.config import TrainConfig, load_train_config_or_exit
from training.fold_spans import SpanSet, compute_spans
from training.labeling import NO_FILL, label_window_end, triple_barrier
from training.sampling import hourly_grid
from training.tune_policy import backtest_config, load_markets, probe_registry

E_MIN_TRADES: Final = 150
E_MIN_NEFF: Final = 100
N_BOOT: Final = 10_000
N_PERM: Final = 10_000
SEED: Final = 1234
T_INCONCLUSIVE: Final = 1.0
PERM_P_MAX: Final = 0.05
FOLD_SHARE_MAX: Final = 0.50
MIN_POSITIVE_FOLDS: Final = 3
Z_HOLDOUT_MIN: Final = -1.96
MIN_HOLDOUT_TRADES: Final = 5
ECE_MAX: Final = 0.05
G2_MIN_FOLDS: Final = 4
EXIT_INCONCLUSIVE: Final = 5
OOS_TRADES: Final = "oos_trades.jsonl"
OOS_SPANS: Final = "oos_spans.json"
OOS_SPANS_SCHEMA: Final = "chronos.oos_spans.v1"
OOS_TRADE_FIELDS: Final = (
    "product", "fold", "decision_ts", "fill_ts", "exit_ts", "exit_reason", "net_ret_bps", "pnl_eur", "notional_eur",
    "neff_weight",
)  # fmt: skip

Status = Literal["GO", "INCONCLUSO", "NO-GO"]


# ---------------------------------------------------------------------------------- statistics


def _utc_value(ts: pd.Timestamp) -> int:
    t = pd.Timestamp(ts)
    return int((t.tz_convert(None) if t.tzinfo is not None else t).value)


def _ns(x: Any) -> np.ndarray:
    """Instants as int64 nanoseconds of naive UTC, whether the input is tz-aware or not."""
    idx = pd.DatetimeIndex(x)
    naive = idx.tz_convert(None) if idx.tz is not None else idx
    return np.asarray(naive.as_unit("ns")).astype("datetime64[ns]").astype(np.int64)


def effective_trades(trades: pd.DataFrame, rho: float) -> float:
    """``sum 1 / (1 + rho * c_i)``, ``c_i`` = number of OTHER products with a trade overlapping ``[fill_i, exit_i]``.

    Three products always trading together count ``3 / (1 + 2 rho)``; disjoint trades count one each. Overlaps of the
    same product (fold boundaries) are ignored.
    """
    return float(sum(trade_weights(trades, rho).tolist()))


def trade_weights(trades: pd.DataFrame, rho: float) -> np.ndarray:
    """The E1 weight of every trade (input order): ``1 / (1 + rho * c_i)``; their sum is ``effective_trades``."""
    if trades.empty:
        return np.empty(0, dtype=np.float64)
    products = sorted(set(trades["product"]))
    by_product: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for p in products:
        g = trades[trades["product"] == p].sort_values("fill_ts")
        starts, ends = _ns(g["fill_ts"]), _ns(g["exit_ts"])
        by_product[p] = (starts, np.maximum.accumulate(ends))
    out: list[float] = []
    for p, f, e in zip(trades["product"], trades["fill_ts"], trades["exit_ts"], strict=True):
        f_ns, e_ns = _utc_value(f), _utc_value(e)
        c = 0
        for q, (starts, run_max) in by_product.items():
            if q == p:
                continue
            k = int(np.searchsorted(starts, e_ns, side="left")) - 1  # last interval starting before this one ends
            if k >= 0 and run_max[k] > f_ns:
                c += 1
        out.append(1.0 / (1.0 + rho * c))
    return np.asarray(out, dtype=np.float64)


@dataclass(frozen=True)
class BlockGrid:
    """The circle of a span cut into the blocks of the circular block bootstrap. ONE definition: E2
    (``block_bootstrap``) and the paired comparison of rule L5 (``training.oos_compare``) draw and assign blocks
    through it."""

    span_ns: int
    block_ns: int
    n_blocks: int  # blocks drawn per replicate: ceil(span / block)
    start_ns: int  # the circle starts here (naive UTC nanoseconds)


def block_grid(span_start: pd.Timestamp, span_end: pd.Timestamp, block: pd.Timedelta) -> BlockGrid:
    span_ns = max(int(_utc_value(span_end) - _utc_value(span_start)), 1)
    block_ns = min(int(block.value), span_ns)
    return BlockGrid(span_ns, block_ns, max(1, math.ceil(span_ns / block_ns)), _utc_value(span_start))


def draw_block_starts(grid: BlockGrid, n_rep: int, rng: np.random.Generator) -> np.ndarray:
    """Random block starts on the hourly grid of the span: shape ``(n_rep, n_blocks)``, nanoseconds on the circle."""
    hour = 3_600_000_000_000
    return np.asarray(rng.integers(0, max(grid.span_ns // hour, 1), size=(n_rep, grid.n_blocks)) * hour)


@dataclass(frozen=True)
class BlockedValues:
    """Values placed on the circle of a ``BlockGrid`` (sorted by position, doubled for the wrap-around) with prefix
    sums, so that the count and the sum inside any block are two lookups."""

    xs2: np.ndarray
    pv: np.ndarray
    block_ns: int

    @classmethod
    def place(cls, grid: BlockGrid, ts: Any, values: np.ndarray) -> BlockedValues:
        """``ts``: the instant that assigns each value to a block (E2: the fill of the trade)."""
        x = (_ns(ts) - grid.start_ns) % grid.span_ns
        order = np.argsort(x, kind="stable")
        xs, vs = x[order], np.asarray(values, dtype=np.float64)[order]
        xs2 = np.concatenate([xs, xs + grid.span_ns])
        pv = np.concatenate([[0.0], np.cumsum(np.concatenate([vs, vs]))])
        return cls(xs2, pv, grid.block_ns)

    def window_sums(self, starts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """``(count, sum)`` of the values in the blocks ``[u, u + block)`` of every replicate (rows of ``starts``)."""
        lo = np.searchsorted(self.xs2, starts, side="left")
        hi = np.searchsorted(self.xs2, starts + self.block_ns, side="left")
        return (hi - lo).sum(axis=1), (self.pv[hi] - self.pv[lo]).sum(axis=1)


@dataclass(frozen=True)
class Boot:
    mean: float
    se: float
    q025: float
    q975: float
    t: float
    n: int


def block_bootstrap(
    values_bps: np.ndarray, ts: np.ndarray, span_start: pd.Timestamp, span_end: pd.Timestamp, block: pd.Timedelta,
    n_rep: int, seed: int,
) -> Boot:  # fmt: skip
    """Circular block bootstrap of the mean over TIME: every replicate draws ``ceil(span / block)`` random block
    starts on the hourly grid of the span and takes all trades whose fill falls in each block (all products share
    the blocks)."""
    v = np.asarray(values_bps, dtype=np.float64)
    n = len(v)
    if n == 0:
        return Boot(math.nan, math.nan, math.nan, math.nan, math.nan, 0)
    grid = block_grid(span_start, span_end, block)
    placed = BlockedValues.place(grid, ts, v)
    rng = np.random.default_rng(seed)
    means: list[np.ndarray] = []
    got = 0
    while got < n_rep:
        cnt, tot = placed.window_sums(draw_block_starts(grid, n_rep, rng))
        ok = cnt > 0
        means.append(tot[ok] / cnt[ok])  # an empty replicate is redrawn
        got += int(ok.sum())
    rep = np.concatenate(means)[:n_rep]
    mean = float(v.mean())
    se = float(rep.std(ddof=1))
    t = (math.inf if mean > 0 else -math.inf if mean < 0 else 0.0) if se == 0 else mean / se
    return Boot(mean, se, float(np.quantile(rep, 0.025)), float(np.quantile(rep, 0.975)), t, n)


@numba.njit(cache=True)
def _perm_kernel(  # pragma: no cover - compiled
    starts: np.ndarray, ends: np.ndarray, vals: np.ndarray, offs: np.ndarray, targets: np.ndarray,
    n_rep: int, seed: int,
    out_mean: np.ndarray, out_short: np.ndarray,
) -> None:  # fmt: skip
    np.random.seed(seed)
    n_sets = offs.shape[0] - 1
    for r in range(n_rep):
        total = 0.0
        count = 0
        short = 0
        for s in range(n_sets):
            lo, hi = offs[s], offs[s + 1]
            want = targets[s]
            if want == 0:
                continue
            perm = np.random.permutation(hi - lo)
            acc_s = np.empty(want, dtype=np.int64)
            acc_e = np.empty(want, dtype=np.int64)
            got = 0
            for k in range(perm.shape[0]):
                i = lo + perm[k]
                ok = True
                for j in range(got):
                    if starts[i] <= acc_e[j] and ends[i] >= acc_s[j]:
                        ok = False
                        break
                if ok:
                    acc_s[got] = starts[i]
                    acc_e[got] = ends[i]
                    got += 1
                    total += vals[i]
                    count += 1
                    if got == want:
                        break
            short += want - got
        out_mean[r] = total / count if count > 0 else np.nan
        out_short[r] = short


@dataclass(frozen=True)
class Candidates:
    t_idx: np.ndarray
    exit_idx: np.ndarray
    label_bps: np.ndarray


@dataclass(frozen=True)
class PermResult:
    p: float
    shortfall: float  # mean number of trades per replicate that could not be placed (reported, never a failure)
    n_rep: int


def permutation_pvalue(
    model_label_bps: np.ndarray, model_counts: Mapping[tuple[int, str], int],
    candidates: Mapping[tuple[int, str], Candidates], n_rep: int, seed: int,
) -> PermResult:  # fmt: skip
    """P(random entries do at least as well as the model), measured on the label net return of the decision bar.

    For every (fold, product) the null draws as many entries as the model took from the candidates of that fold and
    product (fill and fee floor passed), never overlapping each other; the statistic is the pooled mean.
    """
    keys = sorted(k for k, c in model_counts.items() if c > 0 and k in candidates and len(candidates[k].t_idx))
    m_model = float(np.mean(model_label_bps)) if len(model_label_bps) else math.nan
    if not keys:
        return PermResult(math.nan, 0.0, n_rep)
    starts = np.concatenate([candidates[k].t_idx for k in keys]).astype(np.int64)
    ends = np.concatenate([candidates[k].exit_idx for k in keys]).astype(np.int64)
    vals = np.concatenate([candidates[k].label_bps for k in keys]).astype(np.float64)
    offs = np.concatenate([[0], np.cumsum([len(candidates[k].t_idx) for k in keys])]).astype(np.int64)
    targets = np.array([model_counts[k] for k in keys], dtype=np.int64)
    out_mean = np.empty(n_rep)
    out_short = np.empty(n_rep)
    _perm_kernel(starts, ends, vals, offs, targets, n_rep, seed, out_mean, out_short)
    valid = ~np.isnan(out_mean)
    p = (1 + int((out_mean[valid] >= m_model).sum())) / (1 + int(valid.sum()))
    return PermResult(float(p), float(out_short.mean()), int(valid.sum()))


def concentration(pnl_eur_by_fold: Mapping[int, float]) -> tuple[float, int]:
    """``(largest share of the total PnL held by one fold, folds with PnL > 0)``; the share is inf if the total <= 0."""
    total = sum(pnl_eur_by_fold.values())
    n_pos = sum(1 for v in pnl_eur_by_fold.values() if v > 0)
    if total <= 0:
        return math.inf, n_pos
    return max(pnl_eur_by_fold.values()) / total, n_pos


def holdout_consistency(boot_h: Boot, boot_oos: Boot) -> float:
    """``z = (m_h - m_oos) / sqrt(se_h^2 + se_oos^2)``: how far the holdout mean is from the OOS mean."""
    den = math.sqrt(boot_h.se**2 + boot_oos.se**2)
    return (boot_h.mean - boot_oos.mean) / den if den > 0 else math.inf


@dataclass(frozen=True)
class EdgeVerdict:
    status: Status
    checks: dict[str, bool | None]
    reasons: list[str] = field(default_factory=list)


def edge_status(g0_ok: bool, checks: Mapping[str, bool | None], mean_oos: float, t_oos: float) -> EdgeVerdict:
    """The six ordered rules of the resolution (D3). ``checks``: E1..E5; ``None`` = not evaluable (E5 only)."""
    e = dict(checks)
    e1, e2, e3, e4, e5 = (e.get(k) for k in ("E1", "E2", "E3", "E4", "E5"))
    if not g0_ok:
        return EdgeVerdict("NO-GO", e, ["regime_change"])
    if not e1:
        return EdgeVerdict("INCONCLUSO" if mean_oos > 0 else "NO-GO", e, ["too_few_trades"])
    if e2 and e3 and e4 and e5:
        return EdgeVerdict("GO", e, [])
    if e3 and e4 and e5 is not False and not e2 and t_oos >= T_INCONCLUSIVE:
        return EdgeVerdict("INCONCLUSO", e, ["E2_below_significance"])
    if e2 and e3 and e4 and e5 is None:
        return EdgeVerdict("INCONCLUSO", e, ["holdout_not_evaluable"])
    return EdgeVerdict("NO-GO", e, [k for k, v in e.items() if v is False])


# ------------------------------------------------------------------------------ gate inputs


def candidates_for(
    pm: ProductMarket, label: LoadedLabelConfig, span: Span, every_min: int, min_net_edge_bps: float
) -> Candidates:
    """Entries a random strategy could take in ``span``: decision bars with a fill and the fee floor passed."""
    rows = decision_rows(pm, label, span, every_min)
    if rows.size == 0:
        return Candidates(np.empty(0, np.int64), np.empty(0, np.int64), np.empty(0))
    cfg = label.cfg
    mb = pm.bars
    ts = pd.DatetimeIndex(mb.ts_close[rows])
    spread = np.array([label.spread_for(pm.name, t.to_pydatetime()) for t in ts], dtype=np.float64)
    r = triple_barrier(mb.o, mb.h, mb.lo, mb.c, pm.atr, rows, cfg, label.fees, label.costs, spread)
    atr_bps = pm.atr[rows] / mb.c[rows] * 1e4
    cost_tp = np.array(
        [
            CostModel(fees=label.fees, entry_mode=cfg.entry_mode, spread_bps=float(x), params=label.costs).cost_tp
            for x in spread
        ]
    )
    ok = (r.outcome != NO_FILL) & (cfg.k_tp * atr_bps - cost_tp >= min_net_edge_bps)
    return Candidates(rows[ok], r.exit_idx[ok].astype(np.int64), 1e4 * r.net_ret[ok])


def regime_check(
    markets: Mapping[str, ProductMarket], label: LoadedLabelConfig, min_net_edge_bps: float
) -> dict[str, Any]:  # fmt: skip
    """G0: the lift of the label_config's combination on the holdout (raw bars) is still compatible with the study."""
    cfg = label.cfg
    last_close = max(pd.Timestamp(pm.bars.ts_close[-1]) for pm in markets.values())
    products: list[fz.ProductData] = []
    for name, pm in markets.items():
        mb = pm.bars
        ts = pd.DatetimeIndex(mb.ts_close)
        grid = hourly_grid(ts - pd.Timedelta(minutes=1))
        end = label_window_end(ts[grid], cfg)
        keep = (ts[grid] >= pd.Timestamp(label.holdout_start).tz_convert(None)) & (end <= last_close)
        keep &= np.isfinite(pm.atr[grid])
        arr = fz.BarArrays(
            mb.o, mb.h, mb.lo, mb.c, {tf: pm.ind[f"atr_14_{tf}"].to_numpy(dtype=np.float64) for tf in fz.ATR_TFS}
        )
        products.append(
            fz.ProductData(name, label.spread_for(name, ts[0].to_pydatetime()), mb.ts_close, grid[keep], arr, arr, 0.0)
        )
    raws = fz.label_all(cfg, products, label.fees, label.costs, min_net_edge_bps, "raw")
    rho = float(label.raw.get("feasibility", {}).get("rho_raw", 1.0))
    n_eff = fz.pooled_n_eff([fz.product_n_eff(r) for r in raws.values()], rho)
    m = fz._metrics(list(raws.values()), cfg, n_eff)
    se = math.sqrt(m.p_hist * (1 - m.p_hist) / n_eff) if n_eff > 0 else math.inf
    return {"lift_holdout": m.lift, "se": se, "n_eff": n_eff, "p_hist": m.p_hist, "p_breakeven": m.p_breakeven,
            "limit": fz.LIFT_MAX + 2 * se, "passed": bool(m.lift <= fz.LIFT_MAX + 2 * se)}  # fmt: skip


def edge_gates(
    samples: pd.DataFrame,
    label: LoadedLabelConfig,
    cfg: TrainConfig,
    model_dir: Path,
    policies: Mapping[str, PolicyConfig | None],
) -> dict[str, Any]:  # fmt: skip
    """G1 (ECE) and G2 (edge logloss vs the train base rate) per fold test, plus the decision-region diagnostics."""
    import lightgbm as lgb

    members = set_membership(samples, label, cfg)
    rho = float(label.raw.get("feasibility", {}).get("rho_raw", 1.0))
    folds: dict[str, Any] = {}
    for name, m in members.items():
        if name == FINAL:
            continue
        tr = _select(samples, m == "train", "edge", MARKET_FEATURES)
        te = _select(samples, m == "test", "edge", MARKET_FEATURES)
        if not len(te.y):
            continue
        cal = Calibration.model_validate_json((model_dir / name / "calibration.json").read_text(encoding="utf-8"))
        z = np.asarray(lgb.Booster(model_file=str(model_dir / name / "edge.txt")).predict(te.x, raw_score=True))
        p, y = cal.edge_probs(z), te.y.astype(float)
        base = float((tr.w * tr.y).sum() / tr.w.sum())
        pol = policies.get(name)
        pbe = te.x[:, MARKET_FEATURES.index("p_breakeven")]  # the state token the policy and the RiskManager see
        region = (p >= pbe + pol.p_be_buffer) if pol is not None else np.zeros(len(p), dtype=bool)
        raw_w = _edge_frame(samples, m == "test")["w_edge"].to_numpy(dtype=np.float64)
        n_eff_test = float(raw_w.sum() / (1.0 + (len(set(samples["product"])) - 1) * rho))
        p_obs = float((te.w * y).sum() / te.w.sum())
        se = math.sqrt(max(p_obs * (1 - p_obs), 1e-12) / max(n_eff_test, 1e-12))
        bias_all = float((te.w * (p - y)).sum() / te.w.sum())
        entry: dict[str, Any] = {
            "edge_ece": ece(p, y, te.w), "edge_logloss": logloss_binary(p, y, te.w),
            "prior_logloss": logloss_binary(np.full(len(y), base), y, te.w), "n_edge": int(len(y)),
            "bias_all": bias_all, "n_eff_test": n_eff_test, "se": se, "bias_z": bias_all / se,
        }  # fmt: skip
        if region.any():
            w = te.w[region]
            entry["region_ece"] = ece(p[region], y[region], w)
            entry["region_bias"] = float((w * (p[region] - y[region])).sum() / w.sum())
            entry["region_n"] = int(region.sum())
        folds[name] = entry
    return folds


# ------------------------------------------------------------------------ trivial baselines (report only)


def buy_and_hold(markets: Mapping[str, ProductMarket], label: LoadedLabelConfig, span: Span) -> dict[str, Any]:
    """B0: equal-weight buy and hold of the products over ``span`` (one round trip, no rebalancing).

    Entry at the close of the first bar with the maker fee, exit at the close of the last bar with the taker fee, half
    the spread and the slippage buffer; exact ``net_return``. Reports the return, the maximum drawdown of the
    equal-weight daily curve and the return per day of exposure.
    """
    start_ns, end_ns = _utc_value(span.entry_start), _utc_value(span.entry_end)
    rets: list[float] = []
    curves: list[np.ndarray] = []
    for name, pm in markets.items():
        t_ns = pm.bars.ts_close.astype("datetime64[ns]").astype(np.int64)
        idx = np.flatnonzero((t_ns >= start_ns) & (t_ns < end_ns))
        if idx.size < 2:
            continue
        first, last = int(idx[0]), int(idx[-1])
        s = label.spread_for(name, pd.Timestamp(pm.bars.ts_close[first]).to_pydatetime())
        entry = Decimal(repr(float(pm.bars.c[first])))
        exit_ = Decimal(repr(float(pm.bars.c[last]) * (1.0 - (s / 2 + label.costs.slippage_buffer_bps) / 1e4)))
        rets.append(1e4 * float(net_return(entry, exit_, label.fees.maker_bps, label.fees.taker_bps)))
        curves.append(pm.bars.c[idx[::1440]] / pm.bars.c[first])
    if not rets:
        return {"n_products": 0}
    k = min(len(c) for c in curves)
    curve = np.mean([c[:k] for c in curves], axis=0)
    days = max((end_ns - start_ns) / 86_400e9, 1e-9)
    mdd = float(np.max(1.0 - curve / np.maximum.accumulate(curve)))
    return {"n_products": len(rets), "return_bps": float(np.mean(rets)), "days": days,
            "bps_per_day": float(np.mean(rets)) / days, "max_drawdown": mdd}  # fmt: skip


def always_invested(cand: Mapping[tuple[int, str], Candidates]) -> dict[str, Any]:
    """B2: the strategy "enter whenever flat" with the same bracket: greedy chain over the candidates of every
    (fold, product): the first candidate, then the first one starting at or after the exit of the previous."""
    n, total = 0, 0.0
    for c in cand.values():
        next_free = -1
        for i in np.argsort(c.t_idx, kind="stable"):
            if int(c.t_idx[i]) >= next_free:
                n += 1
                total += float(c.label_bps[i])
                next_free = int(c.exit_idx[i])
    return {"n": n, "mean_label_bps": total / n if n else None}


# ---------------------------------------------------------------------------------- the run


@dataclass
class Evaluation:
    gates: dict[str, Any] = field(default_factory=dict)
    ok_except_ge: bool = False
    status: Status = "NO-GO"
    exit_code: int = 1
    oos_trades: pd.DataFrame = field(default_factory=pd.DataFrame)  # the concatenated OOS trades GE was computed on
    oos_spans: dict[str, Any] = field(default_factory=dict)  # their test spans, for ``oos_spans.json``


def oos_block(cfg: LabelConfig) -> pd.Timedelta:
    """Length of the bootstrap blocks of E2 (and of the paired comparison of rule L5): ``ceil_days(T_in + F + H)``."""
    return pd.Timedelta(days=math.ceil((cfg.entry_first_bar + cfg.entry_timeout_min - 1 + cfg.horizon_min) / 1440))


def _iso(ts: Any) -> str:
    t = pd.Timestamp(ts)
    return (t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")).isoformat()


def oos_spans_doc(spans: SpanSet, label: LoadedLabelConfig, rho: float) -> dict[str, Any]:
    """Content of ``oos_spans.json``: the test span ``[start, end)`` of every fold, the ``label_config`` it was run
    under and the bootstrap block length (days)."""
    return {
        "v": OOS_SPANS_SCHEMA, "label_config_sha256": label.sha256, "rho": rho, "block_days": oos_block(label.cfg).days,
        "spans": [
            {"fold": f.index, "start": _iso(f.test.entry_start), "end": _iso(f.test.entry_end)} for f in spans.folds
        ],
    }  # fmt: skip


def oos_trade_rows(trades: pd.DataFrame, rho: float) -> list[dict[str, Any]]:
    """The rows of ``oos_trades.jsonl``: one OOS trade per row with its E1 weight (``neff_weight``)."""
    if trades.empty:
        return []
    weights = trade_weights(trades, rho)
    rows: list[dict[str, Any]] = []
    for i, rec in enumerate(trades.to_dict("records")):
        row = {k: rec[k] for k in OOS_TRADE_FIELDS if k not in ("neff_weight",)}
        for k in ("decision_ts", "fill_ts", "exit_ts"):
            row[k] = _iso(row[k])
        row["fold"] = int(row["fold"])
        for k in ("net_ret_bps", "pnl_eur", "notional_eur"):
            row[k] = float(row[k])
        row["neff_weight"] = float(weights[i])
        rows.append(row)
    return rows


def write_oos_artifacts(model_dir: Path, ev: Evaluation) -> None:
    """``oos_trades.jsonl`` and ``oos_spans.json`` next to ``evaluation.json``: they become part of the immutable
    registered version (the learner registers AFTER ``evaluate``) and feed the paired comparison of rule L5."""
    rho = float(ev.oos_spans.get("rho", 1.0))
    lines = [json.dumps(r, sort_keys=True, allow_nan=False) for r in oos_trade_rows(ev.oos_trades, rho)]
    (model_dir / OOS_TRADES).write_text("".join(f"{ln}\n" for ln in lines), encoding="utf-8", newline="\n")
    (model_dir / OOS_SPANS).write_text(
        json.dumps(ev.oos_spans, indent=2, sort_keys=True, allow_nan=False), encoding="utf-8", newline="\n"
    )


async def evaluate_model(
    model_dir: Path,
    samples: pd.DataFrame,
    cfg: TrainConfig,
    label: LoadedLabelConfig,
    markets: Mapping[str, ProductMarket],
    registry: Any, spans: SpanSet, app: Any, cache_dir: Path | None = None,
) -> Evaluation:  # fmt: skip
    questions = build_questions(label.cfg.horizon_min, label.cfg.k_tp, label.cfg.k_sl, label.cfg.atr_tf)
    flat_cache, long_cache = FlatProbCache(cache_dir), LongProbCache()
    min_edge = app.risk.min_net_edge_bps
    bt1 = backtest_config(cfg, app, EVAL_DECISION_EVERY_MIN)
    bt15 = backtest_config(cfg, app, TUNE_DECISION_EVERY_MIN)
    policies: dict[str, PolicyConfig | None] = {}
    fold_status: dict[str, str] = {}
    fold_summary: dict[str, Any] = {}
    for sub in [f"fold_{f.index}" for f in spans.folds] + [FINAL]:
        raw = json.loads((model_dir / sub / "policy.json").read_text(encoding="utf-8"))
        policies[sub] = None if raw.get("untunable") else PolicyConfig.model_validate(raw)
        fold_status[sub] = str(raw.get("reason", "untunable")) if raw.get("untunable") else "ok"

    # ---- backtests
    oos: list[BacktestResult] = []
    sens: dict[str, Any] = {}
    cand: dict[tuple[int, str], Candidates] = {}
    for f in spans.folds:
        sub = f"fold_{f.index}"
        pol = policies[sub]
        for name, pm in markets.items():
            cand[(f.index, name)] = candidates_for(pm, label, f.test, EVAL_DECISION_EVERY_MIN, min_edge)
        if pol is None:
            print(f"{sub}: {fold_status[sub]}, no entries (PnL 0.00, not positive)", flush=True)
            fold_summary[sub] = {"trades": 0, "pnl_eur": 0.0}
            continue
        decider = BaselineDecider.load(model_dir, label=label, questions=questions, subset=sub, allow_degenerate=True)
        res = await run_backtest(markets, decider, pol, label, f.test, bt1, registry, flat_cache, long_cache)
        res15 = await run_backtest(markets, decider, pol, label, f.test, bt15, registry, flat_cache, long_cache)
        oos.append(res)
        sens[sub] = {
            "trades_1min": len(res.trades), "trades_15min": len(res15.trades),
            "mean_bps_1min": float(res.trades["net_ret_bps"].mean()) if len(res.trades) else None,
            "mean_bps_15min": float(res15.trades["net_ret_bps"].mean()) if len(res15.trades) else None,
        }  # fmt: skip
        pnl_f = float(res.trades["pnl_eur"].sum()) if len(res.trades) else 0.0
        fold_summary[sub] = {"trades": len(res.trades), "pnl_eur": pnl_f}
        print(f"{sub}: {len(res.trades)} trades, PnL {pnl_f:.2f} EUR", flush=True)
    trades = pd.concat([r.trades for r in oos], ignore_index=True) if oos else pd.DataFrame()

    hold = pd.DataFrame()
    pol_final = policies[FINAL]
    if pol_final is not None:
        final = BaselineDecider.load(model_dir, label=label, questions=questions, subset=FINAL)
        hold = (
            await run_backtest(markets, final, pol_final, label, spans.holdout, bt1, registry, flat_cache, long_cache)
        ).trades
        print(f"holdout: {len(hold)} trades, PnL {hold['pnl_eur'].sum() if len(hold) else 0:.2f} EUR", flush=True)
    else:
        print(f"final: {fold_status[FINAL]}, holdout without entries", flush=True)
    fold_summary[FINAL] = {"trades": len(hold), "pnl_eur": float(hold["pnl_eur"].sum()) if len(hold) else 0.0}

    # ---- gates
    g0 = regime_check(markets, label, min_edge)
    folds = edge_gates(samples, label, cfg, model_dir, policies)
    g1 = {k: v["edge_ece"] <= ECE_MAX for k, v in folds.items()}
    g2 = {k: v["edge_logloss"] < v["prior_logloss"] for k, v in folds.items()}
    ge = _ge(
        trades, hold, cand, label, spans, g0["passed"], float(label.raw.get("feasibility", {}).get("rho_raw", 1.0))
    )

    b0 = {f"fold_{f.index}": buy_and_hold(markets, label, f.test) for f in spans.folds}
    b0["holdout"] = buy_and_hold(markets, label, spans.holdout)
    gates = {"G0": g0, "G1": {"per_fold": g1, "passed": all(g1.values()) and bool(g1)},
             "G2": {"per_fold": g2, "n_better": sum(g2.values()), "passed": sum(g2.values()) >= G2_MIN_FOLDS},
             "GE": ge, "G3": {"applicable": False, "note": "the candidate is the baseline"},
             "G4": {"passed": True, "note": "trivial: sentiment_available=false"},
             "edge_gates_per_fold": folds, "cadence_sensitivity": sens,
             "fold_status": fold_status, "fold_summary": fold_summary,
             "baselines": {"B0_buy_and_hold": b0, "B2_always_invested": always_invested(cand)}}  # fmt: skip
    others = g0["passed"] and gates["G1"]["passed"] and gates["G2"]["passed"]
    status: Status = ge["status"]
    code = 0 if others and status == "GO" else EXIT_INCONCLUSIVE if others and status == "INCONCLUSO" else 1
    rho = float(label.raw.get("feasibility", {}).get("rho_raw", 1.0))
    return Evaluation(gates, others, status, code, trades, oos_spans_doc(spans, label, rho))


def _ge(
    trades: pd.DataFrame, hold: pd.DataFrame, cand: Mapping[tuple[int, str], Candidates], label: LoadedLabelConfig,
    spans: SpanSet, g0_ok: bool, rho: float,
) -> dict[str, Any]:  # fmt: skip
    block = oos_block(label.cfg)
    n = len(trades)
    out: dict[str, Any] = {"n_oos_trades": n, "n_holdout_trades": len(hold), "block_days": block.days}
    pnl_by_fold: dict[int, float] = {f.index: 0.0 for f in spans.folds}  # a fold without trades counts as 0.0
    if n == 0:
        out.update(
            status=edge_status(g0_ok, {"E1": False}, 0.0, 0.0).status,
            checks={"E1": False},
            reasons=["no_trades"],
            pnl_by_fold=pnl_by_fold,
        )
        return out
    start, end = spans.folds[0].test.entry_start, spans.holdout_start
    neff = effective_trades(trades, rho)
    boot = block_bootstrap(
        trades["net_ret_bps"].to_numpy(), trades["fill_ts"].to_numpy(), start, end, block, N_BOOT, SEED
    )
    counts: dict[tuple[int, str], int] = {}
    for fold, product in zip(trades["fold"], trades["product"], strict=True):
        counts[(int(fold), str(product))] = counts.get((int(fold), str(product)), 0) + 1
    perm = permutation_pvalue(trades["label_net_bps"].to_numpy(), counts, cand, N_PERM, SEED)
    for fold, pnl in zip(trades["fold"], trades["pnl_eur"], strict=True):
        pnl_by_fold[int(fold)] = pnl_by_fold.get(int(fold), 0.0) + float(pnl)
    share, n_pos = concentration(pnl_by_fold)
    checks: dict[str, bool | None] = {
        "E1": n >= E_MIN_TRADES and neff >= E_MIN_NEFF,
        "E2": bool(boot.q025 > 0 and trades["pnl_eur"].sum() > 0),
        "E3": bool(perm.p <= PERM_P_MAX),
        "E4": bool(share <= FOLD_SHARE_MAX and n_pos >= MIN_POSITIVE_FOLDS),
        "E5": None,
    }
    z = math.nan
    if len(hold) >= MIN_HOLDOUT_TRADES:
        h_start = spans.holdout.entry_start
        boot_h = block_bootstrap(
            hold["net_ret_bps"].to_numpy(),
            hold["fill_ts"].to_numpy(),
            h_start,
            spans.holdout.entry_end,
            block,
            N_BOOT,
            SEED,
        )
        z = holdout_consistency(boot_h, boot)
        checks["E5"] = bool(hold["pnl_eur"].sum() > 0 and z > Z_HOLDOUT_MIN)
    elif len(hold) == 0:
        checks["E5"] = False  # a holdout that never trades is not evidence of anything
    verdict = edge_status(g0_ok, checks, boot.mean, boot.t)
    if len(hold) == 0 and "E5" in verdict.reasons:
        verdict.reasons.append("holdout_no_trades")
    out.update(
        status=verdict.status, checks=checks, reasons=verdict.reasons, n_eff_trades=neff,
        bootstrap={"mean_bps": boot.mean, "se": boot.se, "q025": boot.q025, "q975": boot.q975, "t": boot.t},
        permutation={"p": perm.p, "shortfall": perm.shortfall}, pnl_by_fold=pnl_by_fold, max_fold_share=share,
        n_positive_folds=n_pos, holdout_z=z, oos_pnl_eur=float(trades["pnl_eur"].sum()),
        holdout_pnl_eur=float(hold["pnl_eur"].sum()) if len(hold) else 0.0,
    )  # fmt: skip
    return out


def render(ev: Evaluation, model: str) -> str:
    g = ev.gates
    ge, g0 = g["GE"], g["G0"]
    mark = {True: "PASS", False: "FAIL"}
    lines = [f"# Evaluation of {model}", ""]
    lines.append(
        f"G0 regime        : {mark[g0['passed']]} (holdout lift {g0['lift_holdout']:+.3f}, limit {g0['limit']:.3f})"
    )
    lines.append(f"G1 calibration   : {mark[g['G1']['passed']]} {g['G1']['per_fold']}")
    lines.append(
        f"G2 discrimination: {mark[g['G2']['passed']]} ({g['G2']['n_better']} folds better than the base rate) "
        f"{g['G2']['per_fold']}"
    )
    lines.append(f"GE evidence      : {ge['status']} checks={ge.get('checks')} reasons={ge.get('reasons')}")
    lines.append(
        f"   OOS trades {ge['n_oos_trades']}, holdout trades {ge['n_holdout_trades']}, "
        f"N_eff {ge.get('n_eff_trades', 0):.0f}"
    )
    if "bootstrap" in ge:
        b = ge["bootstrap"]
        lines.append(
            f"   mean {b['mean_bps']:+.1f} bps/trade, q2.5 {b['q025']:+.1f}, t {b['t']:.2f}; "
            f"permutation p {ge['permutation']['p']:.4f}; folds PnL {ge['pnl_by_fold']}; holdout z {ge['holdout_z']}"
        )
    for sub_, st in g.get("fold_status", {}).items():
        sm = g.get("fold_summary", {}).get(sub_, {})
        lines.append(f"   {sub_:<7}: {st}, trades {sm.get('trades', 0)}, PnL {sm.get('pnl_eur', 0.0):+.2f} EUR")
    b = g.get("baselines", {})
    if b:
        lines.append(f"baselines (report only): B2 always invested {b['B2_always_invested']}")
        for k, v in b["B0_buy_and_hold"].items():
            lines.append(f"   B0 buy and hold {k}: {v}")
    lines.append(f"G3               : n/a ({g['G3']['note']})")
    lines.append(f"G4               : PASS ({g['G4']['note']})")
    lines.append(f"cadence sensitivity: {g['cadence_sensitivity']}")
    lines += ["", f"RESULT: {ev.status}, exit code {ev.exit_code}"]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Chronos evaluation gates (spec sez. 3.4)")
    ap.add_argument("--config", default="config/train.yml")
    ap.add_argument("--model", required=True)
    ap.add_argument("--dataset", default=None)
    ap.add_argument("--probe", default="data/probe/coinbase_probe.json")
    args = ap.parse_args(argv)
    cfg = load_train_config_or_exit(args.config)
    try:
        label = load_label_config(Path(cfg.label_config))
    except (ValueError, OSError) as e:
        print(f"label_config invalid: {e}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    app = cfg.app()
    run_dir = Path(args.dataset) if args.dataset else latest_run(Path(cfg.datasets_dir))
    samples = pd.read_parquet(run_dir / "samples.parquet")
    markets = load_markets(Path(cfg.data_root) / "bars" / "1m", cfg.products, label)
    last_close = max(pd.Timestamp(pm.bars.ts_close[-1], tz="UTC") for pm in markets.values())
    spans = compute_spans(samples, label, cfg, last_close)
    registry = probe_registry(Path(args.probe), cfg.products)
    model_dir = Path(args.model)
    ev = asyncio.run(
        evaluate_model(
            model_dir, samples, cfg, label, markets, registry, spans, app, Path(cfg.data_root) / "backtest_cache"
        )
    )
    report = render(ev, model_dir.name)
    print(report)
    (model_dir / "evaluation.md").write_text(report, encoding="utf-8", newline="\n")
    write_oos_artifacts(model_dir, ev)  # BEFORE evaluation.json: its mtime is how the learner sees a fresh run
    (model_dir / "evaluation.json").write_text(
        json.dumps(ev.gates, indent=2, default=str), encoding="utf-8", newline="\n"
    )
    return ev.exit_code


if __name__ == "__main__":
    sys.exit(main())
