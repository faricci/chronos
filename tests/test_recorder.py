from __future__ import annotations

import gzip
import json
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from modules.dryrun.recorder import (
    SequenceReport,
    WsRecorder,
    merged_messages,
    read_recording,
    recorded_days,
    route,
    verify_sequence,
)
from modules.market.ws_feed import CoinbaseMarketStream

D1 = datetime(2026, 10, 17, 10, 0, tzinfo=UTC)


def l2(seq: int, product: str = "BTC-EUR") -> str:
    ev = {
        "type": "update",
        "product_id": product,
        "updates": [{"side": "bid", "price_level": "1", "new_quantity": "2"}],
    }
    return json.dumps(
        {"channel": "l2_data", "timestamp": "2026-10-17T10:00:00.1Z", "sequence_num": seq, "events": [ev]}
    )


def trades(seq: int, product: str = "SOL-EUR") -> str:
    t = {"product_id": product, "trade_id": "7", "price": "1", "size": "1", "time": "2026-10-17T10:00:00.1Z"}
    return json.dumps(
        {"channel": "market_trades", "timestamp": "2026-10-17T10:00:00.1Z", "sequence_num": seq,
         "events": [{"type": "update", "trades": [t]}]}
    )  # fmt: skip


def ticker(seq: int, product: str = "ETH-EUR") -> str:
    t = {"product_id": product, "price": "1", "best_bid": "1", "best_ask": "2", "best_bid_quantity": "1",
         "best_ask_quantity": "1"}  # fmt: skip
    return json.dumps(
        {"channel": "ticker", "timestamp": "2026-10-17T10:00:00.1Z", "sequence_num": seq,
         "events": [{"type": "update", "tickers": [t]}]}
    )  # fmt: skip


def heartbeat(seq: int) -> str:
    return json.dumps(
        {"channel": "heartbeats", "timestamp": "2026-10-17T10:00:00.1Z", "sequence_num": seq,
         "events": [{"current_time": "x", "heartbeat_counter": seq}]}
    )  # fmt: skip


def at(day: datetime, seconds: float = 0.0) -> float:
    return day.timestamp() + seconds


# ---------------------------------------------------------------------------------- routing


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (l2(1), ("l2_data", "BTC-EUR")),
        (trades(1), ("market_trades", "SOL-EUR")),
        (ticker(1), ("ticker", "ETH-EUR")),
        (heartbeat(1), ("heartbeats", "all")),
        ('{"type":"error","message":"x"}', ("error", "all")),
        ("not json at all", ("unparsed", "all")),
    ],
)
def test_route(raw: str, expected: tuple[str, str]) -> None:
    assert route(raw) == expected


# ------------------------------------------------------------------------------ recorder


def finish(rec: WsRecorder) -> None:
    rec.stop()


def test_messages_round_trip_into_files_by_day_channel_and_product(tmp_path: Path) -> None:
    rec = WsRecorder(tmp_path)
    rec.start()
    rec.record(at(D1, 1.5), l2(1))
    rec.record(at(D1, 2.5), l2(2, "SOL-EUR"))
    rec.record(at(D1, 3.5), trades(3))
    rec.record(at(D1, 4.5), heartbeat(4))
    finish(rec)
    folder = tmp_path / "2026-10-17"
    assert sorted(p.name for p in folder.iterdir()) == [
        "heartbeats-all.jsonl.gz",
        "l2_data-BTC-EUR.jsonl.gz",
        "l2_data-SOL-EUR.jsonl.gz",
        "market_trades-SOL-EUR.jsonl.gz",
    ]  # fmt: skip
    got = list(read_recording(folder / "l2_data-BTC-EUR.jsonl.gz"))
    assert got == [(at(D1, 1.5), l2(1))]  # the raw text comes back byte for byte, with its arrival time
    assert rec.stats.written == 4 and rec.stats.dropped == 0 and not rec.stats.errors


def test_utc_day_rotation_follows_the_arrival_time(tmp_path: Path) -> None:
    rec = WsRecorder(tmp_path)
    rec.start()
    rec.record(at(datetime(2026, 10, 17, 23, 59, 59, tzinfo=UTC)), heartbeat(1))
    rec.record(at(datetime(2026, 10, 18, 0, 0, 1, tzinfo=UTC)), heartbeat(2))
    finish(rec)
    assert [d.isoformat() for d in recorded_days(tmp_path)] == ["2026-10-17", "2026-10-18"]
    assert len(list(read_recording(tmp_path / "2026-10-18" / "heartbeats-all.jsonl.gz"))) == 1


