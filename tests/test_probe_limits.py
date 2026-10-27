"""The hard limits of the P4-pre probe: constants that the configuration can only lower."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from modules.config import AppConfig, ExecProbeConfig
from modules.probe import limits

D = Decimal


def test_the_defaults_are_the_hard_limits_of_the_plan_table() -> None:
    cfg = ExecProbeConfig()
    assert cfg.enabled is False  # off by default
    assert (cfg.product, cfg.max_total_loss_quote, cfg.max_entry_quote, cfg.test_order_quote) == (
        "BTC-EUR", D("3.00"), D("3.00"), D("2.00"),
    )  # fmt: skip
    assert (cfg.max_daily_loss_quote, cfg.anomaly_loss_quote) == (D("0.30"), D("0.50"))
    assert (cfg.tp_frac, cfg.sl_frac, cfg.horizon_min, cfg.entry_timeout_s) == (D("0.006"), D("0.006"), 480, 1800)
    assert (cfg.max_atr1h_bps, cfg.max_spread_bps, cfg.quote_max_age_s) == (60, 10, 5)
    assert (cfg.max_consecutive_sl, cfg.max_watchdog_exits) == (2, 1)
    assert (cfg.max_entries_per_day, cfg.max_entries_total, cfg.max_orders_per_day) == (2, 5, 16)
    assert (cfg.bracket_open_alarm_s, cfg.bracket_open_max_s, cfg.sl_fill_max_s) == (10, 30, 60)
    assert (cfg.sl_floor_ratio, cfg.sl_slip_alarm_bps, cfg.sl_slip_severe_bps) == (D("0.96"), 15, 50)
    assert cfg.entry_slots_utc == ("09:00", "11:00") and cfg.accept_gtc_fallback is False
    assert cfg.db_path == "state/chronos_probe.db"  # distinct from paths.live_db
    assert cfg.db_path != AppConfig().paths.live_db


def test_the_probe_is_a_section_of_the_app_config_and_off() -> None:
    assert AppConfig().exec_probe.enabled is False


@pytest.mark.parametrize("name", sorted(limits.CAPS))
def test_every_cap_refuses_a_higher_value(name: str) -> None:
    cap = limits.CAPS[name]
    higher = cap + (D("0.001") if isinstance(cap, Decimal) else 1)
    with pytest.raises(ValidationError, match=name):
        ExecProbeConfig(**{name: higher})


@pytest.mark.parametrize("value", [D("0.95"), D("0.5")])
def test_the_stop_floor_ratio_cannot_be_looser(value: Decimal) -> None:
    with pytest.raises(ValidationError, match="sl_floor_ratio"):
        ExecProbeConfig(sl_floor_ratio=value)
    assert ExecProbeConfig(sl_floor_ratio=D("0.98")).sl_floor_ratio == D("0.98")  # stricter is allowed


def test_the_limits_can_be_lowered() -> None:
    cfg = ExecProbeConfig(
        max_total_loss_quote=D("2.00"), max_entry_quote=D("2.50"), max_entries_total=3, max_daily_loss_quote=D("0.20"),
        anomaly_loss_quote=D("0.40"), horizon_min=120, entry_slots_utc=("09:00",),
    )  # fmt: skip
    assert cfg.max_total_loss_quote == D("2.00") and cfg.entry_slots_utc == ("09:00",)


@pytest.mark.parametrize(
    "bad",
    [
        {"product": "ETH-EUR"},
        {"product": "BTC-USD"},
        {"entry_slots_utc": ("09:00", "13:00")},
        {"entry_slots_utc": ()},
        {"entry_slots_utc": ("09:00", "09:00")},
        {"bracket_open_alarm_s": 20, "bracket_open_max_s": 15},
        {"sl_slip_alarm_bps": 40, "sl_slip_severe_bps": 30},
        {"max_daily_loss_quote": D("0.40"), "anomaly_loss_quote": D("0.30")},
        {"anomaly_loss_quote": D("0.50"), "max_total_loss_quote": D("0.40")},
        {"unknown_key": 1},
        {"entry_timeout_s": 60},
    ],
)
def test_incoherent_or_unlisted_values_are_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ExecProbeConfig(**bad)


def test_the_confirmation_value_is_the_product_and_the_total_loss() -> None:
    assert ExecProbeConfig().confirm_value == "BTC-EUR:3.00"
    assert ExecProbeConfig(max_total_loss_quote=D("2")).confirm_value == "BTC-EUR:2.00"


def test_n_br_is_about_one_euro_and_three_cents() -> None:
    assert limits.n_br(D("1"), D("0.006")).quantize(D("0.01")) == D("1.03")


def test_the_daily_loss_fraction_is_the_quote_over_the_reference_capital() -> None:
    assert limits.max_daily_loss_frac(D("0.30")) == pytest.approx(0.015)
