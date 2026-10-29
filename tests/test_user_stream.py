"""User stream: parsing, the sink that turns exchange order states into ledger/balance changes, and the WS shell.

No network and no keys: the exchange is ``FakeExchange`` (reads only) and the SDK thread is a fake transport.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from modules.config import CoinbaseConfig
from modules.execution import user_stream
from modules.execution.user_stream import (
    CoinbaseUserStream,
    OrderEventSink,
    parse_order_message,
    parse_user_message,
    user_transport_factory,
)
from modules.ledger.positions import PositionLedger
from modules.market.ws_feed import ErrorEvent, Heartbeat
from tests.coverage_fixtures import T0
from tests.execution_fakes import (
    PLAN,
    B,
    P,
    World,
    bracket_spec,
    buy_spec,
    fill,
    info,
    later,
    new_world_parts,
)

D = Decimal


@pytest.fixture
async def world() -> AsyncIterator[World]:
    ledger = await PositionLedger.open(":memory:", "live")
    w = new_world_parts(ledger)
    w.snapshot()
    yield w
    await ledger.close()


def fills_of(w: World, order_id: str, *fills: Any) -> None:
    w.exchange.fills[order_id] = list(fills)


# ------------------------------------------------------------------------------------------ parsing


def ws_order(
    cid: str, status: str = "OPEN", cum: str = "0", leaves: str = "0.024", side: str = "BUY", limit: str = "92.03",
    stop: str = "0", avg: str = "0", oid: str | None = None,
) -> dict[str, Any]:  # fmt: skip
    return {
        "order_id": oid or f"ex-{cid}", "client_order_id": cid, "product_id": P, "order_side": side, "status": status,
        "cumulative_quantity": cum, "leaves_quantity": leaves, "limit_price": limit, "stop_price": stop,
        "avg_price": avg, "total_fees": "0", "order_type": "Limit",
    }  # fmt: skip


def ws(seq: int, orders: list[dict[str, Any]], kind: str = "update") -> str:
    return json.dumps(
        {
            "channel": "user",
            "client_id": "",
            "timestamp": "2026-10-17T12:00:00.123456789Z",
            "sequence_num": seq,
            "events": [{"type": kind, "orders": orders}],
        }  # fmt: skip
    )


def test_user_channel_message_is_parsed_to_order_updates() -> None:
    raw = ws(7, [ws_order("b1", cum="0.010", leaves="0.014", avg="92.03"), ws_order("s1", side="SELL", stop="83.57")],
             kind="snapshot")  # fmt: skip
    env, updates = parse_user_message(raw)
    assert (env.channel, env.sequence_num, env.events) == ("user", 7, ())
    assert [u.snapshot for u in updates] == [True, True]
    a, b = updates[0].order, updates[1].order
    assert (a.client_order_id, a.side, a.status, a.filled_size) == ("b1", "BUY", "OPEN", D("0.010"))
    assert a.base_size == D("0.024") and a.leaves_size == D("0.014") and a.average_filled_price == D("92.03")
    assert a.stop_trigger_price is None  # "0" on this channel means: not applicable
    assert (b.side, b.stop_trigger_price, b.limit_price) == ("SELL", D("83.57"), D("92.03"))
    assert updates[0].ts.year == 2026 and updates[0].ts.microsecond == 123456


def test_an_update_event_is_not_a_snapshot() -> None:
    _, updates = parse_user_message(ws(1, [ws_order("b1")]))
    assert [u.snapshot for u in updates] == [False]


def test_heartbeats_subscriptions_and_errors_use_the_market_parser() -> None:
    hb = json.dumps({"channel": "heartbeats", "timestamp": "2026-10-17T12:00:00Z", "sequence_num": 3,
                     "events": [{"current_time": "x", "heartbeat_counter": 1}]})  # fmt: skip
    env, updates = parse_user_message(hb)
    assert updates == () and isinstance(env.events[0], Heartbeat)
    env, _ = parse_user_message(json.dumps({"type": "error", "message": "authentication failure"}))
    assert env.sequence_num is None and env.events == (ErrorEvent("authentication failure"),)


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        ws(1, [{**ws_order("b1"), "status": ""}]),
        ws(1, [{**ws_order("b1"), "cumulative_quantity": "abc"}]),
        ws(1, [{**ws_order("b1"), "order_id": None}]),
        json.dumps({"channel": "user", "timestamp": "2026-10-17T12:00:00Z", "events": []}),
    ],
)
def test_a_malformed_user_message_raises(raw: str) -> None:
    with pytest.raises((ValueError, KeyError, TypeError)):
        parse_user_message(raw)


def test_a_price_of_zero_is_not_a_price() -> None:
    o = parse_order_message(ws_order("m1", limit="0", stop="0"))
    assert o.limit_price is None and o.stop_trigger_price is None


# ----------------------------------------------------------------------------------- sink: entries


async def test_an_entry_fill_opens_the_long_position_with_its_plan_and_consumes_the_reservation(world: World) -> None:
    await world.send(buy_spec(), leg="entry")
    assert world.book.reserved("EUR") == D("2.25") and world.book.free("EUR") == D("997.75")
    fills_of(world, "ex-b1", fill("t1", "b1", size="0.024", fee="0.02", at=later(5)))
    res = await world.sink.sync_order(info("b1", status="FILLED", filled="0.024"))
    assert (res.status, res.new_fills, res.finalized) == ("synced", 1, True)
    pos = world.ledger.view(B, P)
    assert pos.is_long and pos.base_qty == D("0.024") and pos.avg_entry == D("92.03")
    assert (pos.tp, pos.sl, pos.entry_decision_id, pos.opened_at) == (PLAN.tp, PLAN.sl, "dec1", later(5))
    assert world.book.free("SOL") == D("0.024")
    consumed = D("92.03") * D("0.024") + D("0.02")
    assert world.book.reserved("EUR") == 0 and world.book.free("EUR") == D("1000") - consumed
    assert world.alarms == []
    (rec,) = await world.ledger.fills_for("b1")
    assert (rec.trade_id, rec.size, rec.fee) == ("t1", D("0.024"), D("0.02"))
    stored = await world.sink.local_order("b1")
    assert stored is not None and (stored.status, stored.exchange_order_id) == ("FILLED", "ex-b1")


async def test_partial_fills_accumulate_and_the_terminal_status_releases_the_remainder(world: World) -> None:
    await world.send(buy_spec(), leg="entry")
    fills_of(world, "ex-b1", fill("t1", "b1", size="0.010", fee="0.01", at=later(1)))
    first = await world.sink.sync_order(info("b1", filled="0.010"))
    assert (first.new_fills, first.finalized) == (1, False)
    assert world.ledger.view(B, P).base_qty == D("0.010")
    assert world.book.reserved("EUR") == D("2.25") - (D("92.03") * D("0.010") + D("0.01"))  # still reserved
    fills_of(
        world, "ex-b1", fill("t1", "b1", size="0.010", fee="0.01", at=later(1)),
        fill("t2", "b1", size="0.014", fee="0.01", at=later(2)),
    )  # fmt: skip
    second = await world.sink.sync_order(info("b1", status="CANCELLED", base="0.024", filled="0.024"))
    assert (second.new_fills, second.finalized) == (1, True)
    pos = world.ledger.view(B, P)
    assert pos.base_qty == D("0.024") and pos.avg_entry == D("92.03") and pos.opened_at == later(1)
    assert world.book.reserved("EUR") == 0 and world.book.free("SOL") == D("0.024")
    assert world.book.total("EUR") == D("1000") - (D("92.03") * D("0.024") + D("0.02"))


async def test_a_cancel_in_progress_keeps_the_reservation_until_the_exchange_confirms(world: World) -> None:
    await world.send(buy_spec(), leg="entry")
    res = await world.sink.sync_order(info("b1", status="CANCEL_QUEUED"))
    assert not res.finalized and world.book.reserved("EUR") == D("2.25")  # no optimistic release
    stored = await world.sink.local_order("b1")
    assert stored is not None and stored.status == "CANCEL_QUEUED"
    res = await world.sink.sync_order(info("b1", status="CANCELLED"))
    assert res.finalized and world.book.reserved("EUR") == 0 and world.book.free("EUR") == D("1000")
    assert not world.ledger.view(B, P).is_long


async def test_the_same_fill_from_the_ws_and_from_the_polling_is_applied_once(world: World) -> None:
    await world.send(buy_spec(), leg="entry")
    f = fill("t1", "b1", size="0.024", fee="0.02")
    fills_of(world, "ex-b1", f, f)  # even a duplicate inside one answer
    await world.sink.sync_order(info("b1", status="FILLED", filled="0.024"), source="ws")
    before = (world.book.free("EUR"), world.book.free("SOL"), world.ledger.view(B, P))
    for source in ("poll", "ws", "startup"):
        res = await world.sink.sync_order(info("b1", status="FILLED", filled="0.024"), source=source)
        assert res.new_fills == 0
    assert (world.book.free("EUR"), world.book.free("SOL"), world.ledger.view(B, P)) == before
    assert len(await world.ledger.fills_for("b1")) == 1 and world.alarms == []


async def test_fills_are_applied_in_trade_order_whatever_order_the_exchange_lists_them(world: World) -> None:
    await world.send(buy_spec(), leg="entry")
    early, late = later(1), later(2)
    fills_of(
        world, "ex-b1", fill("t2", "b1", price="92.05", size="0.014", fee="0.01", at=late),
        fill("t1", "b1", price="92.01", size="0.010", fee="0.01", at=early),
    )  # fmt: skip
    await world.sink.sync_order(info("b1", status="FILLED", filled="0.024"))
    pos = world.ledger.view(B, P)
    assert pos.opened_at == early and pos.base_qty == D("0.024")  # the position opens at the FIRST trade
    assert pos.avg_entry == (D("92.01") * D("0.010") + D("92.05") * D("0.014")) / D("0.024")


async def test_a_fill_already_booked_by_another_path_changes_nothing(world: World) -> None:
    """The gateway books fills too (same ``record_fill`` anchor): whoever comes second must be a no-op."""
    from modules.ledger.positions import FillRecord

    await world.send(buy_spec(), leg="entry")
    world.book.apply_fill("b1", base_currency="SOL", quote_currency="EUR", price=D("92.03"), size=D("0.024"),
                          fee=D("0.02"), trade_id="t1")  # fmt: skip
    await world.ledger.record_fill(FillRecord("t1", B, "b1", P, "BUY", D("92.03"), D("0.024"), D("0.02"), "MAKER", T0))
    await world.ledger.apply_entry_fill(B, P, D("0.024"), D("92.03"), "dec1", T0, PLAN)
    fills_of(world, "ex-b1", fill("t1", "b1", size="0.024", fee="0.02"))
    res = await world.sink.sync_order(info("b1", status="FILLED", filled="0.024"))
    assert (res.new_fills, res.finalized) == (0, True)
    assert world.ledger.view(B, P).base_qty == D("0.024") and world.book.free("SOL") == D("0.024")
    assert world.alarms == []


async def test_fills_not_published_yet_keep_the_order_open_and_its_reservation(world: World) -> None:
    await world.send(buy_spec(), leg="entry")
    fills_of(world, "ex-b1", fill("t1", "b1", size="0.010", fee="0.01"))
    res = await world.sink.sync_order(info("b1", status="FILLED", filled="0.024"))
    assert (res.status, res.new_fills, res.finalized) == ("incomplete", 1, False)
    stored = await world.sink.local_order("b1")
    assert stored is not None and stored.status == "NEW"  # not finalized: the fills have to consume it first
    assert world.book.reserved("EUR") > 0 and world.ledger.view(B, P).base_qty == D("0.010")
    fills_of(
        world, "ex-b1", fill("t1", "b1", size="0.010", fee="0.01"),
        fill("t2", "b1", size="0.014", fee="0.01", at=later(1)),
    )  # fmt: skip
    res = await world.sink.sync_order(info("b1", status="FILLED", filled="0.024"))
    assert (res.status, res.new_fills, res.finalized) == ("synced", 1, True)
    assert world.book.reserved("EUR") == 0 and world.alarms == []


async def test_a_terminal_status_never_goes_back_to_open(world: World) -> None:
    await world.send(buy_spec(), leg="entry")
    await world.sink.sync_order(info("b1", status="CANCELLED"))
    res = await world.sink.sync_order(info("b1", status="OPEN"))  # a late, reordered update
    assert res.status == "synced"
    stored = await world.sink.local_order("b1")
    assert stored is not None and stored.status == "CANCELLED"


async def test_an_order_chronos_never_recorded_is_ignored(world: World) -> None:
    fills_of(world, "ex-manual", fill("tm", "manual"))
    res = await world.sink.sync_order(info("manual", status="FILLED", filled="0.024"))
    assert res.status == "foreign"
    assert world.exchange.calls == []  # not even its fills are read
    assert world.book.free("EUR") == D("1000") and not world.ledger.view(B, P).is_long and world.alarms == []
    assert (await world.sink.sync_order(info(""))).status == "foreign"


async def test_a_fill_for_an_order_without_a_reservation_alarms_and_leaves_the_balances(world: World) -> None:
    await world.record(buy_spec(), leg="entry")  # recorded but never authorized: no reservation
    fills_of(world, "ex-b1", fill("t1", "b1", size="0.024", fee="0.02"))
    await world.sink.sync_order(info("b1", status="FILLED", filled="0.024"))
    assert world.kinds == ["reservation_overdraw"]
    assert world.book.free("EUR") == D("1000") and world.book.free("SOL") == 0  # the next snapshot corrects them


async def test_an_unusable_fill_is_skipped_not_applied(world: World) -> None:
    await world.send(buy_spec(), leg="entry")
    fills_of(world, "ex-b1", fill("t0", "b1", size="0"))
    res = await world.sink.sync_order(info("b1", filled="0.024"))
    assert res.status == "incomplete" and not world.ledger.view(B, P).is_long
    assert world.book.reserved("EUR") == D("2.25")


async def test_an_entry_without_context_is_applied_without_levels_and_logged(world: World) -> None:
    world.contexts.default = None
    await world.send(buy_spec(), leg="entry")
    fills_of(world, "ex-b1", fill("t1", "b1", size="0.024", fee="0.02"))
    await world.sink.sync_order(info("b1", status="FILLED", filled="0.024"))
    pos = world.ledger.view(B, P)
    assert pos.is_long and pos.sl is None and pos.tp is None and pos.entry_decision_id == "int1"


# ------------------------------------------------------------------------------- sink: exits


async def long_with_bracket(w: World) -> None:
    w.snapshot(EUR=("1000", "0"), SOL=("0.024", "0"))
    await w.open_position()
    await w.send(bracket_spec(), leg="bracket")
    await w.ledger.set_bracket(B, P, "br1", PLAN.tp, PLAN.sl)


async def test_the_bracket_fill_closes_the_position_and_is_reported_once(world: World) -> None:
    await long_with_bracket(world)
    assert world.book.reserved("SOL") == D("0.024")
    fills_of(world, "ex-br1", fill("x1", "br1", side="SELL", price="108.96", size="0.024", fee="0.03", at=later(60)))
    done = info("br1", side="SELL", status="FILLED", base="0.024", filled="0.024", stop="83.57")
    res = await world.sink.sync_order(done)
    assert (res.new_fills, res.finalized) == (1, True)
    pos = world.ledger.view(B, P)
    assert not pos.is_long and pos.base_qty == 0
    assert world.book.reserved("SOL") == 0 and world.book.free("SOL") == 0
    assert world.book.free("EUR") == D("1000") + D("108.96") * D("0.024") - D("0.03")
    (closed,) = world.closed
    assert closed.position.base_qty == D("0.024") and closed.order.leg == "bracket" and closed.price == D("108.96")
    await world.sink.sync_order(info("br1", side="SELL", status="FILLED", base="0.024", filled="0.024"))
    assert len(world.closed) == 1  # a replay does not report it again


async def test_a_partial_exit_leaves_the_rest_of_the_position_open(world: World) -> None:
    await long_with_bracket(world)
    fills_of(world, "ex-br1", fill("x1", "br1", side="SELL", price="108.96", size="0.010", fee="0.01"))
    await world.sink.sync_order(info("br1", side="SELL", base="0.024", filled="0.010", stop="83.57"))
    pos = world.ledger.view(B, P)
    assert pos.is_long and pos.base_qty == D("0.014") and world.closed == []
    assert world.book.reserved("SOL") == D("0.014")
    fills_of(
        world, "ex-br1", fill("x1", "br1", side="SELL", price="108.96", size="0.010", fee="0.01"),
        fill("x2", "br1", side="SELL", price="108.90", size="0.014", fee="0.01", at=later(1)),
    )  # fmt: skip
    await world.sink.sync_order(info("br1", side="SELL", status="FILLED", base="0.024", filled="0.024"))
    assert not world.ledger.view(B, P).is_long and len(world.closed) == 1 and world.closed[0].price == D("108.90")


async def test_a_sell_fill_larger_than_the_ledger_position_alarms_and_never_goes_short(world: World) -> None:
    await long_with_bracket(world)
    fills_of(world, "ex-br1", fill("x1", "br1", side="SELL", price="100", size="0.030", fee="0.03"))
    await world.sink.sync_order(info("br1", side="SELL", status="FILLED", base="0.030", filled="0.030"))
    assert world.kinds == ["reservation_overdraw", "reservation_overdraw"]  # the book's and the ledger's
    assert not world.ledger.view(B, P).is_long and world.ledger.view(B, P).base_qty == 0
    assert world.book.free("SOL") >= 0 and world.book.free("EUR") >= 0


async def test_a_sell_fill_on_a_flat_ledger_position_alarms(world: World) -> None:
    world.snapshot(EUR=("1000", "0"), SOL=("0.024", "0"))
    await world.open_position()
    await world.send(bracket_spec(), leg="bracket")
    await world.ledger.apply_exit_fill(B, P, D("0.024"))  # the ledger already says FLAT
    fills_of(world, "ex-br1", fill("x1", "br1", side="SELL", price="100", size="0.024", fee="0.03"))
    await world.sink.sync_order(info("br1", side="SELL", status="FILLED", base="0.024", filled="0.024"))
    assert world.kinds == ["reservation_overdraw"] and world.closed == []


async def test_a_failing_observer_does_not_stop_the_bookkeeping(world: World) -> None:
    async def boom(event: Any) -> None:
        raise RuntimeError("observer down")

    sink = OrderEventSink(
        book=B, ledger=world.ledger, balances=world.book, registry=world.registry, fills=world.exchange,
        contexts=world.contexts, on_alarm=world.alarms.append, on_closed=boom, clock=world.clock,
    )  # fmt: skip
    await long_with_bracket(world)
    fills_of(world, "ex-br1", fill("x1", "br1", side="SELL", price="100", size="0.024", fee="0.03"))
    res = await sink.sync_order(info("br1", side="SELL", status="FILLED", base="0.024", filled="0.024"))
    assert res.finalized and not world.ledger.view(B, P).is_long


# --------------------------------------------------------------------------------- sink: cut-off


async def test_fills_traded_before_the_startup_snapshot_reach_the_ledger_but_not_the_book(world: World) -> None:
    await world.send(buy_spec(), leg="entry")
    world.sink.set_book_cutoff(later(10))
    fills_of(world, "ex-b1", fill("t1", "b1", size="0.010", fee="0.01", at=later(10)))  # exactly at the cut-off
    await world.sink.sync_order(info("b1", filled="0.010"))
    assert world.ledger.view(B, P).base_qty == D("0.010")  # ledger: yes
    assert world.book.free("SOL") == 0 and world.book.reserved("EUR") == D("2.25")  # book: the snapshot has it
    fills_of(
        world, "ex-b1", fill("t1", "b1", size="0.010", fee="0.01", at=later(10)),
        fill("t2", "b1", size="0.014", fee="0.01", at=later(10) + timedelta(microseconds=1)),
    )  # fmt: skip
    await world.sink.sync_order(info("b1", filled="0.024"))
    assert world.book.free("SOL") == D("0.014")  # one microsecond later: applied
    assert world.ledger.view(B, P).base_qty == D("0.024")


def test_the_sink_refuses_a_balance_book_of_another_book() -> None:
    from modules.execution.balance import BalanceBook
    from modules.execution.spot import SpotRegistry

    with pytest.raises(ValueError, match="same|not the book"):
        OrderEventSink(
            book=B, ledger=None, balances=BalanceBook("other", "exchange"), registry=SpotRegistry({}), fills=None,  # type: ignore[arg-type]
        )  # fmt: skip


# ----------------------------------------------------------------------------------------- stream


class FakeTransport:
    def __init__(self, on_message: Callable[[str], None], on_close: Callable[[], None]) -> None:
        self.on_message, self.on_close = on_message, on_close
        self.opened = self.closed = False
        self.subscriptions: list[tuple[tuple[str, ...], tuple[str, ...]]] = []

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.closed = True

    def subscribe(self, product_ids: Sequence[str], channels: Sequence[str]) -> None:
        self.subscriptions.append((tuple(product_ids), tuple(channels)))


def make_stream(w: World, **kwargs: Any) -> tuple[CoinbaseUserStream, list[str], list[FakeTransport]]:
    reasons: list[str] = []
    transports: list[FakeTransport] = []

    def factory(on_message: Callable[[str], None], on_close: Callable[[], None]) -> FakeTransport:
        t = FakeTransport(on_message, on_close)
        transports.append(t)
        return t

    stream = CoinbaseUserStream(
        [P], w.sink, transport_factory=factory, request_reconcile=reasons.append, clock=w.clock, **kwargs
    )
    return stream, reasons, transports


async def test_an_order_update_from_the_ws_reaches_the_ledger_and_the_book(world: World) -> None:
    await world.send(buy_spec(), leg="entry")
    fills_of(world, "ex-b1", fill("t1", "b1", size="0.024", fee="0.02"))
    stream, reasons, _ = make_stream(world)
    await stream.handle_raw(ws(1, [ws_order("b1", status="FILLED", cum="0.024", leaves="0")]))
    assert world.ledger.view(B, P).base_qty == D("0.024") and world.book.reserved("EUR") == 0
    assert (stream.stats.updates, stream.stats.foreign, reasons) == (1, 0, [])


async def test_a_manual_order_on_the_account_counts_as_foreign(world: World) -> None:
    stream, reasons, _ = make_stream(world)
    await stream.handle_raw(ws(1, [ws_order("manual")]))
    assert (stream.stats.foreign, reasons) == (1, [])


async def test_a_sequence_gap_requests_a_reconciliation(world: World) -> None:
    stream, reasons, _ = make_stream(world)
    await stream.handle_raw(ws(1, []))
    await stream.handle_raw(ws(2, []))
    assert reasons == []
    await stream.handle_raw(ws(4, []))  # message 3 is lost
    assert reasons == ["sequence_gap"] and stream.stats.gaps == 1
    await stream.handle_raw(ws(5, []))
    assert reasons == ["sequence_gap"]  # contiguous again


async def test_a_sequence_that_restarts_requests_a_reconciliation(world: World) -> None:
    stream, reasons, _ = make_stream(world)
    await stream.handle_raw(ws(8, []))
    await stream.handle_raw(ws(0, []))  # a new connection: events in between may be lost
    assert reasons == ["sequence_reset"] and stream.stats.resets == 1


async def test_a_message_that_cannot_be_parsed_requests_a_reconciliation(world: World) -> None:
    stream, reasons, _ = make_stream(world)
    await stream.handle_raw("{broken")
    assert reasons == ["parse_error"] and stream.stats.parse_errors == 1


async def test_a_failing_sink_does_not_kill_the_stream_and_asks_the_reconciler_to_redo_it(world: World) -> None:
    await world.send(buy_spec(), leg="entry")
    world.exchange.fail["get_fills"] = ConnectionError("down")
    stream, reasons, _ = make_stream(world)
    await stream.handle_raw(ws(1, [ws_order("b1", status="FILLED", cum="0.024", leaves="0")]))
    assert reasons == ["sink_error"] and stream.stats.sink_errors == 1
    assert world.book.reserved("EUR") == D("2.25")  # nothing was released on a failure
    del world.exchange.fail["get_fills"]
    fills_of(world, "ex-b1", fill("t1", "b1", size="0.024", fee="0.02"))
    await stream.handle_raw(ws(2, [ws_order("b1", status="FILLED", cum="0.024", leaves="0")]))
    assert world.ledger.view(B, P).base_qty == D("0.024")


async def test_incomplete_fills_ask_for_a_reconciliation(world: World) -> None:
    await world.send(buy_spec(), leg="entry")
    stream, reasons, _ = make_stream(world)
    await stream.handle_raw(ws(1, [ws_order("b1", status="FILLED", cum="0.024", leaves="0")]))
    assert reasons == ["incomplete_fills"] and stream.stats.incomplete == 1


async def test_a_broken_reconcile_callback_does_not_stop_the_stream(world: World) -> None:
    def boom(_: str) -> None:
        raise RuntimeError("no")

    stream = CoinbaseUserStream([P], world.sink, transport_factory=lambda *_: None, request_reconcile=boom)  # type: ignore[arg-type, return-value]
    await stream.handle_raw("{broken")
    await stream.handle_raw(ws(1, []))


async def test_silence_is_detected_once_and_the_first_message_after_it_asks_for_a_reconciliation(world: World) -> None:
    stream, reasons, _ = make_stream(world, silence_s=5.0)
    assert not stream.check_silence(world.clock())  # never heard anything: nothing to be silent about
    await stream.handle_raw(ws(1, []))
    world.clock.advance(5)
    assert not stream.check_silence(world.clock())  # exactly silence_s is not yet silence
    world.clock.advance(0.001)
    assert stream.check_silence(world.clock()) and not stream.check_silence(world.clock())
    await stream.handle_raw(ws(2, []))
    assert reasons == ["ws_silence_recovered"]


async def wait_for(condition: Callable[[], bool], timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.005)


async def test_run_bridges_the_sdk_thread_subscribes_and_reconnects(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(user_stream, "BACKOFF_MIN_S", 0.01)
    await world.send(buy_spec(), leg="entry")
    fills_of(world, "ex-b1", fill("t1", "b1", size="0.024", fee="0.02"))
    stream, reasons, transports = make_stream(world)
    task = asyncio.create_task(stream.run())
    try:
        await wait_for(lambda: len(transports) == 1 and bool(transports[0].subscriptions))
        t = transports[0]
        assert t.opened and t.subscriptions == [((P,), ("user", "heartbeats"))]
        raw = ws(1, [ws_order("b1", status="FILLED", cum="0.024", leaves="0")])
        threading.Thread(target=t.on_message, args=(raw,)).start()  # the SDK calls back from its own thread
        await wait_for(lambda: world.ledger.view(B, P).is_long)
        assert reasons == ["ws_connected"]  # orders may have moved while nobody listened
        threading.Thread(target=t.on_close).start()
        await wait_for(lambda: len(transports) == 2 and bool(transports[1].subscriptions))
        assert t.closed and stream.stats.reconnects == 1
        threading.Thread(target=transports[1].on_message, args=(ws(0, []),)).start()
        await wait_for(lambda: len(reasons) == 2)
        assert reasons[1] == "ws_connection_closed"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert transports[-1].closed


async def test_the_first_connection_is_retried_with_a_back_off(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(user_stream, "BACKOFF_MIN_S", 0.01)
    attempts: list[int] = []
    transports: list[FakeTransport] = []

    def factory(on_message: Callable[[str], None], on_close: Callable[[], None]) -> FakeTransport:
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionError("refused")
        t = FakeTransport(on_message, on_close)
        transports.append(t)
        return t

    stream = CoinbaseUserStream([P], world.sink, transport_factory=factory, clock=world.clock)
    task = asyncio.create_task(stream.run())
    try:
        await wait_for(lambda: bool(transports) and bool(transports[0].subscriptions))
        assert len(attempts) == 3
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_the_production_transport_is_built_from_the_environment_credentials_without_a_network() -> None:
    factory = user_transport_factory(CoinbaseConfig(api_key="organizations/x/apiKeys/y", api_secret="secret"))  # type: ignore[arg-type]
    transport = factory(lambda _: None, lambda: None)
    assert type(transport).__name__ == "CoinbaseUserTransport"
    anonymous = user_transport_factory(CoinbaseConfig())
    assert callable(anonymous)


async def test_a_crash_between_the_fill_and_the_position_leaves_neither(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.send(buy_spec(), leg="entry")
    fills_of(world, "ex-b1", fill("t1", "b1", size="0.024", fee="0.02", at=later(5)))

    async def broken(_view: Any) -> None:
        raise RuntimeError("crash")

    monkeypatch.setattr(world.ledger, "_write_position", broken)
    with pytest.raises(RuntimeError):
        await world.sink.sync_order(info("b1", status="FILLED", filled="0.024"))
    assert await world.ledger.fills_for("b1") == [] and not world.ledger.view(B, P).is_long
    assert world.book.free("SOL") == 0  # the book was not touched either
    monkeypatch.undo()
    res = await world.sink.sync_order(info("b1", status="FILLED", filled="0.024"))  # the retry finds it missing
    assert res.new_fills == 1 and world.ledger.view(B, P).base_qty == D("0.024")


async def test_the_sink_reads_orders_through_the_ledger_accessor(world: World) -> None:
    await world.send(buy_spec(), leg="entry")
    rec = await world.sink.local_order("b1")
    assert rec == await world.ledger.get_order("b1", B)
    assert await world.sink.local_order("") is None and await world.sink.local_order("nope") is None
