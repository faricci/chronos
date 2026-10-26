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


def impact(**kw: Any) -> dict[str, Any]:
    """The worst ``ImpactDay`` of a book on a product and day, as ``bars.<p>.revision_impact.<book>`` carries it."""
    base = {"version": "m@1", "states": 1440, "share": 0.0, "max_shift": 0.004, "flip_share": 0.0, "r3": "measured"}
    return {**base, **kw}


def measured_drift(alarm: bool = False) -> dict[str, Any]:
    """The ``feature_drift`` block of L0 for one served product (PSI-v2 adopted), as the parity section carries it."""
    flagged = [{"feature": "atr_14_1d_bps", "psi": 1.92, "x_f": 0.61, "p": 0.0006}] if alarm else []
    return {
        "rule": "PSI_V2", "status": "measured", "reason": None, "alarm": alarm,
        "calibration": {"c": 0.0021, "window_days": 7, "alpha": 0.01},
        "products": {"SOL-EUR": {"status": "measured", "states": 168, "S": 0.0006 if alarm else 0.3, "alarm": alarm,
                                 "flagged": flagged}},
    }  # fmt: skip


def btc_context(**kw: Any) -> dict[str, Any]:
    """The ``btc_context`` block of L0: one alt product whose states of the day were all aligned."""
    product = {
        "alt_states": 1440, "misaligned": 0, "share": 0.0,
        "classes": {"live_missing": 0, "batch_missing": 0, "value_mismatch": 0, "flag_inconsistent": 0},
        "max_shift": 0.0, "flips": 0, "shift_status": "measured",
    }  # fmt: skip
    return {"products": {"SOL-EUR": {**product, **kw}}}


