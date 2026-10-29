"""``python -m modules.probe report``: the blocking criteria and the descriptive measures of the plan (sez. 5).

REST reads only. Every ledger order and fill is compared with what the exchange says now; the balances, the fees, the
post-only behaviour, the bracket coverage, the expiry of the entries and the fields of the ``user`` channel (table B of
``docs/decisions/2026-10-20-canale-user-p4.md``) are checked; the latencies and the fill rate are described, not judged.

Each criterion is ``pass``, ``fail`` or ``not_observed`` (nothing to judge yet: a probe with no fill cannot prove the
fee
criterion). The probe is VALIDATED only when every blocking criterion passes.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Literal, Protocol

from modules.config import ExecProbeConfig
from modules.execution.balance import AccountSnapshot
from modules.execution.exchange import FillInfo, OrderInfo
from modules.ledger.positions import FillRecord, OrderRecord, PositionLedger
from modules.probe import limits
from modules.probe.control import EventFile
from modules.probe.userlog import compare_order, compare_order_open, fill_latencies, order_messages
from modules.risk.costs import FeeRates

ZERO: Final = Decimal(0)
BPS: Final = Decimal(10_000)
Status = Literal["pass", "fail", "not_observed"]
RAW_GLOB: Final = "user_channel_raw.jsonl.gz*"
PRICE_TOL: Final = Decimal("0.00000001")


class ReportExchange(Protocol):
    """Reads only."""

    async def get_order(self, order_id: str) -> OrderInfo: ...
    async def get_fills(self, order_id: str) -> list[FillInfo]: ...
    async def get_accounts(self) -> AccountSnapshot: ...
    async def get_fee_rates(self) -> FeeRates: ...


def _check(status: Status, detail: str, **extra: Any) -> dict[str, Any]:
    return {"status": status, "detail": detail, **extra}


def clopper_pearson(k: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Exact two-sided interval of a proportion (the fill rate of the entries)."""
    from scipy.stats import beta

    lo = 0.0 if k == 0 else float(beta.ppf(alpha / 2, k, n - k + 1))
    hi = 1.0 if k == n else float(beta.ppf(1 - alpha / 2, k + 1, n - k))
    return lo, hi


def _tolerance(n_fill: Decimal) -> Decimal:
    return max(limits.FEE_TOLERANCE_ABS, limits.FEE_TOLERANCE_BPS / BPS * n_fill)


def _decimals(value: Decimal) -> int:
    exponent = value.as_tuple().exponent
    return -exponent if isinstance(exponent, int) and exponent < 0 else 0


def _entry_limit(rec: OrderRecord) -> Decimal | None:
    try:
        return Decimal(str(json.loads(rec.payload or "{}").get("limit_price")))
    except (ValueError, ArithmeticError):
        return None


# ------------------------------------------------------------------------------------ the criteria


