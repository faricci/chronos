<!-- Risoluzione di architetto-opus del 2026-10-18 (fonte canonica delle barre 1m: candele REST a T+S). Le sezioni 7.x sono gia' nella spec; 5 (firme) e 6 (test) sono il riferimento per bars.py, backfill.py, ws_feed.py, e per DecisionEngine, KillSwitch, learner L0 e report ancora da scrivere. -->
> Valori numerici di market.bars e stale_bar_s superati dalla taratura del 2026-10-18 (S = 120 s): vedi sez. 2.3 A.2 della spec.
> Q3.2 (criterio 0.005 come gate) superato dalla decisione 2026-10-20 (`docs/decisions/2026-10-20-revisioni-rest-e-promozione.md`): il valore 0.005 resta come allarme diagnostico; il gate misura l'impatto delle revisioni (R1-R4).

# RISOLUZIONE ARCHITETTONICA - Fonte canonica delle barre a 1 minuto

Da: architetto-opus. Per: Sviluppatore (Sonnet). Stato: **RISOLTO**, implementabile senza altre domande.
Riguarda: `modules/market/bars.py`, `modules/market/ws_feed.py`, `modules/market/backfill.py`, `modules/config.py`,
`modules/app.py` (cablaggio), `DecisionEngine`, `KillSwitch`, `dryrun/recorder.py`, `dryrun/replay.py`,
`dryrun/label_resolver.py`, `dryrun/learner.py` (L0), `dryrun/report.py`, spec `docs/specs/chronos-system1-spec.md`.

---

## 0. Principio (perche' la scelta non e' una preferenza)

Decision Tokenization: il modello legge i logit sui token di etichetta dato lo **stato canonico serializzato**. Lo stato
servito deve essere, byte per byte, quello che la pipeline di training (`indicators_batch` sullo store) produrrebbe per
lo stesso `t`. Le feature sono interi in bps (`ret_15m_bps`, `dist_vwap_1d_bps`, ...): uno scarto di pochi bps cambia il
token. Le tue misure dimostrano che le barre dai trade WS e le candele REST differiscono nel ~50% dei minuti e che la
regola di assegnazione dell'exchange non e' ricostruibile dal campo `time`. Quindi:

**Invariante I1 (fonte unica).** Esiste una sola definizione di barra 1m nel sistema: la candela REST
`get_public_candles(granularity="ONE_MINUTE")` letta quando e' definitiva. Storico, runtime, store, label, replay e
training la usano tutti. Nessuna barra costruita dai trade entra mai nello stato, nello store o nelle label.

**Invariante I2 (store = input dell'engine).** Per ogni prodotto la sequenza di barre date all'`IndicatorEngine`
coincide con `load_bars(store)` (barre piatte ricostruite comprese). E' cio' che rende verificabile la parita'
batch/incrementale (L0, G-DRY-OPS) sul runtime reale e non solo sullo storico.

---

## 1. Decisioni per domanda

### Q1 - Fonte canonica: **B (REST a T+S)**. C respinta, A respinta.
- A respinta: skew train/serve strutturale sul livello 1m, viola I1.
- C respinta: la barra WS "provvisoria" non ha consumatori (watchdog e simulatore di bracket usano ticker e trade, lo
  stale guard usa l'eta' della barra pubblicata, il report non ne ha bisogno); il fallback WS rimetterebbe nello stato e
  nello store barre che il training non produce mai, cioe' violerebbe I1 proprio nei momenti degradati.
- Conseguenza: `BarAggregator` (costruzione da trade, dirty window, `apply_candles`, 15m/1h) **viene eliminato**. Il
  `BarService` diventa un "backfill incrementale a cadenza di un minuto" con la stessa sorgente e lo stesso parsing
  (`CandleSource` + `candles_to_frame`) del `Backfiller`.

### Q2 - Tempo di decisione e determinismo
1. `Decision.ts_bar_close = t` (fine del minuto `M`, `t = M + 1 min`). La decisione viene **calcolata** a `t + S` (lag
   reale `bar_lag_s`). **Confermato.** Le label (sez. 3.1.3) restano definite su `t`: nessun cambiamento.
2. Tutti i componenti dello stato usano l'istante logico `t`, non l'istante di calcolo:
   barre `<= M`; `clock.hour_utc`/`dow` da `t`; `SentimentService.snapshot(product, as_of=t)` (solo `ingested_at <= t`).
   Eccezioni volute, perche' descrivono l'esecuzione e non il mercato passato: `position` (vista corrente) e `costs` /
   `spread_bps` (book corrente, a `t + S`).
3. L'engine riceve **solo** barre definitive e contigue: la barra `M` entra a `>= M + 1 min + S` e mai prima.
   **Nessuna sostituzione** di una barra gia' consumata, in nessun caso (Wilder/EMA/somme mobili).
4. **Nessun fallback WS.** Se REST non risponde la barra resta in attesa (retry ogni `retry_s`); nessuna barra viene
   inventata da un fetch fallito. La contiguita' blocca in ordine anche le barre successive del prodotto.
5. Barra pubblicata con `bar_lag_s > decision_max_lag_s` (recupero dopo un'interruzione REST o all'avvio): entra
   nell'engine e nello store, **nessuna decisione e nessun DecisionRecord** (lo stato non sarebbe point-in-time per
   posizione e costi); contatore `decisions_skipped_late`. Solo le barre in tempo producono decisioni.
6. `LabelResolver` e training leggono lo store, che contiene solo candele REST: la domanda sulle barre `ws` decade.

