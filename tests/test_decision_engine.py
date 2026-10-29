"""DecisionEngine: the runtime path of one bar, against fakes (spec sez. 2.1, 2.2.4, 2.3 C.5, 4.1).

The central test is the parity with the backtest: the same bars go through the engine (bar by bar, incremental
indicators, scalar ``StateBuilder``, ``DecisionPolicy.resolve``) and through the batch path of ``training/backtest.py``
(``compute_indicators`` -> ``flat_matrix`` -> ``answers_batch`` -> ``buy_mask`` -> ``plan_entry``), and must give the
same decision and the same ``OrderIntent`` on every row. The other tests pin the contracts the engine adds on top of
that chain: late bars, the logical instant, the sentiment instant, fallback of the decider, one position per product,
the kill switch, error isolation and the shape of the ``DecisionRecord``.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from modules.config import AggregationConfig, BarsConfig, DecisionConfig, EntryTimeout, FallbackFees, RiskConfig
from modules.decision.baseline import AnswerArrays, BaselineDecider
from modules.decision.client import DecideResult
from modules.decision.engine import (
    DecisionEngine,
    DecisionLog,
    ExecutionReport,
    Processed,
    new_ulid,
)
from modules.decision.policy import (
    REGIME_FEATURE,
    REGIME_RULE,
    Decision,
    DecisionPolicy,
    PolicyConfig,
    regime_from_pctile,
    regimes_from_pctile,
)
from modules.decision.protocol import ACTIONS, DeciderTimeout, DecisionAnswers
from modules.decision.questions import build_questions
from modules.execution.gateway import EntryResult, ExitResult
from modules.execution.ids import client_order_id
from modules.execution.orders import ProductMeta
from modules.features.schemas import DecisionState
from modules.features.serializer import FLAT_COLUMNS, flatten_state
from modules.features.state_matrix import flat_matrix
from modules.ledger.positions import PositionView
from modules.market.bars import BarClosed, BarSource
from modules.market.indicators import IndicatorEngine
from modules.market.timeframes import Bar
from modules.risk.costs import FeeRates
from modules.risk.risk_manager import ExitIntent, MarketQuote, OrderIntent, PolicyContractError, RiskManager
from modules.sentiment.aggregator import SentimentAggregator
from tests.indicator_fixtures import synthetic_bars
from tests.test_backtest import LABEL, POLICY, ExtremeRegimeStub, StubDecider, registry
from training.backtest import build_market

D = Decimal
BOOK = "book"
REGIME_COL = FLAT_COLUMNS.index(REGIME_FEATURE)
QUESTIONS = build_questions(LABEL.cfg.horizon_min, LABEL.cfg.k_tp, LABEL.cfg.k_sl, LABEL.cfg.atr_tf)
DAY = 1440
WARM_A = 139 * DAY  # BTC regime 1 (and 0 from day 140), SOL regime 3
WARM_B = 142 * DAY  # BTC regime 0 (veto), SOL regime 3/4


# ------------------------------------------------------------------------------------------------ deciders


class StubBaseline:
    """The ``BaselineDecider`` contract (``decide(state, questions)``) over an ``answers_batch`` object: one row at a
    time through ``flatten_state``, exactly what ``BaselineDecider.decide`` does with its boosters."""

    def __init__(self, inner: Any, *, fail: Exception | None = None) -> None:
        self.inner, self.fail, self.calls = inner, fail, 0

    async def decide(self, state: str, questions: Any) -> DecisionAnswers:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        flat = flatten_state(DecisionState.model_validate_json(state))
        arrays = self.inner.answers_batch(np.array([[flat[c] for c in FLAT_COLUMNS]]))
        return BaselineDecider._answers(arrays, 0)


class FeatureStub:
    """Answers that depend on several features, so that a wrong feature in the engine's state changes a decision."""

    model_key = "feature-stub"

    @staticmethod
    def _col(x: np.ndarray, name: str) -> np.ndarray:
        return np.nan_to_num(x[:, FLAT_COLUMNS.index(name)])

    def answers_batch(self, x: np.ndarray) -> AnswerArrays:
        c = self._col
        z = 0.02 * c(x, "ret_1h_bps") + 0.04 * (c(x, "rsi_14_1h") - 50) + 0.5 * (c(x, "hh_ll_pos_30d") - 0.5)
        buy = 0.9 / (1.0 + np.exp(-z))
        pa = np.column_stack([buy, np.full(len(x), 0.02), 0.98 - buy])
        zedge = 0.004 * c(x, "ret_6h_bps") - 0.03 * (c(x, "rsi_14_6h") - 50) + 0.01 * c(x, "dist_vwap_1d_bps")
        edge = 0.30 + 0.45 / (1.0 + np.exp(-zedge))
        reg = np.tile([0.1, 0.2, 0.4, 0.2, 0.1], (len(x), 1))
        return AnswerArrays(pa, edge, reg)


# ------------------------------------------------------------------------------------------------- fakes


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2024, 6, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class Gateway:
    """Records what the engine sends. An entry/exit can be held open (``hold``) or made to fail (``fail``)."""

    def __init__(self) -> None:
        self.entries: list[OrderIntent] = []
        self.exits: list[ExitIntent] = []
        self.hold: asyncio.Event | None = None
        self.fail: Exception | None = None

    async def _wait(self) -> None:
        if self.hold is not None:
            await self.hold.wait()
        if self.fail is not None:
            raise self.fail

    async def submit_entry_with_bracket(self, intent: OrderIntent) -> EntryResult:
        self.entries.append(intent)
        await self._wait()
        return EntryResult("no_fill", client_order_id(intent.decision_id, "entry"))

    async def submit_exit(self, intent: ExitIntent) -> ExitResult:
        self.exits.append(intent)
        await self._wait()
        return ExitResult("closed", reason=intent.reason)

    async def emergency_exit(self, product_id: str) -> ExitResult:
        raise AssertionError("the engine never asks for an emergency exit")

    async def cancel(self, product_id: str) -> Any:
        raise AssertionError("the engine never cancels")


class Context:
    """Quote, fees, product facts and balances of the book: the same numbers the backtest uses (100 EUR)."""

    def __init__(self) -> None:
        self.quotes: dict[str, MarketQuote | None] = {}
        self.equity_value, self.free_value = D("100"), D("100")
        self.marks: dict[str, Decimal] = {}
        self._registry = registry()

    def quote(self, product_id: str) -> MarketQuote | None:
        return self.quotes.get(product_id)

    def fees(self) -> FeeRates:
        return LABEL.fees

    def meta(self, product_id: str) -> ProductMeta:
        return self._registry.meta(product_id)

    def equity(self, marks: Any) -> Decimal:
        self.marks = dict(marks)
        return self.equity_value

    def free_quote(self) -> Decimal:
        return self.free_value


