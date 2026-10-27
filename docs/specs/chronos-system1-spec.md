# Chronos v2 — Specifica Tecnica "System 1" Decision Architecture

Versione: 2.14-draft | Data: 2026-10-26 | Stato: proposta per implementazione
Target implementatore: Sonnet 5.5 (ogni blocco "Linee Guida" e' scritto per essere usato come prompt diretto)

Changelog 2.1: perimetro infrastrutturale a costo zero (locale, CPU-only, AWS fuori scope); nuova sez. 5 "Dry Run"; piano di rilascio riscritto; la sandbox Coinbase e' statica e non viene piu' usata per validare i payload.

Changelog 2.2 (risposta alla review di implementazione):
- modello di costo unico e asimmetrico (sez. 0.3), usato da label, RiskManager, FillModel e stato;
- parametri di barriera v1 ricavati da dati reali del 2026-10-17 (sez. 0.4): ATR giornaliero, ingresso maker, orizzonte di giorni; i default 15m/240 min della 2.1 non coprivano le fee;
- prodotti v1: BTC-EUR, ETH-EUR, SOL-EUR (XLM-EUR escluso per illiquidita');
- nuova sez. 0.5 "Fondamenta P0" (config, backfill, market stream, storage, Python 3.12, branch);
- nuova sez. 0.6 "Ambito per book" (RiskManager, KillSwitch, daily loss);
- backend di default `baseline` anche nella config; policy versionata per modello nel registry;
- veto di regime solo nella DecisionPolicy, regime = argmax (superato dal 2.10: il regime viene dal token di stato `atr_1d_pctile_365d`);
- calibrazione del sentiment non bloccante e flusso di etichettatura a costo zero;
- gate del dry run separato in operativo (G-DRY-OPS) ed economico (G-DRY-ECON);
- `level2` verificato: funziona senza autenticazione.

Changelog 2.3 (risoluzione del conflitto sul CostModel):
- `script/probe_coinbase.py` usa `CostModel` senza calcoli di costo propri, si esegue con `python -m script.probe_coinbase` e legge le credenziali solo da env;
- tabella dei lift della sez. 0.4 ricalcolata con `CostModel` (righe 1h e 6h);
- EV a due esiti dichiarato limite inferiore dell'EV reale (sez. 0.3); `tune_policy` valuta le soglie con il backtest completo (sez. 3.1.6, 3.5); diagnostica VERTICAL nello studio di fattibilita' (sez. 3.1.3).

Changelog 2.4 (vincoli finanziari assoluti):
- nuovo vincolo C11 (solo spot, copertura totale, nessun debito) e nuova sez. 4.1.6 "Copertura spot" (`BalanceBook`, `BalanceGuard`, `CoverageTicket`);
- bracket sempre standalone dopo lo stato terminale dell'ingresso; `attached_order_configuration` non piu' usato;
- `size_cap` (4.1.2) calcolato con la formula di copertura invece del cuscinetto 0.98; nuovo cap `risk.max_order_quote`;
- arrotondamento del limite d'ingresso prima di SL/TP e del sizing (4.1.3);
- riconciliazione dei saldi con regola `epoch` e staleness fail-closed per i BUY (4.1.5);
- `VirtualAccount` compone lo stesso `BalanceBook` del live (5.3).

Changelog 2.4.1 (allineamento all'implementazione, nessun cambio di comportamento):
- `load_bars()` taglia `end` all'ultima barra reale salvata (0.5); nuovo `modules/market/timeframes.py` (2.3-A);
- tipi condivisi in `modules/execution/orders.py`, fill idempotenti per `trade_id`, test di collegamento `tests/test_c11_wiring.py`, v1 `order_execution.py` disattivato (4.1.6).

Changelog 2.5 (definizione esatta delle feature): nuova sez. 2.2.1.b (formule, minimi storici, unita'; serie HTF solo su barre chiuse; ancora unica dello storico); rimosso `liquidity.trades_z_1d` (non ricostruibile dallo storico OHLCV); `vol_z_1d` su `ln(1 + quote volume)`; warm-up del runtime = replay completo (2.3 A.4); label `vol_regime` con la stessa `RVd` dello stato e quantili su finestre concluse (3.1.3).

Changelog 2.6 (fonte canonica delle barre 1m): le barre 1m del runtime sono le candele REST `get_public_candles` lette `S` secondi dopo la chiusura del minuto (`BarService`, sez. 2.3 A.2), la stessa fonte dello storico; i trade WebSocket non costruiscono piu' barre (`BarAggregator` eliminato); la decisione si calcola a `t + S` con `ts_bar_close = t`; nuova sezione `market.bars` della config; guardia stale separata per barre (`stale_bar_s`) e ticker; audit notturno delle revisioni REST (L0) e criterio G-DRY-OPS relativo; replay delle barre dallo store (`StoreCandleSource`).

Changelog 2.6.1 (taratura di S, misure del 2026-10-18): le candele REST cambiano fino a ~92 s dopo la chiusura (0 su 70 dopo 120 s; nessuna revisione sulle candele con eta' >= 1 h): `market.bars.rest_delay_s` = 120 s (validator 60..300) e valori derivati `rest_max_delay_s` = 180, `decision_max_lag_s` = 240, `stale_bar_s` = 270; nuovo validator `decision_max_lag_s < stale_bar_s`; scala di taratura di S guidata da `rest_revision_ratio` (sez. 2.3 A.2).

Changelog 2.7 (studio di fattibilita' con fee reali 25/50 bps e regole del trader-quant):
- parametri di barriera: fonte di verita' unica `data/datasets/label_config.json` scritto da `training/feasibility.py`; la sez. 0.4 non contiene piu' valori normativi (i default 1d / 4-2 / 10 giorni non sono sostenibili: `p_vertical` 0.60);
- studio di fattibilita': finestra di selezione pre-holdout, `lift_0` senza drift (detrend solo per la scelta, mai in stato o label), eleggibilita' `p_vertical <= 0.05` con il piu' piccolo H, `H <= 15` giorni, pareggio entro 0.01 risolto con `N_eff`, jackknife semestrale e guardia per prodotto nel GO, diagnostiche (chi2 di omogeneita', H = 20, durate reali);
- campioni indipendenti: somma dei pesi di unicita' sulla durata reale `[t, exit]`, corretta per la correlazione fra prodotti;
- label: `entry_first_bar` F = 1 + ceil(S/60) = 3 per maker e taker, limite al `close[t+F-1]`, finestra d'ingresso di `T_in` barre, nella barra di fill solo lo SL; spread di label `max(probe, 3 bps)` incluso nello `sha256`; `label_window_end(t)` unica fine finestra per purge, selezione e LabelResolver;
- holdout: inizio congelato nel `label_config` (`selection.holdout_start`), `purged_walk_forward(..., holdout_start=...)`; embargo 2 giorni uniformato (3.5);
- valutazione: nuovi gate G0 (cambio di regime sull'holdout) e GE (evidenza assoluta E1-E5) con esito GO / INCONCLUSO / NO-GO; la rampa R0 richiede GE = GO; ECE nella regione di decisione solo diagnostica;
- tabella 5.1.3 allineata a 3.1.3.

Changelog 2.8 (spread e costi dello stato, backtest, baseline):
- lo stato non contiene piu' lo spread vivo (`liquidity.spread_bps` rimosso): il blocco `costs` e' calcolato dal `label_config` (fee, slippage e spread di label via `spread_for`) e dall'ATR dello snapshot, identico in training e serving; fee e spread vivi li usa solo il `RiskManager`; `StateBuilder.build(*, snapshot, btc, sentiment, position, close_t)` senza parametri di costo; lo schema resta `chronos.state.v1` perche' nessun dataset o modello era stato rilasciato;
- backtest (sez. 3.5 punto 8): cadenza di 1 minuto per i gate e per il backtest di riferimento, 15 minuti solo nella ricerca di `tune_policy`; un solo book con equity comune, ordini tutti autorizzati dal `BalanceGuard` su un `VirtualAccount`, kill switch reale; fill dell'ingresso maker tutto-o-niente con haircut sul volume della barra; uscita per orizzonte come nella label;
- baseline LightGBM: iperparametri fissi, pesi di unicita' normalizzati a media 1, sottocampionamento di HOLD con correzione del prior, LONG sintetici dal kernel delle label, calibrazione Platt/temperatura sul validation del proprio fold, soglie della policy per fold, split finale `final_fit_split`;
- l'uscita per orizzonte `H` deve essere implementata nel watchdog (buco della sez. 5.1.2).

Changelog 2.9 (centro di controllo, fase P7):
- nuova sez. 6 "Centro di controllo": GUI opzionale in quattro container del profilo compose `ui` (`ui-db` PostgreSQL, `ui-ingest` e `ui-api` FastAPI, `ui-web` SPA statica su nginx), senza reti in comune con `chronos`, senza accesso a Internet, porta solo su `127.0.0.1`; `docker compose up` continua ad avviare solo l'agente;
- l'agente resta l'unico scrittore dei propri file: la GUI li legge `:ro` e li copia nel proprio DB; nuovi artefatti dell'agente, attivi anche senza GUI (`modules/telemetry/`): `status.json`, eventi ed equity in JSONL, snapshot periodici dei DB SQLite (sez. 6.3, prerequisiti D1-D15 nelle fasi P0-P3);
- canale comandi a basso privilegio (`modules/control/`, sez. 6.4): file firmati HMAC in un volume che l'agente monta `:ro`; dalla GUI solo `pause_entries`, `cancel_pending_entry`, `close_position` (percorso normale `RiskManager` -> gateway -> `BalanceGuard`) e `flatten_all`; ripresa e reset di `coverage_breach` solo dal canale locale; cambio dry/live mai dalla GUI;
- nuova guardia `manual_pause` (4.1.4); esito `MANUAL` neutro per la loss streak; tabella `closed_trades` anche nel DB live (5.2); minuti successivi a un comando manuale esclusi dalla parita' delle decisioni (5.1.7);
- nuovi test `tests/test_ui_isolation.py`, `tests/test_control_commands.py`, `tests/test_telemetry.py`, `tests/test_agent_independent_of_ui.py` e obblighi in `tests/test_obligations.py`; `tests/test_c11_wiring.py` invariato; riga P7 nell'Appendice A; dipendenze della GUI nell'Appendice B (immagini separate).

Changelog 2.10 (regime dal token di stato, buffer sul pareggio, calibrazione OOF, fold piatti; risoluzione del conflitto P1c):
- il veto di regime non legge piu' la risposta del modello: il regime e' una funzione del token di stato `atr_1d_pctile_365d` (intero 0..100), tagliato a soglie fisse 5/35/65/95 (`REGIME_RULE = "ATR1D_PCTILE365:5/35/65/95"`), uguale in training, backtest e runtime; ingresso ammesso nei regimi 1, 2, 3 (`ENTRY_REGIMES`); token assente -> nessun ingresso (`regime_unavailable`); le soglie sono decise prima del tuning e non si ottimizzano (sez. 2.1.2, 2.2.5, 4.1.1);
- la testa `vol_regime` resta fra le domande (`QUESTIONS_V1` invariata) ma e' solo log e diagnostica (`model_regime` nel `DecisionRecord`): non decide ne' il veto ne' il sizing (sez. 2.1, 2.2.4);
- la policy perde la soglia `p_edge_min` e i regimi nel `policy.json`, che portano invece `p_be_buffer` e `regime_rule`; un `policy.json` con le chiavi vecchie non si carica; il motivo `edge_veto` sparisce (sez. 2.1.2, 2.2.5);
- `p_be_buffer` (0 <= buffer <= 0.20) e' un margine di probabilita' sul pareggio della singola decisione, applicato solo dal `RiskManager`: `EV(p_edge) > ev_min_bps` e `EV(p_edge) >= p_be_buffer * (W + L)`, cioe' `p_edge >= p_breakeven(t) + p_be_buffer`; nuovo skip `ev_buffer`; il pavimento della singola decisione (`p_floor`) alimenta lo `scale_p` del sizing, che con `p_floor >= p_edge_full` vale 0 o 1 invece di sollevare un errore; default dei moltiplicatori di regime 1.0 per i regimi 1, 2, 3 (sez. 0.3, 4.1, 4.1.1, 4.1.2);
- calibrazione di `edge` fuori campione (`OOF_PLATT_V1`): Platt su punteggi cross-fitted sul train del fold (4 blocchi con purge ed embargo) piu' il validation, mai sul test; se `N_eff` < 200 la calibrazione del fold non e' affidabile (fold piatto) e per il modello finale e' un errore (sez. 3.1.6);
- griglia di `tune_policy` di al piu' 20 combinazioni (`tau_buy` a 2 quantili, `c_min` in 2 valori, buffer in 5 valori; `tau_sell` fisso a 0.50); scelta solo con almeno 30 trade e PnL di validation > 0; esiti `untunable_calibration`, `no_candidates_regime`, `untunable_ev`, `untunable_pnl`, e diagnostica `val_diag` (sez. 3.1.6, 3.5 punto 10);
- fold piatti nel criterio P1 e in GE: un fold o un holdout senza trade ha PnL 0 e non e' positivo; E5 = False senza trade dell'holdout; `fold_status` in `evaluation.json`; E1-E3 usano solo i trade esistenti (sez. 3.4, Appendice A);
- baseline banali B0 (buy and hold equal-weight) e B2 (sempre investito con lo stesso bracket) solo nel report di `evaluate`, senza effetto su alcun gate; cache del backtest `FLAT_CACHE_VERSION = "v2"` (contiene `vol_pctile`); nuovo CLI di sola lettura `training/regime_deciles.py` per la diagnostica dei decili del regime (sez. 3.5 punti 8-10).

Changelog 2.11 (revisioni REST e promozione, conflitto P1e):
- parte A, criterio sulle revisioni delle candele REST:
- il criterio "Barre REST" di G-DRY-OPS e di P0 passa dal rapporto di minuti rivisti (`rest_revision_ratio`, ora diagnostica con allarme a 0.005) all'impatto delle revisioni sugli stati e sulle decisioni della versione in uso: R1 quota di stati con spostamento > 0.01 <= 0.005, R2 spostamento massimo <= 0.02, R3 flip di azionabilita' <= 0.001, R4 completezza (zero minuti comparsi o scomparsi); la soglia 0.005 non si sposta, cambia la grandezza misurata: il rapporto non ordina i prodotti per impatto e il suo unico rimedio (alzare S) non agisce sulle revisioni rilette ore dopo (soak P0 del 18-20/10/2026) (sez. 0.5, 5.1.7);
- il calcolo e' di L0 (sez. 5.1.6): mondo servito `S` = `fill_flat_bars` delle RIGHE REALI dello store; mondo rivisto `F` = le stesse righe con tutte le revisioni di valore note, griglia ricostruita con la stessa `fill_flat_bars` (applicare la revisione alla griglia gia' riempita lascerebbe i minuti piatti seguenti al close vecchio); lo store non cambia (I2); le revisioni del giorno sono in `data/reports/learner/revisions/<giorno>.parquet`; nuovo `modules/dryrun/revision_impact.py` (funzioni pure e CLI `certify`), `classify_revisions` in `bars.py`, `masked_buy_prob`/`masked_sell_prob` in `policy.py`;
- `gate_ops` ha 4 chiavi nuove (`revision_shift_threshold`, `max_revision_shift_share`, `max_revision_shift`, `max_revision_flip_share`; i YAML esistenti restano validi); il report separa diagnostica (`gate_ops.diagnostics`, `alarms`) e criteri R1-R4; `anomalies.revision_by_version` assegna R1-R3 alla versione che li ha falliti; il verdetto del soak P0 ha `rest_bars_complete` al posto di `rest_revision_ratio_within_limit` (sez. 5.1.7, 5.2);
- la scala di taratura di S (sez. 2.3 A.2) vale solo per le revisioni di assestamento; certificazione separata del soak P0 gia' concluso (`data/reports/p0_soak_certification.json`), `p0_soak.json` non si riscrive.
- P1e-bis (2026-10-23, dopo il primo `fail` della certificazione del soak P0, e dichiarato come tale): R1 e R3 su blocchi di 1.440 stati consecutivi (giorno UTC intero = blocco; giorno con meno stati esteso a 1.440 stati verso l'interno della serie; serie piu' corta `unmeasured`; precedenza `fail`, `unmeasured`, `pass`), R2 e R4 per giorno UTC; soglie invariate; nessuna chiave di config (la dimensione del blocco, `ImpactThresholds.block_states`, e' la definizione del test di produzione); in L0 i minuti finali senza scambi di D-1 sono stati del giorno se lo store prosegue oltre la mezzanotte (K16), un D-1 parziale si misura con il giorno prima (giorno ausiliario, mai giudicato); certificazione `chronos.p0_bars_certification.v2` in un file nuovo (mai riscritto) con `previous_run`, candele rilette salvate con sha256 ed esito anche con la regola v1 come diagnostica; diagnostica senza soglia (finestra mobile di 24 h, revisioni indipendenti, 5 spostamenti maggiori con origine, uso di R2); il soak 18-20/10/2026 non viene ricertificato con la regola v2 (decisione U2): il criterio P0 "barre REST" resta non certificato in attesa di giorni UTC interi del dry run (sez. 0.5, 5.1.7, 5.2).
- parte B, regola di promozione L5 e rollback:
- regola L5 riscritta (sez. 5.1.6): finestra comune di 90 giorni fuori campione per entrambi (`data.final_train_end` nel model card), challenger con G0, G1, G2, G4 e GE = GO, almeno 30 trade nella finestra, bootstrap a blocchi ACCOPPIATO della differenza di PnL sull'OOS comune (quantile 5% > 0, solo se anche il champion e' GO), PnL > 0 e Sharpe/PnL strettamente maggiori, drawdown non peggiore e <= 5%, shadow di 14 giorni con R1-R4; di default nessuna promozione finche' i dati sono insufficienti; il learner non promuove mai;
- difetti del codice corretti: L5 giudica il challenger in carica PRIMA del refit (`challenger_before`: L2 sposta l'alias sulla versione appena addestrata e L5 non poteva mai promuovere) e non dipende piu' dall'esito di L3/L1/L2; una finestra comune per i due modelli invece di una per `label_config`; `HoldoutBacktester` con policy non tarabile restituisce metriche definite (zero trade, Sharpe 0.0) invece di `Sharpe None` per sempre; `_gate_flags` porta G3 e G4 nel manifest;
- `LearnerConfig.promotion` diventa `PromotionConfig` (`backtest_recent_days`, `min_shadow_days`, `superiority_quantile`; minimo di trade e drawdown da `gate_econ`); `PromotionRule`, `PromotionEvidence`, `BacktestEvidence`, `GateSummary`, `OosEvidence` in `modules/dryrun/registry.py`;
- `training/evaluate.py` scrive `oos_trades.jsonl` e `oos_spans.json` nella versione (blocchi di E2 condivisi: `BlockGrid`, `BlockedValues`); nuovo `training/oos_compare.py` (confronto accoppiato); `training/backtest.py::equity_sharpe_and_drawdown` e' l'unica definizione di Sharpe e drawdown;
- rollback solo dalla CLI locale `python -m modules.dryrun.registry rollback champion --reason ...` (`--reason` obbligatorio anche per `promote`, `op` e `reason` in `aliases.json`), effetto al reload delle 00:30 UTC, nessun ordine toccato; la promozione manuale scavalca L5 (decisione U2); raccomandazioni di rollback nel report (`rollback_advice`, solo consiglio); reload delle 00:30 con swap atomico e `model_version_changed`, learner delle 00:15 come processo figlio a bassa priorita' (`modules/dryrun/schedule.py`), regola `label_config_mismatch` per la SELL e modello registrato all'ingresso della posizione (`positions.model_version`, `label_config_sha256`, `horizon_min`).

Changelog 2.12 (deriva delle feature e contesto BTC, conflitto P1f; decisione e pre-registrazione in `docs/decisions/2026-10-25-p1f-psi-v2.md`, esito di SIM-P1f in `docs/decisions/2026-10-25-p1f-psi-v2-esito.md`):
- il criterio "Parita' feature" di G-DRY-OPS (PSI < 0.10 per ogni feature, gli stati al minuto di UN giorno contro gli stati orari degli ultimi 90 giorni) non misurava una deriva: un giorno di feature persistenti cade in pochi decili e la funzione con floor 1e-4 ha un tetto di ~8,3 (quattro prodotti con un valore ciascuno in 4 / 3 / 2 decili: 4,69 / 5,67 / 6,81). Misurato sul dry run vero: 37 feature su 70 sopra 0,10, massimo 7,81, due notti su due; la parita' live/batch era invece 0,9979. Diventa la diagnostica `feature_psi_daily_v1` (`dry_run.gate_ops.max_feature_psi` e `risk.kill_switch.psi_threshold` ne sono solo le soglie di allarme: nessun criterio, anomalia o guardia); sparisce dalle anomalie di parita' (che erano condizione P4 di L5: con il PSI v1 nessun challenger poteva essere promosso);
- nuovo criterio `feature_drift` = regola PSI-v2 (`modules/dryrun/drift.py`): deriva degli input rispetto alla distribuzione di TRAINING del modello in uso (riferimento R congelato per versione di champion), 12 bin ai quantili di R, smoothing di Jeffreys, p-value empirici sul nullo delle finestre di R stesso e correzione minP di Westfall-Young con alfa 1%; ipotesi nulla e limite per le feature lente (`n_eff_7d`) nel documento di decisione. Le costanti sono la DEFINIZIONE pre-registrata del test (`drift.RULE`), non chiavi di config. Fino all'adozione da parte di SIM-P1f il criterio e' `unmeasured` (mai `pass`, mai `fail`): G-DRY-OPS resta al massimo INCOMPLETE e non chiude P3;
- `script/sim_p1f.py` (SIM-P1f): calibrazione S0-S5 con regola di decisione D0-D3 scritta prima dei risultati (D0 diagnosi riprodotta; D1 FWER fuori campione <= 2%; D2 soglia <= 0,25 sulle feature con `n_eff_7d` >= 50; D3 potenza >= 80% a delta = 1 sigma; finestra di 7 giorni, una sola ripetizione a 14); artefatti `data/reports/sim_p1f/<versione>/00_preregistration.json`, `10_calibration.json`, `20_s6.json` scritti una volta (apertura esclusiva), `drift.load_calibration` rifiuta una pre-registrazione modificata dopo la corsa o una regola diversa da quella del codice. Esito della corsa vera (2026-10-25, `docs/decisions/2026-10-25-p1f-psi-v2-esito.md`): `rejected`. D0 riprodotta (889 giorni su 889 sopra 0,10, mediane delle lente 3,05-5,85); a 7 giorni D1 non superata (FWER fuori campione 2,9-3,1% contro 2%) e D2 non superata (`x_f` delle 14 feature veloci 1,06-1,27 contro 0,25), D3 superata (0,89-0,98); a 14 giorni D1 3,8-4,7%, D2 0,74-0,84, D3 1,00. Nessuna regola adottata: `feature_drift` resta `unmeasured` (G-DRY-OPS al massimo INCOMPLETE); aperto il conflitto P1f-bis, nessun parametro ritoccato.
- contesto BTC degli stati delle alt: un'alt il cui stato non ha la barra BTC dello stesso ts non compra (`HOLD`, motivo `btc_context_stale`, campo `btc_context` del `DecisionRecord`); SELL, bracket e watchdog invariati. L0 classifica le righe disallineate (`live_missing`, `batch_missing`, i due difetti `value_mismatch` e `flag_inconsistent`), misura spostamento `m` e flip di azionabilita' col modello e la policy della versione che ha deciso, e separa la parita' degli stati (`match` senza le righe disallineate, `match_all` con tutte); la parita' delle decisioni esclude i minuti `btc_context_stale` del live (`excluded_btc_stale`);
- G-DRY-OPS: `feature_psi` rimosso; nuovi criteri `feature_drift` (letto dall'ultimo giorno della finestra) e `btc_context_alignment` (su tutti i giorni, `gate_ops.max_btc_stale_share` 0.005 e `max_btc_stale_shift` 0.01, zero flip, zero `value_mismatch`); diagnostica `feature_psi_daily_v1`; `alarms` ha `feature_drift:<prodotto>`; nessuna chiave di config rimossa (i YAML esistenti restano validi);
- la guardia `drift` di `SystemGuards` (sez. 4.1.4) resta senza scrittore: collegare l'allarme PSI-v2 a un blocco degli ingressi e' una decisione separata, da prendere dopo l'adozione e solo per prodotto.


Changelog 2.13 (regola PSI-v3 e contesto BTC con policy non tarabile, conflitto P1f-bis; decisione e pre-registrazione in `docs/decisions/2026-10-26-p1f-bis-psi-v3.md`, esito di SIM-P1f-bis in `docs/decisions/2026-10-26-p1f-bis-psi-v3-esito.md`):
- SIM-P1f ha respinto PSI-v2 (2026-10-25, `rejected`: il PSI nullo di una settimana di rendimenti orari ha il 99-esimo percentile a 1,06-1,27 perche' la loro scala segue il regime di volatilita', persistente per settimane; FWER fuori campione 2,9-3,1% contro 2%). PSI-v2 e' CHIUSA: `python -m script.sim_p1f run` rifiuta (exit 2), `drift.RULE` e il suo file restano leggibili;
- `feature_drift` = PSI-v3 (`PSI_V3_VOLSTD`, `drift.RULE_V3`, ULTIMA regola): i 14 rendimenti veloci (`ret_15m_bps`, `ret_1h_bps`, `seq_1h_bps_00..11`) standardizzati per `rv_1d_bps` dello stesso stato (`z_f = f / rv_1d_bps`, NaN se la volatilita' non e' finita o <= 0), famiglia del minP = le z con `n_eff_7d` >= 50 misurato in R (insieme J), finestra unica di 7 giorni, stesso R di PSI-v2, alfa 1%. Decisione D1 (FWER fuori campione <= 2%), D1h (allarmi sulle settimane non sovrapposte dell'holdout `[holdout_start, giorno - 7)` <= k_H, il minimo k con P(Bin(n_H; 0,01) > k) <= 0,05; n_H = 0 = falsa), D2 (`x_f` <= 0,25 per f in J), D3 (potenza a 1 sigma_z >= 80% per f in J); `adopted` se e solo se J non e' vuoto e tutte e quattro sono vere, altrimenti `rejected` definitivo. `script/sim_p1f_v3.py` (`run`, `s6`) in `data/reports/sim_p1f/PSI_V3_VOLSTD/<versione>/`; la corsa e' rifiutata se il documento di decisione e i tre file di codice non sono tracciati e puliti in git, e una seconda corsa per la stessa versione e' rifiutata. Esito della corsa vera (2026-10-26, `docs/decisions/2026-10-26-p1f-bis-psi-v3-esito.md`): `rejected`. J = le 14 z (`n_eff_7d` 168), `x_f` 0,18-0,21 (D2 superata), potenza 1,00 (D3 superata), 0 allarmi su 51 settimane di holdout (D1h superata), ma FWER fuori campione BTC 1,47%, ETH 2,04%, SOL 1,02% (D1 non superata per un allarme su ETH-EUR: 18 invece di 17 su ~883 finestre). Nessun parametro ritoccato; `feature_drift` = `not_testable` (definitivo), criterio di gate `unmeasured`; aperto il conflitto P1g (calibrazione di `p_edge`).
- perimetro dichiarato: le 28 feature non veloci e il livello di volatilita' (`n_eff_7d` < 50) non sono nel test; diagnostica senza soglia di gate per prodotto: `support` (quota di stati con almeno una feature fuori da `[r_min, r_max]` di R; > 0 in `alarms` come `feature_support:<prodotto>`, mai nel gate), percentile del PSI della settimana fra le settimane del training per le feature non testate, PSI di `p_edge` contro le previsioni del champion sull'HOLDOUT. L'output riporta `scope` (testate, non testate con `n_eff_7d`, nota `drift.scope_note`) e la diagnostica `feature_drift_scope`;
- stato `not_testable` del blocco `feature_drift` (PSI-v3 respinta, definitivo): il criterio di gate resta `unmeasured` (nessun nuovo stato di gate); non esiste un criterio di calibrazione di `p_edge` sugli esiti in un gate richiesto per P3 (`edge_ece` e' di G-DRY-ECON, che non e' richiesto per P3, e non e' misurabile nei 7 giorni minimi), quindi G-DRY-OPS non puo' chiudersi senza `feature_drift` (conflitto P1g);
- `btc_context_alignment` con policy `untunable`: se la policy della versione che ha deciso e' ESPLICITAMENTE `untunable` (`revision_impact.policy_kind`, non un `policy.json` assente) e i book del modello (`champion`, `challenger`) non hanno nel giorno nessun BUY (compresi gli HOLD `btc_context_stale`) ne' intent per quella versione, i flip di azionabilita' valgono esattamente 0 (`flips_basis = policy_untunable`, `untunable_check` nel blocco); un solo BUY o intent e' `untunable_violation` e il criterio fallisce. R3 (`revision_decision_flips`) NON e' esteso (domanda in P1g).


Changelog 2.14 (chiusura di `feature_drift` con PSI-v3 respinta, conflitto P1g; decisione in `docs/decisions/2026-10-26-p1g-chiusura-drift.md`):
- `feature_drift` con PSI-v3 respinta (`not_testable`, definitivo per la versione): il criterio assume lo stato `not_covered` (non certificato, non e' mai `pass`, non blocca G-DRY-OPS, e' elencato in `gate_ops.not_covered`) se in OGNI giorno della finestra la sorveglianza `feature_drift_watch` e' misurata per ogni prodotto e la diagnostica `support` e il PSI di `p_edge` sono presenti; altrimenti `unmeasured`. La sorveglianza usa `reference_diagnostic` e `c_diagnostic` della corsa respinta con la stessa `window_stat`, e' solo un avviso (`alarms`: `feature_drift_watch:<prodotto>`) e dichiara i falsi allarmi fuori campione (`watch.false_alarm_oos`); nessuna costante di PSI toccata. La regola vale per ogni champion presente e futuro, fissata prima dei loro dati;
- `python -m modules.dryrun.revision_impact certify --days START END --products ...` certifica il criterio P0 "barre REST" sui giorni UTC interi del dry run (finestra `[START 00:00Z, END+1 00:00Z)`, stesso calcolo del modo soak, chiave `dry_run_days` al posto del soak report);
- il criterio P0 "barre REST" si certifica sui tre prodotti del modello (BTC-EUR, ETH-EUR, SOL-EUR; decisione dell'utente dopo il `fail` di XRP-EUR, che resta diagnostica): `pass` sui giorni 24-25 ott del dry run (`p0_bars_certification_dry_2026-10-24_2026-10-25_btc_eth_sol.json`); `gate_ops.window_start` (data, default null) esclude dalla finestra del gate i giorni precedenti; il controllo delle barre duplicate o fuori ordine dell'`OpsRecorder` e' per-processo;
- differito a P4 (solo spec, nessun codice): `decider` nel `DecisionRecord`, `flips_basis` su R3, calibrazione di `p_edge` in G-DRY-ECON, guardia `drift` per prodotto per le versioni `not_covered`, stop di calibrazione in R0 (dettaglio in 5.1.7).

---

## 0. Vincoli trasversali (leggere prima di ogni sezione)

Questi vincoli valgono per tutte le sezioni. Sono il motivo di molte scelte che seguono.

| ID | Vincolo | Conseguenza architetturale |
|----|---------|----------------------------|
| C1 | Coinbase Advanced Trade spot: **no short selling**. | `SELL` significa sempre "chiudi/riduci un long esistente". Le azioni valide dipendono dallo stato della posizione. |
| C2 | Le fee retail di Coinbase al tier di volume piu' basso (presunte maker 60 / taker 120 bps, da confermare con il probe della sez. 0.5) superano l'ATR a 1h dei prodotti v1 (30–60 bps misurati il 2026-10-17). | La **cadenza decisionale** resta 1 minuto, ma le **barriere e l'orizzonte** sono su scala di giorni (ATR giornaliero, sez. 0.4). Ogni TP deve superare il proprio costo (sez. 0.3). Il fee tier reale va letto da `get_transaction_summary`, mai hardcoded. |
| C3 | La latenza in millisecondi del modello riduce il ritardo decisione→ordine, ma il costo dominante sono fee e spread. | Ingresso di default con limit **post-only maker** (fee maker, nessuno spread pagato) e timeout; alternativa limit marketable (taker) selezionabile. Lo slippage realizzato viene misurato. |
| C4 | OpenJev e' un'implementazione **protocol-compatible** di Jev, non il modello Jev ufficiale. Calibrazione e velocita' non sono verificate indipendentemente (dichiarato dal progetto stesso). Non esiste un modello chiamato "OpenJev Flash 9B": l'equivalente piu' vicino e' il backend `Clef-flash 1.0` (base Qwen3.5-9B). | Il **protocollo** `/v1/systemone` e' il contratto stabile; il backend e' intercambiabile (OpenJev zero-shot in fase 0, server `chronos-decider` con LoRA fine-tuned in fase 1+). |
| C5 | L'host attuale e' Windows 11 senza GPU NVIDIA. Un modello 9B a latenza di ms richiede GPU NVIDIA (>=24 GB VRAM; NVFP4 di Clef richiede Blackwell). | Il container `chronos` e' CPU-only. Nella fase locale il backend decisionale di default e' `BaselineDecider` (LightGBM, CPU). Il container `decider` LLM e' definito ma attivo solo con il profilo compose `gpu`. |
| C6 | Nessuna fonte news "webhook" e' sia affidabile sia gratuita. | Il real-time news e' implementato come polling RSS con GET condizionale (ETag/Last-Modified) a 30–60 s, piu' sorgenti opzionali con token. Latenza effettiva 1–5 min: accettabile perche' l'orizzonte e' di giorni (C2). |
| C7 | Point-in-time correctness. | Ogni feature usata in training deve essere ricostruibile con soli dati noti al tempo `t`. Per le news vale `ingested_at`, non `published_at`. |
| C8 | **Costo zero nella fase attuale.** L'infrastruttura AWS e' fuori scope di questa specifica e verra' progettata separatamente. | Tutto gira in Docker sulla macchina locale. Nessun servizio a pagamento (niente API LLM, niente GPU cloud, niente feed news a pagamento). Il codice resta **cloud-ready** senza dipendere da AWS: configurazione da env, segreti solo da env, container stateless salvo i volumi `data/` e `models/`, storage dietro interfacce (`NewsStore`, `PositionLedger`, `ModelRegistry`) cosi' che SQLite/filesystem possano essere sostituiti in seguito senza toccare la logica. Vietato importare `boto3` o SDK cloud in questa fase. |
| C9 | **Dry mode di default.** Il bot deve poter girare in produzione senza inviare ordini reali, simulando gli esiti sul flusso di mercato reale. | `execution.mode: dry` e' il default. Il passaggio a `live` richiede una conferma esplicita (sez. 5). Pipeline unica: in dry cambia solo l'implementazione iniettata dell'`ExecutionGateway`. |
| C10 | **Micro-capitale.** Il live parte con ordini minimi e cresce solo dopo evidenza netta di fee. | Le size sono vincolate da `base_min_size`/`quote_min_size` del prodotto (lette dall'API, mai hardcoded). Rampa di capitale a gradini con gate (sez. 5.1.7). Le fee sono percentuali, quindi gli ordini piccoli non peggiorano il costo in %, ma al tier di volume piu' basso le fee sono le piu' alte: ogni trade deve superare il fee floor della sez. 4.1. |
| C11 | **Solo spot, copertura totale, nessun debito** (vincoli finanziari assoluti di CLAUDE.md). Vietati futures, margin, opzioni, leva. Ogni ordine e' coperto al 100% dal saldo reale; nessuna logica puo' rendere negativo un saldo. | Registro spot all'avvio (`product_type == "SPOT"`), whitelist del body d'ordine, nessun endpoint futures/perpetuals nel wrapper (`SpotViolation`, exit code 4). Ogni ordine passa da `BalanceGuard.authorize()` (sez. 4.1.6), che verifica in memoria `saldo disponibile >= importo + fee` e prenota l'importo; senza `CoverageTicket` nessun ordine puo' essere inviato, in live come in dry. Guard non disattivabile da config. Rischio massimo di un ordine = sua riserva. |

### 0.1 Difetti del codice attuale da NON riportare nella v2

| File | Difetto | Correzione v2 |
|------|---------|---------------|
| [modules/order_execution.py](../../modules/order_execution.py) | Legge `config["risk_management"]` ma il template definisce `trading.risk_management` -> `KeyError` a runtime. | Config tipizzata con pydantic (sez. 4.3). |
| [modules/order_execution.py](../../modules/order_execution.py) | `place_order(..., entry_price=, take_profit_price=)`: kwargs inesistenti nell'SDK. | Payload `attached_order_configuration.trigger_bracket_gtc` documentato (sez. 4). |
| [modules/order_execution.py](../../modules/order_execution.py) | TP/SL calcolati sempre "long-style" anche per `SELL`. | Bracket solo su ingressi long (C1). |
| [modules/data_preprocessing.py](../../modules/data_preprocessing.py) | `MinMaxScaler.fit_transform` sull'intera finestra: leakage in training, scala instabile in inferenza. | Feature scale-free (rendimenti in bps, ATR%, z-score rolling). |
| [main.py](../../main.py) | `size = 5` fisso; posizioni solo in memoria; loop sincrono `schedule`. | Sizing a rischio fisso, ledger SQLite, orchestratore asyncio. |
| [modules/trading_logic.py](../../modules/trading_logic.py) | Previsione di prezzo + soglie arbitrarie `±0.01` su valori normalizzati. | Decisione tipizzata calibrata (sez. 2). |

### 0.2 Topologia target

```
                      +-------------------- container: chronos (CPU) ---------------------+
 Coinbase WS ---------> MarketStream -> LocalOrderBook, trade (spread, simulatori)        |
 Coinbase REST -------> BarService(1m, T+S) ------------> IndicatorEngine(1m,1h,6h,1d)    |
 (market_trades,      |                                         |                         |
  ticker, user)       |                                         v                         |
                      |  RSS/CryptoPanic -> NewsIngestor -> SentimentClassifier(ONNX)     |
                      |                         |                   |                     |
                      |                         v                   v                     |
                      |                    NewsStore(SQLite)  SentimentAggregator         |
                      |                                             |                     |
                      |                     StateBuilder <----------+                     |
                      |                         | (DecisionState v1, canonical JSON)      |
                      |                         v                                         |
                      |                  DecisionClient --HTTP--> [container: decider]    |
                      |                         |                  /v1/systemone (GPU)    |
                      |                         v                                         |
                      |                  DecisionPolicy -> RiskManager -> ExecutionGateway|
                      |                                         |         (Live | Shadow)  |
                      |                                   PositionLedger   Coinbase REST  |
                      |                                    (SQLite)      o FillSimulator  |
                      +-------------------------------------------------------------------+
```

`ExecutionGateway` e' un'interfaccia con due implementazioni: `LiveExecutionGateway` (ordini reali, sez. 4) e `ShadowExecutionGateway` (simulazione tick-level sul flusso reale, sez. 5). Il container `decider` e' opzionale (profilo `gpu`); senza di esso il `DecisionEngine` usa il `BaselineDecider` in-process. Il centro di controllo (sez. 6) e' un gruppo di container opzionali (profilo compose `ui`) senza reti in comune con `chronos`: legge i file dell'agente in sola lettura e gli parla solo tramite una coda di comandi a basso privilegio.

Moduli rimossi: `vector_storage.py`, `sentiment_analysis.py`, `trading_logic.py`, `script/train_timeseries_model.py`.
Dipendenze rimosse: `faiss-cpu`, `beautifulsoup4`, `langchain`, `langchain-community`, `sentence-transformers`, `schedule`.

### 0.3 Modello di costo unico (`COST_V1`)

Unica definizione dei costi nel sistema, implementata in `modules/risk/costs.py` (`CostModel`) e importata da: label di training (sez. 3.1.3), StateBuilder (sez. 2.2.1), RiskManager (sez. 4.1), FillModel (sez. 5.1.3). Nessun altro modulo calcola costi per conto proprio, compreso `script/probe_coinbase.py`, che importa `CostModel` per la tabella di fattibilita'. L'ingresso maker non paga spread ne' slippage per costruzione (post-only al `best_bid`, mai inseguito). La selezione avversa del fill maker non e' un costo in bps: e' contenuta in `p_edge`, perche' la domanda `edge` e' condizionata all'avvenuto fill (sez. 3.1.3, sample `NO_FILL` esclusi).

Simboli (tutti in bps): `f_m`, `f_t` = fee maker/taker del tier dell'account (`FeeProvider`: `get_transaction_summary`, cache 1 h, fallback `dry_run.fill_model.fallback_fees`); `s` = spread corrente; `slip` = `risk.slippage_buffer_bps` (default 5); `x` = `dry_run.fill_model.stop_extra_slip_bps` (default 10).

| Gamba | Ordine | Costo in bps |
|-------|--------|--------------|
| Ingresso, `entry_mode: maker` (default) | limit post-only al `best_bid` | `c_in = f_m` (nessuno spread pagato) |
| Ingresso, `entry_mode: taker` | limit marketable al `best_ask` | `c_in = f_t + s/2 + slip` |
| Uscita TP | limit sell (maker) | `c_tp_out = f_m` |
| Uscita SL | stop (taker) | `c_sl_out = f_t + s/2 + slip + x` |
| Uscita del modello / orizzonte / watchdog | limit marketable o market (taker) | `c_mkt_out = f_t + s/2 + slip` |

```
cost_tp  = c_in + c_tp_out          # costo totale se si chiude al TP
cost_sl  = c_in + c_sl_out          # costo totale se si chiude allo SL
cost_mkt = c_in + c_mkt_out         # costo totale se si chiude a mercato

tp_bps   = k_tp * ATR_bps ;  sl_bps = k_sl * ATR_bps

fee floor:   tp_bps - cost_tp >= risk.min_net_edge_bps                       (default 30)
EV(p)    =   p * (tp_bps - cost_tp) - (1 - p) * (sl_bps + cost_sl)
p_breakeven = (sl_bps + cost_sl) / (tp_bps - cost_tp + sl_bps + cost_sl)
EV gate:     EV(p_edge) > risk.ev_min_bps                                     (default 0)
EV buffer:   EV(p_edge) >= p_be_buffer * (W + L)                              (W = tp_bps - cost_tp, L = sl_bps + cost_sl)
```

`p_be_buffer` e' un margine di probabilita' sul pareggio della singola decisione (0 <= `p_be_buffer` <= 0.20), parametro del `policy.json` del modello (sez. 2.2.5) e applicato solo dal RiskManager: `EV(p) >= p_be_buffer * (W + L)` equivale a `p >= p_breakeven + p_be_buffer`. Con buffer 0 vale il solo EV gate. Il RiskManager e' l'unico punto in cui il pareggio della decisione e' noto in modo esatto (costi vivi, livelli arrotondati); la policy non ha soglie su `p_edge`.

**EV a due esiti (limite inferiore).** `p = p_edge = P(UPPER)` (sez. 3.1.3). `1 - p` comprende LOWER e VERTICAL, ed entrambi sono valutati come uno SL. Un'uscita VERTICAL avviene con il prezzo strettamente dentro le barriere e costa `cost_mkt <= cost_sl`, quindi la sua perdita in bps e' sempre minore di `sl_bps + cost_sl`. Ne segue che `EV(p_edge)` e' un limite inferiore dell'EV reale e `p_breakeven` un limite superiore del breakeven reale. E' voluto: il gate e' prudenziale e usa solo la probabilita' calibrata della domanda `edge`. Il costo di questa prudenza e' misurato dallo studio di fattibilita' (`p_vertical`, `vert_mean_net_bps`, sez. 3.1.3) ed e' gia' incorporato nelle soglie, perche' `tune_policy` valuta ogni combinazione con il backtest completo, RiskManager incluso (sez. 3.1.6). Stimare `P(VERTICAL)` a runtime richiederebbe una nuova domanda (ad esempio `choice` UPPER/LOWER/VERTICAL), quindi una nuova versione di `QUESTIONS` e del modello: fuori scope v2.

Le formule in bps sono approssimazioni da usare **solo** per gate e feature. Il PnL realizzato, simulato e delle label e' sempre esatto, calcolato sui prezzi di fill e sulle fee effettive di ciascuna gamba:

```
entry_cost_quote = entry_px * size * (1 + f_in)
exit_value_quote = exit_px  * size * (1 - f_out)
net_ret          = exit_value_quote / entry_cost_quote - 1
```

dove `entry_px` ed `exit_px` includono gia' spread e slippage della gamba taker (e `x` per lo SL).

**Esempio di riferimento** (usato in tutti gli esempi del documento, fee presunte 60/120): SOL-EUR, `best_bid = 92.03`, `s = 3`, `ATR_14_1d = 460 bps` (4.2334 EUR), ingresso maker a 92.03, `k_tp = 4`, `k_sl = 2`.

L'esempio usa parametri 1d / 4-2 e fee presunte 60/120 solo come illustrazione numerica, ripresa nelle sez. 4.1.2 e 4.2: i parametri in vigore sono quelli del `label_config.json` (sez. 0.4).
TP = 108.96 (`tp_bps` = 1840), SL = 83.57 (`sl_bps` = 920); `cost_tp` = 120, `cost_sl` = 60 + 120 + 1.5 + 5 + 10 = 197; fee floor 1720 >= 30, ok; `p_breakeven` = 1117 / 2837 = **0.39**. Con `p_edge` = 0.52: EV = 0.52 x 1720 - 0.48 x 1117 = **+358 bps**.

### 0.4 Parametri di barriera v1 e prodotti (dati reali del 2026-10-17)

Misure dal probe di sola lettura `script/probe_coinbase.py` (endpoint pubblici, fee presunte maker 60 / taker 120 bps):

| Prodotto | Spread bps | Volume 24h EUR | ATR14 1h | ATR14 6h | ATR14 1d | Copertura candele 1m (a 3 anni) |
|----------|-----------|----------------|----------|----------|----------|-------------------------------|
| BTC-EUR | 2.9 | 5.7 M | 30 | 120 | 253 | ~98% |
| ETH-EUR | 2.0 | 1.8 M | 48 | 187 | 375 | ~98% |
| SOL-EUR | 3.3 | 1.0 M | 61 | 222 | 460 | ~64% (~97% a 2 anni) |
| XLM-EUR | 5.9 | 0.08 M | 50 | 221 | 585 | ~30%: **escluso** (minuti senza scambi, book sottile) |

`p_breakeven` e `lift_rw` (= `p_breakeven` meno `k_sl / (k_tp + k_sl)`, probabilita' di toccare prima il TP in un random walk senza drift). Non e' il criterio dello studio di fattibilita', che usa `lift_0` (sez. 3.1.3): serve solo come ordine di grandezza.

| ATR | k_tp/k_sl | Ingresso | BTC lift_rw | ETH lift_rw | SOL lift_rw |
|-----|-----------|----------|----------|----------|----------|
| 1h | 4/2 | taker | TP non copre i costi | +0.65 (sotto fee floor) | +0.54 |
| 1h | 4/2 | maker | TP non copre i costi | +0.47 | +0.39 |
| 6h | 4/2 | taker | +0.30 | +0.20 | +0.17 |
| 6h | 4/2 | maker | +0.21 | +0.14 | +0.12 |
| 1d | 4/2 | taker | +0.15 | +0.10 | +0.08 |
| 1d | 4/2 | maker | **+0.11** | **+0.07** | **+0.06** |

Valori calcolati con `CostModel` (sez. 0.3; fee presunte 60/120, `slip` = 5, `x` = 10) sugli spread e sugli ATR arrotondati della tabella precedente, riprodotti da `tests/test_costs.py`. Il probe li ricalcola sui valori non arrotondati e puo' differire di +-0.01. Con ATR 1h il rapporto 3/2 e' omesso perche' e' peggiore di 4/2 per tutti i prodotti.

Decisioni v1:
- **Prodotti:** BTC-EUR, ETH-EUR, SOL-EUR (quote EUR, `quote_min_size` = 1 EUR per tutti). Da confermare dall'utente.
- **Barriere:** `atr_tf`, `k_tp`, `k_sl`, `entry_mode`, `horizon_min`, `entry_timeout_min` (30), `entry_first_bar` e lo spread di label sono quelli di `data/datasets/label_config.json`, scritto da `training/feasibility.py` (sez. 3.1.3), verificato con `sha256` da `load_label_config` e copiato nel model card. Nessun altro file li definisce; label, RiskManager, backtest e dry run li leggono da li'.
- **Esito del 2026-10-18** (studio v2.7, fee reali 25/50 bps, finestra pre-holdout fino al 2026-06-18, holdout dal 2026-06-20): `6h`, 3/1.5, maker, H = 15 giorni, `entry_first_bar` 3, spread di label 3 bps; `lift_0` +0.074, lift grezzo +0.058, `N_eff` 665 (`rho` 0.54), jackknife semestrale massimo +0.089, durata mediana dei trade 48 h (p95 257 h). Pareggio entro 0.01 con 4/1.5 e 3/2, risolto con `N_eff`. I default precedenti (1d, 4/2, H = 10 giorni) sono scartati: `p_vertical` 0.60, UPPER solo nel 10% dei casi. Dettagli in `data/reports/feasibility.md` e `docs/decisions/2026-10-18-fattibilita.md`.
- **Vincoli della griglia:** `atr_tf in {6h, 1d}` (1h escluso dal fee floor); `H <= 15` giorni (= holdout / 8); H = 20 solo diagnostica.
- Conseguenza da accettare: a questo tier di fee Chronos e' un sistema **swing multi-giorno**: barriere di alcuni ATR a 6h, durata reale di alcuni giorni (misurata dallo studio, `dur_*`), tetto H di 15 giorni. La prova statistica dell'edge viene dal backtest walk-forward (gate GE, sez. 3.4); il dry run verifica che il sistema reale si comporti come il backtest (sez. 5.1.6-5.1.7).
- Il tier migliora solo con il volume a 30 giorni, che con micro-capitale non cambia: lo studio va rieseguito se le fee reali lette dal probe cambiano. Un nuovo `label_config` e' una nuova versione del modello.

### 0.5 Fondamenta (fase P0)

Componenti trasversali che le sezioni 1–5 danno per esistenti. Sono il primo lavoro da implementare.

| Tema | Decisione |
|------|-----------|
| Branch | Lavoro su branch `v2`. Tag `v1-final` sull'attuale `main`. Ogni modulo v1 viene cancellato nella stessa PR che introduce il suo sostituto v2 (es. `vector_storage.py` e `sentiment_analysis.py` con la sez. 1). `main.py` passa all'orchestratore v2 alla fine di P2. La v1 non va tenuta in esecuzione: non e' funzionante (`KeyError` sulla config, payload d'ordine non validi). |
| Python | 3.12. `Dockerfile` su `python:3.12-slim`. `mypy --strict`, `ruff`, `pytest` in CI locale (`docker compose --profile test`). |
| Storage su Windows | I DB SQLite stanno in un **named volume** Docker `chronos-state` montato su `/app/state` (WAL su bind mount NTFS non e' affidabile). I file append-only restano sul bind mount `./data`, gli artefatti su `./models`. Backup giornaliero dei DB con `sqlite3.Connection.backup()` in `./data/backup/` (rotazione 7 giorni). |
| Percorsi | `state/news.db` (news e sentiment, condiviso tra modalita'), `state/chronos_live.db`, `state/chronos_dry.db`; `data/bars/1m/product=<id>/year=<y>/month=<m>.parquet`; `data/decisions/`, `data/live_labels/`, `data/ws_recordings/`, `data/reports/`, `data/probe/`; `models/registry/`. Centro di controllo (sez. 6.3): `data/events/`, `data/equity/`, `state/control/local_inbox/`; named volume `chronos-telemetry` su `/app/telemetry` (scritto solo dall'agente) e `chronos-commands` su `/app/commands` (montato `:ro` nell'agente). |
| Chiavi Coinbase | Due API key CDP separate: **View** (dry, sviluppo, probe) e **Trade** (solo live, creata in P4). Nessuna con permesso Transfer. Selezionare l'algoritmo di firma supportato dall'SDK `coinbase-advanced-py` (ECDSA; da verificare nella console CDP al momento della creazione). Segreti solo da env. |
| Market data | Endpoint pubblici e WebSocket `market_trades`, `ticker`, `level2`, `heartbeats` **senza autenticazione** (verificato per `level2` il 2026-10-17). La key View serve solo per fee, account e permessi. |
| Probe | `script/probe_coinbase.py` (gia' nel repo), eseguito dalla radice del repo come modulo: `python -m script.probe_coinbase`. Rileva permessi della key, fee tier, metadati prodotto, spread, ATR 1h/6h/1d, profondita' dello storico 1m, `level2` e la tabella di fattibilita', calcolata con `CostModel` (sez. 0.3). Credenziali solo da env (`COINBASE_API_KEY`, `COINBASE_API_SECRET`). Da rieseguire con la key View prima di fissare la config di rischio. |

**Backfill storico** (`modules/market/backfill.py`, CLI `python -m modules.market.backfill --products BTC-EUR ETH-EUR SOL-EUR --start 2023-10-01`):
- `RESTClient().get_public_candles(granularity="ONE_MINUTE")` senza chiavi, finestre di 300 minuti (limite API 350 candele), dalla data piu' recente gia' salvata in avanti, oppure da `--start` se non c'e' nulla; ripresa idempotente dopo interruzione;
- limite superiore esclusivo `floor_minute(now - market.bars.rest_delay_s)` (default 120 s; il `Backfiller` riceve lo stesso valore come `settle_s`, senza chiave propria): un minuto viene scaricato solo quando `ts + 1 min + S <= now`, cioe' quando la candela REST e' definitiva (stessa regola del `BarService`). Storico e runtime leggono la stessa versione della candela;
- rate limit client-side configurabile (default 5 req/s) con `aiolimiter`, retry `tenacity` su 429/5xx;
- scrittura Parquet partizionata per prodotto/anno/mese, schema `ts (UTC, inizio minuto), open, high, low, close, volume`; dedup su `ts`;
- i minuti senza candela non sono errori (nessuno scambio): **non** vengono scritti; il loader (`load_bars()`) li ricostruisce come barre piatte (`OHLC = close precedente`, `volume = 0`), con la stessa regola del `BarService` live (sez. 2.3 A.2). `load_bars(end=...)` taglia `end` all'ultima barra reale salvata: oltre quel punto non si distingue un mercato fermo da dati mai scaricati, e inventare barre piatte falserebbe feature e label. Il parametro `store_complete_until_end=True` esiste solo per chi sa che lo store e' completo fino a `end` (recorder live);
- report finale per prodotto: righe, intervallo, percentuale di minuti coperti, buchi piu' lunghi di 60 min;
- ordine di grandezza: ~1.6 M righe e ~5.300 richieste per prodotto per 3 anni (~20 min per prodotto a 5 req/s).

**Invarianti delle barre 1m.** (I1) Esiste una sola definizione di barra 1m: la candela REST `ONE_MINUTE` definitiva. Storico, runtime, store, label, replay e training usano solo questa; nessuna barra costruita dai trade WebSocket entra nello stato, nello store o nelle label (le due serie differiscono nell'assegnazione dei trade ai minuti, misurato il 2026-10-17, e lo scarto cambierebbe i token dello stato). (I2) Per ogni prodotto la sequenza di barre consumate dall'`IndicatorEngine` coincide con `load_bars(store)`: lo store e' la registrazione di cio' che il runtime ha consumato e non viene mai riscritto per correggere revisioni successive dell'exchange, che sono solo misurate (sez. 5.1.6, L0). Le revisioni note si applicano solo a una copia in memoria delle righe reali, con la griglia ricostruita da `fill_flat_bars`, per misurarne l'impatto (L0, sez. 5.1.7), mai allo store.

Ordine di implementazione P0: `modules/config.py` -> `modules/market/backfill.py` + `load_bars()` -> `indicators_batch.py` -> `indicators.py` (incrementale, test di parita' sullo storico) -> `bars.py` -> `ws_feed.py` + `orderbook.py` -> `ledger/` -> `dryrun/recorder.py`.

Criterio di uscita P0: storico 1m backfillato per i tre prodotti; test di parita' batch/incrementale verde su >= 5.000 barre per prodotto; 48 h di stream WS registrato senza buchi di sequenza non gestiti; 48 h di `BarService` con R4 (zero minuti comparsi o scomparsi) e con R1 e R2 della sez. 5.1.7 (riga "Barre REST") per prodotto con il modello in uso (R1 su blocchi di 1.440 stati consecutivi, R2 per giorno UTC), misurati rileggendo da REST i minuti con eta' >= 15 min e certificati con `python -m modules.dryrun.revision_impact certify`; R3 (decisioni che cambiano) si valuta in G-DRY-OPS; `rest_revision_ratio` e' diagnostica (allarme se > 0.005). Il soak P0 del 18-20/10/2026 (`data/reports/p0_soak.json`) e' stato misurato con il criterio precedente (`rest_revision_ratio <= 0.005`, non superato da ETH-EUR con 1,09%) e resta invariato; la prima certificazione (2026-10-23 06:17Z, regola v1 "R1 per giorno UTC, anche parziale") e' `data/reports/p0_soak_certification.json`, invariato: `fail` su R1 per ETH-EUR nel giorno parziale 2026-10-20 (2 stati su 343). La regola R1/R3 a blocchi di 1.440 stati (v2) e' stata fissata il 2026-10-23, DOPO quel risultato (conflitto P1e-bis, decisione U1 dell'utente), perche' un giorno parziale e' un test con meno stati del gate di produzione; sugli stessi dati la v2 darebbe `pass` (calcolo solo informativo, `certify --info-only`), ma per decisione dell'utente (U2) il soak 18-20/10/2026 NON viene ricertificato con una regola scritta dopo averne visto il `fail`. Il criterio "barre REST" di P0 risulta quindi non certificato, in attesa di giorni UTC interi del dry run, dove la regola v1 e la v2 coincidono. I soak futuri si pianificano allineati a 00:00 UTC e di durata multipla di 24 ore, con `certify` dopo 15 minuti di eta'.

### 0.6 Ambito per book

Un **book** e' un conto, reale (`live`) o virtuale (sez. 5.1.4). Regole:

| Componente | Ambito | Note |
|------------|--------|------|
| `RiskManager`, sizing, esposizione | Per book | Usa equity e posizioni del proprio book. |
| Guardie di conto del `KillSwitch`: daily loss, loss streak, esposizione | Per book | Un book bloccato non blocca gli altri. |
| Guardie di sistema del `KillSwitch`: dati stale, salute del decider, drift, stato WS | Globali | Quando scattano bloccano i nuovi ingressi di tutti i book. |
| `PositionLedger`, ordini, fill | Per book (colonna `book` in ogni tabella) | |
| `DecisionEngine` | Per coppia (prodotto, decider) | Una decisione viene calcolata una sola volta e distribuita a tutti i book che usano quel decider. |

Persistenza: tabella `kill_switch_state(book TEXT, guard TEXT, tripped_at TEXT, until TEXT, reason TEXT, PRIMARY KEY (book, guard))` nel DB della modalita' a cui appartiene il book: `chronos_live.db` per il book `live`; `chronos_dry.db` per i book virtuali, compreso lo shadow twin quando si e' in live. Le guardie globali usano `book = '*'` in entrambi i DB.

Al massimo **un ordine di ingresso pendente** per (book, prodotto). Mentre un ingresso maker e' in attesa di fill, le decisioni successive per quel prodotto vengono registrate ma non eseguite.

---

## 1. MODULO SENTIMENT — pipeline asincrona e Choice token calibrato

### 1.1 Architettura Concettuale

**Prima (v1):** ogni 8 h -> scraping sincrono CoinDesk con BeautifulSoup -> embedding sentence-transformers -> FAISS. La similarita' vettoriale non viene mai trasformata in un segnale usabile dal trading loop.

**Dopo (v2):** flusso a eventi, ogni news diventa entro secondi un evento categorico calibrato agganciato alla serie temporale.

```
[Source pollers] --NewsItem--> asyncio.Queue(raw) --> [Deduplicator] --> [AssetTagger]
      ^                                                                       |
      | conditional GET (ETag)                                                v
      |                                                       asyncio.Queue(tagged)
                                                                              |
                                              [SentimentClassifier worker pool, to_thread]
                                                                              |
                                                                    SentimentEvent
                                                                     |            |
                                                          NewsStore(SQLite)   SentimentAggregator
                                                                              (EWM per asset + market)
                                                                                    |
                                                         snapshot(t) letto da StateBuilder ogni minuto
```

Fasi:

1. **Ingest** — un `Source` per feed. Polling indipendente per sorgente, intervallo configurabile, GET condizionale, backoff esponenziale su errori, circuit breaker per feed morti.
2. **Normalizzazione + Dedup** — chiave = `sha1(normalize(title))`; `normalize` = lowercase, rimozione punteggiatura, collapse spazi. Finestra di dedup 48 h in memoria (LRU) + vincolo UNIQUE su SQLite. Le stesse notizie ripubblicate da piu' testate vengono contate una volta ma con `source_count` incrementato (segnale di diffusione).
3. **Asset tagging** — regex word-boundary su alias configurati per prodotto: nomi case-insensitive (`bitcoin`, `ethereum`, `solana`), ticker case-sensitive in maiuscolo (`BTC`, `ETH`, `SOL`), cosi' `sol` o `solar` in minuscolo non producono falsi positivi. Le news senza asset ma con keyword macro (`sec, etf, fed, regulation, hack, exchange`) sono taggate `MARKET`. Le news non pertinenti vengono scartate prima della classificazione (risparmio CPU).
4. **Classificazione** — micro-modello encoder quantizzato ONNX int8 su CPU che restituisce una distribuzione su 3 classi, ricalibrata con temperature scaling. Output = **Choice token** `{0: BEARISH, 1: NEUTRAL, 2: BULLISH}` + probabilita' + confidenza.
5. **Aggregazione** — per ogni asset e per `MARKET`, aggregatore esponenziale con half-life configurabile. Lo `StateBuilder` legge uno snapshot immutabile al minuto `t` usando solo eventi con `ingested_at <= t` (C7).

#### 1.1.1 Sorgenti (free)

| Sorgente | Tipo | Endpoint | Note |
|----------|------|----------|------|
| CoinDesk | RSS | `https://www.coindesk.com/arc/outboundfeeds/rss/` | Primaria. |
| Cointelegraph | RSS | `https://cointelegraph.com/rss` | Primaria. |
| Decrypt | RSS | `https://decrypt.co/feed` | Secondaria. |
| CryptoSlate | RSS | `https://cryptoslate.com/feed/` | Secondaria. |
| Bitcoin Magazine | RSS | `https://bitcoinmagazine.com/.rss/full/` | Solo BTC. |
| CryptoPanic | REST JSON | `https://cryptopanic.com/api/...` | **Opzionale**, richiede `auth_token`. Verificare quota e versione dell'API del piano free al momento dell'implementazione; disabilitata se il token manca. |
| Alternative.me Fear & Greed | REST JSON | `https://api.alternative.me/fng/` | Feature macro giornaliera, polling ogni 1 h. |
| GDELT DOC 2.0 | REST JSON | `https://api.gdeltproject.org/api/v2/doc/doc` | **Solo backfill storico** per il dataset di training (sez. 3), non in live. |

Ogni URL viene validato all'avvio con un health-check: una sorgente che risponde con errore per N cicli consecutivi passa in stato `DEGRADED` e viene ritentata ogni 15 min. Il sistema funziona con qualunque sottoinsieme di sorgenti attive.

#### 1.1.2 Micro-modello di classificazione

Scelta raccomandata (CPU, p50 < 30 ms per titolo):

| Opzione | Modello | Pro | Contro |
|---------|---------|-----|--------|
| **A (default)** | `ElKulako/cryptobert` (3 classi Bearish/Neutral/Bullish, dominio crypto) | Etichette native gia' allineate al Choice a 3 classi. | Addestrato su social post, va ricalibrato sui titoli. |
| B | `ProsusAI/finbert` (positive/negative/neutral) | Robusto sul linguaggio finanziario formale. | Dominio non crypto; serve il remapping delle classi. |
| C | Backend OpenJev `verdict` (ModernBERT-base GLiClass, 151M) via `choice` zero-shot | Stesso protocollo del decision layer. | Richiede il container decider anche per il sentiment; accoppia i guasti. |

FinGPT (LoRA su LLaMA 7B+) e' scartato: troppo pesante su CPU per il beneficio marginale su titoli brevi.

**Calibrazione (non bloccante in fase locale):** temperature scaling (un singolo scalare `T` ottimizzato per NLL) su un set etichettato di titoli crypto. `T` e' salvata nel `sentiment_model_card.json` (campo `calibrated: true|false`) e applicata ai logit prima del softmax.

- **Perche' non blocca:** in fase locale il consumatore del sentiment e' il `BaselineDecider` LightGBM, che usa `ewm`, `mass`, `disp`, `shock` come feature numeriche e impara da se' la loro relazione con le label. Una distorsione monotona delle probabilita' del classificatore viene assorbita dagli alberi. Il sentiment parte quindi con `T = 1.0` e `calibrated: false`.
- **Quando diventa obbligatoria:** prima che un decider LLM consumi il sentiment (fase cloud), oppure se `ewm` viene usato direttamente in una regola di policy. Soglia: ECE (15 bin) <= 0.07 su >= 1.000 titoli (split 70/30).
- **Etichettatura a costo zero:** (1) `python -m modules.sentiment.label_sample --n 1200` estrae dal `NewsStore` un campione stratificato per sorgente e asset in `data/labels/headlines_todo.csv`; (2) l'agente di sviluppo (Claude Code, gia' disponibile: nessuna API esterna a pagamento) scrive una proposta di etichetta con motivazione di una riga in `headlines_proposed.csv`; (3) l'utente rivede con `python -m modules.sentiment.label_review`, una CLI a tasto singolo (Invio = accetta, `0`/`1`/`2` = correggi, `s` = salta), circa 3 s a titolo, quindi 1.200 titoli in circa un'ora; (4) l'output e' `headlines.csv`. Si puo' fare a meta' della prima settimana di dry run, quando il `NewsStore` ha abbastanza titoli.

#### 1.1.3 Aggregazione in feature temporali

Per ogni chiave `k` (asset o `MARKET`) e per ogni evento `i`, con `s_i = p_bull,i - p_bear,i` (in [-1, 1]):

```
w_i(t)   = conf_i * relevance_i * log(1 + source_count_i) * 2^(-(t - ingested_at_i) / half_life)
ewm(t)   = sum_i w_i(t) * s_i / (sum_i w_i(t) + eps)          # in [-1, 1]
mass(t)  = sum_i w_i(t)                                         # intensita' del flusso
disp(t)  = sqrt(weighted_var(s_i, w_i(t)))                      # disaccordo
shock_15m(t) = max_{i: t - ingested_at_i <= 15m} |s_i| * conf_i # evento ad alto impatto recente
```

`conf_i = 1 - H(p_i) / ln(3)` (stessa definizione di confidenza usata da Jev/OpenJev, cosi' i due layer parlano la stessa lingua).
`relevance_i` = 1.0 se l'asset e' nel titolo, 0.6 se e' solo nel summary, 0.4 per `MARKET`.
Due velocita' per chiave, perche' l'orizzonte di trading e' di giorni (sez. 0.4): `ewm` (veloce, half-life 90 min asset / 240 min `MARKET`) ed `ewm_slow` (half-life 24 h asset / 48 h `MARKET`), con la stessa formula. Anche `mass` e' calcolata per entrambe le velocita' (`mass`, `mass_slow`).

### 1.2 Struttura dei Dati / Schema JSON

**NewsItem** (output dei Source, prima della classificazione):

```json
{
  "news_id": "c0f1a9e2b7d4...",
  "source": "coindesk_rss",
  "url": "https://www.coindesk.com/markets/2026/10/17/...",
  "title": "Stellar Development Foundation announces ...",
  "summary": "First 512 chars of description, HTML stripped",
  "published_at": "2026-10-17T13:58:41Z",
  "ingested_at": "2026-10-17T14:00:12.384Z",
  "source_count": 1,
  "assets": ["SOL"],
  "is_market_wide": false
}
```

**SentimentEvent** (output del classificatore, persistito su SQLite tabella `sentiment_events`):

```json
{
  "news_id": "c0f1a9e2b7d4...",
  "asset_key": "SOL",
  "ingested_at": "2026-10-17T14:00:12.384Z",
  "classified_at": "2026-10-17T14:00:12.401Z",
  "model": "cryptobert-onnx-int8@2026-10-01",
  "choice": 2,
  "label": "BULLISH",
  "probabilities": {"BEARISH": 0.06, "NEUTRAL": 0.21, "BULLISH": 0.73},
  "confidence": 0.41,
  "relevance": 1.0,
  "latency_ms": 17.2
}
```

**SentimentSnapshot** (letto dallo StateBuilder, immutabile):

```json
{
  "as_of": "2026-10-17T14:03:00Z",
  "asset": {"key": "SOL", "ewm": 0.38, "ewm_slow": 0.21, "mass": 2.71, "mass_slow": 9.40, "disp": 0.22, "shock_15m": 0.30,
            "n_1h": 3, "n_24h": 14, "last_choice": 2, "last_conf": 0.41, "last_age_min": 3},
  "market": {"ewm": -0.05, "ewm_slow": 0.02, "mass": 6.10, "mass_slow": 31.2, "disp": 0.41, "shock_15m": 0.12, "n_1h": 9},
  "fear_greed": {"value": 54, "age_h": 6}
}
```

**Config YAML** (sezione nuova):

```yaml
sentiment:
  classifier:
    model_dir: "models/sentiment/cryptobert-onnx-int8"
    model_card: "models/sentiment/sentiment_model_card.json"   # contiene temperature T e label_map
    max_workers: 2
    max_seq_len: 128
  aggregation:
    half_life_min: {asset: 90, market: 240}
    half_life_slow_min: {asset: 1440, market: 2880}
    retention_days: 7
    shock_window_min: 15
  dedup_window_hours: 48
  assets:
    BTC: {names: ["bitcoin"], tickers: ["BTC", "XBT"]}
    ETH: {names: ["ethereum", "ether"], tickers: ["ETH"]}
    SOL: {names: ["solana"], tickers: ["SOL"]}
  market_keywords: ["sec", "etf", "fed", "regulation", "hack", "exchange", "stablecoin"]
  sources:
    - {name: coindesk_rss, kind: rss, url: "https://www.coindesk.com/arc/outboundfeeds/rss/", interval_s: 45}
    - {name: cointelegraph_rss, kind: rss, url: "https://cointelegraph.com/rss", interval_s: 45}
    - {name: decrypt_rss, kind: rss, url: "https://decrypt.co/feed", interval_s: 60}
    - {name: cryptoslate_rss, kind: rss, url: "https://cryptoslate.com/feed/", interval_s: 60}
    - {name: cryptopanic, kind: cryptopanic, interval_s: 120, token_env: "CRYPTOPANIC_TOKEN", enabled_if_token: true}
    - {name: fear_greed, kind: fear_greed, url: "https://api.alternative.me/fng/", interval_s: 3600}
```

**SQLite DDL** (`state/news.db`, sez. 0.5):

```sql
CREATE TABLE IF NOT EXISTS news_items (
  news_id TEXT PRIMARY KEY, source TEXT NOT NULL, url TEXT, title TEXT NOT NULL,
  summary TEXT, published_at TEXT, ingested_at TEXT NOT NULL, source_count INTEGER DEFAULT 1,
  assets TEXT NOT NULL, is_market_wide INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sentiment_events (
  news_id TEXT NOT NULL, asset_key TEXT NOT NULL, ingested_at TEXT NOT NULL,
  model TEXT NOT NULL, choice INTEGER NOT NULL, p_bear REAL, p_neu REAL, p_bull REAL,
  confidence REAL, relevance REAL, PRIMARY KEY (news_id, asset_key)
);
CREATE INDEX IF NOT EXISTS ix_sent_asset_time ON sentiment_events(asset_key, ingested_at);
```

### 1.3 Linee Guida per l'implementazione in Sonnet 5.5

> PROMPT — Modulo Sentiment
>
> Implementa il package `modules/sentiment/` per Chronos v2 in Python 3.12. Vincoli generali: asyncio con `asyncio.TaskGroup`, nessuna chiamata bloccante nel loop (I/O sincrono e inferenza ONNX vanno in `asyncio.to_thread` o in un `ThreadPoolExecutor` dedicato), tipi completi (mypy strict), modelli dati in pydantic v2 (`model_config = ConfigDict(frozen=True)`), log con `structlog` in JSON.
>
> File e responsabilita':
>
> 1. `modules/sentiment/schemas.py` — `NewsItem`, `SentimentEvent`, `SentimentSnapshot`, `AssetSentiment`, `MarketSentiment` esattamente come negli schemi JSON della sez. 1.2. Timestamp `datetime` timezone-aware UTC; serializzazione ISO-8601 con `Z`.
> 2. `modules/sentiment/sources.py` — classe astratta `Source(ABC)` con `name: str`, `interval_s: float`, `async def fetch(self, client: httpx.AsyncClient) -> list[NewsItem]`. Implementazioni:
>    - `RssSource`: GET con header `If-None-Match`/`If-Modified-Since` dagli ultimi valori ricevuti; su `304` ritorna `[]`. Parsing con `feedparser.parse(response.content)` eseguito in `asyncio.to_thread`. Pulizia HTML del summary con `selectolax` (non BeautifulSoup). Tronca il summary a 512 caratteri.
>    - `CryptoPanicSource`: legge il token da env (`token_env`); se assente, `enabled = False` e il Runner non la schedula. Mappa il campo `currencies` della risposta su `assets`.
>    - `FearGreedSource`: non produce `NewsItem`; espone `latest() -> FearGreed | None`.
>    - `SourceRunner`: un task per sorgente, loop `while True: fetch -> put su queue -> await asyncio.sleep(interval_s + jitter)`, jitter uniforme +-10%. Retry con `tenacity` (`wait_exponential(min=2, max=120)`, `stop_after_attempt(5)`), dopo di che stato `DEGRADED` e re-try ogni 900 s. Timeout httpx 10 s. User-Agent esplicito `Chronos/2.0 (+contact)`.
> 3. `modules/sentiment/dedup.py` — `Deduplicator` con `normalize_title()` puro e testato, `cachetools.TTLCache(maxsize=50_000, ttl=48h)`; su duplicato incrementa `source_count` nel DB (`UPDATE news_items SET source_count = source_count + 1`) e non riemette l'evento.
> 4. `modules/sentiment/tagger.py` — `AssetTagger(aliases: dict[str, list[str]], market_keywords: list[str])`. Compila una regex per asset `\b(alias1|alias2)\b` case-insensitive. `tag(item) -> NewsItem | None` (None = scartata). `relevance` = 1.0 match nel titolo, 0.6 solo nel summary, 0.4 per MARKET.
> 5. `modules/sentiment/classifier.py` — `OnnxSentimentClassifier`:
>    - carica modello con `optimum.onnxruntime.ORTModelForSequenceClassification` + `AutoTokenizer`; `onnxruntime` con `intra_op_num_threads` configurabile;
>    - `label_map` dal model card mappa gli indici nativi del modello su `{0: BEARISH, 1: NEUTRAL, 2: BULLISH}` (obbligatorio per FinBERT, che ha ordine diverso);
>    - `classify_batch(texts: list[str]) -> list[ClassOutput]` sincrona: logit -> `/ T` -> softmax -> riordina con label_map -> `confidence = 1 - entropy(p)/ln(3)`;
>    - input del modello: `title + " . " + summary[:200]`, `max_seq_len=128`, truncation;
>    - micro-batching: il worker async raccoglie fino a 16 item o 200 ms, poi un'unica chiamata `to_thread`.
> 6. `modules/sentiment/calibration.py` — script CLI `python -m modules.sentiment.calibration --labels data/labels/headlines.csv`: ottimizza `T` con `scipy.optimize.minimize_scalar` su NLL (bounds 0.05–10), calcola ECE a 15 bin e Brier prima e dopo, scrive il model card con `calibrated: true`. Con `--strict` fallisce con exit code 1 se ECE dopo > 0.07. Implementa anche `label_sample.py` e `label_review.py` (CLI a tasto singolo con `readchar`, salvataggio incrementale a ogni risposta, ripresa dal punto in cui si era rimasti) come descritto nella sez. 1.1.2.
> 7. `modules/sentiment/aggregator.py` — `SentimentAggregator`: per chiave mantiene una `collections.deque` di eventi degli ultimi `retention_days` giorni; `snapshot(as_of: datetime) -> SentimentSnapshot` calcola `ewm, ewm_slow, mass, mass_slow, disp, shock_15m, n_1h, n_24h` con le formule della sez. 1.1.3 usando **solo eventi con `ingested_at <= as_of`**. Funzione pura rispetto allo stato: stesso stato e stesso `as_of` danno lo stesso output. All'avvio ricarica gli ultimi `retention_days` giorni da SQLite.
> 8. `modules/sentiment/store.py` — `NewsStore` su `aiosqlite`, DDL della sez. 1.2, `INSERT OR IGNORE`, WAL mode (`PRAGMA journal_mode=WAL`).
> 9. `modules/sentiment/service.py` — `SentimentService` che possiede le queue e i task: `async def run(self) -> None` e `def snapshot(self, product_id: str, as_of: datetime) -> SentimentSnapshot` (mappa `SOL-EUR` -> chiave `SOL`).
>
> Test (pytest + pytest-asyncio + respx):
> - `RssSource` con fixture XML locali, incluso il caso 304;
> - dedup con titoli che differiscono per punteggiatura/maiuscole;
> - tagger: `"Solana"` e `"SOL"` matchano SOL; `"solar"`, `"sol"` minuscolo e `"console"` no;
> - aggregator: test di non-lookahead (un evento con `ingested_at > as_of` non deve cambiare lo snapshot) e test del decadimento (dopo un half-life il peso si dimezza, tolleranza 1e-9);
> - classifier: mock di ORT; verifica del remapping delle label e che le probabilita' sommino a 1.

---

## 2. UNIFICAZIONE AI/ML LAYER — la filosofia "Jev"

### 2.1 Architettura Concettuale

**Prima (v1):** `TradingLogic` normalizza con MinMax, passa `close` a un `InformerForPrediction`, ottiene un valore previsto e lo confronta con soglie fisse. La catena "previsione -> soglia -> azione" introduce due errori (errore di previsione + soglia arbitraria) e nessuna probabilita' calibrata.

**Dopo (v2):** il modello non prevede il prezzo. Riceve uno **stato unificato** (tecnica + sentiment + posizione + costi) e risponde in un'unica chiamata a **domande tipizzate** del protocollo System One:

| Domanda (`key`) | Tipo | Semantica | Uso a valle |
|-----------------|------|-----------|-------------|
| `action` | `choice` {BUY, SELL, HOLD} | Azione ottima allo stato attuale, dato il vincolo spot long-only. | Azione candidata. |
| `edge` | `noul` | P(che un long aperto ora con le barriere ATR standard tocchi il TP prima dello SL entro l'orizzonte H). | Gate di valore atteso (con `p_be_buffer`) + sizing (sez. 4). |
| `vol_regime` | `score` [very_low, low, normal, high, extreme] | Regime di volatilita' realizzata **futura** sull'orizzonte H. | Solo log e diagnostica (`model_regime` nel `DecisionRecord`): non decide ne' il veto di ingresso ne' il sizing. Il regime che decide viene dal token di stato `atr_1d_pctile_365d` (sez. 2.1.2). |

Flusso per ogni prodotto, ogni chiusura di barra da 1 minuto:

```
bar_closed(product, t)                             # pubblicata dal BarService a t + S (sez. 2.3 A.2)
  -> IndicatorEngine.snapshot(product, t)          # O(1), incrementale
  -> SentimentService.snapshot(product, t)
  -> PositionLedger.view(product)
  -> StateBuilder.build(...) -> DecisionState (pydantic)
  -> serialize_state(state) -> str canonica         # stessa funzione usata in training
  -> DecisionClient.decide(state_str, QUESTIONS)    # 1 richiesta HTTP, budget 250 ms
  -> DecisionPolicy.resolve(answers, side, vol_pctile=state.volatility.atr_1d_pctile_365d)   # mascheramento azioni invalide + soglie + regime dal token
  -> Decision(BUY|SELL|HOLD, p_edge, regime, ...)
  -> RiskManager / ExecutionGateway (sez. 4)
  -> DecisionRecord -> data/decisions/YYYY-MM-DD.jsonl (audit, replay, dataset futuro)
```

**Istante logico e istante di calcolo.** `t` = chiusura del minuto (`ts_bar_close`); la decisione si calcola a `t + S` (`market.bars.rest_delay_s`, default 120 s), quando la candela REST del minuto e' definitiva. Tutto lo stato usa l'istante logico `t`: barre fino al minuto che chiude in `t`, `clock` da `t`, sentiment con `as_of = t` (`ingested_at <= t`). Fa eccezione solo `position` (lato, ingresso, SL/TP e ATR d'ingresso dal ledger al momento del calcolo), con `unreal_bps` e distanze calcolate su `close[t]`. `costs` e' funzione del `label_config` e dello snapshot: fee e spread vivi li usa solo il RiskManager (sez. 4.1). Il ritardo `S` e' irrilevante a questo orizzonte: 120 s sono lo 0.014% di `H`, la finestra di fill maker reale `[t+S, t+S+T_in]` copre il 93% di quella della label, e lo scarto fra il limite reale (best_bid a `t+S`) e quello della label e' una deriva di prezzo di 2 minuti a media nulla, piccola rispetto a barriere di piu' ATR giornalieri. Il prezzo dell'ordine viene comunque dal `LocalOrderBook` all'invio. Una barra pubblicata con ritardo oltre `market.bars.decision_max_lag_s` aggiorna indicatori e store ma non produce decisione ne' `DecisionRecord` (contatore `decisions.skipped_late`).

Le tre domande condividono lo stesso stato: sul server il prefisso di stato viene calcolato una volta sola (prefix/KV caching) e le domande vengono lette in un'unica forward pass batch. Questo e' cio' che rende la decisione "System 1": nessuna generazione autoregressiva di testo, solo lettura dei logit sui token di etichetta.

#### 2.1.1 Backend intercambiabili dietro lo stesso contratto

| Fase | Backend | Scopo |
|------|---------|-------|
| L — Locale, costo zero (attuale) | `BaselineDecider` in-process (LightGBM sugli stessi campi numerici, addestrato su CPU con le label della sez. 3.1.3, stessa interfaccia `decide()`) | Dry run (sez. 5), primo live a micro-capitale. Produce i `DecisionRecord` e le label live che serviranno al fine-tuning LLM. |
| L-opt — Zero-shot locale | OpenJev (`docker compose up`, default `127.0.0.1:8080`) | Solo se e' disponibile hardware compatibile (GPU NVIDIA >= 24 GB o Apple Silicon >= 16 GB liberi). Gira come seconda shadow book in dry, per confronto. |
| C — Fine-tuned (fase cloud, fuori scope infrastrutturale) | `chronos-decider` (sez. 3.3): server FastAPI che espone lo stesso `POST /v1/systemone`, base Qwen3.5-9B (o 4B) + LoRA Chronos. | Sostituisce il baseline solo dopo aver superato i gate della sez. 3.4. |
| Fallback (sempre) | `BaselineDecider` | Usato se il decider LLM e' irraggiungibile o fuori budget di latenza. |

La scelta del backend e' solo configurazione (`decision.backend`). Il client e' identico.

#### 2.1.2 Policy di risoluzione

1. **Mascheramento** per stato posizione (C1): se `FLAT`, `SELL` e' invalida; se `LONG`, `BUY` e' invalida (niente pyramiding in v2). Le probabilita' delle azioni valide vengono rinormalizzate.
2. **Regime (dal token di stato, non dal modello):** `vol_pctile = state.volatility.atr_1d_pctile_365d` (intero 0..100, sez. 2.2.1.b), letto SEMPRE nella forma arrotondata del token (`flatten_state` a runtime, `flat_matrix` nel backtest e nel tuning), mai dalla colonna grezza dell'indicatore. `regime = bisect_right((5, 35, 65, 95), vol_pctile)`, indice 0..4 (`REGIME_EDGES_PCT`, `REGIME_RULE = "ATR1D_PCTILE365:5/35/65/95"`). Le soglie sono fisse, decise prima del tuning e mai ottimizzate. Se il token manca o non e' finito (NaN con meno di 90 barre 1d di storico) il regime e' `None`. La funzione e' la stessa in training, backtest e runtime (`regime_from_pctile`, gemella vettoriale `regimes_from_pctile`, -1 dove NaN). La risposta `vol_regime` del modello non entra: viene solo loggata (`model_regime = argmax` delle sue probabilita') per diagnostica.

   | `vol_pctile` | Regime | Ingresso |
   |--------------|--------|----------|
   | < 5 | 0 very_low | Vietato: barriere troppo strette rispetto alle fee |
   | 5 <= p < 35 | 1 low | Permesso |
   | 35 <= p < 65 | 2 normal | Permesso |
   | 65 <= p < 95 | 3 high | Permesso |
   | >= 95 | 4 extreme | Vietato: rischio di gap oltre lo stop |
   | assente / NaN | `None` | Nessun ingresso (`regime_unavailable`) |

   Ai confini (la funzione accetta anche valori non interi): 4.99 -> 0, 5 -> 1, 34 -> 1, 35 -> 2, 64 -> 2, 65 -> 3, 94 -> 3, 95 -> 4. Gli ingressi sono ammessi nei regimi `ENTRY_REGIMES = (1, 2, 3)`, costante del codice e non parametro del `policy.json`.
3. **Soglie** (`policy.json` del modello, sez. 2.2.5), nell'ordine, con posizione FLAT:
   - `BUY` se `P(BUY)` mascherata `>= tau_buy` **e** `action.confidence >= c_min` **e** il regime e' in `ENTRY_REGIMES`; se le prime due condizioni valgono ma il regime e' `None` -> `HOLD` con `reason="regime_unavailable"`, se non e' in `ENTRY_REGIMES` -> `HOLD` con `reason="regime_veto"`; se `P(BUY)` mascherata `< tau_buy` oppure `confidence < c_min` -> `HOLD` con `reason="below_threshold"`;
   - `SELL` (solo se LONG) se `P(SELL)` mascherata `>= tau_sell`; con posizione LONG il regime e' ignorato;
   - altrimenti `HOLD`.

   La policy **non ha soglie su `p_edge`**: nel `policy.json` non stanno ne' `p_edge_min` ne' i regimi ammessi. `resolve(answers, side, *, vol_pctile)` riceve `vol_pctile` come keyword obbligatoria (anche se ignorata con posizione LONG) e non legge mai `answers.vol_regime`; `buy_mask(p_action, vol_pctile)` non riceve nemmeno la risposta di regime: e' la garanzia strutturale che il modello non decide il veto.

   Il **veto di regime esiste solo qui**. Il `RiskManager` riceve soltanto decisioni con regime in `ENTRY_REGIMES`; se riceve un BUY con regime `None` o assente da `risk.regime_multipliers` solleva `PolicyContractError` (errore di programmazione, non uno skip). Inoltre il `RiskManager` rifiuta di avviarsi (`ValueError`) se `risk.regime_multipliers` non contiene tutti i regimi di `ENTRY_REGIMES`.

   **Confidenza comparabile tra backend:** ogni backend restituisce probabilita' gia' calibrate (temperature o Platt del proprio model card); la confidenza viene sempre ricalcolata dal client come `1 - H(p)/ln K` su quelle probabilita', mai presa dal backend. Le soglie non sono condivise: ogni versione di modello ha il proprio `policy.json`, prodotto da `training/tune_policy.py` sul proprio validation set. Baseline e LLM hanno quindi soglie diverse.

   **Pavimento economico:** il pavimento su `p_edge` non e' una soglia della policy: e' il pareggio vivo della singola decisione piu' il buffer, `p_floor = p_breakeven(t) + p_be_buffer` (sez. 0.3), calcolato dal `RiskManager` con i costi vivi e i livelli arrotondati (sez. 4.1). Il `p_be_buffer` sta nel `policy.json` (0 <= `p_be_buffer` <= 0.20) ma lo applica solo il `RiskManager`. Le sole diagnostiche (regione di decisione di G1, `val_diag` del tuning) usano il token di stato `costs.p_breakeven`, che nel backtest coincide con il `p_breakeven` del `RiskManager` a meno dell'arrotondamento a 2 decimali.
4. **Coerenza**: se `action=BUY` ma `edge.noul < p_breakeven(t) + p_be_buffer`, il `RiskManager` scarta il BUY con lo skip `ev_negative` (EV non positivo) o `ev_buffer` (EV positivo ma sotto `p_be_buffer * (W + L)`, sez. 0.3, 4.1). Il motivo di policy `edge_veto` non esiste piu'. Le incoerenze tra domande vengono monitorate (sono un segnale di drift).
5. **Degradazione**: timeout, HTTP 429/5xx, o risposta non valida -> `HOLD` con `reason="decider_unavailable"`, metrica incrementata, mai retry sincrono dentro il budget della barra. Le posizioni aperte restano protette dai bracket lato exchange.

### 2.2 Struttura dei Dati / Schema JSON

#### 2.2.1 DecisionState v1 (payload unificato)

Regole di encoding (critiche per un modello LLM: numeri brevi e stabili = token stabili):
- rendimenti e distanze in **basis points interi** (`ret_15m_bps: -42`);
- RSI e percentili interi 0–100;
- rapporti con 2 decimali;
- sentiment `ewm` con 2 decimali;
- chiavi in ordine fisso definito dallo schema, JSON compatto senza spazi (`separators=(",", ":")`);
- nessun timestamp assoluto nello stato (evita memorizzazione delle date); solo feature cicliche (`hour_utc`, `dow`);
- campi mancanti = `null`, mai omessi.

Forma "pretty" (documentazione):

```json
{
  "v": "chronos.state.v1",
  "product": "SOL-EUR",
  "clock": {"hour_utc": 14, "dow": 5},
  "position": {"side": "FLAT", "hours_held": 0, "unreal_bps": null, "dist_sl_atr": null, "dist_tp_atr": null},
  "price": {
    "ret_bps": {"15m": -12, "1h": -38, "6h": 95, "1d": 210, "3d": -340, "7d": 520},
    "seq_1h_bps": [12, -8, 30, -15, -40, 22, -9, -42, 18, 5, -11, -38],
    "seq_1d_bps": [140, -260, 310, -90, -410, 75, 210],
    "dist_vwap_1d_bps": -55,
    "bb_pos_1d": 0.41,
    "hh_ll_pos_30d": 0.61
  },
  "momentum": {"rsi_14_1h": 44, "rsi_14_6h": 51, "rsi_14_1d": 47, "macd_hist_1d_bps": -18},
  "volatility": {
    "atr_14_1h_bps": 61,
    "atr_14_6h_bps": 222,
    "atr_14_1d_bps": 460,
    "rv_1d_bps": 410,
    "rv_ratio_1d_30d": 0.89,
    "atr_1d_pctile_365d": 38
  },
  "liquidity": {"vol_z_1d": 0.6},
  "sentiment": {
    "asset": {"ewm": 0.38, "ewm_slow": 0.21, "mass": 2.71, "mass_slow": 9.40, "disp": 0.22, "shock_15m": 0.30,
              "n_1h": 3, "n_24h": 14, "last_choice": 2, "last_age_min": 3},
    "market": {"ewm": -0.05, "ewm_slow": 0.02, "mass": 6.10, "mass_slow": 31.2, "disp": 0.41, "shock_15m": 0.12, "n_1h": 9},
    "fear_greed": 54
  },
  "btc_context": {"ret_1d_bps": -35, "rv_ratio_1d_30d": 1.12},
  "costs": {"entry_mode": "maker", "cost_tp_bps": 120, "cost_sl_bps": 197, "p_breakeven": 0.39}
}
```

Forma canonica inviata al modello (una riga, ~200–300 token Qwen):

```
{"v":"chronos.state.v1","product":"SOL-EUR","clock":{"hour_utc":14,"dow":5},"position":{"side":"FLAT",...},...}
```

Note sui campi:
- `seq_1h_bps` (ultime 12 ore) e `seq_1d_bps` (ultimi 7 giorni): sostituiscono la finestra grezza che il Transformer v1 riceveva; danno contesto temporale con 19 numeri alla scala dell'orizzonte (sez. 0.4).
- `btc_context`: per prodotti non-BTC, BTC e' il driver di regime dominante. Per `BTC-*` il blocco e' `null`.
- `costs`: calcolati dal `CostModel` con fee, `slip`, `x` e spread del `label_config` (`spread_for`) e con `atr_14_<atr_tf>_bps` dello snapshot; identici in training e serving. Il modello deve "vedere" i costi perche' le label sono nette di fee (sez. 3).
- `sentiment: null` quando il servizio sentiment non ha dati (avvio a freddo, outage): il modello e' addestrato anche su questo caso (feature dropout, sez. 3.1.5).

#### 2.2.1.b Definizione esatta delle feature di mercato

Unica definizione delle feature prodotte dall'`IndicatorEngine`. Costanti e nomi delle colonne in
`modules/market/indicator_defs.py`; due implementazioni: `indicators_batch.compute_indicators` (training) e
`indicators.IndicatorEngine` (runtime), verificate dal test di parita' della sez. 2.3 A.3.

Convenzioni:
- Riga `i` = barra 1m con inizio `ts_i` sulla griglia contigua di `load_bars` (barre piatte incluse). Istante di decisione
  `t_i = ts_i + 1 min`. La riga `i` usa solo barre 1m con indice `<= i`. `c_i, h_i, l_i, v_i` = OHLCV della barra 1m.
- Sorgente: le barre 1m sono sempre candele REST definitive (invariante I1, sez. 0.5), in batch e a runtime. La riga `i` entra nell'`IndicatorEngine` a `t_i + S`, una sola volta, in ordine e senza buchi; una riga gia' consumata non viene mai sostituita.
- Barre HTF (`tf` in 1h, 6h, 1d; bucket UTC di `modules/market/timeframes.py`): una barra con inizio `B` e' visibile dalla
  riga `i` se e solo se `B + tf <= t_i`. `K = K(i, tf)` = ultima barra visibile; `C_k, H_k, L_k` = OHLC della barra HTF `k`.
  La barra in formazione non entra in nessuna serie.
- `tp_j = (h_j + l_j + c_j) / 3`; `pv_j = tp_j * v_j` (quote, EUR).
- Ancora: tutte le serie partono dalla prima riga di `load_bars(root, product)` senza `start`, in batch e a runtime.
- Valore non disponibile = `NaN` nell'indicatore, `null` nello stato. Storico sotto il minimo o denominatore nullo -> `NaN`.
- Gli indicatori restituiscono float gia' in bps (non frazioni); arrotondamento e clamp solo nello `StateBuilder`.

Ricorrenze (identiche su ogni timeframe):
- `Wilder(n)`: primo valore = media dei primi `n` input; poi `x_k = (x_{k-1} * (n-1) + u_k) / n`.
- `EMA(N)`: valore all'indice `N-1` = media dei primi `N` input; poi `e_k = alpha * x_k + (1 - alpha) * e_{k-1}`, `alpha = 2/(N+1)`.
- `TR_0 = H_0 - L_0`; `TR_k = max(H_k - L_k, |H_k - C_{k-1}|, |L_k - C_{k-1}|)`; `ATR = Wilder(14)` su `TR_0..` (primo valore a `k = 13`).
- `d_k = C_k - C_{k-1}` (`k >= 1`), `G = max(d, 0)`, `P = max(-d, 0)`; `avgG = Wilder(14)` su `G_1..`, `avgP = Wilder(14)` su
  `P_1..` (primo valore a `k = 14`); `RSI = 100 - 100 / (1 + avgG/avgP)`; se `avgP == 0`: `RSI = 100` se `avgG > 0`, altrimenti `50`.
- `MACD_k = EMA12_k - EMA26_k` (da `k = 25`); `SIG = EMA(9)` sulla serie `MACD_25..` (primo valore a `k = 33`); `HIST_k = MACD_k - SIG_k`.
- `r_k = ln(C_k / C_{k-1})` su barre 1h; `RVd(m, k) = 1e4 * sqrt((24/m) * fsum(r_{k-m+1..k}^2))`, definita per `k >= m`.
  `RVd` e' l'unica funzione di volatilita' realizzata del sistema (stato e label `vol_regime`, sez. 3.1.3).

| Campo stato | Formula (riga `i`) | Minimo storico | Unita' |
|-------------|--------------------|----------------|--------|
| `clock.hour_utc` | `t_i.hour` | - | ora 0..23 |
| `clock.dow` | `t_i.weekday()` (lunedi' = 0) | - | 0..6 |
| `price.ret_bps[w]` | `1e4 * (c_i / c_{i-w} - 1)`, `w` = 15, 60, 360, 1440, 4320, 10080 min per 15m, 1h, 6h, 1d, 3d, 7d | `w + 1` barre 1m | bps |
| `price.seq_1h_bps` | `[1e4 * (C_k / C_{k-1} - 1) for k in K-11..K]`, 1h, dal piu' vecchio | 13 barre 1h chiuse | bps x 12 |
| `price.seq_1d_bps` | come sopra su 1d, `k in K-6..K` | 8 barre 1d chiuse | bps x 7 |
| `price.dist_vwap_1d_bps` | `VWAP = fsum(pv_j) / fsum(v_j)`, `j = i-1439..i`; `1e4 * (c_i / VWAP - 1)`; NaN se nessun `v_j > 0` (contatore intero) | 1440 barre 1m | bps |
| `price.bb_pos_1d` | su `C_{K-19..K}` 1d: `m = fsum/20`, `sd = sqrt(fsum((C - m)^2)/20)`; `(c_i - (m - 2 sd)) / (4 sd)`; NaN se `sd == 0`; non clampato | 20 barre 1d | rapporto |
| `price.hh_ll_pos_30d` | `HH = max h_j`, `LL = min l_j`, `j = i-43199..i`; `(c_i - LL) / (HH - LL)`; NaN se `HH == LL` | 43200 barre 1m | rapporto in [0, 1] |
| `momentum.rsi_14_<tf>` (1h, 6h, 1d) | `RSI_K` | 15 barre `tf` | 0..100 |
| `momentum.macd_hist_1d_bps` | `1e4 * HIST_K / C_K` su 1d | 34 barre 1d | bps |
| `volatility.atr_14_<tf>_bps` (1h, 6h, 1d) | `1e4 * ATR_K / C_K` | 14 barre `tf` | bps |
| `volatility.rv_1d_bps` | `RVd(24, K)` su 1h | 25 barre 1h | bps/giorno |
| `volatility.rv_ratio_1d_30d` | `RVd(24, K) / RVd(720, K)`; NaN se il denominatore e' 0 | 721 barre 1h | rapporto |
| `volatility.atr_1d_pctile_365d` | `a_k = atr_14_1d_bps` della barra 1d `k`; `Prev = {a_{K-365..K-1}}` disponibili, `N = len(Prev)`; `100 * #{p in Prev : p <= a_K} / N`; NaN se `N < 90` | 104 barre 1d | 0..100 |
| `liquidity.vol_z_1d` | `QV_D = fsum(pv_j)` sui minuti del giorno 1d `D`; `y_D = ln(1 + QV_D)`; `(y_K - m) / s`, `m`, `s` (ddof=1, due passate) su `y_{K-30..K-1}`; NaN se `s == 0` | 31 barre 1d | z-score |
| `btc_context.ret_1d_bps`, `btc_context.rv_ratio_1d_30d` | stesse formule sulla serie BTC-EUR alla stessa riga `ts_i`; blocco `null` per `BTC-*` o se la riga BTC manca | come sopra | come sopra |

Colonne esportate non presenti nello stato (consumatori: label, RiskManager, watchdog, CUSUM, feasibility):
`atr_14_1h`, `atr_14_6h`, `atr_14_1d` = `ATR_K` in unita' di prezzo (quote); `rv_30d_bps = RVd(720, K)`.
Label e RiskManager usano sempre l'ATR in prezzo della riga della decisione: `upper = entry + k_tp * atr_14_<atr_tf>`,
mai `bps * prezzo`.

Visibilita' (C7): ogni valore della riga `i` e' funzione delle sole barre 1m `<= i`. Test di troncamento obbligatorio
(sez. 2.3 A.3): `compute_indicators(bars[:i+1])` all'ultima riga coincide con `compute_indicators(bars)` alla riga `i`.

#### 2.2.2 Richiesta al decider (`POST /v1/systemone`)

```json
{
  "model": "chronos-decider-qwen35-9b-lora@v1.2.0",
  "state": "{\"v\":\"chronos.state.v1\",\"product\":\"SOL-EUR\",...}",
  "questions": {
    "action": {
      "type": "choice",
      "instructions": "Spot long-only crypto account. Given the state, which action maximizes expected net return after fees over the next 10 days?",
      "criteria": {
        "BUY": "open a long position now (only valid when position.side is FLAT)",
        "SELL": "close the existing long now (only valid when position.side is LONG)",
        "HOLD": "do nothing this minute"
      }
    },
    "edge": {
      "type": "noul",
      "instructions": "If a long were opened now with take-profit at +4.0 ATR(1d) and stop-loss at -2.0 ATR(1d), would the take-profit be hit first within 10 days?"
    },
    "vol_regime": {
      "type": "score",
      "instructions": "What will realized volatility be over the next 10 days relative to the last 365 days?",
      "criteria": ["very_low", "low", "normal", "high", "extreme"]
    }
  }
}
```

I valori `10 days`, `+4.0 ATR(1d)`, `-2.0 ATR(1d)` vengono dal `label_config` del modello (sez. 0.4 e 3.1.3). Il testo di `instructions` e `criteria` e' **congelato e versionato** (`modules/decision/questions.py`, costante `QUESTIONS_V1` + hash SHA-256). Deve essere identico byte per byte a quello usato nel dataset di training. Modificare una domanda = nuova versione del modello.

#### 2.2.3 Risposta attesa

```json
{
  "model": "chronos-decider-qwen35-9b-lora@v1.2.0",
  "answers": {
    "action": {"choice": "BUY", "probabilities": {"BUY": 0.71, "SELL": 0.02, "HOLD": 0.27}, "confidence": 0.37},
    "edge": {"noul": 0.52},
    "vol_regime": {"score": 2.31, "legend": {"0": "very_low", "1": "low", "2": "normal", "3": "high", "4": "extreme"},
                   "probabilities": {"very_low": 0.03, "low": 0.12, "normal": 0.42, "high": 0.33, "extreme": 0.10},
                   "confidence": 0.22}
  },
  "usage": {"input_tokens": 412, "output_tokens": 0}
}
```

Header `Server-Timing: model;dur=41.2, server;dur=2.8, total;dur=44.0` (OpenJev lo emette; `chronos-decider` deve emetterlo con lo stesso formato) -> loggato come `decider_model_ms`.

#### 2.2.4 DecisionRecord (audit, una riga JSONL)

```json
{
  "decision_id": "01JAB3...ULID",
  "ts_bar_close": "2026-10-17T14:03:00Z",
  "decided_at": "2026-10-17T14:05:00.412Z", "bar_source": "rest", "bar_lag_s": 120.4,
  "product": "SOL-EUR",
  "state_sha256": "9f2c...",
  "state": "{...canonical...}",
  "questions_version": "QUESTIONS_V1:5e1a...",
  "backend": "baseline",
  "model": "baseline-lgbm@2026-10-16.1",
  "answers": {"...": "come 2.2.3"},
  "policy": {"policy_version": "baseline-lgbm@2026-10-16.1", "regime": 2, "regime_pctile": 38,
             "regime_rule": "ATR1D_PCTILE365:5/35/65/95", "model_regime": 2,
             "masked_probs": {"BUY": 0.72, "HOLD": 0.28}, "final": "BUY", "reason": "thresholds_met",
             "tau_buy": 0.55, "p_be_buffer": 0.05, "p_breakeven": 0.39},
  "latency_ms": {"build": 1.4, "inference": 2.1, "http_total": null, "server_model": null},
  "order_intent_id": "01JAB3...ULID"
}
```

`bar_source` in `rest | flat | flat_unproven` (sez. 2.3 A.2); `bar_lag_s = published_at - ts_bar_close` della barra; `decided_at` = istante di calcolo.

Blocco `policy`: `regime` e `regime_pctile` sono il regime e il token `atr_1d_pctile_365d` che hanno deciso (letti dallo stesso `DecisionState` inviato al decider), `regime_rule` la regola con cui sono stati ricavati, `p_be_buffer` il buffer del `policy.json` e `p_breakeven` il token di stato `costs.p_breakeven`. `model_regime` (`argmax` delle probabilita' di `vol_regime`) e' solo diagnostica: non partecipa a nessuna decisione. Con `reason` in `regime_veto | regime_unavailable` la decisione e' `HOLD` e il `RiskManager` non viene interpellato.

#### 2.2.5 Config YAML

```yaml
decision:
  backend: "baseline"                # baseline (default, CPU) | openjev | chronos-decider
  model: "registry:champion"         # alias del ModelRegistry (sez. 5.3): risolve model card, policy.json, label_config
  fallback_to_baseline: true
  questions_version: "QUESTIONS_V1"
  remote:                            # usato solo se backend in (openjev, chronos-decider)
    base_url: "http://decider:8080"
    api_key_env: "DECIDER_API_KEY"
    timeout_ms: 250
```

Nella config non stanno ne' soglie di policy ne' parametri di barriera: sono artefatti della versione di modello, nel registry (`models/registry/<version>/`). Cosi' non si possono applicare per errore le soglie di un modello a un altro. Esempio di `policy.json`:

```json
{
  "model": "baseline-lgbm@2026-10-16.1",
  "tau_buy": 0.55,
  "tau_sell": 0.50,
  "c_min": 0.10,
  "p_be_buffer": 0.04,
  "regime_rule": "ATR1D_PCTILE365:5/35/65/95",
  "tuned_on": {"split": "val_final", "span": "...", "n_trades": 52, "pnl_eur": 0.87, "objective": "net_pnl_eur",
               "min_trades": 30, "cadence_min": 15},
  "label_config_sha256": "a41c..."
}
```

Il `policy.json` non contiene soglie su `p_edge` ne' regimi ammessi: `p_edge_min` e `allowed_regimes_for_entry` sono chiavi vecchie e un file che le contiene non si carica (`PolicyConfig` solleva `ValueError`: fail closed). `regime_rule` deve essere esattamente `ATR1D_PCTILE365:5/35/65/95`; `tau_sell` e' fisso a 0.50 (non si tara). `tuned_on` e' un campo libero di tracciabilita'. Per un sottoinsieme non tarabile `tune_policy` scrive `{"untunable": true, "reason": "<esito>"}` (sez. 3.1.6); un file cosi' non e' un `policy.json` caricabile dal `DecisionEngine`.

Il `DecisionEngine` rifiuta di caricare un `policy.json` il cui `label_config_sha256` non coincide con quello del model card.

### 2.3 Linee Guida per l'implementazione in Sonnet 5.5

> PROMPT — Feature, Stato e Decision Layer
>
> Implementa i package `modules/market/`, `modules/features/`, `modules/decision/` di Chronos v2 (Python 3.12, asyncio, pydantic v2, mypy strict).
>
> **A. `modules/market/`**
> 1. `ws_feed.py` — `CoinbaseMarketStream`: usa `coinbase.websocket.WSClient` (da `coinbase-advanced-py`) sui canali `market_trades`, `ticker`, `level2`, `heartbeats` (`level2` alimenta il `LocalOrderBook` usato per spread, stima dello slippage e simulatore di fill della sez. 5; verificare se il canale richiede autenticazione JWT e, nel caso, usare la stessa key read-only). Il WSClient lavora con callback in un thread proprio: fai il bridge verso asyncio con `loop.call_soon_threadsafe(queue.put_nowait, msg)`. Riconnessione automatica con backoff; rileva gap tramite `sequence_num`; ogni intervallo di dati danneggiato (gap, reset, silenzio) viene notificato con `on_damage(start, end | None)` (consumatore: `BracketSimulator`, sez. 5.1.3). I trade, deduplicati per `trade_id` (il canale rimanda uno snapshot a ogni riconnessione), vanno a `on_trades` (simulatori di fill e bracket). Il feed WS non costruisce barre e non e' mai la fonte delle feature (invariante I1, sez. 0.5). Non usare il canale `candles` (granularita' 5 min).
> 1b. `timeframes.py` — aggregazione di barre 1m contigue in barre 1h/6h/1d allineate all'epoca UTC (1d chiude alle 00:00 UTC): `aggregate_bars` (vettoriale) e `TimeframeAggregator` (incrementale) con la stessa regola; una barra HTF esiste solo quando tutti i suoi minuti sono arrivati (mai la barra in formazione, vincolo C7); input non contiguo -> `BarGapError`. Usato da `IndicatorEngine` e `indicators_batch`.
> 2. `bars.py` — `BarService` (shell asyncio) + `BarFinalizer` (nucleo puro, senza I/O ne' clock) + `BarStoreWriter`. Le barre 1m sono le candele REST `get_public_candles(granularity="ONE_MINUTE")` (`CandleSource` e `candles_to_frame` di `backfill.py`, stesso parsing dello storico). Il minuto `M` e' pronto quando `now >= M + 1 min + S`. A ogni risveglio (`M + 1 min + S`, oppure `retry_s` se un minuto pronto e' ancora pendente) il servizio scarica per ogni prodotto `[next_minute, floor(now) + 1 min)` (chunk da 300) e, per ogni minuto pronto in ordine: candela presente -> barra `rest`; candela assente ed esiste nella risposta una candela successiva -> barra piatta `flat` (OHLC = close precedente, volume 0, stessa regola di `load_bars`); candela assente senza candele successive -> attesa, e oltre `rest_max_delay_s` barra piatta `flat_unproven` (solo con un fetch riuscito). Un fetch fallito non pubblica nulla e non inventa barre: retry ogni `retry_s`, senza limite. La candela del minuto in corso serve solo come prova di completezza, mai come dato (C7). Le barre sono pubblicate una sola volta, contigue, ordinate per `(ts, product)`, come `BarClosed(product_id, bar, source, published_at)`; il `BarStoreWriter` scrive nello store le sole barre `rest` ogni `store_flush_minutes` e allo shutdown, ed espone `complete_until(product)`. Nessuna aggregazione 15m/1h qui: gli HTF li aggrega solo l'`IndicatorEngine`. Config:
> ```yaml
> market:
>   bars:
>     rest_delay_s: 120         # S, validator 60..300; candele REST stabili dopo ~92 s (max su 70, misura del 2026-10-18)
>     rest_max_delay_s: 180     # S + 60; oltre: minuto senza candela e senza prova -> flat_unproven
>     retry_s: 5
>     decision_max_lag_s: 240   # S + 120; oltre: barra consumata, nessuna decisione (deve essere < stale_bar_s)
>     store_flush_minutes: 15
> ```
> Taratura di S: i default seguono S con `rest_max_delay_s = S + 60`, `decision_max_lag_s = S + 120`, `risk.kill_switch.stale_bar_s = S + 150`. Il valore di S e' accettato solo da un soak di 48 h che supera R4 (zero minuti comparsi o scomparsi) e R1, R2 (impatto delle revisioni sugli stati e sulle decisioni, sez. 5.1.7 riga "Barre REST"; minuti riletti con eta' >= 15 min); `rest_revision_ratio` e' diagnostica con allarme a 0.005. La scala di taratura si applica quando R1 o R2 falliscono e la classificazione mostra revisioni di assestamento (revisioni di valore entro pochi minuti dalla chiusura): si sale di un gradino (S = 120 -> 180 -> 300) con un nuovo soak; se prevalgono candele comparse o scomparse, se le revisioni sono rilette ore dopo la chiusura (alzare S non le risolve, misurato nel soak del 18-20/10/2026), oppure al gradino 300, si apre un CONFLITTO. Il criterio non si allenta e S non si abbassa sotto il valore validato.
> 3. `indicators.py` — `IndicatorEngine`, **incrementale O(1) per barra**, uno stato per (product, timeframe) con timeframe 1h, 6h, 1d aggregati dalle barre 1m (barre 1d con chiusura alle 00:00 UTC): ATR di Wilder (14), RSI di Wilder (14), EMA per MACD (12/26/9), Bollinger (20, 2), VWAP rolling 1d, volatilita' realizzata `RVd`, z-score di `ln(1 + quote volume)` giornaliero (30 giorni), percentile dell'ATR 1d su 365 giorni (`sortedcontainers.SortedList`) e i campi di prezzo, con le definizioni esatte della sez. 2.2.1.b. Fornisci anche la versione vettoriale degli stessi indicatori in `indicators_batch.py` (polars o pandas). Un test deve verificare che batch e incrementale coincidano (tolleranza `|a-b| <= 1e-9 * max(1, |b|)`, maschere NaN identiche; test T1-T10 della risoluzione indicatori, `docs/decisions/2026-10-17-indicatori.md`) su >= 5.000 barre 1m per ciascun prodotto, lette dallo storico backfillato in Parquet (sez. 0.5); `data/historical_*.csv` della v1 non va usato. La versione batch e' quella usata per costruire il dataset di training.
> 4. All'avvio, warm-up: `load_bars` sull'intero storico del prodotto (ancora della sez. 2.2.1.b) e replay di tutte le barre 1m nello stesso `IndicatorEngine` (`warm_up`, senza snapshot). Un warm-up parziale non e' ammesso: le ricorrenze di Wilder ed EMA dipendono dal punto di partenza. Budget <= 120 s per prodotto. Se il Parquet non copre il periodo, il processo esegue prima il backfill incrementale (sez. 0.5) e solo dopo apre gli stream. Dopo il warm-up il `BarFinalizer` viene inizializzato (`seed`) con l'ultima riga del frame del warm-up; il primo ciclo del `BarService` recupera da REST i minuti tra quella riga e `now - S`.
>
> **B. `modules/features/`**
> 1. `schemas.py` — pydantic `DecisionState` con i sotto-modelli della sez. 2.2.1. Campi interi dichiarati `int`, i decimali con validator che arrotonda (`round(x, 2)`). Ordine dei campi = ordine di dichiarazione.
> 2. `state_builder.py` — `StateBuilder(label: LoadedLabelConfig).build(*, snapshot: IndicatorSnapshot, btc: IndicatorSnapshot | None, sentiment: SentimentSnapshot | None, position: PositionInput, close_t: float) -> DecisionState`. Funzione pura, senza parametri di spread, fee o quote (garantito dalla firma): `position` e' l'unico input vivo e usa `close_t`, mai il mid del book. Per `BTC-*` `btc` deve essere `None`. Gli indicatori sono gia' in bps: conversione `int(round(x))` (non `x * 10_000`); `NaN -> None`. Clamp: `ret_bps`, `seq_1h_bps`, `seq_1d_bps`, `dist_vwap_1d_bps`, `macd_hist_1d_bps` in [-5000, 5000]; `vol_z_1d` in [-6, 6]; `bb_pos_1d` in [-1.0, 2.0]; `hh_ll_pos_30d` e rapporti con `round(x, 2)`. Per evitare token fuori distribuzione.
> 3. `serializer.py` — `serialize_state(state: DecisionState) -> str` = `state.model_dump_json()` con ordine stabile e senza spazi; `state_sha256(s: str) -> str`. **Unico punto di serializzazione** del sistema: lo importano sia il runtime sia `training/build_dataset.py`. Golden test: 3 stati fissi in `tests/golden/state_*.json` devono serializzarsi byte per byte uguali.
>
> **C. `modules/decision/`**
> 1. `questions.py` — `QUESTIONS_V1: Final[dict]` con il testo esatto della sez. 2.2.2 (instructions parametrizzate da `horizon_min`, `k_tp`, `k_sl` del training run, risolte una volta all'import dalla config del modello, non a ogni chiamata), `QUESTIONS_V1_SHA256` calcolato all'import.
> 2. `client.py` — `SystemOneClient`:
>    - `httpx.AsyncClient(base_url, timeout=httpx.Timeout(timeout_ms/1000), limits=httpx.Limits(max_keepalive_connections=8), http2=False)` riusato per tutta la vita del processo;
>    - `async def decide(self, state: str, questions: dict) -> DecisionAnswers` -> `POST /v1/systemone`, header `Authorization: Bearer <key>` se configurata;
>    - parse con pydantic (`ChoiceAnswer`, `NoulAnswer`, `ScoreAnswer`); validazione: probabilita' in [0,1] che sommano a 1 +-1e-3, chiavi di `criteria` complete;
>    - estrai `Server-Timing` con regex;
>    - errori -> eccezioni tipizzate `DeciderTimeout`, `DeciderUnavailable`, `DeciderBadResponse`; nessun retry interno.
> 3. `baseline.py` — `BaselineDecider` con la stessa firma di `decide`, che produce `DecisionAnswers` da tre modelli LightGBM (multiclass per action, binary per edge, multiclass per regime) sugli stessi campi numerici dello stato appiattito (`flatten_state()` in `features/serializer.py`). Le probabilita' grezze passano dalla calibrazione del model card (Platt `OOF_PLATT_V1` per `edge`, sez. 3.1.6; temperature per `action` e `vol_regime`) prima di essere restituite. La confidenza la calcola il client (sez. 2.1.2), non il decider.
> 4. `policy.py` — `DecisionPolicy(config).resolve(answers, side, *, vol_pctile) -> Decision` secondo la sez. 2.1.2, con `regime_from_pctile` / `regimes_from_pctile`, `REGIME_FEATURE`, `REGIME_EDGES_PCT`, `REGIME_RULE`, `ENTRY_REGIMES` e il `PolicyConfig` con `p_be_buffer` e `regime_rule` (le chiavi `p_edge_min` e `allowed_regimes_for_entry` fanno fallire il caricamento). Funzione pura, tabella di test parametrizzata con tutti i casi (FLAT/LONG x BUY/SELL/HOLD x `regime_veto` x `regime_unavailable` x decider down), test dei confini del regime (4.99/5/34/35/64/65/94/95), test che la risposta `vol_regime` e' ignorata e che `buy_mask` coincide con `resolve`.
> 5. `engine.py` — `DecisionEngine`: consuma `BarClosed`, aggiorna sempre l'`IndicatorEngine` con la barra; se `bar_lag_s <= market.bars.decision_max_lag_s` costruisce lo stato con istante logico `ts_bar_close = bar.ts + 1 min` (sentiment con `as_of = ts_bar_close`, sez. 2.1) e registra `decided_at`, `bar_source`, `bar_lag_s` nel `DecisionRecord`; altrimenti non decide e incrementa `decisions_skipped_late`; per prodotto costruisce lo stato, chiama il backend configurato (fallback su `BaselineDecider` in caso di eccezione se `decision.fallback_to_baseline: true`), applica la policy, scrive `DecisionRecord` su JSONL con `aiofiles` (rotazione giornaliera), pubblica `Decision` sulla queue verso il RiskManager. I prodotti vengono processati in parallelo con `asyncio.gather`, ma con un `asyncio.Semaphore(4)` sul decider.
>
> Test: client con `respx` (risposta valida, 429, timeout, JSON malformato, probabilita' che non sommano a 1); integrazione opzionale marcata `@pytest.mark.live` contro `OPENJEV_LIVE_URL`.

---

## 3. STRATEGIA DI FINE-TUNING DEL MODELLO DECISIONALE

### 3.1 Architettura Concettuale

#### 3.1.1 Principio: Decision Tokenization

Ogni coppia (stato, domanda) diventa un esempio il cui target e' **un singolo token di etichetta**. Il modello impara a mettere massa di probabilita' sui token di etichetta nella posizione di risposta. In inferenza si leggono i logit **solo** su quei token e si applica softmax (con temperatura calibrata per domanda): niente generazione, niente parsing di testo, una forward pass.

```
prompt = TEMPLATE(state_canonico, domanda, opzioni)  --->  [LM backbone + LoRA]  ---> logits[pos_answer]
                                                                                       |
                                                    gather(logits, label_token_ids) / T_q -> softmax -> probabilita'
```

Questa forma e' compatibile con il modo in cui OpenJev produce probabilita' (lettura diretta dei logit sui token di etichetta, confidenza `1 - H(p)/ln K`), ma il template e' **nostro** e identico in training e serving, cosa che non si puo' garantire puntando a un backend terzo. Per questo il serving di produzione e' `chronos-decider` (sez. 3.3), compatibile a livello di protocollo.

#### 3.1.2 Scelta del modello base

| Base | Parametri | Pro | Contro | Raccomandazione |
|------|-----------|-----|--------|-----------------|
| Qwen3.5-9B | 9B dense | Stessa base di Clef-flash (OpenJev); buona aritmetica su interi brevi. | ~18 GB bf16; serve una GPU >= 24 GB per servire a latenza bassa. | **Target v1.** |
| Qwen3.5-4B | 4B dense | Stessa base di JevK5; ~2x piu' veloce; entra in 16 GB. | Capacita' inferiore. | **Da addestrare in parallelo come controllo.** Se la differenza nella metrica economica (sez. 3.4) e' < 5%, si va in produzione con il 4B. |
| DiffusionGemma 26B-A4B | MoE | Backend di default di OpenJev. | Fine-tuning di un modello a diffusione mascherata non e' supportato dagli strumenti PEFT standard. | Solo zero-shot in fase 0. |

Verificare la licenza dei pesi scelti prima del deploy (i pesi Clef non sono elencati tra quelli Apache-2.0 nel README OpenJev: si parte dai pesi Qwen ufficiali, non da Clef).

**Perimetro di costo (C8):** il fine-tuning LoRA richiede una GPU ed e' rimandato alla fase cloud. Nella fase locale di questa sezione si implementa e si esegue tutto cio' che gira su CPU: `labeling`, `sampling`, `splits`, `build_dataset`, `baseline_lgbm`, `tune_policy`, `backtest`, `evaluate`. `train_lora.py`, `calibrate.py` (per il LLM) e `decider/` vengono scritti e coperti da test con un modello tiny su CPU, ma non vengono addestrati su scala. Il dataset JSONL della sez. 3.2.1 si arricchisce nel frattempo con le label live prodotte dal dry run (sez. 5.1.5).

#### 3.1.3 Costruzione delle label (triple-barrier, nette di fee)

Per ogni prodotto e ogni timestamp di campionamento `t` (chiusura barra 1m), con i parametri del `label_config` (`label_config.json`, sez. 0.4) e i costi del `CostModel` (sez. 0.3):

```
F          = entry_first_bar                                # 1 + ceil(S/60) = 3 con S = 120 s
A          = ATR_14_<atr_tf>(t)                             # noto al tempo t; atr_tf dal label_config
# l'ordine e' prezzato alla chiusura della barra t+F-1 (istante t + S) e vive dalla barra t+F (BarFillModel, sez. 5.1.3)
if entry_mode == maker:
    limit  = close[t+F-1] * (1 - s/2/1e4)                   # stima del best_bid all'invio
    fill   = prima barra k in [t+F, t+F+T_in-1] con low[k] < limit   # T_in = entry_timeout (30 barre)
    nessun fill con finestra osservata -> esito = NO_FILL (nessun trade)
    entry  = limit ; f_in = f_m
if entry_mode == taker:
    fill   = t+F ; entry = open[t+F] * (1 + (s/2 + slip)/1e4) ; f_in = f_t
upper      = entry + k_tp * A
lower      = entry - k_sl * A
vertical   = fill + H
barra fill:  low <= lower -> esito = LOWER (watchdog: il bracket non e' ancora piazzato, il TP non vale)
scan 1m bars in (fill, vertical]:
    if   low  <= lower -> esito = LOWER (anche se high > upper nella stessa barra: conservativo)
    elif high >  upper -> esito = UPPER, exit_px = upper, f_out = f_m
LOWER: exit_px = min(lower, open) * (1 - (s/2 + slip + x)/1e4), f_out = f_t
nessuna barriera   -> esito = VERTICAL, exit_px = close[vertical] * (1 - (s/2 + slip)/1e4), f_out = f_t
finestra non osservata o A non valido -> UNRESOLVED (non e' una label)
net_ret    = exit_px * (1 - f_out) / (entry * (1 + f_in)) - 1                 # formula esatta della sez. 0.3
label_window_end(t) = max(t + (F + T_in - 1) min, ceil_to_hour(t)) + H        # fine dei dati usati da tutte le label di t
```

La scansione e la stima dei costi chiamano gli stessi oggetti del runtime (`CostModel`, `BarFillModel`), non reimplementazioni. `s` (spread) per il training e' la mediana per prodotto e ora del giorno misurata dal recorder (sez. 5); finche' non ci sono registrazioni si usa `max(spread del probe, 3 bps)` per prodotto. Lo spread fa parte della label ed entra nello `sha256` del `label_config`. Lo stesso valore (`spread_for`) e' l'unico spread usato nel blocco `costs` dello stato; lo stato non contiene lo spread vivo. I parametri sono scritti nel `label_config` del model card e **devono** coincidere con quelli usati dal RiskManager (sez. 4), altrimenti `edge.noul` non ha significato in esecuzione.

`t` e' l'istante logico di chiusura della barra (`ts_bar_close`), non l'istante di calcolo `t + S` (sez. 2.1). La label dipende da `S` solo attraverso `entry_first_bar`, registrato nel `label_config`: e' la prima barra in cui l'ordine reale, inviato a `t + S`, puo' essere eseguito. Se `1 + ceil(market.bars.rest_delay_s / 60) != entry_first_bar`, il runtime logga `label_exec_delay_mismatch` (non bloccante); lo scarto residuo rientra nella parita' misurata dal dry run (sez. 5.1.7).

**Studio di fattibilita' (`training/feasibility.py`, prima del primo training).** Sullo storico backfillato e con le fee reali (o presunte, marcate come tali) valuta la griglia `atr_tf in {6h, 1d}`, `k_tp in {3, 4, 5}`, `k_sl in {1.5, 2}`, `entry_mode in {maker, taker}`, `H in {5, 10, 15, 20} giorni` (H = 20 solo diagnostica) sui sample della griglia oraria, con i prodotti in pool (un solo `label_config`).
- **Finestra di selezione:** `holdout_start` = giorno UTC dell'ultima barra meno 120 giorni; si usano solo le barre con chiusura < `holdout_start - embargo` e i sample con `label_window_end` (calcolato con H = 20) < `holdout_start - embargo`. L'holdout non entra mai nella scelta; `holdout_start` viene scritto nel `label_config` (sez. 3.1.4).
- **Lift senza drift:** ogni prodotto viene detrendizzato sulla finestra (`mu = (ln C[T1] - ln C[T0]) / (T1 - T0)`, `X~ = X * exp(-mu (i - i0))` su open, high, low, close); ATR e label si ricalcolano sulla serie detrendizzata; `lift_0 = p_breakeven - p_hist_0`, con `p_hist_0` = frequenza di UPPER sui sample con fill. La serie detrendizzata esiste solo dentro lo studio: non entra nello stato, nelle label di training ne' nello `sha256` (C7). Il lift grezzo e `lift_rw` sono riportati come informazione.
- **Campioni indipendenti:** `N_p` = somma dei pesi di unicita' (sez. 3.1.4) dei sample con fill sul loro intervallo reale `[t, exit]`; `N_eff = sum_p N_p / (1 + (K-1) rho)`, con `rho` = correlazione media a coppie degli indicatori UPPER a stesso `t` (troncata in [0, 1]); `indep/yr = N_eff / anni`; `SE = sqrt(p (1-p) / N_eff)`.
- **Eleggibilita':** fee floor superato in >= 80% dei sample, fill maker >= 50%, `p_vertical <= 0.05`, `H <= 15` giorni; per ogni (atr_tf, k_tp, k_sl, entry_mode) conta solo il piu' piccolo H eleggibile.
- **Scelta:** `lift_0` minimo. Le combinazioni entro 0.01 dal minimo sono equivalenti: si sceglie quella con `N_eff` massimo, poi H minore, poi l'ordine lessicografico.
- **GO** se `lift_0 <= 0.10` sul pool, `max_s lift_0(-s) <= 0.10` (jackknife: ricalcolo escludendo un semestre di calendario alla volta, `mu` invariato) e, per ogni prodotto, `lift_0,p <= 0.10 + 2 SE_p`. Altrimenti il report dichiara NO-GO con i motivi, `label_config.json` non viene scritto, l'exit code e' 3 e il progetto non procede al training.
- **Diagnostiche senza soglia:** `p_vertical` e `vert_mean_net_bps` (prudenza dell'EV a due esiti, sez. 0.3), durate reali `exit - fill`, `p_hist` e lift per semestre e anno con SE, test chi2 di omogeneita' fra semestri, saturazione in H (flag `drift_dominated` se il lift grezzo a H = 20 scende di oltre 0.02 rispetto a H = 15), `sigma_r`, `W + L` e `N_min` per edge 0.05/0.08/0.10/0.15, ATR_6h mediano in bps per prodotto.

Output: `label_config.json` (con `sha256`, `selection`, spread di label e metriche dello studio) + `data/reports/feasibility.md`.

Label per domanda:

| Domanda | Stato posizione nel sample | Regola target |
|---------|----------------------------|---------------|
| `action` | FLAT | `BUY` se esito = UPPER **e** `net_ret > 0`, altrimenti `HOLD` (compreso `NO_FILL`). |
| `action` | LONG (sintetico, vedi sotto) | `SELL` se, uscendo ora a mercato (costo `c_mkt_out`), il risultato e' migliore di quello del bracket ancora attivo: cioe' esito futuro del bracket = LOWER, oppure VERTICAL con `exit_px * (1 - f_t)` < valore di uscita immediata. Altrimenti `HOLD`. |
| `edge` | FLAT e LONG | `true` se esito = UPPER, `false` se LOWER o VERTICAL. I sample `NO_FILL` vengono esclusi per questa domanda (la domanda e' condizionata all'apertura). |
| `vol_regime` | FLAT e LONG | Bucket 0..4 di `fwd(t) = R(E_t + H)` rispetto ai quantili 20/40/60/80 (`np.quantile`, `method="linear"`) di `ref(t) = {R(E) : F_t - 365 d < E <= F_t}`, con `R = RVd(H/60, .)` della sez. 2.2.1.b, `E_t = ceil_to_hour(t)`, `F_t = floor_to_hour(t)`; `bucket = searchsorted(q, fwd, side="right")`. Nessuna label se `fwd` o uno dei valori di `ref` e' NaN (quindi le label `vol_regime` esistono solo da circa 375 giorni dopo l'inizio dello storico). |

**Sample LONG sintetici:** per ogni `t` campionato si genera anche uno stato con `position.side = LONG`, dove l'ingresso fittizio e' a `t - U`, con `U ~ Uniform{60..H}` minuti. Si calcolano `unreal_bps`, `dist_sl_atr`, `dist_tp_atr` coerenti con quell'ingresso e si scartano i sample in cui il bracket fittizio sarebbe gia' stato chiuso prima di `t`. In questo modo il modello vede entrambe le condizioni di posizione e impara il mascheramento.

#### 3.1.4 Campionamento, bilanciamento, split

- **Campionamento:** non ogni minuto (autocorrelazione estrema, label quasi duplicate). Si usa l'unione di (a) una griglia oraria e (b) eventi del filtro CUSUM simmetrico sui log-rendimenti 1m con soglia `h = 1.0 * ATR_1h%`. Ordine di grandezza atteso: ~10–15k timestamp per prodotto per anno. Le label si sovrappongono molto: i pesi di unicita' (sotto) sono obbligatori, e per giudicare la significativita' statistica si usa `N_eff` dello studio di fattibilita' (sez. 3.1.3; `indep/yr` nel report), non il numero di righe.
- **Bilanciamento:** `HOLD` domina. Sottocampionamento di `HOLD` a un rapporto massimo 3:1 rispetto alla classe di minoranza **solo nel train**; validation e test restano con la distribuzione naturale (necessaria per la calibrazione). Pesi per sample = unicita' media della label sul suo intervallo reale, alla Lopez de Prado: `[t, exit_idx]` per `edge` e `action` con fill, `[t, t+F+T_in-1]` per i `NO_FILL`, `[t, label_window_end(t)]` per `vol_regime`.
- **Split:** walk-forward con espansione, 5 fold. Tra train e validation/test: **purge** dei sample con `label_window_end(t)` (sez. 3.1.3) >= inizio del periodo successivo - embargo, con **embargo** di 2 giorni (`training/splits.py`, `DEFAULT_EMBARGO`). Nessuno split casuale.
- **Holdout finale:** inizia a `selection.holdout_start` del `label_config` (fissato dallo studio di fattibilita': giorno UTC dell'ultima barra meno 120 giorni) e comprende tutto lo storico successivo, che quindi cresce nel tempo. Non e' mai usato per scegliere `label_config`, modello o soglie; `build_dataset` passa questo valore a `purged_walk_forward(holdout_start=...)`.

#### 3.1.5 Sentiment storico e feature dropout

Le news RSS non hanno storico. Strategia:
1. **v1.0:** sentiment storico ricostruito da GDELT (titoli con timestamp di ingestione GDELT, usati come `ingested_at`) passato nello stesso classificatore ONNX e nello stesso aggregatore. Dove GDELT non copre, `sentiment: null`.
2. **Feature dropout:** nel 25% dei sample di train il blocco `sentiment` viene forzato a `null`, cosi' il modello e' robusto sia agli outage live sia alla differenza di distribuzione GDELT vs RSS live.
3. **v1.x:** retraining mensile includendo i `DecisionRecord` live, che contengono il sentiment reale delle sorgenti RSS. Dopo >= 6 mesi di dati live, il peso dei dati GDELT viene ridotto.

#### 3.1.6 Calibrazione post-training

Temperature scaling **per domanda** (`T_action`, `T_edge`, `T_regime`) sul validation fold piu' recente. Le temperature sono salvate nel model card e applicate dal server. Dopo la calibrazione, `DecisionPolicy` viene tarata (grid search delle soglie `tau_buy`, `c_min` e del buffer `p_be_buffer`) massimizzando il PnL netto simulato sul validation set con `training/backtest.py`, cioe' con la stessa `DecisionPolicy` e lo stesso `RiskManager` del runtime (fee floor, EV gate ed EV buffer della sez. 0.3 attivi). L'output e' `policy.json`.

**Calibrazione di `edge` fuori campione (baseline LightGBM, schema `OOF_PLATT_V1`).** Il calibratore di `edge` e' un Platt a 2 parametri (nessun isotonico); cambia l'insieme su cui si stima. In `fit_subset` (`training/baseline_lgbm.py`):
1. il booster principale e' invariato: train, early stopping sul validation, `n* = best_iteration`;
2. le righe `edge` del train (FLAT con `edge_target` noto), ordinate per `(t, product)`, si dividono in `CAL_FOLDS = 4` blocchi contigui di pari numerosita'. Per il blocco `b`, con `s_b = min t` e `E_b = max label_window_end`, l'insieme di fit contiene le righe del train con `label_window_end < s_b - embargo` oppure `t > E_b + embargo` (purge sui due lati, `embargo = cfg.embargo_days`); con meno di `2 x min_data_in_leaf` righe di fit il blocco non si valuta. Un booster con gli stessi iperparametri, `n*` alberi, senza validation ne' early stopping e con pesi `w_edge` normalizzati a media 1 sull'insieme di fit produce i punteggi grezzi `z[b]` (`oof_edge_scores`);
3. l'insieme di calibrazione `C` e' fatto dalle righe del train con punteggio fuori campione piu' le righe del validation (punteggi del booster principale). Mai righe del test del fold: le finestre dei fold precedenti non sono ammesse, perche' stanno nel train espandente del fold e sarebbero in-sample;
4. `N_eff(C) = sum(w_edge su C) / (1 + (K - 1) * rho)`, con `K` = numero di prodotti presenti in `C` e `rho = label.raw["feasibility"]["rho_raw"]` (1.0 se manca), lo stesso `rho` usato da `evaluate`;
5. Platt (`fit_platt`) su `C` con pesi `w_edge / mean(w_edge su C)`;
6. se `N_eff(C) < CAL_NEFF_MIN = 200`: per un fold `edge_cal.ok = False` (la calibrazione si salva comunque, resta informativa per G1 e G2) e il fold e' non tarabile; per il modello `final` e' un `TrainingError` (exit 1).

`calibration.json` registra `edge_cal: {scheme, n_rows_oof, n_rows_val, n_eff, n_eff_min, ok}`; il model card registra `calibration_scheme: "OOF_PLATT_V1"` e lo stesso blocco per ogni sottoinsieme. `action` e `vol_regime` restano calibrati con la temperatura sul validation.

**Griglia e scelta (`training/tune_policy.py`).** Prima della griglia si controlla che il model card abbia `calibration_scheme == "OOF_PLATT_V1"`: un modello addestrato con un altro schema non si tara con questo codice. La griglia ha al massimo 20 combinazioni (`MAX_COMBINATIONS`, asserita in `grid()`): `tau_buy` a 2 quantili (0.80 e 0.95) di `P(BUY)` mascherata sulle righe di decisione del validation il cui regime e' in `ENTRY_REGIMES`; `c_min` in {0.00, 0.10}; `p_be_buffer` in {0.00, 0.02, 0.04, 0.06, 0.08}. Non ci sono dimensione di regime, quantili di `p_edge` ne' soglie su `p_edge`; `tau_sell` e' fisso a 0.50 (SELL piu' probabile di HOLD fra le azioni valide) e non si tara mai. Ogni combinazione e' valutata con il backtest completo a cadenza di 15 minuti (sez. 3.5 punto 8). Una combinazione si sceglie solo se ha `n_trades >= 30` (`MIN_TRADES`) **e** `pnl_eur > 0`; fra queste vince il PnL piu' alto e, a parita', il buffer piu' alto, poi il `tau_buy` piu' alto, poi il `c_min` piu' alto. Le soglie si tarano per fold sul validation del fold stesso, e quelle del modello `final` sul validation finale.

Esito per sottoinsieme, valutato in quest'ordine:
1. `untunable_calibration`: `edge_cal.ok` e' falso, la griglia non si esegue;
2. `ok`: esiste una combinazione scelta;
3. `no_candidates_regime`: nessuna riga di decisione del validation ha un regime ammesso (dopo il passaggio al token e' un difetto da indagare);
4. `untunable_ev`: il quantile 95% di `p_edge - p_breakeven` (token di stato, righe con regime ammesso e `p_breakeven` finito) e' < 0;
5. `untunable_pnl`: tutti gli altri casi.

`policy.json` di un sottoinsieme non tarabile: `{"untunable": true, "reason": "<esito>"}`; in `evaluate` quel sottoinsieme gira come "nessun ingresso" (sez. 3.4), senza prendere soglie da altri fold. `tune_report.json` riporta, per sottoinsieme, `reason`, `n_combinations`, `best`, `edge_cal` e `val_diag = {n_rows, n_rows_regime_ok, regime_share, mean_p_edge, p_hist_obs, n_eff, se, bias_z, ev_headroom_q95}` (con `bias_z = (mean_p_edge - p_hist_obs) / se`, calcolato sulle righe `edge` del validation con la calibrazione del sottoinsieme): distingue un problema di calibrazione da un modello che non vede nessun valore atteso positivo. La diagnostica `training/regime_deciles.py` (sola lettura, `data/reports/regime_deciles.md`) riporta per ogni decile di `atr_1d_pctile_365d` (bande fisse [0,10) ... [90,100], piu' una riga "all"), sulle righe `edge` con fill nella finestra pre-holdout, `n`, `p_hist` pesata, `p_breakeven` mediano, lift, `N_eff`, `SE` e la media di `net_ret_bps`: verifica le soglie 5/35/65/95, non le sceglie.

### 3.2 Struttura dei Dati / Schema JSON

#### 3.2.1 Dataset JSONL (un record per coppia stato-domanda)

```json
{
  "id": "SOL-EUR|2025-03-01T12:15:00Z|FLAT|action",
  "fold": 3,
  "split": "train",
  "product": "SOL-EUR",
  "t": "2025-03-01T12:15:00Z",
  "position_side": "FLAT",
  "state": "{\"v\":\"chronos.state.v1\",...}",
  "question_key": "action",
  "question_type": "choice",
  "options": ["BUY", "SELL", "HOLD"],
  "target": "BUY",
  "target_index": 0,
  "weight": 0.83,
  "meta": {
    "barrier_outcome": "UPPER",
    "net_ret_bps": 1698,
    "fill_minute": 7,
    "exit_minute": 7215,
    "atr_tf": "1d", "atr_bps": 460,
    "label_config_sha256": "a41c...",
    "questions_version": "QUESTIONS_V1:5e1a...",
    "sentiment_dropped": false
  }
}
```

Esempio `noul` (`target` in {"true","false"}) e `score` (`target` in {"0".."4"}).

#### 3.2.2 Template di prompt (raw, senza chat template)

Il chat template di Qwen3.5 viene **evitato** di proposito (token di thinking e system prompt variabili). Template fisso, versionato come `PROMPT_TEMPLATE_V1`:

```
### STATE
{state}
### QUESTION [{question_type}]
{instructions}
### OPTIONS
{option_lines}
### ANSWER
```

`option_lines` per tipo:

| Tipo | Linee | Token di etichetta (preceduti da spazio) |
|------|-------|------------------------------------------|
| choice | `A = BUY: <criteria>` / `B = SELL: ...` / `C = HOLD: ...` | ` A`, ` B`, ` C` |
| noul | `Y = yes` / `N = no` | ` Y`, ` N` |
| score | `0 = very_low` ... `4 = extreme` | ` 0` ... ` 4` |

Il target nel training e' la stringa del prompt seguita **esattamente** da un token di etichetta, poi EOS. Test obbligatorio: ciascun token di etichetta deve tokenizzarsi in **un solo id** con il tokenizer del modello base; se non e' cosi', il build del dataset fallisce.

#### 3.2.3 Configurazione PEFT/LoRA

```yaml
finetune:
  base_model: "Qwen/Qwen3.5-9B"            # verificare l'id esatto su HF al momento del run
  control_model: "Qwen/Qwen3.5-4B"
  prompt_template: "PROMPT_TEMPLATE_V1"
  max_seq_len: 768                          # stato ~300 + domanda ~120 + margine
  quantization:                             # QLoRA se VRAM < 40 GB
    load_in_4bit: true
    bnb_4bit_quant_type: "nf4"
    bnb_4bit_compute_dtype: "bfloat16"
    bnb_4bit_use_double_quant: true
  lora:
    r: 16
    lora_alpha: 32
    lora_dropout: 0.05
    bias: "none"
    target_modules: ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]
    task_type: "CAUSAL_LM"
  training:
    epochs: 2
    learning_rate: 1.0e-4
    lr_scheduler: "cosine"
    warmup_ratio: 0.03
    weight_decay: 0.0
    per_device_batch_size: 16
    gradient_accumulation_steps: 4          # batch effettivo 64
    gradient_checkpointing: true
    bf16: true
    max_grad_norm: 1.0
    group_by_length: true
    packing: false                          # un target per sequenza
    eval_steps: 500
    early_stopping_patience: 3              # su val log-loss ristretta ai token di etichetta
    seed: 1234
  loss:
    type: "restricted_label_ce"             # CE solo sulla posizione di risposta, softmax ristretta ai token di etichetta della domanda
    use_sample_weights: true
  ablations:                                # da eseguire, risultati nel model card
    - {lora.r: 8}
    - {lora.r: 32, lora_alpha: 64}
    - {lora.target_modules: ["q_proj", "v_proj"]}
```

Perche' `restricted_label_ce` invece della CE standard sul vocabolario: la CE standard spende capacita' nel ridurre la probabilita' di decine di migliaia di token irrilevanti; la CE ristretta ottimizza esattamente la quantita' che il server legge (softmax sui soli token di etichetta) e produce probabilita' meglio calibrate prima del temperature scaling.

#### 3.2.4 Model card (artefatto di rilascio)

```json
{
  "model": "chronos-decider-qwen35-9b-lora@v1.2.0",
  "base_model": "Qwen/Qwen3.5-9B",
  "adapter_sha256": "...",
  "prompt_template": "PROMPT_TEMPLATE_V1",
  "questions_version": "QUESTIONS_V1:5e1a...",
  "state_schema": "chronos.state.v1",
  "label_config": {"k_tp": 4.0, "k_sl": 2.0, "horizon_min": 14400, "atr": "ATR_14_1d", "atr_tf": "1d", "entry_mode": "maker",
                   "entry_timeout_min": 30, "entry_first_bar": 3, "cost_model": "COST_V1",
                   "fees_bps": {"maker": 60, "taker": 120, "source": "assumed"},
                   "slip_bps": 5, "stop_extra_slip_bps": 10,
                   "spread_bps": {"BTC-EUR": 3.0, "ETH-EUR": 3.0, "SOL-EUR": 3.0},
                   "selection": {"holdout_start": "2026-06-20T00:00:00Z"}, "sha256": "a41c..."},
  "label_token_ids": {"action": [362, 425, 356], "edge": [809, 451], "vol_regime": [220, 16, 17, 18, 19]},
  "temperatures": {"action": 1.31, "edge": 1.12, "vol_regime": 1.07},
  "data": {"products": ["BTC-EUR", "ETH-EUR", "SOL-EUR"], "indicator_anchor": {"BTC-EUR": "2023-10-01T00:00:00Z"}, "train_range": ["2023-10-01", "2026-04-30"],
           "n_samples": 210000, "holdout": ["2026-06-01", "2026-09-30"]},
  "metrics_holdout": {"action_logloss": 0.0, "edge_brier": 0.0, "edge_ece": 0.0, "net_pnl_bps": 0, "sharpe": 0.0,
                      "vs_baseline_lgbm": {"net_pnl_bps_delta": 0}},
  "created_at": "2026-10-17T00:00:00Z"
}
```

(I valori `label_token_ids` e le metriche sono placeholder: vengono scritti dagli script.)

### 3.3 Serving: `chronos-decider`

- FastAPI + uvicorn, un solo processo, modello caricato una volta su GPU (bf16 per il 9B su >= 24 GB; adapter LoRA fuso con `merge_and_unload()` al rilascio per eliminare overhead).
- `POST /v1/systemone` (sottoinsieme del protocollo: `noul`, `choice`, `score`) e `GET /v1/models`.
- Per una richiesta con Q domande: tokenizza il prefisso `### STATE\n{state}\n` una volta, calcola il KV cache del prefisso, poi esegui in batch le Q continuazioni (domanda + opzioni + `### ANSWER`) riusando il cache (`past_key_values` espanso sul batch). Leggi `logits[:, -1, label_token_ids_q]`, dividi per `T_q`, softmax.
- `noul` = P(` Y`). `choice` = argmax + probabilita' + confidenza `1 - H(p)/ln K`. `score` = `sum(i * p_i)` + legend + probabilita' + confidenza.
- Header `Server-Timing` nello stesso formato di OpenJev.
- Target di latenza: p50 <= 60 ms, p99 <= 150 ms per 3 domande con stato ~300 token su GPU classe RTX 4090 / L4 o superiore. Verificato da un benchmark nello stesso container.
- `torch.inference_mode()`, `torch.compile` opzionale dietro flag, warm-up all'avvio con 20 richieste sintetiche, healthcheck `GET /healthz` che fa una vera inferenza su uno stato golden e confronta le probabilita' con quelle attese (tolleranza 1e-3) per rilevare pesi corrotti.

### 3.4 Gate di valutazione (deploy solo se tutti passano)

| Gate | Criterio |
|------|----------|
| G0 Regime | Prima di ogni altro gate. Sull'holdout, con il `label_config` del modello e le barre grezze: `lift_holdout = p_breakeven - p_hist` (griglia oraria, sample con fill) <= `0.10 + 2 SE_holdout` (`N_eff` e `rho` come nello studio di fattibilita'). Se fallisce: NO-GO per cambio di regime, e lo studio va rifatto su dati nuovi. |
| G1 Calibrazione | ECE (15 bin) <= 0.05 per `edge` e per la classe BUY di `action`, su ogni fold di test. Diagnostica senza soglia: ECE e bias firmato di `edge` nella regione di decisione, cioe' sui soli sample con `p_edge >= p_breakeven + p_be_buffer` (`p_breakeven` = token di stato letto dallo stato del sample, `p_be_buffer` = quello del `policy.json` del fold), sui fold di test; per ogni fold anche `bias_all` (media pesata di `p - y`), `n_eff_test`, `se` e `bias_z`. |
| G2 Discriminazione | Log-loss di `edge` inferiore a quella del predittore a priori (frequenza base) su >= 4/5 fold. |
| GE Evidenza assoluta | E1-E5 sui trade del backtest (fold di test concatenati, policy congelata) e sull'holdout; esito GO / INCONCLUSO / NO-GO. Calcolato per il candidato e per il baseline LightGBM. |
| G3 Economico relativo | Solo se il candidato non e' il baseline. Backtest event-driven (sez. 3.5) con fee reali e slippage: PnL netto e Sharpe del modello > baseline LightGBM su >= 3/5 fold **e** sull'holdout. |
| G4 Robustezza | Con `sentiment = null` forzato su tutto l'holdout, il PnL netto non scende sotto il 70% di quello con sentiment. |
| G5 Latenza | p99 server <= 150 ms sotto carico di N prodotti in parallelo. |
| G6 Dry run | G-DRY-OPS della sez. 5.1.7 superato con il modello candidato come shadow book, piu' la regola di promozione L5 della sez. 5.1.6. |

Criteri GE (OOS = fold di test concatenati; blocchi del bootstrap di `ceil_days(T_in + F + H)`, circolari sul tempo, tutti i prodotti nello stesso blocco; 10 000 repliche, seed fisso):
- E1 campione: >= 150 trade OOS e `N_eff_trade` >= 100 (peso di ogni trade `1 / (1 + rho * c)`, con `c` = numero di altri prodotti con un trade sovrapposto);
- E2 significativita': percentile 2.5% del block bootstrap della media di `net_ret` (bps, non pesata) > 0, e PnL OOS in EUR > 0;
- E3 selezione: test di permutazione contro ingressi casuali dello stesso fold e prodotto (fill e fee floor superato, una posizione per prodotto, stesso numero di trade), sul `net_ret` della label alla barra di decisione: p <= 0.05;
- E4 concentrazione: nessun fold oltre il 50% del PnL OOS e >= 3/5 fold con PnL > 0;
- E5 holdout: >= 5 trade, PnL > 0 e `z = (m_h - m_oos) / sqrt(se_h^2 + se_oos^2) > -1.96`. Un holdout con 0 trade (anche quando il modello `final` non e' tarabile) e' E5 = False, con motivo `holdout_no_trades`; con 1..4 trade E5 resta non valutabile.

**Fold piatti.** Un sottoinsieme `untunable` (sez. 3.1.6) non prende soglie da altri fold: il suo test gira come "nessun ingresso", con zero trade. `pnl_by_fold` contiene sempre le 5 chiavi e un fold piatto vale 0.0; 0.0 non e' maggiore di 0, quindi non conta fra i fold positivi di E4 e non cambia la quota sul totale (un fold senza trade non e' neutro). E1, E2 e il bootstrap usano solo i trade esistenti, senza valori fittizi a 0 bps; E3 considera solo le coppie (fold, prodotto) con trade. `evaluation.json` contiene `fold_status` (`fold_k` -> `ok` oppure l'esito `untunable_*`, e lo stesso dato per `final`) e il report stampa una riga per fold (esito, trade, PnL).

**Baseline banali (solo report).** `evaluate` riporta B0, buy and hold equal-weight su ogni span di test e sull'holdout (ingresso al close della prima barra con fee maker, uscita al close dell'ultima con fee taker piu' `s/2` e slippage, formula esatta di `net_return`; rendimento, max drawdown della curva giornaliera equal-weight, rendimento per giorno di esposizione), e B2, sempre investito con lo stesso bracket (catena greedy sui candidati di ogni coppia (fold, prodotto): il primo candidato, poi il primo con ingresso alla barra di uscita del precedente o dopo; numero di trade e media di `label_bps`). Non hanno soglie e non cambiano alcun gate ne' l'esito di GE.

**Criterio di uscita di P1** (Appendice A): PnL > 0 su >= 3/5 fold e sull'holdout; un fold o un holdout senza trade ha PnL 0 e non e' positivo.

Esito: G0 fallito -> NO-GO; E1 fallito -> INCONCLUSO se la media OOS e' > 0, altrimenti NO-GO; tutti ok -> GO; E2 fallito con `t = media / SE >= 1.0` e gli altri ok (E5 ok o non valutabile) -> INCONCLUSO; E5 non valutabile e gli altri ok -> INCONCLUSO; altrimenti NO-GO.

Deploy: la rampa R0 (sez. 5.1.7) richiede G0, G1, G2, G4 superati e GE = GO per il decider del book champion, piu' G3 se il candidato e' un LLM. Con GE = INCONCLUSO il champion gira solo in dry, con capitale live zero, finche' la rivalutazione di GE sui trade OOS piu' i trade chiusi del book champion in dry non da' GO. Se G3 fallisce si va in produzione con il `BaselineDecider`, alle stesse condizioni sul suo GE: il modello LLM deve guadagnarsi il posto con i numeri, e anche il baseline deve dimostrare un edge assoluto prima di usare capitale reale. `training/evaluate.py` esce con 0 (GO), 5 (INCONCLUSO) o 1 (altro).

### 3.5 Linee Guida per l'implementazione in Sonnet 5.5

> PROMPT — Pipeline di training e server decider
>
> Crea il package `training/` e la directory `decider/` di Chronos v2. Python 3.12. Librerie: `polars` (dataset), `numpy`, `numba` (scansione barriere), `transformers`, `peft`, `bitsandbytes`, `accelerate`, `datasets`, `lightgbm`, `scikit-learn` (solo metriche), `scipy`, `fastapi`, `uvicorn`, `pydantic` v2. Niente `trl`: il trainer e' un `transformers.Trainer` con `compute_loss` custom.
>
> 1. `training/labeling.py`
>    - `triple_barrier(open, high, low, close, atr, t_idx, label_config: LabelConfig, fees: FeeRates, costs: CostParams, spread_bps) -> TripleBarrierResult(outcome, fill_idx, exit_idx, net_ret, entry_px)` con l'algoritmo della sez. 3.1.3 (ingresso maker/taker, `NO_FILL`, regola conservativa "entrambe le barriere nella stessa barra -> LOWER", `net_ret` esatto). Kernel interno compilato con `@numba.njit` che riceve solo array e scalari; `CostParams` e' la vista numerica del `CostModel` della sez. 0.3 (`modules/risk/costs.py`), non una copia delle formule;
>    - `forward_rv_bucket(...)` con la definizione della sez. 3.1.3, usando `indicators_batch.rv_per_day_bps` (quantili sui 365 giorni **precedenti** a ogni t, nessun dato futuro);
>    - `label_window_end(ts_bar_close, label_config)` e `load_label_config(path)` (verifica dello `sha256`, exit code 2 se non coincide);
>    - test con serie sintetiche costruite a mano per ogni esito (UPPER, LOWER, VERTICAL, NO_FILL, stessa barra) e un test che ricalcola a mano il `net_ret` dell'esempio della sez. 0.3.
> 2. `training/sampling.py` — `cusum_events(log_ret, h)` e griglia oraria; `uniqueness_weights(t0, t1)`.
> 2b. `training/feasibility.py` — CLI `python -m training.feasibility --products BTC-EUR ETH-EUR SOL-EUR [--fees-from-probe data/probe/coinbase_probe.json]`, studio della sez. 3.1.3 (finestra pre-holdout, `lift_0`, eleggibilita', pareggio su `N_eff`, jackknife, guardia per prodotto) e stop esplicito (exit code 3) se non e' un GO. Produce `label_config.json` (con `sha256`) e `data/reports/feasibility.md`. Va eseguito **prima** di `build_dataset.py`.
> 3. `training/splits.py` — `purged_walk_forward(timestamps, t1, n_folds=5, embargo=timedelta(days=2), holdout_days=120, val_frac=0.15, holdout_start=None)` che produce indici (`timestamps` = `ts_bar_close`, `t1` = `label_window_end`); `holdout_start_from(last_ts_bar_close, holdout_days)`; test che verifica l'assenza di sovrapposizione tra intervalli `[t, t1]` di train e di test.
> 4. `training/build_dataset.py` — CLI: `python -m training.build_dataset --config config/train.yml`. Passi: carica candele 1m da Parquet -> calcola indicatori con `modules.market.indicators_batch.compute_indicators` sull'intero storico del prodotto (ancora sez. 2.2.1.b), poi seleziona i timestamp (sample con `atr_14_<atr_tf>` NaN esclusi) -> ricostruisce sentiment storico da `data/news_history.db` con `modules.sentiment.aggregator` (stesso codice del runtime) -> costruisce `DecisionState` con `modules.features.state_builder` e serializza con `modules.features.serializer.serialize_state` (stesso codice del runtime) -> genera sample FLAT e LONG sintetici -> label -> feature dropout 25% sul blocco sentiment nel solo train -> scrive `data/datasets/<run_id>/{train,val,test}.jsonl` e `label_config.json`. Fallisce se un token di etichetta non e' single-token.
> 5. `training/prompting.py` — `PROMPT_TEMPLATE_V1`, `render_prompt(state, question) -> str`, `label_token_ids(tokenizer, question) -> list[int]`. Importato sia dal trainer sia dal server.
> 6. `training/train_lora.py` — carica il base model (4-bit NF4 se `quantization.load_in_4bit`), `prepare_model_for_kbit_training`, `get_peft_model(LoraConfig(...))` dai parametri della sez. 3.2.3. Collator: tokenizza `prompt` (senza token di etichetta), registra la posizione dell'ultimo token e gli id dei token di etichetta della domanda. `compute_loss`: `logits_last = outputs.logits[arange(B), last_pos]`, `restricted = logits_last.gather(1, label_ids_padded)` con mascheramento `-inf` dove le domande hanno meno opzioni, `loss = (F.cross_entropy(restricted, target_index, reduction="none") * weight).mean()`. Valutazione ogni 500 step con log-loss ristretta e accuracy per domanda. Salva adapter e tokenizer.
> 7. `training/calibrate.py` — inferenza sul validation set, ottimizzazione di `T_q` per domanda (NLL, `minimize_scalar`), ECE e Brier prima/dopo, reliability diagram PNG con matplotlib. Scrive `temperatures` nel model card.
> 8. `training/backtest.py` — simulatore event-driven barra per barra che usa **la stessa** `DecisionPolicy` e lo **stesso** `RiskManager` del runtime (sez. 4) con un `SimulatedExchange` (fill del limit se `low <= limit` per BUY; bracket TP/SL simulati con la regola conservativa; fee maker/taker da config); decisione alla barra `t`, ordine prezzato a `close[t+F-1]` ed eseguibile dalla barra `t+F` (`entry_first_bar`), SELL del modello a `open[t'+F]`, nella barra di fill solo lo SL, tutto con i kernel di `modules/dryrun/fill_model.py`; ogni fold riparte da `backtest.starting_quote`; nell'ultimo fold nessun ingresso con `label_window_end >= holdout_start - embargo`. Il regime di ogni riga di decisione e' calcolato dal token `atr_1d_pctile_365d` della stessa `flat_matrix` da cui nascono le risposte del modello (`vol_pctile = x[:, FLAT_COLUMNS.index(REGIME_FEATURE)]`, poi `regimes_from_pctile`), mai da `p_regime`: `buy = pol.buy_mask(p_action, vol_pctile)`, e `plan_entry(..., p_be_buffer=policy.p_be_buffer)`. `FlatProbCache.get(...)` restituisce `FlatAnswers(answers, vol_pctile)`; la cache su disco (npz) salva anche `vol_pctile` e la sua chiave include `FLAT_CACHE_VERSION = "v2"`, cosi' le cache precedenti non si rileggono mai (`data/backtest_cache` si puo' cancellare). `p_regime` resta in cache come diagnostica e non decide nulla. La trade list contiene `product, fold, decision_ts, decision_idx, fill_ts, exit_ts, exit_reason, net_ret_bps, pnl_eur, base_size, notional_eur`. Output: equity curve, trade list, Sharpe annualizzato, max drawdown, profit factor, turnover, fee pagate.
> 9. `training/baseline_lgbm.py` — addestra tre LightGBM sugli stessi split usando `flatten_state()`, salva in `models/baseline/`. La calibrazione di `edge` e' `OOF_PLATT_V1` (sez. 3.1.6): `oof_edge_scores` (cross-fitting in `CAL_FOLDS = 4` blocchi con purge ed embargo sul train del fold), Platt su punteggi fuori campione piu' validation, mai sul test; `edge_cal = {scheme, n_rows_oof, n_rows_val, n_eff, n_eff_min, ok}` in `calibration.json` e nel model card (`calibration_scheme`); con `N_eff < CAL_NEFF_MIN = 200` il fold ha `edge_cal.ok = False` e il modello `final` solleva `TrainingError` (exit 1). Firma `fit_subset(samples, member, cfg, tag, allow_degenerate, rho)`. Stampa l'orario di inizio e di fine di ogni sottoinsieme. Il model card dichiara `data.final_train_end` (massimo `label_window_end` delle righe di train e validation del modello `final`, UTC, `None` se vuoto): serve alla regola L5 per provare che una finestra di backtest e' fuori campione (`final_train_end <= inizio - embargo`).
> 10. `training/tune_policy.py` — grid search delle soglie su validation -> `policy.json`. Ogni combinazione di soglie viene valutata eseguendo `training/backtest.py` (stessa `DecisionPolicy`, stesso `RiskManager`, quindi fee floor, EV gate ed EV buffer della sez. 0.3 attivi). E' vietata una simulazione semplificata che salti il RiskManager. Griglia di al piu' 20 combinazioni (`BUY_QUANTILES = (0.80, 0.95)`, `C_MIN_GRID = (0.0, 0.10)`, `P_BE_BUFFER_GRID = (0.00, 0.02, 0.04, 0.06, 0.08)`, `TAU_SELL = 0.50` fisso, `MAX_COMBINATIONS = 20`, `MIN_TRADES = 30`, nessuna dimensione di regime); scelta solo con `n_trades >= 30` e `pnl_eur > 0`; rifiuta un modello il cui model card non ha `calibration_scheme == "OOF_PLATT_V1"`; esiti `untunable_calibration`, `ok`, `no_candidates_regime`, `untunable_ev`, `untunable_pnl` e `val_diag` in `tune_report.json` (sez. 3.1.6). Un sottoinsieme non tarabile scrive `{"untunable": true, "reason": ...}` come `policy.json`.
> 11. `training/evaluate.py` — esegue G0, G1, G2, GE (E1-E5), G3, G4 della sez. 3.4 senza fermarsi al primo fallimento e stampa una tabella con una riga per fold (esito, trade, PnL); i fold non tarabili girano come "nessun ingresso" (PnL 0, non positivo), `fold_status` va in `evaluation.json`, B0 e B2 sono solo report (sez. 3.4); exit code 0 se tutto passa con GE = GO, 5 se GE = INCONCLUSO e il resto passa, 1 altrimenti. Scrive nella directory del modello, prima di `evaluation.json` e quindi prima della registrazione, `oos_trades.jsonl` (un trade OOS per riga: `product, fold, decision_ts, fill_ts, exit_ts, exit_reason, net_ret_bps, pnl_eur, notional_eur, neff_weight`, con il peso E1 del trade) e `oos_spans.json` (`v: chronos.oos_spans.v1`, `label_config_sha256`, `rho`, `block_days`, e i span di test `[start, end)` di ogni fold): entrano nella versione immutabile del registry e alimentano il confronto accoppiato di L5 (`training/oos_compare.py`: OOS comune = intersezione degli span, blocchi circolari di E2 estratti in `BlockGrid`/`BlockedValues` e condivisi, stessi blocchi per i due modelli, Sharpe e drawdown con `equity_sharpe_and_drawdown` del backtest).
> 12. `decider/server.py` + `decider/Dockerfile` (base `nvidia/cuda` runtime + torch CUDA) secondo la sez. 3.3. Schemi request/response pydantic identici a `modules/decision/client.py` (pacchetto condiviso `modules/decision/protocol.py`). Merge dell'adapter in `decider/export.py`.
>
> Test di parita' obbligatorio (`tests/test_train_serve_parity.py`): dato uno stato golden e un modello tiny (`hf-internal-testing/tiny-random-Qwen2` o equivalente), le probabilita' calcolate dal percorso di training (`compute_loss` in eval) e quelle restituite dal server devono coincidere (tolleranza 1e-4).

---

## 4. RISK MANAGEMENT DINAMICO — Execution Layer

### 4.1 Architettura Concettuale

**Prima (v1):** SL = prezzo x 0.98, TP = prezzo x 1.05, per ogni prodotto e ogni regime; quantita' fissa 5; payload di bracket non valido per l'API.

**Dopo (v2):** barriere espresse in unita' ATR (le stesse delle label, sez. 3.1.3), sizing a rischio fisso modulato dalla probabilita' calibrata e dal regime di volatilita' (token di stato), gate di valore atteso netto di fee (sez. 0.3), bracket nativo Coinbase allegato all'ordine di ingresso. Un `RiskManager` per book (sez. 0.6).

```
Decision(BUY, p_edge, regime, conf)            # regime (dal token atr_1d_pctile_365d) gia' ammesso dalla DecisionPolicy
   -> RiskManager[book].plan_entry()
        1. quote: best_bid/best_ask/spread dal LocalOrderBook; product meta (increments, min size) cache 1h
        2. fees: FeeProvider (transaction summary, cache 1h)
        3. A = `atr_14_<atr_tf>` (in prezzo) dello snapshot della barra della decisione, lo stesso valore usato dalla label, mai riletto al momento dell'invio; timeframe dal label_config; entry = floor_to(best_bid, quote_increment) (maker)
                                                       | ceil_to(best_ask, quote_increment) (taker)
        4. SL = entry - k_sl * A ; TP = entry + k_tp * A                  (k da label_config; arrotondati come 4.1.3)
        5. costs = CostModel(entry_mode, fees, spread)                    -> cost_tp, cost_sl (sez. 0.3)
        6. fee floor: tp_bps - cost_tp >= min_net_edge_bps                altrimenti SKIP(fee_floor)
        7. EV gate:   EV(p_edge) > ev_min_bps                              altrimenti SKIP(ev_negative)
           EV buffer: EV(p_edge) >= p_be_buffer * (W + L)                  altrimenti SKIP(ev_buffer)
                      p_floor = L / (W + L) + p_be_buffer                  # = p_breakeven(t) + p_be_buffer, pavimento della decisione
        8. sizing:    vedi 4.1.2 (scale_p con p_floor; cap di copertura, sola lettura del BalanceBook)  eventuale SKIP(below_min_size)
        9. KillSwitch.check(book)                                          SKIP(kill_switch) se attivo
   -> OrderIntent (Decimal, gia' arrotondati agli increment)
   -> ExecutionGateway[book].submit_entry_with_bracket()
        BalanceGuard.authorize(BUY limit)                  SKIP(insufficient_balance | balance_stale | kill_switch)
        maker: limit_limit_gtc post_only=true al best_bid ; taker: limit_limit_gtc al best_ask (marketable)
        attesa dello stato terminale: FILLED, oppure timeout (maker 30 min, taker 20 s) -> cancel residuo -> conferma
        filled > 0: LONG dal primo fill; al terminale BalanceGuard.authorize(SELL bracket, base_size = filled posseduto)
                    -> trigger_bracket_gtc standalone (sez. 4.1.6)
   -> fills -> PositionLedger[book]
   -> Watchdog: posizione con bracket: prezzo oltre lo SL di 1 x ATR_1h -> uscita d'emergenza
                posizione senza bracket (finestra d'ingresso): prezzo <= SL -> uscita d'emergenza
```

Ingresso maker: se l'exchange rifiuta il post-only perche' il prezzo nel frattempo incrocerebbe il book, l'intent viene ricalcolato al minuto successivo (nessun retry immediato). Un solo ingresso pendente per (book, prodotto) (sez. 0.6).

Decision(SELL) su posizione LONG: cancella il bracket attivo e attende la conferma terminale (nessun rilascio ottimistico della base), poi chiude con limit marketable `sell` a `best_bid` con timeout e fallback a `market_market_ioc`. Ogni ordine della sequenza passa dal `BalanceGuard` (sez. 4.1.6).

#### 4.1.1 Dove interviene "la volatilita' calcolata dall'AI"

L'adattivita' arriva da due fonti, con ruoli separati per mantenere la coerenza con le label:

1. **ATR in tempo reale** (deterministico): le distanze di SL/TP scalano automaticamente con la volatilita' corrente, perche' sono `k * ATR`. I moltiplicatori `k_sl`, `k_tp` sono **fissi per versione del modello**: sono quelli con cui `edge.noul` e' stato addestrato. Cambiarli a runtime renderebbe `p_edge` non calibrata.
2. **Regime di volatilita' dal token di stato** (`atr_1d_pctile_365d` tagliato a 5/35/65/95, sez. 2.1.2; non la risposta `vol_regime` del modello, che e' solo diagnostica): decide **se** entrare (veto, solo nella `DecisionPolicy`) e seleziona il moltiplicatore di sizing (solo qui). Non sposta le barriere.

| Regime | Percentile ATR 1d | Ingresso (`ENTRY_REGIMES = (1, 2, 3)`, costante del codice) | `m_reg` (`risk.regime_multipliers`) |
|--------|-------------------|-------------------------------------------------------------|-------------------------------------|
| 0 very_low | < 5 | Vietato: barriere troppo strette rispetto alle fee | n/a (non arriva al RiskManager) |
| 1 low | 5..34 | Permesso | 1.0 |
| 2 normal | 35..64 | Permesso | 1.0 |
| 3 high | 65..94 | Permesso | 1.0 |
| 4 extreme | >= 95 | Vietato: rischio di gap oltre lo stop | n/a (non arriva al RiskManager) |

Il default di `risk.regime_multipliers` e' `{1: 1.0, 2: 1.0, 3: 1.0}`: il default e' neutro, quindi il regime decide l'ingresso ma non modula il rischio. Le chiavi 0 e 4 restano assenti, e il `RiskManager` non si avvia se manca una chiave di `ENTRY_REGIMES` (sez. 2.1.2).

Estensione v2.1 (opzionale): `k` dipendenti dal regime, ma solo con label generate per regime (un training set per tabella `k(regime)`) e una domanda `edge` per tabella.

#### 4.1.2 Position sizing

```
equity          = BalanceBook.total(quote) + sum(mark_to_market posizioni del book)   # in memoria, live e dry (sez. 4.1.6)
risk_frac       = risk.risk_per_trade                                        # es. 0.005 = 0.5% equity
p_floor         = p_breakeven(t) + p_be_buffer                               # = L / (W + L) + p_be_buffer, sez. 0.3
scale_p         = clip((p_edge - p_floor) / (p_edge_full - p_floor), 0, 1)   # se p_floor >= p_edge_full: 1 se p_edge >= p_floor, altrimenti 0
risk_eur        = equity * risk_frac * scale_p * m_reg
loss_per_unit   = (entry - SL) + entry * cost_sl / 1e4                       # perdita per unita' a SL, fee e slippage inclusi
size_raw        = risk_eur / loss_per_unit
f_cov           = coverage_fee_rate(fees, fallback_fees)                      # sez. 4.1.6, non e' un costo
notional_cap    = min(equity * max_position_frac, max_order_quote) / entry
cover_cap       = max_base_for_quote(BalanceBook.free(quote), entry, f_cov, quote_increment, base_increment)
size_cap        = floor_to(min(notional_cap, cover_cap), base_increment)
size            = floor_to_increment(min(size_raw, size_cap), base_increment)
min_size        = max(base_min_size, ceil_to_increment(quote_min_size / entry, base_increment))
if size < min_size:
    if min_size * loss_per_unit <= equity * risk_frac and min_size <= size_cap:
        size = min_size                                                      # "min-size bump": rischio comunque nel budget
    else:
        SKIP(reason="below_min_size")
```

`entry` e' il prezzo limite gia' arrotondato (sez. 4.1.3). Il sizing usa le formule di copertura della sez. 4.1.6 solo per non produrre ordini che il guard rifiuterebbe; l'autorita' finale resta `BalanceGuard.authorize()`. `max_order_quote` (config `risk`, default 50 EUR) e' un cap assoluto per ordine che sale con la rampa (sez. 5.1.7).

`compute_size` riceve `p_floor` come suo parametro `p_edge_min`, cioe' il pavimento della singola decisione (il nome indica solo il ruolo nella formula; non e' una soglia della policy). Se `p_floor >= p_edge_full` (`p_edge_full <= p_edge_min` nella funzione) non c'e' piu' un errore: `scale_p` vale 1 se `p_edge >= p_floor` e 0 altrimenti, cioe' ogni trade che passa il gate ha "convinzione piena" per definizione e il rischio resta comunque nel budget `risk_per_trade`. `plan_entry` riceve `p_be_buffer` come keyword obbligatoria (`policy.p_be_buffer`) e lo riporta nel blocco `risk` dell'`OrderIntent`.

Esempio (sez. 0.3, book da 100 EUR, `p_breakeven` = 1117 / 2837 = 0.3937, `p_be_buffer` = 0.05, quindi `p_floor` = 0.4437; `p_edge_full` = 0.60, `p_edge` = 0.52, regime normal con `m_reg` = 1.0): `scale_p` = (0.52 - 0.4437) / (0.60 - 0.4437) = 0.49, `risk_eur` = 0.24 EUR, `loss_per_unit` = 8.46 + 1.81 = 10.27 EUR/SOL, `size` = 0.023 SOL (2.12 EUR di controvalore, sopra il minimo di 1 EUR). Con capitali piccoli e stop larghi (ATR giornaliero) le posizioni sono di pochi euro: e' voluto, e' la fase R0/R1 della sez. 5.1.7.

#### 4.1.3 Arrotondamenti (Decimal, mai float in questo layer)

Ordine: il prezzo limite d'ingresso si arrotonda per primo; SL, TP, costi, sizing e copertura si calcolano sul prezzo arrotondato.

| Valore | Regola | Motivo |
|--------|--------|--------|
| `base_size` | floor a `base_increment` | Mai superare il rischio pianificato. |
| entry limit BUY maker | floor a `quote_increment` | Non incrocia il book (post-only). |
| entry limit BUY taker | ceil a `quote_increment` | Resta marketable. |
| `limit_price` TP | floor a `quote_increment` | Leggermente piu' facile da raggiungere, non oltre il piano. |
| `stop_trigger_price` SL | ceil a `quote_increment` | Lo stop non va mai piu' lontano del pianificato. |

Vincoli di validazione prima dell'invio: `SL < entry < TP`; `(TP-entry) >= 2 * quote_increment`; `(entry-SL) >= 2 * quote_increment`.

#### 4.1.4 Kill switch e guardie

| Guardia | Ambito (sez. 0.6) | Condizione | Azione |
|---------|-------------------|-----------|--------|
| Daily loss | Book | PnL realizzato + non realizzato del giorno UTC <= -`max_daily_loss_frac` x equity di apertura | Blocca nuovi ingressi del book fino a 00:00 UTC. |
| Loss streak | Book | `max_consecutive_losses` stop consecutivi | Pausa del book per 48 h. |
| Exposure | Book | Esposizione totale > `max_total_exposure_frac` | Nessun nuovo ingresso del book. |
| Stale data | Globale | Ultima barra 1m pubblicata con `now - ts_bar_close > stale_bar_s` (default 270 s; validator: >= `market.bars.rest_max_delay_s` + 90 e > `market.bars.decision_max_lag_s`), oppure ultimo ticker piu' vecchio di `stale_data_s` (90 s) | Nessun ingresso in nessun book; i bracket esistenti restano. |
| Decider health | Globale | > 20% di decisioni in fallback nell'ultima ora, oppure tasso di incoerenza action/edge > soglia | Passa a baseline o HOLD-only (config). |
| Drift | Globale | PSI > 0.25 su `edge.noul` o sulle feature principali rispetto alla finestra di training | Alert + HOLD-only. Oggi senza scrittore: `risk.kill_switch.psi_threshold` e' solo la soglia della diagnostica v1; il collegamento a PSI-v2 e' una decisione separata, per prodotto (conflitto P1f). |
| Watchdog SL | Per posizione | Posizione con bracket: `last_price <= SL - 1 x ATR_1h` e posizione ancora aperta dopo 10 s. Posizione senza bracket (finestra d'ingresso o bracket rifiutato): `last_price <= SL`. | Cancella bracket, `market_market_ioc` sell (in dry: walk dei bid). La doc Coinbase dichiara che l'esecuzione della protezione downside non e' garantita in alta volatilita'. |
| Coverage breach | Book | `reservation_overdraw`, `hold_exceeds_reservation`, oppure >= 3 `balance_drift` in 24 h (sez. 4.1.6) | Blocca i BUY del book fino a reset manuale (`reset_guard` dal canale locale, sez. 6.4.3); le uscite restano attive. |
| Manual pause | Book o globale (`*`) | Comando `pause_entries` o `flatten_all` (sez. 6.4) | Blocca i nuovi ingressi del book (o di tutti i book); uscite, bracket, watchdog e uscita per orizzonte restano attivi. Si rimuove solo dal canale locale (`resume_entries`, sez. 6.4.3), mai dalla GUI. Non conta come guardia scattata per bug in G-DRY-OPS. |

#### 4.1.5 Riconciliazione

- Avvio: legge account (`get_accounts`: `available_balance`, `hold`), ordini aperti (`list_orders` con `order_status=OPEN`) e fill recenti; ricostruisce il ledger e il `BalanceBook`: per ogni ordine Chronos aperto ripristina la riserva (`BUY`: `required_quote(limit, leaves_size)`; `SELL`: `leaves_size`), poi applica lo snapshot. Un ordine con `client_order_id` di Chronos senza record locale viene importato; una posizione senza bracket attivo riceve un bracket standalone `trigger_bracket_gtc` sell con `base_size = floor_to(min(base_qty, sellable))`, oppure diventa dust se sotto il minimo vendibile.
- Runtime ordini: canale WS `user` per gli aggiornamenti degli ordini e dei fill (aggiornano subito `BalanceBook`); `get_order` ogni `entry_poll_s` per l'ingresso non terminale; polling REST di sicurezza ogni `reconcile_interval_s`.
- Runtime saldi: `LiveBalanceFeed` ogni `balance_refresh_s` (default 30 s) legge saldi e ordini aperti; lo snapshot viene scartato se il `BalanceBook` ha cambiato `epoch` durante la lettura; se accettato, `free = max(available - (reserved - H_credit), 0)` per valuta, con `H_credit = min(hold, reserved)` senza ordini estranei sulla valuta e `0` altrimenti. Drift fra totale locale e totale dell'exchange oltre `max(2 x increment, 1e-4 x totale)`: allarme `balance_drift`, il valore dell'exchange prevale. Il canale WS `user` non porta saldi spot.
- Idempotenza: `client_order_id = uuid5(NAMESPACE_CHRONOS, f"{decision_id}:{leg}")`. Un retry di rete non crea mai un secondo ordine; un errore ambiguo lascia la riserva in stato UNKNOWN finche' la riconciliazione non trova o esclude l'ordine (rilascio dopo 2 cicli senza riscontro).

#### 4.1.6 Copertura spot (vincolo C11)

Componenti (`modules/execution/`): `BalanceBook` (saldi in memoria per book), `BalanceGuard` (unica autorita' di copertura), `CoverageTicket` (capacita' d'invio), `SpotRegistry` e `build_order_body` (divieto di leva). Formule in `modules/risk/costs.py` (sez. 0.3). I tipi condivisi `OrderSpec` e `ProductMeta` stanno in `modules/execution/orders.py`.

Formule (Decimal):

```
f_cov          = max(f_t corrente, f_t fallback, 120 bps)            # tetto di prenotazione, non un costo
required_quote = ceil_to(limit_price * base_size * (1 + f_cov), quote_increment) + quote_increment     # BUY
required_base  = base_size                                                                           # SELL
sellable       = min(position.base_qty - reserved_sell_base(book, product), free(base))
BUY ammesso  se  free(quote) >= required_quote ; SELL ammessa se sellable >= base_size
```

Regole:
1. Ogni ordine, live o shadow, e' inviabile solo con un `CoverageTicket` emesso da `BalanceGuard.authorize()`, che verifica e prenota nello stesso passo sincrono (nessuna chiamata di rete, nessun `await`). Il body JSON e' costruito dal ticket con `build_order_body`.
2. BUY: solo `limit_limit_gtc`. Rifiutato se il saldo e' insufficiente (`insufficient_balance`), se l'ultimo snapshot dei saldi ha piu' di 120 s (`balance_stale`, solo live) o se `coverage_breach` e' attivo. SELL: `limit_limit_gtc`, `trigger_bracket_gtc`, `market_market_ioc`, solo su base posseduta da Chronos; mai bloccata dall'eta' dello snapshot.
3. Riserve: al fill BUY si consuma `p*q + fee`, al terminale si rilascia il residuo; al fill SELL si consuma `q`. Nessun rilascio ottimistico: una cancellazione libera la riserva solo alla conferma terminale dell'exchange.
4. Bracket: sempre standalone, piazzato dopo lo stato terminale dell'ingresso con `base_size = floor_to(min(filled, sellable), base_increment)`. Sotto il minimo vendibile (`base_min_size`, `quote_min_size`) la quantita' resta come dust e si fonde nel bracket dell'ingresso successivo sullo stesso prodotto.
5. Rischio massimo di un ordine = sua riserva: la spesa d'ingresso non supera `required_quote` e il valore d'uscita non e' mai negativo.
6. Non disattivabile: nessuna chiave di config; stesso codice in live, dry, backtest (`VirtualAccount` compone un `BalanceBook`). Violazioni di whitelist o prodotto non spot: `SpotViolation`, exit code 4.
7. Verifiche continue delle ipotesi sull'exchange: `hold > reserved` senza ordini estranei, riserva consumata oltre il prenotato, drift ripetuti -> `coverage_breach` (sez. 4.1.4).
8. `BalanceBook.apply_fill` e' idempotente per `trade_id` (lo stesso fill arriva dal WS e dal polling); un fill per un ordine senza riserva non modifica i saldi e segnala `reservation_overdraw`.
9. Il collegamento e' imposto da test (`tests/test_c11_wiring.py`): solo `exchange.py`, `user_stream.py`, `ws_feed.py`, `backfill.py` importano l'SDK Coinbase, i metodi d'ordine dell'SDK solo in `exchange.py`, gateway senza `BalanceGuard` obbligatorio non passano. Il vecchio `modules/order_execution.py` (v1) e' disattivato e viene cancellato quando nasce `gateway.py`.

### 4.2 Struttura dei Dati / Schema JSON

**OrderIntent** (interno, prodotto dal RiskManager):

```json
{
  "intent_id": "01JAB3...ULID",
  "decision_id": "01JAB3...ULID",
  "product_id": "SOL-EUR",
  "side": "BUY",
  "book": "champion",
  "entry": {"type": "limit_post_only", "limit_price": "92.03", "time_in_force_s": 1800},
  "base_size": "0.023",
  "bracket": {"take_profit": "108.96", "stop_loss": "83.57",
              "k_tp": 4.0, "k_sl": 2.0, "atr_tf": "1d", "atr": "4.2334"},
  "risk": {"equity_eur": "100.00", "risk_eur": "0.24", "p_edge": 0.52, "scale_p": 0.49,
           "regime": 2, "m_reg": 1.0, "ev_bps": 358, "cost_tp_bps": 120, "cost_sl_bps": 197,
           "p_breakeven": 0.39, "p_be_buffer": 0.05, "spread_bps": 3, "min_size_bump": false},
  "created_at": "2026-10-17T14:03:01.120Z"
}
```

Tutti i numeri sono quelli dell'esempio di riferimento della sez. 0.3 e della sez. 4.1.2.

**Payload Coinbase — ingresso** (`POST /api/v3/brokerage/orders`):

```json
{
  "client_order_id": "6f1c2a8e-5b1e-5c9a-9d1f-1f0b7c3a2e11",
  "product_id": "SOL-EUR",
  "side": "BUY",
  "order_configuration": {
    "limit_limit_gtc": {"base_size": "0.024", "limit_price": "92.03", "post_only": true}
  }
}
```

Con `entry_mode: taker` cambiano solo `limit_price` (best ask, arrotondato per eccesso) e `post_only: false`.

L'attached bracket (`attached_order_configuration`) non e' usato in v2.4: la sua semantica su fill parziali non e' verificata e un bracket piu' grande della quantita' fillata potrebbe vendere base non di Chronos (C11). Si potra' reintrodurre solo con una nuova versione della spec, dopo verifica documentata del comportamento su fill parziale.

**Validazione dei payload:** la sandbox Advanced Trade (`api-sandbox.coinbase.com`) restituisce solo risposte statiche predefinite e non valida i payload, quindi non serve a verificare l'accettazione dei payload. La validazione avviene in due modi: (1) `POST /api/v3/brokerage/orders/preview` in produzione con lo stesso body, che non crea ordini (verificare quale permesso della API key richiede: se richiede `trade`, in dry mode resta disattivato, sez. 5); (2) il primo ordine reale della fase live-micro (sez. 5.1.7).

**Payload Coinbase — bracket standalone** (percorso unico e riconciliazione):

```json
{
  "client_order_id": "b2d9...",
  "product_id": "SOL-EUR",
  "side": "SELL",
  "order_configuration": {
    "trigger_bracket_gtc": {"base_size": "0.024", "limit_price": "108.96", "stop_trigger_price": "83.57"}
  }
}
```

**PositionLedger** (SQLite; stesso schema in `state/chronos_live.db` e `state/chronos_dry.db`, sez. 0.5–0.6):

```sql
CREATE TABLE IF NOT EXISTS positions (
  book TEXT NOT NULL, product_id TEXT NOT NULL, side TEXT NOT NULL CHECK (side IN ('FLAT','LONG')),
  base_qty TEXT NOT NULL, avg_entry TEXT, opened_at TEXT, entry_decision_id TEXT,
  bracket_order_id TEXT, tp TEXT, sl TEXT, k_tp REAL, k_sl REAL, atr_at_entry TEXT,
  dust_qty TEXT NOT NULL DEFAULT '0', dust_cost TEXT NOT NULL DEFAULT '0',
  PRIMARY KEY (book, product_id)
);
CREATE TABLE IF NOT EXISTS orders (
  client_order_id TEXT PRIMARY KEY, book TEXT NOT NULL, exchange_order_id TEXT, intent_id TEXT, leg TEXT,
  product_id TEXT, side TEXT, status TEXT, payload TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS fills (
  trade_id TEXT PRIMARY KEY, book TEXT NOT NULL, client_order_id TEXT, product_id TEXT, side TEXT,
  price TEXT, size TEXT, fee TEXT, liquidity TEXT, trade_time TEXT
);
CREATE TABLE IF NOT EXISTS kill_switch_state (
  book TEXT NOT NULL, guard TEXT NOT NULL, tripped_at TEXT, until TEXT, reason TEXT,
  PRIMARY KEY (book, guard)
);
```

Nel DB dry i `trade_id` dei fill simulati hanno il prefisso `sim-`.

Prezzi e quantita' sono salvati come `TEXT` (stringhe Decimal) per non perdere precisione.

**Config YAML**:

```yaml
risk:
  # k_tp, k_sl, atr_tf, entry_mode, horizon: sempre dal label_config del modello champion, mai qui
  risk_per_trade: 0.005
  p_edge_full: 0.60
  max_position_frac: 0.20
  max_order_quote: "50.00"                # cap assoluto per ordine (sez. 4.1.2); sale con la rampa
  max_total_exposure_frac: 0.50
  min_net_edge_bps: 30
  ev_min_bps: 0
  slippage_buffer_bps: 5
  regime_multipliers: {"1": 1.0, "2": 1.0, "3": 1.0}    # solo regimi ammessi (ENTRY_REGIMES, obbligatori); il veto e' nella DecisionPolicy
  kill_switch:
    max_daily_loss_frac: 0.02
    max_consecutive_losses: 4
    loss_streak_pause_h: 48
    stale_data_s: 90                  # solo ticker
    stale_bar_s: 270                  # barre 1m (pubblicate a t + S); = S + 150, > decision_max_lag_s
    max_fallback_ratio_1h: 0.20
    psi_threshold: 0.25                # soglia della diagnostica PSI v1 in L0; collegata a nessuna guardia (P1f)
  watchdog:
    sl_breach_atr_1h: 1.0
    grace_s: 10
execution:
  mode: "dry"                             # dry | live  (sez. 5; live richiede CHRONOS_LIVE_CONFIRM)
  entry_timeout_s: {maker: 1800, taker: 20}
  exit_timeout_s: 15
  reconcile_interval_s: 60
  fee_refresh_s: 3600
  product_meta_refresh_s: 3600
  balance_refresh_s: 30                   # <= 40 (BALANCE_MAX_AGE_S / 3), sez. 4.1.6
  entry_poll_s: 5
  cancel_confirm_timeout_s: 10
```

### 4.3 Linee Guida per l'implementazione in Sonnet 5.5

> PROMPT — Risk Manager ed Execution Gateway
>
> Implementa `modules/risk/`, `modules/execution/`, `modules/ledger/` e l'orchestratore `modules/app.py` di Chronos v2. Python 3.12, asyncio, pydantic v2, `decimal.Decimal` con contesto a 28 cifre per **tutti** i prezzi e le size di questo layer (conversione da float solo tramite `Decimal(str(x))`).
>
> 1. `modules/config.py` — sostituisce `modules/utils.load_config`: `pydantic-settings` con modelli `SentimentConfig`, `DecisionConfig`, `RiskConfig`, `ExecutionConfig`, `CoinbaseConfig`; i segreti vengono **solo** da variabili d'ambiente (`COINBASE_API_KEY`, `COINBASE_API_SECRET`, `DECIDER_API_KEY`, `CRYPTOPANIC_TOKEN`), mai dal YAML. Validazione all'avvio; errori di config = exit code 2 con messaggio leggibile.
> 2. `modules/execution/exchange.py` — `CoinbaseExchange`, wrapper async attorno a `coinbase.rest.RESTClient` (chiamate sincrone in `asyncio.to_thread`). Metodi: `get_product(product_id) -> ProductMeta` (`base_increment`, `quote_increment`, `base_min_size`, `quote_min_size`), `get_fee_rates() -> FeeRates` (da `get_transaction_summary`, campi del fee tier maker/taker), `get_best_bid_ask(product_id)`, `create_order(ticket: CoverageTicket) -> CreateOrderResult`, `cancel_orders(order_ids)`, `list_open_orders()`, `get_fills(order_id)`, `get_accounts()`, `get_api_key_permissions()`. I metodi del `RESTClient` usabili sono limitati all'allowlist della sez. 4.1.6. Gestisci `success: false` + `error_response` trasformandoli in `OrderRejected(reason, message)`. Rate limiting con `aiolimiter.AsyncLimiter` (configurabile, default 20 req/s privati).
> 3. `modules/execution/rounding.py` — `floor_to(x, inc)`, `ceil_to(x, inc)` con `Decimal.quantize`/divisione intera; test property-based con `hypothesis` (il risultato e' multiplo dell'increment ed entro un increment dall'input, nella direzione corretta).
> 4. `modules/risk/sizing.py` — funzione pura `compute_size(...) -> SizingResult | SkipReason` secondo la sez. 4.1.2. Test con `hypothesis`: la perdita a SL (incluse le fee) non supera mai `risk_eur` + un increment. Con `p_edge_full <= p_edge_min` (pavimento della decisione) `scale_p` vale 1 se `p_edge >= p_edge_min`, altrimenti 0 (nessun errore).
> 5. `modules/risk/risk_manager.py` — `RiskManager.plan_entry(decision, market, position, ..., p_be_buffer) -> OrderIntent | Skip` implementa i passi 1–9 della sez. 4.1 (EV gate e EV buffer con `p_be_buffer`, skip `ev_negative` / `ev_buffer`, `p_floor` al sizing) e l'arrotondamento della sez. 4.1.3; `PolicyContractError` per un BUY con regime `None` o assente da `risk.regime_multipliers`, e `ValueError` all'avvio se `risk.regime_multipliers` non copre `ENTRY_REGIMES`. Un'istanza per book (sez. 0.6). Legge `k_tp`, `k_sl`, `atr_tf`, `entry_mode`, `horizon_min` dal `label_config` del modello champion caricato all'avvio (e ricaricato allo swap notturno della sez. 5.1.6); se il `label_config` manca, rifiuta di avviarsi. Tutti i costi passano da `modules/risk/costs.py` (`CostModel`, `FeeProvider`, sez. 0.3): scrivi questo file per primo, con test che riproducono esattamente i numeri dell'esempio di riferimento della sez. 0.3 e della sez. 4.1.2. `plan_exit(decision, position) -> ExitIntent`. Ogni `Skip` ha un `reason` enumerato e viene loggato nel `DecisionRecord`.
> 6. `modules/risk/kill_switch.py` — `KillSwitch` con le guardie della sez. 4.1.4, separate in `AccountGuards` (un'istanza per book) e `SystemGuards` (una sola istanza, condivisa). Stato persistito nella tabella `kill_switch_state` del DB della modalita' del book (sez. 0.6), cosi' sopravvive al riavvio del container; `check(book) -> KillState` combina le guardie del book con quelle globali.
> 7. `modules/execution/gateway.py` — `ExecutionGateway(Protocol)` con `submit_entry_with_bracket`, `submit_exit`, `emergency_exit`, `cancel`; questa sezione implementa `LiveExecutionGateway`, la sez. 5 implementa `ShadowExecutionGateway` con la stessa interfaccia. `LiveExecutionGateway`:
>    - `submit_entry_with_bracket(intent)`: `BalanceGuard.authorize` (BUY), invio, registrazione in `orders`; attesa dello stato terminale (WS `user` + `get_order` ogni `entry_poll_s`); timeout `entry_timeout_s` -> cancel del residuo con conferma; al terminale, se `filled > 0`, `BalanceGuard.authorize` (SELL bracket) e bracket standalone `trigger_bracket_gtc` con `base_size` = quantita' fillata posseduta, oppure dust (sez. 4.1.6);
>    - `submit_exit(intent)`: cancella il bracket attivo, attende la conferma della cancellazione (o lo stato terminale); se il bracket risulta FILLED non vende; ogni ordine della sequenza passa da `BalanceGuard`; poi limit marketable sell a `best_bid`, timeout, fallback `market_market_ioc`;
>    - `emergency_exit(product_id)` usato dal watchdog.
> 8. `modules/execution/user_stream.py` — WS canale `user` (autenticato) -> eventi `OrderUpdate` -> aggiornamento `orders`, `fills`, `positions`. Bridge thread->asyncio come per il market stream.
> 9. `modules/execution/reconciler.py` — `Reconciler.on_startup()` e loop periodico secondo la sez. 4.1.5.
> 10. `modules/execution/watchdog.py` — task che a ogni ticker verifica la condizione della sez. 4.1.4 per ogni posizione LONG.
> 11. `modules/ledger/positions.py` — `PositionLedger` su `aiosqlite`, DDL della sez. 4.2, tutte le mutazioni in transazione; `view(product_id) -> PositionView` usato dallo StateBuilder.
> 12. `modules/app.py` — orchestratore: costruisce i servizi, `async with asyncio.TaskGroup() as tg:` avvia market stream, bar aggregator, sentiment service, decision engine, risk/execution consumer, user stream, reconciler, watchdog, metriche. Gestione `SIGTERM`/`SIGINT` (Docker stop): smette di aprire posizioni, attende le operazioni in volo fino a 10 s, chiude WS e DB; **non** cancella i bracket esistenti (le posizioni restano protette lato exchange). `main.py` diventa `asyncio.run(app.main())`.
> 13. `docker-compose.yml`: dichiara il named volume `chronos-state` e montalo su `/app/state` nei servizi `chronos`, `test` e `dev` (sez. 0.5); porta il `Dockerfile` a `python:3.12-slim`. Aggiungi il servizio `decider` con `profiles: [gpu]` (non parte di default; `build: ./decider`, `deploy.resources.reservations.devices: [{driver: nvidia, count: 1, capabilities: [gpu]}]`, porta esposta solo sulla rete interna, volume `./models/decider:/models:ro`, healthcheck su `/healthz`). `chronos` **non** dichiara `depends_on` verso `decider`: se `decision.backend != baseline` e il decider non risponde, il `DecisionEngine` usa il fallback baseline (sez. 2.1.2). Sostituisci l'healthcheck fittizio attuale di `chronos` con uno che verifica l'eta' dell'ultimo heartbeat scritto su file (`data/heartbeat`) < 120 s. Su Windows il servizio GPU richiede Docker Desktop con backend WSL2 e driver NVIDIA recenti.
> 14. `modules/execution/balance.py`, `balance_guard.py`, `spot.py`, `balance_feed.py` secondo la sez. 4.1.6, con i test property-based: nessun ordine con `required > available`, riserve e saldi mai negativi, conservazione di `free + reserved`.
> 15. `modules/telemetry/` (`schemas`, `StatusWriter`, `EventLog`, `DbSnapshotter`), la tabella `closed_trades` del `PositionLedger` e gli eventi `decision_outcome` e `coverage_refused` secondo la sez. 6.3 (D4-D8, D12-D14): servono al centro di controllo ma sono parte dell'agente, girano anche senza GUI e nessun loro errore puo' propagarsi al `TaskGroup` (sez. 6.3.5).
> 16. `KillSwitch`: guardia `manual_pause` (sez. 4.1.4) ed esito di chiusura `MANUAL`, neutro per la loss streak (sez. 6.4.4).
>
> Test:
> - `SimulatedExchange` (condiviso con `training/backtest.py`) per test end-to-end: decisione BUY -> intent -> payload -> fill simulato -> posizione LONG con bracket -> TP colpito -> posizione FLAT, PnL corretto al centesimo con fee;
> - payload snapshot test: il JSON prodotto per l'intent della sez. 4.2 coincide con il fixture;
> - kill switch: daily loss raggiunta blocca nuovi ingressi ma non le uscite;
> - reconciler: posizione presente su exchange senza bracket -> viene creato il bracket standalone;
> - test marcato `@pytest.mark.preview` (eseguito solo manualmente) che invia il payload della sez. 4.2 a `orders/preview` in produzione, verifica `errs` vuoto e registra nel log la risposta; Non usare la sandbox per questo: e' statica.

---

## 5. MODULO DRY RUN — Shadow Execution e apprendimento sul campo

### 5.1 Architettura Concettuale

#### 5.1.1 Principio: stessa produzione, unico punto di divergenza

Il dry run **non** e' un backtest e non e' un processo separato. E' il processo di produzione completo (stesso container, stessi stream WebSocket reali, stesso sentiment, stesso decider, stessa policy, stesso RiskManager, stesso KillSwitch, stesso watchdog, stesso ledger), in cui l'unico componente sostituito per dependency injection e' l'`ExecutionGateway`:

```
                       execution.mode
                 +-----------+-----------+
                 |                       |
               "live"                  "dry"
                 |                       |
   LiveExecutionGateway         ShadowExecutionGateway
   - Coinbase REST (trade)      - FillSimulator sul LocalOrderBook reale
   - user WS channel            - BracketSimulator sui market_trades reali
   - account reale              - VirtualAccount (capitale virtuale, fee reali)
                 |                       |
                 +----------+------------+
                            v
               PositionLedger / DecisionRecord / Report
               (stessi schemi; DB separato per modalita')
```

Conseguenza: tutto cio' che funziona in dry funziona in live, tranne cio' che dipende dalla controparte reale (latenza effettiva di accettazione, coda dei limit, rifiuti dell'exchange). Queste differenze vengono misurate nella fase live-micro (sez. 5.1.7) con lo shadow twin.

**Sicurezza (difesa in profondita'):**
1. In dry si usa una API key CDP con **solo permesso View** (nessun permesso Trade/Transfer): il bot non puo' fisicamente piazzare ordini anche in presenza di un bug.
2. `CoinbaseExchange(read_only=True)`: ogni metodo di scrittura (`create_order`, `cancel_orders`, `preview_order`) solleva `DryRunViolation` prima di qualunque chiamata di rete. Un `DryRunViolation` e' un errore fatale: il processo termina con exit code 3.
3. In dry il `LiveExecutionGateway` non viene istanziato.
4. Il passaggio a `live` richiede **entrambe** le condizioni: `execution.mode: live` nella config **e** la variabile d'ambiente `CHRONOS_LIVE_CONFIRM=I_ACCEPT_REAL_ORDERS`. Se ne manca una, il processo parte in dry e logga un warning.

#### 5.1.2 Cosa "sarebbe successo": oltre il tick successivo

Il tick successivo basta solo per stimare il **prezzo di ingresso**. Per sapere se il trade avrebbe guadagnato, il simulatore deve seguire la posizione virtuale tick per tick finche' il bracket non si chiude (TP, SL, uscita del modello, watchdog, orizzonte). Ciclo di vita di un ordine shadow:

```
t0      Decision(BUY) -> RiskManager -> OrderIntent (identico al live, Decimal, increment, min size)
t0      ShadowGateway.submit_entry_with_bracket(intent)
t0+L    primo evento level2/ticker successivo a t0 + latency_ms (latenza simulata)
          -> FillSimulator.walk_book(asks, size, limit)  => fill marketable (VWAP sui livelli), fee taker
          -> residuo non fillato resta "resting" al limit
t0+L..  per ogni market_trade: il resting si filla solo se il trade passa ATTRAVERSO il limit (prezzo < limit
          per BUY), quantita' massima = queue_haircut x volume scambiato oltre il limit; fee maker
t0+T    entry_timeout_s: cancella il residuo; se fill = 0 -> ordine scaduto, nessuna posizione
fill    VirtualPosition(LONG) dal primo fill; VirtualBracket(TP, SL) creato allo stato terminale dell'ingresso con
        size = quantita' fillata (stessa regola del live, sez. 4.1.6); prima del bracket vale lo stop virtuale allo SL
...     per ogni market_trade / ticker: BracketSimulator.evaluate()
          TP: si filla se un trade stampa > TP (strettamente), haircut sulla quantita', fee maker
          SL: trigger se last_trade <= stop_trigger_price -> uscita modellata come vendita a mercato:
              walk_book(bids) al momento del trigger + stop_extra_slip_bps, fee taker
          stesso evento che soddisfa TP e SL -> SL (conservativo)
exit    trade virtuale chiuso: PnL netto di fee e slippage -> ledger dry -> report
```

#### 5.1.3 Modello di fill (unica fonte di verita')

Il `FillModel` e' **lo stesso oggetto** usato dal dry run (a livello di tick), dal backtest della sez. 3.5 (approssimazione a barre) e dai test e2e della sez. 4.3. Regole:

| Evento | Regola dry (tick-level) | Approssimazione backtest (bar-level) | Fee |
|--------|------------------------|--------------------------------------|-----|
| Ingresso taker, parte marketable | Walk degli ask al primo snapshot del book dopo `t0 + latency_ms`, fino al prezzo limite. | `open[t+F] * (1 + spread/2 + slip_bps)`, con F = `entry_first_bar` | taker |
| Ingresso maker (post-only, intero ordine) e residuo taker | Fill solo su trade con prezzo < limite (strettamente); `qty <= queue_haircut * volume_oltre_limite`. Se al momento dell'arrivo il limite incrocerebbe il book, l'ordine e' rifiutato come farebbe l'exchange con `post_only`. | Limite `close[t+F-1] * (1 - spread/2)`; fill se `low[k] < limit` per k in `[t+F, t+F+T_in-1]`, haircut sul volume della barra. | maker |
| Timeout ingresso | Cancella il residuo. | Uguale. | — |
| TP (limit sell) | Trade con prezzo > TP; haircut sulla quantita'. | `high > TP`. | maker |
| SL (stop) | Trigger su `last <= stop`; walk dei bid + `stop_extra_slip_bps`. | `min(stop, open) * (1 - (spread/2 + slip_bps + stop_extra_slip_bps))`; nella barra di fill vale solo lo SL (watchdog senza bracket). | taker |
| TP e SL nello stesso evento o barra | SL. | SL. | — |
| Uscita del modello (SELL) / watchdog | Walk dei bid. | `open[t+F] * (1 - spread/2 - slip_bps)` per una decisione presa a `t`. | taker |
| Gap di dati (WS disconnesso con posizione aperta) | Alla riconnessione si scaricano le candele 1m del gap via REST; se `low <= SL` nel gap, SL eseguito a `min(SL, open della barra di trigger)`; trade marcato `gap_resolved=true`. | n/a | taker |

Parametri iniziali, conservativi e configurabili: `latency_ms = 300`, `queue_haircut = 0.25`, `stop_extra_slip_bps = 10`. Le fee sono quelle **reali** del tier dell'account, lette con `get_transaction_summary` (lettura consentita dalla key View) e aggiornate ogni ora; se la chiamata fallisce si usano i valori di config, piu' alti per prudenza.

#### 5.1.4 VirtualAccount e shadow book paralleli

Il dry run non costa nulla, quindi gira **piu' portafogli virtuali in parallelo** sullo stesso flusso di mercato. Ogni "book" ha capitale, ledger e report propri:

| Book | Decisioni | Scopo |
|------|-----------|-------|
| `champion` | Modello + policy attualmente promossi. | Candidato al live. |
| `challenger` | Ultimi parametri prodotti dal learner notturno (5.1.6). | Promosso solo se batte il champion. |
| `random_entry` | Ingressi casuali con la stessa frequenza media di `champion`, stessi bracket ATR e stesso sizing. | Benchmark: se il champion non batte questo, l'edge non esiste e i guadagni sono rumore o regime di mercato. |
| `buy_and_hold` | Compra al tempo 0 con lo stesso capitale, nessuna uscita. | Benchmark di mercato. |
| `openjev_zeroshot` (opzionale) | Backend OpenJev se disponibile in locale. | Confronto LLM zero-shot contro baseline. |

Ogni book ha il proprio `VirtualAccount` (saldo quote, saldo base, fee pagate, equity mark-to-market al mid del `LocalOrderBook`). Capitale iniziale virtuale configurabile, di default uguale al capitale con cui partira' il live (es. 100 EUR), cosi' i vincoli di min size e di arrotondamento sono realistici fin dal primo giorno.

#### 5.1.5 Label live: il bot impara anche dalle decisioni non prese

Ogni minuto e per ogni prodotto il `DecisionEngine` scrive un `DecisionRecord`, anche quando la decisione e' HOLD o SKIP. Il `LabelResolver` gira ogni minuto: per ogni record con `label_window_end(t) <= now` (sez. 3.1.3) calcola le label (triple-barrier, `edge`, `vol_regime`) sulle barre 1m reali registrate, usando **la stessa funzione** `training/labeling.py` del dataset storico. Output in `data/live_labels/YYYY-MM-DD.jsonl` nel formato esatto della sez. 3.2.1 (con `split: "live"`).

Effetti:
- ogni minuto produce esempi etichettati (non solo i pochi trade eseguiti);
- il sentiment negli esempi e' quello **reale** delle sorgenti live (risolve il problema GDELT vs RSS della sez. 3.1.5);
- il dataset e' direttamente riutilizzabile per il retraining del baseline e, in fase cloud, per il LoRA.

#### 5.1.6 Apprendimento sul campo: cosa si impara, e quando

Con i parametri del `label_config` (sez. 0.4) una label si risolve solo a `label_window_end(t)` (circa 15.5 giorni con H = 15), e i campioni indipendenti per prodotto e anno sono `indep/yr` del report di fattibilita'. Quindi:
- nella **prima settimana** di dry run non si risolve nessuna label: il bot non puo' imparare l'edge dal campo in una settimana, a nessuna condizione;
- la **prova statistica dell'edge** viene dal backtest walk-forward su 3 anni di storico (sez. 3.4, G3), non dal dry run;
- il dry run impara subito tutto cio' che **non** dipende dall'esito dei trade: costi reali, tasso di fill degli ingressi maker, latenza, coerenza tra pipeline live e pipeline di training;
- il modello si aggiorna con **retraining periodico su storico esteso** (il backfill cresce ogni giorno) piu' le label live man mano che si risolvono.

Le soglie minime sono espresse in **campioni effettivi** (`n_eff` = somma dei pesi di unicita' della sez. 3.1.4), non in numero di righe: con label sovrapposte di 10 giorni, migliaia di righe valgono pochi campioni indipendenti.

`NightlyLearner` (ogni giorno alle 00:15 UTC, fuori dal percorso critico, in un processo figlio a bassa priorita'):

| Step | Frequenza | Cosa aggiorna | Condizione minima | Output |
|------|-----------|---------------|-------------------|--------|
| L4 Modello di costo | Giornaliera, da subito | `latency_ms` (p75 del RTT REST), spread mediano per prodotto e ora del giorno, tasso di fill maker entro `T_in`, fee correnti. | Sempre. | `fill_model.json` e tabella spread usata dalle label (sez. 3.1.3) |
| L0 Parita' live/training | Giornaliera, da subito | Ricostruisce con `indicators_batch` + `StateBuilder` gli stati di ieri dalle barre registrate e li confronta con quelli calcolati live; deriva degli input PSI-v3 (`feature_drift`, `modules/dryrun/drift.py`, ultima regola; PSI-v2, respinta il 2026-10-25, resta leggibile come ripiego): ultimi 7 giorni UTC di stati orari ricostruiti dalle barre, per prodotto servito, contro il riferimento R congelato per versione del champion (stati orari del training, prima di `holdout_start`, prodotti di `model_card.data.products`), 12 bin ai quantili di R, smoothing di Jeffreys, p-value empirici e minP di Westfall-Young con alfa 1%, sui 14 rendimenti veloci standardizzati per `rv_1d_bps` dello stesso stato (famiglia J: `n_eff_7d` >= 50 sulla z in R; calibrazione in `data/reports/sim_p1f/PSI_V3_VOLSTD/<versione>/`); le altre feature solo in diagnostica (`support`, percentile del PSI, PSI di `p_edge` contro l'holdout); senza calibrazione `unmeasured`, con PSI-v3 respinta `not_testable` (definitivo); in quel caso sorveglianza `watch` per prodotto con riferimento e soglia diagnostici della corsa respinta, solo allarme. Il PSI giornaliero contro gli ultimi 90 giorni (v1) resta come diagnostica (`state_parity.psi_v1`). Contesto BTC degli stati delle alt (`btc_context`): righe disallineate e loro classi, spostamento e flip col modello della decisione (con policy esplicitamente `untunable`: flip 0 con `flips_basis = policy_untunable`). Audit delle revisioni REST: riscarica le candele del giorno precedente (tutte con eta' >= 15 min all'esecuzione delle 00:15) e le confronta con le RIGHE REALI dello store (minuto rivisto = presenza o OHLCV diversi; classificato in `value`, `appeared`, `disappeared`); lo store non viene modificato. Le revisioni del giorno sono salvate in `data/reports/learner/revisions/<giorno>.parquet`. Impatto delle revisioni (R1-R3, sez. 5.1.7): per ogni book `champion`/`challenger` e ogni versione di modello in uso nel giorno (`policy.policy_version` dei `DecisionRecord`) confronta le risposte del modello sul mondo servito `S` (`fill_flat_bars` delle righe reali) e sul mondo rivisto `F` (le stesse righe con TUTTE le revisioni di valore note fino al giorno, griglia ricostruita con la stessa `fill_flat_bars`), sugli stati FLAT e sugli stati LONG presi dal ledger dry. | Sempre. | Sezione "parity" del report (`feature_drift` e `btc_context`; `psi_v1` diagnostica con la lista `alerts` a `risk.kill_switch.psi_threshold`, nessuna azione); sezione `bars` del report (`rest_revision_ratio` come diagnostica, `rest_revisions_value/appeared/disappeared`, `revision_impact`) |
| L3 Refit baseline | Mensile (1° del mese) | LightGBM riaddestrato su storico esteso fino a ieri + label live risolte (peso 2), stessi split purgati. | Storico aggiornato. | Modello challenger |
| L1 Calibrazione | Con L3 | Platt per `edge` (schema `OOF_PLATT_V1`, punteggi fuori campione piu' validation, sez. 3.1.6), temperature per `action`/`vol_regime`, sul fold di validation piu' recente (storico). Le label live servono a **misurare** la calibrazione, non a fittarla. | `n_eff` del fold >= 300. | `calibration.json` del challenger |
| L2 Soglie di policy | Con L3 | `tune_policy.py` sul validation piu' recente. | Come L1. | `policy.json` del challenger |
| L5 Promozione | Con L3 (giorno di refit; non dipende dall'esito di L3, L1, L2) | Challenger -> champion solo con verdetto `promote` di `decide_promotion`, che valuta nell'ordine: (P0) finestra comune degli ultimi 90 giorni, fuori campione per entrambi i modelli (`data.final_train_end <= inizio - embargo`, il model card lo dichiara), stessi costi e capitale, stessa finestra per i due modelli (mai una finestra per `label_config`), >= 30 trade del challenger (`gate_econ.min_trades`); (P1) challenger con G0, G1, G2, G4 (G3 se LLM) superati e GE = GO (GE = NO-GO: `reject`; INCONCLUSO o `evaluation.json` assente: `insufficient`); (P2, solo se anche il champion ha GE = GO) quantile 5% (`promotion.superiority_quantile`) del bootstrap a blocchi ACCOPPIATO della differenza di PnL, sull'OOS walk-forward comune dei due modelli, > 0 (stessi blocchi temporali per i due modelli, 10.000 repliche e seed di GE), Sharpe maggiore e max DD non peggiore sull'OOS; (P3) sui 90 giorni PnL > 0, PnL e Sharpe strettamente maggiori del champion, max DD non peggiore (se il champion ha trade), max DD <= 5% (`gate_econ.max_drawdown_frac`) sui 90 giorni e sull'OOS; (P4) >= 14 giorni come book `challenger` in dry senza anomalie operative ne' di parita' (R1-R4 della versione compresi, sez. 5.1.7; la parita' e' quella delle decisioni: la deriva delle feature misura il mercato, vale per champion e challenger allo stesso modo e non e' un'anomalia di parita'). Un modello senza trade nella finestra ha PnL 0, Sharpe 0 e drawdown 0 (definiti). Il default e' tenere il champion: finche' nessun challenger ha GE = GO (oggi nessuno), nessuna promozione. L5 valuta il challenger IN CARICA prima del refit della notte (`challenger_before` del documento del learner), perche' L2 sposta l'alias sulla versione appena addestrata, che ha 0 giorni di shadow. Il confronto dei PnL delle shadow book a 14 giorni non e' un criterio: con trade di piu' giorni e' rumore. Il learner riporta verdetto e motivi e non promuove mai (`promoted: false`); il comando di promozione e' locale. | Entrambe. | Verdetto nel documento del learner; nuova versione nel `ModelRegistry` solo per atto locale |

Regole invarianti:
- nessun parametro cambia durante la giornata; alle 00:30 UTC (`learner.reload_at_utc`) l'app (`Runtime.reload_models`) ri-risolve gli alias del registry per ogni book e, se la versione e' cambiata, installa insieme decider, policy, label, domande e `RiskManager` (e quelli del book `random_entry` che lo segue) con uno swap atomico FRA UNA BARRA E LA SUCCESSIVA (lock delle barre: mai a meta' di una decisione, un ordine gia' in volo conserva il suo intent) e logga `model_version_changed {book, alias, from, to, op, reason}`; se la nuova versione non si carica o non e' servibile (file mancante, sha256 non coerente, `label_config`/domande/colonne incompatibili, sentiment diverso, spread mancante) i book RESTANO sulla versione che girano e l'errore e' l'evento `model_reload_failed` (fail closed: mai un book senza modello); un book `registry:<alias>` non partito perche' l'alias non c'era all'avvio parte solo al riavvio (evento `book_needs_restart`);
- ogni artefatto e' versionato in `models/registry/<version>/` con `manifest.json` (hash degli input, finestra dati, metriche, esiti G0-G4 e GE); rollback = puntare il champion alla versione precedente, SOLO dalla CLI locale `python -m modules.dryrun.registry rollback champion --reason ...` (`--reason` obbligatorio, registrato in `aliases.json` con `op`), con effetto al reload delle 00:30 UTC e senza che alcun ordine sia aperto, chiuso o modificato (nessun comando nel canale remoto o nella GUI); la promozione manuale dalla stessa CLI scavalca L5 (stampa l'ultimo verdetto L5 della versione); il rollback e' solo raccomandato dal report (`rollback_advice`, sez. 5.1.7), mai eseguito da un processo; una versione tolta da un rollback torna champion solo con un nuovo L5 completo o con la CLI;
- la SELL del modello non si applica a una posizione aperta con un `label_config` (sha256) diverso da quello della versione corrente (decisione HOLD, motivo `label_config_mismatch`): bracket, orizzonte `H` della posizione e watchdog restano. Ogni posizione registra nel ledger (`positions.model_version`, `label_config_sha256`, `horizon_min`) il modello che ha DECISO il suo ingresso (annunciato prima dell'ordine: un ingresso in attesa di fill attraverso uno swap appartiene a chi l'ha deciso); il watchdog chiude a scadenza al SUO orizzonte; `closed_trades.model_version` e' la versione che ha aperto la posizione, non quella che gira alla chiusura; all'avvio le posizioni aperte senza modello (database scritti prima) prendono quello del book (migrazione additiva, nessuna riga esistente modificata);
- nessun BUY su uno stato di un'alt il cui contesto BTC non ha lo stesso ts (HOLD, motivo `btc_context_stale`, campo `btc_context` del `DecisionRecord`); SELL, bracket e watchdog invariati;
- il learner non tocca mai i moltiplicatori `k_tp`/`k_sl` ne' l'orizzonte `H` (sez. 4.1.1): cambiarli invaliderebbe tutte le label;
- il fine-tuning LLM non avviene in questo loop (C8): le label live si accumulano per la fase cloud.

#### 5.1.7 Gate di uscita dal dry run e rampa di capitale

Il gate e' diviso in due, perche' a questo orizzonte la parte economica richiede mesi.

**G-DRY-OPS** (minimo 7 giorni; e' quello che si puo' chiudere "in una settimana"):

| Criterio | Soglia |
|----------|--------|
| Operativita' | uptime >= 99%, zero `DryRunViolation`, zero crash non gestiti, nessun kill switch scattato per bug |
| Parita' decisioni | Rieseguendo la settimana con il replay (sez. 5.3) e con la pipeline batch di training, le decisioni coincidono con quelle live in >= 99% dei minuti (per la coppia (book, prodotto) interessata sono esclusi i minuti da un comando manuale applicato alla fine del giorno UTC, sez. 6.4.4, e i minuti in cui il record live ha `btc_context == "stale"`, contati a parte in `excluded_btc_stale`) |
| Deriva delle feature | `feature_drift` (PSI-v3) sulla famiglia J dichiarata nell'output (`scope`): nessun prodotto servito in allarme sulla finestra di 7 giorni che termina con l'ultimo giorno del gate; `unmeasured` senza calibrazione, `not_testable` (PSI-v3 respinta, definitivo) diventa `not_covered` (non certificato, non blocca il gate, elencato in `not_covered`) se in ogni giorno della finestra la sorveglianza `feature_drift_watch` e' misurata per ogni prodotto e la diagnostica `support` e PSI di `p_edge` e' presente; altrimenti `unmeasured`. Un allarme `feature_drift_watch:<prodotto>` e' un avviso (`alarms`), non un criterio. Un `support` > 0 e' un allarme (`feature_support:<prodotto>`), non un criterio. Ipotesi nulla, limite per le feature lente e regole di decisione in `docs/decisions/2026-10-25-p1f-psi-v2.md` e `2026-10-26-p1f-bis-psi-v3.md`. Il vecchio PSI giornaliero contro 90 giorni e' la diagnostica `feature_psi_daily_v1`, non un criterio |
| Contesto BTC | `btc_context_alignment`, su ogni giorno e prodotto non BTC: quota di stati con contesto BTC di un altro istante <= `max_btc_stale_share` (0.005), spostamento `m` <= `max_btc_stale_shift` (0.01), zero flip di azionabilita' con base esplicita (`flips_basis`: `computed`, oppure `policy_untunable` se la policy della versione e' `untunable` esplicita e il giorno ha zero BUY, compresi gli HOLD `btc_context_stale`, e zero intent dei book `champion`/`challenger` per quella versione; uno solo = `fail`), zero `value_mismatch` e `flag_inconsistent`; un giorno senza blocco, con spostamento o flip non misurabili o senza base: `unmeasured` |
| Barre REST | Impatto delle revisioni REST sul modello in uso, per prodotto, per giorno UTC giudicato e per versione di modello dei book `champion`/`challenger` (calcolato da L0, sez. 5.1.6). Per ogni stato (FLAT o LONG) `m = max(de, db)` (FLAT) o `max(de, ds)` (LONG), con `de` = spostamento di `p_edge`, `db`/`ds` = spostamento della probabilita' mascherata di BUY (su BUY+HOLD) / SELL (su SELL+HOLD), fra mondo servito e mondo rivisto (NaN in un solo mondo: `m = +inf`). **R1** quota di stati con `m > 0.01` <= 0.005; **R2** `max m` <= 0.02; **R3** quota di stati in cui l'azionabilita' cambia (BUY azionabile su FLAT = soglie di policy, regime, fee floor ed EV gate con i token di ciascun mondo; SELL su LONG) <= 0.001, non valutabile (`unmeasured`) con policy `untunable` o decider senza `answers_batch`; **R4** zero minuti comparsi o scomparsi nella rilettura (fallisce e apre un CONFLITTO); zero barre fuori ordine o duplicate. Unita' di misura (v2, conflitto P1e-bis, fissata dopo il primo `fail` della certificazione P0): R1 e R3 si valutano su blocchi di 1.440 stati consecutivi; un giorno UTC con 1.440 stati e' il proprio blocco; un giorno con meno stati usa i 1.440 stati consecutivi della serie misurata che terminano con il suo ultimo stato oppure, se prima non ce ne sono abbastanza, i primi 1.440 della serie (il blocco puo' sovrapporsi al giorno adiacente, che si valuta comunque per conto suo; le quote non si sommano fra blocchi); una serie con meno di 1.440 stati da' R1 e R3 `unmeasured`; precedenza `fail`, poi `unmeasured`, poi `pass`. R2 e R4 si valutano per giorno UTC (non dipendono dal raggruppamento). In L0 i minuti finali senza scambi di D-1 sono stati del giorno quando lo store prosegue oltre la mezzanotte (provati piatti dalla riga successiva; l'engine li ha consumati); solo uno store fermo prima della mezzanotte lascia un giorno parziale, misurato insieme al giorno prima. Diagnostica senza soglia: finestra mobile di 24 h, revisioni indipendenti, 5 spostamenti maggiori con origine, `max_shift / max_revision_shift`. `rest_revision_ratio` (quota di minuti rivisti) e' diagnostica nel report, con allarme se > 0.005 (obbliga alla classificazione), non entra nello stato del gate. Le soglie sono in `dry_run.gate_ops` (sez. 5.2). La scala di taratura di S (sez. 2.3 A.2) vale solo per le revisioni di assestamento. Un giorno senza il dato e' `unmeasured`; con un champion a policy `untunable` R3 e' `unmeasured` e il gate resta al massimo `INCOMPLETE` |
| Frequenza di BUY | Entro +-50% della frequenza attesa dal backtest sullo stesso periodo (se il periodo non ha BUY ne' nel backtest ne' live, criterio superato) |
| Costi | Fill rate maker misurato >= 50%; spread mediano entro 2x quello assunto nelle label |
| Fee floor | Quota di intent scartati per `fee_floor` < 80% |

**G-DRY-ECON** (sul book `champion`, guidato dal numero di trade, valutato in continuo; il tempo necessario per 30 trade si ricava dalla frequenza di trade del backtest):

| Criterio | Soglia |
|----------|--------|
| Trade chiusi | >= 30 |
| PnL netto dopo fee e slippage simulati | > 0 |
| Profit factor | >= 1.2 |
| Contro `random_entry` | PnL netto champion > random_entry |
| Max drawdown | <= 5% del capitale virtuale |
| Calibrazione live `edge` | ECE <= 0.10 su tutte le label live risolte (larga per il campione ridotto) |

**Rampa:**

| Gradino | Prerequisito | Capitale live | Size | Condizione per salire |
|---------|--------------|---------------|------|-----------------------|
| R0 live-micro | G-DRY-OPS **e** gate della sez. 3.4 superati con GE = GO | Minimo operativo (es. 20–50 EUR) | `min_size` della sez. 4.1.2 (circa 1–2 EUR di controvalore per trade, perdita massima a SL di pochi centesimi) | 10 trade live con parity report entro le tolleranze |
| R1 | G-DRY-ECON | Capitale iniziale scelto dall'utente | Sizing a rischio della sez. 4.1.2 | 30 trade live, PF >= 1.2, DD <= 5% |
| R2+ | R1 superato | Raddoppio per gradino | Sizing a rischio | Stesse condizioni di R1 sul gradino corrente |

R0 parte presto di proposito: il rischio e' di pochi centesimi per trade e serve a misurare fill, fee e comportamento dei bracket reali (shadow twin, sotto) mentre G-DRY-ECON matura in parallelo.

In qualunque gradino: violazione del kill switch o PF < 1.0 su 30 trade -> discesa di un gradino; due discese consecutive -> ritorno in dry.

**Differito a P4 (CONFLITTO P1g, nessun codice ora; da implementare prima del primo ordine reale):** (D1) campo `decider` nel `DecisionRecord` (valore di `BookConfig.decider`): le prove `untunable` filtrano `registry:*` al posto di `MODEL_BOOKS`; (D2) `flips_basis` su R3 solo per versioni `untunable` esplicite, con zero BUY, zero intent e zero stati LONG della versione nel giorno; (D3) G-DRY-ECON con `edge_calibration_mean` d >= -0,05 ed `edge_ece` <= 0,10 sullo stesso insieme (label `edge` live dei book del modello della versione champion, deduplicate per (versione, prodotto, ts)), `unmeasured` sotto `n_eff` >= 200 (somma dei pesi di unicita' per prodotto / (1 + (K - 1) rho)), `n_eff` e d nel report di ogni giorno, motivo `policy_untunable` nello stato del gate; (D4) guardia `drift` per prodotto per le versioni `not_covered`: un allarme `feature_drift_watch` o `support` > 0 del giorno D blocca gli INGRESSI di quel prodotto dal reload successivo per un giorno (API per prodotto con scadenza in `SystemGuards`, oggi globale e senza scadenza; dato mancante = blocco; SELL, bracket, orizzonte e watchdog invariati; book live, shadow twin e book del modello in dry); (D5) stop di calibrazione in R0 per ogni versione: con `n_eff` >= 30, d + 1,645 se < -0,05 -> ritorno in dry. Regola di rampa: una versione `not_covered` non entra in R0 se D4 e D5 non sono implementati e testati.

**Raccomandazioni di rollback (solo report, nessuna azione):** il documento giornaliero del report ha il blocco `rollback_advice` per la versione champion corrente: `{version, since, closed_trades, defect_triggers, economic_triggers, triggers, economics, advice: "none" | "consider_rollback", command}`. Difetti, subito e senza minimo di trade: parita' delle decisioni < `min_decision_parity`; R1-R4 falliti con la versione; fallback del decider oltre `risk.kill_switch.max_fallback_ratio_1h`; versione non caricabile (alias o file danneggiati); scatto del kill switch per bug. Economia, solo con >= `gate_econ.min_trades` trade chiusi DELLA VERSIONE (`MANUAL` esclusi): profit factor < 1.0; `z = (m_post - m_oos) / sqrt(se_post^2 + se_oos^2) < -1.96` (`m` = media di `net_bps`, `se` con il numero effettivo di trade, la stessa funzione di E5 `holdout_consistency`; `m_oos` e `se_oos` da `oos_trades.jsonl` della versione); >= 2 scatti del kill switch per perdita (`daily_loss`, `loss_streak`, `bug = false`) dalla data della versione. I trade si attribuiscono alla versione con il `model_version` che il ledger registra alla chiusura; senza, l'economia e' `unmeasured`, mai zero. Il report legge il registry e non lo scrive mai, ne' scrive nell'inbox dei comandi; l'unico rollback e' la CLI locale `python -m modules.dryrun.registry rollback champion --reason "..."` (sez. 5.1.6). Il blocco automatico degli ingressi del book interessato e' una decisione dell'utente (U3), non implementata.

**Shadow twin in live:** in modalita' `live` lo `ShadowExecutionGateway` resta attivo in parallelo e simula ogni ordine reale con lo stesso intent. Il `ParityReport` giornaliero confronta reale e simulato per ciascun ordine: prezzo medio di fill, fee, tempo di fill, esito del bracket. Tolleranze: |delta prezzo di ingresso| <= 10 bps mediano, stesso esito del bracket in >= 90% dei trade. Gli scostamenti ricalibrano `latency_ms`, `queue_haircut`, `stop_extra_slip_bps` (step L4); un dry run con modello di fill calibrato sul reale e' molto piu' affidabile del precedente.

### 5.2 Struttura dei Dati / Schema JSON

**Config YAML:**

```yaml
execution:
  mode: "dry"                               # dry | live ; live richiede env CHRONOS_LIVE_CONFIRM=I_ACCEPT_REAL_ORDERS
dry_run:
  db_path: "state/chronos_dry.db"           # named volume (sez. 0.5); DB separato: dati dry e live non si mescolano mai
  books:
    - {name: champion, decider: registry:champion, starting_quote: "100.00"}
    - {name: challenger, decider: registry:challenger, starting_quote: "100.00"}
    - {name: random_entry, decider: random, match_frequency_of: champion, seed: 42, starting_quote: "100.00"}
    - {name: buy_and_hold, decider: buy_and_hold, starting_quote: "100.00"}
  fill_model:
    path: "models/registry/fill_model.json" # sovrascrive i default sotto se presente
    latency_ms: 300
    queue_haircut: 0.25
    stop_extra_slip_bps: 10
    fallback_fees: {maker_bps: 60, taker_bps: 120}
  recording:
    enabled: true
    dir: "data/ws_recordings"               # messaggi WS grezzi gzip, per replay deterministico
    retention_days: 30
  learner:
    run_at_utc: "00:15"
    reload_at_utc: "00:30"
    refit_day_of_month: 1
    min_n_eff_calibration: 300
    live_weight: 2.0
    # Regola L5 (5.1.6). Minimo di trade e limite di drawdown NON hanno chiave qui: si leggono da gate_econ.
    promotion: {backtest_recent_days: 90, min_shadow_days: 14, superiority_quantile: 0.05}
  gate_ops:
    min_days: 7
    min_uptime: 0.99
    min_decision_parity: 0.99
    window_start: null                 # primo giorno UTC contato dal gate (2.14); null = ultimi `--days` giorni
    max_feature_psi: 0.10              # ALLARME della diagnostica `feature_psi_daily_v1` (P1f), non un gate
    max_btc_stale_share: 0.005         # btc_context_alignment: quota massima di stati alt con contesto BTC di un altro istante
    max_btc_stale_shift: 0.01          # ... e spostamento massimo m delle risposte del modello fra stato servito e allineato
    buy_rate_tolerance: 0.50
    min_maker_fill_rate: 0.50
    max_spread_ratio_vs_labels: 2.0
    max_fee_floor_skip_ratio: 0.80
    max_rest_revision_ratio: 0.005     # ALLARME di una diagnostica (quota di minuti rivisti), non un gate (2.11)
    revision_shift_threshold: 0.01     # R1: uno stato e' spostato se m > 0.01
    max_revision_shift_share: 0.005    # R1: quota massima di stati spostati
    max_revision_shift: 0.02           # R2: spostamento massimo di uno stato
    max_revision_flip_share: 0.001     # R3: quota massima di stati con azionabilita' diversa
  gate_econ:
    min_trades: 30
    min_profit_factor: 1.2
    max_drawdown_frac: 0.05
    max_ece_edge: 0.10
live_ramp:
  steps: ["R0", "R1", "R2"]
  r0_min_trades: 10
  step_min_trades: 30
  parity: {max_median_entry_delta_bps: 10, min_bracket_outcome_agreement: 0.90}
```

**ShadowOrder** (tabella `shadow_orders` nel DB dry):

```json
{
  "shadow_order_id": "01JAC0...ULID",
  "book": "champion",
  "intent_id": "01JAB3...ULID",
  "product_id": "SOL-EUR",
  "side": "BUY",
  "entry_mode": "maker",
  "limit_price": "92.03",
  "base_size": "0.024",
  "submitted_at": "2026-10-17T14:03:01.120Z",
  "simulated_arrival_at": "2026-10-17T14:03:01.420Z",
  "fills": [
    {"price": "92.03", "size": "0.024", "liquidity": "MAKER", "fee": "0.0133", "at": "2026-10-17T14:10:22.517Z", "source": "trade_through"}
  ],
  "status": "FILLED",
  "book_snapshot_ref": "ws_recordings/2026-10-17/SOL-EUR.jsonl.gz#seq=88123401"
}
```

**VirtualTrade** (chiuso, tabella `closed_trades`; la stessa tabella, con lo stesso schema, esiste nel DB live per il book `live`, sez. 6.3 D7; `exit.reason` in `TP`, `SL`, `SELL`, `HORIZON`, `WATCHDOG`, `MANUAL`):

```json
{
  "trade_id": "01JAC1...ULID",
  "book": "champion",
  "product_id": "SOL-EUR",
  "entry_decision_id": "01JAB3...ULID",
  "entry": {"avg_price": "92.03", "size": "0.024", "fees": "0.0133", "at": "2026-10-17T14:10:22.517Z"},
  "bracket": {"tp": "108.96", "sl": "83.57", "k_tp": 4.0, "k_sl": 2.0},
  "exit": {"reason": "TP", "avg_price": "108.96", "fees": "0.0157", "at": "2026-10-22T14:25:40.310Z"},
  "pnl": {"gross_quote": "0.4063", "fees_quote": "0.0290", "net_quote": "0.3774", "net_bps": 1698, "r_multiple": 1.53},
  "held_min": 7215,
  "flags": {"gap_resolved": false, "partial_entry": false},
  "model_version": "baseline-lgbm@2026-10-16.1",
  "fill_model_version": "fill@2026-10-16.1"
}
```

**ResolvedLabel** (`data/live_labels/YYYY-MM-DD.jsonl`): stesso schema della sez. 3.2.1 con `split: "live"` e, in piu':

```json
{
  "decision_id": "01JAB3...ULID",
  "predicted": {"action": {"BUY": 0.71, "SELL": 0.02, "HOLD": 0.27}, "edge": 0.58, "vol_regime": 2.31},
  "policy_final": "BUY",
  "resolved_at": "2026-10-17T18:04:00Z"
}
```

**Report giornaliero** (`data/reports/dry/YYYY-MM-DD.json`, piu' rendering Markdown):

```json
{
  "date": "2026-10-17",
  "mode": "dry",
  "books": {
    "champion": {"equity_start": "100.00", "equity_end": "100.31", "net_pnl_quote": "0.31", "fees_quote": "0.03",
                 "open_positions": 2, "trades_closed_today": 1, "trades_closed_total": 1,
                 "profit_factor_total": null, "max_dd_frac": 0.004},
    "random_entry": {"net_pnl_quote": "-0.12", "open_positions": 1, "trades_closed_total": 0},
    "buy_and_hold": {"net_pnl_quote": "0.40"}
  },
  "decisions": {"total": 4320, "BUY": 2, "SELL": 0, "HOLD": 4318,
                "skip_reasons": {"fee_floor": 0, "ev_negative": 3, "ev_buffer": 1, "regime_veto": 0, "below_min_size": 0,
                                 "kill_switch": 0, "entry_pending": 41, "insufficient_balance": 0, "balance_stale": 0}, "skipped_late": 0},
  "labels": {"resolved_total": 0, "first_resolution_expected": "2026-10-27"},
  "parity": {"decision_match": 1.0, "compared": 4320, "matched": 4320, "excluded_manual": 0, "excluded_btc_stale": 0,
             "state_match": 1.0, "state_match_all": 0.9979,
             "max_feature_psi": 7.81, "worst_feature": "seq_1d_bps_5",  # diagnostica v1: chiavi della 5.2 mantenute
             "psi_v1": {"rule": "PSI_V1_DAILY_90D", "status": "diagnostic", "above_0_10": 37, "alerts": []},
             "feature_drift": {"rule": "PSI_V2", "status": "unmeasured", "reason": "no_calibration"},
             "btc_context": {"products": {"ETH-EUR": {"alt_states": 1440, "misaligned": 4, "share": 0.0028,
                             "classes": {"live_missing": 4, "batch_missing": 0, "value_mismatch": 0, "flag_inconsistent": 0},
                             "max_shift": 0.0031, "flips": 0, "shift_status": "measured"}}}},
  "bars": {"SOL-EUR": {"rest": 1402, "flat": 36, "flat_unproven": 2, "lag_s_p50": 120.3, "lag_s_p99": 126.1, "fetch_failures": 0, "late": 0, "rest_revisions": 1, "rest_revision_ratio": 0.0007, "rest_revision_max_close_bps": 1.8, "rest_revisions_value": 1, "rest_revisions_appeared": 0, "rest_revisions_disappeared": 0, "revision_impact": {"champion": {"version": "baseline-lgbm@2026-10-18.2", "states": 1440, "share": 0.0, "max_shift": 0.0029, "flip_share": null, "r3": "policy_untunable"}}}},
  "alarms": [],
  "costs": {"median_spread_bps": {"BTC-EUR": 3, "ETH-EUR": 2, "SOL-EUR": 3}, "fee_maker_bps": 60, "fee_taker_bps": 120,
            "fee_source": "assumed", "maker_fill_rate": 0.67, "latency_ms_p75": 280},
  "ops": {"uptime": 0.9993, "ws_reconnects": 2, "decider_fallbacks": 0, "dry_run_violations": 0},
  "gate_ops": {"days": 3, "status": "IN_PROGRESS"},
  "gate_econ": {"trades": 1, "status": "IN_PROGRESS"}
}
```

Note sullo schema del report (2.11, revisioni REST): `bars.<prodotto>.revision_impact` ha una voce per book (`champion`, `challenger`) con la versione di modello PEGGIORE del giorno (`version`, `states`, `share` e `flip_share` del BLOCCO di R1/R3, `max_shift`, `r3`, e in piu' `day_share`, `block_states`, `block_kind`, `revisions`, `max_cluster`, `r2_utilization`, `top_shifts`, `first_at`, `last_at`, `shifted_at`; `share` e' `null` se il blocco non e' formabile o se il documento L0 e' anteriore ai blocchi e il giorno e' parziale); il documento L0 ha anche `blocks` e `helper_days` (il giorno ausiliario D-2 misurato quando D-1 e' parziale, mai giudicato); `bars.<prodotto>.revision_impact_by_version` ha la stessa voce per ogni versione usata da qualunque book. `alarms` elenca le diagnostiche scattate (`rest_revision_ratio:<prodotto>`) e non entra in `anomalies`. `anomalies.revision_by_version` assegna R1-R3 falliti alla versione che li ha falliti (`{versione: ["R1:<prodotto>", ...]}`; lo shadow del challenger conta solo la propria); R4 > 0 e' un'anomalia operativa globale. Il risultato dei gate (`gate_ops`) ha `criteria` (`revision_shift_share` = R1, `revision_shift_max` = R2, `revision_decision_flips` = R3, `rest_completeness` = R4, `bars_in_order_no_duplicates`) e `diagnostics: [{"name": "rest_revision_ratio", "value", "alarm_threshold", "alarm", "note"}, ...]` con in piu' `revision_shift_rolling_max`, `revision_independent_revisions`, `revision_r2_utilization` (`{"name", "value", "detail", "note"}`, senza soglia, fuori dai criteri). Il soak P0 produce `data/reports/p0_soak.json` con `verdict.rest_bars_complete` (R4) e `checks.rest_revision_alarm` al posto di `rest_revision_ratio_within_limit`; R1/R2 del soak si certificano con `python -m modules.dryrun.revision_impact certify` (`chronos.p0_bars_certification.v2`; un file di certificazione non si riscrive: ogni corsa ne scrive uno nuovo, con `previous_run`, le candele rilette salvate con sha256 e l'esito anche con la regola v1 come diagnostica; `--info-only` stampa gli esiti v1 e v2 senza scrivere nulla ed e' l'unica modalita' ammessa sul soak 18-20/10/2026).

Note sullo schema del report (2.12, deriva delle feature e contesto BTC, conflitto P1f): `parity` ha `excluded_btc_stale`, `state_match` (senza le righe di contesto BTC disallineato) e `state_match_all` (tutte), le chiavi `max_feature_psi` e `worst_feature` della v1 (diagnostica), `psi_v1`, `feature_drift` (`{rule: "PSI_V2", status: measured|unmeasured, reason, calibration: {run_dir, sha256, champion_version, window_days, alpha, c}, window, products: {prodotto: {status, states, S, alarm, flagged, features, unmeasured_features}}, alarm}`; un documento del learner senza il blocco o con `skipped` vale `{status: unmeasured, reason}`) e `btc_context` (per prodotto non BTC: `alt_states`, `misaligned`, `share`, `classes`, `max_shift`, `flips`, `shift_status` in `measured`, `no_deciders`, `policy_untunable`, `decider_without_batch`); un documento del learner scritto prima di P1f si legge ancora (chiavi v1 di primo livello). `gate_ops.criteria` ha `feature_drift` (letto solo dall'ultimo giorno della finestra) e `btc_context_alignment` (su tutti i giorni, precedenza `fail`, `unmeasured`, `pass`) al posto di `feature_psi`; `gate_ops.diagnostics` ha `feature_psi_daily_v1` (`{value, detail: {worst_feature, above_0_10, day}, alarm_threshold, alarm, note}`); `alarms` ha `feature_drift:<prodotto>` per ogni prodotto in allarme PSI-v2 (un'allerta, non un'anomalia di parita'). Testo della nota di `feature_drift` mentre non e' misurato: "feature_drift: unmeasured. Il PSI giornaliero contro gli ultimi 90 giorni misura in quanti decili cade un giorno di feature persistenti (4 prodotti x 1 valore, tetto ~8,3), non la deriva rispetto al training. Criterio sospeso (CONFLITTO P1f) fino alla regola PSI-v2 calibrata (SIM-P1f, D0-D3). Diagnostica: max PSI = <valore> (<feature>), <k> feature sopra 0,10. Motivo: <reason>."

Note sullo schema del report (2.13, regola PSI-v3 e contesto BTC, conflitto P1f-bis): il blocco `parity.feature_drift` di PSI-v3 ha `rule: "PSI_V3_VOLSTD"`, `status` in `measured`, `unmeasured`, `not_testable` (reason `calibration_rejected_final`), `calibration` (`run_dir`, `sha256`, `champion_version`, `window_days`, `alpha`, `c`, `decision`, `final`), `scope` (`tested`, `untested: [{feature, n_eff_7d}]`, `note` = `drift.scope_note`), `window`, `products` (per prodotto `status`, `states`, `S`, `alarm`, `flagged`, `features` con le z, `unmeasured_features` e `diagnostics`: `support`, `psi_percentile`, `p_edge_psi`), `alarm`, `support_alarm`; il blocco di PSI-v2 resta com'e'. `parity.btc_context.products.<prodotto>` ha in piu' `flips_basis` (`computed`, `policy_untunable`, `mixed`, null) e `untunable_check` (`{versions: {versione: {policy: tuned|untunable|missing, buy_records, intent_records}}}`). `gate_ops.criteria.feature_drift` con `not_testable` ha lo stato `unmeasured` e la nota "feature_drift: not_testable. La regola PSI-v3 (ultima, pre-registrata) e' stata respinta da SIM-P1f-bis: nessun test della deriva delle feature e' adottato. G-DRY-OPS puo' chiudersi senza questo criterio solo con un criterio di calibrazione di p_edge sugli esiti in un gate richiesto per P3, che oggi non esiste (CONFLITTO P1g)."; in `pass` e `fail` la nota e' l'elenco dei flag seguito da ` | ` e dalla nota di `scope`; `gate_ops.diagnostics` ha `feature_drift_scope`; `alarms` ha `feature_support:<prodotto>`; il testo `unmeasured` dice "Criterio sospeso (CONFLITTO P1f-bis) fino alla regola PSI-v3 calibrata (SIM-P1f-bis, D1/D1h/D2/D3)."

Note sullo schema del report (2.14, chiusura di `feature_drift`, conflitto P1g): con PSI-v3 respinta il blocco `parity.feature_drift` (`status: "not_testable"`) ha in piu' `watch` di primo livello (`{rule, reference: "reference_diagnostic", c, false_alarm_oos, gate: false}`, `false_alarm_oos` = `window.d1` della corsa respinta) e `watch_alarm` (prodotti in allarme, ordinati), e per prodotto `watch` (`{status: measured|unmeasured, S, alarm, flagged}` oppure `{status: "unmeasured", reason: no_diagnostic_reference|incomplete}`). Lo stato di criterio `not_covered` ha `value` = `{prodotto: S}` dell'ultimo giorno, `threshold` = `watch.c`, la nota `NOT_COVERED_NOTE`; `gate_ops.not_covered` elenca i criteri in quello stato (la lista e' vuota se nessuno); il testo `unmeasured` di un blocco `not_testable` e' `not_testable_note(reason)` con i motivi `no_learner_document`, `not_testable_missing`, `no_products`, `product_not_measured`, `watch_unmeasured`, `support_missing`, `p_edge_psi_unmeasured` (giorno e prodotto in coda); `alarms` ha `feature_drift_watch:<prodotto>`. La certificazione P0 dei giorni del dry run ha `soak_report: null` e `dry_run_days: {start, end, products}`.

### 5.3 Linee Guida per l'implementazione in Sonnet 5.5

> PROMPT — Modulo Dry Run
>
> Implementa il package `modules/dryrun/` di Chronos v2 e le modifiche di wiring indicate. Python 3.12, asyncio, pydantic v2, `Decimal` per prezzi, size, fee e saldi. Requisito chiave: in dry mode il percorso di codice dev'essere **identico** al live, salvo l'implementazione di `ExecutionGateway` e la sorgente dell'account. Non duplicare logica di decisione, rischio o ledger dentro `dryrun/`.
>
> 1. `modules/execution/exchange.py` (modifica) — parametro `read_only: bool`. Se `True`, `create_order`, `cancel_orders`, `preview_order` e qualunque metodo POST sollevano `DryRunViolation` **prima** della chiamata di rete. Decorator `@write_operation` applicato a tutti i metodi di scrittura; un test enumera via introspezione tutti i metodi pubblici che fanno POST/DELETE e verifica che abbiano il decorator.
> 2. `modules/config.py` (modifica) — `ExecutionConfig.mode: Literal["dry", "live"] = "dry"`; funzione `resolve_mode(cfg, env) -> Literal["dry","live"]` che ritorna `"live"` solo se `cfg.mode == "live"` **e** `env.get("CHRONOS_LIVE_CONFIRM") == "I_ACCEPT_REAL_ORDERS"`; altrimenti `"dry"` con warning. Test della tabella di verita' completa.
> 3. `modules/market/orderbook.py` — `LocalOrderBook` per prodotto, alimentato dal canale `level2` (snapshot + update), lati in `sortedcontainers.SortedDict` (bid in ordine decrescente tramite chiave negata), metodi `best_bid()`, `best_ask()`, `mid()`, `spread_bps()`, `walk(side, size, limit) -> WalkResult(filled, vwap, levels_consumed)`. Verifica di integrita': se `best_bid >= best_ask` o il sequence number salta -> risottoscrive e marca il book `stale` finche' non arriva un nuovo snapshot; con book `stale` lo `ShadowExecutionGateway` rimanda i fill.
> 4. `modules/dryrun/fill_model.py` — `FillModel` con le regole della sez. 5.1.3 in due facce: `TickFillModel` (input: `LocalOrderBook`, eventi `market_trades`) e `BarFillModel` (input: barre OHLCV), stessi parametri da `FillModelParams` (pydantic, caricabile da `fill_model.json`). `training/backtest.py` e il `SimulatedExchange` dei test della sez. 4.3 devono importare `BarFillModel` da qui; rimuovi ogni logica di fill duplicata.
> 5. `modules/dryrun/virtual_account.py` — `VirtualAccount(book, starting_quote, clock)`: compone un `BalanceBook(book, source="virtual")` (sez. 4.1.6), stessa classe del live; riserve, fill e rilasci passano dal `BalanceGuard` e dal `BalanceBook`, nessuna logica di riserva propria. Espone `equity(mark_prices)`, `fees_paid`, `realized_pnl`. Invarianti testati con `hypothesis`: `quote + sum(base_i * price_i) + fees_pagate_cumulative == starting_quote + pnl_realizzato + pnl_non_realizzato` (tolleranza 1e-12); riserve e saldi mai negativi; `free + reserved` invariato da reserve/release.
> 6. `modules/dryrun/shadow_gateway.py` — `ShadowExecutionGateway(book, account, fill_model, ledger, clock)` che implementa `ExecutionGateway(Protocol)` della sez. 4.3:
>    - `submit_entry_with_bracket(intent)`: registra un `ShadowOrder`, ottiene un `CoverageTicket` da `BalanceGuard.authorize` (stessa prenotazione del live), programma l'arrivo a `now + latency_ms`; al primo evento di book successivo esegue `walk` per la parte marketable; il residuo resta resting e viene valutato su ogni `market_trade`; timeout con `entry_timeout_s`;
>    - al fill crea la posizione `LONG` nel `PositionLedger` dry (stesso schema della sez. 4.2) e un `VirtualBracket`;
>    - `submit_exit`, `emergency_exit`: walk dei bid;
>    - nessun `sleep` reale: tutto guidato dagli eventi di mercato e dal `Clock` iniettato (in replay e test il clock e' simulato).
> 7. `modules/dryrun/bracket_sim.py` — `BracketSimulator.on_trade(trade)` e `on_book(book)` per tutte le posizioni virtuali aperte del prodotto: regole TP/SL/"stesso evento -> SL"; gestione del gap di dati come in sez. 5.1.3 (download delle candele del gap via `RESTClient.get_candles`, lettura consentita).
> 8. `modules/dryrun/books.py` — `ShadowBookManager`: istanzia un `ShadowExecutionGateway` + `VirtualAccount` + ledger per ogni book in config; fa fan-out degli eventi di mercato verso tutti i book; per `random_entry` usa un `RandomDecider(seed)` che emette BUY con probabilita' pari alla frequenza giornaliera di BUY osservata nel champion (aggiornata ogni ora); per `buy_and_hold` un `BuyAndHoldDecider` che compra una volta sola. I decider dei book passano tutti dalla stessa `DecisionPolicy` e dallo stesso `RiskManager` (tranne `buy_and_hold`, che salta il RiskManager per costruzione ma non il `BalanceGuard`: size = `max_base_for_quote(free(quote), ...)`).
> 9. `modules/dryrun/recorder.py` — `WsRecorder`: scrive ogni messaggio WS grezzo con timestamp di ricezione in `data/ws_recordings/YYYY-MM-DD/<channel>-<product>.jsonl.gz` (scrittura in un thread dedicato con buffer, flush ogni 5 s, rotazione giornaliera, retention). `modules/dryrun/replay.py` — `ReplayMarketStream` con la stessa interfaccia di `CoinbaseMarketStream`, che rilegge le registrazioni con clock simulato: consente di rieseguire offline e in modo deterministico una settimana di dry run con parametri diversi (what-if a costo zero). Le barre 1m non sono nelle registrazioni WS: in replay il `BarService` usa `StoreCandleSource` (store Parquet, stessa interfaccia `CandleSource`) con lo stesso clock simulato. Test: lo stesso replay eseguito due volte produce ledger identici byte per byte.
> 10. `modules/dryrun/label_resolver.py` — task ogni 60 s: seleziona i `DecisionRecord` con `ts_bar_close + T_in + H <= now` (parametri dal `label_config` del modello che ha preso la decisione) non ancora risolti, carica le barre 1m dal Parquet locale (alimentato dal `BarService` e dal backfill incrementale, sez. 2.3 A.2; `store_complete_until_end=True` solo se `BarStoreWriter.complete_until(product) >= vertical`, altrimenti la risoluzione viene rimandata), chiama `training.labeling.triple_barrier` e `forward_rv_bucket` (stesso codice del dataset storico), scrive `ResolvedLabel`. Idempotente (tabella `resolved` con `decision_id` PK).
> 11. `modules/dryrun/learner.py` — `NightlyLearner` con gli step L0–L5 della sez. 5.1.6 e le rispettive frequenze (L0 e L4 ogni notte; L3, L1, L2, L5 il giorno `refit_day_of_month`), con soglie in `n_eff` (somma dei pesi di unicita' di `training/sampling.py`), eseguito con `asyncio.create_subprocess_exec(sys.executable, "-m", "modules.dryrun.learner", ...)` e `os.nice(10)` nel figlio dove supportato, per non rallentare il loop di trading. Ogni step controlla la propria soglia minima e, in sotto-soglia, registra `skipped: insufficient_samples` invece di produrre artefatti. L'app lo pianifica da sola (`Runtime.run_scheduled`, task `schedule` non critico): alle `learner.run_at_utc` lancia `python -m modules.dryrun.learner --once --config <config dell'app>` come PROCESSO FIGLIO con priorita' BELOW_NORMAL (Windows) o `nice` (altrove), un solo learner alla volta (un lancio con il figlio precedente ancora vivo e' l'evento `learner_skipped`), con un timeout (6 ore); un figlio che fallisce, va in timeout o non parte e' un evento (`learner_failed`, `learner_timeout`, `learner_error`), mai un arresto dell'app; all'arresto dell'app il figlio e' terminato (il learner riprende la data iniziata). Un job parte una volta per giorno UTC quando l'orologio supera il suo orario; uno il cui orario e' gia' passato all'avvio aspetta il giorno dopo. Gli audit di L0 (parita' degli stati, impatto delle revisioni R1-R3) usano la `label_config` del MODELLO CHE GIRA (il champion del registry; per una decisione, quella della versione che l'ha presa; `RegistryDeciderLoader` carica la label di ciascuna versione e un prodotto che la label di una versione non copre non e' misurato per essa), non quella del dataset di ricerca di `train.yml`, che serve solo al refit (L1-L3); senza champion nel registry gli audit ripiegano sulla label di training e lo dicono (`details.audit_label.source = training_fallback` e un avviso nel log). Il documento del giorno riporta `challenger_before` (l'alias `challenger` a inizio notte) e L5 giudica quella versione, non quella che L2 sta per proporre: L5 gira nel giorno di refit anche se L3, L1 o L2 sono falliti o saltati (`skipped: no_previous_challenger` se non c'era un challenger, `skipped: challenger_is_champion`). Il learner calcola UNA finestra comune (`fine = min(ultima barra dei prodotti, cutoff)`, `inizio = max(holdout_start dei due modelli, fine - backtest_recent_days)`), la passa a `RecentBacktester.__call__(model_dir, start, end)` per entrambi i modelli, legge i gate dall'`evaluation.json` di ciascuna versione (`_gate_flags` porta G0, G1, G2, G3, G4, GE nel manifest) e le prove OOS da `OosComparator` (`training/oos_compare.py`). `HoldoutBacktester` con policy `untunable` restituisce zero trade con Sharpe 0.0 e drawdown 0 (metriche definite). L0 aggiunge a quanto sopra la deriva delle feature `feature_drift` (PSI-v2, `modules/dryrun/drift.py`, con la calibrazione di `data/reports/sim_p1f/<versione del champion>/`: assente, rifiutata o di un'altra `label_config` = `unmeasured`; una pre-registrazione modificata dopo la corsa fa fallire la parte) e il blocco `btc_context`; `script/sim_p1f.py` (`s6`; il suo `run` per PSI-v2 e' chiuso) e `script/sim_p1f_v3.py` (`run`, `s6`) calibrano la regola una volta per versione di champion (PSI-v3, ultima).
> 12. `modules/dryrun/registry.py` — `ModelRegistry` su filesystem (`models/registry/`): `register(artifacts, manifest) -> version`, `get(alias)`, `promote(version, alias="champion", *, reason="")`, `rollback(alias, *, reason="")`. Gli alias sono un file `aliases.json` scritto in modo atomico (scrittura su file temporaneo + `os.replace`); ogni voce porta `op` (`promote` | `rollback`) e `reason` (un file senza questi campi si legge come `promote`, `""`; lo schema resta `chronos.registry.aliases.v1`). La CLI locale `python -m modules.dryrun.registry list|show|promote|rollback` richiede `--reason` per `promote` e `rollback` (`rollback` ha `champion` come alias di default), stampa che l'effetto parte al reload successivo (00:30 UTC) e che nessun ordine viene toccato, e per `promote` stampa l'ultimo verdetto L5 della versione senza imporlo (la promozione manuale scavalca L5, decisione U2). `PromotionRule.from_config(learner, gate_econ)`, `PromotionEvidence` (con `BacktestEvidence`, `GateSummary`, `OosEvidence`) e `decide_promotion` fissano la regola L5 della sez. 5.1.6; `promote_if_eligible` e' l'unico percorso che promuove sulla base della regola, il learner non lo chiama. Interfaccia astratta `ModelRegistry(Protocol)` per sostituire il backend in futuro (C8).
> 13. `modules/dryrun/report.py` — job alle 00:05 UTC e CLI `python -m modules.dryrun.report --days 7 [--book champion]`: genera il JSON della sez. 5.2 e un Markdown con tabelle per book, istogramma degli skip, reliability diagram di `edge` (PNG matplotlib, solo quando ci sono label live risolte), parita' L0, costi L4, stato di G-DRY-OPS e G-DRY-ECON, data prevista della prima label risolta. Metriche: PnL netto, fee, win rate, profit factor, R medio, max drawdown, Sharpe giornaliero (indicativo, con nota sul campione), confronto con `random_entry` e `buy_and_hold`.
> 14. `modules/dryrun/parity.py` — solo in live: per ogni intent reale invia lo stesso intent allo shadow twin e produce il `ParityReport` giornaliero con le tolleranze della sez. 5.1.7.
> 15. `modules/app.py` (modifica) — `build_execution(mode)`: in `dry` istanzia `CoinbaseExchange(read_only=True)`, `ShadowBookManager` e **non** importa `LiveExecutionGateway`; in `live` istanzia `LiveExecutionGateway` + shadow twin. Il `DecisionEngine` riceve in entrambi i casi la lista dei book da servire (in live: `live` + `shadow_twin`). Banner di avvio che stampa la modalita', i book attivi e il permesso effettivo della API key; se in `live` la key non ha il permesso Trade, rifiuta di partire.
> 16. `modules/control/` (`commands`, `CommandInbox`, `CommandExecutor`, CLI del canale locale) secondo la sez. 6.4; il `NightlyLearner` scrive l'esito di ogni step in `data/reports/learner/YYYY-MM-DD.json` (sez. 6.3, D11). Il replay (punto 9) non rilegge mai la coda comandi.
>
> Test obbligatori:
> - `test_dry_never_writes`: pipeline completa in dry su 30 minuti di replay con un `RESTClient` mock che fa fallire il test a qualunque chiamata di scrittura;
> - `test_fill_model_tick_vs_bar`: sullo stesso replay il PnL del `TickFillModel` e del `BarFillModel` stanno entro una tolleranza documentata (il bar model dev'essere piu' pessimista o uguale);
> - `test_same_event_tp_sl_is_sl`;
> - `test_gap_resolution`: disconnessione WS simulata con SL attraversato durante il gap;
> - `test_label_resolver_parity`: label live e label del dataset storico coincidono sugli stessi timestamp;
> - `test_l5_*` (`tests/test_registry.py`): un test per ogni condizione di L5 (finestre corte o diverse, OOS non provabile, valutazione assente o INCONCLUSA, GE NO-GO anche con shadow corto, G4 fallito, meno di 30 trade, modello senza trade con metriche definite, PnL non positivo, pareggi respinti, DD contro il champion saltato se il champion non ha trade, DD oltre il 5%, P2 saltato con champion non GO, quantile 0 respinto, Sharpe o DD OOS peggiori, anomalie, promozione con tutte le condizioni, elenco di tutti i motivi), `test_current_models_never_promoted`, `test_rollback_records_op_and_reason`, `test_aliases_without_op_still_load`, `test_cli_requires_reason`, `test_cli_rollback_says_effective_at_reload`; `tests/test_oos_compare.py` (confronto accoppiato); `tests/test_learner_promotion.py` (L5 nel learner: sfidante di prima del refit, finestra comune, end to end con challenger sintetico e una condizione violata per volta); `tests/test_rollback_advice.py` (raccomandazioni nel report, nessuna scrittura nel registry);
> - `test_resolve_mode`: tutte le combinazioni di config ed env;
> - `tests/test_drift_psi_v2.py` (formula a mano, Jeffreys senza tetto, 12 bin con code, minimo di valori finiti, equivalenza di p-value e soglia, FWER del minP, caricatore di calibrazione) e `tests/test_sim_p1f.py` (tabella di `decide`, pre-registrazione prima dei dati, seconda corsa rifiutata, determinismo, S0 con la vecchia funzione e i vecchi bordi, S6): obbligatori;
> - `test_alt_buy_blocked_when_btc_bar_of_the_minute_is_missing` e gli altri test `btc_context` di `tests/test_decision_engine.py`;
> - `certify --days` e i test del watch (`test_load_calibration_v3_rejected_loads_watch`, `test_watch_stat_equals_adopted_stat`, `test_l0_v3_rejected_*`, `test_feature_drift_not_covered_*`, `test_certify_days_*`): obbligatori;
> - `tests/test_drift_psi_v3.py` (standardizzazione, record `RULE_V3` uguale al documento di decisione, `k_h`, caricatore sotto due regole, diagnostica) e `tests/test_sim_p1f_v3.py` (tabella di `decide_v3`, pre-registrazione prima dei dati, corsa rifiutata se non congelata, seconda corsa rifiutata, famiglia J, finestre di D1h, determinismo): obbligatori.
>
> Operativita' locale: `docker compose up chronos` avvia il bot in dry con il named volume `chronos-state` e i bind mount `data/` e `models/` persistenti (sez. 0.5); `docker compose run --rm chronos python -m modules.dryrun.report --days 7` mostra lo stato di G-DRY-OPS e G-DRY-ECON. Il container deve sopravvivere a un riavvio del PC (`restart: unless-stopped`) e ripristinare le posizioni virtuali dal DB dry all'avvio: una posizione virtuale aperta prima del riavvio viene risolta con la procedura del gap.

---

## 6. CENTRO DI CONTROLLO — GUI opzionale (fase P7)

Scopo: vedere cosa fa l'agente (stato, andamento, decisioni, dati storici, modelli, gate) e intervenire solo per **ridurre o fermare** il rischio. La GUI e' facoltativa e l'agente non sa che esiste: produce i propri file come sempre, e una parte di essi (sez. 6.3) e' pensata per essere letta da fuori.

### 6.1 Architettura Concettuale

#### 6.1.1 Invarianti

| ID | Invariante | Come e' garantito |
|----|------------|-------------------|
| U1 | **Opzionalita'.** `docker compose up` avvia solo `chronos`, come oggi. La GUI parte solo con il profilo `ui`. | Tutti i servizi della GUI hanno `profiles: [ui]` (o `ui-test`); `chronos` non ha profili, ne' `depends_on`, ne' reti in comune con la GUI. Test `test_default_profile_starts_only_the_agent`. |
| U2 | **Indipendenza.** L'agente non importa codice della GUI, non fa health check verso di lei, non riceve chiamate di rete da lei e gira identico se la GUI e' spenta, cade o scrive spazzatura nella coda comandi. | Comunicazione solo tramite file su volumi; telemetria e coda comandi isolate dagli errori (6.3.5, 6.4.4). Test `test_agent_independent_of_ui`. |
| U3 | **Unico scrittore.** L'agente e' l'unico scrittore dei propri file (ledger SQLite, Parquet, JSONL, report, registry). La GUI li monta `:ro`, li legge con aperture brevi (apri, leggi, chiudi), non prende lock e non apre mai i DB SQLite vivi dell'agente. | Mount `:ro`; snapshot SQLite prodotti dall'agente (6.3.4); test `test_ui_services_mount_agent_data_read_only`. |
| U4 | **Fonte di verita'.** Il DB della GUI e' una cache derivata, ricostruibile da zero dai file dell'agente (salvo utenti, sessioni e audit della GUI). La GUI non ricalcola valori con significato decisionale (PnL di un trade, fee, probabilita', indicatori, esiti dei gate): li mostra come li ha scritti l'agente. Calcola solo aggregati di visualizzazione (conteggi, somme, drawdown dalla serie di equity, downsampling), etichettati come tali. | Regola di implementazione; prezzi, size e importi viaggiano come stringhe Decimal fino al browser. |
| U5 | **Nessun percorso d'ordine (C11).** La GUI non ha chiavi Coinbase (nemmeno View), non importa l'SDK, non ha accesso a Internet ne' al socket Docker e non puo' esprimere un ordine: deposita solo richieste di un elenco chiuso, che l'agente valida, puo' rifiutare ed esegue con i propri componenti (`RiskManager`, `ExecutionGateway`, `BalanceGuard`). | Rete `ui-internal` con `internal: true`; allowlist doppia (API e agente); tripwire della sez. 6.8. |
| U6 | **Il peggio e' fermarsi.** Ogni comando ammesso dalla GUI riduce o lascia invariato il rischio. Con il canale comandi compromesso il danno massimo e' "ingressi fermi" e "posizioni chiuse a mercato" (fee e spread di uscita), mai un acquisto, un aumento di size o di rischio, un cambio di modalita' o una ripresa dopo una pausa. | Sez. 6.4.6. |
| U7 | **Basso impatto.** La GUI non rallenta l'agente. | Limiti di CPU e memoria per ogni container, ingest a priorita' bassa, letture brevi, l'API non legge mai i file dell'agente; criterio di uscita della 6.9. |
| U8 | **Costo zero (C8).** Solo software libero in locale, nessun servizio esterno, nessuna dipendenza nuova nel `requirements.txt` del container `chronos`. | Appendice B. |

#### 6.1.2 Topologia e flusso dei dati

```
 browser (host) -> 127.0.0.1:8088 -> ui-web (nginx, SPA statica) --/api--> ui-api (FastAPI)
                                                                          |          |
                                                                   legge  |          | scrive file firmati
                                                                          v          v
 ui-ingest (Python, nice 10) ------------- scrive ------------> ui-db (PostgreSQL)   vol chronos-commands
      |                                                                                  |
      | legge (:ro), ogni file aperto e chiuso a ogni ciclo                              | legge (:ro)
      v                                                                                  v
 ./data            ./models          vol chronos-telemetry  <--- scrive ---  container chronos
 (bars, decisions, (registry, model  (status.json,                           (unico scrittore dei propri
  events, equity,   card, policy)     db/*.sqlite)                            file; nessuna rete in comune
  reports, labels)                                                            con la GUI)

 ui-db, ui-ingest, ui-api, ui-web: rete ui-internal (internal: true, niente Internet); solo ui-web anche su ui-edge.
```

1. **Lettura.** L'agente scrive i propri file come sempre; `ui-ingest` li rilegge (ciclo veloce 15 s, lento 5 min, sez. 6.2.1), li valida con i contratti (6.3.6) e li copia nel proprio PostgreSQL. `ui-api` legge solo PostgreSQL.
2. **Comandi.** Il browser chiama `POST /api/v1/commands`; `ui-api` controlla ruolo, CSRF, riautenticazione e allowlist, firma il comando e lo deposita in modo atomico nel volume `chronos-commands`. L'agente lo legge (`:ro`), lo valida di nuovo, lo esegue o lo rifiuta e scrive l'esito nel proprio DB e nei propri eventi; l'esito torna alla GUI per la via 1.

Perche' file su volume e non un'API dell'agente o un broker: (a) un endpoint HTTP nell'agente sarebbe una superficie di rete e una dipendenza di runtime; (b) un broker (Redis, NATS) sarebbe un servizio in piu' da cui l'agente dipenderebbe o che dovrebbe gestire in errore; (c) i file su volume rispettano C8 (stesso schema di `ModelRegistry` e `PositionLedger` dietro interfacce: in cloud diventano oggetti o code senza toccare la logica) e rendono la direzione dei permessi verificabile leggendo il compose (`:ro`).

Perche' snapshot e non lettura diretta dei DB SQLite dell'agente: un lettore esterno su un DB in WAL (a) con mount `:ro` non puo' creare ne' aggiornare il file `-shm` e ricade in una modalita' di lettura speciale di SQLite, delicata da verificare; (b) una transazione di lettura lunga del lettore impedisce al checkpoint di riportare il WAL all'inizio, quindi il WAL dell'agente crescerebbe per colpa della GUI. Lo snapshot (6.3.4) costa all'agente pochi millisecondi al minuto su DB di pochi MB ed elimina ogni accoppiamento.

#### 6.1.3 Scelte tecnologiche

| Strato | Scelta | Motivazione | Alternative scartate |
|--------|--------|-------------|----------------------|
| Database (`ui-db`) | PostgreSQL 17, immagine `postgres:17-alpine` (tag fissato per digest all'implementazione) | Servizio vero e separato, come richiesto; scrittore (ingest) e lettore (API) concorrenti senza i limiti a scrittore unico; `date_bin`, indici BRIN su `ts` e `jsonb` bastano per ~10 M righe di serie temporali; libero, 50-100 MB di RAM. | TimescaleDB (immagine piu' pesante, licenza a due livelli, inutile a questi volumi; lo schema resta compatibile per aggiungerla dopo); DuckDB e SQLite (embedded, un solo processo scrittore: non sono un servizio). |
| Backend (`ui-api`, `ui-ingest`) | Python 3.12; FastAPI + uvicorn (1 worker); SQLAlchemy 2 async + asyncpg; Alembic; pydantic v2; polars + pyarrow (solo ingest) | Stesso linguaggio e stile dell'agente (pydantic, structlog, mypy strict); FastAPI e' gia' nella spec per il decider; polars legge i Parquet dell'agente senza importarne il codice. Una sola immagine e due servizi: l'ingest e' l'unico processo che legge i file dell'agente, l'API tocca solo PostgreSQL e la coda comandi, con limiti di risorse separati. | Django (pesante per una API); Node lato server (secondo linguaggio); ingest come task dentro l'API (mescola limiti di risorse e mount). |
| Frontend (`ui-web`) | SPA statica React 18 + TypeScript + Vite; TanStack Query (polling e cache); `lightweight-charts` (candele, marker, equity); Apache ECharts con import modulare (calibrazione, istogrammi); servita da `nginxinc/nginx-unprivileged:alpine`, che fa anche da reverse proxy su `/api` | Nessun runtime Node in esercizio (solo nella build), circa 10 MB di RAM; pagina e API sulla stessa origine, quindi nessun CORS; `lightweight-charts` regge serie lunghe con marker di ingresso e uscita; React e' lo stack che l'implementatore conosce meglio. | Rendering lato server con HTMX/Jinja nel backend (niente frontend separato, contro la richiesta); Grafana (ottimo per serie temporali, non per comandi con conferme e audit; si puo' affiancare in P7b in sola lettura su `ui-db`). |
| Aggiornamento | Polling: stato ogni 5 s, liste ogni 30 s o su richiesta | Lo stato dell'agente cambia ogni 10 s (6.3.1): SSE o WebSocket non aggiungono nulla e sono una parte mobile in piu'. | SSE, WebSocket. |

### 6.2 Contenuti del centro di controllo

Fonti: `S` = `telemetry/status.json` (presente); `E` = `data/events/`; `Q` = `data/equity/`; `L` = snapshot del DB della modalita' (`positions`, `orders`, `fills`, `closed_trades`, `kill_switch_state`, `shadow_orders`, `ui_commands`); `N` = snapshot di `news.db`; `D` = `data/decisions/`; `B` = `data/bars/1m/` (Parquet); `R` = `data/reports/` (dry, learner, evaluate, backtest, parity, `feasibility.md`, `p0_soak.json`); `M` = `models/` (registry, model card, `policy.json`, `calibration.json`) e `data/datasets/` (`label_config.json`, manifest); `P` = `data/probe/coinbase_probe.json` (senza segreti). I codici `D1`-`D15` sono i prerequisiti della sez. 6.3.

| Vista | Contenuto | Fonte | Fase |
|-------|-----------|-------|------|
| V1 Stato | Modalita' (dry/live) e permessi effettivi della key; uptime, versione, eta' dello stato (banner "agente non risponde" oltre 120 s); salute dei feed WS (connessione, eta' dell'ultimo messaggio per canale e prodotto, riconnessioni e buchi di sequenza in 24 h, book `stale`) e REST; barre per prodotto (ultima `ts_bar_close`, ritardo, p50/p99 del giorno, barre `flat`/`flat_unproven`, `skipped_late`, ultimo `rest_revision_ratio`); kill switch per book e guardia; saldi in cache per book (totale, libero, riservato per valuta, eta' dello snapshot contro il limite di 120 s, `epoch`, fonte); fallback del decider; riconciliazione (drift in 24 h, riserve UNKNOWN); fee correnti e `label_fee_mismatch`; spazio disco; stato del canale comandi. | S, E | P7a |
| V2 Posizioni e ordini | Posizioni aperte per book: ingresso, size, SL, TP, ultimo prezzo, PnL non realizzato, distanza da SL/TP in ATR, ore di permanenza, scadenza dell'orizzonte `H`; ingressi pendenti con scadenza; ordini aperti (bracket); dust. | S, L | P7a |
| V3 Storico trade | Trade chiusi per book, con filtri (book, prodotto, periodo, motivo di uscita): ingresso, uscita, motivo (`TP`, `SL`, `SELL`, `HORIZON`, `WATCHDOG`, `MANUAL`), PnL lordo, fee, netto in quote e bps, R multiplo, durata, versione di modello e di fill model; export CSV. | L (`closed_trades`) | P7a |
| V4 Equity e drawdown | Equity e drawdown per book (live, shadow twin, champion, challenger, random_entry, buy_and_hold), sovrapponibili; fee cumulate; PnL giornaliero. | Q, R | P7a |
| V5 Decisioni | Per prodotto: serie di `p(BUY)`, `p_edge` contro `p_breakeven` e soglie (`tau_buy`, pavimento `p_breakeven + p_be_buffer`), regime (token `regime_pctile` e classe 0..4 di `regime_rule`; la classe del modello, `model_regime`, solo come diagnostica), decisione finale e motivo della policy (`thresholds_met`, `below_threshold`, `regime_veto`, `regime_unavailable`, `decider_unavailable`, ...); esito per book (intent oppure skip con motivo: `fee_floor`, `ev_negative`, `ev_buffer`, `below_min_size`, `kill_switch`, `insufficient_balance`, `balance_stale`, `entry_pending`); istogramma giornaliero dei motivi di skip; dettaglio di un `DecisionRecord` con lo stato canonico in forma leggibile. | D, E (`decision_outcome`) | P7a |
| V6 Grafico prezzi | Candele per prodotto con timeframe automatico (6.2.1); pannelli con gli indicatori **visti dal modello** (RSI 1h/6h/1d, ATR in bps, `bb_pos_1d`, `vol_z_1d`, ... letti dallo stato dei `DecisionRecord`, mai ricalcolati); marker di ingresso e di uscita con il motivo, linee SL/TP delle posizioni aperte, BUY scartati (opzionale). | B, D, L | P7a |
| V7 Dati in pancia | Per prodotto: prima e ultima barra, righe, copertura per mese, buchi oltre 60 min, minuti senza candela; registrazioni WS (giorni, dimensione); `label_config.json` completo con `sha256` e `holdout_start`; dataset (`manifest.json`); studio di fattibilita' (`feasibility.md` renderizzato); esito del soak P0. | B, R, M | P7a |
| V8 Modelli e gate | Registry (alias champion/challenger, versioni, manifest), model card, `policy.json`, calibrazione (ECE, Brier, logloss prima e dopo), curve di affidabilita', importanza delle feature, esiti di G0, G1, G2, GE (E1-E5), G3, G4 e verdetto, `cadence_sensitivity`; storico delle promozioni e degli eventi `model_version_changed`. | M, R (D1, D2), E | P7a (curve e importanza solo con D1/D2) |
| V9 Gate del dry run | G-DRY-OPS per criterio e per giorno; G-DRY-ECON (trade n/30, PF, DD, confronto con random_entry, ECE live); parita' L0 e live (`ParityReport`); revisioni REST; costi misurati (fill rate maker, spread mediano, latenza); quota di skip `fee_floor`; stato della rampa. | R | P7a |
| V10 Eventi e allarmi | Eventi filtrabili per tipo, gravita', book, prodotto; allarmi aperti (kill switch, `coverage_breach`, `balance_drift`, `DryRunViolation`, drift PSI, sorgenti news `DEGRADED`, feed stale); richieste respinte dal `BalanceGuard` con motivo e importi (`coverage_refused`); log ERROR dell'agente. | E | P7a |
| V11 Comandi e audit | Pulsanti dei comandi ammessi (6.4.2) con conferme; registro dei comandi (chi, quando, cosa, esito dell'agente, motivo del rifiuto); audit della GUI (login riusciti e falliti, export). | ui-db, L (`ui_commands`), E | P7a |
| V12 Sentiment e news | Notizie con fonte, asset, classe, probabilita', confidenza, rilevanza; serie `ewm`/`ewm_slow`/`mass` per asset e `MARKET`; Fear & Greed; stato delle sorgenti. | N, S | P7b |
| V13 Costi e fee | Fee pagate per book e giorno, quota maker/taker, slippage realizzato (L4, parity), fee tier e confronto con le fee del `label_config`. | L, R, P | P7b |
| V14 Backtest, dry e live a confronto | Per versione di modello: frequenza dei trade, win rate, media di `net_ret_bps`, PF, fill rate, mix dei motivi di uscita per backtest (fold di test e holdout), dry (champion) e live; parita' live/shadow twin per ordine. | R (D3), L | P7b |
| V15 Operativita' | Job notturni del learner (L0-L5: esito, durata, `skipped: insufficient_samples`); dimensione delle directory e previsione di riempimento del disco; retention. | R (D11), S | P7b |
| V16 Notifiche | Notifiche del browser sugli allarmi; canali esterni solo su decisione dell'utente (domanda 5 della 6.10). | E | P7b |

Altro in P7b: export CSV di tutte le tabelle; comando `reduce_position` (6.4.2); TOTP come secondo fattore; cruscotti Grafana in sola lettura su `ui-db`; indicatori per periodi fuori dalla retention delle decisioni (esportati dall'agente, mai ricalcolati dalla GUI).

#### 6.2.1 Dati pesanti: aggregazione, paginazione, limiti

- **Barre.** `ui-ingest` tiene in PostgreSQL le barre 1m degli ultimi 30 giorni e gli aggregati 15m, 1h, 6h, 1d di tutto lo storico (bucket UTC allineati all'epoca, 1d che chiude alle 00:00 UTC, come `modules/market/timeframes.py`). Si usano solo le candele REST salvate: nessuna barra piatta inventata, i buchi restano visibili. Ordine di grandezza: 3 anni x 3 prodotti, circa 320 k righe a 15m. `GET /api/v1/bars` sceglie il timeframe piu' fine con al massimo `max_points` punti (default 2 000, tetto 5 000); oltre i 30 giorni il minimo e' 15m.
- **Decisioni.** 1 440 al giorno per prodotto. Righe complete (stato in `jsonb`) per 90 giorni; oltre, solo aggregati orari (media e massimo di `p_edge`, conteggi per decisione finale e per motivo). Le serie su intervalli oltre i 2 giorni usano gli aggregati orari.
- **Equity.** Un campione al minuto per book (D8), servito con `date_bin` (ultimo valore e minimo del bucket) entro `max_points`.
- **Liste.** Paginazione a cursore (keyset su `(ts, id)`), `limit` <= 500; filtro di periodo obbligatorio sulle tabelle grandi; export CSV in streaming, al massimo 1 M righe e un export al minuto per utente.
- **Database.** Ruolo dell'API con `statement_timeout` di 5 s e sola lettura sulle tabelle dei dati; indici BRIN su `ts`, B-tree sulle chiavi di filtro.
- **Ingest.** Processo con `os.nice(10)`. Ciclo veloce ogni 15 s (stato, eventi, equity, decisioni) e lento ogni 5 min (Parquet, report, modelli, snapshot). Legge solo i file cambiati (dimensione, `mtime`, offset salvati in `ingest_cursor`). Ogni file si apre, si legge per intero o dall'offset e si chiude subito: nessun handle tenuto aperto, nessun `flock`. Una riga JSONL senza `\n` finale si ignora e si rilegge al ciclo successivo; un Parquet illeggibile (scrittura in corso) si ritenta al ciclo successivo; al massimo 50 MB letti per ciclo. Primo avvio: storico completo delle barre, 90 giorni di decisioni, 180 di eventi, a blocchi mensili con pause, per non saturare il disco condiviso con l'agente.
- **Retention in `ui-db`:** decisioni complete 90 giorni, eventi 180 giorni, barre 1m 30 giorni; il resto senza scadenza. La fonte resta nei file dell'agente.

### 6.3 Contratto agente -> GUI (prerequisiti nelle fasi P0-P3)

Tutto cio' che la GUI mostra come storia deve stare in un file append-only o in un DB dell'agente; `status.json` e' solo il presente. Cosi' una GUI spenta per una settimana, alla riaccensione, ricostruisce tutto. I componenti sotto sono parte dell'agente, girano anche senza GUI (servono da soli per diagnostica, healthcheck e audit) e non aggiungono dipendenze al `requirements.txt` (solo libreria standard: `json`, `sqlite3`, `hmac`, `os.replace`; i JSON Schema vengono da pydantic).

| ID | Fase | Componente dell'agente | Cosa deve produrre | Vista |
|----|------|------------------------|--------------------|-------|
| D1 | P1 | `training/evaluate.py` | `data/reports/evaluate/<model_version>.json`: esiti di G0, G1, G2, GE (E1-E5 con i valori), G3, G4, verdetto ed exit code, `cadence_sensitivity`, e i 15 bin di affidabilita' (`p_mean`, `freq`, `n`) di `edge` e della classe BUY per fold di test e holdout. Se `evaluate.py` scrive gia' un JSON, si aggiungono solo i campi mancanti. | V8 |
| D2 | P1 | `training/baseline_lgbm.py` | `feature_importance` (gain e split per feature, per i modelli `action`, `edge`, `vol_regime` del modello finale) nel `model_card.json`. | V8 |
| D3 | P1 | `training/backtest.py` / `evaluate.py` | `data/reports/backtest/<model_version>/{trades.parquet, equity.parquet, metrics.json}` per fold di test e holdout, cadenza 1 minuto. | V14 |
| D4 | P2 | `modules/telemetry/status.py` (`StatusWriter`) | `telemetry/status.json` ogni `telemetry.status_interval_s` (10 s), schema 6.3.1, scrittura atomica (file temporaneo nella stessa directory + `os.replace`). | V1, V2 |
| D5 | P2 | `modules/telemetry/events.py` (`EventLog`) | `data/events/YYYY-MM-DD.jsonl`, schema 6.3.2, emesso dai componenti della tabella 6.3.2; un processor `structlog` inoltra anche i log >= ERROR. | V5, V10 |
| D6 | P2 | `modules/telemetry/snapshot.py` (`DbSnapshotter`) | `telemetry/db/{chronos_dry,chronos_live,news}.sqlite` secondo la 6.3.4. | V2, V3, V11, V12 |
| D7 | P2 | `modules/ledger/positions.py` | Tabella `closed_trades` con lo schema `VirtualTrade` (sez. 5.2) in entrambi i DB, scritta alla chiusura di ogni trade di ogni book, `live` compreso; `exit.reason` in `TP`, `SL`, `SELL`, `HORIZON`, `WATCHDOG`, `MANUAL`. Se nel codice esiste gia' una tabella `virtual_trades`, si rinomina con una migrazione (nessun dato e' ancora stato rilasciato). | V3, V4 |
| D8 | P2/P3 | `StatusWriter` | `data/equity/YYYY-MM-DD.jsonl`, una riga per book ogni `telemetry.equity_interval_s` (60 s), schema 6.3.3, valori da `BalanceBook`/`VirtualAccount` (mai ricalcolati). | V4 |
| D9 | P2 | `modules/risk/kill_switch.py` | Guardia `manual_pause` (book o `*`, sez. 4.1.4); `observe_trade_closed` accetta `MANUAL`, neutro per la loss streak (6.4.4). | V1, V11 |
| D10 | P3 | `modules/control/` | Canale comandi della sez. 6.4. | V11 |
| D11 | P3 | `modules/dryrun/learner.py` | `data/reports/learner/YYYY-MM-DD.json`: per ogni step L0-L5 inizio, fine, esito (`ok`, `skipped: insufficient_samples`, `failed` con messaggio), artefatti prodotti. | V15 |
| D12 | P2 | `modules/telemetry/schemas.py` | Modelli pydantic di status, eventi, equity e comando; CLI `python -m modules.telemetry.schemas export ui/contracts/` che scrive i JSON Schema, il DDL SQLite degli snapshot (`sqlite_master` di DB vuoti creati con il codice dell'agente) e i vettori di test della firma dei comandi (`command_test_vectors.json`). | tutte |
| D13 | P2 | `modules/app.py` | Avvio di `StatusWriter`, `EventLog`, `DbSnapshotter` e `CommandInbox` nel `TaskGroup` con l'isolamento della 6.3.5; nessun import da `ui/`. | - |
| D14 | P2 | consumer del `RiskManager`, gateway | Evento `decision_outcome` per ogni coppia (decisione non HOLD, book) e per ogni `entry_pending`; evento `coverage_refused` a ogni `CoverageRefused` del `BalanceGuard`. | V5, V10 |
| D15 | P0 (verifica) | `BarStoreWriter`, backfill | Scrittura dei Parquet atomica (temporaneo + `os.replace`); un errore di `os.replace` (file aperto da un lettore sul bind mount NTFS) si ritenta al flush successivo senza perdere barre. Da verificare nel codice dopo la fine del soak di P0, senza toccare il codice mentre il soak gira. | V6, V7 |

Riusati senza modifiche: `data/heartbeat` (4.3 punto 13), `data/decisions/` (2.2.4), `data/live_labels/` e `data/reports/dry/` (5.2), `ParityReport` (5.3 punto 14), `models/registry/` (5.3 punto 12), `data/datasets/label_config.json`, `data/reports/feasibility.md`, `data/reports/p0_soak.json`, `data/probe/coinbase_probe.json`.

#### 6.3.1 Stato corrente (`telemetry/status.json`, `chronos.status.v1`)

```json
{
  "v": "chronos.status.v1",
  "written_at": "2026-10-20T09:15:10.120Z",
  "agent": {"version": "git:720663a", "started_at": "2026-10-19T07:00:02Z", "uptime_s": 94508, "mode": "dry",
            "books": ["champion", "challenger", "random_entry", "buy_and_hold"],
            "models": {"champion": "baseline-lgbm@2026-10-18.1", "challenger": null},
            "label_config_sha256": "a41c...", "api_key": {"can_view": true, "can_trade": false, "can_transfer": false}},
  "feeds": {"ws": {"connected": true, "reconnects_24h": 1, "seq_gaps_24h": 0,
                   "channels": {"level2": {"SOL-EUR": {"last_msg_age_s": 0.4, "book_stale": false}}}},
            "rest": {"last_ok_age_s": 12.0, "errors_1h": 0}},
  "bars": {"SOL-EUR": {"last_ts_bar_close": "2026-10-20T09:13:00Z", "last_lag_s": 120.3, "lag_s_p50_today": 120.2,
                       "lag_s_p99_today": 126.0, "flat_today": 12, "flat_unproven_today": 0, "fetch_failures_today": 0,
                       "skipped_late_today": 0, "rest_revision_ratio_last_audit": 0.0007, "complete_until": "2026-10-20T09:00:00Z"}},
  "kill_switch": [{"book": "*", "guard": "stale_data", "tripped_at": null, "until": null, "reason": null}],
  "balances": {"champion": {"source": "virtual", "snapshot_age_s": 0.0, "epoch": 412, "stale": false,
                            "currencies": {"EUR": {"total": "99.12", "free": "97.80", "reserved": "1.32"},
                                           "SOL": {"total": "0.024", "free": "0", "reserved": "0.024"}}}},
  "positions": [{"book": "champion", "product": "SOL-EUR", "side": "LONG", "base_qty": "0.024", "avg_entry": "92.03",
                 "sl": "85.70", "tp": "104.70", "opened_at": "2026-10-19T14:10:22Z", "horizon_at": "2026-11-03T14:10:22Z",
                 "mark": "93.10", "unreal_quote": "0.02", "bracket_order_id": "b2d9...", "dust_qty": "0"}],
  "pending_entries": [{"book": "champion", "product": "ETH-EUR", "client_order_id": "6f1c...", "limit_price": "3401.20",
                       "base_size": "0.0006", "submitted_at": "2026-10-20T09:05:00Z", "expires_at": "2026-10-20T09:35:00Z"}],
  "decider": {"backend": "baseline", "fallbacks_1h": 0,
              "last": {"SOL-EUR": {"ts_bar_close": "2026-10-20T09:13:00Z", "final": "HOLD", "reason": "regime_veto"}}},
  "reconcile": {"last_run_at": "2026-10-20T09:14:30Z", "balance_drift_24h": 0, "unknown_reservations": 0},
  "fees": {"maker_bps": 25, "taker_bps": 50, "source": "account", "age_s": 1200, "label_fee_mismatch": false},
  "control": {"ui_channel": "enabled", "local_channel": "enabled", "paused_books": [],
              "last_results": [{"command_id": "01JAD7Q9V5N2K8XG3M4T6R1PZB", "status": "applied", "reason": null,
                                "decided_at": "2026-10-20T09:15:04Z"}]},
  "telemetry": {"events_dropped_total": 0, "errors_1h": 0,
                "snapshots": {"ledger": {"at": "2026-10-20T09:15:00Z", "ms": 6, "bytes": 401408}}},
  "disk": {"mounts": {"data": {"free_bytes": 0, "used_bytes": 0}, "state": {"free_bytes": 0, "used_bytes": 0}},
           "sizes_bytes": {"data/bars": 0, "data/ws_recordings": 0, "data/decisions": 0}, "sizes_at": "2026-10-20T09:00:00Z"}
}
```

Regole: importi, prezzi e size come stringhe Decimal; timestamp ISO-8601 UTC con `Z`; un blocco il cui componente non e' attivo vale `null` (mai omesso); `positions` e `balances` sono letture dello stato in memoria del ledger e del `BalanceBook`, non calcoli paralleli; `sizes_bytes` si aggiorna una volta l'ora in un thread (percorrere le directory e' lento); i numeri dell'esempio sono illustrativi.

#### 6.3.2 Eventi (`data/events/YYYY-MM-DD.jsonl`, `chronos.event.v1`)

```json
{"v":"chronos.event.v1","event_id":"01JAD7R2...","ts":"2026-10-20T09:05:00.512Z","type":"coverage_refused","severity":"warning",
 "book":"champion","product":"SOL-EUR","data":{"decision_id":"01JAD7Q...","side":"BUY","order":"limit_limit_gtc",
 "reason":"insufficient_balance","required_quote":"2.25","free_quote":"1.10","snapshot_age_s":4.2}}
```

| Tipo | Gravita' | Emesso da | Campi di `data` |
|------|----------|-----------|-----------------|
| `agent_started`, `agent_stopping` | info | `app.py` | mode, books, version, permessi della key |
| `ws_reconnect`, `ws_gap` | warning | `ws_feed.py` | channel, product, start, end |
| `bar_flat_unproven`, `bar_late` | warning | `BarService`, `DecisionEngine` | product, ts, lag_s |
| `kill_switch_tripped`, `kill_switch_cleared` | warning, info | `KillSwitch` | guard, reason, until, origin (`auto`, `ui`, `local`) |
| `coverage_refused` | warning | gateway, sul `CoverageRefused` del guard | decision_id, side, order, reason, required, free, snapshot_age_s |
| `coverage_alarm`, `balance_drift` | error | `BalanceBook`, `LiveBalanceFeed` | currency, local, exchange, alarm |
| `decision_outcome` | info | consumer del `RiskManager` | decision_id, final, outcome (`intent`, `skip`, `entry_pending`, `exit`), skip_reason, intent_id, ev_bps, p_breakeven, base_size |
| `order_submitted`, `order_terminal`, `fill` | info | gateway | client_order_id, leg, side, price, size, fee, status |
| `position_opened`, `position_closed` | info | `PositionLedger` | trade_id, exit_reason, net_quote, net_bps |
| `decider_fallback`, `drift_alert` | warning | `DecisionEngine`, learner | reason, feature, psi |
| `model_version_changed` | info | `DecisionEngine` | alias, from, to |
| `learner_step`, `report_written` | info | learner, report | step, outcome, file |
| `source_degraded` | warning | `SourceRunner` | source |
| `dry_run_violation` | critical | `CoinbaseExchange` | method |
| `command_received`, `command_result` | info, warning | `CommandInbox` | command_id, channel, type, status, reason |
| `command_flood` | error | `CommandInbox` | pending_files |
| `telemetry_dropped` | warning | `EventLog` | count |
| `log_error` | error | processor `structlog` | event, logger, message (troncato a 2 KB) |

Nessun evento contiene chiavi, JWT, header di autenticazione o risposte grezze degli endpoint privati dell'exchange (test `test_events_never_contain_secrets`).

#### 6.3.3 Equity (`data/equity/YYYY-MM-DD.jsonl`, `chronos.equity.v1`)

```json
{"v":"chronos.equity.v1","ts":"2026-10-20T09:15:00Z","book":"champion","mode":"dry","equity":"100.31","quote_total":"98.10","quote_free":"96.70","exposure_quote":"2.21","unrealized_quote":"0.02","realized_today_quote":"0.00","fees_today_quote":"0.01","open_positions":1}
```

#### 6.3.4 Snapshot dei DB SQLite (`DbSnapshotter`)

- Ogni `telemetry.snapshot_interval_s.ledger` (60 s) per il DB della modalita' (e anche per `chronos_dry.db` in live, per lo shadow twin) e ogni `telemetry.snapshot_interval_s.news` (900 s) per `news.db`, in `asyncio.to_thread`:
  1. `src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)`; `dst = sqlite3.connect(tmp)`, con `tmp = telemetry/db/.<nome>.sqlite.tmp`;
  2. `src.backup(dst, pages=1024)`: in WAL legge senza bloccare lo scrittore;
  3. `dst.execute("PRAGMA journal_mode=DELETE")` e chiusura: lo snapshot non e' in WAL e non ha file `-wal`/`-shm`;
  4. `os.replace(tmp, telemetry/db/<nome>.sqlite)`.
- Uno snapshot non si modifica mai dopo la sostituzione: la GUI lo apre con `file:...?mode=ro&immutable=1` (nessun lock, nessun file accessorio). `chronos-telemetry` e' un named volume (filesystem Linux): `os.replace` con un lettore aperto e' sicuro, il lettore tiene il vecchio inode.
- Durata e dimensione di ogni snapshot vanno in `status.telemetry.snapshots`; oltre 2 s si logga un warning (il DB e' cresciuto e gli intervalli vanno rivisti).

#### 6.3.5 Isolamento degli errori e configurazione

- `StatusWriter`, `EventLog`, `DbSnapshotter` e `CommandInbox` non propagano mai eccezioni al `TaskGroup` di `app.py`: ogni ciclo sta in `try/except Exception`, con un log al massimo una volta al minuto per tipo di errore e il contatore `status.telemetry.errors_1h`. Si propaga solo `asyncio.CancelledError` (shutdown).
- `EventLog.emit()` e' sincrono, O(1) e non bloccante: `queue.Queue(maxsize=telemetry.events_queue_max).put_nowait`; con la coda piena l'evento si scarta e si incrementa `events_dropped_total`. Un thread scrittore svuota la coda e fa flush ogni secondo. Nessun componente di trading aspetta la telemetria.
- Disco pieno o volume non scrivibile: la telemetria degrada (contatori, warning) e il trading prosegue. Le guardie del kill switch non dipendono dalla telemetria.
- Con `telemetry.enabled: false` i tre componenti non partono e il comportamento di trading resta identico (test `test_agent_independent_of_ui`).

```yaml
telemetry:
  enabled: true
  dir: "telemetry"                       # named volume chronos-telemetry su /app/telemetry
  status_interval_s: 10
  equity_interval_s: 60
  events_dir: "data/events"
  equity_dir: "data/equity"
  events_queue_max: 10000
  events_retention_days: 180
  snapshot_interval_s: {ledger: 60, news: 900}
control:
  ui_inbox_dir: "commands/inbox"         # named volume chronos-commands, montato :ro; directory assente = canale ui spento
  local_inbox_dir: "state/control/local_inbox"
  ui_key_env: "CHRONOS_UI_COMMAND_KEY"   # variabile assente o < 32 byte = canale ui spento (la inbox non si legge)
  poll_s: 2
  max_age_s: 300
  max_future_s: 30
  max_file_bytes: 4096
  max_files_per_poll: 20
  max_pending_files: 1000                # oltre: canale ui sospeso, allarme command_flood
  close_rate_limit_per_h: 6              # solo canale ui
  flatten_min_interval_s: 1800           # solo canale ui
```

#### 6.3.6 Contratti

- I JSON Schema di status, eventi, equity e comando stanno in `ui/contracts/`, generati dall'agente (D12). La GUI non importa `modules.*` e l'agente non importa `ui/`. Un cambio di schema e' una nuova `v` (`chronos.status.v2`, ...); l'ingest accetta le versioni che conosce, conserva le altre come grezze con un warning e non si ferma.
- Le tabelle lette dagli snapshot (`positions`, `orders`, `fills`, `closed_trades`, `kill_switch_state`, `shadow_orders`, `ui_commands`, `news_items`, `sentiment_events`) sono un contratto: rinominare o togliere una colonna richiede di aggiornare l'ingest nello stesso commit. L'ingest si prova su DB creati con il DDL esportato in `ui/contracts/sqlite/`.

### 6.4 Canale comandi

#### 6.4.1 Vista e comandi

- **Vista** (default, ruolo `viewer`): tutto il contenuto della sez. 6.2. Nessuna scrittura verso l'agente.
- **Comandi** (ruolo `operator`): solo le azioni della tabella 6.4.2, che riducono il rischio o non toccano il denaro. Un comando non e' un ordine: e' una richiesta che l'agente valida, puo' rifiutare ed esegue con i componenti del trading automatico.

#### 6.4.2 Comandi ammessi e vietati

| Comando | Parametri | Effetto nell'agente | Perche' non aumenta il rischio | Conferme nella GUI |
|---------|-----------|---------------------|--------------------------------|--------------------|
| `pause_entries` | `book` (nome o `*`), `reason`, `until` opzionale (<= 30 giorni) | Guardia `manual_pause` del book (o globale) in `kill_switch_state`: nessun nuovo ingresso; uscite, bracket, watchdog e uscita per orizzonte restano attivi. | Toglie solo ingressi. | Dialogo di conferma. |
| `cancel_pending_entry` | `book`, `product`, `reason` | `ExecutionGateway.cancel` dell'ingresso pendente; la riserva si libera solo alla conferma terminale (4.1.6). Un eventuale fill parziale segue il percorso normale (bracket standalone o dust). | Annulla un acquisto non ancora eseguito. | Dialogo di conferma. |
| `close_position` | `book`, `product`, `reason` | `Decision` SELL sintetica con `reason="manual"` (senza probabilita') -> `RiskManager.plan_exit` -> `ExecutionGateway.submit_exit` (cancellazione del bracket con conferma, limit marketable, fallback `market_market_ioc`), ogni ordine autorizzato dal `BalanceGuard`; `client_order_id = uuid5(NAMESPACE_CHRONOS, f"{command_id}:manual_exit")`. | Vende solo base posseduta da Chronos (C1, C11) e converte la posizione in quote. Costo: fee taker e spread di uscita. | Nome del prodotto digitato + riautenticazione (password negli ultimi 5 minuti). |
| `flatten_all` | `book` (nome o `*`), `reason` | Nell'ordine: `pause_entries(book)`, `cancel_pending_entry` per ogni ingresso pendente, `close_position` per ogni posizione LONG del book. | Composizione dei tre comandi precedenti. | Come `close_position`, con la parola `FLATTEN` digitata. |

Vietati dalla GUI (assenti dall'allowlist del canale `ui`: l'API li rifiuta con 422 senza scrivere nulla, l'agente con `not_allowed`): qualunque acquisto o ordine manuale libero; modifica di size, rischio, soglie, `max_order_quote`, gradino della rampa o di qualunque chiave di config; `resume_entries` e reset di qualunque guardia; disattivazione del kill switch; cambio di modalita' dry/live in entrambe le direzioni; promozione o rollback di modelli; avvio o arresto del processo; qualunque operazione sui fondi.

Motivazioni dei divieti principali:
- **Modalita' dry/live:** C9 la lega a due atti sull'host (config **e** `CHRONOS_LIVE_CONFIRM`) e a un riavvio. Un canale remoto che abilita ordini reali e' proprio l'escalation da impedire. Anche live -> dry e' escluso: richiede un riavvio (la GUI non ha il socket Docker) e l'effetto "smetti di aprire" si ottiene subito con `pause_entries(live)`, senza abbandonare la gestione delle posizioni reali.
- **Ripresa dopo una pausa:** annullerebbe una riduzione di rischio decisa da una persona, e con il canale compromesso un attaccante potrebbe riattivare un book in pausa. Si riprende solo dal canale locale (6.4.3). Domanda 1 della 6.10.
- **Promozione e rollback:** cambiano il comportamento del modello, quindi il rischio; li governano la regola L5 e la CLI del registry (5.3 punto 12). Domanda 4.
- **`reduce_position` (P7b):** riduce il rischio ed e' ammissibile, ma richiede cancellare il bracket, vendere una parte e ripiazzare un bracket standalone sulla parte restante con `base_size = floor_to(min(restante, sellable))`, tutto tramite gateway e guard, con le regole del dust e dei minimi vendibili: va specificato a parte.

#### 6.4.3 Due canali, due livelli di privilegio

| Canale | Chi scrive | Dove | Allowlist | Firma |
|--------|------------|------|-----------|-------|
| `ui` | solo `ui-api` | volume `chronos-commands`, montato `:ro` nell'agente | tabella 6.4.2 | HMAC-SHA256 obbligatoria con `CHRONOS_UI_COMMAND_KEY` |
| `local` | CLI dell'agente: `docker compose exec chronos python -m modules.control.cli <comando> ...` | `state/control/local_inbox/` (volume `chronos-state`, mai montato nella GUI) | tabella 6.4.2 + `resume_entries(book)` (toglie solo `manual_pause`) + `reset_guard(book, guard="coverage_breach")` (il reset manuale richiesto dalla 4.1.4) | nessuna: chi puo' eseguire `docker compose exec` controlla gia' l'host |

Il privilegio dipende dal canale, mai dal contenuto: un `resume_entries` nella inbox `ui` e' rifiutato anche se firmato correttamente. L'agente cancella i file del canale `local` dopo l'elaborazione (la directory e' sua); non puo' cancellare quelli del canale `ui` (`:ro`): li rimuove `ui-api` quando conosce l'esito, o dopo `max_age_s + 60` s. Per fermare il processo resta `docker compose stop chronos` dall'host.

#### 6.4.4 Esecuzione nell'agente

`CommandInbox`, un task del `TaskGroup`, ogni `control.poll_s`:
1. Il canale `ui` e' attivo solo se esiste `control.ui_inbox_dir` **e** la variabile `control.ui_key_env` contiene almeno 32 byte; altrimenti la directory non viene nemmeno letta e `status.control.ui_channel = "disabled"`.
2. Considera solo file regolari (niente link simbolici ne' directory) con nome `^[0-9A-HJKMNP-TV-Z]{26}\.json$`; ignora `.part` e i file oltre `max_file_bytes`; elabora al massimo `max_files_per_poll` file per ciclo, in ordine di nome. Oltre `max_pending_files` file nel canale `ui`: canale sospeso, allarme `command_flood`, ripresa automatica quando il numero scende.
3. Valida in quest'ordine, fermandosi al primo errore: JSON e schema `chronos.command.v1` (pydantic, `extra="forbid"`) -> `bad_schema`; `channel` uguale al canale da cui arriva il file -> `bad_schema`; firma (`hmac.compare_digest`, solo `ui`) -> `bad_signature`; `issued_at` non piu' vecchio di `max_age_s` e non oltre `max_future_s` nel futuro -> `expired` / `future_dated`; `command_id` gia' presente in `ui_commands` -> `duplicate` (nessun effetto: idempotenza); tipo nell'allowlist del canale -> `not_allowed`; `book` esistente (o `*` dove ammesso) e `product` nel `SpotRegistry` -> `unknown_book` / `unknown_product`; limiti di frequenza -> `rate_limited`; precondizioni (posizione LONG per `close_position`, ingresso pendente per `cancel_pending_entry`) -> `nothing_to_do`.
4. Scrive la riga in `ui_commands` (DB della modalita'): `command_id TEXT PRIMARY KEY, channel, type, book, product, reason, issued_by, issued_at, received_at, decided_at, status ('applied' | 'rejected' | 'failed'), status_reason, payload_sha256`; poi esegue con `CommandExecutor` ed emette `command_result`.
5. `CommandExecutor` usa solo: `KillSwitch.manual_pause`; `KillSwitch.manual_resume` e `KillSwitch.reset_guard("coverage_breach")` (solo canale `local`); `ExecutionGateway.cancel`; `RiskManager.plan_exit` + `ExecutionGateway.submit_exit`. Non chiama mai `plan_entry`, `submit_entry_with_bracket`, `BalanceGuard.authorize` direttamente, `create_order`, `build_order_body` e non nomina `CoverageTicket` (tripwire `test_control_package_cannot_buy`).

Regole di rischio:
- Canale `ui`: al massimo `close_rate_limit_per_h` (6) `close_position` l'ora e un `flatten_all` ogni `flatten_min_interval_s` (30 min). Le chiusure interne a un `flatten_all` non consumano il limite di `close_position`: il book e' gia' in pausa e senza il canale locale non puo' riaprire, quindi non c'e' ciclo apri-chiudi. Il canale `local` non ha limiti di frequenza.
- Esito `MANUAL`: entra nel PnL, nel daily loss e nel drawdown come ogni chiusura; per la loss streak e' **neutro** (non incrementa e non azzera il conteggio), altrimenti chiudere a mano un trade in perdita azzererebbe la guardia e aumenterebbe il rischio.
- Gate: il report conta a parte i trade con uscita `MANUAL` (`manual_exits`) e li esclude dal numero di trade e dal profit factor di G-DRY-ECON e delle condizioni della rampa (5.1.7), perche' un intervento umano non e' evidenza dell'edge del modello; restano nel PnL netto e nel drawdown (scelta prudente). Default dell'architetto, rivedibile dal `trader-quant`.
- Parita' delle decisioni (G-DRY-OPS): un comando applicato su (book, prodotto) cambia lo stato di posizione rispetto al replay; per quella coppia i minuti dal comando alla fine del giorno UTC sono esclusi e contati in `parity.excluded_manual`. Il replay non rilegge mai la coda comandi.
- `manual_pause` non conta come "kill switch scattato per bug" in G-DRY-OPS e non e' downtime.
- In live, `close_position` e `flatten_all` sul book `live` passano allo shadow twin con lo stesso intent, come ogni ordine reale (5.1.7), cosi' il `ParityReport` resta confrontabile.

#### 6.4.5 Formato del comando (`chronos.command.v1`)

```json
{
  "v": "chronos.command.v1",
  "command_id": "01JAD7Q9V5N2K8XG3M4T6R1PZB",
  "channel": "ui",
  "type": "close_position",
  "book": "champion",
  "product": "SOL-EUR",
  "until": null,
  "reason": "chiusura manuale prima di un annuncio",
  "issued_by": "fabiano",
  "issued_at": "2026-10-20T09:15:02.118Z",
  "sig": "<64 caratteri esadecimali>"
}
```

`sig = hex(HMAC-SHA256(key, canonical))`, con `canonical` = JSON UTF-8 di tutti i campi tranne `sig`, chiavi ordinate, `separators=(",", ":")`. `reason`: al massimo 200 caratteri stampabili. `ui-api` scrive `<command_id>.json.part` e lo rinomina in `<command_id>.json` sullo stesso volume (atomico); il nome del file coincide con `command_id`. La chiave si genera con `python -c "import secrets; print(secrets.token_hex(32))"`.

#### 6.4.6 Canale compromesso: caso peggiore

| Scenario | Cosa puo' fare | Cosa non puo' fare |
|----------|----------------|--------------------|
| Scrittura nel volume `chronos-commands` senza chiave HMAC | Nulla: ogni file e' `bad_signature`; un flood sospende solo il canale `ui` (allarme). | Rallentare il trading: la inbox si legge con limiti per ciclo, in un task isolato. |
| Chiave HMAC rubata o `ui-api` compromesso | Mettere in pausa i book; annullare ingressi pendenti; chiudere posizioni (al massimo 6 l'ora, oppure un `flatten_all` ogni 30 minuti, dopo il quale i book restano in pausa). Danno massimo: occasioni perse, fee e spread di uscita (circa 55 bps per posizione con le fee attuali) ed eventuale perdita realizzata prima del bracket. | Comprare; aumentare size o rischio; riprendere un book in pausa; resettare guardie; cambiare modalita' o modello; spostare fondi (nessuna key ha il permesso Transfer, sez. 0.5); raggiungere Coinbase (rete `internal`); leggere le chiavi Coinbase (non sono nei container della GUI). |
| `ui-db` o `ui-ingest` compromessi | Mostrare dati falsi nella GUI. | Agire sull'agente: non scrivono nella inbox. La verita' resta nei file e nei report dell'agente. |
| Account della GUI rubato | Come la riga precedente, entro i limiti dell'API e con riautenticazione per le chiusure. | Come la riga precedente. |

Revoca: togliere `CHRONOS_UI_COMMAND_KEY` dall'ambiente e ricreare il container dell'agente (`docker compose up -d chronos`); il canale `ui` si spegne, la vista resta.

### 6.5 Sicurezza di backend e frontend

- **Rete.** `ui-db`, `ui-ingest`, `ui-api` solo sulla rete `ui-internal` (`internal: true`: niente Internet, nessuna porta pubblicata). `ui-web` su `ui-internal` e `ui-edge`; unica porta pubblicata `127.0.0.1:${CHRONOS_UI_PORT:-8088}:8080`. `chronos` non e' su nessuna delle due reti. L'accesso fuori da localhost e' una decisione dell'utente (domanda 3) e richiede TLS e secondo fattore.
- **Autenticazione obbligatoria** su ogni endpoint tranne `GET /api/v1/health` e `POST /api/v1/auth/login`. Utenti in `ui-db` con hash argon2id; il primo si crea con `docker compose --profile ui run --rm ui-api python -m chronos_ui.manage create-user --role operator` (password da stdin, mai da argomento). Ruoli `viewer` e `operator`.
- **Sessioni.** Token casuale di 256 bit nel cookie `chronos_ui_session` (`HttpOnly`, `SameSite=Strict`, `Path=/api`, `Secure` quando c'e' TLS); in DB solo lo SHA-256 del token; scadenza per inattivita' 30 min, assoluta 12 h; il logout la invalida lato server.
- **CSRF.** Double submit: cookie `chronos_ui_csrf` (non `HttpOnly`) uguale all'header `X-CSRF-Token` su ogni richiesta che non sia GET; inoltre `Origin` obbligatorio e uguale a `CHRONOS_UI_ORIGIN` (oppure `Sec-Fetch-Site: same-origin`).
- **CORS.** Nessun middleware CORS: nessuna risposta contiene `Access-Control-Allow-Origin`; pagina e API stanno sulla stessa origine tramite nginx.
- **Rate limit** (in memoria, un solo worker): login 5 tentativi in 15 min per utente e per IP, poi blocco di 15 min; API 20 richieste/s per sessione; comandi 10/min per utente; export 1/min.
- **Riautenticazione** per `close_position` e `flatten_all`: `POST /api/v1/auth/reauth` con la password, valida 5 minuti.
- **Audit della GUI** (`ui-db`, tabella `ui_audit`): login riusciti e falliti, logout, creazione utenti, comandi inviati (con `command_id`), export. Mai password o token.
- **Segreti.** Il frontend non contiene segreti (build statica senza variabili d'ambiente; test sul bundle). `ui-api`: password del ruolo `ui_api`, `CHRONOS_UI_COMMAND_KEY`; `ui-ingest`: password del ruolo `ui_ingest`; `ui-db`: password del proprietario e dei due ruoli. Nessuna variabile `COINBASE_*` e nessuna key View: cio' che la GUI mostra dell'account (fee, permessi, saldi) arriva gia' dall'agente (`status.json`, probe), quindi una key in piu' sarebbe solo superficie d'attacco. I file `ui/env/{db,api,ingest}.env` (ignorati da git, con i rispettivi `.example`) sono caricati con `env_file` solo dal servizio corrispondente; `CHRONOS_UI_COMMAND_KEY` arriva a `chronos` e a `ui-api` per interpolazione dal `.env` della radice e a nessun altro servizio.
- **Privilegi in PostgreSQL.** Ruolo `ui_ingest`: scrittura sulle tabelle dei dati. Ruolo `ui_api`: lettura sulle tabelle dei dati, scrittura solo su `users`, `sessions`, `ui_audit`, `ui_command_log`, `statement_timeout` 5 s.
- **Container.** Utente non root, `read_only: true` con `tmpfs: /tmp`, `cap_drop: [ALL]`, `security_opt: ["no-new-privileges:true"]`, nessun mount di `/var/run/docker.sock`.
- **Header** (nginx): `Content-Security-Policy: default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`.

### 6.6 Docker compose e layout dei file

```
ui/
  README.md                      # avvio, creazione utente, ricostruzione di ui-db, revoca della chiave
  env/                           # db.env, api.env, ingest.env (ignorati da git) + *.env.example
  contracts/                     # generato dall'agente (D12): *.schema.json, sqlite/*.sql, command_test_vectors.json
  db/init/00-roles.sh            # crea i ruoli ui_ingest e ui_api con le password da env
  backend/
    Dockerfile                   # python:3.12-slim, utente non root, crea /commands/inbox con proprietario non root
    requirements.txt             # Appendice B, sezione GUI; mai coinbase-advanced-py
    pyproject.toml               # mypy strict, ruff, pytest
    openapi.json                 # schema dell'API versionato (snapshot)
    alembic/
    chronos_ui/
      settings.py                # pydantic-settings, solo env
      api/                       # main.py, un router per vista, dipendenze (auth, csrf, rate limit)
      auth/                      # utenti, sessioni, argon2, reauth
      commands/                  # allowlist, firma HMAC, scrittura atomica, pulizia della inbox
      ingest/                    # __main__.py; un lettore per sorgente: status, events, equity, decisions, bars, snapshots, reports, models
      db/                        # modelli SQLAlchemy
      manage.py                  # create-user, rebuild (svuota le tabelle derivate e reingerisce)
    tests/
  frontend/
    Dockerfile                   # build con node:22-alpine -> nginxinc/nginx-unprivileged:alpine
    nginx.conf                   # SPA, proxy /api -> ui-api:8000, header di sicurezza
    package.json, package-lock.json, vite.config.ts, tsconfig.json
    src/                         # pagine V1-V11, client API generato da openapi.json
```

Lato agente i file nuovi sono `modules/telemetry/{__init__,schemas,status,events,snapshot}.py` e `modules/control/{__init__,commands,inbox,executor,cli}.py`. Il servizio `chronos` cambia solo per due volumi e una variabile:

```yaml
services:
  chronos:
    # ... invariato ...
    volumes:
      # ... volumi esistenti ...
      - chronos-telemetry:/app/telemetry          # scritto solo dall'agente (sez. 6.3)
      - chronos-commands:/app/commands:ro         # coda comandi: l'agente la legge soltanto (sez. 6.4)
    environment:
      # ... esistenti ...
      - CHRONOS_UI_COMMAND_KEY=${CHRONOS_UI_COMMAND_KEY:-}   # vuota = canale ui spento

  test:
    # ... invariato, piu' i mount per tests/test_ui_isolation.py ...
    volumes:
      - ./ui:/app/ui:ro
      - ./docker-compose.yml:/app/docker-compose.yml:ro

  ui-db:
    image: postgres:17-alpine
    profiles: [ui]
    env_file: [ui/env/db.env]
    command: ["postgres", "-c", "shared_buffers=128MB", "-c", "max_connections=30"]
    volumes:
      - chronos-ui-db:/var/lib/postgresql/data
      - ./ui/db/init:/docker-entrypoint-initdb.d:ro
    networks: [ui-internal]
    healthcheck: {test: ["CMD-SHELL", "pg_isready -U $${POSTGRES_USER}"], interval: 10s, timeout: 5s, retries: 5}
    mem_limit: 512m
    cpus: 0.5
    restart: unless-stopped

  ui-ingest:
    build: ./ui/backend
    profiles: [ui]
    command: ["python", "-m", "chronos_ui.ingest"]
    env_file: [ui/env/ingest.env]
    volumes:
      - ./data:/agent/data:ro
      - ./models:/agent/models:ro
      - chronos-telemetry:/agent/telemetry:ro
    networks: [ui-internal]
    depends_on: {ui-db: {condition: service_healthy}}
    read_only: true
    tmpfs: [/tmp]
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    mem_limit: 768m
    cpus: 0.5
    cpu_shares: 256
    restart: unless-stopped

  ui-api:
    build: ./ui/backend
    profiles: [ui]
    command: ["uvicorn", "chronos_ui.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
    env_file: [ui/env/api.env]
    environment:
      - CHRONOS_UI_COMMAND_KEY=${CHRONOS_UI_COMMAND_KEY:-}   # vuota = la GUI disabilita i comandi
    volumes:
      - chronos-commands:/commands               # unico volume scrivibile condiviso con l'agente
    networks: [ui-internal]
    depends_on: {ui-db: {condition: service_healthy}}
    read_only: true
    tmpfs: [/tmp]
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    mem_limit: 384m
    cpus: 0.5
    cpu_shares: 256
    restart: unless-stopped

  ui-web:
    build: ./ui/frontend
    profiles: [ui]
    ports: ["127.0.0.1:${CHRONOS_UI_PORT:-8088}:8080"]
    networks: [ui-internal, ui-edge]
    depends_on: [ui-api]
    read_only: true
    tmpfs: [/tmp]
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    mem_limit: 64m
    cpus: 0.25
    restart: unless-stopped

  ui-db-test:
    image: postgres:17-alpine
    profiles: [ui-test]
    environment: [POSTGRES_USER=test, POSTGRES_PASSWORD=test, POSTGRES_DB=test]
    tmpfs: [/var/lib/postgresql/data]
    networks: [ui-internal]
    healthcheck: {test: ["CMD-SHELL", "pg_isready -U test"], interval: 5s, timeout: 5s, retries: 10}

  ui-test:
    build: ./ui/backend
    profiles: [ui-test]
    command: ["pytest", "-q", "tests"]
    environment: [UI_TEST_DATABASE_URL=postgresql+asyncpg://test:test@ui-db-test:5432/test]
    networks: [ui-internal]
    depends_on: {ui-db-test: {condition: service_healthy}}

networks:
  ui-internal: {internal: true}
  ui-edge: {}

volumes:
  chronos-state:
  chronos-telemetry:
  chronos-commands:
  chronos-ui-db:
```

Note: il volume `chronos-commands` nasce vuoto; al primo mount in `ui-api` Docker vi copia `/commands/inbox` dell'immagine con il proprietario non root. `ui-api` all'avvio verifica di poter scrivere in `/commands/inbox`: se non puo', parte con i comandi disabilitati e un messaggio esplicito (mai un ciclo di riavvii). Il `.dockerignore` della radice esclude `ui/`: l'immagine dell'agente non contiene il codice della GUI (i contesti di build della GUI sono `./ui/backend` e `./ui/frontend`).

Uso:
- Solo agente, come oggi: `docker compose up -d`.
- Agente e GUI: `docker compose --profile ui up -d`.
- Solo la GUI, con l'agente gia' attivo o spento: `docker compose --profile ui up -d ui-web ui-ingest` (porta su anche `ui-api` e `ui-db`).
- Fermare la GUI senza toccare l'agente: `docker compose --profile ui stop ui-web ui-api ui-ingest ui-db`. Attenzione: `docker compose --profile ui down` ferma anche `chronos`.
- Ricostruire la cache: `docker compose --profile ui run --rm ui-ingest python -m chronos_ui.manage rebuild`.
- Test della GUI: `docker compose --profile ui-test run --rm ui-test`.

### 6.7 Linee Guida per l'implementazione in Sonnet 5.5

> PROMPT — Centro di controllo (P7)
>
> Implementa la fase P7 di Chronos v2 nell'ordine della sez. 6.9. Regole generali: la GUI sta tutta sotto `ui/` e non importa mai `modules`, `training`, `script` ne' l'SDK `coinbase`; l'agente non importa mai `ui/` (l'unica eccezione e' la CLI di D12, che scrive in `ui/contracts/`); la GUI non ricalcola valori decisionali (U4); prezzi, size e importi restano stringhe Decimal dal file al browser; Python 3.12, pydantic v2, mypy strict, ruff, structlog in JSON.
>
> A. Agente (dentro P2/P3, file nominati nella sez. 6.3 e 6.4):
> 1. `modules/telemetry/schemas.py` (D12): modelli `StatusV1`, `EventV1`, `EquityV1`, `CommandV1` e la funzione `canonical_command_bytes(cmd) -> bytes` usata sia per firmare sia per verificare; CLI `export` con JSON Schema, DDL SQLite e vettori di test della firma; test `test_contract_schemas_up_to_date`.
> 2. `modules/telemetry/events.py` (D5): `EventLog.emit(type, severity, *, book=None, product=None, **data)` non bloccante (6.3.5), thread scrittore, rotazione giornaliera, retention; processor `structlog` per i log >= ERROR. Collega le emissioni della tabella 6.3.2 nei componenti esistenti con una sola riga ciascuna, senza cambiarne la logica.
> 3. `modules/telemetry/status.py` (D4, D8): `StatusWriter` che compone `status.json` leggendo i componenti tramite metodi di sola lettura (`snapshot()`/`view()`), scrittura atomica; serie di equity ogni 60 s.
> 4. `modules/telemetry/snapshot.py` (D6): procedura della 6.3.4.
> 5. `modules/ledger/positions.py` (D7): tabella `closed_trades`. `modules/risk/kill_switch.py` (D9): guardia `manual_pause` con `origin`, `manual_resume`, `reset_guard("coverage_breach")`, `MANUAL` neutro per la loss streak.
> 6. `modules/control/commands.py` (allowlist dei due canali come `frozenset`, validazione della 6.4.4 punto 3), `inbox.py` (`CommandInbox`), `executor.py` (`CommandExecutor`), `cli.py` (canale locale: `pause`, `resume`, `reset-guard`, `cancel`, `close`, `flatten`; scrive file nella `local_inbox` e attende l'esito fino a 30 s stampandolo).
> 7. `modules/app.py` (D13): avvio dei task con isolamento degli errori; tabella `ui_commands` nel DB della modalita'.
> 8. `docker-compose.yml`: volumi e variabile del servizio `chronos`, mount del servizio `test`, servizi `ui-*`, reti e volumi come nella sez. 6.6; `.dockerignore` della radice con `ui/` (crearlo se manca).
>
> B. Backend (`ui/backend/`):
> 1. `settings.py`, modelli SQLAlchemy e migrazioni Alembic: `ingest_cursor`, `agent_status` (ultimo stato + campioni al minuto), `equity_ts`, `bars_1m` (30 giorni), `bars_agg` (15m, 1h, 6h, 1d), `bar_coverage`, `decisions` (90 giorni), `decisions_hourly`, `decision_outcomes`, `positions`, `orders`, `fills`, `closed_trades`, `kill_switch_state`, `events`, `models`, `gate_results`, `dry_reports`, `label_configs`, `datasets`, `news`, `sentiment` (P7b), `users`, `sessions`, `ui_audit`, `ui_command_log`.
> 2. `ingest/`: un lettore per sorgente con cursore, upsert idempotente sulle chiavi naturali (`event_id`, `decision_id`, `trade_id`, `(product, tf, ts)`, ...), validazione con `ui/contracts/`, regole di lettura della 6.2.1, `os.nice(10)` all'avvio. Il lettore dei Parquet legge i byte del file in una sola chiamata, chiude il file e decodifica dalla memoria.
> 3. `api/`: endpoint versionati `/api/v1/...`: `auth/{login,logout,me,reauth}`, `status`, `books`, `positions`, `orders`, `trades` (+ `trades.csv`), `equity`, `decisions` (+ `/{decision_id}`, `/summary`), `bars`, `markers`, `data/{coverage,label-config,datasets}`, `models` (+ `/{version}`), `gates/dry`, `reports/dry/{date}`, `events`, `commands` (GET audit, POST invio), `health`. Paginazione, `max_points` e limiti della 6.2.1; sicurezza della 6.5.
> 4. `commands/`: allowlist (copia difensiva: l'autorita' resta l'agente), firma con la stessa forma canonica verificata sui vettori di test di D12, scrittura `.part` + rinomina, pulizia.
> 5. `openapi.json` versionato e test di snapshot.
>
> C. Frontend (`ui/frontend/`): pagine V1-V11; polling (stato 5 s); banner "agente non risponde" se `written_at` ha piu' di 120 s e "comandi disabilitati" se il canale `ui` e' spento; orari in UTC con selettore Europe/Rome; numeri monetari mostrati dalla stringa Decimal senza conversioni in float; dialoghi di conferma della 6.4.2; client generato da `openapi.json` con `openapi-typescript`; nessuna variabile d'ambiente nella build.
>
> D. Documentazione: `ui/README.md` con i comandi della sez. 6.6 e la procedura di revoca della chiave.

### 6.8 Test e tripwire

`tests/test_c11_wiring.py` non si modifica: `COINBASE_IMPORTERS` resta di quattro file. La GUI ne resta fuori perche' vive fuori da `modules/`, che e' il perimetro di quella scansione; la copre il nuovo `tests/test_ui_isolation.py`, che importa da `tests/test_c11_wiring.py` `imports_coinbase`, `ORDER_METHOD`, `FORBIDDEN_TOKENS` e `FORBIDDEN_IDENTIFIERS` (importando solo questi nomi, nessun test viene raccolto due volte). In `tests/test_obligations.py` si aggiungono gli obblighi: quando esiste `modules/telemetry/` devono esistere per nome i test di `tests/test_telemetry.py`; quando esiste `modules/control/` quelli di `tests/test_control_commands.py`; quando esistono `modules/app.py` e `modules/dryrun/replay.py`, `test_agent_independent_of_ui`.

**Agente** (`docker compose --profile test`):

| File | Test | Cosa verifica |
|------|------|---------------|
| `tests/test_ui_isolation.py` | `test_default_profile_starts_only_the_agent` | Nel compose i servizi senza `profiles` sono esattamente `{chronos}`; ogni servizio `ui-*` ha profilo `ui` o `ui-test`. |
| | `test_agent_does_not_depend_on_ui` | `chronos` senza `depends_on`, `links` e reti `ui-*`; nessun file di `modules/`, `training/`, `script/` o `main.py` importa `chronos_ui`; `requirements.txt` senza `fastapi`, `sqlalchemy`, `asyncpg`, `alembic`, `psycopg`. |
| | `test_ui_never_imports_coinbase_sdk` | Nessun `.py` sotto `ui/` importa `coinbase`; nessun `requirements*.txt`, `pyproject.toml` o `package.json` sotto `ui/` cita `coinbase`. |
| | `test_ui_never_imports_agent_modules` | Nessun `.py` sotto `ui/` importa `modules`, `training` o `script`. |
| | `test_ui_has_no_order_identifiers` | Nessun identificatore sotto `ui/backend/` corrisponde a `ORDER_METHOD` (escluse le alternative `post`, `put`, `delete`, che in FastAPI sono i decoratori delle route) o ai token e identificatori vietati di C11. |
| | `test_ui_services_have_no_exchange_secrets` | Nei servizi `ui-*` nessuna variabile `COINBASE_*`; `env_file` solo sotto `ui/env/`; i file `ui/env/*.example` non citano `COINBASE`; nessun mount di `/var/run/docker.sock` ne' di `chronos-state`. |
| | `test_ui_services_mount_agent_data_read_only` | Ogni mount di `./data`, `./models`, `chronos-telemetry` nei servizi `ui-*` termina con `:ro`; `chronos-commands` e' scrivibile solo in `ui-api`, `:ro` in `chronos` e assente altrove. |
| | `test_ui_ports_bound_to_localhost` | Solo `ui-web` pubblica porte, tutte con prefisso `127.0.0.1:`; `ui-internal` ha `internal: true`; `ui-db`, `ui-api`, `ui-ingest` non sono su `ui-edge`. |
| `tests/test_control_commands.py` | `test_ui_allowlist_is_exact` | `UI_ALLOWED == {"pause_entries", "cancel_pending_entry", "close_position", "flatten_all"}` e `LOCAL_ALLOWED` = `UI_ALLOWED` unito a `{"resume_entries", "reset_guard"}`. |
| | `test_ui_channel_rejects_risk_increasing_commands` | Parametrico: `buy`, `place_order`, `open_position`, `increase_size`, `set_risk`, `set_config`, `set_mode`, `go_live`, `resume_entries`, `reset_guard`, `disable_kill_switch`, `promote_model`, `rollback_model`, `transfer`, tipo vuoto, campi extra (`size`, `side`, `price`): esito `rejected`; `kill_switch_state`, ledger e `BalanceBook` invariati; nessuna chiamata al gateway. |
| | `test_bad_signature_expired_future_duplicate` | Motivi `bad_signature`, `expired`, `future_dated`, `duplicate`; un duplicato non produce effetti. |
| | `test_reset_guard_only_coverage_breach_and_only_local` | `reset_guard` rifiutato dal canale `ui` e per guardie diverse da `coverage_breach`. |
| | `test_manual_close_goes_through_risk_manager_gateway_and_guard` | Spy: `plan_exit`, `submit_exit`, `BalanceGuard.authorize` con side SELL; mai `plan_entry` ne' BUY. |
| | `test_manual_close_is_neutral_for_loss_streak` | Una chiusura `MANUAL` in perdita non incrementa e non azzera la loss streak. |
| | `test_pause_blocks_entries_not_exits` | Con `manual_pause` gli ingressi danno `Skip(kill_switch)`; TP, SL, SELL, orizzonte e watchdog funzionano. |
| | `test_close_rate_limit_and_flatten_interval` | Settima chiusura nell'ora e secondo `flatten_all` entro 30 min: `rate_limited`; canale `local` senza limiti. |
| | `test_inbox_garbage_never_crashes` | File binario, JSON enorme, nome non ULID, link simbolico, directory, 1 001 file, file che sparisce durante la lettura: il task resta vivo; oltre `max_pending_files` allarme `command_flood`. |
| | `test_ui_channel_disabled_without_key` | Senza variabile (o con meno di 32 byte) la directory del canale `ui` non viene mai elencata (spy su `os.scandir`). |
| | `test_control_package_cannot_buy` | AST su `modules/control/`: nessun import di `coinbase` o di `modules.execution.exchange`; nessun identificatore `plan_entry`, `submit_entry_with_bracket`, `authorize`, `CoverageTicket`, `create_order`, `build_order_body`; nessuna stringa letterale `"BUY"`. |
| `tests/test_telemetry.py` | `test_status_json_is_atomic_and_valid` | Un lettore concorrente non vede mai un JSON parziale; il file rispetta lo schema. |
| | `test_event_log_never_blocks` | A coda piena `emit` ritorna in meno di 1 ms e `events_dropped_total` cresce. |
| | `test_telemetry_failures_do_not_propagate` | Directory non scrivibile, disco pieno simulato, eccezione nello snapshot: il `TaskGroup` resta vivo. |
| | `test_db_snapshot_is_consistent_and_immutable` | Con scritture concorrenti nel DB sorgente lo snapshot si apre con `immutable=1`, e' in `journal_mode=delete`, non ha `-wal`/`-shm` e rappresenta uno stato coerente. |
| | `test_events_never_contain_secrets` | Nessun evento contiene il valore di prova di `COINBASE_API_SECRET` o una chiave in formato CDP. |
| | `test_contract_schemas_up_to_date` | `ui/contracts/` coincide con l'export di D12. |
| `tests/test_agent_independent_of_ui.py` | `test_agent_independent_of_ui` | Stesso replay di 30 minuti (5.3 punto 9) in cinque scenari: (a) `telemetry.enabled: false` e inbox assente; (b) telemetria attiva, inbox vuota; (c) inbox piena di file non validi e firme sbagliate; (d) chiave assente con inbox piena di comandi validi; (e) directory di telemetria non scrivibile. `DecisionRecord`, ledger e `closed_trades` identici byte per byte fra gli scenari. Un sesto scenario con un `pause_entries` valido e' il controllo positivo: deve cambiare il risultato. |

**GUI** (`docker compose --profile ui-test run --rm ui-test`, in `ui/backend/tests/` e `ui/frontend/`):
- contratto dell'API: `test_openapi_snapshot`, `test_every_endpoint_requires_auth`, `test_viewer_cannot_send_commands`, `test_decimal_fields_are_strings`;
- sicurezza: `test_csrf_required_on_mutations`, `test_cross_origin_mutation_rejected`, `test_no_cors_headers`, `test_login_rate_limit_and_lockout`, `test_session_idle_and_absolute_expiry`, `test_reauth_required_for_close_and_flatten`, `test_audit_records_logins_and_commands`;
- comandi: `test_api_rejects_non_allowlisted_commands` (422 e nessun file scritto), `test_command_file_is_signed_and_atomic` (firma uguale ai vettori di D12), `test_inbox_cleanup`, `test_commands_disabled_when_inbox_not_writable`;
- ingest: `test_ingest_is_idempotent`, `test_ingest_tolerates_partial_lines_rotation_and_bad_parquet`, `test_ingest_never_writes_agent_mounts` (sorgenti in sola lettura: contenuto e `mtime` invariati, nessun file creato), `test_ingest_validates_against_contracts`, `test_unknown_schema_version_is_kept_raw`, `test_rebuild_from_scratch`;
- dati pesanti: `test_bars_downsampling_caps_points`, `test_pagination_limits`, `test_statement_timeout`;
- frontend: `vitest` sui formatter (Decimal come stringa, fusi orari) e `test_no_secrets_in_bundle` (`dist/` non contiene `COINBASE`, `CHRONOS_UI_COMMAND_KEY` o stringhe esadecimali di 64 caratteri).

### 6.9 Ordine consigliato, sottofasi e criteri di uscita

P7 non e' sul percorso critico: nessuna fase P2-P6 lo aspetta e la GUI non e' mai prerequisito del trading. La priorita' resta chiudere P1 e completare P2/P3.

| Sottofase | Quando | Contenuto | Criterio di uscita |
|-----------|--------|-----------|--------------------|
| P7a-1 Sola lettura su P0/P1 | Subito dopo il verdetto di P1 (per non distrarne la chiusura), in parallelo a P2. Codice separato in `ui/`; dell'agente tocca solo D1 e D2 (e D15 dopo il soak). | Compose `ui`, auth, ingest di Parquet, report e modelli; V6 (candele, senza overlay), V7, V8. | `tests/test_ui_isolation.py` e test della GUI verdi; `docker compose config --services` senza profili elenca solo `chronos`; con `--profile ui` le viste mostrano lo storico dei tre prodotti e il baseline; ricostruzione di `ui-db` da zero in meno di 30 minuti. |
| P7a-2 Viste live | D4-D9 e D12-D14 si scrivono insieme ad `app.py` (P2); le viste si chiudono con il dry run (P3). | V1-V5, V6 con overlay e marker, V9, V10. | `tests/test_telemetry.py` e `test_agent_independent_of_ui` verdi. 72 h di dry run con la GUI accesa, poi `docker kill` dei container `ui-*` e riavvio: heartbeat dell'agente continuo, nessun errore dell'agente dovuto alla GUI, 1 440 decisioni al giorno per prodotto meno `skipped_late`. Impatto misurato su 24 h con la GUI accesa e 24 h spenta: CPU media della telemetria sotto il 2% di un core; p99 di `decided_at - (ts_bar_close + S)` con GUI accesa entro +5% rispetto a GUI spenta. Dopo 24 h di GUI spenta, la GUI riaccesa recupera tutta la storia mancante. |
| P7a-3 Comandi | Dopo P7a-2; consigliato prima di P4 (live-micro). | V11, `modules/control/`, canale locale. | `tests/test_control_commands.py` e test di sicurezza della GUI verdi. In dry: `pause_entries` applicato entro 10 s e visibile nell'audit; `close_position` su un book virtuale chiude tramite `RiskManager`, gateway e guard con esito `MANUAL`; un comando falsificato e uno vietato rifiutati con il motivo giusto; `resume_entries` funziona solo dalla CLI locale. |
| P7b | Dopo P4, in parallelo a P5. | V12-V16, `reduce_position`, TOTP, export completi, Grafana opzionale. | Test delle nuove viste; criteri di P7a ancora verdi. |

Con P1 NO-GO o INCONCLUSO, P7a-1 resta utile per ispezionare dati e gate; le altre sottofasi seguono il destino di P2/P3.

### 6.10 Domande aperte per l'utente

1. **Ripresa dopo una pausa dalla GUI.** Default: no, solo dalla CLI locale (`docker compose exec chronos python -m modules.control.cli resume --book <book>`). Vuoi `resume_entries` anche dalla GUI, con password e conferma, accettando che con il canale compromesso si possa annullare una pausa?
2. **Modalita' dry/live.** Default: mai dalla GUI, in nessuna direzione; per fermare il live ci sono `pause_entries(live)` e `flatten_all(live)`. Confermi?
3. **Accesso da fuori questo PC** (LAN, VPN tipo Tailscale/WireGuard, Internet). Default: solo `127.0.0.1`. Se serve, quale scenario? Richiede TLS e secondo fattore (TOTP, oggi in P7b).
4. **Promozione e rollback del modello.** Default: no dalla GUI (regola L5 e CLI del registry). Vuoi almeno un rollback al champion precedente dal canale locale o dalla GUI?
5. **Notifiche fuori dal browser** (Telegram, ntfy, e-mail). Richiedono un container con accesso a Internet e un token. Le vuoi in P7b, e su quale canale?
6. **P7a-3 prerequisito di P4.** Default: consigliato, non obbligatorio. Vuoi renderlo obbligatorio prima del primo ordine reale?
7. **Limiti dei comandi.** Vanno bene 6 chiusure manuali l'ora e un `flatten_all` ogni 30 minuti dalla GUI? Vuoi il pulsante `flatten_all` gia' in P7a?
8. **Utenti e retention.** Un solo utente `operator` o anche utenti `viewer` separati? Nella GUI vanno bene 90 giorni di decisioni complete e 180 di eventi?

---

## Appendice A — Piano di rilascio

Perimetro: fase locale a costo zero (C8). L'infrastruttura cloud e il fine-tuning LLM su scala sono progettati a parte.

| Fase | Contenuto | Criterio di uscita |
|------|-----------|--------------------|
| P0 Fondamenta | Sez. 0.5: config pydantic, `CostModel`, backfill storico in Parquet, indicatori batch e incrementali, barre, market stream WS + `LocalOrderBook`, ledger SQLite, recorder WS. Probe rieseguito con la key View. | Criterio di uscita della sez. 0.5 (barre REST: R1 su blocchi di 1.440 stati, R2 e R4 della sez. 5.1.7, non il rapporto di minuti rivisti; certificabile sui giorni UTC interi del dry run con `revision_impact certify --days`, sui prodotti del modello; certificato `pass` il 2026-10-26 sui giorni 24-25 ott). |
| P1 Fattibilita', dati e baseline | `training/feasibility.py` **per primo**; poi sez. 1 (sentiment live, in raccolta) + sez. 3 parte CPU: label triple-barrier, split purgati, `BaselineDecider` LightGBM, backtest con `BarFillModel`. | Lo studio di fattibilita' trova una combinazione con lift richiesto <= 0.10 (altrimenti stop, sez. 3.1.3); backtest walk-forward con PnL netto > 0 su >= 3/5 fold e sull'holdout; un fold o un holdout senza trade ha PnL 0 e non e' positivo (sez. 3.4). |
| P2 Decision + Risk | Sez. 2 e sez. 4 completi, con `ShadowExecutionGateway` come unico gateway istanziabile. | Test e2e verdi; `test_dry_never_writes` verde. |
| P3 Dry run | Sez. 5: book paralleli, label resolver, learner notturno, report. Esecuzione continua in locale. Etichettatura del sentiment (sez. 1.1.2) a meta' della prima settimana. | G-DRY-OPS superato (minimo 7 giorni; con `feature_drift` `unmeasured` o `not_testable` G-DRY-OPS e' al massimo INCOMPLETE e non chiude P3; `not_covered` non blocca). G-DRY-ECON continua a maturare. |
| P4 Live-micro (R0) | Key con permesso Trade, `CHRONOS_LIVE_CONFIRM`, size minime, shadow twin + parity report. Prima di P4: probe esteso con `product_type`, `product_venue`, `future_product_details`, `portfolio_type`, `can_transfer` (allowlist della sez. 4.1.6). Primo ordine = validazione reale dell'ingresso post-only e del bracket standalone. | Condizioni R0 della sez. 5.1.7; per una versione `not_covered`: D4 e D5 implementati prima del primo ordine, D1-D3 con P4 (sez. 5.1.7, differito). |
| P5 Rampa | Gradini R1, R2, ... | R1 richiede G-DRY-ECON; poi condizioni per gradino, con discesa automatica in caso di violazioni. |
| P6 (fuori scope) | Migrazione cloud, `chronos-decider` LLM fine-tuned su GPU, che entra come book `challenger` in dry prima di qualunque promozione. | Gate sez. 3.4 + G-DRY-OPS + regola L5. |
| P7 Centro di controllo (opzionale) | Sez. 6: GUI in container separati (profilo compose `ui`: `ui-db`, `ui-ingest`, `ui-api`, `ui-web`), telemetria e canale comandi dell'agente (`modules/telemetry/`, `modules/control/`). Sottofasi: P7a-1 sola lettura su P0/P1 (dopo il verdetto di P1, in parallelo a P2), P7a-2 viste live (con P2/P3), P7a-3 comandi (consigliato prima di P4), P7b (dopo P4). Non e' prerequisito di nessuna fase. | Criteri della sez. 6.9 per sottofase. |

## Appendice B — Dipendenze v2 (`requirements.txt` del container chronos)

```
pandas
polars
numpy
pyarrow
httpx
feedparser
selectolax
tenacity
aiolimiter
cachetools
sortedcontainers
aiosqlite
aiofiles
pydantic>=2
pydantic-settings
pyyaml
structlog
onnxruntime
optimum[onnxruntime]
transformers
lightgbm
coinbase-advanced-py
```

Solo training / decider (immagini separate): `torch` (CUDA), `peft`, `bitsandbytes`, `accelerate`, `datasets`, `numba`, `scipy`, `scikit-learn`, `matplotlib`, `fastapi`, `uvicorn`.

Centro di controllo (sez. 6; immagini separate, mai nel `requirements.txt` del container `chronos`, che non cambia):
- `ui/backend/requirements.txt` (`ui-api`, `ui-ingest`): `fastapi`, `uvicorn`, `sqlalchemy>=2`, `asyncpg`, `alembic`, `pydantic>=2`, `pydantic-settings`, `polars`, `pyarrow`, `argon2-cffi`, `structlog`, `jsonschema`, `python-ulid`; test: `pytest`, `pytest-asyncio`, `httpx`. Vietati `coinbase-advanced-py` e qualunque SDK di exchange o cloud (C8, C11).
- `ui/frontend/package.json`: `react`, `react-dom`, `react-router-dom`, `@tanstack/react-query`, `lightweight-charts`, `echarts`; sviluppo: `vite`, `typescript`, `vitest`, `openapi-typescript`, `eslint`. Node 22 solo nella fase di build dell'immagine.
- Immagini: `postgres:17-alpine`, `python:3.12-slim`, `node:22-alpine` (solo build), `nginxinc/nginx-unprivileged:alpine`, con tag fissati per digest.

## Appendice C — Riferimenti

- Jev / protocollo System One (tipi `noul`, `choice`, `score`, campi di risposta): https://openrouter.ai/blog/insights/what-is-jev/ , https://openrouter.ai/docs/guides/community/jev-tutorial
- OpenJev (server protocol-compatible, backend, porta 8080, `/v1/systemone`, `Server-Timing`, lettura dei logit): https://github.com/razorback16/openjev
- Coinbase Advanced Trade, ordini e TP/SL allegati: https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/guides/orders , https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/orders/create-order
- Coinbase Advanced Trade sandbox (statica, risposte predefinite): https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/sandbox
- Triple-barrier labeling, purged CV, sample uniqueness: M. Lopez de Prado, *Advances in Financial Machine Learning*, cap. 3, 4, 7.
- Temperature scaling: Guo et al., *On Calibration of Modern Neural Networks*, ICML 2017.
