# ESITO: ipotesi A (trend Donchian 50/20) e B (rotazione mensile momentum), braccio L

Autore: sviluppatore (Sonnet). Data: 2026-10-28. Tipo: esito di ricerca offline, nessun ordine, nessuna modifica al bot ne' al dry run. Documento di riferimento (congelato, approvato dall'utente il 2026-10-28): `docs/decisions/2026-10-28-ipotesi-strategie-laboratorio.md`. Codice: `script/study_trend_rotation.py` + `tests/test_study_trend_rotation.py` (27 test sintetici). Dati grezzi della corsa: `data/reports/study_trend_rotation/L/` (ignorata da git).

## 1. Verdetti

| Ipotesi | Verdetto | Motivo (regola di sez. 4 del documento) |
|---|---|---|
| A trend Donchian 50/20 | INCONCLUSO | n sotto il minimo in training: 27 trade contro 30 (holdout 22 contro 15: ok). Il segno non si guarda prima del minimo. |
| B rotazione momentum | INCONCLUSO | n sotto il minimo: training 23 (min 30), holdout 14 (min 15). |

C e D non fanno parte di questa corsa (serve il download 15m USD, curato da altri); entrano in Holm con p = 1 (caso peggiore del documento). Nessuna ipotesi e' GO: nessun book A o B arriva al capitale reale, e nessuna e' NO-GO: nessuna e' chiusa.

## 2. Procedura (verificabile nei file della corsa)

- Documento approvato e committato (36b494d) prima del codice; script e test committati (72f6285) prima della corsa; la corsa e' stata fatta con lo script pulito (rifiuto di girare altrimenti), una sola volta (`L/` non si sovrascrive).
- `00_preregistration.json` scritto prima di leggere qualunque barra: sha256 script `76634f7f...e029e0b`, documento `ebe6ab86...f6709`, parametri `1a75254f...d1600c`, dipendenza `study_volume_patterns.py` `23a17dc0...faa27`, commit `72f62850...`.
- Ordine di lettura: dati USD tagliati a 2020-12-31 in lettura -> `01_data`, `02_train_results` (con p-value e Holm) -> `03_train_hash` (sha256 di `02`: `e6650ee5...34549`, 16:51:07 UTC) -> SOLO ALLORA holdout 2021-01-01..2023-09-30 e replica EUR -> `04_holdout_results`, `05_secondary`, `06_decision`. Nessun rendimento stampato prima della decisione. I test provano questo ordine (eventi di lettura contro i file scritti) e che il training non dipende dai dati successivi.
- Dati: Coinbase BTC-USD 2015-07-20.., ETH-USD 2016-05-18.. (2 giorni mancanti, 2016-05-21/22, trattati come giorni non validi; una finestra con un buco non produce segnali). SOL-USD NON usato: il documento lo prevede solo per la replica EUR e il dry run (SOL-EUR da `data/bars/1m`), nessun download necessario. Replica EUR 2023-10-01..2026-10-19 (ultimo giorno completo in repo).

## 3. Numeri chiave (costi primari 110 bps a giro; rendimenti giornalieri netti; "ref" = riferimento della sez. 3)

### Ipotesi A (portafoglio BTC+ETH, sleeve 1/2 mai trasferite; statistica = media giornaliera netta contro cassa)

| | Training 2015-09..2020-12 | Holdout 2021-01..2023-09 | Replica EUR 2023-11..2026-10 (BTC/ETH/SOL) |
|---|---|---|---|
| trade (BTC / ETH) | 27 (15 / 12, di cui 2 chiusi a forza a fine periodo) | 22 (11 / 11) | 33 |
| netto medio giornaliero | +27,9 bps (+101,7% annuo aritmetico) | +4,8 bps (+17,7% annuo) | +4,9 bps |
| IC 90% | [+14,0; +43,8] bps | [-8,1; +17,8] bps | [-4,2; +14,4] bps |
| p (max fra bootstrap e HAC) | 0,00047 (grezzo) | 0,27 | 0,19 |
| Sharpe annualizzato (ref: buy and hold) | 1,88 (1,49) | 0,39 (0,61) | 0,61 (0,60) |
| drawdown massimo (ref: buy and hold stesse sleeve) | 54,7% (88,9%) | 59,2% (77,4%) | 41,0% (65,4%) |
| rendimento totale netto (ref: buy and hold lordo) | +10.256% (+9.321%) | +22,4% (+59,7%) | +48,8% (+70,1%) |
| senza l'anno migliore | +15,4 bps/giorno | -6,6 bps/giorno | -0,4 bps/giorno |
| rendimento medio per trade lordo / netto | +114,5% / +112,2% | +5,2% / +4,0% | +6,7% / +5,5% |

- p di Holm di A: 1,0, perche' un test con n sotto il minimo entra in Holm con p = 1 (convenzione dello studio sui volumi, scelta prima della corsa; il documento non la fissa). Con il p grezzo sarebbe 4 x 0,00047 = 0,0019 (famiglia m = 4, caso peggiore).
- Frequenza osservata: 27 trade in 5,3 anni, cioe' circa 2,5 per moneta all'anno, contro 5-8 stimati nel documento [S]: la stima ha sovrastimato di circa il doppio, ed e' questa la sola ragione dell'INCONCLUSO.

### Ipotesi B (rotazione mensile; statistica = media giornaliera di B netto meno B0 netto)

