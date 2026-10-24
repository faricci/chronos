"""News event study (offline research, NOT a model): do crypto headlines move BTC/ETH enough to beat the costs?

The question comes from the idea "news such as 'Bitcoin legalised in a state' make the price rise, so a model that
reads the news gives a signal at once". The idea is TESTED here on public headline datasets with a timestamp, before
building anything on it. Everything is pre-registered in ``docs/decisions/2026-10-20-studio-eventi-news.md``: the
tagging rules below are frozen (``TAGGER_VERSION``), the windows, the control, the multiple-testing family and the
cost (77 bps) are fixed before the returns are computed.

Layers (the first four are pure and tested with synthetic data):

- ``tag_title``                rule-based event class of a headline (``EventClass``), no model, no network;
- ``cluster_events``           headlines of the same class and UTC day are ONE independent event;
- ``PriceGrid`` / ``window_returns``  returns around the publication from 5-minute candles (no look-ahead: the
                               pre-event window ends at the bar open at or before the publication, the entry is
                               ``entry_delay`` after it);
- ``ControlPool`` / ``excess_stats``  random instants of the same dataset matched on (quarter, weekday, hour) with a
                               fallback to (weekday, hour); week-block bootstrap and control draws;
- ``holm_adjust`` / ``bh_adjust`` / ``mde``  multiple comparisons and statistical power;
- ``fetch_candles``, loaders, ``classify_titles``, ``main``  I/O (public read-only Coinbase endpoint, the local ONNX
                               sentiment classifier).

Prices live in ``data/external/prices`` (never in ``data/bars``, which belongs to the soak/dry run).
"""

from __future__ import annotations

import argparse
import hashlib
import math
import re
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, Protocol

import httpx
import numpy as np
import numpy.typing as npt
import pandas as pd
from scipy import stats

__all__ = [
    "COST_BPS",
    "TAGGER_VERSION",
    "ControlPool",
    "EventClass",
    "PriceGrid",
    "bh_adjust",
    "block_bootstrap_mean",
    "cluster_events",
    "dedupe_titles",
    "excess_stats",
    "fetch_candles",
    "holm_adjust",
    "independent_spacing",
    "mde",
    "move_triggered_news_profile",
    "normalize_text",
    "tag_price_move",
    "tag_title",
    "window_returns",
]

EventClass = Literal[
    "regolazione_positiva", "regolazione_negativa", "adozione_istituzionale", "hack_exploit", "altro"
]
EVENT_CLASSES: Final[tuple[EventClass, ...]] = (
    "regolazione_positiva",
    "regolazione_negativa",
    "adozione_istituzionale",
    "hack_exploit",
)
FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]

# average round trip of the cost model (fees maker 25 / taker 50 bps, slippage 5, extra stop slippage 10)
COST_BPS: Final = 77.0
CANDLES_URL: Final = "https://api.coinbase.com/api/v3/brokerage/market/products/{product}/candles"
CANDLE_S: Final = 300
MAX_CANDLES: Final = 350
MIN_INTERVAL_S: Final = 0.25  # <= 4 requests/s, below the ~5/s ceiling of the public endpoint
DAY_S: Final = 86_400
WEEK_S: Final = 7 * DAY_S
COINDESK_LOCAL_TZ: Final = "Asia/Tehran"  # clock of the CoinDesk/CryptoCompare dataset (see load_coindesk)
ENTRY_DELAY_S: Final = 300  # the system decides at t + 2 min and enters after: 5 min is the optimistic bound

# window name -> (kind, seconds). pre = run-up ending at the bar open at or before the publication; react = move from
# there to the entry; post = tradeable return from the entry. PRIMARY windows are the tradeable ones.
WINDOWS: Final[dict[str, tuple[str, int]]] = {
    "pre_24h": ("pre", DAY_S),
    "pre_6h": ("pre", 6 * 3600),
    "pre_1h": ("pre", 3600),
    "react": ("react", 0),
    "post_1h": ("post", 3600),
    "post_6h": ("post", 6 * 3600),
    "post_24h": ("post", DAY_S),
    "post_3d": ("post", 3 * DAY_S),
}
PRIMARY_WINDOWS: Final = ("post_1h", "post_6h", "post_24h", "post_3d")
DIAGNOSTIC_WINDOWS: Final = ("pre_24h", "pre_6h", "pre_1h", "react")

# ------------------------------------------------------------------------------------------------ tagging

_APOS_S = re.compile(r"['’�]s\b")
_APOS = re.compile(r"['’�]")
_NON_WORD = re.compile(r"[^a-z0-9$%?]+")


def normalize_text(title: str) -> str:
    """Lowercase, possessive and apostrophes removed, everything but ``[a-z0-9$%?]`` becomes one space."""
    t = _APOS.sub("", _APOS_S.sub("", title.lower()))
    return _NON_WORD.sub(" ", t).strip()


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


_CRYPTO = (
    r"\b(?:bitcoin|btc|crypto\w*|ether|ethereum|eth|digital assets?|blockchain|stablecoins?|virtual assets?|mining|"
    r"miners?|tokens?|ico|icos|cbdc|etfs?)\b"
)
_REG_ACTOR = (
    r"\b(?:sec|cftc|doj|irs|ftc|justice department|regulators?|regulatory|authorities|authority|government|court|"
    r"judge|senate|senator|congress|parliament|ministry|commission|central bank|fca|mas|sfc|esma|finma|bafin|fed|"
    r"cboe|nasdaq|nyse|china|chinese|india|indian|russia|russian|korea|korean|japan|hong kong|brazil|eu|uk|"
    r"attorney general|prosecutors?|police|treasury|ofac|nydfs|finra|state|country|president)\b"
)

