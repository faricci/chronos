"""Impact of the REST candle revisions on what the model sees and decides (spec sez. 5.1.7, riga "Barre REST").

The store keeps the bars the system CONSUMED (invariant I2). The exchange may revise a candle after that. What matters
is not how many minutes were revised (``rest_revision_ratio``: a diagnostic since spec 2.11) but how much the model's
answers and decisions would have been different on the revised bars:

* the SERVED world ``S`` = ``fill_flat_bars(real rows of the store)``, the sequence the engine consumed;
* the REVISED world ``F`` = the same real rows with the known value revisions applied, the grid rebuilt with the SAME
  ``fill_flat_bars`` (a revised minute is followed by flat minutes that carry ITS close, exactly as ``load_bars``
  would build them on the re-read candles; applying the revision to the already filled grid would leave the flat
  minutes at the old close and create a spurious jump). ``F`` is a copy in memory: the store is never written.

For every state of a product and UTC day, and for the model version in use:

* ``de = |p_edge(F) - p_edge(S)|``; FLAT: ``db = |P(BUY | BUY or HOLD)(F) - (S)|``; LONG: ``ds`` the same for SELL;
  ``m = max`` of the defined ones (a NaN in one world only is ``+inf``; NaN in both is 0);
* R1  share of states with ``m > shift`` is at most ``max_share``, measured on BLOCKS of 1440 consecutive states
  (rule v2, P1e-bis: a full UTC day is its own block; a shorter day uses the 1440 states of the measured series that
  end with its last state, or the first 1440 if there are not enough before; a series shorter than 1440 states is
  ``unmeasured``). It is the same ``n`` as the production gate, which judges D-1 as 1440 states;
* R2  ``max m`` is at most ``max_shift`` (per UTC day: the maximum does not depend on the grouping);
* R3  share of states whose actionability flips (BUY actionable on FLAT; SELL on LONG) is at most ``max_flip_share``,
  on the same blocks as R1;
* R4  zero minutes that appeared or disappeared (completeness of the endpoint, a different problem: it fails).

Thresholds come from ``dry_run.gate_ops`` (``ImpactThresholds.from_config``). A criterion that cannot be measured is
``unmeasured``, never ``pass``. ``certify`` re-runs all of it on the soak P0 already done (no verdict is rewritten)::

    python -m modules.dryrun.revision_impact certify --soak-report data/reports/p0_soak.json \\
        --model-dir models/baseline/<version> --subset final --out data/reports/<new file>.json [--previous <file>]

A certification file is never rewritten (``--out`` must not exist) and the re-read candles are saved next to it
with their sha256. ``--info-only`` computes and prints the outcome under the rules v1 and v2 and writes nothing.
Network only in ``certify`` (public candles). Exit code: 0 criterion passed, 1 failed or unmeasured, 2 configuration
error.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import os
import sys
from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal

import numpy as np
import pandas as pd

from modules.config import GateOpsConfig, RiskConfig
from modules.decision.policy import (
    REGIME_FEATURE,
    DecisionPolicy,
    PolicyConfig,
    load_policy,
    masked_buy_prob,
    masked_sell_prob,
)
from modules.features.serializer import FLAT_COLUMNS
from modules.features.state_matrix import flat_matrix, long_matrix
from modules.market.backfill import BarStore, CandleSource, candles_to_frame, fill_flat_bars
from modules.market.bars import OHLCV, REST_CHUNK_MIN, classify_revisions, floor_minute, rest_revisions
from modules.market.indicators_batch import compute_indicators
from modules.risk.label_config import LoadedLabelConfig

__all__ = [
    "BLOCK_RULE",
    "STATES_PER_DAY",
    "ImpactBlock",
    "ImpactDay",
    "ImpactThresholds",
    "PositionSpan",
    "ShiftedState",
    "actionable_mask",
    "apply_revisions",
    "block_status",
    "calendar_verdict",
    "certify",
    "day_blocks",
    "impact_day",
    "main",
    "note_text",
    "revised_world",
    "revision_impact",
    "rolling_max",
    "summary",
    "verdict",
]

MINUTE: Final = timedelta(minutes=1)
MIN_AGE: Final = timedelta(minutes=15)
CERT_SCHEMA: Final = "chronos.p0_bars_certification.v2"
STATES_PER_DAY: Final = 1440  # one UTC day of 1-minute decisions: the n of the production gate (G-DRY-OPS judges D-1)
TOP_SHIFTS: Final = 5
BLOCK_RULE: Final[Mapping[str, Any]] = {
    "version": "v2",
    "block_states": STATES_PER_DAY,
    "rule": "R1 e R3 su blocchi di 1440 stati consecutivi: giorno UTC intero = blocco; giorno con meno stati = i 1440 "
    "stati della serie che terminano con il suo ultimo stato, o i primi 1440 della serie se prima non ce ne "
    "sono abbastanza; serie con meno di 1440 stati = unmeasured. R2 e R4 per giorno UTC.",
    "supersedes": "v1: R1 e R3 per giorno UTC, anche parziale (A.2/A.6, 2026-10-20)",
    "decided_on": "2026-10-23",
    "fixed_after_first_run": True,
    "first_run": "2026-10-23T06:17Z, p0_bars_criterion fail: R1 ETH-EUR 2026-10-20, 2/343 stati",
    "reason": "un giorno parziale di n stati e' un test diverso dal gate di produzione (1440 stati per D-1): "
    "con n = 343 "
    "e revisioni che spostano 2 stati la quota 0.005 ammette zero revisioni",
    "decision": "docs/decisions/2026-10-20-revisioni-rest-e-promozione.md (sez. P1e-bis)",
}
# Soaks whose certification was decided NOT to be (re)written after the rule v2 (decision U2, 2026-10-23): the
# certification of P0 comes from whole UTC days of the dry run, where the rules v1 and v2 coincide.
NOT_CERTIFIABLE: Final[Mapping[str, str]] = {
    "2f5cd7c3f1b69059eac51631ddccdbe98d104a2082c790bed7922c57d9bc439d": "soak P0 18-20/10/2026: the v1 certification "
    "(fail) stays as it is; the rule v2 was written after seeing it (decision U2, 2026-10-23). Use --info-only.",
}
DECISION_DOC: Final = "docs/decisions/2026-10-20-revisioni-rest-e-promozione.md"
EXIT_PASS, EXIT_FAIL, EXIT_CONFIG = 0, 1, 2
INF_AS_JSON: Final = 1.0  # a shift of +inf is written as 1.0: the largest possible change of a probability
_COL: Final = {c: i for i, c in enumerate(FLAT_COLUMNS)}

R3Status = Literal["measured", "policy_untunable", "decider_without_batch"]
Verdict = Literal["pass", "fail", "unmeasured"]


# ----------------------------------------------------------------------------------------------- data


@dataclass(frozen=True)
class ImpactThresholds:
    shift: float  # R1: a state is shifted if m > shift
    max_share: float  # R1: max share of shifted states
    max_shift: float  # R2
    max_flip_share: float  # R3
    # The unit of R1 and R3: states per block. NOT a config key: it is the definition of the production test (one UTC
    # day of 1-minute decisions), not a threshold; the synthetic tests build smaller blocks directly.
    block_states: int = STATES_PER_DAY

    def __post_init__(self) -> None:
        if self.block_states < 1:
            raise ValueError("block_states must be at least 1")

    @classmethod
    def from_config(cls, g: GateOpsConfig) -> ImpactThresholds:
        return cls(
            g.revision_shift_threshold,
            g.max_revision_shift_share,
            g.max_revision_shift,
            g.max_revision_flip_share,
            STATES_PER_DAY,
        )


@dataclass(frozen=True)
class PositionSpan:
    """One position of the book as the ledger has it (the numbers ``PositionInput.from_view`` gives, as floats).
    The states with ``opened_at <= t < closed_at`` (``closed_at`` None: still open) are LONG."""

    product: str
    opened_at: datetime
    closed_at: datetime | None
    entry_px: float
    stop_loss: float
    take_profit: float
    atr_entry: float


def _json_m(v: float) -> float | None:
    return v if math.isfinite(v) else (INF_AS_JSON if v > 0 else None)


def _iso(t: datetime | None) -> str | None:
    return None if t is None else t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class ShiftedState:
    """A state whose answer moved by ``m``. ``origin`` = ``"<product> <minute of the revision, %Y-%m-%dT%H:%MZ>"``: the
    most recent revised minute (of the product, or of BTC for the others) not after the bar of the state. It is an
    APPROXIMATE attribution (the 1-day features also feel older revisions): a diagnostic, never a criterion."""

    t: datetime  # close instant of the state (ts_bar_close), UTC
    m: float  # shift (inf when NaN in one world only)
    origin: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"t": _iso(self.t), "m": _json_m(self.m), "origin": self.origin}


@dataclass(frozen=True)
class ImpactDay:
    product: str
    day: date
    version: str
    states: int
    flat_states: int
    long_states: int
    shifted: int
    share: float
    max_shift: float
    p99_de: float
    p99_db: float
    p99_ds: float | None
    flips: int | None
    flip_share: float | None
    r3: R3Status
    states_any_token_diff: int
    token_diff_by_column: dict[str, int]
    states_nan_both: int
    first_at: datetime | None = None  # close instant of the first and of the last state (None when states == 0)
    last_at: datetime | None = None
    shifted_states: tuple[ShiftedState, ...] = ()  # the states with m > shift, by t; len == shifted
    flipped_at: tuple[datetime, ...] = ()  # t of the flips (empty when R3 is not measured)
    top_shifts: tuple[ShiftedState, ...] = ()  # the largest m of the day (m descending, then t ascending)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe: ``day`` as ISO date, instants as ISO Z, non finite numbers as ``INF_AS_JSON`` (inf) or None."""
        doc = {k: v for k, v in asdict(self).items() if k not in ("shifted_states", "flipped_at", "top_shifts")}
        doc["day"] = self.day.isoformat()
        for key in ("max_shift", "p99_de", "p99_db", "p99_ds"):
            v = doc[key]
            if isinstance(v, float):
                doc[key] = _json_m(v)
        doc["first_at"], doc["last_at"] = _iso(self.first_at), _iso(self.last_at)
        doc["shifted_states"] = [x.to_dict() for x in self.shifted_states]
        doc["flipped_at"] = [_iso(t) for t in self.flipped_at]
        doc["top_shifts"] = [x.to_dict() for x in self.top_shifts]
        return doc


