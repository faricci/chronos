"""Parity of the real orders with their shadow twin: the daily ``ParityReport`` (spec sez. 5.1.7, 5.3 punto 14).

Only meaningful in live: every real intent is sent, unchanged, to the ``ShadowExecutionGateway`` of a twin book, and
the two outcomes are compared. Nothing here sends an order or touches a balance; this module is the interface
(``TwinMirror``, ``ParityLog``) and the calculation (``compute_parity``). Its numbers are checked against the
tolerances of the live ramp (``live_ramp.parity``) and are the evidence that recalibrates ``latency_ms``,
``queue_haircut`` and ``stop_extra_slip_bps`` (step L4); it decides nothing by itself.

What is compared, per intent that reached the real gateway:

* the entry: average fill price, fees (in bps of the notional) and the time the entry took to end, real versus
  simulated. ``delta`` is ``(sim - real) / real`` in bps, so a positive number means the simulation paid more;
* the bracket: how the trade ended on each side (``TP``, ``SL``, ``SELL``, ``HORIZON``, ``WATCHDOG``, ``MANUAL``,
  ``DUST`` for a fill too small to protect), compared only on trades CLOSED on both sides. A trade closed on one side
  and still open on the other is ``outcome_pending``: it is not an agreement and not a disagreement yet.

Tolerances (``ParityTolerance``): the MEDIAN of the absolute entry delta at most ``max_median_entry_delta_bps`` (10),
and the share of trades with the same bracket outcome at least ``min_bracket_outcome_agreement`` (0.90). A check with no
data does not pass and does not fail: the report says ``no_data`` when neither could be computed.

Anomalies are failures of the comparison itself, not differences of outcome: a real intent the twin never answered
(``missing_twin``) and a twin that raised (``twin_errors``). They are counted apart because rule L5 wants "no parity
anomalies" (sez. 5.1.6) and the ramp wants a twin that was actually there.

The twin must never hurt the real order: ``TwinMirror`` returns the real result, the twin runs beside it and its
failures are recorded, not raised.
"""

from __future__ import annotations

import asyncio
import json
import statistics
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

import structlog

from modules.config import ParityTolerance

if TYPE_CHECKING:
    from modules.execution.gateway import CancelOutcome, EntryResult, ExitResult, GatewayContract
    from modules.risk.risk_manager import ExitIntent, OrderIntent

__all__ = [
    "LegOutcome",
    "ParityLog",
    "ParityPair",
    "ParityReport",
    "Side",
    "TwinMirror",
    "compute_parity",
]

log = structlog.get_logger(__name__)

Side = Literal["real", "sim"]
Status = Literal["ok", "breach", "no_data"]
ZERO: Final = Decimal(0)
BPS: Final = Decimal(10_000)


# --------------------------------------------------------------------------------------------- data


@dataclass(frozen=True)
class LegOutcome:
    """What one side (real or simulated) did with one intent."""

    intent_id: str
    product_id: str
    submitted_at: datetime
    status: str  # EntryResult.status of that side
    entry_done_at: datetime | None
    filled_size: Decimal
    entry_avg_price: Decimal | None
    entry_fees: Decimal
    exit_reason: str | None  # None while the trade is open (or never opened)
    error: str | None = None  # the side raised instead of answering

    @property
    def filled(self) -> bool:
        return self.filled_size > 0 and self.entry_avg_price is not None

    @property
    def fee_bps(self) -> Decimal | None:
        if not self.filled or self.entry_avg_price is None:
            return None
        notional = self.entry_avg_price * self.filled_size
        return None if notional <= 0 else self.entry_fees / notional * BPS

    @property
    def entry_seconds(self) -> float | None:
        return None if self.entry_done_at is None else (self.entry_done_at - self.submitted_at).total_seconds()


@dataclass(frozen=True)
class ParityPair:
    """One intent seen by both sides. ``sim`` is ``None`` when the twin never answered."""

    real: LegOutcome
    sim: LegOutcome | None


