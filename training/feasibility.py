"""Feasibility study (spec sez. 3.1.3, v2.7): can any barrier combination beat the fees at all?

Run BEFORE any training::

    python -m training.feasibility --products BTC-EUR ETH-EUR SOL-EUR --fees-from-probe data/probe/coinbase_probe.json

Design (docs/decisions/2026-10-18-fattibilita.md, sections B and C):

- **Selection window (R-c1).** Only bars that close before ``cut = holdout_start - embargo`` are loaded; the holdout
  (last 120 days, frozen in ``label_config.json``) is never seen. One sample set serves every combination: hourly grid,
  labels resolved for the longest horizon of the grid.
- **Two labelings.** ``detr``: the same bars with the sample's average drift removed (private to this file, it never
  reaches the state, the training labels or the hash, vincolo C7); the choice and the GO use only this series.
  ``raw``: the real bars, for the diagnostics (raw lift, durations, sigma_r, ATR) and for the saturation check.
- **Lift without drift.** ``lift_0 = median(p_breakeven) - p_hist`` computed on ``detr``.
- **Independent samples.** ``N_p`` is the sum of the uniqueness weights over the real span ``[t, exit]`` of the filled
  samples; the three products are correlated, so the pool is ``sum N_p / (1 + (K - 1) rho)``.
- **Choice.** Eligible: ``H <= 15 d``, fee floor in >= 80% of the samples, maker fill >= 50%, ``p_vertical <= 5%``.
  Per family (atr_tf, k_tp, k_sl, entry) the smallest eligible H; among the families within 0.01 of the minimum
  ``lift_0`` the one with the largest ``N_eff``. H = 20 d is diagnostic only.
- **GO** = ``lift_0 <= 0.10`` and the semester jackknife ``<= 0.10`` and, for every product,
  ``lift_0,p <= 0.10 + 2 SE_p``. Otherwise exit code 3 and no ``label_config.json``.
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
from scipy import stats

from modules.config import AppConfig, load_config_or_exit
from modules.market.backfill import bars_root, load_bars
from modules.market.indicators_batch import compute_indicators
from modules.risk.costs import CostModel, CostParams, FeeRates
from training.labeling import (
    NO_FILL,
    UNRESOLVED,
    UPPER,
    VERTICAL,
    LabelConfig,
    label_window_end,
    triple_barrier,
)
from training.sampling import hourly_grid, uniqueness_weights
from training.splits import DEFAULT_EMBARGO, holdout_start_from

ATR_TFS = ("6h", "1d")
K_TP = (3.0, 4.0, 5.0)
K_SL = (1.5, 2.0)
ENTRY_MODES = ("maker", "taker")
HORIZON_DAYS = (5, 10, 15, 20)  # 20 is diagnostic only (R-a4): never eligible
HOLDOUT_DAYS = 120
H_MAX_DAYS = HOLDOUT_DAYS // 8  # R-a1
EMBARGO = DEFAULT_EMBARGO
FEE_FLOOR_SHARE_MIN = 0.80
MAKER_FILL_MIN = 0.50
P_VERTICAL_MAX = 0.05
LIFT_MAX = 0.10
LIFT_TIE = 0.01
SATURATION_TOL = 0.02
PRODUCT_SE_MULT = 2.0
LABEL_SPREAD_FLOOR_BPS = 3.0
CHI2_MIN_EXPECTED = 5.0
RHO_MIN_COMMON = 30
DAYS_PER_YEAR = 365.25
EXIT_NO_GO = 3
NMIN_DELTAS = (0.05, 0.08, 0.10, 0.15)

Series = Literal["detr", "raw"]


# ------------------------------------------------------------------------------------------------ data


@dataclass(frozen=True)
class BarArrays:
    o: np.ndarray
    h: np.ndarray
    lo: np.ndarray
    c: np.ndarray
    atr: dict[str, np.ndarray]  # price units, per timeframe


@dataclass(frozen=True)
class ProductData:
    name: str
    spread_bps: float
    ts_close: np.ndarray  # datetime64[ns], ts_bar_close of every bar
    samples: np.ndarray  # bar indices of the samples (C1)
    raw: BarArrays
    detr: BarArrays
    mu_per_min: float

    def arrays(self, series: Series) -> BarArrays:
        return self.detr if series == "detr" else self.raw


def _detrend(bars: pd.DataFrame) -> tuple[pd.DataFrame, float]:
    """Remove the average drift of the loaded window: ``mu = ln(c[-1] / c[0]) / (n - 1)`` per minute (R-c2).

    The same factor multiplies open, high, low and close of a bar, so ``low <= open, close <= high`` still holds and the
    fill kernels work unchanged. Uses the close at the END of the window: information from the future of every inner
    sample, acceptable only because it serves the choice of the label parameters. Private on purpose (C7).
    """
    n = len(bars)
    if n < 2 or not (pd.DatetimeIndex(bars.index).to_series().diff().dropna() == pd.Timedelta(minutes=1)).all():
        raise ValueError("detrend needs a contiguous 1m frame with at least two bars")
    close = bars["close"].to_numpy(dtype=np.float64)
    mu = (math.log(close[-1]) - math.log(close[0])) / (n - 1)
    g = np.exp(-mu * np.arange(n))
    out = bars.copy()
    for col in ("open", "high", "low", "close"):
        out[col] = bars[col].to_numpy(dtype=np.float64) * g
    return out, mu


def _arrays(bars: pd.DataFrame) -> BarArrays:
    ind = compute_indicators(bars)
    return BarArrays(
        o=bars["open"].to_numpy(dtype=np.float64),
        h=bars["high"].to_numpy(dtype=np.float64),
        lo=bars["low"].to_numpy(dtype=np.float64),
        c=bars["close"].to_numpy(dtype=np.float64),
        atr={tf: ind[f"atr_14_{tf}"].to_numpy(dtype=np.float64) for tf in ATR_TFS},
    )


def build_product(
    bars: pd.DataFrame,
    name: str,
    spread_bps: float,
    *,
    cut: pd.Timestamp | None,
    cfg_ref: LabelConfig,
    detrend: bool = True,
    sample_start: pd.Timestamp | None = None,
) -> ProductData:
    """Samples and bar arrays of one product from its whole 1m history (the indicators need the anchor, 2.2.1.b).

    ``cut``: only bars with ``ts_bar_close < cut`` are used and only samples whose label window ends before ``cut`` are
    kept (C1). ``cut=None`` keeps every bar and the samples whose window ends within the data (holdout checks).
    """
    close_ts = pd.DatetimeIndex(bars.index) + pd.Timedelta(minutes=1)
    if cut is not None:
        bars = bars.loc[close_ts < cut]
        close_ts = close_ts[close_ts < cut]
    if len(bars) < 2:
        raise ValueError(f"{name}: not enough bars before the cut")
    raw = _arrays(bars)
    if detrend:
        detr_bars, mu = _detrend(bars)
        detr = _arrays(detr_bars)
    else:
        detr, mu = raw, 0.0
    grid = hourly_grid(pd.DatetimeIndex(bars.index))
    ok = np.ones(len(grid), dtype=bool)
    for arr in (raw, detr):
        for tf in ATR_TFS:
            ok &= ~np.isnan(arr.atr[tf][grid])
    end = label_window_end(close_ts[grid], cfg_ref)
    ok &= (end < cut) if cut is not None else (end <= close_ts[-1])
    if sample_start is not None:
        ok &= close_ts[grid] >= sample_start
    naive = close_ts.tz_convert(None) if close_ts.tz is not None else close_ts  # datetime64[ns] naive UTC
    return ProductData(name, spread_bps, naive.to_numpy(), grid[ok], raw, detr, mu)


def load_product(
    root: Path,
    product: str,
    spread_bps: float,
    *,
    cut: pd.Timestamp | None,
    cfg_ref: LabelConfig,
    detrend: bool = True,
    sample_start: pd.Timestamp | None = None,
) -> ProductData:
    return build_product(
        load_bars(root, product), product, spread_bps, cut=cut, cfg_ref=cfg_ref, detrend=detrend,
        sample_start=sample_start,
    )  # fmt: skip


# ----------------------------------------------------------------------------------------- labeling


@dataclass(frozen=True)
class _Raw:
    """Per-sample arrays of one product, one combination and one series (all samples; none UNRESOLVED)."""

    outcome: np.ndarray
    net_ret: np.ndarray
    p_be: np.ndarray  # NaN where the TP does not cover its own costs
    floor_pass: np.ndarray
    t_idx: np.ndarray
    fill_idx: np.ndarray
    exit_idx: np.ndarray
    ts: np.ndarray  # datetime64[ns] ts_bar_close of the samples
    w: np.ndarray  # uniqueness weight of the filled samples, NaN elsewhere

    @property
    def filled(self) -> np.ndarray:
        return np.asarray(self.outcome != NO_FILL)


def _label_product(
    p: ProductData, cfg: LabelConfig, fees: FeeRates, costs: CostParams, min_net_edge_bps: float, series: Series
) -> _Raw:
    a = p.arrays(series)
    res = triple_barrier(a.o, a.h, a.lo, a.c, a.atr[cfg.atr_tf], p.samples, cfg, fees, costs, p.spread_bps)
    if (res.outcome == UNRESOLVED).any():  # the selection window must make this impossible
        raise AssertionError(f"{p.name}: unresolved samples inside the selection window")
    t = p.samples
    model = CostModel(fees=fees, entry_mode=cfg.entry_mode, spread_bps=p.spread_bps, params=costs)
    atr_bps = a.atr[cfg.atr_tf][t] / a.c[t] * 1e4
    tp_bps, sl_bps = cfg.k_tp * atr_bps, cfg.k_sl * atr_bps
    win = tp_bps - model.cost_tp  # vectorised CostModel.tp_net_bps / sl_loss_bps / p_breakeven
    loss = sl_bps + model.cost_sl
    with np.errstate(divide="ignore", invalid="ignore"):
        p_be = np.where(win > 0, loss / (win + loss), np.nan)
    filled = res.outcome != NO_FILL
    w = np.full(len(t), np.nan)
    if filled.any():
        w[filled] = uniqueness_weights(t[filled], res.exit_idx[filled].astype(np.int64))
    return _Raw(
        res.outcome, res.net_ret, p_be, win >= min_net_edge_bps, t, res.fill_idx, res.exit_idx, p.ts_close[t], w
    )


# ----------------------------------------------------------------------------------------- metrics


@dataclass(frozen=True)
class Metrics:
    n: int
    filled: int
    fill_rate: float
    p_hist: float
    p_breakeven: float
    lift: float
    lift_rw: float
    fee_floor_share: float
    p_vertical: float
    vert_mean_net_bps: float
    n_eff: float
    se_p_hist: float
    indep_per_year: float
    dur_mean_min: float
    dur_median_min: float
    dur_p90_min: float
    dur_p95_min: float
    sigma_r_bps: float
    wl_bps: float


_NAN = float("nan")


def _empty_metrics(n: int, filled: int) -> Metrics:
    return Metrics(n, filled, _NAN, _NAN, _NAN, _NAN, _NAN, _NAN, _NAN, _NAN, 0.0, _NAN, 0.0, *([_NAN] * 6))


def _cat(raws: Sequence[_Raw], name: str) -> np.ndarray:
    return np.concatenate([getattr(r, name) for r in raws])


def _span_years(ts: np.ndarray) -> float:
    if ts.size < 2:
        return _NAN
    return float((ts.max() - ts.min()) / np.timedelta64(1, "D")) / DAYS_PER_YEAR


def _metrics(raws: Sequence[_Raw], cfg: LabelConfig, n_eff: float) -> Metrics:
    outcome, net = _cat(raws, "outcome"), _cat(raws, "net_ret")
    p_be, floor = _cat(raws, "p_be"), _cat(raws, "floor_pass")
    n = int(outcome.size)
    fmask = outcome != NO_FILL
    filled = int(fmask.sum())
    if n == 0 or filled == 0:
        return _empty_metrics(n, filled)
    upper = outcome[fmask] == UPPER
    p_hist = float(upper.mean())
    finite = np.isfinite(p_be[fmask])
    p_be_med = float(np.nanmedian(p_be[fmask])) if finite.any() else _NAN
    vert = outcome == VERTICAL
    dur = np.concatenate([(r.exit_idx - r.fill_idx)[r.filled] for r in raws]).astype(np.float64)
    net_f = net[fmask]
    win_mask = outcome[fmask] == UPPER
    both = win_mask.any() and (~win_mask).any()
    wl = float(1e4 * (net_f[win_mask].mean() - net_f[~win_mask].mean())) if both else _NAN
    years = _span_years(_cat(raws, "ts")[fmask])
    return Metrics(
        n=n,
        filled=filled,
        fill_rate=filled / n,
        p_hist=p_hist,
        p_breakeven=p_be_med,
        lift=p_be_med - p_hist,
        lift_rw=p_be_med - cfg.k_sl / (cfg.k_tp + cfg.k_sl),
        fee_floor_share=float(floor.mean()),
        p_vertical=float(vert.sum() / filled),
        vert_mean_net_bps=float(1e4 * net[vert].mean()) if vert.any() else _NAN,
        n_eff=n_eff,
        se_p_hist=math.sqrt(p_hist * (1 - p_hist) / n_eff) if n_eff > 0 else _NAN,
        indep_per_year=n_eff / years if years and years > 0 else _NAN,
        dur_mean_min=float(dur.mean()),
        dur_median_min=float(np.median(dur)),
        dur_p90_min=float(np.percentile(dur, 90)),
        dur_p95_min=float(np.percentile(dur, 95)),
        sigma_r_bps=float(1e4 * net_f.std(ddof=1)) if filled > 1 else _NAN,
        wl_bps=wl,
    )


def product_n_eff(r: _Raw) -> float:
    """``N_p``: sum of the average uniqueness over the real span ``[t, exit]`` of the filled samples (P1)."""
    return float(np.nansum(r.w))


def pairwise_rho(raws: Mapping[str, _Raw]) -> float:
    """Mean Pearson correlation of ``1[UPPER]`` between products at common timestamps, clipped to [0, 1] (P2).

    Pairs with fewer than ``RHO_MIN_COMMON`` common filled timestamps or without variance are skipped; with no
    valid pair the value is 1.0 (conservative: the products count as one). Alignment is by timestamp, never by index.
    """
    if len(raws) < 2:
        return 0.0
    series = {
        k: pd.Series((r.outcome[r.filled] == UPPER).astype(float), index=pd.DatetimeIndex(r.ts[r.filled]))
        for k, r in raws.items()
    }
    values: list[float] = []
    for a, b in itertools.combinations(sorted(series), 2):
        joined = pd.concat([series[a].rename("a"), series[b].rename("b")], axis=1, join="inner")
        if len(joined) < RHO_MIN_COMMON or joined["a"].std() == 0 or joined["b"].std() == 0:
            continue
        values.append(float(joined["a"].corr(joined["b"])))
    return float(min(max(np.mean(values), 0.0), 1.0)) if values else 1.0


def pooled_n_eff(n_eff_by_product: Sequence[float], rho: float) -> float:
    k = len(n_eff_by_product)
    return float(sum(n_eff_by_product) / (1.0 + (k - 1) * rho))


@dataclass(frozen=True)
class ComboResult:
    cfg: LabelConfig
    detr: Metrics
    raw: Metrics
    per_product: dict[str, tuple[Metrics, Metrics]]  # (detr, raw)
    rho_detr: float
    rho_raw: float


def label_all(
    cfg: LabelConfig, products: Sequence[ProductData], fees: FeeRates, costs: CostParams, min_net_edge_bps: float,
    series: Series,
) -> dict[str, _Raw]:  # fmt: skip
    return {p.name: _label_product(p, cfg, fees, costs, min_net_edge_bps, series) for p in products}


def _series_metrics(cfg: LabelConfig, raws: Mapping[str, _Raw]) -> tuple[Metrics, dict[str, Metrics], float]:
    rho = pairwise_rho(raws)
    n_eff = {k: product_n_eff(r) for k, r in raws.items()}
    per = {k: _metrics([r], cfg, n_eff[k]) for k, r in raws.items()}
    return _metrics(list(raws.values()), cfg, pooled_n_eff(list(n_eff.values()), rho)), per, rho


def evaluate(
    cfg: LabelConfig, products: Sequence[ProductData], fees: FeeRates, costs: CostParams, min_net_edge_bps: float
) -> ComboResult:
    detr_raws = label_all(cfg, products, fees, costs, min_net_edge_bps, "detr")
    raw_raws = label_all(cfg, products, fees, costs, min_net_edge_bps, "raw")
    detr, per_d, rho_d = _series_metrics(cfg, detr_raws)
    raw, per_r, rho_r = _series_metrics(cfg, raw_raws)
    return ComboResult(cfg, detr, raw, {k: (per_d[k], per_r[k]) for k in per_d}, rho_d, rho_r)


# ---------------------------------------------------------------------------------- choice (C5, C7)


def grid() -> list[LabelConfig]:
    return [
        LabelConfig(atr_tf=tf, k_tp=ktp, k_sl=ksl, entry_mode=mode, horizon_min=days * 1440)  # type: ignore[arg-type]
        for tf, ktp, ksl, mode, days in itertools.product(ATR_TFS, K_TP, K_SL, ENTRY_MODES, HORIZON_DAYS)
    ]


def ineligible_reason(r: ComboResult) -> str:
    """Empty string when eligible, else the first rule that excludes the combination."""
    d = r.detr
    if r.cfg.horizon_min > H_MAX_DAYS * 1440:
        return "h_gt_hmax"
    if not math.isfinite(d.lift) or d.fee_floor_share < FEE_FLOOR_SHARE_MIN:
        return "fee_floor"
    if r.cfg.entry_mode == "maker" and d.fill_rate < MAKER_FILL_MIN:
        return "fill"
    if d.p_vertical > P_VERTICAL_MAX:
        return "p_vertical"
    return ""


def family(cfg: LabelConfig) -> tuple[str, float, float, str]:
    return (cfg.atr_tf, cfg.k_tp, cfg.k_sl, cfg.entry_mode)


def choose(results: Sequence[ComboResult]) -> tuple[ComboResult | None, list[ComboResult]]:
    """``(best, tie)``: per family the smallest eligible H; among the families within ``LIFT_TIE`` of the minimum
    ``lift_0`` the largest ``N_eff`` (then the smallest H, then a fixed order, so the result is deterministic)."""
    reps: dict[tuple[str, float, float, str], ComboResult] = {}
    for r in results:
        if ineligible_reason(r):
            continue
        key = family(r.cfg)
        if key not in reps or r.cfg.horizon_min < reps[key].cfg.horizon_min:
            reps[key] = r
    if not reps:
        return None, []
    lift_min = min(r.detr.lift for r in reps.values())
    tie = [r for r in reps.values() if r.detr.lift <= lift_min + LIFT_TIE]
    best = min(
        tie, key=lambda r: (-r.detr.n_eff, r.cfg.horizon_min, r.cfg.atr_tf, r.cfg.k_tp, r.cfg.k_sl, r.cfg.entry_mode)
    )
    return best, tie


def verdict(
    best: ComboResult | None, jack_max: float, per_product_detr: Mapping[str, Metrics]
) -> tuple[bool, list[str]]:
    """GO iff ``lift_0``, the semester jackknife and every product (with a 2 SE margin) are within the limit."""
    if best is None:
        return False, ["no_eligible"]
    reasons: list[str] = []
    if not best.detr.lift <= LIFT_MAX:
        reasons.append("lift0")
    if not jack_max <= LIFT_MAX:
        reasons.append("jackknife")
    for name, m in sorted(per_product_detr.items()):
        if not m.lift <= LIFT_MAX + PRODUCT_SE_MULT * m.se_p_hist:
            reasons.append(f"product_lift:{name}")
    return not reasons, reasons


# ------------------------------------------------------------------------------ diagnostics (C6)


def semester_of(ts: pd.DatetimeIndex | np.ndarray) -> np.ndarray:
    idx = pd.DatetimeIndex(ts)
    return np.array([f"{y}H{1 if m <= 6 else 2}" for y, m in zip(idx.year, idx.month, strict=True)])


def _fills(raws: Mapping[str, _Raw]) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Pooled arrays over the filled samples: ``(ts, upper, p_be, w)``."""
    rs = list(raws.values())
    ts = np.concatenate([r.ts[r.filled] for r in rs])
    up = np.concatenate([(r.outcome[r.filled] == UPPER) for r in rs]).astype(np.float64)
    p_be = np.concatenate([r.p_be[r.filled] for r in rs])
    w = np.concatenate([r.w[r.filled] for r in rs])
    return ts, up, p_be, w


