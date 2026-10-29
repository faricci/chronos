"""Policy thresholds by grid search on the validation backtest (spec sez. 3.1.6, 3.5 punto 10; decisions O3.5, P1c).

::

    python -m training.tune_policy --config config/train.yml --model models/baseline/<version> [--dataset <run>]

Every combination of thresholds is evaluated by ``training.backtest.run_backtest``: the SAME ``DecisionPolicy``,
the SAME ``RiskManager`` (fee floor and EV gate with the ``p_be_buffer`` active), the same guard and venue. A
simplified simulation that skips the RiskManager is forbidden. The model answers are computed once per (model, span)
and reused by all combinations (only the thresholds change).

The grid is small on purpose (at most ``MAX_COMBINATIONS``): ``tau_buy`` at two quantiles of the masked P(BUY) of the
validation decisions whose regime allows an entry, ``c_min`` and the buffer over the live break-even. ``tau_sell`` is
fixed at 0.50 and never tuned; the regime rule is fixed too (``REGIME_RULE``). A combination is chosen only if it has
at least ``MIN_TRADES`` trades AND a positive net PnL on the validation: otherwise the subset is not tunable and its
test runs as "no entry" (the conservative choice), with the reason recorded:

1. ``untunable_calibration``  the ``edge`` calibration of the subset has too few effective samples (grid not run);
2. ``ok``                     a combination was chosen;
3. ``no_candidates_regime``   no validation decision has a regime that allows an entry;
4. ``untunable_ev``           even the 95th percentile of ``p_edge - p_breakeven`` is negative: the model never sees
                              a positive expected value in the validation window;
5. ``untunable_pnl``          every other case.

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
from typing import Any, Final

import numpy as np
import pandas as pd

from modules.config import EXIT_CONFIG_ERROR, AppConfig
from modules.decision.baseline import BaselineDecider
from modules.decision.policy import ENTRY_REGIMES, REGIME_FEATURE, REGIME_RULE, PolicyConfig, regimes_from_pctile
from modules.decision.questions import build_questions
from modules.dryrun.fill_model import FillModelParams
from modules.execution.spot import SpotRegistry
from modules.features.serializer import FLAT_COLUMNS
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
from training.baseline_lgbm import CALIBRATION_SCHEME, FINAL, latest_run, set_membership
from training.config import TrainConfig, load_train_config_or_exit
from training.fold_spans import SpanSet, compute_spans

MIN_TRADES: Final = 30
BUY_QUANTILES: Final = (0.80, 0.95)
C_MIN_GRID: Final = (0.0, 0.10)
P_BE_BUFFER_GRID: Final = (0.00, 0.02, 0.04, 0.06, 0.08)
TAU_SELL: Final = 0.50  # a priori: SELL more probable than HOLD among the valid actions; never tuned
MAX_COMBINATIONS: Final = 20

UNTUNABLE_CALIBRATION: Final = "untunable_calibration"
OK: Final = "ok"
NO_CANDIDATES_REGIME: Final = "no_candidates_regime"
UNTUNABLE_EV: Final = "untunable_ev"
UNTUNABLE_PNL: Final = "untunable_pnl"


@dataclass(frozen=True)
class TuneResult:
    policy: PolicyConfig | None
    table: pd.DataFrame  # one row per combination: thresholds, n_trades, pnl_eur
    reason: str  # "ok" | "untunable_pnl" (or "no_candidates_regime" when there was no grid) | ...


class ModelSchemeError(Exception):
    """The model was trained with another calibration scheme than the one this tuner understands."""


def probe_registry(probe_path: Path, products: Sequence[str]) -> SpotRegistry:
    """Spot registry from the product facts stored by the read-only probe (never typed by hand)."""
    probe = json.loads(probe_path.read_text(encoding="utf-8"))
    raw = {p: {"product_id": p, **probe["products"][p]["meta"]} for p in products}
    return SpotRegistry.from_raw(raw)


def grid(
    decider: BaselineDecider, markets: Mapping[str, ProductMarket], label: LoadedLabelConfig, span: Span,
    cache: FlatProbCache, every_min: int,
) -> list[dict[str, Any]]:  # fmt: skip
    """Threshold combinations: ``tau_buy`` at quantiles of the masked P(BUY) of the decisions whose regime allows an
    entry (so that every combination can trade), times ``c_min`` and the break-even buffer."""
    key = f"{model_key(decider, label)}-{span.tag()}-D{every_min}"
    masked = []
    for pm in markets.values():
        rows = decision_rows(pm, label, span, every_min)
        fa = cache.get(key, decider, pm, label, rows)
        p = fa.answers.p_action
        ok = np.isin(regimes_from_pctile(fa.vol_pctile), np.array(ENTRY_REGIMES))
        masked.append((p[:, 0] / np.maximum(p[:, 0] + p[:, 2], 1e-12))[ok])
    m = np.concatenate(masked) if masked else np.empty(0)
    if m.size == 0:
        return []
    taus = sorted({round(float(np.quantile(m, q)), 4) for q in BUY_QUANTILES})
    combos = [
        {"tau_buy": tb, "c_min": c, "p_be_buffer": b, "tau_sell": TAU_SELL, "regime_rule": REGIME_RULE}
        for tb, c, b in itertools.product(taus, C_MIN_GRID, P_BE_BUFFER_GRID)
    ]
    assert len(combos) <= MAX_COMBINATIONS
    return combos


def _rank(pnl: float, c: Mapping[str, Any]) -> tuple[float, float, float, float]:
    """Best PnL; on a tie the most prudent: highest buffer, then highest ``tau_buy``, then highest ``c_min``."""
    return (pnl, float(c["p_be_buffer"]), float(c["tau_buy"]), float(c["c_min"]))


async def tune(
    decider: BaselineDecider, markets: Mapping[str, ProductMarket], label: LoadedLabelConfig, span: Span,
    bt: BacktestConfig, registry: SpotRegistry, flat_cache: FlatProbCache, long_cache: LongProbCache, model: str,
    split_name: str, min_trades: int = MIN_TRADES,
) -> TuneResult:  # fmt: skip
    combos = grid(decider, markets, label, span, flat_cache, bt.decision_every_min)
    if not combos:
        return TuneResult(None, pd.DataFrame(columns=["n_trades", "pnl_eur"]), NO_CANDIDATES_REGIME)
    rows = []
    best: tuple[tuple[float, ...], dict[str, Any], int, float] | None = None
    for c in combos:
        pol = PolicyConfig(model=model, label_config_sha256=label.sha256, **c)
        res = await run_backtest(markets, decider, pol, label, span, bt, registry, flat_cache, long_cache)
        n, pnl = len(res.trades), float(res.trades["pnl_eur"].sum()) if len(res.trades) else 0.0
        rows.append({**c, "n_trades": n, "pnl_eur": pnl})
        if n >= min_trades and pnl > 0 and (best is None or _rank(pnl, c) > best[0]):
            best = (_rank(pnl, c), c, n, pnl)
    table = pd.DataFrame(rows)
    if best is None:
        return TuneResult(None, table, UNTUNABLE_PNL)
    policy = PolicyConfig(
        model=model, label_config_sha256=label.sha256, **best[1], tuned_on={  # type: ignore[call-arg]
            "split": split_name, "span": span.tag(), "n_trades": best[2], "pnl_eur": best[3],
            "objective": "net_pnl_eur", "min_trades": min_trades, "cadence_min": bt.decision_every_min,
        },
    )  # fmt: skip
    return TuneResult(policy, table, OK)


def val_diagnostics(
    decider: BaselineDecider, samples: pd.DataFrame, member: np.ndarray, rho: float, n_products: int
) -> dict[str, Any]:
    """What the model says on the ``edge`` rows of the validation set, with the calibration of the subset.

    ``bias_z`` tells a calibration problem (the mean prediction differs from the observed frequency) from a model that
    simply sees no edge; ``ev_headroom_q95`` is the 95th percentile of ``p_edge - p_breakeven`` on the rows whose
    regime allows an entry (the state tokens, the same the policy and the RiskManager see).
    """
    s = samples[(member == "val") & (samples["position_side"] == "FLAT") & samples["edge_target"].notna()]
    if len(s) == 0:
        return {"n_rows": 0}
    x = s[list(FLAT_COLUMNS)].to_numpy(dtype=np.float64)
    p = decider.answers_batch(x).p_edge
    w = s["w_edge"].to_numpy(dtype=np.float64)
    y = s["edge_target"].to_numpy(dtype=np.float64)
    regime = regimes_from_pctile(x[:, FLAT_COLUMNS.index(REGIME_FEATURE)])
    ok = np.isin(regime, np.array(ENTRY_REGIMES))
    n_eff = float(w.sum() / (1.0 + (n_products - 1) * rho))
    mean_p = float(np.average(p, weights=w))
    p_obs = float(np.average(y, weights=w))
    se = float(np.sqrt(max(p_obs * (1 - p_obs), 1e-12) / max(n_eff, 1e-12)))
    pbe = x[:, FLAT_COLUMNS.index("p_breakeven")]
    head = (p - pbe)[ok & np.isfinite(pbe)]
    return {
        "n_rows": int(len(s)),
        "n_rows_regime_ok": int(ok.sum()),
        "regime_share": {str(r): float(np.mean(regime == r)) for r in range(5)}
        | {"missing": float(np.mean(regime < 0))},
        "mean_p_edge": mean_p,
        "p_hist_obs": p_obs,
        "n_eff": n_eff,
        "se": se,
        "bias_z": (mean_p - p_obs) / se,
        "ev_headroom_q95": float(np.quantile(head, 0.95)) if head.size else None,
    }  # fmt: skip


def classify(result: TuneResult, diag: Mapping[str, Any]) -> str:
    """Refine a failed tuning into its reason (see the module docstring, rules 3-5)."""
    if result.reason != UNTUNABLE_PNL:
        return result.reason
    if diag.get("n_rows_regime_ok", 0) == 0:
        return NO_CANDIDATES_REGIME
    head = diag.get("ev_headroom_q95")
    if head is not None and head < 0:
        return UNTUNABLE_EV
    return UNTUNABLE_PNL


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


def _write_policy(path: Path, reason: str, policy: PolicyConfig | None) -> None:
    if policy is None:
        path.write_text(json.dumps({"untunable": True, "reason": reason}), encoding="utf-8", newline="\n")
    else:
        path.write_text(policy.model_dump_json(indent=2), encoding="utf-8", newline="\n")


def check_scheme(card: Mapping[str, Any]) -> None:
    """A model trained with another calibration scheme is not tuned with this code."""
    if card.get("calibration_scheme") != CALIBRATION_SCHEME:
        raise ModelSchemeError(
            f"model calibration_scheme {card.get('calibration_scheme')!r} != {CALIBRATION_SCHEME!r}: retrain the model"
        )


async def tune_all(
    model_dir: Path, samples: pd.DataFrame, cfg: TrainConfig, label: LoadedLabelConfig, app: AppConfig,
    markets: Mapping[str, ProductMarket], registry: SpotRegistry, spans: SpanSet, cache_dir: Path | None = None,
) -> dict[str, TuneResult]:  # fmt: skip
    card = json.loads((model_dir / "model_card.json").read_text(encoding="utf-8"))
    check_scheme(card)
    bt = backtest_config(cfg, app, TUNE_DECISION_EVERY_MIN)
    questions = build_questions(label.cfg.horizon_min, label.cfg.k_tp, label.cfg.k_sl, label.cfg.atr_tf)
    flat_cache, long_cache = FlatProbCache(cache_dir), LongProbCache()
    members = set_membership(samples, label, cfg)
    rho = float(label.raw.get("feasibility", {}).get("rho_raw", 1.0))
    jobs = [(f"fold_{f.index}", f.val, f"val_fold_{f.index}") for f in spans.folds] + [
        (FINAL, spans.final_val, "val_final")
    ]
    out: dict[str, TuneResult] = {}
    report: dict[str, Any] = {}
    for sub, span, split in jobs:
        decider = BaselineDecider.load(
            model_dir, label=label, questions=questions, subset=sub, allow_degenerate=sub != FINAL
        )
        edge_cal = card["folds"][sub]["edge_cal"]
        diag = val_diagnostics(decider, samples, members[sub], rho, len(markets))
        if not edge_cal["ok"]:
            res = TuneResult(None, pd.DataFrame(columns=["n_trades", "pnl_eur"]), UNTUNABLE_CALIBRATION)
        else:
            res = await tune(decider, markets, label, span, bt, registry, flat_cache, long_cache, model_dir.name, split)
            res = TuneResult(res.policy, res.table, classify(res, diag))
        _write_policy(model_dir / sub / "policy.json", res.reason, res.policy)
        res.table.to_csv(model_dir / sub / "tune_table.csv", index=False)
        out[sub] = res
        report[sub] = {"reason": res.reason, "n_combinations": len(res.table),
                       "best": None if res.policy is None else res.policy.model_dump(),
                       "edge_cal": edge_cal, "val_diag": diag}  # fmt: skip
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
    try:
        asyncio.run(
            tune_all(
                Path(args.model),
                samples,
                cfg,
                label,
                app,
                markets,
                registry,
                spans,
                Path(cfg.data_root) / "backtest_cache",
            )
        )
    except ModelSchemeError as e:
        print(str(e), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
