from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import modules.market.ws_feed as ws_feed
from modules.market.ws_feed import (
    CHANNELS,
    BookEvent,
    CoinbaseMarketStream,
    ErrorEvent,
    Heartbeat,
    Quote,
    SequenceMonitor,
    Subscribed,
    Trade,
    TradesEvent,
    parse_message,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ws_messages.jsonl"
D = Decimal
T0 = datetime(2026, 10, 17, 10, 0, tzinfo=UTC)
MIN = timedelta(minutes=1)
P = "BTC-EUR"


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


# ---------------------------------------------------------------------------- message builders


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def msg(channel: str, seq: int, events: list[dict[str, Any]], ts: datetime = T0) -> str:
    return json.dumps({"channel": channel, "timestamp": iso(ts), "sequence_num": seq, "events": events})


def l2(
    seq: int, kind: str, bids: Sequence[tuple[str, str]], offers: Sequence[tuple[str, str]], product: str = P
) -> str:
    updates = [{"side": "bid", "price_level": p, "new_quantity": q} for p, q in bids]
    updates += [{"side": "offer", "price_level": p, "new_quantity": q} for p, q in offers]
    return msg("l2_data", seq, [{"type": kind, "product_id": product, "updates": updates}])


def trades(seq: int, kind: str, rows: Sequence[tuple[str, datetime, str, str]], product: str = P) -> str:
    items = [
        {"product_id": product, "trade_id": tid, "price": px, "size": sz, "time": iso(ts), "side": "BUY"}
        for tid, ts, px, sz in rows
    ]
    return msg("market_trades", seq, [{"type": kind, "trades": items}])


def ticker(seq: int, bid: str, ask: str, product: str = P) -> str:
    t = {
        "type": "ticker", "product_id": product, "price": bid, "best_bid": bid, "best_ask": ask,
        "best_bid_quantity": "1", "best_ask_quantity": "2",
    }  # fmt: skip
    return msg("ticker", seq, [{"type": "update", "tickers": [t]}])


def heartbeat(seq: int) -> str:
    return msg("heartbeats", seq, [{"current_time": "x", "heartbeat_counter": seq}])


# --------------------------------------------------------------------------------- the parser


def fixture_messages() -> list[str]:
    return FIXTURE.read_text(encoding="utf-8").splitlines()


def test_every_real_message_parses() -> None:
    envelopes = [parse_message(raw) for raw in fixture_messages()]
    assert {e.channel for e in envelopes} == {"market_trades", "subscriptions", "ticker", "l2_data", "heartbeats"}
    assert all(e.sequence_num is not None and e.ts is not None for e in envelopes)


def test_real_level2_messages_become_decimal_levels() -> None:
    books = [ev for raw in fixture_messages() for ev in parse_message(raw).events if isinstance(ev, BookEvent)]
    snap = next(b for b in books if b.snapshot)
    assert snap.product_id in {"BTC-EUR", "SOL-EUR"} and len(snap.levels) == 30
    assert {lv.side for lv in snap.levels} == {"bid", "offer"}
    assert all(isinstance(lv.price, Decimal) and isinstance(lv.quantity, Decimal) for lv in snap.levels)
    upd = next(b for b in books if not b.snapshot)
    assert upd.levels and not upd.snapshot


def test_real_trades_ticker_heartbeat_and_subscription_messages() -> None:
    events = [ev for raw in fixture_messages() for ev in parse_message(raw).events]
    snapshot = next(e for e in events if isinstance(e, TradesEvent) and e.snapshot)
    assert len(snapshot.trades) == 8 and snapshot.trades[0].trade_id.isdigit() and snapshot.trades[0].price > 0
    assert snapshot.trades[0].ts.tzinfo is not None
    q = next(e for e in events if isinstance(e, Quote))
    assert q.best_bid < q.best_ask and q.product_id in {"BTC-EUR", "SOL-EUR"}
    assert any(isinstance(e, Heartbeat) for e in events)
    assert any(isinstance(e, Subscribed) for e in events)


def test_nanosecond_timestamps_are_accepted() -> None:
    env = parse_message(heartbeat(4).replace("T10:00:00.000000Z", "T10:00:00.123456789Z"))
    assert env.ts == T0.replace(microsecond=123456)


def test_error_messages_are_typed_and_carry_no_sequence() -> None:
    env = parse_message(json.dumps({"type": "error", "message": "authentication failure"}))
    assert env.sequence_num is None and env.events == (ErrorEvent("authentication failure"),)


@pytest.mark.parametrize(
    "raw",
    ["not json", "{}", '{"channel":"l2_data"}', l2(1, "snapshot", [("1", "1")], []).replace('"bid"', '"sideways"')],
)
def test_malformed_messages_raise(raw: str) -> None:
    with pytest.raises((ValueError, KeyError)):
        parse_message(raw)


# --------------------------------------------------------------------------------- sequence


def test_sequence_monitor() -> None:
    m = SequenceMonitor()
    assert [m.check(s) for s in (0, 1, 2)] == ["ok", "ok", "ok"]
    assert m.check(5) == "gap"  # 3 and 4 were lost
    assert m.check(6) == "ok"
    assert m.check(0) == "reset"  # a new connection starts again from 0
    assert m.check(1) == "ok"
    assert m.check(1) == "reset"  # a repeated number is not a normal continuation either


# --------------------------------------------------------------------------------- dispatch


def make_stream(**kw: Any) -> tuple[CoinbaseMarketStream, Clock, list[tuple[float, str]]]:
    clock = Clock()
    tap: list[tuple[float, str]] = []
    stream = CoinbaseMarketStream([P, "ETH-EUR"], clock=clock, raw_tap=lambda t, r: tap.append((t, r)), **kw)
    return stream, clock, tap


def feed(stream: CoinbaseMarketStream, *raws: str) -> None:
    for raw in raws:
        stream.handle_raw(0.0, raw)


def test_snapshot_and_updates_build_the_book() -> None:
    stream, *_ = make_stream()
    feed(stream, l2(0, "snapshot", [("100", "5"), ("99", "7")], [("101", "3"), ("102", "9")]))
    book = stream.books[P]
    assert not book.stale and (book.best_bid(), book.best_ask()) == (D(100), D(101))
    feed(stream, l2(1, "update", [("100", "0"), ("99.5", "2")], [("101", "4")]))
    assert (book.best_bid(), book.best_ask(), book.best_ask_size()) == (D("99.5"), D(101), D(4))
    assert stream.books["ETH-EUR"].stale  # no snapshot for it yet


def test_a_crossed_update_requests_a_level2_resubscription() -> None:
    stream, *_ = make_stream()
    feed(stream, l2(0, "snapshot", [("100", "5")], [("101", "3")]), l2(1, "update", [("101.5", "1")], []))
    assert stream.books[P].stale and stream._resubscribe == {P}
    feed(stream, l2(2, "snapshot", [("100", "5")], [("101", "3")]))
    assert not stream.books[P].stale and stream._resubscribe == set()


def test_updates_before_any_snapshot_are_ignored_without_noise() -> None:
    stream, *_ = make_stream()
    feed(stream, l2(0, "update", [("100", "5")], [("101", "3")]))
    assert stream.books[P].stale and stream._resubscribe == set() and stream.books[P].depth("bid") == 0


def test_on_trades_receives_each_trade_once_even_when_the_snapshot_is_resent() -> None:
    got: list[Trade] = []
    stream, *_ = make_stream(on_trades=got.extend)
    rows = [
        ("12", T0 + timedelta(seconds=30), "100", "1"),
        ("11", T0 + timedelta(seconds=10), "99", "2"),
    ]  # newest first
    feed(stream, trades(0, "snapshot", rows), trades(1, "update", [("13", T0 + timedelta(seconds=40), "101", "1")]))
    feed(stream, trades(2, "snapshot", rows))  # the channel resends recent trades after a reconnect
    assert [t.trade_id for t in got] == ["12", "11", "13"] and stream.stats.trades == 3
    assert (got[0].product_id, got[0].price, got[0].size) == (P, 100.0, 1.0)
    # the same trade id on another product is a different trade
    feed(stream, trades(3, "update", [("12", T0 + timedelta(seconds=50), "5", "1")], product="ETH-EUR"))
    assert len(got) == 4 and stream.stats.trades == 4


def test_without_a_trades_callback_trades_are_still_counted() -> None:
    stream, *_ = make_stream()
    feed(stream, trades(0, "update", [("1", T0, "1", "1")]))
    assert stream.stats.trades == 1


def test_the_seen_trades_memory_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ws_feed, "SEEN_TRADES_MAX", 3)
    got: list[Trade] = []
    stream, *_ = make_stream(on_trades=got.extend)
    for i in range(6):
        feed(stream, trades(i, "update", [(str(i), T0, "1", "1")]))
    assert len(stream._seen) == 3 and len(got) == 6


