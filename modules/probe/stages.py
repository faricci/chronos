"""The stages that come before the first real entry (plan sez. 4) and their outcome files.

Every stage writes ``<out_dir>/<stage>.json`` with ``ok``. A stage starts only if the previous one is ``ok``, with
the same config hash and on the same UTC day; the ``live`` stage accepts a ``reject`` outcome at most
``LIVE_REJECT_MAX_AGE_DAYS`` old. The files are the memory of the sequence: a changed config, a new day or a
missing stage sends the operator back to the previous step, never forward.

The stage actions themselves (``place-cancel``, ``expire``, ``reject``) are plain intents run through the gateway like
any other entry; this module holds only what is common: the files, the chain, and the order intents of the stages.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Literal

from modules.config import ExecProbeConfig
from modules.execution.orders import ProductMeta
from modules.execution.rounding import ceil_to, floor_to
from modules.probe import limits
from modules.risk.risk_manager import OrderIntent

STAGES: Final = ("preflight", "preview", "place-cancel", "expire", "reject", "live")
PREVIOUS: Final = dict(zip(STAGES[1:], STAGES[:-1], strict=True))
EntryMode = Literal["gtd", "gtc_fallback"]
TEST_DECISION_PREFIX: Final = "probe-test"  # never counted as an entry of the probe


class StageBlocked(Exception):
    """The previous stage is missing, failed, stale or was run with another config."""


def config_hash(cfg: ExecProbeConfig) -> str:
    """sha256 of the probe section of the config, canonical JSON. Any change of a limit changes the hash."""
    payload = json.dumps(cfg.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class StageRecord:
    stage: str
    ok: bool
    at: datetime
    config_sha256: str
    entry_mode: EntryMode | None = None  # set by ``preview``: GTD, or the GTC fallback the user did not accept
    skipped: bool = False  # ``expire`` with the GTC fallback: not applicable
    details: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "stage": self.stage, "ok": self.ok, "at": self.at.astimezone(UTC).isoformat(),
                "day": self.at.astimezone(UTC).date().isoformat(), "config_sha256": self.config_sha256,
                "entry_mode": self.entry_mode, "skipped": self.skipped, "details": self.details,
            },
            indent=2, sort_keys=True, default=str,
        )  # fmt: skip

    @classmethod
    def from_json(cls, text: str) -> StageRecord:
        d = json.loads(text)
        return cls(
            d["stage"], bool(d["ok"]), datetime.fromisoformat(d["at"]), d["config_sha256"], d.get("entry_mode"),
            bool(d.get("skipped", False)), d.get("details") or {},
        )  # fmt: skip


def stage_path(out_dir: Path, stage: str) -> Path:
    return out_dir / f"{stage}.json"


def write_stage(out_dir: Path, record: StageRecord) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = stage_path(out_dir, record.stage)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(record.to_json(), encoding="utf-8")
    tmp.replace(path)
    return path


def read_stage(out_dir: Path, stage: str) -> StageRecord | None:
    path = stage_path(out_dir, stage)
    if not path.is_file():
        return None
    try:
        return StageRecord.from_json(path.read_text(encoding="utf-8"))
    except (ValueError, KeyError, TypeError):
        return None


def check_chain(out_dir: Path, stage: str, cfg: ExecProbeConfig, now: datetime) -> StageRecord | None:
    """Raise :class:`StageBlocked` unless ``stage`` may start. Returns the ``preview`` record when one is needed (for
    the entry mode), else ``None``. ``preflight`` has no predecessor."""
    if stage not in STAGES:
        raise StageBlocked(f"unknown stage {stage!r}")
    sha = config_hash(cfg)
    today = now.astimezone(UTC).date()
    if stage == "preflight":
        return None
    previous = PREVIOUS[stage]
    prev = read_stage(out_dir, previous)
    if prev is None or not prev.ok:
        raise StageBlocked(f"stage {previous!r} has no ok outcome: run it first")
    if prev.config_sha256 != sha:
        raise StageBlocked(f"stage {previous!r} was run with another config: run it again")
    if stage == "live":
        age = now - prev.at
        if age > timedelta(days=limits.LIVE_REJECT_MAX_AGE_DAYS) or age < timedelta(0):
            raise StageBlocked(
                f"the 'reject' outcome is older than {limits.LIVE_REJECT_MAX_AGE_DAYS} days: run it again"
            )
        for earlier in STAGES[: STAGES.index("reject")]:
            rec = read_stage(out_dir, earlier)
            if rec is None or not rec.ok or rec.config_sha256 != sha:
                raise StageBlocked(f"stage {earlier!r} has no ok outcome with this config")
    elif prev.at.astimezone(UTC).date() != today:
        raise StageBlocked(f"stage {previous!r} was run on {prev.at.astimezone(UTC).date()}, not today: run it again")
    preview = read_stage(out_dir, "preview") if stage != "preview" else None
    if stage in ("expire", "live") and (preview is None or preview.entry_mode is None):
        raise StageBlocked("the 'preview' outcome does not say whether the entry is GTD")
    if stage == "expire" and preview is not None and preview.entry_mode != "gtd":
        raise StageBlocked("the 'expire' stage exists only for a GTD entry")
    if stage == "live" and preview is not None and preview.entry_mode != "gtd" and not cfg.accept_gtc_fallback:
        raise StageBlocked(
            "the exchange does not accept the GTD entry and the user has not accepted the GTC fallback in writing "
            "(exec_probe.accept_gtc_fallback): the probe stops here and the decision goes back to the user"
        )
    return preview


# ------------------------------------------------------------------------------------------ stage intents


def _base_intent(
    decision: str, product: str, meta: ProductMeta, limit_price: Decimal, size: Decimal, tif_s: int,
    now: datetime, *, expires_on_exchange: bool,
) -> OrderIntent:  # fmt: skip
    """An entry intent for a stage order: neutral risk block, a bracket far from the entry that is never placed unless
    the order fills (then it is a normal entry with a bracket, 2 EUR being above the protectable notional)."""
    qinc = meta.quote_increment
    tp = floor_to(limit_price * (1 + limits.TP_FRAC), qinc)
    sl = ceil_to(limit_price * (1 - limits.SL_FRAC), qinc)
    return OrderIntent.model_validate(
        {
            "intent_id": decision, "decision_id": decision, "product_id": product, "side": "BUY", "book": limits.BOOK,
            "entry": {
                "type": "limit_post_only", "limit_price": limit_price, "time_in_force_s": tif_s,
                "expires_on_exchange": expires_on_exchange,
            },
            "base_size": size,
            "bracket": {
                "take_profit": tp, "stop_loss": sl, "k_tp": 0.0, "k_sl": 0.0, "atr_tf": "probe", "atr": Decimal(1),
            },
            "risk": {
                "equity_eur": Decimal(0), "risk_eur": Decimal(0), "p_edge": 0.0, "scale_p": 0.0, "regime": 0,
                "m_reg": 0.0,
                "ev_bps": 0, "cost_tp_bps": 0, "cost_sl_bps": 0, "p_breakeven": None, "p_be_buffer": 0.0,
                "spread_bps": 0, "min_size_bump": False,
            },
            "created_at": now,
        }
    )  # fmt: skip


def stage_decision_id(stage: str, now: datetime) -> str:
    return f"{TEST_DECISION_PREFIX}:{stage}:{now.astimezone(UTC):%Y%m%dT%H%M%SZ}"


def stage_order_size(quote: Decimal, price: Decimal, meta: ProductMeta) -> Decimal:
    """``ceil_to(quote / price, base_increment)``: the nominal quote rounded UP, never under the minimum."""
    return ceil_to(quote / price, meta.base_increment)


def far_buy_intent(
    stage: str, bid: Decimal, meta: ProductMeta, cfg: ExecProbeConfig, now: datetime, *, tif_s: int
) -> OrderIntent:
    """``place-cancel`` / ``expire``: a GTD BUY of ``test_order_quote`` at ``floor_to(bid * 0.95)``, far from the
    market, so that it should not fill. If it fills it is a normal entry (bracket included)."""
    price = floor_to(bid * Decimal("0.95"), meta.quote_increment)
    size = stage_order_size(cfg.test_order_quote, price, meta)
    return _base_intent(
        stage_decision_id(stage, now), meta.product_id, meta, price, size, tif_s, now, expires_on_exchange=True
    )


def crossing_buy_intent(
    stage: str, ask: Decimal, meta: ProductMeta, cfg: ExecProbeConfig, now: datetime, *, tif_s: int
) -> OrderIntent:
    """``reject``: a post-only BUY at ``ceil_to(ask)`` that would take liquidity, so the exchange must refuse it."""
    price = ceil_to(ask, meta.quote_increment)
    size = stage_order_size(cfg.test_order_quote, price, meta)
    return _base_intent(
        stage_decision_id(stage, now), meta.product_id, meta, price, size, tif_s, now, expires_on_exchange=True
    )
