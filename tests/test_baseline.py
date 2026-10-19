"""Baseline LightGBM: metrics, calibration, HOLD subsampling, per-fold training and the BaselineDecider contract."""

from __future__ import annotations

import asyncio
import inspect
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from scipy import special

from modules.decision.baseline import MARKET_FEATURES, BaselineDecider, Calibration, flat_columns_sha256, softmax
from modules.decision.protocol import ACTIONS, REGIME_LEGEND, DeciderBadResponse
from modules.decision.questions import build_questions
from modules.features.serializer import FLAT_COLUMNS, flatten_state, serialize_state
from tests.indicator_fixtures import synthetic_bars
from tests.label_fixtures import loaded_label
from tests.state_fixtures import ALL_STATES
from training import baseline_lgbm as bl
from training.baseline_lgbm import (
    SetData,
    TrainingError,
    brier_binary,
    ece,
    fit_platt,
    fit_subset,
    fit_temperature,
    logloss_binary,
    logloss_multi,
    prior_logloss,
    set_membership,
    subsample_hold,
    train_baseline,
)
from training.build_dataset import build_dataset
from training.config import TrainConfig

HOLDOUT = pd.Timestamp("2024-10-27", tz="UTC")  # day 300 of the synthetic history
DAYS = 330
CFG_KW = {"n_folds": 2, "val_frac": 0.4}  # short history: validation windows must outlive the 17-day labels
LABEL = loaded_label({"BTC-EUR": 3.0, "SOL-EUR": 3.0}, holdout_start=HOLDOUT)


# -------------------------------------------------------------------------------------- metrics


def test_logloss_and_brier_known_values() -> None:
    y = np.array([1.0, 0.0])
    assert logloss_binary(np.array([0.8, 0.2]), y) == pytest.approx(-math.log(0.8))
    assert brier_binary(np.array([0.8, 0.2]), y) == pytest.approx(0.04)
    p = np.array([[0.7, 0.2, 0.1], [0.1, 0.1, 0.8]])
    assert logloss_multi(p, np.array([0, 2])) == pytest.approx(-(math.log(0.7) + math.log(0.8)) / 2)
    w = np.array([3.0, 1.0])
    assert logloss_binary(np.array([0.8, 0.2]), y, w) == pytest.approx(-math.log(0.8))  # same value, both rows equal


def test_prior_logloss_is_the_base_rate_predictor() -> None:
    y = np.array([1.0, 0.0, 0.0, 0.0])
    assert prior_logloss(y) == pytest.approx(-(0.25 * math.log(0.25) + 0.75 * math.log(0.75)))


def test_ece_is_zero_for_a_calibrated_forecaster_and_large_for_a_biased_one() -> None:
    rng = np.random.default_rng(0)
    p = rng.uniform(0.05, 0.95, 40_000)
    y = (rng.uniform(size=p.size) < p).astype(float)
    assert ece(p, y) < 0.02
    assert ece(np.clip(p + 0.2, 0, 1), y) > 0.1
    assert ece(np.full(10, 0.5), np.array([1.0] * 5 + [0.0] * 5)) == pytest.approx(0.0)


# -------------------------------------------------------------------------------- calibration


def test_platt_recovers_the_planted_parameters() -> None:
    rng = np.random.default_rng(1)
    z = rng.normal(0, 1.5, 60_000)
    y = (rng.uniform(size=z.size) < special.expit(2.0 * z + 0.5)).astype(float)
    a, b = fit_platt(z, y, np.ones_like(z))
    assert a == pytest.approx(2.0, abs=0.1) and b == pytest.approx(0.5, abs=0.1)


def test_temperature_recovers_an_overconfident_model() -> None:
    rng = np.random.default_rng(2)
    logits = rng.normal(0, 1.0, (40_000, 3))
    p = softmax(logits)
    y = np.array([rng.choice(3, p=row) for row in p[:20_000]])
    t = fit_temperature(logits[:20_000] * 2.0, y, np.ones(20_000))  # the model reports logits twice as sharp
    assert t == pytest.approx(2.0, abs=0.2)


