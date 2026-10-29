"""Count the SIGNALS (no return, no outcome, no simulated exit) of hypotheses C and D in the TRAINING period only.

::

    python -m script.count_signals_cd --usd-dir data/external/prices --bars-root data/bars/1m \
        --out data/reports/count_signals_cd/counts.json

Specification: docs/decisions/2026-10-28-ipotesi-strategie-laboratorio.md sections 2-6 and 10 (E1-E10) and
docs/decisions/2026-10-28-book-laboratorio-spec.md section 13 (Q3, Q4). The signal rules live in
``modules/lab/signals_cd.py`` (reused by the real backtest). Sizing question answered here: is there enough material
to reach the minimum of 150 independent units in training and 60 in holdout (section 4)?

Data and cuts (no holdout bar is used):

* arm U (decides C and D): BTC-USD and ETH-USD 15m, read with a parquet filter ``ts < 2021-01-01`` (training ends
  2020-12-31 inclusive; ETH starts 2016-05-18). A 1h or 4h bar exists if at least one 15m bar falls in it; a hole in a
  window gives no signal (E5, E6).
* arm E (information only: the document has no EUR decision for C and D): SOL-EUR 1m from the store via
  ``load_bars``, 2023-10-01 .. 2025-08-31 (training of arm E, Q4); q20 of SOL is estimated on those bars.

Independence unit (sections 3.C, 3.D): events of different coins with the decision time within 24 h are one unit;
implemented as anchored clusters (a unit opens at its first event and absorbs the events of the next 24 h). Units
are computed per coin and per arm (merged stream).

D has no position state here. Bracket for the number of entries: ``raw_signal_bars`` is an upper bound (a trade needs
a signal bar), ``min_separation_61_bars`` is the count if every trade lasted the maximum 60 bars (a new entry is
evaluable from the close 61 bars after the entry decision), ``run_starts`` collapses consecutive signal bars.
For C a signal is not a trade: fill ~50-70% and the busy book reduce the trades further (units here are an upper
bound of the trade units).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from modules.lab.signals_cd import (
    REVERT,
    SQUEEZE,
    resample_bars,
    revert_events,
    squeeze_signals,
    squeeze_threshold,
    thin_events,
)
from modules.market.backfill import load_bars

SCHEMA = "chronos.count_signals_cd.v1"
USD_CUT = pd.Timestamp("2021-01-01", tz="UTC")  # exclusive: training ends 2020-12-31 inclusive
E_START = datetime(2023, 10, 1, tzinfo=UTC)
E_END = datetime(2025, 8, 31, 23, 59, tzinfo=UTC)
UNIT_GAP = pd.Timedelta(hours=24)
MIN_TRAIN_UNITS = 150
MIN_HOLDOUT_UNITS = 60
HOLDOUT_DAYS = 1003  # 2021-01-01 .. 2023-09-30, arm U (only its length is used, no bar is read)
ARM_OF = {"BTC-USD": "U", "ETH-USD": "U", "SOL-EUR": "E"}
USD_FILES = {"BTC-USD": "BTC-USD_15m.parquet", "ETH-USD": "ETH-USD_15m.parquet"}
BAR_4H = pd.Timedelta(hours=4)


# ------------------------------------------------------------------------------------------------ loading


def cut_training(frame: pd.DataFrame, cut: pd.Timestamp) -> pd.DataFrame:
    """Drop every row at or after ``cut`` (defence in depth: the reader already filters)."""
    return frame[(frame["ts"] < cut).to_numpy()].reset_index(drop=True)


def read_usd_training(path: Path, cut: pd.Timestamp = USD_CUT) -> pd.DataFrame:
    """15m USD candles with ``ts < cut``, the filter is pushed down to the parquet read."""
    frame = pd.read_parquet(path, filters=[("ts", "<", cut)])
    frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
    return cut_training(frame, cut)


def read_sol_training(root: Path) -> pd.DataFrame:
    """SOL-EUR 1m bars of the arm E training window."""
    frame = load_bars(root, "SOL-EUR", E_START, E_END).reset_index()  # load_bars indexes by ts
    frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
    return cut_training(frame, pd.Timestamp(E_END) + pd.Timedelta(minutes=1))


@dataclass(frozen=True)
class CoinBars:
    arm: str
    bars_1h: pd.DataFrame
    bars_4h: pd.DataFrame
    first_day: pd.Timestamp
    last_day: pd.Timestamp  # exclusive


def coin_bars(arm: str, base: pd.DataFrame) -> CoinBars:
    first = pd.Timestamp(base["ts"].iloc[0]).normalize()
    last = pd.Timestamp(base["ts"].iloc[-1]).normalize() + pd.Timedelta(days=1)
    return CoinBars(arm, resample_bars(base, "1h"), resample_bars(base, "4h"), first, last)


# ------------------------------------------------------------------------------------------------ signals


def c_times(bars_1h: pd.DataFrame) -> pd.DatetimeIndex:
    """Detected events of C (24 h deduplication of E6), bar open time."""
    return pd.DatetimeIndex(revert_events(bars_1h, REVERT).index)


def d_times(bars_4h: pd.DataFrame, q20: float) -> dict[str, pd.DatetimeIndex]:
    """Entry decisions of D under the three counting conventions (see module docstring)."""
    sig = squeeze_signals(bars_4h, q20, SQUEEZE).fillna(False).astype(bool)
    raw = pd.DatetimeIndex(sig.index[sig.to_numpy()])
    starts = sig & ~sig.shift(1, fill_value=False)
    return {
        "raw_signal_bars": raw,
        "run_starts": pd.DatetimeIndex(sig.index[starts.to_numpy()]),
        "min_separation_61_bars": thin_events(raw, BAR_4H * (SQUEEZE.max_hold_bars + 1)),
    }


# ------------------------------------------------------------------------------------------------ counting


def by_year(times: pd.DatetimeIndex) -> dict[str, int]:
    counts = Counter(int(t.year) for t in times)
    return {str(y): counts[y] for y in sorted(counts)}


def units(times: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Anchored 24 h clusters over the (merged) event stream: one timestamp (the anchor) per unit."""
    return thin_events(times.sort_values(), UNIT_GAP)


