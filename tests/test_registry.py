"""ModelRegistry (spec sez. 5.1.6 L5, 5.3 punto 12): immutable versions, atomic aliases, rollback, the promotion rule.

Proves: a version is complete or absent and never changes; an alias moves with one atomic replace of ``aliases.json``
(a crash or a concurrent reader never sees half of it); concurrent promotions from threads and from processes lose
nothing; rollback walks the history; a damaged version or alias file is refused, never papered over; rule L5 as a table
of cases; and that nothing remote can promote a model.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from modules.config import LearnerConfig
from modules.dryrun import registry as registry_module
from modules.dryrun.registry import (
    BacktestEvidence,
    CorruptRegistry,
    FilesystemModelRegistry,
    ModelRegistry,
    NothingToRollback,
    PromotionEvidence,
    PromotionRule,
    RegistryBusy,
    RegistryError,
    UnknownAlias,
    UnknownVersion,
    VersionExists,
    artifacts_from_dir,
    decide_promotion,
    promote_if_eligible,
)

ROOT = Path(__file__).resolve().parent.parent
D = Decimal
NOW = datetime(2026, 10, 19, 0, 30, tzinfo=UTC)


def reg(root: Path) -> FilesystemModelRegistry:
    return FilesystemModelRegistry(root, clock=lambda: NOW)


def make(tmp_path: Path, version: str, body: str = "model") -> tuple[dict[str, Path], dict[str, Any]]:
    src = tmp_path / "src" / version
    (src / "final").mkdir(parents=True, exist_ok=True)
    (src / "final" / "action.txt").write_text(f"{body}-{version}", encoding="utf-8")
    (src / "model_card.json").write_text(json.dumps({"v": version}), encoding="utf-8")
    return artifacts_from_dir(src), {"version": version, "data": {"window": "2023-10..2026-10"}}


def add(r: FilesystemModelRegistry, tmp_path: Path, version: str) -> str:
    artifacts, manifest = make(tmp_path, version)
    return r.register(artifacts, manifest)


# ----------------------------------------------------------------------------------------- register


def test_register_copies_the_artifacts_and_vouches_for_them_with_sha256(tmp_path: Path) -> None:
    r = reg(tmp_path / "registry")
    artifacts, manifest = make(tmp_path, "baseline-lgbm@2026-10-18.1")
    version = r.register(artifacts, manifest)
    assert version == "baseline-lgbm@2026-10-18.1"
    folder = tmp_path / "registry" / version
    assert (folder / "final" / "action.txt").read_text(encoding="utf-8") == f"model-{version}"
    stored = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert stored["data"] == manifest["data"] and stored["registered_at"] == "2026-10-19T00:30:00Z"
    assert set(stored["files"]) == {"final/action.txt", "model_card.json"}
    assert all(len(h) == 64 for h in stored["files"].values())
    assert r.versions() == [version]


def test_a_version_is_never_overwritten(tmp_path: Path) -> None:
    r = reg(tmp_path / "registry")
    add(r, tmp_path, "v1")
    artifacts, manifest = make(tmp_path, "v1", body="other")
    with pytest.raises(VersionExists):
        r.register(artifacts, manifest)
    assert (tmp_path / "registry" / "v1" / "final" / "action.txt").read_text(encoding="utf-8") == "model-v1"
    assert [p.name for p in (tmp_path / "registry").iterdir() if p.name.startswith(".tmp-")] == []


def test_a_failed_register_leaves_no_version_and_no_temporary_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = reg(tmp_path / "registry")
    artifacts, manifest = make(tmp_path, "v1")

    def boom(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(registry_module.shutil, "copyfile", boom)
    with pytest.raises(OSError, match="disk full"):
        r.register(artifacts, manifest)
    monkeypatch.undo()
    assert r.versions() == [] and list((tmp_path / "registry").iterdir()) == []
    assert r.register(artifacts, manifest) == "v1"  # and the version is still free


def test_the_race_for_one_version_has_one_winner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The version appears between the existence check and the publication rename: the loser is refused cleanly."""
    r = reg(tmp_path / "registry")
    other = reg(tmp_path / "registry")
    artifacts, manifest = make(tmp_path, "v1")
    real_rename = os.rename
    rival_done = False

    def rename_after_a_rival(src: Any, dst: Any) -> None:
        nonlocal rival_done
        if not rival_done:
            rival_done = True
            other.register(*make(tmp_path, "v1", body="rival"))  # the rival wins just before our rename
        real_rename(src, dst)

    monkeypatch.setattr(registry_module.os, "rename", rename_after_a_rival)
    with pytest.raises(VersionExists):
        r.register(artifacts, manifest)
    monkeypatch.undo()
    assert (tmp_path / "registry" / "v1" / "final" / "action.txt").read_text(encoding="utf-8") == "rival-v1"
    assert [p.name for p in (tmp_path / "registry").iterdir() if p.name.startswith(".tmp-")] == []


