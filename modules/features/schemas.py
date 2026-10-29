"""DecisionState v1: the unified payload the decider reads (spec sez. 2.2.1).

Encoding rules (numbers must be short and stable because they become tokens):
- returns and distances are INTEGER basis points; RSI and percentiles are integers 0..100; ratios have 2 decimals;
- keys are in the declaration order of these models, JSON is compact (``serializer.serialize_state``);
- no absolute timestamp in the state, only the cyclic ``hour_utc`` and ``dow``;
- a missing value is ``None`` (``null``), never an omitted key.

Decimal fields round to 2 places in a validator, so ``StateBuilder`` and any hand-written test state serialize the same
way. Integer fields are plain ``int``: the builder is responsible for ``int(round(x))`` (sez. 2.3 B.2).
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

STATE_VERSION: Final = "chronos.state.v1"


def _round2(v: object) -> object:
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, int | float):
        return round(float(v), 2)
    return v


Dec2 = Annotated[float, BeforeValidator(_round2)]


class _Block(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Clock(_Block):
    hour_utc: int
    dow: int


class Position(_Block):
    side: Literal["FLAT", "LONG"]
    hours_held: int
    unreal_bps: int | None
    dist_sl_atr: Dec2 | None
    dist_tp_atr: Dec2 | None


class RetBps(_Block):
    """Keys of the spec ("15m", "1h", ...) are not valid identifiers: they are aliases, always dumped by alias."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    m15: int | None = Field(alias="15m")
    h1: int | None = Field(alias="1h")
    h6: int | None = Field(alias="6h")
    d1: int | None = Field(alias="1d")
    d3: int | None = Field(alias="3d")
    d7: int | None = Field(alias="7d")


class Price(_Block):
    ret_bps: RetBps
    seq_1h_bps: list[int] | None
    seq_1d_bps: list[int] | None
    dist_vwap_1d_bps: int | None
    bb_pos_1d: Dec2 | None
    hh_ll_pos_30d: Dec2 | None


class Momentum(_Block):
    rsi_14_1h: int | None
    rsi_14_6h: int | None
    rsi_14_1d: int | None
    macd_hist_1d_bps: int | None


class Volatility(_Block):
    atr_14_1h_bps: int | None
    atr_14_6h_bps: int | None
    atr_14_1d_bps: int | None
    rv_1d_bps: int | None
    rv_ratio_1d_30d: Dec2 | None
    atr_1d_pctile_365d: int | None


class Liquidity(_Block):
    # No spread: the live spread cannot be rebuilt from history, so it never enters the state (decision O1, option C)
    vol_z_1d: Dec2 | None


class AssetSentimentState(_Block):
    ewm: Dec2
    ewm_slow: Dec2
    mass: Dec2
    mass_slow: Dec2
    disp: Dec2
    shock_15m: Dec2
    n_1h: int
    n_24h: int
    last_choice: int | None
    last_age_min: int | None


class MarketSentimentState(_Block):
    ewm: Dec2
    ewm_slow: Dec2
    mass: Dec2
    mass_slow: Dec2
    disp: Dec2
    shock_15m: Dec2
    n_1h: int


class SentimentState(_Block):
    asset: AssetSentimentState | None
    market: MarketSentimentState | None
    fear_greed: int | None


class BtcContext(_Block):
    ret_1d_bps: int | None
    rv_ratio_1d_30d: Dec2 | None


class Costs(_Block):
    entry_mode: Literal["maker", "taker"]
    cost_tp_bps: int
    cost_sl_bps: int
    p_breakeven: Dec2 | None


class DecisionState(_Block):
    v: Literal["chronos.state.v1"] = STATE_VERSION
    product: str
    clock: Clock
    position: Position
    price: Price
    momentum: Momentum
    volatility: Volatility
    liquidity: Liquidity
    sentiment: SentimentState | None
    btc_context: BtcContext | None
    costs: Costs
