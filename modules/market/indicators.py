"""Incremental market features for the live loop (spec sez. 2.2.1.b, docs/decisions/2026-10-17-indicatori.md).

``IndicatorEngine`` is the runtime twin of ``indicators_batch.compute_indicators``: one 1m bar in, O(1) work out,
and on every row the same value as the batch code (tolerance ``|a-b| <= 1e-9 * max(1, |b|)``, identical NaN
masks). Rules that make this true:

- higher-timeframe series (1h, 6h, 1d) are updated only when ``TimeframeAggregator`` returns a CLOSED bar, so a
  value changes exactly on the 1m row of its last minute and nothing provisional ever enters (vincolo C7);
- recurrences (Wilder, EMA) use SMA seeds and the literal formulas of the spec, like the batch code;
- scalar formulas are imported from ``indicators_batch`` so the two sides cannot drift;
- the anchor is the first bar ever ingested. At startup ``warm_up`` replays the product's WHOLE history through
  the same ``_ingest``: a partial warm-up changes Wilder/EMA values (the difference decays only as (13/14)^k).
"""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal

import pandas as pd
import structlog
from sortedcontainers import SortedList

from modules.market.indicator_defs import (
    ATR_N,
    BB_N,
    FEATURE_COLUMNS,
    HHLL_WIN_MIN,
    HTF_TIMEFRAMES,
    MACD_FAST,
    MACD_SIGNAL,
    MACD_SLOW,
    PCTILE_DAYS,
    PCTILE_MIN_N,
    RET_WINDOWS_MIN,
    RSI_N,
    RV_1D_BARS,
    RV_30D_BARS,
    SEQ_1D_COLUMNS,
    SEQ_1D_LEN,
    SEQ_1H_COLUMNS,
    SEQ_1H_LEN,
    VOLZ_DAYS,
    VWAP_WIN_MIN,
)
from modules.market.indicators_batch import bb_pos, bb_stats, rsi_value, rvd_value, zscore_value
from modules.market.timeframes import Bar, BarGapError, TimeframeAggregator

log = structlog.get_logger(__name__)

NAN: Final = float("nan")
_MINUTE = timedelta(minutes=1)
WARMUP_BUDGET_S: Final = 120.0  # per product on ~3 years of 1m bars (spec 2.3 A.4); exceeding it needs a new CONFLITTO


# ------------------------------------------------------------------------------------ small states


class WilderState:
    """Wilder smoothing: SMA of the first ``n`` inputs, then ``x = (x * (n-1) + u) / n``. NaN until seeded."""

    def __init__(self, n: int) -> None:
        self._n = n
        self._count = 0
        self._acc = 0.0
        self._x = NAN

    def update(self, u: float) -> float:
        if self._count < self._n:
            self._acc += u
            self._count += 1
            if self._count == self._n:
                self._x = self._acc / self._n
                return self._x
            return NAN
        self._x = (self._x * (self._n - 1) + u) / self._n
        return self._x


class EmaState:
    """EMA seeded with the SMA of the first ``n`` inputs, ``alpha = 2/(n+1)``. NaN until seeded."""

    def __init__(self, n: int) -> None:
        self._n = n
        self._alpha = 2.0 / (n + 1)
        self._count = 0
        self._acc = 0.0
        self._e = NAN

    def update(self, x: float) -> float:
        if self._count < self._n:
            self._acc += x
            self._count += 1
            if self._count == self._n:
                self._e = self._acc / self._n
                return self._e
            return NAN
        self._e = self._alpha * x + (1.0 - self._alpha) * self._e
        return self._e


class RollingSum:
    """Sum of the last ``window`` values by add/subtract, kept exact enough to read.

    Adding and subtracting leaves a rounding residue, and after a large value leaves the window the residue can
    dwarf what is left (a day with a single traded minute). ``_err`` is an upper bound of that residue; ``value``
    recomputes the sum exactly with ``math.fsum`` whenever the bound exceeds ``REL_TOL`` of the sum. ``resum``
    is also called once a day. In normal conditions a read costs O(1); an exact recompute is O(window) and
    happens rarely. Readers must use ``value``; the number returned by ``update`` is only the raw running sum.
    """

    REL_TOL: Final = 1e-14
    _EPS: Final = 1.2e-16  # unit roundoff of a double, rounded up

    def __init__(self, window: int) -> None:
        self._window = window
        self._buf: deque[float] = deque()
        self._sum = 0.0
        self._err = 0.0

    def update(self, x: float) -> float:
        self._buf.append(x)
        self._sum += x
        self._err += self._EPS * abs(self._sum)
        if len(self._buf) > self._window:
            self._sum -= self._buf.popleft()
            self._err += self._EPS * abs(self._sum)
        return self._sum if len(self._buf) == self._window else NAN

    @property
    def full(self) -> bool:
        return len(self._buf) == self._window

    @property
    def value(self) -> float:
        if not self.full:
            return NAN
        if self._err > self.REL_TOL * abs(self._sum):
            self.resum()
        return self._sum

    def resum(self) -> None:
        self._sum = math.fsum(self._buf)
        self._err = 0.0


