"""The report of the probe (plan sez. 5): blocking criteria against REST, descriptive measures, Clopper-Pearson."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import pytest

from modules.execution.exchange import OrderInfo
from modules.execution.raw_log import RawLog
from modules.probe import limits
from modules.probe.report import (
    _user_channel,
    build_report,
    clopper_pearson,
    render_markdown,
    write_report,
)
from tests.probe_fakes import P
from tests.test_probe_runtime import Env, build, close

D = Decimal


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    e = await build(tmp_path)
    yield e
    await close(e)


async def report_of(env: Env) -> dict:  # type: ignore[type-arg]
    return await build_report(env.rt.pcfg, env.venue, env.rt.ledger, env.rt.out_dir)  # type: ignore[arg-type]


async def tp_cycle(env: Env) -> None:
    await env.enter_at_nine()
    await env.rt.protections.check()  # (the monitor's loop is not running here: one look records the bracket latency)
    env.venue.set_market("80500.00")
    await env.rt.reconciler.reconcile_once()
    await env.settle()


async def test_an_empty_probe_is_not_validated_and_nothing_fails(env: Env) -> None:
    report = await report_of(env)
    assert report["validated"] is False and report["failed"] == []
    assert report["criteria"]["integrity"]["status"] == "pass"
    assert set(report["not_observed"]) >= {"orders_concordance", "fees", "post_only", "user_channel"}


async def test_a_clean_cycle_passes_every_criterion_the_run_could_judge(env: Env) -> None:
    await tp_cycle(env)
    report = await report_of(env)
    c = report["criteria"]
    assert report["failed"] == [], {k: v for k, v in c.items() if v["status"] == "fail"}
    for name in (
        "orders_concordance",
        "fills_concordance",
        "balances",
        "fees",
        "post_only",
        "bracket_coverage",
        "integrity",
    ):
        assert c[name]["status"] == "pass", (name, c[name])
    assert c["fees"]["detail"].startswith("4 fills") or "fills" in c["fees"]["detail"]
    assert set(report["not_observed"]) == {
        "entry_expiry",
        "user_channel",
        "fill_latency",
    }  # no stream, no unfilled entry
    assert report["validated"] is False  # not every criterion could be seen
    d = report["descriptive"]
    assert d["entries"]["sent"] == 1 and d["entries"]["filled"] == 1 and d["exit_reasons"] == {"TP": 1}
    assert D(d["pnl_quote"]) > 0 and d["bracket_open_seconds"]


async def test_a_fee_that_the_exchange_reports_differently_fails_the_concordance_and_the_fee_criterion(
    env: Env,
) -> None:
    await tp_cycle(env)
    order = next(o for o in env.venue.orders.values() if o.fills and o.spec.side == "BUY")
    fill = order.fills[0]
    order.fills[0] = fill.model_copy(update={"fee": fill.fee * 2})  # the exchange says it charged twice as much
    report = await report_of(env)
    assert "fills_concordance" in report["failed"] and "orders_concordance" in report["failed"]
    assert report["criteria"]["fills_concordance"]["differences"]


async def test_the_fee_criterion_checks_the_rate_of_the_tier(env: Env) -> None:
    await tp_cycle(env)
    env.venue.maker_bps = 40.0  # the account tier is now different from the one the fills were charged at
    report = await report_of(env)
    assert report["criteria"]["fees"]["status"] == "fail"


async def test_a_ledger_that_forgot_a_fill_is_caught(env: Env) -> None:
    await tp_cycle(env)
    await env.rt.ledger.connection.execute("DELETE FROM fills WHERE side='SELL'")
    report = await report_of(env)
    assert "fills_concordance" in report["failed"] or "orders_concordance" in report["failed"]


async def test_a_balance_the_ledger_does_not_know_is_caught(env: Env) -> None:
    await tp_cycle(env)
    env.venue.btc_total += D("0.001")  # BTC nobody accounted for
    report = await report_of(env)
    assert report["criteria"]["balances"]["status"] == "fail"


async def test_a_watchdog_exit_fails_the_integrity_criterion(env: Env) -> None:
    await env.enter_at_nine()
    await env.rt.exits.emergency_exit(P)
    report = await report_of(env)
    assert report["criteria"]["integrity"]["status"] == "fail" and "integrity" in report["failed"]


async def test_an_unfilled_entry_must_end_expired(env: Env) -> None:
    await env.at(9)
    await env.rt.scheduler.tick()
    await env.settle(timeout_s=30)
    report = await report_of(env)
    assert report["criteria"]["entry_expiry"]["status"] == "pass"
    assert (
        report["descriptive"]["entries"]["filled"] == 0 and report["descriptive"]["entries"]["fill_rate_ci95"][0] == 0.0
    )


async def test_the_report_files_are_written_and_hold_no_secret(env: Env, tmp_path: Path) -> None:
    await tp_cycle(env)
    report = await report_of(env)
    js, md = write_report(tmp_path / "rep", report)
    assert json.loads(js.read_text())["product"] == P
    text = md.read_text()
    assert "NON ANCORA VALIDATA" in text and "| orders_concordance | pass |" in text
    assert "apiKeys" not in text and "PRIVATE KEY" not in text
    assert render_markdown({**report, "validated": True}).count("VALIDATA") >= 1


# ----------------------------------------------------------------------------------- the user channel


def info(status: str, filled: str = "0", base: str = "0.00003", limit: str | None = "76000.00", stop: str | None = None,
         oid: str = "ex-1", cid: str = "c1", side: str = "BUY", fees: str = "0") -> OrderInfo:  # fmt: skip
    return OrderInfo(
        order_id=oid, client_order_id=cid, product_id=P, side=side, status=status, base_size=D(base),
        limit_price=None if limit is None else D(limit), stop_trigger_price=None if stop is None else D(stop),
        filled_size=D(filled), average_filled_price=None if D(filled) == 0 else D(limit or "0"), total_fees=D(fees),
    )  # fmt: skip


def message(status: str, filled: str = "0", base: str = "0.00003", limit: str = "76000.00", stop: str = "0",
            oid: str = "ex-1", cid: str = "c1", side: str = "BUY") -> dict[str, str]:  # fmt: skip
    leaves = D(base) - D(filled)
    return {
        "order_id": oid, "client_order_id": cid, "product_id": P, "order_side": side, "status": status,
        "cumulative_quantity": filled, "leaves_quantity": str(leaves), "limit_price": limit, "stop_price": stop,
        "avg_price": "0" if D(filled) == 0 else limit, "total_fees": "0", "number_of_fills": "0",
    }  # fmt: skip


def write_channel(tmp_path: Path, msgs: list[dict[str, str]]) -> Path:
    log = RawLog(tmp_path / "chan", clock=lambda: 1.0)
    for i, m in enumerate(msgs):
        raw = json.dumps({"channel": "user", "events": [{"type": "update", "orders": [m]}]})
        log.write({"t": 1000.0 + i, "raw": raw})
    log.close()
    return log.path


def orders_for(*cids: str):  # type: ignore[no-untyped-def]
    from datetime import UTC, datetime

    from modules.ledger.positions import OrderRecord

    now = datetime(2026, 10, 28, tzinfo=UTC)
    return [OrderRecord(c, limits.BOOK, P, "BUY", "entry", "FILLED", now, now, f"ex-{c}", None) for c in cids]


def test_the_channel_agrees_with_rest_on_every_sample_it_must_show(tmp_path: Path) -> None:
    msgs = [
        message("OPEN", cid="a", oid="ex-a"), message("CANCELLED", cid="a", oid="ex-a"),
        message("OPEN", cid="b", oid="ex-b"), message("EXPIRED", cid="b", oid="ex-b"),
        message("OPEN", cid="c", oid="ex-c", filled="0.00001"),
        message("FILLED", cid="c", oid="ex-c", filled="0.00003"),
        message("OPEN", cid="d", oid="ex-d", side="SELL", limit="80480.00", stop="79520.00"),
        message("FILLED", cid="d", oid="ex-d", side="SELL", limit="80480.00", stop="79520.00", filled="0.00003"),
    ]  # fmt: skip
    infos = {
        "a": info("CANCELLED", oid="ex-a", cid="a"), "b": info("EXPIRED", oid="ex-b", cid="b"),
        "c": info("FILLED", filled="0.00003", oid="ex-c", cid="c"),
        "d": info("FILLED", filled="0.00003", limit="80480.00", stop="79520.00", oid="ex-d", cid="d", side="SELL"),
    }  # fmt: skip
    check, _ = _user_channel([write_channel(tmp_path, msgs)], orders_for("a", "b", "c", "d"), infos)
    assert check["status"] == "pass", check


def test_a_field_that_differs_fails_the_channel_check(tmp_path: Path) -> None:
    msgs = [message("OPEN", cid="a", oid="ex-a"), message("CANCELLED", cid="a", oid="ex-a", limit="75000.00")]
    check, _ = _user_channel(
        [write_channel(tmp_path, msgs)], orders_for("a"), {"a": info("CANCELLED", oid="ex-a", cid="a")}
    )
    assert check["status"] == "fail" and any("limit_price" in d for d in check["differences"])


def test_missing_samples_are_not_observed_not_failed(tmp_path: Path) -> None:
    msgs = [message("OPEN", cid="a", oid="ex-a"), message("CANCELLED", cid="a", oid="ex-a")]
    check, _ = _user_channel(
        [write_channel(tmp_path, msgs)], orders_for("a"), {"a": info("CANCELLED", oid="ex-a", cid="a")}
    )
    assert check["status"] == "not_observed" and {"EXPIRED", "FILLED", "BRACKET"} <= set(check["missing_samples"])


def test_no_message_at_all_is_not_observed(tmp_path: Path) -> None:
    check, msgs = _user_channel([], orders_for("a"), {})
    assert check["status"] == "not_observed" and msgs == []


def test_clopper_pearson_matches_the_known_intervals() -> None:
    lo, hi = clopper_pearson(4, 5)
    assert lo == pytest.approx(0.2836, abs=1e-3) and hi == pytest.approx(
        0.9949, abs=1e-3
    )  # the plan quotes [0.28; 0.99]
    assert clopper_pearson(0, 5)[0] == 0.0 and clopper_pearson(5, 5)[1] == 1.0
    lo0, hi0 = clopper_pearson(0, 5)
    assert hi0 == pytest.approx(0.5218, abs=1e-3)


def test_the_child_order_of_a_triggered_stop_is_listed_with_its_floor_ratio() -> None:
    from modules.probe.report import bracket_child_fields

    bracket = orders_for("br")[0].__class__(
        "br", limits.BOOK, P, "SELL", "bracket", "FILLED",
        orders_for("br")[0].created_at, orders_for("br")[0].updated_at, "ex-br", None,
    )  # fmt: skip
    open_msg = message("OPEN", cid="br", oid="ex-br", side="SELL", limit="80480.00", stop="79520.00")
    open_msg["order_type"], open_msg["trigger_status"] = "LIMIT", "STOP_PENDING"
    child = message("OPEN", cid="br", oid="ex-br", side="SELL", limit="75544.00", stop="79520.00")
    child["order_type"], child["trigger_status"] = "STOP_LIMIT", "STOP_TRIGGERED"
    other = message("OPEN", cid="not-a-bracket", oid="ex-x")
    got = bracket_child_fields([(1.0, open_msg), (2.0, child), (3.0, child), (4.0, other)], [bracket])
    assert [g["trigger_status"] for g in got] == ["STOP_PENDING", "STOP_TRIGGERED"]  # distinct states, in order
    assert got[1]["order_type"] == "STOP_LIMIT" and got[1]["limit_over_stop"] == pytest.approx(0.95)
    assert bracket_child_fields([], [bracket]) == []
