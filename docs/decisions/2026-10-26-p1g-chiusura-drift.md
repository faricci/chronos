# DECISIONE P1g (risolta il 2026-10-26) - chiusura di `feature_drift` con PSI-v3 respinta

## Contesto

SIM-P1f (PSI-v2) e SIM-P1f-bis (PSI-v3) sono state respinte (`docs/decisions/2026-10-25-p1f-psi-v2-esito.md`,
`2026-10-26-p1f-bis-psi-v3-esito.md`); PSI-v3 per un solo allarme su ETH-EUR (2,04% contro 2%), con D1h, D2 e D3 superate. Per
`baseline-lgbm@2026-10-18.2` `feature_drift` e' `not_testable`, definitivo: nessuna terza regola. Il CONFLITTO P1g chiede
come chiudere G-DRY-OPS senza un criterio di deriva certificato.

## Decisione

Regola del trader-quant (sez. 1), valida per OGNI champion, attuale e futuri, fissata prima dei loro dati:
1. PSI-v3 `adopted` per la versione: `feature_drift` pass/fail come oggi.
2. PSI-v3 `rejected` (`not_testable`): il criterio diventa `not_covered` (non e' mai `pass`; non blocca il gate; e' elencato nel
   risultato) se in OGNI giorno della finestra (a) la sorveglianza `feature_drift_watch` e' misurata per ogni prodotto, con il
   riferimento e la soglia diagnostici della corsa respinta (`reference_diagnostic`, `c_diagnostic`), solo allarme, con i falsi
   allarmi fuori campione dichiarati; (b) `support` e PSI di `p_edge` contro l'holdout sono presenti; (c) la nota elenca cio' che
   non e' coperto. Altrimenti `unmeasured`.
3. Il PSI-v3 si applica a un nuovo champion con il suo R, `holdout_start` e cartella, stessa `RULE_V3`, una corsa: applicazione
   meccanica. Non e' ammesso cambiare una costante, rieseguire sulla stessa versione, o scegliere il champion guardando l'esito.
4. La calibrazione di `p_edge` sugli esiti NON entra in G-DRY-OPS ne' in P3 (matura in mesi).
Rischio residuo (quant): la regola e' scritta dopo il fallimento di PSI-v3; e' accettabile solo perche' vale per tutti i
champion futuri ed e' fissata prima dei loro dati. Con il champion attuale P4 e' comunque bloccato da GE != GO, non da questo.

## Differito a P4 (solo spec, nessun codice ora; prima del primo ordine reale)

- D1: campo `decider` nel `DecisionRecord` (le prove `untunable` filtrano `registry:*` invece di `MODEL_BOOKS`).
- D2: `flips_basis` su R3 (condizioni (a)-(d) del quant, solo versioni `untunable` esplicite).
- D3: G-DRY-ECON con `edge_calibration_mean` d >= -0,05 ed `edge_ece` <= 0,10 sullo stesso insieme, `unmeasured` sotto `n_eff` 200.
- D4: guardia `drift` per prodotto (solo versioni `not_covered`), fail-closed, solo ingressi.
- D5: stop di calibrazione in R0 (con `n_eff` >= 30, d + 1,645 se < -0,05: ritorno in dry).
- Regola di rampa: una versione `not_covered` non entra in R0 se D4 e D5 non sono implementati e testati.

## Correzioni dell'architetto

- B1: la guardia `drift` esistente e' globale e senza scadenza, non quella per prodotto: differita a P4.
- B2: la nota dice che guardia per prodotto e stop di calibrazione sono obbligatori in R0 e non ancora implementati.
- B3: con un champion `untunable` R3 resta `unmeasured` e G-DRY-OPS INCOMPLETE anche con `feature_drift` `not_covered` (accettato).
- B4: `n_eff` della calibrazione = somma dei pesi di unicita' per prodotto / (1 + (K - 1) rho), insieme deduplicato per
  (versione, prodotto, ts).

## Certificazione P0

Criterio "barre REST" di P0 (R1 su blocchi di 1.440 stati, R2, R4; R3 non ne fa parte) sui giorni UTC interi del dry run, senza
ricertificare il soak del 18-20/10 (decisione U2). UNA corsa: `python -m modules.dryrun.revision_impact certify --days 2026-10-24
2026-10-25 --products <prodotti serviti dalla config del dry run> --model-dir <models_dir>\registry\baseline-lgbm@2026-10-18.2
--subset final --label-config <stessa cartella>\label_config.json --bars-root <data_dir>\bars\1m --config
C:\Users\faric\Documents\sviluppo\chronos-dryrun\config.yml --out data/reports/p0_bars_certification_dry_2026-10-24_2026-10-25.json`.
`fail` o `unmeasured` si documentano senza ritocchi.

## Esito P0

UNA corsa (2026-10-26), file `data/reports/p0_bars_certification_dry_2026-10-24_2026-10-25.json` (sha256 `88261afc...d`), schema
`chronos.p0_bars_certification.v2`, config e dati del dry run in sola lettura, prodotti BTC-EUR, ETH-EUR, SOL-EUR, XRP-EUR,
champion `baseline-lgbm@2026-10-18.2` (policy `untunable`), finestra `[2026-10-24 00:00Z, 2026-10-26 00:00Z)`, 2.880 stati per prodotto.

**`p0_bars_criterion` = `fail`** (exit 1; anche con la regola v1). Nessun ritocco.

| prodotto | R1 | R2 (max m) | R4 | note |
|---|---|---|---|---|
| BTC-EUR | pass (0 stati spostati) | pass (0,0037) | pass (0/0) | 4 minuti rivisti |
| ETH-EUR | pass (0) | pass (0,0046) | pass (0/0) | 21 minuti rivisti |
| SOL-EUR | pass (0) | pass (0,0028) | pass (0/0) | 6 minuti rivisti |
| XRP-EUR | **fail** | pass (0,0157) | pass (0/0) | 65 minuti rivisti (3,4%), 16 stati spostati |

R1 di XRP-EUR per blocco di 1.440 stati: `extended_forward` (il 24-ott ha 1.439 stati, l'ultima barra chiude a mezzanotte)
12 stati spostati = 0,83% contro lo 0,5% ammesso; `day` 4 (0,28%); `extended_back` (un solo stato del 26-ott) 4 (0,28%). R3
`unmeasured` per tutti (policy `untunable`; non fa parte di P0). Il criterio P0 "barre REST" NON e' certificato: XRP-EUR
e' fuori dai prodotti di training (R1 era gia' vicino al limite il 23-ott: 0,417% con max m 0,0198). BTC, ETH e SOL, i tre
prodotti di training, passano tutti i criteri.

