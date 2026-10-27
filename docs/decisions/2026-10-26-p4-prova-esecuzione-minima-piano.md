# P4-pre: prova di esecuzione a taglia minima con soldi veri (piano esecutivo)

Autore: Architetto (Opus). Data: 2026-10-26, rev. 2 del 2026-10-27 (allineata alla risposta del trader-quant, Allegato A). Stato: APPROVATA il 2026-10-27 (decisioni della sez. 10 registrate nella sez. 10.1 con le risposte dell'utente e l'accettazione scritta del rischio dell'ordine pendente). Nessun ordine reale e' stato eseguito da chi ha scritto il codice: gli stadi reali (sez. 4) li esegue solo l'utente, uno alla volta.

Fonti lette: spec 2.14 (sez. 0 C9-C11, 4.1-4.1.6, 4.2, 5.1.1, 5.1.7, 5.3 punto 15, Appendice A), `docs/decisions/2026-10-20-canale-user-p4.md`, `modules/execution/*` (in particolare `spot.py`, `orders.py`, `exchange.py`, `gateway.py`, `balance_guard.py`, `watchdog.py`), `modules/app.py`, `modules/risk/kill_switch.py`, `modules/risk/risk_manager.py`, `modules/config.py`, CLAUDE.md. Il file `.env` NON e' stato letto.

Tutti i valori numerici sono nella tabella unica della sez. 9; il testo vi rimanda con il nome del parametro.

## 0. Perimetro e deviazione dichiarata

- Scopo: validare l'impianto di esecuzione reale: post-only, bracket standalone, fee reali, fill reali, canale `user`, riconciliazione, `LiveBalanceFeed`, watchdog e kill switch. NON e' una strategia: il PnL atteso e' negativo (circa 0,02 EUR di fee per ciclo, Allegato A/10.1) e non e' un criterio.
- Deviazione dalla spec: R0 (sez. 5.1.7) richiede G-DRY-OPS e GE = GO, oggi non soddisfatti. La prova e' una fase nuova, `P4-pre` (modalita' `exec_probe`), distinta dalla rampa. Non e' R0, i suoi trade non contano per R0, G-DRY-ECON, L5 o altri gate e non sbloccano la rampa.
- Nessun modello, `DecisionEngine`, policy o `RiskManager`. Il precedente e' il book `buy_and_hold` (sez. 5.3 punto 8).
- C11 invariato: ogni ordine nasce da `BalanceGuard.authorize`, body solo dalla matrice di `spot.py` (estesa con la sola variante GTD dell'ingresso BUY, sez. 3.3), SELL solo su base posseduta registrata nel ledger.
- I fill della prova sono solo diagnostica (Allegato A/10.7). Si salvano a parte con `source = exec_probe` e non entrano mai nel fit del fill model ne' tra i trade del modello.

## 1. Che cosa si autorizza (limiti HARD)

Modalita' `exec_probe`: processo separato (`python -m modules.probe`), book `probe`, un solo prodotto. I limiti sono COSTANTI in `modules/probe/limits.py`, con i valori della sez. 9. La config puo' solo abbassarli: un validator rifiuta i valori superiori.

1.1 Ingresso
- Solo BUY `limit_limit_gtd` con `post_only = true` al `floor_to(best_bid, quote_increment)` e `end_time = invio + entry_timeout_s` (sez. 3.3). Se GTD non e' fattibile (verifica in 4.2), si ripiega su `limit_limit_gtc` con annullamento attivo (sez. 3.3, ripiego). Mai taker, mai market in ingresso.
- Nessun riprezzamento e nessun secondo tentativo nello stesso slot.
- Una posizione e un ingresso pendente alla volta. Nessun ingresso se esiste un ordine aperto qualsiasi su `BTC-EUR`, anche estraneo.
- `required_quote` dell'ingresso (fee di copertura `f_cov` incluse) <= `min(Q_max, L_rem - dust_mark)`. `L_rem = equity_probe - (C - L_tot)`, con `equity_probe = C + somma(net_quote dei closed_trades del book probe) + valore a mercato (bid) della posizione e del dust - costo della posizione aperta`. Con i valori della sez. 9 il nozionale tipico `n` e' circa 2,96 EUR.
- Fine per esaurimento: se `min(Q_max, L_rem - dust_mark) < N_br * (1 + f_cov)`, la prova termina.

1.2 Livelli e uscite
- `TP = floor_to(entry * (1 + tp_frac))`, `SL = ceil_to(entry * (1 - sl_frac))`, regole 4.1.3.
- Uscita per orizzonte del `Watchdog` esistente a `horizon_min`.
- Market ammesso SOLO in quattro casi: `emergency_exit` del watchdog o della protezione SL (1.4); residuo dell'uscita normale dopo il limit al bid (percorso X4-X5 del gateway); bracket non piazzato; `FLATTEN` manuale. Il motivo va nel `decision_id` (`probe-exit:<motivo>:<ts>`).

1.3 Perdite e serie
- Perdita giornaliera > `max_daily_loss_quote`: ingressi bloccati fino alle 00:00 UTC. Si usa `AccountGuards` con `max_daily_loss_frac = max_daily_loss_quote / C`, alimentato con `equity_probe`.
- Perdita cumulata > `anomaly_loss_quote`: fine della prova e CONFLITTO.
- `L_tot` e' il tetto per costruzione: e' garantito dalla regola di `required_quote` anche con perdita del 100% della posizione.
- Serie contate SEPARATAMENTE da `ProbeStreaks` (nuovo, persistito nel DB della prova):
  - `sl_streak`: chiusure `SL` consecutive. Si azzera con `TP`, `HORIZON`, `SELL`, `MANUAL`; un `WATCHDOG` non la tocca. A `max_consecutive_sl` gli ingressi sono in pausa fino alle 00:00 UTC del giorno successivo; non e' la fine della prova.
  - `watchdog_count`: una sola chiusura `WATCHDOG`, qualunque sia la causa (bracket assente, SL non eseguito, protezione 1.4), porta alla fine della prova e a un CONFLITTO.
  - La serie unica di `AccountGuards`, che conta `SL` e `WATCHDOG` insieme, va neutralizzata nella prova con `max_consecutive_losses = max_entries_total + 1`, cosi' non scatta mai. Nel resto del sistema resta invariata.

1.4 Protezioni (bloccanti; ogni intervento e' un'uscita `WATCHDOG`, quindi fine della prova)
- Dal terminale dell'ingresso al bracket `OPEN`: allarme oltre `bracket_open_alarm_s`. Senza bracket `OPEN` entro `bracket_open_max_s`: uscita immediata.
- SL scattato (`trigger_status` del bracket da `get_order`/canale `user`) e non completamente eseguito entro `sl_fill_max_s`, oppure `best_bid < SL * sl_floor_ratio` con la posizione aperta: `emergency_exit`.
- Liquidita' `MAKER` su un fill di SL: CONFLITTO (lo stop e' rimasto nel book).
- Il watchdog standard (`SL - 1 x ATR1h` per 10 s; prezzo <= SL senza bracket) resta attivo e invariato.
- Uscita per orizzonte fallita dopo l'annullamento del bracket (esito `refused`, `rejected`, `unknown`, `unconfirmed` o `partial` con la posizione ancora LONG e senza bracket): il runner chiama subito `gateway.place_missing_bracket` con la quantita' posseduta e i TP/SL della posizione, senza aspettare il ciclo del riconciliatore. Il watchdog ritenta l'uscita dopo `retry_s`.

