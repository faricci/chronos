"""PSI-v3 (conflict P1f-bis, docs/decisions/2026-10-26-p1f-bis-psi-v3.md): the volatility standardisation, the rule
record, the loader under two rules, the diagnostics. Synthetic data only."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from modules.dryrun import drift
from modules.dryrun.drift import (
    CALIBRATION_FILE,
    CALIBRATION_SCHEMA,
    CALIBRATION_SCHEMA_V3,
    PREREG_FILE,
    RULE,
    RULE_V3,
    Calibration,
    CalibrationError,
    FeatureRef,
    feature_values,
    inner_edges,
    k_h,
    load_calibration,
    ref_shares,
    run_dir_of,
    scope_note,
    window_diagnostics,
    window_stat,
)
from modules.features.serializer import FLAT_COLUMNS

COL = {n: j for j, n in enumerate(FLAT_COLUMNS)}
DOC = Path(drift.__file__).resolve().parents[2] / "docs" / "decisions" / "2026-10-26-p1f-bis-psi-v3.md"


def matrix(**cols: np.ndarray) -> np.ndarray:
    n = len(next(iter(cols.values())))
    x = np.full((n, len(FLAT_COLUMNS)), np.nan)
    for name, v in cols.items():
        x[:, COL[name]] = v
    return x


# ------------------------------------------------------------------------------------------ feature_values


def test_feature_values_raw_is_the_column() -> None:
    x = matrix(ret_1h_bps=np.arange(5.0), rv_1d_bps=np.full(5, 10.0))
    assert feature_values(x, "ret_1h_bps").tolist() == [0.0, 1.0, 2.0, 3.0, 4.0]
    assert np.shares_memory(feature_values(x, "ret_1h_bps"), x)  # a view of the column, v2 reads exactly this


def test_feature_values_volstd_and_nan_rules() -> None:
    ret = np.array([10.0, -20.0, 5.0, np.nan, 7.0, 3.0])
    rv = np.array([5.0, 10.0, 0.0, 4.0, -2.0, np.nan])
    z = feature_values(matrix(ret_1h_bps=ret, rv_1d_bps=rv), "z_ret_1h_bps")
    assert z[:2].tolist() == [2.0, -2.0]
    assert np.isnan(z[2:]).all()  # rv <= 0, NaN numerator, rv <= 0, NaN denominator
    assert drift.VOLSTD_FEATURES[0] == "z_ret_15m_bps" and len(drift.VOLSTD_FEATURES) == 14
    assert all(f in FLAT_COLUMNS for f in drift.VOLSTD_BASE) and "rv_1d_bps" in FLAT_COLUMNS


def synthetic_cal(rng: np.random.Generator) -> tuple[Calibration, np.ndarray]:
    """A calibration whose only feature is ``z_ret_1h_bps`` (reference: 20000 standardised returns)."""
    ref_x = matrix(ret_1h_bps=rng.normal(size=20_000) * 20.0, rv_1d_bps=np.full(20_000, 20.0))
    z = feature_values(ref_x, "z_ret_1h_bps")
    inner = inner_edges(z)
    assert inner is not None
    null = np.sort(np.linspace(0.02, 1.0, 100))
    ref = FeatureRef("z_ret_1h_bps", inner, ref_shares(z, inner), float(z.min()), float(z.max()), float(z.std()),
                     null, 0.9, 168.0)  # fmt: skip
    cal = Calibration("m", "s", 7, 0.01, 0.01, {"z_ret_1h_bps": ref}, "adopted", "p", "c", Path("."), "PSI_V3_VOLSTD")
    return cal, ref_x


def test_window_stat_v3_is_scale_invariant() -> None:
    """The fast features and the volatility of the same state both double: the standardised value, the PSI and the
    flag do not move (the quantity PSI-v2 could not measure: a week with another volatility)."""
    rng = np.random.default_rng(10)
    cal, _ = synthetic_cal(rng)
    ret = rng.normal(size=168) * 20.0
    rv = np.full(168, 20.0)
    a = window_stat(matrix(ret_1h_bps=ret, rv_1d_bps=rv), cal, "ETH-EUR")
    b = window_stat(matrix(ret_1h_bps=2 * ret, rv_1d_bps=2 * rv), cal, "ETH-EUR")
    assert a.features["z_ret_1h_bps"].psi == pytest.approx(b.features["z_ret_1h_bps"].psi, rel=1e-12)
    assert a.S == b.S and a.alarm == b.alarm
    # the same returns with a volatility that does not follow them DO move it
    c = window_stat(matrix(ret_1h_bps=ret, rv_1d_bps=2 * rv), cal, "ETH-EUR")
    assert c.features["z_ret_1h_bps"].psi != a.features["z_ret_1h_bps"].psi


# ------------------------------------------------------------------------------------------- the rule record


def test_rule_v3_literal_and_shared_constants_equal_v2() -> None:
    assert RULE_V3["id"] == "PSI_V3_VOLSTD" and RULE_V3["FINAL"] is True and RULE_V3["WINDOW_DAYS"] == [7]
    differing = {"id", "WINDOW_DAYS"}
    for key in set(RULE) & set(RULE_V3) - differing:
        assert RULE[key] == RULE_V3[key], key
    # the code uses the module constants: the record cannot drift from them
    assert RULE_V3["ALPHA"] == drift.ALPHA and RULE_V3["D1H_LEVEL"] == drift.D1H_LEVEL
    assert RULE_V3["D1H_STEP_DAYS"] == drift.D1H_STEP_DAYS and RULE_V3["D1H_END_LAG_DAYS"] == drift.D1H_END_LAG_DAYS
    assert RULE_V3["OOS_BLOCK_DAYS"] == drift.OOS_BLOCK_DAYS and RULE_V3["VOLSTD_BASE"] == list(drift.VOLSTD_BASE)
    assert RULE_V3["VOLSTD_PREFIX"] == drift.VOLSTD_PREFIX and RULE_V3["VOLSTD_DENOMINATOR"] == "rv_1d_bps"
    assert len(RULE_V3["VOLSTD_BASE"]) == 14 and "D0_SLOW" not in RULE_V3
    assert drift.RULES == {"PSI_V2": RULE, "PSI_V3_VOLSTD": RULE_V3}
    assert RULE["id"] == "PSI_V2" and RULE["WINDOW_DAYS"] == [7, 14]  # the closed rule is untouched


def test_rule_v3_in_decision_document() -> None:
    text = DOC.read_text(encoding="utf-8")
    for key, value in RULE_V3.items():
        assert f"`{key} = {value!r}`" in text, key
    assert "ULTIMA regola" in text and "decide_v3" in text


# ----------------------------------------------------------------------------------------------- k_h


def test_k_h() -> None:
    assert k_h(51) == 2 and k_h(0) is None and k_h(-3) is None
    # exact tail: P(Bin(51, 0.01) > 2) ~ 1.5%, P(> 1) ~ 8.7% (the document says ~9%)
    tail = lambda k: sum(math.comb(51, i) * 0.01**i * 0.99 ** (51 - i) for i in range(k + 1, 52))  # noqa: E731
    assert tail(2) <= 0.05 < tail(1)
    values = [k_h(n) for n in range(1, 400)]
    assert all(v is not None for v in values) and values == sorted(values)  # type: ignore[type-var]
    assert k_h(1) == 0 and k_h(5) == 0 and k_h(6) == 1
    assert k_h(100, 0.01, 0.05) == 3


# ----------------------------------------------------------------------------------------- the loader


def feature_json(rng: np.random.Generator, null: list[float] | None = None) -> dict[str, Any]:
    ref = rng.normal(size=4000)
    inner = inner_edges(ref)
    assert inner is not None
    return {
        "inner": inner.tolist(), "p": ref_shares(ref, inner).tolist(), "r_min": float(ref.min()),
        "r_max": float(ref.max()), "sigma": 1.0, "null_sorted": null or [0.01, 0.02, 0.5, 0.9], "x_f": None,
        "n_eff_7d": 168.0,
    }  # fmt: skip


def write_run(folder: Path, rule: dict[str, Any], *, decision: str = "adopted") -> Path:
    folder.mkdir(parents=True)
    (folder / PREREG_FILE).write_text(json.dumps({"x": 1}), encoding="utf-8")
    sha = hashlib.sha256((folder / PREREG_FILE).read_bytes()).hexdigest()
    rng = np.random.default_rng(3)
    adopted = decision == "adopted"
    doc: dict[str, Any] = {
        "schema": CALIBRATION_SCHEMA_V3 if rule["id"] != "PSI_V2" else CALIBRATION_SCHEMA,
        "rule": drift.rule_json(rule), "champion_version": "m@1", "label_config_sha256": "abc", "decision": decision,
        "window_days": 7 if adopted else None, "alpha": 0.01, "c": 0.002 if adopted else None,
        "preregistration_sha256": sha, "final": True,
        "reference": {"window_days": 7, "features": {"z_ret_1h_bps": feature_json(rng)}} if adopted else None,
        "diagnostic_reference": {"atr_14_1d_bps": feature_json(rng), "ret_1h_bps": feature_json(rng)},
        "scope": {"tested": ["z_ret_1h_bps"], "untested": [{"feature": "atr_14_1d_bps", "n_eff_7d": 0.9},
                                                           {"feature": "z_gone", "n_eff_7d": None}]},
        "p_edge_reference": {"inner": [0.3, 0.4, 0.5], "p": [0.25, 0.25, 0.25, 0.25], "r_min": 0.1, "r_max": 0.8},
    }  # fmt: skip
    (folder / CALIBRATION_FILE).write_text(json.dumps(doc), encoding="utf-8")
    return folder


def test_load_calibration_rule_mismatch_both_ways(tmp_path: Path) -> None:
    v3 = write_run(tmp_path / "v3", RULE_V3)
    v2 = write_run(tmp_path / "v2", RULE)
    with pytest.raises(CalibrationError, match="schema"):
        load_calibration(v3)  # a v3 file read as PSI-v2
    with pytest.raises(CalibrationError, match="schema"):
        load_calibration(v2, RULE_V3)  # and the other way round
    ok3 = load_calibration(v3, RULE_V3)
    ok2 = load_calibration(v2)
    assert ok3 is not None and ok2 is not None and ok3.rule_id == "PSI_V3_VOLSTD" and ok2.rule_id == "PSI_V2"
    assert not ok2.diagnostic and not ok2.untested and ok2.p_edge_ref is None and not ok2.final
    # same schema, other constants: the rule record is compared whole
    doc = json.loads((v3 / CALIBRATION_FILE).read_text(encoding="utf-8"))
    doc["rule"]["ALPHA"] = 0.05
    (v3 / CALIBRATION_FILE).write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(CalibrationError, match="rule of the code"):
        load_calibration(v3, RULE_V3)


def test_load_calibration_v3_rejected_keeps_diagnostics(tmp_path: Path) -> None:
    cal = load_calibration(write_run(tmp_path / "rej", RULE_V3, decision="rejected"), RULE_V3)
    assert cal is not None and cal.decision == "rejected" and cal.window_days is None and cal.c is None
    assert not cal.features  # nothing adopted ...
    assert set(cal.diagnostic) == {"atr_14_1d_bps", "ret_1h_bps"} and cal.final  # ... but the diagnostics load
    assert cal.untested == (("atr_14_1d_bps", 0.9), ("z_gone", None))
    assert cal.p_edge_ref is not None and cal.p_edge_ref.r_max == 0.8
    adopted = load_calibration(write_run(tmp_path / "ok", RULE_V3), RULE_V3)
    assert adopted is not None and set(adopted.features) == {"z_ret_1h_bps"} and adopted.window_days == 7
    tampered = write_run(tmp_path / "t", RULE_V3)
    (tampered / PREREG_FILE).write_text("{}", encoding="utf-8")
    with pytest.raises(CalibrationError, match="changed after the run"):
        load_calibration(tampered, RULE_V3)


def test_window_diagnostics_support_percentile_pedge(tmp_path: Path) -> None:
    cal = load_calibration(write_run(tmp_path / "d", RULE_V3), RULE_V3)
    assert cal is not None
    r = cal.diagnostic["atr_14_1d_bps"]
    n = 168
    inside = np.zeros(n)
    x = matrix(atr_14_1d_bps=inside, ret_1h_bps=np.zeros(n))
    x[:10, COL["atr_14_1d_bps"]] = r.r_max + 1.0  # 10 states outside the range seen in the training
    x[3:12, COL["ret_1h_bps"]] = cal.diagnostic["ret_1h_bps"].r_min - 1.0
    p_edge = np.linspace(0.2, 0.7, n)
    d = window_diagnostics(x, cal, "ETH-EUR", p_edge)
    assert d["support"]["states"] == n and d["support"]["states_outside"] == 12  # rows 0-11
    assert d["support"]["share"] == pytest.approx(12 / n)
    assert d["support"]["features"] == {"atr_14_1d_bps": 10, "ret_1h_bps": 9}
    pct = d["psi_percentile"]
    assert set(pct) == {"atr_14_1d_bps"} and pct["atr_14_1d_bps"]["n_eff_7d"] == 0.9  # z_gone is not a reference
    assert pct["atr_14_1d_bps"]["psi"] is not None and pct["atr_14_1d_bps"]["percentile"] == 1.0  # above the null
    edge = d["p_edge_psi"]
    assert edge["status"] == "measured" and edge["reference"] == "holdout" and edge["n"] == n
    none = window_diagnostics(x, cal, "ETH-EUR", None)["p_edge_psi"]
    assert none == {"status": "unmeasured", "reason": "no_deciders"}
    bare = Calibration("m", "s", 7, 0.01, 0.01, {}, "adopted", "p", "c", Path("."), "PSI_V3_VOLSTD", cal.diagnostic)
    assert window_diagnostics(x, bare, "ETH-EUR", p_edge)["p_edge_psi"]["reason"] == "no_reference"
    quiet = window_diagnostics(matrix(atr_14_1d_bps=inside, ret_1h_bps=np.zeros(n)), cal, "ETH-EUR", None)
    assert quiet["support"]["states_outside"] == 0 and quiet["support"]["features"] == {}
    # an unmeasured week: too few finite values
    gap = matrix(atr_14_1d_bps=np.full(n, np.nan), ret_1h_bps=np.zeros(n))
    assert window_diagnostics(gap, cal, "ETH-EUR", None)["psi_percentile"]["atr_14_1d_bps"]["percentile"] is None


def test_scope_note_exact() -> None:
    assert scope_note(28) == (
        "feature_drift (PSI-v3): forma dei rendimenti veloci standardizzati per volatilita'. Livello di volatilita' e "
        "28 feature persistenti non testabili in 7 giorni (n_eff_7d < 50): solo diagnostica support."
    )
    assert "3 feature persistenti" in scope_note(3)


def test_run_dir_of_layouts(tmp_path: Path) -> None:
    assert run_dir_of(tmp_path, RULE, "m@1") == tmp_path / "m@1"  # the layout of the run of 2026-10-25
    assert run_dir_of(tmp_path, RULE_V3, "m@1") == tmp_path / "PSI_V3_VOLSTD" / "m@1"


# ------------------------------------------------------------------------------- the watch (conflict P1g)


def write_rejected(folder: Path, *, with_reference: bool = True) -> Path:
    """A PSI-v3 file with decision ``rejected`` as SIM-P1f-bis writes it: ``reference_diagnostic``, ``c_diagnostic`` and
    ``window.d1`` (the out-of-sample false alarms of the rejected run)."""
    write_run(folder, RULE_V3, decision="rejected")
    doc = json.loads((folder / CALIBRATION_FILE).read_text(encoding="utf-8"))
    if with_reference:
        feature = feature_json(np.random.default_rng(5))
        doc["reference_diagnostic"] = {"window_days": 7, "features": {"z_ret_1h_bps": feature}}
        doc["c_diagnostic"] = 0.002265
    doc["window"] = {"d1": {"fwer_oos": {"ETH-EUR": 0.020385}, "max": 0.020385, "ok": False}}
    (folder / CALIBRATION_FILE).write_text(json.dumps(doc), encoding="utf-8")
    return folder


def test_load_calibration_v3_rejected_loads_watch(tmp_path: Path) -> None:
    cal = load_calibration(write_rejected(tmp_path / "rej"), RULE_V3)
    assert cal is not None and cal.decision == "rejected" and not cal.features
    assert set(cal.watch) == {"z_ret_1h_bps"} and cal.watch_c == 0.002265
    assert cal.watch_false_alarm == {"fwer_oos": {"ETH-EUR": 0.020385}, "max": 0.020385, "ok": False}
    adopted = load_calibration(write_run(tmp_path / "ok", RULE_V3), RULE_V3)
    assert adopted is not None and not adopted.watch and adopted.watch_c is None and adopted.watch_false_alarm is None
    v2 = load_calibration(write_run(tmp_path / "v2", RULE), RULE)
    assert v2 is not None and not v2.watch


def test_watch_calibration_none_without_reference(tmp_path: Path) -> None:
    from modules.dryrun.drift import watch_calibration

    bare = load_calibration(write_rejected(tmp_path / "bare", with_reference=False), RULE_V3)
    assert bare is not None and watch_calibration(bare) is None
    adopted = load_calibration(write_run(tmp_path / "ok", RULE_V3), RULE_V3)
    assert adopted is not None and watch_calibration(adopted) is None


def test_watch_stat_equals_adopted_stat(tmp_path: Path) -> None:
    """One function computes the statistic: the watch of a rejected run gives, on the same reference and threshold,
    exactly what an adopted calibration gives."""
    from dataclasses import replace

    from modules.dryrun.drift import watch_calibration

    rng = np.random.default_rng(11)
    cal, _ = synthetic_cal(rng)
    rejected = replace(cal, decision="rejected", window_days=None, c=None, features={}, watch=cal.features,
                       watch_c=cal.c)  # fmt: skip
    watch = watch_calibration(rejected)
    assert watch is not None and watch.window_days == 7 and watch.c == cal.c and watch.features == cal.features
    for shift in (0.0, 3.0):
        window = matrix(ret_1h_bps=rng.normal(size=168) * 20.0 + shift * 20.0, rv_1d_bps=np.full(168, 20.0))
        a, b = window_stat(window, cal, "ETH-EUR"), window_stat(window, watch, "ETH-EUR")
        assert a.S == b.S and a.alarm == b.alarm and a.features == b.features
    assert window_stat(window, watch, "ETH-EUR").alarm  # a shifted week rings
