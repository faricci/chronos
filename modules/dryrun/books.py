"""Parallel dry-run books and their manager (spec sez. 0.6, 5.1.4, 5.3 punto 8; vincolo C11).

A **book** is one virtual account fed by the same market stream. The manager builds each book from the same parts the
live book uses and fans every published bar out to all of them:

    book                 decisions                                   what is its own
    -------------------  ------------------------------------------  ---------------------------------------------
    ``model`` (champion, the model's backend -> ``DecisionPolicy``   VirtualAccount, BalanceGuard,
    challenger, ...)     -> ``RiskManager`` of THAT model's label    ShadowExecutionGateway, RiskManager,
                                                                     DecisionEngine
    ``random_entry``     ``RandomDecider`` through the SAME policy,  as above; policy, label and questions are
                         RiskManager and sizing as its source book   the source book's
    ``buy_and_hold``     one entry per product, no exit; no          VirtualAccount, BalanceGuard, gateway
                         RiskManager, but the BalanceGuard
    ``twin``             none: it receives the intents of the real   VirtualAccount, BalanceGuard, gateway
                         book (``parity.TwinMirror``)

What is shared: the DRY ledger (one database per mode, a ``book`` column in every table, sez. 0.6), the
``SpotRegistry``, the fee source, the kill switch object (one ``AccountGuards`` per book inside it) and the market.
Nothing else: a book's balances, reservations, positions, orders and fills are its own, and an error in one book is
caught, counted and logged there and never reaches the others (the fan-out catches ``Exception`` per book and per
stage).

Safety, by construction (the manager has no exchange to hold, and no other way to move money):

* the ledger must be the ``dry`` one (``LedgerModeMismatch`` otherwise) and every gateway is a
  ``ShadowExecutionGateway``, which refuses a live ledger itself; no object here can place an order on Coinbase;
* every order of every book, ``buy_and_hold`` included, is authorized by that book's ``BalanceGuard`` on its own
  virtual ``BalanceBook`` (the sizing of ``buy_and_hold`` uses ``max_base_for_quote`` with the guard's own fee rate, so
  the guard never refuses what the book asks);
* a book the kill switch does not know is refused at build time: an unknown book is "breached" for the guard, and every
  BUY of it would fail closed without a word.

Order of work for one batch of bars of a book: first the bars go to the gateway (fills, brackets and exits of the orders
the book already has), then to the engine (indicators always; decisions when the bar is on time). The engine and the
gateway stages fail independently: a failed gateway does not keep the indicators from seeing the bar.
"""

from __future__ import annotations

import asyncio
import random
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final, Literal, Protocol

import structlog

from modules.config import (
    BarsConfig,
    BookConfig,
    DecisionConfig,
    EntryTimeout,
    FallbackFees,
    RiskConfig,
)
from modules.decision.engine import (
    BaselineBackend,
    DecisionEngine,
    DecisionSink,
    ExecutionReport,
    Processed,
    RemoteBackend,
    SentimentSource,
    new_ulid,
)
from modules.decision.policy import PolicyConfig
from modules.decision.protocol import REGIME_LEGEND, DecisionAnswers
from modules.decision.questions import QuestionSet
from modules.dryrun.fill_model import BarFillModel, FillModelParams
from modules.dryrun.restore import BookRestore
from modules.dryrun.shadow_gateway import BarEvent, RestoreReport, ShadowExecutionGateway
from modules.dryrun.virtual_account import VirtualAccount
from modules.execution.balance import Clock, CoverageAlarm
from modules.execution.balance_guard import BalanceGuard, FeeSource, KillSwitchView
from modules.execution.gateway import EntryResult, ExecutionGateway, ExitResult
from modules.execution.orders import ProductMeta
from modules.execution.rounding import ceil_to
from modules.execution.spot import SpotRegistry
from modules.ledger.positions import LedgerModeMismatch, PositionLedger
from modules.market.bars import BarClosed
from modules.risk.costs import FeeRates, coverage_fee_rate, max_base_for_quote
from modules.risk.label_config import LoadedLabelConfig
from modules.risk.risk_manager import EntryGate, MarketQuote, OrderIntent, RiskManager

