"""Paired comparison on the common OOS (rule L5, P2/S3) and the OOS artifacts ``evaluate`` writes for it."""

from __future__ import annotations

import json
from dataclasses import astuple
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from tests.label_fixtures import loaded_label
from training import oos_compare
from training.backtest import Span, equity_sharpe_and_drawdown
from training.evaluate import (
    OOS_SPANS,
    OOS_TRADE_FIELDS,
    OOS_TRADES,
    BlockedValues,
    Candidates,
    Evaluation,
    _ge,
    block_bootstrap,
    block_grid,
    draw_block_starts,
    effective_trades,
    oos_block,
    oos_spans_doc,
    oos_trade_rows,
    trade_weights,
    write_oos_artifacts,
)
from training.fold_spans import FoldSpans, SpanSet
from training.oos_compare import (
    common_spans,
    compare_models,
    load_oos_artifacts,
    oos_metrics,
    paired_oos_comparison,
)

T0 = pd.Timestamp("2025-01-01", tz="UTC")
LABEL = loaded_label({"A": 3.0})
START = 10_000.0


def day(n: float) -> pd.Timestamp:
    return T0 + pd.Timedelta(days=n)


def book(
    pnls: list[float], first_day: float = 1.0, every: float = 3.0, product: str = "A", fold: int = 0
) -> pd.DataFrame:
    """One trade every ``every`` days, two hours long, decided one minute before the fill."""
    rows = []
    for i, pnl in enumerate(pnls):
        fill = day(first_day + i * every)
        rows.append({
            "product": product, "fold": fold, "decision_ts": fill - pd.Timedelta(minutes=1), "fill_ts": fill,
            "exit_ts": fill + pd.Timedelta(hours=2), "exit_reason": "TP", "net_ret_bps": pnl / 2.0, "pnl_eur": pnl,
            "notional_eur": 200.0, "neff_weight": 1.0,
        })  # fmt: skip
    return pd.DataFrame(rows, columns=list(OOS_TRADE_FIELDS))


SPANS = [(day(0), day(150)), (day(150), day(300))]


def noisy(seed: int, n: int = 90, mean: float = 1.0) -> list[float]:
    return [float(x) for x in np.random.default_rng(seed).normal(mean, 4.0, n)]


# ----------------------------------------------------------------------------------- spans


def test_common_oos_is_span_intersection() -> None:
    a = [(day(0), day(100)), (day(100), day(200))]
    b = [(day(50), day(150)), (day(250), day(300))]
    assert common_spans(a, b) == [(day(50), day(100)), (day(100), day(150))]
    assert common_spans(a, [(day(200), day(210))]) == []  # touching is empty: [start, end) is half-open
    assert common_spans([], b) == []


def test_trades_outside_the_common_span_are_excluded() -> None:
    ch = book([5.0] * 10)  # days 1..28
    cp = book([5.0] * 10)
    inside = [(day(0), day(10))]
    out = paired_oos_comparison(ch, cp, inside, inside, 16, 200, 1, 0.05, START)
    assert out.challenger.n_trades == out.champion.n_trades == 4  # decisions at days 1, 4, 7 and ~9.99 (< day 10)
    assert out.common_days == pytest.approx(10.0)
    with pytest.raises(ValueError, match="in common"):
        paired_oos_comparison(ch, cp, [(day(0), day(5))], [(day(6), day(9))], 16, 200, 1, 0.05, START)


def test_span_end_is_exclusive_and_start_inclusive() -> None:
    edge = book([5.0] * 3)
    edge["decision_ts"] = [day(0), day(10), day(5)]  # on the start, on the end, inside
    spans = [(day(0), day(10))]
    out = paired_oos_comparison(edge, edge, spans, spans, 16, 100, 1, 0.05, START)
    assert out.challenger.n_trades == out.champion.n_trades == 2
    assert oos_metrics(edge, spans, START).n_trades == 2


# ------------------------------------------------------------------------------ the bootstrap


def test_identical_models_quantile_is_zero() -> None:
    t = book(noisy(1))
    out = paired_oos_comparison(t, t.copy(), SPANS, SPANS, 16, 2000, 1234, 0.05, START)
    assert out.d_pnl_quantile == 0.0 and out.d_pnl_mean == 0.0  # not > 0: rule L5 rejects "no difference"
    assert out.challenger == out.champion


