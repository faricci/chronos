"""Live labels in the dataset (spec 3.1.5, 5.1.5, 5.1.6 L3): ``build_dataset(live=...)``, the split rule for live rows,
the weights, the manifest, the non-regression of the historical dataset and the learner end to end.

The live labels are the real ones: synthetic ``DecisionRecord`` s (states built by the very ``StateBuilder`` of the
dataset) resolved by the real ``LabelResolver`` over the stored bars, then read back by ``build_dataset``.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from modules.config import EXIT_CONFIG_ERROR, LearnerConfig
from modules.dryrun.label_resolver import LabelResolver
from modules.dryrun.learner import LearnerPaths, NightlyLearner
from modules.dryrun.registry import FilesystemModelRegistry
from modules.features.schemas import DecisionState
from modules.features.serializer import FLAT_COLUMNS, flatten_state, serialize_state, state_sha256
from modules.features.state_builder import PositionInput, StateBuilder
from modules.market.backfill import BarStore
from modules.risk.label_config import LoadedLabelConfig
from tests.indicator_fixtures import synthetic_bars
from tests.label_fixtures import loaded_label
from training import baseline_lgbm as bl
from training import build_dataset as bd
from training import labeling
from training.build_dataset import FlatSplits, LiveInput, build_dataset
from training.config import TrainConfig
from training.sampling import uniqueness_weights

ORIG_VOL_REF = labeling.VOL_REF_DAYS
PRODUCTS = ["BTC-EUR", "SOL-EUR"]
HOLDOUT = pd.Timestamp("2024-07-20", tz="UTC")
LABEL = loaded_label({"BTC-EUR": 3.0, "SOL-EUR": 3.0}, holdout_start=HOLDOUT)
MIN = timedelta(minutes=1)
EMBARGO = pd.Timedelta(days=2)
VAL_FRAC = 0.15
N_FOLDS = 3
T0 = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
BOOKS = ("champion", "random_entry")
START = pd.Timestamp("2024-01-01", tz="UTC")
NAIVE_START = START.tz_convert(None)


def iso(t: datetime | pd.Timestamp) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def day_rows(first: float, last: float, step: float) -> list[int]:
    """Bar rows at fractional days after the start, at minute :16 of the bar (decision close at :17: never on the
    hourly grid of the historical dataset)."""
    return [int(d * 1440) + 16 for d in np.arange(first, last, step)]


@pytest.fixture(scope="module", autouse=True)
def short_vol_reference() -> Any:
    """235 days of data: a year of reference leaves no ``vol_regime`` label (resolver and builder read the same)."""
    mp = pytest.MonkeyPatch()
    mp.setattr(labeling, "VOL_REF_DAYS", 30)
    yield
    mp.undo()


# ------------------------------------------------------------------------------------------------ the world


@dataclass
class Live:
    """Synthetic decisions, resolved by the real LabelResolver, over bars written to a real store."""

    root: Path
    frames: dict[str, pd.DataFrame]
    pf: dict[str, bd.ProductFrames]
    label: LoadedLabelConfig

    @property
    def labels_dir(self) -> Path:
        return self.root / "live_labels"

    def records(self, product: str, rows: list[int]) -> list[dict[str, Any]]:
        own, btc = self.pf[product], self.pf.get("BTC-EUR")
        arr = np.asarray(rows, dtype=np.int64)
        btc_rows = bd._btc_rows(own, btc, arr)
        snaps = bd._snapshots(own, arr)
        btc_iter = iter(bd._snapshots(btc, btc_rows[btc_rows >= 0])) if btc is not None else iter(())
        btc_of = [next(btc_iter) if r >= 0 else None for r in btc_rows]
        builder = StateBuilder(self.label)
        close = own.bars["close"].to_numpy(dtype=np.float64)
        out: list[dict[str, Any]] = []
        for i, s, b in zip(rows, snaps, btc_of, strict=True):
            state = serialize_state(
                builder.build(snapshot=s, btc=b, sentiment=None, position=PositionInput.flat(), close_t=float(close[i]))
            )
            t = own.bars.index[i].to_pydatetime() + MIN
            for book in BOOKS:
                out.append({
                    "decision_id": f"{book[:3]}-{product}-{i:07d}", "ts_bar_close": iso(t),
                    "decided_at": iso(t + 2 * MIN),
                    "product": product, "book": book, "state": state, "questions_version": "Q1",
                    "policy": {"policy_version": "m1", "final": "HOLD"}, "answers": None,
                })  # fmt: skip
        return out

    def resolve(self, rows: dict[str, list[int]]) -> Path:
        decisions = self.root / "decisions"
        decisions.mkdir(parents=True, exist_ok=True)
        for product, product_rows in rows.items():
            for rec in self.records(product, product_rows):
                day = rec["decided_at"][:10]
                with (decisions / f"{day}.jsonl").open("a", encoding="utf-8", newline="\n") as fh:
                    fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
        for product, frame in self.frames.items():
            BarStore(self.root / "bars" / "1m").write(product, frame.reset_index())
        last = max(f.index[-1] for f in self.frames.values()).to_pydatetime()

        class Store:
            def complete_until(self, product_id: str) -> datetime:
                return last

        resolver = LabelResolver(
            decisions_dir=decisions, labels_dir=self.labels_dir, bars_root=self.root / "bars" / "1m", store=Store(),
            label=self.label, clock=lambda: last + timedelta(days=40), recompute_every_s=0.0,
        )  # fmt: skip
        resolver.run_once(force=True)
        resolver.close()
        return self.labels_dir

    def lines(self, folder: Path | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for p in sorted((folder or self.labels_dir).glob("*.jsonl")):
            out += [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
        return out


def cfg_for(tmp: Path, **kw: Any) -> TrainConfig:
    return TrainConfig(
        products=PRODUCTS, n_folds=N_FOLDS, val_frac=VAL_FRAC, embargo_days=2,
        sentiment_history_db=str(tmp / "no_news_history.db"), **kw,
    )  # fmt: skip


@dataclass
class World:
    tmp: Path
    live: Live
    cfg: TrainConfig
    lc: Path
    plain: Path
    live2: Path
    live1: Path
    dup_rows: dict[str, list[int]]
    rows: dict[str, list[int]]

    def build(self, name: str, live: LiveInput | None, *, jsonl: bool = False) -> Path:
        return build_dataset(
            self.cfg, LABEL, self.live.frames, self.tmp / name, label_config_path=self.lc, write_jsonl=jsonl, now=T0,
            live=live,
        )  # fmt: skip

    def samples(self, run: Path) -> pd.DataFrame:
        return pd.read_parquet(run / "samples.parquet")

    def manifest(self, run: Path) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
        return loaded


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> World:
    tmp = tmp_path_factory.mktemp("live")
    frames = {
        "BTC-EUR": synthetic_bars(days=235, seed=31, zero_volume_day=None, constant_days=None),
        "SOL-EUR": synthetic_bars(days=235, seed=32, zero_volume_day=None, constant_days=None),
    }
    lc = tmp / "label_config.json"
    lc.write_text('{"placeholder": true}')
    cfg = cfg_for(tmp)
    live = Live(tmp, frames, {p: bd.ProductFrames.load(p, f) for p, f in frames.items()}, LABEL)
    w = World(tmp, live, cfg, lc, Path(), Path(), Path(), {}, {})
    w.plain = w.build("plain", None, jsonl=True)
    hist = w.samples(w.plain)
    flat = hist[hist["position_side"] == "FLAT"]
    rng = np.random.default_rng(7)
    for p in PRODUCTS:
        mine = flat[(flat["product"] == p) & (flat["t"] > NAIVE_START + pd.Timedelta(days=6))
                    & (flat["t"] < NAIVE_START + pd.Timedelta(days=20))]  # fmt: skip
        w.dup_rows[p] = sorted(int(x) for x in rng.choice(mine["t_idx"].to_numpy(), size=12, replace=False))
        # days 5..40: in the training set of the folds (the first ones too); 160..166: ends inside the final
        # validation: no training set at all; 205..216: after the holdout start
        base = day_rows(5, 40, 0.25) + day_rows(60, 70, 0.25) + day_rows(160, 166, 0.25) + day_rows(205, 216, 0.5)
        w.rows[p] = sorted(set(base + w.dup_rows[p]))
    live.resolve(w.rows)
    w.live2 = w.build("live2", LiveInput(live.labels_dir, 2.0), jsonl=True)
    w.live1 = w.build("live1", LiveInput(live.labels_dir, 1.0))
    return w


def live_part(df: pd.DataFrame) -> pd.DataFrame:
    return df[df["source"] == "live"].reset_index(drop=True)


# --------------------------------------------------------------------------------------- what is in the rows


def test_live_rows_have_the_columns_and_dtypes_of_the_historical_rows(world: World) -> None:
    plain, live2 = world.samples(world.plain), world.samples(world.live2)
    assert [c for c in live2.columns if c != "source"] == list(plain.columns)
    assert dict(live2.drop(columns="source").dtypes) == dict(plain.dtypes)
    mine = live_part(live2)
    assert len(mine) > 300 and set(live2["source"]) == {"hist", "live"}
    assert (mine["position_side"] == "FLAT").all() and mine["product"].isin(PRODUCTS).all()
    assert not mine["w_action"].isna().any() and (mine["w_action"] > 0).all()


def test_live_states_are_the_recorded_production_states(world: World) -> None:
    mine = live_part(world.samples(world.live2))
    recorded = {(ln["product"], ln["t"]): ln["state"] for ln in world.live.lines() if ln["question_key"] == "action"}
    for r in mine.sample(60, random_state=3).to_dict("records"):
        state = DecisionState.model_validate_json(r["state"])
        assert r["state"] == serialize_state(state) == recorded[(r["product"], iso(pd.Timestamp(r["t"])))]
        assert state_sha256(r["state"]) == r["state_sha256"] and state.position.side == "FLAT"
        flat = flatten_state(state)
        for col in FLAT_COLUMNS:
            assert (np.isnan(flat[col]) and np.isnan(r[col])) or flat[col] == r[col], col
        assert (state.clock.hour_utc, state.clock.dow) == (pd.Timestamp(r["t"]).hour, pd.Timestamp(r["t"]).weekday())


def test_live_targets_are_the_labels_of_the_training_functions(world: World) -> None:
    """The row carries what ``triple_barrier`` / ``forward_rv_bucket`` give on the very bars: outcome, fill and exit
    bars, targets (the resolver wrote them, the builder must put them in the same columns with the same meaning)."""
    mine = live_part(world.samples(world.live2))
    cfg = LABEL.cfg
    checked = 0
    for product, pf in world.live.pf.items():
        sub = mine[mine["product"] == product]
        bars = pf.bars
        o, h, lo, c = (bars[k].to_numpy(dtype=np.float64) for k in ("open", "high", "low", "close"))
        rows = sub["t_idx"].to_numpy(dtype=np.int64)
        res = labeling.triple_barrier(o, h, lo, c, pf.ind[cfg.atr_column].to_numpy(dtype=np.float64), rows, cfg,
                                      LABEL.fees, LABEL.costs, 3.0)  # fmt: skip
        np.testing.assert_array_equal(sub["outcome"].to_numpy(), res.outcome)
        np.testing.assert_array_equal(sub["fill_idx"].to_numpy(), res.fill_idx)
        np.testing.assert_array_equal(sub["exit_idx"].to_numpy(), res.exit_idx)
        filled = res.outcome != labeling.NO_FILL
        np.testing.assert_array_equal(sub["edge_target"].to_numpy()[filled], (res.outcome[filled] == 1).astype(float))
        assert sub["edge_target"].isna().to_numpy()[~filled].all() and sub["w_edge"].isna().to_numpy()[~filled].all()
        buy = (res.outcome == 1) & (res.net_ret > 0)
        np.testing.assert_array_equal(sub["action_target"].to_numpy(), np.where(buy, "BUY", "HOLD"))
        np.testing.assert_allclose(sub["net_ret_bps"].to_numpy()[filled], 1e4 * res.net_ret[filled], atol=0.5)
        close_ts = pd.DatetimeIndex(bars.index[rows] + MIN)
        vol = labeling.forward_rv_bucket(aggregate_close(bars), close_ts, cfg.horizon_min)
        np.testing.assert_array_equal(sub["vol_target"].to_numpy(), vol)
        checked += len(sub)
    assert checked == len(mine) and (mine["outcome"] == labeling.NO_FILL).any() and (mine["vol_target"] >= 0).any()
    assert (mine["action_target"] == "BUY").any() and (mine["outcome"] == 1).any()


def aggregate_close(bars: pd.DataFrame) -> pd.Series:
    from modules.market.timeframes import aggregate_bars

    return aggregate_bars(bars, "1h")["close"]


def test_window_ends_are_the_single_definition(world: World) -> None:
    mine = live_part(world.samples(world.live2))
    want = labeling.label_window_end(pd.DatetimeIndex(mine["t"]).tz_localize("UTC"), LABEL.cfg).tz_convert(None)
    np.testing.assert_array_equal(pd.DatetimeIndex(mine["label_window_end"]).to_numpy(), want.to_numpy())


# --------------------------------------------------------------------------- the split rule (explained in FlatSplits)


def expected_candidates(world: World) -> pd.DataFrame:
    """Every distinct FLAT instant of the live labels that is not historical and not in the holdout period, with its
    window end: derived from the label LINES and the historical samples, not from the builder."""
    plain = world.samples(world.plain)
    hist_keys = {(r.product, r.t) for r in plain[plain["position_side"] == "FLAT"].itertuples()}
    seen: dict[tuple[str, pd.Timestamp], None] = {}
    for ln in world.live.lines():
        if ln["question_key"] == "action" and ln["position_side"] == "FLAT":
            t = pd.Timestamp(ln["t"]).tz_convert(None)
            if (ln["product"], t) not in hist_keys and t < HOLDOUT.tz_convert(None):
                seen[(ln["product"], t)] = None
    df = pd.DataFrame(list(seen), columns=["product", "t"])
    df["t1"] = labeling.label_window_end(pd.DatetimeIndex(df["t"]).tz_localize("UTC"), LABEL.cfg).tz_convert(None)
    return df


def boundaries(world: World) -> dict[str, pd.Timestamp]:
    """``val_start`` of every subset, from the definition of the splits (sez. 3.1.4) and the historical samples."""
    plain = world.samples(world.plain)
    flat = plain[plain["position_side"] == "FLAT"]
    hold = HOLDOUT.tz_convert(None)
    dev = flat[(flat["t"] < hold) & (flat["label_window_end"] < hold - EMBARGO)]
    start = dev["t"].min()
    block = (hold - start) / (N_FOLDS + 1)
    out = {f"fold_{k}": start + (block * (k + 1)) * (1.0 - VAL_FRAC) for k in range(N_FOLDS)}
    out["final"] = start + (hold - start) * (1.0 - VAL_FRAC)
    return out


def test_a_live_row_trains_a_subset_only_when_the_purge_allows_it(world: World) -> None:
    """Rule: the same purge as a historical training row (window ended before ``val_start - embargo``); the manifest
    counts, per subset, exactly the candidates the definition of the splits lets in."""
    cand = expected_candidates(world)
    bounds = boundaries(world)
    expect = {k: int(((cand["t"] < v) & (cand["t1"] < v - EMBARGO)).sum()) for k, v in bounds.items()}
    live = world.manifest(world.live2)["live"]
    assert live["rows_in_train"] == expect
    in_any = np.zeros(len(cand), dtype=bool)
    for v in bounds.values():
        in_any |= ((cand["t"] < v) & (cand["t1"] < v - EMBARGO)).to_numpy()
    assert live["rows"] == int(in_any.sum()) and live["excluded"]["purged"] == int((~in_any).sum())
    assert 0 < expect["fold_0"] < expect["fold_1"] <= expect["final"] and live["excluded"]["purged"] > 0
    # and the membership the trainers use says the same, row by row
    samples = world.samples(world.live2)
    members = bl.set_membership(samples, LABEL, world.cfg)
    mine = samples["source"] == "live"
    for name, v in bounds.items():
        tag = members[name][mine.to_numpy()]
        t, t1 = samples.loc[mine, "t"], pd.DatetimeIndex(samples.loc[mine, "label_window_end"])
        want = ((t < v) & (t1 < v - EMBARGO)).to_numpy()
        assert (tag == "train").tolist() == want.tolist(), name
        assert set(tag) <= {"train", ""}  # never validation, never test


def test_live_rows_never_reach_validation_test_or_holdout(world: World) -> None:
    samples = world.samples(world.live2)
    mine = samples[samples["source"] == "live"]
    assert (mine["fold_test"] == bd.FOLD_NONE).all() and mine["final_split"].isin(["train", "purged"]).all()
    assert (samples[samples["final_split"] == "holdout"]["source"] == "hist").all()
    assert (samples[samples["fold_test"] >= 0]["source"] == "hist").all()
    assert (samples[samples["final_split"] == "val"]["source"] == "hist").all()
    assert (mine["t"] < HOLDOUT.tz_convert(None)).all()
    members = bl.set_membership(samples, LABEL, world.cfg)
    for name, tags in members.items():
        assert not ((samples["source"] == "live").to_numpy() & np.isin(tags, ["val", "test"])).any(), name


def test_decisions_in_the_holdout_period_are_excluded_and_counted(world: World) -> None:
    held = sum(len(day_rows(205, 216, 0.5)) for _ in PRODUCTS)
    live = world.manifest(world.live2)["live"]
    assert live["excluded"]["holdout_period"] == held > 0


def test_the_yardsticks_do_not_move_when_live_labels_exist(world: World) -> None:
    """Validation, test and holdout of the training and of the evaluation are exactly the historical ones."""
    plain, live2 = world.samples(world.plain), world.samples(world.live2)
    hist = live2[live2["source"] == "hist"].drop(columns="source").reset_index(drop=True)
    pd.testing.assert_frame_equal(hist, plain)  # every historical row, weight, split: identical
    for name in ("val", "test"):
        assert (world.live2 / f"{name}.jsonl").read_bytes() == (world.plain / f"{name}.jsonl").read_bytes()
    ref, got = bl.set_membership(plain, LABEL, world.cfg), bl.set_membership(live2, LABEL, world.cfg)
    for name, tags in ref.items():
        assert (got[name][: len(plain)] == tags).all(), name


def test_flat_splits_train_only_blanks_the_live_rows_that_are_not_train() -> None:
    fs = FlatSplits(None, np.array([0]), np.array([1]), np.array([False, True, True, False]))  # type: ignore[arg-type]
    tag = np.array(["val", "val", "train", "test"], dtype=object)
    assert fs.train_only(tag).tolist() == ["val", "", "train", "test"]
    assert tag.tolist() == ["val", "val", "train", "test"]  # the input is untouched


# ------------------------------------------------------------------------------------------ duplicates


def test_no_instant_is_twice_in_the_dataset_and_the_historical_row_wins(world: World) -> None:
    plain, live2 = world.samples(world.plain), world.samples(world.live2)
    flat = live2[live2["position_side"] == "FLAT"]
    assert not flat.duplicated(["product", "t"]).any()
    live_lines = world.live.lines()
    planted = {(p, iso(NAIVE_START + pd.Timedelta(minutes=i + 1))) for p, rows in world.dup_rows.items() for i in rows}
    assert len({(ln["product"], ln["t"]) for ln in live_lines} & planted) == len(planted) == 24  # they were labelled
    key = flat.set_index(["product", "t"])
    plain_key = plain[plain["position_side"] == "FLAT"].set_index(["product", "t"])
    for p, t in list(planted):
        stamp = pd.Timestamp(t).tz_convert(None)
        assert key.loc[(p, stamp), "source"] == "hist"
        assert key.loc[(p, stamp), "state_sha256"] == plain_key.loc[(p, stamp), "state_sha256"]
        assert key.loc[(p, stamp), "w_action"] == plain_key.loc[(p, stamp), "w_action"]
    assert world.manifest(world.live2)["live"]["excluded"]["duplicate_historical"] >= len(planted)


def test_two_books_at_the_same_instant_give_one_row(world: World) -> None:
    lines = [ln for ln in world.live.lines() if ln["question_key"] == "action"]
    assert len(lines) == 2 * len({(ln["product"], ln["t"]) for ln in lines})  # champion and random_entry
    live = world.manifest(world.live2)["live"]
    assert live["excluded"]["duplicate_book"] == len(lines) // 2 > 0
    assert live["decisions"] == len(lines) // 2


# ------------------------------------------------------------------------------------------- the weights


def spans(df: pd.DataFrame, n: int, close0: np.datetime64) -> dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """The spans of sez. 3.1.4, written once more from the spec: [t, exit] with a fill, [t, t + F + T_in - 1] for the
    action of a NO_FILL, [t, label_window_end] for the regime."""
    cfg = LABEL.cfg
    t0 = df["t_idx"].to_numpy(dtype=np.int64)
    filled = (df["outcome"] != labeling.NO_FILL).to_numpy()
    exit_ = df["exit_idx"].to_numpy(dtype=np.int64)
    end = np.where(filled, exit_, t0 + cfg.entry_first_bar + cfg.entry_timeout_min - 1)
    win = df["label_window_end"].to_numpy(dtype="datetime64[ns]")
    vol_end = np.minimum(np.rint((win - close0) / np.timedelta64(1, "m")).astype(np.int64), n - 1)
    return {
        "action": (np.ones(len(df), dtype=bool), t0, end),
        "edge": (filled, t0, exit_),
        "vol": ((df["vol_target"] >= 0).to_numpy(), t0, np.maximum(vol_end, t0)),
    }


def joint_weights(world: World, run: Path, product: str, weight: float) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """(expected live weights, plain weights) per question, from uniqueness over the historical + live spans."""
    s = world.samples(run)
    h = s[(s["product"] == product) & (s["position_side"] == "FLAT") & (s["source"] != "live")]
    lv = s[(s["product"] == product) & (s["source"] == "live")]
    frame = world.live.frames[product]
    n, close0 = len(frame), (frame.index[0] + MIN).tz_convert(None).to_datetime64()
    sh, sl = spans(h, n, close0), spans(lv, n, close0)
    out = {}
    for q, col in (("action", "w_action"), ("edge", "w_edge"), ("vol", "w_vol")):
        (mh, ah, bh), (ml, al, bl_) = sh[q], sl[q]
        alone = uniqueness_weights(ah[mh], bh[mh])
        np.testing.assert_array_equal(alone, h[col].to_numpy()[mh])  # the spans ARE the ones of build_product_rows
        joint = uniqueness_weights(np.concatenate([ah[mh], al[ml]]), np.concatenate([bh[mh], bl_[ml]]))
        want = np.full(len(lv), np.nan)
        want[ml] = joint[int(mh.sum()) :] * weight
        out[col] = (want, lv[col].to_numpy())
    return out


def test_live_weight_is_the_uniqueness_weight_times_live_weight(world: World) -> None:
    for product in PRODUCTS:
        for col, (want, got) in joint_weights(world, world.live2, product, 2.0).items():
            np.testing.assert_allclose(got, want, rtol=1e-12, equal_nan=True, err_msg=col)
    mine = live_part(world.samples(world.live2))
    assert mine["w_action"].gt(0).all() and mine["w_edge"].notna().any() and mine["w_vol"].notna().any()


def test_the_live_weight_scales_only_the_live_rows(world: World) -> None:
    a, b = live_part(world.samples(world.live1)), live_part(world.samples(world.live2))
    assert len(a) == len(b) > 0
    for col in ("w_action", "w_edge", "w_vol"):
        np.testing.assert_allclose(b[col].to_numpy(), 2.0 * a[col].to_numpy(), rtol=1e-12, equal_nan=True)
    h1 = world.samples(world.live1).query("source == 'hist'")
    h2 = world.samples(world.live2).query("source == 'hist'")
    pd.testing.assert_frame_equal(h1.reset_index(drop=True), h2.reset_index(drop=True))  # historical rows: untouched
    for col in ("state_sha256", "t_idx", "outcome", "action_target", "fold_test", "final_split"):
        assert (a[col] == b[col]).all()
    ratio = (b["w_action"] / a["w_action"]).dropna()
    assert np.allclose(ratio, 2.0)


def test_a_live_row_inside_a_covered_window_weighs_less_than_alone(world: World) -> None:
    """The concurrency counts the historical rows too: against the live rows alone the weights are smaller."""
    mine = live_part(world.samples(world.live1))
    s = world.samples(world.live1)
    alone_total = 0.0
    for product in PRODUCTS:
        lv = mine[mine["product"] == product]
        frame = world.live.frames[product]
        sl = spans(lv, len(frame), (frame.index[0] + MIN).tz_convert(None).to_datetime64())
        m, a, b = sl["action"]
        alone_total += float(uniqueness_weights(a[m], b[m]).sum())
    assert float(mine["w_action"].sum()) < alone_total and float(s["w_action"].sum()) > 0


# -------------------------------------------------------------------------------------------- manifest


def test_manifest_records_live_rows_weight_and_exclusions(world: World) -> None:
    m, plain_m = world.manifest(world.live2), world.manifest(world.plain)
    assert "live" not in plain_m
    live = m["live"]
    mine = live_part(world.samples(world.live2))
    assert live["weight"] == 2.0 and live["cutoff"] is None and live["labels_dir"] == str(world.live.labels_dir)
    assert live["rows"] == len(mine) > 0
    assert live["rows_by_product"] == {p: int((mine["product"] == p).sum()) for p in PRODUCTS}
    for col in ("w_action", "w_edge", "w_vol"):
        assert live["weight_sums"][col] == pytest.approx(float(np.nansum(mine[col])), rel=1e-12)
    assert set(live["excluded"]) == set(bd.LIVE_EXCLUSIONS)
    assert live["lines_read"] == len(world.live.lines()) and live["bad_lines"] == 0
    accounted = live["rows"] + sum(v for k, v in live["excluded"].items() if k != "duplicate_book")
    assert accounted == live["decisions"]  # every decision is a row or has exactly one reason
    assert set(live["rows_in_train"]) == {f"fold_{k}" for k in range(N_FOLDS)} | {"final"}
    # the historical numbers of the manifest are the historical ones
    assert m["rows"] == plain_m["rows"] and m["rows_per_fold_test"] == plain_m["rows_per_fold_test"]
    assert m["label_config_sha256"] == plain_m["label_config_sha256"] and m["sentiment_available"] is False


def test_jsonl_marks_live_records_and_keeps_validation_and_test_apart(world: World) -> None:
    def read(path: Path) -> list[dict[str, Any]]:
        return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]

    train = read(world.live2 / "train.jsonl")
    live_recs = [r for r in train if r.get("source") == "live"]
    assert live_recs and all(r["split"] == "train" and r["position_side"] == "FLAT" for r in live_recs)
    assert all("source" not in r for r in train if r.get("source") != "live")
    assert not any("source" in r for n in ("val", "test") for r in read(world.live2 / f"{n}.jsonl"))
    plain_train = read(world.plain / "train.jsonl")
    assert not any("source" in r for r in plain_train)
    assert set(plain_train[0]) | {"source"} >= set(live_recs[0])
    for q in ("action", "edge"):
        w = [r["weight"] for r in train if r["question_key"] == q]
        assert np.mean(w) == pytest.approx(1.0, abs=1e-9)  # still normalised to mean 1 after the live multiplication
    one = next(r for r in live_recs if r["question_key"] == "edge")
    assert one["options"][one["target_index"]] == one["target"] and one["meta"]["questions_version"]


# ------------------------------------------------------------------------------------- exclusions by reason


def planted_defects(world: World, dest: Path) -> dict[str, int]:
    """A copy of the live labels plus one extra file with one decision per defect. Returns the planted counts."""
    dest.mkdir(parents=True, exist_ok=True)
    for p in world.live.labels_dir.glob("*.jsonl"):
        (dest / p.name).write_bytes(p.read_bytes())
    groups: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for ln in world.live.lines():
        if ln["product"] == "SOL-EUR" and ln["decision_id"].startswith("cha"):
            groups.setdefault((ln["product"], ln["t"]), {})[ln["question_key"]] = ln
    filled = next(g for k, g in sorted(groups.items()) if "edge" in g and "vol_regime" in g and k[1] > "2024-01-20")
    nofill = next(g for k, g in sorted(groups.items()) if "edge" not in g and k[1] > "2024-01-20")
    extra: list[dict[str, Any]] = []
    counts: dict[str, int] = {}

    def plant(
        reason: str, group: dict[str, dict[str, Any]], minute: int, edit: Any = None, t: str | None = None
    ) -> None:
        t_new = t or iso(NAIVE_START.tz_localize("UTC") + pd.Timedelta(days=30, minutes=minute))
        for key, ln in group.items():
            c = copy.deepcopy(ln)
            c["t"], c["decision_id"] = t_new, f"planted-{reason}-{minute}"
            if edit is not None:
                c = edit(key, c)
            if c is not None:
                c["id"] = f"{c['product']}|{c['t']}|{c['position_side']}|{c['question_key']}"
                extra.append(c)
        counts[reason] = counts.get(reason, 0) + 1

    def to_long(key: str, c: dict[str, Any]) -> dict[str, Any]:
        c["position_side"] = "LONG"
        return c

    def drop_action(key: str, c: dict[str, Any]) -> dict[str, Any] | None:
        return None if key == "action" else c

    def drop_edge(key: str, c: dict[str, Any]) -> dict[str, Any] | None:
        return None if key == "edge" else c

    def bad_target(key: str, c: dict[str, Any]) -> dict[str, Any]:
        if key == "action":
            c["target"] = "SELL"
        return c

    def product_eth(key: str, c: dict[str, Any]) -> dict[str, Any]:
        c["product"] = "ETH-EUR"
        return c

    def other_sha(key: str, c: dict[str, Any]) -> dict[str, Any]:
        c["meta"]["label_config_sha256"] = "0" * 64
        return c

    def broken_state(key: str, c: dict[str, Any]) -> dict[str, Any]:
        c["state"] = "{}"
        return c

    plant("not_flat", filled, 1, to_long)
    plant("incomplete", filled, 2, drop_action)
    plant("incomplete", filled, 3, drop_edge)
    plant("incomplete", filled, 4, bad_target)
    plant("unknown_product", filled, 5, product_eth)
    plant("label_config_mismatch", filled, 6, other_sha)
    plant("bad_state", filled, 7, broken_state)
    plant("bar_missing", filled, 8, t="2030-01-01T00:00:00Z")
    plant("before_history", nofill, 9, t="2024-01-01T03:00:00Z")
    dup = filled["action"]
    extra.append(copy.deepcopy(dup))  # the very same line again (another book): not a new decision
    counts["duplicate_book"] = 1
    text = "".join(json.dumps(c, separators=(",", ":")) + "\n" for c in extra)
    text += json.dumps({**dup, "split": "train", "id": "x|y|FLAT|action"}) + "\n"  # not a live line
    text += '{"id":"SOL-EUR|2024-03-01T00:00:00Z|FLAT|act'  # the torn last line of a crashed writer
    (dest / "zz-planted.jsonl").write_text(text, encoding="utf-8")
    counts["bad_lines"] = 2
    return counts


def test_every_defect_is_excluded_for_its_own_reason(world: World) -> None:
    folder = world.tmp / "defects_in"
    planted = planted_defects(world, folder)
    out = world.build("defects", LiveInput(folder, 2.0))
    base, got = world.manifest(world.live2)["live"], world.manifest(out)["live"]
    for reason in bd.LIVE_EXCLUSIONS:
        assert got["excluded"][reason] - base["excluded"][reason] == planted.get(reason, 0), reason
    assert got["bad_lines"] - base["bad_lines"] == planted["bad_lines"]
    assert got["rows"] == base["rows"] and got["rows_in_train"] == base["rows_in_train"]
    pd.testing.assert_frame_equal(world.samples(out), world.samples(world.live2))  # the defects changed nothing else


def test_a_label_window_past_the_cutoff_is_out_and_one_ending_exactly_at_it_is_in(world: World) -> None:
    pick = day_rows(10, 10.01, 1)[0]
    t = NAIVE_START + pd.Timedelta(minutes=pick + 1)
    cutoff = labeling.label_window_end(pd.DatetimeIndex([t]).tz_localize("UTC"), LABEL.cfg)[0]
    out = world.build("cutoff", LiveInput(world.live.labels_dir, 2.0, cutoff.to_pydatetime()))
    got, mine = world.manifest(out)["live"], live_part(world.samples(out))
    assert got["cutoff"] == iso(cutoff) and (mine["label_window_end"] <= cutoff.tz_convert(None)).all()
    assert (mine["label_window_end"] == cutoff.tz_convert(None)).any()  # the boundary row is in
    later = pd.Timestamp(t) + pd.Timedelta(hours=6)
    assert not ((mine["product"] == "SOL-EUR") & (mine["t"] == later)).any()
    cand = expected_candidates(world)
    assert got["excluded"]["window_after_cutoff"] > 0
    inside = int((cand["t1"] <= cutoff.tz_convert(None)).sum())
    assert got["rows"] + got["excluded"]["purged"] == inside  # what the cutoff lets through is then purged or kept


def test_when_nothing_qualifies_the_dataset_is_the_historical_one_byte_for_byte(world: World) -> None:
    only_holdout = world.tmp / "holdout_only"
    only_holdout.mkdir()
    keep = [ln for ln in world.live.lines() if pd.Timestamp(ln["t"]).tz_convert(None) >= HOLDOUT.tz_convert(None)]
    (only_holdout / "2024-07-21.jsonl").write_text("".join(json.dumps(x) + "\n" for x in keep), encoding="utf-8")
    out = world.build("none", LiveInput(only_holdout, 2.0), jsonl=True)
    for name in ("samples.parquet", "train.jsonl", "val.jsonl", "test.jsonl", "label_config.json"):
        assert (out / name).read_bytes() == (world.plain / name).read_bytes(), name
    assert "source" not in world.samples(out).columns
    live = world.manifest(out)["live"]
    assert live["rows"] == 0 and live["excluded"]["holdout_period"] > 0 and live["weight"] == 2.0
    assert all(v == 0 for v in live["rows_in_train"].values()) and live["weight_sums"] == {
        "w_action": 0.0, "w_edge": 0.0, "w_vol": 0.0,
    }  # fmt: skip
    plain_m = world.manifest(world.plain)
    assert {k: v for k, v in world.manifest(out).items() if k not in ("live", "run_id", "git_commit")} == {
        k: v for k, v in plain_m.items() if k not in ("run_id", "git_commit")
    }


def test_an_empty_folder_and_a_missing_folder(world: World) -> None:
    empty = world.tmp / "empty_labels"
    empty.mkdir()
    out = world.build("empty", LiveInput(empty, 2.0))
    assert (out / "samples.parquet").read_bytes() == (world.plain / "samples.parquet").read_bytes()
    live = world.manifest(out)["live"]
    assert live["rows"] == 0 and live["lines_read"] == 0 and live["decisions"] == 0
    with pytest.raises(bd.DatasetError, match="does not exist"):
        world.build("missing", LiveInput(world.tmp / "nowhere", 2.0))


@pytest.mark.parametrize("weight", [0.0, -1.0, float("nan"), float("inf")])
def test_the_live_weight_must_be_a_positive_number(weight: float) -> None:
    with pytest.raises(bd.DatasetError, match="live_weight"):
        LiveInput(Path("x"), weight)


# --------------------------------------------------------------------------------------- non regression

# Fingerprints of the dataset of tests/test_build_dataset.py (235 days, BTC + SOL, 3 folds) built by the code BEFORE the
# live input existed (git HEAD 1535920). Without live labels the dataset must not change by a bit.
PINNED = {
    "content": "a36edb8680e23ec476af69c6b499ebc3cc84922cf3685dedb6da374933347ca7",
    "train": "98dba45d6463d817ef9eb131d6d7736e982952052387aeca8f64970e48468de2",
    "val": "79b73ef010ef226f203221ca9d21c1136090ea500f16af787c1afe37018078e7",
    "test": "de1e0312d8bb513c702bed303fd83a9678f3da45a6adc43e1f7c0952690c21ca",
}


def fingerprint(run: Path) -> dict[str, str]:
    df = pd.read_parquet(run / "samples.parquet")
    h = hashlib.sha256()
    h.update(",".join(f"{c}:{df[c].dtype}" for c in df.columns).encode())
    h.update(pd.util.hash_pandas_object(df, index=False).to_numpy().tobytes())
    out = {"content": h.hexdigest()}
    for name in ("train", "val", "test"):
        out[name] = hashlib.sha256((run / f"{name}.jsonl").read_bytes()).hexdigest()
    return out


def test_without_live_labels_the_dataset_is_what_it_was_before(tmp_path: Path) -> None:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(labeling, "VOL_REF_DAYS", ORIG_VOL_REF)  # the pinned dataset was built with the real reference
        frames = {
            "BTC-EUR": synthetic_bars(days=235, seed=31, zero_volume_day=None, constant_days=None),
            "SOL-EUR": synthetic_bars(days=235, seed=32, zero_volume_day=None, constant_days=None),
        }
        lc = tmp_path / "lc.json"
        lc.write_text('{"placeholder": true}')
        label = loaded_label({"BTC-EUR": 3.0, "SOL-EUR": 3.0}, holdout_start=HOLDOUT)
        cfg = TrainConfig(products=PRODUCTS, n_folds=3, sentiment_history_db=str(tmp_path / "none.db"))
        no_live = build_dataset(cfg, label, frames, tmp_path / "a", label_config_path=lc, now=T0)
        assert fingerprint(no_live) == PINNED
        assert "live" not in json.loads((no_live / "manifest.json").read_text())
        assert "source" not in pd.read_parquet(no_live / "samples.parquet").columns


# ----------------------------------------------------------------------------------------------- the CLI


def test_cli_passes_the_live_input_and_defaults_the_weight_to_the_app_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "train.yml"
    cfg.write_text(f"products: [SOL-EUR]\ndatasets_dir: {tmp_path.as_posix()}/ds\n", encoding="utf-8")
    monkeypatch.setattr(bd, "load_label_config", lambda p: LABEL)
    monkeypatch.setattr(bd, "load_bars", lambda root, product: pd.DataFrame())
    seen: list[LiveInput | None] = []

    def fake_build(*a: Any, live: LiveInput | None = None, **k: Any) -> Path:
        seen.append(live)
        return tmp_path

    monkeypatch.setattr(bd, "build_dataset", fake_build)
    assert bd.main(["--config", str(cfg)]) == 0 and seen[-1] is None
    assert bd.main(["--config", str(cfg), "--live-labels", "L", "--live-cutoff", "2026-11-01T00:00:00Z"]) == 0
    assert seen[-1] == LiveInput(Path("L"), 2.0, datetime(2026, 11, 1, tzinfo=UTC))  # 2.0 = dry_run.learner.live_weight
    assert bd.main(["--config", str(cfg), "--live-labels", "L", "--live-weight", "3.5"]) == 0
    assert seen[-1] == LiveInput(Path("L"), 3.5, None)
    assert bd.main(["--config", str(cfg), "--live-labels", "L", "--live-weight", "0"]) == EXIT_CONFIG_ERROR
    assert bd.main(["--config", str(cfg), "--live-labels", "L", "--live-cutoff", "yesterday"]) == EXIT_CONFIG_ERROR


# ---------------------------------------------------------------------- N_eff of the OOF calibration


def planted_with_live(w_live: float, n: int = 6000, n_live: int = 1500) -> tuple[pd.DataFrame, np.ndarray]:
    """Rows with a learnable edge; the last ``n_live`` train rows are live rows whose raw weights are ``w_live``."""
    rng = np.random.default_rng(4)
    x = rng.normal(size=(n, len(FLAT_COLUMNS)))
    df = pd.DataFrame(x, columns=list(FLAT_COLUMNS))
    edge = (x[:, FLAT_COLUMNS.index("ret_1d_bps")] + rng.normal(0, 1.0, n) > 0.3).astype(float)
    df["position_side"], df["product"] = "FLAT", np.array(["BTC-EUR", "SOL-EUR"])[np.arange(n) % 2]
    df["t"] = pd.date_range("2024-01-01", periods=n, freq="1h")
    df["label_window_end"] = df["t"] + pd.Timedelta(hours=6)
    df["edge_target"], df["vol_target"], df["w_vol"] = edge, -1, np.nan
    df["action_target"] = np.where((edge == 1) & (rng.uniform(size=n) < 0.5), "BUY", "HOLD")
    df["w_action"] = df["w_edge"] = 0.5 * rng.uniform(0.5, 1.5, n)
    n_tr, n_va = int(n * 0.7), int(n * 0.15)
    member = np.array(["train"] * n_tr + ["val"] * n_va + ["test"] * (n - n_tr - n_va), dtype=object)
    is_live = np.zeros(n, dtype=bool)
    is_live[n_tr - n_live : n_tr] = True
    df["source"] = np.where(is_live, "live", "hist")
    df.loc[is_live, ["w_action", "w_edge"]] *= w_live
    return df, member


def test_the_calibration_n_eff_counts_live_rows_with_their_real_weight() -> None:
    cfg = TrainConfig()
    results = {}
    for w_live in (1.0, 4.0):
        df, member = planted_with_live(w_live)
        res = bl.fit_subset(df, member, cfg, tag=0, allow_degenerate=True, rho=1.0)
        ftr, fva = (bl._edge_frame(df, member == s) for s in ("train", "val"))
        tr = bl._select(df, member == "train", "edge", bl.MARKET_FEATURES)
        z = bl.oof_edge_scores(ftr, tr.x, tr.y, res["best_iteration"]["edge"], timedelta(days=cfg.embargo_days), 0)
        done = np.isfinite(z)
        in_c = df.loc[ftr.index[done], "source"].eq("live").to_numpy()
        w_c = np.concatenate([ftr["w_edge"].to_numpy()[done], fva["w_edge"].to_numpy()])
        want = float(w_c.sum() / (1.0 + (2 - 1) * 1.0))
        assert res["edge_cal"]["n_eff"] == pytest.approx(want, rel=1e-12)
        results[w_live] = (res["edge_cal"]["n_eff"], float(ftr["w_edge"].to_numpy()[done][in_c].sum()), int(in_c.sum()))
    (n1, live_w1, k1), (n4, live_w4, k4) = results[1.0], results[4.0]
    assert k1 == k4 > 0 and live_w4 == pytest.approx(4.0 * live_w1)
    assert n4 - n1 == pytest.approx((live_w4 - live_w1) / 2.0)  # K = 2, rho = 1: the live rows add 3x their weight


# ------------------------------------------------------------------------ the learner, end to end


@pytest.fixture(scope="module")
def e2e(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """330 days; live decisions of days 5..150 resolved by the real resolver; the learner builds the dataset WITH them
    (``TrainingPipeline`` semantics through the real ``build_dataset``) and trains the real baseline on it."""
    tmp = tmp_path_factory.mktemp("e2e")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(labeling, "VOL_REF_DAYS", ORIG_VOL_REF)
        mp.setattr(bl, "CAL_NEFF_MIN", 1.0)  # the synthetic history is far below the real N_eff floor
        hold = pd.Timestamp("2024-10-27", tz="UTC")
        label = loaded_label({"BTC-EUR": 3.0, "SOL-EUR": 3.0}, holdout_start=hold)
        frames = {
            "BTC-EUR": synthetic_bars(days=330, seed=31, zero_volume_day=None, constant_days=None),
            "SOL-EUR": synthetic_bars(days=330, seed=32, zero_volume_day=None, constant_days=None),
        }
        cfg = TrainConfig(products=PRODUCTS, n_folds=2, val_frac=0.4, sentiment_history_db=str(tmp / "none.db"))
        lc = tmp / "label_config.json"
        lc.write_text("{}")
        world = Live(tmp, frames, {p: bd.ProductFrames.load(p, f) for p, f in frames.items()}, label)
        world.resolve({p: day_rows(5, 150, 0.25) for p in PRODUCTS})

        class Pipe:
            def __init__(self) -> None:
                self.live_seen: list[LiveInput | None] = []

            def build_dataset(self, live: LiveInput | None = None) -> Path:
                self.live_seen.append(live)
                return build_dataset(cfg, label, frames, tmp / "datasets", label_config_path=lc, write_jsonl=False,
                                     now=T0, live=live)  # fmt: skip

            def train(self, dataset: Path) -> Path:
                samples = pd.read_parquet(dataset / "samples.parquet")
                return bl.train_baseline(samples, label, cfg, dataset, tmp / "models", label_config_path=lc,
                                         allow_degenerate_final=True, now=T0)  # fmt: skip

            def tune(self, model: Path, dataset: Path) -> None:
                (model / "final" / "policy.json").write_text(json.dumps({"untunable": True}), encoding="utf-8")

            def evaluate(self, model: Path, dataset: Path) -> dict[str, Any]:
                return {"exit_code": 1, "status": "NO-GO", "gates": {}}

        pipe = Pipe()
        paths = LearnerPaths(
            reports_dir=tmp / "reports", decisions_dir=tmp / "decisions", live_labels_dir=world.labels_dir,
            bars_root=tmp / "bars" / "1m", fill_model_path=tmp / "registry" / "fill_model.json", label_config=lc,
            work_dir=tmp / "work",
        )  # fmt: skip
        learner = NightlyLearner(
            cfg=LearnerConfig(live_weight=2.0, min_n_eff_calibration=1), paths=paths,
            registry=FilesystemModelRegistry(tmp / "registry"), products=PRODUCTS,
            clock=lambda: datetime(2026, 11, 1, 0, 15, tzinfo=UTC), pipeline=pipe, label=label,
        )  # fmt: skip
        doc = learner.run(date(2026, 11, 1))
        # the same data and model WITHOUT live labels, for the comparison of the calibration sample size
        base_ds = build_dataset(cfg, label, frames, tmp / "datasets_plain", label_config_path=lc,
                                write_jsonl=False, now=T0)  # fmt: skip
        base_model = bl.train_baseline(pd.read_parquet(base_ds / "samples.parquet"), label, cfg, base_ds,
                                       tmp / "models_plain", label_config_path=lc, allow_degenerate_final=True,
                                       now=T0)  # fmt: skip
    return {"doc": doc, "pipe": pipe, "tmp": tmp, "base_model": base_model, "registry": learner._registry}


def test_learner_builds_the_dataset_with_the_live_labels_and_the_weight(e2e: dict[str, Any]) -> None:
    doc, pipe = e2e["doc"], e2e["pipe"]
    assert {s: r["outcome"] for s, r in doc["steps"].items() if s in ("L3", "L1", "L2")} == {
        "L3": "ok", "L1": "ok", "L2": "ok",
    }, doc["steps"]  # fmt: skip
    (live,) = pipe.live_seen
    assert live.weight == 2.0 and live.cutoff == datetime(2026, 11, 1, tzinfo=UTC)
    d = doc["steps"]["L3"]["details"]
    assert d["live_weight"] == 2.0 and d["live_weight_applied"] is True
    block = d["live_dataset"]
    assert block["rows"] > 500 and block["weight"] == 2.0 and block["rows_in_train"]["final"] > 0
    assert block["rows_in_train"]["fold_0"] < block["rows_in_train"]["fold_1"] < block["rows_in_train"]["final"]
    assert doc["steps"]["L2"]["details"]["version"]
    assert e2e["registry"].get("challenger").manifest["live_dataset"] == block


def test_the_trained_model_records_the_live_rows_and_calibrates_with_their_weight(e2e: dict[str, Any]) -> None:
    doc = e2e["doc"]
    model = Path(doc["steps"]["L3"]["artifacts"][0])
    card = json.loads((model / "model_card.json").read_text(encoding="utf-8"))
    assert card["data"]["live"] == doc["steps"]["L3"]["details"]["live_dataset"]
    base = json.loads((e2e["base_model"] / "model_card.json").read_text(encoding="utf-8"))
    assert "live" not in base["data"]
    for sub in ("fold_1", "final"):
        with_live, without = card["folds"][sub]["edge_cal"], base["folds"][sub]["edge_cal"]
        assert with_live["n_rows_oof"] > without["n_rows_oof"] and with_live["n_eff"] > without["n_eff"], sub
        assert card["folds"][sub]["n_rows"]["edge"]["train"] > base["folds"][sub]["n_rows"]["edge"]["train"]
        assert card["folds"][sub]["n_rows"]["edge"]["val"] == base["folds"][sub]["n_rows"]["edge"]["val"]
    assert card["folds"]["final"]["n_rows"]["action"]["val"] == base["folds"]["final"]["n_rows"]["action"]["val"]


def test_live_rows_that_are_all_purged_leave_the_historical_dataset_untouched(world: World) -> None:
    """Rows exist but none can train anything (their windows end inside the final validation): same bytes as without."""
    folder = world.tmp / "purged_only"
    folder.mkdir()
    lo, hi = START + pd.Timedelta(days=160), START + pd.Timedelta(days=167)
    keep = [ln for ln in world.live.lines() if lo <= pd.Timestamp(ln["t"]) < hi]
    (folder / "2024-06-09.jsonl").write_text("".join(json.dumps(x) + "\n" for x in keep), encoding="utf-8")
    out = world.build("purged_only", LiveInput(folder, 2.0))
    assert (out / "samples.parquet").read_bytes() == (world.plain / "samples.parquet").read_bytes()
    live = world.manifest(out)["live"]
    assert live["rows"] == 0 and live["excluded"]["purged"] > 0
    assert live["decisions"] == sum(v for k, v in live["excluded"].items() if k != "duplicate_book")
