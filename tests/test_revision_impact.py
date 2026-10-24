"""Impact of REST revisions on states and decisions (spec 5.1.7 "Barre REST", R1-R4) on synthetic data.

The decisive cases: a late trade on an almost empty minute (the real family of the P0 soak, measured by
``script/soak_revision_*.py``) and the K15 construction of the revised world on the REAL rows of the store.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from dataclasses import replace as dc_replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from modules.config import AppConfig, GateOpsConfig, RiskConfig
from modules.decision.baseline import AnswerArrays
from modules.decision.policy import PolicyConfig
from modules.dryrun import revision_impact as ri
from modules.dryrun.revision_impact import (
    STATES_PER_DAY,
    ImpactDay,
    ImpactThresholds,
    PositionSpan,
    ShiftedState,
    actionable_mask,
    apply_revisions,
    block_status,
    calendar_verdict,
    day_blocks,
    impact_day,
    note_text,
    revised_world,
    revision_impact,
    rolling_max,
    verdict,
)
from modules.features.serializer import FLAT_COLUMNS
from modules.features.state_matrix import flat_matrix
from modules.market.backfill import BarStore, fill_flat_bars, load_bars
from modules.market.bars import OHLCV, classify_revisions
from modules.market.indicators_batch import compute_indicators
from modules.risk.costs import CostModel
from tests.indicator_fixtures import START, synthetic_bars
from tests.label_fixtures import FEES_REAL, loaded_label

DAYS = 36  # vol_z_1d exists from the 32nd day on
LABEL = loaded_label({"BTC-EUR": 3.0, "ETH-EUR": 3.0})
RISK = RiskConfig()
THR = ImpactThresholds(0.01, 0.005, 0.02, 0.001)
COL = {c: i for i, c in enumerate(FLAT_COLUMNS)}
NCOL = len(FLAT_COLUMNS)
T_REV = START + timedelta(days=DAYS - 2, minutes=96)  # 01:36 of the second to last day
REV_DAY = (START + timedelta(days=DAYS - 2)).date()
LAST_DAY = (START + timedelta(days=DAYS - 1)).date()
END = START + timedelta(days=DAYS) - timedelta(minutes=1)


def real_rows(seed: int) -> pd.DataFrame:
    """The rows a store would hold: only the minutes with trades (flat minutes have no row)."""
    bars = synthetic_bars(days=DAYS, seed=seed, zero_volume_day=None, constant_days=None)
    real = bars[bars["volume"] > 0].reset_index()[["ts", *OHLCV]]
    return real.assign(ts=real["ts"].astype("datetime64[ns, UTC]"))  # the dtype of the store


def late_trade(real: pd.DataFrame, t: datetime, *, quiet_min: int = 180) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(stored rows, value revisions): the minute ``t`` was published with volume 0.026, REST now has 2.205 and a close
    16 bps higher; the ``quiet_min`` minutes after it have no trades, so they are flat minutes in both worlds."""
    ts = pd.Timestamp(t)
    keep = ~((real["ts"] >= ts) & (real["ts"] <= ts + pd.Timedelta(minutes=quiet_min)))
    prev = float(real.loc[real["ts"] < ts, "close"].iloc[-1])
    stored_row = pd.DataFrame({"ts": [ts], "open": prev, "high": prev, "low": prev, "close": prev, "volume": 0.026})
    stored = pd.concat([real[keep], stored_row]).sort_values("ts").reset_index(drop=True)
    new = prev * 1.0016
    value = pd.DataFrame(
        {"open": [prev], "high": [new], "low": [prev], "close": [new], "volume": [2.205]},
        index=pd.DatetimeIndex([ts], name="ts"),
    )
    return stored, value


@dataclass
class ConstDecider:
    """Answers that do not depend on the state: nothing can shift."""

    p_edge: float = 0.4

    def answers_batch(self, x: np.ndarray) -> AnswerArrays:
        n = len(x)
        pa = np.tile([0.2, 0.1, 0.7], (n, 1))
        return AnswerArrays(pa, np.full(n, self.p_edge), np.zeros((n, 5)))


class SteepDecider:
    """``p_edge`` is a steep function of ``vol_z_1d`` and of ``dist_vwap_1d_bps``: both sum a whole day, so a model
    that is sensitive to them moves with one revised minute for 24 hours."""

    def answers_batch(self, x: np.ndarray) -> AnswerArrays:
        z = np.nan_to_num(x[:, COL["vol_z_1d"]], nan=0.0)
        vwap = np.nan_to_num(x[:, COL["dist_vwap_1d_bps"]], nan=0.0)
        p_edge = 1.0 / (1.0 + np.exp(-(4.0 * z + vwap / 10.0)))
        return AnswerArrays(np.tile([0.2, 0.1, 0.7], (len(x), 1)), p_edge, np.zeros((len(x), 5)))


class ColumnDecider:
    """Exact answers read from the first three columns: p_edge = x0, P(BUY) = x1, P(SELL) = x2 (HOLD the rest)."""

    def answers_batch(self, x: np.ndarray) -> AnswerArrays:
        pa = np.column_stack([x[:, 1], x[:, 2], 1.0 - x[:, 1] - x[:, 2]])
        return AnswerArrays(pa, x[:, 0].copy(), np.zeros((len(x), 5)))


class Recorder:
    """Keeps the matrices it is asked about (first call: served world, second: revised world)."""

    def __init__(self) -> None:
        self.seen: list[np.ndarray] = []

    def answers_batch(self, x: np.ndarray) -> AnswerArrays:
        self.seen.append(x.copy())
        return ConstDecider().answers_batch(x)


@pytest.fixture(scope="module")
def world() -> dict[str, Any]:
    """BTC and ETH, 12 days; ETH has the late trade on the last day at 01:36."""
    btc = real_rows(1)
    eth_stored, value = late_trade(real_rows(2), T_REV)
    s_btc, f_btc = revised_world(btc, btc.iloc[:0].set_index("ts"), None, END)
    s_eth, f_eth = revised_world(eth_stored, value, None, END)
    return {"btc": btc, "eth": eth_stored, "value": value, "S": {"BTC-EUR": s_btc, "ETH-EUR": s_eth},
            "F": {"BTC-EUR": f_btc, "ETH-EUR": f_eth}}  # fmt: skip


def run(w: dict[str, Any], decider: Any, policy: PolicyConfig | None = None) -> list[ImpactDay]:
    days = [REV_DAY, LAST_DAY]
    return revision_impact(w["S"], w["F"], days, decider, policy, LABEL, [], RISK, "v1", thresholds=THR)


# ----------------------------------------------------------------------------------- revised world (K15)


def test_apply_revisions_returns_copy_and_store_untouched(tmp_path: Path) -> None:
    real = real_rows(3)
    store = BarStore(tmp_path)
    store.write("BTC-EUR", real)
    files = sorted(tmp_path.rglob("*.parquet"))
    before = {f: hashlib.sha256(f.read_bytes()).hexdigest() for f in files}
    stored = pd.concat([store.read_month("BTC-EUR", *m) for m in store.months("BTC-EUR")])
    snapshot = stored.copy()
    t = real["ts"].iloc[500]
    value = pd.DataFrame({k: [real[k].iloc[500] * 1.01] for k in OHLCV}, index=pd.DatetimeIndex([t], name="ts"))
    out = apply_revisions(stored, value)
    assert out is not stored and out.loc[out["ts"] == t, "close"].iloc[0] == pytest.approx(
        real["close"].iloc[500] * 1.01
    )
    pd.testing.assert_frame_equal(stored, snapshot)  # the input is untouched
    revised_world(stored, value, None, END)
    assert {f: hashlib.sha256(f.read_bytes()).hexdigest() for f in files} == before  # I2: Parquet files unchanged
    assert sorted(tmp_path.rglob("*.parquet")) == files


