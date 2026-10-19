"""Parallel dry-run books and their manager (spec sez. 0.6, 5.1.4, 5.3 punto 8; vincolo C11).

Proves: every book gets its own account, guard, gateway, RiskManager and engine, built from ITS model; the books run
only on the dry ledger and cannot reach an exchange; a bar is served to all the books and a failure stays in the
book that had it; ``random_entry`` follows the BUY frequency of its source and ``buy_and_hold`` buys once through
the guard; and the whole pipeline in dry never writes anywhere but the dry ledger (``test_dry_never_writes``).
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from modules.config import (
    BarsConfig,
    BookConfig,
    DecisionConfig,
    EntryTimeout,
    FallbackFees,
    KillSwitchConfig,
    RiskConfig,
)
from modules.decision.engine import DecisionEngine, ExecutionReport, Processed
from modules.decision.policy import REGIME_RULE, Decision, DecisionPolicy, PolicyConfig
from modules.decision.questions import build_questions
from modules.dryrun import books as books_module
from modules.dryrun.books import (
    BookConfigError,
    BookDeps,
    BookModel,
    BuyAndHoldDriver,
    BuyRateTracker,
    RandomDecider,
    ShadowBook,
    ShadowBookManager,
    build_books,
    build_buy_and_hold_book,
    build_model_book,
    build_random_book,
    build_twin_book,
)
from modules.dryrun.fill_model import FillModelParams
from modules.execution.gateway import ExitResult
from modules.ledger.positions import LedgerModeMismatch, PositionLedger
from modules.market.bars import BarClosed
from modules.market.timeframes import Bar
from modules.risk.kill_switch import AccountGuards, KillSwitch, SystemGuards
from modules.risk.risk_manager import MarketQuote
from tests.coverage_fixtures import Clock
from tests.label_fixtures import loaded_label
from tests.test_backtest import LABEL, POLICY, StubDecider, registry
from tests.test_decision_engine import QUESTIONS, WARM_A, Sink, StubBaseline, frame, quote_around, warmed

D = Decimal
BTC, SOL = "BTC-EUR", "SOL-EUR"
PRODUCTS = (BTC, SOL)
T0 = datetime(2026, 10, 19, 8, 0, tzinfo=UTC)


# -------------------------------------------------------------------------------------------- fakes


class Fees:
    current = LABEL.fees


class Quotes:
    def __init__(self) -> None:
        self.by_product: dict[str, MarketQuote | None] = {}

    def quote(self, product_id: str) -> MarketQuote | None:
        return self.by_product.get(product_id)

    def around(self, product_id: str, price: float, spread_bps: float = 3.0) -> None:
        self.by_product[product_id] = quote_around(price, spread_bps)


def kill_switch(clock: Clock, names: list[str]) -> KillSwitch:
    cfg = KillSwitchConfig()
    return KillSwitch(
        SystemGuards(cfg, clock), {n: AccountGuards(n, cfg, RiskConfig(), clock) for n in names}, clock
    )


@dataclass
class World:
    ledger: PositionLedger
    clock: Clock
    quotes: Quotes
    sink: Sink
    kill: KillSwitch
    deps: BookDeps
    reports: list[tuple[str, ExecutionReport]] = field(default_factory=list)


def make_world(ledger: PositionLedger, names: list[str], **overrides: Any) -> World:
    clock = Clock()
    clock.now = T0
    quotes, sink = Quotes(), Sink()
    kill = kill_switch(clock, names)
    reports: list[tuple[str, ExecutionReport]] = []
    values: dict[str, Any] = {
        "ledger": ledger, "clock": clock, "registry": registry(), "fees": Fees(), "kill_switch": kill, "quotes": quotes,
        "sink": sink, "risk": RiskConfig(), "fallback_fees": FallbackFees(), "entry_timeout": EntryTimeout(),
        "fill_params": FillModelParams(), "decision": DecisionConfig(), "bars": BarsConfig(), "products": PRODUCTS,
        "on_execution": lambda book, report: reports.append((book, report)),
    }  # fmt: skip
    values.update(overrides)
    world = World(ledger, clock, quotes, sink, kill, BookDeps(**values))
    world.reports = reports
    return world


@pytest.fixture
async def ledger() -> AsyncIterator[PositionLedger]:
    led = await PositionLedger.open(":memory:", "dry")
    yield led
    await led.close()


@pytest.fixture
async def live_ledger() -> AsyncIterator[PositionLedger]:
    led = await PositionLedger.open(":memory:", "live")
    yield led
    await led.close()


MODEL_A = BookModel("model-a", LABEL, POLICY, QUESTIONS, StubBaseline(StubDecider()))  # type: ignore[arg-type]
LABEL_B = loaded_label({BTC: 3.0, SOL: 3.0}, horizon_min=4 * 1440, k_tp=4.0, k_sl=2.0)
POLICY_B = PolicyConfig(
    model="stub-b", tau_buy=0.5, tau_sell=0.5, c_min=0.0, p_be_buffer=0.0, regime_rule=REGIME_RULE,
    label_config_sha256=LABEL_B.sha256,
)  # fmt: skip
MODEL_B = BookModel(
    "model-b", LABEL_B, POLICY_B, build_questions(LABEL_B.cfg.horizon_min, 4.0, 2.0, LABEL_B.cfg.atr_tf),
    StubBaseline(StubDecider()),  # type: ignore[arg-type]
)  # fmt: skip
MODELS = {"champion": MODEL_A, "challenger": MODEL_B}


def resolve(alias: str) -> BookModel:
    return MODELS[alias]


def cfg(name: str, decider: str, **kw: Any) -> BookConfig:
    return BookConfig(name=name, decider=decider, **kw)


CONFIGS = [
    cfg("champion", "registry:champion"),
    cfg("challenger", "registry:challenger"),
    cfg("random_entry", "random", match_frequency_of="champion", seed=42),
    cfg("buy_and_hold", "buy_and_hold"),
]
NAMES = [c.name for c in CONFIGS]


def event(product: str, minute: int, price: float = 100.0, *, lag: float = 120.0, spread: float = 0.0) -> BarClosed:
    ts = T0 + timedelta(minutes=minute)
    bar = Bar(ts, price, price * (1 + spread), price * (1 - spread), price, 1000.0)
    return BarClosed(product, bar, "rest", ts + timedelta(seconds=60 + lag))


async def armed(book: ShadowBook, product: str) -> None:
    for _ in range(2000):
        if book.gateway.has_pending_entry(product):
            return
        await asyncio.sleep(0.002)
    pytest.fail(f"{book.name}: the entry of {product} was never armed")


# =========================================================================================== building


async def test_every_book_has_its_own_account_guard_gateway_and_engine(ledger: PositionLedger) -> None:
    w = make_world(ledger, NAMES)
    books = build_books(CONFIGS, w.deps, resolve)
    assert [b.name for b in books] == NAMES
    assert [b.kind for b in books] == ["model", "model", "random_entry", "buy_and_hold"]
    for attr in ("account", "guard", "gateway", "engine"):
        assert len({id(getattr(b, attr)) for b in books}) == len(books), attr
    assert len({id(b.account.balances) for b in books}) == len(books)
    for b in books:
        assert b.account.book == b.name and b.account.balances.source == "virtual"
        assert b.account.balances.free("EUR") == D("100.00")


async def test_each_model_book_gets_the_risk_manager_and_policy_of_its_own_model(ledger: PositionLedger) -> None:
    w = make_world(ledger, NAMES)
    champion, challenger = build_books(CONFIGS[:2], w.deps, resolve)
    for book, model in ((champion, MODEL_A), (challenger, MODEL_B)):
        assert isinstance(book.engine, DecisionEngine) and book.risk is not None
        assert book.risk.book == book.name and book.risk._label is model.label.cfg
        assert book.risk._costs is model.label.costs and book.risk._gate is w.kill
        assert book.engine._risk is book.risk and book.engine._label is model.label
        assert book.engine._policy_cfg is model.policy and book.engine._questions is model.questions
        assert book.engine._positions._book == book.name  # reads the ledger of its own book only
        assert book.guard._positions._book == book.name  # and so does the coverage check of its guard
        assert book.guard._balances is book.account.balances
    assert champion.risk is not challenger.risk
    assert (champion.risk._label.k_tp, challenger.risk._label.k_tp) == (3.0, 4.0)
    assert champion.spread == MODEL_A.label.spread_for and challenger.spread == MODEL_B.label.spread_for


async def test_a_policy_tuned_for_another_label_is_refused(ledger: PositionLedger) -> None:
    w = make_world(ledger, NAMES)
    wrong = BookModel("bad", LABEL_B, POLICY, MODEL_A.questions, MODEL_A.baseline)  # POLICY belongs to LABEL
    with pytest.raises(ValueError, match="another label_config"):
        build_model_book(cfg("champion", "registry:champion"), w.deps, wrong)


async def test_random_entry_takes_model_policy_and_risk_from_its_source_and_needs_a_seed(
    ledger: PositionLedger,
) -> None:
    w = make_world(ledger, NAMES)
    champion = build_model_book(CONFIGS[0], w.deps, MODEL_A)
    random_book = build_random_book(CONFIGS[2], w.deps, champion)
    assert champion.tracker is not None and random_book.model is MODEL_A
    assert random_book.risk is not champion.risk and random_book.risk is not None
    assert random_book.risk._label is MODEL_A.label.cfg
    assert isinstance(random_book.engine, DecisionEngine) and random_book.engine._policy_cfg is POLICY
    assert isinstance(random_book.engine._baseline, RandomDecider)
    assert random_book.engine._baseline._tracker is champion.tracker  # it follows THIS book's BUY frequency
    with pytest.raises(BookConfigError, match="seed"):
        build_random_book(cfg("random_entry", "random", match_frequency_of="champion"), w.deps, champion)
    bh = build_buy_and_hold_book(CONFIGS[3], w.deps, MODEL_A.label.spread_for)
    with pytest.raises(BookConfigError, match="not a model book"):
        build_random_book(CONFIGS[2], w.deps, bh)


async def test_build_books_rejects_bad_configurations(ledger: PositionLedger) -> None:
    w = make_world(ledger, NAMES + ["lonely", "dup"])
    with pytest.raises(BookConfigError, match="duplicate"):
        build_books([CONFIGS[0], CONFIGS[0]], w.deps, resolve)
    with pytest.raises(BookConfigError, match="match_frequency_of"):
        build_books([cfg("r", "random", seed=1)], w.deps, resolve)
    with pytest.raises(BookConfigError, match="match_frequency_of"):
        build_books([cfg("r", "random", match_frequency_of="nobody", seed=1)], w.deps, resolve)
    with pytest.raises(BookConfigError, match="unknown decider"):
        build_books([cfg("lonely", "oracle")], w.deps, resolve)
    with pytest.raises(BookConfigError, match="spread"):
        build_books([cfg("lonely", "buy_and_hold")], w.deps, resolve)
    (bh,) = build_books([cfg("lonely", "buy_and_hold")], w.deps, resolve, spread=lambda p, t: 3.0)
    assert bh.kind == "buy_and_hold"
    with pytest.raises(KeyError):  # the model of an unknown alias is the caller's error, not hidden
        build_books([cfg("dup", "registry:nobody")], w.deps, resolve)


async def test_the_books_refuse_the_live_ledger(live_ledger: PositionLedger) -> None:
    w = make_world(live_ledger, NAMES)
    for build in (
        lambda: build_model_book(CONFIGS[0], w.deps, MODEL_A),
        lambda: build_buy_and_hold_book(CONFIGS[3], w.deps, lambda p, t: 3.0),
        lambda: build_twin_book("champion", D("100"), w.deps, lambda p, t: 3.0),
    ):
        with pytest.raises(LedgerModeMismatch, match="books run on the dry ledger only"):  # ours, before the gateway's
            build()


async def test_a_book_the_kill_switch_does_not_know_is_refused_at_build_time(ledger: PositionLedger) -> None:
    w = make_world(ledger, ["champion"])  # no AccountGuards for 'ghost': the guard would refuse every BUY silently
    build_twin_book("champion", D("100"), w.deps, lambda p, t: 3.0)
    with pytest.raises(BookConfigError, match="does not know"):
        build_twin_book("ghost", D("100"), w.deps, lambda p, t: 3.0)
    with pytest.raises(BookConfigError, match="does not know"):
        build_model_book(cfg("ghost", "registry:champion"), w.deps, MODEL_A)


async def test_products_must_share_one_quote_currency(ledger: PositionLedger) -> None:
    w = make_world(ledger, ["champion"], products=())
    with pytest.raises(BookConfigError, match="at least one product"):
        build_twin_book("champion", D("100"), w.deps, lambda p, t: 3.0)


# ============================================================================================ fan-out


class FakeEngine:
    def __init__(self, fail: Exception | None = None) -> None:
        self.batches: list[list[tuple[str, int]]] = []
        self.fail = fail
        self.drained: float | None | str = "no"
        self.order: list[str] | None = None
        self.name = ""

    async def on_bars(self, events: Any) -> list[Processed]:
        events = list(events)
        self.batches.append([(e.product_id, int((e.bar.ts - T0).total_seconds() // 60)) for e in events])
        if self.order is not None:
            self.order.append(f"engine:{self.name}")
        if self.fail is not None:
            raise self.fail
        return []

    async def drain(self, timeout: float | None = None) -> None:
        self.drained = timeout


def twins(w: World, names: list[str]) -> list[ShadowBook]:
    out = []
    for n in names:
        book = build_twin_book(n, D("100"), w.deps, lambda p, t: 3.0)
        book.engine = FakeEngine()
        out.append(book)
    return out


def fake(book: ShadowBook) -> FakeEngine:
    assert isinstance(book.engine, FakeEngine)
    return book.engine


async def test_every_bar_reaches_every_book_in_bar_order(ledger: PositionLedger) -> None:
    w = make_world(ledger, ["a", "b", "c"])
    books = twins(w, ["a", "b", "c"])
    seen: dict[str, list[tuple[str, datetime]]] = {b.name: [] for b in books}
    for b in books:
        original = b.gateway.on_bar

        async def spy(bar: Any, _b: ShadowBook = b, _o: Any = original) -> list[ExitResult]:
            seen[_b.name].append((bar.product_id, bar.ts_open))
            return await _o(bar)  # type: ignore[no-any-return]

        b.gateway.on_bar = spy  # type: ignore[method-assign]
    manager = ShadowBookManager(books)
    out = await manager.on_bars([event(SOL, 2), event(BTC, 2), event(BTC, 1)])
    expected = [(BTC, T0 + timedelta(minutes=1)), (BTC, T0 + timedelta(minutes=2)), (SOL, T0 + timedelta(minutes=2))]
    assert set(out) == {"a", "b", "c"}
    for b in books:
        assert seen[b.name] == expected and fake(b).batches == [[(BTC, 1), (BTC, 2), (SOL, 2)]]
        assert b.stats.bars == 3
    assert await manager.on_bars([]) == {}


async def test_the_gateway_sees_the_bar_before_the_engine_decides(ledger: PositionLedger) -> None:
    w = make_world(ledger, ["a"])
    (book,) = twins(w, ["a"])
    order: list[str] = []
    fake(book).order, fake(book).name = order, "a"
    original = book.gateway.on_bar

    async def spy(bar: Any) -> list[ExitResult]:
        order.append("gateway:a")
        return await original(bar)  # type: ignore[no-any-return]

    book.gateway.on_bar = spy  # type: ignore[method-assign]
    await ShadowBookManager([book]).on_bars([event(BTC, 1), event(SOL, 1)])
    assert order == ["gateway:a", "gateway:a", "engine:a"]  # every bar to the gateway, then the batch to the engine


async def test_a_failing_engine_stays_in_its_book(ledger: PositionLedger) -> None:
    w = make_world(ledger, ["a", "b"])
    a, b = twins(w, ["a", "b"])
    fake(a).fail = RuntimeError("decider exploded")
    errors: list[tuple[str, str]] = []
    manager = ShadowBookManager([a, b], on_error=lambda book, stage, exc: errors.append((book, stage)))
    await manager.on_bars([event(BTC, 1)])
    await manager.on_bars([event(BTC, 2)])
    assert fake(b).batches == [[(BTC, 1)], [(BTC, 2)]]  # b never noticed
    assert fake(a).batches == [[(BTC, 1)], [(BTC, 2)]]  # and a still gets its bars
    assert (a.stats.engine_errors, a.stats.consecutive_errors, b.stats.engine_errors) == (2, 2, 0)
    assert a.stats.last_error == "engine: RuntimeError: decider exploded" and b.stats.last_error is None
    assert errors == [("a", "engine"), ("a", "engine")]
    fake(a).fail = None
    await manager.on_bars([event(BTC, 3)])
    assert a.stats.consecutive_errors == 0 and a.stats.engine_errors == 2  # recovered; history kept


async def test_a_failing_gateway_does_not_keep_the_engine_from_seeing_the_bar(ledger: PositionLedger) -> None:
    w = make_world(ledger, ["a", "b"])
    a, b = twins(w, ["a", "b"])

    async def broken(bar: Any) -> list[ExitResult]:
        raise OSError("ledger write failed")

    a.gateway.on_bar = broken  # type: ignore[method-assign]
    manager = ShadowBookManager([a, b])
    await manager.on_bars([event(BTC, 1), event(SOL, 1)])
    assert fake(a).batches == [[(BTC, 1), (SOL, 1)]]  # the indicators of a must not skip a bar
    assert fake(b).batches == [[(BTC, 1), (SOL, 1)]]
    assert (a.stats.gateway_errors, a.stats.engine_errors, a.stats.consecutive_errors) == (2, 0, 2)
    assert a.stats.last_error == "gateway: OSError: ledger write failed" and b.stats.gateway_errors == 0


async def test_a_cancelled_run_is_not_swallowed_as_a_book_error(ledger: PositionLedger) -> None:
    w = make_world(ledger, ["a"])
    (a,) = twins(w, ["a"])
    gate = asyncio.Event()

    async def hang(events: Any) -> list[Processed]:
        await gate.wait()
        return []

    a.engine.on_bars = hang  # type: ignore[union-attr, method-assign]
    manager = ShadowBookManager([a])
    task = asyncio.create_task(manager.on_bars([event(BTC, 1)]))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert a.stats.engine_errors == 0


async def test_bracket_exits_are_reported_with_their_book_and_a_bad_callback_is_isolated(
    ledger: PositionLedger,
) -> None:
    w = make_world(ledger, ["a", "b"])
    a, b = twins(w, ["a", "b"])
    closed_result = ExitResult("closed", "TP", size=D("1"), avg_price=D("101"))

    async def exits(bar: Any) -> list[ExitResult]:
        return [closed_result]

    a.gateway.on_bar = exits  # type: ignore[method-assign]
    got: list[tuple[str, str, ExitResult]] = []

    def on_closed(book: str, product: str, result: ExitResult) -> None:
        got.append((book, product, result))
        raise ValueError("report sink is down")

    manager = ShadowBookManager([a, b], on_closed=on_closed)
    await manager.on_bars([event(SOL, 1)])
    assert got == [("a", SOL, closed_result)]
    assert a.stats.engine_errors == 1 and "on_closed" in (a.stats.last_error or "") and b.stats.engine_errors == 0


async def test_run_serves_the_events_that_are_already_waiting_together(ledger: PositionLedger) -> None:
    w = make_world(ledger, ["a"])
    (a,) = twins(w, ["a"])
    manager = ShadowBookManager([a])
    queue: asyncio.Queue[BarClosed] = asyncio.Queue()
    for p in PRODUCTS:
        queue.put_nowait(event(p, 1))
    task = asyncio.create_task(manager.run(queue))
    for _ in range(200):
        if fake(a).batches:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake(a).batches == [[(BTC, 1), (SOL, 1)]]


async def test_drain_reaches_every_engine_and_a_bad_one_does_not_stop_it(ledger: PositionLedger) -> None:
    w = make_world(ledger, ["a", "b"])
    a, b = twins(w, ["a", "b"])

    async def boom(timeout: float | None = None) -> None:
        raise RuntimeError("drain failed")

    a.engine.drain = boom  # type: ignore[union-attr, method-assign]
    await ShadowBookManager([a, b]).drain(2.5)
    assert fake(b).drained == 2.5 and a.stats.engine_errors == 1


async def test_the_manager_needs_books_with_distinct_names(ledger: PositionLedger) -> None:
    w = make_world(ledger, ["a"])
    (a,) = twins(w, ["a"])
    with pytest.raises(BookConfigError, match="duplicate"):
        ShadowBookManager([a, a])
    with pytest.raises(BookConfigError, match="at least one"):
        ShadowBookManager([])
    m = ShadowBookManager([a])
    assert m["a"] is a and set(m.books) == {"a"}
    status = m.status()["a"]
    assert status["kind"] == "twin" and status["fees_paid"] == "0" and status["bars"] == 0


# ======================================================================================== buy and hold


def bh_world(ledger: PositionLedger, **kw: Any) -> tuple[World, ShadowBook, ShadowBookManager]:
    w = make_world(ledger, ["buy_and_hold"], **kw)
    book = build_buy_and_hold_book(cfg("buy_and_hold", "buy_and_hold"), w.deps, lambda p, t: 2.0)
    return w, book, ShadowBookManager([book])


def prices(w: World, btc: float = 60000.0, sol: float = 150.0) -> None:
    w.quotes.around(BTC, btc)
    w.quotes.around(SOL, sol)


async def settle(book: ShadowBook, products: tuple[str, ...] = PRODUCTS, minute: int = 1) -> None:
    """Feed the bar that fills the entries, once they are armed."""
    assert isinstance(book.engine, BuyAndHoldDriver)
    for p in products:
        await armed(book, p)


async def test_buy_and_hold_buys_each_product_once_with_an_equal_share_through_the_guard(
    ledger: PositionLedger,
) -> None:
    w, book, manager = bh_world(ledger)
    prices(w)
    await manager.on_bars([event(BTC, 0, 60000.0), event(SOL, 0, 150.0)])  # time 0: both entries are submitted
    await settle(book)
    w.clock.now = T0 + timedelta(minutes=5)
    await manager.on_bars([event(BTC, 1, 60000.0), event(SOL, 1, 150.0)])  # the bar that fills them
    await manager.drain(5)
    driver = book.engine
    assert isinstance(driver, BuyAndHoldDriver) and driver.bought == {BTC, SOL}
    for p in PRODUCTS:
        view = ledger.view("buy_and_hold", p)
        assert view.is_long and view.bracket_order_id is not None
        assert view.sl == D("0.01") and view.tp is not None and view.tp > D(150) * 100  # a bracket that cannot trigger
    spent = D("100") - book.account.balances.free("EUR")
    assert D("90") < spent <= D("100")  # the whole starting quote, half per product, fees included
    assert min(book.account.balances.free("EUR"), book.account.balances.reserved("EUR")) >= 0
    # not twice: more bars, no more orders
    orders_before = len(await ledger.open_orders("buy_and_hold"))
    for minute in (2, 3):
        w.clock.now += timedelta(minutes=1)
        await manager.on_bars([event(BTC, minute, 60000.0), event(SOL, minute, 150.0)])
    await manager.drain(0.2)
    assert len(await ledger.open_orders("buy_and_hold")) == orders_before
    assert book.stats.engine_errors == book.stats.gateway_errors == 0


async def test_buy_and_hold_never_asks_the_guard_for_more_than_it_covers(ledger: PositionLedger) -> None:
    """The size is ``max_base_for_quote`` of the free quote: the guard authorizes exactly what the driver asks."""
    w, book, manager = bh_world(ledger)
    prices(w)
    refusals: list[str] = []
    original = book.guard.authorize

    def watch(spec: Any) -> Any:
        try:
            return original(spec)
        except Exception as exc:
            refusals.append(f"{type(exc).__name__}: {exc}")
            raise

    book.guard.authorize = watch  # type: ignore[method-assign]
    await manager.on_bars([event(BTC, 0, 60000.0), event(SOL, 0, 150.0)])
    await settle(book)
    w.clock.now = T0 + timedelta(minutes=5)
    await manager.on_bars([event(BTC, 1, 60000.0), event(SOL, 1, 150.0)])
    await manager.drain(5)
    assert refusals == []
    assert book.account.balances.free("EUR") >= 0
    assert all(r.amount >= 0 for r in book.account.balances.open_reservations())


async def test_buy_and_hold_with_the_whole_quote_on_one_product_is_still_covered(ledger: PositionLedger) -> None:
    """One product gets the whole 100 EUR: only the guard's own fee rate keeps size x limit x (1 + fee) inside it."""
    w, book, manager = bh_world(ledger, products=(BTC,))
    prices(w)
    refusals: list[str] = []
    original = book.guard.authorize

    def watch(spec: Any) -> Any:
        try:
            return original(spec)
        except Exception as exc:
            refusals.append(str(exc))
            raise

    book.guard.authorize = watch  # type: ignore[method-assign]
    await manager.on_bars([event(BTC, 0, 60000.0)])
    await armed(book, BTC)
    w.clock.now = T0 + timedelta(minutes=5)
    await manager.on_bars([event(BTC, 1, 60000.0)])
    await manager.drain(5)
    assert refusals == [] and ledger.view("buy_and_hold", BTC).is_long
    assert D("95") < D("100") - book.account.balances.free("EUR") <= D("100")


