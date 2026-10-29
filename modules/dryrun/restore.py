"""What a virtual book needs to continue after a restart (spec sez. 5.3, "Operativita' locale": the virtual
positions are restored from the dry DB at startup and resolved with the gap procedure).

A virtual book lives in memory (``VirtualAccount``, the simulated brackets of ``ShadowExecutionGateway``) and in the dry
ledger. The ledger is the truth: every fill ever booked, the positions, the orders. ``load_book_restore`` reads from it,
before the book is built, everything the rebuild needs:

* ``fills``: replayed by ``VirtualAccount.restore`` to get the balances, the fees paid and the realized PnL of the
  book exactly as they were (``free + reserved`` per currency is what the fills say, never a guess);
* ``bracket_orders``: the standalone brackets that are still open and belong to a LONG position of the book; their base
  (what the order was sent with, less what its fills already sold: in the tick fill mode a bracket can fill in parts)
  is a reservation of the rebuilt account, and the gateway re-registers them as simulated brackets;
* ``open_orders``: every other order still open (an entry waiting for bars, an exit, a bracket of a closed position):
  none of them can be resumed (the simulated queue and the latency are gone), the gateway cancels them in the ledger
  and the rebuilt account holds no reservation for them.

``starting_quote`` is fixed the first time a book is seen (``meta`` key ``starting_quote:<book>``): a later change of
the configured capital would otherwise make the replayed fills spend money the book never had.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import structlog

from modules.execution.balance import Reservation
from modules.execution.spot import SpotRegistry
from modules.ledger.positions import FillRecord, OrderRecord, PositionLedger

__all__ = ["BookRestore", "load_book_restore", "starting_quote_key"]

log = structlog.get_logger(__name__)


def starting_quote_key(book: str) -> str:
    return f"starting_quote:{book}"


@dataclass(frozen=True)
class BookRestore:
    book: str
    starting_quote: Decimal
    fills: tuple[FillRecord, ...]
    open_orders: tuple[OrderRecord, ...]
    bracket_orders: tuple[OrderRecord, ...]  # the subset of ``open_orders`` that stays alive
    reservations: tuple[Reservation, ...]  # base reserved by ``bracket_orders``

    @property
    def is_fresh(self) -> bool:
        """Nothing to restore: the book never traded and has no open order."""
        return not self.fills and not self.open_orders


def _bracket_size(rec: OrderRecord) -> Decimal | None:
    try:
        return Decimal(str(json.loads(rec.payload or "{}")["base_size"]))
    except (ValueError, KeyError, TypeError, InvalidOperation):
        return None


async def load_book_restore(
    ledger: PositionLedger, book: str, registry: SpotRegistry, configured_quote: Decimal
) -> BookRestore:
    """Read the state of ``book`` from the ledger (a fresh book gets an empty restore and its capital recorded)."""
    key = starting_quote_key(book)
    stored = await ledger.meta_get(key)
    starting = configured_quote if stored is None else Decimal(stored)
    if stored is None:
        await ledger.meta_set(key, str(configured_quote))
    elif starting != configured_quote:
        log.warning(
            "starting_quote_changed", book=book, configured=str(configured_quote), kept=str(starting),
            hint="the book keeps the capital it started with: its fills were paid from it",
        )  # fmt: skip
    positions = {p.product_id: p for p in ledger.long_positions(book)}
    open_orders = tuple(await ledger.open_orders(book))
    fills = tuple(await ledger.book_fills(book))
    sold: dict[str, Decimal] = {}  # base already sold by each order: a bracket of the tick fill mode fills in parts
    for f in fills:
        if f.side == "SELL":
            sold[f.client_order_id] = sold.get(f.client_order_id, Decimal(0)) + f.size
    brackets: list[OrderRecord] = []
    reservations: list[Reservation] = []
    for rec in open_orders:
        pos = positions.get(rec.product_id)
        if rec.leg != "bracket" or rec.side != "SELL" or pos is None or pos.bracket_order_id != rec.client_order_id:
            continue
        size = _bracket_size(rec)
        left = None if size is None else size - sold.get(rec.client_order_id, Decimal(0))
        if left is None or left <= 0 or left > pos.base_qty:
            log.error("restore_bracket_unreadable", book=book, client_order_id=rec.client_order_id)
            continue  # cancelled with the other open orders; the position gets a new bracket
        brackets.append(rec)
        reservations.append(
            Reservation(
                client_order_id=rec.client_order_id, product_id=rec.product_id, side="SELL",
                currency=registry.meta(rec.product_id).base_currency, amount=left,
            )  # fmt: skip
        )
    return BookRestore(book, starting, fills, open_orders, tuple(brackets), tuple(reservations))
