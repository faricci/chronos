# Chronos

A research project: a spot-only, long-only crypto trading agent for Coinbase (BTC / ETH / SOL against EUR), built around
honest, pre-registered statistical gates.

> **Read this first**
>
> - **No edge has been demonstrated.** Every hypothesis tested so far (price indicators with a LightGBM model, news as an
>   entry signal, volume/drawdown patterns, daily trend-following, monthly rotation) either failed or was inconclusive
>   once real Coinbase costs (about 77-110 bps per round trip at the entry fee tier) are charged. The reports are in
>   [`docs/decisions/`](docs/decisions/). Do not run this code expecting profit.
> - **This is not financial advice.** The code can place real orders if you give it a trading-capable API key and
>   explicitly enable the execution probe. You can lose money. You are solely responsible for anything you run.
> - **Real orders are off by default.** The main application refuses to start in live mode, the exchange client is
>   read-only, and the only gateway wired into the dry run is a simulator. The small execution probe
>   ([`modules/probe/`](modules/probe/)) needs four independent confirmations, a key file outside the repository and a typed
>   confirmation phrase. Never put a key with trading permission in `.env`.
> - **Use at your own risk.** No warranty of any kind.

## What it is

- Spot market only. No leverage, no margin, no futures, no short selling.
- Every order must be covered 100% by a real balance held in an in-memory cache that is kept fresh in the background. A
  stale or insufficient cache aborts the order. No debt is ever possible.
- A dry run feeds the real Coinbase market data into the decision chain and simulates fills with a conservative fill model.
  Several "books" run in parallel (a model champion, a random-entry control, buy and hold).
- Gates (G0-G4, GE, G-DRY-OPS, G-DRY-ECON) and the pre-registration of every study are what the project is really about:
  the result of each experiment is decided in writing before looking at the numbers.

## Where to look

| What | Where |
|------|-------|
| Plain-language status | [`docs/STATO.md`](docs/STATO.md) (Italian) |
| Full specification | [`docs/specs/chronos-system1-spec.md`](docs/specs/chronos-system1-spec.md) (Italian) |
| Decisions and study outcomes | [`docs/decisions/`](docs/decisions/) |
| Work register and lessons | [`tasks/todo.md`](tasks/todo.md), [`tasks/lessons.md`](tasks/lessons.md) |
| Execution probe runbook | [`docs/runbook-prova-esecuzione-minima.md`](docs/runbook-prova-esecuzione-minima.md) |

## Layout

```
modules/market      market data: REST candles, WebSocket feed, indicators
modules/decision    state builder, baseline model, policy, decision engine
modules/risk        risk manager, sizing, kill switch, label config
modules/execution   exchange client, balance cache, spot order matrix, reconciler, watchdog
modules/dryrun      shadow gateway, books, learner, reports, gates
modules/probe       small, heavily limited real-order probe (disabled by default)
modules/telemetry   operations recorder (uptime, bars, events)
training/           datasets, labels, walk-forward training, backtest, evaluation
script/             studies, soaks, supervisor and tools
tests/              test suite (includes wiring "tripwires" that forbid leverage and unguarded order paths)
```

## How the model learns

The training code is in [`training/`](training/); the nightly refit hook is `modules/dryrun/learner.py`.

1. **Examples.** Every past moment of the 1-minute Coinbase candles (about 3 years) becomes one example described by indicators
   (returns, RSI, ATR, realised volatility, MACD, volume z-score, BTC context for ETH/SOL, ...). Only past data is used.
2. **Labels (triple barrier).** From each moment a long entry is simulated with a take-profit at 3 ATR and a stop at 1.5 ATR,
   for at most 15 days. The label says which barrier was hit first, with real fees and spread already charged.
3. **Models.** Three small LightGBM models: action (buy / wait / sell), expected edge, volatility regime. Probabilities are
   calibrated out-of-fold (Platt) so that "70%" means about 70%.
4. **No look-ahead.** Training is purged walk-forward (a gap between train and validation folds). The most recent months
   (holdout, from 2026-06-20) are used only once, at the end.
5. **Gates.** Written before looking at results: holdout similar to training, calibrated probabilities, beats a naive
   baseline in at least 4 of 5 folds, and a statistically real net gain after costs (GE). A model that fails does not move on.
6. **Nightly learner (dry run).** At 00:15 UTC it audits live-versus-batch parity and the drift report; it can rebuild the
   dataset and refit a challenger, which is promoted only if it passes the promotion rule (at least 30 trades, positive PnL,
   drawdown at most 5%, 14 shadow days). No challenger has ever been promoted.

Current result: the champion (trained 2026-10-18) fails GE, so with the policy thresholds that survive validation it opens no
trades. See [`docs/decisions/`](docs/decisions/) for the studies that followed.

## Running

Python 3.14 and a virtual environment.

```
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements-dev.txt
python -m pytest -q               # test suite
python -m mypy                    # strict type check
python -m ruff check .
```

The dry run needs a **read-only (View)** Coinbase API key in `.env` (see `.env.example`) and a configuration file based on
`config/config.example.yml`:

```
python -m modules.app --config path\to\config.yml                 # dry run (live mode is refused)
python -m script.supervise --config path\to\config.yml            # restart-on-failure supervisor
python -m modules.dryrun.report --days 3 --config path\to\config.yml
```

## License

No license has been chosen yet. Until one is added, all rights are reserved by default.