def test_same_blocks_for_both_models() -> None:
    """The champion holds every trade of the challenger plus extra winners: with the SAME blocks D = -(extras in the
    blocks) can never be positive. Independent draws would put a large part of the replicates above zero."""
    base = book(noisy(2), every=3.0)
    extra = book([50.0] * 20, first_day=2.0, every=13.0)
    cp = pd.concat([base, extra], ignore_index=True)
    out = paired_oos_comparison(base, cp, SPANS, SPANS, 16, 3000, 7, 0.999, START)
    assert out.d_pnl_quantile <= 1e-9
    assert out.d_pnl_mean < 0.0


def test_bootstrap_deterministic_with_seed() -> None:
    ch, cp = book(noisy(3, mean=2.0)), book(noisy(4, mean=0.5))
    a = paired_oos_comparison(ch, cp, SPANS, SPANS, 16, 1500, 99, 0.05, START)
    b = paired_oos_comparison(ch, cp, SPANS, SPANS, 16, 1500, 99, 0.05, START)
    c = paired_oos_comparison(ch, cp, SPANS, SPANS, 16, 1500, 100, 0.05, START)
    assert a == b and a.d_pnl_quantile != c.d_pnl_quantile


def test_large_difference_detected() -> None:
    good, bad = book(noisy(5, mean=6.0)), book(noisy(6, mean=-1.0))
    assert paired_oos_comparison(good, bad, SPANS, SPANS, 16, 3000, 1234, 0.05, START).d_pnl_quantile > 0
    assert paired_oos_comparison(bad, good, SPANS, SPANS, 16, 3000, 1234, 0.05, START).d_pnl_quantile < 0
    similar = paired_oos_comparison(book(noisy(7)), book(noisy(8)), SPANS, SPANS, 16, 3000, 1234, 0.05, START)
    assert similar.d_pnl_quantile <= 0  # two similar models: no significant superiority