## Addendum (2026-10-26, dopo il fallimento su XRP-EUR): P0 sui tre prodotti del modello

Decisione dell'UTENTE, presa DOPO aver visto il `fail` di XRP-EUR: il criterio P0 "barre REST" si certifica su BTC-EUR, ETH-EUR e
SOL-EUR, l'universo reale del modello (`model_card.data.products`). XRP-EUR non fa parte del training: sta nel dry run come prova
fuori campione e resta SOLO diagnostica, non entra nel criterio P0. Ragioni: il criterio serve a dire se i dati che il modello
consuma sono stabili; per un prodotto che il modello non serve non c'e' skew train/serve da proteggere; e XRP ha una struttura di
mercato diversa (65 minuti rivisti su 2.880, 3,4%, contro 4-21 degli altri). La restrizione e' una scelta di perimetro dichiarata,
presa dopo il risultato e non prima: lo si scrive qui perche' non sembri una regola pre-registrata.

Il risultato su XRP-EUR resta conservato e non si riscrive: `data/reports/p0_bars_certification_dry_2026-10-24_2026-10-25.json`
(sha256 `88261afc...`), R1 `fail` (blocco `extended_forward` 12/1.440 = 0,83% contro 0,5%; 16 stati spostati su 2.880; max m 0,0157;
R2 e R4 pass). XRP-EUR continua a essere misurato ogni notte da L0 (`bars.XRP-EUR.revision_impact`) come diagnostica.

Seconda corsa (UNA, stesso comando con `--products BTC-EUR ETH-EUR SOL-EUR`, file nuovo
`data/reports/p0_bars_certification_dry_2026-10-24_2026-10-25_btc_eth_sol.json`, sha256 `4405a21b...`): **`p0_bars_criterion` = `pass`**
(exit 0, anche con la regola v1). 2.880 stati per prodotto, R1 pass con 0 stati spostati, R2 pass (max m 0,0037 / 0,0046 / 0,0028),
R4 pass (0 minuti comparsi o scomparsi), R3 `unmeasured` (non fa parte di P0). Revisioni di valore: 4 / 21 / 6 minuti.

Altre due decisioni dell'utente dello stesso giorno: (a) `gate_ops.window_start` (chiave di config, default null): i giorni precedenti
non entrano nella finestra del gate; per il dry run il primo giorno intero sotto supervisore e' il 2026-10-24; (b) il controllo delle
barre duplicate o fuori ordine di `OpsRecorder` e' per-processo (a un riavvio la coda flat ripubblicata non e' un'anomalia); il file ops
del 23-ott non si ricalcola.

## Allegato: CONFLITTO P1g

# CONFLITTO P1g - calibrazione di p_edge sugli esiti in un gate richiesto per P3

Autore: Sviluppatore (Sonnet). Data: 2026-10-26. Stato: DA RISOLVERE. Prima `trader-quant`, poi `architetto-opus`. Il conflitto P1f-bis e' risolto e archiviato in `docs/decisions/2026-10-26-p1f-bis-psi-v3.md` (decisione e pre-registrazione) e `docs/decisions/2026-10-26-p1f-bis-psi-v3-esito.md` (numeri). Vincoli: solo spot, nessuna leva, copertura 100%; i gate non si allentano; la regola PSI-v3 e' l'ULTIMA per `feature_drift` e NON si ritocca.

## Fatto

