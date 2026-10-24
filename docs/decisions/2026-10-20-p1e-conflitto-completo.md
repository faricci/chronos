# DECISIONE P1e (archiviata, risolta e implementata il 2026-10-23) - criterio sulle revisioni delle candele REST e regola di promozione del challenger (L5)

Autore: Sviluppatore (Sonnet). Data: 2026-10-20. Stato: DA RISOLVERE. Prima risponde `trader-quant` (sezione "Risposta trader-quant"), poi `architetto-opus` (sezione "Risposta architetto"). Nessun codice finche' non c'e' la risoluzione dell'architetto. Vincoli: solo spot, long-only, nessuna leva, copertura 100% (CLAUDE.md); i gate non si allentano per far passare un risultato: se un criterio cambia, deve cambiare perche' misura meglio la stessa cosa e il motivo deve stare per iscritto.

## Parte A - `rest_revision_ratio` (gate G-DRY-OPS, spec 0.5 / 5.1.7: <= 0,005)

### Fatti misurati
Soak P0 di 48 ore (18-ott 07:44 -> 20-ott 05:44 UTC, S = 120 s): le barre 1m si leggono dalle candele REST a t + 120 s e non si sostituiscono mai (invariante I2). A fine corsa il soak rilegge da REST i minuti salvati con eta' >= 15 minuti e conta quelli cambiati: BTC 0,496% (13/2.622), SOL 0,481% (12/2.493), ETH 1,090% (26/2.386). Il criterio (<= 0,5%) fallisce solo per ETH.

Classificazione (rilettura REST dell'intera finestra; `data/reports/soak_revisions_classified.json`, `script/soak_revision_classify.py`): 12-26 minuti per prodotto, TUTTE di valore (close e volume insieme; nessun minuto comparso o scomparso), concentrate nelle ore notturne quiete (01-06 UTC), rilette ORE dopo la chiusura (quindi salire S da 120 a 180 o 300 secondi NON le risolve: il rimedio previsto dalla tabella di `docs/decisions/2026-10-18-barre-rest.md` non vale per questa famiglia), close fino a 12,8 bps (BTC), 16,3 bps (ETH), 25,8 bps (SOL) in un singolo minuto; tipicamente un trade in ritardo aggiunto a un minuto quasi vuoto (esempio ETH 10-19 01:36: volume 0,026 -> 2,205). Nota: il 18-ott una misura precedente su ~36.000 minuti con eta' >= 1 ora aveva dato 0 revisioni; la differenza e' la finestra (qui tutte le ore notturne) e la rilettura fatta due giorni dopo.

### Impatto misurato su cio' che vede il modello (`data/reports/soak_revision_impact.json`, script `script/soak_revision_impact.py`)
Per ogni prodotto: barre SALVATE (quelle che il sistema ha usato) contro barre FRESCHE (le stesse con i minuti rivisti da REST), stessi indicatori (`compute_indicators`), stessi token di stato (`flat_matrix`, arrotondati come in produzione), 2.690 stati per prodotto nella finestra, e le risposte del modello reale (`baseline-lgbm@2026-10-18.2`, subset final):

| Prodotto | Stati con almeno un token diverso | Colonne coinvolte (stati) | Differenza massima di un token | max Δp(BUY) mascherata | p99 Δp(BUY) | max Δp_edge | stati con Δp(BUY) > 0,01 |
|---|---:|---|---|---:|---:|---:|---:|
| BTC-EUR | 124 (4,6%) | rendimenti 13-26, dist_vwap_1d 84, bb_pos 8, hh_ll 6 | 13 bps | 0,0012 | 0 | 0,0067 | 0 |
| ETH-EUR | 1.395 (51,9%) | dist_vwap_1d 1.197, vol_z_1d 341, rendimenti 22-43, btc_ret_1d 23 | 19 bps (rendimenti), 0,02 (vol_z) | 0,0029 | 0,0004 | 0,0151 | 0 |
| SOL-EUR | 1.479 (55,0%) | vol_z_1d 1.440, dist_vwap_1d 57, rendimenti 10-20 | 26 bps | 0,0023 | 0 | 0 | 0 |

Lettura: i token cambiano spesso (soprattutto `vol_z_1d` e `dist_vwap_1d`, che sommano il volume di un giorno intero e sono arrotondati a 2 decimali), ma le risposte del modello si spostano al massimo di 0,3 punti percentuali sulla probabilita' di BUY mascherata e di 1,5 punti su p_edge; nessuno stato cambia di oltre 1 punto percentuale. Una decisione cambierebbe solo se la probabilita' cadesse entro ~0,3 punti dalla soglia. NB: questa misura e' fatta con il modello attuale, che non entra mai (policy non tarabile); vale come ordine di grandezza della sensibilita' delle feature, non come prova per un modello futuro piu' sensibile.

### Domande A
A1. Il criterio `rest_revision_ratio <= 0,005` misura la stabilita' delle barre grezze, ma cio' che conta e' la stabilita' di cio' che il sistema DECIDE e di cio' su cui il modello e' stato addestrato (le barre storiche del backfill sono rilette dopo molte ore, quindi sono 'definitive'; quelle live sono lette a 120 s: skew train/serve per costruzione). Si puo' sostituire/affiancare con un criterio di IMPATTO senza allentare il rigore? Proponi quale (per esempio: quota di stati con token diversi e quota di stati con |Δp(BUY)| > 1 punto, e |Δp_edge| massimo, misurati su una finestra rigiocata come sopra con il modello in uso; soglie numeriche con motivo), se il vecchio rapporto resta come diagnostica o come gate, e come si certifica la corsa del soak gia' fatta (il verdetto di P0 'rest_revision_ratio_within_limit' e' falso: va lasciato falso nel file e annotato, oppure ricalcolato?).
A2. Va aggiunta una mitigazione (non sul gate ma sul sistema)? Opzioni da valutare: (a) nessuna, perche' impatto trascurabile; (b) ricalcolo dei soli token del giorno corrente (vol_z_1d, VWAP) al passaggio 00:00 UTC con le candele rilette; (c) alzare S solo di notte (le revisioni sono notturne); (d) escludere dal training/validation i minuti di bassa liquidita' notturna o usare per l'addestramento barre 'a 120 s' invece di quelle definitive (cosi' lo skew sparisce). Quale hai evidenza di preferire, con quali numeri?

