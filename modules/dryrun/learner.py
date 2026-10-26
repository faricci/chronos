"""NightlyLearner: the nightly learning job of the dry run (spec sez. 5.1.6, 5.3 punto 11, 6.3 D11).

Runs once a day (00:15 UTC, in a low-priority child process) or by hand::

    python -m modules.dryrun.learner --once [--date YYYY-MM-DD] [--refit] [--force]

What it does, step by step (ids of the spec table):

* L0 parity live/training. Audit of the REST revisions of the previous UTC day (``rest_revisions``, read only: the
  store is never modified), state parity (the FLAT states logged by the engine against the batch path
  ``flat_matrix``), the BTC context of the alt states (``btc_context``: the states whose BTC bar was not of the same
  instant, by class, with the shift of the model answers and the flips of a BUY they cause; zero flips with the
  basis ``policy_untunable`` when the policy of the version is explicitly untunable and the day shows no BUY), the
  drift of the inputs against the training reference (``feature_drift``: PSI-v3, ``modules/dryrun/drift.py``,
  calibrated by SIM-P1f-bis, PSI-v2 as a fallback; ``unmeasured`` without a calibration, ``not_testable`` when PSI-v3
  was rejected; always with the diagnostics ``support``, PSI percentile of the features the test does not cover and
  PSI of ``p_edge``) and, as a DIAGNOSTIC only (``state_parity.psi_v1``), the old daily PSI of each feature against
  the last 90 days of history. Conflicts P1f and P1f-bis, docs/decisions/2026-10-25-p1f-psi-v2.md and
  2026-10-26-p1f-bis-psi-v3.md.
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
* L5 promotion (refit day only): the learner CALLS ``registry.decide_promotion`` and reports the verdict. It never
  promotes. It judges the ``challenger`` that was in charge BEFORE the night (``challenger_before``): L2 moves the
  alias onto the version it has just trained, which has no shadow days, so judging the alias after L2 could never
  promote anything. L5 therefore does not depend on the outcome of L3/L1/L2. Both models are backtested on ONE common
  window (the latest ``backtest_recent_days``, not before the holdout of either), and the OOS proof (V1), the gates
  of ``evaluation.json`` and the paired OOS comparison come from the registered versions themselves.

Invariants (each one has a test):

* the CHAMPION is never changed: the registry is wrapped so that only the ``challenger`` alias can be moved;
* only past data: live labels count only if ``label_window_end(t) <= cutoff`` (the UTC midnight of the run date), and
  the history is refused (``skipped: history_beyond_cutoff``) when the store holds bars past the cutoff plus one day;
* ``k_tp``, ``k_sl`` and ``H`` are never touched: the label_config file is read, never written;
* one failing step is recorded and the others go on; a failed model step leaves everything as it was;
* idempotent and restartable: ``data/reports/learner/YYYY-MM-DD.json`` is rewritten after every step. A rerun of the
  same date skips the steps that ended ``ok`` or ``skipped`` and retries the ``failed`` ones, resuming inside L3/L2
  from the ``progress`` block (dataset, model, tuned, evaluated). ``--force`` redoes everything.

Live labels (L3): when ``live_labels_dir`` exists the dataset is built with them (``training.build_dataset`` option
``--live-labels``): the folder, ``dry_run.learner.live_weight`` and the cutoff go to the builder, which weighs the live
rows (uniqueness weight x ``live_weight``, before the normalisation of the training) and lets them train only the sets
the purge allows. What really entered is read back from the dataset manifest (``details.live_dataset``) and
``live_weight_applied`` is true only when at least one live row is in the dataset: with the holdout frozen in the
label_config a live row of the holdout period never trains anything, exactly like a historical row of that period.

Limits that are written in the output instead of hidden: the spread, REST latency and fee observations of L4 need a
source that nobody persists yet (``CostSource``): without one the step reports what it could measure and nothing else.
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
from typing import TYPE_CHECKING, Any, Final, Protocol

import numpy as np
import pandas as pd
import structlog

from modules.config import GateEconConfig, GateOpsConfig, LearnerConfig, RiskConfig
from modules.dryrun import drift
from modules.dryrun.fill_model import FillModelParams
from modules.dryrun.registry import (
    CHALLENGER,
    CHAMPION,
    GE_GO,
    GE_INCONCLUSIVE,
    GE_NO_GO,
    REQUIRED_GATES,
    BacktestEvidence,
    GateSummary,
    GeStatus,
    OosEvidence,
    PromotionEvidence,
    PromotionRule,
    RegistryError,
    UnknownAlias,
    VersionExists,
    artifacts_from_dir,
    decide_promotion,
)
from modules.dryrun.revision_impact import (
    ImpactSkip,
    day_impact,
    flat_pair_shift,
    policy_kind,
    revisions_frame,
    write_revisions,
)
from modules.features.schemas import DecisionState
from modules.features.serializer import FLAT_COLUMNS, flatten_state
from modules.market.backfill import BarStore, CandleSource, candles_to_frame
from modules.market.bars import classify_revisions, rest_revisions

if TYPE_CHECKING:
    from training.build_dataset import LiveInput

__all__ = [
    "CostSample",
    "CostSource",
    "HoldoutBacktester",
    "LearnerPaths",
    "LedgerCostSource",
    "NightlyLearner",
    "PairedOosComparator",
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
PSI_V1_RULE: Final = "PSI_V1_DAILY_90D"  # the old daily PSI against 90 days: a diagnostic since conflict P1f
PSI_V1_ABOVE: Final = 0.10
BTC_COLUMNS: Final = tuple(j for j, c in enumerate(FLAT_COLUMNS) if c.startswith("btc_"))
BTC_CLASSES: Final = ("live_missing", "batch_missing", "value_mismatch", "flag_inconsistent")
MODEL_BOOKS: Final = ("champion", "challenger")  # the books where the MODEL decides (the others are benchmarks)
_SHIFT_RANK: Final = {"measured": 0, "policy_untunable": 1, "decider_without_batch": 2, "no_deciders": 3}
REST_CHUNK_MIN: Final = 300
OOS_EMBARGO: Final = timedelta(days=2)  # the embargo of ``training.splits.purged_walk_forward`` (V1 of rule L5)
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


def classify_btc_row(flag: str | None, live: np.ndarray, batch: np.ndarray) -> tuple[str | None, bool]:
    """``(class, misaligned)`` of one alt FLAT state from its ``btc_*`` values: ``live`` as the engine logged them,
    ``batch`` as the batch path rebuilds them (always aligned, A8). ``flag`` is the ``btc_context`` of the record
    (``aligned`` / ``stale``, ``None`` for a record written before P1f).

    * ``live_missing`` / ``batch_missing``: a ``btc_*`` token exists on one side only (``misaligned``: the model
      answered on a state without it, or with another one);
    * ``value_mismatch``: both finite and different, and ``flag_inconsistent``: the flag contradicts the values
      (``stale`` with finite values, ``aligned`` with none where the batch has them). Both are DEFECTS, never
      ``misaligned``: the gate fails on them. A token is compared one by one (a young BTC history has no
      ``rv_ratio_1d_30d`` on either side)."""
    live_ok, batch_ok = np.isfinite(live), np.isfinite(batch)
    live_missing = bool((~live_ok & batch_ok).any())  # a token the batch has and the live state lacks
    batch_missing = bool((live_ok & ~batch_ok).any())
    value_mismatch = bool((live_ok & batch_ok & (live != batch)).any())
    live_any = bool(live_ok.any())
    if flag == "stale":  # the engine had no BTC bar of the instant: every btc token of the state is missing
        if live_any:
            return "flag_inconsistent", False
        return ("live_missing" if live_missing else "batch_missing"), True
    if flag == "aligned" and live_missing and not live_any:
        return "flag_inconsistent", False
    if live_missing:
        return "live_missing", True
    if batch_missing:
        return "batch_missing", True
    if value_mismatch:
        return "value_mismatch", False
    return None, False


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
    """The offline pipeline as the learner needs it. Each call returns the directory it produced. ``live``: the live
    labels the dataset is built with (``training.build_dataset.LiveInput``), None for the historical dataset only."""

    def build_dataset(self, live: LiveInput | None = None) -> Path: ...
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

    def build_dataset(self, live: LiveInput | None = None) -> Path:
        from training.build_dataset import main

        argv = ["--config", str(self._config), "--no-jsonl"]
        if live is not None:
            argv += ["--live-labels", str(live.labels_dir), "--live-weight", repr(live.weight)]
            if live.cutoff is not None:
                argv += ["--live-cutoff", _iso(live.cutoff)]
        return self._new_dir(Path(self._cfg.datasets_dir), lambda: main(argv), "build_dataset")

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
    """The verdict of every gate of ``evaluation.json`` for the manifest. G3 is ``None`` when the candidate is not an
    LLM (``applicable: false``), G4 is a boolean; GE is its status string."""
    return {
        k: gates[k].get("passed", gates[k].get("status")) for k in ("G0", "G1", "G2", "G3", "G4", "GE") if k in gates
    }


def gate_summary(model_dir: Path) -> GateSummary:
    """The gates of a registered version, from its ``evaluation.json`` (rule L5, P1). A missing or unreadable file is
    ``ge=None`` with every required gate ``None`` (= missing): L5 then says ``insufficient``. G3 is required only when
    the evaluation says it applies (an LLM backend)."""
    try:
        gates = json.loads((model_dir / "evaluation.json").read_text(encoding="utf-8"))
        if not isinstance(gates, dict):
            raise ValueError("evaluation.json is not an object")
    except (OSError, ValueError):
        return GateSummary(None, dict.fromkeys(REQUIRED_GATES))

    def flag(name: str) -> bool | None:
        value = (gates.get(name) or {}).get("passed")
        return value if isinstance(value, bool) else None

    passed = {k: flag(k) for k in REQUIRED_GATES}
    if (gates.get("G3") or {}).get("applicable") is True:
        passed["G3"] = flag("G3")
    status = (gates.get("GE") or {}).get("status")
    ge: GeStatus | None = (
        GE_GO if status == GE_GO else GE_INCONCLUSIVE if status == GE_INCONCLUSIVE
        else GE_NO_GO if status == GE_NO_GO else None
    )  # fmt: skip
    return GateSummary(ge, passed)


def final_train_end(model_dir: Path) -> datetime | None:
    """``data.final_train_end`` of the model card (UTC), None if the card does not say (an older version): V1 of rule
    L5 cannot be proven then."""
    try:
        card = json.loads((model_dir / "model_card.json").read_text(encoding="utf-8"))
        return datetime.fromisoformat(str(card["data"]["final_train_end"]).replace("Z", "+00:00")).astimezone(UTC)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def holdout_start(model_dir: Path) -> datetime | None:
    """Where the frozen holdout of the model's label_config starts (model card ``label_config.selection``)."""
    try:
        card = json.loads((model_dir / "model_card.json").read_text(encoding="utf-8"))
        text = str(card["label_config"]["selection"]["holdout_start"])
        t = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)
    except (OSError, ValueError, KeyError, TypeError):
        return None


