"""Rollback advice in the daily report (spec 5.1.7, resolution P1e B.7): a recommendation, never an action."""

from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from modules.config import DryRunConfig
from modules.dryrun import report as report_module
from modules.dryrun.registry import FilesystemModelRegistry, artifacts_from_dir
from modules.dryrun.report import (
    ChampionInfo,
    DryReporter,
    JsonOpsSource,
    RegistryChampionSource,
    SqliteTradeSource,
    Trade,
    live_consistency,
    oos_reference,
    parse_trade,
    rollback_advice,
)
from modules.dryrun.report import ReportPaths as Paths

D = Decimal
SINCE = datetime(2026, 10, 10, tzinfo=UTC)
INFO = ChampionInfo("v2", SINCE, None)
OOS = (60.0, 5.0, 0.5)  # mean net bps of the OOS trades, its standard error, rho


def trade(
    net: str = "1", *, version: str | None = "v2", bps: float | None = 58.0, reason: str = "TP", i: int = 0
) -> Trade:
    exit_at = SINCE + timedelta(days=1, hours=i)
    return Trade("champion", "SOL-EUR", reason, exit_at, D(net), D("0.02"), net_bps=bps, model_version=version)


def trades(n: int, **kw: Any) -> list[Trade]:
    """``n`` trades alternating +1 / -0.5 EUR (profit factor 2.0) with net bps 58 +- 10."""
    base = kw.pop("bps", 58.0)
    return [trade("1" if i % 2 == 0 else "-0.5", bps=base + (10 if i % 2 == 0 else -10), i=i, **kw) for i in range(n)]


def advise(info: ChampionInfo | None = INFO, **kw: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "trades": trades(40), "oos": OOS, "parity": 0.995, "min_parity": 0.99, "revision_failures": [],
        "fallback_ratio": 0.01, "max_fallback_ratio": 0.20, "kill_switch_bug": 0, "loss_trips": 0, "min_trades": 30,
    }  # fmt: skip
    return rollback_advice(info, **(args | kw))


def test_rollback_advice_none_when_all_is_well() -> None:
    a = advise()
    assert a["advice"] == "none" and a["triggers"] == [] and a["version"] == "v2" and a["closed_trades"] == 40
    assert a["economics"]["status"] == "measured" and a["economics"]["profit_factor"] == pytest.approx(2.0)
    assert a["command"] == 'python -m modules.dryrun.registry rollback champion --reason "<why>"'
    assert a["since"] == "2026-10-10T00:00:00Z"


def test_rollback_advice_none_below_min_trades() -> None:
    """Below 30 closed trades of the version there is NO economic recommendation, whatever the numbers say."""
    bad = [trade("-1", bps=-100.0, i=i) for i in range(29)]  # a clear loser, but only 29 trades
    a = advise(trades=bad, loss_trips=5)
    assert a["advice"] == "none" and a["triggers"] == []
    assert a["economics"]["status"] == "insufficient_trades" and a["economics"]["closed_trades"] == 29
    # the 30th trade switches the economic criteria on
    bad.append(trade("-1", bps=-100.0, i=30))
    assert advise(trades=bad, loss_trips=0)["advice"] == "consider_rollback"
    # only trades OF THE VERSION count: 29 of v2 and many of another version are still 29
    other = [trade("-1", version="v1", i=i) for i in range(100)]
    assert advise(trades=[*bad[:29], *other])["economics"]["status"] == "insufficient_trades"


def test_manual_closes_and_other_versions_are_not_counted() -> None:
    mixed = [*trades(30), *[trade("-1", reason="MANUAL", i=i) for i in range(10)]]
    a = advise(trades=mixed)
    assert a["closed_trades"] == 30 and a["economics"]["profit_factor"] == pytest.approx(2.0)
    a = advise(trades=[*trades(29), trade("-1", reason="MANUAL")])
    assert a["economics"]["status"] == "insufficient_trades"  # the manual close is not the 30th


