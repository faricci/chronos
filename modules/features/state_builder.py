"""StateBuilder: ``IndicatorSnapshot`` + sentiment + position -> ``DecisionState`` (spec sez. 2.3 B.2, decision O1).

The same pure function builds the state in training (snapshots rebuilt from ``compute_indicators`` rows with
``IndicatorSnapshot.from_row``) and at serving (snapshots from the incremental ``IndicatorEngine``). Everything in the
state is a function of the bars up to ``t`` and of the model's ``label_config``:

- no live spread, no live fee, no quote: the ``costs`` block uses the fees and the spread of the ``label_config``
  (``LoadedLabelConfig.spread_for``), exactly as the labels do. The signature has no parameter that could carry them;
  the live costs belong to the ``RiskManager``;
- the only live input is ``position`` (the real state of the account), and its numbers refer to ``close[t]``, never to
  a live mid.

Rounding: ``int(round(x))`` for integer fields, ``round(x, 2)`` for ratios (the schema rounds), NaN -> ``None``, and
the clamps of sez. 2.3 B.2 to keep token-out-of-distribution values away from the model.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

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
from modules.ledger.positions import PositionView
from modules.market.indicators import IndicatorSnapshot
from modules.risk.costs import CostModel, round_bps
from modules.risk.label_config import LoadedLabelConfig
from modules.sentiment.schemas import SentimentSnapshot

__all__ = ["PositionInput", "StateBuilder"]

BPS_CLAMP = 5000
VOL_Z_CLAMP = 6.0
BB_POS_RANGE = (-1.0, 2.0)
DIST_ATR_CLAMP = 20.0


def _num(x: float | None) -> float | None:
    return None if x is None or math.isnan(x) else float(x)


def _bps(x: float | None, lo: int = -BPS_CLAMP, hi: int = BPS_CLAMP) -> int | None:
    v = _num(x)
    return None if v is None else max(lo, min(hi, int(round(v))))


def _ratio(x: float | None, lo: float | None = None, hi: float | None = None) -> float | None:
    v = _num(x)
    if v is None:
        return None
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return round(v, 2)


def _seq(values: tuple[float, ...] | None) -> list[int] | None:
    return None if values is None else [max(-BPS_CLAMP, min(BPS_CLAMP, int(round(v)))) for v in values]


@dataclass(frozen=True)
class PositionInput:
    """The real position of the account, as the state needs it (float prices: the state only has bps and ATR ratios)."""

    side: Literal["FLAT", "LONG"]
    entry_px: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    atr_entry: float | None = None
    opened_at: datetime | None = None

    @classmethod
    def flat(cls) -> PositionInput:
        return cls("FLAT")

    @classmethod
    def from_view(cls, v: PositionView) -> PositionInput:
        if not v.is_long:
            return cls.flat()

        def f(x: Decimal | None) -> float | None:
            return None if x is None else float(str(x))

        return cls("LONG", f(v.avg_entry), f(v.sl), f(v.tp), f(v.atr_at_entry), v.opened_at)


class StateBuilder:
    def __init__(self, label: LoadedLabelConfig) -> None:
        self._label = label
        self._cfg = label.cfg
        self._atr_bps_field = f"atr_14_{label.cfg.atr_tf}_bps"

    def build(
        self,
        *,
        snapshot: IndicatorSnapshot,
        btc: IndicatorSnapshot | None,
        sentiment: SentimentSnapshot | None,
        position: PositionInput,
        close_t: float,
    ) -> DecisionState:
        """``close_t``: close of the 1m bar that ends at ``snapshot.t`` (reference price of the position block only)."""
        if not close_t > 0:
            raise ValueError("close_t must be positive")
        product = snapshot.product_id
        if product.startswith("BTC-") and btc is not None:
            raise ValueError("a BTC product has no btc_context: pass btc=None")
        s = snapshot
        return DecisionState(
            product=product,
            clock=Clock(hour_utc=s.t.hour, dow=s.t.weekday()),
            position=self._position(s, position, close_t),
            price=Price(
                ret_bps=RetBps.model_validate(
                    {
                        "15m": _bps(s.ret_15m_bps),
                        "1h": _bps(s.ret_1h_bps),
                        "6h": _bps(s.ret_6h_bps),
                        "1d": _bps(s.ret_1d_bps),
                        "3d": _bps(s.ret_3d_bps),
                        "7d": _bps(s.ret_7d_bps),
                    }  # fmt: skip
                ),
                seq_1h_bps=_seq(s.seq_1h_bps),
                seq_1d_bps=_seq(s.seq_1d_bps),
                dist_vwap_1d_bps=_bps(s.dist_vwap_1d_bps),
                bb_pos_1d=_ratio(s.bb_pos_1d, *BB_POS_RANGE),
                hh_ll_pos_30d=_ratio(s.hh_ll_pos_30d),
            ),
            momentum=Momentum(
                rsi_14_1h=_bps(s.rsi_14_1h, 0, 100),
                rsi_14_6h=_bps(s.rsi_14_6h, 0, 100),
                rsi_14_1d=_bps(s.rsi_14_1d, 0, 100),
                macd_hist_1d_bps=_bps(s.macd_hist_1d_bps),
            ),  # fmt: skip
            volatility=Volatility(
                atr_14_1h_bps=_bps(s.atr_14_1h_bps, 0, 10**6),
                atr_14_6h_bps=_bps(s.atr_14_6h_bps, 0, 10**6),
                atr_14_1d_bps=_bps(s.atr_14_1d_bps, 0, 10**6),
                rv_1d_bps=_bps(s.rv_1d_bps, 0, 10**6),
                rv_ratio_1d_30d=_ratio(s.rv_ratio_1d_30d),
                atr_1d_pctile_365d=_bps(s.atr_1d_pctile_365d, 0, 100),
            ),  # fmt: skip
            liquidity=Liquidity(vol_z_1d=_ratio(s.vol_z_1d, -VOL_Z_CLAMP, VOL_Z_CLAMP)),
            sentiment=self._sentiment(sentiment),
            btc_context=None if btc is None else self._btc(btc),
            costs=self._costs(s),
        )

    # ---------------------------------------------------------------------------------- blocks

    def _position(self, s: IndicatorSnapshot, p: PositionInput, close_t: float) -> Position:
        if p.side == "FLAT":
            return Position(side="FLAT", hours_held=0, unreal_bps=None, dist_sl_atr=None, dist_tp_atr=None)
        if None in (p.entry_px, p.stop_loss, p.take_profit, p.atr_entry, p.opened_at):
            raise ValueError("a LONG position needs entry_px, stop_loss, take_profit, atr_entry and opened_at")
        assert p.entry_px is not None and p.stop_loss is not None and p.take_profit is not None
        assert p.atr_entry is not None and p.opened_at is not None
        if not (p.entry_px > 0 and p.atr_entry > 0):
            raise ValueError("entry_px and atr_entry must be positive")
        hours = max(0, math.floor((s.t - p.opened_at).total_seconds() / 3600.0))
        clamp = DIST_ATR_CLAMP
        return Position(
            side="LONG",
            hours_held=hours,
            unreal_bps=_bps(1e4 * (close_t / p.entry_px - 1.0)),
            dist_sl_atr=_ratio((close_t - p.stop_loss) / p.atr_entry, -clamp, clamp),
            dist_tp_atr=_ratio((p.take_profit - close_t) / p.atr_entry, -clamp, clamp),
        )

    @staticmethod
    def _btc(b: IndicatorSnapshot) -> BtcContext:
        return BtcContext(ret_1d_bps=_bps(b.ret_1d_bps), rv_ratio_1d_30d=_ratio(b.rv_ratio_1d_30d))

    @staticmethod
    def _sentiment(sn: SentimentSnapshot | None) -> SentimentState | None:
        if sn is None:
            return None
        a, m = sn.asset, sn.market
        return SentimentState(
            asset=AssetSentimentState(
                ewm=a.ewm,
                ewm_slow=a.ewm_slow,
                mass=a.mass,
                mass_slow=a.mass_slow,
                disp=a.disp,
                shock_15m=a.shock_15m,
                n_1h=a.n_1h,
                n_24h=a.n_24h,
                last_choice=a.last_choice,
                last_age_min=a.last_age_min,
            ),  # fmt: skip
            market=MarketSentimentState(
                ewm=m.ewm,
                ewm_slow=m.ewm_slow,
                mass=m.mass,
                mass_slow=m.mass_slow,
                disp=m.disp,
                shock_15m=m.shock_15m,
                n_1h=m.n_1h,
            ),  # fmt: skip
            fear_greed=None if sn.fear_greed is None else sn.fear_greed.value,
        )

    def _costs(self, s: IndicatorSnapshot) -> Costs:
        """Fees, slippage and spread of the label_config and the ATR in bps of the snapshot: same in both paths."""
        label, cfg = self._label, self._cfg
        spread = label.spread_for(s.product_id, s.t)
        model = CostModel(fees=label.fees, entry_mode=cfg.entry_mode, spread_bps=spread, params=label.costs)
        atr_bps = _num(getattr(s, self._atr_bps_field))
        p_be = None if atr_bps is None else model.p_breakeven(cfg.k_tp * atr_bps, cfg.k_sl * atr_bps)
        return Costs(
            entry_mode=cfg.entry_mode,
            cost_tp_bps=round_bps(model.cost_tp),
            cost_sl_bps=round_bps(model.cost_sl),
            p_breakeven=p_be,
        )
