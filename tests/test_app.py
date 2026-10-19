"""modules/app.py: the dry-run orchestrator against fakes (spec sez. 4.3 punto 12-13, 5.1, 5.3 punto 15).

What these tests pin: no real order can leave the process (live refused, read-only exchange, shadow gateway only);
the model rules (untunable policy = no entries, sentiment only when the model was trained with it); the guards are
hooked to the real flow (stale bar, last ticker, decider health, account guards); a task never dies in silence and a
critical one stops the process with an exit code instead of raising; the shutdown is ordered; the secrets never reach
the logs; the container health check really reads the state the app writes.
"""

from __future__ import annotations

import ast
import asyncio
import json
import signal
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import structlog
import yaml

import modules.app as app_module
from modules.app import (
    HEARTBEAT_FILE,
    Champion,
    Deps,
    Lifecycle,
    LiveModeUnavailable,
    RatioWindow,
    Runtime,
    StartupError,
    amain,
    check_key_permissions,
    collect_secrets,
    find_model_dir,
    install_signal_handlers,
    load_champion,
    main,
    no_entry_policy,
    redact,
    write_json_atomic,
)
from modules.config import AppConfig, CoinbaseConfig, KillSwitchConfig, TelemetryConfig
from modules.decision.policy import ENTRY_REGIMES
from modules.decision.protocol import REGIME_LEGEND
from modules.execution.balance import CoverageAlarm
from modules.execution.exchange import DryRunViolation, KeyPermissionsInfo
from modules.execution.gateway import EntryResult, ExitResult
from modules.execution.spot import SPOT_EXIT_CODE, SpotViolation
from modules.ledger.positions import EntryPlan, FillRecord, OrderRecord, PositionLedger
from modules.risk.kill_switch import AccountGuards, KillSwitch, SystemGuards
from tests.app_fixtures import (
    PRODUCTS,
    VIEW_KEY,
    FakeClock,
    FakeExchange,
    FakeSentiment,
    HoldAll,
    make_cfg,
    make_champion,
    make_rig,
    raw_product,
    tiny_history,
    until,
)
from tests.label_fixtures import loaded_label
from tests.test_decision_engine import WARM_A, frame

D = Decimal
ROOT = Path(__file__).resolve().parent.parent
APP_PY = ROOT / "modules" / "app.py"
LIVE_ENV = {"CHRONOS_LIVE_CONFIRM": "I_ACCEPT_REAL_ORDERS"}


# ============================================================================== 1. no real order can leave


def test_live_is_refused_and_nothing_is_built(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, mode="live")
    built: list[str] = []

    def exchange(c: CoinbaseConfig) -> FakeExchange:
        built.append("exchange")
        return FakeExchange()

    with pytest.raises(LiveModeUnavailable) as err:
        asyncio.run(Runtime.start(cfg, LIVE_ENV, Deps(exchange=exchange)))
    assert err.value.exit_code == 2
    assert built == []  # not even the exchange object exists
    assert not (tmp_path / "state").exists() and not (tmp_path / "data").exists()  # no ledger, no heartbeat


def test_amain_returns_the_config_exit_code_for_live(tmp_path: Path) -> None:
    code = asyncio.run(amain(make_cfg(tmp_path, mode="live"), LIVE_ENV, Deps(exchange=lambda c: FakeExchange())))
    assert code == 2


async def test_live_in_the_config_without_the_env_runs_dry(tmp_path: Path) -> None:
    async with make_rig(tmp_path, cfg=make_cfg(tmp_path, mode="live"), upto=None) as rig:
        assert rig.rt.mode == "dry"
    async with make_rig(
        tmp_path, cfg=make_cfg(tmp_path, mode="live"), upto=None, env={"CHRONOS_LIVE_CONFIRM": "yes"}
    ) as rig:
        assert rig.rt.mode == "dry"  # a wrong confirmation value is not a confirmation