HEDGE: Final = _rx(
    r"\?|\b(?:fake|false|rumou?rs?|rumou?red|denies|deny|denied|could|may|might|would|should|can|will|set to|"
    r"about to|expected|expects?|plans?|planning|seeks?|asks?|wants?|urges?|calls? for|proposes?|proposed|proposal|"
    r"considers?|considering|mulls?|explores?|weighs?|eyes?|hopes?|bets?|predicts?|prediction|analysts?|if|whether|"
    r"when|why|how|what|who|ahead of|pending|awaits?|anticipat\w+|speculation|potential|possible|possibly|"
    r"reportedly|alleged\w*|claims?|anniversary|vs|versus|introduces?|introduced|pushes? for|lobby\w*|campaign\w*|"
    r"likely|poised|warns?|warning|threatens?|threatened|option|to be|fears?|risks?|talks|doubt|must|"
    r"pledges?|signals?|according to|speculate)\b"
)

_LIFT_BAN = _rx(
    r"\b(?:lifts?|lifted|ends?|ended|scraps?|scrapped|repeals?|repealed|overturns?|overturned|removes?|removed)\b"
    r".{0,20}\bban\b|\bban\b.{0,15}\b(?:lifted|ended|scrapped|repealed|overturned)\b"
)
_REG_POS: Final[tuple[re.Pattern[str], ...]] = (
    _rx(r"\blegali[sz](?:e|es|ed|ing|ation)\b"),
    _rx(
        r"\b(?:adopts?|adopted|makes?|made|declares?|declared|recogni[sz]es?|recogni[sz]ed|passes|passed|signs|signed|"
        r"approves?|approved)\b.*\blegal tender\b|\blegal tender\b.*\b(?:passes|passed|signed|approved)\b"
    ),
    _rx(
        rf"^(?=.*{_REG_ACTOR})(?=.*{_CRYPTO}).*"
        r"\b(?:approves?|approved|green ?lights?|green ?lit|clears|cleared|authori[sz]es?|authori[sz]ed)\b"
    ),
    _rx(r"\b(?:gets|receives|secures|wins|obtains|granted|awarded)\b.{0,30}\b(?:approval|licen[cs]e|go ahead)\b"),
    _rx(
        rf"^(?=.*{_CRYPTO}).*"
        r"\b(?:passes|passed|signs|signed|enacts?|enacted|adopts?|adopted)\b.{0,60}"
        r"\b(?:bill|law|act|legislation|regulation|framework|mica)\b"
    ),
    _rx(
        r"\b(?:establish\w*|creates?|created|signs?|signed|passes|passed|approves?|approved|launches?|launched)\b"
        r".{0,40}\b(?:bitcoin|btc|crypto) reserve\b"
    ),
    _LIFT_BAN,
)
_BAN = _rx(r"\b(?:bans?|banned|banning|prohibits?|prohibited|outlaws?|outlawed)\b")
_NEG_VERB = _rx(
    r"\b(?:sues?|sued|charges|charged|fines?|fined|subpoenas?|subpoenaed|indicts?|indicted|sanctions?|sanctioned|"
    r"shuts? down|raids?|raided|cracks? down|clamps? down|crackdown|cease and desist|rejects?|rejected|delays?|"
    r"delayed|blocks?|blocked|halts?|halted|suspends?|suspended|restricts?|restricted|tightens?|tightened|"
    r"freezes?|froze|frozen|seizes?|seized)\b"
)
_NEG_GUARD = _rx(
    r"\b(?:without|no|drops?|dropped|dismiss\w*|closes?|closed|ends?|ended|withdraws?|withdrew|lifts?|lifted|wins?|"
    r"won|overturns?|overturned|clears?|cleared|settles?|settled|appeals?|reverses?|reversed|against|"
    r"fell short|fails?|failed)\b"
)
_HACK_WORD = _rx(
    r"\b(?:hack(?:s|ed|ers?)?|exploit(?:s|ed)?|stolen|steals?|stole|drain(?:s|ed)?|rug ?pull(?:s|ed)?|"
    r"security breach|heist|theft|compromised)\b"
)
_HACK_AMOUNT = _rx(r"\$\s?\d|\b\d[\d ]*(?:m|k|bn|b|mln|million|billion)\b|\bmillion|\bbillion")
_HACK_GUARD = _rx(
    r"\b(?:recover\w*|returns?|returned|refunds?|refunded|arrest\w*|sentenced|convicted|jailed|pleads?|guilty|"
    r"extradited|year to date|ytd|annual|year|losses top|record|lawsuit|sues?|sued|settle\w*|reimburs\w*|"
    r"compensat\w*|bounty|funds? back|stats?|statistics|report|reports|quarter|q[1-4]|total|etfs?|outflows?|"
    r"inflows?|bloodbath|demand)\b"
)
_INSTITUTIONS = (
    r"\b(?:blackrock|fidelity|vanguard|jpmorgan|jp morgan|goldman|goldman sachs|morgan stanley|citi|citigroup|"
    r"wells fargo|bank of america|bny mellon|state street|schwab|franklin templeton|invesco|wisdomtree|ark invest|"
    r"grayscale|visa|mastercard|paypal|stripe|shopify|square|block inc|tesla|microstrategy|strategy|amazon|apple|"
    r"google|microsoft|walmart|starbucks|meta|facebook|nasdaq|nyse|cme|cboe|robinhood|interactive brokers|revolut|"
    r"deutsche bank|hsbc|barclays|santander|ubs|credit suisse|standard chartered|bnp paribas|nomura|"
    r"pension funds?|endowment|sovereign wealth|asset managers?|hedge funds?|insurer|insurance giant|"
    r"nasdaq listed|publicly traded|major banks?|banking giant)\b"
)
_ADOPT_VERB = _rx(
    r"\b(?:adds?|added|buys?|bought|purchases?|purchased|acquires?|acquired|accepts?|accepted|allows?|enables?|"
    r"launch(?:es|ed)?|offers?|offered|integrates?|integrated|rolls? out|lists?|listed|invests?|invested|"
    r"allocates?|allocated|holds|custody|files? for|opens?|opened|begins?|starts?|supports?)\b"
)
_ADOPT_GUARD = _rx(r"\b(?:ends?|ending|halts?|stops?|drops?|dropped|closes?|closed|delists?|delisted|cuts?)\b")
_INST = _rx(_INSTITUTIONS)
_CRYPTO_RX = _rx(_CRYPTO)
_ACTOR_NEG = _rx(_REG_ACTOR)


