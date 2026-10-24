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

Closed trades (spec sez. 5.2 ``VirtualTrade``, sez. 6.3 D7). The SELL fill that takes a position to zero writes one
row of ``closed_trades`` in the SAME transaction as the fill and the position (a crash cannot leave a closed
position without its trade, nor a trade without its fill). The row is computed from the ``fills`` table (every BUY
and SELL of the (book, product) since the position was opened), so it is the same in live and dry and for every path
that closes a position (bracket, model exit, watchdog). ``trade_id`` is the id of the closing fill (unique, and the
write is idempotent). The exit reason is ``TP`` / ``SL`` for a bracket leg (told apart by the fill price against the
position's take profit) and, for the other legs, the reason announced with ``expect_exit`` (``SELL`` when nobody said
anything). A dust conversion is not a sale and writes no trade. ``add_close_listener`` callbacks run after the commit
and can never break the write. Opening an older database adds the table (and renames a legacy ``virtual_trades``)
without touching the existing rows.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Literal

import aiosqlite
import structlog

log = structlog.get_logger(__name__)

Mode = Literal["live", "dry"]
Side = Literal["FLAT", "LONG"]
ZERO: Final = Decimal(0)
BPS: Final = Decimal(10_000)
SCHEMA_VERSION: Final = 2  # 1: positions, orders, fills, kill_switch_state; 2: + closed_trades

SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS positions (
  book TEXT NOT NULL, product_id TEXT NOT NULL, side TEXT NOT NULL CHECK (side IN ('FLAT','LONG')),
  base_qty TEXT NOT NULL, avg_entry TEXT, opened_at TEXT, entry_decision_id TEXT,
  bracket_order_id TEXT, tp TEXT, sl TEXT, k_tp REAL, k_sl REAL, atr_at_entry TEXT,
  dust_qty TEXT NOT NULL DEFAULT '0', dust_cost TEXT NOT NULL DEFAULT '0',
  model_version TEXT, label_config_sha256 TEXT, horizon_min INTEGER,
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
CREATE TABLE IF NOT EXISTS closed_trades (
  trade_id TEXT PRIMARY KEY, book TEXT NOT NULL, product_id TEXT NOT NULL, entry_decision_id TEXT,
  entry_avg_price TEXT, entry_size TEXT, entry_fees TEXT, entry_at TEXT,
  tp TEXT, sl TEXT, k_tp REAL, k_sl REAL,
  exit_reason TEXT NOT NULL, exit_avg_price TEXT, exit_size TEXT, exit_fees TEXT, exit_at TEXT NOT NULL,
  gross_quote TEXT, fees_quote TEXT, net_quote TEXT NOT NULL, net_bps INTEGER, r_multiple REAL,
  held_min INTEGER, partial_entry INTEGER NOT NULL DEFAULT 0, gap_resolved INTEGER NOT NULL DEFAULT 0,
  model_version TEXT, fill_model_version TEXT, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS closed_trades_book_exit ON closed_trades (book, exit_at);
"""

# columns a ``closed_trades`` table must have; an older table that lacks some gets them added (nullable)
CLOSED_TRADES_COLUMNS: Final = {
    "entry_decision_id": "TEXT", "entry_avg_price": "TEXT", "entry_size": "TEXT", "entry_fees": "TEXT",
    "entry_at": "TEXT", "tp": "TEXT", "sl": "TEXT", "k_tp": "REAL", "k_sl": "REAL", "exit_avg_price": "TEXT",
    "exit_size": "TEXT", "exit_fees": "TEXT", "gross_quote": "TEXT", "fees_quote": "TEXT", "net_bps": "INTEGER",
    "r_multiple": "REAL", "held_min": "INTEGER", "partial_entry": "INTEGER NOT NULL DEFAULT 0",
    "gap_resolved": "INTEGER NOT NULL DEFAULT 0", "model_version": "TEXT", "fill_model_version": "TEXT",
}  # fmt: skip

# columns a ``positions`` table must have (spec 5.1.6: the model, label_config and horizon the position was OPENED
# under); an older table gets them added, nullable, and no existing row is touched
POSITIONS_COLUMNS: Final = {"model_version": "TEXT", "label_config_sha256": "TEXT", "horizon_min": "INTEGER"}
ENTRY_STAMPS_MAX: Final = 512  # stamps of entries that never filled are dropped oldest first

TERMINAL_STATUSES: Final = frozenset({"FILLED", "CANCELLED", "EXPIRED", "REJECTED", "FAILED"})
EXIT_REASONS: Final = frozenset({"TP", "SL", "SELL", "HORIZON", "WATCHDOG", "MANUAL"})


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
    # what the position was opened under: the model that decided the entry, the sha256 of its label_config and its
    # horizon H. The SELL of a model with another label_config does not apply to it (spec 5.1.6) and the watchdog
    # closes it at ITS horizon, whatever the book runs now. ``None``: opened before they were recorded.
    model_version: str | None = None
    label_sha256: str | None = None
    horizon_min: int | None = None

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
class EntryStamp:
    """The model an entry was decided by, announced BEFORE the order (``stamp_entry``): the position it opens takes it,
    even when the version of the book changes while the entry waits to fill."""

    model_version: str
    label_sha256: str
    horizon_min: int


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


def _z(t: datetime) -> str:
    return t.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    """One position from its first BUY fill to the SELL fill that took it to zero (``VirtualTrade``, sez. 5.2).

    ``gross_quote`` is the sale value minus the purchase value, ``fees_quote`` the fees of all the fills,
    ``net_quote = gross_quote - fees_quote``. Sizes and prices come from the fills, not from the position row.
    """

    trade_id: str
    book: str
    product_id: str
    entry_decision_id: str | None
    entry_avg_price: Decimal
    entry_size: Decimal
    entry_fees: Decimal
    entry_at: datetime
    tp: Decimal | None
    sl: Decimal | None
    k_tp: float | None
    k_sl: float | None
    exit_reason: str
    exit_avg_price: Decimal
    exit_size: Decimal
    exit_fees: Decimal
    exit_at: datetime
    gross_quote: Decimal
    fees_quote: Decimal
    net_quote: Decimal
    net_bps: int
    r_multiple: float | None
    held_min: int
    partial_entry: bool = False  # the sold size differs from the bought size (dust merged into the entry)
    gap_resolved: bool = False
    model_version: str | None = None
    fill_model_version: str | None = None

    def to_payload(self) -> dict[str, Any]:
        """The ``VirtualTrade`` JSON of sez. 5.2 (money as Decimal strings, times as ISO UTC with ``Z``)."""
        return {
            "trade_id": self.trade_id,
            "book": self.book,
            "product_id": self.product_id,
            "entry_decision_id": self.entry_decision_id,
            "entry": {
                "avg_price": str(self.entry_avg_price), "size": str(self.entry_size),
                "fees": str(self.entry_fees), "at": _z(self.entry_at),
            },
            "bracket": {
                "tp": None if self.tp is None else str(self.tp), "sl": None if self.sl is None else str(self.sl),
                "k_tp": self.k_tp, "k_sl": self.k_sl,
            },
            "exit": {
                "reason": self.exit_reason, "avg_price": str(self.exit_avg_price), "size": str(self.exit_size),
                "fees": str(self.exit_fees), "at": _z(self.exit_at),
            },
            "pnl": {
                "gross_quote": str(self.gross_quote), "fees_quote": str(self.fees_quote),
                "net_quote": str(self.net_quote), "net_bps": self.net_bps, "r_multiple": self.r_multiple,
            },
            "held_min": self.held_min,
            "flags": {"gap_resolved": self.gap_resolved, "partial_entry": self.partial_entry},
            "model_version": self.model_version,
            "fill_model_version": self.fill_model_version,
        }  # fmt: skip


CloseListener = Callable[[ClosedTrade], None]


class BookPositions:
    """Read-only, synchronous view of one book: the ``PositionCache`` the ``BalanceGuard`` needs."""

    def __init__(self, ledger: PositionLedger, book: str) -> None:
        self._ledger = ledger
        self._book = book

    def view(self, product_id: str) -> PositionView:
        return self._ledger.view(self._book, product_id)

    def stamp_entry(self, decision_id: str, stamp: EntryStamp) -> None:
        self._ledger.stamp_entry(self._book, decision_id, stamp)


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
        model_version=r["model_version"],
        label_sha256=r["label_config_sha256"],
        horizon_min=r["horizon_min"],
    )


def _row_to_order(r: aiosqlite.Row) -> OrderRecord:
    return OrderRecord(
        r["client_order_id"], r["book"], r["product_id"], r["side"], r["leg"], r["status"],
        datetime.fromisoformat(r["created_at"]), datetime.fromisoformat(r["updated_at"]),
        r["exchange_order_id"], r["intent_id"], r["payload"],
    )  # fmt: skip


def _s(value: object) -> str | None:
    return None if value is None else str(value)


async def _table_exists(db: aiosqlite.Connection, name: str) -> bool:
    async with db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)) as cur:
        return await cur.fetchone() is not None


async def _migrate(db: aiosqlite.Connection) -> None:
    """Bring any database of this ledger to ``SCHEMA_VERSION`` without touching existing rows.

    ``CREATE ... IF NOT EXISTS`` adds the tables an older database lacks; a legacy ``virtual_trades`` table is renamed
    to ``closed_trades`` (sez. 6.3 D7); a ``closed_trades`` that lacks columns gets them (nullable or with a default).
    """
    if await _table_exists(db, "virtual_trades") and not await _table_exists(db, "closed_trades"):
        await db.execute("ALTER TABLE virtual_trades RENAME TO closed_trades")
    await db.executescript(SCHEMA)
    async with db.execute("PRAGMA table_info(closed_trades)") as cur:
        have = {r["name"] for r in await cur.fetchall()}
    for column, ddl in CLOSED_TRADES_COLUMNS.items():
        if column not in have:
            await db.execute(f"ALTER TABLE closed_trades ADD COLUMN {column} {ddl}")
    async with db.execute("PRAGMA table_info(positions)") as cur:
        have = {r["name"] for r in await cur.fetchall()}
    for column, ddl in POSITIONS_COLUMNS.items():  # additive and nullable: the rows already there are not touched
        if column not in have:
            await db.execute(f"ALTER TABLE positions ADD COLUMN {column} {ddl}")
    await db.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )


def _closed_from_row(r: aiosqlite.Row) -> ClosedTrade:
    return ClosedTrade(
        trade_id=r["trade_id"], book=r["book"], product_id=r["product_id"], entry_decision_id=r["entry_decision_id"],
        entry_avg_price=Decimal(r["entry_avg_price"]), entry_size=Decimal(r["entry_size"]),
        entry_fees=Decimal(r["entry_fees"]), entry_at=datetime.fromisoformat(r["entry_at"]),
        tp=_dec(r["tp"]), sl=_dec(r["sl"]), k_tp=r["k_tp"], k_sl=r["k_sl"], exit_reason=r["exit_reason"],
        exit_avg_price=Decimal(r["exit_avg_price"]), exit_size=Decimal(r["exit_size"]),
        exit_fees=Decimal(r["exit_fees"]), exit_at=datetime.fromisoformat(r["exit_at"]),
        gross_quote=Decimal(r["gross_quote"]), fees_quote=Decimal(r["fees_quote"]),
        net_quote=Decimal(r["net_quote"]), net_bps=int(r["net_bps"]), r_multiple=r["r_multiple"],
        held_min=int(r["held_min"]), partial_entry=bool(r["partial_entry"]), gap_resolved=bool(r["gap_resolved"]),
        model_version=r["model_version"], fill_model_version=r["fill_model_version"],
    )  # fmt: skip


# ----------------------------------------------------------------------------------------- ledger


class PositionLedger:
    def __init__(self, db: aiosqlite.Connection, mode: Mode) -> None:
        self._db = db
        self.mode: Mode = mode
        self._cache: dict[tuple[str, str], PositionView] = {}
        self._lock = asyncio.Lock()
        self._exit_hints: dict[tuple[str, str], str] = {}
        self._trade_info: dict[str, tuple[str | None, str | None]] = {}
        self._book_stamp: dict[str, EntryStamp] = {}  # the model a book runs now: the fallback of an unannounced entry
        self._entry_stamps: dict[tuple[str, str], EntryStamp] = {}  # (book, decision_id) -> the model that decided
        self._listeners: list[CloseListener] = []
        self.gap_resolution = False  # True while the bars of a downtime are replayed: the trades they close say so

    @classmethod
    async def open(cls, path: str | Path, mode: Mode) -> PositionLedger:
        """Open (and create) the database of ``mode``. Raises ``LedgerModeMismatch`` on the wrong one."""
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(path, isolation_level=None)  # transactions are explicit
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=FULL")  # a position record must survive a power cut
        try:
            await db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            async with db.execute("SELECT value FROM meta WHERE key='mode'") as cur:
                row = await cur.fetchone()
            if row is not None and row["value"] != mode:  # the wrong database is not touched, not even migrated
                raise LedgerModeMismatch(f"{path} is a {row['value']!r} database, refusing to open it as {mode!r}")
            await _migrate(db)
            if row is None:
                await db.execute("INSERT INTO meta (key, value) VALUES ('mode', ?)", (mode,))
        except BaseException:
            await db.close()
            raise
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
        await self._db.execute(
            "UPDATE positions SET model_version=?, label_config_sha256=?, horizon_min=? WHERE book=? AND product_id=?",
            (v.model_version, v.label_sha256, v.horizon_min, v.book, v.product_id),
        )

    async def _mutate_position(
        self, book: str, product_id: str, change: object, fill: FillRecord | None = None
    ) -> tuple[PositionView, bool]:
        """Run ``change(old_view) -> new_view`` and persist it atomically; the cache follows only after commit.

        With ``fill`` the fill row is stored in the SAME transaction as the position: either both are on disk or
        neither. A fill already stored (same ``trade_id``) rolls everything back and returns ``(old_view, False)``."""
        assert callable(change)
        trade: ClosedTrade | None = None
        async with self._lock:
            old = self.view(book, product_id)
            if fill is not None and await self._fill_exists(fill.trade_id):
                return old, False
            new: PositionView = change(old)
            await self._db.execute("BEGIN IMMEDIATE")
            try:
                if fill is not None:
                    await self._insert_fill(fill)
                    if fill.side == "SELL" and old.is_long and not new.is_long:  # a sale took the position to zero
                        trade = await self._insert_closed_trade(old, fill)
                await self._write_position(new)
                await self._db.execute("COMMIT")
            except BaseException:
                await self._db.execute("ROLLBACK")
                raise
            self._cache[(book, product_id)] = new
            if trade is not None:
                self._exit_hints.pop((book, product_id), None)
        if trade is not None:
            self._notify_closed(trade)
        return new, True

    # ----------------------------------------------------------------------------------- closed trades

    def expect_exit(self, book: str, product_id: str, reason: str) -> None:
        """Announce why the position of (book, product) is about to be sold by a non-bracket order (``SELL``,
        ``HORIZON``, ``WATCHDOG``, ``MANUAL``). Used by the close that follows; ``clear_exit`` withdraws it."""
        if reason not in EXIT_REASONS:
            reason = "SELL"  # the decision reasons of a model exit all count as a model SELL
        self._exit_hints[(book, product_id)] = reason

    def clear_exit(self, book: str, product_id: str) -> None:
        self._exit_hints.pop((book, product_id), None)

    def set_trade_info(
        self, book: str, *, model_version: str | None, fill_model_version: str | None,
        label_sha256: str | None = None, horizon_min: int | None = None,
    ) -> None:  # fmt: skip
        """What the book runs NOW. ``fill_model_version`` is written on every trade the book closes from now on.
        ``model_version`` is the fallback for a trade whose position carries none (it was opened before the stamps
        existed); a position takes the model that DECIDED its entry (``stamp_entry``), or, when nobody announced one,
        the model the book runs at the first fill (``label_sha256`` and ``horizon_min`` complete it)."""
        self._trade_info[book] = (model_version, fill_model_version)
        if model_version is not None and label_sha256 is not None and horizon_min is not None:
            self._book_stamp[book] = EntryStamp(model_version, label_sha256, horizon_min)
        else:
            self._book_stamp.pop(book, None)

    def stamp_entry(self, book: str, decision_id: str, stamp: EntryStamp) -> None:
        """The model that decided the entry ``decision_id``: the position it opens records it at its first fill."""
        self._entry_stamps[(book, decision_id)] = stamp
        while len(self._entry_stamps) > ENTRY_STAMPS_MAX:
            self._entry_stamps.pop(next(iter(self._entry_stamps)))

    async def stamp_open_positions(self, book: str, stamp: EntryStamp) -> list[str]:
        """Migration at start: the open positions of ``book`` that carry no model (opened by a build that did not
        record it) take ``stamp``, the model the book starts with. Positions that already carry one are NOT touched.
        Returns the products stamped."""
        done: list[str] = []
        for pos in self.long_positions(book):
            if pos.model_version is not None:
                continue
            await self._mutate_position(
                book, pos.product_id,
                lambda old: replace(
                    old, model_version=stamp.model_version, label_sha256=stamp.label_sha256,
                    horizon_min=stamp.horizon_min,
                ),
            )  # fmt: skip
            done.append(pos.product_id)
        return done

    def add_close_listener(self, listener: CloseListener) -> None:
        """``listener(trade)`` after each closed trade is committed (loss streak, events). It runs synchronously, must
        not block, and an exception in it is logged and dropped."""
        self._listeners.append(listener)

    def _notify_closed(self, trade: ClosedTrade) -> None:
        for listener in self._listeners:
            try:
                listener(trade)
            except Exception as exc:
                log.error("close_listener_failed", trade_id=trade.trade_id, error=f"{type(exc).__name__}: {exc}")

    async def _exit_reason_of(self, old: PositionView, fill: FillRecord) -> str:
        async with self._db.execute("SELECT leg FROM orders WHERE client_order_id=?", (fill.client_order_id,)) as cur:
            row = await cur.fetchone()
        if row is not None and row["leg"] == "bracket":  # told apart by the price: the take profit is a limit above
            return "TP" if old.tp is not None and fill.price >= old.tp else "SL"
        return self._exit_hints.get((old.book, old.product_id), "SELL")

    async def _insert_closed_trade(self, old: PositionView, fill: FillRecord) -> ClosedTrade:
        """The row of the position ``old`` that ``fill`` closes. Called inside the transaction of the fill (which is
        already inserted): the fills of the position are those of the (book, product) from its opening on."""
        assert old.opened_at is not None
        since = old.opened_at.astimezone(UTC).isoformat()
        async with self._db.execute(
            "SELECT side, price, size, fee FROM fills WHERE book=? AND product_id=? AND trade_time>=?"
            " ORDER BY trade_time, rowid",
            (old.book, old.product_id, since),
        ) as cur:
            rows = await cur.fetchall()
        buy_size = buy_notional = buy_fees = sell_size = sell_notional = sell_fees = ZERO
        for r in rows:
            price, size, fee = Decimal(r["price"]), Decimal(r["size"]), Decimal(r["fee"])
            if r["side"] == "BUY":
                buy_size, buy_notional, buy_fees = buy_size + size, buy_notional + price * size, buy_fees + fee
            else:
                sell_size, sell_notional, sell_fees = sell_size + size, sell_notional + price * size, sell_fees + fee
        if buy_size <= 0 or sell_size <= 0:  # the fills of the position are not all there: keep the position's own view
            buy_size, buy_notional = old.base_qty, (old.avg_entry or fill.price) * old.base_qty
            sell_size, sell_notional, sell_fees = fill.size, fill.price * fill.size, fill.fee
        gross = sell_notional - buy_notional
        fees = buy_fees + sell_fees
        net = gross - fees
        entry_avg, exit_avg = buy_notional / buy_size, sell_notional / sell_size
        risk = None if old.sl is None else (entry_avg - old.sl) * buy_size
        info_model, fill_model_version = self._trade_info.get(old.book, (None, None))
        model_version = old.model_version or info_model  # the model that opened it, not the one the book runs now
        trade = ClosedTrade(
            trade_id=fill.trade_id, book=old.book, product_id=old.product_id, entry_decision_id=old.entry_decision_id,
            entry_avg_price=entry_avg, entry_size=buy_size, entry_fees=buy_fees, entry_at=old.opened_at,
            tp=old.tp, sl=old.sl, k_tp=old.k_tp, k_sl=old.k_sl, exit_reason=await self._exit_reason_of(old, fill),
            exit_avg_price=exit_avg, exit_size=sell_size, exit_fees=sell_fees, exit_at=fill.trade_time,
            gross_quote=gross, fees_quote=fees, net_quote=net,
            net_bps=int((net / buy_notional * BPS).to_integral_value()) if buy_notional > 0 else 0,
            r_multiple=None if risk is None or risk <= 0 else float(net / risk),
            held_min=max(int((fill.trade_time - old.opened_at).total_seconds() // 60), 0),
            partial_entry=buy_size != sell_size, gap_resolved=self.gap_resolution, model_version=model_version,
            fill_model_version=fill_model_version,
        )  # fmt: skip
        await self._db.execute(
            "INSERT OR IGNORE INTO closed_trades (trade_id, book, product_id, entry_decision_id, entry_avg_price,"
            " entry_size, entry_fees, entry_at, tp, sl, k_tp, k_sl, exit_reason, exit_avg_price, exit_size,"
            " exit_fees, exit_at, gross_quote, fees_quote, net_quote, net_bps, r_multiple, held_min, partial_entry,"
            " gap_resolved, model_version, fill_model_version, payload)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                trade.trade_id, trade.book, trade.product_id, trade.entry_decision_id, str(trade.entry_avg_price),
                str(trade.entry_size), str(trade.entry_fees), trade.entry_at.isoformat(), _s(trade.tp), _s(trade.sl),
                trade.k_tp, trade.k_sl, trade.exit_reason, str(trade.exit_avg_price), str(trade.exit_size),
                str(trade.exit_fees), trade.exit_at.isoformat(), str(trade.gross_quote), str(trade.fees_quote),
                str(trade.net_quote), trade.net_bps, trade.r_multiple, trade.held_min, int(trade.partial_entry),
                int(trade.gap_resolved), trade.model_version, trade.fill_model_version,
                json.dumps(trade.to_payload(), separators=(",", ":")),
            ),
        )  # fmt: skip
        return trade

    async def closed_trades(self, book: str | None = None, since: datetime | None = None) -> list[ClosedTrade]:
        """The closed trades, oldest exit first; optionally of one book and/or with ``exit_at >= since``."""
        where: list[str] = []
        args: list[str] = []
        if book is not None:
            where.append("book=?")
            args.append(book)
        sql = "SELECT * FROM closed_trades" + (f" WHERE {' AND '.join(where)}" if where else "") + " ORDER BY exit_at"
        async with self._db.execute(sql, args) as cur:
            trades = [_closed_from_row(r) for r in await cur.fetchall()]
        return trades if since is None else [t for t in trades if t.exit_at >= since]

    async def day_totals(self, book: str, since: datetime) -> tuple[Decimal, Decimal]:
        """``(gross realized PnL, fees)`` of the book since ``since``: the gross result of the trades that closed
        and the fees of every fill booked (the open positions' entry fees included)."""
        stamp = since.astimezone(UTC).isoformat()
        async with self._db.execute(
            "SELECT gross_quote FROM closed_trades WHERE book=? AND exit_at>=?", (book, stamp)
        ) as cur:
            realized = sum((Decimal(r["gross_quote"]) for r in await cur.fetchall()), ZERO)
        async with self._db.execute("SELECT fee FROM fills WHERE book=? AND trade_time>=?", (book, stamp)) as cur:
            fees = sum((Decimal(r["fee"]) for r in await cur.fetchall()), ZERO)
        return realized, fees

    # ----------------------------------------------------------------------------------------- meta

    async def meta_get(self, key: str) -> str | None:
        async with self._db.execute("SELECT value FROM meta WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
        return None if row is None else str(row["value"])

    async def meta_set(self, key: str, value: str) -> None:
        async with self._lock:
            await self._db.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    async def book_fills(self, book: str) -> list[FillRecord]:
        """Every fill of the book in the order they happened (the rebuild of a virtual account replays them)."""
        async with self._db.execute("SELECT * FROM fills WHERE book=? ORDER BY trade_time, rowid", (book,)) as cur:
            return [
                FillRecord(
                    r["trade_id"], r["book"], r["client_order_id"], r["product_id"], r["side"], Decimal(r["price"]),
                    Decimal(r["size"]), Decimal(r["fee"]), r["liquidity"], datetime.fromisoformat(r["trade_time"]),
                )
                for r in await cur.fetchall()
            ]  # fmt: skip

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
        result, _ = await self._entry_fill(book, product_id, qty, price, decision_id, ts, plan, None)
        return result

    async def apply_entry_fill_recorded(
        self, fill: FillRecord, decision_id: str, plan: EntryPlan | None = None
    ) -> EntryResult | None:
        """Store a BUY fill AND add it to the position in one transaction (a crash cannot leave one without the
        other). ``None`` if the fill (same ``trade_id``) was already stored: nothing changes."""
        result, applied = await self._entry_fill(
            fill.book, fill.product_id, fill.size, fill.price, decision_id, fill.trade_time, plan, fill
        )
        return result if applied else None

    async def _entry_fill(
        self,
        book: str,
        product_id: str,
        qty: Decimal,
        price: Decimal,
        decision_id: str,
        ts: datetime,
        plan: EntryPlan | None,
        fill: FillRecord | None,
    ) -> tuple[EntryResult, bool]:
        if qty <= 0 or price <= 0:
            raise ValueError("an entry fill needs a positive quantity and price")
        merged = False
        stamp = self._entry_stamps.get((book, decision_id)) or self._book_stamp.get(book)

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
                model_version=None if stamp is None else stamp.model_version,
                label_sha256=None if stamp is None else stamp.label_sha256,
                horizon_min=None if stamp is None else stamp.horizon_min,
            )  # fmt: skip

        position, applied = await self._mutate_position(book, product_id, change, fill)
        if applied:
            self._entry_stamps.pop((book, decision_id), None)
        return EntryResult(position, merged and applied), applied

    async def set_bracket(
        self, book: str, product_id: str, bracket_order_id: str | None, tp: Decimal | None, sl: Decimal | None
    ) -> PositionView:
        """Attach (or clear, with ``None``) the standalone bracket of a long position."""

        def change(old: PositionView) -> PositionView:
            if not old.is_long:
                raise ValueError(f"{book}/{product_id} has no open position to protect")
            return replace(old, bracket_order_id=bracket_order_id, tp=tp or old.tp, sl=sl or old.sl)

        return (await self._mutate_position(book, product_id, change))[0]

    async def apply_exit_fill(self, book: str, product_id: str, qty: Decimal) -> PositionView:
        """Reduce the position by a SELL fill; at zero it becomes FLAT (dust is kept). Never goes below zero."""
        return (await self._exit_fill(book, product_id, qty, None))[0]

    async def apply_exit_fill_recorded(self, fill: FillRecord, qty: Decimal | None = None) -> PositionView | None:
        """Store a SELL fill AND reduce the position by ``qty`` (default: the fill's size) in one transaction.
        ``None`` if the fill (same ``trade_id``) was already stored: nothing changes."""
        position, applied = await self._exit_fill(fill.book, fill.product_id, fill.size if qty is None else qty, fill)
        return position if applied else None

    async def _exit_fill(
        self, book: str, product_id: str, qty: Decimal, fill: FillRecord | None
    ) -> tuple[PositionView, bool]:
        if qty <= 0:
            raise ValueError("an exit fill needs a positive quantity")

        def change(old: PositionView) -> PositionView:
            if not old.is_long or qty > old.base_qty:
                raise ShortPositionError(f"{book}/{product_id}: selling {qty} with {old.base_qty} held")
            left = old.base_qty - qty
            if left > 0:
                return replace(old, base_qty=left)
            return PositionView(book, product_id, dust_qty=old.dust_qty, dust_cost=old.dust_cost)

        return await self._mutate_position(book, product_id, change, fill)

    async def set_dust(self, book: str, product_id: str, qty: Decimal, cost: Decimal) -> PositionView:
        """A fill below the minimum sellable size: the book stays FLAT and remembers it (sez. 4.1.6)."""

        def change(old: PositionView) -> PositionView:
            if old.is_long:
                raise ValueError("dust is recorded on a FLAT position only")
            return replace(old, dust_qty=qty, dust_cost=cost)

        return (await self._mutate_position(book, product_id, change))[0]

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

    async def get_order(self, client_order_id: str, book: str | None = None) -> OrderRecord | None:
        """The record of an order in ANY status, or ``None``. With ``book``, an order of another book is not found."""
        args: tuple[str, ...] = (client_order_id,) if book is None else (client_order_id, book)
        sql = "SELECT * FROM orders WHERE client_order_id=?" + ("" if book is None else " AND book=?")
        async with self._db.execute(sql, args) as cur:
            row = await cur.fetchone()
        return None if row is None else _row_to_order(row)

    async def open_orders(self, book: str) -> list[OrderRecord]:
        marks = ",".join("?" * len(TERMINAL_STATUSES))
        sql = f"SELECT * FROM orders WHERE book=? AND status NOT IN ({marks}) ORDER BY created_at"
        async with self._db.execute(sql, (book, *sorted(TERMINAL_STATUSES))) as cur:
            return [_row_to_order(r) for r in await cur.fetchall()]

    async def _insert_fill(self, f: FillRecord) -> bool:
        """The fill INSERT, without lock or transaction of its own: callers hold the lock (and a transaction)."""
        cur = await self._db.execute(
            "INSERT OR IGNORE INTO fills (trade_id, book, client_order_id, product_id, side, price, size, fee,"
            " liquidity, trade_time) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                f.trade_id, f.book, f.client_order_id, f.product_id, f.side, str(f.price), str(f.size),
                str(f.fee), f.liquidity, f.trade_time.isoformat(),
            ),
        )  # fmt: skip
        return cur.rowcount == 1

    async def _fill_exists(self, trade_id: str) -> bool:
        async with self._db.execute("SELECT 1 FROM fills WHERE trade_id=?", (trade_id,)) as cur:
            return await cur.fetchone() is not None

    async def record_fill(self, f: FillRecord) -> bool:
        """Store a fill once. Returns False if this ``trade_id`` was already recorded (WS and polling both deliver).
        It does not touch the position: use ``apply_entry_fill_recorded`` / ``apply_exit_fill_recorded`` for that."""
        async with self._lock:
            return await self._insert_fill(f)

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