async def test_buy_and_hold_retries_an_entry_that_did_not_fill(ledger: PositionLedger) -> None:
    w, book, manager = bh_world(ledger)
    prices(w)
    await manager.on_bars([event(BTC, 0, 60000.0)])  # BTC only; the first bar after the order is far above the limit
    await armed(book, BTC)
    w.clock.now = T0 + timedelta(minutes=5)
    await manager.on_bars([event(BTC, 1, 66000.0)])
    await manager.drain(5)
    assert isinstance(book.engine, BuyAndHoldDriver) and BTC not in book.engine.bought
    assert not ledger.view("buy_and_hold", BTC).is_long
    assert D("100") - book.account.balances.free("EUR") == 0  # the reservation went back to free
    prices(w, btc=66000.0)
    w.clock.now = T0 + timedelta(minutes=6)
    await manager.on_bars([event(BTC, 6, 66000.0)])  # a new attempt, under a new order id
    await armed(book, BTC)
    w.clock.now = T0 + timedelta(minutes=9)
    await manager.on_bars([event(BTC, 8, 66000.0)])
    await manager.drain(5)
    assert BTC in book.engine.bought and ledger.view("buy_and_hold", BTC).is_long


async def test_buy_and_hold_waits_for_a_quote_and_skips_a_budget_below_the_minimum(ledger: PositionLedger) -> None:
    w, book, manager = bh_world(ledger)
    await manager.on_bars([event(BTC, 0, 60000.0)])  # no quote: nothing to price an order with
    assert not book.gateway.has_pending_entry(BTC) and await ledger.open_orders("buy_and_hold") == []
    poor = make_world(ledger, ["poor"])
    poor_cfg = cfg("poor", "buy_and_hold", starting_quote=D("1.5"))
    poor_book = build_buy_and_hold_book(poor_cfg, poor.deps, lambda p, t: 2.0)
    poor.quotes.around(BTC, 60000.0)
    await ShadowBookManager([poor_book]).on_bars([event(BTC, 0, 60000.0)])  # 0.75 EUR per product < 1 EUR minimum
    assert not poor_book.gateway.has_pending_entry(BTC) and await ledger.open_orders("poor") == []


