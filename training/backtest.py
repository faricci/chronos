"""Event-driven backtest of the whole decision path (spec sez. 3.5 punto 8, decision O2).

The backtest runs the SAME objects as the runtime: ``DecisionPolicy`` -> ``RiskManager`` -> ``BalanceGuard`` ->
execution gateway (``BarExecutionGateway`` on a ``SimulatedExchange``) -> ``VirtualAccount`` / ``PositionLedger`` ->
``KillSwitch``. One book (``backtest``) with a common equity for the three products, one pending entry and one position
per product, every order authorized by the guard (constraint C11).

Speed without changing the result. The model answers are computed in batches (``answers_batch`` on a vectorised feature
matrix pinned to the scalar ``StateBuilder`` by tests); the policy is applied with its vectorised twin pinned to
``DecisionPolicy.resolve``. The simulation then visits only the minutes where the policy says BUY, plus the events
of the open trades; fills and exits are decided by the venue from the bars the moment an order is submitted, and
applied to the account and the guard at the simulated time of the event, so the decisions of one product see the
fills and exits of the others in chronological order.

Time keys are ``ts_bar_close``. An event of bar ``k`` happens during that bar and is visible to a decision taken on bar
``t >= k``; on equal time events are applied before decisions.
"""

from __future__ import annotations

import hashlib
import heapq
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import numpy as np
import pandas as pd

from modules.config import EntryTimeout, FallbackFees, RiskConfig
from modules.decision.baseline import AnswerArrays, BaselineDecider
from modules.decision.policy import REGIME_FEATURE, Decision, DecisionPolicy, PolicyConfig, regimes_from_pctile
from modules.dryrun.bar_gateway import (
    BarExecutionGateway,
    ClosedTrade,
    EntryRefused,
    OpenTrade,
    PendingEntry,
)
from modules.dryrun.fill_model import BarFillModel, FillModelParams
from modules.dryrun.sim_exchange import BracketOutcome, MarketBars, SimulatedExchange
from modules.dryrun.virtual_account import VirtualAccount
from modules.execution.balance_guard import BalanceGuard
from modules.execution.spot import SpotRegistry
from modules.features.serializer import FLAT_COLUMNS
from modules.features.state_matrix import flat_matrix, long_matrix
from modules.ledger.positions import PositionLedger
from modules.market.indicators_batch import compute_indicators
from modules.risk.costs import FeeRates
from modules.risk.kill_switch import AccountGuards, KillSwitch, SystemGuards
from modules.risk.label_config import LoadedLabelConfig
from modules.risk.risk_manager import MarketQuote, RiskManager, Skip
from training.labeling import label_window_end, triple_barrier

__all__ = [
    "BacktestConfig",
    "BacktestResult",
    "FlatAnswers",
    "FlatProbCache",
    "LongProbCache",
    "ProductMarket",
    "Span",
    "build_market",
    "decision_rows",
    "equity_sharpe_and_drawdown",
    "run_backtest",
]

CHUNK: Final = 200_000
EVAL_DECISION_EVERY_MIN: Final = 1
TUNE_DECISION_EVERY_MIN: Final = 15
QUOTE: Final = "EUR"
FLAT_CACHE_VERSION: Final = "v2"  # v2: the cache also holds the regime token; older caches are never read
_REGIME_COL: Final = FLAT_COLUMNS.index(REGIME_FEATURE)
_NS_MIN: Final = np.timedelta64(1, "m")


@dataclass(frozen=True)
class BacktestConfig:
    starting_quote: Decimal
    decision_every_min: int
    risk: RiskConfig
    fallback_fees: FallbackFees
    entry_timeout: EntryTimeout
    fill: FillModelParams
    book: str = "backtest"


