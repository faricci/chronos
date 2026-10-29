"""Optional network check: the configured source URLs answer (run manually: ``pytest -m live``)."""

from __future__ import annotations

import pytest

from modules.config import SentimentConfig
from modules.sentiment.sources import FearGreedSource, RssSource, build_sources, make_client


@pytest.mark.live
async def test_default_sources_answer() -> None:
    sources = [s for s in build_sources(SentimentConfig(), env={}) if s.enabled]
    assert sources
    failures: list[str] = []
    async with make_client() as client:
        for s in sources:
            try:
                items = await s.fetch(client)
            except Exception as exc:  # report every dead URL at once
                failures.append(f"{s.name}: {type(exc).__name__}: {exc}")
                continue
            if isinstance(s, RssSource):
                assert items, f"{s.name}: empty feed"
                assert all(i.ingested_at >= i.published_at for i in items if i.published_at), s.name
            if isinstance(s, FearGreedSource):
                assert s.latest() is not None
    assert not failures, "\n".join(failures)
