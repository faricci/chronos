"""Rule L5 inside the nightly learner (spec 5.1.6, resolution P1e part B): L5 judges the challenger that was in charge
BEFORE the night, on ONE common window, with the gates and the OOS artifacts of the registered versions; the learner
reports the verdict and never promotes. End to end (L0..L5) with a synthetic challenger that satisfies every condition,
and with each condition violated in turn."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from modules.config import GateEconConfig, LearnerConfig
from modules.dryrun import learner as learner_module
from modules.dryrun.learner import (
    HoldoutBacktester,
    ShadowRecord,
    _gate_flags,
    final_train_end,
    gate_summary,
    holdout_start,
)
from modules.dryrun.registry import (
    BacktestEvidence,
    OosEvidence,
    PromotionEvidence,
    PromotionRule,
    artifacts_from_dir,
    promote_if_eligible,
)
from tests.test_learner import NOW, REFIT_DAY, Rig, make_version, store_with_bars
from tests.test_oos_compare import SPANS, START, book, noisy, write_artifacts
from training.oos_compare import compare_models

D = Decimal
CUTOFF = datetime(2026, 11, 1, tzinfo=UTC)
NEW = "baseline-lgbm@2026-11-01.1"  # what the fake pipeline trains tonight
LAST_BAR = CUTOFF - timedelta(minutes=1)  # open of the last bar: it closes exactly at the cutoff
WINDOW_START = CUTOFF - timedelta(days=90)


# ------------------------------------------------------------------------------------------- the rig


def evaluation(ge: str = "GO", **gates: bool) -> dict[str, Any]:
    flags = {"G0": True, "G1": True, "G2": True, "G4": True} | gates
    doc: dict[str, Any] = {k: {"passed": v} for k, v in flags.items()}
    doc["G3"] = {"applicable": False, "note": "the candidate is the baseline"}
    doc["GE"] = {"status": ge}
    return doc


def register(
    rig: Rig, name: str, *, ge: str | None = "GO", train_end: str | None = "2026-06-10T00:00:00Z",
    holdout: str = "2026-06-20T00:00:00+00:00", oos: tuple[Any, list[Any]] | None = None, **gates: bool,
) -> str:  # fmt: skip
    """A registered version with the files rule L5 reads: model card (final_train_end, holdout), evaluation.json and,
    optionally, the OOS artifacts. Registered versions are immutable, so everything is written BEFORE registering."""
    model = make_version(rig.tmp / "extra", name)
    card = json.loads((model / "model_card.json").read_text(encoding="utf-8"))
    card["label_config"] = {"sha256": "abc", "selection": {"holdout_start": holdout}}
    card["data"]["final_train_end"] = train_end
    (model / "model_card.json").write_text(json.dumps(card), encoding="utf-8")
    if ge is not None:
        (model / "evaluation.json").write_text(json.dumps(evaluation(ge, **gates)), encoding="utf-8")
    if oos is not None:
        write_artifacts(model / "_oos", oos[0], oos[1], 16)
        for f in ("oos_trades.jsonl", "oos_spans.json"):
            (model / f).write_text((model / "_oos" / f).read_text(encoding="utf-8"), encoding="utf-8")
        for f in ("oos_trades.jsonl", "oos_spans.json"):
            (model / "_oos" / f).unlink()
        (model / "_oos").rmdir()
    return rig.registry.register(artifacts_from_dir(model), {"version": name})


class Backtests:
    """RecentBacktester stub: one evidence per model directory name; records the windows it was asked for."""

    def __init__(self, by_name: dict[str, tuple[int, str, float | None, float]]) -> None:
        self.by_name, self.windows = by_name, []  # name -> (n_trades, pnl, sharpe, dd)
        self.windows: list[tuple[str, datetime, datetime]]

    def __call__(self, model_dir: Path, start: datetime, end: datetime) -> BacktestEvidence:
        self.windows.append((model_dir.name, start, end))
        n, pnl, sharpe, dd = self.by_name[model_dir.name]
        return BacktestEvidence(start, end, (end - start).total_seconds() / 86400.0, n, D(pnl), sharpe, dd)


class OosStub:
    def __init__(self, ev: OosEvidence | None) -> None:
        self.ev, self.calls = ev, []
        self.calls: list[dict[str, Any]]

    def __call__(
        self, challenger_dir: Path, champion_dir: Path, *, champion_go: bool, quantile: float
    ) -> OosEvidence | None:
        names = {"challenger": challenger_dir.name, "champion": champion_dir.name}
        self.calls.append(names | {"go": champion_go, "q": quantile})
        return self.ev


GOOD_OOS = OosEvidence(600.0, 180, 0.03, 170, 12.0, 1.2, 0.8, 0.04)


class Scenario:
    """A previous challenger ``OLD`` and a champion ``CHAMP``; tonight the pipeline trains ``NEW`` (L2 moves the alias
    onto it). Defaults satisfy EVERY condition of rule L5; each test breaks one."""

    OLD, CHAMP = "chall-old@1", "champ@1"

    def __init__(self, tmp_path: Path, **kw: Any) -> None:
        self.rig = Rig(tmp_path, pipeline_kw=kw.pop("pipeline_kw", {}))
        champ_kw = kw.pop("champion", {})
        reg_kw = {"ge": "GO"} | kw.pop("challenger", {})
        self.rig.registry.promote(register(self.rig, self.CHAMP, **champ_kw), "champion")
        if kw.pop("with_challenger", True):
            self.rig.registry.promote(register(self.rig, self.OLD, **reg_kw), "challenger")
        last = kw.pop("last_bar", LAST_BAR)
        store_with_bars(self.rig.paths.bars_root, last)
        stats = {
            self.OLD: (40, "8", 1.4, 0.03), self.CHAMP: (35, "5", 1.0, 0.04),
        } | kw.pop("backtests", {})  # fmt: skip
        self.backtests = Backtests(stats)
        self.oos = OosStub(kw.pop("oos", GOOD_OOS))
        self.shadow = kw.pop("shadow", ShadowRecord(20.0, 0, 0))
        self.captured: list[PromotionEvidence] = []
        assert not kw, kw

    def run(self, monkeypatch: pytest.MonkeyPatch | None = None, **extra: Any) -> dict[str, Any]:
        if monkeypatch is not None:
            real = learner_module.decide_promotion

            def spy(evidence: PromotionEvidence, rule: PromotionRule | None = None) -> Any:
                self.captured.append(evidence)
                return real(evidence, rule)

            monkeypatch.setattr(learner_module, "decide_promotion", spy)
        args: dict[str, Any] = {
            "backtester": self.backtests, "shadow": lambda v, c: self.shadow, "oos": self.oos, "econ": GateEconConfig(),
            "cfg": LearnerConfig(), "clock": lambda: NOW,
        }  # fmt: skip
        return self.rig.learner(**(args | extra))


def l5(doc: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = doc["steps"]["L5"]
    return out


# ---------------------------------------------------------------------------------------------- L5 timing


def test_l5_evaluates_challenger_before_refit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The defect of the old order: L2 moves the alias onto the new version (0 shadow days) before L5 reads it."""
    s = Scenario(tmp_path)
    doc = s.run(monkeypatch).run(REFIT_DAY)
    assert doc["challenger_before"] == s.OLD and doc["challenger_after"] == NEW  # L2 moved the alias...
    assert l5(doc)["details"]["challenger"] == s.OLD  # ...and L5 still judged the one that was in charge before
    assert l5(doc)["details"]["verdict"] == "promote" and l5(doc)["details"]["promoted"] is False
    assert {w[0] for w in s.backtests.windows} == {s.OLD, s.CHAMP}  # never the new version
    assert s.captured[0].shadow_days == 20.0
    assert doc["champion_before"] == doc["champion_after"] == s.CHAMP  # the learner did not touch the champion


