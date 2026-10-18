"""Classify the stories already stored without a sentiment event (spec sez. 1.1, phase P1 "in raccolta").

``python -m modules.sentiment.classify_backlog [--config PATH] [--batch-size N] [--limit N]``

The service stores every tagged story as it arrives, with or without the ONNX model. When the model is installed this
command classifies the stories that have no event yet. The events take the ORIGINAL ``ingested_at`` of each story
(``classify_items`` -> ``build_events``): classifying a week later puts the event exactly where the story entered
the system, so the point-in-time reconstruction (C7) is the same as if the model had been there from the start.
Re-runnable: only stories with no event are picked up, and ``INSERT OR IGNORE`` makes a repeated batch harmless.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from concurrent.futures import Executor, ThreadPoolExecutor

import structlog

from modules.sentiment.classifier import Clock, Relevance, SentimentClassifier, classify_items, try_load_classifier
from modules.sentiment.schemas import utc_now
from modules.sentiment.service import configure_logging, resolve_config
from modules.sentiment.store import NewsStore
from modules.sentiment.tagger import AssetTagger

log = structlog.get_logger(__name__)

DEFAULT_BATCH: int = 64


async def classify_backlog(
    store: NewsStore,
    classifier: SentimentClassifier,
    relevance: Relevance,
    *,
    batch_size: int = DEFAULT_BATCH,
    limit: int | None = None,
    executor: Executor | None = None,
    clock: Clock = utc_now,
) -> int:
    """Classify up to ``limit`` stored stories without events; returns how many stories were classified."""
    done = 0
    while limit is None or done < limit:
        take = batch_size if limit is None else min(batch_size, limit - done)
        items = await store.unclassified(take)
        if not items:
            break
        events = await classify_items(classifier, items, relevance, executor=executor, clock=clock)
        if await store.add_events(events) == 0:
            log.error("backlog_no_progress", n=len(items))  # cannot happen unless the database is read-only
            break
        done += len(items)
        log.info("backlog_progress", classified=done)
    return done


async def _amain(args: argparse.Namespace) -> int:
    cfg = resolve_config(args.config)
    c = cfg.sentiment.classifier
    classifier = try_load_classifier(c.model_dir, c.model_card, max_seq_len=c.max_seq_len)
    if classifier is None:
        print(
            f"No classifier available (model_dir={c.model_dir}, model_card={c.model_card}).\n"
            "Create it with: python -m modules.sentiment.export_model",
        )
        return 1
    store = await NewsStore.open(cfg.paths.news_db)
    executor = ThreadPoolExecutor(max_workers=c.max_workers, thread_name_prefix="sentiment-onnx")
    try:
        n = await classify_backlog(
            store,
            classifier,
            AssetTagger.from_config(cfg.sentiment).relevance,
            batch_size=args.batch_size,
            limit=args.limit,
            executor=executor,
        )
    finally:
        executor.shutdown(wait=True)
        await store.close()
    print(f"classified {n} stories with {classifier.model_name}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Classify stored news that have no sentiment event yet")
    ap.add_argument("--config", default=None)
    ap.add_argument("--batch-size", type=int, default=DEFAULT_BATCH)
    ap.add_argument("--limit", type=int, default=None, help="stop after N stories (default: all)")
    args = ap.parse_args(argv)
    configure_logging()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
