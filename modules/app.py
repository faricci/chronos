"""Chronos v2 orchestrator: ``python -m modules.app`` (spec sez. 0.2, 4.3 punto 12-13, 5.1, 5.3 punto 15, 6.3 D13).

What runs in this phase (P2/P3): the DRY pipeline of every book of ``dry_run.books`` on the real market flow:

    incremental backfill -> warm-up of the indicator engines on the WHOLE history -> WS stream + REST bars at T+S
    -> ShadowBookManager (per book: ShadowExecutionGateway, VirtualAccount, DecisionEngine with state, decider, policy
    and RiskManager) + one Watchdog and one set of account guards per book + KillSwitch, all inside one
    ``asyncio.TaskGroup``.

No real order can leave this process (C9, C11), and that is layered, not assumed:

1. ``resolve_mode`` answers ``live`` only with ``execution.mode: live`` AND ``CHRONOS_LIVE_CONFIRM``. This phase has no
   live path at all: when the answer is ``live`` the process refuses to start (``LiveModeUnavailable``, exit code 2)
   BEFORE it builds an exchange, a ledger or a gateway. ``LiveExecutionGateway`` is not imported by name here.
2. The only exchange object is built with ``read_only=True`` (a literal in ``default_exchange``): any write raises
   ``DryRunViolation`` before the network (exit code 3).
3. The only gateway is ``ShadowExecutionGateway``: it has no exchange, requires a ``BalanceGuard`` and writes the dry
   ledger only. ``tests/test_app.py`` pins all three with an AST scan of this file.
4. The API key is vetted: transfer permission is refused in every mode (``SpotViolation``, exit code 4); a key that can
   trade is allowed but flagged (a dry run is meant to use a View key).

Startup order (exit code 2 on a bad configuration or model, 4 on a ``SpotViolation``, 3 on a ``DryRunViolation``):
mode -> read-only exchange and key permissions -> ``SpotRegistry`` (every product verified SPOT) -> models (the
``champion`` alias of the ``ModelRegistry``, ``model_card.json``, ``label_config.json``, ``final/policy.json``; the
other aliases of the books) -> fee tier -> dry ledger (opened and migrated) -> incremental backfill (``settle_s`` =
``market.bars.rest_delay_s``) -> warm-up of the engines on the whole history (worker threads) -> ``BarFinalizer.seed``
-> kill switch, the state of every book read back from the ledger, ``build_books`` -> restore and gap resolution ->
watchdogs, telemetry -> streams.

The books (one implementation, ``modules/dryrun/books.py``): ``dry_run.books`` says which run. ``champion`` is the model
under the ``champion`` alias; a ``registry:<alias>`` book (``challenger``) starts only if the registry has that alias;
``random_entry`` matches the frequency of its source book (and is skipped if the source is); ``buy_and_hold`` buys each
product once. The app adds what the shared builder cannot know through hooks of ``BookDeps``: ``wrap_gateway`` (a
``SpotViolation`` / ``DryRunViolation`` must stop the process, an exit reason must reach the ledger) and ``restore``.
The gates of the entries are the app's: the untunable policy of the model a book runs, the shutdown, the kill switch.

Champion resolution. ``decision.model: registry:champion`` is read from ``<models_dir>/registry``. When the registry is
EMPTY (no version at all) the newest version under ``<models_dir>/baseline`` is registered and promoted as champion,
once, with a ``champion_bootstrapped`` warning: that is the only case in which the app promotes anything. A registry
that has versions but no ``champion`` alias refuses to start (promotion is a human act, ``python -m
modules.dryrun.registry``).

Model rules wired here:

* the policy of the ``final`` model; when ``policy.json`` says ``untunable`` the books of that model run with NO
  entries: decisions are computed and recorded, the RiskManager gate refuses every entry
  (``policy_untunable:<reason>``);
* the sentiment reaches the engines ONLY when ``model_card.sentiment_available`` is true. The current baseline is
  trained without it, so it is passed as ``None`` (anything else is train/serve skew) and the sentiment service does
  not start. A model book whose card disagrees with the champion's is not started.

Restart. The books live in memory (balances, simulated brackets) and in the dry ledger. At startup every book is
rebuilt from the ledger (``modules/dryrun/restore.py``): the balances and PnL from the booked fills, the open brackets
as simulated brackets, the orders that cannot be resumed cancelled; then the bars of the downtime are fed to the
restored brackets so a take profit or a stop touched while the process was down is resolved at the bar that touched it.

Guards wired here (spec 4.1.4): ``SystemGuards.observe_bar_published`` on every published bar (stale bar), the last
ticker (stale data), the decider-health guard from the engines' ``stats`` (> ``max_fallback_ratio_1h`` of the decisions
of the last hour failed or fell back), and the account guards per book (equity, closed trades through the ledger's
``closed_trades`` listener, coverage alarms). Entries stop before the shutdown begins.

Tasks never die in silence (``Lifecycle.task_failed``): the exception is logged with its traceback, kept in
``failures``, turned into a fault of the decider-health guard (every entry of every book is blocked), and a critical
task also starts an orderly shutdown (exit code 1; Docker restarts the container; exits and brackets are untouched).
``SpotViolation`` and ``DryRunViolation`` are fatal wherever they appear.

Shutdown (SIGINT/SIGTERM or a fatal error): entries are refused, the producers are cancelled, pending entries are
cancelled, the orders in flight are awaited for ``SHUTDOWN_GRACE_S``, the bar store is flushed, the kill switch state
is saved, the ops of the day are written, the ledger is closed. Existing brackets are never cancelled.

Telemetry (spec 6.3, ``modules/telemetry/``): ``status.json`` (``Runtime.status()``), the event log
(``Runtime.on_event`` and the structlog forwarder), ``data/equity/DAY.jsonl`` and ``data/reports/ops/DAY.json`` (what
``modules/dryrun/report.py`` judges G-DRY-OPS on). They run as non-critical tasks and NOTHING they do can reach the
``TaskGroup`` (6.3.5). ``<data_dir>/heartbeat`` is the file of the container health check (spec 4.3 punto 13).
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import math
import os
import re
import signal
import sys
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, MutableMapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any, Final, Protocol

import pandas as pd
import structlog
from aiolimiter import AsyncLimiter

from modules.config import (
    DEFAULT_CONFIG_PATH,
    EXIT_CONFIG_ERROR,
    AppConfig,
    BookConfig,
    CoinbaseConfig,
    Mode,
    load_config_or_exit,
    resolve_mode,
)
from modules.decision.baseline import BaselineDecider
from modules.decision.client import SystemOneClient
from modules.decision.engine import BaselineBackend, DecisionEngine, DecisionLog, ExecutionReport, Processed
from modules.decision.policy import REGIME_RULE, PolicyConfig, PolicyMismatchError, load_policy
from modules.decision.questions import QuestionSet, build_questions
from modules.dryrun.books import (
    BookConfigError,
    BookDeps,
    BookModel,
    ShadowBook,
    ShadowBookManager,
    apply_book_swaps,
    build_books,
    prepare_book_swap,
)
from modules.dryrun.fill_model import FillModelParams
from modules.dryrun.recorder import WsRecorder
from modules.dryrun.registry import (
    CHAMPION as CHAMPION_ALIAS,
)
from modules.dryrun.registry import (
    FilesystemModelRegistry,
    RegisteredModel,
    RegistryError,
    UnknownAlias,
    artifacts_from_dir,
)
from modules.dryrun.restore import BookRestore, load_book_restore
from modules.dryrun.schedule import (
    DEFAULT_LEARNER_TIMEOUT_S,
    DailyJob,
    LearnerLauncher,
    LearnerRunner,
    run_learner_process,
)
from modules.dryrun.shadow_gateway import BarEvent, RestoreReport, ShadowExecutionGateway
from modules.dryrun.virtual_account import VirtualAccount
from modules.execution.balance import Clock, CoverageAlarm, utc_now
from modules.execution.exchange import (
    CoinbaseExchange,
    DryRunViolation,
    KeyPermissionsInfo,
    build_exchange,
)
from modules.execution.gateway import CancelOutcome, EntryResult, ExecutionGateway, ExitResult
from modules.execution.spot import SPOT_EXIT_CODE, SpotRegistry, SpotViolation, assert_live_key_permissions
from modules.execution.watchdog import Watchdog
from modules.ledger.positions import ClosedTrade, EntryStamp, PositionLedger
from modules.market.backfill import (
    Backfiller,
    BarStore,
    CandleSource,
    CoinbaseCandleSource,
    bars_root,
    load_bars,
    parse_utc,
)
from modules.market.bars import BarClosed, BarFinalizer, BarService, BarStoreWriter
from modules.market.indicators import IndicatorEngine
from modules.market.orderbook import LocalOrderBook
from modules.market.ws_feed import CoinbaseMarketStream, Quote, Trade
from modules.risk.costs import FeeProvider, FeeRates
from modules.risk.kill_switch import AccountGuards, ExitReason, KillSwitch, SystemGuards
from modules.risk.label_config import LoadedLabelConfig, load_label_config
from modules.risk.risk_manager import ExitIntent, MarketQuote, OrderIntent
from modules.telemetry.events import EventLog, LogForwarder
from modules.telemetry.files import TelemetryErrors, write_json_atomic
from modules.telemetry.ops import OpsRecorder
from modules.telemetry.status import EquityWriter, StatusWriter

log = structlog.get_logger(__name__)

__all__ = [
    "LOG_FORWARDER",
    "Book",
    "Champion",
    "Deps",
    "LiveModeUnavailable",
    "Runtime",
    "StartupError",
    "TaskSpec",
    "check_key_permissions",
    "find_model_dir",
    "load_alias_model",
    "load_champion",
    "main",
]

# --------------------------------------------------------------------------------------------- constants

EXIT_OK: Final = 0
EXIT_FAILURE: Final = 1  # a critical task died, or an unexpected error: Docker restarts the container
EXIT_DRY_VIOLATION: Final = 3  # DryRunViolation (spec 5.1.1)
CHAMPION: Final = "champion"  # the book (and the registry alias) every run needs
FINAL: Final = "final"  # model subset served at runtime
AGENT_VERSION: Final = "2.0.0"

HEARTBEAT_FILE: Final = "heartbeat"
HEARTBEAT_SCHEMA: Final = "chronos.heartbeat.v1"
HEARTBEAT_S: Final = 10.0
HEARTBEAT_MAX_AGE_S: Final = 120  # the container health check (docker-compose.yml) uses the same number
WS_MAX_AGE_S: Final = 60.0  # no message at all for this long = the feed is dead (heartbeats arrive every second)
TICKER_POLL_S: Final = 1.0
HEALTH_EVERY_S: Final = 60.0
HEALTH_WINDOW_S: Final = 3600.0
MIN_DECISIONS_FOR_HEALTH: Final = 10  # below this the ratio of the last hour says nothing
SHUTDOWN_GRACE_S: Final = 10.0  # spec 4.3 punto 12
FEE_POLL_S: Final = 60.0
FAILURES_KEPT: Final = 20
MINUTE: Final = timedelta(minutes=1)
MODEL_BOOK_KINDS: Final = ("model", "random_entry")  # the books that decide with a model (and have a watchdog)
SCHEDULE_POLL_S: Final = 15.0  # how often the daily clock looks at the time (00:15 learner, 00:30 reload)

Sleep = Callable[[float], Awaitable[None]]
EventHook = Callable[[str, str, Mapping[str, Any]], None]
LOG_FORWARDER: Final = LogForwarder()  # the structlog processor of the event log (set up by ``configure_logging``)


class StartupError(Exception):
    """The process cannot start (bad model, missing credentials, ...). ``exit_code`` is what the process returns."""

    def __init__(self, message: str, exit_code: int = EXIT_CONFIG_ERROR) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class LiveModeUnavailable(StartupError):
    """``resolve_mode`` said ``live``: this build has no live path (the live gateway is not instantiated)."""


# ------------------------------------------------------------------------------------------------ logging


_PEM: Final = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL)
_BEARER: Final = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
SECRET_ENV_NAMES: Final = ("COINBASE_API_KEY", "COINBASE_API_SECRET", "DECIDER_API_KEY", "CRYPTOPANIC_TOKEN")
REDACTED: Final = "***"


def collect_secrets(cfg: AppConfig | None, env: Mapping[str, str]) -> list[str]:
    """Every secret value this process can know (environment and the ``.env`` read by ``CoinbaseConfig``)."""
    values: list[str] = []
    for name in SECRET_ENV_NAMES:
        if env.get(name):
            values.append(env[name])
    if cfg is not None:
        for secret in (cfg.coinbase.api_key, cfg.coinbase.api_secret):
            if secret is not None:
                values.append(secret.get_secret_value())
    # a PEM key exists with real line breaks (parsed config) and with a backslash-n (.env, environment): cover both
    expanded = [*values, *(v.replace("\\n", "\n") for v in values), *(v.replace("\n", "\\n") for v in values)]
    return sorted({v for v in expanded if len(v) >= 6}, key=len, reverse=True)


def redact(value: Any, secrets: Sequence[str]) -> Any:
    """``value`` with every secret, PEM private key and bearer token replaced by ``***`` (strings, lists, dicts)."""
    if isinstance(value, str):
        text = _BEARER.sub(REDACTED, _PEM.sub(REDACTED, value))
        for secret in secrets:
            text = text.replace(secret, REDACTED)
        return text
    if isinstance(value, Mapping):
        return {k: redact(v, secrets) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact(v, secrets) for v in value]
    return value


def configure_logging(secrets: Sequence[str] = ()) -> None:
    """JSON logs on stderr; the redaction runs after the traceback is rendered, last before the renderer. The event
    log forwarder (errors and a few typed facts become events) sits after the redaction: an event carries no secret."""

    def scrub(_logger: Any, _name: str, event: MutableMapping[str, Any]) -> Mapping[str, Any]:
        return {k: redact(v, secrets) for k, v in event.items()}

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            scrub,
            LOG_FORWARDER,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )


# ------------------------------------------------------------------------------------------- the model


@dataclass(frozen=True)
class Champion:
    """A model as the books serve it, read from ``models/baseline/<version>/`` (or its copy in the registry)."""

    version: str
    model_dir: Path
    label: LoadedLabelConfig
    questions: QuestionSet
    policy: PolicyConfig  # a placeholder that never buys when ``no_entry_reason`` is set
    no_entry_reason: str | None  # ``policy.json`` is ``untunable``: the books of this model run with no entries
    sentiment_available: bool
    baseline: BaselineBackend

    def book_model(self) -> BookModel:
        """The artifacts of this version in the form ``build_books`` takes."""
        return BookModel(self.version, self.label, self.policy, self.questions, self.baseline)


_VERSION = re.compile(r"@(\d{4}-\d{2}-\d{2})\.(\d+)$")


def _version_key(path: Path) -> tuple[str, int]:
    m = _VERSION.search(path.name)
    return (m.group(1), int(m.group(2))) if m else ("", 0)


def registry_of(models_dir: Path) -> FilesystemModelRegistry:
    return FilesystemModelRegistry(models_dir / "registry")


def _bootstrap_champion(models_dir: Path, registry: FilesystemModelRegistry) -> RegisteredModel:
    """The registry has no version at all: register the newest ``models/baseline`` version and promote it. This is the
    only place where the app promotes anything, it is explicit, and it is never done when the registry has content."""
    root = models_dir / "baseline"
    found = sorted((p for p in root.glob("*@*") if (p / "model_card.json").is_file()), key=_version_key)
    if not found:
        raise StartupError(
            f"the model registry {registry.root} is empty and there is no model under {root}: "
            "train one with `python -m training.baseline_lgbm`"
        )
    source = found[-1]
    try:
        card = json.loads((source / "model_card.json").read_text(encoding="utf-8"))
        version = str(card.get("model") or source.name)
        registry.register(
            artifacts_from_dir(source),
            {"version": version, "kind": "baseline-lgbm", "origin": f"bootstrap:{source.as_posix()}",
             "label_config_sha256": (card.get("label_config") or {}).get("sha256")},
        )  # fmt: skip
        registry.promote(version, CHAMPION_ALIAS)
        model = registry.get(CHAMPION_ALIAS)
    except (OSError, ValueError, RegistryError) as exc:
        raise StartupError(f"cannot bootstrap the champion from {source}: {type(exc).__name__}: {exc}") from exc
    log.warning(
        "champion_bootstrapped", version=model.version, source=str(source), registry=str(registry.root),
        hint="the registry was empty: the newest baseline model was registered and promoted ONCE; "
             "promotions are made with `python -m modules.dryrun.registry promote`",
    )  # fmt: skip
    return model


def find_model_dir(models_dir: Path, spec: str) -> Path:
    """``registry:<alias>`` -> the directory of the version the alias of ``<models_dir>/registry`` points at (for
    ``champion`` on an EMPTY registry: the bootstrap above); anything else is a version directory name under
    ``models/baseline`` or a path."""
    if spec.startswith("registry:"):
        alias = spec.partition(":")[2]
        registry = registry_of(models_dir)
        try:
            return registry.get(alias).path
        except UnknownAlias:
            if alias != CHAMPION_ALIAS:
                raise StartupError(f"decision.model {spec!r}: the registry has no alias {alias!r}") from None
            if registry.versions():
                raise StartupError(
                    f"the model registry {registry.root} has versions {registry.versions()} but no champion: "
                    "promote one with `python -m modules.dryrun.registry promote <version>`"
                ) from None
            return _bootstrap_champion(models_dir, registry).path
        except RegistryError as exc:
            raise StartupError(f"model registry {registry.root}: {type(exc).__name__}: {exc}") from exc
    root = models_dir / "baseline"
    for candidate in (root / spec, Path(spec)):
        if (candidate / "model_card.json").is_file():
            return candidate
    raise StartupError(f"decision.model {spec!r}: no model_card.json under {root / spec}")


def no_entry_policy(model: str, label_sha256: str) -> PolicyConfig:
    """Thresholds nobody can reach (BUY needs a one-hot answer); the RiskManager gate refuses the entry anyway."""
    return PolicyConfig(
        model=model, tau_buy=1.0, tau_sell=1.0, c_min=1.0, p_be_buffer=0.20, regime_rule=REGIME_RULE,
        label_config_sha256=label_sha256,
    )  # fmt: skip


def _load_model_dir(model_dir: Path) -> Champion:
    """Load the ``final`` model of a version directory. A broken or foreign artifact refuses to start (fail closed); an
    ``untunable`` policy is NOT an error: the books of the model run without entries."""
    try:
        card = json.loads((model_dir / "model_card.json").read_text(encoding="utf-8"))
        label = load_label_config(model_dir / "label_config.json")
        questions = build_questions(label.cfg.horizon_min, label.cfg.k_tp, label.cfg.k_sl, label.cfg.atr_tf)
        baseline = BaselineDecider.load(model_dir, label=label, questions=questions, subset=FINAL)
        version = str(card.get("model") or model_dir.name)
        policy_path = model_dir / FINAL / "policy.json"
        raw = json.loads(policy_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and raw.get("untunable"):
            reason: str | None = str(raw.get("reason") or "untunable")
            policy = no_entry_policy(version, label.sha256)
        else:
            reason, policy = None, load_policy(policy_path, label.sha256)
    except (OSError, ValueError, KeyError, PolicyMismatchError) as exc:  # pydantic and JSON errors are ValueErrors
        raise StartupError(f"model {model_dir.name} cannot be loaded: {type(exc).__name__}: {exc}") from exc
    if reason is not None:
        log.warning("model_runs_without_entries", model=version, reason=reason)
    available = card.get("sentiment_available") is True
    return Champion(version, model_dir, label, questions, policy, reason, available, baseline)


def load_champion(models_dir: Path, spec: str) -> Champion:
    """The champion book's model: ``decision.model`` resolved by ``find_model_dir`` and loaded."""
    return _load_model_dir(find_model_dir(models_dir, spec))