def test_trades_without_a_recorded_version_make_the_economics_unmeasured() -> None:
    a = advise(trades=trades(40, version=None))
    assert a["economics"]["status"] == "unmeasured" and a["economics"]["closed_trades"] is None
    assert a["economics"]["reason"] == "trades_without_model_version" and a["advice"] == "none"  # never "zero"


def test_rollback_advice_pf_below_one() -> None:
    losing = [trade("1" if i % 3 == 0 else "-1", i=i) for i in range(30)]  # 10 wins, 20 losses: PF 0.5
    a = advise(trades=losing)
    assert a["advice"] == "consider_rollback" and a["economic_triggers"] == ["profit_factor:0.500"]
    even = [trade("1" if i % 2 == 0 else "-1", i=i) for i in range(30)]  # PF exactly 1.0: not below
    assert advise(trades=even)["economic_triggers"] == []


def test_rollback_advice_z_below_threshold() -> None:
    far = advise(trades=trades(40, bps=5.0))
    assert far["advice"] == "consider_rollback"
    assert len(far["economic_triggers"]) == 1 and far["economic_triggers"][0].startswith("oos_inconsistency_z:")
    assert far["economics"]["z_vs_oos"] < -1.96
    near = advise(trades=trades(40, bps=58.0))
    assert near["economic_triggers"] == [] and near["economics"]["z_vs_oos"] > -1.96
    assert advise(oos=None)["economics"]["z_vs_oos"] is None  # a version without OOS artifacts: no z, no trigger


def test_live_consistency_uses_the_e5_function() -> None:
    from training.evaluate import Boot, holdout_consistency

    ts = trades(40, bps=20.0)
    z = live_consistency(ts, OOS)
    assert z is not None
    import math
    import statistics

    values = [t.net_bps for t in ts if t.net_bps is not None]
    expected = holdout_consistency(
        Boot(statistics.fmean(values), statistics.stdev(values) / math.sqrt(40), 0, 0, 0, 40),
        Boot(OOS[0], OOS[1], 0, 0, 0, 0),
    )
    assert z == pytest.approx(expected)
    assert live_consistency(ts[:1], OOS) is None
    assert live_consistency([*ts[:5], trade(bps=None)], OOS) is None


def test_rollback_advice_two_loss_trips() -> None:
    assert advise(loss_trips=1)["advice"] == "none"
    two = advise(loss_trips=2)
    assert two["advice"] == "consider_rollback" and two["economic_triggers"] == ["kill_switch_loss_trips:2"]


def test_rollback_advice_defects_immediate() -> None:
    """Defects need no minimum of trades."""
    few = trades(3)
    cases = {
        "decision_parity:0.9800": {"parity": 0.98},
        "revision:R1:BTC-EUR": {"revision_failures": ["R1:BTC-EUR"]},
        "decider_fallback_ratio:0.250": {"fallback_ratio": 0.25},
        "kill_switch_bug": {"kill_switch_bug": 1},
    }
    for trigger, kw in cases.items():
        a = advise(trades=few, **kw)
        assert a["advice"] == "consider_rollback" and a["triggers"] == [trigger], trigger
        assert a["defect_triggers"] == [trigger] and a["economics"]["status"] == "insufficient_trades"
    # the thresholds are inclusive on the good side
    assert advise(trades=few, parity=0.99)["advice"] == "none"
    assert advise(trades=few, fallback_ratio=0.20)["advice"] == "none"
    assert advise(trades=few, parity=None, fallback_ratio=None, kill_switch_bug=None)["advice"] == "none"
    broken = advise(ChampionInfo("v2", SINCE, None, False, "CorruptRegistry: x"), trades=few)
    assert broken["triggers"] == ["version_not_loadable"] and broken["error"] == "CorruptRegistry: x"
    both = advise(trades=few, parity=0.5, kill_switch_bug=2)
    assert both["triggers"] == ["decision_parity:0.5000", "kill_switch_bug"]


