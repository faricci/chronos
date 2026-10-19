"""Bar-level fill model (spec sez. 5.1.3, column "Approssimazione backtest").

Single source of truth for how a limit/stop/market order is filled from OHLCV bars. The training labels
(``training.labeling.triple_barrier``), the backtest and the unit tests import these functions; nobody
re-implements fill rules. The scalar kernels are ``numba.njit`` so the labeling loop can call them in nopython
mode, and plain Python can call them too (same compiled code, same numbers).

The tick-level face (``TickFillModel``, order book + market trades, Decimal arithmetic) lives at the end of this file
with the same ``FillModelParams``: it holds the exit-side rules of the ``BracketSimulator`` (take profit through a
trade, stop trigger, stop liquidation with the extra slip); the SAME strict comparisons as the bar face.

Price rules (``spread_bps`` = s, ``slip_bps`` = slip, ``x_bps`` = stop_extra_slip_bps; sez. 0.3 and 3.1.3):

| leg                      | price                                                       | fee   |
|--------------------------|-------------------------------------------------------------|-------|
| entry maker              | ``close[t+F-1] * (1 - s/2)``, first bar of ``[t+F, t+F+T_in-1]`` with low < limit | maker |
| entry taker              | ``open[t+F] * (1 + s/2 + slip)``                            | taker |
| TP                       | ``upper`` when ``high > upper``                             | maker |
| SL                       | ``min(lower, open) * (1 - (s/2 + slip + x))``               | taker |
| model exit / vertical    | ``px * (1 - (s/2 + slip))``                                 | taker |
| TP and SL in one bar     | SL                                                          | -     |
| fill bar                 | only the SL acts (no bracket yet; the watchdog covers it)   | -     |

``F = entry_first_bar = 1 + ceil(S / 60) = 3``: the decision of bar ``t`` is computed at ``ts_bar_close + S``.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import numba
from pydantic import BaseModel, ConfigDict, Field

from modules.execution.rounding import floor_to

__all__ = [
    "BarFillModel",
    "FillModelParams",
    "TickFillModel",
    "maker_entry_limit",
    "maker_entry_filled",
    "market_exit_px",
    "sl_exit_px",
    "taker_entry_px",
    "tp_touched",
]

_BPS = 1e4


# ---------------------------------------------------------------------------------------------- kernels


@numba.njit(cache=True)
def maker_entry_limit(close: float, spread_bps: float) -> float:
    """Post-only limit at the estimated best bid: half a spread under the close."""
    return close * (1.0 - spread_bps / 2.0 / _BPS)


@numba.njit(cache=True)
def maker_entry_filled(low: float, limit: float) -> bool:
    """A resting buy fills only when the market trades strictly below the limit."""
    return low < limit


@numba.njit(cache=True)
def taker_entry_px(next_open: float, spread_bps: float, slip_bps: float) -> float:
    """Marketable entry at the next bar open, paying half the spread plus the slippage buffer."""
    return next_open * (1.0 + (spread_bps / 2.0 + slip_bps) / _BPS)


@numba.njit(cache=True)
def tp_touched(high: float, upper: float) -> bool:
    """A resting sell at ``upper`` fills only when the market trades strictly above it."""
    return high > upper


@numba.njit(cache=True)
def sl_exit_px(lower: float, bar_open: float, spread_bps: float, slip_bps: float, x_bps: float) -> float:
    """Stop modelled as a market sell: gap-aware (``min(stop, open)``) then spread, slippage and extra stop slip."""
    return min(lower, bar_open) * (1.0 - (spread_bps / 2.0 + slip_bps + x_bps) / _BPS)


@numba.njit(cache=True)
def market_exit_px(px: float, spread_bps: float, slip_bps: float) -> float:
    """Marketable exit (model SELL, watchdog, vertical barrier): half the spread plus slippage."""
    return px * (1.0 - (spread_bps / 2.0 + slip_bps) / _BPS)


# ------------------------------------------------------------------------------------------ parameters


class FillModelParams(BaseModel):
    """Parameters shared by the bar and tick faces. Loadable from ``fill_model.json``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    latency_ms: float = Field(default=300, ge=0)
    queue_haircut: float = Field(default=0.25, ge=0, le=1)
    stop_extra_slip_bps: float = Field(default=10, ge=0)
    slippage_buffer_bps: float = Field(default=5, ge=0)

    @classmethod
    def from_json(cls, path: Path) -> FillModelParams:
        return cls.model_validate(json.loads(path.read_text(encoding="utf-8")))


