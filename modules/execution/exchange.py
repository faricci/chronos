"""The only door to Coinbase REST for orders and account reads (spec sez. 4.1.6, 4.3 punto 2, 5.3 punto 1; C11).

``CoinbaseExchange`` wraps the SDK's ``RESTClient`` (blocking, run with ``asyncio.to_thread``) and exposes an
ALLOWLIST of operations: nothing that moves funds, nothing derivative, nothing leveraged.

How "no order without a ticket" is enforced here:

* ``create_order`` is the only method that sends an order and its only input is a ``CoverageTicket``. The body is
  never passed in: it is built from ``ticket.consume()`` by ``build_order_body`` (spot.py), which accepts exactly the
  four order shapes of the spot matrix. A ticket is single use and must be a genuine ``CoverageTicket`` (an
  exact-type check, so a look-alike object or a subclass is refused).
* ``preview_order`` and ``cancel_orders`` cannot open a position. They are still write operations.
* ``read_only=True`` (dry mode) makes every write operation raise ``DryRunViolation`` before the rate limiter, the
  thread and the network. The decorator ``@write_operation`` marks them, and tests enumerate the marked methods.

Errors are typed. Reads are retried with backoff (they are idempotent). A write is NEVER retried: after an
ambiguous failure (timeout, 5xx, broken connection) the outcome is ``OutcomeUnknown`` and the caller keeps the
reservation until reconciliation finds the order by its deterministic ``client_order_id`` (sez. 4.1.5). A
cancelled ``await`` of a write can leave the request in flight: callers treat it as unknown as well.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from multiprocessing import AuthenticationError
from typing import Any, Concatenate, Final, Protocol

import requests
import structlog
from aiolimiter import AsyncLimiter
from pydantic import BaseModel, ConfigDict, ValidationError
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential
from tenacity.wait import wait_base

from modules.config import CoinbaseConfig
from modules.execution.balance import AccountSnapshot, Clock, utc_now
from modules.execution.balance_guard import CoverageTicket
from modules.execution.orders import OrderSpec, ProductMeta
from modules.execution.spot import SpotRegistry, SpotViolation, build_order_body
from modules.risk.costs import FeeRates

log = structlog.get_logger(__name__)

ZERO: Final = Decimal(0)
BPS: Final = Decimal(10_000)
DEFAULT_RATE_LIMIT_PER_S: Final = 20.0  # private endpoints (spec sez. 4.3 punto 2)
DEFAULT_READ_ATTEMPTS: Final = 4
MAX_CANCEL_BATCH: Final = 100
MAX_PAGES: Final = 50  # a paginated read that does not end is a bug, not a reason to loop forever
TERMINAL_STATUSES: Final = frozenset({"FILLED", "CANCELLED", "EXPIRED", "FAILED"})
OPEN_STATUSES: Final = ("OPEN",)

# Which SDK entry points are writes (POST/DELETE) and which are reads. Both sets together are the allowlist of
# tests/test_c11_wiring.py; the introspection test checks they classify every SDK call this module makes.
WRITE_SDK_METHODS: Final = frozenset({"create_order", "cancel_orders", "preview_order"})
READ_SDK_METHODS: Final = frozenset(
    {
        "get_product", "get_transaction_summary", "get_best_bid_ask", "list_orders", "get_order", "get_fills",
        "get_accounts", "get_api_key_permissions", "get_public_candles", "get_candles",
    }
)  # fmt: skip

_WRITE_MARK: Final = "__chronos_write_operation__"


# ------------------------------------------------------------------------------------------------ errors


class DryRunViolation(Exception):
    """A write was attempted on a read-only exchange (dry mode). A programming error, never swallowed."""


class ExchangeError(Exception):
    """Base of the failures that come from talking to the exchange."""


class ExchangeAuthError(ExchangeError):
    """Missing, invalid or insufficient credentials (HTTP 401/403, no key). Retrying cannot help."""


class ExchangeUnavailable(ExchangeError):
    """A read kept failing for a transient reason (network, 429, 5xx) after the retries."""


class ExchangeRequestError(ExchangeError):
    """The exchange refused a request (HTTP 4xx). Definitive: the request had no effect."""

    def __init__(self, status: int | None, reason: str, message: str) -> None:
        super().__init__(f"HTTP {status}: {reason}: {message}")
        self.status = status
        self.reason = reason
        self.message = message


class ExchangeNotFound(ExchangeRequestError):
    pass


class ExchangeResponseError(ExchangeError):
    """The exchange answered, but not with the shape or values we rely on."""


class OrderRejected(ExchangeError):
    """The exchange definitively refused the order (``success: false`` or HTTP 4xx). Nothing is open."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason
        self.message = message


