# PROPOSTA: ipotesi di strategia per il laboratorio (backtest + dry run) prima di rischiare capitale

Autore: trader-quant. Data: 2026-10-28. Tipo: proposta di pre-registrazione, nessuna modifica a ordini, saldi, `RiskManager`, `BalanceGuard`. Stato: APPROVATA dall'utente il 2026-10-28 (famiglia A-D e metodo, sez. 9); le sezioni 2-6 sono congelate da questo commit, PRIMA di qualunque calcolo. Legenda: [M] misurato o letto nei documenti del repo, [D] dedotto da formula, [S] stima da verificare.

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
