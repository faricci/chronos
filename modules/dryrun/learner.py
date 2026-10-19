"""NightlyLearner: the nightly learning job of the dry run (spec sez. 5.1.6, 5.3 punto 11, 6.3 D11).

Runs once a day (00:15 UTC, in a low-priority child process) or by hand::

    python -m modules.dryrun.learner --once [--date YYYY-MM-DD] [--refit] [--force]

What it does, step by step (ids of the spec table):

* L0 parity live/training. Audit of the REST revisions of the previous UTC day (``rest_revisions``, read only: the
  store is never modified), state parity (the FLAT states logged by the engine against the batch path
  ``flat_matrix``) and the PSI of each feature against the last 90 days of history.
* L4 cost model. Maker fill rate of the dry ledger, REST latency p75, median spread per product and hour. Only
  ``latency_ms`` can change ``fill_model.json`` (the file stays loadable by ``FillModelParams``); the rest goes to
  ``cost_table.json``. Never touches a label (decision O1.3).
* L3 + L1 refit and calibration (day ``refit_day_of_month``, or ``--refit``): the OFFLINE pipeline is reused, not
  duplicated (``training.build_dataset`` -> ``training.baseline_lgbm`` -> ``training.tune_policy`` ->
  ``training.evaluate``). ``baseline_lgbm`` fits the model and the ``OOF_PLATT_V1`` calibration together; the learner
  checks the edge calibration against ``min_n_eff_calibration`` and reports the live labels (calibration is MEASURED
  on them, never fitted).
* L2 policy thresholds: ``tune_policy`` on the most recent validation, then the version is registered and proposed as
  the ``challenger`` alias (the book that shadows it).
* L5 promotion: the learner only CALLS ``registry.decide_promotion`` and reports the verdict. It never promotes.

Invariants (each one has a test):

* the CHAMPION is never changed: the registry is wrapped so that only the ``challenger`` alias can be moved;
* only past data: live labels count only if ``label_window_end(t) <= cutoff`` (the UTC midnight of the run date), and
  the history is refused (``skipped: history_beyond_cutoff``) when the store holds bars past the cutoff plus one day;
* ``k_tp``, ``k_sl`` and ``H`` are never touched: the label_config file is read, never written;
* one failing step is recorded and the others go on; a failed model step leaves everything as it was;
* idempotent and restartable: ``data/reports/learner/YYYY-MM-DD.json`` is rewritten after every step. A rerun of the
  same date skips the steps that ended ``ok`` or ``skipped`` and retries the ``failed`` ones, resuming inside L3/L2
  from the ``progress`` block (dataset, model, tuned, evaluated). ``--force`` redoes everything.

Limits that are written in the output instead of hidden: ``live_weight`` (2.0 in the spec) is NOT applied to the
training because ``training.build_dataset`` has no live-label input (``live_weight_applied: false``); the spread, REST
latency and fee observations of L4 need a source that nobody persists yet (``CostSource``): without one the step
reports what it could measure and nothing else.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sqlite3
import statistics
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Protocol

import numpy as np
import pandas as pd
import structlog

from modules.config import LearnerConfig
from modules.dryrun.fill_model import FillModelParams
from modules.dryrun.registry import (
    CHALLENGER,
    CHAMPION,
    BacktestEvidence,
    PromotionEvidence,
    PromotionRule,
    RegistryError,
    UnknownAlias,
    VersionExists,
    artifacts_from_dir,
    decide_promotion,
)
from modules.features.schemas import DecisionState
from modules.features.serializer import FLAT_COLUMNS, flatten_state
from modules.market.backfill import BarStore, CandleSource, candles_to_frame
from modules.market.bars import rest_revisions

__all__ = [
    "CostSample",
    "CostSource",
    "HoldoutBacktester",
    "LearnerPaths",
    "LedgerCostSource",
    "NightlyLearner",
    "ReportShadowEvidence",
    "TrainingPipeline",
    "feature_parity",
    "population_stability_index",
    "spawn_nightly",
]

log = structlog.get_logger(__name__)

SCHEMA: Final = "chronos.learner.v1"
STEPS: Final = ("L0", "L4", "L3", "L1", "L2", "L5")
MINUTE: Final = timedelta(minutes=1)
HISTORY_SLACK: Final = timedelta(days=1)
PSI_HISTORY_DAYS: Final = 90
PSI_BINS: Final = 10
PSI_MIN_SAMPLES: Final = 100  # implementation choice (10 per bin): the spec gives the threshold, not the sample size
PSI_FLOOR: Final = 1e-4
PSI_ALERT: Final = 0.25
REST_CHUNK_MIN: Final = 300
_DONE_PREFIXES: Final = ("ok", "skipped")
Registry = Any  # FilesystemModelRegistry-like: register / get / promote (see _ChallengerOnly)


class Skip(Exception):
    """Raised inside a step: the step has nothing to do or too little to do it; becomes ``skipped: <reason>``."""

    def __init__(self, reason: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = dict(details or {})


@dataclass
class StepResult:
    details: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json(path: Path, doc: Mapping[str, Any]) -> None:
    """Atomic: a temporary file next to the target, then ``os.replace`` (readers never see half a file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=True, default=str, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _clean(obj: Any) -> Any:
    """JSON-safe copy: NaN/inf become null, numpy scalars become Python numbers."""
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_clean(v) for v in obj]
    if isinstance(obj, np.generic):
        obj = obj.item()
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


# ------------------------------------------------------------------------------------------ pure math


