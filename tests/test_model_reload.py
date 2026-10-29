"""The app side of the model life cycle (spec 5.1.5, 5.1.6): the learner at 00:15 as a child process, the reload at
00:30 with an atomic swap between two bars, fail-closed loading, the label_config rule for open positions and the
version a trade is attributed to (resolution P1e, step 17)."""

from __future__ import annotations

import asyncio
import dataclasses
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from modules.app import Champion, StartupError, no_entry_policy
from modules.config import BookConfig
from modules.decision.baseline import AnswerArrays
from modules.decision.engine import LABEL_CONFIG_MISMATCH
from modules.decision.questions import build_questions
from modules.dryrun.books import RandomDecider
from modules.dryrun.schedule import DailyJob, LearnerResult
from tests.app_fixtures import PRODUCTS, AllIn, HoldAll, Rig, make_cfg, make_champion, make_rig, until
from tests.label_fixtures import loaded_label
from tests.test_app_books import feed_bars, settle
from tests.test_decision_engine import WARM_A, StubBaseline, frame

OTHER_LABEL = loaded_label({"BTC-EUR": 3.0, "SOL-EUR": 3.0}, horizon_min=2 * 1440, k_tp=2.0)
NIGHT = datetime(2026, 11, 2, tzinfo=UTC)


class SellAll:
    """BUY never, SELL always: a LONG position is sold whenever the policy lets the answer through."""

    model_key = "sell-all"

    def answers_batch(self, x: np.ndarray) -> AnswerArrays:
        n = len(x)
        return AnswerArrays(
            np.tile([0.0, 1.0, 0.0], (n, 1)), np.full(n, 0.9), np.tile([0.0, 0.0, 1.0, 0.0, 0.0], (n, 1))
        )


def variant(
    version: str, inner: Any, *, label: Any = None, untunable: str | None = None, sentiment: bool = False,
    policy_sha: str | None = None,
) -> Champion:  # fmt: skip
    """Another version of the stub champion: its own decider and, optionally, another label_config."""
    base = make_champion()
    chosen = label or base.label
    questions = build_questions(chosen.cfg.horizon_min, chosen.cfg.k_tp, chosen.cfg.k_sl, chosen.cfg.atr_tf)
    if untunable is not None:
        policy = no_entry_policy(version, chosen.sha256)
    else:
        raw = {**base.policy.model_dump(), "model": version, "label_config_sha256": policy_sha or chosen.sha256}
        policy = type(base.policy)(**raw)
    return Champion(
        version, Path(f"models/baseline/{version}"), chosen, questions, policy, untunable, sentiment,
        StubBaseline(inner),
    )  # fmt: skip


class Registry:
    """What ``Deps.model_alias`` answers: the version each alias points at right now (None: not set)."""

    def __init__(self) -> None:
        self.by_alias: dict[str, Champion | Exception | None] = {}

    def __call__(self, models_dir: Path, alias: str) -> Champion | None:
        value = self.by_alias.get(alias)
        if isinstance(value, Exception):
            raise value
        return value


def events_of(rig: Rig) -> list[tuple[str, str, dict[str, Any]]]:
    seen: list[tuple[str, str, dict[str, Any]]] = []
    rig.rt.on_event = lambda name, severity, data: seen.append((name, severity, dict(data)))
    return seen


def last_policy_version(rig: Rig, book: str = "champion") -> str:
    recs = [r for r in rig.decisions() if r["book"] == book and "policy" in r]
    return str(recs[-1]["policy"]["policy_version"])


# ================================================================================================= the reload


