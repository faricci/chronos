"""More pins on the app wiring: the exit reason reaches the ledger, the ops see the maker fills and the last sample, a
restart keeps the live brackets alive in the ledger."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from modules.app import _ObservedGateway
from modules.execution.exchange import DryRunViolation
from modules.execution.gateway import ExitResult
from modules.ledger.positions import PositionLedger
from modules.risk.risk_manager import ExitIntent
from tests.app_fixtures import make_rig
from tests.test_app_books import _open_run, cfg_with, feed_bars
from tests.test_decision_engine import WARM_A

D = Decimal
NOW = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)


class _Inner:
    def __init__(self, ledger: PositionLedger, fail: BaseException | None = None) -> None:
        self.ledger = ledger
        self.fail = fail
        self.seen: list[str | None] = []

    async def submit_exit(self, intent: ExitIntent) -> ExitResult:
        self.seen.append(self.ledger._exit_hints.get((intent.book, intent.product_id)))  # noqa: SLF001
        if self.fail is not None:
            raise self.fail
        return ExitResult("closed", intent.reason)

    async def emergency_exit(self, product_id: str) -> ExitResult:
        self.seen.append(self.ledger._exit_hints.get(("champion", product_id)))  # noqa: SLF001
        return ExitResult("closed", "WATCHDOG")


def _intent(reason: str) -> ExitIntent:
    return ExitIntent(
        intent_id="i", decision_id="d", product_id="SOL-EUR", book="champion", base_size=D(1),
        cancel_bracket_order_id=None, reason=reason, created_at=NOW,
    )  # fmt: skip


async def test_the_observed_gateway_announces_the_reason_of_an_exit_to_the_ledger_and_withdraws_it(
    tmp_path: Path,
) -> None:
    ledger = await PositionLedger.open(tmp_path / "dry.db", "dry")
    fatal: list[BaseException] = []
    inner = _Inner(ledger)
    gateway = _ObservedGateway(inner, ledger, "champion", fatal.append)  # type: ignore[arg-type]
    await gateway.submit_exit(_intent("HORIZON"))
    await gateway.emergency_exit("SOL-EUR")
    assert inner.seen == ["HORIZON", "WATCHDOG"]  # the ledger knew the reason while the order was in flight
    assert ledger._exit_hints == {}  # noqa: SLF001  (and nothing is left for the next trade)
    inner.fail = DryRunViolation("create_order")
    with pytest.raises(DryRunViolation):
        await gateway.submit_exit(_intent("SELL"))
    assert len(fatal) == 1 and ledger._exit_hints == {}  # noqa: SLF001
    await ledger.close()


async def test_ops_see_the_maker_fills_and_the_last_sample_before_the_stop(tmp_path: Path) -> None:
    cfg = cfg_with(tmp_path, "champion", "random_entry")
    async with make_rig(tmp_path, cfg=cfg) as rig:
        await feed_bars(rig, range(WARM_A, WARM_A + 12))
        assert rig.rt.ops is not None and rig.rt.ops.today()["maker_fill_rate"] is not None
        rig.rt.kill_switch._accounts["champion"].trip_coverage_breach("test")  # noqa: SLF001
        rig.rt.request_stop("test")
        await rig.rt._shutdown()  # noqa: SLF001  (the loop never sampled: the stop must)
    day = rig.clock.now.date().isoformat()
    doc = json.loads((tmp_path / "data" / "reports" / "ops" / f"{day}.json").read_text(encoding="utf-8"))
    trips = [(t["book"], t["guard"], t["bug"]) for t in doc["kill_switch_trips"] if t["book"] != "*"]
    assert trips == [("champion", "coverage_breach", True)]
    assert doc["maker_fill_rate"] is not None and doc["running"] is False


async def test_after_a_restart_the_live_brackets_are_still_open_orders_in_the_ledger(tmp_path: Path) -> None:
    async with await _open_run(tmp_path) as first:
        await feed_bars(first, range(WARM_A, WARM_A + 12))
        first.rt.request_stop("test")
        await first.rt._shutdown()  # noqa: SLF001
    async with await _open_run(tmp_path, upto=WARM_A + 12) as second:
        checked = 0
        for book in second.rt.books:
            open_ids = {o.client_order_id for o in await second.rt.ledger.open_orders(book.name)}
            for pos in second.rt.ledger.long_positions(book.name):
                assert pos.bracket_order_id in open_ids  # restored, not cancelled and re-created
                checked += 1
        assert checked >= 3


def test_the_helpers_are_importable() -> None:
    assert Any
