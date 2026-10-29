"""GTD entry (spec 2.15): the order matrix, ``entry_spec``, the gateway life cycle with EXPIRED, and the dust rule
at the stop.

Everything here is a default-off extension: a GTC entry, a bracket and a SELL are byte for byte what they were.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from modules.execution.gateway import entry_spec
from modules.execution.ids import client_order_id
from modules.execution.orders import OrderSpec
from modules.execution.reconciler import BracketRequest
from modules.execution.spot import SpotViolation, build_order_body, validate_body, validate_structure
from modules.risk.risk_manager import OrderIntent
from tests.coverage_fixtures import T0
from tests.gateway_fixtures import BOOK, PRODUCT, LiveEnv, make_live
from tests.test_risk_manager import plan

D = Decimal
ENTRY = client_order_id("dec-1", "entry")
BRACKET = client_order_id("dec-1", "bracket")
END = T0 + timedelta(seconds=1800)


def limit(**kw: object) -> OrderSpec:
    args: dict[str, object] = {
        "client_order_id": "c1", "book": BOOK, "product_id": PRODUCT, "side": "BUY", "kind": "limit",
        "base_size": D("0.023"), "limit_price": D("92.03"), "post_only": True,
    }  # fmt: skip
    return OrderSpec(**{**args, **kw})  # type: ignore[arg-type]


def gtd_intent(decision: str = "dec-1", size: str | None = None, *, flag: bool = True) -> OrderIntent:
    i = plan(decision_id=decision, intent_id=f"int-{decision}")
    assert isinstance(i, OrderIntent)
    entry = i.entry.model_copy(update={"expires_on_exchange": flag})
    update: dict[str, object] = {"entry": entry, "created_at": T0}
    if size is not None:
        update["base_size"] = D(size)
    return i.model_copy(update=update)


@pytest.fixture
async def make() -> AsyncIterator[Callable[..., Awaitable[LiveEnv]]]:
    envs: list[LiveEnv] = []

    async def factory(**kwargs: object) -> LiveEnv:
        env = await make_live(**kwargs)  # type: ignore[arg-type]
        envs.append(env)
        return env

    yield factory
    for env in envs:
        await env.ledger.close()


# ------------------------------------------------------------------------------------------- the matrix


def test_a_gtd_body_has_exactly_the_four_keys() -> None:
    body = build_order_body(limit(end_time=datetime(2026, 10, 27, 9, 30, 15, 987654, tzinfo=UTC)))
    assert body["order_configuration"] == {
        "limit_limit_gtd": {
            "base_size": "0.023",
            "limit_price": "92.03",
            "end_time": "2026-10-27T09:30:15Z",
            "post_only": True,
        }
    }
    assert set(body) == {"client_order_id", "product_id", "side", "order_configuration"}


def test_the_gtd_end_time_is_written_in_utc() -> None:
    from datetime import timezone

    rome = timezone(timedelta(hours=1))
    body = build_order_body(limit(end_time=datetime(2026, 10, 27, 10, 30, 0, tzinfo=rome)))
    assert body["order_configuration"]["limit_limit_gtd"]["end_time"] == "2026-10-27T09:30:00Z"


def test_gtc_and_bracket_bodies_are_unchanged_byte_for_byte() -> None:
    gtc = build_order_body(limit())
    assert gtc["order_configuration"] == {
        "limit_limit_gtc": {"base_size": "0.023", "limit_price": "92.03", "post_only": True}
    }
    bracket = build_order_body(
        OrderSpec(
            client_order_id="b1",
            book=BOOK,
            product_id=PRODUCT,
            side="SELL",
            kind="bracket",
            base_size=D("0.023"),
            limit_price=D("108.96"),
            stop_trigger_price=D("83.57"),
        )
    )
    assert bracket["order_configuration"] == {
        "trigger_bracket_gtc": {"base_size": "0.023", "limit_price": "108.96", "stop_trigger_price": "83.57"}
    }


@pytest.mark.parametrize("kind", ["limit", "bracket", "market"])
def test_a_gtd_sell_is_refused(kind: str) -> None:
    spec = OrderSpec(
        client_order_id="s1", book=BOOK, product_id=PRODUCT, side="SELL", kind=kind,  # type: ignore[arg-type]
        base_size=D("0.023"), end_time=END,
        limit_price=None if kind == "market" else D("108.96"),
        stop_trigger_price=D("83.57") if kind == "bracket" else None,
    )  # fmt: skip
    with pytest.raises(SpotViolation, match="end_time"):
        build_order_body(spec)


def test_a_gtd_sell_body_is_not_in_the_matrix() -> None:
    body = build_order_body(limit())
    sell = {**body, "side": "SELL"}
    sell["order_configuration"] = {
        "limit_limit_gtd": {
            "base_size": "1",
            "limit_price": "1",
            "end_time": "2026-10-27T09:30:15Z",
            "post_only": False,
        }
    }
    with pytest.raises(SpotViolation, match="not an allowed order"):
        validate_body(sell)


def test_a_naive_end_time_is_refused() -> None:
    with pytest.raises(SpotViolation, match="timezone-aware"):
        build_order_body(limit(end_time=datetime(2026, 10, 27, 9, 30)))


def test_an_end_time_in_the_past_is_refused_when_the_clock_is_given() -> None:
    spec = limit(end_time=END)
    validate_structure(spec, now=T0)  # in the future: fine
    with pytest.raises(SpotViolation, match="not in the future"):
        validate_structure(spec, now=END)
    with pytest.raises(SpotViolation, match="not in the future"):
        validate_structure(spec, now=END + timedelta(seconds=1))


@pytest.mark.parametrize("bad", ["2026-10-27 09:30:15", "2026-10-27T09:30:15", "2026-10-27T09:30:15+00:00", "x", 5])
def test_the_body_validator_checks_the_end_time_format(bad: object) -> None:
    body = build_order_body(limit(end_time=END))
    body["order_configuration"]["limit_limit_gtd"]["end_time"] = bad
    with pytest.raises(SpotViolation, match="end_time"):
        validate_body(body)


def test_a_gtd_body_with_an_extra_or_missing_key_is_refused() -> None:
    body = build_order_body(limit(end_time=END))
    inner = body["order_configuration"]["limit_limit_gtd"]
    del inner["post_only"]
    with pytest.raises(SpotViolation, match="keys must be exactly"):
        validate_body(body)
    body = build_order_body(limit(end_time=END))
    body["order_configuration"]["limit_limit_gtd"]["quote_size"] = "3"
    with pytest.raises(SpotViolation, match="keys must be exactly"):
        validate_body(body)


# ---------------------------------------------------------------------------------------------- entry_spec


def test_entry_spec_without_the_flag_is_what_it_always_was() -> None:
    i = plan(decision_id="dec-1", intent_id="int-1")
    assert isinstance(i, OrderIntent) and i.entry.expires_on_exchange is False
    spec = entry_spec(i)
    assert spec.end_time is None
    assert spec == OrderSpec(
        client_order_id=ENTRY, book=BOOK, product_id=PRODUCT, side="BUY", kind="limit", base_size=i.base_size,
        limit_price=i.entry.limit_price, post_only=True,
    )  # fmt: skip
    assert "limit_limit_gtc" in build_order_body(spec)["order_configuration"]


def test_entry_spec_with_the_flag_expires_at_creation_plus_the_timeout() -> None:
    spec = entry_spec(gtd_intent())
    assert spec.end_time == T0 + timedelta(seconds=1800)
    assert "limit_limit_gtd" in build_order_body(spec)["order_configuration"]


# ----------------------------------------------------------------------------------------------- gateway


async def test_a_gtd_entry_that_never_fills_ends_expired_without_a_cancel(
    make: Callable[..., Awaitable[LiveEnv]],
) -> None:
    env = await make()
    result = await env.gateway.submit_entry_with_bracket(gtd_intent())
    assert (result.status, result.reason) == ("no_fill", "expired")
    assert [e for e in env.venue.log if e[0] == "cancel"] == []  # the exchange ended it, the bot cancelled nothing
    assert env.venue.creates()[0].end_time == END
    assert env.balances.reservation(ENTRY) is None and env.balances.free("EUR") == D(100)
    assert await env.ledger.open_orders(BOOK) == []  # EXPIRED is terminal in the ledger
    assert (await env.ledger.get_order(ENTRY)).status == "EXPIRED"  # type: ignore[union-attr]
    assert env.clock() >= END  # it waited for the exchange, not less


async def test_a_gtd_entry_filled_in_full_gets_its_bracket(make: Callable[..., Awaitable[LiveEnv]]) -> None:
    env = await make()
    env.venue.plan_fills(ENTRY, (60, "0.023", "92.03"))
    result = await env.gateway.submit_entry_with_bracket(gtd_intent())
    assert (result.status, result.bracket, result.filled_size) == ("filled", "placed", D("0.023"))
    assert env.ledger.view(BOOK, PRODUCT).bracket_order_id == BRACKET


async def test_a_partial_fill_then_expired_still_gets_a_bracket_on_what_was_filled(
    make: Callable[..., Awaitable[LiveEnv]],
) -> None:
    env = await make()
    env.venue.plan_fills(ENTRY, (60, "0.012", "92.03"))
    result = await env.gateway.submit_entry_with_bracket(gtd_intent())
    assert (result.status, result.bracket, result.filled_size) == ("filled", "placed", D("0.012"))
    assert [e for e in env.venue.log if e[0] == "cancel"] == []
    assert env.venue.creates("SELL")[0].base_size == D("0.012")
    assert env.balances.reserved("EUR") == 0 and env.balances.reserved("SOL") == D("0.012")
    assert (await env.ledger.get_order(ENTRY)).status == "EXPIRED"  # type: ignore[union-attr]


async def test_if_the_exchange_does_not_expire_the_order_the_bot_cancels_as_the_second_line(
    make: Callable[..., Awaitable[LiveEnv]],
) -> None:
    env = await make(gtd_grace_s=30.0)
    env.venue.no_expire.add("ex-1")
    result = await env.gateway.submit_entry_with_bracket(gtd_intent())
    assert (result.status, result.reason) == ("no_fill", "cancelled")
    assert [e for e in env.venue.log if e[0] == "cancel"]  # the backstop
    assert (env.clock() - END).total_seconds() >= 30  # but only after end_time + grace


async def test_an_end_time_too_close_is_refused_without_any_effect(make: Callable[..., Awaitable[LiveEnv]]) -> None:
    env = await make()
    soon = gtd_intent().model_copy(update={"created_at": T0 - timedelta(seconds=1800 - 59)})  # ends in 59 s
    result = await env.gateway.submit_entry_with_bracket(soon)
    assert (result.status, result.reason) == ("refused", "end_time_too_close")
    assert env.venue.creates() == [] and env.balances.reserved("EUR") == 0
    assert await env.ledger.get_order(ENTRY) is None and not env.gateway.has_pending_entry(PRODUCT)
    ok = gtd_intent("dec-2").model_copy(update={"created_at": T0 - timedelta(seconds=1800 - 61)})
    assert (await env.gateway.submit_entry_with_bracket(ok)).status == "no_fill"  # 61 s of lead is enough


async def test_a_gtc_entry_is_untouched_by_the_gtd_code(make: Callable[..., Awaitable[LiveEnv]]) -> None:
    env = await make()
    result = await env.gateway.submit_entry_with_bracket(gtd_intent(flag=False))
    assert (result.status, result.reason) == ("no_fill", "cancelled")
    assert env.venue.creates()[0].end_time is None
    assert [e for e in env.venue.log if e[0] == "cancel"]


# ------------------------------------------------------------------------------- dust rule at the stop


async def test_a_fill_sellable_at_the_bid_but_not_at_the_stop_is_dust(make: Callable[..., Awaitable[LiveEnv]]) -> None:
    env = await make()
    env.venue.plan_fills(ENTRY, (60, "0.011", "92.03"))  # 0.011 * 92.03 = 1.012 at the bid, * 83.57 = 0.919 at the stop
    result = await env.gateway.submit_entry_with_bracket(gtd_intent())
    assert (result.status, result.bracket) == ("filled", "dust")
    assert env.venue.creates("SELL") == []
    pos = env.ledger.view(BOOK, PRODUCT)
    assert (pos.side, pos.dust_qty) == ("FLAT", D("0.011"))


async def test_a_fill_sellable_at_the_stop_gets_its_bracket(make: Callable[..., Awaitable[LiveEnv]]) -> None:
    env = await make()
    env.venue.plan_fills(ENTRY, (60, "0.012", "92.03"))  # 0.012 * 83.57 = 1.0028 >= 1
    result = await env.gateway.submit_entry_with_bracket(gtd_intent())
    assert (result.status, result.bracket) == ("filled", "placed")


async def test_the_dust_joins_the_next_entry_and_the_bracket_covers_the_sum(
    make: Callable[..., Awaitable[LiveEnv]],
) -> None:
    env = await make()
    env.venue.plan_fills(ENTRY, (60, "0.011", "92.03"))
    first = await env.gateway.submit_entry_with_bracket(gtd_intent())
    assert first.bracket == "dust"
    env.refresh_snapshot()
    second_cid = client_order_id("dec-2", "entry")
    env.venue.plan_fills(second_cid, (60, "0.012", "92.03"))
    again = gtd_intent("dec-2", size="0.012").model_copy(update={"created_at": env.clock()})  # a new intent, now
    second = await env.gateway.submit_entry_with_bracket(again)
    assert (second.status, second.bracket, second.dust_merged) == ("filled", "placed", True)
    assert env.venue.creates("SELL")[0].base_size == D("0.023")  # 0.011 of dust + 0.012 filled
    pos = env.ledger.view(BOOK, PRODUCT)
    assert (pos.side, pos.base_qty, pos.dust_qty) == ("LONG", D("0.023"), 0)
    assert env.balances.reserved("SOL") == D("0.023") and env.balances.free("SOL") == 0


async def test_a_missing_bracket_below_the_minimum_at_the_stop_turns_the_position_into_dust(
    make: Callable[..., Awaitable[LiveEnv]],
) -> None:
    env = await make(sol="0.011")
    await env.ledger.apply_entry_fill(BOOK, PRODUCT, D("0.011"), D("92.03"), "dec-9", T0, None)
    await env.gateway.place_missing_bracket(BracketRequest(BOOK, PRODUCT, D("0.011"), D("108.96"), D("83.57")))
    assert env.venue.creates() == []
    pos = env.ledger.view(BOOK, PRODUCT)
    assert (pos.side, pos.dust_qty) == ("FLAT", D("0.011"))


async def test_a_missing_bracket_sellable_at_the_stop_is_placed(make: Callable[..., Awaitable[LiveEnv]]) -> None:
    env = await make(sol="0.012")
    await env.ledger.apply_entry_fill(BOOK, PRODUCT, D("0.012"), D("92.03"), "dec-9", T0, None)
    await env.gateway.place_missing_bracket(BracketRequest(BOOK, PRODUCT, D("0.012"), D("108.96"), D("83.57")))
    assert [s.kind for s in env.venue.creates()] == ["bracket"]
    assert env.ledger.view(BOOK, PRODUCT).side == "LONG"
