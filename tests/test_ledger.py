from __future__ import annotations

import asyncio
import tempfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from modules.execution.balance_guard import InsufficientCoverage
from modules.ledger.positions import (
    EntryPlan,
    FillRecord,
    LedgerModeMismatch,
    OrderRecord,
    PositionLedger,
    PositionView,
    ShortPositionError,
)
from tests.coverage_fixtures import make_guard, sell

D = Decimal
T0 = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
B, P = "champion", "SOL-EUR"
PLAN = EntryPlan(tp=D("108.96"), sl=D("83.57"), k_tp=4.0, k_sl=2.0, atr=D("4.2334"))


@pytest.fixture
async def ledger(tmp_path: Path) -> AsyncIterator[PositionLedger]:
    lg = await PositionLedger.open(tmp_path / "state" / "chronos_dry.db", "dry")
    yield lg
    await lg.close()


async def reload(path: Path, mode: str = "dry") -> PositionLedger:
    return await PositionLedger.open(path, mode)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------- positions


async def test_unknown_product_is_flat(ledger: PositionLedger) -> None:
    v = ledger.view(B, P)
    assert (v.side, v.base_qty, v.avg_entry, v.dust_qty) == ("FLAT", D(0), None, D(0)) and not v.is_long


async def test_first_entry_fill_opens_a_long_position_with_its_plan(ledger: PositionLedger) -> None:
    r = await ledger.apply_entry_fill(B, P, D("0.024"), D("92.03"), "dec1", T0, PLAN)
    v = r.position
    assert not r.dust_merged and v.is_long and v.base_qty == D("0.024") and v.avg_entry == D("92.03")
    assert (v.tp, v.sl, v.k_tp, v.k_sl, v.atr_at_entry) == (D("108.96"), D("83.57"), 4.0, 2.0, D("4.2334"))
    assert v.entry_decision_id == "dec1" and v.opened_at == T0 and v.bracket_order_id is None
    assert ledger.view(B, P) == v


async def test_further_entry_fills_average_the_price(ledger: PositionLedger) -> None:
    await ledger.apply_entry_fill(B, P, D("1"), D("100"), "d", T0, PLAN)
    r = await ledger.apply_entry_fill(B, P, D("3"), D("104"), "d", T0 + timedelta(minutes=1))
    assert r.position.base_qty == D(4) and r.position.avg_entry == D("103")
    assert r.position.opened_at == T0 and r.position.tp == D("108.96")  # the plan and the opening time are kept


async def test_bracket_is_attached_and_cleared(ledger: PositionLedger) -> None:
    with pytest.raises(ValueError, match="no open position"):
        await ledger.set_bracket(B, P, "bk1", D(1), D(1))
    await ledger.apply_entry_fill(B, P, D("0.024"), D("92.03"), "d", T0, PLAN)
    v = await ledger.set_bracket(B, P, "bk1", D("109.00"), D("83.60"))
    assert (v.bracket_order_id, v.tp, v.sl) == ("bk1", D("109.00"), D("83.60"))
    v = await ledger.set_bracket(B, P, None, None, None)
    assert v.bracket_order_id is None and v.tp == D("109.00")  # None keeps the levels, drops the order id


async def test_partial_and_full_exit(ledger: PositionLedger) -> None:
    await ledger.apply_entry_fill(B, P, D("1"), D("100"), "d", T0, PLAN)
    await ledger.set_bracket(B, P, "bk1", None, None)
    v = await ledger.apply_exit_fill(B, P, D("0.4"))
    assert v.is_long and v.base_qty == D("0.6") and v.avg_entry == D(100) and v.bracket_order_id == "bk1"
    v = await ledger.apply_exit_fill(B, P, D("0.6"))
    assert not v.is_long and v.base_qty == D(0) and v.bracket_order_id is None and v.tp is None and v.avg_entry is None


async def test_selling_more_than_held_is_refused_and_changes_nothing(ledger: PositionLedger) -> None:
    await ledger.apply_entry_fill(B, P, D("1"), D("100"), "d", T0, PLAN)
    with pytest.raises(ShortPositionError):
        await ledger.apply_exit_fill(B, P, D("1.000000001"))
    assert ledger.view(B, P).base_qty == D(1)
    with pytest.raises(ShortPositionError):
        await ledger.apply_exit_fill(B, "ETH-EUR", D("0.1"))  # nothing held at all