- SIM-P1f-bis (una sola corsa, 2026-10-26 08:53Z, codice congelato e tracciato, commit `cbf6b91`): **`rejected`**. D2, D3 e D1h superate con ampio margine (`x_f` 0,179-0,205 contro 0,25; potenza 1,00; 0 allarmi su 51 settimane di holdout con k_H = 2). **D1 non superata per un solo allarme**: ETH-EUR 2,0385% contro il 2% ammesso (18 allarmi su ~883 finestre invece di 17); BTC-EUR 1,47%, SOL-EUR 1,02%. La risposta del quant dice che `rejected` e' definitivo: nessuna terza regola per `feature_drift`.
- Conseguenza codificata (sez. B1 dell'architetto): il blocco `feature_drift` del learner e' `not_testable` (reason `calibration_rejected_final`) con la diagnostica (`support`, percentile del PSI delle feature non testate, PSI di `p_edge` sull'holdout); il criterio di gate resta `unmeasured` (nota `not_testable`). G-DRY-OPS e' al massimo INCOMPLETE e non chiude P3.
- A1 dell'architetto: un criterio di calibrazione di `p_edge` sugli esiti NON esiste in G-DRY-OPS. In G-DRY-ECON esiste (`edge_ece` <= `gate_econ.max_ece_edge` = 0,10 su tutte le label live `edge` risolte del champion), ma G-DRY-ECON non e' richiesto per P3 (Appendice A: P3 = G-DRY-OPS; R0 = G-DRY-OPS + GE = GO), il suo stato resta `IN_PROGRESS` sotto 30 trade (per sempre con un champion `untunable`), non ha minimo di `n_eff` sulle label, e nessuna label si risolve prima di ~15,5 giorni (5.1.6): non e' misurabile nei 7 giorni minimi di G-DRY-OPS.
- Misure disponibili oggi per la calibrazione: label live `edge` risolte (`data/live_labels/*.jsonl`, `question_key == "edge"`, `split == "live"`), `p_edge` previsto nel `DecisionRecord`, la distribuzione di `p_edge` sull'holdout (`p_edge_reference` in `10_calibration.json`: 8.712 stati, p_edge in [0,259; 0,401]) e il PSI settimanale di `p_edge` live contro di essa, gia' prodotto da L0 come diagnostica (notti 24-25 ott: 0,23-0,75 a seconda del prodotto, senza soglia). Il champion e' `untunable` (nessun ingresso): le sole label `edge` risolte sono quelle dei book benchmark.

## Il dubbio logico

(a) Senza un criterio di calibrazione di `p_edge` in un gate richiesto per P3, la deriva delle feature non e' misurata da nessun criterio e G-DRY-OPS non puo' chiudersi (voluto: "non misurato non e' superato"), ma nessun criterio oggi potrebbe chiuderlo nemmeno in futuro. (b) La calibrazione sugli esiti ha un costo in tempo: le label sono sovrapposte (orizzonte H ~15 giorni) e `n_eff` cresce lentamente; un criterio con `n_eff` minimo allunga P3 oltre i 7 giorni. (c) Con un champion `untunable` non ci sono trade del modello: le label `edge` risolte esistono per ogni decisione (non solo per i trade), quindi la calibrazione e' misurabile anche senza ingressi.
(d) Dubbio emerso nell'implementazione di Q3 (fuori dalla lista della sez. J): il book `random_entry` ha la STESSA `policy_version` del champion ma forza BUY e intent (parametro di prova, "non e' una strategia"; 5 BUY e 2 intent il 24-ott). La prova di "zero BUY del modello con policy `untunable`" conta quindi solo i book `champion` e `challenger` (`MODEL_BOOKS` nel learner). Va confermato dal quant, o il `DecisionRecord` deve dire con che cosa ha deciso (campo `backend`/`decider` del book) perche' la prova non dipenda dal nome del book.

## Domande

A (quant). Metrica e soglia della calibrazione di `p_edge`: ECE a 15 bin (come `edge_ece`), reliability per decile, Brier contro l'holdout, o altro; quale soglia, e contro quale riferimento (l'holdout del training, non il training: previsioni in-sample hanno un'altra distribuzione).
B (quant). Minimo di `n_eff` delle label `edge` (sovrapposte, H ~15 giorni, peso di unicita') perche' il criterio sia misurabile, e quindi la durata minima di P3 se il criterio entra in G-DRY-OPS: il criterio puo' restare `unmeasured` sotto il minimo senza far scattare un `fail`?
C (quant, poi architetto). Gate ospite: G-DRY-OPS stesso, un prerequisito di P3 separato, o una modifica dei requisiti di P3 (G-DRY-ECON richiesto, oppure solo `edge_ece`)? Rapporto con `edge_ece` di G-DRY-ECON (stessa grandezza, due soglie?). Come si evita che un champion `untunable` blocchi per sempre il gate economico.
D (quant). R3 (`revision_decision_flips`) e `flips_basis` con champion `untunable`: la stessa deduzione di Q3 (con policy esplicitamente `untunable` l'azionabilita' del BUY e' falsa per qualunque input, flip = 0) vale per R3 sugli stati FLAT (la spec 2.11 l'ha rinviata al primo champion tarabile senza regola scritta). Estendere `flips_basis` a R3, con le stesse condizioni (policy esplicita, zero BUY e zero intent dei book del modello nel giorno)?
E (quant). Conferma di (d): conta solo `champion`/`challenger`, o serve un campo nel record?
F (architetto). Dopo le risposte: dove vive il criterio (spec 5.1.7), quale stato assume `feature_drift` `not_testable` nel gate quando esiste il criterio sostitutivo, e modifiche minime di report/learner.

## Cosa NON e' stato fatto

Nessun ritocco di D1, alfa, bin, finestre, famiglia, feature. Nessuna terza regola. Il dry run in esecuzione non e' stato fermato; l'unico file scritto nella sua cartella dati e' la copia byte per byte di `data/reports/sim_p1f/PSI_V3_VOLSTD/baseline-lgbm@2026-10-18.2/` (passo 11 della spec dell'architetto); i suoi documenti del learner e i suoi report non sono stati rigenerati (`--force` non lanciato).

## Risposta trader-quant

Autore: trader-quant, 2026-10-26. Legenda: [M] misurato (numeri di questo file e di `docs/decisions/2026-10-26-p1f-bis-psi-v3-esito.md`; testo della spec 5.1.7 e Appendice A letto), [D] dedotto dalla formula o dal testo, [S] stima da verificare sui dati. Nessun codice eseguito.

### 0. Decisioni in sintesi

1. `feature_drift` resta `not_testable` per `baseline-lgbm@2026-10-18.2`, definitivo. D1 non si reinterpreta (niente "1 allarme e' rumore"). Nessuna terza regola.
2. Il calcolo della calibrazione di `p_edge` sugli esiti NON entra in G-DRY-OPS ne' in un prerequisito di P3: e' strutturalmente lento (mesi). La condizione che io stesso avevo posto in P1f-bis Q2.4 ("G-DRY-OPS si chiude senza `feature_drift` solo con un criterio di calibrazione in un gate richiesto per P3") era sbagliata: chiedeva a un gate di 7 giorni di ospitare una misura che matura in mesi. La ritiro e la sostituisco con la regola della sez. 1, scritta ORA, prima di qualunque dato di un champion futuro.
3. `random_entry` escluso dalla prova: confermato; la prova deve dipendere dal decider registrato, non dal nome del book (sez. 7).
4. R3: `flips_basis = policy_untunable` si estende a R3 con le stesse condizioni di Q3 piu' una (sez. 6). P0 non richiede R3 (Appendice A).
5. Con il champion attuale P4 e' bloccato da GE != GO e dall'assenza di una policy, non da `feature_drift`: smettere di investire nel gate di deriva per questo champion (sez. 8).

### 1. Chiusura di G-DRY-OPS senza `feature_drift` (dubbio a-c)

Perche' il blocco attuale e' sbagliato come progetto, non come esito:
- [D] Con D1 stimata per prodotto su ~883 finestre l'errore standard e' ~0,47% [M]: con un tasso vero dell'1,5% (compatibile con i tre prodotti) P(stima > 2%) ~ 14% per prodotto, ~35% su tre. Un champion futuro con R sovrapposto per gran parte a quello attuale avra' un esito di D1 dominato dal rumore [S]. Fare dipendere l'accesso a R0 da una moneta lanciata vicino al 2% non protegge il capitale: seleziona a caso.
- [D] Anche con PSI-v3 adottata il criterio copriva solo la forma di 14 rendimenti veloci; livello di volatilita' e 28 feature persistenti non erano coperti comunque. La protezione finanziaria reale contro la deriva e' l'esito realizzato (calibrazione, PnL), che non si misura in 7 giorni.

Regola (vale per OGNI champion, attuale e futuri; fissata prima di vedere i loro dati):
1. PSI-v3 `adopted` per la versione: criterio `feature_drift` pass/fail come oggi (spec 5.1.7). Nessun cambiamento.
2. PSI-v3 `rejected` (`not_testable`): il criterio assume lo stato dichiarato `not_covered`. G-DRY-OPS PUO' chiudersi, alle tre condizioni seguenti, tutte obbligatorie; se una manca il criterio resta `unmeasured` e G-DRY-OPS INCOMPLETE:
   - (a) Sorveglianza attiva su ogni giorno della finestra del gate: la statistica PSI-v3 si calcola con il riferimento e la soglia diagnostici della corsa respinta (`reference_diagnostic`, `c_diagnostic` del `10_calibration.json` della versione), senza ritoccare nulla. Esito = allarme `feature_drift_watch:<prodotto>`, mai `pass`/`fail` del gate. L'output dichiara il tasso di falsi allarmi misurato fuori campione, non quello nominale: per il champion attuale 1,02-2,04% per finestra, 0,31-0,61 giorni in allarme ogni 30 [M].
   - (b) Diagnostica `support` e PSI di `p_edge` contro l'holdout presenti (gia' prodotte da L0).
   - (c) La nota del gate elenca cio' che NON e' coperto: "Deriva delle feature non certificata: PSI-v3 respinta per questa versione (D1). Coperto solo da sorveglianza senza soglia di gate, guardia per prodotto in R0 e stop di calibrazione in R0."
3. Controlli compensativi, attivi da R0 in poi solo per versioni in `not_covered` (vanno nella rampa, non in G-DRY-OPS):
   - Guardia per prodotto: un allarme `feature_drift_watch` o `support` > 0 del giorno sospende gli INGRESSI di quel prodotto per il giorno successivo; SELL, bracket, orizzonte e watchdog invariati. Direzione fail-safe: blocca esposizione nuova, non ne crea. Costo atteso [M]: <= ~2% dei giorni-prodotto senza ingressi. Si applica al book live, al suo shadow twin e ai book del modello in dry, perche' la parita' non cambi.
   - Stop di calibrazione in R0 (sez. 3-5): violazione = ritorno in dry, come la discesa di gradino.

Perche' non e' un allentamento: il gate non certifica una cosa che non ha misurato (lo stato e' `not_covered`, non `pass`, ed e' scritto nell'output); il rischio della cosa non misurata e' limitato dal capitale R0 (minimo operativo, perdita a SL di pochi centesimi per trade, solo spot) e coperto da due controlli live che oggi non esistono. Il sistema complessivo e' piu' restrittivo in live di prima. Rischio residuo dichiarato: la regola e' scritta dopo il fallimento di PSI-v3; e' accettabile solo perche' vale per tutti i champion futuri ed e' fissata prima dei loro dati.

### 2. Champion futuro: quando PSI-v3 si applica legittimamente

1. Nuova versione di champion = nuovo R (la sua finestra di training), nuovo `holdout_start` (dal suo `label_config`), nuova cartella `PSI_V3_VOLSTD/<versione>/`, `00_preregistration.json` scritto prima delle barre, UNA corsa, stessa `RULE_V3` byte per byte. E' applicazione meccanica, ammessa.
2. Non ammesso: cambiare una costante di `RULE_V3`, rieseguire sulla stessa versione, scegliere il champion o la sua finestra di training guardando l'esito di PSI-v3.
3. D1h deve avere n_H >= 1 (finestre del suo holdout non viste); n_H = 0 -> D1h falsa, come gia' scritto (B4).
4. Esito `adopted`: sez. 1 punto 1. Esito `rejected`: sez. 1 punto 2. In entrambi i casi l'accesso a R0 non dipende piu' dalla moneta di D1.

### 3. Domanda A - metrica della calibrazione di `p_edge`

- ECE a 15 bin e' inadatta a campione piccolo [D]: `p_edge` sull'holdout vive in [0,259; 0,401] [M], larghezza 0,14, cioe' 2-3 bin su 15. Con n_b label per bin la distorsione di |freq - p| per un modello PERFETTAMENTE calibrato e' ~ sqrt(2/pi) * sqrt(p(1-p)/n_b) ~ 0,80 * sqrt(0,22/n_b): con n_eff = 30 (n_b ~ 10) vale ~0,12, sopra la soglia 0,10. Oggi `edge_ece` fallirebbe per costruzione su un modello calibrato.
- Brier contro l'holdout: rifiutato, dipende dal tasso base della label, che cambia col regime [D].
- Metrica scelta: calibrazione media (calibration-in-the-large), unidirezionale, perche' solo la sovrastima di p_edge distrugge EV (EV = p W - (1 - p) L, dEV/dp = W + L: una sovrastima delta toglie delta (W + L) di EV; la sottostima perde solo occasioni).
  - d = sum_i w_i (y_i - p_i) / sum_i w_i, con y_i = esito della label `edge` risolta, p_i = `p_edge` del `DecisionRecord` dello stesso stato, w_i = peso di unicita' sulla durata reale [t, exit] (stessa funzione del training, corretta per la correlazione fra prodotti, spec 2.7).
  - n_eff = sum_i w_i; s^2 = sum_i w_i (y_i - p_i - d)^2 / sum_i w_i; se = s / sqrt(n_eff).
  - Insieme: tutte le decisioni dei book del modello della versione (non solo i trade), split `live`.
- Riferimento: gli esiti realizzati, non una distribuzione. L'holdout serve solo come linea di base diagnostica (d_holdout, atteso ~0 per la calibrazione OOF) e per il PSI di `p_edge`; mai il training.
- Soglia di tolleranza delta_tol = 0,05 [S], coerente con il buffer di probabilita' della policy (p_be_buffer fino a 0,20): un errore di 0,05 a p ~ 0,33 e' ~15% relativo.

### 4. Domanda B - minimo di `n_eff`

- Stop in R0: misurabile con n_eff >= 30; violazione se d + 1,645 se < -0,05 (evidenza al 95% di sovrastima oltre la tolleranza). Con n_eff = 30, se ~ 0,086 [D]: scatta solo per errori grossolani (d < ~-0,19). E' un interruttore, non una certificazione.
- Certificazione (G-DRY-ECON, sez. 5): n_eff >= 200, lo stesso minimo gia' usato per la calibrazione OOF del fold (spec 2.10). Con 2-3 bin occupati n_b ~ 67-100 e la distorsione dell'ECE scende a ~0,04-0,05 [D], meta' della soglia.
- Sotto il minimo: `unmeasured`, MAI `fail`. Si', puo' restarci.
- Velocita' [S]: un prodotto produce ~1/D campioni indipendenti al giorno (D = durata media reale delle label, giorni), e i tre prodotti sono correlati. Con D ~ 5 giorni l'ordine e' 0,3-0,6 al giorno: 30 in 2-3 mesi, 200 in molti mesi. Va misurato sulle label vere (l'architetto riporti n_eff giornaliero nel report). Per questo non puo' entrare in P3.

### 5. Domanda C - gate ospite

- NON G-DRY-OPS, NON un prerequisito di P3. Ospiti:
  - Rampa R0: stop di calibrazione (sez. 4) come condizione di permanenza, per ogni versione (non solo `not_covered`).
  - G-DRY-ECON: `edge_ece` <= 0,10 resta la soglia, con minimo n_eff >= 200 (sotto: `unmeasured`), piu' la condizione unidirezionale d >= -0,05 (stima puntuale) con lo stesso minimo. Una sola grandezza per sede: nessuna doppia soglia sulla stessa misura.
- Champion `untunable` e G-DRY-ECON: che non lo superi mai e' l'esito corretto, non un difetto: non ha un edge da certificare. Il report lo dica esplicitamente (stato `IN_PROGRESS` con motivo `policy_untunable`, oppure stato dedicato a scelta dell'architetto), invece di un `IN_PROGRESS` muto.

### 6. Domanda D (B2) - R3 e certificazione P0

- [D] Si', `flips_basis = policy_untunable` si estende a R3. Su FLAT: con policy `untunable` esplicita l'azionabilita' del BUY e' falsa in entrambi i mondi, flip = 0 esatto. Su LONG: la SELL esiste solo su posizioni del modello; con zero BUY della versione non ci sono LONG della versione.
- Condizioni (tutte, per giorno e versione; una mancante: R3 `unmeasured`; violazione di (b): `fail`):
  - (a) `policy_kind == "untunable"` letto esplicitamente (non `None` per file mancante);
  - (b) zero BUY (compresi gli HOLD `btc_context_stale`) e zero intent dei book del modello per la versione;
  - (c) zero stati LONG dei book del modello aperti dalla versione nel blocco (se esistono, R3 su quegli stati resta `computed` o `unmeasured`);
  - (d) base esplicita `flips_basis` nell'output di R3.
- Non rivede la decisione 2.11: quella diceva "non valutabile" perche' la regola non era scritta; ora e' scritta, deterministica, e torna `computed` appena la policy diventa tarabile.
- P0 [M]: l'Appendice A definisce il criterio P0 "barre REST" come R1 su blocchi di 1.440 stati, R2 e R4. R3 NON ne fa parte: il champion `untunable` non blocca P0. Si certifica con `revision_impact certify` sui giorni UTC interi del dry run gia' disponibili (dal 24-ott), senza ricertificare il soak 18-20/10 (decisione U2). Questo va fatto subito: costa poco e chiude una fase.

### 7. Domanda E e dubbio (d) - `random_entry`

- Confermato [D]: la prova riguarda le decisioni del MODELLO. `random_entry` forza BUY indipendentemente dal modello; contarlo farebbe fallire la prova per costruzione e non direbbe nulla sul modello. Resta soggetto a C11 (copertura 100% via `BalanceGuard`), come ogni book.
- Correzione: il filtro per nome (`MODEL_BOOKS`) e' fragile (un book rinominato o un secondo challenger cambiano la prova). La condizione deve essere "decider del book = `registry:<alias>`", ricavata dalla config del giorno o, meglio, da un campo `decider` nel `DecisionRecord`. Inoltre un record di `random_entry` non deve poter essere letto come decisione della policy di quella versione (stessa `policy_version` e' fuorviante): scelta del campo all'architetto. Fino ad allora il filtro `champion`/`challenger` e' accettabile perche' coincide con la config attuale [M].

### 8. Costo-beneficio: cosa blocca davvero P3/P4

- Blocca P4 (R0 = G-DRY-OPS **e** GE = GO, spec 5.1.7 [M]): con policy `untunable` l'holdout non ha trade, quindi E5 e' falso e GE != GO [D, changelog 2.10]. Il champion attuale non arriva a R0 qualunque cosa faccia G-DRY-OPS. E un R0 con questo champion non piazzerebbe alcun ordine: nessuna validazione di post-only, bracket, fee.
- Il vero collo di bottiglia e' un champion con policy tarabile e GE = GO (lavoro di modello/edge, fase P1), non il gate di deriva.
- Non blocca piu' (dopo questa risposta): `feature_drift` (sez. 1), R3 (sez. 6), P0 (sez. 6, certificabile ora).
- Raccomandazione: chiudere P1g con il minimo (sez. 1 regola scritta, sorveglianza da `c_diagnostic`, `flips_basis` su R3, campo `decider`, certificazione P0) e spostare lo sforzo sull'edge. Nessun altro lavoro sul gate di deriva per questo champion: valore finanziario nullo.
- Opzione per l'utente, non mia decisione: se vuole validare la meccanica d'esecuzione reale (post-only, bracket standalone, fee, fill) prima di avere un edge, serve una fase dichiarata di test d'esecuzione a size minima, separata da R0 e con perdita massima pari al capitale spot allocato (es. <= 20 EUR). Costo atteso [S]: ~55 bps a giro su 1-2 EUR, cioe' centesimi. Va decisa dall'utente e scritta in spec; la spec attuale non la prevede.

### Vincoli

Nessuna modifica a sizing, saldi, ordini, `RiskManager`, `BalanceGuard`. Solo spot, copertura 100%, nessun debito: invariati. I controlli nuovi (guardia per prodotto, stop di calibrazione) possono solo bloccare ingressi o riportare in dry, mai creare esposizione. Nessuna costante di PSI-v3 toccata; `not_covered` non e' mai `pass`.

### Per l'architetto (F)

Da specificare: stato `not_covered` del criterio e condizioni (a)-(c) della sez. 1; allarme `feature_drift_watch` da `c_diagnostic`; guardia per prodotto (scrittore della guardia `drift` di `SystemGuards`, oggi senza scrittore) con il perimetro della sez. 1.3; stop di calibrazione in rampa e criterio ECON della sez. 5 con n_eff riportato nel report; R3 con `flips_basis` (sez. 6); campo `decider` (sez. 7); comando di certificazione P0 sui giorni del dry run.

## Risposta architetto

Autore: architetto-opus, 2026-10-26. Perimetro ridotto su richiesta dell'utente (via coordinatore): in (I) solo il minimo per chiudere il conflitto; tutto il resto in spec come DIFFERITO a P4, senza codice. Codice letto: `modules/dryrun/drift.py` (`Calibration`, `load_calibration`, `window_stat`, `window_diagnostics`), `script/sim_p1f_v3.py::calibrate_v3`, `modules/dryrun/learner.py` (`_feature_drift`, `_feature_drift_v3`, `_btc_shift`, `_count_model_record`, `_live_labels`), `modules/dryrun/report.py` (`_feature_drift_criterion`, `_gate_status`, `evaluate_gate_ops`, `evaluate_gate_econ`, `_alarms`, `_champion_labels`, `render_markdown`), `modules/dryrun/revision_impact.py` (`certify`, `main`, `_previous_run`), `modules/risk/kill_switch.py::SystemGuards`, `modules/decision/engine.py::_record`, `modules/dryrun/books.py` (`build_*_book`, `_engine`); spec 2.13 sez. 5.1.6, 5.1.7, 5.2, 5.3, Appendice A.

### A. Fatti verificati nel codice

A1. `10_calibration.json` di una corsa v3 `rejected` con J non vuoto contiene `reference_diagnostic` (stesso formato di `reference`: `{"features": {nome: {...}}}`, scritto da `reference_doc`), `c_diagnostic` (= `ev["c"]`) e `window.d1` (FWER fuori campione per prodotto, il numero che il quant chiede di dichiarare). `load_calibration` oggi NON li carica: con `rejected` `features` e' vuoto, `c` e `window_days` sono null. La sorveglianza richiede quindi solo un caricamento additivo e il riuso di `window_stat` (nessuna formula nuova, nessuna costante toccata).
A2. `certify` lavora solo su un soak report (`--soak-report`, finestra da `started_at`/`updated_at`, prodotti dal report). Per i giorni UTC interi del dry run serve una sorgente di finestra alternativa: `--days START END` + `--products`. Il resto del calcolo (rilettura REST, mondi S/F, blocchi R1/R3 di 1.440 stati, R2, R4, file mai riscritto) e' riusato identico.
A3. `SystemGuards.set_drift` e' GLOBALE (blocca tutti i book e tutti i prodotti) e senza scadenza: non e' la guardia per prodotto del quant (vedi B1).

### B. Incoerenze del quant e correzione minima

B1. Guardia per prodotto: la guardia `drift` esistente e' globale e "HOLD-only until cleared". La guardia del quant richiede una API per prodotto con scadenza e un percorso learner -> app (il learner e' un processo figlio, la guardia vive nell'app). Correzione: DIFFERITA a P4 con quel disegno (sez. D); in P4 l'assenza del dato per una versione `not_covered` blocca gli ingressi del prodotto (fail-closed).
B2. Nota (c) del quant: dice "coperto da guardia per prodotto in R0 e stop di calibrazione in R0", che oggi NON esistono e con questo perimetro non esisteranno prima di P4. Correzione: testo della nota che li dichiara obbligatori e non ancora implementati (C3), e prerequisito esplicito di R0 per una versione `not_covered` (spec, sez. E).
B3. Conseguenza del rinvio di `flips_basis` su R3: con il champion attuale (`untunable`) R3 resta `unmeasured` e G-DRY-OPS resta INCOMPLETE anche con `feature_drift` `not_covered`. Accettato: il quant (sez. 8) mostra che questo champion non puo' comunque arrivare a R0 (GE != GO); per un champion tarabile R3 e' `computed` e il rinvio non pesa.
B4. Calibrazione di p_edge (sez. 3-5 del quant), differita: `n_eff` deve essere la definizione del training, cioe' la somma dei pesi di unicita' per prodotto divisa per `1 + (K - 1) rho` (`training/feasibility.py`), non la semplice somma; e l'insieme va deduplicato per (versione, prodotto, ts) perche' due book con la stessa versione danno righe identiche. Annotato nella riga differita.

### C. (I) Da implementare ORA

C1. `modules/dryrun/drift.py` (additivo; `RULE`, `RULE_V3` e le costanti NON cambiano):
- `Calibration`: tre campi nuovi IN CODA con default: `watch: Mapping[str, FeatureRef] = field(default_factory=dict)`, `watch_c: float | None = None`, `watch_false_alarm: Any = None`.
- `load_calibration`, ramo v3 con `decision != "adopted"`: `watch = {n: feature_ref(n, r) for n, r in (doc.get("reference_diagnostic") or {}).get("features", {}).items()}`, `watch_c = doc.get("c_diagnostic")` (float o None), `watch_false_alarm = (doc.get("window") or {}).get("d1")` copiato com'e'. Ramo `adopted` e v2: invariati.
- Nuova `watch_calibration(cal: Calibration) -> Calibration | None`: `None` se `not cal.watch` o `cal.watch_c is None`; altrimenti `dataclasses.replace(cal, features=cal.watch, window_days=RULE_V3["WINDOW_DAYS"][0], c=cal.watch_c)`. La statistica e' `window_stat(x, watch_calibration(cal), product)`: una sola funzione.
- `__all__` += `watch_calibration`.

C2. `modules/dryrun/learner.py::_feature_drift_v3`, solo ramo `not testable` (decisione respinta):
- `wcal = drift.watch_calibration(cal)` una volta. Per ogni prodotto con finestra completa, nel blocco del prodotto (che resta `status: "not_testable"` con `states` e `diagnostics`) aggiungi `watch`:
  - `wcal is None`: `{"status": "unmeasured", "reason": "no_diagnostic_reference"}`;
  - altrimenti `res = drift.window_stat(x, wcal, product)`; `res.S is None`: `{"status": "unmeasured", "reason": "incomplete"}`; altrimenti `{"status": "measured", "S": res.S, "alarm": res.alarm, "flagged": [{"feature", "psi", "x_f", "p"} delle feature con flag]}`.
- Nel blocco di primo livello: `"watch": {"rule": "PSI_V3_VOLSTD", "reference": "reference_diagnostic", "c": cal.watch_c, "false_alarm_oos": cal.watch_false_alarm, "gate": false}` e `"watch_alarm": [prodotti con watch.alarm True]` (lista ordinata). `status`, `reason`, `alarm` (null) invariati.

C3. `modules/dryrun/report.py`:
- `Verdict = Literal["pass", "fail", "unmeasured", "not_covered"]`. `_gate_status` invariato: `not_covered` non e' ne' `fail` ne' `unmeasured`, quindi non blocca; `evaluate_gate_ops` aggiunge al risultato `"not_covered": [nomi dei criteri con quello stato]` (lista vuota se nessuno).
- Costante, stringa esatta: `NOT_COVERED_NOTE = "feature_drift: not_covered. Deriva delle feature non certificata: PSI-v3 respinta per questa versione (D1). Coperta solo da sorveglianza senza soglia di gate (feature_drift_watch, riferimento e soglia diagnostici della corsa respinta, falsi allarmi fuori campione nel blocco watch). Guardia per prodotto e stop di calibrazione obbligatori in R0, non ancora implementati (differiti a P4, CONFLITTO P1g)."`
- `NOT_TESTABLE_NOTE` diventa la funzione `not_testable_note(reason: str) -> str`, stringa esatta: `f"feature_drift: not_testable. La regola PSI-v3 (ultima, pre-registrata) e' stata respinta da SIM-P1f-bis. Il criterio diventa not_covered solo con la sorveglianza feature_drift_watch misurata in ogni giorno della finestra e la diagnostica support e PSI di p_edge presenti (CONFLITTO P1g); altrimenti resta unmeasured. Motivo: {reason}."`
- `_feature_drift_criterion`, se l'ultimo giorno ha `status == "not_testable"`: scorri TUTTI i giorni della finestra (dal piu' vecchio) e per ciascuno il blocco `parity.feature_drift`; la prima condizione mancante da' `unmeasured` con `not_testable_note(reason)`:
  - blocco assente: `no_learner_document:<giorno>`; `status != "not_testable"`: `not_testable_missing:<giorno>`; `products` vuoto: `no_products:<giorno>`;
  - per ogni prodotto: `status != "not_testable"`: `product_not_measured:<giorno>:<prodotto>`; `watch.status != "measured"`: `watch_unmeasured:<giorno>:<prodotto>`; `diagnostics.support` non mappa: `support_missing:<giorno>:<prodotto>`; `diagnostics.p_edge_psi.status != "measured"`: `p_edge_psi_unmeasured:<giorno>:<prodotto>`.
  - Tutte soddisfatte: `Criterion("feature_drift", "not_covered", {prodotto: watch.S dell'ultimo giorno}, watch.c dell'ultimo giorno, NOT_COVERED_NOTE)`.
  Gli allarmi `watch` NON cambiano lo stato del criterio (solo avviso). Rami `measured`/`unmeasured`/PSI-v2 invariati.