async def _orders_and_fills(
    exchange: ReportExchange, ledger: PositionLedger, orders: Sequence[OrderRecord]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, OrderInfo]]:
    infos: dict[str, OrderInfo] = {}
    order_diffs: list[str] = []
    fill_diffs: list[str] = []
    compared = fills_compared = 0
    for rec in orders:
        if rec.exchange_order_id is None:
            if rec.status not in ("REJECTED", "FAILED"):
                order_diffs.append(f"{rec.client_order_id}: no exchange id and status {rec.status}")
            continue
        info = await exchange.get_order(rec.exchange_order_id)
        infos[rec.client_order_id] = info
        mine = await ledger.fills_for(rec.client_order_id)
        theirs = await exchange.get_fills(rec.exchange_order_id)
        compared += 1
        filled = sum((f.size for f in mine), ZERO)
        avg = None if not mine else sum((f.price * f.size for f in mine), ZERO) / filled
        fees = sum((f.fee for f in mine), ZERO)
        if rec.status != info.status:
            order_diffs.append(f"{rec.client_order_id}: status ledger {rec.status} vs exchange {info.status}")
        if filled != info.filled_size:
            order_diffs.append(f"{rec.client_order_id}: filled ledger {filled} vs exchange {info.filled_size}")
        if (avg is None) != (info.average_filled_price is None) or (
            avg is not None
            and info.average_filled_price is not None
            and abs(avg - info.average_filled_price) > PRICE_TOL
        ):
            order_diffs.append(
                f"{rec.client_order_id}: average price ledger {avg} vs exchange {info.average_filled_price}"
            )
        if abs(fees - info.total_fees) > PRICE_TOL:
            order_diffs.append(f"{rec.client_order_id}: fees ledger {fees} vs exchange {info.total_fees}")
        fills_compared += len(theirs) + len(mine)
        mine_by_id: dict[str, FillRecord] = {f.trade_id: f for f in mine}
        theirs_by_id = {f.trade_id: f for f in theirs}
        for tid in sorted(set(mine_by_id) ^ set(theirs_by_id)):
            side = "missing from the ledger" if tid in theirs_by_id else "not on the exchange"
            fill_diffs.append(f"{rec.client_order_id}: fill {tid} {side}")
        for tid in sorted(set(mine_by_id) & set(theirs_by_id)):
            a, b = mine_by_id[tid], theirs_by_id[tid]
            if (a.price, a.size, a.fee, a.liquidity) != (b.price, b.size, b.fee, b.liquidity):
                fill_diffs.append(
                    f"{rec.client_order_id}: fill {tid} differs ({a.price} {a.size} {a.fee} {a.liquidity})"
                )
    if not compared:
        none = _check("not_observed", "no order with an exchange id yet")
        return none, dict(none), infos
    orders_check = _check("fail" if order_diffs else "pass", f"{compared} orders compared", differences=order_diffs)
    fills_check = _check(
        "fail" if fill_diffs else "pass", f"{fills_compared} fill rows compared", differences=fill_diffs
    )
    return orders_check, fills_check, infos


async def _balances(
    cfg: ExecProbeConfig, exchange: ReportExchange, ledger: PositionLedger, out_dir: Path
) -> dict[str, Any]:
    snap = await exchange.get_accounts()
    base, quote = cfg.product.split("-")
    position = ledger.view(limits.BOOK, cfg.product)
    expected_base = position.base_qty + position.dust_qty
    actual_base = sum(snap.balances.get(base, (ZERO, ZERO)))
    base_tol = max(Decimal("0.00000002"), actual_base / 10_000)
    diffs: list[str] = []
    if abs(actual_base - expected_base) > base_tol:
        diffs.append(f"{base}: ledger holds {expected_base}, the exchange {actual_base}")
    pre = out_dir / "preflight.json"
    detail = f"{base} ledger {expected_base} vs exchange {actual_base}"
    if pre.is_file():
        try:
            start = Decimal(str(json.loads(pre.read_text(encoding="utf-8"))["details"]["quote_available"]))
        except (ValueError, KeyError, ArithmeticError):
            start = None
        if start is not None:
            flow = ZERO
            for f in await ledger.book_fills(limits.BOOK):
                flow += (f.price * f.size - f.fee) if f.side == "SELL" else -(f.price * f.size + f.fee)
            actual_quote = sum(snap.balances.get(quote, (ZERO, ZERO)))
            expected_quote = start + flow
            quote_tol = max(Decimal("0.02"), actual_quote / 10_000)
            if abs(actual_quote - expected_quote) > quote_tol:
                diffs.append(
                    f"{quote}: preflight {start} + flows {flow} = {expected_quote}, the exchange {actual_quote}"
                )
            detail += f"; {quote} expected {expected_quote} vs exchange {actual_quote}"
    return _check("fail" if diffs else "pass", detail, differences=diffs)


async def _fees(fills: Sequence[FillRecord], exchange: ReportExchange) -> dict[str, Any]:
    if not fills:
        return _check("not_observed", "no fill yet")
    rates = await exchange.get_fee_rates()
    diffs: list[str] = []
    for f in fills:
        n = f.price * f.size
        rate = Decimal(str(rates.maker_bps if f.liquidity.upper().startswith("MAKER") else rates.taker_bps)) / BPS
        if abs(f.fee - n * rate) > _tolerance(n):
            diffs.append(f"{f.trade_id}: fee {f.fee} vs {n * rate:.8f} ({f.liquidity}, {n:.4f} EUR)")
    if all(_decimals(f.fee) <= 2 for f in fills):
        diffs.append("every fee has at most 2 decimals: the criterion cannot be verified at this size")
    return _check(
        "fail" if diffs else "pass", f"{len(fills)} fills, tier maker {rates.maker_bps} / taker {rates.taker_bps} bps",
        differences=diffs,
    )  # fmt: skip