1.5 Dust
- Soglia di proteggibilita': `N_br = quote_min_size * 1.02 / (1 - sl_frac)`, circa 1,03 EUR.
- Classificazione nel gateway (correzione generale, sez. 3.4): una quantita' e' dust se non e' vendibile al prezzo dello stop, cioe' `is_sellable(size, min(best_bid, stop_loss), meta)` e' falso. Il ledger la porta a FLAT con `dust_qty`/`dust_cost` (`to_dust`, esistente).
- Fusione nel bracket successivo, meccanica gia' prevista da spec 4.1.6 regola 4 e dal gateway (`_PendingEntry.prev_dust_qty`, `EntryResult.dust_merged`): al fill dell'ingresso successivo sullo stesso prodotto la posizione vale `fill + dust_qty`, e il bracket copre `floor_to(min(base_qty, sellable))`, dust incluso. `BalanceBook.free(BTC)` contiene gia' il dust, perche' e' rimasto sul conto. Va reso vero da un test (sez. 3.6); se il ledger non fonde, CONFLITTO prima di procedere.
- Contabilita' della prova: il dust e' valutato al bid in `equity_probe` e sottratto dal margine dell'ingresso (`- dust_mark` in 1.1). Cosi' la perdita massima, dust incluso, resta <= `L_tot`.
- Fine prova con dust residuo: resta sul conto, dichiarato nell'esito con quantita' e valore. Non si vende (e' sotto il minimo); l'utente lo puo' vendere a mano quando sara' vendibile.

1.6 Calendario e interruttori
- Ingressi solo agli slot UTC della sez. 9, lunedi'-venerdi'. Il secondo slot si usa solo se la posizione del primo e' chiusa o non si e' riempita. Bracket, watchdog e uscite sono sempre attivi.
- Giorni di FOMC, CPI USA, NFP o riunione BCE: nessun ingresso. Il calendario e' a carico dell'operatore, che usa `STOP`.
- File `<state_dir>/exec_probe/STOP`: blocca gli ingressi e annulla con conferma l'ingresso pendente; bracket e posizione restano. File `FLATTEN`: `STOP` piu' l'uscita della posizione (motivo `MANUAL`). Si creano con la CLI `python -m modules.probe stop|flatten`, sono controllati ogni secondo e prima di ogni ingresso, e si rimuovono solo a mano.
- Tetto di ordini creati per giorno UTC (`max_orders_per_day`): blocca SOLO i nuovi ingressi, mai bracket, uscite o ripiazzamenti di protezione.

## 2. Come nasce la decisione d'ingresso

- Trigger deterministico: a ogni slot si genera al piu' un intent, se tutte le condizioni sono vere. Nessun modello e nessun segnale di merito: il mercato fissa solo prezzo, size e livelli. E' un test dell'impianto. Lo stesso percorso a valle e' gia' stato esercitato in shadow dal book `random_entry` con `forced_buy_rate`; la casualita' e' tolta perche' l'operatore deve sapere quando parte un ordine reale.
- Condizioni allo slot, nell'ordine; la prima falsa registra `probe_skip` con il motivo:
  1. nessun `STOP`/`FLATTEN`;
  2. `KillSwitch.entry_blocked("probe")` vuoto e pausa di `ProbeStreaks` non attiva;
  3. limiti del giorno e totali non raggiunti;
  4. posizione FLAT e nessun ordine aperto su `BTC-EUR`;
  5. snapshot saldi con eta' <= 120 s;
  6. quote REST con eta' <= `quote_max_age_s` e `spread_bps <= max_spread_bps`;
  7. `ATR1h_bps` (Wilder 14 su candele REST `ONE_HOUR`, in bps dell'ultimo close) <= `max_atr1h_bps`;
  8. margine di 1.1 sufficiente;
  9. size vendibile allo stop.
- Identita': `decision_id = f"probe:{product}:{slot:%Y%m%dT%H%MZ}"`; da qui `client_order_id` uuid5. Uno slot gia' presente nel ledger non si ripete mai, nemmeno dopo un riavvio.
- `OrderIntent`: book `probe`, `entry.type = "limit_post_only"`, `expires_on_exchange = true` (sez. 3.3), `time_in_force_s = entry_timeout_s`, `base_size = max_base_for_quote(margine 1.1, entry, f_cov, ...)`. Il bracket ha `k_tp = k_sl = 0.0`, `atr_tf = "probe"` e `atr = ATR1h` (solo informativo). Blocco `risk` con valori neutri dichiarati: `p_edge 0.0`, `scale_p 0.0`, `regime 0`, `m_reg 0.0`, `ev_bps 0`, `p_breakeven None`, `p_be_buffer 0.0`. La prova si riconosce dal book `probe` e dal prefisso `probe:`.

## 3. Modifiche al codice

Il rifiuto di default resta identico: `modules/app.py`, `resolve_mode`, i controlli del costruttore di `LiveExecutionGateway`, `BalanceGuard` ed `exchange.py` non cambiano.

3.1 Config (`modules/config.py`): sezione `exec_probe: ExecProbeConfig`, default `enabled: false`. Contiene i campi della sez. 9, `db_path` (default `state/chronos_probe.db`, distinto da `paths.live_db`) e `out_dir`. Validator: ogni valore <= costante di `limits.py`; prodotto nell'allowlist.

3.2 Package `modules/probe/`
- `limits.py`: le costanti della sez. 9.
- `planner.py`: `ProbePlanner` puro, che produce solo intent post-only con scadenza.
- `budget.py`: `ProbeBudget`, `ProbeLimiter` (conteggi dal ledger) e `ProbeStreaks`.
- `runner.py`: l'UNICO modulo, oltre a `gateway.py`, che importa `LiveExecutionGateway` e chiama `build_exchange(..., read_only=False)`.
  - Cablaggio, nell'ordine: gate di abilitazione (3.5) -> exchange -> `assert_live_key_permissions` -> `SpotRegistry.load([product])` -> `FeeProvider` -> `PositionLedger.open(db_path, "live")` -> `BalanceBook("probe", "exchange")` -> `SystemGuards` + `AccountGuards("probe")`, con la config di 1.3 e `max_total_exposure_frac = 1.0` (l'esposizione e' gia' limitata da 1.1) -> `KillSwitch.load` -> `BalanceGuard` -> `LiveExecutionGateway(..., book="probe", execution=ExecutionConfig(mode="live", ...))` -> `restore_entry_contexts()` -> `Reconciler.on_startup()` (placer = gateway; `CoverageMonitor` -> `trip_coverage_breach`) -> `LiveBalanceFeed` -> `CoinbaseUserStream` + `OrderEventSink` -> `CoinbaseMarketStream([product])` -> `Watchdog(book="probe", horizon_min=...)` -> monitor delle protezioni 1.4 -> `ProbeScheduler` -> watcher di `STOP`/`FLATTEN`.
  - Tutti i task stanno in un `asyncio.TaskGroup`.
  - Fatali: `SpotViolation` (exit 4), `DryRunViolation` (exit 3), `ExchangeAuthError`.
  - All'arresto: ingresso pendente annullato con conferma, bracket MAI annullati.
- Registrazione:
  - messaggi grezzi redatti del canale `user` in `out_dir/user_channel_raw.jsonl.gz`, con l'involucro e `redact` di `script/user_channel_soak.py` spostati in un modulo condiviso;
  - `WsRecorder` sul market stream di `BTC-EUR` (per la parita' offline con `replay.py`; niente shadow twin in linea).
- ATR1h: aggiornato ogni ora; senza un valore valido nessun ingresso. Un test copre il comportamento del watchdog con `atr_1h` `None`.
- `__main__.py`: `preflight`, `run --stage {preview,place-cancel,expire,reject,live}`, `stop`, `flatten`, `status`, `report`.

3.3 Ingresso GTD (raccomandato)
- Scelta: l'ingresso scade LATO EXCHANGE con `limit_limit_gtd`. Con il bot fermo, un `limit_limit_gtc` resta nel book a tempo indefinito e puo' riempirsi ore dopo senza bracket (Allegato A/10.9 a). GTD limita questa finestra a `entry_timeout_s`. NON la azzera: un fill dentro la finestra con il bot fermo resta comunque senza bracket fino al riavvio (sez. 11, punto 1). Serve quindi l'accettazione scritta dell'utente anche con GTD (sez. 10, punto 5).
- Fattibilita' nel wrapper: `exchange.create_order` passa a `RESTClient.create_order` l'`order_configuration` costruito da `build_order_body`, lo stesso percorso gia' usato per `limit_limit_gtc` e `trigger_bracket_gtc`, quindi `exchange.py` non cambia. `parse_order` legge la prima configurazione interna (`base_size`, `limit_price`). `EXPIRED` e' gia' terminale (`TERMINAL_STATUSES`) e il canale `user` lo tratta come gli altri stati terminali. [Da verificare in 4.2] Che Coinbase accetti `post_only` dentro `limit_limit_gtd`, il formato di `end_time` e un eventuale anticipo minimo.
- Modifiche, tutte con il default che lascia invariato il comportamento attuale:
  1. `orders.py`: `OrderSpec.end_time: datetime | None = None` (UTC, timezone-aware). Con `kind = "limit"` ed `end_time` valorizzato si ottiene `limit_limit_gtd`. `OrderKind` non cambia.
  2. `spot.py`: `ORDER_MATRIX["BUY"]["limit_limit_gtd"] = {"base_size", "limit_price", "end_time", "post_only"}`.
     - `validate_structure`: `end_time` solo per BUY `limit`, timezone-aware, nel futuro.
     - `build_order_body`: sceglie `limit_limit_gtd` solo se `end_time` e' valorizzato, con `end_time` in ISO 8601 UTC `YYYY-MM-DDTHH:MM:SSZ`.
     - `validate_body`: `end_time` stringa ISO valida.
     - Nessun GTD per i SELL.
  3. `risk_manager._EntryLeg.expires_on_exchange: bool = False`; `gateway.entry_spec` imposta `end_time = intent.created_at + time_in_force_s` solo se il flag e' vero. Dry, shadow e backtest restano invariati.
  4. `gateway._run_entry`: con `end_time` attende il terminale fino a `end_time + gtd_grace_s`; l'esito normale e' `EXPIRED` dall'exchange. Allo scadere, `_cancel_order` resta come seconda linea.
  5. Il gateway rifiuta l'invio se `end_time - now < 60 s` (oppure l'anticipo minimo osservato in preview), con esito `refused`, senza effetti.
  6. Spec: sez. 4.1.6 regola 2 ("BUY: `limit_limit_gtc` oppure `limit_limit_gtd` post-only") e matrice documentata. Nessun cambio di sostanza di C11: resta un limite coperto al 100%.
- Ripiego, se la preview rifiuta GTD con post-only: `expires_on_exchange = false`, cioe' `limit_limit_gtc` con annullamento attivo del gateway a `entry_timeout_s`. In questo caso l'accettazione scritta dell'utente copre una finestra indefinita. Il ripiego NON e' stato accettato dall'utente (sez. 10.1, punto 5): lo stadio `live` lo rifiuta finche' `exec_probe.accept_gtc_fallback` non e' `true`, cosa che richiede una nuova accettazione scritta.

3.4 Correzione generale della soglia di dust del bracket
- In `gateway._protect` e `place_missing_bracket` la vendibilita' del bracket si giudica a `min(best_bid, stop_loss)` invece che al solo best bid. La gamba di stop vende al piu' allo SL: un bracket vendibile al bid ma non allo stop verrebbe rifiutato e lascerebbe la posizione scoperta.
- `is_sellable` e' condivisa con lo shadow: cambia il dry solo al confine (piu' dust). Va scritta nel changelog della spec e coperta da un test.

