# Chronos v2 — Specifica Tecnica "System 1" Decision Architecture

Versione: 2.1-draft | Data: 2026-10-17 | Stato: proposta per implementazione
Target implementatore: Sonnet 5.5 (ogni blocco "Linee Guida" e' scritto per essere usato come prompt diretto)

Changelog 2.1: perimetro infrastrutturale a costo zero (locale, CPU-only, AWS fuori scope); nuova sez. 5 "Dry Run"; piano di rilascio riscritto; la sandbox Coinbase e' statica e non viene piu' usata per validare i payload.

---

## 0. Vincoli trasversali (leggere prima di ogni sezione)

Questi vincoli valgono per tutte le sezioni. Sono il motivo di molte scelte che seguono.

| ID | Vincolo | Conseguenza architetturale |
|----|---------|----------------------------|
| C1 | Coinbase Advanced Trade spot: **no short selling**. | `SELL` significa sempre "chiudi/riduci un long esistente". Le azioni valide dipendono dallo stato della posizione. |
| C2 | Le fee retail di Coinbase (taker tipicamente 0.6–1.2%, maker inferiore, dipende dal tier) superano di un ordine di grandezza l'ATR a 1 minuto (tipicamente 0.05–0.3%). | La **cadenza decisionale** resta 1 minuto, ma l'**orizzonte di holding e le barriere** devono essere su scala multi-ora (ATR su barre 15m/1h). Ogni soglia di TP deve superare il costo round-trip. Il fee tier reale va letto da `get_transaction_summary`, mai hardcoded. |
| C3 | La latenza in millisecondi del modello riduce il ritardo decisione→ordine, ma lo slippage dominante deriva da spread e tipo d'ordine. | L'esecuzione usa limit marketable con timeout, non market puro, e misura lo slippage realizzato. |
| C4 | OpenJev e' un'implementazione **protocol-compatible** di Jev, non il modello Jev ufficiale. Calibrazione e velocita' non sono verificate indipendentemente (dichiarato dal progetto stesso). Non esiste un modello chiamato "OpenJev Flash 9B": l'equivalente piu' vicino e' il backend `Clef-flash 1.0` (base Qwen3.5-9B). | Il **protocollo** `/v1/systemone` e' il contratto stabile; il backend e' intercambiabile (OpenJev zero-shot in fase 0, server `chronos-decider` con LoRA fine-tuned in fase 1+). |
| C5 | L'host attuale e' Windows 11 senza GPU NVIDIA. Un modello 9B a latenza di ms richiede GPU NVIDIA (>=24 GB VRAM; NVFP4 di Clef richiede Blackwell). | Il container `chronos` e' CPU-only. Nella fase locale il backend decisionale di default e' `BaselineDecider` (LightGBM, CPU). Il container `decider` LLM e' definito ma attivo solo con il profilo compose `gpu`. |
| C8 | **Costo zero nella fase attuale.** L'infrastruttura AWS e' fuori scope di questa specifica e verra' progettata separatamente. | Tutto gira in Docker sulla macchina locale. Nessun servizio a pagamento (niente API LLM, niente GPU cloud, niente feed news a pagamento). Il codice resta **cloud-ready** senza dipendere da AWS: configurazione da env, segreti solo da env, container stateless salvo i volumi `data/` e `models/`, storage dietro interfacce (`NewsStore`, `PositionLedger`, `ModelRegistry`) cosi' che SQLite/filesystem possano essere sostituiti in seguito senza toccare la logica. Vietato importare `boto3` o SDK cloud in questa fase. |
| C9 | **Dry mode di default.** Il bot deve poter girare in produzione senza inviare ordini reali, simulando gli esiti sul flusso di mercato reale. | `execution.mode: dry` e' il default. Il passaggio a `live` richiede una conferma esplicita (sez. 5). Pipeline unica: in dry cambia solo l'implementazione iniettata dell'`ExecutionGateway`. |
| C10 | **Micro-capitale.** Il live parte con ordini minimi e cresce solo dopo evidenza netta di fee. | Le size sono vincolate da `base_min_size`/`quote_min_size` del prodotto (lette dall'API, mai hardcoded). Rampa di capitale a gradini con gate (sez. 5.1.7). Le fee sono percentuali, quindi gli ordini piccoli non peggiorano il costo in %, ma al tier di volume piu' basso le fee sono le piu' alte: ogni trade deve superare il fee floor della sez. 4.1. |
| C6 | Nessuna fonte news "webhook" e' sia affidabile sia gratuita. | Il real-time news e' implementato come polling RSS con GET condizionale (ETag/Last-Modified) a 30–60 s, piu' sorgenti opzionali con token. Latenza effettiva 1–5 min: accettabile perche' l'orizzonte e' multi-ora (C2). |
| C7 | Point-in-time correctness. | Ogni feature usata in training deve essere ricostruibile con soli dati noti al tempo `t`. Per le news vale `ingested_at`, non `published_at`. |

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
 Coinbase WS ---------> MarketStream -> BarAggregator(1m) -> IndicatorEngine(1m,15m,1h)   |
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
3. **Asset tagging** — regex word-boundary su alias configurati per prodotto (`BTC: [bitcoin, btc]`, `XLM: [stellar, xlm]`). Le news senza asset ma con keyword macro (`sec, etf, fed, regulation, hack, exchange`) sono taggate `MARKET`. Le news non pertinenti vengono scartate prima della classificazione (risparmio CPU).
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

**Calibrazione obbligatoria:** temperature scaling (un singolo scalare `T` ottimizzato per NLL) su un set etichettato di 1500–3000 titoli crypto (etichettatura LLM-assisted + revisione umana, split 70/30). Accettazione: ECE (15 bin) <= 0.05 sul test. `T` e' salvata nel `sentiment_model_card.json` e applicata ai logit prima del softmax.

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
Default: `half_life = 90 min` per asset, `240 min` per `MARKET`.

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
  "assets": ["XLM"],
  "is_market_wide": false
}
```

**SentimentEvent** (output del classificatore, persistito su SQLite tabella `sentiment_events`):

```json
{
  "news_id": "c0f1a9e2b7d4...",
  "asset_key": "XLM",
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
  "asset": {"key": "XLM", "ewm": 0.38, "mass": 2.71, "disp": 0.22, "shock_15m": 0.30,
            "n_1h": 3, "n_24h": 11, "last_choice": 2, "last_conf": 0.41, "last_age_min": 3},
  "market": {"ewm": -0.05, "mass": 6.10, "disp": 0.41, "shock_15m": 0.12, "n_1h": 9},
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
    shock_window_min: 15
  dedup_window_hours: 48
  assets:
    BTC: ["bitcoin", "btc"]
    ETH: ["ethereum", "eth", "ether"]
    XLM: ["stellar", "xlm"]
  market_keywords: ["sec", "etf", "fed", "regulation", "hack", "exchange", "stablecoin"]
  sources:
    - {name: coindesk_rss, kind: rss, url: "https://www.coindesk.com/arc/outboundfeeds/rss/", interval_s: 45}
    - {name: cointelegraph_rss, kind: rss, url: "https://cointelegraph.com/rss", interval_s: 45}
    - {name: decrypt_rss, kind: rss, url: "https://decrypt.co/feed", interval_s: 60}
    - {name: cryptoslate_rss, kind: rss, url: "https://cryptoslate.com/feed/", interval_s: 60}
    - {name: cryptopanic, kind: cryptopanic, interval_s: 120, token_env: "CRYPTOPANIC_TOKEN", enabled_if_token: true}
    - {name: fear_greed, kind: fear_greed, url: "https://api.alternative.me/fng/", interval_s: 3600}
```

**SQLite DDL** (`data/chronos.db`):

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
> 6. `modules/sentiment/calibration.py` — script CLI `python -m modules.sentiment.calibration --labels data/labels/headlines.csv`: ottimizza `T` con `scipy.optimize.minimize_scalar` su NLL (bounds 0.05–10), calcola ECE a 15 bin e Brier prima e dopo, scrive il model card. Fallisce con exit code 1 se ECE dopo > 0.05.
> 7. `modules/sentiment/aggregator.py` — `SentimentAggregator`: per chiave mantiene una `collections.deque` di eventi delle ultime 24 h; `snapshot(as_of: datetime) -> SentimentSnapshot` calcola `ewm, mass, disp, shock_15m, n_1h, n_24h` con le formule della sez. 1.1.3 usando **solo eventi con `ingested_at <= as_of`**. Funzione pura rispetto allo stato: stesso stato e stesso `as_of` danno lo stesso output. All'avvio ricarica le ultime 24 h da SQLite.
> 8. `modules/sentiment/store.py` — `NewsStore` su `aiosqlite`, DDL della sez. 1.2, `INSERT OR IGNORE`, WAL mode (`PRAGMA journal_mode=WAL`).
> 9. `modules/sentiment/service.py` — `SentimentService` che possiede le queue e i task: `async def run(self) -> None` e `def snapshot(self, product_id: str, as_of: datetime) -> SentimentSnapshot` (mappa `XLM-EUR` -> chiave `XLM`).
>
> Test (pytest + pytest-asyncio + respx):
> - `RssSource` con fixture XML locali, incluso il caso 304;
> - dedup con titoli che differiscono per punteggiatura/maiuscole;
> - tagger: `"Stellar"` matcha XLM, `"stellarator"` no;
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
| `vol_regime` | `score` [very_low, low, normal, high, extreme] | Regime di volatilita' realizzata **futura** sull'orizzonte H. | Moltiplicatore di sizing e veto (sez. 4). |

Flusso per ogni prodotto, ogni chiusura di barra da 1 minuto:

```
bar_closed(product, t)
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
2. **Soglie** (da `DecisionPolicyConfig`, tarate sul validation set, mai a mano):
   - `BUY` se `p(BUY) >= tau_buy` **e** `action.confidence >= c_min` **e** `edge.noul >= p_edge_min` **e** `vol_regime.score` nell'intervallo permesso;
   - `SELL` (solo se LONG) se `p(SELL) >= tau_sell`;
   - altrimenti `HOLD`.
3. **Coerenza**: se `action=BUY` ma `edge.noul < p_edge_min`, la policy forza `HOLD` e registra `reason="edge_veto"`. Le incoerenze tra domande vengono monitorate (sono un segnale di drift).
4. **Degradazione**: timeout, HTTP 429/5xx, o risposta non valida -> `HOLD` con `reason="decider_unavailable"`, metrica incrementata, mai retry sincrono dentro il budget della barra. Le posizioni aperte restano protette dai bracket lato exchange.

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
  "product": "XLM-EUR",
  "clock": {"hour_utc": 14, "dow": 5},
  "position": {"side": "FLAT", "bars_held_15m": 0, "unreal_bps": null, "dist_sl_atr": null, "dist_tp_atr": null},
  "price": {
    "ret_bps": {"1m": 3, "15m": -42, "1h": -118, "4h": 65, "24h": 410},
    "seq_15m_bps": [12, -8, 30, -15, -40, 22, -9, -42],
    "dist_vwap_4h_bps": -55,
    "bb_pos_1h": 0.18,
    "hh_ll_pos_24h": 0.61
  },
  "momentum": {"rsi_14_15m": 38, "rsi_14_1h": 47, "macd_hist_1h_bps": -6},
  "volatility": {
    "atr_14_15m_bps": 64,
    "atr_14_1h_bps": 152,
    "rv_1h_bps": 58,
    "rv_ratio_1h_24h": 1.34,
    "atr_pctile_30d": 72
  },
  "liquidity": {"spread_bps": 9, "vol_z_1h": 1.8, "trades_z_1h": 2.1},
  "sentiment": {
    "asset": {"ewm": 0.38, "mass": 2.71, "disp": 0.22, "shock_15m": 0.30, "n_1h": 3, "last_choice": 2, "last_age_min": 3},
    "market": {"ewm": -0.05, "mass": 6.10, "disp": 0.41, "shock_15m": 0.12, "n_1h": 9},
    "fear_greed": 54
  },
  "btc_context": {"ret_1h_bps": -35, "rv_ratio_1h_24h": 1.12},
  "costs": {"round_trip_bps": 140, "min_tp_bps": 180}
}
```

Forma canonica inviata al modello (una riga, ~200–300 token Qwen):

```
{"v":"chronos.state.v1","product":"XLM-EUR","clock":{"hour_utc":14,"dow":5},"position":{"side":"FLAT",...},...}
```

Note sui campi:
- `seq_15m_bps`: ultimi 8 rendimenti a 15 min, sostituisce la finestra grezza che il Transformer v1 riceveva; da' contesto temporale con 8 numeri.
- `btc_context`: per prodotti non-BTC, BTC e' il driver di regime dominante. Per `BTC-*` il blocco e' `null`.
- `costs`: il modello deve "vedere" i costi, perche' le label del fine-tuning sono nette di fee (sez. 3). `round_trip_bps` = `2 * taker_fee + spread_bps + slippage_buffer_bps`.
- `sentiment: null` quando il servizio sentiment non ha dati (avvio a freddo, outage): il modello e' addestrato anche su questo caso (feature dropout, sez. 3.1.5).

#### 2.2.2 Richiesta al decider (`POST /v1/systemone`)

```json
{
  "model": "chronos-decider-qwen35-9b-lora@v1.2.0",
  "state": "{\"v\":\"chronos.state.v1\",\"product\":\"XLM-EUR\",...}",
  "questions": {
    "action": {
      "type": "choice",
      "instructions": "Spot long-only crypto account. Given the state, which action maximizes expected net return after fees over the next 4 hours?",
      "criteria": {
        "BUY": "open a long position now (only valid when position.side is FLAT)",
        "SELL": "close the existing long now (only valid when position.side is LONG)",
        "HOLD": "do nothing this minute"
      }
    },
    "edge": {
      "type": "noul",
      "instructions": "If a long were opened now with take-profit at +3.0 ATR(15m) and stop-loss at -2.0 ATR(15m), would the take-profit be hit first within 4 hours?"
    },
    "vol_regime": {
      "type": "score",
      "instructions": "What will realized volatility be over the next 4 hours relative to the last 30 days?",
      "criteria": ["very_low", "low", "normal", "high", "extreme"]
    }
  }
}
```

Il testo di `instructions` e `criteria` e' **congelato e versionato** (`modules/decision/questions.py`, costante `QUESTIONS_V1` + hash SHA-256). Deve essere identico byte per byte a quello usato nel dataset di training. Modificare una domanda = nuova versione del modello.

#### 2.2.3 Risposta attesa

```json
{
  "model": "chronos-decider-qwen35-9b-lora@v1.2.0",
  "answers": {
    "action": {"choice": "BUY", "probabilities": {"BUY": 0.71, "SELL": 0.02, "HOLD": 0.27}, "confidence": 0.37},
    "edge": {"noul": 0.58},
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
  "product": "XLM-EUR",
  "state_sha256": "9f2c...",
  "state": "{...canonical...}",
  "questions_version": "QUESTIONS_V1:5e1a...",
  "backend": "chronos-decider",
  "model": "chronos-decider-qwen35-9b-lora@v1.2.0",
  "answers": {"...": "come 2.2.3"},
  "policy": {"masked_probs": {"BUY": 0.97, "HOLD": 0.03}, "final": "BUY", "reason": "thresholds_met",
             "tau_buy": 0.62, "p_edge_min": 0.52},
  "latency_ms": {"build": 1.4, "http_total": 48.9, "server_model": 41.2},
  "order_intent_id": "01JAB3...ULID"
}
```

#### 2.2.5 Config YAML

```yaml
decision:
  backend: "chronos-decider"        # openjev | chronos-decider | baseline
  base_url: "http://decider:8080"
  api_key_env: "DECIDER_API_KEY"
  model: "chronos-decider-qwen35-9b-lora@v1.2.0"
  timeout_ms: 250
  questions_version: "QUESTIONS_V1"
  horizon_min: 240
  policy:                            # caricata da models/decider/<version>/policy.json, prodotta dal validation
    tau_buy: 0.62
    tau_sell: 0.55
    c_min: 0.15
    p_edge_min: 0.52
    allowed_regimes_for_entry: [1, 2, 3]   # vieta very_low (barriere < costi) ed extreme
  baseline_model_path: "models/baseline/lgbm_v1.txt"
```

### 2.3 Linee Guida per l'implementazione in Sonnet 5.5

> PROMPT — Feature, Stato e Decision Layer
>
> Implementa i package `modules/market/`, `modules/features/`, `modules/decision/` di Chronos v2 (Python 3.12, asyncio, pydantic v2, mypy strict).
>
> **A. `modules/market/`**
> 1. `ws_feed.py` — `CoinbaseMarketStream`: usa `coinbase.websocket.WSClient` (da `coinbase-advanced-py`) sui canali `market_trades`, `ticker`, `level2`, `heartbeats` (`level2` alimenta il `LocalOrderBook` usato per spread, stima dello slippage e simulatore di fill della sez. 5; verificare se il canale richiede autenticazione JWT e, nel caso, usare la stessa key read-only). Il WSClient lavora con callback in un thread proprio: fai il bridge verso asyncio con `loop.call_soon_threadsafe(queue.put_nowait, msg)`. Riconnessione automatica con backoff; rileva gap tramite `sequence_num` e, in caso di gap, segnala `BarAggregator.mark_dirty(product)`. Non usare il canale `candles` (granularita' 5 min): le barre 1m si costruiscono dai trade.
> 2. `bars.py` — `BarAggregator`: costruisce barre OHLCV 1m dai trade (bucket = `floor(trade_time, 1min)`), emette `BarClosed(product, bar)` su una queue alla chiusura del minuto (timer allineato al wall-clock + 2 s di grazia per trade tardivi). Barre senza trade = barra piatta con `volume=0` e `close` precedente. Su `mark_dirty` riallinea con `RESTClient.get_candles(granularity="ONE_MINUTE")` in `to_thread`. Aggrega anche 15m e 1h dalle barre 1m.
> 3. `indicators.py` — `IndicatorEngine`, **incrementale O(1) per barra**, uno stato per (product, timeframe): ATR di Wilder (14), RSI di Wilder (14), EMA per MACD (12/26/9), Bollinger (20, 2), VWAP rolling 4h, realized vol = `sqrt(sum(r^2))` su finestra, z-score rolling di volume e numero di trade (finestra 24h), percentile ATR su 30 giorni (usa `sortedcontainers.SortedList` con finestra scorrevole). Fornisci anche la versione vettoriale pandas degli stessi indicatori in `indicators_batch.py`: un test deve verificare che batch e incrementale coincidano (tolleranza 1e-9) su 5000 barre reali da `data/historical_*.csv`. La versione batch e' quella usata per costruire il dataset di training.
> 4. All'avvio, warm-up degli indicatori con le ultime 30 giornate di candele 1m (cache Parquet in `data/bars/<product>.parquet`, aggiornata incrementalmente).
>
> **B. `modules/features/`**
> 1. `schemas.py` — pydantic `DecisionState` con i sotto-modelli della sez. 2.2.1. Campi interi dichiarati `int`, i decimali con validator che arrotonda (`round(x, 2)`). Ordine dei campi = ordine di dichiarazione.
> 2. `state_builder.py` — `StateBuilder.build(product, as_of, indicators, sentiment, position, costs) -> DecisionState`. Funzione pura. Conversione in bps: `int(round(x * 10_000))`. Clamp dei valori estremi (`ret_bps` in [-5000, 5000], z-score in [-6, 6]) per evitare token fuori distribuzione.
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
> 3. `baseline.py` — `BaselineDecider` con la stessa firma di `decide`, che produce `DecisionAnswers` da tre modelli LightGBM (multiclass per action, binary per edge, multiclass ordinale per regime) sugli stessi campi numerici dello stato appiattito (`flatten_state()` in `features/serializer.py`).
> 4. `policy.py` — `DecisionPolicy(config).resolve(answers, position_side) -> Decision` secondo la sez. 2.1.2. Funzione pura, tabella di test parametrizzata con tutti i casi (FLAT/LONG x BUY/SELL/HOLD x veto edge x veto regime x decider down).
> 5. `engine.py` — `DecisionEngine`: consuma `BarClosed`, per prodotto costruisce lo stato, chiama il backend configurato (fallback su `BaselineDecider` in caso di eccezione se `decision.fallback_to_baseline: true`), applica la policy, scrive `DecisionRecord` su JSONL con `aiofiles` (rotazione giornaliera), pubblica `Decision` sulla queue verso il RiskManager. I prodotti vengono processati in parallelo con `asyncio.gather`, ma con un `asyncio.Semaphore(4)` sul decider.
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

Per ogni prodotto e ogni timestamp di campionamento `t` (chiusura barra 1m):

```
entry      = open[t+1] * (1 + slip_bps/1e4)                # ingresso alla barra successiva: niente lookahead
A          = ATR_14_15m(t)                                  # noto al tempo t
upper      = entry + k_tp * A
lower      = entry - k_sl * A
vertical   = t + H                                          # H = 240 min
scan 1m bars in (t+1, t+H]:
    if low <= lower and high >= upper nella stessa barra -> esito = LOWER   (conservativo)
    elif high >= upper -> esito = UPPER
    elif low  <= lower -> esito = LOWER
se nessuna barriera -> esito = VERTICAL, exit = close[t+H]
net_ret    = (exit_price / entry - 1) - round_trip_fee - spread_cost
```

Default del training run v1: `k_tp = 3.0`, `k_sl = 2.0`, `H = 240`, `round_trip_fee` = fee taker del tier attuale x 2, `slip_bps = 5`. Questi parametri sono scritti nel `model_card.json` e **devono** coincidere con la sez. 4 (RiskManager), altrimenti `edge.noul` non ha significato in esecuzione.

Label per domanda:

| Domanda | Stato posizione nel sample | Regola target |
|---------|----------------------------|---------------|
| `action` | FLAT | `BUY` se esito = UPPER **e** `net_ret > 0`, altrimenti `HOLD`. |
| `action` | LONG (sintetico, vedi sotto) | `SELL` se esito = LOWER **oppure** (VERTICAL e `net_ret_from_now < -exit_fee`), altrimenti `HOLD`. |
| `edge` | FLAT e LONG | `true` se esito = UPPER, altrimenti `false`. |
| `vol_regime` | FLAT e LONG | Bucket 0..4 di `rv_forward(t, t+H)` rispetto ai quantili 20/40/60/80 della distribuzione di `rv` a 4h dei **30 giorni precedenti a t** (quantili point-in-time). |

**Sample LONG sintetici:** per ogni `t` campionato si genera anche uno stato con `position.side = LONG`, dove l'ingresso fittizio e' a `t - U`, con `U ~ Uniform{15..H}` minuti. Si calcolano `unreal_bps`, `dist_sl_atr`, `dist_tp_atr` coerenti con quell'ingresso e si scartano i sample in cui il bracket fittizio sarebbe gia' stato chiuso prima di `t`. In questo modo il modello vede entrambe le condizioni di posizione e impara il mascheramento.

#### 3.1.4 Campionamento, bilanciamento, split

- **Campionamento:** non ogni minuto (autocorrelazione estrema, label quasi duplicate). Si usa l'unione di (a) una griglia ogni 15 minuti e (b) eventi del filtro CUSUM simmetrico sui log-rendimenti 1m con soglia `h = 0.5 * ATR_15m%`. Ordine di grandezza atteso: ~40–60k timestamp per prodotto per anno.
- **Bilanciamento:** `HOLD` domina. Sottocampionamento di `HOLD` a un rapporto massimo 3:1 rispetto alla classe di minoranza **solo nel train**; validation e test restano con la distribuzione naturale (necessaria per la calibrazione). Pesi per sample = unicita' media della label (inverso del numero di label concorrenti che si sovrappongono nel tempo), alla Lopez de Prado.
- **Split:** walk-forward con espansione, 5 fold. Tra train e validation/test: **purge** dei sample la cui finestra `[t, t+H]` interseca il periodo di test + **embargo** di 24 h. Nessuno split casuale.
- **Holdout finale:** ultimi 60 giorni, non toccati fino al gate finale.

#### 3.1.5 Sentiment storico e feature dropout

Le news RSS non hanno storico. Strategia:
1. **v1.0:** sentiment storico ricostruito da GDELT (titoli con timestamp di ingestione GDELT, usati come `ingested_at`) passato nello stesso classificatore ONNX e nello stesso aggregatore. Dove GDELT non copre, `sentiment: null`.
2. **Feature dropout:** nel 25% dei sample di train il blocco `sentiment` viene forzato a `null`, cosi' il modello e' robusto sia agli outage live sia alla differenza di distribuzione GDELT vs RSS live.
3. **v1.x:** retraining mensile includendo i `DecisionRecord` live, che contengono il sentiment reale delle sorgenti RSS. Dopo >= 6 mesi di dati live, il peso dei dati GDELT viene ridotto.

#### 3.1.6 Calibrazione post-training

Temperature scaling **per domanda** (`T_action`, `T_edge`, `T_regime`) sul validation fold piu' recente. Le temperature sono salvate nel model card e applicate dal server. Dopo la calibrazione, `DecisionPolicy` viene tarata (grid search delle soglie `tau_buy`, `p_edge_min`, `c_min`) massimizzando il PnL netto simulato sul validation set, con vincolo `trade_count >= 30` per fold. L'output e' `policy.json`.

### 3.2 Struttura dei Dati / Schema JSON

#### 3.2.1 Dataset JSONL (un record per coppia stato-domanda)

```json
{
  "id": "XLM-EUR|2025-03-01T12:15:00Z|FLAT|action",
  "fold": 3,
  "split": "train",
  "product": "XLM-EUR",
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
    "net_ret_bps": 212,
    "exit_minute": 133,
    "atr_15m_bps": 64,
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
  "label_config": {"k_tp": 3.0, "k_sl": 2.0, "horizon_min": 240, "atr": "ATR_14_15m",
                   "round_trip_fee_bps": 120, "slip_bps": 5, "sha256": "a41c..."},
  "label_token_ids": {"action": [362, 425, 356], "edge": [809, 451], "vol_regime": [220, 16, 17, 18, 19]},
  "temperatures": {"action": 1.31, "edge": 1.12, "vol_regime": 1.07},
  "data": {"products": ["BTC-EUR", "ETH-EUR", "XLM-EUR"], "train_range": ["2024-01-01", "2026-06-30"],
           "n_samples": 612000, "holdout": ["2026-08-01", "2026-09-30"]},
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
| G6 Dry run | Gate G-DRY della sez. 5.1.7 superato con il modello candidato come shadow book. |

Se G3 fallisce, il sistema va in produzione con `BaselineDecider`. Il modello LLM deve guadagnarsi il posto con i numeri: questa e' la contromisura principale contro l'overfitting di un modello da 9B su pochi campioni indipendenti.

### 3.5 Linee Guida per l'implementazione in Sonnet 5.5

> PROMPT — Pipeline di training e server decider
>
> Crea il package `training/` e la directory `decider/` di Chronos v2. Python 3.12. Librerie: `polars` (dataset), `numpy`, `numba` (scansione barriere), `transformers`, `peft`, `bitsandbytes`, `accelerate`, `datasets`, `lightgbm`, `scikit-learn` (solo metriche), `scipy`, `fastapi`, `uvicorn`, `pydantic` v2. Niente `trl`: il trainer e' un `transformers.Trainer` con `compute_loss` custom.
>
> 1. `training/labeling.py`
>    - `triple_barrier(open, high, low, close, atr, t_idx, k_tp, k_sl, horizon, fee_rt, slip_bps) -> (outcome: int8, exit_idx: int32, net_ret: float64)` vettorizzata e compilata con `@numba.njit`, regola conservativa "entrambe le barriere nella stessa barra -> LOWER";
>    - `forward_rv_bucket(...)` con quantili calcolati su finestra 30 giorni **precedente** a ogni t (rolling, nessun dato futuro);
>    - test con serie sintetiche costruite a mano per ogni esito (UPPER, LOWER, VERTICAL, stessa barra).
> 2. `training/sampling.py` — `cusum_events(log_ret, h)` e griglia a 15 minuti; `uniqueness_weights(t0, t1)`.
> 3. `training/splits.py` — `purged_walk_forward(timestamps, t1, n_folds=5, embargo=timedelta(hours=24))` che produce indici; test che verifica l'assenza di sovrapposizione tra intervalli `[t, t1]` di train e di test.
> 4. `training/build_dataset.py` — CLI: `python -m training.build_dataset --config config/train.yml`. Passi: carica candele 1m da Parquet -> calcola indicatori con `modules.market.indicators_batch` -> ricostruisce sentiment storico da `data/news_history.db` con `modules.sentiment.aggregator` (stesso codice del runtime) -> costruisce `DecisionState` con `modules.features.state_builder` e serializza con `modules.features.serializer.serialize_state` (stesso codice del runtime) -> genera sample FLAT e LONG sintetici -> label -> feature dropout 25% sul blocco sentiment nel solo train -> scrive `data/datasets/<run_id>/{train,val,test}.jsonl` e `label_config.json`. Fallisce se un token di etichetta non e' single-token.
> 5. `training/prompting.py` — `PROMPT_TEMPLATE_V1`, `render_prompt(state, question) -> str`, `label_token_ids(tokenizer, question) -> list[int]`. Importato sia dal trainer sia dal server.
> 6. `training/train_lora.py` — carica il base model (4-bit NF4 se `quantization.load_in_4bit`), `prepare_model_for_kbit_training`, `get_peft_model(LoraConfig(...))` dai parametri della sez. 3.2.3. Collator: tokenizza `prompt` (senza token di etichetta), registra la posizione dell'ultimo token e gli id dei token di etichetta della domanda. `compute_loss`: `logits_last = outputs.logits[arange(B), last_pos]`, `restricted = logits_last.gather(1, label_ids_padded)` con mascheramento `-inf` dove le domande hanno meno opzioni, `loss = (F.cross_entropy(restricted, target_index, reduction="none") * weight).mean()`. Valutazione ogni 500 step con log-loss ristretta e accuracy per domanda. Salva adapter e tokenizer.
> 7. `training/calibrate.py` — inferenza sul validation set, ottimizzazione di `T_q` per domanda (NLL, `minimize_scalar`), ECE e Brier prima/dopo, reliability diagram PNG con matplotlib. Scrive `temperatures` nel model card.
> 8. `training/backtest.py` — simulatore event-driven barra per barra che usa **la stessa** `DecisionPolicy` e lo **stesso** `RiskManager` del runtime (sez. 4) con un `SimulatedExchange` (fill del limit se `low <= limit` per BUY; bracket TP/SL simulati con la regola conservativa; fee maker/taker da config). Output: equity curve, trade list, Sharpe annualizzato, max drawdown, profit factor, turnover, fee pagate.
> 9. `training/baseline_lgbm.py` — addestra tre LightGBM sugli stessi split usando `flatten_state()`, salva in `models/baseline/`.
> 10. `training/tune_policy.py` — grid search delle soglie su validation -> `policy.json`.
> 11. `training/evaluate.py` — esegue G1–G4 della sez. 3.4 e stampa una tabella; exit code 1 se un gate fallisce.
> 12. `decider/server.py` + `decider/Dockerfile` (base `nvidia/cuda` runtime + torch CUDA) secondo la sez. 3.3. Schemi request/response pydantic identici a `modules/decision/client.py` (pacchetto condiviso `modules/decision/protocol.py`). Merge dell'adapter in `decider/export.py`.
>
> Test di parita' obbligatorio (`tests/test_train_serve_parity.py`): dato uno stato golden e un modello tiny (`hf-internal-testing/tiny-random-Qwen2` o equivalente), le probabilita' calcolate dal percorso di training (`compute_loss` in eval) e quelle restituite dal server devono coincidere (tolleranza 1e-4).

---

## 4. RISK MANAGEMENT DINAMICO — Execution Layer

### 4.1 Architettura Concettuale

**Prima (v1):** SL = prezzo x 0.98, TP = prezzo x 1.05, per ogni prodotto e ogni regime; quantita' fissa 5; payload di bracket non valido per l'API.

**Dopo (v2):** barriere espresse in unita' ATR (le stesse delle label, sez. 3.1.3), sizing a rischio fisso modulato dalla probabilita' calibrata e dal regime previsto, gate di valore atteso netto di fee, bracket nativo Coinbase allegato all'ordine di ingresso.

```
Decision(BUY, p_edge, regime, conf)
   -> RiskManager.plan_entry()
        1. quote: best_bid/best_ask/spread da ticker WS; product meta (increments, min size) cache 1h
        2. fees: maker/taker dal transaction summary, cache 1h
        3. A = ATR_14_15m(t); entry_ref = best_ask
        4. SL = entry_ref - k_sl * A ; TP = entry_ref + k_tp * A           (k da model card)
        5. fee floor:   (TP - entry)/entry >= round_trip + spread + min_net_edge     altrimenti SKIP
        6. EV gate:     EV = p_edge*(TP-entry) - (1-p_edge)*(entry-SL) - costs > 0   altrimenti SKIP
        7. sizing:      vedi 4.1.2
        8. KillSwitch.check()  -> SKIP se attivo
   -> OrderIntent (Decimal, gia' arrotondati agli increment)
   -> ExecutionGateway.submit_entry_with_bracket()
        POST /api/v3/brokerage/orders  (limit_limit_gtc marketable + attached trigger_bracket_gtc)
        timeout ingresso N s -> cancel residuo
   -> user channel WS -> fills -> PositionLedger (SQLite)
   -> Watchdog: prezzo oltre SL di X ATR e posizione ancora aperta -> uscita d'emergenza
```

Decision(SELL) su posizione LONG: cancella il bracket attivo, poi chiude con limit marketable `sell` a `best_bid` con timeout e fallback a `market_market_ioc`.

#### 4.1.1 Dove interviene "la volatilita' calcolata dall'AI"

L'adattivita' arriva da due fonti, con ruoli separati per mantenere la coerenza con le label:

1. **ATR in tempo reale** (deterministico): le distanze di SL/TP scalano automaticamente con la volatilita' corrente, perche' sono `k * ATR`. I moltiplicatori `k_sl`, `k_tp` sono **fissi per versione del modello**: sono quelli con cui `edge.noul` e' stato addestrato. Cambiarli a runtime renderebbe `p_edge` non calibrata.
2. **Regime previsto dal modello** (`vol_regime`, volatilita' futura sull'orizzonte H): modula **quanto** rischiare e **se** entrare, non dove mettere le barriere.

| `vol_regime` (argmax) | Moltiplicatore sizing `m_reg` | Ingresso |
|-----------------------|-------------------------------|----------|
| 0 very_low | 0.0 | Vietato: barriere troppo strette rispetto alle fee. |
| 1 low | 0.75 | Permesso. |
| 2 normal | 1.0 | Permesso. |
| 3 high | 0.6 | Permesso. |
| 4 extreme | 0.0 | Vietato: rischio di gap oltre lo stop. |

Estensione v2.1 (opzionale): `k` dipendenti dal regime, ma solo con label generate per regime (un training set per tabella `k(regime)`) e una domanda `edge` per tabella.

#### 4.1.2 Position sizing

```
equity          = quote_available + sum(mark_to_market posizioni)            # da accounts API, cache 60 s
risk_frac       = config.risk_per_trade                                      # es. 0.005 = 0.5% equity
scale_p         = clip((p_edge - p_edge_min) / (p_edge_full - p_edge_min), 0, 1)
risk_eur        = equity * risk_frac * scale_p * m_reg
stop_dist       = entry - SL                                                 # in quote currency
size_raw        = risk_eur / (stop_dist + entry * fee_rt)                    # include le fee nella perdita a SL
size_cap        = min(equity * max_position_frac, quote_available * 0.98) / entry
size            = floor_to_increment(min(size_raw, size_cap), base_increment)
if size < base_min_size or size*entry < quote_min_size -> SKIP(reason="below_min_size")
```

#### 4.1.3 Arrotondamenti (Decimal, mai float in questo layer)

| Valore | Regola | Motivo |
|--------|--------|--------|
| `base_size` | floor a `base_increment` | Mai superare il rischio pianificato. |
| entry limit BUY | ceil a `quote_increment` | Resta marketable. |
| `limit_price` TP | floor a `quote_increment` | Leggermente piu' facile da raggiungere, non oltre il piano. |
| `stop_trigger_price` SL | ceil a `quote_increment` | Lo stop non va mai piu' lontano del pianificato. |

Vincoli di validazione prima dell'invio: `SL < entry < TP`; `(TP-entry) >= 2 * quote_increment`; `(entry-SL) >= 2 * quote_increment`.

#### 4.1.4 Kill switch e guardie

| Guardia | Condizione | Azione |
|---------|-----------|--------|
| Daily loss | PnL realizzato + non realizzato del giorno UTC <= -`max_daily_loss_frac` x equity di apertura | Blocca nuovi ingressi fino a 00:00 UTC. |
| Loss streak | `max_consecutive_losses` stop consecutivi | Pausa 6 h. |
| Stale data | ultima barra o ultimo ticker piu' vecchio di 90 s | Nessun ingresso; i bracket esistenti restano. |
| Decider health | > 20% di decisioni in fallback nell'ultima ora, oppure tasso di incoerenza action/edge > soglia | Passa a baseline o HOLD-only (config). |
| Drift | PSI > 0.25 su `edge.noul` o sulle feature principali rispetto alla finestra di training | Alert + HOLD-only. |
| Exposure | Esposizione totale > `max_total_exposure_frac` | Nessun nuovo ingresso. |
| Watchdog SL | `last_price <= SL - 0.5*A` e posizione ancora aperta dopo 10 s | Cancella bracket, `market_market_ioc` sell. (La doc Coinbase dichiara che l'esecuzione della protezione downside non e' garantita in alta volatilita'.) |

#### 4.1.5 Riconciliazione

- Avvio: legge account, ordini aperti (`list_orders` con `order_status=OPEN`) e fill recenti; ricostruisce il ledger; un ordine con `client_order_id` di Chronos senza record locale viene importato; una posizione senza bracket attivo riceve un bracket standalone `trigger_bracket_gtc` sell.
- Runtime: canale WS `user` per gli aggiornamenti degli ordini; polling REST di sicurezza ogni 60 s.
- Idempotenza: `client_order_id = uuid5(NAMESPACE_CHRONOS, f"{decision_id}:{leg}")`. Un retry di rete non crea mai un secondo ordine.

### 4.2 Struttura dei Dati / Schema JSON

**OrderIntent** (interno, prodotto dal RiskManager):

```json
{
  "intent_id": "01JAB3...ULID",
  "decision_id": "01JAB3...ULID",
  "product_id": "XLM-EUR",
  "side": "BUY",
  "entry": {"type": "limit_marketable", "limit_price": "0.31842", "time_in_force_s": 20},
  "base_size": "1570",
  "bracket": {"take_profit": "0.33070", "stop_loss": "0.31023",
              "k_tp": 3.0, "k_sl": 2.0, "atr_15m": "0.000204"},
  "risk": {"equity_eur": "2500.00", "risk_eur": "12.50", "p_edge": 0.58, "scale_p": 0.6,
           "regime": 2, "m_reg": 1.0, "ev_bps": 37, "fee_rt_bps": 120, "spread_bps": 9},
  "created_at": "2026-10-17T14:03:01.120Z"
}
```

**Payload Coinbase — ingresso con TP/SL allegato** (`POST /api/v3/brokerage/orders`):

```json
{
  "client_order_id": "6f1c2a8e-5b1e-5c9a-9d1f-1f0b7c3a2e11",
  "product_id": "XLM-EUR",
  "side": "BUY",
  "order_configuration": {
    "limit_limit_gtc": {"base_size": "1570", "limit_price": "0.31842", "post_only": false}
  },
  "attached_order_configuration": {
    "trigger_bracket_gtc": {"limit_price": "0.33070", "stop_trigger_price": "0.31023"}
  }
}
```

Note dalla documentazione Coinbase: nell'ordine allegato **non** si specifica la size (eredita quella dell'ordine originario); quando un fill avviene su uno dei due livelli l'altro lato viene disattivato automaticamente. La doc mostra l'esempio con `limit_limit_gtc`: il supporto con `market_market_ioc` come ordine originario non e' documentato. Il default della v2 e' quindi il limit marketable con timeout.

**Validazione dei payload:** la sandbox Advanced Trade (`api-sandbox.coinbase.com`) restituisce solo risposte statiche predefinite e non valida i payload, quindi non serve a verificare l'accettazione dell'attached bracket. La validazione avviene in due modi: (1) `POST /api/v3/brokerage/orders/preview` in produzione con lo stesso body, che non crea ordini (verificare quale permesso della API key richiede: se richiede `trade`, in dry mode resta disattivato, sez. 5); (2) il primo ordine reale della fase live-micro (sez. 5.1.7).

**Payload Coinbase — bracket standalone** (fallback a due passi, e riconciliazione):

```json
{
  "client_order_id": "b2d9...",
  "product_id": "XLM-EUR",
  "side": "SELL",
  "order_configuration": {
    "trigger_bracket_gtc": {"base_size": "1570", "limit_price": "0.33070", "stop_trigger_price": "0.31023"}
  }
}
```

**PositionLedger** (SQLite):

```sql
CREATE TABLE IF NOT EXISTS positions (
  product_id TEXT PRIMARY KEY, side TEXT NOT NULL CHECK (side IN ('FLAT','LONG')),
  base_qty TEXT NOT NULL, avg_entry TEXT, opened_at TEXT, entry_decision_id TEXT,
  bracket_order_id TEXT, tp TEXT, sl TEXT, k_tp REAL, k_sl REAL, atr_at_entry TEXT
);
CREATE TABLE IF NOT EXISTS orders (
  client_order_id TEXT PRIMARY KEY, exchange_order_id TEXT, intent_id TEXT, leg TEXT,
  product_id TEXT, side TEXT, status TEXT, payload TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS fills (
  trade_id TEXT PRIMARY KEY, client_order_id TEXT, product_id TEXT, side TEXT,
  price TEXT, size TEXT, fee TEXT, liquidity TEXT, trade_time TEXT
);
```

Prezzi e quantita' sono salvati come `TEXT` (stringhe Decimal) per non perdere precisione.

**Config YAML**:

```yaml
risk:
  barriers_from_model_card: true          # k_tp, k_sl, atr timeframe letti dal model card del decider
  risk_per_trade: 0.005
  p_edge_full: 0.70
  max_position_frac: 0.20
  max_total_exposure_frac: 0.50
  min_net_edge_bps: 30
  slippage_buffer_bps: 5
  regime_multipliers: [0.0, 0.75, 1.0, 0.6, 0.0]
  kill_switch:
    max_daily_loss_frac: 0.02
    max_consecutive_losses: 4
    stale_data_s: 90
    max_fallback_ratio_1h: 0.20
    psi_threshold: 0.25
  watchdog:
    sl_breach_atr: 0.5
    grace_s: 10
execution:
  mode: "dry"                             # dry | live  (sez. 5; live richiede CHRONOS_LIVE_CONFIRM)
  entry_timeout_s: 20
  exit_timeout_s: 15
  reconcile_interval_s: 60
  fee_refresh_s: 3600
  product_meta_refresh_s: 3600
```

### 4.3 Linee Guida per l'implementazione in Sonnet 5.5

> PROMPT — Risk Manager ed Execution Gateway
>
> Implementa `modules/risk/`, `modules/execution/`, `modules/ledger/` e l'orchestratore `modules/app.py` di Chronos v2. Python 3.12, asyncio, pydantic v2, `decimal.Decimal` con contesto a 28 cifre per **tutti** i prezzi e le size di questo layer (conversione da float solo tramite `Decimal(str(x))`).
>
> 1. `modules/config.py` — sostituisce `modules/utils.load_config`: `pydantic-settings` con modelli `SentimentConfig`, `DecisionConfig`, `RiskConfig`, `ExecutionConfig`, `CoinbaseConfig`; i segreti vengono **solo** da variabili d'ambiente (`COINBASE_API_KEY`, `COINBASE_API_SECRET`, `DECIDER_API_KEY`, `CRYPTOPANIC_TOKEN`), mai dal YAML. Validazione all'avvio; errori di config = exit code 2 con messaggio leggibile.
> 2. `modules/execution/exchange.py` — `CoinbaseExchange`, wrapper async attorno a `coinbase.rest.RESTClient` (chiamate sincrone in `asyncio.to_thread`). Metodi: `get_product(product_id) -> ProductMeta` (`base_increment`, `quote_increment`, `base_min_size`, `quote_min_size`), `get_fee_rates() -> FeeRates` (da `get_transaction_summary`, campi del fee tier maker/taker), `get_best_bid_ask(product_id)`, `create_order(body: dict) -> CreateOrderResult`, `cancel_orders(order_ids)`, `list_open_orders()`, `get_fills(order_id)`, `get_accounts()`. Per `create_order` con `attached_order_configuration`: verifica con un test di introspezione se `RESTClient.create_order` della versione installata accetta il parametro; se no, usa `client.post("/api/v3/brokerage/orders", data=body)`. Gestisci `success: false` + `error_response` trasformandoli in `OrderRejected(reason, message)`. Rate limiting con `aiolimiter.AsyncLimiter` (configurabile, default 20 req/s privati).
> 3. `modules/execution/rounding.py` — `floor_to(x, inc)`, `ceil_to(x, inc)` con `Decimal.quantize`/divisione intera; test property-based con `hypothesis` (il risultato e' multiplo dell'increment ed entro un increment dall'input, nella direzione corretta).
> 4. `modules/risk/sizing.py` — funzione pura `compute_size(...) -> SizingResult | SkipReason` secondo la sez. 4.1.2. Test con `hypothesis`: la perdita a SL (incluse le fee) non supera mai `risk_eur` + un increment.
> 5. `modules/risk/risk_manager.py` — `RiskManager.plan_entry(decision, market, position) -> OrderIntent | Skip` implementa i passi 1–8 della sez. 4.1 e l'arrotondamento della sez. 4.1.3. Legge `k_tp`, `k_sl`, timeframe ATR dal model card del decider caricato all'avvio; se il model card manca, rifiuta di avviarsi in `live`. `plan_exit(decision, position) -> ExitIntent`. Ogni `Skip` ha un `reason` enumerato e viene loggato nel `DecisionRecord`.
> 6. `modules/risk/kill_switch.py` — `KillSwitch` con le guardie della sez. 4.1.4; stato persistito in SQLite (sopravvive al riavvio del container); `check() -> KillState`.
> 7. `modules/execution/gateway.py` — `ExecutionGateway(Protocol)` con `submit_entry_with_bracket`, `submit_exit`, `emergency_exit`, `cancel`; questa sezione implementa `LiveExecutionGateway`, la sez. 5 implementa `ShadowExecutionGateway` con la stessa interfaccia. `LiveExecutionGateway`:
>    - `submit_entry_with_bracket(intent)`: costruisce il payload JSON della sez. 4.2 (stringhe Decimal), `client_order_id` deterministico `uuid5`, invia, registra in `orders`; avvia un task di timeout che dopo `entry_timeout_s` cancella il residuo non fillato;
>    - se l'exchange rifiuta l'attached bracket (feature non disponibile per il prodotto), passa al percorso a due passi: ingresso limit, poi, al fill (evento dal canale `user`), bracket standalone `trigger_bracket_gtc` SELL con `base_size` = quantita' effettivamente fillata;
>    - `submit_exit(intent)`: cancella il bracket attivo, attende la conferma della cancellazione (o lo stato terminale), limit marketable sell a `best_bid`, timeout, fallback `market_market_ioc`;
>    - `emergency_exit(product_id)` usato dal watchdog.
> 8. `modules/execution/user_stream.py` — WS canale `user` (autenticato) -> eventi `OrderUpdate` -> aggiornamento `orders`, `fills`, `positions`. Bridge thread->asyncio come per il market stream.
> 9. `modules/execution/reconciler.py` — `Reconciler.on_startup()` e loop periodico secondo la sez. 4.1.5.
> 10. `modules/execution/watchdog.py` — task che a ogni ticker verifica la condizione della sez. 4.1.4 per ogni posizione LONG.
> 11. `modules/ledger/positions.py` — `PositionLedger` su `aiosqlite`, DDL della sez. 4.2, tutte le mutazioni in transazione; `view(product_id) -> PositionView` usato dallo StateBuilder.
> 12. `modules/app.py` — orchestratore: costruisce i servizi, `async with asyncio.TaskGroup() as tg:` avvia market stream, bar aggregator, sentiment service, decision engine, risk/execution consumer, user stream, reconciler, watchdog, metriche. Gestione `SIGTERM`/`SIGINT` (Docker stop): smette di aprire posizioni, attende le operazioni in volo fino a 10 s, chiude WS e DB; **non** cancella i bracket esistenti (le posizioni restano protette lato exchange). `main.py` diventa `asyncio.run(app.main())`.
> 13. `docker-compose.yml`: aggiungi il servizio `decider` con `profiles: [gpu]` (non parte di default; `build: ./decider`, `deploy.resources.reservations.devices: [{driver: nvidia, count: 1, capabilities: [gpu]}]`, porta esposta solo sulla rete interna, volume `./models/decider:/models:ro`, healthcheck su `/healthz`). `chronos` **non** dichiara `depends_on` verso `decider`: se `decision.backend != baseline` e il decider non risponde, il `DecisionEngine` usa il fallback baseline (sez. 2.1.2). Sostituisci l'healthcheck fittizio attuale di `chronos` con uno che verifica l'eta' dell'ultimo heartbeat scritto su file (`data/heartbeat`) < 120 s. Su Windows il servizio GPU richiede Docker Desktop con backend WSL2 e driver NVIDIA recenti.
>
> Test:
> - `SimulatedExchange` (condiviso con `training/backtest.py`) per test end-to-end: decisione BUY -> intent -> payload -> fill simulato -> posizione LONG con bracket -> TP colpito -> posizione FLAT, PnL corretto al centesimo con fee;
> - payload snapshot test: il JSON prodotto per l'intent della sez. 4.2 coincide con il fixture;
> - kill switch: daily loss raggiunta blocca nuovi ingressi ma non le uscite;
> - reconciler: posizione presente su exchange senza bracket -> viene creato il bracket standalone;
> - test marcato `@pytest.mark.preview` (eseguito solo manualmente) che invia il payload della sez. 4.2 a `orders/preview` in produzione, verifica `errs` vuoto e registra nel log la risposta; un secondo caso con `market_market_ioc` + attached documenta se la combinazione e' accettata. Non usare la sandbox per questo: e' statica.

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
fill    VirtualPosition(LONG) + VirtualBracket(TP, SL) con size = quantita' fillata
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
| Ingresso, parte marketable | Walk degli ask al primo snapshot del book dopo `t0 + latency_ms`, fino al prezzo limite. | `open[t+1] * (1 + spread/2 + slip_bps)` | taker |
| Ingresso, parte resting | Fill solo su trade con prezzo < limite; `qty <= queue_haircut * volume_oltre_limite`. | Fill se `low[t+k] < limit`, haircut sul volume della barra. | maker |
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

#### 5.1.6 Apprendimento sul campo: cosa si puo' imparare in una settimana

Una settimana contiene 10.080 barre da 1 minuto per prodotto, ma con orizzonte `H = 240 min` solo **~42 finestre indipendenti** per prodotto. Una settimana basta per **validare e calibrare**; non basta per addestrare un modello da zero, ne' per provare statisticamente che la strategia guadagna. Il learner e' quindi vincolato da soglie minime di campione e non sostituisce il training storico della sez. 3.

`NightlyLearner` (ogni giorno alle 00:15 UTC, fuori dal percorso critico, in un processo figlio a bassa priorita'):

| Step | Cosa aggiorna | Condizione minima | Output |
|------|---------------|-------------------|--------|
| L1 Calibrazione `edge` | Platt scaling (`a`, `b` su `logit(p_edge)`) fittato sulle label live degli ultimi 7 giorni. | >= 1.000 label risolte. Con label sovrapposte il campione effettivo e' minore: in sotto-soglia lo step non fa nulla. | `calibration.json` del challenger |
| L2 Soglie di policy | Grid search di `tau_buy`, `p_edge_min`, `c_min` su (validation storico + label live con peso 2). | >= 1.000 label live. | `policy.json` del challenger |
| L3 Refit baseline | LightGBM riaddestrato su storico + live (peso live 2, `init_model` = champion per warm start). | >= 2.000 label live cumulative. | `lgbm_*.txt` del challenger |
| L4 Modello di costo | `latency_ms` (p75 del RTT misurato verso le API REST), distribuzione dello spread per prodotto e ora, fee correnti. | Sempre. | `fill_model.json` (vale per tutti i book) |
| L5 Promozione | Challenger -> champion se, sugli ultimi 7 giorni di shadow book: PnL netto challenger > champion, >= 20 trade per book, max drawdown non peggiore, ECE di `edge` non peggiore. | Tutte le condizioni. | Nuova versione nel `ModelRegistry` |

Regole invarianti:
- nessun parametro cambia durante la giornata; il `DecisionEngine` ricarica gli artefatti promossi alle 00:30 UTC con uno swap atomico del riferimento e logga `model_version_changed`;
- ogni artefatto e' versionato in `models/registry/<version>/` con `manifest.json` (hash degli input, finestra dati, metriche); rollback = puntare il champion alla versione precedente;
- il learner non tocca mai i moltiplicatori `k_tp`/`k_sl` ne' l'orizzonte `H` (sez. 4.1.1): cambiarli invaliderebbe tutte le label;
- il fine-tuning LLM non avviene in questo loop (C8): le label live si accumulano per la fase cloud.

#### 5.1.7 Gate di uscita dal dry run e rampa di capitale

**G-DRY** (valutato dal report, sul book `champion`, su finestra minima 7 giorni):

| Criterio | Soglia |
|----------|--------|
| Numero di trade chiusi | >= 30. Se dopo 7 giorni sono meno di 30, il dry run **si estende** automaticamente: una settimana puo' non bastare. |
| PnL netto (dopo fee e slippage simulati) | > 0 |
| Profit factor | >= 1.2 |
| Contro `random_entry` | PnL netto champion > random_entry |
| Max drawdown | <= 5% del capitale virtuale |
| Calibrazione live `edge` | ECE <= 0.08 (piu' larga della sez. 3.4 per il campione ridotto) |
| Operativita' | uptime >= 99%, zero `DryRunViolation`, zero crash non gestiti, nessun kill switch scattato per bug |
| Fee floor | Quota di decisioni BUY scartate per `fee_floor` < 80% (sopra questa soglia il sistema non riesce a battere le fee e va rivisto l'orizzonte H, non forzato il live) |

**Rampa dopo G-DRY:**

| Gradino | Capitale live | Size | Condizione per salire |
|---------|---------------|------|-----------------------|
| R0 live-micro | Minimo operativo | `max(base_min_size, quote_min_size / price)` arrotondato, per ogni trade | 20 trade live, PnL netto >= 0, parity report entro le tolleranze |
| R1 | 2x R0 | Sizing a rischio della sez. 4.1.2 | 30 trade, PF >= 1.2, DD <= 5% |
| R2+ | Raddoppio per gradino | Sizing a rischio | Stesse condizioni di R1 sul gradino corrente |

In qualunque gradino: violazione del kill switch o PF < 1.0 su 30 trade -> discesa di un gradino; due discese consecutive -> ritorno in dry.

**Shadow twin in live:** in modalita' `live` lo `ShadowExecutionGateway` resta attivo in parallelo e simula ogni ordine reale con lo stesso intent. Il `ParityReport` giornaliero confronta reale e simulato per ciascun ordine: prezzo medio di fill, fee, tempo di fill, esito del bracket. Tolleranze: |delta prezzo di ingresso| <= 10 bps mediano, stesso esito del bracket in >= 90% dei trade. Gli scostamenti ricalibrano `latency_ms`, `queue_haircut`, `stop_extra_slip_bps` (step L4); un dry run con modello di fill calibrato sul reale e' molto piu' affidabile del precedente.

### 5.2 Struttura dei Dati / Schema JSON

**Config YAML:**

```yaml
execution:
  mode: "dry"                               # dry | live ; live richiede env CHRONOS_LIVE_CONFIRM=I_ACCEPT_REAL_ORDERS
dry_run:
  db_path: "data/chronos_dry.db"            # DB separato: dati dry e live non si mescolano mai
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
    min_labels_calibration: 1000
    min_labels_policy: 1000
    min_labels_refit: 2000
    live_weight: 2.0
    promotion: {min_trades: 20, window_days: 7}
  gate:
    min_days: 7
    min_trades: 30
    min_profit_factor: 1.2
    max_drawdown_frac: 0.05
    max_ece_edge: 0.08
    max_fee_floor_skip_ratio: 0.80
live_ramp:
  steps: ["R0", "R1", "R2"]
  r0_min_trades: 20
  step_min_trades: 30
  parity: {max_median_entry_delta_bps: 10, min_bracket_outcome_agreement: 0.90}
```

**ShadowOrder** (tabella `shadow_orders` nel DB dry):

```json
{
  "shadow_order_id": "01JAC0...ULID",
  "book": "champion",
  "intent_id": "01JAB3...ULID",
  "product_id": "XLM-EUR",
  "side": "BUY",
  "limit_price": "0.31842",
  "base_size": "1570",
  "submitted_at": "2026-10-17T14:03:01.120Z",
  "simulated_arrival_at": "2026-10-17T14:03:01.420Z",
  "fills": [
    {"price": "0.31838", "size": "1200", "liquidity": "TAKER", "fee": "0.4585", "at": "2026-10-17T14:03:01.431Z", "source": "book_walk"},
    {"price": "0.31842", "size": "370",  "liquidity": "MAKER", "fee": "0.0707", "at": "2026-10-17T14:03:09.002Z", "source": "trade_through"}
  ],
  "status": "FILLED",
  "book_snapshot_ref": "ws_recordings/2026-10-17/XLM-EUR.jsonl.gz#seq=88123401"
}
```

**VirtualTrade** (chiuso, tabella `virtual_trades`):

```json
{
  "trade_id": "01JAC1...ULID",
  "book": "champion",
  "product_id": "XLM-EUR",
  "entry_decision_id": "01JAB3...ULID",
  "entry": {"avg_price": "0.31839", "size": "1570", "fees": "0.5292", "at": "2026-10-17T14:03:09.002Z"},
  "bracket": {"tp": "0.33070", "sl": "0.31023", "k_tp": 3.0, "k_sl": 2.0},
  "exit": {"reason": "TP", "avg_price": "0.33071", "fees": "0.2597", "at": "2026-10-17T16:41:55.310Z"},
  "pnl": {"gross_quote": "19.3424", "fees_quote": "0.7889", "net_quote": "18.5535", "net_bps": 371, "r_multiple": 1.48},
  "held_min": 159,
  "flags": {"gap_resolved": false, "partial_entry": true},
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
    "champion": {"equity_start": "100.00", "equity_end": "101.84", "net_pnl_quote": "1.84", "fees_quote": "0.92",
                 "trades": 6, "win_rate": 0.50, "profit_factor": 1.61, "avg_r": 0.31, "max_dd_frac": 0.012},
    "random_entry": {"net_pnl_quote": "-0.77", "trades": 6, "profit_factor": 0.71},
    "buy_and_hold": {"net_pnl_quote": "0.40"}
  },
  "decisions": {"total": 4320, "BUY": 9, "SELL": 5, "HOLD": 4306,
                "skip_reasons": {"fee_floor": 31, "ev_negative": 12, "regime_veto": 7, "below_min_size": 0, "kill_switch": 0}},
  "calibration_7d": {"edge_ece": 0.061, "edge_brier": 0.231, "n_labels": 2870},
  "costs": {"median_spread_bps": {"XLM-EUR": 9}, "fee_maker_bps": 60, "fee_taker_bps": 120, "latency_ms_p75": 280},
  "ops": {"uptime": 0.9993, "ws_reconnects": 2, "decider_fallbacks": 0, "dry_run_violations": 0},
  "gate_progress": {"days": 3, "trades": 14, "status": "IN_PROGRESS"}
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
> 5. `modules/dryrun/virtual_account.py` — `VirtualAccount(book, starting_quote)`: saldi quote/base per asset, `reserve()`/`release()` per gli ordini aperti (un ordine shadow blocca il capitale come farebbe l'exchange), `apply_fill()`, `equity(mark_prices)`. Invariante testato con `hypothesis`: `quote + sum(base_i * price_i) + fees_pagate_cumulative == starting_quote + pnl_realizzato + pnl_non_realizzato` (tolleranza 1e-12).
> 6. `modules/dryrun/shadow_gateway.py` — `ShadowExecutionGateway(book, account, fill_model, ledger, clock)` che implementa `ExecutionGateway(Protocol)` della sez. 4.3:
>    - `submit_entry_with_bracket(intent)`: registra un `ShadowOrder`, prenota il capitale, programma l'arrivo a `now + latency_ms`; al primo evento di book successivo esegue `walk` per la parte marketable; il residuo resta resting e viene valutato su ogni `market_trade`; timeout con `entry_timeout_s`;
>    - al fill crea la posizione `LONG` nel `PositionLedger` dry (stesso schema della sez. 4.2) e un `VirtualBracket`;
>    - `submit_exit`, `emergency_exit`: walk dei bid;
>    - nessun `sleep` reale: tutto guidato dagli eventi di mercato e dal `Clock` iniettato (in replay e test il clock e' simulato).
> 7. `modules/dryrun/bracket_sim.py` — `BracketSimulator.on_trade(trade)` e `on_book(book)` per tutte le posizioni virtuali aperte del prodotto: regole TP/SL/"stesso evento -> SL"; gestione del gap di dati come in sez. 5.1.3 (download delle candele del gap via `RESTClient.get_candles`, lettura consentita).
> 8. `modules/dryrun/books.py` — `ShadowBookManager`: istanzia un `ShadowExecutionGateway` + `VirtualAccount` + ledger per ogni book in config; fa fan-out degli eventi di mercato verso tutti i book; per `random_entry` usa un `RandomDecider(seed)` che emette BUY con probabilita' pari alla frequenza giornaliera di BUY osservata nel champion (aggiornata ogni ora); per `buy_and_hold` un `BuyAndHoldDecider` che compra una volta sola. I decider dei book passano tutti dalla stessa `DecisionPolicy` e dallo stesso `RiskManager` (tranne `buy_and_hold`, che salta il RiskManager per costruzione).
> 9. `modules/dryrun/recorder.py` — `WsRecorder`: scrive ogni messaggio WS grezzo con timestamp di ricezione in `data/ws_recordings/YYYY-MM-DD/<channel>-<product>.jsonl.gz` (scrittura in un thread dedicato con buffer, flush ogni 5 s, rotazione giornaliera, retention). `modules/dryrun/replay.py` — `ReplayMarketStream` con la stessa interfaccia di `CoinbaseMarketStream`, che rilegge le registrazioni con clock simulato: consente di rieseguire offline e in modo deterministico una settimana di dry run con parametri diversi (what-if a costo zero). Test: lo stesso replay eseguito due volte produce ledger identici byte per byte.
> 10. `modules/dryrun/label_resolver.py` — task ogni 60 s: seleziona i `DecisionRecord` con `ts_bar_close + H <= now` non ancora risolti, carica le barre 1m dal Parquet locale, chiama `training.labeling.triple_barrier` e `forward_rv_bucket` (stesso codice del dataset storico), scrive `ResolvedLabel`. Idempotente (tabella `resolved` con `decision_id` PK).
> 11. `modules/dryrun/learner.py` — `NightlyLearner` con gli step L1–L5 della sez. 5.1.6, eseguito con `asyncio.create_subprocess_exec(sys.executable, "-m", "modules.dryrun.learner", ...)` e `os.nice(10)` nel figlio dove supportato, per non rallentare il loop di trading. Ogni step controlla la propria soglia minima e, in sotto-soglia, registra `skipped: insufficient_samples` invece di produrre artefatti.
> 12. `modules/dryrun/registry.py` — `ModelRegistry` su filesystem (`models/registry/`): `register(artifacts, manifest) -> version`, `get(alias)`, `promote(version, alias="champion")`, `rollback(alias)`. Gli alias sono un file `aliases.json` scritto in modo atomico (scrittura su file temporaneo + `os.replace`). Interfaccia astratta `ModelRegistry(Protocol)` per sostituire il backend in futuro (C8).
> 13. `modules/dryrun/report.py` — job alle 00:05 UTC e CLI `python -m modules.dryrun.report --days 7 [--book champion]`: genera il JSON della sez. 5.2 e un Markdown con tabelle per book, istogramma degli skip, reliability diagram di `edge` (PNG matplotlib), stato del gate G-DRY. Metriche: PnL netto, fee, win rate, profit factor, R medio, max drawdown, Sharpe giornaliero (indicativo, con nota sul campione), confronto con `random_entry` e `buy_and_hold`.
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
> Operativita' locale: `docker compose up chronos` avvia il bot in dry con i volumi `data/` e `models/` persistenti; `docker compose run --rm chronos python -m modules.dryrun.report --days 7` mostra lo stato del gate. Il container deve sopravvivere a un riavvio del PC (`restart: unless-stopped`) e ripristinare le posizioni virtuali dal DB dry all'avvio: una posizione virtuale aperta prima del riavvio viene risolta con la procedura del gap.

---

## Appendice A — Piano di rilascio

Perimetro: fase locale a costo zero (C8). L'infrastruttura cloud e il fine-tuning LLM su scala sono progettati a parte.

| Fase | Contenuto | Criterio di uscita |
|------|-----------|--------------------|
| P0 Fondamenta | Config pydantic, market stream WS + `LocalOrderBook` + barre + indicatori incrementali, recorder WS, ledger SQLite, backfill storico in Parquet. | Indicatori incrementali uguali al batch; 48 h di stream senza perdita di sequenza non gestita. |
| P1 Dati e baseline | Sez. 1 (sentiment live) + sez. 3 parte CPU: label triple-barrier, split purgati, `BaselineDecider` LightGBM, backtest con `BarFillModel`. | Backtest walk-forward con PnL netto > 0 su >= 3/5 fold; altrimenti si rivedono feature e orizzonte prima di andare avanti. |
| P2 Decision + Risk | Sez. 2 e sez. 4 completi, con `ShadowExecutionGateway` come unico gateway istanziabile. | Test e2e verdi; `test_dry_never_writes` verde. |
| P3 Dry run | Sez. 5: book paralleli, label resolver, learner notturno, report. Esecuzione continua in locale. | G-DRY superato (minimo 7 giorni, estensione automatica se servono piu' trade). |
| P4 Live-micro (R0) | Key con permesso Trade, `CHRONOS_LIVE_CONFIRM`, size minime, shadow twin + parity report. Primo ordine = validazione reale del payload con attached bracket. | Condizioni R0 della sez. 5.1.7. |
| P5 Rampa | Gradini R1, R2, ... | Condizioni per gradino; discesa automatica in caso di violazioni. |
| P6 (fuori scope) | Migrazione cloud, `chronos-decider` LLM fine-tuned su GPU, che entra come book `challenger` in dry prima di qualunque promozione. | Gate sez. 3.4 + G-DRY. |

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
