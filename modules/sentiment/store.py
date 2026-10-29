"""``NewsStore``: stories and sentiment events in SQLite (``state/news.db``, spec sez. 1.2 and 1.3 punto 8).

One file shared by the modes (sez. 0.5): news and sentiment do not depend on dry/live. WAL mode, so the service can
write while ``classify_backlog`` or the label tools read. Timestamps are stored as the fixed-width ISO text of
``schemas.to_iso``: the column sorts like time and ``ingested_at <= ?`` is exact (constraint C7).

The tables are the ones of the spec, nothing more. A story is stored as soon as it is tagged, WITHOUT waiting for the
classifier: with no model installed the collection still runs, and the stories can be classified later with their
original ``ingested_at`` (``unclassified`` + ``classify_backlog``), so the point-in-time view loses nothing.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Final

import aiosqlite
import structlog

from modules.sentiment.schemas import LABELS, NewsItem, SentimentEvent, parse_iso, to_iso

log = structlog.get_logger(__name__)

SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS news_items (
  news_id TEXT PRIMARY KEY, source TEXT NOT NULL, url TEXT, title TEXT NOT NULL,
  summary TEXT, published_at TEXT, ingested_at TEXT NOT NULL, source_count INTEGER DEFAULT 1,
  assets TEXT NOT NULL, is_market_wide INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sentiment_events (
  news_id TEXT NOT NULL, asset_key TEXT NOT NULL, ingested_at TEXT NOT NULL,
  model TEXT NOT NULL, choice INTEGER NOT NULL, p_bear REAL, p_neu REAL, p_bull REAL,
  confidence REAL, relevance REAL, PRIMARY KEY (news_id, asset_key)
);
CREATE INDEX IF NOT EXISTS ix_sent_asset_time ON sentiment_events(asset_key, ingested_at);
"""


def _row_to_item(r: aiosqlite.Row) -> NewsItem:
    return NewsItem(
        news_id=r["news_id"],
        source=r["source"],
        url=r["url"],
        title=r["title"],
        summary=r["summary"] or "",
        published_at=None if r["published_at"] is None else parse_iso(r["published_at"]),
        ingested_at=parse_iso(r["ingested_at"]),
        source_count=r["source_count"] or 1,
        assets=tuple(json.loads(r["assets"])),
        is_market_wide=bool(r["is_market_wide"]),
    )


def _row_to_event(r: aiosqlite.Row) -> SentimentEvent:
    p_bear, p_neu, p_bull = r["p_bear"], r["p_neu"], r["p_bull"]
    choice = r["choice"]
    return SentimentEvent(
        news_id=r["news_id"],
        asset_key=r["asset_key"],
        ingested_at=parse_iso(r["ingested_at"]),
        model=r["model"],
        choice=choice,
        label=LABELS[choice],
        probabilities={"BEARISH": p_bear, "NEUTRAL": p_neu, "BULLISH": p_bull},
        confidence=r["confidence"],
        relevance=r["relevance"],
    )