def _tagger_version() -> str:
    parts = [HEDGE.pattern, _LIFT_BAN.pattern, _BAN.pattern, _NEG_VERB.pattern, _NEG_GUARD.pattern, _HACK_WORD.pattern]
    parts += [_HACK_AMOUNT.pattern, _HACK_GUARD.pattern, _INSTITUTIONS, _ADOPT_VERB.pattern, _CRYPTO, _REG_ACTOR]
    parts += [_ADOPT_GUARD.pattern]
    parts += [p.pattern for p in _REG_POS]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:12]


TAGGER_VERSION: Final = _tagger_version()


def tag_title(title: str) -> EventClass:
    """Event class of a headline. Pure and deterministic. Precedence when several match:
    ``hack_exploit`` > ``regolazione_negativa`` > ``regolazione_positiva`` > ``adozione_istituzionale`` > ``altro``.

    A headline with a hedge (question, 'could', 'plans', 'fake', ...) is ``altro``: the rules look for events that
    HAPPENED, not for expectations. Titles are matched on :func:`normalize_text`.
    """
    t = normalize_text(title)
    if not t or HEDGE.search(t):
        return "altro"
    if _HACK_WORD.search(t) and _HACK_AMOUNT.search(t) and not _HACK_GUARD.search(t):
        return "hack_exploit"
    lifted = _LIFT_BAN.search(t) is not None
    if not _NEG_GUARD.search(t) and _CRYPTO_RX.search(t):
        if (_BAN.search(t) and not lifted) or (_NEG_VERB.search(t) and _ACTOR_NEG.search(t)):
            return "regolazione_negativa"
    if any(p.search(t) for p in _REG_POS):
        return "regolazione_positiva"
    if _INST.search(t) and _ADOPT_VERB.search(t) and _CRYPTO_RX.search(t) and not _ADOPT_GUARD.search(t):
        return "adozione_istituzionale"
    return "altro"


_MOVE_UP = _rx(r"\b(?:surges?|soars?|jumps?|rallies|rockets?|spikes?|skyrockets?|climbs?|rebounds?|pumps?)\b")
_MOVE_DOWN = _rx(
    r"\b(?:plunges?|crashes?|tumbles?|drops?|falls?|sinks?|slumps?|slides?|plummets?|dips?|tanks?|dumps?)\b"
)
MOVE_CLASSES: Final = ("move_up", "move_down")


def tag_price_move(title: str) -> str:
    """POST-HOC validation tag (not part of the pre-registration): a headline that REPORTS a price move of a major
    coin. It must show a run-up/run-down BEFORE the stamp if the stamps are right and the news come after the price."""
    t = normalize_text(title)
    if not t or HEDGE.search(t) or not re.search(r"\b(?:bitcoin|btc|ether|ethereum|eth|crypto\w*)\b", t):
        return ""
    up, down = _MOVE_UP.search(t) is not None, _MOVE_DOWN.search(t) is not None
    return "move_up" if up and not down else "move_down" if down and not up else ""


def dedupe_titles(df: pd.DataFrame) -> pd.DataFrame:
    """Keep the earliest row of every normalised title (``ts``, ``title``). Adds ``n_copies`` (rows with that title).

    A republished headline (same normalised title) is the same story: only its first appearance is an event.
    """
    key = df["title"].map(normalize_text)
    out = df.assign(key=key).sort_values("ts", kind="stable")
    copies = out.groupby("key")["ts"].transform("size")
    out = out.assign(n_copies=copies).drop_duplicates("key", keep="first")
    return out[out["key"] != ""].reset_index(drop=True)


def cluster_events(df: pd.DataFrame, *, class_col: str = "cls") -> pd.DataFrame:
    """One INDEPENDENT event per (class, UTC day): the earliest headline of the day. ``n_titles`` = headlines merged."""
    d = df.assign(day=df["ts"].dt.floor("D"))
    d = d.sort_values("ts", kind="stable")
    g = d.groupby([class_col, "day"], sort=True)
    first = g.head(1).copy()
    first["n_titles"] = g["ts"].transform("size").loc[first.index].to_numpy()
    return first.sort_values("ts").reset_index(drop=True)


def independent_spacing(ts_s: IntArray, min_gap_s: int) -> int:
    """Events left by a greedy filter that keeps one only if it is >= ``min_gap_s`` after the last one kept."""
    n = 0
    last: int | None = None
    for t in np.sort(ts_s):
        if last is None or int(t) - last >= min_gap_s:
            n += 1
            last = int(t)
    return n


# ------------------------------------------------------------------------------------------------ prices


def _epoch_s_of(idx: pd.DatetimeIndex) -> IntArray:
    """Epoch seconds of a tz-aware index whatever its resolution (pandas 3 does not guarantee ns)."""
    return np.asarray(idx.tz_convert("UTC").values.astype("datetime64[s]").astype(np.int64), dtype=np.int64)


