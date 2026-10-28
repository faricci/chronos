# CONFLITTO_CD: C e D sotto il minimo di 150 unita' in training (per il trader-quant)

Stato: conteggio dei soli SEGNALI (nessun rendimento, nessuna uscita simulata), solo training, nulla e' stato cambiato.
Regole: `docs/decisions/2026-10-28-ipotesi-strategie-laboratorio.md` sez. 3-4 e 10 (E5, E6); regole di segnale in
`modules/lab/signals_cd.py`; script `script/count_signals_cd.py`; risultati completi in
`data/reports/count_signals_cd/counts.json` (cartella ignorata da git: rigenerabile con
`python -m script.count_signals_cd`).

Dati: braccio U = BTC-USD, ETH-USD 15m USD, taglio in lettura `ts < 2021-01-01` (ETH da 2016-05-18); braccio E
(solo informativo: il documento non decide C e D su EUR) = SOL-EUR 1m 2023-10-01 .. 2025-08-31.
Unita' = eventi di monete diverse entro 24 h (cluster ancorato al primo evento).

## Dubbio

Il braccio U decide C e D e il minimo e' 150 unita' in training (sez. 4: sotto = INCONCLUSO prima di guardare il segno).
Sia C sia D risultano sotto il minimo gia' come SEGNALI, cioe' prima di fill, posizione occupata e uscite. Sono necessarie
le istruzioni del quant: restare con INCONCLUSO (e quindi nessun GO possibile offline), oppure una nuova
pre-registrazione. Non e' stato cambiato alcun parametro.

## Conteggi

### C (evento -3 sigma4h, deduplica 24 h, E6)

| Moneta | 2016 | 2017 | 2018 | 2019 | 2020 | Eventi | Unita' (moneta) |
|---|---|---|---|---|---|---|---|
| BTC-USD | 26 | 24 | 19 | 16 | 19 | 104 | 104 |
| ETH-USD | 11 | 16 | 25 | 20 | 22 | 94 | 94 |
| Braccio U (unito) | - | - | - | - | - | 198 | 147 (34, 27, 32, 28, 26) |
| SOL-EUR (2023: 5, 2024: 22, 2025: 16) | - | - | - | - | - | 43 | 43 |

- Training braccio U: 147 unita' di SEGNALE contro 150. Le unita' di TRADE saranno meno: fill maker 50-70% (sez. 3.C) e
  libro occupato (una posizione alla volta) tolgono ancora; stima 75-100 unita' di trade. Esito: sotto.
- Holdout (stima dal tasso del training, 29,4 unita'/anno x 1003 giorni): 80,7 unita' di segnale contro 60; con fill
  50-70% circa 40-56 unita' di trade: probabilmente sotto. L'holdout non e' stato letto.

### D (compressione Q <= q20 + breakout del canale 30 barre 4h)

q20 dal training: BTC-USD 0,5966; ETH-USD 0,6162; SOL-EUR 0,7192 (SOL dal braccio E, Q4).

Poiche' D non ha stato di posizione in questo conteggio, tre convenzioni: `raw_signal_bars` = limite superiore (un trade
richiede una barra di segnale), `run_starts` = barre consecutive collassate, `min_separation_61_bars` = limite inferiore
(ogni trade dura le 60 barre massime, nuovo ingresso valutabile dopo 61 barre).

| Convenzione | BTC-USD eventi (unita') | ETH-USD eventi (unita') | Braccio U unita' | per anno U 2016..2020 | SOL-EUR eventi (unita') |
|---|---|---|---|---|---|
| raw_signal_bars | 177 (88) | 115 (56) | 125 | 29, 45, 15, 24, 12 | 48 (22) |
| run_starts | 123 (87) | 73 (55) | 124 | 29, 44, 15, 24, 12 | 30 (22) |
| min_separation_61_bars | 49 (49) | 36 (36) | 73 | 19, 18, 10, 17, 9 | 13 (13) |

- Training braccio U: anche il limite superiore (125) e' sotto 150. Esito: sotto con certezza (i trade sono al piu' i segnali).
- Holdout (stima, x 1003 giorni): tra 40,1 (inferiore) e 68,6 (superiore) contro 60: ambiguo; l'holdout non e' stato letto.

## Confronto con i minimi (sez. 4)

| Ipotesi | Training U (minimo 150) | Holdout stimato (minimo 60) |
|---|---|---|
| C | 147 segnali, ~75-100 trade: SOTTO | 81 segnali, ~40-56 trade: probabilmente sotto |
| D | 73-125: SOTTO | 40-69: ambiguo |