async def test_the_reload_swaps_the_champion_and_logs_the_change(tmp_path: Path) -> None:
    reg = Registry()
    async with make_rig(tmp_path, champion=variant("v1", HoldAll()), model_alias=reg) as rig:
        seen = events_of(rig)
        await feed_bars(rig, range(WARM_A, WARM_A + 3))
        assert last_policy_version(rig) == "v1"
        reg.by_alias["champion"] = variant("v2", HoldAll())
        changes = await rig.rt.reload_models()
        assert changes == [{"book": "champion", "alias": "champion", "from": "v1", "to": "v2"}]
        assert rig.book.engine is not None and rig.book.engine.model_version == "v2"
        assert rig.book.book.model_version == "v2" and rig.rt.champion.version == "v2"
        assert rig.rt.models["champion"].version == "v2"
        await feed_bars(rig, range(WARM_A + 3, WARM_A + 5))
        assert last_policy_version(rig) == "v2"
        versions = [r["policy"]["policy_version"] for r in rig.decisions() if r["book"] == "champion"]
        assert versions == ["v1"] * 6 + ["v2"] * 4  # 3 bars x 2 products, then 2 x 2: switched between two bars
        (event,) = [e for e in seen if e[0] == "model_version_changed"]
        assert event[1] == "info" and event[2]["from"] == "v1" and event[2]["to"] == "v2"
        assert event[2]["alias"] == "champion" and event[2]["book"] == "champion"
        assert rig.rt.status()["agent"]["models"]["champion"] == "v2" and rig.rt.status()["model"] == "v2"
        assert rig.rt.reload_log[0]["to"] == "v2"


async def test_a_reload_with_nothing_new_changes_nothing(tmp_path: Path) -> None:
    reg = Registry()
    async with make_rig(tmp_path, champion=variant("v1", HoldAll()), model_alias=reg) as rig:
        seen = events_of(rig)
        assert await rig.rt.reload_models() == []  # the alias is not set: the books keep what they run
        reg.by_alias["champion"] = variant("v1", HoldAll())
        assert await rig.rt.reload_models() == []  # same version
        assert [e for e in seen if e[0].startswith("model_")] == []
        assert rig.book.book.model_version == "v1"


async def test_the_random_entry_book_follows_the_model_of_the_book_it_matches(tmp_path: Path) -> None:
    books = (
        BookConfig(name="champion", decider="registry:champion"),
        BookConfig(
            name="random_entry", decider="random", match_frequency_of="champion", seed=7, forced_buy_rate=1.0,
            forced_edge=0.9,
        ),  # fmt: skip
        BookConfig(name="buy_and_hold", decider="buy_and_hold"),
    )
    reg = Registry()
    cfg = make_cfg(tmp_path, books=books)
    async with make_rig(tmp_path, cfg=cfg, champion=variant("v1", HoldAll()), model_alias=reg, upto=None) as rig:
        seen = events_of(rig)
        before_risk = {b.name: b.book.risk for b in rig.rt.books}
        reg.by_alias["champion"] = variant("v2", HoldAll(), label=OTHER_LABEL)
        changes = await rig.rt.reload_models()
        assert {c["book"] for c in changes} == {"champion", "random_entry"}  # not buy_and_hold: it has no model
        for name in ("champion", "random_entry"):
            b = rig.book_named(name)
            assert b.engine is not None and b.engine.model_version == "v2" and b.book.model.label is OTHER_LABEL
            assert b.book.risk is not before_risk[name]  # its own RiskManager, built from the new label
            assert b.watchdog is not None and b.watchdog._horizon_min == 2 * 1440  # noqa: SLF001
        assert rig.book_named("buy_and_hold").book.model is None
        assert sum(1 for e in seen if e[0] == "model_version_changed") == 2
        random_engine = rig.book_named("random_entry").engine
        assert random_engine is not None and isinstance(random_engine._baseline, RandomDecider)  # noqa: SLF001
        champion_engine = rig.book_named("champion").engine
        assert champion_engine is not None and not isinstance(champion_engine._baseline, RandomDecider)  # noqa: SLF001


async def test_an_untunable_version_switches_the_entry_gate_on_and_back_off(tmp_path: Path) -> None:
    reg = Registry()
    async with make_rig(tmp_path, champion=variant("v1", HoldAll()), model_alias=reg, upto=None) as rig:
        assert rig.rt.gates.entry_blocked("champion") is None
        reg.by_alias["champion"] = variant("v2", HoldAll(), untunable="untunable_ev")
        await rig.rt.reload_models()
        assert rig.rt.gates.entry_blocked("champion") == "policy_untunable:untunable_ev"
        reg.by_alias["champion"] = variant("v3", HoldAll())  # a rollback or a better version
        await rig.rt.reload_models()
        assert rig.rt.gates.entry_blocked("champion") is None


# ================================================================================== fail closed, one by one