class RecentBacktester(Protocol):
    def __call__(self, model_dir: Path, start: datetime, end: datetime) -> BacktestEvidence: ...


class OosComparator(Protocol):
    """The paired OOS evidence of two registered versions (P2, S3); None when the challenger has no OOS artifacts."""

    def __call__(
        self, challenger_dir: Path, champion_dir: Path, *, champion_go: bool, quantile: float
    ) -> OosEvidence | None: ...


class HoldoutBacktester:
    """Rule L5, P3: the ``final`` model and policy of a version over the window ``[start, end)`` that the learner
    computed ONCE for both models (a window per model would compare different periods). The window starts at or after
    the frozen holdout of each label_config, which no version trained on: it is out of sample for both (proved by
    ``data.final_train_end``, V1). The metrics are those of the backtest (``run_backtest``), one definition.

    A model whose policy is ``untunable`` does not trade: PnL 0, Sharpe 0.0, drawdown 0 and ``n_trades == 0`` (defined
    metrics: the old ``Sharpe None`` made L5 answer ``insufficient`` forever)."""

    def __init__(self, train_config: Path, probe: Path = Path("data/probe/coinbase_probe.json")) -> None:
        self._train_config, self._probe = Path(train_config), Path(probe)

    def __call__(self, model_dir: Path, start: datetime, end: datetime) -> BacktestEvidence:
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

        days = (end - start).total_seconds() / 86400.0
        raw = json.loads((model_dir / "final" / "policy.json").read_text(encoding="utf-8"))
        if raw.get("untunable"):
            return BacktestEvidence(start, end, days, 0, Decimal(0), 0.0, 0.0)
        cfg = load_train_config(self._train_config)
        label = load_label_config(model_dir / "label_config.json")
        markets = load_markets(Path(cfg.data_root) / "bars" / "1m", cfg.products, label)
        t0, t1 = pd.Timestamp(start), pd.Timestamp(end)
        span = Span(t0, t1, t1, -1)
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
        n = len(result.trades)
        sharpe = m.get("sharpe")
        return BacktestEvidence(
            start, end, days, n, Decimal(str(m.get("pnl_eur", 0.0))),
            (0.0 if n == 0 else None) if sharpe is None else float(sharpe), float(m.get("max_dd_frac", 0.0)),
        )  # fmt: skip


