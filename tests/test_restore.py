"""What a virtual book reads from the ledger before it is rebuilt (modules/dryrun/restore.py) and a few more
``closed_trades`` rules (a trade counts only its own fills, a sale larger than the purchase is flagged)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from modules.dryrun.restore import load_book_restore, starting_quote_key
from modules.execution.spot import SpotRegistry
from modules.ledger.positions import EntryPlan, FillRecord, OrderRecord, PositionLedger
from tests.coverage_fixtures import SOL

D = Decimal
T0 = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)
B, P = "champion", "SOL-EUR"
REGISTRY = SpotRegistry({P: SOL})
PLAN = EntryPlan(D("110"), D("90"), 3.0, 1.5, D("5"))


@pytest.fixture
async def ledger(tmp_path: Path) -> AsyncIterator[PositionLedger]:
    lg = await PositionLedger.open(tmp_path / "dry.db", "dry")
    yield lg
    await lg.close()


async def order(lg: PositionLedger, cid: str, leg: str, side: str, size: str = "1", payload: str | None = None) -> None:
    body = payload if payload is not None else json.dumps({"base_size": size})
    await lg.record_order(OrderRecord(cid, B, P, side, leg, "OPEN", T0, T0, payload=body))  # type: ignore[arg-type]


def fill(tid: str, side: str, price: str, size: str, fee: str, at: datetime, cid: str) -> FillRecord:
    return FillRecord(tid, B, cid, P, side, D(price), D(size), D(fee), "maker", at)  # type: ignore[arg-type]


async def long_with_bracket(lg: PositionLedger, bracket_id: str = "bk") -> None:
    await lg.apply_entry_fill_recorded(fill("f1", "BUY", "100", "1", "0.1", T0, "e"), "d", PLAN)
    await lg.set_bracket(B, P, bracket_id, D("110"), D("90"))


async def test_a_fresh_book_records_its_capital_and_has_nothing_to_restore(ledger: PositionLedger) -> None:
    state = await load_book_restore(ledger, B, REGISTRY, D("100"))
    assert state.is_fresh and state.starting_quote == D(100) and state.reservations == ()
    assert await ledger.meta_get(starting_quote_key(B)) == "100"


async def test_the_capital_a_book_started_with_wins_over_the_configured_one(ledger: PositionLedger) -> None:
    await load_book_restore(ledger, B, REGISTRY, D("100"))
    assert (await load_book_restore(ledger, B, REGISTRY, D("500"))).starting_quote == D(100)


async def test_the_open_bracket_of_the_position_is_kept_with_its_reservation(ledger: PositionLedger) -> None:
    await long_with_bracket(ledger)
    await order(ledger, "bk", "bracket", "SELL")
    state = await load_book_restore(ledger, B, REGISTRY, D("100"))
    assert [o.client_order_id for o in state.bracket_orders] == ["bk"] and not state.is_fresh
    (r,) = state.reservations
    assert (r.client_order_id, r.currency, r.amount, r.side) == ("bk", "SOL", D(1), "SELL")
    assert len(state.fills) == 1


async def test_a_bracket_that_is_not_the_one_the_position_points_at_is_not_kept(ledger: PositionLedger) -> None:
    await long_with_bracket(ledger, bracket_id="bk")
    await order(ledger, "old-bracket", "bracket", "SELL")  # a stale order of an earlier position
    await order(ledger, "entry", "entry", "BUY")
    await order(ledger, "exit", "exit", "SELL")
    state = await load_book_restore(ledger, B, REGISTRY, D("100"))
    assert state.bracket_orders == () and state.reservations == ()
    assert {o.client_order_id for o in state.open_orders} == {"old-bracket", "entry", "exit"}


async def test_an_unreadable_or_oversized_bracket_is_not_kept(ledger: PositionLedger) -> None:
    await long_with_bracket(ledger)
    await order(ledger, "bk", "bracket", "SELL", payload="{not json")
    assert (await load_book_restore(ledger, B, REGISTRY, D("100"))).bracket_orders == ()
    await ledger.update_order("bk", "CANCELLED", T0)
    await order(ledger, "bk2", "bracket", "SELL", size="5")  # more than the position holds
    await ledger.set_bracket(B, P, "bk2", D("110"), D("90"))
    assert (await load_book_restore(ledger, B, REGISTRY, D("100"))).bracket_orders == ()


# ------------------------------------------------------------------------------------------- closed trades


async def test_a_trade_counts_only_the_fills_of_its_own_position(ledger: PositionLedger) -> None:
    await ledger.apply_entry_fill_recorded(fill("b1", "BUY", "100", "1", "0.25", T0, "e1"), "dec1", PLAN)
    await order(ledger, "x1", "exit", "SELL")
    await ledger.apply_exit_fill_recorded(fill("s1", "SELL", "110", "1", "0.5", T0 + timedelta(hours=1), "x1"))
    await ledger.apply_entry_fill_recorded(
        fill("b2", "BUY", "200", "2", "1", T0 + timedelta(hours=2), "e2"), "dec2", PLAN
    )
    await order(ledger, "x2", "exit", "SELL")
    await ledger.apply_exit_fill_recorded(fill("s2", "SELL", "190", "2", "1.5", T0 + timedelta(hours=3), "x2"))
    first, second = await ledger.closed_trades(B)
    assert (first.gross_quote, first.fees_quote, first.net_quote) == (D(10), D("0.75"), D("9.25"))
    assert (second.entry_avg_price, second.entry_size, second.entry_fees) == (D(200), D(2), D(1))
    assert (second.gross_quote, second.fees_quote, second.net_quote) == (D(-20), D("2.5"), D("-22.5"))
    assert second.entry_decision_id == "dec2" and second.held_min == 60


async def test_a_sale_larger_than_the_purchase_is_flagged_as_a_partial_entry(ledger: PositionLedger) -> None:
    await ledger.set_dust(B, P, D("0.5"), D("50"))  # dust from an earlier fill, merged into the next entry
    await ledger.apply_entry_fill_recorded(fill("b1", "BUY", "100", "1", "0", T0, "e1"), "d", PLAN)
    assert ledger.view(B, P).base_qty == D("1.5")
    await order(ledger, "x", "exit", "SELL")
    await ledger.apply_exit_fill_recorded(fill("s", "SELL", "100", "1.5", "0", T0 + timedelta(hours=1), "x"))
    (t,) = await ledger.closed_trades(B)
    assert t.partial_entry and (t.entry_size, t.exit_size) == (D(1), D("1.5"))