def jackknife_lift0(raws_detr: Mapping[str, _Raw]) -> dict[str, float]:
    """``lift_0(-s)`` for each semester ``s`` among the fills: the pool without that semester (mu unchanged)."""
    ts, up, p_be, _ = _fills(raws_detr)
    sem = semester_of(pd.DatetimeIndex(ts))
    out: dict[str, float] = {}
    for s in sorted(set(sem.tolist())):
        keep = sem != s
        if keep.any() and np.isfinite(p_be[keep]).any():
            out[s] = float(np.nanmedian(p_be[keep]) - up[keep].mean())
    return out


@dataclass(frozen=True)
class PeriodRow:
    period: str
    n_eff: float
    p_hist: float
    p_breakeven: float
    lift: float
    se: float


def period_table(raws: Mapping[str, _Raw], rho: float, by: Literal["semester", "year"]) -> list[PeriodRow]:
    d = 1.0 / (1.0 + (len(raws) - 1) * rho)
    ts, up, p_be, w = _fills(raws)
    idx = pd.DatetimeIndex(ts)
    label = semester_of(idx) if by == "semester" else idx.year.astype(str).to_numpy()
    rows = []
    for s in sorted(set(label.tolist())):
        m = label == s
        n_eff = float(w[m].sum() * d)
        p = float(up[m].mean())
        be = float(np.nanmedian(p_be[m])) if np.isfinite(p_be[m]).any() else _NAN
        rows.append(PeriodRow(s, n_eff, p, be, be - p, math.sqrt(p * (1 - p) / n_eff) if n_eff > 0 else _NAN))
    return rows