async def test_buy_and_hold_does_not_buy_again_when_the_position_already_exists(ledger: PositionLedger) -> None:
    w, book, manager = bh_world(ledger)
    prices(w)
    await ledger.apply_entry_fill("buy_and_hold", BTC, D("0.001"), D("60000"), "old", T0)  # restart: already long
    await manager.on_bars([event(BTC, 0, 60000.0)])
    assert isinstance(book.engine, BuyAndHoldDriver) and book.engine.bought == {BTC}
    assert not book.gateway.has_pending_entry(BTC)


# ============================================================================================== random


def processed(action: str, p_edge: float | None) -> Processed:
    decision = Decision(action, "t", p_edge, 2)  # type: ignore[arg-type]
    return Processed("d", BTC, T0, decision, None, None, "", {})


def test_the_tracker_reports_the_buy_frequency_and_the_mean_edge_of_the_last_24_hours() -> None:
    clock = Clock()
    clock.now = T0
    t = BuyRateTracker(clock)
    assert (t.rate(), t.edge()) == (0.0, None)  # nothing observed: no entries
    t.observe([processed("BUY", 0.6), processed("HOLD", 0.9), processed("HOLD", None), processed("BUY", 0.8)])
    assert (t.rate(), t.edge()) == (0.0, None)  # recomputed once an hour, not at every decision
    clock.advance(3600)
    assert t.rate() == pytest.approx(2 / 4) and t.edge() == pytest.approx(0.7)
    t.observe([processed("HOLD", 0.5)] * 4)
    clock.advance(1800)
    assert t.rate() == pytest.approx(2 / 4)  # still the value of the last hourly refresh
    clock.advance(1800)
    assert t.rate() == pytest.approx(2 / 8)
    clock.advance(86400)  # everything is older than 24 h now
    assert (t.rate(), t.edge()) == (0.0, None)


