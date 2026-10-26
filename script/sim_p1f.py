"""SIM-P1f: calibration of the PSI-v2 rule of the feature drift (conflict P1f, docs/decisions/2026-10-25-p1f-psi-v2.md).

::

    python -m script.sim_p1f run --config config/config.yml [--train-config config/train.yml] \\
        [--champion <version>] [--out-root data/reports/sim_p1f]
    python -m script.sim_p1f s6 --run-dir data/reports/sim_p1f/<version> [--end YYYY-MM-DD] [--config ...]

Folder of a run: ``<out-root>/<champion_version>/``. Three files, each one written with exclusive open (never
rewritten):

* ``00_preregistration.json``: the rule (``drift.RULE``), the label_config of the champion, the products, the git
  commit and the sha256 of the decision document. Written BEFORE any bar is read (a run interrupted before the
  calibration may rewrite it; once ``10_`` exists the run is refused, exit 2);
* ``10_calibration.json``: S0-S5, the checks D0-D3 of every window tried, the decision (``adopted`` /
  ``rejected`` / ``diagnosis_wrong``) and, when adopted, the frozen reference that L0 reads
  (``drift.load_calibration``). Exit code 0 if adopted, 1 otherwise;
* ``20_s6.json``: only from the ``s6`` sub-command (the rule on the last days of the dry run; it never enters the
  decision).

The decision rule was written before the results (the decision document). After seeing them nothing is tuned: a result
that is not ``adopted`` is documented and opens a new conflict. Deterministic: no network, no seed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from modules.dryrun import drift
from modules.dryrun.drift import (
    CALIBRATION_FILE,
    CALIBRATION_SCHEMA,
    PREREG_FILE,
    PREREG_SCHEMA,
    S6_FILE,
    S6_SCHEMA,
    Calibration,
    CalibrationError,
    FeatureRef,
)
from modules.dryrun.learner import population_stability_index
from modules.features.serializer import FLAT_COLUMNS
from modules.features.state_matrix import flat_matrix
from modules.risk.label_config import LoadedLabelConfig, load_label_config

DECISION_DOC = "docs/decisions/2026-10-25-p1f-psi-v2.md"
CODE_FILES = ("script/sim_p1f.py", "modules/dryrun/drift.py")
REPO_ROOT = Path(__file__).resolve().parents[1]
OLD_HISTORY_DAYS = 90  # the bounds of the old code: [day + 1 - 90 d, day), 89 days (A6 of the decision)
OLD_ALERT = 0.10
S0_STEP_DAYS = 7  # S0, windows against R: non overlapping windows (the old function is slow on 50k values)
EXIT_REFUSED = 2
_COL = {name: j for j, name in enumerate(FLAT_COLUMNS)}
D = np.timedelta64(1, "D")
HOUR_NS = np.timedelta64(1, "h")

MarketsLoader = Callable[[LoadedLabelConfig, Sequence[str]], Mapping[str, Any]]
Verdict = tuple[bool, bool, bool]


# ------------------------------------------------------------------------------------------------- inputs


@dataclass(frozen=True)
class Champion:
    version: str
    training_products: tuple[str, ...]
    sentiment_available: bool
    label: LoadedLabelConfig
    holdout_start: datetime


def load_champion(model_dir: Path, version: str) -> Champion:
    """Version, training products, ``sentiment_available``, label_config and holdout start of a registry version."""
    card = json.loads((model_dir / "model_card.json").read_text(encoding="utf-8"))
    label = load_label_config(model_dir / "label_config.json")
    raw = json.loads((model_dir / "label_config.json").read_text(encoding="utf-8"))
    holdout = datetime.fromisoformat(str(raw["selection"]["holdout_start"]).replace("Z", "+00:00"))
    return Champion(
        version=version, training_products=tuple(card["data"]["products"]),
        sentiment_available=bool(card.get("sentiment_available", False)), label=label,
        holdout_start=holdout.astimezone(UTC),
    )  # fmt: skip


def _iso(t: datetime | np.datetime64 | pd.Timestamp) -> str:
    stamp = pd.Timestamp(t)
    stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean(obj: Any) -> Any:
    """JSON-safe copy: NaN/inf become null, numpy becomes Python."""
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_clean(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return _clean(obj.tolist())
    if isinstance(obj, np.generic):
        obj = obj.item()
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if isinstance(obj, datetime | pd.Timestamp):
        return _iso(obj)
    if isinstance(obj, Path):
        return str(obj)
    return obj


def _write_new(path: Path, doc: Mapping[str, Any]) -> None:
    """Exclusive open: a file of the run is written once (``FileExistsError`` otherwise)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(_clean(doc), sort_keys=True, separators=(",", ":"), allow_nan=False)
    with open(path, "x", encoding="utf-8", newline="\n") as fh:
        fh.write(text + "\n")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ------------------------------------------------------------------------------------------------ states