async def test_invalid_fills_are_rejected(ledger: PositionLedger) -> None:
    for qty, price in ((D(0), D(1)), (D(1), D(0)), (D(-1), D(1))):
        with pytest.raises(ValueError):
            await ledger.apply_entry_fill(B, P, qty, price, "d", T0)
    with pytest.raises(ValueError):
        await ledger.apply_exit_fill(B, P, D(0))


async def test_books_are_independent(ledger: PositionLedger) -> None:
    await ledger.apply_entry_fill("champion", P, D("1"), D("100"), "d", T0)
    assert ledger.view("challenger", P).base_qty == D(0)
    await ledger.apply_entry_fill("challenger", P, D("2"), D("90"), "d", T0)
    await ledger.apply_exit_fill("champion", P, D("1"))
    assert ledger.view("challenger", P).base_qty == D(2) and not ledger.view("champion", P).is_long
    assert [v.book for v in ledger.long_positions("challenger")] == ["challenger"] and ledger.long_positions(
        "champion"
    ) == []


# ------------------------------------------------------------------------------------- dust


async def test_dust_is_kept_on_a_flat_position_and_merged_into_the_next_entry(ledger: PositionLedger) -> None:
    v = await ledger.set_dust(B, P, D("0.0004"), D("0.0370"))
    assert not v.is_long and v.dust_qty == D("0.0004") and v.dust_cost == D("0.0370")
    r = await ledger.apply_entry_fill(B, P, D("0.0246"), D("92.00"), "d", T0, PLAN)
    assert r.dust_merged and r.position.base_qty == D("0.0250")
    assert r.position.avg_entry == (D("92.00") * D("0.0246") + D("0.0370")) / D("0.0250")
    assert (r.position.dust_qty, r.position.dust_cost) == (D(0), D(0))  # merged: nothing left over


async def test_dust_survives_a_full_exit_and_cannot_be_set_on_a_long(ledger: PositionLedger) -> None:
    await ledger.set_dust(B, P, D("0.0004"), D("0.0370"))
    await ledger.apply_entry_fill(B, P, D("1"), D("100"), "d", T0)
    with pytest.raises(ValueError, match="FLAT"):
        await ledger.set_dust(B, P, D("1"), D("1"))
    v = await ledger.apply_exit_fill(B, P, D("1.0004"))  # dust was merged, so the whole lot sells
    assert not v.is_long and v.dust_qty == 0


# ------------------------------------------------------------------------ persistence and mode


async def test_state_survives_a_restart_exactly(tmp_path: Path) -> None:
    path = tmp_path / "chronos_dry.db"
    lg = await PositionLedger.open(path, "dry")
    await lg.apply_entry_fill(B, P, D("0.123456789"), D("92.030000001"), "d1", T0, PLAN)
    await lg.set_bracket(B, P, "bk1", D("109.00"), D("83.60"))
    await lg.set_dust(B, "ETH-EUR", D("0.00001"), D("0.02"))
    before = {p: lg.view(B, p) for p in (P, "ETH-EUR")}
    await lg.close()
    again = await reload(path)
    try:
        assert {p: again.view(B, p) for p in (P, "ETH-EUR")} == before
        assert again.view(B, P).base_qty == D("0.123456789")  # no float ever touched it
    finally:
        await again.close()


