"""Replay of recorded market data through the runtime pipeline (spec sez. 5.1.7, 5.3 punto 9).

The pipeline under test is the real one: ``DecisionEngine`` -> ``DecisionPolicy`` -> ``RiskManager`` ->
``ShadowExecutionGateway`` -> ``BalanceGuard`` / ``VirtualAccount`` / dry ``PositionLedger``, fed by a ``BarService``
over the Parquet store and by a ``ReplayMarketStream`` over a recording made with the real ``WsRecorder``. Only the
data is synthetic (the 150-day series of the engine tests, one book per minute around the close of the bar).
"""

from __future__ import annotations

import ast
import builtins
import gzip
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from modules.config import BarsConfig, DecisionConfig, EntryTimeout, FallbackFees, RiskConfig
from modules.decision.baseline import AnswerArrays
from modules.decision.engine import DecisionEngine
from modules.decision.policy import REGIME_FEATURE, Decision, DecisionPolicy, PolicyConfig, regimes_from_pctile
from modules.dryrun import replay as replay_module
from modules.dryrun.fill_model import BarFillModel
from modules.dryrun.recorder import WsRecorder
from modules.dryrun.replay import ReplayError, ReplayMarketStream, ReplayResult, ReplayRunner, SimClock
from modules.dryrun.shadow_gateway import ShadowExecutionGateway
from modules.dryrun.virtual_account import VirtualAccount
from modules.execution.balance_guard import BalanceGuard
from modules.execution.gateway import CancelOutcome, EntryResult, ExitResult
from modules.execution.ids import client_order_id
from modules.execution.orders import ProductMeta
from modules.execution.spot import SpotRegistry
from modules.features.serializer import FLAT_COLUMNS
from modules.features.state_matrix import flat_matrix
from modules.ledger.positions import PositionLedger, PositionView
from modules.market.backfill import BarStore
from modules.market.ws_feed import Trade
from modules.risk.costs import FeeRates
from modules.risk.risk_manager import MarketQuote, OrderIntent, RiskManager
from tests.coverage_fixtures import KillSwitch
from tests.gateway_fixtures import FeeSourceStub
from tests.indicator_fixtures import synthetic_bars
from tests.test_backtest import LABEL, POLICY, StubDecider, registry
from tests.test_decision_engine import (
    QUESTIONS,
    WARM_A,
    Gate,
    Sink,
    StubBaseline,
    frame,
    policy_with,
    warmed,
)
from training.backtest import build_market

D = Decimal
BOOK = "champion"
PRODUCTS = ("BTC-EUR", "SOL-EUR")
MIN = timedelta(minutes=1)
S = timedelta(seconds=120)
REGIME_COL = FLAT_COLUMNS.index(REGIME_FEATURE)
REPLAY_PY = Path(replay_module.__file__)
SPREAD_BPS = 3.0


class RareBuy:
    """Answers that read the state: BUY only after a strong hour (about one minute in ten), edge varies with it."""

    model_key = "rare-buy"

    def answers_batch(self, x: np.ndarray) -> AnswerArrays:
        r = np.nan_to_num(x[:, FLAT_COLUMNS.index("ret_1h_bps")])
        buy = np.where(r > 22.0, 0.9, 0.1)
        pa = np.column_stack([buy, np.full(len(x), 0.02), 0.98 - buy])
        edge = np.clip(0.45 + 0.004 * r, 0.05, 0.95)
        return AnswerArrays(pa, edge, np.tile([0.1, 0.2, 0.4, 0.2, 0.1], (len(x), 1)))


# ------------------------------------------------------------------------------------------ the data


def book_levels(close: float) -> tuple[Decimal, Decimal]:
    """The best bid / ask of the recorded book around a close: 3 bps wide, on the cent."""
    bid = D(repr(close * (1 - 1.5e-4))).quantize(D("0.01"), rounding=ROUND_FLOOR)
    ask = D(repr(close * (1 + 1.5e-4))).quantize(D("0.01"), rounding=ROUND_CEILING)
    return bid, ask


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def l2_message(t: datetime, seq: int, product: str, kind: str, updates: list[tuple[str, Decimal, Decimal]]) -> str:
    ups = [{"side": s, "price_level": str(p), "new_quantity": str(q)} for s, p, q in updates]
    ev = [{"type": kind, "product_id": product, "updates": ups}]
    return json.dumps({"channel": "l2_data", "timestamp": iso(t), "sequence_num": seq, "events": ev})


