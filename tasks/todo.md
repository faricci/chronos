# Chronos v2 - avanzamento rispetto alla spec

Spec: `docs/specs/chronos-system1-spec.md` v2.6.1. Branch: `reachitect/v2-opus55-spec` (scopo: implementare quella spec).
Aggiornato: 2026-10-18. Test: 427 passati, 27 promemoria in skip (`pytest -rs`), 5 `data`/`live` da lanciare a mano; mypy strict e ruff puliti sui file v2.
Verificato anche su Python 3.12.15 in un container pulito (411 test, mypy strict e ruff puliti; i 3 test sui dati reali restano a parte). Si lavora in un venv locale 3.14.

## 1. Dove siamo, per fase dell'Appendice A

| Fase | Contenuto (spec) | Stato | Cosa c'e' / cosa manca |
|------|------------------|-------|------------------------|
| P0 Fondamenta | sez. 0.5 | CODICE COMPLETO; soak di 48 h IN CORSO | FATTO e testato: `config`, `risk/costs`, `market/{backfill, timeframes, indicator_defs, indicators_batch, indicators, bars, orderbook, ws_feed}`, `ledger/positions`, `dryrun/recorder`, probe. Storico 3 anni (BTC 1,56 M righe, ETH 1,51 M, SOL 1,44 M). Parita' engine/batch: storico intero (`pytest -m data`) e barre live del `BarService` (errore massimo 7e-14). Soak di 48 h: il primo avvio (18-ott 01:25 locale) e' morto dopo 51 minuti per un riavvio del PC; RILANCIATO il 18-ott 07:43 locale (sez. 11). Probe con la key View FATTO il 18-ott (H5 confermato: sez. 12). MANCA solo l'esito del soak |
| P1 Fattibilita', dati, baseline | sez. 1, 3 (parte CPU) | 0% | sentiment (0/9 file), `training/*` (0/12), `BaselineDecider` |
| P2 Decision + Risk | sez. 2, 4 | circa 15% | FATTO: nucleo di copertura spot (`rounding`, `orders`, `balance`, `balance_guard`, `spot`) e formule in `costs.py`. MANCA: `features/*` (0/3), `decision/*` (0/6), `sizing`, `risk_manager`, `kill_switch`, `exchange`, `gateway`, `user_stream`, `reconciler`, `watchdog`, `balance_feed`, `app.py` |
| P3 Dry run | sez. 5 | 0% | 0/12 file di `modules/dryrun/` |
| P4-P6 | live-micro, rampa, cloud | non iniziato | H5 (probe con la key View) gia' confermato: sez. 12 |

Ordine P0 della spec (sez. 0.5): config -> backfill+load_bars -> indicators_batch -> indicators -> bars -> ws_feed+orderbook -> ledger -> recorder.
Criterio di uscita P0 (sez. 0.5): storico backfillato (FATTO); parita' batch/incrementale su >= 5.000 barre per prodotto (FATTO: `pytest -m data`, tutti e tre i prodotti, storico intero); 48 h di stream WS senza buchi non gestiti e 48 h di `BarService` con `rest_revision_ratio <= 0.005` (IN CORSO: vedi sez. 11).

## 2. Per sezione della spec (file nominati dalla spec)

