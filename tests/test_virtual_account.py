"""VirtualAccount: the dry-run / backtest account composes the live BalanceBook (spec sez. 5.3 punto 5, vincolo C11)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from modules.config import FallbackFees
from modules.dryrun.virtual_account import VirtualAccount
from modules.execution.balance import AccountSnapshot, BalanceBook, Reservation
from modules.execution.balance_guard import BalanceGuard, CoverageRefused
from modules.execution.rounding import floor_to
from modules.execution.spot import SpotRegistry
from modules.risk.costs import FeeProvider
from tests.coverage_fixtures import SOL, Clock, KillSwitch, buy, sell

D = Decimal
B = "champion"
P = "SOL-EUR"
F_COV = D("0.012")  # the fallback taker fee (120 bps) is also the coverage floor
INC = {"EUR": D("0.01"), "SOL": D("0.001")}
TOL = D("1e-12")


class Held:
    """Stand-in for the ledger's PositionCache, fed by the test (or by the VirtualAccount's own bookkeeping)."""

    class _View:
        def __init__(self, qty: Decimal) -> None:
            self.base_qty = qty

    def __init__(self, qty: Callable[[str], Decimal] | None = None) -> None:
        self.qty = D(0)
        self._source = qty

    def view(self, product_id: str) -> Any:
        return self._View(self.qty if self._source is None else self._source(product_id))


def make_guard(book: BalanceBook, clock: Clock, held: Held) -> BalanceGuard:
    fees = FallbackFees()
    return BalanceGuard(book, held, SpotRegistry({P: SOL}), FeeProvider(fees, clock=clock), fees, KillSwitch())


def make_account(start: str = "1000.00") -> tuple[VirtualAccount, BalanceGuard, Clock]:
    clock = Clock()
    account = VirtualAccount(B, D(start), clock)
    return account, make_guard(account.balances, clock, Held(account.position_qty)), clock


def fill(account: VirtualAccount, cid: str, price: str, size: str, fee: str, trade_id: str | None = None) -> Any:
    return account.record_fill(
        cid, product_id=P, base_currency="SOL", price=D(price), size=D(size), fee=D(fee), trade_id=trade_id
    )


# ------------------------------------------------------------------------------------------ composition


def test_the_account_composes_a_virtual_balance_book() -> None:
    account, _, _ = make_account("100.00")
    assert isinstance(account.balances, BalanceBook)
    assert account.balances.source == "virtual" and account.balances.book == B
    assert account.balances.free("EUR") == D("100.00") and account.balances.total("SOL") == 0
    assert account.balances.snapshot_age_s() == 0  # a virtual book is always fresh
    assert account.fees_paid == 0 and account.realized_pnl == 0


def test_a_non_positive_starting_quote_is_refused() -> None:
    with pytest.raises(ValueError):
        VirtualAccount(B, D(0), Clock())


def test_the_quote_currency_is_configurable() -> None:
    account = VirtualAccount(B, D("50"), Clock(), quote_currency="USD")
    assert account.balances.free("USD") == 50 and account.balances.free("EUR") == 0


# ------------------------------------------------------------------------------------------ fills and PnL


def test_buy_then_sell_round_trip_with_fees() -> None:
    account, guard, _ = make_account("100.00")
    guard.authorize(buy("b1", "0.024", "92.03"))
    assert fill(account, "b1", "92.03", "0.024", "0.02") == []
    account.balances.release("b1")
    assert account.balances.free("EUR") == D("100.00") - D("92.03") * D("0.024") - D("0.02")
    assert account.balances.free("SOL") == D("0.024") and account.position_qty(P) == D("0.024")
    assert account.avg_cost(P) == D("92.03")
    assert (account.fees_paid, account.realized_pnl) == (D("0.02"), 0)

    guard.authorize(sell("s1", "0.024", kind="market"))
    assert fill(account, "s1", "100.00", "0.024", "0.03") == []
    account.balances.release("s1")
    assert account.realized_pnl == (D("100.00") - D("92.03")) * D("0.024")  # gross of fees
    assert account.fees_paid == D("0.05")
    assert account.net_realized_pnl == account.realized_pnl - D("0.05")
    assert account.position_qty(P) == 0 and account.avg_cost(P) is None
    assert account.balances.total("SOL") == 0


def test_average_cost_across_two_buys_and_a_partial_sell() -> None:
    account, guard, _ = make_account("1000.00")
    for cid, price in (("b1", "100.00"), ("b2", "110.00")):
        guard.authorize(buy(cid, "1.000", price))
        assert fill(account, cid, price, "1.000", "0") == []
        account.balances.release(cid)
    assert account.avg_cost(P) == D("105")
    guard.authorize(sell("s1", "0.500", kind="market"))
    assert fill(account, "s1", "120.00", "0.500", "0") == []
    assert account.realized_pnl == D("7.5")  # 0.5 x (120 - 105)
    assert account.avg_cost(P) == D("105")  # the average does not move on a sale


def test_equity_is_quote_plus_base_at_the_mark() -> None:
    account, guard, _ = make_account("100.00")
    assert account.equity({}, {P: "SOL"}) == D("100.00")  # nothing held: no mark needed
    guard.authorize(buy("b1", "0.5", "50.00"))
    assert fill(account, "b1", "50.00", "0.5", "0.10") == []
    # reserved remainder still counts as ours
    assert account.equity({P: D("60.00")}, {P: "SOL"}) == D("100.00") - D("25.00") - D("0.10") + D("0.5") * D("60.00")


def test_equity_without_a_mark_for_a_held_base_is_an_error() -> None:
    account, guard, _ = make_account("100.00")
    guard.authorize(buy("b1", "0.1", "50.00"))
    fill(account, "b1", "50.00", "0.1", "0")
    with pytest.raises(KeyError):
        account.equity({}, {P: "SOL"})
    with pytest.raises(KeyError):
        account.unrealized_pnl({})


def test_a_repeated_trade_id_changes_nothing() -> None:
    account, guard, _ = make_account("100.00")
    guard.authorize(buy("b1", "0.5", "50.00"))
    fill(account, "b1", "50.00", "0.2", "0.10", trade_id="t1")
    snapshot = (account.balances.free("EUR"), account.balances.free("SOL"), account.fees_paid, account.position_qty(P))
    assert fill(account, "b1", "50.00", "0.2", "0.10", trade_id="t1") == []
    assert (
        account.balances.free("EUR"),
        account.balances.free("SOL"),
        account.fees_paid,
        account.position_qty(P),
    ) == snapshot


def test_a_fill_for_an_unknown_order_moves_nothing_and_alarms() -> None:
    account, _, _ = make_account("100.00")
    alarms = fill(account, "ghost", "50.00", "0.2", "0.10")
    assert [a.kind for a in alarms] == ["reservation_overdraw"]
    assert account.balances.free("EUR") == D("100.00") and account.fees_paid == 0 and account.position_qty(P) == 0


def test_selling_more_than_the_recorded_position_is_a_bug() -> None:
    account, _, _ = make_account("100.00")
    # a SELL reservation for base the account never bought (only possible if the ledger and the account disagree)
    account.balances.restore_reservation(
        Reservation(client_order_id="s1", product_id=P, side="SELL", currency="SOL", amount=D("1"))
    )
    with pytest.raises(ValueError, match="exceeds"):
        fill(account, "s1", "50.00", "1", "0")


# ------------------------------------------------------------------------------------------ invariants


@dataclass
class World:
    account: VirtualAccount
    guard: BalanceGuard
    orders: dict[str, tuple[str, Decimal, Decimal]]  # cid -> (side, limit price or 0, remaining size)
    n: int = 0


def check_invariants(w: World, mark: Decimal) -> None:
    book = w.account.balances
    for currency in ("EUR", "SOL"):
        assert book.free(currency) >= 0 and book.reserved(currency) >= 0, currency
    assert all(r.amount >= 0 for r in book.open_reservations())
    assert book.total("SOL") == w.account.position_qty(P)
    gap = (
        w.account.equity({P: mark}, {P: "SOL"})
        + w.account.fees_paid
        - (w.account.starting_quote + w.account.realized_pnl + w.account.unrealized_pnl({P: mark}))
    )
    assert abs(gap) <= TOL, gap


def step(w: World, op: str, a: int, b: int, c: int) -> None:
    book = w.account.balances
    totals = {cur: book.total(cur) for cur in ("EUR", "SOL")}
    if op == "buy":
        w.n += 1
        cid, size, price = f"b{w.n}", D(a % 500 + 1) / 1000, D(5000 + b % 10001) / 100
        try:
            w.guard.authorize(buy(cid, str(size), str(price)))
        except CoverageRefused:
            return
        w.orders[cid] = ("BUY", price, size)
        assert {cur: book.total(cur) for cur in totals} == totals  # reserve conserves free + reserved
    elif op == "sell":
        w.n += 1
        sellable = min(w.account.position_qty(P) - book.reserved_sell_base(P), book.free("SOL"))
        size = floor_to(sellable * D(a % 100 + 1) / 100, INC["SOL"])
        if size <= 0:
            return
        cid = f"s{w.n}"
        w.guard.authorize(sell(cid, str(size), kind="market"))
        w.orders[cid] = ("SELL", D(0), size)
        assert {cur: book.total(cur) for cur in totals} == totals
    elif op == "fill" and w.orders:
        cid = sorted(w.orders)[a % len(w.orders)]
        side, limit, remaining = w.orders[cid]
        if remaining <= 0:
            return  # fully filled, waiting for its terminal release
        size = remaining * D(b % 100 + 1) / 100
        price = limit * (1 - D(c % 100) / 10_000) if side == "BUY" else D(4000 + c * 7) / 100
        fee = price * size * F_COV * D((c // 100) % 101) / 100
        alarms = w.account.record_fill(cid, product_id=P, base_currency="SOL", price=price, size=size, fee=fee)
        assert alarms == []
        w.orders[cid] = (side, limit, remaining - size)
    elif op == "release" and w.orders:
        cid = sorted(w.orders)[a % len(w.orders)]
        book.release(cid)
        del w.orders[cid]
        assert {cur: book.total(cur) for cur in totals} == totals  # release conserves free + reserved


OPS = st.tuples(
    st.sampled_from(["buy", "buy", "fill", "fill", "sell", "release"]),
    st.integers(0, 10_000),
    st.integers(0, 10_000),
    st.integers(0, 10_000),
)


@settings(max_examples=250, deadline=None)
@given(st.lists(OPS, max_size=60), st.integers(1_000, 30_000))
def test_accounting_identity_and_non_negative_balances(ops: list[tuple[str, int, int, int]], mark_cents: int) -> None:
    """quote + sum(base x mark) + fees == starting + realized + unrealized, within 1e-12, after every operation."""
    account, guard, _ = make_account("1000.00")
    w = World(account, guard, {})
    for op, a, b, c in ops:
        step(w, op, a, b, c)
        check_invariants(w, D(mark_cents) / 100)


@settings(max_examples=100, deadline=None)
@given(st.lists(OPS, min_size=1, max_size=40))
def test_reserve_and_release_never_change_free_plus_reserved(ops: list[tuple[str, int, int, int]]) -> None:
    account, guard, _ = make_account("200.00")
    w = World(account, guard, {})
    for op, a, b, c in ops:
        step(w, op, a, b, c)  # step() asserts conservation for authorize and release
    for cid in list(w.orders):
        account.balances.release(cid)
    assert account.balances.reserved("EUR") == 0 and account.balances.reserved("SOL") == 0


# ------------------------------------------------------------------------------------------ live vs dry


INITIAL = {"EUR": D("100.00"), "SOL": D("0")}


@dataclass
class Env:
    name: str
    book: BalanceBook
    guard: BalanceGuard
    held: Held
    do_fill: Callable[[str, str, str, str], None]


def exchange_env() -> Env:
    clock = Clock()
    book = BalanceBook(B, "exchange", clock, None)
    snapshot = AccountSnapshot(balances={cur: (amount, D(0)) for cur, amount in INITIAL.items()}, taken_at=clock())
    outcome, alarms = book.apply_snapshot(snapshot, book.epoch, INC)
    assert outcome.value == "accepted" and alarms == []
    held = Held()

    def do_fill(cid: str, price: str, size: str, fee: str) -> None:
        assert (
            book.apply_fill(cid, base_currency="SOL", quote_currency="EUR", price=D(price), size=D(size), fee=D(fee))
            == []
        )

    return Env("exchange", book, make_guard(book, clock, held), held, do_fill)


def virtual_env() -> Env:
    clock = Clock()
    account = VirtualAccount(B, INITIAL["EUR"], clock)
    held = Held()

    def do_fill(cid: str, price: str, size: str, fee: str) -> None:
        assert fill(account, cid, price, size, fee) == []

    return Env("virtual", account.balances, make_guard(account.balances, clock, held), held, do_fill)


def run_sequence(env: Env) -> list[tuple[str, str, tuple[Decimal, ...]]]:
    """The same OrderSpecs, fills and terminal releases against one environment."""
    log: list[tuple[str, str, tuple[Decimal, ...]]] = []

    def state() -> tuple[Decimal, ...]:
        b = env.book
        return (b.free("EUR"), b.reserved("EUR"), b.free("SOL"), b.reserved("SOL"))

    def authorize(label: str, spec: Any) -> None:
        try:
            env.guard.authorize(spec)
            outcome = "ok"
        except CoverageRefused as e:
            outcome = f"{type(e).__name__}:{e.reason}"
        log.append((label, outcome, state()))

    authorize("buy b1", buy("b1", "0.024", "92.03"))
    authorize("buy b2 too big", buy("b2", "5", "92.03"))
    authorize("sell with nothing", sell("s0", "0.024", kind="market"))
    env.do_fill("b1", "92.03", "0.024", "0.02")
    log.append(("fill b1", "applied", state()))
    env.book.release("b1")
    env.held.qty = D("0.024")
    log.append(("terminal b1", "released", state()))
    authorize("bracket s1", sell("s1", "0.024", kind="bracket", price="108.96", stop="83.57"))
    authorize("market s2 on reserved base", sell("s2", "0.024", kind="market"))
    env.do_fill("s1", "108.96", "0.024", "0.03")
    env.held.qty = D(0)
    env.book.release("s1")
    log.append(("fill+terminal s1", "applied", state()))
    authorize("sell flat", sell("s3", "0.024", kind="market"))
    authorize("buy b3", buy("b3", "0.024", "92.03"))
    authorize("buy b4 large", buy("b4", "0.5", "150.00"))
    return log


def test_live_and_dry_guard_parity() -> None:
    """Same OrderSpec sequence, same fills: an 'exchange' BalanceBook with a fresh snapshot and a VirtualAccount
    give the same accept/refuse outcomes and the same reservations at every step (T17)."""
    live, dry = run_sequence(exchange_env()), run_sequence(virtual_env())
    assert live == dry
    outcomes = {label: outcome for label, outcome, _ in dry}
    assert outcomes["buy b1"] == "ok" and outcomes["buy b2 too big"] == "InsufficientCoverage:insufficient_balance"
    assert outcomes["sell with nothing"] == "InsufficientCoverage:insufficient_balance"
    assert outcomes["bracket s1"] == "ok" and outcomes["market s2 on reserved base"].startswith("InsufficientCoverage")
    assert outcomes["buy b3"] == "ok"
    assert any(state[1] > 0 for _, _, state in dry)  # reservations really were compared


def test_the_only_difference_between_live_and_dry_is_the_snapshot_age_rule() -> None:
    clock_a, clock_b = Clock(), Clock()
    live_book = BalanceBook(B, "exchange", clock_a, None)
    live_book.apply_snapshot(AccountSnapshot(balances={"EUR": (D("100"), D(0))}, taken_at=clock_a()), 0, INC)
    dry = VirtualAccount(B, D("100"), clock_b)
    live_guard, dry_guard = make_guard(live_book, clock_a, Held()), make_guard(dry.balances, clock_b, Held())
    clock_a.advance(121)
    clock_b.advance(121)
    with pytest.raises(CoverageRefused) as stale:
        live_guard.authorize(buy("b1"))
    assert stale.value.reason == "balance_stale"
    dry_guard.authorize(buy("b1"))  # the virtual book is its own source of truth