def test_calibration_applies_the_hold_prior_before_the_temperature() -> None:
    raw = np.array([[0.2, -0.3, 0.5]])
    plain = Calibration().action_probs(raw)
    shifted = Calibration(action_hold_log_prior=math.log(0.25)).action_probs(raw)
    assert plain.sum() == pytest.approx(1.0) and shifted[0, 2] < plain[0, 2]
    expected = softmax(np.array([[0.2, -0.3, 0.5 + math.log(0.25)]]) / 1.0)
    np.testing.assert_allclose(shifted, expected)
    t2 = Calibration(T_action=2.0).action_probs(raw)
    np.testing.assert_allclose(t2, softmax(raw / 2.0))
    assert Calibration(platt_edge={"a": 2.0, "b": 0.5}).edge_probs(np.array([0.3]))[0] == pytest.approx(
        special.expit(1.1)
    )


# ----------------------------------------------------------------------------- HOLD subsample


def hold_set(n_buy: int, n_sell: int, n_hold: int) -> SetData:
    y = np.array([0] * n_buy + [1] * n_sell + [2] * n_hold)
    rng = np.random.default_rng(3)
    return SetData(rng.normal(size=(len(y), 3)), y, np.ones(len(y)))


def test_hold_is_subsampled_to_three_times_the_minority() -> None:
    sub, keep = subsample_hold(hold_set(100, 200, 10_000), seed=1234, tag=0)
    assert int((sub.y == 2).sum()) == 300 and int((sub.y == 0).sum()) == 100 and int((sub.y == 1).sum()) == 200
    assert keep == pytest.approx(300 / 10_000) and sub.w.mean() == pytest.approx(1.0)


def test_hold_subsampling_is_deterministic_and_skipped_when_balanced() -> None:
    a, _ = subsample_hold(hold_set(100, 200, 10_000), 1234, 0)
    b, _ = subsample_hold(hold_set(100, 200, 10_000), 1234, 0)
    np.testing.assert_array_equal(a.x, b.x)
    c, _ = subsample_hold(hold_set(100, 200, 10_000), 1234, 1)
    assert not np.array_equal(a.x, c.x)  # another fold draws another subset
    same, keep = subsample_hold(hold_set(100, 100, 250), 1234, 0)
    assert keep == 1.0 and len(same.y) == 450
    none, keep0 = subsample_hold(hold_set(0, 0, 500), 1234, 0)
    assert keep0 == 1.0 and len(none.y) == 500  # no minority at all: nothing to balance against


# ------------------------------------------------------------------------- fit_subset on a planted signal


def planted_samples(
    n: int = 9000, seed: int = 4, w_scale: float = 0.01, products: int = 1
) -> tuple[pd.DataFrame, np.ndarray]:
    rng = np.random.default_rng(seed)
    x = rng.normal(size=(n, len(FLAT_COLUMNS)))
    df = pd.DataFrame(x, columns=list(FLAT_COLUMNS))
    edge = (x[:, FLAT_COLUMNS.index("ret_1d_bps")] + rng.normal(0, 1.0, n) > 0.3).astype(float)
    side = np.where(rng.uniform(size=n) < 0.8, "FLAT", "LONG")
    df["position_side"] = side
    df["product"] = np.array(["BTC-EUR", "SOL-EUR"])[np.arange(n) % products]
    df["t"] = pd.date_range("2024-01-01", periods=n, freq="1h")
    df["label_window_end"] = df["t"] + pd.Timedelta(hours=6)
    df["edge_target"] = edge
    df["vol_target"] = -1
    sell = (x[:, FLAT_COLUMNS.index("pos_unreal_bps")] < -1.0) & (side == "LONG")
    buy = (edge == 1) & (side == "FLAT") & (rng.uniform(size=n) < 0.5)
    df["action_target"] = np.where(sell, "SELL", np.where(buy, "BUY", "HOLD"))
    df["w_action"] = df["w_edge"] = w_scale * rng.uniform(0.5, 1.5, n)
    df["w_vol"] = np.nan
    n_tr, n_va = int(n * 0.7), int(n * 0.15)
    member = np.array(["train"] * n_tr + ["val"] * n_va + ["test"] * (n - n_tr - n_va), dtype=object)
    return df, member


