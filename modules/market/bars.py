"""Live 1-minute bars = REST candles read S seconds after the minute closes (spec sez. 2.3 A.2).

Invariants (spec sez. 0.5):
- I1  one definition of a 1m bar in the whole system: the final REST candle (``get_public_candles``, ONE_MINUTE).
      History, runtime, store, labels, replay and training all use it. No bar is ever built from WebSocket trades:
      the exchange assigns trades to candle minutes by a rule that cannot be rebuilt from the trade ``time`` field
      (measured 2026-10-17: about half of the minutes differ, in pairs of adjacent minutes that cancel out).
- I2  the sequence of bars the ``IndicatorEngine`` consumes equals ``load_bars(store)``. A consumed bar is never
      replaced, even if the exchange revises its candle later: revisions are measured (``rest_revisions``), not applied.

Pieces:
- ``BarFinalizer``  pure core, no I/O and no clock: given the candles fetched so far, which minutes can be published;
- ``BarService``    asyncio shell: wakes at ``M + 1 min + S`` (``retry_s`` while a ready minute is pending), fetches the
                    candles of every product in parallel, publishes in order;
- ``BarStoreWriter`` persists the ``rest`` bars to the Parquet store and tracks how far the store is complete.

A minute ``M`` is ready when ``now >= M + 1 min + S``. Its bar is:
- the candle of ``M``                                         -> source ``rest``;
- no candle, but the response holds a LATER candle (proof that REST has processed past ``M``, so ``M`` really had no
  trades)                                                     -> flat bar, source ``flat``;
- neither: wait; past ``rest_max_delay_s`` (and only after a SUCCESSFUL fetch) -> flat bar, source ``flat_unproven``.
A failed fetch publishes nothing and invents nothing: the minute simply waits and the next ones wait behind it.
The candle of the minute still in progress is used only as that proof, never as data (no lookahead, constraint C7).
"""

from __future__ import annotations

import asyncio
from collections import Counter, deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

import pandas as pd
import structlog

from modules.market.backfill import BarStore, CandleSource, candles_to_frame
from modules.market.timeframes import Bar

log = structlog.get_logger(__name__)

MINUTE: Final = timedelta(minutes=1)
REST_CHUNK_MIN: Final = 300  # candles per REST request (API limit is 350)
LAG_SAMPLES: Final = 5_000
OHLCV: Final = ("open", "high", "low", "close", "volume")

BarSource = Literal["rest", "flat", "flat_unproven"]
Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]


def utc_now() -> datetime:
    return datetime.now(UTC)


def floor_minute(t: datetime) -> datetime:
    return t.replace(second=0, microsecond=0)


@dataclass(frozen=True, slots=True)
class BarClosed:
    """A final 1m bar, published once, in order."""

    product_id: str
    bar: Bar
    source: BarSource
    published_at: datetime

    @property
    def t(self) -> datetime:
        """The logical instant of the bar: the close of its minute (``ts_bar_close``)."""
        return self.bar.ts + MINUTE

    @property
    def lag_s(self) -> float:
        return (self.published_at - self.t).total_seconds()


# ----------------------------------------------------------------------------------- the core


