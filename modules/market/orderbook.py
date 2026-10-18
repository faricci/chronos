"""Local level-2 order book per product (spec sez. 5.3 punto 3).

Fed by the ``level2`` channel of the public WebSocket: one ``snapshot`` and then ``update`` messages whose levels
carry the NEW quantity (``0`` removes the level). Prices and sizes are ``Decimal``: the book feeds the spread of
the cost model and the fill simulator of the dry run, which are exact-arithmetic code.

Integrity rules, all of which make the book ``stale`` until the next snapshot:
- before the first snapshot, and after ``mark_stale`` (sequence gap, reconnect);
- when an update leaves the book crossed (``best_bid >= best_ask``).
A stale book refuses to answer ``walk``: a fill simulated on a broken book would be invented.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final, Literal

from sortedcontainers import SortedDict

Side = Literal["BUY", "SELL"]
BookSide = Literal["bid", "offer"]
ZERO: Final = Decimal(0)


class StaleBookError(Exception):
    """The book cannot be trusted right now (no snapshot yet, gap, or crossed)."""


@dataclass(frozen=True, slots=True)
class LevelUpdate:
    side: BookSide
    price: Decimal
    quantity: Decimal  # new quantity at this price; 0 deletes the level


@dataclass(frozen=True, slots=True)
class WalkResult:
    """Outcome of consuming liquidity: what a marketable order of ``requested`` would get right now."""

    requested: Decimal
    filled: Decimal
    notional: Decimal
    vwap: Decimal | None
    levels_consumed: int

    @property
    def complete(self) -> bool:
        return self.filled == self.requested


class LocalOrderBook:
    def __init__(self, product_id: str) -> None:
        self.product_id = product_id
        # bids keyed by the NEGATED price so that plain ascending iteration is best (highest) bid first
        self._bids: SortedDict[Decimal, Decimal] = SortedDict()
        self._asks: SortedDict[Decimal, Decimal] = SortedDict()
        self._stale_reason: str | None = "no snapshot yet"
        self.updated_at: datetime | None = None

    # ------------------------------------------------------------------------------- state

    @property
    def stale(self) -> bool:
        return self._stale_reason is not None

    @property
    def stale_reason(self) -> str | None:
        return self._stale_reason

    def mark_stale(self, reason: str) -> None:
        """Sequence gap or reconnect: keep the levels (useful for diagnostics) but stop trusting them."""
        self._stale_reason = reason

    def apply_snapshot(self, levels: Iterable[LevelUpdate], ts: datetime | None = None) -> None:
        """Replace the whole book. A snapshot that is itself crossed leaves the book stale."""
        self._bids.clear()
        self._asks.clear()
        self._apply(levels)
        self.updated_at = ts
        self._stale_reason = None
        self._check_crossed()

    def apply_update(self, levels: Iterable[LevelUpdate], ts: datetime | None = None) -> bool:
        """Apply incremental changes. Returns False (and goes stale) if the book is crossed afterwards.

        An update arriving while the book is stale (before a snapshot) is ignored: it would apply to nothing.
        """
        if self._stale_reason == "no snapshot yet":
            return False
        self._apply(levels)
        self.updated_at = ts
        return self._check_crossed()

    def _apply(self, levels: Iterable[LevelUpdate]) -> None:
        for lv in levels:
            book, key = (self._bids, -lv.price) if lv.side == "bid" else (self._asks, lv.price)
            if lv.quantity > 0:
                book[key] = lv.quantity
            else:
                book.pop(key, None)

    def _check_crossed(self) -> bool:
        bid, ask = self.best_bid(), self.best_ask()
        if bid is not None and ask is not None and bid >= ask:
            self._stale_reason = f"crossed book: bid {bid} >= ask {ask}"
            return False
        return True

    # --------------------------------------------------------------------------------- reads

    def best_bid(self) -> Decimal | None:
        return -self._bids.peekitem(0)[0] if self._bids else None

    def best_ask(self) -> Decimal | None:
        return self._asks.peekitem(0)[0] if self._asks else None

    def best_bid_size(self) -> Decimal | None:
        return self._bids.peekitem(0)[1] if self._bids else None

    def best_ask_size(self) -> Decimal | None:
        return self._asks.peekitem(0)[1] if self._asks else None

    def mid(self) -> Decimal | None:
        bid, ask = self.best_bid(), self.best_ask()
        return None if bid is None or ask is None else (bid + ask) / 2

    def spread_bps(self) -> float | None:
        bid, ask = self.best_bid(), self.best_ask()
        if bid is None or ask is None:
            return None
        return float((ask - bid) / ((ask + bid) / 2) * 10_000)

    def depth(self, side: BookSide) -> int:
        return len(self._bids if side == "bid" else self._asks)

    def walk(self, side: Side, size: Decimal, limit: Decimal | None = None) -> WalkResult:
        """Consume liquidity as a marketable order would. BUY eats asks up to ``limit`` (a maximum price); SELL
        eats bids down to ``limit`` (a minimum price). ``limit=None`` means any price. Raises on a stale book."""
        if self._stale_reason is not None:
            raise StaleBookError(f"{self.product_id}: {self._stale_reason}")
        if size <= 0:
            raise ValueError("size must be positive")
        remaining, notional, levels = size, ZERO, 0
        if side == "BUY":
            ladder = ((price, qty) for price, qty in self._asks.items())
        else:
            ladder = ((-key, qty) for key, qty in self._bids.items())
        for price, qty in ladder:
            if limit is not None and ((side == "BUY" and price > limit) or (side == "SELL" and price < limit)):
                break
            take = min(remaining, qty)
            notional += take * price
            remaining -= take
            levels += 1
            if remaining == 0:
                break
        filled = size - remaining
        return WalkResult(size, filled, notional, notional / filled if filled > 0 else None, levels)
