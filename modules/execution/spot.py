"""Spot-only enforcement (vincolo C11, spec sez. 4.1.6): no leverage, no margin, no futures, no short.

Everything here is an ALLOWLIST. Anything not explicitly known to be a plain spot order on a plain spot
product is a ``SpotViolation``, which is fatal (the process exits with code 4). None of it is configurable.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Protocol

from modules.execution.orders import OrderSpec, ProductMeta
from modules.execution.rounding import is_multiple_of

SPOT_EXIT_CODE: Final = 4

# Values the probe must confirm with a real key before the first live order (spec PARTE D, H5). Unknown
# values are refused rather than guessed: a wrong allowlist stops the bot, it never lets a bad product in.
DEFAULT_SPOT_VENUES: Final = frozenset({"CBE"})
DEFAULT_PORTFOLIO_TYPES: Final = frozenset({"DEFAULT", "CONSUMER"})

_PRODUCT_ID = re.compile(r"^[A-Z0-9]+-[A-Z]+$")

TOP_LEVEL_KEYS: Final = frozenset({"client_order_id", "product_id", "side", "order_configuration"})
_LIMIT_KEYS: Final = frozenset({"base_size", "limit_price", "post_only"})
_BRACKET_KEYS: Final = frozenset({"base_size", "limit_price", "stop_trigger_price"})
_MARKET_KEYS: Final = frozenset({"base_size"})
# side -> order_configuration kind -> exact set of inner keys. The only orders Chronos can ever send.
ORDER_MATRIX: Final[dict[str, dict[str, frozenset[str]]]] = {
    "BUY": {"limit_limit_gtc": _LIMIT_KEYS},
    "SELL": {
        "limit_limit_gtc": _LIMIT_KEYS,
        "trigger_bracket_gtc": _BRACKET_KEYS,
        "market_market_ioc": _MARKET_KEYS,
    },
}
_KIND_TO_CONFIG: Final = {"limit": "limit_limit_gtc", "bracket": "trigger_bracket_gtc", "market": "market_market_ioc"}


class SpotViolation(Exception):
    """The system was about to do something that is not a plain, fully covered spot trade. Fatal."""


# ----------------------------------------------------------------------------------- products


def _decimal(raw: Mapping[str, Any], key: str, product_id: str) -> Decimal:
    try:
        value = Decimal(str(raw[key]))
    except (KeyError, InvalidOperation) as e:
        raise SpotViolation(f"{product_id}: missing or invalid {key!r}") from e
    return value


def _meta_from_raw(product_id: str, raw: Mapping[str, Any], allowed_venues: frozenset[str]) -> ProductMeta:
    if raw.get("product_type") != "SPOT":
        raise SpotViolation(f"{product_id}: product_type {raw.get('product_type')!r} is not SPOT")
    if raw.get("future_product_details"):
        raise SpotViolation(f"{product_id}: has future_product_details, it is not a spot product")
    venue = raw.get("product_venue")
    if venue is not None and venue not in allowed_venues:
        raise SpotViolation(f"{product_id}: product_venue {venue!r} not in {sorted(allowed_venues)}")
    base, quote = raw.get("base_currency_id"), raw.get("quote_currency_id")
    if not base or not quote or f"{base}-{quote}" != product_id or not _PRODUCT_ID.match(product_id):
        raise SpotViolation(f"{product_id}: id does not match base/quote currencies ({base!r}/{quote!r})")
    if raw.get("product_id") not in (None, product_id):
        raise SpotViolation(f"{product_id}: exchange returned product_id {raw.get('product_id')!r}")
    for flag in ("trading_disabled", "cancel_only", "view_only"):
        if raw.get(flag):
            raise SpotViolation(f"{product_id}: {flag} is set")
    try:
        return ProductMeta(
            product_id=product_id,
            base_currency=str(base),
            quote_currency=str(quote),
            base_increment=_decimal(raw, "base_increment", product_id),
            quote_increment=_decimal(raw, "quote_increment", product_id),
            base_min_size=_decimal(raw, "base_min_size", product_id),
            quote_min_size=_decimal(raw, "quote_min_size", product_id),
        )
    except ValueError as e:  # pydantic: non-positive increments
        raise SpotViolation(f"{product_id}: invalid increments or sizes: {e}") from e


class ProductReader(Protocol):
    async def get_product_raw(self, product_id: str) -> Mapping[str, Any]: ...


class SpotRegistry:
    """The only products Chronos may trade, verified to be spot at startup."""

    def __init__(self, metas: Mapping[str, ProductMeta]) -> None:
        self._metas = dict(metas)

    @classmethod
    def from_raw(
        cls,
        raw_products: Mapping[str, Mapping[str, Any]],
        allowed_venues: frozenset[str] = DEFAULT_SPOT_VENUES,
    ) -> SpotRegistry:
        return cls({pid: _meta_from_raw(pid, raw, allowed_venues) for pid, raw in raw_products.items()})

    @classmethod
    async def load(
        cls,
        exchange: ProductReader,
        products: Iterable[str],
        allowed_venues: frozenset[str] = DEFAULT_SPOT_VENUES,
    ) -> SpotRegistry:
        raws = {pid: await exchange.get_product_raw(pid) for pid in products}
        return cls.from_raw(raws, allowed_venues)

    def meta(self, product_id: str) -> ProductMeta:
        try:
            return self._metas[product_id]
        except KeyError:
            raise SpotViolation(f"{product_id} is not in the verified spot registry") from None

    @property
    def product_ids(self) -> frozenset[str]:
        return frozenset(self._metas)


class KeyPermissions(Protocol):
    can_trade: bool
    can_transfer: bool
    portfolio_type: str


def assert_live_key_permissions(
    perms: KeyPermissions, allowed_portfolio_types: frozenset[str] = DEFAULT_PORTFOLIO_TYPES
) -> None:
    """Live needs a key that can trade but never transfer, on a plain portfolio. Else refuse to start."""
    if not perms.can_trade:
        raise SpotViolation("live key cannot trade")
    if perms.can_transfer:
        raise SpotViolation("live key has transfer permission: create a key without it")
    if perms.portfolio_type not in allowed_portfolio_types:
        raise SpotViolation(f"portfolio_type {perms.portfolio_type!r} not in {sorted(allowed_portfolio_types)}")


# -------------------------------------------------------------------------------- order shape


def validate_structure(spec: OrderSpec) -> None:
    """Shape of the order, independent of the product: side/kind matrix and required prices."""
    if spec.kind not in _KIND_TO_CONFIG:
        raise SpotViolation(f"unknown order kind {spec.kind!r}")
    if spec.side == "BUY" and spec.kind != "limit":
        raise SpotViolation(f"BUY {spec.kind} is not allowed: BUY must be a limit order")
    if spec.base_size <= 0:
        raise SpotViolation("base_size must be positive")
    if spec.kind == "market":
        if spec.limit_price is not None or spec.stop_trigger_price is not None or spec.post_only:
            raise SpotViolation("a market order carries only base_size")
        return
    if spec.limit_price is None or spec.limit_price <= 0:
        raise SpotViolation(f"{spec.kind} order needs a positive limit_price")
    if spec.kind == "limit":
        if spec.stop_trigger_price is not None:
            raise SpotViolation("a limit order has no stop_trigger_price")
        if spec.side == "SELL" and spec.post_only:
            raise SpotViolation("SELL limit orders are not post-only")
    else:  # bracket
        if spec.stop_trigger_price is None or not 0 < spec.stop_trigger_price < spec.limit_price:
            raise SpotViolation("a bracket needs 0 < stop_trigger_price < limit_price (SL below TP)")
        if spec.post_only:
            raise SpotViolation("a bracket is not post-only")


def validate_against_product(spec: OrderSpec, meta: ProductMeta) -> None:
    """Sizes and prices must sit exactly on the product's increments (rounding happens before, never here)."""
    if spec.product_id != meta.product_id:
        raise SpotViolation(f"order for {spec.product_id} checked against {meta.product_id}")
    if not is_multiple_of(spec.base_size, meta.base_increment):
        raise SpotViolation(f"base_size {spec.base_size} is not a multiple of {meta.base_increment}")
    for name, price in (("limit_price", spec.limit_price), ("stop_trigger_price", spec.stop_trigger_price)):
        if price is not None and not is_multiple_of(price, meta.quote_increment):
            raise SpotViolation(f"{name} {price} is not a multiple of {meta.quote_increment}")


