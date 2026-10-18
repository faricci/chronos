"""Stratified sample of stored headlines for hand labelling (spec sez. 1.1.2, step 1).

``python -m modules.sentiment.label_sample --n 1200``  ->  ``data/labels/headlines_todo.csv``

The sample is stratified by (source, first asset key): the strata are visited round-robin, so every source and asset
is represented even when one of them dominates the feed, and the draw inside a stratum is random but reproducible
(``--seed``). Stories already in ``headlines.csv`` (labelled or skipped) are not offered again.
Columns: ``news_id,source,asset,title,summary``.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import random
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

from modules.sentiment.schemas import NewsItem
from modules.sentiment.service import resolve_config
from modules.sentiment.store import NewsStore

DEFAULT_DIR = Path("data/labels")
TODO_COLUMNS = ("news_id", "source", "asset", "title", "summary")


def stratified_sample(items: Sequence[NewsItem], n: int, seed: int = 0) -> list[NewsItem]:
    """Up to ``n`` stories, drawn round-robin over the (source, first key) strata."""
    strata: dict[tuple[str, str], list[NewsItem]] = defaultdict(list)
    for it in items:
        strata[(it.source, it.keys[0])].append(it)
    rng = random.Random(seed)
    pools = [rng.sample(v, len(v)) for _, v in sorted(strata.items())]  # sorted keys: reproducible order
    out: list[NewsItem] = []
    while len(out) < n and any(pools):
        for pool in pools:
            if pool and len(out) < n:
                out.append(pool.pop())
    return out


def labeled_ids(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    with path.open(encoding="utf-8", newline="") as f:
        return {row["news_id"] for row in csv.DictReader(f)}


def write_todo(path: Path, items: Sequence[NewsItem]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(TODO_COLUMNS)
        for it in items:
            w.writerow([it.news_id, it.source, "+".join(it.keys), it.title, it.summary])


async def _amain(args: argparse.Namespace) -> int:
    cfg = resolve_config(args.config)
    out = Path(args.out)
    if out.exists() and not args.force:
        print(f"{out} already exists (use --force to overwrite it)")
        return 1
    store = await NewsStore.open(cfg.paths.news_db)
    try:
        items = await store.all_news()
    finally:
        await store.close()
    done = labeled_ids(Path(args.labeled))
    sample = stratified_sample([i for i in items if i.news_id not in done], args.n, args.seed)
    write_todo(out, sample)
    print(f"{len(sample)} headlines (of {len(items)} stored, {len(done)} already labelled) -> {out}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Extract a stratified sample of headlines to label")
    ap.add_argument("--n", type=int, default=1200)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default=str(DEFAULT_DIR / "headlines_todo.csv"))
    ap.add_argument("--labeled", default=str(DEFAULT_DIR / "headlines.csv"), help="stories already labelled: excluded")
    ap.add_argument("--config", default=None)
    ap.add_argument("--force", action="store_true")
    return asyncio.run(_amain(ap.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