@pytest.fixture(scope="module")
def planted() -> tuple[pd.DataFrame, np.ndarray, dict]:
    df, member = planted_samples()
    return df, member, fit_subset(df, member, TrainConfig(), tag=0, allow_degenerate=True, rho=1.0)


def test_the_booster_learns_the_planted_edge(planted: tuple[pd.DataFrame, np.ndarray, dict]) -> None:
    _, _, res = planted
    m = res["metrics"]["edge"]
    assert m["val_logloss_after"] < m["val_prior_logloss"] - 0.02  # clearly better than the base rate
    # the Platt map is no longer fitted on the validation set, so "after <= before" holds only up to a small slack
    assert m["val_ece_after"] < 0.05 and m["val_logloss_after"] <= m["val_logloss_before"] + 5e-3


def test_test_metrics_use_the_test_rows_only(planted: tuple[pd.DataFrame, np.ndarray, dict]) -> None:
    df, member, res = planted
    tm = bl.test_metrics(df, member, res)
    assert tm["edge_logloss"] < tm["edge_prior_logloss"] and tm["n_edge"] > 0
    assert "action_ece_buy" in tm


def edge_inputs(df: pd.DataFrame, member: np.ndarray, which: str) -> tuple[pd.DataFrame, bl.SetData]:
    return bl._edge_frame(df, member == which), bl._select(df, member == which, "edge", MARKET_FEATURES)


def test_oof_calibration_never_uses_test_rows() -> None:
    df, member = planted_samples(6000, w_scale=1.0)
    seen: list[pd.DataFrame] = []
    real = bl.oof_edge_scores

    def spy(tr: pd.DataFrame, *a: object, **k: object) -> np.ndarray:
        seen.append(tr)
        return real(tr, *a, **k)  # type: ignore[arg-type]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bl, "oof_edge_scores", spy)
        ref = fit_subset(df, member, TrainConfig(), tag=0, allow_degenerate=True, rho=1.0)
        assert len(seen) == 1 and (seen[0]["t"] < df["t"][member == "test"].min()).all()
        # destroy the test rows: labels flipped, features and weights replaced -> the calibration cannot move
        bad = df.copy()
        te = member == "test"
        bad.loc[te, "edge_target"] = 1.0 - bad.loc[te, "edge_target"]
        bad.loc[te, "w_edge"] = 7.0
        bad.loc[te, list(FLAT_COLUMNS)] = 1e6
        again = fit_subset(bad, member, TrainConfig(), tag=0, allow_degenerate=True, rho=1.0)
    assert again["calibration"].platt_edge == ref["calibration"].platt_edge
    assert again["edge_cal"] == ref["edge_cal"]
    ec = ref["edge_cal"]
    assert ec["n_rows_val"] == int(((member == "val") & (df["position_side"] == "FLAT")).sum())
    assert 0 < ec["n_rows_oof"] <= int(((member == "train") & (df["position_side"] == "FLAT")).sum())


