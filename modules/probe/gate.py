"""The enabling of the real run (plan sez. 3.5): four acts, all checked BEFORE the exchange, the ledger or the
gateway is built. If one is missing the process exits with code 2 and has touched neither the network nor the
credentials' host.

1. ``exec_probe.enabled: true`` in the config given with ``--config``;
2. ``CHRONOS_LIVE_CONFIRM=I_ACCEPT_REAL_ORDERS`` and ``CHRONOS_EXEC_PROBE_CONFIRM=<product>:<L_tot>`` (e.g.
   ``BTC-EUR:3.00``), equal to the config, in the shell of the probe only;
3. the credentials in a ``--key-file`` OUTSIDE the repository (a path that resolves under the repo root is refused), and
   no ``COINBASE_API_KEY`` / ``COINBASE_API_SECRET`` in the environment (a second key of the dry run must not win);
4. a terminal on ``stdin`` and the typed phrase ``CONFERMO <product> <L_tot> EUR``. There is no ``--yes``.

A stage that cannot send an order (``preflight``, ``report``) needs the acts 1 and 3 only.

The credentials are read with ``dotenv_values`` (a ``.env``-style file) or from the JSON that the Coinbase console
downloads (``name`` and ``privateKey``) and handed explicitly to ``CoinbaseConfig``: the ``.env`` of the repo is never
read for the probe. Nothing here prints a secret; the key is shown as ``sha256(api_key)[:8]``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Final

from dotenv import dotenv_values
from pydantic import SecretStr

from modules.config import LIVE_CONFIRM_ENV, LIVE_CONFIRM_VALUE, CoinbaseConfig, ExecProbeConfig

PROBE_CONFIRM_ENV: Final = "CHRONOS_EXEC_PROBE_CONFIRM"
FORBIDDEN_ENV: Final = ("COINBASE_API_KEY", "COINBASE_API_SECRET")
EXIT_GATE: Final = 2


class GateRefused(Exception):
    """One of the acts of the enabling is missing. ``str(e)`` says which, without any secret."""


def check_gate(
    cfg: ExecProbeConfig,
    env: Mapping[str, str],
    key_file: str | Path | None,
    repo_root: Path,
    *,
    can_send_orders: bool,
) -> Path:
    """Acts 1-3. Returns the resolved key file. Pure with respect to the network and to the credentials."""
    if not cfg.enabled:
        raise GateRefused("exec_probe.enabled is false in the config: the probe stays off")
    if can_send_orders:
        if env.get(LIVE_CONFIRM_ENV) != LIVE_CONFIRM_VALUE:
            raise GateRefused(f"{LIVE_CONFIRM_ENV} is not set to the exact confirmation value")
        if env.get(PROBE_CONFIRM_ENV) != cfg.confirm_value:
            raise GateRefused(
                f"{PROBE_CONFIRM_ENV} must be exactly {cfg.confirm_value!r} (product:total loss of the config)"
            )
    if key_file is None:
        raise GateRefused("--key-file is required: the credentials live in a file outside the repo")
    path = Path(key_file).expanduser().resolve()
    if not path.is_file():
        raise GateRefused(f"key file {path} does not exist or is not a file")
    root = repo_root.resolve()
    if path == root or root in path.parents:
        raise GateRefused(f"key file {path} is inside the repository {root}: move it outside")
    present = [name for name in FORBIDDEN_ENV if name in env]
    if present:
        raise GateRefused(
            f"{', '.join(present)} found in the environment: the probe takes its key only from --key-file"
        )
    return path


def require_terminal(stdin: IO[str]) -> None:
    """Act 4, first half: the confirmation is typed by a person at a terminal."""
    isatty = getattr(stdin, "isatty", None)
    if isatty is None or not isatty():
        raise GateRefused("stdin is not a terminal: the confirmation must be typed (there is no --yes)")


def confirm_phrase(cfg: ExecProbeConfig) -> str:
    return f"CONFERMO {cfg.product} {cfg.max_total_loss_quote:.2f} EUR"


def ask_confirmation(cfg: ExecProbeConfig, summary: list[str], stdin: IO[str], stdout: IO[str]) -> bool:
    """Act 4, second half: show what is about to happen and read the phrase. Exact match, nothing else is accepted."""
    stdout.write("\n".join(summary) + "\n")
    stdout.write(f"\nPer procedere scrivi esattamente:  {confirm_phrase(cfg)}\n> ")
    stdout.flush()
    return stdin.readline().strip() == confirm_phrase(cfg)


# ------------------------------------------------------------------------------------------- credentials


def read_credentials(path: Path) -> CoinbaseConfig:
    """``CoinbaseConfig`` from the key file, never from the environment or from the repo's ``.env``."""
    text = path.read_text(encoding="utf-8")
    key: str | None
    secret: str | None
    if text.lstrip().startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise GateRefused(f"key file {path.name} is not valid JSON ({exc.msg})") from exc
        key = data.get("name") or data.get("api_key") or data.get("COINBASE_API_KEY")
        secret = data.get("privateKey") or data.get("api_secret") or data.get("COINBASE_API_SECRET")
    else:
        values = dotenv_values(path)
        key, secret = values.get("COINBASE_API_KEY"), values.get("COINBASE_API_SECRET")
    if not key or not secret:
        raise GateRefused(f"key file {path.name} lacks COINBASE_API_KEY / COINBASE_API_SECRET (or name / privateKey)")
    return CoinbaseConfig(_env_file=None, api_key=SecretStr(key), api_secret=SecretStr(secret))  # type: ignore[call-arg]


def key_fingerprint(cfg: CoinbaseConfig) -> str:
    """``sha256(api_key)[:8]``: enough to recognise the key, useless to anyone else."""
    assert cfg.api_key is not None
    return hashlib.sha256(cfg.api_key.get_secret_value().encode("utf-8")).hexdigest()[:8]


def secrets_of(cfg: CoinbaseConfig) -> list[str]:
    """Every form in which the credentials could appear in a log line or a message (for the redaction)."""
    out: list[str] = []
    if cfg.api_key is not None:
        out.append(cfg.api_key.get_secret_value())
    if cfg.api_secret is not None:
        pem = cfg.api_secret.get_secret_value()
        out += [pem, pem.replace("\n", "\\n"), pem.replace("\n", "\\\\n")]
        out += [line.strip() for line in pem.splitlines() if len(line.strip()) >= 20]
    return out


@dataclass(frozen=True, slots=True)
class Credentials:
    config: CoinbaseConfig
    fingerprint: str
    secrets: list[str]