class Positions:
    def __init__(self) -> None:
        self.views: dict[str, PositionView] = {}
        self.fail: dict[str, Exception] = {}

    def view(self, product_id: str) -> PositionView:
        if product_id in self.fail:
            raise self.fail[product_id]
        return self.views.get(product_id, PositionView(book=BOOK, product_id=product_id))


class Sink:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.days: list[Any] = []
        self.fail: Exception | None = None

    async def append(self, record: Any, day: Any) -> None:
        if self.fail is not None:
            raise self.fail
        self.records.append(json.loads(json.dumps(record)))  # what a JSONL reader would get
        self.days.append(day)


class Gate:
    def __init__(self, reason: str | None = None) -> None:
        self.reason = reason

    def entry_blocked(self, book: str) -> str | None:
        return self.reason


class Remote:
    def __init__(self, answers: DecisionAnswers | None = None, exc: Exception | None = None) -> None:
        self.answers, self.exc, self.calls = answers, exc, []

    async def decide(self, state: str, questions: Any) -> DecideResult:
        self.calls.append((state, questions))
        if self.exc is not None:
            raise self.exc
        assert self.answers is not None
        return DecideResult(self.answers, "remote@1", {"model": 41.2, "total": 44.0}, 44.0, 412)


class Sentiment:
    def __init__(self, fail: Exception | None = None) -> None:
        self.calls: list[tuple[str, datetime]] = []
        self.fail = fail
        self._agg = SentimentAggregator(AggregationConfig())

    def snapshot(self, product_id: str, as_of: datetime) -> Any:
        self.calls.append((product_id, as_of))
        if self.fail is not None:
            raise self.fail
        return self._agg.snapshot(as_of, product_id.partition("-")[0], None)


# -------------------------------------------------------------------------------------------------- world

FRAMES: dict[str, pd.DataFrame] = {}
_WARM: dict[tuple[str, int], IndicatorEngine] = {}


def frame(product: str) -> pd.DataFrame:
    if product not in FRAMES:
        seed = {"BTC-EUR": 51, "SOL-EUR": 52}[product]
        FRAMES[product] = synthetic_bars(days=150, seed=seed, zero_volume_day=None, constant_days=None)
    return FRAMES[product]


def warmed(product: str, upto: int) -> IndicatorEngine:
    """A deep copy of an IndicatorEngine that has consumed ``frame(product)[:upto]`` (built once per process)."""
    if (product, upto) not in _WARM:
        engine = IndicatorEngine(product)
        engine.warm_up(frame(product).iloc[:upto])
        _WARM[product, upto] = engine
    return copy.deepcopy(_WARM[product, upto])


def caught_up(product: str, upto: int) -> IndicatorEngine:
    """A copy of the WARM_A engine that has also consumed the bars up to ``upto`` (exclusive)."""
    engine = warmed(product, WARM_A)
    for ts, row in frame(product).iloc[WARM_A:upto].iterrows():
        engine.update(
            Bar(
                ts.to_pydatetime(),
                float(row.open),
                float(row.high),
                float(row.low),
                float(row.close),
                float(row.volume),
            )
        )
    return engine


def policy_with(**kw: Any) -> PolicyConfig:
    return PolicyConfig(**{**POLICY.model_dump(), **kw})


def quote_around(price: float, spread_bps: float = 3.0) -> MarketQuote:
    """The quote the backtest synthesises around a reference price."""
    return MarketQuote(D(repr(price * (1 - spread_bps / 2 / 1e4))), D(repr(price * (1 + spread_bps / 2 / 1e4))))


@dataclass
class World:
    engine: DecisionEngine
    gateway: Gateway
    ctx: Context
    positions: Positions
    sink: Sink
    clock: Clock
    inner: StubDecider | FeatureStub
    baseline: StubBaseline | None
    remote: Remote | None = None
    reports: list[ExecutionReport] = field(default_factory=list)
    cursor: dict[str, int] = field(default_factory=dict)

    def event(self, product: str, i: int, *, lag: float = 120.0, source: BarSource = "rest") -> BarClosed:
        row = frame(product).iloc[i]
        ts = frame(product).index[i].to_pydatetime()
        bar = Bar(ts, float(row.open), float(row.high), float(row.low), float(row.close), float(row.volume))
        return BarClosed(product, bar, source, ts + timedelta(seconds=60 + lag))

    async def feed(self, product: str, i: int, **kw: Any) -> list[Processed]:
        ev = self.event(product, i, **kw)
        self.clock.now = ev.published_at
        self.ctx.quotes.setdefault(product, quote_around(float(frame(product).close.iloc[i])))
        out = await self.engine.on_bar(ev)
        if self.gateway.hold is None or self.gateway.hold.is_set():
            await self.engine.drain()
        return out

    async def next(self, product: str, **kw: Any) -> Processed:
        i = self.cursor[product]
        self.cursor[product] = i + 1
        (p,) = await self.feed(product, i, **kw)
        return p


def make_world(
    *,
    inner: StubDecider | FeatureStub | None = None,
    products: tuple[str, ...] = ("BTC-EUR", "SOL-EUR"),
    upto: int | None = WARM_A,
    backend: str = "baseline",
    fallback: bool = True,
    remote: Remote | None = None,
    baseline: bool = True,
    gate: Gate | None = None,
    policy: PolicyConfig = POLICY,
    cooldown_s: float = 0.0,
    sentiment: Sentiment | None = None,
    decision_max_lag_s: float = 240.0,
    log_to: DecisionLog | None = None,
) -> World:
    inner = inner or StubDecider()
    clock, gateway, ctx, positions, sink = Clock(), Gateway(), Context(), Positions(), Sink()
    rm = RiskManager(BOOK, RiskConfig(), LABEL.cfg, FallbackFees(), EntryTimeout(), gate, LABEL.costs)
    stub = StubBaseline(inner) if baseline else None
    counter = iter(range(1, 10**6))
    reports: list[ExecutionReport] = []
    engine = DecisionEngine(
        products=products, label=LABEL, policy=policy, questions=QUESTIONS, risk=rm, gateway=gateway,
        positions=positions, context=ctx,
        decision=DecisionConfig(backend=backend, fallback_to_baseline=fallback),  # type: ignore[arg-type]
        bars=BarsConfig(decision_max_lag_s=decision_max_lag_s), sink=log_to or sink, baseline=stub, remote=remote,
        sentiment=sentiment, clock=clock, new_id=lambda at: f"id-{next(counter):05d}", cooldown_s=cooldown_s,
        on_execution=reports.append,
    )  # fmt: skip
    world = World(engine, gateway, ctx, positions, sink, clock, inner, stub, remote, reports)
    if upto is not None:
        for p in products:
            engine.adopt(
                p, warmed(p, upto), frame(p).index[upto - 1].to_pydatetime(), float(frame(p).close.iloc[upto - 1])
            )
            world.cursor[p] = upto
    return world


