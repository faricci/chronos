"""Tests of the news event study (training/news_event_study.py) with synthetic data only: no network, no model."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import numpy as np
import pandas as pd
import pytest

from training import news_event_study as nes

DAY = 86_400


# ------------------------------------------------------------------------------------------------ tagger


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Russia legalizes Bitcoin and cryptocurrency mining", "regolazione_positiva"),
        ("Brazil's Congress Passes Bill Legalizing Bitcoin, Ether Payments", "regolazione_positiva"),
        ("SEC approves 11 Bitcoin spot ETFs to begin trading after 10-year wait", "regolazione_positiva"),
        ("El Salvador makes Bitcoin legal tender", "regolazione_positiva"),
        ("Circle Gets MiCA Stablecoin Issuer License", "regolazione_positiva"),
        ("Pennsylvania passes bill allowing Bitcoin payments, crypto self-custody", "regolazione_positiva"),
        ("Laos lifts crypto ban", "regolazione_positiva"),
        ("China bans Bitcoin mining", "regolazione_negativa"),
        ("SEC sues Binance over unregistered crypto exchange", "regolazione_negativa"),
        ("SEC rejects spot Bitcoin ETF application", "regolazione_negativa"),
        ("Venezuela Bans Crypto Mining As It Grapples With Power Crisis", "regolazione_negativa"),
        ("BlackRock files for Bitcoin ETF", "adozione_istituzionale"),
        ("MicroStrategy buys 122 BTC in April", "adozione_istituzionale"),
        ("PayPal launches crypto service", "adozione_istituzionale"),
        ("DeFi protocol Cream Finance hacked for more than $130 million", "hack_exploit"),
        ("Hackers steal $115 Million from BadgerDAO users", "hack_exploit"),
        ("Bitcoin price rises 3% on Tuesday", "altro"),
        ("", "altro"),
    ],
)
def test_tag_title_examples(title: str, expected: str) -> None:
    assert nes.tag_title(title) == expected


@pytest.mark.parametrize(
    "title",
    [
        "Will Russia legalize Bitcoin?",  # question
        "Fake tweet says SEC approves Bitcoin ETF",  # fake news
        "Arizona Senator wants to make Bitcoin a legal tender",  # wish, not an event
        "SEC may sue Binance",  # hedge
        "India might ban Bitcoin and Ether",  # hedge
        "SEC is likely to reject Ethereum ETF applications",  # expectation
        "Hackers could steal $1 million from wallets",  # hedge
        "Norwegian Authorities Recover $5.7M from $600M Ronin Hack",  # aftermath of a hack
        "Ether drained by $91M as ETF outflows hit record",  # flows, not a hack
        "European Parliament votes against PoW ban",  # ban failed
        "Vanguard to end support for Grayscale Bitcoin products",  # negative adoption
        "SEC closes its ETH probe without charges against ethereum",  # negation of the enforcement
    ],
)
def test_tag_title_rejects_hedged_and_negated_titles(title: str) -> None:
    assert nes.tag_title(title) == "altro"


def test_tag_title_precedence_and_determinism() -> None:
    # a hack wins over a regulator word in the same title; the tagger has no state
    t = "China bans Bitcoin trading as hackers steal $5 million from an exchange"
    assert nes.tag_title(t) == "hack_exploit"
    assert nes.tag_title(t) == nes.tag_title(t)
    assert len(nes.TAGGER_VERSION) == 12


def test_tag_price_move_validation_tag() -> None:
    assert nes.tag_price_move("Bitcoin plunges below $20,000 as FTX contagion spreads") == "move_down"
    assert nes.tag_price_move("Ether surges 8% after the Merge") == "move_up"
    assert nes.tag_price_move("Bitcoin could crash again?") == ""  # hedge
    assert nes.tag_price_move("Stocks surge and bonds fall") == ""  # not crypto
    assert nes.tag_price_move("Bitcoin surges while altcoins plunge") == ""  # both directions


def test_normalize_text_handles_possessive_and_mojibake() -> None:
    assert nes.normalize_text("SEC�s  'Bitcoin' ETF -- Approved!") == "sec bitcoin etf approved"
    assert nes.normalize_text("Don't") == "dont"


# ------------------------------------------------------------------------------------------------ dedup / clusters


def _news(rows: list[tuple[str, str]]) -> pd.DataFrame:
    return pd.DataFrame({"ts": pd.to_datetime([r[0] for r in rows], utc=True), "title": [r[1] for r in rows]})


def test_dedupe_keeps_earliest_copy_and_counts_copies() -> None:
    df = _news(
        [
            ("2024-01-02 10:00", "SEC Approves Bitcoin ETF!"),
            ("2024-01-02 09:00", "sec approves bitcoin etf"),
            ("2024-01-02 11:00", "Another story"),
        ]
    )
    out = nes.dedupe_titles(df)
    assert len(out) == 2
    first = out[out["key"] == "sec approves bitcoin etf"].iloc[0]
    assert first["ts"] == pd.Timestamp("2024-01-02 09:00", tz="UTC")
    assert first["n_copies"] == 2


def test_cluster_events_one_per_class_and_day() -> None:
    df = _news(
        [
            ("2024-01-02 09:00", "a"),
            ("2024-01-02 15:00", "b"),
            ("2024-01-02 16:00", "c"),
            ("2024-01-03 01:00", "d"),
        ]
    ).assign(cls=["x", "x", "y", "x"])
    ev = nes.cluster_events(df)
    assert len(ev) == 3  # (x, 2 Jan), (y, 2 Jan), (x, 3 Jan)
    x2 = ev[(ev["cls"] == "x") & (ev["ts"].dt.day == 2)].iloc[0]
    assert x2["title"] == "a"  # the earliest of the day
    assert x2["n_titles"] == 2


def test_independent_spacing_is_greedy() -> None:
    ts = np.array([0, 1 * DAY, 2 * DAY, 3 * DAY, 3 * DAY + 5, 7 * DAY], dtype=np.int64)
    assert nes.independent_spacing(ts, 3 * DAY) == 3  # 0, 3d, 7d
    assert nes.independent_spacing(np.empty(0, dtype=np.int64), DAY) == 0


def test_load_coindesk_reads_the_stamps_as_iran_local_time(tmp_path: Path) -> None:
    csv = tmp_path / "cd.csv"
    pd.DataFrame(
        {
            "published_on": ["2021-07-01 12:00:00.000000", "2021-12-01 12:00:00.000000", "2023-07-01 12:00:00.000000"],
            "title": ["a", "b", "c"],
            "source": ["x", "x", "x"],
        }
    ).to_csv(csv, index=False)
    out = nes.load_coindesk(csv)
    assert [t.strftime("%Y-%m-%d %H:%M") for t in out["ts"]] == [
        "2021-07-01 07:30",  # summer 2021: UTC+4:30
        "2021-12-01 08:30",  # winter: UTC+3:30
        "2023-07-01 08:30",  # no DST from 2023
    ]
    assert str(out["ts"].dt.tz) == "UTC"


# ------------------------------------------------------------------------------------------------ prices / windows


def _candles(n: int, *, drop: tuple[int, ...] = ()) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="5min", tz="UTC")
    op = 100.0 + np.arange(n, dtype=np.float64)
    df = pd.DataFrame({"open": op}, index=idx)
    return df.drop(df.index[list(drop)]) if drop else df


def test_price_grid_marks_missing_bars_nan() -> None:
    g = nes.PriceGrid(_candles(10, drop=(4,)))
    t0 = g.start_s
    got = g.open_at(np.array([t0, t0 + 300 * 4, t0 + 300 * 5, t0 + 300 * 99, t0 - 300, t0 + 7], dtype=np.int64))
    assert got[0] == 100.0
    assert np.isnan(got[1])  # dropped bar
    assert got[2] == 105.0
    assert np.isnan(got[3]) and np.isnan(got[4])  # outside the grid
    assert np.isnan(got[5])  # not a bar start


def test_window_returns_values_and_entry_delay() -> None:
    g = nes.PriceGrid(_candles(2000))
    t0 = g.start_s
    pub = np.array([t0 + 300 * 1000 + 120], dtype=np.int64)  # 2 min after the bar #1000 opened
    r = nes.window_returns(g, pub)
    a, e = 1000, 1002  # a = bar at/before publication; E = first bar >= pub + 5 min -> bar 1002 (pub + 5 min = 1001.4)
    assert r["react"][0] == pytest.approx((100 + e) / (100 + a) * 1e4 - 1e4)
    assert r["pre_1h"][0] == pytest.approx((100 + a) / (100 + a - 12) * 1e4 - 1e4)
    assert r["post_1h"][0] == pytest.approx((100 + e + 12) / (100 + e) * 1e4 - 1e4)
    late = nes.window_returns(g, np.array([t0 + 300 * 1500], dtype=np.int64), windows=["post_3d"])
    assert np.isnan(late["post_3d"][0])  # beyond the end of the series


def test_pre_event_window_never_sees_the_future() -> None:
    base = _candles(3000)
    changed = base.copy()
    changed.iloc[1001:, 0] *= 3.0  # everything after the publication bar is different
    pub = np.array([nes.PriceGrid(base).start_s + 300 * 1000 + 30], dtype=np.int64)
    a = nes.window_returns(nes.PriceGrid(base), pub, windows=["pre_24h", "pre_6h", "pre_1h"])
    b = nes.window_returns(nes.PriceGrid(changed), pub, windows=["pre_24h", "pre_6h", "pre_1h"])
    for k in a:
        assert a[k][0] == b[k][0]


# ------------------------------------------------------------------------------------------------ statistics


def test_block_bootstrap_mean_centred_and_constant() -> None:
    rng = np.random.default_rng(0)
    vals = np.arange(100, dtype=np.float64)
    blocks = np.arange(100, dtype=np.int64) // 5
    boot = nes.block_bootstrap_mean(vals, blocks, n_boot=4000, rng=rng)
    assert abs(boot.mean() - vals.mean()) < 1.0
    assert boot.std() > 0
    const = nes.block_bootstrap_mean(np.full(20, 7.0), np.arange(20, dtype=np.int64) // 4, n_boot=50, rng=rng)
    assert np.allclose(const, 7.0)


def _pool(n: int = 20_000, seed: int = 1) -> tuple[nes.ControlPool, np.random.Generator]:
    rng = np.random.default_rng(seed)
    ret = rng.normal(0.0, 100.0, n)
    dh = rng.integers(0, 168, n).astype(np.int64)
    full = dh + 168 * rng.integers(0, 8, n)  # 8 pseudo-quarters
    return nes.ControlPool(ret, full.astype(np.int64), dh), rng


def test_control_pool_match_prefers_full_stratum_and_falls_back() -> None:
    ret = np.array([1.0, 2.0, 3.0, 4.0])
    full = np.array([10, 10, 20, 30], dtype=np.int64)
    dh = np.array([1, 1, 2, 2], dtype=np.int64)
    pool = nes.ControlPool(ret, full, dh)
    lo, n = pool.match(np.array([10, 99], dtype=np.int64), np.array([1, 2], dtype=np.int64))
    assert list(n) == [2, 2]  # full stratum 10 has 2; 99 is empty -> (weekday, hour) 2 has 2
    assert list(pool.stratum_mean(lo, n)) == [1.5, 3.5]
    draws = pool.draw_means(lo, n, n_draws=200, rng=np.random.default_rng(0))
    assert set(np.round(draws, 6)) <= {2.0, 2.5, 3.0}  # one value of {1, 2} plus one of {3, 4}, averaged


def test_excess_stats_detects_a_planted_effect_and_not_a_null() -> None:
    pool, rng = _pool()
    n = 150
    dh = rng.integers(0, 168, n).astype(np.int64)
    full = (dh + 168 * rng.integers(0, 8, n)).astype(np.int64)
    blocks = np.arange(n, dtype=np.int64) // 3
    null = nes.excess_stats(rng.normal(0, 100, n), pool, full, dh, blocks, n_boot=1500, n_draws=1000, rng=rng)
    eff = nes.excess_stats(rng.normal(60, 100, n), pool, full, dh, blocks, n_boot=1500, n_draws=1000, rng=rng)
    assert null is not None and eff is not None
    assert null.p_boot > 0.01 and null.excess_lo < 0 < null.excess_hi
    assert eff.p_boot < 0.01 and eff.p_ctrl < 0.01
    assert eff.excess_bps == pytest.approx(60, abs=25)
    assert eff.excess_lo > 0
    assert eff.control_lo < eff.control_bps < eff.control_hi


def test_excess_stats_needs_enough_events() -> None:
    pool, rng = _pool(2000)
    full = np.array([1, 2], dtype=np.int64)
    out = nes.excess_stats(np.array([1.0, 2.0]), pool, full, full, full, n_boot=10, n_draws=10, rng=rng)
    assert out is None


def test_holm_and_bh_known_values() -> None:
    p = [0.01, 0.04, 0.03, 0.005]
    assert nes.holm_adjust(p) == pytest.approx([0.03, 0.06, 0.06, 0.02])
    assert nes.bh_adjust(p) == pytest.approx([0.02, 0.04, 0.04, 0.02])
    assert nes.holm_adjust([0.9]) == pytest.approx([0.9])


def test_mde_matches_the_textbook_formula() -> None:
    assert nes.mde(1.0) == pytest.approx(1.959964 + 0.841621, rel=1e-5)
    assert nes.mde(2.0, n_tests=10) > nes.mde(2.0)
    assert nes.mde(0.0) == 0.0


def test_move_triggered_profile_peaks_at_the_true_lag() -> None:
    n = 24 * 60 * 12  # 60 days of 5-minute bars
    idx = pd.date_range("2024-01-01", periods=n, freq="5min", tz="UTC")
    px = np.full(n, 100.0)
    jumps = [i * 12 * 24 * 3 + 12 * 10 for i in range(1, 19)]  # one jump every 3 days at 10:00
    for j in jumps:
        px[j:] *= 1.05
    grid = nes.PriceGrid(pd.DataFrame({"open": px}, index=idx))
    base = int(idx[0].timestamp())

    def news_after(shift_h: int) -> np.ndarray:  # 30 headlines in the hour after each jump, shifted
        ts = [base + j * 300 + 600 + k * 60 + shift_h * 3600 for j in jumps for k in range(30)]
        return np.array(ts, dtype=np.int64)

    lags, prof = nes.move_triggered_news_profile(news_after(0), grid, top=15, max_lag_h=8)
    assert lags[int(np.argmax(prof))] == 1  # news in the hour AFTER the hour of the move
    lags, prof = nes.move_triggered_news_profile(news_after(3), grid, top=15, max_lag_h=8)
    assert lags[int(np.argmax(prof))] == 4  # a 3 h shift of the clock moves the peak by 3 h


# ------------------------------------------------------------------------------------------------ study end to end


def test_run_study_end_to_end_on_synthetic_data() -> None:
    rng = np.random.default_rng(7)
    n = 24 * 12 * 120  # 120 days
    idx = pd.date_range("2024-01-01", periods=n, freq="5min", tz="UTC")
    px = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.001, n)))
    grids = {"BTC": nes.PriceGrid(pd.DataFrame({"open": px}, index=idx))}
    stamps = pd.to_datetime(
        1704067200 + rng.integers(0, 110 * DAY, 4000), unit="s", utc=True
    )  # random news instants over 110 days
    news = pd.DataFrame({"ts": stamps, "title": [f"story {i}" for i in range(len(stamps))], "cls": "altro"})
    news.loc[news.index[:200], "cls"] = "hack_exploit"
    news.loc[news.index[200:260], "cls"] = "regolazione_positiva"
    table, counts = nes.run_study("syn", news, grids, n_boot=200, n_draws=200, seed=1)
    assert set(counts["event"]) == set(nes.EVENT_CLASSES)
    row = counts[counts["event"] == "hack_exploit"].iloc[0]
    assert row["n_titles"] == 200 and 1 <= row["n_events_day"] <= 110 and row["n_indep_3d"] <= row["n_events_day"]
    hack = table[(table["event"] == "hack_exploit") & (table["window"] == "post_1h")]
    assert len(hack) == 1 and hack.iloc[0]["n"] > 50
    fams = set(table["family"])
    assert fams == {"primary", "diagnostic"}
    adj = nes.add_corrections(table)
    assert (adj["p_holm"] >= adj["p_boot"] - 1e-12).all()
    assert (adj["p_bh"] <= adj["p_holm"] + 1e-12).all()
    assert (adj["net_bps"] == adj["mean_bps"] - nes.COST_BPS).all()
    md = nes.render_report(adj, counts, {"syn": "lag(h):rate +0:1.00"}, {"tagger": nes.TAGGER_VERSION})
    assert "hack_exploit" in md and "Holm" in md


def test_sentiment_classes_use_the_datasets_own_quantiles() -> None:
    score = np.linspace(-0.5, 1.0, 101)  # biased towards bullish like the real model
    cls = nes.sentiment_classes(score, tail=0.1)
    assert (cls == "sent_bull").sum() == pytest.approx(10, abs=1)
    assert (cls == "sent_bear").sum() == pytest.approx(10, abs=1)
    assert cls[0] == "sent_bear" and cls[-1] == "sent_bull" and cls[50] == ""


def test_score_titles_is_p_bull_minus_p_bear() -> None:
    class Fake:
        def classify_batch(self, texts: list[str]) -> list[SimpleNamespace]:
            bull, bear = SimpleNamespace(probs=(0.1, 0.2, 0.7)), SimpleNamespace(probs=(0.6, 0.3, 0.1))
            return [bull if "up" in t else bear for t in texts]

    s = nes.score_titles(Fake(), ["btc up", "btc down", "up again"], batch=2)
    assert s == pytest.approx([0.6, -0.5, 0.6])


# ------------------------------------------------------------------------------------------------ download


def test_fetch_candles_paginates_dedups_and_retries() -> None:
    calls: list[tuple[int, int]] = []
    state = {"fail_once": True}

    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params
        start, end = int(q["start"]), int(q["end"])
        calls.append((start, end))
        assert q["granularity"] == "FIVE_MINUTE" and int(q["limit"]) == 350
        assert (end - start) // 300 + 1 <= 350
        if state["fail_once"]:
            state["fail_once"] = False
            return httpx.Response(429, json={})
        candles = [
            {"start": str(t), "low": "1", "high": "3", "open": "2", "close": "2", "volume": "5"}
            for t in range(end // 300 * 300, start - 1, -300)  # the API returns newest first
        ]
        return httpx.Response(200, json={"candles": candles})

    sleeps: list[float] = []
    start = pd.Timestamp("2024-01-01", tz="UTC")
    end = start + pd.Timedelta(minutes=5 * 800)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        df = nes.fetch_candles("BTC-USD", start, end, client=client, sleep=sleeps.append)
    assert len(df) == 800 and df.index.is_monotonic_increasing and df.index.is_unique
    assert df.index[0] == start and df.index[-1] == end - pd.Timedelta(minutes=5)
    assert len(calls) == 4  # 3 pages (350 + 350 + 100) + 1 retry
    assert sleeps and sleeps[0] > 0
