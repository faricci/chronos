"""Spot-only enforcement: product registry, order matrix, key permissions (C11, spec sez. 4.1.6)."""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any

import pytest

from modules.execution.spot import (
    SPOT_EXIT_CODE,
    SpotRegistry,
    SpotViolation,
    assert_live_key_permissions,
    build_order_body,
    validate_body,
)
from tests.coverage_fixtures import RAW_SOL, buy, sell


def raw(**changes: Any) -> dict[str, Any]:
    d = copy.deepcopy(RAW_SOL)
    d.update(changes)
    return d


def test_exit_code_is_4() -> None:
    assert SPOT_EXIT_CODE == 4


# --------------------------------------------------------------------------------------- registry


def test_valid_spot_product_is_registered() -> None:
    reg = SpotRegistry.from_raw({"SOL-EUR": RAW_SOL})
    meta = reg.meta("SOL-EUR")
    assert (meta.base_currency, meta.quote_currency) == ("SOL", "EUR")
    assert str(meta.base_increment) == "0.001" and str(meta.quote_min_size) == "1"
    assert reg.product_ids == {"SOL-EUR"}


@pytest.mark.parametrize(
    ("changes", "fragment"),
    [
        ({"product_type": "FUTURE"}, "not SPOT"),
        ({"product_type": None}, "not SPOT"),
        ({"future_product_details": {"venue": "cde", "contract_code": "SLP"}}, "future_product_details"),
        ({"product_venue": "INTX"}, "product_venue"),
        ({"product_id": "SOL-USD"}, "product_id"),
        ({"base_currency_id": "ETH"}, "does not match"),
        ({"trading_disabled": True}, "trading_disabled"),
        ({"cancel_only": True}, "cancel_only"),
        ({"view_only": True}, "view_only"),
        ({"base_increment": "0"}, "invalid increments"),
        ({"quote_increment": "abc"}, "quote_increment"),
        ({"base_min_size": None}, "base_min_size"),
    ],
)
def test_non_spot_or_unusable_product_is_a_violation(changes: dict[str, Any], fragment: str) -> None:
    with pytest.raises(SpotViolation, match=fragment):
        SpotRegistry.from_raw({"SOL-EUR": raw(**changes)})


def test_perpetual_style_ids_are_rejected_by_allowlist_not_by_name() -> None:
    perp = raw(product_id="SOL-PERP-INTX", base_currency_id="SOL", quote_currency_id="PERP-INTX")
    with pytest.raises(SpotViolation):
        SpotRegistry.from_raw({"SOL-PERP-INTX": perp})


def test_unknown_product_is_refused() -> None:
    with pytest.raises(SpotViolation, match="not in the verified spot registry"):
        SpotRegistry.from_raw({"SOL-EUR": RAW_SOL}).meta("BTC-EUR")


async def test_load_reads_every_product() -> None:
    class Reader:
        async def get_product_raw(self, product_id: str) -> dict[str, Any]:
            return RAW_SOL

    reg = await SpotRegistry.load(Reader(), ["SOL-EUR"])
    assert reg.meta("SOL-EUR").product_id == "SOL-EUR"


def test_missing_venue_field_is_tolerated_but_a_wrong_one_is_not() -> None:
    no_venue = {k: v for k, v in RAW_SOL.items() if k != "product_venue"}
    SpotRegistry.from_raw({"SOL-EUR": no_venue})
    with pytest.raises(SpotViolation):
        SpotRegistry.from_raw({"SOL-EUR": raw(product_venue="OTHER")})


# --------------------------------------------------------------------------------- key permissions


def perms(**kw: Any) -> Any:
    base = {"can_trade": True, "can_transfer": False, "portfolio_type": "DEFAULT"}
    return SimpleNamespace(**{**base, **kw})


def test_live_key_must_trade_and_must_not_transfer() -> None:
    assert_live_key_permissions(perms())
    for bad in (perms(can_trade=False), perms(can_transfer=True), perms(portfolio_type="INTX")):
        with pytest.raises(SpotViolation):
            assert_live_key_permissions(bad)


# ------------------------------------------------------------------------------------- order body


def test_allowed_bodies_snapshot() -> None:
    assert build_order_body(buy()) == {
        "client_order_id": "b1",
        "product_id": "SOL-EUR",
        "side": "BUY",
        "order_configuration": {"limit_limit_gtc": {"base_size": "0.024", "limit_price": "92.03", "post_only": True}},
    }
    assert build_order_body(sell("s1", kind="bracket", price="108.96", stop="83.57")) == {
        "client_order_id": "s1",
        "product_id": "SOL-EUR",
        "side": "SELL",
        "order_configuration": {
            "trigger_bracket_gtc": {"base_size": "0.024", "limit_price": "108.96", "stop_trigger_price": "83.57"}
        },
    }
    assert build_order_body(sell("s2", kind="market"))["order_configuration"] == {
        "market_market_ioc": {"base_size": "0.024"}
    }
    limit_sell = build_order_body(sell("s3", kind="limit", price="95.00"))
    assert limit_sell["order_configuration"]["limit_limit_gtc"]["post_only"] is False


