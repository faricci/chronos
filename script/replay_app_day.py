"""Replay a recorded stretch of real market data through the REAL app (``modules.app.Runtime``), in a scratch copy.

What it proves: the same assembly that ``python -m modules.app`` runs (registry champion, ``ShadowBookManager`` with the
configured books, watchdogs, guards, ledger with ``closed_trades``, telemetry) decides and trades on a day that was
really recorded (``data/ws_recordings`` for the order books, the 1m store for the bars), and that the daily report and
the two gates can be computed from what it leaves. Optionally the process is stopped half way and started again on the
same ledger (``--restart-at``): open positions must continue to be managed.

SAFETY: it never writes to ``data/``. The recordings of the day and the bar store are COPIED into ``--work`` and every
path of the run (ledger, decisions, telemetry, models, registry) is under ``--work``. A recording or store that another
process is still writing is only read.

    python -m script.replay_app_day --work <scratch dir> --day 2026-10-18 --from 06:00 --to 12:00 \
        --restart-at 09:00 --variant demo

``--variant real``: the champion is the newest model of ``models/baseline`` (its policy is ``untunable``: no entries).
``--variant demo``: a COPY of that model whose label (barriers, horizon) and policy are replaced by short, permissive
ones, so that trades open and close within hours. It is a test of the MECHANICS (entries, brackets, exits, ledger,
report), not a strategy: its ``model_card.json`` says ``"demo": true``. ``random_entry`` draws BUY at
``--random-rate`` (its documented test setting) in both variants; with a real untunable champion the gate still
blocks it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import time
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from modules.app import Champion, Deps, Runtime, load_champion
from modules.config import AppConfig, CoinbaseConfig, load_config
from modules.decision.questions import build_questions
from modules.dryrun.books import BuyAndHoldDriver
from modules.dryrun.replay import ReplayMarketStream, ReplayResult, ReplayRunner, SimClock
from modules.execution.balance import Clock
from modules.market.backfill import CandleSource, load_bars
from modules.market.bars import BarService
from modules.risk.costs import CostParams, FeeRates
from modules.risk.label_config import LabelConfig
from modules.telemetry.status import EquityWriter, StatusWriter

MINUTE = timedelta(minutes=1)
DEMO_VERSION = "baseline-lgbm@2026-10-19.1"


# ------------------------------------------------------------------------------------------------ scratch copy


def copy_inputs(src: Path, work: Path, days: Sequence[date]) -> None:
    """Copy (read only on the source) the bar store, the recordings of ``days`` and the models into ``work``."""
    t0 = time.time()
    bars_dst = work / "data" / "bars"
    if not bars_dst.exists():
        shutil.copytree(src / "data" / "bars", bars_dst, ignore=shutil.ignore_patterns("*.tmp", ".*"))
    for d in days:
        dst = work / "data" / "ws_recordings" / d.isoformat()
        if not dst.exists():
            shutil.copytree(src / "data" / "ws_recordings" / d.isoformat(), dst)
    models_dst = work / "models" / "baseline"
    if not models_dst.exists():
        shutil.copytree(src / "models" / "baseline", models_dst)
    print(f"inputs copied into {work} in {time.time() - t0:.0f} s", flush=True)


def make_demo_model(work: Path, *, atr_tf: str, k_tp: float, k_sl: float, horizon_min: int) -> None:
    """A copy of the newest real model with a short label and a permissive policy (see the module docstring)."""
    root = work / "models" / "baseline"
    newest = sorted(p for p in root.glob("*@*") if (p / "model_card.json").is_file() and p.name != DEMO_VERSION)[-1]
    demo = root / DEMO_VERSION
    if demo.exists():
        shutil.rmtree(demo)
    shutil.copytree(newest, demo)
    raw = json.loads((demo / "label_config.json").read_text(encoding="utf-8"))
    raw.update(atr_tf=atr_tf, k_tp=k_tp, k_sl=k_sl, horizon_min=horizon_min, atr=f"ATR_14_{atr_tf}")
    label = LabelConfig(
        atr_tf=raw["atr_tf"], k_tp=k_tp, k_sl=k_sl, entry_mode=raw["entry_mode"], horizon_min=horizon_min,
        entry_timeout_min=raw["entry_timeout_min"], entry_first_bar=raw["entry_first_bar"],
    )  # fmt: skip
    fees = FeeRates(maker_bps=raw["fees_bps"]["maker"], taker_bps=raw["fees_bps"]["taker"], assumed=True)
    costs = CostParams(slippage_buffer_bps=raw["slip_bps"], stop_extra_slip_bps=raw["stop_extra_slip_bps"])
    sha = label.sha256(fees, costs, {k: float(v) for k, v in raw["spread_bps"].items()})
    raw["sha256"] = sha
    (demo / "label_config.json").write_text(json.dumps(raw, indent=2, sort_keys=True), encoding="utf-8")
    card = json.loads((demo / "model_card.json").read_text(encoding="utf-8"))
    card["model"] = DEMO_VERSION
    card["label_config"]["sha256"] = sha
    card["questions_version"] = build_questions(horizon_min, k_tp, k_sl, atr_tf).version_tag  # type: ignore[arg-type]
    card["demo"] = True
    card["demo_note"] = "label and policy replaced to exercise the mechanics; NOT a strategy"
    (demo / "model_card.json").write_text(json.dumps(card, indent=2, sort_keys=True), encoding="utf-8")
    policy = {
        "model": DEMO_VERSION, "tau_buy": 0.01, "tau_sell": 1.0, "c_min": 0.0, "p_be_buffer": 0.0,
        "regime_rule": "ATR1D_PCTILE365:5/35/65/95", "label_config_sha256": sha,
    }  # fmt: skip
    (demo / "final" / "policy.json").write_text(json.dumps(policy, indent=2), encoding="utf-8")


def write_config(work: Path, args: argparse.Namespace) -> Path:
    books: list[dict[str, Any]] = [
        {"name": "champion", "decider": "registry:champion", "starting_quote": "100.00"},
        {"name": "random_entry", "decider": "random", "match_frequency_of": "champion", "seed": 42,
         "starting_quote": "100.00", "forced_buy_rate": args.random_rate, "forced_edge": 0.9},
        {"name": "buy_and_hold", "decider": "buy_and_hold", "starting_quote": "100.00"},
    ]  # fmt: skip
    risk: dict[str, Any] = {}
    if args.variant == "demo":
        risk = {"min_net_edge_bps": 0, "ev_min_bps": -10000}
    cfg = {
        "market": {"products": list(args.products)},
        "paths": {
            "state_dir": str(work / "state"), "data_dir": str(work / "data"), "models_dir": str(work / "models"),
            "news_db": str(work / "state" / "news.db"), "live_db": str(work / "state" / "chronos_live.db"),
        },
        "risk": risk,
        "dry_run": {
            "db_path": str(work / "state" / "chronos_dry.db"), "books": books,
            "fill_model": {"path": str(work / "models" / "registry" / "fill_model.json")},
            "recording": {"enabled": False, "dir": str(work / "data" / "ws_recordings")},
        },
        "telemetry": {"dir": str(work / "telemetry"), "ops_interval_s": 60},
    }  # fmt: skip
    path = work / "config.yml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return path


def assert_scratch(cfg: AppConfig, work: Path) -> None:
    """Refuse to run if any path of the config is outside ``work`` (the real ``data/`` must never be written)."""
    paths = [
        cfg.paths.state_dir, cfg.paths.data_dir, cfg.paths.models_dir, cfg.paths.news_db, cfg.paths.live_db,
        cfg.dry_run.db_path, cfg.dry_run.fill_model.path, cfg.dry_run.recording.dir, cfg.telemetry.dir,
    ]  # fmt: skip
    for p in paths:
        if work.resolve() not in Path(p).resolve().parents and Path(p).resolve() != work.resolve():
            raise SystemExit(f"refusing to run: {p} is outside the scratch directory {work}")


# ------------------------------------------------------------------------------------------------ the replay


class NoCandles:
    def fetch(self, product_id: str, start_s: int, end_s: int) -> list[dict[str, Any]]:
        return []


class _SilentBars:
    async def run(self) -> None:
        await asyncio.Event().wait()


class AppReplayRunner(ReplayRunner):
    """``ReplayRunner`` for the whole app: a bar goes through ``Runtime.process_bars`` (every book, venue first, then
    the engine), then the watchdogs look at the horizon with the simulated clock, then the telemetry writers run."""

    def __init__(self, runtime: Runtime, clock: SimClock, stream: ReplayMarketStream, **kw: Any) -> None:
        first = runtime.books[0]
        assert first.engine is not None
        super().__init__(clock=clock, stream=stream, engine=first.engine, venue=first.shadow, warm_up=False, **kw)
        self._rt = runtime
        self._minutes_done = 0
        self._seen_quotes: dict[str, Any] = {}
        data = Path(runtime.cfg.paths.data_dir)
        self._equity = EquityWriter(
            Path(runtime.cfg.telemetry.equity_dir or data / "equity"), runtime._equity_rows,  # noqa: SLF001
            clock=runtime.deps.clock,
        )  # fmt: skip
        self._status = StatusWriter(Path(runtime.cfg.telemetry.dir), runtime.status, clock=runtime.deps.clock)

    async def _publish(
        self, service: BarService, queue: asyncio.Queue[Any], minute: datetime, result: ReplayResult
    ) -> None:
        self._stream.advance(self._pub(minute))
        events = await service.tick()
        while not queue.empty():
            queue.get_nowait()
        if not events:
            raise RuntimeError(f"the store did not give the bar of {minute.isoformat()}")
        result.bars += len(events)
        for processed in (await self._rt.process_bars(events)).values():
            result.decisions += processed
        await self._quiesce()
        await self._rt.pump_tickers(self._seen_quotes)  # the last prices: stale-data guard and the watchdogs' stop
        for book in self._rt.books:
            if book.watchdog is not None:
                await book.watchdog.check_horizons()
        await self._settle_watchdogs()
        self._minutes_done += 1
        if self._rt.ops is not None:
            self._rt._sample_ops()  # noqa: SLF001
            self._rt.ops.tick()
            await self._rt.ops.flush()
        if self._minutes_done % 5 == 0:
            await self._equity.write_once()
            await self._status.write_once()

    def _in_motion(self, product: str) -> bool:
        for book in self._rt.books:
            engine = book.engine
            if engine is not None:
                kind = engine.pending(product)
                if kind == "entry" and not book.shadow.has_pending_entry(product):
                    return True
                if kind == "exit" and not book.shadow.has_pending_exit(product):
                    return True
            driver = book.book.engine
            if isinstance(driver, BuyAndHoldDriver) and product in driver.busy:
                if not book.shadow.has_pending_entry(product):
                    return True
        return False

    async def _settle_watchdogs(self) -> None:
        """A watchdog exit is a task: wait until its order is armed at the venue, so the next bar can fill it."""
        for _ in range(400):
            waiting = False
            for book in self._rt.books:
                if book.watchdog is None:
                    continue
                for product, task in list(book.watchdog._tasks.items()):  # noqa: SLF001
                    if not task.done() and not book.shadow.has_pending_exit(product):
                        waiting = True
            if not waiting:
                return
            await asyncio.sleep(0.005)

    async def _finish(self, result: ReplayResult) -> None:
        for book in self._rt.books:
            for product in self._products:
                if book.shadow.has_pending_entry(product):
                    await book.shadow.cancel(product)
                    result.cancelled_at_end.append(f"{book.name}:{product}")
        await self._quiesce()
        await self._rt.manager.drain(timeout=0)


def build_deps(clock: SimClock, work: Path, holder: dict[str, Any], start: datetime) -> Deps:
    def history(root: Path, product: str) -> Any:
        return load_bars(root, product, end=start - MINUTE, store_complete_until_end=True)

    def stream(products: Sequence[str], tap: Any, on_damage: Any) -> ReplayMarketStream:
        holder["stream"] = ReplayMarketStream(products, clock, on_damage=on_damage)
        return holder["stream"]  # type: ignore[no-any-return]

    async def no_backfill(cfg: AppConfig, source: CandleSource, clk: Clock) -> None:
        return None

    return Deps(
        clock=clock, candle_source=NoCandles, backfill=no_backfill, history=history, stream=stream,
        bar_service=lambda *a, **k: _SilentBars(), champion=_champion_loader(),
    )  # fmt: skip


def _champion_loader() -> Any:
    def load(models_dir: Path, spec: str) -> Champion:
        return load_champion(models_dir, spec)

    return load


async def run_phase(
    cfg: AppConfig, work: Path, start: datetime, end: datetime, *, label: str, preroll: timedelta
) -> tuple[Runtime, ReplayResult, float]:
    clock = SimClock(start)
    holder: dict[str, Any] = {}
    t0 = time.time()
    runtime = await Runtime.start(cfg, {}, build_deps(clock, work, holder, start))
    print(f"[{label}] runtime built in {time.time() - t0:.0f} s; books: {[b.name for b in runtime.books]}", flush=True)
    for name, report in runtime.restore_reports.items():
        print(f"[{label}] restored {name}: brackets {list(report.restored)}, new {list(report.placed)}, "
              f"cancelled {len(report.cancelled)}, unprotected {list(report.unprotected)}", flush=True)  # fmt: skip
    runner = AppReplayRunner(
        runtime, clock, holder["stream"], products=cfg.market.products, recordings=work / "data" / "ws_recordings",
        bars_root=work / "data" / "bars" / "1m", bars=cfg.market.bars, spread_bps=lambda p, t: 0.0, preroll=preroll,
        timeout_s=120.0,
    )  # fmt: skip
    t1 = time.time()
    result = await runner.run(start, end)
    clock.advance_to(end + timedelta(seconds=cfg.market.bars.rest_delay_s))
    print(f"[{label}] replayed {result.bars} bars, {result.messages} messages in {time.time() - t1:.0f} s; "
          f"decisions {len(result.decisions)}, watchdog exits "
          f"{sum(b.watchdog.exits_started for b in runtime.books if b.watchdog is not None)}", flush=True)  # fmt: skip
    runtime.request_stop("end of the window")
    await runtime._shutdown()  # noqa: SLF001  (the clean stop of the app: cancel entries, flush, ops, close)
    return runtime, result, time.time() - t0


def summarize(rt: Runtime, label: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for b in rt.books:
        out[b.name] = {
            "open_positions": [p.product_id for p in rt.ledger.long_positions(b.name)],
            "cash": str(b.account.balances.total("EUR")), "fees_paid": str(b.account.fees_paid),
            "realized_gross": str(b.account.realized_pnl), "engine": None if b.engine is None else dict(
                decisions=b.engine.stats.decisions, intents=b.engine.stats.intents, errors=b.engine.stats.errors,
                skips=dict(b.engine.stats.skips), actions=dict(b.engine.stats.actions),
            ),
        }  # fmt: skip
    print(f"[{label}] " + json.dumps(out, default=str, indent=1), flush=True)
    return out


async def amain(args: argparse.Namespace) -> int:
    src, work = Path(args.src).resolve(), Path(args.work).resolve()
    day = date.fromisoformat(args.day)
    hh, mm = (int(x) for x in args.from_.split(":"))
    start = datetime(day.year, day.month, day.day, hh, mm, tzinfo=UTC)
    hh, mm = (int(x) for x in args.to.split(":"))
    end = datetime(day.year, day.month, day.day, hh, mm, tzinfo=UTC)
    restart = None
    if args.restart_at:
        hh, mm = (int(x) for x in args.restart_at.split(":"))
        restart = datetime(day.year, day.month, day.day, hh, mm, tzinfo=UTC)
    work.mkdir(parents=True, exist_ok=True)
    copy_inputs(src, work, [day])
    if args.variant == "demo":
        make_demo_model(work, atr_tf=args.atr_tf, k_tp=args.k_tp, k_sl=args.k_sl, horizon_min=args.horizon_min)
    config_path = write_config(work, args)
    cfg = load_config(config_path)
    cfg = cfg.model_copy(update={"coinbase": CoinbaseConfig()})
    assert_scratch(cfg, work)
    preroll = timedelta(minutes=args.preroll_min)
    phases = [(start, end)] if restart is None else [(start, restart), (restart, end)]
    for i, (a, b) in enumerate(phases):
        # every phase feeds the recording from the same instant: the order books need the snapshot of the connection
        rt, res, took = await run_phase(cfg, work, a, b, label=f"phase {i + 1}", preroll=preroll + (a - start))
        summarize(rt, f"phase {i + 1} end")
        print(f"[phase {i + 1}] took {took:.0f} s", flush=True)
    print(f"done. config: {config_path}", flush=True)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", default=".", help="repository root holding data/ and models/ (read only)")
    ap.add_argument("--work", required=True, help="scratch directory (everything is written here)")
    ap.add_argument("--day", required=True)
    ap.add_argument("--from", dest="from_", required=True, help="HH:MM UTC")
    ap.add_argument("--to", required=True, help="HH:MM UTC")
    ap.add_argument("--restart-at", default=None, help="HH:MM UTC: stop the app there and start it again")
    ap.add_argument("--variant", choices=("real", "demo"), default="demo")
    ap.add_argument("--random-rate", type=float, default=0.01, help="forced BUY probability (test setting)")
    ap.add_argument("--products", nargs="+", default=["BTC-EUR", "ETH-EUR", "SOL-EUR"])
    ap.add_argument("--atr-tf", default="1h")
    ap.add_argument("--k-tp", type=float, default=2.0)
    ap.add_argument("--k-sl", type=float, default=1.5)
    ap.add_argument("--horizon-min", type=int, default=60)
    ap.add_argument("--preroll-min", type=int, default=20, help="minutes of recording fed before the window")
    args = ap.parse_args(argv)
    from modules.app import collect_secrets, configure_logging

    configure_logging(collect_secrets(None, {}))
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