def long_view(product: str, i: int, hours: float = 3.0) -> PositionView:
    """A LONG opened ``hours`` before bar ``i`` with a bracket (what the ledger holds after an entry)."""
    close = float(frame(product).close.iloc[i])
    ts = frame(product).index[i].to_pydatetime() + timedelta(minutes=1)
    return PositionView(
        book=BOOK, product_id=product, side="LONG", base_qty=D("0.001"), avg_entry=D(repr(close)),
        opened_at=ts - timedelta(hours=hours), entry_decision_id="d0", bracket_order_id="br-1",
        tp=D(repr(close * 1.05)), sl=D(repr(close * 0.97)), k_tp=3.0, k_sl=1.5, atr_at_entry=D(repr(close * 0.01)),
    )  # fmt: skip


# =========================================================================== parity with the backtest


@dataclass
class Parity:
    rows: int = 0
    buys: int = 0
    intents: int = 0
    skips: dict[str, int] = field(default_factory=dict)
    vetoes: int = 0
    below: int = 0
    mismatches: list[str] = field(default_factory=list)


async def run_parity(inner: StubDecider | FeatureStub, policy: PolicyConfig, n: int) -> Parity:
    """Feed ``n`` bars of BTC and SOL to the engine and compare each row with the batch path of the backtest."""
    btc = build_market("BTC-EUR", frame("BTC-EUR"), LABEL)
    markets = {"BTC-EUR": btc, "SOL-EUR": build_market("SOL-EUR", frame("SOL-EUR"), LABEL, btc.ind)}
    world = make_world(inner=inner, policy=policy)
    start = WARM_A
    rows = np.arange(start, start + n)
    F = LABEL.cfg.entry_first_bar
    batch_rm = RiskManager(BOOK, RiskConfig(), LABEL.cfg, FallbackFees(), EntryTimeout(), None, LABEL.costs)
    ref_policy = DecisionPolicy(policy)
    ref: dict[str, dict[str, Any]] = {}
    for p, pm in markets.items():
        x = flat_matrix(pm.ind, rows, p, LABEL, pm.btc_ind, None if pm.btc_rows is None else pm.btc_rows[rows])
        a = inner.answers_batch(x)
        pct = x[:, REGIME_COL]
        ref[p] = {"x": x, "a": a, "buy": ref_policy.buy_mask(a.p_action, pct), "reg": regimes_from_pctile(pct)}

    out = Parity()
    for k, i in enumerate(rows):
        for p in markets:  # the quote of the backtest: the reference price of bar t + F - 1
            world.ctx.quotes[p] = quote_around(float(markets[p].bars.c[i + F - 1]))
        world.clock.now = world.event("BTC-EUR", int(i)).published_at
        events = [world.event(p, int(i)) for p in markets]
        done = {r.product_id: r for r in await world.engine.on_bars(events)}
        await world.engine.drain()
        for p, pm in markets.items():
            r, got = ref[p], done[p]
            out.rows += 1
            want_buy = bool(r["buy"][k])
            pct = r["x"][k, REGIME_COL]
            if got.record["policy"]["regime_pctile"] != (None if np.isnan(pct) else int(pct)):
                out.mismatches.append(f"{p}[{i}] regime token")
            if (got.decision is not None and got.decision.action == "BUY") != want_buy:
                out.mismatches.append(f"{p}[{i}] action {got.decision} vs buy={want_buy}")
                continue
            if got.record["answers"]["edge"]["noul"] != float(r["a"].p_edge[k]):
                out.mismatches.append(f"{p}[{i}] p_edge")
            if got.record["answers"]["action"]["probabilities"] != dict(
                zip(ACTIONS, map(float, r["a"].p_action[k]), strict=True)
            ):
                out.mismatches.append(f"{p}[{i}] action probabilities")
            if not want_buy:
                out.below += 1
                out.vetoes += got.record["policy"]["reason"] == "regime_veto"
                continue
            out.buys += 1
            assert got.decision is not None and got.decision.regime == int(r["reg"][k])
            batch_decision = Decision("BUY", "thresholds_met", p_edge=float(r["a"].p_edge[k]), regime=int(r["reg"][k]))
            want = batch_rm.plan_entry(
                decision=batch_decision, decision_id="x", intent_id="x", now=world.clock.now, product_id=p,
                quote=world.ctx.quotes[p], atr=D(repr(float(pm.atr[i]))), fees=LABEL.fees, meta=world.ctx.meta(p),
                equity=D("100"), free_quote=D("100"), p_be_buffer=policy.p_be_buffer,
                position=PositionView(book=BOOK, product_id=p),
            )  # fmt: skip
            if isinstance(want, OrderIntent):
                ids: Any = {
                    "intent_id": True,
                    "decision_id": True,
                    "bracket": {"atr"},
                }  # atr: batch vs incremental, 1e-13
                if not isinstance(got.intent, OrderIntent) or got.intent.model_dump(exclude=ids) != want.model_dump(
                    exclude=ids
                ):
                    out.mismatches.append(f"{p}[{i}] intent")
                elif abs(float(got.intent.bracket.atr) / float(want.bracket.atr) - 1.0) > 1e-9:
                    out.mismatches.append(f"{p}[{i}] atr")
                out.intents += 1
            elif got.skip_reason != want.reason or got.skip_detail != want.detail or got.intent is not None:
                out.mismatches.append(f"{p}[{i}] skip {got.skip_reason!r} vs {want.reason!r}")
            else:
                out.skips[want.reason] = out.skips.get(want.reason, 0) + 1
    return out


async def test_engine_matches_the_backtest_chain_with_the_always_buy_stub() -> None:
    """Same bars, same decider object: same decision and same intent on every row (the StubDecider of the backtest)."""
    res = await run_parity(StubDecider(), POLICY, 3 * 1440)
    assert not res.mismatches, res.mismatches[:5]
    assert res.intents >= 100 and res.vetoes >= 100, (
        res
    )  # entries, and rows vetoed by the regime token (BTC from day 140)


async def test_engine_matches_the_backtest_chain_with_a_feature_dependent_decider() -> None:
    """A decider that reads the state (so a wrong feature changes a decision), thresholds and an EV buffer."""
    policy = policy_with(tau_buy=0.55, c_min=0.02, p_be_buffer=0.02)
    res = await run_parity(FeatureStub(), policy, 3 * 1440)
    assert not res.mismatches, res.mismatches[:5]
    assert res.intents >= 300 and res.below >= 2000, res
    assert sum(res.skips.values()) >= 5, res  # the RM refused some BUYs: the engine reports the same reason and detail


