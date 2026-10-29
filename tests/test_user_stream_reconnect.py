"""User stream: reconnection defects found by the read-only soak with the real feed (2026-10-20).

The fake transport copies what the SDK really does: ``close()`` calls ``on_close`` (it is the ONLY source of that
callback, a server-side drop produces none) and raises when the socket is already dead.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from modules.execution import user_stream
from modules.execution.user_stream import CoinbaseUserStream
from modules.ledger.positions import PositionLedger
from tests.execution_fakes import World, new_world_parts


@pytest.fixture
async def world() -> AsyncIterator[World]:
    ledger = await PositionLedger.open(":memory:", "live")
    w = new_world_parts(ledger)
    w.snapshot()
    yield w
    await ledger.close()


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(user_stream, "BACKOFF_MIN_S", 0.01)
    monkeypatch.setattr(user_stream, "SUPERVISE_S", 0.02)


def heartbeat(seq: int) -> str:
    return json.dumps(
        {"channel": "heartbeats", "timestamp": datetime.now(UTC).isoformat(), "sequence_num": seq,
         "events": [{"current_time": "x", "heartbeat_counter": seq}]}  # fmt: skip
    )


class SdkLike:
    """``talking`` makes it send a heartbeat every 30 ms from its own thread; clear it to make the feed go quiet."""

    def __init__(
        self, on_message: Callable[[str], None], on_close: Callable[[], None], talking: bool = True
    ) -> None:
        self.on_message, self.on_close = on_message, on_close
        self.alive = False
        self.subscribed = False
        self.close_calls = 0
        self.talking = talking

    def open(self) -> None:
        self.alive = True

    def subscribe(self, product_ids: Sequence[str], channels: Sequence[str]) -> None:
        self.subscribed = True
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        seq = 0
        while self.alive:
            if self.talking:
                self.on_message(heartbeat(seq))
                seq += 1
            threading.Event().wait(0.03)

    def close(self) -> None:
        self.close_calls += 1
        if not self.alive:
            raise RuntimeError("Event loop is not running.")
        self.alive = False
        threading.Thread(target=self.on_close).start()  # like close_async -> self.on_close()

    def push(self, raw: str) -> None:
        threading.Thread(target=self.on_message, args=(raw,)).start()


class Harness:
    def __init__(
        self, world: World, fail_connects: int = 0, fail_subscribe: int = 0, talk: bool = True, **kw: Any
    ) -> None:
        self.talk = talk
        self.transports: list[SdkLike] = []
        self.factory_calls = 0
        self.fail_connects = fail_connects
        self.fail_subscribe = fail_subscribe
        self.reasons: list[str] = []
        self.stream = CoinbaseUserStream(
            ["SOL-EUR"], world.sink, transport_factory=self._factory, request_reconcile=self.reasons.append, **kw
        )

    def _factory(self, on_message: Callable[[str], None], on_close: Callable[[], None]) -> SdkLike:
        self.factory_calls += 1
        if 1 < self.factory_calls <= 1 + self.fail_connects:
            raise ConnectionError("refused")
        t = SdkLike(on_message, on_close, self.talk)
        if self.fail_subscribe and 1 < self.factory_calls <= 1 + self.fail_subscribe:
            t.subscribe = self._boom  # type: ignore[method-assign]
        self.transports.append(t)
        return t

    @staticmethod
    def _boom(product_ids: Sequence[str], channels: Sequence[str]) -> None:
        raise ConnectionError("subscribe failed")

    @property
    def live(self) -> list[SdkLike]:
        return [t for t in self.transports if t.alive]


async def wait_for(condition: Callable[[], bool], timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.005)


async def settle(seconds: float = 0.4) -> None:
    await asyncio.sleep(seconds)


async def stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def first_message_then(h: Harness) -> SdkLike:
    """Wait for the first connection to talk; return it."""
    await wait_for(lambda: len(h.transports) >= 1 and h.stream.stats.messages >= 2)
    return h.transports[0]


async def test_a_silent_feed_with_a_live_socket_reconnects_once_not_forever(world: World) -> None:
    """Closing the old socket makes the SDK call ``on_close``; that callback is OURS, not a new outage."""
    h = Harness(world, silence_s=0.15)
    task = asyncio.create_task(h.stream.run())
    try:
        first = await first_message_then(h)
        first.talking = False  # quiet, but the socket stays open
        await wait_for(lambda: len(h.transports) == 2 and h.transports[1].subscribed)  # silence -> reconnect
        await settle(0.8)  # long enough for a cascade of reconnects to show
        assert h.stream.stats.reconnects == 1
        assert len(h.transports) == 2 and h.transports[1].alive and first.close_calls == 1
    finally:
        await stop(task)


async def test_a_server_side_drop_is_found_by_the_silence_and_the_dead_socket_does_not_block_the_reconnect(
    world: World,
) -> None:
    h = Harness(world, silence_s=0.15)
    task = asyncio.create_task(h.stream.run())
    try:
        first = await first_message_then(h)
        first.alive = False  # the socket dies, no callback; close() will raise
        await wait_for(lambda: len(h.transports) == 2 and h.transports[1].subscribed)
        await wait_for(lambda: h.stream.stats.messages > 2 and h.reasons[-1] == "ws_feed_silent")
        await settle(0.4)
        assert (h.stream.stats.reconnects, len(h.transports)) == (1, 2)
    finally:
        await stop(task)


async def test_a_refused_reconnection_is_retried_until_it_works(world: World) -> None:
    h = Harness(world, fail_connects=3, silence_s=0.15)
    task = asyncio.create_task(h.stream.run())
    try:
        first = await first_message_then(h)
        first.talking = False
        await wait_for(lambda: len(h.transports) == 2 and h.transports[1].subscribed)
        assert h.factory_calls == 5  # one, three refusals, one that works
        assert h.stream.stats.reconnects == 1
        await settle(0.4)
        assert len(h.transports) == 2
    finally:
        await stop(task)


async def test_a_half_open_transport_is_closed_before_the_next_attempt(world: World) -> None:
    h = Harness(world, fail_subscribe=2, silence_s=0.15)
    task = asyncio.create_task(h.stream.run())
    try:
        first = await first_message_then(h)
        first.talking = False
        await wait_for(lambda: len(h.transports) == 4 and h.transports[3].subscribed)
        assert [t.close_calls for t in h.transports[1:3]] == [1, 1]  # the two that opened but could not subscribe
        assert len(h.live) == 1 and h.live[0] is h.transports[3]
    finally:
        await stop(task)


async def test_a_connection_that_never_delivers_a_message_is_detected(world: World) -> None:
    """Silence is measured from the connection too: a feed that opens and says nothing is an outage."""
    h = Harness(world, talk=False, silence_s=0.15)
    task = asyncio.create_task(h.stream.run())
    try:
        await wait_for(lambda: len(h.transports) >= 3 and h.transports[2].subscribed)
        assert h.stream.stats.reconnects >= 2 and h.stream.stats.messages == 0
    finally:
        await stop(task)


async def test_messages_of_a_transport_that_was_replaced_are_ignored(world: World) -> None:
    h = Harness(world, silence_s=0.15)
    task = asyncio.create_task(h.stream.run())
    try:
        old = await first_message_then(h)
        old.talking = False
        await wait_for(lambda: len(h.transports) == 2 and h.transports[1].subscribed)
        h.transports[1].talking = False  # keep the new one quiet while we look at the stale message
        before = h.stream.stats.messages
        old.on_message(heartbeat(99))  # a late message from the socket we gave up on
        await settle(0.05)
        assert h.stream.stats.messages == before and h.stream.stats.stale_dropped >= 1
        assert h.stream.stats.gaps == 0 and h.stream.stats.resets == 0
    finally:
        await stop(task)


async def test_the_first_connection_that_opens_but_cannot_subscribe_is_closed_and_retried(world: World) -> None:
    transports: list[SdkLike] = []

    def factory(on_message: Callable[[str], None], on_close: Callable[[], None]) -> SdkLike:
        t = SdkLike(on_message, on_close)
        if not transports:
            t.subscribe = Harness._boom  # type: ignore[method-assign]
        transports.append(t)
        return t

    stream = CoinbaseUserStream(["SOL-EUR"], world.sink, transport_factory=factory)
    task = asyncio.create_task(stream.run())
    try:
        await wait_for(lambda: len(transports) == 2 and transports[1].subscribed)
        assert transports[0].close_calls == 1 and not transports[0].alive
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_closing_a_dead_sdk_client_does_not_leave_its_thread_running() -> None:
    """The real SDK raises from close() when the socket is dead, before it stops its own event loop and thread."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    class DeadClient:
        def __init__(self) -> None:
            self.loop, self.thread = loop, thread

        def close(self) -> None:
            raise RuntimeError("WebSocket is closed or was never opened.")

    transport = user_stream.CoinbaseUserTransport.__new__(user_stream.CoinbaseUserTransport)
    transport._ws = DeadClient()
    with pytest.raises(RuntimeError, match="never opened"):
        transport.close()
    assert not thread.is_alive() and loop.is_closed()
    with pytest.raises(RuntimeError):
        transport.close()  # a second close on the already cleaned client is harmless