__all__ = [
    "BookConfigError",
    "BookDeps",
    "BookEngine",
    "BookKillSwitch",
    "BookModel",
    "BookStats",
    "BuyAndHoldDriver",
    "BuyRateTracker",
    "QuoteSource",
    "RandomDecider",
    "ShadowBook",
    "ShadowBookManager",
    "VirtualBookContext",
    "build_books",
    "build_buy_and_hold_book",
    "build_model_book",
    "build_random_book",
    "build_twin_book",
    "to_bar_event",
]

log = structlog.get_logger(__name__)

BookKind = Literal["model", "random_entry", "buy_and_hold", "twin"]
SpreadFor = Callable[[str, datetime], float]
ZERO: Final = Decimal(0)

# buy_and_hold buys with a marketable limit this far above the best ask (the bar model fills at open + half spread +
# slippage, which a limit AT the ask would never reach) and holds with a bracket that cannot be reached.
BUY_AND_HOLD_HEADROOM_BPS: Final = Decimal(100)
UNREACHABLE_TP_MULTIPLE: Final = Decimal(1000)
EDGE_WINDOW: Final = timedelta(hours=24)
EDGE_REFRESH: Final = timedelta(hours=1)


class BookConfigError(Exception):
    """The books cannot be built from what was given (a wiring mistake, never a market event)."""


# ---------------------------------------------------------------------------------------- contracts


class QuoteSource(Protocol):
    """Best bid/ask per product (the ``LocalOrderBook``), ``None`` when empty or stale."""

    def quote(self, product_id: str) -> MarketQuote | None: ...


class BookKillSwitch(EntryGate, KillSwitchView, Protocol):
    """The ``KillSwitch``: ``entry_blocked`` for the RiskManager, ``coverage_breach_active`` for the guard."""


class BookEngine(Protocol):
    """The decision side of a book: ``DecisionEngine``, or a driver that never decides (``buy_and_hold``)."""

    async def on_bars(self, events: Iterable[BarClosed]) -> list[Processed]: ...

    async def drain(self, timeout: float | None = None) -> None: ...


@dataclass(frozen=True)
class BookModel:
    """The artifacts of ONE model version, as the registry resolves them: label, policy, questions and a backend."""

    version: str
    label: LoadedLabelConfig
    policy: PolicyConfig
    questions: QuestionSet
    baseline: BaselineBackend | None
    remote: RemoteBackend | None = None


ModelResolver = Callable[[str], BookModel]  # registry alias ("champion") -> the model it resolves to


@dataclass(frozen=True)
class BookDeps:
    """Everything the books of one dry database share."""

    ledger: PositionLedger
    clock: Clock
    registry: SpotRegistry
    fees: FeeSource
    kill_switch: BookKillSwitch
    quotes: QuoteSource
    sink: DecisionSink
    risk: RiskConfig
    fallback_fees: FallbackFees
    entry_timeout: EntryTimeout
    fill_params: FillModelParams
    decision: DecisionConfig
    bars: BarsConfig
    products: tuple[str, ...]
    sentiment: SentimentSource | None = None
    remote: RemoteBackend | None = None
    cooldown_s: float = 0.0
    btc_product: str = "BTC-EUR"
    new_id: Callable[[datetime], str] = new_ulid
    on_execution: Callable[[str, ExecutionReport], None] | None = None
    on_alarm: Callable[[str, CoverageAlarm], None] | None = None
    # The process that runs the books may need every call of a model book's engine to go through its own wrapper (a
    # fatal violation must stop the process, an exit reason must reach the ledger): ``wrap_gateway(book, gateway)``
    # returns what the engine gets instead of the bare shadow gateway. The wrapper must pass every call through.
    wrap_gateway: Callable[[str, ExecutionGateway], ExecutionGateway] | None = None
    # state read from the dry ledger before the books are built (restart): the account of a book is rebuilt from it
    restore: Mapping[str, BookRestore] = field(default_factory=dict)


@dataclass
class BookStats:
    bars: int = 0
    gateway_errors: int = 0
    engine_errors: int = 0
    consecutive_errors: int = 0
    last_error: str | None = None
    coverage_alarms: int = 0