def test_ticker_and_spread_fallback() -> None:
    stream, *_ = make_stream()
    feed(stream, ticker(0, "100", "100.2"))
    assert stream.quotes[P].best_ask == 100.2
    assert stream.spread_bps(P) == pytest.approx(0.2 / 100.1 * 1e4)  # book not ready: ticker
    feed(stream, l2(1, "snapshot", [("100", "5")], [("100.1", "3")]))
    assert stream.spread_bps(P) == pytest.approx(0.1 / 100.05 * 1e4)  # book is fresh: book wins
    stream.books[P].mark_stale("x")
    assert stream.spread_bps(P) == pytest.approx(0.2 / 100.1 * 1e4)
    assert stream.spread_bps("ETH-EUR") is None


def test_heartbeats_and_errors_are_counted() -> None:
    stream, clock, _ = make_stream()
    feed(stream, heartbeat(0), heartbeat(1))
    stream.handle_raw(0.0, json.dumps({"type": "error", "message": "boom"}))
    assert stream.stats.heartbeats == 2 and stream.stats.errors == ["boom"]
    assert stream.last_message_at == clock.now


def test_malformed_input_is_counted_and_does_not_stop_the_stream() -> None:
    stream, _, tap = make_stream()
    feed(stream, "garbage", heartbeat(0))
    assert stream.stats.parse_errors == 1 and stream.stats.heartbeats == 1
    assert [r for _, r in tap][0] == "garbage"  # the recorder still sees every raw message


