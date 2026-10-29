"""Money and counts of the probe: ``ProbeBudget`` (how much may still be at risk), ``ProbeLimiter`` (how many
entries and orders), ``ProbeStreaks`` (series of stops and watchdog exits). All three read the ledger of the
probe, never a cache of their own, so a restart gives the same answers (plan sez. 1.1, 1.3).

Equity of the probe is notional: ``C + cash flow of the fills of the book + base held at the bid``, with ``C`` = 20 EUR.
The cash flow is taken from the fills (the money that really left and entered), not from ``closed_trades``, so a dust
that never closed a trade, or one merged into the next position, is counted exactly once. ``L_rem = L_tot + (equity
- C)`` is what may still be lost; an entry is allowed only if its ``required_quote`` fits in ``min(Q_max, L_rem -
dust_mark)``, so the loss of the whole position AND of the dust stays inside ``L_tot``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Final

import structlog

from modules.config import ExecProbeConfig
from modules.ledger.positions import ClosedTrade, PositionLedger
from modules.probe import limits

log = structlog.get_logger(__name__)

ZERO: Final = Decimal(0)
LIVE_ENTRY_PREFIX: Final = "probe:"  # decision ids of real entries; the stage orders use ``probe-test:``


def next_midnight_utc(t: datetime) -> datetime:
    return datetime.combine(t.astimezone(UTC).date() + timedelta(days=1), time(0), tzinfo=UTC)


# ------------------------------------------------------------------------------------------------ budget


@dataclass(frozen=True, slots=True)
class BudgetState:
    equity: Decimal  # notional equity of the probe
    cumulative_loss: Decimal  # max(C - equity, 0)
    l_rem: Decimal  # what may still be lost
    dust_mark: Decimal  # the dust held, at the bid
    exposure: Decimal  # the base held (position and dust), at the bid
    entry_cap: Decimal  # min(Q_max, L_rem - dust_mark): the largest required_quote of the next entry

    def exhausted(self, protectable_quote: Decimal) -> bool:
        """True when not even the smallest protectable entry (``N_br`` plus coverage fee) fits any more."""
        return self.entry_cap < protectable_quote


class ProbeBudget:
    def __init__(self, cfg: ExecProbeConfig, ledger: PositionLedger, *, book: str = limits.BOOK) -> None:
        self._cfg = cfg
        self._ledger = ledger
        self._book = book

    async def state(self, product_id: str, bid: Decimal) -> BudgetState:
        """The budget at the bid ``bid``. The cash flow is the sum of the fills: a BUY costs ``price*size + fee``, a
        SELL returns ``price*size - fee``."""
        cash = ZERO
        for fill in await self._ledger.book_fills(self._book):
            notional = fill.price * fill.size
            cash += -(notional + fill.fee) if fill.side == "BUY" else notional - fill.fee
        position = self._ledger.view(self._book, product_id)
        held = position.base_qty + position.dust_qty
        c = limits.CAPITAL_REF_QUOTE
        equity = c + cash + held * bid
        dust_mark = position.dust_qty * bid
        l_rem = self._cfg.max_total_loss_quote + (equity - c)
        return BudgetState(
            equity=equity,
            cumulative_loss=max(c - equity, ZERO),
            l_rem=l_rem,
            dust_mark=dust_mark,
            exposure=held * bid,
            entry_cap=min(self._cfg.max_entry_quote, l_rem - dust_mark),
        )


# ----------------------------------------------------------------------------------------------- limiter


@dataclass(frozen=True, slots=True)
class LimitCounts:
    entries_today: int
    entries_total: int
    orders_today: int


class ProbeLimiter:
    """Counts of entries and orders, from the ledger (``orders`` of the book). An entry counts as soon as its order was
    recorded, filled or not: a restart cannot give the probe more orders than it was allowed."""

    def __init__(self, cfg: ExecProbeConfig, ledger: PositionLedger, *, book: str = limits.BOOK) -> None:
        self._cfg = cfg
        self._ledger = ledger
        self._book = book

    async def counts(self, now: datetime) -> LimitCounts:
        day: date = now.astimezone(UTC).date()
        entries_today = entries_total = orders_today = 0
        for rec in await self._ledger.book_orders(self._book):
            same_day = rec.created_at.astimezone(UTC).date() == day
            if same_day:
                orders_today += 1
            if rec.leg == "entry" and (rec.intent_id or "").startswith(LIVE_ENTRY_PREFIX):
                entries_total += 1
                if same_day:
                    entries_today += 1
        return LimitCounts(entries_today, entries_total, orders_today)

    async def blocked(self, now: datetime) -> str | None:
        """Why NEW entries are blocked by the counts, or ``None``. The cap on orders never stops a bracket, an exit
        or a protective re-placement: it is consulted by the planner only."""
        c = await self.counts(now)
        if c.entries_total >= self._cfg.max_entries_total:
            return "max_entries_total"
        if c.entries_today >= self._cfg.max_entries_per_day:
            return "max_entries_per_day"
        if c.orders_today >= self._cfg.max_orders_per_day:
            return "max_orders_per_day"
        return None


# ----------------------------------------------------------------------------------------------- streaks


class ProbeStreaks:
    """Series counted SEPARATELY (plan sez. 1.3), unlike the single SL+WATCHDOG series of ``AccountGuards``:

    * ``sl_streak``: consecutive ``SL`` closes. A ``TP``, ``HORIZON``, ``SELL`` or ``MANUAL`` close resets it, a
      ``WATCHDOG`` does not touch it. At ``max_consecutive_sl`` the entries pause until 00:00 UTC of the next day and
      the count starts again; it is not the end of the probe.
    * ``watchdog_count``: ``WATCHDOG`` closes, whatever the cause. At ``max_watchdog_exits`` the probe is over.

    The state is a pure function of the closed trades in order: ``replay`` rebuilds it after a restart, ``observe_exit``
    is the live feed (the ledger's close listener).
    """

    def __init__(self, cfg: ExecProbeConfig) -> None:
        self._max_sl = cfg.max_consecutive_sl
        self._max_watchdog = cfg.max_watchdog_exits
        self.sl_streak = 0
        self.watchdog_count = 0
        self.pause_until: datetime | None = None

    def observe_exit(self, reason: str, at: datetime) -> None:
        if reason == "SL":
            self.sl_streak += 1
            if self.sl_streak >= self._max_sl:
                self.pause_until = next_midnight_utc(at)
                self.sl_streak = 0  # after the pause a whole new series is needed
                log.warning("probe_sl_pause", until=self.pause_until.isoformat())
        elif reason == "WATCHDOG":
            self.watchdog_count += 1
            log.error("probe_watchdog_exit_counted", count=self.watchdog_count)
        else:
            self.sl_streak = 0

    def replay(self, trades: Iterable[ClosedTrade]) -> None:
        self.sl_streak, self.watchdog_count, self.pause_until = 0, 0, None
        for t in sorted(trades, key=lambda x: x.exit_at):
            self.observe_exit(t.exit_reason, t.exit_at)

    @property
    def finished(self) -> bool:
        return self.watchdog_count >= self._max_watchdog

    def blocked_reason(self, now: datetime) -> str | None:
        if self.finished:
            return "finished:watchdog_exit"
        if self.pause_until is not None and now < self.pause_until:
            return "sl_streak_pause"
        return None
