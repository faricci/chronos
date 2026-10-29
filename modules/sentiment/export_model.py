"""Export a Hugging Face sequence-classification model to ONNX int8 for ``OnnxSentimentClassifier``.

``python -m modules.sentiment.export_model [--source ElKulako/cryptobert] [--revision SHA] [--version YYYY-MM-DD]``

Writes, from the paths of the config:
- ``models/sentiment/cryptobert-onnx-int8/``       tokenizer + ``model_quantized.onnx`` (dynamic int8, AVX2 profile);
- ``models/sentiment/sentiment_model_card.json``   ``T = 1.0``, ``calibrated: false`` (spec sez. 1.1.2) and the
                                                   ``label_map`` DERIVED from the ``id2label`` of the model config.

The export needs the stack ``torch transformers optimum[onnxruntime]`` (an export-time dependency; the runtime of the
classifier needs only onnxruntime, optimum and the tokenizer). The result is neither committed nor shipped by the
repository: it is a ~120 MB artifact in ``models/``.
"""

from __future__ import annotations

import argparse
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from modules.config import ClassifierConfig
from modules.sentiment.classifier import ModelCard, save_model_card

DEFAULT_SOURCE: Final = "ElKulako/cryptobert"
# model id2label name (lower case) -> Choice {0: BEARISH, 1: NEUTRAL, 2: BULLISH}
NAME_TO_CHOICE: Final = {"bearish": 0, "negative": 0, "neutral": 1, "bullish": 2, "positive": 2}


def derive_label_map(id2label: Mapping[int | str, str]) -> dict[int, int]:
    """Native index -> Choice, from the names in the model config. Refuses names it does not know: guessing the
    order of an unknown model would silently invert the sentiment."""
    out: dict[int, int] = {}
    for idx, name in id2label.items():
        choice = NAME_TO_CHOICE.get(name.strip().lower())
        if choice is None:
            raise ValueError(f"unknown class name {name!r} in id2label {dict(id2label)}; set label_map by hand")
        out[int(idx)] = choice
    return out


def export(source: str, revision: str | None, model_dir: Path, card_path: Path, version: str) -> ModelCard:
    from optimum.onnxruntime import ORTModelForSequenceClassification, ORTQuantizer
    from optimum.onnxruntime.configuration import AutoQuantizationConfig
    from transformers import AutoConfig, AutoTokenizer

    label_map = derive_label_map(AutoConfig.from_pretrained(source, revision=revision).id2label)
    model_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="chronos-onnx-fp32-") as tmp:
        ORTModelForSequenceClassification.from_pretrained(source, revision=revision, export=True).save_pretrained(tmp)
        quantizer = ORTQuantizer.from_pretrained(tmp)
        quantizer.quantize(
            save_dir=model_dir, quantization_config=AutoQuantizationConfig.avx2(is_static=False, per_channel=False)
        )
    tokenizer = AutoTokenizer.from_pretrained(source, revision=revision)  # type: ignore[no-untyped-call,unused-ignore]
    tokenizer.save_pretrained(str(model_dir))
    card = ModelCard(
        model_id=f"{source.rsplit('/', 1)[-1]}-onnx-int8",
        version=version,
        source_model=source if revision is None else f"{source}@{revision}",
        temperature=1.0,
        calibrated=False,
        label_map=label_map,
    )
    save_model_card(card_path, card)
    return card


def main(argv: Sequence[str] | None = None) -> int:
    defaults = ClassifierConfig()
    ap = argparse.ArgumentParser(description="Export a Hugging Face classifier to ONNX int8")
    ap.add_argument("--source", default=DEFAULT_SOURCE)
    ap.add_argument("--revision", default=None, help="commit SHA of the model repository (reproducible export)")
    ap.add_argument("--version", default=datetime.now(UTC).strftime("%Y-%m-%d"))
    ap.add_argument("--model-dir", default=defaults.model_dir)
    ap.add_argument("--model-card", default=defaults.model_card)
    args = ap.parse_args(argv)
    card = export(args.source, args.revision, Path(args.model_dir), Path(args.model_card), args.version)
    print(f"exported {card.model_name} -> {args.model_dir}; card {args.model_card} (T=1.0, calibrated=false)")
    print(f"label_map (native index -> Choice): {card.label_map}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
