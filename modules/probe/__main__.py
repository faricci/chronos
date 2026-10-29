"""Command line of the P4-pre probe (plan sez. 3.2).

::

    python -m modules.probe preflight    --config <cfg> --key-file <file>
    python -m modules.probe run --stage preview|place-cancel|expire|reject|stop-switch|live --config <cfg> --key-file
    <file>
    python -m modules.probe stop         --config <cfg>
    python -m modules.probe flatten      --config <cfg>
    python -m modules.probe status       --config <cfg>
    python -m modules.probe report       --config <cfg> --key-file <file>

Exit codes: 0 ok, 1 the stage failed or the process crashed, 2 refused before anything was built (gate, chain, config),
3 a write on a read-only exchange, 4 a spot violation. ``stop`` and ``flatten`` only create a file and need no key.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from modules.config import DEFAULT_CONFIG_PATH, AppConfig, ConfigError, load_config
from modules.probe.control import FinishMarker, Switches, read_status
from modules.probe.runner import EXIT_FAILED, EXIT_OK, Deps, complain, execute_stage, say
from modules.probe.stages import STAGES

RUN_STAGES = tuple(s for s in STAGES if s != "preflight")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config", default=DEFAULT_CONFIG_PATH, help="the YAML config (exec_probe.enabled must be true)"
    )
    keyed = argparse.ArgumentParser(add_help=False, parents=[common])
    keyed.add_argument("--key-file", default=None, help="the Coinbase key file, OUTSIDE the repository")

    parser = argparse.ArgumentParser(prog="python -m modules.probe", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight", parents=[keyed], help="read-only checks of the account, the product and the fees")
    run = sub.add_parser("run", parents=[keyed], help="run one stage (in order) or the probe itself (live)")
    run.add_argument("--stage", choices=RUN_STAGES, required=True)
    sub.add_parser("stop", parents=[common], help="no new entries; the pending entry is cancelled")
    sub.add_parser("flatten", parents=[common], help="stop, and sell the position")
    sub.add_parser("status", parents=[common], help="what the running probe says (and the hand switches)")
    sub.add_parser("report", parents=[keyed], help="REST reads only: concordance of the ledger with the exchange")
    return parser


def _switches(cfg: AppConfig) -> Switches:
    return Switches(Path(cfg.paths.state_dir) / "exec_probe")


def cmd_stop(cfg: AppConfig, deps: Deps) -> int:
    path = _switches(cfg).request_stop()
    say(deps, f"Creato {path}. Nessun nuovo ingresso; l'ordine d'ingresso pendente viene annullato (con conferma).")
    say(deps, "Gli stop (bracket) e la posizione restano. Per ripartire cancella il file A MANO.")
    return EXIT_OK


def cmd_flatten(cfg: AppConfig, deps: Deps) -> int:
    path = _switches(cfg).request_flatten()
    say(deps, f"Creato {path}. STOP piu' vendita della posizione (motivo MANUAL).")
    say(deps, "Per ripartire cancella il file FLATTEN (e STOP, se c'e') A MANO.")
    return EXIT_OK


def format_status(cfg: AppConfig) -> list[str]:
    out_dir = Path(cfg.exec_probe.out_dir)
    switches = _switches(cfg)
    status: dict[str, Any] | None = read_status(out_dir)
    lines: list[str] = []
    finished = FinishMarker(out_dir).read()
    if finished is not None:
        lines.append(f"PROVA TERMINATA: {finished.reason} - {finished.detail} ({finished.at.isoformat()})")
    if status is None:
        lines.append("Nessuno stato scritto: il programma non e' mai partito (o e' fermo da prima di scrivere).")
        blocked = "manual_stop" if switches.stop_active() else None
    else:
        blocked = "manual_stop" if switches.stop_active() else status.get("entries_blocked")
        lines.append(f"Aggiornato: {status.get('updated_at')}")
        pos = status.get("position") or {}
        lines.append(
            f"Posizione: {pos.get('side')} {pos.get('base_qty')} "
            f"(stop su Coinbase: {pos.get('bracket')}, SL {pos.get('sl')})"
        )
        lines.append(f"Ordine d'ingresso pendente: {status.get('pending_entry')}")
        lines.append(f"Conteggi: {status.get('counts')}")
        lines.append(f"Budget: {status.get('budget')}")
        lines.append(f"Serie: {status.get('streaks')}")
        lines.append(f"Ultimo salto di slot: {status.get('last_skip')}")
    lines.append(f"entries_blocked: {blocked}")
    lines.append(
        f"STOP: {'presente' if switches.stop_active() else 'assente'}   "
        f"FLATTEN: {'presente' if switches.flatten_active() else 'assente'}"
    )
    return lines


def cmd_status(cfg: AppConfig, deps: Deps) -> int:
    for line in format_status(cfg):
        say(deps, line)
    return EXIT_OK


def main(argv: Sequence[str] | None = None, deps: Deps | None = None) -> int:
    args = build_parser().parse_args(argv)
    deps = deps or Deps()
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        complain(deps, str(exc))
        return 2
    command = args.command
    if command == "stop":
        return cmd_stop(cfg, deps)
    if command == "flatten":
        return cmd_flatten(cfg, deps)
    if command == "status":
        return cmd_status(cfg, deps)
    if command == "report":
        from modules.probe.runner import execute_report

        return asyncio.run(execute_report(cfg, deps, args.key_file))
    stage = "preflight" if command == "preflight" else args.stage
    try:
        return asyncio.run(execute_stage(cfg, deps, stage, args.key_file))
    except KeyboardInterrupt:
        complain(deps, "Interrotto.")
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
