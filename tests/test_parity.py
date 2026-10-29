"""ParityReport (spec sez. 5.1.7, 5.3 punto 14): the daily comparison of the real orders with their shadow twin.

Synthetic outcomes pin the arithmetic and the tolerances (boundaries included); ``ParityLog`` and ``TwinMirror`` are
tested against fake gateways (the twin can never hurt the real order); one end-to-end test runs two real
``ShadowExecutionGateway`` objects (one standing for the live book) on the same bars.
"""

from __future__ import annotations

import ast
import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from modules.config import FallbackFees, ParityTolerance
from modules.dryrun import parity as parity_module
from modules.dryrun.fill_model import BarFillModel, FillModelParams
from modules.dryrun.parity import LegOutcome, ParityLog, ParityPair, TwinMirror, compute_parity
from modules.dryrun.shadow_gateway import BarEvent, ShadowExecutionGateway
from modules.dryrun.virtual_account import VirtualAccount
from modules.execution.balance_guard import BalanceGuard
from modules.execution.gateway import CancelOutcome, EntryResult, ExitResult
from modules.execution.spot import SpotRegistry
from modules.ledger.positions import PositionLedger
from modules.risk.risk_manager import ExitIntent, OrderIntent
from tests.coverage_fixtures import SOL, T0, Clock, KillSwitch
from tests.gateway_fixtures import PRODUCT, FeeSourceStub
from tests.test_shadow_gateway import intent as shadow_intent

D = Decimal
DAY = date(2026, 10, 19)
AT = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def leg(
    price: str | None = "100",
    *,
    size: str = "1",
    fees: str = "0.6",
    seconds: float = 30,
    exit_reason: str | None = None,
    status: str = "filled",
    error: str | None = None,
    intent_id: str = "i1",
    at: datetime = AT,
) -> LegOutcome:
    return LegOutcome(
        intent_id, PRODUCT, at, status, at + timedelta(seconds=seconds), D(size) if price else D(0),
        None if price is None else D(price), D(fees), exit_reason, error,
    )  # fmt: skip


def pair(real: LegOutcome, sim: LegOutcome | None) -> ParityPair:
    return ParityPair(real, sim)


def report(pairs: list[ParityPair], tol: ParityTolerance | None = None):  # type: ignore[no-untyped-def]
    return compute_parity(pairs, DAY, tol)


# ------------------------------------------------------------------------------------- arithmetic


def test_an_empty_day_is_no_data_and_checks_nothing() -> None:
    r = report([])
    assert (r.status, r.n_intents, r.anomalies) == ("no_data", 0, 0)
    assert r.entry_within is None and r.bracket_within is None
    assert r.entry_delta_bps_median_abs is None and r.bracket_agreement is None and r.fill_agreement is None


def test_the_entry_delta_is_sim_minus_real_in_bps_of_the_real_price() -> None:
    r = report([pair(leg("92.03"), leg("92.04"))])
    assert r.entry_delta_bps_median == pytest.approx(0.01 / 92.03 * 1e4)
    assert r.entry_delta_bps_median_abs == pytest.approx(r.entry_delta_bps_median or 0)
    cheaper = report([pair(leg("100"), leg("99.95"))])
    assert cheaper.entry_delta_bps_median == pytest.approx(-5.0)
    assert cheaper.entry_delta_bps_median_abs == pytest.approx(5.0)


def test_the_median_of_the_absolute_delta_is_what_is_compared_not_the_signed_one() -> None:
    pairs = [
        pair(leg("100", intent_id="a"), leg("100.12", intent_id="a")),
        pair(leg("100", intent_id="b"), leg("99.88", intent_id="b")),
    ]
    r = report(pairs)
    assert r.entry_delta_bps_median == pytest.approx(0.0)  # +12 and -12 cancel: no bias...
    assert r.entry_delta_bps_median_abs == pytest.approx(12.0) and r.entry_within is False  # ...but not within 10 bps


