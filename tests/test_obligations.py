"""Tripwires for what the indicator resolution (docs/decisions/2026-10-17-indicatori.md) leaves to later modules.

Each entry turns into a failing test the moment its file exists without the required wiring or tests. Until then it
shows up as a skip in ``pytest -rs``, so it cannot be forgotten.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.wiring_helpers import ROOT, defined_test_names, skip_until

# file -> (source tokens it must contain, tests that must exist, why)
OBLIGATIONS: dict[str, tuple[set[str], set[str], str]] = {
    "training/labeling.py": (
        {"rv_per_day_bps", "forward_rv_bucket"},
        {"test_label_atr_is_snapshot_atr", "test_vol_regime_point_in_time"},
        "vol_regime must use the single RVd function; barrier ATR must be the snapshot ATR (T8, T9)",
    ),
    "training/build_dataset.py": (
        {"compute_indicators", "indicator_anchor"},
        set(),
        "indicators are computed on the whole product history and the anchor is written to the model card",
    ),
    "modules/features/schemas.py": (
        {"vol_z_1d"},
        set(),
        "liquidity block is {spread_bps, vol_z_1d}; trades_z_1d was removed (checked below)",
    ),
    "modules/features/state_builder.py": (
        {"IndicatorSnapshot"},
        {"test_state_serialization_parity"},
        "training (from_row) and serving (update) must go through the same snapshot -> state path (T10)",
    ),
    "modules/risk/risk_manager.py": (
        {"atr_14_"},
        set(),
        "TP/SL use the ATR of the decision's snapshot, never one re-read at send time (resolution Q1)",
    ),
    "modules/app.py": (
        {"warm_up"},
        set(),
        "startup replays the whole stored history through IndicatorEngine.warm_up before opening streams",
    ),
}


@pytest.mark.parametrize("path", sorted(OBLIGATIONS))
def test_modules_that_come_later_honour_the_indicator_resolution(path: str) -> None:
    source = skip_until(path).read_text(encoding="utf-8")
    tokens, tests, why = OBLIGATIONS[path]
    missing = {t for t in tokens if t not in source}
    assert not missing, f"{path} must use {sorted(missing)}: {why}"
    absent = tests - defined_test_names({Path(__file__).name})
    assert not absent, f"{path} exists but these required tests do not: {sorted(absent)} ({why})"


def test_trades_z_1d_does_not_come_back() -> None:
    """The historical store has no trade counts: the field would be train/serve skew (resolution Q4)."""
    schemas = ROOT / "modules" / "features" / "schemas.py"
    if schemas.exists():
        assert "trades_z" not in schemas.read_text(encoding="utf-8")


def test_spread_skew_is_decided_before_the_state_builder() -> None:
    """Open item O1: spread_bps and the costs block are medians in training but live values at runtime.
    It needs its own CONFLITTO and a decision under docs/decisions/ before state_builder.py is written."""
    skip_until("modules/features/state_builder.py")
    decisions = list((ROOT / "docs" / "decisions").glob("*spread*"))
    assert decisions, "write the O1 decision (training vs runtime spread) in docs/decisions/*spread*.md first"