def _plain(d: Decimal) -> str:
    return format(d, "f")


def build_order_body(spec: OrderSpec) -> dict[str, Any]:
    """The ONE place an order body is made. Decimal strings, only keys of the allowed matrix."""
    validate_structure(spec)
    if spec.kind == "limit":
        assert spec.limit_price is not None
        inner: dict[str, Any] = {
            "base_size": _plain(spec.base_size),
            "limit_price": _plain(spec.limit_price),
            "post_only": spec.post_only,
        }
    elif spec.kind == "bracket":
        assert spec.limit_price is not None and spec.stop_trigger_price is not None
        inner = {
            "base_size": _plain(spec.base_size),
            "limit_price": _plain(spec.limit_price),
            "stop_trigger_price": _plain(spec.stop_trigger_price),
        }
    else:
        inner = {"base_size": _plain(spec.base_size)}
    body: dict[str, Any] = {
        "client_order_id": spec.client_order_id,
        "product_id": spec.product_id,
        "side": spec.side,
        "order_configuration": {_KIND_TO_CONFIG[spec.kind]: inner},
    }
    validate_body(body)
    return body


def validate_body(body: Mapping[str, Any]) -> None:
    """Second line of defence, applied to any dict right before the network: only the allowed matrix passes.

    Rejects ``leverage``, ``margin_type``, ``attached_order_configuration``, ``quote_size``, ``preview_id``,
    BUY market, and any key not listed in :data:`ORDER_MATRIX`.
    """
    extra = set(body) - TOP_LEVEL_KEYS
    missing = TOP_LEVEL_KEYS - set(body)
    if extra or missing:
        raise SpotViolation(f"order body keys not allowed: extra={sorted(extra)} missing={sorted(missing)}")
    side = body["side"]
    if side not in ORDER_MATRIX:
        raise SpotViolation(f"side {side!r} not allowed")
    config = body["order_configuration"]
    if not isinstance(config, Mapping) or len(config) != 1:
        raise SpotViolation("order_configuration must hold exactly one order type")
    ((kind, inner),) = config.items()
    allowed = ORDER_MATRIX[side].get(kind)
    if allowed is None:
        raise SpotViolation(f"{side} {kind} is not an allowed order")
    if not isinstance(inner, Mapping) or set(inner) != allowed:
        raise SpotViolation(f"{kind} keys must be exactly {sorted(allowed)}, got {sorted(inner)}")
    if kind == "limit_limit_gtc" and side == "SELL" and inner["post_only"] is not False:
        raise SpotViolation("SELL limit orders are not post-only")
