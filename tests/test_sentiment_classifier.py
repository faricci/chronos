"""OnnxSentimentClassifier (spec sez. 1.3 punto 5): ORT is mocked, the maths and the plumbing are real."""

from __future__ import annotations

import asyncio
import json
import math
import sys
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from pydantic import ValidationError

from modules.config import SentimentConfig
from modules.sentiment.classifier import (
    ClassifierWorker,
    ClassOutput,
    ModelCard,
    OnnxSentimentClassifier,
    OrtBackend,
    build_events,
    classify_items,
    load_model_card,
    model_text,
    save_model_card,
    try_load_classifier,
)
from modules.sentiment.schemas import NewsItem, SentimentEvent
from modules.sentiment.tagger import AssetTagger
from tests.sentiment_helpers import T0, FakeBackend, FakeClock, card, item

LN3 = math.log(3.0)


def clf(table: dict[str, tuple[float, float, float]] | None = None, **card_kw: object) -> OnnxSentimentClassifier:
    return OnnxSentimentClassifier(FakeBackend(table), card(**card_kw))


# ----------------------------------------------------------------------------------- the maths


def test_probabilities_sum_to_one_and_choice_is_argmax() -> None:
    c = clf({"up": (0.2, 0.5, 3.0), "down": (2.5, 0.1, -1.0), "flat": (0.0, 1.0, 0.0)})
    outs = c.classify_batch(["up", "down", "flat"])
    for o in outs:
        assert sum(o.probs) == pytest.approx(1.0, abs=1e-12)
        assert o.choice == int(np.argmax(o.probs))
        assert 0.0 <= o.confidence <= 1.0
    assert [o.choice for o in outs] == [2, 0, 1]


def test_confidence_is_one_minus_normalised_entropy() -> None:
    uniform = clf({"u": (1.0, 1.0, 1.0)}).classify_batch(["u"])[0]
    assert uniform.confidence == pytest.approx(0.0, abs=1e-12)
    peaked = clf({"p": (0.0, 0.0, 50.0)}).classify_batch(["p"])[0]
    assert peaked.confidence == pytest.approx(1.0, abs=1e-9) and peaked.probs[2] == pytest.approx(1.0)
    p = np.exp([0.5, 0.0, 1.5]) / np.exp([0.5, 0.0, 1.5]).sum()
    expected = 1.0 + float((p * np.log(p)).sum()) / LN3
    got = clf({"x": (0.5, 0.0, 1.5)}).classify_batch(["x"])[0]
    assert got.confidence == pytest.approx(expected, abs=1e-12)


def test_temperature_divides_the_logits_before_the_softmax() -> None:
    z = (0.5, 0.0, 1.5)
    hot = clf({"x": z}, temperature=4.0).classify_batch(["x"])[0]
    cold = clf({"x": z}, temperature=1.0).classify_batch(["x"])[0]
    zz = np.array(z) / 4.0
    ref = np.exp(zz) / np.exp(zz).sum()
    assert hot.probs == pytest.approx(tuple(ref), abs=1e-12)
    assert hot.confidence < cold.confidence  # T > 1 flattens


def test_no_overflow_with_huge_logits() -> None:
    o = clf({"x": (1e4, -1e4, 0.0)}).classify_batch(["x"])[0]
    assert o.probs == pytest.approx((1.0, 0.0, 0.0)) and o.choice == 0 and not math.isnan(o.confidence)


def test_empty_batch() -> None:
    assert clf().classify_batch([]) == []


# ------------------------------------------------------------------------------ label remapping


def test_identity_label_map_for_cryptobert_order() -> None:
    # native order = (Bearish, Neutral, Bullish)
    o = clf({"x": (9.0, 0.0, 0.0)}, label_map={0: 0, 1: 1, 2: 2}).classify_batch(["x"])[0]
    assert o.choice == 0 and o.probs[0] > 0.99