def test_data_is_readable_after_a_flush_without_stopping(tmp_path: Path) -> None:
    """A crash must not lose what was already flushed: the gzip stream is sync-flushed."""
    rec = WsRecorder(tmp_path, flush_s=0.1)
    rec.start()
    rec.record(at(D1), l2(1))
    path = tmp_path / "2026-10-17" / "l2_data-BTC-EUR.jsonl.gz"
    deadline = time.time() + 5
    got: list[tuple[float, str]] = []
    while time.time() < deadline and not got:
        time.sleep(0.05)
        if path.exists():
            got = list(read_recording(path))  # file still open for writing
    finish(rec)
    assert got == [(at(D1), l2(1))]


def test_a_file_cut_short_by_a_crash_still_yields_what_was_written(tmp_path: Path) -> None:
    path = tmp_path / "x.jsonl.gz"
    with gzip.open(path, "wb") as fh:
        fh.write(f"{at(D1):.6f}\t{l2(1)}\n".encode())
        fh.write(f"{at(D1, 1):.6f}\t{l2(2)}\n".encode())
    raw = path.read_bytes()
    path.write_bytes(raw[:-12])  # lose the gzip trailer and part of the data
    assert len(list(read_recording(path))) >= 1


def test_a_truncated_member_followed_by_an_appended_one_yields_what_precedes_the_damage(tmp_path: Path) -> None:
    """What the soak left on 2026-10-18: a run killed mid-write, then a restart appending a new gzip member."""
    path = tmp_path / "x.jsonl.gz"
    with gzip.open(path, "wb") as fh:
        for i in range(200):
            fh.write(f"{at(D1, i):.6f}\t{l2(i)}\n".encode())
    cut = path.read_bytes()[:-40]  # the first run died: its member has no trailer and misses its tail
    with gzip.open(tmp_path / "second.gz", "wb") as fh:
        fh.write(f"{at(D1, 999):.6f}\t{l2(999)}\n".encode())
    path.write_bytes(cut + (tmp_path / "second.gz").read_bytes())
    got = list(read_recording(path))  # must not raise
    assert len(got) >= 150 and got[:150] == [(at(D1, i), l2(i)) for i in range(150)]  # all that precedes the damage


def test_a_restart_writes_a_new_part_and_keeps_everything(tmp_path: Path) -> None:
    for seq in (1, 2, 3):  # three runs on the same day: each one has its own file
        rec = WsRecorder(tmp_path)
        rec.start()
        rec.record(at(D1, seq), l2(seq))
        finish(rec)
    folder = tmp_path / "2026-10-17"
    names = sorted(p.name for p in folder.glob("l2_data-BTC-EUR*"))
    assert names == ["l2_data-BTC-EUR.jsonl.gz", "l2_data-BTC-EUR.part1.jsonl.gz", "l2_data-BTC-EUR.part2.jsonl.gz"]
    assert [raw for _, _, raw in merged_messages(tmp_path, [D1.date()])] == [l2(1), l2(2), l2(3)]


def test_a_crash_damaged_file_does_not_hide_what_the_next_run_recorded(tmp_path: Path) -> None:
    """The soak of 2026-10-18: the PC restarted, the recorder appended to a truncated gzip and lost the rest."""
    rec = WsRecorder(tmp_path)
    rec.start()
    for i in range(50):
        rec.record(at(D1, i), l2(i))
    finish(rec)
    first = tmp_path / "2026-10-17" / "l2_data-BTC-EUR.jsonl.gz"
    first.write_bytes(first.read_bytes()[:-30])  # the first run "died" mid-write
    rec2 = WsRecorder(tmp_path)
    rec2.start()
    rec2.record(at(D1, 1000), l2(1000))
    finish(rec2)
    got = [raw for _, _, raw in merged_messages(tmp_path, [D1.date()])]
    assert l2(1000) in got  # the new session is intact whatever happened to the old file


def test_a_message_with_a_newline_cannot_break_the_one_line_format(tmp_path: Path) -> None:
    pretty = json.dumps(json.loads(l2(1)), indent=2)
    rec = WsRecorder(tmp_path)
    rec.start()
    rec.record(at(D1), pretty)
    finish(rec)
    ((_, raw),) = read_recording(tmp_path / "2026-10-17" / "l2_data-BTC-EUR.jsonl.gz")
    assert json.loads(raw) == json.loads(l2(1)) and "\n" not in raw


