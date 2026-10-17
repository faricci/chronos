# Chronos v2 - avanzamento rispetto alla spec

Spec: `docs/specs/chronos-system1-spec.md` v2.5. Branch: `reachitect/v2-opus55-spec` (scopo: implementare quella spec).
Aggiornato: 2026-10-17. Test: 299 passati, 22 promemoria in skip (`pytest -rs`), 4 `data`/`live` da lanciare a mano; mypy strict e ruff puliti sui file v2.
Verifica nel container Python 3.12 ancora da fare (Docker spento); si lavora in un venv locale 3.14 con codice compatibile 3.12.

## 1. Dove siamo, per fase dell'Appendice A

| Fase | Contenuto (spec) | Stato | Cosa c'e' / cosa manca |
|------|------------------|-------|------------------------|
| P0 Fondamenta | sez. 0.5 | circa 60% | FATTO: `config.py`, `risk/costs.py` (CostModel), `market/backfill.py` + `load_bars`, `market/timeframes.py`, `market/indicator_defs.py`, `indicators_batch.py`, `indicators.py` (parita' verificata su dati reali BTC/ETH/SOL), probe. IN CORSO: backfill 3 anni. NON INIZIATO: `bars.py`, `ws_feed.py`, `orderbook.py`, `ledger/positions.py`, `dryrun/recorder.py` |
| P1 Fattibilita', dati, baseline | sez. 1, 3 (parte CPU) | 0% | sentiment (0/9 file), `training/*` (0/12), `BaselineDecider` |
| P2 Decision + Risk | sez. 2, 4 | circa 15% | FATTO: nucleo di copertura spot (`rounding`, `orders`, `balance`, `balance_guard`, `spot`) e formule in `costs.py`. MANCA: `features/*` (0/3), `decision/*` (0/6), `sizing`, `risk_manager`, `kill_switch`, `exchange`, `gateway`, `user_stream`, `reconciler`, `watchdog`, `balance_feed`, `app.py` |
| P3 Dry run | sez. 5 | 0% | 0/12 file di `modules/dryrun/` |
| P4-P6 | live-micro, rampa, cloud | non iniziato | P4 richiede il probe con la key View (H5) |

Ordine P0 della spec (sez. 0.5): config -> backfill+load_bars -> indicators_batch -> indicators -> bars -> ws_feed+orderbook -> ledger -> recorder.
Criterio di uscita P0: storico backfillato (in corso), parita' batch/incrementale su >= 5.000 barre per prodotto (FATTO sullo storico gia' scaricato: `pytest -m data`; da ripetere a backfill finito), 48 h di stream WS senza buchi (non fatto).

## 2. Per sezione della spec (file nominati dalla spec)

