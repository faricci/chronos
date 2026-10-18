"""News sources and their runner (spec sez. 1.1 fase 1, sez. 1.1.1, sez. 1.3 punto 2).

- ``Source``            abstract: ``name``, ``interval_s``, ``fetch(client) -> list[NewsItem]``;
- ``RssSource``         conditional GET (ETag / Last-Modified); 304 returns ``[]``; ``feedparser`` runs in a thread;
- ``CryptoPanicSource`` optional, needs ``auth_token`` from the environment, disabled (never scheduled) without it;
- ``FearGreedSource``   no ``NewsItem``: keeps the readings, ``latest()`` / ``at(as_of)``;
- ``SourceRunner``      one task per source: fetch -> queue -> sleep ``interval_s`` +-10 %. Errors are retried with
                        exponential backoff (``tenacity``, 2..120 s, 5 attempts); after that the source is DEGRADED and
                        retried every 900 s. Any subset of sources keeps the system running.

``ingested_at`` is the instant the HTTP response ARRIVED (taken before parsing), identical for the whole batch: it is
the moment we got to know the story, the only timestamp the point-in-time logic trusts (C7). ``published_at`` is kept
only as information. The tokens of the URLs never reach the logs (httpx messages contain the full URL).
"""

from __future__ import annotations

import asyncio
import os
import random
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal

import feedparser
import httpx
import structlog
from selectolax.lexbor import LexborHTMLParser
from tenacity import AsyncRetrying, RetryCallState, stop_after_attempt, wait_exponential

from modules.config import SentimentConfig, SourceConfig
from modules.sentiment.dedup import news_id_for
from modules.sentiment.schemas import FearGreed, FearGreedSnapshot, NewsItem, utc_now

log = structlog.get_logger(__name__)

SUMMARY_MAX_CHARS: Final = 512
HTTP_TIMEOUT_S: Final = 10.0
JITTER: Final = 0.10
RETRY_ATTEMPTS: Final = 5
BACKOFF_MIN_S: Final = 2.0
BACKOFF_MAX_S: Final = 120.0
DEGRADED_RETRY_S: Final = 900.0
FEAR_GREED_KEEP: Final = 400  # readings kept in memory (the index is daily)
# The free-plan endpoint and its version could not be verified offline (spec sez. 1.1.1: "verificare ... al momento
# dell'implementazione"); ``url`` of the source in the config overrides this default.
CRYPTOPANIC_DEFAULT_URL: Final = "https://cryptopanic.com/api/developer/v2/posts/"

Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]
Status = Literal["OK", "DEGRADED"]


def user_agent() -> str:
    """Explicit User-Agent ``Chronos/2.0 (+contact)``; the contact is ``CHRONOS_CONTACT`` if set."""
    return f"Chronos/2.0 (+{os.environ.get('CHRONOS_CONTACT', 'contact')})"


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=HTTP_TIMEOUT_S, headers={"User-Agent": user_agent()}, follow_redirects=True)