def test_oof_blocks_are_purged() -> None:
    n, emb = 2000, timedelta(hours=12)
    t = pd.date_range("2024-01-01", periods=n, freq="1h")
    tr = pd.DataFrame({"t": t, "product": "BTC-EUR", "label_window_end": t + pd.Timedelta(hours=30), "w_edge": 1.0})
    tr = tr.sample(frac=1.0, random_state=0).reset_index(drop=True)  # the function must not rely on the input order
    blocks = bl._cal_blocks(tr, emb)
    assert len(blocks) == bl.CAL_FOLDS and sorted(np.concatenate([b for b, _ in blocks])) == list(range(n))
    for block, fit in blocks:
        start, last = tr["t"].iloc[block].min(), tr["label_window_end"].iloc[block].max()
        assert not set(block) & set(fit)
        f = tr.iloc[fit]
        assert ((f["label_window_end"] < start - emb) | (f["t"] > last + emb)).all()
        # nothing is purged that did not have to be: every other row outside the block is within the embargo
        rest = tr.drop(index=np.concatenate([block, fit]))
        assert ((rest["label_window_end"] >= start - emb) & (rest["t"] <= last + emb)).all()
    # the scores: NaN where the fit set is too small to train on
    x, y = np.random.default_rng(0).normal(size=(n, 3)), np.random.default_rng(1).integers(0, 2, n)
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(bl.LGBM_PARAMS, "min_data_in_leaf", 30)
        assert np.isfinite(bl.oof_edge_scores(tr, x, y, 5, emb, 1)).all()
        mp.setitem(bl.LGBM_PARAMS, "min_data_in_leaf", 800)  # 2 x 800 exceeds every fit set
        assert np.isnan(bl.oof_edge_scores(tr, x, y, 5, emb, 1)).all()


def test_cal_neff_below_200_marks_fold_and_fails_final(tmp_path: Path) -> None:
    low, member = planted_samples(w_scale=0.01, products=2)
    res = fit_subset(low, member, TrainConfig(), tag=0, allow_degenerate=True, rho=1.0)
    ec = res["edge_cal"]
    assert ec["n_eff"] < bl.CAL_NEFF_MIN == 200.0 and ec["ok"] is False and ec["n_eff_min"] == 200.0
    assert res["calibration"].platt_edge.a != 1.0  # a not-ok calibration is still saved
    high, member = planted_samples(w_scale=1.0, products=2)
    ok = fit_subset(high, member, TrainConfig(), tag=0, allow_degenerate=True, rho=1.0)["edge_cal"]
    assert ok["ok"] is True and ok["n_eff"] >= 200.0
    # N_eff = sum(w) / (1 + (K - 1) rho): rho 0 -> K independent products, rho 1 -> one
    free = fit_subset(high, member, TrainConfig(), tag=0, allow_degenerate=True, rho=0.0)["edge_cal"]
    assert free["n_eff"] == pytest.approx(2.0 * ok["n_eff"])
    # the final model refuses an uncalibrated edge (exit 1) and writes nothing for itself
    frames = {p: synthetic_bars(days=DAYS, seed=31 + i, zero_volume_day=None, constant_days=None)
              for i, p in enumerate(("BTC-EUR", "SOL-EUR"))}  # fmt: skip
    cfg = TrainConfig(products=list(frames), sentiment_history_db=str(tmp_path / "none.db"), **CFG_KW)
    lc = tmp_path / "label_config.json"
    lc.write_text("{}")
    run = build_dataset(cfg, LABEL, frames, tmp_path / "ds", label_config_path=lc, write_jsonl=False,
                        now=datetime(2026, 10, 18, 12, 0, tzinfo=UTC))  # fmt: skip
    samples = pd.read_parquet(run / "samples.parquet")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bl, "CAL_NEFF_MIN", 1e9)
        with pytest.raises(TrainingError, match="final: edge calibration"):
            train_baseline(samples, LABEL, cfg, run, tmp_path / "models", label_config_path=lc,
                           allow_degenerate_final=True)  # fmt: skip
    assert list((tmp_path / "models").glob("*/fold_0/calibration.json")) != []  # the folds were saved anyway
    assert not list((tmp_path / "models").glob("*/final/*"))


