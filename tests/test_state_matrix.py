"""The vectorised FLAT matrix equals the scalar StateBuilder + flatten_state path, value for value."""

from __future__ import annotations

import numpy as np
import pandas as pd

from modules.features.serializer import FLAT_COLUMNS, flatten_state
from modules.features.state_builder import PositionInput, StateBuilder
from modules.features.state_matrix import flat_matrix
from modules.market.indicators import IndicatorSnapshot
from modules.market.indicators_batch import compute_indicators
from tests.indicator_fixtures import synthetic_bars
from tests.label_fixtures import loaded_label

LABEL = loaded_label({"BTC-EUR": 3.0, "SOL-EUR": 4.5})


def snapshot(product: str, ind: pd.DataFrame, i: int) -> IndicatorSnapshot:
    row = dict(zip(ind.columns, ind.to_numpy(dtype=np.float64)[i], strict=True))
    return IndicatorSnapshot.from_row(product, ind.index[i].to_pydatetime(), row)


def scalar_matrix(
    product: str, ind: pd.DataFrame, btc: pd.DataFrame | None, rows: np.ndarray, close: np.ndarray
) -> np.ndarray:
    builder = StateBuilder(LABEL)
    out = []
    for i in rows:
        b = snapshot("BTC-EUR", btc, i) if btc is not None else None
        state = builder.build(snapshot=snapshot(product, ind, i), btc=b, sentiment=None, position=PositionInput.flat(),
                              close_t=float(close[i]))  # fmt: skip
        flat = flatten_state(state)
        out.append([flat[c] for c in FLAT_COLUMNS])
    return np.array(out)


def assert_same(a: np.ndarray, b: np.ndarray) -> None:
    assert a.shape == b.shape
    nan_a, nan_b = np.isnan(a), np.isnan(b)
    assert (nan_a == nan_b).all(), "NaN masks differ"
    bad = ~nan_a & (a != b)
    assert not bad.any(), [(FLAT_COLUMNS[j], a[i, j], b[i, j]) for i, j in zip(*np.nonzero(bad), strict=True)][:5]


def test_matrix_equals_the_scalar_path_for_sol_with_btc_context() -> None:
    sol = synthetic_bars(days=25, seed=41, zero_volume_day=None, constant_days=None)
    btc = synthetic_bars(days=25, seed=42, zero_volume_day=None, constant_days=None)
    i_sol, i_btc = compute_indicators(sol), compute_indicators(btc)
    rows = np.arange(0, len(sol), 53)  # includes the cold-start region (many NaN) and the full-history region
    ref = scalar_matrix("SOL-EUR", i_sol, i_btc, rows, sol["close"].to_numpy())
    got = flat_matrix(i_sol, rows, "SOL-EUR", LABEL, i_btc, i_btc.index.get_indexer(i_sol.index[rows]))
    assert_same(got, ref)
    assert (~np.isnan(got)).sum() > 0.5 * got.size  # the comparison is not vacuous


def test_matrix_equals_the_scalar_path_for_btc_without_context() -> None:
    btc = synthetic_bars(days=25, seed=43, zero_volume_day=None, constant_days=None)
    ind = compute_indicators(btc)
    rows = np.arange(7, len(btc), 61)
    ref = scalar_matrix("BTC-EUR", ind, None, rows, btc["close"].to_numpy())
    assert_same(flat_matrix(ind, rows, "BTC-EUR", LABEL), ref)


def test_missing_btc_rows_give_nan_context() -> None:
    sol = synthetic_bars(days=25, seed=44, zero_volume_day=None, constant_days=None)
    ind = compute_indicators(sol)
    rows = np.array([30000, 31000])
    got = flat_matrix(ind, rows, "SOL-EUR", LABEL, ind, np.array([-1, -1]))
    assert np.isnan(got[:, FLAT_COLUMNS.index("btc_ret_1d_bps")]).all()


def test_costs_follow_the_spread_of_the_label() -> None:
    sol = synthetic_bars(days=25, seed=45, zero_volume_day=None, constant_days=None)
    ind = compute_indicators(sol)
    rows = np.array([30000])
    got = flat_matrix(ind, rows, "SOL-EUR", LABEL)
    assert (
        got[0, FLAT_COLUMNS.index("cost_tp_bps")] == 50 and got[0, FLAT_COLUMNS.index("cost_sl_bps")] == 92
    )  # s = 4.5: 25+50+2.25+5+10


def test_long_matrix_equals_the_scalar_long_state() -> None:

    from modules.features.state_matrix import long_matrix

    sol = synthetic_bars(days=25, seed=46, zero_volume_day=None, constant_days=None)
    btc = synthetic_bars(days=25, seed=47, zero_volume_day=None, constant_days=None)
    i_sol, i_btc = compute_indicators(sol), compute_indicators(btc)
    close = sol["close"].to_numpy()
    rows = np.arange(26000, 30000, 37)
    btc_rows = i_btc.index.get_indexer(i_sol.index[rows])
    entry, atr_e = float(close[25000]), 2.3
    opened = i_sol.index[24990]
    base = flat_matrix(i_sol, rows, "SOL-EUR", LABEL, i_btc, btc_rows)
    got = long_matrix(base, pd.DatetimeIndex(i_sol.index[rows]) + pd.Timedelta(minutes=1), close[rows], entry_px=entry,
                      stop_loss=entry - 1.5 * atr_e, take_profit=entry + 3 * atr_e, atr_entry=atr_e,
                      opened_at=opened)  # fmt: skip
    builder = StateBuilder(LABEL)
    ref = []
    for i in rows:
        pos = PositionInput("LONG", entry, entry - 1.5 * atr_e, entry + 3 * atr_e, atr_e, opened.to_pydatetime())
        st = builder.build(snapshot=snapshot("SOL-EUR", i_sol, i), btc=snapshot("BTC-EUR", i_btc, i), sentiment=None,
                           position=pos, close_t=float(close[i]))  # fmt: skip
        flat = flatten_state(st)
        ref.append([flat[c] for c in FLAT_COLUMNS])
    assert_same(got, np.array(ref))
