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
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import numpy as np
import pandas as pd

from modules.config import EXIT_CONFIG_ERROR, ConfigError
from modules.decision.protocol import ACTIONS, REGIME_LEGEND
from modules.decision.questions import build_questions
from modules.features.schemas import STATE_VERSION
from modules.features.serializer import FLAT_COLUMNS, flatten_state, serialize_state, state_sha256
from modules.features.state_builder import PositionInput, StateBuilder
from modules.market.backfill import bars_root, load_bars
from modules.market.indicators import IndicatorSnapshot
from modules.market.indicators_batch import compute_indicators
from modules.market.timeframes import aggregate_bars
from modules.risk.costs import round_bps
from modules.risk.label_config import LoadedLabelConfig, load_label_config
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
from training.splits import final_fit_split, purged_walk_forward

OUTCOME_NAMES: Final = {0: "NO_FILL", 1: "UPPER", 2: "LOWER", 3: "VERTICAL"}
FOLD_NONE: Final = -1
FOLD_HOLDOUT: Final = -2
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


def assign_splits(rows: pd.DataFrame, label: LoadedLabelConfig, cfg: TrainConfig) -> pd.DataFrame:
    """Add ``fold_test`` and ``final_split``; LONG rows inherit the FLAT row of the same (product, t)."""
    flat = rows[rows["position_side"] == "FLAT"].sort_values(["t", "product"], kind="stable")
    hs = label.holdout_start
    ts = pd.DatetimeIndex(flat["t"]).tz_localize("UTC")
    t1 = pd.DatetimeIndex(flat["label_window_end"]).tz_localize("UTC")
    from datetime import timedelta

    embargo = timedelta(days=cfg.embargo_days)
    splits = purged_walk_forward(ts, t1, n_folds=cfg.n_folds, embargo=embargo, val_frac=cfg.val_frac, holdout_start=hs)
    fold = np.full(len(flat), FOLD_NONE, dtype=np.int64)
    for f in splits.folds:
        fold[f.test] = f.index
    fold[splits.holdout] = FOLD_HOLDOUT
    train, val = final_fit_split(ts, t1, hs, cfg.val_frac, embargo)
    final = np.full(len(flat), "purged", dtype=object)
    final[train], final[val] = "train", "val"
    final[splits.holdout] = "holdout"
    keyed = pd.DataFrame({"product": flat["product"].to_numpy(), "t": flat["t"].to_numpy(), "fold_test": fold,
                          "final_split": final})  # fmt: skip
    return rows.merge(keyed, on=["product", "t"], how="left", validate="many_to_one")


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
                out[fname].append(
                    {
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
                )
    return out


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
) -> Path:
    """Build the dataset from the whole-history bars of each product (``load_bars``); returns the run directory."""
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
    rows = assign_splits(pd.concat(parts, ignore_index=True), label, cfg)

    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    run_dir = out_root / f"{stamp}-{label.sha256[:8]}"
    run_dir.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(label_config_path, run_dir / "label_config.json")
    qversion = build_questions(label.cfg.horizon_min, label.cfg.k_tp, label.cfg.k_sl, label.cfg.atr_tf).version_tag
    rows.to_parquet(run_dir / "samples.parquet", index=False)
    counts = rows.groupby(["position_side", "final_split"]).size()
    manifest = {
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
        "rows_per_fold_test": {str(k): int(v) for k, v in rows["fold_test"].value_counts().sort_index().items()},
        "seed": cfg.seed,
        "sentiment_available": False,
        "label_tokens_checked": tokenizer is not None,
        "git_commit": _git_commit(),
    }
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
    tokenizer = None
    if args.tokenizer:
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(args.tokenizer)  # type: ignore[no-untyped-call]
    root = bars_root(app) if cfg.data_root == app.paths.data_dir else Path(cfg.data_root) / "bars" / "1m"
    frames = {p: load_bars(root, p) for p in cfg.products}
    try:
        run_dir = build_dataset(
            cfg, label, frames, Path(cfg.datasets_dir), label_config_path=lc_path, tokenizer=tokenizer,
            write_jsonl=not args.no_jsonl,
        )  # fmt: skip
    except DatasetError as e:
        print(f"dataset error: {e}", file=sys.stderr)
        return 1
    print(f"dataset written to {run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
