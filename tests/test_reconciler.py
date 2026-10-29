"""Reconciler: startup rebuild, periodic polling, unknown orders, brackets, drift alarms.

Everything runs against ``FakeExchange`` (reads only). A scenario that reaches for ``create_order`` or
``cancel_orders`` fails with an AssertionError raised by the fake itself.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from decimal import Decimal

import pytest

from modules.config import FallbackFees
from modules.execution.balance import CoverageAlarm, Reservation
from modules.execution.balance_guard import BalanceStale
from modules.execution.exchange import ExchangeUnavailable
from modules.execution.reconciler import (
    DRIFT_ALARMS_TO_TRIP,
    UNKNOWN_CYCLES,
    BracketRequest,
    CoverageMonitor,
    Reconciler,
    ReconcileReport,
)
from modules.ledger.positions import FillRecord, OrderRecord, PositionLedger
from modules.risk.costs import FeeProvider
from tests.coverage_fixtures import T0, Clock
from tests.execution_fakes import (
    PLAN,
    READS,
    B,
    P,
    World,
    bracket_spec,
    buy_spec,
    fill,
    info,
    new_world_parts,
)

D = Decimal
FALLBACK = FallbackFees()


@pytest.fixture
async def world() -> AsyncIterator[World]:
    """A process that just started: the book has never seen a snapshot."""
    ledger = await PositionLedger.open(":memory:", "live")
    w = new_world_parts(ledger)
    yield w
    await ledger.close()


class RecordingPlacer:
    def __init__(self) -> None:
        self.requests: list[BracketRequest] = []
        self.fail: Exception | None = None

    async def place_missing_bracket(self, request: BracketRequest) -> None:
        if self.fail is not None:
            raise self.fail
        self.requests.append(request)


def build(
    w: World,
    *,
    placer: RecordingPlacer | None = None,
    adopt: Callable[..., OrderRecord | None] | None = None,
    interval_s: float = 60.0,
) -> tuple[Reconciler, list[str], CoverageMonitor]:
    trips: list[str] = []
    monitor = CoverageMonitor(trips.append, w.clock)
    rec = Reconciler(
        book=B, exchange=w.exchange, ledger=w.ledger, balances=w.book, registry=w.registry, sink=w.sink,
        fees=FeeProvider(FALLBACK, clock=w.clock), fallback=FALLBACK, interval_s=interval_s,
        on_alarm=monitor.on_alarm, placer=placer, adopt=adopt, clock=w.clock,
    )  # fmt: skip
    return rec, trips, monitor


def balances(w: World, **b: tuple[str, str]) -> None:
    w.exchange.balances = dict(b)


async def with_bracket(w: World) -> None:
    await w.open_position()
    await w.record(bracket_spec(), leg="bracket", status="OPEN", exchange_order_id="ex-br1")
    await w.ledger.set_bracket(B, P, "br1", PLAN.tp, PLAN.sl)


def bracket_info(status: str = "OPEN", filled: str = "0") -> object:
    return info("br1", side="SELL", status=status, base="0.024", filled=filled, limit="108.96", stop="83.57")


# -------------------------------------------------------------------------------- startup: reservations


async def test_startup_restores_open_order_reservations(world: World) -> None:
    await world.record(buy_spec(), leg="entry", status="NEW")
    world.exchange.open = [info("b1", base="0.024", limit="92.03")]
    balances(world, EUR=("997.79128", "2.20872"), SOL=("0", "0"))  # the exchange holds limit x size
    rec, trips, _ = build(world)
    report = await rec.on_startup()
    r = world.book.reservation("b1")
    # required_quote(limit, leaves)
    assert r is not None and (r.side, r.currency, r.amount) == ("BUY", "EUR", D("2.25"))
    assert world.book.free("EUR") == D("997.75") and world.book.total("EUR") == D("1000.00000")
    assert world.book.snapshot_at is not None and world.book.epoch > 0
    assert (report.snapshot_applied, report.restored_reservations, report.foreign_orders) == (True, 1, 0)
    assert trips == [] and world.alarms == []
    stored = await world.sink.local_order("b1")
    assert stored is not None and (stored.status, stored.exchange_order_id) == ("OPEN", "ex-b1")


async def test_startup_restores_the_reservation_of_a_bracket_with_the_leaves_size(world: World) -> None:
    await with_bracket(world)
    world.exchange.open = [bracket_info()]  # type: ignore[list-item]
    balances(world, EUR=("1000", "0"), SOL=("0", "0.024"))
    placer = RecordingPlacer()
    rec, trips, _ = build(world, placer=placer)
    report = await rec.on_startup()
    r = world.book.reservation("br1")
    assert r is not None and (r.side, r.currency, r.amount) == ("SELL", "SOL", D("0.024"))
    assert world.book.free("SOL") == 0 and world.book.reserved_sell_base(P) == D("0.024")
    assert placer.requests == [] and report.unprotected_positions == 0 and trips == []


async def test_the_reservation_of_a_partly_filled_buy_uses_the_leaves_not_the_order_size(world: World) -> None:
    await world.record(buy_spec(), leg="entry")
    earlier = T0 - timedelta(minutes=5)
    past = FillRecord("t1", B, "b1", P, "BUY", D("92.03"), D("0.010"), D("0.01"), "MAKER", earlier)
    await world.ledger.record_fill(past)
    await world.ledger.apply_entry_fill(B, P, D("0.010"), D("92.03"), "dec1", earlier, PLAN)
    world.exchange.open = [info("b1", base="0.024", filled="0.010")]
    world.exchange.fills["ex-b1"] = [fill("t1", "b1", size="0.010", fee="0.01", at=T0 - timedelta(minutes=5))]
    balances(world, EUR=("998.0", "1.3"), SOL=("0.010", "0"))
    rec, _, _ = build(world)
    await rec.on_startup()
    r = world.book.reservation("b1")
    assert r is not None and r.amount == D("1.32")  # ceil(92.03 x 0.014 x 1.012) + 0.01 = 1.31 + 0.01
    assert world.ledger.view(B, P).base_qty == D("0.010")  # the fill was already in the ledger: not applied twice


async def test_startup_applies_the_snapshot_after_the_reservations_not_before(world: World) -> None:
    """free = max(available - (reserved - min(hold, reserved)), 0): it needs the restored reservation."""
    await world.record(buy_spec(), leg="entry")
    world.exchange.open = [info("b1")]
    balances(world, EUR=("100.00", "2.20872"), SOL=("0", "0"))
    rec, _, _ = build(world)
    await rec.on_startup()
    assert world.book.free("EUR") == D("100.00") - (D("2.25") - D("2.20872"))


# ---------------------------------------------------------------------------------- startup: foreign orders


async def test_an_order_chronos_does_not_know_is_left_alone_and_its_hold_is_not_credited(world: World) -> None:
    world.exchange.open = [info("manual", side="SELL", base="0.5", limit="150")]
    balances(world, EUR=("1000", "0"), SOL=("1.0", "0.5"))
    rec, trips, _ = build(world)
    report = await rec.on_startup()
    assert report.foreign_orders == 1 and world.book.open_reservations() == []
    assert world.book.free("SOL") == D("1.0")  # no credit for a hold we did not make
    assert trips == [] and world.alarms == []  # a foreign hold is not "hold_exceeds_reservation"
    assert set(world.exchange.calls) <= READS


async def test_an_order_closed_locally_but_open_on_the_exchange_is_not_credited_either(world: World) -> None:
    await world.record(buy_spec(), leg="entry", status="CANCELLED")
    world.exchange.open = [info("b1")]
    balances(world, EUR=("997.79", "2.21"), SOL=("0", "0"))
    rec, trips, _ = build(world)
    report = await rec.on_startup()
    assert report.foreign_orders == 1 and world.book.reservation("b1") is None
    assert world.book.free("EUR") == D("997.79") and trips == []
    stored = await world.sink.local_order("b1")
    assert stored is not None and stored.status == "CANCELLED"  # the record is not revived


async def test_an_unrecorded_order_can_be_adopted_when_the_hook_recognises_it(world: World) -> None:
    def adopt(order: object) -> OrderRecord | None:
        cid = order.client_order_id  # type: ignore[attr-defined]
        if cid != "mine":
            return None
        return OrderRecord(cid, B, P, "BUY", "entry", "NEW", T0, T0)

    world.exchange.open = [info("mine"), info("theirs")]
    balances(world, EUR=("997.79128", "2.20872"), SOL=("0", "0"))
    rec, _, _ = build(world, adopt=adopt)
    report = await rec.on_startup()
    assert (report.adopted_orders, report.foreign_orders) == (1, 1)
    assert world.book.reservation("mine") is not None and world.book.reservation("theirs") is None
    assert await world.sink.local_order("mine") is not None and await world.sink.local_order("theirs") is None


async def test_a_hold_without_any_order_trips_coverage_breach(world: World) -> None:
    balances(world, EUR=("990", "10"), SOL=("0", "0"))
    rec, trips, _ = build(world)
    await rec.on_startup()
    assert len(trips) == 1 and trips[0].startswith("hold_exceeds_reservation:EUR")


async def test_the_first_snapshot_is_not_a_drift(world: World) -> None:
    await world.open_position()  # the ledger and the exchange disagree wildly, but nothing is "local" yet
    balances(world, EUR=("1000", "0"), SOL=("5", "0"))
    rec, trips, monitor = build(world)
    await rec.on_startup()
    assert trips == [] and monitor.drift_count_24h() == 0 and world.book.total("SOL") == 5


# ------------------------------------------------------------------- startup: what happened while down


async def test_an_entry_that_filled_while_the_process_was_down_updates_the_ledger_and_not_the_book_twice(
    world: World,
) -> None:
    await world.record(buy_spec(), leg="entry", status="NEW")
    world.clock.advance(120)
    world.exchange.history = [info("b1", status="FILLED", filled="0.024")]
    world.exchange.fills["ex-b1"] = [fill("t1", "b1", size="0.024", fee="0.02", at=T0 + timedelta(seconds=60))]
    balances(world, EUR=("997.77128", "0"), SOL=("0.024", "0"))  # the exchange already shows the fill
    rec, trips, _ = build(world)
    report = await rec.on_startup()
    pos = world.ledger.view(B, P)
    assert pos.is_long and pos.base_qty == D("0.024") and pos.entry_decision_id == "dec1"
    assert world.book.free("SOL") == D("0.024")  # NOT 0.048
    assert world.book.free("EUR") == D("997.77128") and world.book.open_reservations() == []
    assert (report.new_fills, report.finalized_orders, trips, world.alarms) == (1, 1, [], [])
    stored = await world.sink.local_order("b1")
    assert stored is not None and stored.status == "FILLED"


async def test_a_bracket_that_filled_while_the_process_was_down_closes_the_position(world: World) -> None:
    await with_bracket(world)
    world.clock.advance(300)
    world.exchange.history = [bracket_info("FILLED", "0.024")]  # type: ignore[list-item]
    world.exchange.fills["ex-br1"] = [
        fill("x1", "br1", side="SELL", price="108.96", size="0.024", fee="0.03", at=T0 + timedelta(seconds=30))
    ]
    balances(world, EUR=("1002.58", "0"), SOL=("0", "0"))
    rec, trips, _ = build(world, placer=RecordingPlacer())
    report = await rec.on_startup()
    assert not world.ledger.view(B, P).is_long and len(world.closed) == 1
    assert world.book.free("EUR") == D("1002.58") and world.book.free("SOL") == 0  # snapshot only, no double credit
    assert trips == [] and world.alarms == [] and report.finalized_orders == 1 and report.unprotected_positions == 0


async def test_an_order_that_exists_on_the_exchange_under_its_client_order_id_is_found_after_a_timeout(
    world: World,
) -> None:
    """OutcomeUnknown: the record has no exchange id; the reservation must come back."""
    await world.record(buy_spec(), leg="entry", status="NEW")
    world.exchange.open = [info("b1")]
    balances(world, EUR=("997.79128", "2.20872"), SOL=("0", "0"))
    rec, _, _ = build(world)
    await rec.on_startup()
    stored = await world.sink.local_order("b1")
    assert stored is not None and stored.exchange_order_id == "ex-b1"
    assert world.book.reservation("b1") is not None and world.book.reserved("EUR") == D("2.25")


async def test_the_history_lookup_starts_a_minute_before_the_oldest_unresolved_order(world: World) -> None:
    await world.record(buy_spec(), leg="entry", status="NEW")
    balances(world, EUR=("1000", "0"), SOL=("0", "0"))
    rec, _, _ = build(world)
    await rec.on_startup()
    (args,) = world.exchange.history_args
    assert args == {"start_date": (T0 - timedelta(minutes=1)).isoformat()}


async def test_there_is_no_history_lookup_when_every_local_order_is_open_on_the_exchange(world: World) -> None:
    await world.record(buy_spec(), leg="entry", status="NEW")
    world.exchange.open = [info("b1")]
    balances(world, EUR=("997.79128", "2.20872"), SOL=("0", "0"))
    rec, _, _ = build(world)
    await rec.on_startup()
    assert "list_orders" not in world.exchange.calls


# ------------------------------------------------------------------------- startup: races and failures


def race_once(w: World, times: int) -> Callable[[], None]:
    count = {"n": 0}

    def hook() -> None:
        if count["n"] < times:
            count["n"] += 1  # something touches the book while the exchange is being read
            w.book.restore_reservation(Reservation(client_order_id=f"race{count['n']}", product_id=P, side="SELL",
                                                   currency="SOL", amount=D("0.001")))  # fmt: skip

    return hook


async def test_a_book_that_changed_during_the_reads_gets_its_snapshot_from_a_second_read(world: World) -> None:
    world.exchange.on_accounts = race_once(world, 1)
    rec, _, _ = build(world)
    report = await rec.on_startup()
    assert report.snapshot_applied and world.exchange.calls.count("get_accounts") == 2
    assert any("repeated" in n for n in report.notes)


async def test_a_book_that_keeps_changing_never_gets_a_snapshot_over_a_race(world: World) -> None:
    world.exchange.on_accounts = race_once(world, 99)
    rec, _, _ = build(world)
    report = await rec.on_startup()
    assert not report.snapshot_applied and world.exchange.calls.count("get_accounts") == 3
    assert world.book.snapshot_at is None  # fail closed: the guard keeps refusing BUYs
    from tests.execution_fakes import buy_spec as spec

    with pytest.raises(BalanceStale):
        world.guard.authorize(spec())


async def test_a_failed_read_at_startup_propagates_and_changes_nothing(world: World) -> None:
    world.exchange.fail["list_open_orders"] = ExchangeUnavailable("down")
    rec, _, _ = build(world)
    with pytest.raises(ExchangeUnavailable):
        await rec.on_startup()
    assert world.book.snapshot_at is None and world.book.epoch == 0 and world.book.open_reservations() == []


# ------------------------------------------------------------------------------------ periodic cycles


async def test_a_fill_the_ws_missed_is_found_by_the_polling_and_applied_once(world: World) -> None:
    world.snapshot()
    await world.send(buy_spec(), leg="entry")
    world.exchange.open = [info("b1", base="0.024", filled="0.010")]
    world.exchange.fills["ex-b1"] = [fill("t1", "b1", size="0.010", fee="0.01", at=T0 + timedelta(seconds=5))]
    rec, trips, _ = build(world)
    first = await rec.reconcile_once()
    assert first.new_fills == 1 and world.ledger.view(B, P).base_qty == D("0.010")
    assert world.book.free("SOL") == D("0.010")
    assert world.book.reserved("EUR") == D("2.25") - (D("0.920300") + D("0.01"))
    second = await rec.reconcile_once()
    assert second.new_fills == 0 and world.ledger.view(B, P).base_qty == D("0.010")
    assert world.book.free("SOL") == D("0.010") and trips == [] and world.alarms == []


async def test_the_periodic_cycle_reads_only_what_it_needs(world: World) -> None:
    world.snapshot()
    rec, _, _ = build(world)
    await rec.reconcile_once()
    assert world.exchange.calls == ["list_open_orders"]  # no accounts (the balance feed owns them), no history


async def test_an_order_unknown_to_the_exchange_keeps_its_reservation_for_two_cycles_then_is_released(
    world: World,
) -> None:
    world.snapshot()
    await world.send(buy_spec(), leg="entry")  # sent, the answer was lost (OutcomeUnknown), the exchange never got it
    rec, _, _ = build(world)
    assert UNKNOWN_CYCLES == 2
    first = await rec.reconcile_once()
    assert first.unresolved_orders == 1 and first.released_unknown == 0
    assert world.book.reserved("EUR") == D("2.25")
    second = await rec.reconcile_once()
    assert second.released_unknown == 1
    assert world.book.reserved("EUR") == 0 and world.book.free("EUR") == D("1000")
    stored = await world.sink.local_order("b1")
    assert stored is not None and stored.status == "FAILED"
    third = await rec.reconcile_once()
    assert third.unresolved_orders == 0  # closed: not looked for again


async def test_a_sighting_resets_the_count_of_missing_cycles(world: World) -> None:
    world.snapshot()
    await world.send(buy_spec(), leg="entry")
    rec, _, _ = build(world)
    await rec.reconcile_once()  # missing (1)
    world.exchange.open = [info("b1")]
    await rec.reconcile_once()  # seen
    world.exchange.open = []
    again = await rec.reconcile_once()  # missing (1 again, not 2)
    assert again.released_unknown == 0 and world.book.reserved("EUR") == D("2.25")


async def test_an_order_found_terminal_in_the_history_is_finalized_not_released_as_unknown(world: World) -> None:
    world.snapshot()
    await world.send(buy_spec(), leg="entry")
    world.exchange.history = [info("b1", status="CANCELLED")]
    rec, _, _ = build(world)
    report = await rec.reconcile_once()
    assert (report.finalized_orders, report.unresolved_orders, report.released_unknown) == (1, 0, 0)
    assert world.book.reserved("EUR") == 0
    stored = await world.sink.local_order("b1")
    assert stored is not None and stored.status == "CANCELLED"


async def test_an_open_order_without_a_reservation_is_reported_not_invented(world: World) -> None:
    world.snapshot()
    await world.record(buy_spec(), leg="entry", status="OPEN")  # a bug elsewhere: nobody reserved it
    world.exchange.open = [info("b1")]
    rec, _, _ = build(world)
    report = await rec.reconcile_once()
    assert world.book.reservation("b1") is None and any("without a reservation" in n for n in report.notes)


async def test_a_failing_cycle_changes_nothing_and_the_next_one_recovers(world: World) -> None:
    world.snapshot()
    await world.send(buy_spec(), leg="entry")
    world.exchange.fail["list_open_orders"] = ExchangeUnavailable("down")
    rec, _, _ = build(world)
    with pytest.raises(ExchangeUnavailable):
        await rec.reconcile_once()
    assert world.book.reserved("EUR") == D("2.25")
    del world.exchange.fail["list_open_orders"]
    world.exchange.open = [info("b1")]
    report = await rec.reconcile_once()
    assert report.synced_orders == 1


# -------------------------------------------------------------------------------------------- brackets


async def flat_long(
    w: World, *, qty: str = "0.024", price: str = "92.03", plan: bool = True, sol: str = "0.024"
) -> None:
    await w.open_position(qty, price, plan=PLAN if plan else None)
    balances(w, EUR=("1000", "0"), SOL=(sol, "0"))


async def test_a_position_without_a_bracket_is_handed_to_the_placer_at_startup(world: World) -> None:
    await flat_long(world)
    placer = RecordingPlacer()
    rec, _, _ = build(world, placer=placer)
    report = await rec.on_startup()
    assert placer.requests == [BracketRequest(B, P, D("0.024"), PLAN.tp, PLAN.sl)]
    assert (report.bracket_requests, report.unprotected_positions) == (1, 1)


async def test_the_bracket_covers_min_of_the_position_and_the_sellable_base(world: World) -> None:
    await flat_long(world, sol="0.0159")  # less base is free than the ledger holds
    placer = RecordingPlacer()
    rec, _, _ = build(world, placer=placer)
    await rec.on_startup()
    assert [r.base_size for r in placer.requests] == [D("0.015")]  # floor_to(min(0.024, 0.0159), 0.001)


async def test_a_sellable_amount_below_the_minimum_gets_no_bracket_and_stays_a_position(world: World) -> None:
    await flat_long(world, sol="0.010")  # 0.010 x 92 = 0.92 EUR < quote_min_size 1
    placer = RecordingPlacer()
    rec, _, _ = build(world, placer=placer)
    report = await rec.on_startup()
    assert placer.requests == [] and world.ledger.view(B, P).is_long and report.dust_conversions == 0
    assert any("sellable" in n for n in report.notes)


@pytest.mark.parametrize(("bid", "dust"), [("999", True), ("1000", False)])
async def test_the_minimum_sellable_size_is_inclusive(world: World, bid: str, dust: bool) -> None:
    await flat_long(world, qty="0.001", price="999", sol="0.001")
    world.exchange.bid = D(bid)  # 0.001 x 999 = 0.999 < 1 <= 0.001 x 1000
    placer = RecordingPlacer()
    rec, _, _ = build(world, placer=placer)
    report = await rec.on_startup()
    assert (report.dust_conversions == 1) is dust and (placer.requests == []) is dust
    pos = world.ledger.view(B, P)
    assert pos.is_long is (not dust)
    if dust:
        assert pos.dust_qty == D("0.001") and pos.dust_cost == D("0.999") and pos.base_qty == 0


async def test_a_position_below_the_minimum_base_size_becomes_dust(world: World) -> None:
    await flat_long(world, qty="0.0009", price="90", sol="0.0009")
    rec, _, _ = build(world, placer=RecordingPlacer())
    report = await rec.on_startup()
    assert report.dust_conversions == 1 and world.ledger.view(B, P).dust_qty == D("0.0009")


async def test_no_bracket_is_requested_without_a_placer_or_without_levels(world: World) -> None:
    await flat_long(world, plan=False)
    placer = RecordingPlacer()
    rec, _, _ = build(world, placer=placer)
    report = await rec.on_startup()
    assert placer.requests == [] and report.bracket_requests == 0 and world.ledger.view(B, P).is_long
    other_ledger = await PositionLedger.open(":memory:", "live")
    try:
        w2 = new_world_parts(other_ledger)
        await flat_long(w2)
        rec, _, _ = build(w2, placer=None)
        report2 = await rec.on_startup()
        assert report2.bracket_requests == 0 and any("placer" in n for n in report2.notes)
    finally:
        await other_ledger.close()


async def test_a_position_whose_entry_is_still_open_waits_for_that_order(world: World) -> None:
    await flat_long(world)
    await world.record(buy_spec("b2"), leg="entry", status="OPEN")
    world.exchange.open = [info("b2", filled="0")]
    placer = RecordingPlacer()
    rec, _, _ = build(world, placer=placer)
    report = await rec.on_startup()
    assert placer.requests == [] and any("entry still open" in n for n in report.notes)


async def test_a_missing_quote_means_no_judgement_not_dust(world: World) -> None:
    await flat_long(world, qty="0.001", price="999", sol="0.001")
    world.exchange.fail["get_best_bid_ask"] = ExchangeUnavailable("no quote")
    placer = RecordingPlacer()
    rec, _, _ = build(world, placer=placer)
    report = await rec.on_startup()
    assert placer.requests == [] and world.ledger.view(B, P).is_long and report.dust_conversions == 0


async def test_a_failing_placer_does_not_stop_the_startup(world: World) -> None:
    await flat_long(world)
    placer = RecordingPlacer()
    placer.fail = RuntimeError("gateway down")
    rec, _, _ = build(world, placer=placer)
    report = await rec.on_startup()
    assert report.bracket_requests == 0 and any("failed" in n for n in report.notes) and world.book.snapshot_at


async def test_the_periodic_cycle_never_asks_for_a_bracket(world: World) -> None:
    """Between the first fill and the bracket a position legitimately has none: the gateway is on it."""
    world.snapshot(EUR=("1000", "0"), SOL=("0.024", "0"))
    await world.open_position()
    placer = RecordingPlacer()
    rec, _, _ = build(world, placer=placer)
    report = await rec.reconcile_once()
    assert placer.requests == [] and report.unprotected_positions == 1 and report.bracket_requests == 0


async def test_a_bracket_on_the_exchange_that_the_ledger_forgot_is_relinked(world: World) -> None:
    await world.open_position()
    await world.record(bracket_spec(), leg="bracket", status="NEW")  # crashed between the send and set_bracket
    world.exchange.open = [bracket_info()]  # type: ignore[list-item]
    balances(world, EUR=("1000", "0"), SOL=("0", "0.024"))
    placer = RecordingPlacer()
    rec, _, _ = build(world, placer=placer)
    report = await rec.on_startup()
    pos = world.ledger.view(B, P)
    assert (pos.bracket_order_id, pos.tp, pos.sl) == ("br1", D("108.96"), D("83.57"))
    assert report.relinked_brackets == 1 and placer.requests == []


async def test_a_dead_bracket_is_replaced_not_trusted(world: World) -> None:
    await with_bracket(world)
    world.exchange.history = [bracket_info("CANCELLED")]  # type: ignore[list-item]
    balances(world, EUR=("1000", "0"), SOL=("0.024", "0"))
    placer = RecordingPlacer()
    rec, _, _ = build(world, placer=placer)
    report = await rec.on_startup()
    assert report.finalized_orders == 1 and [r.base_size for r in placer.requests] == [D("0.024")]


# --------------------------------------------------------------------------------- ledger vs balances


async def test_a_ledger_position_larger_than_the_account_is_a_balance_drift(world: World) -> None:
    world.snapshot(EUR=("1000", "0"), SOL=("0.010", "0"))
    await with_bracket(world)
    rec, trips, monitor = build(world)
    world.exchange.open = [bracket_info()]  # type: ignore[list-item]
    await world.sink.sync_order(bracket_info())  # type: ignore[arg-type]
    report = await rec.reconcile_once()
    assert report.drift_alarms == 1 and monitor.drift_count_24h() == 1 and trips == []
    for _ in range(DRIFT_ALARMS_TO_TRIP - 1):
        world.clock.advance(60)
        await rec.reconcile_once()
    assert len(trips) == 1 and "balance_drift in 24h" in trips[0]


@pytest.mark.parametrize(("sol", "alarms"), [("0.022", 0), ("0.021", 1)])
async def test_the_drift_tolerance_is_two_increments(world: World, sol: str, alarms: int) -> None:
    world.snapshot(EUR=("1000", "0"), SOL=(sol, "0"))
    await world.open_position()  # 0.024 held
    rec, _, _ = build(world)
    assert (await rec.reconcile_once()).drift_alarms == alarms


async def test_a_foreign_hold_on_the_currency_suspends_the_comparison(world: World) -> None:
    world.snapshot(EUR=("1000", "0"), SOL=("0.010", "0"))
    await world.open_position()
    world.exchange.open = [info("manual", side="SELL", base="0.5", limit="150")]
    rec, trips, _ = build(world)
    assert (await rec.reconcile_once()).drift_alarms == 0 and trips == []


async def test_no_comparison_before_the_first_snapshot(world: World) -> None:
    await world.open_position()
    rec, _, _ = build(world)
    assert (await rec.reconcile_once()).drift_alarms == 0


# ----------------------------------------------------------------------------------------- the monitor


def alarm(kind: str) -> CoverageAlarm:
    return CoverageAlarm(kind=kind, currency="EUR", detail="x")  # type: ignore[arg-type]


@pytest.mark.parametrize("kind", ["reservation_overdraw", "hold_exceeds_reservation"])
def test_overdraw_and_excess_hold_trip_at_once(kind: str) -> None:
    trips: list[str] = []
    CoverageMonitor(trips.append, Clock()).on_alarm(alarm(kind))
    assert len(trips) == 1 and trips[0].startswith(kind)


def test_the_third_drift_within_24_hours_trips_and_the_second_does_not() -> None:
    clock, trips = Clock(), []
    monitor = CoverageMonitor(trips.append, clock)
    for n in range(2):
        monitor.on_alarm(alarm("balance_drift"))
        clock.advance(3600)
        assert trips == [] and monitor.drift_count_24h() == n + 1
    monitor.on_alarm(alarm("balance_drift"))
    assert len(trips) == 1 and "3 balance_drift" in trips[0]


@pytest.mark.parametrize(("gap_s", "trips_expected"), [(24 * 3600 - 1, 1), (24 * 3600, 0)])
def test_a_drift_exactly_24_hours_old_no_longer_counts(gap_s: int, trips_expected: int) -> None:
    clock, trips = Clock(), []
    monitor = CoverageMonitor(trips.append, clock)
    monitor.on_alarm(alarm("balance_drift"))
    clock.advance(gap_s)
    monitor.on_alarm(alarm("balance_drift"))
    monitor.on_alarm(alarm("balance_drift"))
    assert len(trips) == trips_expected


# ---------------------------------------------------------------------------------------------- run loop


async def wait_until(condition: Callable[[], bool], timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.005)


async def test_a_request_wakes_the_loop_and_requests_collapse_into_one_cycle(world: World) -> None:
    world.snapshot()
    rec, _, _ = build(world, interval_s=60)
    rec.request("sequence_gap")
    rec.request("sequence_gap")
    rec.request("ws_reconnected")
    task = asyncio.create_task(rec.run())
    try:
        await wait_until(lambda: rec.last_report is not None)
        assert isinstance(rec.last_report, ReconcileReport)
        assert rec.last_report.reason == "sequence_gap,ws_reconnected"
        assert world.exchange.calls.count("list_open_orders") == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_the_loop_runs_on_its_own_period(world: World) -> None:
    world.snapshot()
    rec, _, _ = build(world, interval_s=0.01)
    task = asyncio.create_task(rec.run())
    try:
        await wait_until(lambda: world.exchange.calls.count("list_open_orders") >= 3)
        assert rec.last_report is not None and rec.last_report.reason == "periodic"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_the_loop_survives_failing_cycles_and_reports_them(world: World) -> None:
    world.snapshot()
    world.exchange.fail["list_open_orders"] = ExchangeUnavailable("down")
    rec, _, _ = build(world, interval_s=0.01)
    task = asyncio.create_task(rec.run())
    try:
        await wait_until(lambda: rec.consecutive_failures >= 2)
        assert rec.last_error is not None and "ExchangeUnavailable" in rec.last_error
        del world.exchange.fail["list_open_orders"]
        await wait_until(lambda: rec.consecutive_failures == 0 and rec.last_error is None)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# --------------------------------------------------------------------------------------- construction


async def test_the_reconciler_validates_its_wiring(world: World) -> None:
    from modules.execution.balance import BalanceBook

    kwargs = {
        "book": B, "exchange": world.exchange, "ledger": world.ledger, "balances": world.book,
        "registry": world.registry, "sink": world.sink, "fees": FeeProvider(FALLBACK), "fallback": FALLBACK,
        "interval_s": 60.0,
    }  # fmt: skip
    Reconciler(**kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="positive"):
        Reconciler(**{**kwargs, "interval_s": 0})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="exchange book"):
        Reconciler(**{**kwargs, "balances": BalanceBook(B, "virtual")})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="same book"):
        Reconciler(**{**kwargs, "book": "other"})  # type: ignore[arg-type]
