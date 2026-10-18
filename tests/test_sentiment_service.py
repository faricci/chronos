"""SentimentService end to end (spec sez. 1.3 punto 9), the no-model collection and the backlog classifier.

Sources are the local XML/JSON fixtures served by respx; the ONNX model is replaced by ``FakeBackend``.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from modules.config import AppConfig, ClassifierConfig, PathsConfig, SentimentConfig, SourceConfig
from modules.sentiment.classifier import OnnxSentimentClassifier
from modules.sentiment.classify_backlog import classify_backlog
from modules.sentiment.service import SentimentService, asset_of, resolve_config, run_collection
from modules.sentiment.sources import SourceRunner, build_sources
from modules.sentiment.store import NewsStore
from modules.sentiment.tagger import AssetTagger
from tests.sentiment_helpers import FIXTURES, T0, FakeBackend, FakeClock, card, item

COINDESK = "https://www.coindesk.com/arc/outboundfeeds/rss/"
COINTELEGRAPH = "https://cointelegraph.com/rss"
FNG = "https://api.alternative.me/fng/"
MIN = timedelta(minutes=1)

CFG = SentimentConfig(
    sources=(
        SourceConfig(name="coindesk_rss", kind="rss", url=COINDESK, interval_s=3600),
        SourceConfig(name="cointelegraph_rss", kind="rss", url=COINTELEGRAPH, interval_s=3600),
        SourceConfig(name="fear_greed", kind="fear_greed", url=FNG, interval_s=3600),
    )
)
TABLE = {
    "Solana": (0.0, 0.5, 3.0),  # bullish
    "Bitcoin": (0.0, 0.5, 3.0),
    "SEC": (3.0, 0.5, 0.0),  # bearish
    "Brooklyn": (0.0, 3.0, 0.0),
}


def mock_network() -> None:
    respx.get(COINDESK).respond(200, content=(FIXTURES / "coindesk.xml").read_bytes())
    respx.get(COINTELEGRAPH).respond(200, content=(FIXTURES / "cointelegraph.xml").read_bytes())
    respx.get(FNG).respond(200, json=json.loads((FIXTURES / "fear_greed.json").read_text()))


@pytest.fixture
async def store() -> AsyncIterator[NewsStore]:
    s = await NewsStore.open(":memory:")
    yield s
    await s.close()


def make_service(
    store: NewsStore, *, with_classifier: bool, clock: FakeClock, backend: FakeBackend | None = None
) -> SentimentService:
    classifier = OnnxSentimentClassifier(backend or FakeBackend(TABLE), card()) if with_classifier else None
    sources = build_sources(CFG, env={}, clock=clock)
    return SentimentService(CFG, store, classifier=classifier, sources=sources, clock=clock)


async def run_until(svc: SentimentService, done: Callable[[], bool], timeout: float = 5.0) -> None:
    task = asyncio.create_task(svc.run())
    try:
        async with asyncio.timeout(timeout):
            while not done():
                if task.done():
                    task.result()
                await asyncio.sleep(0.02)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# ---------------------------------------------------------------------- collection without a model


@respx.mock
async def test_collects_and_stores_without_a_classifier(store: NewsStore) -> None:
    mock_network()
    clock = FakeClock()
    svc = make_service(store, with_classifier=False, clock=clock)
    await run_until(svc, lambda: svc.stats["stored"] >= 3 and svc.stats["repost"] >= 1)

    stored = await store.all_news()
    assert len(stored) == 3 and svc.stats["discarded"] == 1  # the pizza story is dropped before storage
    sol = next(n for n in stored if "Solana" in n.title)
    sec = next(n for n in stored if "SEC" in n.title)
    btc = next(n for n in stored if "bitcoin" in n.title.lower())
    assert sol.assets == ("SOL",) and not sol.is_market_wide
    assert sec.assets == () and sec.is_market_wide
    assert btc.assets == ("BTC",) and btc.source_count == 2  # coindesk + cointelegraph, counted once, count raised
    assert all(n.ingested_at == T0 for n in stored)  # ingestion time, not the publisher's
    assert await store.load_events(T0 - timedelta(days=1)) == []  # no model: stored, not classified
    assert len(await store.unclassified(100)) == 3


@respx.mock
async def test_polling_the_same_feed_again_stores_nothing_new(store: NewsStore) -> None:
    mock_network()
    svc = make_service(store, with_classifier=False, clock=FakeClock())
    await run_until(svc, lambda: svc.stats["stored"] >= 3)
    n_before = await store.count_news()
    before = {n.news_id: n.source_count for n in await store.all_news()}
    src = next(s for s in svc.sources if s.name == "coindesk_rss")
    async with httpx.AsyncClient() as client:  # the service closed its own client when it stopped
        assert await SourceRunner([src], svc._raw, client).cycle(src) == 4  # a second poll: the same four items
    await svc._drain()
    assert svc.stats["duplicate"] >= 4
    assert await store.count_news() == n_before
    assert {n.news_id: n.source_count for n in await store.all_news()} == before  # same source: no count change


# --------------------------------------------------------------------------- with a classifier


@respx.mock
async def test_classified_events_reach_store_and_aggregator(store: NewsStore) -> None:
    mock_network()
    clock = FakeClock()
    svc = make_service(store, with_classifier=True, clock=clock)
    await run_until(svc, lambda: svc.stats["classified"] >= 3 and svc.stats["repost"] >= 1)

    events = await store.load_events(T0 - timedelta(days=1))
    assert sorted(e.asset_key for e, _ in events) == ["BTC", "MARKET", "SOL"]
    by_key = {e.asset_key: e for e, _ in events}
    assert by_key["SOL"].label == "BULLISH" and by_key["MARKET"].label == "BEARISH"
    assert by_key["MARKET"].relevance == 0.4 and by_key["SOL"].relevance == 1.0
    assert all(e.model == "test-model@1" and e.ingested_at == T0 for e, _ in events)

    snap = svc.snapshot("SOL-EUR", T0 + 3 * MIN)
    assert snap.as_of == T0 + 3 * MIN
    assert snap.asset.key == "SOL" and snap.asset.n_1h == 1 and snap.asset.ewm > 0.5
    assert snap.asset.last_choice == 2 and snap.asset.last_age_min == 3
    assert snap.market.n_1h == 1 and snap.market.ewm < -0.5
    assert snap.fear_greed is not None and snap.fear_greed.value == 54
    assert snap.fear_greed.age_h == pytest.approx(14.0 + 3 / 60)  # 2026-10-17 00:00 -> 14:03

    # the republication raised the weight of the BTC story: log(1 + 2) instead of log(1 + 1)
    btc = svc.snapshot("BTC-EUR", T0).asset
    conf = by_key["BTC"].confidence
    assert btc.mass == pytest.approx(conf * 1.0 * math.log(3.0), abs=1e-9)


@respx.mock
async def test_service_snapshot_is_point_in_time(store: NewsStore) -> None:
    mock_network()
    svc = make_service(store, with_classifier=True, clock=FakeClock())
    await run_until(svc, lambda: svc.stats["classified"] >= 3)
    before = svc.snapshot("SOL-EUR", T0 - timedelta(milliseconds=1))
    assert before.asset.n_24h == 0 and before.market.n_1h == 0
    assert before.fear_greed is None  # fetched at T0: not visible one millisecond earlier
    assert svc.snapshot("SOL-EUR", T0).asset.n_24h == 1


@respx.mock
async def test_restart_warms_dedup_and_aggregator_from_the_store(store: NewsStore) -> None:
    mock_network()
    clock = FakeClock()
    first = make_service(store, with_classifier=True, clock=clock)
    await run_until(first, lambda: first.stats["classified"] >= 3 and first.stats["repost"] >= 1)
    snap = first.snapshot("BTC-EUR", T0 + 5 * MIN)
    count = await store.count_news()

    second = make_service(store, with_classifier=True, clock=clock)
    await second.prepare()
    assert second.snapshot("BTC-EUR", T0 + 5 * MIN).asset == snap.asset  # same weights, source_count included
    assert second.snapshot("SOL-EUR", T0 + 5 * MIN).market == snap.market
    for it in [item("Solana Foundation announces new validator program", source="coindesk_rss", ingested_at=T0)]:
        assert await second.dedup.admit(it) == "duplicate"  # remembered across the restart
    assert await store.count_news() == count


def test_product_to_key_and_unknown_product(store: NewsStore) -> None:
    assert asset_of("SOL-EUR") == "SOL" and asset_of("BTC-USDC") == "BTC"
    svc = SentimentService(CFG, store, sources=[], clock=FakeClock())
    with pytest.raises(KeyError, match="DOGE"):
        svc.snapshot("DOGE-EUR", T0)
    s = svc.snapshot("ETH-EUR", T0)  # cold start: a valid, empty snapshot
    assert s.asset.n_24h == 0 and s.asset.last_age_min is None and s.fear_greed is None


async def test_a_failing_story_does_not_stop_the_pipeline(store: NewsStore, monkeypatch: pytest.MonkeyPatch) -> None:
    svc = make_service(store, with_classifier=False, clock=FakeClock())
    real_add = store.add_news

    async def flaky(n: Any) -> bool:
        if "Boom" in n.title:
            raise RuntimeError("disk full")
        return await real_add(n)

    monkeypatch.setattr(store, "add_news", flaky)
    await svc.handle(item("Boom Solana", assets=()))
    await svc.handle(item("Fine Solana story"))
    assert svc.stats["errors"] == 1 and svc.stats["stored"] == 1


# -------------------------------------------------------------------------------- the backlog


async def test_backlog_classification_keeps_the_original_ingested_at(store: NewsStore) -> None:
    clock = FakeClock()
    tagger = AssetTagger.from_config(CFG)
    old = [
        item("Solana Foundation announces new validator program", ingested_at=T0, assets=("SOL",)),
        item("SEC Postpones Decision", ingested_at=T0 + 10 * MIN, is_market_wide=True),
    ]
    for it in old:
        await store.add_news(it)

    classifier = OnnxSentimentClassifier(FakeBackend(TABLE), card())
    clock.t = T0 + timedelta(days=3)  # classified three days after the stories entered the system
    n = await classify_backlog(store, classifier, tagger.relevance, batch_size=1, clock=clock)
    assert n == 2 and await store.unclassified(10) == []
    assert await classify_backlog(store, classifier, tagger.relevance) == 0  # re-runnable

    events = {e.asset_key: e for e, _ in await store.load_events(T0 - timedelta(days=1))}
    assert events["SOL"].ingested_at == T0 and events["MARKET"].ingested_at == T0 + 10 * MIN

    # the aggregation a live run would have produced
    svc = SentimentService(CFG, store, classifier=classifier, sources=[], clock=clock)
    clock.t = T0 + 15 * MIN
    await svc.prepare()
    snap = svc.snapshot("SOL-EUR", T0 + 15 * MIN)
    assert snap.asset.n_1h == 1 and snap.asset.last_age_min == 15 and snap.market.n_1h == 1
    assert svc.snapshot("SOL-EUR", T0 + 5 * MIN).market.n_1h == 0  # the macro story is ingested later


async def test_backlog_limit(store: NewsStore) -> None:
    for i in range(5):
        await store.add_news(item(f"Solana story {i}", assets=("SOL",), ingested_at=T0 + i * MIN))
    classifier = OnnxSentimentClassifier(FakeBackend(TABLE), card())
    tagger = AssetTagger.from_config(CFG)
    assert await classify_backlog(store, classifier, tagger.relevance, batch_size=2, limit=3) == 3
    assert len(await store.unclassified(10)) == 2


# ------------------------------------------------------------------------------------ entry point


def app_config(tmp_path: Path, model_dir: Path | None = None) -> AppConfig:
    classifier = ClassifierConfig(
        model_dir=str(model_dir or tmp_path / "no-model"), model_card=str(tmp_path / "no-card.json")
    )
    return AppConfig(
        paths=PathsConfig(news_db=str(tmp_path / "state" / "news.db")),
        sentiment=CFG.model_copy(update={"classifier": classifier}),
    )


@respx.mock
async def test_run_collection_without_onnx_model_stores_news(tmp_path: Path) -> None:
    mock_network()
    cfg = app_config(tmp_path)
    rc = await run_collection(cfg, duration_h=2.0 / 3600, use_classifier=True)  # model missing -> still collects
    assert rc == 0
    store = await NewsStore.open(cfg.paths.news_db)
    try:
        assert await store.count_news() == 3
        assert len(await store.unclassified(10)) == 3
    finally:
        await store.close()


@respx.mock
async def test_run_collection_survives_dead_sources(tmp_path: Path) -> None:
    respx.get(COINDESK).respond(200, content=(FIXTURES / "coindesk.xml").read_bytes())
    respx.get(COINTELEGRAPH).mock(side_effect=httpx.ConnectError("down"))
    respx.get(FNG).respond(500)
    rc = await run_collection(app_config(tmp_path), duration_h=2.0 / 3600, use_classifier=False)
    assert rc == 0
    store = await NewsStore.open(app_config(tmp_path).paths.news_db)
    try:
        assert await store.count_news() == 3  # the live source worked while the others were retrying
    finally:
        await store.close()


def test_resolve_config_falls_back_to_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)  # no config/config.yml here
    assert resolve_config(None) == AppConfig()


def test_utc_helpers_are_consistent() -> None:
    assert T0.tzinfo is UTC and datetime(2026, 10, 17, 14, tzinfo=UTC) == T0