@dataclass(frozen=True)
class Span:
    """Which entry decisions are allowed: ``ts_bar_close`` in ``[entry_start, entry_end)`` whose label window ends
    before ``data_limit`` (so that no outcome uses prices past it). ``fold``: -1 is the holdout."""

    entry_start: pd.Timestamp
    entry_end: pd.Timestamp
    data_limit: pd.Timestamp | None
    fold: int

    def tag(self) -> str:
        data_limit = "none" if self.data_limit is None else self.data_limit.isoformat()
        return f"f{self.fold}-{self.entry_start:%Y%m%d}-{self.entry_end:%Y%m%d}-{data_limit}"


def _naive(ts: pd.Timestamp) -> np.datetime64:
    t = pd.Timestamp(ts)
    return np.datetime64((t.tz_convert(None) if t.tzinfo is not None else t).to_datetime64())


@dataclass(frozen=True)
class ProductMarket:
    """Raw bars of one product, its indicators and the alignment with BTC (for the BTC context)."""

    name: str
    bars: MarketBars
    ind: pd.DataFrame
    atr: np.ndarray  # atr_14_<atr_tf> in price units
    btc_ind: pd.DataFrame | None
    btc_rows: np.ndarray | None  # per bar: row in btc_ind (-1 where BTC has no bar)

    @property
    def n(self) -> int:
        return len(self.bars.c)


def build_market(
    name: str, bars: pd.DataFrame, label: LoadedLabelConfig, btc_ind: pd.DataFrame | None = None
) -> ProductMarket:
    ind = compute_indicators(bars)
    close_ts = (pd.DatetimeIndex(bars.index) + pd.Timedelta(minutes=1)).tz_convert(None)
    mb = MarketBars(
        name,
        close_ts.to_numpy(),
        *(bars[k].to_numpy(dtype=np.float64) for k in ("open", "high", "low", "close", "volume")),
    )
    btc_rows = None
    if btc_ind is not None and not name.startswith("BTC-"):
        btc_rows = btc_ind.index.get_indexer(ind.index)
    return ProductMarket(name, mb, ind, ind[label.cfg.atr_column].to_numpy(dtype=np.float64), btc_ind, btc_rows)


def decision_rows(pm: ProductMarket, label: LoadedLabelConfig, span: Span, every_min: int) -> np.ndarray:
    """Bar indices where an entry decision may be taken in ``span`` (cadence, finite ATR, window and data limit)."""
    ts = pm.bars.ts_close
    ok = (ts >= _naive(span.entry_start)) & (ts < _naive(span.entry_end))
    if every_min > 1:
        ok &= pd.DatetimeIndex(ts).minute % every_min == 0
    ok &= np.isfinite(pm.atr) & (pm.atr > 0)
    ok &= np.arange(pm.n) + label.cfg.entry_first_bar <= pm.n - 1
    rows = np.flatnonzero(ok)
    if span.data_limit is not None and rows.size:
        end = label_window_end(pd.DatetimeIndex(ts[rows]), label.cfg)
        rows = rows[np.asarray(end < _naive(span.data_limit).astype("datetime64[ns]"))]
    return rows


# ------------------------------------------------------------------------------------------ caches


def model_key(decider: BaselineDecider, label: LoadedLabelConfig) -> str:
    return hashlib.sha256(f"{decider.model_key}|{label.sha256}".encode()).hexdigest()[:16]


@dataclass(frozen=True)
class FlatAnswers:
    """Model answers for the FLAT states of ``rows`` plus the state token the policy cuts the regime from."""

    answers: AnswerArrays
    vol_pctile: np.ndarray  # state.volatility.atr_1d_pctile_365d as the integer token (NaN = missing), per row