async def test_extreme_model_regime_head_does_not_veto_and_is_only_logged() -> None:
    w = make_world(inner=ExtremeRegimeStub())
    p = await w.next("BTC-EUR")
    assert p.decision is not None and p.decision.action == "BUY" and isinstance(p.intent, OrderIntent)
    pol = p.record["policy"]
    assert pol["model_regime"] == 4 and pol["regime"] in (1, 2, 3) and pol["regime_rule"] == REGIME_RULE
    assert pol["regime"] == regime_from_pctile(pol["regime_pctile"])


# ========================================================================= the tripwires of the bars decision


async def test_late_bar_updates_the_engine_without_a_decision() -> None:
    w = make_world()
    i = w.cursor["BTC-EUR"]
    assert await w.feed("BTC-EUR", i, lag=241.0) == []  # published 241 s after t: beyond decision_max_lag_s (240)
    assert w.sink.records == [] and w.gateway.entries == []
    assert w.engine.stats.skipped_late == 1 and w.engine.stats.decisions == 0
    snap = w.engine.indicators["BTC-EUR"].snapshot()
    assert snap.ts == frame("BTC-EUR").index[i].to_pydatetime()  # the late bar IS in the indicators
    (p,) = await w.feed("BTC-EUR", i + 1)  # and the next one continues without a gap
    assert p.decision is not None and w.engine.pending("BTC-EUR") is None
    assert w.engine.stats.decisions == 1 and len(w.sink.records) == 1


async def test_a_bar_exactly_at_the_limit_decides() -> None:
    w = make_world()
    (p,) = await w.feed("BTC-EUR", w.cursor["BTC-EUR"], lag=240.0)
    assert p.decision is not None and w.engine.stats.skipped_late == 0


async def test_engine_lag_counts_too_not_only_the_publication_lag() -> None:
    """The queue was backed up: published on time, computed late. The state would not be point-in-time."""
    w = make_world()
    i = w.cursor["BTC-EUR"]
    ev = w.event("BTC-EUR", i, lag=120.0)
    w.clock.now = ev.published_at + timedelta(seconds=200)
    assert await w.engine.on_bar(ev) == [] and w.engine.stats.skipped_late == 1


async def test_ts_bar_close_is_bar_ts_plus_one_minute() -> None:
    w = make_world()
    i = w.cursor["BTC-EUR"] + 13 * 60 - 1  # a bar that starts one minute before the hour
    for j in range(w.cursor["BTC-EUR"], i):
        await w.feed("BTC-EUR", j, lag=300.0)  # late: indicators only, no decision
    bar_ts = frame("BTC-EUR").index[i].to_pydatetime()
    (p,) = await w.feed("BTC-EUR", i)
    t = bar_ts + timedelta(minutes=1)
    assert p.ts_bar_close == t and w.sink.records[-1]["ts_bar_close"] == t.strftime("%Y-%m-%dT%H:%M:%SZ")
    state = DecisionState.model_validate_json(w.sink.records[-1]["state"])
    assert (state.clock.hour_utc, state.clock.dow) == (t.hour, t.weekday())
    assert t.minute == 0 and bar_ts.hour != t.hour  # the logical instant, not the bar's start, drives the clock


async def test_sentiment_is_read_as_of_ts_bar_close() -> None:
    sent = Sentiment()
    w = make_world(sentiment=sent)
    i = w.cursor["SOL-EUR"]
    (p,) = await w.feed("SOL-EUR", i)
    t = frame("SOL-EUR").index[i].to_pydatetime() + timedelta(minutes=1)
    assert sent.calls == [("SOL-EUR", t)] and p.ts_bar_close == t
    state = DecisionState.model_validate_json(p.record["state"])
    assert state.sentiment is not None and state.sentiment.asset is not None  # the snapshot reached the state


async def test_a_sentiment_failure_is_a_missing_sentiment_not_an_error() -> None:
    w = make_world(sentiment=Sentiment(fail=KeyError("no asset")))
    (p,) = await w.feed("SOL-EUR", w.cursor["SOL-EUR"])
    assert p.decision is not None and DecisionState.model_validate_json(p.record["state"]).sentiment is None
    assert w.engine.stats.errors == 0


# ================================================================================= the decider and its fallback


def remote_answers() -> DecisionAnswers:
    arrays = StubDecider(buy=0.9).answers_batch(np.zeros((1, len(FLAT_COLUMNS))))
    return BaselineDecider._answers(arrays, 0)


async def test_remote_decider_answers_and_the_record_says_so() -> None:
    remote = Remote(remote_answers())
    w = make_world(backend="openjev", remote=remote)
    p = await w.next("BTC-EUR")
    rec = p.record
    assert (rec["backend"], rec["model"], rec["fallback"], rec["decider_error"]) == ("openjev", "remote@1", False, None)
    assert rec["latency_ms"]["http_total"] == 44.0 and rec["latency_ms"]["server_model"] == 41.2
    assert len(remote.calls) == 1 and remote.calls[0][1] is QUESTIONS and w.baseline is not None
    assert w.baseline.calls == 0 and isinstance(p.intent, OrderIntent)


@pytest.mark.parametrize("failure", [DeciderTimeout("slow"), RuntimeError("bug in the client")])
async def test_failing_remote_falls_back_to_the_baseline(failure: Exception) -> None:
    w = make_world(backend="chronos-decider", remote=Remote(exc=failure))
    p = await w.next("BTC-EUR")
    assert p.record["backend"] == "baseline" and p.record["fallback"] is True
    assert type(failure).__name__ in p.record["decider_error"] and p.record["model"] == POLICY.model
    assert p.decision is not None and p.decision.action == "BUY" and w.engine.stats.fallbacks == 1


async def test_absent_remote_falls_back_to_the_baseline() -> None:
    w = make_world(backend="openjev", remote=None)
    p = await w.next("BTC-EUR")
    assert p.record["backend"] == "baseline" and p.record["fallback"] and "not configured" in p.record["decider_error"]


async def test_no_fallback_means_hold_decider_unavailable() -> None:
    w = make_world(backend="openjev", remote=Remote(exc=DeciderTimeout("slow")), fallback=False)
    p = await w.next("BTC-EUR")
    assert p.decision is not None and (p.decision.action, p.decision.reason) == ("HOLD", "decider_unavailable")
    assert p.record["answers"] is None and p.record["policy"]["model_regime"] is None
    assert w.gateway.entries == [] and w.baseline is not None and w.baseline.calls == 0
    assert w.engine.stats.unavailable == 1


