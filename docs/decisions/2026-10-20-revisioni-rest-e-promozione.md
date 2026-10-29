# Decisione 2026-10-20 - criterio sulle revisioni delle candele REST (parte A)

Stato: implementata (parte A) il 2026-10-23. Fonte: `.claude/CONFLITTO.md` (conflitto P1e), risposte di `trader-quant` e `architetto-opus` del 2026-10-20, copiate qui senza modifiche. La parte B (regola di promozione L5, rollback) e la sua decisione vanno aggiunte da chi la chiude. Esito della certificazione del soak P0 e dubbio aperto: `.claude/CONFLITTO_certificazione_P0.md`.

## Risposta trader-quant, Parte A

### Parte A - revisioni REST

**A0. Cosa deve proteggere il criterio, e perche' il rapporto grezzo non lo misura.**
La decisione del 2026-10-18 (Taratura di S, punto 0) giustifica la soglia cosi': "ogni revisione dopo S e' skew train/serve".
Le grandezze protette sono quindi due: (1) la decisione presa (BUY/HOLD/SELL, piu' gli skip del RiskManager) e (2) la distanza fra
lo stato servito e lo stato che il modello avrebbe visto sulle barre definitive, cioe' sul tipo di barre su cui e' addestrato
(storico del backfill, letto ore dopo). Il rapporto di minuti rivisti e' un proxy di (2), e i nostri dati mostrano che e' un
proxy cattivo in entrambe le direzioni:

| Prodotto | ratio [M, p0_soak] | vecchio criterio | stati con token diverso [M, impact] | max Δp_edge [M] | max Δp(BUY) [M] |
|---|---:|---|---:|---:|---:|
| BTC-EUR | 0,496% | passa | 4,6% | 0,0067 | 0,0012 |
| ETH-EUR | 1,090% | fallisce | 51,9% | 0,0151 | 0,0029 |
| SOL-EUR | 0,481% | passa | 55,0% | 0 | 0,0023 |

- SOL passa con la quota di stati toccati piu' alta dei tre; BTC e SOL hanno lo stesso ratio (0,48-0,50%) e un impatto sui token
  diverso di 12 volte. Il ratio non ordina nemmeno i prodotti per impatto. Motivo [D]: una revisione di volume in un minuto
  notturno quasi vuoto entra per 24 ore nelle somme giornaliere (`vol_z_1d`, `dist_vwap_1d`), mentre una revisione in un minuto
  liquido puo' non spostare nessun token. Quindi il ratio sottostima (SOL) o sovrastima (nessuna relazione con Δp) cio' che conta.
- L'unico rimedio previsto dal gate (alzare S) non agisce su questa famiglia [M: revisioni rilette ore dopo la chiusura].
  Un gate che non misura la grandezza protetta e il cui rimedio e' dimostrato inefficace non e' un controllo di rischio: blocca
  senza proteggere, e spinge a ritoccare il numero.
Conclusione: il gate va sostituito con un criterio che misura (1) e (2) direttamente, per il modello in uso. Il ratio resta come
diagnostica obbligatoria nel report.

**A1. Criterio di impatto (G-DRY-OPS, riga "Barre REST"), per prodotto e per giorno UTC.**
Calcolato da L0 sul giorno D-1 (minuti con eta' >= 15 min, regola di misura gia' in vigore) e, una tantum, sulla finestra del soak.

Definizioni. Per ogni minuto di decisione `t` del giorno:
- `S_t` = stato servito (barre dello store, cioe' quelle consumate); `F_t` = stato rigiocato con le barre rilette (minuti rivisti
  sostituiti), stesso `compute_indicators`, stessi token arrotondati. `F_t` approssima cio' che il training vede sulle barre
  definitive: la differenza `F_t - S_t` E' lo skew train/serve dovuto alle revisioni.
- Per ogni modello in uso (champion e, se esiste, challenger), con le risposte calibrate:
  `de_t = |p_edge(F_t) - p_edge(S_t)|`, `db_t = |P(BUY)_masc(F_t) - P(BUY)_masc(S_t)|` (stati FLAT),
  `ds_t = |P(SELL)_masc(F_t) - P(SELL)_masc(S_t)|` (stati LONG), `m_t = max` di quelli definiti.
- `flip_t = 1` se la decisione finale (policy + RiskManager con gli stessi costi registrati nel DecisionRecord) e' azionabile in
  uno dei due casi e non nell'altro: BUY contro non-BUY, SELL contro non-SELL.

Soglie (tutte devono valere):
- **R1 quota di stati spostati in modo materiale:** `#{t : m_t > 0,01} / #stati <= 0,005`.
- **R2 spostamento massimo:** `max_t m_t <= 0,02`.
- **R3 flip azionabili:** `#flip / #minuti di decisione <= 0,001` (con 1.440 minuti: al massimo 1 per prodotto e giorno).
- **R4 completezza:** zero minuti "comparsi" (pubblicati `flat`/`flat_unproven`, ora con candela) e zero "scomparsi" (candela
  pubblicata, ora assente). E' un altro problema (completezza dell'endpoint): fallimento e nuovo CONFLITTO, come deciso il 18-10.
- Invariato: zero barre fuori ordine o duplicate.

Motivo di ogni numero:
- 0,01 (soglia di R1). E' la risoluzione con cui il sistema stesso legge il pareggio (token `p_breakeven` a 2 decimali: K7 accetta
  gia' questo scarto fra diagnostica e RM) ed e' meta' del passo della griglia `p_be_buffer`. In valore atteso
  `dEV = dp x (W + L) = 0,01 x 1.116 ~ 11 bps` [D]: un terzo di `min_net_edge_bps` (30) e un ottavo dell'edge minimo dimostrabile
  di K5 (delta 0,08 = 89 bps). Uno spostamento sotto 0,01 e' piu' piccolo dell'arrotondamento che il sistema gia' tollera sul
  pavimento economico.
- 0,005 (quota di R1). E' lo stesso budget del criterio attuale (0,5%), applicato alla quota di stati in cui il modello cambia
  risposta in modo materiale invece che alla quota di minuti grezzi. Stesso numero, grandezza giusta: non e' un allentamento.
- 0,02 (R2). Un passo della griglia `p_be_buffer`, cioe' la piu' piccola differenza di soglia che il tuning sa distinguere;
  `dEV = 22 bps` [D] < 30 bps. E' anche sotto l'errore del livello della calibrazione (SE <= 0,033 con N_eff >= 200, R2.1) [D].
  Nessuno stato puo' essere spostato oltre la risoluzione con cui la policy e' stata scelta.
- 0,001 (R3). Il gate "Parita' decisioni" concede l'1% di minuti discordanti per TUTTE le cause; alle revisioni se ne assegna al
  massimo un decimo. Un flip e' possibile solo in uno stato entro `m_t` dalla soglia, quindi con R2 solo dove l'EV e' entro ~22
  bps dal pavimento `p_be(t) + p_be_buffer`: il costo atteso di un flip e' piccolo, ma lo si conta lo stesso perche' e' l'effetto
  finale sulla decisione.

Perche' misura meglio la cosa che interessa (e non e' piu' permissivo):
- (i) Misura direttamente le due grandezze protette: `m_t` e' lo skew train/serve come lo vede il modello, `flip_t` e' l'effetto
  sulla decisione. Il ratio ne e' solo un proxy, e la tabella di A0 mostra che il proxy non e' monotono nell'impatto.
- (ii) E' piu' severo dove conta: con un modello piu' sensibile, lo 0,4% di minuti rivisti (che oggi passa il vecchio gate)
  fa fallire R1/R2. Il vecchio criterio non lo vedrebbe mai.
- (iii) Si ricalcola per ogni modello: il NB della Parte A (misura fatta con un modello che non entra) e' risolto per costruzione,
  perche' R1-R4 vanno soddisfatti dal champion e dal challenger sui giorni in cui girano. Rientra quindi anche nei 14 giorni di
  shadow di L5 (b) come "nessuna anomalia di parita'".
- Limite dichiarato: con il modello attuale (policy non tarabile, zero ingressi) R3 non e' valutabile (0 flip per costruzione) e
  R1/R2 descrivono la sensibilita' di un LightGBM a foglie, che per natura e' bassa su piccoli spostamenti. La valutazione piena
  arriva con il primo champion tarato.

Il vecchio rapporto: **diagnostica, non gate.** Nel report restano `rest_revision_ratio`, `rest_revision_max_close_bps`, classe
(valore / comparsa / scomparsa), ora UTC ed eta' alla rilettura, quota di stati con token diverso per colonna, p99 di `de`/`db`,
e il numero di label live risolte nel giorno che cambiano se ricalcolate sulle barre rilette. Allarme (non blocco) se ratio >
0,005: obbliga alla classificazione nel report del giorno. La scala di taratura di S resta valida solo per la famiglia
"assestamento" (revisioni di valore entro pochi minuti dalla chiusura); per revisioni rilette ore dopo non si applica.

Esito R1-R4 sulla finestra del soak, con `baseline-lgbm@2026-10-18.2` final [M, `soak_revision_impact.json`]:

| Prodotto | R1 (stati con m > 0,01) | R2 (max m) | R3 | R4 |
|---|---|---:|---|---|
| BTC-EUR | 0 (max de 0,0067, max db 0,0012) -> ok | 0,0067 ok | non valutabile | [?] |
| ETH-EUR | [?] db: 0; de: almeno 1 (max 0,0151), meno di 27 (p99 = 0) | 0,0151 ok | non valutabile | ok (0 comparsi/scomparsi) |
| SOL-EUR | 0 -> ok | 0,0023 ok | non valutabile | [?] |

- [?] ETH R1: il report conta solo gli stati con `dp_buy > 0,01`, non quelli con `dp_edge > 0,01`. Serve il conteggio, per
  giorno: soglia <= 0,005 x stati del giorno (~1.345 stati per giorno -> al massimo 6).
- [?] R4: la classificazione ha `stored_flat_then_traded` = 0 ma `fresh_only` = 1 per BTC e per SOL. Va verificato che sia un
  minuto al bordo della finestra di confronto (artefatto) e non una comparsa vera. Se e' una comparsa, R4 fallisce e si apre il
  CONFLITTO sulla completezza.

Cosa fare del verdetto gia' scritto:
- `data/reports/p0_soak.json` NON si modifica: `rest_revision_ratio_within_limit: false` e' la misura corretta contro il criterio
  in vigore quando il soak e' partito, e un artefatto misurato non si riscrive.
- Si aggiunge una certificazione separata (formato all'architetto) con: criterio sostituito il 2026-10-20 e motivo (A0), valori
  R1-R4 ricalcolati sulla stessa finestra con lo stesso modello, esito per prodotto.
- Il criterio di uscita di P0 "barre REST" e' certificabile come superato se R1 (conteggio ETH), R2 e R4 (verifica `fresh_only`)
  passano; R3 viene registrato come "non valutabile" e rinviato al G-DRY-OPS del primo champion tarato, dove deve valere su tutti
  i 7 giorni. Non serve un nuovo soak di 48 h per la Parte A (finestra e store sono gli stessi); serve solo se R4 fallisce.
- La modifica del testo della spec (0.5 criterio P0, 5.1.7, 5.2) va fatta con riga di changelog e rimando a questa decisione.

**A2. Mitigazione sul sistema: (a) nessuna, con R1-R4 come trigger.**
Evidenza per (a):
- Sul modello [M]: max Δp(BUY) 0,0029, max Δp_edge 0,0151, zero stati con Δp(BUY) > 0,01, contro un passo di griglia di 0,02 e
  un pavimento `p_be(t) + p_be_buffer`. Lo skew esiste sui token, non arriva alla decisione.
- Sulle label live [D]: un minuto rivisto cambia una label solo se e' il minuto in cui il prezzo tocca una barriera e la revisione
  sposta high/low attraverso di essa. Le barriere distano centinaia di bps (k x ATR_6h), le revisioni spostano il close di <= 26
  bps in un minuto [M]; la probabilita' per label e' al massimo il tasso di revisione (0,5-1%) per la probabilita' che quel minuto
  sia proprio il minuto del tocco con la barriera entro 26 bps: trascurabile. Le label live sono inoltre una piccola frazione del
  training. [?] Va contato con la diagnostica di A1.
Opzioni respinte:
- (b) Ricalcolo dei token del giorno a 00:00: per avere token coerenti bisogna riscrivere barre gia' consumate (viola I2 e le
  ricorrenze Wilder/EMA) oppure tenere due versioni del giorno. Costo strutturale certo, beneficio su Δp < 0,015.
- (c) S piu' alto di notte: [M] le revisioni sono rilette ore dopo, S = 300 s non le cattura; in piu' crea due S nel sistema,
  contro la regola "un solo S". Nessuna evidenza di beneficio.
- (d1) Escludere le ore 01-06 UTC da training/validation: 5/24 = 21% dei campioni in meno. N_eff e' il vincolo che lega: l'effetto
  minimo dimostrabile di K5 cresce di `1 / sqrt(0,79) = 1,125` (delta 0,08 -> 0,09) [D]. Costo certo, beneficio non misurabile;
  in piu' introduce una regola oraria che il runtime dovrebbe replicare.
- (d2) Addestrare su barre "a 120 s": per lo storico non esistono (il backfill legge le versioni definitive; quelle a 120 s di tre
  anni non sono ricostruibili). Per il periodo live lo store contiene gia' le barre servite (I2), quindi la parte live del
  training usa gia' barre a 120 s. Infattibile per lo storico, gia' vero per il live.
Se in futuro R1 o R2 falliscono con un modello sensibile, si apre un CONFLITTO con i dati di quei giorni: solo allora (b) va
rivalutata, perche' solo allora avrebbe un beneficio misurabile.


## Risposta architetto, sezioni 0 e A

## Risposta architetto

Autore: architetto-opus. Data: 2026-10-20. Stato: **RISOLTO**. I passi 1-5 della sezione C si implementano subito; i passi che
cambiano un gate o la spec aspettano le decisioni dell'utente U1-U3 (sezione D). Nessun codice e' stato scritto dall'architetto.
File letti: `CLAUDE.md`; spec 0.5, 3.4, 3.5 (punti 8-11), 5.1.6, 5.1.7, 5.2, Appendice A; `modules/market/bars.py`,
`modules/market/backfill.py` (`fill_flat_bars`, `load_bars`, `BarStore`, `candles_to_frame`),
`modules/dryrun/recorder.py`, `report.py`, `registry.py`, `learner.py`, `modules/config.py`, `modules/features/state_matrix.py`,
`modules/decision/policy.py`, i due script `script/soak_revision_*.py`, `docs/decisions/2026-10-18-barre-rest.md`.
Revisione del 2026-10-20 (seconda lettura del codice): aggiunta la correzione K15 (costruzione del mondo rivisto sulle righe
reali dello store), con le modifiche conseguenti in A.1, A.3, A.9 e C. Il resto e' invariato.

