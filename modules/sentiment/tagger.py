"""Asset tagging (spec sez. 1.1 fase 3, sez. 1.3 punto 4).

Per asset two regexes with word boundaries:
- the NAMES (``bitcoin``, ``ethereum``) match case-insensitively;
- the TICKERS (``BTC``, ``SOL``) match case-sensitively, upper case only, so ``sol``, ``solar`` and ``console``
  produce no false positive.
A story without assets but with a macro keyword (``sec``, ``etf``, ``fed``...) is tagged ``MARKET``; one with neither
is discarded before the classifier (CPU saving).

Deviation from the literal signature of the spec (``aliases: dict[str, list[str]]``): names and tickers have different
case rules (sez. 1.1), so the aliases are the config's ``AssetAliases`` (``names`` + ``tickers``), not one flat list.

``relevance`` (sez. 1.1.3) is a pure function of the stored story, so it can be recomputed later by the backlog
classifier: 1.0 when the asset is in the title, 0.6 when only in the summary, 0.4 for ``MARKET``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Final

from modules.config import AssetAliases, SentimentConfig
from modules.sentiment.schemas import MARKET_KEY, NewsItem

REL_TITLE: Final = 1.0
REL_SUMMARY: Final = 0.6
REL_MARKET: Final = 0.4


def _alternation(words: Iterable[str]) -> str:
    return "|".join(re.escape(w) for w in sorted(set(words), key=lambda w: (-len(w), w)))


class _AssetPattern:
    """Compiled names + tickers of one asset."""

    def __init__(self, aliases: AssetAliases) -> None:
        self._names = re.compile(rf"\b(?:{_alternation(aliases.names)})\b", re.IGNORECASE) if aliases.names else None
        self._tickers = re.compile(rf"\b(?:{_alternation(aliases.tickers)})\b") if aliases.tickers else None

    def search(self, text: str) -> bool:
        return bool(
            (self._names is not None and self._names.search(text))
            or (self._tickers is not None and self._tickers.search(text))
        )


class AssetTagger:
    def __init__(self, aliases: Mapping[str, AssetAliases], market_keywords: Iterable[str]) -> None:
        self._patterns = {key: _AssetPattern(a) for key, a in aliases.items()}  # config order = output order
        keywords = tuple(market_keywords)
        self._macro = re.compile(rf"\b(?:{_alternation(keywords)})\b", re.IGNORECASE) if keywords else None

    @classmethod
    def from_config(cls, cfg: SentimentConfig) -> AssetTagger:
        return cls(cfg.assets, cfg.market_keywords)

    @property
    def asset_keys(self) -> tuple[str, ...]:
        return tuple(self._patterns)

    def _matches(self, key: str, text: str) -> bool:
        return self._patterns[key].search(text)

    def tag(self, item: NewsItem) -> NewsItem | None:
        """The item with ``assets`` / ``is_market_wide`` filled in, or None if it is not relevant.

        Assets the source already declared (CryptoPanic ``currencies``) are kept when they are configured assets.
        """
        text = f"{item.title}\n{item.summary}"
        declared = set(item.assets)
        assets = tuple(k for k in self._patterns if k in declared or self._matches(k, text))
        if assets:
            return item.model_copy(update={"assets": assets, "is_market_wide": False})
        if self._macro is not None and self._macro.search(text):
            return item.model_copy(update={"assets": (), "is_market_wide": True})
        return None

    def relevance(self, item: NewsItem, key: str) -> float:
        """Weight of the (story, key) pair in the aggregation."""
        if key == MARKET_KEY:
            return REL_MARKET
        # in the title: 1.0. Otherwise 0.6: in the summary, or declared by the source but not found in the text, or an
        # asset removed from the config since the story was stored (a backlog can be older than the config)
        return REL_TITLE if key in self._patterns and self._matches(key, item.title) else REL_SUMMARY