class RollingExtreme:
    """Max or min of the last ``window`` values (monotonic deque, exact). NaN until the window is full."""

    def __init__(self, window: int, mode: Literal["max", "min"]) -> None:
        self._window = window
        self._is_max = mode == "max"
        self._d: deque[tuple[int, float]] = deque()

    def update(self, i: int, x: float) -> float:
        d = self._d
        if self._is_max:
            while d and d[-1][1] <= x:
                d.pop()
        else:
            while d and d[-1][1] >= x:
                d.pop()
        d.append((i, x))
        while d[0][0] <= i - self._window:
            d.popleft()
        return d[0][1] if i >= self._window - 1 else NAN


class PrevPercentile:
    """Percentile (0..100) of ``x`` among the previous ``window`` available values, ``x`` excluded, ties ``<=``."""

    def __init__(self, window: int, min_n: int) -> None:
        self._window = window
        self._min_n = min_n
        self._positions: deque[float] = deque()  # NaN keeps its position so the window is by bars, not by count
        self._sorted: SortedList[float] = SortedList()

    def update(self, x: float) -> float:
        n = len(self._sorted)
        pct = NAN if math.isnan(x) or n < self._min_n else 100.0 * self._sorted.bisect_right(x) / n
        self._positions.append(x)
        if not math.isnan(x):
            self._sorted.add(x)
        if len(self._positions) > self._window:
            old = self._positions.popleft()
            if not math.isnan(old):
                self._sorted.remove(old)
        return pct


class PrevZScore:
    """z-score of ``y`` against the previous ``window`` values (ddof=1), ``y`` excluded. NaN until full."""

    def __init__(self, window: int) -> None:
        self._window = window
        self._prev: deque[float] = deque(maxlen=window)

    def update(self, y: float) -> float:
        z = zscore_value(list(self._prev), y) if len(self._prev) == self._window else NAN
        self._prev.append(y)
        return z


# ----------------------------------------------------------------------------------------- snapshot


@dataclass(frozen=True, slots=True)
class IndicatorSnapshot:
    """Features of one 1m row. NaN / ``None`` = not available. Values are in bps, unrounded."""

    product_id: str
    ts: datetime  # start of the 1m bar
    t: datetime  # ts + 1 min: the decision instant
    ret_15m_bps: float
    ret_1h_bps: float
    ret_6h_bps: float
    ret_1d_bps: float
    ret_3d_bps: float
    ret_7d_bps: float
    dist_vwap_1d_bps: float
    bb_pos_1d: float
    hh_ll_pos_30d: float
    rsi_14_1h: float
    rsi_14_6h: float
    rsi_14_1d: float
    macd_hist_1d_bps: float
    atr_14_1h: float
    atr_14_6h: float
    atr_14_1d: float
    atr_14_1h_bps: float
    atr_14_6h_bps: float
    atr_14_1d_bps: float
    rv_1d_bps: float
    rv_30d_bps: float
    rv_ratio_1d_30d: float
    atr_1d_pctile_365d: float
    vol_z_1d: float
    seq_1h_bps: tuple[float, ...] | None
    seq_1d_bps: tuple[float, ...] | None

    def as_row(self) -> dict[str, float]:
        """Keys = ``FEATURE_COLUMNS``; the two sequences are flattened (all NaN when ``None``)."""
        row: dict[str, float] = {}
        for col in FEATURE_COLUMNS:
            if col.startswith("seq_1h_bps_"):
                row[col] = NAN if self.seq_1h_bps is None else self.seq_1h_bps[SEQ_1H_COLUMNS.index(col)]
            elif col.startswith("seq_1d_bps_"):
                row[col] = NAN if self.seq_1d_bps is None else self.seq_1d_bps[SEQ_1D_COLUMNS.index(col)]
            else:
                row[col] = getattr(self, col)
        return row

    @classmethod
    def from_row(cls, product_id: str, ts: datetime, row: Mapping[str, float]) -> IndicatorSnapshot:
        """Build a snapshot from a ``compute_indicators`` row, so training and serving share one path."""

        def seq(cols: tuple[str, ...]) -> tuple[float, ...] | None:
            values = tuple(float(row[c]) for c in cols)
            return None if any(math.isnan(v) for v in values) else values

        scalars = {c: float(row[c]) for c in FEATURE_COLUMNS if not c.startswith("seq_")}
        return cls(
            product_id=product_id,
            ts=ts,
            t=ts + _MINUTE,
            seq_1h_bps=seq(SEQ_1H_COLUMNS),
            seq_1d_bps=seq(SEQ_1D_COLUMNS),
            **scalars,
        )