class NewsStore:
    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db
        self._lock = asyncio.Lock()  # one writer at a time inside this process; WAL covers the other processes

    @classmethod
    async def open(cls, path: str | Path) -> NewsStore:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(path, isolation_level=None)  # transactions are explicit
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")  # WAL + NORMAL cannot corrupt; a power cut loses at most the tail
        await db.execute("PRAGMA busy_timeout=5000")  # another process (classify_backlog) may be writing
        await db.executescript(SCHEMA)
        return cls(db)

    async def close(self) -> None:
        await self._db.close()

    # ------------------------------------------------------------------------------------- writes

    async def add_news(self, item: NewsItem) -> bool:
        """``INSERT OR IGNORE``; True if the story was new to the database."""
        async with (
            self._lock,
            self._db.execute(
                "INSERT OR IGNORE INTO news_items (news_id, source, url, title, summary, published_at, ingested_at,"
                " source_count, assets, is_market_wide) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    item.news_id,
                    item.source,
                    item.url,
                    item.title,
                    item.summary,
                    None if item.published_at is None else to_iso(item.published_at),
                    to_iso(item.ingested_at),
                    item.source_count,
                    json.dumps(list(item.assets)),
                    int(item.is_market_wide),
                ),
            ) as cur,
        ):
            return cur.rowcount == 1

    async def increment_source_count(self, news_id: str) -> bool:
        """``UPDATE news_items SET source_count = source_count + 1``; False if the story is not stored."""
        async with (
            self._lock,
            self._db.execute(
                "UPDATE news_items SET source_count = source_count + 1 WHERE news_id = ?", (news_id,)
            ) as cur,
        ):
            return cur.rowcount == 1

    async def add_events(self, events: Sequence[SentimentEvent]) -> int:
        """``INSERT OR IGNORE`` in one transaction; returns how many rows were new."""
        if not events:
            return 0
        rows = [
            (
                e.news_id,
                e.asset_key,
                to_iso(e.ingested_at),
                e.model,
                e.choice,
                e.probabilities["BEARISH"],
                e.probabilities["NEUTRAL"],
                e.probabilities["BULLISH"],
                e.confidence,
                e.relevance,
            )
            for e in events
        ]
        async with self._lock:
            await self._db.execute("BEGIN IMMEDIATE")
            try:
                before = self._db.total_changes
                await self._db.executemany(
                    "INSERT OR IGNORE INTO sentiment_events (news_id, asset_key, ingested_at, model, choice,"
                    " p_bear, p_neu, p_bull, confidence, relevance) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    rows,
                )
                added = self._db.total_changes - before
                await self._db.execute("COMMIT")
            except BaseException:
                await self._db.execute("ROLLBACK")
                raise
        return added

    # -------------------------------------------------------------------------------------- reads

    async def count_news(self) -> int:
        async with self._db.execute("SELECT COUNT(*) FROM news_items") as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def get_news(self, news_id: str) -> NewsItem | None:
        async with self._db.execute("SELECT * FROM news_items WHERE news_id = ?", (news_id,)) as cur:
            row = await cur.fetchone()
        return None if row is None else _row_to_item(row)

    async def recent_news(self, since: datetime) -> list[tuple[str, str]]:
        """``(news_id, source)`` of the stories ingested at or after ``since`` (warm-up of the deduplicator)."""
        async with self._db.execute(
            "SELECT news_id, source FROM news_items WHERE ingested_at >= ?", (to_iso(since),)
        ) as cur:
            return [(r["news_id"], r["source"]) for r in await cur.fetchall()]

    async def all_news(self) -> list[NewsItem]:
        async with self._db.execute("SELECT * FROM news_items ORDER BY ingested_at, news_id") as cur:
            return [_row_to_item(r) for r in await cur.fetchall()]

    async def unclassified(self, limit: int) -> list[NewsItem]:
        """Oldest stories that have no sentiment event yet (the backlog of a run without a model)."""
        async with self._db.execute(
            "SELECT n.* FROM news_items n"
            " WHERE NOT EXISTS (SELECT 1 FROM sentiment_events e WHERE e.news_id = n.news_id)"
            " ORDER BY n.ingested_at, n.news_id LIMIT ?",
            (limit,),
        ) as cur:
            return [_row_to_item(r) for r in await cur.fetchall()]

    async def load_events(self, since: datetime) -> list[tuple[SentimentEvent, int]]:
        """Events ingested at or after ``since``, oldest first, with the ``source_count`` of their story."""
        async with self._db.execute(
            "SELECT e.*, n.source_count AS source_count FROM sentiment_events e"
            " JOIN news_items n ON n.news_id = e.news_id WHERE e.ingested_at >= ?"
            " ORDER BY e.ingested_at, e.news_id, e.asset_key",
            (to_iso(since),),
        ) as cur:
            return [(_row_to_event(r), int(r["source_count"] or 1)) for r in await cur.fetchall()]
