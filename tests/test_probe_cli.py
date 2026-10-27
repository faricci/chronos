"""The command line and the stage ladder of the probe (plan sez. 3.5, 4): the gate, the chain, the typed confirmation,
and every stage up to the live smoke, all on the fake venue. No network, no real key (the key file holds invented
text)."""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from modules.config import CoinbaseConfig, ExecutionConfig
from modules.execution.exchange import OrderRejected
from modules.execution.raw_log import RawLog
from modules.probe import runner as runner_module
from modules.probe.__main__ import main
from modules.probe.runner import Deps, ProbeRuntime, execute_stage
from modules.probe.stages import read_stage
from tests.probe_fakes import Clock, P, ScaledClock, Venue
from tests.test_probe_runtime import ENTRY9, LIVE_ENV, make_cfg

D = Decimal
PHRASE = "CONFERMO BTC-EUR 3.00 EUR\n"
PEM = (
    "-----BEGIN EC PRIVATE KEY-----\\n"
    "MHcCAQEEIFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEoAoGCCqGSM49\\n"
    "-----END EC PRIVATE KEY-----\\n"
)


class Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


class World:
    """A repo directory, a key file outside it, a config file, a venue and a Deps that counts what gets built."""

    def __init__(self, tmp_path: Path, clock: Clock | ScaledClock | None = None, *, stdin: str = PHRASE) -> None:
        self.tmp = tmp_path
        self.repo = tmp_path / "repo"
        self.repo.mkdir(exist_ok=True)
        (tmp_path / "outside").mkdir(exist_ok=True)
        self.key = tmp_path / "outside" / "probe.key"
        self.key.write_text(f'COINBASE_API_KEY=organizations/o/apiKeys/k1\nCOINBASE_API_SECRET="{PEM}"\n')
        self.clock = clock or Clock()
        self.venue = Venue(self.clock)
        self.read_calls: list[str] = []
        self.write_calls: list[str] = []
        self.out = io.StringIO()
        self.err = io.StringIO()
        self.runtimes: list[ProbeRuntime] = []
        self.cfg_path = self.write_config(enabled=True)
        self.stdin = Tty(stdin)
        self.env = dict(LIVE_ENV)
        self.sleep: Callable[[float], Any] = self.clock.sleep

    def write_config(self, *, enabled: bool) -> Path:
        path = self.tmp / "config.yml"
        state = (self.tmp / "state").as_posix()
        path.write_text(
            f"paths: {{state_dir: '{state}'}}\n"
            f"exec_probe: {{enabled: {str(enabled).lower()}, db_path: '{state}/probe.db', "
            f"out_dir: '{(self.tmp / 'out').as_posix()}'}}\n"
        )
        return path

    def deps(self, **kw: Any) -> Deps:
        def read(creds: CoinbaseConfig) -> Venue:
            self.read_calls.append("read")
            return self.venue

        def write(creds: CoinbaseConfig) -> Venue:
            self.write_calls.append("write")
            return self.venue

        return Deps(
            read_exchange=read, write_exchange=write,  # type: ignore[arg-type]
            env=self.env, stdin=self.stdin, stdout=self.out, stderr=self.err, clock=self.clock, sleep=self._sleep,
            repo_root=self.repo, configure_logs=False, install_signals=False, streams=False,
            on_runtime=self.runtimes.append, **kw,
        )  # fmt: skip

    async def _sleep(self, seconds: float) -> None:
        await self.sleep(seconds)

    def run(self, *argv: str, key: bool = True, **kw: Any) -> int:
        full = [*argv, "--config", str(self.cfg_path)]
        if key:
            full += ["--key-file", str(self.key)]
        return main(full, self.deps(**kw))

    @property
    def out_dir(self) -> Path:
        return self.tmp / "out"


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


# ---------------------------------------------------------------------------------------------- the gate


def test_the_default_probe_is_off(world: World) -> None:
    world.cfg_path = world.write_config(enabled=False)
    assert world.run("preflight") == 2
    assert world.read_calls == [] and world.write_calls == [] and "enabled is false" in world.err.getvalue()