- `_alarms` += `f"feature_drift_watch:{p}"` per `p` in `drift.get("watch_alarm") or []`. Mai in `anomalies`, mai nel gate.
- `_feature_drift_line`, caso `not_testable`: testo attuale + `" | watch: ALARM on <prodotti>"` oppure `" | watch: no alarm"` (dal `watch_alarm`). `render_markdown`: se `gate.get("not_covered")` non vuoto, una riga `"criteri not_covered (non certificati): <nomi>"` sotto il titolo del gate.
- Nessuna stringa o identificatore con `margin`/`margine` in `modules/`.

C4. `modules/dryrun/revision_impact.py` - certificazione P0 su giorni UTC interi del dry run:
- `certify(..., soak_report: Path | None, days: tuple[date, date] | None = None, products: Sequence[str] | None = None, ...)`: esattamente uno fra `soak_report` e `days` (altrimenti `ConfigurationError`). Modo soak: invariato byte per byte.
- Modo giorni: `START <= END` (altrimenti errore); `products` obbligatorio e non vuoto; `lo = START 00:00Z`, `hi = (END + 1 giorno) 00:00Z`; il controllo esistente `now - hi < MIN_AGE` resta (END deve essere un giorno UTC concluso); `ends[p] = hi` per ogni prodotto; `NOT_CERTIFIABLE` non si applica; `--out` mai riscritto e rilettura salvata come oggi. Nel documento: `"soak_report": null`, nuova chiave `"dry_run_days": {"start": START, "end": END, "products": [...]}`, `sections[p]["diagnostic"]["soak_report"] = null`, una nota in piu' in `notes`: `"modo giorni del dry run: finestra [START 00:00Z, END+1 00:00Z) per ogni prodotto, stati FLAT di ogni minuto (nessun book)"`. Schema invariato (`chronos.p0_bars_certification.v2`, chiave additiva).
- `_previous_run` in modo giorni: stesso `dry_run_days`, versione e subset (altrimenti `ConfigurationError`). `note_text` resta solo per il soak (in modo giorni `ValueError`).
- CLI: `--soak-report` con default `None`; nuovi `--days START END` (`nargs=2`, date ISO) e `--products` (`nargs="+"`); in `main`: senza `--days` e senza `--soak-report` si usa `data/reports/p0_soak.json` (comportamento attuale); entrambi: errore di configurazione (exit 2).