### Q3 - Non applicabile (A respinta). Nessuna misura di skew WS/REST da implementare.
Si misura invece l'unico scarto residuo della scelta B: le **revisioni REST dopo S** (sez. 4, audit notturno).

### Q4 - Non applicabile. Con una sola sorgente e una sola regola di assegnazione non esiste giunzione: niente
`mark_dirty`, niente finestre sporche, niente riparazioni, niente doppi conteggi. Un'interruzione del WS non tocca le
barre; un'interruzione di REST ritarda le barre senza alterarle.

---

## 2. Parametri (nuova sezione `market.bars` della config)

| Chiave | Default | Vincolo (validator) | Significato |
|--------|---------|---------------------|-------------|
| `rest_delay_s` (S) | 30.0 | `5 <= S <= 120` | Un minuto `M` e' "pronto" quando `now >= M + 1 min + S`. |
| `rest_max_delay_s` | 90.0 | `>= rest_delay_s` | Oltre questo lag un minuto senza candela e senza prova di completezza diventa barra piatta `flat_unproven`. |
| `retry_s` | 5.0 | `> 0` | Intervallo di retry mentre un minuto pronto non e' ancora stato pubblicato. |
| `decision_max_lag_s` | 120.0 | `>= rest_max_delay_s + retry_s` | Oltre questo lag la barra non produce decisione. |
| `store_flush_minutes` | 15 | `>= 1` | Cadenza di scrittura nello store (Parquet), piu' flush allo shutdown. |

Nuovo in `risk.kill_switch`: `stale_bar_s: 180` (validator a livello `AppConfig`:
`stale_bar_s >= market.bars.rest_max_delay_s + 90`). `stale_data_s: 90` resta e vale **solo per il ticker**.
Motivo: con S = 30 l'eta' normale dell'ultima barra (`now - t_last`) arriva a 90 s subito prima della pubblicazione
successiva; con la vecchia soglia unica la guardia scatterebbe ogni minuto.

Nuovo in `dry_run.gate_ops`: `max_rest_revision_ratio: 0.005`.

`Backfiller` usa `market.bars.rest_delay_s` per il proprio limite superiore (sez. 5, punto 3): un solo S nel sistema.

Flag di sorgente della barra: `source: Literal["rest", "flat", "flat_unproven"]` (mai `"ws"`).

---

## 3. Flusso temporale esatto di una barra (minuto `M`, `T = M + 1 min = t`)

| Istante | Cosa accade |
|---------|-------------|
| `M .. T` | Trade WS -> `LocalOrderBook`, `quotes`, `on_trades` (simulatore di fill/bracket). Nessuna barra in costruzione. |
| `T + 2 s` | **Nulla.** La grazia di 2 s e la barra WS spariscono. |
| `T + S` | `BarService.tick()`: per ogni prodotto con minuti pronti, fetch REST di `[next_minute, floor(now) + 1 min)` (chunk da 300; include la candela del minuto in corso solo come prova). Per ogni minuto pronto `m` (`m < floor(now - S)`), in ordine: candela presente -> barra `rest`; assente e esiste una candela con `ts > m` nella risposta -> barra piatta `flat` (OHLC = close precedente, volume 0); assente senza prova -> stop (attesa). Pubblicazione ordinata per `(bar.ts, product_id)`: `store_writer.add(e)` poi `queue.put_nowait(e)`. |
| `T + S` (consumo) | `DecisionEngine`: `IndicatorEngine.update(bar)` sempre; se `e.lag_s <= decision_max_lag_s`: stato (as_of `t`), decider, policy, `DecisionRecord` con `ts_bar_close = t`, `decided_at`, `bar_source`, `bar_lag_s`. |
| `T + S + k*retry_s` | Retry se il fetch e' fallito o il minuto attende la prova. Un fetch fallito non pubblica nulla. |
| `T + rest_max_delay_s` | Se un fetch **riuscito** non mostra ne' la candela di `m` ne' una candela successiva: barra piatta `flat_unproven` (log `bar_flat_unproven`). Se i fetch falliscono ancora: nessuna barra (log `bar_rest_unavailable`), retry continuo. |
| `T + decision_max_lag_s` | Da qui in poi la barra, quando arriva, aggiorna engine e store ma non genera decisione. |
| `t_last + stale_bar_s` | Nessuna barra pubblicata da 180 s dalla chiusura dell'ultima: guardia globale `stale_data` (blocca ingressi). |

Verifiche:
- **C7.** La barra `M` usa solo la candela di `M`; la candela del minuto in corso e' usata solo come booleano ("REST ha
  elaborato oltre `m`") e non entra mai nei valori: con o senza prova la barra piatta e' identica, cambia solo
  *quando* viene pubblicata. Lo stato a `t` usa barre `<= M` e sentiment con `ingested_at <= t`. Nessun lookahead.
- **Definitive e contigue.** Emissione sequenziale da `next_minute`, un minuto alla volta, mai riemessa; `BarGapError`
  dell'engine resta come rete di sicurezza.
- **Parita' batch/incrementale.** Stesso parsing (`candles_to_frame`) in backfill e runtime; store con i soli minuti
  `rest`; barre piatte con la stessa regola di `fill_flat_bars`; quindi `load_bars(store)` == input dell'engine (I2) e
  `compute_indicators(load_bars(store))` == snapshot live riga per riga (tolleranza della sez. 2.3 A.3).
