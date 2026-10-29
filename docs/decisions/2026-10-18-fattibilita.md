# CONFLITTO - P1: esito dello studio di fattibilita' (training/feasibility.py)

Autore: Sviluppatore (Sonnet). Data: 2026-10-18. File coinvolti: `training/feasibility.py`, `training/labeling.py`,
`docs/specs/chronos-system1-spec.md` (sez. 0.4, 3.1.3), `data/reports/feasibility.md`.

## Esito dello studio (storico reale 2023-10-01 .. 2026-10-18, fee REALI del probe: maker 25 / taker 50 bps, slip 5, x 10)

Griglia della spec (atr_tf 6h/1d x k_tp 3/4/5 x k_sl 1.5/2 x maker/taker x H 5/10/15 giorni), 72 combinazioni, campioni = griglia
oraria, 3 prodotti in pool (spread del probe: BTC 1.09, ETH 1.59, SOL 2.17 bps). Tutte le combinazioni passano fee floor (100%) e fill
maker (0.90). La regola di scelta della spec (lift richiesto minimo) sceglie:

    atr_tf = 6h, k_tp/k_sl = 3/2, maker, H = 15 giorni
    p_breakeven mediano 0.465, p_hist (freq. storica di UPPER sui fill) 0.410, lift richiesto +0.055 (limite 0.10) -> GO
    lift vs random walk +0.065, p_vertical 0.04, indipendenti/anno/prodotto ~22
    per prodotto: BTC +0.067, ETH +0.056, SOL +0.049

