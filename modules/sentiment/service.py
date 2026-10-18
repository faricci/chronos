"""``SentimentService``: sources -> dedup -> tagger -> store -> classifier -> aggregator (spec sez. 1.1, 1.3 punto 9).

    [SourceRunner tasks] -> raw queue -> admit (dedup) -> tag -> NewsStore.add_news -> tagged queue
                                                                          |
                                         ClassifierWorker (micro-batch, executor) -> NewsStore.add_events
                                                                                  -> SentimentAggregator

The one deviation from the diagram of the spec is deliberate: a story is written to ``news_items`` as soon as it is
tagged, not after the classification. With no ONNX model installed (phase P1 "sentiment live, in raccolta") the
service still collects, and ``python -m modules.sentiment.classify_backlog`` classifies the stored stories later with
their original ``ingested_at``: nothing is lost for the point-in-time view (C7).

Entry point (collection):  ``python -m modules.sentiment.service [--duration-h N] [--config PATH] [--no-classifier]``
runs until the duration expires or Ctrl+C. ``snapshot(product_id, as_of)`` is the read side for the ``StateBuilder``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from concurrent.futures import Executor, ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

import httpx
import structlog

from modules.config import DEFAULT_CONFIG_PATH, AppConfig, SentimentConfig, load_config_or_exit
from modules.sentiment.aggregator import SentimentAggregator
from modules.sentiment.classifier import ClassifierWorker, Clock, SentimentClassifier, try_load_classifier
from modules.sentiment.dedup import Deduplicator
from modules.sentiment.schemas import NewsItem, SentimentEvent, SentimentSnapshot, utc_now
from modules.sentiment.sources import FearGreedSource, Source, SourceRunner, build_sources, make_client
from modules.sentiment.store import NewsStore
from modules.sentiment.tagger import AssetTagger

log = structlog.get_logger(__name__)

QUEUE_MAX: Final = 2_000
STATS_INTERVAL_S: Final = 300.0


def configure_logging() -> None:
    """JSON logs on stderr (spec sez. 1.3: structlog in JSON)."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )


def resolve_config(path: str | None) -> AppConfig:
    """``--config`` if given; else ``config/config.yml`` if it exists; else the defaults (equal to the example)."""
    if path:
        return load_config_or_exit(path)
    if Path(DEFAULT_CONFIG_PATH).is_file():
        return load_config_or_exit(DEFAULT_CONFIG_PATH)
    return AppConfig()


def asset_of(product_id: str) -> str:
    """``SOL-EUR`` -> ``SOL``."""
    return product_id.partition("-")[0]