def test_platt_is_the_only_edge_calibrator() -> None:
    df, member = planted_samples(6000, w_scale=1.0)
    res = fit_subset(df, member, TrainConfig(), tag=0, allow_degenerate=True, rho=1.0)
    cal = res["calibration"]
    assert "isotonic" not in inspect.getsource(bl).lower() and "isotonic" not in cal.model_dump_json().lower()
    # rebuild C independently (OOF scores of the train rows + validation scores) and fit the same 2-parameter map
    ftr, tr = edge_inputs(df, member, "train")
    fva, va = edge_inputs(df, member, "val")
    n_star = res["best_iteration"]["edge"]
    z_oof = bl.oof_edge_scores(ftr, tr.x, tr.y, n_star, timedelta(days=TrainConfig().embargo_days), 0)
    done = np.isfinite(z_oof)
    z_val = np.asarray(res["boosters"]["edge"].predict(va.x, raw_score=True))
    w = np.concatenate([ftr["w_edge"].to_numpy()[done], fva["w_edge"].to_numpy()])
    z_c, y_c = np.concatenate([z_oof[done], z_val]), np.concatenate([tr.y[done], va.y]).astype(float)
    a, b = fit_platt(z_c, y_c, w / w.mean())
    assert (cal.platt_edge.a, cal.platt_edge.b) == pytest.approx((a, b), rel=1e-6)
    assert res["edge_cal"]["n_rows_oof"] == int(done.sum()) and res["edge_cal"]["scheme"] == "OOF_PLATT_V1"
    z = np.linspace(-3, 3, 7)  # the map is a sigmoid of an affine function of the raw score, nothing else
    np.testing.assert_allclose(cal.edge_probs(z), special.expit(cal.platt_edge.a * z + cal.platt_edge.b))


def test_the_action_head_finds_the_planted_sell_rule(planted: tuple[pd.DataFrame, np.ndarray, dict]) -> None:
    df, member, res = planted
    long = df[(member == "test") & (df["position_side"] == "LONG")]
    from modules.decision.baseline import ACTION_FEATURES

    raw = np.asarray(res["boosters"]["action"].predict(long[list(ACTION_FEATURES)].to_numpy(), raw_score=True))
    p = res["calibration"].action_probs(raw.reshape(len(long), 3))
    is_sell = (long["action_target"] == "SELL").to_numpy()
    assert p[is_sell, 1].mean() > p[~is_sell, 1].mean() + 0.05


def test_the_regime_head_is_degenerate_without_labels_and_forbidden_for_the_final(planted: tuple) -> None:
    df, member, res = planted
    assert res["degenerate"] == {"vol_regime": True} and "vol_regime" not in res["boosters"]
    with pytest.raises(TrainingError, match="vol_regime"):
        fit_subset(df, member, TrainConfig(), tag=99, allow_degenerate=False, rho=1.0)


def test_empty_validation_is_an_error() -> None:
    df, member = planted_samples(3000)
    member[member == "val"] = ""
    with pytest.raises(TrainingError):
        fit_subset(df, member, TrainConfig(), tag=0, allow_degenerate=True, rho=1.0)


def test_features_of_edge_and_regime_exclude_the_position_block() -> None:
    assert not any(c.startswith("pos_") for c in MARKET_FEATURES) and len(MARKET_FEATURES) < len(FLAT_COLUMNS)


# ------------------------------------------------------------------- integration with a real dataset build


@pytest.fixture(scope="module")
def trained(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, Path]:
    tmp = tmp_path_factory.mktemp("bl")
    frames = {
        "BTC-EUR": synthetic_bars(days=DAYS, seed=31, zero_volume_day=None, constant_days=None),
        "SOL-EUR": synthetic_bars(days=DAYS, seed=32, zero_volume_day=None, constant_days=None),
    }
    cfg = TrainConfig(products=["BTC-EUR", "SOL-EUR"], sentiment_history_db=str(tmp / "none.db"), **CFG_KW)
    lc = tmp / "label_config.json"
    lc.write_text("{}")
    run = build_dataset(cfg, LABEL, frames, tmp / "ds", label_config_path=lc, write_jsonl=False,
                        now=datetime(2026, 10, 18, 12, 0, tzinfo=UTC))  # fmt: skip
    samples = pd.read_parquet(run / "samples.parquet")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bl, "CAL_NEFF_MIN", 1.0)  # the synthetic history is far below the real N_eff floor
        root = train_baseline(samples, LABEL, cfg, run, tmp / "models", label_config_path=lc,
                              allow_degenerate_final=True, now=datetime(2026, 10, 18, 12, 5, tzinfo=UTC))  # fmt: skip
    return root, run, lc


