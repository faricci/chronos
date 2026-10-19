"""Watchdog of the open positions: stop backup and exit at the horizon (spec sez. 4.1.4, 4.3 punto 10, 5.1.2; C11).

At every ticker, for the LONG position of the product (the book of this instance):

* **Position with an active bracket** (the standalone ``trigger_bracket_gtc``): the exchange should have sold at ``SL``.
  If ``last_price <= SL - sl_breach_atr_1h x ATR_1h`` continuously for ``grace_s`` and the position is still open, the
  bracket failed to execute (the exchange does not guarantee it in fast markets): emergency exit.
* **Position without a usable bracket** (the window between the first fill and the bracket, or a bracket that was
  rejected, cancelled or never recorded): ``last_price <= SL`` is an emergency exit at once. A position that has neither
  a usable bracket nor a known ``SL`` cannot be judged at all and is exited as well (fail closed).
* **Horizon exit** (the vertical barrier of the label, spec 3.1.3): a position older than the horizon is sold at market.
  This closes the hole of spec 5.1.2: nothing else in the runtime would ever close a position that touched neither
  barrier, and the ``edge`` probability is calibrated on exactly that exit.

The label scans bars: the fill happens in the bar ``fill``, the vertical barrier is the bar ``fill + H`` (``H =
horizon_min``, 1 minute bars) and the exit price is the CLOSE of that bar (``training/labeling.py``,
``SimulatedExchange``). The runtime equivalent is a deadline at the close of that bar:
``floor_to_minute(opened_at) + (H + 1) minutes``, see :func:`horizon_deadline` (decision O2.4: "trigger a
ts_bar_close(vertical)").

The watchdog never builds, authorizes or sends an order. It asks an :class:`ExitGateway` (spec 4.3 punto 7:
``emergency_exit`` for the stop backup, ``submit_exit`` with an ``ExitIntent`` for the horizon) which cancels the
bracket, waits for the terminal state and sells, every order of which goes through ``BalanceGuard.authorize``. The
exits run as separate tasks, so a slow exit of one product never delays the ticker handling of the others; at most
one exit per product is in flight, and a failed one is retried after ``retry_s``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final, Literal, Protocol

import structlog

from modules.config import WatchdogConfig
from modules.execution.balance import Clock, utc_now
from modules.ledger.positions import TERMINAL_STATUSES, OrderRecord, PositionView
from modules.market.ws_feed import Quote
from modules.risk.risk_manager import ExitIntent

log = structlog.get_logger(__name__)

ZERO: Final = Decimal(0)
DEFAULT_RETRY_S: Final = 5.0
DEFAULT_HORIZON_CHECK_S: Final = 5.0
BLIND_LOG_EVERY: Final = timedelta(minutes=5)

WatchdogExit = Literal["WATCHDOG", "HORIZON"]  # members of kill_switch.ExitReason

VerdictKind = Literal[
    "flat",  # no LONG position
    "ok",  # nothing to do
    "breach_pending",  # beyond SL - k x ATR, the grace period is running
    "blind",  # bracket present but SL unknown: the backup cannot be evaluated
    "exit_due",  # internal: the rules ask for an exit (becomes exit_started / exit_in_flight / exit_backoff)
    "exit_started",  # an emergency exit was launched
    "exit_in_flight",  # one is already running
    "exit_backoff",  # the last one failed, waiting to retry
]


class ExitGateway(Protocol):
    """The part of ``ExecutionGateway`` (spec 4.3 punto 7) the watchdog uses. Both calls return when the sequence is
    over; every order inside them is authorized by ``BalanceGuard.authorize`` in the gateway, the watchdog sees none.

    * ``emergency_exit`` (stop backup): cancel the pending entry and the bracket, wait for the terminal state (if the
      bracket turns out FILLED nothing is sold) and sell at market. The gateway records the reason ``WATCHDOG``.
    * ``submit_exit`` (horizon): the normal exit path, driven by an ``ExitIntent`` with ``reason="HORIZON"``: cancel the
      bracket with confirmation, marketable limit at the best bid, fallback to market. ``HORIZON`` does not count as a
      stop for the loss streak.
    """

    async def emergency_exit(self, product_id: str) -> object: ...

    async def submit_exit(self, intent: ExitIntent) -> object: ...


class WatchdogLedger(Protocol):
    """The part of ``PositionLedger`` the watchdog reads."""

    def long_positions(self, book: str) -> list[PositionView]: ...
    def view(self, book: str, product_id: str) -> PositionView: ...
    async def open_orders(self, book: str) -> list[OrderRecord]: ...


class AtrSource(Protocol):
    def __call__(self, product_id: str) -> Decimal | None: ...


@dataclass(frozen=True, slots=True)
class Verdict:
    kind: VerdictKind
    product_id: str
    cause: WatchdogExit | None = None
    detail: str = ""


def horizon_deadline(opened_at: datetime, horizon_min: int) -> datetime:
    """Instant at which the vertical barrier of the label is reached for a position opened at ``opened_at``.

    The label fills in the 1 minute bar that contains the fill and exits at the close of the bar ``H`` bars later:
    ``floor_to_minute(opened_at) + (H + 1) minutes``. A position is due at that instant, not before.
    """
    if opened_at.tzinfo is None:
        raise ValueError("opened_at must be timezone-aware (UTC)")
    if horizon_min < 1:
        raise ValueError("horizon_min must be at least 1")
    bar_start = opened_at.astimezone(UTC).replace(second=0, microsecond=0)
    return bar_start + timedelta(minutes=horizon_min + 1)


class Watchdog:
    def __init__(
        self,
        *,
        book: str,
        ledger: WatchdogLedger,
        gateway: ExitGateway,
        cfg: WatchdogConfig,
        horizon_min: int,
        atr_1h: AtrSource,
        clock: Clock = utc_now,
        retry_s: float = DEFAULT_RETRY_S,
        horizon_check_s: float = DEFAULT_HORIZON_CHECK_S,
    ) -> None:
        if horizon_min < 1:
            raise ValueError("horizon_min must be at least 1 (it comes from the label_config of the champion)")
        if retry_s <= 0 or horizon_check_s <= 0:
            raise ValueError("retry_s and horizon_check_s must be positive")
        self.book = book
        self._ledger = ledger
        self._gateway = gateway
        self._cfg = cfg
        self._horizon_min = horizon_min
        self._atr = atr_1h
        self._clock = clock
        self._retry = timedelta(seconds=retry_s)
        self._horizon_check_s = horizon_check_s
        self._breach_since: dict[str, datetime] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._next_attempt: dict[str, datetime] = {}
        self._blind_logged: dict[str, datetime] = {}
        self.exits_started = 0
        self.exits_failed = 0

    # ----------------------------------------------------------------------------------- tickers

    async def on_quote(self, quote: Quote) -> Verdict:
        """Convenience for the market stream: the ticker's last price, converted with ``Decimal(str(x))``."""
        return await self.on_ticker(quote.product_id, Decimal(str(quote.price)))

    async def on_ticker(self, product_id: str, last_price: Decimal) -> Verdict:
        """Judge the position of ``product_id`` against ``last_price`` and the clock."""
        now = self._clock()
        pos = self._ledger.view(self.book, product_id)
        if not pos.is_long:
            self._forget(product_id)
            return Verdict("flat", product_id)
        verdict = await self._price_verdict(pos, last_price, now)
        if verdict.kind != "exit_due":
            horizon = self._horizon_verdict(pos, now)
            if horizon is not None:
                verdict = horizon
        return self._act(verdict, now)

    async def check_horizons(self) -> list[Verdict]:
        """Horizon check of every LONG position, independent of tickers (a silent market must not hold one open)."""
        now = self._clock()
        out: list[Verdict] = []
        for pos in self._ledger.long_positions(self.book):
            verdict = self._horizon_verdict(pos, now)
            out.append(self._act(verdict, now) if verdict is not None else Verdict("ok", pos.product_id))
        return out

    async def run(self) -> None:
        """The horizon loop (a task of the app's ``TaskGroup``); the ticker path is driven by ``on_ticker``."""
        while True:
            try:
                await self.check_horizons()
            except Exception as exc:  # never end the loop: a position past its horizon must still be closed later
                log.error("watchdog_horizon_check_failed", error=f"{type(exc).__name__}: {exc}")
            await asyncio.sleep(self._horizon_check_s)

    async def drain(self) -> None:
        """Wait for the exits in flight (shutdown and tests)."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)

    # ------------------------------------------------------------------------------------ verdicts

    async def _price_verdict(self, pos: PositionView, price: Decimal, now: datetime) -> Verdict:
        product = pos.product_id
        if pos.sl is not None and price > pos.sl:
            self._breach_since.pop(product, None)  # above the stop: neither case can fire
            return Verdict("ok", product)
        if not await self._has_bracket(pos):
            self._breach_since.pop(product, None)
            if pos.sl is None:
                return Verdict("exit_due", product, "WATCHDOG", "no usable bracket and no stop level known")
            return Verdict("exit_due", product, "WATCHDOG", f"no bracket, price {price} <= SL {pos.sl}")
        if pos.sl is None:
            return self._blind(pos, now)
        atr = self._atr(product)
        distance = ZERO if atr is None else Decimal(str(self._cfg.sl_breach_atr_1h)) * atr
        trigger = pos.sl - distance
        if price > trigger:
            self._breach_since.pop(product, None)
            return Verdict("ok", product)
        since = self._breach_since.setdefault(product, now)
        if (now - since).total_seconds() < self._cfg.grace_s:
            return Verdict("breach_pending", product, detail=f"price {price} <= {trigger} since {since.isoformat()}")
        return Verdict(
            "exit_due", product, "WATCHDOG",
            f"bracket did not execute: price {price} <= SL {pos.sl} - {distance} for {(now - since).total_seconds()} s",
        )  # fmt: skip

    def _horizon_verdict(self, pos: PositionView, now: datetime) -> Verdict | None:
        if pos.opened_at is None:
            log.error("watchdog_position_without_opened_at", product_id=pos.product_id)
            return None
        due = horizon_deadline(pos.opened_at, self._horizon_min)
        if now < due:
            return None
        return Verdict("exit_due", pos.product_id, "HORIZON", f"horizon reached at {due.isoformat()}")

    def _blind(self, pos: PositionView, now: datetime) -> Verdict:
        last = self._blind_logged.get(pos.product_id)
        if last is None or now - last >= BLIND_LOG_EVERY:
            self._blind_logged[pos.product_id] = now
            log.error("watchdog_blind", product_id=pos.product_id, bracket=pos.bracket_order_id)
        return Verdict("blind", pos.product_id, detail="bracket present but the ledger has no SL")

    async def _has_bracket(self, pos: PositionView) -> bool:
        """The ledger links an order that is still alive (non terminal), is a SELL on this product."""
        if pos.bracket_order_id is None:
            return False
        for rec in await self._ledger.open_orders(self.book):
            if (
                pos.bracket_order_id in (rec.client_order_id, rec.exchange_order_id)
                and rec.side == "SELL"
                and rec.product_id == pos.product_id
                and rec.status not in TERMINAL_STATUSES
            ):
                return True
        return False

    # ----------------------------------------------------------------------------------------- exits

    def _act(self, verdict: Verdict, now: datetime) -> Verdict:
        if verdict.kind != "exit_due" or verdict.cause is None:
            return verdict
        product = verdict.product_id
        task = self._tasks.get(product)
        if task is not None and not task.done():
            return Verdict("exit_in_flight", product, verdict.cause, verdict.detail)
        if now < self._next_attempt.get(product, now):
            return Verdict("exit_backoff", product, verdict.cause, verdict.detail)
        emit = log.info if verdict.cause == "HORIZON" else log.error  # the horizon is a planned exit, not an incident
        emit("watchdog_exit", book=self.book, product_id=product, cause=verdict.cause, detail=verdict.detail)
        self.exits_started += 1
        self._tasks[product] = asyncio.create_task(self._exit(product, verdict.cause))
        return Verdict("exit_started", product, verdict.cause, verdict.detail)

    async def _exit(self, product_id: str, cause: WatchdogExit) -> None:
        try:
            pos = self._ledger.view(self.book, product_id)  # fresh: the position may have closed in the meantime
            if not pos.is_long:
                return
            if cause == "HORIZON":
                result = await self._gateway.submit_exit(self._horizon_intent(pos))
            else:
                result = await self._gateway.emergency_exit(product_id)
            done = log.info if cause == "HORIZON" else log.error
            done("watchdog_exit_done", product_id=product_id, cause=cause, result=repr(result))
        except Exception as exc:  # the position is still open: try again after the back-off
            self.exits_failed += 1
            log.error("watchdog_exit_failed", product_id=product_id, cause=cause, error=f"{type(exc).__name__}: {exc}")
        finally:
            self._next_attempt[product_id] = self._clock() + self._retry

    def _horizon_intent(self, pos: PositionView) -> ExitIntent:
        """The normal exit of the whole position, one decision id per attempt (a retry must not reuse the id of a
        cancelled order)."""
        now = self._clock()
        decision_id = f"horizon:{pos.entry_decision_id or pos.product_id}:{int(now.timestamp() * 1000)}"
        return ExitIntent(
            intent_id=decision_id,
            decision_id=decision_id,
            product_id=pos.product_id,
            book=self.book,
            base_size=pos.base_qty,
            cancel_bracket_order_id=pos.bracket_order_id,
            reason="HORIZON",
            created_at=now,
        )

    def _forget(self, product_id: str) -> None:
        self._breach_since.pop(product_id, None)
        self._blind_logged.pop(product_id, None)
        self._next_attempt.pop(product_id, None)