def test_apply_revisions_ignores_appeared_and_disappeared() -> None:
    real = real_rows(3).iloc[:50]
    fresh = real.copy()
    fresh = fresh.drop(index=fresh.index[10])  # disappeared: in the store, not in REST
    fresh.loc[fresh.index[20], "close"] *= 1.001  # value
    extra = fresh.iloc[[0]].copy()
    extra["ts"] = real["ts"].iloc[-1] + pd.Timedelta(minutes=1)  # appeared: in REST, no row in the store
    fresh = pd.concat([fresh, extra]).reset_index(drop=True)
    rev = classify_revisions(
        real, fresh, real["ts"].iloc[0].to_pydatetime(), real["ts"].iloc[-1].to_pydatetime() + timedelta(hours=1)
    )
    assert len(rev.value) == 1 and len(rev.appeared) == 1 and len(rev.disappeared) == 1
    out = apply_revisions(real, rev.value)
    assert list(out["ts"]) == list(real["ts"])  # no row added, none removed
    changed = (out[list(OHLCV)] != real[list(OHLCV)]).any(axis=1)
    assert int(changed.sum()) == 1
    foreign = rev.value.copy()
    foreign.index = foreign.index + pd.Timedelta(days=400)
    with pytest.raises(KeyError):
        apply_revisions(real, foreign)


def test_flat_after_revised_minute_carries_revised_close() -> None:
    """K15: a real minute revised, followed by 3 minutes without trades. In F those 3 flat minutes carry the RE-READ
    close, and F is exactly ``fill_flat_bars`` of the re-read rows (not the old grid with one cell replaced)."""
    t0 = pd.Timestamp("2026-01-05 10:00", tz="UTC")
    ts = [t0 + pd.Timedelta(minutes=m) for m in (0, 1, 5)]  # minutes 2, 3, 4 have no trades
    real = pd.DataFrame({"ts": ts, "open": 100.0, "high": 100.0, "low": 100.0, "close": [100.0, 100.0, 101.0],
                         "volume": [1.0, 1.0, 1.0]})  # fmt: skip
    value = pd.DataFrame({"open": [100.0], "high": [100.5], "low": [100.0], "close": [100.5], "volume": [3.0]},
                         index=pd.DatetimeIndex([ts[1]], name="ts"))  # fmt: skip
    served, revised = revised_world(real, value, None, t0 + pd.Timedelta(minutes=5))
    assert served["close"].tolist() == [100.0, 100.0, 100.0, 100.0, 100.0, 101.0]
    assert revised["close"].tolist() == [100.0, 100.5, 100.5, 100.5, 100.5, 101.0]  # the 3 flat minutes follow
    assert revised["volume"].tolist() == [1.0, 3.0, 0.0, 0.0, 0.0, 1.0]
    rereads = real.copy()
    rereads.loc[1, ["high", "close", "volume"]] = [100.5, 100.5, 3.0]
    pd.testing.assert_frame_equal(revised, fill_flat_bars(rereads, None, t0 + pd.Timedelta(minutes=5)))
    naive = served.copy()  # the wrong construction: replace one cell of the filled grid
    naive.loc[ts[1], ["high", "close", "volume"]] = [100.5, 100.5, 3.0]
    assert naive["close"].tolist()[2] == 100.0 != revised["close"].tolist()[2]  # it would keep the old close


def test_revised_world_same_grid_as_served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = real_rows(4)
    store = BarStore(tmp_path)
    store.write("BTC-EUR", real)
    t = real["ts"].iloc[300]
    value = pd.DataFrame({k: [real[k].iloc[300] * 1.002] for k in OHLCV}, index=pd.DatetimeIndex([t], name="ts"))
    start, end = real["ts"].iloc[0].to_pydatetime(), real["ts"].iloc[-1].to_pydatetime()
    served, revised = revised_world(real, value, start, end)
    assert served.index.equals(revised.index)
    pd.testing.assert_frame_equal(served, load_bars(tmp_path, "BTC-EUR", start, end))  # S is what load_bars builds
    monkeypatch.setattr(ri, "apply_revisions", lambda r, v: r.iloc[5:])  # a grid that lost minutes
    with pytest.raises(ValueError, match="differs"):
        revised_world(real, value, start, end)


def test_late_trade_on_quiet_minute_moves_daily_tokens(world: dict[str, Any]) -> None:
    """The soak family: a trade comes late on an almost empty minute (volume 0.026 -> 2.205, close +16 bps). The VWAP of
    the day moves for the next 24 hours, ``vol_z_1d`` (the quote volume of each COMPLETE UTC day) moves for the whole
    following day, the returns only inside their window, and nothing moves before the revised minute."""
    s, f = world["S"]["ETH-EUR"], world["F"]["ETH-EUR"]
    ind_s, ind_f = compute_indicators(s), compute_indicators(f)
    rows = np.arange(len(s))
    xs = flat_matrix(ind_s, rows, "ETH-EUR", LABEL, None, None)
    xf = flat_matrix(ind_f, rows, "ETH-EUR", LABEL, None, None)
    differs = ~((xs == xf) | (np.isnan(xs) & np.isnan(xf)))
    t_bar = s.index.get_loc(pd.Timestamp(T_REV))
    last_day_start = (DAYS - 1) * 1440
    assert not differs[:t_bar].any()  # nothing before the revised minute
    za, zb = ind_s["vol_z_1d"].to_numpy(), ind_f["vol_z_1d"].to_numpy()  # raw: the token is rounded to 2 decimals
    raw_z = np.flatnonzero(~((za == zb) | (np.isnan(za) & np.isnan(zb))))
    assert raw_z.size == 1441 and raw_z[0] == last_day_start - 1  # the bar closing at 00:00 and the whole next day
    vwap = np.flatnonzero(differs[:, COL["dist_vwap_1d_bps"]])
    assert vwap.size > 150 and vwap[0] == t_bar and vwap[-1] <= t_bar + 1440  # the flat stretch, then the 24 hours
    ret15 = np.flatnonzero(differs[:, COL["ret_15m_bps"]])
    assert ret15.size > 0 and ret15[-1] <= t_bar + 180 + 15  # inside the flat stretch + the window
    assert not differs[:, COL["pos_is_long"]].any()  # FLAT states only


def test_insensitive_decider_passes_r1_r2(world: dict[str, Any]) -> None:
    days = run(world, ConstDecider())
    eth = [d for d in days if d.product == "ETH-EUR"]
    assert sum(d.states_any_token_diff for d in eth) > 0  # tokens did move
    assert all(d.shifted == 0 and d.max_shift == 0.0 for d in days)
    assert verdict(eth, THR)["R1"] == "pass" and verdict(eth, THR)["R2"] == "pass"


def test_sensitive_decider_fails_r1_with_low_ratio(world: dict[str, Any]) -> None:
    """One revised minute out of ~16.000 (ratio 0.006%, far below the old 0.5% gate) is enough to fail R1 with a model
    that is steep in the daily tokens: the old criterion would pass this, the new one does not (spec A.7 (ii))."""
    days = run(world, SteepDecider())
    eth = [d for d in days if d.product == "ETH-EUR"]
    v = verdict(eth, THR)
    assert v["R1"] == "fail" and v["R2"] == "fail"
    assert max(d.share for d in eth) > 0.05
    assert verdict([d for d in days if d.product == "BTC-EUR"], THR)["R1"] == "pass"  # BTC had no revision
    ratio = 1 / len(world["eth"])
    assert ratio < 0.005  # the old gate would have passed


# -------------------------------------------------------------------------------------- thresholds / R3


MIN = timedelta(minutes=1)
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def mkday(
    day: date, first: datetime, n: int, shifted_at: Any = (), *, version: str = "v", max_shift: float | None = None,
    flipped_at: Any = None, r3: str = "measured", origins: Any = None,
) -> ImpactDay:  # fmt: skip
    """A consistent ``ImpactDay`` of ``n`` contiguous states from ``first`` (close instants), built by hand."""
    shifted = [ShiftedState(t, 0.05, None if origins is None else origins.get(t)) for t in shifted_at]
    flips = None if flipped_at is None or r3 != "measured" else len(flipped_at)
    top = max_shift if max_shift is not None else (0.05 if shifted else 0.0)
    return ImpactDay(
        "BTC-EUR", day, version, n, n, 0, len(shifted), len(shifted) / n if n else 0.0, top, 0.0, 0.0, None, flips,
        None if flips is None else flips / n, r3, 0, {}, 0,  # type: ignore[arg-type]
        first if n else None, first + (n - 1) * MIN if n else None, tuple(shifted),
        tuple(flipped_at or ()) if r3 == "measured" else (), tuple(shifted[:5]),
    )  # fmt: skip


def series(spec: Any, start: datetime = T0, **kw: Any) -> list[ImpactDay]:
    """Contiguous days: ``spec`` = [(n, [offsets in minutes from the first state of THAT day of shifted states])]."""
    out, first = [], start
    for i, (n, offsets) in enumerate(spec):
        day = (start + timedelta(days=i)).date()
        out.append(mkday(day, first, n, [first + o * MIN for o in offsets], **kw))
        first = first + n * MIN
    return out


