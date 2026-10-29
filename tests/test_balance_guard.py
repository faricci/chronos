"""BalanceGuard: no order without full spot coverage (vincolo C11)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from modules.config import FallbackFees
from modules.execution.balance import AccountSnapshot
from modules.execution.balance_guard import (
    BALANCE_MAX_AGE_S,
    BalanceGuard,
    BalanceStale,
    CoverageBreached,
    CoverageTicket,
    InsufficientCoverage,
)
from modules.execution.orders import OrderSpec
from modules.execution.spot import SpotRegistry, SpotViolation, build_order_body
from modules.risk.costs import FeeProvider, coverage_fee_rate, required_quote
from tests.coverage_fixtures import KillSwitch, Positions, buy, make_guard, sell

D = Decimal
INC = {"EUR": D("0.01"), "SOL": D("0.001")}

# SOL-EUR, 0.024 SOL at 92.03: notional 2.20872; f_cov = 120 bps -> ceil(2.2352...) = 2.24, + 0.01 = 2.25
REQUIRED = D("2.25")


def test_required_quote_of_the_reference_order() -> None:
    f = coverage_fee_rate(FeeProvider(FallbackFees()).current, FallbackFees())
    assert f == D("0.012")
    assert required_quote(D("92.03"), D("0.024"), f, D("0.01")) == REQUIRED


# --------------------------------------------------------------------------------------------- BUY


def test_covered_buy_reserves_exactly_the_required_amount() -> None:
    guard, book, *_ = make_guard(quote="100.00")
    ticket = guard.authorize(buy())
    assert (ticket.reserved, ticket.currency) == (REQUIRED, "EUR")
    assert book.free("EUR") == D("100.00") - REQUIRED and book.reserved("EUR") == REQUIRED


def test_exactly_enough_passes_and_one_cent_less_is_refused() -> None:
    guard, *_ = make_guard(quote=str(REQUIRED))
    guard.authorize(buy())
    guard2, book2, *_ = make_guard(quote=str(REQUIRED - D("0.01")))
    with pytest.raises(InsufficientCoverage) as exc:
        guard2.authorize(buy())
    assert (exc.value.required, exc.value.available, exc.value.currency) == (REQUIRED, D("2.24"), "EUR")
    assert exc.value.reason == "insufficient_balance"
    assert book2.free("EUR") == D("2.24") and not book2.open_reservations()  # a refusal changes nothing


def test_a_refused_order_does_not_move_the_epoch() -> None:
    guard, book, *_ = make_guard(quote="1.00")
    before = book.epoch
    with pytest.raises(InsufficientCoverage):
        guard.authorize(buy())
    assert book.epoch == before


def test_two_buys_cannot_both_use_the_same_euro() -> None:
    guard, book, *_ = make_guard(quote="4.00")
    guard.authorize(buy("b1"))
    with pytest.raises(InsufficientCoverage):
        guard.authorize(buy("b2"))  # 4.00 - 2.25 = 1.75 left, 2.25 needed
    assert book.free("EUR") == D("1.75")


def test_released_reservation_makes_room_again() -> None:
    guard, book, *_ = make_guard(quote="4.00")
    guard.authorize(buy("b1"))
    book.release("b1")
    guard.authorize(buy("b2"))


def test_the_tier_fee_above_the_floor_raises_the_reservation() -> None:
    guard, book, _, _, clock = make_guard(quote="100.00")

    async def fetch() -> tuple[float, float]:
        return (100.0, 200.0)  # 200 bps taker

    provider = FeeProvider(FallbackFees(), fetch, clock=clock)
    guard2 = BalanceGuard(book, Positions(), SpotRegistry({"SOL-EUR": guard._products.meta("SOL-EUR")}), provider,
                          FallbackFees(), KillSwitch())
    import asyncio

    asyncio.run(provider.refresh())
    ticket = guard2.authorize(buy())
    assert ticket.reserved == required_quote(D("92.03"), D("0.024"), D("0.02"), D("0.01")) > REQUIRED


def test_buy_below_the_reference_is_never_cheaper_than_fallback_fee() -> None:
    """Even if the account fee read is lower than the fallback, the reservation uses the higher one."""
    low = FallbackFees(maker_bps=10, taker_bps=20)
    f = coverage_fee_rate(FeeProvider(low).current, low)
    assert f == D("0.012")  # floor of 120 bps


# ------------------------------------------------------------------------------------------- SELL


def test_covered_sell_reserves_base() -> None:
    guard, book, *_ = make_guard(base="0.1", owned="0.1")
    ticket = guard.authorize(sell(size="0.024"))
    assert (ticket.currency, ticket.reserved) == ("SOL", D("0.024"))
    assert book.free("SOL") == D("0.076") and book.reserved_sell_base("SOL-EUR") == D("0.024")


def test_cannot_sell_what_chronos_does_not_own_even_if_the_account_has_it() -> None:
    """The conto has 5 SOL of the user; the ledger says Chronos owns 0.1: short/foreign sales are impossible."""
    guard, *_ = make_guard(base="5", owned="0.1")
    guard.authorize(sell("s1", size="0.1"))
    with pytest.raises(InsufficientCoverage):
        guard.authorize(sell("s2", size="0.001"))  # the whole 0.1 is already committed


def test_cannot_sell_more_than_the_ledger_position() -> None:
    guard, *_ = make_guard(base="5", owned="0.1")
    with pytest.raises(InsufficientCoverage) as exc:
        guard.authorize(sell(size="0.101"))
    assert exc.value.currency == "SOL" and exc.value.available == D("0.100")


def test_cannot_sell_more_than_the_free_balance() -> None:
    guard, *_ = make_guard(base="0.05", owned="0.1")  # ledger says 0.1 but the account holds 0.05
    with pytest.raises(InsufficientCoverage):
        guard.authorize(sell(size="0.06"))


def test_flat_position_cannot_sell_anything() -> None:
    guard, *_ = make_guard(base="3", owned="0")
    with pytest.raises(InsufficientCoverage) as exc:
        guard.authorize(sell(size="0.001"))
    assert exc.value.available == 0


def test_second_sell_while_the_bracket_is_still_reserved_is_refused() -> None:
    """submit_exit must cancel and wait for confirmation first: no optimistic release (Q2)."""
    guard, book, *_ = make_guard(base="0.1", owned="0.1")
    guard.authorize(sell("bracket", size="0.1", kind="bracket", price="108.96", stop="83.57"))
    with pytest.raises(InsufficientCoverage):
        guard.authorize(sell("exit", size="0.1"))
    book.release("bracket")  # exchange confirmed the cancel
    guard.authorize(sell("exit", size="0.1"))


# ------------------------------------------------------------------------- staleness and breach


def exchange_guard() -> tuple[BalanceGuard, ...]:
    guard, book, positions, ks, clock = make_guard(quote="0", base="0", owned="0.1", source="exchange")
    return guard, book, positions, ks, clock  # type: ignore[return-value]


def feed(book, clock, eur: str = "100", sol: str = "0.1") -> None:  # type: ignore[no-untyped-def]
    snap = AccountSnapshot(balances={"EUR": (D(eur), D(0)), "SOL": (D(sol), D(0))}, taken_at=clock.now)
    book.apply_snapshot(snap, book.epoch, INC)


def test_live_buy_is_refused_before_the_first_snapshot() -> None:
    guard, book, *_ = exchange_guard()
    with pytest.raises(BalanceStale) as exc:
        guard.authorize(buy())
    assert exc.value.age_s is None and exc.value.reason == "balance_stale"


def test_live_buy_fails_closed_after_120_seconds() -> None:
    guard, book, _, _, clock = exchange_guard()
    feed(book, clock)
    guard.authorize(buy("b1"))
    clock.advance(BALANCE_MAX_AGE_S)  # exactly at the limit: still trusted
    guard.authorize(buy("b2"))
    clock.advance(1)
    with pytest.raises(BalanceStale):
        guard.authorize(buy("b3"))
    feed(book, clock)  # a fresh snapshot re-opens the door
    guard.authorize(buy("b4"))


def test_sell_is_never_blocked_by_an_old_snapshot() -> None:
    guard, book, _, _, clock = exchange_guard()
    feed(book, clock)
    clock.advance(3600)
    guard.authorize(sell(size="0.1"))  # emergency exits must work with a dead feed


def test_virtual_book_is_never_stale() -> None:
    guard, _, _, _, clock = make_guard(quote="100", source="virtual")
    clock.advance(10**6)
    guard.authorize(buy())


def test_coverage_breach_blocks_buys_but_not_sells() -> None:
    guard, _, _, ks, _ = make_guard(quote="100", base="0.1", owned="0.1")
    ks.breached.add("champion")
    with pytest.raises(CoverageBreached) as exc:
        guard.authorize(buy())
    assert exc.value.reason == "kill_switch"
    guard.authorize(sell(size="0.1"))


# ------------------------------------------------------------------------------------ the ticket


def test_ticket_is_single_use() -> None:
    guard, *_ = make_guard()
    ticket = guard.authorize(buy())
    spec = ticket.consume()
    assert build_order_body(spec)["client_order_id"] == "b1"
    assert ticket.consumed
    with pytest.raises(SpotViolation, match="already used"):
        ticket.consume()


def test_a_ticket_cannot_be_forged() -> None:
    with pytest.raises(SpotViolation, match="only be issued"):
        CoverageTicket(buy(), D("1"), "EUR")
    with pytest.raises(SpotViolation):
        CoverageTicket(buy(), D("1"), "EUR", _token=object())


# ---------------------------------------------------------------------------------- spot shape


def test_unknown_product_is_a_violation_not_a_skip() -> None:
    guard, *_ = make_guard()
    spec = buy().model_copy(update={"product_id": "BTC-EUR"})
    with pytest.raises(SpotViolation):
        guard.authorize(spec)


@pytest.mark.parametrize(
    "spec",
    [
        buy(size="0.0245"),  # not a multiple of base_increment
        buy(price="92.035"),  # not a multiple of quote_increment
    ],
)
def test_orders_off_the_increment_grid_are_refused(spec: OrderSpec) -> None:
    guard, book, *_ = make_guard()
    with pytest.raises(SpotViolation):
        guard.authorize(spec)
    assert not book.open_reservations()


def test_buy_market_is_a_violation_even_with_plenty_of_money() -> None:
    guard, *_ = make_guard(quote="1000000")
    spec = OrderSpec(client_order_id="x", book="champion", product_id="SOL-EUR", side="BUY", kind="market",
                     base_size=D("1"))
    with pytest.raises(SpotViolation):
        guard.authorize(spec)


def test_duplicate_client_order_id_is_not_reserved_twice() -> None:
    guard, book, *_ = make_guard(quote="100")
    guard.authorize(buy("same"))
    with pytest.raises(ValueError, match="already reserved"):
        guard.authorize(buy("same"))
    assert book.reserved("EUR") == REQUIRED


# ----------------------------------------------------------------------------------- no off switch


def test_the_guard_cannot_be_built_without_its_checks() -> None:
    guard, book, positions, ks, clock = make_guard()
    with pytest.raises(TypeError):
        BalanceGuard(book, positions, guard._products, FeeProvider(FallbackFees()), FallbackFees())  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        BalanceGuard()  # type: ignore[call-arg]