@pytest.mark.parametrize("version", ["", "../x", "a/b", "a\\b", ".hidden", "x" * 129, "aliases.json", "v 1", "v1/"])
def test_unsafe_version_names_are_refused(tmp_path: Path, version: str) -> None:
    r = reg(tmp_path / "registry")
    artifacts, manifest = make(tmp_path, "ok")
    with pytest.raises(RegistryError):
        r.register(artifacts, {**manifest, "version": version})
    assert not (tmp_path / "x").exists()


@pytest.mark.parametrize(
    "name", ["../evil.txt", "/abs.txt", "a/../b.txt", "a//b.txt", "manifest.json", "c:evil", "a\\b"]
)
def test_unsafe_artifact_names_are_refused(tmp_path: Path, name: str) -> None:
    r = reg(tmp_path / "registry")
    artifacts, manifest = make(tmp_path, "v1")
    with pytest.raises(RegistryError):
        r.register({**artifacts, name: artifacts["model_card.json"]}, manifest)
    assert r.versions() == []


def test_register_needs_files_and_does_not_let_the_caller_forge_the_hashes(tmp_path: Path) -> None:
    r = reg(tmp_path / "registry")
    artifacts, manifest = make(tmp_path, "v1")
    with pytest.raises(RegistryError, match="at least one"):
        r.register({}, manifest)
    with pytest.raises(RegistryError, match="not files"):
        r.register({"x.txt": tmp_path / "nope.txt"}, manifest)
    with pytest.raises(RegistryError, match="written by the registry"):
        r.register(artifacts, {**manifest, "files": {"final/action.txt": "0" * 64}})
    with pytest.raises(ValueError):
        r.register(artifacts, {**manifest, "metric": float("nan")})
    assert r.versions() == []


# --------------------------------------------------------------------------------- promote and get


def test_promote_then_get_resolves_the_alias(tmp_path: Path) -> None:
    r = reg(tmp_path / "registry")
    add(r, tmp_path, "v1")
    with pytest.raises(UnknownAlias):
        r.get("champion")
    r.promote("v1")
    model = r.get("champion")
    assert (model.version, model.alias) == ("v1", "champion")
    assert model.path == tmp_path / "registry" / "v1" and model.manifest["version"] == "v1"
    assert (model.path / "final" / "action.txt").is_file()


def test_the_protocol_is_satisfied_and_aliases_are_independent(tmp_path: Path) -> None:
    r: ModelRegistry = reg(tmp_path / "registry")
    add(r, tmp_path, "v1")  # type: ignore[arg-type]
    add(r, tmp_path, "v2")  # type: ignore[arg-type]
    r.promote("v1", "champion")
    r.promote("v2", "challenger")
    assert (r.get("champion").version, r.get("challenger").version) == ("v1", "v2")
    r.promote("v2")
    assert r.rollback("champion") == "v1" and r.get("challenger").version == "v2"


def test_promoting_an_unregistered_version_changes_nothing(tmp_path: Path) -> None:
    r = reg(tmp_path / "registry")
    add(r, tmp_path, "v1")
    r.promote("v1")
    before = (tmp_path / "registry" / "aliases.json").read_text(encoding="utf-8")
    with pytest.raises(UnknownVersion):
        r.promote("v9")
    assert (tmp_path / "registry" / "aliases.json").read_text(encoding="utf-8") == before


def test_promoting_the_current_version_is_a_no_op(tmp_path: Path) -> None:
    r = reg(tmp_path / "registry")
    add(r, tmp_path, "v1")
    r.promote("v1")
    r.promote("v1")
    assert r.history("champion") == ["v1"]
    with pytest.raises(NothingToRollback):
        r.rollback("champion")


