# Chronos v2 - avanzamento rispetto alla spec

Spec: `docs/specs/chronos-system1-spec.md` v2.10-draft. Branch: `reachitect/v2-opus55-spec` (scopo: implementare quella spec).
Aggiornato: 2026-10-18. Test: 974 passati, 17 promemoria in skip (`pytest -rs`), 1 xfail (v1 `order_execution`), 6 `data`/`live` da lanciare a mano; mypy strict e ruff puliti sui file v2.
Python 3.12 verificato in container pulito all'epoca della v2.6 (411 test); da rifare a fine P1. Si lavora in un venv locale 3.14.

## 1. Dove siamo, per fase dell'Appendice A

| Fase | Contenuto (spec) | Stato | Cosa c'e' / cosa manca |
|------|------------------|-------|------------------------|
| P0 Fondamenta | sez. 0.5 | CODICE COMPLETO; soak di 48 h IN CORSO | FATTO e testato: `config`, `risk/costs`, `market/{backfill, timeframes, indicator_defs, indicators_batch, indicators, bars, orderbook, ws_feed}`, `ledger/positions`, `dryrun/recorder`, probe. Storico 3 anni (BTC 1,56 M righe, ETH 1,51 M, SOL 1,44 M). Parita' engine/batch: storico intero (`pytest -m data`) e barre live del `BarService` (errore massimo 7e-14). Soak di 48 h: il primo avvio (18-ott 01:25 locale) e' morto dopo 51 minuti per un riavvio del PC; RILANCIATO il 18-ott 07:43 locale (sez. 11). Probe con la key View FATTO il 18-ott (H5 confermato: sez. 12). MANCA solo l'esito del soak |
| P1 Fattibilita', dati, baseline | sez. 1, 3 (parte CPU) | CODICE COMPLETO; GATE IN CORSO | FATTO: fattibilita' (GO), label, split, dataset reale (159k righe), baseline LightGBM addestrato, backtest, `tune_policy`, `evaluate`, sentiment (servizio di raccolta attivo). IN CORSO: tuning soglie, poi gate G0-G2/GE sul modello reale (sez. 13-15). MANCA: storico news per l'addestramento (GDELT), parti LLM (fuori scope C8) |
| P2 Decision + Risk | sez. 2, 4 | circa 55% | FATTO: copertura spot (`rounding`, `orders`, `balance`, `balance_guard`, `spot`), `costs`, `features/*` (4/4), `decision/{protocol,questions,policy,client,baseline}` (5/6), `sizing`, `risk_manager`, `kill_switch`. MANCA: `decision/engine`, `exchange`, `gateway`, `user_stream`, `reconciler`, `watchdog` (con uscita per orizzonte H), `balance_feed`, `app.py` |
| P3 Dry run | sez. 5 | circa 25% | FATTO (anticipati per il backtest): `fill_model`, `virtual_account`, `sim_exchange`, `bar_gateway`, `recorder`. MANCA: `shadow_gateway`, `bracket_sim`, `books`, `replay`, `label_resolver`, `learner`, `registry`, `report`, `parity`, faccia a tick del fill model |
| P4-P6 | live-micro, rampa, cloud | non iniziato | H5 (probe con la key View) gia' confermato: sez. 12 |

Ordine P0 della spec (sez. 0.5): config -> backfill+load_bars -> indicators_batch -> indicators -> bars -> ws_feed+orderbook -> ledger -> recorder.
Criterio di uscita P0 (sez. 0.5): storico backfillato (FATTO); parita' batch/incrementale su >= 5.000 barre per prodotto (FATTO: `pytest -m data`, tutti e tre i prodotti, storico intero); 48 h di stream WS senza buchi non gestiti e 48 h di `BarService` con `rest_revision_ratio <= 0.005` (IN CORSO: vedi sez. 11).

## 2. Per sezione della spec (file nominati dalla spec)