def test_the_median_of_an_even_count_is_the_mean_of_the_middle_two_and_ignores_outliers() -> None:
    sims = ["100.01", "100.03", "100.05", "100.40"]  # 1, 3, 5, 40 bps
    r = report([pair(leg("100", intent_id=str(i)), leg(s, intent_id=str(i))) for i, s in enumerate(sims)])
    assert r.entry_delta_bps_median_abs == pytest.approx(4.0)  # (3 + 5) / 2: the 40 bps outlier does not move it
    assert r.entry_within is True


def test_the_entry_tolerance_is_inclusive_at_10_bps() -> None:
    assert report([pair(leg("100"), leg("100.10"))]).entry_within is True  # exactly 10.000 bps
    assert report([pair(leg("100"), leg("100.11"))]).entry_within is False
    custom = ParityTolerance(max_median_entry_delta_bps=5, min_bracket_outcome_agreement=0.9)
    assert report([pair(leg("100"), leg("100.10"))], custom).entry_within is False


def closed_pairs(agree: int, total: int) -> list[ParityPair]:
    out = []
    for i in range(total):
        real = leg("100", intent_id=str(i), exit_reason="TP")
        sim = leg("100", intent_id=str(i), exit_reason="TP" if i < agree else "SL")
        out.append(pair(real, sim))
    return out


def test_the_bracket_agreement_is_inclusive_at_90_percent() -> None:
    nine = report(closed_pairs(9, 10))
    assert (nine.n_closed_both, nine.n_outcome_agree, nine.bracket_agreement) == (10, 9, 0.9)
    assert nine.bracket_within is True and nine.status == "ok"
    eight = report(closed_pairs(8, 10))
    assert eight.bracket_agreement == 0.8 and eight.bracket_within is False and eight.status == "breach"
    custom = ParityTolerance(max_median_entry_delta_bps=10, min_bracket_outcome_agreement=0.95)
    assert report(closed_pairs(9, 10), custom).bracket_within is False


def test_a_trade_closed_on_one_side_only_is_pending_not_a_disagreement() -> None:
    pairs = [
        pair(leg("100", intent_id="a", exit_reason="TP"), leg("100", intent_id="a", exit_reason=None)),
        pair(leg("100", intent_id="b", exit_reason=None), leg("100", intent_id="b", exit_reason="SL")),
        pair(leg("100", intent_id="c", exit_reason="TP"), leg("100", intent_id="c", exit_reason="TP")),
    ]
    r = report(pairs)
    assert (r.n_closed_both, r.n_outcome_agree, r.outcome_pending, r.bracket_agreement) == (1, 1, 2, 1.0)


def test_open_trades_on_both_sides_are_not_counted_at_all() -> None:
    r = report([pair(leg("100"), leg("100"))])
    assert (r.n_closed_both, r.outcome_pending, r.bracket_agreement, r.bracket_within) == (0, 0, None, None)


def test_status_is_ok_with_one_check_that_ran_and_passed() -> None:
    r = report([pair(leg("100"), leg("100.02"))])
    assert (r.entry_within, r.bracket_within, r.status) == (True, None, "ok")
    only_bracket = report([pair(leg("100", exit_reason="TP"), leg("100.50", exit_reason="TP"))])
    assert only_bracket.status == "breach"  # entry 50 bps off


def test_fill_mismatches_are_counted_but_not_in_the_price_statistics() -> None:
    pairs = [
        pair(leg("100", intent_id="a"), leg(None, intent_id="a", status="no_fill")),
        pair(leg(None, intent_id="b", status="no_fill"), leg(None, intent_id="b", status="no_fill")),
        pair(leg("100", intent_id="c"), leg("100", intent_id="c")),
    ]
    r = report(pairs)
    assert r.n_intents == 3 and r.n_both_filled == 1
    assert r.fill_agreement == pytest.approx(2 / 3)
    assert r.entry_delta_bps_median_abs == 0.0


