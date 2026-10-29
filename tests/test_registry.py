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

from modules.config import GateEconConfig, LearnerConfig
from modules.dryrun import registry as registry_module
from modules.dryrun.registry import (
    BacktestEvidence,
    CorruptRegistry,
    FilesystemModelRegistry,
    GateSummary,
    ModelRegistry,
    NothingToRollback,
    OosEvidence,
    PromotionEvidence,
    PromotionRule,
    RegistryBusy,
    RegistryError,
    UnknownAlias,
    UnknownVersion,
    VersionExists,
    artifacts_from_dir,
    decide_promotion,
    last_l5_verdict,
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
            [sys.executable, "-m", "modules.dryrun.registry", "--root", str(base), "promote", v, "--reason", "race"],
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
    assert run_cli(base, "promote", "v1", "--reason", "first").returncode == 0
    assert run_cli(base, "promote", "v2", "--reason", "second").returncode == 0
    listing = json.loads(run_cli(base, "list").stdout)
    assert listing == {"versions": ["v1", "v2"], "aliases": {"champion": "v2"}}
    shown = json.loads(run_cli(base, "show", "champion").stdout)
    assert shown["history"] == ["v1", "v2"] and shown["op"] == "promote" and shown["reason"] == "second"
    assert json.loads(run_cli(base, "rollback", "champion", "--reason", "bad").stdout)["version"] == "v1"
    failed = run_cli(base, "promote", "v9", "--reason", "x")
    assert failed.returncode == 1 and "UnknownVersion" in failed.stderr
    assert run_cli(base, "rollback", "champion", "--reason", "again").returncode == 1  # nothing before v1


def test_cli_requires_reason(tmp_path: Path) -> None:
    base = tmp_path / "registry"
    seed = reg(base)
    add(seed, tmp_path, "v1")
    add(seed, tmp_path, "v2")
    seed.promote("v1")
    seed.promote("v2")
    for args in (("promote", "v1"), ("rollback", "champion"), ("rollback",), ("promote", "v1", "--reason", "  ")):
        out = run_cli(base, *args)
        assert out.returncode == 2, args  # argparse usage error: nothing was done
        assert "--reason" in out.stderr
    assert seed.aliases() == {"champion": "v2"}


def test_cli_rollback_says_effective_at_reload(tmp_path: Path) -> None:
    base = tmp_path / "registry"
    seed = reg(base)
    add(seed, tmp_path, "v1")
    add(seed, tmp_path, "v2")
    seed.promote("v1")
    seed.promote("v2")
    out = run_cli(base, "rollback", "--reason", "parity broke")  # the alias defaults to champion
    assert out.returncode == 0, out.stderr
    doc = json.loads(out.stdout)
    assert doc["version"] == "v1" and doc["op"] == "rollback" and doc["reason"] == "parity broke"
    assert "00:30 UTC" in doc["note"] and "no order is opened, closed or modified" in doc["note"]
    assert seed.state("champion").op == "rollback"


def test_cli_promote_prints_the_last_l5_verdict_and_goes_on(tmp_path: Path) -> None:
    """Decision U2: a manual promotion overrides L5. It shows what L5 said and does not ask for it."""
    base = tmp_path / "registry"
    seed = reg(base)
    add(seed, tmp_path, "v1")
    add(seed, tmp_path, "v2")
    reports = tmp_path / "learner"
    reports.mkdir()
    for day, verdict in (("2026-10-01", "reject"), ("2026-11-01", "insufficient")):
        doc = {"date": day, "steps": {"L5": {"details": {"challenger": "v2", "verdict": verdict, "reasons": ["x"]}}}}
        (reports / f"{day}.json").write_text(json.dumps(doc), encoding="utf-8")
    out = run_cli(base, "--learner-reports", str(reports), "promote", "v2", "--reason", "maintenance refit")
    assert out.returncode == 0, out.stderr
    doc = json.loads(out.stdout)
    assert doc["last_l5_verdict"] == {"date": "2026-11-01", "verdict": "insufficient", "reasons": ["x"]}
    assert seed.get("champion").version == "v2"  # L5 did not approve it, the human did
    assert last_l5_verdict(reports, "v-never-evaluated") is None


def test_rollback_records_op_and_reason(tmp_path: Path) -> None:
    r = reg(tmp_path / "registry")
    for v in ("v1", "v2"):
        add(r, tmp_path, v)
    r.promote("v1", reason="seed")
    r.promote("v2", reason="refit")
    assert (r.state("champion").op, r.state("champion").reason) == ("promote", "refit")
    assert r.rollback("champion", reason="parity < 99%") == "v1"
    st = r.state("champion")
    assert (st.version, st.op, st.reason, st.previous) == ("v1", "rollback", "parity < 99%", ())
    on_disk = json.loads((tmp_path / "registry" / "aliases.json").read_text(encoding="utf-8"))
    assert on_disk["aliases"]["champion"]["op"] == "rollback"
    assert on_disk["aliases"]["champion"]["reason"] == "parity < 99%"
    r.promote("v2", reason="again")  # a later promotion overwrites the record
    assert (r.state("champion").op, r.state("champion").reason) == ("promote", "again")


def test_aliases_without_op_still_load(tmp_path: Path) -> None:
    r = reg(tmp_path / "registry")
    add(r, tmp_path, "v1")
    (tmp_path / "registry" / "aliases.json").write_text(
        json.dumps({"schema": "chronos.registry.aliases.v1",
                    "aliases": {"champion": {"version": "v1", "previous": [], "updated_at": "2026-10-01T00:00:00Z"}}}),
        encoding="utf-8",
    )  # fmt: skip
    st = r.state("champion")
    assert (st.op, st.reason, st.version) == ("promote", "", "v1")
    assert r.get("champion").version == "v1"
    (tmp_path / "registry" / "aliases.json").write_text(
        json.dumps({"schema": "chronos.registry.aliases.v1",
                    "aliases": {"champion": {"version": "v1", "previous": [], "updated_at": "x", "op": "delete"}}}),
        encoding="utf-8",
    )  # fmt: skip
    with pytest.raises(CorruptRegistry):
        r.state("champion")


# ----------------------------------------------------------------------------------------- rule L5

W0, W1 = datetime(2026, 7, 20, tzinfo=UTC), datetime(2026, 10, 18, tzinfo=UTC)
RULE = PromotionRule()


def bt(
    pnl: str = "5", sharpe: float | None = 1.0, dd: float = 0.04, days: float = 90.0, n: int = 40,
    start: datetime = W0, end: datetime = W1,
) -> BacktestEvidence:  # fmt: skip
    return BacktestEvidence(start, end, days, n, D(pnl), sharpe, dd)


def gates(ge: Any = "GO", **flags: bool | None) -> GateSummary:
    passed: dict[str, bool | None] = {"G0": True, "G1": True, "G2": True, "G4": True}
    passed.update(flags)
    return GateSummary(ge, passed)


def oos(**kw: Any) -> OosEvidence:
    base: dict[str, Any] = {
        "common_days": 600.0, "n_trades_ch": 180, "max_dd_ch": 0.03, "n_trades_cp": 170, "d_pnl_quantile": 12.0,
        "sharpe_ch": 1.2, "sharpe_cp": 0.8, "max_dd_cp": 0.04,
    }  # fmt: skip
    return OosEvidence(**{**base, **kw})


def evidence(**kw: Any) -> PromotionEvidence:
    """A challenger that satisfies EVERY condition of rule L5; each test breaks one thing."""
    base: dict[str, Any] = {
        "challenger": bt("8", 1.4, 0.03), "champion": bt("5", 1.0, 0.04), "challenger_gates": gates(),
        "champion_gates": gates(), "oos": oos(), "oos_provable": True, "shadow_days": 14.0,
        "operational_anomalies": 0, "parity_anomalies": 0,
    }  # fmt: skip
    return PromotionEvidence(**{**base, **kw})


def verdict_of(ev: PromotionEvidence, rule: PromotionRule = RULE) -> tuple[str, tuple[str, ...]]:
    d = decide_promotion(ev, rule)
    assert d.approved == (d.verdict == "promote")
    return d.verdict, d.reasons


def test_promotion_rule_from_config_reads_learner_and_gate_econ() -> None:
    rule = PromotionRule.from_config(
        LearnerConfig(promotion={"backtest_recent_days": 60, "min_shadow_days": 7, "superiority_quantile": 0.1}),
        GateEconConfig(min_trades=45, max_drawdown_frac=0.08),
    )
    assert rule == PromotionRule(
        backtest_recent_days=60, min_shadow_days=7, min_recent_trades=45, max_drawdown_frac=0.08,
        superiority_quantile=0.1,
    )  # fmt: skip
    assert PromotionRule.from_config(LearnerConfig(), GateEconConfig()) == PromotionRule()  # the spec defaults
    assert (PromotionRule().min_recent_trades, PromotionRule().max_drawdown_frac) == (30, 0.05)
    # the old YAML (two keys) is still valid and gets the default quantile
    old = LearnerConfig(promotion={"backtest_recent_days": 90, "min_shadow_days": 14})
    assert old.promotion.superiority_quantile == 0.05
    with pytest.raises(ValueError):
        LearnerConfig(promotion={"backtest_recent_days": 90, "min_shadow_days": 14, "typo": 1})


def test_ge_literals_are_the_ones_of_training_evaluate() -> None:
    import typing

    from training.evaluate import Status

    ours = {registry_module.GE_GO, registry_module.GE_INCONCLUSIVE, registry_module.GE_NO_GO}
    assert ours == set(typing.get_args(Status))
    assert set(typing.get_args(registry_module.GeStatus)) == ours


def test_l5_promote_when_all_conditions_hold() -> None:
    assert verdict_of(evidence()) == ("promote", ())
    # the boundaries that are inclusive
    assert verdict_of(evidence(challenger=bt("8", 1.4, 0.04))) == ("promote", ())  # drawdown equal to the champion
    assert verdict_of(evidence(challenger=bt("8", 1.4, 0.05), champion=bt("5", 1.0, 0.05))) == ("promote", ())
    assert verdict_of(evidence(shadow_days=14.0)) == ("promote", ())
    assert verdict_of(evidence(challenger=bt("8", 1.4, 0.03, n=30))) == ("promote", ())  # exactly 30 trades
    assert verdict_of(evidence(oos=oos(max_dd_ch=0.04))) == ("promote", ())  # OOS drawdown equal to the champion's
    assert verdict_of(evidence(oos=oos(max_dd_ch=0.05, max_dd_cp=0.05))) == ("promote", ())


def test_l5_insufficient_window_short() -> None:
    assert verdict_of(evidence(challenger=bt("8", 1.4, 0.03, days=89.99))) == (
        "insufficient", ("challenger_backtest_window_short",))
    assert verdict_of(evidence(champion=bt(days=30))) == ("insufficient", ("champion_backtest_window_short",))
    assert verdict_of(evidence(challenger=bt("8", 1.4, 0.03, days=float("nan")))) == (
        "insufficient", ("challenger_backtest_window_short",))
    assert verdict_of(evidence(shadow_days=13.99)) == ("insufficient", ("shadow_days_short",))
    assert verdict_of(evidence(shadow_days=float("nan"))) == ("insufficient", ("shadow_days_short",))
    short = evidence(challenger=bt("99", 9.0, 0.0, days=10), shadow_days=2)
    assert verdict_of(short)[0] == "insufficient"  # a short sample never looks like a win
    assert verdict_of(evidence(), PromotionRule(backtest_recent_days=120))[0] == "insufficient"
    assert verdict_of(evidence(shadow_days=7), PromotionRule(min_shadow_days=7)) == ("promote", ())


def test_l5_insufficient_windows_differ() -> None:
    other = bt(end=datetime(2026, 10, 19, tzinfo=UTC))
    assert verdict_of(evidence(champion=other)) == ("insufficient", ("backtest_windows_differ",))
    other = bt(start=datetime(2026, 7, 21, tzinfo=UTC))
    assert verdict_of(evidence(champion=other)) == ("insufficient", ("backtest_windows_differ",))


def test_l5_insufficient_oos_not_provable() -> None:
    assert verdict_of(evidence(oos_provable=False)) == ("insufficient", ("oos_not_provable",))


def test_l5_insufficient_challenger_evaluation_missing_or_inconclusive() -> None:
    missing = GateSummary(None, {"G0": None, "G1": None, "G2": None, "G4": None})
    assert verdict_of(evidence(challenger_gates=missing)) == (
        "insufficient",
        ("challenger_evaluation_missing", "challenger_G0_missing", "challenger_G1_missing",
         "challenger_G2_missing", "challenger_G4_missing"),
    )  # fmt: skip
    assert verdict_of(evidence(challenger_gates=gates("INCONCLUSO"))) == (
        "insufficient", ("challenger_ge_inconclusive",))
    assert verdict_of(evidence(challenger_gates=gates(G4=None))) == ("insufficient", ("challenger_G4_missing",))
    absent_key = GateSummary("GO", {"G0": True, "G1": True, "G2": True})  # G4 not even listed
    assert verdict_of(evidence(challenger_gates=absent_key)) == ("insufficient", ("challenger_G4_missing",))
    llm = GateSummary("GO", {"G0": True, "G1": True, "G2": True, "G3": None, "G4": True})  # an LLM backend reports G3
    assert verdict_of(evidence(challenger_gates=llm)) == ("insufficient", ("challenger_G3_missing",))
    assert verdict_of(evidence(oos=None)) == ("insufficient", ("oos_unavailable",))
    no_pair = oos(d_pnl_quantile=None)
    assert verdict_of(evidence(oos=no_pair)) == ("insufficient", ("paired_oos_unavailable",))
    assert verdict_of(evidence(oos=oos(sharpe_cp=float("nan")))) == ("insufficient", ("paired_oos_unavailable",))


def test_l5_reject_ge_nogo_even_with_short_shadow() -> None:
    v, reasons = verdict_of(evidence(challenger_gates=gates("NO-GO"), shadow_days=2))
    assert v == "reject" and reasons[0] == "challenger_ge_nogo" and "shadow_days_short" in reasons


def test_l5_reject_gate_g4_failed() -> None:
    assert verdict_of(evidence(challenger_gates=gates(G4=False))) == ("reject", ("challenger_G4_failed",))
    assert verdict_of(evidence(challenger_gates=gates(G0=False, G2=False))) == (
        "reject", ("challenger_G0_failed", "challenger_G2_failed"))
    llm = GateSummary("GO", {"G0": True, "G1": True, "G2": True, "G3": False, "G4": True})
    assert verdict_of(evidence(challenger_gates=llm)) == ("reject", ("challenger_G3_failed",))


def test_l5_insufficient_recent_trades_below_min() -> None:
    assert verdict_of(evidence(challenger=bt("8", 1.4, 0.03, n=29))) == (
        "insufficient", ("challenger_recent_trades_short",))
    assert verdict_of(evidence(challenger=bt("8", 1.4, 0.03, n=10)), PromotionRule(min_recent_trades=10)) == (
        "promote", ())
    undefined = evidence(challenger=bt("8", None, 0.03))
    assert verdict_of(undefined) == ("insufficient", ("challenger_sharpe_undefined",))
    nan = evidence(champion=bt("5", float("nan"), 0.04))
    assert verdict_of(nan) == ("insufficient", ("champion_sharpe_undefined",))


def test_l5_zero_trade_model_has_defined_metrics() -> None:
    idle = bt("0", 0.0, 0.0, n=0)
    assert verdict_of(evidence(champion=idle, champion_gates=gates(None))) == ("promote", ())
    undefined_sharpe = bt("0", None, 0.0, n=0)  # a backtest that reports no Sharpe for an idle model: it is 0.0
    assert verdict_of(evidence(champion=undefined_sharpe, champion_gates=gates(None))) == ("promote", ())
    # the idle champion is the reference, so a challenger that loses is rejected, not "insufficient"
    losing = evidence(challenger=bt("-3", -0.4, 0.02), champion=idle, champion_gates=gates(None))
    assert verdict_of(losing) == ("reject", ("recent_pnl_not_positive", "recent_pnl_not_better",
                                             "recent_sharpe_not_better"))  # fmt: skip
    # but an idle CHALLENGER cannot be promoted: it has no trades to be judged on
    assert verdict_of(evidence(challenger=idle)) == ("insufficient", ("challenger_recent_trades_short",))


def test_l5_reject_recent_pnl_not_positive() -> None:
    both_negative = evidence(challenger=bt("-1", 0.5, 0.03), champion=bt("-4", 0.2, 0.04))
    assert verdict_of(both_negative) == ("reject", ("recent_pnl_not_positive",))  # the less negative does not win
    assert verdict_of(evidence(challenger=bt("0", 1.4, 0.03), champion=bt("-1", 1.0, 0.04))) == (
        "reject", ("recent_pnl_not_positive",))  # zero is not positive


def test_l5_reject_recent_ties() -> None:
    assert verdict_of(evidence(challenger=bt("5", 1.4, 0.03))) == ("reject", ("recent_pnl_not_better",))
    assert verdict_of(evidence(challenger=bt("8", 1.0, 0.03))) == ("reject", ("recent_sharpe_not_better",))
    assert verdict_of(evidence(challenger=bt("8", 1.4, 0.0401))) == ("reject", ("recent_max_dd_worse",))
    assert verdict_of(evidence(challenger=bt("8", 0.99, 0.03))) == ("reject", ("recent_sharpe_not_better",))


def test_l5_dd_vs_champion_skipped_when_champion_has_no_trades() -> None:
    idle = bt("0", 0.0, 0.0, n=0)
    ok = evidence(challenger=bt("8", 1.4, 0.04), champion=idle, champion_gates=gates(None))
    assert verdict_of(ok) == ("promote", ())  # 0.04 > 0.0 would be "worse" against an idle champion: S3 decides
    bad = evidence(challenger=bt("8", 1.4, 0.06), champion=idle, champion_gates=gates(None))
    assert verdict_of(bad) == ("reject", ("recent_max_dd_above_limit",))


def test_l5_reject_dd_above_limit_recent_and_oos() -> None:
    assert verdict_of(evidence(challenger=bt("8", 1.4, 0.0501), champion=bt("5", 1.0, 0.06))) == (
        "reject", ("recent_max_dd_above_limit",))
    assert verdict_of(evidence(oos=oos(max_dd_ch=0.0501, max_dd_cp=0.06))) == ("reject", ("oos_max_dd_above_limit",))
    both = evidence(challenger=bt("8", 1.4, 0.07), champion=bt("5", 1.0, 0.07), oos=oos(max_dd_ch=0.07, max_dd_cp=0.07))
    assert verdict_of(both) == ("reject", ("recent_max_dd_above_limit", "oos_max_dd_above_limit"))
    assert verdict_of(evidence(challenger=bt("8", 1.4, 0.07), champion=bt("5", 1.0, 0.07)), PromotionRule(
        max_drawdown_frac=0.10))[0] == "promote"  # the limit is the rule's (gate_econ.max_drawdown_frac)


def test_l5_p2_skipped_when_champion_not_go() -> None:
    weak_oos = oos(d_pnl_quantile=None, sharpe_cp=None, max_dd_cp=None, n_trades_cp=None)
    for champion_ge in (None, "NO-GO", "INCONCLUSO"):
        ev = evidence(champion_gates=gates(champion_ge), oos=weak_oos)
        assert verdict_of(ev) == ("promote", ()), champion_ge
    # even numbers that would fail P2 are not looked at when the champion is not GO
    ev = evidence(champion_gates=gates(None), oos=oos(d_pnl_quantile=-5.0, sharpe_cp=9.0, max_dd_cp=0.0))
    assert verdict_of(ev) == ("promote", ())


def test_l5_reject_paired_quantile_not_positive() -> None:
    for q in (0.0, -0.01, -50.0):
        assert verdict_of(evidence(oos=oos(d_pnl_quantile=q))) == ("reject", ("oos_pnl_difference_not_significant",)), q
    assert verdict_of(evidence(oos=oos(d_pnl_quantile=float("nan")))) == ("insufficient", ("paired_oos_unavailable",))
    assert verdict_of(evidence(oos=oos(d_pnl_quantile=1e-9))) == ("promote", ())


def test_l5_reject_oos_sharpe_or_dd_worse() -> None:
    assert verdict_of(evidence(oos=oos(sharpe_ch=0.8))) == ("reject", ("oos_sharpe_not_better",))
    assert verdict_of(evidence(oos=oos(sharpe_ch=0.5))) == ("reject", ("oos_sharpe_not_better",))
    assert verdict_of(evidence(oos=oos(max_dd_ch=0.0401))) == ("reject", ("oos_max_dd_worse",))
    worst = evidence(oos=oos(d_pnl_quantile=0.0, sharpe_ch=0.1, max_dd_ch=0.049, max_dd_cp=0.01))
    assert verdict_of(worst) == (
        "reject", ("oos_pnl_difference_not_significant", "oos_sharpe_not_better", "oos_max_dd_worse"))


def test_l5_reject_anomalies() -> None:
    assert verdict_of(evidence(operational_anomalies=1)) == ("reject", ("operational_anomalies",))
    assert verdict_of(evidence(parity_anomalies=2)) == ("reject", ("parity_anomalies",))
    # an anomaly is definitive even when the sample is short, and the shortness is reported too
    v, reasons = verdict_of(evidence(operational_anomalies=5, shadow_days=3))
    assert v == "reject" and reasons == ("operational_anomalies", "shadow_days_short")


def test_l5_reasons_list_every_failed_condition() -> None:
    ev = evidence(
        challenger=bt("-2", 0.2, 0.09), champion=bt("5", 1.0, 0.04),
        oos=oos(d_pnl_quantile=-1.0, sharpe_ch=0.1, max_dd_ch=0.08, max_dd_cp=0.04),
    )  # fmt: skip
    assert verdict_of(ev) == (
        "reject",
        ("recent_pnl_not_positive", "recent_pnl_not_better", "recent_sharpe_not_better", "recent_max_dd_worse",
         "recent_max_dd_above_limit", "oos_max_dd_above_limit", "oos_pnl_difference_not_significant",
         "oos_sharpe_not_better", "oos_max_dd_worse"),
    )  # fmt: skip
    nothing = evidence(
        challenger_gates=gates("NO-GO", G1=False), operational_anomalies=1, parity_anomalies=1, shadow_days=1.0
    )
    assert verdict_of(nothing) == (
        "reject",
        ("challenger_ge_nogo", "challenger_G1_failed", "operational_anomalies", "parity_anomalies",
         "shadow_days_short"),
    )  # fmt: skip


def test_current_models_never_promoted() -> None:
    """Today every subset is untunable: the challenger does not trade (n=0), its GE is not GO. Whatever the rest says,
    the verdict is never ``promote`` (the default is to keep the champion)."""
    idle = bt("0", 0.0, 0.0, n=0)
    for ge in ("NO-GO", "INCONCLUSO", None):
        for champion in (idle, bt("5", 1.0, 0.04)):
            ev = evidence(challenger=idle, challenger_gates=gates(ge), champion=champion, oos=oos(n_trades_ch=0))
            assert verdict_of(ev)[0] in ("reject", "insufficient"), (ge, champion)
    # and with a flat nightly evidence (no oos files, no evaluation) the answer is "not enough", never "yes"
    flat = evidence(challenger=idle, challenger_gates=GateSummary(None, {}), oos=None, oos_provable=False)
    assert verdict_of(flat)[0] == "insufficient"


class RecordingRegistry:
    def __init__(self) -> None:
        self.promoted: list[tuple[str, str]] = []

    def register(self, artifacts: Any, manifest: Any) -> str:
        raise AssertionError("not used")

    def get(self, alias: str) -> Any:
        raise AssertionError("not used")

    def promote(self, version: str, alias: str = "champion", *, reason: str = "") -> None:
        self.promoted.append((version, alias))

    def rollback(self, alias: str, *, reason: str = "") -> str:
        raise AssertionError("not used")


def test_only_an_approved_challenger_reaches_the_registry() -> None:
    fake = RecordingRegistry()
    assert promote_if_eligible(fake, "v2", evidence()).approved
    assert fake.promoted == [("v2", "champion")]
    for bad in (
        evidence(shadow_days=3), evidence(operational_anomalies=1), evidence(challenger=bt("1", 0.1, 0.1)),
        evidence(oos_provable=False), evidence(challenger_gates=gates("NO-GO")),
    ):  # fmt: skip
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
    assert r.get("champion").version == "v2" and r.rollback("champion", reason="test") == "v1"


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