class PriceGrid:
    """Regular 5-minute grid of the bar OPEN prices; a missing bar is NaN (never filled)."""

    def __init__(self, candles: pd.DataFrame) -> None:
        idx = candles.index
        self.start_s = int(idx[0].timestamp()) // CANDLE_S * CANDLE_S
        end_s = int(idx[-1].timestamp()) // CANDLE_S * CANDLE_S
        n = (end_s - self.start_s) // CANDLE_S + 1
        self.end_s = end_s
        self.open: FloatArray = np.full(n, np.nan)
        pos = (_epoch_s_of(pd.DatetimeIndex(idx)) - self.start_s) // CANDLE_S
        self.open[pos] = candles["open"].to_numpy(dtype=np.float64)

    def open_at(self, bar_start_s: IntArray) -> FloatArray:
        """Open of the bar starting at each second timestamp (a multiple of 300), NaN when out of range or missing."""
        pos = (bar_start_s - self.start_s) // CANDLE_S
        ok = (pos >= 0) & (pos < len(self.open)) & ((bar_start_s - self.start_s) % CANDLE_S == 0)
        out: FloatArray = np.full(bar_start_s.shape, np.nan)
        out[ok] = self.open[pos[ok]]
        return out


def window_returns(
    grid: PriceGrid, pub_s: IntArray, *, entry_delay_s: int = ENTRY_DELAY_S, windows: Sequence[str] | None = None
) -> dict[str, FloatArray]:
    """Simple returns in bps for every window around the publication instants ``pub_s`` (epoch seconds).

    ``a`` = bar open at or before the publication (nothing after the publication leaks into ``pre_*``);
    ``E`` = first bar open at or after ``pub + entry_delay`` (the earliest price at which the system can be filled);
    ``pre_k``  = open(a) / open(a - k) - 1;  ``react`` = open(E) / open(a) - 1;  ``post_h`` = open(E + h) / open(E) - 1.
    """
    a = pub_s // CANDLE_S * CANDLE_S
    e = -(-(pub_s + entry_delay_s) // CANDLE_S) * CANDLE_S
    p_a = grid.open_at(a)
    p_e = grid.open_at(e)
    out: dict[str, FloatArray] = {}
    for name in windows if windows is not None else tuple(WINDOWS):
        kind, secs = WINDOWS[name]
        if kind == "pre":
            r = p_a / grid.open_at(a - secs) - 1.0
        elif kind == "react":
            r = p_e / p_a - 1.0
        else:
            r = grid.open_at(e + secs) / p_e - 1.0
        out[name] = r * 1e4
    return out


# ------------------------------------------------------------------------------------------------ statistics


def block_bootstrap_mean(
    values: FloatArray, block_ids: IntArray, *, n_boot: int, rng: np.random.Generator
) -> FloatArray:
    """Bootstrap distribution of the mean when whole BLOCKS (e.g. weeks) are resampled with replacement."""
    ids, inv = np.unique(block_ids, return_inverse=True)
    sums = np.bincount(inv, weights=values, minlength=len(ids))
    cnts = np.bincount(inv, minlength=len(ids)).astype(np.float64)
    pick = rng.integers(0, len(ids), size=(n_boot, len(ids)))
    out: FloatArray = sums[pick].sum(axis=1) / cnts[pick].sum(axis=1)
    return out


class ControlPool:
    """Control instants of one window: returns plus the strata (quarter, weekday, hour) and (weekday, hour).

    :meth:`match` returns, for events with given strata, the slice of the sorted pool each one draws from (the full
    stratum when it has candidates, else the (weekday, hour) one) so that draws are fully vectorised.
    """

    def __init__(self, ret: FloatArray, full: IntArray, dh: IntArray) -> None:
        ok = np.isfinite(ret)
        o_full = np.argsort(full[ok], kind="stable")
        o_dh = np.argsort(dh[ok], kind="stable")
        self._full_codes = full[ok][o_full]
        self._dh_codes = dh[ok][o_dh]
        self._pool: FloatArray = np.concatenate([ret[ok][o_full], ret[ok][o_dh]])
        self._dh_offset = int(ok.sum())
        self._cum: FloatArray = np.concatenate([[0.0], np.cumsum(self._pool)])

    def match(self, full: IntArray, dh: IntArray) -> tuple[IntArray, IntArray]:
        lo_f = np.searchsorted(self._full_codes, full, side="left")
        n_f = np.searchsorted(self._full_codes, full, side="right") - lo_f
        lo_d = np.searchsorted(self._dh_codes, dh, side="left") + self._dh_offset
        n_d = np.searchsorted(self._dh_codes, dh, side="right") + self._dh_offset - lo_d
        use_full = n_f > 0
        lo = np.where(use_full, lo_f, lo_d).astype(np.int64)
        n = np.where(use_full, n_f, n_d).astype(np.int64)
        return lo, n

    def stratum_mean(self, lo: IntArray, n: IntArray) -> FloatArray:
        with np.errstate(invalid="ignore", divide="ignore"):
            out: FloatArray = (self._cum[lo + n] - self._cum[lo]) / n
        return out

    def draw_means(self, lo: IntArray, n: IntArray, *, n_draws: int, rng: np.random.Generator) -> FloatArray:
        """``n_draws`` means over the events of one control instant per event, drawn uniformly in its stratum."""
        u = rng.random((n_draws, len(lo)))
        idx = lo[None, :] + np.minimum((u * n[None, :]).astype(np.int64), n[None, :] - 1)
        out: FloatArray = self._pool[idx].mean(axis=1)
        return out


@dataclass(frozen=True)
class ExcessStats:
    n: int
    mean_bps: float
    mean_lo: float
    mean_hi: float
    control_bps: float
    control_lo: float
    control_hi: float
    excess_bps: float
    excess_lo: float
    excess_hi: float
    p_boot: float
    p_ctrl: float
    se_excess: float
    median_bps: float
    pos_share: float


def _two_sided_p(dist: FloatArray, centre: float = 0.0) -> float:
    """Share of a distribution on the far side of ``centre``, doubled, with a +1 smoothing."""
    b = len(dist)
    below = (np.sum(dist <= centre) + 1) / (b + 1)
    above = (np.sum(dist >= centre) + 1) / (b + 1)
    return float(min(1.0, 2.0 * min(below, above)))


def excess_stats(
    ret: FloatArray,
    pool: ControlPool,
    full: IntArray,
    dh: IntArray,
    block_ids: IntArray,
    *,
    n_boot: int,
    n_draws: int,
    rng: np.random.Generator,
) -> ExcessStats | None:
    """Mean return of the events, of their matched controls, and the excess with block-bootstrap intervals.

    Events with a NaN return are dropped. ``excess_i = r_i - mean(controls in its stratum)``; the CI and ``p_boot`` come
    from resampling weeks of ``excess_i``; ``p_ctrl`` from comparing the event mean with ``n_draws`` draws of one
    control per event (the null of 'these instants behave like matched random news instants').
    """
    ok = np.isfinite(ret)
    if ok.sum() < 3:
        return None
    r, f, d, b = ret[ok], full[ok], dh[ok], block_ids[ok]
    lo, n = pool.match(f, d)
    has = n > 0
    if has.sum() < 3:
        return None
    r, f, d, b, lo, n = r[has], f[has], d[has], b[has], lo[has], n[has]
    strat = pool.stratum_mean(lo, n)
    exc = r - strat
    boot_exc = block_bootstrap_mean(exc, b, n_boot=n_boot, rng=rng)
    boot_raw = block_bootstrap_mean(r, b, n_boot=n_boot, rng=rng)
    draws = pool.draw_means(lo, n, n_draws=n_draws, rng=rng)
    m_e = float(r.mean())
    m_c = float(draws.mean())
    p_ctrl = float((np.sum(np.abs(draws - m_c) >= abs(m_e - m_c)) + 1) / (n_draws + 1))
    q = (2.5, 97.5)
    return ExcessStats(
        n=int(len(r)),
        mean_bps=m_e,
        mean_lo=float(np.percentile(boot_raw, q[0])),
        mean_hi=float(np.percentile(boot_raw, q[1])),
        control_bps=m_c,
        control_lo=float(np.percentile(draws, q[0])),
        control_hi=float(np.percentile(draws, q[1])),
        excess_bps=float(exc.mean()),
        excess_lo=float(np.percentile(boot_exc, q[0])),
        excess_hi=float(np.percentile(boot_exc, q[1])),
        p_boot=_two_sided_p(boot_exc, 0.0),
        p_ctrl=p_ctrl,
        se_excess=float(boot_exc.std(ddof=1)),
        median_bps=float(np.median(r)),
        pos_share=float((r > 0).mean()),
    )


def holm_adjust(p: Sequence[float]) -> FloatArray:
    """Holm-Bonferroni adjusted p-values (family-wise error rate), in the input order."""
    arr = np.asarray(p, dtype=np.float64)
    m = len(arr)
    order = np.argsort(arr)
    adj = np.empty(m)
    run = 0.0
    for rank, i in enumerate(order):
        run = max(run, (m - rank) * arr[i])
        adj[i] = min(1.0, run)
    return adj


def bh_adjust(p: Sequence[float]) -> FloatArray:
    """Benjamini-Hochberg adjusted p-values (false discovery rate), in the input order."""
    arr = np.asarray(p, dtype=np.float64)
    m = len(arr)
    order = np.argsort(arr)[::-1]
    adj = np.empty(m)
    run = 1.0
    for rank, i in enumerate(order):
        k = m - rank
        run = min(run, arr[i] * m / k)
        adj[i] = run
    return adj


def mde(se: float, *, alpha: float = 0.05, power: float = 0.8, n_tests: int = 1) -> float:
    """Minimum detectable true mean excess (same unit as ``se``) for a two-sided test at ``alpha / n_tests``."""
    z_a = float(stats.norm.isf(alpha / n_tests / 2.0))
    z_b = float(stats.norm.ppf(power))
    return (z_a + z_b) * se


def move_triggered_news_profile(
    news_s: IntArray, grid: PriceGrid, *, top: int = 300, max_lag_h: int = 12
) -> tuple[IntArray, FloatArray]:
    """Timestamp check: news rate (relative to the average rate) around the biggest ABSOLUTE hourly BTC moves.

    For the ``top`` largest non-overlapping |1h return| the hour counts of headlines are collected at lags
    ``-max_lag_h..+max_lag_h`` hours from the START of the move hour and divided by the mean hourly rate. If the
    timestamps are UTC, headlines react to moves: the profile rises at lag 0..+2 h; a shift of k hours moves the peak.
    """
    hours = np.arange(grid.start_s, grid.end_s - 3600, 3600, dtype=np.int64)
    p0 = grid.open_at(hours)
    p1 = grid.open_at(hours + 3600)
    mv = np.abs(p1 / p0 - 1.0)
    mv[~np.isfinite(mv)] = 0.0
    order = np.argsort(mv)[::-1]
    chosen: list[int] = []
    taken = np.zeros(len(hours), dtype=bool)
    for i in order:
        if len(chosen) >= top or mv[i] <= 0:
            break
        lo, hi = max(0, i - max_lag_h), min(len(hours), i + max_lag_h + 1)
        if taken[lo:hi].any():
            continue
        taken[i] = True
        chosen.append(int(i))
    counts = np.bincount(((news_s - grid.start_s) // 3600).clip(0, len(hours) - 1))
    counts = np.pad(counts, (0, max(0, len(hours) - len(counts))))
    rate = counts[: len(hours)].mean()
    lags = np.arange(-max_lag_h, max_lag_h + 1, dtype=np.int64)
    prof = np.zeros(len(lags))
    used = 0
    for i in chosen:
        if i - max_lag_h < 0 or i + max_lag_h >= len(hours):
            continue
        prof += counts[i - max_lag_h : i + max_lag_h + 1]
        used += 1
    return lags, prof / max(used, 1) / max(rate, 1e-12)


# ------------------------------------------------------------------------------------------------ I/O


def fetch_candles(
    product: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    *,
    client: httpx.Client,
    sleep: Callable[[float], None] = time.sleep,
) -> pd.DataFrame:
    """5-minute candles of a public Coinbase product in ``[start, end)``, ascending, no duplicates (read-only)."""
    rows: list[dict[str, Any]] = []
    span = MAX_CANDLES * CANDLE_S
    cur = int(start.timestamp())
    stop = int(end.timestamp())
    last_call = 0.0
    while cur < stop:
        hi = min(cur + span - CANDLE_S, stop - CANDLE_S)
        for attempt in range(8):
            wait = MIN_INTERVAL_S - (time.monotonic() - last_call)
            if wait > 0:
                sleep(wait)
            last_call = time.monotonic()
            try:
                r = client.get(
                    CANDLES_URL.format(product=product),
                    params={"start": cur, "end": hi, "granularity": "FIVE_MINUTE", "limit": MAX_CANDLES},
                    timeout=30.0,
                )
            except httpx.TransportError:
                sleep(2.0 * (attempt + 1))
                continue
            if r.status_code == 200:
                rows += r.json().get("candles", [])
                break
            sleep(2.0 * (attempt + 1))
        else:
            raise RuntimeError(f"{product}: request {cur}..{hi} failed after 8 attempts")
        cur = hi + CANDLE_S
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows)
    df["t"] = pd.to_datetime(df["start"].astype("int64"), unit="s", utc=True)
    df = df.drop_duplicates("t").sort_values("t").set_index("t")
    return df[["open", "high", "low", "close", "volume"]].astype("float64")


def load_dlt(path: Path) -> pd.DataFrame:
    """DLT-Sentiment-News (CryptoPanic): columns ``ts`` (UTC), ``title``, ``source``."""
    raw = pd.read_parquet(path)
    ts = pd.to_datetime(raw["timestamp"], utc=True)
    host = raw["source_url"].str.extract(r"https?://(?:www\.)?([^/]+)")[0]
    return pd.DataFrame({"ts": ts, "title": raw["title"].astype(str), "source": host.astype(str)})


def load_coindesk(path: Path) -> pd.DataFrame:
    """crypto-news-coindesk-2020-2025 (CryptoCompare news API): columns ``ts`` (UTC), ``title``, ``source``.

    MEASURED: ``published_on`` is NOT UTC. The same headlines in DLT-Sentiment-News (whose clock matches the price
    moves of known events) are 3.5 h earlier in winter and 4.5 h earlier in summer in 2021-2022, always 3.5 h from 2023:
    the pattern of Iran Standard Time (UTC+3:30, DST until 2022). The naive stamps are therefore read as
    ``COINDESK_LOCAL_TZ`` and converted (ambiguous hour of the DST change dropped).
    """
    raw = pd.read_csv(path, usecols=["published_on", "title", "source"], low_memory=False)
    naive = pd.to_datetime(raw["published_on"], errors="coerce")
    ts = naive.dt.tz_localize(COINDESK_LOCAL_TZ, ambiguous="NaT", nonexistent="shift_forward").dt.tz_convert("UTC")
    df = pd.DataFrame({"ts": ts, "title": raw["title"].fillna("").astype(str), "source": raw["source"].astype(str)})
    return df.dropna(subset=["ts"]).reset_index(drop=True)


class _Classifier(Protocol):
    def classify_batch(self, texts: list[str]) -> Sequence[Any]: ...


def score_titles(clf: _Classifier, titles: Sequence[str], *, batch: int = 64) -> FloatArray:
    """Score ``p_bull - p_bear`` in [-1, 1] of every title (title only, no summary: the datasets have no clean one)."""
    out = np.empty(len(titles))
    for i in range(0, len(titles), batch):
        res = clf.classify_batch(list(titles[i : i + batch]))
        out[i : i + len(res)] = [o.probs[2] - o.probs[0] for o in res]
    return out


def load_classifier(model_dir: Path) -> _Classifier:
    from modules.sentiment.classifier import OnnxSentimentClassifier, load_model_card

    card = load_model_card(model_dir / "sentiment_model_card.json")
    return OnnxSentimentClassifier.from_dir(model_dir / "cryptobert-onnx-int8", card)


def sentiment_classes(score: FloatArray, *, tail: float = 0.10) -> npt.NDArray[np.object_]:
    """``sent_bull`` for the top ``tail`` of the dataset's own score distribution, ``sent_bear`` for the bottom
    ``tail`` (the local model is biased bullish: fixed thresholds would make the two classes unequal), else ``''``."""
    lo, hi = np.quantile(score, [tail, 1.0 - tail])
    out = np.full(score.shape, "", dtype=object)
    out[score >= hi] = "sent_bull"
    out[score <= lo] = "sent_bear"
    return out


# ------------------------------------------------------------------------------------------------ the study

SENT_CLASSES: Final = ("sent_bull", "sent_bear")


def strata(ts: pd.Series) -> tuple[IntArray, IntArray]:
    """(quarter, weekday, hour) and (weekday, hour) stratum codes of UTC timestamps."""
    dh = (ts.dt.dayofweek * 24 + ts.dt.hour).to_numpy(dtype=np.int64)
    q = (ts.dt.year * 4 + (ts.dt.quarter - 1)).to_numpy(dtype=np.int64)
    return q * 168 + dh, dh


def _epoch_s(ts: pd.Series) -> IntArray:
    return _epoch_s_of(pd.DatetimeIndex(ts))


def run_study(
    dataset: str,
    news: pd.DataFrame,
    grids: dict[str, PriceGrid],
    *,
    n_boot: int = 5000,
    n_draws: int = 2000,
    seed: int = 20261020,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Event study of one dataset. ``news``: deduplicated headlines with ``ts`` (UTC), ``cls``, maybe ``sent_cls``.

    Returns ``(table, counts)``: one row per (asset, class, window) and the per-class event counts.
    """
    rng = np.random.default_rng(seed)
    ctrl = news[news["cls"] == "altro"].reset_index(drop=True)
    c_s = _epoch_s(ctrl["ts"])
    c_full, c_dh = strata(ctrl["ts"])
    groups: list[tuple[str, pd.DataFrame]] = [(c, news[news["cls"] == c]) for c in EVENT_CLASSES]
    if "sent_cls" in news.columns:
        groups += [(c, news[news["sent_cls"] == c]) for c in SENT_CLASSES]
    if "move_cls" in news.columns:
        groups += [(c, news[news["move_cls"] == c]) for c in MOVE_CLASSES]
    rows: list[dict[str, Any]] = []
    counts: list[dict[str, Any]] = []
    pools: dict[tuple[str, str], ControlPool] = {}
    for asset, grid in grids.items():
        cr = window_returns(grid, c_s)
        for w in WINDOWS:
            pools[(asset, w)] = ControlPool(cr[w], c_full, c_dh)
    for cls, g in groups:
        ev = cluster_events(g.assign(cls=cls)) if len(g) else g.assign(cls=cls)
        e_s = _epoch_s(ev["ts"]) if len(ev) else np.empty(0, dtype=np.int64)
        counts.append(
            {
                "dataset": dataset,
                "event": cls,
                "n_titles": int(len(g)),
                "n_events_day": int(len(ev)),
                "n_indep_3d": independent_spacing(e_s, 3 * DAY_S),
            }
        )
        if len(ev) < 3:
            continue
        e_full, e_dh = strata(ev["ts"])
        blocks = e_s // WEEK_S
        for asset, grid in grids.items():
            er = window_returns(grid, e_s)
            for w in WINDOWS:
                st = excess_stats(
                    er[w], pools[(asset, w)], e_full, e_dh, blocks, n_boot=n_boot, n_draws=n_draws, rng=rng
                )
                if st is None:
                    continue
                if cls in MOVE_CLASSES:
                    family = "validation"
                elif w in DIAGNOSTIC_WINDOWS:
                    family = "diagnostic"
                else:
                    family = "sentiment" if cls in SENT_CLASSES else "primary"
                rows.append(
                    {"dataset": dataset, "asset": asset, "event": cls, "window": w, "family": family, **st.__dict__}
                )
    return pd.DataFrame(rows), pd.DataFrame(counts)


def add_corrections(table: pd.DataFrame, *, cost_bps: float = COST_BPS) -> pd.DataFrame:
    """Holm and Benjamini-Hochberg adjusted ``p_boot`` inside every family, MDE, and the net-of-cost columns."""
    t = table.copy()
    t["p_holm"] = np.nan
    t["p_bh"] = np.nan
    t["m_family"] = 0
    for idx in t.groupby("family").groups.values():
        p = t.loc[idx, "p_boot"].to_numpy(dtype=np.float64)
        t.loc[idx, "p_holm"] = holm_adjust(p)
        t.loc[idx, "p_bh"] = bh_adjust(p)
        t.loc[idx, "m_family"] = len(p)
    t["net_bps"] = t["mean_bps"] - cost_bps
    t["net_lo"] = t["mean_lo"] - cost_bps
    t["net_hi"] = t["mean_hi"] - cost_bps
    t["mde80"] = [mde(se) for se in t["se_excess"]]
    t["mde80_adj"] = [mde(se, n_tests=int(m)) for se, m in zip(t["se_excess"], t["m_family"], strict=True)]
    return t


def _f(x: Any, nd: int = 0) -> str:
    return "n/d" if not math.isfinite(x) else f"{x:.{nd}f}"


def render_report(
    table: pd.DataFrame, counts: pd.DataFrame, alignment: dict[str, str], meta: dict[str, str]
) -> str:
    lines = ["# News event study", ""]
    lines += [f"- {k}: {v}" for k, v in meta.items()]
    lines += ["", "## Timestamp alignment (news rate around the biggest absolute hourly BTC moves)", ""]
    for k, v in alignment.items():
        lines += [f"- {k}: {v}"]
    lines += ["", "## Events per class", ""]
    lines += ["| dataset | class | titles | event-days | independent (>= 3 d apart) |", "|---|---|--:|--:|--:|"]
    for r in counts.itertuples(index=False):
        lines.append(f"| {r.dataset} | {r.event} | {r.n_titles} | {r.n_events_day} | {r.n_indep_3d} |")
    for fam, title in (
        ("primary", "Tradeable windows (entry 5-10 min after publication), rule-based classes"),
        ("sentiment", "Control: local sentiment classifier (top / bottom decile of its score)"),
        ("diagnostic", "Diagnostics: run-up BEFORE the publication and reaction until the entry"),
        ("validation", "Validation (post-hoc, outside the pre-registration): headlines that REPORT a price move"),
    ):
        sub = table[table["family"] == fam]
        if sub.empty:
            continue
        lines += [
            "",
            f"## {title}",
            "",
            "Returns in bps of the asset price; `net` = mean - 77 bps; `excess` = event minus matched random "
            "news instants "
            "(95% week-block bootstrap CI); `p` = bootstrap p of excess = 0; `Holm`/`BH` = adjusted inside the family; "
            "`MDE` = minimum detectable true excess (power 80%, alpha 5%, and Bonferroni-adjusted alpha).",
            "",
            "| dataset | asset | class | window | n | mean | net | net 95% CI | control | excess | excess 95% CI "
            "| p | Holm | BH | MDE | MDE adj |",
            "|---|---|---|---|--:|--:|--:|---|--:|--:|---|--:|--:|--:|--:|--:|",
        ]
        for r in sub.sort_values(["dataset", "asset", "event", "window"]).itertuples(index=False):
            lines.append(
                f"| {r.dataset} | {r.asset} | {r.event} | {r.window} | {r.n} | {_f(r.mean_bps)} | {_f(r.net_bps)} | "
                f"[{_f(r.net_lo)}, {_f(r.net_hi)}] | {_f(r.control_bps)} | {_f(r.excess_bps)} | "
                f"[{_f(r.excess_lo)}, {_f(r.excess_hi)}] | {_f(r.p_boot, 3)} | {_f(r.p_holm, 3)} | {_f(r.p_bh, 3)} | "
                f"{_f(r.mde80)} | {_f(r.mde80_adj)} |"
            )
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------------------------------ CLI


def _load_news(name: str, root: Path) -> pd.DataFrame:
    return load_dlt(root / "dlt.parquet") if name == "dlt" else load_coindesk(root / "coindesk.csv")


def _read_grid(prices: Path, product: str) -> PriceGrid:
    return PriceGrid(pd.read_parquet(prices / f"{product}_5m.parquet"))


def _cmd_classify(args: argparse.Namespace) -> int:
    clf = load_classifier(args.model_dir)
    for name in args.datasets:
        out = args.news_dir / f"{name}_sent.parquet"
        d = dedupe_titles(_load_news(name, args.news_dir))
        done = pd.read_parquet(out) if out.exists() else pd.DataFrame({"key": [], "s": []})
        todo = d[~d["key"].isin(set(done["key"]))]
        print(name, "to classify", len(todo), flush=True)
        parts = [done]
        for i in range(0, len(todo), 4096):
            chunk = todo.iloc[i : i + 4096]
            s = score_titles(clf, chunk["title"].tolist())
            parts.append(pd.DataFrame({"key": chunk["key"].to_numpy(), "s": s}))
            pd.concat(parts, ignore_index=True).to_parquet(out)
            print(name, i + len(chunk), "/", len(todo), flush=True)
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    grids = {"BTC": _read_grid(args.prices, "BTC-USD"), "ETH": _read_grid(args.prices, "ETH-USD")}
    tables: list[pd.DataFrame] = []
    counts: list[pd.DataFrame] = []
    alignment: dict[str, str] = {}
    for name in args.datasets:
        d = dedupe_titles(_load_news(name, args.news_dir))
        d["cls"] = d["title"].map(tag_title)
        sent = args.news_dir / f"{name}_sent.parquet"
        if sent.exists():
            sc = pd.read_parquet(sent).drop_duplicates("key")
            d = d.merge(sc, on="key", how="left")
            known = d["s"].notna()
            d["sent_cls"] = ""
            d.loc[known, "sent_cls"] = sentiment_classes(d.loc[known, "s"].to_numpy(dtype=np.float64))
        d["move_cls"] = d["title"].map(tag_price_move)
        s = _epoch_s(d["ts"])
        lags, prof = move_triggered_news_profile(s, grids["BTC"])
        alignment[name] = "lag(h):rate " + ", ".join(f"{int(la):+d}:{p:.2f}" for la, p in zip(lags, prof, strict=True))
        t, c = run_study(name, d, grids, n_boot=args.n_boot, n_draws=args.n_draws)
        tables.append(t)
        counts.append(c)
    table = add_corrections(pd.concat(tables, ignore_index=True))
    cnt = pd.concat(counts, ignore_index=True)
    meta = {
        "tagger": TAGGER_VERSION,
        "cost_bps": str(COST_BPS),
        "entry_delay_s": str(ENTRY_DELAY_S),
        "draws": str(args.n_draws),
        "bootstrap": str(args.n_boot),
        "prices": "Coinbase BTC-USD / ETH-USD, 5-minute candles",
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(args.out.with_suffix(".csv"), index=False)
    cnt.to_csv(args.out.with_name(args.out.stem + "_counts.csv"), index=False)
    args.out.write_text(render_report(table, cnt, alignment, meta), encoding="utf-8", newline="\n")
    print("written", args.out)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="News event study (see the module docstring)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch-prices", help="download 5-minute candles (public Coinbase endpoint)")
    f.add_argument("--products", nargs="+", default=["BTC-USD", "ETH-USD"])
    f.add_argument("--start", default="2019-10-01")
    f.add_argument("--end", default="2025-06-01")
    f.add_argument("--out", type=Path, default=Path("data/external/prices"))
    c = sub.add_parser("classify", help="score the headlines with the local ONNX sentiment classifier (resumable)")
    r = sub.add_parser("run", help="run the event study and write the report")
    for p in (c, r):
        p.add_argument("--datasets", nargs="+", default=["dlt", "coindesk"], choices=["dlt", "coindesk"])
        p.add_argument("--news-dir", type=Path, default=Path("data/external/news"))
    c.add_argument("--model-dir", type=Path, default=Path("models/sentiment"))
    r.add_argument("--prices", type=Path, default=Path("data/external/prices"))
    r.add_argument("--out", type=Path, default=Path("data/reports/news_event_study.md"))
    r.add_argument("--n-boot", type=int, default=5000)
    r.add_argument("--n-draws", type=int, default=2000)
    args = ap.parse_args(argv)
    if args.cmd == "fetch-prices":
        args.out.mkdir(parents=True, exist_ok=True)
        with httpx.Client() as client:
            for product in args.products:
                df = fetch_candles(
                    product, pd.Timestamp(args.start, tz="UTC"), pd.Timestamp(args.end, tz="UTC"), client=client
                )
                df.to_parquet(args.out / f"{product}_5m.parquet")
                print(product, len(df), df.index.min(), df.index.max(), flush=True)
        return 0
    return _cmd_classify(args) if args.cmd == "classify" else _cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
