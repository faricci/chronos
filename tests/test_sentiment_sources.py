"""Sources and SourceRunner (spec sez. 1.1.1, 1.3 punto 2). Local fixtures only: no network."""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from modules.config import SentimentConfig
from modules.sentiment.dedup import news_id_for
from modules.sentiment.schemas import NewsItem
from modules.sentiment.sources import (
    CryptoPanicSource,
    FearGreedSource,
    RssSource,
    SourceError,
    SourceRunner,
    build_sources,
    describe_error,
    strip_html,
    user_agent,
)
from tests.sentiment_helpers import FIXTURES, T0, FakeClock

COINDESK = "https://www.coindesk.com/arc/outboundfeeds/rss/"
XML = (FIXTURES / "coindesk.xml").read_bytes()


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as c:
        yield c


# ------------------------------------------------------------------------------------------- RSS


@respx.mock
async def test_rss_parses_items(client: httpx.AsyncClient) -> None:
    respx.get(COINDESK).respond(200, content=XML)
    clock = FakeClock(T0 + timedelta(microseconds=999))
    items = await RssSource("coindesk_rss", COINDESK, 45, clock).fetch(client)
    assert [i.title for i in items] == [
        "Solana Foundation announces new validator program",
        "Bitcoin Tops $120,000 as ETF Inflows Accelerate",
        "SEC Postpones Decision on Crypto Rules",
        "Best Pizza Places in Brooklyn",
    ]
    first = items[0]
    assert first.source == "coindesk_rss"
    assert first.news_id == news_id_for(first.title)
    assert first.published_at == datetime(2026, 10, 17, 13, 58, 41, tzinfo=UTC)
    assert first.ingested_at == T0  # the arrival time of the response, millisecond precision; not published_at
    assert first.url == "https://www.coindesk.com/tech/2026/10/17/solana-validator-program/"
    assert first.assets == () and first.source_count == 1 and not first.is_market_wide


@respx.mock
async def test_rss_summary_is_stripped_and_truncated(client: httpx.AsyncClient) -> None:
    respx.get(COINDESK).respond(200, content=XML)
    items = await RssSource("coindesk_rss", COINDESK, 45).fetch(client)
    assert items[0].summary == "The Solana Foundation said on Friday it will fund & support new validators."
    long_xml = (
        b"<rss version='2.0'><channel><item><title>T</title><description>" + b"x" * 2000 + b"</description></item>"
        b"</channel></rss>"
    )
    respx.get(COINDESK).respond(200, content=long_xml)
    [long_item] = await RssSource("s", COINDESK, 45).fetch(client)
    assert len(long_item.summary) == 512


@respx.mock
async def test_rss_conditional_get_and_304(client: httpx.AsyncClient) -> None:
    route = respx.get(COINDESK)
    route.side_effect = [
        httpx.Response(200, content=XML, headers={"ETag": '"abc"', "Last-Modified": "Fri, 17 Oct 2026 14:00:00 GMT"}),
        httpx.Response(304),
        httpx.Response(200, content=XML, headers={"ETag": '"def"'}),
    ]
    src = RssSource("coindesk_rss", COINDESK, 45)
    assert len(await src.fetch(client)) == 4
    assert "if-none-match" not in route.calls[0].request.headers  # nothing to send the first time

    assert await src.fetch(client) == []  # 304
    second = route.calls[1].request.headers
    assert second["if-none-match"] == '"abc"'
    assert second["if-modified-since"] == "Fri, 17 Oct 2026 14:00:00 GMT"

    assert len(await src.fetch(client)) == 4  # changed: full body again
    third = route.calls[2].request.headers
    assert third["if-none-match"] == '"abc"'  # the validators of the 304 round are still the last received


@respx.mock
async def test_rss_http_error_raises_and_keeps_validators(client: httpx.AsyncClient) -> None:
    route = respx.get(COINDESK)
    route.side_effect = [httpx.Response(200, content=XML, headers={"ETag": '"abc"'}), httpx.Response(503)]
    src = RssSource("coindesk_rss", COINDESK, 45)
    await src.fetch(client)
    with pytest.raises(httpx.HTTPStatusError):
        await src.fetch(client)
    route.side_effect = [httpx.Response(304)]
    assert await src.fetch(client) == []
    assert route.calls[2].request.headers["if-none-match"] == '"abc"'