@dataclass
class ShadowBook:
    name: str
    kind: BookKind
    account: VirtualAccount
    guard: BalanceGuard
    gateway: ShadowExecutionGateway
    spread: SpreadFor
    engine: BookEngine | None = None
    risk: RiskManager | None = None
    model: BookModel | None = None
    tracker: BuyRateTracker | None = None  # set on a book that other books match the BUY frequency of
    stats: BookStats = field(default_factory=BookStats)

    @property
    def model_version(self) -> str | None:
        return None if self.model is None else self.model.version


# -------------------------------------------------------------------------------------------- helpers


def to_bar_event(event: BarClosed, spread_bps: float) -> BarEvent:
    b = event.bar
    return BarEvent(event.product_id, b.ts, b.open, b.high, b.low, b.close, b.volume, spread_bps)


class VirtualBookContext:
    """``BookContext`` of a virtual book: the same questions the live context answers, from the virtual account."""

    def __init__(self, account: VirtualAccount, deps: BookDeps) -> None:
        self._account = account
        self._deps = deps
        self._base_of = {p: deps.registry.meta(p).base_currency for p in deps.products}

    def quote(self, product_id: str) -> MarketQuote | None:
        return self._deps.quotes.quote(product_id)

    def fees(self) -> FeeRates:
        return self._deps.fees.current

    def meta(self, product_id: str) -> ProductMeta:
        return self._deps.registry.meta(product_id)

    def equity(self, marks: Mapping[str, Decimal]) -> Decimal:
        return self._account.equity(marks, self._base_of)

    def free_quote(self) -> Decimal:
        return self._account.balances.free(self._account.quote_currency)


def _check_deps(deps: BookDeps) -> str:
    """Refuse anything that is not the dry database; return the quote currency shared by the products."""
    if deps.ledger.mode != "dry":
        raise LedgerModeMismatch(f"the books run on the dry ledger only, got the {deps.ledger.mode!r} one")
    if not deps.products:
        raise BookConfigError("the books need at least one product")
    quotes = {deps.registry.meta(p).quote_currency for p in deps.products}
    if len(quotes) != 1:
        raise BookConfigError(f"the products must share one quote currency, got {sorted(quotes)}")
    return next(iter(quotes))


def _infrastructure(
    name: str, starting_quote: Decimal, deps: BookDeps
) -> tuple[VirtualAccount, BalanceGuard, ShadowExecutionGateway, BookStats]:
    quote_currency = _check_deps(deps)
    blocked = deps.kill_switch.entry_blocked(name)
    if blocked is not None and blocked.startswith("unknown_book"):
        raise BookConfigError(f"the kill switch does not know the book {name!r}: every BUY of it would be refused")
    stats = BookStats()
    state = deps.restore.get(name)
    account = VirtualAccount(
        name, starting_quote if state is None else state.starting_quote, deps.clock, quote_currency
    )
    if state is not None and not state.is_fresh:  # a restart: the balances are what the booked fills say
        account.restore(
            state.fills, {p: deps.registry.meta(p).base_currency for p in deps.products}, state.reservations
        )  # fmt: skip
    guard = BalanceGuard(
        account.balances, deps.ledger.book_view(name), deps.registry, deps.fees, deps.fallback_fees, deps.kill_switch
    )

    def on_alarm(alarm: CoverageAlarm) -> None:
        stats.coverage_alarms += 1
        log.error("book_coverage_alarm", book=name, kind=alarm.kind, currency=alarm.currency, detail=alarm.detail)
        if deps.on_alarm is not None:
            deps.on_alarm(name, alarm)

    gateway = ShadowExecutionGateway(
        name, account, BarFillModel(deps.fill_params), deps.ledger, deps.clock, guard, deps.registry, deps.fees,
        on_alarm=on_alarm,
    )  # fmt: skip
    return account, guard, gateway, stats


# ------------------------------------------------------------------------------- random entry decider