@dataclass
class Hourly:
    """All the hourly states (``ts_close.minute == 0``) of a product through the batch path."""

    ts: np.ndarray  # datetime64[ns], naive UTC, the close instant
    x: np.ndarray  # (n, len(FLAT_COLUMNS))
    btc_missing: np.ndarray  # bool: the alt state has no BTC bar at its instant


def hourly_of(pm: Any, label: LoadedLabelConfig) -> Hourly:
    rows = np.flatnonzero(pd.DatetimeIndex(pm.bars.ts_close).minute == 0)
    btc_rows = None if pm.btc_rows is None else pm.btc_rows[rows]
    x = flat_matrix(pm.ind, rows, pm.name, label, pm.btc_ind, btc_rows)
    missing = np.zeros(len(rows), dtype=bool) if btc_rows is None else np.asarray(btc_rows < 0)
    return Hourly(np.asarray(pm.bars.ts_close)[rows], x, missing)


def _ceil_day(t: np.datetime64) -> np.datetime64:
    d = t.astype("datetime64[D]")
    ceiled: np.datetime64 = (d if d.astype("datetime64[ns]") == t else d + D).astype("datetime64[ns]")
    return ceiled


def _floor_day(t: np.datetime64) -> np.datetime64:
    floored: np.datetime64 = t.astype("datetime64[D]").astype("datetime64[ns]")
    return floored


def _naive(t: datetime) -> np.datetime64:
    return np.datetime64(t.astimezone(UTC).replace(tzinfo=None), "ns")


# ------------------------------------------------------------------------------------------- decision


def decide(d0: bool, results: Mapping[int, Verdict]) -> tuple[str, int | None]:
    """The pre-registered decision. ``d0``: the diagnosis was reproduced; ``results``: window -> (D1, D2, D3). D0 false:
    ``diagnosis_wrong``. Window 7 with D1, D2 and D3: ``adopted`` 7; else window 14 with the same checks: ``adopted``
    14; else ``rejected``."""
    if not d0:
        return "diagnosis_wrong", None
    for window in drift.WINDOW_DAYS:
        verdict = results.get(window)
        if verdict is not None and all(verdict):
            return "adopted", window
    return "rejected", None


# ------------------------------------------------------------------------------------------- the study


@dataclass
class Windows:
    """The windows of ``W`` days of the training products: one per (product, UTC midnight)."""

    days: int
    product: list[str]
    start: np.ndarray  # int64: days since the epoch of the midnight the window starts at
    lo: np.ndarray  # first hourly row
    hi: np.ndarray  # one past the last hourly row


