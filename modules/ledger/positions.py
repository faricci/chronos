"""Positions, orders and fills of one trading mode, in SQLite (spec sez. 4.2, 4.3 punto 11).

One database per mode (``state/chronos_live.db`` / ``state/chronos_dry.db``): dry and live data never mix, and a
``meta`` row refuses to open a database with the wrong mode. Every table has a ``book`` column (sez. 0.6).
Prices and quantities are stored as ``TEXT`` (Decimal strings): no float ever touches money.

All mutations run in one transaction; the in-memory cache is updated only after the commit, so
``view`` (synchronous, no I/O) always equals what a fresh load of the database would give. The
``BalanceGuard`` reads positions through ``book_view`` on the order path, which therefore never waits on SQLite.

Long-only (constraint C1): a position is FLAT or LONG, an exit larger than the position raises ``ShortPositionError``.
Dust: a fill below the minimum sellable size keeps the book FLAT but remembers ``dust_qty`` / ``dust_cost``; it is
merged into the next entry on the same product (sez. 4.1.6).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final, Literal

import aiosqlite
import structlog

log = structlog.get_logger(__name__)

Mode = Literal["live", "dry"]
Side = Literal["FLAT", "LONG"]
ZERO: Final = Decimal(0)

SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS positions (
  book TEXT NOT NULL, product_id TEXT NOT NULL, side TEXT NOT NULL CHECK (side IN ('FLAT','LONG')),
  base_qty TEXT NOT NULL, avg_entry TEXT, opened_at TEXT, entry_decision_id TEXT,
  bracket_order_id TEXT, tp TEXT, sl TEXT, k_tp REAL, k_sl REAL, atr_at_entry TEXT,
  dust_qty TEXT NOT NULL DEFAULT '0', dust_cost TEXT NOT NULL DEFAULT '0',
  PRIMARY KEY (book, product_id)
);
CREATE TABLE IF NOT EXISTS orders (
  client_order_id TEXT PRIMARY KEY, book TEXT NOT NULL, exchange_order_id TEXT, intent_id TEXT, leg TEXT,
  product_id TEXT, side TEXT, status TEXT, payload TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS fills (
  trade_id TEXT PRIMARY KEY, book TEXT NOT NULL, client_order_id TEXT, product_id TEXT, side TEXT,
  price TEXT, size TEXT, fee TEXT, liquidity TEXT, trade_time TEXT
);
CREATE TABLE IF NOT EXISTS kill_switch_state (
  book TEXT NOT NULL, guard TEXT NOT NULL, tripped_at TEXT, until TEXT, reason TEXT,
  PRIMARY KEY (book, guard)
);
"""

TERMINAL_STATUSES: Final = frozenset({"FILLED", "CANCELLED", "EXPIRED", "REJECTED", "FAILED"})


class LedgerModeMismatch(Exception):
    """The database belongs to the other mode: dry and live data must never mix (C9)."""


class ShortPositionError(Exception):
    """An exit larger than the position: selling what we do not own is forbidden (C1, C11)."""


# -------------------------------------------------------------------------------------- records


@dataclass(frozen=True, slots=True)
class PositionView:
    book: str
    product_id: str
    side: Side = "FLAT"
    base_qty: Decimal = ZERO
    avg_entry: Decimal | None = None
    opened_at: datetime | None = None
    entry_decision_id: str | None = None
    bracket_order_id: str | None = None
    tp: Decimal | None = None
    sl: Decimal | None = None
    k_tp: float | None = None
    k_sl: float | None = None
    atr_at_entry: Decimal | None = None
    dust_qty: Decimal = ZERO
    dust_cost: Decimal = ZERO

    @property
    def is_long(self) -> bool:
        return self.side == "LONG"

    def hours_held(self, now: datetime) -> float:
        if self.opened_at is None:
            return 0.0
        return max((now - self.opened_at).total_seconds() / 3600.0, 0.0)


@dataclass(frozen=True, slots=True)
class EntryPlan:
    """Bracket parameters decided with the entry, stored with the position at its first fill."""

    tp: Decimal
    sl: Decimal
    k_tp: float
    k_sl: float
    atr: Decimal


