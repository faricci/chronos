"""SIM-P1f (conflict P1f): the decision table, the files of a run, determinism, S0 with the old function and the old
bounds, and the rule applied on synthetic markets. The rule itself is tested in ``test_drift_psi_v2.py``."""

from __future__ import annotations

import hashlib
import itertools
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from modules.dryrun import drift
from modules.dryrun.drift import CALIBRATION_FILE, PREREG_FILE, S6_FILE
from modules.dryrun.learner import population_stability_index
from modules.features.serializer import FLAT_COLUMNS
from script import sim_p1f
from script.sim_p1f import Champion, decide
from tests.indicator_fixtures import synthetic_bars
from tests.label_fixtures import loaded_label
from training.backtest import ProductMarket, build_market

COL = {n: j for j, n in enumerate(FLAT_COLUMNS)}
START = datetime(2024, 1, 1, tzinfo=UTC)
DAYS = 125
HOLDOUT = START + timedelta(days=100)
FAST = ("ret_1h_bps", "rsi_14_1h", "atr_14_1h_bps", "bb_pos_1d", "btc_ret_1d_bps")
NOW = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
LABEL = loaded_label({"BTC-EUR": 3.0, "SOL-EUR": 3.0, "XRP-EUR": 3.0}, holdout_start=pd.Timestamp(HOLDOUT))
DOC = Path(drift.__file__).resolve().parents[2] / "docs" / "decisions" / "2026-10-25-p1f-psi-v2.md"

_CACHE: dict[str, Any] = {}


def markets_of() -> dict[str, ProductMarket]:
    """BTC, SOL (training) and XRP (served, outside the training) over 125 synthetic days; cached (the slow part)."""
    if "m" not in _CACHE:
        btc_bars = synthetic_bars(days=DAYS, seed=61, zero_volume_day=None, constant_days=None)
        btc = build_market("BTC-EUR", btc_bars, LABEL)
        out = {"BTC-EUR": btc}
        for name, seed in (("SOL-EUR", 62), ("XRP-EUR", 63)):
            bars = synthetic_bars(days=DAYS, seed=seed, zero_volume_day=None, constant_days=None)
            out[name] = build_market(name, bars, LABEL, btc.ind)
        _CACHE["m"] = out
    return dict(_CACHE["m"])  # type: ignore[no-any-return]


def champion(version: str = "m@1") -> Champion:
    return Champion(version, ("BTC-EUR", "SOL-EUR"), False, LABEL, HOLDOUT)


def loader(markets: dict[str, ProductMarket], seen: list[Any] | None = None) -> Any:
    def load(label: Any, products: Any) -> dict[str, ProductMarket]:
        if seen is not None:
            seen.append(list(products))
        return {p: markets[p] for p in products}

    return load


