# STUDIO PRE-REGISTRATO: drawdown multi-mese e volume anomalo -> rendimento a 7/14/30 giorni

Autore: trader-quant. Data: 2026-10-26. Tipo: ricerca offline, nessuna modifica ai moduli del sistema (ordini, saldi, `RiskManager`, `BalanceGuard` intoccati). Stato: PRE-REGISTRAZIONE, scritta PRIMA di qualunque calcolo confermativo; le sezioni 0-14 sono congelate al commit di questo file; la sezione 15 (esito) si compila dopo l'unica corsa. Legenda: [M] misurato o letto, [D] dedotto da formula, [S] stima da verificare.

## 0. Sintesi onesta (scritta prima dei dati)

1. Due ipotesi primarie (H1 capitolazione, segno +; H2 recupero a basso volume, segno -) x 3 orizzonti = famiglia di 6 test, correzione di Holm. Tutto il resto e' esplorativo e non puo' produrre un GO.
2. Con i soli 3 anni EUR in repo (braccio E) lo studio NON puo' concludere: i dati sono gia' stati esplorati (`pattern_explore.py`) quindi l'holdout non e' pulito e il GO e' escluso per costruzione (massimo CANDIDATO); il minimo di episodi a 14/30 g quasi certamente non si raggiunge [S]. Esito atteso: INCONCLUSO.
3. Con la storia giornaliera Coinbase USD 2015/2016-2023 (braccio L, raccomandato, serve l'approvazione dell'utente al download) il test e' pulito, ma rileva solo effetti enormi: lordo medio per episodio di circa +11% a 7 g, +16% a 14 g, +27% a 30 g con potenza ~64% (sez. 12) [S]. Effetti realistici di qualche punto percentuale non sono rilevabili con nessun braccio.
4. Il vincolo non e' il costo (77 bps) ma la varianza: deviazione standard del rendimento di un episodio 13-27% a 7-30 g [S], con poche decine di episodi indipendenti al massimo.

## 1. Domanda e vincoli

Dopo un drawdown profondo dal massimo di 180 g accompagnato da volume anomalo alto (capitolazione), oppure dopo un recupero dal minimo con volume basso, il rendimento a 7/14/30 g di un acquisto spot differisce dal rendimento di un ingresso qualsiasi, abbastanza da coprire 77 bps a giro? Operazione valutata: acquisto spot long-only, size coperta al 100% dal saldo, nessuna leva, uscita a scadenza H, nessuno stop (perdita massima = capitale spot allocato alla posizione). Una ipotesi con segno negativo (H2) in un sistema solo long puo' valere solo come veto, mai come ingresso.

## 2. Dati e bracci

### 2.1 Braccio E (dati in repo)

Barre 1m BTC-EUR, ETH-EUR, SOL-EUR da `data/bars/1m` via `modules/market/backfill.py::load_bars` (griglia completa con barre piatte) e `BarStore` (barre reali, per i buchi), da circa 2023-10-01 [M]. Contaminazione: l'esplorazione `pattern_explore.py` (scratchpad, non in repo) ha guardato questi dati su un periodo non documentato. Regola: se prima della corsa lo sviluppatore non documenta (leggendo lo script, se esiste ancora) un intervallo esplorato che termina prima di `holdout_start`, si assume esplorato TUTTO il periodo EUR: `holdout_clean = false`.

### 2.2 Braccio L (proposto, storia giornaliera piu' lunga)