class FlatProbCache:
    """Answers of the model for the FLAT states of a (model, product, span, cadence); optionally on disk (npz)."""

    def __init__(self, directory: Path | None = None) -> None:
        self._dir = directory
        self._mem: dict[str, tuple[np.ndarray, FlatAnswers]] = {}

    def get(
        self, key: str, decider: BaselineDecider, pm: ProductMarket, label: LoadedLabelConfig, rows: np.ndarray
    ) -> FlatAnswers:
        key = f"{FLAT_CACHE_VERSION}|{key}"
        mem_key = (
            f"{key}|{pm.name}|{rows.size}|{int(rows[0]) if rows.size else -1}|{int(rows[-1]) if rows.size else -1}"
        )
        if mem_key in self._mem:
            return self._mem[mem_key][1]
        path = (
            None
            if self._dir is None
            else self._dir
            / hashlib.sha256(key.encode()).hexdigest()[:16]
            / f"{pm.name}_{hashlib.sha256(mem_key.encode()).hexdigest()[:10]}.npz"
        )
        if path is not None and path.exists():
            z = np.load(path)
            ans = FlatAnswers(AnswerArrays(z["p_action"], z["p_edge"], z["p_regime"]), z["vol_pctile"])
        else:
            parts, pct = [], []
            for lo in range(0, rows.size, CHUNK):
                r = rows[lo : lo + CHUNK]
                x = flat_matrix(pm.ind, r, pm.name, label, pm.btc_ind, None if pm.btc_rows is None else pm.btc_rows[r])
                parts.append(decider.answers_batch(x))
                pct.append(x[:, _REGIME_COL])
            ans = FlatAnswers(
                AnswerArrays(
                    np.concatenate([p.p_action for p in parts]) if parts else np.empty((0, 3)),
                    np.concatenate([p.p_edge for p in parts]) if parts else np.empty(0),
                    np.concatenate([p.p_regime for p in parts]) if parts else np.empty((0, 5)),
                ),
                np.concatenate(pct) if pct else np.empty(0),
            )
            if path is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                a = ans.answers
                np.savez(path, p_action=a.p_action, p_edge=a.p_edge, p_regime=a.p_regime, vol_pctile=ans.vol_pctile)
        self._mem[mem_key] = (rows, ans)
        return ans


class LongProbCache:
    """P(action) for the LONG states of one trade, computed once and shared by every threshold combination."""

    def __init__(self) -> None:
        self._mem: dict[tuple[Any, ...], tuple[np.ndarray, np.ndarray]] = {}

    def get(
        self, key: tuple[Any, ...], decider: BaselineDecider, pm: ProductMarket, label: LoadedLabelConfig,
        rows: np.ndarray, position: Mapping[str, Any],
    ) -> np.ndarray:  # fmt: skip
        if key in self._mem:
            return self._mem[key][1]
        ts = pd.DatetimeIndex(pm.bars.ts_close[rows])
        x = flat_matrix(pm.ind, rows, pm.name, label, pm.btc_ind, None if pm.btc_rows is None else pm.btc_rows[rows])
        x = long_matrix(x, ts, pm.bars.c[rows], **position)
        p = decider.answers_batch(x).p_action.astype(np.float32)
        self._mem[key] = (rows, p)
        return p


# ------------------------------------------------------------------------------------------ result


@dataclass(frozen=True)
class BacktestResult:
    trades: pd.DataFrame
    equity: pd.Series  # daily, UTC midnight
    skips: dict[str, int]
    metrics: dict[str, float]
    open_at_end: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


_TRADE_COLUMNS: Final = (
    "product", "fold", "decision_ts", "decision_idx", "fill_ts", "exit_ts", "exit_reason", "net_ret_bps", "pnl_eur",
    "base_size", "notional_eur", "entry_px", "exit_px", "fees_eur", "label_outcome", "label_net_bps", "cadence_min",
)  # fmt: skip


class _FeeSource:
    def __init__(self, fees: FeeRates) -> None:
        self._fees = fees

    @property
    def current(self) -> FeeRates:
        return self._fees


def _dt(x: np.datetime64) -> datetime:
    return pd.Timestamp(x, tz="UTC").to_pydatetime()


