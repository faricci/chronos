"""L0 of the nightly learner, conflict P1f (docs/decisions/2026-10-25-p1f-psi-v2.md): the BTC context of the alt states,
the feature drift (PSI-v2) against the calibration of SIM-P1f, and the old daily PSI as a diagnostic only."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from modules.decision.baseline import AnswerArrays
from modules.dryrun import drift
from modules.dryrun.drift import CALIBRATION_FILE, CALIBRATION_SCHEMA, PREREG_FILE, inner_edges, ref_shares
from modules.dryrun.learner import LearnerPaths, classify_btc_row
from modules.dryrun.revision_impact import flat_pair_shift
from modules.features.serializer import FLAT_COLUMNS
from modules.risk.label_config import load_label_config
from tests.indicator_fixtures import synthetic_bars
from tests.test_audit_labels import register
from tests.test_learner import LABEL, Loader, Rig, live_state_records
from tests.test_revision_impact import LABEL as IMPACT_LABEL
from tests.test_revision_impact import RISK, ColumnDecider, entry_rows, policy
from training.backtest import build_market

COL = {n: j for j, n in enumerate(FLAT_COLUMNS)}
DAY = date(2024, 1, 20)  # the audited day of the rigs; the run date is the day after
CHAMPION = "baseline-lgbm@2026-10-24.1"
FEATS = ("ret_1h_bps", "rsi_14_1h", "atr_14_1h_bps")


class BtcSensitive:
    """A model that reads the BTC context: it moves when the state loses ``btc_ret_1d_bps``."""

    def answers_batch(self, x: np.ndarray) -> AnswerArrays:
        z = np.nan_to_num(x[:, COL["btc_ret_1d_bps"]], nan=0.0) / 50.0
        return AnswerArrays(np.tile([0.2, 0.1, 0.7], (len(x), 1)), 1.0 / (1.0 + np.exp(-z)), np.zeros((len(x), 5)))


def markets_and_bars() -> tuple[dict[str, Any], Any, Any, int]:
    from tests.indicator_fixtures import synthetic_bars as bars_of

    sol = bars_of(days=25, seed=41, zero_volume_day=None, constant_days=None)
    btc = bars_of(days=25, seed=42, zero_volume_day=None, constant_days=None)
    m_btc = build_market("BTC-EUR", btc, LABEL)
    m_sol = build_market("SOL-EUR", sol, LABEL, m_btc.ind)
    first = (datetime(2024, 1, 20, tzinfo=UTC) - sol.index[0].to_pydatetime()).days * 1440
    return {"SOL-EUR": m_sol, "BTC-EUR": m_btc}, sol, btc, first


def with_version(recs: list[dict[str, Any]], version: str = "v1") -> list[dict[str, Any]]:
    for r in recs:
        r["policy"] = {"policy_version": version, "final": "HOLD"}
    return recs


def btc_records(edit: dict[str, int] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """280 SOL and 280 BTC FLAT records of the audited day, as the engine would log them. ``edit`` (a count per
    kind) damages the first SOL records: stale (BTC bar missing, flagged), old (the same without the flag),
    inconsistent (flagged stale but with the values), mismatch (finite and different)."""
    markets, sol, btc, first = markets_and_bars()
    rows = range(first + 30, first + 1430, 5)
    ok = live_state_records("SOL-EUR", sol, markets["SOL-EUR"].ind, markets["BTC-EUR"].ind, rows)
    lost = live_state_records("SOL-EUR", sol, markets["SOL-EUR"].ind, None, rows)  # the state without the BTC context
    recs = with_version(ok + live_state_records("BTC-EUR", btc, markets["BTC-EUR"].ind, None, rows))
    for r in recs[:280]:
        r["btc_context"] = "aligned"
    edit = edit or {}
    i = 0
    for _ in range(edit.get("stale", 0)):
        recs[i] = {**with_version([lost[i]])[0], "btc_context": "stale"}
        i += 1
    for _ in range(edit.get("old", 0)):
        recs[i] = with_version([lost[i]])[0]  # a record written before P1f: no btc_context key at all
        i += 1
    for _ in range(edit.get("inconsistent", 0)):
        recs[i]["btc_context"] = "stale"
        i += 1
    for _ in range(edit.get("mismatch", 0)):
        doc = json.loads(recs[i]["state"])
        doc["btc_context"]["ret_1d_bps"] += 7
        recs[i]["state"] = json.dumps(doc)
        i += 1
    return recs, markets


def btc_rig(
    tmp_path: Path, edit: dict[str, int] | None = None, mutate: Any = None, **kw: Any
) -> tuple[Rig, dict[str, Any]]:
    recs, markets = btc_records(edit)
    if mutate is not None:
        mutate(recs)
    rig = Rig(tmp_path)
    rig.paths.decisions_dir.mkdir(parents=True)
    path = rig.paths.decisions_dir / f"{DAY.isoformat()}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    rig.kw.update(markets=lambda label: markets, products=["SOL-EUR", "BTC-EUR"], **kw)
    return rig, markets


def l0(rig: Rig, day: date = date(2024, 1, 21)) -> dict[str, Any]:
    doc = rig.learner().run(day)
    return doc["steps"]["L0"]  # type: ignore[no-any-return]


# ------------------------------------------------------------------------------------------- PSI v1 diagnostic


def test_l0_psi_v1_is_diagnostic_only(tmp_path: Path) -> None:
    rig, _ = btc_rig(tmp_path)
    step = l0(rig)
    sp = step["details"]["state_parity"]
    assert step["outcome"] == "ok"
    v1 = sp["psi_v1"]
    assert v1["status"] == "diagnostic" and v1["rule"] == "PSI_V1_DAILY_90D"
    assert v1["max_feature_psi"] is not None and v1["worst_feature"] in FLAT_COLUMNS
    assert set(v1["psi"]) == set(FLAT_COLUMNS) and v1["alert_threshold"] == 0.25
    assert v1["above_0_10"] == sum(1 for v in v1["psi"].values() if v is not None and v > 0.10)
    over = [a["feature"] for a in v1["alerts"]]
    assert all(v1["psi"][f] > 0.25 for f in over)
    assert all(v <= 0.25 for k, v in v1["psi"].items() if v is not None and k not in over)
    # the top-level keys of the old schema are gone: nobody can read the diagnostic as a verdict
    assert not {"psi", "max_feature_psi", "worst_feature", "drift_alerts"} & set(sp)


# ----------------------------------------------------------------------------------------- BTC context


@pytest.mark.parametrize(
    ("flag", "live", "batch", "expected"),
    [
        (None, [np.nan, np.nan], [10.0, 1.2], ("live_missing", True)),
        ("aligned", [np.nan, np.nan], [10.0, 1.2], ("flag_inconsistent", False)),
        ("stale", [np.nan, np.nan], [10.0, 1.2], ("live_missing", True)),
        ("stale", [np.nan, np.nan], [np.nan, np.nan], ("batch_missing", True)),
        ("stale", [10.0, 1.2], [10.0, 1.2], ("flag_inconsistent", False)),
        (None, [10.0, 1.2], [np.nan, np.nan], ("batch_missing", True)),
        ("aligned", [10.0, 1.2], [np.nan, np.nan], ("batch_missing", True)),
        ("aligned", [10.0, 1.2], [17.0, 1.2], ("value_mismatch", False)),
        (None, [10.0, 1.2], [10.0, 1.2], (None, False)),
        ("aligned", [np.nan, np.nan], [np.nan, np.nan], (None, False)),  # a BTC history too young: both sides
        ("aligned", [10.0, np.nan], [10.0, np.nan], (None, False)),  # a token missing on both sides
    ],
)
def test_classify_btc_row_table(flag: str | None, live: list[float], batch: list[float], expected: Any) -> None:
    assert classify_btc_row(flag, np.array(live), np.array(batch)) == expected


def test_l0_btc_context_classified_and_excluded_from_match(tmp_path: Path) -> None:
    rig, _ = btc_rig(tmp_path, {"stale": 10, "old": 5, "inconsistent": 2, "mismatch": 1})
    sp = l0(rig)["details"]["state_parity"]
    block = sp["btc_context"]["products"]
    assert set(block) == {"SOL-EUR"}  # BTC itself has no BTC context
    sol = block["SOL-EUR"]
    assert sol["alt_states"] == 280 and sol["misaligned"] == 15 and sol["share"] == pytest.approx(15 / 280)
    assert sol["classes"] == {"live_missing": 15, "batch_missing": 0, "value_mismatch": 1, "flag_inconsistent": 2}
    assert sol["shift_status"] == "no_deciders" and sol["max_shift"] is None and sol["flips"] is None
    par = sp["state_parity"]
    assert par["rows"] == 560
    # the 15 misaligned rows differ in the btc tokens: in ``match_all``, not in ``match``; the value_mismatch row
    # (a defect) stays in both
    assert par["match_all"] == pytest.approx(1 - 16 / 560)
    assert par["match"] == pytest.approx(1 - 1 / (560 - 15))
    assert set(par["mismatch_by_feature"]) == {"btc_ret_1d_bps"} and par["worst_feature"] == "btc_ret_1d_bps"


def test_l0_btc_context_clean_day_is_a_measured_zero(tmp_path: Path) -> None:
    rig, _ = btc_rig(tmp_path)
    sp = l0(rig)["details"]["state_parity"]
    sol = sp["btc_context"]["products"]["SOL-EUR"]
    assert sol["misaligned"] == 0 and sol["share"] == 0.0 and sol["shift_status"] == "measured"
    assert sol["max_shift"] == 0.0 and sol["flips"] == 0
    assert sp["state_parity"]["match"] == sp["state_parity"]["match_all"] == 1.0


def test_l0_btc_shift_is_measured_with_the_model_of_the_decision(tmp_path: Path) -> None:
    rig, _ = btc_rig(tmp_path, {"stale": 10})
    loader = Loader({"v1": BtcSensitive()})
    rig.kw["deciders"] = loader
    sol = l0(rig)["details"]["state_parity"]["btc_context"]["products"]["SOL-EUR"]
    assert loader.asked and set(loader.asked) == {"v1"}  # the version that took the decision
    # no tuned policy: the shift is measured, the flips are not
    assert sol["shift_status"] == "policy_untunable" and sol["flips"] is None
    assert sol["max_shift"] is not None and sol["max_shift"] > 0.01


def test_l0_btc_shift_with_a_policy_counts_flips(tmp_path: Path) -> None:
    class WithPolicy(Loader):
        def __call__(self, version: str) -> tuple[Any, Any]:
            return self.deciders[version], policy(label_config_sha256=LABEL.sha256)

    rig, _ = btc_rig(tmp_path, {"stale": 10})
    rig.kw["deciders"] = WithPolicy({"v1": BtcSensitive()})
    sol = l0(rig)["details"]["state_parity"]["btc_context"]["products"]["SOL-EUR"]
    assert sol["shift_status"] == "measured" and isinstance(sol["flips"], int) and sol["flips"] >= 0
    assert sol["flips_basis"] == "computed" and sol["untunable_check"] is None
    assert sol["max_shift"] is not None and sol["max_shift"] > 0.01


def test_l0_btc_shift_decider_without_batch(tmp_path: Path) -> None:
    rig, _ = btc_rig(tmp_path, {"stale": 3})
    rig.kw["deciders"] = Loader({"v1": object()})
    sol = l0(rig)["details"]["state_parity"]["btc_context"]["products"]["SOL-EUR"]
    assert sol["shift_status"] == "decider_without_batch" and sol["max_shift"] is None and sol["flips"] is None


def register_policy(rig: Rig, version: str, policy: dict[str, Any] | None) -> None:
    """Register ``version`` in the rig's registry with a ``final/policy.json`` (None: no file at all)."""
    from modules.dryrun.registry import artifacts_from_dir
    from tests.test_learner import make_version

    model = make_version(rig.tmp / "extra", version)
    if policy is not None:
        (model / "final" / "policy.json").write_text(json.dumps(policy), encoding="utf-8")
    rig.registry.register(artifacts_from_dir(model), {"version": version})


