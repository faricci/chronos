# STUDIO A EVENTI: le notizie crypto muovono BTC/ETH abbastanza da battere i costi?

Autore: Sviluppatore (Sonnet). Data: 2026-10-20. Tipo: ricerca offline, NESSUNA modifica ai moduli del sistema. Codice: `training/news_event_study.py`, test `tests/test_news_event_study.py`. Report con tutte le tabelle: `data/reports/news_event_study.md` (+ `.csv`, `_counts.csv`; la cartella `data/reports/` e' ignorata da git).

Stato di questo documento: sezioni 1-4 (dati, fuso orario, prezzi, PRE-REGISTRAZIONE) scritte PRIMA di calcolare un solo rendimento per classe; la sezione 5 (risultati) e' stata aggiunta dopo la corsa. Versione del tagger congelata: `TAGGER_VERSION = 1538d136f3d6` (sha256 delle regole; il report la ristampa: se non coincide, le regole sono state cambiate dopo la pre-registrazione).

## Domanda

L'utente pensa che notizie come "il Bitcoin e' diventato legale in uno stato" o "adottato da..." facciano salire il prezzo, e che un modello che legge le news (anche un LLM) possa dare un segnale subito, senza mesi di raccolta. Si puo' verificare ora su titoli storici con data e ora. Vincoli del progetto invariati: solo spot, long-only, nessuna leva, costi coperti. Il sistema decide a t + 2 min e entra dopo: l'ingresso ottimistico assunto e' a 5-10 minuti dalla pubblicazione. Un round trip costa circa 77 bps (fee reali maker 25 / taker 50 bps, slippage 5, extra stop 10: `data/datasets/label_config.json`, `modules/risk/costs.py`).

## 1. Dataset

| | DLT-Sentiment-News | CoinDesk / CryptoCompare |
|---|---|---|
| URL | https://huggingface.co/datasets/ExponentialScience/DLT-Sentiment-News (file `data/train-00000-of-00001.parquet`) | https://huggingface.co/datasets/maryamfakhari/crypto-news-coindesk-2020-2025 (file `coindesk-crypto-news-2020-2025.csv`) |
| Origine | Post di CryptoPanic (aggregatore): titolo, descrizione, voti della comunita', URL della fonte | Notizie di CoinDesk Data via API CryptoCompare, molte testate (coinotag, coinpedia, newsbtc, cointurken, coingape, ambcrypto, cointelegraph, dailyhodl, decrypt...) |
| Licenza | `cc-by-nc-4.0` (dichiarata nella scheda e nei metadati) | metadati `cc-by-nc-4.0`; il README aggiunge "condiviso con permesso per uso accademico e di ricerca, non per ridistribuzione commerciale"; il contenuto e' di CoinDesk Data. Licenza di fatto non chiara per i testi originali |
| Uso qui | Solo ricerca interna, nessuna ridistribuzione, nessun uso commerciale; i file stanno in `data/external/` (ignorata da git, e `data/` e' gia' nel `.dockerignore`) | Idem |
| Periodo effettivo | 2021-01-01 .. 2025-05-23 | 2019-10-29 .. 2025-02-01 (il README dice 2020-2025) |
| Righe | 23.301 | 229.172 |
| Titoli unici (dopo normalizzazione) | 23.291 (10 duplicati; 240 URL ripetuti con titolo diverso) | 226.795 (2.377 duplicati, 1,0%; 172 URL ripetuti) |
| Distribuzione | 2021: 7.016, 2022: 8.490, 2023: 3.121, 2024: 3.451, 2025: 1.223 | molto sbilanciata verso il recente: 2019: 1.637, 2020: 10.677, 2021: 14.001, 2022: 26.050, 2023: 47.770, 2024: 124.469, 2025: 4.568 |
| Fuso orario | UTC (verificato, vedi sez. 2) | ora locale iraniana (Asia/Tehran, UTC+3:30, con ora legale fino al 2022), NON UTC. Corretta nel caricamento |
| Note di qualita' | Titoli con caratteri sostituiti da `�` (apostrofi); le date hanno i secondi | Idem; `upvotes`/`downvotes` sono tutti 0 (inutili); alcune testate (coinotag, coinpedia, cointurken) pubblicano molti articoli di bassa qualita'; il campione cresce di 5x nel 2024 |

Duplicati e titoli ripubblicati: chiave = titolo normalizzato (minuscolo, senza punteggiatura/apostrofi); si tiene la prima apparizione (`dedupe_titles`). Non si fa deduplicazione "fuzzy": la stessa storia con titoli diversi di testate diverse resta piu' volte, ed e' proprio per questo che gli eventi si raggruppano per giorno (sez. 4). Non provato: CryptoVision su Mendeley Data (richiede scaricamento manuale dal browser); i due dataset bastano per una replica indipendente (CryptoPanic contro CryptoCompare).

Cosa NON sono i dati: DLT contiene i post che la comunita' di CryptoPanic ha votato, quindi e' un campione selezionato per attenzione; CoinDesk/CryptoCompare e' un flusso quasi completo ma con peso enorme del 2024. I titoli di fonte secondaria arrivano dopo la fonte primaria (il ritardo e' un fatto del dataset, vedi i test "prima della pubblicazione").

