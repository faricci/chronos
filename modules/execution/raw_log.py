"""Redaction and the raw record of the ``user`` channel, shared by ``script/user_channel_soak.py`` and the P4-pre probe.

``redact`` removes every credential-looking string (known secrets, JWTs, PEM blocks, key names, bearer tokens,
credential fields of a JSON object). ``RawLog`` is the gzip JSON-lines envelope that keeps every raw message AFTER
the redaction: a secret never reaches a file. Nothing here imports the Coinbase SDK or touches the network.
"""

from __future__ import annotations

import gzip
import json
import re
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, Final

from modules.config import CoinbaseConfig

RAW_NAME: Final = "user_channel_raw.jsonl.gz"
REDACTED: Final = "[REDACTED]"
FLUSH_EVERY_S: Final = 5.0

_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}(?:\.[A-Za-z0-9_-]*)?")
_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)
_KEY_NAME = re.compile(r"organizations/[\w-]+/api[Kk]eys/[\w-]+")
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")
_SECRET_FIELD = re.compile(
    r'("(?:jwt|token|access_token|id_token|signature|api_key|api_secret|private_key|secret|retail_portfolio_id)"'
    r'\s*:\s*")[^"]*(")',
    re.I,
)


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Remove every credential-looking string from ``text``: known secrets first, then JWTs, PEM blocks, key names,
    bearer tokens and credential fields of a JSON object."""
    for secret in sorted(set(secrets), key=len, reverse=True):
        if len(secret) >= 8:
            text = text.replace(secret, REDACTED)
    text = _PEM.sub(REDACTED, text)
    text = _JWT.sub(REDACTED, text)
    text = _KEY_NAME.sub(REDACTED, text)
    text = _BEARER.sub(r"\1" + REDACTED, text)
    return _SECRET_FIELD.sub(r"\1" + REDACTED + r"\2", text)


def secrets_of(cfg: CoinbaseConfig) -> list[str]:
    """Every form in which the credentials could show up in a message or a log line."""
    out: list[str] = []
    if cfg.api_key is not None:
        out.append(cfg.api_key.get_secret_value())
    if cfg.api_secret is not None:
        pem = cfg.api_secret.get_secret_value()
        out += [pem, pem.replace("\n", "\\n"), pem.replace("\n", "\\\\n")]
        out += [line.strip() for line in pem.splitlines() if len(line.strip()) >= 20]
    return out


class RawLog:
    """Gzip JSON lines in the output folder. Flushed (sync flush) every few seconds, so a killed process leaves a
    readable file up to the last flush. A second run in the same folder opens a ``.partN`` file."""

    def __init__(self, folder: Path, clock: Callable[[], float] = time.time) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / RAW_NAME
        part = 1
        while path.exists():
            part += 1
            path = folder / f"{RAW_NAME}.part{part}"
        self.path = path
        self._clock = clock
        self._file = gzip.open(path, "wt", encoding="utf-8")
        self._last_flush = clock()

    def write(self, entry: Mapping[str, Any]) -> None:
        if self._file.closed:  # a straggler from the SDK thread after the run ended
            return
        self._file.write(json.dumps(entry, separators=(",", ":")) + "\n")
        now = self._clock()
        if now - self._last_flush >= FLUSH_EVERY_S:
            self._file.flush()
            self._last_flush = now

    def flush(self) -> None:
        """Make everything written so far readable from another handle (a stage reads the file back)."""
        if not self._file.closed:
            self._file.flush()
            self._last_flush = self._clock()

    def close(self) -> None:
        self._file.close()