class SentimentService:
    def __init__(
        self,
        cfg: SentimentConfig,
        store: NewsStore,
        *,
        classifier: SentimentClassifier | None = None,
        sources: Sequence[Source] | None = None,
        client: httpx.AsyncClient | None = None,
        env: Mapping[str, str] | None = None,
        clock: Clock = utc_now,
        executor: Executor | None = None,
        stats_interval_s: float = STATS_INTERVAL_S,
    ) -> None:
        self._cfg = cfg
        self._store = store
        self._classifier = classifier
        self._clock = clock
        self._executor = executor
        self._client = client
        self._stats_interval_s = stats_interval_s
        self.sources: list[Source] = list(sources) if sources is not None else build_sources(cfg, env, clock)
        self.tagger = AssetTagger.from_config(cfg)
        self.dedup = Deduplicator(store, window_hours=cfg.dedup_window_hours)
        self.aggregator = SentimentAggregator(cfg.aggregation)
        self.stats: Counter[str] = Counter()
        self._raw: asyncio.Queue[NewsItem] = asyncio.Queue(QUEUE_MAX)
        self._tagged: asyncio.Queue[NewsItem] = asyncio.Queue(QUEUE_MAX)
        self.runner: SourceRunner | None = None

    # ------------------------------------------------------------------------------- read side

    def snapshot(self, product_id: str, as_of: datetime) -> SentimentSnapshot:
        """Sentiment at ``as_of`` for ``product_id`` (``SOL-EUR`` -> key ``SOL``), from events with
        ``ingested_at <= as_of`` only. Whether to hand ``sentiment: null`` to the model on a cold start or an outage is
        the decision of the ``StateBuilder`` (an empty window shows as ``n_24h == 0`` and ``last_age_min is None``)."""
        key = asset_of(product_id)
        if key not in self._cfg.assets:
            raise KeyError(f"no sentiment asset configured for product {product_id!r} (key {key!r})")
        fear = next((s for s in self.sources if isinstance(s, FearGreedSource)), None)
        return self.aggregator.snapshot(as_of, key, None if fear is None else fear.at(as_of))

    # ------------------------------------------------------------------------------- lifecycle

    async def prepare(self) -> None:
        """Warm-up from the store: the dedup window and the aggregator window."""
        now = self._clock()
        self.dedup.warm(await self._store.recent_news(now - timedelta(hours=self._cfg.dedup_window_hours)))
        loaded = self.aggregator.load(await self._store.load_events(now - self.aggregator.retention))
        log.info("sentiment_prepared", dedup=len(self.dedup), events=loaded)

    async def run(self) -> None:
        await self.prepare()
        own_client = self._client is None
        client = make_client() if self._client is None else self._client
        self.runner = SourceRunner(self.sources, self._raw, client, clock=self._clock)
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self.runner.run(), name="sources")
                tg.create_task(self._pipeline(), name="pipeline")
                tg.create_task(self._heartbeat(), name="heartbeat")
                if self._classifier is not None:
                    worker = ClassifierWorker(
                        self._classifier,
                        self._tagged,
                        self._on_events,
                        self.tagger.relevance,
                        executor=self._executor,
                        clock=self._clock,
                    )
                    tg.create_task(worker.run(), name="classifier")
        finally:
            await self._drain()
            if own_client:
                await client.aclose()

    # ------------------------------------------------------------------------------- pipeline

    async def _pipeline(self) -> None:
        while True:
            await self.handle(await self._raw.get())

    async def _drain(self) -> None:
        """Stories already fetched but not yet processed are stored before stopping (never lost)."""
        while not self._raw.empty():
            await self.handle(self._raw.get_nowait())

    async def handle(self, item: NewsItem) -> None:
        """dedup -> tag -> store -> (classifier queue). Never raises: a bad story must not stop the pipeline."""
        try:
            verdict = await self.dedup.admit(item)
            if verdict == "duplicate":
                self.stats["duplicate"] += 1
                return
            if verdict == "repost":
                self.stats["repost"] += 1
                self.aggregator.register_repost(item.news_id, item.ingested_at)
                return
            tagged = self.tagger.tag(item)
            if tagged is None:
                self.stats["discarded"] += 1
                return
            if not await self._store.add_news(tagged):
                self.stats["already_stored"] += 1
                return
            self.stats["stored"] += 1
            if self._classifier is not None:
                await self._tagged.put(tagged)
        except Exception as exc:
            self.stats["errors"] += 1
            log.error("pipeline_item_failed", source=item.source, error=f"{type(exc).__name__}: {exc}")

    async def _on_events(self, items: Sequence[NewsItem], events: Sequence[SentimentEvent]) -> None:
        await self._store.add_events(events)
        counts = {i.news_id: i.source_count for i in items}
        for e in events:
            self.aggregator.add(e, counts.get(e.news_id, 1))
        self.stats["classified"] += len(items)

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self._stats_interval_s)
            health = {} if self.runner is None else {n: h.status for n, h in self.runner.health.items()}
            log.info(
                "sentiment_stats",
                **self.stats,
                queue_raw=self._raw.qsize(),
                queue_tagged=self._tagged.qsize(),
                sources=health,
            )


# ------------------------------------------------------------------------------------------- CLI


async def run_collection(cfg: AppConfig, *, duration_h: float | None, use_classifier: bool) -> int:
    store = await NewsStore.open(cfg.paths.news_db)
    classifier: SentimentClassifier | None = None
    executor: ThreadPoolExecutor | None = None
    if use_classifier:
        c = cfg.sentiment.classifier
        classifier = try_load_classifier(c.model_dir, c.model_card, max_seq_len=c.max_seq_len)
        if classifier is not None:
            executor = ThreadPoolExecutor(max_workers=c.max_workers, thread_name_prefix="sentiment-onnx")
    if classifier is None:
        log.warning("collecting_without_classifier", hint="run `python -m modules.sentiment.classify_backlog` later")
    service = SentimentService(cfg.sentiment, store, classifier=classifier, executor=executor)
    try:
        async with asyncio.timeout(None if duration_h is None else duration_h * 3600.0):
            await service.run()
    except TimeoutError:
        log.info("duration_elapsed", duration_h=duration_h)
    finally:
        if executor is not None:
            executor.shutdown(wait=False)
        log.info("sentiment_stopped", news_in_db=await store.count_news(), **service.stats)
        await store.close()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Collect crypto news into state/news.db (and classify them if a model exists)"
    )
    ap.add_argument("--duration-h", type=float, default=None, help="stop after N hours (default: until Ctrl+C)")
    ap.add_argument("--config", default=None, help="YAML config (default: config/config.yml if present, else defaults)")
    ap.add_argument("--no-classifier", action="store_true", help="only collect and store; classify later")
    args = ap.parse_args(argv)
    configure_logging()
    cfg = resolve_config(args.config)
    try:
        return asyncio.run(run_collection(cfg, duration_h=args.duration_h, use_classifier=not args.no_classifier))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