@respx.mock
async def test_rss_garbage_is_a_source_error_and_not_remembered(client: httpx.AsyncClient) -> None:
    respx.get(COINDESK).respond(200, content=b"<html>maintenance</html>", headers={"ETag": '"bad"'})
    src = RssSource("coindesk_rss", COINDESK, 45)
    with pytest.raises(SourceError):
        await src.fetch(client)
    assert src._etag is None  # a broken answer must be fetched again in full


@respx.mock
async def test_rss_skips_entries_without_title(client: httpx.AsyncClient) -> None:
    body = (
        b"<rss version='2.0'><channel><item><description>no title</description></item>"
        b"<item><title>Ok</title></item></channel></rss>"
    )
    respx.get(COINDESK).respond(200, content=body)
    assert [i.title for i in await RssSource("s", COINDESK, 45).fetch(client)] == ["Ok"]


def test_strip_html() -> None:
    assert strip_html("<p>a <b>b</b></p><script>x()</script><style>p{}</style>") == "a b"
    assert strip_html("") == "" and strip_html("   ") == ""
    assert strip_html("Tom &amp; Jerry") == "Tom & Jerry"
    assert strip_html("abcdef", max_chars=3) == "abc"


# ------------------------------------------------------------------------------------- CryptoPanic


def test_cryptopanic_is_disabled_without_token() -> None:
    assert CryptoPanicSource("cryptopanic", 120, env={}).enabled is False
    assert CryptoPanicSource("cryptopanic", 120, env={"CRYPTOPANIC_TOKEN": "t"}).enabled is True


@respx.mock
async def test_cryptopanic_maps_currencies_to_assets(client: httpx.AsyncClient) -> None:
    url = "https://cryptopanic.test/api/posts/"
    route = respx.get(url).respond(200, json=json.loads((FIXTURES / "cryptopanic.json").read_text()))
    src = CryptoPanicSource("cryptopanic", 120, url=url, env={"CRYPTOPANIC_TOKEN": "secret"}, clock=FakeClock())
    eth, fed = await src.fetch(client)
    assert route.calls[0].request.url.params["auth_token"] == "secret"
    assert eth.assets == ("ETH", "XYZ")  # the tagger will keep only the configured ones
    assert eth.published_at == datetime(2026, 10, 17, 13, 0, tzinfo=UTC) and eth.ingested_at == T0
    assert fed.assets == () and fed.published_at == datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


@respx.mock
async def test_cryptopanic_bad_shape(client: httpx.AsyncClient) -> None:
    respx.get("https://cryptopanic.test/p").respond(200, json={"detail": "nope"})
    src = CryptoPanicSource("cp", 120, url="https://cryptopanic.test/p", env={"CRYPTOPANIC_TOKEN": "t"})
    with pytest.raises(SourceError):
        await src.fetch(client)


@respx.mock
async def test_token_never_reaches_the_error_description(client: httpx.AsyncClient) -> None:
    respx.get("https://cryptopanic.test/p").respond(401)
    src = CryptoPanicSource("cp", 120, url="https://cryptopanic.test/p", env={"CRYPTOPANIC_TOKEN": "TOPSECRET"})
    with pytest.raises(httpx.HTTPStatusError) as ei:
        await src.fetch(client)
    assert "TOPSECRET" in str(ei.value)  # httpx does put the URL in its message...
    assert "TOPSECRET" not in describe_error(ei.value)  # ...which is why the logs use this instead
    assert describe_error(ei.value) == "HTTPStatusError 401"


# ------------------------------------------------------------------------------------- Fear & Greed

FNG = "https://api.alternative.me/fng/"