def describe(times: pd.DatetimeIndex) -> dict[str, Any]:
    u = units(times)
    return {"events": len(times), "events_by_year": by_year(times), "units": len(u), "units_by_year": by_year(u)}


def summarize(times_by_coin: Mapping[str, pd.DatetimeIndex], coins: Mapping[str, CoinBars]) -> dict[str, Any]:
    """Per coin and per arm (merged stream) counts, plus the projection of the units to the holdout length."""
    out: dict[str, Any] = {"coins": {c: describe(t) for c, t in times_by_coin.items()}, "arms": {}}
    for arm in sorted({cb.arm for cb in coins.values()}):
        members = [c for c in times_by_coin if coins[c].arm == arm]
        desc = describe(_union([times_by_coin[c] for c in members]))
        span_days = (max(coins[c].last_day for c in members) - min(coins[c].first_day for c in members)).days
        desc["training_span_days"] = span_days
        desc["units_per_year"] = round(desc["units"] / span_days * 365.25, 2)
        if arm == "U":  # only the arm that decides has a holdout of this length
            desc["holdout_projection_units"] = round(desc["units"] / span_days * HOLDOUT_DAYS, 1)
        out["arms"][arm] = desc
    return out


def _union(parts: Sequence[pd.DatetimeIndex]) -> pd.DatetimeIndex:
    merged = pd.DatetimeIndex([], tz="UTC")
    for p in parts:
        merged = pd.DatetimeIndex(merged.append(p))
    return merged.sort_values()