UNTUNABLE = {"untunable": True, "reason": "untunable_ev"}


def untunable_block(tmp_path: Path, mutate: Any = None, policy: dict[str, Any] | None = UNTUNABLE) -> dict[str, Any]:
    rig, _ = btc_rig(tmp_path, {"stale": 10}, mutate)
    register_policy(rig, "v1", policy)
    rig.kw["deciders"] = Loader({"v1": BtcSensitive()})  # the loader gives policy None for BOTH untunable and missing
    block: dict[str, Any] = l0(rig)["details"]["state_parity"]["btc_context"]["products"]["SOL-EUR"]
    return block


def test_btc_shift_untunable_flips_zero_with_basis(tmp_path: Path) -> None:
    """An explicitly untunable policy never buys: the flips are exactly 0, with their basis written down."""

    def benchmark_buys(recs: list[dict[str, Any]]) -> None:  # the benchmark books are not the model
        recs.append({**recs[0], "book": "random_entry", "policy": {"policy_version": "v1", "final": "BUY"},
                     "order_intent_id": "i1", "risk": {"outcome": "intent"}})  # fmt: skip

    sol = untunable_block(tmp_path, benchmark_buys)
    assert sol["shift_status"] == "policy_untunable" and sol["flips"] == 0 and sol["flips_basis"] == "policy_untunable"
    clean = {"policy": "untunable", "buy_records": 0, "intent_records": 0}
    assert sol["untunable_check"] == {"versions": {"v1": clean}}
    assert sol["max_shift"] is not None and sol["max_shift"] > 0.01  # the shift is measured as before