THR_SMALL = ImpactThresholds(0.01, 0.005, 0.02, 0.001, block_states=1000)


def test_block_full_day_is_the_day() -> None:
    (d,) = series([(1440, [10, 11])])
    (b,) = day_blocks([d], STATES_PER_DAY) or []
    assert (b.kind, b.states, b.start, b.end, b.shifted) == ("day", 1440, d.first_at, d.last_at, 2)
    assert b.share == pytest.approx(2 / 1440) and b.day == d.day and b.flips == 0 and b.r3 == "measured"


def test_block_partial_final_day_uses_last_block_states() -> None:
    full, part = series([(1440, [100, 400]), (300, [5])])
    blocks = day_blocks([full, part], STATES_PER_DAY)
    assert blocks is not None and [b.kind for b in blocks] == ["day", "extended_back"]
    back = blocks[1]
    assert back.end == part.last_at and back.start == part.last_at - 1439 * MIN and back.states == 1440
    # the block starts 1139 states into the full day: offset 100 is outside, offset 400 and the final day's 5 inside
    assert back.shifted == 2
    assert back.day == part.day


def test_block_partial_initial_day_uses_first_block_states() -> None:
    part, full = series([(300, [5]), (1440, [200, 1300])])
    blocks = day_blocks([part, full], STATES_PER_DAY)
    assert blocks is not None and [b.kind for b in blocks] == ["extended_forward", "day"]
    fwd = blocks[0]
    assert fwd.start == part.first_at and fwd.end == part.first_at + 1439 * MIN and fwd.states == 1440
    assert fwd.shifted == 1 + 1  # offset 5 of the partial day and offset 200 of the full day (state 500 of the series)


def test_block_bounds_are_inclusive() -> None:
    """A shifted state exactly at the first or at the last instant of a block belongs to it."""
    full, part = series([(1440, [0]), (300, [299])])  # the first state of the day-block, the last of the series
    first, last = day_blocks([full, part], STATES_PER_DAY) or []
    assert first.shifted == 1 and first.start == full.first_at  # offset 0 is the first instant of the day-block
    assert last.end == part.last_at and last.shifted == 1  # the last instant counts; offset 0 is before the block
    # the extended block starts at offset 300 of the full day: 300 is inside, 299 is not
    inside = series([(1440, [300]), (300, [])])
    outside = series([(1440, [299]), (300, [])])
    assert (day_blocks(inside, STATES_PER_DAY) or [])[1].shifted == 1
    assert (day_blocks(outside, STATES_PER_DAY) or [])[1].shifted == 0


def test_block_partial_day_with_few_states() -> None:
    """A final day of 3 states, 1 shifted: rule v1 judges 1/3 (fail); rule v2 judges 1/1440 on the extended block."""
    full, tiny = series([(1440, []), (3, [1])])
    assert calendar_verdict([full, tiny], ImpactThresholds(0.01, 0.005, 0.02, 0.001))["R1"] == "fail"
    assert verdict([full, tiny], ImpactThresholds(0.01, 0.005, 0.02, 0.001))["R1"] == "pass"
    (_, back) = day_blocks([full, tiny], STATES_PER_DAY) or []
    assert back.shifted == 1 and back.share == pytest.approx(1 / 1440)


def test_block_partial_day_can_fail_where_calendar_day_passes() -> None:
    """Symmetry: the v2 is not uniformly looser. A full day with 7 shifted states in its last hours passes (7/1440);
    the final partial day of 300 states with 1 shifted passes the v1 (floor(1.5) = 1) but its extended block holds 8."""
    thr = ImpactThresholds(0.01, 0.005, 0.02, 0.001)
    full, part = series([(1440, [1300, 1310, 1320, 1330, 1340, 1350, 1360]), (300, [10])])
    assert full.shifted == 7 and 7 / 1440 <= 0.005 and part.share == pytest.approx(1 / 300) and 1 / 300 <= 0.005
    assert calendar_verdict([full, part], thr)["R1"] == "pass"
    v2 = verdict([full, part], thr)
    assert v2["R1"] == "fail"
    assert (day_blocks([full, part], STATES_PER_DAY) or [])[1].shifted == 8


def test_overlapping_blocks_counted_per_block_not_summed() -> None:
    """The state at the overlap of the initial and the final block counts in BOTH blocks' own share, once in the
    distinct total of the window."""
    first, last = series(
        [(1000, [900]), (1000, [])]
    )  # 2000 states; blocks [0, 1439] and [560, 1999]: overlap 560..1439
    blocks = day_blocks([first, last], STATES_PER_DAY) or []
    assert [b.shifted for b in blocks] == [1, 1] and [b.share for b in blocks] == [1 / 1440, 1 / 1440]
    assert sum(d.shifted for d in (first, last)) == 1  # distinct states shifted in the window
    assert sum(b.shifted for b in blocks) == 2  # the blocks are not additive: never summed into a quota


def test_series_shorter_than_block_is_unmeasured() -> None:
    d = series([(1439, [5, 6, 7, 8, 9, 10, 11, 12, 13, 14])], max_shift=0.03)[0]  # 10 shifted: would fail anywhere
    thr = ImpactThresholds(0.01, 0.005, 0.02, 0.001)
    assert day_blocks([d], STATES_PER_DAY) is None
    assert verdict([d], thr) == {"R1": "unmeasured", "R2": "fail", "R3": "unmeasured"}  # R2 is still judged
    ok = series([(1439, [])])[0]
    assert verdict([ok], thr) == {"R1": "unmeasured", "R2": "pass", "R3": "unmeasured"}


def test_series_of_exactly_block_states_two_partial_days() -> None:
    a, b = series([(700, [3]), (740, [100])])
    blocks = day_blocks([a, b], STATES_PER_DAY) or []
    assert [x.kind for x in blocks] == ["extended_forward", "extended_back"]
    assert all(x.start == a.first_at and x.end == b.last_at and x.states == 1440 for x in blocks)
    assert [x.shifted for x in blocks] == [2, 2]


def test_noncontiguous_series_raises() -> None:
    a, b = series([(1440, []), (1440, [])])
    gap = mkday(b.day, b.first_at + 5 * MIN, 1440)  # a hole of 5 minutes between the two days
    with pytest.raises(ValueError, match="contiguous"):
        day_blocks([a, gap], STATES_PER_DAY)
    with pytest.raises(ValueError, match="contiguous"):
        verdict([a, gap], ImpactThresholds(0.01, 0.005, 0.02, 0.001))
    short = dc_replace(a, states=1000)  # states do not match the instants
    with pytest.raises(ValueError, match="contiguous"):
        day_blocks([short], 10)
    bare = dc_replace(a, first_at=None, last_at=None)
    with pytest.raises(ValueError, match="first_at"):
        day_blocks([bare], 10)


def test_verdict_fail_precedes_unmeasured() -> None:
    thr = ImpactThresholds(0.01, 0.005, 0.02, 0.001)
    bad = series([(1440, list(range(20)))], version="a")  # 20/1440: R1 fails for version a
    short = series([(100, [])], version="b")  # version b has no block
    assert verdict(bad + short, thr)["R1"] == "fail"
    good = series([(1440, [])], version="a")
    assert verdict(good + short, thr)["R1"] == "unmeasured"  # unmeasured beats pass
    assert verdict(good, thr)["R1"] == "pass"


def test_r2_and_r4_do_not_depend_on_grouping() -> None:
    days = series([(1440, [3]), (300, [4])], max_shift=0.0)
    days[1] = dc_replace(days[1], max_shift=0.021)
    for block_states in (1440, 500, 100, 1):
        assert verdict(days, ImpactThresholds(0.01, 0.005, 0.02, 0.001, block_states))["R2"] == "fail"
    days[1] = dc_replace(days[1], max_shift=0.02)
    assert verdict(days, ImpactThresholds(0.01, 0.005, 0.02, 0.001, 1000))["R2"] == "pass"
    # the day that raises R2 is judged by its own maximum even if it is only an extension of the block
    only_first = verdict(days, ImpactThresholds(0.01, 0.005, 0.02, 0.001), judged={days[0].day})
    assert only_first["R2"] == "pass"