- Residuo: candela REST rivista dopo S. Non si corregge (violerebbe I2 e le ricorrenze); si misura (sez. 4).

---

## 4. Effetti su DecisionRecord, store, recorder, replay, label, report

- **DecisionRecord** (2.2.4): nuovi campi `decided_at` (ISO, wall clock del calcolo), `bar_source`
  (`rest|flat|flat_unproven`), `bar_lag_s` (float, `decided_at` non c'entra: `published_at - t`). `ts_bar_close = t`.
- **Store** (`data/bars/1m`): scritto dal `BarService` tramite `BarStoreWriter` con i soli minuti `rest` (come il
  backfill: i minuti senza scambi non si scrivono). Mai riscritto per correggere revisioni. Crash prima del flush: al
  riavvio il backfill incrementale riscarica quei minuti e il warm-up completo riallinea engine e store (I2 ristabilito).
- **Recorder** (`WsRecorder`): invariato, registra solo i messaggi WS grezzi. Le barre non sono nel recorder: sono nello
  store.
- **Replay**: le barre vengono dallo store tramite `StoreCandleSource` (stessa interfaccia `CandleSource`) con clock
  simulato; trade e book dalle registrazioni WS. Deterministico.
- **LabelResolver**: legge lo store; usa `store_complete_until_end=True` solo se
  `BarStoreWriter.complete_until(product) >= vertical`, altrimenti rimanda la risoluzione.
- **Audit revisioni** (L0 notturno, 00:15 UTC): riscarica da REST le candele del giorno precedente (5 richieste per
  prodotto), le confronta con lo store: minuto rivisto = presenza diversa o un qualunque campo OHLCV diverso
  (confronto esatto: stesso parsing). Sola lettura.
- **Report giornaliero**: nuova sezione `bars` per prodotto: `rest`, `flat`, `flat_unproven`, `lag_s_p50`, `lag_s_p99`,
  `fetch_failures`, `late` (barre oltre `decision_max_lag_s`), `rest_revisions`, `rest_revision_ratio`,
  `rest_revision_max_close_bps`. In `decisions`: `skipped_late`. Se `rest_revision_ratio > max_rest_revision_ratio`
  la correzione e' aumentare `market.bars.rest_delay_s` (config), non cambiare codice.

---

## 5. Modifiche al codice (firme)

### 5.1 `modules/market/bars.py` (riscrittura)
Eliminare: `BarAggregator`, `_Acc`, `_Product`, `_order_key`, `HIGHER_TIMEFRAMES`, `SEEN_TRADES_MAX`, la logica dirty,
l'emissione 15m/1h (l'`IndicatorEngine` aggrega gia' gli HTF con `TimeframeAggregator`: una sola aggregazione).
Spostare `Trade` in `ws_feed.py`. Mantenere `floor_minute`, `utc_now`, `MINUTE`, `REST_CHUNK_MIN`.

```python
BarSource = Literal["rest", "flat", "flat_unproven"]

@dataclass(frozen=True, slots=True)
class BarClosed:
    product_id: str
    bar: Bar                      # 1m, definitiva
    source: BarSource
    published_at: datetime        # wall clock della pubblicazione
    @property
    def t(self) -> datetime: ...  # bar.ts + MINUTE
    @property
    def lag_s(self) -> float: ... # (published_at - t).total_seconds()

class BarFinalizer:
    """Nucleo puro: niente I/O, niente clock proprio."""
    def __init__(self, products: Sequence[str], rest_delay_s: float = 30.0, rest_max_delay_s: float = 90.0) -> None
    def seed(self, product_id: str, last_bar_ts: datetime, last_close: float) -> None
    def next_minute(self, product_id: str) -> datetime                 # RuntimeError se non seedato
    def due(self, product_id: str) -> datetime                         # next_minute + MINUTE + rest_delay
    def fetch_window(self, product_id: str, now: datetime) -> tuple[datetime, datetime] | None
        # (next_minute, floor_minute(now) + MINUTE) se next_minute < floor_minute(now - S), altrimenti None
    def apply(self, product_id: str, candles: Sequence[Bar], now: datetime) -> list[BarClosed]
        # regole della tabella sez. 3; published_at = now; aggiorna next_minute e prev_close

class BarStoreWriter:
    def __init__(self, store: BarStore, complete_until: Mapping[str, datetime], flush_minutes: int = 15) -> None
    def add(self, event: BarClosed) -> None                            # bufferizza solo source == "rest"
    def due(self) -> bool
    async def flush(self) -> int                                       # to_thread(store.write); errore -> log, buffer tenuto
    def complete_until(self, product_id: str) -> datetime              # ultimo minuto (rest o flat) reso persistente

class BarService:
    def __init__(self, finalizer: BarFinalizer, queue: asyncio.Queue[BarClosed], source: CandleSource, *,
                 products: Sequence[str], store_writer: BarStoreWriter | None = None, retry_s: float = 5.0,
                 clock: Clock = utc_now, sleep: Sleep = asyncio.sleep) -> None
    async def run(self) -> None
        # loop: tick(); wake = min(finalizer.due(p)); se wake <= now -> sleep(retry_s) altrimenti sleep(wake - now)
    async def tick(self) -> list[BarClosed]
        # fetch in parallelo per prodotto (asyncio.gather di to_thread, chunk REST_CHUNK_MIN, candles_to_frame);
        # un prodotto che fallisce non blocca gli altri; publish ordinato; writer.add prima di queue.put_nowait;
        # se writer.due(): await writer.flush()

@dataclass(frozen=True, slots=True)
class RevisionStats:
    product_id: str; minutes: int; revised: int; ratio: float; max_close_diff_bps: float

def rest_revisions(product_id: str, stored: pd.DataFrame, fresh: pd.DataFrame, start: datetime, end: datetime) -> RevisionStats
    # entrambi nel formato di candles_to_frame, ristretti a [start, end)
```

### 5.2 `modules/market/backfill.py`
- `Backfiller`: nuovo campo `settle_s: float = 30.0`; `end = floor_minute(self.clock() - timedelta(seconds=self.settle_s))`
  (oggi `floor_minute(self.clock())`). `main()` passa `cfg.market.bars.rest_delay_s`.
- Nuova `StoreCandleSource(root: Path)` che implementa `CandleSource.fetch(product_id, start_s, end_s)` leggendo le righe
  reali dello store in `[start_s, end_s)` nel formato API (`{"start": str(sec), "open": ..., ...}`). Per replay e test.
- Docstring: "same rule the live `BarService` uses".

### 5.3 `modules/market/ws_feed.py`
```python
class CoinbaseMarketStream:
    def __init__(self, products: Sequence[str], transport_factory: TransportFactory = CoinbaseTransport, *,
                 on_trades: Callable[[Sequence[Trade]], None] | None = None,
                 on_damage: Callable[[datetime, datetime | None], None] | None = None,
                 clock: Clock = utc_now, raw_tap: Callable[[float, str], None] | None = None,
                 silence_s: float = SILENCE_S) -> None
```
- `Trade` definito qui. Deduplicazione per `trade_id` spostata qui (set + deque, `SEEN_TRADES_MAX = 20_000`):
  `on_trades` riceve solo trade mai visti; `stats.trades` conta quelli inoltrati.
- `_close_damage(last_good, now)` -> `on_damage(last_good, now)`; `_enter_trouble` -> `on_damage(last_message_at, None)`.
  Consumatore futuro: `BracketSimulator` (gap, sez. 5.1.3).
- Eliminare: parametro `aggregator`, `_catch_up_once`, `_caught_up`, `feed_alive_at`, import da `bars`.

### 5.4 `modules/config.py`
- `BarsConfig(_Section)` con i campi della sez. 2 e i validator; `MarketConfig.bars: BarsConfig = BarsConfig()`.
- `KillSwitchConfig.stale_bar_s: float = Field(default=180, gt=0)`.
- `GateOpsConfig.max_rest_revision_ratio: float = Field(default=0.005, gt=0, le=1)`.
- `AppConfig` `model_validator(mode="after")`: `risk.kill_switch.stale_bar_s >= market.bars.rest_max_delay_s + 90`.

### 5.5 Avvio (`modules/app.py`, quando lo scrivi)
1. `Backfiller(settle_s=S)` incrementale per tutti i prodotti.
2. Per prodotto: `frame = load_bars(root, pid)`; `engine.warm_up(frame)`;
   `finalizer.seed(pid, frame.index[-1], frame["close"].iloc[-1])`; `complete_until[pid] = frame.index[-1]`.
3. Avvio di `CoinbaseMarketStream`, `BarService.run`, `DecisionEngine`, ecc. Il primo `tick` recupera i minuti fra
   l'ultima barra e `now - S` (pubblicati in ritardo: nessuna decisione salvo l'ultima se entro `decision_max_lag_s`).
