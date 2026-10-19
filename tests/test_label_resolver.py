"""LabelResolver: live labels from the recorded decisions, with the very functions of the historical dataset.

Two kinds of tests. The parity test builds a real dataset (``training.build_dataset``) and resolves, through the
resolver, one ``DecisionRecord`` per FLAT sample of it: every label must coincide. The others use small synthetic
stores and hand-made records to pin the readiness rule, the idempotency / restart protocol and the final cases
(NO_FILL, missing ATR, LONG state, torn file).
"""

from __future__ import annotations

import ast
import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from modules.dryrun import label_resolver as resolver_module
from modules.dryrun.label_resolver import LabelResolver
from modules.market.backfill import BarStore, load_bars
from modules.market.indicators_batch import compute_indicators
from modules.risk.costs import round_bps
from tests.indicator_fixtures import synthetic_bars
from tests.label_fixtures import loaded_label
from training import build_dataset as bd
from training import labeling
from training.build_dataset import build_dataset
from training.config import TrainConfig

PRODUCT = "SOL-EUR"
MIN = timedelta(minutes=1)
HOUR = timedelta(hours=1)
LABEL = loaded_label({PRODUCT: 3.0}, atr_tf="1h", horizon_min=600, k_tp=1.0, k_sl=1.0)
SOURCE = Path(resolver_module.__file__)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Store:
    until: datetime

    def complete_until(self, product_id: str) -> datetime:
        return self.until


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def write_bars(root: Path, frame: pd.DataFrame, product: str = PRODUCT) -> None:
    BarStore(root / "bars" / "1m").write(product, frame.reset_index())


def rising_bars(days: int = 12) -> pd.DataFrame:
    """A price that only goes up, every low above the previous close: a maker order below the market never fills."""
    n = days * 1440
    idx = pd.date_range("2024-01-01", periods=n, freq="1min", tz="UTC", name="ts")
    close = 100.0 + 0.01 * np.arange(n)
    open_ = np.concatenate([[100.0], close[:-1]])
    return pd.DataFrame(
        {"open": open_, "high": close + 0.001, "low": open_, "close": close, "volume": np.full(n, 5.0)}, index=idx
    )


def state_json(side: str = "FLAT", atr_bps: int | None = 0) -> str:
    return json.dumps(
        {
            "v": "chronos.state.v1",
            "product": PRODUCT,
            "position": {"side": side},
            "volatility": {"atr_14_1h_bps": atr_bps},
        },
        separators=(",", ":"),
    )


