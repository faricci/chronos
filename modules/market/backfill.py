"""Historical 1-minute bar store: backfill from Coinbase public candles, and ``load_bars`` (spec sez. 0.5).

Storage: ``<data_dir>/bars/1m/product=<id>/year=<y>/month=<m>.parquet`` with columns
``ts`` (UTC, start of the minute), ``open``, ``high``, ``low``, ``close``, ``volume``.

Minutes with no trade have no candle on the exchange: they are NOT errors and are NOT written. ``load_bars``
rebuilds them as flat bars (OHLC = previous close, volume = 0), the same rule the live ``BarAggregator`` uses.

CLI: ``python -m modules.market.backfill --products BTC-EUR ETH-EUR SOL-EUR --start 2023-10-01``
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import pandas as pd
import requests
import structlog
from aiolimiter import AsyncLimiter
from pydantic import BaseModel, ConfigDict
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)
from tenacity.wait import wait_base

from modules.config import AppConfig, BackfillConfig, load_config_or_exit

log = structlog.get_logger(__name__)

MINUTE = timedelta(minutes=1)
COLUMNS = ["open", "high", "low", "close", "volume"]
GAP_REPORT_MIN = 60  # report holes longer than this many minutes
FLUSH_ROWS = 20_000  # persist the buffer at least this often so an interrupted run loses little
_PARTITION = re.compile(r"year=(\d{4})[/\\]month=(\d{1,2})\.parquet$")

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def floor_minute(t: datetime) -> datetime:
    return t.replace(second=0, microsecond=0)


def parse_utc(value: str) -> datetime:
    """``2023-10-01`` or a full ISO timestamp -> aware UTC datetime."""
    t = datetime.fromisoformat(value)
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)


# ---------------------------------------------------------------------------------------------- source


class CandleSource(Protocol):
    """Blocking source of raw 1m candles for ``[start, end)`` (unix seconds)."""

    def fetch(self, product_id: str, start_s: int, end_s: int) -> list[dict[str, Any]]: ...


class CoinbaseCandleSource:
    """Public candles via ``RESTClient.get_public_candles`` - no API key needed."""

    def __init__(self) -> None:
        from coinbase.rest import RESTClient

        self._client = RESTClient()

    def fetch(self, product_id: str, start_s: int, end_s: int) -> list[dict[str, Any]]:
        resp = self._client.get_public_candles(
            product_id=product_id, start=str(start_s), end=str(end_s), granularity="ONE_MINUTE"
        )
        data: Any = resp.to_dict() if hasattr(resp, "to_dict") else resp
        candles: list[dict[str, Any]] = data.get("candles", [])
        return candles


class StoreCandleSource:
    """A ``CandleSource`` that serves the candles already in the store, in the API's string format.

    Used by the replay (same clock, same ``BarService``, bars from the store) and by tests. ``[start_s, end_s)``.
    """

    def __init__(self, root: Path) -> None:
        self._store = BarStore(root)

    def fetch(self, product_id: str, start_s: int, end_s: int) -> list[dict[str, Any]]:
        lo = datetime.fromtimestamp(start_s, UTC)
        hi = datetime.fromtimestamp(end_s, UTC)
        out: list[dict[str, Any]] = []
        for year, month in self._store.months(product_id):
            if (year, month) < (lo.year, lo.month) or (year, month) > (hi.year, hi.month):
                continue
            df = self._store.read_month(product_id, year, month)
            rows = zip(df["ts"], df["open"], df["high"], df["low"], df["close"], df["volume"], strict=True)
            for ts, o, h, lo_, c, v in rows:
                sec = int(pd.Timestamp(ts).timestamp())
                if start_s <= sec < end_s:
                    out.append(
                        {
                            "start": str(sec),
                            "open": repr(float(o)),
                            "high": repr(float(h)),
                            "low": repr(float(lo_)),
                            "close": repr(float(c)),
                            "volume": repr(float(v)),
                        }
                    )
        return out


def is_transient(exc: BaseException) -> bool:
    """Retry on rate limits (429), server errors (5xx) and network failures; fail fast on other 4xx."""
    if isinstance(exc, requests.exceptions.HTTPError):
        status = exc.response.status_code if exc.response is not None else None
        return status is None or status == 429 or status >= 500
    return isinstance(exc, requests.exceptions.ConnectionError | requests.exceptions.Timeout)


def default_retry_wait() -> wait_base:
    return wait_exponential(multiplier=1, min=1, max=60)


# ------------------------------------------------------------------------------------------- store


def candles_to_frame(candles: Sequence[dict[str, Any]], start_s: int, end_s: int) -> pd.DataFrame:
    """Raw API candles -> frame with ``ts`` + OHLCV, keeping only ``start_s <= ts < end_s``, deduplicated."""
    rows = [
        (int(c["start"]), float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"]), float(c["volume"]))
        for c in candles
        if start_s <= int(c["start"]) < end_s
    ]
    df = pd.DataFrame(rows, columns=["sec", *COLUMNS])
    df["ts"] = pd.to_datetime(df.pop("sec"), unit="s", utc=True).astype("datetime64[ns, UTC]")
    df = df.dropna().drop_duplicates("ts", keep="last").sort_values("ts")
    return df[["ts", *COLUMNS]].reset_index(drop=True)


class BarStore:
    """Month-partitioned Parquet store of real (traded) 1m bars."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _product_dir(self, product_id: str) -> Path:
        return self.root / f"product={product_id}"

    def _path(self, product_id: str, year: int, month: int) -> Path:
        return self._product_dir(product_id) / f"year={year}" / f"month={month}.parquet"

    def months(self, product_id: str) -> list[tuple[int, int]]:
        found = []
        for p in self._product_dir(product_id).glob("year=*/month=*.parquet"):
            m = _PARTITION.search(str(p))
            if m:
                found.append((int(m.group(1)), int(m.group(2))))
        return sorted(found)

    def read_month(self, product_id: str, year: int, month: int) -> pd.DataFrame:
        return pd.read_parquet(self._path(product_id, year, month))

    def write(self, product_id: str, df: pd.DataFrame) -> int:
        """Merge ``df`` into the month files (dedup on ts, newer wins). Atomic per file. Returns rows written."""
        if df.empty:
            return 0
        written = 0
        for (year, month), part in df.groupby([df["ts"].dt.year, df["ts"].dt.month]):
            path = self._path(product_id, int(year), int(month))  # type: ignore[call-overload]
            merged = pd.concat([pd.read_parquet(path), part]) if path.exists() else part
            merged = merged.drop_duplicates("ts", keep="last").sort_values("ts").reset_index(drop=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".parquet.tmp")
            merged.to_parquet(tmp, index=False)
            os.replace(tmp, path)
            written += len(part)
        return written

    def last_ts(self, product_id: str) -> datetime | None:
        months = self.months(product_id)
        if not months:
            return None
        latest: pd.Timestamp = self.read_month(product_id, *months[-1])["ts"].max()
        return latest.to_pydatetime()


