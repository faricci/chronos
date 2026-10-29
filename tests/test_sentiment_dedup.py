"""Normalisation and de-duplication (spec sez. 1.1 fase 2, sez. 1.3 punto 3)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from modules.sentiment.dedup import Deduplicator, news_id_for, normalize_title
from modules.sentiment.store import NewsStore
from tests.sentiment_helpers import item


@pytest.fixture
async def store() -> AsyncIterator[NewsStore]:
    s = await NewsStore.open(":memory:")
    yield s
    await s.close()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Bitcoin TOPS $120,000!", "bitcoin tops $120000"),
        ("  Bitcoin   tops \t 120,000  ", "bitcoin tops 120000"),
        ("“Quoted” — and it’s here…", "quoted and its here"),
        ("ＢＴＣ　rallies", "btc rallies"),  # full-width letters and ideographic space
        ("", ""),
    ],
)
def test_normalize_title(raw: str, expected: str) -> None:
    assert normalize_title(raw) == expected


def test_titles_differing_by_punctuation_and_case_share_the_id() -> None:
    a = news_id_for("Bitcoin tops $120,000 as ETF inflows accelerate")
    b = news_id_for("BITCOIN TOPS $120,000 AS ETF INFLOWS ACCELERATE!")
    c = news_id_for("Bitcoin  tops $120,000, as ETF inflows accelerate...")
    assert a == b == c
    assert len(a) == 40
    assert news_id_for("Bitcoin tops $130,000") != a


def test_new_duplicate_and_repost() -> None:
    d = Deduplicator()
    first = item("Bitcoin tops 120k", source="coindesk_rss")
    assert d.check(first) == "new"
    assert d.check(first) == "duplicate"  # the same feed polled again
    assert d.check(item("BITCOIN TOPS 120K!", source="cointelegraph_rss")) == "repost"
    assert d.check(item("bitcoin tops 120k", source="cointelegraph_rss")) == "duplicate"  # already counted


async def test_repost_increments_source_count_in_the_database_once_per_source(store: NewsStore) -> None:
    d = Deduplicator(store)
    a = item("Bitcoin tops 120k", source="coindesk_rss", assets=("BTC",))
    await store.add_news(a)
    assert await d.admit(a) == "new"
    assert await d.admit(a) == "duplicate"
    assert await d.admit(item("Bitcoin tops 120k!", source="cointelegraph_rss")) == "repost"
    assert await d.admit(item("Bitcoin tops 120k!", source="cointelegraph_rss")) == "duplicate"
    assert await d.admit(item("Bitcoin tops 120k?", source="decrypt_rss")) == "repost"
    stored = await store.get_news(a.news_id)
    assert stored is not None and stored.source_count == 3


def test_window_expires_with_the_ttl() -> None:
    now = [0.0]
    d = Deduplicator(window_hours=48, timer=lambda: now[0])
    it = item("Bitcoin tops 120k")
    assert d.check(it) == "new"
    now[0] = 47 * 3600
    assert d.check(it) == "duplicate"
    now[0] = 48 * 3600 + 1  # past the window the story counts as new again
    assert d.check(it) == "new"


def test_a_repost_does_not_extend_the_window() -> None:
    now = [0.0]
    d = Deduplicator(window_hours=48, timer=lambda: now[0])
    assert d.check(item("Bitcoin tops 120k", source="a")) == "new"
    now[0] = 40 * 3600
    assert d.check(item("Bitcoin tops 120k", source="b")) == "repost"
    now[0] = 49 * 3600
    assert d.check(item("Bitcoin tops 120k", source="c")) == "new"


def test_lru_is_bounded() -> None:
    d = Deduplicator(maxsize=3)
    for i in range(10):
        d.check(item(f"story number {i}"))
    assert len(d) == 3


def test_warm_up_from_the_store() -> None:
    d = Deduplicator()
    it = item("Bitcoin tops 120k", source="coindesk_rss")
    d.warm([(it.news_id, "coindesk_rss")])
    assert d.check(it) == "duplicate"
