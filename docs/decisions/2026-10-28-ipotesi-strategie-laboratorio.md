# PROPOSTA: ipotesi di strategia per il laboratorio (backtest + dry run) prima di rischiare capitale

Autore: trader-quant. Data: 2026-10-28. Tipo: proposta di pre-registrazione, nessuna modifica a ordini, saldi, `RiskManager`, `BalanceGuard`. Stato: APPROVATA dall'utente il 2026-10-28 (famiglia A-D e metodo, sez. 9); le sezioni 2-6 sono congelate da questo commit, PRIMA di qualunque calcolo. Legenda: [M] misurato o letto nei documenti del repo, [D] dedotto da formula, [S] stima da verificare. ERRATA CORRIGE 2026-10-28 in sez. 10 (dopo la corsa del braccio L di A e B, prima di qualunque calcolo su C e D e prima dell'avvio dei book forward): dove la sez. 10 contraddice le sez. 3-5, vale la sez. 10. Per C e D la corsa offline (script `script/study_cd.py`) segue il testo con l'errata E1-E10, APPROVATA il 2026-10-28 e congelata da questo commit.

Vincoli fissi per tutte le ipotesi: solo spot, solo long o cassa, nessuna leva, nessuno short, ogni ordine coperto al 100% dal saldo in cache (perdita massima di un trade = capitale spot allocato a quel trade). Costi: maker 25 bps, taker 50 bps (tier Intro) [M]; round trip tipico 77 bps (maker + taker + ~2 bps di slippage) [M]; round trip prudente 110 bps (taker + taker + 5 bps di slippage per lato) [D].

## 0. Sintesi onesta

1. Il collo di bottiglia e' il rapporto costo/volatilita' all'orizzonte del trade. Con 77 bps a giro il trading "continuo" (minuti-ore) richiede un potere predittivo che nessun segnale credibile ha (sez. 1). Le sole famiglie compatibili con i costi hanno pochi trade e orizzonti di giorni-settimane, oppure entrano solo in eventi estremi e rari.
2. Ipotesi in ordine di plausibilita' economica: A trend-following giornaliero long/cassa; B rotazione per momentum relativo con filtro di cassa; C mean reversion dopo eccessi di liquidazione con ingresso maker; D compressione di volatilita' + breakout su barre 4h. Scartate: effetti ora/giorno, maker paziente incondizionato (diventa un book di controllo), scalping/grid.
3. A e B si possono testare SUBITO sullo storico giornaliero USD gia' in repo, con un holdout pulito. C e D sul solo dato EUR possono arrivare al massimo a CANDIDATO (dato EUR gia' esplorato); per un test pulito serve scaricare candele intraday USD (approvazione dell'utente).
4. Probabilita' a priori che almeno una ipotesi ottenga un GO: circa 30-40% [S]; che il GO sia qualcosa di diverso dal "tenere crypto nei trend e stare in cassa nei bear" (A): circa 10-15% [S].
5. Con circa 28 EUR anche una strategia eccellente (+30%/anno netto) produce circa 8 EUR/anno [D]. Il laboratorio vale per l'informazione, non per il profitto. Il tier di fee non si puo' abbassare con il volume a questo capitale (sez. 7).

## 1. Il conto: perche' l'orizzonte decide tutto

Con segnale z (normale standard) correlato rho (IC) al rendimento dell'orizzonte h, il rendimento atteso di un trade selezionato e' E[r | selezione] = IC x sigma_h x E[z | selezione] [D]. Selezionando il quintile migliore E[z] = 1,40 [D]. Pareggio sui costi: IC_min = c / (sigma_h x 1,40).

BTC, sigma giornaliera ~2,5% [S, da misurare su EUR 2023-2026], sigma_h = 2,5% x sqrt(h / 1 g), c = 77 bps:

| orizzonte h | sigma_h | c / sigma_h | IC_min (quintile migliore) |
|---|---|---|---|
| 1 min | 0,066% | 11,7 | > 1 (impossibile) |
| 1 h | 0,51% | 1,5 | > 1 (impossibile) |
| 4 h | 1,0% | 0,75 | 0,54 |
| 1 g | 2,5% | 0,31 | 0,22 |
| 1 settimana | 6,6% | 0,12 | 0,083 |
| 30 g | 13,7% | 0,056 | 0,040 |

Un IC fuori campione realistico per predittori di rendimento su mercati liquidi e' 0,01-0,05 [S]. Conseguenze [D]: sotto il giorno nessun segnale "medio" paga i costi (coerente con il NO-GO di P1); servono orizzonti >= 1 settimana, oppure eventi estremi (|z| >= 3, dove E[z] e' grande ma i trade sono rari), oppure payoff asimmetrici (trend-following: poche vincite grandi, molte perdite piccole) che non passano per l'IC. SOL ha sigma circa doppia: IC_min dimezza, la conclusione non cambia.

Formula di potenza usata sotto (test a una coda, alpha Holm nel caso peggiore 0,05/4 = 0,0125, potenza 80%): n_min = ((2,24 + 0,84) x s / delta)^2 = 9,5 x (s / delta)^2, con s = deviazione standard del netto per unita' indipendente e delta = netto medio vero [D]. Per serie temporali: t atteso = SR x sqrt(anni); per potenza 80% serve SR >= 3,08 / sqrt(anni) [D].

## 2. Dati, split, famiglia di test (comuni)

