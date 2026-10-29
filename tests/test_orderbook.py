from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from modules.market.orderbook import LevelUpdate, LocalOrderBook, StaleBookError

D = Decimal
TS = datetime(2026, 10, 17, 18, 0, tzinfo=UTC)


def lv(side: str, price: str, qty: str) -> LevelUpdate:
    return LevelUpdate(side, D(price), D(qty))  # type: ignore[arg-type]


def book() -> LocalOrderBook:
    b = LocalOrderBook("SOL-EUR")
    b.apply_snapshot(
        [
            lv("bid", "92.00", "5"),
            lv("bid", "91.99", "10"),
            lv("bid", "91.90", "20"),
            lv("offer", "92.04", "3"),
            lv("offer", "92.05", "7"),
            lv("offer", "92.20", "50"),
        ],
        TS,
    )
    return b


def test_a_new_book_is_stale_until_its_snapshot() -> None:
    b = LocalOrderBook("SOL-EUR")
    assert b.stale and b.stale_reason == "no snapshot yet"
    assert b.best_bid() is None and b.best_ask() is None and b.mid() is None and b.spread_bps() is None
    with pytest.raises(StaleBookError):
        b.walk("BUY", D(1))
    assert b.apply_update([lv("bid", "1", "1")]) is False  # nothing to apply it to
    assert b.depth("bid") == 0


def test_best_prices_mid_and_spread() -> None:
    b = book()
    assert not b.stale
    assert (b.best_bid(), b.best_ask()) == (D("92.00"), D("92.04"))
    assert (b.best_bid_size(), b.best_ask_size()) == (D(5), D(3))
    assert b.mid() == D("92.02")
    assert b.spread_bps() == pytest.approx(0.04 / 92.02 * 1e4)
    assert (b.depth("bid"), b.depth("offer")) == (3, 3)


def test_update_replaces_adds_and_deletes_levels() -> None:
    b = book()
    assert b.apply_update([lv("bid", "92.00", "8"), lv("bid", "92.01", "1"), lv("offer", "92.04", "0")], TS)
    assert (b.best_bid(), b.best_bid_size()) == (D("92.01"), D(1))
    assert b.best_ask() == D("92.05")  # the 92.04 level was removed
    assert b.apply_update([lv("bid", "91.99", "0")], TS)
    assert b.depth("bid") == 3  # 92.01, 92.00, 91.90
    assert b.apply_update([lv("bid", "50", "0")], TS)  # deleting an unknown level is harmless


def test_a_new_snapshot_replaces_everything_and_clears_stale() -> None:
    b = book()
    b.mark_stale("sequence gap")
    assert b.stale
    b.apply_snapshot([lv("bid", "10", "1"), lv("offer", "11", "1")], TS)
    assert not b.stale and (b.best_bid(), b.best_ask()) == (D(10), D(11)) and b.depth("bid") == 1


def test_a_crossed_update_makes_the_book_stale() -> None:
    b = book()
    assert b.apply_update([lv("bid", "92.10", "1")]) is False
    assert b.stale and "crossed" in (b.stale_reason or "")
    with pytest.raises(StaleBookError, match="crossed"):
        b.walk("SELL", D(1))
    b.apply_snapshot([lv("bid", "92.00", "1"), lv("offer", "92.04", "1")])
    assert not b.stale


def test_equal_bid_and_ask_is_crossed() -> None:
    b = book()
    assert b.apply_update([lv("offer", "92.00", "1")]) is False


def test_a_crossed_snapshot_is_stale_from_the_start() -> None:
    b = LocalOrderBook("X")
    b.apply_snapshot([lv("bid", "5", "1"), lv("offer", "4", "1")])
    assert b.stale


# ---------------------------------------------------------------------------------------- walk


