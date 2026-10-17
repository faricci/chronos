"""Single cost model of the system (spec sez. 0.3, ``COST_V1``).

This is the ONLY place where trading costs are computed. Training labels, the StateBuilder, the RiskManager
and the FillModel import from here; nobody else derives costs on their own.

All ``*_bps`` quantities are floats in basis points and are approximations meant for gates and features
(fee floor, EV gate, ``p_breakeven``). Realized, simulated and label PnL is always computed exactly from
fill prices and per-leg fees with :func:`net_return` (Decimal).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Final, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, model_validator

from modules.config import AppConfig, FallbackFees
from modules.execution.rounding import ceil_to, floor_to

log = structlog.get_logger(__name__)

EntryMode = Literal["maker", "taker"]
Clock = Callable[[], datetime]
# Returns (maker_bps, taker_bps) of the account fee tier
FeeFetcher = Callable[[], Awaitable[tuple[float, float]]]

_BPS = Decimal(10_000)


def utc_now() -> datetime:
    return datetime.now(UTC)


def round_bps(x: float) -> int:
    """Round half up for display and persisted features (196.5 -> 197); ``round()`` would give 196."""
    return int(Decimal(repr(x)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


# ----------------------------------------------------------------------------------------------- fees


class FeeRates(BaseModel):
    """Maker/taker fee of the account in bps. ``assumed`` is True when not read from the account."""

    model_config = ConfigDict(frozen=True)

    maker_bps: float = Field(ge=0)
    taker_bps: float = Field(ge=0)
    assumed: bool
    as_of: datetime | None = None

    @model_validator(mode="after")
    def _maker_not_above_taker(self) -> FeeRates:
        if self.maker_bps > self.taker_bps:
            raise ValueError(f"maker fee {self.maker_bps} bps above taker fee {self.taker_bps} bps")
        return self


class FeeProvider:
    """Fee tier of the account with a 1 h cache (sez. 0.3).

    ``fetcher`` wraps ``get_transaction_summary`` (it will be provided by ``CoinbaseExchange``). Without a
    fetcher, or when it fails, the configured fallback fees are used and flagged ``assumed=True``: they are
    the presumed lowest tier, deliberately conservative. A failure is retried after ``retry_s`` rather than
    after the full cache period.
    """

    def __init__(
        self,
        fallback: FallbackFees,
        fetcher: FeeFetcher | None = None,
        *,
        refresh_s: float = 3600,
        retry_s: float = 60,
        clock: Clock = utc_now,
    ) -> None:
        self._fetcher = fetcher
        self._refresh = timedelta(seconds=refresh_s)
        self._retry = timedelta(seconds=retry_s)
        self._clock = clock
        self._lock = asyncio.Lock()
        self._current = FeeRates(maker_bps=fallback.maker_bps, taker_bps=fallback.taker_bps, assumed=True)
        self._next_refresh: datetime | None = None
        self._fallback = self._current

    @property
    def current(self) -> FeeRates:
        """Last known fees. Never blocks: before the first refresh these are the fallback fees."""
        return self._current

    async def refresh(self) -> FeeRates:
        """Return the fees, hitting the fetcher only when the cache is expired."""
        async with self._lock:
            now = self._clock()
            if self._next_refresh is not None and now < self._next_refresh:
                return self._current
            if self._fetcher is None:
                self._next_refresh = now + self._refresh
                return self._current
            try:
                maker, taker = await self._fetcher()
                self._current = FeeRates(maker_bps=maker, taker_bps=taker, assumed=False, as_of=now)
                self._next_refresh = now + self._refresh
            except Exception as e:  # network, auth, malformed payload: never block trading on fee lookup
                log.warning("fee_fetch_failed_using_fallback", error=f"{type(e).__name__}: {e}")
                self._current = self._fallback
                self._next_refresh = now + self._retry
            return self._current


# ------------------------------------------------------------------------------------------ cost model


class CostParams(BaseModel):
    """Non-fee cost parameters: ``slip`` and ``x`` of sez. 0.3."""

    model_config = ConfigDict(frozen=True)

    slippage_buffer_bps: float = Field(default=5, ge=0)
    stop_extra_slip_bps: float = Field(default=10, ge=0)

    @classmethod
    def from_config(cls, cfg: AppConfig) -> CostParams:
        return cls(
            slippage_buffer_bps=cfg.risk.slippage_buffer_bps,
            stop_extra_slip_bps=cfg.dry_run.fill_model.stop_extra_slip_bps,
        )


class CostModel(BaseModel):
    """Asymmetric cost of a long round trip: entry leg + exit leg that depends on how the trade ends.

    | leg                         | order                | cost                          |
    |-----------------------------|----------------------|-------------------------------|
    | entry, maker (default)      | post-only at bid     | f_m                           |
    | entry, taker                | marketable at ask    | f_t + s/2 + slip              |
    | exit at TP                  | limit sell (maker)   | f_m                           |
    | exit at SL                  | stop (taker)         | f_t + s/2 + slip + x          |
    | exit by model/horizon/dog   | marketable (taker)   | f_t + s/2 + slip              |
    """

    model_config = ConfigDict(frozen=True)

    fees: FeeRates
    entry_mode: EntryMode = "maker"
    spread_bps: float = Field(ge=0)
    params: CostParams = CostParams()

    # --- legs
    @property
    def _taker_leg(self) -> float:
        return self.fees.taker_bps + self.spread_bps / 2 + self.params.slippage_buffer_bps

    @property
    def c_in(self) -> float:
        return self.fees.maker_bps if self.entry_mode == "maker" else self._taker_leg

    @property
    def c_tp_out(self) -> float:
        return self.fees.maker_bps

    @property
    def c_sl_out(self) -> float:
        return self._taker_leg + self.params.stop_extra_slip_bps

    @property
    def c_mkt_out(self) -> float:
        return self._taker_leg

    # --- round trips
    @property
    def cost_tp(self) -> float:
        return self.c_in + self.c_tp_out

    @property
    def cost_sl(self) -> float:
        return self.c_in + self.c_sl_out

    @property
    def cost_mkt(self) -> float:
        return self.c_in + self.c_mkt_out

    # --- gates
    def tp_net_bps(self, tp_bps: float) -> float:
        """Gain at TP after costs."""
        return tp_bps - self.cost_tp

    def sl_loss_bps(self, sl_bps: float) -> float:
        """Loss at SL including costs (positive number)."""
        return sl_bps + self.cost_sl

    def passes_fee_floor(self, tp_bps: float, min_net_edge_bps: float) -> bool:
        return self.tp_net_bps(tp_bps) >= min_net_edge_bps

    def p_breakeven(self, tp_bps: float, sl_bps: float) -> float | None:
        """Minimum P(UPPER) for a positive two-outcome EV, an upper bound of the real breakeven (see ``ev_bps``).

        ``None`` when the TP does not even cover its own costs.
        """
        win = self.tp_net_bps(tp_bps)
        if win <= 0:
            return None
        loss = self.sl_loss_bps(sl_bps)
        return loss / (win + loss)

    def ev_bps(self, p_edge: float, tp_bps: float, sl_bps: float) -> float:
        """Two-outcome expected value per trade in bps (sez. 0.3).

        ``p_edge`` is P(UPPER), the calibrated answer to the ``edge`` question. Every other outcome (LOWER and
        VERTICAL) is valued as a stop-loss exit. A VERTICAL exit happens strictly inside the barriers and costs
        ``cost_mkt <= cost_sl``, so its loss is always smaller than ``sl_loss_bps``: the result is a lower bound
        of the real EV. This is intended (conservative gate). Do not add a VERTICAL term: a runtime P(VERTICAL)
        would need a new model question (new QUESTIONS version).
        """
        if not 0.0 <= p_edge <= 1.0:
            raise ValueError(f"p_edge must be in [0, 1], got {p_edge}")
        return p_edge * self.tp_net_bps(tp_bps) - (1.0 - p_edge) * self.sl_loss_bps(sl_bps)

    def required_lift(self, tp_bps: float, sl_bps: float) -> float | None:
        """How much the model must beat a driftless random walk: ``p_breakeven - sl/(tp+sl)`` (sez. 0.4)."""
        p_be = self.p_breakeven(tp_bps, sl_bps)
        if p_be is None:
            return None
        return p_be - sl_bps / (tp_bps + sl_bps)


# --------------------------------------------------------------------------------------- exact pnl


def _frac(bps: float | Decimal) -> Decimal:
    return (bps if isinstance(bps, Decimal) else Decimal(str(bps))) / _BPS


def net_return(
    entry_px: Decimal,
    exit_px: Decimal,
    f_in_bps: float | Decimal,
    f_out_bps: float | Decimal,
) -> Decimal:
    """Exact net return of a long round trip (sez. 0.3).

    ``entry_px`` / ``exit_px`` already include spread and slippage of their taker legs (and ``x`` for a stop).
    ``f_in_bps`` / ``f_out_bps`` are the fees actually paid on each leg. The size cancels out in the ratio.
    """
    entry_cost = entry_px * (1 + _frac(f_in_bps))
    exit_value = exit_px * (1 - _frac(f_out_bps))
    return exit_value / entry_cost - 1


def net_pnl_quote(
    entry_px: Decimal,
    exit_px: Decimal,
    size: Decimal,
    f_in_bps: float | Decimal,
    f_out_bps: float | Decimal,
) -> Decimal:
    """Exact net PnL in quote currency: exit value minus entry cost, fees included."""
    entry_cost = entry_px * size * (1 + _frac(f_in_bps))
    exit_value = exit_px * size * (1 - _frac(f_out_bps))
    return exit_value - entry_cost


# ------------------------------------------------------------------------------------ spot coverage
# Absolute financial constraint C11 (spec sez. 4.1.6). These are RESERVATION formulas, not costs: they are
# deliberately pessimistic upper bounds and never enter EV, labels or the fee floor.

COVERAGE_FEE_FLOOR_BPS: Final = Decimal(120)  # hard-coded on purpose: a floor for the reservation, not config


def coverage_fee_rate(fees: FeeRates, fallback: FallbackFees) -> Decimal:
    """Fee rate (fraction) used to reserve quote for a BUY: an upper bound of any fill's fee rate."""
    bps = max(Decimal(str(fees.taker_bps)), Decimal(str(fallback.taker_bps)), COVERAGE_FEE_FLOOR_BPS)
    return bps / _BPS


def required_quote(limit_price: Decimal, base_size: Decimal, fee_rate: Decimal, quote_increment: Decimal) -> Decimal:
    """Quote that must be free to place a limit BUY, one increment of cushion included.

    Valid whether the exchange holds only the notional or notional plus fee: both are <= this amount.
    """
    return ceil_to(limit_price * base_size * (1 + fee_rate), quote_increment) + quote_increment


def max_base_for_quote(
    quote: Decimal,
    limit_price: Decimal,
    fee_rate: Decimal,
    quote_increment: Decimal,
    base_increment: Decimal,
) -> Decimal:
    """Largest base size whose :func:`required_quote` fits in ``quote`` (inverse used by the sizing)."""
    budget = floor_to(quote, quote_increment) - quote_increment
    if budget <= 0:
        return Decimal(0)
    size = floor_to(budget / (limit_price * (1 + fee_rate)), base_increment)
    while size > 0 and required_quote(limit_price, size, fee_rate, quote_increment) > quote:
        size -= base_increment  # only reachable through Decimal division rounding
    return max(size, Decimal(0))
