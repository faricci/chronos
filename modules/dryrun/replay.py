"""Deterministic replay of recorded market data through the runtime pipeline (spec sez. 5.1.7, 5.3 punto 9).

The point of the replay is parity: the SAME objects that run in production decide here, fed by a recording instead of
the exchange. ``ReplayRunner`` owns no decision, risk, ledger or fill logic. It drives, with one simulated clock:

* ``ReplayMarketStream``: a ``CoinbaseMarketStream`` (same constructor, same ``books`` / ``quotes`` / ``handle_raw`` /
  ``check_silence``, same damage callback) whose messages come from ``data/ws_recordings/`` through ``feed``. Nothing
  connects: a recorded sequence gap or reset marks the books stale and reports the damage exactly as live does, and
  a stretch with no message for ``silence_s`` is the same silence the live watchdog would have seen;
* the bars: a real ``BarService`` over a ``StoreCandleSource`` (the Parquet store, never a bar built from the recorded
  trades: invariant I1) with the simulated clock, so the minute ``M`` is published at ``M + 1 min + S`` and gets
  the very lag, ``flat`` / ``flat_unproven`` classification and ordering of the live service;
* the ``DecisionEngine`` and the venue (a ``ShadowExecutionGateway``) the caller built with the same clock: bars go to
  the engine first, then to the venue, which resolves the entries, brackets and exits the engine started.

Ordering is total and deterministic: a recorded message is applied before a bar published at the very same instant, so
a decision sees the book of its own minute; messages follow the arrival order of the recording (arrival time, then
``sequence_num``). The engine's tasks are never raced: after every event the runner waits until each product is either
idle or parked on a market event (an entry waiting for bars, an exit waiting for its bar).

Running the same window twice gives byte-identical ledgers. The replay never reads the command queue of the control
channel (sez. 6.4): a replay is a function of the recorded data and the parameters, not of what an operator typed.

Before the window the stream is fed from ``preroll`` earlier (default: the start of the UTC day), so the order books
are alive at the first decision: a ``level2`` snapshot is sent once per connection, not every minute.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Final, Protocol

import structlog

from modules.config import BarsConfig
from modules.decision.engine import DecisionEngine, Processed
from modules.dryrun.recorder import merged_messages
from modules.dryrun.shadow_gateway import BarEvent
from modules.execution.gateway import CancelOutcome, ExitResult
from modules.market.backfill import StoreCandleSource, load_bars
from modules.market.bars import BarClosed, BarFinalizer, BarService
from modules.market.ws_feed import SILENCE_S, CoinbaseMarketStream, Trade

__all__ = ["ReplayError", "ReplayMarketStream", "ReplayResult", "ReplayRunner", "ReplayVenue", "SimClock"]

log = structlog.get_logger(__name__)

MINUTE: Final = timedelta(minutes=1)
QUIESCE_TIMEOUT_S: Final = 30.0


class ReplayError(Exception):
    """The window cannot be replayed (no history before it, no recording, a pipeline that does not settle)."""


class SimClock:
    """The one clock of a replay: callable like ``utc_now``, moved only by the replay and never backwards."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance_to(self, t: datetime) -> None:
        self.now = max(self.now, t)


class ReplayMarketStream(CoinbaseMarketStream):
    """``CoinbaseMarketStream`` fed from a recording: ``feed`` replaces the socket, everything else is the live code."""

    def __init__(
        self,
        products: Sequence[str],
        clock: SimClock,
        *,
        on_trades: Callable[[Sequence[Trade]], None] | None = None,
        on_damage: Callable[[datetime, datetime | None], None] | None = None,
        silence_s: float = SILENCE_S,
    ) -> None:
        super().__init__(products, on_trades=on_trades, on_damage=self._record_damage, clock=clock, silence_s=silence_s)
        self._sim = clock
        self._forward_damage = on_damage
        self.damage: list[tuple[datetime, datetime | None]] = []

    def _record_damage(self, start: datetime, end: datetime | None) -> None:
        self.damage.append((start, end))
        if self._forward_damage is not None:
            self._forward_damage(start, end)

    def feed(self, recv_ts: float, raw: str) -> None:
        """Apply one recorded message at its arrival time, after the silence check the live watchdog would have made."""
        self.advance(datetime.fromtimestamp(recv_ts, UTC))
        self.handle_raw(recv_ts, raw)

    def advance(self, t: datetime) -> None:
        """Move the clock to ``t`` and run the silence watchdog there."""
        self.check_silence(t)
        self._sim.advance_to(t)

    async def run(self) -> None:
        raise ReplayError("a ReplayMarketStream has no connection: drive it with feed()")


