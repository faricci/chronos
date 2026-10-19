# DECISIONE P1c (archiviata dal conflitto, risolto e implementato il 2026-10-18) - tuning fallito: veto di regime, EV gate; e proposta dell'utente (piu' strumenti volatili)

Autore: Sviluppatore (Sonnet). Data: 2026-10-18. Stato: RISOLTO e implementato (spec 2.10). Prima risponde `trader-quant` (sezione "Risposta trader-quant"), poi `architetto-opus` (sezione "Risposta architetto"). Nessun codice va scritto finche' non c'e' la risoluzione dell'architetto.

## Contesto e fatti misurati (non ipotesi)

Baseline LightGBM (`models/baseline/baseline-lgbm@2026-10-18.1`), tuning delle soglie sulle finestre di validation di ogni fold (`python -m training.tune_policy`, griglia da 20 a 80 combinazioni per fold, ogni combinazione e' un backtest completo con RiskManager, EV gate, kill switch, fee reali maker 25 / taker 50 bps). Esito (`models/baseline/.../tune_report.json`, `tune_table.csv`):

| Periodo | Esito |
|---------|-------|
| fold_0, fold_1, fold_2 | Nessuna combinazione ha PnL netto > 0. Miglior PnL con >= 30 trade: -0,31 / -2,78 / -0,48 EUR su 100 EUR di capitale |
| fold_3, fold_4 | ZERO trade in tutte le 80 combinazioni |
| final | ZERO trade in tutte le 80 combinazioni |

Cause misurate (`data/reports/diag_*.log`):

1. fold_3 e fold_4: la testa `vol_regime` e' quasi uniforme (probabilita' medie per classe 0,19-0,21). L'argmax cade sempre su una classe estrema (fold_3: sempre 4; fold_4: 0 o 4). La policy ammette l'ingresso solo nei regimi (1,2,3) e il `RiskManager` rifiuta per contratto i regimi 0 e 4 (`PolicyContractError`). Risultato: 0 candidati BUY. In fold_0..2 la testa e' "degenere" (una sola classe, regime 2) e non blocca.
2. final: i regimi non bloccano tutto (circa 2.860 candidati BUY dalla policy sulla combinazione piu' permissiva), ma il `RiskManager` li scarta TUTTI con `ev_negative` (2.861 su 2.861). Il `p_edge` calibrato del modello finale ha mediana circa 0,22 sulla finestra di validation finale (fine 2025 - giugno 2026), mentre `p_breakeven` e' circa 0,40 (costi/barriere 3 / 1,5 ATR 6h con fee reali). Il modello dice: "in questa finestra il take-profit viene toccato per primo nel 22% dei casi", quindi il valore atteso e' negativo dopo i costi. La griglia ancora le soglie sui quantili delle risposte e non su `p_breakeven`: molte combinazioni non possono mai passare l'EV gate.
3. Sui fold con trade (0..2) i trade ci sono, il segnale e' debole: AUC edge per fold 0,43 / 0,57 / 0,59 / 0,61 / 0,50, holdout 0,553; il logloss dell'edge non batte il prior del train in nessun fold (G2 fallirebbe 0/5). Il tasso base di UPPER cambia tra regimi di mercato (chi2 di omogeneita' fra semestri p = 0,004; semestre 2023H2 lift -0,096).

## Domanda 1 (opzione A decisa dall'utente): come si corregge il veto di regime?

Il veto di regime nasce dalla sez. 4 (ingressi vietati in regime estremo/molto basso). Con una testa non informativa l'argmax e' rumore. Opzioni da valutare (indica la tua, o un'altra, con motivo):
- (a) togliere il veto dal baseline (regimi ammessi = tutti tranne i vietati dal contratto, applicato su una grandezza diretta, non su un classificatore);
- (b) calcolare il regime dal valore diretto di una feature gia' nello stato (es. `rv_ratio_1d_30d`, `atr_1d_pctile_365d`) con soglie fisse, indipendente dal modello;
- (c) usare il veto del modello solo se la testa e' informativa (margine dell'argmax sopra l'uniforme, o skill misurato in validation), altrimenti degenere come nei fold 0-2;
- (d) il veto diventa un parametro della griglia con valore "nessuno".
Quale scopo finanziario ha il veto? Va davvero applicato, e con quale grandezza? Serve un test che pinni la scelta e quale impatto ha sul contratto `PolicyContractError` del `RiskManager`.

## Domanda 2: EV gate e griglia del tuning

Se `p_edge` calibrato sta sempre sotto `p_breakeven`, nessuna soglia produce trade e il fold e' "untunable". E' il comportamento corretto del sistema (non si entra con valore atteso negativo)? Oppure la calibrazione sul solo validation di un fold (finestre corte, tasso base diverso dal train) distorce `p_edge`? Va ancorata la griglia a `p_breakeven` (p_edge_min tra `p_breakeven` e il massimo osservato)? Un fold senza trade e' un risultato "neutro" o va contato come fallimento nel criterio di uscita di P1 (PnL > 0 su >= 3/5 fold e holdout)? Come si tratta nel gate GE un fold con zero trade?

## Domanda 3: idea dell'utente (SOLO ANALISI, NESSUNA IMPLEMENTAZIONE)

Testo dell'utente, quasi letterale: "quando ho pensato Chronos mi sono immaginato una cosa molto semplice. Se prendessi tipo 10 strumenti molto volatili potrei banalmente fare un ordine per ognuno di essi e aspettare che il prezzo salga, per vedere, calcolando le fee, se guadagno. Le news e gli indicatori mi dicono se lo strumento sale o scende a lungo termine per grossi volumi e tanti trade, ma potrebbe essere molto piu' semplice la cosa."

Si chiede a trader-quant e architetto:
- La strategia semplice (basket di ~10 strumenti volatili spot, un ordine long ciascuno, uscita a target/stop o a orizzonte, esito netto di fee) ha un vantaggio statistico in se'? O l'edge dipende comunque da un criterio di selezione dell'istante di ingresso e dello strumento (come fa il modello)?
- Aiuterebbe sul problema misurato? In particolare: (i) piu' strumenti aumentano N_eff e la potenza del test (oggi N_eff 665 campioni su 3 prodotti con rho 0,54; K5 chiede circa 345 trade indipendenti per rilevare un edge di 0,08)? (ii) un approccio cross-section (scegliere quali strumenti comprare tra 10) e' un'alternativa al market timing a 3 prodotti? (iii) cosa succede ai costi (spread, slippage, fee maker 25 / taker 50 bps) su strumenti piu' volatili e meno liquidi? (iv) rischi: bias di sopravvivenza (strumenti delistati o crollati), liquidita', concentrazione in un fattore comune (beta crypto), size minime del conto (100 EUR), copertura 100% dello spot (C11) con 10 posizioni aperte, correlazione fra posizioni.
- Cosa significa "aspettare che il prezzo salga" con fee reali e orizzonte H = 15 giorni, e quale baseline banale (buy and hold equal-weight, ingresso casuale con stesso bracket) dovremmo battere per dire che c'e' un edge? Il test di permutazione E3 del gate GE e' gia' questo confronto?
- Come si inserisce nel piano senza violare i vincoli (solo spot, long-only, nessuna leva, copertura 100%)? Quali dati servirebbero (backfill di altri prodotti EUR, quali criteri oggettivi di selezione) e quanto costa in tempo?
- Raccomandazione: tenere BTC/ETH/SOL, ampliare il paniere in una fase successiva, sostituire l'approccio, o scartare. Se si amplia, in quale ordine rispetto a: correggere veto/griglia, rifare tuning ed evaluate, aggiungere lo storico news.

Vincoli: rispondere con numeri e formule dove possibile, distinguere cio' che e' misurato nei nostri dati da cio' che e' conoscenza generale di mercato, e dire esplicitamente "non so / serve un dato" quando e' cosi'.

## Risposta trader-quant

Autore: trader-quant. Data: 2026-10-18. Ambito: Domande 1, 2, 3. Legenda: [M] misurato nei nostri dati (fonte indicata);
[D] derivato con una formula da numeri misurati; [G] conoscenza generale di mercato, non verificata sui nostri dati;
[?] non so, serve il dato indicato. Vincoli invariati: solo spot, long-only, nessuna leva, copertura 100%, perdita massima di
un ordine = il suo notional. Nessuna regola qui sotto allenta G0-G4, GE (E1-E5) o l'EV gate.

### Premessa: cosa dicono gia' i numeri

- Pareggio della combinazione in vigore (6h, 3/1.5, maker, H 15 d), A = ATR_6h in bps, s = spread di label:
  `W = k_tp A - c_tp`, `L = k_sl A + c_sl`, `p_be = L / (W + L)`, `c_tp = 50`, `c_sl = 25 + 50 + s/2 + slip + x = 90 + s/2` bps.
  Verifica [D] contro [M] (`feasibility.md`, per prodotto): BTC A = 164 -> 337 / 779 = 0.433 (report 0.432); SOL A = 305 ->
  549 / 1414 = 0.388 (report 0.388). La formula riproduce i dati e la uso sotto.
- Entrare sempre, senza selezione: [M] p_hist raw 0.348, p_be 0.405, W + L 1116 bps -> [D] EV ~ (0.348 - 0.405) x 1116 ~
  -64 bps per trade (approssimazione a due esiti; con 3/2 il report precedente misurava -43.6 bps). [?] Il valore misurato per
  3/1.5 (media di net_ret su tutti i fill) va stampato nel report.
- Attrito dei costi [D]: durata media 74 h [M] -> un book sempre investito fa ~ 8760 / 74 ~ 118 round trip per prodotto
  all'anno; costo medio per round trip ~ 0.35 x 50 + 0.63 x 92 + 0.02 x 82 ~ 77 bps -> ~90% del notional investito all'anno.
  Buy and hold paga un solo round trip (~80 bps).
- Potere discriminante [M]: AUC edge per fold 0.43-0.61, holdout 0.553; logloss mai sotto il prior. [D] Con un modello
  binormale (`d' = sqrt(2) Phi^-1(AUC)`) e tasso base 0.35: AUC 0.55 da' precisione ~0.42 nel decile superiore e ~0.44 nel 5%
  superiore, cioe' +0.02 / +0.03 sopra p_be; per +0.08 sul decile superiore serve AUC ~0.60, stabile fra fold. Con K5 [M]
  (`sigma_r` 584, `W + L` 1116): `N_min = 2.21 / delta^2`, quindi delta 0.02 -> ~5.500 trade indipendenti, indimostrabile.
  Il problema di fondo e' il segnale. Veto e griglia sono difetti che oggi nascondono il verdetto, non ne sono la causa.

### Domanda 1: veto di regime

Scopo finanziario. Il veto non prevede la direzione. Serve a non entrare quando il modello di costo e di esecuzione su cui e'
calcolato `p_be` smette di valere.
- Volatilita' molto bassa: A piccolo -> `p_be` alto. Fee floor ed EV gate lo prezzano gia' in modo esatto con l'A della
  decisione (es. A = 100 bps: L = 242, W = 250, p_be = 0.49 [D]). Un veto in piu' e' ridondante e toglie solo campioni.
- Volatilita' estrema: lo slippage reale dello stop supera `stop_extra_slip_bps` = 10, ci sono gap oltre lo stop e il fill
  maker e' piu' avverso [G]. Li' il `c_sl` reale e' maggiore di quello del CostModel, quindi il `p_be` reale e' maggiore di quello
  calcolato: il veto protegge da un EV sovrastimato. [?] Non e' misurato: serve lo slippage reale degli stop per percentile di
  volatilita' (recorder / dry run) e la diagnostica D1.4.
- Conseguenza: il veto riguarda le condizioni ATTUALI all'ingresso e va misurato su una grandezza osservata adesso. La testa
  `vol_regime` prevede la volatilita' FUTURA e oggi e' non informativa ([M] probabilita' medie 0.19-0.21, argmax su una classe
  estrema): usarla come filtro significa filtrare con il rumore.

Decisione: opzione (b), con soglie fisse decise prima del tuning e mai ottimizzate.
- D1.1 Grandezza: `q_t` = rango percentile di `a_t = atr_14_6h(t) / close(t)` (la stessa A del bracket) rispetto ai valori dello
  stesso prodotto nella finestra causale `[t - 365 d, t)`. Se nello stato c'e' solo `atr_1d_pctile_365d`, e' un proxy accettabile
  (stessa informazione su scala piu' lenta). La preferenza va alla 6h, perche' e' la A che fissa stop e costi. Finestra minima
  90 giorni: con meno storico, nessun ingresso. [?] Non so se esistono barre prima del 2023-10-01; se no, la validation di
  fold_0 (da 2024-02-21) avra' una finestra di ~140 giorni, sufficiente.
- D1.2 Classi (le 5 del contratto, con nuovi confini): 0 se q < 0.05; 1 se 0.05 <= q < 0.35; 2 se 0.35 <= q < 0.65; 3 se
  0.65 <= q < 0.95; 4 se q >= 0.95. Ingressi ammessi {1, 2, 3}. Si esclude cosi' il 10% del tempo (5% per coda) e non il 40% dei
  quintili. Ogni punto di tempo escluso riduce N_eff, che e' gia' il vincolo che lega. Il 5% basso e' quasi sempre gia' scartato
  dall'EV gate, il 5% alto e' la coda in cui il costo dello stop non e' affidabile.
- D1.3 `m_reg = 1.0` per le classi 1, 2, 3 nel baseline. Il sizing e' gia' normalizzato per la volatilita' (notional
  proporzionale a `1 / (k_sl A + c_sl)`). Un moltiplicatore di regime raddoppierebbe la correzione senza un dato che lo giustifichi.
- D1.4 Diagnostica (verifica la soglia, non la sceglie): per decile di q, sulla finestra di selezione della feasibility e sulla
  serie raw, riportare p_hist, p_be, lift, `SE = sqrt(p (1-p) / N_eff_decile)` e net medio. Se il decile alto non e' peggiore
  del pool di oltre 2 SE, le label non mostrano danno nella coda. Il veto resta comunque, per il motivo di esecuzione, finche'
  il dry run non misura lo slippage degli stop.
- Contratto: `PolicyContractError` invariato. La policy emette per un BUY solo le classi {1, 2, 3} e il RiskManager continua a
  rifiutare 0 e 4. Le probabilita' della testa `vol_regime` non entrano piu' nella decisione di BUY.
- Test che fissano la scelta (l'architetto li formalizza): (i) q = 0.96 -> HOLD `regime_veto`; q = 0.50 -> ammesso;
  q = 0.04 -> HOLD `regime_veto`; (ii) cambiare le probabilita' di `vol_regime` non cambia ne' `resolve` ne' `buy_mask`;
  (iii) su un backtest completo ogni BUY ha un regime presente in `regime_multipliers` (zero `PolicyContractError`);
  (iv) causalita': `q_t` non cambia se si alterano le barre successive a t.

Opzioni respinte:
- (a) Nessun veto: accettabile come caso limite, ma lascia la coda alta senza protezione sul costo dello stop. (b) costa solo il
  5% dei campioni.
- (c) Interruttore sullo skill della testa: lo skill si misura in validation con N_eff 17-51 per fold (Domanda 2), quindi
  l'interruttore stesso sarebbe rumore e il comportamento cambierebbe da fold a fold per caso.
- (d) Veto nella griglia: aggiunge gradi di liberta' a un tuning che gia' sceglie il massimo fra 20-80 combinazioni su 35-55
  trade (R2.5). Aumenta il selection bias senza aggiungere informazione.

### Domanda 2: EV gate, calibrazione, griglia, fold senza trade

2.1 Il comportamento e' corretto. Con `p_edge < p_be` il valore atteso dopo i costi e' negativo e non si entra. L'EV gate resta
e nessuna soglia va abbassata.

2.2 La calibrazione su una sola finestra di validation pero' distorce `p_edge`, ed e' misurabile.
- [D] Campioni indipendenti per finestra: indep/yr del pool 254 [M] -> ~0.70 al giorno. Finestre di validation [M,
  `tune_report.json`]: fold_0 ~25 giorni -> N_eff ~17; fold_1 ~50 -> ~35; fold_2 ~74 -> ~51. Errore standard del tasso base
  `sqrt(0.33 x 0.67 / N_eff)` = 0.11 / 0.08 / 0.066. Il lift richiesto (p_be - p_hist) e' 0.074 [M]: l'errore sul solo livello
  vale quanto tutto l'edge richiesto.
- Un calibratore stimato su quella finestra (isotonico o qualunque calibratore che riallinea la media) porta la media di
  `p_edge` sul tasso di UPPER della finestra, +- 1 SE. L'EV gate finisce cosi' per decidere sul rumore della finestra, non sul
  modello.
- final: `p_edge` ha mediana 0.22 [M], mentre p_hist raw per semestre e' 0.353 nel 2025H2 e 0.315 nel 2026H1 [M, feasibility].
  Le spiegazioni compatibili sono due. (i) La finestra di validation finale ha davvero avuto un tasso di UPPER intorno a
  0.22-0.25: allora il modello ha ragione e non si deve entrare. (ii) La calibrazione e' distorta. [?] Servono media di `p_edge`,
  frequenza osservata di UPPER e N_eff sulla finestra di validation finale (griglia oraria, sample con fill), e gli stessi valori
  per fold_3 e fold_4. Se `|media(p_edge) - p_hist osservata| > 2 SE`, la causa e' la calibrazione.

Regole di calibrazione (sostituiscono il calibratore stimato sulla sola finestra di validation):
- R2.1 Il calibratore di `edge` si stima su un insieme con N_eff >= 200 (SE del livello <= 0.033, meno della meta' del lift
  richiesto). Fonte: previsioni out-of-fold del solo passato (cross-fitting con purge sul train del fold, oppure le validation
  dei fold precedenti concatenate). Mai dati del test del fold.
- R2.2 Con N_eff < 500 il calibratore e' parametrico, a 2 parametri (Platt: `logit(p_cal) = a + b logit(p_raw)`), non
  isotonico. L'isotonico ha tanti gradi di liberta' quanti i salti e, con N_eff ~200, sbaglia soprattutto nelle code, cioe'
  proprio nella regione `p_edge >= p_be` dove si decide.
- R2.3 Diagnostica per fold: `media(p_edge) - p_hist osservata` sul test, con SE, anche sui soli sample con `p_edge >= soglia`
  (e' gia' `edge_bias_decision_region`, decisione D5 della fattibilita').

2.3 Griglia ancorata all'EV, non ai quantili.
- La condizione effettiva d'ingresso e' `p_edge >= max(p_edge_min, p_be(t) + ev_min_bps / (W + L)(t))`, con `p_be(t)` calcolato
  per ogni decisione (dipende da A e dallo spread). Qualunque `p_edge_min < min_t p_be(t)` e' un duplicato: oggi molte combinazioni
  sono sprecate o non possono mai passare.
- R2.4 La soglia diventa un margine sopra il pareggio della singola decisione: `p_edge >= p_be(t) + delta`, con
  `delta in {0.00, 0.02, 0.04, 0.06, 0.08}`, equivalente a `EV(t) >= delta x (W + L)(t)` in bps. delta = 0 coincide con l'EV
  gate, quindi la griglia non puo' scendere sotto il pareggio. I margini positivi coprono l'errore di calibrazione: con ECE
  0.03-0.05, un trade con `p_edge = p_be` ha EV reale nullo o negativo. Nota per l'architetto: `p_edge_min` entra anche in
  `scale_p` del sizing; la nuova semantica va riportata li' in modo coerente.
- R2.5 Meno gradi di liberta'. [D] La media per trade su 35 trade ha SE ~ 584 / sqrt(35) ~ 99 bps, e il massimo di 80
  combinazioni correlate e' gonfiato fino a ~ +2 SE dal solo rumore. Il fatto che ANCHE il massimo sia negativo in fold_0..2 [M]
  e' una prova forte che in quelle finestre non c'e' edge sfruttabile. Raccomando al massimo 20 combinazioni (delta x 5,
  tau_buy x 2, c_min x 2, nessuna dimensione di regime). Regola di scelta invariata: massimo PnL fra le combinazioni con >= 30
  trade.
- R2.6 Esiti distinti per fold nel report: `no_candidates_regime` (difetto, dopo D1 non deve piu' comparire), `untunable_ev`
  (il quantile 95% di `p_edge - p_be(t)` sulla finestra e' < 0: il modello vede EV negativo ovunque), `untunable_pnl` (ci sono
  trade, ma nessuna combinazione con >= 30 trade ha PnL > 0), `ok`.

2.4 Fold senza trade nel criterio di uscita e nel gate GE.
- Su un fold untunable il test si esegue con la politica "nessun ingresso" (book piatto). Non si prendono in prestito le soglie
  di altri fold: cambierebbe la procedura che si sta valutando.
- PnL del fold = 0 esatto, che conta come NON positivo nel criterio di P1 ("PnL > 0 su >= 3/5 fold") e in E4
  (`n_positive >= 3`). Non e' una perdita ma non e' nemmeno evidenza, e il criterio chiede una prova positiva di edge. Trattarlo
  come neutro (es. 3 positivi su 4 fold con trade) equivale ad abbassare la soglia. E1 conta 0 trade. E2 e il bootstrap usano
  solo i trade esistenti, senza valori fittizi a 0 bps. E3 su (fold, prodotto) con n = 0 non estrae nulla. Nella concentrazione
  di E4 il fold vale 0.
- Lettura finanziaria: in produzione astenersi in un regime a EV negativo e' il comportamento voluto, ma per DICHIARARE un edge
  servono trade. Con 2 fold piatti su 5 servono 3 fold positivi su 3 attivi: e' severo, e va bene cosi'.
- Dopo le correzioni (D1, R2.1-R2.6) si rifanno tune ed evaluate. Aspettativa onesta [D], dai numeri di AUC: GE molto
  probabilmente NO-GO, oppure INCONCLUSO per E1. E' un risultato valido, non un errore da aggirare.

### Domanda 3: paniere di ~10 strumenti volatili, un long ciascuno

3.1 Vantaggio statistico in se'? No.
- Prezzo senza drift (martingala): per il teorema dell'arresto opzionale ogni regola di uscita (target, stop, orizzonte) ha
  rendimento lordo atteso 0, quindi il netto e' meno i costi per trade [G, matematica standard]. Con barriere 3/1.5, in un random
  walk P(TP prima) = `k_sl / (k_tp + k_sl)` = 1/3, contro p_be 0.405 [M]: `lift_rw` 0.072 [M].
- Con drift mu > 0 la strategia guadagna mu x tempo in posizione, meno ~77 bps per round trip [D]. Quel drift e' il mercato
  (beta), non un edge: buy and hold lo incassa intero con un solo round trip, mentre il bracket lo incassa a pezzi di ~3 giorni e
  paga ~118 round trip all'anno [D]. Nei nostri dati entrare sempre con il bracket da' circa -64 bps per trade [D] (con 3/2
  -43.6 [M]), nonostante un mu di +0.29/anno per BTC e +0.42 per SOL [M].
- "Aspettare che il prezzo salga" senza stop equivale a buy and hold. Su strumenti molto volatili la crescita composta e'
  `g = mu_arit - sigma^2 / 2` [G]. Con sigma annua 1.0 (ordine di grandezza di molte altcoin [G]) la volatilita' toglie 50%
  all'anno di crescita; con sigma 0.5 (ordine di BTC) il 12.5%. Cercare strumenti "molto volatili" alza il drift necessario
  per guadagnare tenendo.
- Quindi l'edge dipende comunque da un criterio di selezione (quando e cosa comprare). Il paniere non lo sostituisce: cambia
  la popolazione su cui il criterio lavora.

3.2 Dove aiuta.
- (i) Potenza. `N_eff = sum_p N_p / (1 + (K - 1) rho)`. Il guadagno rispetto a un prodotto e' `K / (1 + (K - 1) rho)`, con tetto
  `1 / rho` per K -> infinito. Con rho = 0.54 [M, BTC/ETH/SOL]: K = 3 -> 1.44; K = 10 -> 1.71 (+19%); tetto 1.85. A parita' di
  storico (N_p ~460 [M]) N_eff passa da 665 a ~790. Anche con rho = 0.30 (ipotesi favorevole, [?]) K = 10 da' 2.70 contro 1.88
  a K = 3 (+44%), N_eff ~1.240. K5 [M]: un delta di 0.08 richiede 345 trade, un delta di 0.05 ne richiede 883. Il paniere non
  rende dimostrabile un edge di 0.03 e avvicina di poco un edge di 0.08. Finche' rho resta ~0.5, la potenza cresce molto di piu'
  con il tempo (storico e dry run) che con il numero di strumenti.
- Costo espresso in probabilita': un vantaggio reale ma piccolo. p_be scende verso `k_sl / (k_tp + k_sl)` = 1/3 al crescere di A.
  [M] BTC (A 164) -> 0.432; SOL (A 305) -> 0.388. [D] Per un'ipotetica altcoin con A = 500 bps, spread 20, slip 20, x 30:
  c_sl = 135, L = 885, W = 1450, p_be = 0.379 (`lift_rw` 0.046 invece di 0.072). Lo slippage piu' alto se ne mangia una parte:
  [?] spread, slippage e slippage degli stop reali delle altcoin quotate in EUR non sono misurati.
- (ii) Cross-section. Il confronto relativo (comprare i migliori k su 10 nello stesso istante) cancella il fattore comune nel
  TEST: i residui `r_i - r_media` sono poco correlati e danno fino a ~(K - 1) osservazioni per istante invece di `1 / rho`. E' un
  vantaggio reale per misurare la capacita' di selezione. Ma un portafoglio long-only spot non puo' coprire il fattore: il PnL
  resta `beta x mercato + alpha - costi`, e in un mercato che scende tutti e 10 perdono. Letteratura [G]: nel cross-section crypto
  sono documentati fattori di mercato, size e momentum (es. Liu, Tsyvinski, Wu), non verificati su Coinbase EUR spot con le nostre
  fee e a 15 giorni. E' un problema diverso (label di rango relativo, altro modello), non una correzione di P1.

3.3 Dove non aiuta o peggiora.
- (iii) Costi. Le fee in bps sono le stesse (25/50: il tier dipende dal volume [M, probe]). Spread e slippage sulle coppie EUR
  delle altcoin sono piu' alti [G: molte altcoin hanno la liquidita' su USD/USDC e un book EUR sottile]. [?] I valori su
  Coinbase EUR non sono misurati. `stop_extra_slip_bps` = 10, tarato sui major, non vale per loro. Con ordini da 5-15 EUR
  l'impatto sul book e' trascurabile: contano lo spread e i gap degli stop.
- (iv) Sopravvivenza. Scegliere oggi i 10 strumenti piu' volatili fra quelli quotati esclude quelli delistati o crollati [G],
  quindi il backtest e' ottimista per costruzione. Scegliere per volatilita' o rendimento passati su tutto lo storico e'
  look-ahead. [?] Non so se l'API Coinbase restituisce candele di prodotti delistati: se non lo fa, il bias resta in parte e va
  dichiarato.
- Correlazione e concentrazione. Dieci long sono in gran parte una sola posizione sul fattore crypto [M: rho 0.54 sui 3 major;
  G: correlazione alt-BTC alta]. [D] Varianza del paniere equal-weight diviso varianza di un singolo strumento = `rho + (1 - rho) / K`:
  0.69 con K = 3, 0.59 con K = 10 (rho dagli indicatori UPPER, usato come proxy). La diversificazione e' modesta, e in un crollo
  gli stop scattano insieme.
- Conto da 100 EUR e C11. [D, sizing 4.1.2] notional / equity = `0.005 / ((k_sl A + c_sl) / 1e4)`: con A = 300 -> 9.2%
  (9.2 EUR); con A = 500 e c_sl 135 -> 5.6%. Con `max_total_exposure_frac` 0.50 si aprono 5-8 posizioni insieme, non sempre 10.
  La copertura regge: la somma di notional e fee resta <= saldo libero (cap al 50% e BalanceGuard), la perdita massima e' il
  notional allocato e il saldo non va mai sotto zero. Stop colpiti tutti insieme: n x 0.5% dell'equity piu' i gap. Caso estremo
  (gap totale): l'esposizione aperta, <= 50 EUR. [?] Per ogni nuovo prodotto servono `quote_min_size`, `base_increment` e
  `quote_increment`: incrementi grossolani spostano stop e target con l'arrotondamento.
- Fill maker: [M] 0.92 sui major; [?] sulle altcoin. Inoltre un limite d'acquisto si riempie piu' facilmente proprio quando il
  prezzo scende (selezione avversa) [G].

3.4 Baseline banali da battere (stesso periodo, stesse fee, stessa finestra OOS).
- B0 Buy and hold equal-weight dei prodotti del book: un round trip, nessun ribilanciamento. Misura il beta disponibile al
  costo minimo. Report: rendimento annualizzato, max drawdown e rendimento per giorno di esposizione, confrontato con quello del
  modello (il modello e' investito solo per una frazione del tempo).
- B1 Ingresso casuale con lo stesso bracket, lo stesso numero di trade per fold e prodotto e una posizione per prodotto: e'
  esattamente E3 (permutazione) del gate GE. Separa la capacita' di selezione dal drift del periodo.
- B2 Sempre investito con lo stesso bracket (la strategia dell'utente sui 3 major): circa -64 bps per trade [D]. [?] Il valore
  misurato va stampato da evaluate.
- E3 e' il confronto giusto per dire "c'e' un edge di selezione", ma non risponde a "conviene rispetto a tenere": in un mercato
  rialzista un modello puo' battere B1 e perdere contro B0. Raccomando B0 e B2 come diagnostica nel report di evaluate, senza
  soglia e senza toccare GE. Scegliere fra un bot che batte B1 ma non B0 e un buy and hold e' una decisione dell'utente.

3.5 Inserimento nel piano, vincoli, dati.
- Vincoli: compatibile. Solo spot, long-only, ogni posizione coperta dal saldo, nessuna leva: nessun nuovo rischio di debito.
- Criteri oggettivi dell'universo, point-in-time (ricalcolati ogni mese con i soli dati passati): prodotto spot quotato in EUR su
  Coinbase e online; storico >= 365 giorni (serve a q di D1 e riduce il rischio delle nuove quotazioni); volume medio giornaliero
  in EUR sui 90 giorni precedenti >= V_min ([?] soglia da fissare dopo il probe); spread mediano misurato <= 10 bps (c_sl <= ~95
  bps, p_be entro ~0.01 da quello dei major). Ordinamento per volume, MAI per rendimento o volatilita' passati: la volatilita'
  entra solo tramite fee floor ed EV gate, come oggi.
- Dati: (1) probe di spread e profondita' dei candidati EUR, ripetuto in giorni e orari diversi per 2-4 settimane (per coprire
  piu' regimi); (2) backfill 1m dei candidati sullo stesso periodo dei major; (3) `feasibility.py` rieseguita con i nuovi
  prodotti: produce gia' lift_0 per prodotto con la guardia B6, rho e N_eff. Costo: backfill e feasibility sono codice gia'
  esistente (ore di calcolo; [?] dipende dal rate limit misurato nei backfill attuali). Il tempo vero e' il probe, che dura
  settimane.
- Regola di adozione (numerica): il paniere si allarga solo se, sulla finestra di selezione, (a) ogni nuovo prodotto passa la
  guardia B6, (b) N_eff del pool cresce di almeno il 30% rispetto ai 3 major, (c) lift_0 del pool non peggiora di oltre 0.01.
  Con rho ~0.5 la condizione (b) e' improbabile [D]: e' il primo numero da misurare.

3.6 Raccomandazione e ordine.
1. Tenere BTC/ETH/SOL. Correggere il veto (D1) e calibrazione e griglia (R2.1-R2.6).
2. Rifare tune ed evaluate, con B0 e B2 nel report. Il verdetto GE, anche se NO-GO, e' l'informazione decisiva e costa poco.
3. In parallelo, solo dati: probe di spread e profondita' dei candidati EUR e feasibility estesa (rho, N_eff, lift_0 per
   prodotto). Il modello non si tocca.
4. Se GE e' NO-GO con AUC ~0.55 e logloss sopra il prior, il paniere NON e' la cura: +19% di N_eff non crea segnale e moltiplica
   l'attrito dei costi. Il passo successivo e' lo storico news (informazione nuova), poi una nuova valutazione. Se GE e'
   INCONCLUSO con media OOS > 0, il paniere diventa utile per accumulare evidenza, ma solo se il punto 3 supera la regola di
   adozione.
5. Approccio cross-section: fase di ricerca separata, dopo P1, con il suo studio di fattibilita'.
Come sostituto del modello l'idea va scartata: comprare e aspettare con il bracket ha EV negativo nei nostri dati. Come fonte di
N_eff e come baseline (B0, B2) va tenuta.

## Risposta architetto

Autore: architetto-opus. Data: 2026-10-18. Ambito: Domande 1 e 2 (specifiche finali, implementabili), Domanda 3 (solo
valutazione, nessuna implementazione). Vincoli invariati: solo spot, long-only, nessuna leva, copertura 100% (C1, C11). Nessuna
regola qui sotto allenta fee floor, EV gate, G0-G4 o GE. Tripwire da NON modificare e da tenere verdi: `tests/test_c11_wiring.py`,
`tests/test_obligations.py`.

### 0. Principio e correzioni minime alla risposta del quant

Principio (Decision Tokenization, regole T1-T3 della risoluzione P1b): una grandezza che decide un ingresso e' (a) un token dello
stato, cioe' la stessa funzione punto-in-tempo in training, backtest e runtime, oppure (b) un costo vivo usato solo dal
RiskManager. Il veto di regime diventa di tipo (a). Il buffer sul pareggio diventa di tipo (b). La testa `vol_regime` resta una
risposta del modello, ma non decide piu' nulla.

Accetto D1 (opzione b) e R2.1-R2.6, con queste correzioni:
- K1. Nome. `margin` e' un token vietato da C11 in tutto `modules/`: `test_no_derivative_margin_or_fund_movement_identifier_exists`
  spezza gli identificatori su `_`, quindi `edge_margin` o `ev_margin` farebbero fallire il tripwire. Il delta del quant si
  chiama `p_be_buffer` ovunque: codice, `policy.json`, report. Nessun identificatore con il token `margin`.
- K2. Grandezza del regime: `atr_1d_pctile_365d`, cioe' il proxy che il quant accetta in D1.1, e non un percentile dell'ATR 6h.
  Motivo: e' gia' un token dello stato (in `FLAT_COLUMNS`), con parita' batch/incrementale e test di troncamento (C7) gia'
  verdi. Un percentile 6h richiederebbe una delle due cose seguenti. O una nuova colonna di stato: `chronos.state.v1` e'
  congelato dal primo `build_dataset`, quindi servirebbero `chronos.state.v2`, un nuovo dataset e un nuovo modello. Oppure un
  input fuori stato per la policy, che viola T1/T2. Si potra' rivalutare solo dopo la diagnostica D1.4.
- K3. Unita'. Il token di stato e' un intero 0..100 (percentuale). Le soglie D1.2 si applicano a quel token come 5/35/65/95. Si
  legge SEMPRE il token arrotondato (`flatten_state` a runtime, `flat_matrix` nel backtest), mai la colonna grezza
  dell'indicatore: cosi' runtime e backtest decidono sullo stesso numero.
- K4. R2.1: la seconda fonte proposta ("validation dei fold precedenti concatenate") NON e' ammissibile. Quelle finestre stanno
  dentro il train espandente del fold k, quindi su di esse i punteggi del booster k sono in-sample. Resta solo il cross-fitting
  con purge sul train del fold, piu' il validation del fold stesso.
- K5. R2.2 e' gia' soddisfatta: il calibratore di `edge` e' gia' un Platt a 2 parametri (`fit_platt`) e nel codice non esiste un
  isotonico. Cambia solo l'insieme su cui si stima.
- K6. R2.5 non fissa `tau_sell`: con 5 x 2 x 2 = 20 combinazioni resta fuori dalla griglia. Lo fisso a priori a 0.50 (SELL piu'
  probabile di HOLD fra le azioni valide, cioe' la regola argmax), e non si tara mai.
- K7. Il buffer si applica al `p_be(t)` del RiskManager (costi vivi, livelli arrotondati), non nella policy: e' l'unico punto in
  cui il pareggio della singola decisione e' noto in modo esatto. La policy perde la soglia `p_edge_min`, che era un duplicato
  (quant 2.3). Le sole diagnostiche (R2.6, regione di G1) usano il token di stato `costs.p_breakeven`. Nel backtest coincide con
  il `p_be` del RM, a meno dell'arrotondamento a 2 decimali.
- K8. Selezione. Una combinazione si sceglie solo se ha >= 30 trade E PnL di validation > 0 (conseguenza di R2.6
  `untunable_pnl` + quant 2.4); altrimenti il fold resta piatto in test. Oggi fold_0..2 sceglievano il massimo anche se negativo.
  E' un cambio di comportamento voluto, e va segnalato all'utente (sez. 6).

### 1. Domanda 1: regime dal token di stato (D1)

1.1 `modules/decision/policy.py` (resta l'unico posto del veto):

    REGIME_FEATURE: Final = "atr_1d_pctile_365d"          # colonna di FLAT_COLUMNS = state.volatility.atr_1d_pctile_365d
    REGIME_EDGES_PCT: Final = (5.0, 35.0, 65.0, 95.0)      # fissi, decisi prima del tuning, mai ottimizzati
    REGIME_RULE: Final = "ATR1D_PCTILE365:5/35/65/95"
    ENTRY_REGIMES: Final = (1, 2, 3)

    def regime_from_pctile(pctile: float | None) -> int | None
        # None o non finito -> None; altrimenti bisect_right(REGIME_EDGES_PCT, pctile), valore in 0..4
        # 4.99 -> 0, 5 -> 1, 34 -> 1, 35 -> 2, 64 -> 2, 65 -> 3, 94 -> 3, 95 -> 4
    def regimes_from_pctile(pctile: np.ndarray) -> np.ndarray
        # gemello vettoriale: np.searchsorted(edges, x, side="right"); -1 dove x e' NaN; dtype int64

    class PolicyConfig(BaseModel):                         # frozen, extra="allow" (serve per tuned_on)
        model: str
        tau_buy: float = Field(ge=0, le=1)
        tau_sell: float = Field(ge=0, le=1)
        c_min: float = Field(ge=0, le=1)
        p_be_buffer: float = Field(ge=0, le=0.20)
        regime_rule: Literal["ATR1D_PCTILE365:5/35/65/95"]
        label_config_sha256: str
        # model_validator: le chiavi legacy "p_edge_min" o "allowed_regimes_for_entry" -> ValueError
        # (un policy.json vecchio o ritoccato a mano non si carica: fail closed)

    class DecisionPolicy:
        def resolve(self, answers: DecisionAnswers, side: Side, *, vol_pctile: float | None) -> Decision
        def buy_mask(self, p_action: np.ndarray, vol_pctile: np.ndarray) -> np.ndarray
        def sell_mask(self, p_action: np.ndarray) -> np.ndarray                  # invariata

Regole di `resolve` con posizione FLAT, in quest'ordine:
1. mascheramento C1 (invariato);
2. P(BUY) mascherata < `tau_buy` oppure `confidence < c_min` -> HOLD `below_threshold`;
3. `regime = regime_from_pctile(vol_pctile)`: se `None` -> HOLD `regime_unavailable`; se `regime not in ENTRY_REGIMES` -> HOLD
   `regime_veto`;
4. altrimenti BUY `thresholds_met`, con `regime` e `p_edge` nella `Decision`.

Con posizione LONG la regola e' invariata (SELL se P(SELL) mascherata >= `tau_sell`). `vol_pctile` viene ignorato, ma resta
obbligatorio nella firma (keyword senza default, T2). La policy non ha piu' soglie su `p_edge`: il motivo `edge_veto` sparisce e
diventa lo skip `ev_negative` / `ev_buffer` del RM (2.2). `resolve` non legge piu' `answers.vol_regime`, e `buy_mask` non lo
riceve nemmeno: e' la garanzia strutturale del test (ii).

1.2 Dove si calcola il regime (stessa funzione dappertutto, nessuno skew, C7):
- Runtime (`modules/decision/engine.py`, quando verra' scritto): `vol_pctile = state.volatility.atr_1d_pctile_365d`, letto dallo
  stesso `DecisionState` inviato al decider. Il blocco `policy` del DecisionRecord registra `regime`, `regime_pctile`,
  `regime_rule`, `p_be_buffer` e, solo come diagnostica, `model_regime = regime_from_probs(answers.vol_regime.probabilities)`.
- Backtest, tune ed evaluate: `vol_pctile = x[:, FLAT_COLUMNS.index(REGIME_FEATURE)]` della stessa `flat_matrix` da cui nascono le
  risposte (i test esistenti la fissano gia' a `flatten_state(StateBuilder.build(...))`), poi `regimes_from_pctile`.
- Training: il regime non entra nel training (nessuna label nuova, nessuna feature nuova). `atr_1d_pctile_365d` era gia' fra le
  FLAT_COLUMNS.
- C7: il token usa solo barre 1d chiuse `K-365..K-1` piu' `K` (sez. 2.2.1.b) ed e' NaN se N < 90, quindi niente ingresso, come
  chiede D1.1. E' coperto dal test di troncamento degli indicatori e dalla parita' T10. Per fold_0 (validation dal 2024-02-21) il
  token e' disponibile.

1.3 `training/backtest.py`:
- `FlatProbCache.get(...)` restituisce `FlatAnswers(answers: AnswerArrays, vol_pctile: np.ndarray)`, nuova dataclass frozen nello
  stesso file. L'npz salva anche `vol_pctile`. La chiave su disco include la costante `FLAT_CACHE_VERSION = "v2"`, cosi' le cache
  vecchie non si rileggono mai (`data/backtest_cache` si puo' cancellare).
- `run_backtest`: `buy = pol.buy_mask(fa.answers.p_action, fa.vol_pctile)`;
  `cand[p] = (rows[buy], p_edge[buy], regimes_from_pctile(fa.vol_pctile[buy]))`; `plan_entry(..., p_be_buffer=policy.p_be_buffer)`.
- `p_regime` resta in cache e in `AnswerArrays` come diagnostica e non decide nulla.

1.4 Testa `vol_regime`: invariata (domanda in QUESTIONS_V1, label `forward_rv_bucket`, booster, temperatura, regola "il modello
finale non puo' essere degenere"). Toglierla cambierebbe QUESTIONS, quindi nuova versione di modello e del protocollo LLM, senza
alcun beneficio. Uso: solo log e diagnostica (`model_regime`), mai veto e mai sizing. I fold degeneri non influiscono piu' sulle
decisioni.

1.5 Contratto con il RiskManager (`PolicyContractError` invariato):
- `plan_entry` continua a sollevare `PolicyContractError` per un BUY con `regime` None o assente da `risk.regime_multipliers`.
- Nuovo controllo strutturale in `RiskManager.__init__`: se non vale `set(ENTRY_REGIMES) <= set(risk.regime_multipliers)` ->
  `ValueError`. Una config che farebbe saltare il contratto non parte.
- D1.3: default `RiskConfig.regime_multipliers = {1: 1.0, 2: 1.0, 3: 1.0}` in `modules/config.py`, e in
  `config/config.example.yml` se contiene la chiave. Le chiavi 0 e 4 restano assenti.

1.6 Test che fissano D1 (`tests/test_decision_policy.py`, salvo dove indicato):
- `test_regime_from_pctile_boundaries`: la tabella di 1.1, piu' None e NaN -> None; il gemello vettoriale da' gli stessi valori.
- `test_regime_veto_tails` (quant (i)): pctile 96 -> HOLD `regime_veto`; 50 -> BUY; 4 -> HOLD `regime_veto`; None -> HOLD
  `regime_unavailable`.
- `test_vol_regime_answer_is_ignored` (quant (ii)): 50 distribuzioni casuali di `vol_regime`, anche one-hot su 0 e su 4 ->
  `resolve` identico.
- `test_buy_mask_matches_resolve`: il test di parita' esistente, adattato alla nuova firma, con pctile NaN e sui confini.
- `test_policy_rejects_legacy_keys_and_other_regime_rule`.
- `tests/test_backtest.py::test_extreme_model_regime_does_not_block_entries` (quant (iii)): decider finto con `p_regime` one-hot
  su 4 ovunque e pctile fra 5 e 95 -> ci sono trade, zero `PolicyContractError`, ogni trade ha un regime presente in
  `regime_multipliers`.
- `tests/test_backtest.py::test_regime_is_causal` (quant (iv)): su barre reali, il regime della riga i calcolato su `bars[:i+1]`
  e' uguale a quello calcolato su tutte le barre, su >= 200 righe campionate.
- `tests/test_risk_manager.py::test_entry_regimes_need_multipliers`.
- Vanno adattati tutti i test che costruiscono `PolicyConfig` con `p_edge_min` o `allowed_regimes_for_entry` (decision policy,
  backtest, tune, evaluate).

1.7 Diagnostica D1.4 (verifica la soglia, non la sceglie). Nuovo CLI di sola lettura
`python -m training.regime_deciles --config config/train.yml [--dataset ...]` -> `data/reports/regime_deciles.md`. Righe:
`samples.parquet` FLAT con `edge_target` noto (fill avvenuto) e `t < holdout_start - embargo` (finestra pre-holdout). NON
rieseguire `feasibility.py`: ricalcolerebbe `holdout_start`. Per ogni decile di `atr_1d_pctile_365d`, piu' una riga "tutti":
n, `p_hist` pesata con `w_edge`, `p_be` mediano (token `p_breakeven`), lift, `N_eff = sum(w_edge) / (1 + (K-1) rho_raw)`, `SE`,
media di `net_ret_bps`. La riga "tutti" fornisce anche il dato [?] del quant "media di net_ret su tutti i fill" per 3/1.5.

### 2. Domanda 2: calibrazione, EV gate con buffer, griglia, fold piatti

2.1 Calibrazione di `edge` (R2.1, R2.2) in `training/baseline_lgbm.py::fit_subset`:

    CAL_FOLDS: Final = 4
    CAL_NEFF_MIN: Final = 200.0
    CALIBRATION_SCHEME: Final = "OOF_PLATT_V1"

    def oof_edge_scores(tr: pd.DataFrame, x: np.ndarray, y: np.ndarray, n_rounds: int,
                        embargo: timedelta, threads: int) -> np.ndarray
        # tr: righe edge del train (FLAT, edge_target noto) con t, label_window_end, w_edge grezzo
        # ritorna il raw score per riga, NaN dove la riga non e' stata valutata

Algoritmo:
1. Booster principale invariato: train -> early stopping sul val -> `n* = best_iteration`.
2. Le righe edge del train, ordinate per `(t, product)`, si dividono in `CAL_FOLDS` blocchi contigui di pari numerosita'
   (`np.array_split`). Per il blocco b: `s_b = min t`, `E_b = max label_window_end`. Insieme di fit = righe del train con
   `label_window_end < s_b - embargo` OPPURE `t > E_b + embargo` (purge sui due lati, `embargo = cfg.embargo_days`). Se l'insieme
   di fit ha meno di `2 x min_data_in_leaf` righe, il blocco non si valuta (NaN). Booster con `LGBM_PARAMS` e objective binary,
   `num_boost_round = n*`, senza validation e senza early stopping, pesi `w_edge` normalizzati a media 1 sull'insieme di fit;
   `z[b] = predict(raw_score=True)`.
3. Insieme di calibrazione C = righe del train con z finito (punteggi OOF dei sotto-modelli) + righe del validation (punteggi del
   booster principale). Mai righe del test del fold.
4. `N_eff(C) = sum(w_edge grezzi su C) / (1 + (K - 1) * rho)`, con K = numero di prodotti e
   `rho = label.raw["feasibility"]["rho_raw"]`, lo stesso valore usato da evaluate (1.0 se manca).
5. Platt (`fit_platt`, invariato) su C, con pesi `w_edge / mean(w_edge su C)`. Nessun isotonico.
6. Se `N_eff(C) < CAL_NEFF_MIN`: per un fold `edge_cal.ok = False` (la calibrazione si salva comunque e resta informativa per G1 e
   G2); per `final` -> `TrainingError` (exit 1).
- `calibration.json` aggiunge `edge_cal: {scheme, n_rows_oof, n_rows_val, n_eff, n_eff_min, ok}`. Il model card aggiunge
  `calibration_scheme: "OOF_PLATT_V1"` e lo stesso blocco per ogni subset.
- Firma: `fit_subset(samples, member, cfg, tag, allow_degenerate, rho: float)`; l'embargo viene da `cfg`.
- Le metriche `val_*` esistenti restano calcolate sul validation, ora con il Platt stimato su C. `action` e `vol_regime` restano
  calibrati come oggi: il quant non li ha toccati.
- Atteso con i numeri del quant (~0.70 indipendenti al giorno nel pool): fold_0 (train di ~140 giorni) ha N_eff ~110 e NON e'
  calibrabile, quindi resta piatto; fold_1 ~230, al limite; fold_2..4 e final sopra 200.

2.2 EV gate con buffer (R2.4) in `modules/risk/risk_manager.py::plan_entry`:
- Firma: il parametro `p_edge_min: float` diventa `p_be_buffer: float` (keyword, obbligatorio). Il chiamante passa
  `policy.p_be_buffer`.
- Dopo il fee floor:

      ev   = model.ev_bps(p_edge, tp_bps, sl_bps)
      win  = model.tp_net_bps(tp_bps) ; loss = model.sl_loss_bps(sl_bps)
      if not ev > risk.ev_min_bps:           -> Skip("ev_negative")                       (invariato)
      if ev < p_be_buffer * (win + loss):    -> Skip("ev_buffer", detail: ev, need, p_be, buffer)
      p_floor = loss / (win + loss) + p_be_buffer                                         # = p_be(t) + buffer

  Equivalenza: `ev >= buffer (W + L)` vale se e solo se `p_edge >= p_be(t) + buffer`. Con buffer 0 il comportamento e' quello di
  oggi.
- `SkipReason` aggiunge `"ev_buffer"`; `_RiskBlock` aggiunge `p_be_buffer: float`.
- Sizing (nota del quant su `scale_p`): `compute_size(..., p_edge_min=p_floor, ...)`. In `modules/risk/sizing.py` il `ValueError`
  per `p_edge_full <= p_edge_min` diventa `scale_p = 1 se p_edge >= p_edge_min, altrimenti 0`. Se il pavimento della singola
  decisione supera `p_edge_full`, ogni trade che passa il gate e' "convinzione piena" per definizione, e il rischio resta comunque
  nel budget `risk_per_trade`. Docstring aggiornata: `p_edge_min` = pavimento della singola decisione.
- Il golden `intent_sol_reference.json` e i test che passano `p_edge_min=` a `plan_entry` si rigenerano con `p_be_buffer=0.05`.
  Il diff deve toccare solo `scale_p`, `risk_eur`, `base_size` e i valori derivati, piu' il nuovo campo `p_be_buffer`.
- La docstring di `risk_manager.py` deve continuare a contenere `atr_14_` (obbligo di `test_obligations.py`).

2.3 Griglia e scelta (`training/tune_policy.py`, R2.4-R2.6):

    BUY_QUANTILES = (0.80, 0.95)              # di P(BUY) mascherata sulle righe di decisione del validation con regime ammesso
    C_MIN_GRID = (0.0, 0.10)
    P_BE_BUFFER_GRID = (0.00, 0.02, 0.04, 0.06, 0.08)
    TAU_SELL = 0.50                           # fisso (K6)
    MAX_COMBINATIONS = 20                     # assert in grid(): mai di piu'
    MIN_TRADES = 30

- Niente dimensione di regime, niente quantili di `p_edge`, niente `p_edge_full - 0.01`.
- Scelta: massimo `pnl_eur` fra le combinazioni con `n_trades >= 30` E `pnl_eur > 0`. A parita': buffer piu' alto, poi `tau_buy`
  piu' alto, poi `c_min` piu' alto (deterministico e prudente).
- Prima della griglia si controlla il model card: `calibration_scheme == "OOF_PLATT_V1"`, altrimenti errore. Un modello vecchio
  non si tara con il codice nuovo.
- Esito per subset, valutato in quest'ordine:
  1. `untunable_calibration`: `edge_cal.ok` falso, la griglia non si esegue;
  2. `ok`: esiste una combinazione scelta;
  3. `no_candidates_regime`: nessuna riga di decisione con regime ammesso (dopo D1 e' un difetto da indagare);
  4. `untunable_ev`: il quantile 95% di `p_edge - p_breakeven` (token di stato, sulle righe con regime ammesso e `p_breakeven`
     finito) e' < 0;
  5. `untunable_pnl`: tutti gli altri casi.
- `policy.json` di un subset non tarato: `{"untunable": true, "reason": "<esito>"}`. Di un subset tarato: i campi di
  `PolicyConfig` piu' `tuned_on` (split, span, n_trades, pnl_eur, objective, min_trades, cadence_min).
- `tune_report.json`, per subset: `reason`, `n_combinations`, `best`, `edge_cal` e
  `val_diag = {n_rows, n_rows_regime_ok, regime_share{0..4}, mean_p_edge, p_hist_obs, n_eff, se, bias_z, ev_headroom_q95}`,
  calcolato sulle righe edge del validation (`samples`, `member == "val"`, calibrazione del subset), con
  `bias_z = (mean_p_edge - p_hist_obs) / se`. E' il dato [?] che il quant chiede per final, fold_3 e fold_4 (R2.3: decide fra le
  spiegazioni (i) e (ii) della sua 2.2).

2.4 Fold piatti nel criterio P1 e nel gate GE (`training/evaluate.py`):
- Subset `untunable`: il test gira come "nessun ingresso", con zero trade, senza prendere soglie da altri fold.
- `pnl_by_fold` contiene SEMPRE le 5 chiavi; un fold piatto vale 0.0. `concentration` resta invariata: 0.0 non e' > 0, quindi non
  conta fra i positivi di E4 e non cambia la quota sul totale.
- E1 conta solo i trade esistenti. E2 e bootstrap usano solo i trade esistenti, senza valori fittizi a 0 bps. E3 considera solo le
  coppie (fold, prodotto) con trade (e' gia' cosi').
- E5: holdout con 0 trade (anche quando `final` non e' tarato) -> `E5 = False`, motivo `holdout_no_trades`. Con 1..4 trade resta
  `None` (invariato).
- `evaluation.json` aggiunge `fold_status{fold_k: reason}` e lo stesso dato per `final`; il report stampa una riga per fold
  (esito, trade, PnL).
- Criterio di uscita P1 (testo per `tasks/todo.md` e per la spec): "PnL > 0 su >= 3/5 fold e sull'holdout; un fold o un holdout
  senza trade ha PnL 0 e non e' positivo".
- Diagnostica R2.3 in `edge_gates`, per ogni fold di test: `bias_all` (media pesata di `p - y`), `n_eff_test`, `se`, `bias_z`.
  La regione di decisione diventa `p >= p_breakeven_token + p_be_buffer` (token letto da `te.x`), al posto di `p >= p_edge_min`.
- Baseline banali (quant 3.4): solo report, nessuna soglia, nessun effetto su GE.
  - B0, buy and hold equal-weight, per ogni span di test e per l'holdout: ingresso al close della prima barra con fee maker,
    uscita al close dell'ultima con fee taker + s/2 + slip, formula esatta `net_return`. Si riportano rendimento, max drawdown
    della curva giornaliera equal-weight e rendimento per giorno di esposizione.
  - B2, sempre investito con lo stesso bracket: catena greedy sui `candidates_for` di ogni (fold, prodotto), cioe' il primo
    candidato, poi il primo con `t_idx >= exit_idx` del precedente, e cosi' via. Si riportano n e media di `label_bps`.
  - Test: togliere B0 e B2 non cambia `status`.

2.5 Formati e versioni:

| Artefatto | Cambia? | Azione |
|---|---|---|
| `DecisionState`, `chronos.state.v1`, FLAT_COLUMNS | No | nessuna |
| QUESTIONS_V1 | No | nessuna |
| `label_config.json` e `sha256` | No | nessuna (NON rieseguire feasibility) |
| `samples.parquet` e manifest | No | nessuna rigenerazione del dataset |
| booster e calibrazione `edge` | Si' | riaddestrare: nuova versione `baseline-lgbm@<data>.<n>` con `calibration_scheme` |
| `policy.json` | Si' (schema) | aggiunge `p_be_buffer` e `regime_rule`; toglie `p_edge_min` e `allowed_regimes_for_entry` |
| cache del backtest | Si' | `FLAT_CACHE_VERSION = "v2"` |
| default di `RiskConfig.regime_multipliers` | Si' | `{1: 1.0, 2: 1.0, 3: 1.0}` |
| `OrderIntent.risk` | Si' (campo nuovo) | golden rigenerato (2.2) |

2.6 Test (oltre a quelli di 1.6):
- `tests/test_risk_manager.py`: `test_ev_buffer_gate` (`p_edge = p_be + buffer - 0.001` -> `ev_buffer`; `+ 0.001` -> intent;
  buffer 0 -> regole di oggi), `test_scale_p_uses_the_decision_floor`, `test_floor_above_p_edge_full_gives_full_scale`, piu' il
  test esistente del contratto `PolicyContractError` per regime 0, 4 e None.
- Il test di sizing che oggi fissa il `ValueError` va aggiornato alla nuova regola.
- `tests/test_baseline.py`: `test_oof_calibration_never_uses_test_rows`, `test_oof_blocks_are_purged` (nessuna riga di fit con
  `[t, label_window_end]` a meno di `embargo` dal blocco), `test_cal_neff_below_200_marks_fold_and_fails_final`,
  `test_platt_is_the_only_edge_calibrator`.
- `tests/test_tune_policy.py`: `test_grid_is_at_most_20_without_regime_dimension`, `test_buffer_grid_and_fixed_tau_sell`,
  `test_selection_requires_positive_pnl`, `test_untunable_reasons_order`, `test_old_calibration_scheme_is_refused`.
- `tests/test_evaluate.py`: `test_flat_fold_is_zero_and_not_positive` (2 fold positivi + 3 piatti -> E4 False),
  `test_bootstrap_uses_only_real_trades`, `test_e5_false_without_holdout_trades`, `test_e5_none_with_one_to_four_trades`,
  `test_trivial_baselines_do_not_change_status`.
- Tripwire: `tests/test_c11_wiring.py` e `tests/test_obligations.py` NON si modificano e devono restare verdi (in particolare:
  nessun identificatore con il token `margin` in `modules/`).

### 3. Ordine dei passi e tempi

1. Policy e regime (1.1, parte policy di 1.6). Non serve nessun modello.
2. RiskManager, sizing, config, golden (1.5, 2.2).
3. Backtest (1.3) e i suoi due test.
4. `baseline_lgbm`: calibrazione OOF (2.1) e test.
5. `tune_policy` (2.3) e test; `evaluate` (2.4) e test.
6. `pytest` completo (tripwire inclusi), `mypy --strict`, `ruff`.
7. Esecuzione: `regime_deciles` (D1.4) -> `baseline_lgbm` (nuova versione; si riusa il dataset
   `data/datasets/20261018T105953Z-1c09e367`) -> `tune_policy` -> `evaluate`.
8. Spec (sez. 5), archiviazione di questo file in `docs/decisions/2026-10-18-p1c-regime-ev.md`, poi svuotare
   `.claude/CONFLITTO.md`.

Tempi (stime grossolane dai log in `data/reports`):
- Dataset: 0, nessuna rigenerazione.
- Baseline: `baseline_run.log` non ha orari, quindi il tempo non e' misurabile. La calibrazione OOF aggiunge 4 fit `edge` per
  subset (24 in tutto), senza early stopping e con `n*` alberi: da qualche minuto a qualche decina di minuti in piu'. Sonnet deve
  stampare l'orario di inizio e di fine di ogni subset.
- Tune: `tune_run.log` misura ~14 min per 80 combinazioni su fold_1 e ~21 min su fold_2, cioe' 10-16 s per combinazione con trade,
  cache compresa. Con al massimo 20 combinazioni per subset e fold_0 saltato: <= ~25 min, probabilmente 10-15.
- Evaluate: mai eseguito su questo modello (nessun log). Stima della risoluzione P1b (O2.1): ~5 min di stati FLAT e ~3 min di
  stati LONG, piu' i backtest a 1 e 15 minuti e 10 000 repliche di permutazione e bootstrap: 20-40 min alla prima esecuzione.
- `regime_deciles`: meno di un minuto.

### 4. Domanda 3: paniere di ~10 strumenti (solo valutazione, nessuna implementazione)

Cosa richiederebbe il codice attuale:

| Componente | Oggi | Lavoro per allargare |
|---|---|---|
| Backfill | `modules.market.backfill --products ...`, gia' generico | solo esecuzione: ~20 min per prodotto e 3 anni a 5 req/s (sez. 0.5), ~2-3 h per 7 prodotti |
| Registry spot | `SpotRegistry.from_raw` dal probe, mai a mano | rieseguire il probe con i nuovi prodotti; controllo di ammissibilita': SPOT, quotato in EUR, `quote_increment / prezzo` piccolo rispetto all'ATR (se no l'arrotondamento sposta TP/SL e scatta `invalid_levels`), `quote_min_size` e `base_min_size * prezzo` <= ~2 EUR |
| Config | `train.yml products`, `AppConfig` | liste di prodotti; alias del sentiment solo se si usano le news |
| `label_config` | uno per il pool, `spread_bps` per prodotto dentro lo `sha256` | nuovi prodotti = nuovo `label_config` = nuovo studio e nuova versione del modello. `feasibility.py` ricalcola `holdout_start`: serve un flag `--report-only` (scrive solo `data/reports/feasibility_universe.md`, mai `label_config.json`) per misurare rho, N_eff e lift_0 senza bruciare l'holdout attuale |
| `spread_for` | costante per prodotto | codice invariato; i valori arrivano dal probe ripetuto (2-4 settimane, quant 3.5) |
| Universo point-in-time | non esiste | componente nuovo: appartenenza mensile calcolata solo sul passato (volume a 90 giorni, storico >= 365 giorni), applicata in `build_dataset` (sample) e in `decision_rows` (backtest). Senza di esso il backtest ha bias di sopravvivenza e look-ahead |
| Split e N_eff | `purged_walk_forward` e `pooled_n_eff` generici | nessun cambio di codice; un solo `rho` medio diventa grossolano con 10 prodotti eterogenei (va riportato per coppia nel report) |
| Backtest a book unico | generico su `markets` | nessun cambio; tempi ~x3.3 (stati FLAT, tune, evaluate); `marks()` costa O(K) per evento, accettabile a K = 10 |
| C11 con piu' posizioni | `BalanceGuard` prenota per ordine, una posizione per prodotto, `max_total_exposure_frac` 0.50 | nessun cambio strutturale: la copertura resta del guard; con 100 EUR e notional di 5-9 EUR le posizioni contemporanee sono 5-8, non 10 |
| Size minime a 100 EUR | sizing con min-size bump dentro il budget | da verificare per prodotto sul probe; con incrementi grossolani molti ingressi finiscono in `below_min_size` o `invalid_levels` |
| Kill switch | daily loss 2% per book | con 5-8 long correlati gli stop scattano insieme e il daily loss diventa il vincolo che lega (lo misura il backtest; non si allenta) |

Costo di complessita': medio-alto. Il codice nuovo e' poco (universo, flag `--report-only`, controllo di ammissibilita'). Pero'
ogni allargamento produce un nuovo `label_config`, un nuovo dataset (~x3.3 righe), un nuovo modello, un nuovo tuning e una nuova
valutazione, e richiede settimane di probe. Il cross-section (scegliere i migliori k su 10) e' un'altra domanda (label di rango,
QUESTIONS_V2): e' un progetto separato.

Raccomandazione, coerente con il quant 3.6:
1. Prima D1 e R2.1-R2.6, poi tune ed evaluate con B0 e B2. Costa ore e da' il verdetto.
2. In parallelo SOLO dati: probe ripetuto dei candidati EUR e `feasibility --report-only`, senza toccare il percorso del modello.
3. Il paniere si apre solo se il verdetto e' INCONCLUSO con media OOS > 0 E la regola di adozione del quant 3.5 e' superata. Con
   NO-GO e AUC ~0.55 il passo successivo e' lo storico news (informazione nuova), non piu' strumenti.
Unica aggiunta architetturale rispetto al quant: il flag `--report-only` e' un prerequisito, perche' senza di esso misurare il
paniere sposterebbe l'holdout.

Versione semplice, per chi non e' tecnico:
> Comprare 10 monete volatili e aspettare che salgano non da' di per se' un vantaggio: nei nostri dati, entrare sempre con lo
> stesso stop e lo stesso obiettivo perde circa mezzo punto percentuale a operazione, dopo le commissioni.
> Piu' monete servono a fare piu' prove, ma le criptovalute salgono e scendono quasi insieme: 10 monete valgono meno di 2 monete
> indipendenti.
> Le monete piccole costano di piu' da comprare e vendere e sono piu' rischiose (alcune spariscono dal mercato).
> Prima finiamo la verifica in corso sulle tre monete principali: in poche ore dira' se il modello ha davvero un vantaggio.
> Nel frattempo possiamo raccogliere i dati delle altre monete, senza rischi e senza toccare il bot, per decidere dopo con i numeri.

### 5. Modifiche alla spec (le applica lo sviluppatore; changelog 2.10)

- Changelog 2.10 (nuovo): regime dal token `atr_1d_pctile_365d` con soglie fisse 5/35/65/95; testa `vol_regime` solo
  diagnostica; `p_be_buffer` sul pareggio della singola decisione, nel RiskManager; calibrazione OOF di `edge` con N_eff >= 200;
  griglia di 20 combinazioni; esiti `untunable_*`; fold piatti non positivi; E5 False senza trade; B0/B2 nel report.
- 0.3: righe dell'EV gate -> `EV(p_edge) > ev_min_bps` e `EV(p_edge) >= p_be_buffer * (W + L)`.
- 2.1, tabella delle domande: riga `vol_regime`, colonna "Uso a valle" -> "solo log e diagnostica".
- 2.1.2: punto 2 (regime da `atr_1d_pctile_365d`, tabella delle classi, `regime_unavailable`); punto 3 (soglie senza `p_edge_min`
  e senza regimi nel `policy.json`); paragrafo "Pavimento economico" (il pavimento e' `p_be(t) + p_be_buffer`, nel RM); punto 4
  (la coerenza action/edge diventa lo skip `ev_negative` / `ev_buffer`).
- 2.2.4: blocco `policy` del DecisionRecord (`regime_pctile`, `regime_rule`, `p_be_buffer`, `model_regime`).
- 2.2.5: esempio di `policy.json`.
- 3.1.6: calibrazione (2.1), griglia e scelta (2.3).
- 3.4: GE con fold piatti, E5 senza trade, `fold_status`, B0/B2; criterio di uscita P1.
- 3.5 punti 8, 9, 10: cache v2 e regime dal token; calibrazione OOF; griglia ed esiti.
- 4.1 (schema di `plan_entry`, passo 7), 4.1.1 (punto 2 e tabella: classi dal token, `m_reg` 1.0), 4.1.2 (`scale_p` con
  `p_floor`, caso `p_floor >= p_edge_full`, esempio ricalcolato).
- Cercare in tutta la spec `allowed_regimes_for_entry`, `p_edge_min` e `argmax(vol_regime` e allineare ogni occorrenza, anche in
  sez. 5.

### 6. Decisioni che spettano all'utente (qui non vengono prese)

1. Accettare la regola "fold o holdout senza trade = PnL 0, non positivo", con E5 = False senza trade e `untunable_pnl` = fold
   piatto. Con i numeri attuali P1 molto probabilmente non si supera, e GE esce NO-GO (o INCONCLUSO per E1). E' la scelta del
   quant; l'alternativa (fold neutri) abbassa la soglia di prova.
2. Accettare che fold_0 resti piatto per costruzione, a causa della regola N_eff >= 200 della calibrazione. P1 richiede allora 3
   fold positivi fra fold_1 e fold_4.
3. Dopo l'esito: investire nel probe del paniere (settimane, macchina accesa, costo zero), nello storico news, in entrambi, oppure
   fermarsi. Avviare il probe in parallelo fin da ora e' compatibile con il piano.
4. (quant 3.4) Se un bot che batte gli ingressi casuali (E3) ma non il buy and hold (B0) valga la pena di essere usato.