def test_the_raw_tap_gets_arrival_time_and_text() -> None:
    stream, _, tap = make_stream()
    stream.handle_raw(123.5, heartbeat(0))
    assert tap == [(123.5, heartbeat(0))]


# ----------------------------------------------------------------------- gaps and silence


def damaged_stream() -> tuple[CoinbaseMarketStream, list[tuple[datetime, datetime | None]], Clock]:
    damage: list[tuple[datetime, datetime | None]] = []
    stream, clock, _ = make_stream(on_damage=lambda start, end: damage.append((start, end)))
    return stream, damage, clock


def test_a_sequence_gap_stales_the_books_and_reports_the_damaged_interval() -> None:
    stream, damage, clock = damaged_stream()
    feed(stream, l2(0, "snapshot", [("100", "5")], [("101", "3")]), heartbeat(1))
    last_good = clock.now
    clock.advance(75)
    feed(stream, heartbeat(5))  # messages 2..4 were lost
    assert stream.stats.gaps == 1 and stream.books[P].stale and P in stream._resubscribe
    assert damage == [(last_good, clock.now)]  # closed interval: from the last good message to now


def test_a_reset_is_a_reconnection_and_is_reported_like_a_gap() -> None:
    stream, damage, clock = damaged_stream()
    feed(stream, heartbeat(0), heartbeat(1), heartbeat(2))
    last_good = clock.now
    clock.advance(130)
    feed(stream, heartbeat(0))  # the connection restarted
    assert stream.stats.resets == 1 and stream.books[P].stale
    assert damage == [(last_good, clock.now)]


def test_no_damage_is_reported_on_a_clean_stream() -> None:
    stream, damage, clock = damaged_stream()
    for i in range(20):
        clock.advance(1)
        feed(stream, heartbeat(i))
    assert damage == [] and stream.stats.gaps == 0 and stream.stats.resets == 0


def test_silence_is_detected_once_and_reported_as_an_open_interval_closed_on_resumption() -> None:
    stream, damage, clock = damaged_stream()
    feed(stream, l2(0, "snapshot", [("100", "5")], [("101", "3")]), heartbeat(1))
    last_good = clock.now
    clock.advance(5)
    assert stream.check_silence(clock.now) is False  # 5 s is still fine
    clock.advance(1)
    assert stream.check_silence(clock.now) is True and stream.books[P].stale
    assert damage == [(last_good, None)]  # open: the damage is still going on
    assert stream.check_silence(clock.now) is False  # only once
    clock.advance(300)
    feed(stream, heartbeat(2))  # the feed is back (same numbering) at 10:05:06
    assert damage[-1] == (last_good, clock.now) and len(damage) == 2  # closed at the first message after the silence


def test_damage_callbacks_are_optional() -> None:
    stream, clock, _ = make_stream()
    feed(stream, heartbeat(0))
    clock.advance(60)
    feed(stream, heartbeat(9))  # a gap with nobody listening
    assert stream.stats.gaps == 1


# --------------------------------------------------------------------------- asyncio shell


