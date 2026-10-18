"""Virtual account of a dry-run / backtest book (spec sez. 5.1.4, 5.3 punto 5; vincolo C11).

``VirtualAccount`` COMPOSES the same ``BalanceBook`` the live trading uses, with ``source="virtual"``. It has no
reservation logic of its own: orders are authorized and reserved by the ``BalanceGuard`` on ``account.balances``,
terminal orders are released with ``account.balances.release``, and fills go through ``record_fill``, which calls
``BalanceBook.apply_fill`` and only adds the bookkeeping the book needs for reports: fees paid and realized PnL.

PnL convention (what makes the accounting identity exact):

* ``realized_pnl`` and ``unrealized_pnl`` are GROSS of fees, against the average entry price of the product
  (``fees_paid`` is tracked apart; ``net_realized_pnl = realized_pnl - fees_paid``);
* ``quote_total + sum(base_total_i * mark_i) + fees_paid == starting_quote + realized_pnl + unrealized_pnl``.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Final

from modules.execution.balance import BalanceBook, Clock, CoverageAlarm

__all__ = ["VirtualAccount"]

ZERO: Final = Decimal(0)


class VirtualAccount:
    def __init__(self, book: str, starting_quote: Decimal, clock: Clock, quote_currency: str = "EUR") -> None:
        if starting_quote <= 0:
            raise ValueError("starting_quote must be positive")
        self.book = book
        self.quote_currency = quote_currency
        self.starting_quote = starting_quote
        self.balances = BalanceBook(book, "virtual", clock, {quote_currency: starting_quote})
        self.fees_paid = ZERO
        self.realized_pnl = ZERO
        # product -> [base quantity held through recorded fills, quote spent on it (fees excluded)]
        self._qty: dict[str, Decimal] = {}
        self._cost: dict[str, Decimal] = {}

    # ----------------------------------------------------------------------------------------- reads

    @property
    def net_realized_pnl(self) -> Decimal:
        return self.realized_pnl - self.fees_paid

    def position_qty(self, product_id: str) -> Decimal:
        return self._qty.get(product_id, ZERO)

    def avg_cost(self, product_id: str) -> Decimal | None:
        qty = self.position_qty(product_id)
        return None if qty <= 0 else self._cost[product_id] / qty

    def equity(self, mark_prices: Mapping[str, Decimal], base_of: Mapping[str, str]) -> Decimal:
        """Total quote plus ``base_total x mark`` of every product in ``base_of`` (product -> base currency).

        Reserved balances count (they are still ours until the order fills). A base balance without a mark price is
        an error: guessing a price would invent or hide money.
        """
        value = self.balances.total(self.quote_currency)
        for product, base in base_of.items():
            qty = self.balances.total(base)
            if qty == 0:
                continue
            if product not in mark_prices:
                raise KeyError(f"no mark price for {product}, which holds {qty} {base}")
            value += qty * mark_prices[product]
        return value

    def unrealized_pnl(self, mark_prices: Mapping[str, Decimal]) -> Decimal:
        total = ZERO
        for product, qty in self._qty.items():
            if qty > 0:
                if product not in mark_prices:
                    raise KeyError(f"no mark price for {product}, which holds {qty}")
                total += qty * mark_prices[product] - self._cost[product]
        return total

    # ----------------------------------------------------------------------------------------- fills

    def record_fill(
        self,
        client_order_id: str,
        *,
        product_id: str,
        base_currency: str,
        price: Decimal,
        size: Decimal,
        fee: Decimal,
        trade_id: str | None = None,
    ) -> list[CoverageAlarm]:
        """Apply one fill to the balances (``BalanceBook.apply_fill``) and update fees and realized PnL.

        The side comes from the order's reservation. A fill for an unknown order, or a repeated ``trade_id``, changes
        nothing here (the alarm, if any, is returned for the caller to trip ``coverage_breach``). Selling more than
        the recorded position raises ``ValueError``: the guard makes it impossible, so it is a bug.
        """
        reservation = self.balances.reservation(client_order_id)
        epoch = self.balances.epoch
        alarms = self.balances.apply_fill(
            client_order_id,
            base_currency=base_currency,
            quote_currency=self.quote_currency,
            price=price,
            size=size,
            fee=fee,
            trade_id=trade_id,
        )
        if reservation is None or self.balances.epoch == epoch:
            return alarms  # unknown order or duplicate fill: the balances did not move
        if reservation.side == "SELL" and size > self.position_qty(product_id):
            # The guard only lets a SELL reserve base we own: this is a bug, not a market event.
            raise ValueError(f"SELL fill of {size} {product_id} exceeds the {self.position_qty(product_id)} held")
        self.fees_paid += fee
        if reservation.side == "BUY":
            self._qty[product_id] = self.position_qty(product_id) + size
            self._cost[product_id] = self._cost.get(product_id, ZERO) + price * size
        else:
            held, cost = self._qty[product_id], self._cost[product_id]
            released = cost if size == held else cost * size / held
            self.realized_pnl += price * size - released
            self._qty[product_id] = held - size
            self._cost[product_id] = cost - released
        return alarms