def homogeneity_chi2(o: np.ndarray, n: np.ndarray) -> tuple[float, int, float, list[int]]:
    """Chi-square homogeneity of the UPPER rate across periods. ``o``: effective successes, ``n``: effective sizes.

    Returns ``(chi2, df, p_value, excluded)``; periods with ``n * min(p_bar, 1 - p_bar) < 5`` are excluded; with
    fewer than two periods left the test is not defined (NaN).
    """
    o, n = np.asarray(o, dtype=np.float64), np.asarray(n, dtype=np.float64)
    p_bar = float(o.sum() / n.sum()) if n.sum() > 0 else _NAN
    if not math.isfinite(p_bar) or p_bar in (0.0, 1.0):
        return _NAN, 0, _NAN, list(range(len(o)))
    keep = n * min(p_bar, 1.0 - p_bar) >= CHI2_MIN_EXPECTED
    excluded = [int(i) for i in np.flatnonzero(~keep)]
    if keep.sum() < 2:
        return _NAN, 0, _NAN, excluded
    chi2 = float((((o[keep] - n[keep] * p_bar) ** 2) / (n[keep] * p_bar * (1 - p_bar))).sum())
    df = int(keep.sum() - 1)
    return chi2, df, float(stats.chi2.sf(chi2, df)), excluded