class FakeTransport:
    instances: list[FakeTransport] = []

    def __init__(self, on_message: Callable[[str], None], on_close: Callable[[], None]) -> None:
        self.on_message, self.on_close = on_message, on_close
        self.calls: list[tuple[str, Any]] = []
        FakeTransport.instances.append(self)

    def open(self) -> None:
        self.calls.append(("open", None))

    def close(self) -> None:
        self.calls.append(("close", None))

    def subscribe(self, product_ids: Sequence[str], channels: Sequence[str]) -> None:
        self.calls.append(("subscribe", (tuple(product_ids), tuple(channels))))

    def unsubscribe(self, product_ids: Sequence[str], channels: Sequence[str]) -> None:
        self.calls.append(("unsubscribe", (tuple(product_ids), tuple(channels))))


async def settle(cond: Callable[[], bool], timeout: float = 2.0) -> None:
    for _ in range(int(timeout / 0.01)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


@pytest.fixture
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ws_feed, "BACKOFF_MIN_S", 0.01)
    FakeTransport.instances = []


async def test_run_connects_subscribes_and_dispatches_messages(fast_backoff: None) -> None:
    stream = CoinbaseMarketStream([P], FakeTransport, clock=Clock())
    task = asyncio.create_task(stream.run())
    await settle(lambda: len(FakeTransport.instances) == 1)
    t = FakeTransport.instances[0]
    assert t.calls[:2] == [("open", None), ("subscribe", ((P,), CHANNELS))]
    t.on_message(l2(0, "snapshot", [("100", "5")], [("101", "3")]))  # from the "SDK thread"
    await settle(lambda: not stream.books[P].stale)
    assert stream.books[P].best_ask() == D(101)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ("close", None) in t.calls  # the connection is closed on shutdown


async def test_a_closed_connection_is_reopened_resubscribed_and_the_blind_period_is_reported(
    fast_backoff: None,
) -> None:
    damage: list[tuple[datetime, datetime | None]] = []
    clock = Clock()
    stream = CoinbaseMarketStream([P], FakeTransport, clock=clock, on_damage=lambda a, b: damage.append((a, b)))
    task = asyncio.create_task(stream.run())
    await settle(lambda: len(FakeTransport.instances) == 1)
    first = FakeTransport.instances[0]
    first.on_message(heartbeat(0))
    await settle(lambda: stream.stats.heartbeats == 1)
    last_good = clock.now
    first.on_close()
    await settle(lambda: len(FakeTransport.instances) == 2)
    second = FakeTransport.instances[1]
    assert second.calls[:2] == [("open", None), ("subscribe", ((P,), CHANNELS))]
    assert stream.stats.reconnects == 1 and ("close", None) in first.calls
    assert stream.books[P].stale  # until the new snapshot arrives
    assert damage == [(last_good, None)]  # the blind period starts at the last good message
    clock.advance(90)
    second.on_message(heartbeat(0))
    await settle(lambda: stream.stats.heartbeats == 2)
    assert damage[-1] == (last_good, clock.now)  # and is closed by the first message of the new connection
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_stale_book_triggers_unsubscribe_and_subscribe_of_level2(fast_backoff: None) -> None:
    stream = CoinbaseMarketStream([P], FakeTransport, clock=Clock())
    task = asyncio.create_task(stream.run())
    await settle(lambda: len(FakeTransport.instances) == 1)
    t = FakeTransport.instances[0]
    t.on_message(l2(0, "snapshot", [("100", "5")], [("101", "3")]))
    t.on_message(l2(1, "update", [("102", "1")], []))  # crosses the book
    await settle(lambda: stream.stats.resubscribes == 1)
    assert ("unsubscribe", ((P,), ("level2",))) in t.calls and ("subscribe", ((P,), ("level2",))) in t.calls
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_first_connection_failures_are_retried(fast_backoff: None) -> None:
    attempts = {"n": 0}

    class Flaky(FakeTransport):
        def open(self) -> None:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise ConnectionError("network down")
            super().open()

    stream = CoinbaseMarketStream([P], Flaky, clock=Clock())
    task = asyncio.create_task(stream.run())
    await settle(lambda: attempts["n"] == 3 and any(c[0] == "subscribe" for c in FakeTransport.instances[-1].calls))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --------------------------------------------------------------------------- live (manual)


@pytest.mark.live
async def test_live_public_feed_for_a_few_seconds() -> None:
    stream = CoinbaseMarketStream([P])
    task = asyncio.create_task(stream.run())
    await settle(lambda: stream.stats.heartbeats >= 3 and not stream.books[P].stale, timeout=20.0)
    spread = stream.spread_bps(P)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert spread is not None and 0 < spread < 50
    assert stream.stats.gaps == 0 and stream.stats.parse_errors == 0 and not stream.stats.errors
    assert stream.books[P].best_bid() < stream.books[P].best_ask()  # type: ignore[operator]