def test_btc_shift_untunable_with_buy_record_is_violation(tmp_path: Path) -> None:
    def one_buy(recs: list[dict[str, Any]]) -> None:
        recs[100]["policy"]["final"] = "BUY"

    sol = untunable_block(tmp_path, one_buy)
    assert sol["flips"] is None and sol["flips_basis"] is None
    assert sol["untunable_check"]["versions"]["v1"] == {"policy": "untunable", "buy_records": 1, "intent_records": 0}

    def one_intent(recs: list[dict[str, Any]]) -> None:
        recs[100]["order_intent_id"] = "x"

    sol2 = untunable_block(tmp_path / "i", one_intent)
    assert sol2["flips"] is None and sol2["untunable_check"]["versions"]["v1"]["intent_records"] == 1


def test_btc_shift_untunable_with_stale_hold_is_violation(tmp_path: Path) -> None:
    """A BUY that the engine turned into HOLD ``btc_context_stale`` is still a BUY of the model."""

    def stale_hold(recs: list[dict[str, Any]]) -> None:
        recs[100]["policy"].update(final="HOLD", reason="btc_context_stale")

    sol = untunable_block(tmp_path, stale_hold)
    assert sol["flips"] is None and sol["untunable_check"]["versions"]["v1"]["buy_records"] == 1