def test_walk_buy_consumes_asks_best_first() -> None:
    r = book().walk("BUY", D(5))
    # 3 @ 92.04 + 2 @ 92.05
    assert (r.filled, r.levels_consumed, r.complete) == (D(5), 2, True)
    assert r.notional == D("3") * D("92.04") + D("2") * D("92.05")
    assert r.vwap == r.notional / 5


def test_walk_sell_consumes_bids_best_first() -> None:
    r = book().walk("SELL", D(12))
    assert (r.filled, r.levels_consumed) == (D(12), 2)  # 5 @ 92.00 + 7 @ 91.99
    assert r.notional == D(5) * D("92.00") + D(7) * D("91.99")


def test_walk_respects_the_limit_price() -> None:
    b = book()
    buy = b.walk("BUY", D(100), limit=D("92.05"))  # only the first two ask levels are acceptable
    assert (buy.filled, buy.levels_consumed, buy.complete) == (D(10), 2, False)
    sell = b.walk("SELL", D(100), limit=D("91.99"))
    assert (sell.filled, sell.levels_consumed) == (D(15), 2)
    none = b.walk("BUY", D(1), limit=D("92.00"))  # limit below the best ask: nothing is marketable
    assert (none.filled, none.vwap, none.levels_consumed) == (D(0), None, 0)


def test_walk_beyond_the_book_is_incomplete() -> None:
    r = book().walk("BUY", D(1000))
    assert r.filled == D(60) and not r.complete and r.levels_consumed == 3


def test_walk_does_not_modify_the_book() -> None:
    b = book()
    before = (b.best_ask(), b.best_ask_size(), b.depth("offer"))
    b.walk("BUY", D(1000))
    assert (b.best_ask(), b.best_ask_size(), b.depth("offer")) == before


def test_walk_rejects_non_positive_size() -> None:
    with pytest.raises(ValueError):
        book().walk("BUY", D(0))


def test_walk_vwap_is_between_the_extremes() -> None:
    r = book().walk("BUY", D(30))
    assert D("92.04") <= r.vwap <= D("92.20")  # type: ignore[operator]


# ------------------------------------------------------------------------------------ property

PRICES = st.integers(9000, 9300)
QTY = st.integers(0, 5)


@given(
    st.lists(st.tuples(st.sampled_from(["bid", "offer"]), PRICES, QTY), max_size=80),
    st.lists(st.tuples(st.sampled_from(["bid", "offer"]), PRICES, QTY), max_size=80),
)
def test_book_matches_a_plain_dict_model(snapshot: list, updates: list) -> None:  # type: ignore[type-arg]
    b = LocalOrderBook("X")
    model: dict[str, dict[int, int]] = {"bid": {}, "offer": {}}

    def apply(items: list) -> None:  # type: ignore[type-arg]
        for side, price, qty in items:
            if qty:
                model[side][price] = qty
            else:
                model[side].pop(price, None)

    b.apply_snapshot([LevelUpdate(s, D(p) / 100, D(q)) for s, p, q in snapshot])
    apply(snapshot)
    bids, asks = model["bid"], model["offer"]
    snapshot_crossed = bool(bids and asks and max(bids) >= min(asks))
    assert b.stale == snapshot_crossed
    ok = b.apply_update([LevelUpdate(s, D(p) / 100, D(q)) for s, p, q in updates])
    apply(updates)
    assert b.depth("bid") == len(bids) and b.depth("offer") == len(asks)
    top_bid = D(max(bids)) / 100 if bids else None
    top_ask = D(min(asks)) / 100 if asks else None
    assert b.best_bid() == top_bid and b.best_ask() == top_ask
    crossed = top_bid is not None and top_ask is not None and top_bid >= top_ask
    assert ok == (not crossed)
    assert b.stale == (snapshot_crossed or crossed)  # stale stays until the next snapshot, even if later uncrossed
    if not b.stale and asks:
        r = b.walk("BUY", D(sum(asks.values())))
        assert r.complete and r.levels_consumed == len(asks)