3.5 Abilitazione (tutte obbligatorie; verificate PRIMA di costruire exchange, ledger o gateway; se ne manca una, exit 2 senza rete)
1. `exec_probe.enabled: true` nella config passata con `--config`.
2. `CHRONOS_LIVE_CONFIRM=I_ACCEPT_REAL_ORDERS` e `CHRONOS_EXEC_PROBE_CONFIRM=<product>:<L_tot>` (es. `BTC-EUR:3.00`), uguale alla config. Solo nella shell della prova, mai in `docker-compose.yml`.
3. Credenziali da `--key-file` FUORI dal repo (path risolto sotto la radice del repo = rifiuto), lette con `dotenv_values` e passate esplicitamente a `CoinbaseConfig(...)`. Se nell'ambiente esistono anche `COINBASE_API_KEY`/`COINBASE_API_SECRET`: rifiuto. I due valori entrano in `configure_logging` prima di qualunque log.
4. `stdin` TTY, nessun `--yes`. Il processo stampa: prodotto, `L_tot`, `Q_max`, slot, impronta `sha256(api_key)[:8]`, permessi booleani, EUR disponibili e l'esito della scelta GTD/ripiego. L'utente digita `CONFERMO BTC-EUR 3.00 EUR`. La conferma si ripete a ogni avvio; nessun riavvio automatico.

3.6 Test (prima di ogni corsa reale, con exchange finto)
- AST: `LiveExecutionGateway` importato solo da `gateway.py` e `modules/probe/runner.py`; `read_only=False` solo in `runner.py`. `tests/test_app.py` e `tests/test_c11_wiring.py` restano verdi; un eventuale allowlist si estende solo con quei nomi esatti.
- Matrice: body GTD con esattamente le quattro chiavi; GTD SELL rifiutato; `end_time` naive o nel passato rifiutato; GTC e bracket invariati byte per byte; `entry_spec` con flag falso identico a oggi.
- Gate di 3.5: ogni elemento mancante porta al rifiuto senza chiamare la fabbrica dell'exchange.
- Limiti e budget: valori oltre le costanti rifiutati; il planner non produce mai taker o market; `required_quote <= min(Q_max, L_rem - dust_mark)`; fine per esaurimento; conteggi dal ledger dopo un riavvio; slot gia' usato non ripetuto; il tetto di ordini non blocca uscite e bracket.
- Serie: `SL, SL` -> pausa fino alle 00:00 UTC del giorno dopo; `SL, TP, SL` -> nessuna pausa; un `WATCHDOG` -> fine; un `WATCHDOG` non entra in `sl_streak`.
- Dust: fill parziale sotto la soglia di 1.5 -> dust; ingresso successivo -> `base_qty = fill + dust` e bracket sulla somma; dust sottratto dal margine.
- Scenari e2e: ciclo completo con TP; `EXPIRED` senza fill (GTD) e annullamento (ripiego); fill parziale + `EXPIRED` + bracket; bracket rifiutato -> uscita e fine; `OutcomeUnknown` all'invio -> nessun reinvio; protezioni 1.4 (bracket in ritardo, SL non eseguito in 60 s, bid sotto `SL * sl_floor_ratio`, SL con liquidita' MAKER); uscita per orizzonte fallita -> bracket ripiazzato; `STOP` con ingresso pendente; `FLATTEN`; riavvio con posizione aperta; perdita giornaliera e per anomalia; `balance_stale`.

