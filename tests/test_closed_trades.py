"""``closed_trades`` of the ledger (spec sez. 5.2 VirtualTrade, sez. 6.3 D7): one row per closed position, written in
the transaction of the closing fill, in dry and in live, and a safe migration of the databases that exist."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from modules.dryrun.report import SqliteTradeSource
from modules.ledger.positions import (
    SCHEMA_VERSION,
    ClosedTrade,
    EntryPlan,
    FillRecord,
    LedgerModeMismatch,
    OrderRecord,
    PositionLedger,
)

D = Decimal
T0 = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
B, P = "champion", "SOL-EUR"
PLAN = EntryPlan(tp=D("108"), sl=D("92"), k_tp=4.0, k_sl=2.0, atr=D("4"))


@pytest.fixture
async def ledger(tmp_path: Path) -> AsyncIterator[PositionLedger]:
    lg = await PositionLedger.open(tmp_path / "state" / "chronos_dry.db", "dry")
    yield lg
    await lg.close()


def fill(trade_id: str, side: str, price: str, size: str, fee: str, at: datetime, cid: str = "c") -> FillRecord:
    return FillRecord(trade_id, B, cid, P, side, D(price), D(size), D(fee), "maker", at)  # type: ignore[arg-type]


async def order(lg: PositionLedger, cid: str, leg: str, side: str = "SELL") -> None:
    await lg.record_order(OrderRecord(cid, B, P, side, leg, "OPEN", T0, T0))  # type: ignore[arg-type]


async def open_long(lg: PositionLedger, size: str = "1", price: str = "100", fee: str = "0.25") -> None:
    await order(lg, "buy1", "entry", "BUY")
    rec = await lg.apply_entry_fill_recorded(fill("f-buy", "BUY", price, size, fee, T0, "buy1"), "dec1", PLAN)
    assert rec is not None


async def test_a_full_sale_writes_one_trade_with_the_numbers_of_the_fills(ledger: PositionLedger) -> None:
    await open_long(ledger)
    await order(ledger, "sell1", "exit")
    ledger.expect_exit(B, P, "HORIZON")
    t_exit = T0 + timedelta(minutes=90)
    await ledger.apply_exit_fill_recorded(fill("f-sell", "SELL", "103", "1", "0.5", t_exit, "sell1"))
    (t,) = await ledger.closed_trades(B)
    assert (t.trade_id, t.book, t.product_id, t.entry_decision_id) == ("f-sell", B, P, "dec1")
    assert (t.entry_avg_price, t.entry_size, t.entry_fees, t.entry_at) == (D(100), D(1), D("0.25"), T0)
    assert (t.exit_avg_price, t.exit_size, t.exit_fees) == (D(103), D(1), D("0.5"))
    assert (t.exit_at, t.exit_reason) == (t_exit, "HORIZON")
    assert (t.gross_quote, t.fees_quote, t.net_quote) == (D(3), D("0.75"), D("2.25"))
    assert t.net_bps == 225 and t.held_min == 90 and not t.partial_entry
    assert t.r_multiple == pytest.approx(2.25 / 8)  # risk to the stop: (100 - 92) x 1
    assert (t.tp, t.sl, t.k_tp, t.k_sl) == (D(108), D(92), 4.0, 2.0)
    assert not ledger.view(B, P).is_long


async def test_the_payload_is_the_virtual_trade_of_the_spec_and_the_report_reads_it(
    tmp_path: Path, ledger: PositionLedger
) -> None:
    ledger.set_trade_info(B, model_version="m@1", fill_model_version="fill@1")
    await open_long(ledger)
    await order(ledger, "bk", "bracket")
    await ledger.apply_exit_fill_recorded(fill("f-tp", "SELL", "108", "1", "0.3", T0 + timedelta(hours=2), "bk"))
    con = sqlite3.connect(tmp_path / "state" / "chronos_dry.db")
    (payload,) = con.execute("SELECT payload FROM closed_trades").fetchone()
    doc = json.loads(payload)
    assert doc["entry"] == {"avg_price": "100", "size": "1", "fees": "0.25", "at": "2026-10-17T12:00:00.000Z"}
    assert doc["exit"]["reason"] == "TP" and doc["pnl"]["net_quote"] == "7.45" and doc["held_min"] == 120
    assert doc["flags"] == {"gap_resolved": False, "partial_entry": False}
    assert (doc["model_version"], doc["fill_model_version"]) == ("m@1", "fill@1")
    con.close()
    loaded = SqliteTradeSource(tmp_path / "state" / "chronos_dry.db").load()
    assert [(x.book, x.product, x.exit_reason, x.net_quote) for x in loaded] == [(B, P, "TP", D("7.45"))]


@pytest.mark.parametrize(("price", "reason"), [("108", "TP"), ("109.5", "TP"), ("91.9", "SL"), ("95", "SL")])
async def test_a_bracket_leg_is_tp_or_sl_by_its_price(ledger: PositionLedger, price: str, reason: str) -> None:
    await open_long(ledger)
    await order(ledger, "bk", "bracket")
    ledger.expect_exit(B, P, "WATCHDOG")  # a hint never overrides what the bracket leg says
    await ledger.apply_exit_fill_recorded(fill("f", "SELL", price, "1", "0.1", T0 + timedelta(minutes=5), "bk"))
    assert (await ledger.closed_trades(B))[0].exit_reason == reason


@pytest.mark.parametrize(
    ("hint", "expected"),
    [(None, "SELL"), ("SELL", "SELL"), ("HORIZON", "HORIZON"), ("WATCHDOG", "WATCHDOG"), ("MANUAL", "MANUAL"),
     ("decider_says_so", "SELL")],
)  # fmt: skip
async def test_a_model_exit_carries_the_announced_reason(
    ledger: PositionLedger, hint: str | None, expected: str
) -> None:
    await open_long(ledger)
    await order(ledger, "x", "exit")
    if hint is not None:
        ledger.expect_exit(B, P, hint)
    await ledger.apply_exit_fill_recorded(fill("f", "SELL", "99", "1", "0.1", T0 + timedelta(minutes=5), "x"))
    assert (await ledger.closed_trades(B))[0].exit_reason == expected


async def test_a_withdrawn_or_used_hint_does_not_leak_to_the_next_trade(ledger: PositionLedger) -> None:
    await open_long(ledger)
    await order(ledger, "x", "exit")
    ledger.expect_exit(B, P, "HORIZON")
    ledger.clear_exit(B, P)
    await ledger.apply_exit_fill_recorded(fill("f1", "SELL", "99", "1", "0", T0 + timedelta(minutes=5), "x"))
    ledger.expect_exit(B, P, "HORIZON")
    await order(ledger, "buy2", "entry", "BUY")
    f2 = fill("f2", "BUY", "100", "1", "0", T0 + timedelta(hours=1), "buy2")
    await ledger.apply_entry_fill_recorded(f2, "d2", PLAN)
    await order(ledger, "y", "exit")
    await ledger.apply_exit_fill_recorded(fill("f3", "SELL", "99", "1", "0", T0 + timedelta(hours=2), "y"))
    await order(ledger, "buy3", "entry", "BUY")
    f4 = fill("f4", "BUY", "100", "1", "0", T0 + timedelta(hours=3), "buy3")
    await ledger.apply_entry_fill_recorded(f4, "d3", PLAN)
    await order(ledger, "z", "exit")
    await ledger.apply_exit_fill_recorded(fill("f5", "SELL", "99", "1", "0", T0 + timedelta(hours=4), "z"))
    assert [t.exit_reason for t in await ledger.closed_trades(B)] == ["SELL", "HORIZON", "SELL"]


async def test_partial_sales_make_one_trade_when_the_position_reaches_zero(ledger: PositionLedger) -> None:
    await open_long(ledger, size="2")
    await order(ledger, "p1", "exit")
    await ledger.apply_exit_fill_recorded(fill("s1", "SELL", "102", "0.5", "0.1", T0 + timedelta(minutes=10), "p1"))
    assert await ledger.closed_trades(B) == []  # still long
    await order(ledger, "p2", "exit")
    await ledger.apply_exit_fill_recorded(fill("s2", "SELL", "104", "1.5", "0.3", T0 + timedelta(minutes=20), "p2"))
    (t,) = await ledger.closed_trades(B)
    assert t.exit_size == D(2) and t.exit_avg_price == D("103.5") and t.exit_fees == D("0.4")
    assert t.gross_quote == D(7) and t.net_quote == D("6.35")  # 2 x (103.5 - 100) minus 0.25 + 0.4 of fees


async def test_a_dust_conversion_is_not_a_sale_and_writes_nothing(ledger: PositionLedger) -> None:
    await open_long(ledger, size="0.0001")
    await ledger.apply_exit_fill(B, P, D("0.0001"))
    await ledger.set_dust(B, P, D("0.0001"), D("0.01"))
    assert await ledger.closed_trades() == []


async def test_a_fill_booked_twice_writes_the_trade_and_calls_the_listener_once(ledger: PositionLedger) -> None:
    seen: list[ClosedTrade] = []
    ledger.add_close_listener(seen.append)
    await open_long(ledger)
    await order(ledger, "x", "exit")
    f = fill("same", "SELL", "101", "1", "0", T0 + timedelta(minutes=1), "x")
    assert await ledger.apply_exit_fill_recorded(f) is not None
    assert await ledger.apply_exit_fill_recorded(f) is None
    assert len(await ledger.closed_trades()) == 1 and [t.trade_id for t in seen] == ["same"]


async def test_a_listener_that_raises_does_not_break_the_close_or_the_others(ledger: PositionLedger) -> None:
    seen: list[str] = []

    def bad(_t: ClosedTrade) -> None:
        raise RuntimeError("listener bug")

    ledger.add_close_listener(bad)
    ledger.add_close_listener(lambda t: seen.append(t.trade_id))
    await open_long(ledger)
    await order(ledger, "x", "exit")
    await ledger.apply_exit_fill_recorded(fill("f", "SELL", "101", "1", "0", T0 + timedelta(minutes=1), "x"))
    assert seen == ["f"] and not ledger.view(B, P).is_long


async def test_the_trade_is_written_in_the_transaction_of_the_fill(tmp_path: Path) -> None:
    path = tmp_path / "state" / "chronos_dry.db"
    lg = await PositionLedger.open(path, "dry")
    await open_long(lg)
    await order(lg, "x", "exit")
    await lg.connection.execute("DROP TABLE closed_trades")  # the trade cannot be written: everything rolls back
    with pytest.raises(Exception, match="closed_trades"):
        await lg.apply_exit_fill_recorded(fill("f", "SELL", "101", "1", "0", T0 + timedelta(minutes=1), "x"))
    assert lg.view(B, P).is_long and await lg.fills_for("x") == []
    await lg.close()


async def test_the_live_ledger_has_the_same_table(tmp_path: Path) -> None:
    lg = await PositionLedger.open(tmp_path / "chronos_live.db", "live")
    await lg.apply_entry_fill_recorded(fill("b", "BUY", "100", "1", "0", T0, "buy1"), "d", PLAN)
    await lg.apply_exit_fill_recorded(fill("s", "SELL", "90", "1", "0", T0 + timedelta(minutes=3), "x"))
    assert [t.net_quote for t in await lg.closed_trades("champion")] == [D(-10)]
    await lg.close()


async def test_meta_and_book_fills(ledger: PositionLedger) -> None:
    assert await ledger.meta_get("k") is None
    await ledger.meta_set("k", "1")
    await ledger.meta_set("k", "2")
    assert await ledger.meta_get("k") == "2" and await ledger.meta_get("schema_version") == str(SCHEMA_VERSION)
    await open_long(ledger)
    await order(ledger, "x", "exit")
    await ledger.apply_exit_fill_recorded(fill("s", "SELL", "101", "1", "0", T0 + timedelta(minutes=1), "x"))
    assert [f.trade_id for f in await ledger.book_fills(B)] == ["f-buy", "s"] and await ledger.book_fills("other") == []


# ------------------------------------------------------------------------------------------ migration

OLD_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE positions (book TEXT NOT NULL, product_id TEXT NOT NULL, side TEXT NOT NULL, base_qty TEXT NOT NULL,
  avg_entry TEXT, opened_at TEXT, entry_decision_id TEXT, bracket_order_id TEXT, tp TEXT, sl TEXT, k_tp REAL,
  k_sl REAL, atr_at_entry TEXT, dust_qty TEXT NOT NULL DEFAULT '0', dust_cost TEXT NOT NULL DEFAULT '0',
  PRIMARY KEY (book, product_id));
CREATE TABLE orders (client_order_id TEXT PRIMARY KEY, book TEXT NOT NULL, exchange_order_id TEXT, intent_id TEXT,
  leg TEXT, product_id TEXT, side TEXT, status TEXT, payload TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE fills (trade_id TEXT PRIMARY KEY, book TEXT NOT NULL, client_order_id TEXT, product_id TEXT, side TEXT,
  price TEXT, size TEXT, fee TEXT, liquidity TEXT, trade_time TEXT);
CREATE TABLE kill_switch_state (book TEXT NOT NULL, guard TEXT NOT NULL, tripped_at TEXT, until TEXT, reason TEXT,
  PRIMARY KEY (book, guard));
INSERT INTO meta VALUES ('mode', 'dry');
INSERT INTO positions (book, product_id, side, base_qty, avg_entry, opened_at, entry_decision_id)
  VALUES ('champion', 'SOL-EUR', 'LONG', '0.5', '100', '2026-10-17T12:00:00+00:00', 'dOLD');
INSERT INTO fills VALUES ('t1', 'champion', 'c', 'SOL-EUR', 'BUY', '100', '0.5', '0.1', 'maker',
  '2026-10-17T12:00:00+00:00');
"""


