"""Protections of the probe (plan sez. 1.4). They are BLOCKING: each intervention is an emergency exit, recorded as a
``WATCHDOG`` close, which is the end of the probe (``ProbeStreaks``) and a CONFLITTO for the operator.

* From the terminal state of the entry to the bracket ``OPEN``: an alarm beyond ``bracket_open_alarm_s``; without a
  bracket ``OPEN`` beyond ``bracket_open_max_s`` the position is sold at once. (A partial fill BEFORE the entry is
  terminal is covered only by the standard watchdog, for at most the entry's 30 minutes: plan sez. 11, punto 2.)
* The stop-loss: the bid at or under the SL for ``sl_fill_max_s`` with the position still open (the stop did not
  execute, or only partly), or the bid under ``SL * sl_floor_ratio`` (the stop-limit floor is lost): emergency exit.
* A stop-loss fill with ``MAKER`` liquidity: the stop stayed in the book instead of executing as a taker. CONFLITTO.
* The slippage of the stop is measured on every ``SL`` close: an alarm beyond ``sl_slip_alarm_bps``; beyond
  ``sl_slip_severe_bps`` the entries are stopped with the ``STOP`` file until the operator has looked.

The standard ``Watchdog`` (``SL - 1 x ATR1h`` for 10 s, or the price under the SL without a bracket) stays on and
unchanged. This monitor only asks the gateway for ``emergency_exit``: it builds, authorizes and sends no order.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Final, Protocol

import structlog

from modules.config import ExecProbeConfig
from modules.execution.balance import Clock, utc_now
from modules.execution.ids import client_order_id
from modules.ledger.positions import TERMINAL_STATUSES, ClosedTrade, FillRecord, OrderRecord, PositionView

log = structlog.get_logger(__name__)

_BPS: Final = Decimal(10_000)
DEFAULT_RETRY_S: Final = 5.0

EventSink = Callable[[str, dict[str, Any]], None]
BidSource = Callable[[], Awaitable[Decimal | None]]
FinishFn = Callable[[str, str], None]


class ExitGateway(Protocol):
    async def emergency_exit(self, product_id: str) -> object: ...


class ProtectionLedger(Protocol):
    def view(self, book: str, product_id: str) -> PositionView: ...
    async def get_order(self, client_order_id: str, book: str | None = None) -> OrderRecord | None: ...
    async def open_orders(self, book: str) -> list[OrderRecord]: ...
    async def book_fills(self, book: str) -> list[FillRecord]: ...


class ProtectionMonitor:
    def __init__(
        self,
        *,
        cfg: ExecProbeConfig,
        book: str,
        product_id: str,
        ledger: ProtectionLedger,
        gateway: ExitGateway,
        bid: BidSource,
        on_event: EventSink,
        finish: FinishFn,
        request_stop: Callable[[], object],
        clock: Clock = utc_now,
        retry_s: float = DEFAULT_RETRY_S,
    ) -> None:
        self._cfg = cfg
        self._book = book
        self._product = product_id
        self._ledger = ledger
        self._gateway = gateway
        self._bid = bid
        self._event = on_event
        self._finish = finish
        self._request_stop = request_stop
        self._clock = clock
        self._retry = timedelta(seconds=retry_s)
        self._key: str | None = None
        self._alarmed: set[str] = set()
        self._below_since: datetime | None = None
        self._retry_at: datetime | None = None
        self.exits_requested = 0

    def _reset(self) -> None:
        self._alarmed.clear()
        self._below_since = None
        self._retry_at = None

    # ----------------------------------------------------------------------------------------- one pass

    async def check(self) -> None:
        now = self._clock()
        pos = self._ledger.view(self._book, self._product)
        if not pos.is_long:
            self._key = None
            self._reset()
            return
        key = pos.entry_decision_id or self._product
        if key != self._key:
            self._key = key
            self._reset()
        await self._check_bracket(pos, now)
        await self._check_price(pos, now)

    async def run(self, interval_s: float = 1.0, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        while True:
            try:
                await self.check()
            except Exception as exc:  # a failed pass must not end the monitor: the next one looks again
                log.error("probe_protection_check_failed", error=f"{type(exc).__name__}: {exc}")
            await sleep(interval_s)

    # ------------------------------------------------------------------------------------- the bracket

    async def _open_bracket(self, pos: PositionView) -> OrderRecord | None:
        if pos.bracket_order_id is None:
            return None
        for rec in await self._ledger.open_orders(self._book):
            if rec.client_order_id == pos.bracket_order_id and rec.side == "SELL" and rec.status == "OPEN":
                return rec
        return None

    async def _check_bracket(self, pos: PositionView, now: datetime) -> None:
        entry = await self._ledger.get_order(client_order_id(pos.entry_decision_id or "", "entry"), self._book)
        if entry is None or entry.status not in TERMINAL_STATUSES:
            return  # the entry is still alive: its own sequence places the bracket when it ends
        terminal_at = entry.updated_at
        bracket = await self._open_bracket(pos)
        if bracket is not None:
            if "bracket_latency" not in self._alarmed:
                self._alarmed.add("bracket_latency")
                latency = (bracket.updated_at - terminal_at).total_seconds()
                self._event("bracket_open_latency", {"seconds": latency, "decision_id": pos.entry_decision_id})
                if latency > self._cfg.bracket_open_alarm_s:
                    self._event("bracket_open_alarm", {"seconds": latency})
            return
        waited = (now - terminal_at).total_seconds()
        if waited > self._cfg.bracket_open_alarm_s and "bracket_alarm" not in self._alarmed:
            self._alarmed.add("bracket_alarm")
            self._event("bracket_open_alarm", {"seconds_without_bracket": waited})
            log.error("probe_bracket_late", waited_s=waited)
        if waited > self._cfg.bracket_open_max_s:
            await self._exit("bracket_not_open", f"no bracket OPEN {waited:.0f} s after the entry ended")

    # --------------------------------------------------------------------------------------- the stop

    async def _check_price(self, pos: PositionView, now: datetime) -> None:
        sl = pos.sl
        if sl is None:
            return
        bid = await self._bid()
        if bid is None:
            return
        if bid < sl * self._cfg.sl_floor_ratio:
            await self._exit("sl_floor_breach", f"bid {bid} under SL {sl} x {self._cfg.sl_floor_ratio}")
            return
        if bid <= sl:
            if self._below_since is None:
                self._below_since = now
                self._event("sl_touched", {"bid": str(bid), "sl": str(sl)})
            if (now - self._below_since).total_seconds() >= self._cfg.sl_fill_max_s:
                await self._exit("sl_not_executed", f"bid at or under SL {sl} for {self._cfg.sl_fill_max_s} s")
        else:
            self._below_since = None

    async def _exit(self, reason: str, detail: str) -> None:
        now = self._clock()
        if self._retry_at is not None and now < self._retry_at:
            return
        self._retry_at = now + self._retry
        self.exits_requested += 1
        log.error("probe_protection_exit", reason=reason, detail=detail)
        self._event("protection_exit", {"reason": reason, "detail": detail})
        self._finish("watchdog_exit", f"{reason}: {detail}")
        try:
            result = await self._gateway.emergency_exit(self._product)
            self._event("protection_exit_done", {"reason": reason, "result": repr(result)})
        except Exception as exc:  # the position is still open: the next pass retries after the back-off
            log.error("probe_protection_exit_failed", reason=reason, error=f"{type(exc).__name__}: {exc}")
            self._event("protection_exit_failed", {"reason": reason, "error": f"{type(exc).__name__}: {exc}"})

    # ------------------------------------------------------------------------------ after a close

    async def on_closed(self, trade: ClosedTrade) -> None:
        """Called for every closed trade of the book (the runner chains it to the ledger's close listener)."""
        self._event(
            "trade_closed",
            {"reason": trade.exit_reason, "net_quote": str(trade.net_quote), "held_min": trade.held_min,
             "entry_avg": str(trade.entry_avg_price), "exit_avg": str(trade.exit_avg_price)},
        )  # fmt: skip
        if trade.exit_reason != "SL" or trade.sl is None:
            return
        slip_bps = float((trade.sl - trade.exit_avg_price) / trade.sl * _BPS)
        self._event("sl_slippage", {"bps": slip_bps, "sl": str(trade.sl), "exit_avg": str(trade.exit_avg_price)})
        if slip_bps > self._cfg.sl_slip_severe_bps:
            self._event("sl_slippage_severe", {"bps": slip_bps})
            self._request_stop()  # the operator reviews before the next slot; the STOP file is removed by hand
        elif slip_bps > self._cfg.sl_slip_alarm_bps:
            self._event("sl_slippage_alarm", {"bps": slip_bps})
        for fill in await self._ledger.book_fills(self._book):
            if fill.side != "SELL" or not (trade.entry_at <= fill.trade_time <= trade.exit_at + timedelta(seconds=1)):
                continue
            order = await self._ledger.get_order(fill.client_order_id, self._book)
            if order is not None and order.leg == "bracket" and fill.liquidity.upper().startswith("MAKER"):
                self._event("sl_fill_maker", {"trade_id": fill.trade_id})
                self._finish("sl_fill_maker", "a stop-loss fill had MAKER liquidity: the stop stayed in the book")
                return