class Study:
    """The reference ``R``, its bins, and everything that depends on the windows. ``candidates``: the features the gate
    looks at (the 42 of ``candidate_features`` in a real run)."""

    def __init__(
        self, hourly: Mapping[str, Hourly], champion: Champion, served: Sequence[str], candidates: Sequence[str]
    ) -> None:
        self.hourly, self.champion, self.served = hourly, champion, tuple(served)
        self.training = tuple(p for p in champion.training_products if p in hourly)
        self.holdout = _naive(champion.holdout_start)
        self.candidates = tuple(candidates)
        self.t0: dict[str, np.datetime64] = {}
        for product in (*self.training, *(p for p in self.served if p not in self.training and p in hourly)):
            t0 = self._first_complete(product)
            if t0 is not None:
                self.t0[product] = t0
        self.training = tuple(p for p in self.training if p in self.t0)
        if not self.training:
            raise ValueError("no training product has a state with all the gate features finite")
        self.r_rows = {p: self._r_mask(p) for p in self.training}
        self.r_all = np.vstack([hourly[p].x[self.r_rows[p]] for p in self.training])
        self.edges: dict[str, np.ndarray] = {}
        self.excluded: dict[str, str] = {}
        for name in self.candidates:
            col = self.r_all[:, _COL[name]]
            edges = drift.inner_edges(col)
            if edges is None:
                self.excluded[name] = "fewer_than_3_distinct_edges"
            else:
                self.edges[name] = edges
        self.features = tuple(n for n in self.candidates if n in self.edges)
        self.shares = {n: drift.ref_shares(self.r_all[:, _COL[n]], self.edges[n]) for n in self.features}
        self.tau: dict[str, dict[str, float]] = {n: {} for n in self.features}
        for product in self.training:
            r_p = self.hourly[product].x[self.r_rows[product]]
            for n in self.features:
                if drift.applicable(n, product):
                    self.tau[n][product] = drift.integrated_tau(r_p[:, _COL[n]])
        self.n_eff = {
            n: float(np.mean([drift.TAU_MAX_LAG_H / t for t in taus.values()])) if taus else float("nan")
            for n, taus in self.tau.items()
        }  # n_eff_7d = 168 / tau, mean over the applicable training products
        # the first and last midnight of the period of R
        self.r_start = min(_ceil_day(self.t0[p]) for p in self.training)
        self.r_end = _floor_day(self.holdout)

    def _first_complete(self, product: str) -> np.datetime64 | None:
        h = self.hourly[product]
        cols = [_COL[n] for n in self.candidates if drift.applicable(n, product)]
        ok = np.isfinite(h.x[:, cols]).all(axis=1)
        return None if not ok.any() else h.ts[int(np.argmax(ok))]

    def _r_mask(self, product: str) -> np.ndarray:
        h = self.hourly[product]
        return np.asarray((h.ts >= self.t0[product]) & (h.ts < self.holdout))

    # -------------------------------------------------------------------------------------- windows

    def windows(self, days: int, products: Sequence[str] | None = None) -> Windows:
        chosen = self.training if products is None else tuple(products)
        names: list[str] = []
        start: list[np.ndarray] = []
        lo: list[np.ndarray] = []
        hi: list[np.ndarray] = []
        for product in chosen:
            if product not in self.t0:
                continue
            h = self.hourly[product]
            first = _ceil_day(self.t0[product]).astype("datetime64[D]")
            last = self.r_end.astype("datetime64[D]") - days * D
            if last < first:
                continue
            d = np.arange(first, last + D, D)
            names += [product] * len(d)
            start.append(d.astype(np.int64))
            lo.append(np.searchsorted(h.ts, d.astype("datetime64[ns]"), side="left"))
            hi.append(np.searchsorted(h.ts, (d + days * D).astype("datetime64[ns]"), side="left"))
        if not names:
            raise ValueError(f"no window of {days} days fits in the period of the reference")
        return Windows(days, names, np.concatenate(start), np.concatenate(lo), np.concatenate(hi))

    def x_of(self, w: Windows, i: int) -> np.ndarray:
        return self.hourly[w.product[i]].x[w.lo[i] : w.hi[i]]

    def psi_matrix(self, w: Windows) -> np.ndarray:
        """PSI of every window and every feature (NaN: unmeasured or not applicable)."""
        n_expected = 24 * w.days
        out = np.full((len(w.product), len(self.features)), np.nan)
        for i in range(len(w.product)):
            x = self.x_of(w, i)
            for j, name in enumerate(self.features):
                if not drift.applicable(name, w.product[i]):
                    continue
                psi = drift.psi_v2(x[:, _COL[name]], self.edges[name], self.shares[name], n_expected)
                if psi is not None:
                    out[i, j] = psi
        return out

    # ----------------------------------------------------------------------------------- the nulls

    def nulls(self, psi: np.ndarray, rows: np.ndarray) -> list[np.ndarray]:
        sub = psi[rows]
        return [np.sort(sub[np.isfinite(sub[:, j]), j]) for j in range(sub.shape[1])]

    def p_matrix(self, psi: np.ndarray, nulls: list[np.ndarray]) -> np.ndarray:
        out = np.full(psi.shape, np.nan)
        for j, null in enumerate(nulls):
            ok = np.isfinite(psi[:, j])
            if ok.any() and null.size:
                out[ok, j] = drift.empirical_p(null, psi[ok, j])
        return out

    def calibration(
        self, w: Windows, nulls: list[np.ndarray], c: float, x_f: Sequence[float], decision: str = "adopted"
    ) -> Calibration:
        """A ``Calibration`` object for the window (the real one when adopted, a provisional one for S3/S5)."""
        feats = {}
        for j, name in enumerate(self.features):
            col = self.r_all[:, _COL[name]]
            finite = col[np.isfinite(col)]
            feats[name] = FeatureRef(
                name, self.edges[name], self.shares[name], float(finite.min()), float(finite.max()),
                float(finite.std()), nulls[j], float(x_f[j]), self.n_eff[name],
            )  # fmt: skip
        return Calibration(
            self.champion.version, self.champion.label.sha256, w.days, drift.ALPHA, c, feats, decision, "", "",
            Path("."),
        )  # fmt: skip

    @staticmethod
    def min_p(p: np.ndarray) -> np.ndarray:
        out = np.full(p.shape[0], np.nan)
        ok = np.isfinite(p).any(axis=1)
        if ok.any():
            out[ok] = np.nanmin(p[ok], axis=1)
        return out


# ---------------------------------------------------------------------------------------------- S0


