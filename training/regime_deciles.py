"""Diagnostic D1.4: the edge lift by decile of ``atr_1d_pctile_365d`` (read-only).

Verifies the threshold of the regime rule, it does not choose it. Rows: the FLAT rows of ``samples.parquet`` with a
known ``edge_target`` (the entry filled) and ``t < holdout_start - embargo`` (the pre-holdout window). ``holdout_start``
comes from the ``label_config.json`` of the dataset run; ``feasibility.py`` is NOT re-run (it would recompute the
holdout) and no ``label_config.json`` is ever written. The only output is ``<data_root>/reports/regime_deciles.md``.

Per decile of the state column ``atr_1d_pctile_365d`` (an integer 0..100: the deciles are the fixed bands
``[0,10) ... [90,100]``) plus the row ``all``:

- ``p_hist`` = ``sum(w_edge * 1[UPPER]) / sum(w_edge)``; ``p_be`` = median of the ``p_breakeven`` state token;
- ``lift`` = ``p_hist - p_be`` (positive = the barrier hit rate beats the break-even); ``lift_0`` = ``p_be - p_hist``
  is the same number with the sign of ``feasibility.py`` (``lift_0 <= 0.10`` is its GO limit);
- ``N_eff = sum(w_edge) / (1 + (K - 1) rho_raw)`` (``feasibility.pooled_n_eff``);
  ``SE = sqrt(p_hist (1 - p_hist) / N_eff)``;
- ``net_ret_bps`` = plain mean of the net return of the fills.
"""

from __future__ import annotations

import argparse
import math
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Final

import numpy as np
import pandas as pd

from modules.config import EXIT_CONFIG_ERROR
from modules.risk.label_config import LoadedLabelConfig, load_label_config
from training.baseline_lgbm import TrainingError, latest_run
from training.config import load_train_config_or_exit
from training.feasibility import pooled_n_eff

__all__ = ["REPORT_NAME", "Row", "deciles_table", "pre_holdout_fills", "render_report"]

REPORT_NAME: Final = "regime_deciles.md"
PCTILE_COL: Final = "atr_1d_pctile_365d"
ALL: Final = "all"
MISSING: Final = "n/d"
N_DECILES: Final = 10
_NAN: Final = float("nan")


@dataclass(frozen=True)
class Row:
    name: str
    n: int
    p_hist: float
    p_breakeven: float
    lift: float  # p_hist - p_breakeven
    n_eff: float
    se: float
    mean_net_ret_bps: float


def pre_holdout_fills(samples: pd.DataFrame, holdout_start: pd.Timestamp, embargo: timedelta) -> pd.DataFrame:
    """FLAT rows with a known ``edge_target`` and ``t < holdout_start - embargo`` (``t``: naive UTC)."""
    cut = (holdout_start - embargo).tz_convert("UTC").tz_localize(None)
    keep = (samples["position_side"] == "FLAT") & samples["edge_target"].notna() & (samples["t"] < cut)
    return samples.loc[keep].reset_index(drop=True)


def _row(name: str, g: pd.DataFrame, products: Sequence[str], rho: float) -> Row:
    n = len(g)
    if n == 0:
        return Row(name, 0, _NAN, _NAN, _NAN, 0.0, _NAN, _NAN)
    w = g["w_edge"].to_numpy(dtype=np.float64)
    y = g["edge_target"].to_numpy(dtype=np.float64)
    p_hist = float((w * y).sum() / w.sum()) if w.sum() > 0 else _NAN
    p_be_all = g["p_breakeven"].to_numpy(dtype=np.float64)
    p_be = float(np.nanmedian(p_be_all)) if np.isfinite(p_be_all).any() else _NAN
    by_product = g.groupby("product")["w_edge"].sum()
    n_eff = pooled_n_eff([float(by_product.get(p, 0.0)) for p in products], rho)
    se = math.sqrt(p_hist * (1.0 - p_hist) / n_eff) if n_eff > 0 and math.isfinite(p_hist) else _NAN
    return Row(name, n, p_hist, p_be, p_hist - p_be, n_eff, se, float(g["net_ret_bps"].mean()))


def deciles_table(fills: pd.DataFrame, rho: float) -> list[Row]:
    """Ten decile rows (fixed bands of the percentile), a ``n/d`` row if some percentiles are missing, then ``all``."""
    products = sorted(fills["product"].unique())
    pct = fills[PCTILE_COL].to_numpy(dtype=np.float64)
    band = np.where(np.isfinite(pct), np.minimum(np.floor(np.nan_to_num(pct) / 10.0), N_DECILES - 1), -1).astype(int)
    rows = []
    for d in range(N_DECILES):
        hi = ")" if d < N_DECILES - 1 else "]"
        rows.append(_row(f"D{d + 1} [{10 * d},{10 * (d + 1)}{hi}", fills[band == d], products, rho))
    if (band < 0).any():
        rows.append(_row(MISSING, fills[band < 0], products, rho))
    rows.append(_row(ALL, fills, products, rho))
    return rows