def test_the_tracker_ignores_products_that_failed_before_deciding() -> None:
    clock = Clock()
    clock.now = T0
    t = BuyRateTracker(clock)
    failed = Processed("d", BTC, T0, None, None, "engine_error", "boom", {})
    t.observe([failed, processed("BUY", 0.7)])
    clock.advance(3600)
    assert t.rate() == 1.0


def tracker_with(rate_buys: int, trials: int, edge: float = 0.7) -> BuyRateTracker:
    clock = Clock()
    clock.now = T0
    t = BuyRateTracker(clock)
    t.observe([processed("BUY", edge)] * rate_buys + [processed("HOLD", None)] * (trials - rate_buys))
    clock.advance(3600)
    return t


async def draws(decider: RandomDecider, n: int) -> list[str]:
    return [(await decider.decide("{}", {})).action.choice for _ in range(n)]


async def test_the_random_decider_is_reproducible_and_follows_the_observed_rate() -> None:
    a = await draws(RandomDecider(42, tracker_with(1, 10)), 3000)
    b = await draws(RandomDecider(42, tracker_with(1, 10)), 3000)
    c = await draws(RandomDecider(7, tracker_with(1, 10)), 3000)
    assert a == b and a != c  # same seed, same sequence
    assert 0.08 <= a.count("BUY") / len(a) <= 0.12  # the observed 10%
    assert set(a) == {"BUY", "HOLD"}  # it never says SELL


