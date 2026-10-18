"""StateBuilder: training/serving parity (T10) and the O1 rule that the state never carries live spread or fees."""

from __future__ import annotations

import inspect
import json
import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from modules.features.schemas import DecisionState, Liquidity
from modules.features.serializer import FLAT_COLUMNS, flatten_state, serialize_state
from modules.features.state_builder import PositionInput, StateBuilder
from modules.ledger.positions import PositionView
from modules.market.indicators import IndicatorEngine, IndicatorSnapshot
from modules.market.indicators_batch import compute_indicators
from modules.market.timeframes import Bar
from modules.sentiment.schemas import AssetSentiment, FearGreedSnapshot, MarketSentiment, SentimentSnapshot
from tests.indicator_fixtures import synthetic_bars
from tests.label_fixtures import loaded_label
from tests.state_fixtures import ALL_STATES

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
LABEL = loaded_label()


def sentiment_at(t: datetime) -> SentimentSnapshot:
    return SentimentSnapshot(
        as_of=t,
        asset=AssetSentiment(
            key="SOL",
            ewm=0.384,
            ewm_slow=0.213,
            mass=2.714,
            mass_slow=9.4049,
            disp=0.2251,
            shock_15m=0.3,
            n_1h=3,
            n_24h=14,
            last_choice=2,
            last_conf=0.41,
            last_age_min=3,
        ),  # fmt: skip
        market=MarketSentiment(ewm=-0.05, ewm_slow=0.02, mass=6.1, mass_slow=31.2, disp=0.41, shock_15m=0.12, n_1h=9),
        fear_greed=FearGreedSnapshot(value=54, age_h=6.0),
    )


def snapshot(product: str = "SOL-EUR", **over: float) -> IndicatorSnapshot:
    """A fully populated snapshot (values chosen to hit every clamp and rounding rule)."""
    nan = float("nan")
    base: dict[str, object] = {
        "ret_15m_bps": -12.4, "ret_1h_bps": -38.5, "ret_6h_bps": 95.49, "ret_1d_bps": 210.0, "ret_3d_bps": -340.0,
        "ret_7d_bps": 520.0, "dist_vwap_1d_bps": -55.0, "bb_pos_1d": 0.414, "hh_ll_pos_30d": 0.611,
        "rsi_14_1h": 44.4, "rsi_14_6h": 51.0, "rsi_14_1d": 47.0, "macd_hist_1d_bps": -18.0,
        "atr_14_1h": 0.6, "atr_14_6h": 2.2, "atr_14_1d": 4.2, "atr_14_1h_bps": 61.0, "atr_14_6h_bps": 164.0,
        "atr_14_1d_bps": 460.0, "rv_1d_bps": 410.0, "rv_30d_bps": 460.0, "rv_ratio_1d_30d": 0.894,
        "atr_1d_pctile_365d": 38.0, "vol_z_1d": 0.6, "seq_1h_bps": tuple(float(i) for i in range(12)),
        "seq_1d_bps": tuple(float(10 * i) for i in range(7)),
    }  # fmt: skip
    base.update(over)
    assert nan != nan
    return IndicatorSnapshot(product_id=product, ts=T0 - timedelta(minutes=1), t=T0, **base)  # type: ignore[arg-type]


FLAT = PositionInput.flat()


def build(s: IndicatorSnapshot | None = None, **kw: object) -> DecisionState:
    args: dict[str, object] = {"snapshot": s or snapshot(), "btc": snapshot("BTC-EUR"), "sentiment": None,
                               "position": FLAT, "close_t": 92.0}  # fmt: skip
    args.update(kw)
    return StateBuilder(LABEL).build(**args)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------- blocks


def test_market_blocks_round_and_clamp_like_the_spec() -> None:
    st = build(snapshot(ret_1d_bps=9999.0, ret_3d_bps=-9999.0, bb_pos_1d=3.7, vol_z_1d=-9.0, ret_15m_bps=-12.5))
    assert st.price.ret_bps.d1 == 5000 and st.price.ret_bps.d3 == -5000
    assert st.price.bb_pos_1d == 2.0 and st.liquidity.vol_z_1d == -6.0
    assert st.price.ret_bps.m15 == -12  # int(round(-12.5)) is half-to-even
    assert st.price.hh_ll_pos_30d == 0.61 and st.volatility.rv_ratio_1d_30d == 0.89
    assert st.clock.hour_utc == 12 and st.clock.dow == T0.weekday()


def test_nan_becomes_null_and_sequences_are_never_partial() -> None:
    nan = float("nan")
    st = build(snapshot(rsi_14_1d=nan, atr_14_6h_bps=nan, seq_1h_bps=None))
    assert st.momentum.rsi_14_1d is None and st.volatility.atr_14_6h_bps is None
    assert st.price.seq_1h_bps is None and st.price.seq_1d_bps is not None and len(st.price.seq_1d_bps) == 7
    assert st.costs.p_breakeven is None  # the ATR in bps is missing