4. SIGTERM: `await store_writer.flush()` prima della chiusura.

### 5.6 Componenti non ancora scritti (vincoli da rispettare quando li scrivi)
- `DecisionEngine`: regole Q2.2 e Q2.5; campi del DecisionRecord della sez. 4.
- `KillSwitch`: bar staleness = `now - t_last > stale_bar_s`, con `t_last` = `t` dell'ultima `BarClosed` pubblicata
  (qualunque prodotto in ritardo fa scattare la guardia globale); ticker con `stale_data_s`.
- `learner` L0: chiama `rest_revisions` per ogni prodotto sul giorno precedente.
- `report`: sezione `bars` e `decisions.skipped_late`.

---

## 6. Test da aggiungere / riscrivere

`tests/test_bars.py` (riscrittura completa, `FakeCandleSource` + clock e sleep finti):
1. `test_not_before_t_plus_s`: a `T + S - 1 ms` nessuna barra; a `T + S` barra `rest` con OHLCV identici alla candela.
2. `test_contiguous_catch_up`: 700 minuti arretrati -> 3 chunk, barre contigue in ordine, ognuna una sola volta.
3. `test_flat_with_proof`: candela di `m` assente, candela di `m+1` presente -> `flat`, OHLC = close precedente, volume 0.
4. `test_flat_unproven_waits`: nessuna candela e nessuna prova -> nulla fino a `T + rest_max_delay_s`, poi `flat_unproven`.
5. `test_fetch_error_invents_nothing`: fetch che fallisce oltre `rest_max_delay_s` -> nessuna barra; al ritorno di REST
   le barre arrivano corrette e contigue.
6. `test_product_isolation`: un prodotto in errore non ritarda gli altri.
7. `test_in_progress_candle_is_proof_only` (C7): candela del minuto in corso con valori assurdi non compare in nessuna
   barra ne' cambia gli indicatori.
8. `test_published_at_and_lag`.
9. `test_unseeded_product_raises`.
10. `test_run_schedule`: sonno fino a `due`; `retry_s` quando un minuto pronto resta pendente.
11. `test_store_roundtrip_invariant` (I2): barre emesse (con piatte, `flat_unproven` e catch-up) -> `BarStoreWriter` ->
    `load_bars(store, store_complete_until_end=True)` uguale alla sequenza emessa, confronto esatto.
