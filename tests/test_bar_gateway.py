"""BarExecutionGateway: the live order life cycle on a simulated venue, every order through the BalanceGuard (C11)."""

from __future__ import annotations

import ast
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from modules.config import FallbackFees
from modules.dryrun.bar_gateway import BacktestInvariantError, BarExecutionGateway, EntryRefused, PendingEntry
from modules.dryrun.fill_model import BarFillModel
from modules.dryrun.sim_exchange import BracketOutcome, MarketBars, SimulatedExchange
from modules.dryrun.virtual_account import VirtualAccount
from modules.execution.balance_guard import BalanceGuard
from modules.execution.spot import SpotRegistry
from modules.ledger.positions import PositionLedger
from modules.risk.costs import FeeRates
from modules.risk.risk_manager import OrderIntent
from tests.coverage_fixtures import SOL, KillSwitch
from tests.test_risk_manager import plan

D = Decimal
FEES = FeeRates(maker_bps=25, taker_bps=50, assumed=False)
F, T_IN, H = 3, 30, 200
NOW = datetime(2026, 1, 1, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]


class FeeSource:
    current = FEES


@dataclass
class Env:
    gateway: BarExecutionGateway
    account: VirtualAccount
    ledger: PositionLedger
    exchange: SimulatedExchange
    guard_calls: list[str]
    ks: KillSwitch


def market(n: int = 500, price: float = 92.5) -> MarketBars:
    ts = (np.datetime64("2026-01-01T00:01") + np.arange(n) * np.timedelta64(1, "m")).astype("datetime64[ns]")
    full = np.full(n, price)
    return MarketBars("SOL-EUR", ts, full.copy(), full.copy(), full.copy(), full.copy(), np.full(n, 1000.0))


def intent() -> OrderIntent:
    i = plan()
    assert isinstance(i, OrderIntent)
    return i  # SOL-EUR: BUY 0.024 @ 92.03, TP 108.96, SL 83.57 (maker, book 'champion')


@pytest.fixture
async def env(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Env]:
    ledger = await PositionLedger.open(":memory:", "dry")
    account = VirtualAccount("champion", D("100"), lambda: NOW, "EUR")
    ks = KillSwitch()
    guard = BalanceGuard(account.balances, ledger.book_view("champion"), SpotRegistry({"SOL-EUR": SOL}), FeeSource(),
                         FallbackFees(), ks)  # fmt: skip
    calls: list[str] = []
    original = BalanceGuard.authorize

    def spy(self: BalanceGuard, spec):  # type: ignore[no-untyped-def]
        calls.append(f"{spec.side}:{spec.kind}")
        return original(self, spec)

    monkeypatch.setattr(BalanceGuard, "authorize", spy)
    exchange = SimulatedExchange(BarFillModel(), FEES, {"SOL-EUR": SOL.quote_increment}, F, T_IN, H)
    gateway = BarExecutionGateway("champion", guard, account, exchange, ledger, SpotRegistry({"SOL-EUR": SOL}),
                                  lambda: NOW)  # fmt: skip
    yield Env(gateway, account, ledger, exchange, calls, ks)
    await ledger.close()


def with_fill(m: MarketBars, bar: int, limit: float = 92.03) -> None:
    m.lo[bar] = limit - 0.5  # the entry fills on this bar


# -------------------------------------------------------------------------------------- life cycle


