# Lessons

## 2026-10-17 — Spec v2.1 had internally inconsistent numbers

Pattern: example values (fees, ATR, round-trip cost) were written by hand in several sections and did not follow the formulas stated elsewhere in the same document. The default barrier parameters (ATR 15m, H = 240 min) were never checked against real fees and real volatility, and turned out to be infeasible.

Rules:
- Define each quantity once (one formula, one owner module) and reference it; never restate a formula in another section.
- Derive every numeric example from one worked reference case and reuse that case everywhere.
- Before fixing strategy parameters in a spec, measure the market (spread, ATR, history depth) with a read-only probe and check them against the cost model.
- When a parameter can be wrong in a way that kills the project (fees vs edge), add an explicit stop criterion instead of a default.
- State the scope (per book, global) of every stateful component, and the decision rule (argmax vs score) of every derived value.

## 2026-10-17 - Tooling run on the whole tree touched v1 files

Pattern: `ruff check --fix modules` rewrote eight v1 modules that are not part of the task. Caught by `git status`, reverted with `git checkout`.

Rules:
- Run auto-fixers only on explicit file paths, never on a directory that contains code outside the task.
- Check `git status` after any command with `--fix` or `--write`.
- Keep v1 paths in the `exclude` lists of ruff and mypy until they are deleted.
- The shell tool strips backslashes inside heredocs and inline scripts: write files containing `\` with the Write/Edit tools.

## 2026-10-18 - Do not build on a data-source equivalence nobody has measured

Pattern: the spec said "build 1m bars from trades" and the history was REST candles. I implemented the aggregator exactly as written and only a live soak against the exchange's own candles showed that the two series differ in about half of the minutes. The tests (all green) could not show it: they compared the code with itself.

Rules:
- When two sources are meant to be the same series, compare them on real data before building on either, and say so in the plan.
- A parity test between two implementations of one definition does not prove the definition matches the outside world; add a check against the external source.
- Keep the first soak against the real service early in a module, not at the end.
- Shell heredocs mangle long texts and backslashes: write multi-line patches as files with the Write tool and run them.

## Un processo di lunga durata va ricontrollato, non dato per vivo

- Il soak di 48 h era morto dopo 51 minuti per un riavvio del PC e il todo lo dava "in corso". Regola: prima di scrivere "in corso" in un documento, verificare PID vivo e `updated_at` del report; a ogni ripresa della sessione controllarli per primi.

## numba `cache=True` non vede le dipendenze tra file

- Una funzione `@njit(cache=True)` che chiama kernel definiti in un altro file viene invalidata solo se cambia il PROPRIO file: modificando il kernel (o, nel controllo a mutazioni, un kernel mutato) la versione in cache continua a usare il vecchio codice. Per le label (`training/labeling.py::_scan` chiama `modules/dryrun/fill_model.py`) significherebbe etichette calcolate con regole di fill superate, senza alcun errore.
- Regola: `cache=True` solo su kernel che non chiamano altri file; `_scan` non usa la cache. Dopo i controlli a mutazioni cancellare `*.nbi`/`*.nbc`.
- Il controllo a mutazioni ha trovato l'incoerenza solo perche' un test e' fallito dopo il ripristino: ripetere sempre la suite intera dopo le mutazioni.

## Prima di una corsa lunga, la suite dei test del CLI

- La prima esecuzione reale dello studio di fattibilita' e' crollata dopo circa 10 minuti su un errore (`to_numpy()` di un indice con fuso orario da' un array di oggetti) che il test del CLI scopriva in 4 secondi. Regola: prima di lanciare in background una corsa di minuti, eseguire i test del modulo, in particolare quelli che attraversano `main`.
- Gli script di patch lunghi vanno in un file scritto con lo strumento Write, mai in un heredoc della shell (di nuovo un errore di virgolette).

## Sequenze di escape nelle patch via shell

- Nei comandi Bash (anche con heredoc tra apici) `\n` dentro una stringa Python di una patch e' arrivato nel file come un VERO a capo, rompendo la sintassi (successo 3 volte: `newline="\n"`, `"\n".join`). Regola: per tutto cio' che contiene backslash usare lo strumento Edit/Write, oppure una patch che non contenga `\n` letterali (es. `chr(10)`), e rilanciare `ruff`/`mypy` subito dopo ogni patch.
- Un comando con `python - <<'EOF'` senza `EOF` finale resta in attesa di stdin: usare sempre `.venv/Scripts/python.exe` con heredoc completo.
- Nomi di file di script nella scratchpad condivisa: unici per agente (un sub-agente ha sovrascritto `mutate.py`).

## Diagnosi di un tuning che non produce trade (P1c)

- "Zero trade" ha almeno tre cause diverse: nessun candidato dalla policy (skip vuoti), candidati scartati dal RiskManager (`ev_negative`, `ev_buffer`), o il backtest che non li vede. Prima di concludere guardare `res.skips` e il numero di candidati per prodotto, non solo il PnL. Le tre cause trovate: veto di regime su una testa quasi uniforme (fold 3/4), `p_edge` calibrato sempre sotto `p_breakeven` (final), segnale debole (fold 0-2).
- Una decisione di soglia economica deve usare una grandezza che esiste identica in training, backtest e runtime (un token dello stato), non l'argmax di una testa del modello: un classificatore quasi uniforme sceglie una classe per spareggio.
- Nelle diagnostiche non ammettere regimi vietati dal contratto del RiskManager (0 e 4): solleva `PolicyContractError`.
- C11: nessun identificatore con il token `margin` in `modules/` (il tripwire spezza i nomi su `_`): il margine sul pareggio si chiama `p_be_buffer`.
- Dati sintetici di test: il token `atr_1d_pctile_365d` esiste solo dopo 104 barre giornaliere; le fixture dei test del backtest hanno 150 giorni e le decisioni partono dal giorno 112. Un test che "non entra mai" puo' dipendere da questo.
- Un test che passa al primo colpo non e' una prova: dopo le modifiche di P1c ho introdotto 12 difetti a mano (confini dei regimi, buffer, pavimento del sizing, controllo dei moltiplicatori) e tutti sono stati intercettati; ripetere la suite intera dopo le mutazioni.

## Cablaggio P2/P3: un test unitario verde non dice che il sistema funziona (2026-10-19)

- Il cablaggio di `app.py` aveva "teoria" (1.747 test verdi) ma tre lacune che nessun test unitario poteva vedere: niente book oltre il champion, nessuna ricostruzione dopo un riavvio, nessun file di telemetria per il report. Regola: ogni modulo che produce un file letto da un altro modulo (`ops/DAY.json`, `equity/DAY.jsonl`, `closed_trades`) si prova con il LETTORE reale (il `report`) su dati veri, non solo con uno schema scritto a mano.
- La prova su dati reali ha trovato cio' che i test non trovavano: le registrazioni WS del 18-ott sono illeggibili dopo le 00:21 UTC (un membro gzip troncato dal riavvio del PC, seguito da uno nuovo in append: `zlib.error`, non gestito da `read_recording`); il book di ETH-EUR del 19-ott non ha uno snapshot level2 (la sua registrazione parte da aggiornamenti); `watchdog.py` logga a livello ERROR anche l'uscita per orizzonte, che e' ordinaria. Regola: prima di scegliere il giorno da riprodurre, scandire le registrazioni (messaggi, snapshot, errori di lettura) con uno script.
- Il modello reale e' `untunable`: con lui nessun book che usa la sua policy entra mai, quindi la prova di "si aprono e si chiudono trade" richiede un modello DEMO (etichettato `"demo": true`, copia con barriere brevi e policy permissiva) e il parametro di test `forced_buy_rate` del `random_entry`. Regola: dichiarare nel report che cosa e' reale (dati, ordine di esecuzione, fee dell'account, ledger, report) e che cosa e' un parametro di prova (barriere, policy, frequenza forzata); e non confondere "il champion non ha edge" con "il sistema non funziona".
- Mai riscrivere un file con `open(p, 'w')` di Python su Windows senza `newline=""`: converte tutti i fine riga in CRLF (il working tree e' CRLF, ma `tasks/*.md` no); per i documenti usare gli strumenti Edit/Write o `cat >>`.
- Un test di riavvio deve confrontare lo stato dopo uno spegnimento PULITO (le entrate in attesa vengono annullate e rilasciate) con quello ricostruito; confrontarlo con lo stato a processo ancora vivo fa fallire per le riserve dell'entrata in corso, che e' corretto.
- I test non devono scrivere nel repo: `make_cfg` ora mette anche `telemetry.dir` sotto `tmp`; una cartella `telemetry/` creata nella radice dal primo giro dei test e' stata rimossa a mano.

## Un finto che non imita l'SDK non prova la riconnessione (canale `user`, 2026-10-20)

- I test di `user_stream.py` erano verdi (1.8 mila) e il flusso si congelava al primo guasto sul feed vero: il finto non chiamava `on_close` dentro `close()` come fa l'SDK, non sollevava su un socket morto e non lasciava mai il thread dell'SDK vivo. Regola: un finto di trasporto riproduce i callback e le eccezioni REALI dell'SDK (letti nel sorgente installato); la prova di riconnessione si fa con guasti INIETTATI sul feed vero (`script/user_channel_soak.py --inject`), non solo con test.
- Ogni attesa su un thread dell'SDK (`open`, `subscribe`, `close`) ha un timeout: una `close()` che non torna ha bloccato `_consume` per sempre. Un processo con thread non-daemon abbandonati non esce: lo script di soak termina con `os._exit` dopo aver scritto i rapporti.
- Un flag "gia' segnalato" (`_silent`) che solo un messaggio puo' azzerare disattiva la sorveglianza proprio quando i messaggi non arrivano: il flag va azzerato anche alla nuova connessione.
- Lo stesso schema e' in `modules/market/ws_feed.py` (non toccato): da provare con un soak con `abort`/`silence` sul feed pubblico.

## Dataset esterni di news (studio a eventi, 2026-10-20)

- Il timestamp di un dataset esterno va sempre confrontato con eventi noti prima di usarlo: il campo `published_on` del dataset CoinDesk e' in ora iraniana (Asia/Tehran), 3,5-4,5 ore avanti rispetto all'UTC; senza la correzione ogni risultato sarebbe stato sfasato. Verifica incrociata su titoli presenti in due dataset diversi e su eventi con orario noto (FTX, tweet SEC falso del 9-gen-2024).
- Una regola di decisione (tag, finestre, controllo, famiglie di test) si scrive PRIMA di vedere i risultati e si congela con un hash (`TAGGER_VERSION`); si riportano tutti i test con la correzione per confronti multipli, non solo quelli che escono.
- I titoli che raccontano un movimento arrivano dopo il prezzo: una notizia che 'spiega' un movimento non e' un segnale anticipatore.


## Misurare l'impatto di una correzione di dati: stesso percorso del sistema, non una scorciatoia (revisioni REST, 2026-10-23)

- Gli script di ricerca sostituivano una cella della griglia GIA' riempita; ricostruire la griglia con la stessa `fill_flat_bars` dalle righe reali rivisto (K15) ha cambiato i numeri: spostamento massimo SOL da 0 a 0,0153, stati ETH sopra 0,01 da 1 a 7. Regola: una misura di impatto passa dallo stesso codice di costruzione dei dati del sistema (righe reali -> griglia -> indicatori -> matrice -> modello), e la certificazione si fa sul codice ufficiale, non sullo script di ricerca.
- Un criterio "quota per giorno" su un giorno PARZIALE ha un denominatore piccolo (343 stati: 1 stato = 0,29%): puo' fallire per due stati senza che il modello si comporti diversamente. Prima di scrivere una soglia per periodo, dire cosa succede ai periodi incompleti.
- `vol_z_1d` e' lo z-score del volume dei giorni UTC COMPLETI: una revisione del giorno D lo sposta dal giorno D+1. Leggere la definizione prima di scrivere il test che dice "stesso giorno".
- pandas recente da' `datetime64[us]` agli indici costruiti con `date_range`: le fixture che simulano lo store devono forzare `datetime64[ns, UTC]` (il dtype di `candles_to_frame`) o i confronti fra frame con etichette uguali sollevano.

## Soglie pre-registrate su una statistica di cui non si e' vista la forma del nullo (P1f, 2026-10-25)

- La regola PSI-v2 aveva soglie D1-D3 fissate su una stima [S] "quasi i.i.d." (`E[PSI | H0]` ~ 0,01-0,07); la corsa vera ha dato un nullo con 99-esimo percentile 1,06-1,27 per le feature veloci (la scala dei rendimenti segue il regime di volatilita', persistente per settimane) e un FWER fuori campione del 3-5%: `rejected`, con la pre-registrazione rispettata e nessun ritocco. Regola: prima di pre-registrare soglie numeriche su una statistica nuova, misurare la forma del nullo sul solo periodo di training (senza D1-D3 e senza guardare i dati di prova) e scrivere le soglie dopo; una stima i.i.d. di una serie finanziaria sbaglia di un ordine di grandezza.
- Un engine che serve BTC e un'alt nello stesso oggetto vede ora come `stale` ogni minuto in cui l'alt arriva sola: i test che alimentavano un prodotto alla volta aspettavano un BUY che ora e' un HOLD `btc_context_stale`. Nei test, alimentare BTC e alt nello stesso `on_bars`.
- Una pre-registrazione che include l'hash del codice (`code_sha256`) rende visibile qualunque modifica successiva alla corsa: dopo la corsa non toccare `script/sim_p1f.py` ne' `modules/dryrun/drift.py`.

## Una soglia pre-registrata stretta puo' respingere una regola per un solo evento (P1f-bis, 2026-10-26)

- PSI-v3 ha superato D2, D3 e D1h con ampio margine e ha fallito D1 per UN allarme su ETH-EUR (18 su ~883 finestre contro 17 ammessi: 2,04% contro 2%). La regola era scritta cosi' e la decisione e' definitiva: nessun arrotondamento, nessun cambio di alfa o di soglia dopo il risultato. Lezione per il disegno: una soglia a livello di singolo prodotto con ~900 finestre ha un errore standard di ~0,5 punti, pari a 1/4 della soglia; il criterio andava scritto con la sua incertezza (limite binomiale, come D1h) e non come numero secco.
- Il congelamento in git (documento e tre file di codice tracciati e puliti, rifiuto della corsa se no) e l'hash del codice nel file di pre-registrazione hanno reso la corsa verificabile; farlo sempre per una calibrazione che non si puo' ripetere.
- Un invariante come "nessun BUY con policy non tarabile" va contato sui book dove decide il MODELLO: un book benchmark con BUY forzati ha la stessa `policy_version` del champion e farebbe scattare un falso errore. La prova dipende dal nome del book finche' il record non dice con che cosa ha deciso (domanda E in P1g).

## Una cache chiave per moneta mescola serie diverse della stessa moneta (studio pattern di volume, 2026-10-26)

- Nello studio a due serie (USD principale + EUR replica) la cache dei rendimenti futuri era indicizzata solo per moneta: la replica ha letto la serie USD (shape mismatch). L'ha trovata il controllo end-to-end su dati sintetici con la replica attiva, PRIMA del commit del codice congelato. Regola: ogni cache di serie include l'identita' della serie (`series`), e il controllo sintetico del test end-to-end deve esercitare TUTTI i rami del braccio (replica compresa), non solo quello del test minimo.
- La stima di potenza della pre-registrazione assumeva indipendenza fra due condizioni (drawdown profondo e volume alto) che nei dati veri sono quasi disgiunte nel tempo: n reale 3 contro ~20 stimati. Prima di pre-registrare un minimo di unita', contare le co-occorrenze delle sole feature (nessun rendimento) sui dati di training, oppure dichiarare nella pre-registrazione che la stima di n e' [S] senza questo controllo.

## Strumenti: gli a-capo e le barre rovesciate (P4-pre, 2026-10-27)

- Molti file del repo (`modules/config.py`, `config/config.example.yml`, `modules/ledger/positions.py`) hanno a-capo CRLF. Riscriverli con Python in modalita' testo (`read()` poi `write(newline='')`) li converte in LF e il commit mostra l'intero file cambiato (la prima volta e' successo: corretto con `--amend` riportando CRLF). Regola: sui file esistenti usare lo strumento Edit (conserva gli a-capo) oppure controllare `b'\r\n'` prima e dopo; controllare sempre `git diff --stat` prima del commit.
- Nei comandi della shell gli `\` dentro un heredoc vengono dimezzati: una stringa con `\n` letterale scritta da uno script Python ne usciva con un a-capo vero. Nei sorgenti scritti con lo strumento Write il testo e' letterale. Per un backslash in uno script inline usare `chr(92)`.
- `grep -c $'\r'` nella shell di questo ambiente non conta i CR: contare con Python.
- Un test con tempo finto in cui piu' task dormono sullo stesso orologio che avanza a ogni `sleep` fa correre il tempo a velocita' diverse e rende i test instabili. Per gli scenari con piu' cicli serve un tempo scalato coerente (`ScaledClock`: `sleep(s)` dorme davvero `s/scala`); i cicli con attese REALI dentro (riconciliatore, `asyncio.wait_for`) vanno regolati nella config del test.
