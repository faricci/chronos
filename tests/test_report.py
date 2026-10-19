"""Daily dry-run report and the gates G-DRY-OPS / G-DRY-ECON (spec sez. 5.1.7, 5.2, 6.4.4)."""

from __future__ import annotations

import copy
import json
import math
import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from modules.config import DryRunConfig, GateEconConfig, GateOpsConfig
from modules.dryrun import report as report_module
from modules.dryrun.report import (
    DryReporter,
    JsonOpsSource,
    OpsDay,
    SqliteTradeSource,
    Trade,
    decision_parity,
    evaluate_gate_econ,
    evaluate_gate_ops,
    expected_calibration_error,
    max_drawdown_frac,
    parse_trade,
    profit_factor,
    reliability_bins,
)
from modules.dryrun.report import ReportPaths as Paths

D = Decimal
ECON = GateEconConfig()
OPS = GateOpsConfig()
DAY1 = date(2026, 10, 17)
ZERO = Decimal(0)


def trade(
    net: str, reason: str = "TP", book: str = "champion", at: datetime | None = None, product: str = "SOL-EUR"
) -> Trade:
    return Trade(book, product, reason, at or datetime(2026, 10, 17, 12, tzinfo=UTC), D(net), D("0.02"))


def crit(gate: dict[str, Any], name: str) -> dict[str, Any]:
    found: dict[str, Any] = next(c for c in gate["criteria"] if c["name"] == name)
    return found


# ------------------------------------------------------------------------------------------ trades


def test_parse_the_spec_virtual_trade() -> None:
    raw = {
        "trade_id": "x", "book": "champion", "product_id": "SOL-EUR",
        "entry": {"avg_price": "92.03", "size": "0.024", "fees": "0.0133", "at": "2026-10-17T14:10:22.517Z"},
        "exit": {"reason": "TP", "avg_price": "108.96", "fees": "0.0157", "at": "2026-10-22T14:25:40.310Z"},
        "pnl": {"gross_quote": "0.4063", "fees_quote": "0.0290", "net_quote": "0.3774", "net_bps": 1698,
                "r_multiple": 1.53},
        "held_min": 7215,
    }  # fmt: skip
    t = parse_trade(raw)
    assert (t.book, t.product, t.exit_reason, t.net_quote, t.fees_quote) == (
        "champion",
        "SOL-EUR",
        "TP",
        D("0.3774"),
        D("0.0290"),
    )
    assert (
        t.exit_at == datetime(2026, 10, 22, 14, 25, 40, 310000, tzinfo=UTC)
        and t.r_multiple == 1.53
        and t.held_min == 7215
    )


def test_parse_the_flat_form_and_reject_garbage() -> None:
    t = parse_trade(
        {"book": "b", "product_id": "p", "exit_reason": "SL", "exit_at": "2026-10-17T00:00:00Z", "net_quote": "-0.5"}
    )
    assert t.net_quote == D("-0.5") and t.exit_reason == "SL"
    with pytest.raises(KeyError):
        parse_trade({"book": "b"})
    with pytest.raises(ArithmeticError):
        parse_trade(
            {"book": "b", "product_id": "p", "exit_reason": "SL", "exit_at": "2026-10-17T00:00:00Z", "net_quote": "x"}
        )


def make_db(path: Path, rows: list[tuple[str, str | None]]) -> None:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE closed_trades (trade_id TEXT, book TEXT, payload TEXT)")
    con.executemany("INSERT INTO closed_trades VALUES (?,?,?)", [(f"t{i}", b, p) for i, (b, p) in enumerate(rows)])
    con.commit()
    con.close()


def test_sqlite_trade_source_reads_payloads_and_counts_bad_rows(tmp_path: Path) -> None:
    good = {"book": "champion", "product_id": "SOL-EUR", "exit": {"reason": "MANUAL", "at": "2026-10-17T10:00:00Z"},
            "pnl": {"net_quote": "1.5"}}  # fmt: skip
    make_db(
        tmp_path / "d.db",
        [("champion", json.dumps(good)), ("champion", "{not json"), ("champion", json.dumps({"x": 1}))],
    )
    src = SqliteTradeSource(tmp_path / "d.db")
    got = src.load()
    assert len(got) == 1 and got[0].exit_reason == "MANUAL" and src.bad_rows == 2


