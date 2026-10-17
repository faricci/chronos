"""Shared fakes for the spot-coverage tests (C11)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from modules.config import FallbackFees
from modules.execution.balance import BalanceBook
from modules.execution.balance_guard import BalanceGuard
from modules.execution.orders import OrderSpec, ProductMeta
from modules.execution.spot import SpotRegistry
from modules.risk.costs import FeeProvider

D = Decimal
T0 = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)

SOL = ProductMeta(
    product_id="SOL-EUR",
    base_currency="SOL",
    quote_currency="EUR",
    base_increment=D("0.001"),
    quote_increment=D("0.01"),
    base_min_size=D("0.001"),
    quote_min_size=D("1"),
)

RAW_SOL: dict[str, Any] = {
    "product_id": "SOL-EUR",
    "product_type": "SPOT",
    "product_venue": "CBE",
    "base_currency_id": "SOL",
    "quote_currency_id": "EUR",
    "base_increment": "0.001",
    "quote_increment": "0.01",
    "base_min_size": "0.001",
    "quote_min_size": "1",
    "trading_disabled": False,
    "cancel_only": False,
    "view_only": False,
    "future_product_details": None,
}


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Positions:
    """Stand-in for the ledger's in-memory PositionCache."""

    class _View:
        def __init__(self, qty: Decimal) -> None:
            self.base_qty = qty

    def __init__(self, **qty: str) -> None:
        self.qty: dict[str, Decimal] = {k.replace("_", "-"): D(v) for k, v in qty.items()}

    def view(self, product_id: str) -> Any:
        return self._View(self.qty.get(product_id, D(0)))


class KillSwitch:
    def __init__(self) -> None:
        self.breached: set[str] = set()

    def coverage_breach_active(self, book: str) -> bool:
        return book in self.breached


def make_guard(
    quote: str = "100.00",
    base: str = "0",
    owned: str = "0",
    source: str = "virtual",
) -> tuple[BalanceGuard, BalanceBook, Positions, KillSwitch, Clock]:
    clock = Clock()
    initial = {"EUR": D(quote), "SOL": D(base)}
    book = BalanceBook("champion", source, clock, initial)  # type: ignore[arg-type]
    positions = Positions(SOL_EUR=owned)
    ks = KillSwitch()
    guard = BalanceGuard(
        book, positions, SpotRegistry({"SOL-EUR": SOL}), FeeProvider(FallbackFees(), clock=clock), FallbackFees(), ks
    )
    return guard, book, positions, ks, clock


def buy(cid: str = "b1", size: str = "0.024", price: str = "92.03", post_only: bool = True) -> OrderSpec:
    return OrderSpec(
        client_order_id=cid, book="champion", product_id="SOL-EUR", side="BUY", kind="limit",
        base_size=D(size), limit_price=D(price), post_only=post_only,
    )


def sell(cid: str = "s1", size: str = "0.024", kind: str = "market", price: str | None = None,
         stop: str | None = None) -> OrderSpec:
    return OrderSpec(
        client_order_id=cid, book="champion", product_id="SOL-EUR", side="SELL", kind=kind,  # type: ignore[arg-type]
        base_size=D(size), limit_price=D(price) if price else None, stop_trigger_price=D(stop) if stop else None,
    )