def test_btc_shift_missing_policy_stays_unmeasured(tmp_path: Path) -> None:
    sol = untunable_block(tmp_path, policy=None)  # no policy.json: nothing is proven
    assert sol["shift_status"] == "policy_untunable" and sol["flips"] is None and sol["flips_basis"] is None
    assert sol["untunable_check"]["versions"]["v1"]["policy"] == "missing"
    unregistered, _ = btc_rig(tmp_path / "u", {"stale": 10})
    unregistered.kw["deciders"] = Loader({"v1": BtcSensitive()})  # the version is not even in the registry
    block = l0(unregistered)["details"]["state_parity"]["btc_context"]["products"]["SOL-EUR"]
    assert block["flips"] is None and block["untunable_check"]["versions"]["v1"]["policy"] == "missing"


def test_flat_pair_shift_counts_shift_and_flips() -> None:
    x_a, x_b = entry_rows(3), entry_rows(3)
    x_a[:, 0], x_b[:, 0] = [0.48, 0.48, 0.40], [0.45, 0.48, 0.40]  # row 0 crosses the breakeven (0.4681)
    m, flips = flat_pair_shift(ColumnDecider(), policy(), IMPACT_LABEL, RISK, x_a, x_b)
    assert m.tolist() == pytest.approx([0.03, 0.0, 0.0]) and flips == 1
    m2, none = flat_pair_shift(ColumnDecider(), None, IMPACT_LABEL, RISK, x_a, x_b)
    assert none is None and m2.tolist() == pytest.approx([0.03, 0.0, 0.0])
    x_nan = x_b.copy()
    x_nan[0, 0] = np.nan
    m3, _ = flat_pair_shift(ColumnDecider(), None, IMPACT_LABEL, RISK, x_a, x_nan)
    assert math.isinf(m3[0])  # a NaN answer on one side only is +inf (the learner writes it as 1.0)


# ------------------------------------------------------------------------------------------- feature drift


def calibration_matrix(markets: dict[str, Any], label: Any, shift: float = 0.0) -> np.ndarray:
    """The hourly states of the window of the rigs ([2024-01-14, 2024-01-21)) of SOL-EUR, optionally pushed away."""
    x, _ = drift.hourly_states(
        markets["SOL-EUR"], label, datetime(2024, 1, 14, tzinfo=UTC), datetime(2024, 1, 21, tzinfo=UTC)
    )
    out = x.copy()
    for name in FEATS:
        out[:, COL[name]] = x[:, COL[name]] + shift * np.nanstd(x[:, COL[name]])
    return out


def write_calibration(
    folder: Path, label_sha: str, reference: np.ndarray, *, decision: str = "adopted", window_days: int = 7,
    c: float = 0.01,
) -> Path:
    """A ``10_calibration.json`` as SIM-P1f writes it, for three fast features. Null: 100 values from 0.02 to 1.0 (a
    PSI of the reference window against itself is ~0: p = 1; a PSI of several units is above all of them:
    p = 1/101 < c = 0.01)."""
    folder.mkdir(parents=True)
    (folder / PREREG_FILE).write_text(json.dumps({"schema": drift.PREREG_SCHEMA}), encoding="utf-8")
    sha = hashlib.sha256((folder / PREREG_FILE).read_bytes()).hexdigest()
    feats: dict[str, Any] = {}
    for name in FEATS:
        col = reference[:, COL[name]]
        inner = inner_edges(col)
        assert inner is not None
        finite = col[np.isfinite(col)]
        feats[name] = {
            "inner": inner.tolist(), "p": ref_shares(col, inner).tolist(), "r_min": float(finite.min()),
            "r_max": float(finite.max()), "sigma": float(finite.std()),
            "null_sorted": np.linspace(0.02, 1.0, 100).tolist(), "x_f": 0.9, "n_eff_7d": 40.0,
        }  # fmt: skip
    adopted = decision == "adopted"
    doc = {
        "schema": CALIBRATION_SCHEMA, "rule": drift.rule_json(), "champion_version": CHAMPION,
        "label_config_sha256": label_sha, "decision": decision, "window_days": window_days if adopted else None,
        "alpha": 0.01, "c": c if adopted else None, "preregistration_sha256": sha,
        "reference": {"window_days": window_days, "features": feats} if adopted else None,
    }  # fmt: skip
    (folder / CALIBRATION_FILE).write_text(json.dumps(doc), encoding="utf-8")
    return folder


