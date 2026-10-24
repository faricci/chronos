"""script/user_channel_soak.py: the read-only soak of the ``user`` channel, against a fake SDK and a fake exchange.

No network, no keys. The fake transport imitates what the real SDK does (its own thread, ``on_close`` called by
``close()`` only, a dead socket makes ``close()`` raise), so the reconnection scenarios exercise the real
``CoinbaseUserStream`` the way the real feed does.
"""

from __future__ import annotations

import gzip
import json
import os
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from modules.execution import user_stream
from modules.execution.balance import AccountSnapshot
from modules.execution.exchange import KeyPermissionsInfo
from script import user_channel_soak as soak
from tests.coverage_fixtures import RAW_SOL
from tests.execution_fakes import FakeExchange, info

D = Decimal
SECRET_KEY_NAME = "organizations/aaaa-bbbb/apiKeys/cccc-dddd"
PEM_BODY = "MHcCAQEEIAbcdefghijklmnopqrstuvwxyz0123456789ABCDEF"
SECRET_PEM = f"-----BEGIN EC PRIVATE KEY-----\n{PEM_BODY}\n-----END EC PRIVATE KEY-----\n"
JWT = "eyJhbGciOiJFUzI1NiIsImtpZCI6Im9yZ2FuaXphdGlvbnMifQ.eyJzdWIiOiJhYmMiLCJpc3MiOiJjZHAifQ.c2lnbmF0dXJlMTIzNDU2"


class SilentClock:
    """``FakeExchange`` wants a callable returning an aware datetime."""

    def __call__(self) -> datetime:
        return datetime.now(UTC)


class SoakFakeExchange(FakeExchange):
    """The reads of FakeExchange plus the two calls the soak makes before the stream starts."""

    def __init__(self, *, can_trade: bool = False, can_transfer: bool = False) -> None:
        super().__init__(SilentClock())  # type: ignore[arg-type]
        self.perms = KeyPermissionsInfo(True, can_trade, can_transfer, "DEFAULT")
        self.balances = {"EUR": ("1000", "0"), "SOL": ("0", "0")}

    async def get_api_key_permissions(self) -> KeyPermissionsInfo:
        return self.perms

    async def get_product_raw(self, product_id: str) -> dict[str, Any]:
        return {**RAW_SOL, "product_id": product_id, "base_currency_id": product_id.split("-")[0]}


def message(channel: str, seq: int, events: list[dict[str, Any]]) -> str:
    return json.dumps(
        {"channel": channel, "client_id": "", "timestamp": datetime.now(UTC).isoformat(), "sequence_num": seq,
         "events": events}  # fmt: skip
    )


class Feed:
    """What the fake exchange server sends. ``jam_at`` skips one sequence number on the first connection."""

    def __init__(self, period: float = 0.02) -> None:
        self.period = period
        self.jam_at: int | None = None
        self.extra: list[str] = []  # raw messages sent right after the snapshot (seq numbers are NOT rewritten)
        self.transports: list[FakeSdk] = []
        self.opens = 0


class FakeSdk:
    """Imitates ``WSUserClient`` behind ``CoinbaseUserTransport``: a thread that calls ``on_message``; ``close()``
    calls ``on_close`` (like ``close_async``) and raises if the socket is already dead."""

    def __init__(self, feed: Feed, on_message: Callable[[str], None], on_close: Callable[[], None]) -> None:
        self.feed, self.on_message, self.on_close = feed, on_message, on_close
        self.alive = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        feed.transports.append(self)

    def open(self) -> None:
        self.feed.opens += 1
        self.alive = True

    def subscribe(self, product_ids: Sequence[str], channels: Sequence[str]) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        seq = 0
        first = self.feed.transports[0] is self

        def send(raw: str) -> None:
            if self.alive and not self._stop.is_set():
                self.on_message(raw)

        send(message("user", seq, [{"type": "snapshot", "orders": [], "positions": {}}]))
        for raw in self.feed.extra:
            send(raw)
        seq += 1
        send(message("subscriptions", seq, [{"subscriptions": {"user": ["portfolio"]}}]))
        seq += 1
        send(message("subscriptions", seq, [{"subscriptions": {"heartbeats": ["heartbeats"], "user": ["p"]}}]))
        counter = 1000
        while not self._stop.wait(self.feed.period):
            seq += 1
            if first and self.feed.jam_at == seq:
                seq += 1  # one message lost on the way
            counter += 1
            send(message("heartbeats", seq, [{"current_time": "x", "heartbeat_counter": counter}]))

    def close(self) -> None:
        if not self.alive:
            raise RuntimeError("Event loop is not running.")
        self.alive = False
        self._stop.set()
        threading.Thread(target=self.on_close).start()

    def abort(self) -> None:
        self.alive = False  # the socket dies; nobody is told
        self._stop.set()