@pytest.mark.parametrize(
    "variant",
    [
        "no_live_confirm",
        "wrong_probe_confirm",
        "no_probe_confirm",
        "no_key_file",
        "key_in_repo",
        "key_in_env",
        "no_tty",
    ],
)
def test_each_missing_act_refuses_before_anything_is_built(world: World, variant: str) -> None:
    key = True
    if variant == "no_live_confirm":
        del world.env["CHRONOS_LIVE_CONFIRM"]
    elif variant == "wrong_probe_confirm":
        world.env["CHRONOS_EXEC_PROBE_CONFIRM"] = "BTC-EUR:20.00"
    elif variant == "no_probe_confirm":
        del world.env["CHRONOS_EXEC_PROBE_CONFIRM"]
    elif variant == "no_key_file":
        key = False
    elif variant == "key_in_repo":
        inside = world.repo / "probe.key"
        inside.write_text(world.key.read_text())
        world.key = inside
    elif variant == "key_in_env":
        world.env["COINBASE_API_KEY"] = "organizations/x/apiKeys/y"
    elif variant == "no_tty":
        world.stdin = io.StringIO(PHRASE)
    assert world.run("run", "--stage", "preview", key=key) == 2
    assert world.read_calls == [] and world.write_calls == []
    assert "RIFIUTATO" in world.err.getvalue()


def test_preflight_needs_only_the_flag_and_the_key_file(world: World) -> None:
    world.env.clear()
    world.stdin = io.StringIO("")
    assert world.run("preflight") == 0
    assert world.write_calls == []  # a read-only exchange is all the preflight ever gets
    assert world.read_calls == ["read"]


def test_no_secret_reaches_the_screen(world: World) -> None:
    assert world.run("preflight") == 0
    text = world.out.getvalue() + world.err.getvalue()
    assert "k1" not in text and "FAKEFAKE" not in text and "BEGIN EC" not in text
    assert "impronta" in text


# --------------------------------------------------------------------------------------------- the chain


def test_a_stage_cannot_start_before_the_previous_one(world: World) -> None:
    assert world.run("run", "--stage", "preview") == 2
    assert "STADIO BLOCCATO" in world.err.getvalue() and world.read_calls == [] and world.write_calls == []


def test_the_wrong_phrase_sends_nothing(world: World) -> None:
    assert world.run("preflight") == 0
    world.stdin = Tty("si\n")
    assert world.run("run", "--stage", "preview") == 2
    assert world.write_calls == [] and not [e for e in world.venue.log if e[0] in ("create", "preview", "cancel")]
    assert read_stage(world.out_dir, "preview") is None
    assert "nessun ordine inviato" in world.err.getvalue()


def test_preview_with_the_right_phrase(world: World) -> None:
    assert world.run("preflight") == 0
    world.stdin = Tty(PHRASE)
    assert world.run("run", "--stage", "preview") == 0
    record = read_stage(world.out_dir, "preview")
    assert record is not None and record.ok and record.entry_mode == "gtd"
    assert world.write_calls == ["write"] and world.venue.creates() == []  # a write door, but nothing was created
    assert "EUR disponibili" in world.out.getvalue() and "impronta" in world.out.getvalue()


def test_a_refused_gtd_leaves_the_probe_closed_until_the_user_accepts_the_fallback(world: World) -> None:
    world.venue.preview["gtd"] = OrderRejected("INVALID_END_TIME", "no")
    assert world.run("preflight") == 0
    world.stdin = Tty(PHRASE)
    assert world.run("run", "--stage", "preview") == 0
    record = read_stage(world.out_dir, "preview")
    assert record is not None and record.entry_mode == "gtc_fallback"


# --------------------------------------------------------------------------------- hand switches (CLI)


def test_stop_and_flatten_need_no_key_and_say_what_they_do(world: World) -> None:
    code = main(["stop", "--config", str(world.cfg_path)], world.deps())
    assert code == 0 and (world.tmp / "state" / "exec_probe" / "STOP").is_file()
    assert "A MANO" in world.out.getvalue() and world.read_calls == world.write_calls == []
    assert main(["flatten", "--config", str(world.cfg_path)], world.deps()) == 0
    assert (world.tmp / "state" / "exec_probe" / "FLATTEN").is_file()


def test_status_without_a_running_probe_still_reports_the_hand_switch(world: World) -> None:
    assert main(["status", "--config", str(world.cfg_path)], world.deps()) == 0
    assert "entries_blocked: None" in world.out.getvalue()
    main(["stop", "--config", str(world.cfg_path)], world.deps())
    world.out.truncate(0)
    world.out.seek(0)
    main(["status", "--config", str(world.cfg_path)], world.deps())
    assert "entries_blocked: manual_stop" in world.out.getvalue() and "STOP: presente" in world.out.getvalue()


