"""Fakes and a rig to run ``modules.app.Runtime`` without a network, an exchange or a bar service (test_app.py)."""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

import modules.app as app_module
from modules.app import Champion, Deps, Runtime, no_entry_policy
from modules.config import (
    AppConfig,
    BookConfig,
    CoinbaseConfig,
    DryRunConfig,
    ExecutionConfig,
    MarketConfig,
    PathsConfig,
    RecordingConfig,
    TelemetryConfig,
)
from modules.decision.baseline import AnswerArrays
from modules.decision.policy import PolicyConfig
from modules.decision.questions import build_questions
from modules.execution.exchange import KeyPermissionsInfo
from modules.market.bars import BarClosed
from modules.market.indicators import IndicatorEngine
from modules.market.orderbook import LevelUpdate, LocalOrderBook
from modules.market.timeframes import Bar
from modules.market.ws_feed import Quote
from modules.risk.costs import FeeRates
from tests.coverage_fixtures import RAW_SOL
from tests.test_backtest import LABEL, POLICY
from tests.test_decision_engine import WARM_A, StubBaseline, frame, warmed

D = Decimal
PRODUCTS = ("BTC-EUR", "SOL-EUR")
QUESTIONS = build_questions(LABEL.cfg.horizon_min, LABEL.cfg.k_tp, LABEL.cfg.k_sl, LABEL.cfg.atr_tf)
VIEW_KEY = KeyPermissionsInfo(can_view=True, can_trade=False, can_transfer=False, portfolio_type="DEFAULT")


def raw_product(product_id: str) -> dict[str, Any]:
    base, _, quote = product_id.partition("-")
    raw = copy.deepcopy(RAW_SOL)
    raw.update(product_id=product_id, base_currency_id=base, quote_currency_id=quote)
    if base == "BTC":
        raw["base_increment"], raw["base_min_size"] = "0.00001", "0.00001"
    return raw


class FakeExchange:
    """The three reads startup needs. It has no write method at all."""

    def __init__(self, perms: KeyPermissionsInfo = VIEW_KEY, raws: Mapping[str, dict[str, Any]] | None = None) -> None:
        self.perms = perms
        self.raws = raws
        self.calls: list[str] = []

    async def get_api_key_permissions(self) -> KeyPermissionsInfo:
        self.calls.append("get_api_key_permissions")
        return self.perms

    async def get_product_raw(self, product_id: str) -> dict[str, Any]:
        self.calls.append(f"get_product_raw:{product_id}")
        return dict(self.raws[product_id]) if self.raws is not None else raw_product(product_id)

    async def get_fee_rates(self) -> FeeRates:
        self.calls.append("get_fee_rates")
        return FeeRates(maker_bps=25, taker_bps=50, assumed=False)


class FakeFeed:
    """A market stream that never connects: books and tickers are set by the test."""

    def __init__(self, products: tuple[str, ...]) -> None:
        self._books = {p: LocalOrderBook(p) for p in products}
        self._quotes: dict[str, Quote] = {}
        self._spreads: dict[str, float | None] = {}
        self._last: datetime | None = None
        self.fail: Exception | None = None
        self.on_trades_cb: Any = None  # what the runtime handed to the stream (the trade callback: tick fill mode only)
        self.on_damage_cb: Any = None

    @property
    def books(self) -> Mapping[str, LocalOrderBook]:
        return self._books

    @property
    def quotes(self) -> Mapping[str, Quote]:
        return self._quotes

    @property
    def last_message_at(self) -> datetime | None:
        return self._last

    def spread_bps(self, product_id: str) -> float | None:
        return self._spreads.get(product_id)

    def heard(self, at: datetime) -> None:
        self._last = at

    def quote_around(self, product_id: str, price: float, at: datetime, spread_bps: float = 3.0) -> None:
        bid = D(repr(round(price * (1 - spread_bps / 2 / 1e4), 2)))
        ask = D(repr(round(price * (1 + spread_bps / 2 / 1e4), 2)))
        self._books[product_id].apply_snapshot([LevelUpdate("bid", bid, D(50)), LevelUpdate("offer", ask, D(50))], at)
        self._spreads[product_id] = spread_bps
        self._quotes[product_id] = Quote(product_id, price, float(bid), float(ask), 50.0, 50.0, at)
        self._last = at

    async def run(self) -> None:
        if self.fail is not None:
            raise self.fail
        await asyncio.Event().wait()