async def test_the_random_decider_stays_flat_without_an_observed_buy() -> None:
    assert set(await draws(RandomDecider(1, tracker_with(0, 50)), 500)) == {"HOLD"}  # rate 0
    clock = Clock()
    clock.now = T0
    assert set(await draws(RandomDecider(1, BuyRateTracker(clock)), 100)) == {"HOLD"}  # nothing observed yet


async def test_the_random_decider_answers_what_the_policy_turns_into_buy_and_hold() -> None:
    policy = DecisionPolicy(POLICY)
    always = RandomDecider(3, tracker_with(10, 10, edge=0.7))
    buy = await always.decide("{}", {})
    assert buy.edge.noul == pytest.approx(0.7)  # the source book's mean edge, so it meets the same EV gate
    assert policy.resolve(buy, "FLAT", vol_pctile=50.0).action == "BUY"
    assert policy.resolve(buy, "LONG", vol_pctile=50.0).action == "HOLD"  # holding: BUY is masked, no SELL
    assert policy.resolve(buy, "FLAT", vol_pctile=1.0).reason == "regime_veto"  # the veto reads the state
    hold = await RandomDecider(3, tracker_with(0, 10)).decide("{}", {})
    assert policy.resolve(hold, "FLAT", vol_pctile=50.0).action == "HOLD"


