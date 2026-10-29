"""AssetTagger (spec sez. 1.1 fase 3, sez. 1.3 punto 4)."""

from __future__ import annotations

import pytest

from modules.config import AssetAliases, SentimentConfig
from modules.sentiment.tagger import AssetTagger
from tests.sentiment_helpers import item

TAGGER = AssetTagger.from_config(SentimentConfig())


def tag(title: str, summary: str = "") -> tuple[tuple[str, ...], bool] | None:
    out = TAGGER.tag(item(title, summary=summary))
    return None if out is None else (out.assets, out.is_market_wide)


@pytest.mark.parametrize(
    "title", ["Solana outage", "SOL surges 10%", "Why SOL-EUR is moving", "$SOL rallies", "sOlAnA news"]
)
def test_sol_matches(title: str) -> None:
    assert tag(title) == (("SOL",), False)


@pytest.mark.parametrize(
    "title",
    [
        "Solar panels get cheaper",
        "SOLAR PANELS GET CHEAPER",  # upper case, but SOL is only a prefix of the word
        "sol price today",
        "Console makers report earnings",
        "Soldier returns home",
        "ETHICAL funds grow",
        "Whether traders stay together is unclear",  # "ether" inside other words
    ],
)
def test_sol_false_positives_are_not_tagged(title: str) -> None:
    assert tag(title) is None


def test_names_are_case_insensitive_and_tickers_are_not() -> None:
    assert tag("BITCOIN falls") == (("BTC",), False)
    assert tag("bitcoin falls") == (("BTC",), False)
    assert tag("BTC falls") == (("BTC",), False)
    assert tag("XBT falls") == (("BTC",), False)
    assert tag("btc falls") is None
    assert tag("Ether jumps") == (("ETH",), False)
    assert tag("eth jumps") is None


def test_multiple_assets_in_config_order() -> None:
    assert tag("SOL and ETH outperform Bitcoin") == (("BTC", "ETH", "SOL"), False)


def test_summary_is_searched_too() -> None:
    assert tag("Network upgrade announced", "The Solana Foundation said...") == (("SOL",), False)


def test_market_keyword_without_assets_is_market_wide() -> None:
    assert tag("SEC delays decision") == ((), True)
    assert tag("New ETF filing", "") == ((), True)
    assert tag("Fed holds rates") == ((), True)
    assert tag("Exchange hack drains hot wallet") == ((), True)


def test_asset_wins_over_macro_keyword() -> None:
    assert tag("SEC approves Bitcoin ETF") == (("BTC",), False)


def test_macro_keyword_needs_word_boundary() -> None:
    assert tag("Second quarter results") is None  # "sec" inside "second"
    assert tag("Fedex delivers") is None


def test_irrelevant_story_is_discarded() -> None:
    assert tag("Best pizza places in Brooklyn") is None


def test_source_declared_assets_are_kept_when_configured() -> None:
    out = TAGGER.tag(item("Lender files for bankruptcy", assets=("ETH", "XYZ")))
    assert out is not None and out.assets == ("ETH",)  # XYZ is not a configured asset


def test_relevance_title_summary_market() -> None:
    it = item("SOL rallies", summary="Bitcoin also moved")
    assert TAGGER.relevance(it, "SOL") == 1.0
    assert TAGGER.relevance(it, "BTC") == 0.6
    assert TAGGER.relevance(it, "MARKET") == 0.4


def test_custom_aliases_and_regex_escaping() -> None:
    t = AssetTagger({"X": AssetAliases(names=("c++ coin",), tickers=("A.B",))}, ["s&p"])
    assert t.tag(item("The C++ Coin lives")) is not None
    assert t.tag(item("ticker A.B moves")) is not None
    assert t.tag(item("ticker AxB moves")) is None  # the dot is literal
    assert t.tag(item("Weak s&p session")) is not None
