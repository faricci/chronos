"""What each stage does (plan sez. 4). The functions take their collaborators as arguments, so every one runs against a
fake exchange in the tests; ``runner.py`` builds the real ones. Nothing here imports the live gateway: the order stages
receive an object with ``submit_entry_with_bracket`` and ``cancel``.

* ``preflight`` (read-only): permissions as booleans, product facts, real fees, the balance, zero open orders, quote,
  spread, ATR1h, and three cycles of the balance feed.
* ``preview`` (a write that creates nothing): ``preview_order`` of the exact first entry, GTD and GTC, and the
  decision on the entry mode. If ``commission_total`` has only two decimals the fee criterion cannot be verified at
  this size and the stage stops.
* ``place-cancel`` / ``expire`` / ``reject``: orders of ``test_order_quote`` (2 EUR, above the protectable minimum) far
  from
  the market, or on the wrong side of the book.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Protocol

import structlog

from modules.config import ExecProbeConfig, FallbackFees
from modules.execution.balance import AccountSnapshot, BalanceBook, Clock, CoverageAlarm, SnapshotOutcome
from modules.execution.balance_feed import LiveBalanceFeed
from modules.execution.exchange import (
    BestBidAsk,
    ExchangeError,
    FillInfo,
    KeyPermissionsInfo,
    OrderInfo,
    OrderRejected,
    PreviewResult,
)
from modules.execution.gateway import CancelOutcome, EntryResult, entry_spec
from modules.execution.ids import client_order_id
from modules.execution.orders import OrderSpec, ProductMeta
from modules.execution.spot import DEFAULT_PORTFOLIO_TYPES, SpotRegistry, SpotViolation
from modules.ledger.positions import PositionLedger
from modules.probe import limits
from modules.probe.budget import BudgetState
from modules.probe.facts import AtrCache, MarketReader
from modules.probe.planner import PlanFacts, ProbePlanner, ProbeSkip
from modules.probe.stages import EntryMode, StageRecord, config_hash, crossing_buy_intent, far_buy_intent
from modules.probe.userlog import compare_order, compare_order_open, last_message_by_status, order_messages
from modules.risk.costs import FeeRates
from modules.risk.risk_manager import MarketQuote, OrderIntent

log = structlog.get_logger(__name__)

ZERO: Final = Decimal(0)
D = Decimal
# Allegato A/10.0 of the plan: the values the sizing was calculated on (BTC-EUR, 2026-10-18)
ANNEX_MAKER_BPS: Final = 25.0
ANNEX_TAKER_BPS: Final = 50.0
ANNEX_QUOTE_MIN: Final = D("1")
ANNEX_BASE_MIN: Final = D("0.00000001")
ANNEX_BASE_INC: Final = D("0.00000001")
ANNEX_QUOTE_INC: Final = D("0.01")
FEED_CYCLES: Final = 3
MIN_COMMISSION_DECIMALS: Final = 3  # two decimals = rounded to the cent: the fee criterion cannot be verified


class StageAbort(Exception):
    """A stage could not even run (the reason is the message). The runner records it as ``ok = false``."""


# ------------------------------------------------------------------------------------------- preflight


class PreflightExchange(MarketReader, Protocol):
    async def get_api_key_permissions(self) -> KeyPermissionsInfo: ...
    async def get_product_raw(self, product_id: str) -> dict[str, Any]: ...
    async def get_fee_rates(self) -> FeeRates: ...
    async def get_accounts(self) -> AccountSnapshot: ...


async def preflight(
    cfg: ExecProbeConfig,
    exchange: PreflightExchange,
    *,
    fingerprint: str,
    clock: Clock,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    spacing_s: float = 2.0,
) -> tuple[StageRecord, list[str]]:
    """Read everything the first entry will depend on. Returns the outcome record and the lines to print."""
    checks: dict[str, bool] = {}
    details: dict[str, Any] = {"key_fingerprint": fingerprint}
    lines: list[str] = [f"Chiave: impronta {fingerprint}"]

    perms = await exchange.get_api_key_permissions()
    details["permissions"] = {
        "can_view": perms.can_view, "can_trade": perms.can_trade, "can_transfer": perms.can_transfer,
        "portfolio_type_allowed": perms.portfolio_type in DEFAULT_PORTFOLIO_TYPES,
    }  # fmt: skip
    checks["can_view"] = perms.can_view
    checks["can_trade"] = perms.can_trade
    checks["cannot_transfer"] = not perms.can_transfer
    checks["portfolio_type_allowed"] = perms.portfolio_type in DEFAULT_PORTFOLIO_TYPES
    if not checks["portfolio_type_allowed"]:
        details["conflitto"] = "portfolio_type is not in the allowlist of spot.py (the allowlist is NOT widened here)"
    lines.append(
        "Permessi: vista={can_view} scambio={can_trade} prelievi={can_transfer} "
        "tipo portafoglio ammesso={portfolio_type_allowed}".format(**details["permissions"])
    )

    meta: ProductMeta | None = None
    try:
        registry = await SpotRegistry.load(exchange, [cfg.product])
        meta = registry.meta(cfg.product)
        checks["product_is_spot"] = True
    except SpotViolation as exc:
        checks["product_is_spot"] = False
        details["product_error"] = str(exc)
        registry = SpotRegistry({})
    if meta is not None:
        details["product"] = {
            "base_increment": str(meta.base_increment), "quote_increment": str(meta.quote_increment),
            "base_min_size": str(meta.base_min_size), "quote_min_size": str(meta.quote_min_size),
        }  # fmt: skip
        same = (
            meta.quote_min_size == ANNEX_QUOTE_MIN and meta.base_min_size == ANNEX_BASE_MIN
            and meta.base_increment == ANNEX_BASE_INC and meta.quote_increment == ANNEX_QUOTE_INC
        )  # fmt: skip
        checks["product_matches_annex"] = same

    fees = await exchange.get_fee_rates()
    details["fees"] = {"maker_bps": fees.maker_bps, "taker_bps": fees.taker_bps, "assumed": fees.assumed}
    checks["fees_are_real"] = not fees.assumed
    checks["fees_match_annex"] = fees.maker_bps == ANNEX_MAKER_BPS and fees.taker_bps == ANNEX_TAKER_BPS
    lines.append(
        f"Fee lette dall'account: maker {fees.maker_bps} bps, taker {fees.taker_bps} bps (assunte={fees.assumed})"
    )
    if not (checks["product_matches_annex"] if meta is not None else True) or not checks["fees_match_annex"]:
        details["needs_quant_review"] = (
            "minimums, increments or the fee tier differ from Allegato A/10.0: the quant must redo A/10.1-10.3 "
            "before going on"
        )

    snapshot = await exchange.get_accounts()
    quote_currency = meta.quote_currency if meta is not None else cfg.product.split("-")[1]
    available = snapshot.balances.get(quote_currency, (ZERO, ZERO))[0]
    held = sorted(c for c, (a, h) in snapshot.balances.items() if a + h > 0)
    details["quote_available"] = str(available)
    details["currencies_with_balance"] = held  # codes only, never amounts
    checks["quote_covers_q_max"] = available >= cfg.max_entry_quote
    lines.append(
        f"{quote_currency} disponibili: {available} (servono almeno {cfg.max_entry_quote}); valute con saldo: {held}"
    )

    open_orders = [o for o in await exchange.list_open_orders() if o.product_id == cfg.product]
    details["open_orders_on_product"] = len(open_orders)
    checks["no_open_orders"] = not open_orders

    q = await exchange.get_best_bid_ask(cfg.product)
    mid = (q.bid + q.ask) / 2
    spread_bps = float((q.ask - q.bid) / mid * 10_000)
    atr_cache = AtrCache(exchange, cfg.product, clock)
    atr_ok = await atr_cache.refresh()
    atr = atr_cache.value()
    details["market"] = {"spread_bps": round(spread_bps, 3), "atr1h_bps": None}
    if atr is not None:
        details["market"]["atr1h_bps"] = round(float(atr[0] / atr[1] * 10_000), 2)
    checks["atr_available"] = atr_ok and atr is not None
    lines.append(f"Mercato: spread {spread_bps:.2f} bps, ATR 1h {details['market']['atr1h_bps']} bps")

    if meta is not None:
        book = BalanceBook(limits.BOOK, "exchange", clock)
        alarms: list[CoverageAlarm] = []
        feed = LiveBalanceFeed(exchange, book, registry, refresh_s=30.0, on_alarm=alarms.append, clock=clock)
        accepted = 0
        ages: list[float] = []
        for i in range(FEED_CYCLES):
            if i:
                await sleep(spacing_s)
            if await feed.refresh_once() is SnapshotOutcome.ACCEPTED:
                accepted += 1
            age = feed.age_s()
            if age is not None:
                ages.append(age)
        details["balance_feed"] = {
            "accepted": accepted,
            "max_age_s": max(ages) if ages else None,
            "alarms": len(alarms),
        }
        checks["balance_feed_ok"] = (
            accepted == FEED_CYCLES and bool(ages) and max(ages) < limits.BALANCE_MAX_AGE_S and not alarms
        )
        lines.append(f"Feed saldi: {accepted}/{FEED_CYCLES} snapshot accettati, {len(alarms)} allarmi")

    ok = all(checks.values())
    details["checks"] = checks
    for name, value in checks.items():
        lines.append(f"  [{'OK' if value else 'NO'}] {name}")
    record = StageRecord("preflight", ok, clock(), config_hash(cfg), details=details)
    return record, lines


# --------------------------------------------------------------------------------------------- preview


class PreviewExchange(PreflightExchange, Protocol):
    def preview_order(self, spec: OrderSpec) -> Awaitable[PreviewResult]: ...


def _decimals(value: Decimal) -> int:
    exponent = value.as_tuple().exponent
    return -exponent if isinstance(exponent, int) and exponent < 0 else 0


async def _preview_one(exchange: PreviewExchange, spec: OrderSpec) -> dict[str, Any]:
    try:
        result = await exchange.preview_order(spec)
    except OrderRejected as exc:
        return {"accepted": False, "rejected": f"{exc.reason}: {exc.message}"}
    except ExchangeError as exc:
        return {"accepted": False, "error": f"{type(exc).__name__}: {exc}"}
    out: dict[str, Any] = {
        "accepted": not result.errors,
        "errors": list(result.errors),
        "warnings": list(result.warnings),
        "order_total": None if result.order_total is None else str(result.order_total),
        "commission_total": None if result.commission_total is None else str(result.commission_total),
    }
    if result.commission_total is not None:
        out["commission_decimals"] = _decimals(result.commission_total)
    return out


async def first_entry_intent(
    cfg: ExecProbeConfig, exchange: PreflightExchange, *, clock: Clock, fallback: FallbackFees
) -> OrderIntent | ProbeSkip:
    """The intent the planner would build right now for the first slot, with the real quote, ATR, fees and balance."""
    registry = await SpotRegistry.load(exchange, [cfg.product])
    meta = registry.meta(cfg.product)
    fees = await exchange.get_fee_rates()
    snapshot = await exchange.get_accounts()
    q = await exchange.get_best_bid_ask(cfg.product)
    atr_cache = AtrCache(exchange, cfg.product, clock)
    await atr_cache.refresh()
    atr = atr_cache.value()
    now = clock()
    free = snapshot.balances.get(meta.quote_currency, (ZERO, ZERO))[0]
    cap = min(cfg.max_entry_quote, cfg.max_total_loss_quote)
    facts = PlanFacts(
        now=now, slot=now, product_id=cfg.product, meta=meta, fees=fees, fallback=fallback, manual_stop=False,
        entry_blocked=None, limit_blocked=None, position_flat=True, open_orders=0, balance_age_s=0.0, free_quote=free,
        quote=MarketQuote(q.bid, q.ask), quote_age_s=0.0, atr1h=None if atr is None else atr[0],
        last_close=None if atr is None else atr[1],
        budget=BudgetState(limits.CAPITAL_REF_QUOTE, ZERO, cfg.max_total_loss_quote, ZERO, ZERO, cap),
    )  # fmt: skip
    return ProbePlanner(cfg).plan(facts)


async def preview(
    cfg: ExecProbeConfig,
    exchange: PreviewExchange,
    *,
    fallback: FallbackFees,
    clock: Clock,
) -> tuple[StageRecord, list[str]]:
    lines: list[str] = []
    intent = await first_entry_intent(cfg, exchange, clock=clock, fallback=fallback)
    if isinstance(intent, ProbeSkip):
        skipped = {"skip": intent.reason, "detail": intent.detail}
        lines.append(
            f"Impossibile preparare il primo ingresso adesso: {intent.reason} {intent.detail}. Riprova piu' tardi."
        )
        return StageRecord("preview", False, clock(), config_hash(cfg), details=skipped), lines

    gtd_spec = entry_spec(intent)
    gtc_intent = intent.model_copy(update={"entry": intent.entry.model_copy(update={"expires_on_exchange": False})})
    gtc_spec = entry_spec(gtc_intent).model_copy(
        update={"client_order_id": client_order_id(intent.decision_id, "preview-gtc")}
    )
    gtd = await _preview_one(exchange, gtd_spec)
    gtc = await _preview_one(exchange, gtc_spec)
    bracket = await _preview_bracket(exchange, intent)

    commission_ok = True
    for name, res in (("gtd", gtd), ("gtc", gtc)):
        if res.get("accepted") and res.get("commission_decimals", MIN_COMMISSION_DECIMALS) < MIN_COMMISSION_DECIMALS:
            commission_ok = False
            lines.append(
                f"commission_total ({name}) ha solo {res['commission_decimals']} decimali: arrotondata al centesimo."
            )
    entry_mode: EntryMode | None = "gtd" if gtd["accepted"] else ("gtc_fallback" if gtc["accepted"] else None)
    details: dict[str, Any] = {
        "intent": {
            "limit_price": str(intent.entry.limit_price), "base_size": str(intent.base_size),
            "take_profit": str(intent.bracket.take_profit), "stop_loss": str(intent.bracket.stop_loss),
        },  # fmt: skip
        "gtd": gtd, "gtc": gtc, "bracket_preview_expected_to_fail": bracket,
        "commission_precision_ok": commission_ok,
    }  # fmt: skip
    if entry_mode == "gtc_fallback":
        details["fallback_needs_user_decision"] = (
            "Coinbase refuses the GTD entry with post-only. The user accepted the risk ONLY for GTD: the live stage "
            "stays closed until exec_probe.accept_gtc_fallback is set after a new written acceptance."
        )
    if not commission_ok:
        details["stop"] = (
            "the fee criterion is not verifiable at this size: ask the user and the quant, do NOT raise Q_max"
        )
    ok = entry_mode is not None and commission_ok
    lines.append(
        f"GTD: {'accettato' if gtd['accepted'] else 'RIFIUTATO'}; "
        f"GTC equivalente: {'accettato' if gtc['accepted'] else 'RIFIUTATO'}"
    )
    lines.append(f"Modalita' d'ingresso decisa: {entry_mode}")
    return StageRecord("preview", ok, clock(), config_hash(cfg), entry_mode=entry_mode, details=details), lines


async def _preview_bracket(exchange: PreviewExchange, intent: OrderIntent) -> dict[str, Any]:
    spec = OrderSpec(
        client_order_id=client_order_id(intent.decision_id, "preview-bracket"), book=intent.book,
        product_id=intent.product_id, side="SELL", kind="bracket", base_size=intent.base_size,
        limit_price=intent.bracket.take_profit, stop_trigger_price=intent.bracket.stop_loss,
    )  # fmt: skip
    return await _preview_one(exchange, spec)


# ------------------------------------------------------------------------------------------ order stages


class OrderStageGateway(Protocol):
    async def submit_entry_with_bracket(self, intent: OrderIntent) -> EntryResult: ...
    async def cancel(self, product_id: str) -> CancelOutcome: ...


class OrderStageExchange(MarketReader, Protocol):
    async def get_order(self, order_id: str) -> OrderInfo: ...
    async def get_accounts(self) -> AccountSnapshot: ...
    async def get_fills(self, order_id: str) -> list[FillInfo]: ...


@dataclass
class OrderStageHost:
    """Everything the three order stages touch. ``runner.py`` fills it from the running runtime."""

    cfg: ExecProbeConfig
    meta: ProductMeta
    gateway: OrderStageGateway
    ledger: PositionLedger
    balances: BalanceBook
    exchange: OrderStageExchange
    clock: Clock
    sleep: Callable[[float], Awaitable[None]]
    raw_path: Path | None = None
    flush_raw: Callable[[], None] = lambda: None
    config_sha256: str = ""
    entry_mode: EntryMode | None = "gtd"
    message_wait_s: float = 12.0
    poll_s: float = 1.0
    notes: list[str] = field(default_factory=list)


async def _wait_for(pred: Callable[[], Awaitable[bool]], host: OrderStageHost, timeout_s: float) -> bool:
    waited = 0.0
    while True:
        if await pred():
            return True
        if waited >= timeout_s:
            return False
        await host.sleep(host.poll_s)
        waited += host.poll_s


async def _messages_agree(host: OrderStageHost, cid: str, statuses: tuple[str, ...]) -> dict[str, Any]:
    """The channel's ``user`` messages of the order, status by status, against REST ``get_order`` (table B)."""
    out: dict[str, Any] = {}
    if host.raw_path is None:
        return {"available": False}
    order_id = await host.ledger.get_order(cid)
    exchange_id = None if order_id is None else order_id.exchange_order_id
    if exchange_id is None:
        return {"available": False, "reason": "order has no exchange id"}
    info = await host.exchange.get_order(exchange_id)
    fills = await host.exchange.get_fills(exchange_id)
    by_status: dict[str, dict[str, Any]] = {}
    for _ in range(int(host.message_wait_s / host.poll_s) + 1):
        host.flush_raw()
        by_status = last_message_by_status(order_messages(host.raw_path), cid)
        if all(s in by_status for s in statuses):
            break
        await host.sleep(host.poll_s)
    out["available"] = True
    for status in statuses:
        msg = by_status.get(status)
        if msg is None:
            out[status] = {"seen": False}
            continue
        # the REST order is terminal by now; an OPEN message is compared on identity and sizes only
        diffs = compare_order(msg, info, len(fills)) if status != "OPEN" else compare_order_open(msg, info)
        out[status] = {"seen": True, "differences": diffs}
    return out


