"""The app runs every configured book on the shared builder, restores them from the ledger after a restart, writes the
closed trades and the telemetry the report needs (spec sez. 5.1.4, 5.3, 6.3), and none of it can stop trading."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

import modules.app as app_module
from modules.app import Champion, StartupError, find_model_dir, load_alias_model, load_champion
from modules.config import BookConfig, TelemetryConfig
from modules.dryrun.registry import FilesystemModelRegistry
from modules.market.bars import BarClosed
from modules.market.timeframes import Bar
from tests.app_fixtures import (
    PRODUCTS,
    HoldAll,
    Rig,
    make_cfg,
    make_champion,
    make_rig,
    until,
)
from tests.test_decision_engine import WARM_A, frame

D = Decimal
BOOKS = (
    BookConfig(name="champion", decider="registry:champion"),
    BookConfig(name="challenger", decider="registry:challenger"),
    BookConfig(
        name="random_entry", decider="random", match_frequency_of="champion", seed=7, forced_buy_rate=1.0,
        forced_edge=0.9,
    ),
    BookConfig(name="buy_and_hold", decider="buy_and_hold"),
)  # fmt: skip


def cfg_with(tmp: Path, *names: str, **kw: Any) -> Any:
    return make_cfg(tmp, books=tuple(b for b in BOOKS if b.name in names), **kw)


async def settle(rig: Rig) -> None:
    """Let the gateway tasks of every book reach the point where they wait for the next bar (an armed entry or exit)."""
    for _ in range(300):
        moving = False
        for b in rig.rt.books:
            engine = b.engine
            for p in PRODUCTS:
                kind = engine.pending(p) if engine is not None else None
                if (kind == "entry" and not b.shadow.has_pending_entry(p)) or (
                    kind == "exit" and not b.shadow.has_pending_exit(p)
                ):
                    moving = True
            if b.kind == "buy_and_hold" and b.book.engine is not None and getattr(b.book.engine, "_busy", None):
                moving = moving or any(not b.shadow.has_pending_entry(p) for p in b.book.engine._busy)  # noqa: SLF001
        if not moving:
            await asyncio.sleep(0.02)
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the books did not settle")


async def feed_bars(rig: Rig, indices: range | list[int]) -> None:
    """Publish the bars of ``indices`` (both products) straight into the runtime, one minute at a time."""
    for i in indices:
        events = []
        for p in PRODUCTS:
            ev = rig.event(p, i)
            rig.clock.now = ev.published_at
            rig.feed.quote_around(p, float(frame(p).close.iloc[i]), ev.published_at)
            events.append(ev)
        await rig.rt.process_bars(events)
        await settle(rig)


def read_events(tmp: Path, day: str) -> list[dict[str, Any]]:
    text = (tmp / "data" / "events" / f"{day}.jsonl").read_text(encoding="utf-8")
    return [json.loads(x) for x in text.splitlines()]


def ledger_totals(rig: Rig, book: str) -> dict[str, Decimal]:
    bb = rig.book_named(book).account.balances
    return {c: bb.total(c) for c in ("EUR", "BTC", "SOL")}


# ============================================================================================ 1. which books run


async def test_every_configured_book_starts_and_the_challenger_only_with_a_registry_alias(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, books=BOOKS)
    async with make_rig(tmp_path, cfg=cfg, upto=None) as rig:
        assert [b.name for b in rig.rt.books] == ["champion", "random_entry", "buy_and_hold"]
        assert [b.kind for b in rig.rt.books] == ["model", "random_entry", "buy_and_hold"]
        assert set(rig.rt.kill_switch._accounts) == {"champion", "random_entry", "buy_and_hold"}  # noqa: SLF001
        names = {s.name for s in rig.rt.task_specs()}
        assert {"watchdog:champion", "watchdog:random_entry"} <= names and "watchdog:buy_and_hold" not in names
        assert rig.book_named("buy_and_hold").watchdog is None and rig.book_named("buy_and_hold").engine is None


async def test_a_challenger_in_the_registry_gets_its_own_model_and_book(tmp_path: Path) -> None:
    challenger = dataclasses.replace(make_champion(), version="challenger-stub")
    cfg = cfg_with(tmp_path, "champion", "challenger")
    alias = {"challenger": challenger}
    async with make_rig(tmp_path, cfg=cfg, upto=None, model_alias=lambda d, a: alias.get(a)) as rig:
        assert [b.name for b in rig.rt.books] == ["champion", "challenger"]
        assert rig.book_named("challenger").book.model_version == "challenger-stub"
        assert rig.book_named("champion").book.model_version == "stub"
        assert rig.book_named("champion").engine is not rig.book_named("challenger").engine


async def test_a_challenger_that_disagrees_on_the_sentiment_is_not_started(tmp_path: Path) -> None:
    challenger = dataclasses.replace(make_champion(sentiment=True), version="challenger-stub")
    cfg = cfg_with(tmp_path, "champion", "challenger")
    async with make_rig(tmp_path, cfg=cfg, upto=None, model_alias=lambda d, a: challenger) as rig:
        assert [b.name for b in rig.rt.books] == ["champion"]


async def test_random_entry_without_its_source_book_is_not_started(tmp_path: Path) -> None:
    cfg = make_cfg(
        tmp_path,
        books=(
            BookConfig(name="champion", decider="registry:champion"),
            BookConfig(name="random_entry", decider="random", match_frequency_of="challenger", seed=1),
            BookConfig(name="challenger", decider="registry:challenger"),
        ),
    )  # fmt: skip
    async with make_rig(tmp_path, cfg=cfg, upto=None) as rig:
        assert [b.name for b in rig.rt.books] == ["champion"]


async def test_the_untunable_gate_is_per_model_and_covers_the_random_book_of_that_model(tmp_path: Path) -> None:
    cfg = cfg_with(tmp_path, "champion", "random_entry", "buy_and_hold")
    async with make_rig(tmp_path, cfg=cfg, champion=make_champion(untunable="untunable_ev"), upto=None) as rig:
        gate = rig.rt.gates
        assert gate.entry_blocked("champion") == "policy_untunable:untunable_ev"
        assert gate.entry_blocked("random_entry") == "policy_untunable:untunable_ev"  # same model, same verdict
        assert gate.entry_blocked("buy_and_hold") is None  # the benchmark has no model


# ===================================================================================== 2. the books at work


async def test_the_books_trade_through_one_pipeline_and_every_trade_is_a_closed_trade_row(tmp_path: Path) -> None:
    cfg = cfg_with(tmp_path, "champion", "random_entry", "buy_and_hold")
    async with make_rig(tmp_path, cfg=cfg) as rig:
        await feed_bars(rig, range(WARM_A, WARM_A + 12))
        ledger = rig.rt.ledger
        names = ("champion", "random_entry", "buy_and_hold")
        longs = {name: [p.product_id for p in ledger.long_positions(name)] for name in names}
        assert sorted(longs["buy_and_hold"]) == sorted(PRODUCTS)  # bought each product once
        assert longs["champion"] and longs["random_entry"]  # the model book and the forced random book entered
        recs = rig.decisions()
        assert {r["book"] for r in recs} == {"champion", "random_entry"}  # one decision per (book, product, minute)
        for name in ("champion", "random_entry"):
            pos = ledger.long_positions(name)[0]
            assert pos.bracket_order_id is not None and pos.tp is not None and pos.sl is not None
        # each book has its own account: money spent in one is not missing in another
        eur = {name: ledger_totals(rig, name)["EUR"] for name in longs}
        assert all(v < D(100) for v in eur.values())
        assert rig.book_named("buy_and_hold").account.balances.free("EUR") < D(5)  # it spent its capital, no more
        # close the champion's BTC position through its bracket: a bar that trades below the stop
        champion = rig.book_named("champion")
        pos = ledger.long_positions("champion")[0]
        assert pos.sl is not None
        i = WARM_A + 12
        row = frame(pos.product_id).iloc[i]
        ts = frame(pos.product_id).index[i].to_pydatetime()
        crash = Bar(ts, float(row.open), float(row.high), float(pos.sl) - 1.0, float(pos.sl) - 0.5, float(row.volume))
        ev = BarClosed(pos.product_id, crash, "rest", ts + timedelta(seconds=180))
        rig.clock.now = ev.published_at
        await rig.rt.process_bars([ev])
        await settle(rig)
        (trade,) = [t for t in await ledger.closed_trades("champion") if t.product_id == pos.product_id]
        assert trade.exit_reason == "SL" and trade.net_quote < 0 and trade.model_version == "stub"
        assert not ledger.view("champion", pos.product_id).is_long
        assert champion.account.balances.reserved("EUR") >= 0 and champion.account.balances.free("EUR") > 0
        # the loss reached the account guards through the ledger, and the event log
        assert any(r.guard == "daily_open" for r in champion.guards.state_rows())


async def test_buy_and_hold_never_sells_and_has_no_horizon(tmp_path: Path) -> None:
    cfg = cfg_with(tmp_path, "champion", "buy_and_hold")
    async with make_rig(tmp_path, cfg=cfg, champion=make_champion(inner=HoldAll())) as rig:
        await feed_bars(rig, range(WARM_A, WARM_A + 10))
        assert sorted(p.product_id for p in rig.rt.ledger.long_positions("buy_and_hold")) == sorted(PRODUCTS)
        assert rig.rt.ledger.long_positions("champion") == []  # the HOLD-only champion stays flat
        assert await rig.rt.ledger.closed_trades("buy_and_hold") == []


# ================================================================================================ 3. restart


async def _open_run(tmp: Path, *, history: dict[str, pd.DataFrame] | None = None, **kw: Any) -> Any:
    cfg = cfg_with(tmp, "champion", "random_entry", "buy_and_hold")
    return make_rig(tmp, cfg=cfg, history_frames=history, **kw)


def snapshot(rig: Rig) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for b in rig.rt.books:
        bb = b.account.balances
        out[b.name] = {
            "free": {c: bb.free(c) for c in ("EUR", "BTC", "SOL")},
            "reserved": {c: bb.reserved(c) for c in ("EUR", "BTC", "SOL")},
            "total": {c: bb.total(c) for c in ("EUR", "BTC", "SOL")},
            "fees": b.account.fees_paid, "realized": b.account.realized_pnl,
            "positions": {p.product_id: (p.base_qty, p.avg_entry, p.bracket_order_id, p.tp, p.sl)
                          for p in rig.rt.ledger.long_positions(b.name)},
        }  # fmt: skip
    return out


async def test_a_restart_with_open_positions_rebuilds_the_accounts_and_the_brackets(tmp_path: Path) -> None:
    async with await _open_run(tmp_path) as first:
        await feed_bars(first, range(WARM_A, WARM_A + 12))
        first.rt.request_stop("test")
        await first.rt._shutdown()  # noqa: SLF001  (a clean stop: working entries are cancelled, brackets are NOT)
        before = snapshot(first)
        for name in ("champion", "random_entry", "buy_and_hold"):
            assert before[name]["positions"], f"{name} should hold a position before the restart"
            assert any(v > 0 for v in before[name]["reserved"].values())  # the brackets hold base
            assert before[name]["reserved"]["EUR"] == 0  # nothing is waiting for a fill any more
    async with await _open_run(tmp_path, upto=WARM_A + 12) as second:
        after = snapshot(second)
        assert after == before  # balances, reservations, fees, realized PnL, positions and bracket ids: identical
        for name in after:
            for c, total in after[name]["total"].items():
                assert total >= 0 and total == after[name]["free"][c] + after[name]["reserved"][c]
        for b in second.rt.books:
            assert set(b.shadow._brackets) == set(before[b.name]["positions"])  # noqa: SLF001
        assert all(not r.unprotected and not r.placed for r in second.rt.restore_reports.values())


async def test_after_a_restart_the_restored_positions_are_managed_to_the_end(tmp_path: Path) -> None:
    async with await _open_run(tmp_path) as first:
        await feed_bars(first, range(WARM_A, WARM_A + 12))
        first.rt.request_stop("test")
        await first.rt._shutdown()  # noqa: SLF001
        start = snapshot(first)
    async with await _open_run(tmp_path, upto=WARM_A + 12) as second:
        target = second.book_named("champion")
        product, (qty, avg, _bracket, _tp, sl) = next(iter(start["champion"]["positions"].items()))
        i = WARM_A + 12
        row = frame(product).iloc[i]
        ts = frame(product).index[i].to_pydatetime()
        crash = Bar(ts, float(row.open), float(row.high), float(sl) - 2.0, float(sl) - 1.0, float(row.volume))
        ev = BarClosed(product, crash, "rest", ts + timedelta(seconds=180))
        second.clock.now = ev.published_at
        await second.rt.process_bars([ev])
        await settle(second)
        (trade,) = await second.rt.ledger.closed_trades("champion")
        assert trade.exit_reason == "SL" and trade.product_id == product and trade.entry_size == qty
        assert not second.rt.ledger.view("champion", product).is_long
        bb = target.account.balances
        assert bb.reserved(product.split("-")[0]) == 0 and bb.total(product.split("-")[0]) == 0
        # the account says exactly what the fills say: starting capital + net PnL
        net = sum((t.net_quote for t in await second.rt.ledger.closed_trades("champion")), D(0))
        other = [p for p in start["champion"]["positions"] if p != product]
        if not other:
            assert bb.total("EUR") == D(100) + net
        assert all(bb.free(c) >= 0 and bb.reserved(c) >= 0 for c in ("EUR", "BTC", "SOL"))


async def test_a_stop_touched_while_the_process_was_down_is_resolved_at_the_restart(tmp_path: Path) -> None:
    async with await _open_run(tmp_path) as first:
        await feed_bars(first, range(WARM_A, WARM_A + 12))
        first.rt.request_stop("test")
        await first.rt._shutdown()  # noqa: SLF001
        start = snapshot(first)
    product, (_qty, _avg, _bracket, _tp, sl) = next(iter(start["champion"]["positions"].items()))
    history = {p: frame(p).iloc[: WARM_A + 20].copy() for p in PRODUCTS}
    low_col = history[product].columns.get_loc("low")
    history[product].iloc[WARM_A + 15, low_col] = float(sl) - 3.0  # the downtime crossed the stop
    async with await _open_run(tmp_path, history=history) as second:
        trades = await second.rt.ledger.closed_trades("champion")
        crossed = [t for t in trades if t.product_id == product]
        assert len(crossed) == 1 and crossed[0].exit_reason == "SL" and crossed[0].gap_resolved
        assert crossed[0].exit_at == history[product].index[WARM_A + 15].to_pydatetime()  # at the bar that touched it
        assert not second.rt.ledger.view("champion", product).is_long
        assert second.rt.restore_reports["champion"].catch_up_from  # the gap procedure ran
        bb = second.book_named("champion").account.balances
        assert bb.reserved(product.split("-")[0]) == 0 and all(bb.free(c) >= 0 for c in ("EUR", "BTC", "SOL"))
        guard = second.rt.kill_switch  # a stop out is a loss for the account guards too
        assert guard.entry_blocked("champion") in (None, "loss_streak")


async def test_orders_that_cannot_be_resumed_are_cancelled_and_leave_no_reservation(tmp_path: Path) -> None:
    async with await _open_run(tmp_path) as first:
        await feed_bars(first, range(WARM_A, WARM_A + 2))  # decisions made, entries waiting for their bar
        pending = [b.name for b in first.rt.books if any(b.shadow.has_pending_entry(p) for p in PRODUCTS)]
        assert pending
        first.rt.life.stopping = True
        # crash instead of a clean stop: no shutdown, the working entries stay OPEN in the ledger
        open_before = [o.client_order_id for n in pending for o in await first.rt.ledger.open_orders(n)]
        assert open_before
        await first.rt.ledger.close()
        first.rt.stack.push_async_callback(_noop)
    async with await _open_run(tmp_path, upto=WARM_A + 2) as second:
        for n in pending:
            assert await second.rt.ledger.open_orders(n) == [] or all(
                o.leg == "bracket" for o in await second.rt.ledger.open_orders(n)
            )
            bb = second.book_named(n).account.balances
            assert bb.reserved("EUR") == 0 and bb.free("EUR") == D(100)  # nothing spent, nothing held
        assert any(r.cancelled for r in second.rt.restore_reports.values())


async def _noop() -> None:
    return None


async def test_a_ledger_that_replays_to_a_negative_balance_refuses_to_start(tmp_path: Path) -> None:
    async with await _open_run(tmp_path) as first:
        await feed_bars(first, range(WARM_A, WARM_A + 12))
        first.rt.request_stop("test")
        await first.rt._shutdown()  # noqa: SLF001
    import sqlite3

    con = sqlite3.connect(tmp_path / "state" / "chronos_dry.db")
    con.execute("UPDATE meta SET value='1' WHERE key='starting_quote:champion'")  # the fills spent more than that
    con.commit()
    con.close()
    with pytest.raises(StartupError, match="restored|negative"):
        async with await _open_run(tmp_path, upto=WARM_A + 12):
            pass


async def test_the_starting_capital_is_the_one_the_book_started_with(tmp_path: Path) -> None:
    async with await _open_run(tmp_path) as first:
        await feed_bars(first, range(WARM_A, WARM_A + 3))
        first.rt.request_stop("test")
        await first.rt._shutdown()  # noqa: SLF001
    cfg = make_cfg(
        tmp_path,
        books=(
            BookConfig(name="champion", decider="registry:champion", starting_quote=D("500")),
            BOOKS[2], BOOKS[3],
        ),
    )  # fmt: skip
    async with make_rig(tmp_path, cfg=cfg, upto=WARM_A + 3) as second:
        assert second.book_named("champion").account.starting_quote == D("100.00")


# ======================================================================================== 4. the model registry


def _model_dir(root: Path, name: str, version: str) -> Path:
    d = root / "baseline" / name
    d.mkdir(parents=True)
    (d / "model_card.json").write_text(json.dumps({"model": version}), encoding="utf-8")
    (d / "label_config.json").write_text("{}", encoding="utf-8")
    (d / "final").mkdir()
    (d / "final" / "policy.json").write_text('{"untunable": true}', encoding="utf-8")
    return d


def test_an_empty_registry_bootstraps_the_newest_baseline_once_and_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _model_dir(tmp_path, "baseline-lgbm@2026-10-18.1", "baseline-lgbm@2026-10-18.1")
    newest = _model_dir(tmp_path, "baseline-lgbm@2026-10-18.2", "baseline-lgbm@2026-10-18.2")
    found = find_model_dir(tmp_path, "registry:champion")
    registry = FilesystemModelRegistry(tmp_path / "registry")
    assert registry.versions() == ["baseline-lgbm@2026-10-18.2"] and registry.aliases() == {
        "champion": "baseline-lgbm@2026-10-18.2"
    }
    assert found == registry.get("champion").path != newest and (found / "model_card.json").is_file()
    assert registry.get("champion").manifest["origin"].startswith("bootstrap:")
    assert "champion_bootstrapped" in capsys.readouterr().out
    # a second start reads the alias: nothing is registered or promoted again
    _model_dir(tmp_path, "baseline-lgbm@2026-10-19.1", "baseline-lgbm@2026-10-19.1")
    assert find_model_dir(tmp_path, "registry:champion") == found
    assert registry.versions() == ["baseline-lgbm@2026-10-18.2"]


def test_a_registry_with_versions_but_no_champion_is_never_bootstrapped(tmp_path: Path) -> None:
    src = _model_dir(tmp_path, "baseline-lgbm@2026-10-18.1", "baseline-lgbm@2026-10-18.1")
    from modules.dryrun.registry import artifacts_from_dir

    registry = FilesystemModelRegistry(tmp_path / "registry")
    registry.register(artifacts_from_dir(src), {"version": "v1"})
    with pytest.raises(StartupError, match="no champion"):
        find_model_dir(tmp_path, "registry:champion")
    assert registry.aliases() == {}  # not promoted behind the operator's back


def test_nothing_to_bootstrap_from_is_a_clear_startup_error(tmp_path: Path) -> None:
    with pytest.raises(StartupError, match="empty"):
        find_model_dir(tmp_path, "registry:champion")


def test_a_damaged_registry_is_a_startup_error_not_an_empty_one(tmp_path: Path) -> None:
    (tmp_path / "registry").mkdir()
    (tmp_path / "registry" / "aliases.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(StartupError, match="registry"):
        find_model_dir(tmp_path, "registry:champion")
    with pytest.raises(StartupError, match="registry"):
        load_alias_model(tmp_path, "challenger")


def test_an_alias_that_is_not_set_is_none_and_a_registered_champion_is_used(tmp_path: Path) -> None:
    src = _model_dir(tmp_path, "baseline-lgbm@2026-10-18.1", "baseline-lgbm@2026-10-18.1")
    from modules.dryrun.registry import artifacts_from_dir

    assert load_alias_model(tmp_path, "challenger") is None
    registry = FilesystemModelRegistry(tmp_path / "registry")
    registry.register(artifacts_from_dir(src), {"version": "v1"})
    registry.promote("v1", "champion")
    assert find_model_dir(tmp_path, "registry:champion") == registry.get("champion").path
    with pytest.raises(StartupError, match="no alias"):
        find_model_dir(tmp_path, "registry:challenger")
    assert load_champion  # the loader itself is covered by test_app.py


# ====================================================================================== 5. telemetry at work


async def test_the_run_leaves_status_events_equity_and_ops_and_the_report_reads_them(tmp_path: Path) -> None:
    cfg = cfg_with(tmp_path, "champion", "random_entry", "buy_and_hold")
    async with make_rig(tmp_path, cfg=cfg) as rig:
        rig.start()
        await feed_bars(rig, range(WARM_A, WARM_A + 8))
        day = rig.clock.now.date().isoformat()
        status = tmp_path / "telemetry" / "status.json"
        equity = tmp_path / "data" / "equity" / f"{day}.jsonl"
        ops = tmp_path / "data" / "reports" / "ops" / f"{day}.json"
        await until(lambda: status.exists() and equity.exists() and ops.exists())
        doc = json.loads(status.read_text(encoding="utf-8"))
        assert doc["v"] == "chronos.status.v1" and doc["agent"]["books"] == ["champion", "random_entry", "buy_and_hold"]
        assert set(doc["balances"]) == {"champion", "random_entry", "buy_and_hold"} and doc["positions"]
        assert doc["agent"]["models"]["buy_and_hold"] is None and doc["agent"]["models"]["champion"] == "stub"
        assert await rig.stop() == 0
    rows = [json.loads(x) for x in equity.read_text(encoding="utf-8").splitlines()]
    assert {r["book"] for r in rows} == {"champion", "random_entry", "buy_and_hold"}
    assert rows[0]["v"] == "chronos.equity.v1"
    assert all(D(r["equity"]) > 0 and r["mode"] == "dry" for r in rows)
    ops_doc = json.loads(ops.read_text(encoding="utf-8"))
    assert ops_doc["v"] == "chronos.ops.v1" and ops_doc["uptime"] is not None and ops_doc["running"] is False
    assert ops_doc["crashes"] == 0 and set(ops_doc["bars"]) == set(PRODUCTS) and ops_doc["bars"]["BTC-EUR"]["rest"] >= 8
    assert ops_doc["fee_source"] == "account" and ops_doc["median_spread_bps"]["BTC-EUR"] == pytest.approx(3.0)
    events = read_events(tmp_path, day)
    types = [e["type"] for e in events]
    assert types[0] == "agent_started" and "agent_stopping" in types and "position_opened" in types
    assert "decision_outcome" in types
    from modules.dryrun.report import JsonOpsSource

    assert JsonOpsSource(tmp_path / "data" / "reports" / "ops").day(rig.clock.now.date()) is not None


async def test_a_broken_telemetry_directory_never_stops_the_run(tmp_path: Path) -> None:
    (tmp_path / "telemetry").write_text("a file where the directory should be", encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "equity").write_text("also a file", encoding="utf-8")
    (tmp_path / "data" / "events").write_text("and this one", encoding="utf-8")
    cfg = cfg_with(tmp_path, "champion")
    async with make_rig(tmp_path, cfg=cfg) as rig:
        rig.start()
        await feed_bars(rig, range(WARM_A, WARM_A + 6))
        await until(lambda: rig.rt.telemetry_errors.total >= 3)
        assert rig.rt.life.failures == deque_empty() and rig.rt.life.faults == {}
        assert rig.rt.kill_switch.entry_blocked("champion") is None  # trading is untouched
        assert rig.book_named("champion").engine.stats.decisions >= 6
        assert await rig.stop() == 0
    assert rig.rt.life.exit_code == 0


def deque_empty() -> Any:
    from collections import deque

    return deque([], maxlen=app_module.FAILURES_KEPT)


async def test_a_clean_stop_and_a_crash_are_told_apart_in_the_ops_of_the_day(tmp_path: Path) -> None:
    async with make_rig(tmp_path, cfg=cfg_with(tmp_path, "champion"), upto=None) as first:
        first.start()
        await until(lambda: first.rt.ops is not None and first.rt.ops.today()["running"])
        assert await first.stop() == 0
    day = first.clock.now.date().isoformat()
    ops_file = tmp_path / "data" / "reports" / "ops" / f"{day}.json"
    assert json.loads(ops_file.read_text(encoding="utf-8"))["crashes"] == 0
    # a second run that dies without stopping: its file says running, so the third run counts one crash
    async with make_rig(tmp_path, cfg=cfg_with(tmp_path, "champion"), upto=None) as second:
        second.start()
        await until(lambda: ops_file.exists() and json.loads(ops_file.read_text(encoding="utf-8"))["running"])
        second.rt.ops._dirty.clear()  # noqa: SLF001
        data = json.loads(ops_file.read_text(encoding="utf-8"))
        assert data["running"] is True
        assert await second.stop() == 0
    data = json.loads(ops_file.read_text(encoding="utf-8"))
    data["running"] = True  # what a killed process leaves behind
    ops_file.write_text(json.dumps(data), encoding="utf-8")
    async with make_rig(tmp_path, cfg=cfg_with(tmp_path, "champion"), upto=None) as third:
        assert third.rt.ops is not None and third.rt.ops.today()["crashes"] == 1


async def test_a_dry_run_violation_is_counted_in_the_ops_and_is_an_event(tmp_path: Path) -> None:
    from modules.execution.exchange import DryRunViolation

    async with make_rig(tmp_path, cfg=cfg_with(tmp_path, "champion"), upto=None) as rig:
        rig.start()
        rig.rt.life.fatal(DryRunViolation("create_order"))
        await asyncio.wait_for(rig.task, 20)  # type: ignore[arg-type]
    day = rig.clock.now.date().isoformat()
    ops_doc = json.loads((tmp_path / "data" / "reports" / "ops" / f"{day}.json").read_text(encoding="utf-8"))
    assert ops_doc["dry_run_violations"] == 1
    events = read_events(tmp_path, day)
    assert "dry_run_violation" in [e["type"] for e in events]
    assert rig.rt.life.exit_code == 3


async def test_events_never_contain_secrets(tmp_path: Path) -> None:
    secret = "SUPER-SECRET-VALUE-123456"
    cfg = cfg_with(tmp_path, "champion")
    async with make_rig(tmp_path, cfg=cfg, upto=None, env={"COINBASE_API_KEY": secret}) as rig:
        rig.start()
        rig.rt.emit("probe", "info", detail=f"leaked {secret} here")
        assert await rig.stop() == 0
    day = rig.clock.now.date().isoformat()
    text = (tmp_path / "data" / "events" / f"{day}.jsonl").read_text(encoding="utf-8")
    assert secret not in text and "leaked *** here" in text


def test_the_unused_import_guard() -> None:
    assert datetime and Champion and TelemetryConfig  # keeps the imports above honest for the type checker


async def test_an_exit_parked_on_a_bar_does_not_hold_the_shutdown_for_the_whole_grace(tmp_path: Path) -> None:
    import time

    cfg = cfg_with(tmp_path, "champion")
    async with make_rig(tmp_path, cfg=cfg) as rig:
        await feed_bars(rig, range(WARM_A, WARM_A + 12))
        product = rig.rt.ledger.long_positions("champion")[0].product_id
        task = asyncio.create_task(rig.book_named("champion").shadow.emergency_exit(product))
        await until(lambda: rig.book_named("champion").shadow.has_pending_exit(product))
        rig.rt.request_stop("test")
        t0 = time.monotonic()
        await rig.rt._shutdown()  # noqa: SLF001
        assert time.monotonic() - t0 < app_module.SHUTDOWN_GRACE_S - 3  # not the 10 s of the naive drain
        assert rig.rt.life.exit_code == 0
        task.cancel()