# ----------------------------------------------------------------------------------------- the ladder


class TrackingRawLog(RawLog):
    instances: list[TrackingRawLog] = []

    def __init__(self, *a: Any, **k: Any) -> None:
        super().__init__(*a, **k)
        TrackingRawLog.instances.append(self)


def feed_the_channel(world: World) -> None:
    """Whatever the venue does to an order is published on the recorded ``user`` channel, as the real one would."""

    def on_order(order: Any) -> None:
        if not TrackingRawLog.instances:
            return
        raw = json.dumps({"channel": "user", "events": [{"type": "update", "orders": [world.venue.message(order)]}]})
        TrackingRawLog.instances[-1].write({"t": world.clock().timestamp(), "raw": raw})

    world.venue.listeners.append(on_order)


def test_the_whole_ladder_up_to_the_live_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = ScaledClock(scale=400.0)
    world = World(tmp_path, clock)
    TrackingRawLog.instances.clear()
    monkeypatch.setattr(runner_module, "RawLog", TrackingRawLog)
    feed_the_channel(world)
    world.venue.errors = []

    def step(stage: str, *, phrase: bool = True, expect: int = 0) -> None:
        world.stdin = Tty(PHRASE if phrase else "")
        world.out.truncate(0)
        world.out.seek(0)
        code = world.run("preflight") if stage == "preflight" else world.run("run", "--stage", stage)
        assert code == expect, (stage, world.out.getvalue()[-1500:], world.err.getvalue()[-800:])

    step("preflight", phrase=False)
    step("preview")
    step("place-cancel")
    place = read_stage(world.out_dir, "place-cancel")
    assert place is not None and place.ok and place.details["user_messages"]["CANCELLED"]["seen"]
    step("expire")
    expire = read_stage(world.out_dir, "expire")
    assert expire is not None and expire.ok and expire.details["ledger_status"] == "EXPIRED"
    world.venue.errors = [OrderRejected("INVALID_LIMIT_PRICE_POST_ONLY", "would take liquidity")]
    step("reject")
    assert read_stage(world.out_dir, "reject").ok  # type: ignore[union-attr]

    # the STOP switch: the "operator" removes the file by hand after a few seconds of virtual time
    stop_file = tmp_path / "state" / "exec_probe" / "STOP"
    seen = {"n": 0}
    real_sleep = clock.sleep

    async def operator_sleep(seconds: float) -> None:
        await real_sleep(seconds)
        if stop_file.exists():
            seen["n"] += 1
            if seen["n"] >= 40:
                stop_file.unlink()  # (the test plays the operator; the code under test cannot do this)

    world.sleep = operator_sleep
    step("stop-switch")
    stop = read_stage(world.out_dir, "stop-switch")
    assert stop is not None and stop.ok and not stop_file.exists()
    assert world.runtimes and all(not rt.gateway.has_pending_entry(P) for rt in world.runtimes)
    # nothing but the stage orders (2 EUR) ever went out, and none of them was an entry of the probe
    assert all(s.base_size * (s.limit_price or D(0)) < D("2.1") for s in world.venue.creates("BUY"))
    assert not [order for order in world.venue.orders.values() if order.spec.side == "SELL"]


# ---------------------------------------------------------------------------------------------- live


