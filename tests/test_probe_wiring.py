"""Static proof that the probe cannot leak a way to trade (plan sez. 3.6): the live gateway and a writable exchange
exist
in ONE place, the default refusal of the live mode is unchanged, and the rules of C11 hold for the new modules."""

from __future__ import annotations

import ast
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MODULES = ROOT / "modules"
PROBE = MODULES / "probe"
GATEWAY_ALLOWED = {"modules/execution/gateway.py", "modules/probe/runner.py"}
WRITABLE_ALLOWED = {"modules/probe/runner.py"}


def py_files() -> Iterator[Path]:
    yield from sorted(MODULES.rglob("*.py"))
    yield from sorted((ROOT / "script").glob("*.py"))


def rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


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
            out.add(n.name.split(".")[-1])
    return out


def test_the_live_gateway_is_named_only_by_its_own_module_and_the_probe_runner() -> None:
    hits = [
        rel(p)
        for p in py_files()
        if "LiveExecutionGateway" in identifiers(ast.parse(p.read_text(encoding="utf-8")))
        and rel(p) not in GATEWAY_ALLOWED
    ]
    assert not hits, f"only gateway.py and modules/probe/runner.py may name the live gateway: {hits}"
    assert "LiveExecutionGateway" in identifiers(ast.parse((PROBE / "runner.py").read_text(encoding="utf-8")))


def test_a_writable_exchange_is_built_only_by_the_probe_runner() -> None:
    writable: list[str] = []
    for p in py_files():
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg == "read_only" and isinstance(kw.value, ast.Constant) and kw.value.value is False:
                    writable.append(rel(p))
    assert set(writable) <= WRITABLE_ALLOWED, f"read_only=False outside the probe runner: {sorted(set(writable))}"
    assert writable, "the runner must build its writable exchange with a literal read_only=False"
    runner = ast.parse((PROBE / "runner.py").read_text(encoding="utf-8"))
    flags = {
        ast.unparse(kw.value)
        for node in ast.walk(runner)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "build_exchange"
        for kw in node.keywords
        if kw.arg == "read_only"
    }
    assert flags == {"True", "False"}  # one read-only door, one writable door, both literal


def test_the_live_gateway_is_built_exactly_once_and_with_the_guard() -> None:
    tree = ast.parse((PROBE / "runner.py").read_text(encoding="utf-8"))
    calls = [
        n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "LiveExecutionGateway"
    ]
    assert len(calls) == 1
    assert ast.unparse(calls[0].args[0]) == "guard"  # the BalanceGuard, first and positional


def test_the_default_refusal_of_the_live_mode_in_app_is_unchanged() -> None:
    """modules/app.py, config.resolve_mode and the constructors of the gateway and the guard: no diff against HEAD~N is
    asserted here (git is not a dependency of the suite); their own tests (test_app, test_gateway, test_config,
    test_balance_guard) are the proof, and this checks the facts the probe relies on."""
    app = (MODULES / "app.py").read_text(encoding="utf-8")
    assert "LiveModeUnavailable" in app and "LiveExecutionGateway" not in identifiers(ast.parse(app))
    gateway = (MODULES / "execution" / "gateway.py").read_text(encoding="utf-8")
    assert 'resolve_mode(execution, os.environ if env is None else env) != "live"' in gateway
    assert "a live gateway cannot run on a read-only exchange" in gateway


def test_no_probe_module_imports_the_sdk_or_builds_a_body_or_a_ticket() -> None:
    for p in sorted(PROBE.glob("*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        ids = identifiers(tree)
        assert not {"build_order_body", "CoverageTicket", "RESTClient", "_reserve"} & ids, rel(p)
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom):
                assert (n.module or "").split(".")[0] != "coinbase", rel(p)
            if isinstance(n, ast.Import):
                assert all(a.name.split(".")[0] != "coinbase" for a in n.names), rel(p)


def test_nothing_in_the_probe_says_margin_leverage_or_futures_not_even_in_comments() -> None:
    forbidden = re.compile(r"margin|margine|leverag|(?<![A-Za-z_])futures?(?![A-Za-z_])|perpetual|\bperp\b", re.I)
    hits = []
    for p in [*sorted(PROBE.glob("*.py")), MODULES / "execution" / "raw_log.py"]:
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if forbidden.search(line):
                hits.append(f"{rel(p)}:{i}: {line.strip()[:80]}")
    assert not hits, "\n".join(hits)


def test_only_spot_is_ever_named_by_the_probe_config() -> None:
    from modules.probe import limits

    assert limits.ALLOWED_PRODUCTS == {"BTC-EUR"}


def test_the_hand_switches_have_no_way_to_be_removed_by_the_code() -> None:
    tree = ast.parse((PROBE / "control.py").read_text(encoding="utf-8"))
    assert not {"unlink", "remove", "rmtree", "rmdir", "rename"} & identifiers(tree) - {"replace"}


def test_the_tripwire_tests_are_untouched() -> None:
    """tests/test_c11_wiring.py and tests/test_obligations.py are the guardians of C11: the probe may not edit them."""
    try:
        out = subprocess.run(
            ["git", "diff", "--name-only", "HEAD", "--", "tests/test_c11_wiring.py", "tests/test_obligations.py"],
            cwd=ROOT, capture_output=True, text=True, timeout=30, check=False,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git is not available")
    if out.returncode != 0:
        pytest.skip("not a git checkout")
    assert out.stdout.strip() == "", f"the C11 tripwires were modified: {out.stdout}"