def test_btc_context_rules() -> None:
    assert build().btc_context is not None
    assert build(btc=None).btc_context is None  # BTC row missing
    with pytest.raises(ValueError):
        build(snapshot("BTC-EUR"), btc=snapshot("BTC-EUR"))
    assert build(snapshot("BTC-EUR"), btc=None).btc_context is None
    nan_btc = build(btc=snapshot("BTC-EUR", ret_1d_bps=float("nan"), rv_ratio_1d_30d=float("nan")))
    assert nan_btc.btc_context is not None and nan_btc.btc_context.ret_1d_bps is None


def test_sentiment_block() -> None:
    assert build().sentiment is None
    st = build(sentiment=sentiment_at(T0))
    assert st.sentiment is not None and st.sentiment.asset is not None and st.sentiment.market is not None
    assert (st.sentiment.asset.ewm, st.sentiment.asset.mass_slow, st.sentiment.asset.last_choice) == (0.38, 9.4, 2)
    assert st.sentiment.fear_greed == 54 and st.sentiment.market.n_1h == 9


def test_position_block_flat_and_long() -> None:
    assert build().position.side == "FLAT" and build().position.unreal_bps is None
    pos = PositionInput("LONG", entry_px=90.0, stop_loss=88.0, take_profit=95.0, atr_entry=2.0,
                        opened_at=T0 - timedelta(hours=5, minutes=59))  # fmt: skip
    p = build(position=pos, close_t=91.0).position
    assert p.side == "LONG" and p.hours_held == 5  # floor
    assert p.unreal_bps == round(1e4 * (91.0 / 90.0 - 1))
    assert p.dist_sl_atr == 1.5 and p.dist_tp_atr == 2.0


def test_position_distances_are_clamped_and_hours_never_negative() -> None:
    pos = PositionInput("LONG", 90.0, 1.0, 5000.0, 0.01, T0 + timedelta(hours=1))
    p = build(position=pos, close_t=91.0).position
    assert p.dist_sl_atr == 20.0 and p.dist_tp_atr == 20.0 and p.hours_held == 0
    crash = build(position=PositionInput("LONG", 90.0, 80.0, 99.0, 2.0, T0), close_t=0.0001)
    assert crash.position.unreal_bps == -5000 and crash.position.dist_sl_atr == -20.0


def test_a_long_without_its_numbers_is_refused() -> None:
    with pytest.raises(ValueError):
        build(position=PositionInput("LONG", entry_px=90.0))
    with pytest.raises(ValueError):
        build(close_t=0.0)
    with pytest.raises(ValueError):
        build(position=PositionInput("LONG", 0.0, 1.0, 2.0, 1.0, T0))


def test_position_input_from_the_ledger_view() -> None:
    from decimal import Decimal as D

    view = PositionView(book="b", product_id="SOL-EUR", side="LONG", base_qty=D("1"), avg_entry=D("90.5"),
                        opened_at=T0, tp=D("95.25"), sl=D("88"), atr_at_entry=D("2.1"))  # fmt: skip
    p = PositionInput.from_view(view)
    assert (p.entry_px, p.stop_loss, p.take_profit, p.atr_entry, p.opened_at) == (90.5, 88.0, 95.25, 2.1, T0)
    assert PositionInput.from_view(PositionView(book="b", product_id="SOL-EUR")).side == "FLAT"


# ------------------------------------------------------------------------------------------ O1


def test_state_costs_and_spread_come_from_label_config() -> None:
    assert set(Liquidity.model_fields) == {"vol_z_1d"} and "spread_bps" not in FLAT_COLUMNS
    assert set(inspect.signature(StateBuilder.build).parameters) == {"self", "snapshot", "btc", "sentiment", "position",
                                                                     "close_t"}  # fmt: skip
    st = build(snapshot(atr_14_6h_bps=164.0))
    assert st.costs.cost_tp_bps == 50 and st.costs.cost_sl_bps == 92  # 25+50 + 1.5+5+10 = 91.5 rounds half up
    assert st.costs.p_breakeven == pytest.approx(0.43, abs=1e-9)  # 337.5 / 779.5
    wide = StateBuilder(loaded_label({"SOL-EUR": 9.0})).build(
        snapshot=snapshot(atr_14_6h_bps=164.0), btc=None, sentiment=None, position=FLAT, close_t=92.0
    )
    assert wide.costs.cost_sl_bps == 95  # the spread of the label_config, not of the market
    assert wide.costs.cost_tp_bps == 50


def test_the_atr_timeframe_of_the_costs_follows_the_label_config() -> None:
    st = StateBuilder(loaded_label(atr_tf="1d")).build(
        snapshot=snapshot(atr_14_6h_bps=164.0, atr_14_1d_bps=460.0),
        btc=None,
        sentiment=None,
        position=FLAT,
        close_t=92.0,
    )
    ref = build(snapshot(atr_14_6h_bps=460.0))
    assert st.costs.p_breakeven == ref.costs.p_breakeven


