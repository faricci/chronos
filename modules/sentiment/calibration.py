"""Temperature scaling of the sentiment classifier (spec sez. 1.1.2, sez. 1.3 punto 6).

``python -m modules.sentiment.calibration --labels data/labels/headlines.csv [--strict]``

Not blocking in the local phase (the LightGBM baseline absorbs a monotone distortion of the probabilities, so the model
starts with ``T = 1.0`` and ``calibrated: false``); mandatory before an LLM decider consumes the sentiment or ``ewm``
enters a policy rule. Procedure:

1. the labelled headlines (``headlines.csv`` of ``label_review``) go through the model: raw logits in Choice order;
2. seeded random split 70 / 30: ``T`` is fitted on the 70 % (minimum NLL of ``softmax(z / T)``,
   ``scipy.optimize.minimize_scalar``, bounds 0.05..10);
3. ECE (15 bins, top-label) and Brier (multiclass) are measured on the held-out 30 %, with ``T = 1`` and the new ``T``;
4. the model card is rewritten with the new ``T``, ``calibrated: true`` and the metrics.

``--strict``: exit code 1, and the card is NOT touched, if ECE after calibration is above 0.07. Fewer than 1000 labelled
headlines is reported as a warning (the threshold of the spec is defined on >= 1000).
"""

from __future__ import annotations

import argparse
import csv
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import numpy as np
import numpy.typing as npt
from scipy.optimize import minimize_scalar

from modules.sentiment.classifier import (
    FloatArray,
    ModelCard,
    OnnxSentimentClassifier,
    load_model_card,
    model_text,
    save_model_card,
    softmax,
    try_load_classifier,
)
from modules.sentiment.service import resolve_config

ECE_BINS: Final = 15
ECE_MAX: Final = 0.07
MIN_LABELS: Final = 1_000
T_BOUNDS: Final = (0.05, 10.0)
TRAIN_FRACTION: Final = 0.7

IntArray = npt.NDArray[np.int64]


def nll(logits: FloatArray, labels: IntArray, temperature: float) -> float:
    p = softmax(logits / temperature)
    return float(-np.log(np.clip(p[np.arange(len(labels)), labels], 1e-12, 1.0)).mean())


def ece(probs: FloatArray, labels: IntArray, n_bins: int = ECE_BINS) -> float:
    """Expected calibration error of the top label, ``n_bins`` equal-width bins on the confidence."""
    conf = probs.max(axis=1)
    correct = (probs.argmax(axis=1) == labels).astype(np.float64)
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    idx = np.clip(np.digitize(conf, edges[1:-1], right=True), 0, n_bins - 1)
    total = 0.0
    for b in range(n_bins):
        mask = idx == b
        if mask.any():
            total += mask.mean() * abs(float(correct[mask].mean()) - float(conf[mask].mean()))
    return float(total)


def brier(probs: FloatArray, labels: IntArray) -> float:
    """Multiclass Brier score: mean over samples of the squared distance to the one-hot label."""
    onehot = np.eye(probs.shape[1])[labels]
    return float(((probs - onehot) ** 2).sum(axis=1).mean())


def fit_temperature(logits: FloatArray, labels: IntArray) -> float:
    res = minimize_scalar(lambda t: nll(logits, labels, t), bounds=T_BOUNDS, method="bounded")
    return float(res.x)


@dataclass(frozen=True, slots=True)
class CalibrationReport:
    n_fit: int
    n_eval: int
    temperature: float
    ece_before: float
    ece_after: float
    brier_before: float
    brier_after: float
    nll_before: float
    nll_after: float


def calibrate(logits: FloatArray, labels: IntArray, *, seed: int = 0) -> CalibrationReport:
    n = len(labels)
    if n < 10:
        raise ValueError(f"{n} labelled headlines are not enough to calibrate anything")
    order = np.random.default_rng(seed).permutation(n)
    cut = int(n * TRAIN_FRACTION)
    fit, ev = order[:cut], order[cut:]
    t = fit_temperature(logits[fit], labels[fit])
    before, after = softmax(logits[ev]), softmax(logits[ev] / t)
    return CalibrationReport(
        n_fit=len(fit),
        n_eval=len(ev),
        temperature=t,
        ece_before=ece(before, labels[ev]),
        ece_after=ece(after, labels[ev]),
        brier_before=brier(before, labels[ev]),
        brier_after=brier(after, labels[ev]),
        nll_before=nll(logits[ev], labels[ev], 1.0),
        nll_after=nll(logits[ev], labels[ev], t),
    )


def read_labels(path: Path) -> tuple[list[str], IntArray]:
    """Model inputs and labels of ``headlines.csv``; rows with an empty label (skipped) are ignored."""
    texts: list[str] = []
    labels: list[int] = []
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("label", "").strip() in ("0", "1", "2"):
                texts.append(model_text(row["title"], row.get("summary", "")))
                labels.append(int(row["label"]))
    return texts, np.asarray(labels, dtype=np.int64)


def run(
    labels_path: Path,
    classifier: OnnxSentimentClassifier,
    card_path: Path,
    *,
    strict: bool = False,
    seed: int = 0,
    batch_size: int = 32,
) -> tuple[int, CalibrationReport]:
    """Calibrate, write the card (unless ``strict`` fails). Returns ``(exit_code, report)``."""
    texts, labels = read_labels(labels_path)
    logits = (
        np.concatenate([classifier.choice_logits(texts[i : i + batch_size]) for i in range(0, len(texts), batch_size)])
        if texts
        else np.zeros((0, 3))
    )
    report = calibrate(logits, labels, seed=seed)
    if len(labels) < MIN_LABELS:
        print(f"warning: {len(labels)} labelled headlines, the spec threshold is defined on >= {MIN_LABELS}")
    if strict and report.ece_after > ECE_MAX:
        print(f"FAILED: ECE after calibration {report.ece_after:.4f} > {ECE_MAX}; the model card was not modified")
        return 1, report
    data = load_model_card(card_path).model_dump()
    data.update({f"calibration_{k}": v for k, v in asdict(report).items() if k != "temperature"})
    data.update(
        temperature=report.temperature,
        calibrated=True,
        calibration_n_labels=len(labels),
        calibration_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    save_model_card(card_path, ModelCard.model_validate(data))
    return 0, report


def format_report(r: CalibrationReport) -> str:
    return (
        f"T = {r.temperature:.4f}  (fit on {r.n_fit}, evaluated on {r.n_eval})\n"
        f"ECE   {r.ece_before:.4f} -> {r.ece_after:.4f}\n"
        f"Brier {r.brier_before:.4f} -> {r.brier_after:.4f}\n"
        f"NLL   {r.nll_before:.4f} -> {r.nll_after:.4f}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fit the temperature of the sentiment classifier on labelled headlines")
    ap.add_argument("--labels", default="data/labels/headlines.csv")
    ap.add_argument("--config", default=None)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--strict", action="store_true", help=f"exit 1 if ECE after calibration > {ECE_MAX}")
    args = ap.parse_args(argv)
    cfg = resolve_config(args.config).sentiment.classifier
    classifier = try_load_classifier(cfg.model_dir, cfg.model_card, max_seq_len=cfg.max_seq_len)
    if classifier is None:
        print(f"No classifier available ({cfg.model_dir}); run python -m modules.sentiment.export_model first")
        return 1
    code, report = run(Path(args.labels), classifier, Path(cfg.model_card), strict=args.strict, seed=args.seed)
    print(format_report(report))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
