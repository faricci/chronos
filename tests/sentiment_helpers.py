"""Builders shared by the ``tests/test_sentiment_*`` modules (no test inside)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import numpy as np

from modules.sentiment.classifier import FloatArray, ModelCard
from modules.sentiment.dedup import news_id_for
from modules.sentiment.schemas import LABELS, NewsItem, SentimentEvent

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sentiment"
T0 = datetime(2026, 10, 17, 14, 0, 0, tzinfo=UTC)
Choice = Literal[0, 1, 2]


class FakeClock:
    def __init__(self, t: datetime = T0) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw: float) -> None:
        self.t += timedelta(**kw)


def item(
    title: str = "Solana Foundation announces new validator program",
    *,
    source: str = "coindesk_rss",
    ingested_at: datetime = T0,
    summary: str = "",
    assets: Sequence[str] = (),
    is_market_wide: bool = False,
    source_count: int = 1,
) -> NewsItem:
    return NewsItem(
        news_id=news_id_for(title),
        source=source,
        url=f"https://example.com/{news_id_for(title)[:8]}",
        title=title,
        summary=summary,
        published_at=ingested_at - timedelta(minutes=2),
        ingested_at=ingested_at,
        source_count=source_count,
        assets=tuple(assets),
        is_market_wide=is_market_wide,
    )


def event(
    *,
    key: str = "SOL",
    ingested_at: datetime = T0,
    probs: tuple[float, float, float] = (0.1, 0.2, 0.7),
    confidence: float = 0.5,
    relevance: float = 1.0,
    news_id: str | None = None,
    model: str = "test-model@1",
) -> SentimentEvent:
    choice: Choice = (0, 1, 2)[int(np.argmax(probs))]  # type: ignore[assignment]
    return SentimentEvent(
        news_id=news_id or f"{key}-{ingested_at.isoformat()}-{probs}",
        asset_key=key,
        ingested_at=ingested_at,
        classified_at=ingested_at,
        model=model,
        choice=choice,
        label=LABELS[choice],
        probabilities={"BEARISH": probs[0], "NEUTRAL": probs[1], "BULLISH": probs[2]},
        confidence=confidence,
        relevance=relevance,
        latency_ms=1.0,
    )


class FakeBackend:
    """Stands for ORT: returns logits in the NATIVE order of the (fake) model, keyed by keyword in the text."""

    def __init__(self, table: dict[str, tuple[float, float, float]] | None = None) -> None:
        self.table = table or {}
        self.calls: list[list[str]] = []

    def logits(self, texts: Sequence[str]) -> FloatArray:
        self.calls.append(list(texts))
        rows = []
        for t in texts:
            row = (0.0, 0.0, 0.0)
            for kw, logits in self.table.items():
                if kw in t:
                    row = logits
                    break
            rows.append(row)
        return np.asarray(rows, dtype=np.float64)


def card(**kw: object) -> ModelCard:
    base: dict[str, object] = {"model_id": "test-model", "version": "1"}
    base.update(kw)
    return ModelCard.model_validate(base)
