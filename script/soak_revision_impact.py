"""Impact of the REST candle revisions seen in the 48h soak on what the model sees (read-only).

For each product: STORED bars (what the live system had) vs FRESH bars (stored with the minutes re-read from REST now).
Both go through the same indicator code and the same flat state tokens; we count the states whose tokens differ and
how much the model's answers move.
"""

import json
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from modules.decision.baseline import BaselineDecider
from modules.decision.questions import build_questions
from modules.features.serializer import FLAT_COLUMNS
from modules.features.state_matrix import flat_matrix
from modules.market.backfill import load_bars
from modules.market.indicators_batch import compute_indicators
from modules.risk.label_config import load_label_config

BASE = "https://api.coinbase.com/api/v3/brokerage/market"
START = datetime(2026, 10, 18, 7, 50, tzinfo=UTC)
END = datetime(2026, 10, 20, 5, 40, tzinfo=UTC)
label = load_label_config(Path("data/datasets/label_config.json"))
questions = build_questions(label.cfg.horizon_min, label.cfg.k_tp, label.cfg.k_sl, label.cfg.atr_tf)
model_dir = Path("models/baseline/baseline-lgbm@2026-10-18.2")
decider = BaselineDecider.load(model_dir, label=label, questions=questions, subset="final", allow_degenerate=False)


def fetch(pid, a, b):
    url = f"{BASE}/products/{pid}/candles?start={int(a.timestamp())}&end={int(b.timestamp())}&granularity=ONE_MINUTE"
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "chronos-research"})
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read()).get("candles", [])
        except Exception:  # noqa: BLE001
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("fetch")


def fresh_window(pid):
    rows, t = [], START
    while t < END:
        t2 = min(t + timedelta(minutes=299), END)
        rows += fetch(pid, t, t2)
        t = t2 + timedelta(minutes=1)
        time.sleep(0.25)
    f = pd.DataFrame(rows)
    f["ts"] = pd.to_datetime(f["start"].astype(int), unit="s", utc=True)
    f = f.drop_duplicates("ts").set_index("ts").sort_index()
    for c in ("open", "high", "low", "close", "volume"):
        f[c] = f[c].astype(float)
    return f[["open", "high", "low", "close", "volume"]]


stored, fresh_bars, inds = {}, {}, {}
for pid in ("BTC-EUR", "ETH-EUR", "SOL-EUR"):
    s = load_bars(Path("data/bars/1m"), pid)
    w = fresh_window(pid)
    f = s.copy()
    common = f.index.intersection(w.index)
    common = common[(common >= START) & (common < END)]
    f.loc[common, ["open", "high", "low", "close", "volume"]] = w.loc[common].to_numpy()
    stored[pid], fresh_bars[pid] = s, f
    print(pid, "bars", len(s), "minutes replaced from REST", len(common), flush=True)
    inds[pid] = (compute_indicators(s), compute_indicators(f))

report = {}
for pid in ("BTC-EUR", "ETH-EUR", "SOL-EUR"):
    ind_s, ind_f = inds[pid]
    idx = ind_s.index
    rows = np.flatnonzero((idx >= START + timedelta(hours=1)) & (idx < END))
    btc_s, btc_f = inds["BTC-EUR"]
    if pid.startswith("BTC-"):
        xs = flat_matrix(ind_s, rows, pid, label, None, None)
        xf = flat_matrix(ind_f, rows, pid, label, None, None)
    else:
        br = btc_s.index.get_indexer(idx)[rows]
        xs = flat_matrix(ind_s, rows, pid, label, btc_s, br)
        xf = flat_matrix(ind_f, rows, pid, label, btc_f, br)
    same = (xs == xf) | (np.isnan(xs) & np.isnan(xf))
    row_diff = ~same.all(axis=1)
    cols = {FLAT_COLUMNS[j]: int((~same[:, j]).sum()) for j in range(same.shape[1]) if (~same[:, j]).any()}
    maxabs = {c: float(np.nanmax(np.abs(xs[:, FLAT_COLUMNS.index(c)] - xf[:, FLAT_COLUMNS.index(c)]))) for c in cols}
    a_s, a_f = decider.answers_batch(xs), decider.answers_batch(xf)
    pbuy = lambda a: a.p_action[:, 0] / np.maximum(a.p_action[:, 0] + a.p_action[:, 2], 1e-12)  # noqa: E731
    d_buy = np.abs(pbuy(a_s) - pbuy(a_f))
    d_edge = np.abs(a_s.p_edge - a_f.p_edge)
    report[pid] = {
        "states": int(len(rows)),
        "states_with_any_token_diff": int(row_diff.sum()),
        "share": float(row_diff.mean()),
        "columns_with_diff": cols,
        "max_abs_diff_by_column": maxabs,
        "model": {
            "max_dp_buy": float(d_buy.max()),
            "p99_dp_buy": float(np.quantile(d_buy, 0.99)),
            "max_dp_edge": float(d_edge.max()),
            "p99_dp_edge": float(np.quantile(d_edge, 0.99)),
            "states_dp_buy_over_0.01": int((d_buy > 0.01).sum()),
            "states_dp_edge_over_0.01": int((d_edge > 0.01).sum()),
            "states_dp_edge_over_0.02": int((d_edge > 0.02).sum()),
            "states_dp_edge_over_0.005": int((d_edge > 0.005).sum()),
            "states_dp_buy_over_0.05": int((d_buy > 0.05).sum()),
        },
    }
    print(pid, json.dumps(report[pid])[:1400], flush=True)
json.dump(report, open("data/reports/soak_revision_impact.json", "w"), indent=1)
