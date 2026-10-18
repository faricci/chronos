"""Configuration of the training tools (``config/train.yml``). Nothing here changes a label: barriers, fees, spread
and the holdout come only from ``label_config.json`` (sez. 0.4, decision R1)."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from modules.config import EXIT_CONFIG_ERROR, AppConfig, ConfigError, load_config

__all__ = ["BacktestSettings", "LgbmSettings", "TrainConfig", "load_train_config", "load_train_config_or_exit"]

DEFAULT_TRAIN_CONFIG: Final = "config/train.yml"


class _Section(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BacktestSettings(_Section):
    starting_quote: Decimal = Field(default=Decimal("100.00"), gt=0)
    tune_decision_every_min: int = Field(default=15, ge=1)
    eval_decision_every_min: int = Field(default=1, ge=1)


class LgbmSettings(_Section):
    num_threads: int = Field(default=4, ge=1)


class TrainConfig(_Section):
    data_root: str = "data"
    products: list[str] = ["BTC-EUR", "ETH-EUR", "SOL-EUR"]
    label_config: str = "data/datasets/label_config.json"
    app_config: str | None = None  # AppConfig YAML for risk / fill / timeouts; None = built-in defaults
    datasets_dir: str = "data/datasets"
    models_dir: str = "models/baseline"
    seed: int = 1234
    n_folds: int = Field(default=5, ge=2)
    val_frac: float = Field(default=0.15, gt=0, lt=0.5)
    embargo_days: int = Field(default=2, ge=0)
    sentiment_history_db: str = "data/news_history.db"
    backtest: BacktestSettings = BacktestSettings()
    lgbm: LgbmSettings = LgbmSettings()

    def app(self) -> AppConfig:
        return load_config(self.app_config) if self.app_config else AppConfig()


def load_train_config(path: str | Path = DEFAULT_TRAIN_CONFIG) -> TrainConfig:
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"Train config not found: {p}")
    try:
        raw: Any = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{p} is not valid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{p} must contain a mapping at the top level")
    try:
        return TrainConfig.model_validate(raw)
    except ValidationError as e:
        lines = [f"Invalid train configuration in {p}:"]
        lines += [f"  - {'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()]
        raise ConfigError("\n".join(lines)) from e


def load_train_config_or_exit(path: str | Path = DEFAULT_TRAIN_CONFIG) -> TrainConfig:
    try:
        return load_train_config(path)
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(EXIT_CONFIG_ERROR) from e
