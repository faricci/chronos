"""What the planner needs to know at a slot, gathered from the live world: REST quote, ATR1h, balances, ledger, limits,
guards. Read-only: nothing here can place, change or cancel an order (``MarketReader`` declares reads only).

* ``AtrCache`` keeps the ATR(14) of the completed ONE_HOUR candles, refreshed every hour; an ATR older than two hours is
  no ATR (no value, no entry).
* ``BidSource`` gives the best bid for the protections and the equity feed: the WebSocket ticker while it is fresh, the
  REST quote otherwise.
* ``FactsGatherer.gather`` is the ``Gather`` of the scheduler: it fills a :class:`PlanFacts`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any, Final, Protocol

import structlog

from modules.config import FallbackFees
from modules.execution.balance import BalanceBook, Clock, utc_now
from modules.execution.exchange import BestBidAsk, ExchangeAuthError, ExchangeError, OrderInfo
from modules.execution.orders import ProductMeta
from modules.ledger.positions import PositionLedger
from modules.market.ws_feed import Quote
from modules.probe.budget import BudgetState, ProbeBudget, ProbeLimiter
from modules.probe.planner import ATR_PERIOD, HOUR_S, PlanFacts, atr_wilder
from modules.risk.costs import FeeRates
from modules.risk.risk_manager import MarketQuote

log = structlog.get_logger(__name__)

ATR_REFRESH_S: Final = 3600.0
ATR_MAX_AGE_S: Final = 2 * 3600.0
ATR_CANDLES: Final = ATR_PERIOD + 12  # a buffer over the 15 completed candles the ATR needs
BID_REST_CACHE_S: Final = 1.0


class MarketReader(Protocol):
    """Reads only. ``CoinbaseExchange`` satisfies it structurally."""

    async def get_best_bid_ask(self, product_id: str) -> BestBidAsk: ...
    async def get_candles(
        self, product_id: str, start_s: int, end_s: int, granularity: str = "ONE_MINUTE"
    ) -> list[dict[str, Any]]: ...
    async def list_open_orders(self) -> list[OrderInfo]: ...


# ------------------------------------------------------------------------------------------------- ATR


class AtrCache:
    def __init__(self, reader: MarketReader, product_id: str, clock: Clock = utc_now) -> None:
        self._reader = reader
        self._product = product_id
        self._clock = clock
        self._value: tuple[Decimal, Decimal] | None = None  # (ATR, last close) in price units
        self._fetched_at: datetime | None = None

    def value(self) -> tuple[Decimal, Decimal] | None:
        """``(ATR, last close)`` if it is younger than two hours, else ``None``."""
        if self._value is None or self._fetched_at is None:
            return None
        if (self._clock() - self._fetched_at).total_seconds() > ATR_MAX_AGE_S:
            return None
        return self._value

    def atr_1h(self, product_id: str) -> Decimal | None:
        """The ``AtrSource`` of the watchdog."""
        v = self.value()
        return None if v is None or product_id != self._product else v[0]

    def is_fresh(self) -> bool:
        return self._fetched_at is not None and (self._clock() - self._fetched_at).total_seconds() < ATR_REFRESH_S

    async def refresh(self) -> bool:
        now = self._clock()
        end = int(now.timestamp())
        try:
            candles = await self._reader.get_candles(
                self._product, end - ATR_CANDLES * HOUR_S, end, granularity="ONE_HOUR"
            )
        except ExchangeAuthError:
            raise
        except ExchangeError as exc:
            log.warning("probe_atr_refresh_failed", error=f"{type(exc).__name__}: {exc}")
            return False
        result = atr_wilder(candles, now)
        if result is None:
            log.warning("probe_atr_unavailable", candles=len(candles))
            return False
        self._value, self._fetched_at = result, now
        return True

    async def run(self, interval_s: float = ATR_REFRESH_S) -> None:
        while True:
            ok = await self.refresh()
            await asyncio.sleep(interval_s if ok else 60.0)


# ------------------------------------------------------------------------------------------------- bid


class BidSource:
    def __init__(
        self,
        reader: MarketReader,
        product_id: str,
        *,
        ws_quote: Callable[[], Quote | None] | None = None,
        ws_max_age_s: float = 10.0,
        clock: Clock = utc_now,
    ) -> None:
        self._reader = reader
        self._product = product_id
        self._ws = ws_quote
        self._ws_max_age_s = ws_max_age_s
        self._clock = clock
        self._rest: tuple[datetime, Decimal] | None = None

    async def bid(self) -> Decimal | None:
        now = self._clock()
        if self._ws is not None:
            quote = self._ws()
            if quote is not None and 0 <= (now - quote.ts).total_seconds() <= self._ws_max_age_s and quote.best_bid > 0:
                return Decimal(str(quote.best_bid))
        if self._rest is not None and (now - self._rest[0]).total_seconds() < BID_REST_CACHE_S:
            return self._rest[1]
        try:
            q = await self._reader.get_best_bid_ask(self._product)
        except ExchangeAuthError:
            raise
        except ExchangeError as exc:
            log.warning("probe_bid_unavailable", error=f"{type(exc).__name__}: {exc}")
            return None
        self._rest = (now, q.bid)
        return q.bid


# ---------------------------------------------------------------------------------------------- facts


class Guards(Protocol):
    def manual_stop(self) -> bool: ...
    def entries_blocked(self, now: datetime) -> str | None: ...


class FactsGatherer:
    def __init__(
        self,
        *,
        reader: MarketReader,
        product_id: str,
        meta: ProductMeta,
        ledger: PositionLedger,
        balances: BalanceBook,
        book: str,
        fees: Callable[[], FeeRates],
        fallback: FallbackFees,
        budget: ProbeBudget,
        limiter: ProbeLimiter,
        atr: AtrCache,
        guards: Guards,
        clock: Clock = utc_now,
    ) -> None:
        self._reader = reader
        self._product = product_id
        self._meta = meta
        self._ledger = ledger
        self._balances = balances
        self._book = book
        self._fees = fees
        self._fallback = fallback
        self._budget = budget
        self._limiter = limiter
        self._atr = atr
        self._guards = guards
        self._clock = clock

    async def gather(self, slot: datetime, now: datetime) -> PlanFacts:
        quote, quote_age = await self._quote()
        atr = self._atr.value()
        budget: BudgetState | None = None
        if quote is not None:
            budget = await self._budget.state(self._product, quote.best_bid)
        position = self._ledger.view(self._book, self._product)
        local_open = [o for o in await self._ledger.open_orders(self._book) if o.product_id == self._product]
        exchange_open = [o for o in await self._reader.list_open_orders() if o.product_id == self._product]
        # an order of ours open on the exchange is also in the ledger: count each once, by client_order_id
        ours = {o.client_order_id for o in local_open}
        open_orders = len(ours | {o.client_order_id for o in exchange_open if o.client_order_id})
        open_orders += sum(1 for o in exchange_open if not o.client_order_id)
        return PlanFacts(
            now=now,
            slot=slot,
            product_id=self._product,
            meta=self._meta,
            fees=self._fees(),
            fallback=self._fallback,
            manual_stop=self._guards.manual_stop(),
            entry_blocked=self._guards.entries_blocked(now),
            limit_blocked=await self._limiter.blocked(now),
            position_flat=not position.is_long,
            open_orders=open_orders,
            balance_age_s=self._balances.snapshot_age_s(),
            free_quote=self._balances.free(self._meta.quote_currency),
            quote=quote,
            quote_age_s=quote_age,
            atr1h=None if atr is None else atr[0],
            last_close=None if atr is None else atr[1],
            budget=budget,
        )

    async def _quote(self) -> tuple[MarketQuote | None, float | None]:
        asked_at = self._clock()
        try:
            q = await self._reader.get_best_bid_ask(self._product)
        except ExchangeAuthError:
            raise
        except ExchangeError as exc:
            log.warning("probe_quote_unavailable", error=f"{type(exc).__name__}: {exc}")
            return None, None
        # the answer is as old as the request that fetched it (a slow call makes it older, never younger)
        age = max((self._clock() - asked_at).total_seconds(), 0.0)
        return MarketQuote(best_bid=q.bid, best_ask=q.ask), age