async def test_the_decisions_of_the_source_book_feed_the_tracker_of_the_random_book(ledger: PositionLedger) -> None:
    w = make_world(ledger, NAMES)
    champion = build_model_book(CONFIGS[0], w.deps, MODEL_A)
    random_book = build_random_book(CONFIGS[2], w.deps, champion)
    random_book.engine = FakeEngine()  # only the plumbing is under test here
    champion.engine = FakeEngine()

    async def decide(events: Any) -> list[Processed]:
        return [processed("BUY", 0.65), processed("HOLD", None), processed("HOLD", None), processed("HOLD", None)]

    champion.engine.on_bars = decide  # type: ignore[method-assign]
    manager = ShadowBookManager([champion, random_book])
    await manager.on_bars([event(BTC, 1)])
    w.clock.advance(3600)
    tracker = champion.tracker
    assert tracker is not None and tracker.rate() == pytest.approx(0.25) and tracker.edge() == pytest.approx(0.65)


# ================================================================================================ twin


async def test_the_twin_has_no_engine_and_serves_the_bars_of_the_real_book(ledger: PositionLedger) -> None:
    w = make_world(ledger, ["shadow_twin"])
    spreads: list[tuple[str, float]] = []

    def spread(product: str, at: datetime) -> float:
        spreads.append((product, 3.5))
        return 3.5

    twin = build_twin_book("shadow_twin", D("250"), w.deps, spread)
    assert twin.kind == "twin" and twin.engine is None and twin.risk is None and twin.model_version is None
    assert twin.account.balances.free("EUR") == D("250")
    manager = ShadowBookManager([twin])
    out = await manager.on_bars([event(BTC, 1)])
    assert out == {"shadow_twin": []} and spreads == [(BTC, 3.5)] and twin.stats.bars == 1


