from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from modules.execution.rounding import ceil_to, floor_to, is_multiple_of

D = Decimal
INCREMENTS = st.sampled_from([D("0.00000001"), D("0.0001"), D("0.001"), D("0.01"), D("0.05"), D("1"), D("10")])
VALUES = st.decimals(min_value=D("-1000000"), max_value=D("1000000"), places=10, allow_nan=False, allow_infinity=False)


@pytest.mark.parametrize(
    ("x", "inc", "floor", "ceil"),
    [
        ("0.0243", "0.001", "0.024", "0.025"),
        ("92.039", "0.01", "92.03", "92.04"),
        ("92.03", "0.01", "92.03", "92.03"),
        ("0", "0.01", "0", "0"),
        ("-0.015", "0.01", "-0.02", "-0.01"),
        ("108.9636", "0.01", "108.96", "108.97"),
    ],
)
def test_known_values(x: str, inc: str, floor: str, ceil: str) -> None:
    assert floor_to(D(x), D(inc)) == D(floor)
    assert ceil_to(D(x), D(inc)) == D(ceil)


@given(x=VALUES, inc=INCREMENTS)
def test_floor_and_ceil_are_the_nearest_multiples(x: Decimal, inc: Decimal) -> None:
    lo, hi = floor_to(x, inc), ceil_to(x, inc)
    assert is_multiple_of(lo, inc) and is_multiple_of(hi, inc)
    assert lo <= x <= hi
    assert x - lo < inc and hi - x < inc
    assert hi - lo in (0, inc)


@given(x=VALUES, inc=INCREMENTS)
def test_idempotent(x: Decimal, inc: Decimal) -> None:
    assert floor_to(floor_to(x, inc), inc) == floor_to(x, inc)
    assert ceil_to(ceil_to(x, inc), inc) == ceil_to(x, inc)


def test_increment_must_be_positive() -> None:
    for fn in (floor_to, ceil_to, is_multiple_of):
        with pytest.raises(ValueError, match="positive"):
            fn(D("1"), D("0"))  # type: ignore[operator]
