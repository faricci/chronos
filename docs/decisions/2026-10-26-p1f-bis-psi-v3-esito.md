# ESITO SIM-P1f-bis (2026-10-26): `rejected` - PSI-v3 non adottata (D1 per 0,04 punti su ETH-EUR)

Complemento di `docs/decisions/2026-10-26-p1f-bis-psi-v3.md` (decisione e pre-registrazione, commit `2f7081a`, non
modificata dopo). Questo file contiene solo cio' che la corsa ha prodotto. La regola e' l'ULTIMA per `feature_drift`: nessun
parametro, bin, alfa, soglia, finestra, famiglia o feature e' stato cambiato dopo aver visto i numeri.

## Corsa

- Comando (una sola corsa vera, 2026-10-26 08:53Z): `python -m script.sim_p1f_v3 run --config C:\Users\faric\Documents\sviluppo\chronos-dryrun\config.yml --out-root data/reports/sim_p1f`.
  Config del dry run in sola lettura (registry, barre, `label_config`); i suoi file non sono stati scritti. Exit code 1.
- Congelamento verificato dal codice: documento e `script/sim_p1f.py`, `script/sim_p1f_v3.py`, `modules/dryrun/drift.py`
  tracciati e puliti, commit `cbf6b91`. `00_preregistration.json` (sha256 `094993c1...d70c95`, scritta alle 08:53:05Z prima di
  leggere le barre) registra: sha256 del documento `f7f4af7b...70bf0e`, del codice `ecbe4c71...c12de` (sim_p1f), `07978772...a092`
  (sim_p1f_v3), `8d341fca...32d70c` (drift), `run_day` 2026-10-26, finestra di D1h `[2026-06-20, 2026-10-19)`.
  `10_calibration.json`: sha256 `bd0afb6f...0495a`, cartella `data/reports/sim_p1f/PSI_V3_VOLSTD/baseline-lgbm@2026-10-18.2/`.
  `20_s6.json` non esiste (S6 solo dopo `adopted`).
- Champion `baseline-lgbm@2026-10-18.2`, `label_config` `76950f84...`, training BTC-EUR, ETH-EUR, SOL-EUR; serviti anche XRP-EUR.
  R identico a PSI-v2: 63.984 stati orari, 2024-01-13 -> 2026-06-20; `btc_missing_rows` 0.

## Risultati

- Famiglia J: le 14 `z` (`z_ret_15m_bps`, `z_ret_1h_bps`, `z_seq_1h_bps_00..11`), tutte `n_eff_7d` = 168,0; nessuna esclusa per pochi
  bordi. Le 28 feature non testate hanno `n_eff_7d` da 0,6 a 28,4 (12 sotto 1,7) e restano in diagnostica.
- Finestre nulle di 7 giorni: 2.648; `c` = 0,002265.
- **D1 (FWER fuori campione <= 2%): NON superata.** BTC-EUR 1,47%, ETH-EUR 2,04% (2,0385%), SOL-EUR 1,02%. Giorni in allarme
  ogni 30: 0,44 / 0,61 / 0,31. Allarmi per blocco semestrale (finestre del blocco): 8/545, 4/546, 13/546, 13/546, 2/465.
- D1h (holdout, 51 settimane non sovrapposte 2026-06-20 -> 2026-10-19, k_H = 2): **superata**, 0 allarmi.
- D2 (`x_f` <= 0,25): **superata**, `x_f` 0,179 - 0,205 (era 1,06 - 1,27 con PSI-v2).
- D3 (potenza a 1 sigma_z >= 80%): **superata**, 1,00 per tutte le 14 (a 0,5 sigma_z: 0,98 - 1,00).
- XRP-EUR (fuori training): 3 allarmi su 882 finestre (0,34%).
- Decisione `decide_v3(j_nonempty=True, d1=False, d1h=True, d2=True, d3=True)` = **`rejected`**, definitiva.
  `10_calibration.json` conserva `reference_diagnostic` (solo diagnostico) e, SEMPRE, `diagnostic_reference` (42 feature grezze con
  il loro nullo), `scope` e `p_edge_reference` (8.712 stati dell'holdout, p_edge in [0,259, 0,401]).

## Lettura (non e' una modifica della regola)

1. La standardizzazione per volatilita' ha fatto cio' che il quant prevedeva: `x_f` da ~1,2 a ~0,19, potenza a 1 sigma_z e a 0,5 sigma_z
   quasi piena, XRP quasi mai in allarme, nessun allarme sull'holdout. D2, D3 e D1h sono superate con ampio margine.
2. D1 fallisce per UN prodotto di UN allarme: ETH-EUR 2,0385% contro 2% (~883 finestre per prodotto: 18 allarmi invece dei 17
   ammessi; BTC 1,47% e SOL 1,02% sono dentro). L'errore standard della stima per prodotto e' ~0,47%: lo scarto non e' distinguibile dal
   rumore, ma la regola di decisione era scritta in modo stretto e la risposta del trader-quant dice che `rejected` e' definitivo.
3. Cio' che NON si fa: arrotondare, cambiare alfa (1% -> 0,8%), cambiare la soglia D1, cambiare il blocco o rieseguire con un'altra
   variante. Chiunque lo facesse dopo aver visto questo numero starebbe calibrando sul risultato. Se si volesse riconsiderare D1 serve una
   nuova regola scritta da trader-quant e architetto, con l'avvertenza che i dati di R e dell'holdout sono ormai stati visti.

## Conseguenze (come da risposta dell'architetto, sez. J e A1)

- Blocco `feature_drift` = `not_testable` (reason `calibration_rejected_final`), definitivo per la regola; con la diagnostica
  (`support`, percentile del PSI delle feature non testate, PSI di `p_edge` sull'holdout) che L0 produce comunque.
- Il criterio di gate resta `unmeasured` (nota `not_testable`): G-DRY-OPS al massimo INCOMPLETE e non chiude P3, finche' non esiste un
  criterio di calibrazione di `p_edge` sugli esiti in un gate richiesto per P3. Aperto il **CONFLITTO P1g** (`.claude/CONFLITTO.md`).
- `btc_context_alignment` e' invece misurabile e `pass` con il champion `untunable`: verifica sui dati veri delle notti 24 e 25-ott
  (copia dei report in scratchpad, dry run non scritto): 12 righe `live_missing` (ETH, SOL, XRP, quota 0,28%), `max_shift`
  0,0015 - 0,0035, `flips` 0 con `flips_basis = policy_untunable` (zero BUY e zero intent dei book `champion`/`challenger`).
  Il criterio conta i BUY dei soli book del modello: il book `random_entry` (benchmark con BUY forzati, stessa `policy_version`
  del champion) ha 2 intent il 24-ott e non e' il modello; vedi il dubbio in P1g.
- R3 (`revision_decision_flips`) resta `unmeasured` con policy non tarabile (decisione del 2026-10-23, non estesa).