async def test_baseline_failure_is_hold_decider_unavailable() -> None:
    w = make_world()
    assert w.baseline is not None
    w.baseline.fail = RuntimeError("model file corrupt")
    p = await w.next("BTC-EUR")
    assert p.decision is not None and p.decision.reason == "decider_unavailable" and w.gateway.entries == []
    assert "model file corrupt" in p.record["decider_error"]


def test_engine_needs_a_decider_and_a_policy_for_its_label() -> None:
    with pytest.raises(ValueError, match="no decider"):
        make_world(baseline=False, upto=None)
    with pytest.raises(ValueError, match="label_config"):
        make_world(policy=policy_with(label_config_sha256="0" * 64), upto=None)


# ========================================================================================== HOLD and Skip


async def test_hold_below_threshold_is_recorded_with_its_reason() -> None:
    w = make_world(inner=StubDecider(buy=0.3))
    p = await w.next("BTC-EUR")
    assert p.decision is not None and (p.decision.action, p.decision.reason) == ("HOLD", "below_threshold")
    pol = p.record["policy"]
    assert (pol["final"], pol["reason"]) == ("HOLD", "below_threshold") and p.record["risk"]["outcome"] == "none"
    assert p.record["order_intent_id"] is None and w.gateway.entries == [] and len(w.sink.records) == 1


async def test_regime_veto_from_the_token_and_regime_unavailable_without_history() -> None:
    w = make_world(upto=WARM_B)  # BTC is in regime 0 here (token < 5)
    p = await w.next("BTC-EUR")
    assert p.record["policy"]["reason"] == "regime_veto" and p.record["policy"]["regime"] == 0
    assert w.gateway.entries == [] and p.record["risk"]["outcome"] == "none"

    cold = make_world(upto=None)  # no warm-up: the 365-day percentile does not exist yet
    q = await cold.feed("BTC-EUR", 0)
    assert q[0].record["policy"]["reason"] == "regime_unavailable" and q[0].record["policy"]["regime_pctile"] is None


async def test_a_risk_skip_is_recorded_with_reason_and_detail() -> None:
    w = make_world()
    w.ctx.free_value = D("0.50")  # not enough free quote for the minimum order
    p = await w.next("BTC-EUR")
    assert p.decision is not None and p.decision.action == "BUY" and p.intent is None
    assert p.skip_reason in {"below_min_size", "insufficient_balance"}
    assert p.record["risk"]["outcome"] == "skip" and p.record["risk"]["reason"] == p.skip_reason
    assert p.record["order_intent_id"] is None and w.gateway.entries == []


async def test_no_quote_and_no_atr_are_skips_not_errors() -> None:
    w = make_world()
    w.ctx.quotes["BTC-EUR"] = None
    p = await w.next("BTC-EUR")
    assert p.skip_reason == "no_quote" and w.engine.stats.errors == 0


class NoAtr(IndicatorEngine):
    """Indicators whose ATR is not available (NaN) while everything else is."""

    def update(self, bar: Bar) -> Any:
        return replace(super().update(bar), atr_14_6h=float("nan"))


async def test_a_buy_without_an_atr_is_a_skip_like_the_backtest_excludes_it() -> None:
    w = make_world()
    ie = warmed("BTC-EUR", WARM_A)
    ie.__class__ = NoAtr
    w.engine.adopt("BTC-EUR", ie, frame("BTC-EUR").index[WARM_A - 1].to_pydatetime(), 1.0)
    p = await w.next("BTC-EUR")
    assert p.decision is not None and p.decision.action == "BUY" and p.intent is None
    assert p.skip_reason == "no_quote" and "unavailable" in p.skip_detail and w.gateway.entries == []


async def test_the_kill_switch_blocks_entries_but_never_exits() -> None:
    w = make_world(gate=Gate("daily_loss"))
    p = await w.next("BTC-EUR")
    assert p.skip_reason == "kill_switch" and p.skip_detail == "daily_loss" and w.gateway.entries == []

    sell = make_world(inner=StubDecider(sell_after_h=2), gate=Gate("daily_loss"))
    i = sell.cursor["BTC-EUR"]
    sell.positions.views["BTC-EUR"] = long_view("BTC-EUR", i)
    q = await sell.next("BTC-EUR")
    assert q.decision is not None and q.decision.action == "SELL" and isinstance(q.intent, ExitIntent)
    assert len(sell.gateway.exits) == 1 and sell.gateway.entries == []


# ====================================================================== one position per product, cooldown


async def test_one_entry_at_a_time_per_product() -> None:
    w = make_world()
    w.gateway.hold = asyncio.Event()
    first = await w.next("BTC-EUR")
    assert isinstance(first.intent, OrderIntent) and w.engine.pending("BTC-EUR") == "entry"
    second = await w.next("BTC-EUR")
    assert second.intent is None and second.skip_reason == "entry_pending"
    catch_up = await w.next("SOL-EUR")  # the BTC bar of this minute was consumed two bars ago: not an entry (stale)
    assert catch_up.intent is None and catch_up.record["btc_context"] == "stale"
    other = await w.next("SOL-EUR")  # another product is independent (its BTC bar is the one just consumed)
    assert isinstance(other.intent, OrderIntent) and other.record["btc_context"] == "aligned"
    assert len(w.gateway.entries) == 2 and len(w.sink.records) == 4
    w.gateway.hold.set()
    await w.engine.drain()
    assert w.engine.pending("BTC-EUR") is None and [r.kind for r in w.reports] == ["entry", "entry"]
    third = await w.next("BTC-EUR")
    assert isinstance(third.intent, OrderIntent)  # the first one ended without a fill: free to decide again


async def test_cooldown_after_a_finished_sequence() -> None:
    w = make_world(cooldown_s=150.0)
    assert isinstance((await w.next("BTC-EUR")).intent, OrderIntent)  # decided at t0 + 120 s, ended at once
    blocked = await w.next("BTC-EUR")  # next bar is computed 60 s later: inside the 150 s cooldown
    assert blocked.skip_reason == "cooldown" and "no new entry before" in blocked.skip_detail
    assert (await w.next("BTC-EUR")).skip_reason == "cooldown"  # 120 s after the end
    assert isinstance((await w.next("BTC-EUR")).intent, OrderIntent)  # 180 s after: allowed again