def load_alias_model(models_dir: Path, alias: str) -> Champion | None:
    """The model a registry alias (``challenger``...) points at, or ``None`` when the alias is not set: the book that
    needs it is simply not started. A damaged registry or model is an error, not a missing alias."""
    registry = registry_of(models_dir)
    try:
        return _load_model_dir(registry.get(alias).path)
    except UnknownAlias:
        return None
    except RegistryError as exc:
        raise StartupError(f"model registry {registry.root}: {type(exc).__name__}: {exc}") from exc


# ----------------------------------------------------------------------------------- collaborators (seams)


class ExchangeReader(Protocol):
    """The reads startup needs from ``CoinbaseExchange`` (writes are not even named here)."""

    async def get_product_raw(self, product_id: str) -> Mapping[str, Any]: ...
    async def get_fee_rates(self) -> FeeRates: ...
    async def get_api_key_permissions(self) -> KeyPermissionsInfo: ...


class MarketFeed(Protocol):
    """``CoinbaseMarketStream`` as the app uses it."""

    @property
    def books(self) -> Mapping[str, LocalOrderBook]: ...
    @property
    def quotes(self) -> Mapping[str, Quote]: ...
    @property
    def last_message_at(self) -> datetime | None: ...
    def spread_bps(self, product_id: str) -> float | None: ...
    async def run(self) -> None: ...


class BarRunner(Protocol):
    """``BarService`` as the app uses it."""

    async def run(self) -> None: ...


class SentimentRunner(Protocol):
    """``SentimentService`` as the app uses it."""

    def snapshot(self, product_id: str, as_of: datetime) -> Any: ...
    async def run(self) -> None: ...


def default_exchange(cfg: CoinbaseConfig) -> CoinbaseExchange:
    """The ONLY exchange of this process: read only, whatever the key can do (spec 5.1.1 punto 2)."""
    if not cfg.has_credentials:
        raise StartupError(
            "dry mode needs a Coinbase View key to read the products, fees and balances: set COINBASE_API_KEY and "
            "COINBASE_API_SECRET (environment or .env)"
        )
    return build_exchange(cfg, read_only=True)


async def default_backfill(cfg: AppConfig, source: CandleSource, clock: Clock) -> None:
    await Backfiller(
        store=BarStore(bars_root(cfg)),
        source=source,
        limiter=AsyncLimiter(cfg.market.backfill.rate_limit_per_s, 1),
        cfg=cfg.market.backfill,
        clock=clock,
        settle_s=cfg.market.bars.rest_delay_s,
    ).run(list(cfg.market.products), parse_utc(cfg.market.backfill.start))


async def default_sentiment(cfg: AppConfig, env: Mapping[str, str], stack: AsyncExitStack) -> SentimentRunner:
    from concurrent.futures import ThreadPoolExecutor

    from modules.sentiment.classifier import try_load_classifier
    from modules.sentiment.service import SentimentService
    from modules.sentiment.store import NewsStore

    store = await NewsStore.open(cfg.paths.news_db)
    stack.push_async_callback(store.close)
    c = cfg.sentiment.classifier
    classifier = try_load_classifier(c.model_dir, c.model_card, max_seq_len=c.max_seq_len)
    executor: ThreadPoolExecutor | None = None
    if classifier is not None:
        executor = ThreadPoolExecutor(max_workers=c.max_workers, thread_name_prefix="sentiment-onnx")
        stack.callback(executor.shutdown, wait=False)
    return SentimentService(cfg.sentiment, store, classifier=classifier, executor=executor, env=env)