## 2. Verifica del fuso orario (prima di ogni rendimento)

Prezzi: candele a 5 minuti di Coinbase BTC-USD, orario Unix (UTC) dall'API. Controlli:

1. Eventi noti, DLT (timestamp letto come UTC):
   - Bancarotta FTX, 11-nov-2022: il prezzo crolla alle 14:05-14:25 UTC (apertura 5 min: -73, +37, -104, -173, -97 bps); i primi titoli DLT sono alle 14:23 ("FTX CEO Bankman-Fried Resigns, Bankruptcy Proceedings Begin"), 14:26 ("FTX Filing for Chapter 11") e 14:36. Coerente: il titolo arriva 10-20 minuti DOPO l'inizio del movimento.
   - Falso tweet della SEC, 9-gen-2024: +243 bps alle 21:15 e -279 bps alle 21:20 UTC; primo titolo DLT sul chiarimento (account hackerato) alle 21:30.
   - Approvazione ETF spot, 10-gen-2024: le barre a 5 min mostrano -145 bps alle 20:15 e +94 bps alle 21:00; DLT "SEC approves 11 Bitcoin spot ETFs" 20:56, "BITCOIN SPOT ETF APPROVED" 21:18. Il movimento dell'evento e' rumoroso (la notizia era attesa da settimane e c'erano state voci e smentite), ma nessuna barra e' sfasata di ore.
   - Binance / FTX, 8-nov-2022: massimo rialzo a 15-16 UTC (+399, +347 bps su 15 min), DLT "Binance intends to fully acquire FTX" alle 16:28.
2. CoinDesk/CryptoCompare letto come UTC e' SFASATO: stesso evento FTX 11-nov-2022 alle 17:54-18:02, cioe' 3,5 ore dopo il DLT. Il confronto sistematico sui 4.341 titoli normalizzati presenti in ENTRAMBI i dataset (stessa notizia, stessa testata) da' una differenza `cc - dlt` di esattamente 3,5 ore in inverno e 4,5 ore in estate nel 2021-2022, e sempre 3,5 ore dal 2023: il profilo di Asia/Tehran (UTC+3:30, ora legale abolita nel 2022). Dopo la conversione (`COINDESK_LOCAL_TZ`, testata in `test_load_coindesk_reads_the_stamps_as_iran_local_time`) la differenza e' 0,0 ore per tutti i 4.341 titoli, in ogni trimestre 2021Q1-2025Q1 (quantili 5/25/50/75/95 = 0). L'evento FTX del CoinDesk cade ora a 14:24-14:32 UTC. Senza la correzione tutti i risultati su CoinDesk sarebbero stati falsati.
3. Limite: la correzione del CoinDesk e' misurata sul periodo 2021-2025 (dove c'e' l'intersezione col DLT); per 2019-2020 si assume la stessa regola (stessa pipeline); gli orari ambigui del cambio d'ora (un'ora l'anno) sono scartati.
4. Il profilo "tasso di titoli attorno alle 250 maggiori variazioni orarie assolute di BTC" (`move_triggered_news_profile`) e' calcolato ed e' stampato nel report, ma e' poco informativo (picco poco marcato): non e' usato come criterio, solo come diagnostica.

## 3. Prezzi