class BarFinalizer:
    """Decides which minutes are ready and what bar each one is. No I/O, no clock of its own."""

    def __init__(self, products: Sequence[str], rest_delay_s: float = 120.0, rest_max_delay_s: float = 180.0) -> None:
        self._delay = timedelta(seconds=rest_delay_s)
        self._max_delay = timedelta(seconds=rest_max_delay_s)
        self._next: dict[str, datetime | None] = dict.fromkeys(products)
        self._prev_close: dict[str, float] = {}

    def seed(self, product_id: str, last_bar_ts: datetime, last_close: float) -> None:
        """Continue after the last bar already consumed (the end of the warm-up frame)."""
        if product_id not in self._next:
            raise KeyError(product_id)
        self._next[product_id] = last_bar_ts + MINUTE
        self._prev_close[product_id] = last_close

    def next_minute(self, product_id: str) -> datetime:
        nxt = self._next[product_id]
        if nxt is None:
            raise RuntimeError(f"{product_id} was not seeded from the stored history")
        return nxt

    def due(self, product_id: str) -> datetime:
        """When the next minute becomes ready."""
        return self.next_minute(product_id) + MINUTE + self._delay

    def fetch_window(self, product_id: str, now: datetime) -> tuple[datetime, datetime] | None:
        """REST range ``[start, end)`` to fetch, or None if no minute is ready. ``end`` includes the minute in
        progress, used only as proof of completeness."""
        nxt = self.next_minute(product_id)
        if nxt < floor_minute(now - self._delay):
            return nxt, floor_minute(now) + MINUTE
        return None

    def apply(self, product_id: str, candles: Sequence[Bar], now: datetime) -> list[BarClosed]:
        """Publish every ready minute that can be resolved from ``candles`` (the result of a SUCCESSFUL fetch)."""
        by_ts = {c.ts: c for c in candles}
        newest = max(by_ts) if by_ts else None
        ready_before = floor_minute(now - self._delay)
        out: list[BarClosed] = []
        m = self.next_minute(product_id)
        while m < ready_before:
            candle = by_ts.get(m)
            source: BarSource
            if candle is not None:
                bar, source = candle, "rest"
                self._prev_close[product_id] = candle.close
            else:
                if newest is not None and newest > m:
                    source = "flat"  # REST has moved past this minute and has no candle for it: no trades
                elif now >= m + MINUTE + self._max_delay:
                    source = "flat_unproven"
                    log.warning("bar_flat_unproven", product=product_id, minute=m.isoformat())
                else:
                    break  # not provable yet: wait
                c = self._prev_close[product_id]
                bar = Bar(m, c, c, c, c, 0.0)
            out.append(BarClosed(product_id, bar, source, now))
            m += MINUTE
        self._next[product_id] = m
        return out


# ------------------------------------------------------------------------------------ the store


def bars_to_frame(bars: Sequence[Bar]) -> pd.DataFrame:
    """Same schema as ``candles_to_frame`` (what the store holds)."""
    df = pd.DataFrame(
        {
            "ts": pd.to_datetime([b.ts for b in bars], utc=True).astype("datetime64[ns, UTC]"),
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
            "volume": [b.volume for b in bars],
        }
    )
    return df.sort_values("ts").reset_index(drop=True)


class BarStoreWriter:
    """Persists the ``rest`` bars (like the backfill, minutes without trades are not written) and remembers up to
    which minute the store is complete, flat minutes included."""

    def __init__(self, store: BarStore, complete_until: Mapping[str, datetime], flush_minutes: int = 15) -> None:
        self._store = store
        self._complete = dict(complete_until)
        self._seen = dict(complete_until)
        self._buffer: dict[str, list[Bar]] = {p: [] for p in complete_until}
        self._flush_after = timedelta(minutes=flush_minutes)

    def add(self, event: BarClosed) -> None:
        pid = event.product_id
        self._seen[pid] = max(self._seen[pid], event.bar.ts)
        if event.source == "rest":
            self._buffer[pid].append(event.bar)

    def due(self) -> bool:
        return any(self._seen[p] - self._complete[p] >= self._flush_after for p in self._seen)

    def complete_until(self, product_id: str) -> datetime:
        """Last minute (rest or flat) that is persistent: a flat minute needs no row, so it counts once everything
        before it is on disk."""
        return self._complete[product_id]

    def pending(self, product_id: str) -> int:
        return len(self._buffer[product_id])

    async def flush(self) -> int:
        """Write the buffered bars. A failed write keeps them for the next attempt."""
        written = 0
        for pid in list(self._seen):
            bars, until = list(self._buffer[pid]), self._seen[pid]
            if not bars and until <= self._complete[pid]:
                continue
            if bars:
                try:
                    await asyncio.to_thread(self._store.write, pid, bars_to_frame(bars))
                except Exception as exc:  # disk full, locked file: keep the buffer, try again at the next flush
                    log.error("bar_store_write_failed", product=pid, error=f"{type(exc).__name__}: {exc}")
                    continue
                del self._buffer[pid][: len(bars)]
                written += len(bars)
            self._complete[pid] = until
        return written


# ----------------------------------------------------------------------------------- the service


@dataclass
class ServiceStats:
    published: Counter[str] = field(default_factory=Counter)
    fetch_failures: Counter[str] = field(default_factory=Counter)
    lags_s: deque[float] = field(default_factory=lambda: deque(maxlen=LAG_SAMPLES))


