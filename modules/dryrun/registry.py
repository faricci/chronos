"""ModelRegistry: versioned model artifacts and the aliases that point at them (spec sez. 5.1.6, 5.3 punto 12; C8).

Layout (``models/registry/``)::

    <version>/manifest.json         the manifest given to ``register`` plus ``files`` (sha256 of every artifact)
    <version>/<artifact files>      copied, never modified afterwards
    aliases.json                    {"champion": {"version", "previous": [...], "updated_at", "op", "reason"}, ...}

Rules:

* A version is IMMUTABLE. ``register`` builds it in a temporary directory and publishes it with one rename, so a
  version is either absent or complete; registering an existing version is refused. ``get`` and ``promote`` re-hash
  the files against the manifest and refuse a version that was touched or is incomplete.
* An alias is a pointer. ``aliases.json`` is written to a temporary file and moved over the old one with
  ``os.replace`` (readers see the old or the new file, never half of one); the read-modify-write of promote and
  rollback runs under an exclusive file lock, so concurrent processes (the nightly learner and the CLI) cannot lose
  an update. A corrupt ``aliases.json`` is an error, never "no aliases".
* ``previous`` is the stack of versions the alias pointed at before. ``rollback`` pops it: rolling back twice goes two
  versions back; promoting again pushes the current version. Every entry records the last operation (``op``:
  ``promote`` | ``rollback``) and its ``reason``; a file written before they existed reads as ``promote`` / ``""``.
* Promotion and rollback are LOCAL acts: this module imports no network, process or control code and reads no
  environment variable. The rule L5 (``decide_promotion``, sez. 5.1.6) says WHEN a challenger may become champion;
  ``promote`` itself is the mechanism that the rule and the local CLI (``python -m modules.dryrun.registry``) use. A
  manual ``promote`` from the CLI overrides L5 (decision U2: it prints the last L5 verdict of the version, it does not
  ask for it). The rollback exists only here and in that CLI (``--reason`` is mandatory); it moves a pointer and
  nothing else: the engine takes it up at the next reload (``learner.reload_at_utc``) and no order is opened, closed or
  modified by it. The remote control channel (sez. 6.4.2) has no command for either: ``tests/test_registry.py`` pins
  that nothing in ``modules/control`` can reach this module.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Literal, Protocol

from modules.config import GateEconConfig, LearnerConfig

__all__ = [
    "AliasState",
    "BacktestEvidence",
    "CorruptRegistry",
    "FilesystemModelRegistry",
    "GateSummary",
    "ModelRegistry",
    "NothingToRollback",
    "OosEvidence",
    "PromotionDecision",
    "PromotionEvidence",
    "PromotionRule",
    "RegisteredModel",
    "RegistryBusy",
    "RegistryError",
    "UnknownAlias",
    "UnknownVersion",
    "VersionExists",
    "artifacts_from_dir",
    "decide_promotion",
    "last_l5_verdict",
    "promote_if_eligible",
]

SCHEMA: Final = "chronos.registry.aliases.v1"
MANIFEST: Final = "manifest.json"
ALIASES: Final = "aliases.json"
LOCK_FILE: Final = ".registry.lock"
CHAMPION: Final = "champion"
CHALLENGER: Final = "challenger"
_VERSION: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$")
_ALIAS: Final = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_RESERVED_MANIFEST_KEYS: Final = ("files", "registered_at")
OPS: Final = ("promote", "rollback")
Op = Literal["promote", "rollback"]
_CHUNK: Final = 1 << 20


# --------------------------------------------------------------------------------------------- errors


class RegistryError(Exception):
    """Base of every registry failure. None of them is retried by the registry itself."""


class UnknownAlias(RegistryError):
    pass


class UnknownVersion(RegistryError):
    pass


class VersionExists(RegistryError):
    pass


class NothingToRollback(RegistryError):
    pass


class CorruptRegistry(RegistryError):
    """``aliases.json`` is unreadable, or a version does not match its manifest."""


class RegistryBusy(RegistryError):
    """The registry lock could not be taken in time."""


# ---------------------------------------------------------------------------------------------- types


@dataclass(frozen=True)
class RegisteredModel:
    """A version resolved through an alias (``alias`` is empty when the version was asked for directly)."""

    version: str
    path: Path
    manifest: dict[str, Any]
    alias: str = ""


class ModelRegistry(Protocol):
    """The contract the learner, the engine reload and the CLI rely on. The filesystem backend is one implementation;
    another (object store, database) can replace it without touching them (C8)."""

    def register(self, artifacts: Mapping[str, Path], manifest: Mapping[str, Any]) -> str: ...

    def get(self, alias: str) -> RegisteredModel: ...

    def promote(self, version: str, alias: str = CHAMPION, *, reason: str = "") -> None: ...

    def rollback(self, alias: str, *, reason: str = "") -> str: ...


@dataclass(frozen=True)
class _Entry:
    version: str
    previous: tuple[str, ...]
    updated_at: str
    op: Op = "promote"
    reason: str = ""


@dataclass(frozen=True)
class AliasState:
    """What ``aliases.json`` says about one alias (read only view for the report and the CLI)."""

    version: str
    previous: tuple[str, ...]
    updated_at: str
    op: Op
    reason: str


def _valid_version(version: object) -> str:
    if not isinstance(version, str) or not _VERSION.fullmatch(version) or version.endswith(".json"):
        raise RegistryError(f"invalid version {version!r}: letters, digits and . _ @ + - only, at most 128 characters")
    return version


def _valid_alias(alias: str) -> str:
    if not _ALIAS.fullmatch(alias):
        raise RegistryError(f"invalid alias {alias!r}: lowercase letters, digits and _, starting with a letter")
    return alias


def _valid_op(op: object) -> Op:
    if op == "promote":
        return "promote"
    if op == "rollback":
        return "rollback"
    raise RegistryError(f"invalid op {op!r}: one of {OPS}")


def _valid_artifact_name(name: str) -> str:
    parts = name.split("/")
    if not name or name.startswith("/") or "\\" in name or any(p in ("", ".", "..") for p in parts) or ":" in name:
        raise RegistryError(f"invalid artifact name {name!r}: a relative path with '/' separators and no '..'")
    if name == MANIFEST:
        raise RegistryError(f"{MANIFEST} is written by the registry, it cannot be an artifact")
    return name


def artifacts_from_dir(directory: Path, prefix: str = "") -> dict[str, Path]:
    """Every file under ``directory`` as ``{<prefix><relative posix path>: path}`` (helper for ``register``)."""
    base = prefix.rstrip("/")
    found: dict[str, Path] = {}
    for p in sorted(directory.rglob("*")):
        if p.is_file():
            rel = p.relative_to(directory).as_posix()
            found[f"{base}/{rel}" if base else rel] = p
    return found


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


# ------------------------------------------------------------------------------------------ file lock

if sys.platform == "win32":
    import msvcrt

    def _try_lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def _exclusive(path: Path, timeout_s: float) -> Iterator[None]:
    """Cross-process mutual exclusion on ``path``. The operating system drops the lock if the holder dies."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                _try_lock(fd)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RegistryBusy(f"the registry lock {path} is held by someone else") from None
                time.sleep(0.01)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


