"""The app in the tick fill mode (``dry_run.fill_model.mode: tick``): the brackets of every book follow the trades the
stream forwards and the order book, and what they fill is booked by the same ledger / account path as in the bar mode.

Proves: the trade callback reaches the stream (and only in this mode); the task that books the fills exists (and only
in this mode); a stop and a take profit close a position as a ``closed_trades`` row with the right reason, the loss
reaches the account guards, ``free + reserved`` stays exact; a stop that waits for a usable book is retried by the
periodic pump; a restart in the middle of a partly filled bracket rebuilds the account and the simulator and goes on.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from modules.config import BookConfig, FillModelConfig
from modules.market.orderbook import LevelUpdate
from modules.market.ws_feed import Trade
from tests.app_fixtures import PRODUCTS, Rig, make_cfg, make_rig, until
from tests.test_app_books import feed_bars, settle, snapshot
from tests.test_decision_engine import WARM_A, frame

D = Decimal
BOOKS = (
    BookConfig(name="champion", decider="registry:champion"),
    BookConfig(name="buy_and_hold", decider="buy_and_hold"),
)


def tick_cfg(tmp: Path, mode: str = "tick") -> Any:
    return make_cfg(tmp, books=BOOKS, fill_model=FillModelConfig(mode=mode))  # type: ignore[arg-type]


def trade(rig: Rig, product: str, price: Decimal, size: str = "1000", tid: str = "t1", at_s: float = 0.0) -> Trade:
    return Trade(product, tid, rig.clock.now + timedelta(seconds=at_s), float(price), float(size))


def deliver(rig: Rig, *trades: Trade) -> None:
    """What the market stream does for a message of the ``market_trades`` channel."""
    assert rig.feed.on_trades_cb is not None, "the runtime did not hand the trade callback to the stream"
    rig.feed.on_trades_cb(list(trades))


def bids(rig: Rig, product: str, *levels: tuple[str, str]) -> None:
    rig.feed.books[product].apply_snapshot(
        [LevelUpdate("bid", D(p), D(q)) for p, q in levels] + [LevelUpdate("offer", D("1000000"), D(5))],
        rig.clock.now,
    )


async def open_champion(rig: Rig) -> tuple[str, Any]:
    await feed_bars(rig, range(WARM_A, WARM_A + 12))
    (pos,) = [p for p in rig.rt.ledger.long_positions("champion")][:1]
    assert pos.sl is not None and pos.tp is not None
    return pos.product_id, pos


async def test_the_tick_callback_and_the_booking_task_exist_in_the_tick_mode_only(tmp_path: Path) -> None:
    async with make_rig(tmp_path, cfg=tick_cfg(tmp_path), upto=None) as rig:
        assert rig.feed.on_trades_cb is not None and rig.feed.on_damage_cb is not None
        assert rig.rt.manager.tick_mode and "tick_fills" in {s.name for s in rig.rt.task_specs()}
        assert all(b.shadow.tick_mode for b in rig.rt.books)
        assert rig.rt.manager.status()["champion"]["fill_mode"] == "tick"
    async with make_rig(tmp_path / "bar", cfg=tick_cfg(tmp_path / "bar", "bar"), upto=None) as rig:
        assert rig.feed.on_trades_cb is None  # the factory of the bar mode never sees a trade callback
        assert not rig.rt.manager.tick_mode and "tick_fills" not in {s.name for s in rig.rt.task_specs()}
        assert rig.rt.manager.status()["champion"]["fill_mode"] == "bar"
        rig.rt.manager.on_trades([])  # harmless


async def test_a_stop_through_the_trades_closes_the_position_through_the_ledger_path(tmp_path: Path) -> None:
    async with make_rig(tmp_path, cfg=tick_cfg(tmp_path)) as rig:
        product, pos = await open_champion(rig)
        before = rig.book_named("champion").account.balances.total("EUR")
        base = product.split("-")[0]
        bids(rig, product, (str(pos.sl - 1), "50"))
        deliver(rig, trade(rig, product, pos.sl - D("0.5")))
        await rig.rt.manager.settle_ticks()
        (row,) = [t for t in await rig.rt.ledger.closed_trades("champion") if t.product_id == product]
        assert row.exit_reason == "SL" and row.net_quote < 0 and row.entry_size == pos.base_qty
        assert row.exit_avg_price < pos.sl and row.model_version == "stub"
        bb = rig.book_named("champion").account.balances
        assert bb.reserved(base) == 0 and bb.total(base) == 0 and bb.total("EUR") > before - D("100")
        assert all(bb.free(c) >= 0 and bb.reserved(c) >= 0 for c in ("EUR", "BTC", "SOL"))
        assert not rig.rt.ledger.view("champion", product).is_long
        assert any(r.guard == "daily_open" for r in rig.book_named("champion").guards.state_rows())  # the loss counts
        assert rig.rt.manager.status()["champion"]["tick"]["sl_fills"] == 1  # type: ignore[index]


async def test_a_take_profit_through_the_trades_closes_with_the_maker_fee(tmp_path: Path) -> None:
    async with make_rig(tmp_path, cfg=tick_cfg(tmp_path)) as rig:
        product, pos = await open_champion(rig)
        deliver(rig, trade(rig, product, pos.tp + D("1"), size="100000", at_s=120))  # after the bar of the fill
        await rig.rt.manager.settle_ticks()
        (row,) = [t for t in await rig.rt.ledger.closed_trades("champion") if t.product_id == product]
        assert row.exit_reason == "TP" and row.exit_avg_price == pos.tp and row.net_quote > 0
        assert row.exit_fees == pos.tp * pos.base_qty * D("0.0025")  # the maker fee of the account (25 bps)


async def test_the_booking_task_books_the_fills_by_itself(tmp_path: Path) -> None:
    async with make_rig(tmp_path, cfg=tick_cfg(tmp_path)) as rig:
        product, pos = await open_champion(rig)
        rig.start()
        bids(rig, product, (str(pos.sl - 1), "50"))
        deliver(rig, trade(rig, product, pos.sl - D("0.5")))
        await until(lambda: not rig.rt.ledger.view("champion", product).is_long)
        trades = await rig.rt.ledger.closed_trades("champion")
        assert [t.exit_reason for t in trades if t.product_id == product] == ["SL"]
        assert await rig.stop() == 0


async def test_a_stop_without_a_usable_book_is_retried_by_the_periodic_pump(tmp_path: Path) -> None:
    async with make_rig(tmp_path, cfg=tick_cfg(tmp_path)) as rig:
        product, pos = await open_champion(rig)
        rig.feed.books[product].mark_stale("sequence gap")
        deliver(rig, trade(rig, product, pos.sl - D("0.5")))
        await rig.rt.manager.settle_ticks()
        assert rig.rt.ledger.view("champion", product).is_long  # triggered, nothing to sell into: not invented
        bids(rig, product, (str(pos.sl - 2), "50"))
        await rig.rt.pump_tickers({})  # what the ticker loop does every second
        assert not rig.rt.ledger.view("champion", product).is_long
        (row,) = [t for t in await rig.rt.ledger.closed_trades("champion") if t.product_id == product]
        assert row.exit_reason == "SL"


async def test_a_gap_reported_by_the_stream_holds_the_trades_of_the_open_positions(tmp_path: Path) -> None:
    async with make_rig(tmp_path, cfg=tick_cfg(tmp_path)) as rig:
        product, pos = await open_champion(rig)
        assert rig.feed.on_damage_cb is not None
        now = rig.clock.now
        rig.feed.on_damage_cb(now - timedelta(seconds=30), now)
        assert len(rig.rt.damage) == 1  # the runtime still counts the damage (status, ops)
        deliver(rig, trade(rig, product, pos.sl - D("0.5")))  # held: it would be judged against a hole
        await rig.rt.manager.settle_ticks()
        assert rig.rt.ledger.view("champion", product).is_long
        assert rig.rt.manager["champion"].gateway.tick_stats["gaps_resolved"] == 0


async def test_a_restart_in_the_middle_of_a_partly_filled_bracket_goes_on(tmp_path: Path) -> None:
    cfg = tick_cfg(tmp_path)
    async with make_rig(tmp_path, cfg=cfg) as first:
        product, pos = await open_champion(first)
        part = (pos.base_qty * 2).quantize(D("0.00001"))  # a trade whose haircut share is about half the position
        deliver(first, trade(first, product, pos.tp + D("1"), size=str(part), at_s=120))
        await first.rt.manager.settle_ticks()
        left = first.rt.ledger.view("champion", product)
        assert left.is_long and 0 < left.base_qty < pos.base_qty  # sold in part
        first.rt.request_stop("test")
        await first.rt._shutdown()  # noqa: SLF001  (a clean stop: brackets are NOT cancelled)
        before = snapshot(first)
        assert before["champion"]["reserved"][product.split("-")[0]] == left.base_qty
    async with make_rig(tmp_path, cfg=cfg, upto=WARM_A + 12) as second:
        assert snapshot(second) == before  # balances, reservations, fees, realized PnL, positions: identical
        gateway = second.rt.manager["champion"].gateway
        assert gateway.queued_fills == 0 and set(gateway._brackets) >= {product}  # noqa: SLF001
        sim = gateway._tick.sim  # type: ignore[union-attr]  # noqa: SLF001
        assert sim.remaining(f"champion|{product}") == left.base_qty
        bids(second, product, (str(pos.sl - 1), "500"))
        deliver(second, trade(second, product, pos.sl - D("0.5")))
        await second.rt.manager.settle_ticks()
        (row,) = [t for t in await second.rt.ledger.closed_trades("champion") if t.product_id == product]
        assert row.exit_reason == "SL" and row.entry_size == pos.base_qty and row.exit_size == pos.base_qty
        sells = [f for f in await second.rt.ledger.book_fills("champion") if f.side == "SELL"]
        fills = [f for f in sells if f.product_id == product]
        assert len({f.trade_id for f in fills}) == len(fills) == 2  # the part before the restart and the rest after
        bb = second.book_named("champion").account.balances
        assert bb.reserved(product.split("-")[0]) == 0 and all(bb.free(c) >= 0 for c in ("EUR", "BTC", "SOL"))


async def test_a_restart_in_the_tick_mode_resolves_the_downtime_with_the_bars(tmp_path: Path) -> None:
    """The bars of the downtime are the only data there is: in the tick mode they still resolve a restored bracket."""
    cfg = tick_cfg(tmp_path)
    async with make_rig(tmp_path, cfg=cfg) as first:
        product, pos = await open_champion(first)
        first.rt.request_stop("test")
        await first.rt._shutdown()  # noqa: SLF001
    history = {p: frame(p).iloc[: WARM_A + 20].copy() for p in PRODUCTS}
    col = history[product].columns.get_loc("low")
    history[product].iloc[WARM_A + 15, col] = float(pos.sl) - 3.0  # the downtime crossed the stop
    async with make_rig(tmp_path, cfg=cfg, history_frames=history) as second:
        (row,) = [t for t in await second.rt.ledger.closed_trades("champion") if t.product_id == product]
        assert row.exit_reason == "SL" and row.gap_resolved
        sim = second.rt.manager["champion"].gateway._tick.sim  # type: ignore[union-attr]  # noqa: SLF001
        held = [f"champion|{p.product_id}" for p in second.rt.ledger.long_positions("champion")]
        assert sorted(sim.open_keys()) == sorted(held)
        await settle(second)


async def test_the_manager_keeps_the_tape_the_gateways_look_back_into(tmp_path: Path) -> None:
    async with make_rig(tmp_path, cfg=tick_cfg(tmp_path)) as rig:
        product, pos = await open_champion(rig)
        t = trade(rig, product, pos.sl + D("5"), tid="seen")
        deliver(rig, t)
        rig_ = rig.rt.manager["champion"].gateway._tick  # type: ignore[union-attr]  # noqa: SLF001
        assert [x.trade_id for x in rig_.tape.since(product, t.ts)] == ["seen"]  # type: ignore[union-attr]


async def test_the_periodic_pump_closes_a_data_gap_and_replays_the_held_trades(tmp_path: Path) -> None:
    async with make_rig(tmp_path, cfg=tick_cfg(tmp_path)) as rig:
        product, pos = await open_champion(rig)
        now = rig.clock.now
        rig.feed.on_damage_cb(now - timedelta(seconds=30), now)  # type: ignore[misc]
        bids(rig, product, (str(pos.sl - 1), "50"))
        deliver(rig, trade(rig, product, pos.sl - D("0.5")))  # held while the gap is open
        await rig.rt.manager.settle_ticks()
        assert rig.rt.ledger.view("champion", product).is_long
        rig.clock.now = now + timedelta(minutes=10)  # the candles of the gap are final
        await rig.rt.pump_tickers({})  # the gap closes (no stop in it) and the held trade runs
        assert rig.rt.manager["champion"].gateway.tick_stats["gaps_resolved"] == 1
        (row,) = [t for t in await rig.rt.ledger.closed_trades("champion") if t.product_id == product]
        assert row.exit_reason == "SL"
