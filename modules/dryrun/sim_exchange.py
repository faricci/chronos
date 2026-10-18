"""Bar-level execution venue (spec sez. 5.1.3, column "Approssimazione backtest"; decision O2.4).

``SimulatedExchange`` decides WHEN and AT WHAT PRICE an order fills from OHLCV bars. It has no balances, never
reserves anything and does not know the ``BalanceGuard``: it is a venue, the same role the exchange plays for the live
gateway. All prices come from the kernels of ``modules/dryrun/fill_model.py`` (the ones the labels use), so a backtest
fill and a label fill can only differ by the roundings of the order (limits are rounded in favour of safety).

Rules::

    entry maker  rests on bars [t+F, t+F+T_in-1]; fills ALL-OR-NOTHING on the first bar with low < limit AND
                 qty <= queue_haircut * volume (a bar with low < limit but too little volume does not fill; counted);
    entry taker  fills at taker_entry_px(open[t+F]) when that is <= the limit, else it expires;
    stop         acts from the fill bar included (no bracket exists yet, the watchdog covers it); price
                 sl_exit_px(...), taker fee;
    take-profit  acts from the bar after the fill when high > TP; price = TP, maker fee;
    same bar     stop wins;
    horizon      at bar fill+H, market_exit_px(close), taker fee;
    model SELL   decided at bar t', executed at market_exit_px(open[t'+F]) if the bracket did not exit before that bar.

Sell prices are rounded DOWN and buy prices UP to the quote increment (never favourable to the strategy).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Literal

import numpy as np

from modules.dryrun.fill_model import BarFillModel, market_exit_px, sl_exit_px, taker_entry_px
from modules.risk.costs import FeeRates

__all__ = ["BracketOutcome", "EntryOutcome", "MarketBars", "SimulatedExchange"]

ExitReason = Literal["TP", "SL", "HORIZON", "SELL"]


@dataclass(frozen=True)
class MarketBars:
    """Raw 1m bars of one product on a contiguous grid. ``ts_close[i] = ts[i] + 1 min``, ``datetime64[ns]`` UTC."""

    product_id: str
    ts_close: np.ndarray
    o: np.ndarray
    h: np.ndarray
    lo: np.ndarray
    c: np.ndarray
    v: np.ndarray  # base volume


@dataclass(frozen=True)
class EntryOutcome:
    status: Literal["FILLED", "EXPIRED"]
    bar: int  # fill bar, or last bar of the entry window
    price: Decimal | None
    haircut_blocked_bars: int = 0


@dataclass(frozen=True)
class BracketOutcome:
    reason: ExitReason
    bar: int
    price: Decimal


def _q(x: float, increment: Decimal, mode: str) -> Decimal:
    return Decimal(repr(float(x))).quantize(increment, rounding=mode)


class SimulatedExchange:
    def __init__(
        self,
        fill: BarFillModel,
        fees: FeeRates,
        quote_increment_of: dict[str, Decimal],
        entry_first_bar: int,
        entry_timeout_bars: int,
        horizon_bars: int,
    ) -> None:
        self.fill = fill
        self.fees = fees
        self._qinc = quote_increment_of
        self.F = entry_first_bar
        self.T_in = entry_timeout_bars
        self.H = horizon_bars

    # ----------------------------------------------------------------------------------- entry

    def entry_maker(self, m: MarketBars, decision_bar: int, limit: Decimal, size: Decimal) -> EntryOutcome:
        """Resting post-only buy. All-or-nothing; the queue haircut caps the quantity a bar can give."""
        first = decision_bar + self.F
        last = min(first + self.T_in - 1, len(m.c) - 1)
        if first > len(m.c) - 1:
            return EntryOutcome("EXPIRED", len(m.c) - 1, None)
        lim = float(limit)
        window = slice(first, last + 1)
        touched = m.lo[window] < lim
        cap = self.fill.params.queue_haircut * m.v[window]
        ok = touched & (cap >= float(size))
        if ok.any():
            k = int(np.argmax(ok))
            return EntryOutcome("FILLED", first + k, limit, int((touched[:k] & ~ok[:k]).sum()))
        return EntryOutcome("EXPIRED", last, None, int((touched & ~ok).sum()))

    def entry_taker(
        self, m: MarketBars, decision_bar: int, limit: Decimal, spread_bps: float, product: str
    ) -> EntryOutcome:
        bar = decision_bar + self.F
        if bar > len(m.c) - 1:
            return EntryOutcome("EXPIRED", len(m.c) - 1, None)
        raw = taker_entry_px(float(m.o[bar]), spread_bps, self.fill.params.slippage_buffer_bps)
        px = _q(raw, self._qinc[product], ROUND_UP)
        return EntryOutcome("FILLED", bar, px) if px <= limit else EntryOutcome("EXPIRED", bar, None)

    # --------------------------------------------------------------------------------- bracket

    def bracket(
        self, m: MarketBars, fill_bar: int, tp: Decimal, sl: Decimal, spread_bps: float
    ) -> BracketOutcome | None:
        """First exit of the standalone bracket protecting a position filled at ``fill_bar``.

        ``None`` when the data ends before an exit or the horizon (the trade cannot be resolved yet).
        """
        n = len(m.c)
        vertical = fill_bar + self.H
        end = min(vertical, n - 1)
        stop = float(sl)
        target = float(tp)
        p = self.fill.params
        lo = m.lo[fill_bar : end + 1]
        hi = m.h[fill_bar : end + 1]
        sl_hit = lo <= stop
        tp_hit = hi > target
        tp_hit[0] = False  # the take-profit does not exist in the fill bar
        first_sl = int(np.argmax(sl_hit)) if sl_hit.any() else None
        first_tp = int(np.argmax(tp_hit)) if tp_hit.any() else None
        qinc = self._qinc[m.product_id]
        if first_sl is not None and (first_tp is None or first_sl <= first_tp):  # same bar: stop
            j = fill_bar + first_sl
            px = sl_exit_px(stop, float(m.o[j]), spread_bps, p.slippage_buffer_bps, p.stop_extra_slip_bps)
            return BracketOutcome("SL", j, _q(px, qinc, ROUND_DOWN))
        if first_tp is not None:
            return BracketOutcome("TP", fill_bar + first_tp, tp)
        if vertical <= n - 1:
            px = market_exit_px(float(m.c[vertical]), spread_bps, p.slippage_buffer_bps)
            return BracketOutcome("HORIZON", vertical, _q(px, qinc, ROUND_DOWN))
        return None

    def model_sell(self, m: MarketBars, decision_bar: int, spread_bps: float) -> BracketOutcome | None:
        """Market sell decided at ``decision_bar``: executes at ``open[t'+F]``."""
        bar = decision_bar + self.F
        if bar > len(m.c) - 1:
            return None
        px = market_exit_px(float(m.o[bar]), spread_bps, self.fill.params.slippage_buffer_bps)
        return BracketOutcome("SELL", bar, _q(px, self._qinc[m.product_id], ROUND_DOWN))

    # ------------------------------------------------------------------------------------- fees

    def fee_quote(self, price: Decimal, size: Decimal, liquidity: Literal["maker", "taker"]) -> Decimal:
        bps = Decimal(str(self.fees.maker_bps if liquidity == "maker" else self.fees.taker_bps))
        return price * size * bps / Decimal(10_000)
