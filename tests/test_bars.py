"""BarFinalizer / BarService / BarStoreWriter: 1m bars are final REST candles read at T + S (spec sez. 2.3 A.2)."""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from modules.market.backfill import BarStore, candles_to_frame, load_bars
from modules.market.bars import (
    BarClosed,
    BarFinalizer,
    BarService,
    BarStoreWriter,
    bars_to_frame,
    classify_revisions,
    rest_revisions,
)
from modules.market.indicators import IndicatorEngine
from modules.market.indicators_batch import compute_indicators
from modules.market.timeframes import Bar
from tests.indicator_fixtures import assert_parity, synthetic_bars

P, Q = "BTC-EUR", "ETH-EUR"
T0 = datetime(2026, 10, 17, 10, 0, tzinfo=UTC)  # the minute under test starts here
MIN = timedelta(minutes=1)
S = 30.0  # rest_delay_s
MAX_DELAY = 90.0


def at(minute: float, seconds: float = 0.0) -> datetime:
    return T0 + timedelta(minutes=minute, seconds=seconds)


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class FakeSource:
    """REST stand-in: candles by (product, minute); can be made to fail; records every request."""

    def __init__(self) -> None:
        self.candles: dict[tuple[str, datetime], tuple[float, float, float, float, float]] = {}
        self.failing: set[str] = set()
        self.calls: list[tuple[str, int, int]] = []

    def put(self, product: str, minute: datetime, o: float, h: float, lo: float, c: float, v: float) -> None:
        self.candles[(product, minute)] = (o, h, lo, c, v)

    def fetch(self, product_id: str, start_s: int, end_s: int) -> list[dict[str, Any]]:
        self.calls.append((product_id, start_s, end_s))
        if product_id in self.failing:
            raise ConnectionError("rest down")
        return [
            {
                "start": str(int(m.timestamp())),
                "open": str(o),
                "high": str(h),
                "low": str(lo),
                "close": str(c),
                "volume": str(v),
            }  # fmt: skip
            for (p, m), (o, h, lo, c, v) in sorted(self.candles.items())
            if p == product_id and start_s <= int(m.timestamp()) <= end_s
        ]


def make(
    products: tuple[str, ...] = (P,), now: datetime | None = None, last_close: float = 100.0, **kw: Any
) -> tuple[BarService, FakeSource, FakeClock, asyncio.Queue[BarClosed], BarFinalizer]:
    fin = BarFinalizer(products, rest_delay_s=S, rest_max_delay_s=MAX_DELAY)
    for p in products:
        fin.seed(p, T0 - MIN, last_close)  # the last consumed bar is 09:59
    src, clock = FakeSource(), FakeClock(now or at(1, S))
    queue: asyncio.Queue[BarClosed] = asyncio.Queue()
    svc = BarService(fin, queue, src, products=products, clock=clock, **kw)
    return svc, src, clock, queue, fin


# -------------------------------------------------------------------------- readiness


async def test_not_before_t_plus_s() -> None:
    svc, src, clock, queue, _ = make()
    src.put(P, at(0), 100, 101, 99, 100.5, 3.0)
    src.put(P, at(1), 100.5, 100.6, 100.4, 100.5, 1.0)  # the minute in progress
    clock.now = at(1, S) - timedelta(milliseconds=1)
    assert await svc.tick() == [] and src.calls == []  # nothing is even requested
    clock.now = at(1, S)
    (e,) = await svc.tick()
    assert (e.product_id, e.source, e.bar) == (P, "rest", Bar(T0, 100, 101, 99, 100.5, 3.0))
    assert e.t == at(1) and e.published_at == at(1, S) and e.lag_s == S
    assert queue.qsize() == 1


async def test_nothing_is_published_twice() -> None:
    svc, src, clock, _, _ = make()
    src.put(P, at(0), 100, 101, 99, 100.5, 3.0)
    src.put(P, at(1), 100, 101, 99, 100.5, 1.0)
    assert len(await svc.tick()) == 1
    assert await svc.tick() == [] and len(src.calls) == 1  # next minute not ready: no new request


# ---------------------------------------------------------------------------- catch-up