@dataclass(frozen=True)
class ImpactBlock:
    """The unit of R1 and R3: ``states`` consecutive states of one product and version that contain, or end with, the
    judged day. Several blocks may share states; the share of a block is always over ITS states."""

    product: str
    version: str
    day: date  # the UTC day the block judges
    start: datetime  # close instants of the first and of the last state, inclusive
    end: datetime
    states: int
    kind: Literal["day", "extended_back", "extended_forward"]
    shifted: int
    share: float
    flips: int | None
    flip_share: float | None
    r3: R3Status
    revisions: int  # independent clusters among the shifted states (same origin = one revision)
    max_cluster: int  # shifted states of the largest cluster (0 when none)

    def to_dict(self) -> dict[str, Any]:
        doc = asdict(self)
        doc["day"] = self.day.isoformat()
        doc["start"], doc["end"] = _iso(self.start), _iso(self.end)
        return doc


# ------------------------------------------------------------------------------------ the revised world


def apply_revisions(real: pd.DataFrame, value_revisions: pd.DataFrame) -> pd.DataFrame:
    """Copy of the REAL rows of the store (``ts`` + OHLCV) with the re-read OHLCV of the revised minutes.

    ``value_revisions``: ``RevisionSet.value`` (index ``ts``). ``KeyError`` if a revised minute is not among the real
    rows (``appeared`` and ``disappeared`` are not applied: they are R4). The input is never modified."""
    out = real.copy()
    if value_revisions.empty:
        return out
    pos = pd.DatetimeIndex(out["ts"]).get_indexer(pd.DatetimeIndex(value_revisions.index))
    if (pos < 0).any():
        missing = pd.DatetimeIndex(value_revisions.index)[pos < 0]
        raise KeyError(f"revised minutes not among the real rows: {[str(t) for t in missing[:3]]}")
    for col in OHLCV:
        arr = out[col].to_numpy(dtype=np.float64, copy=True)
        arr[pos] = value_revisions[col].to_numpy(dtype=np.float64)
        out[col] = arr
    return out


