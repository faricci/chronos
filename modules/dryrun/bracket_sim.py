"""Tick-level bracket simulator for the virtual positions of the dry run (spec sez. 5.1.2, 5.1.3, 5.3 punto 7).

It follows every open virtual position tick by tick until its bracket closes. It SELLS only, from a position that
exists, and never sends anything anywhere: its output is a list of ``BracketFill`` that the owner (the shadow book
manager) applies to the dry ledger and the ``VirtualAccount`` with the code it already has. No ledger, no balance and no
reservation logic lives here (constraint C11: nothing can be sold that is not owned, and a fill never exceeds what is
left of the position).

Rules, all from ``TickFillModel`` (the tick face of sez. 5.1.3) and the book of the product:

* take profit: a resting limit sell. A trade that prints STRICTLY above it fills at most ``queue_haircut`` x the size of
  that trade, at the TP price, maker fee; a partial fill carries over. It exists only from ``bracket_at`` (the bracket
  is placed after the entry has ended); before that only the virtual stop acts, as in the live watchdog;
* stop: triggers when a trade prints at or below it (``last <= stop``). From then on the take profit is cancelled
  and the position is liquidated as a market sell: ``walk`` of the bids of the book, minus ``stop_extra_slip_bps``,
  taker fee. A book that is stale or has no bids fills nothing: the stop stays triggered and the next book event
  retries; a book thinner than the position fills a part and the rest follows on the next ones;
* the same event satisfying both: the stop. One ``on_trades`` batch (one message of the feed) is one event.

Data gap (sez. 5.1.3 last row, 5.3 punto 9). ``on_damage(start, end)`` is the callback of ``CoinbaseMarketStream``:
while a gap that touches a position is unresolved, the trades of that product are held, not evaluated (they would be
judged against a hole). ``resolve_gaps`` (awaited by the owner after a reconnection and then every few seconds)
downloads the 1m candles of the gap through the ``CandleSource`` read interface, once they are final; the first candle
with ``low <= stop`` closes the position at ``BarFillModel.sl_exit`` (``min(stop, open)`` less spread, slippage and
the extra stop slip: the bar-level SL, at least as pessimistic as the rule of the table), taker fee,
``gap_resolved=True``. Then the held trades are replayed in order. The take profit is NEVER credited from a candle: a
TP crossed during the hole is not proven and not claimed.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final, Literal

import structlog

from modules.dryrun.fill_model import BarFillModel, TickFillModel
from modules.execution.balance import Clock
from modules.execution.balance_guard import FeeSource
from modules.execution.orders import ProductMeta
from modules.execution.rounding import floor_to
from modules.execution.spot import SpotRegistry
from modules.ledger.positions import PositionView
from modules.market.backfill import CandleSource, candles_to_frame
from modules.market.orderbook import LocalOrderBook, StaleBookError
from modules.market.ws_feed import Trade

__all__ = ["BracketFill", "BracketSimulator", "BracketStats"]

log = structlog.get_logger(__name__)

ZERO: Final = Decimal(0)
BPS: Final = Decimal(10_000)
MINUTE: Final = timedelta(minutes=1)
HELD_BATCHES_MAX: Final = 100_000


def _dec(x: float) -> Decimal:
    return Decimal(repr(float(x)))


def _floor_minute(t: datetime) -> datetime:
    return t.replace(second=0, microsecond=0)


@dataclass(frozen=True, slots=True)
class BracketFill:
    """One simulated exit fill of a virtual position. ``remaining`` is what is left of the position afterwards."""

    key: str
    book: str
    product_id: str
    reason: Literal["TP", "SL"]
    price: Decimal
    size: Decimal
    fee: Decimal
    liquidity: Literal["maker", "taker"]
    at: datetime
    remaining: Decimal
    gap_resolved: bool = False

    @property
    def closed(self) -> bool:
        return self.remaining == 0


@dataclass(slots=True)
class BracketStats:
    tp_fills: int = 0
    sl_fills: int = 0
    gaps_resolved: int = 0
    gap_fetch_failures: int = 0
    held_dropped: int = 0


@dataclass(slots=True)
class _Tracked:
    key: str
    book: str
    meta: ProductMeta
    tp: Decimal
    sl: Decimal
    opened_at: datetime
    remaining: Decimal
    bracket_at: datetime | None
    triggered_at: datetime | None = None


@dataclass(slots=True)
class _Gap:
    start: datetime
    end: datetime | None


@dataclass(slots=True)
class _Held:
    batches: deque[tuple[Trade, ...]] = field(default_factory=lambda: deque(maxlen=HELD_BATCHES_MAX))


class BracketSimulator:
    def __init__(
        self,
        registry: SpotRegistry,
        fees: FeeSource,
        clock: Clock,
        *,
        model: TickFillModel | None = None,
        candles: CandleSource | None = None,
        books: Mapping[str, LocalOrderBook] | None = None,
        gap_spread_bps: float = 3.0,
        settle_s: float = 120.0,
    ) -> None:
        self._registry = registry
        self._fees = fees
        self._clock = clock
        self._tick = model or TickFillModel()
        self._bar = BarFillModel(self._tick.params)
        self._candles = candles
        self._books: dict[str, LocalOrderBook] = dict(books or {})
        self._gap_spread = gap_spread_bps
        self._settle = timedelta(seconds=settle_s)
        self._tracked: dict[str, _Tracked] = {}
        self._gaps: list[_Gap] = []
        self._held: dict[str, _Held] = {}
        self.stats = BracketStats()

    # ------------------------------------------------------------------------------------------ positions

    @staticmethod
    def key_of(book: str, product_id: str) -> str:
        return f"{book}|{product_id}"

    def track(
        self,
        *,
        book: str,
        product_id: str,
        size: Decimal,
        tp: Decimal,
        sl: Decimal,
        opened_at: datetime,
        bracket_at: datetime | None = None,
    ) -> str:
        """Follow a virtual position. ``bracket_at``: when the bracket exists (None: not yet, only the stop acts)."""
        key = self.key_of(book, product_id)
        if key in self._tracked:
            raise ValueError(f"{key} is already tracked: one position per (book, product)")
        if size <= 0 or sl <= 0 or tp <= sl:
            raise ValueError(f"invalid bracket: size {size}, sl {sl}, tp {tp}")
        meta = self._registry.meta(product_id)
        self._tracked[key] = _Tracked(key, book, meta, tp, sl, opened_at, size, bracket_at)
        return key

    def track_view(self, view: PositionView, bracket_at: datetime | None) -> str:
        """Follow a position of the dry ledger (also the way a position survives a restart: see ``on_damage``)."""
        if not view.is_long or view.sl is None or view.tp is None or view.opened_at is None:
            raise ValueError(f"{view.book}|{view.product_id}: not a long position with a bracket")
        return self.track(
            book=view.book, product_id=view.product_id, size=view.base_qty, tp=view.tp, sl=view.sl,
            opened_at=view.opened_at, bracket_at=bracket_at,
        )  # fmt: skip

    def arm_bracket(self, key: str, at: datetime) -> None:
        self._tracked[key].bracket_at = at

    def untrack(self, key: str) -> None:
        """The position left by another path (model exit, watchdog): stop following it."""
        self._tracked.pop(key, None)

    def open_keys(self) -> list[str]:
        return list(self._tracked)

    def remaining(self, key: str) -> Decimal | None:
        t = self._tracked.get(key)
        return None if t is None else t.remaining

    def _of_product(self, product_id: str) -> list[_Tracked]:
        return [t for t in self._tracked.values() if t.meta.product_id == product_id]

    # --------------------------------------------------------------------------------------------- fees

    def _fee(self, price: Decimal, size: Decimal, liquidity: Literal["maker", "taker"]) -> Decimal:
        rates = self._fees.current
        bps = Decimal(str(rates.maker_bps if liquidity == "maker" else rates.taker_bps))
        return price * size * bps / BPS

    # --------------------------------------------------------------------------------------------- feed

    def on_trade(self, trade: Trade) -> list[BracketFill]:
        return self.on_trades((trade,))

    def on_trades(self, trades: Iterable[Trade]) -> list[BracketFill]:
        """One event of the feed. Trades of products with no tracked position cost nothing."""
        by_product: dict[str, list[Trade]] = {}
        for tr in trades:
            by_product.setdefault(tr.product_id, []).append(tr)
        out: list[BracketFill] = []
        for product, batch in by_product.items():
            if not self._of_product(product):
                continue
            if self._on_hold(product):
                held = self._held.setdefault(product, _Held())
                if len(held.batches) == held.batches.maxlen:
                    self.stats.held_dropped += 1
                    log.error("bracket_held_trades_dropped", product=product)
                held.batches.append(tuple(batch))
                continue
            out += self._apply_trades(product, tuple(batch))
        return out

    def on_book(self, book: LocalOrderBook) -> list[BracketFill]:
        """A book update: remember the book and let the stops that wait for liquidity retry."""
        self._books[book.product_id] = book
        out: list[BracketFill] = []
        for t in self._of_product(book.product_id):
            if t.triggered_at is not None:
                out += self._liquidate(t, self._clock())
        return out

    # ------------------------------------------------------------------------------------- the rules

    def _apply_trades(self, product: str, batch: tuple[Trade, ...]) -> list[BracketFill]:
        out: list[BracketFill] = []
        for t in self._of_product(product):
            if t.triggered_at is not None:
                out += self._liquidate(t, batch[-1].ts)  # a stop still waiting for liquidity: retry
                continue
            trigger = next((tr for tr in batch if self._tick.stop_triggered(_dec(tr.price), t.sl)), None)
            if trigger is not None:  # the stop wins over any take-profit print of the same event
                t.triggered_at = trigger.ts
                out += self._liquidate(t, trigger.ts)
                continue
            if t.bracket_at is None:
                continue
            for tr in batch:
                if tr.ts < t.bracket_at or not self._tick.tp_touched(_dec(tr.price), t.tp):
                    continue
                qty = self._tick.tp_fill_qty(_dec(tr.size), t.remaining, t.meta.base_increment)
                if qty > 0:
                    out.append(self._fill(t, "TP", t.tp, qty, "maker", tr.ts))
                if t.key not in self._tracked:
                    break
        return out

    def _liquidate(self, t: _Tracked, at: datetime) -> list[BracketFill]:
        """Sell what is left of a triggered position into the bids. Nothing is invented when the book is not usable."""
        book = self._books.get(t.meta.product_id)
        if book is None or book.stale:
            return []
        try:
            walk = book.walk("SELL", t.remaining)
        except StaleBookError:
            return []
        size = floor_to(walk.filled, t.meta.base_increment)
        if size <= 0 or walk.vwap is None:
            return []
        price = self._tick.stop_exit_price(walk.vwap, t.meta.quote_increment)
        return [self._fill(t, "SL", price, size, "taker", at)]

    def _fill(
        self, t: _Tracked, reason: Literal["TP", "SL"], price: Decimal, size: Decimal,
        liquidity: Literal["maker", "taker"], at: datetime, *, gap_resolved: bool = False,
    ) -> BracketFill:  # fmt: skip
        size = min(size, t.remaining)  # never sell more than is left
        t.remaining -= size
        fill = BracketFill(
            t.key, t.book, t.meta.product_id, reason, price, size, self._fee(price, size, liquidity), liquidity, at,
            t.remaining, gap_resolved,
        )  # fmt: skip
        if t.remaining <= 0:
            self._tracked.pop(t.key, None)
        if reason == "TP":
            self.stats.tp_fills += 1
        else:
            self.stats.sl_fills += 1
        return fill

    # ------------------------------------------------------------------------------------- data gaps

    def on_damage(self, start: datetime, end: datetime | None) -> None:
        """Callback of ``CoinbaseMarketStream``: trades in ``[start, end]`` (None: still going) may be missing."""
        for g in self._gaps:
            if g.start == start and (g.end is None or g.end == end):
                g.end = end
                return
        self._gaps.append(_Gap(start, end))

    def _affects(self, g: _Gap, t: _Tracked) -> bool:
        return g.end is None or g.end > t.opened_at

    def _on_hold(self, product: str) -> bool:
        return any(self._affects(g, t) for g in self._gaps for t in self._of_product(product) if t.triggered_at is None)

    def gap_pending(self) -> bool:
        return bool(self._gaps)

    async def resolve_gaps(self) -> list[BracketFill]:
        """Close the gaps whose candles are final; returns the fills they cause (stops, then the replayed trades)."""
        out: list[BracketFill] = []
        for gap in [g for g in self._gaps if g.end is not None]:
            assert gap.end is not None
            last_minute = _floor_minute(gap.end) + MINUTE  # the minute that contains the reconnection is needed too
            if self._clock() < last_minute + self._settle:
                continue  # its candle is not final yet
            affected = [t for t in self._tracked.values() if t.triggered_at is None and self._affects(gap, t)]
            frames: dict[str, list[tuple[datetime, float, float]]] = {}
            if affected:
                if self._candles is None:
                    log.error("bracket_gap_without_candle_source", start=gap.start.isoformat())
                    continue
                try:
                    for product in sorted({t.meta.product_id for t in affected}):
                        frames[product] = await self._fetch(product, _floor_minute(gap.start), last_minute)
                except Exception as exc:  # network, rate limit: the gap stays open, the trades stay held
                    self.stats.gap_fetch_failures += 1
                    log.warning("bracket_gap_fetch_failed", error=f"{type(exc).__name__}: {exc}")
                    continue
            for t in affected:
                out += self._stop_in_gap(t, gap, frames[t.meta.product_id])
            self._gaps.remove(gap)
            self.stats.gaps_resolved += 1
            out += self._release_held()
        return out

    async def _fetch(self, product: str, start: datetime, end: datetime) -> list[tuple[datetime, float, float]]:
        """Candles ``[start, end)`` as ``(minute, open, low)`` in order."""
        assert self._candles is not None
        s, e = int(start.timestamp()), int(end.timestamp())
        raw = await asyncio.to_thread(self._candles.fetch, product, s, e)
        frame = candles_to_frame(raw, s, e)
        return [
            (ts.to_pydatetime(), float(o), float(lo))
            for ts, o, lo in zip(frame["ts"], frame["open"], frame["low"], strict=True)
        ]

    def _stop_in_gap(
        self, t: _Tracked, gap: _Gap, candles: Sequence[tuple[datetime, float, float]]
    ) -> list[BracketFill]:
        first = _floor_minute(max(gap.start, t.opened_at))
        for minute, open_, low in candles:
            if minute < first or not self._tick.stop_triggered(_dec(low), t.sl):
                continue
            px = floor_to(_dec(self._bar.sl_exit(float(t.sl), open_, self._gap_spread)), t.meta.quote_increment)
            t.triggered_at = minute
            return [self._fill(t, "SL", px, t.remaining, "taker", minute, gap_resolved=True)]
        return []

    def _release_held(self) -> list[BracketFill]:
        """Replay, in order, the trades held for products that have no unresolved gap any more."""
        out: list[BracketFill] = []
        for product in [p for p in self._held if not self._on_hold(p)]:
            batches = self._held.pop(product).batches
            for batch in batches:
                out += self._apply_trades(product, batch)
        return out
