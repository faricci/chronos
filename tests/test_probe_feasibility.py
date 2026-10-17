"""The probe must reuse CostModel (spec sez. 0.3) and reproduce the worked reference case."""

from __future__ import annotations

from typing import Any

import pytest

from modules.risk.costs import FeeRates
from script.probe_coinbase import feasibility, fees_for_feasibility

ASSUMED = FeeRates(maker_bps=60, taker_bps=120, assumed=True)


def _row(rows: list[dict[str, Any]], entry: str, k_tp: float = 4.0) -> dict[str, Any]:
    (row,) = [r for r in rows if r["entry"] == entry and r["k_tp"] == k_tp]
    return row


def test_maker_row_matches_the_reference_example() -> None:
    row = _row(feasibility({"1d": 460.0}, 3.0, ASSUMED), "maker")
    assert row["tp_bps"] == 1840 and row["sl_bps"] == 920
    assert row["cost_tp_bps"] == 120  # the old probe gave 126: maker entry pays no spread/slippage
    assert row["cost_sl_bps"] == 197
    assert row["p_breakeven"] == 0.394
    assert row["required_lift"] == 0.06


def test_taker_row() -> None:
    row = _row(feasibility({"1d": 460.0}, 3.0, ASSUMED), "taker")
    assert row["cost_tp_bps"] == 187
    assert row["cost_sl_bps"] == 263  # the old probe gave 257: it omitted spread/slippage of the entry leg
    assert row["p_breakeven"] == 0.417
    assert row["required_lift"] == 0.084


def test_tp_that_does_not_cover_costs_has_no_breakeven() -> None:
    row = _row(feasibility({"1h": 30.0}, 2.9, ASSUMED), "maker")
    assert row["p_breakeven"] is None and row["required_lift"] is None


def test_missing_atr_is_skipped() -> None:
    assert feasibility({"1h": None}, 3.0, ASSUMED) == []


def test_account_fees_are_used_when_complete() -> None:
    fees = fees_for_feasibility({"maker_bps": 35.0, "taker_bps": 75.0})
    assert (fees.maker_bps, fees.taker_bps, fees.assumed) == (35.0, 75.0, False)


@pytest.mark.parametrize(
    "account",
    [
        None,
        {},
        {"error": "PermissionDenied"},
        {"maker_bps": None, "taker_bps": 75.0},
        {"maker_bps": 35.0, "taker_bps": None},
        {"maker_bps": 90.0, "taker_bps": 75.0},  # maker above taker is unusable
    ],
)
def test_incomplete_account_fees_fall_back_to_assumed(account: Any) -> None:
    fees = fees_for_feasibility(account)
    assert (fees.maker_bps, fees.taker_bps, fees.assumed) == (60.0, 120.0, True)