Aggiungere SOL-EUR (braccio E, 2023-10 .. 2025-08, contaminato e senza ruolo decisionale) darebbe C 147 + 43 = 190
e D 125 + 22 = 147 unita' (limite superiore): non cambia il verdetto e il documento non lo ammette come braccio
decisionale per C e D (sez. 2: senza braccio U solo esito CANDIDATO).

## Domanda al trader-quant

Confermare che con il braccio U sotto il minimo C e D restano INCONCLUSO (la sez. 4 lo prevede prima di guardare il
segno) e dire se serve una nuova pre-registrazione (per esempio altre monete USD o granularita' diversa); lo sviluppatore non cambia soglie,
finestre, minimi ne' definizione di unita' senza tale regola scritta.

## Risposta trader-quant (2026-10-28)

Legenda: [M] misurato/letto, [D] dedotto da formula, [S] stima. Regola scritta come errata E11 in
`docs/decisions/2026-10-28-ipotesi-strategie-laboratorio.md` sez. 11 (prima di qualunque calcolo di esito di C e D).

### 1. I minimi erano giusti? No: erano stimati in difetto, non in eccesso

- C: la formula della sez. 1 con i parametri della sez. 3.C (s 1,8%, delta 0,4%) da' n_min = 9,5 x (1,8 / 0,4)^2 = 192
  [D]. Il minimo 150 era l'estremo basso della frequenza attesa, non n_min: incoerente gia' in origine. In piu' s 1,8% e'
  calibrata sulla volatilita' EUR 2023-2026; nel 2016-2020 sigma1h e' ~1,5-2 volte maggiore [S] e il payoff a barriere
  (+4 / -6 sigma1h, tempo 24 h) implica s ~3,5-5 sigma1h [D], cioe' s ~2,5-4% [S]. Con s 3%: n_min ~530 [D].
- D: 150 coerente con s 4% e delta 1% (n_min 152 [D]), ma delta 1% netto vuol dire 2,1% lordo per trade (ottimistico) e
  per tenute di 3-10 giorni nel 2016-2020 s ~6-8% [S]. Con s 7%: n_min ~470 [D].
- Quindi non esiste un argomento statistico per abbassare i minimi: il calcolo indica che andavano alzati. L'opzione (b)
  e' respinta.

### 2. Potenza effettiva con i conteggi reali [D]

Test a una coda, alpha 0,0125 (Holm caso peggiore), potenza = Phi(delta / s x sqrt(n) - 2,24).

| Ipotesi | delta | s | n = limite basso | n = centrale | n = limite alto |
|---|---|---|---|---|---|
| C | 0,4% | 1,8% (sez. 3) | 0,38 (n 75) | 0,49 (n 100) | 0,67 (n 147, impossibile: e' il n dei segnali) |
| C | 0,4% | 3% [S] | 0,14 | 0,18 | 0,27 |
| D | 1% | 4% (sez. 3) | 0,46 (n 73) | 0,60 (n 100) | 0,71 (n 125) |
| D | 1% | 7% [S] | 0,15 | 0,21 | 0,26 |

Potenza effettiva realistica: C ~15-40%, D ~15-45% sugli effetti ottimistici della sez. 3; 5-20% su effetti dimezzati.
Valore atteso di un'errata che abbassasse i minimi: P(GO) ~ 0,08 x 0,3 x 0,8 ~ 2% [S], di cui circa un quarto falso
positivo [D], al prezzo di bruciare l'unico holdout pulito (che per C sarebbe a sua volta sotto 60). Non conviene.

### 3. Decisione: opzione (a), con holdout riservato

1. C e D: INCONCLUSO per n sotto il minimo in training (sez. 4 applicata come scritta), dichiarato ora, prima di
   qualunque calcolo di esito. Nessun GO ne' CANDIDATO offline per C e D. La famiglia primaria (A, B, C, D) non ha
   piu' alcun test con n sufficiente: nessun GO offline nel laboratorio.
2. Nessun rendimento di C o D si calcola, ne' in training ne' in holdout, neppure come "esplorativo":
   `script/study_cd.py` non si esegue nella forma della sez. 10.
3. Holdout U 2021-01-01 .. 2023-09-30 per C e D: non letto e RISERVATO. Utilizzabile solo da una futura
   pre-registrazione confermativa scritta prima della lettura, parametri di segnale invariati.
4. Ammessa, facoltativa (decide l'utente), una sola corsa IN CIECO sul training che scrive esclusivamente: s
   (deviazione standard del netto per unita', ddof 1), numero di unita' di trade, tasso di fill di C e di C0. Vietato
   calcolare o scrivere media, mediana, somma, segno, t, p, quote di uscita target/stop/tempo, hit rate, equity, PnL per
   anno. Serve solo a ricalcolare n_min = 9,5 x (s / delta)^2 per decidere se un test confermativo su holdout U + forward
   e' fattibile; se n_min supera le unita' ottenibili, C e D si chiudono per futilita'. Raccomandazione: non farla ora.
5. Allargare il campione (opzione c), valutata e respinta ora:
   - altre monete USD 2016-2020 (LTC-USD, poi BCH-USD, ETC-USD [S]): i crolli sono correlati fra monete (198 eventi
     BTC + ETH collassano in 147 unita' [M]); stima +10-30 unita' di trade [S]; potenza C 0,43-0,62 con s 1,8%,
     0,16-0,24 con s 3% [D]. Download, nuova pre-registrazione, monete meno liquide: beneficio insufficiente.
   - barre 5m: le regole sono su 1h/4h con deduplica 24 h; non aggiungono eventi, cambierebbero l'ipotesi. Zero su n.
   - storia piu' lunga: Coinbase USD non esiste prima del 2015; altri exchange = venue e licenza diverse. Unire
     l'holdout al training (C ~115-155 trade) da' 0,56-0,70 con s 1,8% e 0,21-0,30 con s 3% [D], e brucia l'holdout.
6. Forward: i book C, C0, D restano come E1-E9; essendo INCONCLUSO lo sguardo e' descrittivo (E2), non autorizza
   capitale. q20 di D (output di training, non esito) gia' disponibili e da copiare congelati nel book D (E5):
   BTC-USD 0,5966; ETH-USD 0,6162; SOL-EUR 0,7192 [M].

Allo sviluppatore: nessun cambio a soglie, finestre, minimi o unita'. Non implementare la simulazione di esito di
`study_cd.py`; il resto (book forward, q20) procede secondo E1-E10.

### 4. Il progetto nel suo complesso

Stato onesto [M]: prezzi (P1) NO-GO; news, volume, A, B, C, D senza vantaggio dimostrato (INCONCLUSO non e' NO-GO, ma
nessuna evidenza di edge). Causa strutturale [D]: costi 77-110 bps a giro contro edge plausibili di decimi di punto,
e eventi utili troppo rari per accumulare n in pochi anni di dati. Con le regole attuali (E2) nessun book puo' arrivare
al capitale reale entro lo sguardo del 2027-10-28: P ~0 per costruzione [D].

| Opzione | Effetto | Probabilita' / valore [S] |
|---|---|---|
| Restare in dry run con i book forward (raccomandata) | costo ~0 EUR; misura fill, costi realizzati, selezione avversa (C0 a 120 trade in ~3 mesi) | P(evidenza sufficiente per un test confermativo che poi passa) ~5-10% in 2-3 anni |
| Ridurre il perimetro | togliere B e B0 se non ancora implementati (10 trade in ~2 anni, potenza trascurabile); nessuna nuova ipotesi offline sugli stessi dati | riduce solo costo di sviluppo |
| Aumentare il capitale | non crea edge, aumenta la perdita attesa da costi | beneficio ~0 finche' non c'e' un GO; sconsigliato |
| Cambiare exchange (fee 10-40 bps) | dimezza i costi, settimane di integrazione | P(trasforma qualcosa in GO) ~5%; sconsigliato ora |
| Fermare il filone trading | libera tempo di sviluppo | P(di rinunciare a un edge reale sfruttabile) ~10-15% |

Raccomandazione: dry run a capitale zero con perimetro ridotto (A, C, C0, D, HODL), nessuna nuova ricerca offline,
nessun capitale aggiunto, nessun cambio exchange, con scadenza: allo sguardo del 2027-10-28 (o prima per futilita' da
C0), se nessun book mostra segno coerente e costi entro F3, chiudere il filone trading sistematico. Riferimento: il book
HODL; nessuna regola ha dimostrato di batterlo.

Decisioni dell'utente:
1. Presa d'atto di E11: C e D INCONCLUSO a priori, minimi non abbassati, holdout U riservato.
2. Corsa in cieco per s (punto 3.4): si' o no (raccomandato: no ora).
3. Perimetro dei book: tutti e sette oppure senza B e B0.
4. Scadenza del filone: confermare la chiusura al 2027-10-28 se nulla emerge, o altra data fissata ora.
5. I ~28 EUR reali: nessuna regola sistematica e' autorizzata; tenerli in cassa o un eventuale acquisto e tenuta manuale
   sono scelte d'investimento dell'utente, fuori dal bot.