def test_l5_runs_when_refit_failed(tmp_path: Path) -> None:
    s = Scenario(tmp_path, pipeline_kw={"n_eff": 10.0})  # L1 skips for insufficient samples: L2 never runs
    doc = s.run().run(REFIT_DAY)
    assert doc["steps"]["L1"]["outcome"].startswith("skipped") and doc["steps"]["L2"]["outcome"].startswith("skipped")
    assert l5(doc)["outcome"] == "ok" and l5(doc)["details"]["verdict"] == "promote"
    broken = Scenario(tmp_path / "b")
    broken.rig.pipeline.fail_on = "train"
    doc = broken.run().run(REFIT_DAY)
    assert doc["steps"]["L3"]["outcome"] == "failed" and l5(doc)["outcome"] == "ok"
    assert doc["challenger_after"] == broken.OLD  # nothing was proposed tonight, the old one is still in charge


def test_l5_skips_without_previous_challenger(tmp_path: Path) -> None:
    s = Scenario(tmp_path, with_challenger=False)
    doc = s.run().run(REFIT_DAY)
    assert doc["challenger_before"] is None and doc["challenger_after"] == NEW
    assert l5(doc)["outcome"] == "skipped: no_previous_challenger"
    assert s.backtests.windows == []


def test_l5_skips_when_the_challenger_is_the_champion(tmp_path: Path) -> None:
    s = Scenario(tmp_path, with_challenger=False)
    s.rig.registry.promote(s.CHAMP, "challenger")
    doc = s.run().run(REFIT_DAY)
    assert l5(doc)["outcome"] == "skipped: challenger_is_champion"