def s0_old_rule(
    markets: Mapping[str, Any], study: Study, hourly: Mapping[str, Hourly], label: LoadedLabelConfig
) -> dict[str, Any]:
    """The old function (``population_stability_index``: 10 deciles, floor 1e-4, products pooled, the states of the
    day at the minute against the hourly states of ``[day + 1 - 90 d, day)``, on ALL the columns of FLAT) on every
    day of the period of R; then the same function on windows of 7 hourly days against R."""
    products = [p for p in study.served if p in markets]
    first = study.r_start.astype("datetime64[D]")
    last = study.r_end.astype("datetime64[D]")
    days = np.arange(first, last, D)
    by_day: list[dict[str, float | None]] = []
    for d in days:
        lo, hi = d.astype("datetime64[ns]"), (d + D).astype("datetime64[ns]")
        hist_lo = (d + D - OLD_HISTORY_DAYS * D).astype("datetime64[ns]")
        live_parts: list[np.ndarray] = []
        hist_parts: list[np.ndarray] = []
        for p in products:
            pm = markets[p]
            ts = np.asarray(pm.bars.ts_close)
            a, b = np.searchsorted(ts, lo, "left"), np.searchsorted(ts, hi, "left")
            if b <= a:
                continue
            h = hourly[p]
            ha, hb = np.searchsorted(h.ts, hist_lo, "left"), np.searchsorted(h.ts, lo, "left")
            if hb <= ha:
                continue  # like the old code: a product with no history that day contributes to neither side
            rows = np.arange(a, b)
            btc_rows = None if pm.btc_rows is None else pm.btc_rows[rows]
            live_parts.append(flat_matrix(pm.ind, rows, p, label, pm.btc_ind, btc_rows))
            hist_parts.append(h.x[ha:hb])
        if not live_parts:
            continue
        live, hist = np.vstack(live_parts), np.vstack(hist_parts)
        by_day.append({n: population_stability_index(live[:, j], hist[:, j]) for j, n in enumerate(FLAT_COLUMNS)})

    def summary(rows: Sequence[Mapping[str, float | None]]) -> dict[str, Any]:
        per: dict[str, Any] = {}
        for name in FLAT_COLUMNS:
            v = np.array([r[name] for r in rows if r[name] is not None], dtype=np.float64)
            per[name] = (
                {"n": 0, "median": None, "p90": None, "p99": None} if v.size == 0 else
                {"n": int(v.size), "median": float(np.median(v)), "p90": float(np.quantile(v, 0.9)),
                 "p99": float(np.quantile(v, 0.99))}
            )  # fmt: skip
        measured = [r for r in rows if any(x is not None for x in r.values())]
        above = sum(1 for r in measured if any(x is not None and x > OLD_ALERT for x in r.values()))
        return {
            "per_feature": per, "windows": len(rows), "measured": len(measured), "with_alarm": above,
            "share_with_alarm": None if not measured else above / len(measured),
        }  # fmt: skip

    one_day = summary(by_day)
    # the same function on windows of 7 days of hourly states against R (all the columns)
    wins = study.windows(7)
    keep = np.flatnonzero((wins.start - wins.start.min()) % S0_STEP_DAYS == 0)
    seven: list[dict[str, float | None]] = []
    for i in keep:
        x = study.x_of(wins, int(i))
        seven.append({n: population_stability_index(x[:, j], study.r_all[:, j]) for j, n in enumerate(FLAT_COLUMNS)})
    slow = {}
    below = []
    for name in drift.D0_SLOW:
        median = one_day["per_feature"][name]["median"]
        slow[name] = median
        if median is not None and median < drift.D0_MEDIAN_MIN:
            below.append(name)
    return {
        "old_function": {"bins": 10, "floor": 1e-4, "history_days": OLD_HISTORY_DAYS - 1, "alert": OLD_ALERT},
        "one_day": one_day, "seven_days_vs_r": {**summary(seven), "step_days": S0_STEP_DAYS},
        "d0": {
            "slow_medians": slow, "slow_below_min": below, "count": len(below), "min_wrong": drift.D0_MIN_WRONG,
            "passed": len(below) < drift.D0_MIN_WRONG,
        },
    }  # fmt: skip


# -------------------------------------------------------------------------------------- S1-S5, D1-D3


def _blocks(start: np.datetime64, end: np.datetime64) -> list[tuple[int, int]]:
    s = int(start.astype("datetime64[D]").astype(np.int64))
    e = int(end.astype("datetime64[D]").astype(np.int64))
    out = []
    while s < e:
        out.append((s, min(s + drift.OOS_BLOCK_DAYS, e)))
        s += drift.OOS_BLOCK_DAYS
    return out