async def run_order_stage(stage: str, host: OrderStageHost) -> tuple[StageRecord, list[str]]:
    """``place-cancel``, ``expire`` or ``reject``. The order is real (2 EUR, far from the market or crossing it)."""
    if stage not in ("place-cancel", "expire", "reject"):
        raise StageAbort(f"{stage!r} is not an order stage")
    cfg, meta = host.cfg, host.meta
    lines: list[str] = []
    q: BestBidAsk = await host.exchange.get_best_bid_ask(cfg.product)
    now = host.clock()
    gtd = host.entry_mode != "gtc_fallback"  # with the fallback the exchange refuses GTD: the bot's own timeout ends it
    if stage == "place-cancel":
        intent = far_buy_intent(stage, q.bid, meta, cfg, now, tif_s=300, gtd=gtd)
    elif stage == "expire":
        intent = far_buy_intent(stage, q.bid, meta, cfg, now, tif_s=120, gtd=gtd)
    else:
        intent = crossing_buy_intent(stage, q.ask, meta, cfg, now, tif_s=300, gtd=gtd)
    cid = client_order_id(intent.decision_id, "entry")
    quote_free_before = host.balances.free(meta.quote_currency)
    lines.append(
        f"Ordine di prova '{stage}': BUY post-only {intent.base_size} {meta.base_currency} "
        f"a {intent.entry.limit_price} "
        f"(circa {intent.base_size * intent.entry.limit_price:.2f} {meta.quote_currency})"
    )
    task = asyncio.create_task(host.gateway.submit_entry_with_bracket(intent))
    hold_ok = True
    cancel_outcome: CancelOutcome | None = None
    opened_at: datetime | None = None
    reservation_seen = False
    try:
        if stage == "place-cancel":
            started = host.clock()
            while not task.done():
                rec = await host.ledger.get_order(cid)
                if rec is not None and rec.status == "OPEN":
                    if opened_at is None:
                        opened_at = host.clock()
                    reservation_seen = reservation_seen or host.balances.reservation(cid) is not None
                    snap = await host.exchange.get_accounts()
                    hold = snap.balances.get(meta.quote_currency, (ZERO, ZERO))[1]
                    if hold > host.balances.reserved(meta.quote_currency):
                        hold_ok = False
                    if (host.clock() - opened_at).total_seconds() >= 60:
                        cancel_outcome = await host.gateway.cancel(cfg.product)
                        break
                elif (host.clock() - started).total_seconds() > 60:
                    break  # never opened: the result below says why
                await host.sleep(host.poll_s)
        result = await task
    except BaseException:
        if not task.done():
            task.cancel()
        raise

    rec = await host.ledger.get_order(cid)
    status_in_ledger = None if rec is None else rec.status
    rest_status: str | None = None
    if rec is not None and rec.exchange_order_id is not None:
        rest_status = (await host.exchange.get_order(rec.exchange_order_id)).status
    released = host.balances.reservation(cid) is None
    quote_free_after = host.balances.free(meta.quote_currency)
    details: dict[str, Any] = {
        "decision_id": intent.decision_id,
        "result": {"status": result.status, "reason": result.reason, "filled_size": str(result.filled_size)},
        "ledger_status": status_in_ledger, "rest_status": rest_status, "reservation_released": released,
        "quote_free_before": str(quote_free_before), "quote_free_after": str(quote_free_after),
    }  # fmt: skip
    checks: dict[str, bool] = {"reservation_released": released, "no_fill": result.filled_size == 0}

    if stage == "place-cancel":
        checks["outcome_no_fill_cancelled"] = result.status == "no_fill" and result.reason == "cancelled"
        checks["cancelled_in_ledger_and_rest"] = status_in_ledger == "CANCELLED" and rest_status == "CANCELLED"
        checks["reservation_held_while_open"] = reservation_seen
        checks["hold_never_above_reserved"] = hold_ok
        checks["quote_free_restored"] = abs(quote_free_after - quote_free_before) <= meta.quote_increment
        details["cancel_outcome"] = None if cancel_outcome is None else cancel_outcome.status
        details["user_messages"] = await _messages_agree(host, cid, ("OPEN", "CANCELLED"))
        checks["user_messages_agree"] = _agree(details["user_messages"], ("OPEN", "CANCELLED"))
    elif stage == "expire":
        elapsed = (host.clock() - now).total_seconds()
        details["elapsed_s"] = elapsed
        checks["expired_by_the_exchange"] = result.status == "no_fill" and result.reason == "expired"
        checks["expired_in_ledger_and_rest"] = status_in_ledger == "EXPIRED" and rest_status == "EXPIRED"
        checks["within_end_time_plus_grace"] = elapsed <= 120 + cfg.gtd_grace_s + host.poll_s * 3
        checks["quote_free_restored"] = abs(quote_free_after - quote_free_before) <= meta.quote_increment
        details["user_messages"] = await _messages_agree(host, cid, ("EXPIRED",))
        checks["user_messages_agree"] = _agree(details["user_messages"], ("EXPIRED",))
    else:
        refused_by_exchange = result.status == "rejected" or (result.status == "no_fill" and result.reason != "expired")
        checks["refused_by_the_exchange"] = refused_by_exchange
        checks["not_executed_as_taker"] = result.status != "filled"
        checks["quote_free_restored"] = abs(quote_free_after - quote_free_before) <= meta.quote_increment
        if result.status == "filled":
            details["conflitto"] = (
                "a post-only BUY at the ask was executed: post-only is NOT honoured; the fill goes on as an entry"
            )
    ok = all(checks.values())
    details["checks"] = checks
    for name, value in checks.items():
        lines.append(f"  [{'OK' if value else 'NO'}] {name}")
    return StageRecord(stage, ok, host.clock(), host.config_sha256, details=details), lines


def _agree(messages: dict[str, Any], statuses: tuple[str, ...]) -> bool:
    if not messages.get("available"):
        return False
    return all(messages.get(s, {}).get("seen") and not messages[s].get("differences") for s in statuses)
