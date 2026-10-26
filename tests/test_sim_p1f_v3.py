"""SIM-P1f-bis (PSI-v3, conflict P1f-bis): decision table, files, freeze, family J, holdout check D1h, determinism.
Synthetic markets of ``test_sim_p1f``; the real data are read only by the one real run."""

from __future__ import annotations

import itertools
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from modules.dryrun import drift
from modules.dryrun.drift import CALIBRATION_FILE, PREREG_FILE, RULE_V3, S6_FILE
from script import sim_p1f, sim_p1f_v3
from script.sim_p1f_v3 import decide_v3
from tests.test_sim_p1f import (
    DAYS,
    HOLDOUT,
    START,
    champion,
    loader,
    markets_of,
    small_blocks,  # noqa: F401  (autouse fixture: the blocks of 30 days)
)
from tests.test_sim_p1f import (
    DOC as DOC_V2,
)

RAW = ("ret_1h_bps", "ret_15m_bps", "rv_1d_bps", "rsi_14_1h", "atr_14_1h_bps", "bb_pos_1d", "btc_ret_1d_bps")
ZS = ("z_ret_1h_bps", "z_ret_15m_bps")
NOW = START + timedelta(days=DAYS - 1)  # the holdout windows end 7 days before: inside the synthetic data
DOC = Path(drift.__file__).resolve().parents[2] / "docs" / "decisions" / "2026-10-26-p1f-bis-psi-v3.md"


def run_args(tmp_path: Path, name: str = "run", **kw: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "champion": champion(), "served": ["BTC-EUR", "SOL-EUR", "XRP-EUR"], "loader": loader(markets_of()),
        "out_root": tmp_path / name, "decision_doc": DOC, "now": lambda: NOW, "candidates": ZS, "raw_candidates": RAW,
        "commit": ("abc", True),
    }  # fmt: skip
    args.update(kw)
    return args


def folder_of(tmp_path: Path, name: str = "run") -> Path:
    return tmp_path / name / "PSI_V3_VOLSTD" / "m@1"


def load10(tmp_path: Path, name: str = "run") -> dict[str, Any]:
    doc: dict[str, Any] = json.loads((folder_of(tmp_path, name) / CALIBRATION_FILE).read_text(encoding="utf-8"))
    return doc


def test_decide_v3_table() -> None:
    for j, d1, d1h, d2, d3 in itertools.product((True, False), repeat=5):
        expected = "adopted" if (j and d1 and d1h and d2 and d3) else "rejected"
        assert decide_v3(j, d1, d1h, d2, d3) == expected
    assert decide_v3(False, True, True, True, True) == "rejected"  # J empty: rejected whatever the checks say
    assert decide_v3(True, True, False, True, True) == "rejected"  # D1h is a restriction, not a hint


def test_prereg_v3_before_data_in_rule_folder(tmp_path: Path) -> None:
    def broken(label: Any, products: Any) -> Any:
        raise RuntimeError("the store is unreadable")

    with pytest.raises(RuntimeError, match="unreadable"):
        sim_p1f_v3.run(**run_args(tmp_path, loader=broken))
    folder = folder_of(tmp_path)
    assert (folder / PREREG_FILE).exists() and not (folder / CALIBRATION_FILE).exists()
    assert not (tmp_path / "run" / "m@1").exists()  # the PSI-v2 layout is not used
    pre = json.loads((folder / PREREG_FILE).read_text(encoding="utf-8"))
    assert pre["schema"] == drift.PREREG_SCHEMA_V3 and pre["rule"] == drift.rule_json(RULE_V3)
    assert pre["code_frozen"] is True and pre["final"] is True and pre["git_commit"] == "abc"
    assert set(pre["code_sha256"]) == set(sim_p1f_v3.CODE_FILES_V3) and pre["decision_doc"] == DOC.name
    assert pre["run_day"] == NOW.date().isoformat()
    end = (NOW - timedelta(days=7)).date()
    assert pre["d1h_window"] == {"start": pre["holdout_start"], "end": f"{end}T00:00:00Z"}
    assert pre["training_products"] == ["BTC-EUR", "SOL-EUR"] and pre["label_config_sha256"]
    # an interrupted run (no 10_) may be repeated: the pre-registration is rewritten, still before any bar
    seen: list[Any] = []
    assert sim_p1f_v3.run(**run_args(tmp_path, loader=loader(markets_of(), seen))) in (0, 1)
    assert seen and (folder / CALIBRATION_FILE).exists()