def test_retention_deletes_only_old_day_folders(tmp_path: Path) -> None:
    for d in ("2026-09-01", "2026-09-17", "2026-09-18", "2026-10-17", "notes"):
        (tmp_path / d).mkdir()
    rec = WsRecorder(tmp_path, retention_days=30)
    removed = rec.purge(date(2026, 10, 17))
    assert removed == ["2026-09-01"]  # a day exactly 30 days old is still kept; only strictly older goes
    assert sorted(p.name for p in tmp_path.iterdir()) == ["2026-09-17", "2026-09-18", "2026-10-17", "notes"]


def test_a_writer_that_cannot_keep_up_drops_and_counts_instead_of_blocking(tmp_path: Path) -> None:
    rec = WsRecorder(tmp_path, max_queue=3)  # not started: nothing consumes the queue
    for i in range(5):
        rec.record(at(D1, i), heartbeat(i))
    assert rec.stats.dropped == 2


def test_write_errors_are_counted_and_do_not_kill_the_thread(tmp_path: Path) -> None:
    blocker = tmp_path / "2026-10-17"
    blocker.write_text("a file where a folder should be")
    rec = WsRecorder(tmp_path)
    rec.start()
    rec.record(at(D1), heartbeat(1))
    rec.record(at(D1, 1), heartbeat(2))
    finish(rec)
    assert len(rec.stats.errors) == 2 and rec.stats.written == 0


# ----------------------------------------------------------------------------- verification


def feed(messages: list[tuple[float, str]]) -> SequenceReport:
    return verify_sequence(iter([(recv, int(json.loads(raw)["sequence_num"]), raw) for recv, raw in messages]))


def test_a_clean_stream_has_no_gaps() -> None:
    rep = feed([(at(D1, i), heartbeat(i)) for i in range(100)])
    assert (rep.messages, rep.connections, rep.gaps, rep.resets, rep.lost_messages) == (100, 1, 0, 0, 0)
    assert rep.max_silence_s == 1.0 and rep.silences_over_5s == 0


def test_gaps_resets_and_silences_are_counted() -> None:
    msgs = [(at(D1, i), heartbeat(i)) for i in range(5)]  # 0..4
    msgs += [(at(D1, 5), heartbeat(9))]  # 5..8 lost: a gap of 4
    msgs += [(at(D1, 20), heartbeat(10))]  # 15 s of silence, numbering continues
    msgs += [(at(D1, 21), heartbeat(0)), (at(D1, 22), heartbeat(1))]  # a new connection
    rep = feed(msgs)
    assert (rep.gaps, rep.lost_messages, rep.resets, rep.connections) == (1, 4, 1, 2)
    assert rep.silences_over_5s == 1 and rep.max_silence_s == 15.0
    assert len(rep.gap_times) == 1


def test_files_of_different_channels_are_merged_in_arrival_order(tmp_path: Path) -> None:
    rec = WsRecorder(tmp_path)
    rec.start()
    plan = [(0, l2(0)), (1, trades(1)), (2, ticker(2)), (3, heartbeat(3)), (4, l2(4, "SOL-EUR")), (5, trades(5))]
    for sec, raw in plan:
        rec.record(at(D1, sec), raw)
    finish(rec)
    merged = list(merged_messages(tmp_path, recorded_days(tmp_path)))
    assert [seq for _, seq, _ in merged] == [0, 1, 2, 3, 4, 5]
    rep = verify_sequence(iter(merged))
    assert rep.gaps == 0 and rep.by_channel == {"l2_data": 2, "market_trades": 2, "ticker": 1, "heartbeats": 1}


def test_a_message_lost_before_it_was_recorded_shows_as_a_gap(tmp_path: Path) -> None:
    rec = WsRecorder(tmp_path)
    rec.start()
    for sec, raw in [(0, heartbeat(0)), (1, l2(1)), (2, heartbeat(3)), (3, trades(4))]:  # seq 2 never arrived
        rec.record(at(D1, sec), raw)
    finish(rec)
    rep = verify_sequence(merged_messages(tmp_path, recorded_days(tmp_path)))
    assert rep.gaps == 1 and rep.lost_messages == 1


# ----------------------------------------------------------------- with the stream (end to end)