def test_challenger_before_survives_a_rerun_and_force(tmp_path: Path) -> None:
    """On a rerun, or under --force, the alias already points at tonight's version: the saved value is what counts."""
    s = Scenario(tmp_path)
    s.run().run(REFIT_DAY)
    again = s.run().run(REFIT_DAY, force=True)
    assert again["challenger_before"] == s.OLD
    assert l5(again)["details"]["challenger"] == s.OLD and l5(again)["details"]["verdict"] == "promote"


def test_l5_only_on_the_refit_day(tmp_path: Path) -> None:
    s = Scenario(tmp_path)
    doc = s.run().run(REFIT_DAY + timedelta(days=1))
    assert l5(doc)["outcome"] == "skipped: not_refit_day" and s.backtests.windows == []


# ------------------------------------------------------------------------------------- the common window


def test_l5_common_window_from_both_holdouts_and_cutoff(tmp_path: Path) -> None:
    s = Scenario(tmp_path)
    doc = s.run().run(REFIT_DAY)
    (a, b) = s.backtests.windows
    assert a[1:] == b[1:] == (WINDOW_START, CUTOFF)  # one window for both models: last 90 days, ending at the cutoff
    assert l5(doc)["details"]["window"]["days"] == pytest.approx(90.0)
    # a later holdout of ONE of the two pushes the start of BOTH
    late = Scenario(tmp_path / "late", champion={"holdout": "2026-09-01T00:00:00+00:00"})
    late.run().run(REFIT_DAY)
    starts = {w[1] for w in late.backtests.windows}
    assert starts == {datetime(2026, 9, 1, tzinfo=UTC)}
    assert {w[2] for w in late.backtests.windows} == {CUTOFF}
    # the last bar of the products ends the window before the cutoff
    early = Scenario(tmp_path / "early", last_bar=datetime(2026, 10, 30, 12, 0, tzinfo=UTC))
    doc = early.run().run(REFIT_DAY)
    assert {w[2] for w in early.backtests.windows} == {datetime(2026, 10, 30, 12, 1, tzinfo=UTC)}
    assert l5(doc)["details"]["verdict"] == "promote"  # still 90 days: the window slid back with its end


def test_l5_window_shorter_than_the_rule_is_insufficient(tmp_path: Path) -> None:
    s = Scenario(tmp_path, challenger={"holdout": "2026-10-01T00:00:00+00:00"})  # only 31 days after the holdout
    doc = s.run().run(REFIT_DAY)
    d = l5(doc)["details"]
    assert d["verdict"] == "insufficient"
    assert "challenger_backtest_window_short" in d["reasons"] and "champion_backtest_window_short" in d["reasons"]


# ------------------------------------------------------------------------- end to end, one condition at a time