def test_finbert_label_map_reorders_the_classes() -> None:
    # FinBERT native order = (positive, negative, neutral) -> Choice (BULLISH=2, BEARISH=0, NEUTRAL=1)
    finbert = {0: 2, 1: 0, 2: 1}
    table = {"pos": (9.0, 0.0, 0.0), "neg": (0.0, 9.0, 0.0), "neu": (0.0, 0.0, 9.0)}
    outs = clf(table, label_map=finbert).classify_batch(["pos", "neg", "neu"])
    assert [o.choice for o in outs] == [2, 0, 1]
    # the probability vector is in Choice order (bear, neu, bull), not in native order
    assert outs[0].probs[2] > 0.99 and outs[1].probs[0] > 0.99 and outs[2].probs[1] > 0.99


def test_mixed_distribution_is_reordered_not_just_argmaxed() -> None:
    finbert = {0: 2, 1: 0, 2: 1}
    native = np.array([[2.0, 1.0, 0.0]])  # pos, neg, neu
    o = clf({"x": (2.0, 1.0, 0.0)}, label_map=finbert).classify_batch(["x"])[0]
    sm = np.exp(native[0]) / np.exp(native[0]).sum()
    assert o.probs == pytest.approx((sm[1], sm[2], sm[0]), abs=1e-12)


@pytest.mark.parametrize("bad", [{0: 0, 1: 0, 2: 1}, {0: 0, 1: 1}, {0: 0, 1: 1, 3: 2}, {0: 0, 1: 1, 2: 5}])
def test_label_map_must_be_a_permutation(bad: dict[int, int]) -> None:
    with pytest.raises(ValidationError):
        card(label_map=bad)


def test_backend_with_wrong_shape_is_refused() -> None:
    class Bad:
        def logits(self, texts: Any) -> Any:
            return np.zeros((len(texts), 2))

    with pytest.raises(ValueError, match="shape"):
        OnnxSentimentClassifier(Bad(), card()).classify_batch(["a"])


# ------------------------------------------------------------------------------------ model card


def test_model_card_roundtrip_and_name(tmp_path: Path) -> None:
    c = card(model_id="cryptobert-onnx-int8", version="2026-10-01", temperature=1.7, calibrated=True, ece_after=0.05)
    assert c.model_name == "cryptobert-onnx-int8@2026-10-01"
    p = tmp_path / "m" / "card.json"
    save_model_card(p, c)
    back = load_model_card(p)
    assert back == c and back.temperature == 1.7 and back.model_extra == {"ece_after": 0.05}
    assert json.loads(p.read_text())["label_map"] == {"0": 0, "1": 1, "2": 2}


def test_card_defaults_are_the_local_phase_defaults() -> None:
    c = ModelCard(model_id="m", version="1")
    assert c.temperature == 1.0 and c.calibrated is False


def test_card_rejects_nonpositive_temperature() -> None:
    with pytest.raises(ValidationError):
        card(temperature=0)


def test_try_load_classifier_returns_none_when_nothing_is_installed(tmp_path: Path) -> None:
    assert try_load_classifier(tmp_path / "no-model", tmp_path / "no-card.json") is None
    save_model_card(tmp_path / "card.json", card())
    assert try_load_classifier(tmp_path / "no-model", tmp_path / "card.json") is None  # card ok, model dir missing


# ------------------------------------------------------------------------- the ORT adapter (mocked)


