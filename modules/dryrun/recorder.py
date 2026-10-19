"""Raw WebSocket recorder and the P0 stream soak (spec sez. 5.3 punto 9, sez. 0.5 criterio di uscita P0).

``WsRecorder`` writes every raw message with its arrival time to
``<root>/YYYY-MM-DD/<channel>-<product>.jsonl.gz``, one line per message: ``<arrival epoch>\\t<raw JSON>``.
The writer runs in its own thread so that disk and compression never slow the event loop; data is flushed to disk
every ``flush_s`` seconds (a sync-flushed gzip stream stays readable after a crash), files rotate with the UTC day of
the arrival time, and day folders older than ``retention_days`` are deleted.

The recordings are what makes a day of market data reproducible (replay, what-if runs, fill-model calibration).

CLI:
    python -m modules.dryrun.recorder record --hours 48      # P0 soak: live feed + recorder + bar repair, with report
    python -m modules.dryrun.recorder verify --days 2        # sequence continuity of what was recorded
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import heapq
import json
import queue
import re
import shutil
import threading
import time
import zlib
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import IO, Any, Final

import structlog

log = structlog.get_logger(__name__)

DEFAULT_ROOT: Final = Path("data") / "ws_recordings"
_SEQ = re.compile(r'"sequence_num"\s*:\s*(\d+)')
_STOP: Final = object()


def utc_now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------------- routing


def route(raw: str) -> tuple[str, str]:
    """(channel, product) used in the file name. Messages without a single product go to ``all``."""
    try:
        d = json.loads(raw)
    except ValueError:
        return "unparsed", "all"
    if d.get("type") == "error":
        return "error", "all"
    channel = str(d.get("channel", "unknown"))
    for ev in d.get("events", []):
        if "product_id" in ev:
            return channel, str(ev["product_id"])
        for key in ("trades", "tickers"):
            items = ev.get(key)
            if items:
                return channel, str(items[0].get("product_id", "all"))
    return channel, "all"


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


# --------------------------------------------------------------------------------- recorder


@dataclass
class RecorderStats:
    written: int = 0
    dropped: int = 0
    bytes_in: int = 0
    files_opened: int = 0
    errors: list[str] = field(default_factory=list)


class WsRecorder:
    def __init__(
        self,
        root: Path = DEFAULT_ROOT,
        *,
        flush_s: float = 5.0,
        retention_days: int = 30,
        max_queue: int = 500_000,
    ) -> None:
        self.root = Path(root)
        self.stats = RecorderStats()
        self._flush_s = flush_s
        self._retention_days = retention_days
        self._q: queue.SimpleQueue[Any] = queue.SimpleQueue()
        self._max_queue = max_queue
        self._thread: threading.Thread | None = None
        self._open: dict[tuple[date, str, str], tuple[IO[bytes], Any]] = {}

    # ---- producer side (event loop thread): never blocks

    def record(self, recv_ts: float, raw: str) -> None:
        """Hand a message to the writer thread. Drops (and counts) only if the writer is hopelessly behind."""
        if self._q.qsize() >= self._max_queue:
            self.stats.dropped += 1
            return
        self._q.put((recv_ts, raw))

    def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.purge(utc_now().date())
        self._thread = threading.Thread(target=self._run, name="ws-recorder", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Flush everything still queued and close the files."""
        if self._thread is not None:
            self._q.put(_STOP)
            self._thread.join(timeout=30)
            self._thread = None

    # ---- writer thread

    def _run(self) -> None:
        last_flush = time.monotonic()
        current_day: date | None = None
        while True:
            try:
                item = self._q.get(timeout=self._flush_s)
            except queue.Empty:
                item = None
            if item is _STOP:
                break
            if item is not None:
                recv_ts, raw = item
                try:
                    day = datetime.fromtimestamp(recv_ts, UTC).date()
                    if current_day is not None and day != current_day:
                        self._close_day(current_day)
                        self.purge(day)
                    current_day = day
                    self._write(day, recv_ts, raw)
                except Exception as exc:  # a disk problem must not kill the thread; it is counted and reported
                    self.stats.errors.append(f"{type(exc).__name__}: {exc}")
                    log.error("ws_recorder_write_failed", error=str(exc))
            if time.monotonic() - last_flush >= self._flush_s:
                self._flush()
                last_flush = time.monotonic()
        self._flush()
        for key in list(self._open):
            self._close_key(key)

    def _write(self, day: date, recv_ts: float, raw: str) -> None:
        channel, product = route(raw)
        key = (day, channel, product)
        entry = self._open.get(key)
        if entry is None:
            folder = self.root / day.isoformat()
            folder.mkdir(parents=True, exist_ok=True)
            base = f"{_safe(channel)}-{_safe(product)}"
            path, part = folder / f"{base}.jsonl.gz", 0
            while path.exists():  # never append to a file a crash may have left with a truncated gzip member
                part += 1
                path = folder / f"{base}.part{part}.jsonl.gz"
            fh = open(path, "xb")  # noqa: SIM115
            entry = (fh, gzip.GzipFile(fileobj=fh, mode="wb"))
            self._open[key] = entry
            self.stats.files_opened += 1
        line = raw if "\n" not in raw and "\t" not in raw else json.dumps(json.loads(raw), separators=(",", ":"))
        data = f"{recv_ts:.6f}\t{line}\n".encode()
        entry[1].write(data)
        self.stats.written += 1
        self.stats.bytes_in += len(data)

    def _flush(self) -> None:
        for fh, gz in self._open.values():
            try:
                gz.flush()  # Z_SYNC_FLUSH: everything so far is readable even if the process dies now
                fh.flush()
            except Exception as exc:
                self.stats.errors.append(f"flush: {exc}")

    def _close_key(self, key: tuple[date, str, str]) -> None:
        fh, gz = self._open.pop(key)
        try:
            gz.close()
        finally:
            fh.close()

    def _close_day(self, day: date) -> None:
        for key in [k for k in self._open if k[0] == day]:
            self._close_key(key)

    # ---- retention

    def purge(self, today: date) -> list[str]:
        """Delete day folders older than ``retention_days``."""
        removed: list[str] = []
        if not self.root.exists():
            return removed
        cutoff = today - timedelta(days=self._retention_days)
        for folder in self.root.iterdir():
            try:
                day = date.fromisoformat(folder.name)
            except ValueError:
                continue
            if folder.is_dir() and day < cutoff:
                shutil.rmtree(folder, ignore_errors=True)
                removed.append(folder.name)
        return removed


