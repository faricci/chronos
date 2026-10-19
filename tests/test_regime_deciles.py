"""D1.4 regime deciles: pre-holdout window, report columns, and the label_config is never touched."""

from __future__ import annotations

import hashlib
import math
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import training.regime_deciles as rd
from tests.label_fixtures import HOLDOUT_START, loaded_label
from training.feasibility import pooled_n_eff

EMBARGO = timedelta(days=2)
CUT = (HOLDOUT_START - EMBARGO).tz_localize(None)  # naive UTC, as ``t`` in the parquet
RHO = 0.5


def make_samples() -> pd.DataFrame:
    """Hourly rows for 2 products over 6 days around the cut: pctile cycles over 0..99, fills alternate hit/miss."""
    rows = []
    t0 = CUT - pd.Timedelta(days=4)
    for p_i, product in enumerate(["BTC-EUR", "ETH-EUR"]):
        for i in range(6 * 24):
            t = t0 + pd.Timedelta(hours=i)
            k = i * 7 + p_i * 3
            filled = k % 5 != 0
            rows.append(
                {
                    "product": product,
                    "t": t,
                    "position_side": "FLAT",
                    "atr_1d_pctile_365d": float(k % 100) if k % 23 else np.nan,
                    "p_breakeven": 0.40 + 0.01 * (k % 3),
                    "edge_target": float(k % 3 == 0) if filled else np.nan,
                    "w_edge": 0.5 + 0.1 * (k % 4),
                    "net_ret_bps": 100.0 if k % 3 == 0 else -50.0,
                    "label_window_end": t + pd.Timedelta(days=15),
                }
            )
    rows.append({**rows[0], "position_side": "LONG"})  # a LONG row of a filled FLAT sample must never count
    return pd.DataFrame(rows)


def test_window_is_pre_holdout_flat_fills_only() -> None:
    s = make_samples()
    fills = rd.pre_holdout_fills(s, HOLDOUT_START, EMBARGO)
    assert not fills.empty
    assert (fills["t"] < CUT).all()
    assert (fills["position_side"] == "FLAT").all()
    assert fills["edge_target"].notna().all()
    # exclusive of the holdout and of the embargo band before it: nothing at or after the cut, but rows just before it
    assert (s["t"] >= CUT).any() and fills["t"].max() >= CUT - pd.Timedelta(hours=2)
    expected = ((s["position_side"] == "FLAT") & s["edge_target"].notna() & (s["t"] < CUT)).sum()
    assert len(fills) == expected
    # a row exactly at the cut is out (strict inequality)
    edge = s.iloc[[0]].assign(t=CUT, edge_target=1.0)
    assert rd.pre_holdout_fills(edge, HOLDOUT_START, EMBARGO).empty


def test_table_rows_and_formulas() -> None:
    fills = rd.pre_holdout_fills(make_samples(), HOLDOUT_START, EMBARGO)
    rows = rd.deciles_table(fills, RHO)
    names = [r.name for r in rows]
    assert names[-1] == rd.ALL
    assert names[0] == "D1 [0,10)" and names[9] == "D10 [90,100]"
    assert rd.MISSING in names  # the fixture has NaN percentiles
    deciles = [r for r in rows if r.name.startswith("D")]
    assert len(deciles) == 10
    assert sum(r.n for r in rows if r.name != rd.ALL) == len(fills)  # deciles + n/d partition the fills
    allr = rows[-1]
    assert allr.n == len(fills)
    w = fills["w_edge"].to_numpy()
    y = fills["edge_target"].to_numpy()
    assert allr.p_hist == pytest.approx((w * y).sum() / w.sum())
    assert allr.p_breakeven == pytest.approx(float(np.median(fills["p_breakeven"])))
    assert allr.lift == pytest.approx(allr.p_hist - allr.p_breakeven)
    per_product = [float(fills.loc[fills["product"] == p, "w_edge"].sum()) for p in ("BTC-EUR", "ETH-EUR")]
    assert allr.n_eff == pytest.approx(w.sum() / (1 + (2 - 1) * RHO))
    assert allr.n_eff == pytest.approx(pooled_n_eff(per_product, RHO))
    assert allr.se == pytest.approx(math.sqrt(allr.p_hist * (1 - allr.p_hist) / allr.n_eff))
    assert allr.mean_net_ret_bps == pytest.approx(float(fills["net_ret_bps"].mean()))
    # the 100 percentile belongs to the last band, the band edges are half-open
    band = rd.deciles_table(fills.assign(atr_1d_pctile_365d=100.0), RHO)
    assert band[9].n == len(fills) and band[0].n == 0 and math.isnan(band[0].p_hist)