@pytest.mark.parametrize(
    "target",
    [
        pytest.param(StartupError("model v2 cannot be loaded: sha256 does not match"), id="damaged_registry"),
        pytest.param(OSError("file missing"), id="missing_file"),
        pytest.param(variant("v2", HoldAll(), sentiment=True), id="other_sentiment"),
        pytest.param(variant("v2", HoldAll(), label=loaded_label({"BTC-EUR": 3.0})), id="no_spread_for_a_product"),
        pytest.param(variant("v2", HoldAll(), policy_sha="0" * 64), id="policy_for_another_label_config"),
    ],
)
async def test_a_version_that_cannot_be_served_leaves_the_book_on_the_old_one(
    tmp_path: Path, target: Champion | Exception
) -> None:
    reg = Registry()
    async with make_rig(tmp_path, champion=variant("v1", HoldAll()), model_alias=reg) as rig:
        seen = events_of(rig)
        await feed_bars(rig, range(WARM_A, WARM_A + 2))
        reg.by_alias["champion"] = target
        assert await rig.rt.reload_models() == []
        (failed,) = [e for e in seen if e[0] == "model_reload_failed"]
        assert failed[1] == "error" and failed[2]["alias"] == "champion" and failed[2]["kept"] == "v1"
        assert failed[2]["error"]
        assert not [e for e in seen if e[0] == "model_version_changed"]
        assert rig.book.engine is not None and rig.book.engine.model_version == "v1"
        assert rig.rt.champion.version == "v1" and rig.book.book.model_version == "v1"
        await feed_bars(rig, range(WARM_A + 2, WARM_A + 4))  # and the book goes on deciding with it
        assert last_policy_version(rig) == "v1"
        assert list(rig.rt.life.failures) == [] or not rig.rt.life.stopping


async def test_a_swap_is_all_or_nothing_across_the_books_of_an_alias(tmp_path: Path) -> None:
    books = (
        BookConfig(name="champion", decider="registry:champion"),
        BookConfig(
            name="random_entry", decider="random", match_frequency_of="champion", seed=7, forced_buy_rate=1.0,
            forced_edge=0.9,
        ),  # fmt: skip
    )
    reg = Registry()
    cfg = make_cfg(tmp_path, books=books)
    async with make_rig(tmp_path, cfg=cfg, champion=variant("v1", HoldAll()), model_alias=reg, upto=None) as rig:
        from modules.dryrun import books as books_module

        real = books_module.prepare_book_swap
        calls: list[str] = []

        def second_one_fails(book: Any, deps: Any, model: Any) -> Any:
            calls.append(book.name)
            if book.name == "random_entry":
                raise ValueError("cannot be built")
            return real(book, deps, model)

        import modules.app as app_module

        monkey = pytest.MonkeyPatch()
        monkey.setattr(app_module, "prepare_book_swap", second_one_fails)
        try:
            reg.by_alias["champion"] = variant("v2", HoldAll())
            assert await rig.rt.reload_models() == []
        finally:
            monkey.undo()
        assert calls == ["champion", "random_entry"]
        for name in ("champion", "random_entry"):  # the first was prepared, never installed
            assert rig.book_named(name).engine.model_version == "v1"  # type: ignore[union-attr]


async def test_the_swap_waits_for_the_batch_being_decided(tmp_path: Path) -> None:
    """A decision is made entirely with one model: the swap is queued behind the batch that is in flight."""
    gate = asyncio.Event()

    class Slow(StubBaseline):
        async def decide(self, state: str, questions: Any) -> Any:
            await gate.wait()
            return await super().decide(state, questions)

    reg = Registry()
    slow = dataclasses.replace(variant("v1", HoldAll()), baseline=Slow(HoldAll()))
    async with make_rig(tmp_path, champion=slow, model_alias=reg) as rig:
        events = [rig.event(p, WARM_A) for p in PRODUCTS]
        for ev in events:
            rig.clock.now = ev.published_at
            rig.feed.quote_around(ev.product_id, float(frame(ev.product_id).close.iloc[WARM_A]), ev.published_at)
        batch = asyncio.create_task(rig.rt.process_bars(events))
        await asyncio.sleep(0.05)
        reg.by_alias["champion"] = variant("v2", HoldAll())
        reload_task = asyncio.create_task(rig.rt.reload_models())
        await asyncio.sleep(0.1)
        assert not reload_task.done() and rig.book.engine is not None and rig.book.engine.model_version == "v1"
        gate.set()
        await batch
        await reload_task
        assert {r["policy"]["policy_version"] for r in rig.decisions() if r["book"] == "champion"} == {"v1"}
        assert rig.book.engine.model_version == "v2"