def _post_only(
    orders: Sequence[OrderRecord], infos: dict[str, OrderInfo], fills: Sequence[FillRecord]
) -> dict[str, Any]:
    by_order: dict[str, list[FillRecord]] = {}
    for f in fills:
        by_order.setdefault(f.client_order_id, []).append(f)
    diffs: list[str] = []
    n = 0
    for rec in orders:
        if rec.leg != "entry" or rec.client_order_id not in by_order:
            continue
        n += 1
        limit = _entry_limit(rec)
        fs = by_order[rec.client_order_id]
        avg = sum((f.price * f.size for f in fs), ZERO) / sum((f.size for f in fs), ZERO)
        if limit is not None and avg != limit:
            diffs.append(f"{rec.client_order_id}: average entry {avg} is not the limit {limit}")
        if any(not f.liquidity.upper().startswith("MAKER") for f in fs):
            diffs.append(f"{rec.client_order_id}: a fill was not MAKER")
    if not n:
        return _check("not_observed", "no entry has filled yet")
    return _check("fail" if diffs else "pass", f"{n} filled entries", differences=diffs)


def _bracket_coverage(cfg: ExecProbeConfig, events: Sequence[dict[str, Any]], ledger: PositionLedger) -> dict[str, Any]:
    latencies = [float(e["seconds"]) for e in events if e.get("kind") == "bracket_open_latency"]
    diffs: list[str] = []
    late = [x for x in latencies if x > cfg.bracket_open_max_s]
    if late:
        diffs.append(f"bracket OPEN later than {cfg.bracket_open_max_s} s: {late}")
    if any(e.get("kind") == "protection_exit" and e.get("reason") == "bracket_not_open" for e in events):
        diffs.append("a position was sold because its bracket did not open in time")
    pos = ledger.view(limits.BOOK, cfg.product)
    if pos.is_long and pos.bracket_order_id is None:
        diffs.append("the position is LONG without a bracket now")
    if not latencies and not diffs:
        return _check("not_observed", "no bracket was placed yet")
    detail = f"{len(latencies)} bracket latencies, max {max(latencies, default=0):.1f} s"
    return _check("fail" if diffs else "pass", detail, differences=diffs)


def _entry_expiry(orders: Sequence[OrderRecord], fills: Sequence[FillRecord]) -> dict[str, Any]:
    filled = {f.client_order_id for f in fills}
    seen: Counter[str] = Counter()
    diffs: list[str] = []
    for rec in orders:
        if rec.leg != "entry" or not (rec.intent_id or "").startswith("probe:") or rec.client_order_id in filled:
            continue
        seen[rec.status] += 1
        if rec.status not in ("EXPIRED", "CANCELLED"):
            diffs.append(f"{rec.client_order_id}: an unfilled entry ended {rec.status}")
    if not seen and not diffs:
        return _check("not_observed", "no unfilled entry yet")
    note = " (CANCELLED = the bot's second line, to be noted)" if seen.get("CANCELLED") else ""
    return _check("fail" if diffs else "pass", f"unfilled entries by status: {dict(seen)}{note}", differences=diffs)


def _user_channel(
    raw_paths: Sequence[Path], orders: Sequence[OrderRecord], infos: dict[str, OrderInfo]
) -> tuple[dict[str, Any], list[tuple[float, dict[str, Any]]]]:
    messages: list[tuple[float, dict[str, Any]]] = []
    for path in raw_paths:
        messages += order_messages(path)
    if not messages:
        return _check("not_observed", "no order message in the recording"), messages
    by_cid: dict[str, list[dict[str, Any]]] = {}
    for _, m in messages:
        by_cid.setdefault(str(m.get("client_order_id")), []).append(m)
    diffs: list[str] = []
    seen: set[str] = set()
    for rec in orders:
        msgs = by_cid.get(rec.client_order_id)
        info = infos.get(rec.client_order_id)
        if not msgs or info is None:
            continue
        for m in msgs:
            status = str(m.get("status"))
            cumulative = Decimal(str(m.get("cumulative_quantity") or 0))
            leaves = Decimal(str(m.get("leaves_quantity") or 0))
            seen.add(status)
            if cumulative > 0 and leaves > 0:
                seen.add("PARTIAL")
            if Decimal(str(m.get("stop_price") or 0)) > 0:
                seen.add("BRACKET")
        last = msgs[-1]
        if str(last.get("status")) == info.status:
            diffs += [f"{rec.client_order_id}: {d}" for d in compare_order(last, info)]
        first_open = next((m for m in msgs if str(m.get("status")) == "OPEN"), None)
        if first_open is not None:
            diffs += [f"{rec.client_order_id} OPEN: {d}" for d in compare_order_open(first_open, info)]
    required = {"OPEN", "CANCELLED", "EXPIRED", "FILLED", "BRACKET"}
    missing = sorted(required - seen)
    detail = f"statuses seen {sorted(seen)}"
    if diffs:
        return _check("fail", detail, differences=diffs, missing_samples=missing), messages
    if missing:
        note = f"; samples still missing: {missing} (a partial fill: {'PARTIAL' in seen})"
        return _check("not_observed", detail + note, missing_samples=missing), messages
    return _check("pass", detail + f"; partial fill seen: {'PARTIAL' in seen}", missing_samples=[]), messages


