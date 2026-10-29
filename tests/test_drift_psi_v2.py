"""PSI-v2 of the feature drift (conflict P1f, docs/decisions/2026-10-25-p1f-psi-v2.md): maths, calibration object,
artefact loader."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from modules.dryrun import drift
from modules.dryrun.drift import (
    CALIBRATION_FILE,
    CALIBRATION_SCHEMA,
    PREREG_FILE,
    Calibration,
    CalibrationError,
    FeatureRef,
    applicable,
    candidate_features,
    empirical_p,
    inner_edges,
    integrated_tau,
    load_calibration,
    minp_threshold,
    psi_v2,
    ref_shares,
    window_stat,
    x_threshold,
)
from modules.dryrun.learner import population_stability_index
from modules.features.serializer import FLAT_COLUMNS

COL = {n: j for j, n in enumerate(FLAT_COLUMNS)}


# ------------------------------------------------------------------------------------- the old function


def test_old_psi_hits_cap_on_constant_day() -> None:
    """The diagnosis: one value (a persistent feature in one day) occupies one decile and the old function returns
    its ceiling, ~8.28 (floor 1e-4); four products with one value each give 4.69 / 5.67 / 6.81 in 4 / 3 / 2 deciles."""
    hist = np.arange(10_000, dtype=np.float64)  # deciles at 1000, 2000, ...
    one = population_stability_index(np.full(200, 500.0), hist)
    assert one == pytest.approx(8.283, abs=0.01)
    centres = [500.0, 1500.0, 2500.0, 3500.0]
    four = np.repeat(centres, 50)
    three = np.repeat(centres[:3], 70)
    two = np.repeat(centres[:2], 100)
    assert population_stability_index(four, hist) == pytest.approx(4.69, abs=0.02)
    assert population_stability_index(three, hist) == pytest.approx(5.67, abs=0.02)
    assert population_stability_index(two, hist) == pytest.approx(6.81, abs=0.02)


# ------------------------------------------------------------------------------------------- PSI-v2


def test_psi_v2_formula_hand_computed() -> None:
    inner = np.array([1.0, 2.0, 3.0])  # 4 bins
    p = np.array([0.25, 0.25, 0.25, 0.25])
    x = np.array([0.5] * 4 + [1.5] * 3 + [2.5] * 2 + [3.5])  # counts 4, 3, 2, 1 (n = 10)
    q = np.array([4.5, 3.5, 2.5, 1.5]) / (10 + 0.5 * 4)  # Jeffreys
    expected = sum((qi - pi) * math.log(qi / pi) for qi, pi in zip(q, p, strict=True))
    assert psi_v2(x, inner, p, 10) == pytest.approx(expected, rel=1e-12)
    # a value on an edge goes to the UPPER bin
    assert list(drift.bin_counts(np.array([1.0, 2.0, 3.0]), inner)) == [0, 1, 1, 1]
    # NaN is not counted
    assert psi_v2(np.append(x, np.nan), inner, p, 10) == pytest.approx(expected, rel=1e-12)


def test_psi_v2_one_bin_is_not_capped() -> None:
    rng = np.random.default_rng(0)
    ref = rng.normal(size=20_000)
    inner = inner_edges(ref)
    assert inner is not None
    p = ref_shares(ref, inner)
    x = np.full(168, 0.0)  # all in one bin
    value = psi_v2(x, inner, p, 168)
    assert value is not None
    counts = drift.bin_counts(x, inner)
    q = (counts + 0.5) / (168 + 0.5 * counts.size)
    assert value == pytest.approx(float(np.sum((q - p) * np.log(q / p))), rel=1e-12)
    assert 3.0 < value < 8.2  # large, finite, below the ceiling of the old function (8.28)


def test_inner_edges_twelve_bins_with_tails() -> None:
    rng = np.random.default_rng(1)
    ref = rng.normal(size=100_000)
    inner = inner_edges(ref)
    assert inner is not None and inner.size == 11  # 12 bins with the two open ends
    shares = ref_shares(ref, inner)
    assert shares.size == 12
    assert shares[0] == pytest.approx(0.005, abs=0.0005) and shares[-1] == pytest.approx(0.005, abs=0.0005)
    assert shares[1:-1].tolist() == pytest.approx([0.095] + [0.1] * 8 + [0.095], abs=0.001)
    assert shares.sum() == pytest.approx(1.0)


def test_ref_shares_have_a_floor() -> None:
    ref = np.concatenate([np.zeros(1000), np.ones(1000)])
    inner = np.array([0.5, 0.7, 0.9])  # two bins stay empty
    shares = ref_shares(ref, inner)
    assert shares.min() == pytest.approx(0.5 / 2000)


def test_feature_with_few_edges_excluded() -> None:
    assert inner_edges(np.full(1000, 3.0)) is None
    assert inner_edges(np.array([0.0] * 999 + [1.0])) is None  # every quantile is 0: a single edge
    assert inner_edges(np.array([0.0] * 700 + [1.0] * 300)) is not None  # 0, 0.x (interpolated), 1: three edges
    assert inner_edges(np.array([np.nan] * 10)) is None
    assert inner_edges(np.array([0.0, 1.0, 2.0] * 500)) is not None  # three distinct edges


def test_unmeasured_below_90pct_finite() -> None:
    rng = np.random.default_rng(2)
    ref = rng.normal(size=5000)
    inner = inner_edges(ref)
    assert inner is not None
    p = ref_shares(ref, inner)
    x = rng.normal(size=168)
    x150 = x.copy()
    x150[150:] = np.nan  # 150 finite
    x151 = x.copy()
    x151[151:] = np.nan
    assert psi_v2(x150, inner, p, 168) is None
    assert psi_v2(x151, inner, p, 168) is not None
    assert math.floor(0.9 * 336) == 302 and psi_v2(x[:0], inner, p, 336) is None


def test_empirical_p_and_x_threshold_equivalent() -> None:
    rng = np.random.default_rng(3)
    null = np.sort(rng.gamma(2.0, 0.05, size=400))
    for c in (0.01, 0.0333, 0.2):
        x_f = x_threshold(null, c)
        assert math.isfinite(x_f)
        for x in np.concatenate([np.linspace(0.0, 1.0, 801), null, null + 1e-12, null - 1e-12]):
            assert (empirical_p(null, float(x)) < c) == (x > x_f), (c, x, x_f)
    assert empirical_p(null, null.max() + 1) == pytest.approx(1 / 401)
    assert empirical_p(null, null.min() - 1) == pytest.approx(1.0)
    assert x_threshold(null, 1 / 401) == math.inf  # even above the whole null: p = 1/(1+N) is not below c
    assert x_threshold(null, 0.001) == math.inf
    assert math.isfinite(x_threshold(null, 2.5 / 401))


def test_empirical_p_with_repeated_values() -> None:
    null = np.array([0.1, 0.1, 0.1, 0.2, 0.3])
    assert empirical_p(null, 0.1) == pytest.approx((1 + 5) / 6)
    assert empirical_p(null, 0.15) == pytest.approx((1 + 2) / 6)
    for c in (0.3, 0.5, 0.9):
        x_f = x_threshold(null, c)
        for x in (0.05, 0.1, 0.12, 0.2, 0.25, 0.3, 0.4):
            assert (empirical_p(null, x) < c) == (x > x_f)


def test_minp_threshold_controls_fwer() -> None:
    rng = np.random.default_rng(4)
    s = np.round(rng.uniform(size=2000) ** 3, 2)  # many repeated values
    c = minp_threshold(s)
    assert float(np.mean(s < c)) <= drift.ALPHA
    s2 = rng.uniform(size=999)
    assert float(np.mean(s2 < minp_threshold(s2))) <= drift.ALPHA


def _gauss_study(rng: np.random.Generator, n_feat: int, n_windows: int, w: int = 168) -> tuple[Any, ...]:
    ref = rng.normal(size=(60_000, n_feat))
    edges = [inner_edges(ref[:, j]) for j in range(n_feat)]
    shares = [ref_shares(ref[:, j], e) for j, e in enumerate(edges) if e is not None]
    edges_ok = [e for e in edges if e is not None]

    def psi_of(win: np.ndarray) -> list[float]:
        out = []
        for j in range(n_feat):
            v = psi_v2(win[:, j], edges_ok[j], shares[j], w)
            assert v is not None
            out.append(v)
        return out

    null = np.array([psi_of(rng.normal(size=(w, n_feat))) for _ in range(n_windows)])
    return edges_ok, shares, null, psi_of


def test_null_iid_low_fwer_and_shift_detected() -> None:
    rng = np.random.default_rng(5)
    n_feat = 8
    edges, shares, null, psi_of = _gauss_study(rng, n_feat, 1500)
    nulls = [np.sort(null[:, j]) for j in range(n_feat)]

    def s_of(psis: list[float]) -> float:
        return min(float(empirical_p(nulls[j], psis[j])) for j in range(n_feat))

    s_null = np.array([min(float(empirical_p(nulls[j], row[j])) for j in range(n_feat)) for row in null])
    c = minp_threshold(s_null)
    # fresh windows of the same process: false alarms stay at the level of alpha
    fresh = [s_of(psi_of(rng.normal(size=(168, n_feat)))) for _ in range(1500)]
    assert float(np.mean(np.array(fresh) < c)) <= 0.02
    # one feature shifted by one standard deviation: detected
    hits = 0
    for _ in range(200):
        win = rng.normal(size=(168, n_feat))
        win[:, 3] += 1.0
        hits += int(s_of(psi_of(win)) < c)
    assert hits / 200 >= 0.80


def test_candidate_features_are_42() -> None:
    names = candidate_features(False)
    assert len(names) == 42 and len(set(names)) == 42
    assert not any(n.startswith(("clock_", "pos_", "cost_", "sent_")) for n in names)
    assert {"p_breakeven", "btc_ret_1d_bps", "btc_rv_ratio_1d_30d", "seq_1d_bps_5", "atr_1d_pctile_365d"} <= set(names)
    assert [n for n in FLAT_COLUMNS if n in names] == list(names)  # FLAT_COLUMNS order
    with_sentiment = candidate_features(True)
    assert len(with_sentiment) == 42 + 18 and all(n in with_sentiment for n in names)


def test_btc_features_not_applicable_to_btc() -> None:
    assert not applicable("btc_ret_1d_bps", "BTC-EUR")
    assert not applicable("btc_rv_ratio_1d_30d", "BTC-EUR")
    assert applicable("btc_ret_1d_bps", "ETH-EUR") and applicable("ret_1h_bps", "BTC-EUR")


def test_integrated_tau_ar1() -> None:
    rng = np.random.default_rng(6)
    phi, n = 0.5, 40_000
    e = rng.normal(size=n)
    x = np.zeros(n)
    for t in range(1, n):
        x[t] = phi * x[t - 1] + e[t]
    # the sum stops at the first rho_k < 0.05 (k = 5 for phi = 0.5), which is not summed: 1 + 2 (0.5 + ... + 0.5^4)
    assert integrated_tau(x) == pytest.approx(1 + 2 * sum(phi**k for k in range(1, 5)), abs=0.15)
    assert integrated_tau(rng.normal(size=n)) == pytest.approx(1.0, abs=0.1)
    assert integrated_tau(np.full(100, 2.0)) == 1.0  # constant: no autocorrelation defined
    assert 168 / integrated_tau(rng.normal(size=n)) == pytest.approx(168, rel=0.1)


# ------------------------------------------------------------------------------------ window_stat


def make_cal(rng: np.random.Generator, window_days: int = 7, names: tuple[str, ...] = ("ret_1h_bps", "btc_ret_1d_bps")):
    ref = rng.normal(size=(40_000, len(names)))
    feats: dict[str, FeatureRef] = {}
    n_exp = 24 * window_days
    psis: list[list[float]] = []
    for j, name in enumerate(names):
        inner = inner_edges(ref[:, j])
        assert inner is not None
        feats[name] = FeatureRef(name, inner, ref_shares(ref[:, j], inner), float(ref[:, j].min()),
                                 float(ref[:, j].max()), 1.0, np.array([]), math.inf, 100.0)  # fmt: skip
        psis.append([])
    for _ in range(600):
        win = rng.normal(size=(n_exp, len(names)))
        for j, name in enumerate(names):
            v = psi_v2(win[:, j], feats[name].inner, feats[name].p, n_exp)
            assert v is not None
            psis[j].append(v)
    nulls = [np.sort(np.array(v)) for v in psis]
    s_null = np.array([min(float(empirical_p(nulls[j], psis[j][i])) for j in range(len(names))) for i in range(600)])
    c = minp_threshold(s_null)
    full = {
        n: FeatureRef(n, f.inner, f.p, f.r_min, f.r_max, f.sigma, nulls[j], x_threshold(nulls[j], c), f.n_eff_7d)
        for j, (n, f) in enumerate(feats.items())
    }
    cal = Calibration("m@1", "sha", window_days, drift.ALPHA, c, full, "adopted", "pre", "cal", Path("."))
    return cal


def window_matrix(values: dict[str, np.ndarray]) -> np.ndarray:
    n = len(next(iter(values.values())))
    x = np.full((n, len(FLAT_COLUMNS)), np.nan)
    for name, v in values.items():
        x[:, COL[name]] = v
    return x


def test_window_stat_alarm_on_a_shifted_window_and_not_on_a_null_one() -> None:
    rng = np.random.default_rng(7)
    cal = make_cal(rng)
    quiet = window_matrix({"ret_1h_bps": rng.normal(size=168), "btc_ret_1d_bps": rng.normal(size=168)})
    r = window_stat(quiet, cal, "ETH-EUR")
    assert not r.alarm and r.S is not None and r.S >= cal.c  # type: ignore[operator]
    moved = window_matrix({"ret_1h_bps": rng.normal(loc=2.0, size=168), "btc_ret_1d_bps": rng.normal(size=168)})
    r2 = window_stat(moved, cal, "ETH-EUR")
    assert r2.alarm and r2.features["ret_1h_bps"].flag and not r2.features["btc_ret_1d_bps"].flag
    share = r2.features["ret_1h_bps"].out_of_range_share
    assert share is not None and 0.0 <= share <= 1.0


def test_window_stat_skips_btc_features_on_btc_and_marks_unmeasured() -> None:
    rng = np.random.default_rng(8)
    cal = make_cal(rng)
    x = window_matrix({"ret_1h_bps": rng.normal(size=168), "btc_ret_1d_bps": np.full(168, np.nan)})
    on_btc = window_stat(x, cal, "BTC-EUR")
    assert set(on_btc.features) == {"ret_1h_bps"}
    on_eth = window_stat(x, cal, "ETH-EUR")  # all NaN on an alt: unmeasured, never a pass by default
    assert on_eth.features["btc_ret_1d_bps"].psi is None and on_eth.features["btc_ret_1d_bps"].n_finite == 0
    assert on_eth.S == on_eth.features["ret_1h_bps"].p
    nothing = window_stat(window_matrix({"ret_1h_bps": np.full(168, np.nan)}), cal, "BTC-EUR")
    assert nothing.S is None and not nothing.alarm
    with pytest.raises(ValueError, match="adopted window"):
        window_stat(x, Calibration("m", "s", None, 0.01, None, {}, "rejected", "p", "c", Path(".")), "ETH-EUR")


# ----------------------------------------------------------------------------------- the artefact


def feature_json(rng: np.random.Generator) -> dict[str, Any]:
    ref = rng.normal(size=5000)
    inner = inner_edges(ref)
    assert inner is not None
    return {
        "inner": inner.tolist(), "p": ref_shares(ref, inner).tolist(), "r_min": float(ref.min()),
        "r_max": float(ref.max()), "sigma": 1.0, "null_sorted": [0.01, 0.02, 0.5], "x_f": None, "n_eff_7d": 12.5,
    }  # fmt: skip


def write_run(folder: Path, *, decision: str = "adopted", rule: dict[str, Any] | None = None) -> Path:
    import hashlib

    folder.mkdir(parents=True)
    (folder / PREREG_FILE).write_text(json.dumps({"schema": "chronos.sim_p1f.prereg.v1"}), encoding="utf-8")
    sha = hashlib.sha256((folder / PREREG_FILE).read_bytes()).hexdigest()
    rng = np.random.default_rng(9)
    adopted = decision == "adopted"
    doc = {
        "schema": CALIBRATION_SCHEMA, "rule": rule or drift.rule_json(), "champion_version": "m@1",
        "label_config_sha256": "abc", "decision": decision, "window_days": 7 if decision == "adopted" else None,
        "alpha": 0.01, "c": 0.0021 if decision == "adopted" else None, "preregistration_sha256": sha,
        "reference": {"window_days": 7, "features": {"ret_1h_bps": feature_json(rng)}} if adopted else None,
    }  # fmt: skip
    (folder / CALIBRATION_FILE).write_text(json.dumps(doc), encoding="utf-8")
    return folder


def test_load_calibration_roundtrip_and_missing(tmp_path: Path) -> None:
    assert load_calibration(tmp_path / "none") is None
    cal = load_calibration(write_run(tmp_path / "ok"))
    assert cal is not None and cal.decision == "adopted" and cal.window_days == 7 and cal.c == 0.0021
    ref = cal.features["ret_1h_bps"]
    assert ref.x_f == math.inf and ref.n_eff_7d == 12.5 and ref.null_sorted.tolist() == [0.01, 0.02, 0.5]
    assert len(cal.sha256) == 64 and cal.champion_version == "m@1" and cal.label_config_sha256 == "abc"
    rejected = load_calibration(write_run(tmp_path / "rej", decision="rejected"))
    assert rejected is not None and rejected.decision == "rejected" and not rejected.features
    assert rejected.window_days is None and rejected.c is None


def test_load_calibration_rejects_tampered_prereg(tmp_path: Path) -> None:
    folder = write_run(tmp_path / "t")
    (folder / PREREG_FILE).write_text(json.dumps({"schema": "chronos.sim_p1f.prereg.v1", "x": 1}), encoding="utf-8")
    with pytest.raises(CalibrationError, match="changed after the run"):
        load_calibration(folder)
    (folder / PREREG_FILE).unlink()
    with pytest.raises(CalibrationError, match="missing"):
        load_calibration(folder)


def test_load_calibration_rejects_rule_mismatch(tmp_path: Path) -> None:
    other = drift.rule_json()
    other["ALPHA"] = 0.05
    with pytest.raises(CalibrationError, match="RULE"):
        load_calibration(write_run(tmp_path / "r", rule=other))


def test_load_calibration_rejects_another_schema(tmp_path: Path) -> None:
    folder = write_run(tmp_path / "s")
    doc = json.loads((folder / CALIBRATION_FILE).read_text(encoding="utf-8"))
    doc["schema"] = "chronos.sim_p1f.calibration.v0"
    (folder / CALIBRATION_FILE).write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(CalibrationError, match="schema"):
        load_calibration(folder)


def test_rule_is_the_preregistered_one() -> None:
    r = drift.rule_json()
    assert r["id"] == "PSI_V2" and r["ALPHA"] == 0.01 and r["WINDOW_DAYS"] == [7, 14]
    assert r["QUANTILES"] == [0.005, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.995]
    assert (r["D1_MAX_FWER"], r["D2_MAX_X"], r["D3_MIN_POWER"], r["NEFF_JUDGED"]) == (0.02, 0.25, 0.80, 50.0)
    assert len(r["D0_SLOW"]) == 11 and r["D0_MIN_WRONG"] == 6 and r["DELTAS"] == [0.5, 1.0]
    assert r["OOS_BLOCK_DAYS"] == 182 and r["OOS_GUARD_DAYS"] == 7 and r["TAU_MAX_LAG_H"] == 168


# -------------------------------------------------------------------------------- wiring / hygiene


def test_set_drift_has_no_caller_in_modules() -> None:
    """``risk.kill_switch.psi_threshold`` and PSI-v2 act on nothing: the ``drift`` guard of ``SystemGuards`` has no
    writer. Linking it (entry block on a PSI-v2 alarm, per product) is a separate decision: when it is taken this test
    changes with it."""
    root = Path(drift.__file__).resolve().parents[1]
    calls = []
    for path in root.rglob("*.py"):
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\bset_drift\(", line) and not re.search(r"\bdef set_drift\(", line):
                calls.append(f"{path.relative_to(root)}:{no}")
    assert calls == []


def test_no_identifier_with_the_forbidden_token_in_the_new_module() -> None:
    text = Path(drift.__file__).read_text(encoding="utf-8").lower()
    assert "margin" not in text