C5. Test (quelli con * falliscono sul codice attuale; `tests/test_c11_wiring.py` e `tests/test_obligations.py` NON si toccano: se uno fallisce, fermati e apri un CONFLITTO):
- `tests/test_drift_psi_v3.py`: *`test_load_calibration_v3_rejected_loads_watch` (file sintetico respinto con `reference_diagnostic`, `c_diagnostic`, `window.d1`: `watch` pieno, `watch_c`, `watch_false_alarm` identico al file); *`test_watch_calibration_none_without_reference`; *`test_watch_stat_equals_adopted_stat` (stessi riferimento e c: `window_stat` su `watch_calibration` uguale a quello di una calibrazione `adopted` costruita con gli stessi dati); `test_rule_v3_literal_and_shared_constants_equal_v2` resta verde senza modifiche.
- `tests/test_learner_drift.py`: *`test_l0_v3_rejected_has_watch_per_product`; *`test_l0_v3_rejected_watch_alarm_listed`; *`test_l0_v3_rejected_without_reference_diagnostic_watch_unmeasured`; `test_l0_feature_drift_v3_rejected_is_not_testable_with_diagnostics` verde.
- `tests/test_report.py`: *`test_feature_drift_not_covered_when_watch_every_day`; *`test_feature_drift_unmeasured_if_one_day_lacks_watch`; *`test_feature_drift_unmeasured_if_p_edge_psi_missing`; *`test_not_covered_does_not_block_gate_and_is_listed` (tutti gli altri criteri `pass`: stato `PASS`, `not_covered == ["feature_drift"]`); *`test_watch_alarm_in_alarms_not_anomalies_not_gate` (allarme watch: criterio resta `not_covered`); *`test_not_covered_note_exact`; `test_feature_drift_not_testable_note` aggiornato al testo di `not_testable_note` (modifica voluta).
- `tests/test_revision_impact.py`: *`test_certify_days_mode_window_and_document` (store e sorgente REST sintetici: finestra `[START, END+1)`, `soak_report` null, `dry_run_days` valorizzato, R1 a blocchi come nel modo soak); *`test_certify_days_refuses_soak_together`; *`test_certify_days_requires_products`; *`test_certify_days_end_not_concluded_refused`; *`test_certify_days_never_rewrites_out`; i test del modo soak restano verdi senza modifiche.

