"""Dataset builder: files, state parity with the runtime path, splits without leakage, LONG samples and JSONL."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from modules.features.schemas import DecisionState
from modules.features.serializer import FLAT_COLUMNS, flatten_state, state_sha256
from tests.indicator_fixtures import synthetic_bars
from tests.label_fixtures import loaded_label
from training import build_dataset as bd
from training.build_dataset import DatasetError, build_dataset, check_label_tokens
from training.config import TrainConfig

HOLDOUT = pd.Timestamp("2024-07-20", tz="UTC")
LABEL = loaded_label({"BTC-EUR": 3.0, "SOL-EUR": 3.0}, holdout_start=HOLDOUT)
EMBARGO = pd.Timedelta(days=2)


@pytest.fixture(scope="module")
def frames() -> dict[str, pd.DataFrame]:
    return {
        "BTC-EUR": synthetic_bars(days=235, seed=31, zero_volume_day=None, constant_days=None),
        "SOL-EUR": synthetic_bars(days=235, seed=32, zero_volume_day=None, constant_days=None),
    }


def cfg_for(tmp_path: Path) -> TrainConfig:
    return TrainConfig(
        products=["BTC-EUR", "SOL-EUR"], n_folds=3, sentiment_history_db=str(tmp_path / "no_news_history.db")
    )


@pytest.fixture(scope="module")
def run(frames: dict[str, pd.DataFrame], tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    tmp = tmp_path_factory.mktemp("ds")
    lc_path = tmp / "label_config.json"
    lc_path.write_text('{"placeholder": true}')
    label = loaded_label({"BTC-EUR": 3.0, "SOL-EUR": 3.0}, holdout_start=HOLDOUT)
    out = build_dataset(
        cfg_for(tmp), label, frames, tmp / "out", label_config_path=lc_path,
        now=datetime(2026, 10, 18, 12, 0, tzinfo=UTC),
    )  # fmt: skip
    return out, lc_path


@pytest.fixture(scope="module")
def samples(run: tuple[Path, Path]) -> pd.DataFrame:
    return pd.read_parquet(run[0] / "samples.parquet")


# -------------------------------------------------------------------------------- files


def test_files_manifest_and_label_config_copy(run: tuple[Path, Path]) -> None:
    out, lc_path = run
    assert out.name.startswith("20261018T120000Z-")
    assert (out / "label_config.json").read_bytes() == lc_path.read_bytes()
    m = json.loads((out / "manifest.json").read_text())
    assert m["products"] == ["BTC-EUR", "SOL-EUR"] and m["sentiment_available"] is False
    assert m["label_tokens_checked"] is False and m["state_schema"] == "chronos.state.v1"
    assert m["indicator_anchor"]["BTC-EUR"] == "2024-01-01T00:00:00Z"
    assert m["flat_columns_sha256"] == state_sha256(",".join(FLAT_COLUMNS))
    assert m["rows"]["FLAT/train"] > 0 and m["rows"]["FLAT/holdout"] > 0 and "LONG/train" in m["rows"]
    assert out.name.endswith(m["label_config_sha256"][:8])
    for name in ("train.jsonl", "val.jsonl", "test.jsonl", "samples.parquet"):
        assert (out / name).exists()


# ---------------------------------------------------------------------------------- states


def test_states_are_valid_runtime_states(samples: pd.DataFrame) -> None:
    for r in samples.sample(40, random_state=1).to_dict("records"):
        state = DecisionState.model_validate_json(r["state"])
        assert state_sha256(r["state"]) == r["state_sha256"]
        assert state.position.side == r["position_side"] and state.product == r["product"]
        flat = flatten_state(state)
        for col in FLAT_COLUMNS:
            a, b = flat[col], r[col]
            assert (np.isnan(a) and np.isnan(b)) or a == b, col
        assert state.sentiment is None and state.costs.cost_tp_bps == 50 and state.costs.cost_sl_bps == 92


def test_state_clock_matches_the_decision_instant(samples: pd.DataFrame) -> None:
    for r in samples.head(30).to_dict("records"):
        st = DecisionState.model_validate_json(r["state"])
        t = pd.Timestamp(r["t"])
        assert (st.clock.hour_utc, st.clock.dow) == (t.hour, t.weekday())


def test_btc_context_only_for_non_btc(samples: pd.DataFrame) -> None:
    btc = samples[samples["product"] == "BTC-EUR"].head(20)
    sol = samples[(samples["product"] == "SOL-EUR") & samples["atr_14_6h_bps"].notna()].head(20)
    assert all(json.loads(s)["btc_context"] is None for s in btc["state"])
    assert all(json.loads(s)["btc_context"] is not None for s in sol["state"])


# ------------------------------------------------------------------------- labels / weights


def test_targets_and_weights(samples: pd.DataFrame) -> None:
    flat = samples[samples["position_side"] == "FLAT"]
    assert set(flat["action_target"]) <= {"BUY", "HOLD"}
    no_fill = flat[flat["outcome"] == 0]
    assert (
        no_fill["edge_target"].isna().all()
        and no_fill["w_edge"].isna().all()
        and (no_fill["action_target"] == "HOLD").all()
    )
    assert (flat["w_action"] > 0).all() and (flat["w_action"] <= 1).all()
    filled = flat[flat["outcome"] != 0]
    assert (filled["w_edge"] > 0).all()
    assert (filled[filled["outcome"] == 1]["edge_target"] == 1.0).all() and (
        filled[filled["outcome"] != 1]["edge_target"] == 0.0
    ).all()
    buys = flat[flat["action_target"] == "BUY"]
    assert (buys["outcome"] == 1).all() and (buys["net_ret_bps"] > 0).all()
    assert (flat["vol_target"] == -1).all()  # 235 days of history: no vol_regime label exists yet (needs ~375)


def test_long_rows_are_consistent_with_their_fictitious_entry(samples: pd.DataFrame) -> None:
    long = samples[samples["position_side"] == "LONG"]
    assert len(long) > 20
    assert (long["entry_px"] > 0).all() and (long["stop_loss"] < long["entry_px"]).all()
    assert (long["entry_px"] < long["take_profit"]).all()
    assert (long["fill_idx_br"] < long["t_idx"]).all() and (long["exit_idx_br"] >= long["t_idx"] + 3).all()
    assert set(long["action_target"]) <= {"SELL", "HOLD"} and long["opened_at"].notna().all()
    for r in long.head(10).to_dict("records"):
        st = DecisionState.model_validate_json(r["state"])
        assert st.position.side == "LONG" and st.position.hours_held >= 0 and st.position.unreal_bps is not None


# ------------------------------------------------------------------------------------ splits


def test_no_leakage_between_final_splits_and_the_holdout(samples: pd.DataFrame) -> None:
    flat = samples[samples["position_side"] == "FLAT"].copy()
    flat["t"] = pd.to_datetime(flat["t"], utc=True)
    flat["t1"] = pd.to_datetime(flat["label_window_end"], utc=True)
    train, val, hold = (flat[flat["final_split"] == s] for s in ("train", "val", "holdout"))
    assert len(train) and len(val) and len(hold)
    assert (hold["t"] >= HOLDOUT).all() and (flat[flat["final_split"] != "holdout"]["t"] < HOLDOUT).all()
    assert val["t1"].max() < HOLDOUT - EMBARGO
    assert train["t1"].max() + EMBARGO <= val["t"].min()
    assert train["t"].max() < val["t"].min()


def test_fold_tests_are_ordered_and_leave_the_holdout_alone(samples: pd.DataFrame) -> None:
    flat = samples[samples["position_side"] == "FLAT"].copy()
    flat["t"] = pd.to_datetime(flat["t"], utc=True)
    folds = sorted(f for f in flat["fold_test"].unique() if f >= 0)
    assert folds == [0, 1, 2]
    for a, b in zip(folds, folds[1:], strict=False):
        assert flat[flat["fold_test"] == a]["t"].max() < flat[flat["fold_test"] == b]["t"].min()
    assert (flat[flat["fold_test"] == bd.FOLD_HOLDOUT]["t"] >= HOLDOUT).all()
    assert (flat[flat["fold_test"] >= 0]["t"] < HOLDOUT).all()


def test_long_rows_inherit_the_split_of_their_flat_row(samples: pd.DataFrame) -> None:
    key = ["product", "t"]
    flat = samples[samples["position_side"] == "FLAT"].set_index(key)
    long = samples[samples["position_side"] == "LONG"].set_index(key)
    joined = long.join(flat[["fold_test", "final_split"]], rsuffix="_flat", how="left")
    assert joined["fold_test_flat"].notna().all()
    assert (joined["fold_test"] == joined["fold_test_flat"]).all() and (
        joined["final_split"] == joined["final_split_flat"]
    ).all()


# ------------------------------------------------------------------------------------ jsonl


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_jsonl_records_follow_the_spec_format(run: tuple[Path, Path]) -> None:
    out, _ = run
    train, val, test = (read_jsonl(out / f"{s}.jsonl") for s in ("train", "val", "test"))
    assert train and val and test
    r = train[0]
    assert set(r) == {"id", "fold", "split", "product", "t", "position_side", "state", "question_key", "question_type",
                      "options", "target", "target_index", "weight", "meta"}  # fmt: skip
    assert set(r["meta"]) == {"barrier_outcome", "net_ret_bps", "fill_minute", "exit_minute", "atr_tf", "atr_bps",
                              "label_config_sha256", "questions_version", "sentiment_dropped"}  # fmt: skip
    for rec in train + val + test:
        assert rec["options"][rec["target_index"]] == rec["target"]
        assert rec["question_key"] in ("action", "edge")  # no vol_regime label in 235 days
        if rec["question_key"] == "edge":
            assert rec["meta"]["barrier_outcome"] != "NO_FILL"
    assert {x["split"] for x in test} == {"test"} and all(x["fold"] == -2 for x in test)
    assert all(x["split"] == "train" for x in train)


def test_jsonl_weights_have_mean_one_per_question_and_split(run: tuple[Path, Path]) -> None:
    out, _ = run
    for split in ("train", "val", "test"):
        recs = read_jsonl(out / f"{split}.jsonl")
        for q in ("action", "edge"):
            w = [x["weight"] for x in recs if x["question_key"] == q]
            assert w and np.mean(w) == pytest.approx(1.0, abs=1e-9)


def test_long_action_records_carry_the_bracket_outcome(run: tuple[Path, Path]) -> None:
    out, _ = run
    longs = [
        x for x in read_jsonl(out / "train.jsonl") if x["position_side"] == "LONG" and x["question_key"] == "action"
    ]
    assert longs and all(x["target"] in ("SELL", "HOLD") for x in longs)
    assert all(x["meta"]["barrier_outcome"] in ("UPPER", "LOWER", "VERTICAL") for x in longs)


# ------------------------------------------------------------------------ guards and the CLI


def test_two_runs_give_the_same_samples(
    frames: dict[str, pd.DataFrame], run: tuple[Path, Path], tmp_path: Path
) -> None:
    lc = tmp_path / "lc.json"
    lc.write_text("{}")
    label = loaded_label({"BTC-EUR": 3.0, "SOL-EUR": 3.0}, holdout_start=HOLDOUT)
    second = build_dataset(
        cfg_for(tmp_path), label, frames, tmp_path / "again", label_config_path=lc, write_jsonl=False,
        now=datetime(2026, 10, 18, 12, 1, tzinfo=UTC),
    )  # fmt: skip
    pd.testing.assert_frame_equal(
        pd.read_parquet(run[0] / "samples.parquet"), pd.read_parquet(second / "samples.parquet")
    )


def test_a_news_history_that_cannot_be_used_is_refused(frames: dict[str, pd.DataFrame], tmp_path: Path) -> None:
    db = tmp_path / "news_history.db"
    db.write_text("x")
    cfg = TrainConfig(products=["BTC-EUR", "SOL-EUR"], sentiment_history_db=str(db))
    lc = tmp_path / "lc.json"
    lc.write_text("{}")
    with pytest.raises(DatasetError, match="sentiment"):
        build_dataset(cfg, LABEL, frames, tmp_path / "o",
                      label_config_path=lc)  # fmt: skip


def test_missing_product_bars_are_refused(frames: dict[str, pd.DataFrame], tmp_path: Path) -> None:
    lc = tmp_path / "lc.json"
    lc.write_text("{}")
    cfg = TrainConfig(products=["BTC-EUR", "ETH-EUR"], sentiment_history_db=str(tmp_path / "none.db"))
    with pytest.raises(DatasetError, match="ETH-EUR"):
        build_dataset(
            cfg, loaded_label({"BTC-EUR": 3.0}, holdout_start=HOLDOUT), frames, tmp_path / "o", label_config_path=lc
        )


class FakeTokenizer:
    def __init__(self, multi: set[str] | None = None) -> None:
        self.multi = multi or set()

    def encode(self, text: str, add_special_tokens: bool = False) -> list[int]:
        return [1, 2] if text in self.multi else [7]


def test_label_tokens_must_be_single_ids() -> None:
    check_label_tokens(FakeTokenizer())
    with pytest.raises(DatasetError, match="' Y'"):
        check_label_tokens(FakeTokenizer({" Y"}))


def test_tokenizer_check_is_recorded_in_the_manifest(frames: dict[str, pd.DataFrame], tmp_path: Path) -> None:
    lc = tmp_path / "lc.json"
    lc.write_text("{}")
    out = build_dataset(
        cfg_for(tmp_path), LABEL, frames, tmp_path / "o",
        label_config_path=lc, tokenizer=FakeTokenizer(), write_jsonl=False,
    )  # fmt: skip
    assert json.loads((out / "manifest.json").read_text())["label_tokens_checked"] is True
    assert not (out / "train.jsonl").exists()


def test_cli_exits_2_on_a_label_config_with_a_wrong_hash(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    lc = tmp_path / "label_config.json"
    lc.write_text(json.dumps({"k_tp": 3.0}))
    cfg = tmp_path / "train.yml"
    cfg.write_text(f"label_config: {lc.as_posix()}\ndatasets_dir: {tmp_path.as_posix()}/ds\n")
    assert bd.main(["--config", str(cfg)]) == 2
    assert "label_config invalid" in capsys.readouterr().err
