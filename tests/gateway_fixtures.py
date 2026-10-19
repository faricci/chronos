"""Fakes for the gateway tests: an exchange with order states driven by the (fake) clock, and ready-made environments.

``FakeExchange`` mimics the part of ``CoinbaseExchange`` the live gateway uses. It is as strict as the real one on the
points that matter for C11: ``create_order`` takes a genuine ticket and consumes it, and the body is built (and thereby
validated) by ``build_order_body``. Orders move only when the test says so (``plan_fills``, ``trigger``) or when the
fake clock passes the planned instants, so every scenario is deterministic and nothing really sleeps.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any

from modules.config import ExecutionConfig, FallbackFees
from modules.execution.balance import AccountSnapshot, BalanceBook
from modules.execution.balance_guard import BalanceGuard, CoverageTicket
from modules.execution.exchange import BestBidAsk, CancelResult, CreateOrderResult, FillInfo, OrderInfo
from modules.execution.gateway import LiveExecutionGateway
from modules.execution.orders import OrderSpec
from modules.execution.spot import SpotRegistry, build_order_body
from modules.ledger.positions import PositionLedger
from modules.risk.costs import FeeProvider
from tests.coverage_fixtures import SOL, Clock, KillSwitch

D = Decimal
INC = {"EUR": D("0.01"), "SOL": D("0.001")}
LIVE_ENV = {"CHRONOS_LIVE_CONFIRM": "I_ACCEPT_REAL_ORDERS"}
PRODUCT = "SOL-EUR"
BOOK = "champion"
FEE_RATE = D("0.0025")


@dataclass
class FakeOrder:
    spec: OrderSpec
    order_id: str
    created: Any
    status: str = "OPEN"
    fills: list[FillInfo] = field(default_factory=list)
    plan: list[tuple[float, Decimal, Decimal]] = field(default_factory=list)  # (seconds after creation, size, price)
    cancel_polls: int | None = None  # None: no cancel requested; else polls seen since the request

    @property
    def filled(self) -> Decimal:
        return sum((f.size for f in self.fills), D(0))


class FakeExchange:
    read_only = False

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self.log: list[tuple[Any, ...]] = []
        self.orders: dict[str, FakeOrder] = {}
        self.errors: list[BaseException | None] = []  # consumed one per create_order call; None = succeed
        self.fill_plans: dict[str, list[tuple[float, Decimal, Decimal]]] = {}  # by client_order_id
        self.auto_sell = True  # marketable SELL limits and market SELLs fill at once, in full
        self.cancel_lag = 0  # polls after a cancel request before CANCELLED shows
        self.stuck: set[str] = set()  # order ids whose cancel is never confirmed
        self.bid = D("92.03")
        self.ask = D("92.06")
        self.on_get_order: Callable[[], None] | None = None
        self.on_create: Callable[[], None] | None = None  # called when an order reaches the exchange
        self.create_then_fail = False  # an error from ``errors`` is raised AFTER the order exists (a lost answer)
        self._n = 0

    # ----------------------------------------------------------------------------------- scripting

    def plan_fills(self, cid: str, *steps: tuple[float, str, str]) -> None:
        self.fill_plans[cid] = [(t, D(size), D(price)) for t, size, price in steps]

    def creates(self, side: str | None = None) -> list[OrderSpec]:
        specs = [e[1] for e in self.log if e[0] == "create"]
        return [s for s in specs if side is None or s.side == side]

    def index(self, entry: str, /, **match: Any) -> int:
        """Position in the log of the first ``entry`` (``create`` entries match on spec attributes)."""
        for i, e in enumerate(self.log):
            if e[0] != entry:
                continue
            if all(getattr(e[1], k, None) == v for k, v in match.items()):
                return i
        raise AssertionError(f"no {entry} {match} in the log")

    def order_of(self, cid: str) -> FakeOrder:
        return next(o for o in self.orders.values() if o.spec.client_order_id == cid)

    def trigger(self, order_id: str, price: str | Decimal) -> None:
        """The exchange fills the whole order (a bracket hitting TP or SL)."""
        o = self.orders[order_id]
        self._fill(o, o.spec.base_size - o.filled, D(price))

    # --------------------------------------------------------------------------------- internals

    def _fill(self, o: FakeOrder, size: Decimal, price: Decimal) -> None:
        k = len(o.fills) + 1
        fee = price * size * FEE_RATE
        o.fills.append(FillInfo(
            trade_id=f"t-{o.order_id}-{k}", order_id=o.order_id, product_id=PRODUCT, side=o.spec.side, price=price,
            size=size, fee=fee, liquidity="MAKER", trade_time=self._clock(),
        ))  # fmt: skip
        if o.filled >= o.spec.base_size:
            o.status = "FILLED"

    def _advance(self, o: FakeOrder) -> None:
        if o.status == "OPEN":
            for delay, size, price in list(o.plan):
                if self._clock() >= o.created + timedelta(seconds=delay):
                    o.plan.remove((delay, size, price))
                    self._fill(o, min(size, o.spec.base_size - o.filled), price)
        if o.status == "OPEN" and o.cancel_polls is not None and o.order_id not in self.stuck:
            if o.cancel_polls >= self.cancel_lag:
                o.status = "CANCELLED"

    def _info(self, o: FakeOrder) -> OrderInfo:
        spec = o.spec
        avg = None if not o.fills else sum((f.price * f.size for f in o.fills), D(0)) / o.filled
        return OrderInfo(
            order_id=o.order_id, client_order_id=spec.client_order_id, product_id=spec.product_id, side=spec.side,
            status=o.status, base_size=spec.base_size, limit_price=spec.limit_price,
            stop_trigger_price=spec.stop_trigger_price, filled_size=o.filled, average_filled_price=avg,
            total_fees=sum((f.fee for f in o.fills), D(0)),
        )  # fmt: skip

    # ------------------------------------------------------------------------- the exchange API

    async def create_order(self, ticket: CoverageTicket) -> CreateOrderResult:
        assert type(ticket) is CoverageTicket, "the gateway must hand over a genuine ticket"
        spec = ticket.consume()
        build_order_body(spec)  # the same validation the real exchange applies
        self.log.append(("create", spec))
        if self.on_create is not None:
            self.on_create()
        lost_answer: BaseException | None = None
        if self.errors:
            error = self.errors.pop(0)
            if error is not None and self.create_then_fail:
                lost_answer = error
            elif error is not None:
                raise error
        self._n += 1
        order = FakeOrder(
            spec, f"ex-{self._n}", self._clock(), plan=list(self.fill_plans.get(spec.client_order_id, []))
        )
        self.orders[order.order_id] = order
        if self.auto_sell and spec.side == "SELL" and spec.kind in ("limit", "market"):
            self._fill(order, spec.base_size, spec.limit_price or self.bid)
        if lost_answer is not None:
            raise lost_answer
        return CreateOrderResult(
            order_id=order.order_id, client_order_id=spec.client_order_id, product_id=spec.product_id, side=spec.side
        )

    async def cancel_orders(self, order_ids: list[str]) -> list[CancelResult]:
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

    async def get_order(self, order_id: str) -> OrderInfo:
        o = self.orders[order_id]
        self._advance(o)
        info = self._info(o)
        if o.cancel_polls is not None:
            o.cancel_polls += 1
        self.log.append(("get_order", order_id, info.status))
        if self.on_get_order is not None:
            self.on_get_order()
        return info

    async def get_fills(self, order_id: str) -> list[FillInfo]:
        o = self.orders[order_id]
        self._advance(o)
        self.log.append(("get_fills", order_id))
        return list(o.fills)

    async def get_best_bid_ask(self, product_id: str) -> BestBidAsk:
        self.log.append(("best_bid_ask", product_id))
        return BestBidAsk(product_id=product_id, bid=self.bid, ask=self.ask)


class FeeSourceStub:
    def __init__(self, clock: Clock) -> None:
        self._provider = FeeProvider(FallbackFees(), clock=clock)

    @property
    def current(self) -> Any:
        return self._provider.current


@dataclass
class LiveEnv:
    gateway: LiveExecutionGateway
    venue: FakeExchange
    ledger: PositionLedger
    balances: BalanceBook
    guard: BalanceGuard
    clock: Clock
    kill: KillSwitch

    def refresh_snapshot(self) -> None:
        """A fresh balance snapshot consistent with the local book (valid when no order is open)."""
        b = self.balances
        assert not b.open_reservations(), "refresh_snapshot is only meaningful with no open reservation"
        snap = AccountSnapshot(
            balances={"EUR": (b.free("EUR"), D(0)), "SOL": (b.free("SOL"), D(0))}, taken_at=self.clock()
        )
        b.apply_snapshot(snap, b.epoch, INC)


async def make_live(
    *, eur: str = "100", sol: str = "0", execution: ExecutionConfig | None = None, **kwargs: Any
) -> LiveEnv:
    clock = Clock()

    async def sleep(seconds: float) -> None:
        clock.advance(seconds)
        await asyncio.sleep(0)  # let other tasks (a cancel, an emergency exit) run between polls

    ledger = await PositionLedger.open(":memory:", "live")
    balances = BalanceBook(BOOK, "exchange", clock)
    balances.apply_snapshot(
        AccountSnapshot(balances={"EUR": (D(eur), D(0)), "SOL": (D(sol), D(0))}, taken_at=clock()), 0, INC
    )
    kill = KillSwitch()
    registry = SpotRegistry({PRODUCT: SOL})
    fees = FeeSourceStub(clock)
    guard = BalanceGuard(balances, ledger.book_view(BOOK), registry, fees, FallbackFees(), kill)
    venue = FakeExchange(clock)
    try:
        gateway = LiveExecutionGateway(
            guard,
            venue,  # type: ignore[arg-type]
            ledger,
            registry,
            clock,
            book=BOOK,
            balances=balances,
            execution=execution or ExecutionConfig(mode="live"),
            env=kwargs.pop("env", LIVE_ENV),
            sleep=sleep,
            **kwargs,
        )
    except BaseException:
        await ledger.close()  # the aiosqlite thread would keep the interpreter alive
        raise
    return LiveEnv(gateway, venue, ledger, balances, guard, clock, kill)