class PairedOosComparator:
    """``training.oos_compare.compare_models`` with the constants of GE (replicates and seed of E2 from
    ``training.evaluate``) and the starting capital of the backtests (``train.yml``)."""

    def __init__(self, train_config: Path) -> None:
        self._train_config = Path(train_config)

    def __call__(
        self, challenger_dir: Path, champion_dir: Path, *, champion_go: bool, quantile: float
    ) -> OosEvidence | None:
        from training.config import load_train_config
        from training.evaluate import N_BOOT, SEED
        from training.oos_compare import compare_models

        cfg = load_train_config(self._train_config)
        return compare_models(
            challenger_dir, champion_dir, champion_go=champion_go, reps=N_BOOT, seed=SEED, q=quantile,
            starting_quote=float(cfg.backtest.starting_quote),
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
            # R1-R3 of the revisions are charged to the model version that failed them, not to the whole day
            parity += len((block.get("revision_by_version") or {}).get(version, []))
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

    def get_version(self, version: str) -> Any:
        """A registered version by name (read only): L5 judges the challenger that was in charge before the night."""
        return self._inner.get_version(version)

    def promote(self, version: str, alias: str = CHALLENGER, *, reason: str = "") -> None:
        if alias != CHALLENGER:
            raise RegistryError(f"the nightly learner may only move the {CHALLENGER!r} alias, not {alias!r}")
        self._inner.promote(version, alias, reason=reason)


class AuditLabels:
    """The ``label_config`` the AUDITS (L0: state parity, revision impact) measure with: the one of the MODEL THAT RUNS
    (the registry champion; for a decision, the version that took it), not the one of the research pool the refit
    trains on (``train.yml``). The two differ as soon as the champion was extended to a product the research pool does
    not have (``script/extend_label_config.py``): auditing it with the pool's label fails on the spread of that
    product. The refit (L1-L3) keeps the training label. Without a champion the audits fall back to the training label,
    and say so (``audit_label.source`` in the L0 details, and a warning in the log)."""

    def __init__(self, registry: _ChallengerOnly, training: Any | None) -> None:
        self._registry, self._training = registry, training
        self._by_dir: dict[Path, Any] = {}
        self._fallback_logged = False

    def _load(self, model_dir: Path) -> Any:
        from modules.risk.label_config import load_label_config

        if model_dir not in self._by_dir:
            self._by_dir[model_dir] = load_label_config(model_dir / "label_config.json")
        return self._by_dir[model_dir]

    def champion(self) -> tuple[Any | None, dict[str, Any]]:
        """``(label, info)``: the champion's label_config, else the training one (``info["source"]`` says which)."""
        try:
            model = self._registry.get(CHAMPION)
            label = self._load(Path(model.path))
        except (UnknownAlias, RegistryError, OSError, ValueError, KeyError) as exc:
            reason = f"{type(exc).__name__}: {exc}"
            if self._training is None:
                return None, {"source": "none", "reason": reason}
            if not self._fallback_logged:
                self._fallback_logged = True
                log.warning("learner_audit_label_fallback", reason=reason, label_sha256=self._training.sha256)
            return self._training, {
                "source": "training_fallback", "reason": reason, "label_config_sha256": self._training.sha256,
                "products": sorted(self._training.spread_bps),
            }  # fmt: skip
        return label, {
            "source": "champion", "version": model.version, "label_config_sha256": label.sha256,
            "products": sorted(label.spread_bps),
        }  # fmt: skip

    def of_version(self, version: str | None) -> Any | None:
        """The label_config stored in a registry version (the one that took a decision), None if it cannot be read."""
        if not version:
            return None
        try:
            return self._load(Path(self._registry.get_version(version).path))
        except (RegistryError, OSError, ValueError, KeyError):
            return None


@dataclass(frozen=True)
class LearnerPaths:
    reports_dir: Path  # data/reports/learner
    decisions_dir: Path  # data/decisions
    live_labels_dir: Path  # data/live_labels
    bars_root: Path  # data/bars/1m
    fill_model_path: Path  # models/registry/fill_model.json
    label_config: Path  # data/datasets/label_config.json (read only)
    work_dir: Path  # scratch for nothing the spec needs to keep
    drift_dir: Path | None = None  # data/reports/sim_p1f: <version>/ holds the PSI-v2 calibration of that champion


# label_config -> product -> training.backtest.ProductMarket (the products and the BTC context are the label's)
MarketsLoader = Callable[[Any], Mapping[str, Any]]


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
        oos: OosComparator | None = None,
        econ: GateEconConfig | None = None,
        markets: MarketsLoader | None = None,
        label: Any | None = None,
        psi_alert: float = PSI_ALERT,
        gate_ops: GateOpsConfig | None = None,
        risk: RiskConfig | None = None,
        deciders: Callable[[str], tuple[Any, ...]] | None = None,
        ledger_db: Path | None = None,
    ) -> None:
        self._cfg, self._paths, self._products = cfg, paths, tuple(products)
        self._econ = econ or GateEconConfig()
        # L0 impact of the REST revisions (spec 5.1.7 "Barre REST"): thresholds, risk costs, the models in use
        # (policy_version -> (decider, policy)) and the dry ledger for the LONG states
        self._gate_ops, self._risk = gate_ops or GateOpsConfig(), risk or RiskConfig()
        self._deciders, self._ledger_db = deciders, ledger_db
        self._registry = _ChallengerOnly(registry)
        self._clock = clock
        self._candles, self._costs = candle_source, cost_source
        self._pipeline, self._backtester, self._shadow, self._oos = pipeline, backtester, shadow, oos
        self._markets_loader, self._label = markets, label  # ``label``: the TRAINING label (L1-L3)
        self._audit = AuditLabels(self._registry, label)
        self._psi_alert = psi_alert
        self._markets_cache: dict[str, Mapping[str, Any]] = {}
        # BUY records and intents of the model books of the audited day, per model version (set by ``_state_parity``)
        self._buy_records: dict[str, int] = {}
        self._intent_records: dict[str, int] = {}
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
        existing = self._load(day)
        previous = {} if force else existing
        self._doc = {
            "v": SCHEMA, "date": day.isoformat(), "audited_day": (day - timedelta(days=1)).isoformat(),
            "cutoff": _iso(self._cutoff), "refit_day": do_refit,
            "started_at": previous.get("started_at", _iso(started)), "updated_at": _iso(started),
            "champion_before": previous.get("champion_before", self._alias_version(CHAMPION)),
            # the challenger in charge when the night began: what L5 judges. Kept even under --force, because by then
            # L2 may already have moved the alias onto the version trained tonight.
            "challenger_before": existing.get("challenger_before", self._alias_version(CHALLENGER)),
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
        if step == "L5":
            return  # L5 judges the challenger of BEFORE the night: independent of how L3/L1/L2 ended
        order = ("L3", "L1", "L2")
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
            "rest_revisions": self._rest_audit, "revision_impact": self._revision_impact,
            "state_parity": self._state_parity, "feature_drift": self._feature_drift,
        }  # fmt: skip
        result.details["audit_label"] = self._audit.champion()[1]
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
        if all("skipped" in result.details[name] for name in parts):
            raise Skip("insufficient_samples", result.details)
        return result

    def _rest_audit(self, audited: date) -> dict[str, Any]:
        if self._candles is None:
            raise Skip("no_candle_source")
        lo = datetime(audited.year, audited.month, audited.day, tzinfo=UTC)
        hi = lo + timedelta(days=1)
        store = BarStore(self._paths.bars_root)
        out: dict[str, Any] = {}
        tables: list[pd.DataFrame] = []
        for product in self._products:
            months = [m for m in store.months(product) if (lo.year, lo.month) <= m <= (hi.year, hi.month)]
            if not months:
                out[product] = {
                    "minutes": 0, "revised": 0, "ratio": 0.0, "max_close_diff_bps": 0.0, "value_revised": 0,
                    "appeared": 0, "disappeared": 0,
                }  # fmt: skip
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
            tables.append(revisions_frame(product, classify_revisions(stored, fresh, lo, hi), stored, fresh))
            out[product] = {
                "minutes": stats.minutes, "revised": stats.revised, "ratio": stats.ratio,
                "max_close_diff_bps": stats.max_close_diff_bps, "value_revised": stats.value_revised,
                "appeared": stats.appeared, "disappeared": stats.disappeared,
            }  # fmt: skip
        # the revisions of the day, kept for the impact audit of the following days (never applied to the store)
        rows = write_revisions(self._revisions_path(audited), tables)
        return {
            "products": out, "rest_revision_ratio_max": max((v["ratio"] for v in out.values()), default=0.0),
            "revisions_file": self._revisions_path(audited).name, "revisions_rows": rows,
        }  # fmt: skip

    def _revisions_path(self, audited: date) -> Path:
        return self._paths.reports_dir / "revisions" / f"{audited.isoformat()}.parquet"

    def _revision_impact(self, audited: date) -> dict[str, Any]:
        """R1-R3 of the audited day for every registry book and model version in use (revision_impact.day_impact)."""
        label, _info = self._audit.champion()
        if label is None:
            raise Skip("no_label_config")
        if self._deciders is None:
            raise Skip("no_deciders")
        try:
            return day_impact(
                audited=audited, products=self._products, bars_root=self._paths.bars_root,
                revisions_dir=self._paths.reports_dir / "revisions",
                decisions_path=self._paths.decisions_dir / f"{audited.isoformat()}.jsonl", loader=self._deciders,
                label=label, risk=self._risk, gate=self._gate_ops, ledger_db=self._ledger_db,
            )  # fmt: skip
        except ImpactSkip as skip:
            raise Skip(skip.reason, skip.details) from skip

    def _markets(self, label: Any) -> Mapping[str, Any]:
        """The markets of ``label`` (its products, its BTC context), loaded once per label_config."""
        if self._markets_loader is None:
            raise Skip("no_markets_loader")
        if label.sha256 not in self._markets_cache:
            self._markets_cache[label.sha256] = self._markets_loader(label)
        return self._markets_cache[label.sha256]

    def _state_parity(self, audited: date) -> dict[str, Any]:
        """FLAT states of ``audited`` as the engine logged them against the batch path, and PSI against 90 days."""
        from modules.features.state_matrix import flat_matrix

        champion_label, _info = self._audit.champion()
        if champion_label is None:
            raise Skip("no_label_config")
        path = self._paths.decisions_dir / f"{audited.isoformat()}.jsonl"
        if not path.exists():
            raise Skip("no_decisions")
        # the state of a decision was built with the label_config of the version that took it: that is the one the
        # batch path must use (a champion extended to another product, a challenger with other barriers)
        labels: dict[str, Any] = {champion_label.sha256: champion_label}
        # (label sha, product) -> close instant -> (FLAT vector as logged, ``btc_context`` of the record, model version)
        live_rows: dict[tuple[str, str], dict[datetime, tuple[list[float], str | None, str | None]]] = defaultdict(dict)
        self._buy_records, self._intent_records = {}, {}
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
                self._count_model_record(rec)
                state = DecisionState.model_validate_json(rec["state"])
                t = datetime.fromisoformat(rec["ts_bar_close"].replace("Z", "+00:00")).astimezone(UTC)
            except (ValueError, KeyError, TypeError):
                continue
            if state.position.side == "FLAT":
                version = (rec.get("policy") or {}).get("policy_version")
                label = self._audit.of_version(version) or champion_label
                labels[label.sha256] = label
                flat = flatten_state(state)
                context = rec.get("btc_context")
                live_rows[(label.sha256, state.product)][t] = (
                    [flat[c] for c in FLAT_COLUMNS], context if isinstance(context, str) else None, version,
                )  # fmt: skip
        if not live_rows:
            raise Skip("no_decisions")
        live_all, batch_all, psi_by_feature = [], [], defaultdict(list)
        # per compared row, in the order of ``live_all``: label sha, product, ``btc_context`` of the record, version
        meta: list[tuple[str, str, str | None, str | None]] = []
        unmatched = 0
        for (sha, product), by_t in sorted(live_rows.items()):
            label = labels[sha]
            markets = self._markets(label)
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
            batch_all.append(flat_matrix(pm.ind, rows, product, label, pm.btc_ind, btc_rows))
            kept = [by_t[stamps[i]] for i in np.flatnonzero(keep)]
            live_all.append(np.array([vec for vec, _, _ in kept], dtype=np.float64))
            meta += [(sha, product, ctx, version) for _, ctx, version in kept]
            lo = np.datetime64((self._cutoff - timedelta(days=PSI_HISTORY_DAYS)).replace(tzinfo=None), "ns")
            hi = np.datetime64(datetime(audited.year, audited.month, audited.day).replace(tzinfo=None), "ns")
            hist_rows = np.flatnonzero((pm.bars.ts_close >= lo) & (pm.bars.ts_close < hi) & (close.minute == 0))
            if hist_rows.size:
                hist = flat_matrix(
                    pm.ind, hist_rows, product, label, pm.btc_ind,
                    None if pm.btc_rows is None else pm.btc_rows[hist_rows],
                )  # fmt: skip
                for j, name in enumerate(FLAT_COLUMNS):
                    psi_by_feature[name].append((live_all[-1][:, j], hist[:, j]))
        if not live_all:
            raise Skip("no_matching_bars")
        live, batch = np.vstack(live_all), np.vstack(batch_all)
        misaligned, btc_block = self._btc_context(live, batch, meta, labels)
        every = feature_parity(live, batch)
        aligned = feature_parity(live[~misaligned], batch[~misaligned])
        parity = {
            "rows": every["rows"], "match": aligned["match"], "match_all": every["match"],
            "worst_feature": aligned["worst_feature"], "mismatch_by_feature": aligned["mismatch_by_feature"],
        }  # fmt: skip
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
            "state_parity": parity, "unmatched_rows": unmatched, "btc_context": btc_block,
            # the old daily PSI against 90 days: a DIAGNOSTIC (conflict P1f), no criterion, no guard, no gate
            "psi_v1": {
                "rule": PSI_V1_RULE, "status": "diagnostic", "psi": psi,
                "max_feature_psi": None if worst is None else finite[worst], "worst_feature": worst,
                "above_0_10": sum(1 for v in finite.values() if v > PSI_V1_ABOVE),
                "alert_threshold": self._psi_alert, "alerts": [{"feature": k, "psi": finite[k]} for k in alerts],
            },
        }  # fmt: skip

    def _count_model_record(self, rec: Mapping[str, Any]) -> None:
        """Per model version, the BUY decisions (a BUY turned into HOLD ``btc_context_stale`` is a BUY of the model)
        and the order intents of the model books: with a policy that is ``untunable`` there must be none."""
        pol = rec.get("policy")
        if "error" in rec or rec.get("book") not in MODEL_BOOKS or not isinstance(pol, Mapping):
            return
        version = pol.get("policy_version")
        if not isinstance(version, str):
            return
        risk = rec.get("risk") or {}
        if pol.get("final") == "BUY" or pol.get("reason") == "btc_context_stale":
            self._buy_records[version] = self._buy_records.get(version, 0) + 1
        if rec.get("order_intent_id") or risk.get("outcome") == "intent":
            self._intent_records[version] = self._intent_records.get(version, 0) + 1

    def _btc_context(
        self, live: np.ndarray, batch: np.ndarray, meta: Sequence[tuple[str, str, str | None, str | None]],
        labels: Mapping[str, Any],
    ) -> tuple[np.ndarray, dict[str, Any]]:  # fmt: skip
        """BTC context of the alt states (never of BTC itself): which rows had a context that was not of the same
        instant (``misaligned``: the model answered on a state without the BTC features, or with others), by class,
        and how much the model answers and the actionability of a BUY move between the state as served and the aligned
        one (the batch state), with the model and the policy of the version that took the decision. Returns the mask
        of misaligned rows (aligned with ``live``) and the block of the document."""
        misaligned = np.zeros(len(meta), dtype=bool)
        by_product: dict[str, list[int]] = defaultdict(list)
        for i, (_sha, product, _ctx, _version) in enumerate(meta):
            if not product.startswith("BTC-"):
                by_product[product].append(i)
        products: dict[str, Any] = {}
        for product, idx in sorted(by_product.items()):
            classes = dict.fromkeys(BTC_CLASSES, 0)
            mine: list[int] = []
            for i in idx:
                cls, bad = classify_btc_row(meta[i][2], live[i, list(BTC_COLUMNS)], batch[i, list(BTC_COLUMNS)])
                if cls is not None:
                    classes[cls] += 1
                if bad:
                    misaligned[i] = True
                    mine.append(i)
            status, max_shift, flips, basis, check = self._btc_shift(mine, live, batch, meta, labels)
            products[product] = {
                "alt_states": len(idx), "misaligned": len(mine), "share": len(mine) / len(idx), "classes": classes,
                "max_shift": max_shift, "flips": flips, "shift_status": status, "flips_basis": basis,
                "untunable_check": check,
            }  # fmt: skip
        return misaligned, {"products": products}

    def _btc_shift(
        self, rows: Sequence[int], live: np.ndarray, batch: np.ndarray,
        meta: Sequence[tuple[str, str, str | None, str | None]], labels: Mapping[str, Any],
    ) -> tuple[str, float | None, int | None, str | None, dict[str, Any] | None]:  # fmt: skip
        """``(shift_status, max_shift, flips, flips_basis, untunable_check)`` of the misaligned ``rows`` of ONE
        product. Nothing to measure is a measured zero (basis ``computed``). ``max_shift`` stays measured under
        ``policy_untunable``. Flips of a version whose ``policy.json`` is explicitly ``untunable`` are exactly 0 (basis
        ``policy_untunable``: the BUY is not actionable for ANY input, live or batch) but only if the model books of
        the day show no BUY decision and no order intent of that version; otherwise (or with the policy file missing)
        they are ``None`` and the report decides (a BUY under an untunable policy is a defect: ``fail``)."""
        if not rows:
            return "measured", 0.0, 0, "computed", None
        groups: dict[tuple[str, str | None], list[int]] = defaultdict(list)
        for i in rows:
            groups[(meta[i][0], meta[i][3])].append(i)
        status, shift, flips, flips_known = "measured", 0.0, 0, True
        bases: set[str] = set()
        check_versions: dict[str, Any] = {}
        for (sha, version), members in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
            part, part_shift, part_flips = "no_deciders", None, None
            try:
                if self._deciders is not None and version is not None:
                    decider, policy = self._deciders(version)[:2]
                    if not hasattr(decider, "answers_batch"):
                        part = "decider_without_batch"
                    else:
                        m, part_flips = flat_pair_shift(
                            decider, policy, labels[sha], self._risk, live[members], batch[members]
                        )
                        part = "measured" if part_flips is not None else "policy_untunable"
                        part_shift = float(np.where(np.isfinite(m), m, 1.0).max())  # +inf: the largest possible shift
            except Exception as exc:  # a version that cannot be loaded: reported as not measured, never a pass
                log.warning("learner_btc_shift_unavailable", version=version, error=f"{type(exc).__name__}: {exc}")
            if part == "measured":
                bases.add("computed")
            elif part == "policy_untunable" and version is not None:
                try:
                    kind = policy_kind(Path(self._registry.get_version(version).path))
                except Exception:
                    kind = "missing"
                buys, intents = self._buy_records.get(version, 0), self._intent_records.get(version, 0)
                check_versions[version] = {"policy": kind, "buy_records": buys, "intent_records": intents}
                if kind == "untunable" and buys == 0 and intents == 0:
                    part_flips = 0
                    bases.add("policy_untunable")
            if _SHIFT_RANK[part] > _SHIFT_RANK[status]:
                status = part
            shift = shift if part_shift is None else max(shift, part_shift)
            flips_known = flips_known and part_flips is not None
            flips += part_flips or 0
        known = flips_known and status in ("measured", "policy_untunable")
        basis = (next(iter(bases)) if len(bases) == 1 else "mixed") if known and bases else None
        measured_shift = status in ("measured", "policy_untunable")
        return (
            status, shift if measured_shift else None, flips if known else None, basis,
            {"versions": check_versions} if check_versions else None,
        )  # fmt: skip

    # ------------------------------------------------------------------------------------- feature drift

    def _feature_drift(self, audited: date) -> dict[str, Any]:
        """Feature drift (``modules/dryrun/drift.py``) on the last ``window_days`` complete UTC days that end at the
        cutoff of the run: hourly states rebuilt from the stored bars, per served product, against the training
        reference frozen by SIM-P1f-bis (PSI-v3, folder ``PSI_V3_VOLSTD``) or, as a fallback, by SIM-P1f (PSI-v2) for
        the champion in use. No adopted calibration: ``unmeasured`` (PSI-v2) or ``not_testable`` (PSI-v3 rejected,
        final), with the reason; a PSI-v3 calibration always brings the diagnostics."""
        if self._paths.drift_dir is None:
            raise Skip("no_drift_dir")
        label, info = self._audit.champion()
        if label is None or info.get("source") != "champion":
            raise Skip("no_champion")
        version = str(info["version"])
        cal = None
        for rule in (drift.RULE_V3, drift.RULE):  # CalibrationError: the part fails (L0 failed), never a quiet pass
            cal = drift.load_calibration(drift.run_dir_of(self._paths.drift_dir, rule, version), rule)
            if cal is not None:
                break
        if cal is None:
            raise Skip("no_calibration")
        run_dir = cal.run_dir
        if cal.rule_id == drift.RULE_V3["id"]:
            return self._feature_drift_v3(label, version, cal)
        summary = {
            "run_dir": run_dir.as_posix(), "sha256": cal.sha256, "champion_version": cal.champion_version,
            "window_days": cal.window_days, "alpha": cal.alpha, "c": cal.c,
        }  # fmt: skip
        if cal.decision != "adopted" or cal.window_days is None:
            return {"rule": "PSI_V2", "status": "unmeasured", "reason": f"calibration_{cal.decision}",
                    "calibration": summary}  # fmt: skip
        if cal.label_config_sha256 != label.sha256:
            return {"rule": "PSI_V2", "status": "unmeasured", "reason": "calibration_label_mismatch",
                    "calibration": summary}  # fmt: skip
        end = self._cutoff  # only the past: the window ends at 00:00 UTC of the run date
        start = end - timedelta(days=cal.window_days)
        products: dict[str, Any] = {}
        for product, pm in sorted(self._markets(label).items()):
            x, _ts = drift.hourly_states(pm, label, start, end)
            if x.shape[0] < 24 * cal.window_days:
                products[product] = {"status": "unmeasured", "reason": "incomplete_window", "states": int(x.shape[0])}
                continue
            res = drift.window_stat(x, cal, product)
            products[product] = {
                "status": "measured", "states": int(x.shape[0]), "S": res.S, "alarm": res.alarm,
                "flagged": [
                    {"feature": n, "psi": r.psi, "x_f": r.x_f, "p": r.p} for n, r in res.features.items() if r.flag
                ],
                "features": {
                    n: {"psi": r.psi, "p": r.p, "x_f": r.x_f, "n_finite": r.n_finite,
                        "out_of_range_share": r.out_of_range_share, "n_eff_7d": cal.features[n].n_eff_7d}
                    for n, r in res.features.items()
                },
                "unmeasured_features": [n for n, r in res.features.items() if r.psi is None],
            }  # fmt: skip
        measured = [b for b in products.values() if b["status"] == "measured"]
        return {
            "rule": "PSI_V2", "status": "measured" if measured else "unmeasured",
            "reason": None if measured else "incomplete_window", "calibration": summary,
            "window": {"start": _iso(start), "end": _iso(end)}, "products": products,
            "alarm": any(b["alarm"] for b in measured) if measured else None,
        }  # fmt: skip

    def _p_edge(self, version: str, x: np.ndarray) -> tuple[np.ndarray | None, str | None]:
        """The ``p_edge`` of the model version on the states ``x`` (diagnostic ``p_edge_psi``): ``(array, None)`` or
        ``(None, reason)`` with ``no_deciders`` / ``decider_error``."""
        if self._deciders is None:
            return None, "no_deciders"
        try:
            decider = self._deciders(version)[0]
            return np.asarray(decider.answers_batch(x).p_edge, dtype=np.float64), None
        except Exception as exc:
            log.warning("learner_p_edge_unavailable", version=version, error=f"{type(exc).__name__}: {exc}")
            return None, "decider_error"

    @staticmethod
    def _watch(wcal: drift.Calibration | None, x: np.ndarray, product: str) -> dict[str, Any]:
        """The watch of a product (rejected PSI-v3): the statistic of ``drift.window_stat`` with the diagnostic
        reference and threshold of the rejected run. An alarm only, never a verdict of the gate."""
        if wcal is None:
            return {"status": "unmeasured", "reason": "no_diagnostic_reference"}
        res = drift.window_stat(x, wcal, product)
        if res.S is None:
            return {"status": "unmeasured", "reason": "incomplete"}
        return {
            "status": "measured", "S": res.S, "alarm": res.alarm,
            "flagged": [
                {"feature": n, "psi": r.psi, "x_f": r.x_f, "p": r.p} for n, r in res.features.items() if r.flag
            ],
        }  # fmt: skip

    def _feature_drift_v3(self, label: Any, version: str, cal: drift.Calibration) -> dict[str, Any]:
        """PSI-v3 block. ``measured``: the rule on J (``window_stat``) plus the diagnostics; ``not_testable`` (the
        calibration was rejected, definitively): no test, the diagnostics only. ``scope`` says what is covered."""
        untested = [{"feature": n, "n_eff_7d": e} for n, e in cal.untested]
        scope = {"tested": list(cal.features), "untested": untested, "note": drift.scope_note(len(untested))}
        summary = {
            "run_dir": cal.run_dir.as_posix(), "sha256": cal.sha256, "champion_version": cal.champion_version,
            "window_days": cal.window_days, "alpha": cal.alpha, "c": cal.c, "decision": cal.decision,
            "final": cal.final,
        }  # fmt: skip
        head = {"rule": drift.RULE_V3["id"], "calibration": summary, "scope": scope}
        if cal.label_config_sha256 != label.sha256:
            return {**head, "status": "unmeasured", "reason": "calibration_label_mismatch"}
        testable = cal.decision == "adopted" and cal.window_days is not None
        days = cal.window_days or 7
        end = self._cutoff  # only the past: the window ends at 00:00 UTC of the run date
        start = end - timedelta(days=days)
        products: dict[str, Any] = {}
        watch_cal = None if testable else drift.watch_calibration(cal)  # the watch of a rejected run (conflict P1g)
        for product, pm in sorted(self._markets(label).items()):
            x, _ts = drift.hourly_states(pm, label, start, end)
            if x.shape[0] < 24 * days:
                products[product] = {"status": "unmeasured", "reason": "incomplete_window", "states": int(x.shape[0])}
                continue
            p_edge, why = self._p_edge(version, x)
            diagnostics = drift.window_diagnostics(x, cal, product, p_edge)
            if why == "decider_error":
                diagnostics["p_edge_psi"] = {"status": "unmeasured", "reason": why}
            if not testable:
                products[product] = {
                    "status": "not_testable", "states": int(x.shape[0]), "diagnostics": diagnostics,
                    "watch": self._watch(watch_cal, x, product),
                }  # fmt: skip
                continue
            res = drift.window_stat(x, cal, product)
            products[product] = {
                "status": "measured", "states": int(x.shape[0]), "S": res.S, "alarm": res.alarm,
                "flagged": [
                    {"feature": n, "psi": r.psi, "x_f": r.x_f, "p": r.p} for n, r in res.features.items() if r.flag
                ],
                "features": {
                    n: {"psi": r.psi, "p": r.p, "x_f": r.x_f, "n_finite": r.n_finite,
                        "out_of_range_share": r.out_of_range_share, "n_eff_7d": cal.features[n].n_eff_7d}
                    for n, r in res.features.items()
                },
                "unmeasured_features": [n for n, r in res.features.items() if r.psi is None],
                "diagnostics": diagnostics,
            }  # fmt: skip
        support_alarm = [
            p for p, b in products.items() if ((b.get("diagnostics") or {}).get("support") or {}).get("share", 0) > 0
        ]
        window = {"start": _iso(start), "end": _iso(end)}
        if not testable:
            watch_alarm = sorted(p for p, b in products.items() if (b.get("watch") or {}).get("alarm") is True)
            return {
                **head, "status": "not_testable", "reason": f"calibration_{cal.decision}_final", "window": window,
                "products": products, "alarm": None, "support_alarm": support_alarm,
                "watch": {
                    "rule": drift.RULE_V3["id"], "reference": "reference_diagnostic", "c": cal.watch_c,
                    "false_alarm_oos": cal.watch_false_alarm, "gate": False,
                },
                "watch_alarm": watch_alarm,
            }  # fmt: skip
        measured = [b for b in products.values() if b["status"] == "measured"]
        return {
            **head, "status": "measured" if measured else "unmeasured",
            "reason": None if measured else "incomplete_window", "window": window, "products": products,
            "alarm": any(b["alarm"] for b in measured) if measured else None, "support_alarm": support_alarm,
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
            dataset = pipe.build_dataset(self._live_input())
            self._progress["dataset"] = str(dataset)
            self._save(day)
        model = self._reuse("model")
        if model is None:
            model = pipe.train(dataset)
            self._progress["model"] = str(model)
            self._save(day)
        in_dataset = _dataset_live_block(dataset)
        used = int(in_dataset.get("rows", 0)) if in_dataset else 0
        return StepResult(
            {
                "dataset": str(dataset), "model": model.name, "live_labels": live,
                "live_weight": self._cfg.live_weight, "live_weight_applied": used > 0,
                "live_dataset": in_dataset, "cutoff": _iso(self._cutoff),
            },
            [str(model)],
        )  # fmt: skip

    def _live_input(self) -> LiveInput | None:
        """The live labels for the dataset builder: None when the folder is absent or holds no label file."""
        from training.build_dataset import LiveInput

        folder = self._paths.live_labels_dir
        if not folder.is_dir() or not any(folder.glob("*.jsonl")):
            return None
        return LiveInput(folder, self._cfg.live_weight, self._cutoff)

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
            "live_dataset": self._doc["steps"]["L3"]["details"].get("live_dataset"),
        }  # fmt: skip
        try:
            version = self._registry.register(artifacts_from_dir(model), _clean(manifest))
        except VersionExists:
            version = model.name  # a previous run registered it before it could record the step
        self._registry.promote(version, CHALLENGER, reason=f"nightly learner {day.isoformat()} (L2)")
        self._progress["registered"] = version
        return StepResult(
            {"version": version, "alias": CHALLENGER, "final_policy_untunable": untunable, "evaluation": evaluation},
            [version],
        )  # fmt: skip

    # ------------------------------------------------------------------------------------------------ L5

    def _common_end(self) -> datetime:
        """End of the common backtest window: the earliest close of the last bar of the products that have bars, never
        past the cutoff. (No bars at all: the cutoff; the real backtester then fails on its own, loudly.)"""
        store = BarStore(self._paths.bars_root)
        lasts = [t for p in self._products if (t := store.last_ts(p)) is not None]
        return min(min(lasts) + MINUTE, self._cutoff) if lasts else self._cutoff

    def _l5(self, day: date) -> StepResult:
        """Verdict of rule L5 on the challenger that was in charge BEFORE tonight's refit. The learner reports it and
        does nothing else: ``promoted`` is always false (promotion is a local act)."""
        if self._backtester is None or self._shadow is None:
            raise Skip("no_evidence_source")
        before = self._doc.get("challenger_before")
        if not before:
            raise Skip("no_previous_challenger")
        try:
            champion = self._registry.get(CHAMPION)
        except UnknownAlias:
            raise Skip("no_champion") from None
        if before == champion.version:
            raise Skip("challenger_is_champion", {"version": before})
        challenger = self._registry.get_version(before)
        rule = PromotionRule.from_config(self._cfg, self._econ)
        # ONE window for both models: not before the holdout of either label_config, not past the last bar
        end = self._common_end()
        holdouts = [h for h in (holdout_start(challenger.path), holdout_start(champion.path)) if h is not None]
        start = max([end - timedelta(days=rule.backtest_recent_days), *holdouts])
        challenger_gates, champion_gates = gate_summary(challenger.path), gate_summary(champion.path)
        champion_go = champion_gates.ge == GE_GO
        embargo_ok = [
            (t := final_train_end(m.path)) is not None and t <= start - OOS_EMBARGO for m in (challenger, champion)
        ]  # V1: neither model trained on data that ends after the window starts (minus the embargo)
        shadow = self._shadow(challenger.version, self._cutoff)
        evidence = PromotionEvidence(
            challenger=self._backtester(challenger.path, start, end),
            champion=self._backtester(champion.path, start, end),
            challenger_gates=challenger_gates, champion_gates=champion_gates,
            oos=(
                None if self._oos is None else self._oos(
                    challenger.path, champion.path, champion_go=champion_go, quantile=rule.superiority_quantile
                )
            ),
            oos_provable=all(embargo_ok),
            shadow_days=shadow.days, operational_anomalies=shadow.operational_anomalies,
            parity_anomalies=shadow.parity_anomalies,
        )  # fmt: skip
        decision = decide_promotion(evidence, rule)
        return StepResult(
            {
                "challenger": challenger.version, "champion": champion.version, "verdict": decision.verdict,
                "reasons": list(decision.reasons), "promoted": False,
                "window": {"start": _iso(start), "end": _iso(end), "days": (end - start).total_seconds() / 86400.0},
                "command": f'python -m modules.dryrun.registry promote {challenger.version} --reason "<why>"',
                "note": "promotion is a local act; a manual promote overrides this verdict (decision U2)",
                "evidence": _clean(asdict(evidence)),
            }
        )  # fmt: skip


