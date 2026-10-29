"""In-memory spendable balances with per-order reservations (spec sez. 4.1.6, vincolo C11).

One ``BalanceBook`` per book, same class for live (``source="exchange"``) and dry (``source="virtual"``).
All methods are synchronous and contain no ``await``: with a single event loop thread, a check followed by a
reserve is atomic without locks, and the order path never touches the network.

Currency accounting (all Decimal):

    free[c]          spendable now
    reservations[id] amount of one currency set aside for one non-terminal order
    reserved[c]      sum of the amounts reserved in c
    total[c]         free[c] + reserved[c]

A BUY reserves quote, a SELL reserves base. Reserve/release never change ``total``. A BUY fill consumes
``price * size + fee`` of the reservation and credits ``size`` base; a SELL fill consumes ``size`` of the
reservation and credits ``price * size - fee`` quote. A terminal order returns the unused remainder to ``free``.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from modules.execution.orders import Side

BalanceSource = Literal["exchange", "virtual"]
Clock = Callable[[], datetime]
AlarmKind = Literal["reservation_overdraw", "hold_exceeds_reservation", "balance_drift"]

ZERO = Decimal(0)
_SEEN_FILLS_MAX = 50_000  # remembered trade ids, for idempotent fill application


def utc_now() -> datetime:
    return datetime.now(UTC)


class Reservation(BaseModel):
    model_config = ConfigDict(frozen=True)

    client_order_id: str
    product_id: str
    side: Side
    currency: str
    amount: Decimal


class CoverageAlarm(BaseModel):
    """Evidence that the exchange contradicts the assumptions behind the reservations (sez. 4.1.4)."""

    model_config = ConfigDict(frozen=True)

    kind: AlarmKind
    currency: str
    detail: str


class AccountSnapshot(BaseModel):
    """``get_accounts`` + ``list_open_orders`` read together.

    ``balances`` maps currency -> (available, hold). ``foreign_order_currencies`` are the currencies touched by
    open orders that Chronos did not place; their holds must not be credited to our reservations.
    """

    model_config = ConfigDict(frozen=True)

    balances: dict[str, tuple[Decimal, Decimal]]
    foreign_order_currencies: frozenset[str] = frozenset()
    taken_at: datetime


class SnapshotOutcome(StrEnum):
    ACCEPTED = "accepted"
    DISCARDED_EPOCH = "discarded_epoch"


class BalanceBook:
    def __init__(
        self,
        book: str,
        source: BalanceSource,
        clock: Clock = utc_now,
        initial: Mapping[str, Decimal] | None = None,
    ) -> None:
        self.book = book
        self.source: BalanceSource = source
        self._clock = clock
        self._free: dict[str, Decimal] = {}
        for currency, amount in (initial or {}).items():
            if amount < 0:
                raise ValueError(f"negative initial balance for {currency}")
            self._free[currency] = amount
        self._reservations: dict[str, Reservation] = {}
        self._reserved: dict[str, Decimal] = {}
        self._seen_fills: set[str] = set()
        self._seen_order: deque[str] = deque()
        self.epoch = 0
        # A virtual book is the source of truth for itself: always fresh. An exchange book is stale until
        # the first snapshot arrives.
        self.snapshot_at: datetime | None = clock() if source == "virtual" else None

    # ------------------------------------------------------------------------------------------ reads

    def free(self, currency: str) -> Decimal:
        return self._free.get(currency, ZERO)

    def reserved(self, currency: str) -> Decimal:
        return self._reserved.get(currency, ZERO)

    def total(self, currency: str) -> Decimal:
        return self.free(currency) + self.reserved(currency)

    def reserved_sell_base(self, product_id: str) -> Decimal:
        return sum(
            (r.amount for r in self._reservations.values() if r.side == "SELL" and r.product_id == product_id), ZERO
        )

    def reservation(self, client_order_id: str) -> Reservation | None:
        return self._reservations.get(client_order_id)

    def open_reservations(self) -> list[Reservation]:
        return list(self._reservations.values())

    def snapshot_age_s(self) -> float | None:
        if self.snapshot_at is None:
            return None
        return (self._clock() - self.snapshot_at).total_seconds()

    # ------------------------------------------------------------------------------------- mutations

    def _set_reservation(self, r: Reservation) -> None:
        self._reservations[r.client_order_id] = r
        self._reserved[r.currency] = self.reserved(r.currency) + r.amount

    def _adjust_reservation(self, r: Reservation, new_amount: Decimal) -> None:
        self._reserved[r.currency] = self.reserved(r.currency) - r.amount + new_amount
        self._reservations[r.client_order_id] = r.model_copy(update={"amount": new_amount})

    def _reserve(self, r: Reservation) -> None:
        """Set ``r.amount`` aside. Called only by ``BalanceGuard.authorize``; never call it from anywhere else."""
        if r.amount <= 0:
            raise ValueError("reservation amount must be positive")
        if r.client_order_id in self._reservations:
            raise ValueError(f"order {r.client_order_id} is already reserved")
        if self.free(r.currency) < r.amount:
            raise ValueError(f"cannot reserve {r.amount} {r.currency}: only {self.free(r.currency)} free")
        self._free[r.currency] = self.free(r.currency) - r.amount
        self._set_reservation(r)
        self.epoch += 1

    def restore_reservation(self, r: Reservation) -> None:
        """Register the reservation of an order found open at startup. ``free`` is not touched: it is set by the
        ``apply_snapshot`` that must follow, from the exchange's available balance."""
        if r.client_order_id in self._reservations:
            raise ValueError(f"order {r.client_order_id} is already reserved")
        self._set_reservation(r)
        self.epoch += 1

    def release(self, client_order_id: str) -> Decimal:
        """Return the unused remainder of a reservation to ``free``. Idempotent: unknown id -> 0."""
        r = self._reservations.pop(client_order_id, None)
        if r is None:
            return ZERO
        self._reserved[r.currency] = self.reserved(r.currency) - r.amount
        self._free[r.currency] = self.free(r.currency) + r.amount
        self.epoch += 1
        return r.amount

    def _remember_fill(self, trade_id: str) -> bool:
        """True if this trade id is new. Bounded memory."""
        if trade_id in self._seen_fills:
            return False
        self._seen_fills.add(trade_id)
        self._seen_order.append(trade_id)
        if len(self._seen_order) > _SEEN_FILLS_MAX:
            self._seen_fills.discard(self._seen_order.popleft())
        return True

    def apply_fill(
        self,
        client_order_id: str,
        *,
        base_currency: str,
        quote_currency: str,
        price: Decimal,
        size: Decimal,
        fee: Decimal,
        trade_id: str | None = None,
    ) -> list[CoverageAlarm]:
        """Apply one fill. ``trade_id`` makes it idempotent (the same fill arrives from the WS and from polling).

        Returns alarms when the fill exceeds what was reserved; the excess is taken from ``free`` (clamped at 0),
        so balances can never go negative, and the caller must trip ``coverage_breach``.
        """
        if price < 0 or size <= 0 or fee < 0:
            raise ValueError("fill needs price >= 0, size > 0, fee >= 0")
        if trade_id is not None and not self._remember_fill(trade_id):
            return []
        alarms: list[CoverageAlarm] = []
        r = self._reservations.get(client_order_id)
        if r is None:
            alarms.append(
                CoverageAlarm(
                    kind="reservation_overdraw",
                    currency=quote_currency,
                    detail=f"fill for {client_order_id} without a reservation",
                )
            )
            # Unknown order: its side is unknown, so applying it could invent money. Leave the balances alone
            # (the next snapshot corrects them) and let the caller trip coverage_breach.
            self.epoch += 1
            return alarms

        if r.side == "BUY":
            consumed = price * size + fee
            remaining = r.amount - consumed
            if remaining < 0:
                over = -remaining
                alarms.append(
                    CoverageAlarm(
                        kind="reservation_overdraw",
                        currency=quote_currency,
                        detail=f"BUY fill consumed {consumed} > reserved {r.amount}",
                    )
                )
                self._adjust_reservation(r, ZERO)
                self._free[quote_currency] = max(self.free(quote_currency) - over, ZERO)
            else:
                self._adjust_reservation(r, remaining)
            self._free[base_currency] = self.free(base_currency) + size
        else:
            remaining = r.amount - size
            if remaining < 0:
                alarms.append(
                    CoverageAlarm(
                        kind="reservation_overdraw",
                        currency=base_currency,
                        detail=f"SELL fill of {size} > reserved {r.amount}",
                    )
                )
                self._adjust_reservation(r, ZERO)
                self._free[base_currency] = max(self.free(base_currency) + remaining, ZERO)
            else:
                self._adjust_reservation(r, remaining)
            proceeds = price * size - fee
            self._free[quote_currency] = max(self.free(quote_currency) + proceeds, ZERO)
        self.epoch += 1
        return alarms

    # --------------------------------------------------------------------------------------- snapshot

    def apply_snapshot(
        self, snap: AccountSnapshot, epoch_at_request: int, increments: Mapping[str, Decimal]
    ) -> tuple[SnapshotOutcome, list[CoverageAlarm]]:
        """Replace ``free`` with the exchange view, conservatively.

        ``free[c] = max(A - (reserved[c] - H_credit), 0)`` with ``A`` the available balance and
        ``H_credit = min(H, reserved[c])`` only if no foreign open order touches ``c`` (else 0). The formula
        can only under-estimate ``free``. A snapshot read while local events happened is discarded.
        """
        if self.source != "exchange":
            raise ValueError("a virtual book has no exchange snapshot")
        if self.epoch != epoch_at_request:
            return SnapshotOutcome.DISCARDED_EPOCH, []
        first = self.snapshot_at is None
        alarms: list[CoverageAlarm] = []
        for currency in sorted(set(snap.balances) | set(self._reserved) | set(self._free)):
            available, hold = snap.balances.get(currency, (ZERO, ZERO))
            reserved = self.reserved(currency)
            inc = increments.get(currency, ZERO)
            foreign = currency in snap.foreign_order_currencies
            credit = ZERO if foreign else min(hold, reserved)
            if not foreign and hold > reserved + inc:
                alarms.append(
                    CoverageAlarm(
                        kind="hold_exceeds_reservation",
                        currency=currency,
                        detail=f"hold {hold} > reserved {reserved} with no foreign orders",
                    )
                )
            exchange_total = available + hold
            local_total = self.total(currency)
            if not first and abs(exchange_total - local_total) > max(2 * inc, exchange_total / 10_000):
                alarms.append(
                    CoverageAlarm(
                        kind="balance_drift",
                        currency=currency,
                        detail=f"local total {local_total} vs exchange {exchange_total}",
                    )
                )
            self._free[currency] = max(available - (reserved - credit), ZERO)
        self.snapshot_at = snap.taken_at
        self.epoch += 1
        return SnapshotOutcome.ACCEPTED, alarms