def population_stability_index(live: np.ndarray, hist: np.ndarray, bins: int = PSI_BINS) -> float | None:
    """PSI of ``live`` against ``hist`` on the quantile bins of ``hist``. ``None``: too few finite samples or a
    constant history (no bins)."""
    a, b = live[np.isfinite(live)], hist[np.isfinite(hist)]
    if a.size < PSI_MIN_SAMPLES or b.size < PSI_MIN_SAMPLES:
        return None
    edges = np.unique(np.quantile(b, np.linspace(0.0, 1.0, bins + 1)))
    if edges.size < 3:
        return None
    edges[0], edges[-1] = -np.inf, np.inf
    p = np.maximum(np.histogram(b, edges)[0] / b.size, PSI_FLOOR)
    q = np.maximum(np.histogram(a, edges)[0] / a.size, PSI_FLOOR)
    return float(np.sum((q - p) * np.log(q / p)))


def feature_parity(live: np.ndarray, batch: np.ndarray) -> dict[str, Any]:
    """Row-wise comparison of the live FLAT feature vectors with the ones rebuilt from the stored bars. Both are in
    ``FLAT_COLUMNS`` order; NaN equals NaN; the fields are rounded integers or 2-decimal numbers: equality is exact."""
    if live.shape != batch.shape:
        raise ValueError("live and batch matrices differ in shape")
    if live.shape[0] == 0:
        return {"rows": 0, "match": None, "worst_feature": None, "mismatch_by_feature": {}}
    differ = ~((live == batch) | (np.isnan(live) & np.isnan(batch)))
    per_feature = differ.sum(axis=0)
    worst = int(per_feature.argmax())
    return {
        "rows": int(live.shape[0]),
        "match": float(1.0 - differ.any(axis=1).mean()),
        "worst_feature": FLAT_COLUMNS[worst] if per_feature[worst] > 0 else None,
        "mismatch_by_feature": {FLAT_COLUMNS[j]: int(n) for j, n in enumerate(per_feature) if n > 0},
    }


# --------------------------------------------------------------------------------------- collaborators


@dataclass(frozen=True)
class CostSample:
    """What one UTC day shows about the costs. Every field may be empty: the source decides what it can observe."""

    rtt_ms: tuple[float, ...] = ()
    spread_bps: Mapping[str, Mapping[int, tuple[float, ...]]] = field(default_factory=dict)  # product -> hour -> bps
    maker_entries: int = 0  # maker entry orders that reached a terminal state
    maker_filled: int = 0  # ... and got at least one fill
    fees: Mapping[str, Any] | None = None  # {"maker_bps", "taker_bps", "source"}


class CostSource(Protocol):
    def sample(self, day: date) -> CostSample: ...


class LedgerCostSource:
    """Maker fill rate from the dry ledger (``orders`` and ``fills``, read only). Spread, REST latency and fees have no
    persisted source yet: they stay empty (see the module docstring)."""

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)

    def sample(self, day: date) -> CostSample:
        if not self._path.exists():
            return CostSample()
        lo = datetime(day.year, day.month, day.day, tzinfo=UTC)
        hi = lo + timedelta(days=1)
        con = sqlite3.connect(f"file:{self._path.as_posix()}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT o.client_order_id, o.payload, o.status, o.created_at FROM orders o "
                "WHERE o.leg = 'entry' AND o.side = 'BUY'"
            ).fetchall()
            filled = {r[0] for r in con.execute("SELECT DISTINCT client_order_id FROM fills")}
        except sqlite3.Error:
            return CostSample()
        finally:
            con.close()
        entries = done = 0
        terminal = {"FILLED", "CANCELLED", "EXPIRED", "REJECTED", "FAILED"}
        for cid, payload, status, created in rows:
            try:
                at = datetime.fromisoformat(str(created)).astimezone(UTC)
                maker = bool(json.loads(payload or "{}").get("post_only"))
            except (ValueError, TypeError):
                continue
            if lo <= at < hi and maker and status in terminal:
                entries += 1
                done += cid in filled
        return CostSample(maker_entries=entries, maker_filled=done)


class Pipeline(Protocol):
    """The offline pipeline as the learner needs it. Each call returns the directory it produced."""

    def build_dataset(self) -> Path: ...
    def train(self, dataset: Path) -> Path: ...
    def tune(self, model: Path, dataset: Path) -> None: ...
    def evaluate(self, model: Path, dataset: Path) -> dict[str, Any]: ...


