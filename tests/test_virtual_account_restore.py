"""VirtualAccount.restore: the account of a restarted book is what the booked fills say (spec sez. 5.3)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest

from modules.dryrun.virtual_account import VirtualAccount
from modules.execution.balance import Reservation
from tests.coverage_fixtures import Clock, buy, sell
from tests.test_virtual_account import B, P, fill, make_account

D = Decimal


@dataclass
class _Fill:
    product_id: str
    side: Any
    price: Decimal
    size: Decimal
    fee: Decimal


def _running_account() -> tuple[VirtualAccount, list[_Fill]]:
    account, guard, _ = make_account("1000.00")
    fills: list[_Fill] = []
    steps = [
        ("BUY", "92.03", "0.5", "0.2"), ("BUY", "95.00", "0.3", "0.1"),
        ("SELL", "100.00", "0.4", "0.3"), ("SELL", "98.00", "0.4", "0.2"),
    ]  # fmt: skip
    for i, (side, price, size, fee) in enumerate(steps):
        cid = f"o{i}"
        guard.authorize(buy(cid, size, price) if side == "BUY" else sell(cid, size, kind="market"))
        fill(account, cid, price, size, fee)
        account.balances.release(cid)
        fills.append(_Fill(P, side, D(price), D(size), D(fee)))
    return account, fills


def test_restore_gives_back_the_balances_the_fees_and_the_realized_pnl_of_a_running_account() -> None:
    live, fills = _running_account()
    restored = VirtualAccount(B, D("1000.00"), Clock())
    restored.restore(fills, {P: "SOL"})
    for currency in ("EUR", "SOL"):
        assert restored.balances.free(currency) == live.balances.free(currency)
        assert restored.balances.reserved(currency) == live.balances.reserved(currency)
    assert (restored.fees_paid, restored.realized_pnl) == (live.fees_paid, live.realized_pnl)
    assert live.fees_paid > 0 and live.realized_pnl != 0
    assert restored.position_qty(P) == live.position_qty(P) == D("0.0")
    assert restored.balances.free("EUR") == D("1000.00") + live.realized_pnl - live.fees_paid  # a sale ADDS proceeds


def test_restore_keeps_the_reservation_of_an_open_bracket_inside_free_plus_reserved() -> None:
    account, guard, _ = make_account("1000.00")
    guard.authorize(buy("b", "1", "100.00"))
    fill(account, "b", "100.00", "1", "0.5")
    account.balances.release("b")
    reservation = Reservation(client_order_id="bk", product_id=P, side="SELL", currency="SOL", amount=D("1"))
    restored = VirtualAccount(B, D("1000.00"), Clock())
    restored.restore([_Fill(P, "BUY", D("100.00"), D("1"), D("0.5"))], {P: "SOL"}, [reservation])
    bb = restored.balances
    assert (bb.free("SOL"), bb.reserved("SOL"), bb.total("SOL")) == (0, 1, 1)
    assert bb.reservation("bk") == reservation
    assert bb.total("EUR") == account.balances.total("EUR")


def test_restore_refuses_a_ledger_that_would_leave_a_negative_balance_or_a_used_account() -> None:
    restored = VirtualAccount(B, D("10.00"), Clock())
    with pytest.raises(ValueError, match="negative"):
        restored.restore([_Fill(P, "BUY", D("100.00"), D("1"), D("0.5"))], {P: "SOL"})
    with pytest.raises(ValueError, match="exceeds"):
        VirtualAccount(B, D("10.00"), Clock()).restore([_Fill(P, "SELL", D("1"), D("1"), D("0"))], {P: "SOL"})
    used, _ = _running_account()
    with pytest.raises(ValueError, match="fresh"):
        used.restore([], {P: "SOL"})