def test_sqlite_trade_source_without_the_table_or_the_file_is_empty(tmp_path: Path) -> None:
    sqlite3.connect(tmp_path / "e.db").close()
    assert SqliteTradeSource(tmp_path / "e.db").load() == []
    assert SqliteTradeSource(tmp_path / "none.db").load() == []


# ---------------------------------------------------------------------------------------- statistics


def test_profit_factor() -> None:
    assert profit_factor([D("3"), D("-1"), D("-1")]) == pytest.approx(1.5)
    assert profit_factor([D("3")]) == math.inf
    assert profit_factor([D("-1")]) == 0.0
    assert profit_factor([]) is None and profit_factor([D("0")]) is None


def test_max_drawdown() -> None:
    assert max_drawdown_frac([100, 110, 99, 105, 90]) == pytest.approx((110 - 90) / 110)
    assert max_drawdown_frac([100, 101, 102]) == 0.0 and max_drawdown_frac([]) == 0.0


def test_ece_and_reliability_bins() -> None:
    p = np.array([0.1, 0.1, 0.9, 0.9])
    y = np.array([0.0, 1.0, 1.0, 1.0])
    assert expected_calibration_error(p, y) == pytest.approx(0.5 * 0.4 + 0.5 * 0.1)
    assert expected_calibration_error(np.array([1.0]), np.array([1.0])) == 0.0  # p = 1 falls in the last bin
    assert math.isnan(expected_calibration_error(np.empty(0), np.empty(0)))
    bins = reliability_bins(p, y)
    assert [(round(b["p_mean"], 2), b["freq"], b["n"]) for b in bins] == [(0.1, 0.5, 2), (0.9, 1.0, 2)]


def test_decision_parity() -> None:
    live = {("c", "p", "t1"): "BUY", ("c", "p", "t2"): "HOLD", ("c", "p", "t3"): "HOLD"}
    replay = {("c", "p", "t1"): "BUY", ("c", "p", "t2"): "SELL", ("c", "p", "t4"): "HOLD"}
    out = decision_parity(live, replay)
    assert (out["compared"], out["matched"], out["decision_match"]) == (4, 1, 0.25)  # one-sided minutes mismatch
    out = decision_parity(live, replay, {("c", "p", "t2"), ("c", "p", "t3"), ("c", "p", "t4")})
    assert (out["compared"], out["matched"], out["decision_match"], out["excluded_manual"]) == (1, 1, 1.0, 3)
    assert decision_parity({}, {})["decision_match"] is None


# ------------------------------------------------------------------------------------------- G-DRY-ECON


def econ(trades: list[Trade], *, random_pnl: Decimal | None = ZERO, equity: list[float] | None = None,
         ece: float | None = 0.05) -> dict[str, Any]:  # fmt: skip
    return evaluate_gate_econ(trades, ECON, book="champion", starting_quote=D("100"), random_pnl=random_pnl,
                              equity=equity, ece=ece)  # fmt: skip


def winners_and_losers(n: int = 30, win: str = "1", lose: str = "-0.5") -> list[Trade]:
    """Alternating ones: 15 wins, 15 losses by default; PF = 2."""
    return [
        trade(win if i % 2 == 0 else lose, at=datetime(2026, 10, 1, tzinfo=UTC) + timedelta(hours=i)) for i in range(n)
    ]


def test_the_econ_gate_passes_when_every_criterion_holds() -> None:
    g = econ(winners_and_losers())
    assert g["status"] == "PASS" and all(c["status"] == "pass" for c in g["criteria"])
    assert g["trades"] == 30 and g["net_pnl_quote"] == D("7.5")


def test_the_econ_gate_is_in_progress_below_thirty_trades() -> None:
    g = econ(winners_and_losers(29))
    assert g["status"] == "IN_PROGRESS" and crit(g, "closed_trades")["status"] == "fail"


