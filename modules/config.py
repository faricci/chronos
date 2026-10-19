"""Typed configuration for Chronos v2 (spec sez. 4.3 punto 1, sez. 5.3 punto 2).

Replaces ``modules.utils.load_config``. Rules:
- every section is a frozen pydantic model that rejects unknown keys, so a typo or a v1 leftover
  (``risk_management``) fails at startup instead of raising ``KeyError`` at runtime;
- secrets come only from environment variables, never from the YAML;
- barrier parameters (k_tp, k_sl, atr_tf, entry_mode, horizon) and policy thresholds are NOT here:
  they are artifacts of the model version (sez. 0.4, 2.2.5, 4.2).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Literal

import structlog
import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = structlog.get_logger(__name__)

EXIT_CONFIG_ERROR: Final = 2
LIVE_CONFIRM_ENV: Final = "CHRONOS_LIVE_CONFIRM"
LIVE_CONFIRM_VALUE: Final = "I_ACCEPT_REAL_ORDERS"
DEFAULT_CONFIG_PATH: Final = "config/config.yml"

Mode = Literal["dry", "live"]


class ConfigError(Exception):
    """Invalid or unreadable configuration. ``str(e)`` is a human readable multi-line message."""


class _Section(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------------------- paths / market


class PathsConfig(_Section):
    """Storage locations (sez. 0.5). SQLite files live on a named volume, not on a bind mount."""

    state_dir: str = "state"
    data_dir: str = "data"
    models_dir: str = "models"
    news_db: str = "state/news.db"
    live_db: str = "state/chronos_live.db"


class BackfillConfig(_Section):
    start: str = "2023-10-01"
    rate_limit_per_s: float = Field(default=5.0, gt=0)
    window_minutes: int = Field(default=300, gt=0, le=350)  # API limit is 350 candles per request
    max_retries: int = Field(default=5, ge=1)


class BarsConfig(_Section):
    """1m bars are the REST candles read S seconds after the minute closes (spec sez. 2.3 A.2, invariant I1)."""

    # S: a minute is ready at M + 1 min + S. 60..300: REST candles keep changing for up to ~92 s after the close
    # (measured 2026-10-18, 0 of 70 beyond 120 s); below 60 s a third of the bars would be revised later.
    rest_delay_s: float = Field(default=120.0, ge=60, le=300)
    rest_max_delay_s: float = 180.0  # S + 60: past this lag a minute with no candle and no proof is flat_unproven
    retry_s: float = Field(default=5.0, gt=0)
    decision_max_lag_s: float = 240.0  # S + 120: past this lag a bar updates indicators and store, no decision
    store_flush_minutes: int = Field(default=15, ge=1)

    @model_validator(mode="after")
    def _ordering(self) -> BarsConfig:
        if self.rest_max_delay_s < self.rest_delay_s:
            raise ValueError("rest_max_delay_s must be >= rest_delay_s")
        if self.decision_max_lag_s < self.rest_max_delay_s + self.retry_s:
            raise ValueError("decision_max_lag_s must be >= rest_max_delay_s + retry_s")
        return self


class MarketConfig(_Section):
    products: tuple[str, ...] = ("BTC-EUR", "ETH-EUR", "SOL-EUR")
    backfill: BackfillConfig = BackfillConfig()
    bars: BarsConfig = BarsConfig()

    @field_validator("products")
    @classmethod
    def _product_ids(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if not v:
            raise ValueError("at least one product is required")
        for p in v:
            base, sep, quote = p.partition("-")
            if not (sep and base.isalnum() and quote.isalnum() and p == p.upper()):
                raise ValueError(f"invalid product id {p!r}, expected e.g. 'BTC-EUR'")
        if len(set(v)) != len(v):
            raise ValueError("duplicate products")
        return v


# ------------------------------------------------------------------------------------------- sentiment


class ClassifierConfig(_Section):
    model_dir: str = "models/sentiment/cryptobert-onnx-int8"
    model_card: str = "models/sentiment/sentiment_model_card.json"
    max_workers: int = Field(default=2, ge=1)
    max_seq_len: int = Field(default=128, ge=8)


class HalfLife(_Section):
    asset: float = Field(gt=0)
    market: float = Field(gt=0)


class AggregationConfig(_Section):
    half_life_min: HalfLife = HalfLife(asset=90, market=240)
    half_life_slow_min: HalfLife = HalfLife(asset=1440, market=2880)
    retention_days: int = Field(default=7, ge=1)
    shock_window_min: int = Field(default=15, ge=1)


class AssetAliases(_Section):
    names: tuple[str, ...] = ()
    tickers: tuple[str, ...] = ()


class SourceConfig(_Section):
    name: str
    kind: Literal["rss", "cryptopanic", "fear_greed"]
    url: str | None = None
    interval_s: float = Field(gt=0)
    token_env: str | None = None
    enabled_if_token: bool = False

    @model_validator(mode="after")
    def _url_or_token(self) -> SourceConfig:
        if self.kind in ("rss", "fear_greed") and not self.url:
            raise ValueError(f"source {self.name!r} of kind {self.kind!r} needs a url")
        if self.enabled_if_token and not self.token_env:
            raise ValueError(f"source {self.name!r}: enabled_if_token needs token_env")
        return self


def _default_sources() -> tuple[SourceConfig, ...]:
    return (
        SourceConfig(
            name="coindesk_rss", kind="rss", url="https://www.coindesk.com/arc/outboundfeeds/rss/", interval_s=45
        ),
        SourceConfig(name="cointelegraph_rss", kind="rss", url="https://cointelegraph.com/rss", interval_s=45),
        SourceConfig(name="decrypt_rss", kind="rss", url="https://decrypt.co/feed", interval_s=60),
        SourceConfig(name="theblock_rss", kind="rss", url="https://www.theblock.co/rss.xml", interval_s=60),
        SourceConfig(name="cryptoslate_rss", kind="rss", url="https://cryptoslate.com/feed/", interval_s=60),
        SourceConfig(
            name="cryptopanic", kind="cryptopanic", interval_s=120, token_env="CRYPTOPANIC_TOKEN", enabled_if_token=True
        ),
        SourceConfig(name="fear_greed", kind="fear_greed", url="https://api.alternative.me/fng/", interval_s=3600),
    )


def _default_assets() -> dict[str, AssetAliases]:
    return {
        "BTC": AssetAliases(names=("bitcoin",), tickers=("BTC", "XBT")),
        "ETH": AssetAliases(names=("ethereum", "ether"), tickers=("ETH",)),
        "SOL": AssetAliases(names=("solana",), tickers=("SOL",)),
    }


class SentimentConfig(_Section):
    classifier: ClassifierConfig = ClassifierConfig()
    aggregation: AggregationConfig = AggregationConfig()
    dedup_window_hours: int = Field(default=48, ge=1)
    assets: dict[str, AssetAliases] = Field(default_factory=_default_assets)
    market_keywords: tuple[str, ...] = ("sec", "etf", "fed", "regulation", "hack", "exchange", "stablecoin")
    sources: tuple[SourceConfig, ...] = Field(default_factory=_default_sources)

    @field_validator("sources")
    @classmethod
    def _unique_source_names(cls, v: tuple[SourceConfig, ...]) -> tuple[SourceConfig, ...]:
        names = [s.name for s in v]
        if len(set(names)) != len(names):
            raise ValueError("duplicate source names")
        return v


# --------------------------------------------------------------------------------------------- decision


class RemoteDeciderConfig(_Section):
    base_url: str = "http://decider:8080"
    api_key_env: str = "DECIDER_API_KEY"
    timeout_ms: int = Field(default=250, ge=1)


class DecisionConfig(_Section):
    backend: Literal["baseline", "openjev", "chronos-decider"] = "baseline"
    model: str = "registry:champion"
    fallback_to_baseline: bool = True
    questions_version: str = "QUESTIONS_V1"
    remote: RemoteDeciderConfig = RemoteDeciderConfig()

    def remote_api_key(self, env: Mapping[str, str] | None = None) -> SecretStr | None:
        """API key of the remote decider, read from the env var named in the config."""
        value = (os.environ if env is None else env).get(self.remote.api_key_env)
        return SecretStr(value) if value else None


# ------------------------------------------------------------------------------------------------ risk


class KillSwitchConfig(_Section):
    max_daily_loss_frac: float = Field(default=0.02, gt=0, le=1)
    max_consecutive_losses: int = Field(default=4, ge=1)
    loss_streak_pause_h: float = Field(default=48, gt=0)
    stale_data_s: float = Field(default=90, gt=0)  # last ticker
    stale_bar_s: float = Field(default=270, gt=0)  # last 1m bar, published at t + S; = S + 150
    max_fallback_ratio_1h: float = Field(default=0.20, ge=0, le=1)
    psi_threshold: float = Field(default=0.25, gt=0)


class WatchdogConfig(_Section):
    sl_breach_atr_1h: float = Field(default=1.0, ge=0)
    grace_s: float = Field(default=10, ge=0)


class RiskConfig(_Section):
    risk_per_trade: float = Field(default=0.005, gt=0, le=0.1)
    p_edge_full: float = Field(default=0.60, gt=0, le=1)
    max_position_frac: float = Field(default=0.20, gt=0, le=1)
    max_order_quote: Decimal = Field(default=Decimal("50.00"), gt=0)  # absolute cap per order, rises with the ramp
    max_total_exposure_frac: float = Field(default=0.50, gt=0, le=1)
    min_net_edge_bps: float = Field(default=30, ge=0)
    ev_min_bps: float = 0
    slippage_buffer_bps: float = Field(default=5, ge=0)
    regime_multipliers: dict[int, float] = {1: 1.0, 2: 1.0, 3: 1.0}
    kill_switch: KillSwitchConfig = KillSwitchConfig()
    watchdog: WatchdogConfig = WatchdogConfig()

    @field_validator("regime_multipliers")
    @classmethod
    def _regimes(cls, v: dict[int, float]) -> dict[int, float]:
        for regime, mult in v.items():
            if not 0 <= regime <= 4:
                raise ValueError(f"regime {regime} outside 0..4")
            if not 0 <= mult <= 1:
                raise ValueError(f"multiplier for regime {regime} outside 0..1")
        return v

    @model_validator(mode="after")
    def _exposure(self) -> RiskConfig:
        if self.max_position_frac > self.max_total_exposure_frac:
            raise ValueError("max_position_frac cannot exceed max_total_exposure_frac")
        return self


# ------------------------------------------------------------------------------------------- execution


class EntryTimeout(_Section):
    maker: float = Field(default=1800, gt=0)
    taker: float = Field(default=20, gt=0)


class ExecutionConfig(_Section):
    mode: Mode = "dry"
    entry_timeout_s: EntryTimeout = EntryTimeout()
    exit_timeout_s: float = Field(default=15, gt=0)
    reconcile_interval_s: float = Field(default=60, gt=0)
    fee_refresh_s: float = Field(default=3600, gt=0)
    product_meta_refresh_s: float = Field(default=3600, gt=0)
    # Balance snapshot refresh; at most a third of the 120 s staleness limit of the BalanceGuard (sez. 4.1.6),
    # so that at least three attempts happen before BUYs fail closed. There is NO switch to disable the guard.
    balance_refresh_s: float = Field(default=30, gt=0, le=40)
    entry_poll_s: float = Field(default=5, gt=0)
    cancel_confirm_timeout_s: float = Field(default=10, gt=0)


# ------------------------------------------------------------------------------------------------ dry run


class BookConfig(_Section):
    name: str
    decider: str
    starting_quote: Decimal = Field(default=Decimal("100.00"), gt=0)
    match_frequency_of: str | None = None
    seed: int | None = None
    # TEST ONLY, ``random_entry`` books only: draw BUY with this probability per decision and carry this ``p_edge``
    # into the RiskManager, instead of the frequency and the mean edge observed on the source book. It exists to put
    # trades through the real pipeline in a short run (a benchmark that never trades proves nothing in 20 minutes);
    # while it is on the book benchmarks nothing, and the app logs it. Both or none.
    forced_buy_rate: float | None = Field(default=None, ge=0, le=1)
    forced_edge: float | None = Field(default=None, gt=0, lt=1)

    @model_validator(mode="after")
    def _forced_goes_with_random(self) -> BookConfig:
        if (self.forced_buy_rate is None) != (self.forced_edge is None):
            raise ValueError("forced_buy_rate and forced_edge go together")
        if self.forced_buy_rate is not None and self.decider != "random":
            raise ValueError("forced_buy_rate belongs to the random decider only")
        return self


class FallbackFees(_Section):
    """Fees used when the account tier cannot be read. Presumed lowest tier, flagged ``assumed``."""

    maker_bps: float = Field(default=60, ge=0)
    taker_bps: float = Field(default=120, ge=0)

    @model_validator(mode="after")
    def _maker_not_above_taker(self) -> FallbackFees:
        if self.maker_bps > self.taker_bps:
            raise ValueError("maker_bps cannot exceed taker_bps")
        return self


class FillModelConfig(_Section):
    path: str = "models/registry/fill_model.json"
    latency_ms: float = Field(default=300, ge=0)
    queue_haircut: float = Field(default=0.25, ge=0, le=1)
    stop_extra_slip_bps: float = Field(default=10, ge=0)
    fallback_fees: FallbackFees = FallbackFees()


class RecordingConfig(_Section):
    enabled: bool = True
    dir: str = "data/ws_recordings"
    retention_days: int = Field(default=30, ge=1)


class LearnerConfig(_Section):
    run_at_utc: str = Field(default="00:15", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    reload_at_utc: str = Field(default="00:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    refit_day_of_month: int = Field(default=1, ge=1, le=28)
    min_n_eff_calibration: int = Field(default=300, ge=1)
    live_weight: float = Field(default=2.0, gt=0)
    promotion: dict[str, int] = {"backtest_recent_days": 90, "min_shadow_days": 14}


class GateOpsConfig(_Section):
    min_days: int = Field(default=7, ge=1)
    min_uptime: float = Field(default=0.99, gt=0, le=1)
    min_decision_parity: float = Field(default=0.99, gt=0, le=1)
    max_feature_psi: float = Field(default=0.10, gt=0)
    buy_rate_tolerance: float = Field(default=0.50, gt=0)
    min_maker_fill_rate: float = Field(default=0.50, ge=0, le=1)
    max_spread_ratio_vs_labels: float = Field(default=2.0, gt=0)
    max_fee_floor_skip_ratio: float = Field(default=0.80, gt=0, le=1)
    max_rest_revision_ratio: float = Field(default=0.005, gt=0, le=1)


class GateEconConfig(_Section):
    min_trades: int = Field(default=30, ge=1)
    min_profit_factor: float = Field(default=1.2, gt=0)
    max_drawdown_frac: float = Field(default=0.05, gt=0, le=1)
    max_ece_edge: float = Field(default=0.10, gt=0, le=1)


def _default_books() -> tuple[BookConfig, ...]:
    return (
        BookConfig(name="champion", decider="registry:champion"),
        BookConfig(name="challenger", decider="registry:challenger"),
        BookConfig(name="random_entry", decider="random", match_frequency_of="champion", seed=42),
        BookConfig(name="buy_and_hold", decider="buy_and_hold"),
    )


class DryRunConfig(_Section):
    db_path: str = "state/chronos_dry.db"
    books: tuple[BookConfig, ...] = Field(default_factory=_default_books)
    fill_model: FillModelConfig = FillModelConfig()
    recording: RecordingConfig = RecordingConfig()
    learner: LearnerConfig = LearnerConfig()
    gate_ops: GateOpsConfig = GateOpsConfig()
    gate_econ: GateEconConfig = GateEconConfig()

    @model_validator(mode="after")
    def _books_consistent(self) -> DryRunConfig:
        names = [b.name for b in self.books]
        if len(set(names)) != len(names):
            raise ValueError("duplicate book names")
        for b in self.books:
            if b.match_frequency_of is not None and b.match_frequency_of not in names:
                raise ValueError(f"book {b.name!r}: match_frequency_of refers to unknown book {b.match_frequency_of!r}")
        return self


class TelemetryConfig(_Section):
    """What the agent writes for outsiders (spec sez. 6.3): the present (``status.json``), the history (events, equity,
    ops of the day). Empty ``events_dir`` / ``equity_dir`` / ``ops_dir`` mean ``<data_dir>/events``, ``/equity`` and
    ``/reports/ops`` (where the report reads them); ``dir`` is where ``status.json`` goes. Nothing here can stop
    trading: a telemetry error is logged and counted, never raised (sez. 6.3.5)."""

    enabled: bool = True
    dir: str = "telemetry"
    status_interval_s: float = Field(default=10, gt=0)
    equity_interval_s: float = Field(default=60, gt=0)
    ops_interval_s: float = Field(default=60, gt=0)
    events_dir: str | None = None
    equity_dir: str | None = None
    ops_dir: str | None = None
    events_queue_max: int = Field(default=10_000, ge=1)
    events_retention_days: int = Field(default=180, ge=1)


class ParityTolerance(_Section):
    max_median_entry_delta_bps: float = Field(default=10, gt=0)
    min_bracket_outcome_agreement: float = Field(default=0.90, gt=0, le=1)


class LiveRampConfig(_Section):
    steps: tuple[str, ...] = ("R0", "R1", "R2")
    r0_min_trades: int = Field(default=10, ge=1)
    step_min_trades: int = Field(default=30, ge=1)
    parity: ParityTolerance = ParityTolerance()


# ----------------------------------------------------------------------------------------------- secrets


class CoinbaseConfig(BaseSettings):
    """Coinbase CDP credentials, read only from COINBASE_API_KEY / COINBASE_API_SECRET.

    Dry mode and development use a View-only key; a Trade key exists only in live (sez. 0.5).
    Public market data (candles, level2, ticker) needs no credentials.
    """

    model_config = SettingsConfigDict(env_prefix="COINBASE_", env_file=".env", extra="ignore", frozen=True)

    api_key: SecretStr | None = None
    api_secret: SecretStr | None = None

    @field_validator("api_secret")
    @classmethod
    def _unescape_newlines(cls, v: SecretStr | None) -> SecretStr | None:
        # PEM keys are often stored in .env files / compose with literal "\n"
        if v is None:
            return None
        return SecretStr(v.get_secret_value().replace("\\n", "\n"))

    @model_validator(mode="after")
    def _both_or_none(self) -> CoinbaseConfig:
        if (self.api_key is None) != (self.api_secret is None):
            raise ValueError("COINBASE_API_KEY and COINBASE_API_SECRET must be set together")
        return self

    @property
    def has_credentials(self) -> bool:
        return self.api_key is not None and self.api_secret is not None


# --------------------------------------------------------------------------------------------- top level


class AppConfig(_Section):
    paths: PathsConfig = PathsConfig()
    market: MarketConfig = MarketConfig()
    sentiment: SentimentConfig = SentimentConfig()
    decision: DecisionConfig = DecisionConfig()
    risk: RiskConfig = RiskConfig()
    execution: ExecutionConfig = ExecutionConfig()
    dry_run: DryRunConfig = DryRunConfig()
    live_ramp: LiveRampConfig = LiveRampConfig()
    telemetry: TelemetryConfig = TelemetryConfig()
    coinbase: CoinbaseConfig = Field(default_factory=CoinbaseConfig)

    @model_validator(mode="after")
    def _stale_bar_covers_the_publication_lag(self) -> AppConfig:
        # With S = 30 the normal age of the last bar reaches 90 s just before the next one is published: the guard
        # must not fire every minute.
        bars, stale = self.market.bars, self.risk.kill_switch.stale_bar_s
        floor = bars.rest_max_delay_s + 90
        if stale < floor:
            raise ValueError(f"risk.kill_switch.stale_bar_s must be >= market.bars.rest_max_delay_s + 90 = {floor}")
        if stale <= bars.decision_max_lag_s:
            # a decision is only computed on a bar the guard does not already consider stale
            raise ValueError("risk.kill_switch.stale_bar_s must be > market.bars.decision_max_lag_s")
        return self


def _format_validation_error(path: Path | str, err: ValidationError) -> str:
    lines = [f"Invalid configuration in {path}:"]
    for e in err.errors():
        where = ".".join(str(p) for p in e["loc"]) or "<root>"
        lines.append(f"  - {where}: {e['msg']}")
    return "\n".join(lines)


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    """Load and validate the YAML configuration. Raises ``ConfigError`` with a readable message."""
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"Config file not found: {p} (copy config/config.example.yml to {p})")
    try:
        raw: Any = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"Config file {p} is not valid YAML: {e}") from e
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"Config file {p} must contain a mapping at the top level")
    if "coinbase" in raw:
        raise ConfigError(
            f"Invalid configuration in {p}:\n  - coinbase: credentials are read only from the environment "
            "(COINBASE_API_KEY, COINBASE_API_SECRET); remove this section from the YAML"
        )
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(_format_validation_error(p, e)) from e


def load_config_or_exit(path: str | Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    """Entry-point helper: print the error on stderr and exit with code 2 (sez. 4.3 punto 1)."""
    try:
        return load_config(path)
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(EXIT_CONFIG_ERROR) from e


def resolve_mode(cfg: ExecutionConfig, env: Mapping[str, str]) -> Mode:
    """``live`` only if the config says live AND the confirmation env var is exact (sez. 5.1.1, 5.3 punto 2)."""
    if cfg.mode == "live" and env.get(LIVE_CONFIRM_ENV) == LIVE_CONFIRM_VALUE:
        return "live"
    if cfg.mode == "live":
        log.warning("live_not_confirmed_starting_in_dry", required_env=LIVE_CONFIRM_ENV)
    return "dry"