- Braccio L (decide A e B): candele giornaliere Coinbase BTC-USD dal 2015-07-20, ETH-USD dal 2016-05-18, fino al 2026-10-25 [M, `data/external/prices/`]. Training: primo giorno con finestre complete .. 2020-12-31. Holdout: 2021-01-01 .. 2023-09-30, mai usato per regole di trend o rotazione (usato solo come holdout del pattern volume, ipotesi diversa) [M]. Replica EUR 2023-10-01 .. ultimo giorno completo (BTC/ETH/SOL-EUR da `data/bars/1m`): contaminata (tutta la storia EUR e' stata letta da P1 e `pattern_explore.py` [M]), puo' solo BLOCCARE un GO.
- Braccio U (decide C e D, solo con approvazione del download): candele Coinbase BTC-USD, ETH-USD granularita' `FIFTEEN_MINUTE` 2016-01-01 .. 2023-09-30 (~250 mila barre per moneta, ~830 richieste [D]); stessi split del braccio L. Senza download: C e D solo sul braccio E (EUR 1m, training 2023-10-01 .. 2025-08-31, holdout 2025-09-01 .. fine), esito massimo CANDIDATO, mai GO.
- Avanti (dry run): ogni book accumula un secondo holdout pulito dalla data di avvio; uno sguardo solo, alla data o al numero di trade fissati in sez. 5.
- Famiglia primaria m = 4 (una statistica per ipotesi, sez. 3). Holm sui p-value di TRAINING, alpha familiare 0,05 a una coda. Le soglie di Holm sono 0,0125 / 0,0167 / 0,025 / 0,05. Tutto il resto (varianti, costi alternativi, per moneta) e' esplorativo e non crea ne' riapre un verdetto.
- Bias dichiarati: sopravvivenza (BTC/ETH/SOL sono le monete che sono sopravvissute e cresciute, i test di trend sono ottimistici); conoscenza del percorso (tutti sanno del bull 2017/2020/2024 e del bear 2022); A e' prima in classifica anche perche' "si sa" che ha funzionato. Mitigazione: condizioni "senza l'anno o l'unita' migliore" (sez. 4).
- Procedura come negli studi precedenti: script congelato e committato prima della corsa, `00_preregistration.json` con sha256 di script e documento, il codice legge l'holdout solo dopo aver scritto e hashato i risultati di training, una corsa per braccio.

## 3. Le ipotesi (regole esatte, parametri fissati ora)

Notazione: C_t, H_t, L_t, O_t = chiusura, massimo, minimo, apertura della barra t (UTC). Le decisioni usano solo dati <= chiusura di t; l'esecuzione avviene alla barra t+1.

### A. Trend-following giornaliero, long o cassa (Donchian 50/20)

- Ipotesi economica: nelle crypto l'informazione si diffonde lentamente e i flussi retail e le liquidazioni dei derivati a leva rinforzano i movimenti di piu' giorni; chi paga e' chi compra tardi nei trend e chi vende forzato nei crolli. Il vantaggio per uno spot long-only e' soprattutto stare in cassa nei bear lunghi, non predire i giorni.
- Universo: BTC, ETH (braccio L); BTC, ETH, SOL (replica EUR e dry run). Capitale diviso in sleeve uguali 1/N, una per moneta, mai trasferite tra sleeve.
- Ingresso: a chiusura del giorno t, se sleeve in cassa e C_t > max(C_(t-50) .. C_(t-1)), acquisto alla barra t+1. Backtest: prezzo O_(t+1). Dry run/esecuzione: post-only al best bid per 60 minuti, poi taker sul residuo.
- Uscita: a chiusura del giorno t, se sleeve investita e C_t < min(C_(t-20) .. C_(t-1)), vendita alla barra t+1 (backtest O_(t+1)). Nessuno stop intraday, nessun take profit.
- Dimensione: 100% della sleeve (perdita massima = sleeve). Variante esplorativa: peso min(1, 0,50 / sigma60_annua) (mai > 1, nessuna leva).
- Costi: primario 110 bps a giro (prudente), secondario 77 bps.
- Frequenza attesa: 5-8 round trip/anno per moneta [S]; training ~50-70 trade, holdout ~25-35 [S].
- Edge minimo: per trade lordo >= 1,1%; per anno per sleeve lordo >= 6-9% (costo di 5-8 giri x 1,1%) [D].
- Statistica primaria: media dei rendimenti netti giornalieri del portafoglio A contro cassa (0). p = il piu' conservativo fra bootstrap a blocchi stazionario (blocco medio 20 g, 10.000 repliche) e t con errori HAC Newey-West (20 lag).
- Potenza: training ~5,2 anni (BTC) / ~4,5 (ETH) [D]; SR vero 1,0 -> t ~2,2, potenza ~50% alla soglia 0,0125; SR 1,35 -> 80%; SR 0,5 -> ~13% [D]. L'holdout (2,75 anni) serve per segno e drawdown, non per significativita' (servirebbe SR >= 1,86).
- Riportato, non decide: eccesso contro buy and hold delle stesse monete (stesse sleeve).

### B. Rotazione per momentum relativo con filtro di cassa (mensile)

- Ipotesi economica: il capitale crypto ruota a ondate (BTC prima, poi le alt) e i flussi seguono le performance recenti; chi paga e' chi resta sulla moneta in ritardo. Momentum cross-section in crypto e' documentato su universi ampi; su 2-3 monete molto correlate l'effetto atteso e' piccolo [S].
- Ribilanciamento: primo giorno UTC del mese. R28_c = ln(C_t / C_(t-28)) per ogni moneta, t = ultimo giorno del mese precedente.
- Regola: candidata = moneta con R28 massimo. Se R28_candidata <= 0: 100% cassa. Altrimenti 100% nella candidata; se si detiene gia' un'altra moneta con R28 > 0 si cambia solo se R28_candidata - R28_detenuta > 0,05 (isteresi fissa).
- Benchmark B0 (stessa cadenza, stesso filtro): ogni moneta pesa 1/N ed e' investita se il suo R28 > 0, altrimenti la quota e' in cassa.
- Costi: 110 bps per ogni giro (un cambio di moneta = un giro completo); secondario 77.
- Frequenza: 4-9 cambi/anno [S]; costo annuo 4-10% [D].
- Statistica primaria: media giornaliera di (netto B - netto B0) > 0 (isola la selezione dal semplice "investito o no"). p come A.
- Potenza: training 2016-08 .. 2020-12 ~4,4 anni [D]; per potenza 80% serve IR >= 1,47 [D]; IR plausibile 0,3-0,5 [S] -> potenza 10-15%. Esito atteso: INCONCLUSO. Si tiene perche' costa poco ed e' la forma "attiva" che l'utente chiede; la diluizione di Holm che introduce e' piccola (soglia di A da 0,0167 a 0,0125).

### C. Mean reversion dopo eccessi, ingresso maker con scadenza (triple barrier)

- Ipotesi economica: i crolli di poche ore sono spesso guidati da liquidazioni forzate sui derivati, che pagano un premio di immediatezza; chi compra con un limite sotto il prezzo fornisce liquidita' e incassa il rimbalzo. Rischio economico: i market maker professionali catturano questo premio a fee ~0, noi paghiamo 25 bps; i fill maker sono selezionati avversamente (si riempiono quando il prezzo continua a scendere).
- Barre: 1h costruite da 1m (EUR) o da 15m (USD). sigma1h(t) = deviazione standard (ddof 1) dei rendimenti log 1h nelle 720 ore che finiscono in t; sigma4h = 2 x sigma1h.
- Evento: a chiusura dell'ora t, ln(C_t / C_(t-4h)) <= -3 x sigma4h(t). Al massimo un evento per moneta ogni 24 h; ignorato se il book ha gia' una posizione aperta.
- Ingresso: buy post-only a L = C_t x (1 - sigma1h(t)), valido 4 h. Simulazione: riempito solo se in una barra successiva il minimo e' < L di almeno un tick (trade-through); prezzo L. Non riempito = nessun trade (tasso di fill riportato).
- Uscite (prima che si verifica): take profit maker a L x (1 + 2 x sigma4h) (riempito solo se il massimo lo supera di un tick); stop a L x (1 - 3 x sigma4h), eseguito taker a min(stop, apertura della barra) x (1 - 5 bps); tempo: 24 h dopo il fill, taker all'apertura successiva x (1 - 5 bps). Se nella stessa barra si toccano stop e target vale lo stop.
- Dimensione: 100% del capitale del book, una posizione alla volta.
- Costi: 50 bps (maker + maker) se esce a target; 80 bps (maker + taker + slippage) se stop o tempo [D].
- Frequenza: 1-3 eventi per moneta al mese, fill ~50-70% [S]; ~20-60 trade/anno su 3 monete [S].
- Edge minimo: lordo per trade >= 0,6-0,8% (dipende dal mix di uscite) [D]. Senza reversione (martingala) il lordo atteso e' 0 e il netto e' -0,6/-0,8% [D].
- Statistica primaria: netto medio per unita' > 0; unita' = gruppo di trade di monete diverse con ingressi entro 24 h (media del gruppo). p = piu' conservativo fra t e bootstrap a cluster.
- Potenza: s ~1,8% per unita' [S, limitato da target/stop]; delta 0,4% -> n_min ~190; delta 0,25% -> ~490 [D]. Braccio U (2 monete, 2016-2020): ~150-300 unita' in training [S]: rileva solo un netto >= ~0,4%. Braccio E: ~60-180 trade in 3 anni [S], insufficiente e contaminato.

### D. Compressione di volatilita' seguita da breakout (barre 4h)

- Ipotesi economica: la volatilita' si raggruppa (fatto robusto) ma predice l'ampiezza, non la direzione; per un long-only il vantaggio esiste solo se i breakout al rialzo dopo compressione proseguono (stessa fonte di A, a frequenza piu' alta). La compressione serve ad avere movimenti grandi rispetto al costo fisso.
- Barre 4h UTC (00, 04, ..., 20). Q(t) = sd dei rendimenti log 4h sulle ultime 30 barre / sd sulle ultime 180 barre. Soglia q20 = 20o percentile di Q sulle barre di TRAINING della moneta (`numpy.quantile`, metodo linear); la stessa soglia vale in holdout e replica.
- Ingresso: Q(t-1) <= q20 e C_t > max(H_(t-30) .. H_(t-1)); acquisto taker a O_(t+1) x (1 + 5 bps). Sleeve 1/N per moneta, una posizione per sleeve.
- Uscita: C_t < min(L_(t-15) .. L_(t-1)) oppure 60 barre (10 giorni) dall'ingresso; vendita taker a O_(t+1) x (1 - 5 bps).
- Costi: 110 bps a giro [D] (il breakout non si compra in modo affidabile come maker: i fill maker sarebbero solo i breakout falliti).
- Frequenza: 1-2 trade per moneta al mese [S].
- Edge minimo: lordo per trade >= 1,1% [D]; per anno per sleeve >= 13-26% (12-24 giri) [D].
- Statistica primaria: netto medio per unita' > 0 (unita' = trade di monete diverse con ingressi entro 24 h). p come C.
- Potenza: s ~4% [S]; delta 1% -> n_min ~150; delta 0,5% -> ~610 [D]. Braccio U training ~150-200 trade [S].
- Esplorativa: stessa regola senza il filtro Q (dice se la compressione aggiunge qualcosa al semplice breakout).

## 4. Regole di decisione (scritte ora, uguali per tutte salvo dove indicato)

Minimi di unita' (sotto: INCONCLUSO prima di guardare il segno): A e B >= 30 trade in training e >= 15 in holdout; C e D >= 150 unita' in training e >= 60 in holdout.

- GO (autorizza solo il passaggio al dry run come book "candidato al reale", non capitale): (a) p_Holm di training < 0,05; (b) holdout: netto medio > 0 e almeno 1/3 della stima di training; (c) training senza l'anno solare migliore (A, B) o senza l'unita' migliore (C, D): netto medio > 0; (d) replica EUR: non blocca (blocca se netto medio < 0 con estremo superiore dell'IC 90% < 0); (e) solo A: nell'holdout drawdown massimo di A <= 0,6 x drawdown massimo del buy and hold delle stesse monete (verifica che il valore della regola sia uscire dai bear); (f) solo C: tasso di fill dell'holdout entro 15 punti da quello di training.
- NO-GO: netto medio di training <= 0, oppure (a) vera e holdout con netto medio < 0 con n sopra il minimo (non replica). Chiude l'ipotesi su questi dati: nessuna variante di parametri sugli stessi dati.
- INCONCLUSO: n sotto il minimo, oppure training positivo ma (a) falsa, oppure holdout positivo ma sotto 1/3. Nessun capitale; resta solo il book forward con uno sguardo pre-fissato.
- CANDIDATO: esito che sarebbe GO ma su dato contaminato (braccio E). Equivale a INCONCLUSO per il capitale.
- Dal GO al capitale reale (secondo gate, avanti): book in dry run per almeno 6 mesi e almeno 10 trade (A, B) o 40 (C, D), costo medio realizzato in simulazione <= costo assunto + 10 bps, tasso di fill entro 15 punti dal backtest, netto forward >= 0, gate G-DRY-ECON esistente superato senza modifiche. Poi capitale reale solo alla taglia minima dell'exchange.