def make_options(tmp_path: Path, duration_s: float = 1.0, **kw: Any) -> soak.SoakOptions:
    return soak.SoakOptions(
        out=tmp_path / "out", duration_s=duration_s, products=("SOL-EUR",), reconcile_interval_s=0.2,
        silence_s=0.15, progress_s=0.3, heartbeat_s=0.02, **kw,
    )  # fmt: skip


def run(
    opts: soak.SoakOptions, exchange: SoakFakeExchange, feed: Feed, secrets: Sequence[str] = ()
) -> tuple[int, dict[str, Any]]:
    import asyncio

    def make(on_message: Callable[[str], None], on_close: Callable[[], None]) -> FakeSdk:
        return FakeSdk(feed, on_message, on_close)

    code = asyncio.run(soak.run_soak(opts, exchange, make, secrets=secrets))  # type: ignore[arg-type]
    report_path = opts.out / soak.REPORT_JSON
    return code, json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(user_stream, "BACKOFF_MIN_S", 0.01)
    monkeypatch.setattr(user_stream, "SUPERVISE_S", 0.05)


# ------------------------------------------------------------------------------------------ the run


def test_a_view_key_run_reports_authentication_subscriptions_cadence_and_the_reconciler(tmp_path: Path) -> None:
    exchange = SoakFakeExchange()
    exchange.open = [info("manual-1"), info("manual-2", side="SELL", limit="100")]
    exchange.balances = {"EUR": ("123.456789", "0"), "SOL": ("7.654321", "0.5")}
    code, report = run(make_options(tmp_path, 1.2), exchange, Feed())
    assert code == 0, {k: v for k, v in report["checks"].items() if not v["pass"]}
    assert report["authentication"]["ok"] and report["subscriptions_confirmed"] == ["heartbeats", "user"]
    assert report["key"] == {"can_view": True, "can_trade": False, "can_transfer": False, "portfolio_type": "DEFAULT"}
    by_type = report["messages"]["by_type"]
    assert by_type["user/snapshot"] == 1 and by_type["subscriptions"] == 2 and by_type["heartbeats"] > 20
    assert report["heartbeats"]["interval_s"]["median"] == pytest.approx(0.02, abs=0.02)
    assert report["heartbeats"]["counter_jumps"] == 0 and report["heartbeats"]["counter_seen"]
    assert report["sequence"]["gap_count"] == 0 and report["sequence"]["first_sequence_num_per_connection"] == [0]
    assert report["parse"]["errors"] == 0 and report["parse"]["stream_errors"] == 0
    assert [r["reason"] for r in report["reconcile_requests"]] == ["ws_connected"]
    # the real Reconciler.on_startup() against the empty ledger: every open order is foreign, only counts leave
    startup = report["reconciler_startup"]
    assert startup["error"] is None and startup["report"]["snapshot_applied"]
    assert startup["report"]["foreign_orders"] == 2
    cmp = startup["comparison"]
    assert cmp["exchange_open_orders"] == 2 and cmp["ledger_orders"] == 0 and cmp["book_reservations"] == 0
    assert cmp["currencies_with_balance"] == ["EUR", "SOL"] and cmp["currencies_with_hold"] == ["SOL"]
    assert cmp["book_free_differs_from_available"] == []
    assert report["reconciler_runtime"]["last_error"] is None
    text = "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "out").glob("*") if p.suffix != ".gz")
    assert "123.456789" not in text and "7.654321" not in text  # no amount reaches a report or the log
    assert (tmp_path / "out" / soak.REPORT_MD).read_text(encoding="utf-8").startswith("# Soak del canale `user`")


