"""The model a position was opened under (spec 5.1.6, resolution P1e step 17): recorded at the ENTRY, kept across
restarts and model swaps, migrated safely, and the version written on the closed trade."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from modules.ledger import positions as positions_module
from modules.ledger.positions import EntryStamp, FillRecord, PositionLedger, PositionView

D = Decimal
T0 = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
B, P = "champion", "SOL-EUR"
OLD = EntryStamp("v1", "sha-old", 4 * 1440)
NEW = EntryStamp("v2", "sha-new", 2 * 1440)


@pytest.fixture
async def ledger(tmp_path: Path) -> AsyncIterator[PositionLedger]:
    lg = await PositionLedger.open(tmp_path / "dry.db", "dry")
    yield lg
    await lg.close()


def sell_fill(n: int, at: datetime) -> FillRecord:
    return FillRecord(f"s{n}", B, f"x{n}", P, "SELL", D("100"), D("1"), D("0.1"), "taker", at)


async def test_the_entry_stamp_goes_to_the_position_at_the_first_fill(ledger: PositionLedger) -> None:
    ledger.stamp_entry(B, "d1", OLD)
    v = (await ledger.apply_entry_fill(B, P, D("1"), D("100"), "d1", T0)).position
    assert (v.model_version, v.label_sha256, v.horizon_min) == ("v1", "sha-old", 4 * 1440)
    assert ledger.view(B, P) == v


async def test_a_stamp_is_for_its_own_decision_and_is_used_once(ledger: PositionLedger) -> None:
    ledger.stamp_entry(B, "d1", OLD)
    ledger.stamp_entry("other-book", "d1", NEW)  # the same decision id in another book is another entry
    await ledger.apply_entry_fill(B, P, D("1"), D("100"), "d1", T0)
    assert ledger.view(B, P).model_version == "v1"
    await ledger.apply_exit_fill(B, P, D("1"))
    again = (await ledger.apply_entry_fill(B, P, D("1"), D("100"), "d1", T0 + timedelta(hours=1))).position
    assert again.model_version is None  # consumed: nobody announced this second entry and the book has no stamp


async def test_further_fills_do_not_restamp_a_position(ledger: PositionLedger) -> None:
    ledger.stamp_entry(B, "d1", OLD)
    await ledger.apply_entry_fill(B, P, D("1"), D("100"), "d1", T0)
    ledger.stamp_entry(B, "d2", NEW)
    v = (await ledger.apply_entry_fill(B, P, D("1"), D("101"), "d1", T0 + timedelta(minutes=1))).position
    assert v.model_version == "v1" and v.base_qty == D(2)


async def test_an_unannounced_entry_takes_the_model_the_book_runs(ledger: PositionLedger) -> None:
    ledger.set_trade_info(B, model_version="v2", fill_model_version="fill@1", label_sha256="sha-new", horizon_min=2880)
    v = (await ledger.apply_entry_fill(B, P, D("1"), D("100"), "d9", T0)).position
    assert (v.model_version, v.label_sha256, v.horizon_min) == ("v2", "sha-new", 2880)
    # without the label and horizon the book cannot stamp (a half stamp would make the SELL rule guess)
    await ledger.apply_exit_fill(B, P, D("1"))
    ledger.set_trade_info(B, model_version="v2", fill_model_version="fill@1")
    assert (await ledger.apply_entry_fill(B, P, D("1"), D("100"), "d10", T0)).position.label_sha256 is None


async def test_the_announced_entry_beats_the_model_the_book_runs_at_the_fill(ledger: PositionLedger) -> None:
    ledger.set_trade_info(B, model_version="v1", fill_model_version="f", label_sha256="sha-old", horizon_min=5760)
    ledger.stamp_entry(B, "d1", OLD)  # v1 decides; the maker order waits
    ledger.set_trade_info(B, model_version="v2", fill_model_version="f", label_sha256="sha-new", horizon_min=2880)
    v = (await ledger.apply_entry_fill(B, P, D("1"), D("100"), "d1", T0)).position  # it fills after the swap
    assert (v.model_version, v.label_sha256, v.horizon_min) == ("v1", "sha-old", 4 * 1440)


async def test_the_stamp_survives_a_reopen(tmp_path: Path) -> None:
    path = tmp_path / "dry.db"
    first = await PositionLedger.open(path, "dry")
    first.stamp_entry(B, "d1", OLD)
    await first.apply_entry_fill(B, P, D("1"), D("100"), "d1", T0)
    await first.close()
    second = await PositionLedger.open(path, "dry")
    try:
        v = second.view(B, P)
        assert (v.model_version, v.label_sha256, v.horizon_min) == ("v1", "sha-old", 4 * 1440)
    finally:
        await second.close()


async def test_a_trade_closed_after_a_swap_belongs_to_the_model_that_opened_it(ledger: PositionLedger) -> None:
    ledger.set_trade_info(B, model_version="v1", fill_model_version="f", label_sha256="sha-old", horizon_min=5760)
    ledger.stamp_entry(B, "d1", OLD)
    await ledger.apply_entry_fill(B, P, D("1"), D("100"), "d1", T0)
    # the swap: the book now runs v2
    ledger.set_trade_info(B, model_version="v2", fill_model_version="f", label_sha256="sha-new", horizon_min=2880)
    await ledger.apply_exit_fill_recorded(sell_fill(1, T0 + timedelta(hours=3)))
    (trade,) = await ledger.closed_trades(B)
    assert trade.model_version == "v1"  # the opener, not the model the book runs when it closes
    assert trade.to_payload()["model_version"] == "v1"
    # a position opened after the swap is the new model's
    ledger.stamp_entry(B, "d2", NEW)
    await ledger.apply_entry_fill(B, P, D("1"), D("100"), "d2", T0 + timedelta(hours=4))
    await ledger.apply_exit_fill_recorded(sell_fill(2, T0 + timedelta(hours=5)))
    assert [t.model_version for t in await ledger.closed_trades(B)] == ["v1", "v2"]


async def test_a_position_without_a_stamp_closes_with_the_version_of_the_book(ledger: PositionLedger) -> None:
    ledger.set_trade_info(B, model_version="v7", fill_model_version="f")
    await ledger.apply_entry_fill(B, P, D("1"), D("100"), "d1", T0)
    await ledger.apply_exit_fill_recorded(sell_fill(1, T0 + timedelta(hours=1)))
    assert [t.model_version for t in await ledger.closed_trades(B)] == ["v7"]  # as before the stamps existed


async def test_unfilled_stamps_are_bounded(ledger: PositionLedger) -> None:
    for i in range(positions_module.ENTRY_STAMPS_MAX + 50):
        ledger.stamp_entry(B, f"d{i}", OLD)
    kept = ledger._entry_stamps  # noqa: SLF001
    assert len(kept) == positions_module.ENTRY_STAMPS_MAX
    assert (B, "d0") not in kept and (B, f"d{positions_module.ENTRY_STAMPS_MAX + 49}") in kept  # oldest dropped


async def test_book_view_stamps_for_its_own_book(ledger: PositionLedger) -> None:
    ledger.book_view(B).stamp_entry("d1", OLD)
    v = (await ledger.apply_entry_fill(B, P, D("1"), D("100"), "d1", T0)).position
    assert v.model_version == "v1"


# ---------------------------------------------------------------------------------------------- migration

OLD_POSITIONS = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO meta VALUES ('mode', 'dry'), ('schema_version', '2');
CREATE TABLE positions (
  book TEXT NOT NULL, product_id TEXT NOT NULL, side TEXT NOT NULL CHECK (side IN ('FLAT','LONG')),
  base_qty TEXT NOT NULL, avg_entry TEXT, opened_at TEXT, entry_decision_id TEXT,
  bracket_order_id TEXT, tp TEXT, sl TEXT, k_tp REAL, k_sl REAL, atr_at_entry TEXT,
  dust_qty TEXT NOT NULL DEFAULT '0', dust_cost TEXT NOT NULL DEFAULT '0',
  PRIMARY KEY (book, product_id)
);
INSERT INTO positions (book, product_id, side, base_qty, avg_entry, opened_at, entry_decision_id, tp, sl)
VALUES ('champion', 'SOL-EUR', 'LONG', '1.5', '100', '2026-10-17T10:00:00+00:00', 'dOld', '110', '95'),
       ('champion', 'BTC-EUR', 'FLAT', '0', NULL, NULL, NULL, NULL, NULL);
"""