def test_the_stream_feeds_every_raw_message_to_the_recorder(tmp_path: Path) -> None:
    rec = WsRecorder(tmp_path)
    rec.start()
    now = [D1]
    stream = CoinbaseMarketStream(["BTC-EUR"], clock=lambda: now[0], raw_tap=rec.record)
    raws = [heartbeat(0), l2(1), "garbage", heartbeat(2)]
    for i, raw in enumerate(raws):
        now[0] = D1 + timedelta(seconds=i)
        stream.handle_raw(at(D1, i), raw)
    finish(rec)
    assert stream.stats.messages == 4 and stream.stats.parse_errors == 1
    assert rec.stats.written == 4  # the unparsable message is kept too: it may be what explains a problem
    assert len(list(read_recording(tmp_path / "2026-10-17" / "unparsed-all.jsonl.gz"))) == 1


# --------------------------------------------------------------- soak verdict and comparison


def _good(**changes: object) -> dict:  # type: ignore[type-arg]
    base = dict(
        elapsed_hours=48.0, target_hours=48.0, recorder_errors=0, recorder_dropped=0, parse_errors=0,
        parity={"BTC-EUR": {"nan_mismatch_cells": 0, "max_relative_error": 1e-13}},
        revisions={"BTC-EUR": {"ratio": 0.001}}, max_revision_ratio=0.005, seconds_behind={"BTC-EUR": 40.0},
    )  # fmt: skip
    return {**base, **changes}


def test_a_clean_soak_passes_every_criterion() -> None:
    from modules.dryrun.recorder import soak_verdict

    v = soak_verdict(**_good())
    assert v and all(v.values())


@pytest.mark.parametrize(
    ("changes", "failing"),
    [
        ({"elapsed_hours": 30.0}, "hours_reached"),
        ({"recorder_errors": 1}, "recorder_without_errors"),
        ({"recorder_dropped": 5}, "recorder_without_errors"),
        ({"parse_errors": 2}, "no_parse_errors"),
        ({"parity": {"BTC-EUR": {"nan_mismatch_cells": 1, "max_relative_error": 0.0}}}, "engine_matches"),
        ({"parity": {"BTC-EUR": {"nan_mismatch_cells": 0, "max_relative_error": 1e-6}}}, "engine_matches"),
        ({"revisions": {"BTC-EUR": {"ratio": 0.02}}}, "rest_revision_ratio_within_limit"),
        ({"seconds_behind": {"BTC-EUR": 4000.0}}, "bars_caught_up"),
    ],
)  # fmt: skip
def test_each_criterion_fails_on_its_own_evidence(changes: dict, failing: str) -> None:  # type: ignore[type-arg]
    from modules.dryrun.recorder import soak_verdict

    v = soak_verdict(**_good(**changes))
    key = next(k for k in v if k.startswith(failing))  # 'engine_matches' is a prefix of the full criterion name
    assert v[key] is False and sum(not x for x in v.values()) == 1


def test_a_criterion_with_nothing_measured_is_not_passed() -> None:
    from modules.dryrun.recorder import soak_verdict

    v = soak_verdict(**_good(parity={}, revisions={}, seconds_behind={}))
    assert not v["engine_matches_batch_on_stored_bars"]
    assert not v["rest_revision_ratio_within_limit"]
    assert not v["bars_caught_up"]


def test_compare_rows_reports_nan_mismatches_and_the_worst_relative_error() -> None:
    import numpy as np
    import pandas as pd

    from modules.dryrun.recorder import compare_rows

    ref = pd.DataFrame({"a": [1.0, 100.0, np.nan, 5.0], "b": [0.5, 2.0, 3.0, np.nan]})
    inc = pd.DataFrame({"a": [1.0, 100.0000001, np.nan, 5.0], "b": [0.5, 2.0, np.nan, np.nan]})
    r = compare_rows(inc, ref)
    assert r["rows"] == 4 and r["nan_mismatch_cells"] == 1  # b[2] is NaN on one side only
    assert r["max_relative_error"] == pytest.approx(1e-9, rel=0.1)  # 1e-7 absolute on 100 -> 1e-9 relative
    assert r["rows_all_columns_available"] == 2
    assert compare_rows(ref, ref)["max_relative_error"] == 0.0


def test_every_product_must_be_measured_not_just_some() -> None:
    from modules.dryrun.recorder import soak_verdict

    both = ["BTC-EUR", "ETH-EUR"]
    v = soak_verdict(**_good(products=both))  # only BTC has parity and revisions
    assert not v["engine_matches_batch_on_stored_bars"] and not v["rest_revision_ratio_within_limit"]
    full = _good(
        products=both,
        parity={p: {"nan_mismatch_cells": 0, "max_relative_error": 0.0} for p in both},
        revisions={p: {"ratio": 0.0} for p in both},
    )
    assert all(soak_verdict(**full).values())
