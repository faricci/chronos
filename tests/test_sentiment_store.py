"""NewsStore (spec sez. 1.2 DDL, sez. 1.3 punto 8)."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from modules.sentiment.store import NewsStore
from tests.sentiment_helpers import T0, event, item


@pytest.fixture
async def store() -> NewsStore:
    s = await NewsStore.open(":memory:")
    yield s  # type: ignore[misc]
    await s.close()


async def test_news_roundtrip_and_insert_or_ignore(store: NewsStore) -> None:
    n = item("Solana Foundation announces X", assets=("SOL",), summary="S")
    assert await store.add_news(n) is True
    assert await store.add_news(n.model_copy(update={"source": "other"})) is False  # same news_id: ignored
    got = await store.get_news(n.news_id)
    assert got == n
    assert await store.count_news() == 1


async def test_increment_source_count(store: NewsStore) -> None:
    n = item("Story")
    assert await store.increment_source_count(n.news_id) is False  # not stored: no row touched
    await store.add_news(n)
    assert await store.increment_source_count(n.news_id) is True
    got = await store.get_news(n.news_id)
    assert got is not None and got.source_count == 2


async def test_events_insert_or_ignore_and_reload(store: NewsStore) -> None:
    n = item("Story", assets=("SOL",))
    await store.add_news(n)
    e = event(news_id=n.news_id, ingested_at=n.ingested_at, probs=(0.06, 0.21, 0.73), confidence=0.41, relevance=0.6)
    assert await store.add_events([e]) == 1
    assert await store.add_events([e]) == 0
    [(back, count)] = await store.load_events(T0 - timedelta(days=1))
    assert count == 1
    assert (back.news_id, back.asset_key, back.choice, back.label) == (n.news_id, "SOL", 2, "BULLISH")
    assert back.probabilities == e.probabilities and back.confidence == 0.41 and back.relevance == 0.6
    assert back.ingested_at == e.ingested_at and back.classified_at is None  # not persisted by the DDL
    assert await store.add_events([]) == 0


async def test_load_events_filters_on_ingested_at_with_exact_text_comparison(store: NewsStore) -> None:
    for i, t in enumerate([T0, T0 + timedelta(milliseconds=1), T0 + timedelta(seconds=1)]):
        n = item(f"Story {i}", ingested_at=t, assets=("SOL",))
        await store.add_news(n)
        await store.add_events([event(news_id=n.news_id, ingested_at=t)])
    rows = await store.load_events(T0 + timedelta(milliseconds=1))
    assert [e.ingested_at for e, _ in rows] == [T0 + timedelta(milliseconds=1), T0 + timedelta(seconds=1)]


async def test_unclassified_lists_only_stories_without_events(store: NewsStore) -> None:
    a, b = item("Alpha SOL", ingested_at=T0, assets=("SOL",)), item("Beta ETH", ingested_at=T0 + timedelta(minutes=1))
    await store.add_news(a)
    await store.add_news(b)
    await store.add_events([event(news_id=a.news_id, ingested_at=a.ingested_at)])
    assert [n.news_id for n in await store.unclassified(10)] == [b.news_id]
    assert await store.unclassified(0) == []


async def test_recent_news_for_the_dedup_warmup(store: NewsStore) -> None:
    old, new = item("Old story", ingested_at=T0 - timedelta(hours=60)), item("New story", ingested_at=T0)
    await store.add_news(old)
    await store.add_news(new)
    assert await store.recent_news(T0 - timedelta(hours=48)) == [(new.news_id, new.source)]


async def test_ddl_matches_the_spec_and_wal_is_on(tmp_path: Path) -> None:
    path = tmp_path / "state" / "news.db"
    s = await NewsStore.open(path)  # creates the directory
    await s.close()
    con = sqlite3.connect(path)
    try:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        cols = {r[1] for r in con.execute("PRAGMA table_info(news_items)")}
        assert cols == {
            "news_id",
            "source",
            "url",
            "title",
            "summary",
            "published_at",
            "ingested_at",
            "source_count",
            "assets",
            "is_market_wide",
        }
        cols = {r[1] for r in con.execute("PRAGMA table_info(sentiment_events)")}
        assert cols == {
            "news_id",
            "asset_key",
            "ingested_at",
            "model",
            "choice",
            "p_bear",
            "p_neu",
            "p_bull",
            "confidence",
            "relevance",
        }
        indexes = {r[1] for r in con.execute("PRAGMA index_list(sentiment_events)")}
        assert "ix_sent_asset_time" in indexes
    finally:
        con.close()


async def test_persists_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "news.db"
    s = await NewsStore.open(path)
    n = item("Persisted story", assets=("BTC",))
    await s.add_news(n)
    await s.close()
    s2 = await NewsStore.open(path)
    try:
        assert await s2.get_news(n.news_id) == n
    finally:
        await s2.close()
