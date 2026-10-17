"""Higher-timeframe bars built from contiguous 1-minute bars (spec sez. 2.3 punto A.3).

Buckets are aligned to the UTC epoch: 1h on the hour, 6h at 00/06/12/18, 1d at 00:00 UTC. A higher-timeframe
bar exists only once ALL its minutes have arrived: the bar still forming is never exposed, which is what keeps
features point-in-time (spec C7).

Two implementations of the same rule, kept in this file so they cannot drift apart:
- ``aggregate_bars``: vectorized, used to build training data from the Parquet history;
- ``TimeframeAggregator``: O(1) per minute, used live by the ``IndicatorEngine``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

import pandas as pd

TIMEFRAME_SECONDS: Final[dict[str, int]] = {"1m": 60, "15m": 900, "1h": 3600, "6h": 21600, "1d": 86400}
_PANDAS_FREQ: Final[dict[str, str]] = {"1m": "1min", "15m": "15min", "1h": "1h", "6h": "6h", "1d": "1D"}
OHLCV: Final = ["open", "high", "low", "close", "volume"]


class BarGapError(ValueError):
    """Input bars are not contiguous 1-minute bars (a minute is missing, duplicated or out of order)."""


@dataclass(frozen=True, slots=True)
class Bar:
    """One OHLCV bar; ``ts`` is the start of the interval (UTC)."""

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


def bucket_start(ts: datetime, seconds: int) -> datetime:
    """Start of the epoch-aligned bucket of ``seconds`` containing ``ts``."""
    epoch = int(ts.timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, tz=UTC)


def _check_timeframe(tf: str) -> int:
    try:
        return TIMEFRAME_SECONDS[tf]
    except KeyError:
        raise ValueError(f"unknown timeframe {tf!r}, expected one of {sorted(TIMEFRAME_SECONDS)}") from None


def aggregate_bars(bars_1m: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Complete ``tf`` bars from a contiguous 1m frame (UTC ``ts`` index, columns OHLCV).

    Incomplete buckets (the first and last one when the data does not start/end on a boundary) are dropped.
    Raises ``BarGapError`` if the input is not contiguous: build it with ``load_bars`` (flat bars included).
    """
    seconds = _check_timeframe(tf)
    if bars_1m.empty:
        return pd.DataFrame(columns=OHLCV, index=pd.DatetimeIndex([], tz="UTC", name="ts"))
    idx = bars_1m.index
    if len(idx) > 1 and not (idx[1:] - idx[:-1] == pd.Timedelta(minutes=1)).all():
        raise BarGapError("1m bars are not contiguous")
    if tf == "1m":
        return bars_1m[OHLCV].copy()
    grouped = bars_1m.groupby(pd.DatetimeIndex(idx).floor(_PANDAS_FREQ[tf]))
    out = grouped.agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"),
        volume=("volume", "sum"), n=("close", "size"),
    )
    out = out[out["n"] == seconds // 60].drop(columns="n")
    out.index.name = "ts"
    return out


class TimeframeAggregator:
    """Incremental twin of :func:`aggregate_bars` for one timeframe.

    ``update`` takes the next contiguous 1m bar and returns the completed ``tf`` bar when that minute is the
    last of its bucket, otherwise ``None``. A join in the middle of a bucket (cold start) yields nothing for
    that first partial bucket, exactly like ``aggregate_bars`` drops it.
    """

    def __init__(self, tf: str) -> None:
        self.tf = tf
        self._seconds = _check_timeframe(tf)
        self._expected: datetime | None = None
        self._open_bucket: datetime | None = None
        self._acc: Bar | None = None
        self._complete_from_start = False

    def update(self, bar: Bar) -> Bar | None:
        if self._expected is not None and bar.ts != self._expected:
            raise BarGapError(f"expected minute {self._expected.isoformat()}, got {bar.ts.isoformat()}")
        self._expected = bar.ts + timedelta(minutes=1)

        start = bucket_start(bar.ts, self._seconds)
        if start != self._open_bucket:  # first minute of a new bucket
            self._open_bucket = start
            self._complete_from_start = bar.ts == start
            self._acc = Bar(start, bar.open, bar.high, bar.low, bar.close, bar.volume)
        else:
            a = self._acc
            assert a is not None
            self._acc = Bar(start, a.open, max(a.high, bar.high), min(a.low, bar.low), bar.close, a.volume + bar.volume)

        if self._expected == start + timedelta(seconds=self._seconds):
            done, self._acc = self._acc, None
            return done if self._complete_from_start else None
        return None