def describe_error(exc: BaseException) -> str:
    """Log-safe description: never ``str(exc)`` of an httpx error, which embeds the URL (and so a token)."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTPStatusError {exc.response.status_code}"
    if isinstance(exc, httpx.HTTPError):
        return type(exc).__name__
    return f"{type(exc).__name__}: {exc}"


class SourceError(Exception):
    """The source answered something that is not what it should be (malformed feed, wrong JSON shape)."""


def strip_html(html: str, max_chars: int = SUMMARY_MAX_CHARS) -> str:
    """Plain text of an HTML fragment (selectolax), whitespace collapsed, cut to ``max_chars``."""
    if not html or not html.strip():
        return ""
    tree = LexborHTMLParser(html)
    tree.strip_tags(["script", "style"])
    return " ".join(tree.text(separator=" ").split())[:max_chars]


def _clean_title(title: str) -> str:
    return " ".join(strip_html(title, max_chars=10_000).split())


def _parse_time(value: Any) -> datetime | None:
    """feedparser ``*_parsed`` (UTC ``struct_time``) -> aware datetime."""
    if value is None:
        return None
    try:
        year, month, day, hour, minute, second = value[:6]
        return datetime(year, month, day, hour, minute, second, tzinfo=UTC)
    except (TypeError, ValueError):
        return None


class Source(ABC):
    name: str
    interval_s: float
    enabled: bool = True

    @abstractmethod
    async def fetch(self, client: httpx.AsyncClient) -> list[NewsItem]: ...


# ---------------------------------------------------------------------------------------------- RSS


class RssSource(Source):
    def __init__(self, name: str, url: str, interval_s: float, clock: Clock = utc_now) -> None:
        self.name, self.url, self.interval_s = name, url, interval_s
        self._clock = clock
        self._etag: str | None = None
        self._last_modified: str | None = None

    async def fetch(self, client: httpx.AsyncClient) -> list[NewsItem]:
        headers: dict[str, str] = {}
        if self._etag:
            headers["If-None-Match"] = self._etag
        if self._last_modified:
            headers["If-Modified-Since"] = self._last_modified
        response = await client.get(self.url, headers=headers)
        received = self._clock()
        if response.status_code == 304:
            return []
        response.raise_for_status()
        parsed = await asyncio.to_thread(feedparser.parse, response.content)
        if not parsed.entries and (parsed.bozo or not parsed.version):
            reason = type(parsed.get("bozo_exception")).__name__ if parsed.bozo else "unknown format"
            raise SourceError(f"{self.name}: not a parsable feed ({reason})")
        items = [i for e in parsed.entries if (i := self._to_item(e, received)) is not None]
        # remembered only after a good parse: a broken answer is fetched again in full
        self._etag = response.headers.get("ETag") or None
        self._last_modified = response.headers.get("Last-Modified") or None
        return items

    def _to_item(self, entry: Mapping[str, Any], received: datetime) -> NewsItem | None:
        title = _clean_title(str(entry.get("title", "")))
        if not title:
            return None
        return NewsItem(
            news_id=news_id_for(title),
            source=self.name,
            url=entry.get("link") or None,
            title=title,
            summary=strip_html(str(entry.get("summary", ""))),
            published_at=_parse_time(entry.get("published_parsed") or entry.get("updated_parsed")),
            ingested_at=received,
        )


# ----------------------------------------------------------------------------------------- CryptoPanic


class CryptoPanicSource(Source):
    """REST JSON; ``currencies[].code`` of each post becomes ``assets`` (the tagger keeps the configured ones)."""

    def __init__(
        self,
        name: str,
        interval_s: float,
        *,
        url: str | None = None,
        token_env: str = "CRYPTOPANIC_TOKEN",
        env: Mapping[str, str] | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self.name, self.interval_s = name, interval_s
        self.url = url or CRYPTOPANIC_DEFAULT_URL
        self._token = (os.environ if env is None else env).get(token_env) or None
        self._clock = clock
        self.enabled = self._token is not None

    async def fetch(self, client: httpx.AsyncClient) -> list[NewsItem]:
        if self._token is None:
            return []
        response = await client.get(self.url, params={"auth_token": self._token, "public": "true", "kind": "news"})
        received = self._clock()
        response.raise_for_status()
        payload = response.json()
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            raise SourceError(f"{self.name}: unexpected JSON shape")
        items: list[NewsItem] = []
        for post in results:
            title = _clean_title(str(post.get("title", "")))
            if not title:
                continue
            codes = [str(c["code"]) for c in post.get("currencies") or [] if isinstance(c, dict) and c.get("code")]
            published = post.get("published_at") or post.get("created_at")
            items.append(
                NewsItem(
                    news_id=news_id_for(title),
                    source=self.name,
                    url=post.get("url") or None,
                    title=title,
                    summary=strip_html(str(post.get("description") or "")),
                    published_at=None if not published else datetime.fromisoformat(published),
                    ingested_at=received,
                    assets=tuple(dict.fromkeys(codes)),
                )
            )
        return items


# ------------------------------------------------------------------------------------------ Fear & Greed


class FearGreedSource(Source):
    """Alternative.me Fear & Greed index. Produces no ``NewsItem``: the readings are kept here.

    A reading is visible from ``fetched_at`` on (point-in-time): ``at(as_of)`` never returns one fetched later.
    """

    def __init__(self, name: str, url: str, interval_s: float, clock: Clock = utc_now) -> None:
        self.name, self.url, self.interval_s = name, url, interval_s
        self._clock = clock
        self._readings: deque[FearGreed] = deque(maxlen=FEAR_GREED_KEEP)

    async def fetch(self, client: httpx.AsyncClient) -> list[NewsItem]:
        response = await client.get(self.url, params={"limit": 1})
        received = self._clock()
        response.raise_for_status()
        try:
            row = response.json()["data"][0]
            reading = FearGreed(
                value=int(row["value"]),
                classification=str(row.get("value_classification", "")),
                timestamp=datetime.fromtimestamp(int(row["timestamp"]), UTC),
                fetched_at=received,
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise SourceError(f"{self.name}: unexpected JSON shape ({type(exc).__name__})") from exc
        last = self._readings[-1] if self._readings else None
        if last is None or (last.timestamp, last.value) != (reading.timestamp, reading.value):
            self._readings.append(reading)  # an unchanged re-read keeps the first ``fetched_at``
        return []

    def latest(self) -> FearGreed | None:
        return self._readings[-1] if self._readings else None

    def at(self, as_of: datetime) -> FearGreedSnapshot | None:
        """The newest reading fetched at or before ``as_of``; ``age_h`` counts from the instant it refers to."""
        for reading in reversed(self._readings):
            if reading.fetched_at <= as_of:
                return FearGreedSnapshot(
                    value=reading.value, age_h=(as_of - reading.timestamp).total_seconds() / 3600.0
                )
        return None


# ------------------------------------------------------------------------------------------------ runner


@dataclass(slots=True)
class SourceHealth:
    status: Status = "OK"
    consecutive_failures: int = 0
    last_success_at: datetime | None = None
    last_error: str | None = None
    items_total: int = 0


class SourceRunner:
    """One task per enabled source: ``fetch -> queue.put -> sleep(interval_s * (1 +- 10 %))``."""

    def __init__(
        self,
        sources: Sequence[Source],
        queue: asyncio.Queue[NewsItem],
        client: httpx.AsyncClient,
        *,
        clock: Clock = utc_now,
        sleep: Sleep = asyncio.sleep,
        rng: random.Random | None = None,
        backoff_min_s: float = BACKOFF_MIN_S,
        backoff_max_s: float = BACKOFF_MAX_S,
        degraded_retry_s: float = DEGRADED_RETRY_S,
    ) -> None:
        self.sources = [s for s in sources if s.enabled]
        for s in sources:
            if not s.enabled:
                log.info("source_disabled", source=s.name)
        self.health: dict[str, SourceHealth] = {s.name: SourceHealth() for s in self.sources}
        self._queue = queue
        self._client = client
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._backoff = (backoff_min_s, backoff_max_s)
        self._degraded_retry_s = degraded_retry_s

    def jitter(self, interval_s: float) -> float:
        return interval_s * (1.0 + self._rng.uniform(-JITTER, JITTER))

    async def run(self) -> None:
        async with asyncio.TaskGroup() as tg:
            for source in self.sources:
                tg.create_task(self.run_source(source), name=f"source:{source.name}")

    async def run_source(self, source: Source) -> None:
        while True:
            await self.cycle(source)
            h = self.health[source.name]
            await self._sleep(self._degraded_retry_s if h.status == "DEGRADED" else self.jitter(source.interval_s))

    async def cycle(self, source: Source) -> int:
        """One fetch (with retries unless DEGRADED); the items go to the queue. Returns how many."""
        h = self.health[source.name]
        attempts = 1 if h.status == "DEGRADED" else RETRY_ATTEMPTS  # a degraded source is probed, not hammered
        try:
            items = await self._fetch_with_retry(source, attempts)
        except Exception as exc:  # the retries are exhausted: this cycle failed
            h.consecutive_failures += 1
            h.last_error = describe_error(exc)
            if h.status == "OK":
                log.warning("source_degraded", source=source.name, error=h.last_error)
            h.status = "DEGRADED"
            return 0
        if h.status == "DEGRADED":
            log.info("source_recovered", source=source.name)
        h.status, h.consecutive_failures, h.last_error, h.last_success_at = "OK", 0, None, self._clock()
        for item in items:
            await self._queue.put(item)
        h.items_total += len(items)
        return len(items)

    async def _fetch_with_retry(self, source: Source, attempts: int) -> list[NewsItem]:
        def log_retry(rs: RetryCallState) -> None:
            exc = rs.outcome.exception() if rs.outcome is not None else None
            log.info(
                "source_retry",
                source=source.name,
                attempt=rs.attempt_number,
                error=None if exc is None else describe_error(exc),
            )

        retrying = AsyncRetrying(
            stop=stop_after_attempt(attempts),
            wait=wait_exponential(min=self._backoff[0], max=self._backoff[1]),
            sleep=self._sleep,
            reraise=True,
            before_sleep=log_retry,
        )
        return await retrying(source.fetch, self._client)


# --------------------------------------------------------------------------------------------- factory


def build_sources(cfg: SentimentConfig, env: Mapping[str, str] | None = None, clock: Clock = utc_now) -> list[Source]:
    """The sources of the config. A kind whose requirements are missing (token) is built disabled."""
    return [_build(sc, env, clock) for sc in cfg.sources]


def _build(sc: SourceConfig, env: Mapping[str, str] | None, clock: Clock) -> Source:
    if sc.kind in ("rss", "fear_greed") and sc.url is None:
        raise ValueError(f"source {sc.name!r} needs a url")  # SourceConfig already rejects it
    if sc.kind == "rss":
        return RssSource(sc.name, str(sc.url), sc.interval_s, clock)
    if sc.kind == "fear_greed":
        return FearGreedSource(sc.name, str(sc.url), sc.interval_s, clock)
    return CryptoPanicSource(
        sc.name, sc.interval_s, url=sc.url, token_env=sc.token_env or "CRYPTOPANIC_TOKEN", env=env, clock=clock
    )