def test_r3_flips_counted_on_blocks() -> None:
    thr = ImpactThresholds(0.01, 0.005, 0.02, 0.001)
    full, part = series([(1440, []), (300, [])], flipped_at=[])
    one = dc_replace(full, flips=1, flip_share=1 / 1440, flipped_at=(full.first_at + 1000 * MIN,))
    assert verdict([one, part], thr)["R3"] == "pass"  # 1 flip in 1440 states is the budget ("at most 1 per day")
    two = dc_replace(part, flips=1, flip_share=1 / 300, flipped_at=(part.first_at + 10 * MIN,))
    assert verdict([one, two], thr)["R3"] == "fail"  # the extended block holds both: 2/1440 = 0.139%
    blocks = day_blocks([one, two], STATES_PER_DAY) or []
    assert [b.flips for b in blocks] == [1, 2] and blocks[1].flip_share == pytest.approx(2 / 1440)
    untunable = dc_replace(part, r3="policy_untunable", flips=None, flip_share=None, flipped_at=())
    b2 = (day_blocks([one, untunable], STATES_PER_DAY) or [])[1]
    assert b2.r3 == "policy_untunable" and b2.flips is None  # a block touching an unmeasured day is unmeasured
    assert verdict([one, untunable], thr)["R3"] == "unmeasured"


@settings(max_examples=60, deadline=None)
@given(st.lists(st.tuples(st.integers(0, 12), st.integers(0, 3)), min_size=1, max_size=4))
def test_full_days_same_verdict_as_calendar_rule(spec: list[tuple[int, int]]) -> None:
    """In production D-1 is a full day: for any series of FULL days the rule v2 gives the share and the verdict of the
    calendar rule v1, so the production gate does not change."""
    thr = ImpactThresholds(0.01, 0.005, 0.02, 0.001)
    days = series([(1440, list(range(k))) for k, _ in spec])
    flips = [dc_replace(d, flips=f, flip_share=f / 1440, flipped_at=tuple(d.first_at + i * MIN for i in range(f)))
             for d, (_, f) in zip(days, spec, strict=True) if d.first_at]  # fmt: skip
    v1, v2 = calendar_verdict(flips, thr), verdict(flips, thr)
    assert (v1["R1"], v1["R3"]) == (v2["R1"], v2["R3"])
    blocks = day_blocks(flips, STATES_PER_DAY) or []
    assert [b.share for b in blocks] == [d.share for d in flips] and {b.kind for b in blocks} == {"day"}


@pytest.mark.parametrize("disposition", ["before", "after", "mixed"])
def test_eth_soak_shape_synthetic(disposition: str) -> None:
    """The ETH-EUR shape of the P0 soak: 1096 + 1440 + 343 states with the real instants; 2 shifted on 10-20 at
    02:33-02:34; 5 on 10-19 placed before 05:43, after it, or both. v1 fails on 10-20, v2 passes in every case."""
    thr = ImpactThresholds(0.01, 0.005, 0.02, 0.001)
    s0 = datetime(2026, 10, 18, 5, 44, tzinfo=UTC)
    d19, d20 = datetime(2026, 10, 19, tzinfo=UTC), datetime(2026, 10, 20, tzinfo=UTC)
    times19 = {"before": [1, 2, 3, 4, 5], "after": [400, 401, 402, 403, 404], "mixed": [1, 2, 400, 401, 402]}[
        disposition
    ]
    days = [
        mkday(date(2026, 10, 18), s0, 1096),
        mkday(date(2026, 10, 19), d19, 1440, [d19 + m * MIN for m in times19]),
        mkday(date(2026, 10, 20), d20, 343, [d20 + 153 * MIN, d20 + 154 * MIN]),
    ]
    assert calendar_verdict(days, thr)["R1"] == "fail" and days[2].share == pytest.approx(2 / 343)
    assert verdict(days, thr)["R1"] == "pass"
    blocks = day_blocks(days, STATES_PER_DAY) or []
    assert [b.kind for b in blocks] == ["extended_forward", "day", "extended_back"]
    assert blocks[2].start == datetime(2026, 10, 19, 5, 43, tzinfo=UTC) and blocks[2].end == days[2].last_at
    assert max(b.shifted for b in blocks) <= 7
    worse = dc_replace(days[2], shifted=3, share=3 / 343,
                       shifted_states=days[2].shifted_states + (ShiftedState(d20 + 200 * MIN, 0.05),))  # fmt: skip
    if disposition == "after":  # the 5 states of 10-19 are inside the final block: 5 + 2 + 1 = 8 > 7
        assert verdict([*days[:2], worse], thr)["R1"] == "fail"


def test_impact_day_records_timestamps_and_shifted_states() -> None:
    x_s, x_f = np.zeros((5, NCOL)), np.zeros((5, NCOL))
    x_s[:, 0] = 0.5
    x_f[:, 0] = [0.5, 0.75, 0.25, np.nan, 0.5 + 2**-3]  # shifts 0, 0.25, 0.25, inf, 0.125
    ts = pd.date_range("2026-01-01", periods=5, freq="1min", tz="UTC")
    d = impact_day(product="BTC-EUR", day=date(2026, 1, 1), version="v", x_served=x_s, x_revised=x_f,
                   is_long=np.zeros(5, dtype=bool), decider=ColumnDecider(), policy=None, label=LABEL, risk=RISK,
                   thresholds=ImpactThresholds(0.1, 0.5, 0.5, 0.5), t_close=ts.to_numpy())  # fmt: skip
    assert d.first_at == ts[0].to_pydatetime() and d.last_at == ts[4].to_pydatetime()
    assert d.shifted == 4 and len(d.shifted_states) == d.shifted
    assert [x.t for x in d.shifted_states] == [ts[i].to_pydatetime() for i in (1, 2, 3, 4)]
    assert [x.m for x in d.top_shifts][0] == math.inf  # infinity is kept, not clipped, in memory
    assert [x.t for x in d.top_shifts] == [
        ts[3].to_pydatetime(),
        ts[1].to_pydatetime(),
        ts[2].to_pydatetime(),
        ts[4].to_pydatetime(),
    ]  # m descending, ties by t ascending
    doc = d.to_dict()
    assert doc["top_shifts"][0]["m"] == 1.0 and doc["first_at"] == "2026-01-01T00:00:00Z"
    json.dumps(doc, allow_nan=False)
    no_t = impact_day(product="BTC-EUR", day=date(2026, 1, 1), version="v", x_served=x_s, x_revised=x_f,
                      is_long=np.zeros(5, dtype=bool), decider=ColumnDecider(), policy=None, label=LABEL, risk=RISK,
                      thresholds=ImpactThresholds(0.1, 0.5, 0.5, 0.5))  # fmt: skip
    assert no_t.first_at is None and no_t.shifted_states == ()  # without t_close the fields stay at their defaults


def test_origin_is_latest_revision_at_or_before_state() -> None:
    minutes = {"ETH-EUR": pd.DatetimeIndex([T0 + 10 * MIN, T0 + 50 * MIN])}
    at = lambda m: ri._origin("ETH-EUR", None, minutes, T0 + m * MIN)  # noqa: E731
    assert at(5) is None  # no revision yet
    assert at(11) == "ETH-EUR 2026-01-01T00:10Z"  # the bar of the state (t - 1 min) is the revised minute: included
    assert at(10) is None  # revised minute == t is the bar AFTER the state: not yet
    assert at(49) == "ETH-EUR 2026-01-01T00:10Z" and at(51) == "ETH-EUR 2026-01-01T00:50Z"


def test_origin_uses_btc_revisions_for_eth() -> None:
    minutes = {"ETH-EUR": pd.DatetimeIndex([T0 + 10 * MIN]), "BTC-EUR": pd.DatetimeIndex([T0 + 30 * MIN])}
    assert ri._origin("ETH-EUR", "BTC-EUR", minutes, T0 + 40 * MIN) == "BTC-EUR 2026-01-01T00:30Z"
    assert ri._origin("BTC-EUR", "BTC-EUR", minutes, T0 + 40 * MIN) == "BTC-EUR 2026-01-01T00:30Z"
    tie = {"ETH-EUR": pd.DatetimeIndex([T0 + 30 * MIN]), "BTC-EUR": pd.DatetimeIndex([T0 + 30 * MIN])}
    assert ri._origin("ETH-EUR", "BTC-EUR", tie, T0 + 40 * MIN) == "ETH-EUR 2026-01-01T00:30Z"  # a tie: the product


