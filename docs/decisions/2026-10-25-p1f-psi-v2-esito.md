# ESITO SIM-P1f (2026-10-25): `rejected` - nessuna regola PSI-v2 adottata

Complemento di `docs/decisions/2026-10-25-p1f-psi-v2.md` (decisione e pre-registrazione, non modificata dopo la corsa,
commit `7c83cf6`). Questo file contiene solo cio' che la corsa ha prodotto. Regola di decisione applicata: D0-D3 del
documento di decisione, scritta prima di leggere una barra; nessun parametro, bin, alfa, soglia o feature e' stato
cambiato dopo aver visto i numeri.

## Corsa

- Comando (una sola corsa vera): `python -m script.sim_p1f run --config C:\Users\faric\Documents\sviluppo\chronos-dryrun\config.yml --out-root data/reports/sim_p1f`.
  La config e' quella del dry run in sola lettura (registry, barre e `label_config` del champion in uso); i file del dry run
  non sono stati scritti. Exit code 1 (`rejected`).
- Champion: `baseline-lgbm@2026-10-18.2`, `label_config` sha256 `76950f84...523bf5` (quella estesa a XRP-EUR), prodotti di
  training BTC-EUR, ETH-EUR, SOL-EUR, serviti anche XRP-EUR, `sentiment_available` false, `holdout_start` 2026-06-20.
- Artefatti: `data/reports/sim_p1f/baseline-lgbm@2026-10-18.2/00_preregistration.json` (sha256 `c368f788...e94dd1`,
  scritta alle 07:55:04Z prima di leggere le barre) e `10_calibration.json` (sha256 `6956308e...160dd0`, 07:56:18Z).
  La pre-registrazione registra: commit `7c83cf6`, sha256 del documento di decisione `03965918...618b08` (committato e non
  modificato), sha256 di `script/sim_p1f.py` `b6ccd872...077d02` e di `modules/dryrun/drift.py` `45501140...071686`.
  `20_s6.json` non esiste: S6 si esegue solo se la regola e' `adopted`.
