# ANALISI P1d (archiviata, NON implementata) - scelta degli strumenti target

Decisione dell'utente (2026-10-19): non si fa nulla; si resta su BTC-EUR, ETH-EUR, SOL-EUR, si leggono prima i risultati di soak e dry run, al massimo si aggiunge XRP-EUR. Il resto del piano (backfill dei candidati, probe di spread per 4 settimane, flag `--report-only`, data di valutazione fissa) e' sospeso: questo file conserva i dati e i pareri di quant e architetto per quando se ne riparlera'.

Autore: Sviluppatore (Sonnet). Data: 2026-10-19. Stato: DA RISOLVERE. Prima risponde `trader-quant` (sezione "Risposta trader-quant"), poi `architetto-opus` (sezione "Risposta architetto"). Nessun codice finche' non c'e' la risoluzione dell'architetto.

## Richiesta dell'utente (testuale, riassunta)

"Dobbiamo fare un'altra prova. Elenca gli strumenti tradati su Coinbase e vedi quelli piu' volatili con piu' volumi. Potremmo voler cambiare la lista di strumenti. [L'utente ritiene SOL-EUR una stable coin: e' un ERRORE, SOL e' Solana, una crypto volatile; le stablecoin sono USDC/USDT/EURC.] Prima di pensare a nuovi modelli dobbiamo individuare i giusti strumenti target, considerando sempre i limiti."

Limiti del progetto (CLAUDE.md): SOLO spot, nessuna leva, ogni ordine coperto al 100% dal saldo reale, nessun debito; conto da 100 EUR, ordini da circa 5-15 EUR (rischio 0,5% dell'equity per trade).

## Contesto gia' misurato (non rimetterlo in discussione senza motivo)

- P1 e' NO-GO sul pool BTC/ETH/SOL con solo prezzi (docs/decisions/2026-10-18-p1c-regime-ev.md): il modello non ha un vantaggio dimostrabile; media netta di tutti i fill con bracket 3/1.5 ATR(6h) = -58 bps; `lift_0` richiesto +0,074 (limite GO 0,10); N_eff 665 su 3 prodotti con rho 0,54. La risposta precedente di quant e architetto sul "paniere di ~10 strumenti volatili": non e' un vantaggio statistico in se'; +19% di N_eff da 3 a 10 prodotti; i costi, la sopravvivenza e la correlazione pesano; regola di adozione numerica (N_eff +30%, guardia B6 per prodotto, lift_0 non peggiore di 0,01); criteri point-in-time: ordinare per volume, MAI per rendimento o volatilita' passati; probe di spread ripetuto; flag `--report-only` per `feasibility.py` per non bruciare l'holdout. L'utente ora chiede di riesaminare la lista strumenti PRIMA di nuovi modelli.
- Fee reali dell'account: maker 25 bps, taker 50 bps (tier Intro, volume 30 giorni 0). Il `label_config` attuale ha spread di label 3 bps, barriere 3/1,5 ATR(6h), ingresso maker, H = 15 giorni.
- Il codice supporta l'allargamento dei prodotti senza lavoro strutturale (backfill generico, registry da probe, `label_config` per pool con spread per prodotto nello sha256); ogni nuovo pool = nuovo `label_config`, nuovo dataset (circa x N righe), nuovo modello, nuovo tuning e nuova valutazione.

## Dati reali misurati oggi (Coinbase, pubblici, sola lettura, 2026-10-19)

33 prodotti spot quotati in EUR (925 spot in totale su tutte le quote). Colonne: volume mediano giornaliero in EUR sugli ultimi 90 giorni; spread mediano del book su 5 campioni (bps); profondita' al tocco (EUR, il minore fra bid e ask); volatilita' realizzata annualizzata a 90 giorni sui rendimenti giornalieri; storico disponibile (giorni; 1700 = limite della scansione, cioe' >= 4,6 anni); minimo d'ordine 1 EUR per tutti.

| Prodotto | Vol. mediano 90g EUR | Spread bps | Profondita' tocco EUR | Vol. ann. 90g | Storico gg |
|----------|---------------------:|-----------:|----------------------:|--------------:|-----------:|
| USDC-EUR (stablecoin) | 24.868.083 | n/d | n/d | 5% | - |
| USDT-EUR (stablecoin) | 12.818.071 | n/d | n/d | 5% | - |
| BTC-EUR | 12.465.641 | 2,5 | 963 | 36% | >=1700 |
| ETH-EUR | 4.688.196 | 1,3 | 165 | 50% | >=1700 |
| SOL-EUR | 2.568.233 | 2,2 | 484 | 55% | >=1700 |
| XRP-EUR | 1.613.155 | 2,6 | 116 | 66% | 1195 |
| LINK-EUR | 384.091 | 7,3 | 61 | 68% | >=1700 |
| LTC-EUR | 360.473 | 11,5 | 94 | 56% | >=1700 |
| ADA-EUR | 314.681 | 7,8 | 159 | 74% | >=1700 |
| ALGO-EUR | 310.814 | 8,3 | 60 | 74% | >=1700 |
| BCH-EUR | 139.390 | 10,2 | 128 | 92% | >=1700 |
| DOGE-EUR | 137.755 | 5,7 | 458 | 64% | >=1700 |
| DOT-EUR | 135.698 | 8,8 | 62 | 83% | >=1700 |
| XLM-EUR | 131.082 | 5,3 | 62 | 67% | >=1700 |
| AVAX-EUR | 120.169 | 5,9 | 158 | 80% | >=1700 |
| UNI-EUR | 113.205 | 17,6 | 64 | 104% | >=1700 |
| BAT-EUR | 71.438 | 16,8 | 72 | 117% | >=1700 |
| AAVE-EUR | 64.780 | 12,9 | 59 | 85% | >=1700 |

Altri 15 prodotti EUR (volume mediano sotto 60.000 EUR/giorno, non misurato lo spread): CRV, ICP, APE, SHIB, GRT, ATOM, XTZ, FIL, CRO, SNX (flag di trading limitato: `disabled`), ETC, MANA, CHZ, ENS, 1INCH. Osservazione di struttura: la volatilita' cresce al calare della liquidita' (BTC 36% -> BAT 117%); i prodotti "piu' volatili" sono quelli con volume e profondita' minori e spread maggiori (fino a 17 bps contro i 3 bps assunti nelle label). Con ordini da 5-15 EUR il volume giornaliero non e' un vincolo; lo sono lo spread, la profondita' al tocco, lo slippage degli stop e il gap.

Nota di metodo: i volumi e le volatilita' sono misurati sugli ultimi 90 giorni e valgono come ordine di grandezza di oggi; usarli per scegliere i prodotti e poi testare su 3 anni di storico e' look-ahead se la scelta dipende dal rendimento o dalla volatilita' passata.

## Domande

Q1. Gli strumenti attuali (BTC, ETH, SOL) sono la scelta giusta, o conviene un universo diverso? Quali criteri oggettivi e point-in-time (liquidita', spread, profondita', storico minimo, esclusione stablecoin e prodotti limitati) definiscono l'universo ammissibile con i nostri limiti (100 EUR, ordini 5-15 EUR, spot long-only, fee 25/50 bps)? Quali dei 33 prodotti passano e in quale ordine di priorita'? XRP-EUR (4o per volume, spread 2,6 bps, storico 1195 giorni) e' il candidato naturale?
Q2. La volatilita' alta aiuta davvero? Con le fee fisse (25/50 bps) e barriere in ATR, un prodotto con ATR maggiore ha `p_breakeven` piu' basso (costo relativo minore) ma spread e slippage maggiori e gap piu' ampi: calcola, con la formula di CostModel, `p_breakeven` e `required_lift` per i candidati con i loro spread MISURATI (non il 3 bps assunto) per le barriere attuali e per qualche alternativa ragionevole; dì se esiste una fascia di (volatilita', spread) dove il lift richiesto e' piu' basso che su BTC/ETH/SOL, o se il costo di spread e slippage la annulla.
Q3. Come si testa la scelta senza look-ahead e senza bruciare l'holdout (flag `--report-only` per feasibility)? Che cosa va misurato per ogni candidato prima di includerlo (lift_0 con la guardia B6, N_eff, rho verso BTC, spread nel tempo, qualita' dei dati, fill maker osservato)? Quale e' il costo in tempo (backfill di un prodotto: circa 20 minuti; probe di spread ripetuto per settimane) e l'ordine delle azioni?
Q4. Raccomandazione finale: tenere BTC/ETH/SOL; aggiungere XRP; passare a un universo di fascia 2 (LINK, ADA, DOGE, XLM, AVAX...); altro. E cosa NON fare (per esempio scegliere i prodotti per rendimento passato). Distingui cio' che e' misurato qui da cio' che e' conoscenza generale; dì "non so / serve il dato" dove e' cosi'. Correggi esplicitamente nel testo, in una riga, l'errore dell'utente su SOL (non e' una stablecoin).

## Risposta trader-quant

Autore: trader-quant. Data: 2026-10-19. Legenda: [M] misurato (tabella sopra, `feasibility.md`, `label_config.json`); [D] derivato
con una formula da numeri misurati; [G] conoscenza generale di mercato, non verificata sui nostri dati; [?] dato mancante, non so.
Vincoli invariati: solo spot, long-only, nessuna leva, copertura 100%, perdita massima di un ordine = il suo notional. Nessuna regola
qui sotto allenta fee floor, EV gate, G0-G4, GE (E1-E5), la guardia B6 o la regola di adozione P1c 3.5 (a)(b)(c).

Correzione: SOL-EUR NON e' una stablecoin. E' Solana, la crypto piu' volatile del pool attuale (vol. ann. 55% oggi [M], ATR 6h
mediano 305 bps [M]). Le stablecoin sono USDC, USDT ed EURC (vol. ~5% [M]) e restano escluse.