class BuyRateTracker:
    """BUY frequency of a book over the last 24 h, recomputed once an hour (sez. 5.3 punto 8).

    Every decision of the book counts as a trial; a BUY decision is a success. ``edge`` is the mean ``p_edge`` of the
    BUY decisions of the window: the calibrated probability a matched random entry carries into the RiskManager, so
    that it meets the same EV gate and the same sizing as the book it matches.
    """

    def __init__(self, clock: Clock, window: timedelta = EDGE_WINDOW, refresh: timedelta = EDGE_REFRESH) -> None:
        self._clock = clock
        self._window = window
        self._refresh = refresh
        self._trials: deque[tuple[datetime, bool, float]] = deque()  # (at, bought, p_edge)
        self._refreshed_at: datetime | None = None
        self._rate = 0.0
        self._edge: float | None = None

    def observe(self, processed: Iterable[Processed]) -> None:
        now = self._clock()
        for p in processed:
            if p.decision is None:
                continue  # the product failed before deciding: not a trial
            bought = p.decision.action == "BUY"
            self._trials.append((now, bought, p.decision.p_edge if bought and p.decision.p_edge is not None else 0.0))

    def _refresh_if_due(self) -> None:
        now = self._clock()
        if self._refreshed_at is not None and now < self._refreshed_at + self._refresh:
            return
        while self._trials and self._trials[0][0] <= now - self._window:
            self._trials.popleft()
        buys = [edge for _, bought, edge in self._trials if bought]
        self._rate = len(buys) / len(self._trials) if self._trials else 0.0
        self._edge = sum(buys) / len(buys) if buys else None
        self._refreshed_at = now

    def rate(self) -> float:
        self._refresh_if_due()
        return self._rate

    def edge(self) -> float | None:
        self._refresh_if_due()
        return self._edge


class RandomDecider:
    """Baseline backend of ``random_entry``: BUY with the observed frequency of the source book, else HOLD.

    The answers go through the source book's ``DecisionPolicy`` like any model's: a BUY is one-hot (the thresholds and
    the confidence floor pass for any ``policy.json``), HOLD otherwise; the regime veto still applies because it reads
    the state, not the answer. It never answers SELL: a random entry leaves through its bracket. Deterministic per seed.
    """

    def __init__(
        self, seed: int, tracker: BuyRateTracker, *, forced_rate: float | None = None, forced_edge: float | None = None
    ) -> None:
        self._rng = random.Random(seed)
        self._tracker = tracker
        if (forced_rate is None) != (forced_edge is None):
            raise BookConfigError("forced_buy_rate and forced_edge go together")
        self._forced = None if forced_rate is None or forced_edge is None else (forced_rate, forced_edge)

    async def decide(self, state: str, questions: object) -> DecisionAnswers:
        """With ``forced_rate`` / ``forced_edge`` (a TEST aid, ``BookConfig.forced_buy_rate``) the draw uses them in
        place of the observed frequency of the source book: the book then no longer benchmarks anything."""
        rate, edge = self._forced if self._forced is not None else (self._tracker.rate(), self._tracker.edge())
        draw = self._rng.random()  # one draw per call, whatever the rate: the sequence depends on the seed only
        buy = edge is not None and draw < rate
        probs = {"BUY": 1.0, "SELL": 0.0, "HOLD": 0.0} if buy else {"BUY": 0.0, "SELL": 0.0, "HOLD": 1.0}
        regime = {name: 1.0 if name == "normal" else 0.0 for name in REGIME_LEGEND}
        return DecisionAnswers.model_validate(
            {
                "action": {"choice": "BUY" if buy else "HOLD", "probabilities": probs, "confidence": 0.0},
                "edge": {"noul": edge if buy and edge is not None else 0.0},
                "vol_regime": {
                    "score": 2.0,
                    "legend": {str(k): name for k, name in enumerate(REGIME_LEGEND)},
                    "probabilities": regime,
                    "confidence": 0.0,
                },
            }
        )



# --------------------------------------------------------------------------------- buy and hold driver