@dataclass(frozen=True)
class ParityReport:
    date: date
    n_intents: int
    n_both_filled: int
    fill_agreement: float | None  # share of intents that ended filled on both sides or unfilled on both
    entry_delta_bps_median_abs: float | None
    entry_delta_bps_median: float | None  # signed: the bias of the simulation
    fee_delta_bps_median: float | None
    entry_seconds_delta_median: float | None  # sim - real
    n_closed_both: int
    n_outcome_agree: int
    bracket_agreement: float | None
    outcome_pending: int
    missing_twin: int
    twin_errors: int
    entry_within: bool | None
    bracket_within: bool | None
    status: Status
    tolerance: ParityTolerance

    @property
    def anomalies(self) -> int:
        return self.missing_twin + self.twin_errors

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready (the ``parity`` section of the daily report)."""
        out = {k: getattr(self, k) for k in self.__dataclass_fields__ if k not in ("date", "tolerance")}
        out["date"] = self.date.isoformat()
        out["anomalies"] = self.anomalies
        out["tolerance"] = self.tolerance.model_dump()
        return out

    def write(self, directory: Path) -> Path:
        """``<directory>/<date>.json`` (written whole, then moved into place)."""
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.date.isoformat()}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
        tmp.replace(path)
        return path


# ---------------------------------------------------------------------------------------- calculation


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def compute_parity(pairs: Iterable[ParityPair], day: date, tolerance: ParityTolerance | None = None) -> ParityReport:
    """The report of ``day`` from the pairs (all of them intents submitted on that UTC day)."""
    tol = tolerance or ParityTolerance()
    pairs = list(pairs)
    deltas: list[float] = []
    fee_deltas: list[float] = []
    seconds: list[float] = []
    same_fill = n_answered = both_filled = 0
    closed_both = agree = pending = 0
    missing = errors = 0
    for pair in pairs:
        real, sim = pair.real, pair.sim
        if sim is None:
            missing += 1
            continue
        if sim.error is not None:
            errors += 1
            continue
        n_answered += 1
        same_fill += real.filled == sim.filled
        if real.filled and sim.filled:
            both_filled += 1
            assert real.entry_avg_price is not None and sim.entry_avg_price is not None
            deltas.append(float((sim.entry_avg_price - real.entry_avg_price) / real.entry_avg_price * BPS))
            fee_r, fee_s = real.fee_bps, sim.fee_bps
            if fee_r is not None and fee_s is not None:
                fee_deltas.append(float(fee_s - fee_r))
            if real.entry_seconds is not None and sim.entry_seconds is not None:
                seconds.append(sim.entry_seconds - real.entry_seconds)
            if real.exit_reason is not None and sim.exit_reason is not None:
                closed_both += 1
                agree += real.exit_reason == sim.exit_reason
            elif real.exit_reason is not None or sim.exit_reason is not None:
                pending += 1

    median_abs = _median([abs(d) for d in deltas])
    agreement = agree / closed_both if closed_both else None
    entry_ok = None if median_abs is None else median_abs <= tol.max_median_entry_delta_bps
    bracket_ok = None if agreement is None else agreement >= tol.min_bracket_outcome_agreement
    checks = [c for c in (entry_ok, bracket_ok) if c is not None]
    status: Status = "no_data" if not checks else ("ok" if all(checks) else "breach")
    return ParityReport(
        date=day,
        n_intents=len(pairs),
        n_both_filled=both_filled,
        fill_agreement=None if n_answered == 0 else same_fill / n_answered,
        entry_delta_bps_median_abs=median_abs,
        entry_delta_bps_median=_median(deltas),
        fee_delta_bps_median=_median(fee_deltas),
        entry_seconds_delta_median=_median(seconds),
        n_closed_both=closed_both,
        n_outcome_agree=agree,
        bracket_agreement=agreement,
        outcome_pending=pending,
        missing_twin=missing,
        twin_errors=errors,
        entry_within=entry_ok,
        bracket_within=bracket_ok,
        status=status,
        tolerance=tol,
    )


# ------------------------------------------------------------------------------------------ the log


@dataclass
class _Leg:
    intent_id: str
    product_id: str
    submitted_at: datetime
    status: str = "pending"
    entry_done_at: datetime | None = None
    filled_size: Decimal = ZERO
    entry_avg_price: Decimal | None = None
    entry_fees: Decimal = ZERO
    exit_reason: str | None = None
    error: str | None = None
    answered: bool = False

    def snapshot(self) -> LegOutcome:
        return LegOutcome(
            self.intent_id, self.product_id, self.submitted_at, self.status, self.entry_done_at, self.filled_size,
            self.entry_avg_price, self.entry_fees, self.exit_reason, self.error,
        )  # fmt: skip


class ParityLog:
    """Collects the real and simulated outcomes of the intents of a live session, keyed by ``intent_id``.

    Fed by ``TwinMirror`` (entries and exits it sees) and by the wiring (brackets that close on their own, on both
    sides). Single event loop, no locks. Memory: one small record per intent; ``prune`` drops the days already reported.
    """

    def __init__(self) -> None:
        self._real: dict[str, _Leg] = {}
        self._sim: dict[str, _Leg] = {}
        self._open: dict[tuple[Side, str], str] = {}  # (side, product) -> intent_id of the trade not closed yet
        self.unmatched_events = 0

    # ----------------------------------------------------------------------------------- events

    def submitted(self, intent_id: str, product_id: str, at: datetime) -> None:
        """The real intent reached the mirror: both sides start waiting for an answer."""
        if intent_id in self._real:
            raise ValueError(f"intent {intent_id} was already submitted")
        self._real[intent_id] = _Leg(intent_id, product_id, at)
        self._sim[intent_id] = _Leg(intent_id, product_id, at)

    def entry_done(self, side: Side, intent_id: str, result: EntryResult, at: datetime) -> None:
        leg = self._leg(side, intent_id)
        if leg is None:
            return
        leg.answered = True
        leg.status = result.status
        leg.entry_done_at = at
        leg.filled_size = result.filled_size
        leg.entry_avg_price = result.avg_price
        leg.entry_fees = result.fees
        if result.filled_size > 0:
            if result.bracket == "dust":
                leg.exit_reason = "DUST"  # nothing to protect and nothing that can close: the trade is over
            else:
                self._open[side, leg.product_id] = intent_id

    def entry_failed(self, side: Side, intent_id: str, error: str, at: datetime) -> None:
        leg = self._leg(side, intent_id)
        if leg is not None:
            leg.answered = True
            leg.status = "error"
            leg.entry_done_at = at
            leg.error = error

    def trade_closed(self, side: Side, product_id: str, reason: str) -> None:
        """The open trade of ``product_id`` on ``side`` ended (bracket, model exit, watchdog, manual)."""
        intent_id = self._open.pop((side, product_id), None)
        leg = None if intent_id is None else self._leg(side, intent_id)
        if leg is None:
            self.unmatched_events += 1
            return
        leg.exit_reason = reason

    def _leg(self, side: Side, intent_id: str) -> _Leg | None:
        leg = (self._real if side == "real" else self._sim).get(intent_id)
        if leg is None:
            self.unmatched_events += 1
        return leg

    # ------------------------------------------------------------------------------------ report

    def pairs(self, day: date) -> list[ParityPair]:
        """The intents submitted on ``day`` (UTC). A twin that never answered is ``sim=None``."""
        out: list[ParityPair] = []
        for intent_id, real in self._real.items():
            if real.submitted_at.astimezone(UTC).date() != day:
                continue
            sim = self._sim[intent_id]
            out.append(ParityPair(real.snapshot(), sim.snapshot() if sim.answered else None))
        return out

    def report(self, day: date, tolerance: ParityTolerance | None = None) -> ParityReport:
        return compute_parity(self.pairs(day), day, tolerance)

    def prune(self, before: date) -> int:
        """Forget intents submitted before ``before`` that are no longer open. Returns how many were dropped."""
        busy = set(self._open.values())
        drop = [
            i for i, leg in self._real.items()
            if leg.submitted_at.astimezone(UTC).date() < before and i not in busy
        ]  # fmt: skip
        for i in drop:
            del self._real[i]
            del self._sim[i]
        return len(drop)


# ---------------------------------------------------------------------------------------- the mirror


class TwinMirror:
    """The gateway the live engine talks to: the REAL gateway decides what happens, the twin gets the same intent.

    The real call is started first and its result (or its exception) is what the caller gets, whatever the twin does.
    The twin runs beside it as a task: it cannot delay the real answer, and a twin failure is logged and recorded in
    the ``ParityLog``, never raised. ``twin_book`` is the book name of the twin gateway; the intent is re-addressed to
    it and is otherwise identical (same ids, prices, size, bracket).
    """

    def __init__(
        self,
        real: GatewayContract,
        twin: GatewayContract,
        twin_book: str,
        parity: ParityLog,
        clock: Callable[[], datetime],
    ) -> None:
        self._real = real
        self._twin = twin
        self._twin_book = twin_book
        self._parity = parity
        self._clock = clock
        self._tasks: set[asyncio.Task[None]] = set()

    async def submit_entry_with_bracket(self, intent: OrderIntent) -> EntryResult:
        self._parity.submitted(intent.intent_id, intent.product_id, self._clock())
        real = asyncio.ensure_future(self._real.submit_entry_with_bracket(intent))
        self._spawn(self._twin_entry(intent))
        try:
            result = await real
        except Exception as exc:
            self._parity.entry_failed("real", intent.intent_id, f"{type(exc).__name__}: {exc}", self._clock())
            raise
        self._parity.entry_done("real", intent.intent_id, result, self._clock())
        return result

    async def submit_exit(self, intent: ExitIntent) -> ExitResult:
        real = asyncio.ensure_future(self._real.submit_exit(intent))
        self._spawn(self._twin_exit(intent))
        result = await real
        self._real_exit(intent.product_id, result)
        return result

    async def emergency_exit(self, product_id: str) -> ExitResult:
        real = asyncio.ensure_future(self._real.emergency_exit(product_id))
        self._spawn(self._twin_emergency(product_id))
        result = await real
        self._real_exit(product_id, result)
        return result

    async def cancel(self, product_id: str) -> CancelOutcome:
        real = asyncio.ensure_future(self._real.cancel(product_id))
        self._spawn(self._twin_cancel(product_id))
        return await real

    def on_real_closed(self, product_id: str, reason: str) -> None:
        """A real bracket (or any close not made through this mirror) ended the trade of ``product_id``."""
        self._parity.trade_closed("real", product_id, reason)

    def on_sim_closed(self, product_id: str, reason: str) -> None:
        """The twin's bracket closed the trade (the twin book's ``on_closed`` callback)."""
        self._parity.trade_closed("sim", product_id, reason)

    async def drain(self) -> None:
        """Wait for the twin calls still running (shutdown). The real calls are the caller's."""
        if self._tasks:
            await asyncio.wait(set(self._tasks))

    # ------------------------------------------------------------------------------------ internals

    def _real_exit(self, product_id: str, result: ExitResult) -> None:
        if result.status == "closed":
            self._parity.trade_closed("real", product_id, result.reason)

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _twin_entry(self, intent: OrderIntent) -> None:
        twin = intent.model_copy(update={"book": self._twin_book})
        try:
            result = await self._twin.submit_entry_with_bracket(twin)
        except Exception as exc:  # the twin never hurts the real order
            log.error("parity_twin_failed", leg="entry", intent_id=intent.intent_id, error=repr(exc))
            self._parity.entry_failed("sim", intent.intent_id, f"{type(exc).__name__}: {exc}", self._clock())
            return
        self._parity.entry_done("sim", intent.intent_id, result, self._clock())

    async def _twin_exit(self, intent: ExitIntent) -> None:
        try:
            result = await self._twin.submit_exit(intent.model_copy(update={"book": self._twin_book}))
        except Exception as exc:
            log.error("parity_twin_failed", leg="exit", intent_id=intent.intent_id, error=repr(exc))
            return
        if result.status == "closed":
            self._parity.trade_closed("sim", intent.product_id, result.reason)

    async def _twin_emergency(self, product_id: str) -> None:
        try:
            result = await self._twin.emergency_exit(product_id)
        except Exception as exc:
            log.error("parity_twin_failed", leg="emergency", product=product_id, error=repr(exc))
            return
        if result.status == "closed":
            self._parity.trade_closed("sim", product_id, result.reason)

    async def _twin_cancel(self, product_id: str) -> None:
        try:
            await self._twin.cancel(product_id)
        except Exception as exc:
            log.error("parity_twin_failed", leg="cancel", product=product_id, error=repr(exc))