| Sezione | Fatti | Mancanti |
|---------|-------|----------|
| 0.3 CostModel | `risk/costs.py` | - |
| 0.5 / 2.3-A market | `backfill.py`, `timeframes.py` (extra), `indicators.py`, `indicators_batch.py`, `indicator_defs.py` (extra) | `ws_feed`, `bars`, `orderbook` |
| 1 Sentiment | - | tutti i 9 file |
| 2.3-B/C features, decision | - | `features/{schemas,state_builder,serializer}`, `decision/{questions,client,protocol,baseline,policy,engine}` |
| 3.5 training, decider | - | 12 file `training/` (la parte LLM e' fuori scope C8); `decider/` fuori scope |
| 4.3 risk / execution | `config`, `rounding`, `balance`, `balance_guard`, `spot`, `orders` (extra) | `exchange`, `gateway`, `user_stream`, `reconciler`, `watchdog`, `balance_feed`, `risk/{sizing,risk_manager,kill_switch}`, `ledger/positions`, `app` |
| 5.3 dry run | `resolve_mode` (in `config`) | `fill_model`, `virtual_account`, `shadow_gateway`, `bracket_sim`, `books`, `recorder`, `replay`, `label_resolver`, `learner`, `registry`, `report`, `parity` |

In pratica: 9 dei circa 65 file nominati dalla spec sono scritti. Il grosso del lavoro resta il livello dati/decisione (sez. 1-3) e il dry run (sez. 5).

## 3. Decisioni dell'architetto

| Conflitto | Esito | Dove |
|-----------|-------|------|
| CostModel vs probe, EV a due esiti | applicato, spec v2.3 | spec 0.3, 0.4, 0.5 |
| Copertura spot, leva, bracket | applicato, spec v2.4 (C11, 4.1.6). ATTENZIONE: l'attached bracket e' eliminato, il bracket e' sempre standalone | spec 4.1.6; dettaglio e ipotesi H1-H6 in `docs/decisions/2026-10-17-copertura-spot.md` |
| Definizione esatta degli indicatori | applicato, spec v2.5 (sez. 2.2.1.b; `trades_z_1d` rimosso). Testo completo, firme e test T1-T10 in `docs/decisions/2026-10-17-indicatori.md` | `indicators*.py` fatti; T8-T10 e O1 sono promemoria in `tests/test_obligations.py` |

## 4. Prossimi passi

1. Nell'ordine P0 della spec: `bars.py`, `orderbook.py`, `ws_feed.py`, `ledger/positions.py` (con `PositionCache` e colonne `dust_*`), `dryrun/recorder.py`. Nessuno e' bloccato dall'architetto.
3. Poi il percorso di esecuzione collegato al guard: `exchange.py` -> `sizing.py` -> `risk_manager.py` -> `virtual_account.py` -> `gateway.py`/`shadow_gateway.py` (i tripwire di `test_c11_wiring.py` lo impongono).
4. P1: `training/feasibility.py` per primo (la spec lo vuole prima di tutto il resto del training).

## 5. Aperti

- [ ] Verifica nel container `python:3.12-slim` (Docker spento)
- [ ] Esito finale del backfill a 3 anni; rilanciare `pytest -m data` a fine corsa (parita' sull'intero storico)
- [ ] O1 (aperto dall'architetto): `spread_bps` e blocco `costs` sono mediane in training e valori istantanei a runtime, stesso tipo di skew di `trades_z_1d`. Serve un CONFLITTO dedicato prima di `state_builder.py` (imposto da `tests/test_obligations.py`)
- [ ] `training/labeling.py`: `forward_rv_bucket` deve usare `indicators_batch.rv_per_day_bps`; label `vol_regime` solo da circa 375 giorni dopo l'inizio dello storico
- [ ] Probe con la key View (H5): confermare `portfolio_type`, `can_transfer`; `DEFAULT_PORTFOLIO_TYPES` non e' verificato
- [ ] Healthcheck del `chronos` in compose ancora finto; servizio `decider` (profilo gpu) non aggiunto (sez. 4.3 punto 13)
- [ ] Rename del branch a `v2` e tag `v1-final` (sez. 0.5): decisione dell'utente

## 6. Vincoli per P1 (architetto, spec v2.3)

- [ ] `training/tune_policy.py`: ogni combinazione di soglie si valuta con `training/backtest.py` (stesso RiskManager, fee floor ed EV gate attivi). Test: `p_edge` sopra `p_edge_min` ma sotto `p_breakeven` deve dare `Skip(ev_negative)` anche nel tuning.
- [ ] `training/feasibility.py`: colonne solo diagnostiche `p_vertical` e `vert_mean_net_bps` (da `net_return`) in `data/reports/feasibility.md`; non in `label_config.json`, non nella regola di scelta.
- [ ] `CostModel.ev_bps` resta a due esiti: vietato aggiungere `p_vertical`.

## 7. Circuito di copertura spot (C11): cosa resta da collegare

Il guard e' provato in `test_balance_guard.py` e `test_coverage_properties.py` (mutation check: 6 bug introdotti a mano, tutti intercettati).
`tests/test_c11_wiring.py` impedisce di aggirarlo e trasforma ogni "da fare" in un test che FALLISCE appena il file compare:

- sempre attivo: niente identificatori su futures/perp/margin/leverage/spostamento fondi; solo `exchange.py`, `user_stream.py`, `ws_feed.py`, `backfill.py` importano l'SDK Coinbase; i metodi d'ordine dell'SDK solo in `exchange.py`; `build_order_body` solo in `spot.py`/`exchange.py`; `_reserve` solo nel guard; `CoverageTicket` emesso solo dal guard.
- il v1 `modules/order_execution.py` e' DISATTIVATO con un `raise` all'import (altrimenti `main.py` e il `CMD` del Dockerfile avrebbero avviato ordini senza copertura); un `xfail(strict)` obbliga a cancellarlo quando nasce `gateway.py`.
- 15 promemoria (skip, visibili con `pytest -rs`): `exchange.py` deve avere `create_order(ticket)` e solo metodi in allowlist (T11, T12); `gateway.py`/`shadow_gateway.py` richiedono `guard: BalanceGuard` obbligatorio e chiamano `authorize` (T18); `sizing`, `virtual_account`, `kill_switch`, `balance_feed`, `reconciler`, `watchdog`, `positions`, `books`, `app` devono usare i componenti previsti e i test T4, T13-T15, T17 devono esistere per nome.
- Regola: implementando uno di quei file non si modificano i tripwire; si scrivono i test richiesti.

Da fare (ordine B.3): `sizing.py` (T4) -> `risk_manager.py` -> `virtual_account.py` (T17) -> `exchange.py` (T11) -> gateway live/shadow (T13-T15) -> `balance_feed.py` -> `reconciler.py` -> ledger con `dust_*` -> KillSwitch `coverage_breach`.

## 8. Registro e note per chi riprende

- Deviazioni minime dalla risoluzione sulla copertura (ora anche in spec 4.1.6): `OrderSpec` e `ProductMeta` stanno in `execution/orders.py` (evita un import circolare); `BalanceBook.apply_fill` accetta `trade_id` (fill idempotente); un fill senza riserva non modifica i saldi e fa scattare l'allarme; il drift non si valuta sul primo snapshot.
- `DEFAULT_SPOT_VENUES={"CBE"}` confermato dal probe (BTC, ETH, SOL: `product_type=SPOT`); `DEFAULT_PORTFOLIO_TYPES={"DEFAULT","CONSUMER"}` non confermato.
- `load_bars` taglia `end` all'ultima barra reale salvata (un backfill incompleto non deve sembrare un mercato fermo); `store_complete_until_end=True` solo per chi sa che lo store e' completo.
- `round_bps` e' half-up (196.5 -> 197); `CostModel` tiene i float esatti.
- `ruff --fix` lanciato per errore su `modules/` ha toccato 8 file v1; ripristinati (lezione in `tasks/lessons.md`).
- Conflitti risolti: `.claude/CONFLITTO.md` viene cancellato dopo ogni risoluzione; le decisioni lunghe si conservano in `docs/decisions/`.

## 9. Indicatori (sez. 2.2.1.b): note di implementazione

- Batch e incrementale condividono le formule scalari (`rsi_value`, `rvd_value`, `bb_stats`, `bb_pos`, `zscore_value`); le ricorrenze sono scritte due volte e provate uguali. Ancora: prima riga dello storico del prodotto; `warm_up` rigioca TUTTO lo storico (1,1 milioni di righe in circa 10 s, budget 120 s).
- Raffinamento rispetto alla risoluzione: `RollingSum` tiene un limite superiore dell'errore di arrotondamento e ricalcola con `fsum` in lettura quando supera 1e-14 relativo (oltre al ricalcolo giornaliero). Serve per le finestre quasi vuote dopo un giorno senza volume: senza, il VWAP sbagliava di 2e-8 bps (limite 1e-9). Trovato dal test sintetico, non visibile sui dati reali recenti.
- Il batch rende le sequenze `seq_*` tutto-o-niente (bug trovato dal test sintetico: all'inizio dello storico compilava le colonne una alla volta).
- 6 bug introdotti a mano (Wilder, pari merito del percentile, somme mobili, sequenze parziali, finestra dei rendimenti, visibilita' HTF): tutti intercettati.
- `script/probe_coinbase.py` usa `wilder_atr` (punto O2 della risoluzione). `round_bps` accetta anche scalari numpy.
- `pytest -m data` esegue la parita' sullo storico reale (45 s); escluso dal run di default insieme a `-m live`.