def trade_message(t: datetime, seq: int, product: str, tid: int, price: float, size: float) -> str:
    row = {"product_id": product, "trade_id": str(tid), "price": str(price), "size": str(size), "time": iso(t),
           "side": "BUY"}  # fmt: skip
    return json.dumps({"channel": "market_trades", "timestamp": iso(t), "sequence_num": seq,
                       "events": [{"type": "update", "trades": [row]}]})  # fmt: skip


def heartbeat_message(t: datetime, seq: int) -> str:
    ev = [{"current_time": str(t), "heartbeat_counter": seq}]
    return json.dumps({"channel": "heartbeats", "timestamp": iso(t), "sequence_num": seq, "events": ev})


def record_window(
    root: Path,
    frames: dict[str, pd.DataFrame],
    first_row: int,
    last_row: int,
    *,
    silent: tuple[datetime, datetime] | None = None,
) -> None:
    """A recording with the real ``WsRecorder``: a snapshot before the window, then for every bar ``k`` a trade at the
    close of its minute minus 30 s and the book moved around ``close[k]`` at the close of the minute. ``silent``: no
    message in that interval (and a fresh connection, with a snapshot, when it ends)."""
    events: list[tuple[datetime, str, str, Any]] = []
    prev: dict[str, tuple[Decimal, Decimal]] = {}
    for p in PRODUCTS:
        f = frames[p]
        t0 = f.index[first_row].to_pydatetime() - 2 * MIN
        bid, ask = book_levels(float(f.close.iloc[first_row - 1]))
        events.append((t0, p, "snapshot", [("bid", bid, D(100)), ("offer", ask, D(100))]))
        prev[p] = (bid, ask)
        for k in range(first_row, last_row + 1):
            close_ts = f.index[k].to_pydatetime() + MIN
            events.append((close_ts - timedelta(seconds=30), p, "trade", (k, float(f.close.iloc[k]))))
            events.append((close_ts, p, "book", float(f.close.iloc[k])))
    t_first = frames[PRODUCTS[0]].index[first_row].to_pydatetime() - 2 * MIN
    t_last = frames[PRODUCTS[0]].index[last_row].to_pydatetime() + 3 * MIN
    for i in range(int((t_last - t_first).total_seconds())):  # the feed's heartbeat, one a second
        events.append((t_first + timedelta(seconds=i, milliseconds=500), "all", "hb", None))
    events.sort(key=lambda e: (e[0], e[1], e[2]))
    rec = WsRecorder(root, flush_s=0.2, retention_days=20_000)
    rec.start()
    seq, restart_done = 0, False
    for t, p, kind, payload in events:
        if silent is not None and silent[0] <= t < silent[1]:
            continue
        if silent is not None and t >= silent[1] and not restart_done:
            restart_done, seq = True, 0  # a new connection: the sequence restarts from 0
            for q in PRODUCTS:
                bid, ask = prev[q]
                rec.record(
                    t.timestamp(), l2_message(t, seq, q, "snapshot", [("bid", bid, D(100)), ("offer", ask, D(100))])
                )
                seq += 1
        if kind == "hb":
            msg = heartbeat_message(t, seq)
        elif kind == "snapshot":
            msg = l2_message(t, seq, p, "snapshot", payload)
        elif kind == "trade":
            msg = trade_message(t, seq, p, payload[0], payload[1], 0.5)
        else:
            bid, ask = book_levels(payload)
            old_bid, old_ask = prev[p]
            ups = [("bid", old_bid, D(0)), ("offer", old_ask, D(0)), ("bid", bid, D(100)), ("offer", ask, D(100))]
            msg = l2_message(t, seq, p, "update", ups)
            prev[p] = (bid, ask)
        rec.record(t.timestamp(), msg)
        seq += 1
    rec.stop()


# ----------------------------------------------------------------------------------------- the world