| Sezione | Fatti | Mancanti |
|---------|-------|----------|
| 0.3 CostModel | `risk/costs.py` | - |
| 0.5 / 2.3-A market | `backfill`, `timeframes` (extra), `indicators`, `indicators_batch`, `indicator_defs` (extra), `bars`, `orderbook`, `ws_feed` | - (tutto P0 di sez. 2.3-A e' scritto) |
| 1 Sentiment | `modules/sentiment/*` (13 file, tra cui schemi, fonti, dedup, classifier, tagger, aggregator, store, service, calibrazione) | storico news per l'addestramento; decisione S2 (semantica MARKET) prima di usarlo nel training |
| 2.3-B/C features, decision | `features/{schemas,serializer,state_builder}` (+ `state_matrix`), `decision/{questions,client,protocol,baseline,policy}` | `decision/engine` |
| 3.5 training, decider | `training/{labeling,sampling,splits,feasibility,config,build_dataset,baseline_lgbm,fold_spans,backtest,tune_policy,evaluate}` | parte LLM (fuori scope C8); `decider/` fuori scope |
| 4.3 risk / execution | `config`, `rounding`, `balance`, `balance_guard`, `spot`, `orders` (extra), `ledger/positions` | `exchange`, `gateway`, `user_stream`, `reconciler`, `watchdog`, `balance_feed`, `app` (fatti: `risk/{sizing,risk_manager,kill_switch}`) |
| 5.3 dry run | `resolve_mode` (in `config`), `recorder` | `shadow_gateway`, `bracket_sim`, `books`, `replay`, `label_resolver`, `learner`, `registry`, `report`, `parity` (fatti: `fill_model`, `virtual_account`, `recorder`, e in extra `sim_exchange`, `bar_gateway`) |

In pratica: circa 45 dei circa 65 file nominati dalla spec sono scritti (P0 e P1 di codice completi). Resta il percorso di esecuzione reale (P2) e il resto del dry run (P3).

## 3. Decisioni dell'architetto

| Conflitto | Esito | Dove |
|-----------|-------|------|
| CostModel vs probe, EV a due esiti | applicato, spec v2.3 | spec 0.3, 0.4, 0.5 |
| Copertura spot, leva, bracket | applicato, spec v2.4 (C11, 4.1.6). ATTENZIONE: l'attached bracket e' eliminato, il bracket e' sempre standalone | spec 4.1.6; dettaglio e ipotesi H1-H6 in `docs/decisions/2026-10-17-copertura-spot.md` |
| Definizione esatta degli indicatori | applicato, spec v2.5 (sez. 2.2.1.b; `trades_z_1d` rimosso). Testo completo, firme e test T1-T10 in `docs/decisions/2026-10-17-indicatori.md` | `indicators*.py` fatti; T8-T10 e O1 sono promemoria in `tests/test_obligations.py` |
| Fonte canonica delle barre 1m | applicato, spec v2.6 e taratura 2.6.1 (S = 120 s). Le barre 1m sono le candele REST lette a T+S (S=120 s); `BarAggregator` eliminato; decisione calcolata a t+S con `ts_bar_close = t`. Nata da una misura reale: le barre dai trade WS differiscono dalle candele REST in circa meta' dei minuti | spec 0.5 (I1, I2), 2.3 A.2; `docs/decisions/2026-10-18-barre-rest.md` |

## 4. Prossimi passi

1. Chiudere P1: finire il tuning, lanciare `python -m training.evaluate` e leggere G0/G1/G2/GE (sez. 13). Se il verdetto e' NO-GO o INCONCLUSO: decisione dell'utente.
2. Chiudere P0: esito del soak (sez. 11, circa il 20-ott alle 07:45 locale).
3. P2: `exchange.py` -> `gateway.py`/`shadow_gateway.py` -> `balance_feed` -> `reconciler` -> `watchdog` (con uscita per orizzonte) -> `decision/engine` -> `app` (i tripwire di `test_c11_wiring.py` lo impongono).

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

## 13. P1 in corso (avviato 2026-10-18)

Perimetro (Appendice A): `training/feasibility.py` PER PRIMO; poi sentiment (sez. 1); poi la parte CPU della sez. 3 (label, split, `BaselineDecider`, backtest). Il backtest vuole `DecisionPolicy`, `RiskManager`/`sizing` e `BarFillModel` (sez. 3.5 punto 8): questi pezzi di P2/P3 vengono anticipati nella misura minima necessaria, con i tripwire di copertura (C11) che restano validi.

- [x] `modules/dryrun/fill_model.py` (`FillModelParams`, `BarFillModel`, kernel numba condivisi con le label); la faccia a tick (`TickFillModel`) e' di P3
- [x] `training/labeling.py` (`triple_barrier` numba, `forward_rv_bucket`; T8/T9), `modules/risk/label_config.py` (`LabelConfig` condiviso col runtime)
- [x] `training/sampling.py` (CUSUM, griglia oraria, pesi di unicita'), `training/splits.py` (walk-forward purgato, holdout 120 giorni)
- [x] `training/feasibility.py` v2.7 (risoluzione architetto + quant: `docs/decisions/2026-10-18-fattibilita.md`) eseguito sullo storico reale: **GO**, `data/datasets/label_config.json` scritto (6h, 3/1.5, maker, H=15 g, F=3, spread 3 bps, `lift_0` +0,074, N_eff 665, jackknife max +0,089; holdout dal 2026-06-20). Spec alla v2.7.
- [x] `modules/features/{schemas,serializer}` (+ golden), `modules/decision/{protocol,questions,policy,client}`, `modules/risk/{sizing,risk_manager}` (T4, controllo a mutazioni: 14/14 intercettate)
- [x] ricalcolo della fattibilita' con le regole del quant/architetto (K1-K6) e aggiornamento della spec alla 2.7
- [x] O1/O2/O3 risolti dall'architetto (`docs/decisions/2026-10-18-stato-backtest-baseline.md`, `...-spread.md`): lo stato non contiene lo spread (`costs` dal `label_config`), backtest a 1 minuto per i gate e 15 per la ricerca, iperparametri del baseline
- [x] `modules/features/{state_builder,state_matrix}` (T10: 27.168 stati identici byte per byte fra percorso batch e incrementale; matrice vettoriale = percorso scalare), `modules/decision/baseline` (`BaselineDecider`)
- [x] `training/{config,build_dataset,baseline_lgbm,fold_spans,backtest,tune_policy,evaluate}`; `modules/dryrun/{sim_exchange,bar_gateway,virtual_account}`, `modules/risk/kill_switch` (questi ultimi due da sub-agente), `modules/execution/ids`
- [x] dataset reale costruito (`data/datasets/<run>`, 159.000 righe) e baseline addestrato (`models/baseline/baseline-lgbm@2026-10-18.1`)
- [x] tuning delle soglie FINITO (2026-10-18, `models/baseline/.../tune_report.json`): nessuna combinazione ha PnL netto positivo su nessun fold di validation (miglior PnL con >= 30 trade: fold_0 -0,31 EUR, fold_1 -2,78, fold_2 -0,48 su 100 EUR). fold_3, fold_4 e final: ZERO operazioni in tutte le 80 combinazioni. Causa dei fold 3/4 (diagnostica `data/reports/diag_skips.log`): la testa `vol_regime` e' quasi uniforme (probabilita' medie ~0,2 per classe), l'argmax cade su 4 (fold_3) o 0/4 (fold_4) e il veto di regime (1,2,3) blocca ogni BUY: difetto di progetto della regola di veto, non del codice. Per `final` il veto non basta a spiegarlo (da indagare). DECISIONE CRITICA IN ATTESA (utente / quant + architetto) prima di lanciare `evaluate`.
- [x] conflitto P1c risolto (quant + architetto, `.claude/CONFLITTO.md`, sez. "Risposta architetto"): regime dal token `atr_1d_pctile_365d` (soglie 5/35/65/95, testa `vol_regime` solo diagnostica), `p_be_buffer` nel RiskManager, calibrazione OOF con N_eff >= 200, griglia <= 20 combinazioni, fold senza trade = non positivo, B0/B2 nel report; cause misurate: fold 3/4 testa regime uniforme, final `ev_negative` 2861/2861 (p_edge ~0,22 contro p_be ~0,40)
- [x] implementazione P1c completata (2026-10-18, l'utente ha scelto l'opzione A e ha chiuso l'idea del paniere): policy con regime dal token `atr_1d_pctile_365d`, `p_be_buffer` nel RiskManager, sizing, backtest (cache v2), calibrazione OOF_PLATT_V1 (`baseline_lgbm`), `tune_policy` (griglia <= 20, esiti `untunable_*`, `val_diag`), `evaluate` (fold piatti, E5, B0/B2), `training/regime_deciles.py`; spec 2.10; decisione archiviata in `docs/decisions/2026-10-18-p1c-regime-ev.md`; test 1.027+ verdi, mypy strict e ruff puliti, 12/12 mutanti intercettati
- [x] ESITO P1 sul modello `baseline-lgbm@2026-10-18.2` (nuovo, calibrazione OOF): **GE = NO-GO** (exit 1). Tuning: fold_0 `untunable_calibration` (N_eff 97 < 200), fold_1 `untunable_pnl`, fold_2/3/4 e final `untunable_ev` (il 95 percentile di `p_edge - p_breakeven` e' negativo: -0,03 / -0,02 / -0,05 / -0,06). Valutazione: G0 PASS (lift holdout +0,061, limite 0,221), G1 FAIL 0/5 (ECE sopra 0,05), G2 FAIL 3/5 (servono 4), GE NO-GO per zero trade. Baseline di confronto: B2 sempre investito con lo stesso bracket = -85,6 bps/trade su 1.072 candidati; B0 buy and hold: +45% nel fold 1 e +39% nell'holdout, negativo negli altri. Diagnostica dei decili (`data/reports/regime_deciles.md`): media netta di tutti i fill -58,2 bps, nessun decile di volatilita' con vantaggio positivo. Criterio di uscita P1 NON superato. Decisione dell'utente richiesta sul passo successivo (news, altri approcci, fermarsi).
- [ ] RISCHIO sullo storico delle news (2026-10-19): la spec (sez. 3.1.5) prevede GDELT DOC 2.0 per 3 anni di titoli, ma le fonti pubbliche dicono che l'API copre ufficialmente solo gli ultimi 3 mesi (date piu' vecchie 'possono' rispondere, non garantito). Una prova diretta il 19-ott e' stata respinta con HTTP 429 (limite di 1 richiesta ogni 5 s, anche a 6-12 s di distanza), quindi NON verificato. Alternative da valutare con l'utente: GDELT GKG (file da 15 minuti, centinaia di GB), CC-NEWS, dataset pubblici di news crypto, oppure solo Fear & Greed (storico giornaliero gratuito dal 2018). Prima di costruire il backfill serve la scelta della fonte e la decisione S2 (`.claude/CONFLITTO-sentiment.md`).
- [x] P2 (2026-10-19), tutto con test, mutazioni e tripwire C11 intatti: `execution/exchange.py` (solo ticket di copertura, `read_only`, 19 mutanti), `balance_feed.py`, `gateway.py` (`LiveExecutionGateway` + `entry_context` + `place_missing_bracket`; non istanziabile senza modalita' live e conferma), `dryrun/shadow_gateway.py` (stesso `BalanceGuard`, 31 mutanti), `user_stream.py`, `reconciler.py`, `watchdog.py` (con uscita per orizzonte H, 44 mutanti), `ledger` (`get_order`, scritture fill+posizione transazionali), `decision/engine.py` (parita' con il backtest su 8.640 righe, 24 mutanti). Eliminato l'esecutore v1 `modules/order_execution.py` e adattato il tripwire come previsto dal suo stesso marcatore.
- [x] `modules/app.py` + Dockerfile/compose (2026-10-19): avvio ordinato, solo `ShadowExecutionGateway` e exchange `read_only=True` (modalita' live rifiutata all'avvio con exit 2, nessun percorso live), healthcheck sul file `data/heartbeat`, redazione dei segreti, sentiment al motore solo se `model_card.sentiment_available`, policy `untunable` = nessun ingresso. Smoke reale di 7 minuti con la key View: OK. `.dockerignore` ora esclude `.env`, `data/`, `models/`.
- [x] P3 (2026-10-19): `dryrun/{registry (regola L5 + CLI locale), books, parity, bracket_sim, label_resolver, replay, learner, report}.py`, `TickFillModel`. Tutti con test, mutazioni (26-41 difetti ciascuno, tutti intercettati) e tripwire.
- [x] VERIFICA COMPLESSIVA 2026-10-19: 3 esecuzioni complete identiche, 1.747 passati, 2 saltati; mypy --strict su 77 file e ruff puliti; nessun promemoria dei tripwire ancora in sospeso; il fallimento isolato visto da un agente non si e' ripresentato.
- [x] LACUNE (1), (2), (4), (5) e la parte agente di (9) CHIUSE il 2026-10-19 (cablaggio provato su dati reali, vedi la voce sotto): `app.py` avvia i book di `dry_run.books` con `ShadowBookManager`/`build_books` (hook `wrap_gateway` e `restore` in `books.py`; challenger solo se l'alias esiste nel `ModelRegistry`; champion dall'alias `champion`, bootstrap dalla versione piu' recente di `models/baseline` SOLO a registry vuoto, loggato); a un riavvio `modules/dryrun/restore.py` + `VirtualAccount.restore` + `ShadowExecutionGateway.restore` ricostruiscono saldi, riserve e bracket dal ledger e le barre del fermo risolvono TP/SL; tabella `closed_trades` (D7) in `positions.py` con migrazione, scritta nella transazione del fill di chiusura; `modules/telemetry/` (`StatusWriter`, `EventLog`, `OpsRecorder`, `EquityWriter`) scrive `status.json`, `data/events`, `data/equity/DAY.jsonl`, `data/reports/ops/DAY.json`. Mancano di D1-D15: `DbSnapshotter`, `schemas.py` con export, `modules/control/`.
- [x] PROVA SU DATI REALI del cablaggio (2026-10-19; scratch in cartella temporanea, `data/` mai scritto): (a)+(b) `python -m script.replay_app_day` (replay della registrazione WS e delle barre del 19-ott, 00:30-15:00 UTC, BTC-EUR + SOL-EUR, riavvio alle 08:00, modello DEMO a barriere brevi + `forced_buy_rate` 0,05 del random_entry, fee reali dell'account 25/50 bps): 1.740 barre, 3,3 milioni di messaggi, 3.480 decisioni; random_entry 4 fill di acquisto e 3 trade chiusi (2 HORIZON, 1 SL; PnL netto simulato -0,94 EUR), buy_and_hold 2 posizioni (equity 99,88), champion reale-like 0 ingressi (tutte le decisioni BUY scartate `ev_buffer`: nessun edge); cassa dei book = cassa ricalcolata dai fill, `closed_trades` 3 righe, `python -m modules.dryrun.report` calcola PnL, N trade, gate (uptime 1,0 pass, crash 0 pass); dopo il riavvio la posizione SOL e' stata chiusa per orizzonte. (c) `python -m modules.app` reale con la key View su copia dei dati, 22 + 4 + 6 minuti con due riavvii: buy_and_hold 3 posizioni reali simulate, random_entry 2 trade chiusi per orizzonte (-0,1285 e -0,3771 EUR), file status/events/equity/ops aggiornati, spegnimento pulito (exit 0, 0,6 s), riavvio con bracket ripristinati (4), uscita interrotta annullata e bracket ripiazzato. TP non osservato su dati reali (coperto dai test). Il champion reale e' `untunable`: la prova dei trade usa un modello DEMO (`"demo": true`), non una strategia.
- [ ] LACUNE ANCORA APERTE dopo P2/P3: (3) `BracketSimulator` a tick non e' collegato allo shadow gateway (che simula a barre); (6) `live_weight` delle label live non applicato in `build_dataset`; (7) la regola di promozione L5 (lettura di 'batte', soglie di campione) va confermata dal trader-quant; (8) il formato del canale `user` di Coinbase non e' provato con chiave reale: soak in sola lettura; (9) `modules/control/` e `DbSnapshotter` (GUI P7); (10) news: scegliere la fonte dello storico e decidere S2. Difetti trovati dalla prova reale e NON corretti: `read_recording` non recupera i membri gzip DOPO un membro troncato (le registrazioni WS del 18-ott sono leggibili solo fino alle 00:21 UTC; corretto solo il crash con `zlib.error`); il book level2 di ETH-EUR del 19-ott non ha snapshot nel file leggibile; `watchdog.py` logga a livello ERROR anche l'uscita per orizzonte (il LogForwarder la riduce a un evento `watchdog_exit` di avviso); un bracket ripiazzato al riavvio non rivede le barre del fermo.
- [x] Correzioni dopo la prova (2026-10-19): il registratore WS apre sempre un file `.partN` nuovo a ogni avvio (prima accodava a un gzip che un crash poteva aver troncato: le registrazioni del 18-ott dopo le 00:21 UTC non sono recuperabili); il watchdog logga a livello info l'uscita per orizzonte (pianificata); aggiunta la fonte RSS The Block (collettore riavviato, `theblock_rss: OK`). Fonti news attive: CoinDesk, Cointelegraph, Decrypt, The Block, CryptoSlate, Fear & Greed (nessuno storico: servono mesi prima di addestrare con le news; S2 aperta).
- [ ] (precedente, superato) tuning delle soglie sul modello reale, poi `python -m training.evaluate` (gate G0, G1, G2, GE). Prime misure sul baseline: AUC edge per fold 0,43/0,57/0,59/0,61/0,50, holdout 0,553; il logloss dell'edge non batte il prior del train (G2 probabilmente FALLITO); verdetto solo dopo evaluate
- [x] spec 2.9: fase P7 (centro di controllo GUI opzionale, sez. 6) scritta dall'architetto e applicata; nessun codice. Domande aperte per l'utente in sez. 6.10 (8). Prerequisiti dell'agente D1-D15 (telemetria, canale comandi) nelle fasi P0-P3
- [x] spec 2.8 applicata (O1.7/O2/O3); sentiment: servizio di raccolta news attivo (PID in `data/reports/sentiment_collect.pid`)
- [ ] residui spec 2.8: sez. 3.5 punti 8-10 (riferimenti), watchdog con uscita per orizzonte (P2), sez. 3.1.6 soglie per fold e split finale; cancellare `.claude/CONFLITTO*.md` (decisioni gia' in `docs/decisions/`); O3-K da confermare col quant (non bloccante)

## 14. Esito dello studio di fattibilita' (2026-10-18, v2.7)

- Verdetto GO. Combinazione: `atr_tf` 6h, `k_tp/k_sl` 3/1.5, ingresso maker, H = 15 giorni, `entry_first_bar` 3, spread di label 3 bps (probe con minimo 3). Pareggio entro 0,01 con 4/1.5 e 3/2, risolto con N_eff.
- `lift_0` +0,074 (il modello deve portare P(UPPER) da circa 0,33 a 0,405: +7,4 punti), lift grezzo +0,058; per prodotto BTC +0,095, ETH +0,062, SOL +0,074 (tutti dentro la guardia). Jackknife semestrale: massimo +0,089 (limite 0,10): il margine e' stretto. Il semestre 2023H2 ha lift -0,096 (rialzo); il chi2 di omogeneita' fra semestri ha p = 0,004: il tasso base dipende dal regime.
- N_eff 665 campioni indipendenti (rho 0,54), circa 254 l'anno; durata mediana dei trade 48 h, p95 257 h. Per rilevare un edge di 0,08 servono circa 345 trade indipendenti (K5): il backtest da solo e' al limite della potenza; gate GE e dry run decidono.
- Le cifre sono sulla finestra di selezione (fino al 2026-06-18): l'holdout (dal 2026-06-20) non e' ancora stato guardato; G0 lo fara' dopo l'addestramento.