@dataclass(order=True)
class _Event:
    time: int
    prio: int
    seq: int
    kind: str = field(compare=False)
    payload: Any = field(compare=False, default=None)


async def run_backtest(
    markets: Mapping[str, ProductMarket],
    decider: BaselineDecider,
    policy: PolicyConfig,
    label: LoadedLabelConfig,
    span: Span,
    cfg: BacktestConfig,
    registry: SpotRegistry,
    flat_cache: FlatProbCache,
    long_cache: LongProbCache,
) -> BacktestResult:
    lab = label.cfg
    F, d_every = lab.entry_first_bar, cfg.decision_every_min
    pol = DecisionPolicy(policy)
    names = list(markets)
    key = model_key(decider, label)

    # ---- candidates: minutes where the policy says BUY
    cand: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    for p in names:
        pm = markets[p]
        rows = decision_rows(pm, label, span, d_every)
        fa = flat_cache.get(f"{key}-{span.tag()}-D{d_every}", decider, pm, label, rows)
        buy = pol.buy_mask(fa.answers.p_action, fa.vol_pctile)
        cand[p] = (rows[buy], fa.answers.p_edge[buy], regimes_from_pctile(fa.vol_pctile[buy]))

    # ---- the book: the same classes as the live one
    now_box: list[datetime] = [_dt(_naive(span.entry_start))]

    def clock() -> datetime:
        return now_box[0]

    ledger = await PositionLedger.open(":memory:", "dry")
    account = VirtualAccount(cfg.book, cfg.starting_quote, clock, QUOTE)
    guards = AccountGuards(cfg.book, cfg.risk.kill_switch, cfg.risk, clock)
    kill = KillSwitch(SystemGuards(cfg.risk.kill_switch, clock), {cfg.book: guards}, clock)
    guard = BalanceGuard(account.balances, ledger.book_view(cfg.book), registry, _FeeSource(label.fees),
                         cfg.fallback_fees, kill)  # fmt: skip
    qinc = {p: registry.meta(p).quote_increment for p in names}
    base_of = {p: registry.meta(p).base_currency for p in names}
    exchange = SimulatedExchange(BarFillModel(cfg.fill), label.fees, qinc, F, lab.entry_timeout_min, lab.horizon_min)
    gateway = BarExecutionGateway(cfg.book, guard, account, exchange, ledger, registry, clock)
    rm = RiskManager(cfg.book, cfg.risk, lab, cfg.fallback_fees, cfg.entry_timeout, kill, label.costs)

    skips: dict[str, int] = {}
    blocked_bars = 0
    closed: list[tuple[ClosedTrade, str, int, float]] = []
    equity_points: list[tuple[pd.Timestamp, Decimal]] = []
    busy: dict[str, str] = {}
    next_ok: dict[str, int] = {p: 0 for p in names}
    ptr: dict[str, int] = {p: 0 for p in names}
    heap: list[_Event] = []
    seq = 0

    def push(time: int, prio: int, kind: str, payload: Any = None) -> None:
        nonlocal seq
        seq += 1
        heapq.heappush(heap, _Event(time, prio, seq, kind, payload))

    def tkey(market: ProductMarket, bar: int) -> int:
        return int(market.bars.ts_close[bar].astype("datetime64[ns]").astype(np.int64))

    def marks(time_ns: int) -> dict[str, Decimal]:
        out = {}
        for p in names:
            mb = markets[p].bars
            i = int(np.searchsorted(mb.ts_close.astype("datetime64[ns]").astype(np.int64), time_ns, side="right")) - 1
            out[p] = Decimal(repr(float(mb.c[max(i, 0)])))
        return out

    def observe(time_ns: int) -> Decimal:
        px = marks(time_ns)
        equity = account.equity(px, base_of)
        exposure = sum((account.balances.total(base_of[p]) * px[p] for p in names), Decimal(0))
        guards.observe_equity(now_box[0], equity, exposure)
        return equity

    def schedule_next_candidate(p: str) -> None:
        rows = cand[p][0]
        if ptr[p] < rows.size:
            push(tkey(markets[p], int(rows[ptr[p]])), 2, "decision", p)

    for p in names:
        schedule_next_candidate(p)
    # one equity observation per UTC midnight inside the span (daily-loss guard and the equity curve)
    start = pd.Timestamp(_naive(span.entry_start))
    last_bar_ns = max(int(markets[p].bars.ts_close[-1].astype("datetime64[ns]").astype(np.int64)) for p in names)
    end_ns = min(last_bar_ns, pd.Timestamp(_naive(span.entry_end)).value)
    day = start.normalize() + pd.Timedelta(days=1)
    while day.value <= end_ns:
        push(day.value, 1, "daytick", None)
        day += pd.Timedelta(days=1)

    open_trades = 0
    unresolved = 0
    while heap:
        ev = heapq.heappop(heap)
        now_box[0] = _dt(np.datetime64(ev.time, "ns"))
        if ev.kind == "daytick":
            eq = observe(ev.time)
            equity_points.append((pd.Timestamp(ev.time, tz="UTC"), eq))
        elif ev.kind == "entry_terminal":
            pending: PendingEntry = ev.payload
            pm = markets[pending.intent.product_id]
            trade = await gateway.entry_terminal(pending, pm.bars)
            if trade is None:
                blocked_bars += pending.outcome.haircut_blocked_bars
                next_ok[pm.name] = ev.time
                busy.pop(pm.name, None)
                skips["no_fill"] = skips.get("no_fill", 0) + 1
                continue
            blocked_bars += pending.outcome.haircut_blocked_bars
            busy[pm.name] = "position"
            if trade.bracket is None:  # the data ends before the bracket resolves: not a result
                unresolved += 1
                busy[pm.name] = "unresolved"  # stays busy: the data ends before the bracket resolves
                continue
            exit_ = _first_exit(trade, pm, decider, pol, label, d_every, long_cache, key, pending.spread_bps, exchange)
            open_trades += 1
            push(tkey(pm, exit_.bar), 0, "exit", (trade, exit_, pending.decision_bar))
        elif ev.kind == "exit":
            trade, exit_, decision_bar = ev.payload
            pm = markets[trade.intent.product_id]
            ct = await gateway.close(trade, exit_, pm.bars)
            guards.observe_trade_closed(now_box[0], ct.pnl_quote, ct.exit_reason)
            observe(ev.time)
            closed.append((ct, pm.name, decision_bar, 0.0))
            busy.pop(pm.name, None)
            next_ok[pm.name] = ev.time
            open_trades -= 1
        else:  # decision
            p = ev.payload
            pm = markets[p]
            rows, p_edge, regime = cand[p]
            j = ptr[p]
            ptr[p] += 1
            schedule_next_candidate(p)
            t = int(rows[j])
            if p in busy:
                skips["entry_pending" if busy[p] == "pending" else "position_open"] = skips.get(
                    "entry_pending" if busy[p] == "pending" else "position_open", 0) + 1  # fmt: skip
                continue
            if ev.time < next_ok[p]:
                skips["cooldown"] = skips.get("cooldown", 0) + 1
                continue
            equity = observe(ev.time)
            s = label.spread_for(p, now_box[0])
            ref = float(pm.bars.c[t + F - 1])
            quote = MarketQuote(Decimal(repr(ref * (1 - s / 2 / 1e4))), Decimal(repr(ref * (1 + s / 2 / 1e4))))
            decision = Decision("BUY", "thresholds_met", p_edge=float(p_edge[j]), regime=int(regime[j]))
            did = f"bt-{p}-{t}"
            plan = rm.plan_entry(
                decision=decision, decision_id=did, intent_id=f"int-{did}", now=now_box[0], product_id=p, quote=quote,
                atr=Decimal(repr(float(pm.atr[t]))), fees=label.fees, meta=registry.meta(p), equity=equity,
                free_quote=account.balances.free(QUOTE), p_be_buffer=policy.p_be_buffer,
                position=ledger.view(cfg.book, p),
            )  # fmt: skip
            if isinstance(plan, Skip):
                skips[plan.reason] = skips.get(plan.reason, 0) + 1
                continue
            res = await gateway.submit_entry(plan, pm.bars, t, s)
            if isinstance(res, EntryRefused):
                skips[res.reason] = skips.get(res.reason, 0) + 1
                continue
            busy[p] = "pending"
            push(tkey(pm, res.outcome.bar), 0, "entry_terminal", res)

    last_ns = max(tkey(markets[p], markets[p].n - 1) for p in names)
    final = account.equity(marks(last_ns), base_of)
    extra = {
        "final_equity": float(final), "fees_paid": float(account.fees_paid),
        "net_realized_pnl": float(account.net_realized_pnl),
        "quote_free": float(account.balances.free(QUOTE)), "quote_reserved": float(account.balances.reserved(QUOTE)),
    }  # fmt: skip
    await ledger.close()

    result = _result(closed, equity_points, skips, blocked_bars, unresolved, markets, label, cfg, span)
    return BacktestResult(result.trades, result.equity, result.skips, result.metrics, result.open_at_end, extra)


