"""Property-based proofs of the C11 invariants (spec risoluzione Q1-Q6, test T1-T3, T5-T7).

- T1  no ticket is ever issued with required > available; every refusal had a reason
- T2  free and every reservation are never negative
- T3  free + reserved is conserved by reserve/release; fills change it by exactly the fill deltas
- T5  max_base_for_quote is the exact inverse of required_quote
- T6  the worst loss of an order is bounded by its reservation
- T7  an accepted snapshot never makes free larger than what is truly spendable
"""

from __future__ import annotations

from decimal import Decimal

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from modules.config import FallbackFees
from modules.execution.balance import AccountSnapshot, BalanceBook, Reservation, SnapshotOutcome
from modules.execution.balance_guard import InsufficientCoverage
from modules.execution.rounding import is_multiple_of
from modules.risk.costs import FeeProvider, coverage_fee_rate, max_base_for_quote, required_quote
from tests.coverage_fixtures import Clock, make_guard

D = Decimal
F_COV = coverage_fee_rate(FeeProvider(FallbackFees()).current, FallbackFees())  # 0.012


# ------------------------------------------------------------------------------ T1, T2, T3


class CoverageMachine(RuleBasedStateMachine):
    """Random sequences of authorizations, fills (partial, price-improved, with fees), cancels and sells."""

    def __init__(self) -> None:
        super().__init__()
        self.guard, self.book, self.positions, _, _ = make_guard(quote="60.00", base="0", owned="0")
        self.n = 0
        self.expected = {"EUR": D("60.00"), "SOL": D(0)}  # exact totals if the book is right
        self.orders: dict[str, dict[str, Decimal | str]] = {}

    def _cid(self) -> str:
        self.n += 1
        return f"o{self.n}"

    # --- authorizations

    @rule(size_units=st.integers(1, 3000), price_units=st.integers(100, 30000))
    def authorize_buy(self, size_units: int, price_units: int) -> None:
        from tests.coverage_fixtures import buy

        size, price = D(size_units) / 1000, D(price_units) / 100
        cid = self._cid()
        free_before = self.book.free("EUR")
        required = required_quote(price, size, F_COV, D("0.01"))
        try:
            ticket = self.guard.authorize(buy(cid, str(size), str(price)))
        except InsufficientCoverage:
            assert required > free_before  # T1: a refusal is always justified
            return
        assert required <= free_before  # T1: nothing is issued without cover
        assert ticket.reserved == required
        self.orders[cid] = {"side": "BUY", "limit": price, "left": size}

    @rule(size_units=st.integers(1, 3000))
    def authorize_sell(self, size_units: int) -> None:
        from tests.coverage_fixtures import sell

        size = D(size_units) / 1000
        cid = self._cid()
        owned = self.positions.qty.get("SOL-EUR", D(0))
        sellable = min(owned - self.book.reserved_sell_base("SOL-EUR"), self.book.free("SOL"))
        try:
            self.guard.authorize(sell(cid, str(size)))
        except InsufficientCoverage:
            assert size > sellable
            return
        assert size <= sellable
        self.orders[cid] = {"side": "SELL", "left": size}

    # --- exchange events

    def _open(self, side: str) -> list[str]:
        return [c for c, o in self.orders.items() if o["side"] == side and o["left"] > 0]  # type: ignore[operator]

    @precondition(lambda self: bool(self._open("BUY")))
    @rule(data=st.data(), take=st.integers(1, 100), improve=st.integers(0, 50), fee_share=st.integers(0, 100))
    def fill_buy(self, data: st.DataObject, take: int, improve: int, fee_share: int) -> None:
        cid = data.draw(st.sampled_from(self._open("BUY")))
        o = self.orders[cid]
        left, limit = o["left"], o["limit"]
        assert isinstance(left, Decimal) and isinstance(limit, Decimal)
        q = max((left * take / 100).quantize(D("0.001")), D("0.001"))
        q = min(q, left)
        p = limit * (100 - improve) / 100  # fills at or below the limit
        fee = F_COV * p * q * fee_share / 100  # never above the reservation rate
        alarms = self.book.apply_fill(
            cid, base_currency="SOL", quote_currency="EUR", price=p, size=q, fee=fee, trade_id=f"{cid}-{self.n}-{left}"
        )
        assert alarms == []  # T1/H1: a fill within limit and fee rate never exceeds its reservation
        o["left"] = left - q
        self.expected["EUR"] -= p * q + fee
        self.expected["SOL"] += q
        self.positions.qty["SOL-EUR"] = self.positions.qty.get("SOL-EUR", D(0)) + q

    @precondition(lambda self: bool(self._open("SELL")))
    @rule(data=st.data(), take=st.integers(1, 100), price_units=st.integers(100, 30000), fee_share=st.integers(0, 100))
    def fill_sell(self, data: st.DataObject, take: int, price_units: int, fee_share: int) -> None:
        cid = data.draw(st.sampled_from(self._open("SELL")))
        o = self.orders[cid]
        left = o["left"]
        assert isinstance(left, Decimal)
        q = min(max((left * take / 100).quantize(D("0.001")), D("0.001")), left)
        p = D(price_units) / 100
        fee = F_COV * p * q * fee_share / 100
        alarms = self.book.apply_fill(
            cid, base_currency="SOL", quote_currency="EUR", price=p, size=q, fee=fee, trade_id=f"{cid}-{self.n}-{left}"
        )
        assert alarms == []
        o["left"] = left - q
        self.expected["EUR"] += p * q - fee
        self.expected["SOL"] -= q
        self.positions.qty["SOL-EUR"] -= q

    @precondition(lambda self: bool(self.orders))
    @rule(data=st.data())
    def terminal(self, data: st.DataObject) -> None:
        cid = data.draw(st.sampled_from(sorted(self.orders)))
        total_before = {c: self.book.total(c) for c in ("EUR", "SOL")}
        self.book.release(cid)
        del self.orders[cid]
        assert {c: self.book.total(c) for c in ("EUR", "SOL")} == total_before  # T3: release keeps the total

    # --- invariants checked after every step

    @invariant()
    def never_negative(self) -> None:  # T2
        for c in ("EUR", "SOL"):
            assert self.book.free(c) >= 0
            assert self.book.reserved(c) >= 0
        assert all(r.amount >= 0 for r in self.book.open_reservations())

    @invariant()
    def conservation(self) -> None:  # T3
        for c in ("EUR", "SOL"):
            assert self.book.total(c) == self.expected[c]
            in_orders = sum((r.amount for r in self.book.open_reservations() if r.currency == c), D(0))
            assert self.book.reserved(c) == in_orders


