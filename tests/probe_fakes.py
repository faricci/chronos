"""A fake exchange for the probe tests: BTC-EUR, a clock the tests drive, and NO network, key or SDK.

``Venue`` implements what the probe uses from ``CoinbaseExchange`` (reads, ``create_order``, ``cancel_orders``,
``preview_order``). It keeps real balances and holds: a BUY holds its notional plus fee in EUR, a SELL holds its
base, so
the balance feed, the reconciler and the guard see a consistent account. Orders move only when a test says so (planned
fills, ``set_market``) or when the fake clock passes a GTD ``end_time``. A write on a read-only venue raises
``DryRunViolation`` exactly like the real wrapper.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from modules.execution.balance import AccountSnapshot
from modules.execution.balance_guard import CoverageTicket
from modules.execution.exchange import (
    BestBidAsk,
    CancelResult,
    CreateOrderResult,
    DryRunViolation,
    FillInfo,
    KeyPermissionsInfo,
    OrderInfo,
    PreviewResult,
)
from modules.execution.orders import OrderSpec
from modules.execution.spot import build_order_body
from modules.risk.costs import FeeRates

D = Decimal
P = "BTC-EUR"
T0 = datetime(2026, 10, 28, 8, 59, tzinfo=UTC)  # a Wednesday, a minute before the first slot

RAW_BTC: dict[str, Any] = {
    "product_id": P, "product_type": "SPOT", "product_venue": "CBE", "base_currency_id": "BTC",
    "quote_currency_id": "EUR", "base_increment": "0.00000001", "quote_increment": "0.01",
    "base_min_size": "0.00000001", "quote_min_size": "1", "trading_disabled": False, "cancel_only": False,
    "view_only": False, "future_product_details": None,
}  # fmt: skip


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)
        await asyncio.sleep(0.001)  # a real pause, so that tasks that sleep on this clock take turns


@dataclass
class VOrder:
    spec: OrderSpec
    order_id: str
    created: datetime
    status: str = "OPEN"
    fills: list[FillInfo] = field(default_factory=list)
    plan: list[tuple[float, Decimal, Decimal]] = field(default_factory=list)
    cancel_polls: int | None = None
    triggered: bool = False

    @property
    def filled(self) -> Decimal:
        return sum((f.size for f in self.fills), D(0))


class Venue:
    def __init__(self, clock: Clock, *, eur: str = "5.00", btc: str = "0", read_only: bool = False) -> None:
        self.clock = clock
        self.read_only = read_only
        self.eur_total, self.btc_total = D(eur), D(btc)
        self.bid, self.ask = D("80000.00"), D("80001.00")
        self.maker_bps, self.taker_bps = 25.0, 50.0
        self.orders: dict[str, VOrder] = {}
        self.log: list[tuple[Any, ...]] = []
        self.errors: list[BaseException | None] = []  # consumed one per create_order; None = succeed
        self.fill_plans: dict[str, list[tuple[float, Decimal, Decimal]]] = {}
        self.cancel_lag = 0
        self.stuck: set[str] = set()  # order ids whose cancel is never confirmed
        self.no_expire: set[str] = set()
        self.stop_stuck = False  # a triggered stop that the exchange does not execute
        self.auto_sell = True  # a marketable SELL limit and a market SELL fill at once
        self.perms = KeyPermissionsInfo(True, True, False, "DEFAULT")
        self.raw_product = dict(RAW_BTC)
        self.preview: dict[str, PreviewResult | BaseException] = {}  # by config name: gtd | gtc | bracket
        self.extra_balances: dict[str, tuple[Decimal, Decimal]] = {}
        self.listeners: list[Callable[[VOrder], None]] = []
        self.candles = self.make_candles(30)
        self.fail_reads: dict[str, BaseException] = {}
        self.fills_lag_polls = 0
        self._n = 0

    # ------------------------------------------------------------------------------------- scripting

    def make_candles(self, n: int, *, true_range: str = "100", close: str = "80000") -> list[dict[str, str]]:
        end = int(self.clock().timestamp()) // 3600 * 3600
        b, tr = D(close), D(true_range)
        return [
            {
                "start": str(end - (n - i) * 3600),
                "open": close,
                "close": close,
                "volume": "10",
                "high": str(b + tr / 2),
                "low": str(b - tr / 2),
            }  # fmt: skip
            for i in range(n)
        ]

    def plan_fills(self, cid: str, *steps: tuple[float, str, str]) -> None:
        self.fill_plans[cid] = [(t, D(size), D(price)) for t, size, price in steps]

    def creates(self, side: str | None = None) -> list[OrderSpec]:
        specs = [e[1] for e in self.log if e[0] == "create"]
        return [s for s in specs if side is None or s.side == side]

    def order_of(self, cid: str) -> VOrder:
        return next(o for o in self.orders.values() if o.spec.client_order_id == cid)

    def set_market(self, bid: str | Decimal, ask: str | Decimal | None = None) -> None:
        """Move the book: a bracket whose take profit is at or under the bid fills, one whose stop is at or over it
        triggers."""
        self.bid = D(bid)
        self.ask = D(ask) if ask is not None else self.bid + D("1.00")
        for o in list(self.orders.values()):
            if o.status != "OPEN" or o.spec.kind != "bracket" or o.spec.side != "SELL":
                continue
            assert o.spec.limit_price is not None and o.spec.stop_trigger_price is not None
            if self.bid >= o.spec.limit_price:
                self._fill(o, o.spec.base_size - o.filled, o.spec.limit_price, "MAKER")
            elif self.bid <= o.spec.stop_trigger_price:
                o.triggered = True
                if not self.stop_stuck:
                    self._fill(o, o.spec.base_size - o.filled, self.bid, "TAKER")

    def trigger(self, order_id: str, price: str | Decimal, liquidity: str = "TAKER") -> None:
        o = self.orders[order_id]
        self._fill(o, o.spec.base_size - o.filled, D(price), liquidity)

    # ---------------------------------------------------------------------------------- internals

    def _rate(self, liquidity: str) -> Decimal:
        return D(str(self.maker_bps if liquidity == "MAKER" else self.taker_bps)) / 10_000

    def _fill(self, o: VOrder, size: Decimal, price: Decimal, liquidity: str) -> None:
        if size <= 0:
            return
        fee = price * size * self._rate(liquidity)
        o.fills.append(
            FillInfo(
                trade_id=f"t-{o.order_id}-{len(o.fills) + 1}", order_id=o.order_id, product_id=P, side=o.spec.side,
                price=price, size=size, fee=fee, liquidity=liquidity, trade_time=self.clock(),
            )
        )  # fmt: skip
        if o.spec.side == "BUY":
            self.eur_total -= price * size + fee
            self.btc_total += size
        else:
            self.btc_total -= size
            self.eur_total += price * size - fee
        if o.filled >= o.spec.base_size:
            o.status = "FILLED"
        self._notify(o)

    def _notify(self, o: VOrder) -> None:
        for listener in self.listeners:
            listener(o)

    def _advance(self, o: VOrder) -> None:
        if o.status == "OPEN":
            for delay, size, price in list(o.plan):
                if self.clock() >= o.created + timedelta(seconds=delay):
                    o.plan.remove((delay, size, price))
                    self._fill(o, min(size, o.spec.base_size - o.filled), price, "MAKER")
        if (
            o.status == "OPEN" and o.spec.end_time is not None and self.clock() >= o.spec.end_time
            and o.order_id not in self.no_expire
        ):  # fmt: skip
            o.status = "EXPIRED"
            self._notify(o)
        if o.status == "OPEN" and o.cancel_polls is not None and o.order_id not in self.stuck:
            if o.cancel_polls >= self.cancel_lag:
                o.status = "CANCELLED"
                self._notify(o)

    def info(self, o: VOrder) -> OrderInfo:
        spec = o.spec
        avg = None if not o.fills else sum((f.price * f.size for f in o.fills), D(0)) / o.filled
        return OrderInfo(
            order_id=o.order_id, client_order_id=spec.client_order_id, product_id=spec.product_id, side=spec.side,
            status=o.status, base_size=spec.base_size, limit_price=spec.limit_price,
            stop_trigger_price=spec.stop_trigger_price, filled_size=o.filled, average_filled_price=avg,
            total_fees=sum((f.fee for f in o.fills), D(0)),
        )  # fmt: skip

    def holds(self) -> tuple[Decimal, Decimal]:
        eur = btc = D(0)
        for o in self.orders.values():
            if o.status != "OPEN":
                continue
            left = o.spec.base_size - o.filled
            if o.spec.side == "BUY":
                assert o.spec.limit_price is not None
                eur += left * o.spec.limit_price * (1 + self._rate("MAKER"))
            else:
                btc += left
        return eur, btc

    def message(self, o: VOrder) -> dict[str, Any]:
        """The order as the ``user`` channel reports it (fields of table B)."""
        spec = o.spec
        avg = D(0) if not o.fills else sum((f.price * f.size for f in o.fills), D(0)) / o.filled
        return {
            "order_id": o.order_id, "client_order_id": spec.client_order_id, "product_id": P, "order_side": spec.side,
            "status": o.status, "cumulative_quantity": str(o.filled), "leaves_quantity": str(spec.base_size - o.filled),
            "avg_price": str(avg), "limit_price": str(spec.limit_price or 0),
            "stop_price": str(spec.stop_trigger_price or 0),
            "total_fees": str(sum((f.fee for f in o.fills), D(0))), "number_of_fills": str(len(o.fills)),
        }  # fmt: skip

    def _guard_write(self, name: str) -> None:
        if self.read_only:
            raise DryRunViolation(f"{name} is a write operation and this exchange is read-only")

    def _read(self, name: str) -> None:
        self.log.append(("read", name))
        if name in self.fail_reads:
            raise self.fail_reads[name]

    # ------------------------------------------------------------------------------- the exchange

    async def get_product_raw(self, product_id: str) -> dict[str, Any]:
        self._read("get_product_raw")
        return dict(self.raw_product)

    async def get_fee_rates(self) -> FeeRates:
        self._read("get_fee_rates")
        return FeeRates(maker_bps=self.maker_bps, taker_bps=self.taker_bps, assumed=False, as_of=self.clock())

    async def get_best_bid_ask(self, product_id: str) -> BestBidAsk:
        self._read("get_best_bid_ask")
        return BestBidAsk(product_id=product_id, bid=self.bid, ask=self.ask)

    async def get_candles(
        self, product_id: str, start_s: int, end_s: int, granularity: str = "ONE_MINUTE"
    ) -> list[Any]:
        self._read("get_candles")
        return list(self.candles)

    async def get_api_key_permissions(self) -> KeyPermissionsInfo:
        self._read("get_api_key_permissions")
        return self.perms

    async def get_accounts(self) -> AccountSnapshot:
        self._read("get_accounts")
        eur_hold, btc_hold = self.holds()
        balances = {
            "EUR": (self.eur_total - eur_hold, eur_hold),
            "BTC": (self.btc_total - btc_hold, btc_hold),
            **self.extra_balances,
        }
        return AccountSnapshot(balances=balances, taken_at=self.clock())

    async def list_open_orders(self) -> list[OrderInfo]:
        self._read("list_open_orders")
        out = []
        for o in self.orders.values():
            self._advance(o)
            if o.status == "OPEN":
                out.append(self.info(o))
        return out

    async def list_orders(self, **kwargs: Any) -> list[OrderInfo]:
        self._read("list_orders")
        for o in self.orders.values():
            self._advance(o)
        wanted = kwargs.get("statuses")
        return [self.info(o) for o in self.orders.values() if not wanted or o.status in wanted]

    async def get_order(self, order_id: str) -> OrderInfo:
        o = self.orders[order_id]
        self._advance(o)
        info = self.info(o)
        if o.cancel_polls is not None:
            o.cancel_polls += 1
        self.log.append(("get_order", order_id, info.status))
        return info

    async def get_fills(self, order_id: str) -> list[FillInfo]:
        o = self.orders[order_id]
        self._advance(o)
        self.log.append(("get_fills", order_id))
        if self.fills_lag_polls > 0 and o.fills:
            self.fills_lag_polls -= 1
            return []
        return list(o.fills)

    async def create_order(self, ticket: CoverageTicket) -> CreateOrderResult:
        self._guard_write("create_order")
        assert type(ticket) is CoverageTicket, "the gateway must hand over a genuine ticket"
        spec = ticket.consume()
        build_order_body(spec)
        self.log.append(("create", spec))
        if self.errors:
            error = self.errors.pop(0)
            if error is not None:
                raise error
        self._n += 1
        order = VOrder(spec, f"ex-{self._n}", self.clock(), plan=list(self.fill_plans.get(spec.client_order_id, [])))
        self.orders[order.order_id] = order
        self._notify(order)
        if self.auto_sell and spec.side == "SELL" and spec.kind in ("limit", "market"):
            self._fill(order, spec.base_size, spec.limit_price or self.bid, "TAKER")
        return CreateOrderResult(
            order_id=order.order_id, client_order_id=spec.client_order_id, product_id=spec.product_id, side=spec.side
        )

    async def cancel_orders(self, order_ids: list[str]) -> list[CancelResult]:
        self._guard_write("cancel_orders")
        self.log.append(("cancel", tuple(order_ids)))
        results = []
        for oid in order_ids:
            o = self.orders[oid]
            self._advance(o)
            if o.status == "OPEN":
                o.cancel_polls = 0 if o.cancel_polls is None else o.cancel_polls
                results.append(CancelResult(order_id=oid, success=True))
            else:
                results.append(CancelResult(order_id=oid, success=False, failure_reason="UNKNOWN_CANCEL_ORDER"))
        return results

    async def preview_order(self, spec: OrderSpec) -> PreviewResult:
        self._guard_write("preview_order")
        build_order_body(spec)
        self.log.append(("preview", spec))
        kind = "bracket" if spec.kind == "bracket" else ("gtd" if spec.end_time is not None else "gtc")
        out = self.preview.get(kind)
        if isinstance(out, BaseException):
            raise out
        if out is not None:
            return out
        notional = (spec.limit_price or D(0)) * spec.base_size
        return PreviewResult(
            errors=(),
            warnings=(),
            order_total=notional,
            commission_total=(notional * self._rate("MAKER")).quantize(D("0.00000001")),
        )  # fmt: skip


class ScaledClock(Clock):
    """Virtual time that runs ``scale`` times faster than the wall clock, consistently for every task.

    ``sleep(s)`` really sleeps ``s / scale`` seconds, so loops with different intervals keep their proportions and a
    whole GTD lifetime takes a few seconds of test time. ``jump`` moves the virtual instant (to a slot, for
    instance)."""

    def __init__(self, now: datetime = T0, scale: float = 400.0) -> None:
        super().__init__(now)
        import time

        self._time = time.monotonic
        self._anchor = self._time()
        self._base = now
        self._offset = timedelta(0)
        self.scale = scale

    def __call__(self) -> datetime:
        elapsed = (self._time() - self._anchor) * self.scale
        return self._base + self._offset + timedelta(seconds=elapsed)

    @property  # type: ignore[override]
    def now(self) -> datetime:
        return self()

    @now.setter
    def now(self, value: datetime) -> None:  # the base class sets it once, in __init__
        return None

    def jump_to(self, when: datetime) -> None:
        self._offset += when - self()

    def advance(self, seconds: float) -> None:
        self._offset += timedelta(seconds=seconds)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(seconds / self.scale, 0.0005))