async def test_contiguous_catch_up_in_chunks() -> None:
    svc, src, clock, queue, _ = make(now=at(700, S))
    for m in range(0, 702):
        if m % 3:  # a third of the minutes had no trades
            src.put(P, at(m), 100 + m, 101 + m, 99 + m, 100 + m, 1.0)
    events = await svc.tick()
    assert [e.bar.ts for e in events] == [at(m) for m in range(700)]  # contiguous, in order, each once
    assert len(src.calls) == 3  # 701 minutes (the one in progress included) in chunks of at most 300
    assert {e.source for e in events} == {"rest", "flat"}
    flat = events[300]  # minute 300 has m % 3 == 0 -> no candle
    prev = events[299].bar.close
    assert flat.source == "flat"
    assert (flat.bar.open, flat.bar.high, flat.bar.low, flat.bar.close, flat.bar.volume) == (
        prev,
        prev,
        prev,
        prev,
        0.0,
    )
    assert queue.qsize() == 700


# --------------------------------------------------------------------- flat bars and proof


async def test_flat_with_proof() -> None:
    svc, src, clock, _, _ = make(last_close=99.0, now=at(2, S))
    src.put(P, at(1), 100, 101, 99, 100.5, 3.0)  # minute 10:00 has no candle, 10:01 has one: REST moved past 10:00
    events = await svc.tick()
    assert [(e.bar.ts, e.source) for e in events] == [(at(0), "flat"), (at(1), "rest")]
    b = events[0].bar
    assert (b.open, b.high, b.low, b.close, b.volume) == (99.0, 99.0, 99.0, 99.0, 0.0)


async def test_flat_unproven_waits_for_the_max_delay() -> None:
    svc, src, clock, _, fin = make(now=at(1, S))
    assert await svc.tick() == []  # ready, but REST holds nothing: cannot tell a quiet minute from a lagging feed
    clock.now = at(1, MAX_DELAY) - timedelta(milliseconds=1)
    assert await svc.tick() == []
    clock.now = at(1, MAX_DELAY)
    (e,) = await svc.tick()
    assert (e.bar.ts, e.source, e.bar.volume) == (at(0), "flat_unproven", 0.0)
    assert fin.next_minute(P) == at(1)


async def test_the_in_progress_candle_is_proof_only_never_data() -> None:
    """C7: the candle of the minute still in progress decides WHEN a flat bar is published, never WHAT it holds."""
    plain, _, clock1, _, _ = make(now=at(1, MAX_DELAY))  # no proof: flat_unproven once the max delay has passed
    proofed, src2, _, _, _ = make(now=at(1, S))  # proof available: flat as soon as the minute is ready
    src2.put(P, at(1), 1e9, 2e9, 5.0, 7e9, 123456.0)  # nonsense values for a minute that is not over
    (a,) = await plain.tick()
    (b,) = await proofed.tick()
    assert (a.source, b.source) == ("flat_unproven", "flat")
    assert a.bar == b.bar == Bar(T0, 100.0, 100.0, 100.0, 100.0, 0.0)  # identical content, only the timing differs
    assert b.published_at < a.published_at


# ------------------------------------------------------------------------ failures


async def test_fetch_error_invents_nothing_and_recovers_in_order() -> None:
    svc, src, clock, queue, _ = make(now=at(1, S))
    src.failing.add(P)
    for k in range(4):
        clock.now = at(1 + k, MAX_DELAY + 30)  # far beyond rest_max_delay_s for the first minutes
        assert await svc.tick() == []
    assert svc.stats.fetch_failures[P] == 4 and queue.empty()
    src.failing.clear()
    for m, c in ((0, 100.5), (1, 101.0), (3, 102.0)):  # 10:02 had no trades
        src.put(P, at(m), 100, 103, 99, c, 1.0)
    src.put(P, at(4), 102, 103, 101, 102.5, 1.0)
    clock.now = at(5, S)
    events = await svc.tick()
    assert [(e.bar.ts, e.source) for e in events] == [
        (at(0), "rest"), (at(1), "rest"), (at(2), "flat"), (at(3), "rest"), (at(4), "rest"),
    ]  # fmt: skip
    assert events[2].bar.close == 101.0  # the flat minute carries the previous close
    assert all(e.lag_s >= 60 for e in events[:3])  # published late, and honest about it


async def test_a_product_in_error_does_not_delay_the_others() -> None:
    svc, src, clock, _, _ = make(products=(P, Q), now=at(1, S))
    src.put(P, at(0), 100, 101, 99, 100.5, 1.0)
    src.put(Q, at(0), 10, 11, 9, 10.5, 1.0)
    src.failing.add(Q)
    events = await svc.tick()
    assert [(e.product_id, e.source) for e in events] == [(P, "rest")]
    src.failing.clear()
    clock.now = at(1, S + 5)
    assert [e.product_id for e in await svc.tick()] == [Q]