- Fonte: candele pubbliche Coinbase, stesso endpoint gia' usato nello studio news (`https://api.coinbase.com/api/v3/brokerage/market/products/{id}/candles`, granularita' `ONE_DAY`, nessuna chiave), BTC-USD e ETH-USD dal primo giorno disponibile [S: BTC 2015, ETH 2016; il primo giorno si registra in `01_data.json`].
- Licenza: NON e' una licenza aperta; l'uso e' regolato dai termini d'uso di Coinbase. Ammesso qui solo uso interno di ricerca, nessuna ridistribuzione, file in `data/external/prices/` (ignorata da git). Lo sviluppatore verifica la versione corrente dei termini prima del download e ne scrive data e URL in `01_data.json`; se i termini vietano questo uso, il braccio L non si esegue.
- Affidabilita': dato primario della sede su cui si opera; nello studio news 237 barre 5m mancanti su 596.160 per BTC-USD 2019-2025 [M]. Il volume e' solo Coinbase (niente volume aggregato con wash trading), ma la quota di Coinbase sul volume globale cambia negli anni: il rapporto VR (sez. 4) e' adimensionale su <= 97 g e risente solo di spostamenti entro quella finestra [D]. USD invece di EUR: il cambio aggiunge ~1-2% di deviazione standard a 30 g, trascurabile rispetto al 27% del prezzo [S].
- Fonti scartate: aggregatori (CoinGecko, CoinMarketCap: volumi con wash trading, termini API restrittivi), CSV di terzi (licenza non chiara).
- Ruolo: training USD fino al 2020-12-31, holdout USD 2021-01-01..2023-09-30 (mai usato per esplorare questi pattern; lo studio news ha usato BTC-USD solo su finestre di notizie), poi replica sul prodotto negoziato BTC-EUR/ETH-EUR 2023-10..2026 (contaminata: puo' solo bloccare un GO, mai crearlo). SOL esclusa dalle decisioni del braccio L (nessun training: SOL-USD solo dal 2021), riportata come descrittiva.
- Bias dichiarato: le capitolazioni famose (marzo 2020 nel training, giugno e novembre 2022 nell'holdout) sono note a chiunque e hanno ispirato l'idea; non eliminabile, mitigato dalla condizione "senza l'unita' migliore" (sez. 10).

### 2.3 Scelta del braccio (da compilare PRIMA della corsa)

Braccio scelto: ENTRAMBI, E e L (approvazione dell'utente, data 2026-10-26; una corsa per braccio). Esplorazione preliminare letta dallo script (`pattern_explore.py`, scratchpad, ancora esistente): ha letto con `load_bars` TUTTA la storia EUR (BTC/ETH/SOL-EUR, senza limiti di data) e nessun dato USD. Quindi: braccio E con `--exploration-range unknown` (`holdout_clean = false`); braccio L con `--exploration-range 2023-10-01 2026-10-26` (l'intervallo EUR esplorato, che non tocca l'holdout USD 2021-01-01..2023-09-30: `holdout_clean = true` per L; la replica EUR resta contaminata e puo' solo bloccare). Verifica termini d'uso Coinbase (2026-10-26): le pagine dei termini (`https://www.coinbase.com/legal/market_data_terms`, `https://www.coinbase.com/legal/developer-platform/terms-of-service`) rispondono HTTP 403 a un client automatico (WebFetch e curl) e NON sono state lette; come previsto dal mandato si scaricano comunque solo candele giornaliere pubbliche (endpoint senza chiave), per uso interno di ricerca, nessuna ridistribuzione, file in `data/external/prices/` (ignorata da git); la mancata verifica e' scritta in `01_data.json` (`terms_verified = false`, data e URL tentati). Senza approvazione del download: solo E. Tutti i parametri sono congelati qui e identici fra i bracci; eseguire E e L (in qualunque ordine) e' ammesso, una corsa per braccio; se esiste L, la decisione e' quella di L e il risultato di E non la modifica.

## 3. Serie giornaliere

- Giorno = giorno UTC [00:00, 24:00). Il giorno in corso non si usa.
- Braccio E: O_d = open del primo minuto, H_d = max high, L_d = min low, C_d = close dell'ultimo minuto, V_d = somma su minuti di close_m x volume_m (EUR), Vb_d = somma di volume_m (unita' della moneta). Giorno non valido se contiene un tratto di oltre 360 minuti consecutivi senza barre reali nel `BarStore`.
- Braccio L: candela giornaliera; V_d = volume x close (USD, approssimazione del controvalore), Vb_d = volume. Candela mancante = giorno non valido.
- Una feature su una finestra si calcola solo se il giorno t e almeno il 95% dei giorni della finestra sono validi; altrimenti NaN. Un rendimento futuro e' NaN se manca O_(t+1) o O_(t+1+H); l'episodio esce e si conta in `01_data.json`.

## 4. Feature (fine del giorno t, solo dati <= t)

- r_d = ln(C_d / C_(d-1)).
- Drawdown: M_N(t) = max{C_d : t-N+1 <= d <= t}; DD_N(t) = C_t / M_N(t) - 1, N in {90, 180, 365}. Primaria: DD180.
- Volume anomalo: VR(t) = media{V_d : t-6..t} / media{V_d : t-96..t-7} (base di 90 g che non contiene i 7 g recenti).
- Trend del volume: VT(t) = 30 x pendenza OLS di ln(V_d) su d, d = t-29..t (variazione logaritmica su 30 g). Solo secondaria.
- Volatilita': s30(t) = deviazione standard campionaria (ddof 1) di r_d, d = t-29..t. Regime ALTO se s30(t) >= s_med (mediana di s30 sui giorni di training della moneta), BASSO altrimenti. Solo stratificazione secondaria.
- Rimbalzo: RB(t) = ln(C_t / min{C_d : t-29..t}).
- Drawdown recente: DDmin30(t) = min{DD180(d) : t-29..t}.
- Giorno idoneo: tutte le feature primarie (DD180, VR, RB, DDmin30) non NaN. Riscaldamento circa 210 g dal primo dato.

## 5. Split, soglie, purge

| | Braccio E | Braccio L |
|---|---|---|
| Training (segnali t) | primo giorno idoneo .. `holdout_start` con purge | primo giorno idoneo .. 2021-01-01 con purge |
| `holdout_start` | 2025-09-01 | 2021-01-01 |
| Holdout (segnali t) | 2025-09-01 .. ultimo giorno completo con purge | 2021-01-01 .. 2023-09-30 con purge |
| Replica EUR | - | BTC-EUR, ETH-EUR: primo giorno idoneo EUR .. ultimo giorno completo |

- Purge, per ogni H: un segnale t appartiene a uno split solo se t + 1 + H <= fine dello split (per il training: <= `holdout_start`). Le feature dell'holdout possono usare finestre che cadono nel training (sono causali).
- Soglie per moneta, calcolate SOLO sui giorni idonei del training (tutti i giorni, non solo i segnali), `numpy.quantile(..., method="linear")`: theta_DD = Q0,20(DD180); theta_VH = Q0,80(VR); theta_VL = Q0,30(VR); theta_RB = Q0,70(RB); s_med = Q0,50(s30). Le stesse soglie si applicano all'holdout e (braccio L) alla serie EUR della stessa moneta (feature adimensionali).
- Le frequenze dei giorni segnale in training, holdout e replica si riportano (uno spostamento forte di frequenza e' un avviso, non cambia la decisione).

## 6. Ipotesi

Primarie (le sole che possono produrre GO o veto; famiglia m = 6):

| Id | Condizione sul giorno t | Segno atteso | Statistica | Orizzonti |
|---|---|---|---|---|
| H1 capitolazione | DD180(t) <= theta_DD e VR(t) >= theta_VH | rendimento netto > 0 ed eccesso > 0 | netto (ingresso) | 7, 14, 30 g |
| H2 recupero a basso volume | DDmin30(t) <= theta_DD e RB(t) >= theta_RB e VR(t) <= theta_VL | eccesso < 0 | eccesso (solo veto) | 7, 14, 30 g |

Secondarie (esplorative, tutte riportate, nessuna puo' creare un GO ne' riaprire un NO-GO): S1 DD90 e DD365 al posto di DD180; S2 volume in unita' della moneta (Vb) al posto del controvalore; S3 H2 con VT(t) < 0 al posto di VR <= theta_VL; S4 costo 110 bps; S5 ingresso ritardato a O_(t+2); S6 rendimento normalizzato per volatilita' z = g / (s30(t) x sqrt(H)) e split per regime ALTO/BASSO; S7 drawdown da solo (DD180 <= theta_DD, senza volume: dice se il volume aggiunge qualcosa a H1); S8 per moneta; S9 SOL (braccio L, descrittiva).

## 7. Episodi e unita' indipendenti

- Episodi per moneta (greedy cronologico): t e' un episodio se la condizione vale in t e t - t_prec >= 15 g (separazione > 14 g) rispetto all'episodio precedente della stessa ipotesi e moneta. Gli episodi non dipendono da H.
- Unita' di inferenza per l'orizzonte H, dentro ciascuno split: componenti connesse degli episodi di TUTTE le monete, collegati se |t_i - t_j| < max(15, H) (stesso mercato, finestre sovrapposte = un'unita'). Valore dell'unita' = media dei valori dei suoi episodi.
- n = numero di unita'. E' il numero che conta, non il numero di episodi.

## 8. Rendimenti e costi

- Ingresso a O_(t+1) (apertura del giorno dopo il segnale; il segnale usa la chiusura di t, quindi nessun look-ahead), uscita a O_(t+1+H).
- Lordo g = O_(t+1+H) / O_(t+1) - 1. Netto = g - k, k = 0,0077 (77 bps a giro, primario) e 0,0110 (entrambi i lati taker con slippage, secondario S4).
- Eccesso x = g - mu, mu = media di g su TUTTI i giorni idonei della stessa moneta, stesso split, stesso H (ingresso qualsiasi nello stesso periodo). mu e' trattato come fisso: l'IC dell'eccesso e' quindi ottimistico; riportare anche (secondario) un bootstrap a blocchi mobili sul calendario, blocco 2H, che include l'incertezza di mu.
- Rischio: MAE = min{L_d : t+1 <= d <= t+H} / O_(t+1) - 1 per episodio; riportare mediana e 10o percentile (serve al sizing se mai GO).

## 9. Statistica

Per ogni test (ipotesi, H, split), con valori y_u delle n unita' (netto per H1, eccesso per H2):
- Bootstrap a cluster (le unita' sono i blocchi): B = 10.000, seme 20261026 + indice del test, percentili 5% e 95%. p_boot = (1 + #{media* <= 0}) / (B + 1) per H1; per H2 con >= 0.
- t di Student sulle medie delle unita': LB_t = media - t(0,95; n-1) x s / sqrt(n); p_t unilaterale.
- Conservativo: LB = min(LB_boot, LB_t); UB = max(UB_boot, UB_t); p = max(p_boot, p_t). IC al 90% bilaterale = test unilaterale al 5%.
- Holm sui 6 p del training (un test con n sotto il minimo entra con p = 1). L'holdout si testa solo per i test che passano il training (procedura a cancello: FWER complessivo <= 5% senza ulteriore correzione).

## 10. Regola di decisione (fissata ora)

Minimi: n_T >= 10 unita' nel training, n_V >= 6 nell'holdout (sotto ~10 unita' l'IC bootstrap sottocopre e quello t e' enorme); il controllo per moneta vale solo per monete con >= 3 episodi nello split.

Per ogni test di H1:
1. INCONCLUSO (insufficiente): n_T < 10 oppure n_V < 6.
2. NO-GO: con n sufficienti, almeno uno fra: netto medio <= 0 nel training o nell'holdout; eccesso medio <= 0 nel training o nell'holdout; UB del netto < 0 in uno split.
3. GO, tutte: (a) p_Holm del training < 0,05; (b) LB del netto nell'holdout > 0; (c) eccesso medio > 0 in training e holdout; (d) nessuna moneta con netto medio <= 0 in training o holdout; (e) netto medio > 0 in training e holdout anche togliendo l'unita' migliore dello split; (f) braccio L: replica EUR con netto medio > 0 se n_R >= 3 (con n_R < 3 la condizione e' "non valutabile" e non blocca, ma e' scritta); (g) `holdout_clean = true`, altrimenti il verdetto e' CANDIDATO.
4. Altrimenti INCONCLUSO (stime favorevoli ma non abbastanza precise, o (d)/(e)/(f) falliti).

Per ogni test di H2 (veto), stesse regole con il segno rovesciato sull'eccesso: NO-GO se eccesso medio >= 0 in uno split; VETO se p_Holm < 0,05, UB dell'eccesso nell'holdout < 0, nessuna moneta con eccesso medio >= 0, e (e)/(g) come sopra; altrimenti INCONCLUSO.

Esito globale (solo H1 decide sugli ingressi): GO se almeno un test di H1 e' GO (orizzonte operativo = il piu' corto fra quelli GO, deciso ora); NO-GO se tutti e 3 i test di H1 sono NO-GO; CANDIDATO se il migliore e' CANDIDATO; altrimenti INCONCLUSO. H2 si riporta a parte.

Cosa significa INCONCLUSO: i dati sono compatibili sia con un effetto abbastanza grande da pagare i costi sia con un effetto nullo o negativo. Non autorizza a operare, non chiude l'idea, e soprattutto non autorizza a provare varianti sugli stessi dati. NO-GO non prova l'assenza dell'effetto: dice che la stima puntuale e' sfavorevole e che non si spende altro su questi dati.

## 11. Cosa succede in ogni esito

- GO: nessun capitale subito. (1) Spec di una strategia giornaliera separata dal modello a minuti, regola congelata: al piu' una posizione per moneta, controvalore <= min(saldo disponibile al netto delle fee, equity / 3), somma delle posizioni <= 100% del saldo spot (nessuna leva), ogni ordine passa da `BalanceGuard`; perdita massima = capitale allocato. (2) Dry run in avanti con criterio scritto prima: netto medio > 0 su >= 6 nuove unita'. (3) Solo dopo, R0 a capitale minimo.
- NO-GO: idea chiusa come fonte di ingresso, scritto nella sezione 15. Nessuna variante (DD90/365, altri quantili, altri orizzonti) sugli stessi dati; le secondarie non la riaprono. Riapertura solo con dati nuovi indipendenti e una nuova pre-registrazione non derivata da questi risultati.
- INCONCLUSO: nessun capitale, nessuna variante. Un solo riesame pre-programmato con lo script congelato quando i dati EUR in avanti dal 2026-10-27 danno >= 6 nuove unita' a H = 7 per H1, oppure al 2028-10-26 se prima: le nuove unita' sono un secondo holdout (solo regola 3(b)-(e)); un solo sguardo.
- CANDIDATO (braccio E): trattato come INCONCLUSO; unica azione utile e' il braccio L.
- VETO (H2): documentato; non cambia nulla finche' non esiste una strategia di ingresso.

## 12. Potenza attesa [S] e se lo studio puo' concludere

Ipotesi di calcolo [S]: volatilita' giornaliera in regime di drawdown 4,5-6% (braccio L, 2016-2023) e ~4,5% medio (braccio E, BTC/ETH/SOL 2024-2026); correlazione fra monete ~0,8 (un'unita' con 2-3 monete vale ~1 episodio: fattore 0,93-0,95); deviazione standard di un'unita' 13% / 19% / 27% a 7 / 14 / 30 g (braccio L), 11% / 16% / 23% (braccio E).

| | n_T (7/14/30 g) | n_V (7/14/30 g) | SE training bps | lordo medio per potenza 80%, training Holm (z = 2,39 + 0,84) | idem holdout (z = 1,645 + 0,84) |
|---|---|---|---|---|---|
| Braccio L | ~20 / 18 / 14 | ~10 / 9 / 7 | 295 / 441 / 732 | 1.030 / 1.500 / 2.440 bps | 1.115 / 1.630 / 2.660 bps |
| Braccio E | ~7 / 6 / 5 | ~5 / 4 / 3 | n sotto il minimo | non valutabile | non valutabile |

- Braccio L: con un effetto vero di +300 bps lordi a 7 g, P(GO) < 1% (training ~5%, holdout ~13%) [D dalle ipotesi]. Sotto l'ipotesi nulla il falso GO e' <= 5% per costruzione (in pratica < 1%, due cancelli); P(NO-GO globale | nullo) ~55%, il resto INCONCLUSO [S]. Lo studio puo' concludere NO-GO o GO solo per effetti enormi; per effetti realistici l'esito piu' probabile e' INCONCLUSO o NO-GO.
- Braccio E: n sotto il minimo quasi certamente a 14 e 30 g e probabilmente a 7 g nell'holdout -> INCONCLUSO per regola; il GO e' comunque escluso (holdout contaminato). Non puo' concludere.
- Il costo (77 bps) e' il 3-8% della soglia rilevabile: abbassarlo (maker su entrambi i lati) non cambia nulla. Cambierebbe solo il numero di unita', cioe' anni di storia.

## 13. Bias e limiti dichiarati ora

1. Ipotesi ispirate da un'esplorazione sui dati EUR e da capitolazioni famose (sez. 2). 2. Sopravvivenza: BTC/ETH/SOL sono scelte perche' oggi esistono e sono grandi; "comprare il calo" su monete sopravvissute e' distorto verso l'alto (soprattutto SOL). 3. Volume di una sola sede. 4. Non stazionarieta': 2016-2020 (bolle, nessun ETF) non e' 2024-2026. 5. Ingresso a O_(t+1) senza latenza (ottimistico di minuti, irrilevante su 7-30 g; S5 lo misura). 6. Gli orizzonti sono annidati: i 3 test di una ipotesi sono correlati (Holm resta valido).

## 14. Specifica di esecuzione per lo sviluppatore

File: `script/study_volume_patterns.py` (stessa convenzione di `script/sim_p1f_v3.py`), test `tests/test_study_volume_patterns.py`. Solo lettura dei dati, nessun import dai moduli di trading oltre a `modules.market.backfill` (`load_bars`, `BarStore`).

Costanti: un dizionario letterale `PREREG` con tutti i parametri delle sez. 3-10 (finestre 90/180/365, 7/96, 30; quantili 0,20/0,80/0,30/0,70/0,50; separazione 15; minimi 10/6/3; B = 10.000; seme 20261026; costi 0,0077/0,0110; `holdout_start` per braccio; date del braccio L; validita' 95% e buco 360 minuti). Il suo sha256 (JSON con chiavi ordinate) va in `00_preregistration.json`.

CLI:
- `python -m script.study_volume_patterns fetch-usd --products BTC-USD ETH-USD --out data/external/prices` solo dopo la riga compilata in 2.3 e la verifica dei termini; scrive `BTC-USD_1d.parquet`, `ETH-USD_1d.parquet` (colonne `ts`, `open`, `high`, `low`, `close`, `volume`), nessun calcolo.
- `python -m script.study_volume_patterns run --arm {E,L} --bars-root data/bars/1m [--usd-daily data/external/prices] --exploration-range {unknown|START END} --out data/reports/study_volume`.

Ordine obbligato in `run`:
1. Rifiuta (exit 2) se `<out>/<arm>/` esiste o se lo script ha modifiche non committate (`git status --porcelain` sul file).
2. Scrive `00_preregistration.json`: schema `chronos.study_volume.v1`, braccio, commit git, sha256 dello script e di questo documento, sha256 di `PREREG`, `exploration_range`, `holdout_clean` (falso se `unknown` o se l'intervallo tocca l'holdout), ora UTC. Prima di leggere qualunque barra.
3. `01_data.json`: per prodotto primo/ultimo giorno, giorni validi e non validi, buchi, episodi esclusi per NaN, sha256 dei parquet USD, data e URL dei termini Coinbase verificati.
4. `02_thresholds.json`: soglie per moneta e numero di giorni di training usati.
5. `03_episodes.json`: per episodio ipotesi, moneta, t, split, feature, g / netto / eccesso / MAE per H, id dell'unita' per H.
6. `04_results.json`: per (ipotesi, H, split) `n_units`, episodi per moneta, frequenza dei giorni segnale, medie lorda / netta 77 / netta 110 / eccesso in bps, IC 90% bootstrap e t, LB, UB, p, p_Holm (training), medie per moneta, media senza l'unita' migliore, MAE mediana e 10o percentile.
7. `05_secondary.json`: S1-S9, stesse colonne, etichetta `exploratory`.
8. `06_decision.json`: verdetto per test con l'elenco delle condizioni passate/fallite (codici della sez. 10), verdetto globale, verdetto H2.
Nessuna stampa di rendimenti prima che `06_decision.json` sia scritto. Nessuna corsa sui dati veri prima di quella confermativa (neanche in una cartella temporanea): prima si usano solo i test sintetici. Una corsa per braccio; i numeri vanno in un documento `-esito.md` separato e nella sezione 15.

Funzioni pure (testabili): `daily_from_1m(bars, real_ts) -> DataFrame`, `features(daily) -> DataFrame`, `thresholds(feat, train_mask) -> dict`, `signals(feat, thr) -> dict[str, Series]`, `episodes(signal, sep=15) -> list[date]`, `units(episodes_by_coin, horizon) -> list[list[...]]`, `forward(daily, t, horizon, delay=1) -> (g, mae)`, `ci_boot(y, B, seed)`, `ci_t(y)`, `holm(pvals)`, `verdict_h1(...)`, `verdict_h2(...)`, `global_verdict(...)`.

Test sintetici minimi:
1. `daily_from_1m`: open/close/high/low/volume e controvalore su un giorno costruito; un buco di 361 minuti rende il giorno non valido, uno di 360 no.
2. Nessun look-ahead: modificare i dati dopo t non cambia le feature in t.
3. Soglie solo dal training: modificare l'holdout non cambia `02_thresholds`.
4. Episodi: segnali in giorni consecutivi = 1 episodio; segnale a +15 g = nuovo episodio; a +14 g no.
5. Unita': episodi di monete diverse a 10 g = 1 unita'; a 20 g = 2 unita' per H = 7 e 1 per H = 30.
6. Rendimento e costo: percorso noto, ingresso O_(t+1), netto = g - 0,0077; MAE corretto.
7. Purge: un segnale di training con t + 1 + H > `holdout_start` e' escluso.
8. Holm su p noti; `ci_t` e `ci_boot` su un campione noto; con n < minimo il p e' 1.
9. Tabella di verdetti: un caso per INCONCLUSO (n), NO-GO (eccesso <= 0), GO, CANDIDATO (`holdout_clean` falso), INCONCLUSO per moneta discorde e per "senza l'unita' migliore".
10. End-to-end sintetico (3 monete correlate, random walk, 6 anni): con un rialzo iniettato di +25% a 7 g dopo ogni capitolazione -> GO per H1 a 7 g; senza iniezione -> nessun GO (seme fisso).
11. `run` rifiuta una cartella di output esistente e scrive `00_preregistration.json` prima di leggere le barre (store finto che registra l'ordine delle chiamate).

## 15. Esito

Compilato il 2026-10-26 dopo l'unica corsa per braccio (codice al commit 81ac71da, nessun ritocco). Dettaglio, numeri e conseguenze: `docs/decisions/2026-10-26-studio-pattern-volume-esito.md`.

- Braccio L (decide; `holdout_clean = true`): H1 7 / 14 / 30 g = INCONCLUSO / INCONCLUSO / INCONCLUSO (n_T = n_V = 3 unita', minimi 10 / 6); H2 INCONCLUSO x3 (n_T 7 / 7 / 5, n_V 2 / 2 / 1). Globale: INCONCLUSO.
- Braccio E (`holdout_clean = false`): H1 INCONCLUSO x3 (n_T 4 / 4 / 3, n_V 5 / 5 / 3); H2 INCONCLUSO x3. Globale: INCONCLUSO (non modifica la decisione di L).
- Conseguenza (sez. 11): nessun capitale, nessuna variante; un solo riesame pre-programmato (>= 6 nuove unita' a H = 7 per H1 con i dati EUR dal 2026-10-27, oppure 2028-10-26).