| Sezione | Fatti | Mancanti |
|---------|-------|----------|
| 0.3 CostModel | `risk/costs.py` | - |
| 0.5 / 2.3-A market | `backfill`, `timeframes` (extra), `indicators`, `indicators_batch`, `indicator_defs` (extra), `bars`, `orderbook`, `ws_feed` | - (tutto P0 di sez. 2.3-A e' scritto) |
| 1 Sentiment | - | tutti i 9 file |
| 2.3-B/C features, decision | - | `features/{schemas,state_builder,serializer}`, `decision/{questions,client,protocol,baseline,policy,engine}` |
| 3.5 training, decider | - | 12 file `training/` (la parte LLM e' fuori scope C8); `decider/` fuori scope |
| 4.3 risk / execution | `config`, `rounding`, `balance`, `balance_guard`, `spot`, `orders` (extra), `ledger/positions` | `exchange`, `gateway`, `user_stream`, `reconciler`, `watchdog`, `balance_feed`, `risk/{sizing,risk_manager,kill_switch}`, `app` |
| 5.3 dry run | `resolve_mode` (in `config`), `recorder` | `fill_model`, `virtual_account`, `shadow_gateway`, `bracket_sim`, `books`, `replay`, `label_resolver`, `learner`, `registry`, `report`, `parity` |

In pratica: 14 dei circa 65 file nominati dalla spec sono scritti (tutta la fase P0 di codice). Il grosso del lavoro resta il livello dati/decisione (sez. 1-3) e il dry run (sez. 5).

## 3. Decisioni dell'architetto

| Conflitto | Esito | Dove |
|-----------|-------|------|
| CostModel vs probe, EV a due esiti | applicato, spec v2.3 | spec 0.3, 0.4, 0.5 |
| Copertura spot, leva, bracket | applicato, spec v2.4 (C11, 4.1.6). ATTENZIONE: l'attached bracket e' eliminato, il bracket e' sempre standalone | spec 4.1.6; dettaglio e ipotesi H1-H6 in `docs/decisions/2026-10-17-copertura-spot.md` |
| Definizione esatta degli indicatori | applicato, spec v2.5 (sez. 2.2.1.b; `trades_z_1d` rimosso). Testo completo, firme e test T1-T10 in `docs/decisions/2026-10-17-indicatori.md` | `indicators*.py` fatti; T8-T10 e O1 sono promemoria in `tests/test_obligations.py` |
| Fonte canonica delle barre 1m | applicato, spec v2.6 e taratura 2.6.1 (S = 120 s). Le barre 1m sono le candele REST lette a T+S (S=120 s); `BarAggregator` eliminato; decisione calcolata a t+S con `ts_bar_close = t`. Nata da una misura reale: le barre dai trade WS differiscono dalle candele REST in circa meta' dei minuti | spec 0.5 (I1, I2), 2.3 A.2; `docs/decisions/2026-10-18-barre-rest.md` |

## 4. Prossimi passi

1. Chiudere P0: leggere l'esito del soak (sez. 11, circa il 20-ott alle 07:45 locale).
2. Poi il percorso di esecuzione collegato al guard: `exchange.py` -> `sizing.py` -> `risk_manager.py` -> `virtual_account.py` -> `gateway.py`/`shadow_gateway.py` (i tripwire di `test_c11_wiring.py` lo impongono).
3. P1: `training/feasibility.py` per primo (la spec lo vuole prima di tutto il resto del training).

## 5. Aperti

- [ ] `training/labeling.py`: `forward_rv_bucket` deve usare `indicators_batch.rv_per_day_bps`; label `vol_regime` solo da circa 375 giorni dopo l'inizio dello storico
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
- 27 promemoria (skip, visibili con `pytest -rs`): `exchange.py` deve avere `create_order(ticket)` e solo metodi in allowlist (T11, T12); `gateway.py`/`shadow_gateway.py` richiedono `guard: BalanceGuard` obbligatorio e chiamano `authorize` (T18); `sizing`, `virtual_account`, `kill_switch`, `balance_feed`, `reconciler`, `watchdog`, `positions`, `books`, `app` devono usare i componenti previsti e i test T4, T13-T15, T17 devono esistere per nome.
- Regola: implementando uno di quei file non si modificano i tripwire; si scrivono i test richiesti.

Da fare (ordine B.3): `sizing.py` (T4) -> `risk_manager.py` -> `virtual_account.py` (T17) -> `exchange.py` (T11) -> gateway live/shadow (T13-T15) -> `balance_feed.py` -> `reconciler.py` -> ledger con `dust_*` -> KillSwitch `coverage_breach`.

## 8. Registro e note per chi riprende

- Deviazioni minime dalla risoluzione sulla copertura (ora anche in spec 4.1.6): `OrderSpec` e `ProductMeta` stanno in `execution/orders.py` (evita un import circolare); `BalanceBook.apply_fill` accetta `trade_id` (fill idempotente); un fill senza riserva non modifica i saldi e fa scattare l'allarme; il drift non si valuta sul primo snapshot.
- `DEFAULT_SPOT_VENUES={"CBE"}` confermato dal probe (BTC, ETH, SOL: `product_type=SPOT`); `DEFAULT_PORTFOLIO_TYPES={"DEFAULT","CONSUMER"}` confermato per l'account dell'utente (`portfolio_type=DEFAULT`).
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

## 10. Barre 1m dalle candele REST (spec v2.6): note di implementazione

- `bars.py`: `BarFinalizer` (puro), `BarService`, `BarStoreWriter`, `rest_revisions`. 20 test; 7 bug introdotti a mano (prontezza, barra piatta senza prova, `flat_unproven` anticipato, candela in corso usata come dato, fetch fallito che pubblica, `complete_until`, ordine) tutti intercettati. Il test `test_engine_parity_on_runtime_bars` verifica I2 su 7.200 barre.
- `ws_feed.py` non costruisce barre: inoltra i trade deduplicati (`on_trades`) e gli intervalli danneggiati (`on_damage`, per il futuro `BracketSimulator`).
- `backfill.py`: `settle_s` (un minuto si scarica solo se `ts + 1 min + S <= now`), `StoreCandleSource` (replay e test).
- `config.py`: sezione `market.bars`, `risk.kill_switch.stale_bar_s` (>= `rest_max_delay_s` + 90 (default 270 con 180)), `gate_ops.max_rest_revision_ratio`.
- Misure reali (17-18 ott 2026) che hanno guidato la decisione: nessun ritardo di consegna dei trade WS; circa meta' dei minuti con volume diverso tra trade WS e candele REST, sempre a coppie di minuti adiacenti che si compensano; la candela REST continua a cambiare dopo la chiusura (prova breve: circa 25 s; prova lunga su 70 candele: fino a 92 s, da cui S = 120 s).
- Obblighi per i moduli futuri (`DecisionEngine`, `KillSwitch`, `learner`, `report`, `label_resolver`, `replay`, `app`) sono tripwire in `tests/test_obligations.py`.
- Il soak (`run_soak`) non dichiara: calcola a fine corsa parita' engine/batch sulle barre salvate e `rest_revision_ratio`, e scrive un verdetto per criterio.

## 11. Soak di 48 h di P0 (in corso)

Primo avvio 2026-10-18 01:25 locale: interrotto dopo 51 minuti da un riavvio del PC (eventi di sistema 02:24-02:27 locale; copie in `p0_soak_run1*`). RILANCIATO il 2026-10-18 alle 07:43 (ora locale, PID 24800) come processo staccato, PID in `data/reports/p0_soak.pid`:
`python -m modules.dryrun.recorder record --hours 48` (config di default: S = 120 s).

- Cosa fa: backfill incrementale -> warm-up dei 3 engine sull'intero storico -> feed WS + registrazione grezza (`data/ws_recordings/`) + `BarService` che salva le barre nello store -> engine su ogni barra pubblicata.
- Avanzamento: `data/reports/p0_soak.json` si aggiorna ogni 10 minuti; log in `data/reports/p0_soak.log` e `.err.log`.
- Fine (circa 20-ott 07:43 locale): il report finale contiene `checks.parity` (engine live contro `compute_indicators` sulle barre salvate), `checks.rest_revisions` (minuti riletti da REST con eta' >= 15 min) e `verdict` con un booleano per criterio. Un criterio non misurato e' falso.
- Verifica dei buchi di sequenza sulle registrazioni: `python -m modules.dryrun.recorder verify --days 2`.
- Fermarlo: `Stop-Process -Id (Get-Content data\reports\p0_soak.pid)`; in quel caso non viene scritto il verdetto finale (il report intermedio resta). Se il PC si riavvia il processo muore (e' gia' successo una volta; lo standby da rete elettrica e' disattivato): va rilanciato e le 48 ore ripartono.
- Se `rest_revision_ratio` supera 0,005: classificare le revisioni (valore / comparsa / scomparsa) e salire di gradino S 120 -> 180 -> 300 (tabella in `docs/decisions/2026-10-18-barre-rest.md`), con un nuovo soak; il criterio non si allenta.
- Nota: 21 minuti del 17-ott 22:11-22:17 UTC (3 prodotti x 7) sono nello store con S = 30 s, scritti dalla prova breve prima della taratura; il soak li consuma cosi' come sono (I2) e non vanno sovrascritti durante la corsa.
- Misure che hanno portato a S = 120 (18-ott): candele ancora in cambiamento dopo 30 s: 33%, dopo 60 s: 5,7%, dopo 90 s: 1,4%, dopo 120 s: 0 su 70 (max 92 s); candele con eta' >= 1 h: 0 revisioni su ~36.000 minuti.

## 12. Probe con la key View (2026-10-18)

Lanciato con `python -m script.probe_coinbase --products BTC-EUR ETH-EUR SOL-EUR` (non `python script\probe_coinbase.py`: con quel comando `modules` non si trova). Esito in `data/probe/coinbase_probe.json` (ignorato da git, senza segreti).

- Permessi: `can_view=true`, `can_trade=false`, `can_transfer=false`, `portfolio_type=DEFAULT`. H5 confermato: la key e' di sola lettura, nessun permesso di spostare fondi; `DEFAULT` e' tra i tipi di portfolio ammessi.
- Fee reali: tier `Intro`, maker 25 bps, taker 50 bps, volume 30 giorni 0. Sono inferiori alle fee presunte della spec (60/120): le presunte restano come fallback prudente (`FallbackFees`), non cambiano i default.
- Level2 senza e con autenticazione: ricevuto `l2_data` in entrambi i casi.
- Fattibilita' con le fee reali (maker, k=4/2, 1d): lift richiesto +0,05 BTC, +0,03 ETH, +0,03 SOL.