class NullVenue:
    """A venue that never fills: every entry ends at once as ``no_fill``, so the products stay flat and a BUY always
    reaches the RiskManager (used to watch what the pipeline does when the book is missing)."""

    async def submit_entry_with_bracket(self, intent: OrderIntent) -> EntryResult:
        return EntryResult("no_fill", client_order_id(intent.decision_id, "entry"))

    async def submit_exit(self, intent: Any) -> ExitResult:
        return ExitResult("nothing_to_do")

    async def emergency_exit(self, product_id: str) -> ExitResult:
        return ExitResult("nothing_to_do")

    async def cancel(self, product_id: str) -> CancelOutcome:
        return CancelOutcome("nothing_pending")

    async def on_bar(self, bar: Any) -> list[ExitResult]:
        return []

    def has_pending_entry(self, product_id: str) -> bool:
        return False

    def has_pending_exit(self, product_id: str) -> bool:
        return False


class StreamContext:
    """The BookContext of the runtime for this book: quote from the stream's own books, balances from the account."""

    def __init__(self, stream: ReplayMarketStream, reg: SpotRegistry, fees: FeeRates, account: VirtualAccount) -> None:
        self._stream, self._reg, self._fees, self._account = stream, reg, fees, account
        self._base = {p: reg.meta(p).base_currency for p in PRODUCTS}

    def quote(self, product_id: str) -> MarketQuote | None:
        book = self._stream.books[product_id]
        bid, ask = book.best_bid(), book.best_ask()
        return None if book.stale or bid is None or ask is None else MarketQuote(bid, ask)

    def fees(self) -> FeeRates:
        return self._fees

    def meta(self, product_id: str) -> ProductMeta:
        return self._reg.meta(product_id)

    def equity(self, marks: Any) -> Decimal:
        return self._account.equity(marks, self._base)

    def free_quote(self) -> Decimal:
        return self._account.balances.free("EUR")


@dataclass
class Pipeline:
    runner: ReplayRunner
    engine: DecisionEngine
    gateway: ShadowExecutionGateway
    account: VirtualAccount
    ledger: PositionLedger
    stream: ReplayMarketStream
    clock: SimClock
    sink: Sink
    trades: list[Trade] = field(default_factory=list)
    start: datetime = field(default_factory=lambda: datetime(2000, 1, 1, tzinfo=UTC))

    async def run(self, minutes: int) -> ReplayResult:
        return await self.runner.run(self.start, self.start + minutes * MIN)

    async def dump(self) -> bytes:
        """Every row of the dry ledger, canonical: what 'identical ledgers' means."""
        out: dict[str, list[list[Any]]] = {}
        for table, order in (("positions", "book, product_id"), ("orders", "client_order_id"), ("fills", "trade_id")):
            async with self.ledger.connection.execute(f"SELECT * FROM {table} ORDER BY {order}") as cur:
                out[table] = [list(r) for r in await cur.fetchall()]
        return json.dumps(out, sort_keys=True, default=str).encode()

    async def close(self) -> None:
        await self.ledger.close()


def store_dir(tmp: Path) -> Path:
    root = tmp / "data" / "bars" / "1m"
    if not root.exists():
        for p in PRODUCTS:
            BarStore(root).write(p, frame(p).reset_index())
    return root


