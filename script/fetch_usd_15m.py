"""Download of public Coinbase 15-minute USD candles (research data only, no calculation on returns).

Purpose: raw input for hypotheses C and D of docs/decisions/2026-10-28-ipotesi-strategie-laboratorio.md (sez. 5, arm U).
This script only downloads and checks data quality (gaps, duplicates, zero-volume candles, agreement with the daily
candles already in the repo). It never computes returns and never runs a backtest.

Usage:
    python -u -m script.fetch_usd_15m fetch --products BTC-USD ETH-USD
    python -u -m script.fetch_usd_15m check --products BTC-USD ETH-USD     (no network, recomputes the manifest)

Properties:
- public endpoint only (the same one as ``script.study_volume_patterns fetch-usd``), no API key, ``.env`` never read;
- one cache file per fixed window under ``<out>/cache_15m/<product>/``: an interrupted run resumes where it stopped;
- HTTP 429 / 5xx / network errors are retried with exponential backoff (``Retry-After`` honoured), requests are paced;
- output: ``<out>/<product>_15m.parquet`` (columns ts, open, high, low, close, volume; ts is UTC, ascending, unique)
  and ``<out>/manifest_15m.json`` (rows, first/last timestamp, measured gaps, sha256 of the files, quality checks);
- research use only, no redistribution. The Coinbase terms of use for market data could not be verified
  automatically: this is recorded in the manifest (``terms_note``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from script.study_volume_patterns import CANDLES_URL

GRANULARITY = "FIFTEEN_MINUTE"
STEP_S = 900
CANDLES_PER_WINDOW = 300  # the endpoint allows at most 350 per request
WINDOW_S = CANDLES_PER_WINDOW * STEP_S
DEFAULT_START = datetime(2016, 1, 1, tzinfo=UTC)
DEFAULT_END = datetime(2023, 10, 1, tzinfo=UTC)  # exclusive: last candle starts 2023-09-30 23:45
DEFAULT_PRODUCTS = ("BTC-USD", "ETH-USD")
DEFAULT_OUT = Path("data/external/prices")
MAX_ATTEMPTS = 10
BACKOFF_CAP_S = 60.0
PACE_S = 0.2
COLUMNS = ["ts", "open", "high", "low", "close", "volume"]

# Declared tolerances of the comparison with the daily candles (days where both exist).
TOL_PRICE = 5e-4  # 5 bps on close / high / low
TOL_VOLUME = 1e-2  # 1 % on volume
MIN_PASS_FRACTION = 0.99  # the comparison passes if at least 99 % of the compared days are within tolerance
MIN_COVERAGE_FULL = 1.0  # high/low/volume are compared only on days with all 96 slots present
TERMS_NOTE = (
    "Coinbase market-data terms of use not verifiable automatically; data downloaded from the public endpoint "
    "for internal research use only, no redistribution (user approval 2026-10-28)."
)

Getter = Callable[..., Any]
Sleeper = Callable[[float], None]
Window = tuple[int, int]


# --------------------------------------------------------------------------------------------------- windows


def plan_windows(start: datetime, end: datetime, window_s: int = WINDOW_S) -> list[Window]:
    """Contiguous half-open ``[a, b)`` windows covering ``[start, end)``: no gaps, no overlaps."""
    a, z = int(start.timestamp()), int(end.timestamp())
    if a % STEP_S or z % STEP_S:
        raise ValueError("start and end must be aligned to 15 minutes")
    if z <= a:
        raise ValueError("end must be after start")
    out: list[Window] = []
    cur = a
    while cur < z:
        nxt = min(cur + window_s, z)
        out.append((cur, nxt))
        cur = nxt
    return out


# --------------------------------------------------------------------------------------------------- client


def _retry_delay(attempt: int, resp: Any) -> float:
    delay = min(BACKOFF_CAP_S, 2.0**attempt)
    headers = getattr(resp, "headers", None) or {}
    try:
        delay = max(delay, min(BACKOFF_CAP_S, float(headers.get("Retry-After", 0))))
    except (TypeError, ValueError):
        pass
    return delay


def fetch_window(product: str, window: Window, get: Getter, sleep: Sleeper) -> list[dict[str, str]]:
    """Raw candles of one window (as returned, strings kept), retrying 429 / 5xx / network errors with backoff."""
    a, b = window
    params = {"start": a, "end": b, "granularity": GRANULARITY}
    for attempt in range(MAX_ATTEMPTS):
        try:
            resp = get(CANDLES_URL.format(pid=product), params=params, timeout=30)
        except OSError as exc:  # requests exceptions derive from OSError
            print(f"  {product} window {a}: network error {type(exc).__name__}, retry {attempt + 1}", flush=True)
            sleep(min(BACKOFF_CAP_S, 2.0**attempt))
            continue
        if resp.status_code == 200:
            return [
                {k: str(c[k]) for k in ("start", "open", "high", "low", "close", "volume")}
                for c in resp.json().get("candles", [])
                if a <= int(c["start"]) < b
            ]
        if resp.status_code == 429 or resp.status_code >= 500:
            delay = _retry_delay(attempt, resp)
            print(f"  {product} window {a}: HTTP {resp.status_code}, retry {attempt + 1} in {delay:.0f}s", flush=True)
            sleep(delay)
            continue
        raise RuntimeError(f"{product}: HTTP {resp.status_code} for window {datetime.fromtimestamp(a, UTC)}")
    raise RuntimeError(f"{product}: too many retries for window {datetime.fromtimestamp(a, UTC)}")


def _cache_path(cache_dir: Path, product: str, window: Window) -> Path:
    return cache_dir / product / f"{window[0]}.json"


def load_or_fetch_window(
    product: str, window: Window, cache_dir: Path, get: Getter, sleep: Sleeper
) -> tuple[list[dict[str, str]], bool]:
    """Candles of a window from the cache, or downloaded and cached atomically. Returns (candles, was_cached)."""
    path = _cache_path(cache_dir, product, window)
    if path.exists():
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc["product"] == product and doc["window"] == list(window):
            candles: list[dict[str, str]] = doc["candles"]
            return candles, True
    candles = fetch_window(product, window, get, sleep)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"product": product, "window": list(window), "candles": candles}, separators=(",", ":")),
        encoding="utf-8",
    )
    tmp.replace(path)
    sleep(PACE_S)
    return candles, False


# --------------------------------------------------------------------------------------------------- assembly


def assemble(raw: Sequence[Mapping[str, str]]) -> tuple[pd.DataFrame, int]:
    """Ascending, deduplicated (last occurrence wins) frame with the standard columns, and the duplicates dropped."""
    rows = [
        (int(c["start"]), float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"]), float(c["volume"]))
        for c in raw
    ]
    frame = pd.DataFrame(rows, columns=["s", "open", "high", "low", "close", "volume"])
    before = len(frame)
    frame = frame.drop_duplicates(subset="s", keep="last").sort_values("s", kind="stable").reset_index(drop=True)
    frame.insert(0, "ts", pd.to_datetime(frame.pop("s"), unit="s", utc=True).astype("datetime64[ns, UTC]"))
    return frame[COLUMNS], before - len(frame)


# --------------------------------------------------------------------------------------------------- quality


def gap_runs(ts: pd.Series, start: datetime, end: datetime) -> list[tuple[int, int]]:
    """Runs of missing 15-minute slots in ``[start, end)`` as (first missing slot epoch, length in slots)."""
    a, z = int(start.timestamp()), int(end.timestamp())
    have = (ts.astype("int64") // 10**9).to_numpy(dtype=np.int64)  # ns -> s
    have = have[(have >= a) & (have < z)]
    edges = np.concatenate(([a - STEP_S], have, [z]))
    runs: list[tuple[int, int]] = []
    for prev, nxt in zip(edges[:-1], edges[1:], strict=True):
        n = int((nxt - prev) // STEP_S) - 1
        if n > 0:
            runs.append((int(prev) + STEP_S, n))
    return runs


def compare_with_daily(frame: pd.DataFrame, daily: pd.DataFrame) -> dict[str, Any]:
    """Aggregate 15m to UTC days and compare with the daily candles on the days present in both."""
    f = frame.assign(day=frame["ts"].dt.floor("D"))
    agg = f.groupby("day").agg(
        close=("close", "last"), high=("high", "max"), low=("low", "min"), volume=("volume", "sum"), n=("ts", "size")
    )
    d = daily.assign(day=daily["ts"].dt.floor("D")).set_index("day")
    both = agg.join(d[["close", "high", "low", "volume"]], how="inner", lsuffix="_15", rsuffix="_1d")
    out: dict[str, Any] = {
        "tolerance": {
            "close_high_low_rel": TOL_PRICE,
            "volume_rel": TOL_VOLUME,
            "min_pass_fraction": MIN_PASS_FRACTION,
            "hlv_only_on_days_with_all_slots": True,
        },
        "days_compared_close": int(len(both)),
    }
    if both.empty:
        out["passed"] = False
        out["note"] = "no overlapping days"
        return out
    rel_close = ((both["close_15"] - both["close_1d"]).abs() / both["close_1d"]).to_numpy()
    out["close"] = _fraction_stats(rel_close, TOL_PRICE)
    full = both[both["n"] >= int(MIN_COVERAGE_FULL * 96)]
    out["days_compared_hlv"] = int(len(full))
    if len(full):
        for col in ("high", "low"):
            out[col] = _fraction_stats(
                ((full[f"{col}_15"] - full[f"{col}_1d"]).abs() / full[f"{col}_1d"]).to_numpy(), TOL_PRICE
            )
        vol_1d = full["volume_1d"].to_numpy()
        vol_rel = np.where(
            vol_1d > 0, np.abs(full["volume_15"].to_numpy() - vol_1d) / np.where(vol_1d > 0, vol_1d, 1.0), 0.0
        )
        out["volume"] = _fraction_stats(vol_rel, TOL_VOLUME)
    # The verdict rests on the close (the series the hypotheses use); high/low/volume are reported alongside.
    out["passed"] = bool(out["close"]["fraction_within"] >= MIN_PASS_FRACTION)
    return out


def _fraction_stats(rel: np.ndarray, tol: float) -> dict[str, float]:
    return {
        "fraction_within": float(np.mean(rel <= tol)),
        "median_rel": float(np.median(rel)),
        "p99_rel": float(np.quantile(rel, 0.99)),
        "max_rel": float(np.max(rel)),
    }


def quality(
    frame: pd.DataFrame, start: datetime, end: datetime, duplicates_dropped: int, daily: pd.DataFrame | None
) -> dict[str, Any]:
    expected = (int(end.timestamp()) - int(start.timestamp())) // STEP_S
    runs = gap_runs(frame["ts"], start, end) if len(frame) else [(int(start.timestamp()), expected)]
    missing = sum(n for _, n in runs)
    longest = sorted(runs, key=lambda r: -r[1])[:10]
    ohlc_bad = (
        (frame["high"] < frame[["open", "close", "low"]].max(axis=1))
        | (frame["low"] > frame[["open", "close", "high"]].min(axis=1))
        | (frame[["open", "high", "low", "close"]] <= 0).any(axis=1)
    )
    q: dict[str, Any] = {
        "expected_slots": expected,
        "rows": int(len(frame)),
        "missing_slots": int(missing),
        "missing_fraction": missing / expected if expected else 0.0,
        "gap_runs": len(runs),
        "longest_gaps": [{"start": datetime.fromtimestamp(s, UTC).isoformat(), "slots": n} for s, n in longest],
        "duplicates_dropped": int(duplicates_dropped),
        "duplicate_timestamps_in_output": int(frame["ts"].duplicated().sum()),
        "zero_volume_candles": int((frame["volume"] <= 0).sum()),
        "ohlc_inconsistent_candles": int(ohlc_bad.sum()),
        "ts_strictly_increasing": bool(frame["ts"].is_monotonic_increasing and not frame["ts"].duplicated().any()),
    }
    if daily is not None:
        q["daily_comparison"] = compare_with_daily(frame, daily)
    return q


# --------------------------------------------------------------------------------------------------- files


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_daily(out: Path, product: str) -> pd.DataFrame | None:
    path = out / f"{product}_1d.parquet"
    return pd.read_parquet(path) if path.exists() else None


def product_entry(out: Path, product: str, start: datetime, end: datetime, duplicates_dropped: int) -> dict[str, Any]:
    path = out / f"{product}_15m.parquet"
    frame = pd.read_parquet(path)
    daily_path = out / f"{product}_1d.parquet"
    entry: dict[str, Any] = {
        "file": path.as_posix(),
        "sha256": sha256_file(path),
        "rows": int(len(frame)),
        "first_ts": frame["ts"].iloc[0].isoformat() if len(frame) else None,
        "last_ts": frame["ts"].iloc[-1].isoformat() if len(frame) else None,
        "range_requested": [start.isoformat(), end.isoformat()],
        "quality": quality(frame, start, end, duplicates_dropped, _load_daily(out, product)),
    }
    if daily_path.exists():
        entry["daily_reference"] = {"file": daily_path.as_posix(), "sha256": sha256_file(daily_path)}
    return entry


def write_manifest(out: Path, entries: Mapping[str, Any], script_sha256: str | None) -> Path:
    path = out / "manifest_15m.json"
    doc: dict[str, Any] = {}
    if path.exists():
        doc = json.loads(path.read_text(encoding="utf-8"))
    products: dict[str, Any] = dict(doc.get("products", {}))
    products.update(entries)
    doc = {
        "generated_at": datetime.now(UTC).isoformat(),
        "source": CANDLES_URL,
        "granularity": GRANULARITY,
        "terms_note": TERMS_NOTE,
        "script": "script/fetch_usd_15m.py",
        "script_sha256": script_sha256,
        "products": products,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return path


def _script_sha256() -> str | None:
    p = Path(__file__)
    return sha256_file(p) if p.exists() else None


# --------------------------------------------------------------------------------------------------- commands


def download_product(
    product: str,
    out: Path,
    start: datetime,
    end: datetime,
    get: Getter,
    sleep: Sleeper,
    now: Callable[[], float] = time.monotonic,
) -> int:
    """Download (resuming from the window cache) and write the parquet. Returns the duplicates dropped."""
    windows = plan_windows(start, end)
    cache_dir = out / "cache_15m"
    raw: list[dict[str, str]] = []
    t0 = now()
    fetched = 0
    for i, w in enumerate(windows, 1):
        candles, cached = load_or_fetch_window(product, w, cache_dir, get, sleep)
        raw.extend(candles)
        fetched += 0 if cached else 1
        if i % 25 == 0 or i == len(windows):
            elapsed = now() - t0
            eta = elapsed / fetched * (len(windows) - i) if fetched else 0.0
            print(
                f"{product}: window {i}/{len(windows)} ({datetime.fromtimestamp(w[1], UTC).date()}), "
                f"{len(raw)} candles, {fetched} downloaded, {i - fetched} from cache, eta {eta / 60:.1f} min",
                flush=True,
            )
    frame, dups = assemble(raw)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{product}_15m.parquet"
    tmp = path.with_suffix(".tmp")
    frame.to_parquet(tmp, index=False)
    tmp.replace(path)
    print(f"{product}: {len(frame)} candles written to {path}", flush=True)
    return dups


def cmd_fetch(
    products: Sequence[str],
    out: Path,
    start: datetime,
    end: datetime,
    get: Getter | None = None,
    sleep: Sleeper = time.sleep,
) -> int:
    if get is None:
        import requests

        get = requests.get
    entries: dict[str, Any] = {}
    for p in products:
        dups = download_product(p, out, start, end, get, sleep)
        entries[p] = product_entry(out, p, start, end, dups)
        _print_summary(p, entries[p])
    print(f"manifest: {write_manifest(out, entries, _script_sha256())}", flush=True)
    return 0


def cmd_check(products: Sequence[str], out: Path, start: datetime, end: datetime) -> int:
    manifest = out / "manifest_15m.json"
    known: Mapping[str, Any] = json.loads(manifest.read_text(encoding="utf-8"))["products"] if manifest.exists() else {}
    entries = {
        p: product_entry(out, p, start, end, int(known.get(p, {}).get("quality", {}).get("duplicates_dropped", 0)))
        for p in products
    }
    for p, e in entries.items():
        _print_summary(p, e)
    print(f"manifest: {write_manifest(out, entries, _script_sha256())}", flush=True)
    return 0


def _print_summary(product: str, e: Mapping[str, Any]) -> None:
    q = e["quality"]
    cmp = q.get("daily_comparison", {})
    print(
        f"{product}: rows {e['rows']}, first {e['first_ts']}, last {e['last_ts']}, missing {q['missing_slots']} "
        f"({q['missing_fraction']:.2%}) in {q['gap_runs']} runs, zero-volume {q['zero_volume_candles']}, "
        f"dups dropped {q['duplicates_dropped']}, daily check passed={cmp.get('passed')}",
        flush=True,
    )


def _parse_date(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, helptext in (
        ("fetch", "download (resumable) and write parquet + manifest"),
        ("check", "recompute manifest, no network"),
    ):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("--products", nargs="+", default=list(DEFAULT_PRODUCTS))
        s.add_argument("--out", type=Path, default=DEFAULT_OUT)
        s.add_argument("--start", type=_parse_date, default=DEFAULT_START, help="UTC date, inclusive")
        s.add_argument("--end", type=_parse_date, default=DEFAULT_END, help="UTC date, exclusive")
    a = ap.parse_args(argv)
    if a.cmd == "fetch":
        return cmd_fetch(a.products, a.out, a.start, a.end)
    return cmd_check(a.products, a.out, a.start, a.end)


if __name__ == "__main__":
    sys.exit(main())
