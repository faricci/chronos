"""Sentiment classifier: ONNX int8 encoder -> Choice token {0: BEARISH, 1: NEUTRAL, 2: BULLISH} (sez. 1.1.2, 1.3).

Layers, so that everything except the inference itself runs (and is tested) without the model:
- ``ModelCard``                 ``sentiment_model_card.json``: temperature ``T``, ``calibrated``, ``label_map``;
- ``LogitsBackend``             ``logits(texts) -> (n, 3)`` in the NATIVE order of the model. ``OrtBackend`` is the real
                                one (optimum + onnxruntime + tokenizer, imported lazily); tests inject a fake;
- ``OnnxSentimentClassifier``   ``classify_batch``: logits / T -> softmax -> reorder with ``label_map`` ->
                                ``confidence = 1 - H(p) / ln 3``;
- ``classify_items``/``ClassifierWorker``  the async part: micro-batches of up to 16 stories or 200 ms, ONE
                                ``run_in_executor`` call per batch (the loop never runs inference).

``label_map`` maps a native index of the model to the Choice: ``{"0": 0, "1": 1, "2": 2}`` for cryptobert (checked on
its ``config.json``: id2label 0 Bearish, 1 Neutral, 2 Bullish); FinBERT would need ``{"0": 2, "1": 0, "2": 1}``
(positive, negative, neutral). It must be a permutation of {0, 1, 2}.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
from collections.abc import Awaitable, Callable, Sequence
from concurrent.futures import Executor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal, Protocol

import numpy as np
import numpy.typing as npt
import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator

from modules.sentiment.schemas import LABELS, NewsItem, SentimentEvent, utc_now

log = structlog.get_logger(__name__)

SUMMARY_CHARS: Final = 200  # of the summary that reach the model
BATCH_MAX: Final = 16
BATCH_WAIT_S: Final = 0.2
LN3: Final = math.log(3.0)
CHOICES: Final[tuple[Literal[0, 1, 2], ...]] = (0, 1, 2)

FloatArray = npt.NDArray[np.float64]
Relevance = Callable[[NewsItem, str], float]
Clock = Callable[[], datetime]


class ModelCard(BaseModel):
    """``sentiment_model_card.json``. Extra keys (ECE, Brier, counts written by the calibration) are tolerated."""

    model_config = ConfigDict(frozen=True, extra="allow")

    model_id: str  # e.g. "cryptobert-onnx-int8"
    version: str  # e.g. "2026-10-18": the event's ``model`` is ``model_id@version``
    source_model: str | None = None
    temperature: float = Field(default=1.0, gt=0)
    calibrated: bool = False
    label_map: dict[int, int] = {0: 0, 1: 1, 2: 2}

    @field_validator("label_map")
    @classmethod
    def _permutation(cls, v: dict[int, int]) -> dict[int, int]:
        if sorted(v) != [0, 1, 2] or sorted(v.values()) != [0, 1, 2]:
            raise ValueError("label_map must map the native indices {0, 1, 2} onto the Choices {0, 1, 2} one to one")
        return v

    @property
    def model_name(self) -> str:
        return f"{self.model_id}@{self.version}"


def load_model_card(path: str | Path) -> ModelCard:
    return ModelCard.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


def save_model_card(path: str | Path, card: ModelCard) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(card.model_dump(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def model_text(title: str, summary: str) -> str:
    """The input of the model: ``title + " . " + summary[:200]``."""
    return f"{title} . {summary[:SUMMARY_CHARS]}"


@dataclass(frozen=True, slots=True)
class ClassOutput:
    probs: tuple[float, float, float]  # (p_bear, p_neu, p_bull), Choice order
    choice: Literal[0, 1, 2]
    confidence: float


def softmax(logits: FloatArray) -> FloatArray:
    z = logits - logits.max(axis=1, keepdims=True)
    e = np.exp(z)
    out: FloatArray = e / e.sum(axis=1, keepdims=True)
    return out


def confidence_of(p: FloatArray) -> FloatArray:
    """``1 - H(p) / ln 3`` per row (0 log 0 = 0), clipped to [0, 1]."""
    safe = np.where(p > 0.0, p, 1.0)
    entropy = -(p * np.log(safe)).sum(axis=1)
    out: FloatArray = np.clip(1.0 - entropy / LN3, 0.0, 1.0)
    return out


class LogitsBackend(Protocol):
    def logits(self, texts: Sequence[str]) -> FloatArray:
        """Raw logits ``(len(texts), 3)`` in the native class order of the model."""
        ...


class SentimentClassifier(Protocol):
    model_name: str

    def classify_batch(self, texts: list[str]) -> list[ClassOutput]: ...


class OrtBackend:
    """optimum ``ORTModelForSequenceClassification`` + ``AutoTokenizer`` on onnxruntime (CPU). Heavy imports are lazy:
    a machine without the model stack can still collect news."""

    def __init__(self, model_dir: str | Path, *, max_seq_len: int = 128, intra_op_threads: int | None = None) -> None:
        import onnxruntime as ort
        from optimum.onnxruntime import ORTModelForSequenceClassification
        from transformers import AutoTokenizer

        d = Path(model_dir)
        if not d.is_dir():
            raise FileNotFoundError(f"model directory not found: {d}")
        options = ort.SessionOptions()
        options.intra_op_num_threads = intra_op_threads or max(1, (os.cpu_count() or 2) // 2)
        quantized = d / "model_quantized.onnx"
        self._tokenizer: Any = AutoTokenizer.from_pretrained(str(d))  # type: ignore[no-untyped-call,unused-ignore]
        self._model: Any = ORTModelForSequenceClassification.from_pretrained(
            str(d), session_options=options, **({"file_name": quantized.name} if quantized.is_file() else {})
        )
        self._max_len = max_seq_len

    def logits(self, texts: Sequence[str]) -> FloatArray:
        enc = self._tokenizer(list(texts), padding=True, truncation=True, max_length=self._max_len, return_tensors="np")
        out = self._model(**enc)
        return np.asarray(out.logits, dtype=np.float64)


class OnnxSentimentClassifier:
    def __init__(self, backend: LogitsBackend, card: ModelCard) -> None:
        self._backend = backend
        self.card = card
        self.model_name = card.model_name
        # choice -> native index (the inverse of label_map)
        self._native_of = np.array([n for c, n in sorted((c, n) for n, c in card.label_map.items())], dtype=np.intp)

    @classmethod
    def from_dir(
        cls,
        model_dir: str | Path,
        card: ModelCard,
        *,
        max_seq_len: int = 128,
        intra_op_threads: int | None = None,
    ) -> OnnxSentimentClassifier:
        return cls(OrtBackend(model_dir, max_seq_len=max_seq_len, intra_op_threads=intra_op_threads), card)

    def choice_logits(self, texts: Sequence[str]) -> FloatArray:
        """Raw logits in Choice order (before dividing by T): what the calibration fits."""
        native = np.asarray(self._backend.logits(texts), dtype=np.float64)
        if native.ndim != 2 or native.shape != (len(texts), 3):
            raise ValueError(f"backend returned logits of shape {native.shape}, expected ({len(texts)}, 3)")
        out: FloatArray = native[:, self._native_of]
        return out

    def classify_batch(self, texts: list[str]) -> list[ClassOutput]:
        if not texts:
            return []
        p = softmax(self.choice_logits(texts) / self.card.temperature)
        conf = confidence_of(p)
        outs: list[ClassOutput] = []
        for row, c in zip(p, conf, strict=True):
            probs = (float(row[0]), float(row[1]), float(row[2]))
            outs.append(ClassOutput(probs, CHOICES[int(row.argmax())], float(c)))
        return outs


# ----------------------------------------------------------------------------------------- async part


def build_events(
    item: NewsItem,
    out: ClassOutput,
    *,
    model: str,
    relevance: Relevance,
    classified_at: datetime,
    latency_ms: float,
) -> list[SentimentEvent]:
    """One event per aggregation key of the story. ``ingested_at`` is the ORIGINAL one of the story (C7): classifying
    later, in a backlog, does not move the event in time."""
    probs: dict[Literal["BEARISH", "NEUTRAL", "BULLISH"], float] = {
        "BEARISH": out.probs[0],
        "NEUTRAL": out.probs[1],
        "BULLISH": out.probs[2],
    }
    return [
        SentimentEvent(
            news_id=item.news_id,
            asset_key=key,
            ingested_at=item.ingested_at,
            classified_at=classified_at,
            model=model,
            choice=out.choice,
            label=LABELS[out.choice],
            probabilities=probs,
            confidence=out.confidence,
            relevance=relevance(item, key),
            latency_ms=latency_ms,
        )
        for key in item.keys
    ]


async def classify_items(
    classifier: SentimentClassifier,
    items: Sequence[NewsItem],
    relevance: Relevance,
    *,
    executor: Executor | None = None,
    clock: Clock = utc_now,
) -> list[SentimentEvent]:
    """Classify the stories in ONE call to the executor. ``latency_ms`` = the batch time shared among its stories."""
    if not items:
        return []
    texts = [model_text(i.title, i.summary) for i in items]
    started = time.perf_counter()
    outs = await asyncio.get_running_loop().run_in_executor(executor, classifier.classify_batch, texts)
    latency_ms = (time.perf_counter() - started) * 1000.0 / len(items)
    now = clock()
    events: list[SentimentEvent] = []
    for item, out in zip(items, outs, strict=True):
        events += build_events(
            item, out, model=classifier.model_name, relevance=relevance, classified_at=now, latency_ms=latency_ms
        )
    return events


EventSink = Callable[[Sequence[NewsItem], Sequence[SentimentEvent]], Awaitable[None]]


class ClassifierWorker:
    """Reads tagged stories, groups them (up to ``batch_size`` or ``max_wait_s`` after the first), classifies, hands
    the events to ``sink``. A failed batch is logged and dropped: the stories are already in the store without events,
    and ``classify_backlog`` picks them up."""

    def __init__(
        self,
        classifier: SentimentClassifier,
        queue: asyncio.Queue[NewsItem],
        sink: EventSink,
        relevance: Relevance,
        *,
        executor: Executor | None = None,
        batch_size: int = BATCH_MAX,
        max_wait_s: float = BATCH_WAIT_S,
        clock: Clock = utc_now,
    ) -> None:
        self._classifier = classifier
        self._queue = queue
        self._sink = sink
        self._relevance = relevance
        self._executor = executor
        self._batch_size = batch_size
        self._max_wait_s = max_wait_s
        self._clock = clock

    async def collect(self) -> list[NewsItem]:
        batch = [await self._queue.get()]
        try:
            async with asyncio.timeout(self._max_wait_s):
                while len(batch) < self._batch_size:
                    batch.append(await self._queue.get())
        except TimeoutError:
            pass
        return batch

    async def run(self) -> None:
        while True:
            batch = await self.collect()
            try:
                events = await classify_items(
                    self._classifier, batch, self._relevance, executor=self._executor, clock=self._clock
                )
                await self._sink(batch, events)
            except Exception as exc:
                log.error("classify_batch_failed", n=len(batch), error=f"{type(exc).__name__}: {exc}")


def try_load_classifier(
    model_dir: str | Path, model_card: str | Path, *, max_seq_len: int = 128, intra_op_threads: int | None = None
) -> OnnxSentimentClassifier | None:
    """The real classifier, or None (with a warning) if the card, the model or the stack are not there.

    The collection of news must run without the model: the caller treats None as "store now, classify later".
    """
    try:
        card = load_model_card(model_card)
        return OnnxSentimentClassifier.from_dir(
            model_dir, card, max_seq_len=max_seq_len, intra_op_threads=intra_op_threads
        )
    except Exception as exc:  # missing card/dir/package, bad card, onnxruntime refusing the file: all mean "no model"
        log.warning("classifier_unavailable", model_dir=str(model_dir), error=f"{type(exc).__name__}: {exc}")
        return None
