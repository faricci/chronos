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
from dataclasses import dataclass, field
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
    "D1H_END_LAG_DAYS",
    "D1H_LEVEL",
    "D1H_STEP_DAYS",
    "FeatureRef",
    "FeatureResult",
    "PREREG_FILE",
    "PREREG_SCHEMA",
    "RULE",
    "RULES",
    "RULE_V3",
    "S6_FILE",
    "S6_SCHEMA",
    "VOLSTD_BASE",
    "VOLSTD_FEATURES",
    "WindowResult",
    "applicable",
    "bin_counts",
    "candidate_features",
    "empirical_p",
    "feature_ref",
    "feature_values",
    "hourly_states",
    "inner_edges",
    "integrated_tau",
    "k_h",
    "load_calibration",
    "minp_threshold",
    "psi_v2",
    "ref_shares",
    "rule_json",
    "run_dir_of",
    "scope_note",
    "window_diagnostics",
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

# ---------------------------------------------------------------------------------------- PSI-v3 (P1f-bis)
# The LAST rule for ``feature_drift`` (docs/decisions/2026-10-26-p1f-bis-psi-v3.md, pre-registered before any z was
# computed on real data). PSI-v2 above is closed (``rejected``, 2026-10-25) and stays readable. v3 differs in: the 14
# fast features are standardised by the volatility of the same state (``z_f = f / rv_1d_bps``), the min-p family is
# the z with ``n_eff_7d >= 50`` (J), one window of 7 days, an extra check on days the calibration never used (D1h).
VOLSTD_PREFIX: Final = "z_"
VOLSTD_DENOMINATOR: Final = "rv_1d_bps"
VOLSTD_BASE: Final = (
    "ret_15m_bps", "ret_1h_bps", *[c for c in FLAT_COLUMNS if c.startswith("seq_1h_bps_")],
)  # fmt: skip
if len(VOLSTD_BASE) != 14 or not all(c in FLAT_COLUMNS for c in (*VOLSTD_BASE, VOLSTD_DENOMINATOR)):
    raise RuntimeError("VOLSTD_BASE is not the 14 fast features of the pre-registered rule: stop and open a conflict")
VOLSTD_FEATURES: Final = tuple(VOLSTD_PREFIX + b for b in VOLSTD_BASE)
D1H_LEVEL: Final = 0.05
D1H_STEP_DAYS: Final = 7
D1H_END_LAG_DAYS: Final = 7

RULE_V3: Final[dict[str, Any]] = {
    "id": "PSI_V3_VOLSTD",
    "QUANTILES": [0.005, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.995],
    "MIN_INNER_EDGES": 3, "JEFFREYS": 0.5, "REF_FLOOR": 0.5, "MIN_FINITE_FRAC": 0.9, "ALPHA": 0.01,
    "WINDOW_DAYS": [7], "STEP_DAYS": 1, "TAU_MAX_LAG_H": 168, "TAU_STOP": 0.05, "NEFF_JUDGED": 50.0,
    "D1_MAX_FWER": 0.02, "D1H_LEVEL": 0.05, "D1H_STEP_DAYS": 7, "D1H_END_LAG_DAYS": 7, "D2_MAX_X": 0.25,
    "D3_MIN_POWER": 0.8, "DELTAS": [0.5, 1.0], "OOS_BLOCK_DAYS": 182, "OOS_GUARD_DAYS": 7,
    "VOLSTD_BASE": [
        "ret_15m_bps", "ret_1h_bps", "seq_1h_bps_00", "seq_1h_bps_01", "seq_1h_bps_02", "seq_1h_bps_03",
        "seq_1h_bps_04", "seq_1h_bps_05", "seq_1h_bps_06", "seq_1h_bps_07", "seq_1h_bps_08", "seq_1h_bps_09",
        "seq_1h_bps_10", "seq_1h_bps_11",
    ],
    "VOLSTD_DENOMINATOR": "rv_1d_bps", "VOLSTD_PREFIX": "z_", "R_T0": "all_candidate_features_finite",
    "FAMILY": "volstd_neff_7d_ge_NEFF_JUDGED", "FINAL": True,
}  # fmt: skip
RULES: Final[dict[str, dict[str, Any]]] = {"PSI_V2": RULE, "PSI_V3_VOLSTD": RULE_V3}
PREREG_SCHEMA_V3: Final = "chronos.sim_p1f.prereg.v2"
CALIBRATION_SCHEMA_V3: Final = "chronos.sim_p1f.calibration.v2"


class CalibrationError(Exception):
    """The calibration artefact is not trustworthy (tampered pre-registration, another rule, another schema)."""


