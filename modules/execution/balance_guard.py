"""The single authority that decides whether an order is fully covered by real spot balance (vincolo C11).

``BalanceGuard.authorize`` is synchronous: it checks the in-memory ``BalanceBook`` and reserves the amount in
one step, with no network call and no ``await``, so it adds no latency to order submission. It returns a
``CoverageTicket``, the only object from which an order body can be built. Nothing here can be switched off
from configuration, and it is the same code in live, dry and backtest.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final, Literal, Protocol

from modules.config import FallbackFees
from modules.execution.balance import BalanceBook, Reservation
from modules.execution.orders import OrderSpec
from modules.execution.spot import SpotRegistry, SpotViolation, validate_against_product, validate_structure
from modules.risk.costs import FeeRates, coverage_fee_rate, required_quote

# Fail-closed limit for BUYs on a live book: a balance view older than this is not trusted. A constant, not
# configuration. SELLs are never blocked by age: selling owned base cannot create debt.
BALANCE_MAX_AGE_S: Final = 120

CoverageReason = Literal["insufficient_balance", "balance_stale", "kill_switch"]

_TOKEN: Final = object()


class PositionLike(Protocol):
    @property
    def base_qty(self) -> Decimal: ...


class PositionCache(Protocol):
    """In-memory view of the ledger for one book (synchronous, updated with every ledger mutation)."""

    def view(self, product_id: str) -> PositionLike: ...


class KillSwitchView(Protocol):
    def coverage_breach_active(self, book: str) -> bool: ...


class FeeSource(Protocol):
    @property
    def current(self) -> FeeRates: ...


class CoverageRefused(Exception):
    """The order is not covered. Not fatal and never retried: the decision becomes a Skip."""

    reason: CoverageReason

    def __init__(self, reason: CoverageReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class InsufficientCoverage(CoverageRefused):
    def __init__(self, currency: str, required: Decimal, available: Decimal) -> None:
        super().__init__("insufficient_balance", f"need {required} {currency}, only {available} available")
        self.currency = currency
        self.required = required
        self.available = available


class BalanceStale(CoverageRefused):
    def __init__(self, age_s: float | None) -> None:
        super().__init__("balance_stale", f"balance snapshot age {age_s}s exceeds {BALANCE_MAX_AGE_S}s")
        self.age_s = age_s


class CoverageBreached(CoverageRefused):
    def __init__(self, book: str) -> None:
        super().__init__("kill_switch", f"coverage_breach active for book {book}")


class CoverageTicket:
    """Capability to send exactly one order. Only ``BalanceGuard`` can build it; it can be consumed once."""

    __slots__ = ("_consumed", "_spec", "currency", "reserved")

    def __init__(self, spec: OrderSpec, reserved: Decimal, currency: str, *, _token: object = None) -> None:
        if _token is not _TOKEN:
            raise SpotViolation("a CoverageTicket can only be issued by BalanceGuard.authorize")
        self._spec = spec
        self._consumed = False
        self.reserved = reserved
        self.currency = currency

    @property
    def spec(self) -> OrderSpec:
        return self._spec

    @property
    def consumed(self) -> bool:
        return self._consumed

    def consume(self) -> OrderSpec:
        if self._consumed:
            raise SpotViolation(f"ticket for {self._spec.client_order_id} already used")
        self._consumed = True
        return self._spec


class BalanceGuard:
    def __init__(
        self,
        balances: BalanceBook,
        positions: PositionCache,
        products: SpotRegistry,
        fees: FeeSource,
        fallback: FallbackFees,
        kill_switch: KillSwitchView,
    ) -> None:
        # all arguments are mandatory: there is no way to build a guard that skips a check
        self._balances = balances
        self._positions = positions
        self._products = products
        self._fees = fees
        self._fallback = fallback
        self._kill_switch = kill_switch

    def authorize(self, spec: OrderSpec) -> CoverageTicket:
        """Verify and reserve, or raise. Order of checks: spot shape -> increments -> BUY/SELL coverage."""
        meta = self._products.meta(spec.product_id)  # SpotViolation if not a verified spot product
        validate_structure(spec)
        validate_against_product(spec, meta)
        book = self._balances

        if spec.side == "BUY":
            assert spec.limit_price is not None  # guaranteed by validate_structure
            if self._kill_switch.coverage_breach_active(spec.book):
                raise CoverageBreached(spec.book)
            if book.source == "exchange":
                age = book.snapshot_age_s()
                if age is None or age > BALANCE_MAX_AGE_S:
                    raise BalanceStale(age)
            f_cov = coverage_fee_rate(self._fees.current, self._fallback)
            required = required_quote(spec.limit_price, spec.base_size, f_cov, meta.quote_increment)
            available = book.free(meta.quote_currency)
            if available < required:
                raise InsufficientCoverage(meta.quote_currency, required, available)
            currency = meta.quote_currency
        else:
            owned = self._positions.view(spec.product_id).base_qty
            sellable = min(owned - book.reserved_sell_base(spec.product_id), book.free(meta.base_currency))
            required = spec.base_size
            if sellable < required:
                raise InsufficientCoverage(meta.base_currency, required, max(sellable, Decimal(0)))
            currency = meta.base_currency

        book._reserve(
            Reservation(
                client_order_id=spec.client_order_id,
                product_id=spec.product_id,
                side=spec.side,
                currency=currency,
                amount=required,
            )
        )
        return CoverageTicket(spec, required, currency, _token=_TOKEN)