def good_day(day: str = "2026-10-17") -> dict[str, Any]:
    return {
        "date": day,
        "ops": {"uptime": 0.999, "dry_run_violations": 0, "crashes": 0, "kill_switch_bug_trips": 0},
        "parity": {
            "compared": 1000, "matched": 995, "max_feature_psi": 7.81, "worst_feature": "seq_1d_bps_5",
            "psi_v1": {"status": "diagnostic", "max_feature_psi": 7.81, "above_0_10": 37},
            "feature_drift": measured_drift(), "btc_context": btc_context(),
        },
        "bars": {"SOL-EUR": {
            "rest_revision_ratio": 0.001, "out_of_order": 0, "duplicates": 0,
            "rest_revisions_appeared": 0, "rest_revisions_disappeared": 0,
            "revision_impact": {"champion": impact()},
        }},
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
    names = {c["name"] for c in g["criteria"]}
    assert names >= {"uptime", "decision_parity", "feature_drift", "btc_context_alignment", "revision_shift_share"}
    assert "feature_psi" not in names  # the old daily PSI is a diagnostic, not a criterion


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


def test_gate_ops_ratio_is_diagnostic_not_criterion() -> None:
    """A ratio of 1.1% (the ETH of the P0 soak) with R1-R4 in order does not fail the gate: it is a diagnostic."""
    g = ops_gate(seven_days(lambda ds: ds[2]["bars"]["SOL-EUR"].update(rest_revision_ratio=0.011)))
    assert g["status"] == "PASS" and "rest_revision_ratio" not in {c["name"] for c in g["criteria"]}
    diag = next(d for d in g["diagnostics"] if d["name"] == "rest_revision_ratio")
    assert diag["value"] == 0.011
    assert diag["alarm_threshold"] == 0.005 and diag["alarm"] is True
    quiet = ops_gate(seven_days())["diagnostics"][0]
    assert quiet["alarm"] is False and quiet["value"] == 0.001
    edge = ops_gate(seven_days(lambda ds: ds[0]["bars"]["SOL-EUR"].update(rest_revision_ratio=0.005)))
    assert edge["diagnostics"][0]["alarm"] is False  # the alarm is strictly above the limit, as the old gate was


def set_impact(**kw: Any) -> Any:
    def mutate(ds: list[dict[str, Any]]) -> None:
        ds[2]["bars"]["SOL-EUR"]["revision_impact"]["champion"].update(kw)

    return mutate


def test_gate_ops_r1_r2_r3_r4_criteria() -> None:
    names = ("revision_shift_share", "revision_shift_max", "revision_decision_flips", "rest_completeness")
    ok = ops_gate(seven_days())
    assert [crit(ok, n)["status"] for n in names] == ["pass"] * 4
    # R1: the share 0.005 passes, above it fails
    assert crit(ops_gate(seven_days(set_impact(share=0.005))), names[0])["status"] == "pass"
    over = ops_gate(seven_days(set_impact(share=0.0051)))
    assert crit(over, names[0])["status"] == "fail" and over["status"] == "FAIL"
    # R2: 0.02 passes, above fails
    assert crit(ops_gate(seven_days(set_impact(max_shift=0.02))), names[1])["status"] == "pass"
    assert crit(ops_gate(seven_days(set_impact(max_shift=0.0201))), names[1])["status"] == "fail"
    # R3: 0.001 passes, above fails
    assert crit(ops_gate(seven_days(set_impact(flip_share=0.001))), names[2])["status"] == "pass"
    assert crit(ops_gate(seven_days(set_impact(flip_share=0.0011))), names[2])["status"] == "fail"

    # the criteria are those of the BOOK under evaluation: another book's evidence does not count
    def with_challenger(ds: list[dict[str, Any]]) -> None:
        for d in ds:
            d["bars"]["SOL-EUR"]["revision_impact"]["challenger"] = impact(share=0.5)

    other = seven_days(with_challenger)
    assert crit(ops_gate(other), names[0])["status"] == "pass"
    gate = evaluate_gate_ops(other, OPS, "challenger", expected_buy_rate=2.0)
    assert crit(gate, names[0])["status"] == "fail"
    no_data = evaluate_gate_ops(seven_days(), OPS, "random_entry", expected_buy_rate=2.0)
    assert crit(no_data, names[0])["status"] == "unmeasured" and crit(no_data, names[3])["status"] == "pass"


def test_gate_ops_r1_reads_block_share() -> None:
    """R1 reads the share of the BLOCK of 1440 states, not the share of the calendar day."""
    block_ok = ops_gate(seven_days(set_impact(share=0.004, day_share=0.012, block_kind="extended_back")))
    assert crit(block_ok, "revision_shift_share")["status"] == "pass" and block_ok["status"] == "PASS"
    block_bad = ops_gate(seven_days(set_impact(share=0.006, day_share=0.001, block_kind="extended_back")))
    assert crit(block_bad, "revision_shift_share")["status"] == "fail" and block_bad["status"] == "FAIL"
    assert "1440" in crit(block_bad, "revision_shift_share")["note"]


def old_learner_l0(states: int, share: float) -> dict[str, Any]:
    """A learner document written before the blocks existed: ``books`` only, no ``blocks`` key."""
    entry = impact_day_doc("m@1", states=states, share=share, day="2026-10-17", max_shift=0.004)
    return {"revision_impact": {"products": {"SOL-EUR": {"books": {"champion": [entry]}}}}}


def test_gate_ops_old_learner_doc_full_day_same_share(tmp_path: Path) -> None:
    doc = report_with_l0(tmp_path, old_learner_l0(1440, 0.003))
    imp = doc["bars"]["SOL-EUR"]["revision_impact"]["champion"]
    assert imp["share"] == 0.003 and imp["block_kind"] == "day" and imp["block_states"] == 1440  # a full day IS a block
    assert imp["day_share"] == 0.003


def test_gate_ops_old_learner_doc_partial_day_r1_unmeasured(tmp_path: Path) -> None:
    """A partial day of an old document is never judged with the old per-day rule: R1 is unmeasured, R2 is not."""
    doc = report_with_l0(tmp_path, old_learner_l0(343, 0.0058))
    imp = doc["bars"]["SOL-EUR"]["revision_impact"]["champion"]
    assert imp["share"] is None and imp["day_share"] == 0.0058 and imp["max_shift"] == 0.004
    def partial(ds: list[dict[str, Any]]) -> None:
        for d in ds:
            d["bars"]["SOL-EUR"]["revision_impact"] = {"champion": imp}

    g = ops_gate(seven_days(partial))
    assert crit(g, "revision_shift_share")["status"] == "unmeasured" and g["status"] == "INCOMPLETE"
    assert crit(g, "revision_shift_max")["status"] == "pass"
    r3 = crit(g, "revision_decision_flips")
    assert r3["status"] == "unmeasured" and "block_unavailable" in r3["note"]
    # one unmeasured day among measured ones is enough: R1 is unmeasured, never the maximum of the others
    def mixed(ds: list[dict[str, Any]]) -> None:
        ds[3]["bars"]["SOL-EUR"]["revision_impact"] = {"champion": imp}

    one = ops_gate(seven_days(mixed))
    r1 = crit(one, "revision_shift_share")
    assert r1["status"] == "unmeasured" and r1["value"] == 0.0
    # a document WITH blocks but none for this version/day (series shorter than a block): the same
    new = old_learner_l0(343, 0.0058)
    new["revision_impact"]["products"]["SOL-EUR"]["blocks"] = {"champion": []}
    assert report_with_l0(tmp_path / "new", new)["bars"]["SOL-EUR"]["revision_impact"]["champion"]["share"] is None


def test_gate_ops_new_diagnostics_not_criteria() -> None:
    def contiguous(ds: list[dict[str, Any]]) -> None:
        start = datetime(2026, 10, 11, tzinfo=UTC)
        for i, d in enumerate(ds):
            first = start + timedelta(days=i)
            imp = d["bars"]["SOL-EUR"]["revision_impact"]["champion"]
            z = "%Y-%m-%dT%H:%M:%SZ"
            imp.update(first_at=first.strftime(z), last_at=(first + timedelta(minutes=1439)).strftime(z),
                       revisions=1 + i % 2, r2_utilization=0.1 * (i + 1), shifted_at=[])  # fmt: skip
        # three shifted states close to midnight of day 3 / day 4: one window of 1440 states holds them all
        day3 = ds[3]["bars"]["SOL-EUR"]["revision_impact"]["champion"]
        day3["shifted_at"] = ["2026-10-14T23:50:00Z", "2026-10-14T23:51:00Z"]
        ds[4]["bars"]["SOL-EUR"]["revision_impact"]["champion"]["shifted_at"] = ["2026-10-15T00:05:00Z"]

    g = ops_gate(seven_days(contiguous))
    names = {c["name"] for c in g["criteria"]}
    by_name = {d["name"]: d for d in g["diagnostics"]}
    assert {"revision_shift_rolling_max", "revision_independent_revisions", "revision_r2_utilization"} <= set(by_name)
    assert not names & set(by_name)  # diagnostics are not criteria
    assert by_name["revision_shift_rolling_max"]["value"] == pytest.approx(3 / 1440)
    assert by_name["revision_independent_revisions"]["value"] == 2
    assert by_name["revision_r2_utilization"]["value"] == pytest.approx(0.7)
    with_threshold = ("rest_revision_ratio", "feature_psi_daily_v1", "feature_drift_scope")
    thresholdless = [n for n in by_name if n not in with_threshold]
    assert all("senza soglia" in by_name[n]["note"] for n in thresholdless)
    assert g["status"] == "PASS"  # whatever they say, they do not move the status
    bare = ops_gate(seven_days())  # no instants in the days: the diagnostics exist and are empty, not an error
    assert next(d for d in bare["diagnostics"] if d["name"] == "revision_shift_rolling_max")["value"] is None


def test_anomalies_ignore_unmeasured_share(tmp_path: Path) -> None:
    doc = report_with_l0(tmp_path, old_learner_l0(343, 0.5))  # share unmeasured (None): not an R1 anomaly
    assert doc["anomalies"]["revision_by_version"] == {} and doc["anomalies"]["operational"] == 0
    full = report_with_l0(tmp_path / "full", old_learner_l0(1440, 0.5))
    assert full["anomalies"]["revision_by_version"] == {"m@1": ["R1:SOL-EUR"]}  # a measured one still is


def test_gate_ops_r3_unmeasured_gives_incomplete() -> None:
    """A champion with an untunable policy has no flips to count: the gate cannot be better than INCOMPLETE."""

    def untunable(ds: list[dict[str, Any]]) -> None:
        for d in ds:
            d["bars"]["SOL-EUR"]["revision_impact"]["champion"].update(r3="policy_untunable", flip_share=None)

    g = ops_gate(seven_days(untunable))
    c = crit(g, "revision_decision_flips")
    assert c["status"] == "unmeasured" and "policy_untunable" in c["note"] and g["status"] == "INCOMPLETE"
    assert crit(g, "revision_shift_share")["status"] == "pass"  # R1 and R2 are measured all the same
    gone = ops_gate(seven_days(lambda ds: ds[3]["bars"]["SOL-EUR"].pop("revision_impact")))  # a day without the audit
    assert crit(gone, "revision_shift_share")["status"] == "unmeasured" and gone["status"] == "INCOMPLETE"


def test_gate_ops_r4_appeared_fails() -> None:
    appeared = ops_gate(seven_days(lambda ds: ds[5]["bars"]["SOL-EUR"].update(rest_revisions_appeared=1)))
    assert crit(appeared, "rest_completeness")["status"] == "fail" and appeared["status"] == "FAIL"
    gone = ops_gate(seven_days(lambda ds: ds[5]["bars"]["SOL-EUR"].update(rest_revisions_disappeared=2)))
    assert crit(gone, "rest_completeness")["value"] == 2 and gone["status"] == "FAIL"
    none = ops_gate(seven_days(lambda ds: ds[5]["bars"]["SOL-EUR"].update(rest_revisions_appeared=None)))
    assert crit(none, "rest_completeness")["status"] == "unmeasured"


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
    (tmp_path / "learner").mkdir(parents=True)
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
    assert clean == {"operational": 0, "parity": 0, "notes": [], "revision_by_version": {}}  # nothing measured: none


def impact_day_doc(version: str, **kw: Any) -> dict[str, Any]:
    return {"version": version, "states": 1440, "share": 0.0, "max_shift": 0.004, "flip_share": 0.0, "r3": "measured",
            **kw}  # fmt: skip


def report_with_l0(tmp_path: Path, l0: dict[str, Any]) -> dict[str, Any]:
    (tmp_path / "learner").mkdir(parents=True)
    learner = {"steps": {"L0": {"details": l0}}}
    (tmp_path / "learner" / "2026-10-18.json").write_text(json.dumps(learner), encoding="utf-8")
    daily: dict[str, Any] = build(tmp_path).generate(DAY1, 1)["daily"][0]
    return daily


def test_anomalies_ratio_goes_to_alarms(tmp_path: Path) -> None:
    """The raw ratio (ETH of the soak: 1.09%) rings an alarm and is NOT an anomaly any more; R4 is operational."""
    l0 = {"rest_revisions": {"products": {
        "ETH-EUR": {"revised": 26, "ratio": 0.0109, "max_close_diff_bps": 16.3, "value_revised": 26, "appeared": 0,
                    "disappeared": 0},
        "BTC-EUR": {"revised": 1, "ratio": 0.0004, "max_close_diff_bps": 1.0, "value_revised": 1, "appeared": 1,
                    "disappeared": 0},
    }}}  # fmt: skip
    doc = report_with_l0(tmp_path, l0)
    assert doc["alarms"] == ["rest_revision_ratio:ETH-EUR"]
    assert doc["anomalies"]["operational"] == 1 and doc["anomalies"]["notes"] == ["rest_completeness:BTC-EUR"]
    assert doc["bars"]["ETH-EUR"]["rest_revisions_value"] == 26
    assert doc["bars"]["BTC-EUR"]["rest_revisions_appeared"] == 1
    md = (tmp_path / "out" / "2026-10-17.md").read_text(encoding="utf-8")
    assert "ALARM (diagnostic)" in md


def test_revision_impact_is_read_per_book_and_per_version(tmp_path: Path) -> None:
    champion = [impact_day_doc("m@1", share=0.001, max_shift=0.012), impact_day_doc("m@0", share=0.02, max_shift=0.03)]
    challenger = [impact_day_doc("m@2", flip_share=0.01)]
    l0 = {"revision_impact": {"products": {"SOL-EUR": {"books": {"champion": champion, "challenger": challenger}}}}}
    doc = report_with_l0(tmp_path, l0)
    b = doc["bars"]["SOL-EUR"]
    assert b["revision_impact"]["champion"]["version"] == "m@0"  # two versions in the day: the worst one
    assert b["revision_impact"]["challenger"]["flip_share"] == 0.01
    assert set(b["revision_impact_by_version"]) == {"m@0", "m@1", "m@2"}
    # R1-R3 are charged to the version that failed them, never to the others
    assert doc["anomalies"]["revision_by_version"] == {"m@0": ["R1:SOL-EUR", "R2:SOL-EUR"], "m@2": ["R3:SOL-EUR"]}
    assert doc["anomalies"]["operational"] == 0


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


# ----------------------------------------------------------------------- conflict P1f: feature drift, BTC context


G4_PREFIX = (
    "feature_drift: unmeasured. Il PSI giornaliero contro gli ultimi 90 giorni misura in quanti decili cade un giorno "
    "di feature persistenti (4 prodotti x 1 valore, tetto ~8,3), non la deriva rispetto al training. Criterio "
    "sospeso (CONFLITTO P1f-bis) fino alla regola PSI-v3 calibrata (SIM-P1f-bis, D1/D1h/D2/D3). "
)


def without_drift(reason: str | None = "no_calibration") -> Any:
    def mutate(ds: list[dict[str, Any]]) -> None:
        for d in ds:
            d["parity"]["feature_drift"] = None if reason is None else {"status": "unmeasured", "reason": reason}

    return mutate


def test_old_psi_never_fails_the_gate() -> None:
    """Days with a daily PSI v1 of 7.81 (37 features above 0.10): no criterion fails on it, ``feature_drift`` is
    ``unmeasured`` with the exact text of the decision, and the gate is INCOMPLETE, never FAIL, because of it."""
    days = seven_days(without_drift("no_calibration"))
    days[3]["parity"].update(max_feature_psi=9.0, worst_feature="clock_dow")
    g = ops_gate(days)
    assert "feature_psi" not in {c["name"] for c in g["criteria"]}
    fd = crit(g, "feature_drift")
    assert fd["status"] == "unmeasured" and g["status"] == "INCOMPLETE"
    assert not [c for c in g["criteria"] if c["status"] == "fail"]
    assert fd["note"] == (
        G4_PREFIX + "Diagnostica: max PSI = 9.00 (clock_dow), 37 feature sopra 0,10. Motivo: no_calibration."
    )
    no_data = ops_gate(seven_days(lambda ds: [d.update(parity={"decision_match": 1.0}) for d in ds]))
    assert crit(no_data, "feature_drift")["note"] == (
        G4_PREFIX + "Diagnostica: max PSI = n/a (n/a), n/a feature sopra 0,10. Motivo: no_learner_document."
    )


def test_old_psi_is_not_a_parity_anomaly(tmp_path: Path) -> None:
    learner = {"steps": {"L0": {"details": {"state_parity": {
        "state_parity": {"match": 0.9979}, "psi_v1": {
            "status": "diagnostic", "max_feature_psi": 7.81, "worst_feature": "seq_1d_bps_5", "above_0_10": 37,
            "alerts": [{"feature": "seq_1d_bps_5", "psi": 7.81}]},
        "max_feature_psi_unused": None,
    }}}}}  # fmt: skip
    (tmp_path / "learner").mkdir(parents=True)
    (tmp_path / "learner" / "2026-10-18.json").write_text(json.dumps(learner), encoding="utf-8")
    doc = build(tmp_path).generate(DAY1, 1)["daily"][0]
    assert doc["parity"]["max_feature_psi"] == 7.81 and doc["anomalies"]["parity"] == 0
    assert "feature_psi" not in doc["anomalies"]["notes"]


def test_feature_drift_pass_fail_from_last_day() -> None:
    # six days in alarm do not count: the window of the rule ends with the last day
    def last_day_quiet(ds: list[dict[str, Any]]) -> None:
        for d in ds[:-1]:
            d["parity"]["feature_drift"] = measured_drift(alarm=True)

    quiet = ops_gate(seven_days(last_day_quiet))
    fd = crit(quiet, "feature_drift")
    assert fd["status"] == "pass" and fd["value"] == {"SOL-EUR": 0.3} and fd["threshold"] == 0.0021
    assert quiet["status"] == "PASS", [c for c in quiet["criteria"] if c["status"] != "pass"]

    def last_day_alarm(ds: list[dict[str, Any]]) -> None:
        ds[-1]["parity"]["feature_drift"] = measured_drift(alarm=True)

    loud = ops_gate(seven_days(last_day_alarm))
    fail = crit(loud, "feature_drift")
    assert fail["status"] == "fail" and loud["status"] == "FAIL"
    assert fail["note"] == "SOL-EUR atr_14_1d_bps psi=1.92 x_f=0.61 p=0.0006"

    def one_product_unmeasured(ds: list[dict[str, Any]]) -> None:
        block = measured_drift()
        block["products"]["XRP-EUR"] = {"status": "unmeasured", "reason": "incomplete_window"}
        ds[-1]["parity"]["feature_drift"] = block

    part = ops_gate(seven_days(one_product_unmeasured))
    assert crit(part, "feature_drift")["status"] == "unmeasured" and part["status"] == "INCOMPLETE"
    assert crit(part, "feature_drift")["note"].endswith("Motivo: incomplete_window.")


def test_feature_drift_alarm_goes_to_alarms_not_anomalies(tmp_path: Path) -> None:
    learner = {"steps": {"L0": {"details": {
        "state_parity": {"state_parity": {"match": 1.0}, "psi_v1": {"max_feature_psi": 7.81, "worst_feature": "x"}},
        "feature_drift": measured_drift(alarm=True),
    }}}}  # fmt: skip
    (tmp_path / "learner").mkdir(parents=True)
    (tmp_path / "learner" / "2026-10-18.json").write_text(json.dumps(learner), encoding="utf-8")
    doc = build(tmp_path).generate(DAY1, 1)["daily"][0]
    assert "feature_drift:SOL-EUR" in doc["alarms"]
    assert doc["anomalies"]["parity"] == 0 and doc["anomalies"]["operational"] == 0
    assert doc["parity"]["feature_drift"]["alarm"] is True
    skipped = {"steps": {"L0": {"details": {"state_parity": {"state_parity": {"match": 1.0}},
                                            "feature_drift": {"skipped": "no_calibration"}}}}}  # fmt: skip
    (tmp_path / "learner" / "2026-10-18.json").write_text(json.dumps(skipped), encoding="utf-8")
    again = build(tmp_path).generate(DAY1, 1)["daily"][0]
    assert again["parity"]["feature_drift"] == {"status": "unmeasured", "reason": "no_calibration"}
    assert not [a for a in again["alarms"] if a.startswith("feature_drift")]


def test_feature_psi_daily_v1_diagnostic() -> None:
    days = seven_days()
    days[2]["parity"].update(max_feature_psi=9.5, worst_feature="clock_dow")
    days[2]["parity"]["psi_v1"] = {"status": "diagnostic", "max_feature_psi": 9.5, "above_0_10": 41}
    g = ops_gate(days)
    diag = next(d for d in g["diagnostics"] if d["name"] == "feature_psi_daily_v1")
    assert diag["value"] == 9.5 and diag["alarm"] is True and diag["alarm_threshold"] == 0.10
    assert diag["detail"] == {"worst_feature": "clock_dow", "above_0_10": 41, "day": "2026-10-13"}
    assert "feature_psi_daily_v1" not in {c["name"] for c in g["criteria"]}
    assert g["status"] == "PASS"  # a diagnostic alarm does not move the gate
    quiet = ops_gate(seven_days(lambda ds: [d["parity"].update(max_feature_psi=0.05) for d in ds]))
    assert next(d for d in quiet["diagnostics"] if d["name"] == "feature_psi_daily_v1")["alarm"] is False
    none = ops_gate(seven_days(lambda ds: [d.update(parity={}) for d in ds]))
    assert next(d for d in none["diagnostics"] if d["name"] == "feature_psi_daily_v1")["value"] is None


def test_old_learner_document_still_read(tmp_path: Path) -> None:
    """A learner document written before P1f carries the PSI keys at the top level of ``state_parity``."""
    learner = {"steps": {"L0": {"details": {"state_parity": {
        "state_parity": {"match": 0.9982}, "unmatched_rows": 0, "psi": {"a": 7.8, "b": 0.05, "c": 0.4, "d": None},
        "max_feature_psi": 7.8, "worst_feature": "a", "drift_alerts": [{"feature": "a", "psi": 7.8}],
    }}}}}  # fmt: skip
    (tmp_path / "learner").mkdir(parents=True)
    (tmp_path / "learner" / "2026-10-18.json").write_text(json.dumps(learner), encoding="utf-8")
    par = build(tmp_path).generate(DAY1, 1)["daily"][0]["parity"]
    assert par["max_feature_psi"] == 7.8 and par["worst_feature"] == "a"
    assert par["psi_v1"]["above_0_10"] == 2 and par["psi_v1"]["alerts"] == [{"feature": "a", "psi": 7.8}]
    assert par["state_match"] == 0.9982 and par["state_match_all"] == 0.9982
    assert par["feature_drift"] is None and par["btc_context"] is None and "drift_alerts" not in par


def test_decision_parity_excludes_btc_stale_minutes() -> None:
    k = ("champion", "ETH-EUR")
    live = {(*k, f"t{i}"): "HOLD" for i in range(6)}
    live[(*k, "t2")] = "HOLD"  # the live engine blocked the BUY: btc_context_stale
    replay = {key: "HOLD" for key in live} | {(*k, "t2"): "BUY", (*k, "t4"): "BUY"}
    out = decision_parity(live, replay, set(), {(*k, "t2")})
    assert out["excluded_btc_stale"] == 1 and out["excluded_manual"] == 0
    assert out["compared"] == 5 and out["matched"] == 4  # t4 is a real disagreement
    both = decision_parity(live, replay, {(*k, "t2")}, {(*k, "t2"), (*k, "t3")})  # counted ONCE, as manual
    assert both["excluded_manual"] == 1 and both["excluded_btc_stale"] == 1 and both["compared"] == 4


def test_the_report_counts_the_stale_minutes_apart_from_the_manual_ones(tmp_path: Path) -> None:
    live = [decision("champion", "ETH-EUR", m, "HOLD", btc_context="aligned") for m in range(0, 100)]
    live[10] = decision("champion", "ETH-EUR", 10, "HOLD", btc_context="stale")
    replay = [decision("champion", "ETH-EUR", m, "BUY" if m == 10 else "HOLD") for m in range(0, 100)]
    write_jsonl(tmp_path / "decisions" / "2026-10-17.jsonl", live)
    write_jsonl(tmp_path / "replay" / "2026-10-17.jsonl", replay)
    manual_at = datetime(2026, 10, 17, 1, 30, tzinfo=UTC)  # minute 90 up to the end of the day
    rep = build(tmp_path, [trade("-1", "MANUAL", at=manual_at, product="ETH-EUR")], replay_dir=tmp_path / "replay")
    par = rep.generate(DAY1, 1)["daily"][0]["parity"]
    assert par["excluded_btc_stale"] == 1 and par["excluded_manual"] == 10
    assert par["compared"] == 89 and par["decision_match"] == 1.0


def set_btc(**kw: Any) -> Any:
    def mutate(ds: list[dict[str, Any]]) -> None:
        ds[2]["parity"]["btc_context"] = btc_context(**kw)

    return mutate


def test_btc_context_alignment_pass_fail_unmeasured() -> None:
    name = "btc_context_alignment"
    ok = ops_gate(seven_days())
    assert crit(ok, name)["status"] == "pass" and crit(ok, name)["value"] == 0.0
    assert crit(ok, name)["threshold"] == {"share": 0.005, "shift": 0.01, "flips": 0}
    assert crit(ops_gate(seven_days(set_btc(share=0.005, misaligned=7))), name)["status"] == "pass"
    over = ops_gate(seven_days(set_btc(share=0.006, misaligned=9)))
    assert crit(over, name)["status"] == "fail" and over["status"] == "FAIL"
    assert crit(ops_gate(seven_days(set_btc(max_shift=0.01))), name)["status"] == "pass"
    assert crit(ops_gate(seven_days(set_btc(max_shift=0.011))), name)["status"] == "fail"
    assert crit(ops_gate(seven_days(set_btc(flips=1))), name)["status"] == "fail"
    bug = ops_gate(seven_days(set_btc(classes={"live_missing": 0, "batch_missing": 0, "value_mismatch": 1,
                                               "flag_inconsistent": 0})))  # fmt: skip
    assert crit(bug, name)["status"] == "fail" and "value_mismatch" in crit(bug, name)["note"]
    flag = ops_gate(seven_days(set_btc(classes={"flag_inconsistent": 2})))
    assert crit(flag, name)["status"] == "fail"
    # a day with no block, a shift that cannot be measured, flips unknown: unmeasured, never a pass
    missing = ops_gate(seven_days(lambda ds: ds[4]["parity"].update(btc_context=None)))
    assert crit(missing, name)["status"] == "unmeasured" and missing["status"] == "INCOMPLETE"
    assert crit(ops_gate(seven_days(set_btc(shift_status="no_deciders", max_shift=None, flips=None))), name)[
        "status"
    ] == "unmeasured"
    untunable = ops_gate(seven_days(set_btc(shift_status="policy_untunable", flips=None)))
    assert crit(untunable, name)["status"] == "unmeasured"
    # failure wins over unmeasured
    def both(ds: list[dict[str, Any]]) -> None:
        ds[4]["parity"]["btc_context"] = None
        ds[2]["parity"]["btc_context"] = btc_context(flips=2)

    assert crit(ops_gate(seven_days(both)), name)["status"] == "fail"


def test_markdown_shows_the_diagnostic_and_the_new_parity_lines(tmp_path: Path) -> None:
    learner = {"steps": {"L0": {"details": {
        "state_parity": {"state_parity": {"match": 0.9998, "match_all": 0.9979}, "unmatched_rows": 0,
                         "psi_v1": {"max_feature_psi": 7.81, "worst_feature": "seq_1d_bps_5", "above_0_10": 37},
                         "btc_context": btc_context(share=0.0028, misaligned=4)},
        "feature_drift": {"skipped": "no_calibration"},
    }}}}  # fmt: skip
    (tmp_path / "learner").mkdir(parents=True)
    (tmp_path / "learner" / "2026-10-18.json").write_text(json.dumps(learner), encoding="utf-8")
    build(tmp_path).generate(DAY1, 1)
    text = (tmp_path / "out" / "2026-10-17.md").read_text(encoding="utf-8")
    assert "max_feature_psi 7.81 (seq_1d_bps_5) - diagnostica v1, non e' un criterio" in text
    assert "feature_drift unmeasured (no_calibration)" in text
    assert "btc_context worst share 0.0028 (SOL-EUR, misaligned 4)" in text
    assert "excluded_btc_stale 0" in text and "(all rows 0.9979)" in text
    assert "feature_psi_daily_v1" in text


def untunable_context(**kw: Any) -> Any:
    """A day of a champion whose policy is explicitly untunable: flips 0 with their basis."""
    base: dict[str, Any] = {
        "shift_status": "policy_untunable", "flips": 0, "flips_basis": "policy_untunable", "max_shift": 0.0035,
        "misaligned": 4, "share": 0.0028,
        "untunable_check": {"versions": {"m@1": {"policy": "untunable", "buy_records": 0, "intent_records": 0}}},
    }  # fmt: skip
    return set_btc(**{**base, **kw})


def test_btc_alignment_passes_with_policy_untunable_basis() -> None:
    g = ops_gate(seven_days(untunable_context()))
    c = crit(g, "btc_context_alignment")
    assert c["status"] == "pass" and c["note"] == "flips_basis: policy_untunable"
    # the same document with the flips but without the explicit basis is not a proof
    no_basis = ops_gate(seven_days(untunable_context(flips_basis=None)))
    assert crit(no_basis, "btc_context_alignment")["status"] == "unmeasured"


def test_btc_alignment_fails_on_untunable_violation() -> None:
    bad = {"versions": {"m@1": {"policy": "untunable", "buy_records": 1, "intent_records": 0}}}
    g = ops_gate(seven_days(untunable_context(flips=None, flips_basis=None, untunable_check=bad)))
    c = crit(g, "btc_context_alignment")
    assert c["status"] == "fail" and "untunable_violation 1" in c["note"] and g["status"] == "FAIL"
    intent = {"versions": {"m@1": {"policy": "untunable", "buy_records": 0, "intent_records": 2}}}
    assert "untunable_violation 2" in crit(
        ops_gate(seven_days(untunable_context(flips=None, flips_basis=None, untunable_check=intent))),
        "btc_context_alignment",
    )["note"]
    # a missing policy file proves nothing and is not a violation either: unmeasured
    missing = {"versions": {"m@1": {"policy": "missing", "buy_records": 5, "intent_records": 0}}}
    unproven = untunable_context(flips=None, flips_basis=None, untunable_check=missing)
    assert crit(ops_gate(seven_days(unproven)), "btc_context_alignment")["status"] == "unmeasured"
    # the shift limit still applies under an untunable policy
    assert crit(ops_gate(seven_days(untunable_context(max_shift=0.011))), "btc_context_alignment")["status"] == "fail"


def test_btc_alignment_old_document_without_basis_unmeasured() -> None:
    old = set_btc(shift_status="policy_untunable", flips=None, max_shift=0.0035)
    assert crit(ops_gate(seven_days(old)), "btc_context_alignment")["status"] == "unmeasured"


SCOPE_NOTE = (
    "feature_drift (PSI-v3): forma dei rendimenti veloci standardizzati per volatilita'. Livello di volatilita' e 28 "
    "feature persistenti non testabili in 7 giorni (n_eff_7d < 50): solo diagnostica support."
)


def v3_drift(alarm: bool = False, **kw: Any) -> dict[str, Any]:
    """The ``feature_drift`` block of PSI-v3 for one product, with its scope and its diagnostics."""
    block = measured_drift(alarm)
    block.update(
        rule="PSI_V3_VOLSTD",
        scope={"tested": ["z_ret_1h_bps", "z_ret_15m_bps"], "untested": [{"feature": f"f{i}", "n_eff_7d": 1.0}
                                                                          for i in range(28)], "note": SCOPE_NOTE},
        support_alarm=[],
    )  # fmt: skip
    block.update(kw)
    return block


def last_day(block: dict[str, Any]) -> Any:
    def mutate(ds: list[dict[str, Any]]) -> None:
        ds[-1]["parity"]["feature_drift"] = block

    return mutate


def test_feature_drift_not_testable_note() -> None:
    block = {"rule": "PSI_V3_VOLSTD", "status": "not_testable", "reason": "calibration_rejected_final", "alarm": None,
             "products": {"SOL-EUR": {"status": "not_testable", "states": 168}}}  # fmt: skip
    g = ops_gate(seven_days(last_day(block)))
    c = crit(g, "feature_drift")
    assert c["status"] == "unmeasured" and g["status"] == "INCOMPLETE"  # never a pass, never a fail
    assert c["note"] == (
        "feature_drift: not_testable. La regola PSI-v3 (ultima, pre-registrata) e' stata respinta da SIM-P1f-bis. "
        "Il criterio diventa not_covered solo con la sorveglianza feature_drift_watch misurata in ogni giorno della "
        "finestra e la diagnostica support e PSI di p_edge presenti (CONFLITTO P1g); altrimenti resta unmeasured. "
        "Motivo: not_testable_missing:2026-10-11."  # the six days before the last one are not "not_testable" blocks
    )


def test_feature_drift_scope_note_on_pass() -> None:
    ok = ops_gate(seven_days(last_day(v3_drift())))
    c = crit(ok, "feature_drift")
    assert c["status"] == "pass" and c["note"] == "no flagged feature | " + SCOPE_NOTE
    assert ok["status"] == "PASS", [x for x in ok["criteria"] if x["status"] != "pass"]
    loud = ops_gate(seven_days(last_day(v3_drift(alarm=True))))
    fail = crit(loud, "feature_drift")
    assert fail["status"] == "fail" and fail["note"].endswith(" | " + SCOPE_NOTE) and "atr_14_1d_bps" in fail["note"]


def test_feature_drift_scope_diagnostic() -> None:
    g = ops_gate(seven_days(last_day(v3_drift())))
    diag = next(d for d in g["diagnostics"] if d["name"] == "feature_drift_scope")
    assert diag["value"] == 2 and diag["detail"]["rule"] == "PSI_V3_VOLSTD" and len(diag["detail"]["untested"]) == 28
    assert diag["note"] == SCOPE_NOTE and "alarm" not in diag  # no threshold: outside the gate
    bare = next(d for d in ops_gate(seven_days())["diagnostics"] if d["name"] == "feature_drift_scope")
    assert bare["value"] is None and bare["detail"] is None and bare["note"] == "n/a"
    assert "feature_drift_scope" not in {c["name"] for c in g["criteria"]}


def test_feature_support_goes_to_alarms_not_anomalies(tmp_path: Path) -> None:
    block = v3_drift(support_alarm=["SOL-EUR"])
    learner = {"steps": {"L0": {"details": {
        "state_parity": {"state_parity": {"match": 1.0}, "psi_v1": {"max_feature_psi": 7.81, "worst_feature": "x"}},
        "feature_drift": block,
    }}}}  # fmt: skip
    (tmp_path / "learner").mkdir(parents=True)
    (tmp_path / "learner" / "2026-10-18.json").write_text(json.dumps(learner), encoding="utf-8")
    doc = build(tmp_path).generate(DAY1, 1)["daily"][0]
    assert "feature_support:SOL-EUR" in doc["alarms"] and "feature_drift:SOL-EUR" not in doc["alarms"]
    assert doc["anomalies"]["parity"] == 0 and doc["anomalies"]["operational"] == 0
    text = (tmp_path / "out" / "2026-10-17.md").read_text(encoding="utf-8")
    assert "feature_drift measured: no alarm | scope: 2 testate, 28 non testate" in text
    rejected = {"rule": "PSI_V3_VOLSTD", "status": "not_testable", "reason": "calibration_rejected_final"}
    learner["steps"]["L0"]["details"]["feature_drift"] = rejected
    (tmp_path / "learner" / "2026-10-18.json").write_text(json.dumps(learner), encoding="utf-8")
    build(tmp_path).generate(DAY1, 1)
    again = (tmp_path / "out" / "2026-10-17.md").read_text(encoding="utf-8")
    assert "feature_drift not_testable (PSI_V3_VOLSTD respinta, definitivo)" in again


NOT_COVERED_TEXT = (
    "feature_drift: not_covered. Deriva delle feature non certificata: PSI-v3 respinta per questa versione (D1). "
    "Coperta solo da sorveglianza senza soglia di gate (feature_drift_watch, riferimento e soglia diagnostici della "
    "corsa respinta, falsi allarmi fuori campione nel blocco watch). Guardia per prodotto e stop di calibrazione "
    "obbligatori in R0, non ancora implementati (differiti a P4, CONFLITTO P1g)."
)


def rejected_drift(
    *, watch_alarm: bool = False, watch: Any = "measured", edge: Any = "measured", support: bool = True
) -> Any:
    """The ``feature_drift`` block of a rejected PSI-v3 run for one product, with its watch and diagnostics."""
    diagnostics: dict[str, Any] = {"p_edge_psi": {"status": edge}}
    if support:
        diagnostics["support"] = {"states": 168, "states_outside": 0, "share": 0.0, "features": {}}
    block = {"status": "not_testable", "states": 168, "diagnostics": diagnostics}
    block["watch"] = {"status": watch, "S": 0.3, "alarm": watch_alarm, "flagged": []}
    return {
        "rule": "PSI_V3_VOLSTD", "status": "not_testable", "reason": "calibration_rejected_final", "alarm": None,
        "calibration": {"decision": "rejected", "final": True}, "scope": {"tested": [], "untested": [], "note": "n"},
        "watch": {"rule": "PSI_V3_VOLSTD", "reference": "reference_diagnostic", "c": 0.002265, "gate": False,
                  "false_alarm_oos": {"max": 0.0204}},  # fmt: skip
        "products": {"SOL-EUR": block}, "support_alarm": [], "watch_alarm": ["SOL-EUR"] if watch_alarm else [],
    }  # fmt: skip


def every_day(**kw: Any) -> Any:
    def mutate(ds: list[dict[str, Any]]) -> None:
        for d in ds:
            d["parity"]["feature_drift"] = rejected_drift(**kw)

    return mutate


def test_not_covered_note_exact() -> None:
    from modules.dryrun import report as report_mod

    assert report_mod.NOT_COVERED_NOTE == NOT_COVERED_TEXT
    assert report_mod.not_testable_note("x").endswith("Motivo: x.")


def test_feature_drift_not_covered_when_watch_every_day() -> None:
    g = ops_gate(seven_days(every_day()))
    c = crit(g, "feature_drift")
    assert c["status"] == "not_covered" and c["note"] == NOT_COVERED_TEXT
    assert c["value"] == {"SOL-EUR": 0.3} and c["threshold"] == 0.002265  # the threshold of the rejected run


def test_not_covered_does_not_block_gate_and_is_listed() -> None:
    g = ops_gate(seven_days(every_day()))
    assert g["status"] == "PASS" and g["not_covered"] == ["feature_drift"]  # never a pass, never a block
    assert ops_gate(seven_days())["not_covered"] == []
    others = [c for c in g["criteria"] if c["name"] != "feature_drift"]
    assert all(c["status"] == "pass" for c in others)


def test_feature_drift_unmeasured_if_one_day_lacks_watch() -> None:
    def hole(ds: list[dict[str, Any]]) -> None:
        every_day()(ds)
        ds[3]["parity"]["feature_drift"] = rejected_drift(watch="unmeasured")

    g = ops_gate(seven_days(hole))
    c = crit(g, "feature_drift")
    assert c["status"] == "unmeasured" and c["note"].endswith("Motivo: watch_unmeasured:2026-10-14:SOL-EUR.")
    assert g["status"] == "INCOMPLETE" and g["not_covered"] == []

    def no_block(ds: list[dict[str, Any]]) -> None:
        every_day()(ds)
        ds[0]["parity"]["feature_drift"] = None

    assert crit(ops_gate(seven_days(no_block)), "feature_drift")["note"].endswith("no_learner_document:2026-10-11.")


def test_feature_drift_unmeasured_if_p_edge_psi_missing() -> None:
    g = ops_gate(seven_days(every_day(edge="unmeasured")))
    assert crit(g, "feature_drift")["note"].endswith("p_edge_psi_unmeasured:2026-10-11:SOL-EUR.")
    g2 = ops_gate(seven_days(every_day(support=False)))
    assert crit(g2, "feature_drift")["note"].endswith("support_missing:2026-10-11:SOL-EUR.")
    assert crit(g, "feature_drift")["status"] == "unmeasured" and g["status"] == "INCOMPLETE"


def test_watch_alarm_in_alarms_not_anomalies_not_gate(tmp_path: Path) -> None:
    g = ops_gate(seven_days(every_day(watch_alarm=True)))
    assert crit(g, "feature_drift")["status"] == "not_covered" and g["status"] == "PASS"  # a warning only
    learner = {"steps": {"L0": {"details": {
        "state_parity": {"state_parity": {"match": 1.0}, "psi_v1": {"max_feature_psi": 7.81, "worst_feature": "x"}},
        "feature_drift": rejected_drift(watch_alarm=True),
    }}}}  # fmt: skip
    (tmp_path / "learner").mkdir(parents=True)
    (tmp_path / "learner" / "2026-10-18.json").write_text(json.dumps(learner), encoding="utf-8")
    doc = build(tmp_path).generate(DAY1, 1)["daily"][0]
    assert "feature_drift_watch:SOL-EUR" in doc["alarms"] and "feature_drift:SOL-EUR" not in doc["alarms"]
    assert doc["anomalies"]["parity"] == 0 and doc["anomalies"]["operational"] == 0
    text = (tmp_path / "out" / "2026-10-17.md").read_text(encoding="utf-8")
    assert "criteri not_covered (non certificati): feature_drift" in text
    assert "feature_drift not_testable (PSI_V3_VOLSTD respinta, definitivo) | watch: ALARM on SOL-EUR" in text