12. `test_engine_parity_on_runtime_bars`: `IndicatorEngine` alimentato dalle barre emesse == `compute_indicators(load_bars(store))`
    su ogni riga (tolleranza sez. 2.3 A.3, maschere NaN identiche), >= 5.000 barre da fixture.
13. `test_writer_flush_and_complete_until`: cadenza, flat che avanzano `complete_until`, errore di scrittura che tiene il buffer.
14. `test_rest_revisions`: valore cambiato, candela comparsa su un minuto piatto, candela sparita -> conteggi corretti.

`tests/test_backfill.py`: `test_end_respects_settle_s` (minuto con `ts + 1 min + S > now` non scaricato);
`test_store_candle_source` (round trip esatto con `candles_to_frame`).

`tests/test_ws_feed.py`: rimuovere i test su aggregatore/dirty/catch-up; aggiungere `test_on_trades_dedup`
(snapshot di riconnessione non reinoltra trade gia' visti) e `test_on_damage_gap_and_silence` (gap -> `(last_good, now)`;
silenzio -> `(last_message_at, None)` poi chiusura alla ripresa).

`tests/test_config.py`: default di `market.bars`; validator `rest_max_delay_s >= rest_delay_s`,
`decision_max_lag_s >= rest_max_delay_s + retry_s`, `stale_bar_s >= rest_max_delay_s + 90`.

Da scrivere con i rispettivi moduli: `DecisionEngine` (barra oltre `decision_max_lag_s` -> engine aggiornato, nessun record;
`ts_bar_close == bar.ts + 1 min`; sentiment chiamato con `as_of == ts_bar_close`), `KillSwitch` (nessuno scatto con lag 90 s,
scatto oltre 180 s).

---

## 7. Testo da inserire nella spec (`docs/specs/chronos-system1-spec.md`)

Aggiornare l'intestazione: `Versione: 2.6-draft`.

**7.1 Changelog** - nuova riga dopo "Changelog 2.5 (...)" (riga 37):
> Changelog 2.6 (fonte canonica delle barre 1m): le barre 1m del runtime sono le candele REST `get_public_candles` lette `S` secondi dopo la chiusura del minuto (`BarService`, sez. 2.3 A.2), la stessa fonte dello storico; i trade WebSocket non costruiscono piu' barre (`BarAggregator` eliminato); la decisione si calcola a `t + S` con `ts_bar_close = t`; nuova sezione `market.bars` della config; guardia stale separata per barre (`stale_bar_s`) e ticker; audit notturno delle revisioni REST (L0) e criterio G-DRY-OPS relativo; replay delle barre dallo store (`StoreCandleSource`).

**7.2 Sez. 0.2** - sostituire la riga del diagramma che contiene `MarketStream -> BarAggregator(1m) -> IndicatorEngine(1m,1h,6h,1d)`
con le due righe seguenti (allineare il `|` finale al bordo del riquadro):
```
 Coinbase WS ---------> MarketStream -> LocalOrderBook, trade (spread, simulatori)
 Coinbase REST -------> BarService(1m, T+S) ------------> IndicatorEngine(1m,1h,6h,1d)
```

**7.3 Sez. 0.5, Backfill storico** - (a) nel quarto punto sostituire "con la stessa regola del `BarAggregator` live" con
"con la stessa regola del `BarService` live (sez. 2.3 A.2)"; (b) nuovo punto dopo il primo ("`RESTClient().get_public_candles` ..."):
> - limite superiore esclusivo `floor_minute(now - market.bars.rest_delay_s)`: un minuto viene scaricato solo quando `ts + 1 min + S <= now`, cioe' quando la candela REST e' definitiva (stessa regola del `BarService`). Storico e runtime leggono la stessa versione della candela;

