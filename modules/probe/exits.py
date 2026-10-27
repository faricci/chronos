"""Exits of the probe: the one door through which the watchdog, the protection monitor and the hand switches sell.

``ProbeExits`` wraps the live gateway for two things the gateway does not know:

* the REASON of an exit is announced to the ledger before the sale (``expect_exit``), so the closed trade says
  ``HORIZON``,
  ``WATCHDOG`` or ``MANUAL`` and ``ProbeStreaks`` can count it. The reason also goes into the ``decision_id`` of the
  intents built here (``probe-exit:<reason>:<ts>``);
* when an exit that is NOT an emergency fails after the bracket was cancelled (outcome ``refused``, ``rejected``,
  ``unknown``, ``unconfirmed`` or ``partial``) and the position is still LONG without a bracket, the bracket is put back
  at once with ``place_missing_bracket``, without waiting for the reconciler (plan sez. 1.4, last point). The caller
  (the watchdog, the switch watcher) retries the exit later.

Market orders are allowed in exactly four cases (plan sez. 1.2): an emergency exit, the rest of a normal exit after the
limit at the bid, a bracket that could not be placed, and the manual ``FLATTEN``. All of them are the gateway's
sequences;
this class adds no order of its own.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Final, Protocol

import structlog

from modules.execution.balance import Clock, utc_now
from modules.execution.gateway import BracketNotPlaced, ExitResult
from modules.execution.reconciler import BracketRequest
from modules.ledger.positions import PositionLedger
from modules.risk.risk_manager import ExitIntent

log = structlog.get_logger(__name__)

FAILED_STATUSES: Final = frozenset({"refused", "rejected", "unknown", "unconfirmed", "partial"})
EventSink = Callable[[str, dict[str, Any]], None]


class LiveExitSide(Protocol):
    """The part of the live gateway the exits use."""

    async def submit_exit(self, intent: ExitIntent) -> ExitResult: ...
    async def emergency_exit(self, product_id: str) -> ExitResult: ...
    async def place_missing_bracket(self, request: BracketRequest) -> None: ...


def exit_decision_id(reason: str, at: datetime) -> str:
    """``probe-exit:<reason>:<ts>``: one id per attempt, so a retry never reuses the id of a cancelled order."""
    return f"probe-exit:{reason}:{int(at.timestamp() * 1000)}"


class ProbeExits:
    def __init__(
        self,
        gateway: LiveExitSide,
        ledger: PositionLedger,
        *,
        book: str,
        product_id: str,
        on_event: EventSink,
        clock: Clock = utc_now,
    ) -> None:
        self._gateway = gateway
        self._ledger = ledger
        self._book = book
        self._product = product_id
        self._event = on_event
        self._clock = clock

    # --------------------------------------------------------------------------------------- the doors

    async def submit_exit(self, intent: ExitIntent) -> ExitResult:
        """The normal exit (horizon, manual). The ``ExitIntent`` carries the reason."""
        if not intent.decision_id.startswith("probe-exit:"):  # the watchdog names its horizon exits its own way
            did = exit_decision_id(intent.reason, self._clock())
            intent = intent.model_copy(update={"decision_id": did, "intent_id": did})
        self._ledger.expect_exit(self._book, intent.product_id, intent.reason)
        try:
            result = await self._gateway.submit_exit(intent)
        finally:
            self._ledger.clear_exit(self._book, intent.product_id)
        self._event("probe_exit", {"reason": intent.reason, "status": result.status, "detail": result.detail})
        await self._restore_the_bracket_if_needed(result, intent.reason)
        return result

    async def emergency_exit(self, product_id: str) -> ExitResult:
        """The emergency exit: market, reason ``WATCHDOG``. No bracket is put back: the price is already past it."""
        self._ledger.expect_exit(self._book, product_id, "WATCHDOG")
        try:
            result = await self._gateway.emergency_exit(product_id)
        finally:
            self._ledger.clear_exit(self._book, product_id)
        self._event("probe_exit", {"reason": "WATCHDOG", "status": result.status, "detail": result.detail})
        return result

    async def flatten(self) -> ExitResult | None:
        """``FLATTEN``: the position is sold (limit at the bid, then market for the rest), reason ``MANUAL``."""
        position = self._ledger.view(self._book, self._product)
        if not position.is_long:
            return None
        now = self._clock()
        did = exit_decision_id("MANUAL", now)
        intent = ExitIntent(
            intent_id=did,
            decision_id=did,
            product_id=self._product,
            book=self._book,
            base_size=position.base_qty,
            cancel_bracket_order_id=position.bracket_order_id,
            reason="MANUAL",
            created_at=now,
        )
        return await self.submit_exit(intent)

    # ----------------------------------------------------------------------------------- the bracket

    async def _restore_the_bracket_if_needed(self, result: ExitResult, reason: str) -> None:
        if result.status not in FAILED_STATUSES:
            return
        position = self._ledger.view(self._book, self._product)
        if not position.is_long or position.bracket_order_id is not None:
            return
        if position.tp is None or position.sl is None:
            log.error("probe_exit_failed_and_no_levels_to_restore_the_bracket", reason=reason)
            self._event("probe_bracket_restore_impossible", {"reason": reason})
            return
        try:
            await self._gateway.place_missing_bracket(
                BracketRequest(self._book, self._product, position.base_qty, position.tp, position.sl)
            )
            self._event("probe_bracket_restored", {"reason": reason, "after": result.status})
        except BracketNotPlaced as exc:
            log.error("probe_bracket_restore_failed", reason=reason, status=exc.status, why=exc.reason)
            self._event("probe_bracket_restore_failed", {"reason": reason, "status": exc.status, "why": exc.reason})