def _read_text(path: Path, attempts: int = 100) -> str:
    """Read a file that a writer may be replacing right now (on Windows the open is denied for an instant)."""
    for i in range(attempts):
        try:
            return path.read_text(encoding="utf-8")
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.01)
    raise AssertionError("unreachable")


def _replace(src: Path, dst: Path, attempts: int = 100) -> None:
    """``os.replace`` that waits out a reader holding ``dst`` open (Windows refuses to replace an open file)."""
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.01)


# ------------------------------------------------------------------------------------- the filesystem


class FilesystemModelRegistry:
    def __init__(
        self, root: Path | str, *, clock: Callable[[], datetime] = _utc_now, lock_timeout_s: float = 10.0
    ) -> None:
        self.root = Path(root)
        self._clock = clock
        self._lock_timeout = lock_timeout_s

    # ------------------------------------------------------------------------------------------ reads

    def get(self, alias: str) -> RegisteredModel:
        entry = self._read_aliases().get(_valid_alias(alias))
        if entry is None:
            raise UnknownAlias(f"alias {alias!r} is not set")
        return self._load(entry.version, alias=alias)

    def get_version(self, version: str) -> RegisteredModel:
        return self._load(_valid_version(version))

    def versions(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if p.is_dir() and (p / MANIFEST).is_file())

    def aliases(self) -> dict[str, str]:
        return {name: e.version for name, e in sorted(self._read_aliases().items())}

    def state(self, alias: str) -> AliasState:
        """The entry of ``alias`` as written: version, previous versions, when, and the last op with its reason."""
        entry = self._read_aliases().get(_valid_alias(alias))
        if entry is None:
            raise UnknownAlias(f"alias {alias!r} is not set")
        return AliasState(entry.version, entry.previous, entry.updated_at, entry.op, entry.reason)

    def history(self, alias: str) -> list[str]:
        """Versions the alias pointed at, oldest first, the current one last."""
        entry = self._read_aliases().get(_valid_alias(alias))
        if entry is None:
            raise UnknownAlias(f"alias {alias!r} is not set")
        return [*entry.previous, entry.version]

    # -------------------------------------------------------------------------------------- register

    def register(self, artifacts: Mapping[str, Path], manifest: Mapping[str, Any]) -> str:
        """Copy ``artifacts`` (name inside the version -> source file) into ``<root>/<version>/``.

        ``manifest["version"]`` names the version. The stored manifest is the given one plus ``files`` (sha256 of
        each artifact) and ``registered_at``. Raises ``VersionExists`` if the version is already there.
        """
        version = _valid_version(manifest.get("version"))
        reserved = [k for k in _RESERVED_MANIFEST_KEYS if k in manifest]
        if reserved:
            raise RegistryError(f"manifest keys {reserved} are written by the registry")
        if not artifacts:
            raise RegistryError("a version needs at least one artifact")
        names = [_valid_artifact_name(n) for n in artifacts]
        folded = {n.casefold() for n in names}
        if len(folded) != len(names):
            raise RegistryError("artifact names collide (case-insensitive file systems)")
        sources = {n: Path(artifacts[n]) for n in names}
        missing = [n for n, p in sources.items() if not p.is_file()]
        if missing:
            raise RegistryError(f"artifact sources are not files: {sorted(missing)}")
        final = self.root / version
        if final.exists():
            raise VersionExists(f"version {version!r} is already registered")

        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.root / f".tmp-{secrets.token_hex(6)}"
        tmp.mkdir()
        try:
            digests: dict[str, str] = {}
            for name, src in sources.items():
                target = tmp / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, target)
                digests[name] = _sha256_file(target)  # hash the COPY: what is stored is what is vouched for
            stored = {**manifest, "files": dict(sorted(digests.items())), "registered_at": _iso(self._clock())}
            text = json.dumps(stored, indent=2, sort_keys=True, allow_nan=False) + "\n"
            (tmp / MANIFEST).write_text(text, encoding="utf-8", newline="\n")
            try:
                os.rename(tmp, final)  # atomic; fails if the version appeared meanwhile
            except OSError as exc:
                if final.exists():
                    raise VersionExists(f"version {version!r} is already registered") from exc
                raise
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        return version

    # --------------------------------------------------------------------- promote / rollback

    def promote(self, version: str, alias: str = CHAMPION, *, reason: str = "") -> None:
        """Point ``alias`` at ``version`` (registered and intact). Promoting the current version changes nothing.
        ``reason`` is recorded with the entry (the CLI makes it mandatory)."""
        _valid_alias(alias)
        _valid_version(version)
        with self._locked():
            self._load(version)
            data = self._read_aliases()
            current = data.get(alias)
            if current is not None and current.version == version:
                return
            previous = () if current is None else (*current.previous, current.version)
            data[alias] = _Entry(version, previous, _iso(self._clock()), "promote", reason)
            self._write_aliases(data)

    def rollback(self, alias: str, *, reason: str = "") -> str:
        """Point ``alias`` back at the version it pointed at before; returns that version. Nothing else changes: no
        order, no position (the engine takes the new pointer up at its next reload)."""
        _valid_alias(alias)
        with self._locked():
            data = self._read_aliases()
            current = data.get(alias)
            if current is None:
                raise UnknownAlias(f"alias {alias!r} is not set")
            if not current.previous:
                raise NothingToRollback(f"alias {alias!r} has no previous version (it points at {current.version})")
            target = current.previous[-1]
            self._load(target)  # never roll back onto a damaged version
            data[alias] = _Entry(target, current.previous[:-1], _iso(self._clock()), "rollback", reason)
            self._write_aliases(data)
            return target

    # ------------------------------------------------------------------------------------- internals

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with _exclusive(self.root / LOCK_FILE, self._lock_timeout):
            yield

    def _load(self, version: str, *, alias: str = "") -> RegisteredModel:
        path = self.root / _valid_version(version)
        manifest_path = path / MANIFEST
        if not manifest_path.is_file():
            raise UnknownVersion(f"version {version!r} is not registered")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            files = manifest["files"]
            if not isinstance(files, dict) or manifest.get("version") != version:
                raise ValueError("files/version")
        except (ValueError, KeyError, TypeError) as exc:
            raise CorruptRegistry(f"{manifest_path} is not a valid manifest of {version!r}") from exc
        on_disk = {p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file()} - {MANIFEST}
        if on_disk != set(files):
            raise CorruptRegistry(
                f"version {version!r}: files differ from the manifest "
                f"(extra {sorted(on_disk - set(files))}, missing {sorted(set(files) - on_disk)})"
            )
        for name, digest in files.items():
            if _sha256_file(path / name) != digest:
                raise CorruptRegistry(f"version {version!r}: {name} does not match its sha256")
        return RegisteredModel(version, path, manifest, alias)

    def _read_aliases(self) -> dict[str, _Entry]:
        path = self.root / ALIASES
        try:
            text = _read_text(path)
        except FileNotFoundError:
            return {}
        try:
            doc = json.loads(text)
            if doc["schema"] != SCHEMA:
                raise ValueError("schema")
            return {
                _valid_alias(name): _Entry(
                    _valid_version(e["version"]),
                    tuple(_valid_version(v) for v in e["previous"]),
                    str(e["updated_at"]),
                    _valid_op(e.get("op", "promote")),
                    str(e.get("reason", "")),
                )
                for name, e in doc["aliases"].items()
            }
        except (ValueError, KeyError, TypeError, AttributeError, RegistryError) as exc:
            raise CorruptRegistry(f"{path} is not a valid alias file ({type(exc).__name__}: {exc})") from exc

    def _write_aliases(self, data: Mapping[str, _Entry]) -> None:
        doc = {
            "schema": SCHEMA,
            "aliases": {
                name: {
                    "version": e.version, "previous": list(e.previous), "updated_at": e.updated_at, "op": e.op,
                    "reason": e.reason,
                }
                for name, e in sorted(data.items())
            },
        }
        tmp = self.root / f".aliases.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        try:
            with tmp.open("w", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(doc, indent=2, sort_keys=True) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            _replace(tmp, self.root / ALIASES)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise


# ----------------------------------------------------------------------------- promotion rule (L5)

Verdict = Literal["promote", "reject", "insufficient"]
GeStatus = Literal["GO", "INCONCLUSO", "NO-GO"]
# The three values of ``training.evaluate.Status``: this module cannot import ``training`` (heavy, and ``modules``
# does not depend on it); ``tests/test_registry.py`` pins that the literals are the same.
GE_GO: Final = "GO"
GE_INCONCLUSIVE: Final = "INCONCLUSO"
GE_NO_GO: Final = "NO-GO"
REQUIRED_GATES: Final = ("G0", "G1", "G2", "G4")  # G3 is required too when the version reports it (an LLM backend)


@dataclass(frozen=True)
class BacktestEvidence:
    """Walk-forward backtest of one model over the COMMON recent window (sez. 5.1.6, L5 P3). A model that does not
    trade in the window has defined metrics: PnL 0, Sharpe 0.0, drawdown 0 (``n_trades == 0``)."""

    window_start: datetime
    window_end: datetime
    window_days: float
    n_trades: int
    net_pnl: Decimal
    sharpe: float | None  # 0.0 when n_trades == 0; None only with trades and not defined (too few observations)
    max_dd_frac: float


@dataclass(frozen=True)
class GateSummary:
    """The gates of ``evaluation.json`` of a version: ``ge`` (None: the file is missing) and ``passed`` for G0, G1, G2,
    G4 (and G3 when applicable); a value of None means "missing"."""

    ge: GeStatus | None
    passed: Mapping[str, bool | None]


@dataclass(frozen=True)
class OosEvidence:
    """The common out-of-sample walk-forward of the two models (P2, S3). The champion fields are None when the
    champion is not GE = GO (the paired part is not computed then)."""

    common_days: float
    n_trades_ch: int
    max_dd_ch: float
    n_trades_cp: int | None
    d_pnl_quantile: float | None  # quantile ``superiority_quantile`` of D = PnL_ch - PnL_cp (EUR), paired bootstrap
    sharpe_ch: float | None
    sharpe_cp: float | None
    max_dd_cp: float | None


@dataclass(frozen=True)
class PromotionEvidence:
    challenger: BacktestEvidence
    champion: BacktestEvidence
    challenger_gates: GateSummary
    champion_gates: GateSummary
    oos: OosEvidence | None  # None: the OOS artifacts of the challenger are missing
    oos_provable: bool  # V1: both models trained on data that ends before the window starts (minus the embargo)
    shadow_days: float  # days the challenger has run as a dry book (P4)
    operational_anomalies: int
    parity_anomalies: int  # includes R1-R3 of the version (spec 5.1.7)


@dataclass(frozen=True)
class PromotionRule:
    backtest_recent_days: int = 90
    min_shadow_days: int = 14
    min_recent_trades: int = 30
    max_drawdown_frac: float = 0.05
    superiority_quantile: float = 0.05

    @classmethod
    def from_config(cls, learner: LearnerConfig, econ: GateEconConfig) -> PromotionRule:
        """Windows and quantile from ``dry_run.learner.promotion``; the minimum of trades and the drawdown limit from
        ``dry_run.gate_econ`` (one number for L5 and for G-DRY-ECON)."""
        p = learner.promotion
        return cls(
            backtest_recent_days=p.backtest_recent_days, min_shadow_days=p.min_shadow_days,
            min_recent_trades=econ.min_trades, max_drawdown_frac=econ.max_drawdown_frac,
            superiority_quantile=p.superiority_quantile,
        )  # fmt: skip


@dataclass(frozen=True)
class PromotionDecision:
    verdict: Verdict
    reasons: tuple[str, ...] = field(default=())

    @property
    def approved(self) -> bool:
        return self.verdict == "promote"


def _finite(x: float | None) -> bool:
    return x is not None and math.isfinite(x)


def _sharpe(bt: BacktestEvidence) -> float | None:
    """A model without trades has Sharpe 0.0 (a flat return series), whatever the backtest reported."""
    return 0.0 if bt.n_trades == 0 and bt.sharpe is None else bt.sharpe


def decide_promotion(evidence: PromotionEvidence, rule: PromotionRule | None = None) -> PromotionDecision:
    """Rule L5 (spec 5.1.6; resolution P1e, B.3). The default is to KEEP the champion: the challenger becomes champion
    only if every condition holds, evaluated in this fixed order.

    1. definitive failures (always evaluated): GE = NO-GO or a failed gate G0/G1/G2/G3/G4 of the challenger, any
       operational anomaly, any parity anomaly (R1-R4 of the version included);
    2. sufficiency (P0 V1-V3, P1, P4): an evaluation of the challenger with GE = GO and every gate present, a common
       window of at least ``backtest_recent_days`` for both models, the same window, an out-of-sample proof (V1), at
       least ``min_recent_trades`` trades of the challenger in the window, defined Sharpe ratios, at least
       ``min_shadow_days`` of shadow, the OOS artifacts (paired ones too when the champion is GE = GO);
    3. recency (P3), strict comparisons: challenger PnL > 0 and > champion PnL, Sharpe > champion Sharpe, drawdown <=
       champion drawdown (only if the champion traded in the window);
    4. drawdown <= ``max_drawdown_frac`` in the window and in the OOS (S3);
    5. superiority (P2), only when the champion is GE = GO too: the ``superiority_quantile`` quantile of the paired
       block bootstrap of the PnL difference > 0, OOS Sharpe higher, OOS drawdown not worse.

    ``reject``: the evidence is there and says no (every failed condition is listed). ``insufficient``: not enough
    evidence to decide either way. A definitive failure wins over an insufficiency; both lists are reported.
    The PnL of the dry books after 14 days is NOT a criterion (sez. 5.1.6: it is noise).
    """
    rule = rule or PromotionRule()
    ch, cp, oos = evidence.challenger, evidence.champion, evidence.oos
    gates = evidence.challenger_gates
    reject: list[str] = []
    short: list[str] = []

    # 1. definitive
    ge = gates.ge
    if ge == GE_NO_GO:
        reject.append("challenger_ge_nogo")
    reject += [f"challenger_{k}_failed" for k, v in sorted(gates.passed.items()) if v is False]
    if evidence.operational_anomalies > 0:
        reject.append("operational_anomalies")
    if evidence.parity_anomalies > 0:
        reject.append("parity_anomalies")

    # 2. sufficiency
    if ge is None:
        short.append("challenger_evaluation_missing")
    elif ge == GE_INCONCLUSIVE:
        short.append("challenger_ge_inconclusive")
    required = (*REQUIRED_GATES, *(("G3",) if "G3" in gates.passed else ()))
    short += [f"challenger_{k}_missing" for k in required if gates.passed.get(k) is None]
    for label, bt in (("challenger", ch), ("champion", cp)):
        if not math.isfinite(bt.window_days) or bt.window_days < rule.backtest_recent_days:
            short.append(f"{label}_backtest_window_short")
    if (ch.window_start, ch.window_end) != (cp.window_start, cp.window_end):
        short.append("backtest_windows_differ")
    if not evidence.oos_provable:
        short.append("oos_not_provable")
    if ch.n_trades < rule.min_recent_trades:
        short.append("challenger_recent_trades_short")
    for label, bt in (("challenger", ch), ("champion", cp)):
        if bt.n_trades > 0 and not _finite(_sharpe(bt)):
            short.append(f"{label}_sharpe_undefined")
    if not math.isfinite(evidence.shadow_days) or evidence.shadow_days < rule.min_shadow_days:
        short.append("shadow_days_short")
    champion_go = evidence.champion_gates.ge == GE_GO
    if oos is None:
        short.append("oos_unavailable")
    elif champion_go and not all(_finite(x) for x in (oos.d_pnl_quantile, oos.sharpe_ch, oos.sharpe_cp, oos.max_dd_cp)):
        short.append("paired_oos_unavailable")
    if reject:
        return PromotionDecision("reject", (*reject, *short))
    if short:
        return PromotionDecision("insufficient", tuple(short))

    # 3. recency (P3): strict comparisons; a model without trades has PnL 0 / Sharpe 0 / drawdown 0
    assert oos is not None  # "oos_unavailable" above
    ch_sharpe, cp_sharpe = _sharpe(ch), _sharpe(cp)
    assert ch_sharpe is not None and cp_sharpe is not None  # sufficiency above
    if not ch.net_pnl > 0:
        reject.append("recent_pnl_not_positive")
    if not ch.net_pnl > cp.net_pnl:
        reject.append("recent_pnl_not_better")
    if not ch_sharpe > cp_sharpe:
        reject.append("recent_sharpe_not_better")
    if cp.n_trades > 0 and not ch.max_dd_frac <= cp.max_dd_frac:
        reject.append("recent_max_dd_worse")
    # 4. absolute drawdown (S3)
    if ch.max_dd_frac > rule.max_drawdown_frac:
        reject.append("recent_max_dd_above_limit")
    if oos.max_dd_ch > rule.max_drawdown_frac:
        reject.append("oos_max_dd_above_limit")
    # 5. superiority on the common OOS (P2), only against a champion that is GO itself
    if champion_go:
        assert oos.d_pnl_quantile is not None and oos.sharpe_ch is not None  # "paired_oos_unavailable" above
        assert oos.sharpe_cp is not None and oos.max_dd_cp is not None
        if not oos.d_pnl_quantile > 0:
            reject.append("oos_pnl_difference_not_significant")
        if not oos.sharpe_ch > oos.sharpe_cp:
            reject.append("oos_sharpe_not_better")
        if not oos.max_dd_ch <= oos.max_dd_cp:
            reject.append("oos_max_dd_worse")
    return PromotionDecision("reject", tuple(reject)) if reject else PromotionDecision("promote")


def promote_if_eligible(
    registry: ModelRegistry, version: str, evidence: PromotionEvidence, rule: PromotionRule | None = None
) -> PromotionDecision:
    """Apply L5 and, only if it approves, ``registry.promote(version, "champion")``. The nightly learner never calls
    this (it reports the verdict only); whoever does is a local, explicit act."""
    decision = decide_promotion(evidence, rule)
    if decision.approved:
        registry.promote(version, CHAMPION, reason="L5 promote")
    return decision


def last_l5_verdict(reports_dir: Path | str, version: str) -> dict[str, Any] | None:
    """The most recent L5 verdict the nightly learner wrote about ``version`` as the evaluated challenger
    (``data/reports/learner/DAY.json``, step L5), or None. Read only: the CLI prints it before a manual promotion."""
    folder = Path(reports_dir)
    for path in sorted(folder.glob("*.json"), reverse=True) if folder.is_dir() else []:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            details = doc["steps"]["L5"]["details"]
            if details["challenger"] == version and "verdict" in details:
                return {"date": doc["date"], "verdict": details["verdict"], "reasons": details.get("reasons", [])}
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return None


# ------------------------------------------------------------------------------------------------ CLI


def _json(obj: object) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, default=str)