async def test_entry_fill_tp_exit_exact_accounting(env: Env) -> None:
    m = market()
    with_fill(m, 13)
    m.h[60] = 120.0  # the take-profit
    i = intent()
    pending = await env.gateway.submit_entry(i, m, 10, 3.0)
    assert isinstance(pending, PendingEntry) and pending.outcome.bar == 13
    assert env.account.balances.reserved("EUR") > 0  # the BUY is reserved while it rests
    trade = await env.gateway.entry_terminal(pending, m)
    assert trade is not None and trade.bracket is not None and trade.bracket.reason == "TP" and trade.bracket.bar == 60
    pos = env.ledger.view("champion", "SOL-EUR")
    assert pos.is_long and pos.base_qty == D("0.024") and pos.avg_entry == D("92.03") and pos.bracket_order_id
    assert env.account.balances.reserved("SOL") == D("0.024") and env.account.balances.reserved("EUR") == 0
    closed = await env.gateway.close(trade, trade.bracket, m)
    entry_cost = D("92.03") * D("0.024") * (1 + D("25") / 10000)
    proceeds = D("108.96") * D("0.024") * (1 - D("25") / 10000)
    assert closed.exit_reason == "TP" and closed.pnl_quote == proceeds - entry_cost
    assert env.account.balances.free("EUR") == D("100") - entry_cost + proceeds
    assert env.account.balances.free("SOL") == 0 and env.account.balances.reserved("SOL") == 0
    assert not env.ledger.view("champion", "SOL-EUR").is_long
    assert env.guard_calls == ["BUY:limit", "SELL:bracket"]  # TP fills the bracket itself: no extra order
    assert closed.net_ret_bps == pytest.approx(float(D(10_000) * (proceeds / entry_cost - 1)))


async def test_stop_exit_is_a_loss_through_the_bracket(env: Env) -> None:
    m = market()
    with_fill(m, 13)
    m.lo[50] = 80.0
    pending = await env.gateway.submit_entry(intent(), m, 10, 3.0)
    assert isinstance(pending, PendingEntry)
    trade = await env.gateway.entry_terminal(pending, m)
    assert trade is not None and trade.bracket is not None and trade.bracket.reason == "SL"
    closed = await env.gateway.close(trade, trade.bracket, m)
    assert closed.pnl_quote < 0 and closed.exit_reason == "SL"
    assert env.account.balances.free("EUR") > 0 and env.account.balances.free("SOL") == 0


async def test_model_sell_cancels_the_bracket_then_sells_at_market_through_the_guard(env: Env) -> None:
    m = market()
    with_fill(m, 13)
    pending = await env.gateway.submit_entry(intent(), m, 10, 3.0)
    assert isinstance(pending, PendingEntry)
    trade = await env.gateway.entry_terminal(pending, m)
    assert trade is not None
    sell = env.exchange.model_sell(m, 40, 3.0)
    assert sell is not None
    closed = await env.gateway.close(trade, sell, m)
    assert closed.exit_reason == "SELL"
    assert env.guard_calls == [
        "BUY:limit",
        "SELL:bracket",
        "SELL:market",
    ]  # the market sell needs its own authorization
    assert env.account.balances.reserved("SOL") == 0 and env.account.balances.free("SOL") == 0
    assert (await env.ledger.open_orders("champion")) == []


async def test_horizon_exit_also_goes_through_the_guard(env: Env) -> None:
    m = market()
    with_fill(m, 13)
    pending = await env.gateway.submit_entry(intent(), m, 10, 3.0)
    assert isinstance(pending, PendingEntry)
    trade = await env.gateway.entry_terminal(pending, m)
    assert trade is not None and trade.bracket is not None and trade.bracket.reason == "HORIZON"
    closed = await env.gateway.close(trade, trade.bracket, m)
    assert closed.exit_reason == "HORIZON" and env.guard_calls[-1] == "SELL:market"


async def test_an_unfilled_entry_releases_its_reservation(env: Env) -> None:
    m = market()  # the price never reaches the limit
    pending = await env.gateway.submit_entry(intent(), m, 10, 3.0)
    assert isinstance(pending, PendingEntry) and pending.outcome.status == "EXPIRED"
    assert env.account.balances.reserved("EUR") > 0
    assert await env.gateway.entry_terminal(pending, m) is None
    assert env.account.balances.reserved("EUR") == 0 and env.account.balances.free("EUR") == D("100")
    assert not env.ledger.view("champion", "SOL-EUR").is_long


async def test_a_thin_market_expires_the_entry_with_a_blocked_bar(env: Env) -> None:
    m = market()
    m.v[:] = 0.01  # haircut 25% of 0.01 << 0.024
    with_fill(m, 13)
    pending = await env.gateway.submit_entry(intent(), m, 10, 3.0)
    assert isinstance(pending, PendingEntry) and pending.outcome.status == "EXPIRED"
    assert pending.outcome.haircut_blocked_bars == 1