def evaluate_window(study: Study, days: int) -> dict[str, Any]:
    """S2 (null, ``c``, ``x_f``), S3 (out of sample), S4 (power), S5 (products outside the training) and the checks
    D1-D3 for windows of ``days`` days. The frozen reference is returned under ``"_reference"``."""
    w = study.windows(days)
    n_w = len(w.product)
    psi = study.psi_matrix(w)
    everything = np.arange(n_w)
    nulls = study.nulls(psi, everything)
    p_full = study.p_matrix(psi, nulls)
    s_full = study.min_p(p_full)
    s_ok = s_full[np.isfinite(s_full)]
    if s_ok.size == 0:
        raise ValueError("no window with a measured feature")
    c = drift.minp_threshold(s_ok)
    x_f = [drift.x_threshold(null, c) for null in nulls]
    judged = [n for n in study.features if study.n_eff[n] >= drift.NEFF_JUDGED]
    j_of = {n: j for j, n in enumerate(study.features)}

    # D2: x_f <= 0.25 for the features with n_eff_7d >= 50 (unreachable = failed)
    d2_rows = {
        n: {"x_f": x_f[j_of[n]], "n_eff_7d": study.n_eff[n], "ok": x_f[j_of[n]] <= drift.D2_MAX_X} for n in judged
    }
    d2 = all(r["ok"] for r in d2_rows.values())

    # S3: out of sample, blocks of 182 days
    day_of = w.start
    blocks = _blocks(study.r_start, study.r_end)
    evaluated = Counter[str]()
    alarmed = Counter[str]()
    block_rows: list[dict[str, Any]] = []
    power_hits = {n: {delta: 0 for delta in drift.DELTAS} for n in study.features}
    power_n = {n: 0 for n in study.features}
    sigma = {n: float(np.std(study.r_all[:, _COL[n]][np.isfinite(study.r_all[:, _COL[n]])])) for n in study.features}
    for s, e in blocks:
        in_block = (day_of >= s) & (day_of < e)
        guard = drift.OOS_GUARD_DAYS
        touches = (day_of < e + guard) & (day_of + days > s - guard)
        null_rows, ev_rows = np.flatnonzero(~touches), np.flatnonzero(in_block)
        info: dict[str, Any] = {"start": str(np.datetime64(s, "D")), "end": str(np.datetime64(e, "D")),
                                "windows": int(ev_rows.size), "null_windows": int(null_rows.size)}  # fmt: skip
        if null_rows.size == 0 or ev_rows.size == 0:
            info["skipped"] = "no_null_or_no_windows"
            block_rows.append(info)
            continue
        nulls_b = study.nulls(psi, null_rows)
        s_null = study.min_p(study.p_matrix(psi[null_rows], nulls_b))
        s_null = s_null[np.isfinite(s_null)]
        if s_null.size == 0:
            info["skipped"] = "null_without_measured_features"
            block_rows.append(info)
            continue
        c_b = drift.minp_threshold(s_null)
        p_ev = study.p_matrix(psi[ev_rows], nulls_b)
        s_ev = study.min_p(p_ev)
        with np.errstate(invalid="ignore"):
            alarm = s_ev < c_b
        cal_b = study.calibration(w, nulls_b, c_b, [drift.x_threshold(n, c_b) for n in nulls_b])
        for k, i in enumerate(ev_rows):
            res = drift.window_stat(study.x_of(w, int(i)), cal_b, w.product[i])
            if res.alarm != bool(alarm[k]) or (res.S is None) != (not np.isfinite(s_ev[k])):
                raise RuntimeError("window_stat and the matrix path disagree: a defect, not a result")
            evaluated[w.product[i]] += 1
            alarmed[w.product[i]] += int(alarm[k])
        info.update({"c": c_b, "alarms": int(alarm.sum())})
        block_rows.append(info)
        # S4: add delta * sigma to ONE feature at a time and apply the rule again
        n_exp = 24 * days
        for k, i in enumerate(ev_rows):
            row = p_ev[k]
            fin = np.flatnonzero(np.isfinite(row))
            if fin.size == 0:
                continue
            order = fin[np.argsort(row[fin], kind="stable")]
            first, second = order[0], (order[1] if order.size > 1 else None)
            x = study.x_of(w, int(i))
            for j in fin:
                name = study.features[j]
                other = row[second] if (j == first and second is not None) else (np.inf if j == first else row[first])
                power_n[name] += 1
                col = x[:, _COL[name]]
                for delta in drift.DELTAS:
                    shifted = col + delta * sigma[name]
                    p_new = drift.psi_v2(shifted, study.edges[name], study.shares[name], n_exp)
                    if p_new is None:
                        continue
                    s_new = min(float(drift.empirical_p(nulls_b[j], p_new)), float(other))
                    power_hits[name][delta] += int(s_new < c_b)
    fwer = {p: (alarmed[p] / evaluated[p] if evaluated[p] else None) for p in dict.fromkeys(w.product)}
    d1_ok = bool(evaluated) and all(v is not None and v <= drift.D1_MAX_FWER for v in fwer.values())
    power = {
        n: {
            "windows": power_n[n],
            **{f"delta_{d}": (power_hits[n][d] / power_n[n] if power_n[n] else None) for d in drift.DELTAS},
        }
        for n in study.features
    }
    d3_rows = {n: {"power_delta_1": power[n]["delta_1.0"], "ok": (power[n]["delta_1.0"] or 0.0) >= drift.D3_MIN_POWER}
               for n in judged}  # fmt: skip
    d3 = all(r["ok"] for r in d3_rows.values())
    # S5: the served products outside the training, windows of the period of R against R
    cal = study.calibration(w, nulls, c, x_f)
    outside: dict[str, Any] = {}
    for product in (p for p in study.served if p not in study.training and p in study.t0):
        try:
            wo = study.windows(days, [product])
        except ValueError:
            outside[product] = {"windows": 0}
            continue
        flagged = Counter[str]()
        hits = 0
        for n_win in range(len(wo.product)):
            res = drift.window_stat(study.x_of(wo, n_win), cal, product)
            hits += int(res.alarm)
            flagged.update(n for n, r in res.features.items() if r.flag)
        outside[product] = {"windows": len(wo.product), "alarms": hits, "rate": hits / len(wo.product),
                            "flagged_features": dict(flagged.most_common())}  # fmt: skip
    nulls_len = {n: int(nulls[j].size) for j, n in enumerate(study.features)}
    return {
        "window_days": days, "windows": n_w, "c": c, "alpha": drift.ALPHA, "null_sizes": nulls_len,
        "x_f": {n: x_f[j] for j, n in enumerate(study.features)},
        "d1": {"fwer_oos": fwer, "days_in_alarm_per_30": {p: None if v is None else 30 * v for p, v in fwer.items()},
               "max": max((v for v in fwer.values() if v is not None), default=None), "ok": d1_ok},
        "d2": {"judged": d2_rows, "ok": d2}, "d3": {"judged": d3_rows, "ok": d3},
        "s3_blocks": block_rows, "s4_power": power, "s5_outside_training": outside,
        "passed": d1_ok and d2 and d3,
        "_reference": {"nulls": nulls, "c": c, "x_f": x_f},
    }  # fmt: skip