- Riferimento R: 63.984 stati orari (BTC 21.336 dal 2024-01-13, ETH 21.336 dal 2024-01-13, SOL 21.312 dal 2024-01-14) fino al
  2026-06-20 escluso; 42 feature candidate, nessuna esclusa per pochi bordi; `btc_missing_rows` = 0 per BTC, ETH, SOL
  (A8 dell'architetto confermata: il percorso batch non produce mai il contesto BTC mancante).

## S0 - la diagnosi e' riprodotta (D0 superata)

La vecchia funzione (10 decili, floor 1e-4, prodotti aggregati, stati al minuto di un giorno contro gli stati orari di
`[giorno + 1 - 90 g, giorno)`) su 889 giorni del periodo di R: **889 giorni su 889 con almeno una feature sopra 0,10**.
Mediane a un giorno delle 11 feature lente: `rsi_14_1d` 5,85, `atr_1d_pctile_365d` 5,78, `macd_hist_1d_bps` 5,77,
`atr_14_1d_bps` 5,71, `btc_rv_ratio_1d_30d` 5,68, `atr_14_6h_bps` 4,67, `hh_ll_pos_30d` 3,99, `p_breakeven` 3,94,
`ret_7d_bps` 3,72, `rv_ratio_1d_30d` 3,24, `ret_3d_bps` 3,05; nessuna sotto 1 (soglia di D0: meno di 6 sotto 1).
`clock_dow` vale 8,23 a un giorno (il tetto ~8,3) e ~0 a 7 giorni; `seq_1d_bps_5` 5,77 a un giorno. La stessa funzione su
380 finestre non sovrapposte di 7 giorni contro R: ancora 380 su 380 con almeno una feature sopra 0,10 (le lente restano a
3,0-5,9 di mediana: il difetto non e' solo la lunghezza del campione ma la persistenza). Le feature veloci stanno vicino a
0,1-0,5 (`ret_1h_bps` mediana 0,13, `ret_6h_bps` 0,49 a un giorno): come nel dry run vero.

## S1 - indipendenti per settimana

`n_eff_7d` = 168 / tau (tau integrato dell'autocorrelazione oraria, media sui prodotti applicabili):
168,0 per le 14 feature giudicate (`ret_15m_bps`, `ret_1h_bps`, `seq_1h_bps_00..11`: tau = 1, i rendimenti orari non hanno
autocorrelazione lineare); 28,4 `ret_6h_bps`; 10,7 `dist_vwap_1d_bps`; 7,2-7,5 `seq_1d_bps_*`, `ret_1d_bps`,
`btc_ret_1d_bps`; 6,2 `rsi_14_1h`; 2,2-2,6 `rv_ratio_1d_30d`, `ret_3d_bps`, `vol_z_1d`, `btc_rv_ratio_1d_30d`; 0,6-1,6 per
le altre 12 (lente: `atr_1d_pctile_365d`, `atr_14_1d_bps`, `rsi_14_1d`, `hh_ll_pos_30d`, `p_breakeven`, `macd_hist_1d_bps`,
`atr_14_6h_bps`, `bb_pos_1d`, `ret_7d_bps`, `rsi_14_6h`, `atr_14_1h_bps`, `rv_1d_bps`). Le feature con `n_eff_7d` >= 50, quelle
su cui D2 e D3 giudicano, sono quindi le sole 14 veloci; per tutte le altre (28) nessun test di una settimana puo'
certificare "nessuna deriva", come il quant aveva dichiarato.

## S2-S5 e D1-D3

| | W = 7 giorni | W = 14 giorni (unica ripetizione) |
|---|---|---|
| finestre nulle (3 prodotti, passo 1 giorno) | 2.648 | 2.627 |
| `c` (quantile 1% del minP) | 0,001133 | 0,001142 |
| **D1** FWER fuori campione per prodotto (soglia 2%) | BTC 3,06%, ETH 2,94%, SOL 2,95%: **non superata** | BTC 4,68%, ETH 3,77%, SOL 4,46%: **non superata** |
| giorni in allarme ogni 30 (da FWER_oos) | 0,92 / 0,88 / 0,88 | 1,40 / 1,13 / 1,34 |
| **D2** `x_f` <= 0,25 per le 14 feature giudicate | `x_f` 1,06 (`ret_15m_bps`) - 1,27 (`ret_1h_bps`, `seq_1h_bps_11`): **non superata** | `x_f` 0,74 - 0,84: **non superata** |
| **D3** potenza a delta = 1 sigma >= 80% | 0,89 - 0,98: superata | 1,00: superata |
| potenza a delta = 0,5 (veloci) | 0,07 - 0,13 | 0,20 - 0,31 |
| allarmi XRP-EUR (fuori training, S5) | 48 finestre su 882 (5,4%) | 48 su 875 (5,5%) |

Allarmi per blocco semestrale a 7 giorni (finestre del blocco, `c_b` del blocco): 14/545, 13/546, 17/546, 13/546, 22/465
(attesi ~1%: 5, 5, 5, 5, 5). Potenza a delta = 1 delle feature lente a 7 giorni: 0,03-0,43 (nessuna
potenza), delle veloci 0,84-0,98; delle medie 0,06-0,48.

Decisione (funzione `decide`): D0 vera; W = 7: D1 falsa, D2 falsa -> W = 14: D1 falsa, D2 falsa -> **`rejected`**,
`window_days` null, `c` null, nessun riferimento adottato. `10_calibration.json` conserva sotto
`reference_diagnostic` il riferimento della finestra di 7 giorni (solo diagnostico: `load_calibration` lo ignora).

## Lettura (non e' una modifica della regola)

1. D0, D3 e la lettura del nullo confermano la diagnosi del quant sul vecchio criterio e che una deriva grande delle
   feature veloci si rileva (potenza >= 89% a 1 sigma).
2. D2 non e' mai stata raggiungibile con questi dati per le feature veloci: il 99% del PSI nullo di una settimana di
   rendimenti orari e' 1,06-1,27, non ~0,10-0,25 come nella stima [S] del quant (che assumeva campioni quasi i.i.d.,
   `E[PSI | H0]` ~ 0,01-0,07). I rendimenti orari non hanno autocorrelazione lineare (tau = 1) ma la loro SCALA segue il
   regime di volatilita', che e' persistente per settimane: una settimana in un regime di volatilita' diverso da quello
   medio del training sposta la distribuzione dei rendimenti di piu' di quanto lo faccia il campionamento. Il nullo
   empirico lo incorpora (ed e' per questo che `x_f` e' alto e la regola e' oggettivamente poco sensibile
   a delta = 0,5), ma D2 chiedeva che la regola non diventasse "cieca proprio dove i dati bastano".
3. D1: il minP calibrato al 1% sull'intero periodo da' 2,9-3,1% fuori campione a 7 giorni (4-5% a 14): il nullo non e'
   stazionario fra semestri (stessa causa: regimi di volatilita'), quindi le soglie di un semestre non valgono per
   l'altro. Con ~5 blocchi semestrali l'errore standard della stima per prodotto e' ~0,6%: lo scarto da 2% non e' rumore.
4. Cio' che NON si fa ora: cambiare alfa, bin, smoothing, soglie D1-D3, finestre, escludere feature o normalizzare per
   volatilita' (sarebbe misurare un'altra cosa, e i dati di R sono ormai stati visti). Va deciso da trader-quant e
   architetto in un conflitto nuovo.

## Conseguenze

- `feature_drift` resta `unmeasured` (motivo `calibration_rejected` se la cartella e' letta da L0, `no_calibration`
  altrimenti); G-DRY-OPS resta al massimo INCOMPLETE e non chiude P3 (comportamento voluto: non misurato non e' superato).
- Il vecchio PSI non e' un criterio di nulla: resta la diagnostica `feature_psi_daily_v1` (Q2 del quant, implementata).
- Nessuna azione e' collegata a PSI (la guardia `drift` di `SystemGuards` non ha scrittore).
- Aperto il conflitto P1f-bis (`.claude/CONFLITTO.md`) con questi numeri.
- La regola di contesto BTC (Q3) e' indipendente da questo esito ed e' in vigore: vedi la verifica sui dati veri del
  24-ott (12 righe `live_missing`, `max_shift` 0,0015-0,0035, `policy_untunable`).
