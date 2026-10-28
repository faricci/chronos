"""Synthetic tests of the pre-registered trend / rotation backtest (2026-10-28-ipotesi-strategie-laboratorio).

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

from script import study_trend_rotation as st

DAY = pd.Timedelta(days=1)
CUT = pd.Timestamp("2020-12-31")


def make_frame(close: np.ndarray, start: str, open_: np.ndarray | None = None) -> pd.DataFrame:
    """Daily frame; open = previous close unless given."""
    idx = pd.date_range(start, periods=len(close), freq="D")
    o = np.r_[close[0], close[:-1]] if open_ is None else open_
    return pd.DataFrame(
        {
            "open": o,
            "high": np.maximum(o, close) * 1.01,
            "low": np.minimum(o, close) * 0.99,
            "close": close,
            "valid": True,
        },
        index=idx,
    )


def random_walk(n: int, seed: int, vol: float = 0.03, drift: float = 0.0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    out: np.ndarray = 100.0 * np.exp(np.cumsum(rng.normal(drift, vol, n)))
    return out


def regime_walk(n: int, seed: int, flip: float = 1 / 30, mu: float = 0.008, vol: float = 0.03) -> np.ndarray:
    """Persistent up/down regimes: the injected trend effect."""
    rng = np.random.default_rng(seed)
    sign = 1.0
    r = np.empty(n)
    for i in range(n):
        if rng.random() < flip:
            sign = -sign
        r[i] = sign * mu + rng.normal(0.0, vol)
    out: np.ndarray = 100.0 * np.exp(np.cumsum(r))
    return out


def fake_result(
    hyp: str = "A",
    n_trades: int = 40,
    mean: float = 0.001,
    wo_best: float = 0.0005,
    ub: float = 0.002,
    dd: float = 0.2,
    ref_dd: float = 0.6,
) -> st.HypResult:
    test = st.MeanTest(1000, mean, mean - 0.001, ub, 0.01, 0.01, 2.5)
    return st.HypResult(hyp, "x", None, CUT, n_trades, test, wo_best, dd, ref_dd, 0.5, 0.4, 1.0)


# ------------------------------------------------------------------------------------- 1. no look-ahead


def test_trend_state_uses_only_closes_before_the_execution_day() -> None:
    close = random_walk(500, 1)
    frame = make_frame(close, "2018-01-01")
    days = pd.DatetimeIndex(frame.index[100:])
    base = st.trend_state(frame["close"], days)
    k = 250
    shocked = close.copy()
    shocked[k:] = shocked[k:] * np.random.default_rng(9).uniform(0.3, 3.0, len(shocked) - k)
    other = st.trend_state(make_frame(shocked, "2018-01-01")["close"], days)
    # the state of execution day d depends on closes <= d-1: days up to index k (inclusive) cannot change
    upto = k - 100 + 1
    assert base[:upto] == other[:upto]
    assert base != other
    # the open of the execution day is not an input of the signal
    wild_open = make_frame(close, "2018-01-01", open_=close * np.random.default_rng(3).uniform(0.5, 2.0, len(close)))
    assert st.trend_state(wild_open["close"], days) == base


def test_rotation_decision_ignores_the_day_of_the_rebalance() -> None:
    n = 400
    a = make_frame(random_walk(n, 11), "2019-01-01")
    b = make_frame(random_walk(n, 12), "2019-01-01")
    lo, hi = pd.Timestamp("2019-03-01"), pd.Timestamp("2019-12-31")
    base = st.rotation_paths({"BTC": a, "ETH": b}, lo, hi, ["BTC", "ETH"])
    assert base is not None
    d = pd.Timestamp("2019-07-01")
    shocked = b.copy()
    shocked.loc[shocked.index >= d, ["open", "close"]] *= 50.0  # a huge move from the rebalance day onwards
    other = st.rotation_paths({"BTC": a, "ETH": shocked}, lo, hi, ["BTC", "ETH"])
    assert other is not None
    first_diff = [x for x, y in zip(base.choices, other.choices, strict=True) if x != y]
    # the choice made on 2019-07-01 uses R28 of 2019-06-30: it is identical
    assert dict(base.choices)[d] == dict(other.choices)[d]
    # the choice of the next month sees the shock
    assert first_diff == [] or first_diff[0][0] > d


# --------------------------------------------------------------------- 2. execution at the next open


def test_entry_and_exit_are_executed_at_the_next_open() -> None:
    n = 100
    close = np.full(n, 100.0)
    close[60:70] = 110.0  # t=60 is the first close above the 50-day maximum
    close[70:] = 90.0  # t=70 is the first close below the 20-day minimum
    open_ = close * 1.001  # distinguishable from the close
    frame = make_frame(close, "2019-01-01", open_=open_)
    res = st.evaluate_a({"BTC": frame}, "train", None, frame.index[-1])
    assert res is not None
    assert res.n_trades == 1
    days = pd.DatetimeIndex(frame.index)
    pot = st.simulate_pot(
        days[51:],
        st.held_from_states("BTC", st.trend_state(frame["close"], days[51:])),
        {"BTC": frame["open"]},
        {"BTC": frame["close"]},
        st.COST_PRIMARY,
    )
    (trade,) = pot.trades
    assert trade.entry == days[61]
    assert trade.entry_px == pytest.approx(open_[61])
    assert trade.exit == days[71]
    assert trade.exit_px == pytest.approx(open_[71])
    assert not trade.forced


def test_donchian_needs_complete_windows_and_strict_inequality() -> None:
    close = pd.Series(np.full(80, 100.0), index=pd.date_range("2019-01-01", periods=80))
    enter, leave = st.donchian_flags(close)
    assert not enter.any() and not leave.any()  # equal is not a breakout
    close.iloc[60] = 100.01
    assert st.donchian_flags(close)[0].iloc[60]
    gap = close.copy()
    gap.iloc[30] = np.nan
    assert not st.donchian_flags(gap)[0].iloc[60]  # the 50-day window contains a missing day


def test_first_exec_day_is_after_the_first_complete_window() -> None:
    close = pd.Series(np.linspace(100, 200, 120), index=pd.date_range("2019-01-01", periods=120))
    assert st.first_exec_day_a(close) == close.index[st.ENTRY_W] + DAY


# ------------------------------------------------------------------------------------------- 3. costs


def test_costs_per_side_and_per_switch() -> None:
    days = pd.date_range("2020-01-01", periods=8)
    flat = pd.Series(100.0, index=days)
    opens = {"X": flat, "Y": flat}
    closes = {"X": flat, "Y": flat}
    held: list[str | None] = [None, "X", "X", "Y", "Y", None, "X", "X"]
    pot = st.simulate_pot(days, held, opens, closes, 0.011)
    cs = 0.0055
    r = pot.ret.to_numpy()
    assert r[0] == 0.0 and r[2] == 0.0 and r[4] == 0.0
    assert r[1] == pytest.approx(-cs)  # purchase from cash: one side
    assert r[3] == pytest.approx((1 - cs) ** 2 - 1)  # switch: sale + purchase = one round trip
    assert r[5] == pytest.approx(-cs)  # sale to cash
    assert r[6] == pytest.approx(-cs)
    assert r[7] == pytest.approx(-cs)  # forced close on the last day: the sale is charged
    assert [t.forced for t in pot.trades] == [False, False, True]
    assert all(t.net == pytest.approx((1 - cs) ** 2 - 1) for t in pot.trades)


def test_trade_net_matches_the_compounded_daily_series() -> None:
    n = 120
    frame = make_frame(regime_walk(n, 4, mu=0.01), "2019-01-01")
    days = pd.DatetimeIndex(frame.index[51:])
    held = st.held_from_states("BTC", st.trend_state(frame["close"], days))
    pot = st.simulate_pot(days, held, {"BTC": frame["open"]}, {"BTC": frame["close"]}, st.COST_PRIMARY)
    assert pot.trades
    assert float(np.prod(1 + pot.ret.to_numpy())) == pytest.approx(math.prod(1 + t.net for t in pot.trades))
    for t in pot.trades:
        assert t.net == pytest.approx((1 + t.gross) * (1 - st.COST_PRIMARY / 2) ** 2 - 1)


def test_secondary_cost_is_cheaper_than_primary() -> None:
    fr = {"BTC": make_frame(regime_walk(900, 5), "2016-01-01")}
    res = st.evaluate_a(fr, "train", None, fr["BTC"].index[-1])
    assert res is not None
    assert res.secondary["net77"]["mean"] > res.mean


def test_sleeves_are_never_transferred() -> None:
    days = pd.date_range("2020-01-01", periods=3)
    up = pd.Series([1.0, 2.0, 4.0], index=days)
    # one sleeve doubles every day, the other stays in cash: portfolio = (E1 + E2) / (E1' + E2')
    r1 = pd.Series([1.0, 1.0, 0.0], index=days)
    r2 = pd.Series([0.0, 0.0, 0.0], index=days)
    port = st.combine_sleeves([r1, r2])
    assert port.iloc[0] == pytest.approx((2 * 0.5 + 0.5) / 1.0 - 1)
    assert port.iloc[1] == pytest.approx((4 * 0.5 + 0.5) / (2 * 0.5 + 0.5) - 1)
    assert up.iloc[0] == 1.0


# ---------------------------------------------------------------------------------------- 4. rotation


def test_rotation_choice_rules() -> None:
    order = ["BTC", "ETH"]
    assert st.rotation_choice({"BTC": -0.1, "ETH": -0.2}, "BTC", order) is None  # best candidate <= 0: cash
    assert st.rotation_choice({"BTC": 0.0, "ETH": -0.2}, None, order) is None
    assert st.rotation_choice({"BTC": 0.1, "ETH": 0.2}, None, order) == "ETH"
    assert st.rotation_choice({"BTC": 0.2, "ETH": 0.2}, None, order) == "BTC"  # tie: first of the order
    # hysteresis: switch only if the gap is > 0.05
    assert st.rotation_choice({"BTC": 0.10, "ETH": 0.14}, "BTC", order) == "BTC"
    assert st.rotation_choice({"BTC": 0.10, "ETH": 0.16}, "BTC", order) == "ETH"
    assert st.rotation_choice({"BTC": 0.10, "ETH": 0.15 + 1e-12}, "BTC", order) == "ETH"
    # the held coin has R28 <= 0: no hysteresis
    assert st.rotation_choice({"BTC": -0.01, "ETH": 0.02}, "BTC", order) == "ETH"
    # a missing R28 excludes the coin
    assert st.rotation_choice({"BTC": float("nan"), "ETH": 0.02}, "BTC", order) == "ETH"
    assert st.rotation_choice({"BTC": float("nan"), "ETH": float("nan")}, "BTC", order) is None


def test_rotation_paths_rebalance_monthly_from_the_previous_day() -> None:
    n = 150
    # BTC trends up, ETH trends down: B holds BTC, B0 invests only in BTC
    btc = make_frame(100.0 * 1.01 ** np.arange(n), "2020-01-01")
    eth = make_frame(100.0 * 0.99 ** np.arange(n), "2020-01-01")
    paths = st.rotation_paths(
        {"BTC": btc, "ETH": eth}, pd.Timestamp("2020-01-01"), pd.Timestamp("2020-05-29"), ["BTC", "ETH"]
    )
    assert paths is not None
    assert paths.days[0] == pd.Timestamp("2020-02-01")  # first month start with R28 for every coin (t = 2020-01-31)
    assert set(paths.b_held) == {"BTC"}
    assert set(paths.b0_held["BTC"]) == {"BTC"} and set(paths.b0_held["ETH"]) == {None}
    assert [d.month for d, _ in paths.choices] == [2, 3, 4, 5]


# --------------------------------------------------------------------------------------- 5. statistics


def test_stationary_bootstrap_is_reproducible_and_block_aware() -> None:
    rng = np.random.default_rng(0)
    x = rng.normal(0.0, 1.0, 2000)
    m1 = st.stationary_means(x, 2000, 20, 7)
    m2 = st.stationary_means(x, 2000, 20, 7)
    assert np.array_equal(m1, m2)
    assert m1.mean() == pytest.approx(x.mean(), abs=0.01)
    assert m1.std() == pytest.approx(1.0 / math.sqrt(2000), rel=0.15)  # iid: close to the naive standard error
    # positively autocorrelated series: the block bootstrap sees a larger error than the naive one
    e = rng.normal(0.0, 1.0, 2000)
    ar = np.empty(2000)
    ar[0] = e[0]
    for i in range(1, 2000):
        ar[i] = 0.7 * ar[i - 1] + e[i]
    naive = ar.std(ddof=1) / math.sqrt(len(ar))
    assert st.stationary_means(ar, 2000, 20, 7).std() > 1.5 * naive


def test_hac_standard_error() -> None:
    rng = np.random.default_rng(1)
    x = rng.normal(0.0, 1.0, 5000)
    _, se = st.hac_t(x)
    assert se == pytest.approx(x.std(ddof=1) / math.sqrt(5000), rel=0.15)
    ar = np.empty(5000)
    ar[0] = 0.0
    for i in range(1, 5000):
        ar[i] = 0.7 * ar[i - 1] + rng.normal()
    assert st.hac_t(ar)[1] > 1.5 * ar.std(ddof=1) / math.sqrt(5000)


def test_mean_test_detects_a_drift_and_not_noise() -> None:
    rng = np.random.default_rng(2)
    drift = st.mean_test(rng.normal(0.002, 0.01, 1500), 5, B=2000)
    assert drift.p < 0.001 and drift.lb < drift.mean < drift.ub
    noise = st.mean_test(rng.normal(0.0, 0.01, 1500), 5, B=2000)
    assert noise.p > 0.05
    assert st.mean_test([0.1], 5).p == 1.0  # too short: never significant
    assert drift.p == max(drift.p_boot, drift.p_hac)


def test_drawdown_and_year_exclusion() -> None:
    assert st.max_drawdown([0.1, -0.5, 0.1]) == pytest.approx(0.5)
    assert st.max_drawdown([0.1, 0.1]) == 0.0
    idx = pd.date_range("2018-12-30", periods=6)
    x = pd.Series([0.01, 0.01, 0.10, 0.10, -0.01, -0.01], index=idx)  # 2019 is the best year
    assert st.mean_without_best_year(x) == pytest.approx(0.01)
    assert math.isnan(st.mean_without_best_year(x.iloc[2:4]))  # a single year: nothing is left


def test_holm_family_with_untested_hypotheses() -> None:
    assert st.holm([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])
    fam = st.holm_family(0.002, 0.5)
    assert fam["A"] == pytest.approx(4 * 0.002)  # worst case: C and D at p = 1
    assert fam["B"] == pytest.approx(1.0)
    assert fam["C"] == fam["D"] == 1.0
    assert st.holm_family(0.0125 - 1e-9, 1.0)["A"] < 0.05 <= st.holm_family(0.0125 + 1e-6, 1.0)["A"]


def test_undersized_training_enters_holm_with_p_one() -> None:
    assert st.train_p(None) == 1.0
    assert st.train_p(fake_result(n_trades=29)) == 1.0
    assert st.train_p(fake_result(n_trades=30)) == 0.01


# ------------------------------------------------------------------------------------ 6. verdict table


def test_verdict_table() -> None:
    train = fake_result()
    hold = fake_result(mean=0.0006, dd=0.2, ref_dd=0.6)
    ok = st.decide("A", train, hold, 0.01, fake_result(mean=0.0002))
    assert ok.verdict == "GO" and not ok.failed

    assert st.decide("A", fake_result(n_trades=29), hold, 0.01, None).verdict == "INCONCLUSO"
    assert st.decide("A", train, fake_result(n_trades=14), 0.01, None).failed == ["INS_n"]
    assert st.decide("A", None, hold, 0.01, None).failed == ["INS_n"]
    # sign is not looked at when n is too small
    assert st.decide("A", fake_result(n_trades=5, mean=-0.01), hold, 0.01, None).verdict == "INCONCLUSO"

    assert st.decide("A", fake_result(mean=0.0), hold, 0.001, None).verdict == "NO-GO"
    assert st.decide("A", fake_result(mean=-0.001), hold, 0.9, None).verdict == "NO-GO"
    neg_hold = fake_result(mean=-0.0001)
    assert st.decide("A", train, neg_hold, 0.01, None).verdict == "NO-GO"  # (a) true and holdout < 0
    assert st.decide("A", train, neg_hold, 0.2, None).verdict == "INCONCLUSO"  # (a) false: not a NO-GO

    assert "a_pHolm_train<0.05" in st.decide("A", train, hold, 0.06, None).failed
    weak = fake_result(mean=0.0003)  # positive but below a third of the training estimate (0.00033)
    assert st.decide("A", train, weak, 0.01, None).verdict == "INCONCLUSO"
    assert st.decide("A", train, fake_result(mean=0.0004), 0.01, None).verdict == "GO"
    no_year = fake_result(wo_best=-0.0001)
    assert "c_train_without_best_year>0" in st.decide("A", no_year, hold, 0.01, None).failed

    # replica: blocks only with mean < 0 and upper bound < 0
    blocking = fake_result(mean=-0.002, ub=-0.0005)
    assert st.decide("A", train, hold, 0.01, blocking).verdict == "INCONCLUSO"
    soft = fake_result(mean=-0.002, ub=0.001)
    assert st.decide("A", train, hold, 0.01, soft).verdict == "GO"

    # drawdown condition applies only to A
    deep = fake_result(mean=0.0006, dd=0.5, ref_dd=0.6)
    assert "e_holdout_dd<=0.6_buy_and_hold_dd" in st.decide("A", train, deep, 0.01, None).failed
    assert st.decide("B", fake_result("B"), deep, 0.01, None).verdict == "GO"
    edge = fake_result(mean=0.0006, dd=0.36, ref_dd=0.6)
    assert st.decide("A", train, edge, 0.01, None).verdict == "GO"

    # a contaminated holdout can only give a candidate
    assert st.decide("A", train, hold, 0.01, None, holdout_clean=False).verdict == "CANDIDATO"


# --------------------------------------------------------------- 7. thresholds only from training data


def test_training_results_do_not_depend_on_later_data() -> None:
    fr = {
        "BTC": make_frame(regime_walk(3300, 1), "2015-07-20"),
        "ETH": make_frame(regime_walk(3300, 2), "2016-05-18"),
    }
    cut_frames = {c: f.loc[:CUT] for c, f in fr.items()}
    wild = {c: f.copy() for c, f in fr.items()}
    for f in wild.values():
        f.loc[f.index > CUT, ["open", "high", "low", "close"]] *= 7.0
    for split_frames in (fr, wild):
        a_full = st.evaluate_a({c: f.loc[:CUT] for c, f in split_frames.items()}, "train", None, CUT)
        a_cut = st.evaluate_a(cut_frames, "train", None, CUT)
        assert a_full is not None and a_cut is not None
        assert a_full.test == a_cut.test and a_full.n_trades == a_cut.n_trades
    # even if the frame handed over contains later rows, the training window ends at ``hi``
    a_long = st.evaluate_a(fr, "train", None, CUT)
    a_short = st.evaluate_a(cut_frames, "train", None, CUT)
    assert a_long is not None and a_short is not None
    assert a_long.test == a_short.test and a_long.max_dd == a_short.max_dd
    b_long = st.evaluate_b(fr, "train", fr["BTC"].index[0], CUT)
    b_short = st.evaluate_b(cut_frames, "train", fr["BTC"].index[0], CUT)
    assert b_long is not None and b_short is not None
    assert b_long.test == b_short.test and b_long.n_trades == b_short.n_trades


def test_load_usd_cuts_rows_at_read_time(tmp_path: Path) -> None:
    ts = pd.date_range("2020-12-20", periods=20, freq="D", tz="UTC")
    c = np.linspace(100, 120, 20)
    pd.DataFrame({"ts": ts, "open": c, "high": c + 1, "low": c - 1, "close": c, "volume": 1.0}).to_parquet(
        tmp_path / "BTC-USD_1d.parquet", index=False
    )
    cut = st.load_usd(tmp_path / "BTC-USD_1d.parquet", CUT)
    assert cut.index[-1] == CUT
    assert st.load_usd(tmp_path / "BTC-USD_1d.parquet", None).index[-1] == pd.Timestamp("2021-01-08")


# ------------------------------------------------------------------------------- 8. end to end


def build_world(effect: bool, seed: int) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    gen: Any = (lambda n, sd: regime_walk(n, sd, flip=1 / 20, mu=0.025)) if effect else random_walk
    usd = {
        "BTC": make_frame(gen(3700, seed), "2015-07-20"),
        "ETH": make_frame(gen(3700, seed + 1), "2016-05-18"),
    }
    eur = {
        "BTC": make_frame(gen(1000, seed + 2), "2023-06-01"),
        "ETH": make_frame(gen(1000, seed + 3), "2023-06-01"),
        "SOL": make_frame(gen(1000, seed + 4), "2023-06-01"),
    }
    return usd, eur


class Spy:
    """Env with synthetic loaders; records the order of reads against the files written."""

    def __init__(self, folder: Path, usd: dict[str, pd.DataFrame], eur: dict[str, pd.DataFrame], clean: bool = True):
        self.folder = folder
        self.usd, self.eur, self.clean = usd, eur, clean
        self.events: list[tuple[str, bool, bool]] = []

    def usd_loader(self, path: Path, end: pd.Timestamp | None) -> pd.DataFrame:
        coin = path.name.split("-")[0]
        f = self.usd[coin]
        self.events.append(
            (
                "usd_train" if end is not None else "usd_full",
                (self.folder / "00_preregistration.json").exists(),
                (self.folder / "03_train_hash.json").exists(),
            )
        )
        return f.loc[:end] if end is not None else f

    def replica_loader(self, root: Path, product: str) -> pd.DataFrame:
        self.events.append(("replica", True, (self.folder / "03_train_hash.json").exists()))
        return self.eur[product.split("-")[0]]

    def env(self) -> st.Env:
        return st.Env(
            head=lambda: "deadbeef",
            script_clean=lambda: self.clean,
            now=lambda: datetime(2026, 10, 28, tzinfo=UTC),
            usd_loader=self.usd_loader,
            replica_loader=self.replica_loader,
        )


def run_world(tmp_path: Path, effect: bool, seed: int) -> tuple[Spy, Path, int]:
    usd, eur = build_world(effect, seed)
    folder = tmp_path / "out" / st.ARM
    spy = Spy(folder, usd, eur)
    code = st.run_study(st.RunArgs(tmp_path / "bars", tmp_path / "usd", tmp_path / "out"), spy.env())
    return spy, folder, code


def read(folder: Path, name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((folder / name).read_text(encoding="utf-8"))
    return loaded


def test_end_to_end_with_injected_trend_effect(tmp_path: Path) -> None:
    spy, folder, code = run_world(tmp_path, True, 21)
    assert code == 0
    train = read(folder, "02_train_results.json")["A"]
    hold = read(folder, "04_holdout_results.json")["holdout"]["A"]
    dec = read(folder, "06_decision.json")
    assert train["mean_daily_net"] > 0 and train["p"] < 0.0125
    assert hold["mean_daily_net"] > 0
    assert dec["A"]["verdict"] == "GO", dec["A"]
    assert dec["p_holm_train"]["A"] < 0.05 and dec["p_holm_train"]["C"] == 1.0


def test_end_to_end_without_effect_is_not_a_go(tmp_path: Path) -> None:
    verdicts = []
    for seed in (31, 41, 51):
        spy, folder, code = run_world(tmp_path / str(seed), False, seed)
        assert code == 0
        dec = read(folder, "06_decision.json")
        verdicts.append((dec["A"]["verdict"], dec["B"]["verdict"]))
    assert all(a != "GO" and b != "GO" for a, b in verdicts), verdicts


def test_run_order_and_files(tmp_path: Path) -> None:
    spy, folder, code = run_world(tmp_path, True, 21)
    assert code == 0
    # the holdout (full data) and the replica are read only after 03_train_hash.json exists; 00 exists before any read
    assert all(pre for _, pre, _ in spy.events)
    for kind, _, hashed in spy.events:
        assert hashed == (kind != "usd_train"), (kind, hashed)
    assert sorted(p.name for p in folder.iterdir()) == [
        "00_preregistration.json",
        "01_data.json",
        "02_train_results.json",
        "03_train_hash.json",
        "04_holdout_results.json",
        "05_secondary.json",
        "06_decision.json",
    ]
    import hashlib

    h = read(folder, "03_train_hash.json")
    assert h["sha256"] == hashlib.sha256((folder / "02_train_results.json").read_bytes()).hexdigest()
    pre = read(folder, "00_preregistration.json")
    assert pre["prereg_sha256"] == st.prereg_sha256() and pre["prereg"] == st.PREREG
    assert pre["script_sha256"] and pre["document_sha256"]


def test_run_refuses_a_second_run_and_a_dirty_script(tmp_path: Path) -> None:
    spy, folder, code = run_world(tmp_path, True, 21)
    assert code == 0
    again = st.run_study(st.RunArgs(tmp_path / "bars", tmp_path / "usd", tmp_path / "out"), spy.env())
    assert again == st.EXIT_REFUSED
    usd, eur = build_world(True, 21)
    dirty = Spy(tmp_path / "o2" / st.ARM, usd, eur, clean=False)
    code = st.run_study(st.RunArgs(tmp_path / "bars", tmp_path / "usd", tmp_path / "o2"), dirty.env())
    assert code == st.EXIT_REFUSED and dirty.events == [] and not (tmp_path / "o2").exists()


def test_json_files_have_no_nan_and_are_written_once(tmp_path: Path) -> None:
    _, folder, _ = run_world(tmp_path, False, 61)
    for p in folder.iterdir():
        json.loads(p.read_text(encoding="utf-8"), parse_constant=_fail_on_constant)
    with pytest.raises(FileExistsError):
        st._write_new(folder / "06_decision.json", {})


def test_script_is_clean_detects_untracked_files(tmp_path: Path) -> None:
    assert st.script_is_clean("script/does_not_exist.py") is False


def leadership_pair(
    seed: int, effect: bool, mu: float = 0.01, flip: float = 1 / 60, n: int = 2200
) -> dict[str, pd.DataFrame]:
    """Two coins with one shared noise; with ``effect`` the relative leadership persists (the injected B effect)."""
    rng = np.random.default_rng(seed)
    common = rng.normal(0.0005, 0.02, n)
    sign = 1.0
    rel = np.zeros(n)
    for i in range(n):
        if rng.random() < flip:
            sign = -sign
        rel[i] = sign * mu if effect else 0.0
    x = common + rel + rng.normal(0.0, 0.01, n)
    y = common - rel + rng.normal(0.0, 0.01, n)
    return {
        "BTC": make_frame(100.0 * np.exp(np.cumsum(x)), "2015-07-20"),
        "ETH": make_frame(100.0 * np.exp(np.cumsum(y)), "2015-07-20"),
    }


def test_rotation_with_and_without_injected_leadership() -> None:
    end = pd.Timestamp("2021-08-01")
    for seed in (1, 3):
        fr = leadership_pair(seed, True)
        res = st.evaluate_b(fr, "train", fr["BTC"].index[0], end)
        assert res is not None and res.mean > 0 and res.test.p < 0.0125
    for seed in (1, 2, 3):
        fr = leadership_pair(seed, False)
        res = st.evaluate_b(fr, "train", fr["BTC"].index[0], end)
        assert res is not None and res.test.p > 0.05


def _fail_on_constant(c: str) -> Any:
    pytest.fail(f"non-finite constant {c} in a report file")