def test_the_raw_record_has_every_message_and_the_parser_saw_them_all(tmp_path: Path) -> None:
    code, report = run(make_options(tmp_path, 0.6), SoakFakeExchange(), Feed())
    raw = tmp_path / "out" / soak.RAW_NAME
    with gzip.open(raw, "rt", encoding="utf-8") as fh:
        lines = [json.loads(line) for line in fh]
    assert len(lines) == report["messages"]["total"] and all(line["conn"] == 1 for line in lines)
    assert [line["msg"]["sequence_num"] for line in lines] == list(range(len(lines)))
    assert code == 0


def test_a_key_that_can_trade_or_transfer_is_refused_before_anything_is_connected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for flags in ({"can_trade": True}, {"can_transfer": True}):
        feed = Feed()
        code, _ = run(make_options(tmp_path), SoakFakeExchange(**flags), feed)
        assert code == soak.EXIT_WRITE_KEY == 4
        assert feed.opens == 0 and feed.transports == []
        assert not (tmp_path / "out").exists()  # not even the folder
    assert "REFUSED" in capsys.readouterr().err


def test_an_existing_report_is_not_overwritten_without_the_flag(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / soak.REPORT_JSON).write_text("{}", encoding="utf-8")
    feed = Feed()
    code, _ = run(make_options(tmp_path), SoakFakeExchange(), feed)
    assert code == soak.EXIT_CONFIG and feed.opens == 0 and (out / soak.REPORT_JSON).read_text(encoding="utf-8") == "{}"


def test_nothing_is_written_outside_the_output_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("TMP", str(tmp_path / "scratch"))
    code, _ = run(make_options(tmp_path, 0.8, faults=(soak.Fault("abort", 0.3),)), SoakFakeExchange(), Feed())
    assert code in (0, 1)
    assert sorted(os.listdir(cwd)) == []  # no data/, state/, telemetry/ or database file next to the process
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == sorted(
        [soak.RAW_NAME, soak.REPORT_JSON, soak.REPORT_MD]
    )
    assert not list((tmp_path / "out").glob("*.tmp"))


def test_the_reconciler_and_the_sink_can_only_read_the_exchange() -> None:
    tap = soak.ReadTap(SoakFakeExchange())
    for name in ("create_order", "cancel_orders", "preview_order", "edit_order", "move_funds"):
        with pytest.raises(AssertionError, match="may only read"):
            getattr(tap, name)
    source = Path(soak.__file__).read_text(encoding="utf-8")
    assert "import coinbase" not in source and "from coinbase" not in source  # the SDK is reached via the module only


# --------------------------------------------------------------------------------------- redaction


def test_redact_removes_jwts_pem_key_names_bearer_tokens_and_known_secrets() -> None:
    text = (
        f'{{"jwt":"{JWT}","k":"{SECRET_KEY_NAME}","m":"Bearer {JWT}","pem":"{SECRET_PEM.replace(chr(10), "\\n")}",'
        f'"x":"my-api-secret-value","retail_portfolio_id":"abc"}} and a bare {JWT}'
    )
    clean = soak.redact(text, secrets=["my-api-secret-value"])
    for leaked in (JWT, SECRET_KEY_NAME, "my-api-secret-value", "MHcCAQEE", "BEGIN EC PRIVATE KEY", "eyJhbGci"):
        assert leaked not in clean
    assert clean.count(soak.REDACTED) >= 5