def test_model_files_and_card(trained: tuple[Path, Path, Path]) -> None:
    root, run, lc = trained
    assert root.name == "baseline-lgbm@2026-10-18.1"
    for sub in ("fold_0", "fold_1", "final"):
        assert (root / sub / "action.txt").exists() and (root / sub / "edge.txt").exists()
        assert (root / sub / "calibration.json").exists()
    assert (root / "label_config.json").read_bytes() == lc.read_bytes()
    card = json.loads((root / "model_card.json").read_text())
    assert card["backend"] == "baseline" and card["flat_columns_sha256"] == flat_columns_sha256()
    assert card["label_config"]["sha256"] == LABEL.sha256 and card["data"]["dataset_run_id"] == run.name
    assert card["lgbm"]["params"]["num_leaves"] == 7 and card["lgbm"]["params"]["lambda_l2"] == 10.0
    assert card["degenerate"]["final"] == {"vol_regime": True}
    assert card["calibration_scheme"] == "OOF_PLATT_V1"
    for sub in ("fold_0", "fold_1", "final"):
        ec = card["folds"][sub]["edge_cal"]
        assert ec["scheme"] == "OOF_PLATT_V1" and ec["ok"] and ec["n_rows_oof"] > 0 and ec["n_rows_val"] > 0
        assert json.loads((root / sub / "calibration.json").read_text())["edge_cal"] == ec
    assert set(card["folds"]) == {"fold_0", "fold_1", "final"} and card["folds"]["final"]["metrics_test"] == {}


def test_a_second_training_gets_the_next_version_number(trained: tuple[Path, Path, Path], tmp_path: Path) -> None:
    root, _, _ = trained
    assert bl._version(root.parent, datetime(2026, 10, 18, tzinfo=UTC)) == "baseline-lgbm@2026-10-18.2"


def test_set_membership_matches_the_dataset_splits(trained: tuple[Path, Path, Path]) -> None:
    _, run, _ = trained
    samples = pd.read_parquet(run / "samples.parquet")
    members = set_membership(samples, LABEL, TrainConfig(**CFG_KW))
    for k in range(2):
        m = members[f"fold_{k}"]
        assert ((m == "test") == (samples["fold_test"] == k)).all()  # the test sets are the ones the dataset recorded
        t = pd.to_datetime(samples["t"], utc=True)
        assert t[m == "train"].max() < t[m == "val"].min() <= t[m == "val"].max() < t[m == "test"].min()
    fin = members["final"]
    assert ((fin == "train") == (samples["final_split"] == "train")).all() and (
        (fin == "val") == (samples["final_split"] == "val")
    ).all()


def make_decider(trained: tuple[Path, Path, Path], **kw: object) -> BaselineDecider:
    root, _, _ = trained
    q = build_questions(LABEL.cfg.horizon_min, LABEL.cfg.k_tp, LABEL.cfg.k_sl, LABEL.cfg.atr_tf)
    return BaselineDecider.load(root, label=LABEL, questions=q, allow_degenerate=True, **kw)  # type: ignore[arg-type]