VIOLATIONS: list[tuple[str, dict[str, Any], str, str]] = [
    # id, scenario kwargs, verdict, a reason that must be listed
    ("ge_no_go", {"challenger": {"ge": "NO-GO"}}, "reject", "challenger_ge_nogo"),
    ("g4_failed", {"challenger": {"G4": False}}, "reject", "challenger_G4_failed"),
    ("g1_failed", {"challenger": {"G1": False}}, "reject", "challenger_G1_failed"),
    ("ge_inconclusive", {"challenger": {"ge": "INCONCLUSO"}}, "insufficient", "challenger_ge_inconclusive"),
    ("evaluation_missing", {"challenger": {"ge": None}}, "insufficient", "challenger_evaluation_missing"),
    ("oos_not_provable", {"challenger": {"train_end": "2026-09-30T00:00:00Z"}}, "insufficient", "oos_not_provable"),
    ("final_train_end_unknown", {"challenger": {"train_end": None}}, "insufficient", "oos_not_provable"),
    ("champion_trained_on_window", {"champion": {"train_end": "2026-10-20T00:00:00Z"}}, "insufficient",
     "oos_not_provable"),  # fmt: skip
    ("too_few_trades", {"backtests": {"chall-old@1": (29, "8", 1.4, 0.03)}}, "insufficient",
     "challenger_recent_trades_short"),  # fmt: skip
    ("pnl_not_positive", {"backtests": {"chall-old@1": (40, "-1", 1.4, 0.03), "champ@1": (35, "-3", 1.0, 0.04)}},
     "reject", "recent_pnl_not_positive"),  # fmt: skip
    ("pnl_not_better", {"backtests": {"chall-old@1": (40, "5", 1.4, 0.03)}}, "reject", "recent_pnl_not_better"),
    ("sharpe_not_better", {"backtests": {"chall-old@1": (40, "8", 1.0, 0.03)}}, "reject", "recent_sharpe_not_better"),
    ("recent_dd_worse", {"backtests": {"chall-old@1": (40, "8", 1.4, 0.045)}}, "reject", "recent_max_dd_worse"),
    ("recent_dd_above_limit", {"backtests": {"chall-old@1": (40, "8", 1.4, 0.06), "champ@1": (35, "5", 1.0, 0.07)}},
     "reject", "recent_max_dd_above_limit"),  # fmt: skip
    ("shadow_short", {"shadow": ShadowRecord(13.0, 0, 0)}, "insufficient", "shadow_days_short"),
    ("operational_anomaly", {"shadow": ShadowRecord(20.0, 1, 0)}, "reject", "operational_anomalies"),
    ("parity_anomaly", {"shadow": ShadowRecord(20.0, 0, 3)}, "reject", "parity_anomalies"),
    ("oos_missing", {"oos": None}, "insufficient", "oos_unavailable"),
    ("paired_quantile_not_positive", {"oos": OosEvidence(600.0, 180, 0.03, 170, 0.0, 1.2, 0.8, 0.04)}, "reject",
     "oos_pnl_difference_not_significant"),  # fmt: skip
    ("oos_sharpe_worse", {"oos": OosEvidence(600.0, 180, 0.03, 170, 12.0, 0.7, 0.8, 0.04)}, "reject",
     "oos_sharpe_not_better"),  # fmt: skip
    ("oos_dd_worse", {"oos": OosEvidence(600.0, 180, 0.045, 170, 12.0, 1.2, 0.8, 0.04)}, "reject", "oos_max_dd_worse"),
    ("oos_dd_above_limit", {"oos": OosEvidence(600.0, 180, 0.06, 170, 12.0, 1.2, 0.8, 0.07)}, "reject",
     "oos_max_dd_above_limit"),  # fmt: skip
]


def test_synthetic_challenger_satisfying_every_condition_is_promoted_only_by_promote_if_eligible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = Scenario(tmp_path)
    doc = s.run(monkeypatch).run(REFIT_DAY)
    assert [doc["steps"][k]["outcome"] for k in ("L0", "L4", "L3", "L1", "L2", "L5")][2:] == ["ok"] * 4
    d = l5(doc)["details"]
    assert (d["verdict"], d["reasons"], d["promoted"]) == ("promote", [], False)
    assert s.oos.calls == [{"challenger": s.OLD, "champion": s.CHAMP, "go": True, "q": 0.05}]
    assert s.rig.registry.get("champion").version == s.CHAMP  # the learner approved nothing by itself
    assert d["command"].startswith(f"python -m modules.dryrun.registry promote {s.OLD}")
    # only the explicit local call, with the very evidence the learner judged, moves the champion
    (evidence,) = s.captured
    assert promote_if_eligible(s.rig.registry, s.OLD, evidence, PromotionRule()).approved
    assert s.rig.registry.get("champion").version == s.OLD
    assert s.rig.registry.rollback("champion", reason="test") == s.CHAMP


