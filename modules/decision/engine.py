"""DecisionEngine: from a published 1m bar to a ``DecisionRecord`` and, when the book allows it, an order.

Spec sez. 2.1, 2.2.4, 2.3 C.5, 4.1 steps 1-9; docs/decisions/2026-10-18-barre-rest.md Q2.

The runtime walks the SAME chain as ``training/backtest.py``, one bar at a time instead of one array at a time:

    IndicatorEngine.update -> StateBuilder.build (flat state of the ledger position) -> backend answers
    -> DecisionPolicy.resolve(vol_pctile = state.volatility.atr_1d_pctile_365d) -> RiskManager.plan_entry / plan_exit
    -> ExecutionGateway.

Parity with the backtest is structural, not a copy: the objects are the same (``StateBuilder`` is pinned to
``flat_matrix`` by ``tests/test_state_matrix.py``, ``DecisionPolicy.resolve`` to ``buy_mask`` by
``tests/test_decision_policy.py``), the regime is the state token and never the model's ``vol_regime`` head (that goes
to the record as ``model_regime``, diagnostics only), and ``tests/test_decision_engine.py`` compares decisions and
intents with the batch path on the same bars.

Time (spec sez. 2.1). A ``BarClosed`` is published at ``t + S`` and ``t = bar.ts + 1 min`` is the logical instant: the
state, the sentiment (``as_of = t``) and the record's ``ts_bar_close`` use ``t``; ``decided_at`` is the instant of
computation. EVERY bar updates the ``IndicatorEngine``; a bar older than ``market.bars.decision_max_lag_s`` makes no
decision and no record (counter ``skipped_late``).

Orders. The engine never touches the exchange: it hands an ``OrderIntent`` / ``ExitIntent`` to the gateway it
receives, which authorizes every order through the BalanceGuard (C11). A gateway call can last minutes (a maker entry
waits up to 30 min), so it runs as a task; while it runs the product is busy and further decisions are recorded but not
executed (``entry_pending`` / ``exit_pending``). An entry is never started when its record could not be written
(no unaudited entry); exits are never held back.

Failures. Nothing escapes the loop: a failure of one product (state, decider, policy, risk, log) becomes an
``engine_error`` record for that product and the other products go on. A decider that is absent or fails falls back to
the baseline when ``decision.fallback_to_baseline`` is set, otherwise the decision is ``Decision.unavailable()``.
"""

from __future__ import annotations

import asyncio
import json
import math
import secrets
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import groupby
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, Final, Literal, Protocol

import pandas as pd
import structlog

from modules.config import BarsConfig, DecisionConfig
from modules.decision.client import DecideResult
from modules.decision.policy import REGIME_RULE, Decision, DecisionPolicy, PolicyConfig, regime_from_pctile
from modules.decision.protocol import ChoiceQuestion, DecisionAnswers, NoulQuestion, ScoreQuestion, regime_from_probs
from modules.decision.questions import QuestionSet
from modules.execution.orders import ProductMeta
from modules.features.serializer import serialize_state, state_sha256
from modules.features.state_builder import PositionInput, StateBuilder
from modules.ledger.positions import EntryStamp, PositionView
from modules.market.bars import BarClosed
from modules.market.indicators import IndicatorEngine, IndicatorSnapshot
from modules.risk.costs import FeeRates
from modules.risk.label_config import LoadedLabelConfig
from modules.risk.risk_manager import ExitIntent, MarketQuote, OrderIntent, RiskManager, Skip
from modules.sentiment.schemas import SentimentSnapshot

if TYPE_CHECKING:
    from modules.execution.gateway import EntryResult, ExecutionGateway, ExitResult

__all__ = [
    "BaselineBackend",
    "BookContext",
    "DecisionEngine",
    "DecisionLog",
    "DecisionSink",
    "EngineStats",
    "ExecutionReport",
    "KEEP_BACKEND",
    "ModelSwap",
    "PositionSource",
    "Processed",
    "RemoteBackend",
    "SentimentSource",
    "new_ulid",
]

log = structlog.get_logger(__name__)

Clock = Callable[[], datetime]
Questions = Mapping[str, ChoiceQuestion | NoulQuestion | ScoreQuestion]
Kind = Literal["entry", "exit"]