@respx.mock
async def test_fear_greed_reading_is_point_in_time(client: httpx.AsyncClient) -> None:
    respx.get(FNG).respond(200, json=json.loads((FIXTURES / "fear_greed.json").read_text()))
    clock = FakeClock(T0)
    src = FearGreedSource("fear_greed", FNG, 3600, clock)
    assert src.latest() is None
    assert await src.fetch(client) == []  # produces no NewsItem
    latest = src.latest()
    assert latest is not None and latest.value == 54 and latest.classification == "Neutral"
    assert latest.timestamp == datetime(2026, 10, 17, tzinfo=UTC) and latest.fetched_at == T0
    assert src.at(T0 - timedelta(seconds=1)) is None  # not fetched yet at that time
    snap = src.at(T0 + timedelta(hours=1))
    assert snap is not None and snap.value == 54 and snap.age_h == pytest.approx(15.0)  # 00:00 -> 15:00


@respx.mock
async def test_fear_greed_unchanged_reading_keeps_the_first_fetched_at(client: httpx.AsyncClient) -> None:
    respx.get(FNG).respond(200, json=json.loads((FIXTURES / "fear_greed.json").read_text()))
    clock = FakeClock(T0)
    src = FearGreedSource("fear_greed", FNG, 3600, clock)
    await src.fetch(client)
    clock.advance(hours=1)
    await src.fetch(client)
    latest = src.latest()
    assert latest is not None and latest.fetched_at == T0
    assert src.at(T0) is not None


@respx.mock
async def test_fear_greed_bad_shape(client: httpx.AsyncClient) -> None:
    respx.get(FNG).respond(200, json={"data": []})
    with pytest.raises(SourceError):
        await FearGreedSource("fg", FNG, 3600).fetch(client)


# ---------------------------------------------------------------------------------------- factory


def test_build_sources_from_default_config() -> None:
    sources = build_sources(SentimentConfig(), env={})
    assert [s.name for s in sources] == [
        "coindesk_rss",
        "cointelegraph_rss",
        "decrypt_rss",
        "cryptoslate_rss",
        "cryptopanic",
        "fear_greed",
    ]
    assert [s.enabled for s in sources] == [True, True, True, True, False, True]
    assert build_sources(SentimentConfig(), env={"CRYPTOPANIC_TOKEN": "x"})[4].enabled is True


def test_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHRONOS_CONTACT", raising=False)
    assert user_agent() == "Chronos/2.0 (+contact)"
    monkeypatch.setenv("CHRONOS_CONTACT", "me@example.com")
    assert user_agent() == "Chronos/2.0 (+me@example.com)"


# ----------------------------------------------------------------------------------------- runner


class ScriptedSource(RssSource):
    """An RssSource whose fetch is replaced by a script of results/exceptions."""

    def __init__(self, name: str, script: list[object], interval_s: float = 60) -> None:
        super().__init__(name, "http://unused", interval_s)
        self.script = script
        self.calls = 0

    async def fetch(self, client: httpx.AsyncClient) -> list[NewsItem]:
        step = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(step, Exception):
            raise step
        assert isinstance(step, list)
        return step


class Sleeper:
    def __init__(self) -> None:
        self.slept: list[float] = []

    async def __call__(self, s: float) -> None:
        self.slept.append(s)


async def test_runner_puts_items_on_the_queue_and_reports_health(client: httpx.AsyncClient) -> None:
    from tests.sentiment_helpers import item

    q: asyncio.Queue[NewsItem] = asyncio.Queue()
    src = ScriptedSource("a", [[item("One"), item("Two")]])
    runner = SourceRunner([src], q, client, sleep=Sleeper(), clock=FakeClock())
    assert await runner.cycle(src) == 2
    assert q.qsize() == 2
    h = runner.health["a"]
    assert (h.status, h.items_total, h.consecutive_failures, h.last_success_at) == ("OK", 2, 0, T0)


async def test_runner_retries_with_exponential_backoff_then_succeeds(client: httpx.AsyncClient) -> None:
    from tests.sentiment_helpers import item

    sleeper = Sleeper()
    q: asyncio.Queue[NewsItem] = asyncio.Queue()
    boom = httpx.ConnectError("down")
    src = ScriptedSource("a", [boom, boom, boom, [item("Finally")]])
    runner = SourceRunner([src], q, client, sleep=sleeper)
    assert await runner.cycle(src) == 1
    assert src.calls == 4
    assert sleeper.slept == [2.0, 2.0, 4.0]  # tenacity wait_exponential(min=2): 1, 2, 4 raised to the 2 s floor
    assert runner.health["a"].status == "OK"


