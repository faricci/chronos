"""DecisionState schema, canonical serialization and flat numeric view (spec sez. 2.2.1, 2.3 B)."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from pydantic import ValidationError

from modules.features.schemas import AssetSentimentState, Costs, DecisionState
from modules.features.serializer import FLAT_COLUMNS, flatten_state, serialize_state, state_sha256
from tests.state_fixtures import ALL_STATES, btc_long, cold_start, sol_flat

GOLDEN = Path(__file__).parent / "golden"


@pytest.mark.parametrize("name", sorted(ALL_STATES))
def test_golden_state_serializes_byte_for_byte(name: str) -> None:
    expected = (GOLDEN / f"state_{name}.json").read_bytes()
    assert serialize_state(ALL_STATES[name]()).encode("utf-8") == expected


@pytest.mark.parametrize("name", sorted(ALL_STATES))
def test_golden_state_round_trips(name: str) -> None:
    text = (GOLDEN / f"state_{name}.json").read_text(encoding="utf-8")
    assert serialize_state(DecisionState.model_validate_json(text)) == text


def test_canonical_form_is_compact_and_keeps_the_spec_key_order() -> None:
    s = serialize_state(sol_flat())
    assert " " not in s and "\n" not in s
    d = json.loads(s)
    assert list(d) == [
        "v", "product", "clock", "position", "price", "momentum", "volatility", "liquidity", "sentiment",
        "btc_context", "costs",
    ]  # fmt: skip
    assert list(d["price"]["ret_bps"]) == ["15m", "1h", "6h", "1d", "3d", "7d"]
    assert list(d["price"]) == ["ret_bps", "seq_1h_bps", "seq_1d_bps", "dist_vwap_1d_bps", "bb_pos_1d", "hh_ll_pos_30d"]
    assert d["v"] == "chronos.state.v1"


def test_missing_values_are_null_never_omitted() -> None:
    d = json.loads(serialize_state(cold_start()))
    assert d["price"]["seq_1h_bps"] is None and d["volatility"]["atr_14_1d_bps"] is None
    assert d["sentiment"] == {"asset": None, "market": None, "fear_greed": None}
    assert json.loads(serialize_state(btc_long()))["sentiment"] is None


def test_decimal_fields_round_to_two_places() -> None:
    a = AssetSentimentState(
        ewm=0.38449, ewm_slow=0.2149, mass=2.714, mass_slow=9.4049, disp=0.2251, shock_15m=0.2999, n_1h=3, n_24h=14,
        last_choice=None, last_age_min=None,
    )  # fmt: skip
    assert (a.ewm, a.ewm_slow, a.mass, a.disp, a.shock_15m) == (0.38, 0.21, 2.71, 0.23, 0.3)
    c = Costs(entry_mode="maker", cost_tp_bps=1, cost_sl_bps=2, p_breakeven=0.39499)
    assert c.p_breakeven == 0.39


def test_schema_rejects_unknown_fields_and_bad_values() -> None:
    d = json.loads(serialize_state(sol_flat()))
    d["extra"] = 1
    with pytest.raises(ValidationError):
        DecisionState.model_validate(d)
    d = json.loads(serialize_state(sol_flat()))
    d["position"]["side"] = "SHORT"
    with pytest.raises(ValidationError):
        DecisionState.model_validate(d)
    d = json.loads(serialize_state(sol_flat()))
    d["v"] = "chronos.state.v2"
    with pytest.raises(ValidationError):
        DecisionState.model_validate(d)


def test_trades_z_does_not_exist() -> None:
    assert "trades_z" not in serialize_state(sol_flat())
    assert "vol_z_1d" in serialize_state(sol_flat())


def test_state_sha256_is_stable_and_sensitive() -> None:
    s = serialize_state(sol_flat())
    assert state_sha256(s) == state_sha256(s) and len(state_sha256(s)) == 64
    assert state_sha256(s) != state_sha256(serialize_state(btc_long()))
    assert state_sha256(s) != state_sha256(s.replace('"hour_utc":14', '"hour_utc":15'))


# --------------------------------------------------------------------------------------- flatten


def test_flatten_follows_the_declared_columns() -> None:
    for make in ALL_STATES.values():
        flat = flatten_state(make())
        assert tuple(flat) == FLAT_COLUMNS
    assert len(FLAT_COLUMNS) == len(set(FLAT_COLUMNS))


def test_flatten_values_of_the_reference_state() -> None:
    f = flatten_state(sol_flat())
    assert f["clock_hour_utc"] == 14 and f["clock_dow"] == 5
    assert f["pos_is_long"] == 0.0 and math.isnan(f["pos_unreal_bps"])
    assert f["ret_15m_bps"] == -12 and f["ret_7d_bps"] == 520
    assert f["seq_1h_bps_00"] == 12 and f["seq_1h_bps_11"] == -38 and f["seq_1d_bps_6"] == 210
    assert f["sent_asset_ewm"] == 0.38 and f["sent_market_n_1h"] == 9 and f["sent_fear_greed"] == 54
    assert f["btc_ret_1d_bps"] == -35 and f["cost_is_taker"] == 0.0 and f["p_breakeven"] == 0.39


def test_flatten_maps_absent_blocks_to_nan() -> None:
    f = flatten_state(btc_long())
    assert f["pos_is_long"] == 1.0 and f["pos_hours_held"] == 51
    assert all(math.isnan(v) for k, v in f.items() if k.startswith("sent_") or k.startswith("btc_"))
    c = flatten_state(cold_start())
    assert math.isnan(c["seq_1h_bps_03"]) and math.isnan(c["seq_1d_bps_0"]) and c["cost_is_taker"] == 1.0
    assert math.isnan(c["sent_asset_ewm"]) and math.isnan(c["p_breakeven"])