DECIDER_SLOTS: Final = 4  # concurrent calls to the decider (spec sez. 2.3 C.5)
_CROCKFORD: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


# ------------------------------------------------------------------------------------------------ ids / log


def new_ulid(at: datetime) -> str:
    """26-character ULID: 48 bits of milliseconds since the epoch (``at``) and 80 random bits, Crockford base32."""
    value = (int(at.timestamp() * 1000) << 80) | secrets.randbits(80)
    return "".join(_CROCKFORD[(value >> shift) & 31] for shift in range(125, -1, -5))


class DecisionSink(Protocol):
    """Where the records go. ``append`` may raise: the engine isolates the failure."""

    async def append(self, record: Mapping[str, Any], day: date) -> None: ...


class DecisionLog:
    """Append-only JSONL, one file per UTC day: ``<root>/YYYY-MM-DD.jsonl`` (spec sez. 0.5: ``data/decisions/``).

    A line is written whole and flushed before ``append`` returns. The write runs in a worker thread (one at a time) so
    the event loop never waits on the disk; the file of the day stays open and rotates when the day changes.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._lock = asyncio.Lock()
        self._fh: IO[str] | None = None
        self._day: date | None = None

    @classmethod
    def under(cls, data_dir: str | Path) -> DecisionLog:
        return cls(Path(data_dir) / "decisions")

    async def append(self, record: Mapping[str, Any], day: date) -> None:
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        async with self._lock:
            await asyncio.to_thread(self._write, line, day)

    def _write(self, line: str, day: date) -> None:
        if self._fh is None or self._day != day:
            if self._fh is not None:
                self._fh.close()
            self._root.mkdir(parents=True, exist_ok=True)
            self._fh = open(self._root / f"{day.isoformat()}.jsonl", "a", encoding="utf-8", newline="\n")  # noqa: SIM115
            self._day = day
        self._fh.write(line + "\n")
        self._fh.flush()

    async def aclose(self) -> None:
        async with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None


# ------------------------------------------------------------------------------------------- collaborators


class PositionSource(Protocol):
    """The ledger of ONE book as the engine reads it (``PositionLedger.book_view(book)``)."""

    def view(self, product_id: str) -> PositionView: ...


class SentimentSource(Protocol):
    def snapshot(self, product_id: str, as_of: datetime) -> SentimentSnapshot: ...


class BookContext(Protocol):
    """What the RiskManager needs from the live world and the engine does not own (steps 1, 2, 8 of sez. 4.1).

    ``quote``: the current ``LocalOrderBook`` best bid/ask (``None`` when the book is empty or stale);
    ``equity``: cash plus the positions marked at ``marks`` (last close per product); ``free_quote``: the free quote
    balance of the in-memory ``BalanceBook``. The BalanceGuard stays the final authority on coverage at submission.
    """

    def quote(self, product_id: str) -> MarketQuote | None: ...

    def fees(self) -> FeeRates: ...

    def meta(self, product_id: str) -> ProductMeta: ...

    def equity(self, marks: Mapping[str, Decimal]) -> Decimal: ...

    def free_quote(self) -> Decimal: ...


class BaselineBackend(Protocol):
    """``BaselineDecider``: in-process, takes the question dict."""

    async def decide(self, state: str, questions: Questions) -> DecisionAnswers: ...


class RemoteBackend(Protocol):
    """``SystemOneClient``: HTTP, takes the ``QuestionSet``."""

    async def decide(self, state: str, questions: QuestionSet) -> DecideResult: ...


# ------------------------------------------------------------------------------------------------ results


@dataclass(frozen=True, slots=True)
class Processed:
    """What the engine did with one bar of one product (also what the caller gets back from ``on_bars``)."""

    decision_id: str
    product_id: str
    ts_bar_close: datetime
    decision: Decision | None
    intent: OrderIntent | ExitIntent | None
    skip_reason: str | None
    skip_detail: str
    record: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    """The end of a gateway call, for whoever wants to follow it (telemetry); never raises into the engine."""

    decision_id: str
    product_id: str
    kind: Kind
    result: EntryResult | ExitResult | None
    error: str | None


@dataclass
class EngineStats:
    bars: int = 0
    skipped_late: int = 0
    out_of_order: int = 0
    decisions: int = 0
    fallbacks: int = 0
    unavailable: int = 0
    errors: int = 0
    log_errors: int = 0
    intents: int = 0
    actions: Counter[str] = field(default_factory=Counter)
    reasons: Counter[str] = field(default_factory=Counter)
    skips: Counter[str] = field(default_factory=Counter)


@dataclass
class _Answered:
    answers: DecisionAnswers | None
    backend: str | None
    model: str | None
    inference_ms: float
    http_total_ms: float | None = None
    server_model_ms: float | None = None
    fallback: bool = False
    error: str | None = None


@dataclass
class _Work:
    event: BarClosed
    snapshot: IndicatorSnapshot | None  # None: the product's indicators are down (see ``_ingest``)
    decided_at: datetime
    decision_id: str
    view: PositionView | None = None
    build_ms: float = 0.0
    state_str: str = ""
    state_sha: str = ""
    vol_pctile: int | None = None
    p_breakeven: float | None = None
    side: Literal["FLAT", "LONG"] = "FLAT"
    answered: _Answered | None = None


def _iso(t: datetime, spec: Literal["seconds", "milliseconds"]) -> str:
    return t.astimezone(UTC).isoformat(timespec=spec).replace("+00:00", "Z")


def _dec(x: float) -> Decimal:
    return Decimal(repr(float(x)))


LABEL_CONFIG_MISMATCH: Final = "label_config_mismatch"
KEEP_BACKEND: Final = object()  # "leave the backend as it is" in ``DecisionEngine.build_swap``


@dataclass(frozen=True)
class ModelSwap:
    """The model parts of an engine, ready to be installed (``DecisionEngine.build_swap`` makes them, ``apply_swap``
    installs them). Everything that can fail is done when it is built; installing it only assigns references."""

    version: str
    label: LoadedLabelConfig
    policy_cfg: PolicyConfig
    policy: DecisionPolicy
    questions: QuestionSet
    risk: RiskManager
    builder: StateBuilder
    baseline: BaselineBackend | None
    remote: RemoteBackend | None


# -------------------------------------------------------------------------------------------------- engine


class DecisionEngine:
    """One per book (spec sez. 0.6). Single consumer: ``on_bars`` is not re-entrant, ``run`` drives it from a queue."""

    def __init__(
        self,
        *,
        products: Sequence[str],
        label: LoadedLabelConfig,
        policy: PolicyConfig,
        questions: QuestionSet,
        risk: RiskManager,
        gateway: ExecutionGateway,
        positions: PositionSource,
        context: BookContext,
        decision: DecisionConfig,
        bars: BarsConfig,
        sink: DecisionSink,
        baseline: BaselineBackend | None = None,
        remote: RemoteBackend | None = None,
        sentiment: SentimentSource | None = None,
        clock: Clock | None = None,
        new_id: Callable[[datetime], str] = new_ulid,
        cooldown_s: float = 0.0,
        btc_product: str = "BTC-EUR",
        on_execution: Callable[[ExecutionReport], None] | None = None,
        model_version: str | None = None,
        stamp_entry: Callable[[str, EntryStamp], None] | None = None,
    ) -> None:
        if not products:
            raise ValueError("the engine needs at least one product")
        if policy.label_config_sha256 != label.sha256:
            raise ValueError("policy.json was tuned for another label_config than the model's")
        if baseline is None and (decision.backend == "baseline" or remote is None):
            raise ValueError("no decider: the baseline backend is required unless a remote backend is given")
        if cooldown_s < 0:
            raise ValueError("cooldown_s cannot be negative")
        self._products = tuple(products)
        self._label = label
        self._atr_attr = label.cfg.atr_column
        self._policy_cfg = policy
        self._policy = DecisionPolicy(policy)
        self._questions = questions
        self._risk = risk
        self._gateway = gateway
        self._positions = positions
        self._ctx = context
        self._decision_cfg = decision
        self._bars_cfg = bars
        self._sink = sink
        self._baseline = baseline
        self._remote = remote
        self._sentiment = sentiment
        self._clock: Clock = clock or (lambda: datetime.now(UTC))
        self._new_id = new_id
        self._cooldown_s = cooldown_s
        self._btc = btc_product
        self._on_execution = on_execution
        self._builder = StateBuilder(label)
        self._model_version = model_version or policy.model
        self._stamp_entry = stamp_entry  # tells the ledger which model decided an entry (before the order)
        self._slots = asyncio.Semaphore(DECIDER_SLOTS)
        self.indicators: dict[str, IndicatorEngine] = {p: IndicatorEngine(p) for p in self._products}
        self._last_ts: dict[str, datetime] = {}
        self._last_close: dict[str, float] = {}
        self._snap: dict[str, IndicatorSnapshot] = {}
        self._broken: dict[str, str] = {}
        self._busy: dict[str, Kind] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._next_ok: dict[str, datetime] = {}
        self.stats = EngineStats()

    # ------------------------------------------------------------------------------------------ public

    def warm_up(self, product_id: str, bars_1m: pd.DataFrame) -> None:
        """Replace the product's indicators with a fresh engine fed the whole history (spec sez. 2.3 A.4)."""
        engine = IndicatorEngine(product_id)
        engine.warm_up(bars_1m)
        last_ts = pd.Timestamp(bars_1m.index[-1]).to_pydatetime()
        self.adopt(product_id, engine, last_ts, float(bars_1m["close"].iloc[-1]))

    def adopt(self, product_id: str, indicators: IndicatorEngine, last_bar_ts: datetime, last_close: float) -> None:
        """Take over an ``IndicatorEngine`` that was warmed up elsewhere (a worker thread: three years of bars take a
        while) and which has consumed every bar up to ``last_bar_ts``. Clears a ``broken`` mark."""
        if product_id not in self.indicators:
            raise KeyError(product_id)
        self.indicators[product_id] = indicators
        self._last_ts[product_id] = last_bar_ts
        self._last_close[product_id] = last_close
        self._broken.pop(product_id, None)

    @property
    def model_version(self) -> str:
        return self._model_version

    @property
    def label(self) -> LoadedLabelConfig:
        return self._label

    def build_swap(
        self, *, version: str, label: LoadedLabelConfig, policy: PolicyConfig, questions: QuestionSet,
        risk: RiskManager, baseline: BaselineBackend | None | object = KEEP_BACKEND,
        remote: RemoteBackend | None | object = KEEP_BACKEND,
    ) -> ModelSwap:  # fmt: skip
        """The parts of another model version for this engine. Nothing is installed: every check that can fail
        (the policy was tuned for another label_config, the state builder cannot be made) fails HERE, so an
        installation never fails half way. ``baseline`` / ``remote`` left out keep the current ones (a random book
        keeps its decider)."""
        if policy.label_config_sha256 != label.sha256:
            raise ValueError("policy.json was tuned for another label_config than the model's")
        return ModelSwap(
            version, label, policy, DecisionPolicy(policy), questions, risk, StateBuilder(label),
            self._baseline if baseline is KEEP_BACKEND else baseline,  # type: ignore[arg-type]
            self._remote if remote is KEEP_BACKEND else remote,  # type: ignore[arg-type]
        )  # fmt: skip

    def apply_swap(self, swap: ModelSwap) -> None:
        """Install ``swap``. Synchronous and without an ``await``: it cannot interleave with a decision. The caller
        runs it BETWEEN two ``on_bars`` calls (the runtime holds its bar lock), so a decision is made entirely with
        one model; a gateway call already running keeps the intent it was started with."""
        self._label = swap.label
        self._atr_attr = swap.label.cfg.atr_column
        self._policy_cfg = swap.policy_cfg
        self._policy = swap.policy
        self._questions = swap.questions
        self._risk = swap.risk
        self._builder = swap.builder
        self._baseline = swap.baseline
        self._remote = swap.remote
        self._model_version = swap.version

    def pending(self, product_id: str) -> Kind | None:
        """``entry`` / ``exit`` while a gateway call of the product is running."""
        return self._busy.get(product_id)

    async def run(self, queue: asyncio.Queue[BarClosed]) -> None:
        """Consume ``queue`` until cancelled. Events that are already waiting are processed together, so the products
        of one minute are all in the indicators before any of them decides (BTC context)."""
        while True:
            batch = [await queue.get()]
            while True:
                try:
                    batch.append(queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                await self.on_bars(batch)
            except Exception:  # defence in depth: on_bars isolates the products itself
                log.exception("engine_batch_failed", n=len(batch))
                self.stats.errors += 1

    async def on_bar(self, event: BarClosed) -> list[Processed]:
        return await self.on_bars([event])

    async def on_bars(self, events: Iterable[BarClosed]) -> list[Processed]:
        out: list[Processed] = []
        ordered = sorted(events, key=lambda e: (e.bar.ts, e.product_id))
        for _, group in groupby(ordered, key=lambda e: e.bar.ts):
            works = [w for e in group if (w := self._ingest(e)) is not None]
            out += await self._decide_group(works)
        return out

    async def drain(self, timeout: float | None = None) -> None:
        """Wait for the running gateway calls (shutdown: they are never cancelled here)."""
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=timeout)

    # ------------------------------------------------------------------------------- 1. bar -> snapshot

    def _ingest(self, event: BarClosed) -> _Work | None:
        """Feed the bar to the indicators (always) and return the work item when a decision is due."""
        p, bar = event.product_id, event.bar
        engine = self.indicators.get(p)
        if engine is None:
            log.error("engine_unknown_product", product=p)
            self.stats.errors += 1
            return None
        last = self._last_ts.get(p)
        if last is not None and bar.ts <= last:
            self.stats.out_of_order += 1  # a bar is consumed once: a replay or a duplicate changes nothing
            return None
        self.stats.bars += 1
        decided_at = self._clock()
        snapshot: IndicatorSnapshot | None = None
        if p not in self._broken:
            try:
                snapshot = engine.update(bar)
                self._snap[p] = snapshot
            except Exception as exc:  # a gap or a corrupt bar: the indicators cannot be trusted until a new warm_up
                self._broken[p] = f"{type(exc).__name__}: {exc}"
                log.error("engine_indicators_broken", product=p, error=self._broken[p])
                self.stats.errors += 1
        self._last_ts[p] = bar.ts
        self._last_close[p] = bar.close
        # a bar that arrives too late (lag in the publication or in the engine itself) feeds the indicators only
        if max(event.lag_s, (decided_at - event.t).total_seconds()) > self._bars_cfg.decision_max_lag_s:
            self.stats.skipped_late += 1
            return None
        return _Work(event, snapshot, decided_at, self._new_id(decided_at))

    # ----------------------------------------------------------------------------------- 2. decide group

    async def _decide_group(self, works: list[_Work]) -> list[Processed]:
        out: list[Processed] = []
        ready: list[_Work] = []
        for w in works:
            try:
                self._prepare(w)
                ready.append(w)
            except Exception as exc:
                out.append(await self._fail(w, "state", exc))
        answered = await asyncio.gather(*(self._ask(w) for w in ready))
        for w, a in zip(ready, answered, strict=True):
            w.answered = a
            try:
                out.append(await self._conclude(w))
            except Exception as exc:
                out.append(await self._fail(w, "conclude", exc))
        return out

    def _prepare(self, w: _Work) -> None:
        """Build the state of the logical instant ``t`` (sentiment as of ``t``, position from the ledger)."""
        started = time.perf_counter()
        p, snap = w.event.product_id, w.snapshot
        if snap is None:
            raise RuntimeError(f"indicators of {p} are down: {self._broken.get(p, 'unknown')}")
        w.view = self._positions.view(p)
        btc = self._snap.get(self._btc)
        btc = btc if (btc is not None and btc.ts == snap.ts and not p.startswith("BTC-")) else None
        state = self._builder.build(
            snapshot=snap,
            btc=btc,
            sentiment=self._read_sentiment(p, w.event.t),
            position=PositionInput.from_view(w.view),
            close_t=w.event.bar.close,
        )
        w.state_str = serialize_state(state)
        w.state_sha = state_sha256(w.state_str)
        w.vol_pctile = state.volatility.atr_1d_pctile_365d
        w.p_breakeven = state.costs.p_breakeven
        w.side = state.position.side
        w.build_ms = (time.perf_counter() - started) * 1000

    def _read_sentiment(self, product_id: str, as_of: datetime) -> SentimentSnapshot | None:
        if self._sentiment is None:
            return None
        try:
            return self._sentiment.snapshot(product_id, as_of)
        except Exception as exc:  # no sentiment is a valid state (the model is trained for it): never a reason to stop
            log.warning("engine_sentiment_unavailable", product=product_id, error=f"{type(exc).__name__}: {exc}")
            return None

    # --------------------------------------------------------------------------------------- 3. decider

    async def _ask(self, w: _Work) -> _Answered:
        """Answers for the state: the configured backend, then the baseline, else nothing (never raises)."""
        started = time.perf_counter()
        error: str | None = None
        backend = self._decision_cfg.backend
        if backend != "baseline":
            if self._remote is None:
                error = "remote decider not configured"
            else:
                try:
                    async with self._slots:
                        res = await self._remote.decide(w.state_str, self._questions)
                    return _Answered(
                        res.answers, backend, res.model, (time.perf_counter() - started) * 1000,
                        res.http_total_ms, res.server_timing_ms.get("model"),
                    )  # fmt: skip
                except Exception as exc:  # DeciderError, or a bug of the client: both mean "no answer from the remote"
                    error = f"{type(exc).__name__}: {exc}"
            if not self._decision_cfg.fallback_to_baseline:
                return _Answered(None, backend, None, (time.perf_counter() - started) * 1000, error=error)
        if self._baseline is None:
            return _Answered(None, backend, None, (time.perf_counter() - started) * 1000, error=error or "no baseline")
        try:
            answers = await self._baseline.decide(w.state_str, self._questions.questions)
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            return _Answered(None, "baseline", None, (time.perf_counter() - started) * 1000, error=error or msg)
        return _Answered(
            answers.with_recomputed_confidence(), "baseline", self._policy_cfg.model,
            (time.perf_counter() - started) * 1000, fallback=error is not None, error=error,
        )  # fmt: skip

    # ------------------------------------------------------------------------- 4. policy, risk, gateway

    async def _conclude(self, w: _Work) -> Processed:
        p, ans = w.event.product_id, w.answered
        assert ans is not None
        if ans.answers is None:
            decision = Decision.unavailable()
        else:
            decision = self._policy.resolve(ans.answers, w.side, vol_pctile=w.vol_pctile)
            if decision.action == "SELL" and self._foreign_label(w.view):
                # the SELL head of this model was trained on other barriers than the ones the position was opened
                # under: it does not apply. Bracket, the position's own horizon and the watchdog still govern it.
                decision = replace(decision, action="HOLD", reason=LABEL_CONFIG_MISMATCH)

        intent: OrderIntent | ExitIntent | None = None
        skip: tuple[str, str] | None = None
        if decision.action == "BUY":
            intent, skip = self._plan_entry(w, decision)
        elif decision.action == "SELL":
            intent, skip = self._plan_exit(w, decision)

        record = self._record(w, ans, decision, intent, skip)
        audited = await self._write(record, w.decided_at)
        if isinstance(intent, OrderIntent) and not audited:
            intent, skip = None, ("audit_unavailable", "the record could not be written: no unaudited entry")
            record["risk"] = {"outcome": "skip", "reason": skip[0], "detail": skip[1]}
            record["order_intent_id"] = None
        if intent is not None:
            self.stats.intents += 1
            if isinstance(intent, OrderIntent) and self._stamp_entry is not None:
                try:  # the position this entry opens records the model that decided it, whenever it fills
                    self._stamp_entry(
                        w.decision_id,
                        EntryStamp(self._model_version, self._label.sha256, self._label.cfg.horizon_min),
                    )
                except Exception:
                    log.exception("engine_entry_stamp_failed", product=p, decision_id=w.decision_id)
            self._launch(p, intent, w.decision_id)
            await asyncio.sleep(0)  # let the gateway reserve the balance before the next product plans its size
        self._count(decision, ans, skip)
        return Processed(
            w.decision_id, p, w.event.t, decision, intent, None if skip is None else skip[0],
            "" if skip is None else skip[1], record,
        )  # fmt: skip

    def _foreign_label(self, view: PositionView | None) -> bool:
        """The LONG position was opened under another ``label_config`` (sha256) than the one this engine runs. A
        position that records none (opened before it was recorded; the app stamps those at start) is not foreign."""
        return (
            view is not None and view.is_long and view.label_sha256 is not None
            and view.label_sha256 != self._label.sha256
        )  # fmt: skip

    def _plan_entry(self, w: _Work, decision: Decision) -> tuple[OrderIntent | None, tuple[str, str] | None]:
        p = w.event.product_id
        assert w.view is not None and w.snapshot is not None
        if (kind := self._busy.get(p)) is not None:
            return None, (f"{kind}_pending", f"a gateway {kind} of {p} is still running")
        if w.decided_at < self._next_ok.get(p, w.decided_at):
            return None, ("cooldown", f"no new entry before {_iso(self._next_ok[p], 'milliseconds')}")
        atr = getattr(w.snapshot, self._atr_attr)
        if not (math.isfinite(atr) and atr > 0):
            return None, ("no_quote", f"{self._atr_attr} unavailable")  # same exclusion as the backtest's decision rows
        quote = self._ctx.quote(p)
        if quote is None:
            return None, ("no_quote", "no usable best bid/ask")
        marks = {name: _dec(c) for name, c in self._last_close.items()}
        plan = self._risk.plan_entry(
            decision=decision, decision_id=w.decision_id, intent_id=self._new_id(w.decided_at), now=w.decided_at,
            product_id=p, quote=quote, atr=_dec(atr), fees=self._ctx.fees(), meta=self._ctx.meta(p),
            equity=self._ctx.equity(marks), free_quote=self._ctx.free_quote(), p_be_buffer=self._policy_cfg.p_be_buffer,
            position=w.view,
        )  # fmt: skip
        if isinstance(plan, Skip):
            return None, (plan.reason, plan.detail)
        return plan, None

    def _plan_exit(self, w: _Work, decision: Decision) -> tuple[ExitIntent | None, tuple[str, str] | None]:
        p = w.event.product_id
        assert w.view is not None
        if (kind := self._busy.get(p)) is not None:
            return None, (f"{kind}_pending", f"a gateway {kind} of {p} is still running")
        plan = self._risk.plan_exit(
            decision=decision, decision_id=w.decision_id, intent_id=self._new_id(w.decided_at), now=w.decided_at,
            position=w.view,
        )  # fmt: skip
        return plan, None

    # ------------------------------------------------------------------------------------- gateway task

    def _launch(self, product_id: str, intent: OrderIntent | ExitIntent, decision_id: str) -> None:
        kind: Kind = "entry" if isinstance(intent, OrderIntent) else "exit"
        self._busy[product_id] = kind  # set before the task exists: nothing can slip between two decisions
        coro = self._run_gateway(product_id, kind, intent, decision_id)
        task = asyncio.create_task(coro, name=f"{kind}-{product_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_gateway(
        self, product_id: str, kind: Kind, intent: OrderIntent | ExitIntent, decision_id: str
    ) -> None:
        result: EntryResult | ExitResult | None = None
        error: str | None = None
        try:
            if isinstance(intent, OrderIntent):
                result = await self._gateway.submit_entry_with_bracket(intent)
            else:
                result = await self._gateway.submit_exit(intent)
        except Exception as exc:  # the gateway returns typed results; this is a bug or an outage, never a stop
            error = f"{type(exc).__name__}: {exc}"
            log.exception("engine_gateway_failed", product=product_id, kind=kind, decision_id=decision_id)
            self.stats.errors += 1
        finally:
            self._busy.pop(product_id, None)
            self._next_ok[product_id] = self._clock() + timedelta(seconds=self._cooldown_s)
        if self._on_execution is not None:
            try:
                self._on_execution(ExecutionReport(decision_id, product_id, kind, result, error))
            except Exception:
                log.exception("engine_execution_callback_failed", product=product_id)

    # ------------------------------------------------------------------------------------------ record

    def _record(
        self, w: _Work, ans: _Answered, decision: Decision, intent: OrderIntent | ExitIntent | None,
        skip: tuple[str, str] | None,
    ) -> dict[str, Any]:  # fmt: skip
        cfg = self._policy_cfg
        answers = ans.answers
        regime = regime_from_pctile(None if w.vol_pctile is None else float(w.vol_pctile))
        risk_outcome = "intent" if intent is not None else ("skip" if skip is not None else "none")
        return {
            "decision_id": w.decision_id,
            "ts_bar_close": _iso(w.event.t, "seconds"),
            "decided_at": _iso(w.decided_at, "milliseconds"),
            "bar_source": w.event.source,
            "bar_lag_s": round(w.event.lag_s, 3),
            "product": w.event.product_id,
            "book": self._risk.book,
            "state_sha256": w.state_sha,
            "state": w.state_str,
            "questions_version": self._questions.version_tag,
            "backend": ans.backend,
            "model": ans.model,
            "fallback": ans.fallback,
            "decider_error": ans.error,
            "answers": None if answers is None else answers.model_dump(mode="json"),
            "policy": {
                "policy_version": cfg.model,
                "regime": regime,
                "regime_pctile": w.vol_pctile,
                "regime_rule": REGIME_RULE,
                "model_regime": None if answers is None else regime_from_probs(answers.vol_regime.probabilities),
                "masked_probs": decision.masked_probs,
                "final": decision.action,
                "reason": decision.reason,
                "tau_buy": cfg.tau_buy,
                "tau_sell": cfg.tau_sell,
                "c_min": cfg.c_min,
                "p_be_buffer": cfg.p_be_buffer,
                "p_breakeven": w.p_breakeven,
            },
            "risk": {
                "outcome": risk_outcome,
                "reason": None if skip is None else skip[0],
                "detail": "" if skip is None else skip[1],
            },
            "latency_ms": {
                "build": round(w.build_ms, 3),
                "inference": round(ans.inference_ms, 3),
                "http_total": None if ans.http_total_ms is None else round(ans.http_total_ms, 3),
                "server_model": ans.server_model_ms,
            },
            "order_intent_id": None if intent is None else intent.intent_id,
        }

    async def _write(self, record: dict[str, Any], decided_at: datetime) -> bool:
        try:
            await self._sink.append(record, decided_at.astimezone(UTC).date())
        except Exception as exc:
            self.stats.log_errors += 1
            log.error(
                "engine_record_not_written",
                decision_id=record.get("decision_id"),
                error=f"{type(exc).__name__}: {exc}",
                record=json.dumps(record, default=str),
            )
            return False
        return True

    async def _fail(self, w: _Work, stage: str, exc: Exception) -> Processed:
        """One product failed: say so in its record and in the log, leave the others alone."""
        self.stats.errors += 1
        message = f"{type(exc).__name__}: {exc}"
        log.error("engine_product_failed", product=w.event.product_id, stage=stage, error=message, exc_info=exc)
        record: dict[str, Any] = {
            "decision_id": w.decision_id,
            "ts_bar_close": _iso(w.event.t, "seconds"),
            "decided_at": _iso(w.decided_at, "milliseconds"),
            "bar_source": w.event.source,
            "bar_lag_s": round(w.event.lag_s, 3),
            "product": w.event.product_id,
            "book": self._risk.book,
            "questions_version": self._questions.version_tag,
            "policy": {"policy_version": self._policy_cfg.model, "final": "HOLD", "reason": "engine_error"},
            "error": {"stage": stage, "type": type(exc).__name__, "message": str(exc)},
            "order_intent_id": None,
        }
        await self._write(record, w.decided_at)
        self.stats.actions["HOLD"] += 1
        self.stats.reasons["engine_error"] += 1
        return Processed(w.decision_id, w.event.product_id, w.event.t, None, None, "engine_error", message, record)

    def _count(self, decision: Decision, ans: _Answered, skip: tuple[str, str] | None) -> None:
        st = self.stats
        st.decisions += 1
        st.actions[decision.action] += 1
        st.reasons[decision.reason] += 1
        if ans.fallback:
            st.fallbacks += 1
        if ans.answers is None:
            st.unavailable += 1
        if skip is not None:
            st.skips[skip[0]] += 1