async def test_runner_backoff_is_capped_and_five_attempts_then_degraded(client: httpx.AsyncClient) -> None:
    sleeper = Sleeper()
    src = ScriptedSource("a", [httpx.ConnectError("down")])
    runner = SourceRunner([src], asyncio.Queue(), client, sleep=sleeper, backoff_max_s=5.0)
    assert await runner.cycle(src) == 0
    assert src.calls == 5
    assert sleeper.slept == [2.0, 2.0, 4.0, 5.0]
    h = runner.health["a"]
    assert (h.status, h.consecutive_failures, h.last_error) == ("DEGRADED", 1, "ConnectError")


async def test_degraded_source_is_probed_once_every_900s_and_recovers(client: httpx.AsyncClient) -> None:
    from tests.sentiment_helpers import item

    sleeper = Sleeper()
    src = ScriptedSource("a", [httpx.ConnectError("down")] * 5 + [httpx.ConnectError("still")] + [[item("Back")]])
    q: asyncio.Queue[NewsItem] = asyncio.Queue()
    runner = SourceRunner([src], q, client, sleep=sleeper, rng=random.Random(0))
    await runner.cycle(src)
    assert runner.health["a"].status == "DEGRADED" and src.calls == 5
    await runner.cycle(src)  # degraded: ONE attempt, no hammering
    assert src.calls == 6 and runner.health["a"].consecutive_failures == 2
    await runner.cycle(src)
    assert src.calls == 7 and runner.health["a"].status == "OK" and q.qsize() == 1
    assert runner.health["a"].last_error is None


async def test_run_source_sleeps_interval_with_10_percent_jitter(client: httpx.AsyncClient) -> None:
    class Stop(Exception):
        pass

    sleeps: list[float] = []

    async def sleep(s: float) -> None:
        sleeps.append(s)
        if len(sleeps) == 40:
            raise Stop

    src = ScriptedSource("a", [[]], interval_s=100)
    runner = SourceRunner([src], asyncio.Queue(), client, sleep=sleep, rng=random.Random(1))
    with pytest.raises(Stop):
        await runner.run_source(src)
    assert all(90.0 <= s <= 110.0 for s in sleeps) and len(set(sleeps)) > 10


async def test_degraded_source_sleeps_900s(client: httpx.AsyncClient) -> None:
    class Stop(Exception):
        pass

    sleeps: list[float] = []

    async def sleep(s: float) -> None:
        if s >= 900:
            sleeps.append(s)
            raise Stop

    src = ScriptedSource("a", [httpx.ConnectError("down")])
    runner = SourceRunner([src], asyncio.Queue(), client, sleep=sleep)
    with pytest.raises(Stop):
        await runner.run_source(src)
    assert sleeps == [900.0]


async def test_disabled_sources_are_not_scheduled(client: httpx.AsyncClient) -> None:
    cp = CryptoPanicSource("cryptopanic", 120, env={})
    ok = ScriptedSource("a", [[]])
    runner = SourceRunner([cp, ok], asyncio.Queue(), client, sleep=Sleeper())
    assert [s.name for s in runner.sources] == ["a"] and set(runner.health) == {"a"}


async def test_one_dead_source_does_not_stop_the_others(client: httpx.AsyncClient) -> None:
    from tests.sentiment_helpers import item

    q: asyncio.Queue[NewsItem] = asyncio.Queue()
    dead = ScriptedSource("dead", [httpx.ConnectError("down")])
    alive = ScriptedSource("alive", [[item("News")]])
    runner = SourceRunner([dead, alive], q, client, sleep=Sleeper())
    await asyncio.gather(runner.cycle(dead), runner.cycle(alive))
    assert q.qsize() == 1
    assert runner.health["dead"].status == "DEGRADED" and runner.health["alive"].status == "OK"
