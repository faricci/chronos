"""NightlyLearner (spec sez. 5.1.6, 5.3 punto 11, 6.3 D11): steps, invariants, idempotence, L0 and L4 measures."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from modules.config import LearnerConfig
from modules.dryrun import learner as learner_module
from modules.dryrun.fill_model import FillModelParams
from modules.dryrun.learner import (
    CostSample,
    LearnerPaths,
    LedgerCostSource,
    NightlyLearner,
    ReportShadowEvidence,
    ShadowRecord,
    TrainingPipeline,
    feature_parity,
    population_stability_index,
)
from modules.dryrun.registry import (
    BacktestEvidence,
    FilesystemModelRegistry,
    RegistryError,
    artifacts_from_dir,
)
from modules.features.serializer import FLAT_COLUMNS, serialize_state
from modules.features.state_builder import PositionInput, StateBuilder
from modules.ledger.positions import SCHEMA as LEDGER_SCHEMA
from modules.market.backfill import BarStore, StoreCandleSource
from modules.market.indicators import IndicatorSnapshot
from tests.indicator_fixtures import synthetic_bars
from tests.label_fixtures import loaded_label

REFIT_DAY = date(2026, 11, 1)  # day 1 of the month = refit_day_of_month
NOW = datetime(2026, 11, 1, 0, 15, tzinfo=UTC)
LABEL = loaded_label({"BTC-EUR": 3.0, "SOL-EUR": 4.5})
GOOD = BacktestEvidence(90.0, Decimal("10"), 2.0, 0.01)
WORSE = BacktestEvidence(90.0, Decimal("1"), 0.5, 0.02)


# ------------------------------------------------------------------------------------------- fixtures


def make_version(root: Path, name: str, *, n_eff: float = 673.0, edge_ok: bool = True) -> Path:
    model = root / "models" / name
    (model / "final").mkdir(parents=True)
    cal = {"edge_cal": {"scheme": "OOF_PLATT_V1", "n_eff": n_eff, "n_eff_min": 200.0, "ok": edge_ok}}
    (model / "final" / "calibration.json").write_text(json.dumps(cal), encoding="utf-8")
    card = {
        "model": name, "label_config": {"sha256": "abc"}, "calibration": cal,
        "data": {"dataset_run_id": "run1", "indicator_anchor": {}, "holdout": ["2026-06-20", "2026-10-31"]},
    }  # fmt: skip
    (model / "model_card.json").write_text(json.dumps(card), encoding="utf-8")
    (model / "label_config.json").write_text("{}", encoding="utf-8")
    return model


class FakePipeline:
    """Same four calls as ``TrainingPipeline`` without training anything."""

    def __init__(self, root: Path, *, n_eff: float = 673.0, edge_ok: bool = True, untunable: bool = False) -> None:
        self.root, self.n_eff, self.edge_ok, self.untunable = root, n_eff, edge_ok, untunable
        self.calls: list[str] = []
        self.fail_on: str | None = None
        self.versions = 0

    def _maybe_fail(self, what: str) -> None:
        self.calls.append(what)
        if self.fail_on == what:
            raise RuntimeError(f"{what} exploded")

    def build_dataset(self) -> Path:
        self._maybe_fail("build")
        run = self.root / "datasets" / "run1"
        run.mkdir(parents=True, exist_ok=True)
        (run / "manifest.json").write_text("{}", encoding="utf-8")
        return run

    def train(self, dataset: Path) -> Path:
        self._maybe_fail("train")
        self.versions += 1
        return make_version(
            self.root, f"baseline-lgbm@2026-11-01.{self.versions}", n_eff=self.n_eff, edge_ok=self.edge_ok
        )

    def tune(self, model: Path, dataset: Path) -> None:
        self._maybe_fail("tune")
        policy: dict[str, Any] = {"untunable": True, "reason": "untunable_ev"} if self.untunable else {"tau_buy": 0.5}
        (model / "final" / "policy.json").write_text(json.dumps(policy), encoding="utf-8")

    def evaluate(self, model: Path, dataset: Path) -> dict[str, Any]:
        self._maybe_fail("evaluate")
        return {"exit_code": 1, "status": "NO-GO", "gates": {"G0": True, "GE": "NO-GO"}}


class Evidence:
    """Backtester stub: ``by_version[model dir name]``; counts the calls."""

    def __init__(self, by_version: dict[str, BacktestEvidence], default: BacktestEvidence = WORSE) -> None:
        self.by_version, self.default, self.calls = by_version, default, 0

    def __call__(self, model_dir: Path, days: int, cutoff: datetime) -> BacktestEvidence:
        self.calls += 1
        return self.by_version.get(model_dir.name, self.default)


def shadow_ok(version: str, cutoff: datetime) -> ShadowRecord:
    return ShadowRecord(20.0, 0, 0)


class Rig:
    def __init__(self, tmp_path: Path, **kw: Any) -> None:
        self.tmp = tmp_path
        self.registry = FilesystemModelRegistry(tmp_path / "registry")
        base = make_version(tmp_path / "base", "baseline-lgbm@2026-09-01.1")
        self.registry.register(artifacts_from_dir(base), {"version": base.name})
        self.registry.promote(base.name, "champion")
        self.champion = base.name
        self.pipeline = FakePipeline(tmp_path / "work", **kw.pop("pipeline_kw", {}))
        self.paths = LearnerPaths(
            reports_dir=tmp_path / "reports" / "learner", decisions_dir=tmp_path / "decisions",
            live_labels_dir=tmp_path / "live_labels", bars_root=tmp_path / "bars",
            fill_model_path=tmp_path / "registry" / "fill_model.json", label_config=tmp_path / "label_config.json",
            work_dir=tmp_path / "work",
        )  # fmt: skip
        self.kw = kw

    def learner(self, **extra: Any) -> NightlyLearner:
        args: dict[str, Any] = {
            "cfg": LearnerConfig(), "paths": self.paths, "registry": self.registry, "products": ["SOL-EUR"],
            "clock": lambda: NOW, "pipeline": self.pipeline, "label": LABEL,
        }  # fmt: skip
        args.update(self.kw)
        args.update(extra)
        return NightlyLearner(**args)

    def doc(self, day: date = REFIT_DAY) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads((self.paths.reports_dir / f"{day.isoformat()}.json").read_text("utf-8"))
        return loaded


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def outcomes(doc: dict[str, Any]) -> dict[str, str]:
    return {s: r["outcome"] for s, r in doc["steps"].items()}


# --------------------------------------------------------------------------------- the champion is safe


def test_the_champion_is_never_moved_and_the_challenger_is_proposed(rig: Rig) -> None:
    doc = rig.learner().run(REFIT_DAY)
    assert doc["champion_before"] == doc["champion_after"] == rig.champion
    assert rig.registry.get("champion").version == rig.champion
    assert rig.registry.get("challenger").version == "baseline-lgbm@2026-11-01.1"
    assert doc["challenger_after"] == "baseline-lgbm@2026-11-01.1"
    assert outcomes(doc)["L2"] == "ok"


def test_the_registry_the_learner_holds_cannot_promote_the_champion_or_roll_back(rig: Rig) -> None:
    held = rig.learner()._registry
    with pytest.raises(RegistryError, match="challenger"):
        held.promote("baseline-lgbm@2026-09-01.1", "champion")
    assert not hasattr(held, "rollback")
    assert rig.registry.history("champion") == [rig.champion]


def test_a_bug_that_asks_for_a_champion_promotion_cannot_move_it(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    real = learner_module.NightlyLearner._l2

    def evil(self: NightlyLearner, day: date) -> Any:
        out = real(self, day)
        self._registry.promote(self._progress["registered"], "champion")
        return out

    monkeypatch.setattr(learner_module.NightlyLearner, "_l2", evil)
    doc = rig.learner().run(REFIT_DAY)
    assert doc["steps"]["L2"]["outcome"] == "failed"
    assert rig.registry.get("champion").version == rig.champion


def test_l5_reports_the_verdict_and_never_promotes(rig: Rig) -> None:
    ev = Evidence({"baseline-lgbm@2026-11-01.1": GOOD, rig.champion: WORSE})
    doc = rig.learner(backtester=ev, shadow=shadow_ok).run(REFIT_DAY)
    l5 = doc["steps"]["L5"]
    assert l5["outcome"] == "ok" and l5["details"]["verdict"] == "promote"
    assert l5["details"]["promoted"] is False
    assert rig.registry.get("champion").version == rig.champion


@pytest.mark.parametrize(
    ("challenger", "shadow", "verdict", "reason"),
    [
        (WORSE, ShadowRecord(20.0, 0, 0), "reject", "net_pnl_not_better"),
        (GOOD, ShadowRecord(20.0, 1, 0), "reject", "operational_anomalies"),
        (GOOD, ShadowRecord(3.0, 0, 0), "insufficient", "shadow_days_short"),
    ],
)
def test_l5_verdicts_follow_the_registry_rule(
    rig: Rig, challenger: BacktestEvidence, shadow: ShadowRecord, verdict: str, reason: str
) -> None:
    ev = Evidence({"baseline-lgbm@2026-11-01.1": challenger, rig.champion: WORSE})
    doc = rig.learner(backtester=ev, shadow=lambda v, c: shadow).run(REFIT_DAY)
    assert doc["steps"]["L5"]["details"]["verdict"] == verdict
    assert reason in doc["steps"]["L5"]["details"]["reasons"]
    assert rig.registry.get("champion").version == rig.champion


def test_l5_without_a_champion_is_skipped(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    (tmp_path / "registry" / "aliases.json").unlink()
    doc = rig.learner(backtester=Evidence({}), shadow=shadow_ok).run(REFIT_DAY)
    assert doc["steps"]["L5"]["outcome"] == "skipped: no_champion"


# ------------------------------------------------------------------------------------- calendar and gates


def test_model_steps_wait_for_the_refit_day(rig: Rig) -> None:
    doc = rig.learner().run(date(2026, 11, 2))
    assert [doc["steps"][s]["outcome"] for s in ("L3", "L1", "L2", "L5")] == ["skipped: not_refit_day"] * 4
    assert rig.pipeline.calls == []
    assert doc["refit_day"] is False


def test_refit_can_be_forced_on_any_day(rig: Rig) -> None:
    doc = rig.learner().run(date(2026, 11, 2), refit=True)
    assert doc["steps"]["L3"]["outcome"] == "ok"


def test_too_few_effective_samples_skip_the_calibration_and_everything_after(tmp_path: Path) -> None:
    rig = Rig(tmp_path, pipeline_kw={"n_eff": 299.0})
    doc = rig.learner().run(REFIT_DAY)
    o = outcomes(doc)
    assert o["L3"] == "ok" and o["L1"] == "skipped: insufficient_samples"
    assert o["L2"] == "skipped: upstream_skipped" and o["L5"] == "skipped: upstream_skipped"
    assert doc["steps"]["L1"]["details"]["n_eff_min"] == 300
    assert rig.registry.versions() == [rig.champion]
    assert "tune" not in rig.pipeline.calls


def test_the_calibration_minimum_is_inclusive(tmp_path: Path) -> None:
    rig = Rig(tmp_path, pipeline_kw={"n_eff": 300.0})
    assert outcomes(rig.learner().run(REFIT_DAY))["L1"] == "ok"


def test_an_edge_calibration_flagged_not_ok_is_not_used(tmp_path: Path) -> None:
    rig = Rig(tmp_path, pipeline_kw={"edge_ok": False, "n_eff": 900.0})
    assert outcomes(rig.learner().run(REFIT_DAY))["L1"] == "skipped: insufficient_samples"


def test_an_untunable_challenger_is_registered_and_flagged(tmp_path: Path) -> None:
    rig = Rig(tmp_path, pipeline_kw={"untunable": True})
    doc = rig.learner().run(REFIT_DAY)
    assert doc["steps"]["L2"]["details"]["final_policy_untunable"] is True
    assert rig.registry.get("challenger").manifest["metrics"]["final_policy"] == "untunable"


# ----------------------------------------------------------------------------------- failures and restarts


def test_a_failing_step_is_recorded_and_the_champion_stays(rig: Rig) -> None:
    rig.pipeline.fail_on = "train"
    doc = rig.learner().run(REFIT_DAY)
    o = outcomes(doc)
    assert o["L3"] == "failed" and "train exploded" in doc["steps"]["L3"]["message"]
    assert o["L1"] == o["L2"] == o["L5"] == "skipped: upstream_failed"
    assert doc["champion_after"] == rig.champion and doc["challenger_after"] is None
    assert rig.registry.versions() == [rig.champion]


def test_a_failing_step_does_not_stop_the_independent_ones(rig: Rig) -> None:
    class Boom:
        def sample(self, day: date) -> CostSample:
            raise OSError("ledger locked")

    rig.pipeline.fail_on = "build"
    doc = rig.learner(cost_source=Boom()).run(REFIT_DAY)
    assert outcomes(doc)["L4"] == "failed" and outcomes(doc)["L3"] == "failed"
    assert "L0" in doc["steps"]


def test_a_system_exit_inside_a_step_is_a_failure_not_an_exit(rig: Rig) -> None:
    class Exits(FakePipeline):
        def build_dataset(self) -> Path:
            raise SystemExit(2)

    rig.pipeline = Exits(rig.tmp / "work")
    doc = rig.learner().run(REFIT_DAY)
    assert doc["steps"]["L3"]["outcome"] == "failed"


def test_a_rerun_of_the_same_date_changes_nothing(rig: Rig) -> None:
    first = rig.learner().run(REFIT_DAY)
    calls = list(rig.pipeline.calls)
    versions = rig.registry.versions()
    second = rig.learner().run(REFIT_DAY)
    assert rig.pipeline.calls == calls and rig.registry.versions() == versions
    assert {k: v["started_at"] for k, v in second["steps"].items()} == {
        k: v["started_at"] for k, v in first["steps"].items()
    }
    assert outcomes(second) == outcomes(first)


def test_a_restart_resumes_inside_l2_without_retraining(rig: Rig) -> None:
    rig.pipeline.fail_on = "evaluate"
    assert rig.learner().run(REFIT_DAY)["steps"]["L2"]["outcome"] == "failed"
    assert rig.doc()["progress"]["tuned"] is True
    rig.pipeline.fail_on = None
    doc = rig.learner().run(REFIT_DAY)
    assert doc["steps"]["L2"]["outcome"] == "ok"
    assert rig.pipeline.calls.count("build") == 1 and rig.pipeline.calls.count("train") == 1
    assert rig.pipeline.calls.count("tune") == 1 and rig.pipeline.calls.count("evaluate") == 2


def test_a_crash_after_the_registration_does_not_register_twice(rig: Rig) -> None:
    rig.learner().run(REFIT_DAY)
    doc = rig.doc()
    del doc["steps"]["L2"]  # as if the process died before it could record the step
    (rig.paths.reports_dir / f"{REFIT_DAY.isoformat()}.json").write_text(json.dumps(doc), encoding="utf-8")
    again = rig.learner().run(REFIT_DAY)
    assert again["steps"]["L2"]["outcome"] == "ok"
    assert rig.registry.versions().count("baseline-lgbm@2026-11-01.1") == 1


def test_force_redoes_the_work(rig: Rig) -> None:
    rig.learner().run(REFIT_DAY)
    rig.learner().run(REFIT_DAY, force=True)
    assert rig.pipeline.calls.count("build") == 2


def test_the_output_is_the_d11_document(rig: Rig) -> None:
    doc = rig.learner().run(REFIT_DAY)
    assert doc["v"] == "chronos.learner.v1" and doc["date"] == "2026-11-01" and doc["audited_day"] == "2026-10-31"
    assert list(doc["steps"]) == ["L0", "L4", "L3", "L1", "L2", "L5"]
    for step in doc["steps"].values():
        assert {"started_at", "finished_at", "outcome", "message", "artifacts", "details"} <= set(step)
    assert not list(rig.paths.reports_dir.glob("*.tmp"))  # atomic write leaves no temporary file


def test_the_label_config_is_never_written(rig: Rig) -> None:
    rig.paths.label_config.write_text('{"k_tp": 3}', encoding="utf-8")
    before = rig.paths.label_config.read_bytes()
    rig.learner().run(REFIT_DAY)
    assert rig.paths.label_config.read_bytes() == before


# --------------------------------------------------------------------------------------- only past data


def write_labels(rig: Rig, rows: list[dict[str, Any]]) -> None:
    rig.paths.live_labels_dir.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(r) + "\n" for r in rows)
    (rig.paths.live_labels_dir / "2026-10-01.jsonl").write_text(text, encoding="utf-8")


def label_row(t: str, edge: float = 0.5, target: str = "true") -> dict[str, Any]:
    return {
        "question_key": "edge", "split": "live", "product": "SOL-EUR", "t": t, "target": target,
        "meta": {"fill_minute": 5, "exit_minute": 600}, "predicted": {"edge": edge},
    }  # fmt: skip


def test_live_labels_whose_window_ends_after_the_cutoff_are_not_used(rig: Rig) -> None:
    # H = 15 days: a decision of 2026-10-15 ends at about 2026-10-30 (past), one of 2026-10-20 at about 2026-11-04
    write_labels(rig, [label_row("2026-10-10T10:00:00Z"), label_row("2026-10-15T10:00:00Z"),
                       label_row("2026-10-20T10:00:00Z"), label_row("2026-10-31T23:00:00Z")])  # fmt: skip
    learner = rig.learner()
    learner.run(REFIT_DAY)
    info = rig.doc()["steps"]["L3"]["details"]["live_labels"]
    assert (info["rows"], info["used"], info["excluded_future"]) == (4, 2, 2)
    assert info["n_eff"] > 0 and info["n_scored"] == 2
    assert info["ece"] is not None


def test_a_label_that_ends_exactly_at_the_cutoff_counts(rig: Rig) -> None:
    from training.labeling import label_window_end

    end = pd.Timestamp("2026-11-01 00:00:00")
    # find the decision instant whose window ends exactly at the cutoff
    t = end - (label_window_end(pd.DatetimeIndex([end]), LABEL.cfg)[0] - end)
    assert label_window_end(pd.DatetimeIndex([t]), LABEL.cfg)[0] == end
    write_labels(
        rig,
        [
            label_row(t.strftime("%Y-%m-%dT%H:%M:%SZ")),
            label_row((t + pd.Timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")),
        ],
    )
    rig.learner().run(REFIT_DAY)
    info = rig.doc()["steps"]["L3"]["details"]["live_labels"]
    assert (info["used"], info["excluded_future"]) == (1, 1)


def test_the_live_weight_is_declared_not_applied(rig: Rig) -> None:
    rig.learner().run(REFIT_DAY)
    d = rig.doc()["steps"]["L3"]["details"]
    assert d["live_weight"] == 2.0 and d["live_weight_applied"] is False


def store_with_bars(root: Path, last: datetime) -> None:
    idx = pd.date_range(last - timedelta(minutes=9), last, freq="1min", tz="UTC")
    df = pd.DataFrame({"ts": idx, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0})
    BarStore(root).write("SOL-EUR", df)


def test_a_history_that_runs_past_the_cutoff_is_refused(rig: Rig) -> None:
    store_with_bars(rig.paths.bars_root, datetime(2026, 11, 3, 0, 5, tzinfo=UTC))  # cutoff 2026-11-01 + 1 day < this
    doc = rig.learner().run(REFIT_DAY)
    assert doc["steps"]["L3"]["outcome"] == "skipped: history_beyond_cutoff"
    assert rig.pipeline.calls == []


def test_the_history_of_a_real_night_is_accepted(rig: Rig) -> None:
    store_with_bars(rig.paths.bars_root, datetime(2026, 11, 1, 0, 14, tzinfo=UTC))
    assert rig.learner().run(REFIT_DAY)["steps"]["L3"]["outcome"] == "ok"


# ------------------------------------------------------------------------------------------------- L4


def test_l4_writes_the_cost_table_and_the_latency_only(rig: Rig) -> None:
    class Source:
        def sample(self, day: date) -> CostSample:
            return CostSample(
                rtt_ms=(100.0, 200.0, 300.0, 400.0), spread_bps={"SOL-EUR": {3: (2.0, 4.0, 6.0)}},
                maker_entries=4, maker_filled=3, fees={"maker_bps": 25, "taker_bps": 50, "source": "account"},
            )  # fmt: skip

    rig.paths.fill_model_path.write_text(json.dumps(FillModelParams(queue_haircut=0.4).model_dump()), encoding="utf-8")
    doc = rig.learner(cost_source=Source()).run(date(2026, 11, 2))
    d = doc["steps"]["L4"]["details"]
    assert d["latency_ms_p75"] == pytest.approx(325.0)
    assert d["maker_fill_rate"] == {"rate": 0.75, "entries": 4, "filled": 3}
    assert d["spread_median_bps_by_hour"] == {"SOL-EUR": {"3": 4.0}}
    fm = FillModelParams.from_json(rig.paths.fill_model_path)  # still loadable: no foreign key
    assert fm.latency_ms == pytest.approx(325.0) and fm.queue_haircut == 0.4
    table = json.loads(rig.paths.fill_model_path.with_name("cost_table.json").read_text("utf-8"))
    assert table["audited_day"] == "2026-11-01" and table["fees"]["source"] == "account"


def test_l4_without_samples_is_skipped_and_touches_nothing(rig: Rig) -> None:
    class Empty:
        def sample(self, day: date) -> CostSample:
            return CostSample()

    doc = rig.learner(cost_source=Empty()).run(date(2026, 11, 2))
    assert doc["steps"]["L4"]["outcome"] == "skipped: insufficient_samples"
    assert not rig.paths.fill_model_path.exists()


def test_l4_fill_rate_alone_leaves_fill_model_json_alone(rig: Rig) -> None:
    class Rate:
        def sample(self, day: date) -> CostSample:
            return CostSample(maker_entries=2, maker_filled=1)

    rig.learner(cost_source=Rate()).run(date(2026, 11, 2))
    assert not rig.paths.fill_model_path.exists()
    assert rig.paths.fill_model_path.with_name("cost_table.json").exists()


def test_ledger_cost_source_counts_maker_entries_that_ended(tmp_path: Path) -> None:
    db = tmp_path / "dry.db"
    con = sqlite3.connect(db)
    con.executescript(LEDGER_SCHEMA)
    day = "2026-10-31T10:00:00+00:00"

    def order(cid: str, status: str, post_only: bool, leg: str = "entry", created: str = day) -> None:
        con.execute(
            "INSERT INTO orders (client_order_id, book, leg, side, status, payload, created_at) VALUES (?,?,?,?,?,?,?)",
            (cid, "champion", leg, "BUY", status, json.dumps({"post_only": post_only}), created),
        )

    order("a", "FILLED", True)
    order("b", "CANCELLED", True)
    order("c", "CANCELLED", True)  # cancelled with a partial fill: counts as filled
    order("d", "NEW", True)  # still open: not a terminal outcome yet
    order("e", "FILLED", False)  # taker: not a maker entry
    order("f", "FILLED", True, leg="bracket")
    order("g", "FILLED", True, created="2026-10-30T10:00:00+00:00")  # another day
    for cid in ("a", "c"):
        con.execute("INSERT INTO fills (trade_id, book, client_order_id) VALUES (?,?,?)", (f"t-{cid}", "champion", cid))
    con.commit()
    con.close()
    sample = LedgerCostSource(db).sample(date(2026, 10, 31))
    assert (sample.maker_entries, sample.maker_filled) == (3, 2)
    assert LedgerCostSource(tmp_path / "missing.db").sample(date(2026, 10, 31)) == CostSample()


# ------------------------------------------------------------------------------------------------- L0


def day_store(tmp_path: Path) -> tuple[Path, datetime]:
    root = tmp_path / "bars"
    bars = synthetic_bars(days=2, seed=3, zero_volume_day=None, constant_days=None)
    df = bars.rename_axis("ts").reset_index()
    df["ts"] = df["ts"].astype("datetime64[ns, UTC]")  # the dtype of candles_to_frame, hence of the real store
    BarStore(root).write("SOL-EUR", df)
    return root, df["ts"].iloc[0].to_pydatetime()


class Perturbed:
    """The store seen through REST: every 144th minute has a revised close (10 a day), every 288th+1 is missing (5)."""

    def __init__(self, root: Path, changed: bool = False, dropped: bool = False) -> None:
        self.inner, self.changed, self.dropped = StoreCandleSource(root), changed, dropped

    def fetch(self, product_id: str, start_s: int, end_s: int) -> list[dict[str, Any]]:
        out = []
        for r in self.inner.fetch(product_id, start_s, end_s):
            minute = int(r["start"]) // 60
            if self.dropped and minute % 288 == 1:
                continue
            if self.changed and minute % 144 == 0:
                r["close"] = repr(float(r["close"]) + 0.5)
            out.append(r)
        return out


def test_l0_audits_the_rest_revisions_of_the_previous_day(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.paths = LearnerPaths(**{**rig.paths.__dict__, "bars_root": day_store(tmp_path)[0]})
    run_day = date(2024, 1, 2)  # audits 2024-01-01
    clean = rig.learner(candle_source=Perturbed(rig.paths.bars_root)).run(run_day)
    audit = clean["steps"]["L0"]["details"]["rest_revisions"]
    assert audit["products"]["SOL-EUR"]["revised"] == 0 and audit["rest_revision_ratio_max"] == 0.0
    assert audit["products"]["SOL-EUR"]["minutes"] == 1440
    dirty = rig.learner(candle_source=Perturbed(rig.paths.bars_root, changed=True, dropped=True)).run(
        run_day, force=True
    )
    p = dirty["steps"]["L0"]["details"]["rest_revisions"]["products"]["SOL-EUR"]
    assert p["revised"] == 15 and p["ratio"] == pytest.approx(15 / 1440)
    assert p["max_close_diff_bps"] > 0


def test_l0_never_modifies_the_store(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    root = day_store(tmp_path)[0]
    rig.paths = LearnerPaths(**{**rig.paths.__dict__, "bars_root": root})
    before = {p: p.read_bytes() for p in root.rglob("*.parquet")}
    rig.learner(candle_source=Perturbed(root, changed=True)).run(date(2024, 1, 2))
    assert {p: p.read_bytes() for p in root.rglob("*.parquet")} == before


def test_l0_with_nothing_to_audit_is_skipped(rig: Rig) -> None:
    assert rig.learner().run(date(2026, 11, 2))["steps"]["L0"]["outcome"] == "skipped: insufficient_samples"


def test_psi_of_the_same_distribution_is_near_zero_and_a_shift_is_large() -> None:
    rng = np.random.default_rng(0)
    hist = rng.normal(0, 1, 5000)
    assert population_stability_index(rng.normal(0, 1, 1000), hist) == pytest.approx(0.0, abs=0.05)
    assert population_stability_index(rng.normal(1.5, 1, 1000), hist) > 0.25


def test_psi_needs_samples_and_a_history_with_spread() -> None:
    rng = np.random.default_rng(1)
    hist = rng.normal(0, 1, 500)
    assert population_stability_index(rng.normal(0, 1, 99), hist) is None
    assert population_stability_index(rng.normal(0, 1, 100), hist) is not None
    assert population_stability_index(rng.normal(0, 1, 500), np.full(500, 3.0)) is None
    assert population_stability_index(np.full(500, np.nan), hist) is None


def test_psi_is_symmetric_in_sign_of_the_shift() -> None:
    rng = np.random.default_rng(2)
    hist = rng.normal(0, 1, 5000)
    up = population_stability_index(rng.normal(1, 1, 2000), hist)
    down = population_stability_index(rng.normal(-1, 1, 2000), hist)
    assert up is not None and down is not None and up > 0.1 and down > 0.1


def test_feature_parity_counts_rows_and_names_the_worst_feature() -> None:
    a = np.full((4, len(FLAT_COLUMNS)), 1.0)
    a[:, 2] = np.nan
    b = a.copy()
    assert feature_parity(a, b)["match"] == 1.0
    b[1, 5] = 9.0
    b[3, 5] = 9.0
    b[3, 7] = 9.0
    out = feature_parity(a, b)
    assert out["match"] == 0.5 and out["worst_feature"] == FLAT_COLUMNS[5]
    assert out["mismatch_by_feature"] == {FLAT_COLUMNS[5]: 2, FLAT_COLUMNS[7]: 1}
    b2 = a.copy()
    b2[0, 2] = 0.0  # NaN against a number is a mismatch
    assert feature_parity(a, b2)["match"] == 0.75
    assert feature_parity(a[:0], b[:0])["match"] is None
    with pytest.raises(ValueError):
        feature_parity(a, b[:2])


def live_state_records(product: str, bars: pd.DataFrame, ind: pd.DataFrame, btc: pd.DataFrame | None,
                       rows: range, tamper: bool = False) -> list[dict[str, Any]]:  # fmt: skip
    builder = StateBuilder(LABEL)
    out = []
    for i in rows:

        def snap(p: str, frame: pd.DataFrame, i: int = i) -> IndicatorSnapshot:
            row = dict(zip(frame.columns, frame.to_numpy(dtype=np.float64)[i], strict=True))
            return IndicatorSnapshot.from_row(p, frame.index[i].to_pydatetime(), row)

        state = builder.build(
            snapshot=snap(product, ind), btc=snap("BTC-EUR", btc) if btc is not None else None, sentiment=None,
            position=PositionInput.flat(), close_t=float(bars["close"].iloc[i]),
        )  # fmt: skip
        text = serialize_state(state)
        if tamper:
            doc = json.loads(text)
            key = next(k for k, v in doc["momentum"].items() if isinstance(v, int | float) and not isinstance(v, bool))
            doc["momentum"][key] = doc["momentum"][key] + 7
            text = json.dumps(doc)
        t = (ind.index[i] + pd.Timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        out.append({"decision_id": f"d{product}{i}", "ts_bar_close": t, "book": "champion", "product": product,
                    "state": text})  # fmt: skip
    return out


def parity_rig(tmp_path: Path, tamper: bool) -> tuple[Rig, dict[str, Any]]:
    from training.backtest import build_market

    sol = synthetic_bars(days=25, seed=41, zero_volume_day=None, constant_days=None)
    btc = synthetic_bars(days=25, seed=42, zero_volume_day=None, constant_days=None)
    m_btc = build_market("BTC-EUR", btc, LABEL)
    m_sol = build_market("SOL-EUR", sol, LABEL, m_btc.ind)
    audited = date(2024, 1, 20)
    first = (datetime(2024, 1, 20, tzinfo=UTC) - sol.index[0].to_pydatetime()).days * 1440
    rows = range(first + 30, first + 1430, 5)  # 280 states of the audited day
    recs = live_state_records("SOL-EUR", sol, m_sol.ind, m_btc.ind, rows, tamper)
    recs += live_state_records("BTC-EUR", btc, m_btc.ind, None, rows)
    rig = Rig(tmp_path)
    rig.paths.decisions_dir.mkdir(parents=True)
    path = rig.paths.decisions_dir / f"{audited.isoformat()}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    markets = {"SOL-EUR": m_sol, "BTC-EUR": m_btc}
    doc = rig.learner(markets=lambda: markets, products=["SOL-EUR", "BTC-EUR"]).run(date(2024, 1, 21))
    return rig, doc


def test_l0_state_parity_matches_the_batch_path_on_real_states(tmp_path: Path) -> None:
    _, doc = parity_rig(tmp_path, tamper=False)
    p = doc["steps"]["L0"]["details"]["state_parity"]
    assert doc["steps"]["L0"]["outcome"] == "ok"
    assert p["state_parity"]["rows"] == 560 and p["state_parity"]["match"] == 1.0
    assert p["unmatched_rows"] == 0
    assert p["max_feature_psi"] is not None and p["worst_feature"] in FLAT_COLUMNS
    assert set(p["psi"]) == set(FLAT_COLUMNS)


def test_l0_state_parity_catches_a_state_that_differs(tmp_path: Path) -> None:
    _, doc = parity_rig(tmp_path, tamper=True)
    p = doc["steps"]["L0"]["details"]["state_parity"]["state_parity"]
    assert p["match"] == pytest.approx(0.5)  # every SOL state is off, every BTC state is right
    assert p["worst_feature"] is not None


def test_l0_reports_drift_alerts_over_the_threshold(tmp_path: Path) -> None:
    _, doc = parity_rig(tmp_path, tamper=False)
    details = doc["steps"]["L0"]["details"]["state_parity"]
    over = [a["feature"] for a in details["drift_alerts"]]
    assert all(details["psi"][f] > 0.25 for f in over)
    assert all(v <= 0.25 for k, v in details["psi"].items() if v is not None and k not in over)


# ------------------------------------------------------------------------- shadow evidence and pipeline


def test_shadow_evidence_counts_the_days_of_the_version_in_the_challenger_book(tmp_path: Path) -> None:
    dec, rep = tmp_path / "decisions", tmp_path / "dry"
    dec.mkdir()
    rep.mkdir()

    def rec(book: str, version: str) -> str:
        return json.dumps({"book": book, "policy": {"policy_version": version}}) + "\n"

    (dec / "2026-10-20.jsonl").write_text(rec("challenger", "v2") + rec("champion", "v1"), encoding="utf-8")
    (dec / "2026-10-21.jsonl").write_text(rec("challenger", "v2"), encoding="utf-8")
    (dec / "2026-10-22.jsonl").write_text(rec("challenger", "v1") + rec("champion", "v2"), encoding="utf-8")
    (dec / "2026-11-05.jsonl").write_text(rec("challenger", "v2"), encoding="utf-8")  # after the cutoff
    (rep / "2026-10-20.json").write_text(json.dumps({"anomalies": {"operational": 1, "parity": 0}}), encoding="utf-8")
    (rep / "2026-10-21.json").write_text(json.dumps({"anomalies": {"operational": 0, "parity": 2}}), encoding="utf-8")
    got = ReportShadowEvidence(dec, rep)("v2", datetime(2026, 11, 1, tzinfo=UTC))
    assert got == ShadowRecord(2.0, 1, 2)


def test_training_pipeline_chains_the_offline_tools_and_reads_their_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    datasets, models = tmp_path / "datasets", tmp_path / "models"
    cfg = tmp_path / "train.yml"
    cfg.write_text(f"datasets_dir: {datasets.as_posix()}\nmodels_dir: {models.as_posix()}\n", encoding="utf-8")
    seen: dict[str, list[str]] = {}

    def fake_build(argv: list[str]) -> int:
        seen["build"] = argv
        (datasets / "run-1").mkdir(parents=True)
        return 0

    def fake_train(argv: list[str]) -> int:
        seen["train"] = argv
        (models / "m-1").mkdir(parents=True)
        return 0

    def fake_tune(argv: list[str]) -> int:
        seen["tune"] = argv
        return 0

    def fake_evaluate(argv: list[str]) -> int:
        seen["evaluate"] = argv
        (models / "m-1" / "evaluation.json").write_text(json.dumps({"GE": {"status": "NO-GO"}, "G0": {"passed": True}}))
        return 1  # a NO-GO verdict exits 1: not a failure of the tool

    monkeypatch.setattr("training.build_dataset.main", fake_build)
    monkeypatch.setattr("training.baseline_lgbm.main", fake_train)
    monkeypatch.setattr("training.tune_policy.main", fake_tune)
    monkeypatch.setattr("training.evaluate.main", fake_evaluate)
    pipe = TrainingPipeline(cfg)
    run = pipe.build_dataset()
    model = pipe.train(run)
    pipe.tune(model, run)
    out = pipe.evaluate(model, run)
    assert (run.name, model.name) == ("run-1", "m-1")
    assert seen["train"][-2:] == ["--dataset", str(run)] and seen["tune"][-4:-2] == ["--model", str(model)]
    assert out == {"exit_code": 1, "status": "NO-GO", "gates": {"G0": True, "GE": "NO-GO"}}


def test_training_pipeline_failures_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "train.yml"
    cfg.write_text(f"datasets_dir: {(tmp_path / 'd').as_posix()}\nmodels_dir: {(tmp_path / 'm').as_posix()}\n", "utf-8")
    monkeypatch.setattr("training.build_dataset.main", lambda argv: 1)
    monkeypatch.setattr("training.tune_policy.main", lambda argv: 2)
    monkeypatch.setattr("training.evaluate.main", lambda argv: 1)  # exits 1 and writes nothing
    pipe = TrainingPipeline(cfg)
    with pytest.raises(RuntimeError, match="build_dataset exited with code 1"):
        pipe.build_dataset()
    with pytest.raises(RuntimeError, match="tune_policy exited with code 2"):
        pipe.tune(tmp_path, tmp_path)
    model = tmp_path / "model"
    model.mkdir()
    with pytest.raises(RuntimeError, match="no evaluation.json"):
        pipe.evaluate(model, tmp_path)
    monkeypatch.setattr("training.baseline_lgbm.main", lambda argv: (_ for _ in ()).throw(SystemExit(2)))
    with pytest.raises(RuntimeError, match="baseline_lgbm exited with code 2"):
        pipe.train(tmp_path)


def test_a_stale_evaluation_json_is_not_a_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "train.yml"
    cfg.write_text("datasets_dir: d\nmodels_dir: m\n", encoding="utf-8")
    model = tmp_path / "model"
    model.mkdir()
    (model / "evaluation.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr("training.evaluate.main", lambda argv: 3)
    with pytest.raises(RuntimeError, match="no evaluation.json"):
        TrainingPipeline(cfg).evaluate(model, tmp_path)


# -------------------------------------------------------------------------------------------- CLI / misc


def test_the_cli_reports_a_configuration_error(tmp_path: Path) -> None:
    code = learner_module.main(
        ["--once", "--config", str(tmp_path / "none.yml"), "--train-config", str(tmp_path / "x")]
    )
    assert code == 2


def test_seconds_until_the_next_run() -> None:
    now = datetime(2026, 11, 1, 0, 10, tzinfo=UTC)
    assert learner_module._seconds_until(now, "00:15") == 300
    assert learner_module._seconds_until(now.replace(minute=20), "00:15") == 86400 - 300


def test_the_learner_source_audits_rest_revisions_and_never_names_the_champion_alias_in_a_call() -> None:
    import ast

    source = Path(learner_module.__file__).read_text(encoding="utf-8")
    assert "rest_revisions" in source
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"promote", "rollback"}
        ):
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            assert "CHAMPION" not in names, f"line {node.lineno}: a call to {node.func.attr} mentions CHAMPION"


def test_a_skipped_step_is_final_for_the_day_and_not_asked_again(rig: Rig) -> None:
    class Counting:
        calls = 0

        def sample(self, day: date) -> CostSample:
            Counting.calls += 1
            return CostSample()

    source = Counting()
    rig.learner(cost_source=source).run(date(2026, 11, 2))
    rig.learner(cost_source=source).run(date(2026, 11, 2))
    assert Counting.calls == 1


def test_psi_needs_enough_history_too() -> None:
    rng = np.random.default_rng(3)
    assert population_stability_index(rng.normal(0, 1, 500), rng.normal(0, 1, 99)) is None
