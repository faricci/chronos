"""Shared fakes for the user stream, reconciler and watchdog tests: no network, no keys.

``FakeExchange`` is a READ-ONLY exchange: it answers the reads of ``ReconcileExchange`` / ``FillSource`` and fails the
test the instant anything else is touched (``create_order``, ``cancel_orders``, ``preview_order``...). The tests that
prove "this module cannot create orders" run their scenarios against it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from modules.config import FallbackFees
from modules.execution.balance import AccountSnapshot, BalanceBook, CoverageAlarm
from modules.execution.balance_guard import BalanceGuard
from modules.execution.exchange import BestBidAsk, FillInfo, OrderInfo
from modules.execution.orders import OrderSpec
from modules.execution.spot import SpotRegistry
from modules.execution.user_stream import EntryContext, OrderEventSink, PositionClosed
from modules.ledger.positions import EntryPlan, OrderRecord, PositionLedger
from modules.risk.costs import FeeProvider
from tests.coverage_fixtures import SOL, T0, Clock, KillSwitch

D = Decimal
B = "live"
P = "SOL-EUR"
PLAN = EntryPlan(tp=D("108.96"), sl=D("83.57"), k_tp=4.0, k_sl=2.0, atr=D("4.2334"))
READS = {"get_accounts", "list_open_orders", "list_orders", "get_best_bid_ask", "get_fills"}


def info(
    cid: str,
    *,
    side: str = "BUY",
    status: str = "OPEN",
    base: str = "0.024",
    filled: str = "0",
    limit: str | None = "92.03",
    stop: str | None = None,
    oid: str | None = None,
    product: str = P,
) -> OrderInfo:
    return OrderInfo(
        order_id=oid or f"ex-{cid}",
        client_order_id=cid,
        product_id=product,
        side=side,
        status=status,
        base_size=D(base),
        limit_price=None if limit is None else D(limit),
        stop_trigger_price=None if stop is None else D(stop),
        filled_size=D(filled),
    )


def fill(
    trade_id: str,
    cid: str,
    *,
    side: str = "BUY",
    price: str = "92.03",
    size: str = "0.024",
    fee: str = "0.10",
    at: datetime | None = None,
    oid: str | None = None,
) -> FillInfo:
    return FillInfo(
        trade_id=trade_id,
        order_id=oid or f"ex-{cid}",
        product_id=P,
        side=side,
        price=D(price),
        size=D(size),
        fee=D(fee),
        liquidity="MAKER",
        trade_time=at or T0,
    )


class FakeExchange:
    """Scripted reads. ``fail`` maps a read name to an exception raised by it."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.balances: dict[str, tuple[str, str]] = {"EUR": ("1000", "0"), "SOL": ("0", "0")}
        self.open: list[OrderInfo] = []
        self.history: list[OrderInfo] = []
        self.fills: dict[str, list[FillInfo]] = {}
        self.bid = D("92.00")
        self.fail: dict[str, Exception] = {}
        self.calls: list[str] = []
        self.history_args: list[dict[str, Any]] = []
        self.on_accounts: Callable[[], None] | None = None  # something that races with the read

    def _enter(self, name: str) -> None:
        self.calls.append(name)
        if name in self.fail:
            raise self.fail[name]

    async def get_accounts(self) -> AccountSnapshot:
        self._enter("get_accounts")
        if self.on_accounts is not None:
            self.on_accounts()
        balances = {c: (D(a), D(h)) for c, (a, h) in self.balances.items()}
        return AccountSnapshot(balances=balances, taken_at=self.clock())

    async def list_open_orders(self) -> list[OrderInfo]:
        self._enter("list_open_orders")
        return list(self.open)

    async def list_orders(self, **kwargs: Any) -> list[OrderInfo]:
        self._enter("list_orders")
        self.history_args.append(kwargs)
        return [*self.open, *self.history]

    async def get_best_bid_ask(self, product_id: str) -> BestBidAsk:
        self._enter("get_best_bid_ask")
        return BestBidAsk(product_id=product_id, bid=self.bid, ask=self.bid + D("0.02"))

    async def get_fills(self, order_id: str) -> list[FillInfo]:
        self._enter("get_fills")
        return list(self.fills.get(order_id, []))

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        raise AssertionError(f"the exchange was asked for {name!r}: this module may only read")


