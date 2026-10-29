"""Event-driven backtest: properties of the whole path (policy, risk, guard, venue, account) on synthetic bars."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from modules.config import EntryTimeout, FallbackFees, KillSwitchConfig, RiskConfig
from modules.decision.baseline import AnswerArrays
from modules.decision.policy import REGIME_RULE, PolicyConfig, regimes_from_pctile
from modules.dryrun.fill_model import FillModelParams
from modules.execution.balance_guard import BalanceGuard
from modules.execution.orders import ProductMeta
from modules.execution.spot import SpotRegistry
from modules.features.serializer import FLAT_COLUMNS
from modules.ledger.positions import PositionLedger
from modules.market.indicators_batch import compute_indicators
from tests.indicator_fixtures import synthetic_bars
from tests.label_fixtures import loaded_label
from training.backtest import (
    BacktestConfig,
    FlatProbCache,
    LongProbCache,
    Span,
    build_market,
    run_backtest,
)

D = Decimal
POS_LONG = FLAT_COLUMNS.index("pos_is_long")
POS_HOURS = FLAT_COLUMNS.index("pos_hours_held")
LABEL = loaded_label({"BTC-EUR": 3.0, "SOL-EUR": 3.0}, horizon_min=4 * 1440)
POLICY = PolicyConfig(model="stub", tau_buy=0.5, tau_sell=0.5, c_min=0.0, p_be_buffer=0.0,
                      regime_rule=REGIME_RULE, label_config_sha256=LABEL.sha256)  # fmt: skip


class StubDecider:
    """Fixed answers: BUY when flat (edge 0.9, regime normal); SELL once a long has been held ``sell_after_h``."""

    model_key = "stub"

    def __init__(self, buy: float = 0.8, sell_after_h: float | None = None) -> None:
        self.buy, self.sell_after_h = buy, sell_after_h

    def answers_batch(self, x: np.ndarray) -> AnswerArrays:
        n = len(x)
        pa = np.tile([self.buy, 0.02, 0.98 - self.buy], (n, 1))
        if self.sell_after_h is not None:
            sell = (x[:, POS_LONG] == 1.0) & (x[:, POS_HOURS] >= self.sell_after_h)
            pa[sell] = [0.01, 0.9, 0.09]
        reg = np.tile([0.0, 0.0, 1.0, 0.0, 0.0], (n, 1))
        return AnswerArrays(pa, np.full(n, 0.9), reg)


def registry() -> SpotRegistry:
    def meta(pid: str, base: str, binc: str) -> ProductMeta:
        return ProductMeta(product_id=pid, base_currency=base, quote_currency="EUR", base_increment=D(binc),
                           quote_increment=D("0.01"), base_min_size=D(binc), quote_min_size=D("1"))  # fmt: skip

    return SpotRegistry({"BTC-EUR": meta("BTC-EUR", "BTC", "0.00001"), "SOL-EUR": meta("SOL-EUR", "SOL", "0.001")})


@pytest.fixture(scope="module")
def markets() -> dict[str, object]:
    btc = synthetic_bars(days=150, seed=51, zero_volume_day=None, constant_days=None)
    sol = synthetic_bars(days=150, seed=52, zero_volume_day=None, constant_days=None)
    bm = build_market("BTC-EUR", btc, LABEL)
    return {"BTC-EUR": bm, "SOL-EUR": build_market("SOL-EUR", sol, LABEL, bm.ind)}


# the regime token (ATR percentile over 365 days) needs >= 104 daily bars: decisions start at day 112
SPAN = Span(pd.Timestamp("2024-04-22", tz="UTC"), pd.Timestamp("2024-05-27", tz="UTC"), None, 0)


def config(every: int = 15, **risk: object) -> BacktestConfig:
    return BacktestConfig(D("100"), every, RiskConfig(**risk), FallbackFees(), EntryTimeout(), FillModelParams())  # type: ignore[arg-type]


async def run(markets, decider=None, cfg=None, span=SPAN, cache_dir: Path | None = None, policy=POLICY):  # type: ignore[no-untyped-def]
    return await run_backtest(
        markets, decider or StubDecider(), policy, LABEL, span, cfg or config(), registry(), FlatProbCache(cache_dir),
        LongProbCache(),
    )  # fmt: skip


@pytest.fixture(scope="module")
async def base(markets):  # type: ignore[no-untyped-def]
    return await run(markets)


# ------------------------------------------------------------------------------------------ shape


async def test_trades_have_the_documented_columns_and_sane_values(base) -> None:  # type: ignore[no-untyped-def]
    t = base.trades
    assert len(t) > 5, base.skips
    for col in ("product", "fold", "decision_ts", "decision_idx", "fill_ts", "exit_ts", "exit_reason", "net_ret_bps",
                "pnl_eur", "base_size", "notional_eur", "entry_px", "exit_px", "fees_eur", "label_outcome",
                "label_net_bps", "cadence_min"):  # fmt: skip
        assert col in t.columns
    assert (t["exit_ts"] >= t["fill_ts"]).all() and (t["fill_ts"] > t["decision_ts"]).all()
    assert set(t["exit_reason"]) <= {"TP", "SL", "SELL", "HORIZON"} and (t["notional_eur"] > 0).all()
    assert (t["cadence_min"] == 15).all() and (pd.DatetimeIndex(t["decision_ts"]).minute % 15 == 0).all()
    assert set(t["fold"]) == {0}


async def test_one_position_per_product_at_a_time(base) -> None:  # type: ignore[no-untyped-def]
    for _, g in base.trades.groupby("product"):
        g = g.sort_values("fill_ts")
        assert (g["fill_ts"].to_numpy()[1:] >= g["exit_ts"].to_numpy()[:-1]).all()


async def test_a_fill_in_the_backtest_implies_a_fill_in_the_label(base) -> None:  # type: ignore[no-untyped-def]
    """D1 invariant: the rounded limit is at or below the label limit, so the label (NO_FILL = 0) filled too."""
    assert (base.trades["label_outcome"] != 0).all()


async def test_backtest_and_label_agree_on_the_net_return_of_the_same_outcome(base) -> None:  # type: ignore[no-untyped-def]
    t = base.trades
    same = t[
        ((t["exit_reason"] == "TP") & (t["label_outcome"] == 1))
        | ((t["exit_reason"] == "SL") & (t["label_outcome"] == 2))
    ]
    assert len(same) >= 0.6 * len(t[t["exit_reason"].isin(["TP", "SL"])])
    # differences come from the roundings of entry, TP and SL to the quote increment (a few bps at most on ~100 EUR)
    assert (same["net_ret_bps"] - same["label_net_bps"]).abs().max() < 8.0


async def test_accounting_closes(base) -> None:  # type: ignore[no-untyped-def]
    assert base.open_at_end == 0
    x = base.extra
    assert x["quote_reserved"] == pytest.approx(0.0, abs=1e-9)
    assert x["final_equity"] == pytest.approx(100.0 + base.trades["pnl_eur"].sum(), abs=1e-6)
    assert x["fees_paid"] == pytest.approx(base.trades["fees_eur"].sum(), abs=1e-6)
    assert x["quote_free"] == pytest.approx(x["final_equity"], abs=1e-6)  # everything is back in quote


async def test_equity_curve_is_daily_and_starts_near_the_capital(base) -> None:  # type: ignore[no-untyped-def]
    assert (base.equity.index.hour == 0).all() and base.equity.iloc[0] == pytest.approx(100.0, rel=0.02)
    assert {"sharpe", "max_dd_frac", "profit_factor", "turnover_per_year"} <= set(base.metrics)


# --------------------------------------------------------------------------------------- C11 / guard


async def test_backtest_every_order_goes_through_the_guard(markets, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[str] = []
    orders: list[str] = []
    original = BalanceGuard.authorize
    original_record = PositionLedger.record_order

    def spy(self, spec):  # type: ignore[no-untyped-def]
        calls.append(spec.client_order_id)
        return original(self, spec)

    async def spy_record(self, o):  # type: ignore[no-untyped-def]
        orders.append(o.client_order_id)
        return await original_record(self, o)

    monkeypatch.setattr(BalanceGuard, "authorize", spy)
    monkeypatch.setattr(PositionLedger, "record_order", spy_record)
    res = await run(markets, StubDecider(sell_after_h=3))
    assert len(res.trades) > 3 and (res.trades["exit_reason"] == "SELL").any()
    assert calls == orders and len(calls) == len(set(calls))  # one authorization per simulated order, none skipped


async def test_a_coverage_refusal_becomes_a_skip(markets) -> None:  # type: ignore[no-untyped-def]
    cfg = BacktestConfig(D("1.50"), 15, RiskConfig(), FallbackFees(), EntryTimeout(), FillModelParams())
    res = await run(markets, cfg=cfg)
    assert res.trades.empty and sum(res.skips.values()) > 0
    assert set(res.skips) <= {"insufficient_balance", "below_min_size", "cooldown", "no_fill"}


# ------------------------------------------------------------------------------------ determinism


async def test_backtest_is_deterministic_and_the_disk_cache_changes_nothing(markets, base, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    again = await run(markets)
    pd.testing.assert_frame_equal(base.trades, again.trades)
    first = await run(markets, cache_dir=tmp_path)
    cached = await run(markets, cache_dir=tmp_path)  # second run reads the npz files written by the first
    pd.testing.assert_frame_equal(first.trades, cached.trades)
    pd.testing.assert_frame_equal(base.trades, cached.trades)
    assert list(tmp_path.rglob("*.npz"))


# ------------------------------------------------------------------------------- model SELL / kill


async def test_model_sell_exits_follow_the_decision_delay(markets) -> None:  # type: ignore[no-untyped-def]
    res = await run(markets, StubDecider(sell_after_h=2))
    sells = res.trades[res.trades["exit_reason"] == "SELL"]
    assert len(sells) > 3
    # a SELL needs a LONG state held >= 2 h; it executes F bars after the decision bar
    held_min = (sells["exit_ts"] - sells["fill_ts"]).dt.total_seconds() / 60
    assert (held_min >= 2 * 60 + LABEL.cfg.entry_first_bar - 1).all()


async def test_a_tight_daily_loss_limit_blocks_entries(markets) -> None:  # type: ignore[no-untyped-def]
    off = dict(max_daily_loss_frac=1.0, max_consecutive_losses=10_000)  # a guard that cannot trip
    loose = await run(markets, cfg=config(kill_switch=KillSwitchConfig(**off)))
    strict = await run(markets, cfg=config(kill_switch=KillSwitchConfig(**{**off, "max_daily_loss_frac": 0.0001})))
    assert loose.skips.get("kill_switch", 0) == 0 and strict.skips.get("kill_switch", 0) > 0
    assert len(strict.trades) < len(loose.trades)


async def test_a_loss_streak_pauses_the_book(markets) -> None:  # type: ignore[no-untyped-def]
    res = await run(
        markets, cfg=config(kill_switch=KillSwitchConfig(max_consecutive_losses=1, loss_streak_pause_h=500))
    )
    stops = (res.trades["exit_reason"] == "SL").sum()
    if stops:
        assert res.skips.get("kill_switch", 0) > 0  # after the first stop the book is paused for 500 h
        first_stop = res.trades[res.trades["exit_reason"] == "SL"]["exit_ts"].min()
        assert (res.trades["fill_ts"] <= first_stop + pd.Timedelta(minutes=1)).sum() >= 1


async def test_thin_bars_produce_no_fills_and_count_blocked_bars(markets) -> None:  # type: ignore[no-untyped-def]
    thin = {}
    for name, pm in markets.items():
        b = pm.bars
        bars = type(b)(b.product_id, b.ts_close, b.o, b.h, b.lo, b.c, np.full_like(b.v, 1e-9))
        thin[name] = type(pm)(pm.name, bars, pm.ind, pm.atr, pm.btc_ind, pm.btc_rows)
    res = await run(thin)
    assert res.trades.empty and res.metrics["haircut_blocked_bars"] > 0 and res.skips.get("no_fill", 0) > 0


async def test_the_span_limits_the_decisions(markets) -> None:  # type: ignore[no-untyped-def]
    narrow = Span(pd.Timestamp("2024-05-01", tz="UTC"), pd.Timestamp("2024-05-06", tz="UTC"), None, 1)
    res = await run(markets, span=narrow)
    if len(res.trades):
        assert res.trades["decision_ts"].min() >= pd.Timestamp("2024-05-01", tz="UTC")
        assert res.trades["decision_ts"].max() < pd.Timestamp("2024-05-06", tz="UTC")
    assert set(res.trades["fold"]) <= {1}


async def test_data_limit_keeps_every_outcome_inside_the_limit(markets) -> None:  # type: ignore[no-untyped-def]
    limit = pd.Timestamp("2024-05-12", tz="UTC")
    res = await run(markets, span=Span(SPAN.entry_start, SPAN.entry_end, limit, 4))
    assert len(res.trades) > 0 and (res.trades["exit_ts"] <= limit).all()


# ----------------------------------------------------------------------------------- regime (P1c)


class ExtremeRegimeStub(StubDecider):
    """The model's own regime head says "extreme" everywhere: it must not block anything, it only goes to the logs."""

    def answers_batch(self, x: np.ndarray) -> AnswerArrays:
        a = super().answers_batch(x)
        return AnswerArrays(a.p_action, a.p_edge, np.tile([0.0, 0.0, 0.0, 0.0, 1.0], (len(x), 1)))