# ---------------------------------------------------------------------------------- coverage (C11)


async def test_coverage_refusal_on_an_entry_is_a_skip_not_an_exception(env: Env) -> None:
    broke = VirtualAccount("champion", D("2"), lambda: NOW, "EUR")
    guard = BalanceGuard(broke.balances, env.ledger.book_view("champion"), SpotRegistry({"SOL-EUR": SOL}), FeeSource(),
                         FallbackFees(), env.ks)  # fmt: skip
    gw = BarExecutionGateway("champion", guard, broke, env.exchange, env.ledger, SpotRegistry({"SOL-EUR": SOL}),
                             lambda: NOW)  # fmt: skip
    res = await gw.submit_entry(intent(), market(), 10, 3.0)
    assert isinstance(res, EntryRefused) and res.reason == "insufficient_balance"
    assert broke.balances.free("EUR") == D("2") and broke.balances.reserved("EUR") == 0  # nothing was reserved


async def test_a_coverage_breach_blocks_the_entry(env: Env) -> None:
    env.ks.breached.add("champion")
    res = await env.gateway.submit_entry(intent(), market(), 10, 3.0)
    assert isinstance(res, EntryRefused) and res.reason == "kill_switch"


async def test_a_refused_exit_leg_is_a_bug_not_a_skip(env: Env) -> None:
    m = market()
    with_fill(m, 13)
    pending = await env.gateway.submit_entry(intent(), m, 10, 3.0)
    assert isinstance(pending, PendingEntry)
    trade = await env.gateway.entry_terminal(pending, m)
    assert trade is not None
    env.account.balances.release(trade.bracket_cid)  # sabotage: the bracket reservation disappears
    env.account.balances._free["SOL"] = D(0)  # type: ignore[attr-defined]  # and the base with it
    with pytest.raises(BacktestInvariantError):
        await env.gateway.close(trade, BracketOutcome("SELL", 40, D("92")), m)


async def test_every_order_is_recorded_and_authorized_once(env: Env) -> None:
    """Orders simulated == tickets consumed: the ledger and the guard saw exactly the same number of orders."""
    m = market()
    with_fill(m, 13)
    pending = await env.gateway.submit_entry(intent(), m, 10, 3.0)
    assert isinstance(pending, PendingEntry)
    trade = await env.gateway.entry_terminal(pending, m)
    assert trade is not None
    sell = env.exchange.model_sell(m, 40, 3.0)
    assert sell is not None
    await env.gateway.close(trade, sell, m)
    async with env.ledger.connection.execute("SELECT COUNT(*) FROM orders") as cur:
        n_orders = (await cur.fetchone())[0]  # type: ignore[index]
    assert n_orders == len(env.guard_calls) == 3


# ------------------------------------------------------------------------------------- the AST rule


def test_bar_gateway_requires_guard() -> None:
    """Same rule as T18 for the live gateways: a mandatory, typed, non-Optional ``guard`` that is really used."""
    tree = ast.parse((ROOT / "modules" / "dryrun" / "bar_gateway.py").read_text(encoding="utf-8"))
    classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name.endswith("ExecutionGateway")]
    assert classes
    for cls in classes:
        init = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__")
        args = init.args.args
        defaults_start = len(args) - len(init.args.defaults)
        names = [a.arg for a in args]
        assert "guard" in names and names.index("guard") < defaults_start, "guard must be a mandatory parameter"
        guard = args[names.index("guard")]
        text = ast.unparse(guard.annotation) if guard.annotation else ""
        assert "BalanceGuard" in text and "None" not in text
        assert any(isinstance(n, ast.Attribute) and n.attr == "authorize" for n in ast.walk(cls))


def test_the_gateway_never_builds_tickets_or_reservations_itself() -> None:
    source = (ROOT / "modules" / "dryrun" / "bar_gateway.py").read_text(encoding="utf-8")
    assert "CoverageTicket" not in source and "._reserve" not in source and "_free[" not in source