`https://api.coinbase.com/api/v3/brokerage/market/products/{BTC-USD,ETH-USD}/candles`, granularita' FIVE_MINUTE, 350 candele per richiesta, 4 richieste/s al massimo, sola lettura, nessuna chiave. File: `data/external/prices/BTC-USD_5m.parquet` e `ETH-USD_5m.parquet` (2019-10-01 .. 2025-05-31, 595.923 e 595.945 barre; mancano 237 barre di BTC su 596.160). NON si e' usato `data/bars/` (appartiene al soak/dry run) ne' lo store da 2023-10: stessa fonte e stessa granularita' per tutto il periodo. Si usa USD e non EUR perche' i dataset partono dal 2019/2021 e BTC-EUR/ETH-EUR non coprono il periodo con la stessa continuita'; il rendimento in USD e in EUR differisce per il cambio EUR/USD, che e' lo stesso per eventi e controllo. Prezzo di un istante = APERTURA della barra che inizia in quell'istante. Una barra mancante da' NaN (mai riempita) e l'evento esce da quella finestra.

## 4. PRE-REGISTRAZIONE (congelata prima dei risultati)

### 4.1 Classi (funzione pura `tag_title`, solo sul titolo)

Precedenza: `hack_exploit` > `regolazione_negativa` > `regolazione_positiva` > `adozione_istituzionale` > `altro`. Titolo normalizzato (minuscolo, possessivi e apostrofi tolti, resto in spazi).

- Esclusione per cautela (`HEDGE`): se il titolo contiene un punto interrogativo o parole di ipotesi/attesa/smentita (fake, false, rumor, denies, could, may, might, would, can, will, set to, expected, plans, seeks, wants, urges, calls for, proposes, considers, explores, hopes, predicts, analysts, if, whether, when, why, how, ahead of, pending, awaits, speculation, potential, possible, reportedly, alleged, claims, anniversary, introduces, pushes for, campaign, likely, poised, warns, threatens, option, to be, fears, risks, talks, doubt, must, pledges, signals, according to, speculate...), la classe e' `altro`. Le regole cercano eventi ACCADUTI, non attese.
- `hack_exploit`: parola di attacco (hack, exploit, stolen, steals, drain, rug pull, security breach, heist, theft, compromised) E un importo (`$cifra`, million, billion, mln, bn) E nessuna parola di strascico (recover, refund, arrest, sentenced, lawsuit, settle, report, total, etf, outflow, record, year...).
- `regolazione_negativa`: (ban / prohibit / outlaw, senza "lifts ban") oppure (verbo di enforcement: sues, charges, fines, subpoena, indict, sanction, raid, crackdown, reject, delay, block, halt, suspend, restrict, freeze, seize) con un attore regolatore (SEC, CFTC, DOJ, regulator, government, court, Cina, India, Russia, ...); in entrambi i casi serve una parola crypto e nessuna negazione (without, drops, dismiss, closes, ends, withdraws, lifts, wins, overturn, clears, settles, against, fails...).
- `regolazione_positiva`: legalizza (legalize...), "legal tender" con verbo (adopts, makes, passes, signs, approves), approva/autorizza/green light/clears con attore regolatore E parola crypto, "gets/receives/secures ... approval/license", passa/firma legge/bill/framework crypto, riserva strategica di bitcoin creata o firmata, "lifts ban".
- `adozione_istituzionale`: istituzione nominata (BlackRock, Fidelity, Vanguard, JPMorgan, Goldman, Morgan Stanley, PayPal, Visa, Mastercard, Tesla, MicroStrategy/Strategy, Nasdaq, CME, Cboe, Robinhood, Revolut, banche e gestori maggiori, fondi pensione...) E verbo di adozione (buys, adds, accepts, launches, offers, integrates, lists, invests, files for...) E parola crypto, senza parole di chiusura (ends, halts, stops, drops, closes, delists, cuts).
- `altro`: tutto il resto; e' anche la base dei controlli.
- Controllo: classificatore sentiment locale (`modules/sentiment/classifier.py`, `models/sentiment/cryptobert-onnx-int8`, `T=1`, non calibrato, solo titolo): punteggio `p_bull - p_bear`; `sent_bull` = decile superiore e `sent_bear` = decile inferiore della distribuzione del dataset stesso (il modello e' sbilanciato verso rialzo: soglie fisse darebbero classi sbilanciate).

Qualita' del tagger (da leggere a occhio su 20-22 titoli per classe, vedi sez. 5): precisione dichiarata approssimativa, non misurata su un campione etichettato; il recall e' sconosciuto. Il tagger e' volutamente semplice.

### 4.2 Eventi indipendenti

Titoli duplicati (stesso titolo normalizzato) contano una volta. Gli eventi della stessa classe nello stesso giorno UTC sono UN evento (si tiene il titolo piu' precoce del giorno: `cluster_events`). Si riportano anche gli eventi "indipendenti a 3 giorni" (filtro greedy: almeno 3 giorni dall'ultimo tenuto), il numero conservativo per l'orizzonte piu' lungo.

### 4.3 Finestre (rendimenti semplici in bps)

`a` = apertura della barra a 5 min che inizia a o prima della pubblicazione; `E` = prima barra che inizia a o dopo pubblicazione + 5 min (l'ingresso piu' rapido possibile, tra 5 e 10 minuti dopo; il sistema reale e' piu' lento).
- Finestre PRIMARIE (operabili, long dal momento E): `post_1h`, `post_6h`, `post_24h`, `post_3d` = apertura(E + h) / apertura(E) - 1.
- Diagnostiche: `react` = apertura(E) / apertura(a) - 1 (cio' che e' gia' successo prima di poter entrare); `pre_1h`, `pre_6h`, `pre_24h` = apertura(a) / apertura(a - k) - 1 (test "buy the rumor / sell the news": nessun dato dopo la pubblicazione entra in queste finestre; e' coperto da un test).
- Costo: 77 bps per round trip. `net = media - 77`. Il confronto con il controllo e' al lordo (il costo e' uguale per entrambi e si elimina).