def test_manual_exits_leave_the_count_and_the_profit_factor_but_not_the_pnl_or_the_drawdown() -> None:
    base = winners_and_losers(30)
    manual = [trade("-30", "MANUAL", at=datetime(2026, 10, 20, tzinfo=UTC))]
    g = econ(base + manual)
    assert g["trades"] == 30 and g["manual_exits"] == 1  # not a 31st trade
    assert crit(g, "profit_factor")["value"] == pytest.approx(2.0)  # the -30 is not in the PF
    assert g["net_pnl_quote"] == D("-22.5")  # but it is in the PnL...
    assert crit(g, "net_pnl")["status"] == "fail" and g["status"] == "FAIL"
    assert crit(g, "max_drawdown")["value"] > 0.2  # ...and in the drawdown
    only_manual = econ(winners_and_losers(29) + [trade("1", "MANUAL")])
    assert only_manual["trades"] == 29 and only_manual["status"] == "IN_PROGRESS"


@pytest.mark.parametrize(
    ("win", "lose", "expected"),
    [("1.2", "-1", "pass"), ("1.19", "-1", "fail")],  # PF = win/lose with equal counts: >= 1.2 inclusive
)
def test_profit_factor_threshold_is_inclusive(win: str, lose: str, expected: str) -> None:
    assert crit(econ(winners_and_losers(30, win, lose)), "profit_factor")["status"] == expected


def test_net_pnl_must_be_strictly_positive() -> None:
    g = econ(winners_and_losers(30, "1", "-1"))
    assert crit(g, "net_pnl")["status"] == "fail"


def test_the_champion_must_beat_random_entry_strictly() -> None:
    t = winners_and_losers()
    assert crit(econ(t, random_pnl=D("7.5")), "beats_random_entry")["status"] == "fail"
    assert crit(econ(t, random_pnl=D("7.49")), "beats_random_entry")["status"] == "pass"
    assert crit(econ(t, random_pnl=None), "beats_random_entry")["status"] == "unmeasured"


def test_drawdown_limit_is_inclusive_and_equity_wins_over_trades() -> None:
    t = winners_and_losers()
    assert crit(econ(t, equity=[100, 105, 99.75]), "max_drawdown")["status"] == "pass"  # exactly 5%
    assert crit(econ(t, equity=[100, 105, 99.7]), "max_drawdown")["status"] == "fail"
    assert crit(econ(t, equity=[100, 105, 99.75]), "max_drawdown")["note"] == "equity"
    assert crit(econ(t), "max_drawdown")["note"] == "closed trades"


def test_ece_limit_is_inclusive_and_unmeasured_without_labels() -> None:
    t = winners_and_losers()
    assert crit(econ(t, ece=0.10), "edge_ece")["status"] == "pass"
    assert crit(econ(t, ece=0.1001), "edge_ece")["status"] == "fail"
    g = econ(t, ece=None)
    assert crit(g, "edge_ece")["status"] == "unmeasured" and g["status"] == "INCOMPLETE"


def test_a_book_with_no_winner_and_no_loser_has_an_undefined_profit_factor() -> None:
    g = econ([trade("0")] * 30)
    assert crit(g, "profit_factor")["status"] == "unmeasured"


def test_other_books_do_not_count() -> None:
    g = econ(winners_and_losers() + [trade("5", book="random_entry")] * 5)
    assert g["trades"] == 30


# -------------------------------------------------------------------------------------------- G-DRY-OPS


def good_day(day: str = "2026-10-17") -> dict[str, Any]:
    return {
        "date": day,
        "ops": {"uptime": 0.999, "dry_run_violations": 0, "crashes": 0, "kill_switch_bug_trips": 0},
        "parity": {"compared": 1000, "matched": 995, "max_feature_psi": 0.04},
        "bars": {"SOL-EUR": {"rest_revision_ratio": 0.001, "out_of_order": 0, "duplicates": 0}},
        "costs": {"maker_fill_rate": 0.67, "spread_ratio_vs_labels": {"SOL-EUR": 1.1}},
        "decisions_by_book": {"champion": {"BUY": 2, "skip_reasons": {"fee_floor": 0}}},
    }  # fmt: skip


def seven_days(mutate: Any = None) -> list[dict[str, Any]]:
    days = [good_day(f"2026-10-{11 + i}") for i in range(7)]
    if mutate:
        mutate(days)
    return days


def ops_gate(days: list[dict[str, Any]], expected: float | None = 2.0) -> dict[str, Any]:
    return evaluate_gate_ops(days, OPS, "champion", expected_buy_rate=expected)


def test_ops_gate_passes_on_seven_good_days() -> None:
    g = ops_gate(seven_days())
    assert g["status"] == "PASS", [c for c in g["criteria"] if c["status"] != "pass"]
    assert {c["name"] for c in g["criteria"]} >= {"uptime", "decision_parity", "feature_psi", "rest_revision_ratio"}


