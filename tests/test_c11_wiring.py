"""Tripwires that keep the spot-coverage circuit (vincolo C11) wired in as the system grows.

The guard itself is proven in test_balance_guard.py / test_coverage_properties.py. This file proves nobody can
bypass it, and it turns every "to do later" of the execution layer into a failing test the moment the file
that needs it appears:

1. static rules, always on: only a few files may talk to Coinbase, nothing may use a leveraged/derivative API,
   the v1 order executor stays disabled;
2. tripwires: as soon as modules/execution/exchange.py, gateway.py, ... exist, they must be wired to the
   BalanceGuard / CoverageTicket, and the tests the architect asked for (T4, T11-T15, T17, T18) must exist.

Until a file exists its tripwire is reported as a skip (``pytest -rs`` lists what is still owed).
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.wiring_helpers import defined_test_names, skip_until

ROOT = Path(__file__).resolve().parent.parent
MODULES = ROOT / "modules"
TESTS = ROOT / "tests"

V1_FILES = {
    "data_fetch.py", "data_preprocessing.py", "monitoring.py", "order_execution.py",
    "sentiment_analysis.py", "trading_logic.py", "utils.py", "vector_storage.py",
}  # fmt: skip

# The only v2 modules allowed to import the Coinbase SDK. Orders can only leave the process from exchange.py.
COINBASE_IMPORTERS = {
    "modules/execution/exchange.py",
    "modules/execution/user_stream.py",
    "modules/market/ws_feed.py",
    "modules/market/backfill.py",  # public candles only
}
# Order/transfer capable SDK entry points: forbidden everywhere except exchange.py
ORDER_METHOD = re.compile(
    r"^(create_order|place_order|edit_order|cancel_orders|preview_order|post|put|delete"
    r"|(market|limit|stop_limit|trigger_bracket)_order.*|.*_order_(buy|sell))$"
)
# Never mentioned as an identifier anywhere in v2: derivatives, margin, leverage, fund movement
FORBIDDEN_TOKENS = {"futures", "perp", "perps", "perpetual", "perpetuals", "intx", "cfm", "margin", "leverage"}
FORBIDDEN_IDENTIFIERS = {"portfolio_margin", "move_portfolio_funds", "allocate_portfolio"}

# Allowlists from the architect's resolution (docs/decisions/2026-10-17-copertura-spot.md, Q5.4 and T11)
CLIENT_METHODS = {
    "get_product", "get_transaction_summary", "get_best_bid_ask", "create_order", "cancel_orders", "list_orders",
    "get_order", "get_fills", "get_accounts", "get_api_key_permissions", "preview_order", "get_public_candles",
    "get_candles",
}  # fmt: skip
WRAPPER_PUBLIC = {
    "get_product", "get_product_raw", "get_fee_rates", "get_best_bid_ask", "create_order", "cancel_orders",
    "list_open_orders", "list_orders", "get_order", "get_fills", "get_accounts", "get_api_key_permissions",
    "preview_order", "get_candles", "get_public_candles",
}  # fmt: skip


def v2_files() -> Iterator[Path]:
    for p in sorted(MODULES.rglob("*.py")):
        if p.parent == MODULES and p.name in V1_FILES:
            continue
        yield p


def rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


def parse(p: Path) -> ast.Module:
    return ast.parse(p.read_text(encoding="utf-8"), filename=str(p))


def identifiers(tree: ast.AST) -> Iterator[str]:
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute):
            yield n.attr
        elif isinstance(n, ast.Name):
            yield n.id
        elif isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            yield n.name
        elif isinstance(n, ast.arg):
            yield n.arg
        elif isinstance(n, ast.alias):
            yield (n.asname or n.name).split(".")[-1]
        elif isinstance(n, ast.keyword) and n.arg:
            yield n.arg


def tokens(identifier: str) -> set[str]:
    snake = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", identifier).lower()
    return {t for t in snake.split("_") if t}


def imports_coinbase(tree: ast.AST) -> bool:
    for n in ast.walk(tree):
        if isinstance(n, ast.Import) and any(a.name.split(".")[0] == "coinbase" for a in n.names):
            return True
        if isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] == "coinbase":
            return True
    return False


def test_the_scan_sees_the_v2_code() -> None:
    names = {rel(p) for p in v2_files()}
    assert {"modules/config.py", "modules/execution/balance_guard.py", "modules/execution/spot.py"} <= names
    assert "modules/order_execution.py" not in names


# --------------------------------------------------------------------------- 1. static, always on


def test_no_derivative_margin_or_fund_movement_identifier_exists() -> None:
    hits = []
    for p in v2_files():
        for ident in set(identifiers(parse(p))):
            if tokens(ident) & FORBIDDEN_TOKENS or ident in FORBIDDEN_IDENTIFIERS:
                hits.append(f"{rel(p)}: {ident}")
    assert not hits, "leverage/derivatives/fund-movement APIs are forbidden (C11):\n" + "\n".join(hits)


def test_only_a_few_files_may_import_the_coinbase_sdk() -> None:
    importers = {rel(p) for p in v2_files() if imports_coinbase(parse(p))}
    assert importers <= COINBASE_IMPORTERS, (
        f"unexpected Coinbase SDK importers: {sorted(importers - COINBASE_IMPORTERS)}"
    )


def test_order_capable_sdk_methods_appear_only_in_exchange_py() -> None:
    hits = []
    for p in v2_files():
        if rel(p) == "modules/execution/exchange.py":
            continue
        tree = parse(p)
        if not imports_coinbase(tree):
            continue  # files that never touch the SDK may call exchange.create_order(ticket): that is the point
        hits += [
            f"{rel(p)}: .{a.attr}"
            for a in ast.walk(tree)
            if isinstance(a, ast.Attribute) and ORDER_METHOD.match(a.attr)
        ]
    assert not hits, "only exchange.py may call order endpoints:\n" + "\n".join(hits)


def test_only_exchange_py_builds_an_order_body() -> None:
    allowed = {"modules/execution/spot.py", "modules/execution/exchange.py"}
    hits = [rel(p) for p in v2_files() if rel(p) not in allowed and "build_order_body" in set(identifiers(parse(p)))]
    assert not hits, f"order bodies are built only from a CoverageTicket in exchange.py: {hits}"


def test_only_the_guard_makes_tickets_and_reserves() -> None:
    reserve_allowed = {"modules/execution/balance_guard.py", "modules/execution/balance.py"}
    ticket_allowed = {"modules/execution/balance_guard.py"}
    bad_reserve = [
        rel(p) for p in v2_files() if rel(p) not in reserve_allowed and "_reserve" in set(identifiers(parse(p)))
    ]
    bad_ticket = [
        rel(p)
        for p in v2_files()
        if rel(p) not in ticket_allowed
        and "CoverageTicket" in set(identifiers(parse(p)))
        and rel(p) not in {"modules/execution/exchange.py"}
    ]
    assert not bad_reserve, f"BalanceBook._reserve is private to the guard: {bad_reserve}"
    assert not bad_ticket, f"CoverageTicket may only be received (exchange.py) or issued (guard): {bad_ticket}"


def test_v1_order_executor_is_disabled_and_not_imported_by_v2() -> None:
    if (MODULES / "order_execution.py").exists():  # deleted once the v2 gateway exists (test below)
        v1 = parse(MODULES / "order_execution.py")
        assert any(isinstance(n, ast.Raise) for n in v1.body), "modules/order_execution.py must raise at import (C11)"
    importers = [
        rel(p)
        for p in v2_files()
        if any(
            (isinstance(n, ast.ImportFrom) and (n.module or "").endswith("order_execution"))
            or (isinstance(n, ast.Import) and any(a.name.endswith("order_execution") for a in n.names))
            for n in ast.walk(parse(p))
        )
    ]
    assert not importers, f"v2 modules must not import the v1 executor: {importers}"


def test_v1_order_executor_is_deleted_once_the_v2_gateway_exists() -> None:
    if (MODULES / "execution" / "gateway.py").exists():
        assert not (MODULES / "order_execution.py").exists()


# ------------------------------------------------------------------------------ 2. tripwires


def find_class(tree: ast.Module, name: str) -> ast.ClassDef | None:
    return next((n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == name), None)


def init_params(cls: ast.ClassDef) -> list[ast.arg]:
    for n in cls.body:
        if isinstance(n, ast.FunctionDef) and n.name == "__init__":
            a = n.args
            return [*a.posonlyargs, *a.args, *a.kwonlyargs]
    return []


def mandatory(cls: ast.ClassDef, param: str) -> bool:
    for n in cls.body:
        if isinstance(n, ast.FunctionDef) and n.name == "__init__":
            a = n.args
            positional = [*a.posonlyargs, *a.args]
            defaults = {x.arg for x in positional[len(positional) - len(a.defaults) :]}
            kw_defaults = {x.arg for x, d in zip(a.kwonlyargs, a.kw_defaults, strict=True) if d is not None}
            return param in {x.arg for x in positional + a.kwonlyargs} and param not in defaults | kw_defaults
    return False


def annotation_text(a: ast.arg) -> str:
    return ast.unparse(a.annotation) if a.annotation else ""


def test_exchange_only_sends_orders_from_a_ticket() -> None:
    tree = parse(skip_until("modules/execution/exchange.py"))
    cls = find_class(tree, "CoinbaseExchange")
    assert cls is not None, "exchange.py must define CoinbaseExchange"
    methods = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)}
    create = methods.get("create_order")
    assert create is not None, "CoinbaseExchange.create_order is missing"
    ticket_params = [a for a in create.args.args if "CoverageTicket" in annotation_text(a)]
    assert ticket_params, "create_order must take a CoverageTicket"
    calls = {n.func.attr for n in ast.walk(create) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    called = {n.func.id for n in ast.walk(create) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "consume" in calls, "create_order must consume the ticket (single use)"
    assert "build_order_body" in called | calls, "the body must come from build_order_body"
    for name, fn in methods.items():
        if name == "create_order":
            continue
        assert "body" not in {a.arg for a in fn.args.args}, f"{name}: no other method may accept an order body"


def test_exchange_methods_are_allowlisted() -> None:  # T11 (runs once exchange.py exists)
    tree = parse(skip_until("modules/execution/exchange.py"))
    cls = find_class(tree, "CoinbaseExchange")
    assert cls is not None
    public = {
        n.name for n in cls.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and not n.name.startswith("_")
    }
    assert public <= WRAPPER_PUBLIC, f"public methods outside the allowlist: {sorted(public - WRAPPER_PUBLIC)}"
    sdk_calls = {
        n.attr for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Attribute) and n.value.attr == "_client"
    }  # fmt: skip
    assert sdk_calls <= CLIENT_METHODS, f"SDK methods outside the allowlist: {sorted(sdk_calls - CLIENT_METHODS)}"


@pytest.mark.parametrize("path", ["modules/execution/gateway.py", "modules/dryrun/shadow_gateway.py"])
def test_gateways_cannot_exist_without_a_guard(path: str) -> None:  # T18
    tree = parse(skip_until(path))
    gateways = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name.endswith("ExecutionGateway")]
    assert gateways, f"{path} must define a *ExecutionGateway class"
    for cls in gateways:
        assert mandatory(cls, "guard"), f"{cls.name}.__init__ needs a mandatory `guard` parameter"
        param = next(a for a in init_params(cls) if a.arg == "guard")
        text = annotation_text(param)
        assert "BalanceGuard" in text and "None" not in text, f"{cls.name}: guard must be a non-Optional BalanceGuard"
        assert any(isinstance(n, ast.Attribute) and n.attr == "authorize" for n in ast.walk(cls)), (
            f"{cls.name} never calls guard.authorize()"
        )


# file that creates the obligation -> (source tokens it must mention, tests that must exist)
OBLIGATIONS: dict[str, tuple[set[str], set[str]]] = {
    "modules/risk/sizing.py": ({"max_base_for_quote", "coverage_fee_rate"}, {"test_sizing_never_refused_by_guard"}),
    "modules/dryrun/virtual_account.py": ({"BalanceBook"}, {"test_live_and_dry_guard_parity"}),
    "modules/execution/gateway.py": (
        {"authorize", "dust"},
        {
            "test_partial_fill_timeout_cancels_before_bracket",  # T13
            "test_dust_merges_into_next_entry",  # T14
            "test_exit_waits_for_cancel_confirmation",  # T15
            "test_create_order_requires_a_fresh_ticket",  # T12
        },
    ),
    "modules/dryrun/shadow_gateway.py": ({"authorize"}, {"test_live_and_dry_guard_parity"}),
    "modules/dryrun/books.py": ({"max_base_for_quote"}, set()),
    "modules/risk/kill_switch.py": (
        {"coverage_breach_active", "coverage_breach"},
        {"test_coverage_breach_blocks_buys"},
    ),
    "modules/execution/balance_feed.py": (
        {"apply_snapshot", "epoch"},
        {"test_snapshot_read_during_local_event_is_discarded"},
    ),
    "modules/execution/reconciler.py": (
        {"restore_reservation", "apply_snapshot"},
        {"test_startup_restores_open_order_reservations"},
    ),
    "modules/execution/watchdog.py": ({"authorize"}, {"test_watchdog_without_bracket_triggers_at_sl"}),
    "modules/ledger/positions.py": ({"dust_qty", "dust_cost"}, set()),
    "modules/app.py": (
        {"SpotRegistry", "SpotViolation", "SPOT_EXIT_CODE", "assert_live_key_permissions"},
        set(),
    ),
}


@pytest.mark.parametrize("path", sorted(OBLIGATIONS))
def test_new_execution_files_are_wired_to_the_coverage_circuit(path: str) -> None:
    source = skip_until(path).read_text(encoding="utf-8")
    needs_tokens, needs_tests = OBLIGATIONS[path]
    missing_tokens = {t for t in needs_tokens if t not in source}
    assert not missing_tokens, (
        f"{path} must use {sorted(missing_tokens)} (C11, see docs/decisions/2026-10-17-copertura-spot.md)"
    )
    missing_tests = needs_tests - defined_test_names({Path(__file__).name})
    assert not missing_tests, f"{path} exists but these required tests do not: {sorted(missing_tests)}"