def drift_rig(
    tmp_path: Path, *, shift: float = 0.0, decision: str = "adopted", label_sha: str | None = None,
    calibrate: bool = True,
) -> tuple[Rig, Path]:
    """A rig whose champion has a real label_config, a drift folder, and (optionally) the calibration of the champion.
    No decisions are needed: feature drift reads only the bars."""
    markets, _, _, _ = markets_and_bars()
    rig = Rig(tmp_path)
    ext = tmp_path / "pool_label.json"
    from tests.test_audit_labels import write_label

    write_label(ext, LABEL)
    register(rig, CHAMPION, load_label_config(ext), "champion")
    champion_label = load_label_config(rig.registry.get("champion").path / "label_config.json")
    drift_dir = tmp_path / "sim_p1f"
    if calibrate:
        write_calibration(
            drift_dir / CHAMPION, label_sha or champion_label.sha256,
            calibration_matrix(markets, champion_label, shift), decision=decision,
        )
    rig.paths = LearnerPaths(**{**rig.paths.__dict__, "drift_dir": drift_dir})
    alone = {"SOL-EUR": markets["SOL-EUR"]}  # the reference is SOL's own window: another series would ring
    rig.kw.update(markets=lambda label: alone, products=["SOL-EUR"])
    return rig, drift_dir


def test_l0_feature_drift_skipped_without_calibration(tmp_path: Path) -> None:
    rig, _ = drift_rig(tmp_path, calibrate=False)
    step = l0(rig)
    assert step["details"]["feature_drift"] == {"skipped": "no_calibration"}
    # no drift folder configured at all, and no real champion label
    plain = Rig(tmp_path / "plain")
    plain.kw.update(markets=lambda label: markets_and_bars()[0], products=["SOL-EUR"])
    assert l0(plain)["details"]["feature_drift"] == {"skipped": "no_drift_dir"}
    fallback = Rig(tmp_path / "fb")
    fallback.paths = LearnerPaths(**{**fallback.paths.__dict__, "drift_dir": tmp_path / "x"})
    fallback.kw.update(markets=lambda label: markets_and_bars()[0], products=["SOL-EUR"])
    assert l0(fallback)["details"]["feature_drift"] == {"skipped": "no_champion"}


def test_l0_feature_drift_pass_and_alarm(tmp_path: Path) -> None:
    rig, _ = drift_rig(tmp_path / "quiet")
    fd = l0(rig)["details"]["feature_drift"]
    assert fd["rule"] == "PSI_V2" and fd["status"] == "measured" and fd["alarm"] is False and fd["reason"] is None
    sol = fd["products"]["SOL-EUR"]
    assert sol["status"] == "measured" and sol["states"] == 168 and sol["alarm"] is False and sol["flagged"] == []
    assert sol["S"] == pytest.approx(1.0) and set(sol["features"]) == set(FEATS)
    one = sol["features"]["ret_1h_bps"]
    assert one["n_finite"] == 168 and one["n_eff_7d"] == 40.0 and one["x_f"] == 0.9
    assert 0.0 <= one["out_of_range_share"] <= 1.0 and one["psi"] < 0.1 and sol["unmeasured_features"] == []
    cal = fd["calibration"]
    assert cal["window_days"] == 7 and cal["alpha"] == 0.01 and cal["c"] == 0.01 and len(cal["sha256"]) == 64
    assert cal["champion_version"] == CHAMPION and cal["run_dir"].endswith(f"sim_p1f/{CHAMPION}")
    # a reference far away from the live window: every fast feature is in its tail
    loud, _ = drift_rig(tmp_path / "loud", shift=40.0)
    bad = l0(loud)["details"]["feature_drift"]
    assert bad["status"] == "measured" and bad["alarm"] is True
    flagged = {f["feature"] for f in bad["products"]["SOL-EUR"]["flagged"]}
    assert flagged == set(FEATS) and bad["products"]["SOL-EUR"]["alarm"] is True
    assert bad["products"]["SOL-EUR"]["S"] == pytest.approx(1 / 101)
    json.dumps(bad, allow_nan=False)