# --------------------------------------------------------------------------------------------- the features


def candidate_features(sentiment_available: bool) -> tuple[str, ...]:
    """The FLAT columns the gate can look at: not the calendar (deterministic: PSI is 0 on full weeks by
    construction), not ``pos_*`` (null on FLAT states), not ``cost_*`` (constants of the label_config), and not
    ``sent_*`` unless the model uses the sentiment. With ``sentiment_available`` false: 42 names."""
    skipped = ("clock_", "pos_", "cost_") + (() if sentiment_available else ("sent_",))
    return tuple(c for c in FLAT_COLUMNS if not c.startswith(skipped))


def feature_values(x: np.ndarray, name: str) -> np.ndarray:
    """The values of a feature of the gate over the rows of a FLAT matrix. A plain name is its column; ``z_<f>``
    (PSI-v3) is ``f / rv_1d_bps`` of the SAME state, NaN where either is not finite or the volatility is not > 0."""
    if not name.startswith(VOLSTD_PREFIX):
        return x[:, _COL[name]]
    num = x[:, _COL[name[len(VOLSTD_PREFIX) :]]]
    den = x[:, _COL[VOLSTD_DENOMINATOR]]
    ok = np.isfinite(num) & np.isfinite(den) & (den > 0)
    out = np.full(num.shape, np.nan)
    with np.errstate(all="ignore"):
        out[ok] = num[ok] / den[ok]
    return out


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


def k_h(n: int, p0: float = ALPHA, level: float = D1H_LEVEL) -> int | None:
    """The smallest ``k >= 0`` with ``P(Bin(n, p0) > k) <= level``: how many alarms the days that calibration never used
    (D1h) may show. Exact sum, no scipy. ``None`` for ``n == 0`` (D1h cannot be checked: not passed)."""
    if n <= 0:
        return None
    for k in range(n + 1):
        tail = sum(math.comb(n, i) * p0**i * (1.0 - p0) ** (n - i) for i in range(k + 1, n + 1))
        if tail <= level:
            return k
    return n


def scope_note(n_untested: int) -> str:
    """The sentence that goes with every ``feature_drift`` that is measured: what the test does NOT cover."""
    return (
        "feature_drift (PSI-v3): forma dei rendimenti veloci standardizzati per volatilita'. Livello di volatilita' e "
        f"{n_untested} feature persistenti non testabili in 7 giorni (n_eff_7d < 50): solo diagnostica support."
    )


def run_dir_of(root: Path, rule: Mapping[str, Any], version: str) -> Path:
    """Folder of the calibration of ``version``: ``<root>/<version>`` for PSI-v2 (existing layout),
    ``<root>/<rule id>/<version>`` for the later rules."""
    return Path(root) / version if rule["id"] == RULE["id"] else Path(root) / str(rule["id"]) / version


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
    # PSI-v3 (all with defaults: the v2 constructions stay valid): the RAW features of R with their null (diagnostics
    # only), the features the test does not cover, the reference of p_edge on the holdout, ``final`` rule
    rule_id: str = "PSI_V2"
    diagnostic: Mapping[str, FeatureRef] = field(default_factory=dict)
    untested: tuple[tuple[str, float | None], ...] = ()
    p_edge_ref: FeatureRef | None = None
    final: bool = False


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
        col = feature_values(x_window, name)
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


def window_diagnostics(
    x_window: np.ndarray, cal: Calibration, product: str, p_edge: np.ndarray | None
) -> dict[str, Any]:
    """Diagnostics of a window of 7 days, none of them a gate (PSI-v3): ``support`` (the share of states with a feature
    outside the range seen in the training: the boosters extrapolate flat there), ``psi_percentile`` (for the features
    the test does not cover, where this week's PSI falls among the weeks of the training: informative, no p-value) and
    ``p_edge_psi`` (the PSI of the model's ``p_edge`` against its predictions on the HOLDOUT, never on the training)."""
    n_expected = 24 * 7
    n = int(x_window.shape[0])
    outside = np.zeros(n, dtype=bool)
    per_feature: dict[str, int] = {}
    for name, ref in cal.diagnostic.items():
        if not applicable(name, product):
            continue
        col = feature_values(x_window, name)
        bad = np.isfinite(col) & ((col < ref.r_min) | (col > ref.r_max))
        if bad.any():
            per_feature[name] = int(bad.sum())
        outside |= bad
    support = {
        "states": n, "states_outside": int(outside.sum()), "share": float(outside.sum()) / n if n else 0.0,
        "features": per_feature,
    }  # fmt: skip
    percentile: dict[str, Any] = {}
    for name, n_eff in cal.untested:
        slow = cal.diagnostic.get(name)
        if slow is None or not applicable(name, product):
            continue
        psi = psi_v2(feature_values(x_window, name), slow.inner, slow.p, n_expected)
        pct = None
        if psi is not None and slow.null_sorted.size:
            pct = float(np.searchsorted(slow.null_sorted, psi, side="right")) / slow.null_sorted.size
        percentile[name] = {"psi": psi, "percentile": pct, "n_eff_7d": n_eff}
    if cal.p_edge_ref is None:
        edge: dict[str, Any] = {"status": "unmeasured", "reason": "no_reference"}
    elif p_edge is None:
        edge = {"status": "unmeasured", "reason": "no_deciders"}
    else:
        value = psi_v2(p_edge, cal.p_edge_ref.inner, cal.p_edge_ref.p, n_expected)
        edge = (
            {"status": "unmeasured", "reason": "incomplete"} if value is None else
            {"status": "measured", "psi": value, "n": int(np.isfinite(p_edge).sum()), "reference": "holdout"}
        )  # fmt: skip
    return {"support": support, "psi_percentile": percentile, "p_edge_psi": edge}


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