async def test_a_database_cannot_be_opened_in_the_other_mode(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    await (await PositionLedger.open(path, "dry")).close()
    with pytest.raises(LedgerModeMismatch, match="'dry'"):
        await PositionLedger.open(path, "live")
    again = await PositionLedger.open(path, "dry")  # the right mode still works
    await again.close()


async def test_the_database_uses_wal(ledger: PositionLedger) -> None:
    async with ledger.connection.execute("PRAGMA journal_mode") as cur:
        assert (await cur.fetchone())[0] == "wal"  # type: ignore[index]


async def test_a_failed_write_rolls_back_and_leaves_the_cache_untouched(
    ledger: PositionLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    await ledger.apply_entry_fill(B, P, D("1"), D("100"), "d", T0, PLAN)
    before = ledger.view(B, P)

    async def boom(_: PositionView) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(ledger, "_write_position", boom)
    with pytest.raises(RuntimeError):
        await ledger.apply_exit_fill(B, P, D("0.5"))
    assert ledger.view(B, P) == before
    monkeypatch.undo()
    await ledger.apply_exit_fill(B, P, D("0.5"))  # the ledger is still usable afterwards
    assert ledger.view(B, P).base_qty == D("0.5")


# --------------------------------------------------------------------------- orders and fills


def order(cid: str, status: str = "OPEN", book: str = B) -> OrderRecord:
    return OrderRecord(cid, book, P, "BUY", "entry", status, T0, T0, payload='{"x":1}')


async def test_orders_are_stored_updated_and_listed_while_open(ledger: PositionLedger) -> None:
    await ledger.record_order(order("o1"))
    await ledger.record_order(order("o2"))
    await ledger.record_order(order("o3", book="challenger"))
    assert [o.client_order_id for o in await ledger.open_orders(B)] == ["o1", "o2"]
    await ledger.update_order("o1", "FILLED", T0 + timedelta(seconds=5), exchange_order_id="ex1")
    open_now = await ledger.open_orders(B)
    assert [o.client_order_id for o in open_now] == ["o2"]
    with pytest.raises(KeyError):
        await ledger.update_order("ghost", "FILLED", T0)


async def test_an_order_id_cannot_be_recorded_twice(ledger: PositionLedger) -> None:
    await ledger.record_order(order("o1"))
    with pytest.raises(Exception, match="UNIQUE|constraint"):
        await ledger.record_order(order("o1"))


def fill(trade_id: str, size: str = "0.5", price: str = "92.03") -> FillRecord:
    return FillRecord(trade_id, B, "o1", P, "BUY", D(price), D(size), D("0.01"), "MAKER", T0)


async def test_a_fill_is_recorded_once(ledger: PositionLedger) -> None:
    assert await ledger.record_fill(fill("t1")) is True
    assert await ledger.record_fill(fill("t1")) is False  # delivered again by polling
    assert await ledger.record_fill(fill("t2", size="0.25")) is True
    got = await ledger.fills_for("o1")
    assert [f.size for f in got] == [D("0.5"), D("0.25")] and got[0].price == D("92.03")


# -------------------------------------------------------------------- with the BalanceGuard


async def test_the_guard_reads_the_ledger_positions_synchronously(ledger: PositionLedger) -> None:
    guard, book, _, _, _ = make_guard(base="0.1", owned="0")
    guard._positions = ledger.book_view("champion")  # type: ignore[assignment]
    with pytest.raises(InsufficientCoverage):
        guard.authorize(sell("s0", size="0.024"))  # nothing owned according to the ledger
    await ledger.apply_entry_fill("champion", P, D("0.1"), D("92"), "d", T0)
    guard.authorize(sell("s1", size="0.024"))  # now the ledger says 0.1 SOL are held
    with pytest.raises(InsufficientCoverage):
        guard.authorize(sell("s2", size="0.08"))  # 0.1 - 0.024 reserved < 0.08
    assert book.reserved("SOL") == D("0.024")


# ----------------------------------------------------------------------------------- property


async def _run_sequence(ops: list[tuple[str, int, int]], path: Path) -> None:
    lg = await PositionLedger.open(path, "dry")
    held = D(0)
    try:
        for kind, qty_units, price_units in ops:
            qty, price = D(qty_units) / 1000, D(price_units) / 10
            if kind == "buy":
                await lg.apply_entry_fill(B, P, qty, price, "d", T0)
                held += qty
            elif qty <= held:
                v = await lg.apply_exit_fill(B, P, qty)
                held -= qty
                assert v.base_qty == held
            else:
                with pytest.raises(ShortPositionError):
                    await lg.apply_exit_fill(B, P, qty)
            v = lg.view(B, P)
            assert v.base_qty == held >= 0 and v.is_long == (held > 0)
            if held > 0:
                assert v.avg_entry is not None and v.avg_entry > 0
        expected = lg.view(B, P)
    finally:
        await lg.close()
    again = await PositionLedger.open(path, "dry")
    try:
        assert again.view(B, P) == expected  # a restart gives back exactly the same position
    finally:
        await again.close()


@settings(max_examples=40, deadline=None)
@given(
    st.lists(
        st.tuples(st.sampled_from(["buy", "sell"]), st.integers(1, 50), st.integers(1, 300)),
        min_size=1,
        max_size=25,
    )
)
def test_random_fill_sequences_keep_the_invariants_and_survive_a_reload(ops: list[tuple[str, int, int]]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(_run_sequence(ops, Path(tmp) / "dry.db"))


# ---------------------------------------------------------------- get_order and the one-transaction fills


def fill_rec(trade: str, side: str, size: str, price: str = "92.03", cid: str = "c1") -> FillRecord:
    return FillRecord(trade, B, cid, P, side, D(price), D(size), D("0.01"), "maker", T0)  # type: ignore[arg-type]


async def test_get_order_finds_an_order_in_any_status_and_respects_the_book(ledger: PositionLedger) -> None:
    rec = OrderRecord("c1", B, P, "BUY", "entry", "OPEN", T0, T0, intent_id="i1", payload='{"x": 1}')
    await ledger.record_order(rec)
    assert await ledger.get_order("c1") == rec
    assert await ledger.get_order("c1", B) == rec
    assert await ledger.get_order("c1", "other") is None
    assert await ledger.get_order("nope") is None
    await ledger.update_order("c1", "FILLED", T0, "ex-1")  # terminal: no longer in open_orders, still readable
    assert await ledger.open_orders(B) == []
    found = await ledger.get_order("c1")
    assert found is not None and (found.status, found.exchange_order_id) == ("FILLED", "ex-1")


async def test_a_fill_and_its_position_are_written_in_one_transaction(
    ledger: PositionLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(_view: PositionView) -> None:
        raise RuntimeError("crash between the fill and the position")

    monkeypatch.setattr(ledger, "_write_position", broken)
    with pytest.raises(RuntimeError):
        await ledger.apply_entry_fill_recorded(fill_rec("t1", "BUY", "0.023"), "dec-1", PLAN)
    assert await ledger.fills_for("c1") == []  # the fill did not survive without its position
    assert ledger.view(B, P).side == "FLAT"
    monkeypatch.undo()
    result = await ledger.apply_entry_fill_recorded(fill_rec("t1", "BUY", "0.023"), "dec-1", PLAN)  # the retry works
    assert result is not None and result.position.base_qty == D("0.023")
    assert result.position.tp == PLAN.tp and result.position.entry_decision_id == "dec-1"
    assert [f.trade_id for f in await ledger.fills_for("c1")] == ["t1"]


async def test_a_fill_already_stored_changes_nothing_when_recorded_with_its_position(ledger: PositionLedger) -> None:
    first = await ledger.apply_entry_fill_recorded(fill_rec("t1", "BUY", "0.023"), "dec-1", PLAN)
    again = await ledger.apply_entry_fill_recorded(fill_rec("t1", "BUY", "0.023"), "dec-1", PLAN)
    assert first is not None and again is None
    assert ledger.view(B, P).base_qty == D("0.023")  # not 0.046
    assert await ledger.record_fill(fill_rec("t1", "BUY", "0.023")) is False  # the plain path sees it too
    sold = await ledger.apply_exit_fill_recorded(fill_rec("t2", "SELL", "0.010", cid="c2"))
    assert sold is not None and sold.base_qty == D("0.013")
    assert await ledger.apply_exit_fill_recorded(fill_rec("t2", "SELL", "0.010", cid="c2")) is None
    assert ledger.view(B, P).base_qty == D("0.013")


async def test_a_recorded_exit_can_be_clipped_and_an_oversell_stores_nothing(ledger: PositionLedger) -> None:
    await ledger.apply_entry_fill_recorded(fill_rec("t1", "BUY", "0.023"), "dec-1", PLAN)
    flat = await ledger.apply_exit_fill_recorded(fill_rec("t2", "SELL", "0.030", cid="c2"), D("0.023"))
    assert flat is not None and flat.side == "FLAT"  # clipped to what was held
    assert [f.size for f in await ledger.fills_for("c2")] == [D("0.030")]  # the fill itself is stored as it was
    with pytest.raises(ShortPositionError):
        await ledger.apply_exit_fill_recorded(fill_rec("t3", "SELL", "0.001", cid="c3"))
    assert await ledger.fills_for("c3") == []  # and a refused exit leaves no fill behind


async def test_recorded_fills_survive_a_reload(tmp_path: Path) -> None:
    path = tmp_path / "dry.db"
    lg = await PositionLedger.open(path, "dry")
    try:
        await lg.apply_entry_fill_recorded(fill_rec("t1", "BUY", "0.023"), "dec-1", PLAN)
    finally:
        await lg.close()
    again = await reload(path)
    try:
        assert again.view(B, P).base_qty == D("0.023") and len(await again.fills_for("c1")) == 1
    finally:
        await again.close()
