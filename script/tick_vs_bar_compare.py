"""Compare the closed trades of two dry ledgers of the SAME replayed day: one run in the bar fill mode, one in the tick
fill mode (``script.replay_app_day --fill-mode``). Read only: both databases are opened with ``mode=ro``.

    python -m script.tick_vs_bar_compare --bar <work_bar>/state/chronos_dry.db --tick <work_tick>/state/chronos_dry.db

The trades are paired by (book, product, entry time): the entry is bar-driven in both modes, so the same decision gives
the same entry fill as long as the runs have not diverged (a position that leaves earlier or later changes which later
decisions can enter). Reported: how many trades exist in both, how many of those leave for the same reason, in the same
minute and at the same price, the price difference in bps, and the distribution of the stop slippage in bps against the
stop level (``(sl - exit price) / sl``) for the stops of each mode.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Trade:
    book: str
    product: str
    entry_at: datetime
    exit_at: datetime
    reason: str
    entry_price: Decimal
    exit_price: Decimal
    size: Decimal
    sl: Decimal | None
    tp: Decimal | None
    net: Decimal
    gap_resolved: bool

    @property
    def key(self) -> tuple[str, str, datetime]:
        return (self.book, self.product, self.entry_at)

    @property
    def stop_slip_bps(self) -> float | None:
        if self.reason != "SL" or self.sl is None or self.sl <= 0:
            return None
        return float((self.sl - self.exit_price) / self.sl * 10_000)


def load(db: Path) -> list[Trade]:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("SELECT * FROM closed_trades ORDER BY exit_at").fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        out.append(
            Trade(
                r["book"], r["product_id"], datetime.fromisoformat(r["entry_at"]), datetime.fromisoformat(r["exit_at"]),
                r["exit_reason"], Decimal(r["entry_avg_price"]), Decimal(r["exit_avg_price"]), Decimal(r["exit_size"]),
                None if r["sl"] is None else Decimal(r["sl"]), None if r["tp"] is None else Decimal(r["tp"]),
                Decimal(r["net_quote"]), bool(r["gap_resolved"]),
            )
        )
    return out


def quantiles(values: Sequence[float]) -> dict[str, float | int | None]:
    if not values:
        return {"n": 0, "mean": None, "p10": None, "median": None, "p90": None, "min": None, "max": None}
    v = sorted(values)

    def q(p: float) -> float:
        return v[min(len(v) - 1, int(p * len(v)))]

    return {
        "n": len(v), "mean": round(statistics.fmean(v), 2), "p10": round(q(0.10), 2), "median": round(q(0.5), 2),
        "p90": round(q(0.90), 2), "min": round(v[0], 2), "max": round(v[-1], 2),
    }  # fmt: skip


def minute(t: datetime) -> datetime:
    return t.replace(second=0, microsecond=0)


def compare(bar: Sequence[Trade], tick: Sequence[Trade]) -> dict[str, Any]:
    by_bar = {t.key: t for t in bar}
    by_tick = {t.key: t for t in tick}
    both = sorted(set(by_bar) & set(by_tick))
    same_reason = [k for k in both if by_bar[k].reason == by_tick[k].reason]
    same_minute = [k for k in both if minute(by_bar[k].exit_at) == minute(by_tick[k].exit_at)]
    same_price = [k for k in both if by_bar[k].exit_price == by_tick[k].exit_price]
    full = [k for k in both if k in set(same_reason) & set(same_minute) & set(same_price)]
    price_bps = [
        float((by_tick[k].exit_price - by_bar[k].exit_price) / by_bar[k].exit_price * 10_000)
        for k in both
        if by_bar[k].reason == by_tick[k].reason
    ]
    differences = []
    for k in both:
        b, t = by_bar[k], by_tick[k]
        if (b.reason, minute(b.exit_at), b.exit_price) != (t.reason, minute(t.exit_at), t.exit_price):
            differences.append(
                {
                    "book": k[0], "product": k[1], "entry_at": k[2].isoformat(timespec="seconds"),
                    "bar": [b.reason, b.exit_at.isoformat(timespec="seconds"), str(b.exit_price)],
                    "tick": [t.reason, t.exit_at.isoformat(timespec="seconds"), str(t.exit_price)],
                    "exit_minutes_apart": int((t.exit_at - b.exit_at).total_seconds() // 60),
                    "price_bps_tick_vs_bar": round(float((t.exit_price - b.exit_price) / b.exit_price * 10_000), 1),
                    "sl": None if b.sl is None else str(b.sl), "tp": None if b.tp is None else str(b.tp),
                }
            )  # fmt: skip
    pair_stop = [
        (by_bar[k].stop_slip_bps, by_tick[k].stop_slip_bps)
        for k in both
        if by_bar[k].reason == by_tick[k].reason == "SL"
    ]
    return {
        "trades": {"bar": len(bar), "tick": len(tick)},
        "by_reason": {"bar": dict(Counter(t.reason for t in bar)), "tick": dict(Counter(t.reason for t in tick))},
        "net_quote": {
            "bar": str(sum((t.net for t in bar), Decimal(0))), "tick": str(sum((t.net for t in tick), Decimal(0)))
        },
        "paired": len(both),
        "only_in_bar": [list(map(str, k)) for k in sorted(set(by_bar) - set(by_tick))],
        "only_in_tick": [list(map(str, k)) for k in sorted(set(by_tick) - set(by_bar))],
        "same_reason": len(same_reason), "same_exit_minute": len(same_minute), "same_exit_price": len(same_price),
        "identical_exit": len(full),
        "exit_price_bps_tick_minus_bar_same_reason": quantiles(price_bps),
        "stop_slip_bps_vs_stop_level": {
            "bar": quantiles([x for t in bar if (x := t.stop_slip_bps) is not None]),
            "tick": quantiles([x for t in tick if (x := t.stop_slip_bps) is not None]),
        },
        "stop_slip_bps_paired_stops": {
            "n": len(pair_stop),
            "bar": quantiles([a for a, _ in pair_stop if a is not None]),
            "tick": quantiles([b for _, b in pair_stop if b is not None]),
            "tick_minus_bar": quantiles([b - a for a, b in pair_stop if a is not None and b is not None]),
        },
        "tick_gap_resolved": sum(1 for t in tick if t.gap_resolved),
        "differences": differences,
    }  # fmt: skip


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bar", required=True, type=Path)
    ap.add_argument("--tick", required=True, type=Path)
    ap.add_argument("--json", default=None, type=Path, help="also write the full result here")
    ap.add_argument("--book", default=None, help="only this book")
    args = ap.parse_args(argv)
    bar, tick = load(args.bar), load(args.tick)
    if args.book:
        bar, tick = [t for t in bar if t.book == args.book], [t for t in tick if t.book == args.book]
    result = compare(bar, tick)
    text = json.dumps(result, indent=1, default=str)
    print(text)
    if args.json is not None:
        args.json.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