# --------------------------------------------------------------------------------------- the engine


class _TfState:
    """Wilder ATR and RSI of one timeframe, advanced only by closed bars."""

    def __init__(self) -> None:
        self.prev_close: float | None = None
        self.atr = WilderState(ATR_N)
        self.avg_gain = WilderState(RSI_N)
        self.avg_loss = WilderState(RSI_N)
        self.atr_value = NAN
        self.atr_bps = NAN
        self.rsi = NAN


class IndicatorEngine:
    def __init__(self, product_id: str) -> None:
        self.product_id = product_id
        self._row = -1
        self._ts: datetime | None = None
        self._closes: deque[float] = deque(maxlen=max(RET_WINDOWS_MIN.values()) + 1)
        self._pv = RollingSum(VWAP_WIN_MIN)
        self._vol = RollingSum(VWAP_WIN_MIN)
        self._traded = RollingSum(VWAP_WIN_MIN)  # sums of 1.0/0.0: an exact integer count
        self._hh = RollingExtreme(HHLL_WIN_MIN, "max")
        self._ll = RollingExtreme(HHLL_WIN_MIN, "min")
        self._hh_value = NAN
        self._ll_value = NAN
        self._pv_day: list[float] = []
        self._agg = {tf: TimeframeAggregator(tf) for tf in HTF_TIMEFRAMES}
        self._tf = {tf: _TfState() for tf in HTF_TIMEFRAMES}
        # 1h
        self._r2: deque[float] = deque(maxlen=RV_30D_BARS)
        self._ret1_1h: deque[float] = deque(maxlen=SEQ_1H_LEN)
        self._rv_1d = NAN
        self._rv_30d = NAN
        # 1d
        self._ema_fast = EmaState(MACD_FAST)
        self._ema_slow = EmaState(MACD_SLOW)
        self._ema_signal = EmaState(MACD_SIGNAL)
        self._macd_hist_bps = NAN
        self._bb_closes: deque[float] = deque(maxlen=BB_N)
        self._bb_m = NAN
        self._bb_sd = NAN
        self._pctile = PrevPercentile(PCTILE_DAYS, PCTILE_MIN_N)
        self._atr_pctile = NAN
        self._volz = PrevZScore(VOLZ_DAYS)
        self._vol_z = NAN
        self._ret1_1d: deque[float] = deque(maxlen=SEQ_1D_LEN)

    # ---- public API

    def warm_up(self, bars_1m: pd.DataFrame) -> None:
        """Replay a whole 1m history through ``_ingest`` (no snapshots). Same code path as live, on purpose."""
        started = time.monotonic()
        stamps = pd.DatetimeIndex(bars_1m.index).to_pydatetime()
        cols = (bars_1m[k].to_numpy(dtype="float64").tolist() for k in ("open", "high", "low", "close", "volume"))
        for ts, o, h, lo, c, v in zip(stamps, *cols, strict=True):
            self._ingest(Bar(ts, o, h, lo, c, v))
        elapsed = time.monotonic() - started
        log.info("indicator_warmup", product=self.product_id, rows=len(bars_1m), indicator_warmup_s=round(elapsed, 2))
        if elapsed > WARMUP_BUDGET_S:
            log.warning("indicator_warmup_over_budget", product=self.product_id, budget_s=WARMUP_BUDGET_S)

    def update(self, bar: Bar) -> IndicatorSnapshot:
        self._ingest(bar)
        return self.snapshot()

    def snapshot(self) -> IndicatorSnapshot:
        if self._ts is None:
            raise ValueError("no bar ingested yet")
        c = self._closes[-1]
        ret = {}
        for name, w in RET_WINDOWS_MIN.items():
            ret[name] = 1e4 * (c / self._closes[-1 - w] - 1.0) if len(self._closes) > w else NAN
        dist_vwap = NAN
        if self._pv.full and self._traded.value > 0.0:
            dist_vwap = 1e4 * (c / (self._pv.value / self._vol.value) - 1.0)
        hh, ll = self._hh_value, self._ll_value
        hh_ll = NAN if math.isnan(hh) or hh == ll else (c - ll) / (hh - ll)
        h1, h6, d1 = self._tf["1h"], self._tf["6h"], self._tf["1d"]
        return IndicatorSnapshot(
            product_id=self.product_id,
            ts=self._ts,
            t=self._ts + _MINUTE,
            ret_15m_bps=ret["15m"],
            ret_1h_bps=ret["1h"],
            ret_6h_bps=ret["6h"],
            ret_1d_bps=ret["1d"],
            ret_3d_bps=ret["3d"],
            ret_7d_bps=ret["7d"],
            dist_vwap_1d_bps=dist_vwap,
            bb_pos_1d=bb_pos(c, self._bb_m, self._bb_sd),
            hh_ll_pos_30d=hh_ll,
            rsi_14_1h=h1.rsi,
            rsi_14_6h=h6.rsi,
            rsi_14_1d=d1.rsi,
            macd_hist_1d_bps=self._macd_hist_bps,
            atr_14_1h=h1.atr_value,
            atr_14_6h=h6.atr_value,
            atr_14_1d=d1.atr_value,
            atr_14_1h_bps=h1.atr_bps,
            atr_14_6h_bps=h6.atr_bps,
            atr_14_1d_bps=d1.atr_bps,
            rv_1d_bps=self._rv_1d,
            rv_30d_bps=self._rv_30d,
            rv_ratio_1d_30d=self._rv_1d / self._rv_30d if self._rv_30d != 0.0 else NAN,
            atr_1d_pctile_365d=self._atr_pctile,
            vol_z_1d=self._vol_z,
            seq_1h_bps=tuple(self._ret1_1h) if len(self._ret1_1h) == SEQ_1H_LEN else None,
            seq_1d_bps=tuple(self._ret1_1d) if len(self._ret1_1d) == SEQ_1D_LEN else None,
        )

    # ---- ingestion

    def _ingest(self, bar: Bar) -> None:
        if self._ts is not None and bar.ts != self._ts + _MINUTE:
            raise BarGapError(f"expected minute {(self._ts + _MINUTE).isoformat()}, got {bar.ts.isoformat()}")
        self._row += 1
        self._ts = bar.ts
        c, h, lo, v = bar.close, bar.high, bar.low, bar.volume
        self._closes.append(c)
        pv = (h + lo + c) / 3.0 * v
        self._pv.update(pv)
        self._vol.update(v)
        self._traded.update(1.0 if v > 0.0 else 0.0)
        self._hh_value = self._hh.update(self._row, h)
        self._ll_value = self._ll.update(self._row, lo)
        if bar.ts.hour == 0 and bar.ts.minute == 0:
            self._pv_day = []
        self._pv_day.append(pv)
        for tf in HTF_TIMEFRAMES:
            closed = self._agg[tf].update(bar)
            if closed is not None:
                self._on_htf_bar(tf, closed)

    def _on_htf_bar(self, tf: str, bar: Bar) -> None:
        st = self._tf[tf]
        c, pc = bar.close, st.prev_close
        tr = bar.high - bar.low if pc is None else max(bar.high - bar.low, abs(bar.high - pc), abs(bar.low - pc))
        st.atr_value = st.atr.update(tr)
        st.atr_bps = 1e4 * st.atr_value / c
        ret1 = NAN
        if pc is not None:
            d = c - pc
            st.rsi = rsi_value(st.avg_gain.update(d if d > 0 else 0.0), st.avg_loss.update(-d if d < 0 else 0.0))
            ret1 = 1e4 * (c / pc - 1.0)
        st.prev_close = c
        if tf == "1h":
            self._on_hour(c, pc, ret1)
        elif tf == "1d":
            self._on_day(c, ret1, st)

    def _on_hour(self, c: float, pc: float | None, ret1: float) -> None:
        if pc is None:
            return
        r = math.log(c / pc)
        self._r2.append(r * r)
        self._ret1_1h.append(ret1)
        if len(self._r2) >= RV_1D_BARS:
            self._rv_1d = rvd_value(list(self._r2)[-RV_1D_BARS:], RV_1D_BARS)
        if len(self._r2) >= RV_30D_BARS:
            self._rv_30d = rvd_value(list(self._r2), RV_30D_BARS)

    def _on_day(self, c: float, ret1: float, st: _TfState) -> None:
        fast, slow = self._ema_fast.update(c), self._ema_slow.update(c)
        hist = NAN
        if not (math.isnan(fast) or math.isnan(slow)):
            macd = fast - slow
            hist = macd - self._ema_signal.update(macd)
        self._macd_hist_bps = 1e4 * hist / c
        self._bb_closes.append(c)
        if len(self._bb_closes) == BB_N:
            self._bb_m, self._bb_sd = bb_stats(list(self._bb_closes))
        self._atr_pctile = self._pctile.update(st.atr_bps)
        self._vol_z = self._volz.update(math.log(1.0 + math.fsum(self._pv_day)))
        if not math.isnan(ret1):
            self._ret1_1d.append(ret1)
        # exact recomputation of the incremental sums once a day: no drift in a process that runs for months
        self._pv.resum()
        self._vol.resum()
        self._traded.resum()