def bracket_child_fields(
    messages: Sequence[tuple[float, dict[str, Any]]], orders: Sequence[OrderRecord]
) -> list[dict[str, Any]]:
    """What the ``user`` channel said about the brackets (plan Allegato A/10.5): the order type, the trigger status, the
    limit and the stop of each distinct state, and ``limit / stop`` (the stop-limit floor, about 0.95, is what the plan
    expects the child order of a triggered stop to carry). Empty when the recording has no bracket message."""
    brackets = {o.client_order_id for o in orders if o.leg == "bracket"}
    seen: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for _, m in messages:
        if str(m.get("client_order_id")) not in brackets:
            continue
        key = (
            str(m.get("status")), str(m.get("order_type")), str(m.get("trigger_status")),
            str(m.get("limit_price")), str(m.get("stop_price")),
        )  # fmt: skip
        if key in seen:
            continue
        limit, stop = _dec_or_none(m.get("limit_price")), _dec_or_none(m.get("stop_price"))
        ratio = None if not limit or not stop else float(limit / stop)
        seen[key] = {
            "status": key[0], "order_type": key[1], "trigger_status": key[2], "limit_price": key[3],
            "stop_price": key[4], "limit_over_stop": ratio,
        }  # fmt: skip
    return list(seen.values())


def _dec_or_none(value: object) -> Decimal | None:
    try:
        return None if value in (None, "") else Decimal(str(value))
    except ArithmeticError:
        return None


def _latency(
    events: Sequence[dict[str, Any]], messages: list[tuple[float, dict[str, Any]]], poll_s: float
) -> dict[str, Any]:
    timed = [
        (datetime.fromisoformat(e["t"]).timestamp(), str(e["order_id"]), Decimal(str(e["total"])), int(e["n"]))
        for e in events if e.get("kind") == "fills_read"
    ]  # fmt: skip
    values = fill_latencies(messages, timed)
    if not values:
        return _check("not_observed", "no fill message with a matching get_fills read")
    worst = max(values)
    return _check(
        "pass" if worst < poll_s else "fail",
        f"{len(values)} fills, max {worst:.2f} s (limit {poll_s} s)",
        values=values,
    )


