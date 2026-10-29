"""CoinbaseExchange: the only door to the exchange (vincolo C11). No network, no key: every client is a fake.

Proves:
* an order leaves only from a genuine, unused CoverageTicket, with a body that comes from the ticket (T12);
* read_only refuses every write before any call, and the writes are found by introspection of the REAL SDK (T11);
* a write is never retried, a read is, and every failure has a type.
"""

from __future__ import annotations

import ast
import asyncio
import copy
import inspect
import re
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import requests
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from tenacity import wait_none

from modules.execution import exchange as exchange_module
from modules.execution.balance_guard import CoverageTicket
from modules.execution.exchange import (
    READ_SDK_METHODS,
    WRITE_SDK_METHODS,
    CoinbaseExchange,
    DryRunViolation,
    ExchangeAuthError,
    ExchangeNotFound,
    ExchangeRequestError,
    ExchangeResponseError,
    ExchangeUnavailable,
    OrderRejected,
    OutcomeUnknown,
    parse_balances,
    write_methods,
)
from modules.execution.orders import OrderSpec
from modules.execution.spot import TOP_LEVEL_KEYS, SpotViolation, assert_live_key_permissions, build_order_body
from tests.coverage_fixtures import RAW_SOL, T0, buy, make_guard, sell

D = Decimal
EXCHANGE_PY = Path(exchange_module.__file__)


# ----------------------------------------------------------------------------------------------- fakes


def http_error(status: int, body: dict[str, Any] | None = None) -> requests.exceptions.HTTPError:
    import json

    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(body if body is not None else {}).encode()
    return requests.exceptions.HTTPError(f"{status} error", response=response)