class BuyAndHoldDriver:
    """Buys each product once, with an equal share of the starting quote, and never sells (sez. 5.1.4).

    It is the market benchmark, so it skips the RiskManager (no EV gate, no stop-based sizing) but not the coverage
    circuit: the size is ``max_base_for_quote`` of the free quote at the limit, with the guard's own fee rate, and the
    order goes through the book's gateway, hence through its ``BalanceGuard``. The gateway always places a bracket;
    here it is one that cannot be reached (stop at one quote increment, take profit 1000 times the limit).
    An entry that does not fill is retried on the next bar.
    """

    def __init__(self, name: str, account: VirtualAccount, gateway: ShadowExecutionGateway, deps: BookDeps) -> None:
        self._name = name
        self._account = account
        self._gateway = gateway
        self._deps = deps
        self._budget = account.starting_quote / len(deps.products)
        self._bought: set[str] = set()
        self._busy: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self.attempts: dict[str, int] = {}

    @property
    def bought(self) -> frozenset[str]:
        return frozenset(self._bought)

    @property
    def busy(self) -> frozenset[str]:
        """Products with an entry call still running (a replay waits until they are parked on a market event)."""
        return frozenset(self._busy)

    async def on_bars(self, events: Iterable[BarClosed]) -> list[Processed]:
        for event in events:
            p = event.product_id
            if p in self._bought or p in self._busy or p not in self._deps.products:
                continue
            intent = self._intent(p)
            if intent is None:
                continue
            self._busy.add(p)
            task = asyncio.create_task(self._enter(p, intent), name=f"buy-and-hold-{p}")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            await asyncio.sleep(0)  # the gateway reserves the quote before the next product plans its size
        return []

    async def drain(self, timeout: float | None = None) -> None:
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=timeout)

    def _intent(self, product_id: str) -> OrderIntent | None:
        deps = self._deps
        quote = deps.quotes.quote(product_id)
        if quote is None or not quote.valid():
            return None
        if deps.ledger.view(self._name, product_id).is_long:
            self._bought.add(product_id)  # restarted with the position already held: nothing to buy
            return None
        meta = deps.registry.meta(product_id)
        qinc = meta.quote_increment
        limit = ceil_to(quote.best_ask * (1 + BUY_AND_HOLD_HEADROOM_BPS / 10_000), qinc)
        fee_rate = coverage_fee_rate(deps.fees.current, deps.fallback_fees)
        quote_free = self._account.balances.free(self._account.quote_currency)
        size = max_base_for_quote(min(self._budget, quote_free), limit, fee_rate, qinc, meta.base_increment)
        if size < max(meta.base_min_size, ceil_to(meta.quote_min_size / limit, meta.base_increment)):
            log.warning("buy_and_hold_below_min_size", book=self._name, product=product_id, size=str(size))
            return None
        now = deps.clock()
        stop = qinc
        take_profit = ceil_to(limit * UNREACHABLE_TP_MULTIPLE, qinc)
        return OrderIntent.model_validate(
            {
                "intent_id": deps.new_id(now),
                "decision_id": deps.new_id(now),
                "product_id": product_id,
                "book": self._name,
                "entry": {
                    "type": "limit_marketable",
                    "limit_price": limit,
                    "time_in_force_s": int(deps.entry_timeout.taker),
                },
                "base_size": size,
                "bracket": {
                    "take_profit": take_profit, "stop_loss": stop, "k_tp": 0.0, "k_sl": 0.0, "atr_tf": "none",
                    "atr": ZERO,
                },  # fmt: skip
                "risk": {
                    "equity_eur": self._account.starting_quote, "risk_eur": ZERO, "p_edge": 0.0, "scale_p": 0.0,
                    "regime": 0, "m_reg": 0.0, "ev_bps": 0, "cost_tp_bps": 0, "cost_sl_bps": 0,
                    "p_breakeven": None, "p_be_buffer": 0.0, "spread_bps": 0, "min_size_bump": False,
                },  # fmt: skip
                "created_at": now,
            }
        )

    async def _enter(self, product_id: str, intent: OrderIntent) -> None:
        try:
            result: EntryResult = await self._gateway.submit_entry_with_bracket(intent)
            self.attempts[product_id] = self.attempts.get(product_id, 0) + 1
            if result.status == "filled" or result.reason == "position_open":
                self._bought.add(product_id)
            else:
                log.info("buy_and_hold_entry_retry", book=self._name, product=product_id, status=result.status)
        except Exception:
            log.exception("buy_and_hold_entry_failed", book=self._name, product=product_id)
        finally:
            self._busy.discard(product_id)


# ------------------------------------------------------------------------------------------- builders


def _risk_manager(name: str, deps: BookDeps, model: BookModel) -> RiskManager:
    """The book's RiskManager: barriers and costs of the MODEL's label (what its ``p_edge`` was trained for)."""
    return RiskManager(
        name, deps.risk, model.label.cfg, deps.fallback_fees, deps.entry_timeout, deps.kill_switch, model.label.costs
    )


