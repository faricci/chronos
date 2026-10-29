"""The stage procedures (plan sez. 4) on the fake venue: preflight, preview, place-cancel, expire, reject."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import pytest

from modules.config import ExecProbeConfig, FallbackFees
from modules.execution.exchange import KeyPermissionsInfo, OrderRejected, PreviewResult
from modules.execution.raw_log import RawLog
from modules.probe import limits
from modules.probe.stage_actions import OrderStageHost, preflight, preview, run_order_stage
from modules.probe.stages import config_hash
from tests.probe_fakes import P, Venue
from tests.probe_rig import Rig, make_rig

D = Decimal
CFG = ExecProbeConfig(enabled=True)


@pytest.fixture
async def rig() -> AsyncIterator[Rig]:
    r = await make_rig()
    yield r
    await r.close()


async def run_preflight(rig: Rig) -> tuple[bool, dict, list[str]]:  # type: ignore[type-arg]
    record, lines = await preflight(CFG, rig.venue, fingerprint="abcd1234", clock=rig.clock, sleep=rig.clock.sleep)  # type: ignore[arg-type]
    return record.ok, record.details, lines


# ------------------------------------------------------------------------------------------- preflight


async def test_preflight_passes_on_a_clean_account(rig: Rig) -> None:
    ok, details, lines = await run_preflight(rig)
    assert ok, details["checks"]
    assert details["permissions"] == {
        "can_view": True, "can_trade": True, "can_transfer": False, "portfolio_type_allowed": True,
    }  # fmt: skip
    assert details["key_fingerprint"] == "abcd1234" and details["quote_available"] == "5.00"
    assert details["balance_feed"] == {"accepted": 3, "max_age_s": 0.0, "alarms": 0}
    assert details["fees"] == {"maker_bps": 25.0, "taker_bps": 50.0, "assumed": False}
    assert details["open_orders_on_product"] == 0 and details["market"]["atr1h_bps"] == pytest.approx(12.5, abs=0.1)
    assert any("Permessi" in line for line in lines)


async def test_preflight_prints_codes_of_the_currencies_but_never_their_amounts(rig: Rig) -> None:
    rig.venue.extra_balances = {"MANA": (D("123.456789"), D(0))}
    ok, details, lines = await run_preflight(rig)
    assert "MANA" in details["currencies_with_balance"]
    assert "123.456789" not in json.dumps(details) and not any("123.456789" in line for line in lines)


@pytest.mark.parametrize(
    ("change", "failed"),
    [
        (lambda v: setattr(v, "perms", KeyPermissionsInfo(True, True, True, "DEFAULT")), "cannot_transfer"),
        (lambda v: setattr(v, "perms", KeyPermissionsInfo(True, False, False, "DEFAULT")), "can_trade"),
        (lambda v: setattr(v, "perms", KeyPermissionsInfo(False, True, False, "DEFAULT")), "can_view"),
        (lambda v: setattr(v, "perms", KeyPermissionsInfo(True, True, False, "INTX")), "portfolio_type_allowed"),
        (lambda v: setattr(v, "eur_total", D("2.50")), "quote_covers_q_max"),
        (lambda v: setattr(v, "maker_bps", 20.0), "fees_match_annex"),
        (lambda v: v.raw_product.update(quote_min_size="2"), "product_matches_annex"),
        (lambda v: v.raw_product.update(product_type="FUTURE"), "product_is_spot"),
        (lambda v: setattr(v, "candles", []), "atr_available"),
    ],
)
async def test_preflight_fails_on_each_wrong_fact(rig: Rig, change, failed: str) -> None:  # type: ignore[no-untyped-def]
    change(rig.venue)
    ok, details, _ = await run_preflight(rig)
    assert not ok and details["checks"][failed] is False


async def test_preflight_stops_when_the_values_differ_from_the_annex(rig: Rig) -> None:
    rig.venue.taker_bps = 60.0
    ok, details, _ = await run_preflight(rig)
    assert not ok and "needs_quant_review" in details


async def test_preflight_refuses_an_order_already_open_on_the_product(rig: Rig) -> None:
    from modules.execution.gateway import entry_spec
    from modules.execution.orders import OrderSpec
    from modules.probe.stages import far_buy_intent

    intent = far_buy_intent("x", rig.venue.bid, rig.registry.meta(P), CFG, rig.clock(), tif_s=300)
    ticket = rig.guard.authorize(entry_spec(intent))
    await rig.venue.create_order(ticket)
    assert isinstance(entry_spec(intent), OrderSpec)
    ok, details, _ = await run_preflight(rig)
    assert not ok and details["open_orders_on_product"] == 1


async def test_preflight_sends_nothing_and_a_read_only_venue_is_enough() -> None:
    r = await make_rig()
    try:
        r.venue.read_only = True
        ok, _, _ = await run_preflight(r)
        assert (
            ok and r.venue.creates() == [] and not [e for e in r.venue.log if e[0] in ("create", "cancel", "preview")]
        )
    finally:
        await r.close()


async def test_a_balance_feed_alarm_fails_the_preflight(rig: Rig) -> None:
    rig.venue.extra_balances = {}
    orig = rig.venue.get_accounts
    calls = {"n": 0}

    async def drifting():  # type: ignore[no-untyped-def]
        calls["n"] += 1
        snap = await orig()
        if calls["n"] >= 3:  # the third read: the account suddenly holds something nobody ordered
            b = dict(snap.balances)
            b["EUR"] = (b["EUR"][0], D("1.00"))
            return snap.model_copy(update={"balances": b})
        return snap

    rig.venue.get_accounts = drifting  # type: ignore[method-assign]
    ok, details, _ = await run_preflight(rig)
    assert not ok and details["balance_feed"]["alarms"] >= 1


# --------------------------------------------------------------------------------------------- preview


async def run_preview(venue: Venue, rig: Rig) -> tuple:  # type: ignore[type-arg]
    return await preview(CFG, venue, fallback=FallbackFees(), clock=rig.clock)  # type: ignore[arg-type]


async def test_preview_accepts_gtd_and_decides_the_gtd_mode(rig: Rig) -> None:
    record, _ = await run_preview(rig.venue, rig)
    assert record.ok and record.entry_mode == "gtd"
    assert record.details["gtd"]["accepted"] and record.details["gtc"]["accepted"]
    assert record.details["commission_precision_ok"] and record.details["gtd"]["commission_decimals"] >= 4
    kinds = [e[1].end_time is not None for e in rig.venue.log if e[0] == "preview"]
    assert kinds == [True, False, False]  # GTD entry, GTC entry, bracket
    assert rig.venue.creates() == []  # a preview creates nothing
    intent = record.details["intent"]
    assert intent["limit_price"] == "80000.00" and D(intent["base_size"]) * D(intent["limit_price"]) <= D("3.00")


async def test_a_gtd_refused_by_the_exchange_means_the_gtc_fallback_the_user_did_not_accept(rig: Rig) -> None:
    rig.venue.preview["gtd"] = OrderRejected("INVALID_END_TIME", "post_only with GTD is not supported")
    record, _ = await run_preview(rig.venue, rig)
    assert record.ok and record.entry_mode == "gtc_fallback"
    assert not record.details["gtd"]["accepted"] and "INVALID_END_TIME" in record.details["gtd"]["rejected"]
    assert "fallback_needs_user_decision" in record.details


async def test_both_refused_means_no_entry_mode(rig: Rig) -> None:
    rig.venue.preview["gtd"] = OrderRejected("X", "no")
    rig.venue.preview["gtc"] = PreviewResult(errors=("INSUFFICIENT_FUND",), warnings=())
    record, _ = await run_preview(rig.venue, rig)
    assert not record.ok and record.entry_mode is None


async def test_a_commission_rounded_to_the_cent_stops_the_probe(rig: Rig) -> None:
    rig.venue.preview["gtd"] = PreviewResult(errors=(), warnings=(), order_total=D("2.96"), commission_total=D("0.01"))
    record, lines = await run_preview(rig.venue, rig)
    assert not record.ok and record.details["commission_precision_ok"] is False
    assert "stop" in record.details and "Q_max" in record.details["stop"]
    assert any("centesimo" in line for line in lines)


async def test_preview_without_a_first_entry_to_show_is_not_ok_and_calls_nothing(rig: Rig) -> None:
    rig.venue.set_market("80000.00", "80100.00")  # 12.5 bps: the planner would skip
    record, _ = await run_preview(rig.venue, rig)
    assert not record.ok and record.details["skip"] == "spread_wide"
    assert not [e for e in rig.venue.log if e[0] == "preview"]


# ------------------------------------------------------------------------------------------ order stages


class Recording:
    """The user channel, as the fake venue would publish it: every order change is a message in a real RawLog."""

    def __init__(self, tmp_path: Path, rig: Rig) -> None:
        self.log = RawLog(tmp_path / "out", clock=lambda: rig.clock().timestamp())
        self.rig = rig
        rig.venue.listeners.append(self._on_order)

    def _on_order(self, order) -> None:  # type: ignore[no-untyped-def]
        raw = json.dumps({"channel": "user", "events": [{"type": "update", "orders": [self.rig.venue.message(order)]}]})
        self.log.write({"t": self.rig.clock().timestamp(), "raw": raw})


def host_for(rig: Rig, rec: Recording, entry_mode: str = "gtd") -> OrderStageHost:
    return OrderStageHost(
        cfg=CFG, meta=rig.registry.meta(P), gateway=rig.gateway, ledger=rig.ledger, balances=rig.balances,
        exchange=rig.venue, clock=rig.clock, sleep=rig.clock.sleep, raw_path=rec.log.path, flush_raw=rec.log.flush,
        config_sha256=config_hash(CFG), entry_mode=entry_mode, poll_s=5.0,  # type: ignore[arg-type]
    )  # fmt: skip


async def test_place_cancel_stage(rig: Rig, tmp_path: Path) -> None:
    rec = Recording(tmp_path, rig)
    record, lines = await run_order_stage("place-cancel", host_for(rig, rec))
    assert record.ok, record.details["checks"]
    order = rig.venue.creates()[0]
    assert order.end_time is not None and order.post_only and order.limit_price == D("76000.00")
    assert D("2.00") <= order.base_size * order.limit_price < D("2.01")
    assert [e for e in rig.venue.log if e[0] == "cancel"]  # the bot cancelled it, after about 60 s
    assert record.details["ledger_status"] == record.details["rest_status"] == "CANCELLED"
    assert record.details["quote_free_after"] == record.details["quote_free_before"]
    msgs = record.details["user_messages"]
    assert msgs["OPEN"]["seen"] and msgs["CANCELLED"]["seen"] and msgs["OPEN"]["differences"] == []
    assert any("OK" in line for line in lines)


async def test_place_cancel_fails_if_the_channel_says_something_else(rig: Rig, tmp_path: Path) -> None:
    rec = Recording(tmp_path, rig)
    original = rig.venue.message

    def wrong(order):  # type: ignore[no-untyped-def]
        msg = original(order)
        if order.status == "CANCELLED":
            msg["cumulative_quantity"] = "0.5"  # the channel claims a fill that REST does not have
        return msg

    rig.venue.message = wrong  # type: ignore[method-assign]
    record, _ = await run_order_stage("place-cancel", host_for(rig, rec))
    assert not record.ok and record.details["checks"]["user_messages_agree"] is False
    assert any("cumulative_quantity" in d for d in record.details["user_messages"]["CANCELLED"]["differences"])


async def test_place_cancel_fails_without_channel_messages(rig: Rig, tmp_path: Path) -> None:
    rec = Recording(tmp_path, rig)
    rig.venue.listeners.clear()  # the stream delivers nothing
    record, _ = await run_order_stage("place-cancel", host_for(rig, rec))
    assert not record.ok and record.details["checks"]["user_messages_agree"] is False


async def test_expire_stage_ends_by_the_exchange_without_a_cancel(rig: Rig, tmp_path: Path) -> None:
    rec = Recording(tmp_path, rig)
    record, _ = await run_order_stage("expire", host_for(rig, rec))
    assert record.ok, record.details["checks"]
    assert [e for e in rig.venue.log if e[0] == "cancel"] == []
    assert record.details["ledger_status"] == record.details["rest_status"] == "EXPIRED"
    assert record.details["user_messages"]["EXPIRED"]["seen"]
    assert rig.venue.creates()[0].end_time is not None


async def test_expire_stage_fails_if_the_exchange_does_not_expire_the_order(rig: Rig, tmp_path: Path) -> None:
    rec = Recording(tmp_path, rig)
    rig.venue.no_expire = {"ex-1"}
    record, _ = await run_order_stage("expire", host_for(rig, rec))
    assert (
        not record.ok and record.details["checks"]["expired_by_the_exchange"] is False
    )  # only the bot's cancel ended it


async def test_reject_stage(rig: Rig, tmp_path: Path) -> None:
    rec = Recording(tmp_path, rig)
    rig.venue.errors = [OrderRejected("INVALID_LIMIT_PRICE_POST_ONLY", "would take liquidity")]
    record, _ = await run_order_stage("reject", host_for(rig, rec))
    assert record.ok, record.details["checks"]
    assert record.details["result"]["status"] == "rejected" and rig.venue.orders == {}
    assert rig.venue.creates()[0].limit_price == D("80001.00")  # the ask, rounded up
    assert rig.balances.reservation(rig.venue.creates()[0].client_order_id) is None


async def test_reject_stage_flags_a_post_only_that_was_executed(rig: Rig, tmp_path: Path) -> None:
    rec = Recording(tmp_path, rig)
    from modules.probe.stages import crossing_buy_intent

    intent = crossing_buy_intent("reject", rig.venue.ask, rig.registry.meta(P), CFG, rig.clock(), tif_s=300)
    from modules.execution.ids import client_order_id

    rig.venue.plan_fills(
        client_order_id(intent.decision_id, "entry"), (1, str(intent.base_size), str(intent.entry.limit_price))
    )
    record, _ = await run_order_stage("reject", host_for(rig, rec))
    assert not record.ok and "conflitto" in record.details
    assert record.details["result"]["status"] == "filled"  # and it went on as a normal entry, with its bracket


def test_the_stage_orders_are_two_euros_and_never_an_entry_of_the_probe() -> None:
    assert limits.TEST_ORDER_QUOTE == D("2.00") and limits.TEST_ORDER_QUOTE > limits.n_br(D(1), limits.SL_FRAC)