def default_stream(
    products: Sequence[str],
    raw_tap: Callable[[float, str], None] | None,
    on_damage: Callable[[datetime, datetime | None], None],
    on_trades: Callable[[Sequence[Trade]], None] | None = None,
) -> MarketFeed:
    return CoinbaseMarketStream(products, raw_tap=raw_tap, on_damage=on_damage, on_trades=on_trades)


def default_bar_service(
    finalizer: BarFinalizer,
    queue: asyncio.Queue[BarClosed],
    source: CandleSource,
    writer: BarStoreWriter,
    cfg: AppConfig,
    clock: Clock,
) -> BarRunner:
    return BarService(
        finalizer, queue, source, products=cfg.market.products, store_writer=writer,
        retry_s=cfg.market.bars.retry_s, clock=clock,
    )  # fmt: skip


@dataclass(frozen=True)
class Deps:
    """Everything that touches the outside world, replaceable by fakes. Defaults are the production wiring."""

    clock: Clock = utc_now
    sleep: Sleep = asyncio.sleep
    exchange: Callable[[CoinbaseConfig], ExchangeReader] = default_exchange
    candle_source: Callable[[], CandleSource] = CoinbaseCandleSource
    backfill: Callable[[AppConfig, CandleSource, Clock], Awaitable[None]] = default_backfill
    history: Callable[[Path, str], pd.DataFrame] = load_bars
    champion: Callable[[Path, str], Champion] = load_champion
    model_alias: Callable[[Path, str], Champion | None] = load_alias_model
    stream: Callable[..., MarketFeed] = default_stream
    bar_service: Callable[..., BarRunner] = default_bar_service
    sentiment: Callable[[AppConfig, Mapping[str, str], AsyncExitStack], Awaitable[SentimentRunner]] = default_sentiment
    run_learner: LearnerRunner = run_learner_process  # the nightly learner as a low-priority child process (00:15)


# ------------------------------------------------------------------------------------------- key / safety


def check_key_permissions(mode: Mode, perms: KeyPermissionsInfo) -> None:
    """Vet the API key before anything else uses it (spec 5.1.1, 5.3 punto 15; C11).

    Live (not reachable in this phase: ``Runtime.start`` refuses it first) needs a key that can trade, cannot transfer
    and sits in a plain portfolio. Dry refuses any key that can move funds and flags one that can trade.
    """
    if mode == "live":
        assert_live_key_permissions(perms)
        return
    if perms.can_transfer:
        raise SpotViolation(
            "the API key can transfer funds: Chronos refuses to run with it in any mode (use a View key)"
        )
    if perms.can_trade:
        log.warning("dry_run_key_can_trade", hint="a dry run is meant to use a View-only key (spec 5.1.1)")


# -------------------------------------------------------------------------------------------- lifecycle


class RatioWindow:
    """Ratio of ``bad`` events over ``total`` events in the last ``window_s`` seconds, from cumulative counters."""

    def __init__(self, window_s: float = HEALTH_WINDOW_S) -> None:
        self._window = timedelta(seconds=window_s)
        self._samples: deque[tuple[datetime, int, int]] = deque()

    def update(self, now: datetime, total: int, bad: int) -> tuple[float | None, int]:
        """Record the counters; return ``(ratio, events in the window)``. No events: ``(None, 0)``."""
        self._samples.append((now, total, bad))
        while len(self._samples) > 1 and self._samples[1][0] <= now - self._window:
            self._samples.popleft()  # keep one sample at or before the start of the window as the baseline
        _, base_total, base_bad = self._samples[0]
        n = total - base_total
        return ((bad - base_bad) / n if n > 0 else None), n


class Lifecycle:
    """Stop request, exit code, failures and the faults that block the entries of every book.

    Shared by the ``Runtime`` and by the gateway wrapper (which reports the fatal violations).
    """

    def __init__(self, system: SystemGuards, clock: Clock) -> None:
        self._system = system
        self._clock = clock
        self.stop = asyncio.Event()
        self.stopping = False
        self.exit_code = EXIT_OK
        self.faults: dict[str, str] = {}
        self.failures: deque[dict[str, str]] = deque(maxlen=FAILURES_KEPT)
        self.fatal_hooks: list[Callable[[BaseException, int], None]] = []  # telemetry: (error, exit code)

    def request_stop(self, reason: str) -> None:
        """Entries stop at once (the gate reads ``stopping``); the rest of the shutdown follows in ``Runtime.run``."""
        if not self.stopping:
            log.info("stop_requested", reason=reason)
        self.stopping = True
        self.stop.set()

    def _set_code(self, code: int) -> None:
        if code in (EXIT_DRY_VIOLATION, SPOT_EXIT_CODE) or self.exit_code == EXIT_OK:
            self.exit_code = code

    def set_fault(self, key: str, message: str) -> None:
        if self.faults.get(key) != message:
            self.faults[key] = message
            self.publish_health()

    def clear_fault(self, key: str) -> None:
        if self.faults.pop(key, None) is not None:
            self.publish_health()

    def publish_health(self) -> None:
        """Write the faults to the decider-health guard of the SystemGuards (every entry of every book)."""
        reason = "; ".join(f"{k}: {v}" for k, v in sorted(self.faults.items()))
        self._system.set_decider_health(not self.faults, reason=reason, at=self._clock())

    def fatal(self, exc: BaseException) -> None:
        """A violation of the spot / dry rules (or a dead critical task): record it and stop."""
        code = EXIT_FAILURE
        if isinstance(exc, SpotViolation):
            code = SPOT_EXIT_CODE
        elif isinstance(exc, DryRunViolation):
            code = EXIT_DRY_VIOLATION
        log.critical("fatal_error", error=f"{type(exc).__name__}: {exc}", exit_code=code)
        self._set_code(code)
        for hook in self.fatal_hooks:
            try:
                hook(exc, code)
            except Exception as hook_exc:  # telemetry must never get in the way of a stop
                log.error("fatal_hook_failed", error=f"{type(hook_exc).__name__}: {hook_exc}")
        self.request_stop(f"fatal:{type(exc).__name__}")

    def task_failed(self, name: str, exc: BaseException, *, critical: bool) -> None:
        message = f"{type(exc).__name__}: {exc}"
        log.error("task_failed", task=name, critical=critical, error=message, exc_info=exc)
        self.failures.append({"task": name, "error": message, "at": self._clock().isoformat()})
        self.set_fault(f"task:{name}", message)  # blocks every entry of every book (SystemGuards)
        if isinstance(exc, SpotViolation | DryRunViolation) or critical:
            self.fatal(exc)


@dataclass(frozen=True)
class TaskSpec:
    """A task of the ``TaskGroup``. ``critical``: its death stops the process; otherwise it is only a fault."""

    name: str
    run: Callable[[], Awaitable[None]]
    critical: bool = True


# --------------------------------------------------------------------------------------------- the books


class _EntryGates:
    """``BookKillSwitch`` of ``books.py`` for every book: the entry gates plus the coverage breach the guard reads.

    The first gate with a reason blocks the entry: the untunable policy of the model the book runs, the shutdown, the
    kill switch (system guards, then the guards of the book).
    """

    def __init__(self, life: Lifecycle, kill_switch: KillSwitch) -> None:
        self._life, self._kill_switch = life, kill_switch
        self.no_entry: dict[str, str] = {}  # book -> reason (set once the books are built)

    def entry_blocked(self, book: str) -> str | None:
        reason = self.no_entry.get(book)
        if reason is not None:
            return f"policy_untunable:{reason}"
        if self._life.stopping:
            return "shutting_down"
        return self._kill_switch.entry_blocked(book)

    def coverage_breach_active(self, book: str) -> bool:
        return self._kill_switch.coverage_breach_active(book)


class _FeedQuotes:
    """``QuoteSource`` of ``modules/dryrun/books.py`` over the market stream: the best bid/ask of the live book."""

    def __init__(self, feed: MarketFeed) -> None:
        self._feed = feed

    def quote(self, product_id: str) -> MarketQuote | None:
        book = self._feed.books.get(product_id)
        if book is None or book.stale:
            return None  # a stale book is not a price
        bid, ask = book.best_bid(), book.best_ask()
        if bid is None or ask is None:
            return None
        quote = MarketQuote(bid, ask)
        return quote if quote.valid() else None


class _ObservedGateway:
    """The gateway as the engine and the watchdog of a model book see it: every call goes to the shadow gateway.

    It adds two things the wiring needs: a ``SpotViolation`` / ``DryRunViolation`` is fatal for the process (the engine
    and the watchdog would otherwise swallow it in their own error handling), and the reason of an exit that is not a
    bracket is announced to the ledger before the sale, so the ``closed_trades`` row says why the position closed.
    """

    def __init__(
        self, inner: ExecutionGateway, ledger: PositionLedger, book: str, on_fatal: Callable[[BaseException], None]
    ) -> None:
        self._inner, self._ledger, self._book, self._on_fatal = inner, ledger, book, on_fatal

    async def submit_entry_with_bracket(self, intent: OrderIntent) -> EntryResult:
        try:
            return await self._inner.submit_entry_with_bracket(intent)
        except (SpotViolation, DryRunViolation) as exc:
            self._on_fatal(exc)
            raise

    async def submit_exit(self, intent: ExitIntent) -> ExitResult:
        self._ledger.expect_exit(self._book, intent.product_id, intent.reason)
        try:
            return await self._inner.submit_exit(intent)
        except (SpotViolation, DryRunViolation) as exc:
            self._on_fatal(exc)
            raise
        finally:
            self._ledger.clear_exit(self._book, intent.product_id)

    async def emergency_exit(self, product_id: str) -> ExitResult:
        self._ledger.expect_exit(self._book, product_id, "WATCHDOG")
        try:
            return await self._inner.emergency_exit(product_id)
        except (SpotViolation, DryRunViolation) as exc:
            self._on_fatal(exc)
            raise
        finally:
            self._ledger.clear_exit(self._book, product_id)

    async def cancel(self, product_id: str) -> CancelOutcome:
        return await self._inner.cancel(product_id)


@dataclass
class Book:
    """One book of the process: the ``ShadowBook`` of ``books.py`` and what the app adds (guards, watchdog)."""

    book: ShadowBook
    guards: AccountGuards
    gateway: _ObservedGateway | None  # what the engine and the watchdog call; ``None`` for a book without an engine
    watchdog: Watchdog | None  # model books only: a buy-and-hold position has no horizon
    base_of: dict[str, str]

    @property
    def name(self) -> str:
        return self.book.name

    @property
    def kind(self) -> str:
        return self.book.kind

    @property
    def account(self) -> VirtualAccount:
        return self.book.account

    @property
    def shadow(self) -> ShadowExecutionGateway:
        return self.book.gateway

    @property
    def engine(self) -> DecisionEngine | None:
        engine = self.book.engine
        return engine if isinstance(engine, DecisionEngine) else None

    @property
    def risk(self) -> Any:
        return self.book.risk


def _exit_reason(reason: str) -> ExitReason:
    """The kill switch knows five exit reasons; a manual close and the decision reasons of a model SELL all count as
    ``SELL`` (a manual close is neutral for the loss streak)."""
    if reason in ("TP", "SL", "HORIZON", "WATCHDOG"):
        return reason  # type: ignore[return-value]
    return "SELL"