def test_the_source_has_no_way_to_send_an_order() -> None:
    """AST scan of app.py: the live gateway, order calls and ticket objects are not even named."""
    tree = ast.parse(APP_PY.read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    forbidden = {
        "LiveExecutionGateway", "LiveBalanceFeed", "create_order", "cancel_orders", "preview_order", "build_order_body",
        "CoverageTicket", "RESTClient",
    }  # fmt: skip
    assert not forbidden & names, sorted(forbidden & names)
    modules = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    modules |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not {m for m in modules if m.split(".")[0] == "coinbase"}  # the SDK is imported by exchange.py only
    gateways = {n for n in names if n.endswith("ExecutionGateway")}
    assert gateways == {"ShadowExecutionGateway", "ExecutionGateway"}  # the shadow class and the protocol, nothing else
    for node in ast.walk(tree):  # the single exchange of the process is built read-only, by a literal
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "build_exchange":
            assert {k.arg: ast.unparse(k.value) for k in node.keywords}["read_only"] == "True"


def test_the_default_exchange_is_read_only_and_needs_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(app_module, "build_exchange", lambda cfg, **kw: seen.append(kw) or object())
    with pytest.raises(StartupError, match="View key"):
        app_module.default_exchange(CoinbaseConfig())
    keyed = CoinbaseConfig(api_key="organizations/x/apiKeys/y", api_secret="secret-value-123")  # type: ignore[arg-type]
    app_module.default_exchange(keyed)
    assert seen == [{"read_only": True}]


@pytest.mark.parametrize(
    ("perms", "code"),
    [
        (KeyPermissionsInfo(True, False, True, "DEFAULT"), SPOT_EXIT_CODE),  # can transfer: refused
        (KeyPermissionsInfo(True, True, True, "DEFAULT"), SPOT_EXIT_CODE),
    ],
)
def test_a_key_that_can_move_funds_stops_the_start(tmp_path: Path, perms: KeyPermissionsInfo, code: int) -> None:
    deps = Deps(exchange=lambda c: FakeExchange(perms), champion=lambda m, s: make_champion())
    assert asyncio.run(amain(make_cfg(tmp_path), {}, deps)) == code


def test_a_trading_key_is_allowed_in_dry_but_flagged_and_live_needs_one() -> None:
    with structlog.testing.capture_logs() as logs:
        check_key_permissions("dry", KeyPermissionsInfo(True, True, False, "DEFAULT"))
    assert any(e["event"] == "dry_run_key_can_trade" for e in logs)
    check_key_permissions("dry", VIEW_KEY)
    with pytest.raises(SpotViolation, match="cannot trade"):
        check_key_permissions("live", VIEW_KEY)  # the live branch exists and vets the key (unreachable today)
    with pytest.raises(SpotViolation, match="transfer"):
        check_key_permissions("live", KeyPermissionsInfo(True, True, True, "DEFAULT"))


def test_a_non_spot_product_stops_the_start_with_the_spot_exit_code(tmp_path: Path) -> None:
    raws = {p: {**raw_product(p), "product_type": "FUTURE"} for p in ("BTC-EUR", "SOL-EUR")}
    deps = Deps(exchange=lambda c: FakeExchange(raws=raws), champion=lambda m, s: make_champion())
    assert asyncio.run(amain(make_cfg(tmp_path), {}, deps)) == SPOT_EXIT_CODE


# ================================================================================================ 2. the model


def _write_model(root: Path, name: str, *, policy: dict[str, Any] | None, sentiment: Any = False) -> Path:
    d = root / "baseline" / name
    (d / "final").mkdir(parents=True)
    label = loaded_label({"BTC-EUR": 3.0, "SOL-EUR": 3.0})
    raw = {
        "atr_tf": label.cfg.atr_tf, "k_tp": label.cfg.k_tp, "k_sl": label.cfg.k_sl, "entry_mode": label.cfg.entry_mode,
        "horizon_min": label.cfg.horizon_min, "entry_timeout_min": label.cfg.entry_timeout_min,
        "entry_first_bar": label.cfg.entry_first_bar,
        "fees_bps": {"maker": 25.0, "taker": 50.0, "source": "probe"}, "slip_bps": 5.0, "stop_extra_slip_bps": 10.0,
        "spread_bps": label.spread_bps, "selection": {"holdout_start": label.holdout_start.isoformat()},
        "sha256": label.sha256,
    }  # fmt: skip
    (d / "label_config.json").write_text(json.dumps(raw), encoding="utf-8")
    card = {"model": name, "sentiment_available": sentiment}
    (d / "model_card.json").write_text(json.dumps(card), encoding="utf-8")
    if policy is not None:
        (d / "final" / "policy.json").write_text(json.dumps(policy), encoding="utf-8")
    return d


@pytest.fixture
def stub_booster(monkeypatch: pytest.MonkeyPatch) -> None:
    """The LightGBM files are not needed to test what the app does with the card and the policy."""
    monkeypatch.setattr(app_module.BaselineDecider, "load", classmethod(lambda cls, *a, **k: object()))
    monkeypatch.setattr(app_module, "load_label_config", lambda p: loaded_label({"BTC-EUR": 3.0, "SOL-EUR": 3.0}))


def test_the_newest_baseline_version_is_the_champion(tmp_path: Path) -> None:
    for name in ("baseline-lgbm@2026-10-18.1", "baseline-lgbm@2026-10-18.10", "baseline-lgbm@2026-10-18.2"):
        _write_model(tmp_path, name, policy={"untunable": True})
    assert find_model_dir(tmp_path, "registry:champion").name == "baseline-lgbm@2026-10-18.10"
    assert find_model_dir(tmp_path, "baseline-lgbm@2026-10-18.2").name == "baseline-lgbm@2026-10-18.2"
    with pytest.raises(StartupError):
        find_model_dir(tmp_path, "baseline-lgbm@nope")
    with pytest.raises(StartupError, match="no model"):
        find_model_dir(tmp_path / "empty", "registry:champion")


def test_an_untunable_policy_loads_as_no_entries(tmp_path: Path, stub_booster: None) -> None:
    _write_model(tmp_path, "baseline-lgbm@2026-10-18.2", policy={"untunable": True, "reason": "untunable_ev"})
    champion = load_champion(tmp_path, "registry:champion")
    assert champion.no_entry_reason == "untunable_ev"
    assert champion.policy == no_entry_policy(champion.version, champion.label.sha256)
    assert (champion.policy.tau_buy, champion.policy.tau_sell, champion.policy.c_min) == (1.0, 1.0, 1.0)


def test_a_tunable_policy_is_loaded_and_a_foreign_or_broken_one_refuses_to_start(
    tmp_path: Path, stub_booster: None
) -> None:
    good = {
        "model": "m", "tau_buy": 0.6, "tau_sell": 0.7, "c_min": 0.1, "p_be_buffer": 0.05,
        "regime_rule": "ATR1D_PCTILE365:5/35/65/95",
        "label_config_sha256": loaded_label({"BTC-EUR": 3.0, "SOL-EUR": 3.0}).sha256,
    }  # fmt: skip
    _write_model(tmp_path, "baseline-lgbm@2026-10-18.1", policy=good)
    champion = load_champion(tmp_path, "baseline-lgbm@2026-10-18.1")
    assert champion.no_entry_reason is None and champion.policy.tau_buy == 0.6
    _write_model(tmp_path, "baseline-lgbm@2026-10-18.2", policy={**good, "label_config_sha256": "0" * 64})
    with pytest.raises(StartupError, match="label_config_sha256"):
        load_champion(tmp_path, "baseline-lgbm@2026-10-18.2")
    _write_model(tmp_path, "baseline-lgbm@2026-10-18.3", policy={**good, "tau_buy": 1.5})
    with pytest.raises(StartupError):
        load_champion(tmp_path, "baseline-lgbm@2026-10-18.3")
    _write_model(tmp_path, "baseline-lgbm@2026-10-18.4", policy=None)
    with pytest.raises(StartupError, match="cannot be loaded"):
        load_champion(tmp_path, "baseline-lgbm@2026-10-18.4")


@pytest.mark.parametrize(
    ("flag", "expected"), [(False, False), ("true", False), (1, False), (None, False), (True, True)]
)
def test_sentiment_is_available_only_when_the_card_says_true(
    tmp_path: Path, stub_booster: None, flag: Any, expected: bool
) -> None:
    _write_model(tmp_path, "baseline-lgbm@2026-10-18.1", policy={"untunable": True}, sentiment=flag)
    assert load_champion(tmp_path, "baseline-lgbm@2026-10-18.1").sentiment_available is expected


def test_the_real_model_directory_loads_when_it_is_there() -> None:
    models = ROOT / "models"
    if not (models / "baseline").is_dir() or not any((models / "baseline").glob("*@*/model_card.json")):
        pytest.skip("no trained baseline in models/")
    champion = load_champion(models, "registry:champion")
    assert champion.sentiment_available is False
    assert champion.policy.label_config_sha256 == champion.label.sha256


# ============================================================================== 3. start, run, stop


async def test_start_and_stop_are_clean(tmp_path: Path) -> None:
    async with make_rig(tmp_path, upto=None) as rig:
        hb = tmp_path / "data" / HEARTBEAT_FILE
        assert json.loads(hb.read_text(encoding="utf-8"))["ok"] is False  # before run(): "starting", never healthy
        rig.start()
        await until(lambda: json.loads(hb.read_text(encoding="utf-8")).get("phase") == "running")
        assert await rig.stop() == 0
        final = json.loads(hb.read_text(encoding="utf-8"))
        assert final["phase"] == "stopped" and final["ok"] is False
        assert list(rig.rt.life.failures) == [] and rig.rt.life.exit_code == 0
        assert rig.rt.ledger._db is not None  # noqa: SLF001
        with pytest.raises(ValueError):  # the aiosqlite connection is closed
            await rig.rt.ledger.connection.execute("SELECT 1")


async def test_the_tasks_of_the_group_are_the_documented_ones(tmp_path: Path) -> None:
    async with make_rig(tmp_path, upto=None) as rig:
        names = {s.name for s in rig.rt.task_specs()}
        assert names == {
            "market_stream", "bar_service", "bar_pump", "tickers", "health", "heartbeat", "fees",
            "watchdog:champion", "telemetry_status", "telemetry_equity", "telemetry_ops",
        }  # fmt: skip
        crit = {s.name for s in rig.rt.task_specs() if not s.critical}
        assert crit == {"fees", "telemetry_status", "telemetry_equity", "telemetry_ops"}  # telemetry never stops us


async def test_telemetry_disabled_starts_no_telemetry_task_and_trading_is_the_same(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, telemetry=TelemetryConfig(enabled=False, dir=str(tmp_path / "telemetry")))
    async with make_rig(tmp_path, cfg=cfg, upto=None) as rig:
        names = {s.name for s in rig.rt.task_specs()}
        assert not {n for n in names if n.startswith("telemetry")} and rig.rt.events is None and rig.rt.ops is None
        rig.start()
        assert await rig.stop() == 0
    assert not (tmp_path / "telemetry").exists() and not (tmp_path / "data" / "events").exists()


async def test_only_the_configured_books_start(tmp_path: Path) -> None:
    async with make_rig(tmp_path, upto=None) as rig:
        assert [b.name for b in rig.rt.books] == ["champion"]
        assert set(rig.rt.kill_switch._accounts) == {"champion"}  # noqa: SLF001


async def test_bars_reach_the_engine_and_the_venue_in_order_and_the_record_is_written(tmp_path: Path) -> None:
    async with make_rig(tmp_path, champion=make_champion(inner=HoldAll())) as rig:
        seen: list[tuple[str, float]] = []
        real = rig.book.shadow.on_bar

        async def spy(bar: Any) -> list[ExitResult]:
            seen.append((bar.product_id, bar.spread_bps))
            return await real(bar)

        rig.book.shadow.on_bar = spy
        rig.start()
        rig.feed._spreads["SOL-EUR"] = None  # noqa: SLF001
        rig.publish("BTC-EUR", WARM_A)
        rig.publish("SOL-EUR", WARM_A)
        rig.feed._spreads["SOL-EUR"] = None  # noqa: SLF001
        await until(lambda: rig.book.engine.stats.bars == 2)
        await rig.book.engine.drain()
        assert [p for p, _ in seen] == ["BTC-EUR", "SOL-EUR"]
        assert seen[0][1] == 3.0  # the live spread of the book
        assert seen[1][1] == 3.0  # no live spread: the label spread of the model
        assert {d["product"] for d in rig.decisions()} == {"BTC-EUR", "SOL-EUR"}
        assert rig.rt.marks["BTC-EUR"] > 0
        assert await rig.stop() == 0


async def test_the_bar_service_continues_exactly_after_the_warmed_history(tmp_path: Path) -> None:
    """The finalizer is seeded and the store writer starts from the last warmed bar, and so does the engine."""
    async with make_rig(tmp_path) as rig:
        for product in rig.cfg.market.products:
            last = frame(product).index[WARM_A - 1].to_pydatetime()
            assert rig.bars.finalizer.next_minute(product) == last + timedelta(minutes=1)
            assert rig.bars.writer.complete_until(product) == last
            assert rig.book.engine.indicators[product].snapshot().ts == last  # warmed on the whole history
            assert rig.rt.marks[product] == D(repr(float(frame(product).close.iloc[WARM_A - 1])))
        # the first bar after the history is consumed, the last bar of the history again would be a duplicate
        rig.start()
        rig.publish("BTC-EUR", WARM_A - 1)
        await until(lambda: rig.book.engine.stats.out_of_order == 1)
        assert await rig.stop() == 0


# =========================================================================== 4. entries: untunable policy


async def _entry_flow(tmp_path: Path, champion: Champion) -> tuple[Any, list[dict[str, Any]]]:
    async with make_rig(tmp_path, champion=champion) as rig:
        rig.start()
        rig.publish("BTC-EUR", WARM_A)
        await until(lambda: rig.book.engine.stats.decisions == 1)
        await asyncio.sleep(0.05)
        pending = rig.book.shadow.has_pending_entry("BTC-EUR")
        records = rig.decisions()
        assert await rig.stop() == 0
        return pending, records


async def test_a_tunable_policy_lets_the_same_decider_enter(tmp_path: Path) -> None:
    """The control of the next test: with a real policy the all-in decider reaches the venue."""
    pending, records = await _entry_flow(tmp_path, make_champion())
    assert records[0]["policy"]["final"] == "BUY" and records[0]["risk"]["outcome"] == "intent"
    assert pending is True


async def test_an_untunable_policy_means_no_entry_and_no_error(tmp_path: Path) -> None:
    champion = make_champion(untunable="untunable_ev")
    pending, records = await _entry_flow(tmp_path, champion)
    policy = records[0]["policy"]
    assert policy["final"] == "BUY"  # the placeholder thresholds are met by a one-hot answer: the gate must stop it
    assert records[0]["risk"]["outcome"] == "skip"
    assert records[0]["risk"]["reason"] == "kill_switch"
    assert records[0]["risk"]["detail"] == "policy_untunable:untunable_ev"
    assert pending is False
    assert all("error" not in r for r in records)


def test_the_no_entry_policy_never_buys_a_normal_answer() -> None:
    from modules.decision.policy import DecisionPolicy
    from modules.decision.protocol import DecisionAnswers

    policy = DecisionPolicy(no_entry_policy("m", "0" * 64))
    answers = DecisionAnswers.model_validate(
        {
            "action": {"choice": "BUY", "probabilities": {"BUY": 0.9, "SELL": 0.01, "HOLD": 0.09}, "confidence": 0.0},
            "edge": {"noul": 0.9},
            "vol_regime": {
                "score": 2.0, "legend": {str(k): n for k, n in enumerate(REGIME_LEGEND)},
                "probabilities": {"very_low": 0.0, "low": 0.0, "normal": 1.0, "high": 0.0, "extreme": 0.0},
                "confidence": 0.0,
            },
        }
    )  # fmt: skip
    assert policy.resolve(answers, "FLAT", vol_pctile=50.0).action == "HOLD"
    assert ENTRY_REGIMES  # the veto of the regime is still the policy's, not this wiring's


# ============================================================================================= 5. sentiment


async def test_sentiment_is_not_started_nor_given_to_the_engine_when_the_model_was_trained_without_it(
    tmp_path: Path,
) -> None:
    sentiment = FakeSentiment()
    async with make_rig(tmp_path, champion=make_champion(sentiment=False, inner=HoldAll()), sentiment=sentiment) as rig:
        assert rig.sentiment_factory_calls == [] and rig.rt.sentiment is None
        assert "sentiment" not in {s.name for s in rig.rt.task_specs()}
        rig.start()
        rig.publish("BTC-EUR", WARM_A)
        await until(lambda: rig.book.engine.stats.bars == 1)
        await rig.book.engine.drain()
        assert sentiment.calls == [] and not sentiment.ran
        assert await rig.stop() == 0


async def test_sentiment_reaches_the_engine_when_the_model_uses_it(tmp_path: Path) -> None:
    sentiment = FakeSentiment()
    async with make_rig(tmp_path, champion=make_champion(sentiment=True, inner=HoldAll()), sentiment=sentiment) as rig:
        assert rig.sentiment_factory_calls == [1] and rig.rt.sentiment is sentiment
        spec = next(s for s in rig.rt.task_specs() if s.name == "sentiment")
        assert spec.critical is False
        rig.start()
        rig.publish("BTC-EUR", WARM_A)
        await until(lambda: len(sentiment.calls) == 1)
        await until(lambda: sentiment.ran)
        product, as_of = sentiment.calls[0]
        assert product == "BTC-EUR" and as_of == rig.event("BTC-EUR", WARM_A).t  # read as of the logical instant t
        assert await rig.stop() == 0


# =================================================================================================== 6. guards


async def test_the_stale_bar_guard_is_fed_by_every_published_bar(tmp_path: Path) -> None:
    async with make_rig(tmp_path, champion=make_champion(inner=HoldAll())) as rig:
        rig.start()
        ev = rig.publish("BTC-EUR", WARM_A)
        await until(lambda: rig.rt.system.bar_lag_s("BTC-EUR") is not None)
        assert rig.rt.system.bar_lag_s("BTC-EUR") == 120.0 and rig.rt.last_bar_t["BTC-EUR"] == ev.t
        assert rig.rt.kill_switch.entry_blocked("champion") is None
        stale = rig.rt.cfg.risk.kill_switch.stale_bar_s
        rig.clock.now = ev.t + timedelta(seconds=stale + 1)
        assert rig.rt.kill_switch.entry_blocked("champion") == "stale_bar:BTC-EUR"
        assert await rig.stop() == 0


async def test_the_stale_data_guard_is_fed_by_the_tickers_and_armed_at_start(tmp_path: Path) -> None:
    async with make_rig(tmp_path, champion=make_champion(inner=HoldAll())) as rig:
        t0 = rig.clock.now
        stale = rig.rt.cfg.risk.kill_switch.stale_data_s
        rig.clock.now = t0 + timedelta(seconds=stale - 1)
        assert rig.rt.kill_switch.entry_blocked("champion") is None
        rig.clock.now = t0 + timedelta(seconds=stale + 1)
        # no ticker ever arrived: the observation made at start now counts as stale (a feed that never delivers)
        assert str(rig.rt.kill_switch.entry_blocked("champion")).startswith("stale_data:")
        rig.start()
        rig.feed.quote_around("BTC-EUR", 100.0, rig.clock.now)
        rig.feed.quote_around("SOL-EUR", 100.0, rig.clock.now)
        await until(lambda: rig.rt.kill_switch.entry_blocked("champion") is None)
        assert await rig.stop() == 0


async def test_the_watchdog_receives_the_tickers(tmp_path: Path) -> None:
    async with make_rig(tmp_path) as rig:
        seen: list[str] = []

        async def on_quote(quote: Any) -> None:
            seen.append(quote.product_id)

        rig.book.watchdog.on_quote = on_quote
        rig.start()
        rig.feed.quote_around("BTC-EUR", 100.0, rig.clock.now)
        await until(lambda: seen == ["BTC-EUR"])
        assert await rig.stop() == 0


def _fresh_tickers(rig: Any) -> None:
    for p in rig.rt.cfg.market.products:
        rig.rt.system.observe_ticker(p, rig.clock.now)


async def test_decider_health_trips_above_the_limit_and_clears_when_it_recovers(tmp_path: Path) -> None:
    async with make_rig(tmp_path, upto=None) as rig:
        st = rig.book.engine.stats
        assert rig.rt.cfg.risk.kill_switch.max_fallback_ratio_1h == 0.20
        rig.rt.evaluate_decider_health()  # the baseline sample of the window
        st.decisions, st.unavailable = 5, 5  # 100%, but fewer than MIN_DECISIONS_FOR_HEALTH: it says nothing
        rig.clock.now += timedelta(minutes=1)
        _fresh_tickers(rig)
        rig.rt.evaluate_decider_health()
        assert rig.rt.kill_switch.entry_blocked("champion") is None
        st.decisions, st.unavailable = 100, 21  # 21% > 20%
        rig.clock.now += timedelta(minutes=1)
        _fresh_tickers(rig)
        assert rig.rt.evaluate_decider_health() == 0.21
        assert rig.rt.kill_switch.entry_blocked("champion") == "decider_health"
        assert "decider" in rig.rt.life.faults
        st.decisions = 400  # the same 21 failures over 400 decisions: 5%
        rig.clock.now += timedelta(minutes=1)
        _fresh_tickers(rig)
        rig.rt.evaluate_decider_health()
        assert rig.rt.kill_switch.entry_blocked("champion") is None


async def test_a_decider_health_latch_of_the_previous_run_is_kept_until_a_healthy_window(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    ledger = await PositionLedger.open(cfg.dry_run.db_path, "dry")
    clock = FakeClock(datetime(2024, 1, 4, tzinfo=UTC))
    system = SystemGuards(cfg.risk.kill_switch, clock)
    system.set_decider_health(False, reason="task:bar_service: boom", at=clock.now)
    guards = AccountGuards("champion", cfg.risk.kill_switch, cfg.risk, clock)
    await KillSwitch(system, {"champion": guards}, clock).save(ledger.connection)
    await ledger.close()
    async with make_rig(tmp_path, cfg=cfg, upto=None) as rig:
        _fresh_tickers(rig)
        assert "decider" in rig.rt.life.faults
        assert rig.rt.kill_switch.entry_blocked("champion") == "decider_health"
        rig.rt.evaluate_decider_health()  # no decisions yet: nothing to judge, the latch stays
        assert rig.rt.kill_switch.entry_blocked("champion") == "decider_health"
        rig.book.engine.stats.decisions = 40  # a healthy window of this run
        rig.clock.now += timedelta(minutes=1)
        _fresh_tickers(rig)
        rig.rt.evaluate_decider_health()
        assert rig.rt.kill_switch.entry_blocked("champion") is None


async def test_fallbacks_and_pipeline_errors_count_as_decider_failures(tmp_path: Path) -> None:
    async with make_rig(tmp_path, upto=None) as rig:
        st = rig.book.engine.stats
        rig.rt.evaluate_decider_health()  # the baseline sample of the window
        rig.clock.now += timedelta(minutes=1)
        _fresh_tickers(rig)
        st.decisions, st.fallbacks = 100, 20  # exactly 20%: not above the limit
        assert rig.rt.evaluate_decider_health() == 0.2
        assert rig.rt.kill_switch.entry_blocked("champion") is None
        rig.clock.now += timedelta(minutes=1)
        _fresh_tickers(rig)
        st.errors = 30  # errors enter numerator and denominator: 50 / 130
        rig.rt.evaluate_decider_health()
        assert rig.rt.kill_switch.entry_blocked("champion") == "decider_health"


def test_ratio_window_is_a_trailing_window_over_cumulative_counters() -> None:
    w = RatioWindow(3600)
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    assert w.update(t0, 0, 0) == (None, 0)
    assert w.update(t0 + timedelta(minutes=30), 100, 10) == (0.10, 100)
    assert w.update(t0 + timedelta(minutes=59), 100, 10) == (0.10, 100)
    # the sample at t0 leaves the window: the baseline becomes the 30 minute one (100, 10)
    assert w.update(t0 + timedelta(minutes=91), 150, 40) == (0.60, 50)
    assert w.update(t0 + timedelta(minutes=92), 150, 40)[0] == 0.60
    # a long quiet hour: nothing happened inside the window
    assert w.update(t0 + timedelta(hours=5), 150, 40) == (None, 0)


_AT = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


async def _close_position(rig: Any, n: int, *, leg: str, price: str, hint: str | None = None) -> None:
    """Open and close one position of the champion in the REAL ledger (the guards hear it through the listener)."""
    ledger, at = rig.rt.ledger, _AT + timedelta(hours=n)
    plan = EntryPlan(D("120"), D("95"), 3.0, 1.5, D("5"))
    await ledger.apply_entry_fill_recorded(
        FillRecord(f"b{n}", "champion", f"e{n}", "BTC-EUR", "BUY", D("100"), D("1"), D("0.1"), "maker", at), "d", plan
    )
    await ledger.record_order(OrderRecord(f"x{n}", "champion", "BTC-EUR", "SELL", leg, "OPEN", at, at))
    if hint is not None:
        ledger.expect_exit("champion", "BTC-EUR", hint)
    sell = FillRecord(f"s{n}", "champion", f"x{n}", "BTC-EUR", "SELL", D(price), D("1"), D("0.1"), "taker", at)
    await ledger.apply_exit_fill_recorded(sell)


async def test_the_account_guards_see_closed_trades_through_the_ledger(tmp_path: Path) -> None:
    async with make_rig(tmp_path, upto=None) as rig:
        for n in range(rig.rt.cfg.risk.kill_switch.max_consecutive_losses):
            assert rig.rt.kill_switch.entry_blocked("champion") is None
            await _close_position(rig, n, leg="bracket", price="94")  # below the stop level 95: a stop out
        assert rig.rt.kill_switch.entry_blocked("champion") == "loss_streak"
        assert rig.book.gateway is not None
        trades = await rig.rt.ledger.closed_trades("champion")
        assert [t.exit_reason for t in trades] == ["SL"] * len(trades) and all(t.net_quote < 0 for t in trades)


async def test_a_model_exit_is_not_a_stop_for_the_loss_streak(tmp_path: Path) -> None:
    async with make_rig(tmp_path, upto=None) as rig:
        n = rig.rt.cfg.risk.kill_switch.max_consecutive_losses
        for i, hint in enumerate(["SELL", "HORIZON", "MANUAL"] * n):
            await _close_position(rig, i, leg="exit", price="90", hint=hint)  # all at a loss, none a stop
        assert rig.rt.kill_switch.entry_blocked("champion") is None
        reasons = {t.exit_reason for t in await rig.rt.ledger.closed_trades("champion")}
        assert reasons == {"SELL", "HORIZON", "MANUAL"}


async def test_the_equity_is_observed_with_every_bar(tmp_path: Path) -> None:
    async with make_rig(tmp_path, champion=make_champion(inner=HoldAll())) as rig:
        rig.start()
        rig.publish("BTC-EUR", WARM_A)
        rows: list[str] = []

        def seen() -> bool:
            rows[:] = [r.guard for r in rig.book.guards.state_rows()]
            return "daily_open" in rows

        await until(seen)
        assert await rig.stop() == 0


async def test_coverage_alarms_of_the_virtual_book_trip_the_coverage_breach(tmp_path: Path) -> None:
    async with make_rig(tmp_path, upto=None) as rig:
        assert not rig.rt.kill_switch.coverage_breach_active("champion")
        alarm = CoverageAlarm(kind="reservation_overdraw", currency="EUR", detail="x")
        rig.book.shadow._booking._alarm(alarm)  # noqa: SLF001  (what LedgerBooking calls on a coverage alarm)
        assert rig.rt.kill_switch.coverage_breach_active("champion")
        assert rig.rt.kill_switch.entry_blocked("champion") == "coverage_breach"


# ===================================================================================== 7. failures and codes


async def test_a_critical_task_that_dies_stops_the_process_without_raising(tmp_path: Path) -> None:
    async with make_rig(tmp_path, upto=None) as rig:
        rig.bars.fail = RuntimeError("bar service exploded")
        rig.start()
        code = await asyncio.wait_for(rig.task, 20)  # type: ignore[arg-type]
        assert code == 1
        (failure,) = rig.rt.life.failures
        assert failure["task"] == "bar_service" and "exploded" in failure["error"]
        assert rig.rt.kill_switch.entry_blocked("champion") == "decider_health"
        assert "task:bar_service" in rig.rt.life.faults


async def test_a_non_critical_task_that_dies_is_a_fault_but_not_a_stop(tmp_path: Path) -> None:
    async with make_rig(tmp_path, upto=None) as rig:

        async def broken() -> None:
            raise OSError("disk gone")

        rig.rt.fees.refresh = broken  # type: ignore[method-assign,assignment]
        rig.start()
        await until(lambda: "task:fees" in rig.rt.life.faults)
        assert rig.task is not None and not rig.task.done()
        assert rig.rt.kill_switch.entry_blocked("champion") == "decider_health"  # every entry blocked, process alive
        assert await rig.stop() == 0  # a non-critical failure alone does not change the exit code
        assert rig.rt.life.failures[0]["task"] == "fees"


async def test_a_task_that_returns_is_a_failure(tmp_path: Path) -> None:
    async with make_rig(tmp_path, upto=None) as rig:

        async def done() -> None:
            return None

        rig.rt.extra_tasks.append(app_module.TaskSpec("telemetry", done, critical=False))
        rig.start()
        await until(lambda: "task:telemetry" in rig.rt.life.faults)
        assert "returned" in rig.rt.life.failures[0]["error"]
        assert await rig.stop() == 0


@pytest.mark.parametrize(
    ("exc", "code"),
    [(SpotViolation("x"), 4), (DryRunViolation("x"), 3), (RuntimeError("x"), 1)],
)
async def test_fatal_codes(tmp_path: Path, exc: BaseException, code: int) -> None:
    async with make_rig(tmp_path, upto=None) as rig:
        rig.start()
        rig.rt.life.fatal(exc)
        assert await asyncio.wait_for(rig.task, 20) == code  # type: ignore[arg-type]


def test_a_violation_outranks_a_generic_failure() -> None:
    life = Lifecycle(SystemGuards(KillSwitchConfig()), lambda: datetime.now(UTC))
    life.fatal(RuntimeError("a"))
    assert life.exit_code == 1
    life.fatal(DryRunViolation("b"))
    assert life.exit_code == 3
    life.fatal(RuntimeError("c"))
    assert life.exit_code == 3
    life.fatal(SpotViolation("d"))
    assert life.exit_code == 4


async def test_a_spot_violation_inside_the_gateway_is_fatal_even_though_the_engine_swallows_it(tmp_path: Path) -> None:
    async with make_rig(tmp_path, upto=None) as rig:

        async def boom(intent: Any) -> EntryResult:
            raise SpotViolation("not a spot order")

        rig.book.shadow.submit_entry_with_bracket = boom  # type: ignore[method-assign]
        rig.start()
        with pytest.raises(SpotViolation):
            await rig.book.gateway.submit_entry_with_bracket(object())  # type: ignore[arg-type]
        assert await asyncio.wait_for(rig.task, 20) == SPOT_EXIT_CODE  # type: ignore[arg-type]


async def test_a_dry_run_violation_in_an_exit_is_fatal(tmp_path: Path) -> None:
    async with make_rig(tmp_path, upto=None) as rig:

        async def boom(product: str) -> ExitResult:
            raise DryRunViolation("write on a read-only exchange")

        rig.book.shadow.emergency_exit = boom  # type: ignore[method-assign]
        rig.start()
        with pytest.raises(DryRunViolation):
            await rig.book.gateway.emergency_exit("BTC-EUR")
        assert await asyncio.wait_for(rig.task, 20) == 3  # type: ignore[arg-type]


def _start_deps(**over: Any) -> Deps:
    base: dict[str, Any] = {
        "exchange": lambda c: FakeExchange(), "champion": lambda m, s: make_champion(),
        "backfill": lambda c, s, k: asyncio.sleep(0),
    }  # fmt: skip
    return Deps(**{**base, **over})


def test_startup_failures_return_clean_codes(tmp_path: Path) -> None:
    def broken_history(root: Path, pid: str) -> Any:
        raise RuntimeError("parquet is corrupt")

    assert asyncio.run(amain(make_cfg(tmp_path), {}, _start_deps(history=broken_history))) == 1
    empty = _start_deps(history=lambda r, p: pd.DataFrame())
    assert asyncio.run(amain(make_cfg(tmp_path), {}, empty)) == 2  # no history: refuses to start


def test_a_start_that_fails_late_closes_what_it_opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[str] = []
    real = PositionLedger.close

    async def spy(self: PositionLedger) -> None:
        closed.append("ledger")
        await real(self)

    monkeypatch.setattr(PositionLedger, "close", spy)

    def stream(products: Any, tap: Any, on_damage: Any) -> Any:
        raise RuntimeError("cannot build the stream")

    history = {p: tiny_history(p) for p in PRODUCTS}
    deps = _start_deps(history=lambda r, p: history[p], stream=stream)
    assert asyncio.run(amain(make_cfg(tmp_path), {}, deps)) == 1
    assert closed == ["ledger"]  # opened before the failure, closed by the unwinding


# =============================================================================================== 8. shutdown


async def test_the_shutdown_closes_the_entry_gate_first_and_runs_every_step_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with make_rig(tmp_path, upto=None) as rig:
        order: list[str] = []
        rt = rig.rt

        def wrap(name: str, extra: Any = None) -> None:
            real = getattr(rt, name)

            async def step() -> None:
                order.append(name)
                if extra:
                    extra()
                await real()

            monkeypatch.setattr(rt, name, step)

        wrap("_cancel_entries", lambda: order.append(f"stopping={rt.life.stopping}"))
        wrap("_drain")
        wrap("_flush_bars")
        wrap("_save_kill_switch")
        real_close = rt.stack.aclose

        async def close() -> None:
            order.append("close_resources")
            await real_close()

        monkeypatch.setattr(rt.stack, "aclose", close)
        rig.start()
        await until(lambda: True)
        assert await rig.stop() == 0
        assert order[:3] == ["_cancel_entries", "stopping=True", "_drain"]
        assert order.index("_flush_bars") > order.index("_drain")
        assert order.index("_save_kill_switch") > order.index("_flush_bars")
        assert order[-1] == "close_resources"


async def test_a_failing_shutdown_step_does_not_skip_the_next_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with make_rig(tmp_path, upto=None) as rig:
        done: list[str] = []

        async def bad() -> None:
            raise OSError("disk full")

        async def good() -> None:
            done.append("flush")

        monkeypatch.setattr(rig.rt, "_cancel_entries", bad)
        monkeypatch.setattr(rig.rt, "_drain", bad)
        monkeypatch.setattr(rig.rt, "_flush_bars", good)
        rig.start()
        assert await rig.stop() == 0
        assert done == ["flush"]  # the bar store is flushed whatever happened before


async def test_the_shutdown_cancels_a_pending_entry_and_releases_its_reservation_but_not_the_position(
    tmp_path: Path,
) -> None:
    async with make_rig(tmp_path) as rig:
        rig.start()
        rig.publish("BTC-EUR", WARM_A)
        await until(lambda: rig.book.shadow.has_pending_entry("BTC-EUR"))
        assert rig.book.account.balances.open_reservations()
        assert await rig.stop() == 0
        assert rig.book.account.balances.open_reservations() == []  # the simulated entry was cancelled and released
        assert not rig.book.shadow.has_pending_entry("BTC-EUR")


async def test_entries_are_refused_as_soon_as_the_stop_is_requested(tmp_path: Path) -> None:
    async with make_rig(tmp_path) as rig:
        assert rig.rt.kill_switch.entry_blocked("champion") is None
        rig.start()
        rig.rt.request_stop("test")
        assert rig.book.risk._gate.entry_blocked("champion") == "shutting_down"  # noqa: SLF001
        await asyncio.wait_for(rig.task, 20)  # type: ignore[arg-type]


async def test_the_dry_ledger_and_decisions_are_kept_but_dry_state_left_over_is_announced(tmp_path: Path) -> None:
    async with make_rig(tmp_path, upto=None) as rig:
        rig.start()
        assert await rig.stop() == 0
    from modules.ledger.positions import PositionLedger

    ledger = await PositionLedger.open(tmp_path / "state" / "chronos_dry.db", "dry")
    try:
        assert ledger.mode == "dry"
    finally:
        await ledger.close()
    assert not (tmp_path / "state" / "chronos_live.db").exists()  # the live database is never created


# =============================================================================== 9. heartbeat / health check


def _healthcheck_code() -> str:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    test = compose["services"]["chronos"]["healthcheck"]["test"]
    assert test[:2] == ["CMD", "python"] and test[2] == "-c"
    return str(test[3])


def _run_check(cwd: Path) -> int:
    return subprocess.run([sys.executable, "-I", "-c", _healthcheck_code()], cwd=cwd, capture_output=True).returncode


def test_the_compose_health_check_reads_the_heartbeat_the_app_writes(tmp_path: Path) -> None:
    import time

    hb = tmp_path / "data" / HEARTBEAT_FILE
    assert _run_check(tmp_path) != 0  # no file: unhealthy
    write_json_atomic(hb, {"ts": time.time(), "ok": True})
    assert _run_check(tmp_path) == 0
    write_json_atomic(hb, {"ts": time.time(), "ok": False})
    assert _run_check(tmp_path) != 0  # alive but unhealthy
    write_json_atomic(hb, {"ts": time.time() - 121, "ok": True})
    assert _run_check(tmp_path) != 0  # the app stopped writing
    write_json_atomic(hb, {"ts": time.time() - 100, "ok": True})
    assert _run_check(tmp_path) == 0
    hb.write_text("{not json", encoding="utf-8")
    assert _run_check(tmp_path) != 0


async def test_the_heartbeat_is_healthy_only_when_bars_and_feed_are_alive(tmp_path: Path) -> None:
    async with make_rig(tmp_path, champion=make_champion(inner=HoldAll())) as rig:
        rig.start()
        hb = tmp_path / "data" / HEARTBEAT_FILE
        rig.publish("BTC-EUR", WARM_A)
        rig.publish("SOL-EUR", WARM_A)
        await until(lambda: json.loads(hb.read_text(encoding="utf-8")).get("ok") is True)
        status = json.loads(hb.read_text(encoding="utf-8"))["status"]
        assert status["mode"] == "dry" and status["entries_enabled"] is True and status["books"] == ["champion"]
        assert status["key"] == {"can_view": True, "can_trade": False, "can_transfer": False}
        rig.clock.now += timedelta(seconds=rig.rt.cfg.risk.kill_switch.stale_bar_s + 1)
        await until(lambda: "stale_bar:BTC-EUR" in json.loads(hb.read_text(encoding="utf-8"))["reasons"])
        assert json.loads(hb.read_text(encoding="utf-8"))["ok"] is False
        assert await rig.stop() == 0


async def test_a_silent_feed_makes_the_heartbeat_unhealthy(tmp_path: Path) -> None:
    async with make_rig(tmp_path, upto=None) as rig:
        now = rig.clock.now
        rig.feed.heard(now)
        assert "ws_silent" not in rig.rt.unhealthy_reasons(now)
        assert "ws_silent" in rig.rt.unhealthy_reasons(now + timedelta(seconds=61))
        rig.feed._last = None  # noqa: SLF001
        assert "ws_silent" in rig.rt.unhealthy_reasons(now)


async def test_a_stale_heartbeat_of_a_previous_run_is_overwritten_at_start(tmp_path: Path) -> None:
    write_json_atomic(tmp_path / "data" / HEARTBEAT_FILE, {"ts": 9e12, "ok": True})
    async with make_rig(tmp_path, upto=None):
        data = json.loads((tmp_path / "data" / HEARTBEAT_FILE).read_text(encoding="utf-8"))
        assert data["ok"] is False and data["phase"] == "starting"


async def test_status_is_json_ready_and_the_event_hook_is_isolated(tmp_path: Path) -> None:
    async with make_rig(tmp_path, upto=None) as rig:
        json.dumps(rig.rt.status())
        events: list[str] = []

        def hook(event: str, severity: str, data: Any) -> None:
            events.append(event)
            raise RuntimeError("observer bug")

        rig.rt.on_event = hook
        rig.start()
        await until(lambda: "agent_started" in events)
        assert await rig.stop() == 0
        assert "agent_stopping" in events


# =========================================================================================== 10. the secrets


def test_redaction_covers_secrets_keys_and_bearer_tokens() -> None:
    pem = "-----BEGIN EC PRIVATE KEY-----\nMHcCAQEE\nabc\n-----END EC PRIVATE KEY-----"
    out = redact({"a": f"x {pem} y", "b": ["token abc-SECRET-123 done"], "c": "Bearer eyJhbGciOi.payload.sig", "n": 3},
                 ["abc-SECRET-123"])  # fmt: skip
    assert "PRIVATE KEY" not in json.dumps(out) and "abc-SECRET-123" not in json.dumps(out)
    assert "eyJhbGciOi" not in json.dumps(out) and out["n"] == 3


def test_collect_secrets_reads_env_and_the_dot_env_values() -> None:
    cfg = AppConfig(coinbase=CoinbaseConfig(api_key="organizations/o/apiKeys/k1", api_secret="line1\\nline2-secret"))  # type: ignore[arg-type]
    secrets = collect_secrets(cfg, {"DECIDER_API_KEY": "decider-key-9", "OTHER": "visible-value"})
    assert "organizations/o/apiKeys/k1" in secrets and "decider-key-9" in secrets
    assert "line1\nline2-secret" in secrets and "line1\\nline2-secret" in secrets
    assert "visible-value" not in secrets
    assert redact("the key is organizations/o/apiKeys/k1!", secrets) == "the key is ***!"
    assert (
        collect_secrets(None, {"COINBASE_API_KEY": "abc"}) == []
    )  # shorter than 6 characters: not a secret worth cutting


async def test_a_run_never_logs_the_credentials(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    keyed = cfg.model_copy(
        update={"coinbase": CoinbaseConfig(api_key="organizations/o/apiKeys/k1", api_secret="sup3r-secret-value")}
    )  # type: ignore[arg-type]
    with structlog.testing.capture_logs() as logs:
        async with make_rig(tmp_path, cfg=keyed, upto=None) as rig:
            rig.start()
            rig.rt.life.task_failed("x", RuntimeError("boom"), critical=False)
            assert await rig.stop() == 0
    text = json.dumps(logs, default=str)
    assert "sup3r-secret-value" not in text and "organizations/o/apiKeys/k1" not in text


def test_configure_logging_scrubs_before_rendering(capsys: pytest.CaptureFixture[str]) -> None:
    app_module.configure_logging(["top-secret-xyz"])
    try:
        structlog.get_logger("t").error("event", detail="value top-secret-xyz here", nested={"k": "top-secret-xyz"})
        try:
            raise RuntimeError("failed with top-secret-xyz")
        except RuntimeError:
            structlog.get_logger("t").exception("boom")
    finally:
        structlog.reset_defaults()
    err = capsys.readouterr().err
    assert "top-secret-xyz" not in err and "***" in err


# ============================================================================================ 11. CLI, docker


def test_signal_handlers_work_on_a_loop_without_add_signal_handler() -> None:
    async def scenario() -> list[int]:
        loop = asyncio.get_running_loop()
        hit = asyncio.Event()

        def refuse(*_a: Any, **_k: Any) -> None:
            raise NotImplementedError

        loop.add_signal_handler = refuse  # type: ignore[method-assign]
        sigs = [signal.SIGINT, signal.SIGTERM, *([signal.SIGBREAK] if hasattr(signal, "SIGBREAK") else [])]
        saved = {s: signal.getsignal(s) for s in sigs}
        try:
            installed = install_signal_handlers(loop, hit.set)
            signal.raise_signal(signal.SIGTERM)
            await asyncio.wait_for(hit.wait(), 5)
            return installed
        finally:
            for s, h in saved.items():
                signal.signal(s, h)

    assert {int(signal.SIGINT), int(signal.SIGTERM)} <= set(asyncio.run(scenario()))


def test_main_exits_with_2_on_a_bad_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "config.yml"
    bad.write_text("risk_management: {}\n", encoding="utf-8")
    with pytest.raises(SystemExit) as err:
        main(["--config", str(bad)])
    assert err.value.code == 2


def test_dockerfile_and_compose_run_the_v2_app_dry_with_the_four_mounts() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert 'CMD ["python", "-m", "modules.app"]' in dockerfile
    code_lines = [ln for ln in dockerfile.splitlines() if not ln.lstrip().startswith("#")]
    assert not any("main.py" in ln for ln in code_lines)  # the v1 entry point is neither copied nor started
    assert not any(ln.strip() in ("COPY . .", "COPY . /app") for ln in code_lines)  # .env / data / models stay out
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    chronos = compose["services"]["chronos"]
    mounts = " ".join(chronos["volumes"])
    for target in ("/app/data", "/app/models", "/app/state", "/app/config"):
        assert f":{target}" in mounts, target
    assert "chronos-state:/app/state" in chronos["volumes"]
    env = " ".join(str(e) for e in chronos["environment"])
    assert "CHRONOS_LIVE_CONFIRM" not in env  # the container cannot be confirmed live by accident
    assert chronos["restart"] == "unless-stopped"
    assert chronos["healthcheck"]["start_period"]
    assert set(compose["services"]) >= {"chronos", "test", "dev"}
    assert compose["services"]["test"]["profiles"] == ["test"] and compose["services"]["dev"]["profiles"] == ["dev"]
    assert "sys.exit(0)" not in _healthcheck_code().replace("else 1", "")  # not the old always-true check