def test_l0_feature_drift_rejected_calibration_unmeasured(tmp_path: Path) -> None:
    rig, _ = drift_rig(tmp_path, decision="rejected")
    fd = l0(rig)["details"]["feature_drift"]
    assert fd["status"] == "unmeasured" and fd["reason"] == "calibration_rejected" and "products" not in fd
    assert fd["calibration"]["window_days"] is None
    other, _ = drift_rig(tmp_path / "other", decision="diagnosis_wrong")
    assert l0(other)["details"]["feature_drift"]["reason"] == "calibration_diagnosis_wrong"


def test_l0_feature_drift_label_mismatch_is_unmeasured(tmp_path: Path) -> None:
    rig, _ = drift_rig(tmp_path, label_sha="0" * 64)
    fd = l0(rig)["details"]["feature_drift"]
    assert fd["status"] == "unmeasured" and fd["reason"] == "calibration_label_mismatch"


def test_l0_feature_drift_window_ends_at_cutoff(tmp_path: Path) -> None:
    rig, _ = drift_rig(tmp_path)
    fd = l0(rig)["details"]["feature_drift"]
    assert fd["window"] == {"start": "2024-01-14T00:00:00Z", "end": "2024-01-21T00:00:00Z"}
    assert {p: b["states"] for p, b in fd["products"].items()} == {"SOL-EUR": 168}
    # the bars go on for days after the cutoff and none of them is used
    later = l0(rig, date(2024, 1, 22))["details"]["feature_drift"]
    assert later["window"] == {"start": "2024-01-15T00:00:00Z", "end": "2024-01-22T00:00:00Z"}


def test_l0_feature_drift_incomplete_window(tmp_path: Path) -> None:
    rig, _ = drift_rig(tmp_path)
    fd = l0(rig, date(2024, 1, 4))["details"]["feature_drift"]  # the bars start on 2024-01-01: < 7 days
    assert fd["status"] == "unmeasured" and fd["reason"] == "incomplete_window" and fd["alarm"] is None
    for block in fd["products"].values():
        assert block["status"] == "unmeasured" and block["reason"] == "incomplete_window" and block["states"] < 168


def test_l0_feature_drift_tampered_preregistration_fails_the_part(tmp_path: Path) -> None:
    rig, drift_dir = drift_rig(tmp_path)
    (drift_dir / CHAMPION / PREREG_FILE).write_text("{}", encoding="utf-8")
    step = l0(rig)
    assert step["outcome"] == "failed" and "feature_drift: CalibrationError" in step["message"]
    assert "changed after the run" in step["message"]


def test_synthetic_bars_cover_the_windows() -> None:
    bars = synthetic_bars(days=25, seed=41, zero_volume_day=None, constant_days=None)
    assert bars.index[0] <= datetime(2024, 1, 1, 1, tzinfo=UTC) and bars.index[-1] >= datetime(2024, 1, 24, tzinfo=UTC)


# --------------------------------------------------------------------------- feature drift, PSI-v3 (P1f-bis)


def write_calibration_v3(
    folder: Path, label_sha: str, reference: np.ndarray, *, decision: str = "adopted", c: float = 0.01,
    watch_ref: bool = True,
) -> Path:
    """A PSI-v3 ``10_calibration.json``: one standardised feature in the test, three raw ones as diagnostics (two of
    them not covered by the test), and the distribution of p_edge on the holdout."""
    folder.mkdir(parents=True)
    (folder / PREREG_FILE).write_text(json.dumps({"schema": drift.PREREG_SCHEMA_V3}), encoding="utf-8")
    sha = hashlib.sha256((folder / PREREG_FILE).read_bytes()).hexdigest()

    def feature(values: np.ndarray, n_eff: float) -> dict[str, Any]:
        inner = inner_edges(values)
        assert inner is not None
        finite = values[np.isfinite(values)]
        return {
            "inner": inner.tolist(), "p": ref_shares(values, inner).tolist(), "r_min": float(finite.min()),
            "r_max": float(finite.max()), "sigma": float(finite.std()),
            "null_sorted": np.linspace(0.02, 1.0, 100).tolist(), "x_f": 0.9, "n_eff_7d": n_eff,
        }  # fmt: skip

    adopted = decision == "adopted"
    z = feature(drift.feature_values(reference, "z_ret_1h_bps"), 168.0)
    edge = np.linspace(0.2, 0.7, 500)
    edge_inner = inner_edges(edge)
    assert edge_inner is not None
    doc = {
        "schema": drift.CALIBRATION_SCHEMA_V3, "rule": drift.rule_json(drift.RULE_V3), "champion_version": CHAMPION,
        "label_config_sha256": label_sha, "decision": decision, "window_days": 7 if adopted else None, "alpha": 0.01,
        "c": c if adopted else None, "preregistration_sha256": sha, "final": True,
        "reference": {"window_days": 7, "features": {"z_ret_1h_bps": z}} if adopted else None,
        "diagnostic_reference": {n: feature(reference[:, COL[n]], e) for n, e in
                                 (("ret_1h_bps", 168.0), ("rsi_14_1h", 6.2), ("atr_14_1h_bps", 1.2))},
        "scope": {"tested": ["z_ret_1h_bps"], "untested": [{"feature": "rsi_14_1h", "n_eff_7d": 6.2},
                                                           {"feature": "atr_14_1h_bps", "n_eff_7d": 1.2}]},
        "p_edge_reference": {"inner": edge_inner.tolist(), "p": ref_shares(edge, edge_inner).tolist(),
                             "r_min": 0.2, "r_max": 0.7},
    }  # fmt: skip
    if not adopted and watch_ref:  # what a rejected run leaves for the watch
        doc["reference_diagnostic"] = {"window_days": 7, "features": {"z_ret_1h_bps": z}}
        doc["c_diagnostic"] = c
        doc["window"] = {"d1": {"fwer_oos": {"SOL-EUR": 0.0204}, "max": 0.0204, "ok": False}}
    (folder / CALIBRATION_FILE).write_text(json.dumps(doc), encoding="utf-8")
    return folder