def _log_execution(book: str, report: ExecutionReport) -> None:
    log.info(
        "execution_report", book=book, decision_id=report.decision_id, product=report.product_id, kind=report.kind,
        status=None if report.result is None else report.result.status, error=report.error,
    )  # fmt: skip


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _plan_books(
    cfg: AppConfig, champion: Champion, models_dir: Path, load_alias: Callable[[Path, str], Champion | None]
) -> tuple[list[BookConfig], dict[str, Champion]]:
    """The books of ``dry_run.books`` that can start, in config order, and the model of each registry alias.

    A ``registry:<alias>`` book starts only if the registry has the alias, the model has a spread for every product and
    its card agrees with the champion's on the sentiment; ``random_entry`` only if the book it matches starts."""
    configured = list(cfg.dry_run.books)
    if CHAMPION not in {b.name for b in configured}:
        raise StartupError(f"dry_run.books has no book named {CHAMPION!r}")
    models: dict[str, Champion] = {CHAMPION_ALIAS: champion}
    started: list[BookConfig] = []
    for b in configured:
        prefix, sep, alias = b.decider.partition(":")
        if not (sep and prefix == "registry"):
            continue
        model = models.get(alias)
        if model is None:
            model = load_alias(models_dir, alias)
            if model is None:
                log.info("book_not_started", book=b.name, reason=f"the model registry has no alias {alias!r}")
                continue
            if model.sentiment_available != champion.sentiment_available:
                log.warning("book_not_started", book=b.name, reason="its model and the champion differ on sentiment")
                continue
            try:
                for product in cfg.market.products:
                    model.label.spread_for(product, utc_now())
            except ValueError as exc:
                log.warning("book_not_started", book=b.name, reason=f"model {model.version}: {exc}")
                continue
            models[alias] = model
        started.append(b)
    names = {b.name for b in started}
    for b in configured:
        if b.decider.startswith("registry:"):
            continue  # decided above
        if b.decider == "random" and b.match_frequency_of not in names:
            log.info("book_not_started", book=b.name, reason=f"the book it matches ({b.match_frequency_of}) is off")
            continue
        started.append(b)
        names.add(b.name)
    order = {b.name: i for i, b in enumerate(configured)}
    return sorted(started, key=lambda b: order[b.name]), models


class _TickRelay:
    """The market stream is built before the books, so its callbacks go through here: the damage intervals are kept
    (status, ops) and, once the manager exists, handed to it together with the trades (tick fill mode)."""

    def __init__(self, damage: list[tuple[datetime, datetime | None]]) -> None:
        self.manager: ShadowBookManager | None = None
        self._damage = damage

    def on_trades(self, trades: Sequence[Trade]) -> None:
        if self.manager is not None:
            self.manager.on_trades(trades)

    def on_damage(self, start: datetime, end: datetime | None) -> None:
        self._damage.append((start, end))
        if self.manager is not None:
            self.manager.on_damage(start, end)


class _Late:
    """Callbacks the books need before the ``Runtime`` exists (``BookDeps`` is built first). Until ``runtime`` is set
    they do the part that needs nothing else; afterwards they forward."""

    def __init__(self, guards: Mapping[str, AccountGuards], clock: Clock) -> None:
        self.runtime: Runtime | None = None
        self._guards = guards
        self._clock = clock

    def closed(self, trade: ClosedTrade) -> None:
        if self.runtime is not None:
            self.runtime.on_trade_closed(trade)
            return
        guards = self._guards.get(trade.book)
        if guards is not None:
            guards.observe_trade_closed(self._clock(), trade.net_quote, _exit_reason(trade.exit_reason))

    def execution(self, book: str, report: ExecutionReport) -> None:
        _log_execution(book, report)
        if self.runtime is not None:
            self.runtime.on_execution(book, report)

    def alarm(self, book: str, alarm: CoverageAlarm) -> None:
        guards = self._guards.get(book)
        if guards is not None:
            guards.trip_coverage_breach(f"{alarm.kind}:{alarm.currency}:{alarm.detail}")

    def error(self, book: str, stage: str, exc: Exception) -> None:
        if self.runtime is not None:
            message = f"{type(exc).__name__}: {exc}"
            self.runtime.emit("book_stage_failed", "error", book=book, stage=stage, error=message)


def _tail_since(history: pd.DataFrame, since: datetime | None) -> pd.DataFrame:
    """The bars of ``history`` from ``since`` on (the downtime of a restored position); empty when ``since`` is None."""
    if since is None:
        return history.iloc[:0]
    stamp = pd.Timestamp(since)
    index = pd.DatetimeIndex(history.index)
    if index.tz is None:
        stamp = stamp.tz_convert("UTC").tz_localize(None)
    return history[index >= stamp]