@pytest.mark.parametrize("alias", ["", "Champion", "a b", "../x", "1a", "x" * 33])
def test_unsafe_alias_names_are_refused(tmp_path: Path, alias: str) -> None:
    r = reg(tmp_path / "registry")
    add(r, tmp_path, "v1")
    with pytest.raises(RegistryError):
        r.promote("v1", alias)
    with pytest.raises(RegistryError):
        r.get(alias)


def test_get_refuses_a_version_that_was_touched(tmp_path: Path) -> None:
    r = reg(tmp_path / "registry")
    add(r, tmp_path, "v1")
    r.promote("v1")
    target = tmp_path / "registry" / "v1" / "final" / "action.txt"
    target.write_text("poisoned", encoding="utf-8")
    with pytest.raises(CorruptRegistry, match="sha256"):
        r.get("champion")
    target.write_text("model-v1", encoding="utf-8")
    assert r.get("champion").version == "v1"
    (tmp_path / "registry" / "v1" / "extra.txt").write_text("x", encoding="utf-8")
    with pytest.raises(CorruptRegistry, match="extra"):
        r.get("champion")
    (tmp_path / "registry" / "v1" / "extra.txt").unlink()
    target.unlink()
    with pytest.raises(CorruptRegistry, match="missing"):
        r.get("champion")


def test_a_damaged_version_cannot_be_promoted(tmp_path: Path) -> None:
    r = reg(tmp_path / "registry")
    add(r, tmp_path, "v1")
    (tmp_path / "registry" / "v1" / "model_card.json").write_text("{}", encoding="utf-8")
    with pytest.raises(CorruptRegistry):
        r.promote("v1")
    assert r.aliases() == {}


# ------------------------------------------------------------------------------------------ rollback


def test_rollback_walks_the_history_back(tmp_path: Path) -> None:
    r = reg(tmp_path / "registry")
    for v in ("v1", "v2", "v3"):
        add(r, tmp_path, v)
        r.promote(v)
    assert r.history("champion") == ["v1", "v2", "v3"]
    assert r.rollback("champion") == "v2" and r.get("champion").version == "v2"
    assert r.rollback("champion") == "v1" and r.get("champion").version == "v1"
    with pytest.raises(NothingToRollback):
        r.rollback("champion")
    assert r.get("champion").version == "v1"


def test_a_promotion_after_a_rollback_forgets_the_abandoned_version(tmp_path: Path) -> None:
    r = reg(tmp_path / "registry")
    for v in ("v1", "v2", "v3"):
        add(r, tmp_path, v)
    r.promote("v1")
    r.promote("v2")
    r.rollback("champion")
    r.promote("v3")
    assert r.history("champion") == ["v1", "v3"]
    assert r.rollback("champion") == "v1"


def test_rollback_of_an_unknown_alias_and_onto_a_damaged_version(tmp_path: Path) -> None:
    r = reg(tmp_path / "registry")
    with pytest.raises(UnknownAlias):
        r.rollback("champion")
    add(r, tmp_path, "v1")
    add(r, tmp_path, "v2")
    r.promote("v1")
    r.promote("v2")
    (tmp_path / "registry" / "v1" / "final" / "action.txt").write_text("rot", encoding="utf-8")
    with pytest.raises(CorruptRegistry):
        r.rollback("champion")
    assert r.aliases() == {"champion": "v2"}


# ------------------------------------------------------------------------------------- atomicity