def fill_flat_bars(real: pd.DataFrame, start: datetime | None = None, end: datetime | None = None) -> pd.DataFrame:
    """Rebuild a complete 1-minute grid from real bars.

    Missing minutes become flat bars: open = high = low = close = previous close, volume = 0 (same rule as the
    live ``BarAggregator``). The grid runs from ``start`` (default: first real bar) to ``end`` (default: last
    real bar), both inclusive. Minutes before the first known close cannot be built and are dropped.
    """
    if real.empty:
        return pd.DataFrame(columns=COLUMNS, index=pd.DatetimeIndex([], tz="UTC", name="ts"))
    bars = real.set_index("ts")[COLUMNS].sort_index()
    first, last_real = bars.index[0], bars.index[-1]
    hi = pd.Timestamp(end) if end is not None else last_real
    bars = bars.reindex(pd.date_range(first, max(last_real, hi), freq="1min"))
    prev_close = bars["close"].ffill()
    flat = bars["close"].isna()
    for col in ("open", "high", "low", "close"):
        bars.loc[flat, col] = prev_close[flat]
    bars.loc[flat, "volume"] = 0.0
    bars.index.name = "ts"
    lo = pd.Timestamp(start) if start is not None else first
    return bars.loc[(bars.index >= lo) & (bars.index <= hi)]