# ============================================================================== positions across a swap


def opened(rig: Rig) -> dict[str, Any]:
    return {p.product_id: p for p in rig.rt.ledger.long_positions("champion")}


async def open_positions(rig: Rig) -> tuple[dict[str, Any], int]:
    """Feed bars until the all-in champion holds both products; returns the positions and the next bar index."""
    i = WARM_A
    while len(opened(rig)) < len(PRODUCTS):
        assert i < WARM_A + 40, "the all-in champion should hold both products by now"
        await feed_bars(rig, [i])
        i += 1
    return opened(rig), i


async def test_positions_record_the_model_that_opened_them(tmp_path: Path) -> None:
    async with make_rig(tmp_path, champion=variant("v1", AllIn())) as rig:
        for pos in (await open_positions(rig))[0].values():
            assert (pos.model_version, pos.label_sha256, pos.horizon_min) == (
                "v1", rig.rt.champion.label.sha256, rig.rt.champion.label.cfg.horizon_min,
            )  # fmt: skip


async def test_the_sell_of_another_label_config_does_not_touch_a_position_opened_before_the_swap(
    tmp_path: Path,
) -> None:
    reg = Registry()
    async with make_rig(tmp_path, champion=variant("v1", AllIn()), model_alias=reg) as rig:
        positions, nxt = await open_positions(rig)
        old_label = rig.rt.champion.label
        reg.by_alias["champion"] = variant("v2", SellAll(), label=OTHER_LABEL)
        await rig.rt.reload_models()
        await feed_bars(rig, range(nxt, nxt + 6))
        after = opened(rig)
        assert set(after) == set(positions)  # nothing was sold
        for pos in after.values():
            assert pos.model_version == "v1" and pos.label_sha256 == old_label.sha256  # still the opener's
        held = [r for r in rig.decisions() if r["policy"].get("policy_version") == "v2" and r["product"] in after]
        assert held and all(r["policy"]["final"] == "HOLD" for r in held)
        assert {r["policy"]["reason"] for r in held} == {LABEL_CONFIG_MISMATCH}
        assert await rig.rt.ledger.closed_trades("champion") == []
        # bracket and watchdog are what govern it: the stop still closes it
        product, pos = next(iter(after.items()))
        from datetime import timedelta as td

        from modules.market.bars import BarClosed
        from modules.market.timeframes import Bar

        i = nxt + 6
        row = frame(product).iloc[i]
        ts = frame(product).index[i].to_pydatetime()
        crash = Bar(ts, float(row.open), float(row.high), float(pos.sl) - 2.0, float(pos.sl) - 1.0, float(row.volume))
        ev = BarClosed(product, crash, "rest", ts + td(seconds=180))
        rig.clock.now = ev.published_at
        await rig.rt.process_bars([ev])
        await settle(rig)
        (trade,) = await rig.rt.ledger.closed_trades("champion")
        assert trade.exit_reason == "SL"
        assert trade.model_version == "v1"  # attributed to the opener, though it closed after the swap


async def test_the_sell_of_the_same_label_config_applies_and_the_trade_is_the_openers(tmp_path: Path) -> None:
    reg = Registry()
    async with make_rig(tmp_path, champion=variant("v1", AllIn()), model_alias=reg) as rig:
        _, nxt = await open_positions(rig)
        reg.by_alias["champion"] = variant("v2", SellAll())  # same label_config: the SELL head is meant for it
        await rig.rt.reload_models()
        for i in range(nxt, nxt + 8):
            await feed_bars(rig, [i])
            if await rig.rt.ledger.closed_trades("champion"):
                break
        trades = await rig.rt.ledger.closed_trades("champion")
        assert trades and {t.exit_reason for t in trades} == {"SELL"}
        assert {t.model_version for t in trades} == {"v1"}  # opened by v1, sold by v2's decision


