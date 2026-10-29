# CONFLITTO kill switch / VirtualAccount - scelte prudenti prese (nessun blocco, da convalidare)

File: `modules/risk/kill_switch.py`, `modules/dryrun/virtual_account.py`. Lo sviluppatore ha proseguito con la scelta indicata;
`architetto-opus` / `trader-quant` possono correggerla senza toccare le API fissate in O2.5/O2.6.

1. **Stale data: prodotto mai osservato = non giudicato.** `SystemGuards` valuta solo i prodotti per cui ha visto almeno una barra
   o un ticker; basta UN prodotto stale per bloccare TUTTI i book. Nel backtest i ticker non esistono e non scattano mai (O2.6).
   Rischio: in live, se il WS ticker muore prima del primo ticker, nessuna guardia scatta (le barre REST continuano). Prudente:
   l'orchestratore deve chiamare `observe_ticker` a ogni ticker e, se vuole il fail-closed all'avvio, non aprire ingressi finche'
   non ha visto un ticker per prodotto. Domanda: serve un parametro `expected_products` con tolleranza di avvio?
2. **`published_at` e' solo diagnostico** (`bar_lag_s`): la soglia `stale_bar_s` si applica a `now - ts_bar_close` dell'ultima
   barra (come da 4.1.4). Confronto stretto (`>`): a esattamente 270 s non scatta.
3. **Loss streak:** il contatore conta ogni chiusura con `exit_reason in {SL, WATCHDOG}` a prescindere dal segno di `pnl_quote`
   (letterale O2.6; con un long-only SL/WATCHDOG chiudono sotto l'ingresso). Allo scatto il contatore riparte da 0: dopo le 48 h
   servono altri 4 stop. Una chiusura durante la pausa che riporta il conteggio a 4 estende la pausa (mai accorcia).
4. **Daily loss:** una volta scattata resta bloccata fino a 00:00 UTC anche se l'equity recupera (latch). Un'osservazione di un
   giorno UTC passato (fuori ordine) e' ignorata del tutto. Equity <= 0 blocca come "exposure" finche' non arriva un'osservazione
   migliore.
5. **Persistenza oltre le 3 guardie della tabella:** oltre a `coverage_breach`, `daily_loss`, `loss_streak` (per book) e
   `decider_health`, `drift` (book `*`), `save()` scrive due righe di servizio per book, `daily_open` (equity di apertura del
   giorno nel campo `reason`, istante nel campo `tripped_at`) e `loss_streak_count`, perche' un riavvio a meta' giornata non
   riazzeri l'equity di apertura (altrimenti la perdita gia' subita sparirebbe dal calcolo). Stale ed exposure non sono
   persistiti (si ricalcolano dalla prima osservazione). Schema invariato. `save()` sostituisce TUTTE le righe dei book che il
   `KillSwitch` possiede piu' `*`: un solo `KillSwitch` per database.
6. **Coverage breach:** nessun timeout, solo `reset_coverage_breach()`. `entry_blocked` restituisce codici brevi
   (`coverage_breach`, `daily_loss`, `loss_streak`, `exposure`, `stale_bar:<prodotto>`, `stale_data:<prodotto>`,
   `decider_health`, `drift`, `unknown_book:<book>`); un book sconosciuto e' bloccato e `coverage_breach_active` e' `True`
   (fail-closed). Ordine: sistema, poi conto; nel conto breach > daily loss > streak > exposure.
7. **`VirtualAccount`:** PnL realizzato/non realizzato al LORDO delle fee (costo medio = solo prezzo), fee in `fees_paid`; e'
   cosi' che vale l'identita' `quote + sum(base x mark) + fees == start + realized + unrealized`. Per il `pnl_quote` da passare a
   `observe_trade_closed` usare `net_realized_pnl` (differenza fra due letture) o calcolarlo nel gateway. `equity()` chiede un
   prezzo di mark per ogni base posseduta (`KeyError` altrimenti: niente prezzi inventati). `mark_prices` e `base_of` sono
   indicizzati per prodotto (`"SOL-EUR"` -> `"SOL"`). Nessun `release()`/`reserve()` proprio: i terminali si rilasciano con
   `account.balances.release(client_order_id)` (chi scrive `bar_gateway`/`shadow_gateway` deve usare quello).