def test_ops_gate_is_in_progress_before_seven_days() -> None:
    assert ops_gate(seven_days()[:6])["status"] == "IN_PROGRESS"


def test_an_unmeasured_criterion_is_never_passed() -> None:
    days = seven_days(lambda ds: ds[3]["ops"].update(uptime=None))
    g = ops_gate(days)
    assert crit(g, "uptime")["status"] == "unmeasured" and g["status"] == "INCOMPLETE"
    days = seven_days(lambda ds: ds[0].update(parity={}))
    assert ops_gate(days)["status"] == "INCOMPLETE"


def test_a_failure_wins_over_an_unmeasured_criterion() -> None:
    def mutate(ds: list[dict[str, Any]]) -> None:
        ds[0]["ops"]["uptime"] = None
        ds[1]["ops"]["crashes"] = 1

    assert ops_gate(seven_days(mutate))["status"] == "FAIL"


@pytest.mark.parametrize(
    ("section", "key", "value", "name", "status"),
    [
        ("ops", "uptime", 0.99, "uptime", "pass"),  # mean exactly at the limit
        ("ops", "uptime", 0.98, "uptime", "fail"),
        ("ops", "dry_run_violations", 1, "zero_dry_run_violations", "fail"),
        ("ops", "crashes", 1, "zero_crashes", "fail"),
        ("ops", "kill_switch_bug_trips", 1, "no_kill_switch_for_bug", "fail"),
        ("parity", "max_feature_psi", 0.10, "feature_psi", "fail"),  # PSI < 0.10 is strict
        ("parity", "max_feature_psi", 0.0999, "feature_psi", "pass"),
    ],
)
def test_ops_thresholds(section: str, key: str, value: float, name: str, status: str) -> None:
    g = ops_gate(seven_days(lambda ds: [d[section].update({key: value}) for d in ds]))
    assert crit(g, name)["status"] == status


def test_decision_parity_is_pooled_over_minutes_and_inclusive() -> None:
    def at_limit(ds: list[dict[str, Any]]) -> None:
        for d in ds:
            d["parity"].update(compared=100, matched=99)

    assert crit(ops_gate(seven_days(at_limit)), "decision_parity")["status"] == "pass"

    def below(ds: list[dict[str, Any]]) -> None:
        for d in ds:
            d["parity"].update(compared=1000, matched=989)

    assert crit(ops_gate(seven_days(below)), "decision_parity")["status"] == "fail"


def test_rest_revision_ratio_limit_is_inclusive_per_product_and_day() -> None:
    def edge(ds: list[dict[str, Any]]) -> None:
        ds[2]["bars"]["SOL-EUR"]["rest_revision_ratio"] = 0.005

    def over(ds: list[dict[str, Any]]) -> None:
        ds[2]["bars"]["SOL-EUR"]["rest_revision_ratio"] = 0.0051

    assert crit(ops_gate(seven_days(edge)), "rest_revision_ratio")["status"] == "pass"
    assert crit(ops_gate(seven_days(over)), "rest_revision_ratio")["status"] == "fail"


def test_bars_out_of_order_or_duplicated_fail() -> None:
    days = seven_days(lambda ds: ds[4]["bars"]["SOL-EUR"].update(duplicates=1))
    assert crit(ops_gate(days), "bars_in_order_no_duplicates")["status"] == "fail"


def test_buy_frequency_within_half_of_the_backtest_and_the_vacuous_case() -> None:
    assert crit(ops_gate(seven_days(), expected=2.0), "buy_frequency")["status"] == "pass"  # 2 a day against 2
    assert crit(ops_gate(seven_days(), expected=4.0), "buy_frequency")["status"] == "pass"  # -50% is the limit
    assert crit(ops_gate(seven_days(), expected=4.1), "buy_frequency")["status"] == "fail"
    assert crit(ops_gate(seven_days(), expected=1.3), "buy_frequency")["status"] == "fail"  # +54%
    assert crit(ops_gate(seven_days(), expected=None), "buy_frequency")["status"] == "unmeasured"

    def no_buys(ds: list[dict[str, Any]]) -> None:
        for d in ds:
            d["decisions_by_book"]["champion"]["BUY"] = 0

    assert crit(ops_gate(seven_days(no_buys), expected=0.0), "buy_frequency")["status"] == "pass"
    assert crit(ops_gate(seven_days(no_buys), expected=1.0), "buy_frequency")["status"] == "fail"