async def test_an_entry_waiting_to_fill_belongs_to_the_model_that_decided_it(tmp_path: Path) -> None:
    reg = Registry()
    async with make_rig(tmp_path, champion=variant("v1", AllIn()), model_alias=reg) as rig:
        await feed_bars(rig, [WARM_A])  # v1 decides to buy; the maker order is armed and waits for its fill
        assert any(rig.book.shadow.has_pending_entry(p) for p in PRODUCTS) and not opened(rig)
        reg.by_alias["champion"] = variant("v2", HoldAll(), label=OTHER_LABEL)
        await rig.rt.reload_models()  # the swap happens while the entry waits
        await feed_bars(rig, range(WARM_A + 1, WARM_A + 8))
        positions = opened(rig)
        assert positions
        assert {p.model_version for p in positions.values()} == {"v1"}  # not the v2 that runs when it fills
        assert {p.horizon_min for p in positions.values()} == {4 * 1440}


async def test_the_watchdog_closes_a_position_at_its_own_horizon_not_the_books(tmp_path: Path) -> None:
    reg = Registry()
    async with make_rig(tmp_path, champion=variant("v1", AllIn()), model_alias=reg) as rig:
        positions, _ = await open_positions(rig)
        reg.by_alias["champion"] = variant("v2", HoldAll(), label=OTHER_LABEL)  # horizon 2 days
        await rig.rt.reload_models()
        dog = rig.book.watchdog
        assert dog is not None and dog._horizon_min == 2 * 1440  # noqa: SLF001
        pos = next(iter(positions.values()))
        assert pos.opened_at is not None
        three_days = pos.opened_at + timedelta(days=3)
        assert dog._horizon_verdict(pos, three_days) is None  # noqa: SLF001  (v2's 2 days would close it: v1's 4 days)
        verdict = dog._horizon_verdict(pos, pos.opened_at + timedelta(days=4, minutes=2))  # noqa: SLF001
        assert verdict is not None and verdict.cause == "HORIZON"
        legacy = dataclasses.replace(pos, horizon_min=None)  # a position that records none uses the book's
        assert dog._horizon_verdict(legacy, three_days) is not None  # noqa: SLF001


# ===================================================================================== restart in the middle


async def test_a_restart_on_another_version_keeps_the_position_of_the_previous_one(tmp_path: Path) -> None:
    async with make_rig(tmp_path, champion=variant("v1", AllIn())) as first:
        positions, nxt = await open_positions(first)
        old_sha = first.rt.champion.label.sha256
        first.rt.request_stop("test")
        await first.rt._shutdown()  # noqa: SLF001
    history = {p: frame(p).iloc[:nxt].copy() for p in PRODUCTS}
    async with make_rig(
        tmp_path, champion=variant("v2", SellAll(), label=OTHER_LABEL), history_frames=history
    ) as second:
        restored = opened(second)
        assert set(restored) == set(positions)
        for pos in restored.values():
            assert (pos.model_version, pos.label_sha256) == ("v1", old_sha)  # not re-stamped with the new model
        await feed_bars(second, range(nxt, nxt + 4))
        assert set(opened(second)) == set(positions)  # the new model's SELL does not apply to them
        mine = [r for r in second.decisions() if r["policy"].get("policy_version") == "v2" and r["product"] in restored]
        assert mine and {r["policy"]["reason"] for r in mine} == {LABEL_CONFIG_MISMATCH}
        assert {p.horizon_min for p in opened(second).values()} == {4 * 1440}


async def test_positions_opened_by_a_build_that_recorded_no_model_take_the_one_of_the_book(tmp_path: Path) -> None:
    async with make_rig(tmp_path, champion=variant("v1", AllIn())) as first:
        positions, nxt = await open_positions(first)
        first.rt.request_stop("test")
        await first.rt._shutdown()  # noqa: SLF001
    con = sqlite3.connect(tmp_path / "state" / "chronos_dry.db")
    con.execute("UPDATE positions SET model_version=NULL, label_config_sha256=NULL, horizon_min=NULL")
    con.commit()
    con.close()  # what a database written before the stamps existed looks like
    history = {p: frame(p).iloc[:nxt].copy() for p in PRODUCTS}
    book_model = variant("v1b", HoldAll(), label=OTHER_LABEL)
    async with make_rig(tmp_path, champion=book_model, history_frames=history) as second:
        stamped = opened(second)
        assert set(stamped) == set(positions)
        for pos in stamped.values():
            assert (pos.model_version, pos.label_sha256, pos.horizon_min) == (
                "v1b", OTHER_LABEL.sha256, 2 * 1440,
            )  # fmt: skip