def test_secrets_of_covers_the_forms_a_pem_can_take() -> None:
    from modules.config import CoinbaseConfig

    cfg = CoinbaseConfig(api_key=SECRET_KEY_NAME, api_secret=SECRET_PEM)  # type: ignore[arg-type]
    forms = soak.secrets_of(cfg)
    assert SECRET_KEY_NAME in forms and SECRET_PEM in forms and SECRET_PEM.replace("\n", "\\n") in forms
    assert "MHcCAQEEIAbcdefghijklmnopqrstuvwxyz0123456789ABCDEF" in forms  # the body line alone


def test_tokens_never_reach_the_raw_record_the_report_or_the_log(tmp_path: Path) -> None:
    feed = Feed()
    feed.extra = [
        json.dumps({"type": "error", "message": f"authentication failure for {SECRET_KEY_NAME} token {JWT}"}),
        message("user", 99, [{"type": "update", "orders": [], "jwt": JWT, "note": "my-api-secret-value"}]),
    ]
    code, report = run(make_options(tmp_path, 0.5), SoakFakeExchange(), feed, secrets=["my-api-secret-value"])
    assert code in (0, 1)
    blobs = [p.read_bytes() for p in (tmp_path / "out").glob("*") if p.suffix != ".gz"]
    with gzip.open(tmp_path / "out" / soak.RAW_NAME, "rb") as fh:
        blobs.append(fh.read())
    for blob in blobs:
        for leaked in (JWT.encode(), SECRET_KEY_NAME.encode(), b"my-api-secret-value", b"eyJhbGci"):
            assert leaked not in blob
    assert any("authentication failure" in e for c in report["connections"] for e in c["errors"])


# ------------------------------------------------------------------------------- gaps and parse errors


def test_a_sequence_gap_is_reported_and_asks_for_a_reconciliation(tmp_path: Path) -> None:
    feed = Feed()
    feed.jam_at = 8
    code, report = run(make_options(tmp_path, 0.8), SoakFakeExchange(), feed)
    assert report["sequence"]["gap_count"] == 1
    gap = report["sequence"]["gaps"][0]
    assert (gap["connection"], gap["got"] - gap["after"]) == (1, 2)
    assert "sequence_gap" in [r["reason"] for r in report["reconcile_requests"]]
    assert report["stream_stats"]["gaps"] == 1
    assert not report["checks"]["sequence_continuous"]["pass"] and code == soak.EXIT_CHECKS_FAILED
    assert report["reconciler_runtime"]["last_reason"] is not None  # the reconciler really ran for the request


def test_a_message_the_parser_rejects_is_counted_and_fails_the_check(tmp_path: Path) -> None:
    feed = Feed()
    feed.extra = [
        json.dumps({"channel": "user", "timestamp": "2026-10-20T08:00:00Z", "sequence_num": 77, "events": [
            {"type": "update", "orders": [{"order_id": "o1", "status": ""}]}]}),
    ]  # fmt: skip
    code, report = run(make_options(tmp_path, 0.5), SoakFakeExchange(), feed)
    assert report["parse"]["errors"] == 1 and report["parse"]["stream_errors"] == 1
    assert not report["checks"]["no_parse_errors"]["pass"] and code == soak.EXIT_CHECKS_FAILED


# ---------------------------------------------------------------------------------- faults and recovery