def test_clusters_and_max_cluster_in_block() -> None:
    origins = {}
    d = series([(1440, [1, 2, 3, 4, 5, 6])])[0]
    labels = ["A", "A", "A", "B", None, None]
    for st_, label in zip(d.shifted_states, labels, strict=True):
        origins[st_.t] = label
    d = mkday(d.day, d.first_at or T0, 1440, [x.t for x in d.shifted_states], origins=origins)
    (b,) = day_blocks([d], STATES_PER_DAY) or []
    assert b.shifted == 6 and b.revisions == 4 and b.max_cluster == 3  # A, B and one cluster per unknown origin
    (none,) = day_blocks(series([(1440, [])]), STATES_PER_DAY) or []
    assert (none.revisions, none.max_cluster) == (0, 0)


def test_rolling_max_over_contiguous_series() -> None:
    d1, d2 = series([(1440, [1000, 1001]), (1440, [10, 11, 12])])
    spans = [(d.first_at, d.last_at, [x.t for x in d.shifted_states]) for d in (d1, d2)]
    r = rolling_max(spans, 1440)
    assert r is not None and r["shifted"] == 5 and r["share"] == pytest.approx(5 / 1440)  # the window across midnight
    assert r["start"] == d1.first_at + 13 * MIN and r["end"] == r["start"] + 1439 * MIN  # the FIRST best window
    assert rolling_max(spans[:1], 1441) is None and rolling_max([], 10) is None
    with pytest.raises(ValueError, match="contiguous"):
        rolling_max([spans[0], (d2.first_at + MIN, d2.last_at, [])], 1440)


# ---- thresholds


def test_thresholds_boundaries() -> None:
    thr = THR_SMALL  # blocks of 1000 states: 5 shifted pass (0.005), 6 fail
    five = series([(1000, list(range(5)))])
    six = series([(1000, list(range(6)))])
    assert verdict(five, thr)["R1"] == "pass" and verdict(six, thr)["R1"] == "fail"
    # the maximum: 0.02 passes, 0.0200001 fails, inf fails
    assert verdict(series([(1000, [])], max_shift=0.02), thr)["R2"] == "pass"
    assert verdict(series([(1000, [])], max_shift=0.0200001), thr)["R2"] == "fail"
    assert verdict(series([(1000, [])], max_shift=math.inf), thr)["R2"] == "fail"
    # flips: 1 in 1000 passes (0.001), 2 fail
    one, two = series([(1000, [])], flipped_at=[T0]), series([(1000, [])], flipped_at=[T0, T0 + MIN])
    assert verdict(one, thr)["R3"] == "pass" and verdict(two, thr)["R3"] == "fail"
    assert verdict([], thr) == {"R1": "unmeasured", "R2": "unmeasured", "R3": "unmeasured"}
    bl = (day_blocks(five, 1000) or [])[0]
    assert block_status(bl, thr) == {"R1": "pass", "R3": "pass"}  # measured, no flip
    untunable = dc_replace(bl, r3="policy_untunable", flips=None, flip_share=None)
    assert block_status(untunable, thr) == {"R1": "pass", "R3": "unmeasured"}
    with pytest.raises(ValueError, match="block_states"):
        ImpactThresholds(0.01, 0.005, 0.02, 0.001, 0)
    assert ImpactThresholds(0.01, 0.005, 0.02, 0.001).block_states == STATES_PER_DAY
    # m = shift exactly is NOT shifted (strict), with binary exact numbers
    x_s = np.zeros((4, NCOL))
    x_f = np.zeros((4, NCOL))
    x_s[:, 0], x_f[:, 0] = 0.5, [0.75, 0.75 + 2**-20, 0.5, 0.25]  # shifts 0.25, 0.25+eps, 0, 0.25
    d = impact_day(product="BTC-EUR", day=date(2026, 1, 1), version="v", x_served=x_s, x_revised=x_f,
                   is_long=np.zeros(4, dtype=bool), decider=ColumnDecider(), policy=None, label=LABEL, risk=RISK,
                   thresholds=ImpactThresholds(0.25, 0.5, 0.5, 0.5))  # fmt: skip
    assert d.shifted == 1 and d.share == 0.25 and d.max_shift == pytest.approx(0.25 + 2**-20)


def test_r3_unmeasured_with_untunable_policy() -> None:
    x = np.zeros((3, NCOL))
    x[:, 0] = 0.5
    thr = ImpactThresholds(0.01, 0.005, 0.02, 0.001, block_states=3)
    t = pd.date_range("2026-01-01", periods=3, freq="1min", tz="UTC").to_numpy()
    d = impact_day(product="BTC-EUR", day=date(2026, 1, 1), version="v", x_served=x, x_revised=x.copy(),
                   is_long=np.zeros(3, dtype=bool), decider=ColumnDecider(), policy=None, label=LABEL, risk=RISK,
                   thresholds=thr, t_close=t)  # fmt: skip
    assert d.r3 == "policy_untunable" and d.flips is None and d.flip_share is None
    assert verdict([d], thr) == {"R1": "pass", "R2": "pass", "R3": "unmeasured"}
    nobatch = impact_day(product="BTC-EUR", day=date(2026, 1, 1), version="v", x_served=x, x_revised=x.copy(),
                         is_long=np.zeros(3, dtype=bool), decider=object(), policy=None, label=LABEL, risk=RISK,
                         thresholds=thr, t_close=t)  # fmt: skip
    assert nobatch.r3 == "decider_without_batch" and nobatch.first_at is not None  # the instants are filled everywhere
    assert verdict([nobatch], thr) == {"R1": "unmeasured", "R2": "unmeasured", "R3": "unmeasured"}


def policy(**kw: Any) -> PolicyConfig:
    base: dict[str, Any] = {"model": "m", "tau_buy": 0.5, "tau_sell": 0.5, "c_min": 0.0, "p_be_buffer": 0.0,
                            "regime_rule": "ATR1D_PCTILE365:5/35/65/95",
                            "label_config_sha256": LABEL.sha256}  # fmt: skip
    return PolicyConfig(**(base | kw))


def entry_rows(n: int) -> np.ndarray:
    """FLAT rows where a BUY is plausible: regime 2, TP 300 bps and SL 150 bps of ATR 100, round-trip costs 50 / 70."""
    x = np.zeros((n, NCOL))
    x[:, COL["atr_14_6h_bps"]] = 100.0
    x[:, COL["cost_tp_bps"]], x[:, COL["cost_sl_bps"]] = 50.0, 70.0
    x[:, COL["atr_1d_pctile_365d"]] = 50.0
    x[:, 1] = 0.6  # P(BUY)
    return x


def test_flip_counted_when_actionability_differs() -> None:
    # FLAT at the edge: breakeven = (150 + 70) / (250 + 220) = 0.4681
    x_s, x_f = entry_rows(3), entry_rows(3)
    x_s[:, 0], x_f[:, 0] = [0.48, 0.48, 0.40], [0.45, 0.48, 0.40]  # row 0 crosses the breakeven, rows 1 and 2 do not
    d = impact_day(product="BTC-EUR", day=date(2026, 1, 1), version="v", x_served=x_s, x_revised=x_f,
                   is_long=np.zeros(3, dtype=bool), decider=ColumnDecider(), policy=policy(), label=LABEL, risk=RISK,
                   thresholds=THR)  # fmt: skip
    assert d.r3 == "measured" and d.flips == 1 and d.flip_share == pytest.approx(1 / 3)
    # LONG at the edge of sell_mask: masked P(SELL) = x2 / (x2 + HOLD) with BUY ignored
    y_s, y_f = np.zeros((2, NCOL)), np.zeros((2, NCOL))
    y_s[:, 1], y_f[:, 1] = 0.0, 0.0
    y_s[:, 2], y_f[:, 2] = [0.5, 0.25], [0.4375, 0.25]  # row 0: 0.5 >= tau_sell, then 0.4375 < tau_sell
    y_s[:, 0] = y_f[:, 0] = 0.5
    dl = impact_day(product="BTC-EUR", day=date(2026, 1, 1), version="v", x_served=y_s, x_revised=y_f,
                    is_long=np.ones(2, dtype=bool), decider=ColumnDecider(), policy=policy(), label=LABEL, risk=RISK,
                    thresholds=THR)  # fmt: skip
    assert dl.long_states == 2 and dl.flips == 1 and dl.p99_ds is not None
    assert dl.max_shift == pytest.approx(0.0625)


