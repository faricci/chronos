"""Pre-registered backtest of hypotheses A (Donchian 50/20 trend) and B (monthly relative-momentum rotation).

::

    python -m script.study_trend_rotation run --usd-daily data/external/prices --bars-root data/bars/1m \
        --out data/reports/study_trend_rotation

Specification: docs/decisions/2026-10-28-ipotesi-strategie-laboratorio.md (sections 2-4), approved 2026-10-28 and
committed BEFORE this code. Every parameter is in ``PREREG`` (its sha256 goes in ``00_preregistration.json``, together
with the sha256 of this script and of the document, written before any bar is read). Offline research: read-only on
the data, no order, no balance, no trading module is imported. One run: ``<out>/L/`` must not exist, and the run is
refused (exit 2) if this file has uncommitted changes. Nothing is printed about returns before ``06_decision.json``.

Order of the run (the holdout is read only after the training results are written and hashed):
``00`` preregistration -> training data (cut at 2020-12-31 when read) -> ``01`` data, ``02`` training results (with
the Holm p-values), ``03`` sha256 of ``02`` -> holdout and EUR replica data -> ``04`` holdout results,
``05`` secondary (exploratory) -> ``06`` decision.

Implementation conventions that the document leaves to the code (all fixed before the first run):

* Position state at execution day d depends only on closes <= d-1; entry/exit at the open of d. Day return of a held
  coin is open(d+1)/open(d) - 1; on the last day of a split it is close(d)/open(d) - 1 and the open position is
  closed at close(d) (``forced`` trade, cost included): training uses only data <= 2020-12-31, each split starts flat.
* Round-trip cost c (110 bps primary, 77 bps secondary) is charged as c/2 on every purchase and c/2 on every sale,
  multiplicatively on the equity; a switch of coin is one sale plus one purchase (= one round trip).
* A: one sleeve per coin with fixed weight 1/N and never transferred; the portfolio return is
  sum(E_i(d)) / sum(E_i(d-1)) - 1; a sleeve is cash until its Donchian windows (complete, no missing day) exist.
* B: one pot, rebalanced on the first UTC day of each month with R28 of the previous day; the benchmark B0 holds
  1/N sleeves invested while R28 > 0. B and B0 start at the first rebalance with R28 available for every coin.
  A B "trade" is one holding period of one coin.
* p-value of a test = max(stationary bootstrap, Newey-West t); an under-sized test enters Holm with p = 1, and C, D
  (not tested here) too; interval 90% = widest of the two methods.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats as sps

from script import study_volume_patterns as svp

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_REL = "script/study_trend_rotation.py"
DOC_REL = "docs/decisions/2026-10-28-ipotesi-strategie-laboratorio.md"
DEPENDENCY_REL = "script/study_volume_patterns.py"
SCHEMA = "chronos.study_trend_rotation.v1"
EXIT_REFUSED = 2
ARM = "L"

PREREG: dict[str, Any] = {
    "schema": SCHEMA,
    "A": {"entry_window": 50, "exit_window": 20},
    "B": {"lookback": 28, "hysteresis": 0.05},
    "costs_round_trip": {"primary": 0.011, "secondary": 0.0077},
    "bootstrap": {"B": 10000, "mean_block": 20, "seed": 20261028, "tail": 0.05},
    "hac_lags": 20,
    "alpha": 0.05,
    "holm_family": ["A", "B", "C", "D"],
    "holm_untested_p": 1.0,
    "min_trades": {"train": 30, "holdout": 15},
    "holdout_min_fraction_of_train": [1, 3],
    "max_drawdown_ratio_vs_buy_and_hold": 0.6,
    "days_per_year": 365,
    "train_end": "2020-12-31",
    "holdout_start": "2021-01-01",
    "holdout_end": "2023-09-30",
    "replica_start": "2023-10-01",
    "holdout_clean": True,
    "products_L": ["BTC-USD", "ETH-USD"],
    "products_replica": ["BTC-EUR", "ETH-EUR", "SOL-EUR"],
    "seed_offsets": {"A": {"train": 1, "holdout": 11, "replica": 21}, "B": {"train": 2, "holdout": 12, "replica": 22}},
}
ENTRY_W = int(PREREG["A"]["entry_window"])
EXIT_W = int(PREREG["A"]["exit_window"])
LOOKBACK = int(PREREG["B"]["lookback"])
HYST = float(PREREG["B"]["hysteresis"])
COST_PRIMARY = float(PREREG["costs_round_trip"]["primary"])
COST_SECONDARY = float(PREREG["costs_round_trip"]["secondary"])
BOOT_B = int(PREREG["bootstrap"]["B"])
BOOT_BLOCK = int(PREREG["bootstrap"]["mean_block"])
BOOT_SEED = int(PREREG["bootstrap"]["seed"])
TAIL = float(PREREG["bootstrap"]["tail"])
HAC_LAGS = int(PREREG["hac_lags"])
ALPHA = float(PREREG["alpha"])
MIN_TRAIN = int(PREREG["min_trades"]["train"])
MIN_HOLD = int(PREREG["min_trades"]["holdout"])
HOLD_FRACTION = PREREG["holdout_min_fraction_of_train"][0] / PREREG["holdout_min_fraction_of_train"][1]
DD_RATIO = float(PREREG["max_drawdown_ratio_vs_buy_and_hold"])
DPY = int(PREREG["days_per_year"])
DAY = pd.Timedelta(days=1)

Frames = Mapping[str, pd.DataFrame]


# ------------------------------------------------------------------------------------------------ statistics


def stationary_means(x: np.ndarray, B: int, mean_block: int, seed: int) -> np.ndarray:
    """Means of ``B`` stationary-bootstrap resamples (Politis-Romano, geometric blocks of mean ``mean_block``)."""
    n = len(x)
    rng = np.random.default_rng(seed)
    out = np.empty(B)
    j = np.arange(n)
    chunk = max(1, min(B, 2_000_000 // max(n, 1)))
    for lo in range(0, B, chunk):
        k = min(chunk, B - lo)
        start = rng.integers(0, n, size=(k, n))
        new = rng.random((k, n)) < 1.0 / mean_block
        new[:, 0] = True
        last = np.maximum.accumulate(np.where(new, j, -1), axis=1)
        idx = (np.take_along_axis(start, last, axis=1) + (j - last)) % n
        out[lo : lo + k] = x[idx].mean(axis=1)
    return out


def hac_t(x: np.ndarray, lags: int = HAC_LAGS) -> tuple[float, float]:
    """Mean and its Newey-West (Bartlett kernel) standard error."""
    n = len(x)
    m = float(x.mean())
    d = x - m
    omega = float(d @ d) / n
    for lag in range(1, min(lags, n - 1) + 1):
        omega += 2.0 * (1.0 - lag / (lags + 1.0)) * float(d[lag:] @ d[:-lag]) / n
    return m, math.sqrt(max(omega, 0.0) / n)


@dataclass(frozen=True)
class MeanTest:
    """One-sided test of H0 'mean <= 0' for a daily series; ``p`` = max(bootstrap, HAC t), interval = widest."""

    n: int
    mean: float
    lb: float
    ub: float
    p_boot: float
    p_hac: float
    t_hac: float

    @property
    def p(self) -> float:
        return max(self.p_boot, self.p_hac)


def mean_test(x: Sequence[float] | np.ndarray, seed: int, B: int = BOOT_B) -> MeanTest:
    a = np.asarray(x, dtype=float)
    a = a[np.isfinite(a)]
    n = len(a)
    if n < 3:
        return MeanTest(n, float("nan"), float("nan"), float("nan"), 1.0, 1.0, float("nan"))
    means = stationary_means(a, B, BOOT_BLOCK, seed)
    p_boot = float((1 + (means <= 0).sum()) / (B + 1))
    b_lo, b_hi = np.percentile(means, [100 * TAIL, 100 * (1 - TAIL)])
    m, se = hac_t(a)
    q = float(sps.t.ppf(1 - TAIL, n - 1))
    if se > 0:
        t_stat = m / se
        p_hac = float(sps.t.sf(t_stat, n - 1))
    else:
        t_stat = float("inf") if m > 0 else float("-inf") if m < 0 else 0.0
        p_hac = 0.0 if m > 0 else 1.0
    return MeanTest(n, m, min(float(b_lo), m - q * se), max(float(b_hi), m + q * se), p_boot, p_hac, t_stat)


def max_drawdown(ret: Sequence[float] | np.ndarray) -> float:
    """Largest peak-to-trough loss (positive fraction) of the equity curve starting at 1."""
    r = np.asarray(ret, dtype=float)
    if len(r) == 0:
        return float("nan")
    eq = np.cumprod(1.0 + r)
    peak = np.maximum.accumulate(np.r_[1.0, eq])[1:]
    return float((1.0 - eq / peak).max())


def total_return(ret: Sequence[float] | np.ndarray) -> float:
    r = np.asarray(ret, dtype=float)
    return float(np.prod(1.0 + r) - 1.0) if len(r) else float("nan")


def sharpe_annual(ret: Sequence[float] | np.ndarray) -> float:
    r = np.asarray(ret, dtype=float)
    if len(r) < 2 or r.std(ddof=1) == 0:
        return float("nan")
    return float(r.mean() / r.std(ddof=1) * math.sqrt(DPY))


def mean_without_best_year(x: pd.Series) -> float:
    """Mean of the daily series without the calendar year with the largest total contribution."""
    if x.empty:
        return float("nan")
    year = pd.DatetimeIndex(x.index).year
    sums = x.groupby(year).sum()
    rest = x[year != sums.idxmax()]
    return float(rest.mean()) if len(rest) else float("nan")


holm = svp.holm


# ------------------------------------------------------------------------------------------- strategy simulation


@dataclass(frozen=True)
class Trade:
    coin: str
    entry: pd.Timestamp
    exit: pd.Timestamp
    entry_px: float
    exit_px: float
    forced: bool
    gross: float
    net: float


@dataclass(frozen=True)
class Pot:
    """Daily net return of a pot that holds one coin (or cash) per day, and its trades."""

    ret: pd.Series
    trades: list[Trade]


def _side(round_trip: float) -> float:
    return round_trip / 2.0


def simulate_pot(
    days: pd.DatetimeIndex,
    held: Sequence[str | None],
    opens: Mapping[str, pd.Series],
    closes: Mapping[str, pd.Series],
    round_trip: float,
) -> Pot:
    """Equity of a pot holding ``held[i]`` from the open of ``days[i]`` (None = cash).

    Day return of a held coin: open(d+1)/open(d) - 1 (close(d)/open(d) - 1 on the last day, where the position is
    then closed at close(d)). Sale and purchase cost ``round_trip / 2`` each, charged on the equity at the open of d.
    """
    if len(days) != len(held):
        raise ValueError("held must have one entry per day")
    cs = _side(round_trip)
    n = len(days)
    ret = np.zeros(n)
    trades: list[Trade] = []
    prev: str | None = None
    entry_i = -1
    entry_px = float("nan")
    for i in range(n):
        cur = held[i]
        d = days[i]
        factor = 1.0
        if prev is not None and prev != cur:
            factor *= 1.0 - cs
            px = float(opens[prev].loc[d])
            g = px / entry_px - 1.0
            trades.append(Trade(prev, days[entry_i], d, entry_px, px, False, g, (1.0 + g) * (1.0 - cs) ** 2 - 1.0))
        if cur is not None and cur != prev:
            factor *= 1.0 - cs
            entry_i, entry_px = i, float(opens[cur].loc[d])
        if cur is not None:
            o = float(opens[cur].loc[d])
            end = float(closes[cur].loc[d]) if i == n - 1 else float(opens[cur].loc[days[i + 1]])
            factor *= end / o
            if i == n - 1:
                factor *= 1.0 - cs
                g = end / entry_px - 1.0
                trades.append(Trade(cur, days[entry_i], d, entry_px, end, True, g, (1.0 + g) * (1.0 - cs) ** 2 - 1.0))
        ret[i] = factor - 1.0
        prev = cur
    return Pot(pd.Series(ret, index=days), trades)


def combine_sleeves(rets: Sequence[pd.Series]) -> pd.Series:
    """Portfolio return of equal sleeves that are never rebalanced: sum(E_i(d)) / sum(E_i(d-1)) - 1."""
    n = len(rets)
    eq = pd.concat([(1.0 + r).cumprod() / n for r in rets], axis=1).sum(axis=1)
    return pd.Series(eq / eq.shift(1).fillna(1.0) - 1.0, index=eq.index)


def clean_prices(frame: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Open and close with a missing day carried forward (only used to value an open position)."""
    return frame["open"].ffill(), frame["close"].ffill()