def reference_doc(study: Study, days: int, ref: Mapping[str, Any]) -> dict[str, Any]:
    feats: dict[str, Any] = {}
    for j, name in enumerate(study.features):
        col = study.r_all[:, _COL[name]]
        finite = col[np.isfinite(col)]
        x_f = ref["x_f"][j]
        feats[name] = {
            "inner": study.edges[name], "p": study.shares[name], "r_min": float(finite.min()),
            "r_max": float(finite.max()), "sigma": float(finite.std()), "null_sorted": ref["nulls"][j],
            "x_f": x_f, "x_f_unreachable": not math.isfinite(x_f), "n_eff_7d": study.n_eff[name],
            "tau": study.tau[name],
        }  # fmt: skip
    return {"window_days": days, "features": feats}


def s1_doc(study: Study) -> dict[str, Any]:
    return {
        "r_products": {
            p: {"t0": _iso(study.t0[p]), "states": int(study.r_rows[p].sum())} for p in study.training
        },
        "r_states": int(study.r_all.shape[0]), "r_start": _iso(study.r_start), "r_end": _iso(study.r_end),
        "features": list(study.features), "excluded": study.excluded,
        "n_eff_7d": study.n_eff, "tau": study.tau,
        "judged": [n for n in study.features if study.n_eff[n] >= drift.NEFF_JUDGED],
        "btc_missing_rows": {
            p: int(study.hourly[p].btc_missing[study.r_rows[p]].sum()) for p in study.training
        },
    }  # fmt: skip


def calibrate(
    markets: Mapping[str, Any], champion: Champion, served: Sequence[str], *, candidates: Sequence[str] | None = None
) -> dict[str, Any]:
    """S0-S5 and the decision on already loaded markets. Returns the body of ``10_calibration.json``
    (without the fields the caller adds: schema, created_at, preregistration sha256)."""
    label = champion.label
    cands = tuple(drift.candidate_features(champion.sentiment_available) if candidates is None else candidates)
    hourly = {p: hourly_of(markets[p], label) for p in markets}
    study = Study(hourly, champion, served, cands)
    s0 = s0_old_rule(markets, study, hourly, label)
    body: dict[str, Any] = {
        "rule": drift.rule_json(), "champion_version": champion.version, "label_config_sha256": label.sha256,
        "training_products": list(study.training), "served_products": list(served),
        "holdout_start": _iso(champion.holdout_start), "alpha": drift.ALPHA, "s0": s0,
    }  # fmt: skip
    results: dict[int, Verdict] = {}
    evaluations: dict[int, dict[str, Any]] = {}
    body["s1"] = s1_doc(study)
    if s0["d0"]["passed"]:
        for days in drift.WINDOW_DAYS:
            ev = evaluate_window(study, days)
            evaluations[days] = ev
            results[days] = (ev["d1"]["ok"], ev["d2"]["ok"], ev["d3"]["ok"])
            if all(results[days]):
                break  # the repetition at 14 days exists only when 7 failed
    decision, window = decide(bool(s0["d0"]["passed"]), results)
    body["windows"] = {str(d): {k: v for k, v in ev.items() if k != "_reference"} for d, ev in evaluations.items()}
    body["decision"], body["window_days"] = decision, window
    body["c"] = None
    body["reference"] = None
    if decision == "adopted" and window is not None:
        ev = evaluations[window]
        body["c"] = ev["c"]
        body["reference"] = reference_doc(study, window, ev["_reference"])
    elif decision == "rejected" and 7 in evaluations:
        body["reference_diagnostic"] = reference_doc(study, 7, evaluations[7]["_reference"])
        body["c_diagnostic"] = evaluations[7]["c"]
    return body


