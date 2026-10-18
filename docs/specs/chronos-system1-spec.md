# Chronos v2 — Specifica Tecnica "System 1" Decision Architecture

Versione: 2.6.1-draft | Data: 2026-10-18 | Stato: proposta per implementazione
Target implementatore: Sonnet 5.5 (ogni blocco "Linee Guida" e' scritto per essere usato come prompt diretto)

Changelog 2.1: perimetro infrastrutturale a costo zero (locale, CPU-only, AWS fuori scope); nuova sez. 5 "Dry Run"; piano di rilascio riscritto; la sandbox Coinbase e' statica e non viene piu' usata per validare i payload.

Changelog 2.2 (risposta alla review di implementazione):
- modello di costo unico e asimmetrico (sez. 0.3), usato da label, RiskManager, FillModel e stato;
- parametri di barriera v1 ricavati da dati reali del 2026-10-17 (sez. 0.4): ATR giornaliero, ingresso maker, orizzonte di giorni; i default 15m/240 min della 2.1 non coprivano le fee;
- prodotti v1: BTC-EUR, ETH-EUR, SOL-EUR (XLM-EUR escluso per illiquidita');
- nuova sez. 0.5 "Fondamenta P0" (config, backfill, market stream, storage, Python 3.12, branch);
- nuova sez. 0.6 "Ambito per book" (RiskManager, KillSwitch, daily loss);
- backend di default `baseline` anche nella config; policy versionata per modello nel registry;
- veto di regime solo nella DecisionPolicy, regime = argmax;
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

`ExecutionGateway` e' un'interfaccia con due implementazioni: `LiveExecutionGateway` (ordini reali, sez. 4) e `ShadowExecutionGateway` (simulazione tick-level sul flusso reale, sez. 5). Il container `decider` e' opzionale (profilo `gpu`); senza di esso il `DecisionEngine` usa il `BaselineDecider` in-process.

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
```

**EV a due esiti (limite inferiore).** `p = p_edge = P(UPPER)` (sez. 3.1.3). `1 - p` comprende LOWER e VERTICAL, ed entrambi sono valutati come uno SL. Un'uscita VERTICAL avviene con il prezzo strettamente dentro le barriere e costa `cost_mkt <= cost_sl`, quindi la sua perdita in bps e' sempre minore di `sl_bps + cost_sl`. Ne segue che `EV(p_edge)` e' un limite inferiore dell'EV reale e `p_breakeven` un limite superiore del breakeven reale. E' voluto: il gate e' prudenziale e usa solo la probabilita' calibrata della domanda `edge`. Il costo di questa prudenza e' misurato dallo studio di fattibilita' (`p_vertical`, `vert_mean_net_bps`, sez. 3.1.3) ed e' gia' incorporato nelle soglie, perche' `tune_policy` valuta ogni combinazione con il backtest completo, RiskManager incluso (sez. 3.1.6). Stimare `P(VERTICAL)` a runtime richiederebbe una nuova domanda (ad esempio `choice` UPPER/LOWER/VERTICAL), quindi una nuova versione di `QUESTIONS` e del modello: fuori scope v2.

Le formule in bps sono approssimazioni da usare **solo** per gate e feature. Il PnL realizzato, simulato e delle label e' sempre esatto, calcolato sui prezzi di fill e sulle fee effettive di ciascuna gamba:

```
entry_cost_quote = entry_px * size * (1 + f_in)
exit_value_quote = exit_px  * size * (1 - f_out)
net_ret          = exit_value_quote / entry_cost_quote - 1
```

dove `entry_px` ed `exit_px` includono gia' spread e slippage della gamba taker (e `x` per lo SL).

**Esempio di riferimento** (usato in tutti gli esempi del documento, fee presunte 60/120): SOL-EUR, `best_bid = 92.03`, `s = 3`, `ATR_14_1d = 460 bps` (4.2334 EUR), ingresso maker a 92.03, `k_tp = 4`, `k_sl = 2`.
TP = 108.96 (`tp_bps` = 1840), SL = 83.57 (`sl_bps` = 920); `cost_tp` = 120, `cost_sl` = 60 + 120 + 1.5 + 5 + 10 = 197; fee floor 1720 >= 30, ok; `p_breakeven` = 1117 / 2837 = **0.39**. Con `p_edge` = 0.52: EV = 0.52 x 1720 - 0.48 x 1117 = **+358 bps**.

### 0.4 Parametri di barriera v1 e prodotti (dati reali del 2026-10-17)

Misure dal probe di sola lettura `script/probe_coinbase.py` (endpoint pubblici, fee presunte maker 60 / taker 120 bps):

| Prodotto | Spread bps | Volume 24h EUR | ATR14 1h | ATR14 6h | ATR14 1d | Copertura candele 1m (a 3 anni) |
|----------|-----------|----------------|----------|----------|----------|-------------------------------|
| BTC-EUR | 2.9 | 5.7 M | 30 | 120 | 253 | ~98% |
| ETH-EUR | 2.0 | 1.8 M | 48 | 187 | 375 | ~98% |
| SOL-EUR | 3.3 | 1.0 M | 61 | 222 | 460 | ~64% (~97% a 2 anni) |
| XLM-EUR | 5.9 | 0.08 M | 50 | 221 | 585 | ~30%: **escluso** (minuti senza scambi, book sottile) |

`p_breakeven` e **lift richiesto** (= `p_breakeven` meno la probabilita' di toccare prima il TP in un random walk senza drift, `k_sl / (k_tp + k_sl)`), cioe' di quanto il modello deve battere il caso:

| ATR | k_tp/k_sl | Ingresso | BTC lift | ETH lift | SOL lift |
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
- **Barriere:** `atr_tf = 1d`, `k_tp = 4.0`, `k_sl = 2.0`, `entry_mode = maker`, orizzonte `H = 14400 min` (10 giorni; tempo atteso di uscita di un random walk con barriere +4/-2 ATR giornalieri ~ 8 giorni).
- Questi sono **default di partenza**, non verita': la scelta finale la fa lo studio di fattibilita' su 3 anni di storico con le fee reali (`training/feasibility.py`, sez. 3.1.3), che puo' sostituirli.
- Conseguenza da accettare: a questo tier di fee Chronos e' un sistema **swing multi-giorno**. Le label si risolvono dopo 10 giorni e un trade dura in media piu' di una settimana. La prova statistica dell'edge viene dal backtest walk-forward sullo storico; il dry run verifica che il sistema reale si comporti come il backtest (sez. 5.1.6–5.1.7).
- Il tier migliora solo con il volume a 30 giorni, che con micro-capitale non cambia: i parametri vanno ricalcolati se le fee reali lette dal probe sono diverse da quelle presunte.

### 0.5 Fondamenta (fase P0)

Componenti trasversali che le sezioni 1–5 danno per esistenti. Sono il primo lavoro da implementare.

| Tema | Decisione |
|------|-----------|
| Branch | Lavoro su branch `v2`. Tag `v1-final` sull'attuale `main`. Ogni modulo v1 viene cancellato nella stessa PR che introduce il suo sostituto v2 (es. `vector_storage.py` e `sentiment_analysis.py` con la sez. 1). `main.py` passa all'orchestratore v2 alla fine di P2. La v1 non va tenuta in esecuzione: non e' funzionante (`KeyError` sulla config, payload d'ordine non validi). |
| Python | 3.12. `Dockerfile` su `python:3.12-slim`. `mypy --strict`, `ruff`, `pytest` in CI locale (`docker compose --profile test`). |
| Storage su Windows | I DB SQLite stanno in un **named volume** Docker `chronos-state` montato su `/app/state` (WAL su bind mount NTFS non e' affidabile). I file append-only restano sul bind mount `./data`, gli artefatti su `./models`. Backup giornaliero dei DB con `sqlite3.Connection.backup()` in `./data/backup/` (rotazione 7 giorni). |
| Percorsi | `state/news.db` (news e sentiment, condiviso tra modalita'), `state/chronos_live.db`, `state/chronos_dry.db`; `data/bars/1m/product=<id>/year=<y>/month=<m>.parquet`; `data/decisions/`, `data/live_labels/`, `data/ws_recordings/`, `data/reports/`, `data/probe/`; `models/registry/`. |
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

**Invarianti delle barre 1m.** (I1) Esiste una sola definizione di barra 1m: la candela REST `ONE_MINUTE` definitiva. Storico, runtime, store, label, replay e training usano solo questa; nessuna barra costruita dai trade WebSocket entra nello stato, nello store o nelle label (le due serie differiscono nell'assegnazione dei trade ai minuti, misurato il 2026-10-17, e lo scarto cambierebbe i token dello stato). (I2) Per ogni prodotto la sequenza di barre consumate dall'`IndicatorEngine` coincide con `load_bars(store)`: lo store e' la registrazione di cio' che il runtime ha consumato e non viene mai riscritto per correggere revisioni successive dell'exchange, che sono solo misurate (sez. 5.1.6, L0).

Ordine di implementazione P0: `modules/config.py` -> `modules/market/backfill.py` + `load_bars()` -> `indicators_batch.py` -> `indicators.py` (incrementale, test di parita' sullo storico) -> `bars.py` -> `ws_feed.py` + `orderbook.py` -> `ledger/` -> `dryrun/recorder.py`.

Criterio di uscita P0: storico 1m backfillato per i tre prodotti; test di parita' batch/incrementale verde su >= 5.000 barre per prodotto; 48 h di stream WS registrato senza buchi di sequenza non gestiti; 48 h di `BarService` con `rest_revision_ratio <= 0.005` per prodotto, misurato rileggendo da REST i minuti con eta' >= 15 min.

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
| `edge` | `noul` | P(che un long aperto ora con le barriere ATR standard tocchi il TP prima dello SL entro l'orizzonte H). | Gate di valore atteso + sizing (sez. 4). |
| `vol_regime` | `score` [very_low, low, normal, high, extreme] | Regime di volatilita' realizzata **futura** sull'orizzonte H. | Veto di ingresso (solo nella `DecisionPolicy`, sez. 2.1.2) e moltiplicatore di sizing (solo nel `RiskManager`, sez. 4.1.1). |

Flusso per ogni prodotto, ogni chiusura di barra da 1 minuto:

```
bar_closed(product, t)                             # pubblicata dal BarService a t + S (sez. 2.3 A.2)
  -> IndicatorEngine.snapshot(product, t)          # O(1), incrementale
  -> SentimentService.snapshot(product, t)
  -> PositionLedger.view(product)
  -> StateBuilder.build(...) -> DecisionState (pydantic)
  -> serialize_state(state) -> str canonica         # stessa funzione usata in training
  -> DecisionClient.decide(state_str, QUESTIONS)    # 1 richiesta HTTP, budget 250 ms
  -> DecisionPolicy.resolve(answers, position)      # mascheramento azioni invalide + soglie
  -> Decision(BUY|SELL|HOLD, p_edge, regime, ...)
  -> RiskManager / ExecutionGateway (sez. 4)
  -> DecisionRecord -> data/decisions/YYYY-MM-DD.jsonl (audit, replay, dataset futuro)
```

**Istante logico e istante di calcolo.** `t` = chiusura del minuto (`ts_bar_close`); la decisione si calcola a `t + S` (`market.bars.rest_delay_s`, default 120 s), quando la candela REST del minuto e' definitiva. Tutto lo stato usa l'istante logico `t`: barre fino al minuto che chiude in `t`, `clock` da `t`, sentiment con `as_of = t` (`ingested_at <= t`). Fanno eccezione, perche' descrivono l'esecuzione, `position` e `costs`/`spread_bps`, letti al momento del calcolo. Il ritardo `S` e' irrilevante a questo orizzonte: 120 s sono lo 0.014% di `H`, la finestra di fill maker reale `[t+S, t+S+T_in]` copre il 93% di quella della label, e lo scarto fra il limite reale (best_bid a `t+S`) e quello della label e' una deriva di prezzo di 2 minuti a media nulla, piccola rispetto a barriere di piu' ATR giornalieri. Il prezzo dell'ordine viene comunque dal `LocalOrderBook` all'invio. Una barra pubblicata con ritardo oltre `market.bars.decision_max_lag_s` aggiorna indicatori e store ma non produce decisione ne' `DecisionRecord` (contatore `decisions.skipped_late`).

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
2. **Regime:** `regime = argmax(vol_regime.probabilities)` (indice 0..4). Lo `score` (media pesata) viene solo loggato. La regola vale per tutti i backend e per il RiskManager.
3. **Soglie** (`policy.json` del modello, sez. 2.2.5):
   - `BUY` se `p(BUY) >= tau_buy` **e** `action.confidence >= c_min` **e** `edge.noul >= p_edge_min` **e** `regime in allowed_regimes_for_entry`;
   - `SELL` (solo se LONG) se `p(SELL) >= tau_sell`;
   - altrimenti `HOLD`.

   Il **veto di regime esiste solo qui**. Il `RiskManager` riceve soltanto decisioni con regime ammesso e ne usa il moltiplicatore di sizing; se riceve un regime non ammesso solleva `PolicyContractError` (errore di programmazione, non uno skip).

   **Confidenza comparabile tra backend:** ogni backend restituisce probabilita' gia' calibrate (temperature o Platt del proprio model card); la confidenza viene sempre ricalcolata dal client come `1 - H(p)/ln K` su quelle probabilita', mai presa dal backend. Le soglie non sono condivise: ogni versione di modello ha il proprio `policy.json`, prodotto da `training/tune_policy.py` sul proprio validation set. Baseline e LLM hanno quindi soglie diverse.

   **Pavimento economico:** `p_edge_min` non dovrebbe mai scendere sotto il `p_breakeven` (sez. 0.3) del trade corrente. Il gate EV del RiskManager lo garantisce comunque; la policy evita solo di generare intent inutili.
4. **Coerenza**: se `action=BUY` ma `edge.noul < p_edge_min`, la policy forza `HOLD` e registra `reason="edge_veto"`. Le incoerenze tra domande vengono monitorate (sono un segnale di drift).
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
  "liquidity": {"spread_bps": 3, "vol_z_1d": 0.6},
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
- `costs`: calcolati dal `CostModel` della sez. 0.3 con fee, spread e ATR correnti. Il modello deve "vedere" i costi perche' le label sono nette di fee (sez. 3).
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
| `liquidity.spread_bps` | non prodotto dall'`IndicatorEngine` (LocalOrderBook live) | - | bps |
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
  "policy": {"policy_version": "baseline-lgbm@2026-10-16.1", "regime": 2,
             "masked_probs": {"BUY": 0.72, "HOLD": 0.28}, "final": "BUY", "reason": "thresholds_met",
             "tau_buy": 0.55, "p_edge_min": 0.44, "p_breakeven": 0.39},
  "latency_ms": {"build": 1.4, "inference": 2.1, "http_total": null, "server_model": null},
  "order_intent_id": "01JAB3...ULID"
}
```

`bar_source` in `rest | flat | flat_unproven` (sez. 2.3 A.2); `bar_lag_s = published_at - ts_bar_close` della barra; `decided_at` = istante di calcolo.

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
  "p_edge_min": 0.44,
  "allowed_regimes_for_entry": [1, 2, 3],
  "tuned_on": {"split": "val_fold_5", "n_samples": 41250, "objective": "net_pnl_bps", "min_trades": 30},
  "label_config_sha256": "a41c..."
}
```

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
> Taratura di S: i default seguono S con `rest_max_delay_s = S + 60`, `decision_max_lag_s = S + 120`, `risk.kill_switch.stale_bar_s = S + 150`. Il valore di S e' accettato solo da un soak di 48 h con `rest_revision_ratio <= 0.005` per prodotto (minuti riletti con eta' >= 15 min). Se il criterio fallisce: se prevalgono revisioni di valore su barre `rest`, si sale di un gradino (S = 120 -> 180 -> 300) con un nuovo soak; se prevalgono candele comparse o scomparse, oppure al gradino 300, si apre un CONFLITTO. Il criterio non si allenta e S non si abbassa sotto il valore validato.
> 3. `indicators.py` — `IndicatorEngine`, **incrementale O(1) per barra**, uno stato per (product, timeframe) con timeframe 1h, 6h, 1d aggregati dalle barre 1m (barre 1d con chiusura alle 00:00 UTC): ATR di Wilder (14), RSI di Wilder (14), EMA per MACD (12/26/9), Bollinger (20, 2), VWAP rolling 1d, volatilita' realizzata `RVd`, z-score di `ln(1 + quote volume)` giornaliero (30 giorni), percentile dell'ATR 1d su 365 giorni (`sortedcontainers.SortedList`) e i campi di prezzo, con le definizioni esatte della sez. 2.2.1.b. Fornisci anche la versione vettoriale degli stessi indicatori in `indicators_batch.py` (polars o pandas). Un test deve verificare che batch e incrementale coincidano (tolleranza `|a-b| <= 1e-9 * max(1, |b|)`, maschere NaN identiche; test T1-T10 della risoluzione indicatori, `docs/decisions/2026-10-17-indicatori.md`) su >= 5.000 barre 1m per ciascun prodotto, lette dallo storico backfillato in Parquet (sez. 0.5); `data/historical_*.csv` della v1 non va usato. La versione batch e' quella usata per costruire il dataset di training.
> 4. All'avvio, warm-up: `load_bars` sull'intero storico del prodotto (ancora della sez. 2.2.1.b) e replay di tutte le barre 1m nello stesso `IndicatorEngine` (`warm_up`, senza snapshot). Un warm-up parziale non e' ammesso: le ricorrenze di Wilder ed EMA dipendono dal punto di partenza. Budget <= 120 s per prodotto. Se il Parquet non copre il periodo, il processo esegue prima il backfill incrementale (sez. 0.5) e solo dopo apre gli stream. Dopo il warm-up il `BarFinalizer` viene inizializzato (`seed`) con l'ultima riga del frame del warm-up; il primo ciclo del `BarService` recupera da REST i minuti tra quella riga e `now - S`.
>
> **B. `modules/features/`**
> 1. `schemas.py` — pydantic `DecisionState` con i sotto-modelli della sez. 2.2.1. Campi interi dichiarati `int`, i decimali con validator che arrotonda (`round(x, 2)`). Ordine dei campi = ordine di dichiarazione.
> 2. `state_builder.py` — `StateBuilder.build(product, as_of, indicators, sentiment, position, costs) -> DecisionState`. Funzione pura. Gli indicatori sono gia' in bps: conversione `int(round(x))` (non `x * 10_000`); `NaN -> None`. Clamp: `ret_bps`, `seq_1h_bps`, `seq_1d_bps`, `dist_vwap_1d_bps`, `macd_hist_1d_bps` in [-5000, 5000]; `vol_z_1d` in [-6, 6]; `bb_pos_1d` in [-1.0, 2.0]; `hh_ll_pos_30d` e rapporti con `round(x, 2)`. Per evitare token fuori distribuzione.
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
> 3. `baseline.py` — `BaselineDecider` con la stessa firma di `decide`, che produce `DecisionAnswers` da tre modelli LightGBM (multiclass per action, binary per edge, multiclass per regime) sugli stessi campi numerici dello stato appiattito (`flatten_state()` in `features/serializer.py`). Le probabilita' grezze passano dalla calibrazione del model card (Platt per `edge`, temperature per `action` e `vol_regime`) prima di essere restituite. La confidenza la calcola il client (sez. 2.1.2), non il decider.
> 4. `policy.py` — `DecisionPolicy(config).resolve(answers, position_side) -> Decision` secondo la sez. 2.1.2. Funzione pura, tabella di test parametrizzata con tutti i casi (FLAT/LONG x BUY/SELL/HOLD x veto edge x veto regime x decider down).
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

Per ogni prodotto e ogni timestamp di campionamento `t` (chiusura barra 1m), con i parametri del `label_config` (default sez. 0.4) e i costi del `CostModel` (sez. 0.3):

```
A          = ATR_14_<atr_tf>(t)                             # noto al tempo t (default atr_tf = 1d)
# ingresso: stessa regola del BarFillModel (sez. 5.1.3)
if entry_mode == maker:
    limit  = close[t] * (1 - s/2/1e4)                       # stima del best_bid al tempo t
    fill   = prima barra k in (t+1, t+T_in] con low[k] < limit   # T_in = entry_timeout (default 30 min)
    se nessun fill -> esito = NO_FILL (nessun trade)
    entry  = limit ; f_in = f_m
if entry_mode == taker:
    entry  = open[t+1] * (1 + (s/2 + slip)/1e4) ; f_in = f_t
upper      = entry + k_tp * A
lower      = entry - k_sl * A
vertical   = t_fill + H                                     # default H = 14400 min (10 giorni)
scan 1m bars in (t_fill, vertical]:
    if low <= lower and high >= upper nella stessa barra -> esito = LOWER   (conservativo)
    elif high > upper  -> esito = UPPER, exit_px = upper,                      f_out = f_m
    elif low  <= lower -> esito = LOWER, exit_px = min(lower, open) * (1 - (s/2 + slip + x)/1e4), f_out = f_t
se nessuna barriera  -> esito = VERTICAL, exit_px = close[vertical] * (1 - (s/2 + slip)/1e4),  f_out = f_t
net_ret    = exit_px * (1 - f_out) / (entry * (1 + f_in)) - 1                 # formula esatta della sez. 0.3
```

La scansione e la stima dei costi chiamano gli stessi oggetti del runtime (`CostModel`, `BarFillModel`), non reimplementazioni. `s` (spread) per il training e' la mediana per prodotto e ora del giorno misurata dal recorder (sez. 5); finche' non ci sono registrazioni si usa il valore del probe. I parametri sono scritti nel `label_config` del model card e **devono** coincidere con quelli usati dal RiskManager (sez. 4), altrimenti `edge.noul` non ha significato in esecuzione.

`t` e' l'istante logico di chiusura della barra (`ts_bar_close`), non l'istante di calcolo `t + S` (sez. 2.1): le label non dipendono da `S`. Lo scarto tra l'ingresso simulato a barre e l'ingresso reale a `t + S` rientra nella parita' misurata dal dry run (sez. 5.1.7).

**Studio di fattibilita' (`training/feasibility.py`, prima del primo training).** Sullo storico backfillato e con le fee reali (o presunte, marcate come tali), valuta la griglia `atr_tf in {6h, 1d}`, `k_tp in {3, 4, 5}`, `k_sl in {1.5, 2}`, `entry_mode in {maker, taker}`, `H in {5, 10, 15} giorni` e per ogni combinazione calcola: frequenza storica di UPPER (`p_hist`), `p_breakeven` mediano, `lift richiesto = p_breakeven - p_hist`, quota di timestamp che passano il fee floor, tasso di fill dell'ingresso maker, numero atteso di trade indipendenti per anno. Come sola diagnostica, che non entra nella regola di scelta, riporta anche la frequenza dell'esito VERTICAL (`p_vertical`) e il `net_ret` medio esatto dei soli esiti VERTICAL (`vert_mean_net_bps`): misurano quanto e' prudenziale l'EV a due esiti della sez. 0.3. Regola di scelta: tra le combinazioni con fee floor superato in >= 80% dei timestamp e fill maker >= 50%, si prende quella con lift richiesto minimo, a parita' quella con piu' trade indipendenti. Output: `label_config.json` + tabella Markdown in `data/reports/feasibility.md`. **Se nessuna combinazione ha lift richiesto <= 0.10, il report lo dichiara e il progetto non procede al training:** a quel tier di fee la strategia non e' sostenibile e va rivista (prodotti, fee, orizzonte) prima di scrivere altro codice.

Label per domanda:

| Domanda | Stato posizione nel sample | Regola target |
|---------|----------------------------|---------------|
| `action` | FLAT | `BUY` se esito = UPPER **e** `net_ret > 0`, altrimenti `HOLD` (compreso `NO_FILL`). |
| `action` | LONG (sintetico, vedi sotto) | `SELL` se, uscendo ora a mercato (costo `c_mkt_out`), il risultato e' migliore di quello del bracket ancora attivo: cioe' esito futuro del bracket = LOWER, oppure VERTICAL con `exit_px * (1 - f_t)` < valore di uscita immediata. Altrimenti `HOLD`. |
| `edge` | FLAT e LONG | `true` se esito = UPPER, `false` se LOWER o VERTICAL. I sample `NO_FILL` vengono esclusi per questa domanda (la domanda e' condizionata all'apertura). |
| `vol_regime` | FLAT e LONG | Bucket 0..4 di `fwd(t) = R(E_t + H)` rispetto ai quantili 20/40/60/80 (`np.quantile`, `method="linear"`) di `ref(t) = {R(E) : F_t - 365 d < E <= F_t}`, con `R = RVd(H/60, .)` della sez. 2.2.1.b, `E_t = ceil_to_hour(t)`, `F_t = floor_to_hour(t)`; `bucket = searchsorted(q, fwd, side="right")`. Nessuna label se `fwd` o uno dei valori di `ref` e' NaN (quindi le label `vol_regime` esistono solo da circa 375 giorni dopo l'inizio dello storico). |

**Sample LONG sintetici:** per ogni `t` campionato si genera anche uno stato con `position.side = LONG`, dove l'ingresso fittizio e' a `t - U`, con `U ~ Uniform{60..H}` minuti. Si calcolano `unreal_bps`, `dist_sl_atr`, `dist_tp_atr` coerenti con quell'ingresso e si scartano i sample in cui il bracket fittizio sarebbe gia' stato chiuso prima di `t`. In questo modo il modello vede entrambe le condizioni di posizione e impara il mascheramento.

#### 3.1.4 Campionamento, bilanciamento, split

- **Campionamento:** non ogni minuto (autocorrelazione estrema, label quasi duplicate). Si usa l'unione di (a) una griglia oraria e (b) eventi del filtro CUSUM simmetrico sui log-rendimenti 1m con soglia `h = 1.0 * ATR_1h%`. Ordine di grandezza atteso: ~10–15k timestamp per prodotto per anno. Con un orizzonte di 10 giorni le label si sovrappongono molto: i pesi di unicita' (sotto) sono obbligatori, e il numero di **finestre indipendenti** (~36 per prodotto per anno) e' quello da usare quando si giudica la significativita' statistica.
- **Bilanciamento:** `HOLD` domina. Sottocampionamento di `HOLD` a un rapporto massimo 3:1 rispetto alla classe di minoranza **solo nel train**; validation e test restano con la distribuzione naturale (necessaria per la calibrazione). Pesi per sample = unicita' media della label (inverso del numero di label concorrenti che si sovrappongono nel tempo), alla Lopez de Prado.
- **Split:** walk-forward con espansione, 5 fold. Tra train e validation/test: **purge** dei sample la cui finestra `[t, t+T_in+H]` interseca il periodo di test + **embargo** di 2 giorni. Nessuno split casuale.
- **Holdout finale:** ultimi 120 giorni, non toccati fino al gate finale.

#### 3.1.5 Sentiment storico e feature dropout

Le news RSS non hanno storico. Strategia:
1. **v1.0:** sentiment storico ricostruito da GDELT (titoli con timestamp di ingestione GDELT, usati come `ingested_at`) passato nello stesso classificatore ONNX e nello stesso aggregatore. Dove GDELT non copre, `sentiment: null`.
2. **Feature dropout:** nel 25% dei sample di train il blocco `sentiment` viene forzato a `null`, cosi' il modello e' robusto sia agli outage live sia alla differenza di distribuzione GDELT vs RSS live.
3. **v1.x:** retraining mensile includendo i `DecisionRecord` live, che contengono il sentiment reale delle sorgenti RSS. Dopo >= 6 mesi di dati live, il peso dei dati GDELT viene ridotto.

#### 3.1.6 Calibrazione post-training

Temperature scaling **per domanda** (`T_action`, `T_edge`, `T_regime`) sul validation fold piu' recente. Le temperature sono salvate nel model card e applicate dal server. Dopo la calibrazione, `DecisionPolicy` viene tarata (grid search delle soglie `tau_buy`, `p_edge_min`, `c_min`) massimizzando il PnL netto simulato sul validation set con `training/backtest.py`, cioe' con la stessa `DecisionPolicy` e lo stesso `RiskManager` del runtime (fee floor ed EV gate della sez. 0.3 attivi), con vincolo `trade_count >= 30` per fold. L'output e' `policy.json`.

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
    "atr_1d_bps": 460,
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
  "label_config": {"k_tp": 4.0, "k_sl": 2.0, "horizon_min": 14400, "atr": "ATR_14_1d", "entry_mode": "maker",
                   "entry_timeout_min": 30, "cost_model": "COST_V1",
                   "fees_bps": {"maker": 60, "taker": 120, "source": "assumed"},
                   "slip_bps": 5, "stop_extra_slip_bps": 10, "sha256": "a41c..."},
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
| G1 Calibrazione | ECE (15 bin) <= 0.05 per `edge` e per la classe BUY di `action`, su ogni fold di test. |
| G2 Discriminazione | Log-loss di `edge` inferiore a quella del predittore a priori (frequenza base) su >= 4/5 fold. |
| G3 Economico | Backtest event-driven (sez. 3.5) con fee reali e slippage: PnL netto e Sharpe del modello > baseline LightGBM su >= 3/5 fold **e** sull'holdout. |
| G4 Robustezza | Con `sentiment = null` forzato su tutto l'holdout, il PnL netto non scende sotto il 70% di quello con sentiment. |
| G5 Latenza | p99 server <= 150 ms sotto carico di N prodotti in parallelo. |
| G6 Dry run | G-DRY-OPS della sez. 5.1.7 superato con il modello candidato come shadow book, piu' la regola di promozione L5 della sez. 5.1.6. |

Se G3 fallisce, il sistema va in produzione con `BaselineDecider`. Il modello LLM deve guadagnarsi il posto con i numeri: questa e' la contromisura principale contro l'overfitting di un modello da 9B su pochi campioni indipendenti.

### 3.5 Linee Guida per l'implementazione in Sonnet 5.5

> PROMPT — Pipeline di training e server decider
>
> Crea il package `training/` e la directory `decider/` di Chronos v2. Python 3.12. Librerie: `polars` (dataset), `numpy`, `numba` (scansione barriere), `transformers`, `peft`, `bitsandbytes`, `accelerate`, `datasets`, `lightgbm`, `scikit-learn` (solo metriche), `scipy`, `fastapi`, `uvicorn`, `pydantic` v2. Niente `trl`: il trainer e' un `transformers.Trainer` con `compute_loss` custom.
>
> 1. `training/labeling.py`
>    - `triple_barrier(open, high, low, close, atr, t_idx, label_config: LabelConfig, costs: CostParams) -> (outcome: int8, fill_idx: int32, exit_idx: int32, net_ret: float64)` con l'algoritmo della sez. 3.1.3 (ingresso maker/taker, `NO_FILL`, regola conservativa "entrambe le barriere nella stessa barra -> LOWER", `net_ret` esatto). Kernel interno compilato con `@numba.njit` che riceve solo array e scalari; `CostParams` e' la vista numerica del `CostModel` della sez. 0.3 (`modules/risk/costs.py`), non una copia delle formule;
>    - `forward_rv_bucket(...)` con la definizione della sez. 3.1.3, usando `indicators_batch.rv_per_day_bps` (quantili sui 365 giorni **precedenti** a ogni t, nessun dato futuro);
>    - test con serie sintetiche costruite a mano per ogni esito (UPPER, LOWER, VERTICAL, NO_FILL, stessa barra) e un test che ricalcola a mano il `net_ret` dell'esempio della sez. 0.3.
> 2. `training/sampling.py` — `cusum_events(log_ret, h)` e griglia oraria; `uniqueness_weights(t0, t1)`.
> 2b. `training/feasibility.py` — CLI `python -m training.feasibility --products BTC-EUR ETH-EUR SOL-EUR [--fees-from-probe data/probe/coinbase_probe.json]`, studio della sez. 3.1.3 con la regola di scelta e lo stop esplicito se nessuna combinazione e' sostenibile. Produce `label_config.json` (con `sha256`) e `data/reports/feasibility.md`. Va eseguito **prima** di `build_dataset.py`.
> 3. `training/splits.py` — `purged_walk_forward(timestamps, t1, n_folds=5, embargo=timedelta(hours=24))` che produce indici; test che verifica l'assenza di sovrapposizione tra intervalli `[t, t1]` di train e di test.
> 4. `training/build_dataset.py` — CLI: `python -m training.build_dataset --config config/train.yml`. Passi: carica candele 1m da Parquet -> calcola indicatori con `modules.market.indicators_batch.compute_indicators` sull'intero storico del prodotto (ancora sez. 2.2.1.b), poi seleziona i timestamp (sample con `atr_14_<atr_tf>` NaN esclusi) -> ricostruisce sentiment storico da `data/news_history.db` con `modules.sentiment.aggregator` (stesso codice del runtime) -> costruisce `DecisionState` con `modules.features.state_builder` e serializza con `modules.features.serializer.serialize_state` (stesso codice del runtime) -> genera sample FLAT e LONG sintetici -> label -> feature dropout 25% sul blocco sentiment nel solo train -> scrive `data/datasets/<run_id>/{train,val,test}.jsonl` e `label_config.json`. Fallisce se un token di etichetta non e' single-token.
> 5. `training/prompting.py` — `PROMPT_TEMPLATE_V1`, `render_prompt(state, question) -> str`, `label_token_ids(tokenizer, question) -> list[int]`. Importato sia dal trainer sia dal server.
> 6. `training/train_lora.py` — carica il base model (4-bit NF4 se `quantization.load_in_4bit`), `prepare_model_for_kbit_training`, `get_peft_model(LoraConfig(...))` dai parametri della sez. 3.2.3. Collator: tokenizza `prompt` (senza token di etichetta), registra la posizione dell'ultimo token e gli id dei token di etichetta della domanda. `compute_loss`: `logits_last = outputs.logits[arange(B), last_pos]`, `restricted = logits_last.gather(1, label_ids_padded)` con mascheramento `-inf` dove le domande hanno meno opzioni, `loss = (F.cross_entropy(restricted, target_index, reduction="none") * weight).mean()`. Valutazione ogni 500 step con log-loss ristretta e accuracy per domanda. Salva adapter e tokenizer.
> 7. `training/calibrate.py` — inferenza sul validation set, ottimizzazione di `T_q` per domanda (NLL, `minimize_scalar`), ECE e Brier prima/dopo, reliability diagram PNG con matplotlib. Scrive `temperatures` nel model card.
> 8. `training/backtest.py` — simulatore event-driven barra per barra che usa **la stessa** `DecisionPolicy` e lo **stesso** `RiskManager` del runtime (sez. 4) con un `SimulatedExchange` (fill del limit se `low <= limit` per BUY; bracket TP/SL simulati con la regola conservativa; fee maker/taker da config). Output: equity curve, trade list, Sharpe annualizzato, max drawdown, profit factor, turnover, fee pagate.
> 9. `training/baseline_lgbm.py` — addestra tre LightGBM sugli stessi split usando `flatten_state()`, salva in `models/baseline/`.
> 10. `training/tune_policy.py` — grid search delle soglie su validation -> `policy.json`. Ogni combinazione di soglie viene valutata eseguendo `training/backtest.py` (stessa `DecisionPolicy`, stesso `RiskManager`, quindi fee floor ed EV gate della sez. 0.3 attivi). E' vietata una simulazione semplificata che salti il RiskManager.
> 11. `training/evaluate.py` — esegue G1–G4 della sez. 3.4 e stampa una tabella; exit code 1 se un gate fallisce.
> 12. `decider/server.py` + `decider/Dockerfile` (base `nvidia/cuda` runtime + torch CUDA) secondo la sez. 3.3. Schemi request/response pydantic identici a `modules/decision/client.py` (pacchetto condiviso `modules/decision/protocol.py`). Merge dell'adapter in `decider/export.py`.
>
> Test di parita' obbligatorio (`tests/test_train_serve_parity.py`): dato uno stato golden e un modello tiny (`hf-internal-testing/tiny-random-Qwen2` o equivalente), le probabilita' calcolate dal percorso di training (`compute_loss` in eval) e quelle restituite dal server devono coincidere (tolleranza 1e-4).

---

## 4. RISK MANAGEMENT DINAMICO — Execution Layer

### 4.1 Architettura Concettuale

**Prima (v1):** SL = prezzo x 0.98, TP = prezzo x 1.05, per ogni prodotto e ogni regime; quantita' fissa 5; payload di bracket non valido per l'API.

**Dopo (v2):** barriere espresse in unita' ATR (le stesse delle label, sez. 3.1.3), sizing a rischio fisso modulato dalla probabilita' calibrata e dal regime previsto, gate di valore atteso netto di fee (sez. 0.3), bracket nativo Coinbase allegato all'ordine di ingresso. Un `RiskManager` per book (sez. 0.6).

```
Decision(BUY, p_edge, regime, conf)            # regime gia' ammesso dalla DecisionPolicy
   -> RiskManager[book].plan_entry()
        1. quote: best_bid/best_ask/spread dal LocalOrderBook; product meta (increments, min size) cache 1h
        2. fees: FeeProvider (transaction summary, cache 1h)
        3. A = `atr_14_<atr_tf>` (in prezzo) dello snapshot della barra della decisione, lo stesso valore usato dalla label, mai riletto al momento dell'invio; timeframe dal label_config; entry = floor_to(best_bid, quote_increment) (maker)
                                                       | ceil_to(best_ask, quote_increment) (taker)
        4. SL = entry - k_sl * A ; TP = entry + k_tp * A                  (k da label_config; arrotondati come 4.1.3)
        5. costs = CostModel(entry_mode, fees, spread)                    -> cost_tp, cost_sl (sez. 0.3)
        6. fee floor: tp_bps - cost_tp >= min_net_edge_bps                altrimenti SKIP(fee_floor)
        7. EV gate:   EV(p_edge) > ev_min_bps                              altrimenti SKIP(ev_negative)
        8. sizing:    vedi 4.1.2 (cap di copertura, sola lettura del BalanceBook)  eventuale SKIP(below_min_size)
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
2. **Regime previsto dal modello** (`vol_regime`, volatilita' futura sull'orizzonte H, `regime = argmax`): decide **se** entrare (veto, solo nella `DecisionPolicy`, sez. 2.1.2) e **quanto** rischiare (moltiplicatore, solo qui). Non sposta le barriere.

| Regime | Ingresso (`allowed_regimes_for_entry` di default, nel `policy.json`) | `m_reg` (`risk.regime_multipliers`) |
|--------|---------------------------------------------------------------------|-------------------------------------|
| 0 very_low | Vietato: barriere troppo strette rispetto alle fee | n/a (non arriva al RiskManager) |
| 1 low | Permesso | 0.75 |
| 2 normal | Permesso | 1.0 |
| 3 high | Permesso | 0.6 |
| 4 extreme | Vietato: rischio di gap oltre lo stop | n/a (non arriva al RiskManager) |

Estensione v2.1 (opzionale): `k` dipendenti dal regime, ma solo con label generate per regime (un training set per tabella `k(regime)`) e una domanda `edge` per tabella.

#### 4.1.2 Position sizing

```
equity          = BalanceBook.total(quote) + sum(mark_to_market posizioni del book)   # in memoria, live e dry (sez. 4.1.6)
risk_frac       = risk.risk_per_trade                                        # es. 0.005 = 0.5% equity
scale_p         = clip((p_edge - p_edge_min) / (p_edge_full - p_edge_min), 0, 1)
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

Esempio (sez. 0.3, book da 100 EUR, `p_edge_min` = 0.44, `p_edge_full` = 0.60, `p_edge` = 0.52, regime normal): `scale_p` = 0.5, `risk_eur` = 0.25 EUR, `loss_per_unit` = 8.46 + 1.81 = 10.27 EUR/SOL, `size` = 0.024 SOL (2.21 EUR di controvalore, sopra il minimo di 1 EUR). Con capitali piccoli e stop larghi (ATR giornaliero) le posizioni sono di pochi euro: e' voluto, e' la fase R0/R1 della sez. 5.1.7.

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
| Drift | Globale | PSI > 0.25 su `edge.noul` o sulle feature principali rispetto alla finestra di training | Alert + HOLD-only. |
| Watchdog SL | Per posizione | Posizione con bracket: `last_price <= SL - 1 x ATR_1h` e posizione ancora aperta dopo 10 s. Posizione senza bracket (finestra d'ingresso o bracket rifiutato): `last_price <= SL`. | Cancella bracket, `market_market_ioc` sell (in dry: walk dei bid). La doc Coinbase dichiara che l'esecuzione della protezione downside non e' garantita in alta volatilita'. |
| Coverage breach | Book | `reservation_overdraw`, `hold_exceeds_reservation`, oppure >= 3 `balance_drift` in 24 h (sez. 4.1.6) | Blocca i BUY del book fino a reset manuale; le uscite restano attive. |

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
  "base_size": "0.024",
  "bracket": {"take_profit": "108.96", "stop_loss": "83.57",
              "k_tp": 4.0, "k_sl": 2.0, "atr_tf": "1d", "atr": "4.2334"},
  "risk": {"equity_eur": "100.00", "risk_eur": "0.25", "p_edge": 0.52, "scale_p": 0.5,
           "regime": 2, "m_reg": 1.0, "ev_bps": 358, "cost_tp_bps": 120, "cost_sl_bps": 197,
           "p_breakeven": 0.39, "spread_bps": 3, "min_size_bump": false},
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
  regime_multipliers: {"1": 0.75, "2": 1.0, "3": 0.6}   # solo regimi ammessi; il veto e' nel policy.json
  kill_switch:
    max_daily_loss_frac: 0.02
    max_consecutive_losses: 4
    loss_streak_pause_h: 48
    stale_data_s: 90                  # solo ticker
    stale_bar_s: 270                  # barre 1m (pubblicate a t + S); = S + 150, > decision_max_lag_s
    max_fallback_ratio_1h: 0.20
    psi_threshold: 0.25
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
> 4. `modules/risk/sizing.py` — funzione pura `compute_size(...) -> SizingResult | SkipReason` secondo la sez. 4.1.2. Test con `hypothesis`: la perdita a SL (incluse le fee) non supera mai `risk_eur` + un increment.
> 5. `modules/risk/risk_manager.py` — `RiskManager.plan_entry(decision, market, position) -> OrderIntent | Skip` implementa i passi 1–9 della sez. 4.1 e l'arrotondamento della sez. 4.1.3. Un'istanza per book (sez. 0.6). Legge `k_tp`, `k_sl`, `atr_tf`, `entry_mode`, `horizon_min` dal `label_config` del modello champion caricato all'avvio (e ricaricato allo swap notturno della sez. 5.1.6); se il `label_config` manca, rifiuta di avviarsi. Tutti i costi passano da `modules/risk/costs.py` (`CostModel`, `FeeProvider`, sez. 0.3): scrivi questo file per primo, con test che riproducono esattamente i numeri dell'esempio di riferimento della sez. 0.3 e della sez. 4.1.2. `plan_exit(decision, position) -> ExitIntent`. Ogni `Skip` ha un `reason` enumerato e viene loggato nel `DecisionRecord`.
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
| Ingresso taker, parte marketable | Walk degli ask al primo snapshot del book dopo `t0 + latency_ms`, fino al prezzo limite. | `open[t+1] * (1 + spread/2 + slip_bps)` | taker |
| Ingresso maker (post-only, intero ordine) e residuo taker | Fill solo su trade con prezzo < limite (strettamente); `qty <= queue_haircut * volume_oltre_limite`. Se al momento dell'arrivo il limite incrocerebbe il book, l'ordine e' rifiutato come farebbe l'exchange con `post_only`. | Fill se `low[t+k] < limit` entro `T_in`, haircut sul volume della barra. | maker |
| Timeout ingresso | Cancella il residuo. | Uguale. | — |
| TP (limit sell) | Trade con prezzo > TP; haircut sulla quantita'. | `high > TP`. | maker |
| SL (stop) | Trigger su `last <= stop`; walk dei bid + `stop_extra_slip_bps`. | `min(stop, open)` se la barra apre sotto lo stop (gap), altrimenti `stop * (1 - stop_extra_slip_bps)`. | taker |
| TP e SL nello stesso evento o barra | SL. | SL. | — |
| Uscita del modello (SELL) / watchdog | Walk dei bid. | `open[t+1] * (1 - spread/2 - slip_bps)`. | taker |
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

Ogni minuto e per ogni prodotto il `DecisionEngine` scrive un `DecisionRecord`, anche quando la decisione e' HOLD o SKIP. Il `LabelResolver` gira ogni minuto: per ogni record con `t + H <= now` calcola le label (triple-barrier, `edge`, `vol_regime`) sulle barre 1m reali registrate, usando **la stessa funzione** `training/labeling.py` del dataset storico. Output in `data/live_labels/YYYY-MM-DD.jsonl` nel formato esatto della sez. 3.2.1 (con `split: "live"`).

Effetti:
- ogni minuto produce esempi etichettati (non solo i pochi trade eseguiti);
- il sentiment negli esempi e' quello **reale** delle sorgenti live (risolve il problema GDELT vs RSS della sez. 3.1.5);
- il dataset e' direttamente riutilizzabile per il retraining del baseline e, in fase cloud, per il LoRA.

#### 5.1.6 Apprendimento sul campo: cosa si impara, e quando

Con i parametri v1 (sez. 0.4) una label si risolve solo dopo `T_in + H` (circa 10 giorni) e ci sono circa **36 finestre indipendenti per prodotto all'anno**. Quindi:
- nella **prima settimana** di dry run non si risolve nessuna label: il bot non puo' imparare l'edge dal campo in una settimana, a nessuna condizione;
- la **prova statistica dell'edge** viene dal backtest walk-forward su 3 anni di storico (sez. 3.4, G3), non dal dry run;
- il dry run impara subito tutto cio' che **non** dipende dall'esito dei trade: costi reali, tasso di fill degli ingressi maker, latenza, coerenza tra pipeline live e pipeline di training;
- il modello si aggiorna con **retraining periodico su storico esteso** (il backfill cresce ogni giorno) piu' le label live man mano che si risolvono.

Le soglie minime sono espresse in **campioni effettivi** (`n_eff` = somma dei pesi di unicita' della sez. 3.1.4), non in numero di righe: con label sovrapposte di 10 giorni, migliaia di righe valgono pochi campioni indipendenti.

`NightlyLearner` (ogni giorno alle 00:15 UTC, fuori dal percorso critico, in un processo figlio a bassa priorita'):

| Step | Frequenza | Cosa aggiorna | Condizione minima | Output |
|------|-----------|---------------|-------------------|--------|
| L4 Modello di costo | Giornaliera, da subito | `latency_ms` (p75 del RTT REST), spread mediano per prodotto e ora del giorno, tasso di fill maker entro `T_in`, fee correnti. | Sempre. | `fill_model.json` e tabella spread usata dalle label (sez. 3.1.3) |
| L0 Parita' live/training | Giornaliera, da subito | Ricostruisce con `indicators_batch` + `StateBuilder` gli stati di ieri dalle barre registrate e li confronta con quelli calcolati live; PSI di ogni feature rispetto alla distribuzione storica. Audit delle revisioni REST: riscarica le candele del giorno precedente (tutte con eta' >= 15 min all'esecuzione delle 00:15) e le confronta con lo store (minuto rivisto = presenza o OHLCV diversi); lo store non viene modificato. | Sempre. | Sezione "parity" del report; `DriftAlert` se PSI > 0.25; sezione `bars` del report (`rest_revision_ratio`) |
| L3 Refit baseline | Mensile (1° del mese) | LightGBM riaddestrato su storico esteso fino a ieri + label live risolte (peso 2), stessi split purgati. | Storico aggiornato. | Modello challenger |
| L1 Calibrazione | Con L3 | Platt per `edge`, temperature per `action`/`vol_regime`, sul fold di validation piu' recente (storico). Le label live servono a **misurare** la calibrazione, non a fittarla. | `n_eff` del fold >= 300. | `calibration.json` del challenger |
| L2 Soglie di policy | Con L3 | `tune_policy.py` sul validation piu' recente. | Come L1. | `policy.json` del challenger |
| L5 Promozione | Con L3 | Challenger -> champion se (a) batte il champion nel backtest walk-forward che include gli ultimi 90 giorni (PnL netto, Sharpe, max DD non peggiore) **e** (b) gira come book `challenger` in dry da >= 14 giorni senza anomalie operative ne' di parita'. Il confronto dei PnL delle shadow book a 14 giorni non e' un criterio: con trade da 10 giorni e' rumore. | Entrambe. | Nuova versione nel `ModelRegistry` |

Regole invarianti:
- nessun parametro cambia durante la giornata; il `DecisionEngine` ricarica gli artefatti promossi alle 00:30 UTC con uno swap atomico del riferimento e logga `model_version_changed`;
- ogni artefatto e' versionato in `models/registry/<version>/` con `manifest.json` (hash degli input, finestra dati, metriche); rollback = puntare il champion alla versione precedente;
- il learner non tocca mai i moltiplicatori `k_tp`/`k_sl` ne' l'orizzonte `H` (sez. 4.1.1): cambiarli invaliderebbe tutte le label;
- il fine-tuning LLM non avviene in questo loop (C8): le label live si accumulano per la fase cloud.

#### 5.1.7 Gate di uscita dal dry run e rampa di capitale

Il gate e' diviso in due, perche' a questo orizzonte la parte economica richiede mesi.

**G-DRY-OPS** (minimo 7 giorni; e' quello che si puo' chiudere "in una settimana"):

| Criterio | Soglia |
|----------|--------|
| Operativita' | uptime >= 99%, zero `DryRunViolation`, zero crash non gestiti, nessun kill switch scattato per bug |
| Parita' decisioni | Rieseguendo la settimana con il replay (sez. 5.3) e con la pipeline batch di training, le decisioni coincidono con quelle live in >= 99% dei minuti |
| Parita' feature | PSI < 0.10 per ogni feature dello stato rispetto alla distribuzione storica degli ultimi 90 giorni |
| Barre REST | `rest_revision_ratio <= 0.005` per prodotto e per giorno (altrimenti si applica la scala di taratura di S della sez. 2.3 A.2); zero barre fuori ordine o duplicate |
| Frequenza di BUY | Entro +-50% della frequenza attesa dal backtest sullo stesso periodo (se il periodo non ha BUY ne' nel backtest ne' live, criterio superato) |
| Costi | Fill rate maker misurato >= 50%; spread mediano entro 2x quello assunto nelle label |
| Fee floor | Quota di intent scartati per `fee_floor` < 80% |

**G-DRY-ECON** (sul book `champion`, guidato dal numero di trade, valutato in continuo; con circa 36 finestre per prodotto all'anno su 3 prodotti, 30 trade richiedono indicativamente 3–4 mesi):

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
| R0 live-micro | G-DRY-OPS **e** gate G3 della sez. 3.4 sul backtest | Minimo operativo (es. 20–50 EUR) | `min_size` della sez. 4.1.2 (circa 1–2 EUR di controvalore per trade, perdita massima a SL di pochi centesimi) | 10 trade live con parity report entro le tolleranze |
| R1 | G-DRY-ECON | Capitale iniziale scelto dall'utente | Sizing a rischio della sez. 4.1.2 | 30 trade live, PF >= 1.2, DD <= 5% |
| R2+ | R1 superato | Raddoppio per gradino | Sizing a rischio | Stesse condizioni di R1 sul gradino corrente |

R0 parte presto di proposito: il rischio e' di pochi centesimi per trade e serve a misurare fill, fee e comportamento dei bracket reali (shadow twin, sotto) mentre G-DRY-ECON matura in parallelo.

In qualunque gradino: violazione del kill switch o PF < 1.0 su 30 trade -> discesa di un gradino; due discese consecutive -> ritorno in dry.

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
    promotion: {backtest_recent_days: 90, min_shadow_days: 14}
  gate_ops:
    min_days: 7
    min_uptime: 0.99
    min_decision_parity: 0.99
    max_feature_psi: 0.10
    buy_rate_tolerance: 0.50
    min_maker_fill_rate: 0.50
    max_spread_ratio_vs_labels: 2.0
    max_fee_floor_skip_ratio: 0.80
    max_rest_revision_ratio: 0.005
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

**VirtualTrade** (chiuso, tabella `virtual_trades`):

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
                "skip_reasons": {"fee_floor": 0, "ev_negative": 3, "regime_veto": 0, "below_min_size": 0,
                                 "kill_switch": 0, "entry_pending": 41, "insufficient_balance": 0, "balance_stale": 0}, "skipped_late": 0},
  "labels": {"resolved_total": 0, "first_resolution_expected": "2026-10-27"},
  "parity": {"decision_match": 1.0, "max_feature_psi": 0.04, "worst_feature": "liquidity.vol_z_1d"},
  "bars": {"SOL-EUR": {"rest": 1402, "flat": 36, "flat_unproven": 2, "lag_s_p50": 120.3, "lag_s_p99": 126.1, "fetch_failures": 0, "late": 0, "rest_revisions": 1, "rest_revision_ratio": 0.0007, "rest_revision_max_close_bps": 1.8}},
  "costs": {"median_spread_bps": {"BTC-EUR": 3, "ETH-EUR": 2, "SOL-EUR": 3}, "fee_maker_bps": 60, "fee_taker_bps": 120,
            "fee_source": "assumed", "maker_fill_rate": 0.67, "latency_ms_p75": 280},
  "ops": {"uptime": 0.9993, "ws_reconnects": 2, "decider_fallbacks": 0, "dry_run_violations": 0},
  "gate_ops": {"days": 3, "status": "IN_PROGRESS"},
  "gate_econ": {"trades": 1, "status": "IN_PROGRESS"}
}
```

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
> 11. `modules/dryrun/learner.py` — `NightlyLearner` con gli step L0–L5 della sez. 5.1.6 e le rispettive frequenze (L0 e L4 ogni notte; L3, L1, L2, L5 il giorno `refit_day_of_month`), con soglie in `n_eff` (somma dei pesi di unicita' di `training/sampling.py`), eseguito con `asyncio.create_subprocess_exec(sys.executable, "-m", "modules.dryrun.learner", ...)` e `os.nice(10)` nel figlio dove supportato, per non rallentare il loop di trading. Ogni step controlla la propria soglia minima e, in sotto-soglia, registra `skipped: insufficient_samples` invece di produrre artefatti.
> 12. `modules/dryrun/registry.py` — `ModelRegistry` su filesystem (`models/registry/`): `register(artifacts, manifest) -> version`, `get(alias)`, `promote(version, alias="champion")`, `rollback(alias)`. Gli alias sono un file `aliases.json` scritto in modo atomico (scrittura su file temporaneo + `os.replace`). Interfaccia astratta `ModelRegistry(Protocol)` per sostituire il backend in futuro (C8).
> 13. `modules/dryrun/report.py` — job alle 00:05 UTC e CLI `python -m modules.dryrun.report --days 7 [--book champion]`: genera il JSON della sez. 5.2 e un Markdown con tabelle per book, istogramma degli skip, reliability diagram di `edge` (PNG matplotlib, solo quando ci sono label live risolte), parita' L0, costi L4, stato di G-DRY-OPS e G-DRY-ECON, data prevista della prima label risolta. Metriche: PnL netto, fee, win rate, profit factor, R medio, max drawdown, Sharpe giornaliero (indicativo, con nota sul campione), confronto con `random_entry` e `buy_and_hold`.
> 14. `modules/dryrun/parity.py` — solo in live: per ogni intent reale invia lo stesso intent allo shadow twin e produce il `ParityReport` giornaliero con le tolleranze della sez. 5.1.7.
> 15. `modules/app.py` (modifica) — `build_execution(mode)`: in `dry` istanzia `CoinbaseExchange(read_only=True)`, `ShadowBookManager` e **non** importa `LiveExecutionGateway`; in `live` istanzia `LiveExecutionGateway` + shadow twin. Il `DecisionEngine` riceve in entrambi i casi la lista dei book da servire (in live: `live` + `shadow_twin`). Banner di avvio che stampa la modalita', i book attivi e il permesso effettivo della API key; se in `live` la key non ha il permesso Trade, rifiuta di partire.
>
> Test obbligatori:
> - `test_dry_never_writes`: pipeline completa in dry su 30 minuti di replay con un `RESTClient` mock che fa fallire il test a qualunque chiamata di scrittura;
> - `test_fill_model_tick_vs_bar`: sullo stesso replay il PnL del `TickFillModel` e del `BarFillModel` stanno entro una tolleranza documentata (il bar model dev'essere piu' pessimista o uguale);
> - `test_same_event_tp_sl_is_sl`;
> - `test_gap_resolution`: disconnessione WS simulata con SL attraversato durante il gap;
> - `test_label_resolver_parity`: label live e label del dataset storico coincidono sugli stessi timestamp;
> - `test_promotion_rules`: tabella di casi per L5, incluso campione insufficiente;
> - `test_resolve_mode`: tutte le combinazioni di config ed env.
>
> Operativita' locale: `docker compose up chronos` avvia il bot in dry con il named volume `chronos-state` e i bind mount `data/` e `models/` persistenti (sez. 0.5); `docker compose run --rm chronos python -m modules.dryrun.report --days 7` mostra lo stato di G-DRY-OPS e G-DRY-ECON. Il container deve sopravvivere a un riavvio del PC (`restart: unless-stopped`) e ripristinare le posizioni virtuali dal DB dry all'avvio: una posizione virtuale aperta prima del riavvio viene risolta con la procedura del gap.

---

## Appendice A — Piano di rilascio

Perimetro: fase locale a costo zero (C8). L'infrastruttura cloud e il fine-tuning LLM su scala sono progettati a parte.

| Fase | Contenuto | Criterio di uscita |
|------|-----------|--------------------|
| P0 Fondamenta | Sez. 0.5: config pydantic, `CostModel`, backfill storico in Parquet, indicatori batch e incrementali, barre, market stream WS + `LocalOrderBook`, ledger SQLite, recorder WS. Probe rieseguito con la key View. | Criterio di uscita della sez. 0.5. |
| P1 Fattibilita', dati e baseline | `training/feasibility.py` **per primo**; poi sez. 1 (sentiment live, in raccolta) + sez. 3 parte CPU: label triple-barrier, split purgati, `BaselineDecider` LightGBM, backtest con `BarFillModel`. | Lo studio di fattibilita' trova una combinazione con lift richiesto <= 0.10 (altrimenti stop, sez. 3.1.3); backtest walk-forward con PnL netto > 0 su >= 3/5 fold e sull'holdout. |
| P2 Decision + Risk | Sez. 2 e sez. 4 completi, con `ShadowExecutionGateway` come unico gateway istanziabile. | Test e2e verdi; `test_dry_never_writes` verde. |
| P3 Dry run | Sez. 5: book paralleli, label resolver, learner notturno, report. Esecuzione continua in locale. Etichettatura del sentiment (sez. 1.1.2) a meta' della prima settimana. | G-DRY-OPS superato (minimo 7 giorni). G-DRY-ECON continua a maturare. |
| P4 Live-micro (R0) | Key con permesso Trade, `CHRONOS_LIVE_CONFIRM`, size minime, shadow twin + parity report. Prima di P4: probe esteso con `product_type`, `product_venue`, `future_product_details`, `portfolio_type`, `can_transfer` (allowlist della sez. 4.1.6). Primo ordine = validazione reale dell'ingresso post-only e del bracket standalone. | Condizioni R0 della sez. 5.1.7. |
| P5 Rampa | Gradini R1, R2, ... | R1 richiede G-DRY-ECON; poi condizioni per gradino, con discesa automatica in caso di violazioni. |
| P6 (fuori scope) | Migrazione cloud, `chronos-decider` LLM fine-tuned su GPU, che entra come book `challenger` in dry prima di qualunque promozione. | Gate sez. 3.4 + G-DRY-OPS + regola L5. |

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

## Appendice C — Riferimenti

- Jev / protocollo System One (tipi `noul`, `choice`, `score`, campi di risposta): https://openrouter.ai/blog/insights/what-is-jev/ , https://openrouter.ai/docs/guides/community/jev-tutorial
- OpenJev (server protocol-compatible, backend, porta 8080, `/v1/systemone`, `Server-Timing`, lettura dei logit): https://github.com/razorback16/openjev
- Coinbase Advanced Trade, ordini e TP/SL allegati: https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/guides/orders , https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/orders/create-order
- Coinbase Advanced Trade sandbox (statica, risposte predefinite): https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/sandbox
- Triple-barrier labeling, purged CV, sample uniqueness: M. Lopez de Prado, *Advances in Financial Machine Learning*, cap. 3, 4, 7.
- Temperature scaling: Guo et al., *On Calibration of Modern Neural Networks*, ICML 2017.