# ====================================================================== isolation and the dry database


def real_event(product: str, i: int, lag: float = 120.0) -> BarClosed:
    row = frame(product).iloc[i]
    ts = frame(product).index[i].to_pydatetime()
    bar = Bar(ts, float(row.open), float(row.high), float(row.low), float(row.close), float(row.volume))
    return BarClosed(product, bar, "rest", ts + timedelta(seconds=60 + lag))


async def real_manager(ledger: PositionLedger, configs: list[BookConfig]) -> tuple[World, ShadowBookManager]:
    w = make_world(ledger, [c.name for c in configs])
    manager = ShadowBookManager.from_config(configs, w.deps, resolve)
    for book in manager.books.values():
        if isinstance(book.engine, DecisionEngine):
            for p in PRODUCTS:
                last = frame(p).iloc[WARM_A - 1]
                book.engine.adopt(p, warmed(p, WARM_A), frame(p).index[WARM_A - 1].to_pydatetime(), float(last.close))
    return w, manager


async def run_minutes(w: World, manager: ShadowBookManager, minutes: int) -> None:
    for i in range(WARM_A, WARM_A + minutes):
        events = [real_event(p, i) for p in PRODUCTS]
        w.clock.now = events[0].published_at
        for p in PRODUCTS:
            w.quotes.around(p, float(frame(p).close.iloc[i]))
        await manager.on_bars(events)
        await manager.drain(0.05)


