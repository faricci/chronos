"""Dataset builder (spec sez. 3.5 punto 4, decision O3.9): bars -> indicators -> labels -> states -> splits -> files.

::

    python -m training.build_dataset --config config/train.yml [--tokenizer <id|path>] [--no-jsonl]

Per product the indicators are computed on the whole history (``compute_indicators``, anchor = first bar,
``indicator_anchor`` in the manifest), samples are the hourly grid plus the CUSUM events, labels come from
``triple_barrier`` / ``forward_rv_bucket`` / ``synthesize_long_samples`` and states from the SAME ``StateBuilder`` and
``serialize_state`` the runtime uses. Everything that changes a label comes from ``label_config.json`` (verified by
``load_label_config``); nothing here defines a barrier, a fee or a spread.

Output in ``<datasets_dir>/<run_id>/``: ``manifest.json``, ``label_config.json`` (byte copy), ``samples.parquet``
(input of ``baseline_lgbm``) and, unless ``--no-jsonl``, ``train/val/test.jsonl`` in the format of sez. 3.2.1
(``test`` = holdout).
Sentiment: no news history exists yet, so ``sentiment`` is ``null`` in every state and the manifest says so.

Live labels (``--live-labels data/live_labels [--live-weight W] [--live-cutoff ISO]``, spec 3.1.5 / 5.1.5 / 5.1.6 L3):
the labels the ``LabelResolver`` wrote for the decisions of the dry run enter as FLAT rows with ``source = 'live'``,
weight = uniqueness weight x ``live_weight`` (before the per-model normalisation to mean 1 of ``baseline_lgbm``, so the
OOF calibration ``N_eff`` counts them with their real weight). They obey the purge and the embargo like any sample and
are TRAINING rows only (``FlatSplits``): never validation, never test, never holdout; whatever cannot be one is counted
by reason in ``manifest['live']['excluded']``. Without live labels the output is unchanged byte for byte.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import numpy as np
import pandas as pd

from modules.config import EXIT_CONFIG_ERROR, ConfigError
from modules.decision.protocol import ACTIONS, REGIME_LEGEND
from modules.decision.questions import build_questions
from modules.features.schemas import STATE_VERSION, DecisionState
from modules.features.serializer import FLAT_COLUMNS, flatten_state, serialize_state, state_sha256
from modules.features.state_builder import PositionInput, StateBuilder
from modules.market.backfill import bars_root, load_bars
from modules.market.indicators import IndicatorSnapshot
from modules.market.indicators_batch import compute_indicators
from modules.market.timeframes import aggregate_bars
from modules.risk.costs import round_bps
from modules.risk.label_config import LabelConfig, LoadedLabelConfig, load_label_config
from training.config import TrainConfig, load_train_config_or_exit
from training.labeling import (
    NO_FILL,
    UNRESOLVED,
    forward_rv_bucket,
    label_window_end,
    synthesize_long_samples,
    triple_barrier,
)
from training.sampling import sample_indices, uniqueness_weights
from training.splits import Splits, final_fit_split, purged_walk_forward

OUTCOME_NAMES: Final = {0: "NO_FILL", 1: "UPPER", 2: "LOWER", 3: "VERTICAL"}
FOLD_NONE: Final = -1
FOLD_HOLDOUT: Final = -2
SOURCE_HIST: Final = "hist"
SOURCE_LIVE: Final = "live"
LABEL_TOKENS: Final = {
    "choice": [" A", " B", " C"],
    "noul": [" Y", " N"],
    "score": [" 0", " 1", " 2", " 3", " 4"],
}  # the label tokens of PROMPT_TEMPLATE_V1 (sez. 3.2.2): each must be a single token of the base model


def _naive(ts: pd.DatetimeIndex) -> np.ndarray:
    """UTC instants as naive ``datetime64[ns]`` (a tz-aware ``to_numpy`` would give an object array)."""
    return np.asarray(ts.tz_convert(None).to_numpy() if ts.tz is not None else ts.to_numpy())


class DatasetError(Exception):
    """The inputs cannot produce a valid dataset (exit code 1)."""


def check_label_tokens(tokenizer: Any) -> None:
    """Fail when a label token is not a single id for the tokenizer of the base model (sez. 3.2.2)."""
    for kind, tokens in LABEL_TOKENS.items():
        for tok in tokens:
            ids = tokenizer.encode(tok, add_special_tokens=False)
            if len(ids) != 1:
                raise DatasetError(f"label token {tok!r} of '{kind}' is {len(ids)} tokens for this tokenizer")


# ------------------------------------------------------------------------------------------ per product


@dataclass(frozen=True)
class ProductFrames:
    name: str
    bars: pd.DataFrame
    ind: pd.DataFrame

    @classmethod
    def load(cls, name: str, bars: pd.DataFrame) -> ProductFrames:
        return cls(name, bars, compute_indicators(bars))


def _snapshots(frames: ProductFrames, rows: np.ndarray) -> list[IndicatorSnapshot]:
    cols = list(frames.ind.columns)
    values = frames.ind.to_numpy(dtype=np.float64)
    idx = frames.ind.index
    return [
        IndicatorSnapshot.from_row(frames.name, idx[i].to_pydatetime(), dict(zip(cols, values[i], strict=True)))
        for i in rows
    ]


def _btc_rows(own: ProductFrames, btc: ProductFrames | None, rows: np.ndarray) -> np.ndarray:
    """Row of the BTC frame for every sample (by timestamp), -1 where BTC has no such bar."""
    if btc is None or own.name.startswith("BTC-"):
        return np.full(len(rows), -1, dtype=np.int64)
    return btc.ind.index.get_indexer(own.ind.index[rows])


def build_product_rows(
    frames: ProductFrames,
    btc: ProductFrames | None,
    label: LoadedLabelConfig,
    builder: StateBuilder,
    seed: int,
    product_index: int,
) -> pd.DataFrame:  # fmt: skip
    """All FLAT and synthetic LONG rows of one product (see the module docstring)."""
    cfg = label.cfg
    bars, name = frames.bars, frames.name
    idx = pd.DatetimeIndex(bars.index)
    n = len(bars)
    o, h, lo, c = (bars[k].to_numpy(dtype=np.float64) for k in ("open", "high", "low", "close"))
    close_ts = idx + pd.Timedelta(minutes=1)
    atr = frames.ind[cfg.atr_column].to_numpy(dtype=np.float64)
    atr_bps_1h = frames.ind["atr_14_1h_bps"].to_numpy(dtype=np.float64)

    log_ret = np.concatenate([[np.nan], np.diff(np.log(c))])
    cand = sample_indices(idx, log_ret, atr_bps_1h / 1e4)
    end_all = label_window_end(close_ts[cand], cfg)
    keep = np.isfinite(atr[cand]) & (end_all <= close_ts[-1])
    t_idx = cand[keep]
    if t_idx.size == 0:
        raise DatasetError(f"{name}: no resolvable sample")
    ts_close = close_ts[t_idx]

    def spread_at(bar_idx: np.ndarray) -> np.ndarray:
        return np.array([label.spread_for(name, close_ts[int(i)].to_pydatetime()) for i in bar_idx], dtype=np.float64)

    spread = spread_at(t_idx)
    res = triple_barrier(o, h, lo, c, atr, t_idx, cfg, label.fees, label.costs, spread)
    resolved = res.outcome != UNRESOLVED
    if not resolved.all():
        raise DatasetError(f"{name}: {int((~resolved).sum())} unresolved samples inside the label window")

    close_1h = aggregate_bars(bars, "1h")["close"]
    vol = forward_rv_bucket(close_1h, close_ts[t_idx], cfg.horizon_min).astype(np.int64)

    # ---- uniqueness weights (raw; normalised per model inside each training set)
    filled = res.outcome != NO_FILL
    t64 = t_idx.astype(np.int64)
    entry_span_end = t64 + cfg.entry_first_bar + cfg.entry_timeout_min - 1
    action_end = np.where(filled, res.exit_idx.astype(np.int64), entry_span_end)
    w_action = uniqueness_weights(t64, action_end)
    w_edge = np.full(len(t_idx), np.nan)
    if filled.any():
        w_edge[filled] = uniqueness_weights(t64[filled], res.exit_idx[filled].astype(np.int64))
    win_end = label_window_end(ts_close, cfg)
    end_idx = np.minimum(np.rint((win_end - close_ts[0]) / pd.Timedelta(minutes=1)).astype(np.int64), n - 1)
    w_vol = np.full(len(t_idx), np.nan)
    has_vol = vol >= 0
    if has_vol.any():
        w_vol[has_vol] = uniqueness_weights(t64[has_vol], np.maximum(end_idx[has_vol], t64[has_vol]))

    # ---- targets
    action_target = np.where(filled & (res.outcome == 1) & (res.net_ret > 0), "BUY", "HOLD")
    edge_target = np.where(filled, (res.outcome == 1).astype(float), np.nan)

    # ---- states
    btc_rows = _btc_rows(frames, btc, t_idx)
    snaps = _snapshots(frames, t_idx)
    btc_snaps = _snapshots(btc, btc_rows[btc_rows >= 0]) if btc is not None else []
    btc_iter = iter(btc_snaps)
    btc_of = [next(btc_iter) if r >= 0 else None for r in btc_rows]
    flat_states = [
        builder.build(snapshot=s, btc=b, sentiment=None, position=PositionInput.flat(), close_t=float(c[i]))
        for s, b, i in zip(snaps, btc_of, t_idx, strict=True)
    ]

    rng = np.random.default_rng([seed, product_index])
    long = synthesize_long_samples(o, h, lo, c, atr, t_idx, cfg, label.fees, label.costs, spread, spread_at, rng)
    k = long.keep
    w_long = uniqueness_weights(t64[k], long.exit_idx_br.astype(np.int64)) if k.size else np.empty(0)
    long_states = []
    for j, pos in enumerate(k):
        a_e = float(atr[long.t_e[j]])
        entry = float(long.entry_px[j])
        pin = PositionInput(
            "LONG",
            entry,
            entry - cfg.k_sl * a_e,
            entry + cfg.k_tp * a_e,
            a_e,
            idx[int(long.fill_idx[j])].to_pydatetime(),
        )
        long_states.append(
            builder.build(
                snapshot=snaps[pos], btc=btc_of[pos], sentiment=None, position=pin, close_t=float(c[t_idx[pos]])
            )
        )

    def frame(states: Sequence[Any], side: str, pos: np.ndarray) -> pd.DataFrame:
        flat = pd.DataFrame([flatten_state(s) for s in states], columns=list(FLAT_COLUMNS))
        strings = [serialize_state(s) for s in states]
        df = pd.DataFrame(
            {
                "product": name,
                "t": _naive(ts_close[pos]),
                "t_idx": t_idx[pos],
                "position_side": side,
                "state": strings,
                "state_sha256": [state_sha256(x) for x in strings],
            }
        )
        return pd.concat([df, flat], axis=1)

    all_pos = np.arange(len(t_idx))
    flat_df = frame(flat_states, "FLAT", all_pos)
    flat_df["action_target"] = action_target
    flat_df["edge_target"] = edge_target
    flat_df["vol_target"] = np.where(has_vol, vol, -1)
    flat_df["outcome"] = res.outcome.astype(np.int8)
    flat_df["fill_idx"] = res.fill_idx
    flat_df["exit_idx"] = res.exit_idx
    flat_df["net_ret_bps"] = 1e4 * res.net_ret
    flat_df["label_window_end"] = _naive(win_end)
    flat_df["w_action"], flat_df["w_edge"], flat_df["w_vol"] = w_action, w_edge, w_vol

    parts = [flat_df]
    if k.size:
        long_df = frame(long_states, "LONG", k)
        long_df["action_target"] = np.where(long.sell, "SELL", "HOLD")
        long_df["edge_target"] = edge_target[k]
        long_df["vol_target"] = np.where(has_vol, vol, -1)[k]
        long_df["outcome"] = res.outcome[k].astype(np.int8)
        long_df["fill_idx"], long_df["exit_idx"] = res.fill_idx[k], res.exit_idx[k]
        long_df["net_ret_bps"] = 1e4 * res.net_ret[k]
        long_df["label_window_end"] = _naive(win_end)[k]
        long_df["w_action"], long_df["w_edge"], long_df["w_vol"] = w_long, w_edge[k], w_vol[k]
        long_df["t_e"], long_df["entry_px"] = long.t_e, long.entry_px
        long_df["stop_loss"] = long.entry_px - cfg.k_sl * atr[long.t_e]
        long_df["take_profit"] = long.entry_px + cfg.k_tp * atr[long.t_e]
        long_df["atr_entry"] = atr[long.t_e]
        long_df["opened_at"] = _naive(idx[long.fill_idx.astype(np.int64)])
        long_df["fill_idx_br"] = long.fill_idx
        long_df["outcome_br"], long_df["exit_idx_br"] = long.outcome_br.astype(np.int8), long.exit_idx_br
        parts.append(long_df)
    return pd.concat(parts, ignore_index=True)


# -------------------------------------------------------------------------------------------- splits


@dataclass(frozen=True)
class FlatSplits:
    """The purged splits of the FLAT rows (sorted by ``(t, product)``) and which of them are live rows.

    THE rule for live rows (spec 3.1.5 / 5.1.6 L3, decision of this module): a live row is a sample like any other for
    the PURGE (its ``[t, label_window_end]`` must end before the boundary of the next set minus the embargo), but it is
    only ever a TRAINING row. It is never a validation row (validation fits calibration and the early stop: L1 says the
    live labels measure the calibration, they do not shape it), never a test row and never a holdout row (the yardsticks
    stay the historical, frozen ones), and the splits of the historical rows do not move when live rows are added (the
    live rows are later than the first historical sample, so the first development sample and the block length stay
    the same). ``train_only`` is the one place that says so; ``assign_splits`` and ``baseline_lgbm.set_membership``
    use it.
    """

    splits: Splits
    final_train: np.ndarray
    final_val: np.ndarray
    live: np.ndarray  # bool per flat row

    def train_only(self, tag: np.ndarray) -> np.ndarray:
        """``tag`` ('train' / 'val' / 'test' / '' per flat row) with the live rows that are not 'train' blanked."""
        out = tag.copy()
        out[self.live & (out != "train")] = ""
        return out

    def live_in_train(self) -> dict[str, np.ndarray]:
        """Per subset (``fold_<k>`` and ``final``) the boolean mask of the live rows that train it."""
        out = {f"fold_{f.index}": np.zeros(len(self.live), dtype=bool) for f in self.splits.folds}
        for f in self.splits.folds:
            out[f"fold_{f.index}"][f.train] = True
        final = np.zeros(len(self.live), dtype=bool)
        final[self.final_train] = True
        out["final"] = final
        return {k: v & self.live for k, v in out.items()}


def flat_splits(flat: pd.DataFrame, label: LoadedLabelConfig, cfg: TrainConfig) -> FlatSplits:
    """``flat``: the FLAT rows already sorted by ``["t", "product"]`` (stable)."""
    hs = label.holdout_start
    ts = pd.DatetimeIndex(flat["t"]).tz_localize("UTC")
    t1 = pd.DatetimeIndex(flat["label_window_end"]).tz_localize("UTC")
    embargo = timedelta(days=cfg.embargo_days)
    splits = purged_walk_forward(ts, t1, n_folds=cfg.n_folds, embargo=embargo, val_frac=cfg.val_frac, holdout_start=hs)
    train, val = final_fit_split(ts, t1, hs, cfg.val_frac, embargo)
    live = (
        (flat["source"] == SOURCE_LIVE).to_numpy(dtype=bool)
        if "source" in flat.columns
        else np.zeros(len(flat), dtype=bool)
    )
    return FlatSplits(splits, train, val, live)


def assign_splits(
    rows: pd.DataFrame, label: LoadedLabelConfig, cfg: TrainConfig, live_stats: dict[str, int] | None = None
) -> pd.DataFrame:
    """Add ``fold_test`` and ``final_split``; LONG rows inherit the FLAT row of the same (product, t).

    Live rows (``source == 'live'``, see ``FlatSplits``) get ``fold_test = -1`` and ``final_split`` 'train' or
    'purged'; a live row that trains no subset at all (not a fold, not the final model) is dropped from the result.
    ``live_stats`` (optional) receives ``{'fold_<k>': n, ..., 'final': n, 'purged': n}``: the live rows that train each
    subset and the ones dropped.
    """
    flat = rows[rows["position_side"] == "FLAT"].sort_values(["t", "product"], kind="stable")
    fs = flat_splits(flat, label, cfg)
    splits = fs.splits
    fold = np.full(len(flat), FOLD_NONE, dtype=np.int64)
    for f in splits.folds:
        fold[f.test] = f.index
    fold[splits.holdout] = FOLD_HOLDOUT
    final = np.full(len(flat), "purged", dtype=object)
    final[fs.final_train], final[fs.final_val] = "train", "val"
    final[splits.holdout] = "holdout"
    fold[fs.live], final[fs.live & (final != "train")] = FOLD_NONE, "purged"
    keyed = pd.DataFrame({"product": flat["product"].to_numpy(), "t": flat["t"].to_numpy(), "fold_test": fold,
                          "final_split": final})  # fmt: skip
    out = rows.merge(keyed, on=["product", "t"], how="left", validate="many_to_one")
    if fs.live.any():
        trains = fs.live_in_train()
        used = np.any(list(trains.values()), axis=0)
        drop = keyed[fs.live & ~used][["product", "t"]].assign(_drop=True)
        out = out.merge(drop, on=["product", "t"], how="left")
        out = out[out.pop("_drop").isna()].reset_index(drop=True)
        if live_stats is not None:
            live_stats.update({k: int(v.sum()) for k, v in trains.items()})
            live_stats["purged"] = int((fs.live & ~used).sum())
    return out


# ------------------------------------------------------------------------------------------ live labels
#
# ``data/live_labels/*.jsonl`` (``LabelResolver``): one line per (decision, question), the state being the one the
# engine recorded, i.e. the production state. A decision becomes ONE row of the dataset with the columns and the
# semantics of a historical FLAT row (the targets are the lines' ones, the state goes through the same
# ``DecisionState`` / ``serialize_state`` / ``flatten_state`` as the historical rows), ``source = 'live'`` and weights
# = uniqueness weight x ``live_weight``. What does not become a row is counted by reason in the manifest.

_OUTCOME_CODES: Final = {name: code for code, name in OUTCOME_NAMES.items()}
LIVE_EXCLUSIONS: Final = (
    "duplicate_book",  # another book decided at the same (product, instant): same market, same labels: one row
    "not_flat",  # a LONG state: its `action` needs the real bracket, which a DecisionRecord does not carry
    "incomplete",  # no action line, or a filled outcome without edge / minutes
    "unknown_product",
    "label_config_mismatch",  # labelled with another label_config (another barrier, fee or spread)
    "bad_state",  # not a valid DecisionState of this schema, or of another product / side
    "bar_missing",  # the decision bar or the exit bar is not in the bars given to the builder
    "window_after_cutoff",  # label_window_end > cutoff: the label looks at data the refit may not see
    "duplicate_historical",  # the historical dataset already has a row for this (product, instant)
    "holdout_period",  # t >= holdout_start: the instant belongs to the holdout, a live row never enters it
    "before_history",  # earlier than the first historical sample (it would move the fold boundaries)
    "purged",  # in the development period but in no training set of any fold nor of the final model
)
_ID_HEAD: Final = '{"id":"'


@dataclass(frozen=True)
class LiveInput:
    """The live labels of a build: their folder, ``dry_run.learner.live_weight`` and the optional cutoff (UTC)."""

    labels_dir: Path
    weight: float
    cutoff: datetime | None = None

    def __post_init__(self) -> None:
        if not (math.isfinite(self.weight) and self.weight > 0.0):
            raise DatasetError(f"live_weight must be a positive number, got {self.weight!r}")


@dataclass(frozen=True)
class _Candidate:
    product: str
    ts: pd.Timestamp  # close of the decision bar, tz-aware UTC
    state: str
    flat: list[float]
    action: str
    edge: float
    vol: int
    outcome: int
    fill_minute: int
    exit_minute: int
    net_bps: float


def _ns_ints(ts: pd.DatetimeIndex) -> np.ndarray:
    """Naive UTC instants as integer nanoseconds, whatever the resolution of the index (keys of the duplicate test)."""
    return np.asarray(_naive(ts.as_unit("ns")).astype(np.int64))


LiveGroups = dict[tuple[str, str, str], dict[str, dict[str, Any]]]


def _line_id(raw: str) -> str | None:
    """The ``id`` of a label line without parsing it (the resolver writes it first), None for any other shape."""
    if not raw.startswith(_ID_HEAD):
        return None
    end = raw.find('"', len(_ID_HEAD))
    return raw[len(_ID_HEAD) : end] if end > 0 else None


def read_live_groups(labels_dir: Path) -> tuple[LiveGroups, dict[str, int]]:
    """Lines of ``labels_dir/*.jsonl`` grouped by decision instant: ``(product, t, side) -> {question_key: line}``.

    Several books of the dry run decide at the same instant of the same product and get the same labels: the first line
    seen (files in name order) wins and the others count as ``duplicate_book``.
    """
    if not labels_dir.is_dir():
        raise DatasetError(f"live labels folder {labels_dir} does not exist")
    groups: LiveGroups = {}
    seen: set[str] = set()
    counts = {"lines": 0, "bad_lines": 0, "duplicate_book": 0}
    for path in sorted(labels_dir.glob("*.jsonl")):
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                if not raw.strip():
                    continue
                counts["lines"] += 1
                ident = _line_id(raw)
                if ident is not None and ident in seen:
                    counts["duplicate_book"] += ident.endswith("|action")
                    continue
                try:
                    rec = json.loads(raw)
                    ident = ident or str(rec["id"])
                    key = (str(rec["product"]), str(rec["t"]), str(rec["position_side"]))
                    question = str(rec["question_key"])
                    live = rec["split"] == SOURCE_LIVE
                except (ValueError, KeyError, TypeError):
                    counts["bad_lines"] += 1  # a torn last line, or a record of another shape
                    continue
                if not live:
                    counts["bad_lines"] += 1
                    continue
                seen.add(ident)
                groups.setdefault(key, {})[question] = rec
    return groups, counts


def _candidate(
    key: tuple[str, str, str], lines: Mapping[str, Mapping[str, Any]], label: LoadedLabelConfig, products: set[str]
) -> _Candidate | str:
    """The decision as a candidate row, or the reason (a ``LIVE_EXCLUSIONS`` name) why it is not one."""
    product, t_iso, side = key
    if side != "FLAT":
        return "not_flat"
    action = lines.get("action")
    if action is None:
        return "incomplete"
    if product not in products:
        return "unknown_product"
    meta = action.get("meta") or {}
    if meta.get("label_config_sha256") != label.sha256:
        return "label_config_mismatch"
    try:
        outcome = _OUTCOME_CODES[str(meta["barrier_outcome"])]
        a_target = str(action["target"])
        filled = outcome != NO_FILL
        edge = lines.get("edge")
        vol = lines.get("vol_regime")
        net = meta.get("net_ret_bps")
        fill_minute = int(meta["fill_minute"]) if filled else -1
        exit_minute = int(meta["exit_minute"]) if filled else -1
        edge_target = float(edge["target"] == "true") if edge is not None else float("nan")
        vol_target = int(vol["target"]) if vol is not None else -1
        ts = pd.Timestamp(t_iso)
        ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
        net_bps = float("nan") if net is None else float(net)
    except (KeyError, TypeError, ValueError):
        return "incomplete"
    if a_target not in ("BUY", "HOLD"):
        return "incomplete"
    if filled and (edge is None or fill_minute < 0 or exit_minute < fill_minute or np.isnan(net_bps)):
        return "incomplete"  # a filled order has an edge label, both minutes and a net return
    if not filled and (edge is not None or not np.isnan(net_bps)):
        return "incomplete"  # NO_FILL has neither (the edge question is conditional on the fill)
    try:
        state = DecisionState.model_validate_json(str(action["state"]))
    except (KeyError, ValueError):
        return "bad_state"
    if state.product != product or state.position.side != "FLAT":
        return "bad_state"
    flat = flatten_state(state)
    return _Candidate(
        product, ts, serialize_state(state), [flat[c] for c in FLAT_COLUMNS], a_target, edge_target, vol_target,
        outcome, fill_minute, exit_minute, net_bps,
    )  # fmt: skip


def weight_spans(
    cfg: LabelConfig,
    t_idx: np.ndarray,
    outcome: np.ndarray,
    exit_idx: np.ndarray,
    vol_target: np.ndarray,
    window_end: np.ndarray,
    close0: np.datetime64,
    n_bars: int,
) -> dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """``{question: (applies, first, last)}`` bar-index spans of FLAT rows, as ``build_product_rows`` defines them
    (spec 3.1.4): ``[t, exit]`` for a filled ``edge`` and ``action``, ``[t, t + F + T_in - 1]`` for a ``NO_FILL``
    ``action``, ``[t, label_window_end]`` for ``vol_regime``. The one place the live rows use to weigh themselves
    against the historical rows; the tests pin it to the weights ``build_product_rows`` computed."""
    t64 = t_idx.astype(np.int64)
    filled = outcome != NO_FILL
    action_end = np.where(filled, exit_idx.astype(np.int64), t64 + cfg.entry_first_bar + cfg.entry_timeout_min - 1)
    end_idx = np.minimum(np.rint((window_end - close0) / np.timedelta64(1, "m")).astype(np.int64), n_bars - 1)
    return {
        "action": (np.ones(len(t64), dtype=bool), t64, action_end),
        "edge": (filled, t64, exit_idx.astype(np.int64)),
        "vol": (vol_target >= 0, t64, np.maximum(end_idx, t64)),
    }


def _live_frame(
    cands: Sequence[_Candidate], ts_close: pd.DatetimeIndex, t_idx: np.ndarray, window_end: np.ndarray
) -> pd.DataFrame:
    """The live candidates of one product as rows with the dtypes of the historical FLAT rows (weights come later)."""
    outcome = np.array([c.outcome for c in cands], dtype=np.int8)
    fill = np.array([c.fill_minute for c in cands], dtype=np.int64)
    exit_ = np.array([c.exit_minute for c in cands], dtype=np.int64)
    filled = outcome != NO_FILL
    states = [c.state for c in cands]
    df = pd.DataFrame(
        {
            "product": cands[0].product,
            "t": _naive(ts_close),
            "t_idx": t_idx,
            "position_side": "FLAT",
            "state": states,
            "state_sha256": [state_sha256(x) for x in states],
        }
    )
    flat = pd.DataFrame([c.flat for c in cands], columns=list(FLAT_COLUMNS))
    df = pd.concat([df, flat], axis=1)
    df["action_target"] = [c.action for c in cands]
    df["edge_target"] = [c.edge for c in cands]
    df["vol_target"] = np.array([c.vol for c in cands], dtype=np.int64)
    df["outcome"] = outcome
    df["fill_idx"] = np.where(filled, t_idx + fill, -1).astype(np.int32)
    df["exit_idx"] = np.where(filled, t_idx + exit_, -1).astype(np.int32)
    df["net_ret_bps"] = [c.net_bps for c in cands]
    df["label_window_end"] = window_end
    return df


def build_live_rows(
    groups: LiveGroups,
    frames: Mapping[str, ProductFrames],
    label: LoadedLabelConfig,
    hist: pd.DataFrame,
    live: LiveInput,
    counts: dict[str, int],
) -> pd.DataFrame:
    """The live rows of the dataset, weights included; ``counts`` gets one entry per ``LIVE_EXCLUSIONS`` reason.

    Weight of a live row = its average-uniqueness weight x ``live.weight``, with the concurrency counted over the
    historical FLAT rows of the product AND the live rows (they look at the same market path: a live row inside a
    window already covered by the historical rows is mostly not new information). The historical weights are NOT
    recomputed: they must not depend on whether live labels exist (the validation, the test and the holdout are
    measured with them).
    """
    cfg = label.cfg
    for reason in LIVE_EXCLUSIONS:
        counts.setdefault(reason, 0)
    by_product: dict[str, list[_Candidate]] = {p: [] for p in frames}
    for key in sorted(groups, key=lambda k: (k[0], k[1], k[2])):
        got = _candidate(key, groups[key], label, set(frames))
        if isinstance(got, str):
            counts[got] += 1
        else:
            by_product[got.product].append(got)
    hist_flat = hist[hist["position_side"] == "FLAT"]
    hist_ns = _ns_ints(pd.DatetimeIndex(hist_flat["t"])).tolist()
    hist_keys = set(zip(hist_flat["product"].tolist(), hist_ns, strict=True))
    first_t = hist_flat["t"].min()
    hold_t = _naive(pd.DatetimeIndex([label.holdout_start]))[0]
    cutoff = None
    if live.cutoff is not None:
        c = pd.Timestamp(live.cutoff)
        cutoff = _naive(pd.DatetimeIndex([c.tz_localize("UTC") if c.tzinfo is None else c.tz_convert("UTC")]))[0]
    out: list[pd.DataFrame] = []
    for product in frames:
        cands = by_product[product]
        if not cands:
            continue
        bars = frames[product].bars
        idx = pd.DatetimeIndex(bars.index)
        n = len(idx)
        ts_close = pd.DatetimeIndex([c.ts for c in cands]).as_unit(idx.unit)  # the resolution of the historical rows
        t_idx = idx.get_indexer(ts_close - pd.Timedelta(minutes=1)).astype(np.int64)
        fill_exit = np.array([c.exit_minute for c in cands], dtype=np.int64)
        outcome = np.array([c.outcome for c in cands], dtype=np.int8)
        window_end = _naive(label_window_end(ts_close, cfg))
        t_naive = _naive(ts_close)
        bad: list[tuple[str, np.ndarray]] = [
            ("bar_missing", (t_idx < 0) | ((outcome != NO_FILL) & (t_idx + fill_exit > n - 1))),
            ("window_after_cutoff", np.zeros(len(cands), bool) if cutoff is None else window_end > cutoff),
            ("duplicate_historical", np.array([(product, int(t)) in hist_keys for t in _ns_ints(ts_close)])),
            ("holdout_period", t_naive >= hold_t),
            ("before_history", t_naive < first_t.to_datetime64()),
        ]  # fmt: skip
        alive = np.ones(len(cands), dtype=bool)
        for reason, mask in bad:
            hit = mask & alive
            counts[reason] += int(hit.sum())
            alive &= ~hit
        if not alive.any():
            continue
        keep = np.flatnonzero(alive)
        df = _live_frame([cands[i] for i in keep], ts_close[keep], t_idx[keep], window_end[keep])
        close0 = _naive(idx[:1] + pd.Timedelta(minutes=1))[0]
        h = hist_flat[hist_flat["product"] == product]
        sets = {
            "hist": weight_spans(cfg, h["t_idx"].to_numpy(), h["outcome"].to_numpy(), h["exit_idx"].to_numpy(),
                                 h["vol_target"].to_numpy(), h["label_window_end"].to_numpy(dtype="datetime64[ns]"),
                                 close0, n),
            "live": weight_spans(cfg, df["t_idx"].to_numpy(), df["outcome"].to_numpy(), df["exit_idx"].to_numpy(),
                                 df["vol_target"].to_numpy(), df["label_window_end"].to_numpy(dtype="datetime64[ns]"),
                                 close0, n),
        }  # fmt: skip
        for q, col in (("action", "w_action"), ("edge", "w_edge"), ("vol", "w_vol")):
            (m_h, a_h, b_h), (m_l, a_l, b_l) = sets["hist"][q], sets["live"][q]
            w = np.full(len(df), np.nan)
            if m_l.any():
                joint = uniqueness_weights(np.concatenate([a_h[m_h], a_l[m_l]]), np.concatenate([b_h[m_h], b_l[m_l]]))
                w[m_l] = joint[int(m_h.sum()) :] * live.weight
            df[col] = w
        out.append(df)
    if not out:
        return pd.DataFrame()
    return pd.concat(out, ignore_index=True)


# ------------------------------------------------------------------------------------------- outputs


def _git_commit() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10, check=True)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _jsonl_records(rows: pd.DataFrame, label: LoadedLabelConfig, qversion: str) -> dict[str, list[dict[str, Any]]]:
    """Records of sez. 3.2.1 per final split; weights normalised per (question, split) to mean 1."""
    cfg = label.cfg
    out: dict[str, list[dict[str, Any]]] = {"train": [], "val": [], "test": []}
    names = {"train": "train", "val": "val", "holdout": "test"}
    for split, fname in names.items():
        part = rows[rows["final_split"] == split]
        specs = (
            ("action", "choice", list(ACTIONS), "action_target", "w_action"),
            ("edge", "noul", ["true", "false"], "edge_target", "w_edge"),
            ("vol_regime", "score", [str(i) for i in range(len(REGIME_LEGEND))], "vol_target", "w_vol"),
        )
        for key, qtype, options, tcol, wcol in specs:
            sel = part[part[wcol].notna()]
            if key == "edge":
                sel = sel[sel[tcol].notna()]
            if key == "vol_regime":
                sel = sel[sel[tcol] >= 0]
            if sel.empty:
                continue
            mean_w = float(sel[wcol].mean())
            for row in sel.to_dict("records"):
                raw = row[tcol]
                target = (
                    str(raw)
                    if key == "action"
                    else ("true" if raw == 1.0 else "false")
                    if key == "edge"
                    else str(int(raw))
                )
                is_long = row["position_side"] == "LONG"
                stamp = pd.Timestamp(row["t"]).strftime("%Y-%m-%dT%H:%M:%SZ")
                outcome = OUTCOME_NAMES[int(row["outcome_br"] if (is_long and key == "action") else row["outcome"])]
                net = row["net_ret_bps"]
                record: dict[str, Any] = {
                    "id": f"{row['product']}|{stamp}|{row['position_side']}|{key}",
                    "fold": int(row["fold_test"]),
                    "split": fname,
                    "product": row["product"],
                    "t": stamp,
                    "position_side": row["position_side"],
                    "state": row["state"],
                    "question_key": key,
                    "question_type": qtype,
                    "options": options,
                    "target": target,
                    "target_index": options.index(target),
                    "weight": float(row[wcol] / mean_w),
                    "meta": {
                        "barrier_outcome": outcome,
                        "net_ret_bps": None if pd.isna(net) else int(round_bps(float(net))),
                        "fill_minute": None if row["outcome"] == NO_FILL else int(row["fill_idx"] - row["t_idx"]),
                        "exit_minute": None if row["outcome"] == NO_FILL else int(row["exit_idx"] - row["t_idx"]),
                        "atr_tf": cfg.atr_tf,
                        "atr_bps": int(round_bps(float(row[f"atr_14_{cfg.atr_tf}_bps"]))),
                        "label_config_sha256": label.sha256,
                        "questions_version": qversion,
                        "sentiment_dropped": False,
                    },
                }
                if row.get("source") == SOURCE_LIVE:
                    record["source"] = SOURCE_LIVE  # only live records carry the key: the old format is unchanged
                out[fname].append(record)
    return out


def _live_manifest(
    live: LiveInput, read: Mapping[str, int], n_decisions: int, counts: Mapping[str, int],
    in_train: Mapping[str, int], rows: pd.DataFrame,
) -> dict[str, Any]:  # fmt: skip
    """The ``live`` block of the manifest: what was read, what entered (and its weight), what did not and why."""
    mine = rows[rows["source"] == SOURCE_LIVE] if "source" in rows.columns else rows.iloc[0:0]
    return {
        "labels_dir": str(live.labels_dir),
        "weight": live.weight,
        "cutoff": None if live.cutoff is None else pd.Timestamp(live.cutoff).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lines_read": int(read["lines"]),
        "bad_lines": int(read["bad_lines"]),
        "decisions": n_decisions,
        "rows": int(len(mine)),
        "rows_by_product": {str(p): int(n) for p, n in mine["product"].value_counts().sort_index().items()},
        "rows_in_train": {k: int(v) for k, v in sorted(in_train.items())},
        "weight_sums": {
            c: float(np.nansum(mine[c].to_numpy(dtype=np.float64))) if len(mine) else 0.0
            for c in ("w_action", "w_edge", "w_vol")
        },
        "excluded": {k: int(counts.get(k, 0)) for k in LIVE_EXCLUSIONS},
    }


def build_dataset(
    cfg: TrainConfig,
    label: LoadedLabelConfig,
    frames: Mapping[str, pd.DataFrame],
    out_root: Path,
    *,
    label_config_path: Path,
    tokenizer: Any | None = None,
    write_jsonl: bool = True,
    now: datetime | None = None,
    live: LiveInput | None = None,
) -> Path:
    """Build the dataset from the whole-history bars of each product (``load_bars``); returns the run directory.

    ``live`` (default none): the live labels of the dry run (module section "live labels"). Without it, or when no live
    row qualifies, ``samples.parquet`` and the JSONL files are byte for byte what they were before this input existed;
    with it the manifest gets a ``live`` block (rows, weight, rows per training set, exclusions by reason).
    """
    if tokenizer is not None:
        check_label_tokens(tokenizer)
    if Path(cfg.sentiment_history_db).exists():
        raise DatasetError(
            f"{cfg.sentiment_history_db} exists but the historical sentiment reconstruction is not implemented"
        )
    names = [p for p in cfg.products if p in frames]
    if len(names) != len(cfg.products):
        raise DatasetError(f"missing bars for {sorted(set(cfg.products) - set(names))}")
    builder = StateBuilder(label)
    pf = {p: ProductFrames.load(p, frames[p]) for p in names}
    btc = next((pf[p] for p in names if p.startswith("BTC-")), None)
    parts = [build_product_rows(pf[p], btc, label, builder, cfg.seed, i) for i, p in enumerate(names)]
    hist = pd.concat(parts, ignore_index=True)
    live_block: dict[str, Any] | None = None
    live_stats: dict[str, int] = {}
    if live is None:
        rows = assign_splits(hist, label, cfg)
    else:
        groups, read = read_live_groups(live.labels_dir)
        excluded: dict[str, int] = {"duplicate_book": read["duplicate_book"]}
        hist["source"] = SOURCE_HIST
        live_rows = build_live_rows(groups, pf, label, hist, live, excluded)
        if live_rows.empty:
            rows = assign_splits(hist.drop(columns="source"), label, cfg)
        else:
            live_rows["source"] = SOURCE_LIVE
            rows = assign_splits(pd.concat([hist, live_rows], ignore_index=True), label, cfg, live_stats)
            excluded["purged"] = live_stats.pop("purged", 0)
        live_block = _live_manifest(live, read, len(groups), excluded, live_stats, rows)
        if "source" in rows.columns and not (rows["source"] == SOURCE_LIVE).any():
            # nothing live qualified: the dataset is the historical one, rebuilt on the historical rows alone so that
            # it is unchanged to the byte (a concatenation with the dropped live rows may have changed dtypes)
            rows = assign_splits(hist.drop(columns="source"), label, cfg)

    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    run_dir = out_root / f"{stamp}-{label.sha256[:8]}"
    run_dir.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(label_config_path, run_dir / "label_config.json")
    qversion = build_questions(label.cfg.horizon_min, label.cfg.k_tp, label.cfg.k_sl, label.cfg.atr_tf).version_tag
    rows.to_parquet(run_dir / "samples.parquet", index=False)
    base = rows[rows["source"] != SOURCE_LIVE] if "source" in rows.columns else rows  # the historical rows
    counts = base.groupby(["position_side", "final_split"]).size()
    manifest: dict[str, Any] = {
        "run_id": run_dir.name,
        "label_config_sha256": label.sha256,
        "questions_version": qversion,
        "state_schema": STATE_VERSION,
        "flat_columns_sha256": state_sha256(",".join(FLAT_COLUMNS)),
        "products": names,
        "indicator_anchor": {p: pd.Timestamp(frames[p].index[0]).strftime("%Y-%m-%dT%H:%M:%SZ") for p in names},
        "last_bar": {p: pd.Timestamp(frames[p].index[-1]).strftime("%Y-%m-%dT%H:%M:%SZ") for p in names},
        "holdout_start": label.holdout_start.isoformat(),
        "rows": {f"{side}/{split}": int(n) for side, split, n in counts.reset_index().itertuples(index=False)},
        "rows_per_fold_test": {str(k): int(v) for k, v in base["fold_test"].value_counts().sort_index().items()},
        "seed": cfg.seed,
        "sentiment_available": False,
        "label_tokens_checked": tokenizer is not None,
        "git_commit": _git_commit(),
    }
    if live_block is not None:
        manifest["live"] = live_block
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8", newline="\n"
    )
    if write_jsonl:
        for split, records in _jsonl_records(rows, label, qversion).items():
            with (run_dir / f"{split}.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
                for rec in records:
                    fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
    return run_dir


# ------------------------------------------------------------------------------------------------ cli


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Chronos dataset builder (spec sez. 3.5 punto 4)")
    ap.add_argument("--config", default="config/train.yml")
    ap.add_argument(
        "--tokenizer", default=None, help="HF tokenizer id or path: checks that label tokens are single ids"
    )
    ap.add_argument(
        "--no-jsonl", action="store_true", help="skip the LLM JSONL files (the baseline only needs the parquet)"
    )
    ap.add_argument("--live-labels", default=None, help="folder of live labels (data/live_labels): default none")
    ap.add_argument(
        "--live-weight", type=float, default=None,
        help="weight of the live rows (default: dry_run.learner.live_weight of the app config)",
    )  # fmt: skip
    ap.add_argument("--live-cutoff", default=None, help="UTC instant: live rows whose label window ends later are out")
    args = ap.parse_args(argv)
    cfg = load_train_config_or_exit(args.config)
    lc_path = Path(cfg.label_config)
    try:
        label = load_label_config(lc_path)
    except (ValueError, OSError) as e:
        print(f"label_config invalid: {e}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    try:
        app = cfg.app()
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        return EXIT_CONFIG_ERROR
    live = None
    if args.live_labels:
        try:
            stamp = args.live_cutoff
            cutoff = None if stamp is None else datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            weight = app.dry_run.learner.live_weight if args.live_weight is None else args.live_weight
            live = LiveInput(Path(args.live_labels), weight, cutoff)
        except (ValueError, DatasetError) as e:
            print(f"live labels: {e}", file=sys.stderr)
            return EXIT_CONFIG_ERROR
    tokenizer = None
    if args.tokenizer:
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(args.tokenizer)  # type: ignore[no-untyped-call]
    root = bars_root(app) if cfg.data_root == app.paths.data_dir else Path(cfg.data_root) / "bars" / "1m"
    frames = {p: load_bars(root, p) for p in cfg.products}
    try:
        run_dir = build_dataset(
            cfg, label, frames, Path(cfg.datasets_dir), label_config_path=lc_path, tokenizer=tokenizer,
            write_jsonl=not args.no_jsonl, live=live,
        )  # fmt: skip
    except DatasetError as e:
        print(f"dataset error: {e}", file=sys.stderr)
        return 1
    print(f"dataset written to {run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
