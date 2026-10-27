"""Plain data types shared by the execution layer (no logic, no dependency on the rest of ``execution``)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Side = Literal["BUY", "SELL"]
OrderKind = Literal["limit", "bracket", "market"]


class ProductMeta(BaseModel):
    """Spot product facts read from the exchange (never hardcoded, sez. C10)."""

    model_config = ConfigDict(frozen=True)

    product_id: str
    base_currency: str
    quote_currency: str
    base_increment: Decimal = Field(gt=0)
    quote_increment: Decimal = Field(gt=0)
    base_min_size: Decimal = Field(ge=0)
    quote_min_size: Decimal = Field(ge=0)

    def increment_of(self, currency: str) -> Decimal:
        if currency == self.base_currency:
            return self.base_increment
        if currency == self.quote_currency:
            return self.quote_increment
        raise KeyError(f"{currency} is neither base nor quote of {self.product_id}")


class OrderSpec(BaseModel):
    """What the caller wants to send. It becomes an order body only through a ``CoverageTicket``.

    kind=limit   -> ``limit_limit_gtc`` (BUY or SELL); a BUY with ``end_time`` is ``limit_limit_gtd`` (spec 2.15)
    kind=bracket -> ``trigger_bracket_gtc`` (SELL only, standalone): ``limit_price`` is the take profit
    kind=market  -> ``market_market_ioc`` (SELL only)
    """

    model_config = ConfigDict(frozen=True)

    client_order_id: str = Field(min_length=1)
    book: str
    product_id: str
    side: Side
    kind: OrderKind
    base_size: Decimal
    limit_price: Decimal | None = None
    stop_trigger_price: Decimal | None = None
    post_only: bool = False
    end_time: datetime | None = None  # UTC, timezone-aware: the exchange expires the order (BUY limit only)
