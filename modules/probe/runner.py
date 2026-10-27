"""The runner of the P4-pre probe (plan sez. 3.2): the ONE module, besides ``execution/gateway.py``, that builds the
live gateway and a writable exchange. ``tests/test_probe_wiring.py`` scans the source tree and fails if any other
module names ``LiveExecutionGateway`` or calls ``build_exchange(..., read_only=False)``.

Order of the wiring (plan sez. 3.2), after the enabling gate (``gate.py``, checked before anything is built):

    read-only exchange (summary for the typed confirmation) -> writable exchange -> key permissions
    -> ``SpotRegistry`` -> ``FeeProvider`` -> ledger (``state/chronos_probe.db``, mode ``live``) -> ``BalanceBook``
    -> ``SystemGuards`` + ``AccountGuards`` -> ``KillSwitch.load`` -> ``BalanceGuard`` -> live gateway
    -> ``restore_entry_contexts`` -> ``Reconciler.on_startup`` -> balance feed -> ``user`` stream (raw record) -> market
    stream (+ recorder) -> ``Watchdog`` -> protections -> scheduler -> ``STOP`` / ``FLATTEN`` watcher.

Fatal: ``SpotViolation`` (exit 4), ``DryRunViolation`` (exit 3), ``ExchangeAuthError``. On a stop the pending entry is
cancelled WITH confirmation; a bracket is never cancelled here.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import IO, Any, Final, TypeVar

import structlog

from modules.app import configure_logging, install_signal_handlers
from modules.config import (
    AppConfig,
    CoinbaseConfig,
    ExecProbeConfig,
    ExecutionConfig,
    FallbackFees,
    KillSwitchConfig,
    RiskConfig,
    WatchdogConfig,
)
from modules.dryrun.recorder import WsRecorder
from modules.execution.balance import BalanceBook, Clock, utc_now
from modules.execution.balance_feed import LiveBalanceFeed
from modules.execution.balance_guard import BalanceGuard
from modules.execution.exchange import (
    CoinbaseExchange,
    DryRunViolation,
    ExchangeAuthError,
    build_exchange,
)
from modules.execution.gateway import EntryResult, LiveExecutionGateway
from modules.execution.raw_log import RawLog, secrets_of
from modules.execution.reconciler import CoverageMonitor, Reconciler
from modules.execution.spot import (
    SPOT_EXIT_CODE,
    SpotRegistry,
    SpotViolation,
    assert_live_key_permissions,
)
from modules.execution.user_stream import CoinbaseUserStream, OrderEventSink, TransportFactory, user_transport_factory
from modules.execution.watchdog import Watchdog
from modules.ledger.positions import ClosedTrade, PositionLedger
from modules.market.ws_feed import CoinbaseMarketStream
from modules.probe import limits
from modules.probe.budget import ProbeBudget, ProbeLimiter, ProbeStreaks
from modules.probe.control import EventFile, FinishMarker, Switches, read_status, write_status
from modules.probe.exits import ProbeExits
from modules.probe.facts import AtrCache, BidSource, FactsGatherer
from modules.probe.gate import (
    EXIT_GATE,
    GateRefused,
    ask_confirmation,
    check_gate,
    key_fingerprint,
    read_credentials,
    require_terminal,
)
from modules.probe.planner import ProbePlanner
from modules.probe.protections import ProtectionMonitor
from modules.probe.report import build_report, render_markdown, write_report
from modules.probe.scheduler import ProbeScheduler
from modules.probe.stage_actions import (
    OrderStageHost,
    preflight,
    preview,
    run_order_stage,
)
from modules.probe.stages import (
    StageBlocked,
    StageRecord,
    check_chain,
    config_hash,
    read_stage,
    skipped_record,
    write_stage,
)
from modules.probe.userlog import TimedFills, tee_factory
from modules.risk.costs import FeeProvider
from modules.risk.kill_switch import AccountGuards, ExitReason, KillSwitch, SystemGuards
from modules.risk.risk_manager import OrderIntent

log = structlog.get_logger(__name__)

T = TypeVar("T")
Sleep = Callable[[float], Awaitable[None]]
REPO_ROOT: Final = Path(__file__).resolve().parent.parent.parent
EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_DRY_VIOLATION: Final = 3
STATUS_EVERY_S: Final = 5.0
EQUITY_EVERY_S: Final = 10.0
SWITCH_EVERY_S: Final = 1.0
TICKER_EVERY_S: Final = 1.0
KILL_SAVE_EVERY_S: Final = 60.0
SHUTDOWN_CANCEL_WAIT_S: Final = 90.0
IDLE_GRACE_S: Final = 5.0
STAGE_DONE_WAIT_S: Final = 120.0
MARKET_STALE_S: Final = 10.0


# ------------------------------------------------------------------------------------------- defaults


def _build_read_exchange(creds: CoinbaseConfig) -> CoinbaseExchange:
    return build_exchange(creds, read_only=True)


def _build_write_exchange(creds: CoinbaseConfig) -> CoinbaseExchange:
    return build_exchange(creds, read_only=False)


def _build_market_stream(products: Sequence[str], raw_tap: Callable[[float, str], None]) -> CoinbaseMarketStream:
    return CoinbaseMarketStream(products, raw_tap=raw_tap)


@dataclass
class Deps:
    """Everything the outside world provides. The tests replace the exchanges, the streams and the terminal."""

    read_exchange: Callable[[CoinbaseConfig], CoinbaseExchange] = _build_read_exchange
    write_exchange: Callable[[CoinbaseConfig], CoinbaseExchange] = _build_write_exchange
    market_stream: Callable[[Sequence[str], Callable[[float, str], None]], CoinbaseMarketStream] = _build_market_stream
    user_transport: Callable[[CoinbaseConfig], TransportFactory] = user_transport_factory
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    stdin: IO[str] = field(default_factory=lambda: sys.stdin)
    stdout: IO[str] = field(default_factory=lambda: sys.stdout)
    stderr: IO[str] = field(default_factory=lambda: sys.stderr)
    clock: Clock = utc_now
    sleep: Sleep = asyncio.sleep
    repo_root: Path = REPO_ROOT
    configure_logs: bool = True
    install_signals: bool = True
    streams: bool = True  # False: no WebSocket at all (the tests drive the gateway directly)
    on_runtime: Callable[[ProbeRuntime], None] | None = None  # the tests take hold of the runtime here


def say(deps: Deps, text: str) -> None:
    deps.stdout.write(text + "\n")
    deps.stdout.flush()


def complain(deps: Deps, text: str) -> None:
    deps.stderr.write(text + "\n")
    deps.stderr.flush()


# ------------------------------------------------------------------------------------------- runtime


def _exit_reason(reason: str) -> ExitReason:
    """The kill switch knows five reasons; ``MANUAL`` counts as a neutral ``SELL`` there."""
    if reason in ("TP", "SL", "HORIZON", "WATCHDOG"):
        return reason  # type: ignore[return-value]
    return "SELL"


@dataclass(kw_only=True)
class ProbeRuntime:
    cfg: AppConfig
    pcfg: ExecProbeConfig
    deps: Deps
    exchange: CoinbaseExchange
    registry: SpotRegistry
    ledger: PositionLedger
    balances: BalanceBook
    kill: KillSwitch
    account: AccountGuards
    guard: BalanceGuard
    gateway: LiveExecutionGateway
    exits: ProbeExits
    sink: OrderEventSink
    reconciler: Reconciler
    feed: LiveBalanceFeed
    fees: FeeProvider
    watchdog: Watchdog
    protections: ProtectionMonitor
    scheduler: ProbeScheduler
    budget: ProbeBudget
    limiter: ProbeLimiter
    streaks: ProbeStreaks
    atr: AtrCache
    bid_source: BidSource
    switches: Switches
    marker: FinishMarker
    events: EventFile
    timed_fills: TimedFills
    raw_log: RawLog
    out_dir: Path
    user_stream: CoinbaseUserStream | None = None
    market_stream: CoinbaseMarketStream | None = None
    recorder: WsRecorder | None = None
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    finished_reason: str | None = None
    last_skip: dict[str, Any] | None = None
    _tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    _cancelling: bool = False
    _stop_announced: bool = False

    # ---------------------------------------------------------------------------------- the guards

    @property
    def product(self) -> str:
        return self.pcfg.product

    @property
    def clock(self) -> Clock:
        return self.deps.clock

    def manual_stop(self) -> bool:
        return self.switches.stop_active()

    def entries_blocked(self, now: datetime) -> str | None:
        """Why a NEW entry is not allowed now (the hand switches are asked separately by the planner)."""
        finished = self.marker.read()
        if finished is not None:
            return f"finished:{finished.reason}"
        reason = self.streaks.blocked_reason(now)
        if reason is not None:
            return reason
        return self.kill.entry_blocked(limits.BOOK)

    def emit(self, kind: str, data: dict[str, Any]) -> None:
        self.events.emit(kind, data)
        log.info("probe_event", kind=kind, **{k: v for k, v in data.items() if k not in ("kind", "event")})

    def finish(self, reason: str, detail: str) -> None:
        """The probe is over (plan sez. 1.3, 1.4, 5): entries stop for good and the operator is told to analyse."""
        if self.marker.write(reason, detail, self.clock()):
            self.finished_reason = reason
            log.error("probe_finished_conflitto", reason=reason, detail=detail)
            self.emit("probe_finished", {"reason": reason, "detail": detail})

    def _spawn(self, coro: Coroutine[Any, Any, Any], name: str | None = None) -> asyncio.Task[Any]:
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self._tasks.add(task)

        def done(t: asyncio.Task[Any]) -> None:
            self._tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                exc = t.exception()
                log.error("probe_background_task_failed", error=f"{type(exc).__name__}: {exc}")

        task.add_done_callback(done)
        return task

    # ------------------------------------------------------------------------------------ closes

    def on_trade_closed(self, trade: ClosedTrade) -> None:
        """The ledger's close listener: series, guards, slippage, the kill switch's memory."""
        if trade.book != limits.BOOK:
            return
        self.streaks.observe_exit(trade.exit_reason, trade.exit_at)
        self.account.observe_trade_closed(trade.exit_at, trade.net_quote, _exit_reason(trade.exit_reason))
        if self.streaks.finished:
            self.finish("watchdog_exit", f"a position was closed by {trade.exit_reason}")
        self._spawn(self.protections.on_closed(trade))
        self._spawn(self.save_guards())

    async def save_guards(self) -> None:
        # the kill switch writes on the ledger's connection: never inside one of the ledger's own transactions
        try:
            async with self.ledger.exclusive():
                await self.kill.save(self.ledger.connection)
        except Exception as exc:  # the memory of the guards is not worth stopping for; the next save retries
            log.error("probe_kill_switch_save_failed", error=f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------------------------ entries

    def start_entry(self, intent: OrderIntent) -> None:
        self._spawn(self._enter(intent))

    async def _enter(self, intent: OrderIntent) -> EntryResult | None:
        if self.switches.stop_active():  # STOP pressed between the decision and the send
            self.emit("probe_entry_dropped", {"decision_id": intent.decision_id, "why": "manual_stop"})
            return None
        result = await self.gateway.submit_entry_with_bracket(intent)
        self.emit(
            "probe_entry_result",
            {
                "decision_id": intent.decision_id, "status": result.status, "reason": result.reason,
                "filled_size": str(result.filled_size), "avg_price": str(result.avg_price),
                "fees": str(result.fees), "bracket": result.bracket, "dust_merged": result.dust_merged,
            },
        )  # fmt: skip
        if result.status == "filled" and result.bracket in ("refused", "rejected"):
            self.finish("watchdog_exit", f"the bracket was not placed ({result.bracket}: {result.reason})")
            await self.exits.emergency_exit(self.product)
        elif result.status in ("unknown", "unconfirmed"):
            self.emit("probe_entry_needs_reconciliation", {"decision_id": intent.decision_id, "status": result.status})
        return result

    # --------------------------------------------------------------------------------------- loops

    async def _ticker_loop(self) -> None:
        """Feed the watchdog: the WebSocket ticker while it is fresh, the REST bid otherwise."""
        while True:
            try:
                price = None
                if self.market_stream is not None:
                    q = self.market_stream.quotes.get(self.product)
                    if q is not None and 0 <= (self.clock() - q.ts).total_seconds() <= MARKET_STALE_S:
                        price = Decimal(str(q.price))
                if price is None:
                    price = await self.bid_source.bid()
                if price is not None:
                    await self.watchdog.on_ticker(self.product, price)
            except ExchangeAuthError:
                raise
            except Exception as exc:
                log.warning("probe_ticker_loop_failed", error=f"{type(exc).__name__}: {exc}")
            await self.deps.sleep(TICKER_EVERY_S)

    async def switches_once(self) -> None:
        """``STOP``: cancel the pending entry (confirmed). ``FLATTEN``: and sell the position."""
        stop = self.switches.stop_active()
        if stop and not self._stop_announced:
            self._stop_announced = True
            self.emit("probe_stop_switch", {"flatten": self.switches.flatten_active()})
        if not stop:
            self._stop_announced = False
        if stop and self.gateway.has_pending_entry(self.product) and not self._cancelling:
            self._cancelling = True
            self._spawn(self._cancel_pending(), name="cancel_pending")
        if self.switches.flatten_active() and self.ledger.view(limits.BOOK, self.product).is_long:
            if not self._tasks_named("flatten"):
                self._spawn(self._flatten(), name="flatten")

    async def _switch_loop(self) -> None:
        while True:
            try:
                await self.switches_once()
            except Exception as exc:
                log.error("probe_switch_loop_failed", error=f"{type(exc).__name__}: {exc}")
            await self.deps.sleep(SWITCH_EVERY_S)

    def _tasks_named(self, name: str) -> bool:
        return any(not t.done() and t.get_name() == name for t in self._tasks)

    async def _cancel_pending(self) -> None:
        try:
            outcome = await self.gateway.cancel(self.product)
            self.emit("probe_pending_entry_cancelled", {"outcome": outcome.status})
        finally:
            self._cancelling = False

    async def _flatten(self) -> None:
        result = await self.exits.flatten()
        self.emit("probe_flatten", {"status": None if result is None else result.status})
        await self.deps.sleep(5.0)  # the switch watcher asks again if the position is still there

    async def equity_once(self) -> None:
        """One look at the notional equity: the daily-loss guard, the anomaly loss and the total loss."""
        bid = await self.bid_source.bid()
        if bid is None:
            return
        state = await self.budget.state(self.product, bid)
        self.account.observe_equity(self.clock(), state.equity, state.exposure)
        if state.cumulative_loss > self.pcfg.anomaly_loss_quote:
            self.finish("anomaly_loss", f"cumulative loss {state.cumulative_loss} EUR")
        if state.cumulative_loss >= self.pcfg.max_total_loss_quote:
            self.finish("total_loss", f"cumulative loss {state.cumulative_loss} EUR")

    async def _equity_loop(self) -> None:
        """Every few seconds; the guards are saved once a minute."""
        last_save = self.clock()
        while True:
            try:
                await self.equity_once()
                if (self.clock() - last_save).total_seconds() >= KILL_SAVE_EVERY_S:
                    last_save = self.clock()
                    await self.save_guards()
            except ExchangeAuthError:
                raise
            except Exception as exc:
                log.warning("probe_equity_loop_failed", error=f"{type(exc).__name__}: {exc}")
            await self.deps.sleep(EQUITY_EVERY_S)

    async def _fee_loop(self) -> None:
        while True:
            await self.fees.refresh()
            await self.deps.sleep(60.0)

    async def status(self) -> dict[str, Any]:
        now = self.clock()
        position = self.ledger.view(limits.BOOK, self.product)
        blocked = "manual_stop" if self.switches.stop_active() else self.entries_blocked(now)
        counts = await self.limiter.counts(now)
        bid = await self.bid_source.bid()
        budget = None if bid is None else await self.budget.state(self.product, bid)
        return {
            "updated_at": now.astimezone(UTC).isoformat(),
            "entries_blocked": blocked,
            "manual_stop": self.switches.stop_active(),
            "flatten": self.switches.flatten_active(),
            "finished": self.marker.read() is not None,
            "position": {
                "side": position.side,
                "base_qty": str(position.base_qty),
                "sl": None if position.sl is None else str(position.sl),
                "tp": None if position.tp is None else str(position.tp),
                "bracket": position.bracket_order_id is not None,
                "dust_qty": str(position.dust_qty),
            },  # fmt: skip
            "pending_entry": self.gateway.has_pending_entry(self.product),
            "counts": {
                "entries_today": counts.entries_today,
                "entries_total": counts.entries_total,
                "orders_today": counts.orders_today,
            },  # fmt: skip
            "budget": None
            if budget is None
            else {
                "equity": str(budget.equity),
                "cumulative_loss": str(budget.cumulative_loss),
                "l_rem": str(budget.l_rem),
                "entry_cap": str(budget.entry_cap),
            },  # fmt: skip
            "streaks": {
                "sl_streak": self.streaks.sl_streak,
                "watchdog_count": self.streaks.watchdog_count,
                "pause_until": None if self.streaks.pause_until is None else self.streaks.pause_until.isoformat(),
            },  # fmt: skip
            "balance_age_s": self.balances.snapshot_age_s(),
            "last_skip": self.last_skip,
            "user_stream": None
            if self.user_stream is None
            else {
                "messages": self.user_stream.stats.messages,
                "reconnects": self.user_stream.stats.reconnects,
                "gaps": self.user_stream.stats.gaps,
            },  # fmt: skip
        }

    async def _status_loop(self, every_s: float) -> None:
        while True:
            try:
                write_status(self.out_dir, await self.status())
            except Exception as exc:
                log.warning("probe_status_failed", error=f"{type(exc).__name__}: {exc}")
            await self.deps.sleep(every_s)

    async def _idle_loop(self) -> None:
        """When the probe is over (or all the entries are used) and nothing is open any more: stop the process."""
        quiet_since: datetime | None = None
        while True:
            try:
                now = self.clock()
                over = self.marker.read() is not None or await self.limiter.blocked(now) == "max_entries_total"
                position = self.ledger.view(limits.BOOK, self.product)
                busy = (
                    position.is_long
                    or self.gateway.has_pending_entry(self.product)
                    or bool(await self.ledger.open_orders(limits.BOOK))
                    or bool(self._tasks_active())
                )
                if over and not busy:
                    quiet_since = quiet_since or now
                    if (now - quiet_since).total_seconds() >= IDLE_GRACE_S:
                        self.emit("probe_idle_stop", {"finished": self.marker.read() is not None})
                        self.stop_event.set()
                        return
                else:
                    quiet_since = None
            except Exception as exc:
                log.warning("probe_idle_loop_failed", error=f"{type(exc).__name__}: {exc}")
            await self.deps.sleep(2.0)

    def _tasks_active(self) -> list[asyncio.Task[Any]]:
        return [t for t in self._tasks if not t.done()]

    # ------------------------------------------------------------------------------------ running

    def _components(self, *, scheduler: bool, status_every_s: float) -> dict[str, Coroutine[Any, Any, Any]]:
        parts: dict[str, Coroutine[Any, Any, Any]] = {
            "balance_feed": self.feed.run(),
            "reconciler": self.reconciler.run(),
            "watchdog": self.watchdog.run(),
            "protections": self.protections.run(sleep=self.deps.sleep),
            "atr": self.atr.run(),
            "fees": self._fee_loop(),
            "switches": self._switch_loop(),
            "ticker": self._ticker_loop(),
            "equity": self._equity_loop(),
            "status": self._status_loop(status_every_s),
        }
        if self.user_stream is not None:
            parts["user_stream"] = self.user_stream.run()
        if self.market_stream is not None:
            parts["market_stream"] = self.market_stream.run()
        if scheduler:
            parts["scheduler"] = self.scheduler.run()
            parts["idle"] = self._idle_loop()
        return parts

    async def run(
        self,
        job: Callable[[], Awaitable[T]] | None = None,
        *,
        scheduler: bool = False,
        status_every_s: float = STATUS_EVERY_S,
    ) -> T | None:
        """Run the components until the job ends (a stage), a stop is asked (Ctrl+C, the idle stop) or one of them dies.
        A component that dies is a fault: its exception is raised after the orderly shutdown."""
        if self.recorder is not None:
            self.recorder.start()
        if self.deps.install_signals:
            install_signal_handlers(asyncio.get_running_loop(), self.stop_event.set)
        components = {
            name: asyncio.create_task(coro, name=name)
            for name, coro in self._components(scheduler=scheduler, status_every_s=status_every_s).items()
        }
        job_task = asyncio.ensure_future(job()) if job is not None else None
        stop_task = asyncio.create_task(self.stop_event.wait(), name="stop")
        watched: set[asyncio.Task[Any]] = {stop_task, *components.values(), *([job_task] if job_task else [])}
        failure: BaseException | None = None
        result: T | None = None
        try:
            done, _ = await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task is stop_task or task is job_task:
                    continue
                failure = task.exception() if not task.cancelled() else RuntimeError(f"{task.get_name()} was cancelled")
                if failure is None:
                    failure = RuntimeError(f"component {task.get_name()} ended")
            if job_task is not None and job_task in done and not job_task.cancelled():
                exc = job_task.exception()
                if exc is not None:
                    failure = failure or exc
                else:
                    result = job_task.result()
                    await self._wait_until_quiet()
        finally:
            await self.shutdown([stop_task, *([job_task] if job_task else []), *components.values()])
        if failure is not None:
            raise failure
        return result

    async def _wait_until_quiet(self) -> None:
        """A stage has finished its job: wait for a fill it may have produced to be protected or closed."""
        waited = 0.0
        while waited < STAGE_DONE_WAIT_S:
            busy = self.ledger.view(limits.BOOK, self.product).is_long or self.gateway.has_pending_entry(self.product)
            if not busy and not self._tasks_active():
                return
            await self.deps.sleep(1.0)
            waited += 1.0
        if self.ledger.view(limits.BOOK, self.product).is_long:
            say(
                self.deps,
                "ATTENZIONE: c'e' una posizione aperta. Il suo stop e' sull'exchange; rilancia la prova (modo live) "
                "per farla gestire, oppure vendila a mano.",
            )

    async def shutdown(self, tasks: Sequence[asyncio.Task[Any]]) -> None:
        """Orderly end. The scheduler goes first (no new entry), then the pending entry is cancelled WITH
        confirmation. A bracket is never cancelled here: it stays on the exchange and protects the position while the
        process is down."""
        by_name = {t.get_name(): t for t in tasks}
        for name in ("scheduler", "idle"):
            if name in by_name:
                by_name[name].cancel()
        try:
            if self.gateway.has_pending_entry(self.product):
                await asyncio.wait_for(self.gateway.cancel(self.product), timeout=SHUTDOWN_CANCEL_WAIT_S)
        except Exception as exc:
            log.error("probe_shutdown_cancel_failed", error=f"{type(exc).__name__}: {exc}")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)
        await self.save_guards()
        try:
            write_status(self.out_dir, await self.status())
        except Exception as exc:
            log.warning("probe_final_status_failed", error=f"{type(exc).__name__}: {exc}")
        if self.recorder is not None:
            self.recorder.stop()
        self.raw_log.close()
        await self.ledger.close()


# ------------------------------------------------------------------------------------------- building


def guard_config(pcfg: ExecProbeConfig) -> KillSwitchConfig:
    """The probe's use of the kill switch: the daily loss as a fraction of ``C``, and the single SL+WATCHDOG series of
    ``AccountGuards`` neutralised (the probe counts its series itself, ``ProbeStreaks``)."""
    return KillSwitchConfig(
        max_daily_loss_frac=limits.max_daily_loss_frac(pcfg.max_daily_loss_quote),
        max_consecutive_losses=pcfg.max_entries_total + 1,
    )


async def build_runtime(
    cfg: AppConfig, deps: Deps, creds: CoinbaseConfig, secrets: Sequence[str], entry_mode: str | None = "gtd"
) -> ProbeRuntime:
    pcfg, clock = cfg.exec_probe, deps.clock
    out_dir = Path(pcfg.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    exchange = deps.write_exchange(creds)
    ledger: PositionLedger | None = None
    raw_log: RawLog | None = None
    try:
        assert_live_key_permissions(await exchange.get_api_key_permissions())
        registry = await SpotRegistry.load(exchange, [pcfg.product])
        meta = registry.meta(pcfg.product)
        fallback = FallbackFees()

        async def fetch_fees() -> tuple[float, float]:
            rates = await exchange.get_fee_rates()
            return rates.maker_bps, rates.taker_bps

        fees = FeeProvider(fallback, fetch_fees, refresh_s=cfg.execution.fee_refresh_s, clock=clock)
        await fees.refresh()
        ledger = await PositionLedger.open(Path(pcfg.db_path), "live")
        balances = BalanceBook(limits.BOOK, "exchange", clock)
        ks_cfg = guard_config(pcfg)
        system = SystemGuards(ks_cfg, clock)
        account = AccountGuards(
            limits.BOOK, ks_cfg, RiskConfig(max_total_exposure_frac=limits.MAX_TOTAL_EXPOSURE_FRAC), clock
        )
        kill = KillSwitch(system, {limits.BOOK: account}, clock)
        await kill.load(ledger.connection)
        guard = BalanceGuard(balances, ledger.book_view(limits.BOOK), registry, fees, fallback, kill)
        monitor = CoverageMonitor(account.trip_coverage_breach, clock)
        execution: ExecutionConfig = cfg.execution.model_copy(update={"mode": "live"})
        gateway = LiveExecutionGateway(
            guard, exchange, ledger, registry, clock, book=limits.BOOK, balances=balances, execution=execution,
            env=deps.env, sleep=deps.sleep, on_alarm=monitor.on_alarm, gtd_grace_s=pcfg.gtd_grace_s,
        )  # fmt: skip
        await gateway.restore_entry_contexts()
        events = EventFile(out_dir, clock)
        timed_fills = TimedFills(
            exchange,
            on_read=lambda t, oid, total, n: events.emit("fills_read", {"order_id": oid, "total": str(total), "n": n}),
        )
        sink = OrderEventSink(
            book=limits.BOOK, ledger=ledger, balances=balances, registry=registry, fills=timed_fills,
            contexts=gateway, on_alarm=monitor.on_alarm, clock=clock,
        )  # fmt: skip
        reconciler = Reconciler(
            book=limits.BOOK, exchange=exchange, ledger=ledger, balances=balances, registry=registry, sink=sink,
            fees=fees, fallback=fallback, interval_s=cfg.execution.reconcile_interval_s, on_alarm=monitor.on_alarm,
            placer=gateway, clock=clock,
        )  # fmt: skip
        await reconciler.on_startup()
        feed = LiveBalanceFeed(
            exchange, balances, registry, refresh_s=cfg.execution.balance_refresh_s, on_alarm=monitor.on_alarm,
            clock=clock,
        )  # fmt: skip
        raw_log = RawLog(out_dir)
        switches = Switches(Path(cfg.paths.state_dir) / "exec_probe")
        marker = FinishMarker(out_dir)
        streaks = ProbeStreaks(pcfg)
        streaks.replay(await ledger.closed_trades(limits.BOOK))
        budget = ProbeBudget(pcfg, ledger)
        limiter = ProbeLimiter(pcfg, ledger)
        atr = AtrCache(exchange, pcfg.product, clock)
        market_stream: CoinbaseMarketStream | None = None
        recorder: WsRecorder | None = None
        user_stream: CoinbaseUserStream | None = None
        if deps.streams:
            recorder = WsRecorder(out_dir / "ws", retention_days=cfg.dry_run.recording.retention_days)
            market_stream = deps.market_stream([pcfg.product], recorder.record)
            user_stream = CoinbaseUserStream(
                [pcfg.product], sink,
                transport_factory=tee_factory(deps.user_transport(creds), raw_log, secrets),
                request_reconcile=reconciler.request, clock=clock,
            )  # fmt: skip
        bid_source = BidSource(
            exchange,
            pcfg.product,
            ws_quote=None if market_stream is None else (lambda: market_stream.quotes.get(pcfg.product)),
            clock=clock,
        )  # fmt: skip
        exits = ProbeExits(
            gateway, ledger, book=limits.BOOK, product_id=pcfg.product, on_event=events.emit, clock=clock
        )
        watchdog = Watchdog(
            book=limits.BOOK, ledger=ledger, gateway=exits, cfg=WatchdogConfig(), horizon_min=pcfg.horizon_min,
            atr_1h=atr.atr_1h, clock=clock,
        )  # fmt: skip
        rt_ref: list[ProbeRuntime] = []

        def finish(reason: str, detail: str) -> None:
            rt_ref[0].finish(reason, detail)

        protections = ProtectionMonitor(
            cfg=pcfg, book=limits.BOOK, product_id=pcfg.product, ledger=ledger, gateway=exits, bid=bid_source.bid,
            on_event=events.emit, finish=finish, request_stop=switches.request_stop, clock=clock,
        )  # fmt: skip

        def start_entry(intent: OrderIntent) -> None:
            rt_ref[0].start_entry(intent)

        def on_event(kind: str, data: dict[str, Any]) -> None:
            if kind == "probe_skip":
                rt_ref[0].last_skip = dict(data)
            rt_ref[0].emit(kind, data)

        class _Guards:
            def manual_stop(self) -> bool:
                return rt_ref[0].manual_stop()

            def entries_blocked(self, now: datetime) -> str | None:
                return rt_ref[0].entries_blocked(now)

        facts = FactsGatherer(
            reader=exchange, product_id=pcfg.product, meta=meta, ledger=ledger, balances=balances, book=limits.BOOK,
            fees=lambda: fees.current, fallback=fallback, budget=budget, limiter=limiter, atr=atr, guards=_Guards(),
            clock=clock,
        )  # fmt: skip
        scheduler = ProbeScheduler(
            cfg=pcfg, product_id=pcfg.product, ledger=ledger, gather=facts.gather,
            planner=ProbePlanner(pcfg, expires_on_exchange=entry_mode != "gtc_fallback"),
            start_entry=start_entry, on_event=on_event, finish=finish, clock=clock, sleep=deps.sleep,
        )  # fmt: skip
        rt = ProbeRuntime(
            cfg=cfg, pcfg=pcfg, deps=deps, exchange=exchange, registry=registry, ledger=ledger, balances=balances,
            kill=kill, account=account, guard=guard, gateway=gateway, exits=exits, sink=sink, reconciler=reconciler,
            feed=feed, fees=fees, watchdog=watchdog, protections=protections, scheduler=scheduler, budget=budget,
            limiter=limiter, streaks=streaks, atr=atr, bid_source=bid_source, switches=switches, marker=marker,
            events=events, timed_fills=timed_fills, raw_log=raw_log, out_dir=out_dir, user_stream=user_stream,
            market_stream=market_stream, recorder=recorder,
        )  # fmt: skip
        rt_ref.append(rt)
        ledger.add_close_listener(rt.on_trade_closed)
        if streaks.finished:
            rt.finish("watchdog_exit", "a position was closed by WATCHDOG in an earlier run")
        await atr.refresh()
        return rt
    except BaseException:
        if raw_log is not None:
            raw_log.close()
        if ledger is not None:
            await ledger.close()
        raise


# ----------------------------------------------------------------------------------------- the stages


def _entry_mode_text(entry_mode: str | None) -> str:
    if entry_mode == "gtd":
        return "scade lato Coinbase (GTD, 30 minuti)"
    return entry_mode or "da decidere (stadio preview)"


def _summary(pcfg: ExecProbeConfig, fingerprint: str, perms: Any, available: str, entry_mode: str | None) -> list[str]:
    return [
        "",
        "=== Prova di esecuzione a taglia minima (P4-pre) ===",
        f"Prodotto: {pcfg.product}   Perdita massima in tutto (L_tot): {pcfg.max_total_loss_quote} EUR",
        f"Ordine massimo (Q_max): {pcfg.max_entry_quote} EUR   Orari (UTC): {', '.join(pcfg.entry_slots_utc)} lun-ven",
        f"Chiave: impronta {fingerprint}",
        f"Permessi: vista={perms.can_view} scambio={perms.can_trade} prelievi={perms.can_transfer}",
        f"EUR disponibili: {available}",
        f"Ingresso: {_entry_mode_text(entry_mode)}",
        "Verranno inviati ORDINI REALI di piccolo importo.",
    ]


async def execute_stage(cfg: AppConfig, deps: Deps, stage: str, key_file: str | None) -> int:
    """One stage, from the gate to the outcome file. Returns the process exit code."""
    pcfg = cfg.exec_probe
    out_dir = Path(pcfg.out_dir)
    can_send = stage != "preflight"
    try:
        key_path = check_gate(pcfg, deps.env, key_file, deps.repo_root, can_send_orders=can_send)
        if can_send:
            require_terminal(deps.stdin)
        creds = read_credentials(key_path)
    except GateRefused as exc:
        complain(deps, f"RIFIUTATO: {exc}")
        return EXIT_GATE
    secrets = secrets_of(creds)
    if deps.configure_logs:
        configure_logging(secrets)
    fingerprint = key_fingerprint(creds)
    now = deps.clock()
    try:
        preview_record = check_chain(out_dir, stage, pcfg, now)
    except StageBlocked as exc:
        complain(deps, f"STADIO BLOCCATO: {exc}")
        return EXIT_GATE
    if stage == "expire" and preview_record is not None and preview_record.entry_mode != "gtd":
        write_stage(
            out_dir, skipped_record(stage, pcfg, now, "the entry is not GTD: nothing to expire on the exchange")
        )
        say(deps, "Stadio 'expire' non applicabile (ingresso non GTD): saltato.")
        return EXIT_OK

    read_ex = deps.read_exchange(creds)
    try:
        if stage == "preflight":
            record, lines = await preflight(pcfg, read_ex, fingerprint=fingerprint, clock=deps.clock, sleep=deps.sleep)
            return _finish_stage(deps, out_dir, record, lines)
        perms = await read_ex.get_api_key_permissions()
        snapshot = await read_ex.get_accounts()
        available = str(snapshot.balances.get(pcfg.product.split("-")[1], (Decimal(0), Decimal(0)))[0])
        summary = _summary(
            pcfg, fingerprint, perms, available, None if preview_record is None else preview_record.entry_mode
        )
        if not ask_confirmation(pcfg, summary, deps.stdin, deps.stdout):
            complain(deps, "Conferma non data: nessun ordine inviato.")
            return EXIT_GATE
        if stage == "preview":
            write_ex = deps.write_exchange(creds)
            assert_live_key_permissions(await write_ex.get_api_key_permissions())
            record, lines = await preview(pcfg, write_ex, fallback=FallbackFees(), clock=deps.clock)
            return _finish_stage(deps, out_dir, record, lines)
        return await _run_runtime_stage(cfg, deps, stage, creds, secrets, out_dir)
    except SpotViolation as exc:
        complain(deps, f"VIOLAZIONE SPOT: {exc}")
        return SPOT_EXIT_CODE
    except DryRunViolation as exc:
        complain(deps, f"VIOLAZIONE: scrittura su un exchange in sola lettura: {exc}")
        return EXIT_DRY_VIOLATION
    except ExchangeAuthError as exc:
        complain(deps, f"CREDENZIALI RIFIUTATE da Coinbase: {exc}")
        return EXIT_FAILED


async def execute_report(cfg: AppConfig, deps: Deps, key_file: str | None) -> int:
    """``report``: REST reads only (acts 1 and 3 of the gate). Writes ``probe_report.json`` / ``.md``."""
    pcfg = cfg.exec_probe
    out_dir = Path(pcfg.out_dir)
    try:
        creds = read_credentials(check_gate(pcfg, deps.env, key_file, deps.repo_root, can_send_orders=False))
    except GateRefused as exc:
        complain(deps, f"RIFIUTATO: {exc}")
        return EXIT_GATE
    if deps.configure_logs:
        configure_logging(secrets_of(creds))
    exchange = deps.read_exchange(creds)
    ledger = await PositionLedger.open(Path(pcfg.db_path), "live")
    try:
        report = await build_report(pcfg, exchange, ledger, out_dir, entry_poll_s=cfg.execution.entry_poll_s)
    except ExchangeAuthError as exc:
        complain(deps, f"CREDENZIALI RIFIUTATE da Coinbase: {exc}")
        return EXIT_FAILED
    finally:
        await ledger.close()
    js, md = write_report(out_dir, report)
    say(deps, render_markdown(report))
    say(deps, f"Scritti {js} e {md}")
    return EXIT_OK if report["validated"] else EXIT_FAILED


def _finish_stage(deps: Deps, out_dir: Path, record: StageRecord, lines: list[str]) -> int:
    path = write_stage(out_dir, record)
    for line in lines:
        say(deps, line)
    say(deps, f"Esito dello stadio '{record.stage}': {'OK' if record.ok else 'NON OK'}  ({path})")
    return EXIT_OK if record.ok else EXIT_FAILED


async def _run_runtime_stage(
    cfg: AppConfig, deps: Deps, stage: str, creds: CoinbaseConfig, secrets: Sequence[str], out_dir: Path
) -> int:
    pcfg = cfg.exec_probe
    marker = FinishMarker(out_dir)
    if marker.read() is not None and stage != "live":
        complain(deps, f"La prova e' terminata ({marker.read()}): rimuovi {marker.path} solo dopo l'analisi.")
        return EXIT_GATE
    preview_record = read_stage(out_dir, "preview")
    rt = await build_runtime(cfg, deps, creds, secrets, None if preview_record is None else preview_record.entry_mode)
    if deps.on_runtime is not None:
        deps.on_runtime(rt)
    sha = config_hash(pcfg)
    outcome: list[StageRecord] = []

    async def job() -> None:
        if stage in ("place-cancel", "expire", "reject"):
            host = OrderStageHost(
                cfg=pcfg, meta=rt.registry.meta(pcfg.product), gateway=rt.gateway, ledger=rt.ledger,
                balances=rt.balances, exchange=rt.exchange, clock=deps.clock, sleep=deps.sleep,
                raw_path=rt.raw_log.path, flush_raw=rt.raw_log.flush, config_sha256=sha,
                entry_mode=None if preview_record is None else preview_record.entry_mode,
            )  # fmt: skip
            record, lines = await run_order_stage(stage, host)
            for line in lines:
                say(deps, line)
            outcome.append(record)
        elif stage == "stop-switch":
            outcome.append(await _stop_switch_stage(rt, sha))
        else:
            raise AssertionError(stage)

    if stage == "live":
        say(deps, "Prova avviata. Ctrl+C per fermare (l'ordine d'ingresso pendente viene annullato; gli stop restano).")
        marker_now = marker.read()
        if marker_now is not None:
            complain(deps, f"ATTENZIONE: la prova risulta terminata ({marker_now.reason}); nessun nuovo ingresso.")
        await rt.run(scheduler=True)
        record = StageRecord("live", True, deps.clock(), sha, details={"finished": rt.marker.read() is not None})
        write_stage(out_dir, record)
        return EXIT_OK
    await rt.run(job, status_every_s=1.0)
    if not outcome:
        return EXIT_FAILED
    return _finish_stage(deps, out_dir, outcome[0], [])


async def _stop_switch_stage(rt: ProbeRuntime, sha: str) -> StageRecord:
    """Prove the STOP switch: it is created, ``status`` says ``entries_blocked: manual_stop`` within seconds, then the
    operator removes the file by hand (the code has no way to) and the block goes away."""
    deps, out_dir = rt.deps, rt.out_dir
    checks: dict[str, bool] = {}

    async def wait_status(expected: str | None, timeout_s: float) -> bool:
        waited = 0.0
        while waited <= timeout_s:
            status = read_status(out_dir)
            if status is not None and status.get("entries_blocked") == expected:
                return True
            await deps.sleep(1.0)
            waited += 1.0
        return False

    rt.switches.request_stop()
    say(deps, f"Creato {rt.switches.stop_path}: controllo che lo stato dica 'manual_stop'...")
    checks["status_reports_manual_stop"] = await wait_status("manual_stop", 30.0)
    say(deps, f"ORA cancella A MANO il file {rt.switches.stop_path} (il programma non lo fa); attendo fino a 5 minuti.")
    waited = 0.0
    while rt.switches.stop_active() and waited < 300.0:
        await deps.sleep(1.0)
        waited += 1.0
    checks["operator_removed_the_file"] = not rt.switches.stop_active()
    checks["status_released"] = checks["operator_removed_the_file"] and await wait_status(None, 30.0)
    for name, value in checks.items():
        say(deps, f"  [{'OK' if value else 'NO'}] {name}")
    return StageRecord("stop-switch", all(checks.values()), deps.clock(), sha, details={"checks": checks})