def test_ort_backend_uses_tokenizer_limits_and_session_threads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class FakeTokenizer:
        @classmethod
        def from_pretrained(cls, path: str) -> FakeTokenizer:
            seen["tokenizer_path"] = path
            return cls()

        def __call__(self, texts: list[str], **kw: Any) -> dict[str, Any]:
            seen["tok_kwargs"] = kw
            seen["texts"] = texts
            return {"input_ids": np.zeros((len(texts), 4), dtype=np.int64)}

    class FakeModel:
        @classmethod
        def from_pretrained(cls, path: str, **kw: Any) -> FakeModel:
            seen["model_kwargs"] = kw
            return cls()

        def __call__(self, **enc: Any) -> Any:
            n = enc["input_ids"].shape[0]
            return types.SimpleNamespace(logits=np.tile(np.array([[1.0, 2.0, 3.0]], dtype=np.float32), (n, 1)))

    class FakeOptions:
        intra_op_num_threads = 0

    monkeypatch.setitem(sys.modules, "onnxruntime", types.SimpleNamespace(SessionOptions=FakeOptions))
    monkeypatch.setitem(
        sys.modules, "optimum.onnxruntime", types.SimpleNamespace(ORTModelForSequenceClassification=FakeModel)
    )
    monkeypatch.setitem(sys.modules, "transformers", types.SimpleNamespace(AutoTokenizer=FakeTokenizer))
    (tmp_path / "model_quantized.onnx").write_bytes(b"")

    backend = OrtBackend(tmp_path, max_seq_len=64, intra_op_threads=3)
    out = backend.logits(["a", "b"])
    assert out.shape == (2, 3) and out.dtype == np.float64
    assert seen["tok_kwargs"] == {"padding": True, "truncation": True, "max_length": 64, "return_tensors": "np"}
    assert seen["model_kwargs"]["session_options"].intra_op_num_threads == 3
    assert seen["model_kwargs"]["file_name"] == "model_quantized.onnx"

    # and the full classifier on top of it
    c = OnnxSentimentClassifier(backend, card())
    assert c.classify_batch(["a"])[0].choice == 2


def test_ort_backend_missing_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "onnxruntime", types.SimpleNamespace(SessionOptions=object))
    monkeypatch.setitem(
        sys.modules, "optimum.onnxruntime", types.SimpleNamespace(ORTModelForSequenceClassification=object)
    )
    monkeypatch.setitem(sys.modules, "transformers", types.SimpleNamespace(AutoTokenizer=object))
    with pytest.raises(FileNotFoundError):
        OrtBackend(tmp_path / "missing")


# ---------------------------------------------------------------------------- events and worker


def test_model_text_is_title_dot_summary_cut_at_200() -> None:
    assert model_text("Title", "S" * 500) == "Title . " + "S" * 200
    assert model_text("Title", "") == "Title . "


def test_build_events_one_per_key_with_original_ingested_at() -> None:
    it = item("SOL and BTC rally", summary="x", assets=("BTC", "SOL"), ingested_at=T0)
    tagger = AssetTagger.from_config(SentimentConfig())
    out = ClassOutput((0.1, 0.2, 0.7), 2, 0.4)
    later = T0.replace(hour=20)
    events = build_events(it, out, model="m@1", relevance=tagger.relevance, classified_at=later, latency_ms=3.0)
    assert [e.asset_key for e in events] == ["BTC", "SOL"]
    assert all(e.ingested_at == T0 and e.classified_at == later for e in events)  # C7: not moved by classifying late
    assert all(isinstance(e, SentimentEvent) and e.label == "BULLISH" and e.model == "m@1" for e in events)
    assert [e.relevance for e in events] == [1.0, 1.0]
    [m] = build_events(item("SEC delays", is_market_wide=True), out, model="m", relevance=tagger.relevance,
                       classified_at=later, latency_ms=0.0)  # fmt: skip
    assert m.asset_key == "MARKET" and m.relevance == 0.4


async def test_classify_items_runs_one_call_in_the_executor() -> None:
    backend = FakeBackend({"good": (0.0, 0.0, 5.0), "bad": (5.0, 0.0, 0.0)})
    c = OnnxSentimentClassifier(backend, card())
    items = [item("good news SOL", assets=("SOL",)), item("bad news ETH", assets=("ETH",))]
    with ThreadPoolExecutor(max_workers=1) as ex:
        events = await classify_items(c, items, lambda i, k: 1.0, executor=ex, clock=FakeClock())
    assert len(backend.calls) == 1 and len(backend.calls[0]) == 2  # a single batch
    assert [(e.asset_key, e.choice) for e in events] == [("SOL", 2), ("ETH", 0)]
    assert all(e.latency_ms is not None and e.latency_ms >= 0 for e in events)
    assert await classify_items(c, [], lambda i, k: 1.0) == []