def build_model_book(cfg: BookConfig, deps: BookDeps, model: BookModel) -> ShadowBook:
    """A book that decides with ``model``: its engine, RiskManager (the model's own label) and policy."""
    account, guard, gateway, stats = _infrastructure(cfg.name, cfg.starting_quote, deps)
    risk = _risk_manager(cfg.name, deps, model)
    engine = _engine(cfg.name, deps, model, risk, gateway, account, deps.decision, model.baseline, model.remote)
    return ShadowBook(
        cfg.name, "model", account, guard, gateway, model.label.spread_for, engine, risk, model, stats=stats
    )


def build_random_book(cfg: BookConfig, deps: BookDeps, source: ShadowBook) -> ShadowBook:
    """``random_entry``: the source book's model, policy and RiskManager; entries drawn at its BUY frequency."""
    if source.kind != "model" or source.model is None:
        raise BookConfigError(f"book {cfg.name!r} matches {source.name!r}, which is not a model book")
    if cfg.seed is None:
        raise BookConfigError(f"book {cfg.name!r}: a random book needs a seed (reproducibility)")
    model = source.model
    if source.tracker is None:
        source.tracker = BuyRateTracker(deps.clock)
    account, guard, gateway, stats = _infrastructure(cfg.name, cfg.starting_quote, deps)
    risk = _risk_manager(cfg.name, deps, model)
    decider = RandomDecider(cfg.seed, source.tracker, forced_rate=cfg.forced_buy_rate, forced_edge=cfg.forced_edge)
    if cfg.forced_buy_rate is not None:
        log.warning(
            "random_entry_forced_rate", book=cfg.name, rate=cfg.forced_buy_rate, edge=cfg.forced_edge,
            hint="test setting: this book does not benchmark the source book while it is on",
        )  # fmt: skip
    decision = deps.decision.model_copy(update={"backend": "baseline"})
    engine = _engine(cfg.name, deps, model, risk, gateway, account, decision, decider, None)
    return ShadowBook(
        cfg.name, "random_entry", account, guard, gateway, model.label.spread_for, engine, risk, model, stats=stats
    )


def build_buy_and_hold_book(cfg: BookConfig, deps: BookDeps, spread: SpreadFor) -> ShadowBook:
    account, guard, gateway, stats = _infrastructure(cfg.name, cfg.starting_quote, deps)
    driver = BuyAndHoldDriver(cfg.name, account, gateway, deps)
    return ShadowBook(cfg.name, "buy_and_hold", account, guard, gateway, spread, driver, stats=stats)


def build_twin_book(name: str, starting_quote: Decimal, deps: BookDeps, spread: SpreadFor) -> ShadowBook:
    """The shadow twin of the live book: no engine; ``parity.TwinMirror`` hands it the real intents."""
    account, guard, gateway, stats = _infrastructure(name, starting_quote, deps)
    return ShadowBook(name, "twin", account, guard, gateway, spread, stats=stats)


def _engine(
    name: str,
    deps: BookDeps,
    model: BookModel,
    risk: RiskManager,
    gateway: ShadowExecutionGateway,
    account: VirtualAccount,
    decision: DecisionConfig,
    baseline: BaselineBackend | None,
    remote: RemoteBackend | None,
) -> DecisionEngine:
    on_execution = deps.on_execution
    return DecisionEngine(
        products=deps.products,
        label=model.label,
        policy=model.policy,
        questions=model.questions,
        risk=risk,
        gateway=gateway if deps.wrap_gateway is None else deps.wrap_gateway(name, gateway),
        positions=deps.ledger.book_view(name),
        context=VirtualBookContext(account, deps),
        decision=decision,
        bars=deps.bars,
        sink=deps.sink,
        baseline=baseline,
        remote=remote if remote is not None else deps.remote,
        sentiment=deps.sentiment,
        clock=deps.clock,
        new_id=deps.new_id,
        cooldown_s=deps.cooldown_s,
        btc_product=deps.btc_product,
        on_execution=None if on_execution is None else (lambda report: on_execution(name, report)),
    )


