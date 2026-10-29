"""Pre-registered study: multi-month drawdown + abnormal volume -> 7/14/30 day spot return (offline research).

::

    python -m script.study_volume_patterns fetch-usd --products BTC-USD ETH-USD --out data/external/prices
    python -m script.study_volume_patterns run --arm {E,L} --bars-root data/bars/1m [--usd-daily data/external/prices] \
        --exploration-range {unknown|START END} --out data/reports/study_volume

Specification: docs/decisions/2026-10-26-studio-pattern-volume-prereg.md (section 14), committed BEFORE this code. Every
parameter is in ``PREREG`` (its sha256 goes in ``00_preregistration.json``); decision rules are in the document and are
not changed after the data are seen. Read-only on the data; no order, no balance, no trading module is imported
(only ``load_bars`` / ``BarStore`` of the bar store). One run per arm: ``<out>/<arm>/`` must not exist, and the run is
refused (exit 2) if this file has uncommitted changes. Nothing is printed about returns before ``06_decision.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import pandas as pd
from scipy import stats as sps

from modules.market.backfill import BarStore, load_bars

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_REL = "script/study_volume_patterns.py"
DOC_REL = "docs/decisions/2026-10-26-studio-pattern-volume-prereg.md"
SCHEMA = "chronos.study_volume.v1"
EXIT_REFUSED = 2

# Terms of use of Coinbase: the pages answered HTTP 403 to automated clients on 2026-10-26 and were NOT read (the
# study uses only public daily candles, internally, without redistribution; see section 2.3 of the document).
COINBASE_TERMS = {
    "checked_on": "2026-10-26",
    "urls": [
        "https://www.coinbase.com/legal/market_data_terms",
        "https://www.coinbase.com/legal/developer-platform/terms-of-service",
    ],
    "terms_verified": False,
    "note": "pages answer HTTP 403 to automated clients; public daily candles, internal research, no redistribution",
}
CANDLES_URL = "https://api.coinbase.com/api/v3/brokerage/market/products/{pid}/candles"

PREREG: dict[str, Any] = {
    "schema": SCHEMA,
    "drawdown_windows": [90, 180, 365],
    "primary_drawdown_window": 180,
    "volume_recent_days": 7,
    "volume_base_days": 90,
    "volume_base_lag": 7,
    "volume_trend_days": 30,
    "vol_days": 30,
    "rebound_days": 30,
    "ddmin_days": 30,
    "quantiles": {"dd": 0.20, "vol_high": 0.80, "vol_low": 0.30, "rebound": 0.70, "s_med": 0.50},
    "separation_days": 15,
    "min_units": {"train": 10, "holdout": 6, "coin_episodes": 3, "replica": 3},
    "horizons": [7, 14, 30],
    "bootstrap": {"B": 10000, "seed": 20261026, "tail": 0.05, "calendar_block_factor": 2},
    "seed_index": "1000 * variant_index + 3 * horizon_index + split_index (splits: train, holdout, replica)",
    "costs": {"primary": 0.0077, "secondary": 0.0110},
    "alpha": 0.05,
    "holm_family": 6,
    "holdout_start": {"E": "2025-09-01", "L": "2021-01-01"},
    "arm_L": {
        "holdout_end": "2023-09-30",
        "products": ["BTC-USD", "ETH-USD"],
        "replica_products": ["BTC-EUR", "ETH-EUR"],
        "granularity": "ONE_DAY",
    },
    "arm_E": {"products": ["BTC-EUR", "ETH-EUR", "SOL-EUR"]},
    "validity": {"min_fraction": 0.95, "max_gap_minutes": 360},
    "entry_delay_days": 1,
    "entry_delay_days_secondary": 2,
}
HORIZONS: tuple[int, ...] = tuple(PREREG["horizons"])
SEP = int(PREREG["separation_days"])
MIN_T = int(PREREG["min_units"]["train"])
MIN_V = int(PREREG["min_units"]["holdout"])
MIN_COIN = int(PREREG["min_units"]["coin_episodes"])
MIN_R = int(PREREG["min_units"]["replica"])
K_PRIMARY = float(PREREG["costs"]["primary"])
K_SECONDARY = float(PREREG["costs"]["secondary"])
ALPHA = float(PREREG["alpha"])
BOOT_B = int(PREREG["bootstrap"]["B"])
BOOT_SEED = int(PREREG["bootstrap"]["seed"])
SPLIT_ORDER = ("train", "holdout", "replica")
DAILY_COLS = ["open", "high", "low", "close", "V", "Vb"]


# ------------------------------------------------------------------------------------------------ daily series


def _day_range(first: pd.Timestamp, last: pd.Timestamp) -> pd.DatetimeIndex:
    return pd.date_range(first, last, freq="D")


def _finish_daily(daily: pd.DataFrame, valid: pd.Series, first: pd.Timestamp, last: pd.Timestamp) -> pd.DataFrame:
    """Reindex on every calendar day; an invalid or missing day has NaN in every column."""
    idx = _day_range(first, last)
    out = daily.reindex(idx)
    ok = valid.reindex(idx).fillna(False).astype(bool)
    out.loc[~ok.to_numpy(), DAILY_COLS] = np.nan
    out["valid"] = ok.to_numpy()
    out.index.name = "day"
    return out


def _naive_utc(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    return index.tz_convert("UTC").tz_localize(None) if index.tz is not None else index


def daily_from_1m(bars: pd.DataFrame, real_ts: pd.DatetimeIndex) -> pd.DataFrame:
    """UTC daily bars from the complete 1m grid (flat bars included) and the timestamps of the REAL bars.

    ``open`` = first minute open, ``high``/``low`` = extremes, ``close`` = last minute close, ``V`` = sum of
    close x volume (EUR), ``Vb`` = sum of volume. A day is invalid if it contains a stretch of MORE than 360 consecutive
    minutes without a real bar. The day in progress (grid not reaching 23:59) is not used.
    """
    idx = _naive_utc(pd.DatetimeIndex(bars.index))
    b = bars.copy()
    b.index = idx
    day = b.index.normalize()
    grouped = b.groupby(day)
    daily = pd.DataFrame(
        {
            "open": grouped["open"].first(),
            "high": grouped["high"].max(),
            "low": grouped["low"].min(),
            "close": grouped["close"].last(),
            "V": (b["close"] * b["volume"]).groupby(day).sum(),
            "Vb": grouped["volume"].sum(),
        }
    )
    last_ts = idx[-1]
    complete = (
        last_ts.normalize()
        if (last_ts.hour == 23 and last_ts.minute == 59)
        else last_ts.normalize() - pd.Timedelta(days=1)
    )
    daily = daily[daily.index <= complete]
    # longest stretch without a real bar inside each day (clipped to the day)
    epoch = pd.Timestamp("1970-01-01")
    m = np.unique(
        np.asarray((_naive_utc(pd.DatetimeIndex(real_ts)) - epoch) // pd.Timedelta(minutes=1), dtype=np.int64)
    )
    d, r = m // 1440, m % 1440
    first_of_day = np.r_[True, d[1:] != d[:-1]]
    last_of_day = np.r_[d[1:] != d[:-1], True]
    prev_r = np.r_[0, r[:-1]]
    gap = np.maximum.reduce(
        [
            np.where(first_of_day, r, 0),
            np.where(~first_of_day, r - prev_r - 1, 0),
            np.where(last_of_day, 1439 - r, 0),
        ]
    )
    max_gap = pd.Series(gap).groupby(d).max()
    max_gap.index = pd.DatetimeIndex(epoch + pd.to_timedelta(max_gap.index.to_numpy() * 1440, unit="min"))
    valid = max_gap.reindex(daily.index).le(PREREG["validity"]["max_gap_minutes"]).fillna(False)
    if daily.empty:
        raise ValueError("no complete UTC day in the bars")
    return _finish_daily(daily, valid, daily.index[0], daily.index[-1])


def daily_from_usd(frame: pd.DataFrame) -> pd.DataFrame:
    """Daily candles (ts, open, high, low, close, volume) -> daily series; a missing candle is an invalid day."""
    d = frame.copy()
    ts = pd.to_datetime(d["ts"], utc=True).dt.tz_localize(None).dt.normalize()
    d.index = pd.DatetimeIndex(ts)
    d = d[~d.index.duplicated(keep="last")].sort_index()
    ohlc = d[["open", "high", "low", "close"]].astype(float)
    good = np.isfinite(ohlc.to_numpy()).all(axis=1) & (d["close"].to_numpy() > 0) & (d["volume"].to_numpy() >= 0)
    daily = ohlc.copy()
    daily["V"] = d["volume"].astype(float) * d["close"].astype(float)
    daily["Vb"] = d["volume"].astype(float)
    return _finish_daily(daily, pd.Series(good, index=d.index), d.index[0], d.index[-1])


# ----------------------------------------------------------------------------------------------------- features


def _minp(window: int) -> int:
    return math.ceil(round(float(PREREG["validity"]["min_fraction"]) * window, 9))


def _roll(s: pd.Series, window: int, how: str) -> pd.Series:
    """Rolling statistic over ``window`` days ending at t, NaN unless day t exists and >= 95% of the window does."""
    out: pd.Series = getattr(s.rolling(window, min_periods=_minp(window)), how)()
    return out.where(s.notna())


def _volume_ratio(v: pd.Series) -> pd.Series:
    """VR(t) = mean(V[t-6..t]) / mean(V[t-96..t-7])."""
    recent = _roll(v, int(PREREG["volume_recent_days"]), "mean")
    n = int(PREREG["volume_base_days"])
    base = v.rolling(n, min_periods=_minp(n)).mean().shift(int(PREREG["volume_base_lag"]))
    return (recent / base.where(base > 0)).where(v.notna())


def _volume_trend(v: pd.Series) -> pd.Series:
    """VT(t) = 30 x OLS slope of ln(V) over t-29..t (log change over 30 days)."""
    w = int(PREREG["volume_trend_days"])
    y = np.log(v.where(v > 0))
    ok = y.notna()
    x = pd.Series(np.arange(len(y), dtype=float), index=y.index)
    xs, ys = x.where(ok, 0.0), y.where(ok, 0.0)
    cnt = ok.astype(float).rolling(w).sum()
    sx, sy = xs.rolling(w).sum(), ys.rolling(w).sum()
    sxy, sxx = (xs * ys).rolling(w).sum(), (xs * xs).rolling(w).sum()
    denom = cnt * sxx - sx * sx
    slope = (cnt * sxy - sx * sy) / denom.where(denom > 0)
    trend: pd.Series = (w * slope).where(ok & (cnt >= _minp(w)))
    return trend


def features(daily: pd.DataFrame) -> pd.DataFrame:
    """Daily features at the END of day t, using only data <= t (every window ends at t)."""
    c = daily["close"]
    out = pd.DataFrame(index=daily.index)
    r = pd.Series(np.log(c / c.shift(1)), index=c.index)
    out["r"] = r
    for n in PREREG["drawdown_windows"]:
        out[f"DD{n}"] = c / _roll(c, n, "max") - 1.0
    out["VR"] = _volume_ratio(daily["V"])
    out["VRb"] = _volume_ratio(daily["Vb"])
    out["VT"] = _volume_trend(daily["V"])
    out["s30"] = _roll(r, int(PREREG["vol_days"]), "std")
    out["RB"] = np.log(c / _roll(c, int(PREREG["rebound_days"]), "min"))
    for n in PREREG["drawdown_windows"]:
        suffix = "" if n == PREREG["primary_drawdown_window"] else f"_{n}"
        out[f"DDmin30{suffix}"] = _roll(out[f"DD{n}"], int(PREREG["ddmin_days"]), "min")
    out["elig"] = out[["DD180", "VR", "RB", "DDmin30"]].notna().all(axis=1)
    return out


def thresholds(feat: pd.DataFrame, train_mask: pd.Series | np.ndarray) -> dict[str, float]:
    """Thresholds from the eligible TRAINING days only (all days, not just the signals), linear quantiles."""
    q = PREREG["quantiles"]
    m = feat["elig"].to_numpy(dtype=bool) & np.asarray(train_mask, dtype=bool)

    def quant(col: str, p: float) -> float:
        x = feat.loc[m, col].dropna().to_numpy(dtype=float)
        return float(np.quantile(x, p, method="linear")) if len(x) else float("nan")

    return {
        "DD180": quant("DD180", q["dd"]),
        "DD90": quant("DD90", q["dd"]),
        "DD365": quant("DD365", q["dd"]),
        "VH": quant("VR", q["vol_high"]),
        "VL": quant("VR", q["vol_low"]),
        "VHb": quant("VRb", q["vol_high"]),
        "VLb": quant("VRb", q["vol_low"]),
        "RB": quant("RB", q["rebound"]),
        "s_med": quant("s30", q["s_med"]),
        "n_train_days": float(m.sum()),
    }


def signals(feat: pd.DataFrame, thr: Mapping[str, float]) -> dict[str, pd.Series]:
    """Boolean signal days. H1/H2 are primary; the others are the exploratory variants S1-S3 and S7."""
    e = feat["elig"]
    out: dict[str, pd.Series] = {
        "H1": e & (feat["DD180"] <= thr["DD180"]) & (feat["VR"] >= thr["VH"]),
        "H2": e & (feat["DDmin30"] <= thr["DD180"]) & (feat["RB"] >= thr["RB"]) & (feat["VR"] <= thr["VL"]),
        "S2_H1_vol": e & (feat["DD180"] <= thr["DD180"]) & (feat["VRb"] >= thr["VHb"]),
        "S2_H2_vol": e & (feat["DDmin30"] <= thr["DD180"]) & (feat["RB"] >= thr["RB"]) & (feat["VRb"] <= thr["VLb"]),
        "S3_H2_vt": e & (feat["DDmin30"] <= thr["DD180"]) & (feat["RB"] >= thr["RB"]) & (feat["VT"] < 0),
        "S7_dd_only": e & (feat["DD180"] <= thr["DD180"]),
    }
    for n in (90, 365):
        out[f"S1_H1_dd{n}"] = e & (feat[f"DD{n}"] <= thr[f"DD{n}"]) & (feat["VR"] >= thr["VH"])
        out[f"S1_H2_dd{n}"] = (
            e & (feat[f"DDmin30_{n}"] <= thr[f"DD{n}"]) & (feat["RB"] >= thr["RB"]) & (feat["VR"] <= thr["VL"])
        )
    return out


def episodes(signal: pd.Series, sep: int = SEP) -> list[pd.Timestamp]:
    """Greedy chronological episodes: a signal day starts one if it is >= ``sep`` days after the previous episode."""
    out: list[pd.Timestamp] = []
    for t in signal.index[signal.to_numpy(dtype=bool)]:
        if not out or (t - out[-1]).days >= sep:
            out.append(t)
    return out


def units(episodes_by_coin: Mapping[str, Sequence[pd.Timestamp]], horizon: int) -> list[list[tuple[str, pd.Timestamp]]]:
    """Independent units: episodes of ALL coins linked when closer than max(15, H) days (single linkage in time)."""
    flat = sorted(((t, c) for c, ts in episodes_by_coin.items() for t in ts), key=lambda x: (x[0], x[1]))
    link = max(SEP, horizon)
    groups: list[list[tuple[str, pd.Timestamp]]] = []
    prev: pd.Timestamp | None = None
    for t, c in flat:
        if prev is not None and (t - prev).days < link:
            groups[-1].append((c, t))
        else:
            groups.append([(c, t)])
        prev = t
    return groups


# ----------------------------------------------------------------------------------------------- returns, costs


def forward_series(daily: pd.DataFrame, horizon: int, delay: int = 1) -> tuple[pd.Series, pd.Series]:
    """Gross return and MAE for each day t: entry O[t+d], exit O[t+d+H], MAE = min(L[t+d..t+d+H-1]) / entry - 1."""
    o = daily["open"]
    entry = o.shift(-delay)
    g = o.shift(-(delay + horizon)) / entry - 1.0
    low_min = daily["low"].rolling(horizon, min_periods=1).min().shift(-(delay + horizon - 1))
    mae = (low_min / entry - 1.0).where(g.notna())
    return g, mae


def forward(daily: pd.DataFrame, t: pd.Timestamp, horizon: int, delay: int = 1) -> tuple[float, float]:
    g, mae = forward_series(daily, horizon, delay)
    return float(g.loc[t]), float(mae.loc[t])


# --------------------------------------------------------------------------------------------------- statistics


def ci_boot(y: Sequence[float] | np.ndarray, B: int, seed: int) -> tuple[float, float, float, float]:
    """Cluster bootstrap of the mean (the units are the blocks): ``(lo5, hi95, p_mean<=0, p_mean>=0)``."""
    a = np.asarray(y, dtype=float)
    if len(a) == 0:
        return float("nan"), float("nan"), 1.0, 1.0
    rng = np.random.default_rng(seed)
    means = a[rng.integers(0, len(a), size=(B, len(a)))].mean(axis=1)
    tail = float(PREREG["bootstrap"]["tail"])
    lo, hi = np.percentile(means, [100 * tail, 100 * (1 - tail)])
    return float(lo), float(hi), float((1 + (means <= 0).sum()) / (B + 1)), float((1 + (means >= 0).sum()) / (B + 1))


def ci_t(y: Sequence[float] | np.ndarray) -> tuple[float, float, float, float]:
    """Student t on the unit means: ``(lo, hi, p_one_sided_mean<=0, p_one_sided_mean>=0)``, 90% two-sided interval."""
    a = np.asarray(y, dtype=float)
    n = len(a)
    if n < 2:
        return float("nan"), float("nan"), 1.0, 1.0
    mean, s = float(a.mean()), float(a.std(ddof=1))
    tail = float(PREREG["bootstrap"]["tail"])
    q = float(sps.t.ppf(1 - tail, n - 1))
    se = s / math.sqrt(n)
    if se == 0.0:
        return mean, mean, (0.0 if mean > 0 else 1.0), (0.0 if mean < 0 else 1.0)
    stat = mean / se
    return mean - q * se, mean + q * se, float(sps.t.sf(stat, n - 1)), float(sps.t.cdf(stat, n - 1))


@dataclass(frozen=True)
class Ci:
    """Conservative interval: lower = min, upper = max, p = max of bootstrap and t."""

    lb: float
    ub: float
    p_le0: float
    p_ge0: float
    boot: tuple[float, float, float, float]
    t: tuple[float, float, float, float]


def conservative_ci(y: Sequence[float] | np.ndarray, seed: int, B: int = BOOT_B) -> Ci:
    boot, t = ci_boot(y, B, seed), ci_t(y)
    if len(y) == 0:
        return Ci(float("nan"), float("nan"), 1.0, 1.0, boot, t)
    lb = min(boot[0], t[0]) if not math.isnan(t[0]) else boot[0]
    ub = max(boot[1], t[1]) if not math.isnan(t[1]) else boot[1]
    return Ci(lb, ub, max(boot[2], t[2]), max(boot[3], t[3]), boot, t)


def holm(pvals: Sequence[float]) -> list[float]:
    """Holm step-down adjusted p-values (monotone, capped at 1), in the input order."""
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    adj = [0.0] * m
    run = 0.0
    for rank, i in enumerate(order):
        run = max(run, min(1.0, (m - rank) * pvals[i]))
        adj[i] = run
    return adj


# ----------------------------------------------------------------------------------------------- split statistics


@dataclass(frozen=True)
class CoinStat:
    n_ep: int
    net: float
    ex: float


@dataclass(frozen=True)
class SplitStat:
    """Statistics of one (hypothesis, horizon, split) test; returns are fractions (1% = 0.01)."""

    n: int
    n_ep: int = 0
    mean_g: float = float("nan")
    mean_net: float = float("nan")
    mean_net110: float = float("nan")
    mean_ex: float = float("nan")
    net_ci: Ci | None = None
    ex_ci: Ci | None = None
    coins: Mapping[str, CoinStat] = field(default_factory=dict)
    net_wo_best: float = float("nan")  # mean of the unit nets without the best unit
    ex_wo_best: float = float("nan")  # mean of the unit excesses without the MOST NEGATIVE unit (H2 veto)
    mae_median: float = float("nan")
    mae_p10: float = float("nan")
    mean_z: float = float("nan")

    @property
    def net_lb(self) -> float:
        return self.net_ci.lb if self.net_ci else float("nan")

    @property
    def net_ub(self) -> float:
        return self.net_ci.ub if self.net_ci else float("nan")

    @property
    def ex_ub(self) -> float:
        return self.ex_ci.ub if self.ex_ci else float("nan")

    @property
    def p_net(self) -> float:
        """p of H0: mean net <= 0 (H1); 1 if the test is empty."""
        return self.net_ci.p_le0 if self.net_ci else 1.0

    @property
    def p_ex(self) -> float:
        """p of H0: mean excess >= 0 (H2); 1 if the test is empty."""
        return self.ex_ci.p_ge0 if self.ex_ci else 1.0


@dataclass
class Outcome:
    """One episode, for one horizon."""

    coin: str
    t: pd.Timestamp
    split: str | None
    status: str  # ok | nan | outside
    g: float = float("nan")
    net: float = float("nan")
    net110: float = float("nan")
    ex: float = float("nan")
    mae: float = float("nan")
    z: float = float("nan")
    regime: str | None = None


def _mean(a: Sequence[float] | np.ndarray) -> float:
    return float(np.mean(a)) if len(a) else float("nan")


def split_stat(outs: Sequence[Outcome], horizon: int, seed: int) -> SplitStat:
    """Units, means (of the units) and conservative intervals of the net and of the excess."""
    ok = [o for o in outs if o.status == "ok"]
    if not ok:
        return SplitStat(n=0)
    by_coin: dict[str, list[pd.Timestamp]] = {}
    for o in ok:
        by_coin.setdefault(o.coin, []).append(o.t)
    look = {(o.coin, o.t): o for o in ok}
    groups = units(by_coin, horizon)

    def unit_vals(attr: str) -> np.ndarray:
        def one(grp: list[tuple[str, pd.Timestamp]]) -> float:
            v = np.array([getattr(look[k], attr) for k in grp], dtype=float)
            v = v[np.isfinite(v)]
            return float(v.mean()) if len(v) else float("nan")

        return np.array([one(grp) for grp in groups])

    net, ex = unit_vals("net"), unit_vals("ex")
    mae = np.array([o.mae for o in ok], dtype=float)
    coins = {
        c: CoinStat(
            len(v),
            _mean([o.net for o in ok if o.coin == c]),
            _mean([o.ex for o in ok if o.coin == c]),
        )
        for c, v in sorted(by_coin.items())
    }
    z = unit_vals("z")
    return SplitStat(
        n=len(groups),
        n_ep=len(ok),
        mean_g=_mean(unit_vals("g")),
        mean_net=_mean(net),
        mean_net110=_mean(unit_vals("net110")),
        mean_ex=_mean(ex),
        net_ci=conservative_ci(net, seed),
        ex_ci=conservative_ci(ex, seed),
        coins=coins,
        net_wo_best=_mean(np.sort(net)[:-1]) if len(net) > 1 else float("nan"),
        ex_wo_best=_mean(np.sort(ex)[1:]) if len(ex) > 1 else float("nan"),
        mae_median=float(np.median(mae[np.isfinite(mae)])) if np.isfinite(mae).any() else float("nan"),
        mae_p10=float(np.percentile(mae[np.isfinite(mae)], 10)) if np.isfinite(mae).any() else float("nan"),
        mean_z=float(z[np.isfinite(z)].mean()) if np.isfinite(z).any() else float("nan"),
    )


# -------------------------------------------------------------------------------------------------- verdicts


@dataclass(frozen=True)
class Verdict:
    verdict: str
    passed: list[str]
    failed: list[str]
    notes: list[str] = field(default_factory=list)


def _coins_ok(stat: SplitStat, positive: bool) -> bool:
    """Coins with >= 3 episodes in the split: net > 0 (H1) / excess < 0 (H2) for each of them."""
    for cs in stat.coins.values():
        if cs.n_ep >= MIN_COIN and ((cs.net <= 0) if positive else (cs.ex >= 0)):
            return False
    return True


def verdict_h1(
    train: SplitStat,
    hold: SplitStat,
    p_holm_train: float,
    replica: SplitStat | None,
    holdout_clean: bool,
    has_replica: bool,
) -> Verdict:
    """Section 10 rules for H1 (entry): INCONCLUSO (n) / NO-GO / GO / CANDIDATO / INCONCLUSO."""
    if train.n < MIN_T or hold.n < MIN_V:
        return Verdict("INCONCLUSO", [], ["INS_n"], [f"n_T={train.n} (min {MIN_T}), n_V={hold.n} (min {MIN_V})"])
    nogo = {
        "N1_net_train<=0": train.mean_net <= 0,
        "N2_net_holdout<=0": hold.mean_net <= 0,
        "N3_excess_train<=0": train.mean_ex <= 0,
        "N4_excess_holdout<=0": hold.mean_ex <= 0,
        "N5_UBnet_train<0": train.net_ub < 0,
        "N6_UBnet_holdout<0": hold.net_ub < 0,
    }
    hit = [k for k, v in nogo.items() if v]
    if hit:
        return Verdict("NO-GO", [], hit)
    cond = {
        "a_pHolm_train<0.05": p_holm_train < ALPHA,
        "b_LBnet_holdout>0": hold.net_lb > 0,
        "c_excess>0_both": train.mean_ex > 0 and hold.mean_ex > 0,
        "d_no_coin_net<=0": _coins_ok(train, True) and _coins_ok(hold, True),
        "e_net>0_without_best_unit": train.net_wo_best > 0 and hold.net_wo_best > 0,
    }
    notes: list[str] = []
    if has_replica:
        if replica is not None and replica.n >= MIN_R:
            cond["f_replica_net>0"] = replica.mean_net > 0
        else:
            notes.append("f: replica not evaluable (n_R < 3), does not block")
    passed = [k for k, v in cond.items() if v]
    failed = [k for k, v in cond.items() if not v]
    if failed:
        return Verdict("INCONCLUSO", passed, failed, notes)
    if holdout_clean:
        return Verdict("GO", [*passed, "g_holdout_clean"], [], notes)
    return Verdict("CANDIDATO", passed, ["g_holdout_clean"], notes)


def verdict_h2(
    train: SplitStat,
    hold: SplitStat,
    p_holm_train: float,
    replica: SplitStat | None,
    holdout_clean: bool,
    has_replica: bool,
) -> Verdict:
    """Section 10 rules for H2 (veto, sign reversed on the excess): NO-GO / VETO / CANDIDATO / INCONCLUSO."""
    if train.n < MIN_T or hold.n < MIN_V:
        return Verdict("INCONCLUSO", [], ["INS_n"], [f"n_T={train.n} (min {MIN_T}), n_V={hold.n} (min {MIN_V})"])
    nogo = {"N1_excess_train>=0": train.mean_ex >= 0, "N2_excess_holdout>=0": hold.mean_ex >= 0}
    hit = [k for k, v in nogo.items() if v]
    if hit:
        return Verdict("NO-GO", [], hit)
    cond = {
        "a_pHolm_train<0.05": p_holm_train < ALPHA,
        "b_UBexcess_holdout<0": hold.ex_ub < 0,
        "d_no_coin_excess>=0": _coins_ok(train, False) and _coins_ok(hold, False),
        "e_excess<0_without_most_negative_unit": train.ex_wo_best < 0 and hold.ex_wo_best < 0,
    }
    notes = []
    if has_replica:
        if replica is not None and replica.n >= MIN_R:
            cond["f_replica_excess<0"] = replica.mean_ex < 0
        else:
            notes.append("f: replica not evaluable (n_R < 3), does not block")
    passed = [k for k, v in cond.items() if v]
    failed = [k for k, v in cond.items() if not v]
    if failed:
        return Verdict("INCONCLUSO", passed, failed, notes)
    if holdout_clean:
        return Verdict("VETO", [*passed, "g_holdout_clean"], [], notes)
    return Verdict("CANDIDATO", passed, ["g_holdout_clean"], notes)


def train_p(stat: SplitStat, hypothesis: str) -> float:
    """p of a training test as it enters Holm: 1 if the test has fewer than the minimum units."""
    if stat.n < MIN_T:
        return 1.0
    return stat.p_net if hypothesis == "H1" else stat.p_ex


def global_verdict(h1: Mapping[int, Verdict]) -> tuple[str, int | None]:
    """GO if any H1 test is GO (operating horizon = the shortest GO); NO-GO if all are; CANDIDATO; else INCONCLUSO."""
    go = sorted(h for h, v in h1.items() if v.verdict == "GO")
    if go:
        return "GO", go[0]
    if h1 and all(v.verdict == "NO-GO" for v in h1.values()):
        return "NO-GO", None
    if any(v.verdict == "CANDIDATO" for v in h1.values()):
        return "CANDIDATO", None
    return "INCONCLUSO", None


# --------------------------------------------------------------------------------------------- analysis (pure)


@dataclass(frozen=True)
class Split:
    """Signals t in ``[t_lo, t_hi)`` with the purge ``t + delay + H <= end`` (``end`` None: last day of the series)."""

    name: str
    t_lo: pd.Timestamp | None
    t_hi: pd.Timestamp | None
    end: pd.Timestamp | None


@dataclass(frozen=True)
class ArmConfig:
    arm: str
    holdout_start: pd.Timestamp
    holdout_end: pd.Timestamp | None
    holdout_clean: bool
    has_replica: bool


@dataclass
class CoinData:
    coin: str
    daily: pd.DataFrame
    feat: pd.DataFrame
    series: str = "main"  # main | replica: the cache of the engine must not mix the two series of a coin


# variant name -> (signal key, entry delay, volatility regime filter)
VARIANTS: list[tuple[str, str, int, str | None]] = [
    ("H1", "H1", 1, None),
    ("H2", "H2", 1, None),
    ("S1_H1_dd90", "S1_H1_dd90", 1, None),
    ("S1_H1_dd365", "S1_H1_dd365", 1, None),
    ("S1_H2_dd90", "S1_H2_dd90", 1, None),
    ("S1_H2_dd365", "S1_H2_dd365", 1, None),
    ("S2_H1_vol", "S2_H1_vol", 1, None),
    ("S2_H2_vol", "S2_H2_vol", 1, None),
    ("S3_H2_vt", "S3_H2_vt", 1, None),
    ("S5_H1_delay2", "H1", 2, None),
    ("S5_H2_delay2", "H2", 2, None),
    ("S6_H1_regime_high", "H1", 1, "ALTO"),
    ("S6_H1_regime_low", "H1", 1, "BASSO"),
    ("S6_H2_regime_high", "H2", 1, "ALTO"),
    ("S6_H2_regime_low", "H2", 1, "BASSO"),
    ("S7_dd_only", "S7_dd_only", 1, None),
]
PRIMARY = ("H1", "H2")


def coin_of(product: str) -> str:
    return product.split("-")[0]


def build_splits(cfg: ArmConfig) -> list[Split]:
    return [
        Split("train", None, cfg.holdout_start, cfg.holdout_start),
        Split("holdout", cfg.holdout_start, None, cfg.holdout_end),
    ]


def _split_mask(index: pd.Index, sp: Split, horizon: int, delay: int) -> np.ndarray:
    """Days t that belong to the split, with the purge (the exit open must not fall after the end of the split)."""
    idx = pd.DatetimeIndex(index)
    last = idx[-1]
    end = min(sp.end, last) if sp.end is not None else last
    m = np.asarray(idx + pd.Timedelta(days=delay + horizon) <= end)
    if sp.t_lo is not None:
        m &= np.asarray(idx >= sp.t_lo)
    if sp.t_hi is not None:
        m &= np.asarray(idx < sp.t_hi)
    return m


class _Engine:
    """Memoised forward returns, masks and means of ``mu`` (all eligible days of the same coin, split and H)."""

    def __init__(self) -> None:
        self._fwd: dict[tuple[str, str, int, int], tuple[pd.Series, pd.Series]] = {}
        self._mu: dict[tuple[str, str, str, int, int], float] = {}

    def fwd(self, cd: CoinData, horizon: int, delay: int) -> tuple[pd.Series, pd.Series]:
        key = (cd.series, cd.coin, horizon, delay)
        if key not in self._fwd:
            self._fwd[key] = forward_series(cd.daily, horizon, delay)
        return self._fwd[key]

    def mu(self, cd: CoinData, sp: Split, horizon: int, delay: int) -> float:
        key = (cd.series, cd.coin, sp.name, horizon, delay)
        if key not in self._mu:
            g, _ = self.fwd(cd, horizon, delay)
            m = (
                _split_mask(cd.daily.index, sp, horizon, delay)
                & cd.feat["elig"].to_numpy(dtype=bool)
                & g.notna().to_numpy()
            )
            self._mu[key] = float(g[m].mean()) if m.any() else float("nan")
        return self._mu[key]


def outcomes(
    eng: _Engine,
    cd: CoinData,
    eps: Sequence[pd.Timestamp],
    splits: Sequence[Split],
    horizon: int,
    delay: int,
    s_med: float,
) -> list[Outcome]:
    """Outcome of each episode at one horizon: split (with purge), gross, net (77 / 110 bps), excess, MAE, z, regime."""
    g, mae = eng.fwd(cd, horizon, delay)
    masks = {sp.name: _split_mask(cd.daily.index, sp, horizon, delay) for sp in splits}
    out: list[Outcome] = []
    for t in eps:
        pos = int(cd.daily.index.get_indexer(pd.Index([t]))[0])
        name = next((sp.name for sp in splits if masks[sp.name][pos]), None)
        if name is None:
            out.append(Outcome(cd.coin, t, None, "outside"))
            continue
        gv = float(g.iloc[pos])
        if math.isnan(gv):
            out.append(Outcome(cd.coin, t, name, "nan"))
            continue
        sp = next(s for s in splits if s.name == name)
        mu = eng.mu(cd, sp, horizon, delay)
        s30 = float(cd.feat["s30"].iloc[pos])
        regime = None if math.isnan(s30) else ("ALTO" if s30 >= s_med else "BASSO")
        z = gv / (s30 * math.sqrt(horizon)) if (not math.isnan(s30) and s30 > 0) else float("nan")
        out.append(
            Outcome(
                cd.coin, t, name, "ok", gv, gv - K_PRIMARY, gv - K_SECONDARY, gv - mu, float(mae.iloc[pos]), z, regime
            )
        )
    return out


def _calendar_boot(
    pool: Sequence[tuple[pd.Timestamp, float]],
    ep: Sequence[tuple[pd.Timestamp, float]],
    horizon: int,
    seed: int,
    B: int = BOOT_B,
) -> dict[str, Any]:
    """Secondary: calendar moving-block bootstrap (block 2H days) of the pooled excess, with the noise of mu."""
    if not pool or not ep:
        return {"available": False}
    t0 = min(t for t, _ in pool)
    n_days = (max(t for t, _ in pool) - t0).days + 1
    sum_all, cnt_all, sum_ep, cnt_ep = (np.zeros(n_days) for _ in range(4))
    for t, g in pool:
        i = (t - t0).days
        sum_all[i] += g
        cnt_all[i] += 1
    for t, g in ep:
        i = (t - t0).days
        sum_ep[i] += g
        cnt_ep[i] += 1
    b = min(int(PREREG["bootstrap"]["calendar_block_factor"]) * horizon, n_days)
    nb = math.ceil(n_days / b)
    rng = np.random.default_rng(seed)
    res: list[np.ndarray] = []
    chunk = 500
    for lo in range(0, B, chunk):
        k = min(chunk, B - lo)
        starts = rng.integers(0, n_days - b + 1, size=(k, nb))
        flat = (np.arange(k)[:, None] * (n_days + 1) + starts).ravel()
        c = np.bincount(flat, minlength=k * (n_days + 1)).reshape(k, n_days + 1)
        cs = np.cumsum(c, axis=1)[:, :n_days].astype(float)
        mult = cs.copy()
        mult[:, b:] -= cs[:, : n_days - b]
        with np.errstate(invalid="ignore", divide="ignore"):
            ex = (mult @ sum_ep) / (mult @ cnt_ep) - (mult @ sum_all) / (mult @ cnt_all)
        res.append(ex)
    exs = np.concatenate(res)
    exs = exs[np.isfinite(exs)]
    if len(exs) == 0:
        return {"available": False}
    tail = float(PREREG["bootstrap"]["tail"])
    lo5, hi95 = np.percentile(exs, [100 * tail, 100 * (1 - tail)])
    mean_ep = float(sum_ep.sum() / cnt_ep.sum())
    mean_all = float(sum_all.sum() / cnt_all.sum())
    return {
        "available": True,
        "block_days": b,
        "excess_bps": (mean_ep - mean_all) * 1e4,
        "ci90_bps": [float(lo5) * 1e4, float(hi95) * 1e4],
        "p_le0": float((1 + (exs <= 0).sum()) / (len(exs) + 1)),
        "p_ge0": float((1 + (exs >= 0).sum()) / (len(exs) + 1)),
    }


def _bps(x: float) -> float | None:
    return None if x is None or math.isnan(x) else x * 1e4


def _ci_doc(ci: Ci | None) -> dict[str, Any] | None:
    if ci is None:
        return None
    return {
        "lb_bps": _bps(ci.lb),
        "ub_bps": _bps(ci.ub),
        "p_le0": ci.p_le0,
        "p_ge0": ci.p_ge0,
        "boot_ci90_bps": [_bps(ci.boot[0]), _bps(ci.boot[1])],
        "t_ci90_bps": [_bps(ci.t[0]), _bps(ci.t[1])],
        "p_boot": [ci.boot[2], ci.boot[3]],
        "p_t": [ci.t[2], ci.t[3]],
    }


def stat_doc(ss: SplitStat) -> dict[str, Any]:
    return {
        "n_units": ss.n,
        "n_episodes": ss.n_ep,
        "mean_gross_bps": _bps(ss.mean_g),
        "mean_net77_bps": _bps(ss.mean_net),
        "mean_net110_bps": _bps(ss.mean_net110),
        "mean_excess_bps": _bps(ss.mean_ex),
        "net_ci90": _ci_doc(ss.net_ci),
        "excess_ci90": _ci_doc(ss.ex_ci),
        "by_coin": {
            c: {"n_episodes": v.n_ep, "net77_bps": _bps(v.net), "excess_bps": _bps(v.ex)} for c, v in ss.coins.items()
        },
        "net77_without_best_unit_bps": _bps(ss.net_wo_best),
        "excess_without_most_negative_unit_bps": _bps(ss.ex_wo_best),
        "mae_median_bps": _bps(ss.mae_median),
        "mae_p10_bps": _bps(ss.mae_p10),
        "mean_z": None if math.isnan(ss.mean_z) else ss.mean_z,
    }


def _verdict_doc(v: Verdict) -> dict[str, Any]:
    return {"verdict": v.verdict, "passed": v.passed, "failed": v.failed, "notes": v.notes}


def analyze(
    main: Mapping[str, pd.DataFrame], replica: Mapping[str, pd.DataFrame] | None, cfg: ArmConfig
) -> dict[str, Any]:
    """The whole study on already built daily series (no I/O). Returns the documents 01..06 (without provenance)."""
    splits = build_splits(cfg)
    rep_split = [Split("replica", None, None, None)]
    eng = _Engine()
    data = {c: CoinData(c, d, features(d)) for c, d in main.items()}
    rdata = {c: CoinData(c, d, features(d), "replica") for c, d in (replica or {}).items() if c in main}
    thr = {c: thresholds(cd.feat, cd.daily.index < cfg.holdout_start) for c, cd in data.items()}
    sig = {c: signals(cd.feat, thr[c]) for c, cd in data.items()}
    rsig = {c: signals(cd.feat, thr[c]) for c, cd in rdata.items()}
    eps = {v: {c: episodes(sig[c][key]) for c in data} for v, key, _, _ in VARIANTS}
    reps = {v: {c: episodes(rsig[c][key]) for c in rdata} for v, key, _, _ in VARIANTS if v in PRIMARY}

    # outcomes[variant][H][split] -> list[Outcome]
    outs: dict[str, dict[int, dict[str, list[Outcome]]]] = {}
    for vname, _key, delay, regime in VARIANTS:
        outs[vname] = {}
        for h in HORIZONS:
            per: dict[str, list[Outcome]] = {sp.name: [] for sp in splits}
            if vname in reps and rdata:
                per["replica"] = []
            for c, cd in data.items():
                for o in outcomes(eng, cd, eps[vname][c], splits, h, delay, thr[c]["s_med"]):
                    if o.split is not None and (regime is None or o.regime == regime):
                        per[o.split].append(o)
            if vname in reps and rdata:
                for c, cd in rdata.items():
                    for o in outcomes(eng, cd, reps[vname][c], rep_split, h, delay, thr[c]["s_med"]):
                        if o.split is not None:
                            per["replica"].append(o)
            outs[vname][h] = per

    def seed_of(vi: int, hi: int, split: str) -> int:
        return BOOT_SEED + 1000 * vi + 3 * hi + SPLIT_ORDER.index(split)

    stats: dict[str, dict[int, dict[str, SplitStat]]] = {}
    for vi, (vname, *_rest) in enumerate(VARIANTS):
        stats[vname] = {}
        for hi, h in enumerate(HORIZONS):
            stats[vname][h] = {s: split_stat(o, h, seed_of(vi, hi, s)) for s, o in outs[vname][h].items()}

    # Holm over the 6 training p-values (an under-sized test enters with p = 1)
    fam: list[tuple[str, int]] = [(v, h) for v in PRIMARY for h in HORIZONS]
    raw = [train_p(stats[v][h]["train"], v) for v, h in fam]
    p_holm = dict(zip(fam, holm(raw), strict=True))
    p_raw = dict(zip(fam, raw, strict=True))

    verdicts: dict[str, dict[int, Verdict]] = {"H1": {}, "H2": {}}
    for v in PRIMARY:
        fn = verdict_h1 if v == "H1" else verdict_h2
        for h in HORIZONS:
            st = stats[v][h]
            verdicts[v][h] = fn(
                st["train"], st["holdout"], p_holm[(v, h)], st.get("replica"), cfg.holdout_clean, cfg.has_replica
            )
    g_verdict, g_horizon = global_verdict(verdicts["H1"])

    # frequencies of the signal days
    def freq(
        sp: Split, c: str, key: str, d: Mapping[str, CoinData], sg: Mapping[str, Mapping[str, pd.Series]]
    ) -> dict[str, Any]:
        idx = d[c].daily.index
        m = np.ones(len(idx), dtype=bool)
        if sp.t_lo is not None:
            m &= np.asarray(idx >= sp.t_lo)
        if sp.t_hi is not None:
            m &= np.asarray(idx < sp.t_hi)
        if sp.end is not None:
            m &= np.asarray(idx <= sp.end)
        elig = d[c].feat["elig"].to_numpy(dtype=bool) & m
        n_sig = int((sg[c][key].to_numpy(dtype=bool) & m).sum())
        return {
            "eligible_days": int(elig.sum()),
            "signal_days": n_sig,
            "frequency": n_sig / int(elig.sum()) if elig.any() else None,
        }

    results_tests: list[dict[str, Any]] = []
    for v in PRIMARY:
        for h in HORIZONS:
            for s in outs[v][h]:
                doc = stat_doc(stats[v][h][s])
                doc.update(hypothesis=v, horizon=h, split=s)
                sp = next((x for x in (splits + rep_split) if x.name == s), None)
                assert sp is not None
                src_d, src_s = (rdata, rsig) if s == "replica" else (data, sig)
                doc["signal_frequency"] = {c: freq(sp, c, v, src_d, src_s) for c in src_d}
                if s == "train":
                    doc["p_raw"] = p_raw[(v, h)]
                    doc["p_holm"] = p_holm[(v, h)]
                results_tests.append(doc)

    sec_tests: list[dict[str, Any]] = []
    for vname, key, delay, regime in VARIANTS:
        if vname in PRIMARY:
            continue
        for h in HORIZONS:
            for s in outs[vname][h]:
                doc = stat_doc(stats[vname][h][s])
                doc.update(
                    variant=vname,
                    signal=key,
                    entry_delay_days=delay,
                    regime=regime,
                    horizon=h,
                    split=s,
                    label="exploratory",
                )
                sec_tests.append(doc)
    s4 = [
        {
            "hypothesis": v,
            "horizon": h,
            "split": s,
            "mean_net110_bps": _bps(stats[v][h][s].mean_net110),
            "label": "exploratory",
        }
        for v in PRIMARY
        for h in HORIZONS
        for s in outs[v][h]
    ]
    cal: list[dict[str, Any]] = []
    for vi, v in enumerate(PRIMARY):
        for hi, h in enumerate(HORIZONS):
            for s in outs[v][h]:
                src = rdata if s == "replica" else data
                spl = next(x for x in (splits + rep_split) if x.name == s)
                pool: list[tuple[pd.Timestamp, float]] = []
                for cd in src.values():
                    g, _ = eng.fwd(cd, h, 1)
                    m = (
                        _split_mask(cd.daily.index, spl, h, 1)
                        & cd.feat["elig"].to_numpy(dtype=bool)
                        & g.notna().to_numpy()
                    )
                    pool.extend(zip(cd.daily.index[m], g[m].to_numpy(dtype=float), strict=True))
                epl = [(o.t, o.g) for o in outs[v][h][s] if o.status == "ok"]
                d = _calendar_boot(pool, epl, h, seed_of(vi, hi, s) + 500)
                d.update(hypothesis=v, horizon=h, split=s, label="exploratory")
                cal.append(d)
    sec = {
        "label": "exploratory",
        "variants": sec_tests,
        "S4_cost_110bps": s4,
        "S8_by_coin": "in the by_coin field of every block (primary in 04_results.json)",
        "S9_sol": "arm E: SOL is one of the three coins (by_coin); arm L: not computed (SOL-USD not downloaded)",
        "calendar_bootstrap": cal,
    }

    # per-episode table (primary hypotheses)
    ep_docs: list[dict[str, Any]] = []
    unit_ids: dict[tuple[str, int, str, str, pd.Timestamp], str] = {}
    for v in PRIMARY:
        for h in HORIZONS:
            for s, lst in outs[v][h].items():
                ok = [o for o in lst if o.status == "ok"]
                by_coin: dict[str, list[pd.Timestamp]] = {}
                for o in ok:
                    by_coin.setdefault(o.coin, []).append(o.t)
                for k, grp in enumerate(units(by_coin, h)):
                    for c, t in grp:
                        unit_ids[(v, h, s, c, t)] = f"{v}|H{h}|{s}|u{k}"
    excluded: dict[str, int] = {}
    for v in PRIMARY:
        for src_name, src_d, src_e in (("main", data, eps[v]), ("replica", rdata, reps.get(v, {}))):
            for c, cd in src_d.items():
                for t in src_e.get(c, []):
                    feats = {k: float(cd.feat[k].loc[t]) for k in ("DD180", "VR", "RB", "DDmin30", "s30")}
                    row: dict[str, Any] = {
                        "hypothesis": v,
                        "coin": c,
                        "series": src_name,
                        "t": str(t.date()),
                        "features": feats,
                        "horizons": {},
                    }
                    for h in HORIZONS:
                        found = [
                            o for s, lst in outs[v][h].items() if (s == "replica") == (src_name == "replica")
                            for o in lst if o.coin == c and o.t == t
                        ]  # fmt: skip
                        oc = found[0] if found else None
                        if oc is None or oc.status != "ok":
                            status = "outside" if oc is None else oc.status
                            row["horizons"][str(h)] = {"split": None if oc is None else oc.split, "status": status}
                            key = f"{v}|{src_name}|H{h}|{status}"
                            excluded[key] = excluded.get(key, 0) + 1
                        else:
                            row["horizons"][str(h)] = {
                                "split": oc.split, "status": "ok", "gross_bps": _bps(oc.g), "net77_bps": _bps(oc.net),
                                "net110_bps": _bps(oc.net110), "excess_bps": _bps(oc.ex), "mae_bps": _bps(oc.mae),
                                "unit": unit_ids.get((v, h, oc.split or "", c, t)),
                            }  # fmt: skip
                    ep_docs.append(row)

    data_docs: dict[str, Any] = {}
    for src_name, src_d in (("main", data), ("replica", rdata)):
        for c, cd in src_d.items():
            valid = cd.daily["valid"].to_numpy(dtype=bool)
            runs: list[list[str | int]] = []
            i = 0
            while i < len(valid):
                if not valid[i]:
                    j = i
                    while j + 1 < len(valid) and not valid[j + 1]:
                        j += 1
                    runs.append([str(cd.daily.index[i].date()), str(cd.daily.index[j].date()), j - i + 1])
                    i = j + 1
                else:
                    i += 1
            elig = cd.feat["elig"]
            data_docs[f"{src_name}:{c}"] = {
                "first_day": str(cd.daily.index[0].date()),
                "last_day": str(cd.daily.index[-1].date()),
                "days": int(len(valid)),
                "valid_days": int(valid.sum()),
                "invalid_days": int((~valid).sum()),
                "invalid_runs_top50": sorted(runs, key=lambda r: -int(r[2]))[:50],
                "n_invalid_runs": len(runs),
                "first_eligible_day": str(elig[elig].index[0].date()) if elig.any() else None,
            }
    return {
        "data": {"series": data_docs, "episodes_not_ok": excluded},
        "thresholds": {c: {k: (None if math.isnan(v) else v) for k, v in t.items()} for c, t in thr.items()},
        "episodes": ep_docs,
        "results": {"tests": results_tests},
        "secondary": sec,
        "decision": {
            "arm": cfg.arm,
            "holdout_clean": cfg.holdout_clean,
            "has_replica": cfg.has_replica,
            "h1": {str(h): _verdict_doc(verdicts["H1"][h]) for h in HORIZONS},
            "h2": {str(h): _verdict_doc(verdicts["H2"][h]) for h in HORIZONS},
            "global": {"verdict": g_verdict, "operating_horizon_days": g_horizon, "decided_by": "H1 only"},
            "p_holm_train": {f"{v}|H{h}": p_holm[(v, h)] for v, h in fam},
        },
    }


def key_of(variant: str) -> str:
    return next(k for n, k, _, _ in VARIANTS if n == variant)


# ------------------------------------------------------------------------------------------------------ I/O


def _clean(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_clean(v) for v in obj]
    if isinstance(obj, np.generic):
        return _clean(obj.item())
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, pd.Timestamp):
        return str(obj)
    return obj


def _write_new(path: Path, doc: Mapping[str, Any]) -> None:
    """Exclusive open: each file of the run is written once."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(_clean(dict(doc)), sort_keys=True, separators=(",", ":"), allow_nan=False)
    with open(path, "x", encoding="utf-8", newline="\n") as fh:
        fh.write(text + "\n")