### 4.4 Controllo

Istanti di pubblicazione di titoli `altro` dello STESSO dataset, abbinati per (trimestre di calendario, giorno della settimana, ora UTC); se lo strato e' vuoto si ricade su (giorno della settimana, ora). Il trimestre in piu' rispetto alla richiesta serve a non confondere gli eventi con il regime di mercato. Il controllo e' dunque "una notizia qualsiasi alla stessa ora", non un istante a caso del mercato: toglie l'effetto "le notizie escono quando il mercato si muove".
- 2.000 estrazioni (un controllo per evento ad ogni estrazione): distribuzione della media di controllo, intervallo 95%, `p_ctrl`.
- `excess_i = r_i - media dei controlli dello strato`; bootstrap a blocchi (blocco = settimana UTC, 5.000 ricampionamenti) per intervallo 95% e `p_boot` bilaterale dell'eccesso medio = 0. Il `p_boot` e' il p primario (gestisce la correlazione fra eventi vicini); `p_ctrl` e' un controllo di coerenza.
- Intervallo 95% anche per il rendimento lordo medio, da cui l'intervallo del netto.

### 4.5 Confronti multipli e potenza

Famiglie (correzione Holm per il tasso di errore familiare, Benjamini-Hochberg per il FDR, sul `p_boot`): `primary` = classi regola x 4 finestre primarie x 2 asset x 2 dataset (fino a 64 test); `sentiment` = le 2 classi sentiment (fino a 32); `diagnostic` = finestre `react` e `pre_*` (fino a 96 + 48). Si riportano TUTTE le combinazioni: nessuna selezione. `altro` non e' una classe testata.
Potenza: `MDE = (z_(alpha/2) + z_(0,8)) x SE_bootstrap` dell'eccesso, con alpha 5% e con alpha/m (Bonferroni, m = dimensione della famiglia): l'effetto vero minimo rilevabile con potenza 80%.

### 4.6 Regola di decisione (fissata ora)

- "L'effetto esiste": `p_Holm < 0,05` nella famiglia primaria in entrambi i dataset con segno concorde, oppure in uno con `p_Holm < 0,05` e nell'altro `p_boot < 0,05` con lo stesso segno. Un solo p non corretto basso non conta.
- "E' operabile": in piu' il limite inferiore del 95% del rendimento lordo medio e' sopra 77 bps (netto > 0) nella finestra operabile. Le classi negative (hack, regolazione negativa) in un sistema solo long possono servire al piu' come veto (evitare un acquisto), mai come ingresso: per esse conta solo l'eccesso negativo e non c'e' un netto da guadagnare.
- Raccomandazione: se nessuna classe soddisfa "operabile", si chiude l'idea news-driven come fonte di ingresso; il sentiment resta eventualmente come feature/veto, e questo non richiede altro lavoro ora.