async def test_worker_batches_up_to_16_items(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = FakeBackend()
    c = OnnxSentimentClassifier(backend, card())
    q: asyncio.Queue[NewsItem] = asyncio.Queue()
    got: list[int] = []

    async def sink(items: Any, events: Any) -> None:
        got.append(len(items))

    for i in range(40):
        q.put_nowait(item(f"story {i} SOL", assets=("SOL",)))
    worker = ClassifierWorker(c, q, sink, lambda i, k: 1.0, max_wait_s=0.05)
    task = asyncio.create_task(worker.run())
    await asyncio.sleep(0.4)
    task.cancel()
    assert got == [16, 16, 8]
    assert [len(call) for call in backend.calls] == [16, 16, 8]


async def test_worker_flushes_a_partial_batch_after_the_wait() -> None:
    backend = FakeBackend()
    q: asyncio.Queue[NewsItem] = asyncio.Queue()
    got: list[int] = []

    async def sink(items: Any, events: Any) -> None:
        got.append(len(items))

    worker = ClassifierWorker(OnnxSentimentClassifier(backend, card()), q, sink, lambda i, k: 1.0, max_wait_s=0.05)
    task = asyncio.create_task(worker.run())
    for i in range(3):
        q.put_nowait(item(f"early {i}", assets=("SOL",)))
    await asyncio.sleep(0.25)
    q.put_nowait(item("late one", assets=("SOL",)))
    await asyncio.sleep(0.25)
    task.cancel()
    assert got == [3, 1]


async def test_worker_survives_a_failing_batch() -> None:
    class Flaky:
        model_name = "flaky@1"
        calls = 0

        def classify_batch(self, texts: list[str]) -> list[ClassOutput]:
            Flaky.calls += 1
            if Flaky.calls == 1:
                raise RuntimeError("onnx exploded")
            return [ClassOutput((0.0, 0.0, 1.0), 2, 1.0) for _ in texts]

    q: asyncio.Queue[NewsItem] = asyncio.Queue()
    sunk: list[int] = []

    async def sink(items: Any, events: Any) -> None:
        sunk.append(len(events))

    task = asyncio.create_task(ClassifierWorker(Flaky(), q, sink, lambda i, k: 1.0, max_wait_s=0.02).run())
    q.put_nowait(item("first", assets=("SOL",)))
    await asyncio.sleep(0.15)
    q.put_nowait(item("second", assets=("SOL",)))
    await asyncio.sleep(0.15)
    task.cancel()
    assert sunk == [1]  # the first batch was dropped (it stays in the store for the backlog), the worker went on


# ------------------------------------------------------------------- the real exported model (optional)

REAL_DIR = Path("models/sentiment/cryptobert-onnx-int8")
REAL_CARD = Path("models/sentiment/sentiment_model_card.json")


@pytest.mark.skipif(
    not (REAL_DIR.is_dir() and REAL_CARD.is_file()), reason="ONNX model not exported (see export_model)"
)
def test_real_model_smoke() -> None:
    c = try_load_classifier(REAL_DIR, REAL_CARD)
    assert c is not None
    bull, bear = c.classify_batch(
        [
            "Bitcoin surges to a new all-time high as ETF inflows hit a record . Buyers pile in",
            "Exchange hacked, hundreds of millions stolen . Users panic and withdraw funds",
        ]
    )
    assert bear.choice == 0
    assert bull.probs[2] > bear.probs[2] and bull.probs[0] < bear.probs[0]
    assert sum(bull.probs) == pytest.approx(1.0)
