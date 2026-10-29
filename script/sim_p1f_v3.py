"""SIM-P1f-bis: the one and last calibration of ``feature_drift`` with the rule PSI-v3 (``PSI_V3_VOLSTD``).

::

    python -m script.sim_p1f_v3 run --config config/config.yml [--champion <version>] [--out-root data/reports/sim_p1f]
    python -m script.sim_p1f_v3 s6  --run-dir data/reports/sim_p1f/PSI_V3_VOLSTD/<version> [--end YYYY-MM-DD]

Decision: docs/decisions/2026-10-26-p1f-bis-psi-v3.md (pre-registered, committed BEFORE this code). The rule is the
one of PSI-v2 (reference R, 12 bins, Jeffreys smoothing, minP of Westfall-Young at alpha 1%, blocks of 182 days out of
sample) with the 14 fast features standardised by the volatility of the same state (``z_f = f / rv_1d_bps``), the
family J of the ``z`` with ``n_eff_7d >= 50``, one window of 7 days, and the extra check D1h on days that the
calibration never used (the non-overlapping weeks of the holdout, from ``holdout_start`` to 7 days before the run).

Folder of a run: ``<out-root>/PSI_V3_VOLSTD/<champion_version>/``. Same three files as PSI-v2, each written once with
exclusive open: ``00_preregistration.json`` BEFORE any bar is read, ``10_calibration.json``, ``20_s6.json`` (only after
``adopted``). The run is REFUSED (exit 2, nothing written) unless the decision document and the three code files are
tracked and clean in git, and a second run for the same champion version is refused. Exit code: 0 adopted, 1 rejected,
2 refused. Deterministic: no network, no seed. ``rejected`` is final: it is documented and opens the conflict P1g.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from modules.dryrun import drift
from modules.dryrun.drift import (
    CALIBRATION_FILE,
    CALIBRATION_SCHEMA_V3,
    PREREG_FILE,
    PREREG_SCHEMA_V3,
    RULE_V3,
    VOLSTD_BASE,
    VOLSTD_FEATURES,
)
from script import sim_p1f
from script.sim_p1f import (
    EXIT_REFUSED,
    REPO_ROOT,
    Champion,
    MarketsLoader,
    Study,
    _iso,
    _naive,
    _sha256_file,
    _write_new,
    evaluate_window,
    hourly_of,
    reference_doc,
    s1_doc,
)

DECISION_DOC_V3 = "docs/decisions/2026-10-26-p1f-bis-psi-v3.md"
CODE_FILES_V3 = ("script/sim_p1f.py", "script/sim_p1f_v3.py", "modules/dryrun/drift.py")
WINDOW = 7
STATES = 24 * WINDOW
PEdgeFn = Callable[[np.ndarray], np.ndarray]


def decide_v3(j_nonempty: bool, d1: bool, d1h: bool, d2: bool, d3: bool) -> str:
    """``adopted`` if and only if J is not empty and D1, D1h, D2 and D3 all hold; otherwise ``rejected`` (final)."""
    return "adopted" if (j_nonempty and d1 and d1h and d2 and d3) else "rejected"


def _git_freeze(doc: Path, files: Sequence[str]) -> tuple[str | None, bool | None]:
    """``(HEAD, frozen)``: frozen only if the decision document and every code file are tracked and unmodified."""
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=True, timeout=30
        ).stdout.strip()
        paths = [str(doc), *(str(REPO_ROOT / f) for f in files)]
        for path in paths:
            tracked = subprocess.run(
                ["git", "ls-files", "--error-unmatch", "--", path], cwd=REPO_ROOT, capture_output=True, text=True,
                timeout=30,
            ).returncode == 0  # fmt: skip
            status = subprocess.run(
                ["git", "status", "--porcelain", "--", path], cwd=REPO_ROOT, capture_output=True, text=True, check=True,
                timeout=30,
            ).stdout.strip()  # fmt: skip
            if not tracked or status:
                return head, False
    except (OSError, subprocess.SubprocessError):
        return None, None
    return head, True


# --------------------------------------------------------------------------------------------- D1h


def holdout_check(
    study: Study, cal: drift.Calibration, d1h_end: np.datetime64
) -> dict[str, Any]:
    """D1h: the rule on the non-overlapping weeks of the holdout, ``[holdout_start + 7k, + 7 days)``, for every training
    product, up to ``d1h_end``. A week with fewer than 168 hourly states is ``incomplete`` (outside n_H). Alarms are
    counted over all products together; the limit is ``drift.k_h(n_H)``."""
    start = study.holdout
    step = np.timedelta64(drift.D1H_STEP_DAYS, "D")
    rows: list[dict[str, Any]] = []
    n_h = alarms = 0
    for product in study.training:
        h = study.hourly[product]
        first = start
        while first + step <= d1h_end:
            lo = int(np.searchsorted(h.ts, first, side="left"))
            hi = int(np.searchsorted(h.ts, first + step, side="left"))
            row: dict[str, Any] = {"product": product, "start": _iso(first)}
            if hi - lo < STATES:
                row.update(status="incomplete", states=hi - lo)
            else:
                res = drift.window_stat(h.x[lo:hi], cal, product)
                if res.S is None:
                    row.update(status="unmeasured", states=hi - lo)
                else:
                    row.update(status="measured", states=hi - lo, S=res.S, alarm=res.alarm)
                    n_h += 1
                    alarms += int(res.alarm)
            rows.append(row)
            first = first + step
    limit = drift.k_h(n_h)
    return {
        "start": _iso(start), "end": _iso(d1h_end), "step_days": drift.D1H_STEP_DAYS, "windows": rows, "n_h": n_h,
        "alarms": alarms, "k_h": limit, "level": drift.D1H_LEVEL, "ok": limit is not None and alarms <= limit,
    }  # fmt: skip


def p_edge_reference(
    study: Study, d1h_end: np.datetime64, p_edge_fn: PEdgeFn | None
) -> tuple[dict[str, Any] | None, str | None]:
    """The distribution of the champion's ``p_edge`` on the holdout (never on the training: in-sample predictions have
    another distribution). A diagnostic: it does not enter the decision. ``(reference, reason_if_none)``."""
    if p_edge_fn is None:
        return None, "no_decider"
    parts: list[np.ndarray] = []
    for product in study.training:
        h = study.hourly[product]
        lo = int(np.searchsorted(h.ts, study.holdout, side="left"))
        hi = int(np.searchsorted(h.ts, d1h_end, side="left"))
        if hi > lo:
            parts.append(h.x[lo:hi])
    if not parts:
        return None, "no_holdout_states"
    try:
        values = np.asarray(p_edge_fn(np.vstack(parts)), dtype=np.float64)
    except Exception:
        return None, "decider_error"
    finite = values[np.isfinite(values)]
    inner = drift.inner_edges(finite)
    if inner is None:
        return None, "no_reference_data"
    return {
        "source": "holdout", "start": _iso(study.holdout), "end": _iso(d1h_end), "products": list(study.training),
        "n": int(finite.size), "inner": inner, "p": drift.ref_shares(finite, inner), "r_min": float(finite.min()),
        "r_max": float(finite.max()),
    }, None  # fmt: skip


# ---------------------------------------------------------------------------------------- calibration


def calibrate_v3(
    markets: Mapping[str, Any], champion: Champion, served: Sequence[str], *, d1h_end: np.datetime64,
    candidates: Sequence[str] | None = None, raw_candidates: Sequence[str] | None = None,
    p_edge_fn: PEdgeFn | None = None,
) -> dict[str, Any]:  # fmt: skip
    """The body of ``10_calibration.json`` (without schema, created_at and the sha256 of the pre-registration)."""
    label = champion.label
    hourly = {p: hourly_of(markets[p], label) for p in markets}
    raw = tuple(drift.candidate_features(champion.sentiment_available) if raw_candidates is None else raw_candidates)
    zs = tuple(VOLSTD_FEATURES if candidates is None else candidates)

    # the raw features: the SAME reference R as PSI-v2, and the null of their PSI (diagnostic only)
    diag = Study(hourly, champion, served, raw)
    w_diag = diag.windows(WINDOW)
    diag_psi = diag.psi_matrix(w_diag)
    diag_nulls = diag.nulls(diag_psi, np.arange(len(w_diag.product)))
    diagnostic_reference: dict[str, Any] = {}
    for j, name in enumerate(diag.features):
        col = drift.feature_values(diag.r_all, name)
        finite = col[np.isfinite(col)]
        diagnostic_reference[name] = {
            "inner": diag.edges[name], "p": diag.shares[name], "r_min": float(finite.min()),
            "r_max": float(finite.max()), "sigma": float(finite.std()), "null_sorted": diag_nulls[j], "x_f": None,
            "n_eff_7d": diag.n_eff[name], "tau": diag.tau[name],
        }  # fmt: skip

    # the family: the standardised features with n_eff_7d >= 50 measured on z in R
    probe = Study(hourly, champion, served, zs, t0_features=raw)
    if probe.t0 != diag.t0:
        raise ValueError("R starts at another instant for the standardised features: a defect, not a result")
    judged = [n for n in probe.features if probe.n_eff[n] >= drift.NEFF_JUDGED]
    untested: list[dict[str, Any]] = [
        {"feature": n, "n_eff_7d": diag.n_eff[n]} for n in diag.features if n not in VOLSTD_BASE
    ]
    untested += [{"feature": n, "n_eff_7d": probe.n_eff.get(n)} for n in zs if n not in judged]
    body: dict[str, Any] = {
        "rule": drift.rule_json(RULE_V3), "champion_version": champion.version,
        "label_config_sha256": label.sha256, "training_products": list(diag.training), "served_products": list(served),
        "holdout_start": _iso(champion.holdout_start), "alpha": drift.ALPHA, "s1": s1_doc(diag),
        "family": {
            "candidates": list(zs), "n_eff_7d": probe.n_eff, "tau": probe.tau, "excluded": probe.excluded,
            "judged": judged,
        },
        "scope": {"tested": judged, "untested": untested, "note": drift.scope_note(len(untested))},
        "diagnostic_reference": diagnostic_reference, "final": True,
    }  # fmt: skip
    study = Study(hourly, champion, served, judged, t0_features=raw) if judged else None
    d1 = d1h = d2 = d3 = False
    ev: dict[str, Any] | None = None
    holdout: dict[str, Any] | None = None
    if study is not None:
        ev = evaluate_window(study, WINDOW)
        d1, d2, d3 = bool(ev["d1"]["ok"]), bool(ev["d2"]["ok"]), bool(ev["d3"]["ok"])
        ref = ev["_reference"]
        cal = study.calibration(study.windows(WINDOW), ref["nulls"], ref["c"], ref["x_f"])
        holdout = holdout_check(study, cal, d1h_end)
        d1h = bool(holdout["ok"])
        edge, reason = p_edge_reference(study, d1h_end, p_edge_fn)
    else:
        edge, reason = None, "no_family"
    decision = decide_v3(bool(judged), d1, d1h, d2, d3)
    body.update(
        window={k: v for k, v in ev.items() if k != "_reference"} if ev is not None else None, d1h=holdout,
        checks={"j_nonempty": bool(judged), "d1": d1, "d1h": d1h, "d2": d2, "d3": d3}, decision=decision,
        window_days=WINDOW if decision == "adopted" else None, c=None, reference=None,
        p_edge_reference=edge, p_edge_reference_reason=reason,
    )  # fmt: skip
    if ev is not None and study is not None:
        if decision == "adopted":
            body["c"] = ev["c"]
            body["reference"] = reference_doc(study, WINDOW, ev["_reference"])
        else:
            body["reference_diagnostic"] = reference_doc(study, WINDOW, ev["_reference"])
            body["c_diagnostic"] = ev["c"]
    return body


# ------------------------------------------------------------------------------------------------ run


def run(
    champion: Champion, served: Sequence[str], loader: MarketsLoader, out_root: Path, *,
    decision_doc: Path | None = None, now: Callable[[], datetime] = lambda: datetime.now(UTC),
    candidates: Sequence[str] | None = None, raw_candidates: Sequence[str] | None = None,
    p_edge_fn: PEdgeFn | None = None, commit: tuple[str | None, bool | None] | None = None,
) -> int:  # fmt: skip
    """The one calibration run of PSI-v3. Exit code: 0 adopted, 1 rejected, 2 refused (nothing written)."""
    folder = drift.run_dir_of(out_root, RULE_V3, champion.version)
    if (folder / CALIBRATION_FILE).exists():
        print(f"a calibration already exists for {champion.version}: {folder / CALIBRATION_FILE}", file=sys.stderr)
        return EXIT_REFUSED
    doc_path = decision_doc or REPO_ROOT / DECISION_DOC_V3
    if not doc_path.is_file():
        print(f"the decision document is missing: {doc_path}", file=sys.stderr)
        return EXIT_REFUSED
    git_commit, frozen = commit if commit is not None else _git_freeze(doc_path, CODE_FILES_V3)
    if frozen is not True:
        print("refused: the decision document and the code must be tracked and clean in git (freeze)", file=sys.stderr)
        return EXIT_REFUSED
    created = now().astimezone(UTC)
    run_day = created.date()
    midnight = datetime(run_day.year, run_day.month, run_day.day, tzinfo=UTC)
    d1h_end = midnight - timedelta(days=drift.D1H_END_LAG_DAYS)
    prereg = {
        "schema": PREREG_SCHEMA_V3, "rule": drift.rule_json(RULE_V3), "champion_version": champion.version,
        "label_config_sha256": champion.label.sha256, "training_products": list(champion.training_products),
        "served_products": list(served), "holdout_start": _iso(champion.holdout_start), "run_day": run_day.isoformat(),
        "d1h_window": {"start": _iso(champion.holdout_start), "end": _iso(d1h_end)}, "git_commit": git_commit,
        "decision_doc": doc_path.name, "decision_doc_sha256": _sha256_file(doc_path),
        "code_sha256": {name: _sha256_file(REPO_ROOT / name) for name in CODE_FILES_V3}, "code_frozen": True,
        "final": True, "created_at": _iso(created),
    }  # fmt: skip
    folder.mkdir(parents=True, exist_ok=True)
    prereg_path = folder / PREREG_FILE
    prereg_path.unlink(missing_ok=True)  # an interrupted run (no 10_): rewritten, still before any bar
    _write_new(prereg_path, prereg)
    products = sorted(set(served) | set(champion.training_products))
    markets = loader(champion.label, products)  # <- the first bar is read here, after the pre-registration
    try:
        body = calibrate_v3(
            markets, champion, served, d1h_end=_naive(d1h_end), candidates=candidates, raw_candidates=raw_candidates,
            p_edge_fn=p_edge_fn,
        )
    except ValueError as exc:
        print(f"SIM-P1f-bis cannot run: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    doc = {
        "schema": CALIBRATION_SCHEMA_V3, "created_at": _iso(now()), "preregistration_sha256": _sha256_file(prereg_path),
        **body,
    }  # fmt: skip
    _write_new(folder / CALIBRATION_FILE, doc)
    print(json.dumps({"decision": body["decision"], "checks": body["checks"], "c": body["c"]}))
    return 0 if body["decision"] == "adopted" else 1


# ------------------------------------------------------------------------------------------------- CLI


def _cli_p_edge_fn(config: str, champion: Champion) -> PEdgeFn:
    from modules.config import load_config
    from modules.dryrun.registry import FilesystemModelRegistry
    from modules.dryrun.revision_impact import RegistryDeciderLoader

    app = load_config(config)
    registry = FilesystemModelRegistry(Path(app.paths.models_dir) / "registry")
    loaded = RegistryDeciderLoader(registry, champion.label)

    def p_edge(x: np.ndarray) -> np.ndarray:  # loaded when called: a failure is the diagnostic's ``decider_error``
        return np.asarray(loaded(champion.version)[0].answers_batch(x).p_edge, dtype=np.float64)

    return p_edge


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m script.sim_p1f_v3", description="SIM-P1f-bis: PSI-v3 calibration")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "s6"):
        p = sub.add_parser(name)
        p.add_argument("--config", default="config/config.yml")
        p.add_argument("--champion", default=None, help="registry version (default: the champion alias)")
        p.add_argument("--out-root", default=None, help="default: <data_dir>/reports/sim_p1f")
        if name == "s6":
            p.add_argument("--run-dir", default=None, help="default: <out-root>/PSI_V3_VOLSTD/<champion version>")
            p.add_argument("--end", default=None, help="last UTC day of the window (default: yesterday)")
    args = ap.parse_args(argv)
    from modules.config import EXIT_CONFIG_ERROR, ConfigError
    from modules.dryrun.registry import RegistryError

    try:
        champion, served, loader, out_root = sim_p1f._context(args)
        end = date.fromisoformat(args.end) if getattr(args, "end", None) else None
        p_edge_fn = _cli_p_edge_fn(args.config, champion) if args.cmd == "run" else None
    except (ConfigError, RegistryError, OSError, ValueError, KeyError) as exc:
        print(f"SIM-P1f-bis configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    if args.cmd == "run":
        return run(champion, served, loader, out_root, p_edge_fn=p_edge_fn)
    run_dir = Path(args.run_dir) if args.run_dir else drift.run_dir_of(out_root, RULE_V3, champion.version)
    return sim_p1f.run_s6(run_dir, served, loader, champion.label, end=end, rule=RULE_V3)


if __name__ == "__main__":
    raise SystemExit(main())
