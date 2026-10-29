"""The HARD limits of the P4-pre probe (plan sez. 9, single table). Constants: the configuration can only lower them.

``CAPS`` are upper bounds (a configured value above them is refused), ``FLOORS`` are lower bounds (a configured value
below them is refused: for these a smaller number is the LESS strict one). Money is Decimal, in EUR.
"""

from __future__ import annotations

from datetime import time
from decimal import Decimal
from typing import Final

D = Decimal

PRODUCT: Final = "BTC-EUR"
ALLOWED_PRODUCTS: Final = frozenset({PRODUCT})
BOOK: Final = "probe"

# --------------------------------------------------------------------------------------------- money
CAPITAL_REF_QUOTE: Final = D("20.00")  # C: only the scale of the daily-loss fraction; the physical limit is the deposit
MAX_TOTAL_LOSS_QUOTE: Final = D("3.00")  # L_tot: the whole probe cannot lose more, by construction of the sizing
MAX_ENTRY_QUOTE: Final = D("3.00")  # Q_max: required_quote (coverage fee included) of one entry
TEST_ORDER_QUOTE: Final = D("2.00")  # notional of the stage orders (place-cancel, expire, reject)
MAX_DAILY_LOSS_QUOTE: Final = D("0.30")  # entries blocked until 00:00 UTC
ANOMALY_LOSS_QUOTE: Final = D("0.50")  # cumulated loss: end of the probe and CONFLITTO

# -------------------------------------------------------------------------------------------- levels
TP_FRAC: Final = D("0.006")
SL_FRAC: Final = D("0.006")
HORIZON_MIN: Final = 480
ENTRY_TIMEOUT_S: Final = 1800  # GTD: end_time = send + this
GTD_GRACE_S: Final = 30  # how long past end_time the exchange gets to report EXPIRED
QUOTE_MIN_FACTOR: Final = D("1.02")  # N_br = quote_min_size * this / (1 - sl_frac)

# ---------------------------------------------------------------------------------------- market gates
MAX_ATR1H_BPS: Final = 60
MAX_SPREAD_BPS: Final = 10
QUOTE_MAX_AGE_S: Final = 5
BALANCE_MAX_AGE_S: Final = 120  # same number as the BalanceGuard; the planner checks it first to name the skip

# ----------------------------------------------------------------------------------------- series, counts
MAX_CONSECUTIVE_SL: Final = 2  # -> pause until 00:00 UTC of the next day, not the end
MAX_WATCHDOG_EXITS: Final = 1  # -> end and CONFLITTO
MAX_ENTRIES_PER_DAY: Final = 2
MAX_ENTRIES_TOTAL: Final = 5
MAX_ORDERS_PER_DAY: Final = 16  # blocks NEW entries only, never a bracket, an exit or a protective re-placement

# ------------------------------------------------------------------------------------------ protections
BRACKET_OPEN_ALARM_S: Final = 10  # from the terminal state of the entry to the bracket OPEN
BRACKET_OPEN_MAX_S: Final = 30  # beyond it: emergency exit
SL_FILL_MAX_S: Final = 60  # SL triggered and not fully executed: emergency exit
SL_FLOOR_RATIO: Final = D(
    "0.96"
)  # best_bid < SL * this with the position open: emergency exit (a FLOOR: higher = stricter)
SL_SLIP_ALARM_BPS: Final = 15
SL_SLIP_SEVERE_BPS: Final = 50

# ------------------------------------------------------------------------------------------------ fees
FEE_TOLERANCE_ABS: Final = D("0.000001")  # EUR
FEE_TOLERANCE_BPS: Final = D("0.5")

# ---------------------------------------------------------------------------------------------- slots
ENTRY_SLOTS_UTC: Final = (time(9, 0), time(11, 0))
ENTRY_WEEKDAYS: Final = frozenset({0, 1, 2, 3, 4})  # Monday .. Friday
SLOT_WINDOW_S: Final = 300  # a slot is served for this long after its time; later it is missed, never served late
LIVE_REJECT_MAX_AGE_DAYS: Final = 7  # the live stage accepts a ``reject`` outcome at most this old

# ------------------------------------------------------------------------------------------- guards
# ``AccountGuards`` exposure limit of the probe book: exposure is already bounded by the sizing rule (plan 1.1)
MAX_TOTAL_EXPOSURE_FRAC: Final = 1.0

CAPS: Final[dict[str, Decimal | int]] = {
    "max_total_loss_quote": MAX_TOTAL_LOSS_QUOTE,
    "max_entry_quote": MAX_ENTRY_QUOTE,
    "test_order_quote": TEST_ORDER_QUOTE,
    "max_daily_loss_quote": MAX_DAILY_LOSS_QUOTE,
    "anomaly_loss_quote": ANOMALY_LOSS_QUOTE,
    "tp_frac": TP_FRAC,
    "sl_frac": SL_FRAC,
    "horizon_min": HORIZON_MIN,
    "entry_timeout_s": ENTRY_TIMEOUT_S,
    "gtd_grace_s": GTD_GRACE_S,
    "max_atr1h_bps": MAX_ATR1H_BPS,
    "max_spread_bps": MAX_SPREAD_BPS,
    "quote_max_age_s": QUOTE_MAX_AGE_S,
    "max_consecutive_sl": MAX_CONSECUTIVE_SL,
    "max_watchdog_exits": MAX_WATCHDOG_EXITS,
    "max_entries_per_day": MAX_ENTRIES_PER_DAY,
    "max_entries_total": MAX_ENTRIES_TOTAL,
    "max_orders_per_day": MAX_ORDERS_PER_DAY,
    "bracket_open_alarm_s": BRACKET_OPEN_ALARM_S,
    "bracket_open_max_s": BRACKET_OPEN_MAX_S,
    "sl_fill_max_s": SL_FILL_MAX_S,
    "sl_slip_alarm_bps": SL_SLIP_ALARM_BPS,
    "sl_slip_severe_bps": SL_SLIP_SEVERE_BPS,
}
FLOORS: Final[dict[str, Decimal | int]] = {"sl_floor_ratio": SL_FLOOR_RATIO}


def n_br(quote_min_size: Decimal, sl_frac: Decimal) -> Decimal:
    """Smallest notional the bracket can protect: ``quote_min_size * 1.02 / (1 - sl_frac)`` (about 1.03 EUR)."""
    return quote_min_size * QUOTE_MIN_FACTOR / (1 - sl_frac)


def max_daily_loss_frac(max_daily_loss_quote: Decimal) -> float:
    """``AccountGuards`` takes the daily loss as a fraction of the opening equity, which is ``C`` for the probe."""
    return float(max_daily_loss_quote / CAPITAL_REF_QUOTE)


def slot_label(hour: int, minute: int) -> str:
    return f"{hour:02d}:{minute:02d}"
