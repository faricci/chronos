"""LiveBalanceFeed: the background task that keeps the exchange BalanceBook fresh (vincolo C11).

The book and the guard are proven elsewhere (test_balance*.py, test_coverage_properties.py). Here: the feed's own
rules (epoch race, fail closed, foreign orders, timing) and the properties of guard + feed together.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from modules.config import ExecutionConfig, FallbackFees
from modules.execution.balance import AccountSnapshot, BalanceBook, CoverageAlarm, Reservation, SnapshotOutcome
from modules.execution.balance_feed import LiveBalanceFeed, feed_from_config
from modules.execution.balance_guard import (
    BALANCE_MAX_AGE_S,
    BalanceGuard,
    BalanceStale,
    InsufficientCoverage,
)
from modules.execution.exchange import ExchangeUnavailable
from modules.execution.spot import SpotRegistry
from modules.risk.costs import FeeProvider, coverage_fee_rate, required_quote
from tests.coverage_fixtures import SOL, Clock, buy, make_guard, sell

D = Decimal
REGISTRY = SpotRegistry({"SOL-EUR": SOL})
F_COV = coverage_fee_rate(FeeProvider(FallbackFees()).current, FallbackFees())


@dataclass(frozen=True)
class Open:
    client_order_id: str
    product_id: str = "SOL-EUR"
    side: str = "BUY"


class FakeReader:
    """Stand-in for CoinbaseExchange: two reads, a script for failures and for events that race with the read."""

    def __init__(self, clock: Clock, **balances: tuple[str, str]) -> None:
        self.clock = clock
        self.balances: dict[str, tuple[Decimal, Decimal]] = {c: (D(a), D(h)) for c, (a, h) in balances.items()}
        self.orders: list[Open] = []
        self.fail: Exception | None = None
        self.during_read: Callable[[], None] | None = None
        self.read_takes_s = 0.0
        self.reads = 0

    async def get_accounts(self) -> AccountSnapshot:
        self.reads += 1
        if self.fail is not None:
            raise self.fail
        if self.during_read is not None:
            self.during_read()
        self.clock.advance(self.read_takes_s)
        return AccountSnapshot(balances=dict(self.balances), taken_at=self.clock())

    async def list_open_orders(self) -> list[Open]:
        return list(self.orders)


def setup(
    quote: str = "0", base: str = "0", owned: str = "0", **balances: tuple[str, str]
) -> tuple[LiveBalanceFeed, FakeReader, BalanceGuard, BalanceBook, Clock]:
    guard, book, _, _, clock = make_guard(quote=quote, base=base, owned=owned, source="exchange")
    reader = FakeReader(clock, **(balances or {"EUR": ("100", "0"), "SOL": ("0", "0")}))
    feed = LiveBalanceFeed(reader, book, REGISTRY, refresh_s=30, clock=clock)
    return feed, reader, guard, book, clock


# ------------------------------------------------------------------------------------ first snapshot


async def test_nothing_can_be_bought_until_the_first_snapshot_and_then_it_can() -> None:
    feed, reader, guard, book, _ = setup()
    assert feed.is_stale() and feed.age_s() is None
    with pytest.raises(BalanceStale):
        guard.authorize(buy())
    assert await feed.refresh_once() is SnapshotOutcome.ACCEPTED
    assert feed.age_s() == 0.0 and not feed.is_stale() and book.free("EUR") == D(100)
    guard.authorize(buy())


async def test_the_staleness_limit_of_the_feed_is_the_one_of_the_guard() -> None:
    feed, _, guard, _, clock = setup()
    assert feed.max_age_s == BALANCE_MAX_AGE_S == 120
    await feed.refresh_once()
    clock.advance(BALANCE_MAX_AGE_S)
    assert not feed.is_stale()
    guard.authorize(buy("b1"))
    clock.advance(1)
    assert feed.is_stale()
    with pytest.raises(BalanceStale):
        guard.authorize(buy("b2"))


# ----------------------------------------------------------------------------------------- fail closed


async def test_a_dead_feed_ages_the_book_until_the_guard_refuses_buys_but_not_exits() -> None:
    feed, reader, guard, book, clock = setup(quote="0", base="0", owned="0.1", EUR=("100", "0"), SOL=("0.1", "0"))
    await feed.refresh_once()
    reader.fail = ExchangeUnavailable("down")
    for _ in range(5):  # a failure changes nothing: no optimistic carry-over, no reset of the age
        assert await feed.refresh_once() is None
        clock.advance(30)
    assert feed.consecutive_failures == 5 and "ExchangeUnavailable" in (feed.last_error or "")
    assert book.free("EUR") == D(100) and feed.age_s() == 150.0
    with pytest.raises(BalanceStale):
        guard.authorize(buy())
    guard.authorize(sell(size="0.1"))  # selling what we own is never blocked by age
    reader.fail = None
    assert await feed.refresh_once() is SnapshotOutcome.ACCEPTED
    assert feed.consecutive_failures == 0 and feed.last_error is None and feed.age_s() == 0.0
    guard.authorize(buy())


async def test_age_counts_from_the_start_of_the_read() -> None:
    feed, reader, _, _, _ = setup()
    reader.read_takes_s = 5.0
    await feed.refresh_once()
    assert feed.age_s() == 5.0  # the data may be up to 5 s older than the moment it arrived: never under-estimated


# ---------------------------------------------------------------------------------------- epoch rule


async def test_snapshot_read_during_local_event_is_discarded() -> None:
    feed, reader, guard, book, _ = setup()
    await feed.refresh_once()
    reader.balances = {"EUR": (D(100), D(0)), "SOL": (D(0), D(0))}  # the exchange still shows the old picture
    reader.during_read = lambda: guard.authorize(buy("raced"))  # a local reservation while the REST call is open
    before = book.snapshot_at
    assert await feed.refresh_once() is SnapshotOutcome.DISCARDED_EPOCH
    assert book.snapshot_at == before and feed.discarded_snapshots == 1
    assert book.free("EUR") == D("97.75") and book.reserved("EUR") == D("2.25")  # the local event survives
    reader.during_read = None
    reader.balances["EUR"] = (D("97.75"), D("2.25"))
    assert await feed.refresh_once() is SnapshotOutcome.ACCEPTED
    assert book.free("EUR") == D("97.75") and book.reserved("EUR") == D("2.25") and feed.accepted_snapshots == 2


# --------------------------------------------------------------------------------------- foreign orders


async def test_our_own_open_order_gets_its_hold_credited_once() -> None:
    feed, reader, guard, book, _ = setup(quote="100")
    await feed.refresh_once()
    guard.authorize(buy("mine"))  # reserves 2.25
    reader.balances["EUR"] = (D("97.75"), D("2.25"))
    reader.orders = [Open("mine")]
    await feed.refresh_once()
    assert book.free("EUR") == D("97.75")  # 100 total - 2.25 held, subtracted once


async def test_a_foreign_order_removes_the_hold_credit() -> None:
    feed, reader, guard, book, _ = setup(quote="100")
    await feed.refresh_once()
    guard.authorize(buy("mine"))
    reader.balances["EUR"] = (D("77.75"), D("22.25"))  # our 2.25 hold plus 20 of somebody else's
    reader.orders = [Open("mine"), Open("not-ours")]
    alarms: list[CoverageAlarm] = []
    feed._on_alarm = alarms.append
    await feed.refresh_once()
    assert book.free("EUR") == D("75.50") and alarms == []  # 77.75 - 2.25: conservative

    reader.orders = [Open("mine")]  # the same hold with no foreign order to explain it: breach evidence
    await feed.refresh_once()
    assert "hold_exceeds_reservation" in [a.kind for a in alarms]  # (and a balance_drift: the book had 22 EUR less)


@pytest.mark.parametrize(
    ("product", "side", "expected"),
    [
        ("SOL-EUR", "BUY", {"EUR"}),
        ("SOL-EUR", "SELL", {"SOL"}),
        ("SOL-EUR", "", {"SOL", "EUR"}),
        ("SOL-EUR", "UNKNOWN_ORDER_SIDE", {"SOL", "EUR"}),
        ("BTC-USD", "BUY", {"USD"}),  # a product outside the registry is still parsed
        ("BTC-USD", "SELL", {"BTC"}),
    ],
)
async def test_the_currency_on_hold_follows_the_side_of_the_foreign_order(
    product: str, side: str, expected: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    feed, reader, _, book, _ = setup()
    seen: list[AccountSnapshot] = []
    real = book.apply_snapshot
    monkeypatch.setattr(book, "apply_snapshot", lambda snap, e, inc: (seen.append(snap), real(snap, e, inc))[1])
    reader.orders = [Open("x", product, side)]
    await feed.refresh_once()
    assert seen[0].foreign_order_currencies == expected


async def test_alarm_handler_that_raises_does_not_stop_the_refresh() -> None:
    feed, reader, guard, book, _ = setup(quote="100")
    await feed.refresh_once()
    guard.authorize(buy("mine"))
    reader.balances["EUR"] = (D(50), D(50))  # hold far above the reservation, no foreign order
    reader.orders = [Open("mine")]

    def broken(alarm: CoverageAlarm) -> None:
        raise RuntimeError("handler bug")

    feed._on_alarm = broken
    assert await feed.refresh_once() is SnapshotOutcome.ACCEPTED


# ------------------------------------------------------------------------------------------ the loop


class StopLoop(Exception):
    pass


async def run_for(feed: LiveBalanceFeed, steps: int) -> list[float]:
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)
        if len(delays) == steps:
            raise StopLoop

    feed._sleep = sleep
    with pytest.raises(StopLoop):
        await feed.run()
    return delays


async def test_the_loop_paces_success_retry_after_a_discard_and_backoff_after_failures() -> None:
    feed, reader, guard, _, _ = setup(quote="1000")
    script: list[str] = ["ok", "race", "fail", "fail", "fail", "fail", "fail", "fail", "fail", "ok"]
    original = reader.get_accounts
    counter = {"n": 0}

    async def scripted() -> AccountSnapshot:
        step = script[counter["n"]]
        counter["n"] += 1
        reader.fail = ExchangeUnavailable("x") if step == "fail" else None
        reader.during_read = (lambda: guard.authorize(buy(f"r{counter['n']}"))) if step == "race" else None
        return await original()

    reader.get_accounts = scripted  # type: ignore[method-assign]
    delays = await run_for(feed, len(script))
    assert delays == [30, 1, 1, 2, 4, 8, 16, 30, 30, 30]  # ok, discard, then 1,2,4,... capped at refresh_s, ok


async def test_as_a_task_the_feed_refreshes_repeatedly_and_stops_on_cancel() -> None:
    guard, book, _, _, clock = make_guard(source="exchange")
    reader = FakeReader(clock, EUR=("10", "0"))
    feed = LiveBalanceFeed(reader, book, REGISTRY, refresh_s=0.01, clock=clock)
    task = asyncio.create_task(feed.run())
    for _ in range(200):
        if reader.reads >= 3:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert reader.reads >= 3 and book.free("EUR") == D(10)


# --------------------------------------------------------------------------------------- construction


def test_the_feed_only_serves_an_exchange_book_refreshed_often_enough() -> None:
    _, vbook, _, _, clock = make_guard(source="virtual")
    reader = FakeReader(clock)
    with pytest.raises(ValueError, match="virtual"):
        LiveBalanceFeed(reader, vbook, REGISTRY, refresh_s=30)
    _, ebook, _, _, _ = make_guard(source="exchange")
    LiveBalanceFeed(reader, ebook, REGISTRY, refresh_s=BALANCE_MAX_AGE_S / 3)  # three attempts fit
    for bad in (BALANCE_MAX_AGE_S / 3 + 1, 120, 0, -5):
        with pytest.raises(ValueError):
            LiveBalanceFeed(reader, ebook, REGISTRY, refresh_s=bad)
    with pytest.raises(ValueError):
        LiveBalanceFeed(reader, ebook, REGISTRY, refresh_s=30, retry_s=0)


def test_the_refresh_period_comes_from_the_execution_config() -> None:
    _, ebook, _, _, clock = make_guard(source="exchange")
    feed = feed_from_config(ExecutionConfig(balance_refresh_s=25), FakeReader(clock), ebook, REGISTRY)
    assert feed._refresh_s == 25
    assert feed_from_config(ExecutionConfig(), FakeReader(clock), ebook, REGISTRY)._refresh_s == 30


# ------------------------------------------------------------------------ properties (spec sez. 4.3 p.14)

money = st.integers(0, 10_000_000).map(lambda n: D(n) / 100)  # 0.00 .. 100000.00


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    available=money,
    hold=money,
    reserved_units=st.integers(0, 500_000),
    orders=st.lists(st.tuples(st.booleans(), st.sampled_from(["BUY", "SELL", "?"])), max_size=4),
)
def test_whatever_the_exchange_says_free_is_never_negative_and_never_above_available(
    available: Decimal, hold: Decimal, reserved_units: int, orders: list[tuple[bool, str]]
) -> None:
    _, book, _, _, clock = make_guard(quote="0", base="0", source="exchange")
    reserved = D(reserved_units) / 100
    if reserved > 0:
        book.restore_reservation(
            Reservation(client_order_id="mine", product_id="SOL-EUR", side="BUY", currency="EUR", amount=reserved)
        )
    reader = FakeReader(clock)
    reader.balances = {"EUR": (available, hold), "SOL": (D(0), D(0))}
    reader.orders = [Open(f"o{i}" if not ours else "mine", side=side) for i, (ours, side) in enumerate(orders)]
    feed = LiveBalanceFeed(reader, book, REGISTRY, refresh_s=30, clock=clock)

    assert asyncio.run(feed.refresh_once()) is SnapshotOutcome.ACCEPTED
    free = book.free("EUR")
    assert free >= 0 and book.reserved("EUR") == reserved  # a snapshot never touches the reservations
    assert free <= available  # free can never be larger than what the exchange says is available
    foreign_buy = any((not ours or reserved == 0) and side in ("BUY", "?") for ours, side in orders)
    credit = D(0) if foreign_buy else min(hold, reserved)
    assert free == max(available - (reserved - credit), 0)


class FeedMachine(RuleBasedStateMachine):
    """Guard + feed + a fake exchange under random sequences: orders, fills, snapshots, failures, time, races."""

    def __init__(self) -> None:
        super().__init__()
        self.guard, self.book, self.positions, _, self.clock = make_guard(
            quote="0", base="0", owned="5", source="exchange"
        )
        self.reader = FakeReader(self.clock, EUR=("50", "0"), SOL=("5", "0"))
        self.feed = LiveBalanceFeed(self.reader, self.book, REGISTRY, refresh_s=30, clock=self.clock)
        self.loop = asyncio.new_event_loop()
        self.n = 0
        self.open_orders: list[str] = []

    def teardown(self) -> None:
        self.loop.close()

    def _cid(self) -> str:
        self.n += 1
        return f"o{self.n}"

    def _refresh(self) -> SnapshotOutcome | None:
        return self.loop.run_until_complete(self.feed.refresh_once())

    @rule(avail_eur=money, hold_eur=money, avail_sol=money, hold_sol=money)
    def exchange_changes(self, avail_eur: Decimal, hold_eur: Decimal, avail_sol: Decimal, hold_sol: Decimal) -> None:
        self.reader.balances = {"EUR": (avail_eur, hold_eur), "SOL": (avail_sol, hold_sol)}

    @rule()
    def refresh(self) -> None:
        self.reader.fail = None
        before = {c: self.book.reserved(c) for c in ("EUR", "SOL")}
        assert self._refresh() is SnapshotOutcome.ACCEPTED
        assert {c: self.book.reserved(c) for c in ("EUR", "SOL")} == before
        for currency, (avail, _) in self.reader.balances.items():
            assert self.book.free(currency) <= avail  # never more than the exchange says is spendable
        assert self.feed.age_s() == 0.0

    @rule()
    def refresh_fails(self) -> None:
        self.reader.fail = ExchangeUnavailable("down")
        snapshot = (self.book.snapshot_at, self.book.epoch, {c: self.book.free(c) for c in ("EUR", "SOL")})
        assert self._refresh() is None
        assert (self.book.snapshot_at, self.book.epoch, {c: self.book.free(c) for c in ("EUR", "SOL")}) == snapshot
        self.reader.fail = None

    @rule(size_units=st.integers(1, 1500), price_units=st.integers(100, 20000))
    def refresh_racing_with_a_buy(self, size_units: int, price_units: int) -> None:
        cid = self._cid()
        self.reader.during_read = lambda: self._try_buy(cid, size_units, price_units)
        outcome = self._refresh()
        self.reader.during_read = None
        if self.book.reservation(cid) is not None:
            assert outcome is SnapshotOutcome.DISCARDED_EPOCH  # a local event during the read: the read is dropped

    def _try_buy(self, cid: str, size_units: int, price_units: int) -> None:
        size, price = D(size_units) / 1000, D(price_units) / 100
        age = self.book.snapshot_age_s()
        free_before = self.book.free("EUR")
        required = required_quote(price, size, F_COV, D("0.01"))
        try:
            ticket = self.guard.authorize(buy(cid, str(size), str(price)))
        except BalanceStale:
            assert age is None or age > BALANCE_MAX_AGE_S
            return
        except InsufficientCoverage:
            assert required > free_before
            return
        assert age is not None and age <= BALANCE_MAX_AGE_S  # no BUY on a book older than the limit
        assert ticket.reserved == required <= free_before  # nothing without cover
        self.open_orders.append(cid)

    @rule(size_units=st.integers(1, 1500), price_units=st.integers(100, 20000))
    def buy_now(self, size_units: int, price_units: int) -> None:
        self._try_buy(self._cid(), size_units, price_units)

    @precondition(lambda self: bool(self.open_orders))
    @rule(data=st.data())
    def order_ends(self, data: st.DataObject) -> None:
        cid = data.draw(st.sampled_from(self.open_orders))
        self.open_orders.remove(cid)
        total_before = self.book.total("EUR")
        self.book.release(cid)
        assert self.book.total("EUR") == total_before  # reserve/release never create or destroy money

    @rule(seconds=st.integers(0, 200))
    def time_passes(self, seconds: int) -> None:
        self.clock.advance(seconds)

    @invariant()
    def nothing_is_negative(self) -> None:
        for currency in ("EUR", "SOL"):
            assert self.book.free(currency) >= 0 and self.book.reserved(currency) >= 0
        assert all(r.amount >= 0 for r in self.book.open_reservations())

    @invariant()
    def staleness_is_exactly_the_guard_limit(self) -> None:
        age = self.book.snapshot_age_s()
        assert self.feed.is_stale() == (age is None or age > BALANCE_MAX_AGE_S)


TestFeedMachine = FeedMachine.TestCase
TestFeedMachine.settings = settings(
    max_examples=80, stateful_step_count=30, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