def test_costs_criteria() -> None:
    low_fill = seven_days(lambda ds: ds[0]["costs"].update(maker_fill_rate=0.49))
    assert crit(ops_gate(low_fill), "maker_fill_rate")["status"] == "fail"
    edge_fill = seven_days(lambda ds: ds[0]["costs"].update(maker_fill_rate=0.5))
    assert crit(ops_gate(edge_fill), "maker_fill_rate")["status"] == "pass"
    wide = seven_days(lambda ds: ds[0]["costs"].update(spread_ratio_vs_labels={"SOL-EUR": 2.01}))
    assert crit(ops_gate(wide), "spread_vs_labels")["status"] == "fail"
    exact = seven_days(lambda ds: ds[0]["costs"].update(spread_ratio_vs_labels={"SOL-EUR": 2.0}))
    assert crit(ops_gate(exact), "spread_vs_labels")["status"] == "pass"


def test_fee_floor_share_must_be_strictly_below_eighty_percent() -> None:
    def share(buys: int, floor: int) -> list[dict[str, Any]]:
        return seven_days(
            lambda ds: ds[0]["decisions_by_book"]["champion"].update(BUY=buys, skip_reasons={"fee_floor": floor})
        )

    # the other 6 days have 2 BUYs and no fee_floor: totals are (buys + 12, floor)
    assert crit(ops_gate(share(8, 16)), "fee_floor_share")["status"] == "fail"  # 16 / 20 = 0.8
    assert crit(ops_gate(share(8, 15)), "fee_floor_share")["status"] == "pass"
    assert crit(ops_gate(share(8, 15)), "fee_floor_share")["value"] == pytest.approx(0.75)


def test_the_gate_reads_the_book_it_is_asked_about() -> None:
    days = seven_days()
    for d in days:
        d["decisions_by_book"]["challenger"] = {"BUY": 0, "skip_reasons": {}}
    assert crit(evaluate_gate_ops(days, OPS, "challenger", expected_buy_rate=2.0), "buy_frequency")["status"] == "fail"


# ------------------------------------------------------------------------------------- the reporter


class Trades:
    def __init__(self, trades: list[Trade]) -> None:
        self._t = trades

    def load(self) -> list[Trade]:
        return self._t


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def decision(
    book: str, product: str, minute: int, final: str = "HOLD", skip: str | None = None, **extra: Any
) -> dict[str, Any]:
    t = datetime(2026, 10, 17, 0, 0, tzinfo=UTC) + timedelta(minutes=minute)
    rec: dict[str, Any] = {
        "decision_id": f"{book}-{product}-{minute}", "ts_bar_close": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "book": book,
        "product": product, "policy": {"final": final, "policy_version": "v1"},
        "risk": {"outcome": "skip" if skip else "none", "reason": skip},
    }  # fmt: skip
    rec.update(extra)
    return rec


def build(tmp_path: Path, trades: list[Trade] | None = None, **kw: Any) -> DryReporter:
    paths = Paths(
        decisions_dir=tmp_path / "decisions", equity_dir=tmp_path / "equity", learner_dir=tmp_path / "learner",
        labels_dir=tmp_path / "labels", out_dir=tmp_path / "out", **kw,
    )  # fmt: skip
    return DryReporter(DryRunConfig(), paths, Trades(trades or []), JsonOpsSource(tmp_path / "ops"))