# -------------------------------------------------------------------------------------------- the run


def _git_state(doc: Path) -> tuple[str | None, bool | None]:
    """The commit of HEAD and whether the decision document is committed and unmodified (None: no git)."""
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=True, timeout=30
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", str(doc)], cwd=REPO_ROOT, capture_output=True, text=True,
            check=True, timeout=30,
        ).stdout.strip()  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None, None
    return head, status == ""


def run(
    champion: Champion, served: Sequence[str], loader: MarketsLoader, out_root: Path, *,
    decision_doc: Path | None = None, now: Callable[[], datetime] = lambda: datetime.now(UTC),
    candidates: Sequence[str] | None = None, commit: tuple[str | None, bool | None] | None = None,
) -> int:  # fmt: skip
    """One calibration run. Exit code: 0 adopted, 1 rejected or diagnosis_wrong, 2 refused."""
    folder = out_root / champion.version
    if (folder / CALIBRATION_FILE).exists():
        print(f"a calibration already exists for {champion.version}: {folder / CALIBRATION_FILE}", file=sys.stderr)
        return EXIT_REFUSED
    doc_path = decision_doc or REPO_ROOT / DECISION_DOC
    if not doc_path.is_file():
        print(f"the decision document is missing: {doc_path}", file=sys.stderr)
        return EXIT_REFUSED
    git_commit, doc_clean = commit if commit is not None else _git_state(doc_path)
    prereg = {
        "schema": PREREG_SCHEMA, "rule": drift.rule_json(), "champion_version": champion.version,
        "label_config_sha256": champion.label.sha256, "training_products": list(champion.training_products),
        "served_products": list(served), "holdout_start": _iso(champion.holdout_start), "git_commit": git_commit,
        "decision_doc": doc_path.name, "decision_doc_sha256": _sha256_file(doc_path),
        "decision_doc_committed": doc_clean,
        # the code that defines and runs the test (the working tree may be ahead of the commit): its hashes
        "code_sha256": {name: _sha256_file(REPO_ROOT / name) for name in CODE_FILES},
        "created_at": _iso(now()),
    }  # fmt: skip
    folder.mkdir(parents=True, exist_ok=True)
    prereg_path = folder / PREREG_FILE
    prereg_path.unlink(missing_ok=True)  # an interrupted run (no 10_): rewritten, still before any bar
    _write_new(prereg_path, prereg)
    products = sorted(set(served) | set(champion.training_products))
    markets = loader(champion.label, products)  # <- the first bar is read here, after the pre-registration
    try:
        body = calibrate(markets, champion, served, candidates=candidates)
    except ValueError as exc:
        print(f"SIM-P1f cannot run: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    doc = {
        "schema": CALIBRATION_SCHEMA, "created_at": _iso(now()), "preregistration_sha256": _sha256_file(prereg_path),
        **body,
    }  # fmt: skip
    _write_new(folder / CALIBRATION_FILE, doc)
    print(json.dumps({"decision": body["decision"], "window_days": body["window_days"], "c": body["c"]}))
    return 0 if body["decision"] == "adopted" else 1