def _err(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _stamp_of(version: str, label: LoadedLabelConfig) -> EntryStamp:
    """What a position records of the model that opened it."""
    return EntryStamp(version, label.sha256, label.cfg.horizon_min)


def _book_aliases(configs: Sequence[BookConfig]) -> dict[str, str]:
    """book -> registry alias of its model: ``registry:<alias>`` books, and the random books that match one of them
    (their policy, label and questions are the source book's)."""
    alias: dict[str, str] = {}
    for b in configs:
        prefix, sep, name = b.decider.partition(":")
        if sep and prefix == "registry" and name:
            alias[b.name] = name
    for b in configs:
        if b.decider == "random" and b.match_frequency_of in alias:
            alias[b.name] = alias[b.match_frequency_of]
    return alias


def _fill_model_version(params: FillModelParams) -> str:
    import hashlib

    return "fill@" + hashlib.sha256(params.model_dump_json().encode()).hexdigest()[:8]


# ------------------------------------------------------------------------------------------------ runtime


@dataclass(repr=False)
class Runtime:
    """The assembled process. ``Runtime.start`` builds it (and refuses to), ``run`` drives it until it stops."""

    cfg: AppConfig
    mode: Mode
    deps: Deps
    champion: Champion
    models: dict[str, Champion]
    perms: KeyPermissionsInfo
    registry: SpotRegistry
    fees: FeeProvider
    ledger: PositionLedger
    system: SystemGuards
    kill_switch: KillSwitch
    gates: _EntryGates
    life: Lifecycle
    manager: ShadowBookManager
    books: list[Book]
    feed: MarketFeed
    bar_service: BarRunner
    writer: BarStoreWriter
    bar_queue: asyncio.Queue[BarClosed]
    sentiment: SentimentRunner | None
    stack: AsyncExitStack
    marks: dict[str, Decimal]
    last_bar_t: dict[str, datetime]
    damage: list[tuple[datetime, datetime | None]]
    restores: dict[str, BookRestore] = field(default_factory=dict)
    restore_reports: dict[str, RestoreReport] = field(default_factory=dict)
    events: EventLog | None = None
    ops: OpsRecorder | None = None
    telemetry_errors: TelemetryErrors = field(default_factory=TelemetryErrors)
    extra_tasks: list[TaskSpec] = field(default_factory=list)
    on_event: EventHook | None = None
    decider_ratio_1h: float | None = None
    started_at: datetime = field(default_factory=utc_now)
    book_deps: BookDeps | None = None  # what the books were built with (a reload builds the new RiskManagers from it)
    book_alias: dict[str, str] = field(default_factory=dict)  # book -> the registry alias whose model it runs
    fill_version: str | None = None  # ``fill@<hash>`` of the fill model the books run
    forced_test_books: frozenset[str] = frozenset()  # random books with a forced BUY rate: not gated by the model
    config_path: str | None = None  # the YAML the process was started with: the learner child reads the same one
    learner_timeout_s: float = DEFAULT_LEARNER_TIMEOUT_S
    jobs: list[DailyJob] = field(default_factory=list)
    learner: LearnerLauncher | None = None
    reload_log: list[dict[str, Any]] = field(default_factory=list)  # what the reloads did (status, tests)
    _window: RatioWindow = field(default_factory=RatioWindow, init=False)
    _turn: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)  # bars and reload swaps never interleave
    _restart_notified: set[str] = field(default_factory=set, init=False)

    # ------------------------------------------------------------------------------------------ start

    @classmethod
    async def start(cls, cfg: AppConfig, env: Mapping[str, str], deps: Deps | None = None) -> Runtime:
        deps = deps or Deps()
        mode = resolve_mode(cfg.execution, env)
        if mode != "dry":
            raise LiveModeUnavailable(
                "execution.mode is live and CHRONOS_LIVE_CONFIRM is set, but this build has no live path (P2/P3 "
                "run dry only): unset CHRONOS_LIVE_CONFIRM or set execution.mode: dry"
            )
        stack = AsyncExitStack()
        try:
            return await cls._build(cfg, env, deps, mode, stack)
        except BaseException:
            await stack.aclose()
            raise

    @classmethod
    async def _build(
        cls, cfg: AppConfig, env: Mapping[str, str], deps: Deps, mode: Mode, stack: AsyncExitStack
    ) -> Runtime:
        clock = deps.clock
        products = list(cfg.market.products)
        data_dir = Path(cfg.paths.data_dir)
        models_dir = Path(cfg.paths.models_dir)
        write_json_atomic(
            data_dir / HEARTBEAT_FILE,
            {
                "v": HEARTBEAT_SCHEMA,
                "ts": clock().timestamp(),
                "ok": False,
                "phase": "starting",
                "reasons": ["starting"],
            },
        )  # a heartbeat left by a previous run must not make a starting process look healthy
        log.info("agent_starting", mode=mode, products=products)

        # 0. telemetry first: the start itself leaves events (it is closed last, so the shutdown does too)
        errors = TelemetryErrors()
        events: EventLog | None = None
        ops: OpsRecorder | None = None
        tcfg = cfg.telemetry
        if tcfg.enabled:
            secrets = collect_secrets(cfg, env)
            events = EventLog(
                Path(tcfg.events_dir or data_dir / "events"), queue_max=tcfg.events_queue_max, clock=clock,
                errors=errors, redact=lambda v: redact(v, secrets), retention_days=tcfg.events_retention_days,
            )  # fmt: skip
            events.start()
            stack.callback(events.stop)
            LOG_FORWARDER.target = events
            stack.callback(_detach_forwarder, events)
            ops = OpsRecorder(
                Path(tcfg.ops_dir or data_dir / "reports" / "ops"), clock=clock, sleep=deps.sleep,
                interval_s=tcfg.ops_interval_s, errors=errors,
            )  # fmt: skip

        # 1. read-only exchange, key, verified spot products
        exchange = deps.exchange(cfg.coinbase)
        perms = await exchange.get_api_key_permissions()
        check_key_permissions(mode, perms)
        spot = await SpotRegistry.load(exchange, products)

        # 2. models and the books that can start
        champion = deps.champion(models_dir, cfg.decision.model)
        for product in products:
            try:
                champion.label.spread_for(product, clock())
            except ValueError as exc:
                raise StartupError(f"model {champion.version}: {exc}") from exc
        configs, models = _plan_books(cfg, champion, models_dir, deps.model_alias)

        # 3. fees
        fill_cfg = cfg.dry_run.fill_model

        async def fetch_fees() -> tuple[float, float]:
            rates = await exchange.get_fee_rates()
            return rates.maker_bps, rates.taker_bps

        fees = FeeProvider(fill_cfg.fallback_fees, fetch_fees, refresh_s=cfg.execution.fee_refresh_s, clock=clock)
        await fees.refresh()

        # 4. the dry ledger (opened and migrated), then the incremental backfill and the whole history into the
        #    engines (worker threads); the bars since the oldest open position are kept for the gap resolution
        ledger = await PositionLedger.open(cfg.dry_run.db_path, "dry")
        stack.push_async_callback(ledger.close)
        gap_from = min(
            (p.opened_at for b in configs for p in ledger.long_positions(b.name) if p.opened_at is not None),
            default=None,
        )
        source = deps.candle_source()
        await deps.backfill(cfg, source, clock)
        root_1m = Path(cfg.paths.data_dir) / "bars" / "1m"

        def warm(product: str) -> tuple[str, IndicatorEngine, datetime, float, pd.DataFrame]:
            history = deps.history(root_1m, product)
            if history.empty:
                raise StartupError(f"no 1m history for {product} under {root_1m}: run the backfill")
            engine = IndicatorEngine(product)
            engine.warm_up(history)  # the WHOLE history: the anchor of the recurrences (spec 2.2.1.b)
            return (
                product, engine, history.index[-1].to_pydatetime(), float(history["close"].iloc[-1]),
                _tail_since(history, gap_from),
            )  # fmt: skip

        warmed = await asyncio.gather(*(asyncio.to_thread(warm, p) for p in products))

        # 5. kill switch, one set of account guards per book, the state of every book read back from the ledger
        system = SystemGuards(cfg.risk.kill_switch, clock)
        guards = {b.name: AccountGuards(b.name, cfg.risk.kill_switch, cfg.risk, clock) for b in configs}
        kill_switch = KillSwitch(system, guards, clock)
        await kill_switch.load(ledger.connection)
        life = Lifecycle(system, clock)
        if any(row.guard == "decider_health" for row in system.state_rows()):
            # the guard was latched when the previous run ended: keep it until this process has seen a healthy hour
            life.set_fault("decider", "latched by the previous run: waiting for healthy decisions")
        restores = {b.name: await load_book_restore(ledger, b.name, spot, b.starting_quote) for b in configs}

        # 6. the feed, the shared parts and the books (ONE implementation: modules/dryrun/books.py)
        params = (
            FillModelParams.from_json(Path(fill_cfg.path))
            if Path(fill_cfg.path).is_file()
            else FillModelParams(
                latency_ms=fill_cfg.latency_ms,
                queue_haircut=fill_cfg.queue_haircut,
                stop_extra_slip_bps=fill_cfg.stop_extra_slip_bps,
                slippage_buffer_bps=cfg.risk.slippage_buffer_bps,
            )  # fmt: skip
        )
        damage: list[tuple[datetime, datetime | None]] = []
        recorder: WsRecorder | None = None
        if cfg.dry_run.recording.enabled:
            rec = cfg.dry_run.recording
            recorder = WsRecorder(Path(rec.dir), retention_days=rec.retention_days)
            recorder.start()
            stack.push_async_callback(partial(asyncio.to_thread, recorder.stop))
        # the trade callback is handed to the stream only in the tick fill mode (a stream factory that does not know it
        # keeps working in the bar mode)
        relay = _TickRelay(damage)
        tick_kwargs: dict[str, Any] = {"on_trades": relay.on_trades} if fill_cfg.mode == "tick" else {}
        feed = deps.stream(products, None if recorder is None else recorder.record, relay.on_damage, **tick_kwargs)

        gates = _EntryGates(life, kill_switch)
        decision_log = DecisionLog.under(cfg.paths.data_dir)
        stack.push_async_callback(decision_log.aclose)
        remote: SystemOneClient | None = None
        if cfg.decision.backend != "baseline":
            key = cfg.decision.remote_api_key(env)
            remote = SystemOneClient(
                cfg.decision.remote.base_url, model=champion.version, timeout_ms=cfg.decision.remote.timeout_ms,
                api_key=None if key is None else key.get_secret_value(),
            )  # fmt: skip
            stack.push_async_callback(remote.aclose)
        sentiment: SentimentRunner | None = None
        if champion.sentiment_available:  # only a model trained with the sentiment may see it (train/serve skew)
            sentiment = await deps.sentiment(cfg, env, stack)
        else:
            log.info("sentiment_not_used", model=champion.version, reason="model_card.sentiment_available is false")

        late = _Late(guards, clock)
        observed: dict[str, _ObservedGateway] = {}

        def wrap(name: str, gateway: ExecutionGateway) -> ExecutionGateway:
            observed[name] = _ObservedGateway(gateway, ledger, name, life.fatal)
            return observed[name]

        ledger.add_close_listener(late.closed)
        book_deps = BookDeps(
            ledger=ledger, clock=clock, registry=spot, fees=fees, kill_switch=gates, quotes=_FeedQuotes(feed),
            sink=decision_log, risk=cfg.risk, fallback_fees=fill_cfg.fallback_fees,
            entry_timeout=cfg.execution.entry_timeout_s, fill_params=params, decision=cfg.decision,
            bars=cfg.market.bars, products=tuple(products), sentiment=sentiment, remote=remote,
            on_execution=late.execution, on_alarm=late.alarm, wrap_gateway=wrap, restore=restores,
            fill_mode=fill_cfg.mode, order_books=feed.books, candles=source,
        )  # fmt: skip
        try:
            shadow_books = build_books(configs, book_deps, lambda alias: models[alias].book_model())
        except BookConfigError as exc:
            raise StartupError(f"the books cannot be built: {exc}") from exc
        except ValueError as exc:  # an account that cannot be rebuilt from the ledger
            raise StartupError(f"a book cannot be restored from the dry ledger: {exc}") from exc
        base_of = {p: spot.meta(p).base_currency for p in products}
        untunable = {m.version: m.no_entry_reason for m in models.values() if m.no_entry_reason is not None}
        # a random book with a forced BUY rate is a TEST aid (``BookConfig.forced_buy_rate``): it does not benchmark the
        # model, so the model's untunable verdict is not its own. Every other gate still applies to it.
        forced_test_books = {c.name for c in configs if c.forced_buy_rate is not None}
        fill_version = _fill_model_version(params)
        books: list[Book] = []
        for sb in shadow_books:
            sb.spread = _spread_source(feed, sb.model.label if sb.model is not None else champion.label)
            if sb.model is not None and sb.model.version in untunable and sb.name not in forced_test_books:
                gates.no_entry[sb.name] = str(untunable[sb.model.version])
            stamp = None if sb.model is None else _stamp_of(sb.model.version, sb.model.label)
            ledger.set_trade_info(
                sb.name, model_version=sb.model_version, fill_model_version=fill_version,
                label_sha256=None if stamp is None else stamp.label_sha256,
                horizon_min=None if stamp is None else stamp.horizon_min,
            )  # fmt: skip
            if stamp is not None:  # open positions of a build that did not record their model take the book's
                stamped = await ledger.stamp_open_positions(sb.name, stamp)
                if stamped:
                    log.warning("positions_stamped_with_the_book_model", book=sb.name, products=stamped,
                                model=stamp.model_version, hint="opened before the model was recorded")  # fmt: skip
            engine = sb.engine
            watchdog: Watchdog | None = None
            if isinstance(engine, DecisionEngine):
                for product, indicators, last_ts, last_close, _tail in warmed:
                    engine.adopt(product, copy.deepcopy(indicators), last_ts, last_close)
                if sb.kind in MODEL_BOOK_KINDS and sb.model is not None:
                    watchdog = Watchdog(
                        book=sb.name, ledger=ledger, gateway=observed[sb.name], cfg=cfg.risk.watchdog,
                        horizon_min=sb.model.label.cfg.horizon_min, atr_1h=partial(_atr_1h, engine), clock=clock,
                    )  # fmt: skip
            books.append(Book(sb, guards[sb.name], observed.get(sb.name), watchdog, base_of))
        manager = ShadowBookManager(shadow_books, on_error=late.error, restore=restores, tape=book_deps.tape)
        relay.manager = manager

        finalizer = BarFinalizer(products, cfg.market.bars.rest_delay_s, cfg.market.bars.rest_max_delay_s)
        complete: dict[str, datetime] = {}
        marks: dict[str, Decimal] = {}
        last_bar_t: dict[str, datetime] = {}
        for product, _indicators, last_ts, last_close, _tail in warmed:
            finalizer.seed(product, last_ts, last_close)
            complete[product] = last_ts
            marks[product] = Decimal(repr(last_close))
            last_bar_t[product] = last_ts + MINUTE
            system.observe_ticker(product, clock())  # a feed that never delivers a ticker trips stale_data
        writer = BarStoreWriter(BarStore(root_1m), complete, cfg.market.bars.store_flush_minutes)
        bar_queue: asyncio.Queue[BarClosed] = asyncio.Queue()
        bar_service = deps.bar_service(finalizer, bar_queue, source, writer, cfg, clock)

        runtime = cls(
            cfg=cfg, mode=mode, deps=deps, champion=champion, models=models, perms=perms, registry=spot, fees=fees,
            ledger=ledger, system=system, kill_switch=kill_switch, gates=gates, life=life, manager=manager,
            books=books, feed=feed, bar_service=bar_service, writer=writer, bar_queue=bar_queue,
            sentiment=sentiment, stack=stack, marks=marks, last_bar_t=last_bar_t, damage=damage, restores=restores,
            events=events, ops=ops, telemetry_errors=errors, started_at=clock(), book_deps=book_deps,
            book_alias=_book_aliases(configs), forced_test_books=frozenset(forced_test_books),
            fill_version=fill_version,
        )  # fmt: skip
        learner_cfg = cfg.dry_run.learner
        runtime.jobs = [
            DailyJob("learner", learner_cfg.run_at_utc, clock()),
            DailyJob("reload", learner_cfg.reload_at_utc, clock()),
        ]
        late.runtime = runtime
        life.fatal_hooks.append(runtime.on_fatal)
        await runtime._restore_books({p: tail for p, _e, _t, _c, tail in warmed})
        if ops is not None:
            ops.start()
            rates = fees.current
            ops.set_fees(rates.maker_bps, rates.taker_bps, "assumed" if rates.assumed else "account")
        return runtime

    async def _restore_books(self, tails: Mapping[str, pd.DataFrame]) -> None:
        """A restart: every book goes on from its ledger, then the bars of the downtime are fed to the brackets that
        were restored. Anything that fails stops the start: a book on a wrong account must not run."""
        label = self.champion.label

        def bars_since(product: str, since: datetime) -> list[BarEvent]:
            frame = _tail_since(tails[product], since)
            stamps = pd.DatetimeIndex(frame.index).to_pydatetime()
            o, h, lo, c, v = (frame[k].astype(float).tolist() for k in ("open", "high", "low", "close", "volume"))
            return [
                BarEvent(product, t, o[i], h[i], lo[i], c[i], v[i], label.spread_for(product, t))
                for i, t in enumerate(stamps)
            ]

        try:
            reports = await self.manager.restore()
            self.restore_reports = dict(reports)
            self.ledger.gap_resolution = True  # the trades closed from here on are the ones the downtime decided
            try:
                closed = await self.manager.resolve_gap(reports, bars_since)
            finally:
                self.ledger.gap_resolution = False
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            raise StartupError(f"the books cannot be restored from the dry ledger: {detail}") from exc
        for name, report in reports.items():
            self.emit(
                "book_restored", "warning" if report.unprotected or report.cancelled else "info", book=name,
                cancelled=list(report.cancelled), brackets_restored=list(report.restored),
                brackets_placed=list(report.placed), unprotected=list(report.unprotected),
                closed_in_the_gap=closed.get(name, 0),
            )  # fmt: skip
            log.info(
                "book_restored", book=name, cancelled=len(report.cancelled), restored=list(report.restored),
                placed=list(report.placed), unprotected=list(report.unprotected), closed_in_gap=closed.get(name, 0),
            )  # fmt: skip

    # ------------------------------------------------------------------------------------------- events

    def emit(self, event: str, severity: str = "info", *, book: str | None = None, **data: Any) -> None:
        """Log an event, hand it to the hook and to the event log. Nothing here can raise into the caller."""
        getattr(log, severity)(event, **({"book": book} if book else {}), **data)
        product = data.pop("product", None)
        if self.events is not None:
            self.events.emit(event, severity, book=book, product=product, data=data)
        if self.on_event is not None:
            try:
                self.on_event(event, severity, {**data, **({"book": book} if book else {})})
            except Exception as exc:  # a broken observer never reaches the trading path
                log.error("event_hook_failed", name=event, error=f"{type(exc).__name__}: {exc}")

    def _emit(self, event: str, severity: str = "info", **data: Any) -> None:
        self.emit(event, severity, **data)

    def request_stop(self, reason: str) -> None:
        self.life.request_stop(reason)

    def on_fatal(self, exc: BaseException, exit_code: int) -> None:
        if isinstance(exc, DryRunViolation) and self.ops is not None:
            self.ops.violation()
        if self.events is not None:
            kind = "dry_run_violation" if isinstance(exc, DryRunViolation) else "fatal_error"
            self.events.emit(kind, "critical", data={"error": f"{type(exc).__name__}: {exc}", "exit_code": exit_code})

    def on_trade_closed(self, trade: ClosedTrade) -> None:
        """A position of a book closed (the ledger's ``closed_trades`` listener): the loss streak and the event."""
        book = self.books_by_name().get(trade.book)
        if book is not None:
            book.guards.observe_trade_closed(self.deps.clock(), trade.net_quote, _exit_reason(trade.exit_reason))
        self.emit(
            "position_closed", "info", book=trade.book, product=trade.product_id, trade_id=trade.trade_id,
            exit_reason=trade.exit_reason, net_quote=str(trade.net_quote), net_bps=trade.net_bps,
            held_min=trade.held_min,
        )  # fmt: skip

    def on_execution(self, book: str, report: ExecutionReport) -> None:
        """The end of a gateway call of a book (an entry or an exit)."""
        result = report.result
        if report.kind == "entry" and isinstance(result, EntryResult):
            filled = result.status == "filled"
            target = self.books_by_name().get(book)
            model = None if target is None else target.book.model
            maker = model is not None and model.label.cfg.entry_mode == "maker"
            if self.ops is not None and result.status in ("filled", "no_fill"):
                self.ops.observe_entry(maker=maker, filled=filled)
            if filled:
                self.emit(
                    "position_opened", "info", book=book, product=report.product_id, decision_id=report.decision_id,
                    size=str(result.filled_size), avg_price=None if result.avg_price is None else str(result.avg_price),
                    bracket=result.bracket,
                )  # fmt: skip

    def books_by_name(self) -> dict[str, Book]:
        return {b.name: b for b in self.books}

    # ------------------------------------------------------------------------------------------- tasks

    def task_specs(self) -> list[TaskSpec]:
        specs = [
            TaskSpec("market_stream", self.feed.run),
            TaskSpec("bar_service", self.bar_service.run),
            TaskSpec("bar_pump", self._bar_pump),
            TaskSpec("tickers", self._ticker_loop),
            TaskSpec("health", self._health_loop),
            TaskSpec("heartbeat", self._heartbeat_loop),
            TaskSpec("fees", self._fee_loop, critical=False),
            TaskSpec("schedule", self._schedule_loop, critical=False),  # 00:15 learner child, 00:30 model reload
        ]
        if self.manager.tick_mode:
            specs.append(TaskSpec("tick_fills", self.manager.run_ticks))
        for book in self.books:
            if book.watchdog is not None:
                specs.append(TaskSpec(f"watchdog:{book.name}", book.watchdog.run))
        if self.sentiment is not None:
            specs.append(TaskSpec("sentiment", self.sentiment.run, critical=False))
        if self.cfg.telemetry.enabled:  # sez. 6.3.5: none of these can stop the process, whatever happens inside
            tcfg = self.cfg.telemetry
            specs.append(TaskSpec("telemetry_status", StatusWriter(
                Path(tcfg.dir), self.status, interval_s=tcfg.status_interval_s, clock=self.deps.clock,
                sleep=self.deps.sleep, errors=self.telemetry_errors,
            ).run, critical=False))  # fmt: skip
            specs.append(TaskSpec("telemetry_equity", EquityWriter(
                Path(tcfg.equity_dir or Path(self.cfg.paths.data_dir) / "equity"), self._equity_rows,
                interval_s=tcfg.equity_interval_s, clock=self.deps.clock, sleep=self.deps.sleep,
                errors=self.telemetry_errors,
            ).run, critical=False))  # fmt: skip
            specs.append(TaskSpec("telemetry_ops", self._ops_loop, critical=False))
        return [*specs, *self.extra_tasks]

    async def _supervised(self, spec: TaskSpec) -> None:
        """Run one task; nothing it raises reaches the ``TaskGroup`` (it would cancel its siblings unannounced)."""
        try:
            await spec.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.life.task_failed(spec.name, exc, critical=spec.critical)
        else:
            if not self.life.stopping:  # these tasks are loops: returning is a bug
                self.life.task_failed(spec.name, RuntimeError("the task returned"), critical=spec.critical)

    async def run(self) -> int:
        """Run until stopped (signal, fatal error); return the exit code. Never raises for a task failure."""
        self._emit(
            "agent_started", mode=self.mode, books=[b.name for b in self.books], model=self.champion.version,
            version=AGENT_VERSION, entries_enabled=self.champion.no_entry_reason is None,
            sentiment_in_state=self.champion.sentiment_available,
            models={b.name: b.book.model_version for b in self.books},
            key={"can_view": self.perms.can_view, "can_trade": self.perms.can_trade,
                 "can_transfer": self.perms.can_transfer},
        )  # fmt: skip
        try:
            async with asyncio.TaskGroup() as group:
                handles = [group.create_task(self._supervised(s), name=s.name) for s in self.task_specs()]

                async def terminator() -> None:
                    await self.life.stop.wait()
                    for handle in handles:
                        handle.cancel()

                group.create_task(terminator(), name="terminator")
        finally:
            await self._shutdown()
        return self.life.exit_code

    # ---------------------------------------------------------------------------------------------- schedule

    def _learner_argv(self) -> list[str]:
        return [] if self.config_path is None else ["--config", self.config_path]

    async def _schedule_loop(self) -> None:
        """The daily clock: poll the (injected) clock and run what is due. Nothing here can end the process."""
        while True:
            await self.run_scheduled(self.deps.clock())
            await self.deps.sleep(SCHEDULE_POLL_S)

    async def run_scheduled(self, now: datetime) -> list[str]:
        """Run the daily jobs that are due at ``now`` (each fires once per UTC day). The learner is launched as a child
        process and NOT awaited; the reload re-resolves the registry aliases. A job that raises is an event."""
        ran: list[str] = []
        for job in self.jobs:
            if not job.due(now):
                continue
            job.mark(now)
            ran.append(job.name)
            try:
                if job.name == "learner":
                    self._launch_learner(now)
                elif job.name == "reload":
                    await self.reload_models()
            except Exception as exc:  # the schedule never reaches the trading path
                self.emit(f"{job.name}_job_failed", "error", error=_err(exc))
        return ran

    def _launch_learner(self, now: datetime) -> bool:
        if self.learner is None:
            self.learner = LearnerLauncher(
                runner=self.deps.run_learner, emit=self.emit, argv=self._learner_argv(),
                timeout_s=self.learner_timeout_s,
            )
        return self.learner.launch(now)

    # ------------------------------------------------------------------------------------- model reload

    def _swap_problem(self, target: Champion) -> str | None:
        """Why ``target`` cannot be served by the books that run now, or ``None``. (Files, sha256, label_config,
        questions and flat columns were already checked when the model was loaded.)"""
        if target.sentiment_available != self.champion.sentiment_available:
            return "the model and the running champion differ on the sentiment (the feed is wired for one of them)"
        try:
            for product in self.cfg.market.products:
                target.label.spread_for(product, self.deps.clock())
        except ValueError as exc:
            return str(exc)
        return None

    async def reload_models(self) -> list[dict[str, str]]:
        """00:30 UTC: re-resolve the registry alias of every book. A book whose alias now points at another version
        switches to it at once, between two bars (``_turn``): decider, policy, label, questions and RiskManager are
        replaced together for all the books of the alias. Positions already open keep their own bracket, horizon and
        label_config; the SELL of a model with another label_config does not apply to them (engine).

        FAIL CLOSED: a version that cannot be loaded or served (missing file, sha256 that does not match, an
        incompatible label_config or questions, another sentiment) leaves the books on the version they run, as an
        event. A book never ends up without a model. Returns the changes made."""
        if self.book_deps is None:
            return []
        models_dir = Path(self.cfg.paths.models_dir)
        changes: list[dict[str, str]] = []
        for alias in sorted(set(self.book_alias.values())):
            current = self.models.get(alias)
            if current is None:
                continue
            if alias == CHAMPION_ALIAS and not self.cfg.decision.model.startswith("registry:"):
                continue  # the champion book runs a model named in the config: the registry does not move it
            try:
                target = await asyncio.to_thread(self.deps.model_alias, models_dir, alias)
            except Exception as exc:  # StartupError (damaged registry or version), OSError...
                self.emit("model_reload_failed", "error", alias=alias, kept=current.version, error=_err(exc))
                continue
            if target is None or target.version == current.version:
                continue
            problem = self._swap_problem(target)
            books = [b for b in self.books if self.book_alias.get(b.name) == alias and b.engine is not None]
            swaps = []
            if problem is None:
                try:
                    swaps = [prepare_book_swap(b.book, self.book_deps, target.book_model()) for b in books]
                except Exception as exc:
                    problem = _err(exc)
            if problem is not None:
                self.emit(
                    "model_reload_failed", "error", alias=alias, kept=current.version, version=target.version,
                    error=problem,
                )  # fmt: skip
                continue
            op, reason = self._alias_record(models_dir, alias)
            async with self._turn:  # not while a batch of bars is being decided
                apply_book_swaps(swaps)
                self.models[alias] = target
                if alias == CHAMPION_ALIAS:
                    self.champion = target
                for b in books:
                    self._after_swap(b, target)
            for b in books:
                change = {"book": b.name, "alias": alias, "from": current.version, "to": target.version}
                changes.append(change)
                self.reload_log.append({**change, "op": op, "reason": reason, "at": _iso(self.deps.clock())})
                self.emit(
                    "model_version_changed", "info", book=b.name,
                    **{"alias": alias, "from": current.version, "to": target.version, "op": op, "reason": reason},
                )  # fmt: skip
        await self._warn_books_that_need_a_restart(models_dir)
        return changes

    def _after_swap(self, book: Book, target: Champion) -> None:
        """What lives outside the engine and follows the model of a book (called under ``_turn``)."""
        stamp = _stamp_of(target.version, target.label)
        book.book.spread = _spread_source(self.feed, target.label)
        self.ledger.set_trade_info(
            book.name, model_version=stamp.model_version, fill_model_version=self.fill_version,
            label_sha256=stamp.label_sha256, horizon_min=stamp.horizon_min,
        )  # fmt: skip
        if book.watchdog is not None:
            book.watchdog.set_horizon_min(stamp.horizon_min)
        if target.no_entry_reason is not None and book.name not in self.forced_test_books:
            self.gates.no_entry[book.name] = str(target.no_entry_reason)
        else:
            self.gates.no_entry.pop(book.name, None)

    @staticmethod
    def _alias_record(models_dir: Path, alias: str) -> tuple[str | None, str | None]:
        """The last operation recorded for the alias (``promote`` / ``rollback`` and its reason), if readable."""
        try:
            state = registry_of(models_dir).state(alias)
        except RegistryError:
            return None, None
        return state.op, state.reason or None

    async def _warn_books_that_need_a_restart(self, models_dir: Path) -> None:
        """A configured ``registry:<alias>`` book that did not start (its alias was unset) cannot start without a
        restart: say so once per version, the learner has just proposed it."""
        running = {b.name for b in self.books}
        for b in self.cfg.dry_run.books:
            prefix, sep, alias = b.decider.partition(":")
            if b.name in running or not (sep and prefix == "registry"):
                continue
            try:
                found = await asyncio.to_thread(self.deps.model_alias, models_dir, alias)
            except Exception:  # a damaged alias is reported by the reload itself, not here
                continue
            if found is None:
                continue
            version = found.version
            key = f"{b.name}:{version}"
            if key not in self._restart_notified:
                self._restart_notified.add(key)
                self.emit("book_needs_restart", "warning", book=b.name, alias=alias, version=version)

    # -------------------------------------------------------------------------------------------- pump

    async def _bar_pump(self) -> None:
        while True:
            batch = [await self.bar_queue.get()]
            while True:
                try:
                    batch.append(self.bar_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            await self.process_bars(batch)

    async def process_bars(self, events: Sequence[BarClosed]) -> dict[str, list[Processed]]:
        """Published bars: the guards first, then every book (the venue fills at the bar, then the engine decides)."""
        for event in events:
            product, bar = event.product_id, event.bar
            self.marks[product] = Decimal(repr(bar.close))
            self.last_bar_t[product] = event.t
            self.system.observe_bar_published(product, event.t, event.published_at)
            if self.ops is not None:
                self.ops.observe_bar(event)
                self.ops.observe_spread(product, self.feed.spread_bps(product))
        async with self._turn:  # a model swap happens between two bars, never inside one
            results = await self.manager.on_bars(events)
        for book in self.books:
            self._observe_account(book)
        self._outcome_events(results)
        return results

    def _outcome_events(self, results: Mapping[str, Sequence[Processed]]) -> None:
        """``decision_outcome`` (spec 6.3 D14) for every decision that is not a HOLD."""
        for name, processed in results.items():
            for p in processed:
                if p.decision is None or p.decision.action == "HOLD":
                    continue
                intent = p.intent
                outcome = "skip" if intent is None else ("intent" if isinstance(intent, OrderIntent) else "exit")
                data: dict[str, Any] = {
                    "decision_id": p.decision_id, "final": p.decision.action, "outcome": outcome,
                    "skip_reason": p.skip_reason,
                }  # fmt: skip
                if isinstance(intent, OrderIntent):
                    data |= {
                        "intent_id": intent.intent_id, "ev_bps": intent.risk.ev_bps,
                        "p_breakeven": intent.risk.p_breakeven, "base_size": str(intent.base_size),
                    }  # fmt: skip
                self.emit("decision_outcome", "info", book=name, product=p.product_id, **data)

    def _observe_account(self, book: Book) -> None:
        try:
            equity = book.account.equity(self.marks, book.base_of)
        except KeyError:  # a base balance without a mark price: wait for the bar of that product
            return
        exposure = sum(
            (p.base_qty * self.marks[p.product_id] for p in self.ledger.long_positions(book.name)
             if p.product_id in self.marks),
            Decimal(0),
        )  # fmt: skip
        book.guards.observe_equity(self.deps.clock(), equity, exposure)

    async def pump_tickers(self, seen: dict[str, Quote]) -> None:
        """New last prices to the stale-data guard and to the watchdogs (the stream has no callback for tickers).
        ``seen`` remembers the quote already delivered per product; the replay calls this once per simulated minute."""
        for product, quote in list(self.feed.quotes.items()):
            if seen.get(product) is quote:
                continue
            seen[product] = quote
            self.system.observe_ticker(product, quote.ts)
            for book in self.books:
                if book.watchdog is None:
                    continue
                try:
                    await book.watchdog.on_quote(quote)
                except Exception as exc:  # one failed evaluation: log it, the next ticker retries
                    log.error("watchdog_ticker_failed", product=product, error=f"{type(exc).__name__}: {exc}")
        await self.manager.pump_ticks()  # tick fill mode: stops waiting for liquidity, closed data gaps, queued fills

    async def _ticker_loop(self) -> None:
        seen: dict[str, Quote] = {}
        while True:
            await self.deps.sleep(TICKER_POLL_S)
            await self.pump_tickers(seen)

    async def _fee_loop(self) -> None:
        while True:
            await self.fees.refresh()
            if self.ops is not None:
                rates = self.fees.current
                self.ops.set_fees(rates.maker_bps, rates.taker_bps, "assumed" if rates.assumed else "account")
            await self.deps.sleep(min(FEE_POLL_S, self.cfg.execution.fee_refresh_s))

    # ------------------------------------------------------------------------------------------ health

    def evaluate_decider_health(self) -> float | None:
        """Decider-health guard (spec 4.1.4): more than ``max_fallback_ratio_1h`` of the decisions of the last hour
        (all the model books together) without an answer of the configured backend (fallback to the baseline, no
        answer, or an error of the pipeline) blocks every entry. Read from the engines' ``stats``. With fewer than
        ``MIN_DECISIONS_FOR_HEALTH`` decisions in the window the state does not change (a fault latched by a previous
        run stays until a healthy hour of this one)."""
        total = bad = 0
        for book in self.books:
            if book.engine is None:
                continue
            st = book.engine.stats
            total += st.decisions + st.errors
            bad += st.fallbacks + st.unavailable + st.errors
        ratio, n = self._window.update(self.deps.clock(), total, bad)
        self.decider_ratio_1h = ratio
        limit = self.cfg.risk.kill_switch.max_fallback_ratio_1h
        if ratio is not None and n >= MIN_DECISIONS_FOR_HEALTH:
            if ratio > limit:
                msg = f"{ratio:.0%} of the last {n} decisions failed or fell back (limit {limit:.0%})"
                self.life.set_fault("decider", msg)
            else:
                self.life.clear_fault("decider")
        return ratio

    async def _save_kill_switch(self) -> None:
        await self.kill_switch.save(self.ledger.connection)

    async def _health_loop(self) -> None:
        while True:
            await self.deps.sleep(HEALTH_EVERY_S)
            self.evaluate_decider_health()
            try:
                await self._save_kill_switch()
            except Exception as exc:  # the state is saved again at the next turn and at the shutdown
                log.error("kill_switch_save_failed", error=f"{type(exc).__name__}: {exc}")

    def unhealthy_reasons(self, now: datetime) -> list[str]:
        reasons: list[str] = []
        if self.life.stopping:
            reasons.append("stopping")
        reasons += [f"fault:{k}" for k in sorted(self.life.faults)]
        stale = timedelta(seconds=self.cfg.risk.kill_switch.stale_bar_s)
        reasons += [f"stale_bar:{p}" for p, t in sorted(self.last_bar_t.items()) if now - t > stale]
        last = self.feed.last_message_at
        if last is None or (now - last).total_seconds() > WS_MAX_AGE_S:
            reasons.append("ws_silent")
        return reasons

    # ------------------------------------------------------------------------------------------ status

    def status(self) -> dict[str, Any]:
        """The in-memory state, JSON-ready: the body of ``telemetry/status.json`` (spec 6.3.1, ``chronos.status.v1``
        with the blocks this build has; a block without its component is ``null``) and of the heartbeat. It reads
        memory only: balances and positions are the ones of the ``BalanceBook`` and the ledger, not a recomputation."""
        now = self.deps.clock()
        engines = {b.name: b.engine for b in self.books if b.engine is not None}
        stats = {n: {k: v for k, v in vars(e.stats).items() if isinstance(v, int)} for n, e in engines.items()}
        names = [b.name for b in self.books]
        last_msg = self.feed.last_message_at
        feed_stats = getattr(self.feed, "stats", None)
        rates = self.fees.current
        model_versions = {b.name: b.book.model_version for b in self.books}
        blocked = {"system": self.system.blocked_reason(now), **{n: self.kill_switch.entry_blocked(n) for n in names}}
        ops = self.ops
        return {
            "agent": {
                "version": AGENT_VERSION, "started_at": _iso(self.started_at),
                "uptime_s": round((now - self.started_at).total_seconds(), 1), "mode": self.mode, "books": names,
                "models": model_versions, "label_config_sha256": self.champion.label.sha256,
                "api_key": {"can_view": self.perms.can_view, "can_trade": self.perms.can_trade,
                            "can_transfer": self.perms.can_transfer},
            },
            "feeds": {
                "ws": {
                    "connected": last_msg is not None and (now - last_msg).total_seconds() <= WS_MAX_AGE_S,
                    "last_msg_age_s": None if last_msg is None else round((now - last_msg).total_seconds(), 1),
                    "reconnects": getattr(feed_stats, "reconnects", None),
                    "seq_gaps": getattr(feed_stats, "gaps", None),
                    "damage_intervals": len(self.damage),
                },
                "rest": None,
            },
            "bars": {
                p: {"last_ts_bar_close": _iso(t), "age_s": round((now - t).total_seconds(), 1),
                    "last_lag_s": self.system.bar_lag_s(p), "publication_lag_s": self.system.bar_lag_s(p)}
                for p, t in sorted(self.last_bar_t.items())
            },
            "kill_switch": [
                {"book": b.name, "guard": g.guard, "tripped_at": None if g.tripped_at is None else _iso(g.tripped_at),
                 "until": None if g.until is None else _iso(g.until), "reason": g.reason}
                for b in self.books for g in b.guards.state_rows(now)
                if g.guard not in ("daily_open", "loss_streak_count")
            ] + [
                {"book": "*", "guard": g.guard, "tripped_at": None if g.tripped_at is None else _iso(g.tripped_at),
                 "until": None, "reason": g.reason}
                for g in self.system.state_rows()
            ],  # fmt: skip
            "balances": {b.name: self._balances_of(b) for b in self.books},
            "positions": [
                {"book": b.name, "product": p.product_id, "side": "LONG", "base_qty": str(p.base_qty),
                 "avg_entry": None if p.avg_entry is None else str(p.avg_entry),
                 "sl": None if p.sl is None else str(p.sl), "tp": None if p.tp is None else str(p.tp),
                 "opened_at": None if p.opened_at is None else _iso(p.opened_at),
                 "mark": None if p.product_id not in self.marks else str(self.marks[p.product_id]),
                 "bracket_order_id": p.bracket_order_id, "dust_qty": str(p.dust_qty)}
                for b in self.books for p in self.ledger.long_positions(b.name)
            ],  # fmt: skip
            "pending_entries": [
                {"book": b.name, "product": p} for b in self.books for p in self.cfg.market.products
                if b.shadow.has_pending_entry(p)
            ],  # fmt: skip
            "decider": {"backend": self.cfg.decision.backend, "fallback_ratio_1h": self.decider_ratio_1h,
                        "engine": stats},
            "reconcile": None,
            "fees": {"maker_bps": rates.maker_bps, "taker_bps": rates.taker_bps,
                     "source": "assumed" if rates.assumed else "account"},
            "control": None,
            "telemetry": {
                "enabled": self.cfg.telemetry.enabled, "errors_1h": self.telemetry_errors.errors_1h(),
                "events_dropped_total": 0 if self.events is None else self.events.dropped_total,
                "ops_today": None if ops is None else {k: v for k, v in ops.today().items() if k != "state"},
            },
            "disk": None,
            # --- the flat view the heartbeat and the first tests read
            "mode": self.mode,
            "started_at": _iso(self.started_at),
            "uptime_s": round((now - self.started_at).total_seconds(), 1),
            "books": names,
            "model": self.champion.version,
            "label_config_sha256": self.champion.label.sha256,
            "entries_enabled": self.champion.no_entry_reason is None,
            "no_entry_reason": self.champion.no_entry_reason,
            "sentiment_in_state": self.champion.sentiment_available,
            "key": {"can_view": self.perms.can_view, "can_trade": self.perms.can_trade,
                    "can_transfer": self.perms.can_transfer},
            "feed": {
                "last_message_age_s": None if last_msg is None else round((now - last_msg).total_seconds(), 1),
                "damage_intervals": len(self.damage),
            },
            "entries_blocked": blocked,
            "book_stats": self.manager.status(),
            "faults": dict(self.life.faults),
            "failures": list(self.life.failures),
        }  # fmt: skip

    def _balances_of(self, book: Book) -> dict[str, Any]:
        bb = book.account.balances
        currencies = {book.account.quote_currency, *(b for b in book.base_of.values() if bb.total(b) > 0)}
        return {
            "source": bb.source, "snapshot_age_s": bb.snapshot_age_s(), "epoch": bb.epoch, "stale": False,
            "currencies": {
                c: {"total": str(bb.total(c)), "free": str(bb.free(c)), "reserved": str(bb.reserved(c))}
                for c in sorted(currencies)
            },
        }  # fmt: skip

    async def _write_heartbeat(self, phase: str | None = None) -> None:
        now = self.deps.clock()
        reasons = self.unhealthy_reasons(now)
        payload = {
            "v": HEARTBEAT_SCHEMA, "ts": now.timestamp(), "written_at": _iso(now), "ok": not reasons,
            "phase": phase or ("stopping" if self.life.stopping else "running"), "reasons": reasons,
            "status": self.status(),
        }  # fmt: skip
        await asyncio.to_thread(write_json_atomic, Path(self.cfg.paths.data_dir) / HEARTBEAT_FILE, payload)

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                await self._write_heartbeat()
            except Exception as exc:  # disk full: the file ages and the health check says so
                log.error("heartbeat_failed", error=f"{type(exc).__name__}: {exc}")
            await self.deps.sleep(HEARTBEAT_S)

    # ---------------------------------------------------------------------------------------- telemetry

    async def _equity_rows(self) -> list[dict[str, Any]]:
        """One row per book for ``data/equity/DAY.jsonl`` (spec 6.3.3): the values come from the ``VirtualAccount`` and
        the ledger; a book whose equity cannot be marked yet is left out of this minute."""
        now = self.deps.clock()
        day_start = datetime(now.year, now.month, now.day, tzinfo=UTC)
        rows: list[dict[str, Any]] = []
        for b in self.books:
            try:
                equity = b.account.equity(self.marks, b.base_of)
                unrealized = b.account.unrealized_pnl(self.marks)
            except KeyError:
                continue
            realized, fees = await self.ledger.day_totals(b.name, day_start)
            longs = self.ledger.long_positions(b.name)
            quote = b.account.quote_currency
            rows.append({
                "book": b.name, "mode": self.mode, "equity": str(equity),
                "quote_total": str(b.account.balances.total(quote)), "quote_free": str(b.account.balances.free(quote)),
                "exposure_quote": str(sum((p.base_qty * self.marks.get(p.product_id, Decimal(0)) for p in longs),
                                          Decimal(0))),
                "unrealized_quote": str(unrealized), "realized_today_quote": str(realized),
                "fees_today_quote": str(fees), "open_positions": len(longs),
            })  # fmt: skip
        return rows

    def _sample_ops(self) -> None:
        """Feed the ops recorder what only a periodic look can see: counters of the feed and the engines, and the
        guards (a change from "entries allowed" to a reason is a trip, and an event)."""
        ops = self.ops
        if ops is None:
            return
        now = self.deps.clock()
        stream_stats = getattr(self.feed, "stats", None)
        if stream_stats is not None and hasattr(stream_stats, "reconnects"):
            ops.observe_counter("ws_reconnects", int(stream_stats.reconnects))
        engines = [b.engine for b in self.books if b.engine is not None]
        ops.observe_counter("decider_fallbacks", sum(e.stats.fallbacks + e.stats.unavailable for e in engines))
        ops.observe_counter("skipped_late", max((e.stats.skipped_late for e in engines), default=0))
        service_stats = getattr(self.bar_service, "stats", None)
        for product, count in dict(getattr(service_stats, "fetch_failures", {})).items():
            ops.observe_fetch_failures(str(product), int(count))
        task_dead = any(k.startswith("task:") for k in self.life.faults)
        scopes: list[tuple[str, str | None, bool]] = [("*", self.system.blocked_reason(now), task_dead)]
        scopes += [(b.name, b.guards.blocked_reason(now), False) for b in self.books if b.engine is not None]
        # a book without an engine (buy_and_hold) is a benchmark, always fully invested: its guards say nothing
        for scope, reason, bug in scopes:
            tripped, cleared = ops.observe_guard(scope, reason, bug=bug)
            if tripped is not None:
                self.emit("kill_switch_tripped", "warning", book=scope, guard=tripped, reason=reason, origin="auto")
            if cleared is not None:
                self.emit("kill_switch_cleared", "info", book=scope, guard=cleared, origin="auto")

    async def _ops_loop(self) -> None:
        ops = self.ops
        assert ops is not None
        while True:
            self._sample_ops()
            ops.tick()
            await ops.flush()
            await self.deps.sleep(self.cfg.telemetry.ops_interval_s)

    # ---------------------------------------------------------------------------------------- shutdown

    async def _cancel_entries(self) -> None:
        """The entries still waiting for a fill are cancelled (a simulated cancel is confirmed at once)."""
        cancels = [book.shadow.cancel(p) for book in self.books for p in self.cfg.market.products]
        await asyncio.wait_for(asyncio.gather(*cancels), SHUTDOWN_GRACE_S)

    async def _drain(self) -> None:
        """Wait for the orders in flight, all books together, for at most ``SHUTDOWN_GRACE_S`` (spec 4.3 punto 12)."""
        waits = [self.manager.drain(), *(b.watchdog.drain() for b in self.books if b.watchdog is not None)]
        # A simulated exit is parked on a market bar that will not come once the feed is stopped: waiting the whole
        # grace for it would only delay the stop (Docker kills after 10 s). The restart cancels it in the ledger and
        # the horizon / stop watchdog asks again; everything else still gets the full grace.
        parked = [(b.name, p) for b in self.books for p in self.cfg.market.products if b.shadow.has_pending_exit(p)]
        if parked:
            log.warning("exits_parked_at_shutdown", exits=parked)
        try:
            await asyncio.wait_for(asyncio.gather(*waits), 1.0 if parked else SHUTDOWN_GRACE_S)
        except TimeoutError:
            if not parked:
                raise

    async def _flush_bars(self) -> None:
        await self.writer.flush()  # the bars of the last minutes reach the store (SIGTERM, spec 0.5 / P0)

    async def _close_ops(self) -> None:
        """The ops of the day get their last interval as up time and ``running: false`` (a clean stop, not a crash)."""
        if self.ops is not None:
            self._sample_ops()
            self.ops.stop()
            await self.ops.flush()

    async def _stop_learner(self) -> None:
        """A learner child must not outlive the app (it resumes the date it started on its next run)."""
        if self.learner is not None:
            await self.learner.cancel()

    async def _shutdown(self) -> None:
        """Ordered, and every step is attempted whatever happened to the previous one. Brackets are not touched."""
        self.life.request_stop("shutdown")
        self._emit("agent_stopping", exit_code=self.life.exit_code, faults=dict(self.life.faults))
        steps: list[tuple[str, Callable[[], Awaitable[object]]]] = [
            ("stop_learner", self._stop_learner),
            ("cancel_entries", self._cancel_entries),
            ("drain_in_flight", self._drain),
            ("flush_bars", self._flush_bars),
            ("save_kill_switch", self._save_kill_switch),
            ("close_ops", self._close_ops),
            ("heartbeat", partial(self._write_heartbeat, "stopped")),
            ("close_resources", self.stack.aclose),
        ]
        for name, step in steps:
            try:
                await step()
            except Exception as exc:
                log.error("shutdown_step_failed", step=name, error=f"{type(exc).__name__}: {exc}")


def _detach_forwarder(events: EventLog) -> None:
    if LOG_FORWARDER.target is events:
        LOG_FORWARDER.target = None


def _spread_source(feed: MarketFeed, label: LoadedLabelConfig) -> Callable[[str, datetime], float]:
    """The spread a book's venue is given with each bar: the live order book's, else the model's label spread."""

    def spread(product: str, t: datetime) -> float:
        live = feed.spread_bps(product)
        return live if live is not None else label.spread_for(product, t)

    return spread


def _atr_1h(engine: DecisionEngine, product_id: str) -> Decimal | None:
    """ATR(1h) of the product for the watchdog's stop backup; ``None`` while it is not available."""
    indicators = engine.indicators.get(product_id)
    if indicators is None:
        return None
    try:
        value = indicators.snapshot().atr_14_1h
    except ValueError:  # no bar consumed yet
        return None
    return Decimal(repr(value)) if math.isfinite(value) and value > 0 else None


# ---------------------------------------------------------------------------------------------------- CLI


def install_signal_handlers(loop: asyncio.AbstractEventLoop, callback: Callable[[], None]) -> list[int]:
    """SIGINT and SIGTERM (and Ctrl+Break on Windows) call ``callback`` on the loop thread. Windows loops have no
    ``add_signal_handler``: there the C-level handler hops to the loop with ``call_soon_threadsafe``. Returns the
    signals that were installed."""
    installed: list[int] = []
    for sig in (signal.SIGINT, signal.SIGTERM, *([signal.SIGBREAK] if hasattr(signal, "SIGBREAK") else [])):
        try:
            loop.add_signal_handler(sig, callback)
        except (NotImplementedError, RuntimeError, ValueError):
            try:
                signal.signal(sig, lambda *_args: loop.call_soon_threadsafe(callback))
            except (OSError, ValueError):  # not the main thread
                continue
        installed.append(int(sig))
    return installed


async def amain(
    cfg: AppConfig, env: Mapping[str, str], deps: Deps | None = None, config_path: str | None = None
) -> int:
    """Start, run, stop; return the exit code. Nothing but a clean code leaves this function."""
    holder: list[Runtime] = []
    task = asyncio.current_task()

    def on_signal() -> None:
        if holder:
            holder[0].request_stop("signal")
        elif task is not None:
            task.cancel()  # still starting (a backfill can take minutes): abandon it

    install_signal_handlers(asyncio.get_running_loop(), on_signal)
    try:
        runtime = await Runtime.start(cfg, env, deps)
    except asyncio.CancelledError:
        log.info("stopped_while_starting")
        return EXIT_OK
    except StartupError as exc:
        log.error("startup_refused", error=str(exc), exit_code=exc.exit_code)
        return exc.exit_code
    except SpotViolation as exc:
        log.critical("startup_spot_violation", error=str(exc))
        return SPOT_EXIT_CODE
    except DryRunViolation as exc:
        log.critical("startup_dry_run_violation", error=str(exc))
        return EXIT_DRY_VIOLATION
    except Exception:
        log.exception("startup_failed")
        return EXIT_FAILURE
    runtime.config_path = config_path
    holder.append(runtime)
    return await runtime.run()


def resolve_cli_config(path: str | None) -> AppConfig:
    """``--config`` if given; else ``config/config.yml`` if it exists; else the defaults (equal to the example)."""
    if path:
        return load_config_or_exit(path)
    if Path(DEFAULT_CONFIG_PATH).is_file():
        return load_config_or_exit(DEFAULT_CONFIG_PATH)
    return AppConfig()


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Chronos v2 agent (dry run: the real market, simulated orders)")
    ap.add_argument("--config", default=None, help="YAML config (default: config/config.yml if present)")
    args = ap.parse_args(argv)
    cfg = resolve_cli_config(args.config)
    config_path = args.config or (DEFAULT_CONFIG_PATH if Path(DEFAULT_CONFIG_PATH).is_file() else None)
    configure_logging(collect_secrets(cfg, os.environ))
    try:
        return asyncio.run(amain(cfg, os.environ, config_path=config_path))
    except KeyboardInterrupt:
        return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