class BarFillModel:
    """Object face of the kernels above for callers that work bar by bar (backtest, tests)."""

    def __init__(self, params: FillModelParams | None = None) -> None:
        self.params = params or FillModelParams()

    def maker_limit(self, close: float, spread_bps: float) -> float:
        return float(maker_entry_limit(close, spread_bps))

    def maker_filled(self, low: float, limit: float) -> bool:
        return bool(maker_entry_filled(low, limit))

    def maker_fill_qty_cap(self, bar_volume: float) -> float:
        """Largest base quantity a resting order can get from a bar (queue haircut on the bar volume)."""
        return self.params.queue_haircut * bar_volume

    def taker_entry(self, next_open: float, spread_bps: float) -> float:
        return float(taker_entry_px(next_open, spread_bps, self.params.slippage_buffer_bps))

    def tp_touched(self, high: float, upper: float) -> bool:
        return bool(tp_touched(high, upper))

    def sl_exit(self, lower: float, bar_open: float, spread_bps: float) -> float:
        p = self.params
        return float(sl_exit_px(lower, bar_open, spread_bps, p.slippage_buffer_bps, p.stop_extra_slip_bps))

    def market_exit(self, px: float, spread_bps: float) -> float:
        return float(market_exit_px(px, spread_bps, self.params.slippage_buffer_bps))


class TickFillModel:
    """Tick face of the fill model (spec sez. 5.1.3, column "Regola dry"): exits of a virtual bracket.

    Inputs are exact ``Decimal`` prices and sizes (a trade of the ``market_trades`` channel, a ``WalkResult`` of the
    ``LocalOrderBook``). The take-profit test is the bar face's own kernel (``tp_touched``: strictly above), so the
    two faces cannot disagree on the boundary.

    | event                          | rule                                                                      |
    |--------------------------------|---------------------------------------------------------------------------|
    | TP (limit sell)                | a trade prints strictly above TP; quantity <= ``queue_haircut`` x trade   |
    | stop trigger                   | ``last <= stop``                                                          |
    | stop liquidation               | walk of the bids, then ``stop_extra_slip_bps`` off the VWAP, taker fee    |
    """

    def __init__(self, params: FillModelParams | None = None) -> None:
        self.params = params or FillModelParams()
        self._haircut = Decimal(repr(float(self.params.queue_haircut)))
        self._extra_slip = Decimal(repr(float(self.params.stop_extra_slip_bps))) / Decimal(10_000)

    def tp_touched(self, trade_price: Decimal, tp: Decimal) -> bool:
        return bool(tp_touched(float(trade_price), float(tp)))

    def tp_fill_qty(self, trade_size: Decimal, remaining: Decimal, base_increment: Decimal) -> Decimal:
        """Base quantity a resting sell gets from one trade through its price: the haircut share of the trade, on the
        increment, never more than what is left."""
        return min(remaining, floor_to(self._haircut * trade_size, base_increment))

    def stop_triggered(self, last: Decimal, stop: Decimal) -> bool:
        return last <= stop

    def stop_exit_price(self, vwap: Decimal, quote_increment: Decimal) -> Decimal:
        """Price of the stop liquidation: the VWAP of the bids walked, minus the extra stop slip, rounded down."""
        return floor_to(vwap * (1 - self._extra_slip), quote_increment)
