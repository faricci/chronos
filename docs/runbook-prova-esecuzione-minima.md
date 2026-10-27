# Prova di esecuzione con soldi veri: come si fa (P4-pre)

Che cos'e': una prova dell'impianto di esecuzione con ordini veri di circa 3 euro su BTC-EUR, solo mercato spot. Non e' una strategia: serve a vedere che ordini, stop, commissioni e saldi si comportano come previsto. Il programma non puo' perdere piu' di 3 euro in tutto (limite fisico: il portafoglio dedicato ne contiene circa 5) e non puo' andare in debito.

Il piano completo e le tue risposte sono in `docs/decisions/2026-10-26-p4-prova-esecuzione-minima-piano.md`.

## 1. Una volta sola, prima di cominciare

1. Portafoglio dedicato su Coinbase con circa 5 euro (non di piu').
2. Chiave nuova (Coinbase, sezione sviluppatori / API): permessi di VISTA e di SCAMBIO, NESSUN permesso di prelievo o trasferimento, legata al solo portafoglio dedicato, con il tuo indirizzo IP nell'elenco consentito. Scarica il file.
3. Metti il file FUORI dalla cartella del progetto, per esempio `C:\Users\faric\chronos-keys\probe.json`. Va bene il JSON scaricato cosi' com'e', oppure un file di testo con due righe: `COINBASE_API_KEY=...` e `COINBASE_API_SECRET="-----BEGIN EC PRIVATE KEY-----\n...\n-----END EC PRIVATE KEY-----\n"`. Il programma rifiuta un file dentro la cartella del progetto.
4. Nel file `.env` del progetto deve restare la chiave di SOLA LETTURA del dry run. La chiave di scambio non va mai li'.
5. Windows: sospensione disattivata nei giorni di prova (Impostazioni, Sistema, Alimentazione). PC acceso e tu presente dalle 10:00 alle 20:30 ora italiana (giorni feriali).
6. Cartella e file di configurazione della prova, separati dal dry run. Crea `C:\Users\faric\Documents\sviluppo\chronos-probe\config.yml`:

```yaml
paths:
  state_dir: "C:/Users/faric/Documents/sviluppo/chronos-probe/state"
exec_probe:
  enabled: true
  db_path: "C:/Users/faric/Documents/sviluppo/chronos-probe/state/chronos_probe.db"
  out_dir: "C:/Users/faric/Documents/sviluppo/chronos-probe/out"
```

Non cambiare nient'altro: i limiti (3 euro, 0,30 al giorno, 0,50 in totale, 5 ordini, 2 al giorno) sono nel codice e la configurazione puo' solo abbassarli.

## 2. Ogni volta che apri il terminale della prova

Apri PowerShell, entra nella cartella del progetto e prepara il terminale (queste variabili valgono solo in questa finestra):

```powershell
cd C:\Users\faric\Documents\sviluppo\chronos
.\.venv\Scripts\Activate.ps1
Remove-Item Env:COINBASE_API_KEY, Env:COINBASE_API_SECRET -ErrorAction SilentlyContinue
$env:CHRONOS_LIVE_CONFIRM = "I_ACCEPT_REAL_ORDERS"
$env:CHRONOS_EXEC_PROBE_CONFIRM = "BTC-EUR:3.00"
$CFG = "C:\Users\faric\Documents\sviluppo\chronos-probe\config.yml"
$KEY = "C:\Users\faric\chronos-keys\probe.json"
```

Se nell'ambiente c'e' una chiave (`COINBASE_API_KEY`), il programma rifiuta di partire.

## 3. Gli stadi, in ordine (ognuno sblocca il successivo)

Il programma ti fa partire uno stadio solo se il precedente e' andato bene, con la stessa configurazione e nello stesso giorno (UTC). A ogni stadio che puo' inviare ordini devi digitare la frase `CONFERMO BTC-EUR 3.00 EUR`: non esiste un modo per saltarla.

| N. | Comando | Che cosa fa | Dura | Costo |
|---|---|---|---|---|
| 1 | `python -m modules.probe preflight --config $CFG --key-file $KEY` | Legge permessi, prodotto, commissioni, saldo, mercato (solo lettura) | 1 minuto | 0 |
| 2 | `python -m modules.probe run --stage preview --config $CFG --key-file $KEY` | Fa controllare a Coinbase l'ordine vero (a scadenza e normale) senza crearlo | 1 minuto | 0 |
| 3 | `python -m modules.probe run --stage place-cancel --config $CFG --key-file $KEY` | Ordine da 2 euro lontano dal prezzo, annullato dopo 60 secondi | 2 minuti | 0 (non dovrebbe eseguirsi) |
| 4 | `python -m modules.probe run --stage expire --config $CFG --key-file $KEY` | Ordine da 2 euro che deve scadere da solo lato Coinbase | 3 minuti | 0 |
| 5 | `python -m modules.probe run --stage reject --config $CFG --key-file $KEY` | Ordine che Coinbase deve rifiutare (sarebbe da taker) | secondi | 0 |
| 6 | `python -m modules.probe run --stage stop-switch --config $CFG --key-file $KEY` | Prova del freno STOP: ti chiede di cancellare a mano il file indicato | 2 minuti | 0 |
| 7 | `python -m modules.probe run --stage live --config $CFG --key-file $KEY` | La prova vera, finche' la fermi o finisce da sola | giorni | max 3 euro |

Cosa guardare:
- Ogni stadio finisce con `Esito dello stadio ...: OK` oppure `NON OK`. Con `NON OK` non andare avanti: leggi le righe `[NO]` e il file `out\<stadio>.json`.
- `preflight`: se commissioni o minimi sono diversi da quelli del piano, il programma si ferma e dice che li deve ricalcolare il quant.
- `preview`: se dice `gtc_fallback`, Coinbase rifiuta l'ordine a scadenza. Tu hai accettato il rischio SOLO per l'ordine a scadenza: fermati e chiedi. Se la commissione ha solo due decimali (arrotondata al centesimo) anche li' fermati: non si alza l'importo da soli.
- Lo stadio 4 (`expire`) viene saltato da solo se non c'e' l'ordine a scadenza.
- Se qualcosa si esegue per sbaglio (succede solo con prezzi molto strani), il programma lo gestisce come un ingresso vero, con il suo stop su Coinbase.

## 4. Durante la prova (stadio 7)

Avvia lo stadio 7 prima delle 09:00 UTC (10:00 italiane) e lascialo aperto fino alle 20:30 circa. Gli ingressi partono SOLO alle 09:00 e alle 11:00 UTC (10:00 e 12:00 italiane), da lunedi' a venerdi', e soltanto se il mercato e' tranquillo; la seconda finestra serve solo se la prima non si e' riempita o la posizione e' chiusa. Ogni ingresso scade da solo dopo 30 minuti. La posizione ha uno stop e un obiettivo su Coinbase (0,6%) e si chiude comunque dopo 8 ore.

In un altro terminale (stessa preparazione di sezione 2, senza le variabili se vuoi):

```powershell
python -m modules.probe status --config $CFG
```

Mostra: posizione, ordine pendente, conteggi, budget, perdita, e la riga `entries_blocked` (perche' non entra: `manual_stop`, `daily_loss`, `sl_streak_pause`, `finished:...`). Gli eventi dettagliati sono in `out\events.jsonl`.

Freni a mano (creano un file; si tolgono SOLO a mano, il programma non li cancella):
- `python -m modules.probe stop --config $CFG`: niente nuovi ingressi, l'ordine pendente viene annullato con conferma; stop e posizione restano. Usalo nei giorni di FOMC, CPI USA, NFP e riunioni BCE (la sera prima). Per ripartire cancella il file `STOP` nella cartella `state\exec_probe` della configurazione.
- `python -m modules.probe flatten --config $CFG`: come STOP, piu' vende la posizione (a mercato se serve). Poi cancella a mano `FLATTEN` e `STOP`.
- Ctrl+C nella finestra della prova: ferma il programma, annulla l'ordine d'ingresso pendente; gli stop gia' piazzati restano su Coinbase.

Il programma si ferma da solo (e scrive `out\finished.json`) quando: ha usato 5 ordini d'ingresso; ha perso piu' di 0,50 euro in tutto; il budget non basta piu' per un ordine protetto; una posizione e' stata chiusa d'emergenza (uscita WATCHDOG: stop mancante, stop non eseguito, bracket rifiutato); uno stop e' stato eseguito come maker. NON cancellare `finished.json`: e' il segnale che il caso va analizzato (con quant e architetto) prima di qualunque altra mossa.

Pause automatiche, che non sono la fine: perdita di 0,30 euro in un giorno (si riprende il giorno dopo); 2 stop consecutivi (pausa fino a mezzanotte UTC); uno slittamento grave dello stop (oltre 50 bps) crea da solo il file STOP: guarda l'accaduto e poi cancellalo a mano.

Se il programma o il PC si fermano: gli stop gia' piazzati proteggono la posizione su Coinbase. Rischio che hai accettato per iscritto: se il programma si ferma proprio nei 30 minuti dopo l'invio dell'ordine d'ingresso e l'ordine si esegue, la posizione (circa 3 euro) resta senza stop finche' non riavvii lo stadio 7. Riavviandolo, il programma ritrova tutto (ordini, posizione, stop), ti richiede la frase, e non ripete mai un orario gia' servito.

## 5. Dopo

```powershell
python -m modules.probe report --config $CFG --key-file $KEY
```

Solo letture: confronta ogni ordine e ogni fill con Coinbase, i saldi, le commissioni, i messaggi del canale in tempo reale. Scrive `out\probe_report.md` e `.json`. Un criterio e' `pass`, `fail` oppure `not_observed` (non ancora visto: per esempio le commissioni senza nessun fill). La prova e' VALIDATA solo quando tutti passano; allora si scrive il documento di esito `docs/decisions/<data>-p4-prova-esecuzione-minima-esito.md`. Dopo il primo stop eseguito, il rapporto elenca anche i campi dell'ordine figlio (tipo, limite a circa 5% sotto lo stop, stato): servono per confermare il comportamento atteso.

## 6. Cose da NON fare

- Non mettere la chiave di scambio nel progetto, nel `.env`, in un container o in un file dentro `chronos`.
- Non usare Docker per questa prova: gira direttamente sul PC.
- Non avviare due copie della prova insieme, e non usare lo stesso conto con altri programmi.
- Non toccare il database `chronos_probe.db` o i file in `out` a mano.
- Non cercare di alzare i limiti: il programma rifiuta i valori sopra quelli del codice.
- Non rispondere alla frase di conferma con scorciatoie: se non la scrivi esattamente, non parte nulla.

## 7. I file che produce (cartella `chronos-probe\out` e `state`)

| File | Contenuto |
|---|---|
| `preflight.json`, `preview.json`, `place-cancel.json`, `expire.json`, `reject.json`, `stop-switch.json`, `live.json` | esito di ogni stadio |
| `events.jsonl` | un evento per riga: slot saltati, ingressi, uscite, allarmi |
| `status.json` | quello che mostra `status` |
| `user_channel_raw.jsonl.gz` | messaggi grezzi del canale ordini (senza segreti) |
| `ws\` | registrazione del mercato di BTC-EUR |
| `finished.json` | presente solo se la prova e' terminata |
| `probe_report.md`, `probe_report.json` | il rapporto |
| `state\chronos_probe.db` | libro degli ordini e dei fill della prova (non toccarlo) |
| `state\exec_probe\STOP`, `FLATTEN` | i freni a mano |
