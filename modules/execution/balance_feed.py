"""Background refresh of the live ``BalanceBook`` (spec sez. 4.1.5 "Runtime saldi", 4.1.6; vincolo C11).

The order path never reads balances from the network: ``BalanceGuard.authorize`` looks at the in-memory book, and
this task keeps that book close to the exchange. Every ``execution.balance_refresh_s`` it reads the accounts and the
open orders (one asyncio task, REST polling: the WS ``user`` channel carries orders and fills, not spot balances) and
hands them to ``BalanceBook.apply_snapshot``.

* Snapshot rule: the book discards a snapshot if its ``epoch`` moved while the exchange was being read (a local
  reserve, fill or release raced with it). The discarded read is repeated after ``retry_s``.
* Fail closed: a failed read changes nothing, so the age of the book grows; after ``BALANCE_MAX_AGE_S`` (120 s,
  a constant of the guard, not configuration) the guard refuses every BUY. SELLs are never blocked by age.
* An open order whose ``client_order_id`` holds no reservation in the book is "foreign": the hold it puts on the
  account is not credited to our reservations (``H_credit = 0``), which can only lower ``free``.
* ``taken_at`` is the instant the read STARTED, so the age the guard sees is never under-estimated.

Alarms (``hold_exceeds_reservation``, ``balance_drift``) go to ``on_alarm``; the app routes them to the KillSwitch
(``coverage_breach``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from decimal import Decimal
from typing import Protocol

import structlog

from modules.config import ExecutionConfig
from modules.execution.balance import AccountSnapshot, BalanceBook, Clock, CoverageAlarm, SnapshotOutcome, utc_now
from modules.execution.balance_guard import BALANCE_MAX_AGE_S
from modules.execution.spot import SpotRegistry

log = structlog.get_logger(__name__)

# The configuration validator caps ``balance_refresh_s`` at a third of the staleness limit; the feed repeats the
# check so that a feed built by hand cannot be slower than the guard tolerates.
MIN_ATTEMPTS_BEFORE_STALE = 3
DEFAULT_RETRY_S = 1.0

AlarmHandler = Callable[[CoverageAlarm], None]
Sleep = Callable[[float], Awaitable[None]]


class OpenOrderView(Protocol):
    @property
    def client_order_id(self) -> str: ...
    @property
    def product_id(self) -> str: ...
    @property
    def side(self) -> str: ...


class AccountReader(Protocol):
    """What the feed needs from ``CoinbaseExchange``: two reads."""

    async def get_accounts(self) -> AccountSnapshot: ...
    async def list_open_orders(self) -> Sequence[OpenOrderView]: ...


class LiveBalanceFeed:
    def __init__(
        self,
        reader: AccountReader,
        book: BalanceBook,
        registry: SpotRegistry,
        *,
        refresh_s: float,
        retry_s: float = DEFAULT_RETRY_S,
        on_alarm: AlarmHandler | None = None,
        clock: Clock = utc_now,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if book.source != "exchange":
            raise ValueError("the balance feed serves an exchange book; a virtual book is its own source of truth")
        if refresh_s <= 0 or retry_s <= 0:
            raise ValueError("refresh_s and retry_s must be positive")
        if refresh_s * MIN_ATTEMPTS_BEFORE_STALE > BALANCE_MAX_AGE_S:
            raise ValueError(
                f"refresh_s={refresh_s} leaves fewer than {MIN_ATTEMPTS_BEFORE_STALE} attempts before the guard "
                f"refuses BUYs at {BALANCE_MAX_AGE_S} s"
            )
        self._reader = reader
        self._book = book
        self._registry = registry
        self._refresh_s = refresh_s
        self._retry_s = retry_s
        self._on_alarm = on_alarm
        self._clock = clock
        self._sleep = sleep
        self._increments = _increments(registry)
        self.consecutive_failures = 0
        self.discarded_snapshots = 0
        self.accepted_snapshots = 0
        self.last_error: str | None = None

    # ----------------------------------------------------------------------------------------- reads

    @property
    def max_age_s(self) -> int:
        """Age beyond which the guard refuses BUYs (a constant of ``balance_guard``)."""
        return BALANCE_MAX_AGE_S

    def age_s(self) -> float | None:
        """Seconds since the last accepted snapshot started, ``None`` before the first one."""
        return self._book.snapshot_age_s()

    def is_stale(self) -> bool:
        age = self.age_s()
        return age is None or age > BALANCE_MAX_AGE_S

    # --------------------------------------------------------------------------------------- refresh

    async def refresh_once(self) -> SnapshotOutcome | None:
        """One read and, if nothing local happened meanwhile, one snapshot. ``None``: the read failed."""
        epoch_at_request = self._book.epoch
        started = self._clock()
        try:
            read = await self._reader.get_accounts()
            open_orders = await self._reader.list_open_orders()
        except Exception as exc:  # network, auth, malformed answer: keep the book as it is and let it age
            self.consecutive_failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("balance_refresh_failed", error=self.last_error, failures=self.consecutive_failures)
            return None

        # No await from here to the end: the epoch check and the snapshot are one synchronous step.
        snapshot = AccountSnapshot(
            balances=read.balances,
            foreign_order_currencies=self._foreign_currencies(open_orders),
            taken_at=started,
        )
        outcome, alarms = self._book.apply_snapshot(snapshot, epoch_at_request, self._increments)
        if outcome is SnapshotOutcome.ACCEPTED:
            self.consecutive_failures = 0
            self.last_error = None
            self.accepted_snapshots += 1
        else:
            self.discarded_snapshots += 1
            log.info("balance_snapshot_discarded_epoch", epoch_at_request=epoch_at_request, epoch=self._book.epoch)
        for alarm in alarms:
            self._raise_alarm(alarm)
        return outcome

    async def run(self) -> None:
        """Refresh forever (a task of the app's ``TaskGroup``). Only cancellation ends it."""
        while True:
            outcome = await self.refresh_once()
            await self._sleep(self._delay_after(outcome))

    def _delay_after(self, outcome: SnapshotOutcome | None) -> float:
        if outcome is SnapshotOutcome.ACCEPTED:
            return self._refresh_s
        if outcome is SnapshotOutcome.DISCARDED_EPOCH:
            return self._retry_s
        return min(self._retry_s * (1 << min(max(self.consecutive_failures - 1, 0), 16)), self._refresh_s)

    # ------------------------------------------------------------------------------------------ helpers

    def _foreign_currencies(self, open_orders: Sequence[OpenOrderView]) -> frozenset[str]:
        """Currencies on hold because of open orders Chronos holds no reservation for."""
        foreign: set[str] = set()
        for order in open_orders:
            if self._book.reservation(order.client_order_id) is not None:
                continue
            base, quote = self._currencies_of(order.product_id)
            if order.side == "BUY":
                foreign.add(quote)
            elif order.side == "SELL":
                foreign.add(base)
            else:  # unreadable side: assume the worst for both
                foreign.update((base, quote))
        return frozenset(foreign)

    def _currencies_of(self, product_id: str) -> tuple[str, str]:
        if product_id in self._registry.product_ids:
            meta = self._registry.meta(product_id)
            return meta.base_currency, meta.quote_currency
        base, _, quote = product_id.partition("-")
        return base, quote or base

    def _raise_alarm(self, alarm: CoverageAlarm) -> None:
        log.error("coverage_alarm", kind=alarm.kind, currency=alarm.currency, detail=alarm.detail)
        if self._on_alarm is None:
            return
        try:
            self._on_alarm(alarm)
        except Exception as exc:  # a broken handler must not stop the refresh
            log.error("coverage_alarm_handler_failed", error=f"{type(exc).__name__}: {exc}")


def _increments(registry: SpotRegistry) -> Mapping[str, Decimal]:
    """Smallest increment per currency over the registry: the smaller, the more sensitive the drift alarms."""
    out: dict[str, Decimal] = {}
    for product_id in registry.product_ids:
        meta = registry.meta(product_id)
        for currency, inc in ((meta.base_currency, meta.base_increment), (meta.quote_currency, meta.quote_increment)):
            out[currency] = min(out.get(currency, inc), inc)
    return out


def feed_from_config(
    cfg: ExecutionConfig,
    reader: AccountReader,
    book: BalanceBook,
    registry: SpotRegistry,
    *,
    on_alarm: AlarmHandler | None = None,
) -> LiveBalanceFeed:
    """The production wiring: the refresh period comes from ``execution.balance_refresh_s``."""
    return LiveBalanceFeed(reader, book, registry, refresh_s=cfg.balance_refresh_s, on_alarm=on_alarm)
