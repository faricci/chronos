"""Synthetic tests of the pre-registered volume-pattern study (2026-10-26-studio-pattern-volume-prereg).

Only synthetic data: no real bar and no network is touched.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from scipy import stats as sps

from script import study_volume_patterns as sv

DAY = pd.Timedelta(days=1)


def make_daily(
    close: np.ndarray, volume: np.ndarray, start: str = "2018-01-01", open_: np.ndarray | None = None
) -> pd.DataFrame:
    """Daily frame with open = previous close (unless given); V = volume x close, Vb = volume."""
    idx = pd.date_range(start, periods=len(close), freq="D")
    o = np.r_[close[0], close[:-1]] if open_ is None else open_
    d = pd.DataFrame(
        {
            "open": o,
            "high": np.maximum(o, close) * 1.01,
            "low": np.minimum(o, close) * 0.99,
            "close": close,
            "V": volume * close,
            "Vb": volume,
            "valid": True,
        },
        index=idx,
    )
    d.index.name = "day"
    return d


def random_walk(n: int, seed: int, vol: float = 0.03) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0.0, vol, n)))
    volume = np.exp(rng.normal(0.0, 0.5, n)) * 1000.0
    return make_daily(close, volume)


# ------------------------------------------------------------------------------------------------ 1. daily_from_1m


def _grid(days: int = 2) -> pd.DataFrame:
    n = 1440 * days
    ts = pd.date_range("2024-01-01", periods=n, freq="1min", tz="UTC")
    close = 100.0 + np.arange(n) * 0.01
    return pd.DataFrame(
        {
            "open": close - 0.005,
            "high": close + 0.1,
            "low": close - 0.1,
            "close": close,
            "volume": np.ones(n),
        },
        index=pd.DatetimeIndex(ts, name="ts"),
    )


def test_01_daily_from_1m_values_and_gap_validity() -> None:
    grid = _grid()
    real = grid.index
    d = sv.daily_from_1m(grid, real)
    day0 = d.loc[pd.Timestamp("2024-01-01")]
    first, last = grid.iloc[0], grid.iloc[1439]
    assert day0["open"] == pytest.approx(first["open"])
    assert day0["close"] == pytest.approx(last["close"])
    assert day0["high"] == pytest.approx(grid["high"].iloc[:1440].max())
    assert day0["low"] == pytest.approx(grid["low"].iloc[:1440].min())
    assert day0["Vb"] == pytest.approx(1440.0)
    assert day0["V"] == pytest.approx((grid["close"].iloc[:1440] * grid["volume"].iloc[:1440]).sum())
    assert bool(day0["valid"])

    def with_gap(minutes: int) -> pd.DataFrame:
        g = grid.copy()
        lo = 100
        missing = g.index[lo : lo + minutes]
        # the grid has flat bars (previous close, volume 0) where the exchange had no candle
        prev_close = g["close"].iloc[lo - 1]
        g.loc[missing, ["open", "high", "low", "close"]] = prev_close
        g.loc[missing, "volume"] = 0.0
        return sv.daily_from_1m(g, g.index.difference(missing))

    ok = with_gap(360).loc[pd.Timestamp("2024-01-01")]
    bad = with_gap(361).loc[pd.Timestamp("2024-01-01")]
    assert bool(ok["valid"]) and not math.isnan(ok["close"])
    assert not bool(bad["valid"]) and math.isnan(bad["close"]) and math.isnan(bad["V"])


# ------------------------------------------------------------------------------------------------ 2. no look-ahead


def test_02_features_do_not_look_ahead() -> None:
    d = random_walk(520, seed=1)
    cut = 400
    d2 = d.copy()
    rng = np.random.default_rng(5)
    for col in ("open", "high", "low", "close", "V", "Vb"):
        d2.iloc[cut + 1 :, d2.columns.get_loc(col)] = d2[col].iloc[cut + 1 :].to_numpy() * rng.uniform(
            0.2, 5.0, 519 - cut
        )
    f1, f2 = sv.features(d), sv.features(d2)
    pd.testing.assert_frame_equal(f1.iloc[: cut + 1], f2.iloc[: cut + 1])
    assert f1["elig"].iloc[: cut + 1].any()
    assert not f1.iloc[cut + 1 :].equals(f2.iloc[cut + 1 :])


# ------------------------------------------------------------------------------------------ 3. thresholds: training


def test_03_thresholds_use_only_training() -> None:
    d = random_walk(900, seed=2)
    hold = pd.Timestamp("2019-12-01")
    d2 = d.copy()
    cols = ["open", "high", "low", "close", "V", "Vb"]
    d2.loc[d2.index >= hold, cols] = d2.loc[d2.index >= hold, cols].to_numpy() * 3.0
    mask1 = np.asarray(d.index < hold)
    t1 = sv.thresholds(sv.features(d), mask1)
    t2 = sv.thresholds(sv.features(d2), mask1)
    assert t1 == t2
    assert t1["n_train_days"] > 100
    # a different training window gives different thresholds (the function is not constant)
    assert sv.thresholds(sv.features(d), np.asarray(d.index < pd.Timestamp("2019-06-01"))) != t1
    # the quantiles are the linear ones of the eligible training days
    f = sv.features(d)
    m = mask1 & f["elig"].to_numpy(dtype=bool)
    assert t1["DD180"] == pytest.approx(float(np.quantile(f.loc[m, "DD180"], 0.20)))
    assert t1["VH"] == pytest.approx(float(np.quantile(f.loc[m, "VR"], 0.80)))


# --------------------------------------------------------------------------------------------------- 4. episodes


def _signal(offsets: list[int]) -> pd.Series:
    idx = pd.date_range("2023-01-01", periods=80, freq="D")
    s = pd.Series(False, index=idx)
    s.iloc[offsets] = True
    return s


def test_04_episodes_separation() -> None:
    d0 = pd.Timestamp("2023-01-01")
    assert sv.episodes(_signal([10, 11, 12, 13])) == [d0 + 10 * DAY]
    assert sv.episodes(_signal([10, 25])) == [d0 + 10 * DAY, d0 + 25 * DAY]  # +15 days: a new episode
    assert sv.episodes(_signal([10, 24])) == [d0 + 10 * DAY]  # +14 days: no
    # greedy chain: the gap is measured from the previous EPISODE, not from the previous signal day
    assert sv.episodes(_signal([10, 20, 25])) == [d0 + 10 * DAY, d0 + 25 * DAY]


# ----------------------------------------------------------------------------------------------------- 5. units


def test_05_units() -> None:
    d0 = pd.Timestamp("2023-01-01")
    close = {"BTC": [d0], "ETH": [d0 + 10 * DAY]}
    assert len(sv.units(close, 7)) == 1 and len(sv.units(close, 30)) == 1
    far = {"BTC": [d0], "ETH": [d0 + 20 * DAY]}
    assert len(sv.units(far, 7)) == 2
    assert len(sv.units(far, 30)) == 1
    # chain: A-B-C each 12 days apart are one unit (connected components), not two
    chain = {"BTC": [d0, d0 + 24 * DAY], "ETH": [d0 + 12 * DAY]}
    assert len(sv.units(chain, 7)) == 1
    # same coin 15 days apart at H = 7: two units; at H = 30: one
    same = {"BTC": [d0, d0 + 15 * DAY]}
    assert len(sv.units(same, 7)) == 2 and len(sv.units(same, 30)) == 1


# ------------------------------------------------------------------------------------- 6. return, cost and MAE


def test_06_forward_cost_and_mae() -> None:
    n = 12
    opens = np.array([10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21], dtype=float)
    lows = opens - 1.0
    lows[3] = 5.0  # a deep wick on day 3
    close = opens + 0.5
    d = pd.DataFrame(
        {"open": opens, "high": opens + 2, "low": lows, "close": close, "V": 1.0, "Vb": 1.0, "valid": True},
        index=pd.date_range("2023-01-01", periods=n, freq="D"),
    )
    t = d.index[0]
    g, mae = sv.forward(d, t, 3)  # entry O[1] = 11, exit O[4] = 14, days 1..3
    assert g == pytest.approx(14 / 11 - 1)
    assert mae == pytest.approx(5.0 / 11 - 1)
    g2, mae2 = sv.forward(d, t, 3, delay=2)  # entry O[2] = 12, exit O[5] = 15
    assert g2 == pytest.approx(15 / 12 - 1)
    assert mae2 == pytest.approx(min(lows[2:5]) / 12 - 1)
    # beyond the data: NaN
    gn, mn = sv.forward(d, d.index[10], 3)
    assert math.isnan(gn) and math.isnan(mn)

    # net = gross - 0.0077 through the pipeline
    w = random_walk(400, seed=3)
    cd = sv.CoinData("BTC", w, sv.features(w))
    split = sv.Split("train", None, None, None)
    tt = w.index[300]
    out = sv.outcomes(sv._Engine(), cd, [tt], [split], 7, 1, 0.03)[0]
    g7, mae7 = sv.forward(w, tt, 7)
    assert out.status == "ok"
    assert (
        out.g == pytest.approx(g7)
        and out.net == pytest.approx(g7 - 0.0077)
        and out.net110 == pytest.approx(g7 - 0.0110)
    )
    assert out.mae == pytest.approx(mae7)
    mu = float(sv.forward_series(w, 7)[0][cd.feat["elig"]].mean())
    assert out.ex == pytest.approx(g7 - mu)


# ---------------------------------------------------------------------------------------------------- 7. purge


def test_07_purge() -> None:
    w = random_walk(500, seed=4)
    cd = sv.CoinData("BTC", w, sv.features(w))
    hold = w.index[400]
    splits = [sv.Split("train", None, hold, hold), sv.Split("holdout", hold, None, None)]
    eng = sv._Engine()
    # H = 7: a training signal is kept only if t + 1 + 7 <= holdout_start, i.e. t <= hold - 8 days
    ok = sv.outcomes(eng, cd, [hold - 8 * DAY], splits, 7, 1, 0.03)[0]
    cut = sv.outcomes(eng, cd, [hold - 7 * DAY], splits, 7, 1, 0.03)[0]
    assert ok.split == "train" and ok.status == "ok"
    assert cut.split is None and cut.status == "outside"
    # the same day is a valid training signal for a shorter horizon, and the holdout starts at holdout_start
    assert sv.outcomes(eng, cd, [hold - 7 * DAY], splits, 3, 1, 0.03)[0].split == "train"
    assert sv.outcomes(eng, cd, [hold], splits, 7, 1, 0.03)[0].split == "holdout"
    # holdout: the exit must exist (t + 1 + H <= last day)
    last = w.index[-1]
    assert sv.outcomes(eng, cd, [last - 8 * DAY], splits, 7, 1, 0.03)[0].split == "holdout"
    assert sv.outcomes(eng, cd, [last - 7 * DAY], splits, 7, 1, 0.03)[0].split is None


# ------------------------------------------------------------------------------------------- 8. Holm, CI, minimum


def test_08_holm_and_intervals() -> None:
    assert sv.holm([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])
    assert sv.holm([0.5, 0.001]) == pytest.approx([0.5, 0.002])
    assert sv.holm([0.9, 0.8]) == pytest.approx([1.0, 1.0])

    y = [1.0, 2.0, 3.0, 4.0, 5.0]
    lo, hi, p_le, p_ge = sv.ci_t(y)
    q = float(sps.t.ppf(0.95, 4))
    se = float(np.std(y, ddof=1)) / math.sqrt(5)
    assert lo == pytest.approx(3 - q * se) and hi == pytest.approx(3 + q * se)
    assert p_le == pytest.approx(float(sps.t.sf(3 / se, 4))) and p_ge == pytest.approx(1 - p_le)

    b1 = sv.ci_boot(y, 2000, 7)
    assert b1 == sv.ci_boot(y, 2000, 7)  # deterministic
    assert 1.0 < b1[0] < 3.0 < b1[1] < 5.0
    assert b1[2] == pytest.approx(1 / 2001)  # every resampled mean is positive
    cons = sv.conservative_ci(y, 7, B=2000)
    assert cons.lb == min(b1[0], lo) and cons.ub == max(b1[1], hi) and cons.p_le0 == max(b1[2], p_le)
    assert sv.ci_boot([], 100, 1)[2] == 1.0 and sv.ci_t([1.0])[2] == 1.0

    # below the minimum number of units the p entering Holm is 1
    ci = sv.Ci(1.0, 2.0, 0.0001, 0.9999, (0.0,) * 4, (0.0,) * 4)
    assert sv.train_p(sv.SplitStat(n=9, net_ci=ci, ex_ci=ci), "H1") == 1.0
    assert sv.train_p(sv.SplitStat(n=10, net_ci=ci, ex_ci=ci), "H1") == 0.0001
    assert sv.train_p(sv.SplitStat(n=10, net_ci=ci, ex_ci=ci), "H2") == 0.9999


# ------------------------------------------------------------------------------------------------ 9. verdict table


def _ci(lb: float, ub: float, p_le0: float = 0.001, p_ge0: float = 0.999) -> sv.Ci:
    return sv.Ci(lb, ub, p_le0, p_ge0, (lb, ub, p_le0, p_ge0), (lb, ub, p_le0, p_ge0))


def _stat(
    n: int = 12,
    net: float = 0.05,
    ex: float = 0.05,
    lb: float = 0.01,
    ub: float = 0.09,
    wo_best: float = 0.03,
    coins: dict[str, sv.CoinStat] | None = None,
) -> sv.SplitStat:
    return sv.SplitStat(
        n=n,
        n_ep=n,
        mean_net=net,
        mean_ex=ex,
        net_ci=_ci(lb, ub),
        ex_ci=_ci(lb, ub),
        coins=coins or {"BTC": sv.CoinStat(6, net, ex), "ETH": sv.CoinStat(6, net, ex)},
        net_wo_best=wo_best,
        ex_wo_best=-wo_best,
    )


def test_09_verdict_table() -> None:
    good_t, good_v = _stat(), _stat(n=8)
    rep = _stat(n=4)

    assert sv.verdict_h1(_stat(n=9), good_v, 0.01, rep, True, True).verdict == "INCONCLUSO"  # n_T < 10
    assert sv.verdict_h1(good_t, _stat(n=5), 0.01, rep, True, True).verdict == "INCONCLUSO"  # n_V < 6
    assert sv.verdict_h1(good_t, _stat(n=8, ex=-0.001), 0.01, rep, True, True).verdict == "NO-GO"  # excess <= 0
    assert sv.verdict_h1(_stat(net=-0.01, lb=-0.05, ub=0.03), good_v, 0.01, rep, True, True).verdict == "NO-GO"
    assert sv.verdict_h1(good_t, _stat(n=8, net=-0.05, lb=-0.1, ub=-0.01), 0.01, rep, True, True).verdict == "NO-GO"
    go = sv.verdict_h1(good_t, good_v, 0.01, rep, True, True)
    assert go.verdict == "GO" and not go.failed
    assert sv.verdict_h1(good_t, good_v, 0.01, rep, False, True).verdict == "CANDIDATO"  # holdout not clean
    assert sv.verdict_h1(good_t, good_v, 0.20, rep, True, True).verdict == "INCONCLUSO"  # Holm
    assert sv.verdict_h1(good_t, _stat(n=8, lb=-0.01), 0.01, rep, True, True).verdict == "INCONCLUSO"  # LB <= 0
    # one coin with a negative mean net (>= 3 episodes) blocks; with < 3 episodes it does not
    bad_coin = {"BTC": sv.CoinStat(6, 0.08, 0.08), "ETH": sv.CoinStat(3, -0.02, 0.01)}
    v = sv.verdict_h1(_stat(coins=bad_coin), good_v, 0.01, rep, True, True)
    assert v.verdict == "INCONCLUSO" and "d_no_coin_net<=0" in v.failed
    few = {"BTC": sv.CoinStat(6, 0.08, 0.08), "ETH": sv.CoinStat(2, -0.02, 0.01)}
    assert sv.verdict_h1(_stat(coins=few), good_v, 0.01, rep, True, True).verdict == "GO"
    # without the best unit the mean net is <= 0
    v = sv.verdict_h1(_stat(wo_best=-0.001), good_v, 0.01, rep, True, True)
    assert v.verdict == "INCONCLUSO" and "e_net>0_without_best_unit" in v.failed
    # replica (arm L): negative blocks if n_R >= 3, not evaluable below
    neg_rep = _stat(n=4, net=-0.02)
    assert sv.verdict_h1(good_t, good_v, 0.01, neg_rep, True, True).verdict == "INCONCLUSO"
    assert sv.verdict_h1(good_t, good_v, 0.01, _stat(n=2, net=-0.02), True, True).verdict == "GO"
    assert sv.verdict_h1(good_t, good_v, 0.01, None, True, False).verdict == "GO"  # arm E has no replica

    # H2 (veto): sign reversed on the excess
    vt = _stat(ex=-0.05, lb=-0.09, ub=-0.01, wo_best=0.03)
    vv = _stat(n=8, ex=-0.05, lb=-0.09, ub=-0.01, wo_best=0.03)
    assert sv.verdict_h2(vt, vv, 0.01, None, True, False).verdict == "VETO"
    assert sv.verdict_h2(vt, vv, 0.01, None, False, False).verdict == "CANDIDATO"
    assert sv.verdict_h2(_stat(ex=0.001), vv, 0.01, None, True, False).verdict == "NO-GO"  # excess >= 0
    assert sv.verdict_h2(vt, _stat(n=8, ex=-0.05, ub=0.02), 0.01, None, True, False).verdict == "INCONCLUSO"
    assert sv.verdict_h2(_stat(n=3), vv, 0.01, None, True, False).verdict == "INCONCLUSO"

    # global verdict
    mk = sv.Verdict
    g = {7: mk("GO", [], []), 14: mk("GO", [], []), 30: mk("NO-GO", [], [])}
    assert sv.global_verdict(g) == ("GO", 7)
    assert sv.global_verdict({7: mk("INCONCLUSO", [], []), 14: mk("GO", [], []), 30: mk("GO", [], [])}) == ("GO", 14)
    assert sv.global_verdict({h: mk("NO-GO", [], []) for h in (7, 14, 30)}) == ("NO-GO", None)
    assert (
        sv.global_verdict({7: mk("NO-GO", [], []), 14: mk("CANDIDATO", [], []), 30: mk("INCONCLUSO", [], [])})[0]
        == "CANDIDATO"
    )
    assert (
        sv.global_verdict({7: mk("NO-GO", [], []), 14: mk("INCONCLUSO", [], []), 30: mk("NO-GO", [], [])})[0]
        == "INCONCLUSO"
    )


# ----------------------------------------------------------------------------------------- 10. end to end synthetic


def _market(seed: int, inject: bool) -> dict[str, pd.DataFrame]:
    """Three correlated coins, random walk with 10 years of daily data; optionally +25% after each capitulation."""
    n = 10 * 365
    rng = np.random.default_rng(seed)
    market = rng.normal(0.0, 0.025, n)
    out: dict[str, pd.DataFrame] = {}
    for coin in ("BTC", "ETH", "SOL"):
        r = 0.7 * market + 0.7 * rng.normal(0.0, 0.025, n)
        close = 100.0 * np.exp(np.cumsum(r))
        # volume depends on |r| so that big drops come with big volume
        volume = np.exp(rng.normal(0.0, 0.4, n) + 8.0 * np.abs(r)) * 1000.0
        out[coin] = make_daily(close, volume)
    if inject:
        for d in out.values():
            f = sv.features(d)
            thr = sv.thresholds(f, np.asarray(d.index < pd.Timestamp("2023-01-01")))
            eps = sv.episodes(sv.signals(f, thr)["H1"])
            opens = d["open"].to_numpy().copy()
            for t in eps:
                i = d.index.get_loc(t)
                if i + 8 < len(opens):
                    opens[i + 8] *= 1.25  # O[t+1+7] is the exit open of the 7-day trade
            d["open"] = opens
            d["high"] = np.maximum(d["high"], d["open"])
            d["low"] = np.minimum(d["low"], d["open"])
    return out


def test_10_end_to_end_injected_effect_gives_go_and_null_does_not() -> None:
    cfg = sv.ArmConfig("E", pd.Timestamp("2023-01-01"), None, True, False)
    injected = sv.analyze(_market(11, True), None, cfg)["decision"]
    assert injected["h1"]["7"]["verdict"] == "GO", injected["h1"]["7"]
    assert injected["global"]["verdict"] == "GO" and injected["global"]["operating_horizon_days"] == 7
    null = sv.analyze(_market(11, False), None, cfg)["decision"]
    assert null["global"]["verdict"] != "GO"
    assert all(v["verdict"] != "GO" for v in null["h1"].values())
    # holdout not clean: the same injected data can at most be a candidate
    unclean = sv.analyze(_market(11, True), None, sv.ArmConfig("E", pd.Timestamp("2023-01-01"), None, False, False))
    assert unclean["decision"]["h1"]["7"]["verdict"] == "CANDIDATO"
    assert unclean["decision"]["global"]["verdict"] == "CANDIDATO"


# ------------------------------------------------------------------------------------ 11. run: refusal and order


class _Stop(Exception):
    pass


class _RecordingSource:
    def __init__(self, folder: Path, log: list[str]) -> None:
        self.folder, self.log = folder, log

    def bars(self, product: str) -> pd.DataFrame:
        self.log.append(f"bars:{(self.folder / '00_preregistration.json').exists()}")
        raise _Stop

    def real_ts(self, product: str) -> pd.DatetimeIndex:  # pragma: no cover - never reached
        raise AssertionError


def _env(log: list[str], folder: Path, clean: bool = True) -> sv.Env:
    def factory(root: Path) -> Any:
        log.append("factory")
        return _RecordingSource(folder, log)

    return sv.Env(
        head=lambda: "deadbeef",
        script_clean=lambda: clean,
        now=lambda: datetime(2026, 10, 26, 12, 0, tzinfo=UTC),
        source_factory=factory,
    )


def test_11_run_refuses_and_writes_prereg_before_reading_bars(tmp_path: Path) -> None:
    out = tmp_path / "study"
    args = sv.RunArgs("E", tmp_path / "bars", None, ["unknown"], out)
    folder = out / "E"

    # an existing output folder is refused and nothing is read
    folder.mkdir(parents=True)
    log: list[str] = []
    assert sv.run_study(args, _env(log, folder)) == 2
    assert log == [] and list(folder.iterdir()) == []
    folder.rmdir()

    # a script with uncommitted changes is refused
    assert sv.run_study(args, _env(log, folder, clean=False)) == 2
    assert log == [] and not folder.exists()

    # the real run: 00_preregistration.json exists BEFORE the first bar is read
    with pytest.raises(_Stop):
        sv.run_study(args, _env(log, folder))
    assert log == ["factory", "bars:True"]
    doc = json.loads((folder / "00_preregistration.json").read_text(encoding="utf-8"))
    assert doc["schema"] == "chronos.study_volume.v1" and doc["arm"] == "E"
    assert doc["git_commit"] == "deadbeef" and doc["holdout_clean"] is False
    assert doc["prereg_sha256"] == sv.prereg_sha256() and len(doc["prereg_sha256"]) == 64
    assert doc["script_sha256"] == sv._sha256_file(sv.REPO_ROOT / sv.SCRIPT_REL)
    assert sorted(p.name for p in folder.iterdir()) == ["00_preregistration.json"]
    # a second run on the same arm is refused
    assert sv.run_study(args, _env([], folder)) == 2

    # holdout_clean follows the documented exploration range
    hs, he = pd.Timestamp("2021-01-01"), pd.Timestamp("2023-09-30")
    assert sv.holdout_clean_of(["2023-10-01", "2026-10-26"], hs, he) is True
    assert sv.holdout_clean_of(["2023-01-01", "2022-06-01"], hs, he) is False
    assert sv.holdout_clean_of(["unknown"], hs, he) is False


# -------------------------------------------------------------------------------------------- extra: fetch-usd


def test_12_fetch_daily_windows_retries_and_parquet_shape() -> None:
    calls: list[dict[str, Any]] = []

    class Resp:
        def __init__(self, status: int, candles: list[dict[str, str]]) -> None:
            self.status_code, self._c = status, candles

        def json(self) -> dict[str, Any]:
            return {"candles": self._c}

    def get(url: str, params: dict[str, Any], timeout: int) -> Resp:
        calls.append(params)
        if len(calls) == 1:
            return Resp(429, [])
        lo = params["start"]
        return Resp(
            200,
            [
                {"start": str(lo), "open": "1", "high": "2", "low": "0.5", "close": "1.5", "volume": "10"},
                {"start": str(lo), "open": "1", "high": "2", "low": "0.5", "close": "1.5", "volume": "10"},
            ],
        )

    start = datetime(2020, 1, 1, tzinfo=UTC)
    frame = sv.fetch_daily("BTC-USD", start, datetime(2020, 12, 1, tzinfo=UTC), get=get, sleep=lambda s: None)
    assert list(frame.columns) == ["ts", "open", "high", "low", "close", "volume"]
    assert len(frame) == 2  # two windows of 300 days / 45 days, one candle each (the duplicate is dropped)
    assert len(calls) == 3  # one retry after the 429
    assert all(c["granularity"] == "ONE_DAY" for c in calls)
    daily = sv.daily_from_usd(frame)
    assert daily["valid"].sum() == 2 and daily["V"].dropna().iloc[0] == pytest.approx(15.0)