## 5. Costo di scoprire e uso dei book paralleli

| Ipotesi | Test subito offline | Serve | Esito massimo oggi |
|---|---|---|---|
| A | si', braccio L (dato in repo) | uno script | GO possibile |
| B | si', braccio L | stesso script di A | GO possibile, atteso INCONCLUSO |
| C | solo braccio E | download 15m USD per GO | CANDIDATO senza download |
| D | solo braccio E | download 15m USD per GO | CANDIDATO senza download |

Book da aggiungere al dry run (ciascuno con capitale virtuale proprio, regole identiche alla sez. 3, nessun ordine reale):

| Book | Scopo | Trade attesi/mese [S] | Tempo per il minimo forward [S] |
|---|---|---|---|
| A-trend (BTC/ETH/SOL) | ipotesi A | 1-2 | 10 trade: 6-10 mesi |
| B-rotazione | ipotesi B | 0,3-0,8 | 10 cambi: 12-30 mesi |
| C-revert | ipotesi C | 2-5 | 40 trade: 8-20 mesi |
| C0-controllo | stesse regole di ingresso/uscita di C ma piazzate a ora fissa (12:00 UTC) ogni giorno, senza evento: misura la selezione avversa dei fill maker; in forward la statistica di C e' C meno C0 | 40-70 | 2-3 mesi |
| D-squeeze | ipotesi D | 3-6 | 40 trade: 7-14 mesi |
| HODL | buy and hold 1/3 per moneta dal giorno di avvio: riferimento | 0 | - |

