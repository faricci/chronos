"""Helpers for the tripwire tests (test_c11_wiring.py, test_obligations.py)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"


def skip_until(path: str) -> Path:
    """Skip (visible in ``pytest -rs``) while ``path`` does not exist; return it once it does."""
    p = ROOT / path
    if not p.exists():
        pytest.skip(f"pending: owed once {path} exists")
    return p


def defined_test_names(exclude: set[str]) -> set[str]:
    """Names of every test function in ``tests/test_*.py`` except the files in ``exclude``."""
    found: set[str] = set()
    for p in TESTS.glob("test_*.py"):
        if p.name in exclude:
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        found |= {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)}
    return found