Limiti noti gia' ora (scritti prima di vedere i dati): 1) il tagger e' a regole sul titolo, con precisione imperfetta; 2) i titoli di fonte secondaria seguono la fonte primaria; 3) BTC-USD non e' BTC-EUR; 4) ingresso a 5-10 minuti e' ottimistico rispetto a t + 2 min + coda di esecuzione maker; 5) il numero di eventi indipendenti per classe e' piccolo (decine per le regolazioni positive), quindi la potenza e' bassa per effetti piccoli.

## 5. Risultati

Corsa: `python -m training.news_event_study run` (25 s, seed fisso, riproducibile: una seconda corsa da' gli stessi numeri alla cifra). Versione del tagger stampata nel report: `1538d136f3d6`, uguale a quella pre-registrata. Tabelle complete (256 righe: 64 primarie, 32 sentiment, 96 diagnostiche, 64 di validazione) in `data/reports/news_event_study.md`.

### 5.1 Quanti eventi

Eventi = giorni UTC distinti per classe; tra parentesi gli eventi distanziati di almeno 3 giorni (il numero conservativo per l'orizzonte di 3 giorni).

| Classe | DLT (CryptoPanic) | CoinDesk / CryptoCompare |
|---|--:|--:|
| regolazione_positiva | 96 titoli, 70 giorni (51) | 721 titoli, 292 giorni (156) |
| regolazione_negativa | 118, 98 (65) | 1.067, 498 (228) |
| adozione_istituzionale | 226, 175 (123) | 2.737, 778 (327) |
| hack_exploit | 158, 115 (85) | 1.312, 597 (279) |
| sentiment alto / basso (controllo) | 2.330 titoli ciascuno, 973 / 1.025 giorni | 22.679 ciascuno, 1.856 / 1.784 giorni |

La precisione del tagger, letta a occhio su circa 22 titoli estratti a caso per classe e dataset (non misurata su un campione etichettato): hack circa 90%, regolazione positiva 70-75% (errori: "Senate approves mining moratorium", "SEC must approve", commenti), regolazione negativa 60-65% (include i rinvii della SEC, sequestri di polizia), adozione istituzionale 55-65% (include "files for ETF", integrazioni tecniche). Il recall e' sconosciuto. Gli eventi "giusti" sono quindi meno di quelli contati, il che rende un eventuale effetto vero ancora piu' piccolo da rilevare, non piu' grande.

### 5.2 Rendimento medio di BTC e ETH dopo la notizia, al netto di 77 bps

Famiglia primaria: 64 test (2 dataset x 2 asset x 4 classi x 4 orizzonti). Rendimento lordo medio dal momento di ingresso (5-10 min dopo la pubblicazione):

- Orizzonte 1 h e 6 h: la media lorda e' tra -40 e +13 bps in tutte le classi e tutti i dataset (BTC e ETH), contro 77 bps di costo: netto tra -117 e -64 bps; nessun limite, ne' inferiore ne' superiore, dell'intervallo al 95% del netto e' sopra zero a questi orizzonti.
- Orizzonte 24 h e 3 giorni: le medie lorde diventano positive (BTC CoinDesk: +23 bps a 24 h e +67 bps a 3 giorni per l'adozione istituzionale; hack +79 bps a 3 giorni), ma il controllo (notizie qualsiasi alla stessa ora e nello stesso trimestre) ha lo stesso rendimento (+22 e +62 bps): e' la deriva del mercato, non la notizia. L'eccesso e' +1 bps [-21, +23] a 24 h e +5 bps [-47, +56] a 3 giorni.
- 64 test: 1 solo con p < 0,05 non corretto (ne sarebbero attesi circa 3 per caso); p minimo 0,048; dopo Holm il minimo e' 1,00 e dopo Benjamini-Hochberg 0,66. Eccesso negativo in 35 test su 64. Nessuna combinazione ha il limite INFERIORE del netto sopra zero: il massimo e' -49 bps (il limite superiore del netto supera zero solo a 24 h e 3 giorni, dove gli intervalli sono larghissimi: massimo +132 bps).
- Regolazione positiva (l'idea dell'utente), BTC: eccesso a 1 h -15,7 bps [-33, +2] (DLT, 70 eventi) e -4,4 bps [-10, +1] (CoinDesk, 292 eventi); a 6 h -29,6 [-61, +3] e -8,0 [-24, +9]; a 24 h +16 [-65, +101] e -9 [-40, +22]; a 3 giorni -14 [-145, +115] e -31 [-85, +26]. Il segno e' negativo nei due dataset a 1-6 h (coerente con "sell the news"), ma nessuno e' significativo (p tra 0,08 e 0,36 a 1-6 h). ETH idem (CoinDesk: -4, -10, +14, +9 bps).
- Controllo di sentiment (classificatore locale, decile alto e basso, 32 test): 0 test con p < 0,05, p minimo 0,14, eccessi di pochi bps (massimo +22 bps a 3 giorni, intervallo [-25, +68]); il segno dell'eccesso e' quello atteso dal sentiment in 20 test su 32 (62%), cioe' compatibile col caso. Il modello di sentiment locale non da' alcun segnale sul rendimento successivo a questo orizzonte.

### 5.3 Controlli e potenza statistica

- Controllo: 2.000 estrazioni per test, strato (trimestre, giorno della settimana, ora). La media di controllo a 1 h e' entro +/-4 bps, a 24 h +8..+22 bps, a 3 giorni +30..+65 bps (CoinDesk): la deriva di mercato a 3 giorni e' piu' grande di qualsiasi effetto delle classi.
- Potenza (MDE a 80%, alpha 5%; tra parentesi alpha corretto Bonferroni sul numero di test della famiglia), eccesso minimo vero rilevabile:
  - CoinDesk, BTC, regolazione positiva (292 eventi): 8 (12) bps a 1 h, 24 (36) a 6 h, 44 (66) a 24 h, 78 (117) a 3 giorni. Adozione istituzionale (778 eventi): 6 (10), 15 (22), 32 (48), 73 (110).
  - DLT, BTC, regolazione positiva (70 eventi): 26 (38), 46 (69), 120 (179), 186 (278) bps. Per DLT, a 24 h e 3 giorni la potenza NON basta per escludere un effetto di 77 bps (limite superiore dell'eccesso +101 e +115 bps).
  - Con 292 eventi CoinDesk il limite superiore al 95% dell'eccesso della regolazione positiva su BTC e' +1, +9, +22, +26 bps a 1 h, 6 h, 24 h, 3 giorni: un effetto vero di 77 bps (il costo) e' escluso a tutti gli orizzonti; su ETH a 3 giorni (limite +85 bps) non e' escluso.
  - Il costo di 77 bps e' dunque circa 10 volte la sensibilita' a 1 h e circa 2 volte a 24 h su CoinDesk; a 3 giorni la sensibilita' (75-110 bps) e' paragonabile al costo, quindi su quell'orizzonte il risultato e' "non rilevabile", non "assente".

### 5.4 "Buy the rumor, sell the news": il prezzo si e' gia' mosso prima?

- Per le classi dell'idea dell'utente il prezzo non si e' mosso ne' prima ne' subito dopo il titolo: `pre_1h` della regolazione positiva su BTC = +0,7 bps (DLT) e +1,5 bps (CoinDesk), eccesso +3,9 [-19, +27] e -0,1 [-7, +7]; `pre_6h` e `pre_24h` eccessi tra -41 e +9 bps con intervalli larghi attorno a zero; `react` (dalla pubblicazione all'ingresso) eccesso +8,0 bps [-0,7, +17,5] (DLT) e +1,5 bps [-0,8, +3,9] (CoinDesk). Sulle 96 finestre diagnostiche 4 hanno p < 0,05 (attesi 4,8), nessuna sopravvive alla correzione. Non si osserva alcun "gia' incorporato prima": non c'e' nulla da incorporare.
- Validazione del metodo (post-hoc, fuori dalla pre-registrazione, etichettata come tale): i titoli che RACCONTANO un movimento di prezzo ("Bitcoin plunges...", "Ether surges...", `tag_price_move`) vengono dopo il movimento. DLT, BTC, "move_down" (252 giorni): rendimento nelle 24 h PRIMA del titolo -191 bps (eccesso -147 bps [-188, -107]), 6 h prima -99 bps, 1 h prima -30 bps; subito dopo il titolo (react) +2 bps, e nelle ore seguenti nulla (post_1h +5, post_6h +7, post_24h +25 bps, tutti non significativi). "move_up": +107 bps nelle 24 h prima, +33 bps nelle 6 h prima, poi nulla (post_24h -13 bps). Stesso quadro su CoinDesk e su ETH. Tre conclusioni: (1) il fuso orario e' giusto (il movimento precede il titolo, non lo segue di ore); (2) il metodo vede effetti di pochi bps quando ci sono; (3) il flusso di notizie reagisce al prezzo, non lo anticipa, e dopo il titolo il prezzo non continua (anzi, dopo un titolo "crolla" il prezzo recupera in media +18..+64 bps a 24 h, ma non piu' del controllo in modo significativo).

### 5.5 Sintesi onesta

1. L'effetto esiste? Nei dati disponibili NO per nessuna delle quattro classi, ne' per BTC ne' per ETH, ne' nei dati CryptoPanic ne' in quelli CryptoCompare. Non e' una questione di correzione per confronti multipli: persino senza correzione c'e' 1 test su 64 con p < 0,05, meno dei circa 3 attesi per caso.
2. E' abbastanza grande da superare 77 bps dopo il ritardo? No. Il massimo limite inferiore del netto su 64 test e' -49 bps; a 1-24 h con CoinDesk l'eccesso su BTC e' escluso oltre circa +28 bps (ETH a 24 h oltre +53 bps); la media lorda a 3 giorni di +34..+83 bps (CoinDesk) esiste anche per notizie qualsiasi ed e' deriva del mercato (e comunque costerebbe gli stessi 77 bps). Il ritardo di 5-10 minuti non e' la causa: il movimento tra pubblicazione e ingresso (`react`) e' tra -3 e +11 bps in tutte le classi; anche entrando alla pubblicazione senza ritardo non ci sarebbe niente da guadagnare.
3. Quanti eventi? Vedi 5.1: da 70 a 778 eventi-giorno per classe e dataset (51-327 indipendenti a 3 giorni). Abbastanza per escludere effetti dell'ordine di 20-30 bps a 1-24 h su CoinDesk, non abbastanza per escludere 77-100 bps a 3 giorni (MDE 73-117) ne' per DLT (70 eventi, MDE 120-190 bps oltre le 24 h).
4. Cosa NON si puo' concludere: (a) che le notizie non contino mai: si sono testati titoli, non il contenuto, con un tagger a regole di precisione 55-90%; (b) che un evento raro e clamoroso (approvazione dell'ETF spot del 10-gen-2024, FTX) non muova il prezzo: sono pochissimi eventi, vedi il fuso orario; la media di centinaia di titoli "regolazione positiva" non e' quell'evento; (c) niente su altre monete o su orizzonti oltre 3 giorni; (d) niente sull'EUR in senso stretto (BTC-USD); (e) niente su fonti piu' veloci dei titoli (feed istituzionali, tweet, flusso di ordini): qui il titolo e' sempre in ritardo sul prezzo; (f) il periodo e' 2019-2025, in gran parte 2022-2024.
5. Raccomandazione: CHIUDERE l'idea news-driven come fonte di segnale di ingresso. Nessuna classe regolatoria/istituzionale/hack, ne' il sentiment del nostro classificatore locale, ha un eccesso rilevabile o vicino a 77 bps, e i titoli arrivano dopo il movimento di prezzo. Non costruire un modello (ne' un LLM) per leggere i titoli come segnale di ingresso: il dato mostra che non c'e' un segnale da leggere. Questo studio non giustifica altro lavoro sul sentiment come fonte di ingresso (il modulo della spec sez. 1 esiste gia'); se in futuro servisse, l'unico uso plausibile e' come veto/contesto di rischio, da valutare con i dati reali raccolti dal sistema e non da questi titoli.
6. Costo di questa conclusione per l'utente: la domanda "e' diventato legale in uno stato, il prezzo sale?" ha una risposta empirica: sui titoli di regolazione positiva il rendimento a 1-6 ore e' leggermente negativo rispetto al flusso normale di notizie (non significativo), e a 24 ore e 3 giorni non differisce dal controllo.

Limiti metodologici residui: tagger a regole (precisione e recall non misurati con etichette), ingresso a 5-10 minuti (ottimistico), BTC-USD invece di EUR, nessuna deduplicazione semantica fra testate (mitigata dal raggruppamento per giorno), bootstrap a blocchi settimanali (la correlazione fra eventi lontani nello stesso regime non e' modellata oltre la settimana), famiglia di test grande ma tutti i risultati sono riportati.