def _dataset_live_block(dataset: Path) -> dict[str, Any] | None:
    """The ``live`` block of the dataset manifest (rows, weight, rows per training set, exclusions), None if absent."""
    try:
        manifest = json.loads((dataset / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    block = manifest.get("live") if isinstance(manifest, dict) else None
    return block if isinstance(block, dict) else None


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
    from modules.dryrun.revision_impact import RegistryDeciderLoader
    from modules.market.backfill import CoinbaseCandleSource, bars_root
    from modules.risk.label_config import load_label_config

    app = load_config(config_path)
    data = Path(app.paths.data_dir)
    registry = FilesystemModelRegistry(Path(app.paths.models_dir) / "registry")
    label_path = Path(train_config and _label_path(train_config))
    label = load_label_config(label_path) if label_path.exists() else None

    def markets(audit_label: Any) -> Mapping[str, Any]:
        from training.tune_policy import load_markets

        # the products and the BTC context of the label_config being audited (the champion's, not the research pool's)
        products = [p for p in app.market.products if p in audit_label.spread_bps]
        return load_markets(bars_root(app), products, audit_label)

    paths = LearnerPaths(
        reports_dir=data / "reports" / "learner", decisions_dir=data / "decisions",
        live_labels_dir=data / "live_labels", bars_root=bars_root(app),
        fill_model_path=Path(app.dry_run.fill_model.path), label_config=label_path,
        work_dir=data / "reports" / "learner" / "work", drift_dir=data / "reports" / "sim_p1f",
    )  # fmt: skip
    return NightlyLearner(
        cfg=app.dry_run.learner, paths=paths, registry=registry,
        products=app.market.products, candle_source=CoinbaseCandleSource(),
        cost_source=LedgerCostSource(app.dry_run.db_path), pipeline=TrainingPipeline(Path(train_config)),
        backtester=HoldoutBacktester(Path(train_config)),
        shadow=ReportShadowEvidence(data / "decisions", data / "reports" / "dry"),
        oos=PairedOosComparator(Path(train_config)), econ=app.dry_run.gate_econ,
        markets=markets, label=label, psi_alert=app.risk.kill_switch.psi_threshold,
        gate_ops=app.dry_run.gate_ops, risk=app.risk, ledger_db=Path(app.dry_run.db_path),
        deciders=RegistryDeciderLoader(registry, label),
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
