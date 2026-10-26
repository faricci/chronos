"""PSI-v2: drift of the model inputs against the distribution the model in use was trained on (spec sez. 5.1.6 L0,
5.1.7 G-DRY-OPS ``feature_drift``; docs/decisions/2026-10-25-p1f-psi-v2.md).

Pure functions plus ``load_calibration``, the only I/O. Nothing here is configurable on purpose: the constants of
``RULE`` ARE the pre-registered definition of the test (written before any bar was read by SIM-P1f) and are frozen in
the artefact of the simulation (``script/sim_p1f.py``). Making them keys of the YAML would allow exactly the
after-the-result tuning the pre-registration forbids.

What it measures. H0: the live window of ``W`` days is a realisation of the same (persistent, not i.i.d.) process that
generated the training period, i.e. it is not more extreme, over the set of features, than any window of ``W`` days of
the training period. The statistic is the PSI of each feature against the training reference ``R`` (12 bins at the
quantiles of ``R``, Jeffreys smoothing instead of a floor); the threshold is a quantile of the EMPIRICAL null of the
same statistic (the windows of ``R`` itself), with a min-p (Westfall-Young) correction over the features. It is the
same quantity the old gate tried to measure with a threshold that rejected 100% of the time.

Limit that is written down instead of hidden: a slow feature (``n_eff_7d`` <= 2) has no more than one or two
independent observations in a week, so no test of one week can certify "no drift" on it; it can only say whether the
week is more extreme than the weeks of the training were. ``n_eff_7d`` is reported next to every feature.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import numpy as np
import pandas as pd

from modules.features.serializer import FLAT_COLUMNS

if TYPE_CHECKING:
    from modules.risk.label_config import LoadedLabelConfig
    from training.backtest import ProductMarket

__all__ = [
    "CALIBRATION_FILE",
    "CALIBRATION_SCHEMA",
    "Calibration",
    "CalibrationError",
    "FeatureRef",
    "FeatureResult",
    "PREREG_FILE",
    "PREREG_SCHEMA",
    "RULE",
    "S6_FILE",
    "S6_SCHEMA",
    "WindowResult",
    "applicable",
    "bin_counts",
    "candidate_features",
    "empirical_p",
    "feature_ref",
    "hourly_states",
    "inner_edges",
    "integrated_tau",
    "load_calibration",
    "minp_threshold",
    "psi_v2",
    "ref_shares",
    "window_stat",
    "x_threshold",
]

QUANTILES: Final = (0.005, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.995)
MIN_INNER_EDGES: Final = 3
JEFFREYS: Final = 0.5
REF_FLOOR: Final = 0.5  # p_i >= REF_FLOOR / m
MIN_FINITE_FRAC: Final = 0.9
ALPHA: Final = 0.01
WINDOW_DAYS: Final = (7, 14)  # the second one only as the single repetition
STEP_DAYS: Final = 1
TAU_MAX_LAG_H: Final = 168
TAU_STOP: Final = 0.05
NEFF_JUDGED: Final = 50.0
D1_MAX_FWER: Final = 0.02
D2_MAX_X: Final = 0.25
D3_MIN_POWER: Final = 0.80
DELTAS: Final = (0.5, 1.0)
OOS_BLOCK_DAYS: Final = 182
OOS_GUARD_DAYS: Final = 7
D0_SLOW: Final = (
    "ret_3d_bps", "ret_7d_bps", "macd_hist_1d_bps", "rsi_14_1d", "atr_14_6h_bps", "atr_14_1d_bps",
    "atr_1d_pctile_365d", "hh_ll_pos_30d", "rv_ratio_1d_30d", "btc_rv_ratio_1d_30d", "p_breakeven",
)  # fmt: skip
D0_MEDIAN_MIN: Final = 1.0
D0_MIN_WRONG: Final = 6  # half of the 11 slow features, rounded up

RULE: Final[dict[str, Any]] = {
    "id": "PSI_V2",
    "QUANTILES": list(QUANTILES), "MIN_INNER_EDGES": MIN_INNER_EDGES, "JEFFREYS": JEFFREYS, "REF_FLOOR": REF_FLOOR,
    "MIN_FINITE_FRAC": MIN_FINITE_FRAC, "ALPHA": ALPHA, "WINDOW_DAYS": list(WINDOW_DAYS), "STEP_DAYS": STEP_DAYS,
    "TAU_MAX_LAG_H": TAU_MAX_LAG_H, "TAU_STOP": TAU_STOP, "NEFF_JUDGED": NEFF_JUDGED, "D1_MAX_FWER": D1_MAX_FWER,
    "D2_MAX_X": D2_MAX_X, "D3_MIN_POWER": D3_MIN_POWER, "DELTAS": list(DELTAS), "OOS_BLOCK_DAYS": OOS_BLOCK_DAYS,
    "OOS_GUARD_DAYS": OOS_GUARD_DAYS, "D0_SLOW": list(D0_SLOW), "D0_MEDIAN_MIN": D0_MEDIAN_MIN,
    "D0_MIN_WRONG": D0_MIN_WRONG,
}  # fmt: skip

PREREG_FILE: Final = "00_preregistration.json"
CALIBRATION_FILE: Final = "10_calibration.json"
S6_FILE: Final = "20_s6.json"
PREREG_SCHEMA: Final = "chronos.sim_p1f.prereg.v1"
CALIBRATION_SCHEMA: Final = "chronos.sim_p1f.calibration.v1"
S6_SCHEMA: Final = "chronos.sim_p1f.s6.v1"

_COL: Final = {name: j for j, name in enumerate(FLAT_COLUMNS)}


class CalibrationError(Exception):
    """The calibration artefact is not trustworthy (tampered pre-registration, another rule, another schema)."""


# --------------------------------------------------------------------------------------------- the features


def candidate_features(sentiment_available: bool) -> tuple[str, ...]:
    """The FLAT columns the gate can look at: not the calendar (deterministic: PSI is 0 on full weeks by
    construction), not ``pos_*`` (null on FLAT states), not ``cost_*`` (constants of the label_config), and not
    ``sent_*`` unless the model uses the sentiment. With ``sentiment_available`` false: 42 names."""
    skipped = ("clock_", "pos_", "cost_") + (() if sentiment_available else ("sent_",))
    return tuple(c for c in FLAT_COLUMNS if not c.startswith(skipped))


def applicable(feature: str, product: str) -> bool:
    """The BTC context is NaN by construction on a BTC product: not applicable there."""
    return not (feature.startswith("btc_") and product.startswith("BTC-"))


# ------------------------------------------------------------------------------------------------ the maths


def inner_edges(ref: np.ndarray) -> np.ndarray | None:
    """The inner bin edges: the quantiles ``QUANTILES`` of the finite values of ``ref`` (12 bins with the two open
    ends, two of them tails of 0.5%). ``None`` when fewer than ``MIN_INNER_EDGES`` distinct edges exist: the
    feature has no usable shape and is left out of the gate."""
    finite = ref[np.isfinite(ref)]
    if finite.size == 0:
        return None
    edges = np.unique(np.quantile(finite, QUANTILES))
    return None if edges.size < MIN_INNER_EDGES else np.asarray(edges, dtype=np.float64)


def bin_counts(x: np.ndarray, inner: np.ndarray) -> np.ndarray:
    """Counts of the finite values of ``x`` in the ``inner.size + 1`` bins. A value equal to an edge goes to the upper
    bin (``side="right"``), the same rule for the reference, the nulls and the live windows."""
    finite = x[np.isfinite(x)]
    return np.bincount(np.searchsorted(inner, finite, side="right"), minlength=inner.size + 1)


def ref_shares(ref: np.ndarray, inner: np.ndarray) -> np.ndarray:
    """Share of the reference in every bin, floored at ``REF_FLOOR / m`` (``m`` finite reference values)."""
    m = int(np.isfinite(ref).sum())
    return np.maximum(bin_counts(ref, inner) / m, REF_FLOOR / m)


def psi_v2(x: np.ndarray, inner: np.ndarray, p: np.ndarray, n_expected: int) -> float | None:
    """PSI of the window ``x`` against the reference shares ``p``: ``sum (q - p) ln(q / p)`` with the Jeffreys
    smoothing ``q_i = (c_i + 0.5) / (n + 0.5 B)``. ``None`` (unmeasured) when fewer than
    ``floor(MIN_FINITE_FRAC * n_expected)`` values are finite. The smoothing replaces the floor of 1e-4 of the old
    function, which fixed a ceiling of ~8.3 and dominated the value."""
    n = int(np.isfinite(x).sum())
    if n < math.floor(MIN_FINITE_FRAC * n_expected + 1e-9):
        return None
    counts = bin_counts(x, inner)
    q = (counts + JEFFREYS) / (n + JEFFREYS * counts.size)
    return float(np.sum((q - p) * np.log(q / p)))


def empirical_p(null_sorted: np.ndarray, value: Any) -> Any:
    """``(1 + #{null >= value}) / (1 + N)`` (scalar in, float out; array in, array out)."""
    n = null_sorted.size
    out = (1 + (n - np.searchsorted(null_sorted, value, side="left"))) / (1 + n)
    return float(out) if np.ndim(out) == 0 else out


def minp_threshold(s_null: np.ndarray, alpha: float = ALPHA) -> float:
    """``c``: the alpha-quantile (lower) of the min-p statistic over the null windows. An alarm is ``S < c``
    (strict), so ``P(S < c | null) <= alpha`` even with repeated values."""
    return float(np.quantile(s_null, alpha, method="lower"))


def x_threshold(null_sorted: np.ndarray, c: float) -> float:
    """The PSI of a feature above which its empirical p-value is below ``c``: the largest null value whose p-value is
    still ``>= c``. ``math.inf`` (JSON ``null``, ``unreachable``) when even a value above the whole null cannot
    reach ``c`` (``1 / (1 + N) >= c``). Only reported: the flag of a feature is always ``empirical_p < c``."""
    if 1.0 / (1 + null_sorted.size) >= c:
        return math.inf
    p = empirical_p(null_sorted, null_sorted)
    return float(null_sorted[p >= c].max())


def integrated_tau(series: np.ndarray, max_lag: int = TAU_MAX_LAG_H, stop: float = TAU_STOP) -> float:
    """Integrated autocorrelation time ``1 + 2 sum rho_k`` of an hourly series, ``k = 1..max_lag``, stopped at the
    first ``rho_k < stop`` (or NaN), which is not summed. ``168 / tau`` is the number of independent observations of a
    week of hourly values."""
    s = pd.Series(np.asarray(series, dtype=np.float64))
    total = 0.0
    with np.errstate(all="ignore"):
        for k in range(1, max_lag + 1):
            rho = s.autocorr(k)
            if not math.isfinite(rho) or rho < stop:
                break
            total += float(rho)
    return 1.0 + 2.0 * total


# --------------------------------------------------------------------------------------- calibration object


@dataclass(frozen=True)
class FeatureRef:
    """What is frozen for one feature: its bins, its reference shares, its range and scale in the training, the
    sorted null of its PSI over the null windows, the threshold ``x_f`` and its independent observations per week."""

    name: str
    inner: np.ndarray
    p: np.ndarray
    r_min: float
    r_max: float
    sigma: float
    null_sorted: np.ndarray
    x_f: float
    n_eff_7d: float


@dataclass(frozen=True)
class Calibration:
    champion_version: str
    label_config_sha256: str
    window_days: int | None
    alpha: float
    c: float | None
    features: Mapping[str, FeatureRef]
    decision: str
    preregistration_sha256: str
    sha256: str
    run_dir: Path


@dataclass(frozen=True)
class FeatureResult:
    name: str
    psi: float | None  # None: unmeasured (fewer than 90% finite values)
    p: float | None
    x_f: float
    n_finite: int
    out_of_range_share: float | None
    flag: bool


@dataclass(frozen=True)
class WindowResult:
    features: Mapping[str, FeatureResult]
    S: float | None  # min p over the measured features
    alarm: bool


def window_stat(x_window: np.ndarray, cal: Calibration, product: str) -> WindowResult:
    """The rule on one window: ``x_window`` is the FLAT matrix (``FLAT_COLUMNS`` order) of the hourly states of the
    window of ``cal.window_days`` days of ``product``. Used by L0 every night and by SIM-P1f (S3, S6): ONE function."""
    if cal.window_days is None or cal.c is None:
        raise ValueError("the calibration has no adopted window")
    n_expected = 24 * cal.window_days
    out: dict[str, FeatureResult] = {}
    for name, ref in cal.features.items():
        if not applicable(name, product):
            continue
        col = x_window[:, _COL[name]]
        finite = col[np.isfinite(col)]
        psi = psi_v2(col, ref.inner, ref.p, n_expected)
        if psi is None:
            out[name] = FeatureResult(name, None, None, ref.x_f, int(finite.size), None, False)
            continue
        p = float(empirical_p(ref.null_sorted, psi))
        outside = float(np.mean((finite < ref.r_min) | (finite > ref.r_max)))
        out[name] = FeatureResult(name, psi, p, ref.x_f, int(finite.size), outside, p < cal.c)
    measured = [r.p for r in out.values() if r.p is not None]
    s = min(measured) if measured else None
    return WindowResult(out, s, s is not None and s < cal.c)


def hourly_states(
    pm: ProductMarket, label: LoadedLabelConfig, lo: datetime, hi: datetime
) -> tuple[np.ndarray, np.ndarray]:
    """FLAT matrix and close instants of the hourly states (``ts_close.minute == 0``) with ``lo <= ts_close < hi``
    (naive UTC datetimes or tz-aware ones), from the bars of ``pm`` through the batch path ``flat_matrix``."""
    from modules.features.state_matrix import flat_matrix

    ts = pm.bars.ts_close
    lo64 = np.datetime64(lo.replace(tzinfo=None), "ns")
    hi64 = np.datetime64(hi.replace(tzinfo=None), "ns")
    rows = np.flatnonzero((ts >= lo64) & (ts < hi64) & (pd.DatetimeIndex(ts).minute == 0))
    btc_rows = None if pm.btc_rows is None else pm.btc_rows[rows]
    return flat_matrix(pm.ind, rows, pm.name, label, pm.btc_ind, btc_rows), ts[rows]


# --------------------------------------------------------------------------------------------- the artefact


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rule_json() -> dict[str, Any]:
    """``RULE`` as it reads back from a JSON file."""
    loaded: dict[str, Any] = json.loads(json.dumps(RULE))
    return loaded


def feature_ref(name: str, raw: Mapping[str, Any]) -> FeatureRef:
    """A ``FeatureRef`` from its JSON form (``x_f`` null = unreachable = infinity)."""
    x_f = raw.get("x_f")
    return FeatureRef(
        name=name, inner=np.asarray(raw["inner"], dtype=np.float64), p=np.asarray(raw["p"], dtype=np.float64),
        r_min=float(raw["r_min"]), r_max=float(raw["r_max"]), sigma=float(raw["sigma"]),
        null_sorted=np.asarray(raw["null_sorted"], dtype=np.float64),
        x_f=math.inf if x_f is None else float(x_f), n_eff_7d=float(raw["n_eff_7d"]),
    )  # fmt: skip


def load_calibration(run_dir: Path) -> Calibration | None:
    """The calibration of a champion version: ``None`` if ``10_calibration.json`` does not exist (nothing was
    calibrated); ``CalibrationError`` when the pre-registration was changed after the run (its sha256 is not the one
    the calibration recorded), when the rule of the file is not the rule of the code, or when the schema is not ours.
    A decision other than ``adopted`` loads with no features."""
    path = Path(run_dir) / CALIBRATION_FILE
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise CalibrationError(f"{path.name} is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict) or doc.get("schema") != CALIBRATION_SCHEMA:
        raise CalibrationError(f"{path.name}: schema is not {CALIBRATION_SCHEMA!r}")
    prereg = Path(run_dir) / PREREG_FILE
    if not prereg.is_file():
        raise CalibrationError(f"{PREREG_FILE} is missing: the calibration cannot be verified")
    prereg_sha = _sha256_file(prereg)
    if prereg_sha != doc.get("preregistration_sha256"):
        raise CalibrationError(f"{PREREG_FILE} changed after the run (sha256 {prereg_sha[:12]} != the recorded one)")
    if doc.get("rule") != rule_json():
        raise CalibrationError("the rule of the calibration is not the rule of the code (RULE)")
    reference = doc.get("reference") if doc.get("decision") == "adopted" else None
    features = {} if reference is None else {n: feature_ref(n, r) for n, r in reference["features"].items()}
    c = doc.get("c")
    return Calibration(
        champion_version=str(doc["champion_version"]), label_config_sha256=str(doc["label_config_sha256"]),
        window_days=doc.get("window_days"), alpha=float(doc["alpha"]), c=None if c is None else float(c),
        features=features, decision=str(doc["decision"]), preregistration_sha256=prereg_sha,
        sha256=_sha256_file(path), run_dir=Path(run_dir),
    )  # fmt: skip