# ---------------------------------------------------------------------------------- reading


def read_recording(path: Path) -> Iterator[tuple[float, str]]:
    """Yield ``(arrival epoch, raw message)``. A file cut short by a crash yields what is readable.

    A crash can leave a truncated gzip member that an older recorder then appended a new member to (every start now
    writes a new ``.partN`` file): the decompressor fails in the middle of the stream (``zlib.error``). What comes
    before the damage is returned; the messages of the members after it are NOT recovered by this reader."""
    with gzip.open(path, "rb") as fh:
        try:
            for line in fh:
                text = line.decode("utf-8").rstrip("\n")
                recv, _, raw = text.partition("\t")
                try:
                    arrival = float(recv)
                except ValueError:  # the decompressor produced garbage before it failed: this is the damage
                    return
                yield arrival, raw
        except (EOFError, gzip.BadGzipFile, zlib.error):
            return


def recorded_days(root: Path) -> list[date]:
    days = []
    for folder in root.iterdir() if root.exists() else []:
        try:
            days.append(date.fromisoformat(folder.name))
        except ValueError:
            continue
    return sorted(days)


def merged_messages(root: Path, days: Sequence[date]) -> Iterator[tuple[float, int, str]]:
    """All messages of the given days in arrival order: ``(arrival, sequence_num, raw)``."""

    def tagged(path: Path) -> Iterator[tuple[float, int, str]]:
        for recv, raw in read_recording(path):
            m = _SEQ.search(raw)
            yield recv, int(m.group(1)) if m else -1, raw

    streams = [tagged(p) for d in days for p in sorted((root / d.isoformat()).glob("*.jsonl.gz"))]
    return heapq.merge(*streams, key=lambda t: (t[0], t[1]))


# --------------------------------------------------------------------------------- verification


@dataclass
class SequenceReport:
    messages: int = 0
    connections: int = 0
    gaps: int = 0
    resets: int = 0
    lost_messages: int = 0
    max_silence_s: float = 0.0
    silences_over_5s: int = 0
    first: str | None = None
    last: str | None = None
    by_channel: dict[str, int] = field(default_factory=dict)
    gap_times: list[str] = field(default_factory=list)