def run_s6(
    run_dir: Path, served: Sequence[str], loader: MarketsLoader, label: LoadedLabelConfig, *,
    end: date | None = None, now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:  # fmt: skip
    """The rule on the last days of the dry run. Refused (2) without a calibration. Never enters the decision."""
    try:
        cal = drift.load_calibration(run_dir)
    except CalibrationError as exc:
        print(f"calibration not trustworthy: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    if cal is None:
        print(f"no {CALIBRATION_FILE} in {run_dir}: SIM-P1f was not run", file=sys.stderr)
        return EXIT_REFUSED
    target = run_dir / S6_FILE
    if target.exists():
        print(f"{S6_FILE} already exists in {run_dir}", file=sys.stderr)
        return EXIT_REFUSED
    diagnostic_only = cal.window_days is None
    raw_doc = json.loads((run_dir / CALIBRATION_FILE).read_text(encoding="utf-8"))
    if diagnostic_only:
        if raw_doc.get("reference_diagnostic") is None:
            print("the calibration was not adopted and holds no diagnostic reference", file=sys.stderr)
            return EXIT_REFUSED
        study_cal = _diagnostic_calibration(cal, raw_doc)
    else:
        study_cal = cal
    days = int(study_cal.window_days or 7)
    last = end or (now().astimezone(UTC).date() - timedelta(days=1))
    hi = datetime(last.year, last.month, last.day, tzinfo=UTC) + timedelta(days=1)
    lo = hi - timedelta(days=days)
    markets = loader(label, sorted(set(served) | set(raw_doc.get("training_products", []))))  # BTC: context of alts
    products: dict[str, Any] = {}
    for product in served:
        pm = markets.get(product)
        if pm is None:
            products[product] = {"status": "unmeasured", "reason": "no_market"}
            continue
        x, _ = drift.hourly_states(pm, label, lo, hi)
        if x.shape[0] < 24 * days:
            products[product] = {"status": "unmeasured", "reason": "incomplete_window", "states": int(x.shape[0])}
            continue
        res = drift.window_stat(x, study_cal, product)
        products[product] = {
            "status": "measured", "states": int(x.shape[0]), "S": res.S, "alarm": res.alarm,
            "flagged": [
                {"feature": n, "psi": r.psi, "x_f": r.x_f, "p": r.p} for n, r in res.features.items() if r.flag
            ],
            "features": {
                n: {"psi": r.psi, "p": r.p, "x_f": r.x_f, "n_finite": r.n_finite,
                    "out_of_range_share": r.out_of_range_share, "n_eff_7d": study_cal.features[n].n_eff_7d}
                for n, r in res.features.items()
            },
        }  # fmt: skip
    doc = {
        "schema": S6_SCHEMA, "created_at": _iso(now()), "calibration_sha256": cal.sha256,
        "champion_version": cal.champion_version, "decision": cal.decision, "diagnostic_only": diagnostic_only,
        "window_days": days, "window": {"start": _iso(lo), "end": _iso(hi)}, "c": study_cal.c, "products": products,
        "alarm": any(p.get("alarm") for p in products.values()),
    }  # fmt: skip
    _write_new(target, doc)
    print(json.dumps({"alarm": doc["alarm"], "products": {p: v.get("alarm") for p, v in products.items()}}))
    return 0


def _diagnostic_calibration(cal: Calibration, raw: Mapping[str, Any]) -> Calibration:
    ref = raw["reference_diagnostic"]
    feats = {n: drift.feature_ref(n, r) for n, r in ref["features"].items()}
    return Calibration(
        cal.champion_version, cal.label_config_sha256, int(ref["window_days"]), cal.alpha, float(raw["c_diagnostic"]),
        feats, cal.decision, cal.preregistration_sha256, cal.sha256, cal.run_dir,
    )  # fmt: skip


# ------------------------------------------------------------------------------------------------- CLI


def _context(args: argparse.Namespace) -> tuple[Champion, list[str], MarketsLoader, Path]:
    from modules.config import load_config
    from modules.dryrun.registry import FilesystemModelRegistry
    from modules.market.backfill import bars_root
    from training.tune_policy import load_markets

    app = load_config(args.config)
    registry = FilesystemModelRegistry(Path(app.paths.models_dir) / "registry")
    model = registry.get_version(args.champion) if args.champion else registry.get("champion")
    champion = load_champion(Path(model.path), str(model.version))
    served = [p for p in app.market.products if p in champion.label.spread_bps]
    root = bars_root(app)

    def loader(label: LoadedLabelConfig, products: Sequence[str]) -> Mapping[str, Any]:
        return load_markets(root, [p for p in products if p in label.spread_bps], label)

    out_root = Path(args.out_root) if args.out_root else Path(app.paths.data_dir) / "reports" / "sim_p1f"
    return champion, served, loader, out_root


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m script.sim_p1f", description="SIM-P1f: calibration of PSI-v2")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "s6"):
        p = sub.add_parser(name)
        p.add_argument("--config", default="config/config.yml")
        p.add_argument("--champion", default=None, help="registry version (default: the champion alias)")
        p.add_argument("--out-root", default=None, help="default: <data_dir>/reports/sim_p1f")
        if name == "run":
            p.add_argument("--train-config", default="config/train.yml", help="accepted for symmetry; not read")
        else:
            p.add_argument("--run-dir", default=None, help="default: <out-root>/<champion version>")
            p.add_argument("--end", default=None, help="last UTC day of the window (default: yesterday)")
    args = ap.parse_args(argv)
    from modules.config import EXIT_CONFIG_ERROR, ConfigError
    from modules.dryrun.registry import RegistryError

    try:
        champion, served, loader, out_root = _context(args)
        end = date.fromisoformat(args.end) if getattr(args, "end", None) else None
    except (ConfigError, RegistryError, OSError, ValueError, KeyError) as exc:
        print(f"SIM-P1f configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    if args.cmd == "run":
        return run(champion, served, loader, out_root)
    run_dir = Path(args.run_dir) if args.run_dir else out_root / champion.version
    return run_s6(run_dir, served, loader, champion.label, end=end)


if __name__ == "__main__":
    raise SystemExit(main())