def test_fee_and_time_deltas() -> None:
    real = leg("100", size="2", fees="1.2", seconds=60)  # 60 bps
    sim = leg("100", size="2", fees="0.5", seconds=45)  # 25 bps
    r = report([pair(real, sim)])
    assert r.fee_delta_bps_median == pytest.approx(-35.0)
    assert r.entry_seconds_delta_median == pytest.approx(-15.0)


def test_a_missing_or_failed_twin_is_an_anomaly_and_never_a_price_sample() -> None:
    pairs = [
        pair(leg("100", intent_id="a"), None),
        pair(leg("100", intent_id="b"), leg(None, intent_id="b", status="error", error="ValueError: boom")),
        pair(leg("100", intent_id="c"), leg("100.05", intent_id="c")),
    ]
    r = report(pairs)
    assert (r.missing_twin, r.twin_errors, r.anomalies) == (1, 1, 2)
    assert r.n_both_filled == 1 and r.entry_delta_bps_median_abs == pytest.approx(5.0)
    assert r.fill_agreement == 1.0  # of the one intent the twin answered


def test_the_report_is_json_ready_and_written_atomically(tmp_path: Path) -> None:
    r = report([pair(leg("100", exit_reason="TP"), leg("100.04", exit_reason="TP"))])
    doc = json.loads(json.dumps(r.to_dict()))
    assert doc["date"] == "2026-10-19" and doc["status"] == "ok" and doc["anomalies"] == 0
    assert doc["tolerance"] == {"max_median_entry_delta_bps": 10.0, "min_bracket_outcome_agreement": 0.9}
    path = r.write(tmp_path / "parity")
    assert path.name == "2026-10-19.json" and json.loads(path.read_text(encoding="utf-8")) == doc
    assert [p.name for p in path.parent.iterdir()] == ["2026-10-19.json"]


# -------------------------------------------------------------------------------------- the log


def filled_result(
    price: str = "92.03", size: str = "0.024", fees: str = "0.01", bracket: str = "placed"
) -> EntryResult:
    return EntryResult("filled", "cid", filled_size=D(size), avg_price=D(price), fees=D(fees), bracket=bracket)  # type: ignore[arg-type]


def test_the_log_builds_pairs_from_events() -> None:
    log = ParityLog()
    log.submitted("i1", PRODUCT, AT)
    log.entry_done("real", "i1", filled_result("92.03"), AT + timedelta(seconds=40))
    log.entry_done("sim", "i1", filled_result("92.04"), AT + timedelta(seconds=55))
    log.trade_closed("real", PRODUCT, "TP")
    log.trade_closed("sim", PRODUCT, "TP")
    (p,) = log.pairs(DAY)
    assert p.real.entry_avg_price == D("92.03") and p.sim is not None and p.sim.entry_avg_price == D("92.04")
    assert (p.real.exit_reason, p.sim.exit_reason) == ("TP", "TP")
    assert p.real.entry_seconds == 40 and p.sim.entry_seconds == 55
    r = log.report(DAY)
    assert r.status == "ok" and r.n_closed_both == 1


def test_an_unanswered_twin_shows_as_a_missing_twin() -> None:
    log = ParityLog()
    log.submitted("i1", PRODUCT, AT)
    log.entry_done("real", "i1", filled_result(), AT)
    assert log.report(DAY).missing_twin == 1


def test_a_filled_entry_without_a_bracket_to_place_ends_as_dust_on_both_sides() -> None:
    log = ParityLog()
    log.submitted("i1", PRODUCT, AT)
    for side in ("real", "sim"):
        log.entry_done(side, "i1", filled_result(size="0.001", bracket="dust"), AT)  # type: ignore[arg-type]
    r = log.report(DAY)
    assert (r.n_closed_both, r.n_outcome_agree) == (1, 1)


