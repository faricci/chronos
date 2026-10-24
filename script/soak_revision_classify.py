"""Classify the REST revisions of the 48h soak window: stored minute vs the same minute read from REST (read-only)."""

import json
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from modules.market.backfill import load_bars

BASE = "https://api.coinbase.com/api/v3/brokerage/market"
START = datetime(2026, 10, 18, 7, 50, tzinfo=UTC)  # a few minutes after the soak restart
END = datetime(2026, 10, 20, 5, 40, tzinfo=UTC)


def fetch(pid: str, a: datetime, b: datetime) -> list[dict]:
    url = f"{BASE}/products/{pid}/candles?start={int(a.timestamp())}&end={int(b.timestamp())}&granularity=ONE_MINUTE"
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "chronos-research"})
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read()).get("candles", [])
        except Exception:  # noqa: BLE001
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"fetch failed {pid} {a}")


out = {}
for pid in ("BTC-EUR", "ETH-EUR", "SOL-EUR"):
    rows = []
    t = START
    while t < END:
        t2 = min(t + timedelta(minutes=299), END)
        rows += fetch(pid, t, t2)
        t = t2 + timedelta(minutes=1)
        time.sleep(0.25)
    fresh = pd.DataFrame(rows)
    fresh["ts"] = pd.to_datetime(fresh["start"].astype(int), unit="s", utc=True)
    fresh = fresh.drop_duplicates("ts").set_index("ts").sort_index()
    for c in ("open", "high", "low", "close", "volume"):
        fresh[c] = fresh[c].astype(float)
    stored = load_bars(Path("data/bars/1m"), pid, start=START, end=END)
    stored = stored[(stored.index >= START) & (stored.index < END)]
    both = stored.index.intersection(fresh.index)
    only_stored = stored.index.difference(fresh.index)
    only_fresh = fresh.index.difference(stored.index)
    diff_cols = {
        c: int(
            ((stored.loc[both, c] - fresh.loc[both, c]).abs() > 1e-9 * stored.loc[both, c].abs().clip(lower=1)).sum()
        )
        for c in ("open", "high", "low", "close", "volume")
    }
    any_diff = (
        (
            stored.loc[both, ["open", "high", "low", "close", "volume"]]
            - fresh.loc[both, ["open", "high", "low", "close", "volume"]]
        ).abs()
        > 1e-9
    ).any(axis=1)
    bad = both[any_diff.to_numpy()]
    hours = pd.Series(bad.hour).value_counts().sort_index().to_dict()
    days = pd.Series(bad.strftime("%m-%d")).value_counts().sort_index().to_dict()
    vol_only = (
        int(
            (
                ~(
                    stored.loc[bad, ["open", "high", "low", "close"]].to_numpy()
                    != fresh.loc[bad, ["open", "high", "low", "close"]].to_numpy()
                ).any(axis=1)
            ).sum()
        )
        if len(bad)
        else 0
    )
    flat_stored = int((stored.loc[bad, "volume"] == 0).sum()) if len(bad) else 0
    out[pid] = {
        "minutes_compared": len(both),
        "stored_only": len(only_stored),
        "fresh_only": len(only_fresh),
        "revised_minutes": len(bad),
        "ratio": len(bad) / max(len(both), 1),
        "fields_differing": diff_cols,
        "volume_only_revisions": vol_only,
        "stored_flat_then_traded": flat_stored,
        "by_day": days,
        "first_bad": str(bad.min()) if len(bad) else None,
        "last_bad": str(bad.max()) if len(bad) else None,
        "max_close_diff_bps": float(((stored.loc[bad, "close"] / fresh.loc[bad, "close"] - 1).abs() * 1e4).max())
        if len(bad)
        else 0.0,
    }
    out[pid]["only_fresh_minutes"] = [str(x) for x in only_fresh]
    out[pid]["only_stored_first_last"] = [str(only_stored.min()), str(only_stored.max())] if len(only_stored) else None
    print(pid, json.dumps(out[pid]), flush=True)
json.dump(out, open("data/reports/soak_revisions_classified.json", "w"), indent=1)
