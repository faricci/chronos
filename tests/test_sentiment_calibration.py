"""Calibration (sez. 1.3 punto 6), label sampling / review (sez. 1.1.2) and the ONNX export helpers."""

from __future__ import annotations

import csv
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest

from modules.sentiment.calibration import (
    ECE_MAX,
    IntArray,
    brier,
    calibrate,
    ece,
    fit_temperature,
    nll,
    read_labels,
    run,
)
from modules.sentiment.classifier import FloatArray, OnnxSentimentClassifier, load_model_card, save_model_card, softmax
from modules.sentiment.export_model import derive_label_map
from modules.sentiment.label_review import ReviewStats, read_proposals, read_rows, review
from modules.sentiment.label_sample import labeled_ids, stratified_sample, write_todo
from tests.sentiment_helpers import T0, FakeBackend, card, item

# ------------------------------------------------------------------------------------------ metrics


def synthetic(n: int, true_t: float, seed: int = 0) -> tuple[FloatArray, IntArray]:
    """Logits that are ``true_t`` times too sharp: the labels are drawn from softmax(z / true_t)."""
    rng = np.random.default_rng(seed)
    z = rng.normal(0, 2.0, size=(n, 3))
    p = softmax(z / true_t)
    labels = np.array([rng.choice(3, p=row) for row in p], dtype=np.int64)
    return z, labels


def test_ece_of_a_perfectly_calibrated_toy_case() -> None:
    probs = np.array([[0.8, 0.1, 0.1]] * 10)
    labels = np.array([0] * 8 + [1] * 2)  # right 80% of the time at confidence 0.8
    assert ece(probs, labels) == pytest.approx(0.0, abs=1e-12)
    assert ece(probs, np.array([1] * 10)) == pytest.approx(0.8)  # always wrong at 0.8 confidence


def test_ece_is_between_zero_and_one_and_bins_are_15() -> None:
    z, y = synthetic(500, 1.0)
    e = ece(softmax(z), y)
    assert 0.0 <= e <= 1.0
    assert ece(np.array([[1.0, 0.0, 0.0]]), np.array([0])) == 0.0  # confidence exactly 1.0 lands in the last bin


def test_brier_bounds() -> None:
    assert brier(np.array([[1.0, 0.0, 0.0]]), np.array([0])) == 0.0
    assert brier(np.array([[0.0, 0.0, 1.0]]), np.array([0])) == pytest.approx(2.0)
    assert brier(np.full((4, 3), 1 / 3), np.array([0, 1, 2, 0])) == pytest.approx(2 / 3)


def test_fit_temperature_recovers_the_sharpness() -> None:
    z, y = synthetic(6000, true_t=2.5, seed=1)
    t = fit_temperature(z, y)
    assert t == pytest.approx(2.5, rel=0.12)
    assert nll(z, y, t) < nll(z, y, 1.0)


def test_fit_temperature_respects_bounds() -> None:
    z = np.array([[5.0, 0.0, 0.0], [0.0, 5.0, 0.0], [0.0, 0.0, 5.0]] * 20)
    y = np.array([1, 2, 0] * 20)  # always wrong: the NLL wants T as large as allowed
    assert fit_temperature(z, y) == pytest.approx(10.0, abs=1e-3)


def test_calibrate_improves_ece_on_the_held_out_30_percent() -> None:
    z, y = synthetic(4000, true_t=3.0, seed=2)
    r = calibrate(z, y, seed=0)
    assert (r.n_fit, r.n_eval) == (2800, 1200)
    assert r.ece_after < r.ece_before and r.brier_after < r.brier_before and r.nll_after < r.nll_before
    assert r.ece_after <= ECE_MAX
    assert calibrate(z, y, seed=0) == r  # reproducible


def test_calibrate_refuses_tiny_samples() -> None:
    with pytest.raises(ValueError, match="not enough"):
        calibrate(np.zeros((3, 3)), np.zeros(3, dtype=np.int64))


# --------------------------------------------------------------------------------------- CLI logic


