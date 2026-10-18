# CONFLITTO-sentiment - scelte e dubbi dell'implementazione di `modules/sentiment/` (sez. 1 della spec)

Autore: Sviluppatore (Sonnet). Data: 2026-10-18. Nessuno di questi punti blocca il lavoro: per ciascuno e' stata scelta la
lettura piu' letterale e prudente della spec, riportata in cima al file interessato. Servono una conferma o una correzione
dell'architetto prima che il modulo sia dichiarato chiuso. Non e' stato toccato `.claude/CONFLITTO.md`.

## S1 - Il modello ONNX: quale cryptobert, come esportarlo

- **Dubbio.** La spec dice `ElKulako/cryptobert` ma non fissa una revisione, ne' come esportare/quantizzare.
- **Scelta.** Revisione pinnata `9e37c910fe87727cb842a9ac55c6388256fe0f15` (ultima del repo al 2026-10-18), export con
  `optimum` (`ORTModelForSequenceClassification`, `export=True`) e quantizzazione **dinamica int8, profilo AVX2, non per
  canale** (`modules/sentiment/export_model.py`, riproducibile: `python -m modules.sentiment.export_model --revision <sha>`).
  Versione del model card = data dell'export (`cryptobert-onnx-int8@2026-10-18`). `label_map` derivata dal `id2label` del
  config (0 Bearish, 1 Neutral, 2 Bullish -> identita'); l'export rifiuta nomi di classe che non conosce invece di indovinare.
  Card: `T = 1.0`, `calibrated: false` (sez. 1.1.2).
- **Misurato.** Il modello int8 e' piu' veloce del requisito (circa 5 ms a titolo, spec: p50 < 30 ms) ma NON coincide con il
  fp32: su 6 titoli di prova 1 argmax differisce (scarto massimo sui logit 1.69). E' il costo noto della quantizzazione
  dinamica. Alternative: quantizzazione per canale (`per_channel=True`), AVX512-VNNI, oppure restare in fp32 (4x piu' grande).
  Va misurato l'accordo int8/fp32 su un campione vero (le 1.200 etichette di `label_review`) prima di calibrare.
- **Non verificabile qui.** Il dominio di cryptobert sono i post social; i titoli restano fuori distribuzione (e' il motivo
  per cui la spec prevede la calibrazione).

## S2 - `MARKET` solo per le news senza asset

- **Dubbio.** La spec (1.1 fase 3) dice che "le news senza asset ma con keyword macro" sono `MARKET`. Letto alla lettera, una
  news su BTC non contribuisce all'aggregato `market`, che quindi misura solo il flusso macro/regolatorio non attribuibile.
- **Scelta.** Lettura letterale: `assets` pieno -> chiavi = gli asset; `assets` vuoto + keyword -> `MARKET`.
- **Alternativa.** Far contribuire a `MARKET` anche le news di asset (con `relevance` 0.4). Cambia il significato della
  feature `market.*` per il `BaselineDecider`: va deciso prima di raccogliere dati per il training, perche' ricostruire gli
  eventi richiede di riclassificare.

## S3 - `source_count`: quando cresce e come pesa

- **Dubbio.** La spec incrementa `source_count` "su duplicato" senza distinguere chi lo emette; il peso usa
  `log(1 + source_count)` ma `sentiment_events` non ha la colonna, e il valore cresce dopo la classificazione.
- **Scelta.** (a) Un duplicato della STESSA sorgente (il feed RSS ripollato) e' scartato in silenzio; solo la ripubblicazione
  da un'ALTRA sorgente incrementa `source_count` (altrimenti misurerebbe quanto a lungo la news resta nel feed).
  (b) L'aggregatore conta `base_count + ripubblicazioni con ingested_at <= as_of`: il peso a `as_of` non usa conteggi del
  futuro (C7). Ricaricando da SQLite si usa il `source_count` finale, esatto per l'uso live (tutte le ripubblicazioni sono
  nel passato di `now`) ma non per una ricostruzione storica da DB.
- **Limite noto.** Il ricordo di "quali sorgenti hanno gia' riportato la storia" e' solo in memoria (cache 48 h): dopo un
  riavvio, una ripubblicazione gia' contata puo' essere contata di nuovo. Soluzione completa = una tabella
  `news_sightings(news_id, source, seen_at)`, fuori dal DDL della spec: non aggiunta.

## S4 - Timestamp: larghezza fissa a millisecondi

La spec mostra sia `13:58:41Z` sia `14:00:12.384Z`. Tutti i timestamp serializzano `YYYY-MM-DDTHH:MM:SS.mmmZ` (UTC, sempre 3
decimali, troncati al millisecondo): cosi' le colonne TEXT di SQLite ordinano come il tempo e `ingested_at <= ?` e' esatto,
e serializzare -> parsare e' l'identita'. Le firme dei modelli non cambiano.

## S5 - Dettagli che la spec lascia aperti

- `SentimentEvent.classified_at` e `latency_ms` non hanno colonna nel DDL: sono `None` negli eventi ricaricati da SQLite.
  `latency_ms` = tempo del batch diviso per il numero di titoli del batch.
- `SentimentAggregator.snapshot(as_of, asset_key, fear_greed=None)`: l'argomento `asset_key` e' in piu' rispetto alla firma
  della spec, che non puo' dire di quale asset si tratti.
- `SentimentService.snapshot` ritorna sempre un `SentimentSnapshot`; la scelta di passare `sentiment: null` al modello in
  avvio a freddo o in outage (sez. 2.2.1) spetta allo `StateBuilder` (finestra vuota = `n_24h == 0` e `last_age_min is None`).
- `AssetTagger` riceve gli alias del config (`AssetAliases`: `names` + `tickers`), non `dict[str, list[str]]`: nomi e ticker
  hanno regole di maiuscole diverse (sez. 1.1). Un asset dichiarato dalla sorgente (CryptoPanic `currencies`) ma assente dal
  testo ha `relevance` 0.6.
- `age_h` del Fear & Greed e' calcolato dall'istante a cui il valore si riferisce; il valore e' visibile dal momento in cui
  e' stato letto (`fetched_at <= as_of`, C7). Le letture non sono persistite (nessuna tabella nella spec): lo storico completo
  si riscarica da `https://api.alternative.me/fng/?limit=0`.
- Bitcoin Magazine ("Solo BTC", tabella 1.1.1) non e' nel config di default della spec e `SourceConfig` non ha un campo per
  restringere gli asset di una sorgente: non implementata.
- Backoff: `tenacity.wait_exponential(min=2, max=120)` produce 2, 2, 4, 8 ... (il primo gradino e' il pavimento di 2 s).
- Avvio a freddo: al primo avvio i feed contengono le ultime ~30 notizie, tutte con `ingested_at = adesso` (e' l'istante in cui
  le conosciamo). Entrano nell'aggregato come se fossero fresche. Alternativa: ignorarle se `published_at` e' piu' vecchio di N
  minuti; non fatto perche' la spec non lo prevede.

## S6 - CryptoPanic

URL e versione dell'API del piano free non verificabili offline ne' senza token (la pagina dei developer e' renderizzata via
JS; la spec chiede di verificare "al momento dell'implementazione"). Default `https://cryptopanic.com/api/developer/v2/posts/`
con `auth_token`, `public=true`, `kind=news`, sovrascrivibile con `url` nel config. Senza `CRYPTOPANIC_TOKEN` la sorgente e'
disabilitata (non schedulata). Da verificare con un token reale: parsing (`results[].title/published_at/url/currencies[].code`)
coperto solo da fixture. Il token non finisce mai nei log (si logga il tipo di errore e lo status, non l'URL).

## S7 - Dipendenze e file fuori perimetro

- `torch` serve solo per l'export del modello (non per il runtime del classificatore); come gia' scritto in `requirements.txt`
  resta installato a parte.
- `models/sentiment/` (circa 125 MB di ONNX) NON e' ignorato da `.gitignore` (che ignora `model_checkpoints/`, `*.pt`, `*.bin`).
  Non e' stato modificato per restare nel perimetro: da aggiungere `models/sentiment/` prima di un commit.
- Il glob `tests/test_sentiment_*.py` include anche il test v1 `tests/test_sentiment_analysis.py` (importa `bs4`, non installato;
  escluso da `conftest.collect_ignore` e da `extend-exclude` di ruff solo quando non e' passato esplicitamente): usare
  `pytest $(ls tests/test_sentiment_*.py | grep -v analysis)` finche' il v1 non viene eliminato (spec sez. 0.5).