def load_bars(
    root: Path,
    product_id: str,
    start: datetime | None = None,
    end: datetime | None = None,
    *,
    store_complete_until_end: bool = False,
) -> pd.DataFrame:
    """Complete 1m OHLCV frame (UTC ``ts`` index) for ``[start, end]``, flat bars included.

    ``root`` is ``<data_dir>/bars/1m``. Open bounds default to the stored range. The last real bar before
    ``start`` is used as the previous close so that a range starting inside a quiet stretch is still exact.

    ``end`` is clamped to the last stored real bar: past it we cannot tell a quiet market from data that was
    never downloaded, and inventing flat bars there would silently corrupt features and labels. Pass
    ``store_complete_until_end=True`` only when the caller knows the store is complete up to ``end`` (the live
    recorder), to get trailing flat bars.
    """
    store = BarStore(root)
    months = store.months(product_id)
    if not months:
        return fill_flat_bars(pd.DataFrame(columns=["ts", *COLUMNS]))
    s_key = (start.year, start.month) if start else months[0]
    e_key = (end.year, end.month) if end else months[-1]
    wanted = [m for m in months if s_key <= m <= e_key]
    carry = [m for m in months if m < s_key][-1:]  # latest earlier month: only its last bar is needed
    frames = [store.read_month(product_id, *m) for m in wanted]
    if start is not None and carry:
        frames.insert(0, store.read_month(product_id, *carry[0]).tail(1))
    if not frames:
        return fill_flat_bars(pd.DataFrame(columns=["ts", *COLUMNS]))
    real = pd.concat(frames).drop_duplicates("ts").sort_values("ts")
    if end is not None:
        real = real[real["ts"] <= pd.Timestamp(end)]
    if real.empty:
        return fill_flat_bars(real)
    if end is not None and not store_complete_until_end:
        end = min(end, real["ts"].iloc[-1].to_pydatetime())
    return fill_flat_bars(real.reset_index(drop=True), start, end)


# ------------------------------------------------------------------------------------------- report


class Gap(BaseModel):
    model_config = ConfigDict(frozen=True)

    start: datetime
    end: datetime
    minutes: int


class ProductReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    product_id: str
    rows: int
    first: datetime | None
    last: datetime | None
    coverage_pct: float
    gaps_over_60m: tuple[Gap, ...]
    new_rows: int = 0


def build_report(store: BarStore, product_id: str, new_rows: int = 0) -> ProductReport:
    months = store.months(product_id)
    if not months:
        return ProductReport(
            product_id=product_id, rows=0, first=None, last=None, coverage_pct=0.0, gaps_over_60m=(), new_rows=0
        )
    ts = pd.concat([store.read_month(product_id, *m)["ts"] for m in months]).sort_values().reset_index(drop=True)
    first, last = ts.iloc[0], ts.iloc[-1]
    span_minutes = int((last - first) / MINUTE) + 1
    deltas = ts.diff().dropna()
    big = deltas[deltas > pd.Timedelta(minutes=GAP_REPORT_MIN)]
    gaps = tuple(
        Gap(
            start=(ts[i - 1] + MINUTE).to_pydatetime(),
            end=(ts[i] - MINUTE).to_pydatetime(),
            minutes=int(d / MINUTE) - 1,
        )
        for i, d in big.items()
    )
    return ProductReport(
        product_id=product_id,
        rows=len(ts),
        first=first.to_pydatetime(),
        last=last.to_pydatetime(),
        coverage_pct=round(100.0 * len(ts) / span_minutes, 2),
        gaps_over_60m=gaps,
        new_rows=new_rows,
    )


# ----------------------------------------------------------------------------------------- backfill