def test_events_for_unknown_intents_or_trades_are_counted_not_raised() -> None:
    log = ParityLog()
    log.entry_done("real", "ghost", filled_result(), AT)
    log.entry_failed("sim", "ghost", "x", AT)
    log.trade_closed("real", PRODUCT, "TP")
    assert log.unmatched_events == 3
    log.submitted("i1", PRODUCT, AT)
    with pytest.raises(ValueError, match="already"):
        log.submitted("i1", PRODUCT, AT)


def test_a_close_goes_to_the_trade_of_its_product_on_its_side_only() -> None:
    log = ParityLog()
    log.submitted("i1", PRODUCT, AT)
    log.entry_done("real", "i1", filled_result(), AT)
    log.entry_done("sim", "i1", filled_result(), AT)
    log.trade_closed("sim", PRODUCT, "SL")
    (p,) = log.pairs(DAY)
    assert p.sim is not None and p.sim.exit_reason == "SL" and p.real.exit_reason is None


def test_pairs_belong_to_the_utc_day_of_the_submission() -> None:
    log = ParityLog()
    late = datetime(2026, 10, 19, 23, 59, 59, tzinfo=UTC)
    log.submitted("a", PRODUCT, late)
    log.submitted("b", PRODUCT, late + timedelta(seconds=1))
    assert [p.real.intent_id for p in log.pairs(DAY)] == ["a"]
    assert [p.real.intent_id for p in log.pairs(DAY + timedelta(days=1))] == ["b"]


def test_prune_keeps_what_is_still_open() -> None:
    log = ParityLog()
    for i in ("old_open", "old_closed", "new"):
        log.submitted(i, f"P-{i}", AT - timedelta(days=3) if i.startswith("old") else AT)
    log.entry_done("real", "old_open", filled_result(), AT)
    log.entry_done("real", "old_closed", filled_result(), AT)
    log.trade_closed("real", "P-old_closed", "TP")
    assert log.prune(DAY) == 1
    assert {p.real.intent_id for d in (DAY, DAY - timedelta(days=3)) for p in log.pairs(d)} == {"old_open", "new"}


# ---------------------------------------------------------------------------------- the mirror


class FakeGateway:
    def __init__(self, book: str) -> None:
        self.book = book
        self.entries: list[OrderIntent] = []
        self.exits: list[ExitIntent] = []
        self.emergencies: list[str] = []
        self.cancels: list[str] = []
        self.gate: asyncio.Event | None = None
        self.fail: Exception | None = None
        self.entry = filled_result()
        self.exit = ExitResult("closed", "SELL", size=D("0.024"), avg_price=D("92"))

    async def _go(self) -> None:
        if self.gate is not None:
            await self.gate.wait()
        if self.fail is not None:
            raise self.fail

    async def submit_entry_with_bracket(self, intent: OrderIntent) -> EntryResult:
        self.entries.append(intent)
        await self._go()
        return self.entry

    async def submit_exit(self, intent: ExitIntent) -> ExitResult:
        self.exits.append(intent)
        await self._go()
        return self.exit

    async def emergency_exit(self, product_id: str) -> ExitResult:
        self.emergencies.append(product_id)
        await self._go()
        return self.exit

    async def cancel(self, product_id: str) -> CancelOutcome:
        self.cancels.append(product_id)
        await self._go()
        return CancelOutcome("done")


def mirror() -> tuple[TwinMirror, FakeGateway, FakeGateway, ParityLog, list[datetime]]:
    clock_now = [AT]
    real, twin, log = FakeGateway("champion"), FakeGateway("shadow_twin"), ParityLog()
    return TwinMirror(real, twin, "shadow_twin", log, lambda: clock_now[0]), real, twin, log, clock_now  # type: ignore[arg-type]


def exit_intent() -> ExitIntent:
    return ExitIntent(
        intent_id="x1", decision_id="d2", product_id=PRODUCT, book="champion", base_size=D("0.024"),
        cancel_bracket_order_id="br", reason="SELL", created_at=AT,
    )  # fmt: skip