def test_block_length_from_label_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = LABEL.cfg
    expected = -(-(cfg.entry_first_bar + cfg.entry_timeout_min - 1 + cfg.horizon_min) // 1440)
    assert oos_block(cfg) == pd.Timedelta(days=expected)
    seen: list[int] = []
    real = oos_compare.paired_oos_comparison

    def spy(*args: Any, **kw: Any) -> Any:
        seen.append(args[4])
        return real(*args, **kw)

    monkeypatch.setattr(oos_compare, "paired_oos_comparison", spy)
    for name, days in (("ch", 12), ("cp", 20)):
        write_artifacts(tmp_path / name, book(noisy(9)), SPANS, days)
    out = compare_models(
        tmp_path / "ch", tmp_path / "cp", champion_go=True, reps=100, seed=1, q=0.05, starting_quote=START
    )
    assert out is not None and seen == [20]  # the longer of the two label_configs


def test_block_assignment_is_the_ge_one() -> None:
    """E2 and the paired comparison share ONE block definition; E2's numbers did not move when it was extracted."""
    n0 = pd.Timestamp("2025-01-01")
    rng = np.random.default_rng(5)
    ts = n0 + pd.to_timedelta(rng.integers(0, 200 * 24 * 60, 300), unit="m")
    vals = rng.normal(10, 80, 300)
    block = pd.Timedelta(days=16)
    end = n0 + pd.Timedelta(days=200)
    b1 = block_bootstrap(vals, ts.to_numpy(), n0, end, block, 3000, 1)
    assert astuple(b1)[:5] == pytest.approx(
        (11.601497336935731, 5.356053147410953, 1.3331552952764705, 21.821086086403707, 2.166053438536872)
    )  # values of the pre-refactor implementation
    # the same statistic rebuilt from the shared pieces
    grid = block_grid(n0, end, block)
    placed = BlockedValues.place(grid, ts, vals)
    cnt, tot = placed.window_sums(draw_block_starts(grid, 3000, np.random.default_rng(1)))
    ok = cnt > 0
    assert float(np.std((tot[ok] / cnt[ok]), ddof=1)) == pytest.approx(b1.se, rel=0.05)
    assert grid.n_blocks == 13 and grid.block_ns == int(block.value)


def test_oos_metrics_reuse_backtest_definitions(monkeypatch: pytest.MonkeyPatch) -> None:
    t = book([10.0, -4.0, 6.0, -12.0, 8.0], every=2.0)
    spans = [(day(0), day(12))]
    m = oos_metrics(t, spans, START)
    days = pd.date_range(day(0), day(11), freq="D")
    pnl = pd.Series(0.0, index=days)
    for ts, p in zip(t["exit_ts"], t["pnl_eur"], strict=True):
        pnl[ts.floor("D")] += p
    eq = pd.concat([pd.Series([START], index=[day(0) - pd.Timedelta(days=1)]), START + pnl.cumsum()])
    assert (m.sharpe, m.max_dd_frac) == pytest.approx(equity_sharpe_and_drawdown(eq))
    assert m.n_trades == 5 and m.pnl_eur == pytest.approx(8.0)
    assert m.max_dd_frac == pytest.approx(12.0 / (START + 12.0))  # 10, 6 -> peak 10006 -> 9994 after the -12 loss
    monkeypatch.setattr(oos_compare, "equity_sharpe_and_drawdown", lambda eq: (123.0, 0.321))
    assert oos_metrics(t, spans, START).sharpe == 123.0  # no second definition inside oos_compare
    assert oos_metrics(t, [], START) == oos_compare.OosMetrics(0, 0.0, 0.0, 0.0)


# ----------------------------------------------------------------------------- the artifacts


def span_set() -> SpanSet:
    def sp(a: int, b: int, fold: int) -> Span:
        return Span(day(a), day(b), None, fold)

    folds = [FoldSpans(i, sp(0, 1, i), sp(60 * i, 60 * i + 60, i)) for i in range(5)]
    return SpanSet(folds, sp(0, 1, -2), sp(300, 330, -1), day(300), pd.Timedelta(days=2))


def many_trades() -> pd.DataFrame:
    rows = []
    for f in range(5):
        for i in range(30):
            fill = day(60 * f + 1 + 1.5 * i)
            rows.append(
                {
                    "product": "A" if i % 2 else "B", "fold": f, "decision_ts": fill - pd.Timedelta(minutes=1),
                    "fill_ts": fill, "exit_ts": fill + pd.Timedelta(hours=40), "exit_reason": "TP",
                    "net_ret_bps": 30.0, "pnl_eur": 0.6, "notional_eur": 200.0, "label_net_bps": 30.0,
                }
            )  # fmt: skip
    return pd.DataFrame(rows)


def write_artifacts(
    folder: Path, trades: pd.DataFrame, spans: list[tuple[pd.Timestamp, pd.Timestamp]], block_days: int
) -> None:
    folder.mkdir(parents=True)
    rows = trades.assign(**{c: trades[c].map(lambda t: t.isoformat()) for c in ("decision_ts", "fill_ts", "exit_ts")})
    (folder / OOS_TRADES).write_text(
        "".join(json.dumps({k: r[k] for k in OOS_TRADE_FIELDS}) + "\n" for r in rows.to_dict("records")),
        encoding="utf-8",
    )
    listed = [{"fold": i, "start": s.isoformat(), "end": e.isoformat()} for i, (s, e) in enumerate(spans)]
    doc = {
        "v": "chronos.oos_spans.v1", "label_config_sha256": "abc", "rho": 0.5, "block_days": block_days,
        "spans": listed,
    }  # fmt: skip
    (folder / OOS_SPANS).write_text(json.dumps(doc), encoding="utf-8")


def test_evaluate_writes_oos_trades_and_spans(tmp_path: Path) -> None:
    trades = many_trades()
    ev = Evaluation(oos_trades=trades, oos_spans=oos_spans_doc(span_set(), LABEL, 0.5))
    folder = tmp_path / "model"
    folder.mkdir()
    write_oos_artifacts(folder, ev)
    lines = (folder / OOS_TRADES).read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(trades)
    first = json.loads(lines[0])
    assert list(first) == sorted(OOS_TRADE_FIELDS) and first["decision_ts"].endswith("+00:00")
    spans = json.loads((folder / OOS_SPANS).read_text(encoding="utf-8"))
    assert spans["label_config_sha256"] == LABEL.sha256 and spans["block_days"] == oos_block(LABEL.cfg).days
    assert [s["fold"] for s in spans["spans"]] == [0, 1, 2, 3, 4]
    assert spans["spans"][1] == {"fold": 1, "start": day(60).isoformat(), "end": day(120).isoformat()}
    art = load_oos_artifacts(folder)
    assert art is not None and len(art.trades) == len(trades) and art.block_days == spans["block_days"]
    assert art.spans[0] == (day(0), day(60)) and art.label_config_sha256 == LABEL.sha256
    assert art.trades["decision_ts"].dt.tz is not None
    # a model without trades writes empty-but-valid artifacts
    empty = tmp_path / "empty"
    empty.mkdir()
    write_oos_artifacts(empty, Evaluation(oos_spans=oos_spans_doc(span_set(), LABEL, 0.5)))
    assert (empty / OOS_TRADES).read_text(encoding="utf-8") == ""
    loaded = load_oos_artifacts(empty)
    assert loaded is not None and len(loaded.trades) == 0


def test_oos_trades_count_matches_ge_e1() -> None:
    trades = many_trades()
    t = np.arange(400, dtype=np.int64) * 10
    cands = {(f, p): Candidates(t, t + 5, np.full(400, -100.0)) for f in range(5) for p in ("A", "B")}
    out = _ge(trades, pd.DataFrame(), cands, LABEL, span_set(), True, 0.5)
    rows = oos_trade_rows(trades, 0.5)
    assert len(rows) == out["n_oos_trades"] == len(trades)
    assert sum(r["neff_weight"] for r in rows) == pytest.approx(out["n_eff_trades"])  # the E1 weights, per trade
    assert sum(r["neff_weight"] for r in rows) == pytest.approx(effective_trades(trades, 0.5))
    assert trade_weights(trades, 0.5).shape == (len(trades),) and trade_weights(trades.iloc[0:0], 0.5).size == 0


def test_missing_or_foreign_artifacts_are_none(tmp_path: Path) -> None:
    assert load_oos_artifacts(tmp_path) is None  # a version evaluated before they existed
    write_artifacts(tmp_path / "m", book([1.0, 2.0]), SPANS, 16)
    (tmp_path / "m" / OOS_SPANS).write_text(json.dumps({"v": "other"}), encoding="utf-8")
    assert load_oos_artifacts(tmp_path / "m") is None
    (tmp_path / "m" / OOS_SPANS).write_text("{not json", encoding="utf-8")
    assert load_oos_artifacts(tmp_path / "m") is None


def test_compare_models_champion_not_go_leaves_the_paired_part_empty(tmp_path: Path) -> None:
    write_artifacts(tmp_path / "ch", book(noisy(10, mean=3.0)), SPANS, 16)
    write_artifacts(tmp_path / "cp", book(noisy(11)), SPANS, 16)
    solo = compare_models(
        tmp_path / "ch", tmp_path / "cp", champion_go=False, reps=200, seed=1, q=0.05, starting_quote=START
    )
    assert solo is not None
    assert (solo.n_trades_cp, solo.d_pnl_quantile, solo.sharpe_cp, solo.max_dd_cp) == (None, None, None, None)
    assert solo.n_trades_ch == 90 and solo.sharpe_ch is not None and solo.common_days == pytest.approx(300.0)
    both = compare_models(
        tmp_path / "ch", tmp_path / "cp", champion_go=True, reps=200, seed=1, q=0.05, starting_quote=START
    )
    assert (
        both is not None and both.n_trades_cp == 90 and both.d_pnl_quantile is not None and both.max_dd_cp is not None
    )
    assert (
        compare_models(
            tmp_path / "none", tmp_path / "cp", champion_go=True, reps=10, seed=1, q=0.05, starting_quote=START
        )
        is None
    )
    # a champion without artifacts, or with no span in common, leaves the paired fields empty
    absent = compare_models(
        tmp_path / "ch", tmp_path / "none", champion_go=True, reps=10, seed=1, q=0.05, starting_quote=START
    )
    assert absent is not None and absent.d_pnl_quantile is None
    write_artifacts(tmp_path / "late", book([1.0]), [(day(400), day(500))], 16)
    apart = compare_models(
        tmp_path / "ch", tmp_path / "late", champion_go=True, reps=10, seed=1, q=0.05, starting_quote=START
    )
    assert apart is not None and apart.d_pnl_quantile is None
