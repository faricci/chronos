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