def test_baseline_decide_matches_batch(trained: tuple[Path, Path, Path]) -> None:
    """decide(serialize_state(s)) equals answers_batch(flatten_state(s)) for the golden states."""
    dec = make_decider(trained)
    q = build_questions(LABEL.cfg.horizon_min, LABEL.cfg.k_tp, LABEL.cfg.k_sl, LABEL.cfg.atr_tf).questions
    for make in ALL_STATES.values():
        state = make()
        answers = asyncio.run(dec.decide(serialize_state(state), q))
        flat = flatten_state(state)
        batch = dec.answers_batch(np.array([[flat[c] for c in FLAT_COLUMNS]]))
        assert [answers.action.probabilities[a] for a in ACTIONS] == pytest.approx(list(batch.p_action[0]))
        assert answers.edge.noul == pytest.approx(float(batch.p_edge[0]))
        assert [answers.vol_regime.probabilities[r] for r in REGIME_LEGEND] == pytest.approx(list(batch.p_regime[0]))
        assert answers.action.choice == ACTIONS[int(np.argmax(batch.p_action[0]))]
        assert answers.action.confidence == 0.0  # the client / policy recompute it


def test_a_degenerate_regime_answers_normal(trained: tuple[Path, Path, Path]) -> None:
    dec = make_decider(trained)
    assert dec.degenerate_regime
    x = np.array([[flatten_state(ALL_STATES["sol_flat"]())[c] for c in FLAT_COLUMNS]])
    np.testing.assert_allclose(dec.answers_batch(x).p_regime[0], [0, 0, 1, 0, 0])


def test_answers_batch_is_row_independent(trained: tuple[Path, Path, Path]) -> None:
    dec = make_decider(trained)
    rows = np.array([[flatten_state(f())[c] for c in FLAT_COLUMNS] for f in ALL_STATES.values()])
    together = dec.answers_batch(rows)
    for i in range(len(rows)):
        one = dec.answers_batch(rows[i : i + 1])
        np.testing.assert_allclose(one.p_action[0], together.p_action[i])
        assert one.p_edge[0] == pytest.approx(together.p_edge[i])
    with pytest.raises(ValueError):
        dec.answers_batch(rows[:, :10])


def test_load_refuses_a_model_that_does_not_belong(trained: tuple[Path, Path, Path]) -> None:
    root, _, _ = trained
    q = build_questions(LABEL.cfg.horizon_min, LABEL.cfg.k_tp, LABEL.cfg.k_sl, LABEL.cfg.atr_tf)
    other_label = loaded_label({"BTC-EUR": 3.0, "SOL-EUR": 4.0}, holdout_start=HOLDOUT)
    with pytest.raises(ValueError, match="label_config"):
        BaselineDecider.load(root, label=other_label, questions=q, allow_degenerate=True)
    other_q = build_questions(LABEL.cfg.horizon_min, 4.0, LABEL.cfg.k_sl, LABEL.cfg.atr_tf)
    with pytest.raises(ValueError, match="questions"):
        BaselineDecider.load(root, label=LABEL, questions=other_q, allow_degenerate=True)
    with pytest.raises(ValueError, match="degenerate"):
        BaselineDecider.load(root, label=LABEL, questions=q)  # the final model has no regime head here


def test_decide_refuses_other_questions(trained: tuple[Path, Path, Path]) -> None:
    dec = make_decider(trained)
    other = build_questions(LABEL.cfg.horizon_min, 5.0, LABEL.cfg.k_sl, LABEL.cfg.atr_tf).questions
    with pytest.raises(DeciderBadResponse):
        asyncio.run(dec.decide(serialize_state(ALL_STATES["sol_flat"]()), other))


def test_training_refuses_a_dataset_of_another_label(trained: tuple[Path, Path, Path], tmp_path: Path) -> None:
    _, run, lc = trained
    samples = pd.read_parquet(run / "samples.parquet")
    other = loaded_label({"BTC-EUR": 3.0, "SOL-EUR": 4.0}, holdout_start=HOLDOUT)
    with pytest.raises(TrainingError, match="another label_config"):
        train_baseline(samples, other, TrainConfig(**CFG_KW), run, tmp_path / "m", label_config_path=lc)
