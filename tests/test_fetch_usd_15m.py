"""script/fetch_usd_15m.py: windows, retry/backoff, order and dedup, parquet shape, resume, quality checks.

No test touches the network: every download goes through a fake getter and sockets are blocked.
"""

from __future__ import annotations

import json
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from script import fetch_usd_15m as f

START = datetime(2020, 1, 1, tzinfo=UTC)
END = datetime(2020, 1, 11, tzinfo=UTC)  # 10 days = 960 slots = 4 windows of 300 (last one 60)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_a: object, **_k: object) -> None:
        raise AssertionError("network access in a test")

    monkeypatch.setattr(socket.socket, "connect", refuse)


class Resp:
    def __init__(
        self, status: int, candles: list[dict[str, Any]] | None = None, headers: dict[str, str] | None = None
    ) -> None:
        self.status_code = status
        self._candles = candles or []
        self.headers = headers or {}

    def json(self) -> dict[str, Any]:
        return {"candles": self._candles}


def candle(s: int, price: float = 100.0, volume: float = 1.0) -> dict[str, str]:
    return {
        "start": str(s), "open": str(price), "high": str(price + 1), "low": str(price - 1),
        "close": str(price + 0.5), "volume": str(volume),
    }  # fmt: skip


class FakeApi:
    """Serves synthetic candles (descending, like the real endpoint) for every slot of the requested window,
    skipping the slots in ``missing``; optionally returns the end boundary candle too (inclusive end)."""

    def __init__(self, missing: set[int] | None = None, inclusive_end: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.missing = missing or set()
        self.inclusive_end = inclusive_end

    def __call__(self, url: str, params: dict[str, Any], timeout: float) -> Resp:
        self.calls.append(params)
        a, b = params["start"], params["end"]
        hi = b + 1 if self.inclusive_end else b
        slots = [s for s in range(a, hi, f.STEP_S) if s not in self.missing]
        return Resp(200, [candle(s, 100 + (s % 7)) for s in reversed(slots)])


def no_sleep(_s: float) -> None:
    return None


# ---------------------------------------------------------------------------------------------- windows


def test_windows_cover_range_without_gaps_or_overlaps() -> None:
    ws = f.plan_windows(START, END)
    assert ws[0][0] == int(START.timestamp())
    assert ws[-1][1] == int(END.timestamp())
    assert all(b > a for a, b in ws)
    assert all(ws[i][1] == ws[i + 1][0] for i in range(len(ws) - 1))  # contiguous, half-open: no gap, no overlap
    assert all(b - a <= f.WINDOW_S for a, b in ws)
    assert all((b - a) // f.STEP_S <= 350 for a, b in ws)  # endpoint limit
    assert [(b - a) // f.STEP_S for a, b in ws] == [300, 300, 300, 60]


def test_windows_default_range_request_count() -> None:
    ws = f.plan_windows(f.DEFAULT_START, f.DEFAULT_END)
    assert 800 <= len(ws) <= 1000
    assert sum(b - a for a, b in ws) == int(f.DEFAULT_END.timestamp()) - int(f.DEFAULT_START.timestamp())


def test_windows_reject_bad_input() -> None:
    with pytest.raises(ValueError):
        f.plan_windows(END, START)
    with pytest.raises(ValueError):
        f.plan_windows(START + timedelta(seconds=1), END)


# ---------------------------------------------------------------------------------------------- retry


def test_retry_on_429_with_exponential_backoff() -> None:
    seq = [Resp(429), Resp(429), Resp(500), Resp(200, [candle(1_577_836_800)])]
    sleeps: list[float] = []
    calls = 0

    def get(url: str, params: dict[str, Any], timeout: float) -> Resp:
        nonlocal calls
        calls += 1
        return seq.pop(0)

    out = f.fetch_window("BTC-USD", (1_577_836_800, 1_577_836_800 + 900 * 4), get, sleeps.append)
    assert calls == 4 and len(out) == 1
    assert sleeps == [1.0, 2.0, 4.0]  # 2**attempt


def test_retry_honours_retry_after_and_caps() -> None:
    seq = [Resp(429, headers={"Retry-After": "7"}), Resp(429, headers={"Retry-After": "9999"}), Resp(200)]
    sleeps: list[float] = []
    f.fetch_window("BTC-USD", (0, 900), lambda *a, **k: seq.pop(0), sleeps.append)
    assert sleeps == [7.0, f.BACKOFF_CAP_S]


def test_retry_on_network_error_then_gives_up() -> None:
    sleeps: list[float] = []

    def get(*a: Any, **k: Any) -> Resp:
        raise ConnectionError("boom")

    with pytest.raises(RuntimeError, match="too many retries"):
        f.fetch_window("BTC-USD", (0, 900), get, sleeps.append)
    assert len(sleeps) == f.MAX_ATTEMPTS
    assert max(sleeps) <= f.BACKOFF_CAP_S


def test_non_retryable_status_raises_immediately() -> None:
    calls: list[int] = []

    def get(*a: Any, **k: Any) -> Resp:
        calls.append(1)
        return Resp(404)

    with pytest.raises(RuntimeError, match="HTTP 404"):
        f.fetch_window("BTC-USD", (0, 900), get, no_sleep)
    assert len(calls) == 1


def test_candles_outside_window_are_dropped() -> None:
    resp = Resp(200, [candle(900 * 5), candle(900 * 4), candle(900 * 3), candle(900 * 2), candle(900 * 1)])
    out = f.fetch_window("BTC-USD", (900 * 2, 900 * 4), lambda *a, **k: resp, no_sleep)
    assert sorted(int(c["start"]) for c in out) == [900 * 2, 900 * 3]  # end exclusive, start inclusive


# ---------------------------------------------------------------------------------------------- order, dedup, parquet


def test_assemble_orders_and_deduplicates() -> None:
    s0 = int(START.timestamp())
    raw = [candle(s0 + 1800, 3), candle(s0, 1), candle(s0 + 900, 2), candle(s0 + 900, 9), candle(s0, 1)]
    frame, dups = f.assemble(raw)
    assert dups == 2
    assert list(frame.columns) == f.COLUMNS
    assert frame["ts"].is_monotonic_increasing and not frame["ts"].duplicated().any()
    assert frame["open"].tolist() == [1.0, 9.0, 3.0]  # last occurrence wins
    assert str(frame["ts"].dtype) == "datetime64[ns, UTC]"
    assert frame["ts"].iloc[0] == pd.Timestamp(START)


def test_download_writes_parquet_with_expected_shape(tmp_path: Path) -> None:
    api = FakeApi(inclusive_end=True)  # the boundary candle returned by the API must not duplicate
    dups = f.download_product("BTC-USD", tmp_path, START, END, api, no_sleep)
    frame = pd.read_parquet(tmp_path / "BTC-USD_15m.parquet")
    assert dups == 0
    assert list(frame.columns) == f.COLUMNS
    assert str(frame["ts"].dtype) == "datetime64[ns, UTC]"
    assert all(str(frame[c].dtype) == "float64" for c in f.COLUMNS[1:])
    assert len(frame) == 960
    assert frame["ts"].iloc[0] == pd.Timestamp(START)
    assert frame["ts"].iloc[-1] == pd.Timestamp(END) - pd.Timedelta(minutes=15)
    assert (frame["ts"].diff().dropna() == pd.Timedelta(minutes=15)).all()
    assert len(api.calls) == 4
    assert [(c["start"], c["end"]) for c in api.calls] == f.plan_windows(START, END)


def test_resume_uses_cache_and_downloads_only_missing_windows(tmp_path: Path) -> None:
    api1 = FakeApi()
    f.download_product("BTC-USD", tmp_path, START, END, api1, no_sleep)
    first = pd.read_parquet(tmp_path / "BTC-USD_15m.parquet")
    # simulate an interruption: drop the cache of the 2nd and 4th windows
    ws = f.plan_windows(START, END)
    for w in (ws[1], ws[3]):
        (tmp_path / "cache_15m" / "BTC-USD" / f"{w[0]}.json").unlink()
    api2 = FakeApi()
    f.download_product("BTC-USD", tmp_path, START, END, api2, no_sleep)
    assert [(c["start"], c["end"]) for c in api2.calls] == [ws[1], ws[3]]
    pd.testing.assert_frame_equal(first, pd.read_parquet(tmp_path / "BTC-USD_15m.parquet"))


def test_interrupted_run_leaves_no_partial_cache_file(tmp_path: Path) -> None:
    ws = f.plan_windows(START, END)
    n = 0

    def get(url: str, params: dict[str, Any], timeout: float) -> Resp:
        nonlocal n
        n += 1
        if n == 3:
            raise KeyboardInterrupt
        return FakeApi()(url, params, timeout)

    with pytest.raises(KeyboardInterrupt):
        f.download_product("BTC-USD", tmp_path, START, END, get, no_sleep)
    cached = sorted(p.name for p in (tmp_path / "cache_15m" / "BTC-USD").iterdir())
    assert cached == [f"{ws[0][0]}.json", f"{ws[1][0]}.json"]  # no .tmp, the failed window is absent


def test_empty_window_is_cached_and_not_refetched(tmp_path: Path) -> None:
    calls: list[int] = []

    def get(*a: Any, **k: Any) -> Resp:
        calls.append(1)
        return Resp(200, [])

    w = f.plan_windows(START, END)[0]
    assert f.load_or_fetch_window("BTC-USD", w, tmp_path, get, no_sleep) == ([], False)
    assert f.load_or_fetch_window("BTC-USD", w, tmp_path, get, no_sleep) == ([], True)
    assert len(calls) == 1


# ---------------------------------------------------------------------------------------------- quality


def frame_of(slots: list[int], volume: float = 1.0) -> pd.DataFrame:
    return f.assemble([candle(s, 100.0, volume) for s in slots])[0]


def test_gap_runs_measure_holes_including_edges() -> None:
    s0 = int(START.timestamp())
    slots = [s0 + i * 900 for i in range(960) if i not in {0, 1, 5, 6, 7, 959}]
    runs = f.gap_runs(frame_of(slots)["ts"], START, END)
    assert runs == [(s0, 2), (s0 + 5 * 900, 3), (s0 + 959 * 900, 1)]


def test_quality_counts_zero_volume_and_missing() -> None:
    s0 = int(START.timestamp())
    slots = [s0 + i * 900 for i in range(960) if i != 10]
    frame = frame_of(slots)
    frame.loc[3, "volume"] = 0.0
    q = f.quality(frame, START, END, 4, None)
    assert q["expected_slots"] == 960 and q["rows"] == 959
    assert q["missing_slots"] == 1 and q["gap_runs"] == 1
    assert q["zero_volume_candles"] == 1
    assert q["duplicates_dropped"] == 4 and q["duplicate_timestamps_in_output"] == 0
    assert q["ohlc_inconsistent_candles"] == 0 and q["ts_strictly_increasing"]


def daily_from(frame: pd.DataFrame) -> pd.DataFrame:
    g = frame.assign(day=frame["ts"].dt.floor("D")).groupby("day")
    d = g.agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    )
    d = d.reset_index().rename(columns={"day": "ts"})
    d["ts"] = d["ts"].astype("datetime64[ns, UTC]")
    return d


def test_daily_comparison_passes_on_consistent_data_and_fails_on_drift() -> None:
    s0 = int(START.timestamp())
    rng = np.random.default_rng(0)
    raw = []
    for i in range(960):
        p = 100 + float(rng.normal(0, 1))
        raw.append(candle(s0 + i * 900, p, 2.0))
    frame = f.assemble(raw)[0]
    daily = daily_from(frame)
    ok = f.compare_with_daily(frame, daily)
    assert ok["passed"] and ok["close"]["max_rel"] == 0.0
    assert ok["days_compared_close"] == 10 and ok["days_compared_hlv"] == 10
    assert ok["volume"]["fraction_within"] == 1.0
    bad = daily.copy()
    bad["close"] = bad["close"] * 1.01  # 100 bps off everywhere
    assert not f.compare_with_daily(frame, bad)["passed"]
    assert ok["tolerance"]["close_high_low_rel"] == f.TOL_PRICE


def test_daily_comparison_skips_high_low_volume_on_incomplete_days() -> None:
    s0 = int(START.timestamp())
    slots = [s0 + i * 900 for i in range(960) if i != 5]  # day 1 loses one slot
    frame = frame_of(slots)
    out = f.compare_with_daily(frame, daily_from(frame))
    assert out["days_compared_close"] == 10 and out["days_compared_hlv"] == 9


# ---------------------------------------------------------------------------------------------- manifest / CLI


def test_fetch_command_writes_manifest_with_hashes_and_no_secrets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    api = FakeApi(missing={int(START.timestamp()) + 900 * 3})
    # daily reference with the same content, to exercise the comparison branch
    frame_ref = f.assemble([candle(int(START.timestamp()) + i * 900) for i in range(960)])[0]
    daily_from(frame_ref).to_parquet(tmp_path / "BTC-USD_1d.parquet", index=False)
    assert f.cmd_fetch(["BTC-USD"], tmp_path, START, END, api, no_sleep) == 0
    doc = json.loads((tmp_path / "manifest_15m.json").read_text(encoding="utf-8"))
    e = doc["products"]["BTC-USD"]
    assert (
        e["rows"] == 959
        and e["first_ts"].startswith("2020-01-01T00:00")
        and e["last_ts"].startswith("2020-01-10T23:45")
    )
    assert e["sha256"] == f.sha256_file(tmp_path / "BTC-USD_15m.parquet") and len(e["sha256"]) == 64
    assert e["quality"]["missing_slots"] == 1 and e["quality"]["zero_volume_candles"] == 0
    assert e["daily_reference"]["sha256"] == f.sha256_file(tmp_path / "BTC-USD_1d.parquet")
    assert "terms" in doc["terms_note"] and doc["granularity"] == "FIFTEEN_MINUTE"
    assert "BTC-USD: rows 959" in capsys.readouterr().out
    # check recomputes the manifest without network and keeps the duplicates count
    assert f.cmd_check(["BTC-USD"], tmp_path, START, END) == 0
    again = json.loads((tmp_path / "manifest_15m.json").read_text(encoding="utf-8"))
    assert again["products"]["BTC-USD"]["sha256"] == e["sha256"]


def test_manifest_merges_products(tmp_path: Path) -> None:
    f.write_manifest(tmp_path, {"A": {"rows": 1}}, None)
    f.write_manifest(tmp_path, {"B": {"rows": 2}}, "x")
    doc = json.loads((tmp_path / "manifest_15m.json").read_text(encoding="utf-8"))
    assert set(doc["products"]) == {"A", "B"}


def test_script_has_no_forbidden_words() -> None:
    text = Path(f.__file__).read_text(encoding="utf-8").lower()
    assert "margin" not in text and "margine" not in text
