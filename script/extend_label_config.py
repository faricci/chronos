"""Add products to a ``label_config.json`` without changing anything else, optionally on a COPY of a trained model.

::

    python -m script.extend_label_config --src data/datasets/label_config.json --add XRP-EUR=3.0 \\
        --probe XRP-EUR=2.6 --out data/datasets/label_config_xrp.json [--model-dir <copy of models/baseline/...>]

The barriers, fees, costs and the holdout stay exactly as the feasibility study fixed them; the spread of the new
products is part of the hash, so the file gets a new ``sha256``. With ``--model-dir`` the model card and the tuned
``policy.json`` files of that directory are re-pointed to the new hash (and say where they come from). The boosters
are product-agnostic (the state has no product identity; the spread only enters through its cost block), so the
model of the pool runs on the new product as an out-of-sample product: this is for the dry run, NOT a new study. The
source file and the original model directory are never modified: point ``--model-dir`` at a copy.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

from modules.risk.label_config import extend_label_config, load_label_config


def _pairs(items: Sequence[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for item in items:
        product, _, value = item.partition("=")
        if not product or not value:
            raise SystemExit(f"expected PRODUCT=VALUE, got {item!r}")
        out[product] = float(value)
    return out


def apply_to_model(model_dir: Path, new_label: Path, old_sha: str, new_sha: str) -> list[str]:
    """Re-point a COPY of a model to the extended label_config. Returns the files it changed."""
    card_path = model_dir / "model_card.json"
    card = json.loads(card_path.read_text(encoding="utf-8"))
    if card["label_config"]["sha256"] != old_sha:
        raise SystemExit(f"{card_path}: the model was not trained for the source label_config")
    changed = []
    shutil.copyfile(new_label, model_dir / "label_config.json")
    changed.append("label_config.json")
    card["label_config"]["sha256"] = new_sha
    card["label_config"]["extended_from_sha256"] = old_sha
    card_path.write_text(json.dumps(card, indent=2), encoding="utf-8", newline="\n")
    changed.append("model_card.json")
    for policy in sorted(model_dir.glob("*/policy.json")):
        raw = json.loads(policy.read_text(encoding="utf-8"))
        if raw.get("label_config_sha256") == old_sha:
            raw["label_config_sha256"] = new_sha
            policy.write_text(json.dumps(raw, indent=2), encoding="utf-8", newline="\n")
            changed.append(f"{policy.parent.name}/policy.json")
    return changed


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Extend a label_config.json to more products")
    ap.add_argument("--src", required=True, type=Path)
    ap.add_argument("--add", nargs="+", required=True, metavar="PRODUCT=SPREAD_BPS")
    ap.add_argument("--probe", nargs="*", default=[], metavar="PRODUCT=MEASURED_BPS")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--model-dir", type=Path, default=None, help="a COPY of a trained model to re-point")
    args = ap.parse_args(argv)
    if args.out.resolve() == args.src.resolve():
        raise SystemExit("--out must be a different file from --src")
    label = load_label_config(args.src)
    raw = extend_label_config(label, _pairs(args.add), probe=_pairs(args.probe))
    args.out.write_text(json.dumps(raw, indent=2), encoding="utf-8", newline="\n")
    load_label_config(args.out)  # the new file must load and match its own hash
    print(f"{args.out}: {sorted(raw['spread_bps'])} sha256 {raw['sha256'][:12]} (from {label.sha256[:12]})")
    if args.model_dir is not None:
        print("model re-pointed:", apply_to_model(args.model_dir, args.out, label.sha256, str(raw["sha256"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