@dataclass
class World:
    root: Path
    bars: pd.DataFrame
    ind: pd.DataFrame
    clock: Clock
    store: Store
    opened: list[LabelResolver] = field(default_factory=list)

    @property
    def last_close(self) -> datetime:
        return self.bars.index[-1].to_pydatetime() + MIN

    def record(
        self, i: int, side: str = "FLAT", *, did: str | None = None, answers: bool = True, **kw: Any
    ) -> dict[str, Any]:
        """The DecisionRecord of the bar at row ``i`` (its ``ts_bar_close`` is the close of that bar)."""
        t = self.bars.index[i].to_pydatetime() + MIN
        atr_bps = self.ind["atr_14_1h_bps"].iloc[i]
        token = None if np.isnan(atr_bps) else int(round(float(atr_bps)))
        rec: dict[str, Any] = {
            "decision_id": did or f"d{i:06d}", "ts_bar_close": iso(t), "decided_at": iso(t + 2 * MIN) + "",
            "product": PRODUCT, "book": "champion", "state": state_json(side, token), "questions_version": "Q1",
            "policy": {"policy_version": "m1", "final": "BUY"},
            "answers": {
                "action": {"choice": "BUY", "probabilities": {"BUY": 0.7, "SELL": 0.1, "HOLD": 0.2}, "confidence": 0.3},
                "edge": {"noul": 0.61},
                "vol_regime": {"score": 2.3, "legend": {}, "probabilities": {}, "confidence": 0.1},
            } if answers else None,
        }  # fmt: skip
        rec.update(kw)
        return rec

    def write_decisions(self, records: list[dict[str, Any]]) -> None:
        d = self.root / "decisions"
        d.mkdir(parents=True, exist_ok=True)
        for r in records:
            day = datetime.fromisoformat(r["decided_at"].replace("Z", "+00:00")).date()
            with (d / f"{day.isoformat()}.jsonl").open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(r, separators=(",", ":")) + "\n")

    def resolver(self, **kw: Any) -> LabelResolver:
        args: dict[str, Any] = {
            "decisions_dir": self.root / "decisions", "labels_dir": self.root / "live_labels",
            "bars_root": self.root / "bars" / "1m", "store": self.store, "label": LABEL, "clock": self.clock,
            "recompute_every_s": 0.0,
        }  # fmt: skip
        r = LabelResolver(**{**args, **kw})
        self.opened.append(r)
        return r

    def label_lines(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for p in sorted((self.root / "live_labels").glob("*.jsonl")):
            out += [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
        return out


def build_world(tmp_path: Path, bars: pd.DataFrame | None = None, *, now_offset: timedelta = 2 * HOUR) -> World:
    bars = synthetic_bars(days=12, seed=5, zero_volume_day=None, constant_days=None) if bars is None else bars
    write_bars(tmp_path, bars)
    last = bars.index[-1].to_pydatetime()
    return World(tmp_path, bars, compute_indicators(bars), Clock(last + now_offset), Store(last))


@pytest.fixture
def world(tmp_path: Path) -> Any:
    w = build_world(tmp_path)
    yield w
    for r in w.opened:
        r.close()


def window_end(w: World, i: int) -> datetime:
    """t + (F + T_in - 1) min + H for an hourly sample: the fill may be on the last bar of the entry window."""
    t = w.bars.index[i].to_pydatetime() + MIN
    return t + timedelta(minutes=LABEL.cfg.entry_first_bar + LABEL.cfg.entry_timeout_min - 1 + LABEL.cfg.horizon_min)


def row_at(w: World, hours: float) -> int:
    """Row of the bar that closes ``hours`` hours after the start of the data (a whole hour: an hourly sample)."""
    return int(hours * 60) - 1


# ============================================================================================ readiness


def test_nothing_is_resolved_before_the_label_window_ends(world: World) -> None:
    i = row_at(world, 100)
    world.write_decisions([world.record(i)])
    end = window_end(world, i)
    r = world.resolver()
    world.clock.now = end - MIN
    assert r.run_once().resolved == 0 and world.label_lines() == []
    world.clock.now = end
    rep = r.run_once()
    assert rep.resolved == 1 and rep.lines == len(world.label_lines()) >= 2


def test_a_decision_waits_until_the_store_is_complete_up_to_the_end_of_its_window(world: World) -> None:
    i = row_at(world, 100)
    world.write_decisions([world.record(i)])
    end = window_end(world, i)
    world.store.until = end - 2 * MIN  # the last bar the label reads is the one that starts at end - 1 min
    r = world.resolver()
    rep = r.run_once()
    assert (rep.resolved, rep.deferred) == (0, 1) and world.label_lines() == []
    world.store.until = end - MIN
    assert r.run_once().resolved == 1


def test_bars_are_loaded_with_the_completeness_of_the_store_not_with_a_guess(world: World) -> None:
    src = SOURCE.read_text(encoding="utf-8")
    assert "complete_until(" in src and "store_complete_until_end=True" in src


def test_indicators_are_not_rebuilt_more_often_than_asked(world: World) -> None:
    world.write_decisions([world.record(row_at(world, 100))])
    r = world.resolver(recompute_every_s=3600.0)
    assert r.run_once().resolved == 1
    world.write_decisions([world.record(row_at(world, 101))])
    assert r.run_once().resolved == 0  # the product was resolved a moment ago
    assert r.run_once(force=True).resolved == 1
    world.clock.now += 2 * HOUR
    world.write_decisions([world.record(row_at(world, 102))])
    assert r.run_once().resolved == 1  # an hour later the normal pass picks it up


# ============================================================================================ the lines


def test_the_lines_have_the_dataset_format_plus_the_resolved_label_fields(world: World) -> None:
    i = row_at(world, 100)
    rec = world.record(i)
    world.write_decisions([rec])
    world.resolver().run_once()
    lines = world.label_lines()
    assert [ln["question_key"] for ln in lines][0] == "action" and {ln["question_key"] for ln in lines} <= {
        "action", "edge", "vol_regime",
    }  # fmt: skip
    t = iso(world.bars.index[i].to_pydatetime() + MIN)
    for ln in lines:
        assert list(ln)[0] == "id" and ln["id"] == f"{PRODUCT}|{t}|FLAT|{ln['question_key']}"
        assert ln["split"] == "live" and ln["fold"] == -1 and ln["weight"] == 1.0 and ln["t"] == t
        assert ln["state"] == rec["state"] and ln["position_side"] == "FLAT" and ln["product"] == PRODUCT
        assert ln["options"][ln["target_index"]] == ln["target"]
        assert ln["decision_id"] == rec["decision_id"] and ln["policy_final"] == "BUY"
        assert ln["predicted"] == {
            "action": {"BUY": 0.7, "SELL": 0.1, "HOLD": 0.2}, "edge": 0.61, "vol_regime": 2.3,
        }  # fmt: skip
        assert ln["resolved_at"] == iso(world.clock.now)
        m = ln["meta"]
        assert set(m) == {
            "barrier_outcome", "net_ret_bps", "fill_minute", "exit_minute", "atr_tf", "atr_bps",
            "label_config_sha256", "questions_version", "sentiment_dropped",
        }  # fmt: skip
        assert m["atr_tf"] == "1h" and m["label_config_sha256"] == LABEL.sha256 and m["questions_version"] == "Q1"
    kinds = {ln["question_key"]: ln for ln in lines}
    assert kinds["action"]["question_type"] == "choice" and kinds["action"]["options"] == ["BUY", "SELL", "HOLD"]
    if "edge" in kinds:
        assert kinds["edge"]["question_type"] == "noul" and kinds["edge"]["options"] == ["true", "false"]


def test_a_decision_without_answers_has_null_predictions(world: World) -> None:
    world.write_decisions([world.record(row_at(world, 100), answers=False)])
    world.resolver().run_once()
    assert world.label_lines()[0]["predicted"] == {"action": None, "edge": None, "vol_regime": None}


def test_the_label_matches_a_direct_call_of_the_training_functions(world: World) -> None:
    rows = [row_at(world, h) for h in (60, 70, 80, 90, 100)]
    world.write_decisions([world.record(i) for i in rows])
    world.resolver().run_once()
    b = world.bars
    res = labeling.triple_barrier(
        *(b[k].to_numpy(dtype=np.float64) for k in ("open", "high", "low", "close")),
        world.ind["atr_14_1h"].to_numpy(dtype=np.float64), np.array(rows), LABEL.cfg, LABEL.fees, LABEL.costs, 3.0,
    )  # fmt: skip
    by_t = {ln["t"]: ln for ln in world.label_lines() if ln["question_key"] == "action"}
    for k, i in enumerate(rows):
        ln = by_t[iso(b.index[i].to_pydatetime() + MIN)]
        up = res.outcome[k] == labeling.UPPER and res.net_ret[k] > 0
        assert ln["target"] == ("BUY" if up else "HOLD")
        assert (
            ln["meta"]["barrier_outcome"] == {0: "NO_FILL", 1: "UPPER", 2: "LOWER", 3: "VERTICAL"}[int(res.outcome[k])]
        )
        assert ln["meta"]["net_ret_bps"] == (None if np.isnan(res.net_ret[k]) else int(round_bps(1e4 * res.net_ret[k])))


def test_the_resolver_uses_the_label_functions_of_training_and_defines_none_of_its_own() -> None:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    imported = {
        a.name
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and n.module == "training.labeling"
        for a in n.names
    }
    assert {"triple_barrier", "forward_rv_bucket", "label_window_end"} <= imported
    defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert not defined & {"maker_entry_limit", "sl_exit_px", "tp_touched", "taker_entry_px", "market_exit_px"}
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
    }
    assert not names & {"sl_exit_px", "maker_entry_filled", "tp_touched"}  # the kernels stay behind triple_barrier


# ============================================================================================ final cases


def test_no_fill_is_an_action_hold_without_an_edge_label(tmp_path: Path) -> None:
    w = build_world(tmp_path, rising_bars())
    w.write_decisions([w.record(row_at(w, 100))])
    w.resolver().run_once()
    lines = w.label_lines()
    assert [ln["question_key"] for ln in lines] == ["action"]
    assert lines[0]["target"] == "HOLD" and lines[0]["meta"]["barrier_outcome"] == "NO_FILL"
    assert lines[0]["meta"]["net_ret_bps"] is None and lines[0]["meta"]["fill_minute"] is None
    assert lines[0]["meta"]["exit_minute"] is None


def test_a_sample_without_an_atr_is_final_and_never_retried(world: World) -> None:
    rec = world.record(5 * 60 - 1)  # five hours into the data: the 1h ATR needs 14 closed hours
    world.write_decisions([rec])
    r = world.resolver()
    rep = r.run_once(force=True)
    assert (rep.resolved, rep.tombstoned) == (0, 1) and world.label_lines() == [] and r.stats.no_atr == 1
    assert r.run_once(force=True).tombstoned == 0  # it is in the table now
    again = world.resolver()  # and a restart does not retry it either
    assert again.run_once(force=True).tombstoned == 0 and again.stats.no_atr == 0


def test_a_long_state_gets_no_action_label_but_keeps_edge_and_vol_regime(world: World) -> None:
    flat, long_ = world.record(row_at(world, 100), did="flat"), world.record(row_at(world, 100), "LONG", did="long")
    world.write_decisions([flat, long_])
    world.resolver().run_once()
    keys = {(ln["decision_id"], ln["question_key"]) for ln in world.label_lines()}
    assert ("flat", "action") in keys and ("long", "action") not in keys
    if ("flat", "edge") in keys:
        assert ("long", "edge") in keys
    assert any(ln["position_side"] == "LONG" for ln in world.label_lines())


def test_records_without_a_state_are_ignored_and_a_torn_line_is_counted(world: World) -> None:
    good = world.record(row_at(world, 100))
    broken = {"decision_id": "e1", "ts_bar_close": good["ts_bar_close"], "decided_at": good["decided_at"],
              "product": PRODUCT, "error": {"stage": "state"}, "policy": {"final": "HOLD"}}  # fmt: skip
    world.write_decisions([broken, good])
    day = datetime.fromisoformat(good["decided_at"].replace("Z", "+00:00")).date()
    with (world.root / "decisions" / f"{day}.jsonl").open("a", encoding="utf-8") as fh:
        fh.write('{"decision_id":"torn","ts_bar_cl')  # a record cut by a crash
    r = world.resolver()
    assert r.run_once().resolved == 1 and r.stats.bad_lines == 1
    assert {ln["decision_id"] for ln in world.label_lines()} == {good["decision_id"]}


def test_the_atr_token_of_the_record_is_checked_against_the_stored_bars(world: World) -> None:
    i = row_at(world, 100)
    world.write_decisions([world.record(i, did="ok"), world.record(i + 60, did="bad", state=state_json("FLAT", 1))])
    r = world.resolver()
    assert r.run_once().resolved == 2 and r.stats.atr_state_mismatch == 1  # counted, still labelled


def test_each_model_is_labelled_with_its_own_label_config(world: World) -> None:
    other = loaded_label({PRODUCT: 3.0}, atr_tf="1h", horizon_min=600, k_tp=3.0, k_sl=0.5)
    i = row_at(world, 100)
    a = world.record(i, did="a", policy={"policy_version": "m1", "final": "BUY"})
    b = world.record(i, did="b", policy={"policy_version": "m2", "final": "BUY"})
    world.write_decisions([a, b])
    world.resolver(label_by_model={"m2": other}).run_once()
    sha = {ln["decision_id"]: ln["meta"]["label_config_sha256"] for ln in world.label_lines()}
    assert sha == {"a": LABEL.sha256, "b": other.sha256}


# ================================================================ idempotent, restartable, crash-proof


def test_a_second_pass_and_a_restart_write_nothing(world: World) -> None:
    world.write_decisions([world.record(row_at(world, h)) for h in (60, 80, 100)])
    first = world.resolver()
    n = first.run_once().lines
    before = sorted((world.root / "live_labels").glob("*.jsonl"))
    snap = {p.name: p.read_bytes() for p in before}
    assert n == len(world.label_lines()) > 0
    assert first.run_once(force=True).resolved == 0
    restarted = world.resolver()
    assert restarted.run_once(force=True).resolved == 0
    assert {p.name: p.read_bytes() for p in (world.root / "live_labels").glob("*.jsonl")} == snap


def test_the_books_decide_at_the_same_instants_and_each_decision_gets_its_lines(world: World) -> None:
    i = row_at(world, 100)
    world.write_decisions([world.record(i, did=f"{book}-1", book=book) for book in ("champion", "challenger")])
    r = world.resolver()
    assert r.run_once().resolved == 2
    lines = world.label_lines()
    per = {d: [ln["question_key"] for ln in lines if ln["decision_id"] == d] for d in ("champion-1", "challenger-1")}
    assert per["champion-1"] == per["challenger-1"] and "action" in per["champion-1"]
    assert len({ln["id"] for ln in lines}) < len(lines)  # the dataset id is shared: the decision tells them apart
    assert world.resolver().run_once(force=True).resolved == 0  # and a restart does not write either again


def test_files_are_named_after_the_day_of_the_decision(world: World) -> None:
    world.write_decisions([world.record(row_at(world, h)) for h in (60, 100)])  # day 3 and day 5
    world.resolver().run_once()
    names = sorted(p.name for p in (world.root / "live_labels").glob("*.jsonl"))
    assert names == ["2024-01-03.jsonl", "2024-01-05.jsonl"]


def test_a_crash_between_the_file_and_the_table_converges_without_duplicates(world: World) -> None:
    rec = world.record(row_at(world, 100))
    world.write_decisions([rec])
    first = world.resolver()
    first.run_once()
    full = world.label_lines()
    assert len(full) >= 2
    # simulate the crash: the file has the first line and half of the second, the table has no row
    day_file = next((world.root / "live_labels").glob("*.jsonl"))
    data = day_file.read_bytes().split(b"\n")
    day_file.write_bytes(data[0] + b"\n" + data[1][:40])
    first.close()
    import sqlite3

    db = sqlite3.connect(world.root / "live_labels" / "resolved.db")
    db.execute("DELETE FROM resolved")
    db.commit()
    db.close()
    again = world.resolver()
    assert again.run_once(force=True).resolved == 1
    assert world.label_lines() == full  # the torn half line is gone and nothing is written twice
    assert again.run_once(force=True).resolved == 0


def test_the_loop_survives_a_failed_pass(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    r = world.resolver()
    calls = {"n": 0}

    def flaky(*, force: bool = False) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk")
        return None

    monkeypatch.setattr(r, "run_once", flaky)

    async def sleep(seconds: float) -> None:
        if calls["n"] >= 3:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(r.run(60.0, sleep=sleep))
    assert calls["n"] == 3 and r.stats.failures == 1


# ============================================================================== parity with the dataset

HOLDOUT = pd.Timestamp("2024-07-20", tz="UTC")
DS_LABEL = loaded_label({PRODUCT: 3.0}, holdout_start=HOLDOUT)


@dataclass
class Parity:
    samples: pd.DataFrame
    dataset: dict[str, dict[str, Any]]
    lines: list[dict[str, Any]]
    qversion: str


@pytest.fixture(scope="module")
def parity(tmp_path_factory: pytest.TempPathFactory) -> Parity:
    """A real dataset of 235 days, and the resolver run over one DecisionRecord per FLAT sample of it."""
    tmp = tmp_path_factory.mktemp("parity")
    frame = synthetic_bars(days=235, seed=32, zero_volume_day=None, constant_days=None)
    lc_path = tmp / "label_config.json"
    lc_path.write_text('{"placeholder": true}')
    cfg = TrainConfig(products=[PRODUCT], n_folds=3, sentiment_history_db=str(tmp / "no_news_history.db"))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            labeling, "VOL_REF_DAYS", 30
        )  # 235 days of data: a year of reference would leave no vol_regime label
        out = build_dataset(
            cfg, DS_LABEL, {PRODUCT: frame}, tmp / "out", label_config_path=lc_path,
            now=datetime(2026, 10, 18, 12, 0, tzinfo=UTC),
        )  # fmt: skip
        samples = pd.read_parquet(out / "samples.parquet")
        flat = samples[samples["position_side"] == "FLAT"].reset_index(drop=True)
        qversion = json.loads((out / "manifest.json").read_text())["questions_version"]
        dataset: dict[str, dict[str, Any]] = {}
        for name in ("train", "val", "test"):
            for ln in (out / f"{name}.jsonl").read_text(encoding="utf-8").splitlines():
                rec = json.loads(ln)
                if rec["position_side"] == "FLAT":
                    dataset[rec["id"]] = rec
        write_bars(tmp, frame)
        last = frame.index[-1].to_pydatetime()
        records = []
        for r in flat.to_dict("records"):
            t = pd.Timestamp(r["t"]).to_pydatetime().replace(tzinfo=UTC)
            records.append({
                "decision_id": f"{PRODUCT}|{iso(t)}", "ts_bar_close": iso(t), "decided_at": iso(t + 2 * MIN),
                "product": PRODUCT, "state": r["state"], "questions_version": qversion,
                "policy": {"policy_version": "m", "final": "HOLD"}, "answers": None,
            })  # fmt: skip
        w = World(tmp, frame, compute_indicators(frame), Clock(last + 40 * 24 * HOUR), Store(last))
        w.write_decisions(records)
        resolver = w.resolver(label=DS_LABEL)
        resolver.run_once(force=True)
        lines = w.label_lines()
        resolver.close()
    return Parity(flat, dataset, lines, qversion)


def test_label_resolver_parity(parity: Parity) -> None:
    """Live labels and the historical dataset's labels coincide on the very same timestamps."""
    by_id = {ln["id"]: ln for ln in parity.lines}
    assert len(by_id) == len(parity.lines)  # no id twice
    n_action = n_edge = n_vol = 0
    for r in parity.samples.to_dict("records"):
        stamp = pd.Timestamp(r["t"]).strftime("%Y-%m-%dT%H:%M:%SZ")
        base = f"{PRODUCT}|{stamp}|FLAT|"
        assert by_id[base + "action"]["target"] == r["action_target"]
        n_action += 1
        edge = by_id.get(base + "edge")
        if np.isnan(r["edge_target"]):
            assert edge is None  # NO_FILL: no edge label in the dataset either
        else:
            assert edge is not None and edge["target"] == ("true" if r["edge_target"] == 1.0 else "false")
            n_edge += 1
        vol = by_id.get(base + "vol_regime")
        if r["vol_target"] < 0:
            assert vol is None
        else:
            assert vol is not None and vol["target"] == str(int(r["vol_target"]))
            n_vol += 1
    assert n_action > 500 and n_edge > 200 and n_vol > 200  # the comparison is not vacuous
    assert any(ln["target"] == "BUY" for ln in parity.lines if ln["question_key"] == "action")
    assert any(ln["meta"]["barrier_outcome"] == "NO_FILL" for ln in parity.lines)


def test_label_resolver_meta_equals_the_dataset_meta(parity: Parity) -> None:
    """Everything the training JSONL says about a label (outcome, net return, minutes, ATR) is said identically."""
    compared = 0
    for ln in parity.lines:
        want = parity.dataset.get(ln["id"])
        if want is None:  # a sample the dataset purged from every split
            continue
        for k in ("target", "target_index", "options", "question_type", "state", "position_side", "t"):
            assert ln[k] == want[k], (ln["id"], k)
        assert ln["meta"] == want["meta"], ln["id"]
        compared += 1
    assert compared > 500 and parity.qversion == next(iter(parity.dataset.values()))["meta"]["questions_version"]
    assert bd.OUTCOME_NAMES == {0: "NO_FILL", 1: "UPPER", 2: "LOWER", 3: "VERTICAL"}  # the names this module repeats


def test_the_store_round_trip_does_not_alter_a_bar(tmp_path: Path) -> None:
    """The labels read the stored bars: what the store gives back is, bit for bit, what the dataset builder read."""
    frame = synthetic_bars(days=3, seed=9, zero_volume_day=None, constant_days=None)
    write_bars(tmp_path, frame)
    back = load_bars(tmp_path / "bars" / "1m", PRODUCT)
    pd.testing.assert_frame_equal(back, frame, check_freq=False)