def v3_rig(
    tmp_path: Path, *, decision: str = "adopted", shift: float = 0.0, with_v2: bool = False, watch_ref: bool = True
) -> Rig:
    rig, drift_dir = drift_rig(tmp_path, calibrate=with_v2)
    markets, _, _, _ = markets_and_bars()
    label = load_label_config(rig.registry.get("champion").path / "label_config.json")
    ref = calibration_matrix(markets, label, shift)
    run_dir = drift.run_dir_of(drift_dir, drift.RULE_V3, CHAMPION)
    write_calibration_v3(run_dir, label.sha256, ref, decision=decision, watch_ref=watch_ref)
    return rig


def test_l0_feature_drift_prefers_v3_folder(tmp_path: Path) -> None:
    rig = v3_rig(tmp_path, with_v2=True)  # both folders: the v3 one wins
    fd = l0(rig)["details"]["feature_drift"]
    assert fd["rule"] == "PSI_V3_VOLSTD" and fd["calibration"]["run_dir"].endswith(f"PSI_V3_VOLSTD/{CHAMPION}")
    assert fd["calibration"]["decision"] == "adopted" and fd["calibration"]["final"] is True
    # without the v3 folder the v2 one is still read, unchanged
    only_v2, _ = drift_rig(tmp_path / "v2")
    old = l0(only_v2)["details"]["feature_drift"]
    assert old["rule"] == "PSI_V2" and old["status"] == "measured" and "scope" not in old


def test_l0_feature_drift_v2_fallback_unchanged(tmp_path: Path) -> None:
    rig, _ = drift_rig(tmp_path, decision="rejected")
    fd = l0(rig)["details"]["feature_drift"]
    assert fd["rule"] == "PSI_V2" and fd["status"] == "unmeasured" and fd["reason"] == "calibration_rejected"


def test_l0_feature_drift_v3_scope_and_support_alarm(tmp_path: Path) -> None:
    rig = v3_rig(tmp_path)
    rig.kw["deciders"] = Loader({CHAMPION: BtcSensitive()})
    fd = l0(rig)["details"]["feature_drift"]
    assert fd["status"] == "measured" and fd["alarm"] is False and fd["support_alarm"] == []
    assert fd["scope"]["tested"] == ["z_ret_1h_bps"] and len(fd["scope"]["untested"]) == 2
    assert fd["scope"]["note"] == drift.scope_note(2)
    sol = fd["products"]["SOL-EUR"]
    assert sol["status"] == "measured" and sol["states"] == 168 and set(sol["features"]) == {"z_ret_1h_bps"}
    d = sol["diagnostics"]
    assert d["support"]["states"] == 168 and d["support"]["share"] == 0.0
    assert set(d["psi_percentile"]) == {"rsi_14_1h", "atr_14_1h_bps"}
    assert d["psi_percentile"]["atr_14_1h_bps"]["n_eff_7d"] == 1.2
    assert d["p_edge_psi"]["status"] == "measured" and d["p_edge_psi"]["reference"] == "holdout"
    json.dumps(fd, allow_nan=False)
    # the same night without a decider: the diagnostic is unmeasured, nothing else changes
    plain = v3_rig(tmp_path / "plain")
    sol2 = l0(plain)["details"]["feature_drift"]["products"]["SOL-EUR"]
    assert sol2["diagnostics"]["p_edge_psi"] == {"status": "unmeasured", "reason": "no_deciders"}
    # a state outside the range of the training: support alarm (an alert, never a gate)
    path = drift.run_dir_of(tmp_path / "sim_p1f", drift.RULE_V3, CHAMPION) / CALIBRATION_FILE
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["diagnostic_reference"]["ret_1h_bps"]["r_max"] = -1e9
    path.write_text(json.dumps(doc), encoding="utf-8")
    again = l0(rig, date(2024, 1, 22))["details"]["feature_drift"]
    assert again["support_alarm"] == ["SOL-EUR"] and again["products"]["SOL-EUR"]["diagnostics"]["support"]["share"] > 0


