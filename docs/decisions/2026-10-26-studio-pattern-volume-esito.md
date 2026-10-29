# ESITO: studio pre-registrato drawdown + volume anomalo -> rendimento a 7/14/30 giorni

Data: 2026-10-26. Pre-registrazione: `docs/decisions/2026-10-26-studio-pattern-volume-prereg.md` (commit 8c126ad, righe "Braccio scelto" compilate prima del codice). Codice: `script/study_volume_patterns.py` + `tests/test_study_volume_patterns.py` (commit 81ac71da, congelato prima di ogni corsa; ogni corsa ha scritto `00_preregistration.json` con lo sha256 dello script, del documento e di `PREREG` prima di leggere una barra). Una corsa per braccio, nessun ritocco dopo i risultati. File: `data/reports/study_volume/{E,L}/00..06_*.json` (ignorati da git). Nessun ordine, nessuna modifica al bot o ai moduli di trading.

## 1. Verdetto

| | Braccio L (USD 2015/2016-2023, decide) | Braccio E (EUR 2023-2026, contaminato) |
|---|---|---|
| H1 7 g / 14 g / 30 g | INCONCLUSO / INCONCLUSO / INCONCLUSO (n insufficiente, `INS_n`) | INCONCLUSO / INCONCLUSO / INCONCLUSO (n insufficiente) |
| H2 (veto) 7 / 14 / 30 g | INCONCLUSO x3 (n insufficiente) | INCONCLUSO x3 (n insufficiente) |
| Globale (decide solo H1) | **INCONCLUSO** | **INCONCLUSO** |
| `holdout_clean` | vero (intervallo EUR esplorato 2023-10-01..2026-10-26, non tocca l'holdout USD) | falso (`unknown`) |

Il verdetto dello studio e' quello del braccio L (sez. 2.3): **INCONCLUSO**. Nessun GO, nessun NO-GO, nessun VETO. Le regole della sez. 10 sono state applicate senza modifiche: sotto 10 unita' nel training o 6 nell'holdout il verdetto e' INCONCLUSO prima di guardare il segno degli effetti.

## 2. Perche' INCONCLUSO: n molto sotto il minimo (e sotto la stima della sez. 12)

Unita' indipendenti (n_T / n_V; minimi 10 / 6):

| | 7 g | 14 g | 30 g |
|---|---|---|---|
| L, H1 | 3 / 3 | 3 / 3 | 3 / 3 |
| L, H2 | 7 / 2 | 7 / 2 | 5 / 1 |
| E, H1 | 4 / 5 | 4 / 5 | 3 / 3 |
| E, H2 | 4 / 2 | 4 / 2 | 3 / 2 |

La sez. 12 stimava per L circa 20 / 10 unita' a 7 g; il dato reale e' 3 / 3. Controllo di correttezza fatto dopo la corsa sulle sole feature (nessun rendimento, nessuna modifica): per BTC-USD nel training 359 giorni hanno DD180 <= soglia (Q0,20 = -41,8%) e 359 hanno VR >= soglia (Q0,80 = 1,63), ma solo 22 sono congiunti (14 nel 2018, 8 nel 2020; zero nel 2017 quando il volume era alto senza drawdown, zero nel 2019 quando il drawdown c'era con volume basso, VR mediano 0,69-0,74). Le due condizioni sono quasi disgiunte nel tempo nei dati veri: la stima della sez. 12 assumeva indipendenza. ETH-USD ha soglie piu' profonde (DD180 -59,5%) e zero giorni segnale nell'holdout; BTC-USD ne ha 15 (3 episodi, 1 unita' per orizzonte lungo).

Dati (da `01_data.json`): BTC-USD 2015-07-20..2026-10-25, 4.116 giorni validi, 0 non validi (primo giorno idoneo 2016-02-03); ETH-USD 2016-05-18..2026-10-25, 3.811 validi, 2 non validi in 1 tratto (idoneo dal 2016-12-04); replica BTC-EUR / ETH-EUR 2023-10-01..2026-10-19, 1.114 giorni validi, 1 non valido ciascuno; braccio E: SOL-EUR 2 giorni non validi. Episodi fuori dagli split per purge o uscita mancante: 1 per ipotesi e orizzonte nel braccio L, 0 nel braccio E. I termini d'uso Coinbase non sono stati letti (HTTP 403 ai client automatici); scaricati solo candele giornaliere pubbliche, uso interno, nessuna ridistribuzione (`terms_verified = false` in `01_data.json`).

## 3. Numeri chiave di H1 (in bps; netto = lordo - 77; IC al 90% conservativi = min/max di bootstrap a cluster e t)

Braccio L (decide):

| H | split | n unita' | episodi | netto medio | IC 90% netto | eccesso medio |
|---|---|---|---|---|---|---|
| 7 | training | 3 | 5 | +459 | [-3.243, +4.162] | +229 |
| 7 | holdout | 3 | 3 | -75 | [-1.120, +971] | -23 |
| 14 | training | 3 | 5 | +969 | [-3.279, +5.217] | +400 |
| 14 | holdout | 3 | 3 | -332 | [-1.046, +382] | -298 |
| 30 | training | 3 | 5 | +489 | [-4.971, +5.948] | -1.021 |
| 30 | holdout | 3 | 3 | -339 | [-391, -286] | -352 |

Replica EUR (L, 1 unita' per orizzonte, non valutabile): netto +382 / +591 / +796. p_Holm di training = 1 per tutti e 6 i test (n sotto il minimo). MAE mediano 7 g: -2.057 bps (training), -1.531 (holdout).

Braccio E (descrittivo, non decide):

| H | split | n unita' | episodi | netto medio | IC 90% netto | eccesso medio |
|---|---|---|---|---|---|---|
| 7 | training | 4 | 8 | +273 | [-819, +1.364] | +264 |
| 7 | holdout | 5 | 10 | -139 | [-923, +645] | -29 |
| 14 | training | 4 | 8 | +191 | [-1.358, +1.739] | +89 |
| 14 | holdout | 5 | 10 | -27 | [-773, +719] | +119 |
| 30 | training | 3 | 8 | -238 | [-2.953, +2.476] | -579 |
| 30 | holdout | 3 | 10 | -275 | [-1.728, +1.179] | -81 |

H2 (veto, eccesso): L training 7 / 14 / 30 g: -138 / -456 / -657 (n 7 / 7 / 5), holdout +718 / +853 / +1.371 (n 2 / 2 / 1); E training +206 / +307 / +222 (n 4 / 4 / 3), holdout +248 / +264 / -216 (n 2 / 2 / 2). Nessuno e' interpretabile con questi n.

Gli intervalli sono larghi quanto l'effetto: i dati sono compatibili sia con un effetto abbastanza grande da pagare 77 bps sia con zero o negativo (definizione di INCONCLUSO, sez. 10).

## 4. Secondarie (esplorative, non riaprono nulla; `05_secondary.json`)

Nessuna variante crea un GO ne' riapre un verdetto. Solo per completezza: S7 (solo drawdown, senza volume) in L ha piu' unita' (training 19 a 7 g, netto +550, IC [+16, +1.083]; holdout 22 unita', netto -59, IC [-422, +303]): il segno del training non si ripete nell'holdout, quindi non e' un indizio da inseguire. S2 (volume in unita' di moneta) in L: n 11 / 7 a 7 g, netto +746 / -31, IC che includono 0. S1 (DD90, DD365), S5 (ingresso O_(t+2)), S4 (110 bps), S6 (regime di volatilita'), bootstrap a blocchi sul calendario: tutti con IC che includono 0 sull'eccesso nell'holdout. S9 (SOL): non calcolato nel braccio L (SOL-USD non scaricato), nel braccio E SOL e' una delle tre monete (campo `by_coin`).

## 5. Conseguenze (sez. 11)

- INCONCLUSO (decide L): nessun capitale, nessuna strategia giornaliera, nessuna variante sugli stessi dati (DD90/DD365, altri quantili, altri orizzonti, altre fonti di volume). E non e' un NO-GO: l'idea non e' chiusa, ne' autorizzata.
- Un solo riesame pre-programmato, con lo script congelato (commit 81ac71da): quando i dati EUR in avanti dal 2026-10-27 danno >= 6 nuove unita' a H = 7 per H1, oppure al 2028-10-26 se prima; le nuove unita' sono un secondo holdout (solo le regole 3(b)-(e)); un solo sguardo. Dato che nei dati EUR i giorni segnale di H1 sono circa l'1% (o meno) dei giorni per moneta e le condizioni sono quasi disgiunte, e' probabile che valga la data del 2028-10-26.
- Il braccio E (CANDIDATO escluso per `holdout_clean = falso` e n sotto il minimo) e' trattato come INCONCLUSO e non modifica la decisione di L.
- Nessun impatto su `RiskManager`, `BalanceGuard`, sul bot o sul dry run in corso.

## 6. Nota di processo

Un difetto e' stato trovato e corretto PRIMA del commit del codice, con l'unico controllo su dati sintetici (replica a 2 monete): la cache dei rendimenti futuri era indicizzata per moneta e mescolava la serie principale e la replica della stessa moneta (braccio L). Corretto con una chiave che include la serie. Nessuna modifica al codice dopo il commit 81ac71da ne' dopo aver visto i risultati.