class Contexts:
    """EntryContextSource: every entry has decision ``dec1`` and the reference plan, unless told otherwise."""

    def __init__(self) -> None:
        self.by_cid: dict[str, EntryContext] = {}
        self.default: EntryContext | None = EntryContext("dec1", PLAN)

    def entry_context(self, client_order_id: str) -> EntryContext | None:
        return self.by_cid.get(client_order_id, self.default)


@dataclass
class World:
    clock: Clock
    ledger: PositionLedger
    book: BalanceBook
    registry: SpotRegistry
    guard: BalanceGuard
    exchange: FakeExchange
    contexts: Contexts
    sink: OrderEventSink
    alarms: list[CoverageAlarm] = field(default_factory=list)
    closed: list[PositionClosed] = field(default_factory=list)

    @property
    def kinds(self) -> list[str]:
        return [a.kind for a in self.alarms]

    def snapshot(self, **balances: tuple[str, str]) -> None:
        """Apply an exchange balance snapshot the way the feed does (fresh, no foreign orders)."""
        data = balances or {"EUR": ("1000", "0"), "SOL": ("0", "0")}
        snap = AccountSnapshot(balances={c: (D(a), D(h)) for c, (a, h) in data.items()}, taken_at=self.clock())
        self.book.apply_snapshot(snap, self.book.epoch, {"EUR": D("0.01"), "SOL": D("0.001")})

    async def send(
        self, spec: OrderSpec, *, leg: str, status: str = "NEW", exchange_order_id: str | None = None
    ) -> None:
        """What the gateway does: authorize (reserve), consume the ticket, record the order."""
        self.guard.authorize(spec).consume()
        await self.record(spec, leg=leg, status=status, exchange_order_id=exchange_order_id)

    async def record(
        self, spec: OrderSpec, *, leg: str, status: str = "NEW", exchange_order_id: str | None = None
    ) -> None:
        now = self.clock()
        await self.ledger.record_order(
            OrderRecord(
                spec.client_order_id, B, spec.product_id, spec.side, leg, status, now, now,
                exchange_order_id=exchange_order_id, intent_id="int1", payload=spec.model_dump_json(),
            )  # fmt: skip
        )

    async def open_position(self, qty: str = "0.024", price: str = "92.03", *, plan: EntryPlan | None = PLAN) -> None:
        await self.ledger.apply_entry_fill(B, P, D(qty), D(price), "dec1", self.clock(), plan)


def buy_spec(cid: str = "b1", size: str = "0.024", price: str = "92.03") -> OrderSpec:
    return OrderSpec(
        client_order_id=cid, book=B, product_id=P, side="BUY", kind="limit", base_size=D(size),
        limit_price=D(price), post_only=True,
    )  # fmt: skip


def bracket_spec(cid: str = "br1", size: str = "0.024", tp: str = "108.96", sl: str = "83.57") -> OrderSpec:
    return OrderSpec(
        client_order_id=cid, book=B, product_id=P, side="SELL", kind="bracket", base_size=D(size),
        limit_price=D(tp), stop_trigger_price=D(sl),
    )  # fmt: skip


def new_world_parts(ledger: PositionLedger) -> World:
    clock = Clock()
    book = BalanceBook(B, "exchange", clock)
    registry = SpotRegistry({P: SOL})
    fallback = FallbackFees()
    fees = FeeProvider(fallback, clock=clock)
    guard = BalanceGuard(book, ledger.book_view(B), registry, fees, fallback, KillSwitch())
    exchange = FakeExchange(clock)
    contexts = Contexts()
    world = World(clock, ledger, book, registry, guard, exchange, contexts, sink=None)  # type: ignore[arg-type]
    world.sink = OrderEventSink(
        book=B, ledger=ledger, balances=book, registry=registry, fills=exchange, contexts=contexts,
        on_alarm=world.alarms.append, on_closed=_collect(world), clock=clock,
    )  # fmt: skip
    return world


def _collect(world: World) -> Any:
    async def on_closed(event: PositionClosed) -> None:
        world.closed.append(event)

    return on_closed


def later(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)
