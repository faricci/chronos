"""Normalisation and de-duplication of headlines (spec sez. 1.1 fase 2, sez. 1.3 punto 3).

``news_id = sha1(normalize_title(title))``. A story seen again is not emitted twice:
- by the SAME source: it is only the feed being polled again (an RSS feed returns its last items on every 200), so
  it is dropped silently;
- by ANOTHER source: it is a republication, which counts once but raises ``source_count`` (a diffusion signal), in
  the database as ``UPDATE news_items SET source_count = source_count + 1``.

Reading of the spec worth knowing: the spec says "su duplicato incrementa source_count" without distinguishing the two
cases; counting every re-poll would make ``source_count`` a measure of how long a story stays in the feed, not of its
diffusion. The memory of which sources already reported a story lives in the cache only, so right after a restart a
republication from a source that had already been counted can be counted once more (see .claude/CONFLITTO-sentiment.md).
"""

from __future__ import annotations

import hashlib
import time
import unicodedata
from collections.abc import Callable, Iterable
from typing import Final, Literal, Protocol

from cachetools import TTLCache

from modules.sentiment.schemas import NewsItem

DEFAULT_MAXSIZE: Final = 50_000

Verdict = Literal["new", "repost", "duplicate"]


def normalize_title(title: str) -> str:
    """Lowercase, punctuation removed, whitespace collapsed (NFKC first, so full-width and no-break spaces fold)."""
    folded = unicodedata.normalize("NFKC", title).lower()
    kept = "".join(c for c in folded if not unicodedata.category(c).startswith("P"))
    return " ".join(kept.split())


def news_id_for(title: str) -> str:
    return hashlib.sha1(normalize_title(title).encode("utf-8")).hexdigest()  # noqa: S324 - an id, not a secret


class SourceCountStore(Protocol):
    async def increment_source_count(self, news_id: str) -> bool:
        """``source_count += 1`` for the story; True if the story is in the database."""
        ...


class Deduplicator:
    """In-memory window of the stories seen in the last ``window_hours`` (LRU bounded by ``maxsize``)."""

    def __init__(
        self,
        store: SourceCountStore | None = None,
        *,
        window_hours: float = 48.0,
        maxsize: int = DEFAULT_MAXSIZE,
        timer: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._seen: TTLCache[str, set[str]] = TTLCache(maxsize=maxsize, ttl=window_hours * 3600.0, timer=timer)

    def __len__(self) -> int:
        return len(self._seen)

    def warm(self, known: Iterable[tuple[str, str]]) -> None:
        """Seed the window with ``(news_id, source)`` pairs already in the store (after a restart)."""
        for news_id, source in known:
            self._seen.setdefault(news_id, set()).add(source)

    def check(self, item: NewsItem) -> Verdict:
        """Classify ``item`` and remember it. Pure bookkeeping, no I/O."""
        sources = self._seen.get(item.news_id)
        if sources is None:
            self._seen[item.news_id] = {item.source}
            return "new"
        if item.source in sources:
            return "duplicate"
        sources.add(item.source)  # in place: a republication must not extend the 48 h window of the story
        return "repost"

    async def admit(self, item: NewsItem) -> Verdict:
        """``check`` plus the ``source_count`` increment in the database for a republication."""
        verdict = self.check(item)
        if verdict == "repost" and self._store is not None:
            await self._store.increment_source_count(item.news_id)
        return verdict