def test_actionable_mask_matches_risk_scalar() -> None:
    """The vector EV gate is the twin of the RiskManager formulas (CostModel) on the same numbers."""
    rng = np.random.default_rng(5)
    n = 400
    atr = rng.uniform(20, 400, n)
    spread = rng.uniform(0, 30, n)
    p_edge = rng.uniform(0.2, 0.7, n)
    buffer = 0.02
    x = np.zeros((n, NCOL))
    x[:, COL["atr_14_6h_bps"]] = atr
    x[:, COL["atr_1d_pctile_365d"]] = 50.0
    models = [CostModel(fees=FEES_REAL, entry_mode="maker", spread_bps=float(s)) for s in spread]
    x[:, COL["cost_tp_bps"]] = [m.cost_tp for m in models]
    x[:, COL["cost_sl_bps"]] = [m.cost_sl for m in models]
    p_action = np.tile([0.6, 0.1, 0.3], (n, 1))
    got = actionable_mask(x, p_action, p_edge, np.zeros(n, dtype=bool), policy(p_be_buffer=buffer), LABEL, RISK)
    cfg = LABEL.cfg
    want = []
    for m, a, p in zip(models, atr, p_edge, strict=True):
        tp, sl = cfg.k_tp * a, cfg.k_sl * a
        ok = m.passes_fee_floor(tp, RISK.min_net_edge_bps)
        ev = m.ev_bps(float(p), tp, sl)
        win, loss = m.tp_net_bps(tp), m.sl_loss_bps(sl)
        want.append(ok and ev > RISK.ev_min_bps and not ev < buffer * (win + loss))
    assert got.tolist() == want and 0 < sum(want) < n  # the comparison is not vacuous


def test_fee_floor_and_ev_gate_are_inclusive_where_the_risk_manager_is() -> None:
    """Fee floor ``W >= min_net_edge_bps`` is inclusive; the EV buffer ``EV >= p_be_buffer * (W + L)`` bites."""
    x = entry_rows(3)
    x[:, COL["cost_tp_bps"]] = [270.0, 270.0001, 270.0]  # W = 3 * 100 - cost = 30.0 (the floor), just under it, 30.0
    x[:, COL["cost_sl_bps"]] = [70.0, 70.0, 70.0]  # L = 150 + 70 = 220
    x[:, 0] = [0.95, 0.95, 0.95]
    p_action = np.tile([0.6, 0.1, 0.3], (3, 1))
    no_long = np.zeros(3, dtype=bool)
    got = actionable_mask(x, p_action, x[:, 0], no_long, policy(), LABEL, RISK)
    assert got.tolist() == [True, False, True]
    # W = 30, L = 220, buffer 0.1 needs EV >= 25, that is p >= 245 / 250 = 0.98
    buffered = policy(p_be_buffer=0.1)
    above = actionable_mask(x[:1], p_action[:1], np.array([0.981]), no_long[:1], buffered, LABEL, RISK)
    below = actionable_mask(x[:1], p_action[:1], np.array([0.979]), no_long[:1], buffered, LABEL, RISK)
    assert above.tolist() == [True] and below.tolist() == [False]


T3 = pd.date_range("2026-01-01", periods=3, freq="1min", tz="UTC").to_numpy()


def test_nan_on_one_side_is_infinite_shift() -> None:
    x_s, x_f = np.full((3, NCOL), 0.5), np.full((3, NCOL), 0.5)
    x_s[0, 0] = np.nan  # NaN in one world only
    x_s[1, 0] = x_f[1, 0] = np.nan  # NaN in both
    d = impact_day(product="BTC-EUR", day=date(2026, 1, 1), version="v", x_served=x_s, x_revised=x_f,
                   is_long=np.zeros(3, dtype=bool), decider=ColumnDecider(), policy=None, label=LABEL, risk=RISK,
                   thresholds=THR, t_close=T3)  # fmt: skip
    assert math.isinf(d.max_shift) and d.shifted == 1
    assert d.states_nan_both == 1
    assert verdict([d], ImpactThresholds(0.01, 0.005, 0.02, 0.001, block_states=3))["R2"] == "fail"
    assert d.to_dict()["max_shift"] == 1.0  # +inf is written as the largest possible shift
    json.dumps(d.to_dict(), allow_nan=False)


def test_btc_context_from_same_world() -> None:
    """The BTC tokens of an ETH state come from the BTC indicators of the SAME world: a revision of BTC alone shows
    up in the ETH states of F (a decider that reads ``btc_ret_1d_bps``)."""
    btc_stored, btc_value = late_trade(real_rows(1), T_REV, quiet_min=400)
    btc_value = btc_value.assign(close=btc_value["close"] * 1.01, high=btc_value["high"] * 1.01)  # a bigger move
    eth = real_rows(2)
    s_b, f_b = revised_world(btc_stored, btc_value, None, END)
    s_e, f_e = revised_world(eth, eth.iloc[:0].set_index("ts"), None, END)  # ETH itself is not revised

    class BtcDecider:
        def answers_batch(self, x: np.ndarray) -> AnswerArrays:
            z = np.nan_to_num(x[:, COL["btc_ret_1d_bps"]], nan=0.0) / 100.0
            return AnswerArrays(np.tile([0.2, 0.1, 0.7], (len(x), 1)), 1 / (1 + np.exp(-z)), np.zeros((len(x), 5)))

    out = revision_impact({"BTC-EUR": s_b, "ETH-EUR": s_e}, {"BTC-EUR": f_b, "ETH-EUR": f_e}, [LAST_DAY],
                          BtcDecider(), None, LABEL, [], RISK, "v", thresholds=THR)  # fmt: skip
    eth_day = next(d for d in out if d.product == "ETH-EUR")
    assert eth_day.token_diff_by_column.get("btc_ret_1d_bps", 0) > 0 and eth_day.max_shift > 0


def test_long_states_use_position_numbers_and_world_close(world: dict[str, Any]) -> None:
    rec = Recorder()
    opened = pd.Timestamp(T_REV) - pd.Timedelta(hours=1)
    closed = (pd.Timestamp(T_REV) + pd.Timedelta(hours=2)).to_pydatetime()
    span = PositionSpan("ETH-EUR", opened.to_pydatetime(), closed,
                        entry_px=100.0, stop_loss=95.0, take_profit=110.0, atr_entry=2.0)  # fmt: skip
    out = revision_impact(world["S"], world["F"], [REV_DAY], rec, None, LABEL, [span], RISK, "v", thresholds=THR)
    eth = next(d for d in out if d.product == "ETH-EUR")
    assert eth.long_states == 180 and eth.flat_states == eth.states - 180
    # the recorder saw the ETH matrices of S then F (BTC comes first, in dict order)
    xs, xf = rec.seen[2], rec.seen[3]
    is_long = xs[:, COL["pos_is_long"]] == 1.0
    assert int(is_long.sum()) == 180 and (xf[:, COL["pos_is_long"]] == 1.0).sum() == 180
    s_close, f_close = world["S"]["ETH-EUR"]["close"], world["F"]["ETH-EUR"]["close"]
    day_start = pd.Timestamp(REV_DAY, tz="UTC")
    mask = (s_close.index + pd.Timedelta(minutes=1) >= day_start) & (
        s_close.index + pd.Timedelta(minutes=1) < day_start + pd.Timedelta(days=1)
    )
    sc, fc = s_close.to_numpy()[mask][is_long], f_close.to_numpy()[mask][is_long]
    np.testing.assert_array_equal(
        xs[is_long, COL["pos_unreal_bps"]], np.rint(1e4 * (sc / 100.0 - 1.0)).clip(-5000, 5000)
    )
    np.testing.assert_array_equal(
        xf[is_long, COL["pos_unreal_bps"]], np.rint(1e4 * (fc / 100.0 - 1.0)).clip(-5000, 5000)
    )
    assert (xs[is_long, COL["pos_unreal_bps"]] != xf[is_long, COL["pos_unreal_bps"]]).any()  # each world its own close
    assert (xs[~is_long, COL["pos_is_long"]] == 0.0).all()


# --------------------------------------------------------------------------------------------- certify


def candles(df: pd.DataFrame, lo: int, hi: int) -> list[dict[str, str]]:
    sel = df[(df["ts"] >= pd.Timestamp(lo, unit="s", tz="UTC")) & (df["ts"] < pd.Timestamp(hi, unit="s", tz="UTC"))]
    return [{"start": str(int(r.ts.timestamp())), **{k: repr(float(getattr(r, k))) for k in OHLCV}}
            for r in sel.itertuples()]  # fmt: skip


