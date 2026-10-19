"""BracketSimulator and the tick face of the fill model: TP / SL / same-event rules on trades and books, and the
resolution of a data gap with the 1m candles (spec sez. 5.1.2, 5.1.3, 5.3 punto 7).

Everything is synthetic: trades are built by hand, the books are real ``LocalOrderBook`` objects, the candles come from
a fake ``CandleSource``, the clock is a settable fake. Nothing touches the network or the disk.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from modules.dryrun import bracket_sim as bracket_module
from modules.dryrun.bracket_sim import BracketFill, BracketSimulator
from modules.dryrun.fill_model import BarFillModel, FillModelParams, TickFillModel
from modules.execution.rounding import floor_to
from modules.execution.spot import SpotRegistry
from modules.ledger.positions import PositionView
from modules.market.orderbook import LevelUpdate, LocalOrderBook
from modules.market.ws_feed import Trade
from tests.coverage_fixtures import SOL, T0, Clock
from tests.gateway_fixtures import FeeSourceStub

D = Decimal
PRODUCT = "SOL-EUR"
SIZE, TP, SL = D("0.024"), D("108.96"), D("83.57")
MAKER, TAKER = D("0.006"), D("0.012")  # the assumed (fallback) tier: 60 / 120 bps
BRACKET_AT = T0 + timedelta(minutes=1)
MIN = timedelta(minutes=1)
BRACKET_PY = Path(bracket_module.__file__)


def trade(price: str, size: str = "1", at: float = 2.0, tid: str = "t", product: str = PRODUCT) -> Trade:
    return Trade(product, tid, T0 + timedelta(minutes=at), float(price), float(size))


def make_book(bids: list[tuple[str, str]], asks: list[tuple[str, str]] | None = None) -> LocalOrderBook:
    book = LocalOrderBook(PRODUCT)
    levels = [LevelUpdate("bid", D(p), D(q)) for p, q in bids]
    levels += [LevelUpdate("offer", D(p), D(q)) for p, q in asks or [("120", "5")]]
    book.apply_snapshot(levels)
    return book


DEEP_BOOK = [("83.00", "1.0"), ("82.00", "10")]


class Candles:
    """A ``CandleSource`` over a dict minute -> (open, high, low, close); records the requests, can fail."""

    def __init__(self, rows: dict[datetime, tuple[float, float, float, float]] | None = None) -> None:
        self.rows = rows or {}
        self.calls: list[tuple[str, int, int]] = []
        self.fail: Exception | None = None

    def fetch(self, product_id: str, start_s: int, end_s: int) -> list[dict[str, Any]]:
        self.calls.append((product_id, start_s, end_s))
        if self.fail is not None:
            raise self.fail
        out = []
        for minute, (o, h, lo, c) in sorted(self.rows.items()):
            sec = int(minute.timestamp())
            if start_s <= sec < end_s:
                out.append({"start": str(sec), "open": str(o), "high": str(h), "low": str(lo), "close": str(c),
                            "volume": "1"})  # fmt: skip
        return out


@dataclass
class Env:
    sim: BracketSimulator
    clock: Clock
    candles: Candles
    books: dict[str, LocalOrderBook] = field(default_factory=dict)

    def open(self, book: str = "champion", **kw: Any) -> str:
        args: dict[str, Any] = {
            "book": book, "product_id": PRODUCT, "size": SIZE, "tp": TP, "sl": SL, "opened_at": T0,
            "bracket_at": BRACKET_AT,
        }  # fmt: skip
        return self.sim.track(**{**args, **kw})


def make(*, book: bool = True, params: FillModelParams | None = None, **kw: Any) -> Env:
    clock = Clock()
    candles = Candles()
    books = {PRODUCT: make_book(DEEP_BOOK)} if book else {}
    sim = BracketSimulator(
        SpotRegistry({PRODUCT: SOL}), FeeSourceStub(clock), clock, model=TickFillModel(params), candles=candles,
        books=books, **kw,
    )  # fmt: skip
    return Env(sim, clock, candles, books)


def fee(price: Decimal, size: Decimal, rate: Decimal) -> Decimal:
    return price * size * rate


# ===================================================================================== the tick face


def test_tick_face_boundaries_are_strict_where_the_bar_face_is() -> None:
    m = TickFillModel()
    assert not m.tp_touched(D("108.96"), D("108.96")) and m.tp_touched(D("108.97"), D("108.96"))
    assert m.stop_triggered(D("83.57"), D("83.57")) and m.stop_triggered(D("83.56"), D("83.57"))
    assert not m.stop_triggered(D("83.58"), D("83.57"))
    bar = BarFillModel()
    assert bar.tp_touched(108.96, 108.96) is m.tp_touched(D("108.96"), D("108.96"))  # the very same kernel


def test_tick_face_haircut_and_stop_slip_follow_the_parameters() -> None:
    m = TickFillModel(FillModelParams(queue_haircut=0.5, stop_extra_slip_bps=20))
    assert m.tp_fill_qty(D("0.100"), D("1"), D("0.001")) == D("0.050")
    assert m.tp_fill_qty(D("0.100"), D("0.020"), D("0.001")) == D("0.020")  # never more than what is left
    assert m.tp_fill_qty(D("0.0019"), D("1"), D("0.001")) == D("0.000")  # below one increment: nothing
    assert m.stop_exit_price(D("100.00"), D("0.01")) == D("99.80")
    assert m.stop_exit_price(D("100.017"), D("0.01")) == D("99.81")  # rounded down, never up


# ====================================================================================== take profit


def test_take_profit_needs_a_trade_strictly_above_and_gets_a_haircut_share_at_the_tp_price() -> None:
    env = make()
    key = env.open()
    assert env.sim.on_trade(trade("108.96", "5")) == []  # a print AT the TP does not fill it
    (first,) = env.sim.on_trade(trade("108.97", "0.040"))  # haircut 0.25 x 0.040 = 0.010
    assert (first.reason, first.price, first.size, first.liquidity) == ("TP", TP, D("0.010"), "maker")
    assert first.fee == fee(TP, D("0.010"), MAKER) and first.remaining == D("0.014") and not first.closed
    assert env.sim.remaining(key) == D("0.014")
    (second,) = env.sim.on_trade(trade("109.50", "5"))  # cap 1.25, only what is left goes
    assert second.size == D("0.014") and second.closed and env.sim.open_keys() == []
    assert env.sim.on_trade(trade("200", "5")) == []  # a closed position is not followed any more


def test_take_profit_does_not_exist_before_the_bracket_is_placed() -> None:
    env = make()
    key = env.open(bracket_at=None)
    assert env.sim.on_trade(trade("120", "5")) == []  # no bracket yet: only the virtual stop acts
    env.sim.arm_bracket(key, T0 + 5 * MIN)
    assert env.sim.on_trade(trade("120", "5", at=3.0)) == []  # a print from before the bracket does not count
    assert len(env.sim.on_trade(trade("120", "5", at=6.0))) == 1


def test_the_virtual_stop_acts_even_before_the_bracket_exists() -> None:
    env = make()
    env.open(bracket_at=None)
    (fill,) = env.sim.on_trade(trade("83.00", "1"))
    assert fill.reason == "SL" and fill.closed


# ====================================================================================== stop


def test_stop_triggers_at_or_below_and_walks_the_bids_with_the_extra_slip_and_the_taker_fee() -> None:
    env = make()
    env.books[PRODUCT].apply_snapshot(
        [
            LevelUpdate("bid", D("91.00"), D("0.010")),
            LevelUpdate("bid", D("90.50"), D("0.020")),
            LevelUpdate("offer", D("91.20"), D("1")),
        ]  # fmt: skip
    )
    env.open()
    assert env.sim.on_trade(trade("83.58")) == []  # one tick above the stop
    (fill,) = env.sim.on_trade(trade("83.57"))  # AT the stop triggers
    vwap = (D("0.010") * D("91.00") + D("0.014") * D("90.50")) / D("0.024")
    assert fill.reason == "SL" and fill.liquidity == "taker" and fill.size == SIZE and fill.closed
    assert fill.price == floor_to(vwap * D("0.999"), D("0.01")) == D("90.61")  # x = 10 bps off the VWAP
    assert fill.fee == fee(fill.price, SIZE, TAKER) and not fill.gap_resolved


def test_same_event_tp_sl_is_sl() -> None:
    for batch in (
        [trade("109.00", "5", tid="a"), trade("83.00", "1", tid="b")],  # the TP print first
        [trade("83.00", "1", tid="b"), trade("109.00", "5", tid="a")],  # the stop print first
    ):
        env = make()
        env.open()
        fills = env.sim.on_trades(batch)
        assert [f.reason for f in fills] == ["SL"] and fills[0].closed  # never a TP fill from the same event
        assert env.sim.stats.tp_fills == 0 and env.sim.stats.sl_fills == 1


def test_a_triggered_stop_cancels_the_take_profit() -> None:
    env = make(book=False)  # no book: the stop triggers but cannot be liquidated yet
    key = env.open()
    assert env.sim.on_trade(trade("83.00")) == []
    assert env.sim.on_trade(trade("120", "50", at=3.0)) == []  # TP is gone: a print above it fills nothing
    assert env.sim.remaining(key) == SIZE


def test_a_stale_or_empty_book_fills_nothing_and_the_next_book_event_retries() -> None:
    env = make(book=False)
    env.open()
    stale = LocalOrderBook(PRODUCT)  # no snapshot yet: stale
    assert env.sim.on_trade(trade("83.00")) == [] and env.sim.on_book(stale) == []
    no_bids = make_book([], [("120", "1")])
    assert env.sim.on_book(no_bids) == []
    (fill,) = env.sim.on_book(make_book(DEEP_BOOK))
    assert fill.reason == "SL" and fill.closed and fill.price == floor_to(D("83.00") * D("0.999"), D("0.01"))


def test_a_thin_book_fills_a_part_and_the_rest_follows_on_later_books() -> None:
    env = make(book=False)
    key = env.open()
    env.sim.on_book(make_book([("90.00", "0.010")]))
    (a,) = env.sim.on_trade(trade("83.00"))
    assert a.size == D("0.010") and a.remaining == D("0.014") and not a.closed
    (b,) = env.sim.on_book(make_book([("89.00", "5")]))
    assert b.size == D("0.014") and b.closed and env.sim.remaining(key) is None
    assert a.size + b.size == SIZE


def test_a_partial_take_profit_is_followed_by_a_stop_on_the_rest() -> None:
    env = make()
    env.open()
    (tp,) = env.sim.on_trade(trade("109.00", "0.040"))
    (sl,) = env.sim.on_trade(trade("83.00", "1", at=3.0))
    assert tp.size + sl.size == SIZE and sl.size == D("0.014") and sl.closed


# ================================================================================ positions and books


def test_positions_of_other_products_and_other_books_are_independent() -> None:
    env = make()
    a, b = env.open("champion"), env.open("challenger", sl=D("90.00"))
    assert env.sim.on_trades([trade("120", "5", product="BTC-EUR")]) == []  # a product we hold nothing of
    fills = env.sim.on_trade(trade("89.00"))  # below challenger's stop only
    assert [(f.key, f.reason) for f in fills] == [(b, "SL")] and env.sim.open_keys() == [a]
    assert env.sim.on_trade(trade("109.5", "0.040"))[0].key == a


def test_track_validates_and_refuses_a_second_position_of_the_same_book_and_product() -> None:
    env = make()
    env.open()
    with pytest.raises(ValueError, match="already tracked"):
        env.open()
    for bad in ({"size": D(0)}, {"tp": D("80")}, {"sl": D(0)}):
        with pytest.raises(ValueError, match="invalid bracket"):
            env.open("other", **bad)


def test_track_view_follows_a_ledger_position_and_refuses_a_flat_one() -> None:
    env = make()
    view = PositionView(
        book="champion", product_id=PRODUCT, side="LONG", base_qty=SIZE, opened_at=T0, tp=TP, sl=SL,
    )  # fmt: skip
    key = env.sim.track_view(view, BRACKET_AT)
    assert env.sim.remaining(key) == SIZE
    with pytest.raises(ValueError, match="not a long position"):
        env.sim.track_view(PositionView(book="champion", product_id=PRODUCT), None)
    env.sim.untrack(key)
    assert env.sim.open_keys() == []


@settings(max_examples=60, deadline=None)
@given(
    st.lists(
        st.lists(st.tuples(st.integers(8000, 11500), st.integers(1, 3000)), min_size=1, max_size=4),
        min_size=1,
        max_size=12,
    )
)
def test_a_position_is_never_oversold_whatever_the_tape(tape: list[list[tuple[int, int]]]) -> None:
    env = make()
    env.open()
    sold = D(0)
    fills: list[BracketFill] = []
    for i, batch in enumerate(tape):
        trades = [
            trade(str(D(p) / 100), str(D(s) / 1000), at=2.0 + i / 10, tid=f"{i}-{j}") for j, (p, s) in enumerate(batch)
        ]
        fills += env.sim.on_trades(trades)
        if i % 3 == 0:
            fills += env.sim.on_book(make_book(DEEP_BOOK))
    for f in fills:
        assert f.size > 0 and f.remaining >= 0
        sold += f.size
        assert f.price == TP if f.reason == "TP" else f.price > 0
        assert f.fee == fee(f.price, f.size, MAKER if f.liquidity == "maker" else TAKER)
    assert sold <= SIZE and (env.sim.remaining("champion|SOL-EUR") or D(0)) == SIZE - sold


# ======================================================================================== data gap


def candle(
    minute: float, o: float, h: float, lo: float, c: float
) -> tuple[datetime, tuple[float, float, float, float]]:
    return T0 + timedelta(minutes=minute), (o, h, lo, c)


def gap_env(rows: list[Any] | None = None, **kw: Any) -> Env:
    env = make(**kw)
    env.candles.rows = dict(rows or [])
    return env


GAP_START = T0 + timedelta(minutes=2, seconds=30)
GAP_END = T0 + timedelta(minutes=5, seconds=10)  # candles needed: minutes 2..5, final at minute 6 + S (120 s)


async def test_gap_resolution() -> None:
    """The WS is down while the price crosses the stop: the candle of the gap closes the position, flagged."""
    env = gap_env([candle(2, 95, 96, 94, 95), candle(3, 90, 90, 80, 81), candle(4, 81, 85, 80, 84)])
    key = env.open()
    env.sim.on_damage(GAP_START, None)
    assert env.sim.on_trade(trade("100", at=2.8)) == []  # held: it would be judged against a hole
    env.sim.on_damage(GAP_START, GAP_END)
    env.clock.now = T0 + timedelta(minutes=7)
    assert await env.sim.resolve_gaps() == [] and env.candles.calls == []  # the candle of minute 5 is not final yet
    env.clock.now = T0 + timedelta(minutes=8)
    (fill,) = await env.sim.resolve_gaps()
    assert env.candles.calls == [(PRODUCT, int((T0 + 2 * MIN).timestamp()), int((T0 + 6 * MIN).timestamp()))]
    assert (fill.key, fill.reason, fill.gap_resolved, fill.liquidity, fill.closed) == (key, "SL", True, "taker", True)
    assert fill.at == T0 + 3 * MIN and fill.size == SIZE
    want = BarFillModel().sl_exit(float(SL), 90.0, 3.0)  # min(stop, open) less spread/2, slip and the extra stop slip
    assert fill.price == floor_to(D(repr(want)), D("0.01")) and fill.fee == fee(fill.price, SIZE, TAKER)
    assert not env.sim.gap_pending() and env.sim.stats.gaps_resolved == 1


async def test_a_gap_stop_uses_the_open_when_the_candle_opens_below_the_stop() -> None:
    env = gap_env([candle(3, 70, 72, 65, 66)])
    env.open()
    env.sim.on_damage(GAP_START, GAP_END)
    env.clock.now = T0 + timedelta(minutes=8)
    (fill,) = await env.sim.resolve_gaps()
    assert fill.price == floor_to(D(repr(BarFillModel().sl_exit(float(SL), 70.0, 3.0))), D("0.01"))
    assert fill.price < SL


async def test_the_gap_stop_uses_the_configured_spread_not_a_stale_book() -> None:
    env = gap_env([candle(3, 90, 90, 80, 81)], gap_spread_bps=10.0)
    env.open()
    env.sim.on_damage(GAP_START, GAP_END)
    env.clock.now = T0 + timedelta(minutes=8)
    (fill,) = await env.sim.resolve_gaps()  # the book of the test is 36 % wide: it must not leak into the price
    assert fill.price == floor_to(D(repr(BarFillModel().sl_exit(float(SL), 90.0, 10.0))), D("0.01"))


async def test_the_take_profit_is_never_credited_from_a_gap_candle() -> None:
    env = gap_env([candle(3, 100, 130, 99, 120)])  # the price flew over the TP and never came near the stop
    key = env.open()
    env.sim.on_damage(GAP_START, GAP_END)
    env.clock.now = T0 + timedelta(minutes=8)
    assert await env.sim.resolve_gaps() == [] and env.sim.remaining(key) == SIZE
    assert env.sim.stats.tp_fills == 0 and not env.sim.gap_pending()


async def test_a_candle_with_both_barriers_is_a_stop() -> None:
    env = gap_env([candle(3, 100, 130, 80, 120)])
    env.open()
    env.sim.on_damage(GAP_START, GAP_END)
    env.clock.now = T0 + timedelta(minutes=8)
    (fill,) = await env.sim.resolve_gaps()
    assert fill.reason == "SL"


async def test_held_trades_are_replayed_in_order_once_the_gap_is_resolved() -> None:
    env = gap_env([candle(3, 100, 101, 99, 100)])  # nothing crossed in the hole
    env.open("champion")
    env.open("challenger", sl=D("90.00"))
    env.sim.on_damage(GAP_START, None)
    assert env.sim.on_trade(trade("89.50", at=2.9)) == []  # the stop of "challenger", held
    assert env.sim.on_trade(trade("120", "9", at=3.0)) == []
    env.sim.on_damage(GAP_START, GAP_END)
    env.clock.now = T0 + timedelta(minutes=8)
    fills = await env.sim.resolve_gaps()
    # replayed in order: the stop print triggers "challenger" (its TP is cancelled); "champion" gets its TP share
    assert [(f.key, f.reason) for f in fills] == [("challenger|SOL-EUR", "SL"), ("champion|SOL-EUR", "TP")]
    assert fills[1].size == D("0.024")  # 0.25 x 9 = 2.25 >> the position


async def test_a_failed_download_keeps_the_gap_and_the_trades_and_retries() -> None:
    env = gap_env([candle(3, 90, 90, 80, 81)])
    env.open()
    env.sim.on_damage(GAP_START, GAP_END)
    env.sim.on_trades([trade("100", at=5.5)])
    env.clock.now = T0 + timedelta(minutes=8)
    env.candles.fail = ConnectionError("rest down")
    assert await env.sim.resolve_gaps() == [] and env.sim.gap_pending() and env.sim.stats.gap_fetch_failures == 1
    env.candles.fail = None
    (fill,) = await env.sim.resolve_gaps()
    assert fill.gap_resolved and not env.sim.gap_pending()


async def test_a_gap_that_ended_before_the_position_opened_neither_holds_nor_changes_it() -> None:
    env = gap_env([candle(3, 90, 90, 80, 81)])
    env.sim.on_damage(GAP_START, GAP_END)
    key = env.open(opened_at=T0 + 6 * MIN, bracket_at=T0 + 6 * MIN)
    (fill,) = env.sim.on_trade(trade("109.5", "0.040", at=7.0))  # not held: the gap was over when it opened
    assert fill.reason == "TP"
    env.clock.now = T0 + timedelta(minutes=8)
    assert await env.sim.resolve_gaps() == [] and env.candles.calls == [] and env.sim.remaining(key) == D("0.014")
    assert not env.sim.gap_pending()


async def test_a_position_opened_inside_the_gap_is_resolved_from_its_opening_minute() -> None:
    env = gap_env([candle(2, 95, 96, 60, 70), candle(4, 85, 86, 80, 82)])
    env.open(opened_at=T0 + timedelta(minutes=3, seconds=30), bracket_at=None)
    env.sim.on_damage(GAP_START, GAP_END)
    env.clock.now = T0 + timedelta(minutes=8)
    (fill,) = await env.sim.resolve_gaps()
    assert fill.at == T0 + 4 * MIN  # the crash of minute 2 is from before the position existed


async def test_a_gap_with_no_candle_source_stays_open() -> None:
    env = make()
    env.sim._candles = None
    env.open()
    env.sim.on_damage(GAP_START, GAP_END)
    env.clock.now = T0 + timedelta(minutes=8)
    assert await env.sim.resolve_gaps() == [] and env.sim.gap_pending()


def test_the_same_damage_reported_twice_is_one_gap() -> None:
    env = make()
    env.open()
    env.sim.on_damage(GAP_START, None)
    env.sim.on_damage(GAP_START, GAP_END)
    env.sim.on_damage(GAP_START, GAP_END)
    assert len(env.sim._gaps) == 1 and env.sim._gaps[0].end == GAP_END


# ============================================================================== tick vs bar, structure


def test_fill_model_tick_vs_bar() -> None:
    """The same stop liquidated by the tick face (a real book) and by the bar face (spread + slippage + x): the bar
    model is the more pessimistic of the two; the tick take profit is the bar take profit. Documented tolerance: the
    gap between the two is at most the slippage buffer plus half the spread (here 5 + 1.5 bps) of the stop price."""
    env = make()
    env.books[PRODUCT].apply_snapshot(
        [LevelUpdate("bid", D("83.49"), D("1")), LevelUpdate("offer", D("83.51"), D("1"))]
    )  # the 3 bps spread the bar model assumes, around 83.50: the stop (83.57) is crossed
    env.open()
    (tick,) = env.sim.on_trade(trade("83.50"))
    bar = floor_to(D(repr(BarFillModel().sl_exit(float(SL), 83.50, 3.0))), D("0.01"))
    assert bar <= tick.price  # pessimistic or equal
    assert (tick.price - bar) / SL * 10_000 <= 6.5
    env2 = make()
    env2.open()
    (tp,) = env2.sim.on_trade(trade("109.00", "9"))
    assert tp.price == TP  # both faces fill the limit at its price


def test_the_simulator_cannot_send_an_order_anywhere() -> None:
    """Structural: no SDK, no exchange, no order endpoint, no ticket: it only produces ``BracketFill`` objects."""
    tree = ast.parse(BRACKET_PY.read_text(encoding="utf-8"))
    imported = {
        (n.module or "") if isinstance(n, ast.ImportFrom) else a.name
        for n in ast.walk(tree)
        for a in (n.names if isinstance(n, ast.Import | ast.ImportFrom) else [])
    } | {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not {m for m in imported if m.startswith("coinbase") or m.endswith("exchange")}
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
    }
    assert not names & {
        "create_order", "cancel_orders", "preview_order", "CoinbaseExchange", "RESTClient", "build_order_body",
        "CoverageTicket", "authorize", "BalanceBook", "PositionLedger", "VirtualAccount",
    }  # fmt: skip
