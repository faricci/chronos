"""Single-key review of the proposed labels (spec sez. 1.1.2, step 3).

``python -m modules.sentiment.label_review``

Reads ``data/labels/headlines_todo.csv`` and the proposals ``headlines_proposed.csv`` (columns ``news_id,label,reason``,
written by the development agent), shows one headline at a time and writes ``headlines.csv`` incrementally.
Keys: Enter = accept the proposal, ``0``/``1``/``2`` = BEARISH/NEUTRAL/BULLISH, ``s`` = skip, ``q`` = quit.
Every answer is appended and flushed at once, so Ctrl+C or a crash loses nothing; running again resumes after the last
answered headline. A skipped headline is written with an EMPTY label (not asked again; the calibration ignores it).
Columns of ``headlines.csv``: ``news_id,source,asset,title,summary,label,proposed``.
"""

from __future__ import annotations

import argparse
import csv
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import readchar

from modules.sentiment.schemas import LABELS

DEFAULT_DIR = Path("data/labels")
OUT_COLUMNS = ("news_id", "source", "asset", "title", "summary", "label", "proposed")
ENTER_KEYS = ("\r", "\n")


@dataclass(slots=True)
class ReviewStats:
    accepted: int = 0
    corrected: int = 0
    skipped: int = 0
    quit_early: bool = False

    @property
    def answered(self) -> int:
        return self.accepted + self.corrected + self.skipped


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def read_proposals(path: Path) -> dict[str, tuple[str, str]]:
    """``news_id -> (label, reason)``. The label column may be called ``label`` or ``proposed_label``."""
    out: dict[str, tuple[str, str]] = {}
    for row in read_rows(path):
        label = (row.get("label") or row.get("proposed_label") or "").strip()
        if label in ("0", "1", "2"):
            out[row["news_id"]] = (label, (row.get("reason") or "").strip())
    return out


def review(
    todo: Sequence[dict[str, str]],
    proposals: dict[str, tuple[str, str]],
    out_path: Path,
    *,
    read_key: Callable[[], str] = readchar.readkey,
    say: Callable[[str], None] = print,
) -> ReviewStats:
    """Interactive loop; ``read_key`` and ``say`` are injected so that it can be tested without a terminal."""
    done = {r["news_id"] for r in read_rows(out_path)}
    pending = [r for r in todo if r["news_id"] not in done]
    stats = ReviewStats()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not out_path.exists() or out_path.stat().st_size == 0
    with out_path.open("a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(OUT_COLUMNS)
            f.flush()
        for i, row in enumerate(pending, 1):
            proposed, reason = proposals.get(row["news_id"], ("", ""))
            say(f"\n[{i}/{len(pending)}] {row['source']} | {row['asset']}")
            say(f"  {row['title']}")
            if row.get("summary"):
                say(f"  {row['summary'][:200]}")
            if proposed:
                say(f"  proposed: {proposed} {LABELS[int(proposed)]} - {reason}")
            say("  [Enter] accept  [0] BEARISH  [1] NEUTRAL  [2] BULLISH  [s] skip  [q] quit")
            label = _ask(read_key, proposed)
            if label is None:
                stats.quit_early = True
                break
            if label == "":
                stats.skipped += 1
            elif label == proposed:
                stats.accepted += 1
            else:
                stats.corrected += 1
            writer.writerow(
                [row["news_id"], row["source"], row["asset"], row["title"], row["summary"], label, proposed]
            )
            f.flush()
    return stats


def _ask(read_key: Callable[[], str], proposed: str) -> str | None:
    """The label chosen ("0".."2"), "" for skip, None for quit. Other keys are ignored."""
    while True:
        try:
            key = read_key()
        except KeyboardInterrupt:
            return None
        if key in ("0", "1", "2"):
            return key
        if key in ENTER_KEYS and proposed:
            return proposed
        if key.lower() == "s":
            return ""
        if key.lower() == "q":
            return None


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Review proposed headline labels with single keys")
    ap.add_argument("--todo", default=str(DEFAULT_DIR / "headlines_todo.csv"))
    ap.add_argument("--proposed", default=str(DEFAULT_DIR / "headlines_proposed.csv"))
    ap.add_argument("--out", default=str(DEFAULT_DIR / "headlines.csv"))
    args = ap.parse_args(argv)
    todo = read_rows(Path(args.todo))
    if not todo:
        print(f"nothing to review: {args.todo} is missing or empty (run modules.sentiment.label_sample first)")
        return 1
    stats = review(todo, read_proposals(Path(args.proposed)), Path(args.out))
    print(
        f"\nanswered {stats.answered}: accepted {stats.accepted}, corrected {stats.corrected}, "
        f"skipped {stats.skipped}{' (stopped early, run again to resume)' if stats.quit_early else ''}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