@pytest.fixture(autouse=True)
def small_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real blocks are 182 days and the real period of R is two years; the synthetic one is 100 days."""
    monkeypatch.setattr(drift, "OOS_BLOCK_DAYS", 30)


def run_args(tmp_path: Path, name: str = "run", **kw: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "champion": champion(), "served": ["BTC-EUR", "SOL-EUR", "XRP-EUR"], "loader": loader(markets_of()),
        "out_root": tmp_path / name, "decision_doc": DOC, "now": lambda: NOW, "candidates": FAST,
        "commit": ("abc", True),
    }  # fmt: skip
    args.update(kw)
    return args


# ----------------------------------------------------------------------------------------------- decide


def test_decide_table() -> None:
    ok, no = (True, True, True), (True, False, True)
    for d1, d2, d3 in itertools.product((True, False), repeat=3):
        assert decide(True, {7: (d1, d2, d3)})[0] == ("adopted" if d1 and d2 and d3 else "rejected")
    # D0 false: the diagnosis was wrong, whatever the windows say
    assert decide(False, {7: ok, 14: ok}) == ("diagnosis_wrong", None)
    assert decide(False, {}) == ("diagnosis_wrong", None)
    # 7 passes: 7 (the repetition does not exist)
    assert decide(True, {7: ok}) == ("adopted", 7)
    assert decide(True, {7: ok, 14: no}) == ("adopted", 7)
    # 7 fails: the single repetition at 14 days decides
    assert decide(True, {7: no, 14: ok}) == ("adopted", 14)
    assert decide(True, {7: no, 14: no}) == ("rejected", None)
    for failing in itertools.product((True, False), repeat=3):
        if not all(failing):
            assert decide(True, {7: failing, 14: failing}) == ("rejected", None)
    assert decide(True, {7: no}) == ("rejected", None)  # nothing else was tried


def test_the_rule_constants_are_the_decision_document_ones() -> None:
    text = DOC.read_text(encoding="utf-8")
    for needle in ("`QUANTILES = (0.005, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.995)`", "`JEFFREYS = 0.5`",
                   "`ALPHA = 0.01`", "`D1_MAX_FWER = 0.02`", "`D2_MAX_X = 0.25`", "`D3_MIN_POWER = 0.80`",
                   "`OOS_BLOCK_DAYS = 182`", "`D0_MIN_WRONG = 6`", "`MIN_FINITE_FRAC = 0.9`"):  # fmt: skip
        assert needle in text, needle
    assert drift.RULE["OOS_BLOCK_DAYS"] == 182  # the fixture patches the attribute, never the pre-registered RULE


# ------------------------------------------------------------------------------------------ the files


def test_prereg_written_before_data(tmp_path: Path) -> None:
    def broken(label: Any, products: Any) -> Any:
        raise RuntimeError("the store is unreadable")

    with pytest.raises(RuntimeError, match="unreadable"):
        sim_p1f.run(**run_args(tmp_path, loader=broken))
    folder = tmp_path / "run" / "m@1"
    assert (folder / PREREG_FILE).exists() and not (folder / CALIBRATION_FILE).exists()
    pre = json.loads((folder / PREREG_FILE).read_text(encoding="utf-8"))
    assert pre["schema"] == drift.PREREG_SCHEMA and pre["rule"] == drift.rule_json()
    assert pre["decision_doc_sha256"] == hashlib.sha256(DOC.read_bytes()).hexdigest()
    assert pre["champion_version"] == "m@1" and pre["label_config_sha256"] == LABEL.sha256
    assert pre["training_products"] == ["BTC-EUR", "SOL-EUR"] and pre["git_commit"] == "abc"
    assert pre["served_products"] == ["BTC-EUR", "SOL-EUR", "XRP-EUR"] and pre["created_at"] == "2026-10-25T09:00:00Z"
    # an interrupted run (no 10_) may be run again: the pre-registration is rewritten, still before any bar
    seen: list[Any] = []
    assert sim_p1f.run(**run_args(tmp_path, loader=loader(markets_of(), seen))) in (0, 1)
    assert seen and (folder / CALIBRATION_FILE).exists()


def test_the_decision_document_is_the_one_in_the_repository() -> None:
    assert DOC.is_file() and "## Pre-registrazione" in DOC.read_text(encoding="utf-8")


def test_second_run_refused(tmp_path: Path) -> None:
    code = sim_p1f.run(**run_args(tmp_path))
    assert code in (0, 1)
    folder = tmp_path / "run" / "m@1"
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    seen: list[Any] = []
    assert sim_p1f.run(**run_args(tmp_path, loader=loader(markets_of(), seen))) == 2
    assert not seen  # refused before any bar
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before  # nothing rewritten
    other = champion("m@2")  # another version of the champion is a different run
    assert sim_p1f.run(**run_args(tmp_path, champion=other)) in (0, 1)


def test_missing_decision_document_refuses_the_run(tmp_path: Path) -> None:
    assert sim_p1f.run(**run_args(tmp_path, decision_doc=tmp_path / "none.md")) == 2
    assert not (tmp_path / "run" / "m@1").exists() or not (tmp_path / "run" / "m@1" / PREREG_FILE).exists()


def test_run_is_deterministic(tmp_path: Path) -> None:
    sim_p1f.run(**run_args(tmp_path, "a"))
    sim_p1f.run(**run_args(tmp_path, "b"))
    a = json.loads((tmp_path / "a" / "m@1" / CALIBRATION_FILE).read_text(encoding="utf-8"))
    b = json.loads((tmp_path / "b" / "m@1" / CALIBRATION_FILE).read_text(encoding="utf-8"))
    for doc in (a, b):
        doc.pop("created_at")
    assert a == b
    assert (tmp_path / "a" / "m@1" / PREREG_FILE).read_bytes() == (tmp_path / "b" / "m@1" / PREREG_FILE).read_bytes()


def test_document_structure_and_decision(tmp_path: Path) -> None:
    code = sim_p1f.run(**run_args(tmp_path))
    doc = json.loads((tmp_path / "run" / "m@1" / CALIBRATION_FILE).read_text(encoding="utf-8"))
    assert doc["schema"] == drift.CALIBRATION_SCHEMA and doc["rule"] == drift.rule_json()
    assert doc["decision"] in ("adopted", "rejected", "diagnosis_wrong")
    assert (code == 0) == (doc["decision"] == "adopted")
    assert doc["preregistration_sha256"] == hashlib.sha256(
        (tmp_path / "run" / "m@1" / PREREG_FILE).read_bytes()
    ).hexdigest()
    assert doc["s0"]["d0"]["count"] <= 11 and doc["s1"]["features"] == [f for f in FAST if f in doc["s1"]["features"]]
    assert set(doc["s1"]["btc_missing_rows"]) == {"BTC-EUR", "SOL-EUR"}
    assert all(v == 0 for v in doc["s1"]["btc_missing_rows"].values())  # A8: the batch path never lacks a BTC row
    if doc["s0"]["d0"]["passed"]:
        w7 = doc["windows"]["7"]
        assert {"d1", "d2", "d3", "x_f", "c", "s3_blocks", "s4_power", "s5_outside_training"} <= set(w7)
        assert set(w7["s5_outside_training"]) == {"XRP-EUR"}
    else:
        assert doc["decision"] == "diagnosis_wrong" and doc["windows"] == {} and doc["reference"] is None
    if doc["decision"] == "adopted":
        cal = drift.load_calibration(tmp_path / "run" / "m@1")
        assert cal is not None and cal.decision == "adopted" and cal.window_days in (7, 14)
        assert set(cal.features) == set(doc["reference"]["features"]) and cal.c == doc["c"]


# --------------------------------------------------------------------------------------------- S0


def test_s0_uses_old_function_and_old_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    """S0 calls ``population_stability_index`` itself, with the states at the MINUTE of the day against the HOURLY
    states of ``[day + 1 - 90 d, day)`` (89 days) of the same products."""
    calls: list[tuple[int, int]] = []
    real = population_stability_index

    def spy(live: np.ndarray, hist: np.ndarray, bins: int = 10) -> float | None:
        calls.append((live.size, hist.size))
        return real(live, hist, bins)

    monkeypatch.setattr(sim_p1f, "population_stability_index", spy)
    markets = {p: m for p, m in markets_of().items() if p != "XRP-EUR"}
    hourly = {p: sim_p1f.hourly_of(m, LABEL) for p, m in markets.items()}
    study = sim_p1f.Study(hourly, champion(), ["BTC-EUR", "SOL-EUR"], FAST)
    out = sim_p1f.s0_old_rule(markets, study, hourly, LABEL)
    one_day = out["one_day"]
    assert out["old_function"] == {"bins": 10, "floor": 1e-4, "history_days": 89, "alert": 0.10}
    first = calls[0]
    assert first[0] == 2 * 1440 or first[0] == 1440  # the states of the day at the minute, products pooled
    # a day with a full 89 days of history: pooled hourly history of both products
    full = [c for c in calls if c[1] == 2 * 89 * 24]
    assert full and all(c[0] == 2 * 1440 for c in full)
    assert set(one_day["per_feature"]) == set(FLAT_COLUMNS)  # all the columns, as the old code
    days = np.arange(study.r_start.astype("datetime64[D]"), study.r_end.astype("datetime64[D]"))
    assert one_day["windows"] == len(days)
    assert out["seven_days_vs_r"]["step_days"] == 7 and out["seven_days_vs_r"]["windows"] > 0
    d0 = out["d0"]
    assert d0["min_wrong"] == 6 and d0["passed"] == (d0["count"] < 6) and set(d0["slow_medians"]) == set(drift.D0_SLOW)


# ---------------------------------------------------------------------------------------------- study


def test_reference_period_and_applicability() -> None:
    markets = {p: m for p, m in markets_of().items() if p != "XRP-EUR"}
    hourly = {p: sim_p1f.hourly_of(m, LABEL) for p, m in markets.items()}
    study = sim_p1f.Study(hourly, champion(), ["BTC-EUR", "SOL-EUR"], FAST)
    # R: from the first state with every applicable feature finite, up to (excluding) the holdout
    for p in study.training:
        ts = hourly[p].ts[study.r_rows[p]]
        assert ts.min() == study.t0[p] and ts.max() < np.datetime64(HOLDOUT.replace(tzinfo=None), "ns")
    assert "btc_ret_1d_bps" in study.features
    assert set(study.tau["btc_ret_1d_bps"]) == {"SOL-EUR"}  # not applicable to BTC: no tau, no null from BTC
    w = study.windows(7)
    assert set(w.product) == {"BTC-EUR", "SOL-EUR"} and (w.hi - w.lo == 168).all()
    last_start = np.datetime64(HOLDOUT.replace(tzinfo=None), "D").astype(np.int64) - 7
    assert w.start.max() == last_start  # the last window ends exactly at the holdout
    psi = study.psi_matrix(w)
    j = study.features.index("btc_ret_1d_bps")
    btc_rows = [i for i, p in enumerate(w.product) if p == "BTC-EUR"]
    assert np.isnan(psi[btc_rows, j]).all() and np.isfinite(psi[:, 0]).all()


def test_btc_missing_rows_are_counted() -> None:
    markets = {p: m for p, m in markets_of().items() if p != "XRP-EUR"}
    hourly = {p: sim_p1f.hourly_of(m, LABEL) for p, m in markets.items()}
    hourly["SOL-EUR"].btc_missing[50:60] = True
    study = sim_p1f.Study(hourly, champion(), ["BTC-EUR", "SOL-EUR"], FAST)
    doc = sim_p1f.s1_doc(study)
    assert doc["btc_missing_rows"]["BTC-EUR"] == 0
    assert doc["btc_missing_rows"]["SOL-EUR"] == int(hourly["SOL-EUR"].btc_missing[study.r_rows["SOL-EUR"]].sum())


# ---------------------------------------------------------------------------------------------- s6


def adopted_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A run folder whose decision is ``adopted`` (the decision itself is forced: it is the files that matter here)."""
    real = sim_p1f.decide
    monkeypatch.setattr(sim_p1f, "decide", lambda d0, results: real(True, {7: (True, True, True)}))
    sim_p1f.run(**run_args(tmp_path, "adopted"))
    return tmp_path / "adopted" / "m@1"


