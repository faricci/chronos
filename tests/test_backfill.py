from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import requests
from aiolimiter import AsyncLimiter
from tenacity import wait_none

from modules.config import BackfillConfig
from modules.market.backfill import (
    Backfiller,
    BarStore,
    CandleSource,
    CoinbaseCandleSource,
    build_report,
    candles_to_frame,
    fill_flat_bars,
    is_transient,
    load_bars,
)

T0 = datetime(2026, 1, 31, 23, 0, tzinfo=UTC)  # chosen to cross a month boundary
PRODUCT = "BTC-EUR"


def sec(t: datetime) -> int:
    return int(t.timestamp())


def candle(t: datetime, close: float = 100.0, volume: float = 1.0) -> dict[str, Any]:
    """Raw API candle: every field is a string."""
    return {
        "start": str(sec(t)),
        "low": str(close - 1),
        "high": str(close + 1),
        "open": str(close - 0.5),
        "close": str(close),
        "volume": str(volume),
    }


def http_error(status: int) -> requests.exceptions.HTTPError:
    resp = requests.Response()
    resp.status_code = status
    return requests.exceptions.HTTPError(f"{status}", response=resp)


class FakeSource:
    """Serves one candle per minute listed in ``traded`` (a set of datetimes); records every request."""

    def __init__(self, traded: set[datetime], fail: dict[int, Exception] | None = None) -> None:
        self.traded = traded
        self.fail = fail or {}
        self.requests: list[tuple[int, int]] = []

    def fetch(self, product_id: str, start_s: int, end_s: int) -> list[dict[str, Any]]:
        call = len(self.requests)
        self.requests.append((start_s, end_s))
        if call in self.fail:
            raise self.fail[call]
        # like the real API, the end boundary is inclusive: the caller must filter it out
        return [candle(t, 100 + (sec(t) % 7)) for t in sorted(self.traded) if start_s <= sec(t) <= end_s]


def make_backfiller(
    tmp_path: Path, source: CandleSource, now: datetime, *, window: int = 300, retries: int = 3
) -> Backfiller:
    return Backfiller(
        store=BarStore(tmp_path / "bars" / "1m"),
        source=source,
        limiter=AsyncLimiter(1000, 1),
        cfg=BackfillConfig(window_minutes=window, max_retries=retries),
        clock=lambda: now + timedelta(seconds=17),  # mid-minute: the open minute must not be fetched
        retry_wait=wait_none(),
    )


def minutes(start: datetime, n: int, step: int = 1) -> set[datetime]:
    return {start + timedelta(minutes=i * step) for i in range(n)}


# ------------------------------------------------------------------------------------ parsing / store


def test_candles_to_frame_filters_window_dedups_and_sorts() -> None:
    a, b, c = T0, T0 + timedelta(minutes=1), T0 + timedelta(minutes=2)
    raw = [candle(b), candle(a), candle(a, close=105.0), candle(c)]  # c is the exclusive end
    df = candles_to_frame(raw, sec(a), sec(c))
    assert list(df["ts"]) == [pd.Timestamp(a), pd.Timestamp(b)]
    assert df.loc[0, "close"] == 105.0  # last duplicate wins
    assert str(df["ts"].dtype) == "datetime64[ns, UTC]"
    assert list(df.columns) == ["ts", "open", "high", "low", "close", "volume"]


def test_candles_to_frame_empty() -> None:
    assert candles_to_frame([], 0, 60).empty