def test_unknown_product_has_no_label_spread() -> None:
    with pytest.raises(ValueError, match="no label spread"):
        build(snapshot("DOGE-EUR"))


# ------------------------------------------------------------------------- flatten and golden


def test_flatten_roundtrip_through_canonical_string() -> None:
    states = [f() for f in ALL_STATES.values()] + [build(sentiment=sentiment_at(T0)), build()]
    for s in states:
        a = flatten_state(s)
        b = flatten_state(DecisionState.model_validate_json(serialize_state(s)))
        assert list(a) == list(b) and all((math.isnan(a[k]) and math.isnan(b[k])) or a[k] == b[k] for k in a), (
            "flatten differs after a canonical round trip"
        )


# -------------------------------------------------------------------------------------- T10


def _batch_snapshots(product: str, bars: pd.DataFrame) -> list[IndicatorSnapshot]:
    ind = compute_indicators(bars)
    rows = ind.to_dict("records")
    return [IndicatorSnapshot.from_row(product, ts.to_pydatetime(), r) for ts, r in zip(ind.index, rows, strict=True)]


def _stream_snapshots(product: str, bars: pd.DataFrame) -> list[IndicatorSnapshot]:
    engine = IndicatorEngine(product)
    out = []
    cols = bars[["open", "high", "low", "close", "volume"]].to_numpy().tolist()
    for ts, (o, h, lo, c, v) in zip(bars.index.to_pydatetime(), cols, strict=True):
        out.append(engine.update(Bar(ts, o, h, lo, c, v)))
    return out


def _leaves(d: object, path: str = "") -> dict[str, object]:
    if isinstance(d, dict):
        return {k: v for key, val in d.items() for k, v in _leaves(val, f"{path}/{key}").items()}
    if isinstance(d, list):
        return {k: v for i, val in enumerate(d) for k, v in _leaves(val, f"{path}[{i}]").items()}
    return {path: d}


def test_state_serialization_parity() -> None:
    """T10: the state built from batch snapshots equals the one built from the incremental engine, byte for byte, except
    one-unit rounding differences on a value that sits on a rounding boundary (at most 1 per 10 000 rows)."""
    builder = StateBuilder(LABEL)
    products = {"BTC-EUR": synthetic_bars(days=20, seed=21, zero_volume_day=None, constant_days=None),
                "SOL-EUR": synthetic_bars(days=20, seed=22, zero_volume_day=None, constant_days=None)}  # fmt: skip
    batch = {p: _batch_snapshots(p, b) for p, b in products.items()}
    stream = {p: _stream_snapshots(p, b) for p, b in products.items()}
    compared = 0
    near_boundary: list[str] = []
    for product in products:
        bars = products[product]
        first = next(i for i, s in enumerate(batch[product]) if not math.isnan(s.atr_14_6h))
        rows = sorted({*range(first, len(bars), 7), len(bars) - 1})
        for i in rows:
            for side in ("FLAT", "LONG"):
                for senti in (None, "fixed"):
                    pos = PositionInput.flat()
                    if side == "LONG" and i - 30 >= first:
                        j = i - 30
                        atr = batch[product][j].atr_14_6h
                        entry = float(bars["close"].iloc[j])
                        pos = PositionInput("LONG", entry, entry - 1.5 * atr, entry + 3 * atr, atr, batch[product][j].t)
                    kw = {"sentiment": sentiment_at(batch[product][i].t) if senti else None, "position": pos,
                          "close_t": float(bars["close"].iloc[i])}  # fmt: skip
                    btc_b = batch["BTC-EUR"][i] if product != "BTC-EUR" else None
                    btc_s = stream["BTC-EUR"][i] if product != "BTC-EUR" else None
                    a = serialize_state(builder.build(snapshot=batch[product][i], btc=btc_b, **kw))  # type: ignore[arg-type]
                    b = serialize_state(builder.build(snapshot=stream[product][i], btc=btc_s, **kw))  # type: ignore[arg-type]
                    compared += 1
                    if a == b:
                        continue
                    la, lb = _leaves(json.loads(a)), _leaves(json.loads(b))
                    diff = {k: (la[k], lb[k]) for k in la if la[k] != lb[k]}
                    for k, (x, y) in diff.items():
                        assert isinstance(x, int | float) and isinstance(y, int | float), (product, i, k, x, y)
                        assert abs(x - y) <= 1.0001 * (1 if isinstance(x, int) and isinstance(y, int) else 0.01), (
                            product, i, k, x, y,
                        )  # fmt: skip
                    near_boundary.append(f"{product}@{i}: {diff}")
    assert compared > 1000
    print(f"T10: {compared} states compared, {len(near_boundary)} rounding-boundary differences: {near_boundary}")
    assert len(near_boundary) <= max(1, compared // 10_000)
    assert np.isfinite(compared)
