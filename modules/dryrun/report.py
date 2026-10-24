"""Daily dry-run report and the two exit gates (spec sez. 5.1.7, 5.2, 5.3 punto 13, 6.4.4).

::

    python -m modules.dryrun.report --days 7 [--book champion] [--date YYYY-MM-DD]

Output per UTC day: ``data/reports/dry/YYYY-MM-DD.json`` (the schema of sez. 5.2, plus ``decisions_by_book`` and
``anomalies``) and ``YYYY-MM-DD.md``. The command prints the state of the two gates over the last ``--days`` days.

Inputs (all read only; a missing one makes the numbers that need it ``null``, never zero):

* ``data/decisions/DAY.jsonl``      the ``DecisionRecord`` of the engine (decisions, skip reasons, BUY rate);
* ``closed_trades`` of the dry DB   the ``VirtualTrade`` of sez. 5.2 (``payload`` column with that JSON, or flat);
* ``data/equity/DAY.jsonl``         equity per book (sez. 6.3.3); without it the drawdown comes from the closed trades;
* ``data/reports/ops/DAY.json``     ``OpsDay``: uptime, crashes, violations, kill switch trips, bars, costs.
  NOBODY WRITES THIS FILE YET (it is the contract the app/telemetry has to fill, sez. 6.3): until then uptime, bars
  lag, spread and the operational criteria are ``unmeasured`` and the gate cannot pass;
* ``data/reports/learner/DAY+1.json`` the nightly learner (L0 REST revisions and PSI, L4 costs): the learner of the
  night after the day, so the report of the day is complete after 00:15 UTC of the next one;
* ``--replay-dir/DAY.jsonl``        decisions of a replay of the day, for the decision parity;
* ``data/live_labels/*.jsonl``      resolved labels (calibration ``edge`` and the date of the first label).

Rules taken from the spec and nothing else: no threshold lives here, they come from ``dry_run.gate_ops`` and
``dry_run.gate_econ``. A criterion with no data is ``unmeasured`` (a criterion that was not measured is not passed);
a gate is ``PASS`` only if every criterion passes, ``FAIL`` if one fails after the window is long enough,
``IN_PROGRESS`` before that (``gate_ops.min_days`` days, ``gate_econ.min_trades`` trades) and ``INCOMPLETE`` when the
window is long enough, nothing fails, but something could not be measured.

Manual closes (``exit.reason == 'MANUAL'``, sez. 6.4.4) are counted apart (``manual_exits``): they stay in the net PnL
and in the drawdown, and are excluded from the trade count and from the profit factor of G-DRY-ECON. For the decision
parity the minutes from a manual close to the end of its UTC day are excluded for that (book, product) and counted in
``parity.excluded_manual`` (the command channel does not exist yet: the close is the proxy of the command).
"""

from __future__ import annotations

import argparse
import importlib
import json
import math
import os
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any, Final, Literal, Protocol

import numpy as np
import structlog
from pydantic import BaseModel, ConfigDict

from modules.config import DryRunConfig, GateEconConfig, GateOpsConfig

__all__ = [
    "Criterion",
    "DryReporter",
    "JsonOpsSource",
    "OpsDay",
    "SqliteTradeSource",
    "Trade",
    "decision_parity",
    "evaluate_gate_econ",
    "evaluate_gate_ops",
    "expected_calibration_error",
    "max_drawdown_frac",
    "parse_trade",
    "profit_factor",
]

log = structlog.get_logger(__name__)

SCHEMA: Final = "chronos.report.v1"
MANUAL: Final = "MANUAL"
CHAMPION: Final = "champion"
RANDOM_ENTRY: Final = "random_entry"
DAY: Final = timedelta(days=1)
ECE_BINS: Final = 15
Status = Literal["PASS", "FAIL", "IN_PROGRESS", "INCOMPLETE"]
Verdict = Literal["pass", "fail", "unmeasured"]
ZERO: Final = Decimal(0)


