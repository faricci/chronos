"""Baseline LightGBM models (spec sez. 3.5 punto 9, decision O3).

For every walk-forward fold ``k`` and for the final model it trains three boosters (``action``, ``edge``,
``vol_regime``) on the rows of ``samples.parquet`` that belong to the training set and stops on the weighted validation
logloss. ``action`` and ``vol_regime`` are calibrated on the validation set; the Platt map of ``edge`` is fitted on the
out-of-fold scores of the train rows plus the validation scores (``OOF_PLATT_V1``). The test set of a fold is never
used for any fit.

::

    python -m training.baseline_lgbm --config config/train.yml [--dataset data/datasets/<run_id>]

Rules that matter (docs/decisions/2026-10-18-stato-backtest-baseline.md, O3):

- the uniqueness weights are normalised to mean 1 inside each (model, set): with raw weights around 0.01 an L2 of 10
  would flatten every leaf;
- ``action`` trains on FLAT and synthetic LONG rows, ``edge`` and ``vol_regime`` on FLAT rows only and without the
  ``pos_*`` features; HOLD is subsampled to 3:1 in training and the prior is corrected by ``ln(keep_rate)`` when
  serving;
- no hyper-parameter search anywhere; the parameters below are fixed and written to the model card;
- a fold whose training data cannot support a regime model (< 5 000 train or < 1 000 validation rows) is
  ``degenerate``: the regime answer is the constant "normal" there. The final model may not be degenerate.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import lightgbm as lgb
import numpy as np
import pandas as pd
from scipy import optimize

from modules.config import EXIT_CONFIG_ERROR
from modules.decision.baseline import (
    ACTION_FEATURES,
    HOLD,
    MARKET_FEATURES,
    Calibration,
    flat_columns_sha256,
    softmax,
)
from modules.decision.protocol import ACTIONS, REGIME_LEGEND
from modules.decision.questions import build_questions
from modules.risk.label_config import LoadedLabelConfig, load_label_config
from training.build_dataset import flat_splits
from training.config import TrainConfig, load_train_config_or_exit

LGBM_PARAMS: Final[dict[str, Any]] = {
    "boosting_type": "gbdt",
    "num_leaves": 7,
    "max_depth": 3,
    "learning_rate": 0.03,
    "min_data_in_leaf": 500,
    "min_sum_hessian_in_leaf": 1.0,
    "feature_fraction": 0.7,
    "bagging_fraction": 0.7,
    "bagging_freq": 1,
    "lambda_l1": 0.0,
    "lambda_l2": 10.0,
    "max_bin": 63,
    "min_data_in_bin": 50,
    "seed": 1234,
    "deterministic": True,
    "force_row_wise": True,
    "verbosity": -1,
}
NUM_BOOST_ROUND: Final = 600
EARLY_STOPPING_ROUNDS: Final = 50
HOLD_RATIO: Final = 3
VOL_MIN_TRAIN: Final = 5_000
VOL_MIN_VAL: Final = 1_000
ECE_BINS: Final = 15
FINAL: Final = "final"
CAL_FOLDS: Final = 4  # contiguous blocks of the cross-fitting that calibrates ``edge``
CAL_NEFF_MIN: Final = 200.0  # below this effective sample size the edge calibration is not trusted
CALIBRATION_SCHEME: Final = "OOF_PLATT_V1"


class TrainingError(Exception):
    """The data cannot support the requested models (exit code 1)."""


# ---------------------------------------------------------------------------------------- metrics


def _w(w: np.ndarray | None, n: int) -> np.ndarray:
    return np.ones(n) if w is None else np.asarray(w, dtype=np.float64)


def logloss_binary(p: np.ndarray, y: np.ndarray, w: np.ndarray | None = None) -> float:
    p = np.clip(np.asarray(p, dtype=np.float64), 1e-12, 1 - 1e-12)
    ww = _w(w, len(p))
    return float(-(ww * (y * np.log(p) + (1 - y) * np.log(1 - p))).sum() / ww.sum())


def logloss_multi(p: np.ndarray, y: np.ndarray, w: np.ndarray | None = None) -> float:
    ww = _w(w, len(p))
    q = np.clip(p[np.arange(len(p)), y.astype(int)], 1e-12, 1.0)
    return float(-(ww * np.log(q)).sum() / ww.sum())


def brier_binary(p: np.ndarray, y: np.ndarray, w: np.ndarray | None = None) -> float:
    ww = _w(w, len(p))
    return float((ww * (p - y) ** 2).sum() / ww.sum())


def ece(p: np.ndarray, y: np.ndarray, w: np.ndarray | None = None, bins: int = ECE_BINS) -> float:
    """Expected calibration error with equal-width bins, weighted."""
    ww = _w(w, len(p))
    edges = np.linspace(0.0, 1.0, bins + 1)
    which = np.clip(np.digitize(p, edges[1:-1]), 0, bins - 1)
    total = ww.sum()
    err = 0.0
    for b in range(bins):
        m = which == b
        if m.any():
            wb = ww[m].sum()
            err += wb / total * abs(float((ww[m] * y[m]).sum() / wb) - float((ww[m] * p[m]).sum() / wb))
    return float(err)


def prior_logloss(y: np.ndarray, w: np.ndarray | None = None) -> float:
    """Logloss of the base-rate predictor (the reference of gate G2)."""
    ww = _w(w, len(y))
    base = float((ww * y).sum() / ww.sum())
    return logloss_binary(np.full(len(y), base), y, ww)


# ------------------------------------------------------------------------------------- calibration


def fit_platt(z: np.ndarray, y: np.ndarray, w: np.ndarray) -> tuple[float, float]:
    """``p = sigmoid(a z + b)`` minimising the weighted NLL (BFGS from (1, 0))."""

    def nll(ab: np.ndarray) -> float:
        return logloss_binary(1.0 / (1.0 + np.exp(-(ab[0] * z + ab[1]))), y, w)

    res = optimize.minimize(nll, np.array([1.0, 0.0]), method="BFGS")
    return float(res.x[0]), float(res.x[1])


def fit_temperature(raw: np.ndarray, y: np.ndarray, w: np.ndarray) -> float:
    """``p = softmax(raw / T)`` with ``T`` in (0.05, 10) minimising the weighted NLL."""
    res = optimize.minimize_scalar(
        lambda t: logloss_multi(softmax(raw / t), y, w), bounds=(0.05, 10.0), method="bounded"
    )
    return float(res.x)


# --------------------------------------------------------------------------------------- the data


@dataclass(frozen=True)
class SetData:
    x: np.ndarray
    y: np.ndarray
    w: np.ndarray  # normalised to mean 1


def _normalise(w: np.ndarray) -> np.ndarray:
    return np.asarray(w / w.mean())


def _edge_frame(samples: pd.DataFrame, mask: np.ndarray) -> pd.DataFrame:
    """FLAT rows with a known ``edge_target`` inside one set (the rows of the ``edge`` model, in sample order)."""
    s = samples[mask]
    return s[(s["position_side"] == "FLAT") & s["edge_target"].notna()]


def _select(samples: pd.DataFrame, mask: np.ndarray, kind: str, features: Sequence[str]) -> SetData:
    """Rows of one model inside one set. ``mask``: rows of ``samples`` that belong to the set."""
    s = samples[mask]
    if kind == "action":
        y = s["action_target"].map({a: i for i, a in enumerate(ACTIONS)}).to_numpy(dtype=np.int64)
        w = s["w_action"].to_numpy(dtype=np.float64)
    elif kind == "edge":
        s = _edge_frame(samples, mask)
        y = s["edge_target"].to_numpy(dtype=np.int64)
        w = s["w_edge"].to_numpy(dtype=np.float64)
    elif kind == "vol_regime":
        s = s[(s["position_side"] == "FLAT") & (s["vol_target"] >= 0)]
        y = s["vol_target"].to_numpy(dtype=np.int64)
        w = s["w_vol"].to_numpy(dtype=np.float64)
    else:  # pragma: no cover
        raise ValueError(kind)
    if len(s) == 0:
        return SetData(np.empty((0, len(features))), np.empty(0, dtype=np.int64), np.empty(0))
    return SetData(s[list(features)].to_numpy(dtype=np.float64), y, _normalise(w))


def subsample_hold(train: SetData, seed: int, tag: int) -> tuple[SetData, float]:
    """Keep at most ``3 x`` the minority class of HOLD rows (uniform, seeded); return the data and ``keep_rate``."""
    n_buy, n_sell = int((train.y == 0).sum()), int((train.y == 1).sum())
    n_hold = int((train.y == HOLD).sum())
    minority = min(n_buy, n_sell)
    if minority == 0 or n_hold <= HOLD_RATIO * minority:
        return train, 1.0
    rng = np.random.default_rng([seed, tag])
    hold_idx = np.flatnonzero(train.y == HOLD)
    keep_hold = rng.choice(hold_idx, size=HOLD_RATIO * minority, replace=False)
    keep = np.sort(np.concatenate([np.flatnonzero(train.y != HOLD), keep_hold]))
    return SetData(train.x[keep], train.y[keep], _normalise(train.w[keep])), len(keep_hold) / n_hold


def _fit(kind: str, train: SetData, val: SetData, threads: int) -> tuple[lgb.Booster, int]:
    params = dict(LGBM_PARAMS, num_threads=threads)
    if kind == "edge":
        params.update(objective="binary", metric="binary_logloss")
    else:
        n_class = len(ACTIONS) if kind == "action" else len(REGIME_LEGEND)
        params.update(objective="multiclass", num_class=n_class, metric="multi_logloss")
    dtrain = lgb.Dataset(train.x, label=train.y, weight=train.w, free_raw_data=False)
    dval = lgb.Dataset(val.x, label=val.y, weight=val.w, reference=dtrain, free_raw_data=False)
    booster = lgb.train(
        params, dtrain, num_boost_round=NUM_BOOST_ROUND, valid_sets=[dval],
        callbacks=[lgb.early_stopping(EARLY_STOPPING_ROUNDS, first_metric_only=True, verbose=False)],
    )  # fmt: skip
    return booster, int(booster.best_iteration or booster.current_iteration())


def _raw(booster: lgb.Booster, data: SetData, n_class: int | None) -> np.ndarray:
    raw = np.asarray(booster.predict(data.x, raw_score=True))
    return raw.reshape(len(data.x), n_class) if n_class else raw.reshape(len(data.x))


def _cal_blocks(tr: pd.DataFrame, embargo: timedelta) -> list[tuple[np.ndarray, np.ndarray]]:
    """``(block, fit)`` positions of ``tr``: ``CAL_FOLDS`` contiguous blocks by ``(t, product)``; the fit rows are
    the ones whose ``[t, label_window_end]`` ends more than ``embargo`` before the block or starts more than
    ``embargo`` after it (purge on both sides)."""
    order = tr.reset_index(drop=True).sort_values(["t", "product"], kind="stable").index.to_numpy()
    t = tr["t"].to_numpy(dtype="datetime64[ns]")
    end = tr["label_window_end"].to_numpy(dtype="datetime64[ns]")
    gap = np.timedelta64(embargo)
    out: list[tuple[np.ndarray, np.ndarray]] = []
    for block in np.array_split(order, CAL_FOLDS):
        if len(block) == 0:
            continue
        start, last = t[block].min(), end[block].max()
        out.append((block, np.flatnonzero((end < start - gap) | (t > last + gap))))
    return out


def oof_edge_scores(
    tr: pd.DataFrame, x: np.ndarray, y: np.ndarray, n_rounds: int, embargo: timedelta, threads: int
) -> np.ndarray:
    """Raw ``edge`` score of every train row from a booster that never saw it (cross-fitting with purge, no early
    stopping, ``n_rounds`` trees, ``w_edge`` normalised to mean 1). NaN where the block is not evaluated.

    ``tr``: the rows behind ``x`` / ``y`` (same order) with ``t``, ``product``, ``label_window_end`` and ``w_edge``.
    """
    params = dict(LGBM_PARAMS, num_threads=threads, objective="binary")
    w = tr["w_edge"].to_numpy(dtype=np.float64)
    z = np.full(len(tr), np.nan)
    for block, fit in _cal_blocks(tr, embargo):
        if len(fit) < 2 * int(LGBM_PARAMS["min_data_in_leaf"]):
            continue
        dfit = lgb.Dataset(x[fit], label=y[fit], weight=_normalise(w[fit]))
        booster = lgb.train(params, dfit, num_boost_round=n_rounds)
        z[block] = np.asarray(booster.predict(x[block], raw_score=True)).reshape(len(block))
    return z


# ----------------------------------------------------------------------------------------- splits


def set_membership(samples: pd.DataFrame, label: LoadedLabelConfig, cfg: TrainConfig) -> dict[str, np.ndarray]:
    """``{'fold_0': per-row 'train'|'val'|'test'|'', ..., 'final': ...}``; LONG rows inherit their FLAT row."""
    flat = samples[samples["position_side"] == "FLAT"].sort_values(["t", "product"], kind="stable")
    fs = flat_splits(flat, label, cfg)  # live rows (``source == 'live'``) are training rows only: see FlatSplits
    keys = flat[["product", "t"]].reset_index(drop=True)
    out: dict[str, np.ndarray] = {}
    for f in fs.splits.folds:
        tag = np.full(len(flat), "", dtype=object)
        tag[f.train], tag[f.val], tag[f.test] = "train", "val", "test"
        tag = fs.train_only(tag)
        merged = samples[["product", "t"]].merge(keys.assign(s=tag), on=["product", "t"], how="left")
        out[f"fold_{f.index}"] = merged["s"].fillna("").to_numpy(dtype=object)
    out[FINAL] = np.where(samples["final_split"].isin(["train", "val"]), samples["final_split"], "").astype(object)
    return out


# ------------------------------------------------------------------------------------- one subset


def fit_subset(
    samples: pd.DataFrame, member: np.ndarray, cfg: TrainConfig, tag: int, allow_degenerate: bool, rho: float
) -> dict[str, Any]:
    """Boosters + calibration + metrics for one fold (or the final model). Returns plain objects ready to be saved.

    ``rho``: pairwise correlation of the products' labels (``feasibility.rho_raw``), for the ``N_eff`` of the edge
    calibration set. ``result["edge_cal"]["ok"]`` is False when that set is too small; the caller decides what follows.
    """
    threads = cfg.lgbm.num_threads
    result: dict[str, Any] = {"boosters": {}, "metrics": {}, "best_iteration": {}, "n_rows": {}}
    cal: dict[str, Any] = {}

    # ---- action
    tr, va = (_select(samples, member == s, "action", ACTION_FEATURES) for s in ("train", "val"))
    if len(tr.y) == 0 or len(va.y) == 0:
        raise TrainingError("action: empty train or validation set")
    tr_s, keep_rate = subsample_hold(tr, cfg.seed, tag)
    booster, best = _fit("action", tr_s, va, threads)
    raw_va = _raw(booster, va, len(ACTIONS))
    cal["action_hold_log_prior"] = float(np.log(keep_rate)) if keep_rate < 1.0 else 0.0
    shifted = raw_va.copy()
    shifted[:, HOLD] += cal["action_hold_log_prior"]
    cal["T_action"] = fit_temperature(shifted, va.y, va.w)
    p_before, p_after = softmax(shifted), Calibration(**cal).action_probs(raw_va)
    buy = (va.y == 0).astype(float)
    result["boosters"]["action"], result["best_iteration"]["action"] = booster, best
    result["n_rows"]["action"] = {"train": len(tr.y), "train_after_hold_subsample": len(tr_s.y), "val": len(va.y)}
    result["metrics"]["action"] = {
        "val_logloss_before": logloss_multi(p_before, va.y, va.w),
        "val_logloss_after": logloss_multi(p_after, va.y, va.w),
        "val_ece_buy_after": ece(p_after[:, 0], buy, va.w), "hold_keep_rate": keep_rate,
    }  # fmt: skip

    # ---- edge
    tr, va = (_select(samples, member == s, "edge", MARKET_FEATURES) for s in ("train", "val"))
    if len(tr.y) == 0 or len(va.y) == 0:
        raise TrainingError("edge: empty train or validation set")
    booster, best = _fit("edge", tr, va, threads)
    z = _raw(booster, va, None)
    ftr, fva = (_edge_frame(samples, member == s) for s in ("train", "val"))
    z_oof = oof_edge_scores(ftr, tr.x, tr.y, best, timedelta(days=cfg.embargo_days), threads)
    done = np.isfinite(z_oof)  # the calibration set C: OOF train scores + validation scores, never test rows
    w_raw = np.concatenate([ftr["w_edge"].to_numpy(dtype=np.float64)[done], fva["w_edge"].to_numpy(dtype=np.float64)])
    k = len(set(ftr["product"].to_numpy()[done]) | set(fva["product"].to_numpy()))
    n_eff = float(w_raw.sum() / (1.0 + (k - 1) * rho))
    a, b = fit_platt(
        np.concatenate([z_oof[done], z]), np.concatenate([tr.y[done], va.y]).astype(float), _normalise(w_raw)
    )
    cal["platt_edge"] = {"a": a, "b": b}
    cal["edge_cal"] = result["edge_cal"] = {
        "scheme": CALIBRATION_SCHEME, "n_rows_oof": int(done.sum()), "n_rows_val": len(va.y), "n_eff": n_eff,
        "n_eff_min": CAL_NEFF_MIN, "ok": bool(n_eff >= CAL_NEFF_MIN),
    }  # fmt: skip
    p_raw = 1.0 / (1.0 + np.exp(-z))
    p_cal = Calibration(**cal).edge_probs(z)
    y = va.y.astype(float)
    result["boosters"]["edge"], result["best_iteration"]["edge"] = booster, best
    result["edge_base_rate"] = float((tr.w * tr.y).sum() / tr.w.sum())  # what a feature-less model would predict
    result["n_rows"]["edge"] = {"train": len(tr.y), "val": len(va.y)}
    result["metrics"]["edge"] = {
        "val_logloss_before": logloss_binary(p_raw, y, va.w), "val_logloss_after": logloss_binary(p_cal, y, va.w),
        "val_brier_after": brier_binary(p_cal, y, va.w), "val_ece_before": ece(p_raw, y, va.w),
        "val_ece_after": ece(p_cal, y, va.w), "val_prior_logloss": prior_logloss(y, va.w),
    }  # fmt: skip

    # ---- vol_regime
    tr, va = (_select(samples, member == s, "vol_regime", MARKET_FEATURES) for s in ("train", "val"))
    degenerate = len(tr.y) < VOL_MIN_TRAIN or len(va.y) < VOL_MIN_VAL
    result["n_rows"]["vol_regime"] = {"train": len(tr.y), "val": len(va.y)}
    result["degenerate"] = {"vol_regime": degenerate}
    if degenerate:
        if not allow_degenerate:
            raise TrainingError(f"vol_regime: too few rows (train {len(tr.y)}, val {len(va.y)}) for the final model")
        cal["T_vol_regime"] = 1.0
    else:
        booster, best = _fit("vol_regime", tr, va, threads)
        raw = _raw(booster, va, len(REGIME_LEGEND))
        cal["T_vol_regime"] = fit_temperature(raw, va.y, va.w)
        result["boosters"]["vol_regime"], result["best_iteration"]["vol_regime"] = booster, best
        result["metrics"]["vol_regime"] = {
            "val_logloss_before": logloss_multi(softmax(raw), va.y, va.w),
            "val_logloss_after": logloss_multi(Calibration(**cal).regime_probs(raw), va.y, va.w),
        }
    result["calibration"] = Calibration(**cal)
    return result


def test_metrics(samples: pd.DataFrame, member: np.ndarray, res: Mapping[str, Any]) -> dict[str, float]:
    """Edge / action metrics of the fold's test set (reported; no gate decides here)."""
    cal: Calibration = res["calibration"]
    out: dict[str, float] = {}
    te = _select(samples, member == "test", "edge", MARKET_FEATURES)
    if len(te.y):
        p = cal.edge_probs(_raw(res["boosters"]["edge"], te, None))
        y = te.y.astype(float)
        prior = logloss_binary(np.full(len(y), res["edge_base_rate"]), y, te.w)  # the TRAIN base rate, not the test's
        out.update(
            edge_logloss=logloss_binary(p, y, te.w),
            edge_prior_logloss=prior,
            edge_brier=brier_binary(p, y, te.w),
            edge_ece=ece(p, y, te.w),
            n_edge=float(len(y)),
        )
    ta = _select(samples, member == "test", "action", ACTION_FEATURES)
    if len(ta.y):
        pa = cal.action_probs(_raw(res["boosters"]["action"], ta, len(ACTIONS)))
        out.update(
            action_logloss=logloss_multi(pa, ta.y, ta.w), action_ece_buy=ece(pa[:, 0], (ta.y == 0).astype(float), ta.w)
        )
    return out