def chi2_for(raws: Mapping[str, _Raw], rho: float) -> tuple[float, int, float, list[int]]:
    d = 1.0 / (1.0 + (len(raws) - 1) * rho)
    ts, up, _, w = _fills(raws)
    sem = semester_of(pd.DatetimeIndex(ts))
    periods = sorted(set(sem.tolist()))
    o = np.array([d * float((w * up)[sem == s].sum()) for s in periods])
    n = np.array([d * float(w[sem == s].sum()) for s in periods])
    return homogeneity_chi2(o, n)


def saturation(results: Sequence[ComboResult], tie: Sequence[ComboResult]) -> dict[str, tuple[float, float, bool]]:
    """Raw lift at H = 15 and H = 20 for each family of the tie; ``drift_dominated`` if it falls by more than 0.02."""
    by_key = {(*family(r.cfg), r.cfg.horizon_min): r for r in results}
    out: dict[str, tuple[float, float, bool]] = {}
    for r in tie:
        f = family(r.cfg)
        a, b = by_key.get((*f, 15 * 1440)), by_key.get((*f, 20 * 1440))
        if a is None or b is None:
            continue
        out["/".join(str(x) for x in f)] = (a.raw.lift, b.raw.lift, b.raw.lift < a.raw.lift - SATURATION_TOL)
    return out