def make_old_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(OLD_POSITIONS)
    con.commit()
    con.close()


async def test_an_older_database_gets_the_columns_and_keeps_its_rows(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    make_old_db(path)
    ledger = await PositionLedger.open(path, "dry")
    try:
        v = ledger.view("champion", "SOL-EUR")
        assert v.is_long and v.base_qty == D("1.5") and v.avg_entry == D(100) and v.tp == D(110)  # untouched
        assert (v.model_version, v.label_sha256, v.horizon_min) == (None, None, None)
    finally:
        await ledger.close()
    con = sqlite3.connect(path)
    columns = {r[1] for r in con.execute("PRAGMA table_info(positions)")}
    con.close()
    assert {"model_version", "label_config_sha256", "horizon_min"} <= columns
    again = await PositionLedger.open(path, "dry")  # opening it twice is harmless
    await again.close()


async def test_open_positions_without_a_model_are_stamped_once_and_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    make_old_db(path)
    ledger = await PositionLedger.open(path, "dry")
    try:
        assert await ledger.stamp_open_positions("champion", OLD) == ["SOL-EUR"]  # the FLAT one is not a position
        v = ledger.view("champion", "SOL-EUR")
        assert (v.model_version, v.label_sha256, v.horizon_min) == ("v1", "sha-old", 4 * 1440)
        assert v.base_qty == D("1.5") and v.tp == D(110)
        assert await ledger.stamp_open_positions("champion", NEW) == []  # it has a model now: left alone
        assert ledger.view("champion", "SOL-EUR").model_version == "v1"
        assert await ledger.stamp_open_positions("other", NEW) == []
    finally:
        await ledger.close()
    reopened = await PositionLedger.open(path, "dry")
    try:
        assert reopened.view("champion", "SOL-EUR").label_sha256 == "sha-old"  # persisted
    finally:
        await reopened.close()


def test_a_view_without_a_model_has_the_defaults() -> None:
    v = PositionView(B, P)
    assert (v.model_version, v.label_sha256, v.horizon_min) == (None, None, None)