def test_a_crash_while_writing_the_aliases_leaves_the_old_file_and_no_debris(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = reg(tmp_path / "registry")
    add(r, tmp_path, "v1")
    add(r, tmp_path, "v2")
    r.promote("v1")
    before = (tmp_path / "registry" / "aliases.json").read_bytes()

    def crash(src: object, dst: object) -> None:
        raise OSError("power cut")

    monkeypatch.setattr(registry_module.os, "replace", crash)
    with pytest.raises(OSError, match="power cut"):
        r.promote("v2")
    monkeypatch.undo()
    assert (tmp_path / "registry" / "aliases.json").read_bytes() == before
    assert not [p for p in (tmp_path / "registry").iterdir() if p.suffix == ".tmp"]
    assert r.get("champion").version == "v1"
    r.promote("v2")  # and the registry still works
    assert r.get("champion").version == "v2"


def test_aliases_are_written_by_replacing_a_file_not_by_truncating_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = reg(tmp_path / "registry")
    add(r, tmp_path, "v1")
    add(r, tmp_path, "v2")
    r.promote("v1")
    seen: list[tuple[str, str]] = []
    real = os.replace

    def spy(src: Any, dst: Any) -> None:
        seen.append((Path(src).name, Path(dst).name))
        assert json.loads(Path(src).read_text(encoding="utf-8"))["aliases"]["champion"]["version"] == "v2"
        real(src, dst)

    monkeypatch.setattr(registry_module.os, "replace", spy)
    r.promote("v2")
    assert len(seen) == 1 and seen[0][1] == "aliases.json" and seen[0][0].endswith(".tmp")


def test_a_reader_never_sees_a_half_written_alias_file(tmp_path: Path) -> None:
    r = reg(tmp_path / "registry")
    names = [f"v{i}" for i in range(6)]
    for v in names:
        add(r, tmp_path, v)
    r.promote("v0")
    stop = threading.Event()
    problems: list[str] = []

    def reader() -> None:
        rd = reg(tmp_path / "registry")
        while not stop.is_set():
            try:
                rd.aliases()
            except Exception as exc:  # any error here is a torn read
                problems.append(f"{type(exc).__name__}: {exc}")
                return

    t = threading.Thread(target=reader)
    t.start()
    try:
        for i in range(60):
            r.promote(names[(i + 1) % len(names)])
    finally:
        stop.set()
        t.join()
    assert problems == []


def test_a_corrupt_alias_file_is_an_error_never_an_empty_registry(tmp_path: Path) -> None:
    r = reg(tmp_path / "registry")
    add(r, tmp_path, "v1")
    r.promote("v1")
    path = tmp_path / "registry" / "aliases.json"
    for garbage in ("", "{", '{"schema": "other", "aliases": {}}', '{"schema": "chronos.registry.aliases.v1"}'):
        path.write_text(garbage, encoding="utf-8")
        with pytest.raises(CorruptRegistry):
            r.get("champion")
        with pytest.raises(CorruptRegistry):
            r.promote("v1")  # must not overwrite the evidence with a fresh file
        assert path.read_text(encoding="utf-8") == garbage
    entry = {"version": "../x", "previous": [], "updated_at": "t"}
    doc = {"schema": "chronos.registry.aliases.v1", "aliases": {"champion": entry}}
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(CorruptRegistry):  # a version taken from the file can not walk out of the registry
        r.get("champion")


# ------------------------------------------------------------------------------------- concurrency


def test_concurrent_promotions_from_threads_lose_nothing(tmp_path: Path) -> None:
    base = tmp_path / "registry"
    seed = reg(base)
    names = [f"v{i:02d}" for i in range(12)]
    for v in names:
        add(seed, tmp_path, v)
    errors: list[BaseException] = []
    start = threading.Barrier(len(names))

    def work(version: str) -> None:
        try:
            own = reg(base)  # an instance per thread: only the file lock can serialize them
            start.wait()
            own.promote(version)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(v,)) for v in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    history = seed.history("champion")
    assert sorted(history) == names  # every promotion is in the history, exactly once
    assert seed.get("champion").version == history[-1]


def test_concurrent_promotions_from_processes_lose_nothing(tmp_path: Path) -> None:
    base = tmp_path / "registry"
    seed = reg(base)
    names = [f"v{i}" for i in range(6)]
    for v in names:
        add(seed, tmp_path, v)
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "modules.dryrun.registry", "--root", str(base), "promote", v],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )  # fmt: skip
        for v in names
    ]
    results = [(p.wait(timeout=120), p.stderr.read() if p.stderr else "") for p in procs]
    assert [code for code, _ in results] == [0] * len(names), results
    assert sorted(seed.history("champion")) == names


def test_the_lock_times_out_instead_of_hanging(tmp_path: Path) -> None:
    base = tmp_path / "registry"
    holder = FilesystemModelRegistry(base, lock_timeout_s=5)
    add(holder, tmp_path, "v1")
    impatient = FilesystemModelRegistry(base, lock_timeout_s=0.1)
    with holder._locked(), pytest.raises(RegistryBusy):
        impatient.promote("v1")
    impatient.promote("v1")  # released


# ----------------------------------------------------------------------------------------------- CLI