def n_min(sigma_r_bps: float, wl_bps: float, delta: float) -> float:
    """Independent trades needed to detect an edge ``delta`` in the hit rate (z = 2.0, power 0.8; informational)."""
    return ((2.0 + 0.84) * sigma_r_bps / (delta * wl_bps)) ** 2 if wl_bps and math.isfinite(wl_bps) else _NAN


# --------------------------------------------------------------------------------------------- output


def fees_from_probe(path: Path) -> tuple[FeeRates, dict[str, float]]:
    probe = json.loads(path.read_text(encoding="utf-8"))
    fees = probe["fees"]
    rates = FeeRates(maker_bps=float(fees["maker_bps"]), taker_bps=float(fees["taker_bps"]), assumed=False)
    spreads = {p: float(v["meta"]["spread_bps"]) for p, v in probe["products"].items()}
    return rates, spreads


def label_spreads(
    products: Sequence[str], probe: Mapping[str, float], floor: float, override: float | None
) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    """Spread of the label per product: ``max(probe snapshot, floor)`` (B7), or the override for every product."""
    spreads: dict[str, float] = {}
    source: dict[str, dict[str, float]] = {}
    for name in products:
        if override is not None:
            spreads[name], source[name] = override, {"override": override}
            continue
        if name not in probe:
            raise SystemExit(f"no spread for {name}: pass --fees-from-probe or --spread-bps")
        spreads[name] = max(probe[name], floor)
        source[name] = {"probe": probe[name], "floor": floor}
    return spreads, source