@dataclass
class Backfiller:
    """Fills the store from the exchange, forward from the most recent stored minute."""

    store: BarStore
    source: CandleSource
    limiter: AsyncLimiter
    cfg: BackfillConfig
    clock: Clock = utc_now
    retry_wait: wait_base | None = None
    settle_s: float = 120.0  # a minute is downloaded only once its REST candle is final (market.bars.rest_delay_s)

    async def _fetch(self, product_id: str, start_s: int, end_s: int) -> list[dict[str, Any]]:
        retrying = AsyncRetrying(
            retry=retry_if_exception(is_transient),
            stop=stop_after_attempt(self.cfg.max_retries),
            wait=self.retry_wait or default_retry_wait(),
            reraise=True,
            before_sleep=lambda rs: log.warning(
                "candle_fetch_retry",
                product=product_id,
                attempt=rs.attempt_number,
                error=str(rs.outcome.exception() if rs.outcome else None),
            ),
        )
        async for attempt in retrying:
            with attempt:
                async with self.limiter:
                    return await asyncio.to_thread(self.source.fetch, product_id, start_s, end_s)
        raise AssertionError("unreachable")  # pragma: no cover

    async def run_product(self, product_id: str, start: datetime) -> ProductReport:
        last = self.store.last_ts(product_id)
        cursor = floor_minute(start.astimezone(UTC)) if last is None else last + MINUTE
        # exclusive: a minute is fetched only when ts + 1 min + S <= now, i.e. when its candle is final. The live
        # BarService applies the same rule, so history and runtime read the same version of every candle.
        end = floor_minute(self.clock() - timedelta(seconds=self.settle_s))
        window = timedelta(minutes=self.cfg.window_minutes)
        buffer: list[pd.DataFrame] = []
        buffered = new_rows = 0

        def flush() -> int:
            nonlocal buffer, buffered
            n = self.store.write(product_id, pd.concat(buffer, ignore_index=True)) if buffer else 0
            buffer, buffered = [], 0
            return n

        try:
            while cursor < end:
                upper = min(cursor + window, end)
                start_s, end_s = int(cursor.timestamp()), int(upper.timestamp())
                df = candles_to_frame(await self._fetch(product_id, start_s, end_s), start_s, end_s)
                if not df.empty:
                    buffer.append(df)
                    buffered += len(df)
                    if buffered >= FLUSH_ROWS:
                        new_rows += flush()
                cursor = upper
        finally:  # an interrupted run keeps what it downloaded; the next run resumes after the last stored minute
            new_rows += flush()
        report = build_report(self.store, product_id, new_rows)
        log.info(
            "backfill_done",
            product=product_id,
            rows=report.rows,
            new_rows=new_rows,
            coverage_pct=report.coverage_pct,
            gaps_over_60m=len(report.gaps_over_60m),
        )
        return report

    async def run(self, products: Sequence[str], start: datetime) -> list[ProductReport]:
        return list(await asyncio.gather(*(self.run_product(p, start) for p in products)))


def format_report(reports: Sequence[ProductReport], top_gaps: int = 5) -> str:
    lines = []
    for r in reports:
        first = r.first.strftime("%Y-%m-%d %H:%M") if r.first else "-"
        last = r.last.strftime("%Y-%m-%d %H:%M") if r.last else "-"
        lines.append(
            f"{r.product_id}: {r.rows} rows (+{r.new_rows} new), {first} -> {last} UTC, "
            f"coverage {r.coverage_pct:.2f}%, {len(r.gaps_over_60m)} gaps > {GAP_REPORT_MIN} min"
        )
        for g in sorted(r.gaps_over_60m, key=lambda g: g.minutes, reverse=True)[:top_gaps]:
            lines.append(f"    gap {g.minutes:>7} min  {g.start:%Y-%m-%d %H:%M} -> {g.end:%Y-%m-%d %H:%M}")
    return "\n".join(lines)


def bars_root(cfg: AppConfig) -> Path:
    return Path(cfg.paths.data_dir) / "bars" / "1m"


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Backfill 1m candles from Coinbase public endpoints")
    ap.add_argument("--products", nargs="+", help="default: market.products of the config")
    ap.add_argument("--start", help="ISO date, used only for products with nothing stored (default: config)")
    ap.add_argument("--config", default=None, help="optional YAML config; defaults are used if omitted")
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--rate", type=float, default=None, help="requests per second, shared by all products")
    args = ap.parse_args(argv)

    cfg = load_config_or_exit(args.config) if args.config else AppConfig()
    products = args.products or list(cfg.market.products)
    start = parse_utc(args.start or cfg.market.backfill.start)
    root = Path(args.data_dir) / "bars" / "1m" if args.data_dir else bars_root(cfg)
    rate = args.rate or cfg.market.backfill.rate_limit_per_s

    backfiller = Backfiller(
        store=BarStore(root),
        source=CoinbaseCandleSource(),
        limiter=AsyncLimiter(rate, 1),
        cfg=cfg.market.backfill,
        settle_s=cfg.market.bars.rest_delay_s,
    )
    reports = asyncio.run(backfiller.run(products, start))
    print(format_report(reports))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