def donchian_flags(close: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Entry flag C_t > max(C_(t-50..t-1)) and exit flag C_t < min(C_(t-20..t-1)); False if a window has a gap."""
    top = close.rolling(ENTRY_W, min_periods=ENTRY_W).max().shift(1)
    bottom = close.rolling(EXIT_W, min_periods=EXIT_W).min().shift(1)
    return close > top, close < bottom


def first_exec_day_a(close: pd.Series) -> pd.Timestamp | None:
    """First execution day with a computable entry signal (day after the first t with a complete 50-day window)."""
    top = close.rolling(ENTRY_W, min_periods=ENTRY_W).max().shift(1)
    ok = top.notna() & close.notna()
    return pd.Timestamp(str(ok.idxmax())) + DAY if bool(ok.any()) else None


def trend_state(close: pd.Series, days: pd.DatetimeIndex) -> list[bool]:
    """State machine A for one coin on the execution days ``days`` (flat before the first day); True = invested."""
    enter, leave = donchian_flags(close)
    pos = close.index.get_indexer(days)
    states: list[bool] = []
    state = False
    for p in pos:
        if p >= 1:
            if not state and bool(enter.iloc[p - 1]):
                state = True
            elif state and bool(leave.iloc[p - 1]):
                state = False
        states.append(state)
    return states


def held_from_states(coin: str, states: Sequence[bool]) -> list[str | None]:
    return [coin if s else None for s in states]


def aligned(frames: Frames) -> dict[str, pd.DataFrame]:
    """All coins on one daily index (union of the ranges); a day a coin does not have is NaN."""
    first = min(f.index[0] for f in frames.values())
    last = max(f.index[-1] for f in frames.values())
    idx = pd.date_range(first, last, freq="D")
    return {c: f.reindex(idx) for c, f in frames.items()}


def rotation_r28(close: pd.Series) -> pd.Series:
    return pd.Series(np.log(close / close.shift(LOOKBACK)), index=close.index)


def rotation_choice(r: Mapping[str, float], current: str | None, order: Sequence[str]) -> str | None:
    """Monthly decision of B from R28 of each coin: candidate = max R28, cash if <= 0, hysteresis on a switch."""
    valid = {c: v for c, v in r.items() if math.isfinite(v)}
    if not valid:
        return None
    cand = max(valid, key=lambda c: (valid[c], -order.index(c)))
    if valid[cand] <= 0:
        return None
    if current is not None and current != cand and current in valid and valid[current] > 0:
        return current if valid[cand] - valid[current] <= HYST else cand
    return cand


@dataclass(frozen=True)
class RotationPaths:
    days: pd.DatetimeIndex
    b_held: list[str | None]
    b0_held: dict[str, list[str | None]]
    choices: list[tuple[pd.Timestamp, str | None]]


def rotation_paths(frames: Frames, lo: pd.Timestamp, hi: pd.Timestamp, order: Sequence[str]) -> RotationPaths | None:
    """Held coin of B and of the benchmark B0 from the first rebalance with R28 available for every coin.

    The decision of the first of the month uses R28 at the last day of the previous month and is executed at the
    open of the first day. Each split starts in cash. Returns None if no rebalance is possible in ``[lo, hi]``.
    """
    idx = pd.DatetimeIndex(frames[order[0]].index)
    r28 = {c: rotation_r28(frames[c]["close"]) for c in order}
    rebalance = [d for d in idx[(idx >= lo) & (idx <= hi)] if d.day == 1 and d - DAY in idx]
    rebalance = [d for d in rebalance if all(math.isfinite(float(r28[c].loc[d - DAY])) for c in order)]
    if not rebalance:
        return None
    days = idx[(idx >= rebalance[0]) & (idx <= hi)]
    reb = {d for d in days if d.day == 1}
    b_cur: str | None = None
    b0_cur: dict[str, str | None] = {c: None for c in order}
    b_held: list[str | None] = []
    b0_held: dict[str, list[str | None]] = {c: [] for c in order}
    choices: list[tuple[pd.Timestamp, str | None]] = []
    for d in days:
        if d in reb:
            r = {c: float(r28[c].loc[d - DAY]) for c in order}
            b_cur = rotation_choice(r, b_cur, order)
            choices.append((d, b_cur))
            b0_cur = {c: (c if math.isfinite(r[c]) and r[c] > 0 else None) for c in order}
        b_held.append(b_cur)
        for c in order:
            b0_held[c].append(b0_cur[c])
    return RotationPaths(days, b_held, b0_held, choices)


# -------------------------------------------------------------------------------------------- split evaluation


@dataclass
class HypResult:
    """Everything that is decided or reported for one hypothesis in one split."""

    hypothesis: str
    split: str
    lo: pd.Timestamp | None
    hi: pd.Timestamp
    n_trades: int
    test: MeanTest
    mean_wo_best_year: float
    max_dd: float
    reference_max_dd: float
    total_ret: float
    reference_total_ret: float
    sharpe: float
    secondary: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def mean(self) -> float:
        return self.test.mean


def _seed(hyp: str, split: str) -> int:
    return BOOT_SEED + int(PREREG["seed_offsets"][hyp][split])


def _by_coin(trades: Sequence[Trade]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for c in sorted({t.coin for t in trades}):
        net = [t.net for t in trades if t.coin == c]
        out[c] = {
            "n_trades": len(net),
            "mean_trade_net": float(np.mean(net)),
            "win_rate": float(np.mean(np.array(net) > 0)),
        }
    return out


def evaluate_a(frames: Frames, split: str, lo: pd.Timestamp | None, hi: pd.Timestamp) -> HypResult | None:
    """Hypothesis A on ``[lo, hi]`` (``lo`` None: first execution day of any coin)."""
    frames = aligned(frames)
    order = list(frames)
    firsts = {c: first_exec_day_a(frames[c]["close"]) for c in order}
    avail = [d for d in firsts.values() if d is not None]
    if not avail:
        return None
    start = max(lo, min(avail)) if lo is not None else min(avail)
    idx = pd.DatetimeIndex(frames[order[0]].index)
    days = idx[(idx >= start) & (idx <= hi)]
    if len(days) < 3:
        return None
    px = {c: clean_prices(frames[c]) for c in order}
    opens = {c: p[0] for c, p in px.items()}
    closes = {c: p[1] for c, p in px.items()}
    states = {c: trend_state(frames[c]["close"], days) for c in order}

    def run(cost: float) -> tuple[pd.Series, list[Trade]]:
        pots = [simulate_pot(days, held_from_states(c, states[c]), opens, closes, cost) for c in order]
        return combine_sleeves([p.ret for p in pots]), [t for p in pots for t in p.trades]

    def bh_held(c: str) -> list[str | None]:
        first = firsts[c]
        return [c if (first is not None and d >= first) else None for d in days]

    port, trades = run(COST_PRIMARY)
    port77, _ = run(COST_SECONDARY)
    bh = combine_sleeves([simulate_pot(days, bh_held(c), opens, closes, 0.0).ret for c in order])
    test = mean_test(port.to_numpy(), _seed("A", split))
    t77 = mean_test(port77.to_numpy(), _seed("A", split) + 100)
    return HypResult(
        "A",
        split,
        start,
        hi,
        len(trades),
        test,
        mean_without_best_year(port),
        max_drawdown(port.to_numpy()),
        max_drawdown(bh.to_numpy()),
        total_return(port.to_numpy()),
        total_return(bh.to_numpy()),
        sharpe_annual(port.to_numpy()),
        secondary={
            "net77": {"mean": t77.mean, "p": t77.p, "lb": t77.lb, "ub": t77.ub},
            "by_coin": _by_coin(trades),
            "excess_vs_buy_and_hold_mean_daily": float((port - bh).mean()),
            "buy_and_hold_sharpe": sharpe_annual(bh.to_numpy()),
        },
        extra={
            "n_forced": sum(t.forced for t in trades),
            "mean_trade_gross": float(np.mean([t.gross for t in trades])) if trades else float("nan"),
            "mean_trade_net": float(np.mean([t.net for t in trades])) if trades else float("nan"),
            "exposure_fraction_by_coin": {c: float(np.mean(states[c])) for c in order},
            "n_days": len(days),
            "reference": "buy and hold of the same sleeves (no cost)",
        },
    )


def evaluate_b(frames: Frames, split: str, lo: pd.Timestamp, hi: pd.Timestamp) -> HypResult | None:
    """Hypothesis B on ``[lo, hi]``: statistic = mean daily (B net - B0 net)."""
    frames = aligned(frames)
    order = list(frames)
    paths = rotation_paths(frames, lo, hi, order)
    if paths is None or len(paths.days) < 3:
        return None
    px = {c: clean_prices(frames[c]) for c in order}
    opens = {c: p[0] for c, p in px.items()}
    closes = {c: p[1] for c, p in px.items()}
    days = paths.days

    def run(cost: float) -> tuple[Pot, pd.Series]:
        b = simulate_pot(days, paths.b_held, opens, closes, cost)
        b0 = combine_sleeves([simulate_pot(days, paths.b0_held[c], opens, closes, cost).ret for c in order])
        return b, b0

    b, b0 = run(COST_PRIMARY)
    b77, b077 = run(COST_SECONDARY)
    diff = b.ret - b0
    test = mean_test(diff.to_numpy(), _seed("B", split))
    t77 = mean_test((b77.ret - b077).to_numpy(), _seed("B", split) + 100)
    changes = sum(1 for i in range(1, len(paths.choices)) if paths.choices[i][1] != paths.choices[i - 1][1])
    return HypResult(
        "B",
        split,
        days[0],
        hi,
        len(b.trades),
        test,
        mean_without_best_year(diff),
        max_drawdown(b.ret.to_numpy()),
        max_drawdown(b0.to_numpy()),
        total_return(b.ret.to_numpy()),
        total_return(b0.to_numpy()),
        sharpe_annual(diff.to_numpy()),
        secondary={
            "net77": {"mean": t77.mean, "p": t77.p, "lb": t77.lb, "ub": t77.ub},
            "by_coin": _by_coin(b.trades),
            "b_net_mean_daily": float(b.ret.mean()),
            "b0_net_mean_daily": float(b0.mean()),
            "b_sharpe": sharpe_annual(b.ret.to_numpy()),
            "b0_sharpe": sharpe_annual(b0.to_numpy()),
        },
        extra={
            "n_rebalances": len(paths.choices),
            "n_changes_of_position": changes,
            "n_forced": sum(t.forced for t in b.trades),
            "mean_trade_net": float(np.mean([t.net for t in b.trades])) if b.trades else float("nan"),
            "n_days": len(days),
            "reference": "B0 = 1/N sleeves invested while R28 > 0, same cadence and costs",
        },
    )


# --------------------------------------------------------------------------------------------------- verdicts


@dataclass(frozen=True)
class Verdict:
    verdict: str
    passed: list[str]
    failed: list[str]
    notes: list[str] = field(default_factory=list)


def decide(
    hyp: str,
    train: HypResult | None,
    hold: HypResult | None,
    p_holm_train: float,
    replica: HypResult | None,
    holdout_clean: bool = True,
) -> Verdict:
    """Section 4 rules for A and B: INCONCLUSO (n) / NO-GO / GO / INCONCLUSO."""
    if train is None or hold is None or train.n_trades < MIN_TRAIN or hold.n_trades < MIN_HOLD:
        nt = None if train is None else train.n_trades
        nh = None if hold is None else hold.n_trades
        return Verdict(
            "INCONCLUSO", [], ["INS_n"], [f"n_train={nt} (min {MIN_TRAIN}), n_holdout={nh} (min {MIN_HOLD})"]
        )
    a_true = p_holm_train < ALPHA
    if train.mean <= 0:
        return Verdict("NO-GO", [], ["N1_net_train<=0"])
    if a_true and hold.mean < 0:
        return Verdict("NO-GO", [], ["N2_pHolm<0.05_but_holdout<0"])
    cond: dict[str, bool] = {
        "a_pHolm_train<0.05": a_true,
        "b_holdout>0_and_>=1/3_train": hold.mean > 0 and hold.mean >= HOLD_FRACTION * train.mean,
        "c_train_without_best_year>0": train.mean_wo_best_year > 0,
    }
    notes: list[str] = []
    if replica is None:
        notes.append("d: replica not evaluable, does not block")
    else:
        cond["d_replica_does_not_block"] = not (replica.mean < 0 and replica.test.ub < 0)
    if hyp == "A":
        cond["e_holdout_dd<=0.6_buy_and_hold_dd"] = hold.max_dd <= DD_RATIO * hold.reference_max_dd
    passed = [k for k, v in cond.items() if v]
    failed = [k for k, v in cond.items() if not v]
    if failed:
        return Verdict("INCONCLUSO", passed, failed, notes)
    return Verdict("GO" if holdout_clean else "CANDIDATO", passed, [], notes)


def holm_family(p_a: float, p_b: float) -> dict[str, float]:
    """Holm-adjusted p of the family {A, B, C, D}: C and D are not tested here and enter with p = 1."""
    u = float(PREREG["holm_untested_p"])
    adj = holm([p_a, p_b, u, u])
    return {"A": adj[0], "B": adj[1], "C": adj[2], "D": adj[3]}


def train_p(res: HypResult | None) -> float:
    """p entering Holm: 1 if the test is missing or has fewer than the minimum number of trades."""
    if res is None or res.n_trades < MIN_TRAIN:
        return 1.0
    return res.test.p


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
        return str(obj.date())
    return obj


def _write_new(path: Path, doc: Mapping[str, Any]) -> bytes:
    """Exclusive open: each file of the run is written once. Returns the bytes written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = (json.dumps(_clean(dict(doc)), sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
    with open(path, "xb") as fh:
        fh.write(text)
    return text


def _sha256_file(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def prereg_sha256() -> str:
    return hashlib.sha256(json.dumps(PREREG, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def script_is_clean(rel: str = SCRIPT_REL) -> bool:
    """True only if the script is tracked and has no modification (staged, unstaged) in git."""
    try:
        tracked = (
            subprocess.run(
                ["git", "ls-files", "--error-unmatch", "--", rel], cwd=REPO_ROOT, capture_output=True, text=True,
                timeout=30,
            ).returncode == 0
        )  # fmt: skip
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", rel], cwd=REPO_ROOT, capture_output=True, text=True, check=True,
            timeout=30,
        ).stdout.strip()  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return False
    return tracked and not status


def hyp_doc(r: HypResult | None) -> dict[str, Any] | None:
    if r is None:
        return None
    return {
        "hypothesis": r.hypothesis,
        "split": r.split,
        "first_day": r.lo,
        "last_day": r.hi,
        "n_trades": r.n_trades,
        "n_days": r.test.n,
        "mean_daily_net": r.test.mean,
        "mean_annualised_net": r.test.mean * DPY,
        "ci90_daily": [r.test.lb, r.test.ub],
        "p_boot": r.test.p_boot,
        "p_hac": r.test.p_hac,
        "p": r.test.p,
        "t_hac": r.test.t_hac,
        "mean_daily_net_without_best_year": r.mean_wo_best_year,
        "sharpe_annual": r.sharpe,
        "max_drawdown": r.max_dd,
        "reference_max_drawdown": r.reference_max_dd,
        "total_return_net": r.total_ret,
        "reference_total_return": r.reference_total_ret,
        "secondary_exploratory": r.secondary,
        **r.extra,
    }


def _verdict_doc(v: Verdict) -> dict[str, Any]:
    return {"verdict": v.verdict, "passed": v.passed, "failed": v.failed, "notes": v.notes}


def _coverage(frames: Frames) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for c, f in frames.items():
        ok = f["close"].notna()
        out[c] = {
            "first_day": f.index[0], "last_day": f.index[-1], "days": len(f), "missing_days": int((~ok).sum()),
            "missing_list_top20": [str(d.date()) for d in f.index[~ok.to_numpy()][:20]],
        }  # fmt: skip
    return out


# ------------------------------------------------------------------------------------------------------ run


def load_usd(path: Path, end: pd.Timestamp | None) -> pd.DataFrame:
    """Daily USD candles; with ``end`` the rows after that day are dropped at read time (training stage)."""
    frame = pd.read_parquet(path)
    if end is not None:
        ts = pd.to_datetime(frame["ts"], utc=True).dt.tz_localize(None)
        frame = frame[(ts <= end).to_numpy()]
    return svp.daily_from_usd(frame)


@dataclass
class RunArgs:
    bars_root: Path
    usd_daily: Path
    out: Path


@dataclass
class Env:
    head: Callable[[], str | None] = svp.git_head
    script_clean: Callable[[], bool] = script_is_clean
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    usd_loader: Callable[[Path, pd.Timestamp | None], pd.DataFrame] = load_usd
    replica_loader: Callable[[Path, str], pd.DataFrame] | None = None


def _default_replica(root: Path, product: str) -> pd.DataFrame:
    src = svp.StoreSource(root)
    return svp.daily_from_1m(src.bars(product), src.real_ts(product))


def run_study(args: RunArgs, env: Env | None = None) -> int:
    env = env or Env()
    folder = args.out / ARM
    if folder.exists():
        print(f"refused: {folder} already exists (one run)", file=sys.stderr)
        return EXIT_REFUSED
    if not env.script_clean():
        print(f"refused: {SCRIPT_REL} is not tracked or has uncommitted changes", file=sys.stderr)
        return EXIT_REFUSED
    train_end = pd.Timestamp(PREREG["train_end"])
    h_start, h_end = pd.Timestamp(PREREG["holdout_start"]), pd.Timestamp(PREREG["holdout_end"])
    r_start = pd.Timestamp(PREREG["replica_start"])

    # 00: BEFORE reading any bar
    _write_new(
        folder / "00_preregistration.json",
        {
            "schema": SCHEMA, "arm": ARM, "git_commit": env.head(),
            "script_sha256": _sha256_file(REPO_ROOT / SCRIPT_REL), "document_sha256": _sha256_file(REPO_ROOT / DOC_REL),
            "dependency_sha256": {DEPENDENCY_REL: _sha256_file(REPO_ROOT / DEPENDENCY_REL)},
            "prereg_sha256": prereg_sha256(), "prereg": PREREG, "created_utc": env.now().isoformat(),
        },
    )  # fmt: skip

    # stage 1: training data only (cut when read)
    products = list(PREREG["products_L"])
    paths = {p: Path(args.usd_daily) / f"{p}_1d.parquet" for p in products}
    train_frames = {svp.coin_of(p): env.usd_loader(paths[p], train_end) for p in products}
    _write_new(
        folder / "01_data.json",
        {
            "stage": "training data (rows after train_end dropped at read time)", "training": _coverage(train_frames),
            "usd_parquet_sha256": {p: _sha256_file(paths[p]) for p in products}, "coinbase_terms": svp.COINBASE_TERMS,
        },
    )  # fmt: skip
    a_tr = evaluate_a(train_frames, "train", None, train_end)
    b_tr = evaluate_b(train_frames, "train", min(f.index[0] for f in train_frames.values()), train_end)
    p_raw = {"A": train_p(a_tr), "B": train_p(b_tr)}
    p_holm = holm_family(p_raw["A"], p_raw["B"])
    data = _write_new(
        folder / "02_train_results.json",
        {"A": hyp_doc(a_tr), "B": hyp_doc(b_tr), "p_raw_for_holm": p_raw, "p_holm": p_holm, "train_end": train_end},
    )
    _write_new(
        folder / "03_train_hash.json",
        {
            "file": "02_train_results.json",
            "sha256": hashlib.sha256(data).hexdigest(),
            "hashed_utc": env.now().isoformat(),
        },
    )

    # stage 2: only now the holdout and the EUR replica are read
    full = {svp.coin_of(p): env.usd_loader(paths[p], None) for p in products}
    rep_loader = env.replica_loader or _default_replica
    replica = {svp.coin_of(p): rep_loader(args.bars_root, p) for p in PREREG["products_replica"]}
    a_ho = evaluate_a(full, "holdout", h_start, h_end)
    b_ho = evaluate_b(full, "holdout", h_start, h_end)
    rep_end = min(f.index[-1] for f in replica.values())
    a_rp = evaluate_a(replica, "replica", r_start, rep_end)
    b_rp = evaluate_b(replica, "replica", r_start, rep_end)
    _write_new(
        folder / "04_holdout_results.json",
        {
            "holdout": {"A": hyp_doc(a_ho), "B": hyp_doc(b_ho)},
            "replica_eur": {"A": hyp_doc(a_rp), "B": hyp_doc(b_rp), "coverage": _coverage(replica)},
            "holdout_coverage": _coverage(full),
        },
    )
    train_by = {"A": a_tr, "B": b_tr}
    hold_by = {"A": a_ho, "B": b_ho}
    rep_by = {"A": a_rp, "B": b_rp}
    verdicts = {
        h: decide(h, train_by[h], hold_by[h], p_holm[h], rep_by[h], bool(PREREG["holdout_clean"])) for h in ("A", "B")
    }
    _write_new(
        folder / "05_secondary.json",
        {
            "label": "exploratory: does not create or reopen a verdict",
            "A": {s: (r.secondary if r else None) for s, r in (("train", a_tr), ("holdout", a_ho), ("replica", a_rp))},
            "B": {s: (r.secondary if r else None) for s, r in (("train", b_tr), ("holdout", b_ho), ("replica", b_rp))},
            "not_computed": "A weighted by 1/sigma60 (weight timing not defined by the document)",
        },
    )
    _write_new(
        folder / "06_decision.json",
        {
            "arm": ARM, "p_holm_train": p_holm, "p_raw_for_holm": p_raw,
            "A": _verdict_doc(verdicts["A"]), "B": _verdict_doc(verdicts["B"]),
            "note": "C and D are not part of this run (Holm p = 1)",
        },
    )  # fmt: skip
    print(f"arm {ARM}: A {verdicts['A'].verdict}, B {verdicts['B'].verdict}; written to {folder}")
    return 0


# --------------------------------------------------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="the one confirmatory run (A and B together: they share the Holm family)")
    r.add_argument("--bars-root", type=Path, default=Path("data/bars/1m"))
    r.add_argument("--usd-daily", type=Path, default=Path("data/external/prices"))
    r.add_argument("--out", type=Path, default=Path("data/reports/study_trend_rotation"))
    a = ap.parse_args(argv)
    return run_study(RunArgs(a.bars_root, a.usd_daily, a.out))


if __name__ == "__main__":
    raise SystemExit(main())
