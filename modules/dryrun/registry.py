"""ModelRegistry: versioned model artifacts and the aliases that point at them (spec sez. 5.1.6, 5.3 punto 12; C8).

Layout (``models/registry/``)::

    <version>/manifest.json         the manifest given to ``register`` plus ``files`` (sha256 of every artifact)
    <version>/<artifact files>      copied, never modified afterwards
    aliases.json                    {"champion": {"version", "previous": [...], "updated_at"}, "challenger": ...}

Rules:

* A version is IMMUTABLE. ``register`` builds it in a temporary directory and publishes it with one rename, so a
  version is either absent or complete; registering an existing version is refused. ``get`` and ``promote`` re-hash
  the files against the manifest and refuse a version that was touched or is incomplete.
* An alias is a pointer. ``aliases.json`` is written to a temporary file and moved over the old one with
  ``os.replace`` (readers see the old or the new file, never half of one); the read-modify-write of promote and
  rollback runs under an exclusive file lock, so concurrent processes (the nightly learner and the CLI) cannot lose
  an update. A corrupt ``aliases.json`` is an error, never "no aliases".
* ``previous`` is the stack of versions the alias pointed at before. ``rollback`` pops it: rolling back twice goes two
  versions back; promoting again pushes the current version.
* Promotion is a LOCAL act: this module imports no network, process or control code and reads no environment variable.
  The rule L5 (``decide_promotion``) says WHEN a challenger may become champion; ``promote`` itself is the mechanism
  that the rule and the local CLI (``python -m modules.dryrun.registry``) use. The remote control channel (sez. 6.4.2)
  has no command for either: ``tests/test_registry.py`` pins that nothing in ``modules/control`` can reach this module.
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

from modules.config import LearnerConfig

__all__ = [
    "BacktestEvidence",
    "CorruptRegistry",
    "FilesystemModelRegistry",
    "ModelRegistry",
    "NothingToRollback",
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

    def promote(self, version: str, alias: str = CHAMPION) -> None: ...

    def rollback(self, alias: str) -> str: ...


@dataclass(frozen=True)
class _Entry:
    version: str
    previous: tuple[str, ...]
    updated_at: str


def _valid_version(version: object) -> str:
    if not isinstance(version, str) or not _VERSION.fullmatch(version) or version.endswith(".json"):
        raise RegistryError(f"invalid version {version!r}: letters, digits and . _ @ + - only, at most 128 characters")
    return version


def _valid_alias(alias: str) -> str:
    if not _ALIAS.fullmatch(alias):
        raise RegistryError(f"invalid alias {alias!r}: lowercase letters, digits and _, starting with a letter")
    return alias


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

    def promote(self, version: str, alias: str = CHAMPION) -> None:
        """Point ``alias`` at ``version`` (registered and intact). Promoting the current version changes nothing."""
        _valid_alias(alias)
        _valid_version(version)
        with self._locked():
            self._load(version)
            data = self._read_aliases()
            current = data.get(alias)
            if current is not None and current.version == version:
                return
            previous = () if current is None else (*current.previous, current.version)
            data[alias] = _Entry(version, previous, _iso(self._clock()))
            self._write_aliases(data)

    def rollback(self, alias: str) -> str:
        """Point ``alias`` back at the version it pointed at before; returns that version."""
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
            data[alias] = _Entry(target, current.previous[:-1], _iso(self._clock()))
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
                )
                for name, e in doc["aliases"].items()
            }
        except (ValueError, KeyError, TypeError, AttributeError, RegistryError) as exc:
            raise CorruptRegistry(f"{path} is not a valid alias file ({type(exc).__name__}: {exc})") from exc

    def _write_aliases(self, data: Mapping[str, _Entry]) -> None:
        doc = {
            "schema": SCHEMA,
            "aliases": {
                name: {"version": e.version, "previous": list(e.previous), "updated_at": e.updated_at}
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


@dataclass(frozen=True)
class BacktestEvidence:
    """Walk-forward backtest of one model over the most recent days (sez. 5.1.6, L5 (a))."""

    window_days: float
    net_pnl: Decimal
    sharpe: float | None  # None: not defined (too few observations)
    max_dd_frac: float


@dataclass(frozen=True)
class PromotionEvidence:
    challenger: BacktestEvidence
    champion: BacktestEvidence
    shadow_days: float  # days the challenger has run as a dry book (L5 (b))
    operational_anomalies: int
    parity_anomalies: int


@dataclass(frozen=True)
class PromotionRule:
    backtest_recent_days: int = 90
    min_shadow_days: int = 14

    @classmethod
    def from_config(cls, cfg: LearnerConfig) -> PromotionRule:
        return cls(cfg.promotion["backtest_recent_days"], cfg.promotion["min_shadow_days"])


@dataclass(frozen=True)
class PromotionDecision:
    verdict: Verdict
    reasons: tuple[str, ...] = field(default=())

    @property
    def approved(self) -> bool:
        return self.verdict == "promote"


def _finite(x: float | None) -> bool:
    return x is not None and math.isfinite(x)


def decide_promotion(evidence: PromotionEvidence, rule: PromotionRule | None = None) -> PromotionDecision:
    """Rule L5: the challenger becomes champion only if

    (a) it beats the champion in the walk-forward backtest over the last ``backtest_recent_days``: net PnL strictly
        higher, Sharpe strictly higher, max drawdown not worse (<=), AND
    (b) it has run as the ``challenger`` dry book for at least ``min_shadow_days`` with no operational and no parity
        anomaly. The PnL of the dry books after 14 days is NOT a criterion (sez. 5.1.6: it is noise).

    ``insufficient`` means the evidence is not enough to decide either way (a short window, an undefined Sharpe, too
    few shadow days): nothing is promoted and the caller tries again later. ``reject`` means the evidence is there
    and says no. Every failed condition is listed in ``reasons``.
    """
    rule = rule or PromotionRule()
    short: list[str] = []
    for label, bt in (("challenger", evidence.challenger), ("champion", evidence.champion)):
        if not math.isfinite(bt.window_days) or bt.window_days < rule.backtest_recent_days:
            short.append(f"{label}_backtest_window_short")
        if not _finite(bt.sharpe):
            short.append(f"{label}_sharpe_undefined")
    if not math.isfinite(evidence.shadow_days) or evidence.shadow_days < rule.min_shadow_days:
        short.append("shadow_days_short")
    if short:
        return PromotionDecision("insufficient", tuple(short))

    ch, cp = evidence.challenger, evidence.champion
    assert ch.sharpe is not None and cp.sharpe is not None  # _finite() above
    failed: list[str] = []
    if not ch.net_pnl > cp.net_pnl:
        failed.append("net_pnl_not_better")
    if not ch.sharpe > cp.sharpe:
        failed.append("sharpe_not_better")
    if not ch.max_dd_frac <= cp.max_dd_frac:
        failed.append("max_dd_worse")
    if evidence.operational_anomalies > 0:
        failed.append("operational_anomalies")
    if evidence.parity_anomalies > 0:
        failed.append("parity_anomalies")
    if failed:
        return PromotionDecision("reject", tuple(failed))
    return PromotionDecision("promote")


def promote_if_eligible(
    registry: ModelRegistry, version: str, evidence: PromotionEvidence, rule: PromotionRule | None = None
) -> PromotionDecision:
    """Apply L5 and, only if it approves, ``registry.promote(version, "champion")``."""
    decision = decide_promotion(evidence, rule)
    if decision.approved:
        registry.promote(version, CHAMPION)
    return decision


# ------------------------------------------------------------------------------------------------ CLI


def _json(obj: object) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, default=str)


def main(argv: Sequence[str] | None = None) -> int:
    """Local command line: ``python -m modules.dryrun.registry [--root DIR] list|show|promote|rollback``."""
    parser = argparse.ArgumentParser(
        prog="python -m modules.dryrun.registry", description="Local model registry (promotion is a human decision)."
    )
    parser.add_argument("--root", default="models/registry")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="versions and aliases")
    show = sub.add_parser("show", help="the version an alias points at, with its history")
    show.add_argument("alias")
    promote = sub.add_parser("promote", help="point an alias at a registered version (a human decision: bypasses L5)")
    promote.add_argument("version")
    promote.add_argument("--alias", default=CHAMPION)
    rollback = sub.add_parser("rollback", help="point an alias back at its previous version")
    rollback.add_argument("alias")
    args = parser.parse_args(argv)

    registry = FilesystemModelRegistry(args.root)
    try:
        if args.command == "list":
            print(_json({"versions": registry.versions(), "aliases": registry.aliases()}))
        elif args.command == "show":
            model = registry.get(args.alias)
            print(_json({"alias": args.alias, "version": model.version, "history": registry.history(args.alias)}))
        elif args.command == "promote":
            registry.promote(args.version, args.alias)
            print(_json({"alias": args.alias, "version": args.version}))
        else:
            print(_json({"alias": args.alias, "version": registry.rollback(args.alias)}))
    except RegistryError as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

