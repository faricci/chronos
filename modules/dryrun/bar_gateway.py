"""Execution gateway of the backtest (decision O2.4): the life cycle of the live orders on top of a bar-level venue.

Every order goes through ``BalanceGuard.authorize`` (constraint C11): entry, standalone bracket and market sell. The
guard, the ``VirtualAccount`` (a ``BalanceBook`` like the live one) and the ``PositionLedger`` are the same classes the
live gateway uses; only the venue is simulated (``SimulatedExchange``). The gateway is time-agnostic: the runner calls
``submit_entry`` when the decision is taken and ``entry_terminal`` / ``close`` when the simulated clock reaches the bar
of the event, so that fills and exits of one product are visible to the decisions of the others in chronological order.

A refusal of the guard on an ENTRY is a normal skip. A refusal, or a coverage alarm, on an exit leg is a bug of the
simulation or of the guard and raises ``BacktestInvariantError``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

import numpy as np
import pandas as pd

from modules.dryrun.sim_exchange import BracketOutcome, EntryOutcome, MarketBars, SimulatedExchange
from modules.dryrun.virtual_account import VirtualAccount
from modules.execution.balance_guard import BalanceGuard, CoverageRefused
from modules.execution.ids import client_order_id
from modules.execution.orders import OrderSpec
from modules.execution.rounding import floor_to
from modules.execution.spot import SpotRegistry
from modules.ledger.positions import EntryPlan, FillRecord, OrderRecord, PositionLedger
from modules.risk.risk_manager import OrderIntent

__all__ = [
    "BacktestInvariantError",
    "BarExecutionGateway",
    "ClosedTrade",
    "EntryRefused",
    "OpenTrade",
    "PendingEntry",
]

ExitReason = Literal["TP", "SL", "SELL", "HORIZON"]


class BacktestInvariantError(Exception):
    """The simulation broke a coverage invariant: a bug, never a result."""


@dataclass(frozen=True)
class EntryRefused:
    reason: str  # insufficient_balance | balance_stale | kill_switch
    detail: str


@dataclass
class PendingEntry:
    intent: OrderIntent
    spec: OrderSpec
    outcome: EntryOutcome
    decision_bar: int
    spread_bps: float


@dataclass
class OpenTrade:
    intent: OrderIntent
    entry_cid: str
    bracket_cid: str
    fill_bar: int
    size: Decimal
    entry_px: Decimal
    entry_cost: Decimal  # quote spent on the entry, fee included
    bracket: BracketOutcome | None  # None: the data ends before the bracket resolves


@dataclass(frozen=True)
class ClosedTrade:
    intent: OrderIntent
    fill_bar: int
    exit_bar: int
    exit_reason: ExitReason
    entry_px: Decimal
    exit_px: Decimal
    size: Decimal
    fees: Decimal
    pnl_quote: Decimal
    net_ret_bps: float


class BarExecutionGateway:
    def __init__(
        self,
        book: str,
        guard: BalanceGuard,
        account: VirtualAccount,
        exchange: SimulatedExchange,
        ledger: PositionLedger,
        registry: SpotRegistry,
        clock: Callable[[], datetime],
    ) -> None:
        self._book = book
        self._guard = guard  # mandatory and typed: there is no gateway without the coverage circuit
        self._account = account
        self._exchange = exchange
        self._ledger = ledger
        self._registry = registry
        self._clock = clock

    # ---------------------------------------------------------------------------------- entry

    async def submit_entry(
        self, intent: OrderIntent, market: MarketBars, decision_bar: int, spread_bps: float
    ) -> PendingEntry | EntryRefused:
        """Authorize and reserve the BUY, then ask the venue when (and whether) it fills."""
        cid = client_order_id(intent.decision_id, "entry")
        maker = intent.entry.type == "limit_post_only"
        spec = OrderSpec(
            client_order_id=cid, book=self._book, product_id=intent.product_id, side="BUY", kind="limit",
            base_size=intent.base_size, limit_price=intent.entry.limit_price, post_only=maker,
        )  # fmt: skip
        try:
            granted = self._guard.authorize(spec)
        except CoverageRefused as e:
            return EntryRefused(e.reason, str(e))
        granted.consume()
        now = self._clock()
        await self._ledger.record_order(
            OrderRecord(
                cid, self._book, intent.product_id, "BUY", "entry", "OPEN", now, now, intent_id=intent.intent_id
            )
        )
        if maker:
            outcome = self._exchange.entry_maker(market, decision_bar, intent.entry.limit_price, intent.base_size)
        else:
            outcome = self._exchange.entry_taker(
                market, decision_bar, intent.entry.limit_price, spread_bps, intent.product_id
            )
        return PendingEntry(intent, spec, outcome, decision_bar, spread_bps)

    async def entry_terminal(self, pending: PendingEntry, market: MarketBars) -> OpenTrade | None:
        """The entry order reached its terminal state (fill or expiry). On a fill, protect the position."""
        intent, spec, outcome = pending.intent, pending.spec, pending.outcome
        cid = spec.client_order_id
        now = self._clock()
        meta = self._registry.meta(intent.product_id)
        if outcome.status == "EXPIRED" or outcome.price is None:
            self._account.balances.release(cid)
            await self._ledger.update_order(cid, "EXPIRED", now)
            return None
        size, price = intent.base_size, outcome.price
        liquidity: Literal["maker", "taker"] = "maker" if intent.entry.type == "limit_post_only" else "taker"
        fee = self._exchange.fee_quote(price, size, liquidity)
        self._account.record_fill(
            cid, product_id=intent.product_id, base_currency=meta.base_currency, price=price, size=size, fee=fee,
            trade_id=f"sim-{cid}-1",
        )  # fmt: skip
        self._account.balances.release(cid)  # unused remainder of the reservation goes back to free
        await self._ledger.record_fill(
            FillRecord(f"sim-{cid}-1", self._book, cid, intent.product_id, "BUY", price, size, fee, liquidity, now)
        )
        await self._ledger.update_order(cid, "FILLED", now)
        plan = EntryPlan(intent.bracket.take_profit, intent.bracket.stop_loss, intent.bracket.k_tp, intent.bracket.k_sl,
                         intent.bracket.atr)  # fmt: skip
        fill_ts = pd.Timestamp(
            market.ts_close[outcome.bar] - np.timedelta64(1, "m"), tz="UTC"
        ).to_pydatetime()  # bar start
        await self._ledger.apply_entry_fill(
            self._book, intent.product_id, size, price, intent.decision_id, fill_ts, plan
        )

        bracket_cid = client_order_id(intent.decision_id, "bracket")
        sellable = min(size, self._account.balances.free(meta.base_currency))
        bracket_size = floor_to(sellable, meta.base_increment)
        bspec = OrderSpec(
            client_order_id=bracket_cid, book=self._book, product_id=intent.product_id, side="SELL", kind="bracket",
            base_size=bracket_size, limit_price=intent.bracket.take_profit, stop_trigger_price=intent.bracket.stop_loss,
        )  # fmt: skip
        try:
            granted = self._guard.authorize(bspec)
        except CoverageRefused as e:
            raise BacktestInvariantError(f"bracket of {cid} refused by the guard: {e}") from e
        granted.consume()
        await self._ledger.record_order(
            OrderRecord(
                bracket_cid,
                self._book,
                intent.product_id,
                "SELL",
                "bracket",
                "OPEN",
                now,
                now,
                intent_id=intent.intent_id,
            )  # fmt: skip
        )
        await self._ledger.set_bracket(self._book, intent.product_id, bracket_cid, intent.bracket.take_profit,
                                       intent.bracket.stop_loss)  # fmt: skip
        outcome_br = self._exchange.bracket(
            market, outcome.bar, intent.bracket.take_profit, intent.bracket.stop_loss, pending.spread_bps
        )
        return OpenTrade(intent, cid, bracket_cid, outcome.bar, size, price, price * size + fee, outcome_br)

    # ------------------------------------------------------------------------------------ exit

    async def close(self, trade: OpenTrade, exit_: BracketOutcome, market: MarketBars) -> ClosedTrade:
        """Execute the exit of an open position. TP/SL fill the bracket itself; HORIZON and SELL cancel it first."""
        intent = trade.intent
        meta = self._registry.meta(intent.product_id)
        now = self._clock()
        size = trade.size
        if exit_.reason in ("TP", "SL"):
            order_cid = trade.bracket_cid
            liquidity: Literal["maker", "taker"] = "maker" if exit_.reason == "TP" else "taker"
        else:
            # cancel the bracket (the reservation returns to free only on the confirmation, simulated as immediate),
            # then sell at market against a fresh reservation
            self._account.balances.release(trade.bracket_cid)
            await self._ledger.update_order(trade.bracket_cid, "CANCELLED", now)
            order_cid = client_order_id(intent.decision_id, f"exit-{exit_.reason}")
            sellable = floor_to(min(size, self._account.balances.free(meta.base_currency)), meta.base_increment)
            if sellable <= 0:
                raise BacktestInvariantError(
                    f"nothing to sell for {intent.decision_id}: base not free after the cancel"
                )
            spec = OrderSpec(
                client_order_id=order_cid, book=self._book, product_id=intent.product_id, side="SELL", kind="market",
                base_size=sellable,
            )  # fmt: skip
            try:
                granted = self._guard.authorize(spec)
            except CoverageRefused as e:
                raise BacktestInvariantError(f"market exit of {intent.decision_id} refused by the guard: {e}") from e
            granted.consume()
            await self._ledger.record_order(
                OrderRecord(
                    order_cid,
                    self._book,
                    intent.product_id,
                    "SELL",
                    "exit",
                    "OPEN",
                    now,
                    now,
                    intent_id=intent.intent_id,
                )  # fmt: skip
            )
            liquidity = "taker"
        fee = self._exchange.fee_quote(exit_.price, size, liquidity)
        alarms = self._account.record_fill(
            order_cid, product_id=intent.product_id, base_currency=meta.base_currency, price=exit_.price,
            size=size, fee=fee, trade_id=f"sim-{order_cid}-1",
        )  # fmt: skip
        if alarms:
            raise BacktestInvariantError(f"coverage alarm on exit of {intent.decision_id}: {alarms}")
        self._account.balances.release(order_cid)
        await self._ledger.record_fill(
            FillRecord(
                f"sim-{order_cid}-1",
                self._book,
                order_cid,
                intent.product_id,
                "SELL",
                exit_.price,
                size,
                fee,
                liquidity,
                now,
            )  # fmt: skip
        )
        await self._ledger.update_order(order_cid, "FILLED", now)
        await self._ledger.apply_exit_fill(self._book, intent.product_id, size)
        proceeds = exit_.price * size - fee
        pnl = proceeds - trade.entry_cost
        fees = fee + (trade.entry_cost - trade.entry_px * size)
        net_bps = float(Decimal(10_000) * (proceeds / trade.entry_cost - 1))
        return ClosedTrade(
            intent, trade.fill_bar, exit_.bar, exit_.reason, trade.entry_px, exit_.price, size, fees, pnl, net_bps
        )