Nota sul calcolo. In questa sessione non avevo uno strumento per eseguire script, quindi lo script Python NON e' stato eseguito.
I numeri di Q2 li ho calcolati a mano con le formule esatte di `CostModel` (`modules/risk/costs.py`, righe 156-229) e li ho
verificati su tre valori del report: BTC (A 164, s 3) -> 0.433 (report 0.432), ETH (A 230) -> 0.405 (0.405), SOL (A 305) -> 0.388
(0.388). Prima di usare la tabella, lo sviluppatore la ricontrolla con `CostModel(...).p_breakeven / required_lift` (tolleranza 0.001).

### Q2 (prima, perche' serve a Q1): la volatilita' alta aiuta davvero?

Formule (ingresso maker, fee 25/50, slip 5, x 10; A = ATR_6h in bps, s = spread in bps):
`c_in = 25`, `c_tp_out = 25` -> `cost_tp = 50`; `c_sl_out = 50 + s/2 + 5 + 10` -> `cost_sl = 90 + s/2`.
`W = k_tp A - 50`, `L = k_sl A + 90 + s/2`, `p_be = L / (W + L)`, `required_lift = p_be - k_sl / (k_tp + k_sl)`.

Conversione volatilita' -> ATR (assunzione dichiarata):
- [G] Parkinson: un moto browniano ha un range atteso per barra `E[high - low] = sqrt(8/pi) sigma_barra = 1.60 sigma_barra`. Una
  crypto e' quotata 24/7, quindi `sigma_6h = sigma_giorno / 2` e `ATR_6h ~ 0.80 sigma_giorno`.
- [D] Calibrazione sui nostri dati: BTC sigma_ann 36% -> sigma_giorno 188 bps -> 150 bps, contro 164 misurati (x1.09); ETH 50% ->
  209 bps, contro 230 (x1.10). Le code grasse e la microstruttura allargano il range di circa il 10%.
- Assunzione: `A ~ 0.88 sigma_giorno = 460 x sigma_ann` (in bps). Riproduce BTC (166 contro 164) ed ETH (230 contro 230). Per SOL
  da' 253 contro 305 [M]: la vol a 90 giorni di SOL e' sotto la sua media 2023-2026, quindi l'istantanea a 90 giorni puo'
  sottostimare A del 20-30% [?]. Per un confronto omogeneo tutti i candidati usano la stessa conversione; SOL compare anche con l'A
  storico misurato. Con A ridotto del 20% per tutti la classifica non cambia: p_be sale di 0.01-0.012 per tutti.