def _registry_alias(decider: str) -> str | None:
    prefix, sep, alias = decider.partition(":")
    return alias if sep and prefix == "registry" and alias else None


def build_books(
    configs: Sequence[BookConfig], deps: BookDeps, resolve_model: ModelResolver, spread: SpreadFor | None = None
) -> list[ShadowBook]:
    """The books of ``dry_run.books``, in config order. Model books first, because the others refer to them.

    ``decider``: ``registry:<alias>`` (the model the alias resolves to), ``random`` (needs ``match_frequency_of`` and a
    ``seed``), ``buy_and_hold``. ``spread`` is the bar spread for books without a model; by default the label spread of
    the first model book.
    """
    names = [c.name for c in configs]
    if len(set(names)) != len(names):
        raise BookConfigError("duplicate book names")
    built: dict[str, ShadowBook] = {}
    for cfg in configs:
        alias = _registry_alias(cfg.decider)
        if alias is not None:
            built[cfg.name] = build_model_book(cfg, deps, resolve_model(alias))
    fallback_spread = spread or next((b.spread for b in built.values()), None)
    for cfg in configs:
        if cfg.name in built:
            continue
        if cfg.decider == "random":
            source = built.get(cfg.match_frequency_of or "")
            if source is None:
                raise BookConfigError(f"book {cfg.name!r}: match_frequency_of must name a model book")
            built[cfg.name] = build_random_book(cfg, deps, source)
        elif cfg.decider == "buy_and_hold":
            if fallback_spread is None:
                raise BookConfigError(f"book {cfg.name!r} needs a spread source: no model book and no `spread`")
            built[cfg.name] = build_buy_and_hold_book(cfg, deps, fallback_spread)
        else:
            raise BookConfigError(f"book {cfg.name!r}: unknown decider {cfg.decider!r}")
    return [built[n] for n in names]


# ---------------------------------------------------------------------------------------------- manager