async def tables(db: PositionLedger) -> dict[str, int]:
    out = {}
    for table in ("positions", "orders", "fills"):
        async with db.connection.execute(f"SELECT COUNT(*) FROM {table}") as cur:  # noqa: S608
            out[table] = (await cur.fetchone())[0]  # type: ignore[index]
    return out


async def test_real_books_keep_their_state_apart_on_one_dry_ledger(ledger: PositionLedger) -> None:
    w, manager = await real_manager(ledger, CONFIGS[:2])
    await run_minutes(w, manager, 90)
    champion, challenger = manager["champion"], manager["challenger"]
    assert champion.stats.engine_errors == champion.stats.gateway_errors == 0
    assert challenger.stats.engine_errors == challenger.stats.gateway_errors == 0
    async with ledger.connection.execute("SELECT DISTINCT book FROM orders") as cur:
        books_with_orders = {r[0] for r in await cur.fetchall()}
    assert books_with_orders <= {"champion", "challenger"} and books_with_orders  # something traded
    # the records carry their own book, and each engine its own model's version tag
    assert {r["book"] for r in w.sink.records} == {"champion", "challenger"}
    for name in ("champion", "challenger"):
        risk_book = manager[name].risk
        assert risk_book is not None and risk_book.book == name
    for book in (champion, challenger):
        reserved = sum(r.amount for r in book.account.balances.open_reservations() if r.currency == "EUR")
        assert book.account.balances.free("EUR") + reserved == book.account.balances.total("EUR") <= D("100")
        assert book.account.balances.free("EUR") >= 0


async def test_dry_never_writes(
    ledger: PositionLedger, live_ledger: PositionLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The full books pipeline in dry over 30 simulated minutes: no exchange method that writes is ever called (any
    call fails the test) and the live database stays empty."""
    from coinbase.rest import RESTClient

    from modules.execution.exchange import CoinbaseExchange

    def forbidden(name: str) -> Callable[..., Any]:
        def _fail(*args: Any, **kwargs: Any) -> Any:
            pytest.fail(f"a write reached the exchange: {name}")

        return _fail

    for cls in (RESTClient, CoinbaseExchange):
        for name in (
            "create_order", "cancel_orders", "preview_order", "edit_order", "post", "put", "delete",
            "market_order_buy", "market_order_sell", "limit_order_gtc", "limit_order_gtc_buy", "limit_order_gtc_sell",
            "trigger_bracket_order_gtc", "trigger_bracket_order_gtc_sell",
        ):  # fmt: skip
            if hasattr(cls, name):
                monkeypatch.setattr(cls, name, forbidden(f"{cls.__name__}.{name}"))
    w, manager = await real_manager(ledger, CONFIGS)
    await run_minutes(w, manager, 30)
    assert all(b.stats.engine_errors == b.stats.gateway_errors == 0 for b in manager.books.values())
    assert await tables(live_ledger) == {"positions": 0, "orders": 0, "fills": 0}
    assert (await tables(ledger))["orders"] > 0  # ...while the dry ledger did record the simulated orders
    async with live_ledger.connection.execute("SELECT COUNT(*) FROM kill_switch_state") as cur:
        assert tuple(await cur.fetchone()) == (0,)  # type: ignore[arg-type]


def test_the_books_module_cannot_reach_an_exchange_or_a_live_gateway() -> None:
    tree = ast.parse(Path(books_module.__file__).read_text(encoding="utf-8"))
    imported = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    assert not {m for m in imported if m.startswith("coinbase") or m.endswith(".exchange") or "user_stream" in m}
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
    }
    assert not names & {
        "create_order", "cancel_orders", "preview_order", "build_order_body", "CoinbaseExchange", "RESTClient",
        "LiveExecutionGateway", "CoverageTicket", "_reserve",
    }  # fmt: skip
    assert "authorize" not in names, "orders are authorized by the gateways (and so by their own guards), not here"


def test_orders_leave_a_book_only_through_its_gateway() -> None:
    """Every ``submit_*`` call in the module goes to a ShadowExecutionGateway attribute, never to a bare object."""
    tree = ast.parse(Path(books_module.__file__).read_text(encoding="utf-8"))
    calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert {c for c in calls if c.startswith("submit")} == {"submit_entry_with_bracket"}

