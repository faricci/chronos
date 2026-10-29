"""Hand-written DecisionStates shared by the feature, decision and training tests."""

from __future__ import annotations

from modules.features.schemas import (
    AssetSentimentState,
    BtcContext,
    Clock,
    Costs,
    DecisionState,
    Liquidity,
    MarketSentimentState,
    Momentum,
    Position,
    Price,
    RetBps,
    SentimentState,
    Volatility,
)


def sol_flat() -> DecisionState:
    """The pretty example of spec sez. 2.2.1."""
    return DecisionState(
        product="SOL-EUR",
        clock=Clock(hour_utc=14, dow=5),
        position=Position(side="FLAT", hours_held=0, unreal_bps=None, dist_sl_atr=None, dist_tp_atr=None),
        price=Price(
            ret_bps=RetBps(m15=-12, h1=-38, h6=95, d1=210, d3=-340, d7=520),
            seq_1h_bps=[12, -8, 30, -15, -40, 22, -9, -42, 18, 5, -11, -38],
            seq_1d_bps=[140, -260, 310, -90, -410, 75, 210],
            dist_vwap_1d_bps=-55,
            bb_pos_1d=0.41,
            hh_ll_pos_30d=0.61,
        ),
        momentum=Momentum(rsi_14_1h=44, rsi_14_6h=51, rsi_14_1d=47, macd_hist_1d_bps=-18),
        volatility=Volatility(
            atr_14_1h_bps=61,
            atr_14_6h_bps=222,
            atr_14_1d_bps=460,
            rv_1d_bps=410,
            rv_ratio_1d_30d=0.89,
            atr_1d_pctile_365d=38,
        ),  # fmt: skip
        liquidity=Liquidity(vol_z_1d=0.6),
        sentiment=SentimentState(
            asset=AssetSentimentState(
                ewm=0.38,
                ewm_slow=0.21,
                mass=2.71,
                mass_slow=9.40,
                disp=0.22,
                shock_15m=0.30,
                n_1h=3,
                n_24h=14,
                last_choice=2,
                last_age_min=3,
            ),  # fmt: skip
            market=MarketSentimentState(
                ewm=-0.05, ewm_slow=0.02, mass=6.10, mass_slow=31.2, disp=0.41, shock_15m=0.12, n_1h=9
            ),
            fear_greed=54,
        ),
        btc_context=BtcContext(ret_1d_bps=-35, rv_ratio_1d_30d=1.12),
        costs=Costs(entry_mode="maker", cost_tp_bps=120, cost_sl_bps=197, p_breakeven=0.39),
    )


def btc_long() -> DecisionState:
    """BTC (no btc_context), long position, no sentiment (outage)."""
    return DecisionState(
        product="BTC-EUR",
        clock=Clock(hour_utc=0, dow=0),
        position=Position(side="LONG", hours_held=51, unreal_bps=-84, dist_sl_atr=0.75, dist_tp_atr=2.25),
        price=Price(
            ret_bps=RetBps(m15=3, h1=-5, h6=-60, d1=-150, d3=40, d7=300),
            seq_1h_bps=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
            seq_1d_bps=[10, 20, 30, 40, 50, 60, 70],
            dist_vwap_1d_bps=-20,
            bb_pos_1d=-0.35,
            hh_ll_pos_30d=0.02,
        ),
        momentum=Momentum(rsi_14_1h=30, rsi_14_6h=28, rsi_14_1d=35, macd_hist_1d_bps=-120),
        volatility=Volatility(
            atr_14_1h_bps=30,
            atr_14_6h_bps=117,
            atr_14_1d_bps=243,
            rv_1d_bps=200,
            rv_ratio_1d_30d=1.5,
            atr_1d_pctile_365d=90,
        ),  # fmt: skip
        liquidity=Liquidity(vol_z_1d=-0.2),
        sentiment=None,
        btc_context=None,
        costs=Costs(entry_mode="maker", cost_tp_bps=50, cost_sl_bps=91, p_breakeven=0.4),
    )


def cold_start() -> DecisionState:
    """Everything that can be missing is missing: a product right after the history begins."""
    return DecisionState(
        product="ETH-EUR",
        clock=Clock(hour_utc=23, dow=6),
        position=Position(side="FLAT", hours_held=0, unreal_bps=None, dist_sl_atr=None, dist_tp_atr=None),
        price=Price(
            ret_bps=RetBps(m15=0, h1=None, h6=None, d1=None, d3=None, d7=None),
            seq_1h_bps=None,
            seq_1d_bps=None,
            dist_vwap_1d_bps=None,
            bb_pos_1d=None,
            hh_ll_pos_30d=None,
        ),  # fmt: skip
        momentum=Momentum(rsi_14_1h=None, rsi_14_6h=None, rsi_14_1d=None, macd_hist_1d_bps=None),
        volatility=Volatility(
            atr_14_1h_bps=None,
            atr_14_6h_bps=None,
            atr_14_1d_bps=None,
            rv_1d_bps=None,
            rv_ratio_1d_30d=None,
            atr_1d_pctile_365d=None,
        ),  # fmt: skip
        liquidity=Liquidity(vol_z_1d=None),
        sentiment=SentimentState(asset=None, market=None, fear_greed=None),
        btc_context=BtcContext(ret_1d_bps=None, rv_ratio_1d_30d=None),
        costs=Costs(entry_mode="taker", cost_tp_bps=170, cost_sl_bps=257, p_breakeven=None),
    )


ALL_STATES = {"sol_flat": sol_flat, "btc_long": btc_long, "cold_start": cold_start}