class OutcomeUnknown(ExchangeError):
    """A write may or may not have been executed. Never retry blindly: reconcile by ``client_order_id``."""

    def __init__(self, operation: str, client_order_id: str | None, cause: str) -> None:
        super().__init__(f"{operation} outcome unknown ({cause})")
        self.operation = operation
        self.client_order_id = client_order_id


# ------------------------------------------------------------------------------------------------- results


class CreateOrderResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    order_id: str
    client_order_id: str
    product_id: str
    side: str


class CancelResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    order_id: str
    success: bool
    failure_reason: str | None = None


class OrderInfo(BaseModel):
    """One order as the exchange reports it. ``side`` and ``status`` stay raw strings: foreign orders are read too."""

    model_config = ConfigDict(frozen=True)

    order_id: str
    client_order_id: str
    product_id: str
    side: str
    status: str
    base_size: Decimal | None = None
    limit_price: Decimal | None = None
    stop_trigger_price: Decimal | None = None
    filled_size: Decimal = ZERO
    average_filled_price: Decimal | None = None
    total_fees: Decimal = ZERO

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def leaves_size(self) -> Decimal | None:
        return None if self.base_size is None else max(self.base_size - self.filled_size, ZERO)


class FillInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    trade_id: str
    order_id: str
    product_id: str
    side: str
    price: Decimal
    size: Decimal  # base currency
    fee: Decimal  # quote currency
    liquidity: str
    trade_time: datetime


class BestBidAsk(BaseModel):
    model_config = ConfigDict(frozen=True)

    product_id: str
    bid: Decimal
    ask: Decimal


class PreviewResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    order_total: Decimal | None = None
    commission_total: Decimal | None = None


@dataclass(slots=True)
class KeyPermissionsInfo:
    """Structurally a ``spot.KeyPermissions``; ``assert_live_key_permissions`` is the only consumer of the flags."""

    can_view: bool
    can_trade: bool
    can_transfer: bool
    portfolio_type: str


class SdkClient(Protocol):
    """The part of ``RESTClient`` this module may touch: exactly ``READ_SDK_METHODS | WRITE_SDK_METHODS``."""

    def get_product(self, *args: Any, **kwargs: Any) -> Any: ...
    def get_transaction_summary(self, *args: Any, **kwargs: Any) -> Any: ...
    def get_best_bid_ask(self, *args: Any, **kwargs: Any) -> Any: ...
    def create_order(self, *args: Any, **kwargs: Any) -> Any: ...
    def cancel_orders(self, *args: Any, **kwargs: Any) -> Any: ...
    def list_orders(self, *args: Any, **kwargs: Any) -> Any: ...
    def get_order(self, *args: Any, **kwargs: Any) -> Any: ...
    def get_fills(self, *args: Any, **kwargs: Any) -> Any: ...
    def get_accounts(self, *args: Any, **kwargs: Any) -> Any: ...
    def get_api_key_permissions(self, *args: Any, **kwargs: Any) -> Any: ...
    def preview_order(self, *args: Any, **kwargs: Any) -> Any: ...
    def get_public_candles(self, *args: Any, **kwargs: Any) -> Any: ...
    def get_candles(self, *args: Any, **kwargs: Any) -> Any: ...


# --------------------------------------------------------------------------------------- write operations