def revised_world(
    real: pd.DataFrame, value_revisions: pd.DataFrame, start: datetime | None, end: datetime | None
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """``(S, F)``: the served grid and the grid of the revised rows, both built by ``fill_flat_bars`` with the same
    ``start`` / ``end``. ``ValueError`` if the two grids differ (a value revision cannot add or remove minutes)."""
    served = fill_flat_bars(real, start, end)
    revised = fill_flat_bars(apply_revisions(real, value_revisions), start, end)
    if not served.index.equals(revised.index):
        raise ValueError("the revised grid differs from the served one")
    return served, revised


# -------------------------------------------------------------------------------------- the core maths


def _shift(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """``|a - b|`` with NaN rules: NaN on one side only is +inf, NaN on both is 0. Also returns the both-NaN mask."""
    na, nb = np.isnan(a), np.isnan(b)
    with np.errstate(invalid="ignore"):
        d = np.abs(a - b)
    d = np.where(na & nb, 0.0, np.where(na ^ nb, np.inf, d))
    return np.asarray(d), na & nb


def _p99(v: np.ndarray) -> float:
    return float(np.quantile(v, 0.99, method="higher")) if v.size else 0.0


def _buy_actionable(
    x: np.ndarray, p_action: np.ndarray, p_edge: np.ndarray, policy: PolicyConfig, label: LoadedLabelConfig,
    risk: RiskConfig,
) -> np.ndarray:  # fmt: skip
    """BUY on FLAT states: policy thresholds + regime, fee floor and EV gate with the state tokens (sez. 0.3)."""
    cfg = label.cfg
    atr = x[:, _COL[f"atr_14_{cfg.atr_tf}_bps"]]
    win = cfg.k_tp * atr - x[:, _COL["cost_tp_bps"]]
    loss = cfg.k_sl * atr + x[:, _COL["cost_sl_bps"]]
    fee_ok = win >= risk.min_net_edge_bps
    ev = p_edge * win - (1.0 - p_edge) * loss
    ev_ok = (ev > risk.ev_min_bps) & (ev >= policy.p_be_buffer * (win + loss))
    buy = DecisionPolicy(policy).buy_mask(p_action, x[:, _COL[REGIME_FEATURE]])
    return np.asarray(buy & fee_ok & ev_ok)


def actionable_mask(
    x: np.ndarray, p_action: np.ndarray, p_edge: np.ndarray, is_long: np.ndarray, policy: PolicyConfig,
    label: LoadedLabelConfig, risk: RiskConfig,
) -> np.ndarray:  # fmt: skip
    """Would the decision be BUY (FLAT states) or SELL (LONG states)? Computed on the tokens of ONE world."""
    out = np.zeros(len(x), dtype=bool)
    flat = ~is_long
    if flat.any():
        out[flat] = _buy_actionable(x[flat], p_action[flat], p_edge[flat], policy, label, risk)
    if is_long.any():
        out[is_long] = DecisionPolicy(policy).sell_mask(p_action[is_long])
    return out


def impact_day(
    *, product: str, day: date, version: str, x_served: np.ndarray, x_revised: np.ndarray, is_long: np.ndarray,
    decider: Any, policy: PolicyConfig | None, label: LoadedLabelConfig, risk: RiskConfig,
    thresholds: ImpactThresholds, t_close: np.ndarray | None = None,
) -> ImpactDay:  # fmt: skip
    """The numbers of one (product, day, version) from the state matrices of the two worlds (LONG rows built).

    ``t_close``: the close instant of every state (length ``n``); it fills ``first_at``/``last_at`` and the lists of
    shifted and flipped states, which the blocks of the rule v2 need. Without it those fields stay at their defaults."""
    n = len(x_served)
    if x_revised.shape != x_served.shape or is_long.shape != (n,):
        raise ValueError("served and revised matrices must have the same shape")
    if t_close is not None and len(t_close) != n:
        raise ValueError("t_close must have one instant per state")
    n_long = int(is_long.sum())
    instants: list[datetime] = []
    if t_close is not None:
        idx = pd.DatetimeIndex(t_close)
        instants = [t.to_pydatetime() for t in (idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC"))]
    first_at, last_at = (instants[0], instants[-1]) if instants else (None, None)

    def empty(flips: int | None, r3: R3Status) -> ImpactDay:
        return ImpactDay(
            product, day, version, n, n - n_long, n_long, 0, 0.0, 0.0, 0.0, 0.0, None, flips,
            None if flips is None else 0.0, r3, 0, {}, 0, first_at, last_at,
        )  # fmt: skip

    if not hasattr(decider, "answers_batch"):
        return empty(None, "decider_without_batch")
    if n == 0:
        return empty(0, "measured") if policy is not None else empty(None, "policy_untunable")
    a_s, a_f = decider.answers_batch(x_served), decider.answers_batch(x_revised)
    pa_s, pa_f = np.asarray(a_s.p_action, np.float64), np.asarray(a_f.p_action, np.float64)
    pe_s, pe_f = np.asarray(a_s.p_edge, np.float64), np.asarray(a_f.p_edge, np.float64)
    de, nan_de = _shift(pe_s, pe_f)
    db, nan_db = _shift(masked_buy_prob(pa_s), masked_buy_prob(pa_f))
    ds, nan_ds = _shift(masked_sell_prob(pa_s), masked_sell_prob(pa_f))
    m = np.where(is_long, np.maximum(de, ds), np.maximum(de, db))
    nan_both = nan_de | np.where(is_long, nan_ds, nan_db)
    shifted = int((m > thresholds.shift).sum())

    same = (x_served == x_revised) | (np.isnan(x_served) & np.isnan(x_revised))
    by_col = {FLAT_COLUMNS[j]: int((~same[:, j]).sum()) for j in range(same.shape[1]) if not same[:, j].all()}

    flips: int | None = None
    flip_share: float | None = None
    flipped: tuple[datetime, ...] = ()
    r3: R3Status = "policy_untunable"
    if policy is not None:
        act_s = actionable_mask(x_served, pa_s, pe_s, is_long, policy, label, risk)
        act_f = actionable_mask(x_revised, pa_f, pe_f, is_long, policy, label, risk)
        changed = act_s != act_f
        flips = int(changed.sum())
        flip_share, r3 = flips / n, "measured"
        flipped = tuple(instants[i] for i in np.flatnonzero(changed)) if instants else ()
    shifted_states: tuple[ShiftedState, ...] = ()
    top: tuple[ShiftedState, ...] = ()
    if instants:
        shifted_states = tuple(ShiftedState(instants[i], float(m[i])) for i in np.flatnonzero(m > thresholds.shift))
        order = sorted((i for i in range(n) if m[i] > 0), key=lambda i: (-float(m[i]), instants[i]))[:TOP_SHIFTS]
        top = tuple(ShiftedState(instants[i], float(m[i])) for i in order)
    return ImpactDay(
        product, day, version, n, n - n_long, n_long, shifted, shifted / n, float(m.max()), _p99(de),
        _p99(db[~is_long]), _p99(ds[is_long]) if n_long else None, flips, flip_share, r3,
        int((~same.all(axis=1)).sum()), by_col, int(nan_both.sum()), first_at, last_at, shifted_states, flipped, top,
    )  # fmt: skip


def world_indicators(
    served: Mapping[str, pd.DataFrame], revised: Mapping[str, pd.DataFrame]
) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    """``compute_indicators`` of every product in the served and in the revised world."""
    return (
        {p: compute_indicators(s) for p, s in served.items()},
        {p: compute_indicators(revised[p]) for p in served},
    )


def revision_impact(
    served: Mapping[str, pd.DataFrame], revised: Mapping[str, pd.DataFrame], days: Sequence[date], decider: Any,
    policy: PolicyConfig | None, label: LoadedLabelConfig, positions: Sequence[PositionSpan], risk: RiskConfig,
    version: str, *, thresholds: ImpactThresholds | None = None, window: tuple[datetime, datetime] | None = None,
    indicators: tuple[Mapping[str, pd.DataFrame], Mapping[str, pd.DataFrame]] | None = None,
    revised_minutes: Mapping[str, pd.DatetimeIndex] | None = None,
) -> list[ImpactDay]:  # fmt: skip
    """``ImpactDay`` of every (product, day) for ONE model version.

    ``served`` / ``revised``: product -> the ``S`` / ``F`` grids of ``revised_world``. The states of a day are the
    bars whose close instant ``t = ts + 1 min`` falls in that UTC day; ``window`` (bar ``ts`` in ``[lo, hi)``)
    restricts them further (partial days). The BTC context of ETH/SOL comes from the BTC indicators of the SAME
    world. ``indicators``: the result of ``world_indicators`` when several versions are measured on the same worlds
    (the indicators are the slow part). ``revised_minutes``: product -> the value-revised minutes applied to its
    revised world; with it every shifted state gets an ``origin`` (the most recent revised minute of the product, or of
    BTC for the others, not after the bar of the state; on a tie the product wins). APPROXIMATE, diagnostic only."""
    thr = thresholds or ImpactThresholds(0.01, 0.005, 0.02, 0.001)
    ind_s, ind_f = indicators if indicators is not None else world_indicators(served, revised)
    btc = next((p for p in served if p.startswith("BTC-")), None)
    out: list[ImpactDay] = []
    for product, grid in served.items():
        ts = pd.DatetimeIndex(grid.index)
        close_t = ts + MINUTE
        keep = np.ones(len(ts), dtype=bool)
        if window is not None:
            keep = np.asarray((ts >= pd.Timestamp(window[0])) & (ts < pd.Timestamp(window[1])))
        btc_rows = (
            ind_s[btc].index.get_indexer(ind_s[product].index) if btc and not product.startswith("BTC-") else None
        )
        close_s, close_f = grid["close"].to_numpy(np.float64), revised[product]["close"].to_numpy(np.float64)
        for day in days:
            lo = pd.Timestamp(datetime(day.year, day.month, day.day, tzinfo=UTC))
            rows = np.flatnonzero(keep & np.asarray((close_t >= lo) & (close_t < lo + pd.Timedelta(days=1))))
            br = None if btc_rows is None else btc_rows[rows]
            xs = flat_matrix(ind_s[product], rows, product, label, ind_s[btc] if btc else None, br)
            xf = flat_matrix(ind_f[product], rows, product, label, ind_f[btc] if btc else None, br)
            is_long = np.zeros(len(rows), dtype=bool)
            t_rows = close_t[rows]
            for pos in (q for q in positions if q.product == product):
                opened = pd.Timestamp(pos.opened_at)
                member = np.asarray(t_rows >= opened)
                if pos.closed_at is not None:
                    member &= np.asarray(t_rows < pd.Timestamp(pos.closed_at))
                sel = np.flatnonzero(member)
                if not sel.size:
                    continue
                is_long[sel] = True
                for x, close in ((xs, close_s), (xf, close_f)):
                    x[sel] = long_matrix(
                        x[sel], t_rows[sel], close[rows][sel], entry_px=pos.entry_px, stop_loss=pos.stop_loss,
                        take_profit=pos.take_profit, atr_entry=pos.atr_entry, opened_at=opened,
                    )  # fmt: skip
            result = impact_day(product=product, day=day, version=version, x_served=xs, x_revised=xf, is_long=is_long,
                                decider=decider, policy=policy, label=label, risk=risk, thresholds=thr,
                                t_close=t_rows.to_numpy())  # fmt: skip
            if revised_minutes is not None:
                result = _with_origins(result, product, btc, revised_minutes)
            out.append(result)
    return out


def _origin(product: str, btc: str | None, revised: Mapping[str, pd.DatetimeIndex], t: datetime) -> str | None:
    """``"<product> <minute>"`` of the latest revised minute (product or BTC) with ``ts <= t - 1 min``."""
    bar = pd.Timestamp(t) - MINUTE
    best: tuple[pd.Timestamp, str] | None = None
    for name in (product, btc) if btc and btc != product else (product,):
        minutes = revised.get(name)
        if minutes is None or not len(minutes):
            continue
        i = int(minutes.searchsorted(bar, side="right")) - 1
        if i >= 0 and (best is None or minutes[i] > best[0]):  # strictly later: on a tie the product (first) stays
            best = (minutes[i], name)
    return None if best is None else f"{best[1]} {best[0]:%Y-%m-%dT%H:%MZ}"


def _with_origins(day: ImpactDay, product: str, btc: str | None, revised: Mapping[str, pd.DatetimeIndex]) -> ImpactDay:
    def tag(x: ShiftedState) -> ShiftedState:
        return replace(x, origin=_origin(product, btc, revised, x.t))

    return replace(
        day, shifted_states=tuple(tag(x) for x in day.shifted_states), top_shifts=tuple(tag(x) for x in day.top_shifts)
    )


def calendar_verdict(days: Sequence[ImpactDay], thr: ImpactThresholds) -> dict[str, Verdict]:
    """Rule v1 (superseded by v2, kept as a diagnostic): R1, R2, R3 for each calendar day as it is, partial or not."""
    if not days or any(d.r3 == "decider_without_batch" for d in days):
        return {"R1": "unmeasured", "R2": "unmeasured", "R3": "unmeasured"}
    r1: Verdict = "pass" if all(d.share <= thr.max_share for d in days) else "fail"
    r2: Verdict = "pass" if all(d.max_shift <= thr.max_shift for d in days) else "fail"
    if any(d.r3 != "measured" or d.flip_share is None for d in days):
        r3: Verdict = "unmeasured"
    else:
        r3 = "pass" if all((d.flip_share or 0.0) <= thr.max_flip_share for d in days) else "fail"
    return {"R1": r1, "R2": r2, "R3": r3}


def _series(days: Sequence[ImpactDay]) -> list[ImpactDay]:
    """The days with states, by day, checked to be ONE contiguous series of 1-minute states carrying their instants."""
    series = sorted((d for d in days if d.states > 0), key=lambda d: d.day)
    for d in series:
        if d.first_at is None or d.last_at is None:
            raise ValueError("a day with states needs first_at and last_at (impact_day t_close)")
        if (d.last_at - d.first_at) / MINUTE + 1 != d.states:
            raise ValueError("states are not a contiguous series")
        if len(d.shifted_states) != d.shifted:
            raise ValueError("a day needs the instants of its shifted states")
    for a, b in zip(series, series[1:], strict=False):
        if a.last_at is None or b.first_at != a.last_at + MINUTE:
            raise ValueError("states are not a contiguous series")
    return series


def day_blocks(
    days: Sequence[ImpactDay], block_states: int, *, judged: Collection[date] | None = None
) -> list[ImpactBlock] | None:
    """The blocks of R1 and R3 (rule v2) for ONE product and ONE model version; ``None`` when the series is shorter
    than ``block_states``. A day with ``block_states`` states is its own block; a shorter day judged at the END of the
    series uses the ``block_states`` states that end with its last state (``extended_back``), one at the START the
    first ``block_states`` of the series (``extended_forward``). A block counts its OWN shifted states and flips: the
    shares of overlapping blocks are never added. ``ValueError`` when the days are not one contiguous series."""
    series = _series(days)
    if sum(d.states for d in series) < block_states:
        return None
    s0 = series[0].first_at
    assert s0 is not None
    shifted = sorted((x for d in series for x in d.shifted_states), key=lambda x: x.t)
    flips = sorted(t for d in series for t in d.flipped_at)
    out: list[ImpactBlock] = []
    for d in series:
        if judged is not None and d.day not in judged:
            continue
        assert d.first_at is not None and d.last_at is not None
        kind: Literal["day", "extended_back", "extended_forward"]
        if d.states >= block_states:
            start, end, kind = d.first_at, d.last_at, "day"
        elif (d.last_at - s0) / MINUTE + 1 >= block_states:
            start, end, kind = d.last_at - (block_states - 1) * MINUTE, d.last_at, "extended_back"
        else:
            start, end, kind = s0, s0 + (block_states - 1) * MINUTE, "extended_forward"
        inside = [x for x in shifted if start <= x.t <= end]
        states = int((end - start) / MINUTE) + 1
        touched = [x for x in series if x.first_at is not None and x.last_at is not None and x.first_at <= end
                   and x.last_at >= start]  # fmt: skip
        r3: R3Status = next((x.r3 for x in touched if x.r3 != "measured"), "measured")
        n_flips = sum(1 for t in flips if start <= t <= end) if r3 == "measured" else None
        clusters: dict[str, int] = defaultdict(int)
        for i, x in enumerate(inside):
            clusters[x.origin if x.origin is not None else f"unknown {i}"] += 1  # unknown origin: a cluster of its own
        out.append(
            ImpactBlock(
                d.product,
                d.version,
                d.day,
                start,
                end,
                states,
                kind,
                len(inside),
                len(inside) / states,
                n_flips,
                None if n_flips is None else n_flips / states,
                r3,
                len(clusters),
                max(clusters.values(), default=0),
            )
        )
    return out


def block_status(block: ImpactBlock, thr: ImpactThresholds) -> dict[str, Verdict]:
    """R1 and R3 of one block."""
    r1: Verdict = "pass" if block.share <= thr.max_share else "fail"
    if block.r3 != "measured" or block.flip_share is None:
        return {"R1": r1, "R3": "unmeasured"}
    return {"R1": r1, "R3": "pass" if block.flip_share <= thr.max_flip_share else "fail"}


def _worst(statuses: Sequence[Verdict]) -> Verdict:
    """Precedence fail, then unmeasured, then pass (an empty list is unmeasured)."""
    if not statuses:
        return "unmeasured"
    return "fail" if "fail" in statuses else ("unmeasured" if "unmeasured" in statuses else "pass")


def verdict(
    days: Sequence[ImpactDay], thr: ImpactThresholds, *, judged: Collection[date] | None = None
) -> dict[str, Verdict]:
    """R1, R2, R3 of ONE product (rule v2) over one or more model versions. R1 and R3 come from ``day_blocks`` of each
    version, R2 from the judged days. ``judged``: the days whose result counts (the others only lengthen the series,
    e.g. D-2 for an audit of a partial D-1); ``None`` = every day. A block that cannot be formed (series shorter than
    a block, a judged day without states) is ``unmeasured``; precedence fail > unmeasured > pass."""
    if not days or any(d.r3 == "decider_without_batch" for d in days):
        return {"R1": "unmeasured", "R2": "unmeasured", "R3": "unmeasured"}
    wanted = [d for d in days if judged is None or d.day in judged]
    if not wanted:
        return {"R1": "unmeasured", "R2": "unmeasured", "R3": "unmeasured"}
    r2: Verdict = "pass" if all(d.max_shift <= thr.max_shift for d in wanted) else "fail"
    by_version: dict[str, list[ImpactDay]] = defaultdict(list)
    for d in days:
        by_version[d.version].append(d)
    r1s: list[Verdict] = []
    r3s: list[Verdict] = []
    for version_days in by_version.values():
        blocks = day_blocks(version_days, thr.block_states, judged=judged)
        have = {b.day for b in blocks or ()}
        missing = any(d.day not in have for d in version_days if judged is None or d.day in judged)
        if blocks is None or missing:
            r1s.append("unmeasured")
            r3s.append("unmeasured")
        for b in blocks or ():
            status = block_status(b, thr)
            r1s.append(status["R1"])
            r3s.append(status["R3"])
    return {"R1": _worst(r1s), "R2": r2, "R3": _worst(r3s)}


def rolling_max(
    days: Sequence[tuple[datetime, datetime, Sequence[datetime]]], block_states: int
) -> dict[str, Any] | None:
    """Diagnostic without threshold (never a criterion): the window of ``block_states`` consecutive states, anywhere in
    the series, with the most shifted states. ``days``: ``(first_at, last_at, instants of the shifted states)`` per
    day of ONE contiguous series (``ValueError`` otherwise). ``None`` when the series is shorter than a block;
    otherwise ``{"shifted", "share", "start", "end"}`` of the FIRST window with the maximum. Standard library only."""
    spans = sorted(days, key=lambda d: d[0])
    for first, last, _ in spans:
        if last < first:
            raise ValueError("states are not a contiguous series")
    for (_, last, _), (first, _, _) in zip(spans, spans[1:], strict=False):
        if first != last + MINUTE:
            raise ValueError("states are not a contiguous series")
    if not spans:
        return None
    s0, end = spans[0][0], spans[-1][1]
    n = int((end - s0) / MINUTE) + 1
    if n < block_states:
        return None
    index = sorted(int((t - s0) / MINUTE) for _, _, ts in spans for t in ts)
    best, best_w = -1, 0
    for w in range(n - block_states + 1):
        count = bisect.bisect_right(index, w + block_states - 1) - bisect.bisect_left(index, w)
        if count > best:
            best, best_w = count, w
    start = s0 + best_w * MINUTE
    return {"shifted": best, "share": best / block_states, "start": start, "end": start + (block_states - 1) * MINUTE}


# ---------------------------------------------------------------------------------------- certification


class ConfigurationError(Exception):
    """Something the operator has to fix before ``certify`` can say anything (exit code 2)."""


def _parse_t(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _z(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _real_rows(store: BarStore, product: str) -> pd.DataFrame:
    months = store.months(product)
    if not months:
        raise ConfigurationError(f"no stored bars for {product}")
    frames = [store.read_month(product, *m)[["ts", *OHLCV]] for m in months]
    return pd.concat(frames).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)


def _reread(source: CandleSource, product: str, lo: datetime, hi: datetime) -> pd.DataFrame:
    """Candles of ``[lo, hi)`` from REST, chunks of ``REST_CHUNK_MIN``, END exclusive, ``candles_to_frame`` parsing."""
    raw: list[dict[str, Any]] = []
    cursor = lo
    while cursor < hi:
        upper = min(cursor + timedelta(minutes=REST_CHUNK_MIN), hi)
        raw += source.fetch(product, int(cursor.timestamp()), int(upper.timestamp()))
        cursor = upper
    return candles_to_frame(raw, int(lo.timestamp()), int(hi.timestamp()))


def _write_atomic(path: Path, doc: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=False, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _load_policy(model_dir: Path, subset: str, label: LoadedLabelConfig) -> PolicyConfig | None:
    path = model_dir / subset / "policy.json"
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and raw.get("untunable"):
        return None
    return load_policy(path, label.sha256)


def _clusters(states: Sequence[ShiftedState]) -> tuple[int, int]:
    """(independent revisions, size of the largest cluster) among shifted states: same origin = one revision, a state
    of unknown origin is a cluster of its own."""
    counts: dict[str, int] = defaultdict(int)
    for i, x in enumerate(states):
        counts[x.origin if x.origin is not None else f"unknown {i}"] += 1
    return len(counts), max(counts.values(), default=0)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _save_reread(path: Path, fresh: pd.DataFrame) -> str:
    """Atomic write of the re-read candles of a product; returns the sha256 of the file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fresh.to_parquet(tmp, index=False)
    os.replace(tmp, path)
    return _sha256_file(path)


def _previous_run(previous: Path | None, soak_sha: str, version: str, subset: str) -> dict[str, Any] | None:
    """The earlier certification of the SAME soak, model version and subset (read only), or ``ConfigurationError``."""
    if previous is None:
        return None
    try:
        doc = json.loads(previous.read_text(encoding="utf-8"))
        same = (
            doc["soak_report"]["sha256"] == soak_sha and doc["model"]["version"] == version
            and doc["model"]["subset"] == subset
        )  # fmt: skip
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ConfigurationError(f"cannot read the previous certification {previous}: {exc}") from exc
    if not same:
        raise ConfigurationError("--previous belongs to another soak report, model version or subset")
    return {
        "path": previous.as_posix(), "sha256": _sha256_file(previous), "created_at": doc.get("created_at"),
        "p0_bars_criterion": doc.get("p0_bars_criterion"),
        "rule": "v1" if str(doc.get("v", "")).endswith(".v1") else "v2", "doc": doc,
    }  # fmt: skip


def certify(
    *, soak_report: Path, model_dir: Path, subset: str, out: Path | None, bars_root: Path, label_path: Path,
    source: CandleSource, risk: RiskConfig, gate: GateOpsConfig, now: datetime, previous: Path | None = None,
    info_only: bool = False,
) -> tuple[dict[str, Any], int]:  # fmt: skip
    """R1-R4 on a soak (rule v2, schema ``chronos.p0_bars_certification.v2``). Returns the document and the exit code
    (0 pass, 1 fail or unmeasured). Also reports the outcome under the rule v1 (diagnostic).

    The soak report is read, never written (its sha256 goes into the document: the link goes from the certification
    to the soak). The store is read, never written. A certification file is never rewritten: ``out`` must not exist,
    and the re-read candles are saved next to it (``<out stem>.reread/<product>.parquet``) with their sha256, so a
    later question about the rule can be answered on the same data. ``info_only`` computes the same document and
    writes NOTHING; it is the only mode allowed on a soak listed in ``NOT_CERTIFIABLE`` (decision U2).
    ``ConfigurationError`` for anything that must be fixed first."""
    from modules.decision.baseline import BaselineDecider
    from modules.decision.questions import build_questions
    from modules.risk.label_config import load_label_config

    if not info_only:
        if out is None:
            raise ConfigurationError("--out is required (or use --info-only)")
        if out.resolve() == soak_report.resolve():
            raise ConfigurationError("--out would overwrite the soak report: the soak verdict is never rewritten")
        if bars_root.resolve() in out.resolve().parents:
            raise ConfigurationError("--out lies under the bars store: the store is never written")
        if out.exists():
            raise ConfigurationError("a certification is never rewritten: choose a new --out")
    try:
        soak_bytes = soak_report.read_bytes()
        soak = json.loads(soak_bytes)
        started, updated = _parse_t(soak["started_at"]), _parse_t(soak["updated_at"])
        products: list[str] = list(soak["products"])
    except (OSError, ValueError, KeyError) as exc:
        raise ConfigurationError(f"cannot read the soak report {soak_report}: {exc}") from exc
    soak_sha = hashlib.sha256(soak_bytes).hexdigest()
    if not info_only and soak_sha in NOT_CERTIFIABLE:
        raise ConfigurationError(f"this soak is not certified after the rule v2: {NOT_CERTIFIABLE[soak_sha]}")
    lo, hi = floor_minute(started), floor_minute(updated)
    if now - hi < MIN_AGE:
        raise ConfigurationError(f"the window ends at {_z(hi)}: its minutes are younger than 15 minutes")
    label = load_label_config(label_path)
    cfg = label.cfg
    questions = build_questions(cfg.horizon_min, cfg.k_tp, cfg.k_sl, cfg.atr_tf)
    decider = BaselineDecider.load(model_dir, label=label, questions=questions, subset=subset, allow_degenerate=False)
    policy = _load_policy(model_dir, subset, label)
    card = json.loads((model_dir / "model_card.json").read_text(encoding="utf-8"))
    version = str(card.get("model") or model_dir.name)
    prev = _previous_run(previous, soak_sha, version, subset)
    thr = ImpactThresholds.from_config(gate)
    reread_dir = None if out is None or info_only else out.parent / f"{out.stem}.reread"
    if reread_dir is not None and bars_root.resolve() in reread_dir.resolve().parents:
        raise ConfigurationError("the re-read candles would be written under the bars store")

    store = BarStore(bars_root)
    served: dict[str, pd.DataFrame] = {}
    revised: dict[str, pd.DataFrame] = {}
    sections: dict[str, dict[str, Any]] = {}
    ends: dict[str, datetime] = {}
    minutes: dict[str, pd.DatetimeIndex] = {}
    fresh_frames: dict[str, pd.DataFrame] = {}
    for product in products:
        # the soak stops publishing at ``bars.<p>.next_minute``: past it the store has no row and REST has candles
        # (the "appeared" of a window edge, not a revision). The window of each product ends there.
        edge = (soak.get("bars") or {}).get(product, {}).get("next_minute")
        end_p = min(hi, _parse_t(edge)) if edge else hi
        ends[product] = end_p
        real = _real_rows(store, product)
        fresh = _reread(source, product, lo, end_p)
        fresh_frames[product] = fresh
        stats = rest_revisions(product, real, fresh, lo, end_p)
        rev = classify_revisions(real, fresh, lo, end_p)
        minutes[product] = pd.DatetimeIndex(rev.value.index)
        last_bar = min(end_p - MINUTE, real["ts"].iloc[-1].to_pydatetime())
        served[product], revised[product] = revised_world(real, rev.value, None, last_bar)
        prior = ((soak.get("checks") or {}).get("rest_revisions") or {}).get(product) or {}
        sections[product] = {
            "diagnostic": {
                "minutes": stats.minutes, "revised": stats.revised, "ratio": stats.ratio,
                "alarm": stats.ratio > gate.max_rest_revision_ratio, "max_close_diff_bps": stats.max_close_diff_bps,
                "value_revised": stats.value_revised,
                "soak_report": {k: prior.get(k) for k in ("minutes", "revised", "ratio", "max_close_diff_bps")},
            },
            "R4": {"appeared": stats.appeared, "disappeared": stats.disappeared,
                   "status": "pass" if stats.appeared == 0 and stats.disappeared == 0 else "fail"},
            "window": {"start": _z(lo), "end": _z(end_p)},
        }  # fmt: skip

    start_day, end_day = lo.date(), max(ends.values()).date()
    days = [start_day + timedelta(days=i) for i in range((end_day - start_day).days + 1)]
    window = (lo, max(ends.values()))  # a product whose grid ends earlier simply has no later states
    impact = revision_impact(served, revised, days, decider, policy, label, [], risk, version, thresholds=thr,
                             window=window, revised_minutes=minutes)  # fmt: skip
    worst_v2: list[Verdict] = []
    worst_v1: list[Verdict] = []
    for product in products:
        mine = [d for d in impact if d.product == product and d.states > 0]
        v = verdict(mine, thr)
        v1 = calendar_verdict(mine, thr)
        blocks = day_blocks(mine, thr.block_states)
        by_day = {b.day: b for b in blocks or ()}
        sec = sections[product]
        sec["blocks"] = [b.to_dict() for b in blocks or ()]
        day_rows = []
        for d in mine:
            blk = by_day.get(d.day)
            st = block_status(blk, thr) if blk is not None else {"R1": "unmeasured", "R3": "unmeasured"}
            day_rows.append({
                "day": d.day.isoformat(), "states": d.states, "shifted": d.shifted, "share": d.share,
                "max_shift": _json_m(d.max_shift), "p99_de": d.p99_de, "p99_db": d.p99_db, "flips": d.flips,
                "R1": st["R1"], "R2": "pass" if d.max_shift <= thr.max_shift else "fail", "R3": st["R3"],
                "R3_note": None if d.r3 == "measured" else d.r3,
                "block": None if blk is None else blk.to_dict(),
                "shifted_states": [x.to_dict() for x in d.shifted_states],
                "top_shifts": [x.to_dict() for x in d.top_shifts],
                "states_any_token_diff": d.states_any_token_diff, "token_diff_by_column": d.token_diff_by_column,
                "states_nan_both": d.states_nan_both,
            })  # fmt: skip
        sec["days"] = day_rows
        sec["states"] = sum(d.states for d in mine)
        sec["max_shift"] = _json_m(max((d.max_shift for d in mine), default=0.0))
        sec["states_shifted"] = sum(d.shifted for d in mine)
        sec["R1"], sec["R2"], sec["R3"] = v["R1"], v["R2"], v["R3"]  # R4 stays the object with its counts and status
        all_shifted = sorted((x for d in mine for x in d.shifted_states), key=lambda x: x.t)
        n_rev, max_cluster = _clusters(all_shifted)
        rolled = rolling_max(
            [(d.first_at, d.last_at, [x.t for x in d.shifted_states]) for d in mine if d.first_at and d.last_at],
            thr.block_states,
        )  # fmt: skip
        diag = sec["diagnostic"]
        diag["r1_window"] = {"states": sec["states"], "shifted": sec["states_shifted"],
                             "share": sec["states_shifted"] / sec["states"] if sec["states"] else None}  # fmt: skip
        diag["r1_rolling_max"] = None if rolled is None else {
            "shifted": rolled["shifted"], "share": rolled["share"], "start": _iso(rolled["start"]),
            "end": _iso(rolled["end"]),
        }  # fmt: skip
        diag["r1_calendar_days_v1"] = [
            {"day": d.day.isoformat(), "states": d.states, "shifted": d.shifted, "share": d.share,
             "status": "pass" if d.share <= thr.max_share else "fail"}
            for d in mine
        ]  # fmt: skip
        diag["revisions_independent"], diag["max_cluster"] = n_rev, max_cluster
        diag["top_shifts"] = [
            x.to_dict() for x in sorted(all_shifted, key=lambda x: (-x.m, x.t))[:TOP_SHIFTS]
        ]  # the shifted states by size, whole window, with their origin
        diag["r2_utilization"] = max((d.max_shift for d in mine), default=0.0) / thr.max_shift
        if math.isinf(diag["r2_utilization"]):
            diag["r2_utilization"] = None
        if reread_dir is not None:
            path = reread_dir / f"{product}.parquet"
            diag["reread"] = {"path": path.as_posix(), "sha256": _save_reread(path, fresh_frames[product])}
        else:
            diag["reread"] = None
        now_vals = {k: diag[k] for k in ("minutes", "revised", "value_revised", "max_close_diff_bps")}
        now_vals["shifted_by_day"] = {r["day"]: r["shifted"] for r in day_rows}
        diag["reread_vs_previous"] = None
        if prev is not None:
            old = (prev["doc"].get("products") or {}).get(product) or {}
            old_diag = old.get("diagnostic") or {}
            before = {k: old_diag.get(k) for k in ("minutes", "revised", "value_revised", "max_close_diff_bps")}
            before["shifted_by_day"] = {r.get("day"): r.get("shifted") for r in old.get("days") or []}
            diag["reread_vs_previous"] = {"same": before == now_vals, "previous": before, "now": now_vals}
        worst_v2 += [sec["R1"], sec["R2"], sec["R4"]["status"]]
        worst_v1 += [v1["R1"], v1["R2"], sec["R4"]["status"]]
    criterion = _worst(worst_v2)
    doc: dict[str, Any] = {
        "v": CERT_SCHEMA, "created_at": _z(now),
        "soak_report": {
            "path": soak_report.as_posix(), "sha256": soak_sha,
            "started_at": soak["started_at"], "updated_at": soak["updated_at"],
            "verdict_rest_revision_ratio_within_limit": (soak.get("verdict") or {}).get(
                "rest_revision_ratio_within_limit"),
        },
        "criterion": {
            "superseded": "rest_revision_ratio <= 0.005 per prodotto (gate)",
            "by": "R1-R4, spec 5.1.7 riga 'Barre REST' (v2.11)", "decided_on": "2026-10-20", "decision": DECISION_DOC,
            "reason": "il ratio non misura lo skew sugli stati ne' sulle decisioni e il suo unico rimedio (alzare S) "
                      "non agisce sulle revisioni rilette ore dopo",
            "r1_r3_unit": dict(BLOCK_RULE),
        },
        "previous_run": None if prev is None else {k: v for k, v in prev.items() if k != "doc"},
        "window": {"start": _z(lo), "end": _z(hi), "end_by_product": {p: _z(e) for p, e in ends.items()},
                   "reread_at": _z(now), "min_age_min": 15},
        "model": {"dir": model_dir.as_posix(), "version": version, "subset": subset,
                  "policy": "untunable" if policy is None else "tuned"},
        "thresholds": {"shift": thr.shift, "max_share": thr.max_share, "max_shift": thr.max_shift,
                       "max_flip_share": thr.max_flip_share, "block_states": thr.block_states},
        "products": sections,
        "p0_bars_criterion": criterion,
        "p0_bars_criterion_rule_v1": _worst(worst_v1),
        "r3_deferred_to": "G-DRY-OPS del primo champion con policy tarabile (spec 5.1.7)",
        "notes": [
            "finestra di ogni prodotto: da floor(started_at) a min(floor(updated_at), bars.<p>.next_minute) del "
            "soak report: oltre next_minute lo store non ha righe e REST ha candele (bordo, non revisioni)",
            "stati FLAT su ogni minuto della finestra (nessun book); giorno UTC = giorno dell'istante di chiusura",
            "max_shift = +inf (NaN in un solo mondo) si scrive come 1.0",
            "origine di uno stato spostato = revisione piu' recente del prodotto o di BTC non successiva alla barra "
            "dello stato: attribuzione approssimata, solo diagnostica",
            "blocchi R1/R3 secondo criterion.r1_r3_unit; p0_bars_criterion_rule_v1 = stessi dati con R1/R3 per giorno "
            "UTC anche parziale (diagnostica)",
        ],
    }  # fmt: skip
    if not info_only:
        assert out is not None
        _write_atomic(out, doc)
    return doc, EXIT_PASS if criterion == "pass" else EXIT_FAIL


# ------------------------------------------------------------------------------- nightly audit (learner L0)

BOOKS: Final = ("champion", "challenger")  # the books whose decider is a registry model (dry_run.books ``registry:*``)
REVISION_COLUMNS: Final = ("product", "ts", "kind", *OHLCV, "stored_close", "stored_volume")
AUDIT_LOOKBACK_DAYS: Final = 30


class ImpactSkip(Exception):
    """The audit has nothing to measure (L0 turns it into ``skipped: <reason>``)."""

    def __init__(self, reason: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason, self.details = reason, dict(details or {})


DeciderLoader = Any  # callable: policy_version -> (decider with answers_batch, PolicyConfig | None)


def revisions_frame(product: str, rev: Any, stored: pd.DataFrame, fresh: pd.DataFrame) -> pd.DataFrame:
    """The revisions of one product and audited day as a table (``data/reports/learner/revisions/<day>.parquet``): one
    row per revised minute with its kind, the RE-READ OHLCV (NaN when the candle disappeared) and the stored close and
    volume. ``rev``: a ``RevisionSet``; ``stored`` / ``fresh``: the frames it was classified from."""
    s = stored.set_index("ts")
    f = fresh.set_index("ts")
    s.index, f.index = pd.DatetimeIndex(s.index).as_unit("ns"), pd.DatetimeIndex(f.index).as_unit("ns")
    stored_value = rev.stored_value
    value = rev.value.assign(kind="value", stored_close=stored_value["close"], stored_volume=stored_value["volume"])
    appeared = f.loc[rev.appeared, list(OHLCV)].assign(kind="appeared", stored_close=np.nan, stored_volume=np.nan)
    gone = pd.DataFrame(np.nan, index=rev.disappeared, columns=list(OHLCV)).assign(
        kind="disappeared", stored_close=s.loc[rev.disappeared, "close"], stored_volume=s.loc[rev.disappeared, "volume"]
    )
    out = pd.concat([value, appeared, gone])
    out.index.name = "ts"
    out = out.reset_index()
    out.insert(0, "product", product)
    out["ts"] = pd.DatetimeIndex(out["ts"]).as_unit("ns")
    floats = {c: np.float64 for c in (*OHLCV, "stored_close", "stored_volume")}
    table: pd.DataFrame = out[list(REVISION_COLUMNS)].astype({"product": object, "kind": object, **floats})
    return table.reset_index(drop=True)


def write_revisions(path: Path, frames: Sequence[pd.DataFrame]) -> int:
    """Atomic write of the day's revisions (an empty table is written too: it says the day WAS audited)."""
    table = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=list(REVISION_COLUMNS))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    table.to_parquet(tmp, index=False)
    os.replace(tmp, path)
    return len(table)


def known_revisions(revisions_dir: Path, upto: date) -> tuple[pd.DataFrame, list[date]]:
    """Every value revision found by the audits of the days ``<= upto`` (the later ones are not known yet), and the
    days that were audited. Newer audit wins when a minute appears twice."""
    days: list[date] = []
    frames: list[pd.DataFrame] = []
    for path in sorted(revisions_dir.glob("*.parquet")) if revisions_dir.is_dir() else []:
        try:
            day = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if day > upto:
            continue
        days.append(day)
        frames.append(pd.read_parquet(path))
    table = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=list(REVISION_COLUMNS))
    value = table[table["kind"] == "value"].drop_duplicates(["product", "ts"], keep="last")
    return value.reset_index(drop=True), days


def decision_versions(path: Path, books: Sequence[str] = BOOKS) -> dict[str, list[str]]:
    """The model versions (``policy.policy_version``) that took decisions in each registry book on the day, in order of
    appearance. A day with two versions (00:00-00:30 still the previous one) measures with each."""
    out: dict[str, list[str]] = {}
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            book, version = rec.get("book"), (rec.get("policy") or {}).get("policy_version")
            if book in books and isinstance(version, str) and "error" not in rec:
                versions = out.setdefault(book, [])
                if version not in versions:
                    versions.append(version)
    return out


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(str(text).replace("Z", "+00:00")).astimezone(UTC)


def ledger_positions(db_path: Path, book: str, lo: datetime, hi: datetime) -> list[PositionSpan]:
    """The positions of ``book`` that overlap ``[lo, hi)`` from the dry ledger (read only), with the numbers of the
    position block: ``avg_entry``, ``sl``, ``tp``, ``atr_at_entry`` and the opening time. The ledger keeps
    ``atr_at_entry`` for the open position only; for a closed trade it is rebuilt from its bracket (``(tp - entry) /
    k_tp``, exact up to the quote increment: it is the same number in both worlds, so it cannot create a shift)."""
    import sqlite3

    if not db_path.exists():
        return []
    spans: list[PositionSpan] = []
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        for product, opened, closed, entry, tp, sl, k_tp in con.execute(
            "SELECT product_id, entry_at, exit_at, entry_avg_price, tp, sl, k_tp FROM closed_trades WHERE book = ?",
            (book,),
        ):
            if None in (opened, closed, entry, tp, sl, k_tp) or not k_tp:
                continue
            t0, t1 = _utc(opened), _utc(closed)
            if t1 > lo and t0 < hi:
                spans.append(PositionSpan(product, t0, t1, float(entry), float(sl), float(tp),
                                          (float(tp) - float(entry)) / float(k_tp)))  # fmt: skip
        for product, opened, entry, sl, tp, atr in con.execute(
            "SELECT product_id, opened_at, avg_entry, sl, tp, atr_at_entry FROM positions "
            "WHERE book = ? AND side = 'LONG'",
            (book,),
        ):
            if None in (opened, entry, sl, tp, atr):
                continue
            t0 = _utc(opened)
            if t0 < hi:
                spans.append(PositionSpan(product, t0, None, float(entry), float(sl), float(tp), float(atr)))
    except sqlite3.Error:
        return []
    finally:
        con.close()
    return [s for s in spans if s.atr_entry > 0 and s.entry_px > 0]


def day_impact(
    *, audited: date, products: Sequence[str], bars_root: Path, revisions_dir: Path, decisions_path: Path,
    loader: DeciderLoader, label: LoadedLabelConfig, risk: RiskConfig, gate: GateOpsConfig, ledger_db: Path | None,
) -> dict[str, Any]:  # fmt: skip
    """R1-R3 of the audited UTC day for every registry book and model version in use (spec A.3.3, rule v2).

    The served world is the real rows of the store up to the end of the day, the revised world applies ALL the value
    revisions known up to the audited day (the daily and 1d tokens read the days before too). K16: when the store
    goes on past midnight the trailing minutes without trades of the day are states of the day (a later real row
    proves them flat; the engine consumed them); only a store that stops before midnight leaves a partial day. A
    day with fewer states than a block is measured together with the day before (same version, same worlds): that
    one is an auxiliary day, never judged, and the block of R1/R3 extends back over it. Raises ``ImpactSkip``."""
    if not decisions_path.exists():
        raise ImpactSkip("no_decisions")
    versions = decision_versions(decisions_path)
    if not versions:
        raise ImpactSkip("no_registry_decisions")
    value, audited_days = known_revisions(revisions_dir, audited)
    if audited not in audited_days:
        raise ImpactSkip("no_revisions_file", {"audited_days": [d.isoformat() for d in audited_days]})
    day_start = datetime(audited.year, audited.month, audited.day, tzinfo=UTC)
    day_end = day_start + timedelta(days=1)
    store = BarStore(bars_root)
    served: dict[str, pd.DataFrame] = {}
    revised: dict[str, pd.DataFrame] = {}
    revised_at: dict[str, pd.DatetimeIndex] = {}
    applied = 0
    for product in products:
        try:
            every = _real_rows(store, product)
        except ConfigurationError:
            continue
        continues = bool((every["ts"] >= pd.Timestamp(day_end)).any())
        real = every[every["ts"] < pd.Timestamp(day_end)].reset_index(drop=True)
        if real.empty:
            continue
        last = day_end - MINUTE if continues else min(day_end - MINUTE, real["ts"].iloc[-1].to_pydatetime())
        mine = value[value["product"] == product]
        mine = mine[mine["ts"].isin(real["ts"]) & (mine["ts"] <= pd.Timestamp(last))].sort_values("ts")
        applied += len(mine)
        revised_at[product] = pd.DatetimeIndex(mine["ts"])
        served[product], revised[product] = revised_world(real, mine.set_index("ts")[list(OHLCV)], None, last)
    if not served:
        raise ImpactSkip("no_bars")
    thr = ImpactThresholds.from_config(gate)
    inds = world_indicators(served, revised)
    objs: dict[tuple[str, str], list[ImpactDay]] = {}
    helpers: dict[tuple[str, str], list[ImpactDay]] = {}
    errors: dict[str, str] = {}
    previous_day = audited - timedelta(days=1)
    for book, book_versions in versions.items():
        positions = (
            [] if ledger_db is None else ledger_positions(ledger_db, book, day_start - timedelta(days=1), day_end)
        )
        for version in book_versions:
            try:
                decider, policy = loader(version)
            except Exception as exc:  # a version that cannot be loaded is reported, the others are still measured
                errors[f"{book}:{version}"] = f"{type(exc).__name__}: {exc}"
                continue

            kw: dict[str, Any] = {"thresholds": thr, "indicators": inds, "revised_minutes": revised_at}
            main_days = revision_impact(
                served, revised, [audited], decider, policy, label, positions, risk, version, **kw
            )
            partial = any(d.states < thr.block_states for d in main_days)
            aux = (
                revision_impact(served, revised, [previous_day], decider, policy, label, positions, risk, version, **kw)
                if partial
                else []
            )
            for d in main_days:
                objs.setdefault((d.product, book), []).append(d)
            for d in aux:
                if d.states > 0:
                    helpers.setdefault((d.product, book), []).append(d)
    if not objs:
        raise ImpactSkip("no_model_loaded", {"errors": errors})
    out: dict[str, Any] = {p: {"books": {}, "blocks": {}, "helper_days": {}, "verdict": {}} for p in served}
    judged = {audited}
    for (product, book), entries in sorted(objs.items()):
        extra = helpers.get((product, book), [])
        blocks: list[ImpactBlock] = []
        try:
            for ver in sorted({d.version for d in entries}):
                blocks += (
                    day_blocks([d for d in entries + extra if d.version == ver], thr.block_states, judged=judged) or []
                )
            result = verdict(entries + extra, thr, judged=judged)
        except ValueError as exc:  # the series is not contiguous (store with holes): R1 and R3 cannot be formed
            errors[f"{book}:{product}:blocks"] = str(exc)
            blocks = []
            r2 = "pass" if all(d.max_shift <= thr.max_shift for d in entries) else "fail"
            result = {"R1": "unmeasured", "R2": r2, "R3": "unmeasured"}  # type: ignore[dict-item]
        out[product]["books"][book] = [e.to_dict() for e in entries]
        out[product]["blocks"][book] = [b.to_dict() for b in blocks]
        out[product]["helper_days"][book] = [e.to_dict() for e in extra]
        out[product]["verdict"][book] = result
    recent = [audited - timedelta(days=i) for i in range(1, AUDIT_LOOKBACK_DAYS + 1)]
    return {
        "audited_day": audited.isoformat(), "products": out, "revisions_applied": applied,
        "audited_days": [d.isoformat() for d in audited_days], "versions": versions, "errors": errors,
        "days_without_audit": [d.isoformat() for d in reversed(recent) if d not in audited_days],
        "thresholds": asdict(thr),
    }  # fmt: skip


class RegistryDeciderLoader:
    """``policy_version`` -> ``(decider, policy)`` for the audit: the ``final`` model of that registry version. An
    ``untunable`` policy (or none) gives ``None``: R3 is then not measurable. Cached by version."""

    def __init__(self, registry: Any, label: LoadedLabelConfig) -> None:
        self._registry, self._label = registry, label
        self._cache: dict[str, tuple[Any, PolicyConfig | None]] = {}

    def __call__(self, version: str) -> tuple[Any, PolicyConfig | None]:
        if version not in self._cache:
            from modules.decision.baseline import BaselineDecider
            from modules.decision.questions import build_questions

            path = Path(self._registry.get_version(version).path)
            cfg = self._label.cfg
            questions = build_questions(cfg.horizon_min, cfg.k_tp, cfg.k_sl, cfg.atr_tf)
            decider = BaselineDecider.load(path, label=self._label, questions=questions, subset="final")
            self._cache[version] = (decider, _load_policy(path, "final", self._label))
        return self._cache[version]


def note_text(doc: Mapping[str, Any], created: str) -> str:
    """The note for ``tasks/todo.md`` (spec P1e-bis 3.3), generated from the document: no number is copied by hand.
    Only for a certification whose criterion is ``pass`` and whose re-read is identical to the previous run for every
    product (otherwise the numeric limits it states are not valid): ``ValueError`` in any other case."""
    if doc.get("p0_bars_criterion") != "pass":
        raise ValueError("the note is written only for a passed certification")
    for product, sec in doc["products"].items():
        same = ((sec.get("diagnostic") or {}).get("reread_vs_previous") or {}).get("same")
        if same is not True:
            raise ValueError(f"the re-read of {product} is not identical to the previous run")
    eth = doc["products"].get("ETH-EUR") or {}
    eth_block = max((b["shifted"] for b in eth.get("blocks", [])), default=0)
    rule = doc["criterion"]["r1_r3_unit"]
    return (
        f"- [x] P0, criterio barre REST: certificato il {created} in `data/reports/p0_soak_certification.v2.json`\n"
        "  (`chronos.p0_bars_certification.v2`) con la regola R1/R3 v2 a blocchi di 1.440 stati consecutivi "
        "(giorno UTC intero =\n"
        f"  blocco). La regola v2 e' stata fissata il {rule['decided_on']}, DOPO il primo calcolo "
        "(`data/reports/p0_soak_certification.json`,\n"
        '  2026-10-23 06:17Z, `fail` con la regola v1 "R1 per giorno UTC, anche parziale": ETH-EUR 2026-10-20, 2 stati '
        "spostati su\n"
        "  343 = 0,583%), perche' un giorno parziale e' un test con meno stati di quello del gate di produzione (1.440 "
        "per giorno):\n"
        "  con 343 stati e revisioni che spostano 2 stati ciascuna, la quota 0,005 non ammette nessuna revisione. "
        "Sugli stessi dati\n"
        "  l'esito e' `fail` con la v1 e `pass` con la v2. Esito v2: R1, R2 e R4 superati per BTC-EUR, ETH-EUR e "
        "SOL-EUR; R3 non\n"
        "  misurata (policy non tarabile), rinviata al G-DRY-OPS del primo champion tarato. Limiti: ETH-EUR ha 7 stati "
        "spostati in\n"
        "  tutta la finestra, quanti ne ammette un solo blocco di 1.440 stati: margine zero stati (blocco peggiore "
        f"{eth_block}/1.440);\n"
        "  il limite superiore al 95% della quota vera di ETH-EUR e' 0,54-0,64% con revisioni che spostano 2 stati "
        "(0,41% se\n"
        "  indipendenti), sopra il budget di 0,5%: il soak non mostra una violazione ma non la esclude; "
        "`rest_revision_ratio` grezzo\n"
        "  di ETH-EUR 1,13% (allarme, diagnostica). Il verdetto vale per questa pipeline delle barre e per questo "
        "modello\n"
        "  (`baseline-lgbm@2026-10-18.2`, subset final), non per modelli futuri, che R1-R4 rimisurano in G-DRY-OPS.\n"
        "  `data/reports/p0_soak.json` e `data/reports/p0_soak_certification.json` restano invariati. Decisione:\n"
        "  `docs/decisions/2026-10-20-revisioni-rest-e-promozione.md` (sez. P1e-bis)."
    )


def summary(doc: Mapping[str, Any]) -> dict[str, Any]:
    """What ``--info-only`` prints: the outcome under the rule v2 and v1 and the numbers behind it."""
    products = {}
    for product, sec in doc["products"].items():
        diag = sec["diagnostic"]
        products[product] = {
            "R1": sec["R1"], "R2": sec["R2"], "R3": sec["R3"], "R4": sec["R4"]["status"], "states": sec["states"],
            "states_shifted": sec["states_shifted"], "max_shift": sec["max_shift"],
            "blocks": [(b["day"], b["kind"], b["states"], b["shifted"], round(b["share"], 5)) for b in sec["blocks"]],
            "v1_days": [(r["day"], r["states"], r["shifted"], r["status"]) for r in diag["r1_calendar_days_v1"]],
            "r1_window": diag["r1_window"], "r1_rolling_max": diag["r1_rolling_max"],
            "revisions_independent": diag["revisions_independent"], "max_cluster": diag["max_cluster"],
            "r2_utilization": diag["r2_utilization"], "ratio": diag["ratio"],
            "reread_vs_previous_same": (diag["reread_vs_previous"] or {}).get("same"),
        }  # fmt: skip
    return {
        "mode": "info-only" if all(s["diagnostic"]["reread"] is None for s in doc["products"].values()) else "written",
        "rule_v2": doc["p0_bars_criterion"],
        "rule_v1": doc["p0_bars_criterion_rule_v1"],
        "products": products,
    }


def main(argv: Sequence[str] | None = None, *, source: CandleSource | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m modules.dryrun.revision_impact", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("certify", help="R1-R4 on a soak (reads the store and public REST candles)")
    c.add_argument("--soak-report", default="data/reports/p0_soak.json")
    c.add_argument("--model-dir", required=True)
    c.add_argument("--subset", default="final")
    c.add_argument("--out", default=None, help="NEW file (never rewritten); required unless --info-only")
    c.add_argument("--previous", default=None, help="an earlier certification of the same soak and model (read only)")
    c.add_argument(
        "--info-only", action="store_true", help="print the outcome under the rules v1 and v2, write nothing"
    )
    c.add_argument("--bars-root", default="data/bars/1m")
    c.add_argument("--label-config", default="data/datasets/label_config.json")
    c.add_argument("--config", default=None, help="YAML config for risk and gate_ops thresholds (default: built-in)")
    args = ap.parse_args(argv)
    from modules.config import AppConfig, ConfigError, load_config

    try:
        app = load_config(args.config) if args.config else AppConfig()
        src = source
        if src is None:
            from modules.market.backfill import CoinbaseCandleSource

            src = CoinbaseCandleSource()
        doc, code = certify(
            soak_report=Path(args.soak_report), model_dir=Path(args.model_dir), subset=args.subset,
            out=None if args.out is None else Path(args.out), bars_root=Path(args.bars_root),
            label_path=Path(args.label_config), source=src, risk=app.risk, gate=app.dry_run.gate_ops,
            now=datetime.now(UTC), previous=None if args.previous is None else Path(args.previous),
            info_only=args.info_only,
        )  # fmt: skip
    except (ConfigurationError, ConfigError, OSError, ValueError) as exc:
        print(f"certify: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    if args.info_only:
        print(json.dumps(summary(doc), indent=1, default=str))
    else:
        print(json.dumps({"p0_bars_criterion": doc["p0_bars_criterion"], "rule": "v2", "out": args.out}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