def _f(x: float, nd: int = 3) -> str:
    return "nan" if not math.isfinite(x) else f"{x:.{nd}f}"


def _row(r: ComboResult) -> str:
    d, w, c = r.detr, r.raw, r.cfg
    reason = ineligible_reason(r)
    return (
        f"| {c.atr_tf} | {c.k_tp:g}/{c.k_sl:g} | {c.entry_mode} | {c.horizon_min // 1440} | {_f(d.p_breakeven)} | "
        f"{_f(d.p_hist)} | {_f(d.lift)} | {_f(w.lift)} | {_f(d.lift_rw)} | {_f(d.fee_floor_share, 2)} | "
        f"{_f(d.fill_rate, 2)} | {_f(d.p_vertical, 2)} | {_f(w.vert_mean_net_bps, 0)} | "
        f"{_f(w.dur_median_min / 60, 1)} | {_f(w.dur_p95_min / 60, 1)} | {_f(d.n_eff, 0)} | "
        f"{_f(d.indep_per_year, 1)} | {'yes' if not reason else 'no (' + reason + ')'} |"
    )


@dataclass
class Diagnostics:
    jack: dict[str, float] = field(default_factory=dict)
    jack_max: float = _NAN
    sat: dict[str, tuple[float, float, bool]] = field(default_factory=dict)
    semesters: dict[str, list[PeriodRow]] = field(default_factory=dict)
    years: dict[str, list[PeriodRow]] = field(default_factory=dict)
    chi2: dict[str, tuple[float, int, float, list[int]]] = field(default_factory=dict)
    atr6h_median_bps: dict[str, float] = field(default_factory=dict)


def diagnostics(
    best: ComboResult, tie: Sequence[ComboResult], results: Sequence[ComboResult], products: Sequence[ProductData],
    fees: FeeRates, costs: CostParams, min_net_edge_bps: float,
) -> Diagnostics:  # fmt: skip
    detr = label_all(best.cfg, products, fees, costs, min_net_edge_bps, "detr")
    raw = label_all(best.cfg, products, fees, costs, min_net_edge_bps, "raw")
    jack = jackknife_lift0(detr)
    dg = Diagnostics(jack=jack, jack_max=max(jack.values()) if jack else _NAN, sat=saturation(results, tie))
    for name, raws, rho in (("detr", detr, best.rho_detr), ("raw", raw, best.rho_raw)):
        dg.semesters[name] = period_table(raws, rho, "semester")
        dg.years[name] = period_table(raws, rho, "year")
        dg.chi2[name] = chi2_for(raws, rho)
    for p in products:
        a = p.raw
        dg.atr6h_median_bps[p.name] = float(np.median(a.atr["6h"][p.samples] / a.c[p.samples] * 1e4))
    return dg


def _period_lines(title: str, rows: dict[str, list[PeriodRow]]) -> list[str]:
    out = [
        f"### {title}",
        "",
        "| series | period | N_eff | p_hist | p_be | lift | SE |",
        "|---|---|---|---|---|---|---|",
    ]
    for series, table in rows.items():
        for r in table:
            out.append(
                f"| {series} | {r.period} | {_f(r.n_eff, 1)} | {_f(r.p_hist)} | {_f(r.p_breakeven)} | "
                f"{_f(r.lift)} | {_f(r.se)} |"
            )
    return [*out, ""]