async def test_the_twin_gets_the_same_intent_readdressed_to_its_book() -> None:
    m, real, twin, log, _ = mirror()
    intent = shadow_intent()
    result = await m.submit_entry_with_bracket(intent)
    await m.drain()
    assert result is real.entry and real.entries == [intent]
    (sent,) = twin.entries
    assert sent.book == "shadow_twin" and sent.model_copy(update={"book": intent.book}) == intent
    (p,) = log.pairs(DAY)
    assert p.sim is not None and p.real.status == p.sim.status == "filled"


async def test_a_twin_that_raises_cannot_hurt_the_real_order() -> None:
    m, real, twin, log, _ = mirror()
    twin.fail = RuntimeError("twin ledger is down")
    result = await m.submit_entry_with_bracket(shadow_intent())
    await m.drain()
    assert result is real.entry
    r = log.report(DAY)
    assert (r.twin_errors, r.missing_twin, r.anomalies) == (1, 0, 1)


async def test_a_slow_twin_does_not_delay_the_real_answer() -> None:
    m, _real, twin, log, _ = mirror()
    twin.gate = asyncio.Event()  # the twin waits for bars that have not come yet
    result = await asyncio.wait_for(m.submit_entry_with_bracket(shadow_intent()), timeout=2)
    assert result.status == "filled"
    assert log.report(DAY).missing_twin == 1  # not answered yet
    twin.gate.set()
    await m.drain()
    assert log.report(DAY).missing_twin == 0


async def test_the_real_gateway_starts_first_and_its_failure_propagates() -> None:
    m, real, twin, log, _ = mirror()
    order: list[str] = []
    orig_real, orig_twin = real.submit_entry_with_bracket, twin.submit_entry_with_bracket

    async def real_first(i: OrderIntent) -> EntryResult:
        order.append("real")
        return await orig_real(i)

    async def twin_second(i: OrderIntent) -> EntryResult:
        order.append("twin")
        return await orig_twin(i)

    real.submit_entry_with_bracket = real_first  # type: ignore[method-assign]
    twin.submit_entry_with_bracket = twin_second  # type: ignore[method-assign]
    await m.submit_entry_with_bracket(shadow_intent())
    await m.drain()
    assert order == ["real", "twin"]

    real.fail = ConnectionError("exchange unreachable")
    with pytest.raises(ConnectionError):
        await m.submit_entry_with_bracket(shadow_intent("dec-2"))
    await m.drain()
    (_, second) = log.pairs(DAY)
    assert second.real.status == "error" and second.real.error is not None and "ConnectionError" in second.real.error


async def test_exits_emergency_exits_and_cancels_reach_both_gateways() -> None:
    m, real, twin, log, _ = mirror()
    await m.submit_entry_with_bracket(shadow_intent())
    await m.drain()
    assert (await m.submit_exit(exit_intent())).status == "closed"
    await m.drain()
    assert real.exits[0].book == "champion" and twin.exits[0].book == "shadow_twin"
    assert twin.exits[0].model_copy(update={"book": "champion"}) == real.exits[0]
    (p,) = log.pairs(DAY)
    assert p.sim is not None and (p.real.exit_reason, p.sim.exit_reason) == ("SELL", "SELL")
    await m.emergency_exit(PRODUCT)
    await m.cancel(PRODUCT)
    await m.drain()
    assert real.emergencies == twin.emergencies == [PRODUCT] and real.cancels == twin.cancels == [PRODUCT]


async def test_a_failing_twin_exit_is_swallowed() -> None:
    m, real, twin, _log, _ = mirror()
    twin.fail = RuntimeError("boom")
    assert (await m.submit_exit(exit_intent())).status == "closed"
    assert (await m.emergency_exit(PRODUCT)).status == "closed"
    assert (await m.cancel(PRODUCT)).status == "done"
    await m.drain()
    assert len(real.exits) == 1 and len(twin.exits) == 1


