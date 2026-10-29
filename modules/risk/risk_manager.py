"""RiskManager: Decision -> OrderIntent or Skip (spec sez. 4.1, steps 1-9; rounding 4.1.3).

One instance per book (sez. 0.6). Pure with respect to the world: quote, ATR, fees, product facts and balances are
arguments, ids and the clock too, so the live gateway, the shadow gateway and the backtest all call the same code with
the same inputs and get the same intent.

``A`` is the ``atr_14_<atr_tf>`` of the snapshot of the decision's bar, in price units, passed by the caller: it is
the value the label used and it is never re-read when the order is sent. ``k_tp``, ``k_sl``, ``atr_tf`` and
``entry_mode`` come from the ``label_config`` of the model (they are what ``p_edge`` was trained for), never from
the risk config.

Order of the gates: fee floor -> EV gate (with ``p_be_buffer`` over the live break-even) -> sizing (coverage cap
included) -> kill switch. The ``BalanceGuard`` is the
final authority on coverage and acts later, at submission.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, SerializerFunctionWrapHandler, model_serializer

from modules.config import EntryTimeout, FallbackFees, RiskConfig
from modules.decision.policy import ENTRY_REGIMES, Decision
from modules.execution.orders import ProductMeta
from modules.execution.rounding import ceil_to, floor_to
from modules.ledger.positions import PositionView
from modules.risk.costs import CostModel, CostParams, FeeRates, round_bps
from modules.risk.label_config import LabelConfig
from modules.risk.sizing import SizeSkip, compute_size

__all__ = [
    "EntryGate",
    "ExitIntent",
    "MarketQuote",
    "OrderIntent",
    "PolicyContractError",
    "RiskManager",
    "Skip",
]

_BPS: Final = Decimal(10_000)
SkipReason = Literal[
    "fee_floor",
    "ev_negative",
    "ev_buffer",
    "below_min_size",
    "insufficient_balance",
    "kill_switch",
    "invalid_levels",
    "no_quote",
]


class PolicyContractError(Exception):
    """The RiskManager received a decision the policy should have filtered: a programming error, not a skip."""


class EntryGate(Protocol):
    """What the RiskManager needs from the KillSwitch: a reason to block new entries of a book, or ``None``."""

    def entry_blocked(self, book: str) -> str | None: ...


@dataclass(frozen=True)
class MarketQuote:
    best_bid: Decimal
    best_ask: Decimal

    @property
    def spread_bps(self) -> float:
        mid = (self.best_bid + self.best_ask) / 2
        return float((self.best_ask - self.best_bid) / mid * _BPS)

    def valid(self) -> bool:
        return 0 < self.best_bid < self.best_ask


class Skip(BaseModel):
    model_config = ConfigDict(frozen=True)

    reason: SkipReason
    detail: str = ""


class _EntryLeg(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["limit_post_only", "limit_marketable"]
    limit_price: Decimal
    time_in_force_s: int
    # True: the exchange expires the order itself (``limit_limit_gtd``, spec 2.15). Only the P4-pre probe sets it;
    # dry, shadow and backtest keep the default and the behaviour they have always had.
    expires_on_exchange: bool = False

    @model_serializer(mode="wrap")
    def _omit_the_default_flag(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """The spec 4.2 JSON of every other intent stays byte for byte what it is: the flag is written only when set."""
        data: dict[str, Any] = handler(self)
        if not self.expires_on_exchange:
            data.pop("expires_on_exchange", None)
        return data


class _Bracket(BaseModel):
    model_config = ConfigDict(frozen=True)

    take_profit: Decimal
    stop_loss: Decimal
    k_tp: float
    k_sl: float
    atr_tf: str
    atr: Decimal


class _RiskBlock(BaseModel):
    model_config = ConfigDict(frozen=True)

    equity_eur: Decimal
    risk_eur: Decimal
    p_edge: float
    scale_p: float
    regime: int
    m_reg: float
    ev_bps: int
    cost_tp_bps: int
    cost_sl_bps: int
    p_breakeven: float | None
    p_be_buffer: float
    spread_bps: int
    min_size_bump: bool


class OrderIntent(BaseModel):
    """Spec sez. 4.2. Prices and sizes are Decimal already rounded to the product increments."""

    model_config = ConfigDict(frozen=True)

    intent_id: str
    decision_id: str
    product_id: str
    side: Literal["BUY"] = "BUY"
    book: str
    entry: _EntryLeg
    base_size: Decimal
    bracket: _Bracket
    risk: _RiskBlock
    created_at: datetime


class ExitIntent(BaseModel):
    model_config = ConfigDict(frozen=True)

    intent_id: str
    decision_id: str
    product_id: str
    book: str
    base_size: Decimal
    cancel_bracket_order_id: str | None
    reason: str
    created_at: datetime


class RiskManager:
    def __init__(
        self,
        book: str,
        risk: RiskConfig,
        label: LabelConfig,
        fallback_fees: FallbackFees,
        entry_timeout: EntryTimeout,
        gate: EntryGate | None = None,
        costs: CostParams | None = None,
    ) -> None:
        self.book = book
        self._risk = risk
        self._label = label
        self._fallback = fallback_fees
        self._timeout = entry_timeout
        self._gate = gate
        self._costs = costs or CostParams(slippage_buffer_bps=risk.slippage_buffer_bps)
        missing = set(ENTRY_REGIMES) - set(risk.regime_multipliers)
        if missing:
            raise ValueError(
                f"risk.regime_multipliers lacks the entry regimes {sorted(missing)}: the policy breaks the contract"
            )

    def plan_entry(
        self,
        *,
        decision: Decision,
        decision_id: str,
        intent_id: str,
        now: datetime,
        product_id: str,
        quote: MarketQuote,
        atr: Decimal,
        fees: FeeRates,
        meta: ProductMeta,
        equity: Decimal,
        free_quote: Decimal,
        p_be_buffer: float,
        position: PositionView,
    ) -> OrderIntent | Skip:
        if decision.action != "BUY" or position.side != "FLAT":
            raise PolicyContractError(f"plan_entry needs BUY on a FLAT position, got {decision.action}/{position.side}")
        if decision.p_edge is None or decision.regime is None:
            raise PolicyContractError("a BUY decision carries p_edge and regime")
        m_reg = self._risk.regime_multipliers.get(decision.regime)
        if m_reg is None:
            raise PolicyContractError(f"regime {decision.regime} is not allowed for entries")
        if not quote.valid() or atr <= 0:
            return Skip(reason="no_quote", detail=f"bid={quote.best_bid} ask={quote.best_ask} atr={atr}")

        lab, qinc = self._label, meta.quote_increment
        maker = lab.entry_mode == "maker"
        entry = floor_to(quote.best_bid, qinc) if maker else ceil_to(quote.best_ask, qinc)
        take_profit = floor_to(entry + Decimal(str(lab.k_tp)) * atr, qinc)
        stop_loss = ceil_to(entry - Decimal(str(lab.k_sl)) * atr, qinc)
        if not (0 < stop_loss < entry < take_profit) or take_profit - entry < 2 * qinc or entry - stop_loss < 2 * qinc:
            return Skip(reason="invalid_levels", detail=f"sl={stop_loss} entry={entry} tp={take_profit}")

        spread = quote.spread_bps
        model = CostModel(fees=fees, entry_mode=lab.entry_mode, spread_bps=spread, params=self._costs)
        tp_bps = float((take_profit - entry) / entry * _BPS)
        sl_bps = float((entry - stop_loss) / entry * _BPS)
        if not model.passes_fee_floor(tp_bps, self._risk.min_net_edge_bps):
            return Skip(reason="fee_floor", detail=f"tp_bps={tp_bps:.0f} cost_tp={model.cost_tp:.0f}")
        ev = model.ev_bps(decision.p_edge, tp_bps, sl_bps)
        if not ev > self._risk.ev_min_bps:
            return Skip(reason="ev_negative", detail=f"ev_bps={ev:.1f}")
        win, loss = model.tp_net_bps(tp_bps), model.sl_loss_bps(sl_bps)
        if ev < p_be_buffer * (win + loss):  # same as p_edge < p_breakeven(t) + p_be_buffer
            need = p_be_buffer * (win + loss)
            return Skip(reason="ev_buffer", detail=f"ev_bps={ev:.1f} need={need:.1f} buffer={p_be_buffer}")
        p_floor = loss / (win + loss) + p_be_buffer  # the floor of this decision: live break-even plus the buffer

        sized = compute_size(
            equity=equity, free_quote=free_quote, entry=entry, stop_loss=stop_loss, cost_sl_bps=model.cost_sl,
            p_edge=decision.p_edge, p_edge_min=p_floor, p_edge_full=self._risk.p_edge_full, m_reg=m_reg,
            risk_frac=self._risk.risk_per_trade, max_position_frac=self._risk.max_position_frac,
            max_order_quote=self._risk.max_order_quote, meta=meta, fees=fees, fallback_fees=self._fallback,
        )  # fmt: skip
        if isinstance(sized, SizeSkip):
            return Skip(reason=sized.reason, detail=sized.detail)

        if self._gate is not None and (blocked := self._gate.entry_blocked(self.book)) is not None:
            return Skip(reason="kill_switch", detail=blocked)

        return OrderIntent(
            intent_id=intent_id,
            decision_id=decision_id,
            product_id=product_id,
            book=self.book,
            entry=_EntryLeg(
                type="limit_post_only" if maker else "limit_marketable",
                limit_price=entry,
                time_in_force_s=int(self._timeout.maker if maker else self._timeout.taker),
            ),
            base_size=sized.base_size,
            bracket=_Bracket(
                take_profit=take_profit, stop_loss=stop_loss, k_tp=lab.k_tp, k_sl=lab.k_sl, atr_tf=lab.atr_tf, atr=atr
            ),
            risk=_RiskBlock(
                equity_eur=equity,
                risk_eur=sized.risk_eur,
                p_edge=decision.p_edge,
                scale_p=float(sized.scale_p),
                regime=decision.regime,
                m_reg=m_reg,
                ev_bps=round_bps(ev),
                cost_tp_bps=round_bps(model.cost_tp),
                cost_sl_bps=round_bps(model.cost_sl),
                p_breakeven=_round4(model.p_breakeven(tp_bps, sl_bps)),
                p_be_buffer=p_be_buffer,
                spread_bps=round_bps(spread),
                min_size_bump=sized.min_size_bump,
            ),  # fmt: skip
            created_at=now,
        )

    def plan_exit(
        self, *, decision: Decision, decision_id: str, intent_id: str, now: datetime, position: PositionView
    ) -> ExitIntent:
        """SELL decision on a LONG: cancel the active bracket (the gateway waits for the terminal state), then sell."""
        if decision.action != "SELL" or position.side != "LONG":
            raise PolicyContractError(f"plan_exit needs SELL on a LONG position, got {decision.action}/{position.side}")
        return ExitIntent(
            intent_id=intent_id,
            decision_id=decision_id,
            product_id=position.product_id,
            book=self.book,
            base_size=position.base_qty,
            cancel_bracket_order_id=position.bracket_order_id,
            reason=decision.reason,
            created_at=now,
        )


def _round4(x: float | None) -> float | None:
    return None if x is None else round(x, 4)