class ShadowBookManager:
    """Fans every batch of published bars out to all the books; one book's failure is that book's alone."""

    def __init__(
        self,
        books: Sequence[ShadowBook],
        *,
        on_closed: Callable[[str, str, ExitResult], None] | None = None,
        on_error: Callable[[str, str, Exception], None] | None = None,
        restore: Mapping[str, BookRestore] | None = None,
    ) -> None:
        names = [b.name for b in books]
        if len(set(names)) != len(names):
            raise BookConfigError("duplicate book names")
        if not books:
            raise BookConfigError("the manager needs at least one book")
        self._books = {b.name: b for b in books}
        self._on_closed = on_closed
        self._on_error = on_error
        self._restore: Mapping[str, BookRestore] = restore or {}

    @classmethod
    def from_config(
        cls,
        configs: Sequence[BookConfig],
        deps: BookDeps,
        resolve_model: ModelResolver,
        *,
        spread: SpreadFor | None = None,
        extra: Sequence[ShadowBook] = (),
        on_closed: Callable[[str, str, ExitResult], None] | None = None,
        on_error: Callable[[str, str, Exception], None] | None = None,
    ) -> ShadowBookManager:
        books = [*build_books(configs, deps, resolve_model, spread), *extra]
        return cls(books, on_closed=on_closed, on_error=on_error, restore=deps.restore)

    @property
    def books(self) -> Mapping[str, ShadowBook]:
        return self._books

    def __getitem__(self, name: str) -> ShadowBook:
        return self._books[name]

    async def run(self, queue: asyncio.Queue[BarClosed]) -> None:
        """Consume ``queue`` until cancelled. Events already waiting are served together (the BTC context rule of the
        engine: the products of one minute are all in the indicators before any decides)."""
        while True:
            batch = [await queue.get()]
            while True:
                try:
                    batch.append(queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                await self.on_bars(batch)
            except Exception:  # defence in depth: _serve isolates each book itself
                log.exception("book_manager_batch_failed", n=len(batch))

    async def on_bar(self, event: BarClosed) -> dict[str, list[Processed]]:
        return await self.on_bars([event])

    async def on_bars(self, events: Iterable[BarClosed]) -> dict[str, list[Processed]]:
        """Serve the bars to every book at the same time. Returns what each engine processed."""
        batch = sorted(events, key=lambda e: (e.bar.ts, e.product_id))
        if not batch:
            return {}
        served = await asyncio.gather(*(self._serve(book, batch) for book in self._books.values()))
        return dict(zip(self._books, served, strict=True))

    async def restore(self) -> dict[str, RestoreReport]:
        """After a restart: every book continues from the state ``BookDeps.restore`` gave it (orders that cannot be
        resumed are cancelled, brackets simulated again). Run it once, before the first bar. A failure is NOT hidden:
        a book that cannot be restored must not run on a wrong account."""
        return {
            name: await book.gateway.restore(self._restore[name])
            for name, book in self._books.items()
            if name in self._restore and not self._restore[name].is_fresh
        }

    async def resolve_gap(
        self, reports: Mapping[str, RestoreReport], bars: Callable[[str, datetime], Sequence[BarEvent]]
    ) -> dict[str, int]:
        """Feed the market bars of the downtime to the gateways whose brackets were restored (spec 5.3, the gap
        procedure): a take profit or a stop touched while the process was down is resolved at the bar that touched
        it. ``bars(product, since)`` gives the bars from ``since`` on. Returns the positions closed per book."""
        closed_n: dict[str, int] = {}
        for name, report in reports.items():
            book = self._books[name]
            closed_n[name] = 0
            for product, since in sorted(report.catch_up_from.items()):
                for bar in bars(product, since):
                    for result in await book.gateway.on_bar(bar):
                        closed_n[name] += 1
                        self._notify_closed(book, product, result)
        return closed_n

    async def drain(self, timeout: float | None = None) -> None:
        """Wait for the entry/exit calls the engines still run (shutdown). Never raises for one book."""
        for book in self._books.values():
            if book.engine is not None:
                try:
                    await book.engine.drain(timeout)
                except Exception as exc:
                    self._fail(book, "drain", exc)

    def status(self) -> dict[str, dict[str, object]]:
        """Counters per book, for telemetry and tests."""
        return {
            name: {
                "kind": b.kind,
                "model_version": b.model_version,
                "bars": b.stats.bars,
                "gateway_errors": b.stats.gateway_errors,
                "engine_errors": b.stats.engine_errors,
                "consecutive_errors": b.stats.consecutive_errors,
                "last_error": b.stats.last_error,
                "coverage_alarms": b.stats.coverage_alarms,
                "fees_paid": str(b.account.fees_paid),
                "net_realized_pnl": str(b.account.net_realized_pnl),
            }
            for name, b in self._books.items()
        }

    # ------------------------------------------------------------------------------------ internals

    async def _serve(self, book: ShadowBook, batch: list[BarClosed]) -> list[Processed]:
        book.stats.bars += len(batch)
        failed = False
        for event in batch:
            try:
                closed = await book.gateway.on_bar(to_bar_event(event, book.spread(event.product_id, event.t)))
            except Exception as exc:
                self._fail(book, "gateway", exc)
                failed = True
                continue
            for result in closed:
                self._notify_closed(book, event.product_id, result)
        processed: list[Processed] = []
        if book.engine is not None:
            try:
                processed = await book.engine.on_bars(batch)
            except Exception as exc:
                self._fail(book, "engine", exc)
                failed = True
            else:
                if book.tracker is not None:
                    try:
                        book.tracker.observe(processed)
                    except Exception as exc:
                        self._fail(book, "tracker", exc)
        if not failed:
            book.stats.consecutive_errors = 0
        return processed

    def _notify_closed(self, book: ShadowBook, product_id: str, result: ExitResult) -> None:
        if self._on_closed is None:
            return
        try:
            self._on_closed(book.name, product_id, result)
        except Exception as exc:
            self._fail(book, "on_closed", exc)

    def _fail(self, book: ShadowBook, stage: str, exc: Exception) -> None:
        stats = book.stats
        if stage == "gateway":
            stats.gateway_errors += 1
        else:
            stats.engine_errors += 1
        stats.consecutive_errors += 1
        stats.last_error = f"{stage}: {type(exc).__name__}: {exc}"
        log.error("book_failed", book=book.name, stage=stage, error=stats.last_error, exc_info=exc)
        if self._on_error is not None:
            try:
                self._on_error(book.name, stage, exc)
            except Exception:
                log.exception("book_on_error_failed", book=book.name)