Scenario di stress (assunzione [G], non misurata): `slip = 5 + s` (uno spread intero in piu' sulle gambe taker) e `x = 0.05 A` (gap
dello stop proporzionale alla volatilita': 8 bps su BTC, 12 su ETH, 15-27 sulle altcoin). Entrano solo nella gamba di stop
(ingresso e TP sono maker): `L_stress = 1.55 A + 80 + 1.5 s`.

Margine di stop (headroom): extra costo dello stop, oltre a quello gia' nel CostModel (s/2 + 15 bps), che riporta il p_be del
prodotto a 0.405, cioe' a ETH e alla mediana del pool attuale. Formula: `margine = 0.6807 W - L` (bps).

| Prodotto | sigma ann [M] | s [M] | A [D] | p_be 3/1.5 | lift rich. 3/1.5 | p_be stress | lift stress | margine stop bps | lift 4/1.5 | lift 3/2 | U5 oggi |
|---|---|---|---|---|---|---|---|---|---|---|---|
| BTC | 36% | 2.5 | 166 | 0.432 | 0.098 | 0.432 | 0.099 | -35 | 0.084 | 0.086 | ok |
| ETH | 50% | 1.3 | 230 | 0.405 | 0.072 | 0.407 | 0.073 | 0 | 0.061 | 0.062 | ok |
| SOL (A da sigma) | 55% | 2.2 | 253 | 0.399 | 0.066 | 0.401 | 0.068 | 12 | 0.056 | 0.057 | ok |
| SOL (A storico [M]) | - | 2.2 | 305 | 0.388 | 0.055 | 0.391 | 0.058 | 40 | 0.046 | 0.048 | ok |
| XRP | 66% | 2.6 | 304 | 0.388 | 0.055 | 0.392 | 0.058 | 40 | 0.047 | 0.048 | ok |
| LINK | 68% | 7.3 | 313 | 0.388 | 0.054 | 0.393 | 0.060 | 42 | 0.046 | 0.047 | ok |
| LTC | 56% | 11.5 | 258 | 0.400 | 0.067 | 0.407 | 0.074 | 10 | 0.057 | 0.058 | no (spread) |
| ADA | 74% | 7.8 | 340 | 0.384 | 0.050 | 0.389 | 0.056 | 56 | 0.043 | 0.044 | ok |
| ALGO | 74% | 8.3 | 340 | 0.384 | 0.050 | 0.390 | 0.056 | 56 | 0.043 | 0.044 | ok |
| BCH | 92% | 10.2 | 423 | 0.374 | 0.041 | 0.381 | 0.048 | 100 | 0.035 | 0.036 | limite (spread) |
| DOGE | 64% | 5.7 | 294 | 0.391 | 0.058 | 0.395 | 0.062 | 32 | 0.049 | 0.050 | ok |
| DOT | 83% | 8.8 | 382 | 0.378 | 0.045 | 0.385 | 0.051 | 79 | 0.038 | 0.039 | ok |
| XLM | 67% | 5.3 | 308 | 0.388 | 0.055 | 0.393 | 0.059 | 40 | 0.047 | 0.048 | ok |
| AVAX | 80% | 5.9 | 368 | 0.380 | 0.046 | 0.385 | 0.051 | 73 | 0.039 | 0.040 | ok |
| UNI | 104% | 17.6 | 478 | 0.371 | 0.038 | 0.380 | 0.046 | 126 | 0.032 | 0.033 | no (spread) |
| BAT | 117% | 16.8 | 538 | 0.367 | 0.033 | 0.375 | 0.042 | 159 | 0.028 | 0.029 | no (spread) |
| AAVE | 85% | 12.9 | 391 | 0.378 | 0.045 | 0.386 | 0.052 | 81 | 0.038 | 0.039 | no (spread) |

(Pool attuale [M]: p_be mediano 0.405, lift_rw 0.072; lift_0 3/1.5 0.074, 4/1.5 0.069, 3/2 0.075.)

Lettura:
1. Nel CostModel il lift richiesto scende in modo monotono con A, e lo spread conta pochissimo. [D] A ~300 bps:
   `dp_be/ds = 0.5 W / (W + L)^2 ~ 0.0002` per bps, quindi 10 bps di spread valgono +0.002 di p_be e 1 bps di spread equivale a
   ~1.2 bps di ATR. Le fee fisse (50 bps per TP, 90 per SL) dominano lo spread. Lo spread misurato (fino a 17.6 bps) NON annulla il
   vantaggio della volatilita'.
2. Il rischio vero e' lo stop. `dp_be/dx = W / (W + L)^2 ~ 0.00043` per bps: 1 bps di slippage dello stop non modellato pesa come
   ~2.5 bps di ATR. Per la fascia 2 il margine e' 30-80 bps: se lo stop reale costa 30-80 bps piu' del modello, il vantaggio sparisce.
   [?] Lo slippage reale degli stop sui book EUR sottili (profondita' al tocco 60 EUR) non e' misurato. In condizioni normali un
   ordine da 15 EUR si riempie al bid (costo s/2, gia' nel modello). Il rischio sono le cascate e le "spazzate" del solo book EUR
   [G]: queste pero' compaiono gia' nelle candele EUR, quindi la feasibility le conta in p_hist (stop toccato), non nello slippage.
3. La fascia esiste, ma e' piccola. Con A >= ~300 bps (sigma_ann >= ~65%) e s <= 10 bps, il lift richiesto e' 0.045-0.058 nominale
   (0.051-0.062 sotto stress), contro 0.072 di ETH e del pool e 0.098 di BTC: 0.015-0.027 punti di probabilita' in meno (0.010-0.020
   sotto stress). Su un pool di fascia 2 + major la mediana di p_be scenderebbe da 0.405 a ~0.388 [D].
4. XRP: p_be 0.388 / lift 0.055, uguale a SOL con il suo A storico (0.388 / 0.055) e migliore di BTC (0.098) ed ETH (0.072). XRP non
   migliora rispetto a SOL: migliora la media del pool perche' e' un quarto prodotto con A sopra la mediana.
5. Barriere alternative: 4/1.5 abbassa il lift richiesto di ~0.01 per tutti (0.061 -> 0.043 su ADA); 3/2 e' simile a 3/1.5. Con
   barriere in unita' di ATR il tempo d'uscita in barre non dipende da A [D, invarianza di scala], quindi p_vertical e il vincolo
   H = 15 restano simili sulle altcoin ([?] da verificare: code e mean reversion diverse). La scelta delle barriere resta alla
   regola R-a3 della feasibility sul nuovo pool, non a questa tabella.
6. Il lift richiesto pero' NON e' un edge. Per un prezzo martingala `p_hist = k_sl / (k_tp + k_sl) = 1/3`, quindi lift_0 ~ lift_rw
   (pool [M]: p_hist_0 0.332, lift_0 0.074 contro lift_rw 0.072). Con AUC 0.55 [M] la precisione nel decile superiore e' ~0.42 [D,
   modello binormale P1c]. Margine sopra p_be: pool 0.405 -> +0.015; fascia 2 0.384 -> +0.036 (+0.03 sotto stress). Con K5 [M]
   (`N_min = 2.21 / delta^2`) servono ~9.800 trade indipendenti per +0.015 e ~2.450 per +0.03, contro un N_eff disponibile di
   665-930. Non e' dimostrabile in nessuno dei due casi, e l'AUC varia fra fold da 0.43 a 0.61 con un logloss che non batte mai il
   prior. La volatilita' abbassa l'asticella, ma non fa saltare piu' in alto.

Sizing con i limiti del conto [D]: `notional / equity = 0.005 / (L / 1e4)`. ADA (L 604) -> 8.3 EUR; BAT (L 905) -> 5.5 EUR. Tutti
gli ordini restano sopra il minimo di 1 EUR e coperti al 100%, senza nessun nuovo rischio di debito.

### Q1: criteri dell'universo ammissibile (point-in-time) e prodotti che passano

BTC/ETH/SOL sono una scelta corretta come nucleo: sono i soli, insieme a XRP, con spread <= 3 bps, profondita' >= 116 EUR e
storico lungo. L'idea "piu' volatili con piu' volumi" non trova riscontro nei dati: volatilita' e liquidita' vanno in senso opposto
[M], e nessun prodotto ha entrambe. XRP e' il compromesso migliore.

Criteri, valutati ogni mese a una data t usando SOLO dati precedenti a t (da fissare per iscritto PRIMA di guardare risultati nuovi):
- U1 Spot quotato in EUR, stato online, nessun flag `trading_disabled`, `limit_only`, `cancel_only` o `post_only` [M flag]. Esclude
  SNX.
- U2 Niente stablecoin o asset agganciati a una valuta (USDC, USDT, EURC, DAI, PYUSD) e niente duplicati wrapped o staked (cbETH,
  wBTC) [G]. Gia' la matematica li esclude: con sigma 5% -> A ~23 bps -> W = 3 x 23 - 50 = 19 bps -> p_be ~0.94.
- U3 Storico >= 365 giorni di candele prima di t (regola P1c 3.5; serve anche ad `atr_1d_pctile_365d`).
- U4 Volume: mediana del volume giornaliero in EUR sui 90 giorni prima di t >= 100.000 EUR. E' calcolabile point-in-time dalle
  candele (volume x close). Lo scopo non e' l'impatto (15 EUR sono lo 0.01% del volume) ma la qualita' dei dati (candele 1m vuote) e
  la tenuta del book quando scattano gli stop. La soglia e' fissata ora, prima dei risultati per prodotto.
- U5 Costo di esecuzione (dal probe ripetuto di Q3): spread mediano <= 10 bps (regola P1c), spread p90 <= 20 bps, profondita' al
  tocco mediana >= 50 EUR (>= 3 volte l'ordine massimo di 15 EUR). [?] Lo spread storico non esiste, quindi U5 si applica come filtro
  di oggi: e' un look-ahead dichiarato, lieve perche' il rango di spread e' persistente [G].
- U6 Griglia di prezzo: `quote_increment / prezzo <= 2 bps` e `base_increment x prezzo <= 0.10 EUR` [?]. Servono i metadati del
  prodotto; per le monete sotto 1 EUR (DOGE, XLM, ADA, ALGO) un tick grossolano sposta stop e TP con l'arrotondamento.
- U7 Qualita' dei dati dopo il backfill: candele 1m mancanti o a volume zero <= 5% e nessun buco > 1 giorno [?].
- Ordinamento: volume U4 decrescente. MAI per volatilita', rendimento o lift passati. La volatilita' entra solo tramite fee floor ed
  EV gate, decisione per decisione.

Esito sui dati di oggi (provvisorio: U5 va ripetuto, U6 e U7 non sono misurati):
- Fascia 1 (volume >= 1M EUR, spread <= 3 bps): BTC, ETH, SOL, XRP.
- Fascia 2, nell'ordine di volume: LINK, ADA, ALGO, DOGE, DOT, XLM, AVAX. LINK, ALGO, DOT e XLM passano la profondita' di poco (60-62
  EUR su 5 campioni).
- Al limite: BCH (spread 10.2, fuori di 0.2; decide il probe ripetuto, senza arrotondare). LTC escluso (11.5).
- Esclusi: UNI, BAT, AAVE (spread 13-18 bps [M]); i 15 prodotti sotto 60k EUR/giorno (U4); SNX (U1); USDC e USDT (U2).

XRP e' il candidato naturale per costo e liquidita' (4o per volume, spread 2.6, profondita' 116). Due limiti:
- U3: 1195 giorni di storico [M] -> quotato dal ~2023-06-12 [D] (riquotazione dopo la sentenza SEC di luglio 2023 [G]). Con U3 a
  365 giorni XRP entra nel pool point-in-time dal ~2024-06, cioe' copre ~24 dei 31.5 mesi della finestra di selezione
  (2023-10-15 .. 2026-05-28). Non abbassare U3 per farlo entrare prima: sarebbe un criterio cucito sul prodotto.
- N_eff [D], con guadagno `K / (1 + (K - 1) rho)`. Solo +XRP, con rho 0.54: 1.527 contro 1.442 = +5.9% nei mesi coperti, ~+4.5% sul
  totale. Anche con rho medio 0.47 (coppie con XRP a 0.40 [?]) si arriva a ~+11%. Quasi certamente XRP da solo NON passa la regola
  (b) (+30%). Con fascia 1 + fascia 2 (K = 11): rho 0.54 -> +19% (N_eff ~790); la regola (b) passa solo se rho medio <= 0.487
  [D: 11 / (1.3 x 1.442) = 1 + 10 rho]. Con rho 0.45 il guadagno e' +39% (N_eff ~920). Il numero decisivo e' rho, e non e' misurato [?].

### Q3: come testare senza look-ahead e senza bruciare l'holdout

Ordine delle azioni (costo basso, nessun modello toccato):
- A0 (oggi, ~0.5 h). Pre-registrazione: si scrivono U1-U7, le soglie, la lista dei candidati (11 + BCH come caso limite), la regola di
  adozione P1c 3.5 (a)(b)(c) INVARIATA e le metriche sotto. Va fatto prima di vedere qualunque numero nuovo.
- A1 (minuti al giorno, 14-28 giorni di calendario, in background). Probe ripetuto di spread e profondita' sui 12 prodotti (piu' i 3
  major come controllo), 4 volte al giorno (00/06/12/18 UTC), weekend compresi. Si salvano mediana, p90, quota di campioni con spread
  > 20 bps e profondita' min(bid, ask). Per stimare lo slippage dello stop: costo effettivo di una vendita a mercato da 15 EUR
  (percorrendo il book), `x_hat = (mid - VWAP_15EUR) / mid - s/2`, mediana e p95, anche condizionato ai minuti con |rendimento 1m|
  sopra il p95 (mercato veloce). E' il dato [?] che decide il margine di Q2.
- A2 (~4 h di macchina: ~11 prodotti x 20 min [M]). Backfill 1m dei candidati sulla stessa finestra dei major (dal 2023-10-01, o dalla
  quotazione), piu' i metadati U6.
- A3 (minuti). Qualita' dei dati U7 per prodotto.
- A4 (ore di calcolo [?]). `feasibility --report-only` su tre pool: P0 = BTC/ETH/SOL (controllo di regressione: deve riprodurre
  esattamente lift_0 0.074 e N_eff 665), P1 = P0 + XRP, P2 = P0 + XRP + fascia 2 ammessa. Vincoli: `holdout_start` resta 2026-06-20,
  letto dal `label_config` attuale e mai ricalcolato; nessuna scrittura del `label_config`; nessuna riga con t >= cut (2026-06-18).
  Spread di label per prodotto = max(3, mediana del probe ripetuto): MAI il 3 fisso sulla fascia 2. Ogni prodotto entra solo nei mesi
  in cui passa U3 e U4 (maschera di appartenenza point-in-time).
- Metriche per prodotto: N_p, p_hist detr/raw, p_be mediano, lift_0 con la guardia B6, lift_rw, p_vertical, fill maker dalle barre,
  durata mediana, jackknife per semestre, matrice rho a coppie verso BTC/ETH/SOL (non solo lo scalare del pool). mu_year solo come
  diagnostica: mai usato per scegliere. Per pool: lift_0, N_eff, rho medio, combinazione scelta con R-a3.
- A5 (decisione). Regola P1c 3.5 invariata: (a) ogni nuovo prodotto passa B6; (b) N_eff del pool +30% rispetto a P0; (c) lift_0 del
  pool non peggiore di oltre 0.01. Si aggiunge una condizione di onesta' dei costi, che e' una stretta e non un allentamento: un
  prodotto entra solo se `x_hat` mediano misurato <= 10 bps (cioe' il CostModel vale cosi' com'e'). Altrimenti serve un x per
  prodotto (decide l'architetto) oppure il prodotto resta fuori. Un lift_0 migliore NON basta da solo ad adottare un prodotto: il
  miglioramento viene dalla volatilita' passata, e adottarlo per questo sarebbe una selezione per volatilita'.
- A6 (solo se A5 adotta; giorni). Nuovo `label_config`, dataset, modello, tune ed evaluate.
- Sopravvivenza: [?] non so se l'API restituisce le candele dei prodotti EUR delistati nel 2023-2026. Se un prodotto con volume
  >= 100k e' sparito e non e' recuperabile, il bias resta e va dichiarato nel report.
- Fill maker reale e selezione avversa sulle altcoin: [?] li misura solo il dry run. La stima dalle barre (0.92 sui major [M]) e'
  ottimista.

### Q4: raccomandazione

1. Tenere BTC/ETH/SOL come nucleo. Non togliere BTC perche' il suo p_be e' alto (0.432): sarebbe una selezione per volatilita', e
   l'EV gate raziona gia' i trade su BTC decisione per decisione.
2. XRP: si', come candidato, ed e' l'unico di fascia 1. Ha un costo pari a SOL. Da solo pero' aggiunge ~5-11% di N_eff e quasi
   certamente non passa la regola (b): non si aggiunge da solo, si testa insieme alla fascia 2 nello stesso run report-only.
3. Fascia 2 (LINK, ADA, ALGO, DOGE, DOT, XLM, AVAX): nel CostModel il vantaggio e' reale ma modesto (lift richiesto 0.045-0.058
   contro 0.072). Si adotta solo se il pool P2 passa (a)(b)(c) e se `x_hat` misurato e' entro il limite. Il numero da guardare per
   primo e' rho medio <= 0.49.
4. NO ai "piu' volatili" (UNI, BAT, AAVE e i 15 piccoli): lo spread li esclude, e la loro volatilita' viene da book sottili.
5. Aspettativa onesta [D]: l'esito piu' probabile e' P0 invariato, oppure P2 con N_eff +20/40% e lift_0 -0.015/-0.02. Nessuno dei due
   rende P1 GO: il collo di bottiglia e' il segnale (AUC 0.55, logloss mai sotto il prior), non l'universo.

Cosa NON fare:
- scegliere o ordinare i prodotti per rendimento, volatilita', lift_0 o mu passati;
- adattare le soglie (U3 per XRP, U5 per BCH) dopo aver visto i numeri;
- usare lo spread di label di 3 bps sulla fascia 2;
- rieseguire `feasibility.py` senza `--report-only` (ricalcola `holdout_start` e brucia l'holdout);
- allentare G0-G4, GE, la guardia B6, l'EV gate o la regola (a)(b)(c);
- aggiungere prodotti "per vedere" con nuovo dataset e modello prima che A5 sia superato;
- contare su piu' strumenti per creare un edge.

Ordine concreto: A0 oggi -> avviare A1 (gira da solo per 2-4 settimane) -> A2 + A3 (mezza giornata di macchina) -> A4 quando il
probe ha almeno 14 giorni -> A5. In parallelo, senza dipendenze, prosegue il piano P1c sul pool attuale: correzioni D1 e R2, re-tune,
evaluate con B0/B2, poi lo storico news. Il test dell'universo non blocca quel piano e non lo sostituisce.

## Risposta architetto

Autore: architetto-opus. Data: 2026-10-19. Ambito: Q1-Q4 (specifica software, ordine dei passi, valutazione di A0-A5). Vincoli
invariati: solo spot, long-only, nessuna leva, copertura 100% (C1, C11). Tutti i nuovi script usano solo endpoint pubblici in
lettura e non inviano, simulano verso l'exchange o cancellano ordini. Nessuna regola qui sotto allenta fee floor, EV gate, G0-G4,
GE, B6 o la regola P1c 3.5. Tripwire invariati e verdi: `tests/test_c11_wiring.py`, `tests/test_obligations.py`.

### 0. Principio e correzioni al piano del quant

Principio (Decision Tokenization, T1-T3). L'appartenenza di un prodotto all'universo decide se un ingresso e' possibile, quindi e'
una grandezza di decisione: UNA funzione pura e punto-in-tempo, usata identica in feasibility, dataset, backtest e runtime (T1).
Lo spread di label per prodotto resta un costo vivo dentro il `label_config` (tipo b). Nessuna grandezza viene scelta guardando
rendimento, volatilita' o lift passati.

Accetto U1-U7 e A0-A5 con queste correzioni:
- K1. `x_hat` dal probe e' degenere. Se la profondita' al tocco e' >= 15 EUR, la vendita da 15 EUR si riempie tutta al bid, quindi
  `(mid - VWAP_15)/mid - s/2 = 0` esatto. U5 richiede >= 50 EUR, quindi la misura vale 0 per costruzione su ogni prodotto ammesso.
  Lo slippage reale dello stop nasce da cio' che una foto a ora casuale non vede: il salto fra l'ultimo trade e lo stop, il
  movimento del book durante la latenza, il book che si svuota proprio durante la discesa. Si misura con stop virtuali sulle
  registrazioni WebSocket continue (sez. 3.2). Il probe REST resta la fonte di U5.
- K2. L'insieme del probe si fissa con una regola, non con lo spread di 5 campioni: tutti i prodotti EUR che oggi passano U1, U2 e
  U4 (volume mediano >= 100.000 EUR). Sono 14: BTC, ETH, SOL, XRP, LINK, LTC, ADA, ALGO, BCH, DOGE, DOT, XLM, AVAX, UNI. LTC e UNI
  erano esclusi su 5 campioni (11,5 e 17,6 bps): li esclude o li ammette U5 sul probe ripetuto, come BCH. Costo aggiuntivo nullo.
  Questa lista sostituisce i conteggi "11 + BCH" e "12 + 3 major" del quant, che erano ambigui.
- K3. U3 si misura dalla data di quotazione, non dallo store 1m. Lo store parte dal 2023-10-01 per tutti, BTC compreso: U3 calcolato
  sullo store escluderebbe i major fino al 2024-10. La data di quotazione e i volumi di U4 vengono dalle candele giornaliere
  pubbliche (scansione, 1.3).
- K4. U7 e' ambiguo nello store. Lo store non scrive i minuti senza trade (`backfill.py`: "NOT errors and NOT written"), quindi
  "candela mancante" e "volume zero" sono la stessa cosa e misurano la liquidita', non un errore dei dati. Stima grossolana [D da
  M; G sulla taglia media dei trade]: ~300.000 EUR/giorno sono ~200 EUR al minuto, cioe' circa un trade al minuto. La quota di minuti
  vuoti e' probabilmente ben sopra il 5%, quindi U7 cosi' scritto rischia di escludere tutta la fascia 2 per un motivo non voluto.
  Lo divido in U7a (integrita': nessun buco > 24 h nella finestra di selezione) e U7b (quota di minuti vuoti <= soglia). La soglia
  di U7b la conferma o la corregge il quant in A0, PRIMA che venga letto il report del backfill. Senza risposta resta il 5%.
- K5. La regola di adozione si valuta sulla combinazione di barriere congelata nel `label_config` attuale (6h, 3/1,5, maker, H 15),
  non sulla combinazione scelta da R-a3 per ogni pool. Cosi' il confronto isola l'effetto dell'universo e non aggiunge gradi di
  liberta'. Per i nuovi pool R-a3 resta diagnostica (`--with-grid`) e torna decisiva solo in A6.
- K6. N_eff punto-in-tempo. Con prodotti presenti solo in parte della finestra (XRP dal 2024-07), la formula a K fisso penalizza il
  pool. Si usa `N_eff_pit = sum_m (sum_p w_{p,m}) / (1 + (K_m - 1) rho)`, dove K_m e' il numero di prodotti membri nel mese m. Se
  tutti i prodotti sono sempre membri, coincide con `pooled_n_eff` (test).
- K7. Contro le scelte a posteriori:
  (i) i prodotti nuovi che falliscono (a) si tolgono in UN solo passaggio (P2 -> P2_pruned), poi si valutano (b) e (c); nessuna
  potatura ripetuta;
  (ii) A4 e A5 si eseguono una sola volta, a una data fissata ora (giorno 28 della registrazione), mai prima perche' i numeri
  "sembrano buoni";
  (iii) un prodotto con `x` misurato > 10 bps resta fuori. Non esiste un `x` per prodotto: `stop_extra_slip_bps` e' globale e sta
  nello sha256 (COST_V1), e un x per prodotto sarebbe un nuovo modello di costo stimato su 4 settimane di dati.
- K8. Controllo sui major: lo stesso studio misura `x` anche su BTC/ETH/SOL. Se la mediana di un major supera 10 bps, il CostModel
  del pool ATTUALE e' ottimista. Non blocca il test dell'universo, ma apre un conflitto separato.

### 1. Specifica software

1.1 `modules/market/universe.py` (nuovo). Modulo puro, importabile dal runtime: solo pandas/numpy, niente numba, niente import da
`training`.

    UNIVERSE_RULE: Final = "UNIVERSE_V1"
    PEGGED_OR_WRAPPED_BASES: Final = frozenset({"USDC", "USDT", "EURC", "DAI", "PYUSD", "CBETH", "WBTC"})   # U2

    @dataclass(frozen=True)
    class UniverseThresholds:                    # valori pre-registrati in A0, mai ottimizzati
        min_history_days: int = 365              # U3
        vol_window_days: int = 90                # U4
        min_median_daily_eur: float = 100_000.0  # U4
        spread_median_max_bps: float = 10.0      # U5, nessun arrotondamento (BCH 10,2 -> fuori)
        spread_p90_max_bps: float = 20.0         # U5
        depth_touch_median_min_eur: float = 50.0 # U5
        tick_max_bps: float = 2.0                # U6: quote_increment / price
        base_step_max_eur: float = 0.10          # U6: base_increment * price
        min_order_max_eur: float = 2.0           # U6: max(quote_min_size, base_min_size * price)
        max_hole_hours: float = 24.0             # U7a
        max_empty_minute_share: float = 0.05     # U7b, da confermare in A0 (K4)

    @dataclass(frozen=True) class SpreadStats: n, median_bps, p90_bps, share_gt20, depth_touch_median_eur
    @dataclass(frozen=True) class BarQuality: empty_minute_share, max_hole_hours

    def daily_quote_volume_eur(daily: pd.DataFrame) -> pd.Series
        # candele giornaliere (indice = giorno UTC; colonne close, volume) -> volume * close; giorni senza candela = 0
    def member_months(daily: pd.DataFrame, listing_day: pd.Timestamp | None, months: pd.DatetimeIndex,
                      th: UniverseThresholds) -> pd.Series      # bool per ogni inizio mese t (00:00 UTC del giorno 1)
        # U3: listing_day is None (oltre il limite della scansione) oppure t - listing_day >= 365 giorni
        # U4: mediana di daily_quote_volume_eur sui giorni [t - 90 d, t) >= soglia, giorni mancanti contati come 0
        # usa SOLO i giorni < t (la candela del giorno D e' completa a D + 1)
    def membership_at(ts: np.ndarray, months: pd.Series) -> np.ndarray
        # ogni istante prende il valore del suo mese di calendario UTC; mese assente -> False
    def static_reasons(raw: Mapping[str, Any], spread: SpreadStats | None, quality: BarQuality | None,
                       th: UniverseThresholds) -> list[str]
        # U1: SpotRegistry.from_raw({pid: raw}); una SpotViolation diventa "U1:<messaggio>" (stessa regola del runtime)
        # U2: base in PEGGED_OR_WRAPPED_BASES -> "U2"
        # U5, U6, U7: un codice per regola violata ("U5:median", "U5:p90", "U5:depth", "U6:tick", "U6:base_step",
        #   "U6:min_order", "U7a:hole", "U7b:empty")
        # spread o quality None -> "U5:unmeasured" / "U7:unmeasured" (fail closed). Lista vuota = ammesso.
    def bar_quality(real_ts: pd.DatetimeIndex, start: pd.Timestamp, end: pd.Timestamp) -> BarQuality
        # empty_minute_share = 1 - (minuti con candela reale) / (minuti in [start, end)); buco piu' lungo in ore
    def rank_by_volume(volume_eur: Mapping[str, float]) -> list[str]
        # volume decrescente, poi product_id. La firma non riceve rendimenti, volatilita' o lift (garanzia strutturale).

Test `tests/test_universe.py`:
- causalita': cambiare o togliere le righe giornaliere con giorno >= t non cambia `member_months` in t;
- U3 al confine: quotazione 365 giorni prima di t -> membro, 364 -> no; `listing_day None` -> membro;
- U4 al confine: mediana esattamente 100.000 -> membro, 99.999 -> no; i giorni mancanti contano 0;
- U2: USDC-EUR, USDT-EUR, EURC-EUR rifiutati qualunque siano le altre metriche;
- U1: `trading_disabled`, `cancel_only`, `limit_only`, `post_only`, `status` "offline" -> rifiutati, con lo stesso messaggio di
  `SpotRegistry.from_raw`;
- U5: mediana 10,0 passa, 10,01 no; p90 20,0 passa; profondita' 50,0 passa; `spread None` -> "U5:unmeasured";
- U6 e U7: confini su ogni soglia; buco di 24 h esatte passa, 24 h + 1 min no;
- `rank_by_volume`: ordine e parita' deterministici.

1.2 `modules/execution/spot.py`: U1 coincide con l'allowlist del runtime (T1, una sola regola). In `_meta_from_raw`:
- aggiungere `limit_only` e `post_only` ai flag fatali, con la convenzione dei flag esistenti (assente = falso). Uno stop a mercato
  su un prodotto `limit_only` o `post_only` e' impossibile, quindi il controllo stringe e non allenta;
- `status`: se presente e diverso da "online" -> `SpotViolation` (stessa convenzione di `product_venue`, controllato solo se
  presente).
`script/probe_coinbase.py::probe_product` aggiunge `limit_only` e `post_only` alle chiavi raccolte. Test nel file di test esistente
dei flag spot: un caso per ciascun flag e per `status` "offline" e "delisted"; un raw senza `status` resta valido.

1.3 Script di sola lettura (endpoint pubblici, nessuna chiave, nessun ordine):
- `script/spread_probe.py`. Per ogni prodotto: `get_public_product_book(limit=50)` e `get_public_product`. Appende una riga JSON per
  prodotto a `data/probe/spread/<YYYY-MM>.jsonl`, con `ts`, `best_bid`, `best_ask`, `spread_bps`,
  `depth_touch_eur = min(bid_size * bid, ask_size * ask)` e i flag U1 (`status`, `trading_disabled`, `limit_only`, `post_only`,
  `cancel_only`). Un errore su un prodotto si registra (`error`) e non ferma gli altri. Con
  `--summarize --since <ISO> --until <ISO>` scrive `data/probe/spread_summary.json`:
  `{product: {n, n_error, median_bps, p90_bps, share_gt20, depth_touch_median_eur, first, last}}`. Si esegue 4 volte al giorno
  (00/06/12/18 UTC), weekend compresi, con l'Utilita' di pianificazione di Windows oppure con un ciclo `--every-hours 6`.
- `script/universe_scan.py`. Elenca i prodotti spot EUR (`get_public_products`) e salva il raw di ognuno (stesse chiavi del probe).
  Scarica le candele `ONE_DAY` all'indietro, finche' la risposta e' vuota o fino a 1700 giorni. Scrive
  `data/probe/universe_scan_<YYYYMMDD>.json`:
  `{product: {meta, daily: [[day, o, h, l, c, v], ...], listing_day | null, scan_limit_hit}}`. Va eseguito una volta (minuti).
  [?] I prodotti delistati non compaiono nell'elenco: il report lo dichiara come bias di sopravvivenza.
- Test `tests/test_spread_probe.py`: il sommario e' una funzione pura (mediana, p90, quota > 20 bps, profondita') su righe
  sintetiche; le righe con `error` sono escluse dalle statistiche e contate in `n_error`.

1.4 `training/feasibility.py --report-only`. Il ramo e' una funzione separata, `run_report_only(args, app) -> int`, che `main`
chiama prima di qualunque calcolo dell'holdout. Il codice che scrive `label_config.json` e `feasibility.md` resta solo nel ramo
normale, quindi dal ramo report-only non e' raggiungibile.

Argomenti (validi solo con `--report-only`):

    --pool-name NAME            # obbligatorio, regex [A-Za-z0-9_-]+ (es. P0, P1, P2, P2_pruned, R0)
    --products ...              # come oggi
    --label-config PATH         # letto e MAI scritto; default data/datasets/label_config.json
    --spread-source {label_config,probe_series}
    --spread-stats PATH         # obbligatorio con probe_series (data/probe/spread_summary.json)
    --universe-scan PATH        # obbligatorio (data/probe/universe_scan_<YYYYMMDD>.json)
    --with-grid                 # diagnostica: griglia intera e scelta R-a3
    --out-dir                   # come oggi, default data/reports

Con `--report-only` gli argomenti `--fees-from-probe`, `--spread-bps` e `--spread-floor-bps` sono VIETATI (errore di argparse):
sono tutti modi di cambiare il costo delle label.

Comportamento, in quest'ordine:
1. `lc = load_label_config(--label-config)`: lo sha256 viene verificato. File assente o non valido -> exit `EXIT_CONFIG_ERROR`.
   Senza un holdout congelato non si misura nulla.
2. `holdout_start = lc.holdout_start`; `cut = holdout_start - EMBARGO` (oggi 2026-06-20 e 2026-06-18). `holdout_start_from` NON
   viene chiamata.
3. `fees = lc.fees`, `costs = lc.costs`, non dal probe ne' da `AppConfig`: stesso modello di costo del pool attuale.
4. Spread. Con `label_config`: `lc.spread_bps[p]`; un prodotto assente -> exit di errore (serve alla regressione R0). Con
   `probe_series`: `max(LABEL_SPREAD_FLOOR_BPS, stats[p].median_bps)` per OGNI prodotto, major compresi; un prodotto assente ->
   errore.
5. Barre: un prodotto alla volta. `load_bars(root, p)` sulla storia intera COME OGGI, poi
   `build_product(..., cut=cut, cfg_ref=LabelConfig(horizon_min=max(HORIZON_DAYS) * 1440))`, poi il frame si libera. NON passare
   `end` a `load_bars`: cambierebbe la coda di barre piatte prima del cut e quindi il detrend, e R0 non tornerebbe. Memoria stimata
   [D] ~150 MB per prodotto (2 serie x 6 array x ~1,4 M minuti), quindi ~2 GB per P2. Sonnet misura su P0 e segnala se la RAM non
   basta, senza ottimizzare in anticipo.
6. Appartenenza: `months = member_months(scan[p] ...)`, poi
   `p = dataclasses.replace(p, samples=p.samples[membership_at(p.ts_close[p.samples], months)])`. `build_product` non cambia.
7. Valutazione sulla combinazione congelata:
   - `r = evaluate(lc.cfg, products, fees, costs, app.risk.min_net_edge_bps)`;
   - `dg = diagnostics(r, [r], [r], products, fees, costs, ...)`;
   - B6 per prodotto con `verdict(r, dg.jack_max, ...)`;
   - `n_eff_pit` (K6) con la nuova `pooled_n_eff_pit(raws, rho, months_by_product)`;
   - la nuova `pairwise_rho_matrix(raws) -> dict[str, tuple[float, int]]`, con la stessa regola di allineamento di `pairwise_rho`,
     che resta invariata e da' lo scalare;
   - con `--with-grid`, anche `grid()` e `choose()`, solo come diagnostica.
8. Scrive SOLO `<out-dir>/feasibility_universe_<pool>.md` e `<out-dir>/feasibility_universe_<pool>.json`. Non scrive mai un
   `label_config` (in nessun percorso), mai `feasibility.md`, nulla sotto `data/datasets`. Exit 0 se il calcolo riesce: il verdetto
   e' solo informativo.

JSON (`"schema": "feasibility_universe.v1"`):
- `pool`, `report_only: true`, `label_config_sha256`, `holdout_start`, `cut`, `combination` (campi di `lc.cfg`), `spread_source`,
  `spreads`, `universe_rule`, `products`;
- `membership{p: {first_member_month, member_months, months_total}}`;
- `pool{lift_0, lift_raw, lift_rw, p_be, p_hist_0, n_eff, n_eff_pit, rho_detr, rho_raw, jack_max}`;
- `per_product{p: {n_p, se, p_hist_0, p_be, lift_0, b6_limit, b6_ok, lift_raw, lift_rw, fill_rate, p_vertical, dur_median_h,
  atr6h_median_bps, mu_year_diag}}`;
- `rho_pairs{"A|B": {rho, n_common}}`, `grid_choice` (null senza `--with-grid`), `generated_at`.
`mu_year_diag` e' solo diagnostica: nessuna regola la legge.

Test in `tests/test_feasibility.py`, su barre sintetiche come i test esistenti:
- `test_report_only_never_writes_label_config`: sha256 dei byte e mtime di `label_config.json` identici prima e dopo, anche quando
  il verdetto sarebbe GO; `feasibility.md` non viene creato; gli unici file scritti sono i due `feasibility_universe_<pool>.*`;
- `test_report_only_uses_frozen_holdout`: le barre vanno oltre `last_close` del label_config e `holdout_start_from` e'
  monkeypatchata per sollevare un errore; l'`holdout_start` del JSON e' quello del label_config;
- `test_report_only_ignores_bars_after_cut`: barre arbitrarie aggiunte dopo `cut` -> metriche del JSON identiche (l'holdout non
  viene mai visto);
- `test_report_only_reproduces_full_run`: un run normale GO scrive un label_config; poi `--report-only --spread-source label_config`
  con appartenenza sempre vera da' `lift_0` e `n_eff` uguali a `raw["feasibility"]["detr"]["lift"]` e `raw["feasibility"]["n_eff"]`
  (tolleranza 1e-9), e `n_eff_pit == n_eff`;
- `test_report_only_rejects_label_writing_flags` e `test_report_only_requires_label_config`;
- `test_membership_mask_drops_non_member_samples` e `test_pooled_n_eff_pit_equals_pooled_when_all_members`.

1.5 Studio dello slippage degli stop: `training/stop_slippage.py`, in sola lettura su registrazioni e barre (specifica in 3.2). La
formula sta in un helper puro, `realized_stop_extra_bps(ref_px, vwap, spread_bps, slip_bps) -> float`, in `modules/risk/costs.py`:
cosi' la stessa definizione servira' anche per gli stop reali del live.

1.6 `training/universe_report.py`, con due sottocomandi:
- `filter --scan ... --spread-stats ... --stops data/reports/stop_slippage.json --at <cut>` -> `data/reports/universe.{md,json}`.
  Per ogni prodotto: esito U1-U7 (codici di `static_reasons`), condizione (d) (3.1), volume U4 alla data, mesi da membro nella
  finestra di selezione. I pool si calcolano per regola: P0 = prodotti del `label_config`; P1 = P0 + XRP-EUR, se XRP passa; P2 = P0 +
  tutti i prodotti che passano, ordinati con `rank_by_volume`.
- `adopt --p0 <json> --p1 <json> --p2 <json> [--p2-pruned <json>]` -> `data/reports/universe_adoption.md`. Applica 3.1 tramite una
  funzione pura `adoption(...) -> AdoptionResult` con esito `ADOPT`, `NOT_ADOPTED` oppure `NEEDS_PRUNED_RUN` (con l'elenco dei
  prodotti di P2_pruned). Con `--p2-pruned` non si pota piu' nulla.
- Test sui confini: N_eff esattamente +30%; lift_0 esattamente +0,01; mediana x 10,0 contro 10,01; `n_events` 9 contro 10; un
  prodotto che fallisce B6 viene tolto una sola volta; se P1 e P2_pruned passano entrambi si sceglie P2_pruned.

1.7 Registry e configurazione: NIENTE cambia fino ad A6.
- Il report-only non usa ne' il registry ne' la config dei prodotti: riceve `--products`.
- In A6 un prodotto entra in tre posti, nello stesso commit: `train.yml products`, `market.products` di AppConfig (e
  `config/config.example.yml`) e il nuovo `label_config` (`spread_bps`). Al runtime `SpotRegistry.load(exchange, market.products)`
  verifica ogni prodotto all'avvio con `_meta_from_raw` (U1 compreso). Un prodotto che non passa da' `SpotViolation` ed exit 4: non
  viene mai scartato in silenzio.
- Controlli da aggiungere in A6 (fail closed): in `build_dataset`, `set(cfg.products) == set(label.spread_bps)`, altrimenti
  `DatasetError` (oggi un train.yml con meno prodotti del label_config passa); al runtime, `set(market.products) <=
  set(label.spread_bps)`, altrimenti `ConfigError` (exit 2).
- Sempre in A6, la stessa `member_months` filtra i sample di `build_dataset`, le righe di decisione del backtest e, al runtime,
  l'ingresso (HOLD `universe_inactive`, registrato nel DecisionRecord con `universe_rule`). E' un gate deterministico fuori dallo
  stato (lo stato v1 e' congelato), con la stessa funzione ovunque (T1).

1.8 `label_config` e sha256 quando il pool cambia.
- Il report-only non tocca il `label_config`: lo sha256 e `holdout_start` restano quelli di oggi.
- Lo sha256 include `spread_bps` per prodotto. Aggiungere o togliere un prodotto, o cambiarne lo spread, produce uno sha256 nuovo.
  Il dataset (manifest), il model card, la calibrazione e `policy.json` (`label_config_sha256`) del modello attuale non sono piu'
  compatibili con il nuovo file, e il sistema li rifiuta (fail closed).
- In A6 il nuovo `label_config` si scrive in un percorso NUOVO (`data/datasets/label_config_<pool>_<YYYYMMDD>.json`) e
  `train.yml label_config` punta a quello. Il file attuale resta, cosi' il modello P0 resta riproducibile. Mai sovrascrivere
  `data/datasets/label_config.json`.
- `holdout_start` in A6: la feasibility normale lo ricalcola da `last_close`, e cosi' sposterebbe l'holdout sopra la finestra gia'
  usata in P1c, ottenendo una finestra in parte gia' vista. E' vietato. Serve una decisione dell'utente (sez. 5, punto 4); il flag
  lo specifico quando la decisione c'e'.

1.9 Artefatti da rigenerare se A5 adotta un pool (A6):

| Artefatto | Azione |
|---|---|
| `label_config_<pool>_<YYYYMMDD>.json` | nuova feasibility completa (qui R-a3 e' decisiva), holdout secondo la decisione dell'utente |
| `train.yml products`, `market.products`, `config.example.yml` | aggiornati insieme al label_config |
| probe con chiavi (`coinbase_probe.json`) | rieseguito con i nuovi prodotti (raw per il registry, fee) |
| appartenenza `UNIVERSE_V1` | codice in `build_dataset`, backtest e runtime (1.7) |
| dataset | nuovo run (righe ~x K/3) |
| booster, calibrazione, model card | nuova versione `baseline-lgbm@<data>.<n>` |
| `policy.json`, `tune_report`, `evaluation.json`, `regime_deciles.md` | rigenerati |
| `data/backtest_cache` | cancellato |
| QUESTIONS | nuova versione solo se R-a3 sceglie barriere o orizzonte diversi |
| stato `chronos.state.v1`, FLAT_COLUMNS, COST_V1 | invariati |
| spec | sez. 0.4/0.5 (universo, prodotti), 3.1.3 (report-only, universo) |

### 2. Ordine dei passi e tempi

Fatti verificati sul codice per la convivenza con il soak (48 h; scrive `data/bars` e `data/ws_recordings` per BTC/ETH/SOL fino a
domattina):
- `BarStore` scrive solo in `product=<id>/year=<y>/month=<m>.parquet`. Un altro processo che scrive ALTRI prodotti nello stesso
  root non tocca i file del soak.
- `BarStore.write` pero' legge, unisce e sostituisce il file senza lock: due processi sullo STESSO prodotto possono perdere righe.
  Regola: un solo scrittore per prodotto alla volta. Il backfill va lanciato con `--products` esplicito: senza, usa
  `market.products` (BTC/ETH/SOL) e si scontra con il soak.
- Un secondo `recorder record` con l'`--out` di default scriverebbe in `data/ws_recordings/<giorno>/`. `verify` unisce tutti i file
  del giorno e mescolerebbe i `sequence_num` di due connessioni (falsi reset e falsi buchi). Il `--report` di default
  (`data/reports/p0_soak.json`) sovrascriverebbe il report del soak. Quindi un secondo recorder parte solo DOPO il soak, con `--out`
  e `--report` dedicati.

| Quando | Passo | Codice | Costo |
|---|---|---|---|
| Oggi, subito | A0: commit di questo file (la data del commit e' la pre-registrazione). Il quant conferma U7b (K4) prima che il report del backfill venga letto | no | 30 min |
| Oggi, in background | backfill 1m: `python -m modules.market.backfill --products XRP-EUR LINK-EUR LTC-EUR ADA-EUR ALGO-EUR BCH-EUR DOGE-EUR DOT-EUR XLM-EUR AVAX-EUR UNI-EUR --start 2023-10-01 --rate 2` | no | ~9 h di macchina [D: ~20 min a 5 req/s per prodotto -> ~6.000 richieste ciascuno]; `--rate 2` lascia spazio alle richieste REST del soak |
| Oggi | `spread_probe.py` (1.3): primo giro subito, poi 4 al giorno sui 14 prodotti (K2) | piccolo, ~1-2 h | minuti al giorno |
| Oggi o domani | `universe_scan.py` (1.3), una sola esecuzione | piccolo, ~1 h | minuti |
| Domattina, a soak finito (report scritto) e backfill concluso | recorder dei 14 prodotti: `python -m modules.dryrun.recorder record --hours 672 --products <14> --out data/ws_recordings_universe --report data/reports/universe_recorder.json`. Con `--data-dir data` il backfill iniziale riprende dall'ultimo minuto salvato; i major tornano ad avere un solo scrittore | no | 28 giorni di macchina accesa. Disco [?]: prima di partire si stima da `recorder.bytes_in` del soak; la retention di 30 giorni basta |
| Entro ~2 settimane, dopo i passi P1c in corso | 1.1, 1.2, 1.4, 1.5, 1.6 con i test; `pytest`, `mypy --strict`, `ruff`. Appena il codice e' pronto: R0 = P0 con `--spread-source label_config` (deve dare lift_0 0,074 e N_eff 665 come il label_config) | si' | ~2 giorni di Sonnet; non e' sul percorso critico, che dipende dal calendario dei dati |
| Data fissa: registrazione giorno 28 (~2026-11-17) | `spread_probe --summarize` (~29 giorni); `stop_slippage` sui 14; `universe_report filter` (A3 compreso); A4: report-only di P0, P1, P2 con `probe_series`; eventuale P2_pruned; A5: `universe_report adopt`, una sola volta | no | minuti per pool sulla combinazione congelata; ore se si aggiunge `--with-grid` [?] |
| Solo se A5 adotta e l'utente ha deciso (sez. 5) | A6 (1.9) | si' | giorni |

Il piano P1c sul pool attuale (D1, R2, re-tune, evaluate con B0/B2) va avanti in parallelo e non dipende da nulla di tutto questo.

### 3. Valutazione del piano A0-A5

Concordo con A0-A5 nel merito, con le correzioni K1-K8. Le principali, con il motivo:
- K1: la misura proposta vale 0 per costruzione;
- K3: U3 calcolato sullo store escluderebbe i major;
- K4: U7 misura la liquidita', non gli errori, e potrebbe decidere tutto per un motivo non voluto;
- K5-K7: meno gradi di liberta' e un solo confronto pre-registrato, eseguito una volta sola.
Le soglie (a)(b)(c) restano quelle fissate in P1c, prima di questi dati. Non le cambio.

3.1 Regola di adozione (pre-registrata; si applica con `adopt`, una sola volta):
- Contesto comune a tutti i pool: combinazione congelata del `label_config` attuale; serie detr; finestra di selezione `t < cut`;
  appartenenza UNIVERSE_V1; spread `probe_series` per tutti i prodotti; fee e costi del `label_config`. P0 si ricalcola con lo
  stesso codice e la stessa regola di spread, non si prendono i valori del vecchio report.
- (d), applicata da `filter` prima di formare P2: per ogni prodotto nuovo, `n_events >= 10` e mediana di `x_real <= 10` bps
  (3.2). Altrimenti il prodotto resta fuori.
- (a) Per ogni prodotto nuovo p: `lift_0,p <= 0,10 + 2 SE_p` (B6, `verdict` esistente). Chi fallisce viene tolto una sola volta:
  P2 -> P2_pruned, e su P2_pruned si fa un nuovo run report-only.
- (b) `N_eff_pit(pool) >= 1,30 x N_eff_pit(P0)`.
- (c) `lift_0(pool) <= lift_0(P0) + 0,01`.
- Se passano sia P1 sia P2 (o P2_pruned), si adotta il pool piu' grande. Da solo, P1 quasi certamente non passa (b) (quant).
- Un `lift_0` migliore non e' mai, da solo, un motivo per adottare un prodotto (quant).

3.2 Come si misura lo slippage degli stop (`training/stop_slippage.py`).
- Dati: le registrazioni continue `level2` + `market_trades` dei 14 prodotti (`data/ws_recordings_universe`; entrambi i canali sono
  gia' nei `CHANNELS` registrati), piu' le barre 1m dello store. Il dry run del bot da solo non basta, perche' fa pochi trade. Lo
  studio usa stop virtuali e lo stesso codice di esecuzione simulata del dry run (T1): `parse_message`, `LocalOrderBook`,
  `TickFillModel.stop_triggered` e il walk dei bid del BracketSimulator. Niente reimplementazioni.
- Popolazione degli eventi, la stessa delle label. A ogni ora della griglia (come `hourly_grid`) in cui il prodotto ha l'ATR 6h, si
  arma un bracket virtuale: `stop = floor_to(close * (1 - k_sl * ATR_6h / close), quote_increment)`, TP a `k_tp * ATR`, durata H;
  k e H vengono dal `label_config`. Il bracket si disarma al TP (un trade sopra il TP), a H, oppure alla fine della registrazione
  (censura, nessun evento). L'ingresso si assume al close: si misura solo la gamba di stop.
- Trigger: il primo trade di `market_trades` per cui `stop_triggered(price, stop)` e' vero, al tempo tau.
- Liquidazione: stato del book a `tau + latency_ms` (`FillModelParams.latency_ms`, 300); walk dei bid per
  `q = floor_to(15 EUR / stop, base_increment)`; VWAP.
- Misura: `ref = min(stop, open della barra 1m che contiene tau)`;
  `x_real = (ref - VWAP) / ref * 1e4 - s_label / 2 - slip_bps`, con `s_label = max(3, mediana del probe)` e `slip_bps` del
  `label_config`. E' esattamente la `x` della formula di stop del modello a barre (`min(lower, open) * (1 - (s/2 + slip + x))`),
  quindi si confronta direttamente con i 10 bps.
- Eventi validi. Si escludono, e si contano a parte, i trigger dentro un intervallo danneggiato (`on_damage`, buco di sequenza, book
  stantio fino al nuovo snapshot). Gli stop scattati nello stesso minuto contano come un solo evento: `n_events` = minuti distinti
  con almeno un trigger, e `x_real` dell'evento = mediana degli stop di quel minuto.
- Output `data/reports/stop_slippage.{md,json}`: per prodotto `n_events`, `n_excluded_damaged`, mediana, p75, p90 e p95 di `x_real`,
  e le stesse statistiche sui minuti di mercato veloce (|rendimento 1m| oltre il p95 del prodotto nella registrazione). La regola (d)
  usa solo la mediana; p95 e mercato veloce sono diagnostica per il quant.
- Numero di eventi atteso [D, grossolano]: con un bracket armato ogni ora e lo stop colpito in circa il 60% dei casi entro H, ci si
  aspettano decine di minuti distinti con trigger per prodotto in 28 giorni. Un prodotto con meno di 10 eventi non ha (d) misurata e
  resta fuori (fail closed).
- Test `tests/test_stop_slippage.py`, con messaggi sintetici:
  - trigger al bid senza movimento -> `x_real = -s/2 - slip`;
  - un aggiornamento del book dentro la latenza toglie il primo livello -> VWAP sul secondo livello, valore atteso esatto;
  - trade stampato sotto lo stop (salto) -> la differenza entra in `x_real`;
  - barra che apre sotto lo stop -> `ref = open`;
  - trigger in un intervallo danneggiato -> escluso e contato;
  - due stop nello stesso minuto -> un solo evento;
  - la funzione di trigger usata e' `TickFillModel.stop_triggered` (spy).
- Limite dichiarato: e' una stima dal book pubblico, senza la latenza reale del motore, la liquidita' nascosta o la coda. La conferma
  arriva solo con ordini reali: allora si registrano `stop_trigger_price`, il fill reale e l'open della barra, e si applica la stessa
  `realized_stop_extra_bps`. [?] Va verificato anche come Coinbase esegue la gamba di stop di `trigger_bracket_gtc` dopo il trigger
  (a mercato o con un limite). Se e' un limite, lo slippage ha un tetto, ma lo stop puo' non riempirsi. Va chiarito nella
  documentazione o misurato prima del live; questo studio non cambia.

### 4. Raccomandazione

1. Tenere BTC/ETH/SOL come nucleo e come pool di riferimento P0. Oggi nessun cambio di config, `label_config` o modello.
2. Misurare, non scegliere: probe dei 14 prodotti EUR ammessi per regola (K2), backfill, scansione giornaliera, 28 giorni di registrazione.
3. Testare P1 (+XRP) e P2 (+ fascia 2 ammessa) solo con `--report-only` e l'holdout congelato al 2026-06-20.
4. Adottare un pool solo se passa (a)(b)(c)(d) alla data fissa (~2026-11-17), con una sola valutazione.
5. Lo slippage degli stop si misura con stop virtuali sulle registrazioni, non con foto del book. I major fanno da controllo (K8).
6. Il codice nuovo e' piccolo e isolato. L'unico file del runtime che cambia comportamento e' l'allowlist U1, che diventa piu' stretta.
7. Il collo di bottiglia resta il segnale (AUC 0,55): l'universo abbassa l'asticella di ~0,015-0,025, ma non crea un edge.
8. MAI scegliere i prodotti per rendimento, volatilita' o lift passati. Prima di A6, mai rieseguire la feasibility senza `--report-only`.

Per chi non e' tecnico:
> Teniamo Bitcoin, Ethereum e Solana. Solana non e' una stablecoin: e' una criptovaluta che oscilla molto.
> Per quattro settimane il computer osserva altre 11 monete in euro, senza comprare nulla: quanto costa comprarle e venderle, e
> cosa succede quando uno stop scatta durante una discesa veloce.
> Le monete si scelgono con regole fissate oggi (quanto vengono scambiate e quanto costano), mai guardando quanto hanno reso in passato.
> Verso meta' novembre una regola gia' scritta dira' se allargare la lista conviene davvero.
> Piu' monete possono ridurre un po' il peso dei costi, ma da sole non fanno guadagnare: serve comunque un segnale migliore.

### 5. Decisioni che spettano all'utente (qui non vengono prese)

1. Approvare il piano e la pre-registrazione: soglie U1-U7, correzioni K1-K8, regola 3.1 e data fissa di A4/A5. Il file va
   committato oggi, prima di qualunque nuovo numero.
2. Tenere la macchina accesa ~4 settimane, per il probe (4 volte al giorno) e per il recorder dei 14 prodotti, dopo aver stimato il
   disco sul soak.
3. Se A6 (nuovo pool -> nuovo `label_config`, dataset e modello) dipende solo da A5, oppure anche dal verdetto GE del piano P1c,
   come raccomandato in P1c (il paniere si apre solo con GE INCONCLUSO e media OOS > 0).
4. Holdout per un eventuale nuovo pool:
   (i) riusare la finestra congelata dal 2026-06-20, gia' consultata in P1c (AUC holdout 0,553), dichiarandolo; oppure
   (ii) un holdout in avanti, con soli dati successivi alla decisione (120 giorni: ~marzo 2027).
   Il ricalcolo automatico della feasibility normale e' escluso in entrambi i casi.
5. Se il controllo K8 trova `x` > 10 bps su un major: aprire un conflitto separato sul modello di costo del pool attuale.