# ================================================================================================== the clock


def learner_runner(results: list[LearnerResult | Exception], calls: list[Any]) -> Any:
    async def run(argv: Any, timeout_s: float) -> LearnerResult:
        calls.append((tuple(argv), timeout_s))
        item = results.pop(0) if results else LearnerResult(0, False, 0.0, "")
        if isinstance(item, Exception):
            raise item
        return item

    return run


async def test_the_clock_runs_the_learner_at_0015_and_the_reload_at_0030(tmp_path: Path) -> None:
    reg, calls = Registry(), []
    async with make_rig(
        tmp_path, champion=variant("v1", HoldAll()), model_alias=reg, run_learner=learner_runner([], calls)
    ) as rig:
        seen = events_of(rig)
        rig.rt.config_path = "config/dry.yml"
        rig.rt.jobs = [DailyJob("learner", "00:15", NIGHT - timedelta(hours=1)), DailyJob("reload", "00:30", NIGHT)]
        # (the second job is built "after 00:30 of the 2nd": it waits for the next day, like a restart at that time)
        rig.rt.jobs[1] = DailyJob("reload", "00:30", NIGHT - timedelta(hours=1))
        reg.by_alias["champion"] = variant("v2", HoldAll())
        assert await rig.rt.run_scheduled(NIGHT - timedelta(minutes=1)) == []
        assert await rig.rt.run_scheduled(NIGHT + timedelta(minutes=14, seconds=59)) == []
        assert await rig.rt.run_scheduled(NIGHT + timedelta(minutes=15)) == ["learner"]
        assert rig.rt.learner is not None
        await rig.rt.learner.wait()
        assert calls == [(("--config", "config/dry.yml"), rig.rt.learner_timeout_s)]
        assert rig.book.engine is not None and rig.book.engine.model_version == "v1"  # 00:15: nothing reloaded yet
        assert await rig.rt.run_scheduled(NIGHT + timedelta(minutes=29)) == []
        assert await rig.rt.run_scheduled(NIGHT + timedelta(minutes=30)) == ["reload"]
        assert rig.book.engine.model_version == "v2"
        assert await rig.rt.run_scheduled(NIGHT + timedelta(minutes=31)) == []  # once a day
        assert [e[0] for e in seen if e[0].startswith(("learner_", "model_"))] == [
            "learner_started", "learner_finished", "model_version_changed",
        ]  # fmt: skip
        reg.by_alias["champion"] = variant("v3", HoldAll())
        assert await rig.rt.run_scheduled(NIGHT + timedelta(days=1, minutes=15)) == ["learner"]
        assert await rig.rt.run_scheduled(NIGHT + timedelta(days=1, minutes=30)) == ["reload"]
        assert rig.book.engine.model_version == "v3" and len(calls) == 2
        await rig.rt.learner.wait()


async def test_the_scheduler_task_fires_from_the_clock_of_the_app_and_never_stops_it(tmp_path: Path) -> None:
    reg, calls = Registry(), []
    async with make_rig(
        tmp_path, champion=variant("v1", HoldAll()), model_alias=reg, run_learner=learner_runner([], calls)
    ) as rig:
        rig.rt.jobs = [DailyJob("learner", "00:15", NIGHT), DailyJob("reload", "00:30", NIGHT)]
        reg.by_alias["champion"] = variant("v2", HoldAll())
        rig.clock.now = NIGHT + timedelta(minutes=5)
        rig.start()
        await asyncio.sleep(0.3)
        assert calls == [] and rig.book.engine is not None and rig.book.engine.model_version == "v1"
        rig.clock.now = NIGHT + timedelta(minutes=16)
        await until(lambda: len(calls) == 1)
        rig.clock.now = NIGHT + timedelta(minutes=31)
        await until(lambda: rig.book.engine is not None and rig.book.engine.model_version == "v2")
        await asyncio.sleep(0.3)
        assert len(calls) == 1  # still once
        assert await rig.stop() == 0 and list(rig.rt.life.failures) == []