def _effective_note() -> str:
    return (
        f"effective at the next reload ({LearnerConfig().reload_at_utc} UTC), not intra-day; "
        "no order is opened, closed or modified by this command"
    )


def _reason(parser: argparse.ArgumentParser, text: str) -> str:
    if not text.strip():
        parser.error("--reason must not be empty")
    return text.strip()


def main(argv: Sequence[str] | None = None) -> int:
    """Local command line: ``python -m modules.dryrun.registry [--root DIR] list|show|promote|rollback``.

    ``promote`` and ``rollback`` need ``--reason`` (it is recorded in ``aliases.json``). A manual ``promote`` overrides
    rule L5 (decision U2): it prints the last L5 verdict of the version and goes on. ``rollback`` defaults to the
    ``champion`` alias and takes effect at the next reload, never intra-day."""
    parser = argparse.ArgumentParser(
        prog="python -m modules.dryrun.registry", description="Local model registry (promotion is a human decision)."
    )
    parser.add_argument("--root", default="models/registry")
    parser.add_argument("--learner-reports", default="data/reports/learner", help="where the nightly L5 verdicts are")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="versions and aliases")
    show = sub.add_parser("show", help="the version an alias points at, with its history")
    show.add_argument("alias")
    promote = sub.add_parser("promote", help="point an alias at a registered version (a human decision: bypasses L5)")
    promote.add_argument("version")
    promote.add_argument("--alias", default=CHAMPION)
    promote.add_argument("--reason", required=True, help="why (recorded in aliases.json)")
    rollback = sub.add_parser("rollback", help="point an alias back at its previous version")
    rollback.add_argument("alias", nargs="?", default=CHAMPION)
    rollback.add_argument("--reason", required=True, help="why (recorded in aliases.json)")
    args = parser.parse_args(argv)
    reason = _reason(parser, args.reason) if args.command in ("promote", "rollback") else ""

    registry = FilesystemModelRegistry(args.root)
    try:
        if args.command == "list":
            print(_json({"versions": registry.versions(), "aliases": registry.aliases()}))
        elif args.command == "show":
            model = registry.get(args.alias)
            st = registry.state(args.alias)
            print(_json({
                "alias": args.alias, "version": model.version, "history": registry.history(args.alias),
                "updated_at": st.updated_at, "op": st.op, "reason": st.reason,
            }))  # fmt: skip
        elif args.command == "promote":
            verdict = last_l5_verdict(args.learner_reports, args.version)
            registry.promote(args.version, args.alias, reason=reason)
            print(_json({
                "alias": args.alias, "version": args.version, "op": "promote", "reason": reason,
                "last_l5_verdict": verdict, "note": _effective_note(),
            }))  # fmt: skip
        else:
            target = registry.rollback(args.alias, reason=reason)
            print(_json({
                "alias": args.alias, "version": target, "op": "rollback", "reason": reason, "note": _effective_note(),
            }))  # fmt: skip
    except RegistryError as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