def _day_start(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


def _iso_z(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_t(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _write_json(path: Path, doc: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(_clean(doc), indent=2, sort_keys=True, default=str, allow_nan=False) + "\n", "utf-8")
    os.replace(tmp, path)


def _clean(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_clean(v) for v in obj]
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, np.generic):
        obj = obj.item()
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


def _impact_key(entry: Mapping[str, Any]) -> tuple[float, float, float]:
    """Order of badness: an unmeasured R1 (``share`` None) is worse than any measured one."""
    share = entry.get("share")
    flip = float(entry.get("flip_share") or 0.0)
    return (math.inf if share is None else float(share), float(entry["max_shift"]), flip)


def _impact_entry(entry: Mapping[str, Any], blocks: Sequence[Mapping[str, Any]] | None, limit: float) -> dict[str, Any]:
    """One ``ImpactDay`` of a book as the report carries it. ``share`` and ``flip_share`` are those of the BLOCK of
    R1/R3 (rule v2, sez. 5.1.7) with the same version and day; ``day_share`` is the day's own. ``blocks`` None = a
    learner document written before the blocks existed: a full day IS its own block, a partial day has no measured
    R1 (``share`` None: unmeasured, never judged with the old per-day rule)."""
    from modules.dryrun.revision_impact import STATES_PER_DAY

    share: Any = None
    flip: Any = None
    r3 = entry.get("r3")
    kind = block_states = revisions = max_cluster = None
    if blocks is None:
        if int(entry["states"]) >= STATES_PER_DAY:
            share, flip, kind, block_states = entry["share"], entry.get("flip_share"), "day", entry["states"]
    else:
        same = [x for x in blocks if x.get("version") == entry["version"] and x.get("day") == entry.get("day")]
        blk = same[0] if same else None
        if blk is not None:
            share, flip, r3 = blk["share"], blk.get("flip_share"), blk.get("r3")
            kind, block_states = blk.get("kind"), blk.get("states")
            revisions, max_cluster = blk.get("revisions"), blk.get("max_cluster")
    return {
        "version": entry.get("version"), "states": entry.get("states"), "share": share,
        "max_shift": entry["max_shift"], "flip_share": flip, "r3": r3, "day_share": entry.get("share"),
        "block_states": block_states, "block_kind": kind, "revisions": revisions, "max_cluster": max_cluster,
        "r2_utilization": float(entry["max_shift"]) / limit, "top_shifts": list(entry.get("top_shifts") or []),
        "first_at": entry.get("first_at"), "last_at": entry.get("last_at"),
        "shifted_at": [x["t"] for x in entry.get("shifted_states") or []],
    }  # fmt: skip


def _worst_impact(entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The worst entry of a book and product (a day with two versions: the worst one)."""
    worst: Mapping[str, Any] = entries[0]
    for e in entries[1:]:
        if _impact_key(e) > _impact_key(worst):
            worst = e
    return dict(worst)


def _impact_by_version(books: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, dict[str, Any]]:
    """The worst entry of each model version used by any book on that product and day (R1-R3 belong to the
    version: ``anomalies.revision_by_version``, shadow evidence of the challenger)."""
    worst: dict[str, Mapping[str, Any]] = {}
    for entries in books.values():
        for e in entries:
            cur = worst.get(str(e["version"]))
            if cur is None or _impact_key(e) > _impact_key(cur):
                worst[str(e["version"])] = e
    return {v: dict(e) for v, e in sorted(worst.items())}


def _rolling_diagnostic(
    runs: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[float | None, dict[str, Any]]:
    """``revision_shift_rolling_max`` (no threshold): the window of 1440 consecutive states with the most shifted
    states, per product, over the maximal runs of contiguous days of the window of the gate."""
    from modules.dryrun.revision_impact import STATES_PER_DAY, rolling_max

    best: float | None = None
    detail: dict[str, Any] = {}
    for product, entries in runs.items():
        groups: list[list[Mapping[str, Any]]] = []
        for e in entries:
            if groups and _parse_t(groups[-1][-1]["last_at"]) + timedelta(minutes=1) == _parse_t(e["first_at"]):
                groups[-1].append(e)
            else:
                groups.append([e])
        found = []
        for g in groups:
            spans = [
                (_parse_t(e["first_at"]), _parse_t(e["last_at"]), [_parse_t(t) for t in e["shifted_at"]]) for e in g
            ]
            r = rolling_max(spans, STATES_PER_DAY)
            if r is not None:
                found.append({"start": _iso_z(r["start"]), "end": _iso_z(r["end"]), "shifted": r["shifted"],
                              "share": r["share"], "versions": sorted({str(e["version"]) for e in g})})  # fmt: skip
                best = r["share"] if best is None else max(best, r["share"])
        detail[product] = found
    return best, detail


# ------------------------------------------------------------------------------------------------ trades


@dataclass(frozen=True)
class Trade:
    """A closed trade (``VirtualTrade`` of sez. 5.2)."""

    book: str
    product: str
    exit_reason: str
    exit_at: datetime
    net_quote: Decimal
    fees_quote: Decimal = ZERO
    gross_quote: Decimal | None = None
    entry_at: datetime | None = None
    net_bps: float | None = None
    r_multiple: float | None = None
    held_min: int | None = None
    model_version: str | None = None  # the model version the book ran when the trade closed (ledger ``set_trade_info``)


def parse_trade(raw: Mapping[str, Any]) -> Trade:
    """``raw``: the ``VirtualTrade`` JSON (nested ``entry`` / ``exit`` / ``pnl``) or its flat form
    (``exit_reason``, ``exit_at``, ``net_quote``...). Raises ``ValueError`` / ``KeyError`` on anything else."""
    exit_ = raw.get("exit")
    pnl = raw.get("pnl")
    entry = raw.get("entry")
    if isinstance(exit_, Mapping) and isinstance(pnl, Mapping):
        reason, exit_at, net = exit_["reason"], exit_["at"], pnl["net_quote"]
        fees, gross = pnl.get("fees_quote", "0"), pnl.get("gross_quote")
        entry_at = entry.get("at") if isinstance(entry, Mapping) else None
        bps, r_mult = pnl.get("net_bps"), pnl.get("r_multiple")
    else:
        reason, exit_at, net = raw["exit_reason"], raw["exit_at"], raw["net_quote"]
        fees, gross, entry_at = raw.get("fees_quote", "0"), raw.get("gross_quote"), raw.get("entry_at")
        bps, r_mult = raw.get("net_bps"), raw.get("r_multiple")
    return Trade(
        book=str(raw["book"]),
        product=str(raw.get("product_id", raw.get("product"))),
        exit_reason=str(reason),
        exit_at=_parse_t(str(exit_at)),
        net_quote=Decimal(str(net)),
        fees_quote=Decimal(str(fees)),
        gross_quote=None if gross is None else Decimal(str(gross)),
        entry_at=None if entry_at is None else _parse_t(str(entry_at)),
        net_bps=None if bps is None else float(bps),
        r_multiple=None if r_mult is None else float(r_mult),
        held_min=None if raw.get("held_min") is None else int(raw["held_min"]),
        model_version=None if raw.get("model_version") is None else str(raw["model_version"]),
    )


class TradeSource(Protocol):
    def load(self) -> list[Trade]: ...


class SqliteTradeSource:
    """Table ``closed_trades`` of the dry DB (sez. 6.3 D7), read only. Rows that do not parse are counted and logged,
    not dropped silently."""

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self.bad_rows = 0

    def load(self) -> list[Trade]:
        if not self._path.exists():
            return []
        con = sqlite3.connect(f"file:{self._path.as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute("SELECT * FROM closed_trades").fetchall()
        except sqlite3.Error:
            return []  # the table is not there yet
        finally:
            con.close()
        out: list[Trade] = []
        for row in rows:
            data = dict(row)
            try:
                raw = json.loads(data["payload"]) if data.get("payload") else data
                if "book" not in raw and "book" in data:
                    raw = {**raw, "book": data["book"]}
                if raw.get("model_version") is None and data.get("model_version") is not None:
                    raw = {**raw, "model_version": data["model_version"]}  # the ledger column
                out.append(parse_trade(raw))
            except (ValueError, KeyError, TypeError, ArithmeticError):
                self.bad_rows += 1
                log.warning("closed_trade_unreadable", trade_id=data.get("trade_id"))
        return out


# --------------------------------------------------------------------------------------------- statistics


def profit_factor(net: Iterable[Decimal]) -> float | None:
    """Gross profit over gross loss. ``None``: no trade, or only winners and losers of zero (undefined); ``inf``: wins
    and no loss."""
    values = list(net)
    wins = sum((v for v in values if v > 0), ZERO)
    losses = -sum((v for v in values if v < 0), ZERO)
    if losses > 0:
        return float(wins / losses)
    return math.inf if wins > 0 else None


def max_drawdown_frac(equity: Sequence[float]) -> float:
    """Largest fall from a running peak, as a fraction of that peak (0.0 for an empty or rising curve)."""
    peak, worst = -math.inf, 0.0
    for e in equity:
        peak = max(peak, e)
        if peak > 0:
            worst = max(worst, (peak - e) / peak)
    return worst


def expected_calibration_error(p: np.ndarray, y: np.ndarray, bins: int = ECE_BINS) -> float:
    """ECE with ``bins`` equal-width bins (the definition of ``training.baseline_lgbm.ece``, unweighted)."""
    if p.size == 0:
        return math.nan
    idx = np.minimum((p * bins).astype(int), bins - 1)
    total = 0.0
    for b in range(bins):
        m = idx == b
        if m.any():
            total += float(m.sum()) / p.size * abs(float(p[m].mean()) - float(y[m].mean()))
    return total


def reliability_bins(p: np.ndarray, y: np.ndarray, bins: int = ECE_BINS) -> list[dict[str, float]]:
    idx = np.minimum((p * bins).astype(int), bins - 1)
    return [
        {"p_mean": float(p[idx == b].mean()), "freq": float(y[idx == b].mean()), "n": int((idx == b).sum())}
        for b in range(bins)
        if (idx == b).any()
    ]


def decision_parity(
    live: Mapping[tuple[str, str, str], str],
    replay: Mapping[tuple[str, str, str], str],
    excluded: set[tuple[str, str, str]] | None = None,
) -> dict[str, Any]:
    """Share of minutes where the replay took the same final decision as the live run. Keys are
    ``(book, product, ts_bar_close)``; a minute present on one side only is a mismatch; ``excluded`` minutes (after a
    manual close) are removed from both sides and counted."""
    skip = excluded or set()
    keys = (live.keys() | replay.keys()) - skip
    matched = sum(1 for k in keys if live.get(k) == replay.get(k))
    excluded_n = len((live.keys() | replay.keys()) & skip)
    return {
        "compared": len(keys), "matched": matched, "decision_match": matched / len(keys) if keys else None,
        "excluded_manual": excluded_n,
    }  # fmt: skip


# ------------------------------------------------------------------------------------------------ ops


class _Free(BaseModel):
    model_config = ConfigDict(extra="ignore")


class KillTrip(_Free):
    book: str = "*"
    guard: str
    bug: bool = False  # the writer of the file classifies it: only it knows whether the cause was a defect


class OpsBars(_Free):
    rest: int | None = None
    flat: int | None = None
    flat_unproven: int | None = None
    lag_s_p50: float | None = None
    lag_s_p99: float | None = None
    fetch_failures: int | None = None
    late: int | None = None
    out_of_order: int | None = None
    duplicates: int | None = None


class OpsDay(_Free):
    """``data/reports/ops/DAY.json`` (``chronos.ops.v1``). Everything optional: ``None`` = not measured."""

    uptime: float | None = None
    ws_reconnects: int | None = None
    decider_fallbacks: int | None = None
    dry_run_violations: int | None = None
    crashes: int | None = None
    skipped_late: int | None = None
    kill_switch_trips: list[KillTrip] = []
    bars: dict[str, OpsBars] = {}
    median_spread_bps: dict[str, float] = {}
    maker_fill_rate: float | None = None
    latency_ms_p75: float | None = None
    fee_maker_bps: float | None = None
    fee_taker_bps: float | None = None
    fee_source: str | None = None


class OpsSource(Protocol):
    def day(self, d: date) -> OpsDay | None: ...


class JsonOpsSource:
    def __init__(self, directory: Path) -> None:
        self._dir = Path(directory)

    def day(self, d: date) -> OpsDay | None:
        try:
            return OpsDay.model_validate_json((self._dir / f"{d.isoformat()}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


# ----------------------------------------------------------------------------------------------- gates


@dataclass(frozen=True)
class Criterion:
    name: str
    status: Verdict
    value: Any = None
    threshold: Any = None
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "value": self.value, "threshold": self.threshold,
                "note": self.note}  # fmt: skip


def _crit(name: str, ok: bool | None, value: Any, threshold: Any, note: str | None = None) -> Criterion:
    return Criterion(name, "unmeasured" if ok is None else ("pass" if ok else "fail"), value, threshold, note)


def _gate_status(criteria: Sequence[Criterion], long_enough: bool) -> Status:
    if not long_enough:
        return "IN_PROGRESS"
    if any(c.status == "fail" for c in criteria):
        return "FAIL"
    if any(c.status == "unmeasured" for c in criteria):
        return "INCOMPLETE"
    return "PASS"


def _all_days(days: Sequence[Mapping[str, Any]], getter: Any) -> list[Any] | None:
    """The value of every day, or ``None`` when at least one day lacks it (a hole in the window is unmeasured)."""
    values = [getter(d) for d in days]
    return None if not values or any(v is None for v in values) else values


def evaluate_gate_ops(
    days: Sequence[Mapping[str, Any]], cfg: GateOpsConfig, book: str, *, expected_buy_rate: float | None = None
) -> dict[str, Any]:
    """G-DRY-OPS (sez. 5.1.7) for ``book`` over the daily documents ``days`` (oldest first)."""
    c: list[Criterion] = []
    ops = [d.get("ops") or {} for d in days]
    n = len(days)

    uptime = _all_days(ops, lambda o: o.get("uptime"))
    c.append(_crit("uptime", None if uptime is None else statistics.fmean(uptime) >= cfg.min_uptime,
                   None if uptime is None else statistics.fmean(uptime), cfg.min_uptime))  # fmt: skip
    for key in ("dry_run_violations", "crashes"):
        vals = _all_days(ops, partial(_get, key))
        c.append(_crit(f"zero_{key}", None if vals is None else sum(vals) == 0, None if vals is None else sum(vals), 0))
    trips = _all_days(ops, lambda o: o.get("kill_switch_bug_trips"))
    c.append(_crit("no_kill_switch_for_bug", None if trips is None else sum(trips) == 0,
                   None if trips is None else sum(trips), 0))  # fmt: skip

    matched = [d.get("parity", {}).get("matched") for d in days]
    compared = [d.get("parity", {}).get("compared") for d in days]
    if n and all(v is not None for v in compared) and sum(compared) > 0:
        ratio = sum(matched) / sum(compared)
        c.append(_crit("decision_parity", ratio >= cfg.min_decision_parity, ratio, cfg.min_decision_parity))
    else:
        c.append(_crit("decision_parity", None, None, cfg.min_decision_parity, "no replay compared"))

    psi = _all_days(days, lambda d: (d.get("parity") or {}).get("max_feature_psi"))
    c.append(_crit("feature_psi", None if psi is None else max(psi) < cfg.max_feature_psi,
                   None if psi is None else max(psi), cfg.max_feature_psi))  # fmt: skip

    # "Barre REST" (sez. 5.1.7, since 2.11): R1-R4 on the impact of the revisions; the raw ratio is a diagnostic
    ratios: list[float] = []
    missing = False
    structure: list[int] = []
    impact_missing = completeness_missing = False
    shares: list[float] = []
    shifts: list[float] = []
    flips: list[float] = []
    r3_notes: set[str] = set()
    share_missing = False
    appeared = 0
    runs: dict[str, list[Mapping[str, Any]]] = defaultdict(list)  # per product, by day: first_at / last_at / shifted_at
    revisions_by: dict[str, Any] = {}
    utilization: list[float] = []
    for d in days:
        for pid, b in (d.get("bars") or {}).items():
            r = b.get("rest_revision_ratio")
            ratios += [] if r is None else [float(r)]
            structure += [(b.get("out_of_order") or 0) + (b.get("duplicates") or 0)]
            counts = (b.get("rest_revisions_appeared"), b.get("rest_revisions_disappeared"))
            completeness_missing = completeness_missing or None in counts
            appeared += sum(int(v) for v in counts if v is not None)
            imp = (b.get("revision_impact") or {}).get(book)
            if imp is None:
                impact_missing = True
                continue
            if imp.get("share") is None:
                share_missing = True  # R1 not measured (partial day of an old learner document): R2 still is
            else:
                shares.append(float(imp["share"]))
            shifts.append(float(imp["max_shift"]))
            if imp.get("r3") == "measured" and imp.get("flip_share") is not None:
                flips.append(float(imp["flip_share"]))
            else:
                r3_notes.add("block_unavailable" if imp.get("r3") == "measured" else str(imp.get("r3")))
            if imp.get("first_at") and imp.get("last_at"):
                runs[str(pid)].append(imp)
            if imp.get("revisions") is not None:
                revisions_by[f"{pid} {d.get('date')}"] = imp["revisions"]
            if imp.get("r2_utilization") is not None:
                utilization.append(float(imp["r2_utilization"]))
        missing = missing or not d.get("bars")
    impact_missing = impact_missing or missing or not shifts
    completeness_missing = completeness_missing or missing
    c.append(_crit("bars_in_order_no_duplicates", None if missing or not structure else sum(structure) == 0,
                   sum(structure) if structure else None, 0, "ops.bars.out_of_order / duplicates"))  # fmt: skip
    r1_note = (f"R1: share of shifted states (> {cfg.revision_shift_threshold}) on blocks of 1440 states "
               "(a full UTC day is its own block), worst product and day")  # fmt: skip
    r1_unmeasured = impact_missing or share_missing or not shares
    c.append(_crit("revision_shift_share", None if r1_unmeasured else max(shares) <= cfg.max_revision_shift_share,
                   max(shares) if shares else None, cfg.max_revision_shift_share, r1_note))  # fmt: skip
    c.append(_crit("revision_shift_max", None if impact_missing else max(shifts) <= cfg.max_revision_shift,
                   max(shifts) if shifts else None, cfg.max_revision_shift, "R2: largest shift"))  # fmt: skip
    r3_unmeasured = impact_missing or bool(r3_notes) or not flips
    r3_note = "R3: not measured: " + ", ".join(sorted(r3_notes)) if r3_notes else "R3: flips of actionability"
    c.append(_crit("revision_decision_flips", None if r3_unmeasured else max(flips) <= cfg.max_revision_flip_share,
                   max(flips) if flips else None, cfg.max_revision_flip_share, r3_note))  # fmt: skip
    r4_note = "R4: minutes appeared + disappeared on re-read"
    c.append(_crit("rest_completeness", None if completeness_missing else appeared == 0,
                   None if completeness_missing else appeared, 0, r4_note))  # fmt: skip
    top_ratio = max(ratios) if ratios else None
    diagnostics: list[dict[str, Any]] = [{
        "name": "rest_revision_ratio", "value": top_ratio, "alarm_threshold": cfg.max_rest_revision_ratio,
        "alarm": top_ratio is not None and top_ratio > cfg.max_rest_revision_ratio,
        "note": "diagnostica, non entra nello stato del gate",
    }]  # fmt: skip
    nothing = "diagnostica senza soglia, non entra nello stato del gate"
    rolled, rolled_detail = _rolling_diagnostic(runs)
    diagnostics += [
        {"name": "revision_shift_rolling_max", "value": rolled, "detail": rolled_detail, "note": nothing},
        {"name": "revision_independent_revisions", "value": max(revisions_by.values(), default=None),
         "detail": revisions_by, "note": nothing},
        {"name": "revision_r2_utilization", "value": max(utilization, default=None), "detail": None, "note": nothing},
    ]  # fmt: skip

    total_buys = sum(int(_book_block_of(d, book).get("BUY", 0)) for d in days)
    if expected_buy_rate is None:
        c.append(_crit("buy_frequency", None, None, cfg.buy_rate_tolerance, "no backtest frequency given"))
    else:
        live_rate = total_buys / n if n else 0.0
        ok = (live_rate == 0 and expected_buy_rate == 0) or (
            expected_buy_rate > 0 and abs(live_rate / expected_buy_rate - 1.0) <= cfg.buy_rate_tolerance
        )
        c.append(_crit("buy_frequency", ok, live_rate, {"expected": expected_buy_rate, "tol": cfg.buy_rate_tolerance}))

    fill = [d.get("costs", {}).get("maker_fill_rate") for d in days]
    fills = [f for f in fill if f is not None]
    c.append(_crit("maker_fill_rate", min(fills) >= cfg.min_maker_fill_rate if fills else None,
                   statistics.fmean(fills) if fills else None, cfg.min_maker_fill_rate))  # fmt: skip
    ratios_s: list[float] = []
    for d in days:
        ratios_s += [v for v in (d.get("costs", {}).get("spread_ratio_vs_labels") or {}).values() if v is not None]
    c.append(_crit("spread_vs_labels", max(ratios_s) <= cfg.max_spread_ratio_vs_labels if ratios_s else None,
                   max(ratios_s) if ratios_s else None, cfg.max_spread_ratio_vs_labels))  # fmt: skip

    ff = sum(int(_book_block_of(d, book).get("skip_reasons", {}).get("fee_floor", 0)) for d in days)
    if total_buys == 0:
        c.append(_crit("fee_floor_share", True, None, cfg.max_fee_floor_skip_ratio, "no BUY decision"))
    else:
        c.append(_crit("fee_floor_share", ff / total_buys < cfg.max_fee_floor_skip_ratio, ff / total_buys,
                       cfg.max_fee_floor_skip_ratio))  # fmt: skip
    return {
        "book": book, "days": n, "min_days": cfg.min_days, "status": _gate_status(c, n >= cfg.min_days),
        "criteria": [x.to_dict() for x in c], "diagnostics": diagnostics,
    }  # fmt: skip


def evaluate_gate_econ(
    trades: Sequence[Trade], cfg: GateEconConfig, *, book: str, starting_quote: Decimal, random_pnl: Decimal | None,
    equity: Sequence[float] | None, ece: float | None,
) -> dict[str, Any]:  # fmt: skip
    """G-DRY-ECON (sez. 5.1.7) on ``book``. ``MANUAL`` closes stay in the PnL and the drawdown and leave the trade count
    and the profit factor (sez. 6.4.4)."""
    mine = [t for t in trades if t.book == book]
    counted = [t for t in mine if t.exit_reason != MANUAL]
    pnl = sum((t.net_quote for t in mine), ZERO)
    pf = profit_factor(t.net_quote for t in counted)
    curve = list(equity) if equity else _curve_from_trades(mine, starting_quote)
    dd = max_drawdown_frac(curve)
    c = [
        _crit("closed_trades", len(counted) >= cfg.min_trades, len(counted), cfg.min_trades,
              f"{len(mine) - len(counted)} manual exits not counted"),
        _crit("net_pnl", pnl > 0, pnl, 0),
        _crit("profit_factor", None if pf is None else pf >= cfg.min_profit_factor, pf, cfg.min_profit_factor),
        _crit("beats_random_entry", None if random_pnl is None else pnl > random_pnl, pnl, random_pnl),
        _crit("max_drawdown", dd <= cfg.max_drawdown_frac, dd, cfg.max_drawdown_frac,
              "equity" if equity else "closed trades"),
        _crit("edge_ece", None if ece is None else ece <= cfg.max_ece_edge, ece, cfg.max_ece_edge),
    ]  # fmt: skip
    return {
        "book": book, "trades": len(counted), "manual_exits": len(mine) - len(counted), "net_pnl_quote": pnl,
        "status": _gate_status(c, len(counted) >= cfg.min_trades), "criteria": [x.to_dict() for x in c],
    }  # fmt: skip


def _curve_from_trades(trades: Sequence[Trade], starting_quote: Decimal) -> list[float]:
    equity = starting_quote
    out = [float(equity)]
    for t in sorted(trades, key=lambda x: x.exit_at):
        equity += t.net_quote
        out.append(float(equity))
    return out


# ------------------------------------------------------------------------------------------ rollback advice
# Spec 5.1.7 (since 2.11): a RECOMMENDATION in the daily report, never an action. The rollback itself is
# ``python -m modules.dryrun.registry rollback champion --reason "..."`` (a human, locally, effective at the next
# reload). Nothing here writes the registry or the command inbox: the registry is only read.

LOSS_GUARDS: Final = ("daily_loss", "loss_streak")  # kill switch guards that trip on losses (not on defects)
ROLLBACK_COMMAND: Final = 'python -m modules.dryrun.registry rollback champion --reason "<why>"'
Z_ROLLBACK: Final = -1.96  # E5's threshold: post-promotion mean significantly below the OOS mean


@dataclass(frozen=True)
class ChampionInfo:
    """The champion version as the registry holds it (``loadable`` False: the alias or the files are damaged)."""

    version: str | None
    since: datetime | None
    path: Path | None
    loadable: bool = True
    error: str | None = None


class ChampionSource(Protocol):
    def current(self) -> ChampionInfo | None: ...


class RegistryChampionSource:
    """Read only view of ``models/registry``: ``state`` and ``get`` never write."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    def current(self) -> ChampionInfo | None:
        from modules.dryrun.registry import FilesystemModelRegistry, RegistryError, UnknownAlias

        registry = FilesystemModelRegistry(self._root)
        try:
            state = registry.state("champion")
        except UnknownAlias:
            return None
        except RegistryError as exc:
            return ChampionInfo(None, None, None, False, f"{type(exc).__name__}: {exc}")
        since = _parse_t(state.updated_at)
        try:
            return ChampionInfo(state.version, since, registry.get("champion").path)
        except RegistryError as exc:
            return ChampionInfo(state.version, since, None, False, f"{type(exc).__name__}: {exc}")


def oos_reference(path: Path | None) -> tuple[float, float, float] | None:
    """``(mean net_ret_bps, standard error, rho)`` of the OOS trades a version was evaluated on
    (``oos_trades.jsonl``, with the E1 effective count as ``n``). None: the version has no OOS artifacts."""
    if path is None:
        return None
    try:
        rows = [
            json.loads(line) for line in (path / "oos_trades.jsonl").read_text(encoding="utf-8").splitlines() if line
        ]
        rho = float(json.loads((path / "oos_spans.json").read_text(encoding="utf-8"))["rho"])
        bps = np.array([float(r["net_ret_bps"]) for r in rows])
        n_eff = sum(float(r["neff_weight"]) for r in rows)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if len(rows) < 2 or n_eff <= 0:
        return None
    return float(bps.mean()), float(bps.std(ddof=1) / math.sqrt(n_eff)), rho


def live_consistency(trades: Sequence[Trade], oos: tuple[float, float, float]) -> float | None:
    """``z = (m_post - m_oos) / sqrt(se_post^2 + se_oos^2)`` (E5's own function, ``holdout_consistency``): ``m`` is
    the mean ``net_bps`` per trade, ``se`` comes from the effective number of trades (E1 weights, ``rho`` of the
    version). None: a trade without ``net_bps``, or fewer than two trades."""
    bps = [t.net_bps for t in trades]
    if len(bps) < 2 or any(b is None for b in bps):
        return None
    import pandas as pd

    from training.evaluate import Boot, effective_trades, holdout_consistency

    values = np.array([b for b in bps if b is not None])
    n_eff = float(len(trades))
    if all(t.entry_at is not None for t in trades):
        frame = pd.DataFrame({
            "product": [t.product for t in trades], "fill_ts": [t.entry_at for t in trades],
            "exit_ts": [t.exit_at for t in trades],
        })  # fmt: skip
        n_eff = effective_trades(frame, oos[2])
    se = float(values.std(ddof=1) / math.sqrt(n_eff)) if n_eff > 0 else math.inf
    post = Boot(float(values.mean()), se, math.nan, math.nan, math.nan, len(values))
    ref = Boot(oos[0], oos[1], math.nan, math.nan, math.nan, 0)
    return holdout_consistency(post, ref)


def rollback_advice(
    info: ChampionInfo | None, *, trades: Sequence[Trade], oos: tuple[float, float, float] | None,
    parity: float | None, min_parity: float, revision_failures: Sequence[str], fallback_ratio: float | None,
    max_fallback_ratio: float, kill_switch_bug: int | None, loss_trips: int, min_trades: int,
) -> dict[str, Any]:  # fmt: skip
    """The daily ``rollback_advice`` block for the CURRENT champion version.

    Defects (advised at once, no minimum of trades): decision parity under ``min_parity``, R1-R4 failed with the
    version, decider fallbacks over ``max_fallback_ratio``, a version that cannot be loaded, a kill switch trip for a
    bug. Economics (only with at least ``min_trades`` closed trades OF THE VERSION, ``MANUAL`` closes excluded): profit
    factor below 1.0, post-promotion mean inconsistent with the OOS (``z < -1.96``), two or more kill switch trips
    for losses since the version took over. Trades are attributed to the version through the version the ledger
    recorded; trades without one make the economics ``unmeasured`` (never zero)."""
    if info is None or (info.version is None and info.loadable):
        return {"version": None, "advice": "none", "triggers": [], "note": "no champion alias"}
    defects: list[str] = []
    if not info.loadable:
        defects.append("version_not_loadable")
    if parity is not None and parity < min_parity:
        defects.append(f"decision_parity:{parity:.4f}")
    defects += [f"revision:{f}" for f in revision_failures]
    if fallback_ratio is not None and fallback_ratio > max_fallback_ratio:
        defects.append(f"decider_fallback_ratio:{fallback_ratio:.3f}")
    if kill_switch_bug:
        defects.append("kill_switch_bug")
    mine = [t for t in trades if t.exit_reason != MANUAL and t.model_version == info.version]
    economics: dict[str, Any] = {"closed_trades": len(mine), "min_trades": min_trades}
    triggers_economic: list[str] = []
    if not any(t.model_version is not None for t in trades):
        economics |= {"status": "unmeasured", "reason": "trades_without_model_version", "closed_trades": None}
    elif len(mine) < min_trades:
        economics["status"] = "insufficient_trades"  # below the minimum: no economic recommendation at all
    else:
        pf = profit_factor(t.net_quote for t in mine)
        z = None if oos is None else live_consistency(mine, oos)
        economics |= {"status": "measured", "profit_factor": pf, "z_vs_oos": z, "loss_trips": loss_trips}
        if pf is not None and pf < 1.0:
            triggers_economic.append(f"profit_factor:{pf:.3f}")
        if z is not None and z < Z_ROLLBACK:
            triggers_economic.append(f"oos_inconsistency_z:{z:.2f}")
        if loss_trips >= 2:
            triggers_economic.append(f"kill_switch_loss_trips:{loss_trips}")
    triggers = [*defects, *triggers_economic]
    return {
        "version": info.version, "since": None if info.since is None else _iso_z(info.since),
        "closed_trades": len(mine), "defect_triggers": defects, "economic_triggers": triggers_economic,
        "triggers": triggers, "economics": economics, "error": info.error,
        "advice": "consider_rollback" if triggers else "none", "command": ROLLBACK_COMMAND,
    }  # fmt: skip


# ------------------------------------------------------------------------------------------- the reporter


@dataclass
class ReportPaths:
    decisions_dir: Path
    equity_dir: Path
    learner_dir: Path
    labels_dir: Path
    out_dir: Path
    replay_dir: Path | None = None
    label_config: Path | None = None


@dataclass
class DryReporter:
    cfg: DryRunConfig
    paths: ReportPaths
    trades: TradeSource
    ops: OpsSource
    mode: str = "dry"
    expected_buy_rate: float | None = None
    champion: ChampionSource | None = None  # where the rollback advice reads the champion version (read only)
    max_fallback_ratio: float = 0.20  # risk.kill_switch.max_fallback_ratio_1h
    _trades: list[Trade] = field(default_factory=list, init=False)

    # ------------------------------------------------------------------------------------ readers

    def _jsonl(self, path: Path) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if not path.exists():
            return out
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue  # a torn last line
            if isinstance(rec, dict):
                out.append(rec)
        return out

    def _decisions(self, d: date) -> list[dict[str, Any]]:
        return self._jsonl(self.paths.decisions_dir / f"{d.isoformat()}.jsonl")

    def _equity(self, d: date) -> dict[str, list[dict[str, Any]]]:
        by_book: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for rec in self._jsonl(self.paths.equity_dir / f"{d.isoformat()}.jsonl"):
            if "book" in rec and "equity" in rec:
                by_book[str(rec["book"])].append(rec)
        return by_book

    def _learner(self, d: date) -> dict[str, Any] | None:
        path = self.paths.learner_dir / f"{(d + DAY).isoformat()}.json"
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return doc if isinstance(doc, dict) else None

    def _resolved_labels(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for path in sorted(self.paths.labels_dir.glob("*.jsonl")) if self.paths.labels_dir.is_dir() else []:
            out += [r for r in self._jsonl(path) if r.get("question_key") == "edge" and r.get("split") == "live"]
        return out

    # ----------------------------------------------------------------------------- one day

    def build_day(self, d: date, *, all_trades: Sequence[Trade] | None = None) -> dict[str, Any]:
        trades = list(self._trades if all_trades is None else all_trades)
        end = _day_start(d) + DAY
        upto = [t for t in trades if t.exit_at < end]
        today = [t for t in upto if t.exit_at >= _day_start(d)]
        records = self._decisions(d)
        ops = self.ops.day(d)
        learner = self._learner(d)
        equity = self._equity(d)

        by_book_dec: dict[str, dict[str, Any]] = {}
        for rec in records:
            b = by_book_dec.setdefault(
                str(rec.get("book")), {"total": 0, "BUY": 0, "SELL": 0, "HOLD": 0, "skip_reasons": Counter()}
            )
            final = (rec.get("policy") or {}).get("final", "HOLD")
            b["total"] += 1
            b[final if final in ("BUY", "SELL", "HOLD") else "HOLD"] += 1
            risk = rec.get("risk") or {}
            if risk.get("outcome") == "skip" and risk.get("reason"):
                b["skip_reasons"][risk["reason"]] += 1
        for b in by_book_dec.values():
            b["skip_reasons"] = dict(sorted(b["skip_reasons"].items()))
        main_block = by_book_dec.get(CHAMPION) or _sum_blocks(by_book_dec.values())
        decisions = {**main_block, "skipped_late": None if ops is None else ops.skipped_late}

        books: dict[str, Any] = {}
        for bc in self.cfg.books:
            books[bc.name] = self._book_block(bc.name, bc.starting_quote, upto, today, equity.get(bc.name, []))

        parity = self._parity(d, records, upto, learner)
        bars = self._bars(ops, learner)
        costs = self._costs(ops, learner)
        labels = self._labels_block(records, d)
        ops_block = self._ops_block(ops, records)
        doc: dict[str, Any] = {
            "v": SCHEMA, "date": d.isoformat(), "mode": self.mode, "books": books, "decisions": decisions,
            "decisions_by_book": by_book_dec, "labels": labels, "parity": parity, "bars": bars, "costs": costs,
            "ops": ops_block, "manual_exits": {b: sum(1 for t in today if t.book == b and t.exit_reason == MANUAL)
                                               for b in books},
        }  # fmt: skip
        doc["anomalies"] = self._anomalies(doc)
        doc["alarms"] = self._alarms(doc)
        doc["rollback_advice"] = self._rollback_advice(d, records, upto, doc)
        return doc

    def _rollback_advice(
        self, d: date, records: Sequence[Mapping[str, Any]], upto: Sequence[Trade], doc: Mapping[str, Any]
    ) -> dict[str, Any]:  # fmt: skip
        """B.7: a recommendation for the current champion version. Reads the registry and the files the report already
        reads; writes nothing and sends nothing."""
        info = self.champion.current() if self.champion is not None else None
        mine = [r for r in records if r.get("book") == CHAMPION]
        fallback = sum(1 for r in mine if r.get("fallback")) / len(mine) if mine else None
        version = None if info is None else info.version
        failed = ((doc.get("anomalies") or {}).get("revision_by_version") or {}).get(version, [])
        loss_trips = 0
        if info is not None and info.since is not None:
            for k in range((d - info.since.date()).days + 1):
                day_ops = self.ops.day(info.since.date() + timedelta(days=k))
                loss_trips += 0 if day_ops is None else sum(
                    1 for t in day_ops.kill_switch_trips
                    if t.guard in LOSS_GUARDS and not t.bug and t.book in (CHAMPION, "*")
                )  # fmt: skip
        return rollback_advice(
            info, trades=[t for t in upto if t.book == CHAMPION],
            oos=oos_reference(None if info is None else info.path),
            parity=(doc.get("parity") or {}).get("decision_match"),
            min_parity=self.cfg.gate_ops.min_decision_parity, revision_failures=failed, fallback_ratio=fallback,
            max_fallback_ratio=self.max_fallback_ratio,
            kill_switch_bug=(doc.get("ops") or {}).get("kill_switch_bug_trips"),
            loss_trips=loss_trips, min_trades=self.cfg.gate_econ.min_trades,
        )  # fmt: skip

    def _book_block(
        self, name: str, starting: Decimal, upto: Sequence[Trade], today: Sequence[Trade],
        equity: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:  # fmt: skip
        mine = [t for t in upto if t.book == name]
        mine_today = [t for t in today if t.book == name]
        counted = [t for t in mine if t.exit_reason != MANUAL]
        eq = [float(e["equity"]) for e in equity]
        curve = eq or _curve_from_trades(mine, starting)
        pf = profit_factor(t.net_quote for t in counted)
        wins = [t for t in counted if t.net_quote > 0]
        rs = [t.r_multiple for t in counted if t.r_multiple is not None]
        eq_start = (
            Decimal(str(equity[0]["equity"]))
            if equity
            else starting + sum((t.net_quote for t in mine if t not in mine_today), ZERO)
        )
        day_net = sum((t.net_quote for t in mine_today), ZERO)
        eq_end = Decimal(str(equity[-1]["equity"])) if equity else eq_start + day_net
        daily = _daily_returns(mine, starting)
        sharpe = None
        if len(daily) >= 2 and statistics.stdev(daily) > 0:
            sharpe = statistics.fmean(daily) / statistics.stdev(daily)
        return {
            "equity_start": eq_start, "equity_end": eq_end,
            "net_pnl_quote": sum((t.net_quote for t in mine), ZERO),
            "fees_quote": sum((t.fees_quote for t in mine), ZERO),
            "open_positions": equity[-1].get("open_positions") if equity else None,
            "trades_closed_today": len(mine_today), "trades_closed_total": len(mine),
            "manual_exits_total": len(mine) - len(counted),
            "profit_factor_total": None if pf is None or math.isinf(pf) else pf,
            "profit_factor_unbounded": pf is not None and math.isinf(pf),
            "win_rate": len(wins) / len(counted) if counted else None,
            "r_mean": statistics.fmean(rs) if rs else None,
            "max_dd_frac": max_drawdown_frac(curve), "max_dd_source": "equity" if eq else "closed_trades",
            "sharpe_daily": sharpe, "sharpe_note": "daily, not annualised; indicative only (small sample)",
        }  # fmt: skip

    def _parity(
        self, d: date, records: Sequence[Mapping[str, Any]], upto: Sequence[Trade], learner: Mapping[str, Any] | None
    ) -> dict[str, Any]:  # fmt: skip
        out: dict[str, Any] = {
            "decision_match": None, "compared": None, "matched": None, "excluded_manual": 0,
            "max_feature_psi": None, "worst_feature": None, "state_match": None,
        }  # fmt: skip
        l0 = ((learner or {}).get("steps") or {}).get("L0", {}).get("details", {}).get("state_parity") or {}
        if l0:
            out["max_feature_psi"], out["worst_feature"] = l0.get("max_feature_psi"), l0.get("worst_feature")
            out["state_match"] = (l0.get("state_parity") or {}).get("match")
            out["drift_alerts"] = l0.get("drift_alerts", [])
        start, end = _day_start(d), _day_start(d) + DAY
        manual = {(t.book, t.product): t.exit_at for t in upto if t.exit_reason == MANUAL and start <= t.exit_at < end}
        if self.paths.replay_dir is not None:
            replay = self._jsonl(self.paths.replay_dir / f"{d.isoformat()}.jsonl")
            if replay:
                live_map = _final_map(records)
                replay_map = _final_map(replay)
                excluded = {
                    k
                    for k in live_map.keys() | replay_map.keys()
                    if (k[0], k[1]) in manual and _parse_t(k[2]) >= _minute(manual[(k[0], k[1])])
                }
                out.update(decision_parity(live_map, replay_map, excluded))
        return out

    def _bars(self, ops: OpsDay | None, learner: Mapping[str, Any] | None) -> dict[str, Any]:
        l0 = ((learner or {}).get("steps") or {}).get("L0", {}).get("details", {})
        audit = (l0.get("rest_revisions") or {}).get("products", {})
        impact = (l0.get("revision_impact") or {}).get("products", {})
        out: dict[str, Any] = {}
        for p in sorted(set(audit) | set(impact) | set(ops.bars if ops else {})):
            b = ops.bars.get(p) if ops else None
            a = audit.get(p) or {}
            books = (impact.get(p) or {}).get("books") or {}
            blocks = (impact.get(p) or {}).get("blocks")  # None: a document written before the blocks existed
            limit = self.cfg.gate_ops.max_revision_shift
            entries = {
                k: [_impact_entry(e, None if blocks is None else blocks.get(k, []), limit) for e in v]
                for k, v in books.items()
                if v
            }
            out[p] = {
                "rest": None if b is None else b.rest, "flat": None if b is None else b.flat,
                "flat_unproven": None if b is None else b.flat_unproven,
                "lag_s_p50": None if b is None else b.lag_s_p50, "lag_s_p99": None if b is None else b.lag_s_p99,
                "fetch_failures": None if b is None else b.fetch_failures, "late": None if b is None else b.late,
                "out_of_order": None if b is None else b.out_of_order,
                "duplicates": None if b is None else b.duplicates,
                "rest_revisions": a.get("revised"), "rest_revision_ratio": a.get("ratio"),
                "rest_revision_max_close_bps": a.get("max_close_diff_bps"),
                "rest_revisions_value": a.get("value_revised"), "rest_revisions_appeared": a.get("appeared"),
                "rest_revisions_disappeared": a.get("disappeared"),
                "revision_impact": {k: _worst_impact(v) for k, v in entries.items()},
                "revision_impact_by_version": _impact_by_version(entries),
            }  # fmt: skip
        return out

    def _costs(self, ops: OpsDay | None, learner: Mapping[str, Any] | None) -> dict[str, Any]:
        l4 = ((learner or {}).get("steps") or {}).get("L4", {}).get("details", {})
        fill = (l4.get("maker_fill_rate") or {}).get("rate")
        spread = dict(ops.median_spread_bps) if ops else {}
        assumed = self._label_spreads()
        return {
            "median_spread_bps": spread,
            "spread_ratio_vs_labels": {p: (spread[p] / assumed[p] if assumed.get(p) else None) for p in spread},
            "fee_maker_bps": None if ops is None else ops.fee_maker_bps,
            "fee_taker_bps": None if ops is None else ops.fee_taker_bps,
            "fee_source": None if ops is None else ops.fee_source,
            "maker_fill_rate": fill if fill is not None else (None if ops is None else ops.maker_fill_rate),
            "latency_ms_p75": l4.get("latency_ms_p75", None if ops is None else ops.latency_ms_p75),
        }  # fmt: skip

    def _label_spreads(self) -> dict[str, float]:
        if self.paths.label_config is None:
            return {}
        try:
            raw = json.loads(self.paths.label_config.read_text(encoding="utf-8"))
            return {str(k): float(v) for k, v in raw.get("spread_bps", {}).items()}
        except (OSError, ValueError, AttributeError):
            return {}

    def _labels_block(self, records: Sequence[Mapping[str, Any]], d: date) -> dict[str, Any]:
        labels = self._resolved_labels()
        first = None
        if self.paths.label_config is not None and self.paths.label_config.exists():
            try:
                import pandas as pd

                from modules.risk.label_config import load_label_config
                from training.labeling import label_window_end

                times = sorted(_parse_t(str(r["ts_bar_close"])) for r in records if "ts_bar_close" in r)
                if times:
                    idx = pd.DatetimeIndex([times[0].replace(tzinfo=None)])
                    first = label_window_end(idx, load_label_config(self.paths.label_config).cfg)[0].date().isoformat()
            except (ValueError, OSError, KeyError):
                first = None
        return {"resolved_total": len(labels), "first_resolution_expected": first}

    def _ops_block(self, ops: OpsDay | None, records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        minutes = {str(r.get("ts_bar_close")) for r in records if "ts_bar_close" in r}
        fallbacks = sum(1 for r in records if r.get("fallback"))
        errors = sum(1 for r in records if "error" in r)
        trips = [] if ops is None else [t for t in ops.kill_switch_trips if t.guard != "manual_pause"]
        return {
            "uptime": None if ops is None else ops.uptime,
            "ws_reconnects": None if ops is None else ops.ws_reconnects,
            "decider_fallbacks": fallbacks, "engine_errors": errors,
            "dry_run_violations": None if ops is None else ops.dry_run_violations,
            "crashes": None if ops is None else ops.crashes,
            "kill_switch_bug_trips": None if ops is None else sum(1 for t in trips if t.bug),
            "decision_minutes": len(minutes), "measured": ops is not None,
        }  # fmt: skip

    def _anomalies(self, doc: Mapping[str, Any]) -> dict[str, Any]:
        """What the learner counts for rule L5 (b) ("no operational and no parity anomaly"): the day's breaches of the
        G-DRY-OPS thresholds, nothing else. A measure that is missing is not an anomaly."""
        g, ops, par = self.cfg.gate_ops, doc["ops"], doc["parity"]
        operational, parity, notes = 0, 0, []
        checks: list[tuple[Any, Callable[[Any], bool], str]] = [
            (ops.get("dry_run_violations"), lambda v: v > 0, "dry_run_violation"),
            (ops.get("crashes"), lambda v: v > 0, "crash"),
            (ops.get("kill_switch_bug_trips"), lambda v: v > 0, "kill_switch_bug"),
            (ops.get("uptime"), lambda v: v < g.min_uptime, "uptime"),
            (ops.get("engine_errors"), lambda v: v > 0, "engine_error"),
        ]
        for value, bad, note in checks:
            if value is not None and bad(value):
                operational += 1
                notes.append(note)
        by_version: dict[str, list[str]] = {}
        for p, b in doc["bars"].items():
            # the raw ratio is an alarm (``_alarms``), no longer an anomaly; R4 is operational (all models alike)
            gone = (b.get("rest_revisions_appeared") or 0) + (b.get("rest_revisions_disappeared") or 0)
            if gone > 0:
                operational += 1
                notes.append(f"rest_completeness:{p}")
            for version, s in (b.get("revision_impact_by_version") or {}).items():  # R1-R3 belong to the version
                failed = [
                    name
                    for name, bad in (
                        ("R1", s.get("share") is not None and s["share"] > g.max_revision_shift_share),
                        ("R2", s["max_shift"] > g.max_revision_shift),
                        ("R3", s.get("r3") == "measured" and (s.get("flip_share") or 0.0) > g.max_revision_flip_share),
                    )
                    if bad
                ]
                by_version.setdefault(version, []).extend(f"{name}:{p}" for name in failed)
        parity_checks: list[tuple[Any, Callable[[Any], bool], str]] = [
            (par.get("decision_match"), lambda v: v < g.min_decision_parity, "decision_parity"),
            (par.get("max_feature_psi"), lambda v: v >= g.max_feature_psi, "feature_psi"),
        ]
        for value, bad, note in parity_checks:
            if value is not None and bad(value):
                parity += 1
                notes.append(note)
        by_version = {v: sorted(f) for v, f in sorted(by_version.items()) if f}
        notes += [f"revision_{name}" for fails in by_version.values() for name in fails]
        return {"operational": operational, "parity": parity, "notes": notes, "revision_by_version": by_version}

    def _alarms(self, doc: Mapping[str, Any]) -> list[str]:
        """Diagnostics that ring without entering a gate or an anomaly: the raw ratio of revised minutes."""
        limit = self.cfg.gate_ops.max_rest_revision_ratio
        return [
            f"rest_revision_ratio:{p}"
            for p, b in doc["bars"].items()
            if b.get("rest_revision_ratio") is not None and b["rest_revision_ratio"] > limit
        ]

    # ------------------------------------------------------------------------------ the window

    def generate(self, end: date, days: int, *, book: str | None = None, write: bool = True) -> dict[str, Any]:
        """Daily documents for the ``days`` UTC days ending at ``end`` (included), the two gates, and the files."""
        self._trades = self.trades.load()
        window = [end - timedelta(days=i) for i in range(days - 1, -1, -1)]
        docs = [self.build_day(d) for d in window]
        champion = book or CHAMPION
        starting = {bc.name: bc.starting_quote for bc in self.cfg.books}
        ops_gate = evaluate_gate_ops(docs, self.cfg.gate_ops, champion, expected_buy_rate=self.expected_buy_rate)
        labels = self._champion_labels(window)
        ece = expected_calibration_error(*labels) if labels[0].size else None
        eq_all = [float(e["equity"]) for d in window for e in self._equity(d).get(champion, [])]
        until = _day_start(end) + DAY
        random_pnl = (
            sum((t.net_quote for t in self._trades if t.book == RANDOM_ENTRY and t.exit_at < until), ZERO)
            if any(bc.name == RANDOM_ENTRY for bc in self.cfg.books) else None
        )  # fmt: skip
        econ = evaluate_gate_econ(
            [t for t in self._trades if t.exit_at < _day_start(end) + DAY], self.cfg.gate_econ, book=champion,
            starting_quote=starting.get(champion, Decimal(100)), random_pnl=random_pnl,
            equity=eq_all or None, ece=None if ece is None or math.isnan(ece) else ece,
        )  # fmt: skip
        for doc in docs:
            doc["gate_ops"] = {"days": ops_gate["days"], "status": ops_gate["status"]}
            doc["gate_econ"] = {"trades": econ["trades"], "status": econ["status"]}
        result = {
            "end": end.isoformat(), "days": days, "book": champion, "gate_ops": ops_gate, "gate_econ": econ,
            "reliability": reliability_bins(*labels) if labels[0].size else [], "daily": docs,
        }  # fmt: skip
        if write:
            for doc in docs:
                self._write_day(doc, ops_gate if doc is docs[-1] else None, econ if doc is docs[-1] else None)
            if labels[0].size:
                self._reliability_png(labels, end)
        cleaned: dict[str, Any] = _clean(result)  # what the files hold: strings for money, null for NaN
        return cleaned

    def _champion_labels(self, window: Sequence[date]) -> tuple[np.ndarray, np.ndarray]:
        """``predicted.edge`` and the outcome of EVERY resolved live label of the champion book (not only the window:
        sez. 5.1.7 says "tutte le label live risolte")."""
        ids: set[str] = set()
        for path in sorted(self.paths.decisions_dir.glob("*.jsonl")) if self.paths.decisions_dir.is_dir() else []:
            recs = self._jsonl(path)
            ids |= {str(r["decision_id"]) for r in recs if r.get("book") == CHAMPION and "decision_id" in r}
        p, y = [], []
        for r in self._resolved_labels():
            edge = (r.get("predicted") or {}).get("edge")
            if edge is not None and r.get("decision_id") in ids:
                p.append(float(edge))
                y.append(1.0 if r.get("target") == "true" else 0.0)
        return np.asarray(p), np.asarray(y)

    def _write_day(
        self, doc: dict[str, Any], ops_gate: Mapping[str, Any] | None, econ: Mapping[str, Any] | None
    ) -> None:
        out = self.paths.out_dir
        _write_json(out / f"{doc['date']}.json", doc)
        (out / f"{doc['date']}.md").write_text(render_markdown(doc, ops_gate, econ), encoding="utf-8", newline="\n")

    def _reliability_png(self, labels: tuple[np.ndarray, np.ndarray], end: date) -> None:
        try:
            plt = importlib.import_module("matplotlib.pyplot")
        except ImportError:
            return  # the bins are in the Markdown anyway
        bins = reliability_bins(*labels)
        fig, ax = plt.subplots(figsize=(4, 4))
        ax.plot([0, 1], [0, 1], linestyle="--")
        ax.plot([b["p_mean"] for b in bins], [b["freq"] for b in bins], marker="o")
        ax.set_xlabel("predicted edge")
        ax.set_ylabel("observed frequency")
        fig.savefig(self.paths.out_dir / f"{end.isoformat()}-reliability.png", dpi=100)
        plt.close(fig)


def _sum_blocks(blocks: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"total": 0, "BUY": 0, "SELL": 0, "HOLD": 0, "skip_reasons": {}}
    skips: Counter[str] = Counter()
    for b in blocks:
        for k in ("total", "BUY", "SELL", "HOLD"):
            out[k] += b[k]
        skips.update(b["skip_reasons"])
    out["skip_reasons"] = dict(sorted(skips.items()))
    return out


def _final_map(records: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str, str], str]:
    return {
        (str(r.get("book")), str(r.get("product")), str(r.get("ts_bar_close"))): _final_of(r)
        for r in records
        if "ts_bar_close" in r
    }


def _final_of(rec: Mapping[str, Any]) -> str:
    return str((rec.get("policy") or {}).get("final"))


def _minute(t: datetime) -> datetime:
    return t.replace(second=0, microsecond=0)


def _get(key: str, o: Mapping[str, Any]) -> Any:
    return o.get(key)


def _book_block_of(day: Mapping[str, Any], book: str) -> Mapping[str, Any]:
    block: Mapping[str, Any] = (day.get("decisions_by_book") or {}).get(book, {})
    return block


def _daily_returns(trades: Sequence[Trade], starting: Decimal) -> list[float]:
    """Return per UTC day (days with a close) over the equity at the start of that day, from the closed trades."""
    by_day: dict[date, Decimal] = defaultdict(lambda: ZERO)
    for t in trades:
        by_day[t.exit_at.date()] += t.net_quote
    equity, out = starting, []
    for day in sorted(by_day):
        out.append(float(by_day[day] / equity))
        equity += by_day[day]
    return out


# --------------------------------------------------------------------------------------------- markdown


def _fmt(v: Any) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.4g}"
    return str(v)


def render_markdown(doc: Mapping[str, Any], ops_gate: Mapping[str, Any] | None, econ: Mapping[str, Any] | None) -> str:
    lines = [f"# Dry run report {doc['date']} ({doc['mode']})", ""]
    lines += ["## Books", "", "| book | equity end | net PnL | fees | trades today | trades total | PF | win | DD |"]
    lines += ["|---|---|---|---|---|---|---|---|---|"]
    for name, b in doc["books"].items():
        lines.append(
            f"| {name} | {_fmt(b['equity_end'])} | {_fmt(b['net_pnl_quote'])} | {_fmt(b['fees_quote'])} | "
            f"{b['trades_closed_today']} | {b['trades_closed_total']} | {_fmt(b['profit_factor_total'])} | "
            f"{_fmt(b['win_rate'])} | {_fmt(b['max_dd_frac'])} |"
        )
    d = doc["decisions"]
    lines += ["", "## Decisions", "", f"total {d['total']}, BUY {d['BUY']}, SELL {d['SELL']}, HOLD {d['HOLD']}, "
              f"skipped_late {_fmt(d['skipped_late'])}", "", "Skip reasons:", ""]  # fmt: skip
    top = max(d["skip_reasons"].values(), default=1)
    lines += [f"- {k}: {v} {'#' * max(1, round(20 * v / top))}" for k, v in d["skip_reasons"].items()] or ["- none"]
    p = doc["parity"]
    lines += ["", "## Parity (L0)", ""]
    lines.append(f"decision_match {_fmt(p['decision_match'])} (excluded_manual {p['excluded_manual']})")
    lines.append(f"max_feature_psi {_fmt(p['max_feature_psi'])} ({_fmt(p['worst_feature'])})")
    lines.append(f"state_match {_fmt(p['state_match'])}")
    lines += ["", "## Bars", "", "| product | rest | flat | flat_unproven | lag_s_p99 | rest_revision_ratio | R1 share "
              "| R2 max | R3 flips | R4 appeared/disappeared |"]  # fmt: skip
    lines += ["|---|---|---|---|---|---|---|---|---|---|"]
    alarmed = {a.split(":", 1)[1] for a in doc.get("alarms", []) if a.startswith("rest_revision_ratio:")}
    for k, b in doc["bars"].items():
        imp = (b.get("revision_impact") or {}).get(CHAMPION) or {}
        ratio = _fmt(b["rest_revision_ratio"]) + (" ALARM (diagnostic)" if k in alarmed else "")
        r1 = _fmt(imp.get("share")) + (" (extended block)" if imp.get("block_kind") not in (None, "day") else "")
        r4 = f"{_fmt(b.get('rest_revisions_appeared'))}/{_fmt(b.get('rest_revisions_disappeared'))}"
        lines.append(
            f"| {k} | {_fmt(b['rest'])} | {_fmt(b['flat'])} | {_fmt(b['flat_unproven'])} | {_fmt(b['lag_s_p99'])} | "
            f"{ratio} | {r1} | {_fmt(imp.get('max_shift'))} | "
            f"{_fmt(imp.get('flip_share')) if imp.get('r3') == 'measured' else _fmt(imp.get('r3'))} | {r4} |"
        )
    c = doc["costs"]
    lines += ["", "## Costs (L4)", ""]
    lines.append(f"maker_fill_rate {_fmt(c['maker_fill_rate'])}, latency_ms_p75 {_fmt(c['latency_ms_p75'])}")
    lines.append(f"median spread {_fmt(c['median_spread_bps'])}")
    lines.append(f"fees {_fmt(c['fee_maker_bps'])}/{_fmt(c['fee_taker_bps'])} ({_fmt(c['fee_source'])})")
    lbl = doc["labels"]
    lines += ["", "## Labels", "", f"resolved_total {lbl['resolved_total']}, first resolution expected "
              f"{_fmt(lbl['first_resolution_expected'])}"]  # fmt: skip
    adv = doc.get("rollback_advice") or {}
    if adv:
        lines += ["", "## Rollback advice (a recommendation, not an action)", ""]
        lines.append(f"champion {_fmt(adv.get('version'))} since {_fmt(adv.get('since'))}: advice {adv['advice']}")
        lines += [f"- trigger: {t}" for t in adv.get("triggers", [])]
        eco = adv.get("economics") or {}
        if eco:
            lines.append(f"economics {_fmt(eco.get('status'))}, trades of the version {_fmt(eco.get('closed_trades'))}")
        if adv["advice"] != "none":
            lines.append(f"command (local, a human): `{adv['command']}`")
    for title, gate in (("G-DRY-OPS", ops_gate), ("G-DRY-ECON", econ)):
        if gate is None:
            continue
        lines += ["", f"## {title}: {gate['status']}", ""]
        lines += ["| criterion | status | value | threshold | note |", "|---|---|---|---|---|"]
        lines += [
            f"| {c_['name']} | {c_['status']} | {_fmt(c_['value'])} | {_fmt(c_['threshold'])} | {_fmt(c_['note'])} |"
            for c_ in gate["criteria"]
        ]  # fmt: skip
        for dg in gate.get("diagnostics", []):
            if "alarm" not in dg:  # a diagnostic with no threshold at all
                lines.append(f"\ndiagnostic {dg['name']}: {_fmt(dg['value'])} (no threshold, not in the gate status)")
                continue
            state = "ALARM (diagnostic, not in the gate status)" if dg["alarm"] else "ok (diagnostic)"
            shown = f"{_fmt(dg['value'])} against {_fmt(dg['alarm_threshold'])}"
            lines.append(f"\ndiagnostic {dg['name']}: {shown}: {state}")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------------------------------ CLI


def build_reporter(config_path: str, replay_dir: str | None, expected_buy_rate: float | None) -> DryReporter:
    from modules.config import load_config

    app = load_config(config_path)
    data = Path(app.paths.data_dir)
    paths = ReportPaths(
        decisions_dir=data / "decisions", equity_dir=data / "equity", learner_dir=data / "reports" / "learner",
        labels_dir=data / "live_labels", out_dir=data / "reports" / "dry",
        replay_dir=None if replay_dir is None else Path(replay_dir),
        label_config=data / "datasets" / "label_config.json",
    )  # fmt: skip
    return DryReporter(
        app.dry_run, paths, SqliteTradeSource(app.dry_run.db_path), JsonOpsSource(data / "reports" / "ops"),
        mode=app.execution.mode, expected_buy_rate=expected_buy_rate,
        champion=RegistryChampionSource(Path(app.paths.models_dir) / "registry"),
        max_fallback_ratio=app.risk.kill_switch.max_fallback_ratio_1h,
    )  # fmt: skip


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m modules.dryrun.report", description="Dry-run report and gates")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--book", default=None, help="book the gates are evaluated on (default: champion)")
    ap.add_argument("--date", default=None, help="last UTC day of the window (default: yesterday)")
    ap.add_argument("--config", default="config/config.yml")
    ap.add_argument("--replay-dir", default=None)
    ap.add_argument("--expected-buys-per-day", type=float, default=None)
    args = ap.parse_args(argv)
    from modules.config import EXIT_CONFIG_ERROR, ConfigError

    if args.days < 1:
        print("--days must be at least 1", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    try:
        reporter = build_reporter(args.config, args.replay_dir, args.expected_buys_per_day)
        end = date.fromisoformat(args.date) if args.date else datetime.now(UTC).date() - DAY
    except (ConfigError, ValueError, OSError) as exc:
        print(f"report configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    result = reporter.generate(end, args.days, book=args.book)
    for key in ("gate_ops", "gate_econ"):
        g = result[key]
        print(f"{key}: {g['status']}")
        for c in g["criteria"]:
            print(f"  {c['name']}: {c['status']} (value {_fmt(c['value'])}, threshold {_fmt(c['threshold'])})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