# --------------------------------------------------------------------------------------- the run


def _version(models_dir: Path, today: datetime) -> str:
    stem = f"baseline-lgbm@{today:%Y-%m-%d}"
    n = 1
    while (models_dir / f"{stem}.{n}").exists():
        n += 1
    return f"{stem}.{n}"


def _save(sub: Path, res: Mapping[str, Any]) -> None:
    sub.mkdir(parents=True, exist_ok=True)
    for name, booster in res["boosters"].items():
        (sub / f"{name}.txt").write_text(booster.model_to_string(), encoding="utf-8", newline="\n")
    (sub / "calibration.json").write_text(res["calibration"].model_dump_json(indent=2), encoding="utf-8", newline="\n")


def final_train_end(samples: pd.DataFrame) -> str | None:
    """When the data of the ``final`` model ends: the largest ``label_window_end`` of the rows it trains and calibrates
    on (``final_split`` train or val), UTC ISO. Rule L5 (V1) needs it to prove that a backtest window is out of sample:
    ``final_train_end <= window start - embargo``. None: the model used no row at all."""
    used = samples.loc[samples["final_split"].isin(["train", "val"]), "label_window_end"]
    if used.empty:
        return None
    return str(pd.Timestamp(used.max()).strftime("%Y-%m-%dT%H:%M:%SZ"))