def test_no_champion_no_advice() -> None:
    a = advise(None)
    assert a == {"version": None, "advice": "none", "triggers": [], "note": "no champion alias"}


# ------------------------------------------------------------------------- the report and the registry


def make_registry(tmp_path: Path) -> FilesystemModelRegistry:
    root = tmp_path / "registry"
    reg = FilesystemModelRegistry(root, clock=lambda: SINCE)
    for v in ("v1", "v2"):
        src = tmp_path / "src" / v
        src.mkdir(parents=True)
        (src / "model_card.json").write_text("{}", encoding="utf-8")
        if v == "v2":
            rows = [{"net_ret_bps": 55.0 + (i % 5) * 2, "neff_weight": 0.8} for i in range(60)]
            (src / "oos_trades.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
            (src / "oos_spans.json").write_text(json.dumps({"rho": 0.5}), encoding="utf-8")
        reg.register(artifacts_from_dir(src), {"version": v})
    reg.promote("v1")
    reg.promote("v2", reason="refit")
    return reg


class Fixed:
    def __init__(self, t: list[Trade]) -> None:
        self.t = t

    def load(self) -> list[Trade]:
        return self.t


def reporter(tmp_path: Path, t: list[Trade], inbox: Path | None = None) -> DryReporter:
    paths = Paths(
        decisions_dir=tmp_path / "decisions", equity_dir=tmp_path / "equity", learner_dir=tmp_path / "learner",
        labels_dir=tmp_path / "labels", out_dir=tmp_path / "out",
    )  # fmt: skip
    return DryReporter(
        DryRunConfig(), paths, Fixed(t), JsonOpsSource(tmp_path / "ops"),
        champion=RegistryChampionSource(tmp_path / "registry"), max_fallback_ratio=0.20,
    )  # fmt: skip


def tree_hash(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*")) if p.is_file()
    }  # fmt: skip


def write_ops(tmp_path: Path, day: str, trips: list[dict[str, Any]]) -> None:
    (tmp_path / "ops").mkdir(exist_ok=True)
    (tmp_path / "ops" / f"{day}.json").write_text(
        json.dumps({"uptime": 0.999, "crashes": 0, "dry_run_violations": 0, "kill_switch_trips": trips}),
        encoding="utf-8",
    )


def test_the_daily_document_carries_the_advice(tmp_path: Path) -> None:
    make_registry(tmp_path)
    day = date(2026, 10, 12)
    for d in (date(2026, 10, 11), day):
        write_ops(tmp_path, d.isoformat(), [{"book": "champion", "guard": "daily_loss"}])
    out = reporter(tmp_path, trades(40)).generate(day, 1)
    adv = out["daily"][0]["rollback_advice"]
    assert adv["version"] == "v2" and adv["since"] == "2026-10-10T00:00:00Z" and adv["closed_trades"] == 40
    assert adv["economics"]["loss_trips"] == 2 and adv["advice"] == "consider_rollback"
    assert adv["economic_triggers"] == ["kill_switch_loss_trips:2"]  # 2 trips over the two days since the version
    md = (tmp_path / "out" / "2026-10-12.md").read_text(encoding="utf-8")
    assert "Rollback advice" in md and "kill_switch_loss_trips:2" in md and "rollback champion --reason" in md
    assert (tmp_path / "out" / "2026-10-12.json").exists()


def test_the_report_counts_only_loss_guards_of_the_champion_since_the_version_took_over(tmp_path: Path) -> None:
    make_registry(tmp_path)
    write_ops(tmp_path, "2026-10-09", [{"guard": "daily_loss", "book": "champion"}] * 3)  # before v2: not its fault
    trips = [
        {"book": "champion", "guard": "daily_loss", "bug": True}, {"book": "random_entry", "guard": "daily_loss"},
        {"book": "champion", "guard": "manual_pause"}, {"book": "*", "guard": "loss_streak"},
    ]  # fmt: skip
    write_ops(tmp_path, "2026-10-12", trips)
    adv = reporter(tmp_path, trades(40)).build_day(date(2026, 10, 12), all_trades=trades(40))["rollback_advice"]
    assert adv["economics"]["loss_trips"] == 1  # only the global loss_streak: not a bug, not another book, not manual
    assert "kill_switch_bug" in adv["defect_triggers"]  # the bug trip is a defect, though


def test_the_advice_without_a_registry_is_none(tmp_path: Path) -> None:
    doc = reporter(tmp_path, []).build_day(date(2026, 10, 12), all_trades=[])
    assert doc["rollback_advice"] == {"version": None, "advice": "none", "triggers": [], "note": "no champion alias"}
    rep = reporter(tmp_path, [])
    rep.champion = None
    assert rep.build_day(date(2026, 10, 12), all_trades=[])["rollback_advice"]["advice"] == "none"


def test_a_damaged_champion_version_is_a_defect(tmp_path: Path) -> None:
    reg = make_registry(tmp_path)
    (reg.get("champion").path / "model_card.json").write_text('{"tampered": true}', encoding="utf-8")
    adv = reporter(tmp_path, []).build_day(date(2026, 10, 12), all_trades=[])["rollback_advice"]
    assert adv["triggers"] == ["version_not_loadable"] and adv["version"] == "v2" and "CorruptRegistry" in adv["error"]


def test_oos_reference_reads_the_registered_artifacts(tmp_path: Path) -> None:
    reg = make_registry(tmp_path)
    ref = oos_reference(reg.get("champion").path)
    assert ref is not None
    mean, se, rho = ref
    assert mean == pytest.approx(59.0) and rho == 0.5
    assert se == pytest.approx((8.0 * 60 / 59) ** 0.5 / (48.0**0.5))  # sample sd over n_eff = 60 trades x 0.8
    assert oos_reference(reg.get_version("v1").path) is None and oos_reference(None) is None


def test_rollback_advice_never_writes_registry_or_inbox(tmp_path: Path) -> None:
    reg = make_registry(tmp_path)
    inbox = tmp_path / "commands" / "inbox"
    inbox.mkdir(parents=True)
    write_ops(tmp_path, "2026-10-12", [{"guard": "daily_loss"}] * 3)
    before_registry, before_inbox = tree_hash(tmp_path / "registry"), tree_hash(inbox)
    reporter(tmp_path, trades(40)).generate(date(2026, 10, 12), 3)
    assert tree_hash(tmp_path / "registry") == before_registry and tree_hash(inbox) == before_inbox
    assert reg.state("champion").op == "promote" and reg.state("champion").reason == "refit"
    # and the module cannot even call the writing methods
    tree = ast.parse(Path(report_module.__file__).read_text(encoding="utf-8"))
    called = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not called & {"promote", "rollback", "register", "promote_if_eligible"}


def test_trade_model_version_is_parsed_from_the_payload_and_the_ledger_column(tmp_path: Path) -> None:
    raw = {"book": "champion", "product": "SOL-EUR", "exit_reason": "TP", "exit_at": "2026-10-12T10:00:00Z",
           "net_quote": "1.0", "model_version": "v2"}  # fmt: skip
    bare = {k: v for k, v in raw.items() if k != "model_version"}
    assert parse_trade(raw).model_version == "v2" and parse_trade(bare).model_version is None
    db = tmp_path / "dry.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE closed_trades (trade_id TEXT, book TEXT, model_version TEXT, payload TEXT)")
    inside = {k: v for k, v in raw.items() if k != "model_version"}
    con.execute("INSERT INTO closed_trades VALUES ('a', 'champion', 'v9', ?)", (json.dumps(inside),))
    con.execute("INSERT INTO closed_trades VALUES ('b', 'champion', 'v1', ?)", (json.dumps(raw),))
    con.commit()
    con.close()
    got = {t.model_version for t in SqliteTradeSource(db).load()}
    assert got == {"v9", "v2"}  # the payload wins; the column fills in when the payload has none