async def build(
    tmp: Path,
    *,
    inner: Any = None,
    policy: PolicyConfig = POLICY,
    first_row: int = WARM_A,
    warm: bool = False,
    silence_s: float = 5.0,
    null_venue: bool = False,
    preroll_min: int = 5,
    history_frames: dict[str, pd.DataFrame] | None = None,
) -> Pipeline:
    clock = SimClock(frame(PRODUCTS[0]).index[first_row].to_pydatetime())
    start = frame(PRODUCTS[0]).index[first_row].to_pydatetime()
    root = store_dir(tmp) if history_frames is None else tmp / "data" / "bars" / "1m"
    if history_frames is not None:
        for p, f in history_frames.items():
            BarStore(root).write(p, f.reset_index())
    ledger = await PositionLedger.open(":memory:", "dry")
    account = VirtualAccount(BOOK, D(100), clock)
    reg = registry()
    kill = KillSwitch()
    fees = FeeSourceStub(clock)  # type: ignore[arg-type]
    guard = BalanceGuard(account.balances, ledger.book_view(BOOK), reg, fees, FallbackFees(), kill)
    gateway: Any = (
        NullVenue()
        if null_venue
        else ShadowExecutionGateway(BOOK, account, BarFillModel(), ledger, clock, guard, reg, fees)
    )
    trades: list[Trade] = []
    stream = ReplayMarketStream(PRODUCTS, clock, on_trades=trades.extend, silence_s=silence_s)
    ctx = StreamContext(stream, reg, LABEL.fees, account)
    rm = RiskManager(BOOK, RiskConfig(), LABEL.cfg, FallbackFees(), EntryTimeout(), Gate(None), LABEL.costs)
    sink = Sink()
    counter = iter(range(1, 10**6))
    engine = DecisionEngine(
        products=PRODUCTS, label=LABEL, policy=policy, questions=QUESTIONS, risk=rm, gateway=gateway,
        positions=ledger.book_view(BOOK), context=ctx,
        decision=DecisionConfig(backend="baseline", fallback_to_baseline=True), bars=BarsConfig(), sink=sink,
        baseline=StubBaseline(inner or StubDecider()), clock=clock, new_id=lambda at: f"id-{next(counter):05d}",
    )  # fmt: skip
    if not warm:
        for p in PRODUCTS:
            engine.adopt(
                p, warmed(p, first_row), frame(p).index[first_row - 1].to_pydatetime(),
                float(frame(p).close.iloc[first_row - 1]),
            )  # fmt: skip
    runner = ReplayRunner(
        clock=clock, stream=stream, engine=engine, venue=gateway, products=PRODUCTS,
        recordings=tmp / "data" / "ws_recordings", bars_root=root, bars=BarsConfig(),
        spread_bps=lambda product, ts: SPREAD_BPS, warm_up=warm, preroll=preroll_min * MIN, timeout_s=60.0,
    )  # fmt: skip
    return Pipeline(runner, engine, gateway, account, ledger, stream, clock, sink, trades, start)


@pytest.fixture
async def make(tmp_path: Path) -> Iterator[Callable[..., Any]]:
    pipes: list[Pipeline] = []

    async def factory(**kw: Any) -> Pipeline:
        p = await build(tmp_path, **kw)
        pipes.append(p)
        return p

    yield factory  # type: ignore[misc]
    for p in pipes:
        await p.close()