def verify_sequence(messages: Iterator[tuple[float, int, str]], silence_s: float = 5.0) -> SequenceReport:
    """Walk the recorded messages in arrival order and measure continuity of ``sequence_num`` and of the flow."""
    rep = SequenceReport()
    last_seq: int | None = None
    last_recv: float | None = None
    channels: Counter[str] = Counter()
    for recv, seq, raw in messages:
        rep.messages += 1
        if rep.first is None:
            rep.first = datetime.fromtimestamp(recv, UTC).isoformat()
        rep.last = datetime.fromtimestamp(recv, UTC).isoformat()
        channels[route(raw)[0]] += 1
        if last_recv is not None:
            quiet = recv - last_recv
            rep.max_silence_s = max(rep.max_silence_s, quiet)
            rep.silences_over_5s += quiet > silence_s
        last_recv = recv
        if seq < 0:
            continue
        if last_seq is None:
            rep.connections = 1
        elif seq == last_seq + 1:
            pass
        elif seq <= last_seq:
            rep.resets += 1
            rep.connections += 1
        else:
            rep.gaps += 1
            rep.lost_messages += seq - last_seq - 1
            rep.gap_times.append(datetime.fromtimestamp(recv, UTC).isoformat())
        last_seq = seq
    rep.by_channel = dict(channels)
    rep.max_silence_s = round(rep.max_silence_s, 3)
    return rep


# --------------------------------------------------------------------------------- the soak run

PARITY_RTOL: Final = 1e-9  # |a-b| <= 1e-9 * max(1, |b|), spec 2.3 A.3


@dataclass
class SoakReport:
    started_at: str
    updated_at: str
    target_hours: float
    elapsed_hours: float
    finished: bool
    products: list[str]
    stream: dict[str, Any]
    recorder: dict[str, Any]
    damage_intervals: int
    bars: dict[str, dict[str, Any]]
    checks: dict[str, Any] = field(default_factory=dict)
    verdict: dict[str, bool] = field(default_factory=dict)


def compare_rows(inc: Any, ref: Any) -> dict[str, Any]:
    """Row-by-row comparison of the features the live engine produced with the batch ones (same index)."""
    import numpy as np

    a, b = inc.to_numpy(dtype=float), ref.to_numpy(dtype=float)
    nan_a, nan_b = np.isnan(a), np.isnan(b)
    both = ~nan_a & ~nan_b
    err = np.abs(a - b)[both] / np.maximum(1.0, np.abs(b)[both])
    return {
        "rows": len(inc),
        "rows_all_columns_available": int((~nan_b.any(axis=1)).sum()),
        "nan_mismatch_cells": int((nan_a != nan_b).sum()),
        "max_relative_error": float(err.max()) if err.size else 0.0,
    }


def soak_verdict(
    *,
    elapsed_hours: float,
    target_hours: float,
    recorder_errors: int,
    recorder_dropped: int,
    parse_errors: int,
    parity: dict[str, dict[str, Any]],
    revisions: dict[str, dict[str, Any]],
    max_revision_ratio: float,
    seconds_behind: dict[str, float],
    products: Sequence[str] | None = None,
    catch_up_s: float = 600.0,
) -> dict[str, bool]:
    """One boolean per P0 exit criterion, from measured numbers. A criterion with nothing measured is False, and
    when ``products`` is given every product must have been measured."""
    wanted = set(products) if products is not None else None

    def covered(measured: dict[str, Any]) -> bool:
        return bool(measured) and (wanted is None or wanted <= set(measured))

    return {
        "hours_reached": elapsed_hours >= target_hours * 0.99,
        "recorder_without_errors": recorder_errors == 0 and recorder_dropped == 0,
        "no_parse_errors": parse_errors == 0,
        "engine_matches_batch_on_stored_bars": covered(parity)
        and all(c["nan_mismatch_cells"] == 0 and c["max_relative_error"] <= PARITY_RTOL for c in parity.values()),
        "rest_revision_ratio_within_limit": covered(revisions)
        and all(r["ratio"] <= max_revision_ratio for r in revisions.values()),
        "bars_caught_up": bool(seconds_behind) and all(v < catch_up_s for v in seconds_behind.values()),
    }