def _sha256_file(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def prereg_sha256() -> str:
    return hashlib.sha256(json.dumps(PREREG, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def git_head() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=True, timeout=30
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def script_is_clean() -> bool:
    """True only if the script is tracked and has no modification (staged, unstaged) in git."""
    try:
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", SCRIPT_REL], cwd=REPO_ROOT, capture_output=True, text=True,
            timeout=30,
        ).returncode == 0  # fmt: skip
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", SCRIPT_REL], cwd=REPO_ROOT, capture_output=True, text=True,
            check=True, timeout=30,
        ).stdout.strip()  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return False
    return tracked and not status


def holdout_clean_of(
    exploration: Sequence[str] | None, holdout_start: pd.Timestamp, holdout_end: pd.Timestamp | None
) -> bool:
    """False if the explored interval is unknown or touches the holdout ``[holdout_start, holdout_end]``."""
    if exploration is None or list(exploration) == ["unknown"]:
        return False
    lo, hi = pd.Timestamp(exploration[0]), pd.Timestamp(exploration[1])
    end = holdout_end if holdout_end is not None else pd.Timestamp.max
    return bool(hi < holdout_start or lo > end)


class BarSource(Protocol):
    def bars(self, product: str) -> pd.DataFrame: ...

    def real_ts(self, product: str) -> pd.DatetimeIndex: ...


class StoreSource:
    """Default source: the 1m store of the repo (``load_bars`` for the grid, ``BarStore`` for the real bars)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._store = BarStore(self.root)

    def bars(self, product: str) -> pd.DataFrame:
        return load_bars(self.root, product)

    def real_ts(self, product: str) -> pd.DatetimeIndex:
        frames = [self._store.read_month(product, y, m)["ts"] for y, m in self._store.months(product)]
        return pd.DatetimeIndex(pd.concat(frames)) if frames else pd.DatetimeIndex([])


@dataclass
class RunArgs:
    arm: str
    bars_root: Path
    usd_daily: Path | None
    exploration: list[str]
    out: Path


@dataclass
class Env:
    head: Callable[[], str | None] = git_head
    script_clean: Callable[[], bool] = script_is_clean
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    source_factory: Callable[[Path], BarSource] = StoreSource


def run_study(args: RunArgs, env: Env | None = None) -> int:
    env = env or Env()
    if args.arm not in ("E", "L"):
        print("arm must be E or L", file=sys.stderr)
        return EXIT_REFUSED
    folder = args.out / args.arm
    if folder.exists():
        print(f"refused: {folder} already exists (one run per arm)", file=sys.stderr)
        return EXIT_REFUSED
    if not env.script_clean():
        print(f"refused: {SCRIPT_REL} is not tracked or has uncommitted changes", file=sys.stderr)
        return EXIT_REFUSED
    if args.arm == "L" and args.usd_daily is None:
        print("refused: arm L needs --usd-daily", file=sys.stderr)
        return EXIT_REFUSED
    holdout_start = pd.Timestamp(PREREG["holdout_start"][args.arm])
    holdout_end = pd.Timestamp(PREREG["arm_L"]["holdout_end"]) if args.arm == "L" else None
    clean = holdout_clean_of(args.exploration, holdout_start, holdout_end)
    cfg = ArmConfig(args.arm, holdout_start, holdout_end, clean, args.arm == "L")

    # 00: BEFORE reading any bar
    _write_new(
        folder / "00_preregistration.json",
        {
            "schema": SCHEMA, "arm": args.arm, "git_commit": env.head(),
            "script_sha256": _sha256_file(REPO_ROOT / SCRIPT_REL), "document_sha256": _sha256_file(REPO_ROOT / DOC_REL),
            "prereg_sha256": prereg_sha256(), "exploration_range": list(args.exploration), "holdout_clean": clean,
            "holdout_start": str(holdout_start.date()), "created_utc": env.now().isoformat(),
        },
    )  # fmt: skip

    source = env.source_factory(args.bars_root)

    def eur_daily(products: Sequence[str]) -> dict[str, pd.DataFrame]:
        return {coin_of(p): daily_from_1m(source.bars(p), source.real_ts(p)) for p in products}

    usd_sha: dict[str, str | None] = {}
    if args.arm == "E":
        main = eur_daily(PREREG["arm_E"]["products"])
        replica = None
    else:
        assert args.usd_daily is not None
        main = {}
        for p in PREREG["arm_L"]["products"]:
            path = Path(args.usd_daily) / f"{p}_1d.parquet"
            usd_sha[p] = _sha256_file(path)
            main[coin_of(p)] = daily_from_usd(pd.read_parquet(path))
        replica = eur_daily(PREREG["arm_L"]["replica_products"])

    doc = analyze(main, replica, cfg)
    _write_new(
        folder / "01_data.json",
        {**doc["data"], "usd_parquet_sha256": usd_sha, "coinbase_terms": COINBASE_TERMS, "arm": args.arm},
    )
    _write_new(
        folder / "02_thresholds.json", {"thresholds": doc["thresholds"], "training_end": str(holdout_start.date())}
    )
    _write_new(folder / "03_episodes.json", {"episodes": doc["episodes"]})
    _write_new(folder / "04_results.json", doc["results"])
    _write_new(folder / "05_secondary.json", doc["secondary"])
    _write_new(folder / "06_decision.json", doc["decision"])
    g = doc["decision"]["global"]
    print(f"arm {args.arm}: global verdict {g['verdict']} (H1 only); holdout_clean={clean}; written to {folder}")
    for hy in ("h1", "h2"):
        print(" ", hy, {h: v["verdict"] for h, v in doc["decision"][hy].items()})
    return 0


# --------------------------------------------------------------------------------------------------- fetch-usd


def fetch_daily(
    product: str,
    start: datetime,
    end: datetime,
    get: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> pd.DataFrame:
    """Public daily candles of ``product`` for ``[start, end)``, 300-day windows, with retries on 429/5xx. No keys."""
    import requests

    getter = get or requests.get
    rows: dict[int, tuple[float, float, float, float, float]] = {}
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(days=300), end)
        params = {
            "start": int(cur.timestamp()), "end": int(nxt.timestamp()), "granularity": PREREG["arm_L"]["granularity"],
        }  # fmt: skip
        for attempt in range(6):
            resp = getter(CANDLES_URL.format(pid=product), params=params, timeout=30)
            if resp.status_code == 200:
                break
            if resp.status_code == 429 or resp.status_code >= 500:
                sleep(min(60.0, 2.0**attempt))
                continue
            raise RuntimeError(f"{product}: HTTP {resp.status_code} for window {cur.date()}")
        else:
            raise RuntimeError(f"{product}: too many retries for window {cur.date()}")
        for c in resp.json().get("candles", []):
            s = int(c["start"])
            if int(cur.timestamp()) <= s < int(nxt.timestamp()):
                rows[s] = (float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"]), float(c["volume"]))
        sleep(0.25)
        cur = nxt
    frame = pd.DataFrame(
        [(pd.Timestamp(s, unit="s", tz="UTC"), *v) for s, v in sorted(rows.items())],
        columns=["ts", "open", "high", "low", "close", "volume"],
    )
    frame["ts"] = frame["ts"].astype("datetime64[ns, UTC]")
    return frame


def cmd_fetch_usd(products: Sequence[str], out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    for p in products:
        frame = fetch_daily(p, datetime(2014, 12, 1, tzinfo=UTC), today)
        path = out / f"{p}_1d.parquet"
        frame.to_parquet(path, index=False)
        print(
            f"{p}: {len(frame)} daily candles, first {frame['ts'].iloc[0].date()}, "
            f"last {frame['ts'].iloc[-1].date()} -> {path}"
        )
    return 0


# --------------------------------------------------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch-usd", help="download public daily Coinbase candles (no calculation)")
    f.add_argument("--products", nargs="+", default=list(PREREG["arm_L"]["products"]))
    f.add_argument("--out", type=Path, default=Path("data/external/prices"))
    r = sub.add_parser("run", help="the one confirmatory run of an arm")
    r.add_argument("--arm", choices=["E", "L"], required=True)
    r.add_argument("--bars-root", type=Path, default=Path("data/bars/1m"))
    r.add_argument("--usd-daily", type=Path, default=None)
    r.add_argument("--exploration-range", nargs="+", required=True, metavar="unknown|START END")
    r.add_argument("--out", type=Path, default=Path("data/reports/study_volume"))
    a = ap.parse_args(argv)
    if a.cmd == "fetch-usd":
        return cmd_fetch_usd(a.products, a.out)
    rng: list[str] = a.exploration_range
    if rng != ["unknown"] and len(rng) != 2:
        ap.error("--exploration-range takes 'unknown' or START END")
    return run_study(RunArgs(a.arm, a.bars_root, a.usd_daily, rng, a.out))


if __name__ == "__main__":
    raise SystemExit(main())