def classify(lower: float, upper: float, minimum: int) -> str:
    if lower >= minimum:
        return "sopra"
    if upper < minimum:
        return "sotto"
    return "ambiguo"


def verdict_c(c_summary: Mapping[str, Any]) -> dict[str, Any]:
    arm = c_summary["arms"]["U"]
    return {
        "training_units": arm["units"], "training_minimum": MIN_TRAIN_UNITS,
        "training": classify(arm["units"], arm["units"], MIN_TRAIN_UNITS),
        "holdout_projection_units": arm["holdout_projection_units"], "holdout_minimum": MIN_HOLDOUT_UNITS,
        "holdout": classify(arm["holdout_projection_units"], arm["holdout_projection_units"], MIN_HOLDOUT_UNITS),
        "note": "signals, not trades: fill ~50-70% and busy book lower the trade units",
    }  # fmt: skip


def verdict_d(d_variants: Mapping[str, Any]) -> dict[str, Any]:
    def arm(v: str) -> Mapping[str, Any]:
        return d_variants[v]["arms"]["U"]  # type: ignore[no-any-return]

    lo, hi = arm("min_separation_61_bars"), arm("raw_signal_bars")
    return {
        "training_units_lower": lo["units"], "training_units_middle": arm("run_starts")["units"],
        "training_units_upper": hi["units"], "training_minimum": MIN_TRAIN_UNITS,
        "training": classify(lo["units"], hi["units"], MIN_TRAIN_UNITS),
        "holdout_projection_lower": lo["holdout_projection_units"],
        "holdout_projection_upper": hi["holdout_projection_units"], "holdout_minimum": MIN_HOLDOUT_UNITS,
        "holdout": classify(lo["holdout_projection_units"], hi["holdout_projection_units"], MIN_HOLDOUT_UNITS),
    }  # fmt: skip


def build_counts(coins: Mapping[str, CoinBars]) -> dict[str, Any]:
    c_summary = summarize({c: c_times(cb.bars_1h) for c, cb in coins.items()}, coins)
    q20 = {c: squeeze_threshold(cb.bars_4h, SQUEEZE) for c, cb in coins.items()}
    per_coin = {c: d_times(cb.bars_4h, q20[c]) for c, cb in coins.items()}
    variants = {v: summarize({c: per_coin[c][v] for c in coins}, coins) for v in next(iter(per_coin.values()))}
    return {
        "schema": SCHEMA,
        "created_utc": datetime.now(UTC).isoformat(),
        "training": {"arm_U": "2016-01-01 (ETH 2016-05-18) .. 2020-12-31", "arm_E": "SOL-EUR 2023-10-01 .. 2025-08-31"},
        "unit_rule": "events of different coins within 24 h = one unit (anchored clusters)",
        "minimums": {"training_units": MIN_TRAIN_UNITS, "holdout_units": MIN_HOLDOUT_UNITS},
        "C": {"summary": c_summary, "verdict": verdict_c(c_summary)},
        "D": {"q20": q20, "variants": variants, "verdict": verdict_d(variants)},
    }


def run(usd_dir: Path, bars_root: Path, out: Path) -> int:
    coins = {c: coin_bars("U", read_usd_training(usd_dir / f)) for c, f in USD_FILES.items()}
    coins["SOL-EUR"] = coin_bars("E", read_sol_training(bars_root))
    result = build_counts(coins)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"C": result["C"]["verdict"], "D": result["D"]["verdict"]}, indent=2))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", maxsplit=1)[0] if __doc__ else None)
    ap.add_argument("--usd-dir", type=Path, default=Path("data/external/prices"))
    ap.add_argument("--bars-root", type=Path, default=Path("data/bars/1m"))
    ap.add_argument("--out", type=Path, default=Path("data/reports/count_signals_cd/counts.json"))
    args = ap.parse_args(argv)
    return run(args.usd_dir, args.bars_root, args.out)


if __name__ == "__main__":
    sys.exit(main())