def write_labels(path: Path, rows: list[tuple[str, int | str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["news_id", "source", "asset", "title", "summary", "label", "proposed"])
        for i, (title, label) in enumerate(rows):
            w.writerow([f"id{i}", "s", "SOL", title, "", label, ""])


def labelled_world(tmp_path: Path, n: int, t_true: float) -> tuple[Path, OnnxSentimentClassifier, Path]:
    z, y = synthetic(n, t_true, seed=3)
    rows = [(f"headline {i}", int(label)) for i, label in enumerate(y)]
    backend = FakeBackend({f"headline {i} .": tuple(z[i]) for i in range(n)})  # type: ignore[misc]
    labels_path = tmp_path / "headlines.csv"
    write_labels(labels_path, rows)
    card_path = tmp_path / "card.json"
    save_model_card(card_path, card(model_id="m", version="1"))
    return labels_path, OnnxSentimentClassifier(backend, card()), card_path


def test_run_writes_the_card_with_calibrated_true(tmp_path: Path) -> None:
    labels_path, clf, card_path = labelled_world(tmp_path, 400, 2.0)
    code, report = run(labels_path, clf, card_path, strict=False, batch_size=64)
    assert code == 0
    c = load_model_card(card_path)
    assert c.calibrated is True and c.temperature == pytest.approx(report.temperature) and c.temperature > 1.2
    assert c.model_extra is not None and c.model_extra["calibration_n_labels"] == 400
    assert c.model_extra["calibration_ece_after"] == pytest.approx(report.ece_after)
    assert c.model_id == "m" and c.label_map == {0: 0, 1: 1, 2: 2}


def test_strict_fails_without_touching_the_card(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # 60 labels all wrong against confident logits: ECE after calibration stays far above 0.07
    z = np.tile(np.array([[6.0, 0.0, 0.0]]), (60, 1))
    backend = FakeBackend({f"headline {i} .": tuple(z[i]) for i in range(60)})  # type: ignore[misc]
    labels_path = tmp_path / "headlines.csv"
    write_labels(labels_path, [(f"headline {i}", 1 + i % 2) for i in range(60)])
    card_path = tmp_path / "card.json"
    save_model_card(card_path, card())
    before = card_path.read_text()
    code, report = run(labels_path, OnnxSentimentClassifier(backend, card()), card_path, strict=True)
    assert code == 1 and report.ece_after > ECE_MAX
    assert card_path.read_text() == before
    assert "FAILED" in capsys.readouterr().out


def test_read_labels_ignores_skipped_rows(tmp_path: Path) -> None:
    p = tmp_path / "h.csv"
    write_labels(p, [("a", 0), ("b", ""), ("c", 2)])
    texts, labels = read_labels(p)
    assert texts == ["a . ", "c . "] and labels.tolist() == [0, 2]


def test_derive_label_map_from_model_config() -> None:
    assert derive_label_map({0: "Bearish", 1: "Neutral", 2: "Bullish"}) == {0: 0, 1: 1, 2: 2}
    assert derive_label_map({"0": "positive", "1": "negative", "2": "neutral"}) == {0: 2, 1: 0, 2: 1}  # FinBERT
    with pytest.raises(ValueError, match="unknown class"):
        derive_label_map({0: "LABEL_0", 1: "LABEL_1", 2: "LABEL_2"})


# ----------------------------------------------------------------------------------- label sample


def test_stratified_sample_covers_every_source_and_asset() -> None:
    items = (
        [item(f"btc {i}", source="big", assets=("BTC",), ingested_at=T0 + timedelta(seconds=i)) for i in range(100)]
        + [item(f"sol {i}", source="big", assets=("SOL",)) for i in range(5)]
        + [item(f"eth {i}", source="small", assets=("ETH",)) for i in range(3)]
    )
    sample = stratified_sample(items, 9, seed=1)
    assert len(sample) == 9 and len({s.news_id for s in sample}) == 9
    strata = {(s.source, s.keys[0]) for s in sample}
    assert strata == {("big", "BTC"), ("big", "SOL"), ("small", "ETH")}
    assert sum(s.keys[0] == "BTC" for s in sample) == 3  # equal allocation, not proportional
    assert stratified_sample(items, 9, seed=1) == sample  # reproducible
    assert [s.news_id for s in stratified_sample(items, 9, seed=2)] != [s.news_id for s in sample]


def test_sample_takes_everything_when_n_exceeds_the_pool() -> None:
    items = [item(f"story {i} SOL", assets=("SOL",)) for i in range(4)]
    assert len(stratified_sample(items, 100)) == 4
    assert stratified_sample([], 10) == []


def test_write_todo_and_labeled_ids(tmp_path: Path) -> None:
    its = [item("Solana up", assets=("SOL",), summary="s"), item("SEC delays", is_market_wide=True)]
    out = tmp_path / "labels" / "headlines_todo.csv"
    write_todo(out, its)
    rows = read_rows(out)
    assert [r["asset"] for r in rows] == ["SOL", "MARKET"] and rows[0]["news_id"] == its[0].news_id
    assert labeled_ids(tmp_path / "missing.csv") == set()
    assert labeled_ids(out) == {i.news_id for i in its}


# ----------------------------------------------------------------------------------- label review


def todo_rows(n: int) -> list[dict[str, str]]:
    return [
        {"news_id": f"id{i}", "source": "s", "asset": "SOL", "title": f"title {i}", "summary": f"sum {i}"}
        for i in range(n)
    ]


def keys(*ks: str) -> Iterator[str]:
    yield from ks


def run_review(
    tmp_path: Path, todo: list[dict[str, str]], proposals: dict[str, tuple[str, str]], *ks: str
) -> tuple[ReviewStats, list[str]]:
    it = keys(*ks)
    shown: list[str] = []
    stats = review(todo, proposals, tmp_path / "headlines.csv", read_key=lambda: next(it), say=shown.append)
    return stats, shown


def test_review_accept_correct_skip(tmp_path: Path) -> None:
    props = {"id0": ("2", "bullish headline"), "id1": ("0", "bearish"), "id2": ("1", "meh"), "id3": ("1", "meh")}
    stats, shown = run_review(tmp_path, todo_rows(4), props, "\r", "1", "s", "\n")
    assert (stats.accepted, stats.corrected, stats.skipped, stats.quit_early) == (2, 1, 1, False)
    rows = read_rows(tmp_path / "headlines.csv")
    assert [(r["news_id"], r["label"], r["proposed"]) for r in rows] == [
        ("id0", "2", "2"),
        ("id1", "1", "0"),
        ("id2", "", "1"),  # skipped: empty label
        ("id3", "1", "1"),
    ]
    assert any("proposed: 2 BULLISH - bullish headline" in line for line in shown)


def test_review_saves_every_answer_and_resumes(tmp_path: Path) -> None:
    todo = todo_rows(5)
    props = {r["news_id"]: ("1", "") for r in todo}
    stats, _ = run_review(tmp_path, todo, props, "\r", "0", "q")
    assert stats.quit_early and stats.answered == 2
    assert [r["news_id"] for r in read_rows(tmp_path / "headlines.csv")] == ["id0", "id1"]

    stats2, shown = run_review(tmp_path, todo, props, "\r", "\r", "\r")  # resume: only id2..id4 are asked
    assert stats2.answered == 3 and not stats2.quit_early
    assert [r["news_id"] for r in read_rows(tmp_path / "headlines.csv")] == ["id0", "id1", "id2", "id3", "id4"]
    assert not any("title 0" in line for line in shown)
    # exactly one header row
    assert (tmp_path / "headlines.csv").read_text().count("news_id,source") == 1


def test_review_enter_without_proposal_is_ignored_and_other_keys_too(tmp_path: Path) -> None:
    stats, _ = run_review(tmp_path, todo_rows(1), {}, "\r", "x", "9", "2")
    assert stats.corrected == 1 and read_rows(tmp_path / "headlines.csv")[0]["label"] == "2"


def test_review_ctrl_c_stops_cleanly(tmp_path: Path) -> None:
    calls = iter(["1"])

    def read_key() -> str:
        try:
            return next(calls)
        except StopIteration:
            raise KeyboardInterrupt from None

    stats = review(todo_rows(3), {}, tmp_path / "headlines.csv", read_key=read_key, say=lambda _: None)
    assert stats.quit_early and stats.answered == 1


def test_read_proposals_accepts_both_column_names(tmp_path: Path) -> None:
    p = tmp_path / "p.csv"
    p.write_text("news_id,proposed_label,reason\na,2,good\nb,7,bad\nc,,none\n", encoding="utf-8")
    assert read_proposals(p) == {"a": ("2", "good")}
    assert read_proposals(tmp_path / "missing.csv") == {}