class FakeClient:
    """Stand-in for the SDK client. It knows ONLY the allowlisted methods: any other attribute is an error."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.script: dict[str, list[Any]] = {}

    def will(self, name: str, *outcomes: Any) -> None:
        """Queue results (values, exceptions or callables). The last one repeats."""
        self.script[name] = list(outcomes)

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.calls if n == name)

    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name not in READ_SDK_METHODS | WRITE_SDK_METHODS:
            raise AttributeError(f"{name} is outside the SDK allowlist")

        def call(**kwargs: Any) -> Any:
            self.calls.append((name, kwargs))
            queue = self.script.get(name)
            if not queue:
                raise AssertionError(f"unscripted call to {name}")
            outcome = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome(**kwargs) if callable(outcome) else outcome

        return call


def make_exchange(
    client: FakeClient | None = None, *, read_only: bool = False, **kwargs: Any
) -> tuple[CoinbaseExchange, FakeClient]:
    client = client or FakeClient()
    kwargs.setdefault("retry_wait", wait_none())
    return CoinbaseExchange(client, read_only=read_only, clock=lambda: T0, **kwargs), client  # type: ignore[arg-type]


def ticket_for(spec: OrderSpec | None = None, **guard_kwargs: str) -> CoverageTicket:
    guard, *_ = make_guard(**{"quote": "1000", "base": "1", "owned": "1", **guard_kwargs})
    return guard.authorize(spec or buy())


def created(cid: str = "b1") -> dict[str, Any]:
    return {
        "success": True,
        "success_response": {"order_id": "ex-1", "product_id": "SOL-EUR", "side": "BUY", "client_order_id": cid},
    }


# ------------------------------------------------------------------------------ T12: the ticket rules


async def test_create_order_sends_exactly_the_body_built_from_the_ticket() -> None:
    ex, client = make_exchange()
    client.will("create_order", created())
    spec = buy()
    result = await ex.create_order(ticket_for(spec))
    assert client.calls == [("create_order", build_order_body(spec))]
    assert (result.order_id, result.client_order_id, result.product_id, result.side) == ("ex-1", "b1", "SOL-EUR", "BUY")


async def test_create_order_requires_a_fresh_ticket() -> None:  # T12
    ex, client = make_exchange()
    client.will("create_order", created())
    ticket = ticket_for()
    await ex.create_order(ticket)
    assert ticket.consumed
    with pytest.raises(SpotViolation, match="already used"):
        await ex.create_order(ticket)  # a used ticket is dead
    assert client.count("create_order") == 1


class _LookAlike:
    """Quacks like a ticket (``consume`` returns a spec) but was not issued by the guard."""

    consumed = False

    def consume(self) -> OrderSpec:
        return buy()


class _Subclass(CoverageTicket):
    def consume(self) -> OrderSpec:
        return buy()


@pytest.mark.parametrize("not_a_ticket", [buy(), build_order_body(buy()), _LookAlike(), None])
async def test_anything_that_is_not_a_guard_ticket_is_refused_before_the_network(not_a_ticket: Any) -> None:
    ex, client = make_exchange()
    client.will("create_order", created())
    with pytest.raises(SpotViolation):
        await ex.create_order(not_a_ticket)
    assert client.calls == []


async def test_a_subclass_of_the_ticket_is_refused_too() -> None:
    real = ticket_for()
    forged = object.__new__(_Subclass)  # bypasses __init__, so no guard involved
    for slot, value in (("_spec", real.spec), ("_consumed", False), ("reserved", D(1)), ("currency", "EUR")):
        object.__setattr__(forged, slot, value)
    ex, client = make_exchange()
    with pytest.raises(SpotViolation):
        await ex.create_order(forged)
    assert client.calls == []


def test_a_ticket_cannot_be_built_outside_the_guard() -> None:
    with pytest.raises(SpotViolation):
        CoverageTicket(buy(), D(1), "EUR")


async def test_a_buy_market_never_gets_a_ticket() -> None:
    guard, *_ = make_guard()
    with pytest.raises(SpotViolation):
        guard.authorize(sell(kind="market").model_copy(update={"side": "BUY"}))


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    size_units=st.integers(1, 50_000),
    price_units=st.integers(1, 5_000_000),
    post_only=st.booleans(),
    kind=st.sampled_from(["buy_limit", "sell_limit", "sell_bracket", "sell_market"]),
)
def test_whatever_the_ticket_the_wire_body_is_a_plain_spot_order(
    size_units: int, price_units: int, post_only: bool, kind: str
) -> None:
    assume(kind != "sell_bracket" or price_units >= 2)
    size, price = str(D(size_units) / 1000), str(D(price_units) / 100)
    spec = {
        "buy_limit": buy("p1", size, price, post_only),
        "sell_limit": sell("p1", size, "limit", price),
        "sell_bracket": sell("p1", size, "bracket", price, str(D(max(price_units // 2, 1)) / 100)),
        "sell_market": sell("p1", size),
    }[kind]
    ex, client = make_exchange()
    client.will("create_order", created("p1"))
    ticket = ticket_for(spec, quote="100000000", base="100000", owned="100000")
    asyncio.run(ex.create_order(ticket))
    ((name, sent),) = client.calls
    assert name == "create_order" and set(sent) == TOP_LEVEL_KEYS and sent == build_order_body(spec)
    assert ticket.consumed


# --------------------------------------------------------------------- T11: allowlist and read_only


def public_async_methods(cls: type) -> set[str]:
    return {n for n, m in inspect.getmembers(cls, inspect.iscoroutinefunction) if not n.startswith("_")}


def sdk_calls_by_public_method() -> dict[str, set[str]]:
    """Static view: public method -> SDK attributes (``self._client.<x>``) its body mentions."""
    tree = ast.parse(EXCHANGE_PY.read_text(encoding="utf-8"))
    cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "CoinbaseExchange")
    out: dict[str, set[str]] = {}
    for fn in cls.body:
        if isinstance(fn, ast.AsyncFunctionDef | ast.FunctionDef) and not fn.name.startswith("_"):
            out[fn.name] = {
                a.attr
                for a in ast.walk(fn)
                if isinstance(a, ast.Attribute) and isinstance(a.value, ast.Attribute) and a.value.attr == "_client"
            }
    return out


def test_every_sdk_entry_point_is_classified_as_read_or_write() -> None:
    assert not READ_SDK_METHODS & WRITE_SDK_METHODS
    used = set().union(*sdk_calls_by_public_method().values())
    assert used == READ_SDK_METHODS | WRITE_SDK_METHODS  # nothing unused, nothing unclassified


def test_the_methods_that_touch_a_write_entry_point_are_exactly_the_marked_ones() -> None:
    static = {name for name, calls in sdk_calls_by_public_method().items() if calls & WRITE_SDK_METHODS}
    assert static == write_methods(CoinbaseExchange) == {"create_order", "cancel_orders", "preview_order"}


@pytest.mark.parametrize("name", sorted(write_methods(CoinbaseExchange)))
async def test_read_only_refuses_every_write_before_any_call(name: str) -> None:
    ex, client = make_exchange(read_only=True)
    ticket = ticket_for()
    arg = {"create_order": ticket, "cancel_orders": ["o1"], "preview_order": buy()}[name]
    with pytest.raises(DryRunViolation):
        await getattr(ex, name)(arg)
    assert client.calls == []
    assert not ticket.consumed  # refused before the ticket is touched


async def test_read_only_still_reads() -> None:
    ex, client = make_exchange(read_only=True)
    client.will("get_accounts", {"accounts": [], "has_next": False})
    assert (await ex.get_accounts()).balances == {}


def test_read_only_is_a_mandatory_choice() -> None:
    with pytest.raises(TypeError):
        CoinbaseExchange(FakeClient())  # type: ignore[call-arg, arg-type]


# --- the same, against the real SDK code paths: which public methods really POST/DELETE?


@pytest.fixture
def real_sdk(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, Any] | None]]:
    """A real ``RESTClient`` whose HTTP layer is replaced: records (verb, path, body without Nones)."""
    from coinbase.rest import rest_base

    sent: list[tuple[str, str, dict[str, Any] | None]] = []
    order = {
        "order_id": "ex-1", "product_id": "SOL-EUR", "side": "BUY", "client_order_id": "b1", "status": "OPEN",
        "order_configuration": {"limit_limit_gtc": {"base_size": "0.024", "limit_price": "92.03", "post_only": True}},
        "filled_size": "0.010", "average_filled_price": "92.03", "total_fees": "0.006",
    }  # fmt: skip
    routes: list[tuple[str, str, Any]] = [
        ("POST", r"/orders/batch_cancel$", {"results": [{"success": True, "order_id": "o1"}]}),
        ("POST", r"/orders/preview$", {"order_total": "2.2", "commission_total": "0.01", "errs": [], "warning": []}),
        ("POST", r"/orders$", {**created(), "order_configuration": order["order_configuration"]}),
        ("GET", r"/orders/historical/batch$", {"orders": [order], "has_next": False, "cursor": ""}),
        ("GET", r"/orders/historical/fills$", {"fills": [{
            "trade_id": "t1", "order_id": "ex-1", "product_id": "SOL-EUR", "side": "BUY", "price": "92.03",
            "size": "0.010", "commission": "0.006", "liquidity_indicator": "MAKER", "size_in_quote": False,
            "trade_time": "2026-10-17T12:00:01.123456789Z"}], "cursor": ""}),
        ("GET", r"/orders/historical/ex-1$", {"order": order}),
        ("GET", r"/accounts$", {"accounts": [
            {"uuid": "u1", "currency": "EUR", "available_balance": {"value": "100.50", "currency": "EUR"},
             "hold": {"value": "2.25", "currency": "EUR"}, "active": True},
            {"uuid": "u2", "currency": "SOL", "available_balance": {"value": "0.5", "currency": "SOL"},
             "hold": {"value": "0", "currency": "SOL"}, "active": True}], "has_next": False, "cursor": ""}),
        ("GET", r"/key_permissions$", {"can_view": True, "can_trade": False, "can_transfer": False,
                                        "portfolio_type": "DEFAULT"}),
        ("GET", r"/transaction_summary$", {"fee_tier": {"pricing_tier": "Advanced 1", "maker_fee_rate": "0.0025",
                                                        "taker_fee_rate": "0.0050"}, "total_volume": 12.0}),
        ("GET", r"/best_bid_ask$", {"pricebooks": [{"product_id": "SOL-EUR", "bids": [{"price": "92.03", "size": "1"}],
                                                    "asks": [{"price": "92.06", "size": "1"}]}]}),
        ("GET", r"/candles$", {"candles": [{"start": "1", "open": "1", "high": "2", "low": "0.5", "close": "1",
                                            "volume": "3"}]}),
        ("GET", r"/products/SOL-EUR$", RAW_SOL),
    ]  # fmt: skip

    def fake(
        self: Any, http_method: str, url_path: str, params: Any = None, data: Any = None, public: bool = False
    ) -> Any:
        body = {k: v for k, v in (data or {}).items() if v is not None} if http_method != "GET" else None
        sent.append((http_method, url_path, body))
        for verb, pattern, response in routes:
            if verb == http_method and re.search(pattern, url_path):
                return copy.deepcopy(response)  # the SDK pops fields out of the dict it is given
        raise AssertionError(f"unexpected request {http_method} {url_path}")

    monkeypatch.setattr(rest_base.RESTBase, "prepare_and_send_request", fake)
    return sent


def calls_of_every_public_method(ex: CoinbaseExchange) -> dict[str, Callable[[], Any]]:
    return {
        "get_product_raw": lambda: ex.get_product_raw("SOL-EUR"),
        "get_product": lambda: ex.get_product("SOL-EUR"),
        "get_fee_rates": lambda: ex.get_fee_rates(),
        "get_best_bid_ask": lambda: ex.get_best_bid_ask("SOL-EUR"),
        "create_order": lambda: ex.create_order(ticket_for()),
        "cancel_orders": lambda: ex.cancel_orders(["o1"]),
        "list_open_orders": lambda: ex.list_open_orders(),
        "list_orders": lambda: ex.list_orders(statuses=["OPEN"]),
        "get_order": lambda: ex.get_order("ex-1"),
        "get_fills": lambda: ex.get_fills("ex-1"),
        "get_accounts": lambda: ex.get_accounts(),
        "get_api_key_permissions": lambda: ex.get_api_key_permissions(),
        "preview_order": lambda: ex.preview_order(buy()),
        "get_candles": lambda: ex.get_candles("SOL-EUR", 0, 60),
        "get_public_candles": lambda: ex.get_public_candles("SOL-EUR", 0, 60),
    }


async def test_the_public_methods_that_post_or_delete_are_exactly_the_marked_ones(
    real_sdk: list[tuple[str, str, dict[str, Any] | None]],
) -> None:
    from coinbase.rest import RESTClient

    ex = CoinbaseExchange(RESTClient(api_key="k", api_secret="s"), read_only=False, clock=lambda: T0)
    table = calls_of_every_public_method(ex)
    assert set(table) == public_async_methods(CoinbaseExchange), "a new public method needs a row here"
    writers: set[str] = set()
    for name, call in table.items():
        real_sdk.clear()
        await call()
        verbs = {verb for verb, _, _ in real_sdk}
        assert verbs, f"{name} made no request"
        if verbs & {"POST", "PUT", "DELETE"}:
            writers.add(name)
        for _, path, body in real_sdk:
            assert (
                "/futures" not in path
                and "/perpetuals" not in path
                and "/portfolios" not in path
                and "convert" not in path
            )
            if name in ("create_order", "preview_order"):
                expected = TOP_LEVEL_KEYS - {"client_order_id"} if name == "preview_order" else TOP_LEVEL_KEYS
                assert body is not None and set(body) == expected  # no leverage, margin or attached orders
    assert writers == write_methods(CoinbaseExchange)


async def test_with_read_only_the_real_sdk_sees_no_post_at_all(
    real_sdk: list[tuple[str, str, dict[str, Any] | None]],
) -> None:
    from coinbase.rest import RESTClient

    ex = CoinbaseExchange(RESTClient(api_key="k", api_secret="s"), read_only=True, clock=lambda: T0)
    for name, call in calls_of_every_public_method(ex).items():
        if name in write_methods(CoinbaseExchange):
            with pytest.raises(DryRunViolation):
                await call()
        else:
            await call()
    assert {verb for verb, _, _ in real_sdk} == {"GET"}


async def test_real_sdk_responses_are_parsed(real_sdk: list[tuple[str, str, dict[str, Any] | None]]) -> None:
    from coinbase.rest import RESTClient

    ex = CoinbaseExchange(RESTClient(api_key="k", api_secret="s"), read_only=False, clock=lambda: T0)
    snap = await ex.get_accounts()
    assert snap.balances == {"EUR": (D("100.50"), D("2.25")), "SOL": (D("0.5"), D(0))} and snap.taken_at == T0
    (order,) = await ex.list_open_orders()
    assert (order.order_id, order.limit_price, order.base_size, order.leaves_size) == (
        "ex-1",
        D("92.03"),
        D("0.024"),
        D("0.014"),
    )
    assert not order.is_terminal
    (fill,) = await ex.get_fills("ex-1")
    assert (fill.price, fill.size, fill.fee, fill.liquidity) == (D("92.03"), D("0.010"), D("0.006"), "MAKER")
    fees = await ex.get_fee_rates()
    assert (fees.maker_bps, fees.taker_bps, fees.assumed) == (25.0, 50.0, False)
    quote = await ex.get_best_bid_ask("SOL-EUR")
    assert (quote.bid, quote.ask) == (D("92.03"), D("92.06"))
    meta = await ex.get_product("SOL-EUR")
    assert (meta.base_increment, meta.quote_increment) == (D("0.001"), D("0.01"))
    assert (await ex.get_api_key_permissions()).can_trade is False
    assert (await ex.get_candles("SOL-EUR", 0, 60))[0]["close"] == "1"


# --------------------------------------------------------------------------------- failures are typed


async def test_a_write_is_never_retried_after_an_ambiguous_failure() -> None:
    for failure in (
        requests.exceptions.ConnectionError("reset"),
        requests.exceptions.Timeout("slow"),
        http_error(500),
        http_error(503),
        http_error(408),
        RuntimeError("anything else"),
    ):
        ex, client = make_exchange()
        client.will("create_order", failure, created())  # a retry WOULD succeed: it must not happen
        with pytest.raises(OutcomeUnknown) as exc:
            await ex.create_order(ticket_for())
        assert exc.value.client_order_id == "b1" and exc.value.operation == "create_order"
        assert client.count("create_order") == 1


async def test_a_success_we_cannot_read_is_not_a_refusal() -> None:
    for answer in ({"success": True}, {"success": True, "success_response": {}}, {}, {"unexpected": 1}):
        ex, client = make_exchange()
        client.will("create_order", answer)
        with pytest.raises(OutcomeUnknown):
            await ex.create_order(ticket_for())
    ex, client = make_exchange()
    client.will("create_order", created("someone-else"))
    with pytest.raises(OutcomeUnknown, match="echoed"):
        await ex.create_order(ticket_for())


async def test_a_definitive_refusal_is_an_order_rejected() -> None:
    ex, client = make_exchange()
    client.will(
        "create_order",
        {"success": False, "failure_reason": "UNKNOWN_FAILURE_REASON",
         "error_response": {"error": "INSUFFICIENT_FUND", "message": "Insufficient balance in source account"}},
    )  # fmt: skip
    with pytest.raises(OrderRejected) as exc:
        await ex.create_order(ticket_for())
    assert (exc.value.reason, exc.value.message) == ("INSUFFICIENT_FUND", "Insufficient balance in source account")

    for status in (400, 404, 422, 429):
        ex, client = make_exchange()
        client.will(
            "create_order", http_error(status, {"error": "INVALID_LIMIT_PRICE_POST_ONLY", "message": "would cross"})
        )
        with pytest.raises(OrderRejected) as exc2:
            await ex.create_order(ticket_for())
        assert exc2.value.reason == "INVALID_LIMIT_PRICE_POST_ONLY" and client.count("create_order") == 1


async def test_credentials_problems_are_auth_errors_on_writes_and_reads() -> None:
    for status in (401, 403):
        ex, client = make_exchange()
        client.will("create_order", http_error(status))
        client.will("get_accounts", http_error(status))
        with pytest.raises(ExchangeAuthError):
            await ex.create_order(ticket_for())
        with pytest.raises(ExchangeAuthError):
            await ex.get_accounts()
        assert client.count("get_accounts") == 1  # not retried


async def test_cancel_orders_reports_each_order_and_is_never_retried() -> None:
    ex, client = make_exchange()
    client.will("cancel_orders", {"results": [
        {"success": True, "order_id": "o1"},
        {"success": False, "failure_reason": "UNKNOWN_CANCEL_ORDER", "order_id": "o2"}]})  # fmt: skip
    res = await ex.cancel_orders(["o1", "o2"])
    assert [(r.order_id, r.success, r.failure_reason) for r in res] == [
        ("o1", True, None), ("o2", False, "UNKNOWN_CANCEL_ORDER")]  # fmt: skip
    assert client.calls == [("cancel_orders", {"order_ids": ["o1", "o2"]})]

    ex, client = make_exchange()
    client.will("cancel_orders", http_error(502), {"results": []})
    with pytest.raises(OutcomeUnknown):
        await ex.cancel_orders(["o1"])
    assert client.count("cancel_orders") == 1

    for bad in ([], [""], ["a", "a"], [str(i) for i in range(101)]):
        with pytest.raises(ValueError):
            await ex.cancel_orders(bad)


async def test_a_read_is_retried_with_backoff_then_gives_up() -> None:
    ex, client = make_exchange(read_attempts=4)
    client.will("get_order", http_error(503), requests.exceptions.Timeout("t"), http_error(429), {"order": ORDER})
    assert (await ex.get_order("ex-1")).order_id == "ex-1" and client.count("get_order") == 4

    ex, client = make_exchange(read_attempts=3)
    client.will("get_order", http_error(500))
    with pytest.raises(ExchangeUnavailable):
        await ex.get_order("ex-1")
    assert client.count("get_order") == 3


async def test_a_definitive_read_error_is_not_retried() -> None:
    ex, client = make_exchange()
    client.will("get_order", http_error(404, {"error": "NOT_FOUND", "message": "no such order"}))
    with pytest.raises(ExchangeNotFound) as exc:
        await ex.get_order("nope")
    assert exc.value.status == 404 and exc.value.reason == "NOT_FOUND" and client.count("get_order") == 1
    client.will("get_order", http_error(400))
    with pytest.raises(ExchangeRequestError):
        await ex.get_order("bad")


@pytest.mark.parametrize(
    "answer",
    [
        {},
        {"order": "x"},
        {"order": {"order_id": "o"}},
        {"order": {"order_id": "o", "product_id": "P", "status": "OPEN", "filled_size": "abc"}},
    ],
)
async def test_malformed_answers_are_response_errors(answer: dict[str, Any]) -> None:
    ex, client = make_exchange()
    client.will("get_order", answer)
    with pytest.raises(ExchangeResponseError):
        await ex.get_order("o")


class CountingLimiter:
    def __init__(self) -> None:
        self.entered = 0

    async def __aenter__(self) -> None:
        self.entered += 1

    async def __aexit__(self, *exc: object) -> None:
        return None


async def test_every_request_waits_for_the_rate_limiter() -> None:
    ex, client = make_exchange()
    limiter = CountingLimiter()
    ex._limiter = limiter  # type: ignore[assignment]
    client.will("get_order", {"order": ORDER})
    client.will("create_order", created())
    client.will("cancel_orders", {"results": []})
    await ex.get_order("ex-1")
    await ex.create_order(ticket_for())
    await ex.cancel_orders(["o1"])
    assert limiter.entered == 3
    client.will("get_order", http_error(503), {"order": ORDER})
    await ex.get_order("ex-1")
    assert limiter.entered == 5  # a retry takes a token as well


# ---------------------------------------------------------------------------------------- reads, parsing

ORDER = {
    "order_id": "ex-1", "product_id": "SOL-EUR", "side": "BUY", "client_order_id": "b1", "status": "OPEN",
    "order_configuration": {"limit_limit_gtc": {"base_size": "0.024", "limit_price": "92.03", "post_only": True}},
    "filled_size": "0", "average_filled_price": "0", "total_fees": "0",
}  # fmt: skip


async def test_get_accounts_follows_pagination_and_adds_up_duplicate_currencies() -> None:
    def acc(cur: str, avail: str, hold: str) -> dict[str, Any]:
        return {"currency": cur, "available_balance": {"value": avail}, "hold": {"value": hold}}

    ex, client = make_exchange()
    pages = [
        {
            "accounts": [acc("EUR", "10.10", "1"), acc("SOL", "0.123456789012345678", "0")],
            "has_next": True,
            "cursor": "c1",
        },
        {"accounts": [acc("EUR", "5", "0.5")], "has_next": False, "cursor": ""},
    ]
    client.will("get_accounts", lambda **kw: pages[0] if kw.get("cursor") is None else pages[1])
    snap = await ex.get_accounts()
    assert snap.balances == {"EUR": (D("15.10"), D("1.5")), "SOL": (D("0.123456789012345678"), D(0))}
    assert [kw.get("cursor") for _, kw in client.calls] == [None, "c1"]


async def test_pagination_that_does_not_end_is_an_error() -> None:
    ex, client = make_exchange()
    client.will("get_accounts", {"accounts": [], "has_next": True, "cursor": "same"})
    with pytest.raises(ExchangeResponseError):
        await ex.get_accounts()
    client.will("get_accounts", {"accounts": [], "has_next": True})
    with pytest.raises(ExchangeResponseError):
        await ex.get_accounts()


def test_negative_or_garbage_balances_are_rejected() -> None:
    with pytest.raises(ExchangeResponseError):
        parse_balances([{"currency": "EUR", "available_balance": {"value": "-1"}}])
    with pytest.raises(ExchangeResponseError):
        parse_balances([{"currency": "EUR", "available_balance": {"value": "NaN"}}])
    with pytest.raises(ExchangeResponseError):
        parse_balances([{"currency": "EUR", "available_balance": {"value": "1"}, "hold": {"value": "oops"}}])


@given(units=st.integers(0, 10**18), scale=st.integers(0, 18), hold=st.integers(0, 10**12))
def test_balances_are_exact_decimals_never_floats(units: int, scale: int, hold: int) -> None:
    value = str(D(units).scaleb(-scale))
    out = parse_balances([{"currency": "X", "available_balance": {"value": value}, "hold": {"value": str(hold)}}])
    assert out == {"X": (D(value), D(hold))} and all(not isinstance(v, float) for v in out["X"])


async def test_get_fee_rates_converts_the_rate_to_bps_and_rejects_nonsense() -> None:
    ex, client = make_exchange()
    client.will("get_transaction_summary", {"fee_tier": {"maker_fee_rate": "0.0040", "taker_fee_rate": "0.0060"}})
    fees = await ex.get_fee_rates()
    assert (fees.maker_bps, fees.taker_bps) == (40.0, 60.0)
    client.will("get_transaction_summary", {"fee_tier": {"maker_fee_rate": "0.0080", "taker_fee_rate": "0.0060"}})
    with pytest.raises(ExchangeResponseError):
        await ex.get_fee_rates()
    client.will("get_transaction_summary", {})
    with pytest.raises(ExchangeResponseError):
        await ex.get_fee_rates()


async def test_get_best_bid_ask_rejects_a_crossed_or_empty_book() -> None:
    ex, client = make_exchange()
    for book in (
        {"bids": [{"price": "92"}], "asks": [{"price": "91"}]},
        {"bids": [], "asks": [{"price": "91"}]},
        {"bids": [{"price": "92"}], "asks": [{"price": "0"}]},
    ):
        client.will("get_best_bid_ask", {"pricebooks": [{"product_id": "SOL-EUR", **book}]})
        with pytest.raises(ExchangeResponseError):
            await ex.get_best_bid_ask("SOL-EUR")
    client.will("get_best_bid_ask", {"pricebooks": []})
    with pytest.raises(ExchangeResponseError):
        await ex.get_best_bid_ask("SOL-EUR")


async def test_get_product_only_accepts_a_verified_spot_product() -> None:
    ex, client = make_exchange()
    client.will("get_product", {**RAW_SOL, "product_type": "FUTURE"})
    with pytest.raises(SpotViolation):
        await ex.get_product("SOL-EUR")
    client.will("get_product", {**RAW_SOL, "future_product_details": {"venue": "cde"}})
    with pytest.raises(SpotViolation):
        await ex.get_product("SOL-EUR")


async def test_key_permissions_feed_the_live_start_check() -> None:
    ex, client = make_exchange()
    client.will(
        "get_api_key_permissions",
        {"can_view": True, "can_trade": True, "can_transfer": False, "portfolio_type": "DEFAULT"},
    )
    assert_live_key_permissions(await ex.get_api_key_permissions())
    client.will(
        "get_api_key_permissions",
        {"can_view": True, "can_trade": True, "can_transfer": True, "portfolio_type": "DEFAULT"},
    )
    with pytest.raises(SpotViolation):
        assert_live_key_permissions(await ex.get_api_key_permissions())
    client.will("get_api_key_permissions", {"can_view": True})
    with pytest.raises(ExchangeResponseError):
        await ex.get_api_key_permissions()


async def test_a_fill_in_quote_currency_is_refused_not_misread() -> None:
    ex, client = make_exchange()
    fill = {"trade_id": "t", "order_id": "o", "product_id": "SOL-EUR", "price": "1", "size": "1", "commission": "0",
            "trade_time": "2026-10-17T12:00:00Z", "size_in_quote": True}  # fmt: skip
    client.will("get_fills", {"fills": [fill], "cursor": ""})
    with pytest.raises(ExchangeResponseError):
        await ex.get_fills("o")


async def test_preview_sends_the_validated_body_and_reports_errors() -> None:
    ex, client = make_exchange()
    client.will("preview_order", {"order_total": "2.2", "commission_total": "0.01",
                                  "errs": [{"message": "PREVIEW_INSUFFICIENT_FUND"}], "warning": []})  # fmt: skip
    res = await ex.preview_order(buy())
    assert res.errors == ("PREVIEW_INSUFFICIENT_FUND",) and res.commission_total == D("0.01")
    # the real endpoint answers 400 "unknown field client_order_id" if it is sent (found by the first live preview)
    sent = {k: v for k, v in build_order_body(buy()).items() if k != "client_order_id"}
    assert client.calls == [("preview_order", sent)]
    assert "client_order_id" not in client.calls[0][1]
    with pytest.raises(SpotViolation):  # the body check applies to previews as well
        await ex.preview_order(sell(kind="market").model_copy(update={"side": "BUY"}))
    assert client.count("preview_order") == 1


def test_the_exchange_module_names_no_leveraged_or_fund_moving_sdk_call() -> None:
    text = EXCHANGE_PY.read_text(encoding="utf-8")
    sdk_attrs = set(re.findall(r"self\._client\.(\w+)", text))
    assert sdk_attrs == READ_SDK_METHODS | WRITE_SDK_METHODS
    assert not any(
        re.search(token, "|".join(sdk_attrs)) for token in ("future", "perp", "margin", "portfolio", "convert", "move")
    )