## Parte B - regola di promozione del challenger (L5, `modules/dryrun/registry.py::decide_promotion`)

Implementata come da spec 5.1.6, con queste letture dello sviluppatore (nessuna soglia nella spec): il challenger diventa champion solo se (a) nel backtest walk-forward degli ultimi `backtest_recent_days` = 90 giorni batte il champion: PnL netto STRETTAMENTE maggiore, Sharpe STRETTAMENTE maggiore, max drawdown non peggiore (<=); nessuna soglia assoluta (se entrambi sono negativi vince il meno negativo); e (b) ha girato come book `challenger` in dry per almeno `min_shadow_days` = 14 giorni senza anomalie operative ne' di parita'. 'Campione insufficiente' (nessuna promozione, si riprova): finestra di backtest < 90 giorni, Sharpe non definito, shadow < 14 giorni; la spec non fissa un numero minimo di trade ne' N_eff per il backtest. Il PnL dei book dry dopo 14 giorni NON e' un criterio (e' rumore, 5.1.6). Il learner non promuove mai da solo e non muta il champion.

### Domande B
B1. Le letture sono finanziariamente giuste? In particolare: 'batte' con PnL e Sharpe strettamente maggiori e drawdown non peggiore, senza soglia assoluta, ti sembra sufficiente o serve (per esempio) che il challenger abbia PnL > 0 in assoluto, un numero minimo di trade/N_eff nel backtest (quale, in base a K5: servono ~345 trade indipendenti per un edge di 0,08), un margine minimo di superiorita' (differenza non entro il rumore: intervallo bootstrap sulla differenza di PnL) per evitare di scambiare un champion con un altro per caso?
B2. Il rollback: cosa deve poter fare il sistema (solo dal canale locale, mai da GUI/comando remoto, come implementato) e con quale criterio automatico o manuale?
B3. Le soglie 90 giorni / 14 giorni sono adeguate dato che N_eff cresce di circa 254 campioni l'anno?

## Risposta trader-quant

Autore: trader-quant. Data: 2026-10-20. Ambito: A1, A2, B1, B2, B3. Legenda: [M] misurato nei nostri dati (fonte indicata);
[D] derivato con una formula da numeri misurati; [G] conoscenza generale, non verificata sui nostri dati; [?] dato mancante.
Vincoli invariati: solo spot, long-only, nessuna leva, copertura 100%, perdita massima di un ordine = il suo notional. Nessuna
regola qui sotto allenta fee floor, EV gate, G0-G4, GE (E1-E5) o G-DRY-ECON.

Costanti usate (tutte gia' nei documenti): `sigma_r` = 584 bps e `W + L` = 1.116 bps (K5, P1c) [M]; `N_min = 2,21 / delta^2`
trade indipendenti per un test a un campione (5% bilaterale, potenza 80%) [D, K5]; campioni indipendenti del pool 254/anno =
0,70/giorno [M]; rho 0,54 [M]; passo della griglia `p_be_buffer` 0,02 su 0..0,08 [spec 2.10]; token `costs.p_breakeven`
arrotondato a 2 decimali [K7 P1c]; `min_net_edge_bps` 30 [config].

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

### Parte B - promozione del challenger (L5)

**B0. Potenza del confronto sui 90 giorni [D].**
- Campioni: 0,70 indipendenti/giorno nel pool [M] -> 90 giorni = 63. Un trade entra nel backtest solo se la sua finestra di label
  e' chiusa (`label_window_end <= now`, ~15,5 giorni): finestra utile ~74,5 giorni -> **N <= 52**. E' un tetto (book sempre in
  posizione su tutti i prodotti). Con esposizione `phi` (quota di tempo in posizione) `N_eff_trade ~ 52 phi`; con phi 0,3-0,6 ->
  **15-31**. [?] phi va letto dal backtest del modello.
- Effetto minimo rilevabile sulla DIFFERENZA fra due modelli (stessa costante di K5; la varianza della differenza raddoppia; i
  trade in comune, stesso t e prodotto, danno differenza esattamente zero, quota `f`):
  `delta_D = sqrt(4,42 x (1 - f) / N)`, in bps `delta_D x 1.116`.
  N = 52, f = 0: 0,29 (326 bps per trade); N = 26, f = 0: 0,41; N = 52, f = 0,5: 0,21.
  Il lift richiesto dal pareggio e' 0,074 e l'AUC misurata implica +0,02/+0,03 sopra `p_be` (P1c). Sui 90 giorni il confronto non
  distingue nemmeno una differenza 4 volte piu' grande dell'intero edge cercato.
- Bootstrap: blocchi di `ceil_days(T_in + F + H)` ~16 giorni -> 90 giorni = 5-6 blocchi: quantili non affidabili [G].
- Sharpe: SE dello Sharpe annualizzato ~ `sqrt((1 + SR^2 / 2) / T_anni)` [G, Lo 2002]; T = 0,25 anni -> SE ~ 2,0. Differenze di
  Sharpe sotto ~3-4 sono rumore. Max DD su 90 giorni: un solo cammino, nessuna stima d'errore.
- Effetto della regola attuale [D]: se i due modelli sono uguali in verita', P(PnL_ch > PnL_cp) = 0,5; la differenza di Sharpe ha
  quasi sempre lo stesso segno della differenza di PnL; DD non peggiore circa 1 volta su 2 -> P(promozione) ~ 0,25-0,35 per
  tentativo. Con L3 mensile (12 tentativi/anno, finestre sovrapposte per 2/3) ci si aspettano ~2-4 cambi di champion all'anno
  senza nessun miglioramento reale. Se il challenger e' PEGGIORE di 0,02 (22 bps per trade): SE della differenza
  `584 x sqrt(2 / 52) = 115 bps`, `z = 0,19`, P(PnL_ch > PnL_cp) = 0,42: lo si promuove quasi con la stessa frequenza.
  La regola, da sola, scambia il champion a caso.

**B1. Le letture non bastano. Regola proposta: si AGGIUNGONO condizioni, nessuna di quelle scritte viene tolta.**
Principio: in promozione il default e' tenere il champion; cambiare richiede evidenza. L'evidenza esiste solo sul walk-forward
completo (fold di test concatenati, dove GE chiede gia' N_eff_trade >= 100); i 90 giorni restano come filtro di recenza.

P0 Validita' del confronto (se manca: `insufficient`, mai promozione):
- V1 Fuori campione per entrambi. Sui 90 giorni ciascun modello deve essere valutato con dati di training (con purge su
  `label_window_end`) che finiscono prima di `inizio finestra - embargo`. Se il challenger L3 e' addestrato "fino a ieri" e
  valutato sugli ultimi 90 giorni con il modello finale, il confronto e' in-sample per il challenger e lo favorisce per
  costruzione: si usa il modello del fold walk-forward il cui test copre la finestra. Stessa regola per il champion.
- V2 Stessa finestra, stesso CostModel (fee, spread, slippage), stesso capitale iniziale, cadenza 1 min, stesso fill model, solo
  trade con `label_window_end <= now`.
- V3 Challenger con >= 30 trade nella finestra (= `gate_econ.min_trades` e `MIN_TRADES` del tuning). Sotto: `insufficient`.

P1 Evidenza assoluta del challenger:
- G0, G1, G2, G4 superati (G3 se e' un LLM) e **GE = GO** sul proprio walk-forward (sez. 3.4). E' lo stesso prerequisito che R0
  chiede al champion: un challenger non GO non puo' andare in live, quindi promuoverlo cambierebbe il riferimento del dry senza
  prova e senza avvicinare il live. GE = INCONCLUSO -> `insufficient`; GE = NO-GO -> `reject`.

P2 Superiorita' con evidenza (solo se anche il champion ha GE = GO; `reject` se fallisce):
- S1 Bootstrap a blocchi ACCOPPIATO sull'OOS comune: gli stessi blocchi temporali per i due modelli, blocchi
  `ceil_days(T_in + F + H)` circolari, tutti i prodotti nello stesso blocco, 10.000 repliche, seed fisso (come GE). Statistica
  `D = PnL_ch - PnL_cp` in EUR sullo stesso capitale. Condizione: **quantile 5% di D > 0** (unilaterale 95%).
- S2 Sull'OOS comune: Sharpe_ch > Sharpe_cp e MaxDD_ch <= MaxDD_cp (la regola attuale, spostata dove ha campione).
- S3 MaxDD_ch <= `gate_econ.max_drawdown_frac` (0,05) sull'OOS e sui 90 giorni (stesso numero di G-DRY-ECON, non allentato).
- Nessun margine aggiuntivo in bps oltre S1: il quantile al 95% e' gia' il margine "fuori dal rumore"; un margine fisso sarebbe
  un numero arbitrario. Con N_eff_trade 100-150 e f = 0,5, S1 rileva (potenza 80%) solo differenze `delta_D` ~0,12-0,15
  (135-165 bps per trade) [D]: miglioramenti grandi (per esempio informazione nuova come lo storico news), non i refit mensili.
  E' il comportamento voluto.

P3 Filtro di recenza sui 90 giorni (la regola attuale, mantenuta, piu' il PnL assoluto; `reject` se fallisce):
- PnL_ch,90 > 0; PnL_ch,90 > PnL_cp,90; Sharpe_ch,90 > Sharpe_cp,90; MaxDD_ch,90 <= MaxDD_cp,90.
- Dichiarato SENZA potenza: non prova la superiorita', scarta un challenger che nel periodo recente perde o va peggio
  (protezione da rottura di regime, stessa logica di G0/E5). Costo: un challenger davvero migliore di 0,08 ha
  P(PnL_90 < 0) ~ 20% [D: z = 89 / (584 / sqrt(30)) = 0,83] e circa 1 su 2 di perdere uno dei confronti puntuali: viene
  rimandato al mese dopo. Costo accettabile e asimmetrico: rinviare una promozione non mette a rischio capitale, promuovere a caso
  si'.
- Modello senza trade nella finestra: PnL 0, Sharpe 0 (serie di rendimenti nulla), DD 0, cioe' definiti e non `insufficient`
  (altrimenti un champion che non opera bloccherebbe per sempre un challenger GO). Con champion senza trade il confronto del DD
  di P3 e' sostituito da S3 (assoluto).

P4 Shadow (invariata): >= 14 giorni come book `challenger`, zero anomalie operative e di parita', inclusi R1-R4 della Parte A
calcolati con il challenger.

Esiti:
- Champion senza GE = GO (situazione attuale): P2 non si applica; valgono P0, P1, P3, P4.
- Nessun challenger con GE = GO (situazione attuale: subset non tarabili): **nessuna promozione**. E' il default finche' i dati non
  bastano, e il book champion del dry resta quello che e'.
- Refit mensile della stessa procedura (stesse feature, iperparametri e `label_config`, solo piu' dati): per costruzione non supera
  quasi mai S1, perche' un refit non porta evidenza di miglioramento. Non e' un difetto della regola. Se l'utente vuole
  "rinfrescare" il champion con i dati nuovi, e' una decisione di manutenzione UMANA (CLI locale), fuori da L5, ammessa solo se il
  refit soddisfa P0, P1, P4 e PnL_ch,90 > 0. Decisione per l'utente, non regola automatica.
- Tentativi ripetuti: 12 all'anno su OOS quasi coincidenti non sono test indipendenti; il livello 5% per tentativo resta
  accettabile. Una versione respinta non si ripresenta: ogni tentativo e' una versione nuova.

**B2. Rollback.**
Cosa deve poter fare il sistema:
- Riportare `champion` alla versione precedente (pila `previous`, integrita' rivalidata: gia' implementato), solo da CLI locale.
  Mai da GUI o canale remoto: un comando remoto che cambia il decider e' un vettore di rischio (errore o accesso non autorizzato).
- Effetto al reload successivo (00:30 UTC), coerente con "nessun parametro cambia durante la giornata". L'azione protettiva
  immediata non e' il rollback ma il blocco degli ingressi (kill switch / fail closed), che agisce in secondi: il rollback non deve
  essere istantaneo.
- Posizioni aperte: il rollback non apre, non chiude e non modifica ordini (C11: nessun ordine nasce da un cambio di versione).
  Ogni posizione resta governata dal proprio bracket, dall'orizzonte H e dal watchdog. Se il `label_config_sha256` della versione
  ripristinata differisce da quello della versione che ha aperto la posizione, la SELL del nuovo decider su quella posizione non si
  applica (la testa SELL e' addestrata su altre barriere); restano bracket, H e watchdog.
- Registrare `model_version_changed` con motivo `rollback`, versione di partenza e di arrivo; le metriche post-promozione si
  contano per versione.
Criteri:
- Automatici (nessun giudizio statistico, solo difetti; durante i primi 14 giorni dopo una promozione e poi sempre): parita'
  decisioni < 99%, R1-R4 falliti con la nuova versione, fallback del decider oltre `max_fallback_ratio_1h` (0,20), versione non
  caricabile o corrotta, kill switch per bug. Azione automatica = **blocco degli ingressi del book interessato (fail closed) e
  allarme**, non lo spostamento del puntatore. Il rollback lo esegue l'umano con la CLI locale. Motivo: il blocco degli ingressi
  azzera il rischio senza mutare il champion (coerente con "il learner non muta il champion"); un rollback automatico sarebbe
  finanziariamente sicuro, ma aggiunge un percorso automatico di scrittura del registry senza ridurre di piu' il rischio.
- Su evidenza economica (raccomandazione stampata nel report, decisione umana), solo dopo >= 30 trade chiusi della versione
  promossa (stessa soglia di G-DRY-ECON e della rampa): (a) PF < 1,0 (in live la rampa scende gia' di un gradino); (b) incoerenza
  con il backtest nella forma di E5: `z = (m_post - m_oos) / sqrt(se_post^2 + se_oos^2) < -1,96`, m = media di `net_ret` in bps,
  se calcolati con N_eff_trade; (c) >= 2 scatti del kill switch per perdita (non per bug). Sotto 30 trade nessuna
  raccomandazione economica: SE della media = 584 / sqrt(30) = 107 bps, piu' dell'edge atteso.
- La versione rimossa con un rollback puo' tornare champion solo con un nuovo L5 completo.

**B3. Soglie 90 / 14 giorni.**
- 14 giorni di shadow: **adeguati e invariati** per lo scopo, che e' operativo. 14 x 1.440 = 20.160 minuti per prodotto: la parita'
  al 99% si misura con SE `sqrt(0,01 x 0,99 / 20.160) = 0,0007` [D]; 14 = 2 x `gate_ops.min_days`. Nessuna label si risolve prima
  di ~15,5 giorni: e' giusto che il PnL dello shadow non sia un criterio.
- 90 giorni: **inadeguati come prova** (B0), **adeguati come filtro di recenza** (P3): >= 5 blocchi del bootstrap e, con phi >=
  0,6, ~30 trade indipendenti. Allungare non risolve: con 365 giorni N <= ~243 -> `delta_D = sqrt(4,42 / 243) = 0,135`, ancora
  quasi il doppio del lift richiesto, e la finestra smette di essere recente. Per questo la prova sta su tutto l'OOS (P2) e i 90
  giorni restano 90.
- Crescita della potenza: l'OOS cresce al massimo di ~254 campioni l'anno (in trade, ~254 phi). `delta_D(N)` con f = 0 / f = 0,5:
  N = 100 -> 0,21 / 0,15; N = 200 -> 0,15 / 0,105; N = 400 -> 0,105 / 0,074 [D]. Una differenza grande quanto il lift richiesto
  (0,074) diventa dimostrabile solo con ~400 trade indipendenti condivisi per meta': anni di dati. S1 stringe da solo con la
  crescita dell'OOS, quindi non servono soglie che cambiano nel tempo, ne' una soglia di N_eff oltre E1 (N_eff_trade >= 100,
  gia' implicita in GE = GO).
- Config: `promotion: {backtest_recent_days: 90, min_shadow_days: 14}` invariati. Parametri nuovi (nomi all'architetto): minimo
  30 trade del challenger nella finestra recente, quantile 0,05 e 10.000 repliche del bootstrap accoppiato (come GE), DD assoluto =
  `gate_econ.max_drawdown_frac`.

Decisioni per l'utente: (1) sostituzione del gate `rest_revision_ratio` con R1-R4 (ratio solo diagnostica) e certificazione di P0
come sopra; (2) refit di manutenzione manuale fuori da L5; (3) reazione automatica = blocco ingressi + rollback manuale (scelta
raccomandata) oppure rollback automatico sui soli difetti.

## Dati aggiuntivi richiesti dal quant (misurati dallo sviluppatore il 2026-10-20)

- `fresh_only` = 1 per BTC e SOL: e' il minuto `2026-10-20 05:40` UTC, cioe' il bordo della finestra di rilettura (END esclusivo, REST ha restituito una candela in piu'): ARTEFATTO di finestra, non un minuto comparso. Minuti presenti solo nello store: 234 / 450 / 350 (BTC/ETH/SOL), tutti barre piatte senza trade (REST non restituisce i minuti senza scambi): atteso. Nessun minuto davvero comparso o scomparso (R4 passa).
- Stati con spostamento di p_edge sopra soglia (2.690 stati per prodotto, modello reale in uso): BTC: > 0,005: 3, > 0,01: 0, > 0,02: 0; ETH: > 0,005: 3, > 0,01: **1** (0,037%), > 0,02: 0; SOL: 0 / 0 / 0. Quindi R1 (<= 0,5% degli stati con spostamento > 0,01) passa per tutti e tre i prodotti, R2 (spostamento massimo <= 0,02) passa; R3 (decisioni BUY/SELL che cambiano) non e' valutabile con la policy non tarabile del modello attuale (nessuna soglia di acquisto): resta da misurare con un modello tarabile o con la policy del benchmark.
- Script riproducibili nel repo: `script/soak_revision_classify.py`, `script/soak_revision_impact.py` (`python -m script.<nome>`).

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

### B. Regola di promozione L5

#### B.1 Cosa succede oggi

Nessuna promozione e nessuna possibile: (1) per K7 L5 valuta sempre una versione con 0 giorni di shadow; (2) con la regola nuova
il challenger deve avere GE = GO, e i modelli attuali hanno policy `final` non tarabile (GE != GO). Il book `champion` del dry
resta quello che e'. L5 gira solo nel giorno di refit (`refit_day_of_month`, oggi l'1 del mese) e non promuove mai: riporta il
verdetto.

#### B.2 Dati: `PromotionConfig`, `PromotionRule`, `PromotionEvidence`

`modules/config.py`:
```python
class PromotionConfig(_Section):
    backtest_recent_days: int = Field(default=90, ge=1)
    min_shadow_days: int = Field(default=14, ge=1)
    superiority_quantile: float = Field(default=0.05, gt=0, lt=0.5)   # S1
class LearnerConfig(_Section):
    ...
    promotion: PromotionConfig = PromotionConfig()   # il YAML {backtest_recent_days: 90, min_shadow_days: 14} resta valido
```
Minimo di trade e drawdown NON hanno chiavi nuove: si leggono da `gate_econ.min_trades` (30) e `gate_econ.max_drawdown_frac`
(0.05), cosi' L5 e G-DRY-ECON non possono divergere. Repliche (10.000) e seed del bootstrap: le costanti di GE importate da
`training/evaluate.py` (nessun secondo valore). Ogni accesso esistente `cfg.promotion["..."]` diventa accesso ad attributo nello
stesso commit.

`modules/dryrun/registry.py`:
```python
@dataclass(frozen=True)
class BacktestEvidence:                 # finestra recente (P3), stessa per i due modelli
    window_start: datetime
    window_end: datetime
    window_days: float
    n_trades: int
    net_pnl: Decimal
    sharpe: float | None                # 0.0 se n_trades == 0; None solo se ci sono trade e non e' definito
    max_dd_frac: float

@dataclass(frozen=True)
class GateSummary:                      # da evaluation.json della versione
    ge: Literal["GO", "INCONCLUSO", "NO-GO"] | None    # None: evaluation.json assente
    passed: Mapping[str, bool | None]   # chiavi richieste: G0, G1, G2, G4 (+ G3 se il backend e' un LLM)

@dataclass(frozen=True)
class OosEvidence:                      # OOS walk-forward comune (P2, S3)
    common_days: float
    n_trades_ch: int
    max_dd_ch: float
    n_trades_cp: int | None             # None se il champion non e' GO (parte accoppiata non calcolata)
    d_pnl_quantile: float | None        # quantile superiority_quantile di D = PnL_ch - PnL_cp (EUR)
    sharpe_ch: float | None
    sharpe_cp: float | None
    max_dd_cp: float | None

@dataclass(frozen=True)
class PromotionEvidence:
    challenger: BacktestEvidence
    champion: BacktestEvidence
    challenger_gates: GateSummary
    champion_gates: GateSummary
    oos: OosEvidence | None             # None: artefatti OOS del challenger assenti
    oos_provable: bool                  # V1 (K13)
    shadow_days: float
    operational_anomalies: int
    parity_anomalies: int               # comprende R1-R3 della versione (A.5)

@dataclass(frozen=True)
class PromotionRule:
    backtest_recent_days: int = 90
    min_shadow_days: int = 14
    min_recent_trades: int = 30
    max_drawdown_frac: float = 0.05
    superiority_quantile: float = 0.05
    @classmethod
    def from_config(cls, learner: LearnerConfig, econ: GateEconConfig) -> PromotionRule
```
La GE string usa le costanti di `training/evaluate.py`, non letterali riscritti.

#### B.3 `decide_promotion` (funzione pura, ordine esatto)

```
reject, short = [], []
# 1. definitivi, valutati sempre
ge = ev.challenger_gates.ge
if ge == "NO-GO":                                   reject += "challenger_ge_nogo"
for k, v in passed: if v is False:                  reject += f"challenger_{k}_failed"
if ev.operational_anomalies > 0:                    reject += "operational_anomalies"
if ev.parity_anomalies > 0:                         reject += "parity_anomalies"
# 2. sufficienza (P0 V1-V3, P1, P4)
if ge is None:                                      short += "challenger_evaluation_missing"
if ge == "INCONCLUSO":                              short += "challenger_ge_inconclusive"
for k required: if passed.get(k) is None:           short += f"challenger_{k}_missing"
for (label, bt): if not finite(bt.window_days) or bt.window_days < rule.backtest_recent_days:
                                                    short += f"{label}_backtest_window_short"
if (ch.window_start, ch.window_end) != (cp.window_start, cp.window_end): short += "backtest_windows_differ"
if not ev.oos_provable:                             short += "oos_not_provable"
if ch.n_trades < rule.min_recent_trades:            short += "challenger_recent_trades_short"
for (label, bt): if bt.n_trades > 0 and not finite(bt.sharpe): short += f"{label}_sharpe_undefined"
if not finite(ev.shadow_days) or ev.shadow_days < rule.min_shadow_days: short += "shadow_days_short"
champion_go = ev.champion_gates.ge == "GO"
if ev.oos is None:                                  short += "oos_unavailable"
elif champion_go and (oos.d_pnl_quantile, oos.sharpe_ch, oos.sharpe_cp, oos.max_dd_cp) has None:
                                                    short += "paired_oos_unavailable"
if reject: return PromotionDecision("reject", reject + short)
if short:  return PromotionDecision("insufficient", short)
# 3. P3 recenza (90 giorni), confronti stretti
if not ch.net_pnl > 0:                              reject += "recent_pnl_not_positive"
if not ch.net_pnl > cp.net_pnl:                     reject += "recent_pnl_not_better"
if not ch.sharpe > cp.sharpe:                       reject += "recent_sharpe_not_better"
if cp.n_trades > 0 and not ch.max_dd_frac <= cp.max_dd_frac: reject += "recent_max_dd_worse"
# 4. S3 assoluto (sempre)
if ch.max_dd_frac > rule.max_drawdown_frac:         reject += "recent_max_dd_above_limit"
if oos.max_dd_ch > rule.max_drawdown_frac:          reject += "oos_max_dd_above_limit"
# 5. P2 solo se il champion e' GO
if champion_go:
    if not oos.d_pnl_quantile > 0:                  reject += "oos_pnl_difference_not_significant"
    if not oos.sharpe_ch > oos.sharpe_cp:           reject += "oos_sharpe_not_better"
    if not oos.max_dd_ch <= oos.max_dd_cp:          reject += "oos_max_dd_worse"
return PromotionDecision("reject", reject) if reject else PromotionDecision("promote")
```
`promote_if_eligible` resta com'e' (lo usa solo chi lo chiama esplicitamente; il learner non lo chiama).

#### B.4 Raccolta delle evidenze nel learner (correzioni K7-K10, K13)

- Inizio notte: `doc["challenger_before"] = previous.get("challenger_before", alias(CHALLENGER))`, come `champion_before`.
- `_gate_model_step`: L5 richiede solo il giorno di refit; NON dipende dagli esiti di L3/L1/L2. L5 valuta
  `challenger_before` (via `get_version`, da aggiungere a `_ChallengerOnly` in sola lettura). `Skip("no_previous_challenger")`
  se manca; `Skip("challenger_is_champion")` se coincide col champion.
- Finestra comune: `end = min(ultima barra di tutti i prodotti, cutoff)`; `start = max(holdout_start_ch, holdout_start_cp,
  end - backtest_recent_days)`. `RecentBacktester.__call__(model_dir, start, end) -> BacktestEvidence`.
- `HoldoutBacktester`: policy `untunable` -> `BacktestEvidence(start, end, days, 0, Decimal(0), 0.0, 0.0)`; altrimenti
  `n_trades` dalla trade list del backtest, Sharpe/DD dalle metriche del backtest (una sola definizione).
- V1: `oos_provable = final_train_end_ch <= start - embargo and final_train_end_cp <= start - embargo` (embargo = quello di
  `purged_walk_forward`, 2 giorni); campo assente -> False.
- `GateSummary` da `<versione>/evaluation.json`; `_gate_flags` con G3 e G4 (K10). Champion senza `evaluation.json` -> `ge=None`
  (quindi non GO: P2 non si applica).
- `OosComparator` (collaboratore iniettabile come `HoldoutBacktester`) -> `OosEvidence | None` (B.5).
- Il documento di L5 riporta `verdict`, `reasons`, `promoted: false`, `evidence` e la riga di comando locale per promuovere.

#### B.5 Artefatti OOS e bootstrap accoppiato

- `training/evaluate.py` scrive nella directory del modello: `oos_trades.jsonl` (un trade OOS per riga, campi della trade list
  della sez. 3.5 punto 8: `product, fold, decision_ts, fill_ts, exit_ts, exit_reason, net_ret_bps, pnl_eur, notional_eur`, piu'
  `neff_weight` di E1) e `oos_spans.json` (gli span di test `[start, end)` dei fold, il `label_config_sha256`, la lunghezza dei
  blocchi). `baseline_lgbm` scrive `data.final_train_end` nel model card.
- Funzione pura `paired_oos_comparison(trades_ch, trades_cp, spans_ch, spans_cp, block_days, reps, seed, q, starting_quote)`:
  - OOS comune = intersezione degli span; trade fuori dall'intersezione esclusi;
  - blocchi: la STESSA funzione di assegnazione e di estrazione dei blocchi di E2 (Sonnet la estrae da `evaluate` in una
    funzione condivisa, senza duplicarla), lunghezza `ceil_days(T_in + F + H)` (se i due `label_config` differiscono, la
    maggiore), circolari, tutti i prodotti nello stesso blocco, trade assegnato al blocco del proprio `decision_ts`;
  - per replica, gli stessi indici di blocco per i due modelli: `D = sum(pnl_ch nei blocchi) - sum(pnl_cp nei blocchi)`;
    risultato `quantile(D, q)`;
  - Sharpe e DD sull'OOS comune: serie giornaliera dei PnL in EUR (giorni senza uscite = 0) sopra `starting_quote`, con le
    stesse funzioni di metrica del backtest (se non sono pubbliche si estraggono, non si copiano).
- Champion non GO: si calcolano solo `n_trades_ch`, `max_dd_ch`, `sharpe_ch` del challenger; i campi del champion restano None.

#### B.6 Rollback

- Meccanismo: la CLI locale esistente `python -m modules.dryrun.registry rollback champion`, con argomento nuovo obbligatorio
  `--reason TEXT` (anche per `promote`). Nessun comando nel canale remoto o nella GUI (test esistente sul package
  `modules/control` invariato).
- `aliases.json`: ogni voce aggiunge `op` (`"promote"|"rollback"`) e `reason`; un file vecchio senza questi campi si legge come
  `op="promote"`, `reason=""`; lo schema resta `chronos.registry.aliases.v1` (campi opzionali in piu').
- Effetto: al reload successivo delle `learner.reload_at_utc` (00:30 UTC); la CLI stampa "effettivo al reload delle 00:30
  UTC". Al reload l'engine registra `model_version_changed {from, to, op, reason}`. Sonnet: se il reload delle 00:30 non e'
  ancora implementato in `modules/app.py`/`engine.py`, il log si aggiunge quando lo si implementa e l'obbligo va in
  `tests/test_obligations.py`.
- Posizioni aperte: nessun ordine nasce dal cambio di versione. Nuova regola dell'engine: su una posizione LONG aperta da una
  versione con `label_config_sha256` diverso da quello della versione corrente, la SELL del modello non si applica (decisione
  HOLD, motivo `label_config_mismatch`); bracket, orizzonte H della posizione e watchdog restano. Sonnet verifica che la
  posizione registri versione e sha all'ingresso (altrimenti li aggiunge al record d'ingresso) e che l'orizzonte usato sia
  quello della posizione.
- Una versione tolta con un rollback torna champion solo con un nuovo L5 completo (o con la CLI, che e' un atto umano).

#### B.7 Raccomandazioni nel report (nessuna azione)

Blocco giornaliero `rollback_advice` per la versione champion corrente:
`{"version", "since", "closed_trades", "triggers": [...], "advice": "none"|"consider_rollback", "command":
"python -m modules.dryrun.registry rollback champion --reason \"...\""}`.
- Difetti (subito, senza minimo di trade): `decision_parity` < `min_decision_parity`; R1-R4 falliti con la versione; fallback
  del decider oltre `risk.kill_switch.max_fallback_ratio_1h`; versione non caricabile; kill switch per bug.
- Economici (solo con >= `gate_econ.min_trades` trade chiusi della versione, `MANUAL` esclusi): PF < 1.0; `z = (m_post -
  m_oos) / sqrt(se_post^2 + se_oos^2) < -1.96` (m in bps di `net_ret`, se con `N_eff_trade`, la stessa funzione di E5; `m_oos`,
  `se_oos` da `oos_trades.jsonl` della versione); >= 2 scatti del kill switch per perdita (guardie di perdita, `bug=false`)
  dalla data della versione.
- Attribuzione dei trade alla versione: tramite la versione registrata all'ingresso (B.6); senza, l'economia e'
  `unmeasured` (mai zero).
- Il report non scrive mai nel registry ne' nell'inbox dei comandi.

#### B.8 Test richiesti (Parte B)

`tests/test_registry.py`:
`test_promotion_rule_from_config_reads_learner_and_gate_econ`, `test_l5_insufficient_window_short`,
`test_l5_insufficient_windows_differ`, `test_l5_insufficient_oos_not_provable`,
`test_l5_insufficient_challenger_evaluation_missing_or_inconclusive`, `test_l5_reject_ge_nogo_even_with_short_shadow`,
`test_l5_reject_gate_g4_failed`, `test_l5_insufficient_recent_trades_below_min`, `test_l5_zero_trade_model_has_defined_metrics`,
`test_l5_reject_recent_pnl_not_positive`, `test_l5_reject_recent_ties` (PnL o Sharpe uguali -> reject),
`test_l5_dd_vs_champion_skipped_when_champion_has_no_trades`, `test_l5_reject_dd_above_limit_recent_and_oos`,
`test_l5_p2_skipped_when_champion_not_go`, `test_l5_reject_paired_quantile_not_positive` (quantile = 0 -> reject),
`test_l5_reject_oos_sharpe_or_dd_worse`, `test_l5_reject_anomalies`, `test_l5_promote_when_all_conditions_hold`,
`test_l5_reasons_list_every_failed_condition`, `test_current_models_never_promoted` (challenger untunable -> mai promote),
`test_rollback_records_op_and_reason`, `test_aliases_without_op_still_load`, `test_cli_requires_reason`,
`test_cli_rollback_says_effective_at_reload`.

`tests/test_oos_compare.py` (nuovo):
`test_common_oos_is_span_intersection`, `test_identical_models_quantile_is_zero`, `test_same_blocks_for_both_models`,
`test_bootstrap_deterministic_with_seed`, `test_large_difference_detected`, `test_block_length_from_label_config`,
`test_block_assignment_is_the_ge_one`, `test_oos_metrics_reuse_backtest_definitions`.

`tests/test_learner.py`:
`test_l5_evaluates_challenger_before_refit`, `test_l5_runs_when_refit_failed`, `test_l5_skips_without_previous_challenger`,
`test_l5_common_window_from_both_holdouts_and_cutoff`, `test_holdout_backtester_untunable_zero_trades`,
`test_gate_flags_include_g3_g4`; il test esistente "il learner non muove il champion" resta.

`tests/test_evaluate.py` (o il file di test di `training/evaluate`): `test_evaluate_writes_oos_trades_and_spans`,
`test_oos_trades_count_matches_ge_e1`, `test_model_card_has_final_train_end`.

Report: `test_rollback_advice_none_below_min_trades`, `test_rollback_advice_pf_below_one`,
`test_rollback_advice_z_below_threshold`, `test_rollback_advice_two_loss_trips`, `test_rollback_advice_defects_immediate`,
`test_rollback_advice_never_writes_registry_or_inbox`.

Engine: `test_sell_ignored_when_position_label_config_differs`, `test_model_version_changed_logs_op_on_reload` (quando il
reload esiste).

#### B.9 Spec (Parte B)

5.1.6 riga L5 sostituita da: "Challenger -> champion solo con verdetto `promote` di `decide_promotion`: (P0) finestra comune
degli ultimi 90 giorni fuori campione per entrambi (`final_train_end <= inizio - embargo`), stessi costi e capitale, >= 30 trade
del challenger; (P1) challenger con G0, G1, G2, G4 (G3 se LLM) e GE = GO; (P2, solo con champion GE = GO) quantile 5% del
bootstrap a blocchi accoppiato della differenza di PnL sull'OOS comune > 0, Sharpe maggiore e DD non peggiore sull'OOS;
(P3) sui 90 giorni PnL > 0, PnL e Sharpe strettamente maggiori, DD non peggiore (se il champion ha trade); DD <= 5% sui 90
giorni e sull'OOS; (P4) >= 14 giorni di shadow senza anomalie operative ne' di parita', R1-R4 compresi. L5 valuta il
challenger in carica prima del refit della notte; il learner non promuove mai." Regole invarianti: "rollback = CLI locale
`python -m modules.dryrun.registry rollback champion --reason ...`, effetto al reload delle 00:30, nessun ordine generato; la
SELL del modello non si applica a posizioni aperte con un `label_config` diverso". 5.2: chiavi di `promotion`.

### C. Ordine dei passi e stima

| Passo | Contenuto | Dipende da | Stima |
|-------|-----------|------------|-------|
| 1 | `classify_revisions` + `RevisionStats` estesa + test | - | 0,5 h |
| 2 | `masked_buy_prob`/`masked_sell_prob` + maschera di azionabilita' + test | - | 1 h |
| 3 | Chiavi nuove di `GateOpsConfig` + test | - | 0,5 h |
| 4 | `revision_impact.py` (`apply_revisions`, `revised_world` con `fill_flat_bars` (K15), nucleo, LONG, CLI `certify`) + test, compreso il caso sintetico | 1-3 | 4,5 h |
| 5 | Esecuzione di `certify` sul soak concluso (rete ~15 min); se `fail`: STOP e nuovo CONFLITTO | 4 | 0,5 h |
| 6 | `soak_verdict` + test | 1, U1 | 0,5 h |
| 7 | L0: file delle revisioni, mondo rivisto, impatto per (book, versione) + test | 4, U1 | 3 h |
| 8 | Report: criteri R1-R4, diagnostica, allarmi, anomalie per versione, Markdown + test | 7 | 2 h |
| 9 | Spec 2.11 (parte A), decisione 2026-10-20, nota sulla 2026-10-18, `tasks/todo.md` | 5, U1 | 1 h |
| 10 | Diagnostica "label live che cambiano sul mondo rivisto" (L0, non gate) | 7 | 1,5 h |
| 11 | `PromotionConfig`, dataclass, `decide_promotion` + test | - | 3 h |
| 12 | `evaluate`: `oos_trades.jsonl`, `oos_spans.json`, `final_train_end`, `_gate_flags` + test (STOP se K13 fallisce) | - | 3 h |
| 13 | `paired_oos_comparison` + metriche condivise + test | 12 | 3 h |
| 14 | L5 nel learner: `challenger_before`, finestra comune, backtester, collaboratori + test | 11-13 | 3 h |
| 15 | Registry `op`/`reason`, CLI `--reason`, log al reload + test | U2 | 1,5 h |
| 16 | `rollback_advice` nel report + attribuzione dei trade alla versione + test | 12, 15 | 2,5 h |
| 17 | Regola SELL su `label_config` diverso (engine) + test | 15 | 1,5 h |
| 18 | Spec 2.11 (parte B) | 11-17 | 1 h |

Totale: parte A ~15 h, parte B ~18,5 h (circa 4 sessioni piene). Il risultato che sblocca P0 (passo 5) arriva dopo ~7 h.
Ogni passo chiude con `pytest`, `mypy --strict` e `ruff` verdi sui file toccati. I passi 7, 8, 14, 16 e 17 toccano file che
altri agenti stanno modificando (`learner.py`, `report.py`, `app.py`/engine): si fanno dopo che quei lavori sono stati
integrati, mai in parallelo sugli stessi file.

### D. Decisioni che spettano all'utente (l'architetto non le prende)

- **U1** Approvare la sostituzione del gate "Barre REST" con R1-R4 (ratio solo diagnostica con allarme a 0.005), il superamento
  della frase Q3.2 della decisione 2026-10-18 e la certificazione di P0 con R3 rinviato a G-DRY-OPS. Senza U1 si fermano i passi
  6-9 (i passi 1-5 misurano soltanto).
- **U2** Promozione manuale dalla CLI: (a) resta un atto umano che scavalca L5, con `--reason` obbligatorio e stampa dell'ultimo
  verdetto L5 della versione (raccomandato); (b) la CLI rifiuta senza verdetto `promote` salvo `--override`. Comprende la
  proposta del quant sul "refit di manutenzione" fuori da L5 (ammesso solo con P0, P1, P4 e PnL_90 > 0?).
- **U3** Reazione ai difetti di una versione: (a) solo allarme e raccomandazione nel report (implementato con questa
  risoluzione); (b) in piu' blocco automatico degli ingressi del book interessato tramite l'inbox locale dei comandi
  (`pause_entries`, nessuna scrittura nel registry), raccomandazione del quant; (c) rollback automatico. Raccomandazione
  dell'architetto: (a) ora, in dry non c'e' capitale; (b) obbligatoria prima di P4/R0.