def test_the_daily_document_has_the_schema_of_the_spec(tmp_path: Path) -> None:
    rows = [decision("champion", "SOL-EUR", m) for m in range(10)]
    rows += [decision("champion", "SOL-EUR", 10, "BUY", "fee_floor"), decision("champion", "SOL-EUR", 11, "SELL")]
    rows += [decision("random_entry", "SOL-EUR", 0, "BUY")]
    write_jsonl(tmp_path / "decisions" / "2026-10-17.jsonl", rows)
    (tmp_path / "ops").mkdir()
    (tmp_path / "ops" / "2026-10-17.json").write_text(json.dumps({
        "uptime": 0.9993, "ws_reconnects": 2, "dry_run_violations": 0, "crashes": 0, "skipped_late": 3,
        "bars": {"SOL-EUR": {"rest": 1402, "flat": 36, "flat_unproven": 2, "lag_s_p50": 120.3, "lag_s_p99": 126.1}},
        "median_spread_bps": {"SOL-EUR": 3}, "maker_fill_rate": 0.67,
    }), encoding="utf-8")  # fmt: skip
    rep = build(tmp_path, [trade("0.31"), trade("-0.1", "SL"), trade("9", book="random_entry")])
    doc = rep.generate(DAY1, 1)["daily"][0]
    assert {
        "date",
        "mode",
        "books",
        "decisions",
        "labels",
        "parity",
        "bars",
        "costs",
        "ops",
        "gate_ops",
        "gate_econ",
    } <= set(doc)
    assert doc["decisions"]["total"] == 12 and doc["decisions"]["BUY"] == 1 and doc["decisions"]["SELL"] == 1
    assert doc["decisions"]["skip_reasons"] == {"fee_floor": 1} and doc["decisions"]["skipped_late"] == 3
    assert doc["decisions_by_book"]["random_entry"]["BUY"] == 1
    champ = doc["books"]["champion"]
    assert champ["trades_closed_today"] == 2 and champ["net_pnl_quote"] == "0.21"
    assert champ["profit_factor_total"] == pytest.approx(3.1) and champ["max_dd_source"] == "closed_trades"
    assert doc["books"]["random_entry"]["net_pnl_quote"] == "9"
    bars = doc["bars"]["SOL-EUR"]
    assert bars["lag_s_p99"] == 126.1 and bars["rest_revision_ratio"] is None  # nothing audited: null, not 0
    assert doc["ops"]["uptime"] == 0.9993 and doc["costs"]["maker_fill_rate"] == 0.67
    assert (tmp_path / "out" / "2026-10-17.json").exists() and (tmp_path / "out" / "2026-10-17.md").exists()


def test_without_any_input_the_numbers_are_null_not_zero(tmp_path: Path) -> None:
    doc = build(tmp_path).generate(DAY1, 1)["daily"][0]
    assert doc["ops"]["uptime"] is None and doc["ops"]["measured"] is False
    assert doc["decisions"]["skipped_late"] is None and doc["parity"]["decision_match"] is None
    assert doc["bars"] == {} and doc["books"]["champion"]["win_rate"] is None
    assert doc["gate_ops"]["status"] == "IN_PROGRESS" and doc["gate_econ"]["status"] == "IN_PROGRESS"


def test_the_rest_audit_and_the_costs_come_from_the_learner_of_the_next_night(tmp_path: Path) -> None:
    learner = {"steps": {
        "L0": {"details": {
            "rest_revisions": {"products": {"SOL-EUR": {"revised": 1, "ratio": 0.0007, "max_close_diff_bps": 1.8}}},
            "state_parity": {"max_feature_psi": 0.04, "worst_feature": "liquidity.vol_z_1d",
                             "state_parity": {"match": 1.0}, "drift_alerts": []},
        }},
        "L4": {"details": {"maker_fill_rate": {"rate": 0.8, "entries": 5, "filled": 4}, "latency_ms_p75": 280.0}},
    }}  # fmt: skip
    (tmp_path / "learner").mkdir()
    (tmp_path / "learner" / "2026-10-18.json").write_text(json.dumps(learner), encoding="utf-8")  # the NEXT day
    (tmp_path / "learner" / "2026-10-17.json").write_text(json.dumps({"steps": {}}), encoding="utf-8")  # not this one
    doc = build(tmp_path).generate(DAY1, 1)["daily"][0]
    assert doc["bars"]["SOL-EUR"]["rest_revision_ratio"] == 0.0007 and doc["bars"]["SOL-EUR"]["rest_revisions"] == 1
    assert doc["parity"]["max_feature_psi"] == 0.04 and doc["parity"]["worst_feature"] == "liquidity.vol_z_1d"
    assert doc["costs"]["maker_fill_rate"] == 0.8 and doc["costs"]["latency_ms_p75"] == 280.0