Il dry run NON produce evidenza statistica sufficiente per A, B, D entro un anno (pochi trade); serve a misurare lo scarto di implementazione (fill, slippage, costi realizzati contro backtest). Solo C0 accumula abbastanza fill da stimare rapidamente la selezione avversa, che decide di fatto se C ha senso. Sguardo unico forward: 2027-10-28 oppure al raggiungimento del minimo della tabella, il primo dei due.

## 6. Scartate, con il conto

- Effetti ora/giorno: la stagionalita' intraday e' robusta su volume e volatilita', instabile sui rendimenti; effetti medi per ora dell'ordine di pochi bps [S]. Anche tenendo una finestra "buona" di 8 h ogni giorno l'edge atteso e' <= 10-20 bps contro 77 di costo [S/D]; e per stimare 10 bps con s ~150 bps servirebbero ~2.100 giorni per moneta [D]. Netto atteso negativo anche se l'effetto esiste. Ammesso solo come filtro di esecuzione, non come strategia.
- Maker paziente incondizionato (limite sotto il prezzo a qualsiasi ora): in assenza di reversione il prezzo e' una martingala e il netto atteso e' -fee - selezione avversa [D]; il fill avviene di preferenza quando il prezzo continua a scendere. Non ha una controparte che paga. Entra come book di controllo C0.
- Scalping, grid trading, qualunque orizzonte < 4 h senza evento estremo: IC_min > 0,5 (sez. 1). Il grid su una martingala ha valore atteso = -fee per giro.
- Rientri su P1 (LightGBM su indicatori), news, pattern volume: chiusi o in attesa del loro riesame pre-programmato; non si riaprono qui.
- Fuori famiglia, solo come idea futura: segnali di posizionamento dei derivati (funding dei perpetui, open interest) usati come filtro per un acquisto spot (chi paga: la leva altrui). Economicamente sensato, ma nessun dato storico in repo; richiede fonte e licenza approvate. Non testabile ora.

## 7. Probabilita' a priori e piano se nulla passa