def test_l0_feature_drift_v3_rejected_is_not_testable_with_diagnostics(tmp_path: Path) -> None:
    rig = v3_rig(tmp_path, decision="rejected")
    rig.kw["deciders"] = Loader({CHAMPION: BtcSensitive()})
    fd = l0(rig)["details"]["feature_drift"]
    assert fd["status"] == "not_testable" and fd["reason"] == "calibration_rejected_final" and fd["alarm"] is None
    assert fd["calibration"]["decision"] == "rejected" and fd["calibration"]["window_days"] is None
    assert fd["scope"]["tested"] == [] and fd["scope"]["note"] == drift.scope_note(2)
    sol = fd["products"]["SOL-EUR"]
    assert sol["status"] == "not_testable" and "S" not in sol and sol["states"] == 168  # no test is applied
    assert sol["diagnostics"]["support"]["states"] == 168 and sol["diagnostics"]["p_edge_psi"]["status"] == "measured"
    short = l0(rig, date(2024, 1, 4))["details"]["feature_drift"]  # an incomplete week is not a diagnostic week
    assert short["products"]["SOL-EUR"]["status"] == "unmeasured"
    mismatch = v3_rig(tmp_path / "m")
    path = drift.run_dir_of(tmp_path / "m" / "sim_p1f", drift.RULE_V3, CHAMPION) / CALIBRATION_FILE
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["label_config_sha256"] = "0" * 64
    path.write_text(json.dumps(doc), encoding="utf-8")
    assert l0(mismatch)["details"]["feature_drift"]["reason"] == "calibration_label_mismatch"


def test_l0_v3_rejected_has_watch_per_product(tmp_path: Path) -> None:
    rig = v3_rig(tmp_path, decision="rejected")  # the watch reference is the window itself: quiet
    fd = l0(rig)["details"]["feature_drift"]
    assert fd["status"] == "not_testable" and fd["alarm"] is None and fd["watch_alarm"] == []
    assert fd["watch"] == {
        "rule": "PSI_V3_VOLSTD", "reference": "reference_diagnostic", "c": 0.01, "gate": False,
        "false_alarm_oos": {"fwer_oos": {"SOL-EUR": 0.0204}, "max": 0.0204, "ok": False},
    }
    sol = fd["products"]["SOL-EUR"]
    assert sol["status"] == "not_testable" and "S" not in sol and "diagnostics" in sol  # block unchanged ...
    assert sol["watch"]["status"] == "measured" and sol["watch"]["alarm"] is False  # ... plus the watch
    assert sol["watch"]["S"] == pytest.approx(1.0) and sol["watch"]["flagged"] == []
    json.dumps(fd, allow_nan=False)
    adopted = l0(v3_rig(tmp_path / "ok"))["details"]["feature_drift"]  # an adopted run has no watch
    assert "watch" not in adopted and "watch" not in adopted["products"]["SOL-EUR"]


def test_l0_v3_rejected_watch_alarm_listed(tmp_path: Path) -> None:
    rig = v3_rig(tmp_path, decision="rejected", shift=40.0)  # the reference of the rejected run is far from the week
    fd = l0(rig)["details"]["feature_drift"]
    assert fd["watch_alarm"] == ["SOL-EUR"] and fd["status"] == "not_testable" and fd["alarm"] is None
    watch = fd["products"]["SOL-EUR"]["watch"]
    assert watch["alarm"] is True and watch["S"] == pytest.approx(1 / 101)
    assert [f["feature"] for f in watch["flagged"]] == ["z_ret_1h_bps"]


def test_l0_v3_rejected_without_reference_diagnostic_watch_unmeasured(tmp_path: Path) -> None:
    rig = v3_rig(tmp_path, decision="rejected", watch_ref=False)
    fd = l0(rig)["details"]["feature_drift"]
    assert fd["products"]["SOL-EUR"]["watch"] == {"status": "unmeasured", "reason": "no_diagnostic_reference"}
    assert fd["watch_alarm"] == [] and fd["watch"]["c"] is None and fd["watch"]["false_alarm_oos"] is None