def without_timers(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The records minus the wall-clock measurements (``latency_ms`` times the host, not the data)."""
    return [{k: v for k, v in r.items() if k != "latency_ms"} for r in records]


def recorded(tmp: Path, minutes: int, first_row: int = WARM_A, **kw: Any) -> None:
    frames = {p: frame(p) for p in PRODUCTS}
    record_window(tmp / "data" / "ws_recordings", frames, first_row, first_row + minutes + 3, **kw)


# ============================================================================== the decisions


async def test_replay_decides_at_the_live_instants_with_the_book_of_its_own_minute(
    make: Callable[..., Any], tmp_path: Path
) -> None:
    recorded(tmp_path, 12)
    pipe = await make(inner=StubDecider())
    res = await pipe.run(12)
    assert res.bars == 24 and len(res.decisions) == 24 and res.messages > 0
    for k, got in enumerate(d for d in res.decisions if d.product_id == "SOL-EUR"):
        t = frame("SOL-EUR").index[WARM_A + k].to_pydatetime() + MIN
        rec = got.record
        assert got.ts_bar_close == t and rec["bar_source"] == "rest" and rec["bar_lag_s"] == 120.0
        assert rec["decided_at"] == (t + S).strftime("%Y-%m-%dT%H:%M:%S.000Z")  # published at t + S
        if isinstance(got.intent, OrderIntent):
            # the recorded message of the instant t + S (the book around close[k + 2]) was applied before the decision
            bid, _ask = book_levels(float(frame("SOL-EUR").close.iloc[WARM_A + k + 2]))
            assert got.intent.entry.limit_price == bid, k
    assert any(isinstance(d.intent, OrderIntent) for d in res.decisions)


async def test_replay_matches_the_batch_pipeline_of_training(make: Callable[..., Any], tmp_path: Path) -> None:
    """Every FLAT decision of the replay equals the one of the vectorised path of the backtest on the same bars."""
    n = 90
    recorded(tmp_path, n)
    inner = RareBuy()
    policy = policy_with(tau_buy=0.5, c_min=0.0, p_be_buffer=0.0)
    pipe = await make(inner=inner, policy=policy)
    res = await pipe.run(n)
    btc = build_market("BTC-EUR", frame("BTC-EUR"), LABEL)
    markets = {"BTC-EUR": btc, "SOL-EUR": build_market("SOL-EUR", frame("SOL-EUR"), LABEL, btc.ind)}
    rows = np.arange(WARM_A, WARM_A + n)
    ref_policy = DecisionPolicy(policy)
    ref: dict[str, dict[str, Any]] = {}
    for p, pm in markets.items():
        x = flat_matrix(pm.ind, rows, p, LABEL, pm.btc_ind, None if pm.btc_rows is None else pm.btc_rows[rows])
        a: AnswerArrays = inner.answers_batch(x)
        ref[p] = {
            "x": x,
            "a": a,
            "buy": ref_policy.buy_mask(a.p_action, x[:, REGIME_COL]),
            "reg": regimes_from_pctile(x[:, REGIME_COL]),
        }
    compared = buys = holds = 0
    first_intent_checked = False
    for got in res.decisions:
        p, k = got.product_id, int(frame(p := got.product_id).index.get_loc(got.ts_bar_close - MIN)) - WARM_A
        state = json.loads(got.record["state"])
        if state["position"]["side"] != "FLAT":
            continue  # the batch matrix is the FLAT state: after a fill the state has a position
        r = ref[p]
        pct = r["x"][k, REGIME_COL]
        assert got.record["policy"]["regime_pctile"] == (None if np.isnan(pct) else int(pct)), (p, k)
        assert (got.decision is not None and got.decision.action == "BUY") == bool(r["buy"][k]), (p, k)
        assert got.record["answers"]["edge"]["noul"] == float(r["a"].p_edge[k]), (p, k)
        compared += 1
        buys += bool(r["buy"][k])
        holds += not r["buy"][k]
        if isinstance(got.intent, OrderIntent) and not first_intent_checked:
            first_intent_checked = True  # nothing reserved yet: the sizing inputs are the batch ones (100 / 100)
            bid, ask = book_levels(float(markets[p].bars.c[WARM_A + k + 2]))
            batch_rm = RiskManager(BOOK, RiskConfig(), LABEL.cfg, FallbackFees(), EntryTimeout(), None, LABEL.costs)
            want = batch_rm.plan_entry(
                decision=Decision("BUY", "thresholds_met", p_edge=float(r["a"].p_edge[k]), regime=int(r["reg"][k])),
                decision_id="x", intent_id="x", now=got.intent.created_at, product_id=p, quote=MarketQuote(bid, ask),
                atr=D(repr(float(markets[p].atr[WARM_A + k]))), fees=LABEL.fees, meta=registry().meta(p),
                equity=D("100"), free_quote=D("100"), p_be_buffer=policy.p_be_buffer,
                position=PositionView(book=BOOK, product_id=p),
            )  # fmt: skip
            assert isinstance(want, OrderIntent)
            drop: Any = {"intent_id": True, "decision_id": True, "bracket": {"atr"}}
            assert got.intent.model_dump(exclude=drop) == want.model_dump(exclude=drop)
    assert compared > 40 and buys >= 2 and holds >= 20, (compared, buys, holds)  # the comparison is not vacuous
    assert first_intent_checked


async def test_replay_twice_gives_identical_ledgers_decisions_and_records(
    make: Callable[..., Any], tmp_path: Path
) -> None:
    recorded(tmp_path, 40)
    a, b = (
        await make(inner=RareBuy(), policy=policy_with(tau_buy=0.5)),
        await make(inner=RareBuy(), policy=policy_with(tau_buy=0.5)),
    )
    ra, rb = await a.run(40), await b.run(40)
    assert await a.dump() == await b.dump()
    assert a.sink.records and without_timers(a.sink.records) == without_timers(b.sink.records)
    assert [d.decision_id for d in ra.decisions] == [d.decision_id for d in rb.decisions]
    assert (ra.messages, ra.bars) == (rb.messages, rb.bars)
    dump = json.loads(await a.dump())
    assert dump["orders"] and dump["fills"], "the replay must have traded for this comparison to mean anything"


async def test_the_shadow_pipeline_ends_the_window_consistent_and_covered(
    make: Callable[..., Any], tmp_path: Path
) -> None:
    recorded(tmp_path, 40)
    pipe = await make(inner=StubDecider())
    res = await pipe.run(40)
    assert res.pending_at_end == []
    open_orders = pipe.account.balances.open_reservations()
    assert open_orders and all(r.side == "SELL" for r in open_orders)  # only brackets rest: every entry is terminal
    for r in open_orders:  # and each bracket is the one of a long position of the ledger, for what it holds
        pos = pipe.ledger.view(BOOK, r.product_id)
        assert pos.is_long and pos.bracket_order_id == r.client_order_id and r.amount <= pos.base_qty
    assert all(pipe.account.balances.free(c) >= 0 for c in ("EUR", "BTC", "SOL"))  # C11: never a negative balance
    assert pipe.ledger.mode == "dry"


async def test_an_entry_still_waiting_when_the_window_ends_is_cancelled_and_released(
    make: Callable[..., Any], tmp_path: Path
) -> None:
    recorded(tmp_path, 1)
    pipe = await make(inner=StubDecider())
    res = await pipe.run(1)  # one bar: both products decide BUY and the window is over before any bar can fill them
    assert sorted(res.cancelled_at_end) == ["BTC-EUR", "SOL-EUR"] and res.pending_at_end == []
    assert pipe.account.balances.open_reservations() == []  # the reservations went back to free
    assert pipe.account.balances.free("EUR") == D(100)
    async with pipe.ledger.connection.execute("SELECT status FROM orders") as cur:
        assert {r["status"] for r in await cur.fetchall()} == {"CANCELLED"}


# ================================================================= stream: silence, gaps, trades


async def test_a_recorded_silence_is_a_damage_and_stale_books_until_the_new_snapshot(
    make: Callable[..., Any], tmp_path: Path
) -> None:
    start = frame("SOL-EUR").index[WARM_A].to_pydatetime()
    silent = (start + timedelta(minutes=8, seconds=40), start + timedelta(minutes=12))
    recorded(tmp_path, 20, silent=silent)
    pipe = await make(inner=StubDecider(), silence_s=5.0, null_venue=True)
    res = await pipe.run(20)
    assert len(res.damage) >= 2 and res.damage[0][1] is None  # opened by the silence ...
    assert any(end is not None and end >= silent[1] for _s, end in res.damage)  # ... closed by the next message
    stale_from, stale_to = silent[0] + timedelta(seconds=5), silent[1]
    inside = [d for d in res.decisions if stale_from < d.ts_bar_close + S < stale_to]
    assert inside and all(d.intent is None for d in inside)  # no order is planned on a book nobody trusts
    assert any(d.skip_reason == "no_quote" for d in inside)  # skipped, not invented
    assert not pipe.stream.books["SOL-EUR"].stale  # the snapshot of the new connection revived the book
    assert any(isinstance(d.intent, OrderIntent) for d in res.decisions if d.ts_bar_close + S > stale_to)


async def test_trades_reach_the_consumer_exactly_as_recorded(make: Callable[..., Any], tmp_path: Path) -> None:
    recorded(tmp_path, 10)
    pipe = await make(inner=StubDecider(buy=0.1))
    await pipe.run(10)
    assert len(pipe.trades) >= 20 and {t.product_id for t in pipe.trades} == set(PRODUCTS)
    assert len({t.trade_id for t in pipe.trades if t.product_id == "SOL-EUR"}) == len(
        [t for t in pipe.trades if t.product_id == "SOL-EUR"]
    )  # none twice


async def test_the_warm_start_comes_from_the_store_and_the_bars_from_the_store_service(
    make: Callable[..., Any], tmp_path: Path
) -> None:
    """warm_up=True: the engine is built from the history before the window; the bars are those of the store."""
    short = {
        p: synthetic_bars(days=4, seed=70 + i, zero_volume_day=None, constant_days=None) for i, p in enumerate(PRODUCTS)
    }
    first = 3 * 1440
    record_window(tmp_path / "data" / "ws_recordings", short, first, first + 13)
    pipe = await make(warm=True, history_frames=short, first_row=first, inner=StubDecider())
    res = await pipe.run(10)
    start = short["SOL-EUR"].index[first].to_pydatetime()
    assert res.bars == 20 and len(res.decisions) == 20
    snap = pipe.engine.indicators["SOL-EUR"].snapshot()
    assert snap.ts == start + 9 * MIN  # the engine consumed the bars of the window, after the stored history
    assert all(d.record["bar_source"] == "rest" for d in res.decisions)


# ====================================================================== refusals and hygiene


async def test_a_window_with_no_history_before_it_or_no_recording_is_refused(
    make: Callable[..., Any], tmp_path: Path
) -> None:
    pipe = await make(inner=StubDecider())
    with pytest.raises(ReplayError, match="no recorded message"):
        await pipe.run(5)  # the store is fine, the recording directory does not exist
    recorded(tmp_path, 5)
    pipe.runner._bars_root = tmp_path / "elsewhere"
    with pytest.raises(ReplayError, match="no bar for the minute before"):
        await pipe.run(5)


async def test_the_window_must_be_minute_aligned_and_non_empty(make: Callable[..., Any], tmp_path: Path) -> None:
    pipe = await make(inner=StubDecider())
    with pytest.raises(ValueError, match="minute aligned"):
        await pipe.runner.run(pipe.start + timedelta(seconds=5), pipe.start + 5 * MIN)
    with pytest.raises(ValueError, match="minute aligned"):
        await pipe.runner.run(pipe.start, pipe.start)


async def test_the_replay_stream_has_no_connection(make: Callable[..., Any], tmp_path: Path) -> None:
    pipe = await make(inner=StubDecider())
    with pytest.raises(ReplayError, match="no connection"):
        await pipe.stream.run()


async def test_the_runner_and_the_stream_must_serve_the_same_products(make: Callable[..., Any], tmp_path: Path) -> None:
    pipe = await make(inner=StubDecider())
    with pytest.raises(ValueError, match="same products"):
        ReplayRunner(
            clock=pipe.clock, stream=pipe.stream, engine=pipe.engine, venue=pipe.gateway, products=("SOL-EUR",),
            recordings=tmp_path, bars_root=tmp_path, bars=BarsConfig(), spread_bps=lambda p, t: 3.0,
        )  # fmt: skip


def test_the_replay_never_names_the_command_queue() -> None:
    """Static half of 'the replay never reads the command queue' (sez. 5.3 punto 16 / 6.4)."""
    tree = ast.parse(REPLAY_PY.read_text(encoding="utf-8"))
    modules = {(n.module or "") for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    assert not {m for m in modules if "control" in m or "command" in m}
    idents = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
    }
    assert not {i for i in idents if "command" in i.lower() or "inbox" in i.lower()}
    assert "StoreCandleSource" in REPLAY_PY.read_text(encoding="utf-8")  # the bars come from the store (I1)


async def test_the_replay_never_reads_the_command_queue(
    make: Callable[..., Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dynamic half: a command waiting in the queue directory is never opened, listed or read during a replay."""
    recorded(tmp_path, 6)
    queue_dir = tmp_path / "data" / "commands" / "inbox"
    queue_dir.mkdir(parents=True)
    (queue_dir / "0001.json").write_text('{"command": "kill_switch_trip"}', encoding="utf-8")
    pipe = await make(inner=StubDecider())
    touched: list[str] = []
    real_open, real_gzip = builtins.open, gzip.open

    def spy_open(file: Any, *a: Any, **k: Any) -> Any:
        touched.append(str(file))
        return real_open(file, *a, **k)

    def spy_gzip(file: Any, *a: Any, **k: Any) -> Any:
        touched.append(str(file))
        return real_gzip(file, *a, **k)

    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(gzip, "open", spy_gzip)
    await pipe.run(6)
    monkeypatch.undo()
    assert touched and not [f for f in touched if "commands" in f or "inbox" in f]
    assert (queue_dir / "0001.json").exists()  # and nobody consumed it
