from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from modules.config import (
    EXIT_CONFIG_ERROR,
    LIVE_CONFIRM_ENV,
    LIVE_CONFIRM_VALUE,
    AppConfig,
    ConfigError,
    ExecutionConfig,
    load_config,
    load_config_or_exit,
    resolve_mode,
)

EXAMPLE = Path(__file__).resolve().parent.parent / "config" / "config.example.yml"


@pytest.fixture(autouse=True)
def _no_ambient_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("COINBASE_API_KEY", "COINBASE_API_SECRET"):
        monkeypatch.delenv(name, raising=False)


def _write(tmp_path: Path, data: Any) -> Path:
    p = tmp_path / "config.yml"
    p.write_text(data if isinstance(data, str) else yaml.safe_dump(data), encoding="utf-8")
    return p


def test_example_file_equals_defaults() -> None:
    """The documented example and the built-in defaults must never drift apart."""
    assert load_config(EXAMPLE) == AppConfig()


def test_defaults_match_spec() -> None:
    cfg = AppConfig()
    assert cfg.execution.mode == "dry"
    assert cfg.decision.backend == "baseline"
    assert cfg.market.products == ("BTC-EUR", "ETH-EUR", "SOL-EUR")
    assert cfg.risk.regime_multipliers == {1: 1.0, 2: 1.0, 3: 1.0}
    assert cfg.dry_run.fill_model.fallback_fees.maker_bps == 60
    assert cfg.dry_run.fill_model.fallback_fees.taker_bps == 120
    assert cfg.dry_run.fill_model.stop_extra_slip_bps == 10
    assert cfg.risk.slippage_buffer_bps == 5
    assert cfg.execution.entry_timeout_s.maker == 1800


def test_empty_yaml_gives_defaults(tmp_path: Path) -> None:
    assert load_config(_write(tmp_path, "")) == AppConfig()


def test_partial_override(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, {"risk": {"risk_per_trade": 0.01}}))
    assert cfg.risk.risk_per_trade == 0.01
    assert cfg.risk.p_edge_full == 0.60


def test_missing_file_is_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yml")


def test_invalid_yaml_is_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(_write(tmp_path, "risk: [unclosed"))


def test_non_mapping_top_level(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="mapping"):
        load_config(_write(tmp_path, "- a\n- b\n"))


def test_v1_keys_are_rejected_at_startup(tmp_path: Path) -> None:
    """v1 read config['risk_management'] and raised KeyError at runtime; v2 rejects unknown keys on load."""
    with pytest.raises(ConfigError, match="trading"):
        load_config(_write(tmp_path, {"trading": {"risk_management": {"stop_loss_percent": 0.02}}}))
    with pytest.raises(ConfigError, match=r"risk\.stop_loss_percent"):
        load_config(_write(tmp_path, {"risk": {"stop_loss_percent": 0.02}}))


def test_secrets_in_yaml_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="environment"):
        load_config(_write(tmp_path, {"coinbase": {"name": "k", "privateKey": "s"}}))


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"risk": {"risk_per_trade": 0}}, r"risk\.risk_per_trade"),
        ({"risk": {"risk_per_trade": 0.5}}, r"risk\.risk_per_trade"),
        ({"risk": {"regime_multipliers": {7: 1.0}}}, "outside 0..4"),
        ({"risk": {"regime_multipliers": {2: 1.5}}}, "outside 0..1"),
        ({"risk": {"max_position_frac": 0.6, "max_total_exposure_frac": 0.5}}, "max_position_frac"),
        ({"execution": {"mode": "paper"}}, r"execution\.mode"),
        ({"market": {"products": []}}, "at least one product"),
        ({"market": {"products": ["btc-eur"]}}, "invalid product id"),
        ({"market": {"products": ["BTC-EUR", "BTC-EUR"]}}, "duplicate products"),
        ({"market": {"backfill": {"window_minutes": 400}}}, "window_minutes"),
        ({"dry_run": {"fill_model": {"fallback_fees": {"maker_bps": 130, "taker_bps": 120}}}}, "maker_bps"),
        ({"dry_run": {"books": [{"name": "a", "decider": "x"}, {"name": "a", "decider": "y"}]}}, "duplicate book"),
        (
            {"dry_run": {"books": [{"name": "r", "decider": "random", "match_frequency_of": "ghost"}]}},
            "unknown book",
        ),
        ({"dry_run": {"learner": {"run_at_utc": "25:00"}}}, "run_at_utc"),
        ({"sentiment": {"sources": [{"name": "x", "kind": "rss", "interval_s": 30}]}}, "needs a url"),
        ({"decision": {"backend": "gpt"}}, r"decision\.backend"),
        ({"risk": {"max_order_quote": 0}}, r"risk\.max_order_quote"),
        ({"market": {"bars": {"rest_delay_s": 59}}}, r"market\.bars\.rest_delay_s"),
        ({"market": {"bars": {"rest_delay_s": 301}}}, r"market\.bars\.rest_delay_s"),
        ({"market": {"bars": {"rest_delay_s": 120, "rest_max_delay_s": 100}}}, "rest_max_delay_s must be >="),
        ({"market": {"bars": {"decision_max_lag_s": 150}}}, "decision_max_lag_s must be >="),
        ({"market": {"bars": {"decision_max_lag_s": 270}}}, "stale_bar_s must be > market.bars.decision_max_lag_s"),
        ({"market": {"bars": {"retry_s": 0}}}, r"market\.bars\.retry_s"),
        ({"market": {"bars": {"rest_max_delay_s": 200, "decision_max_lag_s": 260}}}, "stale_bar_s must be >="),
        ({"risk": {"kill_switch": {"stale_bar_s": 200}}}, "stale_bar_s must be >="),
        ({"execution": {"balance_refresh_s": 60}}, r"execution\.balance_refresh_s"),
    ],
)
def test_validation_errors_are_readable(tmp_path: Path, override: dict[str, Any], fragment: str) -> None:
    with pytest.raises(ConfigError, match=fragment) as exc:
        load_config(_write(tmp_path, override))
    assert "Invalid configuration" in str(exc.value)


