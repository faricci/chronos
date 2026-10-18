# Lessons

## 2026-10-17 — Spec v2.1 had internally inconsistent numbers

Pattern: example values (fees, ATR, round-trip cost) were written by hand in several sections and did not follow the formulas stated elsewhere in the same document. The default barrier parameters (ATR 15m, H = 240 min) were never checked against real fees and real volatility, and turned out to be infeasible.

Rules:
- Define each quantity once (one formula, one owner module) and reference it; never restate a formula in another section.
- Derive every numeric example from one worked reference case and reuse that case everywhere.
- Before fixing strategy parameters in a spec, measure the market (spread, ATR, history depth) with a read-only probe and check them against the cost model.
- When a parameter can be wrong in a way that kills the project (fees vs edge), add an explicit stop criterion instead of a default.
- State the scope (per book, global) of every stateful component, and the decision rule (argmax vs score) of every derived value.

## 2026-10-17 - Tooling run on the whole tree touched v1 files

Pattern: `ruff check --fix modules` rewrote eight v1 modules that are not part of the task. Caught by `git status`, reverted with `git checkout`.

Rules:
- Run auto-fixers only on explicit file paths, never on a directory that contains code outside the task.
- Check `git status` after any command with `--fix` or `--write`.
- Keep v1 paths in the `exclude` lists of ruff and mypy until they are deleted.
- The shell tool strips backslashes inside heredocs and inline scripts: write files containing `\` with the Write/Edit tools.

## 2026-10-18 - Do not build on a data-source equivalence nobody has measured

Pattern: the spec said "build 1m bars from trades" and the history was REST candles. I implemented the aggregator exactly as written and only a live soak against the exchange's own candles showed that the two series differ in about half of the minutes. The tests (all green) could not show it: they compared the code with itself.

Rules:
- When two sources are meant to be the same series, compare them on real data before building on either, and say so in the plan.
- A parity test between two implementations of one definition does not prove the definition matches the outside world; add a check against the external source.
- Keep the first soak against the real service early in a module, not at the end.
- Shell heredocs mangle long texts and backslashes: write multi-line patches as files with the Write tool and run them.

## Un processo di lunga durata va ricontrollato, non dato per vivo

- Il soak di 48 h era morto dopo 51 minuti per un riavvio del PC e il todo lo dava "in corso". Regola: prima di scrivere "in corso" in un documento, verificare PID vivo e `updated_at` del report; a ogni ripresa della sessione controllarli per primi.