async def test_events_are_ordered_by_minute_then_product() -> None:
    svc, src, clock, _, _ = make(products=(Q, P), now=at(2, S))
    for p in (P, Q):
        for m in (0, 1, 2):
            src.put(p, at(m), 1, 2, 0.5, 1.5, 1.0)
    events = await svc.tick()
    assert [(e.bar.ts, e.product_id) for e in events] == [(at(0), P), (at(0), Q), (at(1), P), (at(1), Q)]


def test_an_unseeded_product_cannot_be_used() -> None:
    fin = BarFinalizer([P])
    for call in (lambda: fin.next_minute(P), lambda: fin.due(P), lambda: fin.fetch_window(P, at(5))):
        with pytest.raises(RuntimeError, match="not seeded"):
            call()
    with pytest.raises(KeyError):
        fin.seed("XYZ-EUR", T0, 1.0)


async def test_service_with_an_unseeded_product_fails_loudly() -> None:
    fin = BarFinalizer([P])
    svc = BarService(fin, asyncio.Queue(), FakeSource(), products=[P], clock=FakeClock(at(5)))
    with pytest.raises(RuntimeError):
        await svc.tick()


# ------------------------------------------------------------------------- schedule


async def test_run_sleeps_until_the_next_minute_is_ready_and_retries_while_one_is_pending() -> None:
    svc, src, clock, _, _ = make(now=at(1, 31))
    src.put(P, at(0), 100, 101, 99, 100.5, 1.0)
    src.put(P, at(1), 100, 101, 99, 100.6, 1.0)
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)
        if len(slept) == 3:
            raise asyncio.CancelledError
        clock.now += timedelta(seconds=s)

    svc._sleep = sleep
    with pytest.raises(asyncio.CancelledError):
        await svc.run()
    # tick at 10:01:31 publishes 10:00; 10:01 becomes ready at 10:02:30 -> sleep 59 s;
    # at 10:02:30 it publishes 10:01; 10:02 is ready at 10:03:30 -> sleep 60 s;
    # at 10:03:30 minute 10:02 is ready but REST has nothing yet -> retry_s (5 s)
    assert slept == [pytest.approx(59.0), pytest.approx(60.0), 5.0]


# --------------------------------------------------------------------------- store (I2)


async def test_store_roundtrip_invariant(tmp_path: Path) -> None:
    """I2: what was emitted (rest, flat, flat_unproven, catch-up) is exactly what load_bars gives back."""
    store = BarStore(tmp_path)
    writer = BarStoreWriter(store, {P: T0 - MIN}, flush_minutes=15)
    svc, src, clock, _, _ = make(now=at(30, S), store_writer=writer)
    for m in (0, 1, 2, 5, 6, 7, 8, 9, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28):
        src.put(P, at(m), 100 + m, 101 + m, 99 + m, 100 + m + 0.5, 1.0 + m / 7)
    events = await svc.tick()
    await writer.flush()
    # 29 is missing and nothing follows it: not provable yet, so the batch stops at minute 28
    assert [e.bar.ts for e in events] == [at(m) for m in range(29)]
    loaded = load_bars(tmp_path, P, start=at(0), end=at(28), store_complete_until_end=True)
    emitted = pd.DataFrame(
        [(e.bar.open, e.bar.high, e.bar.low, e.bar.close, e.bar.volume) for e in events],
        columns=["open", "high", "low", "close", "volume"],
        index=pd.DatetimeIndex([e.bar.ts for e in events], name="ts").astype("datetime64[ns, UTC]"),
    )
    pd.testing.assert_frame_equal(loaded, emitted, check_freq=False)
    assert {e.source for e in events} == {"rest", "flat"}


def closed(minute: int, source: str = "rest") -> BarClosed:
    return BarClosed(P, Bar(at(minute), 1, 2, 0.5, 1.5, 1.0), source, at(minute + 1, S))  # type: ignore[arg-type]