@pytest.mark.parametrize(
    ("outcome", "event"),
    [
        (LearnerResult(1, False, 3.0, "boom"), "learner_failed"),
        (LearnerResult(None, True, 21600.0, ""), "learner_timeout"),
        (FileNotFoundError("python"), "learner_error"),
    ],
    ids=["exit_1", "timeout", "cannot_start"],
)
async def test_a_failing_learner_is_an_event_and_the_app_goes_on_trading(
    tmp_path: Path, outcome: LearnerResult | Exception, event: str
) -> None:
    calls: list[Any] = []
    async with make_rig(
        tmp_path, champion=variant("v1", HoldAll()), run_learner=learner_runner([outcome], calls)
    ) as rig:
        seen = events_of(rig)
        rig.rt.jobs = [DailyJob("learner", "00:15", NIGHT)]
        rig.start()
        rig.clock.now = NIGHT + timedelta(minutes=16)
        await until(lambda: any(e[0] == event for e in seen))
        assert next(e for e in seen if e[0] == event)[1] == "error"
        rig.publish(PRODUCTS[0], WARM_A)  # and the pipeline still works
        await until(lambda: any(r["book"] == "champion" for r in rig.decisions()))
        assert await rig.stop() == 0 and list(rig.rt.life.failures) == [] and rig.rt.life.exit_code == 0


async def test_a_second_learner_is_not_started_while_the_first_runs_and_shutdown_kills_it(tmp_path: Path) -> None:
    started, cancelled = asyncio.Event(), []

    async def hang(argv: Any, timeout_s: float) -> LearnerResult:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return LearnerResult(0, False, 0.0, "")

    async with make_rig(tmp_path, champion=variant("v1", HoldAll()), run_learner=hang) as rig:
        seen = events_of(rig)
        rig.rt.jobs = [DailyJob("learner", "00:15", NIGHT - timedelta(hours=1))]
        assert await rig.rt.run_scheduled(NIGHT + timedelta(minutes=15)) == ["learner"]
        await started.wait()
        rig.rt.jobs[0].last_fired = None  # as if the next day came while the child still runs
        assert await rig.rt.run_scheduled(NIGHT + timedelta(days=1, minutes=15)) == ["learner"]
        assert [e[0] for e in seen if e[0].startswith("learner_")] == ["learner_started", "learner_skipped"]
        await rig.rt._shutdown()  # noqa: SLF001
    assert cancelled == [True]


async def test_a_job_that_raises_is_an_event_and_does_not_stop_the_clock(tmp_path: Path) -> None:
    async with make_rig(tmp_path, champion=variant("v1", HoldAll())) as rig:
        seen = events_of(rig)

        async def broken() -> list[dict[str, str]]:
            raise RuntimeError("registry on fire")

        rig.rt.reload_models = broken  # type: ignore[method-assign]
        rig.rt.jobs = [DailyJob("reload", "00:30", NIGHT - timedelta(hours=1))]
        assert await rig.rt.run_scheduled(NIGHT + timedelta(minutes=30)) == ["reload"]
        failed = next(e for e in seen if e[0] == "reload_job_failed")
        assert failed[1] == "error" and "registry on fire" in failed[2]["error"]


async def test_a_configured_book_whose_alias_appears_later_is_announced_for_the_next_restart(tmp_path: Path) -> None:
    books = (
        BookConfig(name="champion", decider="registry:champion"),
        BookConfig(name="challenger", decider="registry:challenger"),
    )
    reg = Registry()
    cfg = make_cfg(tmp_path, books=books)
    async with make_rig(tmp_path, cfg=cfg, champion=variant("v1", HoldAll()), model_alias=reg, upto=None) as rig:
        seen = events_of(rig)
        assert [b.name for b in rig.rt.books] == ["champion"]  # no challenger at start: the book is not running
        reg.by_alias["challenger"] = variant("c1", HoldAll())
        await rig.rt.reload_models()
        await rig.rt.reload_models()  # said once per version
        needs = [e for e in seen if e[0] == "book_needs_restart"]
        assert len(needs) == 1 and needs[0][2]["book"] == "challenger" and needs[0][2]["version"] == "c1"
        assert [b.name for b in rig.rt.books] == ["champion"]