class ReplayVenue(Protocol):
    """What the runner needs from the execution venue (``ShadowExecutionGateway``)."""

    async def on_bar(self, bar: BarEvent) -> list[ExitResult]: ...

    def has_pending_entry(self, product_id: str) -> bool: ...

    def has_pending_exit(self, product_id: str) -> bool: ...

    async def cancel(self, product_id: str) -> CancelOutcome: ...


@dataclass(slots=True)
class ReplayResult:
    decisions: list[Processed] = field(default_factory=list)
    messages: int = 0
    bars: int = 0
    damage: list[tuple[datetime, datetime | None]] = field(default_factory=list)
    cancelled_at_end: list[str] = field(default_factory=list)  # entries still waiting for bars when the window ended
    pending_at_end: list[str] = field(default_factory=list)  # gateway calls the window ended on (exits), left running


class ReplayRunner:
    def __init__(
        self,
        *,
        clock: SimClock,
        stream: ReplayMarketStream,
        engine: DecisionEngine,
        venue: ReplayVenue,
        products: Sequence[str],
        recordings: Path,
        bars_root: Path,
        bars: BarsConfig,
        spread_bps: Callable[[str, datetime], float],
        warm_up: bool = True,
        preroll: timedelta | None = None,
        timeout_s: float = QUIESCE_TIMEOUT_S,
    ) -> None:
        """``bars_root``: ``<data_dir>/bars/1m``. ``spread_bps``: the spread estimate the venue gets with each bar (the
        runtime's own source). ``warm_up=False``: the caller already adopted engines warmed up to the window start."""
        if stream.products != tuple(products):
            raise ValueError("the stream and the runner must serve the same products")
        self._clock = clock
        self._stream = stream
        self._engine = engine
        self._venue = venue
        self._products = tuple(products)
        self._recordings = Path(recordings)
        self._bars_root = Path(bars_root)
        self._cfg = bars
        self._spread = spread_bps
        self._warm_up = warm_up
        self._preroll = preroll
        self._timeout_s = timeout_s

    # ------------------------------------------------------------------------------------------ run

    async def run(self, start: datetime, end: datetime) -> ReplayResult:
        """Replay the bars of the minutes in ``[start, end)`` (minute aligned, UTC) and the messages around them."""
        if start.second or start.microsecond or end.second or end.microsecond or start >= end:
            raise ValueError("the window must be minute aligned and non empty")
        result = ReplayResult()
        queue: asyncio.Queue[BarClosed] = asyncio.Queue()
        first = start - self._preroll if self._preroll is not None else start.replace(hour=0, minute=0)
        minutes = _minutes(start, end)
        last_pub = self._pub(minutes[-1])
        self._clock.now = first
        service = self._warm_start(start, queue)
        messages = merged_messages(self._recordings, _days(first, last_pub))
        next_bar = 0
        for recv_ts, _seq, raw in messages:
            t = datetime.fromtimestamp(recv_ts, UTC)
            if t < first:
                continue
            if t > last_pub:
                break
            while next_bar < len(minutes) and self._pub(minutes[next_bar]) < t:
                await self._publish(service, queue, minutes[next_bar], result)
                next_bar += 1
            self._stream.feed(recv_ts, raw)
            result.messages += 1
        if result.messages == 0:
            raise ReplayError(f"no recorded message in {self._recordings} between {first} and {last_pub}")
        while next_bar < len(minutes):
            await self._publish(service, queue, minutes[next_bar], result)
            next_bar += 1
        await self._finish(result)
        result.damage = list(self._stream.damage)
        return result

    def _pub(self, minute: datetime) -> datetime:
        """When the bar of ``minute`` is published: ``minute + 1 min + S``."""
        return minute + MINUTE + timedelta(seconds=self._cfg.rest_delay_s)

    # -------------------------------------------------------------------------------------- warm start

    def _warm_start(self, start: datetime, queue: asyncio.Queue[BarClosed]) -> BarService:
        finalizer = BarFinalizer(self._products, self._cfg.rest_delay_s, self._cfg.rest_max_delay_s)
        for pid in self._products:
            history = load_bars(self._bars_root, pid, end=start - MINUTE, store_complete_until_end=True)
            if history.empty or history.index[-1].to_pydatetime() != start - MINUTE:
                raise ReplayError(f"{pid}: the store has no bar for the minute before the window ({start - MINUTE})")
            if self._warm_up:
                self._engine.warm_up(pid, history)
            finalizer.seed(pid, history.index[-1].to_pydatetime(), float(history["close"].iloc[-1]))
        return BarService(
            finalizer, queue, StoreCandleSource(self._bars_root), products=self._products,
            clock=self._clock, retry_s=self._cfg.retry_s,
        )  # fmt: skip

    # --------------------------------------------------------------------------------------- one bar

    async def _publish(
        self, service: BarService, queue: asyncio.Queue[BarClosed], minute: datetime, result: ReplayResult
    ) -> None:
        """The instant ``minute + 1 min + S``: the service publishes the bar, the engine decides, the venue acts."""
        self._stream.advance(self._pub(minute))
        events = await service.tick()
        while not queue.empty():
            queue.get_nowait()  # the service also fills a queue for the live loop; the replay takes the return value
        if not events:
            raise ReplayError(f"the store did not give the bar of {minute.isoformat()}: the history has a hole")
        result.bars += len(events)
        result.decisions += await self._engine.on_bars(events)
        await self._quiesce()
        for ev in sorted(events, key=lambda e: (e.bar.ts, e.product_id)):
            b = ev.bar
            await self._venue.on_bar(
                BarEvent(
                    ev.product_id, b.ts, b.open, b.high, b.low, b.close, b.volume, self._spread(ev.product_id, b.ts)
                )
            )
            await self._quiesce()

    async def _quiesce(self) -> None:
        """Wait until every product's gateway call is done or parked on a market event. Real time passes here (the
        ledger is a thread), simulated time does not."""
        deadline = time.monotonic() + self._timeout_s
        while True:
            busy = [p for p in self._products if self._in_motion(p)]
            if not busy:
                return
            if time.monotonic() > deadline:
                raise ReplayError(f"the pipeline did not settle: {busy} still moving after {self._timeout_s} s")
            await asyncio.sleep(0.001)

    def _in_motion(self, product: str) -> bool:
        kind = self._engine.pending(product)
        if kind == "entry":
            return not self._venue.has_pending_entry(product)
        if kind == "exit":
            return not self._venue.has_pending_exit(product)
        return False

    async def _finish(self, result: ReplayResult) -> None:
        """The window is over: an entry still waiting for bars is cancelled; what it filled follows the normal path."""
        for p in self._products:
            if self._engine.pending(p) == "entry" and self._venue.has_pending_entry(p):
                await self._venue.cancel(p)
                result.cancelled_at_end.append(p)
        await self._quiesce()
        await self._engine.drain(timeout=0)
        result.pending_at_end = [p for p in self._products if self._engine.pending(p) is not None]


def _minutes(start: datetime, end: datetime) -> list[datetime]:
    n = int((end - start) / MINUTE)
    return [start + i * MINUTE for i in range(n)]


def _days(first: datetime, last: datetime) -> list[date]:
    out, d = [], first.date()
    while d <= last.date():
        out.append(d)
        d += timedelta(days=1)
    return out