async def test_model_sell_on_a_long_exits_through_the_gateway_once() -> None:
    w = make_world(inner=StubDecider(sell_after_h=2))
    i = w.cursor["BTC-EUR"]
    w.positions.views["BTC-EUR"] = long_view("BTC-EUR", i)
    w.gateway.hold = asyncio.Event()
    p = await w.next("BTC-EUR")
    assert p.decision is not None and p.decision.action == "SELL" and isinstance(p.intent, ExitIntent)
    assert (
        p.intent.base_size == D("0.001")
        and p.intent.cancel_bracket_order_id == "br-1"
        and p.intent.reason == "thresholds_met"
    )
    assert w.engine.pending("BTC-EUR") == "exit"
    again = await w.next("BTC-EUR")
    assert again.skip_reason == "exit_pending" and len(w.gateway.exits) == 1
    w.gateway.hold.set()
    await w.engine.drain()
    assert w.reports[-1].kind == "exit" and w.reports[-1].error is None


async def test_a_long_position_state_comes_from_the_ledger_view() -> None:
    w = make_world(inner=StubDecider(buy=0.3))
    i = w.cursor["BTC-EUR"]
    w.positions.views["BTC-EUR"] = long_view("BTC-EUR", i, hours=5.0)
    p = await w.next("BTC-EUR")
    state = DecisionState.model_validate_json(p.record["state"])
    assert state.position.side == "LONG" and state.position.hours_held == 5
    assert p.decision is not None and p.decision.action == "HOLD" and p.decision.masked_probs.keys() == {"SELL", "HOLD"}


# ============================================================================================ failures


async def test_a_failing_product_does_not_stop_the_others_or_the_next_bar() -> None:
    w = make_world()
    w.positions.fail["SOL-EUR"] = RuntimeError("ledger offline")
    i = w.cursor["BTC-EUR"]
    w.cursor["BTC-EUR"] = w.cursor["SOL-EUR"] = i
    for p in ("BTC-EUR", "SOL-EUR"):
        w.ctx.quotes[p] = quote_around(float(frame(p).close.iloc[i]))
    w.clock.now = w.event("BTC-EUR", i).published_at
    done = {p.product_id: p for p in await w.engine.on_bars([w.event("BTC-EUR", i), w.event("SOL-EUR", i)])}
    await w.engine.drain()
    assert isinstance(done["BTC-EUR"].intent, OrderIntent)
    bad = done["SOL-EUR"]
    assert bad.skip_reason == "engine_error" and bad.decision is None and "ledger offline" in bad.skip_detail
    assert bad.record["error"]["stage"] == "state" and bad.record["policy"]["reason"] == "engine_error"
    assert w.engine.stats.errors == 1 and len(w.sink.records) == 2
    del w.positions.fail["SOL-EUR"]  # the next bar of the same product works again (BTC of the minute arrives too)
    w.cursor["BTC-EUR"] = w.cursor["SOL-EUR"] = i + 2
    w.clock.now = w.event("SOL-EUR", i + 1).published_at
    again = await w.engine.on_bars([w.event("BTC-EUR", i + 1), w.event("SOL-EUR", i + 1)])
    await w.engine.drain()
    ok = next(p for p in again if p.product_id == "SOL-EUR")
    assert ok.decision is not None and ok.decision.action == "BUY"


async def test_an_error_in_the_risk_manager_is_isolated_and_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    w = make_world()

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise PolicyContractError("a BUY with no regime")

    monkeypatch.setattr(w.engine._risk, "plan_entry", boom)
    p = await w.next("BTC-EUR")
    assert p.skip_reason == "engine_error" and p.record["error"]["type"] == "PolicyContractError"
    assert p.record["error"]["stage"] == "conclude" and w.gateway.entries == []
    monkeypatch.undo()
    assert isinstance((await w.next("BTC-EUR")).intent, OrderIntent)


async def test_a_gateway_failure_is_reported_and_releases_the_product() -> None:
    w = make_world()
    w.gateway.fail = RuntimeError("exchange down")
    p = await w.next("BTC-EUR")
    assert isinstance(p.intent, OrderIntent) and w.engine.pending("BTC-EUR") is None
    assert w.reports[-1].result is None and "exchange down" in (w.reports[-1].error or "")
    assert w.engine.stats.errors == 1


async def test_no_entry_without_its_record_but_exits_still_go() -> None:
    w = make_world()
    w.sink.fail = OSError("disk full")
    p = await w.next("BTC-EUR")
    assert p.intent is None and p.skip_reason == "audit_unavailable" and w.gateway.entries == []
    assert w.engine.stats.log_errors == 1 and p.record["order_intent_id"] is None

    sell = make_world(inner=StubDecider(sell_after_h=2))
    sell.sink.fail = OSError("disk full")
    sell.positions.views["BTC-EUR"] = long_view("BTC-EUR", sell.cursor["BTC-EUR"])
    q = await sell.next("BTC-EUR")
    assert isinstance(q.intent, ExitIntent) and len(sell.gateway.exits) == 1  # the position must be able to leave


async def test_a_gap_in_the_bars_breaks_only_that_product_until_it_is_warmed_up_again() -> None:
    w = make_world()
    i = w.cursor["BTC-EUR"]
    await w.feed("BTC-EUR", i)
    (bad,) = await w.feed("BTC-EUR", i + 2)  # minute i + 1 never arrived
    assert bad.skip_reason == "engine_error" and "BarGapError" in bad.skip_detail
    (still,) = await w.feed("BTC-EUR", i + 3)
    assert still.skip_reason == "engine_error"  # no decision on indicators that miss a bar
    (sol,) = await w.feed("SOL-EUR", w.cursor["SOL-EUR"])
    assert sol.decision is not None  # the other product is unaffected
    w.engine.adopt("BTC-EUR", caught_up("BTC-EUR", i + 4), frame("BTC-EUR").index[i + 3].to_pydatetime(), 1.0)
    (back,) = await w.feed("BTC-EUR", i + 4)
    assert back.decision is not None


async def test_a_duplicate_or_old_bar_is_ignored() -> None:
    w = make_world()
    i = w.cursor["BTC-EUR"]
    await w.feed("BTC-EUR", i)
    assert await w.feed("BTC-EUR", i) == [] and await w.feed("BTC-EUR", i - 5) == []
    assert w.engine.stats.out_of_order == 2 and len(w.sink.records) == 1
    (p,) = await w.feed("BTC-EUR", i + 1)
    assert p.decision is not None