def test_report_columns_and_values() -> None:
    fills = rd.pre_holdout_fills(make_samples(), HOLDOUT_START, EMBARGO)
    rows = rd.deciles_table(fills, RHO)
    text = rd.render_report(rows, Path("run123"), HOLDOUT_START, EMBARGO, RHO, 2, 7)
    header = next(line for line in text.splitlines() if line.startswith("| decile"))
    cols = [c.strip() for c in header.strip("|").split("|")]
    assert cols == ["decile", "n", "p_hist", "p_be", "lift", "lift_0", "N_eff", "SE", "mean net_ret_bps"]
    body = [line for line in text.splitlines() if line.startswith(("| D", "| n/d", "| all"))]
    assert len(body) == len(rows)
    assert body[-1].startswith("| all |")
    assert all(len(line.strip("|").split("|")) == len(cols) for line in body)


def test_main_writes_report_and_leaves_label_config_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = tmp_path / "datasets" / "20260101T000000Z-abcd"
    run.mkdir(parents=True)
    make_samples().to_parquet(run / "samples.parquet", index=False)
    lc = run / "label_config.json"
    lc.write_text('{"stub": true}', encoding="utf-8")
    top_lc = tmp_path / "datasets" / "label_config.json"
    top_lc.write_text('{"stub": "top"}', encoding="utf-8")
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (lc, top_lc)}
    label = loaded_label()
    label.raw["feasibility"] = {"rho_raw": RHO}
    monkeypatch.setattr(rd, "load_label_config", lambda path: label)
    cfg = tmp_path / "train.yml"
    cfg.write_text(
        f"data_root: {(tmp_path / 'data').as_posix()}\n"
        f"datasets_dir: {(tmp_path / 'datasets').as_posix()}\n"
        f"label_config: {top_lc.as_posix()}\n",
        encoding="utf-8",
    )
    assert rd.main(["--config", str(cfg)]) == 0
    report = tmp_path / "data" / "reports" / rd.REPORT_NAME
    text = report.read_text(encoding="utf-8")
    assert "| decile | n | p_hist | p_be | lift | lift_0 | N_eff | SE | mean net_ret_bps |" in text
    assert "rho_raw = 0.5000" in text
    assert before == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (lc, top_lc)}
    assert sorted(p.name for p in (tmp_path / "datasets").iterdir()) == ["20260101T000000Z-abcd", "label_config.json"]
    assert sorted(p.name for p in run.iterdir()) == ["label_config.json", "samples.parquet"]


def test_main_without_pre_holdout_fills_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = tmp_path / "datasets" / "r1"
    run.mkdir(parents=True)
    s = make_samples()
    s["edge_target"] = np.nan
    s.to_parquet(run / "samples.parquet", index=False)
    (run / "label_config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(rd, "load_label_config", lambda path: loaded_label())
    cfg = tmp_path / "train.yml"
    cfg.write_text(f"data_root: {(tmp_path / 'data').as_posix()}\ndatasets_dir: {(tmp_path / 'datasets').as_posix()}\n")
    assert rd.main(["--config", str(cfg)]) == 1
    assert not (tmp_path / "data" / "reports").exists()