### 0. Verdetto e conflitti trovati

- **Parte A:** accetto la sostituzione del gate con R1-R4 (ratio = diagnostica con allarme) e la mitigazione (a) "nessuna".
  E' coerente con I2 e non allenta il criterio (motivi in A.7). Correggo sette punti della proposta per renderla calcolabile e
  non piu' debole (K1-K6, K15).
- **Parte B:** accetto P0-P4 del quant. Il codice attuale ha due difetti che rendono L5 inoperante a prescindere dalla regola
  (K7, K8) e altri tre che impediscono di calcolare le condizioni nuove (K9-K12). Una condizione (V1) va resa verificabile (K13).

| # | Dove | Problema | Correzione minima (normativa) |
|---|------|----------|-------------------------------|
| K1 | `docs/decisions/2026-10-18-barre-rest.md` Q3.2 | "Il criterio 0.005 non si cambia in nessun caso." | La frase vietava di spostare la soglia per far passare un soak. Qui la soglia non si sposta (resta 0.005, come allarme); cambia la grandezza perche' la premessa della scala di S ("le revisioni sono di assestamento e S le cattura") e' smentita dalla misura. La decisione 2026-10-18 riceve in testa la nota di superamento (testo in A.10). |
| K2 | R1 del quant (`ds_t` sugli stati LONG) | `flat_matrix` copre solo FLAT. | `modules/features/state_matrix.long_matrix` esiste: gli stati LONG si costruiscono con i numeri della posizione presi dal ledger dry (identici nei due mondi, sono esecuzione) e il `close` di ciascun mondo. Quindi `ds_t` e i flip SELL sono calcolabili e diventano obbligatori. |
| K3 | R3 del quant ("policy + RiskManager con i costi registrati nel DecisionRecord") | Il RiskManager calcola il pareggio solo quando pianifica un BUY, e nel mondo rivisto non gira. | `flip_t` si calcola sui TOKEN di ciascun mondo (`p_breakeven`, `atr_14_<atr_tf>_bps`, `cost_tp_bps`, `cost_sl_bps`) con le formule della sez. 0.3: stessa approssimazione gia' accettata da K7 (token a 2 decimali). Definizione esatta in A.1. |
| K4 | `F_t` del quant (solo le revisioni del giorno D-1) | Le feature a 1 giorno, 1d HTF e percentili usano anche i giorni prima: applicare solo D-1 sottostima lo skew. | L0 salva ogni notte le revisioni trovate; il mondo rivisto applica TUTTE le revisioni di valore note fino al giorno misurato (A.1, A.3). |
| K5 | P4 del quant (R1-R4 del challenger nello shadow) | Il blocco `anomalies` del report e' globale: un fallimento del champion verrebbe addebitato al challenger e viceversa. | Nuovo `anomalies.revision_by_version`; `ReportShadowEvidence` somma solo la propria versione (A.5). |
| K6 | Numeri del soak negli script di ricerca | Gli script usano un parsing proprio (`astype(float)`, fetch con END incluso: e' l'origine dell'artefatto 05:40) e confrontano con la griglia gia' riempita (da qui i 234/450/350 minuti "solo nello store", che sono minuti piatti mai scritti nel Parquet). | La certificazione si rifa' con le funzioni ufficiali (`CoinbaseCandleSource` + `candles_to_frame`, stesso parsing di I1) sulle righe reali dello store (K15). I numeri degli script restano ricerca, non prova (A.6). |
| K7 | `learner.py`: ordine `L3 -> L1 -> L2 -> L5` | L2 sposta l'alias `challenger` sulla versione appena addestrata; L5 legge poi `get(CHALLENGER)` = versione con 0 giorni di shadow: L5 risponde sempre `insufficient`. In piu' `_gate_model_step` salta L5 se L1 o L2 sono `skipped`/`failed`. **L5 non puo' mai promuovere.** | L5 valuta il challenger IN CARICA all'inizio della notte (`challenger_before`, salvato nel documento come `champion_before`), non dipende dall'esito di L3/L1/L2, gira solo nel giorno di refit (B.4). |
| K8 | `HoldoutBacktester` | La finestra e' tagliata dentro l'holdout del `label_config` di CIASCUN modello: con due `label_config` diversi le finestre differiscono (viola V2). | Il learner calcola UNA finestra comune e la passa a entrambi (B.4). |
| K9 | `HoldoutBacktester` per policy `untunable` | Ritorna `sharpe=None` -> `insufficient` per sempre; il quant chiede metriche definite (PnL 0, Sharpe 0, DD 0) per un modello senza trade. | `BacktestEvidence.n_trades`; con `n_trades == 0` Sharpe = 0.0 (B.2). |
| K10 | `learner._gate_flags` | Copia solo G0, G1, G2, GE: G4 (e G3) non arrivano al manifest, P1 non e' valutabile. | Chiavi `("G0", "G1", "G2", "G3", "G4", "GE")`; L5 legge `evaluation.json` dalla directory della versione (B.4). |
| K11 | S1/S2 (bootstrap accoppiato sull'OOS) | Serve la lista dei trade OOS con i tempi, per versione. Non ho trovato traccia che `evaluate` la persista. | `training/evaluate.py` scrive `oos_trades.jsonl` e `oos_spans.json` nella directory del modello PRIMA della registrazione (L2 registra dopo `evaluate`): finiscono nella versione immutabile con sha256 (B.5). Se Sonnet trova che esistono gia', li riusa e lo scrive nel commit. |
| K12 | `LearnerConfig.promotion: dict[str, int]` | Non puo' contenere il quantile (float) e non valida le chiavi. | Sezione tipizzata `PromotionConfig` retrocompatibile (B.2). |
| K13 | V1 (fuori campione sui 90 giorni) | Il docstring di `HoldoutBacktester` afferma che nessuna versione si addestra sull'holdout, ma il manifest non lo prova. | Il model card dichiara `data.final_train_end` (max `label_window_end` dei sample di training del modello `final`); V1 = `final_train_end <= inizio finestra - embargo` per entrambi. Se Sonnet scopre che il `final` si addestra sull'holdout: STOP e nuovo CONFLITTO (non inventare un'alternativa). |
| K14 | B2 del quant: blocco automatico degli ingressi su difetti notturni | E' un percorso automatico nuovo (learner/report -> kill switch); il mandato di questa risoluzione e' "raccomandazioni nel report, non azioni". | Oggi: solo raccomandazione e allarme nel report. Il blocco automatico e' la decisione U3 (sezione D). |
| K15 | `F` del quant ("minuti rivisti sostituiti") applicato alla griglia gia' riempita (`load_bars`) | Il Parquet contiene solo i minuti `rest` (`BarStoreWriter`); i minuti senza scambi li ricostruisce `fill_flat_bars` con il close PRECEDENTE. Sostituire un minuto nella griglia riempita lascia i minuti piatti che lo seguono al close VECCHIO: `F` non e' cio' che `load_bars` produrrebbe sulle barre rilette e contiene un salto spurio al minuto reale successivo. E' esattamente la famiglia notturna (minuti rivisti circondati da minuti vuoti). Inoltre una classificazione fatta sulla griglia riempita conta i minuti piatti come "scomparsi" e farebbe fallire R4 per costruzione. | Le revisioni si applicano alle RIGHE REALI dello store; la griglia del mondo rivisto si ricostruisce con la stessa `fill_flat_bars` e gli stessi `start`/`end` del mondo servito. `classify_revisions` riceve solo righe reali (A.1, A.3). |

### A. Criterio sulle revisioni REST

#### A.1 Definizioni normative (cosa si misura)

- **Righe reali**: `real` = righe Parquet dello store del prodotto (`BarStore.read_month`, solo minuti `rest`, colonna `ts` +
  OHLCV), sull'intervallo `[start, end]` che il mondo servito usa per gli indicatori (warm-up compreso). Mai la griglia gia'
  riempita.
- **Mondo servito `S`** = `fill_flat_bars(real, start, end)` (identico a `load_bars` sullo stesso intervallo e con gli stessi
  argomenti; per I2 e' la sequenza consumata dall'engine). Indicatori: `compute_indicators(S)` (in L0 si possono riusare quelli
  gia' in `markets[product].ind` solo se `markets` e' stato caricato con `load_bars` sullo stesso intervallo).
- **Revisioni note**: per ogni giorno auditato, l'insieme classificato da `classify_revisions` (A.3) su righe reali:
  `value` (minuto presente nei due lati con un campo OHLCV diverso), `appeared` (candela in REST, nessuna riga nello store: il
  minuto era stato pubblicato `flat`/`flat_unproven`), `disappeared` (riga nello store, nessuna candela in REST).
- **Mondo rivisto `F`** = `fill_flat_bars(apply_revisions(real, value_di_tutti_i_giorni_auditati_<=_giorno_misurato), start,
  end)`: stessi `start`/`end` di `S`, quindi i minuti piatti dopo un minuto rivisto portano il close riletto, come farebbe
  `load_bars` sulle barre rilette. `appeared` e `disappeared` NON si applicano (sono R4). `F.index` deve essere uguale a
  `S.index` (le revisioni di valore non aggiungono ne' tolgono minuti): altrimenti `ValueError`. Lo store non si tocca mai (I2).
- **Contesto BTC**: per ETH/SOL lo stato di ciascun mondo usa gli indicatori BTC dello STESSO mondo (`btc_ind_S` con `S`,
  `btc_ind_F` con `F`), con gli stessi `btc_rows`.
- **Stati del book** per (prodotto, giorno UTC): un minuto `t` (= `ts_bar_close`) e' LONG se il book aveva in quel minuto una
  posizione aperta sul prodotto secondo il ledger dry, altrimenti FLAT. Senza book (certificazione del soak): FLAT su ogni minuto.
  - FLAT: `X_flat = flat_matrix(ind_X, rows, product, label, btc_ind_X, btc_rows)`.
  - LONG: `long_matrix(X_flat_rows, ts_close, close_X, entry_px=, stop_loss=, take_profit=, atr_entry=, opened_at=)`, con i
    numeri della posizione presi dalla stessa sorgente che usa `PositionInput.from_view` (Sonnet: individuala e riusala; nessun
    secondo accesso al ledger con formule proprie).
- **Risposte**: `decider.answers_batch(X)` della versione del book (le stesse risposte calibrate dell'engine).
- **Probabilita' mascherate**: due funzioni pubbliche nuove in `modules/decision/policy.py`,
  `masked_buy_prob(p_action) -> ndarray` (BUY / (BUY + HOLD)) e `masked_sell_prob(p_action) -> ndarray` (SELL / (SELL + HOLD)),
  estratte da `buy_mask`/`sell_mask`, che le chiamano (refactoring senza cambio di comportamento). Nessun indice 0/1/2 scritto
  fuori da `policy.py`.
- **Spostamenti**: FLAT `de = |p_edge(F) - p_edge(S)|`, `db = |masked_buy(F) - masked_buy(S)|`; LONG
  `de` come sopra, `ds = |masked_sell(F) - masked_sell(S)|`; `m = max` dei definiti. Valore NaN in uno solo dei due mondi ->
  `m = +inf`; NaN in entrambi -> `m = 0` e conteggio diagnostico `states_nan_both`.
- **Azionabilita'** (`act`), calcolata con i token di ciascun mondo:
  - FLAT: `act = buy_mask(p_action, vol_pctile) & fee_ok & ev_ok`, con `W = k_tp * atr - cost_tp`,
    `L = k_sl * atr + cost_sl` (token `atr_14_<atr_tf>_bps`, `cost_tp_bps`, `cost_sl_bps`; `k_tp`, `k_sl`, `atr_tf` dal
    `label_config` della versione), `fee_ok = W >= risk.min_net_edge_bps`, `EV = p_edge * W - (1 - p_edge) * L`,
    `ev_ok = (EV > risk.ev_min_bps) & (EV >= p_be_buffer * (W + L))`. Sono le formule della sez. 0.3: se in `modules/risk`
    esiste una funzione pura scalare che le implementa, la versione vettoriale ne e' il gemello e un test le confronta
    (`test_actionable_mask_matches_risk_scalar`), come gia' `buy_mask` con `resolve`.
  - LONG: `act = sell_mask(p_action)`.
  - `flip = act(S) != act(F)`.
  - Policy `untunable` (o versione senza `policy.json` caricabile): R3 = `unmeasured`, nota `policy_untunable`.
  - Decider senza `answers_batch` (backend remoto/LLM): R1-R3 = `unmeasured`, nota `decider_without_batch`.

#### A.2 Soglie (per prodotto, per giorno UTC, per versione di modello in uso nel book)

| Criterio | Formula | Soglia (config) | Esito |
|----------|---------|-----------------|-------|
| R1 | `shifted = #{m > revision_shift_threshold}`; `share = shifted / states` | `share <= max_revision_shift_share` | 0.01 (`>` stretto) / 0.005 (`<=`) |
| R2 | `max_shift = max m` | `max_shift <= max_revision_shift` | 0.02 |
| R3 | `flip_share = #flip / states` | `flip_share <= max_revision_flip_share` | 0.001 |
| R4 | `appeared + disappeared` | `== 0` | nessuna chiave: zero fisso |
| Struttura | `out_of_order + duplicates` | `== 0` | invariato |

"Versione in uso" = la `policy.policy_version` dei `DecisionRecord` del book in quel giorno (non l'alias attuale: un alias
cambiato in giornata ha effetto solo al reload delle 00:30). Un giorno con due versioni (00:00-00:30 ancora la precedente) si
misura con ciascuna, su tutti gli stati del giorno; vale il peggiore. Book valutati: `champion` e `challenger` (i book con
decider `registry:*`).

#### A.3 Dove vive il calcolo

1. `modules/market/bars.py` (sola lettura, nessuna dipendenza nuova):
   ```python
   @dataclass(frozen=True)
   class RevisionSet:
       value: pd.DataFrame            # indice ts UTC, colonne OHLCV RILETTE dei minuti rivisti di valore
       stored_value: pd.DataFrame     # stesse righe, valori dello store (diagnostica)
       appeared: pd.DatetimeIndex     # solo in REST
       disappeared: pd.DatetimeIndex  # solo nello store

   def classify_revisions(stored: pd.DataFrame, fresh: pd.DataFrame, start: datetime, end: datetime) -> RevisionSet
       # stored = righe reali dello store (Parquet), MAI la griglia di load_bars/fill_flat_bars;
       # fresh = output di candles_to_frame; stessa finestra [start, end) di rest_revisions; confronto esatto
   @dataclass(frozen=True, slots=True)
   class RevisionStats:  # campi esistenti invariati e nello stesso ordine, poi:
       value_revised: int = 0; appeared: int = 0; disappeared: int = 0
   def rest_revisions(...)  # reimplementata sopra classify_revisions: revised, ratio, max_close_diff_bps IDENTICI a oggi
   ```
2. `modules/dryrun/revision_impact.py` (nuovo; nessuna rete fuori dalla CLI):
   ```python
   @dataclass(frozen=True)
   class ImpactThresholds:
       shift: float; max_share: float; max_shift: float; max_flip_share: float
       @classmethod
       def from_config(cls, g: GateOpsConfig) -> ImpactThresholds
   @dataclass(frozen=True)
   class PositionSpan:
       product: str; opened_at: datetime; closed_at: datetime | None
       entry_px: float; stop_loss: float; take_profit: float; atr_entry: float
   @dataclass(frozen=True)
   class ImpactDay:
       product: str; day: date; version: str
       states: int; flat_states: int; long_states: int
       shifted: int; share: float; max_shift: float
       p99_de: float; p99_db: float; p99_ds: float | None
       flips: int | None; flip_share: float | None
       r3: Literal["measured", "policy_untunable", "decider_without_batch"]
       states_any_token_diff: int; token_diff_by_column: dict[str, int]; states_nan_both: int

   def apply_revisions(real: pd.DataFrame, value_revisions: pd.DataFrame) -> pd.DataFrame
       # real = righe reali dello store (colonna ts + OHLCV); copia con gli OHLCV dei minuti rivisti sostituiti;
       # KeyError se un ts rivisto non e' fra le righe reali; input mai modificato
   def revised_world(real: pd.DataFrame, value_revisions: pd.DataFrame, start: datetime, end: datetime
                     ) -> tuple[pd.DataFrame, pd.DataFrame]
       # (S, F) = (fill_flat_bars(real, start, end), fill_flat_bars(apply_revisions(real, value_revisions), start, end));
       # ValueError se S.index != F.index. fill_flat_bars e' quella di modules.market.backfill (nessuna copia)
   def impact_day(...) -> ImpactDay              # nucleo puro: matrici S/F + risposte + maschere -> numeri
   def revision_impact(served: Mapping[str, pd.DataFrame], revised: Mapping[str, pd.DataFrame], days: Sequence[date],
                       decider: Any, policy: PolicyConfig | None, label: LoadedLabelConfig,
                       positions: Sequence[PositionSpan], risk: RiskConfig, version: str) -> list[ImpactDay]
       # served/revised = coppie (S, F) di revised_world per prodotto
   def verdict(days: Sequence[ImpactDay], thr: ImpactThresholds) -> dict[str, Literal["pass", "fail", "unmeasured"]]
       # chiavi "R1", "R2", "R3"
   def main(argv) -> int   # python -m modules.dryrun.revision_impact certify ... (A.6)
   ```
3. `modules/dryrun/learner.py`, L0: terza parte `revision_impact` accanto a `rest_revisions` e `state_parity`.
   - `_rest_audit` usa `classify_revisions` (lo `stored` che gia' legge da `read_month` e' gia' fatto di righe reali) e scrive
     atomicamente `data/reports/learner/revisions/<audited_day>.parquet` (colonne `product, ts, kind
     ('value'|'appeared'|'disappeared'), open, high, low, close, volume` riletti (NaN per `disappeared`),
     `stored_close, stored_volume`); il dettaglio JSON aggiunge `value_revised`, `appeared`, `disappeared`.
   - `_revision_impact(audited)`: carica TUTTI i file `revisions/*.parquet` con giorno `<= audited`, costruisce `(S, F)` con
     `revised_world` sulle righe reali dello store, chiama `revision_impact` per ogni (book, versione) del giorno e scrive per
     prodotto `{"books": {"champion": [ImpactDay...], "challenger": [...]}, "verdict": {...}}`. Senza `DecisionRecord` del
     giorno: `Skip("no_decisions")`. Giorni senza audit precedente: elencati in `days_without_audit` (ultimi 30 giorni),
     diagnostica.
4. `modules/dryrun/report.py`: legge e valuta (A.5). 5. `modules/dryrun/recorder.py`: verdetto del soak (A.6).

#### A.4 Configurazione (`GateOpsConfig`, retrocompatibile)

```python
max_rest_revision_ratio: float = Field(default=0.005, gt=0, le=1)   # INVARIATA nel nome; ora soglia di ALLARME (diagnostica)
revision_shift_threshold: float = Field(default=0.01, gt=0, lt=1)   # R1: stato spostato se m > 0.01
max_revision_shift_share: float = Field(default=0.005, gt=0, le=1)  # R1: quota massima
max_revision_shift: float = Field(default=0.02, gt=0, lt=1)         # R2
max_revision_flip_share: float = Field(default=0.001, ge=0, le=1)   # R3
# model_validator: revision_shift_threshold <= max_revision_shift
```
Un YAML esistente con la sola `max_rest_revision_ratio` resta valido. Il nome non si cambia (con `extra="forbid"` un rinomino
romperebbe le config esistenti); il commento nel codice, in `config/config.example.yml` e nella spec 5.2 dice "allarme".

#### A.5 Gate nuovo e diagnostica vecchia: come si distinguono

- `report._bars`: per prodotto aggiunge `rest_revisions_value`, `rest_revisions_appeared`, `rest_revisions_disappeared` e
  `revision_impact: {book: {"version", "states", "share", "max_shift", "flip_share", "r3"}}` (versione peggiore se due).
  `rest_revisions`, `rest_revision_ratio`, `rest_revision_max_close_bps` restano.
- `evaluate_gate_ops`: il criterio `rest_revision_ratio` ESCE dai `criteria` ed entra in un elenco separato
  `"diagnostics": [{"name": "rest_revision_ratio", "value": <max>, "alarm_threshold": 0.005, "alarm": <bool>,
  "note": "diagnostica, non entra nello stato del gate"}]`. Entrano quattro criteri per il `book` valutato:
  `revision_shift_share` (R1, max dei giorni), `revision_shift_max` (R2), `revision_decision_flips` (R3; `r3 != "measured"` ->
  `unmeasured` con la nota), `rest_completeness` (R4, somma). `bars_in_order_no_duplicates` invariato. Un giorno senza il dato
  -> `unmeasured` (regola gia' in vigore: un criterio non misurato non e' superato).
- `_anomalies`: il ratio NON conta piu' come anomalia; se supera la soglia va in `doc["alarms"]` (`"rest_revision_ratio:<p>"`).
  R4 > 0 -> anomalia operativa (globale, non dipende dal modello). R1-R3 falliti -> `anomalies.revision_by_version:
  {version: ["R1:<p>", ...]}`; `ReportShadowEvidence` aggiunge ai `parity_anomalies` solo `len(revision_by_version[version])`.
- Markdown: la tabella Bars mostra R1-R4 per il champion e la colonna `rest_revision_ratio` con "ALLARME (diagnostica)"
  quando scatta.
- Conseguenza dichiarata: con un champion a policy non tarabile R3 e' `unmeasured` e G-DRY-OPS resta al massimo `INCOMPLETE`.
  E' corretto: quel champion non puo' comunque andare in R0 (GE != GO).

#### A.6 Soak P0: verdetto futuro e certificazione di quello concluso

- `soak_verdict` (soak futuri): la chiave `rest_revision_ratio_within_limit` esce dal `verdict`; entra
  `rest_bars_complete` (R4: `appeared == disappeared == 0` per ogni prodotto; nulla misurato -> False). Il ratio resta in
  `checks["rest_revisions"]` e si aggiunge `checks["rest_revision_alarm"] = {pid: ratio > max_rest_revision_ratio}`.
  R1/R2 del soak NON si calcolano in `run_soak` (il soak non carica modelli): li calcola sempre la CLI `certify` sotto, per il
  soak concluso come per i futuri. Docstring di `soak_verdict` aggiornato in questo senso.
- **Certificazione del soak gia' concluso** (nessun verdetto riscritto):
  `python -m modules.dryrun.revision_impact certify --soak-report data/reports/p0_soak.json --model-dir
  models/baseline/baseline-lgbm@2026-10-18.2 --subset final --out data/reports/p0_soak_certification.json`
  - finestra: `[floor_minute(started_at), floor_minute(updated_at))` letti da `p0_soak.json`; rilettura REST con
    `CoinbaseCandleSource` + `candles_to_frame`, chunk da 300, END esclusivo; tutti i minuti hanno eta' >= 15 min;
  - `classify_revisions` sulle righe reali dello store; `(S, F)` con `revised_world` (K15), warm-up degli indicatori come in
    `load_markets`;
  - stati: FLAT su ogni minuto della finestra (nessun book), suddivisi per giorno UTC (anche parziali);
  - `p0_soak.json` e' letto in sola lettura; il suo sha256 entra nella certificazione (il legame va dalla certificazione al
    soak, mai il contrario);
  - exit code 0 se `p0_bars_criterion == "pass"`, 1 se `"fail"`, 2 errore di configurazione.
  Schema del file:
  ```json
  {"v": "chronos.p0_bars_certification.v1", "created_at": "<UTC Z>",
   "soak_report": {"path": "data/reports/p0_soak.json", "sha256": "<hex>", "started_at": "...", "updated_at": "...",
                   "verdict_rest_revision_ratio_within_limit": false},
   "criterion": {"superseded": "rest_revision_ratio <= 0.005 per prodotto (gate)",
                 "by": "R1-R4, spec 5.1.7 riga 'Barre REST' (v2.11)", "decided_on": "2026-10-20",
                 "decision": "docs/decisions/2026-10-20-revisioni-rest-e-promozione.md",
                 "reason": "il ratio non misura lo skew sugli stati ne' sulle decisioni e il suo unico rimedio (alzare S) non agisce sulle revisioni rilette ore dopo"},
   "window": {"start": "...", "end": "...", "reread_at": "...", "min_age_min": 15},
   "model": {"dir": "...", "version": "...", "subset": "final", "policy": "untunable|tuned"},
   "thresholds": {"shift": 0.01, "max_share": 0.005, "max_shift": 0.02, "max_flip_share": 0.001},
   "products": {"ETH-EUR": {"diagnostic": {"minutes": 0, "revised": 0, "ratio": 0.0, "alarm": true, "max_close_diff_bps": 0.0},
                            "R4": {"appeared": 0, "disappeared": 0, "status": "pass"},
                            "days": [{"day": "2026-10-19", "states": 0, "shifted": 0, "share": 0.0, "max_shift": 0.0,
                                      "R1": "pass", "R2": "pass", "R3": "unmeasured", "R3_note": "policy_untunable"}],
                            "R1": "pass", "R2": "pass", "R3": "unmeasured", "R4": "pass"}},
   "p0_bars_criterion": "pass",
   "r3_deferred_to": "G-DRY-OPS del primo champion con policy tarabile (spec 5.1.7)"}
  ```
  `p0_bars_criterion = "pass"` se e solo se R1, R2 e R4 sono `pass` per ogni prodotto e ogni giorno.
- **Testi esatti** (da scrivere solo DOPO l'esecuzione, e solo se `pass`):
  - `data/reports/p0_soak.json`: **nessuna nota**, il file non si apre in scrittura.
  - `tasks/todo.md`: "- [x] P0, criterio barre REST: certificato il <data> in `data/reports/p0_soak_certification.json`
    (R1, R2, R4 superati per BTC-EUR, ETH-EUR, SOL-EUR su ogni giorno della finestra 18-20/10; R3 non valutabile con policy
    non tarabile, rinviato al G-DRY-OPS del primo champion tarato). `data/reports/p0_soak.json` resta invariato:
    `rest_revision_ratio_within_limit: false` (ETH-EUR 1,09%) e' la misura corretta contro il criterio in vigore al
    2026-10-18, superato dalla decisione `docs/decisions/2026-10-20-revisioni-rest-e-promozione.md`."
  - spec 0.5, dopo il criterio di uscita P0: "Il soak P0 del 18-20/10/2026 (`data/reports/p0_soak.json`) e' stato misurato
    con il criterio precedente (`rest_revision_ratio <= 0.005`, non superato da ETH-EUR con 1,09%) e resta invariato; il
    criterio attuale e' certificato sulla stessa finestra e sullo stesso store in `data/reports/p0_soak_certification.json`
    (decisione del 2026-10-20)."
  - Se `fail`: nessuna nota di superamento; nuovo `.claude/CONFLITTO.md` con il file di certificazione (protocollo CLAUDE.md).
- **Significato per l'uscita da P0**: il criterio "barre REST" di P0 e' superato se la certificazione e' `pass`; nessun nuovo
  soak di 48 h (stessa finestra, stesso store; il soak si rifa' solo se R4 fallisce o se cambia S). Gli altri criteri di P0
  (backfill, parita' batch/incrementale, 48 h di WS senza buchi non gestiti) restano quelli di `p0_soak.json` e non cambiano.
  R3 non e' un debito di P0: e' un criterio di G-DRY-OPS, obbligatorio prima di R0. I numeri R1/R2 della certificazione
  possono differire di poco da quelli degli script (K6, K15): vale la certificazione.

#### A.7 Coerenza con I2 e con "il criterio non si allenta"

- **I2: coerente.** Lo store resta la registrazione di cio' che e' stato consumato; il mondo rivisto e' una copia in memoria
  usata solo per misurare (`apply_revisions` restituisce una copia; test che verifica lo sha256 dei file Parquet prima e dopo).
  Le revisioni persistite stanno in `data/reports/learner/revisions/`, non in `data/bars/`.
- **Non e' un allentamento**, per quattro ragioni verificabili: (1) misura direttamente le grandezze che la soglia del
  2026-10-18 dichiarava di proteggere (skew sugli stati e sulle decisioni), con lo stesso budget 0.005; (2) e' piu' severo per
  un modello sensibile: un caso sintetico che oggi passa il ratio (0,4% di minuti rivisti) deve far fallire R1 (test
  obbligatorio, A.9); (3) si ripete per ogni versione ogni giorno e nello shadow di ogni challenger, quindi la debolezza della
  misura di P0 (modello poco sensibile) non si trascina: il gate che porta al capitale (G-DRY-OPS -> R0) la rimisura col
  champion vero; (4) il ratio resta calcolato, pubblicato e con allarme alla stessa soglia, e R4 (completezza) resta a zero.
- **Limite dichiarato**: per la finestra del soak R3 non e' misurabile e R1/R2 dipendono da un LightGBM a bassa sensibilita'.
  Per questo P0 certifica la pipeline delle barre, non l'innocuita' delle revisioni per ogni modello futuro.

#### A.8 Mitigazione sul sistema

Opzione (a) "nessuna", con R1-R4 come trigger: accettata. (b), (c), (d1), (d2) respinte per i motivi del quant. Se R1 o R2
falliscono: nuovo CONFLITTO con i dati del giorno (classificazione, eta' della revisione); la scala di taratura di S si applica
solo se la classificazione mostra revisioni di assestamento (entro pochi minuti dalla chiusura).

#### A.9 Test richiesti (Parte A)

`tests/test_bars.py`:
- `test_classify_revisions_value_appeared_disappeared`
- `test_classify_revisions_window_end_exclusive` (candela a `end` ignorata: riproduce l'artefatto 05:40)
- `test_classify_revisions_on_store_rows_counts_no_flat_minute` (i minuti senza scambi, assenti dal Parquet e da REST, non
  sono ne' `appeared` ne' `disappeared`)
- `test_rest_revisions_counts_unchanged` (stessi `revised`, `ratio`, `max_close_diff_bps` di prima su una fixture fissa)

`tests/test_decision_policy.py`:
- `test_masked_probs_match_buy_and_sell_mask`

`tests/test_revision_impact.py` (nuovo):
- `test_apply_revisions_returns_copy_and_store_untouched` (sha256 dei Parquet invariato)
- `test_apply_revisions_ignores_appeared_and_disappeared`
- `test_flat_after_revised_minute_carries_revised_close` (minuto reale rivisto seguito da 3 minuti senza scambi: in `F` i 3
  minuti piatti hanno il close riletto e `F` coincide con `fill_flat_bars` delle righe rilette; K15)
- `test_revised_world_same_grid_as_served` (`S.index == F.index`; `S` uguale a `load_bars` sullo stesso intervallo)
- `test_late_trade_on_quiet_minute_moves_daily_tokens`: 3 giorni sintetici di barre 1m; alle 01:36 del giorno 3 il minuto
  passa da volume 0,026 a 2,205 e close +16 bps; atteso: `vol_z_1d` e `dist_vwap_1d` diversi su molti stati successivi dello
  stesso giorno, rendimenti diversi solo dentro la loro finestra, nessuna differenza prima di 01:36
- `test_insensitive_decider_passes_r1_r2` (decider finto con risposte costanti)
- `test_sensitive_decider_fails_r1_with_low_ratio` (stesso caso con <= 0,4% di minuti rivisti e un decider finto con
  `p_edge` funzione ripida di `vol_z_1d`: R1 fallisce; e' la prova di (ii) in A.7)
- `test_thresholds_boundaries` (m = 0.01 non conta; share = 0.005 passa; max = 0.02 passa; 0.0200001 fallisce)
- `test_r3_unmeasured_with_untunable_policy`
- `test_flip_counted_when_actionability_differs` (FLAT con EV al bordo; LONG con `sell_mask` al bordo)
- `test_actionable_mask_matches_risk_scalar`
- `test_btc_context_from_same_world`
- `test_long_states_use_position_numbers_and_world_close`
- `test_nan_on_one_side_is_infinite_shift`
- `test_certify_writes_schema_and_never_writes_soak_report` (sorgente di candele finta; sha256 di `p0_soak.json` invariato e
  uguale a quello scritto)
- `test_certify_exit_codes`

`tests/test_learner.py`:
- `test_l0_persists_revisions_file`
- `test_l0_revised_world_applies_all_known_revisions`
- `test_l0_impact_uses_version_from_decision_records`
- `test_l0_impact_two_versions_in_one_day_takes_worst`

`tests/test_dryrun_report.py` (o il file di test esistente del report):
- `test_gate_ops_ratio_is_diagnostic_not_criterion` (ratio 0.011 con R1-R4 ok: il gate non fallisce, `alarm` vero)
- `test_gate_ops_r1_r2_r3_r4_criteria`
- `test_gate_ops_r3_unmeasured_gives_incomplete`
- `test_gate_ops_r4_appeared_fails`
- `test_anomalies_ratio_goes_to_alarms`
- `test_shadow_evidence_counts_only_own_version`

`tests/test_config.py`: `test_gate_ops_revision_defaults`, `test_gate_ops_old_yaml_still_valid`,
`test_gate_ops_shift_threshold_not_above_max`.

`tests/test_recorder.py`: `test_soak_verdict_rest_bars_complete`, `test_soak_verdict_has_no_ratio_key`,
`test_soak_ratio_alarm_in_checks`.

I test esistenti che fissano il vecchio comportamento (gate o anomalia sul ratio, chiave del verdetto del soak) si
AGGIORNANO alla semantica nuova nello stesso commit, con il motivo nel messaggio; non si cancellano senza sostituto.

#### A.10 Spec e documenti da cambiare (dopo U1)

- Intestazione: `Versione: 2.11-draft | Data: 2026-10-20`.
- Changelog 2.11 (nuova riga dopo 2.10): "Changelog 2.11 (revisioni REST e promozione, conflitto P1e): il criterio 'Barre REST'
  di G-DRY-OPS e di P0 passa dal rapporto di minuti rivisti (`rest_revision_ratio`, ora diagnostica con allarme a 0.005)
  all'impatto delle revisioni sugli stati e sulle decisioni della versione in uso (R1 quota di stati con spostamento > 0.01
  <= 0.005, R2 spostamento massimo <= 0.02, R3 flip <= 0.001, R4 completezza), calcolato da L0 sulle barre servite e su una
  copia con le revisioni note applicate alle righe reali e la griglia ricostruita con `fill_flat_bars` (lo store non cambia,
  I2); la scala di taratura di S vale solo per le revisioni di assestamento; certificazione separata del soak P0; regola L5
  riscritta (validita' del confronto, GE = GO del challenger, bootstrap a blocchi accoppiato sull'OOS comune, filtro di recenza
  con PnL > 0, drawdown <= 5%, shadow con R1-R4); L5 valuta il challenger in carica prima del refit; rollback solo da CLI
  locale con effetto al reload e raccomandazioni nel report."
- 0.5: criterio di uscita P0, sostituire "48 h di `BarService` con `rest_revision_ratio <= 0.005` per prodotto, misurato
  rileggendo da REST i minuti con eta' >= 15 min" con "48 h di `BarService` con R4 (zero minuti comparsi o scomparsi) e R1, R2
  della sez. 5.1.7 (riga 'Barre REST') per prodotto e per giorno UTC con il modello in uso, misurati rileggendo da REST i minuti
  con eta' >= 15 min e certificati con `python -m modules.dryrun.revision_impact certify`; R3 si valuta in G-DRY-OPS;
  `rest_revision_ratio` e' diagnostica (allarme se > 0.005)", piu' la nota di A.6. Nel paragrafo I2 aggiungere: "Le revisioni
  note si applicano solo a una copia in memoria delle righe reali, con la griglia ricostruita da `fill_flat_bars`, per misurarne
  l'impatto (L0), mai allo store."
- 2.3 A.2: la frase che lega la scala di taratura di S al `rest_revision_ratio` diventa "la scala di taratura si applica quando
  R1 o R2 falliscono e la classificazione mostra revisioni di assestamento".
- 5.1.6: riga L0 (impatto delle revisioni, file `revisions/<giorno>.parquet`, sezione `bars.revision_impact` del report); riga L5
  (B.3); regole invarianti, rollback (B.6).
- 5.1.7: riga "Barre REST" = tabella A.2 in prosa, con la definizione di `m` e di `flip` di A.1 e la frase "`rest_revision_ratio`
  e' diagnostica nel report, allarme se > 0.005"; dopo la rampa, il paragrafo delle raccomandazioni di rollback (B.7).
- 5.2: chiavi nuove di `gate_ops` (A.4) e di `learner.promotion` (B.2); JSON del report: `bars.<p>.revision_impact`,
  `rest_revisions_value/appeared/disappeared`, `alarms`, `gate_ops.diagnostics`, `rollback_advice`.
- 3.5 punto 11: `evaluate` scrive `oos_trades.jsonl` e `oos_spans.json`; punto 9: `data.final_train_end` nel model card.
- Appendice A: riga P0 invariata (rimanda alla 0.5).
- `docs/decisions/2026-10-18-barre-rest.md`, in testa: "> Q3.2 (criterio 0.005 come gate) superato dalla decisione
  2026-10-20 (`docs/decisions/2026-10-20-revisioni-rest-e-promozione.md`): il valore 0.005 resta come allarme diagnostico; il
  gate misura l'impatto delle revisioni (R1-R4)."
- A fine implementazione: questa risoluzione si copia in `docs/decisions/2026-10-20-revisioni-rest-e-promozione.md` e
  `.claude/CONFLITTO.md` si cancella (CLAUDE.md, punto 6).


## Annotazioni dell'implementazione della parte A (2026-10-23)

Punti in cui il codice reale ha richiesto una lettura diversa dalla lettera della risoluzione; si e' seguito il codice.

- Finestra di `certify`: da `floor_minute(started_at)` a `min(floor_minute(updated_at), bars.<prodotto>.next_minute)` del soak report. Oltre `next_minute` lo store non ha righe (il soak ha smesso di pubblicare) e REST ha candele: sarebbero "comparsi" falsi (il bordo che la ricerca aveva visto come `fresh_only` 05:40). Lo stato di un giorno e' il giorno UTC dell'istante di chiusura `t = ts + 1 min`.
- Schema del file di certificazione: la chiave `R4` compare due volte nell'esempio della risoluzione (oggetto e stringa). Si e' tenuto l'oggetto `{"appeared", "disappeared", "status"}`; R1, R2, R3 per prodotto sono stringhe.
- `vol_z_1d` e' lo z-score del volume in quote di ogni giorno UTC COMPLETO: una revisione del giorno D lo sposta dal giorno D+1 (non "nello stesso giorno" come scriveva A.9 per `test_late_trade_on_quiet_minute_moves_daily_tokens`); `dist_vwap_1d_bps` si sposta nelle 24 ore successive. Il test verifica questo.
- Stati LONG: `ImpactDay` li marca con `opened_at <= t < closed_at` del ledger. Il ledger conserva `atr_at_entry` solo per la posizione aperta; per un trade chiuso lo si ricostruisce da `(tp - entry) / k_tp` (esatto a meno del tick, e identico nei due mondi: non puo' creare uno spostamento).
- `max_shift = +inf` (NaN in un solo mondo) si scrive come `1.0` nei JSON (il massimo spostamento possibile di una probabilita'), perche' `NaN`/`Infinity` non sono JSON.
- Il ratio del soak report e quello della certificazione differiscono di poco (BTC 13/2.622 contro 13/2.632; ETH 26/2.386 contro 27/2.396): il soak rileggeva solo fino a `now - 15 min` al suo termine (05:29), la certificazione fino a 05:42.
- Con K15 (revisioni sulle righe reali, griglia ricostruita) gli spostamenti sono piu' grandi di quelli degli script di ricerca: SOL passa da 0 a 0,0153 e gli stati ETH con spostamento > 0,01 da 1 a 7, perche' i minuti piatti dopo un minuto rivisto portano il close riletto. Vale la certificazione.
- `ReportShadowEvidence` (learner.py) somma a `parity_anomalies` solo `len(anomalies.revision_by_version[versione])`.

## P1e-bis - R1 e R3 a blocchi di 1.440 stati, certificazione del soak P0 (2026-10-23)

Stato: implementata il 2026-10-23. Conflitto, risposta di `trader-quant` e risoluzione di `architetto-opus` (K16, regola v2, specifica di `certify` v2, test): `.claude/CONFLITTO_certificazione_P0.md` (copia nella sezione seguente).

### Decisioni dell utente
- U1 = si: regola v2 (R1 e R3 su blocchi di 1.440 stati consecutivi, `ImpactThresholds.block_states` con default `STATES_PER_DAY`, nessuna chiave di config; precedenza fail, unmeasured, pass; R2 e R4 per giorno UTC).
- U2 = non certificare il soak 18-20/10/2026 con una regola scritta dopo averne visto il `fail`: `data/reports/p0_soak_certification.json` (v1, `fail`, sha256 a1b4b1e5...) resta invariato; nessuna nota di superamento e nessun file `.v2.json`; `certify` su quel soak e` ammessa solo con `--info-only` (guardia `NOT_CERTIFIABLE` nel codice). Informativo (2026-10-23, rilettura identica alla prima): v2 `pass` (blocchi ETH-EUR al massimo 5 stati su 1.440, 3 revisioni indipendenti), v1 `fail`. La certificazione di P0 arrivera` da giorni UTC interi del dry run, dove v1 e v2 coincidono.

### Testo della risposta (copia)

## Risposta trader-quant

Autore: trader-quant. Data: 2026-10-23. Ambito: Q1, Q3. Legenda: [M] misurato (fonte: `data/reports/p0_soak_certification.json`
salvo diversa indicazione); [D] derivato con una formula da numeri misurati; [G] conoscenza generale; [?] dato mancante.
Vincoli invariati: solo spot, long-only, nessuna leva, copertura 100%. Nessuna soglia cambia: 0,01 / 0,005 / 0,02 / 0,001
(A.2) restano quelle; nessun gate economico (fee floor, EV gate, G0-G4, GE, G-DRY-ECON) e' toccato.

### Q1.0 Dichiarazione di metodo

- La regola sotto e' scritta DOPO aver visto il `fail`, e lo dichiaro. La protezione contro il "ritocco" non e' la cronologia
  ma due proprieta' verificabili: (a) deriva da un principio che non dipende dal risultato: **la certificazione deve applicare
  lo stesso test statistico del gate di produzione**, che in produzione misura R1 su giorni di 1.440 stati (D-1 intero);
  (b) e' **simmetrica**: puo' far fallire casi che la regola "giorno parziale cosi' com'e'" faceva passare, e non trasforma
  mai in `pass` il `fail` di un giorno intero (un giorno intero e' un blocco e si valuta invariato).
- Controfattuale richiesto: se il risultato fosse stato `pass` con 1/343 (0,29%) sul giorno parziale e `fail` su un giorno
  intero, ragionamento e regola sarebbero identici e il verdetto resterebbe `fail`; in piu' il giorno parziale "passato"
  verrebbe rivalutato sul suo blocco di 1.440 stati e potrebbe fallire anch'esso. Il difetto della regola per giorno
  parziale esiste in entrambe le direzioni (Q1.2: falso `fail` 20-45% sotto il budget, falso `pass` 14-18% al doppio del
  budget) e andava sollevato anche con un `pass`.

### Q1.1 Modello statistico di R1

- Grandezza protetta: il tasso vero `p` di stati con `m > 0,01` (skew materiale), budget 0,005 per 24 ore di stati. R1 e' un
  test puntuale: `p_hat = X / n <= 0,005`, con `X` = stati spostati e `n` = stati del blocco.
- Stati indipendenti [G]: `X ~ Binomiale(n, p)`, `SE(p_hat) = sqrt(p (1 - p) / n)`, errore relativo `1 / sqrt(n p)`.
- Autocorrelazione [M]: i due stati 02:33 e 02:34 del 20-10 vengono dalla stessa revisione (minuto 02:30) propagata dai
  minuti piatti. Gli eventi indipendenti sono le revisioni materiali, non gli stati. Modello Poisson composto [G]: `N` revisioni
  materiali ~ Poisson(`mu n`), ciascuna con `c_i` stati spostati; `Var(X) = mu n E[c^2]`, quindi effetto di disegno
  `Deff = E[c^2] / E[c]` (= `c` se `c` e' costante) e `SE` va moltiplicato per `sqrt(Deff)`. Uso `c = 2` [M per l'unico
  cluster identificato; ? per i 5 stati del 19-10, di cui non conosco i minuti rivisti di origine].
- Discretezza: stati ammessi = `floor(0,005 n)`: n = 343 -> 1; 720 -> 3; 1.096 -> 5; 1.440 -> 7; 1.783 -> 8; 2.879 -> 14.

Errore di campionamento [D] (punti percentuali; "indip. / c = 2"):

| n | stati ammessi | revisioni ammesse (c = 2) | SE(p_hat) a p = 0,24% | SE(p_hat) a p = 0,35% | stati attesi a p = 0,20-0,35% |
|---:|---:|---:|---|---|---|
| 343 | 1 | 0 (una sola revisione fallisce) | 0,26 / 0,37 | 0,32 / 0,45 | 0,69-1,20 |
| 720 | 3 | 1 | 0,18 / 0,26 | 0,22 / 0,31 | 1,44-2,52 |
| 1.440 | 7 | 3 | 0,13 / 0,18 | 0,16 / 0,22 | 2,88-5,04 |

### Q1.2 Errori del test in funzione di n [D] (Poisson; "indip. / cluster c = 2")

| n | P(fail) con p vero = 0,24% (meta' budget) | P(fail) con p = 0,35% | P(pass) con p = 1,0% (doppio budget) |
|---:|---|---|---|
| 343 | 0,20 / 0,34 | 0,34 / 0,45 | 0,14 / 0,18 |
| 720 | 0,10 / 0,21 | 0,25 / 0,36 | 0,07 / 0,13 |
| 1.440 | 0,025 / 0,10 | 0,14 / 0,25 | 0,025 / 0,07 |

Lettura:
- Su 343 stati, con cluster di 2, il test non e' "quota <= 0,5%" ma "zero revisioni materiali in 5,7 ore": e' un criterio a
  tolleranza zero, cioe' un ALTRO criterio rispetto a quello deciso (budget 0,5% su 24 ore). Il giorno parziale non e' lo
  stesso test con meno dati: e' un test diverso, piu' severo verso un modello buono (falso `fail` 1 volta su 3) e piu'
  permissivo verso un modello cattivo (falso `pass` quasi 1 volta su 5 al doppio del budget).
- `n` minimo [D]: (i) la soglia deve ammettere almeno `k = 3` eventi indipendenti, altrimenti degenera in tolleranza 0/1
  (con 3 eventi l'errore relativo del conteggio alla soglia e' gia' `1 / sqrt(3) = 58%`): `n >= k c / 0,005 = 600 c`, cioe'
  600 stati se indipendenti, 1.200 con `c = 2`; (ii) al tasso osservato deve essere atteso almeno 1 evento indipendente:
  `n >= c / p` = 571-1.000 (c = 2, p = 0,35-0,20%). Il vincolo che lega e' (i): **n_min ~ 1.200**. L'unico blocco naturale
  sopra n_min e uguale al test di produzione e' 1.440. Il 720 proposto nel conflitto non ha base: con c = 2 ammette una sola
  revisione e ha falso `fail` 21-36%.
- Anche a 1.440 il test resta grossolano (con cluster, falso `fail` 25% a p = 0,35%): e' la risoluzione del gate di
  produzione, non la cambio qui. Va tenuto presente quando il gate girera' con un champion piu' sensibile (Q1.5).

### Q1.3 Regola proposta (preferita): R1 e R3 su blocchi di esattamente 1.440 stati consecutivi

1. Giorno UTC con 1.440 stati: blocco = il giorno. In produzione (D-1 intero) **nulla cambia**.
2. Giorno con meno di 1.440 stati (bordi di finestra: inizio/fine di un soak, primo giorno di un book, giorno con stati
   mancanti per fermo): il blocco e' formato dai 1.440 stati consecutivi della serie misurata che contengono il giorno e si
   estendono verso il giorno intero adiacente: per un giorno parziale FINALE gli ultimi 1.440 stati della serie; per uno
   INIZIALE i primi 1.440. In produzione, un D-1 incompleto usa gli ultimi 1.440 stati che terminano a fine D-1 (stati di
   D-2 compresi).
3. Serie misurata con meno di 1.440 stati: R1 e R3 = `unmeasured` (mai `pass`; regola gia' in vigore per i non misurati).
4. `share = shifted_blocco / 1.440`; soglie invariate (0,01 stretto, 0,005, 0,001). Il blocco di un giorno parziale si
   sovrappone al giorno intero adiacente, che si valuta comunque per conto suo: uno stato puo' contare in due blocchi, mai in
   nessuno (nessuna diluizione: il caso peggiore e' sempre visto).
5. R3 ha lo stesso difetto e la stessa cura: `0,001 x 343 = 0,34` -> tolleranza zero sul giorno parziale; `0,001 x 1.440`
   -> 1 flip, come deciso in A1 ("al massimo 1 per prodotto e giorno").
6. R2 (massimo) e R4 (zero fisso) non dipendono da `n`: restano per giorno UTC, anche parziale.
7. Diagnostica obbligatoria, senza soglia: per ogni stato con `m > 0,01`: `ts`, `m`, minuto rivisto di origine (cluster);
   per blocco: numero di cluster indipendenti e dimensione massima. Serve a misurare `Deff` [?] e a rendere verificabile Q1.4.
8. Indicazione operativa: i soak futuri si pianificano allineati a 00:00 UTC e di durata multipla di 24 ore (+ 15 minuti di
   eta'), cosi' il punto 2 non serve.

Alternative valutate:
- (A) Giorno con meno di N stati sommato all'adiacente (N = 720): `n` del blocco varia fra 720 e 2.159, quindi alfa e beta
  variano con il calendario; N = 720 non e' derivato (Q1.2); a inizio finestra la scelta del giorno a cui sommare e'
  arbitraria. Respinta.
- (B) R1 sulla finestra intera: `n` cresce con la durata del soak (2.879 ammette 14 stati) e un'esplosione locale si diluisce
  nei giorni puliti; non e' il test di produzione. Respinta come gate; utile come diagnostica (ETH 7/2.879 = 0,24%).
- (C) Finestra mobile di 24 ore (massimo su tutte le finestre di 1.440 stati): indipendente dal calendario e cattura i
  cluster a cavallo della mezzanotte; e' sempre >= della regola preferita. Ma in produzione cambia il test: il massimo su
  finestre sovrapposte (~2 blocchi indipendenti in 48 ore) circa raddoppia il falso `fail` a parita' di `p` [D, prima
  approssimazione]. E' un irrigidimento non derivato dal budget e modifica il gate di produzione, che Q1 chiede di non
  toccare: stesso vizio, di segno opposto, dello spostare la soglia. Non preferita; ammessa come diagnostica
  (`r1_rolling_max_share`).
- (D) Conteggio per revisione indipendente: e' l'unita' statisticamente corretta, ma richiede una soglia NUOVA (revisioni
  materiali per giorno) che oggi non esiste e verrebbe scelta ora, dopo aver visto i dati. Respinta come gate; il conteggio
  dei cluster entra come diagnostica (punto 7) e si rivaluta con i dati del G-DRY-OPS del primo champion tarato.

Perche' la preferisco: stesso `n` del test che porta al capitale (G-DRY-OPS -> R0), quindi stessi alfa e beta; produzione
invariata; nessun numero nuovo; deterministica; simmetrica (Q1.0).

### Q1.4 Esito sotto la regola proposta (conseguenza calcolata, non motivo)

ETH-EUR [D da M]:
- 10-18 (1.096 stati, parziale iniziale): blocco = primi 1.440 stati della finestra (fino a 10-19 ~05:43): 0 + (stati del
  19-10 prima di ~05:43, <= 5) -> share <= 5/1.440 = 0,347% -> **pass**.
- 10-19 (1.440 stati): 5/1.440 = 0,347% -> **pass** (invariato).
- 10-20 (343 stati, parziale finale): blocco = ultimi 1.440 stati (da 10-19 ~05:43): 2 + (stati del 19-10 dopo ~05:43, <= 5)
  -> share <= 7/1.440 = 0,486% <= 0,5% -> **pass**.
- L'esito non dipende dagli orari [?] dei 5 stati del 19-10: anche nel caso peggiore (tutti dopo 05:43) il blocco ha 7 stati
  = `floor(7,2)`, cioe' **margine zero stati**. I minuti vanno comunque scritti nella certificazione (punto 7).
- BTC 0; SOL al massimo 2/1.440 = 0,139%: pass. R2 e R4 invariati: pass. Quindi `p0_bars_criterion` risulterebbe `pass`.

Credibilita' del `pass`:
- Stima puntuale ETH sulla finestra 7/2.879 = 0,24% [M]; blocco peggiore <= 0,49% [D].
- Limite superiore unilaterale al 95% [D, Poisson]: con stati indipendenti (x = 7 -> 11,8) = 0,41% di 2.879, sotto il budget;
  con cluster c = 2 (3-4 revisioni -> 7,75-9,15 revisioni -> 15-18 stati) = 0,54-0,64%, sopra il budget. Quindi il soak non
  mostra una violazione, ma con l'autocorrelazione **non puo' escluderla al 95%**. E' lo stesso standard di ogni gate
  giornaliero (stima puntuale <= soglia): la certificazione dice cio' che dira' il gate di produzione, ne' piu' ne' meno.
- Il ratio grezzo 1,13% non contraddice il `pass`, lo spiega: ETH ha 2,2 volte le revisioni di BTC/SOL [M: 27 contro 12-13] e
  il maggior numero di stati spostati (7 contro 2 e 0); stati spostati per minuto rivisto 0,26 ETH, 0,17 SOL, 0 BTC [D]. ETH e'
  il prodotto piu' vicino al limite di R1: con un champion piu' sensibile e' il primo candidato a fallire R1 nel G-DRY-OPS.
- Giudizio: `pass` credibile come certificazione della PIPELINE delle barre con questo modello (limite gia' dichiarato in A.7);
  non credibile, e non va scritto, come prova che le revisioni siano innocue per i modelli futuri.

### Q1.5 Rischio residuo da registrare (non cambia l'esito)

- Il gate di produzione a 1.440 stati, con cluster, ha falso `fail` ~25% a p = 0,35% e falso `pass` ~7% a p = 1% [D]. Non lo
  cambio (nessuna soglia nuova), ma quando R1 fallira' con il champion tarato il CONFLITTO deve riportare il numero di cluster
  e non solo gli stati, per distinguere una revisione isolata da uno skew sistematico.

### Q3. Margine di R2 e cosa fare se P0 resta "non certificato"

**R2: nessun criterio di coda aggiuntivo; nessun margine imposto ora.**
- R2 e' gia' il criterio di coda piu' severo possibile: e' il massimo (p100). Un criterio p99 sarebbe PIU' DEBOLE (`p99 <=
  max`), non piu' protettivo; p99_de e p99_db sono gia' in diagnostica [M: ETH 20-10 p99_db 0,0024, p99_de 0].
- Margine: 0,0151 e 0,0153 sono il 76% della soglia; distanza 0,0047-0,0049, in valore atteso `0,0049 x 1.116 ~ 5 bps` [D].
  Un gate non ha "margine" per costruzione; imporre ora `max <= 0,015` sarebbe una soglia nuova scelta dopo aver visto i dati.
  R2 e' `pass`.
- Fragilita' dichiarata [D]: (a) il massimo cresce con il numero di stati osservati; il massimo di 2 giorni non stima il
  massimo di un anno, e la distribuzione della coda di `m` e' [?]; (b) al primo ordine `m ~ |dp/dx| x dx`: `dx` dipende dalle
  revisioni (dati), `|dp/dx|` dal modello. Un champion con sensibilita' 0,02 / 0,0153 = 1,3 volte quella di questo LightGBM
  porta R2 al limite. Il controllo vero e' il G-DRY-OPS del primo champion tarato, giornaliero: e' li' che R2 conta.
- Diagnostica raccomandata (senza soglia): per prodotto e giorno i 5 `m` maggiori con `ts` e cluster di origine, e il rapporto
  `max_shift / max_revision_shift`. Se R2 fallisce con un champion: nuovo CONFLITTO e rivalutazione dell'opzione A2 (b)
  (ricalcolo dei token giornalieri), che solo allora avrebbe un beneficio misurabile (A.8).

**Se P0 resta "non certificato" (cioe' se la regola Q1.3 non viene adottata):**
- NON rifare un soak con la stessa configurazione e finestra non allineata "per riprovare": con p ~ 0,24-0,35% e cluster, un
  giorno parziale fallisce per caso nel 34-45% dei casi (Q1.2); ripetere finche' passa e' esattamente il ritocco che il gate
  vieta. NON cambiare modello per passare (scelta del modello in funzione dell'esito); NON cambiare S (A.8: non agisce).
- Se si rifa': 48 ore allineate a 00:00 UTC (finestra certificata esattamente [D 00:00, D+2 00:00), certify dopo D+2 00:15),
  S = 120 s, stesso modello `baseline-lgbm@2026-10-18.2` subset final. Due giorni interi: la questione del denominatore non si
  pone sotto nessuna delle due regole. Costo: 48 ore di calendario, rischio sul capitale nullo.
- Accettare "non certificato" e andare avanti: rischio sul capitale nullo fino a R0, perche' R0 richiede il G-DRY-OPS del
  champion con R1-R4 su 7 giorni interi. Rischi per le fasi successive: (1) governance: un criterio di uscita P0 aperto, e
  ogni fase che presuppone "barre REST certificate" (label live, training che mescola barre live a 120 s e storico definitivo)
  eredita uno skew misurato piccolo (max 0,015) ma non certificato; (2) ETH e' il primo candidato a un fallimento di R1 nel
  G-DRY-OPS, con ritardo di R0 per ETH; (3) R3 resta non misurato fino al primo champion tarato in ogni caso.
- Raccomandazione: adottare Q1.3 (preferita) e rifare `certify` (~1 minuto, nessun nuovo soak). Se viene respinta: soak
  allineato di 48 ore come sopra, in parallelo allo sviluppo (un P0 non certificato blocca R0, non il lavoro sulle fasi
  successive).

Decisioni per l'architetto (Q2): dove scrivere la regola dei blocchi da 1.440 stati (`verdict` riceve oggi solo `ImpactDay`
per giorno: serve la serie degli stati spostati con `ts`, o il conteggio sul blocco ancorato), testo di A.2/A.6, diagnostica
dei punti Q1.3-7 e Q3; per l'utente: adozione della regola prima della nuova `certify`.

## Risposta architetto

Autore: architetto-opus. Data: 2026-10-23. Stato: **RISOLTO lato architettura**. K16 e la diagnostica (passo 1 della sez. 5)
si implementano subito; la regola v2 (passo 2) aspetta U1; la nuova `certify` e qualunque nota di superamento (passi 3-4)
aspettano U2 (sez. 4). Nessun codice scritto dall'architetto, nessun altro file toccato.
File letti: questo file; `.claude/CONFLITTO.md` sez. A (A.1-A.10, K1-K15); `modules/dryrun/revision_impact.py` (intero);
`modules/config.py` (`GateOpsConfig`, `PromotionConfig`); `modules/dryrun/report.py` (`evaluate_gate_ops`, `_bars`,
`_anomalies`, `_impact_key`/`_impact_summary`/`_worst_impact`/`_impact_by_version`); spec 0.5, 5.1.7 (riga "Barre REST"),
nota sullo schema in 5.2, Appendice A riga P0, changelog 2.11.
Vincoli invariati: solo spot, nessuna leva, copertura 100%. Nessuna soglia cambia (0.01 stretto / 0.005 / 0.02 / 0.001);
nessun gate economico e' toccato. Tutto cio' che segue riguarda COME si misura, non quanto si tollera.

### 1. Valutazione della regola del quant

#### 1.1 Provenienza (dichiarata)
- La regola v1 "R1 per giorno UTC, anche parziale" e' dell'architetto (`.claude/CONFLITTO.md` A.2 e A.6, 2026-10-20) ed era
  scritta PRIMA della corsa. Il suo difetto (un giorno parziale e' un test con `n` diverso da quello del gate di produzione)
  non e' stato visto prima del `fail` del 2026-10-23 06:17Z. La regola v2 sotto e' scritta DOPO il `fail`. E' un fatto e va
  nella certificazione e nella nota; non e' un'attenuante.
- Rispetto alla v1 scritta, per QUESTO soak la v2 e' di fatto un allentamento (`fail` -> `pass`). Rispetto al criterio deciso
  (budget 0,5% su 24 ore di stati, lo stesso test del gate che porta a R0) e' la misura corretta: sul giorno parziale la v1
  applicava un test piu' severo del deciso (tolleranza zero revisioni) e, in altri casi, piu' permissivo (falso `pass`
  14-18% al doppio del budget, Q1.2). Quale dei due giudizi prevalga per una certificazione gia' eseguita lo decide l'utente
  (U2), non l'architetto.

#### 1.2 Proprieta' richieste a priori (non dipendono dall'esito)
- P-a stesso `n` del gate di produzione: 1.440 stati per blocco.
- P-b nessuno stato misurato resta fuori da ogni test (niente diluizione, niente stati scartati).
- P-c in produzione un giorno UTC intero e' il proprio blocco: il gate G-DRY-OPS non cambia.
- P-d deterministica e senza numeri nuovi.

| Regola | P-a | P-b | P-c | P-d | R1 ETH-EUR sul soak (conseguenza, NON criterio di scelta) |
|---|---|---|---|---|---|
| v1: giorno UTC anche parziale (A.6) | no (n = 343) | si' | si' | si' | `fail` (2/343) |
| **v2: blocchi di 1.440, giorno parziale esteso (quant Q1.3)** | si' | si' | si' | si' | `pass` (blocco peggiore <= 7/1.440) |
| blocchi non sovrapposti, resto ignorato | si' | **no** (1.439 stati fuori da ogni test) | no | si' | `pass` |
| blocchi non sovrapposti, resto `unmeasured` | si' | si' | **no** (blocchi non allineati al giorno UTC) | si' | `unmeasured` (resto di 1.439) |
| (A) giorno < 720 sommato all'adiacente | no (n 720-2.159) | si' | si' | **no** (720 nuovo; giorno di destinazione arbitrario) | `pass` (7/1.783) |
| (B) finestra intera | no (n cresce con la durata) | si' | no | si' | `pass` (7/2.879) |
| (C) finestra mobile 24 h, massimo | si' | si' | **no** (massimo su finestre sovrapposte = test piu' severo del deciso) | si' | `pass` (7 totali <= 7) |

Solo la v2 ha tutte e quattro le proprieta'. E' quella che specifico (adozione = U1).

#### 1.3 Il blocco che sovrappone il giorno adiacente: accettabile
- Ogni blocco calcola la PROPRIA quota sui PROPRI stati; le quote non si sommano mai fra blocchi. Uno stato nella zona di
  sovrapposizione entra in due test, ma in nessuna quota conta due volte. I totali della certificazione (`states`,
  `states_shifted`) si calcolano sugli stati distinti.
- Conseguenza: piu' test sugli stessi stati -> la probabilita' di almeno un falso `fail` cresce un poco (nel soak 3 blocchi
  su 2.879 stati invece di 2). E' un irrigidimento, mai un allentamento rispetto a una partizione.
- Nel soak (2.879 = 2 x 1.440 - 1) il blocco iniziale e quello finale si sovrappongono per 1 solo stato (chiusura
  2026-10-19 05:43): quasi una partizione della finestra; il giorno intero 10-19 e' un terzo test su stati gia' coperti.
- L'alternativa "blocchi non sovrapposti" non e' equivalente: o scarta il resto (viola P-b: allenta) o lo dichiara
  `unmeasured` (viola P-c e rende non certificabile ogni finestra non multipla di 24 ore). Respinta per le proprieta', non
  per l'esito.

#### 1.4 LIMITE: l'esito per ETH-EUR dipende dalla regola
- Sugli stessi dati: `fail` con la v1; `pass` con la v2 e con ogni regola che porta il giorno parziale ad almeno ~720 stati;
  `unmeasured` con i blocchi non sovrapposti stretti. La v2 e' scelta per P-a..P-d; l'utente deve sapere che con la v1 lo
  stesso soak fallisce. Lo scrivono la certificazione (`p0_bars_criterion_rule_v1`) e la nota (sez. 3).
- La v2 non e' uniformemente piu' severa della v1: identica sui giorni interi; sui giorni parziali puo' andare in entrambe le
  direzioni (test `test_block_partial_day_can_fail_where_calendar_day_passes`).

#### 1.5 Correzione al quant: "in produzione D-1 e' sempre intero" non e' vero nel codice attuale (K16)
- `day_impact` fa `real = real[real.ts < day_end]` e `last = min(day_end - 1 min, ultima riga reale)`: la griglia di D-1
  finisce all'ultimo minuto CON scambi. Gli stati di D-1 (chiusura in D-1, cioe' `ts` da 23:59 di D-2 a 23:58 di D-1) sono
  1.440 solo se esiste una riga reale con `ts >= 23:58`. ETH/SOL hanno molti minuti senza scambi (nel soak ETH 450 su 2.690,
  `.claude/CONFLITTO.md` "Dati aggiuntivi"): in quelle notti D-1 ha meno di 1.440 stati e i minuti piatti finali, che l'engine
  ha consumato (pubblicati `flat`), non sono misurati, anche quando seguono un minuto rivisto (la famiglia notturna di K15).
- **K16 (normativa, indipendente da U1, piu' severa):** in `day_impact`, se lo store del prodotto ha almeno una riga reale con
  `ts >= day_end`, `last = day_end - 1 min` (i minuti finali senza scambi sono provati piatti dalla riga successiva: e' la
  griglia che `load_bars` darebbe su un intervallo che include D). Altrimenti (store fermo prima della mezzanotte) `last`
  resta com'e' e D-1 e' un giorno parziale, gestito dalla v2 (blocco esteso all'indietro su D-2).
- Da verificare (Sonnet, prima di implementare K16): che `fill_flat_bars(real, None, last)` con `last` oltre l'ultima riga
  reale produca i minuti piatti fino a `last` (lo suggerisce il `min(...)` gia' presente in `certify` e `day_impact`). Se non e'
  cosi': STOP, addendum in fondo a questo file, nessuna alternativa inventata.
- Con K16, in produzione v2 = v1 salvo store fermo prima della mezzanotte di D-1 (raro; dichiarato nel changelog).

### 2. Specifica d'implementazione

#### 2.1 Regola v2 (normativa)
- **Serie** (per prodotto e versione): gli `ImpactDay` con `states > 0`, in ordine di giorno; `t` = istante di chiusura dello
  stato (`ts + 1 min`), come gia' per il raggruppamento per giorno. `day_blocks` verifica la contiguita': per ogni giorno
  `states == (last_at - first_at) / 1 min + 1`; fra giorni consecutivi `first_at == last_at_precedente + 1 min`; altrimenti
  `ValueError("states are not a contiguous series")`.
- `B = block_states` (1.440), `S0` = `first_at` del primo giorno, `N` = somma degli stati.
- `N < B`: nessun blocco; R1 e R3 `unmeasured` (nota `series_shorter_than_block`).
- Per ogni giorno giudicato `d`:
  1. `d.states >= B` -> blocco = il giorno (`kind = "day"`, `states = d.states`);
  2. altrimenti, se `(d.last_at - S0) / 1 min + 1 >= B` -> `[d.last_at - (B - 1) min, d.last_at]`, `kind = "extended_back"`;
  3. altrimenti -> `[S0, S0 + (B - 1) min]`, `kind = "extended_forward"` (i primi B stati della serie, che contengono `d`).
  Con la contiguita' solo il primo e l'ultimo giorno possono essere parziali: il primo va in (3), l'ultimo in (2).
- Nel blocco: `shifted` = stati della serie con `m > shift` e `t` nel blocco (estremi inclusi); `share = shifted / states`;
  `flips` idem dai flip, `flip_share = flips / states`; `r3 = "measured"` solo se tutti i giorni toccati dal blocco sono
  `measured`, altrimenti il primo stato non misurato in ordine di giorno, e allora `flips = flip_share = None`.
- Esiti per prodotto (precedenza `fail` > `unmeasured` > `pass`, per R1 e per R3):
  - R1 `fail` se un blocco ha `share > max_share`; `unmeasured` se manca un blocco; altrimenti `pass`;
  - R3 `fail` se un blocco misurato ha `flip_share > max_flip_share`; `unmeasured` se manca un blocco o un blocco non e'
    misurato; altrimenti `pass`;
  - R2 per giorno giudicato (`ImpactDay.max_shift <= thr.max_shift`): il massimo non dipende dal raggruppamento;
  - R4 invariata (somma, zero fisso): non dipende dal raggruppamento.
- Esempio ETH-EUR (t in UTC): 10-18 [05:44, 23:59] 1.096 stati; 10-19 [00:00, 23:59] 1.440; 10-20 [00:00, 05:42] 343;
  `S0` = 10-18 05:44, `N` = 2.879. Blocchi: 10-18 `extended_forward` [10-18 05:44, 10-19 05:43]; 10-19 `day`; 10-20
  `extended_back` [10-19 05:43, 10-20 05:42].

#### 2.2 Dimensione del blocco: `ImpactThresholds.block_states`, nessuna chiave di config
- Costante `STATES_PER_DAY: Final = 1440` in `modules/dryrun/revision_impact.py`.
- `ImpactThresholds` riceve un quinto campo `block_states: int = STATES_PER_DAY` (in coda, con default: `ImpactThresholds(0.01,
  0.005, 0.02, 0.001)` resta valido); `__post_init__`: `ValueError` se `block_states < 1`. `from_config(g)` passa
  `STATES_PER_DAY`. I test sintetici possono usare blocchi piccoli costruendo `ImpactThresholds` direttamente.
- **Nessuna chiave in `GateOpsConfig`.** `block_states` non e' una soglia: e' la definizione dell'unita' del test di
  produzione (un giorno UTC di decisioni a 1 minuto). Una chiave YAML sarebbe esattamente la manopola che il gate vieta di
  girare dopo aver visto un risultato; cambiarla richiede un CONFLITTO e una riga di changelog, come ogni definizione. Con
  `extra="forbid"` una chiave `revision_block_states` scritta nel YAML da' `ConfigError` (test in 2.8).
- **Perche' non 720:** (i) non e' l'`n` del gate di produzione (P-a); (ii) con revisioni che spostano 2 stati ammette una
  sola revisione e ha falso `fail` 21-36% a p = 0,24-0,35% (Q1.2), sotto `n_min ~ 1.200`; (iii) come soglia di fusione fa
  variare `n` fra 720 e 2.159 con il calendario; (iv) e' stato proposto dopo aver visto 343, senza derivazione.

#### 2.3 `modules/dryrun/revision_impact.py`
```python
STATES_PER_DAY: Final = 1440
CERT_SCHEMA: Final = "chronos.p0_bars_certification.v2"
BLOCK_RULE: Final[Mapping[str, Any]] = {
    "version": "v2", "block_states": STATES_PER_DAY,
    "rule": "R1 e R3 su blocchi di 1440 stati consecutivi: giorno UTC intero = blocco; giorno con meno stati = i 1440 "
            "stati della serie che terminano con il suo ultimo stato, o i primi 1440 della serie se prima non ce ne "
            "sono abbastanza; serie con meno di 1440 stati = unmeasured. R2 e R4 per giorno UTC.",
    "supersedes": "v1: R1 e R3 per giorno UTC, anche parziale (A.2/A.6, 2026-10-20)",
    "decided_on": "<AAAA-MM-GG della decisione U1>",
    "fixed_after_first_run": True,
    "first_run": "2026-10-23T06:17Z, p0_bars_criterion fail: R1 ETH-EUR 2026-10-20, 2/343 stati",
    "reason": "un giorno parziale di n stati e' un test diverso dal gate di produzione (1440 stati per D-1): con n = 343 "
              "e revisioni che spostano 2 stati la quota 0.005 ammette zero revisioni",
    "decision": "docs/decisions/2026-10-20-revisioni-rest-e-promozione.md (sez. P1e-bis)",
}

@dataclass(frozen=True)
class ShiftedState:
    t: datetime            # istante di chiusura dello stato (ts_bar_close), UTC
    m: float               # spostamento (inf se NaN in un solo mondo)
    origin: str | None     # "<prodotto> <ts del minuto rivisto %Y-%m-%dT%H:%MZ>", None = sconosciuta

@dataclass(frozen=True)
class ImpactDay:           # campi esistenti invariati e nello stesso ordine, poi (tutti con default):
    first_at: datetime | None = None       # t del primo e dell'ultimo stato del giorno (None se states == 0)
    last_at: datetime | None = None
    shifted_states: tuple[ShiftedState, ...] = ()   # gli stati con m > shift, in ordine di t; len == shifted
    flipped_at: tuple[datetime, ...] = ()            # t dei flip (vuota se R3 non misurata)
    top_shifts: tuple[ShiftedState, ...] = ()        # i 5 m maggiori del giorno (m decrescente, a pari m t crescente)

@dataclass(frozen=True)
class ImpactBlock:
    product: str; version: str; day: date            # il giorno UTC che il blocco giudica
    start: datetime; end: datetime                   # t del primo e dell'ultimo stato, inclusi
    states: int
    kind: Literal["day", "extended_back", "extended_forward"]
    shifted: int; share: float
    flips: int | None; flip_share: float | None
    r3: R3Status
    revisions: int                                   # cluster indipendenti fra gli stati spostati del blocco
    max_cluster: int                                 # stati spostati del cluster piu' grande (0 se nessuno)
    def to_dict(self) -> dict[str, Any]              # date e datetime in ISO Z

def day_blocks(days: Sequence[ImpactDay], block_states: int, *, judged: Collection[date] | None = None
               ) -> list[ImpactBlock] | None
    # days: UN prodotto e UNA versione. Regola 2.1. None se N < block_states. ValueError se non contigua o se un
    # giorno con states > 0 non ha first_at/last_at. judged None = tutti i giorni.
def block_status(block: ImpactBlock, thr: ImpactThresholds) -> dict[str, Verdict]     # {"R1": ..., "R3": ...}
def rolling_max(days: Sequence[tuple[datetime, datetime, Sequence[datetime]]], block_states: int
                ) -> dict[str, Any] | None
    # (first_at, last_at, t degli stati spostati) per giorno, serie contigua (ValueError altrimenti); None se N < B;
    # altrimenti {"shifted", "share", "start", "end"} della PRIMA finestra di B stati consecutivi, interamente dentro la
    # serie, con il massimo di stati spostati. Solo libreria standard (la usa anche il report).
def verdict(days: Sequence[ImpactDay], thr: ImpactThresholds, *, judged: Collection[date] | None = None
            ) -> dict[str, Verdict]
    # UN prodotto, una o piu' versioni: day_blocks per versione; esiti come in 2.1. Invariato: lista vuota o un giorno
    # "decider_without_batch" -> tutto unmeasured.
```
- `impact_day(..., t_close: np.ndarray | None = None)`: con `t_close` (array degli istanti di chiusura, lunghezza `n`) riempie
  `first_at`/`last_at` in OGNI ramo con `n > 0` (anche `decider_without_batch`), `shifted_states` (origine `None`),
  `flipped_at`, `top_shifts`. Senza `t_close` i campi restano ai default (solo test vecchi; `day_blocks` li rifiuta).
- `revision_impact(..., revised_minutes: Mapping[str, pd.DatetimeIndex] | None = None)`: passa `t_close=t_rows` a
  `impact_day` e, se `revised_minutes` e' dato, riempie `origin` di `shifted_states` e `top_shifts` (`dataclasses.replace`).
  Regola dell'origine: fra i minuti rivisti di valore applicati a `F` del prodotto e, per i prodotti non BTC, di BTC (il
  contesto dello stesso mondo), il piu' recente con `ts <= t - 1 min`; a parita' di `ts` vince il prodotto stesso; nessun
  candidato -> `None`. Attribuzione approssimata (le feature a 1 giorno risentono anche di revisioni precedenti): solo
  diagnostica, dichiarata come tale nel docstring e nelle note della certificazione.
- Cluster: stati spostati con la stessa origine; ogni stato con origine `None` e' un cluster a se'. `revisions` = numero di
  cluster nel blocco; `max_cluster` = dimensione del piu' grande.
- `ImpactDay.to_dict`: `first_at`/`last_at`/`flipped_at` in ISO Z; `ShiftedState` come `{"t", "m", "origin"}` con `m` infinito
  scritto `INF_AS_JSON`.
- `note_text(doc, created)`: produce ESATTAMENTE il testo della sez. 3.3 dai campi del documento; `ValueError` se
  `p0_bars_criterion != "pass"` o se `reread_vs_previous.same` e' falso per un prodotto.

#### 2.4 L0 (`day_impact`)
- K16 (sez. 1.5).
- Per ogni (book, versione): `revision_impact(..., [audited], ..., revised_minutes={p: ts dei rivisti applicati a p})`. Se
  l'`ImpactDay` di `audited` ha `states < thr.block_states`: si misura anche `audited - 1 giorno` con la STESSA versione, gli
  stessi mondi e indicatori (`inds`), le posizioni del ledger su `[day_start - 1 giorno, day_end)`; quell'`ImpactDay` e' un
  giorno ausiliario, mai giudicato.
- `verdict(entrate + ausiliari, thr, judged={audited})`; `day_blocks(..., judged={audited})` per versione.
- Documento per prodotto: `{"books": {...invariato...}, "blocks": {book: [ImpactBlock.to_dict()]}, "helper_days": {book:
  [ImpactDay.to_dict()]}, "verdict": {...}}`; `thresholds` (gia' `asdict(thr)`) porta `block_states`. Un `ValueError` di
  contiguita' va in `errors` con chiave `"<book>:<version>:blocks"` e R1/R3 di quel book `unmeasured`.

#### 2.5 CLI `certify` (schema v2)
- Argomenti nuovi: `--previous PATH` (facoltativo, sola lettura): deve avere lo stesso `soak_report.sha256`, lo stesso
  `model.version` e lo stesso `subset`, altrimenti `ConfigurationError` (exit 2).
- `--out` esistente -> `ConfigurationError` ("a certification is never rewritten: choose a new --out"). Un file di
  certificazione e' un artefatto misurato e non si riscrive (stesso principio di `p0_soak.json`).
- Le candele rilette di ogni prodotto (output di `_reread`) si salvano in `<out.parent>/<out.stem>.reread/<prodotto>.parquet`
  (scrittura atomica; vietato sotto `bars_root`, come `--out`), con sha256 nel documento: una futura domanda sulla regola si
  potra' rivalutare sugli stessi dati. (Oggi non e' possibile: la prima corsa non le ha salvate, quindi la seconda rilegge.)
- `p0_bars_criterion` in `{"pass", "fail", "unmeasured"}` = peggiore fra prodotti con precedenza `fail` > `unmeasured` > `pass`
  su R1, R2, R4. Exit 0 solo con `pass`; 1 con `fail` o `unmeasured`; 2 configurazione. Stampa anche `"rule": "v2"`.
- Documento (aggiunte allo schema v1; il resto invariato):
  - `"thresholds"`: `+ "block_states": 1440`; `"criterion"`: `+ "r1_r3_unit": BLOCK_RULE`;
  - `"previous_run"`: `{"path", "sha256", "created_at", "p0_bars_criterion", "rule": "v1"}` oppure `null`;
  - `"p0_bars_criterion_rule_v1"`: esito con la regola v1 (R1 e R3 per giorno, anche parziale) sugli STESSI dati, diagnostica;
  - `products.<p>.blocks`: `[ImpactBlock.to_dict()]`;
  - `products.<p>.days[i]`: tolto `**verdict([d], thr)`; `"R1"`, `"R3"` = `block_status` del blocco del giorno; `"R2"` del
    giorno; `+ "block"`, `"shifted_states"`, `"top_shifts"`;
  - `products.<p>.diagnostic` (senza soglia): `"r1_window"` `{"states", "shifted", "share"}` (alternativa B);
    `"r1_rolling_max"` (alternativa C, `rolling_max`); `"r1_calendar_days_v1"` `[{"day", "states", "shifted", "share",
    "status"}]`; `"revisions_independent"` e `"max_cluster"` sull'intera finestra; `"top_shifts"` (5 sull'intera finestra,
    con origine); `"r2_utilization"` = `max_shift / max_revision_shift` (soglia di R2); `"reread"` `{"path", "sha256"}`;
    `"reread_vs_previous"` `{"same": bool, "previous": {...}, "now": {...}}` sui campi `minutes`, `revised`,
    `value_revised`, `max_close_diff_bps` e sugli `shifted` per giorno (`null` senza `--previous`).
  - `notes`: aggiungere "origine di uno stato spostato = revisione piu' recente del prodotto o di BTC non successiva alla
    barra dello stato: attribuzione approssimata, solo diagnostica" e "blocchi R1/R3 secondo `criterion.r1_r3_unit`".

#### 2.6 Report (`modules/dryrun/report.py`)
- `_bars`: per ogni entrata `ImpactDay` di un book si cerca in `impact[p]["blocks"][book]` il blocco con la stessa
  `version` e lo stesso `day`; il riassunto (`_impact_summary`) diventa: `version`, `states` (del giorno), `share` e
  `flip_share` DEL BLOCCO, `max_shift`, `r3` del blocco, e in piu' `day_share`, `block_states`, `block_kind`, `revisions`,
  `max_cluster`, `r2_utilization` (= `max_shift / cfg.gate_ops.max_revision_shift`), `top_shifts`, `first_at`, `last_at`,
  `shifted_at` (lista ISO dei `t` spostati).
- Retrocompatibilita' (documento L0 senza `blocks`, scritto prima della modifica): se `states >= STATES_PER_DAY` il blocco e'
  il giorno per definizione (`share`, `flip_share` del giorno, `block_kind = "day"`); altrimenti `share = flip_share = None`.
  `STATES_PER_DAY` si importa da `revision_impact` dentro la funzione (import pigro, come gia' `pandas` in `_labels_block`).
- `_impact_key`: `share` `None` vale `+inf` (un R1 non misurato emerge come peggiore).
- `evaluate_gate_ops`: R1 `unmeasured` se uno `share` e' `None` (flag separato da quello di R2, che resta misurato); R3 con
  `r3 == "measured"` e `flip_share` `None` -> nota `block_unavailable`. Nota di R1: "R1: share of shifted states (> thr) on
  blocks of 1440 states (a full UTC day is its own block), worst product and day". Nessuna soglia nuova.
- `diagnostics` (oltre a `rest_revision_ratio`), formato `{"name", "value", "detail", "note": "diagnostica senza soglia, non
  entra nello stato del gate"}`:
  - `revision_shift_rolling_max`: `rolling_max` sulle sequenze massime di giorni contigui della finestra (per prodotto, dai
    `first_at`/`last_at`/`shifted_at` del book valutato); `value` = quota massima; `detail` per prodotto; se il book cambia
    versione nella finestra la sequenza mescola le versioni: scritto nel `detail`;
  - `revision_independent_revisions`: `value` = massimo di `revisions` per blocco; `detail` per prodotto e giorno;
  - `revision_r2_utilization`: `value` = massimo di `r2_utilization`.
- `_anomalies`: uno `share` `None` non e' un'anomalia (regola gia' scritta: una misura mancante non e' un'anomalia).
- Markdown: la colonna R1 della tabella Bars mostra la quota del blocco con "(blocco esteso)" quando `block_kind != "day"`.

#### 2.7 Dove sta la diagnostica senza soglia (riepilogo)

| Diagnostica | Certificazione | L0 / report giornaliero | G-DRY-OPS |
|---|---|---|---|
| finestra mobile 24 h | `diagnostic.r1_rolling_max` | `first_at`/`last_at`/`shifted_at` | `revision_shift_rolling_max` |
| revisioni indipendenti (cluster) | per blocco e sull'intera finestra | `revisions`, `max_cluster` per blocco | `revision_independent_revisions` |
| 5 `m` maggiori con origine | per giorno e sull'intera finestra | `top_shifts` per giorno | - |
| `max_shift / max_revision_shift` | `diagnostic.r2_utilization` | `r2_utilization` | `revision_r2_utilization` |
| regola v1 e finestra intera | `r1_calendar_days_v1`, `r1_window`, `p0_bars_criterion_rule_v1` | `day_share` | - |

#### 2.8 Test richiesti
`tests/test_revision_impact.py`:
- `test_block_full_day_is_the_day`
- `test_block_partial_final_day_uses_last_block_states`
- `test_block_partial_initial_day_uses_first_block_states`
- `test_block_partial_day_with_few_states` (giorno finale di 3 stati con 1 spostato: v1 1/3 `fail`; v2 1/1.440 `pass`)
- `test_block_partial_day_can_fail_where_calendar_day_passes` (simmetria: giorno intero con 7 stati spostati tutti nelle
  ultime ore, `pass` 7/1.440; giorno finale parziale di 300 stati con 1 spostato, `pass` con la v1 perche' `floor(1,5) = 1`;
  il suo blocco `extended_back` contiene 8 stati spostati -> `fail` con la v2)
- `test_overlapping_blocks_counted_per_block_not_summed` (uno stato spostato nella sovrapposizione conta in due blocchi e una
  volta in `states_shifted`)
- `test_series_shorter_than_block_is_unmeasured` (1.439 stati: R1 e R3 `unmeasured`, R2 giudicata)
- `test_series_of_exactly_block_states_two_partial_days`
- `test_noncontiguous_series_raises`
- `test_verdict_fail_precedes_unmeasured`
- `test_r2_and_r4_do_not_depend_on_grouping`
- `test_r3_flips_counted_on_blocks`
- `test_full_days_same_verdict_as_calendar_rule` (`hypothesis`: serie di soli giorni interi -> stessi esiti R1/R3 e stessa
  `share` della regola v1; e' la prova che in produzione, con D-1 intero, lo stesso input da' lo stesso verdetto)
- `test_eth_soak_shape_synthetic` (1.096 + 1.440 + 343 stati con i `t` reali della finestra; 2 spostati il 20-10 alle
  02:33-02:34; i 5 del 19-10 parametrizzati: tutti prima di 05:43, tutti dopo, misti; v1 calcolata nel test da
  `ImpactDay.share` -> `fail` il 20-10; v2 -> `pass` in ogni disposizione; un ottavo stato spostato nel blocco finale -> `fail`)
- `test_impact_day_records_timestamps_and_shifted_states` (`len(shifted_states) == shifted`, `first_at`/`last_at`,
  `top_shifts` ordinati, `inf` conservato)
- `test_origin_is_latest_revision_at_or_before_state`
- `test_origin_uses_btc_revisions_for_eth`
- `test_clusters_and_max_cluster_in_block`
- `test_rolling_max_over_contiguous_series`
- `test_thresholds_boundaries` (AGGIORNATO: blocco sintetico di B = 1.000 stati: 5 spostati passa, 6 fallisce; m = 0.01
  non conta; max 0.02 passa; 0.0200001 fallisce)
- `test_certify_writes_schema_and_never_writes_soak_report` (AGGIORNATO a v2: `blocks`, `r1_r3_unit`,
  `p0_bars_criterion_rule_v1`, candele rilette salvate con sha256, sha256 di `p0_soak.json` invariato)
- `test_certify_refuses_to_overwrite_existing_out`
- `test_certify_previous_run_must_match_soak_and_model`
- `test_certify_reread_vs_previous_flags_difference`
- `test_certify_exit_codes` (AGGIORNATO: `unmeasured` -> 1)
- `test_note_text_only_for_pass_with_same_reread`

`tests/test_learner.py`:
- `test_l0_full_day_measures_one_day_and_no_helper`
- `test_l0_partial_day_extends_into_previous_day_same_version`
- `test_l0_trailing_flat_minutes_are_states` (K16: ultimo scambio di D-1 alle 23:40, una riga reale dopo la mezzanotte ->
  1.440 stati; con il minuto 23:40 rivisto, in `F` i minuti piatti 23:41-23:58 portano il close riletto e sono misurati)
- `test_l0_store_stopped_before_midnight_keeps_partial_day` (K16, caso negativo -> blocco `extended_back`)
- `test_l0_blocks_in_document`

`tests/test_report.py`:
- `test_gate_ops_r1_reads_block_share`
- `test_gate_ops_old_learner_doc_full_day_same_share`
- `test_gate_ops_old_learner_doc_partial_day_r1_unmeasured`
- `test_gate_ops_new_diagnostics_not_criteria`
- `test_anomalies_ignore_unmeasured_share`

`tests/test_config.py`: `test_gate_ops_has_no_block_states_key` (YAML con `revision_block_states` -> `ConfigError`).

I test esistenti che costruiscono `ImpactDay` a mano e chiamano `verdict` si aggiornano nello stesso commit (servono
`first_at`/`last_at`/`shifted_states`), con il motivo nel messaggio; nessuno si cancella senza sostituto.

#### 2.9 Retrocompatibilita'
- YAML: nessuna chiave nuova; ogni config esistente resta valida.
- `ImpactThresholds` a 4 argomenti posizionali e `ImpactDay` posizionale: validi (campi nuovi in coda con default).
- `verdict(days, thr)`: firma compatibile (`judged` facoltativo); semantica cambiata (v2), dichiarata nel changelog.
- Documenti L0 gia' scritti: letti dal report con la regola di 2.6 (giorno intero = blocco; giorno parziale = R1
  `unmeasured`, mai giudicato con la v1).
- `data/reports/p0_soak_certification.json` (v1, `fail`): non si modifica, non si sposta; la v2 e' un file nuovo.

### 3. Certificazione dopo il nuovo calcolo (solo se U1 = si' e U2 = (a))

#### 3.1 Procedura
1. Passi 1-2 della sez. 5 implementati e verdi (`pytest`, `mypy --strict`, `ruff`).
2. Comando esatto:
   `python -m modules.dryrun.revision_impact certify --soak-report data/reports/p0_soak.json --model-dir
   models/baseline/baseline-lgbm@2026-10-18.2 --subset final --previous data/reports/p0_soak_certification.json --out
   data/reports/p0_soak_certification.v2.json`
3. La rilettura avviene giorni dopo la prima: REST puo' aver rivisto altre candele. Se `reread_vs_previous.same` e' falso per
   un prodotto, i limiti numerici della nota (7 stati, 1,13%, 0,54-0,64%) non valgono piu': NESSUNA nota; addendum in fondo
   a questo file con il confronto, e il `trader-quant` ricalcola i limiti. Il documento v2 resta comunque (non si riscrive).
4. La nota della sez. 3.3 si scrive solo se: `p0_bars_criterion == "pass"` E `reread_vs_previous.same` vero per tutti i
   prodotti. Si genera con `note_text` (nessun numero copiato a mano). Se `fail` o `unmeasured`: nessuna nota, nuovo
   CONFLITTO con il file v2 (protocollo CLAUDE.md).

#### 3.2 Campi variabili della nota
`{created}` = `created_at` della v2; `{decided_on}` = `BLOCK_RULE["decided_on"]`; `{eth_block}` = `shifted` massimo fra i
blocchi di ETH-EUR. Tutto il resto e' letterale (vale solo con rilettura identica, punto 3.1.4).

#### 3.3 Testo ESATTO della nota (`tasks/todo.md`)
```
- [x] P0, criterio barre REST: certificato il {created} in `data/reports/p0_soak_certification.v2.json`
  (`chronos.p0_bars_certification.v2`) con la regola R1/R3 v2 a blocchi di 1.440 stati consecutivi (giorno UTC intero =
  blocco). La regola v2 e' stata fissata il {decided_on}, DOPO il primo calcolo (`data/reports/p0_soak_certification.json`,
  2026-10-23 06:17Z, `fail` con la regola v1 "R1 per giorno UTC, anche parziale": ETH-EUR 2026-10-20, 2 stati spostati su
  343 = 0,583%), perche' un giorno parziale e' un test con meno stati di quello del gate di produzione (1.440 per giorno):
  con 343 stati e revisioni che spostano 2 stati ciascuna, la quota 0,005 non ammette nessuna revisione. Sugli stessi dati
  l'esito e' `fail` con la v1 e `pass` con la v2. Esito v2: R1, R2 e R4 superati per BTC-EUR, ETH-EUR e SOL-EUR; R3 non
  misurata (policy non tarabile), rinviata al G-DRY-OPS del primo champion tarato. Limiti: ETH-EUR ha 7 stati spostati in
  tutta la finestra, quanti ne ammette un solo blocco di 1.440 stati: margine zero stati (blocco peggiore {eth_block}/1.440);
  il limite superiore al 95% della quota vera di ETH-EUR e' 0,54-0,64% con revisioni che spostano 2 stati (0,41% se
  indipendenti), sopra il budget di 0,5%: il soak non mostra una violazione ma non la esclude; `rest_revision_ratio` grezzo
  di ETH-EUR 1,13% (allarme, diagnostica). Il verdetto vale per questa pipeline delle barre e per questo modello
  (`baseline-lgbm@2026-10-18.2`, subset final), non per modelli futuri, che R1-R4 rimisurano in G-DRY-OPS.
  `data/reports/p0_soak.json` e `data/reports/p0_soak_certification.json` restano invariati. Decisione:
  `docs/decisions/2026-10-20-revisioni-rest-e-promozione.md` (sez. P1e-bis).
```

#### 3.4 Spec da cambiare (`docs/specs/chronos-system1-spec.md`)
- Intestazione: `Versione: 2.11-draft | Data: {decided_on}` (dopo U1).
- **0.5**, criterio di uscita P0 (dopo U1): "con R1 e R2 della sez. 5.1.7 (riga "Barre REST") per prodotto e per giorno UTC
  con il modello in uso" diventa "con R1 e R2 della sez. 5.1.7 (riga "Barre REST") per prodotto con il modello in uso (R1 su
  blocchi di 1.440 stati consecutivi, R2 per giorno UTC)". La frase "Stato al 2026-10-23: ... (`.claude/CONFLITTO_certificazione_P0.md`)."
  si sostituisce, secondo l'esito, con UNA di queste:
  - U2 = (a) e v2 `pass` con rilettura identica: "Prima certificazione (2026-10-23 06:17Z, regola v1 "R1 per giorno UTC,
    anche parziale"): `fail` su R1 per ETH-EUR nel giorno parziale 2026-10-20 (2 stati su 343), file
    `data/reports/p0_soak_certification.json`, invariato. La regola R1/R3 a blocchi di 1.440 stati (v2) e' stata fissata il
    {decided_on}, dopo quel risultato (conflitto P1e-bis, decisione U1 dell'utente); sugli stessi dati la certificazione v2
    (`data/reports/p0_soak_certification.v2.json`, {created}) e' `pass` per R1, R2 e R4, con R3 non misurata: il criterio
    "barre REST" di P0 e' superato per questa pipeline delle barre e per il modello `baseline-lgbm@2026-10-18.2`, con i limiti
    scritti in `tasks/todo.md` (margine zero stati per ETH-EUR; limite superiore al 95% della quota 0,54-0,64%). I soak futuri
    si pianificano allineati a 00:00 UTC e di durata multipla di 24 ore, con `certify` dopo 15 minuti di eta'."
  - U2 = (b): "Prima certificazione (2026-10-23 06:17Z, regola v1): `fail` su R1 per ETH-EUR nel giorno parziale 2026-10-20
    (2 stati su 343), `data/reports/p0_soak_certification.json`, invariato. Il criterio "barre REST" di P0 non e' certificato:
    si certifica con un soak di 48 h allineato a 00:00 UTC (decisione U2 del <data>), su cui la regola v1 e la v2 coincidono."
  - v2 `fail`/`unmeasured`, oppure rilettura diversa: "... la certificazione v2 (`data/reports/p0_soak_certification.v2.json`)
    e' `<esito>`: il criterio "barre REST" di P0 non e' certificato (`.claude/CONFLITTO.md`)."
- **5.1.7**, riga "Barre REST": "per prodotto, per giorno UTC e per versione di modello" diventa "per prodotto, per giorno UTC
  giudicato e per versione di modello"; dopo "zero barre fuori ordine o duplicate." si inserisce: "Unita' di misura (v2,
  conflitto P1e-bis, fissata dopo il primo `fail` della certificazione P0): R1 e R3 si valutano su blocchi di 1.440 stati
  consecutivi; un giorno UTC con 1.440 stati e' il proprio blocco; un giorno con meno stati usa i 1.440 stati consecutivi
  della serie misurata che terminano con il suo ultimo stato oppure, se prima non ce ne sono abbastanza, i primi 1.440 della
  serie (il blocco puo' sovrapporsi al giorno adiacente, che si valuta comunque per conto suo; le quote non si sommano fra
  blocchi); una serie con meno di 1.440 stati da' R1 e R3 `unmeasured`. R2 e R4 si valutano per giorno UTC (non dipendono dal
  raggruppamento). In L0 i minuti finali senza scambi di D-1 sono stati del giorno quando lo store prosegue oltre la
  mezzanotte. Diagnostica senza soglia: finestra mobile di 24 h, revisioni indipendenti, 5 spostamenti maggiori con origine,
  `max_shift / max_revision_shift`."
- **5.2**, nota sullo schema del report: "(`data/reports/p0_soak_certification.json`, `chronos.p0_bars_certification.v1`)"
  diventa "(`chronos.p0_bars_certification.v2`; un file di certificazione non si riscrive: ogni corsa ne scrive uno nuovo con
  `previous_run`)"; aggiungere: "`bars.<prodotto>.revision_impact.<book>` ha `share` e `flip_share` del BLOCCO e in piu'
  `day_share`, `block_states`, `block_kind`, `revisions`, `max_cluster`, `r2_utilization`, `top_shifts`, `first_at`,
  `last_at`, `shifted_at`; il documento L0 ha `blocks` e `helper_days`; `gate_ops.diagnostics` ha anche
  `revision_shift_rolling_max`, `revision_independent_revisions`, `revision_r2_utilization`."
- **Appendice A**, riga P0, criterio di uscita: "Criterio di uscita della sez. 0.5 (barre REST: R1 su blocchi di 1.440 stati,
  R2 e R4 della sez. 5.1.7, non il rapporto di minuti rivisti)."
- **Changelog 2.11**, nuova voce in fondo alla parte A: "- P1e-bis ({decided_on}, dopo il primo `fail` della certificazione del
  soak P0, e dichiarato come tale): R1 e R3 su blocchi di 1.440 stati consecutivi (giorno UTC intero = blocco; giorno con meno
  stati esteso a 1.440 stati verso l'interno della serie; serie piu' corta `unmeasured`), R2 e R4 per giorno UTC; soglie
  invariate; nessuna chiave di config (la dimensione del blocco e' la definizione del test di produzione); in L0 i minuti
  finali senza scambi di D-1 sono stati del giorno se lo store prosegue oltre la mezzanotte (K16); certificazione
  `chronos.p0_bars_certification.v2` in un file nuovo (il primo resta invariato) con `previous_run`, candele rilette salvate
  con sha256 ed esito anche con la regola v1 come diagnostica; diagnostica senza soglia (finestra mobile di 24 h, revisioni
  indipendenti, 5 spostamenti maggiori con origine, uso di R2) (sez. 0.5, 5.1.7, 5.2)."
- Se U1 = no: solo K16 e la diagnostica entrano nel changelog (voce corrispondente senza la parte sui blocchi); 0.5, 5.1.7 e
  Appendice A restano come sono, salvo la frase di stato di 0.5 (variante U2 = (b)).

#### 3.5 Documento di decisione
In fondo a `docs/decisions/2026-10-20-revisioni-rest-e-promozione.md`: sezione "P1e-bis" con questa risoluzione (o un rimando
testuale completo), le decisioni U1-U3 con data, e la tabella di 1.2. Poi questo file si cancella (CLAUDE.md, punto 6).
`.claude/CONFLITTO.md` non si tocca (lo dice la premessa di Sonnet).

### 4. Decisioni dell'utente (l'architetto non le prende)
- **U1. Adottare la regola v2 come definizione di R1/R3** (in L0/G-DRY-OPS e in `certify`)? Fatti: soddisfa P-a..P-d; e'
  scritta dopo un `fail`; in produzione, con K16, cambia solo se lo store si ferma prima della mezzanotte. Se no: resta la v1
  (giorno parziale cosi' com'e', tolleranza zero revisioni sui giorni corti), si implementano solo K16 e la diagnostica.
- **U2. (solo se U1 = si') Applicare la v2 al soak gia' eseguito?** Il punto vero: accettare una certificazione basata su una
  regola scritta dopo averne visto il `fail`.
  - (a) Si': `certify` v2 (~1 minuto, nessun nuovo soak). Pro: e' il test del gate di produzione; regola derivata da un
    principio; provenienza e dipendenza dall'esito scritte nel file e nella nota. Contro: regola post hoc; l'esito dipende
    dalla regola (`fail` v1, `pass` v2); margine zero stati per ETH-EUR; limite superiore al 95% sopra il budget.
  - (b) No: la v2 vale da ora in poi, ma P0 si certifica con un soak di 48 h allineato a 00:00 UTC, su cui v1 e v2 coincidono
    (esito indipendente dalla regola, quindi senza il problema post hoc). Costo: 48 h di calendario e ~1 minuto di `certify`;
    rischio sul capitale nullo; si lavora in parallelo (un P0 non certificato blocca R0, non lo sviluppo).
  - In entrambi i casi il capitale e' protetto fino a R0 dal G-DRY-OPS del champion su 7 giorni interi.
- **U3. (solo se U2 = (b)) Parametri del soak:** 48 h allineate a 00:00 UTC, finestra certificata esattamente
  [D 00:00, D+2 00:00), `certify` dopo D+2 00:15, S = 120 s, stesso modello `baseline-lgbm@2026-10-18.2` subset final (scelto
  prima, non in funzione dell'esito). Se fallisce R1 su un giorno intero: P0 non certificato e nuovo CONFLITTO; nessun
  tentativo successivo senza una nuova decisione dell'utente (ripetere finche' passa e' il ritocco che il gate vieta).

### 5. Ordine di lavoro per Sonnet
1. Subito (indipendente da U1): K16 con la verifica di 1.5; `ShiftedState`, campi nuovi di `ImpactDay`, `t_close`,
   `revised_minutes` e origine, `top_shifts`, `rolling_max`; i test relativi.
2. Dopo U1 = si': `STATES_PER_DAY`, `ImpactThresholds.block_states`, `ImpactBlock`, `day_blocks`, `block_status`, `verdict` v2,
   L0 (2.4), report (2.6), `certify` v2 (2.5), `BLOCK_RULE` con la data di U1, `note_text`; tutti i test di 2.8; spec 5.1.7,
   5.2, Appendice A, changelog, intestazione, e la prima frase di 0.5.
3. Dopo U2: (a) comando di 3.1 e, solo alle condizioni di 3.1.4, nota di 3.3 e frase di stato di 0.5; (b) frase di stato di
   0.5 variante (b) e soak allineato secondo U3.
4. Documento di decisione (3.5), poi cancellazione di questo file.