I default della sez. 0.4 (1d, 4/2, maker, H=10) NON sono sostenibili con la definizione di lift della sez. 3.1.3:
p_breakeven 0.360, p_hist 0.104 -> lift +0.257, perche' con ATR 1d e k_tp = 4 il 60% dei trade scade sulla barriera verticale
(p_vertical 0.60) e UPPER avviene solo nel 10% dei casi. (Con la definizione "contro il random walk" della tabella 0.4 il lift
sarebbe +0.027: le due definizioni divergono di molto quando p_vertical e' alto.)

Robustezza (combinazione scelta, frequenza di UPPER per semestre sui fill): 2023H2 0.59, 2024H1 0.38, 2024H2 0.45, 2025H1 0.34,
2025H2 0.40, 2026H1 0.36, 2026H2 0.44. Entrare SEMPRE (nessun modello) ha net_ret medio -43.6 bps per trade sull'intero storico.
Il modello deve quindi portare P(UPPER) da ~0.41 a >= 0.465 in modo stabile fra regimi in cui il tasso base oscilla 0.34-0.59.
Finestre indipendenti: ~22/anno/prodotto, ~200 su 3 anni e 3 prodotti: errore standard di una frequenza di successo ~3.5 punti,
cioe' l'edge richiesto (+5.5 punti) e' ~1.6 sigma: il backtest da solo avra' poca potenza statistica.

## Dubbi

D1 (finanziario, trader-quant). La regola della spec sceglie 6h / 3-2 / 15 giorni. Tutte le migliori combinazioni stanno al bordo
della griglia (H = 15 e' il massimo; il lift scende monotonicamente con H). (a) Il bordo indica che la griglia e' troncata:
va estesa (H = 20, 30 giorni; atr_tf = 1h non entra per il fee floor, vedi sez. 0.4)? (b) Una barriera verticale a 15 giorni con
trade lunghi e' coerente con il profilo del sistema (swing) e con C10 (micro-capitale: capitale bloccato fino a 15 giorni)?
(c) `p_hist` e' una frequenza in-sample che incorpora il drift del periodo (BTC 25.5k -> 72.7k EUR, ETH 1580 -> 2145, SOL 20 -> 92):
e' accettabile come criterio GO/NO-GO, oppure serve un criterio robusto (ad esempio lift calcolato sul peggior semestre / anno,
o su un periodo di verifica non usato per scegliere la combinazione) per non scegliere i parametri su un mercato rialzista?
(d) Con ~200 finestre indipendenti, quale evidenza minima pretende il quant dal backtest walk-forward prima di dichiarare un edge
(la spec chiede PnL netto > 0 su >= 3/5 fold e sull'holdout)?

D2 (software, architetto). Conferma della scelta e delle interpretazioni implementate in `labeling.py`:
1. `maker_first_bar = 2`: la spec scrive `k in (t+1, t+T_in]`, intervallo aperto a sinistra. Ho letto: la barra t+1 NON puo' riempire
   (la decisione e' nota solo alla fine della barra t; l'ordine reale arriva a t+S con S = 120 s, quindi anche la barra t+2 e' gia'
   trascorsa all'invio). Il limite resta `close[t] * (1 - s/2)` come da spec. Il parametro e' in `LabelConfig` (e nello sha256).
   Confermi 2, oppure 3 (per allineare a S=120 s), oppure 1?
2. Uscita SL: la tabella 5.1.3 dice "stop * (1 - stop_extra_slip_bps)" mentre 3.1.3 e `CostModel.c_sl_out` includono anche `s/2 + slip`.
   Ho usato la formula di 3.1.3: `min(lower, open) * (1 - (s/2 + slip + x)/1e4)` (piu' prudente e coerente col CostModel).
3. Stessa barra con entrambe le barriere: 3.1.3 scrive `low <= lower and high >= upper -> LOWER` ma poi `high > upper` per UPPER.
   Ho implementato: se `low <= lower` -> LOWER (qualsiasi `high`); UPPER solo se `high > upper` strettamente (un tocco esatto non
   riempie il limit di vendita, come in 5.1.3). Con `high == upper` e `low > lower` non succede nulla.
4. La barra di fill non viene scansionata per le barriere (`(t_fill, vertical]`), anche per l'ingresso taker (fill alla barra t+1).
5. Esiti non risolvibili (finestra oltre l'ultima barra, ATR mancante) = `UNRESOLVED`, non sono label.
6. Selezione sul pool dei tre prodotti (un solo `label_config` per tutti, come la model card); il dettaglio per prodotto e' nel
   report. Va bene, o serve che ogni prodotto abbia lift <= 0.10 (oggi lo soddisfano tutti)?
7. `spread` costante per prodotto (mediana dal probe: BTC 1.09 bps, ETH 1.59, SOL 2.17; la spec 0.4 riportava 2.9/2.0/3.3 misurati il
   17-ott, quindi ha oscillazione); la tabella oraria arrivera' con il recorder. Il probe usa uno snapshot istantaneo: va bene come
   valore del `label_config` (e quindi dell'O1: skew spread train vs runtime) o preferisci un valore conservativo fisso (es. 3 bps)?
8. Aggiornamento della spec: sez. 0.4 "Decisioni v1" e il blocco "Barriere" vanno sostituiti con l'esito dello studio (6h, 3/2, maker,
   H = 15 d, `entry_timeout` 30 min) e il valore `label_config.json` va indicato come fonte di verita'. Anche i riferimenti a
   `ATR_14_1d` (sez. 3.1.3, 4.x, esempio 0.3) usano `atr_tf` del `label_config`; l'esempio numerico della 0.3 resta valido come esempio.

## Cosa serve

1. `trader-quant`: risposte a D1 (a)-(d) con una regola finanziaria chiara (scrivere sotto, sezione "Risposta trader-quant").
2. `architetto-opus`: leggere l'esito + la risposta del quant, decidere D2 1-8 e D1 sul piano software (griglia estesa? criterio di
   robustezza? aggiornamento spec), scrivendo le specifiche finali sotto, sezione "Risoluzione architetto".

## Risposta trader-quant

Autore: trader-quant. Data: 2026-10-18. Ambito: solo D1. I valori marcati "(stima)" sono analitici (random walk su log-prezzo,
fee reali 25/50) e vanno sostituiti dai calcoli K1-K6 elencati in fondo: non sono misure. Vincoli invariati: spot, long-only,
copertura 100%, rischio massimo di un ordine = sua riserva.

### Premessa: due difetti di misura che toccano tutte e quattro le risposte

P1. `indep/yr` del report e' `365 / H x fill` (21.9 = 365/15 x 0.90; 32.8 = 365/10 x 0.90): conta la barriera verticale, non la
durata reale. Con p_vertical = 0.04 quasi tutti i trade escono molto prima di H. Tempo d'uscita atteso di un random walk con
barriere +a / -b: `E[tau] = a * b / sigma^2`. Con `ATR_6h ~ 1.5 sigma_6h` (stima): `E[tau] ~ k_tp * k_sl * 2.25` barre 6h
= 13.5 barre ~ 3.4 giorni per 3/2 (~2.5 giorni per 3/1.5). Definizione corretta: `N_p` = somma, sui sample con fill del prodotto
p, dell'unicita' media (Lopez de Prado, `uniqueness_weights`) calcolata su `[t, exit]` REALE (non `[t, t+H]`); `indep/yr = N_p / anni`.

P2. I tre prodotti non sono indipendenti. `N_eff = (N_BTC + N_ETH + N_SOL) / (1 + (K-1) * rho)`, K = 3, rho = correlazione media a
coppie degli indicatori UPPER (1/0) a stesso t (sample con fill in entrambi i prodotti). Con rho = 0.6 (stima) il pool vale 1.36
prodotti, non 3. Errore standard di una frequenza: `SE(p) = sqrt(p (1-p) / N_eff)`.

P1 aumenta N di ~3-4x, P2 lo riduce di ~2x: il "1.6 sigma" del report non e' affidabile in nessuna delle due direzioni finche'
non si fanno K1-K2.

### D1 (a) Bordo della griglia: NON estendere H

Il lift monotono in H non e' un ottimo troncato ma una saturazione con due componenti:
1. VERTICAL che si risolvono: a H = 15 resta p_vertical = 0.04, quindi estendere H puo' alzare p_hist al massimo di
   ~0.04 x 0.5 = +0.02 (lift >= ~0.035). Non cambia il verdetto.
2. Drift accumulato: piu' esposizione in un campione rialzista = piu' UPPER. Prova dal report: `lift_rw` (indipendente da H e dal
   drift) e' identico per H = 5/10/15 (0.065 per 6h 3/2 maker); `p_breakeven` non dipende da H. Tutta la dipendenza da H passa da
   p_hist, cioe' da verticali + drift. La regola "lift minimo" premia strutturalmente l'H piu' lungo: e' un bias del criterio.

1d: li' la griglia e' davvero troncata (p_vertical 0.29-0.81 perche' `E[tau] ~ 13.5 giorni` con ATR 1d, stima) e servirebbe
`H ~ 4 x E[tau] ~ 60 giorni`. Respinto: (i) l'holdout di 120 giorni conterrebbe <= 2 finestre indipendenti per prodotto;
(ii) purge + embargo di ~62 giorni a ogni confine di fold (fold ~160 giorni) eliminano ~40% dei dati; (iii) la sensibilita' al
drift cresce con la larghezza delle barriere (`dP ~ nu * a * b / (a + b)`, `nu = mu / sigma^2`): barriere 2x larghe = bias 2x.
1h resta escluso dal fee floor (sez. 0.4).

Regole (cambiano la griglia e la regola di scelta):
- R-a1. `H_max = 15 giorni`, vincolo duro: `H <= durata_holdout / 8 = 120 / 8`.
- R-a2. Nuova condizione di eleggibilita' `p_vertical <= 0.05` (oltre a fee floor >= 80% e fill >= 50%). Per ogni
  (atr_tf, k_tp, k_sl, entry) si tiene il piu' piccolo H della griglia che la soddisfa: oltre quel punto H aggiunge solo drift e
  capitale fermo. Effetto: 6h 3/2 maker -> H = 15 (0.04), H = 10 escluso (0.10); tutte le combinazioni 1d escluse.
- R-a3. Pareggio statistico: combinazioni con lift (lift_0 di R-c2) entro 0.01 dal minimo sono equivalenti (le prime tre del
  report differiscono di 0.003, ~0.1 SE). Fra queste si sceglie quella con `N_eff` massimo (P1-P2): massimizza la potenza del
  walk-forward (d). Possibile cambio della scelta da 3/2 a 3/1.5 (E[tau] -25%, p_vertical 0.02, lift +0.058): da decidere con K1-K3.
- R-a4. H = 20 giorni solo come diagnostica (non eleggibile) per 6h 3/2 e 3/1.5: la saturazione e' confermata se
  `lift(20) >= lift(15) - 0.02`; se scende di piu', p_hist e' dominato dal drift e il report lo deve segnalare.

### D1 (b) H = 15 giorni e C10: coerente

- Il capitale resta impegnato per la durata reale, non per H: `E[tau] ~ 3.4 giorni` (stima, K1). H = 15 e' solo il tetto della
  coda (4% dei trade, net medio +80 bps). TP ~ 3 x ATR_6h = 3.6-6.7%, SL ~ 2 x ATR_6h = 2.4-4.4% (ATR_6h 120-222 bps della 0.4):
  profilo swing multi-giorno, piu' corto del default 1d (~8 giorni).
- Capitale fermo = costo opportunita', non rischio. Con C11 la durata non cambia il limite di perdita: perdita pianificata =
  `size * (entry - SL) + costi`; perdita estrema (gap oltre lo stop, prezzo -> 0) = notional dell'ordine, mai saldo negativo.
- Sizing implicito (4.1.2, `risk_per_trade = 0.005`, `scale_p = m_reg = 1`): `notional / equity = 0.005 / ((2 A + cost_sl) / 1e4)`,
  con fee reali `cost_sl = 25 + 50 + s/2 + 5 + 10 ~ 91 bps` e `cost_tp = 50 bps`. A = 120 bps -> 15.1%; A = 170 -> 11.6%;
  A = 222 -> 9.5%. Con una sola posizione per (book, prodotto) l'esposizione lorda e' <= ~45% dell'equity e il saldo libero resta
  >= ~55%: i 15 giorni non bloccano la copertura dei nuovi ordini. Book da 100 EUR: notional 10-15 EUR per trade, sopra
  `quote_min_size` = 1 EUR.
- I tre prodotti sono un unico rischio (correlati): la perdita aggregata a SL delle posizioni aperte e' <= 3 x 0.5% = 1.5%
  dell'equity (piu' lo slip dello stop). Compatibile con C10, nessuna modifica di griglia.
- Il vincolo di frequenza reale e' "una posizione per prodotto", non il capitale: trade max per prodotto
  `~ 365 / E[tau] x fill ~ 95 / anno` (stima, K1).

### D1 (c) p_hist in-sample non basta come GO/NO-GO

Contributo del drift (random walk con drift sul log-prezzo, approssimazione al primo ordine):
`p_hist ~ k_sl / (k_tp + k_sl) + nu * a * b / (a + b)`, con `a = k_tp * A`, `b = k_sl * A` (log), `nu = mu / sigma^2`.
Con mu annuo realizzato BTC 0.35, ETH 0.10, SOL 0.51 (dai prezzi del conflitto) e sigma annuo 0.50 / 0.65 / 0.80 (stima):
drift +0.020 BTC, +0.005 ETH, +0.021 SOL. Il lift senza drift sarebbe ~0.07 invece di 0.055 (stima): probabilmente ancora GO,
ma i parametri vanno scelti senza drift.

La regola "peggior semestre / anno" e' respinta come criterio: un semestre ha N_eff ~15-60 (SE 0.06-0.13) e il minimo atteso di 7
semestri e' `media - 1.35 SE` per solo rumore. Oggi darebbe lift 0.465 - 0.34 = 0.125 -> NO-GO per effetto del rumore, e
respingerebbe quasi ogni combinazione. Resta come diagnostica.

Regole (cambiano il criterio GO/NO-GO):
- R-c1. Finestra di selezione: scelta e GO solo su `[inizio storico, inizio_holdout - T_in - H - embargo]` (holdout = ultimi 120
  giorni, sez. 3.1.4). Oggi lo studio usa tutto lo storico fino al 2026-10-18, holdout compreso: la scelta delle barriere vede
  l'holdout. Va ricalcolato.
- R-c2. Lift senza drift, sostituisce il lift grezzo nella regola di scelta e nel GO. Per prodotto, sulla finestra di selezione
  [T0, T1]: `mu_p = (ln C[T1] - ln C[T0]) / (T1 - T0)`; serie detrendizzata `X~(t) = X(t) * exp(-mu_p * (t - T0))` per X in
  {open, high, low, close}; ATR e label ricalcolati sulla serie detrendizzata; `p_hist_0` = frequenza UPPER sui fill;
  `lift_0 = p_breakeven - p_hist_0`. GO se `lift_0 <= 0.10` sul pool. Il lift grezzo resta nel report come informazione.
- R-c3. Jackknife per semestre: per ogni semestre s, `lift_0` ricalcolato escludendo i sample di s (mu_p invariato). GO richiede
  `max_s lift_0(-s) <= 0.10`. Protegge dal singolo regime (2023H2 = 0.59 su soli 3 mesi di rally) senza il bias del minimo.
- R-c4. Diagnostica senza soglia: p_hist e lift per semestre e anno con `SE_s = sqrt(p (1-p) / N_eff,s)`; test di omogeneita'
  `chi2 = sum_s (O_s - N_s * p_bar)^2 / (N_s * p_bar * (1 - p_bar))`, O_s = UPPER pesati per unicita', N_s = N_eff del semestre,
  gradi di liberta' S - 1. Se p-value < 0.05 il tasso base dipende dal regime: non e' NO-GO (il modello vede il regime), conferma
  che il criterio corretto e' R-c2.
- R-c5. Holdout, dopo il training e non per la scelta: lift grezzo sull'holdout; se `lift_holdout > 0.10 + 2 * SE_holdout`,
  stop per cambio di regime prima del gate G3.

GO della feasibility = (R-a1, R-a2 rispettati) AND `lift_0 <= 0.10` AND `max_s lift_0(-s) <= 0.10`, tutto su finestra R-c1.

### D1 (d) Evidenza minima dal walk-forward

"PnL > 0 su >= 3/5 fold" da solo non e' evidenza: con edge nullo e rumore simmetrico `P(>= 3/5 fold positivi) = 0.5`. Resta
condizione necessaria, non sufficiente.

Potenza. Per trade con fill: `net_ret ~ +W` con prob. p, `-L` con 1-p, `W = k_tp * A - cost_tp`, `L = k_sl * A + cost_sl`
(W + L = 5 A + 41 bps per 3/2 a fee reali). `mu_r ~ (p - p_be) (W + L)`, `sigma_r ~ sqrt(p (1-p)) (W + L) ~ 0.49 (W + L)`.
Trade indipendenti per rilevare `delta = p - p_be` con z_alpha = 2.0 (unilaterale 2.3%) e potenza 0.8:
`N_min = ((2.0 + 0.84) * sigma_r / mu_r)^2 ~ 1.94 / delta^2`: delta 0.05 -> 775; 0.08 -> 303; 0.10 -> 194; 0.15 -> 86.
Conseguenza: con ~100-200 trade effettivi fuori campione il backtest certifica solo un hit-rate >= p_be + 0.10..0.14
(~0.57-0.61 con p_be 0.465). Un edge reale di +0.03-0.05 non e' dimostrabile su 3 anni: si accumula solo nel dry run.

Criteri, tutti necessari, sui fold di test del walk-forward concatenati (holdout escluso, policy congelata da `tune_policy`):
- E1. Campione: `N_trade_OOS >= 150` (5 x 30, coerente con 3.1.6) e `N_eff_trade >= 100` (trade di prodotti diversi sovrapposti
  nel tempo ridotti con la formula di P2). Sotto: INCONCLUSO.
- E2. Significativita': media del net_ret per trade (bps, non pesata) con limite inferiore 2.5% del block bootstrap > 0.
  Bootstrap circolare a blocchi temporali di 15 giorni (~T_in + H), tutti i prodotti nello stesso blocco (cattura la correlazione),
  10 000 repliche. `t = media / SE_bootstrap`; la condizione equivale a t >= ~2.0. Anche il PnL in EUR (pesato per size) > 0.
- E3. Selezione contro il drift (test di permutazione): per fold e prodotto si estraggono a caso `n_trade(fold, prodotto)` ingressi
  fra i sample con fill e fee floor superato dello stesso fold e prodotto, con la stessa regola di non sovrapposizione per prodotto;
  10 000 repliche; p-value = quota di repliche con net_ret medio pooled >= quello del modello; richiesto `p <= 0.05`. Gli ingressi
  casuali catturano lo stesso drift del periodo: passa solo la capacita' di selezione, non il mercato rialzista.
- E4. Concentrazione: nessun fold contribuisce piu' del 50% del PnL netto OOS totale; >= 3/5 fold con PnL > 0 (regola della spec).
- E5. Holdout: PnL netto > 0 e coerenza con l'OOS `z = (m_h - m_oos) / sqrt(se_h^2 + se_oos^2) > -1.96` (SE da bootstrap come E2).
  L'holdout non deve essere significativo da solo (troppo corto).

Esito: E1-E5 ok -> GO (dry run, poi rampa R0). E1, E3, E4, E5 ok ma `1.0 <= t < 2.0` -> INCONCLUSO: solo dry run, zero capitale
live, finche' E2 su trade OOS + label del dry run non raggiunge t >= 2.0. Ogni altro caso -> NO-GO.

Nota collegata (raccomandazione per l'architetto, riguarda G1): ECE <= 0.05 e' dello stesso ordine del lift richiesto (0.055); un
p_edge sovrastimato di 0.05 fa passare l'EV gate a trade con p reale = p_be (EV nullo, meno le fee). Raccomando ECE <= 0.03 calcolato
sui soli bin con `p_edge >= p_edge_min`.

### Calcoli richiesti (non stimabili senza i dati; vanno fatti prima di fissare il label_config)

- K1. Durata reale `exit_idx - fill_idx` (media, mediana, p90, p95) per le combinazioni 6h eleggibili.
- K2. `N_p` con unicita' su `[t, exit]` reale, rho fra prodotti, `N_eff` del pool, `indep/yr` ricalcolato (P1-P2).
- K3. `lift_0` (R-c2) su tutta la griglia nella finestra R-c1, piu' H = 20 diagnostico (R-a4).
- K4. Jackknife semestrale (R-c3) e test chi2 di omogeneita' (R-c4).
- K5. `sigma_r` e `W + L` misurati sul net_ret dei trade con fill (per N_min di (d)).
- K6. Mediana di ATR_6h in bps per prodotto sulla finestra R-c1 (per i numeri di sizing di (b)).

### Riepilogo degli effetti

- Griglia: H_max = 15 giorni; nuova eleggibilita' `p_vertical <= 0.05` (esclude di fatto tutte le 1d); pareggio entro 0.01 ->
  N_eff massimo (scelta possibile 3/1.5 invece di 3/2); H = 20 solo diagnostica.
- GO/NO-GO della feasibility: `lift_0` (senza drift) <= 0.10 e jackknife <= 0.10 sulla finestra pre-holdout, al posto del lift
  grezzo su tutto lo storico. Verdetto atteso ancora GO (lift_0 ~0.07, stima), non confermato fino a K3-K4.
- `indep/yr` ridefinito sulla durata reale e corretto per la correlazione fra prodotti.
- Evidenza dal backtest: E1-E5 in aggiunta a G3; esito a tre stati GO / INCONCLUSO (solo dry) / NO-GO.

## Risoluzione architetto

Autore: architetto-opus. Data: 2026-10-18. Ambito: D2 punti 1-9, traduzione software di D1 (R-a*, R-c*, E1-E5, K1-K6), modifiche
alla spec (v2.7). Vincoli assoluti invariati (C1, C11): solo spot, long-only, copertura 100%. Nessuna regola qui sotto tocca
`RiskManager`, `BalanceGuard`, sizing o payload d'ordine. Non serve chiedere nulla all'utente: ogni scelta qui sotto e' un algoritmo
deterministico, e i calcoli K1-K6 li produce il nuovo `feasibility.py`, senza giudizi manuali.

### A. Principio (Decision Tokenization)

Il token `edge` (noul) di uno stato al tempo `t` e' calibrato contro la sua label: la label deve essere l'esito dell'ordine che il
runtime invierebbe DAVVERO per quella decisione. Non puo' essere piu' ottimista dell'esecuzione e non puo' usare prezzi che l'ordine
non vede mai. Ne seguono quattro regole usate in tutte le decisioni:
- R1. Ogni parametro che cambia una label sta nel `label_config` e nel suo `sha256`, e nessun altro file lo definisce.
- R2. Label, feasibility, backtest e dry run usano gli stessi kernel di `modules/dryrun/fill_model.py` e la stessa
  convenzione temporale.
- R3. La scelta dei parametri (feasibility) non vede mai l'holdout, e gli strumenti usati solo per la scelta (il detrend)
  non entrano mai nello stato, nelle label di training o nello `sha256` (C7).
- R4. Fra due regole ugualmente plausibili vince quella conservativa (cioe' la meno favorevole all'esito UPPER).

### B. D2: decisioni

**B1. `maker_first_bar`: si corregge a 3 e diventa `entry_first_bar` (F), comune a maker e taker.**
- Derivazione. Il sample `t` e' la barra che si chiude a `ts_bar_close = ts[t] + 1 min`. Il runtime legge le barre a
  `ts_bar_close + S` (S = 120 s), cioe' all'inizio della barra `t+3`: le barre `t+1` e `t+2` sono gia' passate quando l'ordine
  arriva. Quindi `F = 1 + ceil(S / 60) = 3`.
- Campo di `LabelConfig`: `entry_first_bar: int = Field(default=3, ge=1, le=6)`, che sostituisce `maker_first_bar`. F = 1 e'
  ammesso solo nei test, perche' riproduce la semantica della 2.6.1; il massimo 6 corrisponde a S = 300 s. Il validator
  `maker_first_bar <= entry_timeout_min` si elimina: con la nuova finestra non serve piu'.
- Maker: `limit = close[t+F-1] * (1 - s/2/1e4)`. La chiusura della barra `t+F-1` e' l'istante di invio, e da li' si stima il
  best_bid su cui il RiskManager prezza davvero (sez. 4.1, passo 3). Fill alla prima barra `k` in `[t+F, t+F+T_in-1]` con
  `low[k] < limit` (`maker_entry_filled`): l'ordine resta nel book esattamente `T_in` barre, coerente con `entry_timeout_s` 1800.
  `NO_FILL` vale solo se `t+F+T_in-1 <= n-1`; altrimenti l'esito e' `UNRESOLVED`.
- Taker: `fill = t+F`, con `entry = taker_entry_px(open[t+F], s, slip)`.
- `A = atr[t]` resta l'ATR della barra di decisione (sez. 4.1, passo 3: mai riletto all'invio). Non cambia.
- Il kernel `_scan` riceve `first_bar = F` e lo applica a entrambi i modi.
- Controllo a runtime, da fare quando si implementa il DecisionEngine e non ora: al caricamento del champion, se
  `1 + ceil(market.bars.rest_delay_s / 60) != label_config.entry_first_bar`, si logga il warning `label_exec_delay_mismatch`
  e lo si riporta nella sezione parity del report del dry run. Non e' bloccante: la scala di taratura di S (2.3 A.2) non deve
  invalidare il modello, e lo scarto di una barra su 30 viene misurato da G-DRY-OPS.

**B2. Uscita SL: confermata la formula di 3.1.3.** `sl_exit_px(lower, open[j], s, slip, x) = min(lower, open[j]) * (1 - (s/2 +
slip + x)/1e4)` con fee taker. E' quella del `CostModel.c_sl_out`. Il testo della tabella 5.1.3 e' sbagliato e va corretto (G10).

**B3. Stessa barra: confermata l'implementazione.** E' logicamente equivalente al testo della 3.1.3: se `low <= lower` l'esito e'
LOWER per qualsiasi `high` (perche' `high > upper` implica `high >= upper`). UPPER solo con `high > upper`, in modo stretto.
Con `high == upper` e `low > lower` non succede nulla. Il testo della 3.1.3 si riscrive nella forma semplificata (G4).

**B4. Barra di fill: si corregge. Nella barra di fill si controlla SOLO lo SL.**
- In quella barra: se `low[fill] <= lower` l'esito e' LOWER, con `exit_idx = fill` ed
  `exit_px = sl_exit_px(lower, open[fill], s, slip, x)`. Il TP NON si controlla.
- Dalla barra `fill+1` fino a `vertical` incluso vale la regola B3. `vertical = fill + H` e non cambia.
- Motivo (R2, R4): il bracket viene piazzato solo dopo lo stato terminale dell'ingresso (4.1.6, regola 4), quindi nella barra di
  fill non esiste un TP. Il watchdog chiude invece a mercato a `last <= SL` sulle posizioni senza bracket (4.1.4). Vale per
  maker e taker: l'`open[t+F]` del taker e' il primo prezzo della barra, quindi tutto il range della barra viene dopo il fill.

**B5. `UNRESOLVED` confermato** (finestra non osservata o `A` non valido). Non e' una label: va esclusa da ogni metrica e dal
dataset. Nuova funzione in `labeling.py`, usata da feasibility, build_dataset (purge), backtest e LabelResolver:

    def label_window_end(ts_bar_close: pd.DatetimeIndex, cfg: LabelConfig) -> pd.DatetimeIndex:
        # max(ts + (F + T_in - 1) min, ceil_to_hour(ts)) + H min
        # Primo termine: chiusura della barra verticale nel caso peggiore (fill all'ultima barra della finestra).
        # Secondo termine: dati usati da forward_rv_bucket (E_t + H).

Test richiesti: hourly sample, cioe' `ts` all'ora piena, deve dare `ts + (F+T_in-1+H) min`; un sample CUSUM a `hh:05` deve dare
`ceil_hour + H` quando `ceil_hour > ts + 32 min`.

**B6. Pool: confermato, con una guardia per prodotto.** La scelta e il GO si fanno sul pool, con un solo `label_config`. Al GO si
aggiunge una condizione: per ogni prodotto `lift_0,p <= 0.10 + 2 * SE_p`, con `SE_p = sqrt(p_hist_0,p (1 - p_hist_0,p) / N_p)`.
Il margine di 2 SE serve a non escludere un prodotto per puro rumore. Se un prodotto fallisce, il verdetto e' NO-GO con motivo
`product_lift:<id>` (exit 3) e il report suggerisce di rieseguire senza quel prodotto. Il riesame e' manuale, via `--products`:
nessuna esclusione automatica, cosi' la scelta resta tracciabile.

**B7. Spread di label: `max(snapshot del probe, 3.0 bps)` per prodotto, finche' non esiste la tabella del recorder (L4).**
- Motivo: G-DRY-OPS richiede `spread_live / spread_label <= 2.0` (`max_spread_ratio_vs_labels`). Con BTC a 1.09 nel probe
  contro 2.9 misurato il 17-ott il rapporto sarebbe 2.66 e il gate fallirebbe. Con il minimo a 3 bps passano spread live fino
  a 6 bps. L'effetto sulle label e' di circa 1 bps su circa 90 di costo: trascurabile per il lift, decisivo per la parita'.
- Costante `LABEL_SPREAD_FLOOR_BPS = 3.0` in `feasibility.py`, con flag CLI `--spread-floor-bps` (default 3.0). Il flag
  `--spread-bps` esistente resta come override totale.
- Lo spread cambia le label, ma oggi NON e' nello `sha256` (difetto, da correggere). Nuova firma:
  `LabelConfig.sha256(fees, costs, spread_bps: Mapping[str, float]) -> str`; il payload aggiunge
  `"spread_bps": {prodotto: valore}` (chiavi ordinate). Quando arrivera' la tabella oraria, entrera' nel payload il suo hash:
  nuovo `label_config`, quindi nuova versione del modello.
- `label_config.json` scrive `spread_bps` (il valore usato) e `spread_source: {prodotto: {"probe": x, "floor": 3.0}}`.

**B8. Aggiornamento della spec: si', ma senza valori numerici normativi nella 0.4.** La fonte di verita' e'
`data/datasets/label_config.json`. Il candidato 6h / 3-2 / maker / 15 d va ricalcolato con le regole della 2.7 e puo' diventare
3/1.5, quindi la 0.4 lo riporta come esito storico, non come decisione (testo in G3). I riferimenti normativi ad `ATR_14_1d` /
`atr_1d` diventano `ATR_14_<atr_tf>`. Gli esempi numerici (0.3, 4.1.2, 4.2) restano in 1d, marcati come esempi (G2).
Requisito per i consumatori: nuova funzione `labeling.load_label_config(path) -> LoadedLabelConfig` (dataclass frozen con
`cfg`, `fees`, `costs`, `spread_bps`, `holdout_start`, `raw`). Ricalcola lo `sha256` e solleva `ValueError` se non coincide; i
CLI lo convertono in exit code 2. Nessun codice di produzione costruisce `LabelConfig()` con i default: i default storici
(1d, 4/2, 14400) restano solo per i test.

**B9. Embargo: confermati 2 giorni (normativa la sez. 3.1.4). Si uniforma la 3.5 punto 3**, la cui firma `timedelta(hours=24)`
e' un refuso. Testo da incollare in G7. `splits.py` resta con `DEFAULT_EMBARGO = timedelta(days=2)`; il commento sul "24 h
superseded" si rimuove dopo l'aggiornamento della spec. Una modifica collegata e obbligatoria per R-c1 (vedi C1):
- `purged_walk_forward(..., holdout_start: pd.Timestamp | None = None)`. Se e' dato, sostituisce `end - holdout_days`;
  `ValueError` se `holdout_start > timestamps[-1]` o se e' prima del primo sample.
- Nuova funzione `splits.holdout_start_from(last_ts_bar_close: pd.Timestamp, holdout_days: int = 120) -> pd.Timestamp` =
  `floor_to_day_utc(last_ts_bar_close) - holdout_days`. E' l'unica definizione.
- I `timestamps` passati a `purged_walk_forward` sono `ts_bar_close` (il campo `t` della 3.2.1); `t1 = label_window_end(t)`.
- Test: con `holdout_start` esplicito l'holdout e' esattamente `t >= holdout_start`; il dev e' purgato con
  `t1 < holdout_start - embargo`.

### C. D1: specifiche per `training/feasibility.py`

**C0. Costanti** (al livello del modulo, nei test si importano):

    ATR_TFS = ("6h", "1d"); K_TP = (3.0, 4.0, 5.0); K_SL = (1.5, 2.0); ENTRY_MODES = ("maker", "taker")
    HORIZON_DAYS = (5, 10, 15, 20)        # 20 solo diagnostica (R-a4): mai eleggibile
    HOLDOUT_DAYS = 120                    # sez. 3.1.4
    H_MAX_DAYS = HOLDOUT_DAYS // 8        # = 15 (R-a1)
    EMBARGO = training.splits.DEFAULT_EMBARGO   # 2 giorni
    FEE_FLOOR_SHARE_MIN = 0.80; MAKER_FILL_MIN = 0.50; P_VERTICAL_MAX = 0.05   # R-a2
    LIFT_MAX = 0.10; LIFT_TIE = 0.01      # R-a3
    SATURATION_TOL = 0.02                 # R-a4
    PRODUCT_SE_MULT = 2.0                 # B6
    LABEL_SPREAD_FLOOR_BPS = 3.0          # B7
    CHI2_MIN_EXPECTED = 5.0
    RHO_MIN_COMMON = 30
    DAYS_PER_YEAR = 365.25; EXIT_NO_GO = 3

Rimossi: `indep_trades_per_year` (formula `365/H x fill`) e `ProductData.years` (lunghezza dello storico intero).

**C1. Finestra di selezione (R-c1).**

    last_close    = max_p (index_p[-1] + 1 min)                      # ultima barra salvata, su tutti i prodotti
    holdout_start = splits.holdout_start_from(last_close, HOLDOUT_DAYS)
    cut           = holdout_start - EMBARGO
    barre caricate: solo quelle con ts_bar_close < cut               # maschera dopo load_bars, nessuna altra semantica di end
    cfg_ref       = LabelConfig(H = max(HORIZON_DAYS), F, T_in)      # H_ref = 20 giorni: stesso insieme di sample per TUTTE le combo
    sample        = griglia oraria, ATR raw e detrend non NaN per entrambi gli atr_tf, label_window_end(ts, cfg_ref) < cut

Decisione: la finestra e' unica per tutte le combinazioni, dimensionata sull'H piu' lungo della griglia, cosi' le combinazioni
si confrontano sugli stessi `t`. E' la stessa regola di purge del dev in `splits.py`, quindi selezione e dev coincidono per
costruzione. Asserzione: dopo il filtro nessun sample deve essere UNRESOLVED; se ne compare uno e' un bug, `AssertionError`.
`label_config.json` scrive `selection = {"holdout_start", "cut", "sample_start", "sample_end", "holdout_days": 120,
"embargo_days": 2, "h_ref_days": 20}`.
Effetto a valle: `build_dataset` passa `selection.holdout_start` a `purged_walk_forward` (B9). L'holdout e' congelato all'istante
della scelta e cresce con lo storico. Rieseguire build_dataset piu' tardi non puo' quindi portare nell'holdout dati gia' visti
dalla selezione (con il vecchio calcolo "ultimo sample risolto - 120 d" sarebbe successo, di circa 15 giorni).

**C2. Detrend (R-c2), solo dentro feasibility (R3).**

    def _detrend(bars: pd.DataFrame) -> tuple[pd.DataFrame, float]:
        # bars: barre caricate (C1), indice 1m contiguo (load_bars ricostruisce i minuti piatti); altrimenti ValueError.
        # i0 = 0, i1 = len - 1 ; mu = (ln close[i1] - ln close[i0]) / (i1 - i0)            # per minuto
        # g_i = exp(-mu * (i - i0)) ; open/high/low/close *= g ; volume invariato
        # ritorna (bars_detrended, mu); nel report mu_year = mu * 1440 * DAYS_PER_YEAR

- ATR detrendizzato: `compute_indicators(bars_detrended)` letto solo come `atr_14_<tf>` (nessuna nuova formula di ATR).
- Il fattore e' identico su O/H/L/C della stessa barra, quindi `low <= open, close <= high` resta vero e i kernel di fill
  funzionano senza modifiche.
- Confine C7. Il `mu` usa la chiusura a T1, cioe' informazione futura rispetto ai `t` interni. E' ammesso solo perche' serve a
  scegliere iperparametri della label, come gia' fa oggi il lift in-sample. Vincoli di implementazione:
  (i) `_detrend` e' privata in `feasibility.py`, non esiste in `labeling.py` ne' in `modules/`;
  (ii) `mu` appare solo in `label_config.json -> feasibility.mu_year` (diagnostica) e mai nello `sha256`;
  (iii) `build_dataset`, `backtest`, `evaluate` e il runtime usano solo barre grezze.

**C3. Due etichettature per combinazione: `series in {"detr", "raw"}`.** Gli input della scelta (eleggibilita', `lift_0`,
`N_eff`, `rho`, jackknife, guardia per prodotto) vengono SOLO dalla serie `detr`. Le diagnostiche K1, K5, K6, il lift grezzo
e la saturazione vengono dalla serie `raw`. Il chi2 si calcola su entrambe.

    @dataclass(frozen=True)
    class ProductData:
        name: str; spread_bps: float; ts_close: np.ndarray  # datetime64[ns], per barra
        samples: np.ndarray                                   # indici (C1)
        raw: BarArrays; detr: BarArrays; mu_per_min: float
    @dataclass(frozen=True)
    class BarArrays: o; h; lo; c: np.ndarray; atr: dict[str, np.ndarray]

    def load_product(root: Path, product: str, spread_bps: float, *, cut: pd.Timestamp,
                     cfg_ref: LabelConfig, detrend: bool = True) -> ProductData
    # evaluate.py (G0) la riusa con detrend=False e una finestra di holdout: parametri `sample_start`, `sample_end`
    # opzionali (default: tutto cio' che soddisfa C1).

`_label_product(p, cfg, fees, costs, min_net_edge_bps, series) -> _Raw` aggiunge a `_Raw` i campi `t_idx`, `fill_idx`,
`exit_idx`, `ts` (ts_bar_close dei sample) e `w` (pesi di unicita' dei soli sample con fill, NaN negli altri).

**C4. Metriche** (per prodotto e per pool, per ciascuna serie). `Metrics` sostituisce `indep_trades_per_year` e aggiunge:

    n, filled, fill_rate, p_hist, p_breakeven, lift, lift_rw, fee_floor_share, p_vertical, vert_mean_net_bps   # come oggi
    n_eff            # prodotto: N_p ; pool: N_eff
    se_p_hist        # sqrt(p_hist (1 - p_hist) / n_eff)
    indep_per_year   # n_eff / years ; years = (max ts - min ts dei sample) / 365.25 d (prodotto) ; pool: span dell'unione
    dur_mean_min, dur_median_min, dur_p90_min, dur_p95_min      # K1: exit_idx - fill_idx sui fill
    sigma_r_bps      # K5: std(net_ret[fill], ddof=1) * 1e4
    wl_bps           # K5: (mean net|UPPER - mean net|LOWER o VERTICAL) * 1e4

- `p_hist` e `p_breakeven` restano NON pesati, come oggi: sono la frequenza per ora d'ingresso, la stessa popolazione su cui
  si calibra `p_edge`. I pesi servono solo per `n_eff`, `SE` e chi2.
- `N_p = uniqueness_weights(t_idx[fill], exit_idx[fill]).sum()`, solo fra i sample con fill dello stesso prodotto, su span
  `[t, exit]` reali e inclusivi (P1). `NO_FILL` escluso.
- `rho` (P2): per ogni coppia di prodotti, Pearson fra `1[UPPER]` sui `ts` comuni con fill in entrambi (allineamento per
  timestamp, mai per indice). Una coppia si esclude se ha meno di `RHO_MIN_COMMON` punti comuni o varianza nulla.
  `rho = mean` delle coppie valide, troncato in `[0, 1]`; senza coppie valide `rho = 1.0` (conservativo); con un solo prodotto
  non si usa. Pool: `N_eff = sum_p N_p / (1 + (K - 1) * rho)`.
- `ComboResult = {cfg, detr: Metrics, raw: Metrics, per_product: dict[str, tuple[Metrics, Metrics]], rho_detr, rho_raw}`.
  `lift_0 = detr.lift`, `lift_raw = raw.lift`.
- Memoria: `evaluate()` restituisce solo aggregati. Gli array per sample si ricostruiscono rietichettando la sola combo
  scelta (C6).

**C5. Eleggibilita' e scelta (R-a1, R-a2, R-a3).**

    eligible(r) = r.cfg.horizon_min <= H_MAX_DAYS * 1440 and isfinite(r.detr.lift)
                  and r.detr.fee_floor_share >= 0.80 and (r.cfg.entry_mode == "taker" or r.detr.fill_rate >= 0.50)
                  and r.detr.p_vertical <= 0.05
    ineligible_reason(r) -> str            # "h_gt_hmax" | "fee_floor" | "fill" | "p_vertical" | "" : colonna del report
    family(r) = (atr_tf, k_tp, k_sl, entry_mode)
    reps      = per famiglia, il membro eleggibile con H minimo           # R-a2
    lift_min  = min(rep.detr.lift)
    tie       = [rep : rep.detr.lift <= lift_min + LIFT_TIE]              # R-a3
    best      = min(tie, key=(-detr.n_eff, horizon_min, atr_tf, k_tp, k_sl, entry_mode))
    def choose(results: Sequence[ComboResult]) -> tuple[ComboResult | None, list[ComboResult]]   # (best, tie)

La scelta fra 3/2 e 3/1.5 (R-a3) la decide questo algoritmo con K2-K3, non un'altra domanda.

**C6. Diagnostiche della combo scelta** (si rietichetta `best` su entrambe le serie):

    def semester_of(ts: pd.DatetimeIndex) -> np.ndarray            # "2024H1" se mese <= 6, altrimenti "2024H2" (UTC)
    def jackknife_lift0(raws_detr: Mapping[str, _Raw]) -> dict[str, float]
        # per ogni semestre s presente fra i fill: lift_0(-s) = median(p_be[fill, sem != s]) - mean(UPPER[fill, sem != s])
        # pool di tutti i prodotti, mu invariato (nessun nuovo detrend). jack_max = max dei valori.
    def period_table(raws, rho, by: Literal["semester", "year"]) -> list[PeriodRow]
        # p_hist, p_be, lift, N_eff,s = sum(w in s) / (1 + (K-1) rho), SE_s ; su detr e raw
    def homogeneity_chi2(o: np.ndarray, n: np.ndarray) -> tuple[float, int, float, list[int]]
        # o_s = d * sum(w * UPPER in s), n_s = d * sum(w in s), d = 1 / (1 + (K-1) rho)
        # p_bar = sum o / sum n ; si includono solo i semestri con n_s * min(p_bar, 1 - p_bar) >= 5
        # chi2 = sum (o_s - n_s p_bar)^2 / (n_s p_bar (1 - p_bar)) ; df = S_incl - 1 ; p = scipy.stats.chi2.sf(chi2, df)
        # S_incl < 2 -> (nan, 0, nan, esclusi) ; ritorna anche gli indici esclusi
    def saturation(results, tie) -> dict[family, tuple[float, float, bool]]
        # per ogni famiglia del tie: (raw.lift(H=15), raw.lift(H=20), drift_dominated = lift20 < lift15 - 0.02)

- Nota R-a4: la saturazione si misura sul lift GREZZO, perche' e' una diagnostica del drift; sulla serie `detr` il drift
  globale e' gia' tolto. Il report mostra anche `lift_0(20)`. Nessun effetto sul verdetto.
- K6: mediana di `atr_6h / close * 1e4` (raw) per prodotto, sui sample.
- K5: `N_min(delta) = ((2.0 + 0.84) * sigma_r_bps / (delta * wl_bps))^2` per `delta in {0.05, 0.08, 0.10, 0.15}` (raw,
  combo scelta). E' solo informativa: le soglie di E1 non dipendono da K5.

**C7. Verdetto.**

    def verdict(best, jack_max, per_product_detr) -> tuple[bool, list[str]]
    GO  <=>  best is not None                                         # altrimenti "no_eligible"
         and best.detr.lift <= LIFT_MAX                               # "lift0"
         and jack_max <= LIFT_MAX                                     # "jackknife"
         and per ogni prodotto p: lift_0,p <= LIFT_MAX + 2 * SE_p     # "product_lift:<id>" (B6)

GO: exit 0 e scrittura di `label_config.json`. NO-GO: exit 3, `label_config.json` non scritto, motivi nel report. Il chi2
(R-c4) e la saturazione (R-a4) restano diagnostica pura.

**C8. Output.**
- `label_config.json`: campi attuali, con `maker_first_bar` sostituito da `entry_first_bar`, piu' `products`, `spread_bps`,
  `spread_source`, `selection` (C1) e `feasibility`. Il blocco `feasibility` contiene: `detr` e `raw` della combo,
  `rho_detr`, `rho_raw`, `n_eff`, `indep_per_year`, `jack_max`, `chi2_p_detr`, `chi2_p_raw`, `mu_year` per prodotto,
  `dur_*`, `sigma_r_bps`, `wl_bps`, `atr6h_median_bps` per prodotto e `verdict_reasons`.
  `sha256 = cfg.sha256(fees, costs, spread_bps)`.
- `feasibility.md`: verdetto con motivi; finestra di selezione; `mu_year` per prodotto; tabella di tutte le combinazioni
  ordinata per `lift_0`, con colonne `atr | k | entry | H | p_be | p_hist_0 | lift_0 | lift_raw | lift_rw | floor | fill |
  p_vert | vert_net | dur_med_h | dur_p95_h | N_eff | indep/yr | eligible (motivo)`; insieme del tie; combo scelta per
  prodotto (detr e raw, `N_p`, `SE_p`, guardia); jackknife; tabelle per semestre e per anno; chi2; saturazione; K5 (N_min); K6.

**C9. Test (`tests/test_feasibility.py`, serie sintetiche).**
1. `_detrend`: su una serie geometrica a drift costante, dopo il detrend vale `close~[i1] == close~[i0]`; l'ordine OHLC e'
   preservato; il volume e' invariato; un indice non contiguo da' `ValueError`.
2. Finestra: nessuna barra con `ts_bar_close >= cut` viene usata; i sample con `label_window_end >= cut` sono esclusi;
   `holdout_start` coincide con `splits.holdout_start_from`.
3. `N_p`: con uscite dopo una barra e sample orari `N_p == n_fill`, e non dipende da H; con span totalmente sovrapposti
   `N_p` vale circa 1.
4. `rho`: esiti identici in due prodotti danno `rho = 1` e `N_eff = sum/2`; esiti opposti danno `rho = 0` per troncamento;
   meno di 30 punti comuni danno `rho = 1`.
5. `choose`: `p_vertical = 0.06` e' escluso; vince l'H minimo per famiglia; H = 20 non viene mai scelto; nel tie entro 0.01
   vince l'`N_eff` maggiore; a parita' esatta vince l'ordine deterministico.
6. `jackknife_lift0`: semestri costruiti con tassi noti producono i valori attesi a mano.
7. `homogeneity_chi2`: dati omogenei danno `chi2 = 0` e `p = 1`; un caso numerico a mano coincide con `scipy`; i semestri
   con valore atteso < 5 vengono esclusi.
8. `verdict`: ognuno dei quattro motivi produce exit 3 e nessun `label_config.json`.
9. `sha256`: cambia se cambia lo spread o `entry_first_bar`; NON cambia se cambia `mu` o `selection`.

Test aggiuntivi in `tests/test_labeling.py`:
- con F = 3 le barre `t+1` e `t+2` non riempiono;
- il limite e' `close[t+2] * (1 - s/2)`;
- il taker entra a `open[t+3]`;
- la finestra maker e' lunga esattamente `T_in` barre;
- c'e' il confine NO_FILL / UNRESOLVED;
- nella barra di fill: `low <= lower` da' LOWER a `sl_exit_px`, mentre `high > upper` NON da' UPPER;
- regola della stessa barra (B3);
- `label_window_end` (B5);
- con F = 1 si riproducono i risultati della semantica 2.6.1.

### D. D1 (d): specifiche per `training/backtest.py` e `training/evaluate.py`

**D1. `backtest.py`: requisiti aggiuntivi (R2).**
- La decisione si prende alla barra `t` (stato con barre `<= t`).
- L'ordine si prezza a `close[t+F-1]`: best_bid/ask stimati con lo spread del `label_config`, poi arrotondati come in 4.1.3.
- Maker: l'ordine resta nel book dalle barre `[t+F, t+F+T_in-1]`. Taker: entra a `open[t+F]`.
- Nella barra di fill vale solo lo SL (B4); il bracket vale da `fill+1`.
- La SELL del modello decisa a `t'` esce a `market_exit_px(open[t'+F], s, slip)`.
- Solo kernel di `fill_model.py`, con F e spread letti da `load_label_config`.
- Ogni fold riparte da un `VirtualAccount` nuovo con `backtest.starting_quote` (config `train.yml`, default `"100.00"`, uguale
  al book champion), cosi' i `pnl_eur` dei fold sono confrontabili (E4).
- Ingressi nell'ultimo fold di test solo con `label_window_end(t) < holdout_start - EMBARGO`: nessun esito OOS usa prezzi
  dell'holdout. I fold intermedi possono chiudere trade oltre `test_end`.
- Backtest dell'holdout: ingressi con `t >= holdout_start` e `label_window_end(t) <= ultima barra`.
- Output `trades` (una riga per trade chiuso):
  `product, fold (int, -1 = holdout), decision_ts, decision_idx, fill_ts, exit_ts, exit_reason, net_ret_bps (esatto, sez. 0.3),
  pnl_eur, base_size, notional_eur`.
- Invariante, da verificare con un test: un trade con fill nel backtest ha la label di `decision_idx` diversa da NO_FILL.
  Il limite arrotondato per difetto e' `<=` al limite della label, quindi un fill nel backtest implica un fill nella label.

**D2. G0, regime sull'holdout (R-c5)**, prima di ogni altro gate:

    def regime_check(lc: LoadedLabelConfig, root: Path) -> RegimeCheck   # (lift_holdout, se, n_eff, passed)
    # load_product(..., detrend=False) sui sample orari con ts >= holdout_start, risolti; metriche come C4 (serie raw)
    # SE = sqrt(p (1-p) / N_eff), con N_eff calcolato usando rho = lc.raw["feasibility"]["rho_raw"] (congelato)
    # passed = lift_holdout <= LIFT_MAX + 2 * SE ; altrimenti NO-GO "regime_change"

**D3. Evidenza assoluta E1-E5**: funzioni pure in `evaluate.py`, importabili dai test.

    E_MIN_TRADES = 150; E_MIN_NEFF = 100; N_BOOT = 10_000; N_PERM = 10_000; SEED = 1234
    T_INCONCLUSIVE = 1.0; PERM_P_MAX = 0.05; FOLD_SHARE_MAX = 0.50; MIN_POSITIVE_FOLDS = 3
    Z_HOLDOUT_MIN = -1.96; MIN_HOLDOUT_TRADES = 5
    BLOCK = ceil_days((F + T_in - 1) min + H)                      # 16 giorni con H = 15 (circa T_in + H, come chiesto dal quant)

    def effective_trades(trades, rho: float) -> float
        # c_i = numero di ALTRI prodotti con almeno un trade sovrapposto a [fill_ts_i, exit_ts_i]
        # w_i = 1 / (1 + rho * c_i) ; N_eff_trade = sum w_i      (rho = rho_raw del label_config)
        # Tre trade sempre sovrapposti -> 3 / (1 + 2 rho), cioe' la formula P2; trade disgiunti -> 1 ciascuno.
        # Sovrapposizioni dello stesso prodotto ai confini di fold: ignorate.
    def block_bootstrap(values_bps, ts, span_start, span_end, block, n_rep, seed) -> Boot   # mean, se, q025, q975, t
        # Circolare sul TEMPO: x_i = (fill_ts_i - span_start) mod span ; B = ceil(span / block)
        # Per replica: B inizi u_b uniformi sulla griglia oraria di [0, span); il blocco b prende i trade con
        # x in [u_b, u_b + block) (con wrap) ; statistica = media non pesata di tutti i trade estratti (con molteplicita').
        # Una replica vuota si riestrae. se = std(ddof=1) delle repliche ; t = mean / se.
        # Tutti i prodotti stanno nello stesso blocco temporale.
    def permutation_pvalue(model_label_bps: np.ndarray, model_counts: Mapping[(fold, prod), int],
                           candidates: Mapping[(fold, prod), Candidates], n_rep, seed) -> PermResult  # p, shortfall
        # Candidates = (t_idx, exit_idx, label_net_bps): barre 1m del fold f e del prodotto p con ts_bar_close in
        # [test_start_f, test_end_f), stessa regola d'ingresso del backtest (D1), label raw del champion con fill,
        # fee floor superato (A = atr[t], spread e fee del label_config).
        # Per replica e per (f, p): permutazione casuale; accettazione greedy se [t, exit] non si sovrappone a quelli
        # gia' accettati dello stesso prodotto, fino a n_fp. Se il pool si esaurisce si tengono gli accettati e si
        # conta lo shortfall (riportato, non fa fallire il test).
        # rep_mean = media pooled delle label_net_bps estratte ; m_model = media delle label_net_bps di decision_idx dei
        # trade OOS del modello ; p = (1 + #{rep_mean >= m_model}) / (1 + n_rep).
    def concentration(pnl_eur_by_fold: Mapping[int, float]) -> tuple[float, int]   # (max_share, n_positive)
        # max_share = max_f pnl_f / sum pnl ; se sum pnl <= 0 -> max_share = inf
    def holdout_consistency(boot_h: Boot, boot_oos: Boot) -> float   # z = (m_h - m_oos) / sqrt(se_h^2 + se_oos^2)
    def edge_status(...) -> EdgeVerdict   # status: Literal["GO", "INCONCLUSO", "NO-GO"], checks: dict[str, bool], reasons

Definizione delle condizioni (OOS = fold di test 0-4 concatenati, `span = [folds[0].test_start, holdout_start)`):
- E1: `N_trade_OOS >= 150` e `effective_trades >= 100`.
- E2: `boot_oos.q025 > 0` e `sum(pnl_eur_OOS) > 0`, su `net_ret_bps` del backtest, non pesato.
- E3: `p <= 0.05`. Il modello e il caso nullo sono misurati con lo stesso metro (net della label alla barra di decisione):
  E3 misura la sola capacita' di selezione degli ingressi; le uscite del modello e il PnL reale li misura E2.
- E4: `max_share <= 0.50` e `n_positive >= 3`.
- E5: holdout con `>= 5` trade, `sum(pnl_eur_h) > 0` e `z > -1.96` (bootstrap con lo stesso `BLOCK` sullo span
  dell'holdout). Con meno di 5 trade E5 non e' valutabile.

Esito, nell'ordine:
1. `G0` fallito -> NO-GO (`regime_change`).
2. E1 fallito -> INCONCLUSO se `boot_oos.mean > 0`, altrimenti NO-GO.
3. E1-E5 tutti ok -> GO.
4. E1, E3, E4 ok, E5 ok o non valutabile, E2 fallito con `boot_oos.t >= 1.0` -> INCONCLUSO.
5. E1-E4 ok ed E5 non valutabile -> INCONCLUSO.
6. Ogni altro caso -> NO-GO.

**D4. Posto nei gate della sez. 3.4.** L'ordine di calcolo e' `G0 -> G1 -> G2 -> GE -> G3 -> G4`. Si calcolano e stampano
tutti, senza fermarsi al primo fallimento.
- G0: nuovo (D2).
- GE (E1-E5): nuovo gate, calcolato per il candidato E per il baseline LightGBM. Oggi in fase locale il candidato e' il
  baseline, perche' il LoRA non viene addestrato (C8).
- G3: resta il confronto RELATIVO con il baseline e si applica solo se il candidato non e' il baseline.
- Deploy: la rampa R0 richiede G-DRY-OPS + G0, G1, G2, G4 + GE = GO per il decider del champion + G3 se il candidato e' un LLM.
  Con GE = INCONCLUSO il champion gira solo in dry e il capitale live e' zero.
- Uscita da INCONCLUSO: rivalutazione di E2 su trade OOS piu' `VirtualTrade` chiusi del book champion in dry, con lo stesso
  bootstrap sul tempo reale. Quando l'esito ricalcolato e' GO, R0 si sblocca. L'implementazione arriva in P3 con il dry run.
- Se G3 fallisce si usa il `BaselineDecider`, ma con il SUO esito GE: senza GO del baseline, solo dry.
- Exit code di `evaluate.py`: 0 se tutto passa con GE = GO; 5 se tutto passa tranne GE = INCONCLUSO; 1 in ogni altro caso.
  Il 5 e' un codice nuovo; quelli in uso sono 2 config, 3 DryRunViolation e NO-GO feasibility, 4 SpotViolation.

**D5. Proposta del quant "ECE <= 0.03 sui bin con `p_edge >= p_edge_min`": RESPINTA come gate, ACCOLTA come diagnostica.**
- Motivo statistico. Per fold, i sample con `p_edge >= p_edge_min` valgono qualche decina di `N_eff`. Il rumore di campionamento
  su `|acc_b - conf_b|` e' circa `sqrt(0.25 / 50) = 0.07`, oltre il doppio della soglia: un modello perfettamente calibrato
  fallirebbe per caso. Anche sui fold concatenati il SE (circa 0.035) supera la soglia.
- Motivo economico. Il danno temuto (EV gate che ammette trade a EV nullo) e' esattamente cio' che misurano E2 ed E3, con una
  potenza calcolata. Inoltre `tune_policy` sceglie `p_edge_min` sugli esiti reali del validation, quindi compensa parte della
  sovrastima.
- Cosa si fa. `evaluate.py` riporta, sui fold di test concatenati e sui soli sample con `p_edge >= p_edge_min` (dal
  `policy.json`), l'ECE a 15 bin ristretto a quei bin e il bias firmato `sum w (p_edge - y) / sum w` (pesi di unicita') con
  `SE = sqrt(p_bar (1 - p_bar) / N_eff_regione)`. Entrambi vanno nel model card (`metrics_holdout.edge_bias_decision_region`),
  senza soglia. G1 resta invariato.

**D6. Test (`tests/test_evaluate.py`, `tests/test_backtest.py`).**
- `effective_trades`: trade disgiunti danno N; tre trade sempre sovrapposti danno `N / (1 + 2 rho)`.
- `block_bootstrap`: valori costanti danno `se = 0` e `t = inf`; a seed fisso il risultato e' deterministico; un trade
  vicino al confine di uno span circolare viene estratto.
- `permutation_pvalue`: se il modello coincide con il miglior sottoinsieme non sovrapposto, `p` vale circa
  `1 / (1 + n_rep)`; se il modello e' estratto a caso, `p` vale circa 0.5 (tolleranza 0.1 su 2000 repliche); la non
  sovrapposizione e' rispettata.
- `concentration`: i casi limite `sum <= 0` e la soglia 0.50.
- `edge_status`: tutte le sei righe dell'ordine D3.
- backtest: invariante D1 (fill nel backtest implica fill nella label); ritardo F; SL nella barra di fill; nessun ingresso
  dell'ultimo fold con `label_window_end >= holdout_start - EMBARGO`.

### E. Conflitti trovati e come li risolvo

1. Quant, contraddizione interna: E1 dice "sotto soglia: INCONCLUSO", la regola finale dice "ogni altro caso: NO-GO".
   Risolto in D3 riga 2: INCONCLUSO se la media OOS e' positiva, NO-GO altrimenti.
2. Quant, E2: "q2.5 > 0" e "t >= ~2.0" non sono equivalenti con distribuzioni asimmetriche. Il criterio di passaggio e' il
   percentile (piu' robusto); `t` serve solo per la soglia di INCONCLUSO.
3. Quant, R-a4 nomina il "lift" dopo aver sostituito il lift grezzo con `lift_0`: la saturazione si misura sul grezzo (C6).
4. Limite di R-c1, segnalato e accettato. R-c1 protegge l'holdout ma non i fold di test del walk-forward: il `label_config` e'
   scelto su dati che contengono quei fold. Si accetta perche' (i) la scelta usa `lift_0`, senza drift, su 3 parametri di
   geometria; (ii) E3 confronta il modello con ingressi casuali con la STESSA geometria, quindi il vantaggio della scelta
   delle barriere e' anche nel caso nullo e si annulla; (iii) l'unico controllo pulito resta l'holdout (G0, E5).
5. Spec 3.1.3 "le label non dipendono da S" contro `entry_first_bar`: la label dipende da S solo tramite F, registrato nel
   `label_config` (G4).
6. Spec 5.1.3: formula dello SL, uscita del modello a `open[t+1]` e ingresso taker a `open[t+1]` sono incoerenti con
   3.1.3 e B1. Si corregge 5.1.3 (G10).
7. Spec: "lift richiesto" ha due definizioni (0.4 contro random walk, 3.1.3 contro `p_hist`). Si rinomina `lift_rw` in 0.4
   e il criterio diventa `lift_0` (G3, G4).
8. Spec: "~36 finestre indipendenti", "10 giorni", "3-4 mesi" (3.1.4, 5.1.6, 5.1.7) usano la formula sbagliata P1. Diventano
   riferimenti a `indep/yr` del report (G5, G11).
9. Codice: lo `sha256` non include lo spread, che pero' cambia le label (B7).
10. Codice e spec: con il calcolo attuale l'holdout di `build_dataset` (ultimo sample risolto - 120 d) inizierebbe circa
    15 giorni prima di quello della feasibility, e i dati della selezione finirebbero nell'holdout. Si risolve congelando
    `selection.holdout_start` (B9, C1).
11. Spec 5.1.5: il LabelResolver usa `t + H <= now`, che ignora `T_in`, F e il forward della `vol_regime`. Diventa
    `label_window_end(t) <= now` (G11).
12. Nessun conflitto fra le regole del quant e C10/C11. I numeri di sizing in (b) rispettano `max_position_frac` 0.20 e
    `max_total_exposure_frac` 0.50: un notional del 15% per trade, 45% con tre posizioni.

### F. Calcoli K: chi decide cosa (nessuna misura manuale richiesta)

| K | Prodotto da | Decide | Stato |
|---|---|---|---|
| K1 durate reali | C4 `dur_*` (raw) | Nessuna soglia; alimenta il testo della 0.4 ("durata reale") e la lunghezza attesa del dry run | Da calcolare con il rerun |
| K2 `N_p`, `rho`, `N_eff`, `indep/yr` | C4 (detr per la scelta, raw per evaluate) | Il pareggio R-a3 (3/2 o 3/1.5), `SE_p` della guardia B6, `rho_raw` usato da G0 ed E1 | Da calcolare |
| K3 `lift_0` sulla griglia + H = 20 | C3-C6 | Scelta e GO (C5, C7); flag `drift_dominated` | Da calcolare |
| K4 jackknife + chi2 | C6 | GO (jackknife); chi2 solo diagnostica | Da calcolare |
| K5 `sigma_r`, `W + L` | C4/C6 (raw) | Nessuna: E1 resta 150/100 (soglie del quant). Serve a spiegare l'esito INCONCLUSO atteso | Da calcolare |
| K6 ATR_6h mediano | C6 (raw) | Nessuna: verifica i numeri di sizing di D1 (b) | Da calcolare |

Oggi nessuna di queste misure esiste: il report attuale va considerato superato, perche' usa tutto lo storico, holdout
compreso, F = 2, uno spread senza minimo e la formula di `indep/yr` sbagliata. Nessun K richiede una decisione umana. L'unica
scelta che una misura futura potra' rivedere e' il minimo di spread di 3 bps, quando arrivera' la tabella del recorder (L4):
allora si fa un nuovo `label_config` e una nuova versione del modello.

### G. Modifiche testuali alla spec (v2.7), testo pronto da incollare

**G1. Intestazione e changelog.** Sostituire la riga `Versione: 2.6.1-draft | ...` con:

    Versione: 2.7-draft | Data: 2026-10-18 | Stato: proposta per implementazione

Inserire dopo il "Changelog 2.6.1":

    Changelog 2.7 (studio di fattibilita' con fee reali 25/50 bps e regole del trader-quant):
    - parametri di barriera: fonte di verita' unica `data/datasets/label_config.json` scritto da `training/feasibility.py`; la sez. 0.4 non contiene piu' valori normativi (i default 1d / 4-2 / 10 giorni non sono sostenibili: `p_vertical` 0.60);
    - studio di fattibilita': finestra di selezione pre-holdout, `lift_0` senza drift (detrend solo per la scelta, mai in stato o label), eleggibilita' `p_vertical <= 0.05` con il piu' piccolo H, `H <= 15` giorni, pareggio entro 0.01 risolto con `N_eff`, jackknife semestrale e guardia per prodotto nel GO, diagnostiche (chi2 di omogeneita', H = 20, durate reali);
    - campioni indipendenti: somma dei pesi di unicita' sulla durata reale `[t, exit]`, corretta per la correlazione fra prodotti;
    - label: `entry_first_bar` F = 1 + ceil(S/60) = 3 per maker e taker, limite al `close[t+F-1]`, finestra d'ingresso di `T_in` barre, nella barra di fill solo lo SL; spread di label `max(probe, 3 bps)` incluso nello `sha256`; `label_window_end(t)` unica fine finestra per purge, selezione e LabelResolver;
    - holdout: inizio congelato nel `label_config` (`selection.holdout_start`), `purged_walk_forward(..., holdout_start=...)`; embargo 2 giorni uniformato (3.5);
    - valutazione: nuovi gate G0 (cambio di regime sull'holdout) e GE (evidenza assoluta E1-E5) con esito GO / INCONCLUSO / NO-GO; la rampa R0 richiede GE = GO; ECE nella regione di decisione solo diagnostica;
    - tabella 5.1.3 allineata a 3.1.3.

**G2. Sez. 0.3**, dopo il paragrafo "Esempio di riferimento", aggiungere:

    L'esempio usa parametri 1d / 4-2 e fee presunte 60/120 solo come illustrazione numerica, ripresa nelle sez. 4.1.2 e 4.2: i parametri in vigore sono quelli del `label_config.json` (sez. 0.4).

**G3. Sez. 0.4.** Sostituire la frase che inizia con "`p_breakeven` e **lift richiesto**" con:

    `p_breakeven` e `lift_rw` (= `p_breakeven` meno `k_sl / (k_tp + k_sl)`, probabilita' di toccare prima il TP in un random walk senza drift). Non e' il criterio dello studio di fattibilita', che usa `lift_0` (sez. 3.1.3): serve solo come ordine di grandezza.

Nella tabella, le intestazioni `BTC lift | ETH lift | SOL lift` diventano `BTC lift_rw | ETH lift_rw | SOL lift_rw`.
Sostituire l'intero blocco "Decisioni v1:" (cinque punti) con:

    Decisioni v1:
    - **Prodotti:** BTC-EUR, ETH-EUR, SOL-EUR (quote EUR, `quote_min_size` = 1 EUR per tutti). Da confermare dall'utente.
    - **Barriere:** `atr_tf`, `k_tp`, `k_sl`, `entry_mode`, `horizon_min`, `entry_timeout_min` (30), `entry_first_bar` e lo spread di label sono quelli di `data/datasets/label_config.json`, scritto da `training/feasibility.py` (sez. 3.1.3), verificato con `sha256` da `load_label_config` e copiato nel model card. Nessun altro file li definisce; label, RiskManager, backtest e dry run li leggono da li'.
    - **Esito del 2026-10-18** (fee reali 25/50 bps, regole della 2.6.1, storico intero): candidato `6h`, 3/2, maker, H = 15 giorni. I default precedenti (1d, 4/2, H = 10 giorni) sono scartati: `p_vertical` 0.60, UPPER solo nel 10% dei casi. Il candidato va ricalcolato con le regole della 2.7 (finestra pre-holdout, `lift_0`, `p_vertical <= 0.05`, pareggio su `N_eff`) e puo' diventare 3/1.5.
    - **Vincoli della griglia:** `atr_tf in {6h, 1d}` (1h escluso dal fee floor); `H <= 15` giorni (= holdout / 8); H = 20 solo diagnostica.
    - Conseguenza da accettare: a questo tier di fee Chronos e' un sistema **swing multi-giorno**: barriere di alcuni ATR a 6h, durata reale di alcuni giorni (misurata dallo studio, `dur_*`), tetto H di 15 giorni. La prova statistica dell'edge viene dal backtest walk-forward (gate GE, sez. 3.4); il dry run verifica che il sistema reale si comporti come il backtest (sez. 5.1.6-5.1.7).
    - Il tier migliora solo con il volume a 30 giorni, che con micro-capitale non cambia: lo studio va rieseguito se le fee reali lette dal probe cambiano. Un nuovo `label_config` e' una nuova versione del modello.

**G4. Sez. 3.1.3.** Prima riga: "(default sez. 0.4)" diventa "(`label_config.json`, sez. 0.4)". Sostituire il blocco di
pseudocodice con:

    F          = entry_first_bar                                # 1 + ceil(S/60) = 3 con S = 120 s
    A          = ATR_14_<atr_tf>(t)                             # noto al tempo t; atr_tf dal label_config
    # l'ordine e' prezzato alla chiusura della barra t+F-1 (istante t + S) e vive dalla barra t+F (BarFillModel, sez. 5.1.3)
    if entry_mode == maker:
        limit  = close[t+F-1] * (1 - s/2/1e4)                   # stima del best_bid all'invio
        fill   = prima barra k in [t+F, t+F+T_in-1] con low[k] < limit   # T_in = entry_timeout (30 barre)
        nessun fill con finestra osservata -> esito = NO_FILL (nessun trade)
        entry  = limit ; f_in = f_m
    if entry_mode == taker:
        fill   = t+F ; entry = open[t+F] * (1 + (s/2 + slip)/1e4) ; f_in = f_t
    upper      = entry + k_tp * A
    lower      = entry - k_sl * A
    vertical   = fill + H
    barra fill:  low <= lower -> esito = LOWER (watchdog: il bracket non e' ancora piazzato, il TP non vale)
    scan 1m bars in (fill, vertical]:
        if   low  <= lower -> esito = LOWER (anche se high > upper nella stessa barra: conservativo)
        elif high >  upper -> esito = UPPER, exit_px = upper, f_out = f_m
    LOWER: exit_px = min(lower, open) * (1 - (s/2 + slip + x)/1e4), f_out = f_t
    nessuna barriera   -> esito = VERTICAL, exit_px = close[vertical] * (1 - (s/2 + slip)/1e4), f_out = f_t
    finestra non osservata o A non valido -> UNRESOLVED (non e' una label)
    net_ret    = exit_px * (1 - f_out) / (entry * (1 + f_in)) - 1                 # formula esatta della sez. 0.3
    label_window_end(t) = max(t + (F + T_in - 1) min, ceil_to_hour(t)) + H        # fine dei dati usati da tutte le label di t

Nel paragrafo successivo, sostituire "finche' non ci sono registrazioni si usa il valore del probe." con:

    finche' non ci sono registrazioni si usa `max(spread del probe, 3 bps)` per prodotto. Lo spread fa parte della label ed entra nello `sha256` del `label_config`.

Sostituire il paragrafo "`t` e' l'istante logico di chiusura della barra ..." con:

    `t` e' l'istante logico di chiusura della barra (`ts_bar_close`), non l'istante di calcolo `t + S` (sez. 2.1). La label dipende da `S` solo attraverso `entry_first_bar`, registrato nel `label_config`: e' la prima barra in cui l'ordine reale, inviato a `t + S`, puo' essere eseguito. Se `1 + ceil(market.bars.rest_delay_s / 60) != entry_first_bar`, il runtime logga `label_exec_delay_mismatch` (non bloccante); lo scarto residuo rientra nella parita' misurata dal dry run (sez. 5.1.7).

Sostituire il paragrafo "**Studio di fattibilita'** ..." con:

    **Studio di fattibilita' (`training/feasibility.py`, prima del primo training).** Sullo storico backfillato e con le fee reali (o presunte, marcate come tali) valuta la griglia `atr_tf in {6h, 1d}`, `k_tp in {3, 4, 5}`, `k_sl in {1.5, 2}`, `entry_mode in {maker, taker}`, `H in {5, 10, 15, 20} giorni` (H = 20 solo diagnostica) sui sample della griglia oraria, con i prodotti in pool (un solo `label_config`).
    - **Finestra di selezione:** `holdout_start` = giorno UTC dell'ultima barra meno 120 giorni; si usano solo le barre con chiusura < `holdout_start - embargo` e i sample con `label_window_end` (calcolato con H = 20) < `holdout_start - embargo`. L'holdout non entra mai nella scelta; `holdout_start` viene scritto nel `label_config` (sez. 3.1.4).
    - **Lift senza drift:** ogni prodotto viene detrendizzato sulla finestra (`mu = (ln C[T1] - ln C[T0]) / (T1 - T0)`, `X~ = X * exp(-mu (i - i0))` su open, high, low, close); ATR e label si ricalcolano sulla serie detrendizzata; `lift_0 = p_breakeven - p_hist_0`, con `p_hist_0` = frequenza di UPPER sui sample con fill. La serie detrendizzata esiste solo dentro lo studio: non entra nello stato, nelle label di training ne' nello `sha256` (C7). Il lift grezzo e `lift_rw` sono riportati come informazione.
    - **Campioni indipendenti:** `N_p` = somma dei pesi di unicita' (sez. 3.1.4) dei sample con fill sul loro intervallo reale `[t, exit]`; `N_eff = sum_p N_p / (1 + (K-1) rho)`, con `rho` = correlazione media a coppie degli indicatori UPPER a stesso `t` (troncata in [0, 1]); `indep/yr = N_eff / anni`; `SE = sqrt(p (1-p) / N_eff)`.
    - **Eleggibilita':** fee floor superato in >= 80% dei sample, fill maker >= 50%, `p_vertical <= 0.05`, `H <= 15` giorni; per ogni (atr_tf, k_tp, k_sl, entry_mode) conta solo il piu' piccolo H eleggibile.
    - **Scelta:** `lift_0` minimo. Le combinazioni entro 0.01 dal minimo sono equivalenti: si sceglie quella con `N_eff` massimo, poi H minore, poi l'ordine lessicografico.
    - **GO** se `lift_0 <= 0.10` sul pool, `max_s lift_0(-s) <= 0.10` (jackknife: ricalcolo escludendo un semestre di calendario alla volta, `mu` invariato) e, per ogni prodotto, `lift_0,p <= 0.10 + 2 SE_p`. Altrimenti il report dichiara NO-GO con i motivi, `label_config.json` non viene scritto, l'exit code e' 3 e il progetto non procede al training.
    - **Diagnostiche senza soglia:** `p_vertical` e `vert_mean_net_bps` (prudenza dell'EV a due esiti, sez. 0.3), durate reali `exit - fill`, `p_hist` e lift per semestre e anno con SE, test chi2 di omogeneita' fra semestri, saturazione in H (flag `drift_dominated` se il lift grezzo a H = 20 scende di oltre 0.02 rispetto a H = 15), `sigma_r`, `W + L` e `N_min` per edge 0.05/0.08/0.10/0.15, ATR_6h mediano in bps per prodotto.
    Output: `label_config.json` (con `sha256`, `selection`, spread di label e metriche dello studio) + `data/reports/feasibility.md`.

**G5. Sez. 3.1.4.**
Campionamento: sostituire "Con un orizzonte di 10 giorni le label si sovrappongono molto: ... quando si giudica la
significativita' statistica." con:

    Le label si sovrappongono molto: i pesi di unicita' (sotto) sono obbligatori, e per giudicare la significativita' statistica si usa `N_eff` dello studio di fattibilita' (sez. 3.1.3; `indep/yr` nel report), non il numero di righe.

Bilanciamento: sostituire "Pesi per sample = unicita' media della label (...), alla Lopez de Prado." con:

    Pesi per sample = unicita' media della label sul suo intervallo reale, alla Lopez de Prado: `[t, exit_idx]` per `edge` e `action` con fill, `[t, t+F+T_in-1]` per i `NO_FILL`, `[t, label_window_end(t)]` per `vol_regime`.

Split: sostituire "**purge** dei sample la cui finestra `[t, t+T_in+H]` interseca il periodo di test + **embargo** di 2 giorni." con:

    **purge** dei sample con `label_window_end(t)` (sez. 3.1.3) >= inizio del periodo successivo - embargo, con **embargo** di 2 giorni (`training/splits.py`, `DEFAULT_EMBARGO`).

Sostituire "**Holdout finale:** ultimi 120 giorni, non toccati fino al gate finale." con:

    **Holdout finale:** inizia a `selection.holdout_start` del `label_config` (fissato dallo studio di fattibilita': giorno UTC dell'ultima barra meno 120 giorni) e comprende tutto lo storico successivo, che quindi cresce nel tempo. Non e' mai usato per scegliere `label_config`, modello o soglie; `build_dataset` passa questo valore a `purged_walk_forward(holdout_start=...)`.

**G6. Sez. 3.4.** Sostituire la tabella dei gate (righe G1-G4; G5 e G6 restano) e il paragrafo "Se G3 fallisce, ..." con:

    | Gate | Criterio |
    |------|----------|
    | G0 Regime | Prima di ogni altro gate. Sull'holdout, con il `label_config` del modello e le barre grezze: `lift_holdout = p_breakeven - p_hist` (griglia oraria, sample con fill) <= `0.10 + 2 SE_holdout` (`N_eff` e `rho` come nello studio di fattibilita'). Se fallisce: NO-GO per cambio di regime, e lo studio va rifatto su dati nuovi. |
    | G1 Calibrazione | ECE (15 bin) <= 0.05 per `edge` e per la classe BUY di `action`, su ogni fold di test. Diagnostica senza soglia: ECE e bias firmato di `edge` sui soli sample con `p_edge >= p_edge_min`, sui fold di test concatenati. |
    | G2 Discriminazione | Log-loss di `edge` inferiore a quella del predittore a priori (frequenza base) su >= 4/5 fold. |
    | GE Evidenza assoluta | E1-E5 sui trade del backtest (fold di test concatenati, policy congelata) e sull'holdout; esito GO / INCONCLUSO / NO-GO. Calcolato per il candidato e per il baseline LightGBM. |
    | G3 Economico relativo | Solo se il candidato non e' il baseline. Backtest event-driven (sez. 3.5) con fee reali e slippage: PnL netto e Sharpe del modello > baseline LightGBM su >= 3/5 fold **e** sull'holdout. |
    | G4 Robustezza | Con `sentiment = null` forzato su tutto l'holdout, il PnL netto non scende sotto il 70% di quello con sentiment. |

    Criteri GE (OOS = fold di test concatenati; blocchi del bootstrap di `ceil_days(T_in + F + H)`, circolari sul tempo, tutti i prodotti nello stesso blocco; 10 000 repliche, seed fisso):
    - E1 campione: >= 150 trade OOS e `N_eff_trade` >= 100 (peso di ogni trade `1 / (1 + rho * c)`, con `c` = numero di altri prodotti con un trade sovrapposto);
    - E2 significativita': percentile 2.5% del block bootstrap della media di `net_ret` (bps, non pesata) > 0, e PnL OOS in EUR > 0;
    - E3 selezione: test di permutazione contro ingressi casuali dello stesso fold e prodotto (fill e fee floor superato, una posizione per prodotto, stesso numero di trade), sul `net_ret` della label alla barra di decisione: p <= 0.05;
    - E4 concentrazione: nessun fold oltre il 50% del PnL OOS e >= 3/5 fold con PnL > 0;
    - E5 holdout: >= 5 trade, PnL > 0 e `z = (m_h - m_oos) / sqrt(se_h^2 + se_oos^2) > -1.96`.
    Esito: G0 fallito -> NO-GO; E1 fallito -> INCONCLUSO se la media OOS e' > 0, altrimenti NO-GO; tutti ok -> GO; E2 fallito con `t = media / SE >= 1.0` e gli altri ok (E5 ok o non valutabile) -> INCONCLUSO; E5 non valutabile e gli altri ok -> INCONCLUSO; altrimenti NO-GO.

    Deploy: la rampa R0 (sez. 5.1.7) richiede G0, G1, G2, G4 superati e GE = GO per il decider del book champion, piu' G3 se il candidato e' un LLM. Con GE = INCONCLUSO il champion gira solo in dry, con capitale live zero, finche' la rivalutazione di GE sui trade OOS piu' i trade chiusi del book champion in dry non da' GO. Se G3 fallisce si va in produzione con il `BaselineDecider`, alle stesse condizioni sul suo GE: il modello LLM deve guadagnarsi il posto con i numeri, e anche il baseline deve dimostrare un edge assoluto prima di usare capitale reale. `training/evaluate.py` esce con 0 (GO), 5 (INCONCLUSO) o 1 (altro).

**G7. Sez. 3.5.**
Punto 1, prima riga: sostituire la firma con

    `triple_barrier(open, high, low, close, atr, t_idx, label_config: LabelConfig, fees: FeeRates, costs: CostParams, spread_bps) -> TripleBarrierResult(outcome, fill_idx, exit_idx, net_ret, entry_px)`

e aggiungere a fine punto:

    ; `label_window_end(ts_bar_close, label_config)` e `load_label_config(path)` (verifica dello `sha256`, exit code 2 se non coincide).

Punto 2b: "studio della sez. 3.1.3 con la regola di scelta" diventa "studio della sez. 3.1.3 (finestra pre-holdout, `lift_0`,
eleggibilita', pareggio su `N_eff`, jackknife, guardia per prodotto)".
Punto 3: sostituire con

    3. `training/splits.py` — `purged_walk_forward(timestamps, t1, n_folds=5, embargo=timedelta(days=2), holdout_days=120, val_frac=0.15, holdout_start=None)` che produce indici (`timestamps` = `ts_bar_close`, `t1` = `label_window_end`); `holdout_start_from(last_ts_bar_close, holdout_days)`; test che verifica l'assenza di sovrapposizione tra intervalli `[t, t1]` di train e di test.

Punto 8, aggiungere dopo "fee maker/taker da config)":

    ; decisione alla barra `t`, ordine prezzato a `close[t+F-1]` ed eseguibile dalla barra `t+F` (`entry_first_bar`), SELL del modello a `open[t'+F]`, nella barra di fill solo lo SL, tutto con i kernel di `modules/dryrun/fill_model.py`; ogni fold riparte da `backtest.starting_quote`; nell'ultimo fold nessun ingresso con `label_window_end >= holdout_start - embargo`. La trade list contiene `product, fold, decision_ts, decision_idx, fill_ts, exit_ts, exit_reason, net_ret_bps, pnl_eur, base_size, notional_eur`

Punto 11: sostituire con

    11. `training/evaluate.py` — esegue G0, G1, G2, GE (E1-E5), G3, G4 della sez. 3.4 senza fermarsi al primo fallimento e stampa una tabella; exit code 0 se tutto passa con GE = GO, 5 se GE = INCONCLUSO e il resto passa, 1 altrimenti.

**G8. Sez. 3.2.1**, nel blocco `meta`: `"atr_1d_bps": 460,` diventa `"atr_tf": "1d", "atr_bps": 460,`.

**G9. Sez. 3.2.4**, nel model card: l'oggetto `label_config` diventa

    "label_config": {"k_tp": 4.0, "k_sl": 2.0, "horizon_min": 14400, "atr": "ATR_14_1d", "atr_tf": "1d", "entry_mode": "maker",
                     "entry_timeout_min": 30, "entry_first_bar": 3, "cost_model": "COST_V1",
                     "fees_bps": {"maker": 60, "taker": 120, "source": "assumed"},
                     "slip_bps": 5, "stop_extra_slip_bps": 10,
                     "spread_bps": {"BTC-EUR": 3.0, "ETH-EUR": 3.0, "SOL-EUR": 3.0},
                     "selection": {"holdout_start": "2026-06-20T00:00:00Z"}, "sha256": "a41c..."},

**G10. Sez. 5.1.3**, nella tabella sostituire quattro righe:

    | Ingresso taker, parte marketable | Walk degli ask al primo snapshot del book dopo `t0 + latency_ms`, fino al prezzo limite. | `open[t+F] * (1 + spread/2 + slip_bps)`, con F = `entry_first_bar` | taker |
    | Ingresso maker (post-only, intero ordine) e residuo taker | Fill solo su trade con prezzo < limite (strettamente); `qty <= queue_haircut * volume_oltre_limite`. Se al momento dell'arrivo il limite incrocerebbe il book, l'ordine e' rifiutato come farebbe l'exchange con `post_only`. | Limite `close[t+F-1] * (1 - spread/2)`; fill se `low[k] < limit` per k in `[t+F, t+F+T_in-1]`, haircut sul volume della barra. | maker |
    | SL (stop) | Trigger su `last <= stop`; walk dei bid + `stop_extra_slip_bps`. | `min(stop, open) * (1 - (spread/2 + slip_bps + stop_extra_slip_bps))`; nella barra di fill vale solo lo SL (watchdog senza bracket). | taker |
    | Uscita del modello (SELL) / watchdog | Walk dei bid. | `open[t+F] * (1 - spread/2 - slip_bps)` per una decisione presa a `t`. | taker |

**G11. Sez. 5.1.5, 5.1.6, 5.1.7.**
5.1.5: "per ogni record con `t + H <= now`" diventa "per ogni record con `label_window_end(t) <= now` (sez. 3.1.3)".
5.1.6, primo paragrafo: sostituire "Con i parametri v1 (sez. 0.4) una label si risolve solo dopo `T_in + H` (circa 10 giorni) e
ci sono circa **36 finestre indipendenti per prodotto all'anno**. Quindi:" con:

    Con i parametri del `label_config` (sez. 0.4) una label si risolve solo a `label_window_end(t)` (circa 15.5 giorni con H = 15), e i campioni indipendenti per prodotto e anno sono `indep/yr` del report di fattibilita'. Quindi:

5.1.6, tabella L5: "con trade da 10 giorni e' rumore" diventa "con trade di piu' giorni e' rumore".
5.1.7: "(sul book `champion`, ... con circa 36 finestre per prodotto all'anno su 3 prodotti, 30 trade richiedono indicativamente
3–4 mesi)" diventa:

    (sul book `champion`, guidato dal numero di trade, valutato in continuo; il tempo necessario per 30 trade si ricava dalla frequenza di trade del backtest)

5.1.7, Rampa, riga R0, colonna Prerequisito: "G-DRY-OPS **e** gate G3 della sez. 3.4 sul backtest" diventa:

    G-DRY-OPS **e** gate della sez. 3.4 superati con GE = GO

**G12.** Cercare nel documento `ATR_14_1d` e `atr_1d`. Nelle definizioni normative (pseudocodice, testo delle regole) si
sostituiscono con `ATR_14_<atr_tf>`. Negli esempi numerici (0.3, 4.1.2, 4.2, 3.2.1, 3.2.4) restano: sono gia' marcati come
esempi da G2.

### H. Ordine di implementazione (Sonnet)

1. `labeling.py`: `entry_first_bar` (B1), barra di fill (B4), `label_window_end` (B5), `sha256` con spread (B7),
   `load_label_config` (B8) e relativi test.
2. `splits.py`: `holdout_start` e `holdout_start_from` (B9) e relativi test.
3. `feasibility.py`: C0-C8 e test C9. Poi rieseguire su dati reali con
   `python -m training.feasibility --products BTC-EUR ETH-EUR SOL-EUR --fees-from-probe data/probe/coinbase_probe.json`
   e allegare il nuovo `feasibility.md` (K1-K6).
4. Spec v2.7 (G1-G12). In G3 si aggiorna il candidato se il rerun sceglie 3/1.5; il rerun non e' un prerequisito delle
   altre modifiche.
5. `backtest.py` ed `evaluate.py` (D1-D6) quando si arriva a quei moduli: non bloccano `build_dataset`.
6. Svuotare questo file a lavoro concluso (protocollo CLAUDE.md, punto 6).