class FakeBars:
    """Stands in for ``BarService``: the test publishes bars straight into the queue the runtime reads."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[BarClosed] | None = None
        self.finalizer: Any = None
        self.writer: Any = None
        self.fail: Exception | None = None

    async def run(self) -> None:
        if self.fail is not None:
            raise self.fail
        await asyncio.Event().wait()


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class CachedEngine(IndicatorEngine):
    """The real ``IndicatorEngine`` whose warm-up of a prefix of the synthetic history is served from a cache of
    deep copies (a warm-up of 139 days takes seconds and every test would pay it twice)."""

    def warm_up(self, bars_1m: pd.DataFrame) -> None:
        if len(bars_1m) == WARM_A and bars_1m.index.equals(frame(self.product_id).index[:WARM_A]):
            self.__dict__.update(warmed(self.product_id, WARM_A).__dict__)
        else:
            super().warm_up(bars_1m)


class NoCandles:
    def fetch(self, product_id: str, start_s: int, end_s: int) -> list[dict[str, Any]]:
        return []


class FakeSentiment:
    def __init__(self) -> None:
        self.calls: list[tuple[str, datetime]] = []
        self.ran = False

    def snapshot(self, product_id: str, as_of: datetime) -> Any:
        self.calls.append((product_id, as_of))
        raise LookupError("no sentiment in this test")  # the engine treats it as "no sentiment" (valid state)

    async def run(self) -> None:
        self.ran = True
        await asyncio.Event().wait()


class AllIn:
    """Always answers BUY with probability 1 and a good edge: only the layers after the policy can say no."""

    model_key = "all-in"

    def answers_batch(self, x: np.ndarray) -> AnswerArrays:
        n = len(x)
        return AnswerArrays(
            np.tile([1.0, 0.0, 0.0], (n, 1)), np.full(n, 0.9), np.tile([0.0, 0.0, 1.0, 0.0, 0.0], (n, 1))
        )


class HoldAll:
    """Always HOLD: the pipeline runs end to end and nothing ever reaches the venue."""

    model_key = "hold-all"

    def answers_batch(self, x: np.ndarray) -> AnswerArrays:
        n = len(x)
        return AnswerArrays(
            np.tile([0.0, 0.0, 1.0], (n, 1)), np.full(n, 0.4), np.tile([0.0, 0.0, 1.0, 0.0, 0.0], (n, 1))
        )


def make_champion(
    *,
    untunable: str | None = None,
    sentiment: bool = False,
    policy: PolicyConfig | None = None,
    inner: Any = None,
) -> Champion:
    baseline = StubBaseline(inner or AllIn())
    if untunable is not None:
        chosen = no_entry_policy("stub", LABEL.sha256)
    else:
        chosen = policy or PolicyConfig(**{**POLICY.model_dump(), "tau_buy": 0.5, "c_min": 0.0})
    return Champion("stub", Path("models/baseline/stub"), LABEL, QUESTIONS, chosen, untunable, sentiment, baseline)


CHAMPION_ONLY = (BookConfig(name="champion", decider="registry:champion"),)


def make_cfg(
    tmp: Path, *, mode: str = "dry", products: tuple[str, ...] = PRODUCTS, telemetry: TelemetryConfig | None = None,
    **dry: Any,
) -> AppConfig:
    """A config whose every path is under ``tmp`` (the telemetry too). The books default to the champion only; a test
    that wants the others passes ``books=...``."""
    dry.setdefault("books", CHAMPION_ONLY)
    return AppConfig(
        paths=PathsConfig(
            state_dir=str(tmp / "state"), data_dir=str(tmp / "data"), models_dir=str(tmp / "models"),
            news_db=str(tmp / "state" / "news.db"), live_db=str(tmp / "state" / "chronos_live.db"),
        ),
        market=MarketConfig(products=products),
        execution=ExecutionConfig(mode=mode),  # type: ignore[arg-type]
        dry_run=DryRunConfig(
            db_path=str(tmp / "state" / "chronos_dry.db"), recording=RecordingConfig(enabled=False), **dry
        ),
        telemetry=telemetry or TelemetryConfig(dir=str(tmp / "telemetry")),
        coinbase=CoinbaseConfig(),
    )  # fmt: skip


async def fast_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds / 200)


@dataclass
class Rig:
    rt: Runtime
    cfg: AppConfig
    clock: FakeClock
    feed: FakeFeed
    bars: FakeBars
    exchange: FakeExchange
    sentiment_factory_calls: list[int] = field(default_factory=list)
    task: asyncio.Task[int] | None = None

    @property
    def book(self) -> Any:
        return self.rt.books[0]

    def book_named(self, name: str) -> Any:
        return next(b for b in self.rt.books if b.name == name)

    def event(self, product: str, i: int, *, lag: float = 120.0) -> BarClosed:
        row = frame(product).iloc[i]
        ts = frame(product).index[i].to_pydatetime()
        bar = Bar(ts, float(row.open), float(row.high), float(row.low), float(row.close), float(row.volume))
        return BarClosed(product, bar, "rest", ts + timedelta(seconds=60 + lag))

    def publish(self, product: str, i: int, **kw: Any) -> BarClosed:
        ev = self.event(product, i, **kw)
        self.clock.now = ev.published_at
        self.feed.quote_around(product, float(frame(product).close.iloc[i]), ev.published_at)
        self.rt.bar_queue.put_nowait(ev)
        return ev

    def start(self) -> None:
        self.task = asyncio.create_task(self.rt.run())

    async def stop(self) -> int:
        assert self.task is not None
        self.rt.request_stop("test")
        return await asyncio.wait_for(self.task, 30)

    def decisions(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for path in sorted((Path(self.cfg.paths.data_dir) / "decisions").glob("*.jsonl")):
            out += [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        return out


async def until(predicate: Any, timeout: float = 20.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


def tiny_history(product: str, days: int = 3) -> pd.DataFrame:
    from tests.indicator_fixtures import synthetic_bars

    seed = {"BTC-EUR": 51, "SOL-EUR": 52}.get(product, 53)
    return synthetic_bars(days=days, seed=seed, zero_volume_day=None, constant_days=None)


@asynccontextmanager
async def make_rig(
    tmp: Path,
    *,
    champion: Champion | None = None,
    cfg: AppConfig | None = None,
    upto: int | None = WARM_A,
    exchange: FakeExchange | None = None,
    sentiment: FakeSentiment | None = None,
    env: Mapping[str, str] | None = None,
    sleep: Any = fast_sleep,
    history_frames: Mapping[str, pd.DataFrame] | None = None,
    **dep_overrides: Any,
) -> AsyncIterator[Rig]:
    """A started (not yet running) ``Runtime`` over fakes. ``upto``: bars of the synthetic history that exist at start
    (``None``: three days only, enough to start but not to decide); ``history_frames`` replaces the history itself."""
    cfg = cfg or make_cfg(tmp)
    products = tuple(cfg.market.products)
    feed, bars, ex = FakeFeed(products), FakeBars(), exchange or FakeExchange()
    history = dict(history_frames) if history_frames is not None else {
        p: (frame(p).iloc[:upto] if upto is not None else tiny_history(p)) for p in products
    }
    end = history[products[0]].index[-1].to_pydatetime()
    clock = FakeClock(end + timedelta(seconds=200))
    rig_calls: list[int] = []

    async def sentiment_factory(c: AppConfig, e: Mapping[str, str], stack: AsyncExitStack) -> FakeSentiment:
        rig_calls.append(1)
        assert sentiment is not None
        return sentiment

    async def no_backfill(c: AppConfig, source: Any, clk: Any) -> None:
        return None

    def bar_service(
        finalizer: Any, queue: asyncio.Queue[BarClosed], source: Any, writer: Any, c: Any, clk: Any
    ) -> FakeBars:
        bars.queue, bars.finalizer, bars.writer = queue, finalizer, writer
        return bars

    def stream(prods: Any, tap: Any, on_damage: Any, on_trades: Any = None) -> FakeFeed:
        feed.on_damage_cb, feed.on_trades_cb = on_damage, on_trades
        return feed

    deps = Deps(
        clock=clock, sleep=sleep, exchange=lambda c: ex, candle_source=NoCandles, backfill=no_backfill,
        history=lambda root, pid: history[pid], champion=lambda models_dir, spec: champion or make_champion(),
        stream=stream, bar_service=bar_service, sentiment=sentiment_factory,
        **dep_overrides,
    )  # fmt: skip
    app_module.IndicatorEngine = CachedEngine  # type: ignore[misc]
    try:
        rt = await Runtime.start(cfg, env or {}, deps)
    finally:
        app_module.IndicatorEngine = IndicatorEngine  # type: ignore[misc]
    rig = Rig(rt, cfg, clock, feed, bars, ex, rig_calls)
    feed.heard(clock.now)
    try:
        yield rig
    finally:
        if rig.task is not None and not rig.task.done():
            rt.request_stop("teardown")
            await asyncio.wait_for(rig.task, 30)
        elif rig.task is None:
            await rt.stack.aclose()


__all__ = [
    "PRODUCTS", "AllIn", "HoldAll", "FakeBars", "FakeClock", "FakeExchange", "FakeFeed", "FakeSentiment", "NoCandles",
    "Rig", "make_champion", "make_cfg", "make_rig", "raw_product", "tiny_history", "until",
]  # fmt: skip