async def test_writer_flush_cadence_and_complete_until(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    writer = BarStoreWriter(store, {P: T0 - MIN}, flush_minutes=15)
    for m in range(14):
        writer.add(closed(m))
    assert not writer.due() and writer.pending(P) == 14
    writer.add(closed(14))
    assert writer.due() and writer.complete_until(P) == T0 - MIN
    assert await writer.flush() == 15
    assert writer.complete_until(P) == at(14) and writer.pending(P) == 0 and not writer.due()
    assert len(store.read_month(P, 2026, 10)) == 15
    # flat minutes are not written, but they advance the completeness mark once everything before them is on disk
    writer.add(closed(15, "flat"))
    writer.add(closed(16, "flat_unproven"))
    assert await writer.flush() == 0 and writer.complete_until(P) == at(16)
    assert len(store.read_month(P, 2026, 10)) == 15


async def test_a_failed_store_write_keeps_the_buffer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = BarStore(tmp_path)
    writer = BarStoreWriter(store, {P: T0 - MIN}, flush_minutes=1)
    for m in range(3):
        writer.add(closed(m))

    def boom(*a: object, **k: object) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(store, "write", boom)
    assert await writer.flush() == 0 and writer.pending(P) == 3 and writer.complete_until(P) == T0 - MIN
    monkeypatch.undo()
    assert await writer.flush() == 3 and writer.complete_until(P) == at(2)


def test_bars_to_frame_has_the_schema_of_the_store() -> None:
    raw = [
        {"start": str(int(at(m).timestamp())), "open": "1", "high": "2", "low": "0.5", "close": "1.5", "volume": "3"}
        for m in range(3)
    ]
    ref = candles_to_frame(raw, 0, 4_000_000_000)
    mine = bars_to_frame([Bar(at(m), 1.0, 2.0, 0.5, 1.5, 3.0) for m in range(3)])
    pd.testing.assert_frame_equal(mine, ref)


# ----------------------------------------------------------------- engine parity at runtime


async def test_engine_parity_on_runtime_bars(tmp_path: Path) -> None:
    """The bars the service publishes, fed to the IndicatorEngine, give the batch features of the stored series."""
    synth = synthetic_bars(days=5, seed=4, zero_volume_day=2, constant_days=None)  # 7200 bars with quiet stretches
    start = synth.index[0].to_pydatetime()
    store = BarStore(tmp_path)
    fin = BarFinalizer([P], S, MAX_DELAY)
    fin.seed(P, start - MIN, float(synth.iloc[0]["open"]))
    writer = BarStoreWriter(store, {P: start - MIN}, flush_minutes=60)
    src = FakeSource()
    for ts, r in zip(synth.index.to_pydatetime(), synth.itertuples(index=False), strict=True):
        if r.volume > 0:  # a quiet minute has no candle
            src.put(P, ts, r.open, r.high, r.low, r.close, r.volume)
    end = synth.index[-1].to_pydatetime()
    clock = FakeClock(end + MIN + timedelta(seconds=S))
    svc = BarService(fin, asyncio.Queue(), src, products=[P], store_writer=writer, clock=clock)
    events = await svc.tick()
    await writer.flush()
    assert len(events) > 5000

    engine = IndicatorEngine(P)
    inc = pd.DataFrame(
        [engine.update(e.bar).as_row() for e in events],
        index=pd.DatetimeIndex([e.bar.ts for e in events], name="ts"),
    )
    stored = load_bars(tmp_path, P, store_complete_until_end=True)
    stored = stored.loc[stored.index <= events[-1].bar.ts]
    assert len(stored) == len(events)
    assert_parity(inc, compute_indicators(stored))


# --------------------------------------------------------------------- audit of revisions


def frame(rows: list[tuple[int, float, float]]) -> pd.DataFrame:
    """(minute, close, volume) rows in the candles_to_frame format."""
    raw = [
        {
            "start": str(int(at(m).timestamp())),
            "open": str(c),
            "high": str(c),
            "low": str(c),
            "close": str(c),
            "volume": str(v),
        }  # fmt: skip
        for m, c, v in rows
    ]
    return candles_to_frame(raw, 0, 4_000_000_000)


def test_rest_revisions_counts_changed_new_and_vanished_minutes() -> None:
    stored = frame([(0, 100, 1), (1, 100, 1), (2, 100, 1), (3, 100, 1), (4, 100, 1)])
    fresh = frame([(0, 100, 1), (1, 101, 1), (2, 100, 2), (4, 100, 1), (5, 100, 1)])
    # 1: close changed; 2: volume changed; 3: vanished; 5: new candle where the store has none
    r = rest_revisions(P, stored, fresh, at(0), at(10))
    assert (r.minutes, r.revised) == (6, 4) and r.ratio == pytest.approx(4 / 6)
    assert r.max_close_diff_bps == pytest.approx(100.0)  # 100 -> 101 is 100 bps
    assert r.product_id == P


def test_rest_revisions_without_changes_and_window_restriction() -> None:
    same = frame([(0, 100, 1), (1, 101, 2)])
    r = rest_revisions(P, same, same.copy(), at(0), at(10))
    assert (r.minutes, r.revised, r.ratio, r.max_close_diff_bps) == (2, 0, 0.0, 0.0)
    other = frame([(0, 100, 1), (1, 105, 2)])
    assert rest_revisions(P, same, other, at(0), at(1)).revised == 0  # minute 10:01 is outside [start, end)
    empty = rest_revisions(P, same.iloc[:0], other.iloc[:0], at(0), at(10))
    assert (empty.minutes, empty.ratio) == (0, 0.0)


def test_classify_revisions_value_appeared_disappeared() -> None:
    stored = frame([(0, 100, 1), (1, 100, 1), (2, 100, 1), (3, 100, 1), (4, 100, 1)])
    fresh = frame([(0, 100, 1), (1, 101, 1), (2, 100, 2), (4, 100, 1), (5, 100, 1)])
    r = classify_revisions(stored, fresh, at(0), at(10))
    assert list(r.value.index) == [pd.Timestamp(at(1)), pd.Timestamp(at(2))]
    assert r.value["close"].tolist() == [101.0, 100.0] and r.value["volume"].tolist() == [1.0, 2.0]  # RE-READ values
    assert r.stored_value["close"].tolist() == [100.0, 100.0] and r.stored_value["volume"].tolist() == [1.0, 1.0]
    assert list(r.appeared) == [pd.Timestamp(at(5))] and list(r.disappeared) == [pd.Timestamp(at(3))]
    same = classify_revisions(stored, stored.copy(), at(0), at(10))
    assert same.value.empty and len(same.appeared) == len(same.disappeared) == 0


def test_classify_revisions_window_end_exclusive() -> None:
    """A candle exactly at ``end`` is ignored (the 05:40 artifact of the research scripts, END was inclusive there)."""
    stored = frame([(0, 100, 1), (1, 100, 1)])
    fresh = frame([(0, 100, 1), (1, 100, 1), (2, 100, 1)])
    assert len(classify_revisions(stored, fresh, at(0), at(2)).appeared) == 0
    assert list(classify_revisions(stored, fresh, at(0), at(3)).appeared) == [pd.Timestamp(at(2))]
    assert len(classify_revisions(stored, fresh, at(1), at(3)).appeared) == 1  # start is inclusive, minute 0 left out


def test_classify_revisions_on_store_rows_counts_no_flat_minute(tmp_path: Path) -> None:
    """Minutes without trades have no row in the store and no candle in REST: neither appeared nor disappeared. On the
    FILLED grid they would all look "disappeared" (the reason ``classify_revisions`` takes the real rows)."""
    real = frame([(0, 100, 1), (5, 101, 2)])  # minutes 1..4 are flat: no row, no candle
    store = BarStore(tmp_path)
    store.write(P, real)
    rows = store.read_month(P, T0.year, T0.month)
    r = classify_revisions(rows, real.copy(), at(0), at(10))
    assert r.value.empty and len(r.appeared) == 0 and len(r.disappeared) == 0
    grid = load_bars(tmp_path, P, store_complete_until_end=False).reset_index()
    assert len(classify_revisions(grid, real.copy(), at(0), at(10)).disappeared) == 4  # what the filled grid would say


def test_rest_revisions_counts_unchanged() -> None:
    """The ratio of the old criterion (now a diagnostic) is what it always was; the new fields only split it."""
    stored = frame([(0, 100, 1), (1, 100, 1), (2, 100, 1), (3, 100, 1), (4, 100, 1)])
    fresh = frame([(0, 100, 1), (1, 101, 1), (2, 100, 2), (4, 100, 1), (5, 100, 1)])
    r = rest_revisions(P, stored, fresh, at(0), at(10))
    assert (r.minutes, r.revised, r.value_revised, r.appeared, r.disappeared) == (6, 4, 2, 1, 1)
    assert r.ratio == pytest.approx(4 / 6) and r.max_close_diff_bps == pytest.approx(100.0)
    only = rest_revisions(P, stored, fresh, at(1), at(3))  # window [1, 3): minutes 1 and 2, both revised by value
    assert (only.minutes, only.revised, only.appeared, only.disappeared) == (2, 2, 0, 0)


def test_bar_closed_properties() -> None:
    e = BarClosed(P, Bar(at(5), 1, 1, 1, 1, 0), "flat", at(6, 42))
    assert e.t == at(6) and e.lag_s == 42.0 and not math.isnan(e.lag_s)
    assert np.isclose(e.lag_s, 42.0)