def render_report(
    results: Sequence[ComboResult], best: ComboResult | None, tie: Sequence[ComboResult], ok: bool,
    reasons: Sequence[str], dg: Diagnostics | None, fees: FeeRates, costs: CostParams, products: Sequence[ProductData],
    source: str, selection: Mapping[str, Any],
) -> str:  # fmt: skip
    out = ["# Feasibility study (spec sez. 3.1.3, v2.7)", ""]
    out.append(
        f"Generated {datetime.now(UTC).isoformat(timespec='seconds')}. Fees: maker {fees.maker_bps:g} bps, taker "
        f"{fees.taker_bps:g} bps ({source}). slip {costs.slippage_buffer_bps:g} bps, stop extra "
        f"{costs.stop_extra_slip_bps:g} bps."
    )
    out.append("Products: " + ", ".join(
        f"{p.name} (spread {p.spread_bps:g} bps, {len(p.samples)} hourly samples, "
        f"mu {p.mu_per_min * 1440 * DAYS_PER_YEAR:+.2f}/yr)" for p in products))  # fmt: skip
    out.append(f"Selection window: {json.dumps(dict(selection), default=str)}")
    out += ["", f"## Verdict: {'GO' if ok else 'NO-GO'}" + ("" if ok else f" ({', '.join(reasons)})")]
    if best is not None:
        c, d, w = best.cfg, best.detr, best.raw
        jack_max = dg.jack_max if dg else _NAN
        out.append(
            f"Best: atr_tf={c.atr_tf}, k_tp/k_sl={c.k_tp:g}/{c.k_sl:g}, {c.entry_mode}, H={c.horizon_min // 1440} d. "
            f"lift_0 {d.lift:+.3f} (limit {LIFT_MAX:.2f}), raw lift {w.lift:+.3f}, N_eff {d.n_eff:.0f}, "
            f"rho {best.rho_detr:.2f}, jackknife max {jack_max:+.3f}."
        )
        out.append("Tie within 0.01 of the minimum lift_0: " + ", ".join(
            f"{t.cfg.atr_tf} {t.cfg.k_tp:g}/{t.cfg.k_sl:g} {t.cfg.entry_mode} H{t.cfg.horizon_min // 1440} "
            f"(lift_0 {t.detr.lift:+.3f}, N_eff {t.detr.n_eff:.0f})" for t in tie))  # fmt: skip
    out += [
        "",
        "`lift_0 = median(p_breakeven) - p_hist` on the detrended series; `lift_raw` on the real bars; `lift_rw` "
        "against a driftless random walk. `p_vertical`, `vert_net` are diagnostics. `dur` = real duration of the "
        "trades (hours). `N_eff` pools the products with the correlation `rho`.",
        "",
    ]
    hdr = ("| atr | k_tp/k_sl | entry | H d | p_be | p_hist_0 | lift_0 | lift_raw | lift_rw | floor | fill | p_vert | "
           "vert_net | dur_med_h | dur_p95_h | N_eff | indep/yr | eligible |\n"
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")  # fmt: skip
    ranked = sorted(results, key=lambda r: (not math.isfinite(r.detr.lift), r.detr.lift))
    out += ["## All combinations (sorted by lift_0)", "", hdr, *[_row(r) for r in ranked], ""]
    if best is not None:
        out += [
            "## Chosen combination, per product",
            "",
            "| product | series | N_p | SE_p | p_hist | p_be | lift | guard (limit) |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for name, (d, w) in best.per_product.items():
            lim = LIFT_MAX + PRODUCT_SE_MULT * d.se_p_hist
            verdict_txt = "ok" if d.lift <= lim else "FAIL"
            out.append(
                f"| {name} | detr | {_f(d.n_eff, 0)} | {_f(d.se_p_hist)} | {_f(d.p_hist)} | {_f(d.p_breakeven)} | "
                f"{_f(d.lift)} | {verdict_txt} ({_f(lim)}) |"
            )
            out.append(
                f"| {name} | raw | {_f(w.n_eff, 0)} | {_f(w.se_p_hist)} | {_f(w.p_hist)} | {_f(w.p_breakeven)} | "
                f"{_f(w.lift)} | |"
            )
        out.append("")
    if dg is not None and best is not None:
        out += ["## Jackknife by semester (lift_0 without that semester)", "",
                *[f"- {s}: {v:+.3f}" for s, v in dg.jack.items()], f"- max: {dg.jack_max:+.3f}", ""]  # fmt: skip
        out += _period_lines("By semester", dg.semesters) + _period_lines("By year", dg.years)
        out += ["### Homogeneity of p_hist across semesters (chi-square, diagnostic)", ""]
        for series, (chi2, df, pv, excl) in dg.chi2.items():
            out.append(f"- {series}: chi2 {_f(chi2, 2)}, df {df}, p {_f(pv, 4)}, excluded periods {excl}")
        out += ["", "### Saturation in H (raw lift at H=15 vs H=20)", ""]
        out += [f"- {k}: {_f(a)} -> {_f(b)} ({'drift dominated' if dom else 'saturated'})"
                for k, (a, b, dom) in dg.sat.items()]  # fmt: skip
        w = best.raw
        out += ["", "### Trades needed to detect an edge (K5, informational)", "",
                f"sigma_r {_f(w.sigma_r_bps, 0)} bps, W+L {_f(w.wl_bps, 0)} bps, duration mean "
                f"{_f(w.dur_mean_min / 60, 1)} h, median {_f(w.dur_median_min / 60, 1)} h, p90 "
                f"{_f(w.dur_p90_min / 60, 1)} h, p95 {_f(w.dur_p95_min / 60, 1)} h",
                *[f"- delta {x:.2f}: {_f(n_min(w.sigma_r_bps, w.wl_bps, x), 0)} trades" for x in NMIN_DELTAS],
                "", "### Median ATR 6h (K6)", "",
                *[f"- {k}: {_f(v, 0)} bps" for k, v in dg.atr6h_median_bps.items()], ""]  # fmt: skip
    return "\n".join(out) + "\n"


def label_config_json(
    best: ComboResult, dg: Diagnostics, reasons: Sequence[str], fees: FeeRates, costs: CostParams, source: str,
    products: Sequence[ProductData], spreads: Mapping[str, float], spread_source: Mapping[str, Any],
    selection: Mapping[str, Any],
) -> dict[str, Any]:  # fmt: skip
    c = best.cfg
    mu_year = {p.name: p.mu_per_min * 1440 * DAYS_PER_YEAR for p in products}
    return {
        "k_tp": c.k_tp,
        "k_sl": c.k_sl,
        "horizon_min": c.horizon_min,
        "atr": f"ATR_14_{c.atr_tf}",
        "atr_tf": c.atr_tf,
        "entry_mode": c.entry_mode,
        "entry_timeout_min": c.entry_timeout_min,
        "entry_first_bar": c.entry_first_bar,
        "cost_model": "COST_V1",
        "fees_bps": {"maker": fees.maker_bps, "taker": fees.taker_bps, "source": source},
        "slip_bps": costs.slippage_buffer_bps,
        "stop_extra_slip_bps": costs.stop_extra_slip_bps,
        "products": [p.name for p in products],
        "spread_bps": dict(spreads),
        "spread_source": dict(spread_source),
        "selection": dict(selection),
        "feasibility": {
            "detr": best.detr.__dict__,
            "raw": best.raw.__dict__,
            "rho_detr": best.rho_detr,
            "rho_raw": best.rho_raw,
            "n_eff": best.detr.n_eff,
            "indep_per_year": best.detr.indep_per_year,
            "jack_max": dg.jack_max,
            "chi2_p_detr": dg.chi2["detr"][2],
            "chi2_p_raw": dg.chi2["raw"][2],
            "mu_year": mu_year,
            "dur_median_min": best.raw.dur_median_min,
            "dur_p95_min": best.raw.dur_p95_min,
            "sigma_r_bps": best.raw.sigma_r_bps,
            "wl_bps": best.raw.wl_bps,
            "atr6h_median_bps": dg.atr6h_median_bps,
            "verdict_reasons": list(reasons),
        },
        "sha256": c.sha256(fees, costs, spreads),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


# ------------------------------------------------------------------------------------------------ cli


def main(argv: Sequence[str] | None = None, cfg: AppConfig | None = None) -> int:
    ap = argparse.ArgumentParser(description="Chronos feasibility study (spec sez. 3.1.3)")
    ap.add_argument("--products", nargs="+", required=True)
    ap.add_argument("--fees-from-probe", type=Path, default=None)
    ap.add_argument("--out-dir", type=Path, default=Path("data/reports"))
    ap.add_argument("--label-config", type=Path, default=Path("data/datasets/label_config.json"))
    ap.add_argument("--config", type=Path, default=None, help="YAML config (default: built-in defaults)")
    ap.add_argument("--spread-bps", type=float, default=None, help="override the spread of every product")
    ap.add_argument("--spread-floor-bps", type=float, default=LABEL_SPREAD_FLOOR_BPS)
    args = ap.parse_args(argv)

    app = cfg or (load_config_or_exit(args.config) if args.config else AppConfig())
    costs = CostParams.from_config(app)
    probe_spreads: dict[str, float] = {}
    if args.fees_from_probe is not None:
        fees, probe_spreads = fees_from_probe(args.fees_from_probe)
        source = "probe"
    else:
        fb = app.dry_run.fill_model.fallback_fees
        fees = FeeRates(maker_bps=fb.maker_bps, taker_bps=fb.taker_bps, assumed=True)
        source = "assumed"
    spreads, spread_source = label_spreads(args.products, probe_spreads, args.spread_floor_bps, args.spread_bps)

    root = bars_root(app)
    frames = {name: load_bars(root, name) for name in args.products}
    last_close = max(pd.DatetimeIndex(f.index)[-1] + pd.Timedelta(minutes=1) for f in frames.values())
    holdout_start = holdout_start_from(last_close, HOLDOUT_DAYS)
    cut = holdout_start - EMBARGO
    cfg_ref = LabelConfig(horizon_min=max(HORIZON_DAYS) * 1440)
    products = [build_product(frames[name], name, spreads[name], cut=cut, cfg_ref=cfg_ref) for name in args.products]
    for p in products:
        mu = p.mu_per_min * 1440 * DAYS_PER_YEAR
        print(f"{p.name}: {len(p.samples)} samples before {cut:%Y-%m-%d}, mu {mu:+.2f}/yr", flush=True)
    sample_ts = np.concatenate([p.ts_close[p.samples] for p in products])
    selection = {
        "holdout_start": holdout_start.isoformat(),
        "cut": cut.isoformat(),
        "sample_start": str(pd.Timestamp(sample_ts.min(), tz="UTC")),
        "sample_end": str(pd.Timestamp(sample_ts.max(), tz="UTC")),
        "holdout_days": HOLDOUT_DAYS,
        "embargo_days": EMBARGO.days,
        "h_ref_days": max(HORIZON_DAYS),
    }

    results = [evaluate(c, products, fees, costs, app.risk.min_net_edge_bps) for c in grid()]
    best, tie = choose(results)
    dg = diagnostics(best, tie, results, products, fees, costs, app.risk.min_net_edge_bps) if best else None
    per_product = {k: v[0] for k, v in best.per_product.items()} if best else {}
    ok, reasons = verdict(best, dg.jack_max if dg else _NAN, per_product)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    report = render_report(results, best, tie, ok, reasons, dg, fees, costs, products, source, selection)
    (args.out_dir / "feasibility.md").write_text(report, encoding="utf-8", newline="\n")
    if ok:
        assert best is not None and dg is not None
        args.label_config.parent.mkdir(parents=True, exist_ok=True)
        payload = label_config_json(best, dg, reasons, fees, costs, source, products, spreads, spread_source, selection)
        args.label_config.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=float), encoding="utf-8", newline="\n"
        )
    print(report.split("## All combinations")[0])
    return 0 if ok else EXIT_NO_GO


if __name__ == "__main__":
    sys.exit(main())
