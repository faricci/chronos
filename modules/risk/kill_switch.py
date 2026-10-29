"""Kill switch: the guards of spec sez. 4.1.4, split by scope (sez. 0.6).

* ``AccountGuards`` - one per book: daily loss, loss streak, exposure, coverage breach. A tripped book does not
  block the others.
* ``SystemGuards`` - one shared instance: stale bars, stale tickers, decider health, drift. When they trip, the new
  entries of every book are blocked.
* ``KillSwitch`` - combines them. ``entry_blocked(book)`` is the ``EntryGate`` of the ``RiskManager`` and
  ``coverage_breach_active(book)`` is what the ``BalanceGuard`` reads (``KillSwitchView``).

The API is synchronous and identical in live, dry and backtest. Time never comes from the wall clock inside the
logic: observations carry their own ``now`` and the blocking questions read the injected ``clock``, which the
backtest drives with simulated time.

Guards act on NEW ENTRIES only. Nothing here can block an exit (a spot sale of owned base cannot create debt, and
the watchdog must always be able to sell): ``KillSwitch.exit_allowed`` is unconditionally ``True`` and the
``BalanceGuard`` consults ``coverage_breach_active`` for BUYs only.

State lives in memory (the synchronous source of truth). ``KillSwitch.save`` / ``load`` copy it to / from the table
``kill_switch_state(book, guard, tripped_at, until, reason)`` of the ledger database; the caller decides when to
save. Global guards use ``book = '*'``. Rows written, per book: ``coverage_breach`` (until NULL = manual reset),
``daily_loss`` and ``loss_streak`` (until = expiry), plus the bookkeeping rows ``daily_open`` (equity of the first
observation of the UTC day, in ``reason``) and ``loss_streak_count``; globally ``decider_health`` and ``drift``.
Stale data and exposure are recomputed from the next observation and are never persisted.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Final, Literal

import aiosqlite
import structlog

from modules.config import KillSwitchConfig, RiskConfig
from modules.execution.balance import Clock

__all__ = [
    "GLOBAL_BOOK",
    "AccountGuards",
    "ExitReason",
    "KillSwitch",
    "StateRow",
    "SystemGuards",
]

log = structlog.get_logger(__name__)

ExitReason = Literal["TP", "SL", "SELL", "HORIZON", "WATCHDOG"]

GLOBAL_BOOK: Final = "*"
# Exits that count as a "stop" for the loss streak (sez. 4.1.4); any other exit resets the count.
_LOSING_EXITS: Final = frozenset({"SL", "WATCHDOG"})

_TABLE: Final = "kill_switch_state"
_BEGIN_RETRIES: Final = 100


# ------------------------------------------------------------------------------------------- helpers


def _utc(t: datetime) -> datetime:
    if t.tzinfo is None:
        raise ValueError("kill switch times must be timezone-aware (UTC)")
    return t.astimezone(UTC)


def _next_midnight(t: datetime) -> datetime:
    return datetime.combine(t.date() + timedelta(days=1), time(0), tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Trip:
    """A latched guard. ``until=None`` means: until a manual reset."""

    tripped_at: datetime | None
    until: datetime | None
    reason: str

    def active(self, now: datetime) -> bool:
        return self.until is None or now < self.until


@dataclass(frozen=True, slots=True)
class StateRow:
    """One row of ``kill_switch_state`` without its ``book`` (the owner adds it)."""

    guard: str
    tripped_at: datetime | None
    until: datetime | None
    reason: str


# -------------------------------------------------------------------------------------- account guards


class AccountGuards:
    """The guards of one book: daily loss, loss streak, exposure, coverage breach."""

    def __init__(self, book: str, cfg: KillSwitchConfig, risk: RiskConfig, clock: Clock) -> None:
        self.book = book
        self._clock = clock
        self._max_daily_loss = Decimal(str(cfg.max_daily_loss_frac))
        self._max_losses = cfg.max_consecutive_losses
        self._pause = timedelta(hours=cfg.loss_streak_pause_h)
        self._max_exposure = Decimal(str(risk.max_total_exposure_frac))
        self._day: date | None = None
        self._day_open: Decimal | None = None
        self._day_open_at: datetime | None = None
        self._daily_loss: Trip | None = None
        self._streak = 0
        self._streak_trip: Trip | None = None
        self._over_exposed = False
        self._breach: Trip | None = None

    # ----------------------------------------------------------------------------------- observations

    def observe_equity(self, now: datetime, equity: Decimal, exposure_quote: Decimal) -> None:
        """Feed the mark-to-market equity of the book and its invested value (base x mark, in quote).

        The first observation of a UTC day fixes the day's opening equity. ``equity - opening <= -max_daily_loss_frac
        x opening`` latches the daily-loss guard until 00:00 UTC. Exposure is not latched: it blocks while the LAST
        observation is above ``max_total_exposure_frac``.
        """
        now = _utc(now)
        day = now.date()
        if self._day is not None and day < self._day:
            return  # an observation from a past day carries no information about today
        if self._day is None or day > self._day:
            self._day, self._day_open, self._day_open_at = day, equity, now
        assert self._day_open is not None
        opening = self._day_open
        if (self._daily_loss is None or not self._daily_loss.active(now)) and (
            equity - opening <= -self._max_daily_loss * opening
        ):
            self._daily_loss = Trip(now, _next_midnight(now), f"equity {equity} vs opening {opening}")
            log.warning("kill_switch_daily_loss", book=self.book, equity=str(equity), opening=str(opening))
        self._over_exposed = equity <= 0 or exposure_quote > self._max_exposure * equity

    def observe_trade_closed(self, now: datetime, pnl_quote: Decimal, exit_reason: ExitReason) -> None:
        """A position of the book closed. ``max_consecutive_losses`` consecutive closes by SL or WATCHDOG pause the
        book for ``loss_streak_pause_h``; any other kind of close resets the count."""
        now = _utc(now)
        if exit_reason not in _LOSING_EXITS:
            self._streak = 0
            return
        self._streak += 1
        if self._streak < self._max_losses:
            return
        until = now + self._pause
        if self._streak_trip is not None and self._streak_trip.active(now):
            until = max(until, self._streak_trip.until or until)
        self._streak_trip = Trip(now, until, f"{self._streak} consecutive stops, last pnl {pnl_quote}")
        self._streak = 0  # after the pause a full new streak is needed
        log.warning("kill_switch_loss_streak", book=self.book, until=until.isoformat())

    def trip_coverage_breach(self, reason: str) -> None:
        """Block the BUYs of this book until ``reset_coverage_breach`` (sez. 4.1.6, rule 7). Idempotent."""
        if self._breach is None:
            self._breach = Trip(_utc(self._clock()), None, reason)
            log.error("kill_switch_coverage_breach", book=self.book, reason=reason)

    def reset_coverage_breach(self) -> None:
        """Manual reset, after a human has checked the account."""
        if self._breach is not None:
            log.warning("kill_switch_coverage_breach_reset", book=self.book)
        self._breach = None

    # --------------------------------------------------------------------------------------- queries

    def coverage_breach_active(self) -> bool:
        return self._breach is not None

    def blocked_reason(self, now: datetime | None = None) -> str | None:
        """Why new entries of this book are blocked, or ``None``. Exits are never blocked."""
        now = _utc(self._clock() if now is None else now)
        if self._breach is not None:
            return "coverage_breach"
        if self._daily_loss is not None and self._daily_loss.active(now):
            return "daily_loss"
        if self._streak_trip is not None and self._streak_trip.active(now):
            return "loss_streak"
        if self._over_exposed:
            return "exposure"
        return None

    # ------------------------------------------------------------------------------------ persistence

    def state_rows(self, now: datetime | None = None) -> list[StateRow]:
        now = _utc(self._clock() if now is None else now)
        rows: list[StateRow] = []
        for guard, trip in (
            ("coverage_breach", self._breach),
            ("daily_loss", self._daily_loss),
            ("loss_streak", self._streak_trip),
        ):
            if trip is not None and trip.active(now):
                rows.append(StateRow(guard, trip.tripped_at, trip.until, trip.reason))
        if self._day_open is not None:
            rows.append(StateRow("daily_open", self._day_open_at, None, str(self._day_open)))
        if self._streak > 0:
            rows.append(StateRow("loss_streak_count", now, None, str(self._streak)))
        return rows

    def restore(self, rows: Iterable[StateRow], now: datetime | None = None) -> None:
        """Replace the in-memory state with ``rows``. Expired trips are dropped; unknown guards are ignored."""
        now = _utc(self._clock() if now is None else now)
        self._daily_loss = self._streak_trip = self._breach = None
        self._day = self._day_open = self._day_open_at = None
        self._streak = 0
        self._over_exposed = False
        for row in rows:
            at = _utc(row.tripped_at or now)
            trip = Trip(at, None if row.until is None else _utc(row.until), row.reason)
            if row.guard == "coverage_breach":
                self._breach = trip
            elif row.guard == "daily_loss" and trip.active(now):
                self._daily_loss = trip
            elif row.guard == "loss_streak" and trip.active(now):
                self._streak_trip = trip
            elif row.guard == "daily_open":
                self._day, self._day_open, self._day_open_at = at.date(), Decimal(row.reason), at
            elif row.guard == "loss_streak_count":
                self._streak = int(row.reason)
            else:
                log.warning("kill_switch_unknown_guard_row", book=self.book, guard=row.guard)


# -------------------------------------------------------------------------------------- system guards


class SystemGuards:
    """The guards shared by every book: stale bars, stale tickers, decider health, drift."""

    def __init__(self, cfg: KillSwitchConfig, clock: Clock | None = None) -> None:
        # ``clock`` only stamps ``tripped_at`` of the flags set by the setters; no decision depends on it.
        self._clock = clock
        self._stale_bar = timedelta(seconds=cfg.stale_bar_s)
        self._stale_data = timedelta(seconds=cfg.stale_data_s)
        self._bar_close: dict[str, datetime] = {}
        self._bar_lag: dict[str, timedelta] = {}
        self._ticker_at: dict[str, datetime] = {}
        self._decider_trip: Trip | None = None
        self._drift_trip: Trip | None = None

    def observe_bar_published(self, product: str, ts_bar_close: datetime, published_at: datetime) -> None:
        """A 1m bar was published (at ``ts_bar_close + S`` in normal operation). The staleness test uses the age of
        the newest bar's ``ts_bar_close``; the publication lag is only kept for diagnostics (``bar_lag_s``)."""
        close, published = _utc(ts_bar_close), _utc(published_at)
        known = self._bar_close.get(product)
        if known is None or close > known:
            self._bar_close[product] = close
            self._bar_lag[product] = published - close

    def observe_ticker(self, product: str, at: datetime) -> None:
        at = _utc(at)
        known = self._ticker_at.get(product)
        if known is None or at > known:
            self._ticker_at[product] = at

    def bar_lag_s(self, product: str) -> float | None:
        lag = self._bar_lag.get(product)
        return None if lag is None else lag.total_seconds()

    def _stamp(self, at: datetime | None) -> datetime | None:
        if at is not None:
            return _utc(at)
        return None if self._clock is None else _utc(self._clock())

    def set_decider_health(self, ok: bool, *, reason: str = "", at: datetime | None = None) -> None:
        """``ok=False``: the decider is unhealthy (too many fallbacks, incoherent answers): block every entry."""
        self._decider_trip = None if ok else Trip(self._stamp(at), None, reason)

    def set_drift(self, flag: bool, *, reason: str = "", at: datetime | None = None) -> None:
        """``flag=True``: PSI above threshold on the main features (HOLD-only until cleared)."""
        self._drift_trip = Trip(self._stamp(at), None, reason) if flag else None

    def blocked_reason(self, now: datetime) -> str | None:
        """Why the new entries of ALL books are blocked, or ``None``. A product never observed is not judged."""
        now = _utc(now)
        for product, close in sorted(self._bar_close.items()):
            if now - close > self._stale_bar:
                return f"stale_bar:{product}"
        for product, at in sorted(self._ticker_at.items()):
            if now - at > self._stale_data:
                return f"stale_data:{product}"
        if self._decider_trip is not None:
            return "decider_health"
        if self._drift_trip is not None:
            return "drift"
        return None

    def state_rows(self) -> list[StateRow]:
        rows: list[StateRow] = []
        for guard, trip in (("decider_health", self._decider_trip), ("drift", self._drift_trip)):
            if trip is not None:
                rows.append(StateRow(guard, trip.tripped_at, None, trip.reason))
        return rows

    def restore(self, rows: Iterable[StateRow]) -> None:
        self._decider_trip = self._drift_trip = None
        for row in rows:
            trip = Trip(row.tripped_at, None, row.reason)
            if row.guard == "decider_health":
                self._decider_trip = trip
            elif row.guard == "drift":
                self._drift_trip = trip
            else:
                log.warning("kill_switch_unknown_guard_row", book=GLOBAL_BOOK, guard=row.guard)


