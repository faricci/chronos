# Lessons

## 2026-10-17 — Spec v2.1 had internally inconsistent numbers

Pattern: example values (fees, ATR, round-trip cost) were written by hand in several sections and did not follow the formulas stated elsewhere in the same document. The default barrier parameters (ATR 15m, H = 240 min) were never checked against real fees and real volatility, and turned out to be infeasible.

Rules:
- Define each quantity once (one formula, one owner module) and reference it; never restate a formula in another section.
- Derive every numeric example from one worked reference case and reuse that case everywhere.
- Before fixing strategy parameters in a spec, measure the market (spread, ATR, history depth) with a read-only probe and check them against the cost model.
- When a parameter can be wrong in a way that kills the project (fees vs edge), add an explicit stop criterion instead of a default.
- State the scope (per book, global) of every stateful component, and the decision rule (argmax vs score) of every derived value.
