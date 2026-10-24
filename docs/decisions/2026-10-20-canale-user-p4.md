# Canale `user` di Coinbase: che cosa prova il soak di sola lettura e che cosa resta da provare al primo micro-ordine di P4

Autore: Sviluppatore (Sonnet). Data: 2026-10-20. Stato: soak eseguito, difetti del flusso corretti; la validazione dei messaggi di ORDINE e di FILL e' rimandata al primo micro-ordine reale di P4 (checklist sotto). Nessun ordine reale e' stato inviato.

Codice: `script/user_channel_soak.py` (test: `tests/test_user_channel_soak.py`), correzioni in `modules/execution/user_stream.py` (test: `tests/test_user_stream_reconnect.py`). Lacuna (8) di `tasks/todo.md` sez. 13.

## 1. Che cosa fa il soak

`python -m script.user_channel_soak --minutes 45 --out <cartella> [--inject KIND@MIN[:SEC] ...]`

- Usa le STESSE classi dell'applicazione: `CoinbaseUserStream` sopra `CoinbaseUserTransport` (SDK `WSUserClient`), il parser reale `parse_user_message`, il `Reconciler` e l'`OrderEventSink` reali. Le uniche aggiunte sono due prese: un involucro del trasporto che timbra e registra ogni messaggio grezzo prima che il flusso lo veda, e un proxy dell'exchange che ammette solo letture (`get_accounts`, `list_open_orders`, `list_orders`, `get_best_bid_ask`, `get_fills`; qualunque altro nome solleva).
- Sola lettura: legge i permessi della chiave e rifiuta di partire (exit 4, nessuna connessione, nessuna cartella creata) se `can_trade` o `can_transfer`. Il ledger e' in memoria; si scrive solo in `--out` (registrazione grezza `user_channel_raw.jsonl.gz`, `user_channel_soak.json`, `.md`, `soak.log`).
- Segreti: ogni messaggio grezzo e ogni riga di log passa da `redact` (chiavi note, blocchi PEM, JWT, nome della chiave `organizations/.../apiKeys/...`, `Bearer ...`, campi `jwt`/`token`/`signature`/`retail_portfolio_id`); nei log sono omessi i campi con importi (`detail`, `qty`, `size`, `limit_price`, ...). Il rapporto contiene conteggi e codici di valuta, mai importi.
- Guasti iniettati, dichiarati nel rapporto: `close` (l'SDK chiude il socket e chiama `on_close`), `abort` (il socket muore senza alcun callback, come una caduta lato server), `silence` (i messaggi arrivano ma non sono consegnati al flusso per N secondi), `refuse` (abort e poi ogni nuova connessione fallisce per N secondi). Un conto quieto non produce mai questi eventi: senza iniezione non si vedrebbe come reagisce il codice.

## 2. Che cosa il soak PROVA (con una chiave View e nessun ordine)

Misurato con la chiave View reale il 2026-10-20 (numeri della corsa da 45 minuti nella sez. 6):

1. Autenticazione e sottoscrizione: l'endpoint `wss://advanced-trade-ws-user.coinbase.com` accetta la chiave; la sottoscrizione `user` + `heartbeats` e' confermata da due messaggi `subscriptions`.
2. Ordine dei messaggi alla connessione (reale): `sequence_num` 0 = snapshot `user` (`events[0].type = "snapshot"`, `orders: []`, `positions` con tre chiavi, fra cui `prediction_market_positions`); 1 = ack con `{"user": [<id portafoglio>]}` (NON gli id prodotto); 2 = ack con `heartbeats` e `user`; poi un heartbeat al secondo con `heartbeat_counter` consecutivo.
3. Cadenza degli heartbeat: mediana 1,0000 s, p99 1,12 s, max 1,81 s (2.647 intervalli in 45 minuti); ritardo di arrivo del feed (orologio locale contro timestamp del messaggio) mediana circa 0 s, p99 0,13 s, max 0,81 s.
4. Continuita' di `sequence_num`: un solo contatore per connessione, da 0, su tutti i canali, senza salti; riparte da 0 a ogni connessione (e questo il flusso lo tratta come `reset`).
5. Il parser reale accetta tutti i messaggi osservati (errori di parsing 0), compresa la chiave nuova `prediction_market_positions` che il codice ignora.
6. Comportamento del flusso a caduta, a silenzio e a rifiuto di connessione (sez. 4): riconnessione, richiesta di riconciliazione al primo messaggio dopo il buco, nessuna riconnessione spuria.
7. `Reconciler.on_startup()` reale contro un ledger vuoto: lettura dei saldi e degli ordini aperti del conto reale, snapshot applicato, `free` del `BalanceBook` uguale al disponibile per ogni valuta, nessun allarme di copertura. Il conto reale ha 114 account, 1 sola valuta con saldo (MANA, nessun EUR) e 0 ordini aperti: quindi i casi "ordine estraneo" e "hold senza ordine" NON sono stati esercitati sui dati reali (sono coperti solo dai test con il finto).
8. Riconciliatore a regime: ogni riconnessione fa partire un ciclo (`ws_connected`, `ws_feed_silent`, ...) e il ciclo periodico gira senza errori.

## 3. Che cosa il soak NON PUO' provare

Con una chiave View e senza ordini non arriva nessun messaggio di ordine o di fill. Restano NON validati:

- il formato dei messaggi `update` di ordine: stati (`PENDING`/`OPEN`/`FILLED`/`CANCELLED`/`EXPIRED`/`FAILED`), `cumulative_quantity`, `leaves_quantity`, `avg_price`, `total_fees`, `limit_price`, `stop_price`, `order_side`, `client_order_id`;
- il formato dei fill e i tempi con cui `get_fills` li pubblica rispetto al messaggio del canale (la regola "ordine non finalizzato se i fill locali non coprono `filled_size`" e' costruita sull'ipotesi che il REST sia in ritardo);
- il comportamento degli ordini bracket (`trigger_bracket_gtc`): significato di `stop_price`, `trigger_status`, `limit_price` quando il bracket scatta;
- l'eco di `client_order_id` sui messaggi (serve a distinguere gli ordini di Chronos da quelli manuali);
- l'ordine fra messaggio WS e visibilita' REST (`get_order`), e l'uso reale di `OrderEventSink` + `BalanceBook.apply_fill` con fill veri;
- snapshot con ordini dentro: la corsa ha ricevuto uno snapshot vuoto, quindi `parse_order_message` non ha mai visto un ordine reale.

## 4. Difetti trovati dal soak reale e corretti in `user_stream.py`

Trovati iniettando guasti sul feed vero (corsa di prova da 5 minuti, prima delle correzioni) e poi riprodotti con test (6 su 7 dei test nuovi falliscono sul codice precedente):

1. Congelamento permanente. Dopo una chiusura il flusso ha smesso di elaborare messaggi (46 elaborati contro 88 ricevuti) e non ha piu' rilevato il silenzio; la corsa non terminava piu' (chiusura in attesa all'infinito). Cause: (a) `close()` dell'SDK richiama `on_close` e quel callback era trattato come nuova caduta, quindi ogni riconnessione per silenzio con socket vivo ne provocava un'altra che chiudeva la connessione appena aperta; (b) due `close()` concorrenti sullo stesso client SDK possono non tornare mai, e `_consume` restava bloccato dentro `_reconnect`; (c) `check_silence` restava disattivato per sempre se `_silent` non veniva azzerato da un messaggio. Correzione: ogni trasporto ha una generazione; messaggi e `on_close` di un trasporto sostituito sono scartati (`stats.stale_dropped`); un solo `_reconnect` alla volta; timeout su open+subscribe (30 s) e su close (10 s).
2. Riconnessione rifiutata = flusso morto: se `_connect` falliva dopo una caduta nessuno riprovava. Ora `_reconnect` ritenta con backoff finche' riesce (`stats.connect_failures`), chiudendo ogni trasporto aperto a meta'.
3. Una connessione che si apre e non consegna mai nulla non veniva mai rilevata (`last_message_at is None`): il silenzio e' ora misurato anche dalla connessione.
4. Un thread dell'SDK (con il suo event loop) restava vivo a ogni socket morto, perche' `close()` solleva prima di fermarlo: `CoinbaseUserTransport.close` ora lo ferma.

Lo stesso schema (callback `on_close` dell'SDK trattato come caduta, `_silent` che blocca il controllo, nessun retry dopo un `_connect` fallito) era presente in `modules/market/ws_feed.py` (`CoinbaseMarketStream`). Il 2026-10-23 un altro agente vi ha corretto il retry della riconnessione fallita (e ha aggiunto un controllo "nessun messaggio dopo la riconnessione"); lo stesso difetto in `user_stream.py` era gia' corretto da questo lavoro (punto 2), quindi nessuna ulteriore modifica. `ws_feed.py` NON e' stato toccato qui: resta da verificare, con lo stesso metodo (soak con `close`/`abort`/`silence` sul feed pubblico), se ignora ancora il callback `on_close` generato dalla propria `close()` (la riga `_reconnect("connection closed")`), che nel canale `user` causava una seconda riconnessione.

## 5. Checklist esatta per il PRIMO micro-ordine reale di P4

Prerequisiti: chiave Trade senza `can_transfer`, ordine da circa 5 EUR, BUY limit `post_only` lontano dal mercato per il primo giro (stato OPEN e poi CANCELLED) e poi un secondo ordine che si esegue (fill reale, anche parziale se possibile); sempre dopo `BalanceGuard.authorize`.

A. Prima di inviare. (1) In un secondo terminale, con la chiave View: `python -m script.user_channel_soak --minutes 30 --out <cartella> --overwrite` (la chiave View riceve gli stessi messaggi del conto: DA VERIFICARE, e' la prima domanda; se non li riceve la registrazione va fatta dal processo Trade). Il soak rifiuta la chiave Trade: non usarla. (2) Annotare `client_order_id` e `order_id` dell'ordine. (3) Non toccare altri ordini del conto durante la prova.

B. Dopo ogni transizione, estrarre dalla registrazione grezza i messaggi con `events[].orders[]` dell'ordine e confrontare CAMPO per CAMPO con REST `get_order` (e con `get_fills` per i fill). Tabella di mappatura che il codice presume:

| Canale `user` (`orders[]`) | REST (`get_order` / `OrderInfo`) | Atteso |
|---|---|---|
| `order_id` | `order_id` | uguale |
| `client_order_id` | `client_order_id` | uguale, non vuoto (eco) |
| `product_id` | `product_id` | uguale |
| `order_side` | `side` | `BUY`/`SELL` |
| `status` | `status` | uno di OPEN, FILLED, CANCELLED, EXPIRED, FAILED (annotare ogni altro valore, es. PENDING/QUEUED) |
| `cumulative_quantity` | `filled_size` | uguale |
| `cumulative_quantity + leaves_quantity` | `order_configuration.*.base_size` | uguale |
| `avg_price` | `average_filled_price` | uguale (`0` se nessun fill) |
| `limit_price` | `order_configuration.*.limit_price` | uguale; `0` o vuoto = non applicabile |
| `stop_price` | `stop_trigger_price` del bracket | uguale; `0`/vuoto = non applicabile |
| `total_fees` | `total_fees` | uguale entro l'arrotondamento |
| `number_of_fills` | numero di righe di `get_fills` | uguale |

C. Cose da annotare per ciascun messaggio: (1) i `sequence_num` e il tempo di arrivo; (2) per ogni fill, quanti ms passano fra il messaggio WS con `cumulative_quantity` aumentata e la comparsa del fill in `get_fills` (se `get_fills` e' in ritardo il sink restituisce `incomplete` e chiede una riconciliazione: verificare che si chiuda da solo e in quanto tempo); (3) se compare un messaggio `update` con `status` terminale PRIMA dell'ultimo fill; (4) se lo snapshot iniziale a una nuova connessione contiene l'ordine ancora aperto con gli stessi campi; (5) il bracket: dopo il fill dell'ingresso, i campi del messaggio del `trigger_bracket_gtc` (`order_type`, `stop_price`, `limit_price`, `trigger_status`) contro `get_order`; (6) il messaggio dell'annullamento (`cancel_reason`) e quello del rifiuto (`reject_reason`) provocando un rifiuto innocuo (es. `post_only` che attraverserebbe il book).

D. Verifiche di effetto, dopo ogni transizione: il `BalanceBook` (`reserved`/`free`) e il ledger (`orders.status`, `fills`, `positions`) devono coincidere con una lettura REST di `get_accounts` e `get_order`/`get_fills`; nessun allarme di copertura; il rilascio della riserva avviene solo allo stato terminale confermato.

E. Se i messaggi DIFFERISCONO da quanto sopra: (1) NON correggere a memoria: conservare la registrazione grezza (redatta) e le risposte REST, e scrivere il caso in `.claude/CONFLITTO.md` (protocollo di CLAUDE.md); (2) adattare solo `parse_order_message` (un campo alla volta) con un test che usa il messaggio reale come fixture in `tests/fixtures`; (3) se un campo manca o ha un significato diverso, la fonte di verita' resta il REST (`get_order`/`get_fills`): il canale `user` serve solo a dire "l'ordine si e' mosso" e la riconciliazione periodica ogni `reconcile_interval_s` copre il resto; (4) se i fill del WS e del REST non concordano, bloccare le entrate (`coverage_breach`) finche' la differenza non e' spiegata; (5) ripetere il primo micro-ordine dopo la correzione prima di aumentare l'importo.

F. Criterio di uscita: tutti i campi della tabella B verificati su almeno un ordine OPEN, un annullato, un fill completo, un fill parziale (o la sua assenza dichiarata) e un bracket; il tempo massimo fra WS e REST misurato e minore di `entry_poll_s`; nessun allarme.

## 6. Risultati della corsa reale da 45 minuti (chiave View, 2026-10-20 11:21-12:06 UTC)

Cartella `C:\Users\faric\Documents\sviluppo\chronos-soak-user\2026-10-20` (fuori dal repo): `user_channel_soak.json`/`.md`, `user_channel_raw.jsonl.gz` (2.667 messaggi grezzi redatti), `soak.log`. Verificato che nessun file contiene JWT, nome della chiave o PEM. Una prima corsa (`2026-10-20-run1-partial`) e' stata interrotta a 22 minuti perche' l'iniettore restava bloccato nella stessa `close()` concorrente descritta al punto 1 della sez. 4 (difetto dello script, corretto: le chiamate di guasto hanno un timeout di 5 s); non entra nei numeri.

- Esito: 15 controlli su 15 superati, exit 0. Durata 2.710 s su 2.700. Autenticazione OK su tutte le connessioni riuscite; sottoscrizioni confermate `user` e `heartbeats`; `open` 0,65 s; dall'invio della sottoscrizione all'ack 0,8-1,1 s.
- Messaggi: 2.667 (2.652 heartbeat, 10 ack `subscriptions`, 5 snapshot `user` vuoti). Errori di parsing 0 (registratore e flusso). Buchi di `sequence_num` 0, reset 0 (il primo numero di ogni connessione e' 0); salti di `heartbeat_counter` 0.
- Heartbeat: intervallo mediana 0,9999 s, p99 1,1155 s, max 1,8091 s.
- Guasti iniettati (4) e reazione del codice reale: `close` a 480 s (buco di 7,4 s), `abort` a 960 s (7,3 s), `silence` di 12 s a 1.440 s (12,3 s), `refuse` di 15 s a 1.920 s con 3 tentativi rifiutati e ritentati con backoff (21,4 s). Riconnessioni del flusso 4 su 4 iniettate, 0 inattese; ogni riconnessione ha chiesto una riconciliazione (`ws_feed_silent`) al primo messaggio dopo il buco (5 richieste con `ws_connected`); nessun congelamento. Thread del processo 3 all'inizio, 6 alla fine.
- Rilevamento: tutti e quattro i guasti sono stati visti dal silenzio (5 s); la `close()` iniettata ha richiesto piu' di 5 s per la stretta di mano di chiusura del server.
- Riconciliatore reale in lettura: `on_startup` 0,40 s, snapshot applicato, 114 account letti, 1 valuta con saldo (MANA), 0 ordini aperti, `free` uguale al disponibile per ogni valuta, 0 allarmi di copertura; 12 letture degli ordini aperti (periodiche e su richiesta) senza errori. Snapshot WS contro REST: 0 contro 0.
- Resta vero cio' che la sez. 3 elenca come NON provato: nessun messaggio di ordine o di fill e' stato visto.