def rule_json(rule: Mapping[str, Any] = RULE) -> dict[str, Any]:
    """A rule (default ``RULE``) as it reads back from a JSON file."""
    loaded: dict[str, Any] = json.loads(json.dumps(rule))
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


def load_calibration(run_dir: Path, rule: Mapping[str, Any] = RULE) -> Calibration | None:
    """The calibration of a champion version under ``rule`` (default PSI-v2): ``None`` if ``10_calibration.json`` does
    not exist (nothing was calibrated); ``CalibrationError`` when the pre-registration was changed after the run (its
    sha256 is not the one the calibration recorded), when the rule of the file is not ``rule``, or when the schema
    is not the one of the rule. A decision other than ``adopted`` loads with no ``features``. PSI-v3 files always load
    their diagnostic part (``diagnostic_reference``, ``scope.untested``, ``p_edge_reference``), in any case."""
    v3 = rule["id"] != RULE["id"]
    schema = CALIBRATION_SCHEMA_V3 if v3 else CALIBRATION_SCHEMA
    path = Path(run_dir) / CALIBRATION_FILE
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise CalibrationError(f"{path.name} is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict) or doc.get("schema") != schema:
        raise CalibrationError(f"{path.name}: schema is not {schema!r}")
    prereg = Path(run_dir) / PREREG_FILE
    if not prereg.is_file():
        raise CalibrationError(f"{PREREG_FILE} is missing: the calibration cannot be verified")
    prereg_sha = _sha256_file(prereg)
    if prereg_sha != doc.get("preregistration_sha256"):
        raise CalibrationError(f"{PREREG_FILE} changed after the run (sha256 {prereg_sha[:12]} != the recorded one)")
    if doc.get("rule") != rule_json(rule):
        raise CalibrationError(f"the rule of the calibration is not the rule of the code (RULE, {rule['id']})")
    reference = doc.get("reference") if doc.get("decision") == "adopted" else None
    features = {} if reference is None else {n: feature_ref(n, r) for n, r in reference["features"].items()}
    c = doc.get("c")
    extra: dict[str, Any] = {}
    if v3:
        edge = doc.get("p_edge_reference")
        extra = {
            "rule_id": str(rule["id"]), "final": bool(doc.get("final", False)),
            "diagnostic": {n: feature_ref(n, r) for n, r in (doc.get("diagnostic_reference") or {}).items()},
            "untested": tuple(
                (str(u["feature"]), None if u.get("n_eff_7d") is None else float(u["n_eff_7d"]))
                for u in (doc.get("scope") or {}).get("untested", [])
            ),
            "p_edge_ref": None if not edge else FeatureRef(
                "p_edge", np.asarray(edge["inner"], dtype=np.float64), np.asarray(edge["p"], dtype=np.float64),
                float(edge["r_min"]), float(edge["r_max"]), 0.0, np.array([]), math.inf, 0.0,
            ),
        }  # fmt: skip
    return Calibration(
        champion_version=str(doc["champion_version"]), label_config_sha256=str(doc["label_config_sha256"]),
        window_days=doc.get("window_days"), alpha=float(doc["alpha"]), c=None if c is None else float(c),
        features=features, decision=str(doc["decision"]), preregistration_sha256=prereg_sha,
        sha256=_sha256_file(path), run_dir=Path(run_dir), **extra,
    )  # fmt: skip