def test_the_live_stage_runs_a_cycle_and_stops_cleanly(tmp_path: Path) -> None:
    clock = ScaledClock(scale=400.0)
    world = World(tmp_path, clock)
    cfg = make_cfg(tmp_path).model_copy(update={"execution": ExecutionConfig(reconcile_interval_s=0.3)})
    from modules.probe.stages import StageRecord, config_hash, write_stage

    for stage in ("preflight", "preview", "place-cancel", "expire", "reject", "stop-switch"):
        write_stage(
            world.out_dir,
            StageRecord(
                stage, True, clock(), config_hash(cfg.exec_probe), entry_mode="gtd" if stage == "preview" else None
            ),
        )
    clock.jump_to(clock().replace(hour=8, minute=59, second=50))
    world.venue.plan_fills(ENTRY9, (30, "1", "80000.00"))
    world.stdin = Tty(PHRASE)

    async def scenario() -> int:
        task = asyncio.create_task(execute_stage(cfg, world.deps(), "live", str(world.key)))
        rt: ProbeRuntime | None = None
        for _ in range(4000):
            await asyncio.sleep(0.01)
            if world.runtimes:
                rt = world.runtimes[0]
                pos = rt.ledger.view("probe", P)
                if pos.is_long and pos.bracket_order_id is not None:
                    break
        assert rt is not None and rt.ledger.view("probe", P).is_long, world.err.getvalue()
        assert rt.ledger.view("probe", P).bracket_order_id is not None
        world.venue.set_market("80500.00")  # the take profit fills; the reconciler books it on its next cycle
        for _ in range(4000):
            await asyncio.sleep(0.01)
            if not rt.ledger.view("probe", P).is_long:
                break
        assert not rt.ledger.view("probe", P).is_long
        rt.stop_event.set()
        return await asyncio.wait_for(task, timeout=60)

    code = asyncio.run(scenario())
    assert code == 0
    live = read_stage(world.out_dir, "live")
    assert live is not None and live.ok
    rt = world.runtimes[0]
    assert rt.streaks.sl_streak == 0 and rt.marker.read() is None
    status = json.loads((world.out_dir / "status.json").read_text())
    assert status["position"]["side"] == "FLAT" and status["counts"]["entries_total"] == 1
    kinds = [json.loads(line)["kind"] for line in (world.out_dir / "events.jsonl").read_text().splitlines()]
    assert "probe_entry" in kinds and "bracket_open_latency" in kinds and "trade_closed" in kinds


def test_an_unexpected_error_prints_no_traceback_and_no_secret(world: World) -> None:
    async def boom() -> None:
        raise RuntimeError(
            "request failed: Bearer eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl organizations/o/apiKeys/k1"
        )

    world.venue.get_api_key_permissions = boom  # type: ignore[assignment]
    assert world.run("preflight") == 1
    text = world.err.getvalue()
    assert "ERRORE: RuntimeError" in text and "eyJhbGci" not in text and "apiKeys/k1" not in text
    assert "Traceback" not in text


def write_ladder(world: World, cfg: Any, clock: ScaledClock) -> None:
    from modules.probe.stages import StageRecord, config_hash, write_stage

    for stage in ("preflight", "preview", "place-cancel", "expire", "reject", "stop-switch"):
        record = StageRecord(
            stage, True, clock(), config_hash(cfg.exec_probe), entry_mode="gtd" if stage == "preview" else None
        )
        write_stage(world.out_dir, record)


def test_a_finished_probe_with_nothing_open_stops_by_itself(tmp_path: Path) -> None:
    clock = ScaledClock(scale=400.0)
    world = World(tmp_path, clock)
    cfg = make_cfg(tmp_path)
    write_ladder(world, cfg, clock)
    from modules.probe.control import FinishMarker

    FinishMarker(world.out_dir).write("anomaly_loss", "a test", clock())
    world.stdin = Tty(PHRASE)
    code = asyncio.run(asyncio.wait_for(execute_stage(cfg, world.deps(), "live", str(world.key)), timeout=30))
    assert code == 0 and "terminata" in world.err.getvalue()
    kinds = [json.loads(x)["kind"] for x in (world.out_dir / "events.jsonl").read_text().splitlines()]
    assert "probe_idle_stop" in kinds
    assert not world.venue.creates()  # nothing was sent


def test_a_live_run_with_all_the_entries_used_stops_by_itself(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from modules.ledger.positions import OrderRecord, PositionLedger

    clock = ScaledClock(scale=400.0)
    world = World(tmp_path, clock)
    cfg = make_cfg(tmp_path)
    write_ladder(world, cfg, clock)

    async def seed() -> None:
        led = await PositionLedger.open(Path(cfg.exec_probe.db_path), "live")
        now = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
        for i in range(5):
            rec = OrderRecord(f"e{i}", "probe", P, "BUY", "entry", "EXPIRED", now, now, f"x{i}", f"probe:BTC-EUR:{i}")
            await led.record_order(rec)
        await led.close()

    asyncio.run(seed())
    world.stdin = Tty(PHRASE)
    code = asyncio.run(asyncio.wait_for(execute_stage(cfg, world.deps(), "live", str(world.key)), timeout=30))
    assert code == 0 and not world.venue.creates()