def test_s6_refused_without_calibration(tmp_path: Path) -> None:
    assert sim_p1f.run_s6(tmp_path / "none", ["BTC-EUR"], loader(markets_of()), LABEL, now=lambda: NOW) == 2
    (tmp_path / "pre").mkdir()
    (tmp_path / "pre" / PREREG_FILE).write_text("{}", encoding="utf-8")  # only the pre-registration: the run died
    assert sim_p1f.run_s6(tmp_path / "pre", ["BTC-EUR"], loader(markets_of()), LABEL, now=lambda: NOW) == 2
    assert not (tmp_path / "pre" / S6_FILE).exists()


def test_s6_records_calibration_sha(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = adopted_run(tmp_path, monkeypatch)
    cal = drift.load_calibration(folder)
    assert cal is not None and cal.decision == "adopted" and cal.window_days == 7
    end = date(2024, 4, 9)  # inside the synthetic data: the last 7 complete UTC days end on the 9th
    code = sim_p1f.run_s6(
        folder, ["BTC-EUR", "SOL-EUR", "XRP-EUR"], loader(markets_of()), LABEL, end=end, now=lambda: NOW
    )
    assert code == 0
    s6 = json.loads((folder / S6_FILE).read_text(encoding="utf-8"))
    assert s6["schema"] == drift.S6_SCHEMA and s6["calibration_sha256"] == cal.sha256
    assert s6["window_days"] == 7 and s6["diagnostic_only"] is False
    assert s6["window"] == {"start": "2024-04-03T00:00:00Z", "end": "2024-04-10T00:00:00Z"}
    for product, block in s6["products"].items():
        assert block["status"] == "measured" and block["states"] == 168, product
    # ONE function: L0 and S6 give the same numbers on the same window
    pm = markets_of()["SOL-EUR"]
    x, _ = drift.hourly_states(pm, LABEL, datetime(2024, 4, 3, tzinfo=UTC), datetime(2024, 4, 10, tzinfo=UTC))
    assert drift.window_stat(x, cal, "SOL-EUR").S == s6["products"]["SOL-EUR"]["S"]
    # written once
    assert sim_p1f.run_s6(folder, ["BTC-EUR"], loader(markets_of()), LABEL, end=end, now=lambda: NOW) == 2


def test_s6_incomplete_window_is_unmeasured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = adopted_run(tmp_path, monkeypatch)
    code = sim_p1f.run_s6(folder, ["SOL-EUR"], loader(markets_of()), LABEL, end=date(2024, 1, 3), now=lambda: NOW)
    assert code == 0
    s6 = json.loads((folder / S6_FILE).read_text(encoding="utf-8"))
    block = s6["products"]["SOL-EUR"]
    assert block["status"] == "unmeasured" and block["reason"] == "incomplete_window" and s6["alarm"] is False


def test_s6_refuses_a_tampered_preregistration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = adopted_run(tmp_path, monkeypatch)
    (folder / PREREG_FILE).write_text("{}", encoding="utf-8")
    assert sim_p1f.run_s6(folder, ["SOL-EUR"], loader(markets_of()), LABEL, end=date(2024, 4, 9), now=lambda: NOW) == 2


def test_s6_of_a_rejected_calibration_is_diagnostic_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sim_p1f, "decide", lambda d0, results: ("rejected", None))
    real_calibrate = sim_p1f.calibrate

    def forced(markets: Any, champion: Champion, served: Any, **kw: Any) -> dict[str, Any]:
        body = real_calibrate(markets, champion, served, **kw)
        if "7" not in body["windows"]:
            pytest.skip("D0 did not pass on this synthetic data")
        return body

    monkeypatch.setattr(sim_p1f, "calibrate", forced)
    sim_p1f.run(**run_args(tmp_path, "rej"))
    folder = tmp_path / "rej" / "m@1"
    doc = json.loads((folder / CALIBRATION_FILE).read_text(encoding="utf-8"))
    assert doc["decision"] == "rejected" and doc["window_days"] is None and doc["reference"] is None
    assert doc["reference_diagnostic"]["window_days"] == 7
    assert sim_p1f.run_s6(folder, ["SOL-EUR"], loader(markets_of()), LABEL, end=date(2024, 4, 9), now=lambda: NOW) == 0
    s6 = json.loads((folder / S6_FILE).read_text(encoding="utf-8"))
    assert s6["diagnostic_only"] is True and s6["window_days"] == 7 and s6["decision"] == "rejected"