@pytest.mark.parametrize(("kw", "verdict", "reason"), [v[1:] for v in VIOLATIONS], ids=[v[0] for v in VIOLATIONS])
def test_each_violated_condition_blocks_the_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kw: dict[str, Any], verdict: str, reason: str
) -> None:
    s = Scenario(tmp_path, **kw)
    doc = s.run(monkeypatch).run(REFIT_DAY)
    d = l5(doc)["details"]
    assert l5(doc)["outcome"] == "ok" and (d["verdict"], reason in d["reasons"]) == (verdict, True), d["reasons"]
    assert d["promoted"] is False and s.rig.registry.get("champion").version == s.CHAMP
    (evidence,) = s.captured
    assert not promote_if_eligible(s.rig.registry, s.OLD, evidence, PromotionRule()).approved
    assert s.rig.registry.get("champion").version == s.CHAMP


def test_p2_does_not_apply_to_a_champion_that_is_not_go(tmp_path: Path) -> None:
    weak = OosEvidence(600.0, 180, 0.03, None, None, 1.2, None, None)
    s = Scenario(tmp_path, champion={"ge": "NO-GO"}, oos=weak)
    d = l5(s.run().run(REFIT_DAY))["details"]
    assert d["verdict"] == "promote" and s.oos.calls[0]["go"] is False
    nogate = Scenario(tmp_path / "x", champion={"ge": None})  # a champion without evaluation.json is not GO either
    assert l5(nogate.run().run(REFIT_DAY))["details"]["verdict"] == "promote"


def test_l5_with_real_oos_artifacts_of_the_registered_versions(tmp_path: Path) -> None:
    """The paired comparison reads the files that ``evaluate`` wrote into the registered versions."""
    good = (book(noisy(21, mean=6.0)), SPANS)
    bad = (book(noisy(22, mean=-1.0)), SPANS)

    def comparator(ch: Path, cp: Path, *, champion_go: bool, quantile: float) -> OosEvidence | None:
        return compare_models(
            ch, cp, champion_go=champion_go, reps=2000, seed=1234, q=quantile, starting_quote=START
        )  # fmt: skip

    s = Scenario(tmp_path, challenger={"oos": good}, champion={"oos": bad}, oos=GOOD_OOS)
    d = l5(s.run(oos=comparator).run(REFIT_DAY))["details"]
    assert d["verdict"] == "promote", d["reasons"]
    same = Scenario(tmp_path / "same", challenger={"oos": good}, champion={"oos": good}, oos=GOOD_OOS)
    d = l5(same.run(oos=comparator).run(REFIT_DAY))["details"]
    assert d["verdict"] == "reject" and "oos_pnl_difference_not_significant" in d["reasons"]  # "no better" is a no
    worse = Scenario(tmp_path / "worse", challenger={"oos": bad}, champion={"oos": good}, oos=GOOD_OOS)
    d = l5(worse.run(oos=comparator).run(REFIT_DAY))["details"]
    assert d["verdict"] == "reject" and "oos_pnl_difference_not_significant" in d["reasons"]
    old = Scenario(tmp_path / "old", oos=GOOD_OOS)  # versions evaluated before the artifacts existed
    d = l5(old.run(oos=comparator).run(REFIT_DAY))["details"]
    assert d["verdict"] == "insufficient" and d["reasons"] == ["oos_unavailable"]


def test_the_current_untunable_models_are_never_promoted(tmp_path: Path) -> None:
    """Today every subset is untunable: no trades, GE is not GO. L5 answers, never ``promote``."""
    idle = (0, "0", 0.0, 0.0)
    s = Scenario(
        tmp_path, challenger={"ge": "NO-GO"}, champion={"ge": None}, oos=None,
        backtests={Scenario.OLD: idle, Scenario.CHAMP: idle},
    )  # fmt: skip
    d = l5(s.run().run(REFIT_DAY))["details"]
    assert d["verdict"] in ("reject", "insufficient") and d["verdict"] != "promote"
    assert "challenger_ge_nogo" in d["reasons"] and "challenger_recent_trades_short" in d["reasons"]
    assert s.rig.registry.get("champion").version == s.CHAMP


