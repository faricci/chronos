"""Exact rounding to an increment (spec sez. 4.1.3, 4.3 punto 3). Decimal only, no dependencies."""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext

_PRECISION = 80  # far above any price * size we handle: the quotient below is always exact


def _units(x: Decimal, inc: Decimal, rounding: str) -> Decimal:
    if inc <= 0:
        raise ValueError(f"increment must be positive, got {inc}")
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        return (x / inc).to_integral_value(rounding=rounding)


def floor_to(x: Decimal, inc: Decimal) -> Decimal:
    """Largest multiple of ``inc`` that is <= ``x``."""
    units = _units(x, inc, ROUND_FLOOR)
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        return units * inc


def ceil_to(x: Decimal, inc: Decimal) -> Decimal:
    """Smallest multiple of ``inc`` that is >= ``x``."""
    units = _units(x, inc, ROUND_CEILING)
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        return units * inc


def is_multiple_of(x: Decimal, inc: Decimal) -> bool:
    if inc <= 0:
        raise ValueError(f"increment must be positive, got {inc}")
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        return (x / inc) == (x / inc).to_integral_value()