async def test_btc_context_uses_the_btc_bar_of_the_same_minute_only() -> None:
    w = make_world()
    i = w.cursor["SOL-EUR"]
    w.clock.now = w.event("SOL-EUR", i).published_at
    for p in ("BTC-EUR", "SOL-EUR"):
        w.ctx.quotes[p] = quote_around(float(frame(p).close.iloc[i]))
    both = {p.product_id: p for p in await w.engine.on_bars([w.event("BTC-EUR", i), w.event("SOL-EUR", i)])}
    assert DecisionState.model_validate_json(both["SOL-EUR"].record["state"]).btc_context is not None
    assert DecisionState.model_validate_json(both["BTC-EUR"].record["state"]).btc_context is None
    w.clock.now = w.event("SOL-EUR", i + 1).published_at
    (alone,) = await w.engine.on_bar(w.event("SOL-EUR", i + 1))  # BTC's bar for this minute has not arrived
    assert DecisionState.model_validate_json(alone.record["state"]).btc_context is None


async def test_flat_and_unproven_bars_decide_and_the_source_is_recorded() -> None:
    w = make_world()
    p = await w.next("BTC-EUR", source="flat")
    q = await w.next("BTC-EUR", source="flat_unproven", lag=180.0)
    assert (p.record["bar_source"], q.record["bar_source"]) == ("flat", "flat_unproven")
    assert q.record["bar_lag_s"] == 180.0 and q.decision is not None


# ===================================================================================== the DecisionRecord