(c) nuovo paragrafo subito prima di "Ordine di implementazione P0":
> **Invarianti delle barre 1m.** (I1) Esiste una sola definizione di barra 1m: la candela REST `ONE_MINUTE` definitiva. Storico, runtime, store, label, replay e training usano solo questa; nessuna barra costruita dai trade WebSocket entra nello stato, nello store o nelle label (le due serie differiscono nell'assegnazione dei trade ai minuti, misurato il 2026-10-17, e lo scarto cambierebbe i token dello stato). (I2) Per ogni prodotto la sequenza di barre consumate dall'`IndicatorEngine` coincide con `load_bars(store)`: lo store e' la registrazione di cio' che il runtime ha consumato e non viene mai riscritto per correggere revisioni successive dell'exchange, che sono solo misurate (sez. 5.1.6, L0).

(d) Criterio di uscita P0, aggiungere in fondo: "; 48 h di `BarService` con `rest_revision_ratio <= 0.005` per prodotto."

**7.4 Sez. 2.1, flusso per barra** - nel blocco di codice sostituire la prima riga con
`bar_closed(product, t)                             # pubblicata dal BarService a t + S (sez. 2.3 A.2)`
e aggiungere dopo il blocco:
> **Istante logico e istante di calcolo.** `t` = chiusura del minuto (`ts_bar_close`); la decisione si calcola a `t + S` (`market.bars.rest_delay_s`, default 30 s), quando la candela REST del minuto e' definitiva. Tutto lo stato usa l'istante logico `t`: barre fino al minuto che chiude in `t`, `clock` da `t`, sentiment con `as_of = t` (`ingested_at <= t`). Fanno eccezione, perche' descrivono l'esecuzione, `position` e `costs`/`spread_bps`, letti al momento del calcolo. Con orizzonte di giorni e ingresso maker con timeout di 30 min il ritardo `S` e' irrilevante; il prezzo dell'ordine viene comunque dal `LocalOrderBook`. Una barra pubblicata con ritardo oltre `market.bars.decision_max_lag_s` aggiorna indicatori e store ma non produce decisione ne' `DecisionRecord` (contatore `decisions.skipped_late`).

**7.5 Sez. 2.2.1.b, Convenzioni** - nuovo punto dopo il primo ("Riga `i` = ..."):
> - Sorgente: le barre 1m sono sempre candele REST definitive (invariante I1, sez. 0.5), in batch e a runtime. La riga `i` entra nell'`IndicatorEngine` a `t_i + S`, una sola volta, in ordine e senza buchi; una riga gia' consumata non viene mai sostituita.

**7.6 Sez. 2.2.4, DecisionRecord** - nel JSON, dopo `"ts_bar_close": ...`, aggiungere:
`"decided_at": "2026-10-17T14:03:30.412Z", "bar_source": "rest", "bar_lag_s": 30.4,`
e dopo il blocco:
> `bar_source` in `rest | flat | flat_unproven` (sez. 2.3 A.2); `bar_lag_s = published_at - ts_bar_close` della barra; `decided_at` = istante di calcolo.

**7.7 Sez. 2.3 A.1** - sostituire da "rileva gap tramite `sequence_num`" fino a "si costruiscono dai trade." con:
> rileva gap tramite `sequence_num`; ogni intervallo di dati danneggiato (gap, reset, silenzio) viene notificato con `on_damage(start, end | None)` (consumatore: `BracketSimulator`, sez. 5.1.3). I trade, deduplicati per `trade_id` (il canale rimanda uno snapshot a ogni riconnessione), vanno a `on_trades` (simulatori di fill e bracket). Il feed WS non costruisce barre e non e' mai la fonte delle feature (invariante I1, sez. 0.5). Non usare il canale `candles` (granularita' 5 min).