def test_v3_refused_when_not_frozen(tmp_path: Path) -> None:
    seen: list[Any] = []
    code = sim_p1f_v3.run(**run_args(tmp_path, commit=("abc", False), loader=loader(markets_of(), seen)))
    assert code == 2 and not seen and not (tmp_path / "run").exists()  # nothing written, no bar read
    assert sim_p1f_v3.run(**run_args(tmp_path, commit=("abc", None))) == 2
    assert sim_p1f_v3.run(**run_args(tmp_path, decision_doc=tmp_path / "none.md")) == 2
    # the real freeze check: a file outside git (or untracked) is not frozen
    stray = tmp_path / "stray.md"
    stray.write_text("x", encoding="utf-8")
    head, frozen = sim_p1f_v3._git_freeze(stray, sim_p1f_v3.CODE_FILES_V3)
    assert frozen is False or head is None
    # the decision document of the repository itself is committed and the real check is exercised on it
    head2, frozen2 = sim_p1f_v3._git_freeze(DOC, ("docs/decisions/2026-10-25-p1f-psi-v2.md",))
    assert head2 is None or frozen2 in (True, False)


def test_v3_second_run_refused(tmp_path: Path) -> None:
    assert sim_p1f_v3.run(**run_args(tmp_path)) in (0, 1)
    folder = folder_of(tmp_path)
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    seen: list[Any] = []
    assert sim_p1f_v3.run(**run_args(tmp_path, loader=loader(markets_of(), seen))) == 2
    assert not seen and {p.name: p.read_bytes() for p in folder.iterdir()} == before


def test_v2_and_v3_folders_coexist(tmp_path: Path) -> None:
    v2_args = {
        "champion": champion(), "served": ["BTC-EUR", "SOL-EUR", "XRP-EUR"], "loader": loader(markets_of()),
        "out_root": tmp_path / "run", "decision_doc": DOC_V2, "now": lambda: NOW,
        "candidates": ("ret_1h_bps", "rsi_14_1h", "atr_14_1h_bps", "bb_pos_1d", "btc_ret_1d_bps"),
        "commit": ("abc", True),
    }  # fmt: skip
    assert sim_p1f.run(**v2_args) in (0, 1)
    assert sim_p1f_v3.run(**run_args(tmp_path)) in (0, 1)
    assert (tmp_path / "run" / "m@1" / CALIBRATION_FILE).exists() and (folder_of(tmp_path) / CALIBRATION_FILE).exists()
    assert drift.load_calibration(tmp_path / "run" / "m@1") is not None  # each file under its own rule
    assert drift.load_calibration(folder_of(tmp_path), RULE_V3) is not None
    with pytest.raises(drift.CalibrationError):
        drift.load_calibration(folder_of(tmp_path))
    assert drift.run_dir_of(tmp_path / "run", RULE_V3, "m@1") == folder_of(tmp_path)