def run_cli(base: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    return subprocess.run(
        [sys.executable, "-m", "modules.dryrun.registry", "--root", str(base), *args],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
    )  # fmt: skip


def test_the_local_cli_lists_promotes_and_rolls_back(tmp_path: Path) -> None:
    base = tmp_path / "registry"
    seed = reg(base)
    add(seed, tmp_path, "v1")
    add(seed, tmp_path, "v2")
    assert run_cli(base, "promote", "v1").returncode == 0
    assert run_cli(base, "promote", "v2").returncode == 0
    listing = json.loads(run_cli(base, "list").stdout)
    assert listing == {"versions": ["v1", "v2"], "aliases": {"champion": "v2"}}
    shown = json.loads(run_cli(base, "show", "champion").stdout)
    assert shown["history"] == ["v1", "v2"]
    assert json.loads(run_cli(base, "rollback", "champion").stdout)["version"] == "v1"
    failed = run_cli(base, "promote", "v9")
    assert failed.returncode == 1 and "UnknownVersion" in failed.stderr
    assert run_cli(base, "rollback", "champion").returncode == 1  # nothing before v1


# ----------------------------------------------------------------------------------------- rule L5


def bt(pnl: str = "5", sharpe: float | None = 1.0, dd: float = 0.04, days: float = 90) -> BacktestEvidence:
    return BacktestEvidence(window_days=days, net_pnl=D(pnl), sharpe=sharpe, max_dd_frac=dd)


def evidence(
    challenger: BacktestEvidence | None = None,
    champion: BacktestEvidence | None = None,
    shadow_days: float = 14,
    ops: int = 0,
    parity: int = 0,
) -> PromotionEvidence:
    return PromotionEvidence(challenger or bt("8", 1.4, 0.03), champion or bt("5", 1.0, 0.04), shadow_days, ops, parity)


L5_CASES = [
    # id, evidence, verdict, reasons
    ("beats_on_all_counts", evidence(), "promote", ()),
    ("equal_drawdown_is_not_worse", evidence(bt("8", 1.4, 0.04)), "promote", ()),
    ("exactly_14_shadow_days_is_enough", evidence(shadow_days=14.0), "promote", ()),
    ("pnl_equal", evidence(bt("5", 1.4, 0.03)), "reject", ("net_pnl_not_better",)),
    ("pnl_lower", evidence(bt("-1", 1.4, 0.03)), "reject", ("net_pnl_not_better",)),
    ("sharpe_equal", evidence(bt("8", 1.0, 0.03)), "reject", ("sharpe_not_better",)),
    ("drawdown_worse", evidence(bt("8", 1.4, 0.0401)), "reject", ("max_dd_worse",)),
    ("operational_anomaly", evidence(ops=1), "reject", ("operational_anomalies",)),
    ("parity_anomaly", evidence(parity=2), "reject", ("parity_anomalies",)),
    ("all_fail_at_once", evidence(bt("0", 0.5, 0.09), ops=1, parity=1), "reject",
     ("net_pnl_not_better", "sharpe_not_better", "max_dd_worse", "operational_anomalies", "parity_anomalies")),
    ("shadow_13_days", evidence(shadow_days=13.99), "insufficient", ("shadow_days_short",)),
    ("shadow_zero_days", evidence(shadow_days=0), "insufficient", ("shadow_days_short",)),
    ("challenger_window_89_days", evidence(bt("8", 1.4, 0.03, days=89)), "insufficient",
     ("challenger_backtest_window_short",)),
    ("champion_window_short", evidence(champion=bt(days=30)), "insufficient", ("champion_backtest_window_short",)),
    ("challenger_sharpe_undefined", evidence(bt("8", None, 0.03)), "insufficient", ("challenger_sharpe_undefined",)),
    ("champion_sharpe_nan", evidence(champion=bt("5", float("nan"), 0.04)), "insufficient",
     ("champion_sharpe_undefined",)),
    ("short_sample_beats_a_good_looking_result", evidence(bt("99", 9.0, 0.0, days=10), shadow_days=2), "insufficient",
     ("challenger_backtest_window_short", "shadow_days_short")),
    ("anomalies_do_not_hide_a_short_sample", evidence(shadow_days=3, ops=5), "insufficient", ("shadow_days_short",)),
]  # fmt: skip


@pytest.mark.parametrize(("case", "ev", "verdict", "reasons"), L5_CASES, ids=[c[0] for c in L5_CASES])
def test_promotion_rules(case: str, ev: PromotionEvidence, verdict: str, reasons: tuple[str, ...]) -> None:
    decision = decide_promotion(ev, PromotionRule(backtest_recent_days=90, min_shadow_days=14))
    assert (decision.verdict, decision.reasons) == (verdict, reasons)
    assert decision.approved == (verdict == "promote")


def test_the_rule_takes_its_windows_from_the_learner_config() -> None:
    rule = PromotionRule.from_config(LearnerConfig(promotion={"backtest_recent_days": 60, "min_shadow_days": 7}))
    assert (rule.backtest_recent_days, rule.min_shadow_days) == (60, 7)
    ok = evidence(bt("8", 1.4, 0.03, days=60), bt("5", 1.0, 0.04, days=60), shadow_days=7)
    assert decide_promotion(ok, rule).approved
    assert not decide_promotion(ok, PromotionRule(90, 14)).approved
    assert PromotionRule() == PromotionRule(90, 14)  # the spec defaults when no rule is given
    assert decide_promotion(evidence()).approved


class RecordingRegistry:
    def __init__(self) -> None:
        self.promoted: list[tuple[str, str]] = []

    def register(self, artifacts: Any, manifest: Any) -> str:
        raise AssertionError("not used")

    def get(self, alias: str) -> Any:
        raise AssertionError("not used")

    def promote(self, version: str, alias: str = "champion") -> None:
        self.promoted.append((version, alias))

    def rollback(self, alias: str) -> str:
        raise AssertionError("not used")


def test_only_an_approved_challenger_reaches_the_registry() -> None:
    fake = RecordingRegistry()
    assert promote_if_eligible(fake, "v2", evidence()).approved
    assert fake.promoted == [("v2", "champion")]
    for bad in (evidence(shadow_days=3), evidence(ops=1), evidence(bt("1", 0.1, 0.1))):
        assert not promote_if_eligible(fake, "v3", bad).approved
    assert fake.promoted == [("v2", "champion")]


def test_l5_end_to_end_on_the_filesystem(tmp_path: Path) -> None:
    r = reg(tmp_path / "registry")
    add(r, tmp_path, "v1")
    add(r, tmp_path, "v2")
    r.promote("v1")
    r.promote("v2", "challenger")
    assert promote_if_eligible(r, "v2", evidence(shadow_days=5)).verdict == "insufficient"
    assert r.get("champion").version == "v1"
    assert promote_if_eligible(r, "v2", evidence()).approved
    assert r.get("champion").version == "v2" and r.rollback("champion") == "v1"


# ----------------------------------------------------------------------------- no remote promotion

FORBIDDEN_IMPORTS = {
    "socket", "http", "urllib", "requests", "aiohttp", "httpx", "asyncio", "subprocess", "signal", "ssl", "ftplib",
    "smtplib", "xmlrpc", "websockets", "modules.control", "modules.app", "coinbase",
}  # fmt: skip


def test_the_registry_has_no_remote_hook() -> None:
    tree = ast.parse(Path(registry_module.__file__).read_text(encoding="utf-8"))
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            imported.add(n.module or "")
    bad = {m for m in imported if m.split(".")[0] in FORBIDDEN_IMPORTS or m in FORBIDDEN_IMPORTS}
    assert not bad, f"the registry must stay a local mechanism: {sorted(bad)}"
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
    }
    assert not names & {"environ", "getenv", "environb"}, "promotion must not depend on the environment"
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)], "no coroutine can be a handler"


def test_the_control_channel_cannot_reach_promote_or_rollback() -> None:
    """Sez. 6.4.2 forbids model promotion and rollback from the GUI: nothing under modules/control may use them."""
    control = ROOT / "modules" / "control"
    if not control.is_dir():
        pytest.skip("pending: modules/control does not exist yet")
    hits = []
    for p in sorted(control.rglob("*.py")):
        for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            ident = {ast.Attribute: lambda x: x.attr, ast.Name: lambda x: x.id}.get(type(n), lambda x: "")(n)
            module = n.module if isinstance(n, ast.ImportFrom) else ""
            if ident in {"promote", "rollback", "ModelRegistry", "FilesystemModelRegistry", "promote_if_eligible"} or (
                module or ""
            ).startswith("modules.dryrun.registry"):
                hits.append(f"{p.relative_to(ROOT)}: {ident or module}")
    assert not hits, "the remote channel must not promote models:\n" + "\n".join(hits)