# ------------------------------------------------------------------------------------------ the switch


class KillSwitch:
    """System guards first, then the guards of the book. One instance per database (mode)."""

    def __init__(self, system: SystemGuards, accounts: Mapping[str, AccountGuards], clock: Clock) -> None:
        self._system = system
        self._accounts = dict(accounts)
        self._clock = clock

    def entry_blocked(self, book: str) -> str | None:
        """Reason why a NEW ENTRY of ``book`` must not be planned, or ``None`` (the ``EntryGate`` of the RiskManager).
        An unknown book is blocked: fail closed."""
        now = _utc(self._clock())
        reason = self._system.blocked_reason(now)
        if reason is not None:
            return reason
        account = self._accounts.get(book)
        if account is None:
            return f"unknown_book:{book}"
        return account.blocked_reason(now)

    def coverage_breach_active(self, book: str) -> bool:
        """What the ``BalanceGuard`` reads before a BUY. An unknown book counts as breached: fail closed."""
        account = self._accounts.get(book)
        return True if account is None else account.coverage_breach_active()

    def exit_allowed(self, book: str) -> bool:
        """Exits are never blocked by a guard (sez. 4.1.4): selling owned spot base cannot create debt."""
        return True

    # ------------------------------------------------------------------------------------ persistence

    def _owned_books(self) -> list[str]:
        return [*self._accounts, GLOBAL_BOOK]

    async def save(self, db: aiosqlite.Connection) -> None:
        """Write the current state, replacing every row of the books this switch owns (one transaction)."""
        now = _utc(self._clock())
        rows: list[tuple[str, str, str | None, str | None, str]] = []
        for book, account in self._accounts.items():
            rows += [_to_tuple(book, r) for r in account.state_rows(now)]
        rows += [_to_tuple(GLOBAL_BOOK, r) for r in self._system.state_rows()]
        owned = self._owned_books()
        marks = ",".join("?" * len(owned))
        await _begin(db)
        try:
            await db.execute(f"DELETE FROM {_TABLE} WHERE book IN ({marks})", owned)
            await db.executemany(
                f"INSERT INTO {_TABLE} (book, guard, tripped_at, until, reason) VALUES (?,?,?,?,?)", rows
            )
            await db.execute("COMMIT")
        except BaseException:
            await db.execute("ROLLBACK")
            raise

    async def load(self, db: aiosqlite.Connection) -> None:
        """Replace the in-memory state of the owned books with what the database holds (call at startup)."""
        now = _utc(self._clock())
        owned = self._owned_books()
        marks = ",".join("?" * len(owned))
        async with db.execute(
            f"SELECT book, guard, tripped_at, until, reason FROM {_TABLE} WHERE book IN ({marks})", owned
        ) as cur:
            stored = await cur.fetchall()
        by_book: dict[str, list[StateRow]] = {book: [] for book in owned}
        for book, guard, tripped_at, until, reason in stored:
            by_book[book].append(
                StateRow(
                    guard,
                    None if tripped_at is None else datetime.fromisoformat(tripped_at),
                    None if until is None else datetime.fromisoformat(until),
                    reason or "",
                )
            )
        for book, account in self._accounts.items():
            account.restore(by_book[book], now)
        self._system.restore(by_book[GLOBAL_BOOK])


def _to_tuple(book: str, r: StateRow) -> tuple[str, str, str | None, str | None, str]:
    return (
        book,
        r.guard,
        None if r.tripped_at is None else r.tripped_at.isoformat(),
        None if r.until is None else r.until.isoformat(),
        r.reason,
    )


async def _begin(db: aiosqlite.Connection) -> None:
    """``BEGIN IMMEDIATE``, waiting briefly if the ledger is inside its own transaction on the same connection."""
    for attempt in range(_BEGIN_RETRIES):
        try:
            await db.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError as e:
            if "within a transaction" not in str(e) or attempt == _BEGIN_RETRIES - 1:
                raise
            await asyncio.sleep(0.01)
