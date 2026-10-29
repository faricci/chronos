"""Feasibility study v2.7: detrend, selection window, N_eff / rho, choice rule, jackknife, chi2 and the GO verdict."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from scipy import stats

from modules.config import AppConfig
from modules.market.backfill import BarStore
from modules.risk.costs import CostParams, FeeRates
from modules.risk.label_config import LabelConfig, load_label_config
from tests.indicator_fixtures import synthetic_bars
from training import feasibility as fz
from training.feasibility import (
    EXIT_NO_GO,
    ComboResult,
    Metrics,
    _detrend,
    _Raw,
    build_product,
    choose,
    evaluate,
    family,
    grid,
    homogeneity_chi2,
    ineligible_reason,
    jackknife_lift0,
    label_spreads,
    main,
    pairwise_rho,
    pooled_n_eff,
    product_n_eff,
    saturation,
    semester_of,
    verdict,
)
from training.labeling import NO_FILL, UPPER, VERTICAL, label_window_end
from training.sampling import uniqueness_weights
from training.splits import holdout_start_from

FEES = FeeRates(maker_bps=25, taker_bps=50, assumed=False)
COSTS = CostParams()


# ------------------------------------------------------------------------------------ fixtures


def metrics(**kw: float) -> Metrics:
    base: dict[str, float] = {
        "n": 1000, "filled": 900, "fill_rate": 0.9, "p_hist": 0.4, "p_breakeven": 0.45, "lift": 0.05, "lift_rw": 0.05,
        "fee_floor_share": 1.0, "p_vertical": 0.04, "vert_mean_net_bps": 10.0, "n_eff": 100.0, "se_p_hist": 0.03,
        "indep_per_year": 30.0, "dur_mean_min": 4000.0, "dur_median_min": 3000.0, "dur_p90_min": 8000.0,
        "dur_p95_min": 9000.0, "sigma_r_bps": 300.0, "wl_bps": 400.0,
    }  # fmt: skip
    base.update(kw)
    return Metrics(**base)  # type: ignore[arg-type]


def combo(
    lift: float = 0.05,
    days: int = 15,
    tf: str = "6h",
    ktp: float = 3.0,
    ksl: float = 2.0,
    mode: str = "maker",
    **m: float,
) -> ComboResult:
    cfg = LabelConfig(atr_tf=tf, k_tp=ktp, k_sl=ksl, entry_mode=mode, horizon_min=days * 1440)  # type: ignore[arg-type]
    d = metrics(lift=lift, **m)
    return ComboResult(cfg, d, metrics(lift=lift + 0.01), {"BTC-EUR": (d, d)}, 0.5, 0.5)


def make_raw(outcome: list[int], ts: pd.DatetimeIndex, p_be: float = 0.5, exit_after: int = 1) -> _Raw:
    n = len(outcome)
    t_idx = np.arange(n, dtype=np.int64) * 60  # hourly samples on a 1m grid
    out = np.array(outcome, dtype=np.int8)
    fill = np.where(out != NO_FILL, t_idx + 3, -1).astype(np.int32)
    exit_ = np.where(out != NO_FILL, t_idx + 3 + exit_after, -1).astype(np.int32)
    filled = out != NO_FILL
    w = np.full(n, np.nan)
    if filled.any():
        w[filled] = uniqueness_weights(t_idx[filled], exit_[filled].astype(np.int64))
    return _Raw(out, np.where(filled, 0.01, np.nan), np.full(n, p_be), np.ones(n, dtype=bool), t_idx, fill, exit_,
                ts.to_numpy(), w)  # fmt: skip


def hourly(n: int, start: str = "2024-01-01") -> pd.DatetimeIndex:
    return pd.date_range(start, periods=n, freq="1h", tz="UTC").tz_localize(None)


# ------------------------------------------------------------------------------------ detrend


def test_detrend_removes_a_constant_drift_and_keeps_the_bar_shape() -> None:
    n = 5000
    idx = pd.date_range("2024-01-01", periods=n, freq="1min", tz="UTC", name="ts")
    close = 100.0 * np.exp(3e-5 * np.arange(n))
    bars = pd.DataFrame({"open": close * 0.999, "high": close * 1.002, "low": close * 0.997, "close": close,
                         "volume": np.arange(n, dtype=float)}, index=idx)  # fmt: skip
    out, mu = _detrend(bars)
    assert mu == pytest.approx(3e-5, rel=1e-9)
    assert out["close"].iloc[-1] == pytest.approx(out["close"].iloc[0], rel=1e-9)
    assert ((out["low"] <= out["open"]) & (out["open"] <= out["high"]) & (out["low"] <= out["close"])).all()
    assert (out["close"] <= out["high"]).all()
    pd.testing.assert_series_equal(out["volume"], bars["volume"])
    assert bars["close"].iloc[-1] > bars["close"].iloc[0] * 1.1  # the input is untouched


def test_detrend_requires_a_contiguous_frame() -> None:
    idx = pd.date_range("2024-01-01", periods=10, freq="1min", tz="UTC").delete(4)
    bars = pd.DataFrame({k: np.ones(9) for k in ("open", "high", "low", "close", "volume")}, index=idx)
    with pytest.raises(ValueError):
        _detrend(bars)
    with pytest.raises(ValueError):
        _detrend(bars.iloc[:1])


# ------------------------------------------------------------------------- selection window


@pytest.fixture(scope="module")
def bars() -> pd.DataFrame:
    return synthetic_bars(days=60, zero_volume_day=None, constant_days=None)


def test_nothing_at_or_after_the_cut_is_used(bars: pd.DataFrame) -> None:
    cfg_ref = LabelConfig(atr_tf="6h", horizon_min=5 * 1440)
    cut = pd.Timestamp(bars.index[0]) + pd.Timedelta(days=40)
    p = build_product(bars, "SYN-EUR", 3.0, cut=cut, cfg_ref=cfg_ref)
    ts = pd.DatetimeIndex(p.ts_close)  # naive UTC
    cut_naive = cut.tz_convert(None) if cut.tzinfo is not None else cut
    assert ts.max() < cut_naive
    assert len(p.raw.c) == len(ts) and len(p.detr.c) == len(ts)
    assert (label_window_end(ts[p.samples], cfg_ref) < cut_naive).all()
    assert len(p.samples) > 100


def test_mu_comes_from_the_window_only(bars: pd.DataFrame) -> None:
    cfg_ref = LabelConfig(atr_tf="6h", horizon_min=2 * 1440)
    a = build_product(bars, "A", 3.0, cut=pd.Timestamp(bars.index[0]) + pd.Timedelta(days=30), cfg_ref=cfg_ref)
    b = build_product(bars, "B", 3.0, cut=pd.Timestamp(bars.index[0]) + pd.Timedelta(days=50), cfg_ref=cfg_ref)
    n = len(a.raw.c)
    expected = (math.log(bars["close"].iloc[n - 1]) - math.log(bars["close"].iloc[0])) / (n - 1)
    assert a.mu_per_min == pytest.approx(expected, rel=1e-9) and a.mu_per_min != b.mu_per_min


def test_holdout_start_is_the_single_definition() -> None:
    last = pd.Timestamp("2026-10-18 06:08", tz="UTC")
    assert holdout_start_from(last, fz.HOLDOUT_DAYS) == pd.Timestamp("2026-06-20", tz="UTC")
    assert fz.H_MAX_DAYS == 15 and fz.EMBARGO.days == 2 and max(fz.HORIZON_DAYS) == 20


def test_grid_has_96_combinations_and_20_days_is_never_eligible() -> None:
    g = grid()
    assert len(g) == 2 * 3 * 2 * 2 * 4 == len(set(c.sha256(FEES, COSTS, {"X": 3.0}) for c in g))
    assert ineligible_reason(combo(days=20)) == "h_gt_hmax"


# ------------------------------------------------------------------------------ N_p and rho


def test_n_p_counts_fills_when_trades_do_not_overlap() -> None:
    raw = make_raw([UPPER] * 20, hourly(20), exit_after=10)  # each trade lasts 10 minutes, samples are hourly
    assert product_n_eff(raw) == pytest.approx(20.0)
    assert product_n_eff(make_raw([NO_FILL] * 5, hourly(5))) == 0.0


def test_n_p_does_not_depend_on_the_vertical_horizon_only_on_the_real_span() -> None:
    short = make_raw([UPPER] * 20, hourly(20), exit_after=30)
    assert product_n_eff(short) == pytest.approx(20.0)


def test_n_p_of_fully_overlapping_trades_is_about_one() -> None:
    raw = make_raw([UPPER] * 10, hourly(10), exit_after=100_000)
    assert product_n_eff(raw) == pytest.approx(1.0, abs=0.35)  # ten trades that all live through the whole span


def test_rho_of_identical_outcomes_is_one_and_the_pool_is_halved() -> None:
    ts = hourly(60)
    out = ([UPPER, 2, UPPER, 2, 2, UPPER] * 10)[:60]
    a, b = make_raw(out, ts), make_raw(out, ts)
    rho = pairwise_rho({"A": a, "B": b})
    assert rho == pytest.approx(1.0)
    assert pooled_n_eff([product_n_eff(a), product_n_eff(b)], rho) == pytest.approx((60 + 60) / 2)


def test_rho_of_opposite_outcomes_is_truncated_to_zero() -> None:
    ts = hourly(60)
    out = ([UPPER, 2, UPPER, 2, 2, UPPER] * 10)[:60]
    flipped = [2 if o == UPPER else UPPER for o in out]
    rho = pairwise_rho({"A": make_raw(out, ts), "B": make_raw(flipped, ts)})
    assert rho == 0.0


def test_rho_with_too_few_common_points_is_conservative() -> None:
    a = make_raw([UPPER, 2] * 10, hourly(20))
    b = make_raw([UPPER, 2] * 10, hourly(20, start="2030-01-01"))  # no common timestamps
    assert pairwise_rho({"A": a, "B": b}) == 1.0
    assert pairwise_rho({"A": a}) == 0.0  # a single product: nothing to correct


def test_rho_aligns_by_timestamp_not_by_position() -> None:
    ts = hourly(80)
    out = ([UPPER, 2, 2, UPPER, 2] * 16)[:80]
    a = make_raw(out, ts)
    b = make_raw(out[10:70], ts[10:70])  # same series, shifted in position
    assert pairwise_rho({"A": a, "B": b}) == pytest.approx(1.0)


# --------------------------------------------------------------------------------- choose


def test_p_vertical_above_5_percent_is_excluded() -> None:
    assert ineligible_reason(combo(p_vertical=0.06)) == "p_vertical"
    assert ineligible_reason(combo(p_vertical=0.05)) == ""
    assert ineligible_reason(combo(fee_floor_share=0.79)) == "fee_floor"
    assert ineligible_reason(combo(fill_rate=0.49)) == "fill"
    assert ineligible_reason(combo(fill_rate=0.49, mode="taker")) == ""
    assert ineligible_reason(combo(lift=float("nan"))) == "fee_floor"


def test_the_smallest_eligible_h_represents_each_family() -> None:
    results = [combo(0.05, days=15), combo(0.09, days=5), combo(0.07, days=10), combo(0.01, days=20)]
    best, tie = choose(results)
    assert best is not None and best.cfg.horizon_min == 5 * 1440 and len(tie) == 1  # H=5 beats the better lift of H=15


def test_h20_is_never_chosen_even_with_the_best_lift() -> None:
    best, _ = choose([combo(0.30, days=15), combo(0.00, days=20)])
    assert best is not None and best.cfg.horizon_min == 15 * 1440


def test_within_the_tie_the_larger_n_eff_wins() -> None:
    a = combo(0.050, ktp=3.0, ksl=2.0, n_eff=80)
    b = combo(0.058, ktp=3.0, ksl=1.5, n_eff=140)  # 0.008 above the minimum: tied
    c = combo(0.070, ktp=4.0, ksl=2.0, n_eff=500)  # outside the tie
    best, tie = choose([a, b, c])
    assert best is b and set(map(id, tie)) == {id(a), id(b)}


def test_exact_ties_resolve_in_a_fixed_order() -> None:
    a = combo(0.05, ktp=3.0, ksl=2.0, n_eff=100)
    b = combo(0.05, ktp=3.0, ksl=1.5, n_eff=100)
    first = choose([a, b])[0]
    assert first is choose([b, a])[0] and first is b  # smaller k_sl sorts first


def test_nothing_eligible_gives_none() -> None:
    assert choose([combo(p_vertical=0.5)]) == (None, [])


def test_family_key() -> None:
    assert family(combo().cfg) == ("6h", 3.0, 2.0, "maker")


def test_saturation_flags_a_drop_of_more_than_0_02() -> None:
    def raw_lift(days: int, lift: float) -> ComboResult:
        c = combo(0.05, days=days)
        return ComboResult(c.cfg, c.detr, metrics(lift=lift), c.per_product, 0.5, 0.5)

    results = [raw_lift(15, 0.060), raw_lift(20, 0.030), raw_lift(10, 0.1)]
    tie = [raw_lift(15, 0.060)]
    ((key, (l15, l20, dominated)),) = saturation(results, tie).items()
    assert (l15, l20, dominated) == (0.060, 0.030, True) and key.startswith("6h/3.0/2.0/maker")
    results[1] = raw_lift(20, 0.050)
    assert next(iter(saturation(results, tie).values()))[2] is False


# ------------------------------------------------------------------------- jackknife / chi2


def test_semesters() -> None:
    got = semester_of(pd.DatetimeIndex(["2024-01-31", "2024-06-30 23:59", "2024-07-01", "2025-12-31"]))
    assert got.tolist() == ["2024H1", "2024H1", "2024H2", "2025H2"]


def test_jackknife_by_hand() -> None:
    # 2024H1: 10 fills, 8 UPPER; 2024H2: 10 fills, 2 UPPER ; p_be 0.5 everywhere
    ts = pd.DatetimeIndex(["2024-03-01"] * 10 + ["2024-09-01"] * 10)
    out = [UPPER] * 8 + [2] * 2 + [UPPER] * 2 + [2] * 8
    raw = make_raw(out, ts)
    got = jackknife_lift0({"A": raw})
    assert got["2024H1"] == pytest.approx(0.5 - 0.2)  # without H1 only H2 remains: p_hist 0.2
    assert got["2024H2"] == pytest.approx(0.5 - 0.8)


def test_homogeneous_data_give_chi2_zero_and_p_one() -> None:
    chi2, df, p, excluded = homogeneity_chi2(np.array([20.0, 40.0, 60.0]), np.array([100.0, 200.0, 300.0]))
    assert chi2 == pytest.approx(0.0, abs=1e-12) and df == 2 and p == pytest.approx(1.0) and excluded == []


def test_chi2_matches_a_hand_calculation_and_scipy() -> None:
    o, n = np.array([10.0, 30.0]), np.array([50.0, 50.0])
    chi2, df, p, _ = homogeneity_chi2(o, n)
    assert chi2 == pytest.approx(2 * 100 / 12) and df == 1
    assert p == pytest.approx(stats.chi2.sf(2 * 100 / 12, 1))


def test_chi2_excludes_periods_with_a_small_expected_count() -> None:
    chi2, df, _, excluded = homogeneity_chi2(np.array([10.0, 30.0, 1.0]), np.array([50.0, 50.0, 4.0]))
    assert excluded == [2] and df == 1
    assert math.isnan(homogeneity_chi2(np.array([10.0, 1.0]), np.array([50.0, 4.0]))[0])


# ----------------------------------------------------------------------------------- verdict


def test_verdict_go() -> None:
    best = combo(0.05)
    assert verdict(best, 0.07, {"BTC-EUR": metrics(lift=0.06)}) == (True, [])


@pytest.mark.parametrize(
    ("best", "jack", "products", "reason"),
    [
        (None, 0.0, {}, "no_eligible"),
        (combo(0.11), 0.05, {"BTC-EUR": metrics(lift=0.05)}, "lift0"),
        (combo(0.05), 0.12, {"BTC-EUR": metrics(lift=0.05)}, "jackknife"),
        (combo(0.05), 0.05, {"BTC-EUR": metrics(lift=0.05), "SOL-EUR": metrics(lift=0.20, se_p_hist=0.03)},
         "product_lift:SOL-EUR"),
    ],
)  # fmt: skip
def test_each_failure_reason(best: ComboResult | None, jack: float, products: dict[str, Metrics], reason: str) -> None:
    ok, reasons = verdict(best, jack, products)
    assert not ok and reasons == [reason]


def test_a_product_gets_a_two_se_margin() -> None:
    assert verdict(combo(0.05), 0.05, {"X": metrics(lift=0.15, se_p_hist=0.03)})[0] is True  # 0.10 + 2 * 0.03 = 0.16
    assert verdict(combo(0.05), 0.05, {"X": metrics(lift=0.17, se_p_hist=0.03)})[0] is False


def test_nan_jackknife_is_not_a_go() -> None:
    assert verdict(combo(0.05), float("nan"), {})[0] is False


# --------------------------------------------------------------- evaluate on synthetic bars


def test_evaluate_reports_both_series_and_consistent_metrics(bars: pd.DataFrame) -> None:
    cfg_ref = LabelConfig(atr_tf="6h", horizon_min=3 * 1440)
    p = build_product(bars, "SYN-EUR", 3.0, cut=None, cfg_ref=cfg_ref)
    cfg = LabelConfig(atr_tf="6h", k_tp=3.0, k_sl=2.0, entry_mode="taker", horizon_min=3 * 1440)
    r = evaluate(cfg, [p], FEES, COSTS, 30.0)
    for m in (r.detr, r.raw):
        assert m.fill_rate == 1.0 and m.n == len(p.samples)
        assert m.lift == pytest.approx(m.p_breakeven - m.p_hist)
        assert m.lift_rw == pytest.approx(m.p_breakeven - 2 / 5)
        assert 0 < m.n_eff <= m.filled and m.dur_p95_min >= m.dur_median_min > 0
    assert r.rho_detr == 0.0  # one product
    assert r.per_product["SYN-EUR"][0].n_eff == pytest.approx(r.detr.n_eff)


def test_a_drift_changes_the_raw_lift_but_much_less_the_detrended_one() -> None:
    base = synthetic_bars(days=80, zero_volume_day=None, constant_days=None, seed=5)
    up = base.copy()
    f = np.exp(8e-6 * np.arange(len(up)))  # +~18% over the window
    for k in ("open", "high", "low", "close"):
        up[k] = up[k].to_numpy() * f
    cfg_ref = LabelConfig(atr_tf="6h", horizon_min=3 * 1440)
    cfg = LabelConfig(atr_tf="6h", k_tp=3.0, k_sl=2.0, entry_mode="taker", horizon_min=3 * 1440)
    flat = evaluate(cfg, [build_product(base, "A", 3.0, cut=None, cfg_ref=cfg_ref)], FEES, COSTS, 30.0)
    drifted = evaluate(cfg, [build_product(up, "A", 3.0, cut=None, cfg_ref=cfg_ref)], FEES, COSTS, 30.0)
    assert drifted.raw.p_hist > flat.raw.p_hist + 0.02  # the drift inflates the raw hit rate
    assert abs(drifted.detr.p_hist - flat.detr.p_hist) < abs(drifted.raw.p_hist - flat.raw.p_hist)


# ------------------------------------------------------------------------- spread / probe / cli


def test_label_spread_is_the_probe_value_with_a_floor() -> None:
    spreads, src = label_spreads(["BTC-EUR", "SOL-EUR"], {"BTC-EUR": 1.09, "SOL-EUR": 5.2}, 3.0, None)
    assert spreads == {"BTC-EUR": 3.0, "SOL-EUR": 5.2}
    assert src["BTC-EUR"] == {"probe": 1.09, "floor": 3.0}
    assert label_spreads(["BTC-EUR"], {}, 3.0, 2.0)[0] == {"BTC-EUR": 2.0}
    with pytest.raises(SystemExit):
        label_spreads(["BTC-EUR"], {}, 3.0, None)


def probe_file(tmp_path: Path, maker: float = 25.0, taker: float = 50.0) -> Path:
    p = tmp_path / "probe.json"
    p.write_text(json.dumps({"fees": {"maker_bps": maker, "taker_bps": taker},
                             "products": {"SYN-EUR": {"meta": {"spread_bps": 2.5}}}}))  # fmt: skip
    return p


def run_cli(tmp_path: Path, extra: list[str]) -> int:
    cfg = AppConfig(paths={"data_dir": str(tmp_path)})  # type: ignore[arg-type]
    args = [
        "--products",
        "SYN-EUR",
        "--out-dir",
        str(tmp_path / "reports"),
        "--label-config",
        str(tmp_path / "lc.json"),
    ]
    return main([*args, *extra], cfg=cfg)


def write_store(tmp_path: Path, days: int, drift: float) -> None:
    bars = synthetic_bars(days=days, zero_volume_day=None, constant_days=None, seed=2)
    if drift:
        f = np.exp(drift * np.arange(len(bars)))
        for k in ("open", "high", "low", "close"):
            bars[k] = bars[k].to_numpy() * f
    BarStore(tmp_path / "bars" / "1m").write("SYN-EUR", bars.reset_index())


def test_cli_prohibitive_fees_are_a_no_go_without_label_config(tmp_path: Path) -> None:
    write_store(tmp_path, days=200, drift=0.0)
    code = run_cli(tmp_path, ["--fees-from-probe", str(probe_file(tmp_path, 600.0, 1200.0))])
    assert code == EXIT_NO_GO and not (tmp_path / "lc.json").exists()
    report = (tmp_path / "reports" / "feasibility.md").read_text(encoding="utf-8")
    assert "NO-GO" in report and "no_eligible" in report


def test_cli_go_writes_a_label_config_that_loads_and_freezes_the_holdout(tmp_path: Path) -> None:
    write_store(tmp_path, days=240, drift=0.0)
    code = run_cli(tmp_path, ["--fees-from-probe", str(probe_file(tmp_path)), "--spread-floor-bps", "3"])
    report = (tmp_path / "reports" / "feasibility.md").read_text(encoding="utf-8")
    assert "## Verdict:" in report and "All combinations" in report
    if code != 0:  # a random walk may legitimately fail the verdict; the contract is the same either way
        assert code == EXIT_NO_GO and not (tmp_path / "lc.json").exists()
        return
    lc = load_label_config(tmp_path / "lc.json")  # re-checks the hash
    raw = lc.raw
    assert lc.spread_bps == {"SYN-EUR": 3.0} and raw["spread_source"]["SYN-EUR"] == {"probe": 2.5, "floor": 3.0}
    sel = raw["selection"]
    assert sel["holdout_days"] == 120 and sel["embargo_days"] == 2 and sel["h_ref_days"] == 20
    assert pd.Timestamp(sel["holdout_start"]) - pd.Timestamp(sel["cut"]) == pd.Timedelta(days=2)
    assert "mu_year" in raw["feasibility"] and "mu" not in raw["feasibility"]["detr"]


def test_cli_assumed_fees_are_marked(tmp_path: Path) -> None:
    write_store(tmp_path, days=200, drift=0.0)
    run_cli(tmp_path, ["--spread-bps", "3"])
    assert "(assumed)" in (tmp_path / "reports" / "feasibility.md").read_text(encoding="utf-8")


def test_the_label_hash_ignores_what_only_serves_the_selection() -> None:
    cfg = LabelConfig(atr_tf="6h", k_tp=3.0, k_sl=2.0, horizon_min=15 * 1440)
    sp = {"SYN-EUR": 3.0}
    h = cfg.sha256(FEES, COSTS, sp)
    assert h == cfg.sha256(FEES, COSTS, dict(sp))  # nothing else can enter: mu / selection are not arguments
    assert cfg.sha256(FEES, COSTS, {"SYN-EUR": 3.5}) != h
    assert (
        LabelConfig(atr_tf="6h", k_tp=3.0, k_sl=2.0, horizon_min=15 * 1440, entry_first_bar=4).sha256(FEES, COSTS, sp)
        != h
    )


def test_outcome_codes_used_by_the_study_are_the_labeling_ones() -> None:
    assert (NO_FILL, UPPER, VERTICAL) == (0, 1, 3)