class TrainingPipeline:
    """``training.*.main`` called in this process (the whole learner already runs in a low-priority child). A
    non-zero exit of build/train/tune is a failure; ``evaluate`` exits 1 for a NO-GO verdict, so its success is the
    ``evaluation.json`` it writes."""

    def __init__(self, train_config: Path) -> None:
        from training.config import load_train_config

        self._config = Path(train_config)
        self._cfg = load_train_config(self._config)

    def _new_dir(self, root: Path, run: Callable[[], int], what: str) -> Path:
        before = {p.name for p in root.iterdir()} if root.is_dir() else set()
        code = _call_main(run)
        if code != 0:
            raise RuntimeError(f"{what} exited with code {code}")
        fresh = sorted(p for p in root.iterdir() if p.name not in before and p.is_dir())
        if len(fresh) != 1:
            raise RuntimeError(f"{what} produced {len(fresh)} directories in {root}, expected 1")
        return fresh[0]

    def build_dataset(self) -> Path:
        from training.build_dataset import main

        return self._new_dir(
            Path(self._cfg.datasets_dir), lambda: main(["--config", str(self._config), "--no-jsonl"]), "build_dataset"
        )

    def train(self, dataset: Path) -> Path:
        from training.baseline_lgbm import main

        return self._new_dir(
            Path(self._cfg.models_dir),
            lambda: main(["--config", str(self._config), "--dataset", str(dataset)]),
            "baseline_lgbm",
        )

    def tune(self, model: Path, dataset: Path) -> None:
        from training.tune_policy import main

        argv = ["--config", str(self._config), "--model", str(model), "--dataset", str(dataset)]
        code = _call_main(lambda: main(argv))
        if code != 0:
            raise RuntimeError(f"tune_policy exited with code {code}")

    def evaluate(self, model: Path, dataset: Path) -> dict[str, Any]:
        from training.evaluate import main

        stale = (model / "evaluation.json").stat().st_mtime if (model / "evaluation.json").exists() else None
        argv = ["--config", str(self._config), "--model", str(model), "--dataset", str(dataset)]
        code = _call_main(lambda: main(argv))
        path = model / "evaluation.json"
        if not path.exists() or (stale is not None and path.stat().st_mtime == stale):
            raise RuntimeError(f"evaluate exited with code {code} and wrote no evaluation.json")
        gates: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return {"exit_code": code, "status": gates.get("GE", {}).get("status"), "gates": _gate_flags(gates)}


def _call_main(run: Callable[[], int]) -> int:
    try:
        return run()
    except SystemExit as exc:  # the CLIs exit(2) on a config error
        return int(exc.code) if isinstance(exc.code, int) else 1


def _gate_flags(gates: Mapping[str, Any]) -> dict[str, Any]:
    return {k: gates[k].get("passed", gates[k].get("status")) for k in ("G0", "G1", "G2", "GE") if k in gates}


class RecentBacktester(Protocol):
    def __call__(self, model_dir: Path, days: int, cutoff: datetime) -> BacktestEvidence: ...


class HoldoutBacktester:
    """Rule L5 (a): the ``final`` model and policy of a version on the most recent ``days`` days. The window is cut
    inside the holdout of the model's label_config (``holdout_start`` is fixed, so no version trained on it: the
    window is out of sample for the challenger and for the champion). A model whose policy is ``untunable`` runs as
    "no entry": zero PnL and no defined Sharpe, hence ``insufficient`` in ``decide_promotion``."""

    def __init__(self, train_config: Path, probe: Path = Path("data/probe/coinbase_probe.json")) -> None:
        self._train_config, self._probe = Path(train_config), Path(probe)

    def __call__(self, model_dir: Path, days: int, cutoff: datetime) -> BacktestEvidence:
        from modules.decision.baseline import BaselineDecider
        from modules.decision.policy import PolicyConfig
        from modules.decision.questions import build_questions
        from modules.risk.label_config import load_label_config
        from training.backtest import (
            EVAL_DECISION_EVERY_MIN,
            FlatProbCache,
            LongProbCache,
            Span,
            run_backtest,
        )
        from training.config import load_train_config
        from training.tune_policy import backtest_config, load_markets, probe_registry

        raw = json.loads((model_dir / "final" / "policy.json").read_text(encoding="utf-8"))
        if raw.get("untunable"):
            return BacktestEvidence(float(days), Decimal(0), None, 0.0)
        cfg = load_train_config(self._train_config)
        label = load_label_config(model_dir / "label_config.json")
        markets = load_markets(Path(cfg.data_root) / "bars" / "1m", cfg.products, label)
        last = min(max(pd.Timestamp(pm.bars.ts_close[-1], tz="UTC") for pm in markets.values()), pd.Timestamp(cutoff))
        start = max(label.holdout_start, last - pd.Timedelta(days=days))
        span = Span(start, last, last, -1)
        questions = build_questions(label.cfg.horizon_min, label.cfg.k_tp, label.cfg.k_sl, label.cfg.atr_tf)
        decider = BaselineDecider.load(model_dir, label=label, questions=questions, subset="final")
        result = asyncio.run(
            run_backtest(
                markets, decider, PolicyConfig.model_validate(raw), label, span,
                backtest_config(cfg, cfg.app(), EVAL_DECISION_EVERY_MIN),
                probe_registry(self._probe, cfg.products), FlatProbCache(Path(cfg.data_root) / "backtest_cache"),
                LongProbCache(),
            )
        )  # fmt: skip
        m = result.metrics
        sharpe = m.get("sharpe")
        return BacktestEvidence(
            (last - start).total_seconds() / 86400.0, Decimal(str(m.get("pnl_eur", 0.0))),
            None if sharpe is None else float(sharpe), float(m.get("max_dd_frac", 0.0)),
        )  # fmt: skip


@dataclass(frozen=True)
class ShadowRecord:
    days: float
    operational_anomalies: int
    parity_anomalies: int


class ShadowEvidence(Protocol):
    def __call__(self, version: str, cutoff: datetime) -> ShadowRecord: ...


class ReportShadowEvidence:
    """L5 (b) from the files the dry run already writes: the days with decisions of the ``challenger`` book taken by
    ``version`` (``policy.policy_version``), and the ``anomalies`` block of the daily report of those days."""

    def __init__(self, decisions_dir: Path, reports_dir: Path, book: str = CHALLENGER) -> None:
        self._decisions, self._reports, self._book = Path(decisions_dir), Path(reports_dir), book

    def __call__(self, version: str, cutoff: datetime) -> ShadowRecord:
        days: list[date] = []
        for path in sorted(self._decisions.glob("*.jsonl")) if self._decisions.is_dir() else []:
            try:
                day = date.fromisoformat(path.stem)
            except ValueError:
                continue
            if datetime(day.year, day.month, day.day, tzinfo=UTC) >= cutoff:
                continue
            if _has_decision(path, self._book, version):
                days.append(day)
        operational = parity = 0
        for day in days:
            report = self._reports / f"{day.isoformat()}.json"
            try:
                block = json.loads(report.read_text(encoding="utf-8")).get("anomalies", {})
            except (OSError, ValueError):
                continue
            operational += int(block.get("operational", 0))
            parity += int(block.get("parity", 0))
        return ShadowRecord(float(len(days)), operational, parity)


