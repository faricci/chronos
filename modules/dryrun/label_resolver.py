"""LabelResolver: the bot learns from the decisions it did not take (spec sez. 5.1.5, 5.2, 5.3 punto 10).

Every minute and product the engine writes a ``DecisionRecord`` (``data/decisions/YYYY-MM-DD.jsonl``). Once the data a
label can read exists, this module labels the record: ``label_window_end(t) <= now`` (``training.labeling``, the single
definition of the window) and the 1m store is known complete up to the end of that window. The label is computed by
the very functions of the historical dataset: ``triple_barrier`` and ``forward_rv_bucket`` of ``training/labeling.py``
over the definitive bars of the store, with the ATR of ``compute_indicators`` (the batch twin of the live indicators,
pinned to them by ``tests/test_indicators_parity.py``). No fill rule, cost or barrier is defined in this file.

Output: ``data/live_labels/YYYY-MM-DD.jsonl`` (day of the decision), one line per (decision, question) in the format of
sez. 3.2.1 with ``split: "live"``, ``fold: -1`` and ``weight: 1.0`` (uniqueness weights need the whole sample and are
computed at training time), plus ``decision_id``, ``predicted``, ``policy_final`` and ``resolved_at`` (sez. 5.2).

Same conventions as the dataset builder:

* ``action`` (FLAT states): BUY if UPPER and ``net_ret > 0``, else HOLD (NO_FILL included). The ``action`` of a LONG
  state needs the real bracket of the position, which a ``DecisionRecord`` does not carry: it is not labelled;
* ``edge``: UPPER or not; NO_FILL has no ``edge`` label (the question is conditional on the order being filled);
* ``vol_regime``: bucket 0..4, no label where ``forward_rv_bucket`` gives none (first year of history, window end);
* ``UNRESOLVED`` is not a label. Missing ATR (the dataset excludes those samples) is final: the decision is recorded
  as ``no_atr`` and never retried. Anything else that leaves a window unobserved is retried at the next run.

``store_complete_until_end``: the bars are loaded up to ``store.complete_until(product)`` with trailing flat minutes
only because the store says it is complete up to there; a decision whose window ends later waits.

Idempotent and restartable: ``resolved.db`` (``resolved(decision_id PRIMARY KEY)``) is written AFTER the lines are on
disk and fsynced, and a (decision, question) already in the day file is never written again, so a crash between the
two converges at the next run. The ``id`` of a line is the dataset's (product, instant, side, question): the books of
the dry run decide at the same instants, so ``decision_id`` is what tells their lines apart. A torn last line of a day
file is cut off before appending.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import threading
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Protocol

import numpy as np
import pandas as pd
import structlog

from modules.decision.protocol import ACTIONS, REGIME_LEGEND
from modules.market.backfill import load_bars
from modules.market.indicators_batch import BarGapError, compute_indicators
from modules.market.timeframes import aggregate_bars
from modules.risk.costs import round_bps
from modules.risk.label_config import LoadedLabelConfig
from training.labeling import (
    LOWER,
    NO_FILL,
    UNRESOLVED,
    UPPER,
    VERTICAL,
    TripleBarrierResult,
    forward_rv_bucket,
    label_window_end,
    triple_barrier,
)

__all__ = ["LabelResolver", "ResolveReport", "ResolverStats", "StoreProgress"]

log = structlog.get_logger(__name__)

Clock = Callable[[], datetime]
MINUTE: Final = timedelta(minutes=1)
OUTCOME_NAMES: Final = {NO_FILL: "NO_FILL", UPPER: "UPPER", LOWER: "LOWER", VERTICAL: "VERTICAL"}
FOLD_NONE: Final = -1  # same value as the dataset builder: "not in any test fold"
VOL_OPTIONS: Final = [str(i) for i in range(len(REGIME_LEGEND))]
_KEY: Final = re.compile(rb'^\{"id":"[^"]*\|([a-z_]+)","decision_id":"([^"]+)"')
TAIL_BYTES: Final = 1 << 20


class StoreProgress(Protocol):
    """``BarStoreWriter``: how far the 1m store is complete (flat minutes included)."""

    def complete_until(self, product_id: str) -> datetime: ...


@dataclass(slots=True)
class ResolverStats:
    runs: int = 0
    resolved: int = 0
    no_fill: int = 0
    lines: int = 0
    deferred: int = 0
    no_atr: int = 0
    empty: int = 0
    atr_state_mismatch: int = 0  # the ATR token of the record differs from the one of the stored bars (parity alarm)
    bad_lines: int = 0
    failures: int = 0


@dataclass(frozen=True, slots=True)
class ResolveReport:
    resolved: int = 0
    lines: int = 0
    deferred: int = 0
    tombstoned: int = 0


@dataclass(frozen=True, slots=True)
class _Pending:
    decision_id: str
    product: str
    t: datetime
    window_end: datetime
    rec: dict[str, Any]
    label: LoadedLabelConfig


@dataclass(slots=True)
class _FileState:
    finished: bool = False
    ids: set[str] = field(default_factory=set)  # (decision, question) of an output day file, loaded when appended to


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_t(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


class LabelResolver:
    def __init__(
        self,
        *,
        decisions_dir: Path,
        labels_dir: Path,
        bars_root: Path,
        store: StoreProgress,
        label: LoadedLabelConfig,
        clock: Clock,
        label_by_model: Mapping[str, LoadedLabelConfig] | None = None,
        recompute_every_s: float = 3600.0,
    ) -> None:
        """``label_by_model``: ``policy_version`` -> the ``label_config`` of the model that took the decision (the
        default ``label`` for the others). ``recompute_every_s``: the indicators of a product are rebuilt on the whole
        history at most this often (labels are 15 days late: nothing is lost by batching)."""
        self._decisions = Path(decisions_dir)
        self._out = Path(labels_dir)
        self._bars_root = Path(bars_root)
        self._store = store
        self._label = label
        self._by_model = dict(label_by_model or {})
        self._clock = clock
        self._every = timedelta(seconds=recompute_every_s)
        self._last_run: dict[str, datetime] = {}
        self._files: dict[str, _FileState] = {}
        self._out_files: dict[Path, _FileState] = {}
        self._lock = threading.Lock()
        self.stats = ResolverStats()
        self._out.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self._out / "resolved.db", check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS resolved (decision_id TEXT PRIMARY KEY, status TEXT NOT NULL, "
            "resolved_at TEXT NOT NULL)"
        )
        self._db.commit()
        self._done: set[str] = {r[0] for r in self._db.execute("SELECT decision_id FROM resolved")}

    def close(self) -> None:
        self._db.close()

    # ------------------------------------------------------------------------------------------ the loop

    async def run(self, interval_s: float = 60.0, *, sleep: Callable[[float], Any] = asyncio.sleep) -> None:
        """Every ``interval_s`` seconds (spec: 60) resolve what is ready. A failed pass is logged, never fatal."""
        while True:
            try:
                await asyncio.to_thread(self.run_once)
            except Exception as exc:
                self.stats.failures += 1
                log.error("label_resolver_failed", error=f"{type(exc).__name__}: {exc}")
            await sleep(interval_s)

    # ---------------------------------------------------------------------------------------- one pass

    def run_once(self, *, force: bool = False) -> ResolveReport:
        """Label every decision that is ready. ``force`` ignores ``recompute_every_s``."""
        with self._lock:
            self.stats.runs += 1
            now = self._clock()
            ready = self._scan(now)
            report = ResolveReport()
            for product in sorted(ready):
                last = self._last_run.get(product)
                if not force and last is not None and now - last < self._every:
                    continue
                part = self._resolve_product(product, ready[product], now)
                self._last_run[product] = now
                report = ResolveReport(
                    report.resolved + part.resolved, report.lines + part.lines, report.deferred + part.deferred,
                    report.tombstoned + part.tombstoned,
                )  # fmt: skip
            return report

    # ------------------------------------------------------------------------------------------- scan

    def _label_for(self, rec: Mapping[str, Any]) -> LoadedLabelConfig:
        policy = rec.get("policy")
        version = policy.get("policy_version") if isinstance(policy, dict) else None
        return self._by_model.get(version, self._label) if isinstance(version, str) else self._label

    def _scan(self, now: datetime) -> dict[str, list[_Pending]]:
        """Decisions whose label window has ended, by product. Files that cannot hold any are not even opened."""
        if not self._decisions.exists():
            return {}
        horizons = [self._label.cfg.horizon_min, *(lc.cfg.horizon_min for lc in self._by_model.values())]
        min_h = timedelta(minutes=min(horizons))
        ready: dict[str, list[_Pending]] = defaultdict(list)
        for path in sorted(self._decisions.glob("*.jsonl")):
            try:
                day = date.fromisoformat(path.stem)
            except ValueError:
                continue
            state = self._files.setdefault(path.name, _FileState())
            day_start = datetime(day.year, day.month, day.day, tzinfo=UTC)
            if state.finished or day_start - timedelta(days=1) + min_h > now:
                continue  # a decision of this day cannot be ready yet (one day of margin for the lag of the bars)
            candidates: list[tuple[str, str, datetime, dict[str, Any], LoadedLabelConfig]] = []
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                    did, product, ts, st = rec["decision_id"], rec["product"], rec["ts_bar_close"], rec.get("state")
                except (ValueError, KeyError, TypeError):
                    self.stats.bad_lines += 1  # a torn last line, or a record of another shape: not ours to label
                    continue
                if not isinstance(st, str) or "error" in rec:
                    continue  # an engine_error record has no state: nothing to label, nothing to wait for
                if did not in self._done:
                    candidates.append((did, product, _parse_t(ts), rec, self._label_for(rec)))
            waiting = bool(candidates)
            by_label: dict[int, list[tuple[str, str, datetime, dict[str, Any], LoadedLabelConfig]]] = defaultdict(list)
            for c in candidates:
                by_label[id(c[4])].append(c)
            for group in by_label.values():
                ends = label_window_end(pd.DatetimeIndex([c[2] for c in group]), group[0][4].cfg).to_pydatetime()
                for (did, product, t, rec, label), end in zip(group, ends, strict=True):
                    if end <= now:
                        ready[product].append(_Pending(did, product, t, end, rec, label))
            if not waiting and now >= day_start + timedelta(days=1):
                state.finished = True  # the day is over and every record of it is done: never read it again
        return dict(ready)

    # ------------------------------------------------------------------------------------- one product

    def _resolve_product(self, product: str, items: list[_Pending], now: datetime) -> ResolveReport:
        done_until = self._store.complete_until(product)
        usable = [p for p in items if p.window_end - MINUTE <= done_until]
        deferred = len(items) - len(usable)
        if not usable:
            self.stats.deferred += deferred
            return ResolveReport(deferred=deferred)
        bars = load_bars(self._bars_root, product, end=done_until, store_complete_until_end=True)
        if bars.empty:
            self.stats.deferred += len(items)
            return ResolveReport(deferred=len(items))
        try:
            ind = compute_indicators(bars)
        except BarGapError as exc:
            log.error("label_resolver_bars_not_contiguous", product=product, error=str(exc))
            self.stats.deferred += len(items)
            return ResolveReport(deferred=len(items))
        close_1h = aggregate_bars(bars, "1h")["close"]
        resolved = lines = tomb = 0
        out_by_day: dict[date, list[dict[str, Any]]] = defaultdict(list)
        finished: list[tuple[_Pending, str]] = []
        by_label: dict[int, list[_Pending]] = defaultdict(list)
        for p in usable:
            by_label[id(p.label)].append(p)
        for group in by_label.values():
            label = group[0].label
            t_close = pd.DatetimeIndex([p.t for p in group])
            t_idx = bars.index.get_indexer(t_close - MINUTE)
            known = np.flatnonzero(t_idx >= 0)
            for i in np.flatnonzero(t_idx < 0):  # before the first stored bar, or in a hole: retried, never invented
                deferred += 1
                log.warning("label_resolver_bar_missing", decision_id=group[int(i)].decision_id, product=product)
            if known.size == 0:
                continue
            rows = t_idx[known].astype(np.int64)
            cfg = label.cfg
            atr = ind[cfg.atr_column].to_numpy(dtype=np.float64)
            spread = np.array([label.spread_for(product, group[int(i)].t) for i in known], dtype=np.float64)
            o, h, lo, c = (bars[k].to_numpy(dtype=np.float64) for k in ("open", "high", "low", "close"))
            res = triple_barrier(o, h, lo, c, atr, rows, cfg, label.fees, label.costs, spread)
            vol = forward_rv_bucket(close_1h, t_close[known], cfg.horizon_min)
            atr_bps = ind[f"atr_14_{cfg.atr_tf}_bps"].to_numpy(dtype=np.float64)
            for j, i in enumerate(known):
                p = group[int(i)]
                row = int(rows[j])
                if not atr[row] > 0.0:  # the dataset leaves these samples out: final
                    finished.append((p, "no_atr"))
                    self.stats.no_atr += 1
                    tomb += 1
                    continue
                outcome = int(res.outcome[j])
                if outcome == UNRESOLVED:
                    deferred += 1  # the window is not fully observed after all: try again later
                    continue
                self._check_atr_token(p, float(atr_bps[row]))
                out = self._lines(p, cfg.atr_tf, row, res, j, int(vol[j]), float(atr_bps[row]), now)
                if not out:
                    finished.append((p, "empty"))
                    self.stats.empty += 1
                    tomb += 1
                    continue
                out_by_day[p.t.date()].extend(out)
                finished.append((p, "labelled"))
                resolved += 1
                lines += len(out)
                self.stats.no_fill += outcome == NO_FILL
        self._commit(out_by_day, finished, now)
        self.stats.resolved += resolved
        self.stats.lines += lines
        self.stats.deferred += deferred
        return ResolveReport(resolved, lines, deferred, tomb)

    def _check_atr_token(self, p: _Pending, recomputed_bps: float) -> None:
        """The ATR token the decision saw must be the one of the stored bars, or the store is not the live series."""
        try:
            seen = json.loads(p.rec["state"])["volatility"][f"atr_14_{p.label.cfg.atr_tf}_bps"]
        except (ValueError, KeyError, TypeError):
            return
        if seen != max(0, min(10**6, int(round(recomputed_bps)))):
            self.stats.atr_state_mismatch += 1
            log.warning("label_resolver_atr_mismatch", decision_id=p.decision_id, state=seen, bars=recomputed_bps)

    # -------------------------------------------------------------------------------------- the lines

    def _lines(
        self,
        p: _Pending,
        atr_tf: str,
        row: int,
        res: TripleBarrierResult,
        j: int,
        vol: int,
        atr_bps: float,
        now: datetime,
    ) -> list[dict[str, Any]]:
        """The lines of one decision, in the dataset's format (see ``training.build_dataset._jsonl_records``)."""
        rec = p.rec
        side = json.loads(rec["state"])["position"]["side"]
        outcome = int(res.outcome[j])
        filled = outcome != NO_FILL
        net = float(res.net_ret[j])
        answers = rec.get("answers") if isinstance(rec.get("answers"), dict) else None
        predicted = {
            "action": None if answers is None else answers["action"]["probabilities"],
            "edge": None if answers is None else answers["edge"]["noul"],
            "vol_regime": None if answers is None else answers["vol_regime"]["score"],
        }
        raw_policy = rec.get("policy")
        policy: dict[str, Any] = raw_policy if isinstance(raw_policy, dict) else {}
        meta = {
            "barrier_outcome": OUTCOME_NAMES[outcome],
            "net_ret_bps": None if np.isnan(net) else int(round_bps(1e4 * net)),
            "fill_minute": int(res.fill_idx[j]) - row if filled else None,
            "exit_minute": int(res.exit_idx[j]) - row if filled else None,
            "atr_tf": atr_tf,
            "atr_bps": int(round_bps(atr_bps)),
            "label_config_sha256": p.label.sha256,
            "questions_version": rec.get("questions_version"),
            "sentiment_dropped": False,
        }
        specs: list[tuple[str, str, list[str], str]] = []
        if side == "FLAT":  # the action of a LONG state depends on the real bracket, which the record does not hold
            hit = outcome == UPPER and net > 0
            specs.append(("action", "choice", list(ACTIONS), "BUY" if hit else "HOLD"))
        if filled:
            specs.append(("edge", "noul", ["true", "false"], "true" if outcome == UPPER else "false"))
        if vol >= 0:
            specs.append(("vol_regime", "score", VOL_OPTIONS, str(vol)))
        stamp, resolved_at = _iso(p.t), _iso(now)
        return [
            {
                "id": f"{p.product}|{stamp}|{side}|{key}",
                "decision_id": p.decision_id,
                "fold": FOLD_NONE,
                "split": "live",
                "product": p.product,
                "t": stamp,
                "position_side": side,
                "state": rec["state"],
                "question_key": key,
                "question_type": qtype,
                "options": options,
                "target": target,
                "target_index": options.index(target),
                "weight": 1.0,
                "meta": meta,
                "predicted": predicted,
                "policy_final": policy.get("final"),
                "resolved_at": resolved_at,
            }
            for key, qtype, options, target in specs
        ]

    # ----------------------------------------------------------------------------------------- output

    def _commit(
        self, out_by_day: dict[date, list[dict[str, Any]]], finished: list[tuple[_Pending, str]], now: datetime
    ) -> None:
        """Lines first (one fsync per day file), table after: a decision is done only once its lines are durable."""
        for day, lines in out_by_day.items():
            self._append(day, lines)
        self._db.executemany(
            "INSERT OR IGNORE INTO resolved (decision_id, status, resolved_at) VALUES (?, ?, ?)",
            [(p.decision_id, status, _iso(now)) for p, status in finished],
        )
        self._db.commit()
        self._done.update(p.decision_id for p, _ in finished)

    def _append(self, day: date, lines: list[dict[str, Any]]) -> None:
        path = self._out / f"{day.isoformat()}.jsonl"
        state = self._out_files.setdefault(path, _FileState())
        if not state.ids and path.exists():
            _cut_torn_tail(path)
            state.ids = _line_ids(path)
        fresh = [ln for ln in lines if _key(ln) not in state.ids]  # a crash between file and table: no duplicates
        if not fresh:
            return
        data = "".join(json.dumps(ln, separators=(",", ":"), allow_nan=False) + "\n" for ln in fresh).encode("utf-8")
        with path.open("ab") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        state.ids.update(_key(ln) for ln in fresh)


def _key(line: dict[str, Any]) -> str:
    """What makes a line unique: the same product and instant are labelled once per decision (one per book / model)."""
    return f"{line['decision_id']}|{line['question_key']}"


def _line_ids(path: Path) -> set[str]:
    ids: set[str] = set()
    with path.open("rb") as fh:
        for raw in fh:
            m = _KEY.match(raw)
            if m:
                ids.add(f"{m.group(2).decode('utf-8')}|{m.group(1).decode('utf-8')}")
    return ids


def _cut_torn_tail(path: Path) -> None:
    """A crash can leave half a line at the end of the file: drop it so the next append starts on a line boundary."""
    size = path.stat().st_size
    if size == 0:
        return
    with path.open("r+b") as fh:
        fh.seek(max(0, size - TAIL_BYTES))
        tail = fh.read()
        if tail.endswith(b"\n"):
            return
        base, cut = max(0, size - TAIL_BYTES), tail.rfind(b"\n")
        if cut < 0 and base > 0:
            return  # a "line" longer than the window we read: not something this module wrote
        fh.truncate(base + cut + 1 if cut >= 0 else 0)
        log.warning("label_resolver_torn_tail_cut", path=str(path))