def _first_exit(
    trade: OpenTrade, pm: ProductMarket, decider: BaselineDecider, pol: DecisionPolicy, label: LoadedLabelConfig,
    every_min: int, long_cache: LongProbCache, key: str, spread_bps: float, exchange: SimulatedExchange,
) -> BracketOutcome:  # fmt: skip
    """The bracket exit, unless a model SELL decided earlier reaches the market before it (decision O2.4)."""
    br = trade.bracket
    assert br is not None
    F = label.cfg.entry_first_bar
    lo, hi = trade.fill_bar + 1, br.bar - F  # a SELL decided at t' executes at t'+F and must not come after the exit
    if hi < lo:
        return br
    rows = np.arange(lo, hi + 1)
    if every_min > 1:
        rows = rows[pd.DatetimeIndex(pm.bars.ts_close[rows]).minute % every_min == 0]
    rows = rows[np.isfinite(pm.atr[rows])]
    if rows.size == 0:
        return br
    i = trade.intent
    position = {
        "entry_px": float(str(trade.entry_px)), "stop_loss": float(str(i.bracket.stop_loss)),
        "take_profit": float(str(i.bracket.take_profit)), "atr_entry": float(str(i.bracket.atr)),
        "opened_at": pd.Timestamp(pm.bars.ts_close[trade.fill_bar] - _NS_MIN, tz="UTC"),
    }  # fmt: skip
    ck = (key, pm.name, i.decision_id, every_min)
    p = long_cache.get(ck, decider, pm, label, rows, position)
    sell = pol.sell_mask(p)
    if not sell.any():
        return br
    exit_ = exchange.model_sell(pm.bars, int(rows[int(np.argmax(sell))]), spread_bps)
    return exit_ if exit_ is not None and exit_.bar <= br.bar else br