class FakeSource:
    """REST stand-in. Serves one frame per product, END exclusive, and records the requests."""

    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        self.frames, self.calls = frames, []  # type: ignore[var-annotated]

    def fetch(self, product_id: str, start_s: int, end_s: int) -> list[dict[str, Any]]:
        self.calls.append((product_id, start_s, end_s))
        return candles(self.frames[product_id], start_s, end_s)


@pytest.fixture
def cert_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A store with two products (ETH has the late trade), a soak report over the last 30 hours of it, a fake model."""
    root = tmp_path / "bars" / "1m"
    btc = real_rows(1)
    eth_stored, value = late_trade(real_rows(2), T_REV)
    store = BarStore(root)
    store.write("BTC-EUR", btc)
    store.write("ETH-EUR", eth_stored)
    fresh_eth = eth_stored.copy()
    fresh_eth.loc[fresh_eth["ts"] == pd.Timestamp(T_REV), list(OHLCV)] = value.iloc[0][list(OHLCV)].to_numpy()
    started = START + timedelta(days=DAYS - 2)
    updated = START + timedelta(days=DAYS) - timedelta(minutes=1)
    soak = tmp_path / "p0_soak.json"
    soak.write_text(json.dumps({
        "started_at": started.isoformat(), "updated_at": updated.isoformat(), "products": ["BTC-EUR", "ETH-EUR"],
        "verdict": {"rest_revision_ratio_within_limit": False},
        "checks": {"rest_revisions": {"ETH-EUR": {"minutes": 1, "revised": 1, "ratio": 1.0,
                                                  "max_close_diff_bps": 16.0}}},
    }), encoding="utf-8")  # fmt: skip
    model = tmp_path / "model"
    (model / "final").mkdir(parents=True)
    (model / "model_card.json").write_text(json.dumps({"model": "stub@1"}), encoding="utf-8")
    (model / "final" / "policy.json").write_text(json.dumps({"untunable": True, "reason": "untunable_ev"}), "utf-8")
    holder: dict[str, Any] = {"decider": ConstDecider()}

    from modules.decision.baseline import BaselineDecider

    monkeypatch.setattr(BaselineDecider, "load", classmethod(lambda cls, *a, **k: holder["decider"]))
    import modules.risk.label_config as lc

    monkeypatch.setattr(lc, "load_label_config", lambda path: LABEL)
    return {"root": root, "soak": soak, "model": model, "out": tmp_path / "out" / "cert.json", "holder": holder,
            "source": FakeSource({"BTC-EUR": btc, "ETH-EUR": fresh_eth}), "tmp": tmp_path}  # fmt: skip


def argv(rig: dict[str, Any], **over: str) -> list[str]:
    a = {"--soak-report": str(rig["soak"]), "--model-dir": str(rig["model"]), "--out": str(rig["out"]),
         "--bars-root": str(rig["root"]), "--label-config": str(rig["tmp"] / "label.json")} | over  # fmt: skip
    return ["certify", *[x for kv in a.items() for x in kv]]


def fresh_out(rig: dict[str, Any], name: str) -> str:
    return str(rig["tmp"] / "out" / name)


def test_certify_writes_schema_and_never_writes_soak_report(cert_rig: dict[str, Any]) -> None:
    rig = cert_rig
    soak_before = hashlib.sha256(rig["soak"].read_bytes()).hexdigest()
    files = sorted(rig["root"].rglob("*.parquet"))
    store_before = {f: hashlib.sha256(f.read_bytes()).hexdigest() for f in files}
    code = ri.main(argv(rig), source=rig["source"])
    assert code == 0
    assert hashlib.sha256(rig["soak"].read_bytes()).hexdigest() == soak_before  # the soak report is never opened
    assert {f: hashlib.sha256(f.read_bytes()).hexdigest() for f in files} == store_before  # nor the store
    doc = json.loads(rig["out"].read_text(encoding="utf-8"))
    assert doc["v"] == "chronos.p0_bars_certification.v2" and doc["p0_bars_criterion"] == "pass"
    assert doc["p0_bars_criterion_rule_v1"] == "pass" and doc["previous_run"] is None
    assert doc["soak_report"]["sha256"] == soak_before  # the link goes from the certification to the soak
    assert doc["soak_report"]["verdict_rest_revision_ratio_within_limit"] is False
    assert doc["criterion"]["decided_on"] == "2026-10-20" and doc["criterion"]["superseded"].startswith(
        "rest_revision_ratio"
    )
    unit = doc["criterion"]["r1_r3_unit"]
    assert unit["version"] == "v2" and unit["block_states"] == 1440 and unit["fixed_after_first_run"] is True
    assert doc["thresholds"] == {"shift": 0.01, "max_share": 0.005, "max_shift": 0.02, "max_flip_share": 0.001,
                                 "block_states": 1440}  # fmt: skip
    assert doc["model"]["version"] == "stub@1" and doc["model"]["policy"] == "untunable"
    assert doc["window"]["min_age_min"] == 15 and set(doc["products"]) == {"BTC-EUR", "ETH-EUR"}
    eth = doc["products"]["ETH-EUR"]
    assert eth["diagnostic"]["revised"] == 1 and eth["diagnostic"]["value_revised"] == 1
    assert eth["R4"] == {"appeared": 0, "disappeared": 0, "status": "pass"}
    assert (eth["R1"], eth["R2"], eth["R3"]) == ("pass", "pass", "unmeasured")
    assert [b["kind"] for b in eth["blocks"]] == [
        "extended_forward",
        "day",
    ]  # 1439 states on the first day (t = ts + 1 min)
    assert eth["days"] and all(d["R3"] == "unmeasured" and d["R3_note"] == "policy_untunable" for d in eth["days"])
    assert all(d["block"] is not None and "shifted_states" in d and "top_shifts" in d for d in eth["days"])
    diag = eth["diagnostic"]
    assert diag["r1_window"]["states"] == eth["states"] and diag["r1_rolling_max"]["share"] == 0.0
    assert [r["day"] for r in diag["r1_calendar_days_v1"]] == [d["day"] for d in eth["days"]]
    assert diag["revisions_independent"] == 0 and diag["r2_utilization"] == 0.0 and diag["top_shifts"] == []
    # the re-read candles are saved with their sha256 next to the certification
    saved = rig["out"].parent / "cert.reread" / "ETH-EUR.parquet"
    assert saved.exists() and diag["reread"]["sha256"] == hashlib.sha256(saved.read_bytes()).hexdigest()
    assert len(pd.read_parquet(saved)) > 0 and diag["reread_vs_previous"] is None
    assert doc["r3_deferred_to"].startswith("G-DRY-OPS")
    # reads only: REST was asked, in chunks of at most 300 minutes, END exclusive
    assert rig["source"].calls and all(e - s <= 300 * 60 for _, s, e in rig["source"].calls)


def test_certify_refuses_to_overwrite_existing_out(cert_rig: dict[str, Any]) -> None:
    rig = cert_rig
    assert ri.main(argv(rig), source=rig["source"]) == 0
    first = rig["out"].read_bytes()
    assert ri.main(argv(rig), source=rig["source"]) == 2  # a certification is never rewritten
    assert rig["out"].read_bytes() == first
    # not even an empty placeholder
    other = rig["tmp"] / "out" / "mine.json"
    other.write_text("{}", encoding="utf-8")
    assert ri.main(argv(rig, **{"--out": str(other)}), source=rig["source"]) == 2 and other.read_text() == "{}"


def test_certify_previous_run_must_match_soak_and_model(cert_rig: dict[str, Any]) -> None:
    rig = cert_rig
    assert ri.main(argv(rig), source=rig["source"]) == 0
    prev = json.loads(rig["out"].read_text(encoding="utf-8"))
    ok = argv(rig, **{"--out": fresh_out(rig, "v2b.json"), "--previous": str(rig["out"])})
    assert ri.main(ok, source=rig["source"]) == 0
    doc = json.loads((rig["tmp"] / "out" / "v2b.json").read_text(encoding="utf-8"))
    pr = doc["previous_run"]
    assert pr["sha256"] == hashlib.sha256(rig["out"].read_bytes()).hexdigest() and pr["rule"] == "v2"
    assert pr["p0_bars_criterion"] == "pass" and pr["created_at"] == prev["created_at"]
    for key, value, name in (("soak_report", "0" * 64, "other_soak.json"), ("model", "other@9", "other_model.json")):
        bad = json.loads(json.dumps(prev))
        bad[key]["sha256" if key == "soak_report" else "version"] = value
        path = rig["tmp"] / "out" / name
        path.write_text(json.dumps(bad), encoding="utf-8")
        args = argv(rig, **{"--out": fresh_out(rig, "never.json"), "--previous": str(path)})
        assert ri.main(args, source=rig["source"]) == 2
    assert not (rig["tmp"] / "out" / "never.json").exists()
    missing = argv(rig, **{"--out": fresh_out(rig, "never.json"), "--previous": str(rig["tmp"] / "nope.json")})
    assert ri.main(missing, source=rig["source"]) == 2


def test_certify_reread_vs_previous_flags_difference(cert_rig: dict[str, Any]) -> None:
    rig = cert_rig
    assert ri.main(argv(rig), source=rig["source"]) == 0
    again = argv(rig, **{"--out": fresh_out(rig, "same.json"), "--previous": str(rig["out"])})
    assert ri.main(again, source=rig["source"]) == 0
    same = json.loads((rig["tmp"] / "out" / "same.json").read_text(encoding="utf-8"))
    assert all(sec["diagnostic"]["reread_vs_previous"]["same"] is True for sec in same["products"].values())
    # REST has revised one more minute of ETH-EUR since the first run
    frame = rig["source"].frames["ETH-EUR"]
    i = frame.index[-500]
    frame.loc[i, "close"] = frame.loc[i, "close"] * 1.001
    changed = argv(rig, **{"--out": fresh_out(rig, "changed.json"), "--previous": str(rig["out"])})
    assert ri.main(changed, source=rig["source"]) == 0
    doc = json.loads((rig["tmp"] / "out" / "changed.json").read_text(encoding="utf-8"))
    rv = doc["products"]["ETH-EUR"]["diagnostic"]["reread_vs_previous"]
    assert rv["same"] is False and rv["now"]["revised"] == rv["previous"]["revised"] + 1
    assert doc["products"]["BTC-EUR"]["diagnostic"]["reread_vs_previous"]["same"] is True
    with pytest.raises(ValueError, match="re-read"):
        note_text(doc, "2026-10-24T00:00:00Z")


def test_certify_exit_codes(cert_rig: dict[str, Any]) -> None:
    rig = cert_rig
    assert ri.main(argv(rig), source=rig["source"]) == 0
    rig["holder"]["decider"] = SteepDecider()  # a sensitive model: R1 fails with one revised minute
    assert ri.main(argv(rig, **{"--out": fresh_out(rig, "fail.json")}), source=rig["source"]) == 1
    fail = json.loads((rig["tmp"] / "out" / "fail.json").read_text(encoding="utf-8"))
    assert fail["p0_bars_criterion"] == "fail" and fail["p0_bars_criterion_rule_v1"] == "fail"
    rig["holder"]["decider"] = ConstDecider()
    # a window shorter than one block: R1 cannot be formed, the outcome is unmeasured and the exit code is 1
    soak_bytes = rig["soak"].read_bytes()
    short = json.loads(soak_bytes)
    short["started_at"] = (datetime.fromisoformat(short["updated_at"]) - timedelta(hours=10)).isoformat()
    rig["soak"].write_text(json.dumps(short), encoding="utf-8")
    assert ri.main(argv(rig, **{"--out": fresh_out(rig, "short.json")}), source=rig["source"]) == 1
    doc = json.loads((rig["tmp"] / "out" / "short.json").read_text(encoding="utf-8"))
    assert doc["p0_bars_criterion"] == "unmeasured" and doc["products"]["ETH-EUR"]["R1"] == "unmeasured"
    assert doc["products"]["ETH-EUR"]["R2"] == "pass"
    rig["soak"].write_bytes(soak_bytes)
    # configuration errors: exit 2 and nothing written over the soak report
    assert ri.main(argv(rig, **{"--out": str(rig["soak"])}), source=rig["source"]) == 2
    assert ri.main(argv(rig, **{"--soak-report": str(rig["tmp"] / "missing.json")}), source=rig["source"]) == 2
    assert ri.main(argv(rig, **{"--out": str(rig["root"] / "cert.json")}), source=rig["source"]) == 2
    assert rig["soak"].read_bytes() == soak_bytes
    young = json.loads(soak_bytes)
    young["updated_at"] = datetime.now(UTC).isoformat()
    young["started_at"] = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    rig["soak"].write_text(json.dumps(young), encoding="utf-8")
    assert ri.main(argv(rig, **{"--out": fresh_out(rig, "young.json")}), source=rig["source"]) == 2  # < 15 min old


def test_certify_r4_fails_on_an_appeared_minute(cert_rig: dict[str, Any]) -> None:
    """A candle REST has and the store does not (the minute was published flat and traded later) fails R4."""
    rig = cert_rig
    path = sorted(rig["root"].rglob("product=BTC-EUR/**/month=*.parquet"))[-1]
    stored = pd.read_parquet(path)
    stored[stored["ts"] != stored["ts"].iloc[-100]].to_parquet(path, index=False)
    assert ri.main(argv(rig), source=rig["source"]) == 1
    btc = json.loads(rig["out"].read_text(encoding="utf-8"))["products"]["BTC-EUR"]
    assert btc["R4"] == {"appeared": 1, "disappeared": 0, "status": "fail"} and btc["R1"] == "pass"


def test_certify_info_only_writes_nothing_and_old_soak_is_not_certifiable(
    cert_rig: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Decision U2: the soak of 18-20/10 gets no certification file after the rule v2; ``--info-only`` still prints
    the outcome under the rules v1 and v2 and writes nothing (no document, no re-read candles)."""
    rig = cert_rig
    sha = hashlib.sha256(rig["soak"].read_bytes()).hexdigest()
    monkeypatch.setattr(ri, "NOT_CERTIFIABLE", {sha: "decision U2"})
    assert ri.main(argv(rig), source=rig["source"]) == 2  # not allowed to write
    assert not rig["out"].exists() and not (rig["out"].parent / "cert.reread").exists()
    args = ["certify", "--soak-report", str(rig["soak"]), "--model-dir", str(rig["model"]), "--info-only",
            "--bars-root", str(rig["root"]), "--label-config", str(rig["tmp"] / "label.json")]  # fmt: skip
    assert ri.main(args, source=rig["source"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["mode"] == "info-only" and printed["rule_v2"] == "pass" and printed["rule_v1"] == "pass"
    assert printed["products"]["ETH-EUR"]["blocks"] and printed["products"]["ETH-EUR"]["R4"] == "pass"
    assert not list(rig["tmp"].rglob("*.reread")) and not rig["out"].exists()


def test_note_text_only_for_pass_with_same_reread(cert_rig: dict[str, Any]) -> None:
    rig = cert_rig
    assert ri.main(argv(rig), source=rig["source"]) == 0
    first = json.loads(rig["out"].read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="re-read"):  # no previous run: nothing proves the re-read is the same
        note_text(first, "2026-10-24T00:00:00Z")
    assert ri.main(argv(rig, **{"--out": fresh_out(rig, "b.json"), "--previous": str(rig["out"])}),
                   source=rig["source"]) == 0  # fmt: skip
    second = json.loads((rig["tmp"] / "out" / "b.json").read_text(encoding="utf-8"))
    text = note_text(second, "2026-10-24T00:00:00Z")
    assert text.startswith("- [x] P0, criterio barre REST: certificato il 2026-10-24T00:00:00Z")
    assert "2026-10-23, DOPO il primo calcolo" in text and "blocco peggiore 0/1.440" in text
    failed = json.loads(json.dumps(second))
    failed["p0_bars_criterion"] = "fail"
    with pytest.raises(ValueError, match="passed"):
        note_text(failed, "2026-10-24T00:00:00Z")
    unmeasured = json.loads(json.dumps(second))
    unmeasured["p0_bars_criterion"] = "unmeasured"
    with pytest.raises(ValueError, match="passed"):
        note_text(unmeasured, "2026-10-24T00:00:00Z")


def test_gate_config_defaults_feed_the_thresholds() -> None:
    thr = ImpactThresholds.from_config(GateOpsConfig())
    assert thr == THR
    assert AppConfig().dry_run.gate_ops.revision_shift_threshold == 0.01