TestCoverageMachine = CoverageMachine.TestCase
TestCoverageMachine.settings = settings(
    max_examples=150, stateful_step_count=40, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)


# ----------------------------------------------------------------------------------------- T5

QUOTES = st.decimals(min_value=D("0"), max_value=D("1000000"), places=2)
PRICES = st.decimals(min_value=D("0.0001"), max_value=D("100000"), places=4)
BASE_INCS = st.sampled_from([D("0.00000001"), D("0.0001"), D("0.001"), D("1")])
QUOTE_INCS = st.sampled_from([D("0.01"), D("0.0001"), D("1")])
FEES = st.decimals(min_value=D("0.012"), max_value=D("0.05"), places=4)


@given(q=QUOTES, price=PRICES, fee=FEES, qinc=QUOTE_INCS, binc=BASE_INCS)
def test_max_base_for_quote_is_the_exact_inverse(
    q: Decimal, price: Decimal, fee: Decimal, qinc: Decimal, binc: Decimal
) -> None:
    size = max_base_for_quote(q, price, fee, qinc, binc)
    assert size >= 0 and is_multiple_of(size, binc)
    if size > 0:
        assert required_quote(price, size, fee, qinc) <= q  # fits
    assert required_quote(price, size + binc, fee, qinc) > q  # and is the largest that fits


# ----------------------------------------------------------------------------------------- T6


@given(
    size=st.decimals(min_value=D("0.001"), max_value=D("100"), places=3),
    limit=PRICES,
    parts=st.lists(st.tuples(st.integers(1, 100), st.integers(0, 99), st.integers(0, 100)), min_size=1, max_size=6),
    exit_price=st.decimals(min_value=D("0"), max_value=D("200000"), places=4),
)
def test_max_loss_is_bounded_by_the_reservation(size, limit, parts, exit_price) -> None:  # type: ignore[no-untyped-def]
    reserved = required_quote(limit, size, F_COV, D("0.01"))
    left, spent, bought = size, D(0), D(0)
    for take, improve, fee_share in parts:
        q = min(max(left * take / 100, D("0.000001")), left)
        if q <= 0:
            break
        p = limit * (100 - improve) / 100  # fills at or below the limit
        spent += p * q + F_COV * p * q * fee_share / 100  # fee never above the reservation rate
        bought += q
        left -= q
    proceeds = exit_price * bought * (1 - F_COV)  # any exit price >= 0, any fee up to f_cov
    assert spent <= reserved  # nothing can cost more than was reserved
    assert spent - proceeds <= reserved  # so the loss on the order is at most its reservation


# ----------------------------------------------------------------------------------------- T7


@given(
    total=st.decimals(min_value=D("0"), max_value=D("10000"), places=2),
    reserved=st.lists(st.decimals(min_value=D("0.01"), max_value=D("500"), places=2), max_size=5),
    hold_share=st.lists(st.integers(0, 100), min_size=5, max_size=5),
    foreign_hold=st.decimals(min_value=D("0"), max_value=D("500"), places=2),
)
def test_snapshot_never_overstates_what_can_be_spent(total, reserved, hold_share, foreign_hold) -> None:  # type: ignore[no-untyped-def]
    """Exchange model: our orders hold between 0% and 100% of their reservation, foreign orders hold more."""
    clock = Clock()
    book = BalanceBook("champion", "exchange", clock)
    holds = D(0)
    for i, amount in enumerate(reserved):
        book.restore_reservation(Reservation(client_order_id=f"o{i}", product_id="SOL-EUR", side="BUY",
                                             currency="EUR", amount=amount))
        holds += amount * hold_share[i] / 100
    foreign = foreign_hold if foreign_hold > 0 else D(0)
    available = total - holds - foreign
    if available < 0:
        return  # the exchange cannot have more on hold than it owns
    snap = AccountSnapshot(
        balances={"EUR": (available, holds + foreign)},
        foreign_order_currencies=frozenset({"EUR"}) if foreign > 0 else frozenset(),
        taken_at=clock.now,
    )
    outcome, _ = book.apply_snapshot(snap, book.epoch, {"EUR": D("0.01")})
    assert outcome is SnapshotOutcome.ACCEPTED
    truly_spendable = total - foreign - sum(reserved, D(0))  # money not owed to our own orders nor to foreign holds
    assert book.free("EUR") >= 0
    assert book.free("EUR") <= max(truly_spendable, D(0))