# ------------------------------------------------------------------------------- the pieces of the learner


def test_holdout_backtester_untunable_zero_trades(tmp_path: Path) -> None:
    model = tmp_path / "v"
    (model / "final").mkdir(parents=True)
    (model / "final" / "policy.json").write_text(json.dumps({"untunable": True, "reason": "untunable_ev"}))
    bt = HoldoutBacktester(tmp_path / "train.yml")  # never read: an untunable model does not trade
    start, end = datetime(2026, 8, 3, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC)
    got = bt(model, start, end)
    assert got == BacktestEvidence(start, end, 90.0, 0, D(0), 0.0, 0.0)  # defined metrics, not "Sharpe None"


def test_gate_flags_include_g3_g4() -> None:
    gates = evaluation("GO", G4=True)
    flags = _gate_flags(gates)
    assert set(flags) == {"G0", "G1", "G2", "G3", "G4", "GE"}
    assert flags["G4"] is True and flags["GE"] == "GO" and flags["G3"] is None  # G3: not an LLM, nothing to pass
    assert _gate_flags(evaluation("NO-GO", G4=False))["G4"] is False
    assert _gate_flags({"G0": {"passed": True}}) == {"G0": True}


def test_gate_summary_reads_the_registered_evaluation(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    register(rig, "v-go")
    register(rig, "v-llm", ge="INCONCLUSO", G4=False)
    register(rig, "v-none", ge=None)
    path = lambda name: rig.registry.get_version(name).path  # noqa: E731
    go = gate_summary(path("v-go"))
    assert go.ge == "GO" and dict(go.passed) == {"G0": True, "G1": True, "G2": True, "G4": True}
    assert gate_summary(path("v-llm")).ge == "INCONCLUSO" and gate_summary(path("v-llm")).passed["G4"] is False
    none = gate_summary(path("v-none"))
    assert none.ge is None and set(none.passed) == {"G0", "G1", "G2", "G4"} and not any(none.passed.values())
    # an LLM backend reports G3: it becomes required
    d = tmp_path / "llm"
    d.mkdir()
    (d / "evaluation.json").write_text(
        json.dumps(evaluation("GO") | {"G3": {"applicable": True, "passed": True}}), encoding="utf-8")
    assert gate_summary(d).passed["G3"] is True
    (d / "evaluation.json").write_text("[]", encoding="utf-8")
    assert gate_summary(d).ge is None


def test_card_readers(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    register(rig, "v1", train_end="2026-06-10T00:00:00Z", holdout="2026-06-20T00:00:00+00:00")
    register(rig, "v2", train_end=None, holdout="2026-06-20T00:00:00")  # naive text is UTC
    p1, p2 = (rig.registry.get_version(n).path for n in ("v1", "v2"))
    assert final_train_end(p1) == datetime(2026, 6, 10, tzinfo=UTC) and final_train_end(p2) is None
    assert holdout_start(p1) == holdout_start(p2) == datetime(2026, 6, 20, tzinfo=UTC)
    assert final_train_end(tmp_path) is None and holdout_start(tmp_path) is None


@pytest.mark.parametrize(
    ("train_end", "verdict"),
    [
        ("2026-08-01T00:00:00Z", "promote"),  # exactly start - 2 days: the embargo is kept (inclusive)
        ("2026-07-25T00:00:00Z", "promote"),
        ("2026-08-01T00:00:01Z", "insufficient"),  # one second inside the embargo
        ("2026-08-02T00:00:00Z", "insufficient"),
        ("2026-08-03T00:00:00Z", "insufficient"),  # trained up to the start of the window
    ],
)
def test_v1_embargo_boundary(tmp_path: Path, train_end: str, verdict: str) -> None:
    s = Scenario(tmp_path, challenger={"train_end": train_end})
    d = l5(s.run().run(REFIT_DAY))["details"]
    assert d["verdict"] == verdict and (verdict == "promote" or d["reasons"] == ["oos_not_provable"])


def test_the_embargo_is_the_one_of_the_splitter() -> None:
    from training.splits import DEFAULT_EMBARGO

    assert learner_module.OOS_EMBARGO == DEFAULT_EMBARGO