def _has_decision(path: Path, book: str, version: str) -> bool:
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if book in line and version in line:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("book") == book and (rec.get("policy") or {}).get("policy_version") == version:
                    return True
    return False


class _ChallengerOnly:
    """The registry as the learner sees it. ``promote`` accepts the ``challenger`` alias only and ``rollback`` does not
    exist: the learner CANNOT move the champion, whatever a bug does (promotion is a local act, rule L5)."""

    def __init__(self, inner: Registry) -> None:
        self._inner = inner

    def register(self, artifacts: Mapping[str, Path], manifest: Mapping[str, Any]) -> str:
        return str(self._inner.register(artifacts, manifest))

    def get(self, alias: str) -> Any:
        return self._inner.get(alias)

    def promote(self, version: str, alias: str = CHALLENGER) -> None:
        if alias != CHALLENGER:
            raise RegistryError(f"the nightly learner may only move the {CHALLENGER!r} alias, not {alias!r}")
        self._inner.promote(version, alias)


@dataclass(frozen=True)
class LearnerPaths:
    reports_dir: Path  # data/reports/learner
    decisions_dir: Path  # data/decisions
    live_labels_dir: Path  # data/live_labels
    bars_root: Path  # data/bars/1m
    fill_model_path: Path  # models/registry/fill_model.json
    label_config: Path  # data/datasets/label_config.json (read only)
    work_dir: Path  # scratch for nothing the spec needs to keep


MarketsLoader = Callable[[], Mapping[str, Any]]  # product -> training.backtest.ProductMarket