3.7 Spec (dopo l'approvazione): changelog 2.15 con riga `P4-pre` nell'Appendice A, regola 2 di 4.1.6 (GTD) e soglia del dust allo stop (3.4). Nessun'altra sezione cambia.

## 4. Sequenza di verifica prima del primo ordine

Ogni stadio scrive `out_dir/<stadio>.json` con `ok`. Lo stadio successivo parte solo se il precedente e' `ok`, con lo stesso hash di config e lo stesso giorno UTC; lo stadio `live` accetta un `reject` di al piu' 7 giorni prima.

4.0 Operatore
- Chiave CDP View + Trade SENZA Transfer, legata a un portafoglio dedicato, con allowlist IP; salvata in un file fuori dal repo.
- Deposito nel portafoglio dedicato (importo: sez. 10).
- Sospensione di Windows disattivata nei giorni di prova.
- Nel `.env` del repo va rimessa la chiave View del dry run.

4.1 `preflight` (exchange `read_only=True`)
- Permessi, stampati solo come booleani piu' l'impronta: `can_view` e `can_trade` veri, `can_transfer` falso, `portfolio_type` nell'allowlist di `spot.py`. Se il tipo e' diverso: CONFLITTO, senza allargare l'allowlist.
- Increment e minimi del prodotto; fee reali (`assumed = false`). Se diversi dai valori dell'Allegato A/10.0, il quant ricalcola A/10.1-10.3 prima di procedere.
- EUR disponibili >= `Q_max`; zero ordini aperti su `BTC-EUR`; valute con saldo annotate solo come codici.
- Quote, spread, ATR1h.
- `LiveBalanceFeed` per 3 cicli: snapshot accettati, eta' < 120 s, zero allarmi.

4.2 `preview` (scrittura senza effetti)
- `preview_order` del body GTD esatto del primo ingresso (post-only al bid, size reale, `end_time`) e del body GTC equivalente.
- Esito GTD: accettato -> GTD; rifiutato -> ripiego GTC, registrato e sottoposto all'utente prima dello stadio `live`. Annotare il permesso richiesto dalla preview, `errs`, `warning`, `order_total` e `commission_total`.
- Precisione delle fee: se `commission_total` ha solo 2 decimali (arrotondata al centesimo), il criterio Fee della sez. 5 non e' verificabile a questa taglia. Ci si FERMA e si riapre la domanda all'utente e al quant, senza alzare `Q_max` in autonomia.
- La preview del bracket fallisce per BTC non posseduto: e' atteso e va solo annotato.

4.3 `place-cancel`
- Ordine di test: nozionale `test_order_quote`, `size = ceil_to(test_order_quote / prezzo, base_increment)`, `limit = floor_to(best_bid * 0.95)`, GTD (fine a 300 s, o il minimo osservato in 4.2), annullato dal bot a 60 s.
- Ok se: esito `no_fill`; `CANCELLED` nel ledger e su REST; riserva rilasciata solo alla conferma; `free(EUR)` torna al valore iniziale; durante l'attesa `hold <= reserved`; messaggi `user` OPEN e CANCELLED confrontati campo per campo con `get_order` (tabella B del documento del 2026-10-20).
- Se si eseguisse: ingresso normale con bracket (2,00 EUR > `N_br`), entro il margine 1.1.

4.4 `expire` (solo con GTD)
- Stesso ordine della 4.3 con fine a 120 s (o il minimo osservato), SENZA annullamento del bot.
- Ok se lo stato terminale e' `EXPIRED` dall'exchange entro `end_time + gtd_grace_s`, la riserva viene rilasciata e il messaggio `user` corrispondente e' registrato. Valida la protezione lato exchange prima del live.

4.5 `reject`
- BUY post-only di nozionale `test_order_quote` a `ceil_to(best_ask)`.
- Ok se arriva `OrderRejected` o un terminale con `reject_reason`, con riserva rilasciata e nessun fill.
- Se fosse eseguito come taker: post-only non rispettato, CONFLITTO; il fill prosegue come ingresso normale con bracket.

4.6 `live`
- Prima del primo slot: prova di `STOP` (lo `status` riporta `entries_blocked: manual_stop`), poi rimozione.
- Primo micro-ordine allo slot successivo, poi cicli fino ai limiti, con la checklist B-E del documento del 2026-10-20.
- Al primo SL: registrare l'ordine figlio (`order_type`, `limit_price` circa `SL * 0.95`, `trigger_status`, liquidita'); se diverso, CONFLITTO (Allegato A/10.5).

## 5. Metriche, criteri e azioni

`python -m modules.probe report` (solo letture REST) produce `probe_report.json`/`.md`.

Criteri BLOCCANTI:

| Metrica | Definizione | Soglia |
|---|---|---|
| Concordanza ordini | ogni ordine del ledger contro `get_order` (stato, `filled_size`, `average_filled_price`, `total_fees`) | 100% |
| Concordanza fill | ogni fill del ledger contro `get_fills` (`trade_id`, prezzo, size, `commission`, `liquidity_indicator`) | 100%, nessun fill mancante o in piu' |
| Saldi | `BalanceBook.total` di EUR e BTC contro `get_accounts` dopo ogni transizione; delta EUR del conto = flussi del ledger | entro `max(2 * increment, 1e-4 * totale)`; zero allarmi di copertura |
| Fee | per ogni fill: `abs(commission - n_fill * rate) <= max(1e-6 EUR, 0.5 bps * n_fill)`, dove `n_fill = price * size`, `rate` = maker o taker del tier letto in quel momento secondo `liquidity_indicator`, `commission` alla piena precisione dell'API. Se l'API la restituisce arrotondata al centesimo: STOP e domanda riaperta (4.2) | sempre |
| Post-only | prezzo medio d'ingresso = limite, liquidita' MAKER | sempre |
| Copertura bracket | dal terminale dell'ingresso al bracket `OPEN` <= `bracket_open_max_s`; base posseduta del book oltre `N_br` sempre sotto un bracket (dust incluso dopo la fusione) | sempre |
| Scadenza ingresso | con GTD ogni ingresso non riempito termina `EXPIRED` dall'exchange (o `CANCELLED` dal bot come seconda linea, annotato) | sempre |
| Canale `user` | campi della tabella B verificati su: un OPEN, un annullato, un `EXPIRED`, un fill completo, un fill parziale o la sua assenza dichiarata, un bracket | tutti |
| Latenza dei fill WS -> REST | dal messaggio con `cumulative_quantity` aumentata alla comparsa in `get_fills` | max < `entry_poll_s` (5 s) |
| Integrita' | `SpotViolation`, crash, ordini doppi, ordini estranei toccati, uscite `WATCHDOG` | zero |