async def run_soak(
    products: Sequence[str],
    hours: float,
    root: Path,
    report_path: Path,
    data_dir: Path,
    report_every_s: float = 600.0,
) -> SoakReport:
    """The P0 pipeline for ``hours``: incremental backfill -> engine warm-up -> live feed + raw recorder +
    ``BarService`` (REST bars at T + S, persisted to the store) -> indicator engine on every published bar.

    At the end it measures what P0 demands instead of asserting it: parity of the live engine with the batch
    features of the stored series (invariant I2) and the REST revision ratio of the minutes it stored.
    """
    import pandas as pd
    from aiolimiter import AsyncLimiter

    from modules.config import AppConfig
    from modules.market.backfill import (
        Backfiller,
        BarStore,
        CoinbaseCandleSource,
        candles_to_frame,
        load_bars,
    )
    from modules.market.bars import BarClosed, BarFinalizer, BarService, BarStoreWriter, rest_revisions
    from modules.market.indicators import IndicatorEngine
    from modules.market.indicators_batch import compute_indicators
    from modules.market.ws_feed import CoinbaseMarketStream

    cfg = AppConfig()
    bars_cfg = cfg.market.bars
    root_1m = data_dir / "bars" / "1m"
    store = BarStore(root_1m)
    source = CoinbaseCandleSource()
    started = utc_now()

    await Backfiller(
        store=store,
        source=source,
        limiter=AsyncLimiter(cfg.market.backfill.rate_limit_per_s, 1),
        cfg=cfg.market.backfill,
        settle_s=bars_cfg.rest_delay_s,
    ).run(products, datetime.fromisoformat(cfg.market.backfill.start).replace(tzinfo=UTC))

    finalizer = BarFinalizer(products, bars_cfg.rest_delay_s, bars_cfg.rest_max_delay_s)
    engines: dict[str, IndicatorEngine] = {}
    complete: dict[str, datetime] = {}
    first_live: dict[str, datetime] = {}
    for pid in products:
        history = load_bars(root_1m, pid)  # the whole history: the anchor of the recurrences (spec 2.2.1.b)
        engines[pid] = IndicatorEngine(pid)
        engines[pid].warm_up(history)
        last = history.index[-1].to_pydatetime()
        finalizer.seed(pid, last, float(history["close"].iloc[-1]))
        complete[pid], first_live[pid] = last, last

    recorder = WsRecorder(root)
    recorder.start()
    damage: list[tuple[datetime, datetime | None]] = []
    stream = CoinbaseMarketStream(
        products, raw_tap=recorder.record, on_damage=lambda start, end: damage.append((start, end))
    )
    writer = BarStoreWriter(store, complete, bars_cfg.store_flush_minutes)
    queue: asyncio.Queue[BarClosed] = asyncio.Queue()
    service = BarService(finalizer, queue, source, products=products, store_writer=writer, retry_s=bars_cfg.retry_s)
    by_source: dict[str, Counter[str]] = {p: Counter() for p in products}
    lags: dict[str, list[float]] = {p: [] for p in products}
    rows: dict[str, list[tuple[datetime, dict[str, float]]]] = {p: [] for p in products}

    def bars_section() -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for p in products:
            ls = sorted(lags[p])
            out[p] = {
                **dict(by_source[p]),
                "lag_s_p50": ls[len(ls) // 2] if ls else None,
                "lag_s_p99": ls[int(len(ls) * 0.99)] if ls else None,
                "fetch_failures": service.stats.fetch_failures[p],
                "next_minute": finalizer.next_minute(p).isoformat(),
            }
        return out

    def snapshot(finished: bool, checks: dict[str, Any] | None = None) -> SoakReport:
        now = utc_now()
        return SoakReport(
            started_at=started.isoformat(),
            updated_at=now.isoformat(),
            target_hours=hours,
            elapsed_hours=round((now - started).total_seconds() / 3600, 3),
            finished=finished,
            products=list(products),
            stream=asdict(stream.stats),
            recorder=asdict(recorder.stats),
            damage_intervals=len(damage),
            bars=bars_section(),
            checks=checks or {},
        )

    async def drain() -> None:
        while True:
            e = await queue.get()
            by_source[e.product_id][e.source] += 1
            lags[e.product_id].append(e.lag_s)
            rows[e.product_id].append((e.bar.ts, engines[e.product_id].update(e.bar).as_row()))
            del rows[e.product_id][:-5000]

    async def reporter() -> None:
        while True:
            await asyncio.sleep(report_every_s)
            _write_report(report_path, snapshot(False))

    tasks = [asyncio.create_task(c) for c in (stream.run(), service.run(), drain(), reporter())]
    try:
        await asyncio.sleep(hours * 3600)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await writer.flush()
        recorder.stop()

    checks: dict[str, Any] = {"parity": {}, "rest_revisions": {}}
    for pid in products:
        if not rows[pid]:
            continue
        stamps = [ts for ts, _ in rows[pid]]
        index = pd.DatetimeIndex(stamps).astype("datetime64[ns, UTC]")
        stored = load_bars(root_1m, pid, store_complete_until_end=True)
        stored = stored.loc[stored.index <= stamps[-1]]
        batch = compute_indicators(stored).loc[index]
        live = pd.DataFrame([r for _, r in rows[pid]], index=index)[list(batch.columns)]
        checks["parity"][pid] = compare_rows(live, batch)
        # Only minutes at least 15 min old are re-read: candles are stable well before that (measured), and a younger
        # one could still be settling (spec 0.5, criterio di uscita P0).
        lo = first_live[pid] + timedelta(minutes=1)
        hi = min(stamps[-1] + timedelta(minutes=1), utc_now().replace(second=0, microsecond=0) - timedelta(minutes=15))
        if hi <= lo:
            continue
        fresh_raw: list[dict[str, Any]] = []
        cursor = lo
        while cursor < hi:
            upper = min(cursor + timedelta(minutes=300), hi)
            fresh_raw += await asyncio.to_thread(source.fetch, pid, int(cursor.timestamp()), int(upper.timestamp()))
            cursor = upper
        fresh = candles_to_frame(fresh_raw, int(lo.timestamp()), int(hi.timestamp()))
        stored_frame = pd.concat([store.read_month(pid, *m) for m in store.months(pid)])
        checks["rest_revisions"][pid] = asdict(rest_revisions(pid, stored_frame, fresh, lo, hi))

    final = snapshot(True, checks)
    final.verdict = soak_verdict(
        elapsed_hours=final.elapsed_hours,
        target_hours=hours,
        recorder_errors=len(recorder.stats.errors),
        recorder_dropped=recorder.stats.dropped,
        parse_errors=stream.stats.parse_errors,
        parity=checks["parity"],
        revisions=checks["rest_revisions"],
        max_revision_ratio=cfg.dry_run.gate_ops.max_rest_revision_ratio,
        seconds_behind={p: (utc_now() - finalizer.next_minute(p)).total_seconds() for p in products},
        products=list(products),
    )
    _write_report(report_path, final)
    return final


def _write_report(path: Path, report: SoakReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(report), indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Raw WebSocket recorder and P0 stream soak")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record", help="run the live feed with the recorder for N hours")
    rec.add_argument("--hours", type=float, default=48.0)
    rec.add_argument("--products", nargs="+")
    rec.add_argument("--out", default=str(DEFAULT_ROOT))
    rec.add_argument("--data-dir", default="data")
    rec.add_argument("--report", default=str(Path("data") / "reports" / "p0_soak.json"))
    ver = sub.add_parser("verify", help="check sequence continuity of the recordings")
    ver.add_argument("--days", type=int, default=2, help="last N recorded days")
    ver.add_argument("--out", default=str(DEFAULT_ROOT))
    ver.add_argument("--report", default=None)
    args = ap.parse_args(argv)

    if args.cmd == "record":
        from modules.config import AppConfig

        products = args.products or list(AppConfig().market.products)
        asyncio.run(run_soak(products, args.hours, Path(args.out), Path(args.report), Path(args.data_dir)))
        return 0

    root = Path(args.out)
    days = recorded_days(root)[-args.days :]
    if not days:
        print(f"no recordings under {root}")
        return 1
    report = verify_sequence(merged_messages(root, days))
    text = json.dumps(asdict(report), indent=2)
    print(f"days: {[d.isoformat() for d in days]}\n{text}")
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