def test_every_kind_of_fault_makes_the_stream_reconnect_exactly_once_and_recover(tmp_path: Path) -> None:
    faults = (
        soak.Fault("close", 0.4), soak.Fault("abort", 0.9), soak.Fault("silence", 1.4, 0.5),
        soak.Fault("refuse", 2.3, 0.4),
    )  # fmt: skip
    feed = Feed()
    code, report = run(make_options(tmp_path, 3.6, faults=faults), SoakFakeExchange(), feed)
    rec = report["reconnects"]
    assert [f["result"] for f in report["injected_faults"]] == ["applied"] * 4
    assert (rec["count"], rec["expected_from_injection"], rec["unexpected"]) == (4, 4, 0), rec
    recovered = [r for r in rec["recoveries"] if r["outcome"] == "recovered"]
    assert len(recovered) == 4 and all(r["outage_s"] < 1.5 for r in recovered)
    assert any(c["open_error"] for c in report["connections"])  # the refused attempts are on record
    assert [r["reason"] for r in report["reconcile_requests"] if r["reason"].startswith("ws_")][:1] == ["ws_connected"]
    ws_requests = [r["reason"] for r in report["reconcile_requests"] if r["reason"].startswith("ws_")]
    assert len(ws_requests) == 5  # the first connection plus one per reconnect
    assert report["messages"]["dropped_by_injection"] > 0
    assert report["checks"]["every_outage_recovered"]["pass"] and report["checks"]["no_unexpected_reconnects"]["pass"]
    assert code == 0, {k: v for k, v in report["checks"].items() if not v["pass"]}


def test_the_stream_stops_the_run_early_and_says_so_when_it_crashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(self: Any) -> None:
        raise RuntimeError("stream exploded")

    monkeypatch.setattr(user_stream.CoinbaseUserStream, "run", boom)
    started = time.monotonic()
    code, report = run(make_options(tmp_path, 30.0), SoakFakeExchange(), Feed())
    assert time.monotonic() - started < 10
    assert report["ended_reason"].startswith("crashed: user_stream: RuntimeError") and code == soak.EXIT_CHECKS_FAILED


# ------------------------------------------------------------------------------------------ plumbing


def test_parse_fault_accepts_the_documented_forms_and_rejects_the_rest() -> None:
    assert soak.parse_fault("abort@12") == soak.Fault("abort", 720.0)
    assert soak.parse_fault("silence@24:12") == soak.Fault("silence", 1440.0, 12.0)
    assert soak.parse_fault("refuse@0.5:20") == soak.Fault("refuse", 30.0, 20.0)
    for bad in ("abort", "explode@3", "silence@3", "refuse@3:0"):
        with pytest.raises(ValueError, match="fault"):
            soak.parse_fault(bad)


def test_summaries_use_nearest_rank_percentiles() -> None:
    assert soak.summarize([]) is None
    s = soak.summarize([float(i) for i in range(1, 101)])
    assert s is not None and (s["median"], s["p99"], s["max"], s["n"]) == (50.5, 99.0, 100.0, 100)


def test_the_cli_refuses_a_bad_fault_and_missing_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert soak.main(["--out", str(tmp_path / "o"), "--inject", "nonsense"]) == soak.EXIT_CONFIG
    monkeypatch.delenv("COINBASE_API_KEY", raising=False)
    monkeypatch.delenv("COINBASE_API_SECRET", raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here
    assert soak.main(["--out", str(tmp_path / "o")]) == soak.EXIT_CONFIG
    assert "no Coinbase credentials" in capsys.readouterr().err
    assert not (tmp_path / "o").exists()


def test_the_account_snapshot_type_used_by_the_tap_is_the_exchange_one() -> None:
    snap = AccountSnapshot(balances={"EUR": (D("1"), D("0")), "SOL": (D("0"), D("0"))}, taken_at=datetime.now(UTC))
    assert sorted(c for c, (a, h) in snap.balances.items() if a > 0 or h > 0) == ["EUR"]


def test_the_soak_log_drops_amounts_and_redacts_secrets(tmp_path: Path) -> None:
    import structlog

    path = tmp_path / "soak.log"
    handle = soak._configure_logging(path, ["my-api-secret-value"])
    try:
        structlog.get_logger("x").warning(
            "event", detail="ledger holds 123.456 SOL", reason=f"jwt {JWT}", note="my-api-secret-value", count=3
        )
    finally:
        handle.close()
        structlog.reset_defaults()
    text = path.read_text(encoding="utf-8")
    assert "123.456" not in text and JWT not in text and "my-api-secret-value" not in text
    assert json.loads(text)["count"] == 3 and json.loads(text)["detail"] == "<omitted>"