def _f(x: float, nd: int = 3) -> str:
    return "n/a" if not math.isfinite(x) else f"{x:.{nd}f}"


def render_report(
    rows: Sequence[Row], run_dir: Path, holdout_start: pd.Timestamp, embargo: timedelta,
    rho: float, k: int, n_leaky: int,
) -> str:  # fmt: skip
    hdr = "| decile | n | p_hist | p_be | lift | lift_0 | N_eff | SE | mean net_ret_bps |"
    lines = [
        "# Regime deciles (D1.4)",
        "",
        f"- dataset: `{run_dir.name}`; holdout_start {holdout_start.isoformat()}, embargo {embargo.days} d; "
        f"window: FLAT fills with `t < holdout_start - embargo`.",
        f"- K = {k} products, rho_raw = {rho:.4f}, `N_eff = sum(w_edge) / (1 + (K-1) rho_raw)`.",
        f"- rows of the window whose `label_window_end` reaches `holdout_start - embargo`: {n_leaky} "
        "(kept: spec 1.7 filters on `t` only).",
        "",
        hdr,
        "|" + "---|" * 9,
    ]
    for r in rows:
        lines.append(
            f"| {r.name} | {r.n} | {_f(r.p_hist)} | {_f(r.p_breakeven)} | {_f(r.lift)} | {_f(-r.lift)} | "
            f"{_f(r.n_eff, 1)} | {_f(r.se)} | {_f(r.mean_net_ret_bps, 1)} |"
        )
    lines += [
        "",
        "Notes",
        "",
        "- `lift = p_hist - p_be` (positive = hit rate above the break-even). `lift_0 = p_be - p_hist` is the sign "
        "used by `feasibility.py` (GO limit 0.10).",
        "- Differences from the feasibility `lift_0`: the samples here are the hourly grid plus CUSUM events on the "
        "real (not detrended) bars, `p_hist` is weighted with `w_edge`, `p_be` is the median of the `p_breakeven` "
        "state token.",
        "- Deciles are fixed bands of the integer state column `atr_1d_pctile_365d` (not empirical quantiles, which "
        "would collapse on ties); `n/d` collects rows with a missing percentile.",
        "- `N_eff` of a decile uses the same `rho_raw` as the whole window (an assumption, not re-estimated per "
        "decile).",
        "- The `all` row `mean net_ret_bps` is the plain mean over every fill of the window (datum [?] of the quant "
        "for 3/1.5).",
        "",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Chronos D1.4: edge lift by decile of atr_1d_pctile_365d (read-only)")
    ap.add_argument("--config", default="config/train.yml")
    ap.add_argument("--dataset", default=None, help="dataset run directory (default: the latest)")
    args = ap.parse_args(argv)
    cfg = load_train_config_or_exit(args.config)
    try:
        run_dir = Path(args.dataset) if args.dataset else latest_run(Path(cfg.datasets_dir))
    except (TrainingError, OSError) as e:
        print(f"dataset error: {e}", file=sys.stderr)
        return 1
    # The dataset carries a byte copy of the label_config it was built with; fall back to the configured one.
    label_path = run_dir / "label_config.json"
    try:
        label: LoadedLabelConfig = load_label_config(label_path if label_path.is_file() else Path(cfg.label_config))
    except (ValueError, OSError) as e:
        print(f"label_config invalid: {e}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    samples = pd.read_parquet(run_dir / "samples.parquet")
    embargo = timedelta(days=cfg.embargo_days)
    fills = pre_holdout_fills(samples, label.holdout_start, embargo)
    if fills.empty:
        print("no pre-holdout fills in the dataset", file=sys.stderr)
        return 1
    rho = float(label.raw.get("feasibility", {}).get("rho_raw", 1.0))
    cut = (label.holdout_start - embargo).tz_convert("UTC").tz_localize(None)
    n_leaky = int((fills["label_window_end"] >= cut).sum())
    rows = deciles_table(fills, rho)
    text = render_report(rows, run_dir, label.holdout_start, embargo, rho, fills["product"].nunique(), n_leaky)
    out = Path(cfg.data_root) / "reports" / REPORT_NAME
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8", newline="\n")
    print(text)
    print(f"report written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