@dataclass(frozen=True, slots=True)
class EntryResult:
    position: PositionView
    dust_merged: bool


@dataclass(frozen=True, slots=True)
class FillRecord:
    trade_id: str
    book: str
    client_order_id: str
    product_id: str
    side: Literal["BUY", "SELL"]
    price: Decimal
    size: Decimal
    fee: Decimal
    liquidity: str
    trade_time: datetime


@dataclass(frozen=True, slots=True)
class OrderRecord:
    client_order_id: str
    book: str
    product_id: str
    side: Literal["BUY", "SELL"]
    leg: str
    status: str
    created_at: datetime
    updated_at: datetime
    exchange_order_id: str | None = None
    intent_id: str | None = None
    payload: str | None = None


class BookPositions:
    """Read-only, synchronous view of one book: the ``PositionCache`` the ``BalanceGuard`` needs."""

    def __init__(self, ledger: PositionLedger, book: str) -> None:
        self._ledger = ledger
        self._book = book

    def view(self, product_id: str) -> PositionView:
        return self._ledger.view(self._book, product_id)


def _dec(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _ts(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _row_to_view(r: aiosqlite.Row) -> PositionView:
    return PositionView(
        book=r["book"],
        product_id=r["product_id"],
        side=r["side"],
        base_qty=Decimal(r["base_qty"]),
        avg_entry=_dec(r["avg_entry"]),
        opened_at=_ts(r["opened_at"]),
        entry_decision_id=r["entry_decision_id"],
        bracket_order_id=r["bracket_order_id"],
        tp=_dec(r["tp"]),
        sl=_dec(r["sl"]),
        k_tp=r["k_tp"],
        k_sl=r["k_sl"],
        atr_at_entry=_dec(r["atr_at_entry"]),
        dust_qty=Decimal(r["dust_qty"]),
        dust_cost=Decimal(r["dust_cost"]),
    )


def _s(value: object) -> str | None:
    return None if value is None else str(value)


# ----------------------------------------------------------------------------------------- ledger


class PositionLedger:
    def __init__(self, db: aiosqlite.Connection, mode: Mode) -> None:
        self._db = db
        self.mode: Mode = mode
        self._cache: dict[tuple[str, str], PositionView] = {}
        self._lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: str | Path, mode: Mode) -> PositionLedger:
        """Open (and create) the database of ``mode``. Raises ``LedgerModeMismatch`` on the wrong one."""
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(path, isolation_level=None)  # transactions are explicit
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=FULL")  # a position record must survive a power cut
        await db.executescript(SCHEMA)
        async with db.execute("SELECT value FROM meta WHERE key='mode'") as cur:
            row = await cur.fetchone()
        if row is None:
            await db.execute("INSERT INTO meta (key, value) VALUES ('mode', ?)", (mode,))
        elif row["value"] != mode:
            await db.close()
            raise LedgerModeMismatch(f"{path} is a {row['value']!r} database, refusing to open it as {mode!r}")
        ledger = cls(db, mode)
        await ledger._load_cache()
        return ledger

    async def close(self) -> None:
        await self._db.close()

    @property
    def connection(self) -> aiosqlite.Connection:
        """For the KillSwitch, which persists its state in this same database (sez. 0.6)."""
        return self._db

    async def _load_cache(self) -> None:
        self._cache.clear()
        async with self._db.execute("SELECT * FROM positions") as cur:
            for r in await cur.fetchall():
                v = _row_to_view(r)
                self._cache[(v.book, v.product_id)] = v

    # ----------------------------------------------------------------------------- reads (sync)

    def view(self, book: str, product_id: str) -> PositionView:
        return self._cache.get((book, product_id)) or PositionView(book, product_id)

    def book_view(self, book: str) -> BookPositions:
        return BookPositions(self, book)

    def long_positions(self, book: str) -> list[PositionView]:
        return [v for (b, _), v in self._cache.items() if b == book and v.is_long]

    # --------------------------------------------------------------------------- transactions

    async def _write_position(self, v: PositionView) -> None:
        await self._db.execute(
            "INSERT INTO positions (book, product_id, side, base_qty, avg_entry, opened_at, entry_decision_id,"
            " bracket_order_id, tp, sl, k_tp, k_sl, atr_at_entry, dust_qty, dust_cost)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(book, product_id) DO UPDATE SET side=excluded.side, base_qty=excluded.base_qty,"
            " avg_entry=excluded.avg_entry, opened_at=excluded.opened_at,"
            " entry_decision_id=excluded.entry_decision_id, bracket_order_id=excluded.bracket_order_id,"
            " tp=excluded.tp, sl=excluded.sl, k_tp=excluded.k_tp, k_sl=excluded.k_sl,"
            " atr_at_entry=excluded.atr_at_entry, dust_qty=excluded.dust_qty, dust_cost=excluded.dust_cost",
            (
                v.book, v.product_id, v.side, str(v.base_qty), _s(v.avg_entry),
                None if v.opened_at is None else v.opened_at.isoformat(), v.entry_decision_id,
                v.bracket_order_id, _s(v.tp), _s(v.sl), v.k_tp, v.k_sl, _s(v.atr_at_entry),
                str(v.dust_qty), str(v.dust_cost),
            ),
        )  # fmt: skip

    async def _mutate_position(self, book: str, product_id: str, change: object) -> PositionView:
        """Run ``change(old_view) -> new_view`` and persist it atomically; the cache follows only after commit."""
        assert callable(change)
        async with self._lock:
            old = self.view(book, product_id)
            new: PositionView = change(old)
            await self._db.execute("BEGIN IMMEDIATE")
            try:
                await self._write_position(new)
                await self._db.execute("COMMIT")
            except BaseException:
                await self._db.execute("ROLLBACK")
                raise
            self._cache[(book, product_id)] = new
            return new

    async def apply_entry_fill(
        self,
        book: str,
        product_id: str,
        qty: Decimal,
        price: Decimal,
        decision_id: str,
        ts: datetime,
        plan: EntryPlan | None = None,
    ) -> EntryResult:
        """Add a BUY fill to the long position. The first fill opens it (and merges any dust); later fills
        average the entry price. ``bracket_order_id`` stays untouched until ``set_bracket``."""
        if qty <= 0 or price <= 0:
            raise ValueError("an entry fill needs a positive quantity and price")
        merged = False

        def change(old: PositionView) -> PositionView:
            nonlocal merged
            if old.is_long:
                total = old.base_qty + qty
                avg = ((old.avg_entry or price) * old.base_qty + price * qty) / total
                return replace(old, base_qty=total, avg_entry=avg)
            merged = old.dust_qty > 0
            total = qty + old.dust_qty
            avg = (price * qty + old.dust_cost) / total  # dust_cost is the quote spent, fees included
            return PositionView(
                book=book, product_id=product_id, side="LONG", base_qty=total, avg_entry=avg, opened_at=ts,
                entry_decision_id=decision_id,
                tp=None if plan is None else plan.tp, sl=None if plan is None else plan.sl,
                k_tp=None if plan is None else plan.k_tp, k_sl=None if plan is None else plan.k_sl,
                atr_at_entry=None if plan is None else plan.atr,
            )  # fmt: skip

        position = await self._mutate_position(book, product_id, change)
        return EntryResult(position, merged)

    async def set_bracket(
        self, book: str, product_id: str, bracket_order_id: str | None, tp: Decimal | None, sl: Decimal | None
    ) -> PositionView:
        """Attach (or clear, with ``None``) the standalone bracket of a long position."""

        def change(old: PositionView) -> PositionView:
            if not old.is_long:
                raise ValueError(f"{book}/{product_id} has no open position to protect")
            return replace(old, bracket_order_id=bracket_order_id, tp=tp or old.tp, sl=sl or old.sl)

        return await self._mutate_position(book, product_id, change)

    async def apply_exit_fill(self, book: str, product_id: str, qty: Decimal) -> PositionView:
        """Reduce the position by a SELL fill; at zero it becomes FLAT (dust is kept). Never goes below zero."""
        if qty <= 0:
            raise ValueError("an exit fill needs a positive quantity")

        def change(old: PositionView) -> PositionView:
            if not old.is_long or qty > old.base_qty:
                raise ShortPositionError(f"{book}/{product_id}: selling {qty} with {old.base_qty} held")
            left = old.base_qty - qty
            if left > 0:
                return replace(old, base_qty=left)
            return PositionView(book, product_id, dust_qty=old.dust_qty, dust_cost=old.dust_cost)

        return await self._mutate_position(book, product_id, change)

    async def set_dust(self, book: str, product_id: str, qty: Decimal, cost: Decimal) -> PositionView:
        """A fill below the minimum sellable size: the book stays FLAT and remembers it (sez. 4.1.6)."""

        def change(old: PositionView) -> PositionView:
            if old.is_long:
                raise ValueError("dust is recorded on a FLAT position only")
            return replace(old, dust_qty=qty, dust_cost=cost)

        return await self._mutate_position(book, product_id, change)

    # ---------------------------------------------------------------------- orders and fills

    async def record_order(self, o: OrderRecord) -> None:
        async with self._lock:
            await self._db.execute(
                "INSERT INTO orders (client_order_id, book, exchange_order_id, intent_id, leg, product_id, side,"
                " status, payload, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    o.client_order_id, o.book, o.exchange_order_id, o.intent_id, o.leg, o.product_id, o.side,
                    o.status, o.payload, o.created_at.isoformat(), o.updated_at.isoformat(),
                ),
            )  # fmt: skip

    async def update_order(
        self, client_order_id: str, status: str, updated_at: datetime, exchange_order_id: str | None = None
    ) -> None:
        async with self._lock:
            cur = await self._db.execute(
                "UPDATE orders SET status=?, updated_at=?, exchange_order_id=COALESCE(?, exchange_order_id)"
                " WHERE client_order_id=?",
                (status, updated_at.isoformat(), exchange_order_id, client_order_id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"unknown order {client_order_id}")

    async def open_orders(self, book: str) -> list[OrderRecord]:
        marks = ",".join("?" * len(TERMINAL_STATUSES))
        sql = f"SELECT * FROM orders WHERE book=? AND status NOT IN ({marks}) ORDER BY created_at"
        async with self._db.execute(sql, (book, *sorted(TERMINAL_STATUSES))) as cur:
            return [
                OrderRecord(
                    r["client_order_id"], r["book"], r["product_id"], r["side"], r["leg"], r["status"],
                    datetime.fromisoformat(r["created_at"]), datetime.fromisoformat(r["updated_at"]),
                    r["exchange_order_id"], r["intent_id"], r["payload"],
                )
                for r in await cur.fetchall()
            ]  # fmt: skip

    async def record_fill(self, f: FillRecord) -> bool:
        """Store a fill once. Returns False if this ``trade_id`` was already recorded (WS and polling both deliver)."""
        async with self._lock:
            cur = await self._db.execute(
                "INSERT OR IGNORE INTO fills (trade_id, book, client_order_id, product_id, side, price, size, fee,"
                " liquidity, trade_time) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    f.trade_id, f.book, f.client_order_id, f.product_id, f.side, str(f.price), str(f.size),
                    str(f.fee), f.liquidity, f.trade_time.isoformat(),
                ),
            )  # fmt: skip
            return cur.rowcount == 1

    async def fills_for(self, client_order_id: str) -> list[FillRecord]:
        async with self._db.execute(
            "SELECT * FROM fills WHERE client_order_id=? ORDER BY trade_time", (client_order_id,)
        ) as cur:
            return [
                FillRecord(
                    r["trade_id"], r["book"], r["client_order_id"], r["product_id"], r["side"], Decimal(r["price"]),
                    Decimal(r["size"]), Decimal(r["fee"]), r["liquidity"], datetime.fromisoformat(r["trade_time"]),
                )
                for r in await cur.fetchall()
            ]  # fmt: skip


def utc_now() -> datetime:
    return datetime.now(UTC)