def test_load_config_or_exit_uses_exit_code_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        load_config_or_exit(tmp_path / "missing.yml")
    assert exc.value.code == EXIT_CONFIG_ERROR == 2
    assert "not found" in capsys.readouterr().err


# ------------------------------------------------------------------------------------------- secrets


def test_credentials_absent_by_default() -> None:
    assert not AppConfig().coinbase.has_credentials


def test_credentials_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COINBASE_API_KEY", "organizations/o/apiKeys/k")
    monkeypatch.setenv("COINBASE_API_SECRET", "-----BEGIN EC PRIVATE KEY-----\\nabc\\n-----END EC PRIVATE KEY-----")
    cb = AppConfig().coinbase
    assert cb.has_credentials
    assert cb.api_secret is not None
    assert "\n" in cb.api_secret.get_secret_value()  # literal \n unescaped
    assert "abc" not in repr(cb) and "abc" not in str(cb)  # never leaked by repr/str


def test_half_configured_credentials_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COINBASE_API_KEY", "k")
    with pytest.raises(Exception, match="set together"):
        AppConfig()


def test_remote_decider_key_from_env() -> None:
    cfg = AppConfig()
    assert cfg.decision.remote_api_key({}) is None
    key = cfg.decision.remote_api_key({"DECIDER_API_KEY": "s3cret"})
    assert key is not None and key.get_secret_value() == "s3cret"


# ------------------------------------------------------------------------------------- resolve_mode

CONFIRM = LIVE_CONFIRM_VALUE


@pytest.mark.parametrize(
    ("cfg_mode", "env", "expected"),
    [
        ("dry", {}, "dry"),
        ("dry", {LIVE_CONFIRM_ENV: CONFIRM}, "dry"),  # env alone never enables live
        ("dry", {LIVE_CONFIRM_ENV: "nope"}, "dry"),
        ("live", {}, "dry"),  # config alone never enables live
        ("live", {LIVE_CONFIRM_ENV: ""}, "dry"),
        ("live", {LIVE_CONFIRM_ENV: "i_accept_real_orders"}, "dry"),  # exact match only
        ("live", {LIVE_CONFIRM_ENV: CONFIRM + " "}, "dry"),
        ("live", {LIVE_CONFIRM_ENV: CONFIRM}, "live"),
    ],
)
def test_resolve_mode_truth_table(cfg_mode: str, env: dict[str, str], expected: str) -> None:
    cfg = ExecutionConfig.model_validate({"mode": cfg_mode})
    assert resolve_mode(cfg, env) == expected


def test_default_execution_mode_is_dry() -> None:
    assert resolve_mode(ExecutionConfig(), {LIVE_CONFIRM_ENV: CONFIRM}) == "dry"


def test_coverage_settings_defaults_and_no_off_switch() -> None:
    cfg = AppConfig()
    assert cfg.risk.max_order_quote == 50
    assert cfg.execution.balance_refresh_s * 3 <= 120  # at least three refresh attempts before the guard fails closed
    # C11: the balance guard, the spot registry and the order whitelist cannot be turned off from the config
    names = {n.lower() for section in (cfg.risk, cfg.execution) for n in type(section).model_fields}
    assert not any(w in n for n in names for w in ("guard", "whitelist", "leverage", "margin", "disable_coverage"))


def test_bar_settings_defaults() -> None:
    bars = AppConfig().market.bars
    assert (bars.rest_delay_s, bars.rest_max_delay_s, bars.retry_s) == (120, 180, 5)
    assert (bars.decision_max_lag_s, bars.store_flush_minutes) == (240, 15)
    ks = AppConfig().risk.kill_switch
    assert (ks.stale_bar_s, ks.stale_data_s) == (270, 90)
    # the derived defaults follow S by fixed formulas, so a retuning changes one number
    s = bars.rest_delay_s
    assert (bars.rest_max_delay_s, bars.decision_max_lag_s, ks.stale_bar_s) == (s + 60, s + 120, s + 150)
    assert AppConfig().dry_run.gate_ops.max_rest_revision_ratio == 0.005


def test_a_larger_publication_lag_needs_a_larger_stale_bar_threshold() -> None:
    ok = {
        "market": {"bars": {"rest_delay_s": 300, "rest_max_delay_s": 360, "decision_max_lag_s": 420}},
        "risk": {"kill_switch": {"stale_bar_s": 450}},
    }
    from modules.config import AppConfig as App

    assert App.model_validate(ok).market.bars.rest_max_delay_s == 360