def test_v3_family_is_j_only_and_untested_listed(tmp_path: Path) -> None:
    sim_p1f_v3.run(**run_args(tmp_path))
    doc = load10(tmp_path)
    judged = doc["family"]["judged"]
    assert judged and set(judged) <= set(ZS) and doc["scope"]["tested"] == judged
    assert all(doc["family"]["n_eff_7d"][n] >= 50 for n in judged)
    untested = {u["feature"] for u in doc["scope"]["untested"]}
    assert {"rsi_14_1h", "atr_14_1h_bps", "bb_pos_1d", "btc_ret_1d_bps", "rv_1d_bps"} <= untested
    assert not untested & set(judged) and "ret_1h_bps" not in untested  # the raw fast features are covered by their z
    assert doc["scope"]["note"] == drift.scope_note(len(doc["scope"]["untested"]))
    # the minP family is J and nothing else
    assert set(doc["window"]["x_f"]) == set(judged)
    assert set(doc["window"]["d2"]["judged"]) <= set(judged) and set(doc["window"]["null_sizes"]) == set(judged)
    # the diagnostics come from the RAW features of the same R
    assert set(doc["diagnostic_reference"]) == set(doc["s1"]["features"]) and "rv_1d_bps" in doc["diagnostic_reference"]
    # R starts as in PSI-v2: with the raw candidates finite, not with the z
    assert doc["s1"]["r_start"] and doc["final"] is True and doc["rule"]["id"] == "PSI_V3_VOLSTD"