def test_store_layout_merge_and_last_ts(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    assert store.last_ts(PRODUCT) is None
    jan = candles_to_frame([candle(T0), candle(T0 + timedelta(minutes=30))], sec(T0), sec(T0) + 3600)
    feb = candles_to_frame([candle(T0 + timedelta(hours=2))], sec(T0), sec(T0) + 7200 + 60)
    assert store.write(PRODUCT, pd.concat([jan, feb])) == 3
    assert (tmp_path / "product=BTC-EUR" / "year=2026" / "month=1.parquet").is_file()
    assert (tmp_path / "product=BTC-EUR" / "year=2026" / "month=2.parquet").is_file()
    assert store.months(PRODUCT) == [(2026, 1), (2026, 2)]
    assert store.last_ts(PRODUCT) == T0 + timedelta(hours=2)
    assert not list(tmp_path.rglob("*.tmp"))

    # rewriting the same minute replaces it, never duplicates it
    again = candles_to_frame([candle(T0, close=999.0)], sec(T0), sec(T0) + 60)
    store.write(PRODUCT, again)
    jan_df = store.read_month(PRODUCT, 2026, 1)
    assert len(jan_df) == 2 and jan_df.loc[0, "close"] == 999.0


# ------------------------------------------------------------------------------------- flat bars


def test_gaps_become_flat_bars() -> None:
    real = candles_to_frame(
        [candle(T0, close=100.0, volume=5.0), candle(T0 + timedelta(minutes=3), close=110.0, volume=2.0)],
        sec(T0),
        sec(T0) + 600,
    )
    bars = fill_flat_bars(real)
    assert len(bars) == 4
    assert list(bars.index) == [pd.Timestamp(T0 + timedelta(minutes=i)) for i in range(4)]
    for i in (1, 2):  # the two quiet minutes
        row = bars.iloc[i]
        assert (row["open"], row["high"], row["low"], row["close"]) == (100.0, 100.0, 100.0, 100.0)
        assert row["volume"] == 0.0
    assert bars.iloc[3]["close"] == 110.0 and bars.iloc[3]["volume"] == 2.0
    assert not bars.isna().any().any()


def test_fill_extends_flat_to_a_later_end() -> None:
    real = candles_to_frame([candle(T0, close=100.0)], sec(T0), sec(T0) + 60)
    bars = fill_flat_bars(real, end=T0 + timedelta(minutes=2))
    assert len(bars) == 3 and set(bars["close"]) == {100.0} and list(bars["volume"]) == [1.0, 0.0, 0.0]


def test_load_bars_uses_previous_month_close_when_start_is_in_a_gap(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    last_jan = datetime(2026, 1, 31, 23, 59, tzinfo=UTC)
    first_feb = datetime(2026, 2, 1, 0, 5, tzinfo=UTC)
    store.write(PRODUCT, candles_to_frame([candle(last_jan, 100.0)], sec(last_jan), sec(last_jan) + 60))
    store.write(PRODUCT, candles_to_frame([candle(first_feb, 120.0)], sec(first_feb), sec(first_feb) + 60))

    start = datetime(2026, 2, 1, 0, 2, tzinfo=UTC)  # inside the quiet stretch, in a different month
    bars = load_bars(tmp_path, PRODUCT, start=start, end=first_feb)
    assert list(bars.index)[0] == pd.Timestamp(start)
    assert list(bars["close"]) == [100.0, 100.0, 100.0, 120.0]
    assert list(bars["volume"]) == [0.0, 0.0, 0.0, 1.0]


def test_load_bars_defaults_and_unknown_product(tmp_path: Path) -> None:
    assert load_bars(tmp_path, "NOPE-EUR").empty
    store = BarStore(tmp_path)
    store.write(PRODUCT, candles_to_frame(
        [candle(T0), candle(T0 + timedelta(minutes=4))], sec(T0), sec(T0) + 600
    ))
    bars = load_bars(tmp_path, PRODUCT)
    assert len(bars) == 5 and bars.index.tz is not None


def test_load_bars_before_first_bar_drops_unbuildable_minutes(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    store.write(PRODUCT, candles_to_frame([candle(T0)], sec(T0), sec(T0) + 60))
    bars = load_bars(tmp_path, PRODUCT, start=T0 - timedelta(hours=1), end=T0 + timedelta(minutes=1))
    assert list(bars.index)[0] == pd.Timestamp(T0)  # no previous close exists before the first trade


# ----------------------------------------------------------------------------------------- backfill


async def test_backfill_windows_resume_and_idempotence(tmp_path: Path) -> None:
    start = datetime(2026, 1, 31, 20, 0, tzinfo=UTC)
    now = start + timedelta(minutes=700)
    traded = minutes(start, 700, step=2)  # every other minute trades
    src = FakeSource(traded)
    bf = make_backfiller(tmp_path, src, now)

    (report,) = await bf.run([PRODUCT], start)
    assert report.rows == 350 and report.new_rows == 350
    # 700 minutes in windows of 300: [0,300) [300,600) [600,700) - the open minute is never requested
    assert src.requests == [
        (sec(start), sec(start + timedelta(minutes=300))),
        (sec(start + timedelta(minutes=300)), sec(start + timedelta(minutes=600))),
        (sec(start + timedelta(minutes=600)), sec(now)),
    ]
    df = pd.concat([bf.store.read_month(PRODUCT, *m) for m in bf.store.months(PRODUCT)])
    assert df["ts"].is_unique and df["ts"].is_monotonic_increasing
    assert df["ts"].min() == pd.Timestamp(start) and df["ts"].max() <= pd.Timestamp(now)

    # second run, same instant: resumes after the last stored minute, adds nothing, creates no duplicates
    src2 = FakeSource(traded)
    (again,) = await make_backfiller(tmp_path, src2, now).run([PRODUCT], start)
    assert again.rows == 350 and again.new_rows == 0
    assert src2.requests[0][0] == sec(report.last + timedelta(minutes=1))  # type: ignore[operator]

    # later run: only the new stretch is requested
    later = now + timedelta(minutes=120)
    src3 = FakeSource(minutes(start, 820, step=2))
    (grown,) = await make_backfiller(tmp_path, src3, later).run([PRODUCT], start)
    assert grown.rows == 410 and grown.new_rows == 60
    assert src3.requests[-1][1] == sec(later)


async def test_interrupted_run_keeps_what_was_downloaded_and_resumes(tmp_path: Path) -> None:
    start = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
    now = start + timedelta(minutes=900)
    traded = minutes(start, 900)
    boom = RuntimeError("network down")
    bf = make_backfiller(tmp_path, FakeSource(traded, fail={2: boom}), now)
    with pytest.raises(RuntimeError, match="network down"):
        await bf.run([PRODUCT], start)
    assert bf.store.last_ts(PRODUCT) == start + timedelta(minutes=599)  # two windows were saved by the finally

    src = FakeSource(traded)
    (report,) = await make_backfiller(tmp_path, src, now).run([PRODUCT], start)
    assert report.rows == 900
    assert src.requests[0][0] == sec(start + timedelta(minutes=600))


async def test_transient_errors_are_retried(tmp_path: Path) -> None:
    start = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
    failures: dict[int, Exception] = {0: http_error(429), 1: http_error(503), 2: requests.exceptions.Timeout()}
    src = FakeSource(minutes(start, 10), fail=failures)
    (report,) = await make_backfiller(tmp_path, src, start + timedelta(minutes=10), retries=4).run([PRODUCT], start)
    assert report.rows == 10 and len(src.requests) == 4


async def test_retries_are_bounded(tmp_path: Path) -> None:
    start = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
    src = FakeSource(minutes(start, 10), fail={i: http_error(500) for i in range(10)})
    with pytest.raises(requests.exceptions.HTTPError):
        await make_backfiller(tmp_path, src, start + timedelta(minutes=10), retries=3).run([PRODUCT], start)
    assert len(src.requests) == 3


async def test_client_errors_fail_fast(tmp_path: Path) -> None:
    start = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
    src = FakeSource(minutes(start, 10), fail={0: http_error(404)})
    with pytest.raises(requests.exceptions.HTTPError):
        await make_backfiller(tmp_path, src, start + timedelta(minutes=10)).run([PRODUCT], start)
    assert len(src.requests) == 1


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (http_error(429), True),
        (http_error(500), True),
        (http_error(502), True),
        (http_error(400), False),
        (http_error(403), False),
        (requests.exceptions.ConnectionError(), True),
        (requests.exceptions.Timeout(), True),
        (requests.exceptions.HTTPError("no response"), True),
        (ValueError("bug"), False),
    ],
)
def test_is_transient(exc: BaseException, expected: bool) -> None:
    assert is_transient(exc) is expected


async def test_products_share_one_run_and_are_stored_separately(tmp_path: Path) -> None:
    start = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
    src = FakeSource(minutes(start, 30))
    reports = await make_backfiller(tmp_path, src, start + timedelta(minutes=30)).run(["BTC-EUR", "ETH-EUR"], start)
    assert [r.product_id for r in reports] == ["BTC-EUR", "ETH-EUR"]
    assert all(r.rows == 30 for r in reports)
    assert sorted(p.name for p in (tmp_path / "bars" / "1m").iterdir()) == ["product=BTC-EUR", "product=ETH-EUR"]


async def test_nothing_to_fetch_when_start_is_in_the_future(tmp_path: Path) -> None:
    now = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
    src = FakeSource(set())
    (report,) = await make_backfiller(tmp_path, src, now).run([PRODUCT], now + timedelta(days=1))
    assert src.requests == [] and report.rows == 0 and report.first is None


# ------------------------------------------------------------------------------------------ report


def test_report_coverage_and_long_gaps(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    times = sorted(minutes(T0, 10) | minutes(T0 + timedelta(minutes=200), 10))  # 190-minute hole
    store.write(PRODUCT, candles_to_frame([candle(t) for t in times], sec(T0), sec(T0) + 86400))
    r = build_report(store, PRODUCT)
    assert r.rows == 20 and r.first == T0
    assert r.coverage_pct == pytest.approx(100 * 20 / 210, abs=0.01)
    (gap,) = r.gaps_over_60m
    assert gap.minutes == 190
    assert gap.start == T0 + timedelta(minutes=10) and gap.end == T0 + timedelta(minutes=199)


def test_report_without_data(tmp_path: Path) -> None:
    r = build_report(BarStore(tmp_path), PRODUCT)
    assert r.rows == 0 and r.first is None and r.gaps_over_60m == ()


# ------------------------------------------------------------------------------- live (manual only)


@pytest.mark.live
def test_live_public_candles_parse() -> None:
    end = datetime.now(UTC).replace(second=0, microsecond=0)
    start = end - timedelta(hours=2)
    raw: Callable[[], list[dict[str, Any]]] = lambda: CoinbaseCandleSource().fetch(  # noqa: E731
        PRODUCT, sec(start), sec(end)
    )
    df = candles_to_frame(raw(), sec(start), sec(end))
    assert len(df) > 0 and df["ts"].is_monotonic_increasing and (df["high"] >= df["low"]).all()