def test_decimals_are_never_written_in_scientific_notation() -> None:
    body = build_order_body(buy(size="0.00000100", price="0.00001200"))
    cfg = body["order_configuration"]["limit_limit_gtc"]
    assert "e" not in cfg["base_size"].lower() and "e" not in cfg["limit_price"].lower()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.update(leverage="5"),
        lambda b: b.update(margin_type="CROSS"),
        lambda b: b.update(retail_portfolio_id="x"),
        lambda b: b.update(preview_id="x"),
        lambda b: b.update(attached_order_configuration={"trigger_bracket_gtc": {}}),
        lambda b: b.pop("client_order_id"),
        lambda b: b.update(side="SHORT"),
        lambda b: b["order_configuration"]["limit_limit_gtc"].update(leverage="3"),
        lambda b: b["order_configuration"]["limit_limit_gtc"].pop("post_only"),
        lambda b: b.update(order_configuration={"market_market_ioc": {"base_size": "1"}}),  # BUY market
        lambda b: b.update(order_configuration={"market_market_ioc": {"quote_size": "10"}}),
        lambda b: b.update(order_configuration={"limit_limit_gtc": {}, "market_market_ioc": {}}),
        lambda b: b.update(order_configuration={"stop_limit_stop_limit_gtc": {}}),
    ],
)
def test_any_key_or_combination_outside_the_matrix_is_a_violation(mutate: Any) -> None:
    body = build_order_body(buy())
    mutate(body)
    with pytest.raises(SpotViolation):
        validate_body(body)


def test_sell_limit_cannot_be_post_only() -> None:
    body = build_order_body(sell("s3", kind="limit", price="95.00"))
    body["order_configuration"]["limit_limit_gtc"]["post_only"] = True
    with pytest.raises(SpotViolation, match="post-only"):
        validate_body(body)


@pytest.mark.parametrize(
    "spec_factory",
    [
        lambda: sell("m", kind="market", price="1.00"),  # market with a price
        lambda: sell("b", kind="bracket", price="80.00", stop="90.00"),  # SL above TP
        lambda: sell("b", kind="bracket", price="100.00"),  # no stop
        lambda: sell("l", kind="limit"),  # limit without price
        lambda: buy(size="0"),
        lambda: buy(price="0"),
    ],
)
def test_malformed_specs_are_violations(spec_factory: Any) -> None:
    with pytest.raises(SpotViolation):
        build_order_body(spec_factory())


def test_buy_market_and_buy_bracket_are_impossible() -> None:
    from modules.execution.orders import OrderSpec
    from tests.coverage_fixtures import D

    for kind in ("market", "bracket"):
        spec = OrderSpec(
            client_order_id="x", book="champion", product_id="SOL-EUR", side="BUY", kind=kind,  # type: ignore[arg-type]
            base_size=D("1"), limit_price=D("1"), stop_trigger_price=D("0.5"),
        )
        with pytest.raises(SpotViolation, match="BUY"):
            build_order_body(spec)


# Fields as returned by the public Coinbase product endpoint on 2026-10-17 (script/probe_coinbase.py)
REAL_PRODUCTS = {
    pid: {
        "product_id": pid, "product_type": "SPOT", "product_venue": "CBE", "future_product_details": None,
        "base_currency_id": pid.split("-")[0], "quote_currency_id": "EUR", "trading_disabled": False,
        "cancel_only": False, "view_only": False, "base_increment": binc, "quote_increment": "0.01",
        "base_min_size": binc, "quote_min_size": "1",
    }
    for pid, binc in (("BTC-EUR", "0.00000001"), ("ETH-EUR", "0.00000001"), ("SOL-EUR", "0.001"))
}


def test_real_coinbase_products_pass_the_spot_registry() -> None:
    reg = SpotRegistry.from_raw(REAL_PRODUCTS)
    assert reg.product_ids == {"BTC-EUR", "ETH-EUR", "SOL-EUR"}
    assert str(reg.meta("BTC-EUR").base_increment) == "1E-8"  # Decimal keeps it exact; bodies never print it so


def test_btc_sized_body_has_no_scientific_notation() -> None:
    from modules.execution.orders import OrderSpec
    from tests.coverage_fixtures import D

    spec = OrderSpec(client_order_id="x", book="champion", product_id="BTC-EUR", side="BUY", kind="limit",
                     base_size=D("0.00000100"), limit_price=D("78123.45"), post_only=True)
    cfg = build_order_body(spec)["order_configuration"]["limit_limit_gtc"]
    assert cfg["base_size"] == "0.00000100" and cfg["limit_price"] == "78123.45"