async def test_extreme_model_regime_does_not_block_entries(markets) -> None:  # type: ignore[no-untyped-def]
    res = await run(markets, ExtremeRegimeStub())
    assert len(res.trades) > 3, res.skips  # no PolicyContractError, no regime veto from the head
    assert "regime_veto" not in res.skips


async def test_no_entry_without_the_regime_token(markets) -> None:  # type: ignore[no-untyped-def]
    """Before 104 daily bars the token is missing: the policy says no entry, whatever the model answers."""
    early = Span(pd.Timestamp("2024-02-01", tz="UTC"), pd.Timestamp("2024-02-20", tz="UTC"), None, 0)
    res = await run(markets, span=early)
    assert res.trades.empty


def test_regime_is_causal() -> None:
    """The regime of row i, computed on bars[:i+1], equals the one computed on all the bars (C7)."""
    bars = synthetic_bars(days=150, seed=77, zero_volume_day=None, constant_days=None)
    full = compute_indicators(bars)["atr_1d_pctile_365d"].to_numpy()
    token = lambda x: regimes_from_pctile(np.clip(np.rint(x), 0, 100))  # noqa: E731
    checked = 0
    for cut in (112 * 1440, 125 * 1440 + 777, 140 * 1440 + 31):
        part = compute_indicators(bars.iloc[:cut])["atr_1d_pctile_365d"].to_numpy()
        np.testing.assert_array_equal(token(part), token(full[:cut]))
        checked += int((token(part) >= 0).sum())
    assert checked >= 200