def make_old_db(path: Path, extra: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(OLD_SCHEMA + extra)
    con.commit()
    con.close()


async def test_an_existing_database_gets_the_table_and_keeps_its_rows(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    make_old_db(path)
    lg = await PositionLedger.open(path, "dry")
    assert lg.view("champion", "SOL-EUR").base_qty == D("0.5")  # the old position is still there
    assert [f.trade_id for f in await lg.book_fills("champion")] == ["t1"]
    assert await lg.closed_trades() == [] and await lg.meta_get("schema_version") == str(SCHEMA_VERSION)
    await lg.apply_exit_fill_recorded(fill("t2", "SELL", "110", "0.5", "0.1", T0 + timedelta(hours=1), "x"))
    (t,) = await lg.closed_trades()  # the old position closes into a trade like any other
    assert t.entry_decision_id == "dOLD" and t.net_quote == D("4.8")
    await lg.close()
    lg2 = await PositionLedger.open(path, "dry")  # opening again is a no-op
    assert len(await lg2.closed_trades()) == 1
    await lg2.close()


async def test_a_legacy_virtual_trades_table_is_renamed_with_its_data(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    make_old_db(
        path,
        "CREATE TABLE virtual_trades (trade_id TEXT PRIMARY KEY, book TEXT NOT NULL, product_id TEXT NOT NULL,"
        " exit_reason TEXT NOT NULL, exit_at TEXT NOT NULL, net_quote TEXT NOT NULL, payload TEXT NOT NULL);"
        "INSERT INTO virtual_trades VALUES ('old1', 'champion', 'SOL-EUR', 'TP', '2026-10-16T00:00:00+00:00',"
        " '1.5', '{}');",
    )
    lg = await PositionLedger.open(path, "dry")
    con = sqlite3.connect(path)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "closed_trades" in tables and "virtual_trades" not in tables
    assert con.execute("SELECT trade_id, net_quote FROM closed_trades").fetchall() == [("old1", "1.5")]
    cols = {r[1] for r in con.execute("PRAGMA table_info(closed_trades)")}
    assert {"entry_avg_price", "r_multiple", "model_version", "partial_entry"} <= cols  # the missing columns were added
    con.close()
    await lg.close()


async def test_the_database_of_the_other_mode_is_refused_before_it_is_touched(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    make_old_db(path)
    with pytest.raises(LedgerModeMismatch):
        await PositionLedger.open(path, "live")
    con = sqlite3.connect(path)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert "closed_trades" not in tables