Probabilita' a priori di GO (offline) [S]: A 25%; B 7%; C 8% (con download), ~0% senza; D 7%. Correlazione positiva fra A e D. Almeno un GO: 30-40% [S]. GO che poi supera anche il gate forward: 15-25% [S]. GO che nei 12 mesi successivi fa crescere il portafoglio piu' del buy and hold: ~10-15% [S] (il valore tipico di A e' ridurre i drawdown, non battere il buy and hold nei bull).

Se nessuna passa:
- Ridurre le fee con il volume NON e' realistico. Ipotesi da verificare leggendo la tabella reale dei tier (endpoint Coinbase `transaction_summary`): primo scalino utile a ~10.000 USD (~9.200 EUR) di volume a 30 giorni [S]. Con 28 EUR un giro vale 56 EUR di volume: servono ~164 giri al mese (~5,5 al giorno), che costano 164 x 0,77% x 28 = ~35 EUR al mese, cioe' ~126% del capitale ogni mese [D]. Anche tutto maker (50 bps): ~23 EUR/mese, 82% del capitale [D]. Il tier dipende dal capitale, non dalla strategia: con il ritmo naturale di C (~4 giri/mese) servirebbero ~1.150 EUR di capitale, con A (~1,2 x capitale di volume al mese) ~7.900 EUR [D/S].
- Aumentare il capitale non crea edge; rende solo i risultati misurabili in euro. Cambiare exchange per fee base piu' basse (10-40 bps [S]) richiede una nuova integrazione completa e non crea edge: ha senso solo dopo un GO limitato dai costi (es. C con netto vicino a zero).
- Opzione onesta: tenere il bot in solo dry run con i book forward (costo ~0), nessun capitale reale su regole sistematiche finche' un book non supera entrambi i gate. Il riferimento contro cui misurare tutto resta il book HODL.

## 8. Rischio (vale per qualunque esito)

Tutte le regole sono spot long-only, una posizione per sleeve/book, size <= capitale allocato, nessun ordine senza copertura del saldo in cache; la perdita massima di ogni trade e' il capitale allocato a quel trade. Nessuna regola richiede leva, prestito o vendita allo scoperto. Gli stop di C sono taker sul saldo in moneta gia' posseduto (mai vendite oltre il saldo).

## 9. Decisioni richieste all'utente

1. Approvare (o ridurre) la famiglia A, B, C, D con parametri e regole come scritti; dopo l'approvazione si congela e non si tocca.
2. Approvare il download delle candele Coinbase 15m BTC-USD ed ETH-USD 2016-2023 (termini d'uso non verificabili in automatico, stesso caso del pattern volume). Senza: C e D si fermano a CANDIDATO.
3. Approvare l'aggiunta dei book A-trend, B-rotazione, C-revert, C0-controllo, D-squeeze, HODL al dry run (nessun ordine reale).
4. Prendere atto che un eventuale GO significa pochi trade (A: ~1-2 al mese), non trading continuo, e che con ~28 EUR l'esito in euro e' trascurabile in ogni caso.
5. Decidere ora cosa fare se nulla passa: restare in dry run (raccomandato), oppure aumentare il capitale, oppure valutare un altro exchange; nessuna delle tre crea un vantaggio da sola.

## 10. ERRATA CORRIGE (2026-10-28, trader-quant)

Origine: incoerenze I1-I7 rilevate dall'architetto in `docs/decisions/2026-10-28-book-laboratorio-spec.md` sez. 11. Le sez. 0-9 restano con il testo approvato; questa sezione le corregge dove indicato.

Perimetro e tracciabilita':
- Scritta DOPO la corsa del braccio L (esito in `docs/decisions/2026-10-28-ipotesi-strategie-esito-AB.md`: A e B INCONCLUSO) e PRIMA di qualunque calcolo su C e D e dell'avvio di qualunque book forward.
- Nessuna regola, parametro o costo usato da `script/study_trend_rotation.py` (`PREREG`) cambia; i verdetti di A e B restano. Il `document_sha256` registrato nel `00_preregistration.json` del braccio L (`ebe6ab86...`) e' quello della versione del commit 36b494d, senza questa sezione: per la verifica `git show 36b494d:docs/decisions/2026-10-28-ipotesi-strategie-laboratorio.md`. Lo script di C e D hashera' la versione con questa sezione.
- Cio' che qui tocca A e B riguarda SOLO la parte forward (book, esecuzione dry run, secondo gate). E' una nuova pre-registrazione della parte forward di A e B, legittima perche' non esiste ancora alcun dato forward; nessuna nuova corsa offline di A o B e' richiesta o ammessa.
- Richiede la presa d'atto dell'utente insieme all'approvazione dei book (sez. 9 punto 3), prima dell'abilitazione.

### E1 (I1) C0-controllo: universo, posizioni, frequenza

- Universo BTC-EUR, ETH-EUR, SOL-EUR; tre sleeve 1/3 mai trasferite, una posizione per sleeve, 100% della cassa della sleeve per occasione (stessa struttura di A e D, NON quella di C).
- Occasione: alla chiusura della barra 1h che chiude alle 12:00 UTC, per ogni moneta la cui sleeve e' flat e senza ingresso pendente. Nessuna condizione di evento, nessuna deduplica. sigma1h, L, TP, SL, validita' 4 h, uscita a tempo, prezzi di fill e costi identici a C (con E4 ed E6).
- Motivo: la statistica forward e' C - C0 sul rendimento netto percentuale per trade, indipendente dalla taglia; a C0 servono molti fill, non la struttura di capitale di C. "Stesse regole di C" (sez. 5) vale per la meccanica del trade, non per il vincolo di posizione.
- Frequenza corretta: probabilita' di fill entro 4 h di un limite a -1 sigma1h, moto browniano: f = 2 Phi(-1 / sqrt(4)) = 2 Phi(-0,5) = 0,62 [D]; con trade-through di un tick e barre discrete 0,50-0,60 [S]. Probabilita' che la posizione sia ancora aperta all'occasione successiva (corridoio +4 / -6 sigma1h in ~20-24 h, sigma ~4,7 sigma1h): q ~0,45 [D]. Fill per moneta al giorno = f / (1 + f q) = 0,41-0,47 -> 35-45 trade/mese su 3 monete [D/S] (non 40-70).
- Statistica forward (allo sguardo di C): Delta = netto medio per unita' di C - netto medio per unita' di C0; unita' di C come sez. 3; unita' di C0 = trade della stessa occasione giornaliera (cluster per giorno). p = il piu' conservativo fra Welch a una coda sulle unita' e bootstrap a cluster (10.000 repliche). Riportato; decide solo il segno (E2, F2).
- Sguardo anticipato di C0 solo descrittivo (tasso di fill; lordo medio per trade = misura della selezione avversa) a 120 trade chiusi di C0 o al 2027-10-28, il primo dei due. Puo' solo motivare l'arresto di C per futilita' (scelta dell'utente, conservativa: riduce soltanto la probabilita' di un GO); non puo' anticipare un GO, ne' cambiare parametri, soglie o data dello sguardo di C.
- Lo script di C e D calcola anche la regola C0 come ESPLORATIVA (non entra in Holm), per avere un tasso di fill di riferimento con cui confrontare il forward.

### E2 (I2) Secondo gate (sostituisce l'ultimo punto della sez. 4)

- Si applica solo a un'ipotesi con GO offline. Per INCONCLUSO o CANDIDATO (oggi A e B) lo sguardo forward e' descrittivo e non autorizza capitale; un riesame richiede una nuova pre-registrazione (scelta dell'utente).
- Sguardo unico al primo fra 2027-10-28 e il minimo F1, sui dati dal `lab_started_at` del book.
- Criteri, tutti veri:
  - F1 durata >= 6 mesi e trade chiusi >= minimo: A 10, B 10 (periodi di detenzione chiusi, definizione di "trade" dello script del braccio L), C 40, D 40. Sotto: INCONCLUSO forward.
  - F2 segno: A media giornaliera netta del book >= 0; B media giornaliera (B - B0) >= 0; C netto medio per unita' >= 0 E Delta (E1) > 0; D netto medio per unita' >= 0.
  - F3 costo: costo realizzato medio per giro <= costo assunto (E4) + 10 bps. Costo realizzato di un giro = fee_buy / nozionale_buy + fee_sell / nozionale_sell + (P_buy / P_ref_buy - 1) + (1 - P_sell / P_ref_sell), con P_ref = prezzo del backtest per quella gamba: O_(t+1) (apertura della prima barra 1m dopo la chiusura della barra di decisione) per gli ordini a mercato di A, B, D e per l'uscita a tempo di C; L per l'ingresso maker; tp per il target; il livello di stop per lo stop. Per C il confronto e' per tipo di uscita.
  - F4 fill (solo C): tasso di fill maker forward entro 15 punti da quello di training di C.
  - F5 rischio (solo A): drawdown massimo di A <= drawdown massimo del book HODL nella stessa finestra. Il fattore 0,6 della condizione (e) resta solo offline: in 12 mesi senza un bear il rapporto non e' stimabile e 0,6 diventerebbe un test sul rumore. B, C, D: drawdown riportato, non decide.
  - F6 operativo: nessuna `SpotViolation` o `DryRunViolation`, nessun rifiuto del `BalanceGuard` su una taglia del driver, nessun prodotto rimasto `degraded`.
- G-DRY-ECON: calcolato invariato e riportato per ogni book di laboratorio, NON decisionale per essi. Motivi: `beats_random_entry` ed `edge_ece` non sono misurabili per un book a regole (nessun gemello random, nessun `p_edge`); `max_drawdown <= 5%` e' strutturalmente irraggiungibile per una strategia investita in crypto; `min_trades` 30 e' sostituito da F1. Il confronto con un riferimento e' gia' in F2 (B0 per B, C0 per C) e in F5 (HODL per A). Battere HODL in rendimento NON e' un criterio: su 12 mesi la deviazione standard della differenza dei rendimenti annui e' dell'ordine di 40-60 punti [S], nessuna potenza.
- I p-value forward (stesse funzioni degli script offline) sono riportati, non decidono: con 10-40 trade la potenza e' trascurabile; F2 e' un controllo di segno, F3-F6 di implementazione.

### E3 (I3) B nel dry run e benchmark B0

- Universo forward di B: BTC-EUR, ETH-EUR, SOL-EUR (come la replica EUR dello script del braccio L).
- Esecuzione: taker in ingresso e in uscita (coerente con 110 bps), alla prima barra 1m dopo la chiusura dell'ultimo giorno UTC del mese; un cambio di moneta = vendita completa, poi acquisto. Parte in cassa fino alla prima fine mese.
- B0 eseguito come settimo book `lab_b0_benchmark`: tre sleeve 1/3 mai trasferite e mai ribilanciate (convenzione `combine_sleeves` dello script congelato), ciascuna al 100% nella sua moneta se il suo R28 a fine mese > 0, altrimenti in cassa; stessa cadenza, stessa esecuzione taker, stesso giorno di avvio di B. B - B0 si calcola sulle equity giornaliere (00:00 UTC) dei due book.

### E4 (I4) Modello di fill e costi assunti per C, C0, D

- Ogni prezzo di fill dei backtest di C, C0 (esplorativo) e D usa i kernel di `modules/dryrun/fill_model.py` con `FillModelParams` di default (slippage 5 bps, extra stop 10 bps), al posto dei prezzi della sez. 3:
  - C, C0: ingresso maker a L se `maker_entry_filled(low, L)`; target a tp se `tp_touched(high, tp)`; stop a `sl_exit_px(SL, open, s, 5, 10)` = min(SL, O) x (1 - (s/2 + 15) bps); tempo a `market_exit_px(O, s, 5)` = O x (1 - (s/2 + 5) bps).
  - D: ingresso `taker_entry_px(O_(t+1), s, 5)` = O_(t+1) x (1 + (s/2 + 5) bps); uscita `market_exit_px(O_(t+1), s, 5)`.
  - s = spread in bps per prodotto: lo stesso valore per prodotto usato dal gateway shadow del dry run, congelato nel `00_preregistration.json` dello script C/D; per il braccio U si usa lo stesso valore EUR (prudente: gli spread USD sono piu' stretti [S]).
  - Nessun tetto di quantita' (queue haircut) nei backtest: taglie 33-100 EUR; i fill parziali si misurano solo in forward.
- Costi assunti per giro (fee Intro 25 / 50 bps), usati in F3: C e C0 target 50 bps; tempo 80 + s/2; stop 90 + s/2 (rispetto al livello di stop o all'apertura in gap); D 110 + s; A e B 110 (invariati: il braccio L e' gia' corso).
- A e B: lo script congelato addebita 55 bps per lato senza spread, equivalente ai kernel con s ~0 sugli USD [S]; nessuna nuova corsa. In forward EUR il costo realizzato include s/2 per lato: e' lo scarto che F3 misura (per A la fase maker puo' solo ridurlo).

### E5 (I5) Soglia q20 di D

- q20 per moneta = `squeeze_threshold` sulle barre 4h di TRAINING del braccio che decide D: BTC ed ETH dal braccio U (training 2016-01-01 .. 2020-12-31, 15m USD aggregate a 4h); SOL dal braccio E (training 2023-10-01 .. 2025-08-31, 1m EUR), perche' il braccio U non ha SOL. Senza braccio U: braccio E per tutte e tre.
- Il valore numerico e' scritto nel file dei risultati di training dello script C/D (prima della lettura dell'holdout: e' un'uscita del training, non rivela esiti) e copiato congelato nei parametri del book D. Il book D non parte finche' i valori non esistono.
- Q e' un rapporto di deviazioni standard: non dipende da valuta ne' livello di prezzo, quindi una soglia stimata su USD vale su barre EUR [D] (volatilita' EUR/USD ~0,3-0,5% al giorno contro 2,5-5% delle crypto [S]: effetto trascurabile).
- Q(t) richiede 181 barre 4h complete (180 rendimenti); con un buco nella finestra nessun segnale.

### E6 (I6) Barre esatte e deduplica

- C, evento: alla chiusura dell'ora t, condizione vera e nessun evento RILEVATO della stessa moneta con chiusura in (t - 24 h, t). Ogni evento rilevato apre la finestra di 24 h, operato o no; la definizione non dipende dallo stato del book (funzione pura dei prezzi).
- C, operativita': evento ignorato (registrato) se il book ha una posizione aperta O un ingresso pendente. Piu' eventi nella stessa ora con book libero: si opera quello con z piu' negativo, z = ln(C_t / C_(t-4h)) / sigma4h(t); a parita', ordine dell'universo BTC, ETH, SOL.
- C, finestre: sigma1h(t) dai 720 rendimenti log 1h r_(t-719) .. r_t, ddof 1, tutte le barre presenti (altrimenti nessun evento); C_(t-4h) = chiusura della barra t-4; ingresso valido sulle barre 1h t+1 .. t+4.
- C e C0, uscite: f = barra 1h del fill. Stop attivo da f inclusa (regola "fill bar" di `fill_model.py`), target da f+1. Se nessuna barriera entro la chiusura di f+24: vendita all'apertura di f+25 (tenuta fra 24 e 25 h). Il dry run su barre 1m e' uguale o piu' favorevole solo sul target dentro f (il backtest e' il lato prudente).
- D: ingresso deciso alla chiusura di t, eseguito a O_(t+1); uscita di canale valutata alle chiusure t+1 .. t+60; se nessuna, uscita decisa alla chiusura di t+60 ed eseguita a O_(t+61) (60 barre = 10 giorni dall'ingresso). Una sola azione per barra e per sleeve: dopo un'uscita decisa alla chiusura di s, un nuovo ingresso si valuta dalla chiusura di s+1.
- A: schema identico a quello gia' implementato nello script del braccio L; nessun cambio.

### E7 (I7) Sguardo unico e rapporto giornaliero

- Ammessi ogni giorno: equity e PnL per book (sezione esistente del report), conteggi di segnali, ordini, fill, blocchi, ritardi, tasso di fill, costo realizzato, slippage contro P_ref. Servono a rilevare errori di implementazione.
- Solo allo sguardo: statistiche primarie, confronto con HODL (rendimento e drawdown), B - B0, C - C0, verdetto F1-F6.
- Regola: nessuna azione sui book (parametri, varianti, aggiunta o rimozione, capitale) puo' dipendere dalla performance interim. Ammessi solo l'arresto per errore operativo o per futilita' (conservativi: riducono soltanto la probabilita' di un GO). Una correzione di esecuzione che cambia i fill si applica solo a un book con un nome nuovo e una nuova data di avvio.

### E8 Altre precisazioni del forward

- A, fill maker parziale nella fase di 60 minuti: il residuo resta in cassa fino all'uscita (il sistema non consente un secondo ingresso sulla stessa posizione); nessun fill maker: taker sull'intera sleeve, come in sez. 3. La frazione di sleeve non investita e' registrata come scarto di implementazione.
- Guardie di conto del `RiskManager` (perdita giornaliera, serie di stop, esposizione <= 50%): NON si applicano ai book di laboratorio (regole identiche alla sez. 3); `BalanceGuard` e copertura 100% sempre. Prima di qualunque capitale reale su A o D (che investono fino al 100% del capitale) l'utente dovra' decidere come conciliare la regola con il limite di esposizione 50%: eseguirla al 50% sarebbe un'altra strategia, non testata.
- Capitale simulato: 100 EUR per book (confrontabilita'; a queste taglie gli arrotondamenti a `base_increment` valgono < 0,01% [D]).

### E9 Frequenze forward aggiornate (tabella della sez. 5)

| Book | Trade attesi/mese | Minimo forward |
|---|---|---|
| A-trend | ~0,6 (2,5 per moneta all'anno osservati nel training L [M], x 3 monete) | 10 trade: ~16 mesi, quindi lo sguardo cadra' il 2027-10-28 probabilmente con n < 10 |
| B-rotazione | ~0,4 (23 trade in ~4,4 anni nel training L [M]) | 10 trade: ~2 anni, sguardo al 2027-10-28 |
| B0-benchmark | - | segue B |
| C-revert | 2-5 [S] | 40 trade |
| C0-controllo | 35-45 [D/S] | sguardo descrittivo a 120 trade |
| D-squeeze | 3-6 [S] | 40 trade |
| HODL | 0 | - |

### E10 Parametri che cambiano

- C: prezzo di stop da min(stop, O) x (1 - 5 bps) a min(stop, O) x (1 - (s/2 + 15) bps); uscita a tempo da O x (1 - 5 bps) a O x (1 - (s/2 + 5) bps); costi assunti da 80 a 80 + s/2 (tempo) e 90 + s/2 (stop). Motivo: un solo modello di fill (spec 5.3 punto 4); piu' prudente.
- D: prezzi di ingresso e uscita da 5 a s/2 + 5 bps per lato; costo assunto da 110 a 110 + s. Stesso motivo.
- C0: tre sleeve 1/3 per moneta invece di un book a posizione unica; frequenza da 40-70 a 35-45 al mese.
- Secondo gate: F1-F6 al posto di "G-DRY-ECON superato senza modifiche"; nuovo F5 per A (drawdown <= HODL in forward).
- Forward di B: esecuzione taker e settimo book B0.
- Invariati: tutte le finestre, soglie e livelli di segnale (50/20, R28 e 0,05, 720 h, 3 sigma4h, L, 2 e 3 sigma4h, 4 h, 24 h, 30/180, q 20%, 30/15, 60 barre), i minimi offline, le regole GO/NO-GO della sez. 4 (salvo l'ultimo punto), tutto il braccio L.

## 11. ERRATA CORRIGE E11 (2026-10-28, trader-quant): potenza di C e D e verdetto a priori

Perimetro e tracciabilita':
- Scritta DOPO il solo conteggio dei SEGNALI di C e D nel training del braccio U (`script/count_signals_cd.py`, commit dbd7b01: nessun rendimento, nessuna uscita simulata, holdout non letto; dettagli in `.claude/CONFLITTO_CD.md`) e PRIMA di qualunque calcolo di esito di C e D.
- Dove questa sezione contraddice le sez. 3-5 o la sez. 10 per C e D, vale questa sezione. Non tocca A, B, i parametri di segnale, i minimi e le regole GO/NO-GO della sez. 4, ne' E1-E10 salvo dove indicato.
- Richiede la presa d'atto dell'utente.

### E11.1 Conteggi (training braccio U)

- C: 147 unita' di segnale [M]; unita' di trade stimate 75-100 (fill 50-70%, posizione unica) [S]. Holdout stimato: 81 segnali, 40-56 trade [S].
- D: 73-125 unita' (limiti inferiore e superiore delle convenzioni di conteggio) [M]; holdout stimato 40-69 [S].

### E11.2 I minimi erano stimati in difetto

- C: con s 1,8% e delta 0,4% (sez. 3.C) la formula della sez. 1 da' n_min = 9,5 x (1,8 / 0,4)^2 = 192 [D]: il minimo 150 era l'estremo basso della frequenza attesa, non n_min. Inoltre s 1,8% riflette la volatilita' EUR 2023-2026; nel 2016-2020 sigma1h e' ~1,5-2 volte maggiore [S] e il payoff a barriere (+4 / -6 sigma1h, tempo 24 h) implica s ~3,5-5 sigma1h [D], cioe' s ~2,5-4% [S]. Con s 3%: n_min ~530 [D].
- D: n_min 152 con s 4% e delta 1% [D], coerente; ma delta 1% netto = 2,1% lordo per trade (ottimistico) e per tenute di 3-10 giorni nel 2016-2020 s ~6-8% [S]. Con s 7%: n_min ~470 [D].
- Conclusione: nessun argomento statistico, indipendente dall'esito, per abbassare i minimi; il calcolo indica che andavano alzati. I minimi della sez. 4 restano invariati.

### E11.3 Potenza effettiva [D]

Una coda, alpha 0,0125, potenza = Phi(delta / s x sqrt(n) - 2,24).

| Ipotesi | delta | s | n basso | n centrale | n alto |
|---|---|---|---|---|---|
| C | 0,4% | 1,8% | 0,38 (75) | 0,49 (100) | 0,67 (147, solo segnali) |
| C | 0,4% | 3% [S] | 0,14 | 0,18 | 0,27 |
| D | 1% | 4% | 0,46 (73) | 0,60 (100) | 0,71 (125) |
| D | 1% | 7% [S] | 0,15 | 0,21 | 0,26 |

Con effetti pari alla meta' di quelli della sez. 3 la potenza scende al 5-20% [D].

### E11.4 Decisione

1. C e D: INCONCLUSO per n sotto il minimo in training (sez. 4 come scritta), dichiarato ora, prima di qualunque calcolo di esito. Nessun GO ne' CANDIDATO offline per C e D.
2. Nessun rendimento di C o D si calcola, ne' in training ne' in holdout, neppure come esplorativo: la simulazione di esito di `script/study_cd.py` (sez. 10) non si esegue.
3. Holdout U 2021-01-01 .. 2023-09-30 per C e D: non letto e riservato. Utilizzabile solo da una futura pre-registrazione confermativa scritta prima della lettura, con parametri di segnale invariati e n_min ricalcolato con s misurata in cieco (punto 4).
4. Facoltativa, su decisione dell'utente: una sola corsa in cieco sul training che scrive esclusivamente s (deviazione standard del netto per unita', ddof 1), numero di unita' di trade, tasso di fill di C e di C0. Vietato calcolare o scrivere media, mediana, somma, segno, t, p, quote di uscita a target/stop/tempo, hit rate, equity, PnL per anno. Uso unico: n_min = 9,5 x (s / delta)^2 con delta della sez. 3; se n_min supera le unita' ottenibili su holdout U + forward, C e D si chiudono per futilita'.
5. Allargamento del campione, valutato e non adottato: altre monete USD 2016-2020 (LTC-USD, BCH-USD, ETC-USD [S]) aggiungono ~10-30 unita' di trade [S] per la correlazione dei crolli fra monete (198 eventi BTC + ETH = 147 unita' [M]), potenza C 0,43-0,62 con s 1,8% e 0,16-0,24 con s 3% [D]; barre 5m non aggiungono eventi (regole su 1h/4h, deduplica 24 h) e cambierebbero l'ipotesi; storia piu' lunga richiede altri exchange (venue, licenza); unire l'holdout al training da' per C 0,56-0,70 con s 1,8% e 0,21-0,30 con s 3% [D] e brucia l'unico holdout pulito.
6. Forward: book C, C0, D come E1-E9; con verdetto INCONCLUSO lo sguardo e' descrittivo (E2) e non autorizza capitale. q20 di D (uscita di training, non esito) copiati congelati nel book D (E5): BTC-USD 0,5966; ETH-USD 0,6162; SOL-EUR 0,7192 [M].
7. Con A, B, C e D INCONCLUSO la famiglia primaria non produce alcun GO offline; nessun book di laboratorio puo' arrivare al capitale reale con le regole attuali.