Misure DESCRITTIVE, senza soglia di gate: fill rate maker, con IC Clopper-Pearson; tempo al fill; latenze (`create_order`, invio -> OPEN, terminale -> bracket OPEN con allarme oltre `bracket_open_alarm_s`, cancel -> CANCELLED); slittamento dello SL `(SL - avg_fill) / SL * 1e4` con allarme oltre `sl_slip_alarm_bps` e allarme grave oltre `sl_slip_severe_bps` (revisione dell'operatore prima dello slot successivo); slittamento delle uscite market contro il bid alla richiesta; PnL.

Azioni per esito:
- Tutti i bloccanti veri: documento di esito `docs/decisions/<data>-p4-prova-esecuzione-minima-esito.md`. L'impianto risulta validato; R0 resta subordinato a G-DRY-OPS e GE = GO.
- Campo del canale `user` diverso dall'atteso: procedura E del documento del 2026-10-20.
- Discrepanza di saldo o allarme di copertura: `STOP`, bracket e watchdog restano, nessun reset automatico, CONFLITTO.
- Bracket non piazzato o qualunque uscita `WATCHDOG`: uscita (automatica), fine della prova, CONFLITTO.
- Fee fuori tolleranza: CONFLITTO. Un tier diverso da quello atteso si annota soltanto (`fallback_fees` resta 60/120, `label_config` non cambia: Allegato A/10.6).
- `OutcomeUnknown`: il riconciliatore deve risolverlo entro 2 cicli senza reinvio; altrimenti CONFLITTO.
- Perdita oltre `anomaly_loss_quote`: fine e CONFLITTO. Perdita giornaliera o `sl_streak`: pausa come da 1.3. Non sono fallimenti dell'impianto.

## 6. Rischi residui e loro limite

- Perdita massima <= `L_tot`, mai debito: solo spot, BUY coperto al 100% con `f_cov` >= 120 bps, SELL solo di base posseduta, dust incluso nel margine. Con il portafoglio dedicato il limite e' anche fisico, pari al deposito.
- Bot fermo con un ingresso aperto: con GTD la finestra e' al massimo `entry_timeout_s`; un fill in quella finestra resta senza bracket fino al riavvio, con perdita al piu' `n` (circa 2,96 EUR). Con il ripiego GTC la finestra e' indefinita, con lo stesso tetto di perdita.
- Gap oltre il pavimento dello stop-limit (-5%): lo SL resta nel book non eseguito. Con il bot vivo interviene la protezione 1.4; con il bot fermo la perdita resta limitata a `n`.
- Posizione con bracket e PC spento: protetta lato exchange; watchdog e orizzonte sono sospesi fino al riavvio.
- Bug che ripete ordini: `client_order_id` deterministici, slot unici dal ledger, limiti dal ledger, guard di copertura.
- Chiave compromessa: senza Transfer non si preleva; si puo' solo scambiare il contenuto del portafoglio dedicato.
- Saldi estranei (MANA): mai toccati.

## 7. Ordine di lavoro per lo sviluppatore

1. Decisioni della sez. 10 scritte in questo documento; stato -> APPROVATO.
2. Spec 2.15 (3.7).
3. GTD: `orders.py`, `spot.py`, `_EntryLeg`, `entry_spec`, `_run_entry` + test della matrice (3.3).
4. Soglia del dust allo stop (3.4) + test, compreso il controllo che il dry non cambi lontano dal confine.
5. `limits.py`, `ExecProbeConfig`, `planner.py`, `budget.py` + test.
6. `runner.py`, gate di abilitazione, monitor delle protezioni + test AST e dei gate.
7. CLI e scenari e2e della 3.6; suite completa verde.
8. Operatore: 4.0, `preflight`, `preview`, `place-cancel`, `expire`, `reject`, `live`.
9. `report`, documento di esito, `tasks/todo.md`.

## 8. Domande al trader-quant

Chiuse: risposte nell'Allegato A, recepite nelle sez. 1-5 e 9. Restano verifiche fattuali, non domande: fee e minimi in preflight (A/10.0), precisione di `commission_total` in preview (4.2), ordine figlio dello SL al primo SL (A/10.5).

## 9. Parametri (tabella unica, valori per `limits.py`)

| Parametro | Valore | Origine |
|---|---|---|
| `product` | `BTC-EUR` | utente |
| `C` (capitale di riferimento) | 20.00 EUR | utente (sez. 10, punto 3) |
| `L_tot` (perdita totale massima) | 3.00 EUR | quant A/10.3 |
| `Q_max` (`required_quote` massimo per ingresso) | 3.00 EUR, applicato come `min(Q_max, L_rem - dust_mark)`; `n` circa 2,96 EUR | quant A/10.1 |
| `test_order_quote` | 2.00 EUR di nozionale | quant A/10.0 |
| `N_br` (soglia di dust) | `quote_min_size * 1.02 / (1 - sl_frac)`, circa 1,03 EUR | quant A/10.0 |
| `tp_frac` / `sl_frac` | 0.006 / 0.006 | quant A/10.2 |
| `horizon_min` | 480 | quant A/10.2 |
| `entry_timeout_s` | 1800 (GTD `end_time` = invio + 1800 s) | spec / quant A/10.9 |
| `gtd_grace_s` | 30 | architetto |
| `max_atr1h_bps` | 60 | quant A/10.2 |
| `max_spread_bps` / `quote_max_age_s` | 10 / 5 | quant A/10.4 |
| `max_daily_loss_quote` | 0.30 EUR (`max_daily_loss_frac = 0.30 / C`) | quant A/10.3 |
| `anomaly_loss_quote` | 0.50 EUR -> fine e CONFLITTO | quant A/10.3 |
| `max_consecutive_sl` | 2 -> pausa fino alle 00:00 UTC del giorno dopo | quant A/10.3 |
| `max_watchdog_exits` | 1 -> fine e CONFLITTO | quant A/10.3 |
| `AccountGuards.max_consecutive_losses` | `max_entries_total + 1` (neutralizzata) | architetto |
| `AccountGuards.max_total_exposure_frac` | 1.0 | architetto |
| `bracket_open_alarm_s` / `bracket_open_max_s` | 10 / 30 (dal terminale dell'ingresso) | quant A/10.9 |
| `sl_fill_max_s` / `sl_floor_ratio` | 60 / 0.96 | quant A/10.5 |
| `sl_slip_alarm_bps` / `sl_slip_severe_bps` | 15 / 50 | quant A/10.5 |
| Tolleranza fee | `max(1e-6 EUR, 0.5 bps * n_fill)`, piena precisione | quant A/10.1 |
| `entry_slots_utc` | 09:00, 11:00 (il secondo solo se la posizione del primo e' chiusa o non riempita), lunedi'-venerdi' | quant A/10.8 |
| `max_entries_per_day` / `max_entries_total` | 2 / 5 | utente |
| `max_orders_per_day` | 16 (blocca solo gli ingressi) | architetto |
| `f_cov` | `max(taker corrente, taker fallback, 120 bps)` (codice esistente) | spec 4.1.6 |

## 10. Decisioni che restano all'utente

1. Prodotto: si usa BTC-EUR?
2. Rischio massimo: accetti di poter perdere al massimo 3 euro in tutto, con operazioni da circa 3 euro ciascuna? Il programma si ferma da solo se perde piu' di 0,30 euro in un giorno (riprende il giorno dopo) o piu' di 0,50 euro in totale (si ferma per sempre e chiede un'analisi).
3. Deposito: quanti euro metti nel portafoglio dedicato? Ne bastano circa 5; con 20 ne restano fermi almeno 15. Meno depositi, piu' il limite e' garantito anche se il programma sbagliasse.
4. Orari: le operazioni partono solo alle 10:00 e alle 12:00 ora italiana, dal lunedi' al venerdi'. Ti va bene essere disponibile e tenere il PC acceso, senza standby, fino a circa le 20:30 in quei giorni? E saltare i giorni con dati economici importanti (FOMC, CPI, NFP, BCE) premendo STOP?
5. Ordine che scade da solo (consigliato): l'ordine d'acquisto si cancella da solo dopo 30 minuti anche se il programma si blocca. Resta un rischio da accettare per iscritto: se il programma si ferma proprio in quei 30 minuti e l'ordine si esegue, la posizione (circa 3 euro) resta senza stop finche' non lo riavvii. Perdita massima circa 3 euro, mai debito. Se Coinbase non accetta l'ordine a scadenza, l'ordine resta aperto finche' il programma non lo cancella: accetti lo stesso rischio senza limite di tempo, oppure in quel caso preferisci rinunciare alla prova?
6. Ordini di prova: va bene usare ordini da 2 euro posti lontano dal prezzo (non dovrebbero eseguirsi, quindi costo zero)?
7. Durata: 5 operazioni in totale, al massimo 2 al giorno. In pratica serve circa una settimana lavorativa.
8. Chiavi: crei una chiave nuova con permesso di scambio ma SENZA prelievi, legata solo al portafoglio dedicato e utilizzabile solo dal tuo indirizzo IP, e la salvi in un file fuori dalla cartella del progetto? Nel file `.env` del progetto rimetti la chiave di sola lettura?
9. Dove gira la prova: direttamente sul PC (ambiente Python) o in un container Docker avviato a mano?

### 10.1 Risposte dell'utente (2026-10-27) e accettazione scritta

L'utente ha approvato le nove decisioni con le risposte consigliate:

1. Prodotto: BTC-EUR.
2. Rischio massimo: 3 EUR in tutto (`L_tot` = 3,00), 0,30 EUR al giorno (fermo fino alle 00:00 UTC), 0,50 EUR cumulati (fine anticipata e CONFLITTO). Operazioni da circa 3 EUR ciascuna (`Q_max` = 3,00 con le fee di copertura).
3. Deposito: 5 EUR nel portafoglio dedicato. `C` = 20 EUR resta il capitale di riferimento solo per il limite giornaliero in frazione (sez. 11, punto 5); il limite fisico e' il deposito.
4. Orari: slot 09:00 e 11:00 UTC (10:00 e 12:00 ora italiana, CET dal 25-ott), lunedi'-venerdi', operatore presente, PC acceso e senza standby fino a circa le 20:30 ora italiana; i giorni FOMC, CPI, NFP e BCE si salta con `STOP`.
5. Ordine d'ingresso con scadenza lato exchange (GTD, 30 minuti). ACCETTAZIONE SCRITTA DEL RISCHIO RESIDUO: se il programma si ferma durante la finestra di 30 minuti e l'ordine si esegue, la posizione (al massimo circa 2,96 EUR) resta senza stop finche' il programma non viene riavviato; la perdita massima e' circa 3 EUR, mai debito. L'accettazione vale per la sola modalita' GTD. Se Coinbase non accetta GTD con post-only (esito della preview, sez. 4.2) NON e' stata data alcuna accettazione per il ripiego GTC senza limite di tempo: in quel caso la prova si ferma allo stadio `live` (config `exec_probe.accept_gtc_fallback` = `false`, default) e la decisione torna all'utente, che per iscritto in questo documento puo' accettare il ripiego o rinunciare.
6. Ordini di prova di 2 EUR di nozionale, lontani dal prezzo.
7. Durata: 5 operazioni in totale, al massimo 2 al giorno.
8. Chiave di scambio (View + Trade, SENZA Transfer) legata al solo portafoglio dedicato, con allowlist IP, in un file FUORI dal repo, passato con `--key-file`. Il `.env` del repo torna in sola lettura (chiave View del dry run).
9. Esecuzione direttamente sul PC (ambiente Python virtuale), non in Docker.

Valore della variabile di conferma: `CHRONOS_EXEC_PROBE_CONFIRM=BTC-EUR:3.00` (`<prodotto>:<L_tot>` come in 3.5, uguale alla config); la frase interattiva e' `CONFERMO BTC-EUR 3.00 EUR`.

## 11. Incoerenze residue segnalate

1. GTD riduce ma non elimina il rischio "bot fermo": un fill dentro la finestra di 30 minuti resta senza bracket fino al riavvio. Lo eliminerebbe solo il bracket allegato all'ordine, vietato dalla spec 2.4. Per questo l'accettazione scritta (sez. 10, punto 5) serve in entrambi i casi.
2. "Dal fill al bracket entro 30 s" (A/10.9 b) non e' compatibile con il bracket posto al terminale dell'ingresso (spec 4.1.6 regola 4): si misura dal terminale. Un fill parziale prima del terminale e' protetto solo dal watchdog (bot vivo) fino a 30 minuti.
3. Il watchdog standard (`SL - 1 x ATR1h` per 10 s, circa 0,3-0,6% sotto lo SL) scatta di solito prima delle soglie del quant (60 s, -4%). Ogni suo intervento chiude la prova: una discesa rapida oltre lo SL mentre lo stop-limit si esegue puo' terminare la prova per un evento di mercato, non per un difetto. Accettato come conservativo; da rivedere solo se succede.
4. Soglia di dust: il gateway la giudica allo stop senza margine, il quant usa un margine del 2% (`N_br`). Fra 1,00 e 1,02 EUR allo stop il bracket viene tentato; se l'exchange lo rifiuta, la prova termina con uscita e CONFLITTO. Il caso richiede un fill parziale di quella taglia esatta. Non e' verificato se Coinbase controlla `quote_min_size` anche sul figlio stop-limit a `SL * 0.95`: rischio solo per i fill parziali, non per `n` circa 2,96 EUR.
5. `C` = 20 EUR ma solo `L_tot` = 3 EUR e' mai a rischio: `C` serve solo al limite giornaliero (come frazione) e al limite fisico del deposito (sez. 10, punto 3).
6. Da verificare prima del live, con arresto se negativo: GTD con post-only e anticipo minimo di `end_time` (4.2); precisione di `commission_total` (4.2); figlio dello SL a -5% (primo SL).
7. La serie unica SL+WATCHDOG di `AccountGuards` resta nel resto del sistema; la separazione vale solo per la prova. Se debba valere anche per R0 e' una domanda futura per la spec, fuori da P4-pre.
8. Con orizzonte di 8 h lo slot delle 11:00 si usa solo se quello delle 09:00 non si e' riempito: in pratica circa un ciclo al giorno.

---

## Allegato A. Risposta del trader-quant (testo originale, era la sez. 10 della rev. 1)

I rimandi interni a "sez. 1", "4.3", "4.4" e "sez. 5" si riferiscono alla rev. 1 e sono recepiti nella rev. 2.

Autore: trader-quant. Data: 2026-10-26. Le sezioni 0-9 non sono modificate. Legenda: [M] misurato (dati del repo: `data/probe/coinbase_probe.json` del 2026-10-18, `modules/execution/spot.py`); [D] derivato o deciso con calcolo esplicito; [S] stima, giudizio o fatto esterno da verificare in preflight/preview o al primo ciclo. Notazione: `n` = nozionale dell'ingresso, `f_m` = 25 bps, `f_t` = 50 bps, `L_tot` = perdita totale massima, `L_rem` = `L_tot` meno la perdita cumulata.

### 10.0 Dati di partenza e minimi d'ordine

- [M] BTC-EUR: `quote_min_size` 1 EUR, `base_min_size` 0.00000001 BTC (circa 0,0007 EUR), `base_increment` 1e-8, `quote_increment` 0.01 (circa 0,0014 bps sul prezzo); spread 1,09 bps; ATR14 1h 32,5 bps, 1d 243,1 bps; tier `Intro` maker 25 bps, taker 50 bps (`assumed = false`). Valori del 18-ott: si rileggono in preflight e, se diversi, si ricalcolano 10.1-10.3.
- [D] Il minimo vincolante e' `quote_min_size` = 1 EUR, non `base_min_size`. 10 EUR per ingresso sono 10 volte il minimo: per l'ingresso NON siamo vicini al minimo.
- [D] Il minimo che conta e' quello del bracket: ogni gamba SELL deve rispettare `q * SL >= quote_min_size`. Soglia di proteggibilita': `N_br = quote_min_size * 1.02 / (1 - sl_frac)` = 1,03 EUR con `sl_frac` 0,006 (2% di margine sul minimo). Una quantita' posseduta con nozionale < `N_br` e' dust: non si puo' coprire con un bracket.
- [D] Conseguenza per 4.3 e 4.4: un ordine di test a `min_size` (circa 1,00 EUR), se si eseguisse per errore, avrebbe la gamba SL a circa 0,99 EUR < 1 EUR: bracket rifiutato, e il "prosegue come ingresso normale con bracket" della 4.3/4.4 non sarebbe possibile. Gli ordini di test vanno a 2,00 EUR di nozionale (`size = ceil_to(2.00 / prezzo, base_increment)`). Se non si eseguono, il costo e' nullo.

### 10.1 Domanda 1: Q_max

- [D] Nella sez. 1 c'e' un'incoerenza. La regola di `ProbeBudget` ammette `required_quote <= equity_probe - (C - L_tot)`, che all'avvio vale 20 - (20 - 3) = 3 EUR. Con `L_tot` = 3 il `Q_max` effettivo e' 3 EUR, non 10.
- Decisione [D]: si mantiene la regola, perche' e' la forma corretta di "perdita massima = capitale allocato": anche la perdita del 100% della posizione (SL non eseguito, bot morto) non supera `L_tot`. Per togliere l'ambiguita' si fissa `Q_max = 3.00 EUR` (`required_quote`, fee di copertura incluse), quindi `n` = 3,00 / 1,012 = 2,96 EUR circa. Il tetto per ingresso resta sempre `min(Q_max, L_rem)`.
- Fine per esaurimento [D]: se `min(Q_max, L_rem) < N_br * (1 + f_cov)` (circa 1,05 EUR), la prova termina, perche' non e' piu' possibile un ingresso proteggibile.
- Perche' non `min_size` (1-2 EUR): il bracket non e' piazzabile o e' al limite (10.0) e quasi ogni fill parziale diventa dust.
- Perche' non 10 EUR: servirebbe `L_tot` >= 10 EUR, oppure sostituire la regola con un sizing basato sullo stop, che presuppone l'esecuzione dello SL. L'esecuzione non e' garantita (sez. 4.1.4 e 10.5). Sconsigliato.
- Vincolo collegato [D]: con `n` = 2,96 EUR la fee maker e' 0,0074 EUR, sotto la tolleranza "1 `quote_increment`" (0,01 EUR) del criterio Fee della sez. 5, che cosi' non verificherebbe niente. Anche con 10 EUR la tolleranza varrebbe 10 bps: distinguerebbe 25 da 60 bps ma non 25 da 35 bps. Il criterio va riformulato cosi': `|commission - n_fill * rate| <= max(1e-6 EUR, 0.5 bps * n_fill)`, con `commission` alla piena precisione restituita dall'API. [S] Nello stadio preview (4.2) si verifica la precisione di `commission_total`. Se arriva arrotondata al centesimo, il controllo delle fee non e' verificabile a 3 EUR: ci si ferma e si riapre la domanda, senza alzare `Q_max` in autonomia.
- Costo atteso [D]. Fee per ciclo: TP 25+25 = 50 bps; SL 25+50 piu' slittamento (circa 5) = circa 80 bps; orizzonte 25 + (25..50) + mezzo spread = circa 60 bps. Con le probabilita' della 10.2 la media e' circa 63 bps * 2,96 = 0,019 EUR per ciclo e circa 0,10 EUR su 5 cicli. Il PnL di prezzo ha media zero (nessun vantaggio) e deviazione di circa 0,02 EUR per ciclo. Per confronto, con 10 EUR sarebbero circa 0,06 EUR per ciclo e circa 0,31 EUR in totale. Il round trip di 75-77 bps (circa 0,15 EUR su 20 EUR) vale solo per un ciclo chiuso a SL, non per un ciclo medio.

### 10.2 Domanda 2: livelli e orizzonte

Decisione [D]: `tp_frac = sl_frac = 0.006`, `horizon_min = 480` (8 h).

- [D] Volatilita': con E[range] = 1,6 sigma, l'ATR1h da' 32,5/1,6 = circa 20 bps/h, cioe' circa 1,0% al giorno; l'ATR1d da' 243/1,6 = circa 1,5% al giorno. Intervallo di lavoro sigma_d 1,0-1,5%, valore centrale 1,2%.
- [D] Modello: moto browniano senza deriva, coerente con un test senza vantaggio. Con barriere simmetriche a +-a, P(TP prima) = P(SL prima) = 0,5 indipendentemente da sigma. La probabilita' di nessuna barriera entro T, con L = 2a = 1,2%, e' `P_H = (4/pi) * exp(-pi^2 * sigma^2 * T / (2 * L^2))` (termini successivi trascurabili).
- [D] Risultati (P_H / TP / SL): sigma_d 1,0% -> 0,41 / 0,30 / 0,30; 1,2% -> 0,25 / 0,375 / 0,375; 1,5% -> 0,10 / 0,45 / 0,45. Con 5 cicli riempiti la probabilita' di osservare almeno una volta ciascuna uscita (TP, SL, HORIZON) e' circa 0,6 nel caso centrale.
- [D] La proposta 0,015/0,010 con 24 h da' un mix simile (sigma 1,2%: P_H 0,39, TP 0,24, SL 0,37), ma ha tre svantaggi: posizione notturna, con l'operatore assente per gran parte della sua vita; collisione con lo slot del giorno dopo (l'uscita per orizzonte cade dopo le 09:00 e lo slot salta); un solo ciclo ogni 1-2 giorni. Con 8 h ogni posizione nasce e muore nella giornata dell'operatore e la perdita per SL e' minore.
- [M] SL a 60 bps = circa 1,8 ATR1h: fuori dal rumore di un'ora, ma abbastanza vicino da scattare. Il vincolo dei 2 tick e' soddisfatto con ampio margine.
- Filtro di volatilita' aggiuntivo [D]: nessun ingresso se `ATR1h_bps > sl_frac * 1e4` (60 bps), perche' lo stop starebbe dentro il range di un'ora e il rischio di gap salirebbe. Oggi l'ATR1h e' 32,5 bps: margine 1,8x.

### 10.3 Domanda 3: limiti di perdita

Perdite per ciclo con `n` = 2,96 EUR:
- [D] SL normale: `n * (sl + f_m + f_t + slip)` = 2,96 * (0,006 + 0,0025 + 0,005 + 0,002) = circa 0,046 EUR.
- [D] Gap fino al pavimento dello stop-limit a -5% (10.5): 2,96 * (0,006 + 0,05 + 0,0075) = circa 0,19 EUR.
- [D] Caso estremo (SL non eseguito, bot morto, crollo): perdita <= `n` <= `L_rem`. Mai debito: e' spot, si vende solo base posseduta e la fee del SELL e' una frazione dell'incasso.

Decisioni:
- Giornaliera [D]: 0,30 EUR invece di 1,00 (`max_daily_loss_frac = 0.30 / C = 0.015`). Due SL normali fanno 0,09 EUR, un gap piu' un SL circa 0,24 EUR: oltre 0,30 c'e' un'anomalia. Ingressi bloccati fino alle 00:00 UTC.
- Totale [D]: 3,00 EUR, confermato, legato a `Q_max` (10.1). E' il tetto per costruzione.
- Fine anticipata per anomalia [D]: perdita cumulata > 0,50 EUR (circa 5 volte il costo atteso, sopra il caso di 5 SL normali piu' un gap) -> fine della prova e CONFLITTO.
- Stop consecutivi [D]: con P(SL) = 0,375 e nessun vantaggio, la probabilita' di 2 SL di fila in 5 cicli e' circa 0,39. Chiudere la prova per questo fermerebbe un test d'impianto per puro caso. Quindi 2 SL consecutivi -> pausa fino alle 00:00 UTC del giorno dopo, non fine. Una singola uscita `WATCHDOG` (bracket assente o SL non eseguito) -> fine della prova e CONFLITTO, perche' indica un difetto di protezione, non un esito di mercato. `SL` e `WATCHDOG` vanno quindi contati separatamente, non nella stessa serie.

### 10.4 Domanda 4: spread

- [D] `max_spread_bps = 10` confermato. Lo spread tipico e' 1-2 bps [M]; 10 bps (5-10 volte il tipico) segnala un book sottile o uno stress. Su 2,96 EUR il costo dello spread e' trascurabile (mezzo spread a 10 bps = 0,0015 EUR): il filtro serve a riconoscere lo stress, non a contenere il costo. Al momento dell'invio la quote REST deve avere eta' <= 5 s.

### 10.5 Domanda 5: lo SL allo scatto

- [S] Per la documentazione di Coinbase Advanced (campo `stop_trigger_price` del `trigger_bracket_gtc`), allo scatto viene piazzato uno stop-LIMIT con limite 5% sotto il trigger per i SELL. Nel repo non e' verificato (`spot.py` invia solo `base_size`, `limit_price`, `stop_trigger_price`). Quindi lo SL e' di fatto un taker (fee 50 bps) con pavimento a -5%: in un gap oltre il 5% resta nel book NON eseguito e la posizione e' scoperta sotto il pavimento.
- Verifica obbligatoria al primo SL [S]: registrare l'ordine figlio dal canale `user` e da `get_order` (`order_type`, `limit_price`, `trigger_status`, `liquidity_indicator`) e confermare che `limit_price` sia circa `stop * 0.95`. Se e' diverso: CONFLITTO.
- Slittamento [D]: `slip_bps = (stop_trigger_price - avg_fill_price) / stop_trigger_price * 1e4`. Atteso [S]: mezzo spread piu' 1-3 bps per 3 EUR, cioe' circa 2-5 bps.
- Soglie di allarme, non di gate [D]: oltre 15 bps -> allarme e annotazione; oltre 50 bps -> allarme grave e revisione dell'operatore prima dello slot successivo.
- Condizioni di protezione, queste SI bloccanti [D]: SL scattato e non completamente eseguito entro 60 s, oppure `best_bid < stop * 0.96` con la posizione ancora aperta -> `emergency_exit` market del watchdog, fine della prova, CONFLITTO. Liquidita' `MAKER` su uno SL (lo SL e' rimasto nel book) -> CONFLITTO.

### 10.6 Domanda 6: fee

- [M] Il tier reale e' 25/50 (`Intro`, 18-ott), gia' diverso da 60/120.
- [D] Solo annotare. `fallback_fees` resta 60/120: si usa quando l'API delle fee non risponde e deve restare un tetto prudente; abbassarlo renderebbe ottimista proprio il caso di guasto. I costi del label (`label_config`) non si cambiano per la prova: 5 fill non misurano il tier (lo misura `get_fee_rates`, gia' letto), e il tier dipende dal volume a 30 giorni e dalle scelte di Coinbase. Un aggiornamento dei costi del label e' una decisione separata (rilabel e confronto), fuori da P4-pre. In esecuzione il `CostModel` usa il tier letto in quel momento (`FeeProvider`, `assumed = false`).
- Unico caso bloccante [D]: una `commission` osservata diversa da `n_fill * rate` del tier letto, oltre la tolleranza della 10.1 -> CONFLITTO.

### 10.7 Domanda 7: fill reali e L4

- [D] Solo diagnostica. Con n <= 5 ingressi l'incertezza e' totale: ad esempio, con 4 fill su 5 l'IC 95% Clopper-Pearson del fill rate e' [0,28; 0,99]. Nessun parametro del fill model si stima su questi dati. E' ammesso registrarli in un dataset separato (`source = exec_probe`) e usarli come controllo di coerenza fuori campione (replay con `WsRecorder`: il modello dava una probabilita' non trascurabile all'esito osservato?), mai nel fit e mai mischiati ai trade del modello. La taglia minima e gli slot fissi producono posizione in coda e selezione diverse da quelle della strategia.

### 10.8 Domanda 8: slot

- [D] Slot alle 09:00 e alle 11:00 UTC, lunedi'-venerdi' (10:00 e 12:00 ora italiana, CET dal 25-ott). Lo slot delle 11:00 si usa solo se la posizione delle 09:00 e' gia' chiusa o non si e' riempita.
- [S] Motivi: 07:00-12:00 UTC e' la finestra europea di maggiore liquidita' per le coppie EUR. Il 13:00 proposto, dopo la fine dell'ora legale USA (1-nov), porta la finestra d'ingresso di 30 min a ridosso delle 13:30 UTC (dati macro USA delle 08:30 ET) e vicino all'apertura azionaria USA (14:30 UTC): rischio di gap sullo SL senza alcun beneficio per il test. Con orizzonte di 8 h l'ultima posizione si chiude entro 11:00 + 0:30 + 8:00 = 19:30 UTC (20:30 ora italiana), con l'operatore presente.
- [S] Regola operativa: nessun ingresso nei giorni di FOMC, CPI USA, NFP o riunione BCE (calendario controllato dall'operatore, con `STOP` manuale).

### 10.9 Ingresso non riempito e protezione con il bot morto (caso del 23-ott)

- Ingresso [D]: post-only al bid, nessun riprezzamento, nessun secondo tentativo nello stesso slot; annullato a 1.800 s con conferma terminale.
- Fill parziale al timeout [D]: si annulla il residuo. Se il nozionale riempito e' >= `N_br`, si piazza il bracket su quella quantita'; altrimenti e' dust (<= 1,03 EUR, perdita massima pari al suo valore), contato a mercato in `equity_probe`. Requisito: tutto il BTC del book `probe` sopra `N_br` deve stare sotto un bracket; l'eventuale dust si somma alla quantita' del bracket successivo (la meccanica spetta all'Architetto).
- Rischio principale con PC spento o in standby [D]. NON e' la posizione con bracket, che e' protetta lato exchange (perdita <= sl + gap, comunque <= `n`). I rischi sono tre: (a) l'ingresso BUY pendente: `limit_limit_gtc` resta nel book a tempo indefinito se il bot muore, puo' riempirsi ore dopo e la posizione nasce SENZA bracket; (b) i secondi fra il fill e il bracket `OPEN`; (c) la finestra fra l'annullamento del bracket e l'uscita per orizzonte.
- Requisiti:
  - (a) L'ingresso deve scadere lato exchange, non lato bot: `limit_limit_gtd` con `end_time` = invio + 1.800 s. Oggi `limit_limit_gtd` e' fuori dalla matrice di `spot.py`: decidono l'Architetto e l'utente. In alternativa l'utente accetta per iscritto che, con il bot morto durante i 30 min, una posizione fino a 2,96 EUR possa restare senza stop fino al suo ritorno (perdita massima = `n`, mai debito).
  - (b) Il tempo senza protezione, dal fill al bracket `OPEN`, si misura a ogni ciclo: allarme oltre 10 s; senza bracket `OPEN` entro 30 s, uscita immediata (gia' prevista).
  - (c) Nell'uscita per orizzonte, se l'uscita fallisce dopo l'annullamento del bracket, il bracket va ripiazzato subito.
- [D] Con il bracket sull'exchange, il PC acceso per 8 h serve al watchdog e all'uscita per orizzonte, non alla protezione del capitale. [S] Durante la prova la sospensione di Windows va disattivata (operatore).

### 10.10 Valori proposti per `limits.py`

| Campo | Valore |
|---|---|
| `max_entry_quote` (`Q_max`) | 3.00 EUR, applicato come `min(Q_max, L_rem)` |
| ordini di test (4.3, 4.4) | 2.00 EUR di nozionale |
| `tp_frac` / `sl_frac` | 0.006 / 0.006 |
| `horizon_min` | 480 |
| filtro ATR | nessun ingresso se ATR1h > 60 bps |
| `max_spread_bps` | 10 (quote con eta' <= 5 s) |
| `max_daily_loss_quote` | 0.30 |
| fine per anomalia | perdita cumulata > 0.50 |
| `max_total_loss_quote` | 3.00 |
| SL consecutivi | 2 -> pausa fino alle 00:00 UTC; 1 `WATCHDOG` -> fine |
| slot | 09:00 e 11:00 UTC, lun-ven |
| allarme slittamento SL | 15 bps, grave 50 bps |
| tolleranza fee | `max(1e-6 EUR, 0.5 bps * n_fill)` a piena precisione |

### 10.11 Richiede decisione dell'utente

1. `Q_max` = 3,00 EUR (al posto di 10) con `L_tot` = 3,00 EUR. L'alternativa, alzare `L_tot`, e' sconsigliata.
2. Orizzonte di 8 h e slot alle 09:00 e alle 11:00 UTC, con l'operatore presente fino alle 19:30 UTC.
3. Limiti: giornaliero 0,30 EUR, fine per anomalia 0,50 EUR, totale 3,00 EUR; 2 SL consecutivi = pausa, non fine.
4. Ingresso con scadenza lato exchange (GTD, richiede anche l'Architetto per la matrice di `spot.py`) oppure accettazione scritta del rischio (a) della 10.9.
5. Ordini di test a 2,00 EUR invece che a `min_size`.
6. Calendario macro a carico dell'operatore e sospensione del PC disattivata durante la prova.