def write_operation[**P, R](
    fn: Callable[Concatenate[CoinbaseExchange, P], Awaitable[R]],
) -> Callable[Concatenate[CoinbaseExchange, P], Awaitable[R]]:
    """Mark a method as a write (POST/DELETE) and refuse it, before anything else, on a read-only exchange."""

    @functools.wraps(fn)
    async def guarded(self: CoinbaseExchange, /, *args: P.args, **kwargs: P.kwargs) -> R:
        if self.read_only:
            name = getattr(fn, "__name__", "operation")
            raise DryRunViolation(f"{name} is a write operation and this exchange is read-only (dry mode)")
        return await fn(self, *args, **kwargs)

    setattr(guarded, _WRITE_MARK, True)
    return guarded


def write_methods(cls: type) -> frozenset[str]:
    """Names of the public methods of ``cls`` marked with ``@write_operation`` (introspection, used by the tests)."""
    return frozenset(
        name
        for name, member in inspect.getmembers(cls, predicate=inspect.isfunction)
        if not name.startswith("_") and getattr(member, _WRITE_MARK, False)
    )


# ------------------------------------------------------------------------------------------------ parsing


def _as_dict(resp: Any) -> dict[str, Any]:
    """SDK responses are objects with ``to_dict``; fakes and older versions return plain dicts."""
    data = resp.to_dict() if hasattr(resp, "to_dict") else resp
    if not isinstance(data, dict):
        raise ExchangeResponseError(f"expected a JSON object, got {type(data).__name__}")
    return data


def _decimal(value: Any, what: str) -> Decimal:
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        raise ExchangeResponseError(f"{what}: {value!r} is not a number") from None
    if not d.is_finite():
        raise ExchangeResponseError(f"{what}: {value!r} is not finite")
    return d


def _opt_decimal(value: Any, what: str) -> Decimal | None:
    return None if value in (None, "") else _decimal(value, what)


def _amount(raw: Any, what: str) -> Decimal:
    """``{"value": "1.5", "currency": "EUR"}`` -> Decimal. Never negative."""
    value = raw.get("value") if isinstance(raw, Mapping) else raw
    d = _decimal(value, what)
    if d < 0:
        raise ExchangeResponseError(f"{what}: negative amount {d}")
    return d


def _time(value: Any, what: str) -> datetime:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise ExchangeResponseError(f"{what}: {value!r} is not a timestamp") from None