class NightlyLearner:
    def __init__(
        self,
        *,
        cfg: LearnerConfig,
        paths: LearnerPaths,
        registry: Registry,
        products: Sequence[str],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        candle_source: CandleSource | None = None,
        cost_source: CostSource | None = None,
        pipeline: Pipeline | None = None,
        backtester: RecentBacktester | None = None,
        shadow: ShadowEvidence | None = None,
        markets: MarketsLoader | None = None,
        label: Any | None = None,
        psi_alert: float = PSI_ALERT,
    ) -> None:
        self._cfg, self._paths, self._products = cfg, paths, tuple(products)
        self._registry = _ChallengerOnly(registry)
        self._clock = clock
        self._candles, self._costs = candle_source, cost_source
        self._pipeline, self._backtester, self._shadow = pipeline, backtester, shadow
        self._markets_loader, self._label = markets, label
        self._psi_alert = psi_alert
        self._markets_cache: Mapping[str, Any] | None = None
        self._doc: dict[str, Any] = {}
        self._cutoff = datetime.now(UTC)

    # --------------------------------------------------------------------------------------------- run

    def report_path(self, day: date) -> Path:
        return self._paths.reports_dir / f"{day.isoformat()}.json"

    def run(self, day: date | None = None, *, refit: bool | None = None, force: bool = False) -> dict[str, Any]:
        """One nightly cycle for the UTC date ``day`` (default: today). Returns the document written to
        ``data/reports/learner/<day>.json``. Never raises for a step: the failure is in the document."""
        started = self._clock()
        day = day or started.astimezone(UTC).date()
        self._cutoff = datetime(day.year, day.month, day.day, tzinfo=UTC)
        do_refit = (day.day == self._cfg.refit_day_of_month) if refit is None else refit
        previous = {} if force else self._load(day)
        self._doc = {
            "v": SCHEMA, "date": day.isoformat(), "audited_day": (day - timedelta(days=1)).isoformat(),
            "cutoff": _iso(self._cutoff), "refit_day": do_refit,
            "started_at": previous.get("started_at", _iso(started)), "updated_at": _iso(started),
            "champion_before": previous.get("champion_before", self._alias_version(CHAMPION)),
            "champion_after": None, "challenger_after": None,
            "progress": previous.get("progress", {}), "steps": previous.get("steps", {}),
        }  # fmt: skip
        self._doc["champion_before"] = self._doc["champion_before"] or self._alias_version(CHAMPION)
        runners: dict[str, Callable[[date], StepResult]] = {
            "L0": self._l0, "L4": self._l4, "L3": self._l3, "L1": self._l1, "L2": self._l2, "L5": self._l5,
        }  # fmt: skip
        for step in STEPS:
            old = self._doc["steps"].get(step)
            if old is not None and str(old.get("outcome", "")).startswith(_DONE_PREFIXES):
                continue
            self._run_step(step, runners[step], day, do_refit)
        self._doc["champion_after"] = self._alias_version(CHAMPION)
        self._doc["challenger_after"] = self._alias_version(CHALLENGER)
        self._doc["finished_at"] = _iso(self._clock())
        self._save(day)
        outcomes = {s: r["outcome"] for s, r in self._doc["steps"].items()}
        log.info("learner_done", date=day.isoformat(), outcomes=outcomes)
        return dict(self._doc)

    def _run_step(self, step: str, run: Callable[[date], StepResult], day: date, do_refit: bool) -> None:
        started = self._clock()
        record: dict[str, Any] = {
            "step": step, "started_at": _iso(started), "message": None, "artifacts": [], "details": {},
        }  # fmt: skip
        try:
            if step in ("L3", "L1", "L2", "L5"):
                self._gate_model_step(step, do_refit)
            result = run(day)
            record["outcome"], record["artifacts"], record["details"] = "ok", result.artifacts, result.details
        except Skip as skip:
            record["outcome"], record["details"] = f"skipped: {skip.reason}", skip.details
        except (Exception, SystemExit) as exc:  # a step never takes the others down
            record["outcome"], record["message"] = "failed", f"{type(exc).__name__}: {exc}"
            log.error("learner_step_failed", step=step, error=record["message"])
        record["finished_at"] = _iso(self._clock())
        self._doc["steps"][step] = _clean(record)
        self._doc["updated_at"] = record["finished_at"]
        self._save(day)

    def _gate_model_step(self, step: str, do_refit: bool) -> None:
        if not do_refit:
            raise Skip("not_refit_day")
        order = ("L3", "L1", "L2", "L5")
        for before in order[: order.index(step)]:
            outcome = str(self._doc["steps"].get(before, {}).get("outcome", ""))
            if outcome == "failed":
                raise Skip("upstream_failed", {"step": before})
            if outcome.startswith("skipped"):
                raise Skip("upstream_skipped", {"step": before, "outcome": outcome})

    def _save(self, day: date) -> None:
        _write_json(self.report_path(day), self._doc)

    def _load(self, day: date) -> dict[str, Any]:
        try:
            doc = json.loads(self.report_path(day).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return doc if isinstance(doc, dict) and doc.get("v") == SCHEMA else {}

    def _alias_version(self, alias: str) -> str | None:
        try:
            return str(self._registry.get(alias).version)
        except (UnknownAlias, RegistryError):
            return None

    @property
    def _progress(self) -> dict[str, Any]:
        progress: dict[str, Any] = self._doc["progress"]
        return progress

    # ----------------------------------------------------------------------------------------------- L0

    def _l0(self, day: date) -> StepResult:
        audited = day - timedelta(days=1)
        result = StepResult()
        errors: list[str] = []
        parts: dict[str, Callable[[date], dict[str, Any]]] = {
            "rest_revisions": self._rest_audit, "state_parity": self._state_parity,
        }  # fmt: skip
        for name, part in parts.items():
            try:
                result.details[name] = part(audited)
            except Skip as skip:
                result.details[name] = {"skipped": skip.reason}
            except Exception as exc:
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
                result.details[name] = {"failed": str(exc)}
        result.details["audited_day"] = audited.isoformat()
        if errors:
            raise RuntimeError("; ".join(errors))
        if all("skipped" in v for v in result.details.values() if isinstance(v, dict)):
            raise Skip("insufficient_samples", result.details)
        return result

    def _rest_audit(self, audited: date) -> dict[str, Any]:
        if self._candles is None:
            raise Skip("no_candle_source")
        lo = datetime(audited.year, audited.month, audited.day, tzinfo=UTC)
        hi = lo + timedelta(days=1)
        store = BarStore(self._paths.bars_root)
        out: dict[str, Any] = {}
        for product in self._products:
            months = [m for m in store.months(product) if (lo.year, lo.month) <= m <= (hi.year, hi.month)]
            if not months:
                out[product] = {"minutes": 0, "revised": 0, "ratio": 0.0, "max_close_diff_bps": 0.0}
                continue
            stored = pd.concat([store.read_month(product, *m) for m in months])
            raw: list[dict[str, Any]] = []
            cursor = lo
            while cursor < hi:
                upper = min(cursor + timedelta(minutes=REST_CHUNK_MIN), hi)
                raw += self._candles.fetch(product, int(cursor.timestamp()), int(upper.timestamp()))
                cursor = upper
            fresh = candles_to_frame(raw, int(lo.timestamp()), int(hi.timestamp()))
            stats = rest_revisions(product, stored, fresh, lo, hi)
            out[product] = {
                "minutes": stats.minutes, "revised": stats.revised, "ratio": stats.ratio,
                "max_close_diff_bps": stats.max_close_diff_bps,
            }  # fmt: skip
        return {"products": out, "rest_revision_ratio_max": max((v["ratio"] for v in out.values()), default=0.0)}

    def _markets(self) -> Mapping[str, Any]:
        if self._markets_loader is None:
            raise Skip("no_markets_loader")
        if self._markets_cache is None:
            self._markets_cache = self._markets_loader()
        return self._markets_cache

    def _state_parity(self, audited: date) -> dict[str, Any]:
        """FLAT states of ``audited`` as the engine logged them against the batch path, and PSI against 90 days."""
        from modules.features.state_matrix import flat_matrix

        if self._label is None:
            raise Skip("no_label_config")
        path = self._paths.decisions_dir / f"{audited.isoformat()}.jsonl"
        if not path.exists():
            raise Skip("no_decisions")
        live_rows: dict[str, dict[datetime, list[float]]] = defaultdict(dict)
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
                state = DecisionState.model_validate_json(rec["state"])
                t = datetime.fromisoformat(rec["ts_bar_close"].replace("Z", "+00:00")).astimezone(UTC)
            except (ValueError, KeyError, TypeError):
                continue
            if state.position.side == "FLAT":
                flat = flatten_state(state)
                live_rows[state.product][t] = [flat[c] for c in FLAT_COLUMNS]
        if not live_rows:
            raise Skip("no_decisions")
        markets = self._markets()
        live_all, batch_all, psi_by_feature = [], [], defaultdict(list)
        unmatched = 0
        for product, by_t in sorted(live_rows.items()):
            pm = markets.get(product)
            if pm is None:
                unmatched += len(by_t)
                continue
            close = pd.DatetimeIndex(pm.bars.ts_close)
            stamps = sorted(by_t)
            rows = close.get_indexer(pd.DatetimeIndex([t.replace(tzinfo=None) for t in stamps]))
            keep = rows >= 0
            unmatched += int((~keep).sum())
            rows = rows[keep].astype(np.int64)
            if rows.size == 0:
                continue
            btc_rows = None if pm.btc_rows is None else pm.btc_rows[rows]
            batch_all.append(flat_matrix(pm.ind, rows, product, self._label, pm.btc_ind, btc_rows))
            live_all.append(np.array([by_t[stamps[i]] for i in np.flatnonzero(keep)], dtype=np.float64))
            lo = np.datetime64((self._cutoff - timedelta(days=PSI_HISTORY_DAYS)).replace(tzinfo=None), "ns")
            hi = np.datetime64(datetime(audited.year, audited.month, audited.day).replace(tzinfo=None), "ns")
            hist_rows = np.flatnonzero((pm.bars.ts_close >= lo) & (pm.bars.ts_close < hi) & (close.minute == 0))
            if hist_rows.size:
                hist = flat_matrix(
                    pm.ind, hist_rows, product, self._label, pm.btc_ind,
                    None if pm.btc_rows is None else pm.btc_rows[hist_rows],
                )  # fmt: skip
                for j, name in enumerate(FLAT_COLUMNS):
                    psi_by_feature[name].append((live_all[-1][:, j], hist[:, j]))
        if not live_all:
            raise Skip("no_matching_bars")
        parity = feature_parity(np.vstack(live_all), np.vstack(batch_all))
        psi: dict[str, float | None] = {
            name: population_stability_index(
                np.concatenate([a for a, _ in pairs]), np.concatenate([b for _, b in pairs])
            )
            for name, pairs in psi_by_feature.items()
        }
        finite = {k: v for k, v in psi.items() if v is not None}
        worst = max(finite, key=lambda k: finite[k]) if finite else None
        alerts = sorted(k for k, v in finite.items() if v > self._psi_alert)
        return {
            "state_parity": parity, "unmatched_rows": unmatched, "psi": psi,
            "max_feature_psi": None if worst is None else finite[worst], "worst_feature": worst,
            "drift_alerts": [{"feature": k, "psi": finite[k]} for k in alerts],
        }  # fmt: skip

    # ----------------------------------------------------------------------------------------------- L4

    def _l4(self, day: date) -> StepResult:
        if self._costs is None:
            raise Skip("insufficient_samples", {"reason": "no cost source"})
        audited = day - timedelta(days=1)
        sample = self._costs.sample(audited)
        measured: dict[str, Any] = {}
        result = StepResult()
        if sample.maker_entries > 0:
            measured["maker_fill_rate"] = {
                "rate": sample.maker_filled / sample.maker_entries, "entries": sample.maker_entries,
                "filled": sample.maker_filled,
            }  # fmt: skip
        spreads = {
            p: {str(h): statistics.median(v) for h, v in sorted(by_hour.items()) if v}
            for p, by_hour in sample.spread_bps.items()
        }
        if any(spreads.values()):
            measured["spread_median_bps_by_hour"] = spreads
        if sample.fees:
            measured["fees"] = dict(sample.fees)
        if sample.rtt_ms:
            measured["latency_ms_p75"] = float(np.percentile(np.asarray(sample.rtt_ms), 75))
        if not measured:
            raise Skip("insufficient_samples", {"audited_day": audited.isoformat()})
        table = self._paths.fill_model_path.with_name("cost_table.json")
        _write_json(table, {"v": "chronos.cost_table.v1", "audited_day": audited.isoformat(), **measured})
        result.artifacts.append(str(table))
        if "latency_ms_p75" in measured:
            current = (
                FillModelParams.from_json(self._paths.fill_model_path)
                if self._paths.fill_model_path.exists()
                else FillModelParams()
            )
            updated = current.model_copy(update={"latency_ms": measured["latency_ms_p75"]})
            _write_json(self._paths.fill_model_path, updated.model_dump())
            result.artifacts.append(str(self._paths.fill_model_path))
        result.details = {"audited_day": audited.isoformat(), "measured": sorted(measured), **measured}
        return result

    # ------------------------------------------------------------------------------------------- L3 / L1

    def _need_pipeline(self) -> Pipeline:
        if self._pipeline is None:
            raise RuntimeError("no training pipeline configured")
        return self._pipeline

    def _live_labels(self) -> dict[str, Any]:
        """Edge labels of the dry run that a refit at ``cutoff`` may look at: ``label_window_end(t) <= cutoff``."""
        from training.labeling import label_window_end
        from training.sampling import uniqueness_weights

        if self._label is None:
            raise RuntimeError("no label_config loaded")
        rows: list[dict[str, Any]] = []
        for path in sorted(self._paths.live_labels_dir.glob("*.jsonl")) if self._paths.live_labels_dir.is_dir() else []:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("question_key") == "edge" and rec.get("split") == "live":
                    rows.append(rec)
        if not rows:
            return {"rows": 0, "used": 0, "excluded_future": 0, "n_eff": 0.0, "ece": None}
        t = pd.DatetimeIndex([pd.Timestamp(r["t"]).tz_convert("UTC").tz_localize(None) for r in rows])
        end = label_window_end(t, self._label.cfg)
        cut = np.datetime64(self._cutoff.replace(tzinfo=None), "ns")
        ok = np.asarray(end <= cut)
        used = [r for r, flag in zip(rows, ok, strict=True) if flag]
        n_eff = 0.0
        by_product: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in used:
            by_product[r["product"]].append(r)
        origin = t.min()
        for items in by_product.values():
            t0, t1 = [], []
            for r in items:
                m = int((pd.Timestamp(r["t"]).tz_convert("UTC").tz_localize(None) - origin).total_seconds() // 60)
                meta = r.get("meta") or {}
                t0.append(m + int(meta.get("fill_minute") or 0))
                t1.append(m + max(int(meta.get("exit_minute") or 0), int(meta.get("fill_minute") or 0)))
            n_eff += float(uniqueness_weights(np.asarray(t0), np.asarray(t1)).sum())
        ece_value = None
        scored = [r for r in used if (r.get("predicted") or {}).get("edge") is not None]
        if scored:
            from training.baseline_lgbm import ece

            p = np.array([float(r["predicted"]["edge"]) for r in scored])
            y = np.array([1.0 if r["target"] == "true" else 0.0 for r in scored])
            ece_value = float(ece(p, y))
        return {
            "rows": len(rows), "used": len(used), "excluded_future": len(rows) - len(used), "n_eff": n_eff,
            "ece": ece_value, "n_scored": len(scored),
        }  # fmt: skip

    def _l3(self, day: date) -> StepResult:
        pipe = self._need_pipeline()
        store = BarStore(self._paths.bars_root)
        for product in self._products:
            last = store.last_ts(product)
            if last is not None and last > self._cutoff + HISTORY_SLACK:
                raise Skip("history_beyond_cutoff", {"product": product, "last_bar": _iso(last)})
        live = self._live_labels()
        dataset = self._reuse("dataset")
        if dataset is None:
            dataset = pipe.build_dataset()
            self._progress["dataset"] = str(dataset)
            self._save(day)
        model = self._reuse("model")
        if model is None:
            model = pipe.train(dataset)
            self._progress["model"] = str(model)
            self._save(day)
        return StepResult(
            {
                "dataset": str(dataset), "model": model.name, "live_labels": live,
                "live_weight": self._cfg.live_weight, "live_weight_applied": False,
                "live_weight_reason": "training.build_dataset has no live-label input",
                "cutoff": _iso(self._cutoff),
            },
            [str(model)],
        )  # fmt: skip

    def _reuse(self, key: str) -> Path | None:
        value = self._progress.get(key)
        return Path(value) if value and Path(value).exists() else None

    def _l1(self, day: date) -> StepResult:
        model = Path(self._progress["model"])
        cal = json.loads((model / "final" / "calibration.json").read_text(encoding="utf-8"))
        edge = cal.get("edge_cal", {})
        n_eff = float(edge.get("n_eff", 0.0))
        details = {
            "scheme": edge.get("scheme"), "n_eff": n_eff, "n_eff_min": self._cfg.min_n_eff_calibration,
            "edge_cal_ok": bool(edge.get("ok")), "live_labels": self._doc["steps"]["L3"]["details"]["live_labels"],
        }  # fmt: skip
        if n_eff < self._cfg.min_n_eff_calibration or not edge.get("ok"):
            raise Skip("insufficient_samples", details)
        return StepResult(details, [str(model / "final" / "calibration.json")])

    # ------------------------------------------------------------------------------------------------ L2

    def _l2(self, day: date) -> StepResult:
        pipe = self._need_pipeline()
        model, dataset = Path(self._progress["model"]), Path(self._progress["dataset"])
        if not self._progress.get("tuned"):
            pipe.tune(model, dataset)
            self._progress["tuned"] = True
            self._save(day)
        evaluation = self._progress.get("evaluation")
        if evaluation is None:
            evaluation = pipe.evaluate(model, dataset)
            self._progress["evaluation"] = evaluation
            self._save(day)
        policy = json.loads((model / "final" / "policy.json").read_text(encoding="utf-8"))
        untunable = bool(policy.get("untunable"))
        card = json.loads((model / "model_card.json").read_text(encoding="utf-8"))
        manifest = {
            "version": model.name, "kind": "baseline-lgbm", "created_by": "nightly_learner",
            "learner_date": day.isoformat(), "cutoff": _iso(self._cutoff),
            "dataset_run_id": card.get("data", {}).get("dataset_run_id"),
            "label_config_sha256": card.get("label_config", {}).get("sha256"),
            "data_window": {"anchor": card.get("data", {}).get("indicator_anchor"),
                            "holdout": card.get("data", {}).get("holdout")},
            "inputs_sha256": _inputs_hash(dataset, self._paths.label_config),
            "metrics": {"evaluation": evaluation, "final_policy": "untunable" if untunable else "tuned",
                        "edge_cal": card.get("calibration", {}).get("edge_cal")},
            "live_labels": self._doc["steps"]["L3"]["details"]["live_labels"],
        }  # fmt: skip
        try:
            version = self._registry.register(artifacts_from_dir(model), _clean(manifest))
        except VersionExists:
            version = model.name  # a previous run registered it before it could record the step
        self._registry.promote(version, CHALLENGER)
        self._progress["registered"] = version
        return StepResult(
            {"version": version, "alias": CHALLENGER, "final_policy_untunable": untunable, "evaluation": evaluation},
            [version],
        )  # fmt: skip

    # ------------------------------------------------------------------------------------------------ L5

    def _l5(self, day: date) -> StepResult:
        """Verdict of rule L5. The learner reports it and does nothing else: ``promoted`` is always false."""
        if self._backtester is None or self._shadow is None:
            raise Skip("no_evidence_source")
        challenger = self._registry.get(CHALLENGER)
        try:
            champion = self._registry.get(CHAMPION)
        except UnknownAlias:
            raise Skip("no_champion") from None
        days = PromotionRule.from_config(self._cfg).backtest_recent_days
        shadow = self._shadow(challenger.version, self._cutoff)
        evidence = PromotionEvidence(
            challenger=self._backtester(challenger.path, days, self._cutoff),
            champion=self._backtester(champion.path, days, self._cutoff),
            shadow_days=shadow.days, operational_anomalies=shadow.operational_anomalies,
            parity_anomalies=shadow.parity_anomalies,
        )  # fmt: skip
        decision = decide_promotion(evidence, PromotionRule.from_config(self._cfg))
        return StepResult(
            {
                "challenger": challenger.version, "champion": champion.version, "verdict": decision.verdict,
                "reasons": list(decision.reasons), "promoted": False,
                "note": "promotion is a local act (python -m modules.dryrun.registry promote)",
                "evidence": _clean(asdict(evidence)),
            }
        )  # fmt: skip


def _inputs_hash(dataset: Path, label_config: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    for p in (dataset / "manifest.json", label_config):
        if p.exists():
            digest.update(p.read_bytes())
    return digest.hexdigest()


# ------------------------------------------------------------------------------------------ process / CLI


async def spawn_nightly(extra_args: Iterable[str] = ()) -> int:
    """Run the learner in a child process (spec 5.3 punto 11), so a slow refit never delays the trading loop. The child
    lowers its own priority (``main``). Returns its exit code."""
    proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "modules.dryrun.learner", "--once", *extra_args)
    return await proc.wait()


def _lower_priority() -> None:
    if hasattr(os, "nice"):
        os.nice(10)
    elif sys.platform == "win32":  # pragma: no cover - platform specific
        import ctypes

        BELOW_NORMAL = 0x00004000
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        ctypes.windll.kernel32.SetPriorityClass(handle, BELOW_NORMAL)


def build_learner(config_path: str, train_config: str) -> NightlyLearner:
    from modules.config import load_config
    from modules.dryrun.registry import FilesystemModelRegistry
    from modules.market.backfill import CoinbaseCandleSource, bars_root
    from modules.risk.label_config import load_label_config

    app = load_config(config_path)
    data = Path(app.paths.data_dir)
    registry_root = Path(app.paths.models_dir) / "registry"
    label_path = Path(train_config and _label_path(train_config))
    label = load_label_config(label_path) if label_path.exists() else None

    def markets() -> Mapping[str, Any]:
        from training.tune_policy import load_markets

        assert label is not None
        return load_markets(bars_root(app), app.market.products, label)

    paths = LearnerPaths(
        reports_dir=data / "reports" / "learner", decisions_dir=data / "decisions",
        live_labels_dir=data / "live_labels", bars_root=bars_root(app),
        fill_model_path=Path(app.dry_run.fill_model.path), label_config=label_path,
        work_dir=data / "reports" / "learner" / "work",
    )  # fmt: skip
    return NightlyLearner(
        cfg=app.dry_run.learner, paths=paths, registry=FilesystemModelRegistry(registry_root),
        products=app.market.products, candle_source=CoinbaseCandleSource(),
        cost_source=LedgerCostSource(app.dry_run.db_path), pipeline=TrainingPipeline(Path(train_config)),
        backtester=HoldoutBacktester(Path(train_config)),
        shadow=ReportShadowEvidence(data / "decisions", data / "reports" / "dry"),
        markets=markets, label=label, psi_alert=app.risk.kill_switch.psi_threshold,
    )  # fmt: skip


def _label_path(train_config: str) -> str:
    from training.config import load_train_config

    return load_train_config(train_config).label_config


def _seconds_until(now: datetime, run_at: str) -> float:
    hh, mm = (int(x) for x in run_at.split(":"))
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def main(argv: Sequence[str] | None = None, *, sleep: Callable[[float], None] = time.sleep) -> int:
    ap = argparse.ArgumentParser(prog="python -m modules.dryrun.learner", description="Nightly learner (spec 5.1.6)")
    ap.add_argument("--once", action="store_true", help="run one cycle now and exit (default: wait for run_at_utc)")
    ap.add_argument("--date", default=None, help="UTC date of the run, YYYY-MM-DD (default: today)")
    ap.add_argument("--refit", action="store_true", help="run L3/L1/L2/L5 whatever the day of the month")
    ap.add_argument("--force", action="store_true", help="ignore the steps already done today")
    ap.add_argument("--config", default="config/config.yml")
    ap.add_argument("--train-config", default="config/train.yml")
    args = ap.parse_args(argv)
    from modules.config import EXIT_CONFIG_ERROR, ConfigError

    try:
        learner = build_learner(args.config, args.train_config)
        run_at = learner._cfg.run_at_utc
        day = date.fromisoformat(args.date) if args.date else None
    except (ConfigError, ValueError, OSError) as exc:
        print(f"learner configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    _lower_priority()
    while True:
        if not args.once:
            sleep(_seconds_until(datetime.now(UTC), run_at))
        doc = learner.run(day, refit=True if args.refit else None, force=args.force)
        failed = [s for s, r in doc["steps"].items() if r["outcome"] == "failed"]
        print(json.dumps({"date": doc["date"], "outcomes": {s: r["outcome"] for s, r in doc["steps"].items()}}))
        if args.once:
            return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