def _result(
    closed: Sequence[tuple[ClosedTrade, str, int, float]], equity_points: Sequence[tuple[pd.Timestamp, Decimal]],
    skips: dict[str, int], blocked_bars: int, unresolved: int, markets: Mapping[str, ProductMarket],
    label: LoadedLabelConfig, cfg: BacktestConfig, span: Span,
) -> BacktestResult:  # fmt: skip
    rows: list[dict[str, Any]] = []
    for ct, product, decision_bar, _ in closed:
        pm = markets[product]
        rows.append(
            {
                "product": product, "fold": span.fold,
                "decision_ts": pd.Timestamp(pm.bars.ts_close[decision_bar], tz="UTC"),
                "decision_idx": decision_bar, "fill_ts": pd.Timestamp(pm.bars.ts_close[ct.fill_bar], tz="UTC"),
                "exit_ts": pd.Timestamp(pm.bars.ts_close[ct.exit_bar], tz="UTC"), "exit_reason": ct.exit_reason,
                "net_ret_bps": ct.net_ret_bps, "pnl_eur": float(ct.pnl_quote), "base_size": float(ct.size),
                "notional_eur": float(ct.entry_px * ct.size), "entry_px": float(ct.entry_px),
                "exit_px": float(ct.exit_px),
                "fees_eur": float(ct.fees), "cadence_min": cfg.decision_every_min,
            }
        )  # fmt: skip
    trades = pd.DataFrame(rows, columns=list(_TRADE_COLUMNS))
    if len(trades):
        outcome = np.zeros(len(trades), dtype=np.int8)
        net = np.zeros(len(trades))
        for product_key, idx in trades.groupby("product").groups.items():
            product = str(product_key)
            pm = markets[str(product)]
            mb = pm.bars
            t_idx = trades.loc[idx, "decision_idx"].to_numpy(dtype=np.int64)
            spread = np.array(
                [label.spread_for(str(product), pd.Timestamp(mb.ts_close[i], tz="UTC").to_pydatetime()) for i in t_idx]
            )
            r = triple_barrier(mb.o, mb.h, mb.lo, mb.c, pm.atr, t_idx, label.cfg, label.fees, label.costs, spread)
            pos = trades.index.get_indexer(idx)
            outcome[pos] = r.outcome
            net[pos] = 1e4 * r.net_ret
        trades["label_outcome"], trades["label_net_bps"] = outcome, net
    trades = trades.sort_values(["fill_ts", "product"], kind="stable").reset_index(drop=True)

    eq = pd.Series(
        [float(v) for _, v in equity_points], index=pd.DatetimeIndex([t for t, _ in equity_points]), dtype=float
    )
    metrics = _metrics(trades, eq, cfg)
    metrics["haircut_blocked_bars"] = float(blocked_bars)
    return BacktestResult(trades, eq, dict(sorted(skips.items())), metrics, open_at_end=unresolved)