def test_equity_files_give_the_curve_and_the_open_positions(tmp_path: Path) -> None:
    write_jsonl(tmp_path / "equity" / "2026-10-17.jsonl", [
        {"book": "champion", "equity": "100.00", "open_positions": 0},
        {"book": "champion", "equity": "104.00", "open_positions": 1},
        {"book": "champion", "equity": "98.00", "open_positions": 2},
    ])  # fmt: skip
    b = build(tmp_path).generate(DAY1, 1)["daily"][0]["books"]["champion"]
    assert b["max_dd_source"] == "equity" and b["max_dd_frac"] == pytest.approx(6 / 104)
    assert b["open_positions"] == 2 and b["equity_start"] == "100.00" and b["equity_end"] == "98.00"


def test_manual_closes_are_counted_apart_and_cut_the_decision_parity(tmp_path: Path) -> None:
    live = [decision("champion", "SOL-EUR", m, "HOLD") for m in range(0, 600)]
    replay = [decision("champion", "SOL-EUR", m, "HOLD" if m < 300 else "BUY") for m in range(0, 600)]
    write_jsonl(tmp_path / "decisions" / "2026-10-17.jsonl", live)
    write_jsonl(tmp_path / "replay" / "2026-10-17.jsonl", replay)
    manual_at = datetime(2026, 10, 17, 5, 0, tzinfo=UTC)  # minute 300: everything from here to the end of the day
    rep = build(tmp_path, [trade("-1", "MANUAL", at=manual_at)], replay_dir=tmp_path / "replay")
    doc = rep.generate(DAY1, 1)["daily"][0]
    assert doc["parity"]["excluded_manual"] == 300 and doc["parity"]["compared"] == 300
    assert doc["parity"]["decision_match"] == 1.0
    assert doc["manual_exits"]["champion"] == 1
    other = build(tmp_path, [trade("-1", "MANUAL", at=manual_at, product="BTC-EUR")], replay_dir=tmp_path / "replay")
    assert other.generate(DAY1, 1)["daily"][0]["parity"]["decision_match"] == pytest.approx(0.5)  # other product


def test_the_report_sees_a_manual_exit_as_neutral_for_the_trade_count(tmp_path: Path) -> None:
    rep = build(tmp_path, [trade("1")] * 3 + [trade("1", "MANUAL")] * 2)
    book = rep.generate(DAY1, 1)["daily"][0]["books"]["champion"]
    assert book["trades_closed_total"] == 5 and book["manual_exits_total"] == 2 and book["win_rate"] == 1.0


def test_anomalies_are_the_breaches_of_the_ops_thresholds(tmp_path: Path) -> None:
    (tmp_path / "ops").mkdir()
    (tmp_path / "ops" / "2026-10-17.json").write_text(json.dumps(
        {"uptime": 0.9, "dry_run_violations": 0, "crashes": 1,
         "kill_switch_trips": [{"guard": "coverage_breach", "bug": True}, {"guard": "manual_pause", "bug": True}]}
    ), encoding="utf-8")  # fmt: skip
    doc = build(tmp_path).generate(DAY1, 1)["daily"][0]
    assert doc["ops"]["kill_switch_bug_trips"] == 1  # manual_pause is neither a bug nor downtime
    assert doc["anomalies"]["operational"] == 3 and set(doc["anomalies"]["notes"]) == {
        "crash",
        "kill_switch_bug",
        "uptime",
    }
    clean = build(tmp_path / "clean").generate(DAY1, 1)["daily"][0]["anomalies"]
    assert clean == {"operational": 0, "parity": 0, "notes": []}  # nothing measured is not an anomaly


def test_the_window_gate_and_the_calibration_use_the_champion_labels_only(tmp_path: Path) -> None:
    write_jsonl(
        tmp_path / "decisions" / "2026-10-17.jsonl",
        [decision("champion", "SOL-EUR", 1), decision("challenger", "SOL-EUR", 1)],
    )

    def label(did: str, edge: float, target: str) -> dict[str, Any]:
        return {
            "question_key": "edge",
            "split": "live",
            "decision_id": did,
            "predicted": {"edge": edge},
            "target": target,
        }

    write_jsonl(tmp_path / "labels" / "2026-10-17.jsonl", [
        label("champion-SOL-EUR-1", 0.9, "true"), label("challenger-SOL-EUR-1", 0.9, "false"),
        {**label("champion-SOL-EUR-1", 0.5, "true"), "question_key": "action"},
    ])  # fmt: skip
    result = build(tmp_path).generate(DAY1, 1)
    assert crit(result["gate_econ"], "edge_ece")["value"] == pytest.approx(0.1)  # |0.9 - 1| on one label
    assert result["reliability"] == [{"p_mean": 0.9, "freq": 1.0, "n": 1}]
    assert result["daily"][0]["labels"]["resolved_total"] == 2  # two edge labels, whatever the book