async def test_the_record_is_complete_and_consistent() -> None:
    w = make_world(policy=policy_with(p_be_buffer=0.04))
    i = w.cursor["BTC-EUR"]
    p = await w.next("BTC-EUR")
    rec = p.record
    assert set(rec) >= {
        "decision_id", "ts_bar_close", "decided_at", "bar_source", "bar_lag_s", "product", "state_sha256", "state",
        "questions_version", "backend", "model", "answers", "policy", "latency_ms", "order_intent_id", "book", "risk",
    }  # fmt: skip
    assert rec["decision_id"] == p.decision_id == "id-00001" and rec["product"] == "BTC-EUR" and rec["book"] == BOOK
    assert rec["state_sha256"] == hashlib.sha256(rec["state"].encode()).hexdigest()
    assert rec["questions_version"] == QUESTIONS.version_tag and rec["backend"] == "baseline"
    assert rec["decided_at"].endswith("Z") and rec["bar_source"] == "rest" and rec["bar_lag_s"] == 120.0
    assert rec["decided_at"] == w.clock.now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    assert rec["ts_bar_close"] == (frame("BTC-EUR").index[i] + pd.Timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert set(rec["answers"]) == {"action", "edge", "vol_regime"}
    assert rec["answers"]["action"]["confidence"] > 0  # recomputed by the engine, never the backend's 0.0
    pol = rec["policy"]
    assert set(pol) >= {"policy_version", "regime", "regime_pctile", "regime_rule", "model_regime", "masked_probs",
                        "final", "reason", "tau_buy", "p_be_buffer", "p_breakeven"}  # fmt: skip
    assert (pol["final"], pol["reason"], pol["p_be_buffer"], pol["policy_version"]) == (
        "BUY",
        "thresholds_met",
        0.04,
        POLICY.model,
    )
    state = DecisionState.model_validate_json(rec["state"])
    assert pol["regime_pctile"] == state.volatility.atr_1d_pctile_365d and pol["p_breakeven"] == state.costs.p_breakeven
    assert set(rec["latency_ms"]) == {"build", "inference", "http_total", "server_model"}
    assert isinstance(p.intent, OrderIntent) and rec["order_intent_id"] == p.intent.intent_id == "id-00002"
    assert rec["risk"] == {"outcome": "intent", "reason": None, "detail": ""} and p.intent.decision_id == "id-00001"
    assert p.intent.risk.p_be_buffer == 0.04 and p.intent.created_at == w.clock.now
    assert w.gateway.entries == [p.intent] and w.sink.days == [w.clock.now.date()]


async def test_records_go_to_daily_jsonl_files_append_only(tmp_path: Path) -> None:
    log = DecisionLog.under(tmp_path)
    w = make_world(log_to=log)
    i0 = WARM_A + 23 * 60 + 56  # the bar of 23:56 UTC is published at 23:59 (S = 120 s), the next one at 00:00
    for j in range(w.cursor["BTC-EUR"], i0):
        await w.feed("BTC-EUR", j, lag=999.0)  # indicators only
    await w.feed("BTC-EUR", i0)
    await w.feed("BTC-EUR", i0 + 1)
    await log.aclose()
    first_day = frame("BTC-EUR").index[WARM_A].date()
    files = sorted((tmp_path / "decisions").glob("*.jsonl"))
    assert [f.name for f in files] == [f"{first_day}.jsonl", f"{first_day + timedelta(days=1)}.jsonl"]  # 00:00 UTC
    rec = json.loads(files[0].read_text(encoding="utf-8").splitlines()[0])
    assert (
        rec["product"] == "BTC-EUR" and rec["policy"]["final"] == "BUY" and rec["decided_at"].startswith(str(first_day))
    )

    again = DecisionLog.under(tmp_path)  # a restart appends: nothing is ever truncated
    await again.append({"decision_id": "later"}, first_day)
    await again.aclose()
    lines = files[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and json.loads(lines[1]) == {"decision_id": "later"}


async def test_the_log_refuses_a_record_that_is_not_json(tmp_path: Path) -> None:
    log = DecisionLog.under(tmp_path)
    with pytest.raises(ValueError):
        await log.append({"x": float("nan")}, datetime(2024, 6, 1, tzinfo=UTC).date())
    assert not (tmp_path / "decisions").exists()  # a refused line never creates or damages a file
    await log.aclose()


def test_ulids_are_26_chars_and_sort_by_time() -> None:
    a = new_ulid(datetime(2024, 6, 1, 12, 0, tzinfo=UTC))
    b = new_ulid(datetime(2024, 6, 1, 12, 0, 1, tzinfo=UTC))
    assert len(a) == len(b) == 26 and set(a + b) <= set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")
    assert a[:10] < b[:10] and new_ulid(datetime(2024, 6, 1, tzinfo=UTC)) != new_ulid(datetime(2024, 6, 1, tzinfo=UTC))


# ================================================================================================= the loop


async def test_run_consumes_a_queue_and_survives_a_bad_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    w = make_world()
    q: asyncio.Queue[BarClosed] = asyncio.Queue()
    i = w.cursor["BTC-EUR"]
    w.clock.now = w.event("BTC-EUR", i).published_at
    for p in ("SOL-EUR", "BTC-EUR"):
        w.ctx.quotes[p] = quote_around(float(frame(p).close.iloc[i]))
    task = asyncio.create_task(w.engine.run(q))
    q.put_nowait(w.event("SOL-EUR", i))
    q.put_nowait(
        w.event("BTC-EUR", i)
    )  # same minute, SOL first on purpose: the BTC bar is ingested before any decision
    for _ in range(200):
        if len(w.sink.records) == 2:
            break
        await asyncio.sleep(0.01)
    assert {r["product"] for r in w.sink.records} == {"BTC-EUR", "SOL-EUR"}
    assert DecisionState.model_validate_json(
        next(r for r in w.sink.records if r["product"] == "SOL-EUR")["state"]
    ).btc_context

    original = w.engine.on_bars
    calls = {"n": 0}

    async def flaky(events: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("unexpected")
        return await original(events)

    monkeypatch.setattr(w.engine, "on_bars", flaky)
    q.put_nowait(w.event("BTC-EUR", i + 1))
    q.put_nowait(w.event("BTC-EUR", i + 2))
    for _ in range(200):
        if calls["n"] >= 2:
            break
        await asyncio.sleep(0.01)
    assert calls["n"] >= 1 and not task.done()  # the loop outlived the failure
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ==================================================== BTC context of an alt state (conflict P1f, decision Q3)


async def minute(w: World, i: int, *, with_btc: bool = True, btc_first: bool = True) -> dict[str, Processed]:
    """One minute ``i`` for both products. ``with_btc`` False: the BTC bar of the minute has not arrived (an alt alone).
    ``btc_first`` False: it arrives in a batch AFTER the alt (late)."""
    for p in ("BTC-EUR", "SOL-EUR"):
        w.ctx.quotes[p] = quote_around(float(frame(p).close.iloc[i]))
    w.clock.now = w.event("SOL-EUR", i).published_at
    batches = [[w.event("BTC-EUR", i), w.event("SOL-EUR", i)]] if with_btc and btc_first else []
    if not with_btc:
        batches = [[w.event("SOL-EUR", i)]]
    elif not btc_first:
        batches = [[w.event("SOL-EUR", i)], [w.event("BTC-EUR", i)]]
    done: dict[str, Processed] = {}
    for batch in batches:
        for p in await w.engine.on_bars(batch):
            done[p.product_id] = p
    await w.engine.drain()
    return done


async def test_alt_buy_blocked_when_btc_bar_of_the_minute_is_missing() -> None:
    w = make_world()
    i = w.cursor["SOL-EUR"]
    done = await minute(w, i, with_btc=False)
    sol = done["SOL-EUR"]
    assert sol.decision is not None and sol.decision.action == "HOLD" and sol.decision.reason == "btc_context_stale"
    assert sol.record["policy"]["final"] == "HOLD" and sol.record["policy"]["reason"] == "btc_context_stale"
    assert sol.record["btc_context"] == "stale" and sol.intent is None and sol.skip_reason is None
    assert w.gateway.entries == [] and w.engine.stats.reasons["btc_context_stale"] == 1
    assert json.loads(sol.record["state"])["btc_context"] is None  # the state lacks the BTC tokens


async def test_record_carries_btc_context() -> None:
    w = make_world()
    done = await minute(w, w.cursor["SOL-EUR"])
    assert done["SOL-EUR"].record["btc_context"] == "aligned"
    assert done["BTC-EUR"].record["btc_context"] is None  # BTC itself has no BTC context
    assert json.loads(done["SOL-EUR"].record["state"])["btc_context"] is not None
    assert json.loads(json.dumps(done["SOL-EUR"].record))["btc_context"] == "aligned"  # survives the JSONL


async def test_alt_buy_allowed_when_btc_aligned() -> None:
    w = make_world()
    done = await minute(w, w.cursor["SOL-EUR"])
    sol = done["SOL-EUR"]
    assert sol.decision is not None and sol.decision.action == "BUY" and isinstance(sol.intent, OrderIntent)
    assert len(w.gateway.entries) == 2  # BTC and SOL


async def test_stale_btc_never_blocks_sell() -> None:
    w = make_world(inner=StubDecider(sell_after_h=2))
    i = w.cursor["SOL-EUR"]
    w.positions.views["SOL-EUR"] = long_view("SOL-EUR", i)
    done = await minute(w, i, with_btc=False)
    sol = done["SOL-EUR"]
    assert sol.record["btc_context"] == "stale"
    assert sol.decision is not None and sol.decision.action == "SELL" and isinstance(sol.intent, ExitIntent)
    assert len(w.gateway.exits) == 1 and w.gateway.entries == []


async def test_btc_not_served_no_rule() -> None:
    w = make_world(products=("SOL-EUR",))
    p = await w.next("SOL-EUR")
    assert p.record["btc_context"] is None
    assert p.decision is not None and p.decision.action == "BUY" and isinstance(p.intent, OrderIntent)


async def test_same_bars_same_records() -> None:
    """Determinism: two engines, the same bars in the same order, the same states and decisions minute by minute. With
    the BTC bar one batch late the only difference in the state is the BTC context and the only decision that changes is
    BUY -> HOLD ``btc_context_stale``."""
    n = 6
    twin_a, twin_b, late = make_world(), make_world(), make_world()
    i0 = twin_a.cursor["SOL-EUR"]
    for k in range(n):
        await minute(twin_a, i0 + k)
        await minute(twin_b, i0 + k)
        await minute(late, i0 + k, btc_first=False)

    def sol(w: World) -> list[dict[str, Any]]:
        return [r for r in w.sink.records if r["product"] == "SOL-EUR"]

    assert [r["state_sha256"] for r in sol(twin_a)] == [r["state_sha256"] for r in sol(twin_b)]
    assert [r["policy"]["final"] for r in sol(twin_a)] == [r["policy"]["final"] for r in sol(twin_b)]
    assert len(sol(twin_a)) == len(sol(late)) == n
    changed = 0
    for ra, rl in zip(sol(twin_a), sol(late), strict=True):
        sa, sl = json.loads(ra["state"]), json.loads(rl["state"])
        assert sa.pop("btc_context") is not None and sl.pop("btc_context") is None
        assert sa == sl  # only the btc tokens differ
        assert rl["btc_context"] == "stale" and ra["btc_context"] == "aligned"
        if ra["policy"]["final"] != rl["policy"]["final"]:
            changed += 1
            assert (ra["policy"]["final"], rl["policy"]["final"]) == ("BUY", "HOLD")
            assert rl["policy"]["reason"] == "btc_context_stale"
    assert changed >= 1
    hold = make_world(inner=StubDecider(buy=0.1))  # a model that never buys: nothing changes with a late BTC bar
    hold_late = make_world(inner=StubDecider(buy=0.1))
    for k in range(3):
        await minute(hold, i0 + k)
        await minute(hold_late, i0 + k, btc_first=False)
    finals = [r["policy"]["final"] for r in hold.sink.records]
    assert finals == [r["policy"]["final"] for r in hold_late.sink.records] and "BUY" not in finals