def _git_commit() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10, check=True)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def train_baseline(
    samples: pd.DataFrame, label: LoadedLabelConfig, cfg: TrainConfig, run_dir: Path, models_dir: Path,
    *, label_config_path: Path, now: datetime | None = None, allow_degenerate_final: bool = False,
) -> Path:  # fmt: skip
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    if manifest["label_config_sha256"] != label.sha256 or manifest["flat_columns_sha256"] != flat_columns_sha256():
        raise TrainingError("the dataset was built for another label_config or state layout")
    now = now or datetime.now(UTC)
    version = _version(models_dir, now)
    root = models_dir / version
    members = set_membership(samples, label, cfg)
    rho = float(label.raw.get("feasibility", {}).get("rho_raw", 1.0))
    folds: dict[str, Any] = {}
    degenerate: dict[str, dict[str, bool]] = {}
    for name, member in members.items():
        is_final = name == FINAL
        tag = 99 if is_final else int(name.split("_")[1])
        print(f"[{name}] start {datetime.now(UTC):%Y-%m-%d %H:%M:%S}", flush=True)
        res = fit_subset(samples, member, cfg, tag, allow_degenerate=(not is_final) or allow_degenerate_final, rho=rho)
        ec = res["edge_cal"]
        print(f"[{name}] end {datetime.now(UTC):%Y-%m-%d %H:%M:%S} edge_cal n_eff={ec['n_eff']:.1f} ok={ec['ok']}",
              flush=True)  # fmt: skip
        if is_final and not ec["ok"]:
            raise TrainingError(f"final: edge calibration N_eff {ec['n_eff']:.1f} < {CAL_NEFF_MIN:.0f}")
        _save(root / name, res)
        degenerate[name] = res["degenerate"]
        folds[name] = {
            "best_iteration": res["best_iteration"], "n_rows": res["n_rows"], "metrics_val": res["metrics"],
            "edge_cal": res["edge_cal"],
            "metrics_test": {} if is_final else test_metrics(samples, member, res),
        }  # fmt: skip
    questions = build_questions(label.cfg.horizon_min, label.cfg.k_tp, label.cfg.k_sl, label.cfg.atr_tf)
    shutil.copyfile(label_config_path, root / "label_config.json")
    features = {
        "action": list(ACTION_FEATURES),
        "edge": list(MARKET_FEATURES),
        "vol_regime": list(MARKET_FEATURES),
        "flat_columns_sha256": flat_columns_sha256(),
    }
    (root / "features.json").write_text(json.dumps(features, indent=2), encoding="utf-8", newline="\n")
    lc = label.cfg
    card = {
        "model": version,
        "backend": "baseline",
        "state_schema": manifest["state_schema"],
        "flat_columns_sha256": flat_columns_sha256(),
        "questions_version": questions.version_tag,
        "label_config": {
            "k_tp": lc.k_tp,
            "k_sl": lc.k_sl,
            "horizon_min": lc.horizon_min,
            "atr": f"ATR_14_{lc.atr_tf}",
            "atr_tf": lc.atr_tf,
            "entry_mode": lc.entry_mode,
            "entry_timeout_min": lc.entry_timeout_min,
            "entry_first_bar": lc.entry_first_bar,
            "cost_model": "COST_V1",
            "fees_bps": {
                "maker": label.fees.maker_bps,
                "taker": label.fees.taker_bps,
                "source": "assumed" if label.fees.assumed else "probe",
            },
            "slip_bps": label.costs.slippage_buffer_bps,
            "stop_extra_slip_bps": label.costs.stop_extra_slip_bps,
            "spread_bps": label.spread_bps,
            "selection": {"holdout_start": label.holdout_start.isoformat()},
            "sha256": label.sha256,
        },
        "data": {
            "products": manifest["products"],
            "indicator_anchor": manifest["indicator_anchor"],
            "dataset_run_id": manifest["run_id"],
            "holdout": [manifest["holdout_start"], max(manifest["last_bar"].values())],
            "final_train_end": final_train_end(samples),
            "rows": manifest["rows"],
        },  # fmt: skip
        "sentiment_available": False,
        "lgbm": {
            "params": LGBM_PARAMS,
            "num_boost_round": NUM_BOOST_ROUND,
            "early_stopping_rounds": EARLY_STOPPING_ROUNDS,
        },
        "calibration_scheme": CALIBRATION_SCHEME,
        "degenerate": degenerate,
        "folds": folds,
        "calibration": json.loads((root / FINAL / "calibration.json").read_text(encoding="utf-8")),
        "metrics_holdout": {},
        "git_commit": _git_commit(),
        "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if "live" in manifest:  # the live labels (rows, weight, exclusions) that shaped this model: reproducibility
        card["data"]["live"] = manifest["live"]
    (root / "model_card.json").write_text(json.dumps(card, indent=2, sort_keys=True), encoding="utf-8", newline="\n")
    return root


def latest_run(datasets_dir: Path) -> Path:
    runs = sorted(p for p in datasets_dir.iterdir() if p.is_dir() and (p / "samples.parquet").exists())
    if not runs:
        raise TrainingError(f"no dataset run in {datasets_dir}")
    return runs[-1]


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Chronos baseline LightGBM (spec sez. 3.5 punto 9)")
    ap.add_argument("--config", default="config/train.yml")
    ap.add_argument("--dataset", default=None, help="dataset run directory (default: the latest)")
    args = ap.parse_args(argv)
    cfg = load_train_config_or_exit(args.config)
    try:
        label = load_label_config(Path(cfg.label_config))
    except (ValueError, OSError) as e:
        print(f"label_config invalid: {e}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    try:
        run_dir = Path(args.dataset) if args.dataset else latest_run(Path(cfg.datasets_dir))
        samples = pd.read_parquet(run_dir / "samples.parquet")
        root = train_baseline(
            samples, label, cfg, run_dir, Path(cfg.models_dir), label_config_path=Path(cfg.label_config)
        )
    except TrainingError as e:
        print(f"training error: {e}", file=sys.stderr)
        return 1
    print(f"baseline written to {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
