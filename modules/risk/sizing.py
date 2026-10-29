"""Position sizing: fixed risk, scaled by the calibrated edge and the predicted regime (spec sez. 4.1.2).

Pure function over ``Decimal``: no float reaches a price or a size. The coverage cap uses the same formulas as the
``BalanceGuard`` (``max_base_for_quote`` / ``coverage_fee_rate``) so that the size it returns is never refused for
coverage; the guard stays the final authority (C11).

::

    scale_p       = clip((p_edge - p_edge_min) / (p_edge_full - p_edge_min), 0, 1)
                    (p_edge_min is the floor of THIS decision: break-even + buffer; when it reaches p_edge_full
                    every decision that passed the EV gate has full conviction: scale_p = 1)
    risk_eur      = equity * risk_frac * scale_p * m_reg
    loss_per_unit = (entry - SL) + entry * cost_sl / 1e4
    size_raw      = risk_eur / loss_per_unit
    notional_cap  = min(equity * max_position_frac, max_order_quote) / entry
    cover_cap     = max_base_for_quote(free_quote, entry, f_cov, quote_inc, base_inc)
    size          = floor_to(min(size_raw, floor_to(min(notional_cap, cover_cap))))
    min_size      = max(base_min_size, ceil_to(quote_min_size / entry, base_inc))
    below min_size: bump to min_size only if min_size * loss_per_unit <= equity * risk_frac and min_size <= size_cap
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from modules.config import FallbackFees
from modules.execution.orders import ProductMeta
from modules.execution.rounding import ceil_to, floor_to
from modules.risk.costs import FeeRates, coverage_fee_rate, max_base_for_quote

__all__ = ["SizeSkip", "SizingResult", "compute_size"]

_BPS = Decimal(10_000)


@dataclass(frozen=True)
class SizingResult:
    base_size: Decimal
    risk_eur: Decimal
    scale_p: Decimal
    loss_per_unit: Decimal
    size_raw: Decimal
    size_cap: Decimal
    min_size_bump: bool


@dataclass(frozen=True)
class SizeSkip:
    reason: Literal["below_min_size", "insufficient_balance"]
    detail: str = ""


def _dec(x: float | Decimal) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def compute_size(
    *,
    equity: Decimal,
    free_quote: Decimal,
    entry: Decimal,
    stop_loss: Decimal,
    cost_sl_bps: float,
    p_edge: float,
    p_edge_min: float,
    p_edge_full: float,
    m_reg: float,
    risk_frac: float,
    max_position_frac: float,
    max_order_quote: Decimal,
    meta: ProductMeta,
    fees: FeeRates,
    fallback_fees: FallbackFees,
) -> SizingResult | SizeSkip:
    """Size of a BUY limit at ``entry`` (already rounded to the quote increment), or why there is none."""
    if entry <= 0 or stop_loss >= entry:
        raise ValueError("sizing needs 0 < stop_loss < entry")
    if p_edge_full <= p_edge_min:
        scale_p = Decimal(1) if p_edge >= p_edge_min else Decimal(0)
    else:
        span = _dec(p_edge_full) - _dec(p_edge_min)
        scale_p = min(max((_dec(p_edge) - _dec(p_edge_min)) / span, Decimal(0)), Decimal(1))
    budget = equity * _dec(risk_frac)  # risk of one trade at full conviction
    risk_eur = budget * scale_p * _dec(m_reg)
    loss_per_unit = (entry - stop_loss) + entry * _dec(cost_sl_bps) / _BPS
    size_raw = risk_eur / loss_per_unit

    f_cov = coverage_fee_rate(fees, fallback_fees)
    notional_cap = min(equity * _dec(max_position_frac), max_order_quote) / entry
    cover_cap = max_base_for_quote(free_quote, entry, f_cov, meta.quote_increment, meta.base_increment)
    size_cap = floor_to(min(notional_cap, cover_cap), meta.base_increment)
    size = floor_to(min(size_raw, size_cap), meta.base_increment)

    min_size = max(meta.base_min_size, ceil_to(meta.quote_min_size / entry, meta.base_increment))
    bump = False
    if size < min_size:
        if min_size <= size_cap and min_size * loss_per_unit <= budget:
            size, bump = min_size, True  # "min-size bump": the risk stays inside the per-trade budget
        elif cover_cap < min_size:
            return SizeSkip("insufficient_balance", f"free quote covers {cover_cap} base, min is {min_size}")
        else:
            return SizeSkip("below_min_size", f"size {size} < min {min_size}")
    return SizingResult(size, risk_eur, scale_p, loss_per_unit, size_raw, size_cap, bump)