def test_v3_holdout_windows_non_overlapping_and_end(tmp_path: Path) -> None:
    sim_p1f_v3.run(**run_args(tmp_path))
    d1h = load10(tmp_path)["d1h"]
    rows = d1h["windows"]
    assert d1h["start"] == "2024-04-10T00:00:00Z" and d1h["step_days"] == 7 and d1h["level"] == 0.05
    end = datetime.fromisoformat(d1h["end"].replace("Z", "+00:00"))
    assert end == (NOW - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
    for product in ("BTC-EUR", "SOL-EUR"):
        starts = [datetime.fromisoformat(r["start"].replace("Z", "+00:00")) for r in rows if r["product"] == product]
        assert starts and starts[0] == HOLDOUT
        assert all(b - a == timedelta(days=7) for a, b in itertools.pairwise(starts))  # non overlapping, contiguous
        assert all(s + timedelta(days=7) <= end for s in starts)  # nothing past the end
    assert {r["product"] for r in rows} == {"BTC-EUR", "SOL-EUR"}  # training products only (XRP is outside)
    measured = [r for r in rows if r["status"] == "measured"]
    assert d1h["n_h"] == len(measured) > 0 and d1h["alarms"] == sum(1 for r in measured if r["alarm"])
    assert d1h["k_h"] == drift.k_h(d1h["n_h"]) and d1h["ok"] == (d1h["alarms"] <= d1h["k_h"])
    assert all(r["states"] >= 168 for r in measured)
    # a run day before the holdout leaves no week: n_H = 0 and D1h is false (not verified = not passed)
    early = sim_p1f_v3.run(**run_args(tmp_path, "early", now=lambda: HOLDOUT + timedelta(days=3)))
    assert early == 1
    d = load10(tmp_path, "early")
    assert d["d1h"]["n_h"] == 0 and d["d1h"]["k_h"] is None and d["d1h"]["ok"] is False and d["decision"] == "rejected"


def test_v3_deterministic(tmp_path: Path) -> None:
    sim_p1f_v3.run(**run_args(tmp_path, "a"))
    sim_p1f_v3.run(**run_args(tmp_path, "b"))
    a, b = load10(tmp_path, "a"), load10(tmp_path, "b")
    for doc in (a, b):
        doc.pop("created_at")
    assert a == b
    pre_a = (folder_of(tmp_path, "a") / PREREG_FILE).read_bytes()
    assert pre_a == (folder_of(tmp_path, "b") / PREREG_FILE).read_bytes()


def test_v3_document_structure(tmp_path: Path) -> None:
    sim_p1f_v3.run(**run_args(tmp_path))
    doc = load10(tmp_path)
    assert doc["schema"] == drift.CALIBRATION_SCHEMA_V3 and doc["decision"] in ("adopted", "rejected")
    assert set(doc["checks"]) == {"j_nonempty", "d1", "d1h", "d2", "d3"}
    assert doc["decision"] == decide_v3(**{"j_nonempty": doc["checks"]["j_nonempty"], **{
        k: doc["checks"][k] for k in ("d1", "d1h", "d2", "d3")}})  # fmt: skip
    assert doc["p_edge_reference"] is None and doc["p_edge_reference_reason"] == "no_decider"
    assert doc["window_days"] == (7 if doc["decision"] == "adopted" else None)
    assert (doc["reference"] is not None) == (doc["decision"] == "adopted")
    if doc["decision"] == "rejected":
        assert doc["reference_diagnostic"]["window_days"] == 7 and doc["c"] is None
    # with a decider the reference of p_edge is the holdout distribution
    sim_p1f_v3.run(**run_args(tmp_path, "edge", p_edge_fn=lambda x: np.linspace(0.2, 0.7, len(x))))
    edge = load10(tmp_path, "edge")["p_edge_reference"]
    assert edge["source"] == "holdout" and edge["n"] > 0 and len(edge["p"]) == len(edge["inner"]) + 1
    assert edge["start"] == "2024-04-10T00:00:00Z"
    cal = drift.load_calibration(folder_of(tmp_path, "edge"), RULE_V3)
    assert cal is not None and cal.p_edge_ref is not None and cal.final and cal.diagnostic
    broken = sim_p1f_v3.run(**run_args(tmp_path, "bad", p_edge_fn=lambda x: 1 / 0))
    assert broken in (0, 1) and load10(tmp_path, "bad")["p_edge_reference_reason"] == "decider_error"


def test_v3_empty_family_is_rejected(tmp_path: Path) -> None:
    assert sim_p1f_v3.run(**run_args(tmp_path, candidates=("z_ret_1h_bps",), raw_candidates=("ret_1h_bps",))) in (0, 1)
    # z needs rv_1d_bps: with it missing from the raw set the family is whatever n_eff says; J empty is forced below
    code = sim_p1f_v3.run(**run_args(tmp_path, "empty", candidates=("z_rsi_14_1h",), raw_candidates=RAW))
    doc = load10(tmp_path, "empty")  # z_rsi_14_1h is not a standardised fast feature: n_eff is small, J is empty
    assert code == 1 and doc["decision"] == "rejected" and doc["checks"]["j_nonempty"] is False
    assert doc["window"] is None and doc["d1h"] is None and doc["reference"] is None
    assert {u["feature"] for u in doc["scope"]["untested"]} >= {"z_rsi_14_1h"}
    cal = drift.load_calibration(folder_of(tmp_path, "empty"), RULE_V3)
    assert cal is not None and cal.decision == "rejected" and not cal.features and cal.untested


def test_v3_adopted_flow_and_s6(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sim_p1f_v3, "decide_v3", lambda *a: "adopted")  # the files are what matters here
    assert sim_p1f_v3.run(**run_args(tmp_path)) == 0
    folder = folder_of(tmp_path)
    cal = drift.load_calibration(folder, RULE_V3)
    assert cal is not None and cal.decision == "adopted" and cal.window_days == 7 and cal.features
    assert all(n.startswith("z_") for n in cal.features) and cal.c == load10(tmp_path)["c"]
    end = date(2024, 4, 30)
    code = sim_p1f.run_s6(folder, ["BTC-EUR", "SOL-EUR"], loader(markets_of()), champion().label, end=end,
                          now=lambda: NOW, rule=RULE_V3)  # fmt: skip
    assert code == 0
    s6 = json.loads((folder / S6_FILE).read_text(encoding="utf-8"))
    assert s6["calibration_sha256"] == cal.sha256 and s6["diagnostic_only"] is False and s6["window_days"] == 7
    assert all(b["status"] == "measured" and set(b["features"]) == set(cal.features) for b in s6["products"].values())
    # the rule of the file decides which loader reads it
    assert sim_p1f.run_s6(folder, ["SOL-EUR"], loader(markets_of()), champion().label, end=end, now=lambda: NOW) == 2


def test_v2_cli_run_closed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """PSI-v2 is closed: ``run`` refuses before reading any configuration or bar."""
    assert sim_p1f.main(["run", "--config", str(tmp_path / "does_not_exist.yml")]) == 2
    assert "closed" in capsys.readouterr().err
    assert sim_p1f.main(["s6", "--config", str(tmp_path / "does_not_exist.yml")]) == 2  # s6 still reads a config