async def test_brackets_that_close_on_their_own_are_reported_by_the_wiring() -> None:
    m, _real, _twin, log, _ = mirror()
    await m.submit_entry_with_bracket(shadow_intent())
    await m.drain()
    m.on_real_closed(PRODUCT, "TP")
    m.on_sim_closed(PRODUCT, "SL")
    r = log.report(DAY)
    assert (r.n_closed_both, r.n_outcome_agree, r.bracket_agreement, r.status) == (1, 0, 0.0, "breach")


# ----------------------------------------------------------------------- two real shadow gateways


async def twin_pair(real_params: FillModelParams, twin_params: FillModelParams) -> Any:
    """Two gateways with a ledger each (live and dry never share one: the order ids are the same)."""
    clock = Clock()
    ledgers = {book: await PositionLedger.open(":memory:", "dry") for book in ("champion", "shadow_twin")}
    registry = SpotRegistry({PRODUCT: SOL})
    fees = FeeSourceStub(clock)
    gateways = {}
    for book, ledger in ledgers.items():
        account = VirtualAccount(book, D("100"), clock)
        guard = BalanceGuard(account.balances, ledger.book_view(book), registry, fees, FallbackFees(), KillSwitch())
        model = BarFillModel(real_params if book == "champion" else twin_params)
        gateways[book] = ShadowExecutionGateway(book, account, model, ledger, clock, guard, registry, fees)
    return list(ledgers.values()), gateways, clock


def bar(minute: float, o: float = 92.1, h: float = 92.2, lo: float = 92.0, c: float = 92.1) -> BarEvent:
    return BarEvent(PRODUCT, T0 + timedelta(minutes=minute), o, h, lo, c, 1000.0, 2.0)


async def test_two_shadow_gateways_on_the_same_bars_produce_a_report() -> None:
    """The real book fills at the limit with the default haircut; the twin has a tiny haircut and fills less."""
    ledgers, gws, clock = await twin_pair(FillModelParams(), FillModelParams(queue_haircut=0.00001))
    try:
        log = ParityLog()
        m = TwinMirror(gws["champion"], gws["shadow_twin"], "shadow_twin", log, clock)
        task = asyncio.create_task(m.submit_entry_with_bracket(shadow_intent()))
        for _ in range(1000):
            if gws["champion"].has_pending_entry(PRODUCT) and gws["shadow_twin"].has_pending_entry(PRODUCT):
                break
            await asyncio.sleep(0.002)
        else:
            pytest.fail("the entry never reached both gateways")
        for g in gws.values():
            await g.on_bar(bar(1))
        real = await asyncio.wait_for(task, 20)
        assert real.status == "filled" and real.filled_size == D("0.023")
        await gws["shadow_twin"].on_bar(bar(40))  # the twin's tiny queue share only partly filled: it times out
        await asyncio.wait_for(m.drain(), 20)
        (p,) = log.pairs(T0.date())
        assert p.sim is not None and p.sim.status == "filled" and p.sim.filled_size == D("0.01")
        assert p.real.entry_avg_price == D("92.03") == p.sim.entry_avg_price
        r = log.report(T0.date())
        assert (r.n_intents, r.n_both_filled, r.anomalies) == (1, 1, 0)
        assert r.entry_delta_bps_median_abs == 0.0 and r.status == "ok"
    finally:
        for ledger in ledgers:
            await ledger.close()


# ------------------------------------------------------------------------------- structure


def test_the_module_cannot_send_orders() -> None:
    tree = ast.parse(Path(parity_module.__file__).read_text(encoding="utf-8"))
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
    }
    assert not names & {"create_order", "cancel_orders", "preview_order", "authorize", "CoverageTicket", "RESTClient"}
    imported = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not {m for m in imported if m.startswith("coinbase") or m.endswith("exchange")}