class BarService:
    def __init__(
        self,
        finalizer: BarFinalizer,
        queue: asyncio.Queue[BarClosed],
        source: CandleSource,
        *,
        products: Sequence[str],
        store_writer: BarStoreWriter | None = None,
        retry_s: float = 5.0,
        clock: Clock = utc_now,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._finalizer = finalizer
        self._queue = queue
        self._source = source
        self._products = tuple(products)
        self._writer = store_writer
        self._retry_s = retry_s
        self._clock = clock
        self._sleep = sleep
        self.stats = ServiceStats()

    async def run(self) -> None:
        """Tick, then sleep until the next minute is ready (or ``retry_s`` while a ready one is still pending)."""
        while True:
            await self.tick()
            now = self._clock()
            wake = min(self._finalizer.due(p) for p in self._products)
            await self._sleep(self._retry_s if wake <= now else (wake - now).total_seconds())

    async def tick(self) -> list[BarClosed]:
        now = self._clock()
        results = await asyncio.gather(*(self._fetch(pid, now) for pid in self._products))
        events: list[BarClosed] = []
        for pid, candles in results:
            if candles is not None:
                events += self._finalizer.apply(pid, candles, now)
        events.sort(key=lambda e: (e.bar.ts, e.product_id))
        for e in events:
            if self._writer is not None:
                self._writer.add(e)  # persist first, then let the consumers see it
            self._queue.put_nowait(e)
            self.stats.published[e.source] += 1
            self.stats.lags_s.append(e.lag_s)
        if self._writer is not None and self._writer.due():
            await self._writer.flush()
        return events

    async def _fetch(self, pid: str, now: datetime) -> tuple[str, list[Bar] | None]:
        window = self._finalizer.fetch_window(pid, now)
        if window is None:
            return pid, None
        start, end = window
        bars: list[Bar] = []
        cursor = start
        try:
            while cursor < end:
                upper = min(cursor + timedelta(minutes=REST_CHUNK_MIN), end)
                s, e = int(cursor.timestamp()), int(upper.timestamp())
                raw = await asyncio.to_thread(self._source.fetch, pid, s, e)
                frame = candles_to_frame(raw, s, e)
                cols = [frame[k].tolist() for k in OHLCV]
                bars += [
                    Bar(ts.to_pydatetime(), *map(float, ohlcv)) for ts, *ohlcv in zip(frame["ts"], *cols, strict=True)
                ]
                cursor = upper
        except Exception as exc:  # nothing is published and nothing is invented: the minute waits
            self.stats.fetch_failures[pid] += 1
            log.warning("bar_rest_unavailable", product=pid, error=f"{type(exc).__name__}: {exc}")
            return pid, None
        return pid, bars


# ---------------------------------------------------------------------- revisions (audit, read only)


@dataclass(frozen=True, slots=True)
class RevisionStats:
    product_id: str
    minutes: int
    revised: int
    ratio: float
    max_close_diff_bps: float


def rest_revisions(
    product_id: str, stored: pd.DataFrame, fresh: pd.DataFrame, start: datetime, end: datetime
) -> RevisionStats:
    """How many stored minutes the exchange has revised since. Both frames are ``candles_to_frame`` output.

    A minute is revised if it is present on one side only, or if any OHLCV field differs (exact comparison: both
    sides went through the same parsing). The store is never modified.
    """
    lo, hi = pd.Timestamp(start), pd.Timestamp(end)
    a = stored[(stored["ts"] >= lo) & (stored["ts"] < hi)].set_index("ts")
    b = fresh[(fresh["ts"] >= lo) & (fresh["ts"] < hi)].set_index("ts")
    union, both = a.index.union(b.index), a.index.intersection(b.index)
    revised = len(union) - len(both)  # present on one side only
    max_bps = 0.0
    if len(both):
        x, y = a.loc[both, list(OHLCV)], b.loc[both, list(OHLCV)]
        revised += int((x != y).any(axis=1).sum())
        max_bps = float(((y["close"] / x["close"] - 1).abs() * 1e4).max())
    minutes = len(union)
    return RevisionStats(product_id, minutes, revised, revised / minutes if minutes else 0.0, max_bps)