| | Training 2016-07..2020-12 | Holdout 2021-01..2023-09 | Replica EUR 2023-11..2026-10 |
|---|---|---|---|
| trade (periodi di possesso) / cambi di posizione | 23 / 34 | 14 / 21 | 15 / 22 |
| B meno B0, media giornaliera | +8,3 bps | -9,1 bps | +1,4 bps |
| IC 90% | [-0,5; +17,4] bps | [-15,3; -3,5] bps | [-7,2; +10,3] bps |
| p | 0,060 | 0,998 (segno opposto) | 0,41 |
| netto giornaliero di B / di B0 | +36,0 / +27,8 bps | +2,3 / +11,5 bps | +10,0 / +8,6 bps |
| rendimento totale B / B0 | +6.072% / +2.266% | -27,2% / +101,0% | +75,5% / +101,0% |
| drawdown massimo B / B0 | 77,0% / 76,9% | 75,4% / 60,6% | 71,0% / 46,7% |

- Lettura alternativa di "trade" (cambi di posizione: 34 in training, 21 in holdout, entrambi sopra i minimi): B resterebbe INCONCLUSO, perche' (a) e' falsa (p grezzo 0,060, con Holm peggiore) e la NO-GO richiede (a) vera. Il verdetto non dipende dalla lettura.
- La rotazione ha perso contro il semplice filtro di cassa 1/N (B0) nell'holdout, con IC interamente sotto zero: non e' un'ipotesi con edge nascosto da potenza bassa, in quel periodo e' peggiore del benchmark; il segno del training (+8 bps, non significativo) non si e' ripetuto.

## 4. Nota onesta su cosa mostrano i numeri (esplorativo, NON cambia il verdetto)

Se il minimo di 30 trade fosse stato raggiunto, A avrebbe comunque fallito: (a) sarebbe vera (0,0019), (c) vera (+15,4 bps senza l'anno migliore), ma (b) falsa (holdout +4,8 bps contro 1/3 del training = 9,3 bps) ed (e) falsa (drawdown dell'holdout 59,2% = 0,76 volte quello del buy and hold, soglia 0,6). In sintesi: nel training 2015-2020 il trend ha prodotto un netto enorme (e' il periodo dei bull 2017 e 2020, con conoscenza del percorso e sopravvivenza dichiarate nel documento); nell'holdout 2021-2023 ha dato un netto positivo ma piccolo, ha perso contro il buy and hold di circa 37 punti di rendimento e ha tagliato il drawdown solo del 24% (non del 40% richiesto). Sull'EUR 2023-2026 stesso quadro: Sharpe pari al buy and hold, drawdown 41% contro 65%, rendimento inferiore. Il valore di A, finora, e' ridurre il drawdown, non aumentare il rendimento: coerente con l'ipotesi economica del documento (sez. 3A) e con la probabilita' a priori del 25% di GO (sez. 7).

## 5. Conseguenze previste dal documento

- INCONCLUSO (sez. 4): nessun capitale reale; l'ipotesi resta "solo il book forward con uno sguardo pre-fissato". Per A e B questo significa i book A-trend e B-rotazione del dry run (sez. 5), con sguardo unico il 2027-10-28 o al raggiungimento del minimo forward (10 trade per A; 10 cambi per B, atteso 12-30 mesi), piu' il book HODL come riferimento. Il dry run non produce evidenza statistica per A e B entro un anno: misura lo scarto di implementazione.
- Nessuna variante di parametri sugli stessi dati: il documento chiude le varianti solo per il NO-GO, ma l'holdout USD e' stato usato e non e' riutilizzabile per modificare le regole (50/20, 28 giorni, 0,05) senza ricontaminarlo. Qualunque aggiustamento (per esempio un minimo di trade o una finestra diversi) sarebbe una nuova ipotesi con nuova pre-registrazione e nuovo holdout (il solo dato pulito ormai e' quello forward).
- Sez. 7, "se nessuna passa": A e B non cambiano il piano. Restano da eseguire C e D (braccio U, download 15m USD) prima di dichiarare chiusa la famiglia; l'opzione raccomandata resta il dry run con i book forward e nessun capitale reale su regole sistematiche finche' un book non supera entrambi i gate.
- Famiglia di Holm: l'esito di A e B non dipende da C e D (INCONCLUSO per n; B anche per (a) falsa con qualunque p di C e D), quindi un eventuale GO di C o D non lo modifica.

## 6. Limiti e convenzioni scelte nel codice (fissate prima della corsa, vedi docstring dello script)

- Esecuzione all'apertura di t+1, segnali con chiusure fino a t; ogni periodo (training, holdout, replica) parte in cassa e chiude a forza l'eventuale posizione aperta all'ultima chiusura (costo incluso): i 2 trade chiusi a forza del training e quello della replica B sono contati.
- Costo 110 bps a giro = 55 bps per acquisto e 55 per vendita; un cambio di moneta e' una vendita piu' un acquisto. Il costo secondario (77 bps) e' in `05_secondary.json`: stesse conclusioni (A: +28,1 / +5,2 / +5,3 bps; B meno B0: +8,5 / -8,9 / +1,6 bps).
- Sleeve 1/N a pesi fissi (la sleeve di ETH e' in cassa fino alla prima finestra completa, luglio 2016): in training il portafoglio di A e' mezzo investito nei primi mesi. B e B0 iniziano al primo ribilanciamento con R28 disponibile per tutte le monete (2016-07-01).
- Non calcolata la variante esplorativa di A con peso 1/sigma60 (il documento non definisce quando si ricalcola il peso). Esplorativo e per moneta: in `05_secondary.json`, non crea ne' riapre verdetti.
- Bias dichiarati nel documento (sopravvivenza, conoscenza del percorso) non eliminati; il training e' dominato dai bull 2017 e 2020.
