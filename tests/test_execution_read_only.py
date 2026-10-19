"""Proof that the user stream and the reconciler cannot create orders, and that the watchdog only asks.

Three independent lines, so that removing one still leaves the claim standing:

1. static: the sources never name an order-capable method, a ticket, an order body or the guard, and import from the
   exchange / guard modules nothing but plain data types and Protocols;
2. typing: the collaborator Protocols of these modules declare reads only (plus the single injected callback of the
   reconciler and the two exit calls of the watchdog);
3. runtime: a complete restart and session run against ``FakeExchange``, whose every non-read attribute raises, and
   the calls it recorded are all reads.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from modules.execution import reconciler, user_stream, watchdog
from modules.ledger.positions import PositionLedger
from tests.execution_fakes import READS, B, P, World, bracket_spec, buy_spec, fill, info, new_world_parts
from tests.test_reconciler import RecordingPlacer, build

ROOT = Path(__file__).resolve().parent.parent
FILES = {
    "user_stream": ROOT / "modules" / "execution" / "user_stream.py",
    "reconciler": ROOT / "modules" / "execution" / "reconciler.py",
    "watchdog": ROOT / "modules" / "execution" / "watchdog.py",
}
# Names that mean "send, change or cancel an order" or "get the right to": none may appear in these modules.
FORBIDDEN = {
    "create_order", "cancel_orders", "preview_order", "edit_order", "place_order", "build_order_body",
    "CoverageTicket", "BalanceGuard", "authorize", "consume", "_reserve", "OrderSpec", "CoinbaseExchange",
    "LiveExecutionGateway", "ShadowExecutionGateway", "ExecutionGateway", "GatewayContract",
}  # fmt: skip
# What each module may import from the modules that sit next to the exchange (data types and Protocols only).
ALLOWED_IMPORTS = {
    "modules.execution.exchange": {"FillInfo", "OrderInfo", "BestBidAsk"},
    "modules.execution.balance_guard": {"FeeSource"},
    "modules.execution.orders": set(),
    "modules.execution.spot": {"SpotRegistry"},
    "modules.risk.risk_manager": {"ExitIntent"},
}


def tree_of(name: str) -> ast.Module:
    return ast.parse(FILES[name].read_text(encoding="utf-8"))


def identifiers(tree: ast.AST) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute):
            out.add(n.attr)
        elif isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            out.add(n.name)
        elif isinstance(n, ast.arg):
            out.add(n.arg)
        elif isinstance(n, ast.alias):
            out.add((n.asname or n.name).split(".")[-1])
        elif isinstance(n, ast.keyword) and n.arg:
            out.add(n.arg)
    return out


@pytest.mark.parametrize("name", sorted(FILES))
def test_no_order_capable_name_appears_in_the_source(name: str) -> None:
    hits = identifiers(tree_of(name)) & FORBIDDEN
    assert not hits, f"{name}.py names {sorted(hits)}: it must not be able to send or cancel an order"


@pytest.mark.parametrize("name", sorted(FILES))
def test_the_imports_next_to_the_exchange_are_data_types_and_protocols_only(name: str) -> None:
    for node in ast.walk(tree_of(name)):
        if not isinstance(node, ast.ImportFrom) or node.module is None:
            continue
        allowed = ALLOWED_IMPORTS.get(node.module)
        if allowed is not None:
            names = {a.name for a in node.names}
            assert names <= allowed, f"{name}.py imports {sorted(names - allowed)} from {node.module}"
        assert not node.module.endswith(("gateway", "shadow_gateway", "engine", "app")), (name, node.module)


def sdk_imports(name: str) -> list[ast.ImportFrom | ast.Import]:
    found: list[ast.ImportFrom | ast.Import] = []
    for n in ast.walk(tree_of(name)):
        if isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] == "coinbase":
            found.append(n)
        elif isinstance(n, ast.Import) and any(a.name.split(".")[0] == "coinbase" for a in n.names):
            found.append(n)
    return found


def test_only_the_user_stream_touches_the_sdk_and_only_for_the_websocket() -> None:
    assert sdk_imports("reconciler") == [] and sdk_imports("watchdog") == []
    (only,) = sdk_imports("user_stream")
    assert isinstance(only, ast.ImportFrom) and only.module == "coinbase.websocket"
    assert [a.name for a in only.names] == ["WSUserClient"]  # the authenticated WebSocket client, nothing REST


def protocol_methods(cls: type) -> set[str]:
    return {n for n, _ in inspect.getmembers(cls, inspect.isfunction) if not n.startswith("_")}


def test_the_exchange_views_of_the_stream_and_the_reconciler_declare_reads_only() -> None:
    assert protocol_methods(user_stream.FillSource) == {"get_fills"}
    assert protocol_methods(reconciler.ReconcileExchange) == {
        "get_accounts", "list_open_orders", "list_orders", "get_best_bid_ask",
    }  # fmt: skip
    assert protocol_methods(reconciler.ReconcileExchange) | protocol_methods(user_stream.FillSource) <= READS
    assert protocol_methods(user_stream.UserTransport) == {"open", "close", "subscribe"}  # no order messages


def test_the_watchdog_can_only_ask_for_an_exit() -> None:
    assert protocol_methods(watchdog.ExitGateway) == {"emergency_exit", "submit_exit"}
    assert protocol_methods(reconciler.BracketPlacer) == {"place_missing_bracket"}


def test_the_websocket_shell_only_ever_opens_closes_and_subscribes() -> None:
    calls = {
        n.func.attr
        for n in ast.walk(tree_of("user_stream"))
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Attribute)
        and n.func.value.attr == "_ws"
    }
    assert calls == {"open", "close", "subscribe"}


@pytest.fixture
async def world() -> AsyncIterator[World]:
    ledger = await PositionLedger.open(":memory:", "live")
    yield new_world_parts(ledger)
    await ledger.close()


async def test_a_whole_restart_and_a_whole_session_only_read(world: World) -> None:
    """Orphans, unknown outcomes, duplicate fills, a bracket to repair, a gap: every call made was a read."""
    await world.record(buy_spec("b1"), leg="entry", status="NEW")  # unknown outcome, found open
    await world.record(buy_spec("b2"), leg="entry", status="NEW")  # unknown outcome, never existed
    await world.open_position()
    await world.record(bracket_spec("br0"), leg="bracket", status="OPEN", exchange_order_id="ex-br0")
    await world.ledger.set_bracket(B, P, "br0", None, None)
    world.exchange.open = [info("b1"), info("manual", side="SELL", base="1", limit="150")]
    world.exchange.history = [info("br0", side="SELL", status="CANCELLED", base="0.024", limit="108.96", stop="83.57")]
    world.exchange.balances = {"EUR": ("997.79128", "2.20872"), "SOL": ("1.024", "1")}
    placer = RecordingPlacer()
    rec, _, _ = build(world, placer=placer)
    await rec.on_startup()
    for _ in range(3):
        await rec.reconcile_once("sequence_gap")
    stream = user_stream.CoinbaseUserStream(
        [P], world.sink, transport_factory=lambda *_: None,  # type: ignore[arg-type, return-value]
        request_reconcile=rec.request,
    )  # fmt: skip
    world.exchange.fills["ex-b1"] = [fill("t1", "b1", size="0.024", fee="0.02")]
    order = (
        '{"order_id":"ex-b1","client_order_id":"b1","product_id":"SOL-EUR","order_side":"BUY","status":"FILLED",'
        '"cumulative_quantity":"0.024","leaves_quantity":"0","limit_price":"92.03","stop_price":"0"}'
    )
    await stream.handle_raw(
        '{"channel":"user","timestamp":"2026-10-17T12:00:00Z","sequence_num":1,'
        '"events":[{"type":"update","orders":[' + order + "]}]}"
    )
    assert world.exchange.calls and set(world.exchange.calls) <= READS
    assert placer.requests == []  # b1's entry is still open, so no bracket was due; the only way out is the placer
    assert await world.sink.local_order("manual") is None  # the foreign order was never touched or recorded


async def test_the_fake_exchange_really_fails_on_a_write(world: World) -> None:
    """The instrument of the proof above must itself be sharp."""
    for write in ("create_order", "cancel_orders", "preview_order"):
        with pytest.raises(AssertionError, match="may only read"):
            getattr(world.exchange, write)
