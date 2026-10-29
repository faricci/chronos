"""``ProbePlanner``: the deterministic entry of the probe (plan sez. 2). No model, no signal: the market fixes only
price, size and levels. A pure function of the facts it is given, so every skip and every intent can be tested
without a clock, a network or a database.

At a slot the first false condition, in this order, is the skip reason:

1. no ``STOP``/``FLATTEN`` file;  2. no entry block (``KillSwitch``, series pause, end of the probe);
3. day and total limits;  4. position FLAT and no open order on the product;  5. balance snapshot younger than 120 s;
6. quote younger than ``quote_max_age_s`` and spread within ``max_spread_bps``;  7. ``ATR1h`` known and within
``max_atr1h_bps``;  8. budget: ``required_quote <= min(Q_max, L_rem - dust_mark)``;  9. size sellable at the stop.

The intent is a post-only BUY at ``floor_to(best_bid)`` that the exchange itself expires (``expires_on_exchange``), with
a bracket at ``tp_frac`` / ``sl_frac`` and neutral risk numbers. It never carries a taker or a market entry.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

from modules.config import ExecProbeConfig, FallbackFees
from modules.execution.gateway import is_sellable
from modules.execution.orders import ProductMeta
from modules.execution.rounding import ceil_to, floor_to
from modules.probe import limits
from modules.probe.budget import BudgetState
from modules.risk.costs import FeeRates, coverage_fee_rate, max_base_for_quote, required_quote
from modules.risk.risk_manager import MarketQuote, OrderIntent

_BPS: Final = Decimal(10_000)
ATR_PERIOD: Final = 14
HOUR_S: Final = 3600
# Skip reasons that can clear within the seconds of a slot window: the scheduler asks again until the window closes.
TRANSIENT_SKIPS: Final = frozenset(
    {
        "balance_stale", "quote_unavailable", "quote_stale", "quote_invalid", "spread_wide", "atr_unavailable",
        "atr_high", "budget_unavailable",
    }
)  # fmt: skip


def decision_id(product_id: str, slot: datetime) -> str:
    """``probe:<product>:<slot>``: the identity of the slot, from which the ``client_order_id`` (uuid5) derives."""
    return f"probe:{product_id}:{slot.astimezone(UTC):%Y%m%dT%H%MZ}"


@dataclass(frozen=True, slots=True)
class ProbeSkip:
    reason: str
    detail: str = ""

    @property
    def transient(self) -> bool:
        return self.reason in TRANSIENT_SKIPS

    @property
    def ends_the_probe(self) -> bool:
        """The budget cannot fund even the smallest protectable entry: the probe is over (plan sez. 1.1)."""
        return self.reason == "budget_exhausted"


@dataclass(frozen=True, slots=True)
class PlanFacts:
    """Everything the planner looks at, already gathered (by the scheduler, or by a test)."""

    now: datetime
    slot: datetime
    product_id: str
    meta: ProductMeta
    fees: FeeRates
    fallback: FallbackFees
    manual_stop: bool
    entry_blocked: str | None  # KillSwitch / series pause / end of the probe
    limit_blocked: str | None  # ProbeLimiter
    position_flat: bool
    open_orders: int  # on the product, ours or not
    balance_age_s: float | None
    free_quote: Decimal
    quote: MarketQuote | None
    quote_age_s: float | None
    atr1h: Decimal | None  # price units, Wilder 14 on completed ONE_HOUR candles
    last_close: Decimal | None
    budget: BudgetState | None


class ProbePlanner:
    def __init__(self, cfg: ExecProbeConfig, *, expires_on_exchange: bool = True) -> None:
        self._cfg = cfg
        # False only for the GTC fallback the user would have to accept in writing (plan sez. 3.3, 10.1)
        self._gtd = expires_on_exchange

    def plan(self, f: PlanFacts) -> OrderIntent | ProbeSkip:
        cfg = self._cfg
        if f.manual_stop:
            return ProbeSkip("manual_stop")
        if f.entry_blocked is not None:
            return ProbeSkip("entry_blocked", f.entry_blocked)
        if f.limit_blocked is not None:
            return ProbeSkip(f.limit_blocked)
        if not f.position_flat:
            return ProbeSkip("position_open")
        if f.open_orders > 0:
            return ProbeSkip("open_orders", f"{f.open_orders} open order(s) on {f.product_id}")
        if f.balance_age_s is None or f.balance_age_s > limits.BALANCE_MAX_AGE_S:
            return ProbeSkip("balance_stale", f"snapshot age {f.balance_age_s}")
        if f.quote is None or f.quote_age_s is None:
            return ProbeSkip("quote_unavailable")
        if f.quote_age_s > cfg.quote_max_age_s:
            return ProbeSkip("quote_stale", f"age {f.quote_age_s:.1f} s")
        if not f.quote.valid():
            return ProbeSkip("quote_invalid", f"bid={f.quote.best_bid} ask={f.quote.best_ask}")
        if f.quote.spread_bps > cfg.max_spread_bps:
            return ProbeSkip("spread_wide", f"{f.quote.spread_bps:.2f} bps")
        if f.atr1h is None or f.last_close is None or f.atr1h <= 0 or f.last_close <= 0:
            return ProbeSkip("atr_unavailable")
        atr_bps = f.atr1h / f.last_close * _BPS
        if atr_bps > Decimal(str(cfg.max_atr1h_bps)):
            return ProbeSkip("atr_high", f"{float(atr_bps):.1f} bps")
        if f.budget is None:
            return ProbeSkip("budget_unavailable")

        meta = f.meta
        f_cov = coverage_fee_rate(f.fees, f.fallback)
        protectable = limits.n_br(meta.quote_min_size, cfg.sl_frac) * (1 + f_cov)
        budget_cap = min(
            f.budget.entry_cap, cfg.max_entry_quote
        )  # the budget already does this; the planner repeats it
        if budget_cap < protectable:
            return ProbeSkip("budget_exhausted", f"cap {budget_cap} < {protectable:.4f}")
        cap = min(budget_cap, f.free_quote)
        if cap < protectable:
            return ProbeSkip("insufficient_balance", f"free {f.free_quote} < {protectable:.4f}")

        qinc = meta.quote_increment
        entry = floor_to(f.quote.best_bid, qinc)  # post-only: never at or above the ask
        size = max_base_for_quote(cap, entry, f_cov, qinc, meta.base_increment)
        take_profit = floor_to(entry * (1 + cfg.tp_frac), qinc)
        stop_loss = ceil_to(entry * (1 - cfg.sl_frac), qinc)
        if not (0 < stop_loss < entry < take_profit) or take_profit - entry < 2 * qinc or entry - stop_loss < 2 * qinc:
            return ProbeSkip("invalid_levels", f"sl={stop_loss} entry={entry} tp={take_profit}")
        if size <= 0 or size * entry < limits.n_br(meta.quote_min_size, cfg.sl_frac):
            # the cap leaves less than a protectable notional (one tick of cushion and the rounding eat the slack)
            reason = "budget_exhausted" if budget_cap <= f.free_quote else "insufficient_balance"
            return ProbeSkip(reason, f"size {size} at {entry} is below the protectable notional")
        if not is_sellable(size, stop_loss, meta):
            return ProbeSkip("not_sellable_at_stop", f"size {size} at stop {stop_loss}")
        if required_quote(entry, size, f_cov, qinc) > cap:  # cannot happen by construction: a belt on the braces
            return ProbeSkip("budget_exhausted", "required_quote above the cap")
        return self._intent(f, entry, size, take_profit, stop_loss)

    def _intent(self, f: PlanFacts, entry: Decimal, size: Decimal, tp: Decimal, sl: Decimal) -> OrderIntent:
        assert f.quote is not None and f.atr1h is not None and f.budget is not None
        did = decision_id(f.product_id, f.slot)
        body: dict[str, Any] = {
            "intent_id": did,
            "decision_id": did,
            "product_id": f.product_id,
            "side": "BUY",
            "book": limits.BOOK,
            "entry": {
                "type": "limit_post_only",
                "limit_price": entry,
                "time_in_force_s": self._cfg.entry_timeout_s,
                "expires_on_exchange": self._gtd,
            },
            "base_size": size,
            "bracket": {
                "take_profit": tp,
                "stop_loss": sl,
                "k_tp": 0.0,
                "k_sl": 0.0,
                "atr_tf": "probe",
                "atr": f.atr1h,
            },
            "risk": {  # neutral by declaration: the probe has no model, no edge, no regime
                "equity_eur": f.budget.equity,
                "risk_eur": size * (entry - sl),
                "p_edge": 0.0,
                "scale_p": 0.0,
                "regime": 0,
                "m_reg": 0.0,
                "ev_bps": 0,
                "cost_tp_bps": 0,
                "cost_sl_bps": 0,
                "p_breakeven": None,
                "p_be_buffer": 0.0,
                "spread_bps": round(f.quote.spread_bps),
                "min_size_bump": False,
            },  # fmt: skip
            "created_at": f.now,
        }
        return OrderIntent.model_validate(body)


# ------------------------------------------------------------------------------------------------- ATR


def atr_wilder(
    candles: Sequence[dict[str, Any]], now: datetime, period: int = ATR_PERIOD
) -> tuple[Decimal, Decimal] | None:
    """``(ATR, last close)`` in price units: Wilder smoothing of the true range of the COMPLETED hourly candles.

    ``candles`` are the REST ``ONE_HOUR`` candles (``start`` epoch seconds, ``high``, ``low``, ``close``
    strings), in any order. The candle still forming (its hour has not ended at ``now``) is dropped. ``None`` with
    fewer than ``period + 1`` completed candles or an unreadable one."""
    try:
        rows = sorted(
            (int(c["start"]), Decimal(str(c["high"])), Decimal(str(c["low"])), Decimal(str(c["close"])))
            for c in candles
        )
    except (KeyError, ValueError, ArithmeticError, TypeError):
        return None
    horizon = now.timestamp()
    rows = [r for r in rows if r[0] + HOUR_S <= horizon]
    if len(rows) < period + 1:
        return None
    true_ranges: list[Decimal] = []
    for prev, cur in zip(rows, rows[1:], strict=False):
        _, high, low, _ = cur
        prev_close = prev[3]
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    atr = sum(true_ranges[:period], Decimal(0)) / period
    for tr in true_ranges[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr, rows[-1][3]