def equity_sharpe_and_drawdown(eq: pd.Series) -> tuple[float, float]:
    """``(annualised Sharpe, max drawdown fraction)`` of a daily equity curve with at least 3 points: THE definition,
    used by the backtest metrics and by the paired OOS comparison of rule L5 (``training.oos_compare``)."""
    ret = eq.pct_change().dropna()
    sd = float(ret.std(ddof=1))
    sharpe = float(ret.mean() / sd * math.sqrt(365)) if sd > 0 else 0.0
    return sharpe, float(((eq.cummax() - eq) / eq.cummax()).max())


def _metrics(trades: pd.DataFrame, eq: pd.Series, cfg: BacktestConfig) -> dict[str, float]:
    out: dict[str, float] = {
        "n_trades": float(len(trades)),
        "pnl_eur": float(trades["pnl_eur"].sum()) if len(trades) else 0.0,
    }
    out["fees_eur"] = float(trades["fees_eur"].sum()) if len(trades) else 0.0
    if len(trades):
        gain, loss = (
            trades.loc[trades["pnl_eur"] > 0, "pnl_eur"].sum(),
            -trades.loc[trades["pnl_eur"] < 0, "pnl_eur"].sum(),
        )
        out["profit_factor"] = float(gain / loss) if loss > 0 else math.inf
        out["win_rate"] = float((trades["pnl_eur"] > 0).mean())
    if len(eq) > 2:
        out["sharpe"], out["max_dd_frac"] = equity_sharpe_and_drawdown(eq)
        years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1 / 365.25)
        if len(trades):
            out["turnover_per_year"] = float(2 * trades["notional_eur"].sum() / float(eq.mean()) / years)
    return out