**7.8 Sez. 2.3 A.2** - sostituire l'intero punto con:
> 2. `bars.py` — `BarService` (shell asyncio) + `BarFinalizer` (nucleo puro, senza I/O ne' clock) + `BarStoreWriter`. Le barre 1m sono le candele REST `get_public_candles(granularity="ONE_MINUTE")` (`CandleSource` e `candles_to_frame` di `backfill.py`, stesso parsing dello storico). Il minuto `M` e' pronto quando `now >= M + 1 min + S`. A ogni risveglio (`M + 1 min + S`, oppure `retry_s` se un minuto pronto e' ancora pendente) il servizio scarica per ogni prodotto `[next_minute, floor(now) + 1 min)` (chunk da 300) e, per ogni minuto pronto in ordine: candela presente -> barra `rest`; candela assente ed esiste nella risposta una candela successiva -> barra piatta `flat` (OHLC = close precedente, volume 0, stessa regola di `load_bars`); candela assente senza candele successive -> attesa, e oltre `rest_max_delay_s` barra piatta `flat_unproven` (solo con un fetch riuscito). Un fetch fallito non pubblica nulla e non inventa barre: retry ogni `retry_s`, senza limite. La candela del minuto in corso serve solo come prova di completezza, mai come dato (C7). Le barre sono pubblicate una sola volta, contigue, ordinate per `(ts, product)`, come `BarClosed(product_id, bar, source, published_at)`; il `BarStoreWriter` scrive nello store le sole barre `rest` ogni `store_flush_minutes` e allo shutdown, ed espone `complete_until(product)`. Nessuna aggregazione 15m/1h qui: gli HTF li aggrega solo l'`IndicatorEngine`. Config:
> ```yaml
> market:
>   bars:
>     rest_delay_s: 30          # S; la candela REST e' risultata definitiva entro ~25 s (misura del 2026-10-17)
>     rest_max_delay_s: 90      # oltre: minuto senza candela e senza prova -> flat_unproven
>     retry_s: 5
>     decision_max_lag_s: 120   # oltre: barra consumata, nessuna decisione
>     store_flush_minutes: 15
> ```

**7.9 Sez. 2.3 A.4** - aggiungere in fondo:
> Dopo il warm-up il `BarFinalizer` viene inizializzato (`seed`) con l'ultima riga del frame del warm-up; il primo ciclo del `BarService` recupera da REST i minuti tra quella riga e `now - S`.

**7.10 Sez. 2.3 C.5** - aggiungere dopo "consuma `BarClosed`,":
> aggiorna sempre l'`IndicatorEngine` con la barra; se `bar_lag_s <= market.bars.decision_max_lag_s` costruisce lo stato con istante logico `ts_bar_close = bar.ts + 1 min` (sentiment con `as_of = ts_bar_close`, sez. 2.1) e registra `decided_at`, `bar_source`, `bar_lag_s` nel `DecisionRecord`; altrimenti non decide e incrementa `decisions_skipped_late`;

**7.11 Sez. 3.1.3** - nuovo paragrafo dopo quello che inizia con "La scansione e la stima dei costi":
> `t` e' l'istante logico di chiusura della barra (`ts_bar_close`), non l'istante di calcolo `t + S` (sez. 2.1): le label non dipendono da `S`. Lo scarto tra l'ingresso simulato a barre e l'ingresso reale a `t + S` rientra nella parita' misurata dal dry run (sez. 5.1.7).

**7.12 Sez. 4.1.4** - sostituire la riga "Stale data" con:
> | Stale data | Globale | Ultima barra 1m pubblicata con `now - ts_bar_close > stale_bar_s` (default 180 s, >= `rest_max_delay_s` + 90), oppure ultimo ticker piu' vecchio di `stale_data_s` (90 s) | Nessun ingresso in nessun book; i bracket esistenti restano. |

e nella config YAML della sez. 4.2, sotto `stale_data_s: 90`, aggiungere `stale_bar_s: 180                # barre 1m (pubblicate a t + S)` e
cambiare il commento implicito di `stale_data_s` in `# solo ticker`.

**7.13 Sez. 5.1.6, riga L0** - aggiungere in coda alla colonna "Cosa aggiorna":
> Audit delle revisioni REST: riscarica le candele del giorno precedente e le confronta con lo store (minuto rivisto = presenza o OHLCV diversi); lo store non viene modificato.

e in coda alla colonna "Output": "; sezione `bars` del report (`rest_revision_ratio`)".

**7.14 Sez. 5.1.7, G-DRY-OPS** - nuova riga dopo "Parita' feature":
> | Barre REST | `rest_revision_ratio <= 0.005` per prodotto e per giorno (altrimenti aumentare `market.bars.rest_delay_s`); zero barre fuori ordine o duplicate |

**7.15 Sez. 5.2** - in `gate_ops` aggiungere `max_rest_revision_ratio: 0.005`; nel JSON del report aggiungere dopo `"parity"`:
`"bars": {"SOL-EUR": {"rest": 1402, "flat": 36, "flat_unproven": 2, "lag_s_p50": 30.3, "lag_s_p99": 36.1, "fetch_failures": 0, "late": 0, "rest_revisions": 1, "rest_revision_ratio": 0.0007, "rest_revision_max_close_bps": 1.8}},`
e in `"decisions"` aggiungere `"skipped_late": 0`.

**7.16 Sez. 5.3 punto 9** - aggiungere dopo "(what-if a costo zero).":
> Le barre 1m non sono nelle registrazioni WS: in replay il `BarService` usa `StoreCandleSource` (store Parquet, stessa interfaccia `CandleSource`) con lo stesso clock simulato.

**7.17 Sez. 5.3 punto 10** - sostituire "(alimentato dal `BarAggregator` live e dal backfill incrementale)" con
"(alimentato dal `BarService` e dal backfill incrementale, sez. 2.3 A.2; `store_complete_until_end=True` solo se `BarStoreWriter.complete_until(product) >= vertical`, altrimenti la risoluzione viene rimandata)".

---

Nessun punto aperto. Dopo l'implementazione: cancellare questo file (protocollo CLAUDE.md, punto 5).

---

# Taratura di S (2026-10-18)

## 0. Lettura delle misure

- La misura che conta e' la sonda lunga (70 candele seguite da prima della chiusura per >= 20 min): ultimo cambiamento
  max 92 s, 0/70 dopo 120 s, 0/70 fra 120 s e 1200 s. Lo store del backfill (eta' >= 1 h) ha 0 revisioni su ~36.000 minuti:
  la candela vecchia e' stabile, quindi lo storico e' "la versione definitiva" e il runtime deve leggere quella.
- 0 su 70 **non dimostra** un tasso < 0.5%: il limite superiore al 95% di 0/70 e' circa 3/70 = 4.3%. La sonda serve a scegliere
  il punto di partenza; l'accettazione la da' solo il soak (~2.880 barre per prodotto in 48 h, dove 0.5% = ~14 revisioni).
- Il costo di S e' asimmetrico: ogni secondo di S in piu' costa quasi nulla (orizzonte di giorni), ogni revisione dopo S e'
  skew train/serve (viola I1) e fa fallire il gate. Quindi S va scelto con margine e alzato solo se il soak lo smentisce.
  Non si scende mai sotto il valore validato: abbassare S non porta benefici misurabili.

---

## 1. Decisioni

### Q1 - Valori di default: **confermato S = 120 s**, con valori derivati da formula e validator allargato
- `rest_delay_s = 120`: margine di 28 s sul massimo osservato (92 s) e primo valore con 0/70 ancora in cambiamento.
  S = 90 (1.4%) e S = 60 (5.7%) sono respinti: superano gia' sul campione il criterio di 0.5%.
- I tre parametri dipendenti si ottengono da S con formule fisse, cosi' una futura taratura cambia un numero solo:
  - `rest_max_delay_s = S + 60` (un minuto in piu' per la prova di completezza di un minuto senza candela);
  - `decision_max_lag_s = S + 120` (>= `rest_max_delay_s + retry_s` con margine per i retry);
  - `stale_bar_s = S + 150` (eta' massima normale dell'ultima barra = `60 + rest_max_delay_s`, piu' 30 s).
  Le formule sono la regola per scegliere i default; il codice applica solo i validator (sotto), non calcola i valori.
- Validator di S: **`60 <= S <= 300`**. Il minimo 60 vieta valori gia' smentiti dalle misure (5.7% a 60 s, peggio sotto);
  il massimo 300 lascia spazio alla scala di taratura di Q3 tenendo il ritardo di decisione entro il 17% della finestra di
  ingresso maker (30 min).
- Nuovo validator di coerenza: `decision_max_lag_s < stale_bar_s` (una decisione si calcola solo su barre che la guardia
  non considera vecchie; altrimenti si calcolerebbero decisioni con gli ingressi gia' bloccati).
- `Backfiller.settle_s` non ha chiave propria: vale sempre `market.bars.rest_delay_s` (un solo S nel sistema).

### Q2 - Effetti del ritardo di 120 s
1. **Irrilevante, confermato, con i numeri.** 120 s sono lo 0.014% dell'orizzonte H (14.400 min). La finestra di fill maker
   reale `[t+S, t+S+T_in]` si sovrappone al 93% a quella della label `(t, t+T_in]`. Il limite reale (best_bid a `t+S`) e
   quello della label (`close[t]` meno meta' spread) differiscono per la deriva del prezzo in 2 min: media zero, ampiezza
   tipica ~5-11 bps (ATR 1h di 30-61 bps scalato a 2 min), contro barriere di `4 x ATR_1d` (~1.000-1.800 bps). Lo stato
   resta point-in-time su `t`; `position`, `costs`, `spread_bps` e il prezzo dell'ordine sono letti all'invio, com'e' giusto.
2. **`max_median_entry_delta_bps` (10) non cambia.** Confronta l'ordine reale con il suo shadow twin per lo **stesso intent**,
   entrambi creati dopo `t+S`: S non entra nel confronto. Lo scarto fra ingresso della label e ingresso reale non ha una
   tolleranza propria e non ne serve una nuova: e' gia' coperto dal fill rate maker misurato (G-DRY-OPS, >= 50%, step L4) e
   dalla frequenza di BUY rispetto al backtest.
3. **`stale_bar_s = 270` accettato.** Sui prezzi d'esecuzione la guardia veloce resta quella del ticker (`stale_data_s = 90`,
   invariata). Le decisioni su barre in ritardo sono gia' escluse per prodotto da `decision_max_lag_s = 240`. `stale_bar_s`
   e' la guardia globale "lo stream REST di un prodotto si e' fermato": rilevarlo in 270 s invece di 180 s non cambia il
   rischio, perche' le posizioni aperte restano protette dai bracket lato exchange e dal watchdog sul ticker.

### Q3 - Regola se il soak dice `rest_revision_ratio > 0.005`: **proposta accettata, con due precisazioni**
1. **Prima classificare le revisioni** (ispezione dei due frame passati a `rest_revisions`, nessun codice nuovo):
   - `valore`: minuto `rest` con OHLCV diverso -> problema di assestamento, alzare S aiuta;
   - `comparsa`: minuto pubblicato `flat`/`flat_unproven` per cui ora esiste una candela, oppure `scomparsa`: candela
     pubblicata che ora non esiste -> problema di completezza/consistenza dell'endpoint, alzare S **non** aiuta: aprire
     subito un nuovo CONFLITTO con i dati.
2. **Scala di taratura** (solo se prevalgono le revisioni `valore`), un gradino per volta, ogni gradino con un nuovo soak
   completo di 48 h:

   | Gradino | `rest_delay_s` | `rest_max_delay_s` | `decision_max_lag_s` | `stale_bar_s` |
   |---------|----------------|--------------------|----------------------|---------------|
   | 0 (default) | 120 | 180 | 240 | 270 |
   | 1 | 180 | 240 | 300 | 330 |
   | 2 | 300 | 360 | 420 | 450 |

   Se il gradino 2 fallisce ancora: nuovo CONFLITTO con i dati. **Il criterio 0.005 non si cambia** in nessun caso.
   Il gradino validato diventa il default nella config, nella spec e in `config.example.yml` (se esiste).
3. Regola di misura (per il soak e per L0): si rileggono solo minuti con eta' >= 15 min al momento della rilettura (le
   misure non mostrano cambiamenti fra 120 s e 1.200 s, e nessuno sopra 1 h). L0 alle 00:15 sul giorno D-1 la soddisfa per
   costruzione; il soak si rilegge almeno 15 min dopo la fine.

---

## 2. Tabella finale (config e validator)

| Chiave | Default | Validator | Dove |
|--------|---------|-----------|------|
| `market.bars.rest_delay_s` (S) | **120.0** | `Field(ge=60, le=300)` | `BarsConfig` |
| `market.bars.rest_max_delay_s` | **180.0** | `>= rest_delay_s` | `BarsConfig` model_validator (invariato) |
| `market.bars.retry_s` | 5.0 | `> 0` | `BarsConfig` (invariato) |
| `market.bars.decision_max_lag_s` | **240.0** | `>= rest_max_delay_s + retry_s` | `BarsConfig` model_validator (invariato) |
| `market.bars.store_flush_minutes` | 15 | `>= 1` | `BarsConfig` (invariato) |
| `risk.kill_switch.stale_bar_s` | **270.0** | `>= market.bars.rest_max_delay_s + 90` **e** `> market.bars.decision_max_lag_s` | `AppConfig` model_validator (la seconda condizione e' nuova) |
| `risk.kill_switch.stale_data_s` | 90 | `> 0` | invariato, solo ticker |
| `dry_run.gate_ops.max_rest_revision_ratio` | 0.005 | `(0, 1]` | invariato |
| `Backfiller.settle_s` | = `market.bars.rest_delay_s` | nessuna chiave propria | `backfill.main()` e `app.py` lo passano dalla config |

Se `BarFinalizer.__init__` o `Backfiller` hanno default numerici propri (30.0 / 90.0), portarli a 120.0 / 180.0 per non avere
due verita'; in produzione arrivano sempre dalla config.

---
