"""``BookConfig.forced_buy_rate`` (test setting of ``random_entry``): a pair, random book only, off by default."""

from __future__ import annotations

import pytest

from modules.config import BookConfig


def test_the_forced_random_rate_is_a_pair_and_belongs_to_the_random_book_only() -> None:
    ok = BookConfig(
        name="r", decider="random", match_frequency_of="champion", seed=1, forced_buy_rate=0.5, forced_edge=0.9
    )
    assert (ok.forced_buy_rate, ok.forced_edge) == (0.5, 0.9)
    with pytest.raises(ValueError, match="go together"):
        BookConfig(name="r", decider="random", seed=1, forced_buy_rate=0.5)
    with pytest.raises(ValueError, match="random decider"):
        BookConfig(name="c", decider="registry:champion", forced_buy_rate=0.5, forced_edge=0.9)
    plain = BookConfig(name="c", decider="registry:champion")
    assert plain.forced_buy_rate is None and plain.forced_edge is None