def _str(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ExchangeResponseError(f"missing field {key!r}")
    return value


def parse_order(raw: Mapping[str, Any]) -> OrderInfo:
    config = raw.get("order_configuration")
    inner: Mapping[str, Any] = {}
    if isinstance(config, Mapping):
        first = next(iter(config.values()), None)
        inner = first if isinstance(first, Mapping) else {}
    stop = inner.get("stop_trigger_price", inner.get("stop_price"))
    average = _opt_decimal(raw.get("average_filled_price"), "average_filled_price")
    return OrderInfo(
        order_id=_str(raw, "order_id"),
        client_order_id=str(raw.get("client_order_id") or ""),
        product_id=_str(raw, "product_id"),
        side=str(raw.get("side") or ""),
        status=_str(raw, "status"),
        base_size=_opt_decimal(inner.get("base_size"), "base_size"),
        limit_price=_opt_decimal(inner.get("limit_price"), "limit_price"),
        stop_trigger_price=_opt_decimal(stop, "stop_trigger_price"),
        filled_size=_opt_decimal(raw.get("filled_size"), "filled_size") or ZERO,
        average_filled_price=average if average else None,
        total_fees=_opt_decimal(raw.get("total_fees"), "total_fees") or ZERO,
    )


def parse_fill(raw: Mapping[str, Any]) -> FillInfo:
    if str(raw.get("size_in_quote")).lower() == "true":
        raise ExchangeResponseError("fill size is expressed in quote currency: unsupported for base-sized orders")
    return FillInfo(
        trade_id=_str(raw, "trade_id"),
        order_id=_str(raw, "order_id"),
        product_id=_str(raw, "product_id"),
        side=str(raw.get("side") or ""),
        price=_decimal(raw.get("price"), "price"),
        size=_decimal(raw.get("size"), "size"),
        fee=_decimal(raw.get("commission", "0"), "commission"),
        liquidity=str(raw.get("liquidity_indicator") or "UNKNOWN_LIQUIDITY_INDICATOR"),
        trade_time=_time(raw.get("trade_time"), "trade_time"),
    )


def parse_balances(accounts: Sequence[Mapping[str, Any]]) -> dict[str, tuple[Decimal, Decimal]]:
    """Accounts -> ``currency -> (available, hold)``. Several accounts of one currency are added up."""
    out: dict[str, tuple[Decimal, Decimal]] = {}
    for acc in accounts:
        currency = _str(acc, "currency")
        available = _amount(acc.get("available_balance", {"value": "0"}), f"{currency} available_balance")
        hold = _amount(acc.get("hold") or {"value": "0"}, f"{currency} hold")
        a0, h0 = out.get(currency, (ZERO, ZERO))
        out[currency] = (a0 + available, h0 + hold)
    return out


# ------------------------------------------------------------------------------------- error translation


def _status_of(exc: BaseException) -> int | None:
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        return int(exc.response.status_code)
    return None


def _error_body(exc: BaseException) -> tuple[str, str]:
    """``(reason, message)`` from a Coinbase error body, falling back to the HTTP status."""
    status = _status_of(exc)
    fallback = f"http_{status}" if status is not None else type(exc).__name__
    response = getattr(exc, "response", None)
    try:
        body = response.json() if response is not None else None
    except ValueError:
        body = None
    if isinstance(body, Mapping):
        reason = str(body.get("error") or body.get("error_details") or fallback)
        return reason, str(body.get("message") or body.get("error_details") or exc)
    return fallback, str(exc)


def _is_transient(exc: BaseException) -> bool:
    """429, 5xx and network failures are worth a retry (reads only); every other 4xx is final."""
    if isinstance(exc, requests.exceptions.HTTPError):
        status = _status_of(exc)
        return status is None or status == 408 or status == 429 or status >= 500
    return isinstance(exc, requests.exceptions.ConnectionError | requests.exceptions.Timeout)


def _is_definitive_refusal(exc: BaseException) -> bool:
    """A 4xx other than 408 means the request was understood and refused: it had no effect."""
    status = _status_of(exc)
    return status is not None and 400 <= status < 500 and status != 408


def _read_error(exc: Exception) -> ExchangeError:
    status = _status_of(exc)
    if isinstance(exc, AuthenticationError) or status in (401, 403):
        return ExchangeAuthError(str(exc))
    if _is_transient(exc):
        return ExchangeUnavailable(f"{type(exc).__name__}: {exc}")
    if status is not None:
        reason, message = _error_body(exc)
        cls = ExchangeNotFound if status == 404 else ExchangeRequestError
        return cls(status, reason, message)
    return ExchangeError(f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------------------------- exchange


class CoinbaseExchange:
    """Async wrapper around the blocking SDK client. ``read_only`` is mandatory: nobody forgets it by default."""

    def __init__(
        self,
        client: SdkClient,
        *,
        read_only: bool,
        rate_limit_per_s: float = DEFAULT_RATE_LIMIT_PER_S,
        read_attempts: int = DEFAULT_READ_ATTEMPTS,
        retry_wait: wait_base | None = None,
        clock: Clock = utc_now,
    ) -> None:
        if rate_limit_per_s <= 0 or read_attempts < 1:
            raise ValueError("rate_limit_per_s must be positive and read_attempts at least 1")
        self._client = client
        self.read_only = read_only
        self._limiter = AsyncLimiter(rate_limit_per_s, 1)
        self._read_attempts = read_attempts
        self._retry_wait = retry_wait or wait_exponential(multiplier=0.5, min=0.5, max=8)
        self._clock = clock

    # ------------------------------------------------------------------------------------- transport

    async def _read(self, fn: Callable[..., Any], **params: Any) -> dict[str, Any]:
        """One idempotent GET: rate limited, retried with backoff on transient failures, errors typed."""
        retrying = AsyncRetrying(
            retry=retry_if_exception(_is_transient),
            stop=stop_after_attempt(self._read_attempts),
            wait=self._retry_wait,
            reraise=True,
        )
        resp: Any = None
        try:
            async for attempt in retrying:
                with attempt:
                    async with self._limiter:
                        resp = await asyncio.to_thread(fn, **params)
        except ExchangeError:
            raise
        except Exception as exc:
            raise _read_error(exc) from exc
        return _as_dict(resp)

    async def _write(self, operation: str, fn: Callable[..., Any], tag: str | None, /, **params: Any) -> Any:
        """One POST/DELETE, never retried. Definitive refusals are typed; anything ambiguous is ``OutcomeUnknown``.

        ``tag`` (the client_order_id, if any) only labels the unknown outcome; it is not sent."""
        try:
            async with self._limiter:
                resp = await asyncio.to_thread(fn, **params)
            return _as_dict(resp)
        except Exception as exc:
            status = _status_of(exc)
            if isinstance(exc, AuthenticationError) or status in (401, 403):
                raise ExchangeAuthError(str(exc)) from exc
            if _is_definitive_refusal(exc):
                reason, message = _error_body(exc)
                raise ExchangeRequestError(status, reason, message) from exc
            log.error("write_outcome_unknown", operation=operation, error=f"{type(exc).__name__}: {exc}")
            raise OutcomeUnknown(operation, tag, f"{type(exc).__name__}: {exc}") from exc

    async def _pages(self, fn: Callable[..., Any], items_key: str, **params: Any) -> list[dict[str, Any]]:
        """Follow ``cursor``/``has_next`` to the end. A cursor that is missing or repeats is an error."""
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(MAX_PAGES):
            data = await self._read(fn, **params, **({"cursor": cursor} if cursor else {}))
            page = data.get(items_key) or []
            if not isinstance(page, list):
                raise ExchangeResponseError(f"{items_key!r} is not a list")
            items.extend(x for x in page if isinstance(x, dict))
            if not data.get("has_next", False) and not (items_key == "fills" and data.get("cursor")):
                return items
            cursor = data.get("cursor") or None
            if cursor is None or cursor in seen:
                raise ExchangeResponseError(f"pagination of {items_key!r} has no usable cursor")
            seen.add(cursor)
        raise ExchangeResponseError(f"pagination of {items_key!r} exceeded {MAX_PAGES} pages")

    # ------------------------------------------------------------------------------------------ reads

    async def get_product_raw(self, product_id: str) -> dict[str, Any]:
        return await self._read(self._client.get_product, product_id=product_id)

    async def get_product(self, product_id: str) -> ProductMeta:
        """Increments and minimum sizes of a verified SPOT product (``SpotViolation`` otherwise)."""
        raw = await self.get_product_raw(product_id)
        return SpotRegistry.from_raw({product_id: raw}).meta(product_id)

    async def get_fee_rates(self) -> FeeRates:
        data = await self._read(self._client.get_transaction_summary)
        tier = data.get("fee_tier")
        if not isinstance(tier, Mapping):
            raise ExchangeResponseError("transaction summary has no fee_tier")
        maker = _decimal(tier.get("maker_fee_rate"), "maker_fee_rate") * BPS
        taker = _decimal(tier.get("taker_fee_rate"), "taker_fee_rate") * BPS
        try:
            return FeeRates(maker_bps=float(maker), taker_bps=float(taker), assumed=False, as_of=self._clock())
        except ValidationError as exc:
            raise ExchangeResponseError(f"implausible fee tier: {exc}") from exc

    async def get_best_bid_ask(self, product_id: str) -> BestBidAsk:
        data = await self._read(self._client.get_best_bid_ask, product_ids=[product_id])
        for book in data.get("pricebooks") or []:
            if isinstance(book, Mapping) and book.get("product_id") == product_id:
                bids, asks = book.get("bids") or [], book.get("asks") or []
                if not bids or not asks:
                    raise ExchangeResponseError(f"{product_id}: empty side of the book")
                bid = _decimal(bids[0].get("price"), "bid")
                ask = _decimal(asks[0].get("price"), "ask")
                if bid <= 0 or ask <= 0 or bid >= ask:
                    raise ExchangeResponseError(f"{product_id}: inconsistent quote bid={bid} ask={ask}")
                return BestBidAsk(product_id=product_id, bid=bid, ask=ask)
        raise ExchangeResponseError(f"{product_id}: no quote in the response")

    async def list_orders(
        self,
        *,
        statuses: Sequence[str] | None = None,
        product_ids: Sequence[str] | None = None,
        order_ids: Sequence[str] | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[OrderInfo]:
        raw = await self._pages(
            self._client.list_orders,
            "orders",
            order_status=list(statuses) if statuses else None,
            product_ids=list(product_ids) if product_ids else None,
            order_ids=list(order_ids) if order_ids else None,
            start_date=start_date,
            end_date=end_date,
        )
        return [parse_order(o) for o in raw]

    async def list_open_orders(self) -> list[OrderInfo]:
        return await self.list_orders(statuses=OPEN_STATUSES)

    async def get_order(self, order_id: str) -> OrderInfo:
        data = await self._read(self._client.get_order, order_id=order_id)
        order = data.get("order")
        if not isinstance(order, Mapping):
            raise ExchangeResponseError(f"get_order {order_id}: no order in the response")
        return parse_order(order)

    async def get_fills(self, order_id: str) -> list[FillInfo]:
        raw = await self._pages(self._client.get_fills, "fills", order_ids=[order_id])
        return [parse_fill(f) for f in raw]

    async def get_accounts(self) -> AccountSnapshot:
        """Balances (``available``, ``hold``) per currency. ``taken_at`` is the instant the read STARTED, so the age
        the guard sees can only be over-estimated. Foreign-order currencies are filled in by the balance feed."""
        taken_at = self._clock()
        accounts = await self._pages(self._client.get_accounts, "accounts", limit=250)
        return AccountSnapshot(balances=parse_balances(accounts), taken_at=taken_at)

    async def get_api_key_permissions(self) -> KeyPermissionsInfo:
        data = await self._read(self._client.get_api_key_permissions)
        flags = {k: data.get(k) for k in ("can_view", "can_trade", "can_transfer")}
        if any(not isinstance(v, bool) for v in flags.values()):
            raise ExchangeResponseError(f"key permissions are incomplete: {flags}")
        return KeyPermissionsInfo(
            can_view=bool(flags["can_view"]),
            can_trade=bool(flags["can_trade"]),
            can_transfer=bool(flags["can_transfer"]),
            portfolio_type=str(data.get("portfolio_type") or ""),
        )

    async def get_candles(
        self, product_id: str, start_s: int, end_s: int, granularity: str = "ONE_MINUTE"
    ) -> list[dict[str, Any]]:
        data = await self._read(
            self._client.get_candles,
            product_id=product_id,
            start=str(start_s),
            end=str(end_s),
            granularity=granularity,
        )
        return _candles(data)

    async def get_public_candles(
        self, product_id: str, start_s: int, end_s: int, granularity: str = "ONE_MINUTE"
    ) -> list[dict[str, Any]]:
        data = await self._read(
            self._client.get_public_candles,
            product_id=product_id,
            start=str(start_s),
            end=str(end_s),
            granularity=granularity,
        )
        return _candles(data)

    # ----------------------------------------------------------------------------------------- writes

    @write_operation
    async def create_order(self, ticket: CoverageTicket) -> CreateOrderResult:
        """Send the one order the ticket stands for. The body comes from the ticket, never from the caller."""
        if type(ticket) is not CoverageTicket:
            raise SpotViolation(f"create_order needs a CoverageTicket issued by BalanceGuard, got {type(ticket)!r}")
        spec = ticket.consume()  # single use: a second call raises SpotViolation
        payload = build_order_body(spec)
        try:
            data = await self._write("create_order", self._client.create_order, spec.client_order_id, **payload)
        except ExchangeRequestError as exc:
            raise OrderRejected(exc.reason, exc.message) from exc
        return _created(data, spec)

    @write_operation
    async def cancel_orders(self, order_ids: Sequence[str]) -> list[CancelResult]:
        ids = list(order_ids)
        if not ids or len(ids) > MAX_CANCEL_BATCH or len(set(ids)) != len(ids) or not all(ids):
            raise ValueError(f"cancel_orders takes 1 to {MAX_CANCEL_BATCH} distinct non-empty ids")
        data = await self._write("cancel_orders", self._client.cancel_orders, None, order_ids=ids)
        results = data.get("results")
        if not isinstance(results, list):
            raise OutcomeUnknown("cancel_orders", None, "response has no results")
        return [
            CancelResult(
                order_id=str(r.get("order_id", "")),
                success=bool(r.get("success")),
                failure_reason=None if r.get("success") else str(r.get("failure_reason") or "UNKNOWN"),
            )
            for r in results
            if isinstance(r, Mapping)
        ]

    @write_operation
    async def preview_order(self, spec: OrderSpec) -> PreviewResult:
        """``POST /orders/preview``: validates the body with the exchange without creating an order or a hold."""
        payload = build_order_body(spec)
        try:
            data = await self._write("preview_order", self._client.preview_order, spec.client_order_id, **payload)
        except ExchangeRequestError as exc:
            raise OrderRejected(exc.reason, exc.message) from exc
        return PreviewResult(
            errors=_messages(data.get("errs")),
            warnings=_messages(data.get("warning")),
            order_total=_opt_decimal(data.get("order_total"), "order_total"),
            commission_total=_opt_decimal(data.get("commission_total"), "commission_total"),
        )


# ------------------------------------------------------------------------------------ response helpers


def _candles(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    candles = data.get("candles")
    if not isinstance(candles, list):
        raise ExchangeResponseError("response has no candles")
    return [c for c in candles if isinstance(c, dict)]


def _messages(items: Any) -> tuple[str, ...]:
    if not isinstance(items, list):
        return ()
    return tuple(str(i.get("message") or i.get("error") or i) if isinstance(i, Mapping) else str(i) for i in items)


def _created(data: Mapping[str, Any], spec: OrderSpec) -> CreateOrderResult:
    """The create-order answer. ``success: false`` is a clean refusal; a success we cannot read is NOT a refusal."""
    if data.get("success") is False:
        err = data.get("error_response")
        error: Mapping[str, Any] = err if isinstance(err, Mapping) else {}
        reason = str(
            error.get("error") or error.get("new_order_failure_reason") or data.get("failure_reason") or "unknown"
        )
        detail = error.get("message") or error.get("error_details") or error.get("preview_failure_reason")
        message = str(detail or reason)
        raise OrderRejected(reason, message)
    ok = data.get("success_response")
    order_id = ok.get("order_id") if isinstance(ok, Mapping) else None
    if data.get("success") is not True or not isinstance(order_id, str) or not order_id:
        raise OutcomeUnknown("create_order", spec.client_order_id, "success response without an order_id")
    echoed = ok.get("client_order_id") if isinstance(ok, Mapping) else None
    if echoed not in (None, spec.client_order_id):
        raise OutcomeUnknown("create_order", spec.client_order_id, f"exchange echoed client_order_id {echoed!r}")
    log.info("order_sent", client_order_id=spec.client_order_id, product_id=spec.product_id, side=spec.side)
    return CreateOrderResult(
        order_id=order_id, client_order_id=spec.client_order_id, product_id=spec.product_id, side=spec.side
    )


def build_exchange(cfg: CoinbaseConfig, *, read_only: bool, timeout_s: int = 10, **kwargs: Any) -> CoinbaseExchange:
    """The production wiring: a ``RESTClient`` from the credentials in the environment (never from the YAML)."""
    from coinbase.rest import RESTClient

    key = cfg.api_key.get_secret_value() if cfg.api_key else None
    secret = cfg.api_secret.get_secret_value() if cfg.api_secret else None
    client = RESTClient(api_key=key, api_secret=secret, timeout=timeout_s)
    return CoinbaseExchange(client, read_only=read_only, **kwargs)