def _integrity(
    orders: Sequence[OrderRecord], trades: Sequence[Any], events: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    problems: list[str] = []
    watchdog = [t for t in trades if t.exit_reason == "WATCHDOG"]
    if watchdog:
        problems.append(f"{len(watchdog)} exit(s) by WATCHDOG")
    unknown = [o.client_order_id for o in orders if o.status == "UNKNOWN"]
    if unknown:
        problems.append(f"{len(unknown)} order(s) still UNKNOWN")
    failed = [e for e in events if e.get("kind") in ("protection_exit_failed", "probe_bracket_restore_failed")]
    if failed:
        problems.append(f"{len(failed)} failed protective action(s)")
    if len({o.client_order_id for o in orders}) != len(orders):
        problems.append("duplicate client_order_id")
    return _check("fail" if problems else "pass", "no SpotViolation, crash, duplicate or WATCHDOG exit" if not problems
                  else "; ".join(problems), differences=problems)  # fmt: skip


# --------------------------------------------------------------------------------------- the report


async def build_report(
    cfg: ExecProbeConfig, exchange: ReportExchange, ledger: PositionLedger, out_dir: Path, *, entry_poll_s: float = 5.0
) -> dict[str, Any]:
    orders = await ledger.book_orders(limits.BOOK)
    fills = await ledger.book_fills(limits.BOOK)
    trades = await ledger.closed_trades(limits.BOOK)
    events = EventFile(out_dir, lambda: datetime.now(UTC)).read()
    orders_check, fills_check, infos = await _orders_and_fills(exchange, ledger, orders)
    raw_paths = sorted(out_dir.glob(RAW_GLOB))
    channel_check, messages = _user_channel(raw_paths, orders, infos)
    criteria = {
        "orders_concordance": orders_check,
        "fills_concordance": fills_check,
        "balances": await _balances(cfg, exchange, ledger, out_dir),
        "fees": await _fees(fills, exchange),
        "post_only": _post_only(orders, infos, fills),
        "bracket_coverage": _bracket_coverage(cfg, events, ledger),
        "entry_expiry": _entry_expiry(orders, fills),
        "user_channel": channel_check,
        "fill_latency": _latency(events, messages, entry_poll_s),
        "integrity": _integrity(orders, trades, events),
    }
    live_entries = [o for o in orders if o.leg == "entry" and (o.intent_id or "").startswith("probe:")]
    filled_ids = {f.client_order_id for f in fills}
    n_filled = sum(1 for o in live_entries if o.client_order_id in filled_ids)
    interval = clopper_pearson(n_filled, len(live_entries)) if live_entries else None
    descriptive = {
        "entries": {"sent": len(live_entries), "filled": n_filled, "fill_rate_ci95": interval},
        "closed_trades": len(trades),
        "exit_reasons": dict(Counter(t.exit_reason for t in trades)),
        "pnl_quote": str(sum((t.net_quote for t in trades), ZERO)),
        "bracket_open_seconds": [float(e["seconds"]) for e in events if e.get("kind") == "bracket_open_latency"],
        "sl_slippage_bps": [float(e["bps"]) for e in events if e.get("kind") == "sl_slippage"],
        "sl_slippage_alarms": sum(1 for e in events if e.get("kind") in ("sl_slippage_alarm", "sl_slippage_severe")),
        "fill_latency_s": criteria["fill_latency"].get("values", []),
        "held_min": [t.held_min for t in trades],
        "bracket_child_fields": bracket_child_fields(messages, orders),
    }  # fmt: skip
    statuses = [c["status"] for c in criteria.values()]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "product": cfg.product,
        "validated": all(s == "pass" for s in statuses),
        "failed": [k for k, c in criteria.items() if c["status"] == "fail"],
        "not_observed": [k for k, c in criteria.items() if c["status"] == "not_observed"],
        "criteria": criteria,
        "descriptive": descriptive,
        "note": "the probe's fills are diagnostics only (source = exec_probe): never used to fit the fill model",
    }


def render_markdown(report: dict[str, Any]) -> str:
    verdict = "VALIDATA" if report["validated"] else "NON ANCORA VALIDATA"
    lines = [f"# Rapporto della prova di esecuzione ({report['product']})", "", f"Esito: **{verdict}**", ""]
    lines += ["| Criterio | Esito | Dettaglio |", "|---|---|---|"]
    for name, c in report["criteria"].items():
        lines.append(f"| {name} | {c['status']} | {c['detail']} |")
    for name, c in report["criteria"].items():
        for d in c.get("differences", []):
            lines.append(f"- {name}: {d}")
    d = report["descriptive"]
    lines += ["", "## Misure descrittive (senza soglia)", ""]
    lines += [f"- Ingressi: {d['entries']}", f"- Chiusure: {d['exit_reasons']}", f"- PnL: {d['pnl_quote']} EUR"]
    lines += [f"- Latenza bracket (s): {d['bracket_open_seconds']}", f"- Slittamento SL (bps): {d['sl_slippage_bps']}"]
    lines += [f"- Latenza fill WS->REST (s): {d['fill_latency_s']}"]
    for child in d.get("bracket_child_fields", []):
        lines.append(f"- Bracket (canale user): {child}")
    lines += ["", report["note"], ""]
    return "\n".join(lines)


def write_report(out_dir: Path, report: dict[str, Any]) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    js, md = out_dir / "probe_report.json", out_dir / "probe_report.md"
    js.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    md.write_text(render_markdown(report), encoding="utf-8")
    return js, md