### D. (II) DIFFERITO a P4 (solo spec, nessun codice ora; da implementare prima del primo ordine reale)

- D1 `decider` nel `DecisionRecord`: valore `BookConfig.decider` passato a `DecisionEngine` (nuovo kwarg, scritto da `_record` e `_fail`); le prove `untunable` filtrano `decider` che inizia con `registry:` al posto di `MODEL_BOOKS`.
- D2 `flips_basis` su R3: le condizioni (a)-(d) del quant (sez. 6), solo per versioni `untunable` esplicite.
- D3 G-DRY-ECON: `edge_calibration_mean` d >= -0,05 ed `edge_ece` <= 0,10 sullo stesso insieme (label `edge` live dei book del modello della versione champion, deduplicate per (versione, prodotto, ts)), `unmeasured` sotto `n_eff` >= 200 (somma dei pesi di unicita' per prodotto / (1 + (K - 1) rho)), `n_eff` e d nel report di ogni giorno, motivo `policy_untunable` nello stato del gate.
- D4 Guardia `drift` per prodotto (solo versioni `not_covered`): `feature_drift_watch` o `support` > 0 del giorno D blocca gli INGRESSI di quel prodotto dal reload successivo per un giorno; API per prodotto con scadenza in `SystemGuards` (oggi globale e senza scadenza); dato mancante = blocco (fail-closed); SELL, bracket, orizzonte e watchdog invariati; book live, shadow twin e book del modello in dry.
- D5 Stop di calibrazione in R0 (ogni versione): con `n_eff` >= 30, d + 1,645 se < -0,05 -> ritorno in dry.
- Regola di rampa: una versione `not_covered` non entra in R0 se D4 e D5 non sono implementati e testati.

### E. Spec `docs/specs/chronos-system1-spec.md` -> 2.14-draft, 2026-10-26

- Intestazione: `Versione: 2.14-draft`.
- Changelog 2.14 (conflitto P1g; decisione `docs/decisions/2026-10-26-p1g-chiusura-drift.md`), tre punti: (1) `feature_drift` con PSI-v3 respinta: criterio `not_covered` se la sorveglianza `feature_drift_watch` (`reference_diagnostic`/`c_diagnostic` della corsa respinta, `window_stat`, solo allarme, falsi allarmi fuori campione dichiarati) e' misurata in ogni giorno della finestra e `support` e PSI di `p_edge` sono presenti; `not_covered` non blocca G-DRY-OPS ed e' elencato nel risultato; altrimenti `unmeasured`; (2) `revision_impact certify --days START END --products ...` certifica il criterio P0 "barre REST" sui giorni UTC interi del dry run; (3) differito a P4: D1-D5 e la regola di rampa della sez. D (testo di D, una riga ciascuno).
- 5.1.6 riga L0: dopo "con PSI-v3 respinta `not_testable` (definitivo)" aggiungi "; in quel caso sorveglianza `watch` per prodotto con riferimento e soglia diagnostici della corsa respinta, solo allarme".
- 5.1.7 riga "Deriva delle feature": sostituisci la frase da "`not_testable` (PSI-v3 respinta, definitivo) conta come `unmeasured`" fino a "(CONFLITTO P1g)" con: "`not_testable` (PSI-v3 respinta, definitivo) diventa `not_covered` (non certificato, non blocca il gate, elencato in `not_covered`) se in ogni giorno della finestra la sorveglianza `feature_drift_watch` e' misurata per ogni prodotto e la diagnostica `support` e PSI di `p_edge` e' presente; altrimenti `unmeasured`. Un allarme `feature_drift_watch:<prodotto>` e' un avviso (`alarms`), non un criterio".
- 5.1.7, dopo la tabella della rampa, paragrafo "Differito a P4 (CONFLITTO P1g)" con D1-D5 e la regola di rampa della sez. D.
- 5.2: "Note sullo schema del report (2.14)": blocchi `watch` e `watch_alarm` di `parity.feature_drift`, stato di criterio `not_covered`, chiave `gate_ops.not_covered`, allarme `feature_drift_watch:<prodotto>`, testi `NOT_COVERED_NOTE` e `not_testable_note`; certificazione con `dry_run_days`.
- 5.3 punto 11 (o nota in coda alla 5.3): `certify --days` e test della sez. C5 obbligatori.
- Appendice A: P0, in coda al criterio: "certificabile sui giorni UTC interi del dry run con `revision_impact certify --days`"; P3: "con `feature_drift` `unmeasured` o `not_testable` G-DRY-OPS e' al massimo INCOMPLETE; `not_covered` non blocca"; P4: "per una versione `not_covered`: D4 e D5 implementati prima del primo ordine; D1-D3 con P4".
- `config/config.example.yml`: nessuna chiave nuova.

### F. Documento di decisione `docs/decisions/2026-10-26-p1g-chiusura-drift.md` (corto)

Titolo "DECISIONE P1g (risolta il 2026-10-26) - chiusura di `feature_drift` con PSI-v3 respinta". Sezioni: Contesto (3 righe, rimandi agli esiti P1f/P1f-bis); Decisione (regola del quant sez. 1 in 5 righe: `not_covered` con le condizioni (a)-(c), vale per ogni champion, fissata prima dei loro dati; rischio residuo con la frase del quant); Differito a P4 (D1-D5 e regola di rampa, una riga ciascuno); Correzioni dell'architetto (B1-B4, una riga ciascuno); Certificazione P0 (comando esatto, una sola corsa, `fail` o `unmeasured` si documentano senza ritocchi); Esito P0 (vuoto, riempito al passo 6). In coda "Allegato: CONFLITTO P1g" con la copia integrale di questo file.

### G. Vincoli

Solo misura offline (drift, learner L0, report, CLI di certificazione): nessuna modifica a sizing, ordini, saldi, `RiskManager`, `BalanceGuard`, `BalanceBook`, gateway, watchdog, kill switch, engine. `RULE`/`RULE_V3` e tutte le costanti PSI invariate. `not_covered` non e' mai `pass` e lo dichiara nel risultato. Tripwire C11 intoccati. Nessun `margin`/`margine` nuovo in `modules/` (controlla con una ricerca prima del commit).

### H. Passi (ognuno chiude con la suite intera verde)

0. Piano in `tasks/todo.md`.
1. Scrivi e committa il documento di decisione (F) senza la sezione Esito compilata.
2. `drift.py` (C1) + test.
3. `learner.py` (C2) + test.
4. `report.py` (C3) + test.
5. `revision_impact.py` (C4) + test; spec 2.14 (E); suite intera; ricerca `margin`; commit.
6. UNA corsa vera, dati del dry run in sola lettura, percorsi presi dalla sua config (`C:\Users\faric\Documents\sviluppo\chronos-dryrun\config.yml`: `paths.data_dir`, `paths.models_dir`): `python -m modules.dryrun.revision_impact certify --days 2026-10-24 2026-10-25 --products <prodotti serviti dalla config del dry run> --model-dir <models_dir>\registry\baseline-lgbm@2026-10-18.2 --subset final --label-config <stessa cartella>\label_config.json --bars-root <data_dir>\bars\1m --config C:\Users\faric\Documents\sviluppo\chronos-dryrun\config.yml --out data/reports/p0_bars_certification_dry_2026-10-24_2026-10-25.json`. Se un percorso non esiste com'e' scritto, fermati e apri un CONFLITTO. Esito (pass, fail o unmeasured, con i numeri del file) nella sezione Esito del documento di decisione; nessun ritocco; commit.
7. Deploy senza fermare il dry run: rilancia il learner del dry run con `--force` sulle notti 2026-10-25 e 2026-10-26 e rigenera i report (blocchi `watch`, criterio `feature_drift`); riporta lo stato del criterio in `tasks/todo.md`.
8. Svuota `.claude/CONFLITTO.md` (la copia e' nell'allegato della decisione), review in `tasks/todo.md`.
