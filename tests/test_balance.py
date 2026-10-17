"""BalanceBook: reservations, fills, releases, snapshots (spec sez. 4.1.6, PARTE A Q3)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from modules.execution.balance import AccountSnapshot, BalanceBook, Reservation, SnapshotOutcome
from tests.coverage_fixtures import T0, Clock

D = Decimal
INC = {"EUR": D("0.01"), "SOL": D("0.001")}


def res(cid: str, side: str, currency: str, amount: str, product: str = "SOL-EUR") -> Reservation:
    return Reservation(client_order_id=cid, product_id=product, side=side, currency=currency, amount=D(amount))  # type: ignore[arg-type]


def virtual(**free: str) -> BalanceBook:
    return BalanceBook("champion", "virtual", Clock(), {k: D(v) for k, v in free.items()})


def exchange(**free: str) -> tuple[BalanceBook, Clock]:
    clock = Clock()
    return BalanceBook("champion", "exchange", clock, {k: D(v) for k, v in free.items()}), clock


def fill(book: BalanceBook, cid: str, price: str, size: str, fee: str = "0", trade_id: str | None = None) -> list:
    return book.apply_fill(
        cid, base_currency="SOL", quote_currency="EUR", price=D(price), size=D(size), fee=D(fee), trade_id=trade_id
    )


# --------------------------------------------------------------------------------- reserve / release


def test_reserve_moves_money_from_free_to_reserved_and_total_is_unchanged() -> None:
    b = virtual(EUR="100")
    b._reserve(res("o1", "BUY", "EUR", "30"))
    assert (b.free("EUR"), b.reserved("EUR"), b.total("EUR")) == (D(70), D(30), D(100))


def test_cannot_reserve_more_than_free() -> None:
    b = virtual(EUR="10")
    with pytest.raises(ValueError, match="only 10 free"):
        b._reserve(res("o1", "BUY", "EUR", "10.01"))
    assert b.free("EUR") == 10 and not b.open_reservations()


def test_two_orders_cannot_spend_the_same_money() -> None:
    b = virtual(EUR="10")
    b._reserve(res("o1", "BUY", "EUR", "6"))
    with pytest.raises(ValueError):
        b._reserve(res("o2", "BUY", "EUR", "6"))


def test_duplicate_id_and_non_positive_amount_are_rejected() -> None:
    b = virtual(EUR="100")
    b._reserve(res("o1", "BUY", "EUR", "5"))
    with pytest.raises(ValueError, match="already reserved"):
        b._reserve(res("o1", "BUY", "EUR", "5"))
    with pytest.raises(ValueError, match="positive"):
        b._reserve(res("o2", "BUY", "EUR", "0"))


def test_release_returns_the_remainder_and_is_idempotent() -> None:
    b = virtual(EUR="100")
    b._reserve(res("o1", "BUY", "EUR", "30"))
    assert b.release("o1") == 30
    assert (b.free("EUR"), b.reserved("EUR")) == (D(100), D(0))
    assert b.release("o1") == 0 and b.free("EUR") == 100


def test_epoch_increases_on_every_mutation() -> None:
    b = virtual(EUR="100")
    e0 = b.epoch
    b._reserve(res("o1", "BUY", "EUR", "30"))
    e1 = b.epoch
    fill(b, "o1", "10", "1")
    e2 = b.epoch
    b.release("o1")
    assert e0 < e1 < e2 < b.epoch


# ------------------------------------------------------------------------------------------ fills


def test_buy_fill_consumes_notional_plus_fee_and_credits_base() -> None:
    b = virtual(EUR="100")
    b._reserve(res("o1", "BUY", "EUR", "2.25"))
    assert fill(b, "o1", "92.03", "0.024", fee="0.01325") == []
    # consumed = 92.03*0.024 + 0.01325 = 2.22197
    assert b.reserved("EUR") == D("2.25") - D("2.22197")
    assert b.free("SOL") == D("0.024")
    assert b.total("EUR") == D("100") - D("2.22197")  # exactly what was spent
    b.release("o1")
    assert b.free("EUR") == D("100") - D("2.22197")


def test_price_improvement_returns_money_on_release() -> None:
    b = virtual(EUR="100")
    b._reserve(res("o1", "BUY", "EUR", "2.25"))
    fill(b, "o1", "90.00", "0.024")  # filled below the limit
    b.release("o1")
    assert b.free("EUR") == D("100") - D("2.16")


def test_partial_fills_then_cancel() -> None:
    b = virtual(EUR="100")
    b._reserve(res("o1", "BUY", "EUR", "10"))
    fill(b, "o1", "10", "0.3", trade_id="t1")
    fill(b, "o1", "10", "0.2", trade_id="t2")
    assert b.free("SOL") == D("0.5") and b.reserved("EUR") == D(5)
    assert b.release("o1") == D(5)
    assert b.free("EUR") == D(95)


def test_sell_fill_consumes_base_and_credits_proceeds_minus_fee() -> None:
    b = virtual(EUR="0", SOL="0.5")
    b._reserve(res("s1", "SELL", "SOL", "0.5"))
    assert b.free("SOL") == 0 and b.reserved_sell_base("SOL-EUR") == D("0.5")
    fill(b, "s1", "100", "0.5", fee="0.30")
    assert b.free("EUR") == D("49.70") and b.reserved("SOL") == 0 and b.reserved_sell_base("SOL-EUR") == 0


def test_a_fill_is_applied_once_even_if_delivered_twice() -> None:
    b = virtual(EUR="100")
    b._reserve(res("o1", "BUY", "EUR", "10"))
    fill(b, "o1", "10", "0.3", trade_id="t1")
    fill(b, "o1", "10", "0.3", trade_id="t1")  # same trade from websocket and polling
    assert b.free("SOL") == D("0.3") and b.reserved("EUR") == D(7)


def test_buy_fill_above_the_reservation_raises_an_alarm_and_never_goes_negative() -> None:
    b = virtual(EUR="5")
    b._reserve(res("o1", "BUY", "EUR", "3"))
    alarms = fill(b, "o1", "10", "0.5")  # consumed 5 > reserved 3
    assert [a.kind for a in alarms] == ["reservation_overdraw"]
    assert b.reserved("EUR") == 0 and b.free("EUR") == 0 and b.free("SOL") == D("0.5")


def test_sell_fill_above_the_reservation_raises_an_alarm() -> None:
    b = virtual(SOL="1")
    b._reserve(res("s1", "SELL", "SOL", "0.4"))
    alarms = fill(b, "s1", "100", "0.6")
    assert [a.kind for a in alarms] == ["reservation_overdraw"]
    assert b.free("SOL") >= 0 and b.reserved("SOL") == 0


def test_fill_without_reservation_alarms_and_changes_nothing() -> None:
    b = virtual(EUR="100", SOL="1")
    alarms = fill(b, "ghost", "100", "0.5")
    assert [a.kind for a in alarms] == ["reservation_overdraw"]
    assert (b.free("EUR"), b.free("SOL")) == (D(100), D(1))


def test_invalid_fill_values_are_rejected() -> None:
    b = virtual(EUR="100")
    b._reserve(res("o1", "BUY", "EUR", "10"))
    for kwargs in ({"size": "0"}, {"price": "-1"}, {"fee": "-0.1"}):
        args = {"price": "1", "size": "1", "fee": "0", **kwargs}
        with pytest.raises(ValueError):
            fill(b, "o1", args["price"], args["size"], args["fee"])


# --------------------------------------------------------------------------- source and staleness


def test_virtual_book_is_always_fresh_and_has_no_snapshot() -> None:
    b = virtual(EUR="1")
    assert b.snapshot_age_s() == 0.0
    with pytest.raises(ValueError, match="virtual"):
        b.apply_snapshot(AccountSnapshot(balances={}, taken_at=T0), b.epoch, INC)


def test_exchange_book_has_no_age_until_the_first_snapshot() -> None:
    b, clock = exchange()
    assert b.snapshot_age_s() is None
    out, _ = b.apply_snapshot(AccountSnapshot(balances={"EUR": (D(50), D(0))}, taken_at=clock.now), b.epoch, INC)
    assert out is SnapshotOutcome.ACCEPTED and b.snapshot_age_s() == 0.0
    clock.advance(45)
    assert b.snapshot_age_s() == 45.0


# ----------------------------------------------------------------------------------- snapshot (b)


def snap(clock: Clock, foreign: set[str] | None = None, **bal: tuple[str, str]) -> AccountSnapshot:
    return AccountSnapshot(
        balances={c: (D(a), D(h)) for c, (a, h) in bal.items()},
        foreign_order_currencies=frozenset(foreign or ()),
        taken_at=clock.now,
    )


def test_snapshot_without_open_orders_sets_free_to_available() -> None:
    b, clock = exchange()
    out, alarms = b.apply_snapshot(snap(clock, EUR=("100", "0"), SOL=("2", "0")), b.epoch, INC)
    assert out is SnapshotOutcome.ACCEPTED and alarms == []
    assert (b.free("EUR"), b.free("SOL")) == (D(100), D(2))


def test_snapshot_with_our_order_on_hold_does_not_subtract_the_reservation_twice() -> None:
    b, clock = exchange(EUR="100")
    b._reserve(res("o1", "BUY", "EUR", "30"))
    # exchange: 100 total, 30 on hold for o1, 70 available
    _, alarms = b.apply_snapshot(snap(clock, EUR=("70", "30")), b.epoch, INC)
    assert alarms == [] and b.free("EUR") == D(70) and b.total("EUR") == D(100)


def test_snapshot_taken_before_the_hold_appeared_subtracts_the_reservation() -> None:
    b, clock = exchange(EUR="100")
    b._reserve(res("o1", "BUY", "EUR", "30"))
    # order not yet visible as hold: exchange still shows all 100 available
    b.apply_snapshot(snap(clock, EUR=("100", "0")), b.epoch, INC)
    assert b.free("EUR") == D(70)  # never more than the truly spendable amount


def test_foreign_orders_get_no_hold_credit() -> None:
    b, clock = exchange(EUR="100")
    b._reserve(res("o1", "BUY", "EUR", "30"))
    # 100 total; our hold 30 + foreign hold 20 -> available 50. Without credit: 50 - 30 = 20 (conservative)
    _, alarms = b.apply_snapshot(snap(clock, foreign={"EUR"}, EUR=("50", "50")), b.epoch, INC)
    assert b.free("EUR") == D(20) and alarms == []


def test_hold_above_reservation_without_foreign_orders_raises_an_alarm() -> None:
    b, clock = exchange(EUR="100")
    b._reserve(res("o1", "BUY", "EUR", "30"))
    _, alarms = b.apply_snapshot(snap(clock, EUR=("60", "40")), b.epoch, INC)  # hold 40 > reserved 30 + 0.01
    assert [a.kind for a in alarms] == ["hold_exceeds_reservation"]


def test_snapshot_read_during_a_local_event_is_discarded() -> None:
    b, clock = exchange(EUR="100")
    epoch_at_request = b.epoch
    b._reserve(res("o1", "BUY", "EUR", "30"))  # local event while the REST call was in flight
    out, alarms = b.apply_snapshot(snap(clock, EUR=("100", "0")), epoch_at_request, INC)
    assert out is SnapshotOutcome.DISCARDED_EPOCH and alarms == []
    assert b.free("EUR") == D(70)  # unchanged


def test_balance_drift_is_reported_but_not_on_the_first_snapshot() -> None:
    b, clock = exchange()
    _, first = b.apply_snapshot(snap(clock, EUR=("100", "0")), b.epoch, INC)
    assert first == []
    _, second = b.apply_snapshot(snap(clock, EUR=("90", "0")), b.epoch, INC)  # 10 EUR vanished
    assert [a.kind for a in second] == ["balance_drift"]
    assert b.free("EUR") == D(90)  # the exchange value prevails
    _, third = b.apply_snapshot(snap(clock, EUR=("90.005", "0")), b.epoch, INC)  # within 2 increments
    assert third == []


def test_snapshot_never_makes_free_negative() -> None:
    b, clock = exchange(EUR="100")
    b._reserve(res("o1", "BUY", "EUR", "100"))
    b.apply_snapshot(snap(clock, EUR=("20", "0")), b.epoch, INC)  # exchange has far less than we reserved
    assert b.free("EUR") == 0


def test_restore_reservation_then_snapshot_rebuilds_the_book() -> None:
    b, clock = exchange()
    b.restore_reservation(res("o1", "BUY", "EUR", "30"))
    assert b.free("EUR") == 0 and b.reserved("EUR") == 30
    b.apply_snapshot(snap(clock, EUR=("70", "30")), b.epoch, INC)
    assert (b.free("EUR"), b.reserved("EUR"), b.total("EUR")) == (D(70), D(30), D(100))
