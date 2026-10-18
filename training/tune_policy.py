"""Policy thresholds by grid search on the validation backtest (spec sez. 3.1.6, 3.5 punto 10; decision O3.5).

::

    python -m training.tune_policy --config config/train.yml --model models/baseline/<version> [--dataset <run>]

Every combination of thresholds is evaluated by ``training.backtest.run_backtest``: the SAME ``DecisionPolicy``,
the SAME ``RiskManager`` (fee floor and EV gate active), the same guard and venue. A simplified simulation that
skips the RiskManager is forbidden. The model answers are computed once per (model, span) and reused by all
combinations (only the thresholds change). The objective is the net PnL in EUR of the validation backtest, with at
least ``MIN_TRADES``
trades; a fold where no combination reaches them is ``untunable`` and does not trade (the conservative choice).

Thresholds are tuned PER FOLD on that fold's own validation set: one policy tuned on the latest validation would have
seen the test periods of the older folds. The production ``final/policy.json`` is tuned on the final validation set.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from modules.config import EXIT_CONFIG_ERROR, AppConfig
from modules.decision.baseline import BaselineDecider
from modules.decision.policy import PolicyConfig
from modules.decision.questions import build_questions
from modules.dryrun.fill_model import FillModelParams
from modules.execution.spot import SpotRegistry
from modules.risk.label_config import LoadedLabelConfig, load_label_config
from training.backtest import (
    TUNE_DECISION_EVERY_MIN,
    BacktestConfig,
    FlatProbCache,
    LongProbCache,
    ProductMarket,
    Span,
    build_market,
    decision_rows,
    model_key,
    run_backtest,
)
from training.baseline_lgbm import FINAL, latest_run
from training.config import TrainConfig, load_train_config_or_exit
from training.fold_spans import SpanSet, compute_spans

MIN_TRADES = 30
BUY_QUANTILES = (0.80, 0.90, 0.95, 0.98, 0.99)
EDGE_QUANTILES = (0.50, 0.70, 0.85, 0.95)
C_MIN_GRID = (0.0, 0.10)
TAU_SELL_GRID = (0.50, 0.70)


@dataclass(frozen=True)
class TuneResult:
    policy: PolicyConfig | None
    table: pd.DataFrame  # one row per combination: thresholds, n_trades, pnl_eur
    reason: str  # "ok" | "untunable"


def probe_registry(probe_path: Path, products: Sequence[str]) -> SpotRegistry:
    """Spot registry from the product facts stored by the read-only probe (never typed by hand)."""
    probe = json.loads(probe_path.read_text(encoding="utf-8"))
    raw = {p: {"product_id": p, **probe["products"][p]["meta"]} for p in products}
    return SpotRegistry.from_raw(raw)


def grid(
    decider: BaselineDecider, markets: Mapping[str, ProductMarket], label: LoadedLabelConfig, span: Span,
    cache: FlatProbCache, every_min: int, p_edge_full: float,
) -> list[dict[str, Any]]:  # fmt: skip
    """Threshold combinations anchored on the quantiles of the validation answers (so every combination can trade)."""
    key = f"{model_key(decider, label)}-{span.tag()}-D{every_min}"
    masked, edge = [], []
    for pm in markets.values():
        rows = decision_rows(pm, label, span, every_min)
        a = cache.get(key, decider, pm, label, rows)
        masked.append(a.p_action[:, 0] / np.maximum(a.p_action[:, 0] + a.p_action[:, 2], 1e-12))
        edge.append(a.p_edge)
    if not masked or sum(len(m) for m in masked) == 0:
        return []
    m, e = np.concatenate(masked), np.concatenate(edge)
    taus = sorted({round(float(np.quantile(m, q)), 4) for q in BUY_QUANTILES})
    # sizing needs p_edge_min < p_edge_full (risk.p_edge_full): thresholds above that cannot size a trade
    ceiling = p_edge_full - 0.01
    edges = sorted({min(round(float(np.quantile(e, q)), 4), ceiling) for q in EDGE_QUANTILES})
    regimes = (2,) if decider.degenerate_regime else (1, 2, 3)
    return [
        {"tau_buy": tb, "p_edge_min": pe, "c_min": c, "tau_sell": ts, "allowed_regimes_for_entry": regimes}
        for tb, pe, c, ts in itertools.product(taus, edges, C_MIN_GRID, TAU_SELL_GRID)
    ]


async def tune(
    decider: BaselineDecider, markets: Mapping[str, ProductMarket], label: LoadedLabelConfig, span: Span,
    bt: BacktestConfig, registry: SpotRegistry, flat_cache: FlatProbCache, long_cache: LongProbCache, model: str,
    split_name: str, min_trades: int = MIN_TRADES,
) -> TuneResult:  # fmt: skip
    combos = grid(decider, markets, label, span, flat_cache, bt.decision_every_min, bt.risk.p_edge_full)
    rows = []
    best: tuple[float, dict[str, Any], int] | None = None
    for c in combos:
        pol = PolicyConfig(model=model, label_config_sha256=label.sha256, **c)
        res = await run_backtest(markets, decider, pol, label, span, bt, registry, flat_cache, long_cache)
        n, pnl = len(res.trades), float(res.trades["pnl_eur"].sum()) if len(res.trades) else 0.0
        rows.append({**c, "n_trades": n, "pnl_eur": pnl})
        if n >= min_trades and (best is None or pnl > best[0]):
            best = (pnl, c, n)
    table = pd.DataFrame(rows)
    if best is None:
        return TuneResult(None, table, "untunable")
    c = best[1]
    policy = PolicyConfig(
        model=model, label_config_sha256=label.sha256, **c, tuned_on={  # type: ignore[call-arg]
            "split": split_name, "span": span.tag(), "n_trades": best[2], "objective": "net_pnl_eur",
            "min_trades": min_trades, "cadence_min": bt.decision_every_min,
        },
    )  # fmt: skip
    return TuneResult(policy, table, "ok")


def backtest_config(cfg: TrainConfig, app: AppConfig, every_min: int) -> BacktestConfig:
    fm = app.dry_run.fill_model
    fill = FillModelParams(
        latency_ms=fm.latency_ms,
        queue_haircut=fm.queue_haircut,
        stop_extra_slip_bps=fm.stop_extra_slip_bps,
        slippage_buffer_bps=app.risk.slippage_buffer_bps,
    )
    return BacktestConfig(
        cfg.backtest.starting_quote, every_min, app.risk, fm.fallback_fees, app.execution.entry_timeout_s, fill
    )


def load_markets(root: Path, products: Sequence[str], label: LoadedLabelConfig) -> dict[str, ProductMarket]:
    from modules.market.backfill import load_bars

    out: dict[str, ProductMarket] = {}
    btc = next((p for p in products if p.startswith("BTC-")), None)
    btc_market = build_market(btc, load_bars(root, btc), label) if btc else None
    for p in products:
        out[p] = (
            btc_market
            if p == btc and btc_market
            else build_market(p, load_bars(root, p), label, btc_market.ind if btc_market else None)
        )
    return out


def _write_policy(path: Path, result: TuneResult) -> None:
    if result.policy is None:
        path.write_text(json.dumps({"untunable": True}), encoding="utf-8", newline="\n")
    else:
        path.write_text(result.policy.model_dump_json(indent=2), encoding="utf-8", newline="\n")


async def tune_all(
    model_dir: Path, samples: pd.DataFrame, cfg: TrainConfig, label: LoadedLabelConfig, app: AppConfig,
    markets: Mapping[str, ProductMarket], registry: SpotRegistry, spans: SpanSet, cache_dir: Path | None = None,
) -> dict[str, TuneResult]:  # fmt: skip
    bt = backtest_config(cfg, app, TUNE_DECISION_EVERY_MIN)
    questions = build_questions(label.cfg.horizon_min, label.cfg.k_tp, label.cfg.k_sl, label.cfg.atr_tf)
    flat_cache, long_cache = FlatProbCache(cache_dir), LongProbCache()
    jobs = [(f"fold_{f.index}", f.val, f"val_fold_{f.index}") for f in spans.folds] + [
        (FINAL, spans.final_val, "val_final")
    ]
    out: dict[str, TuneResult] = {}
    report: dict[str, Any] = {}
    for sub, span, split in jobs:
        decider = BaselineDecider.load(
            model_dir, label=label, questions=questions, subset=sub, allow_degenerate=sub != FINAL
        )
        res = await tune(decider, markets, label, span, bt, registry, flat_cache, long_cache, model_dir.name, split)
        _write_policy(model_dir / sub / "policy.json", res)
        res.table.to_csv(model_dir / sub / "tune_table.csv", index=False)
        out[sub] = res
        report[sub] = {"reason": res.reason, "n_combinations": len(res.table),
                       "best": None if res.policy is None else res.policy.model_dump()}  # fmt: skip
        print(f"{sub}: {res.reason}, {len(res.table)} combinations", flush=True)
    (model_dir / "tune_report.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8", newline="\n"
    )
    return out


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Chronos policy tuning (spec sez. 3.5 punto 10)")
    ap.add_argument("--config", default="config/train.yml")
    ap.add_argument("--model", required=True, help="models/baseline/<version>")
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
    asyncio.run(
        tune_all(
            Path(args.model), samples, cfg, label, app, markets, registry, spans, Path(cfg.data_root) / "backtest_cache"
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