def test_the_window_covers_the_requested_days_and_totals_are_cumulative(tmp_path: Path) -> None:
    old = trade("2", at=datetime(2026, 10, 15, 10, tzinfo=UTC))
    rep = build(tmp_path, [old, trade("1")])
    docs = rep.generate(DAY1, 3)["daily"]
    assert [d["date"] for d in docs] == ["2026-10-15", "2026-10-16", "2026-10-17"]
    assert [d["books"]["champion"]["trades_closed_total"] for d in docs] == [1, 1, 2]
    assert [d["books"]["champion"]["trades_closed_today"] for d in docs] == [1, 0, 1]
    assert (tmp_path / "out" / "2026-10-15.md").exists()


def test_trades_after_the_day_do_not_leak_into_it(tmp_path: Path) -> None:
    future = trade("100", at=datetime(2026, 10, 18, 0, 0, tzinfo=UTC))
    doc = build(tmp_path, [future]).generate(DAY1, 1)["daily"][0]
    assert doc["books"]["champion"]["trades_closed_total"] == 0


def test_the_document_carries_the_tokens_the_tripwire_asks_for(tmp_path: Path) -> None:
    source = Path(report_module.__file__).read_text(encoding="utf-8")
    assert all(t in source for t in ("rest_revision_ratio", "skipped_late", "lag_s_p99"))
    doc = build(tmp_path).generate(DAY1, 1, write=False)["daily"][0]
    assert not (tmp_path / "out").exists()
    assert "skipped_late" in doc["decisions"] and "rest_revision_ratio" not in doc["bars"]


def test_the_markdown_lists_the_gates_and_the_skip_histogram(tmp_path: Path) -> None:
    write_jsonl(
        tmp_path / "decisions" / "2026-10-17.jsonl",
        [decision("champion", "SOL-EUR", m, "BUY", "ev_negative") for m in range(3)],
    )
    build(tmp_path).generate(DAY1, 1)
    md = (tmp_path / "out" / "2026-10-17.md").read_text(encoding="utf-8")
    assert "## G-DRY-OPS: IN_PROGRESS" in md and "## G-DRY-ECON: IN_PROGRESS" in md
    assert "- ev_negative: 3 " in md and "| champion |" in md


def test_the_json_never_contains_nan(tmp_path: Path) -> None:
    doc = json.loads((build(tmp_path).generate(DAY1, 1) and (tmp_path / "out" / "2026-10-17.json")).read_text("utf-8"))
    assert doc["v"] == "chronos.report.v1"


def test_ops_source_ignores_a_corrupt_file(tmp_path: Path) -> None:
    (tmp_path / "ops").mkdir()
    (tmp_path / "ops" / "2026-10-17.json").write_text("{nope", encoding="utf-8")
    assert JsonOpsSource(tmp_path / "ops").day(DAY1) is None
    assert OpsDay().uptime is None


def test_the_cli_rejects_bad_arguments(tmp_path: Path) -> None:
    assert report_module.main(["--days", "0"]) == 2
    assert report_module.main(["--config", str(tmp_path / "none.yml"), "--days", "1"]) == 2


def test_the_configured_thresholds_are_the_ones_used(tmp_path: Path) -> None:
    cfg = DryRunConfig(gate_ops=GateOpsConfig(min_uptime=0.5, min_days=1))
    rep = DryReporter(cfg, build(tmp_path).paths, Trades([]), JsonOpsSource(tmp_path / "ops"))
    days = copy.deepcopy(seven_days()[:1])
    days[0]["ops"]["uptime"] = 0.6
    assert crit(evaluate_gate_ops(days, rep.cfg.gate_ops, "champion"), "uptime")["status"] == "pass"


def test_uptime_is_the_mean_over_the_window_not_the_worst_day() -> None:
    days = seven_days(lambda ds: ds[0]["ops"].update(uptime=0.95))  # one bad day, six at 0.999: mean 0.992
    assert crit(ops_gate(days), "uptime")["status"] == "pass"
