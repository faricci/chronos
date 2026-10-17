<!-- Risoluzione di architetto-opus del 2026-10-17 (copertura spot, vincolo C11). La PARTE C e' gia' nella spec v2.4; PARTE A, B e D restano il riferimento per i moduli ancora da scrivere: sizing.py, risk_manager.py, virtual_account.py, exchange.py, gateway.py, shadow_gateway.py, balance_feed.py, reconciler.py. -->

# RISOLUZIONE ARCHITETTONICA - Copertura spot, divieto di leva, protezione dal debito

Da: architetto-opus. Per: Sviluppatore (Sonnet).
Stato: DECISO. Sblocca `modules/execution/*`, `modules/risk/sizing.py`, `modules/risk/risk_manager.py`,
`modules/dryrun/virtual_account.py`. Spec di riferimento: v2.3 -> diventa v2.4 con i testi della PARTE C.

Fonte dei vincoli: CLAUDE.md, sezione "VINCOLI FINANZIARI ASSOLUTI". Questa risoluzione li traduce in codice; in caso
di conflitto fra spec e CLAUDE.md vince CLAUDE.md.

---

## PARTE 0 - Principi (validi per tutte le risposte)

P1. **Il guard e' un veto deterministico post-policy, non un token decisionale.** Le risposte del modello (`action`,
    `edge`, `vol_regime`, approccio Jev) non vengono toccate. L'esito del guard e' un `Skip` con `reason` enumerato nel
    `DecisionRecord` (`policy_final` resta `BUY`, cambia solo l'esito di esecuzione). Il `LabelResolver` etichetta la
    decisione come sempre: nessun effetto sui dati di training.
P2. **Unica autorita' di copertura:** `BalanceGuard.authorize()`. Il sizing usa le stesse formule solo per ridurre la
    probabilita' di un rifiuto; non sostituisce il guard.
P3. **Nessun ordine senza ticket.** L'unico modo di inviare un ordine (live o shadow) e' passare un `CoverageTicket`
    emesso dal guard. Il body JSON viene costruito dal ticket, non dal chiamante.
P4. **Fail-closed per gli acquisti, mai per le uscite.** Un BUY con dati di saldo incerti viene abortito. Una SELL che
    riduce una posizione posseduta non viene mai bloccata dall'eta' dei dati (una vendita spot non puo' creare debito);
    viene bloccata solo se la base posseduta non basta, e in quel caso non c'e' nulla da vendere.
P5. **Prudenza sui comportamenti Coinbase non verificati.** Ogni regola sotto e' valida in tutte le ipotesi elencate.
    Le ipotesi non verificate sono dichiarate in PARTE D e controllate in esercizio da allarmi automatici.
P6. **Formule di costo solo in `modules/risk/costs.py`** (sez. 0.3). Le formule di copertura sono formule di costo:
    stanno li', le importano sizing, guard e `VirtualAccount`.

---

## PARTE A - Decisioni per domanda

### Q1 - Formula di copertura

DECISIONE: confermata la proposta con tre correzioni (tasso di fee prudente, arrotondamento, ordine degli arrotondamenti).

Tasso di fee di copertura (non e' un costo: non entra in EV, label, fee floor):
```
COVERAGE_FEE_FLOOR_BPS = 120                                    # costante in costs.py, NON in config
f_cov = max(fees.taker_bps, fallback_fees.taker_bps, COVERAGE_FEE_FLOOR_BPS) / 1e4   # Decimal
```
Motivo: limite superiore della fee di qualunque fill (maker o taker, tier vecchio o corrente). Il floor non viola C2:
C2 vieta di hardcodare il costo; qui e' un tetto di prenotazione. Sovra-prenotare di <= 1.2% non cambia il PnL.

BUY (solo `limit_limit_gtc`, maker o taker):
```
required_quote = ceil_to(limit_price * base_size * (1 + f_cov), quote_increment) + quote_increment
condizione:      free(quote) >= required_quote
```
Il `+ quote_increment` e' il cuscinetto per l'arrotondamento per-fill delle fee. Vale sia se Coinbase blocca
(`hold`) il solo nozionale sia se blocca nozionale + fee: in entrambi i casi `hold <= required_quote` (PARTE D, H1).

SELL (limit d'uscita, bracket standalone, `market_market_ioc` di uscita/watchdog):
```
required_base = base_size
sellable      = min(position.base_qty - reserved_sell_base(book, product), free(base))
condizione:   sellable >= required_base
```
Nessuna copertura in quote: per ogni fill `delta_quote = p*q - fee >= p*q*(1 - f_cov) > 0`, quindi una SELL non puo'
rendere negativo alcun saldo (PARTE D, H3 per la valuta della fee).

Inversa per il sizing (in costs.py):
```
max_base_for_quote(q, limit_price, f_cov, quote_inc, base_inc):
    budget = floor_to(q, quote_inc) - quote_inc
    if budget <= 0: return 0
    s = floor_to(budget / (limit_price * (1 + f_cov)), base_inc)
    if required_quote(limit_price, s, f_cov, quote_inc) > q: s -= base_inc     # correzione arrotondamento Decimal, max 1 volta
    return max(s, 0)
postcondizione (property test): required_quote(limit_price, s, ...) <= q
```

Sostituzione del `0.98` (4.1.2): SI'.
```
notional_cap_base = min(equity * max_position_frac, max_order_quote) / entry
cover_cap_base    = max_base_for_quote(free(quote), entry, f_cov, quote_increment, base_increment)
size_cap          = floor_to(min(notional_cap_base, cover_cap_base), base_increment)
```
`entry` e' il prezzo limite GIA' arrotondato (vedi modifica a 4.1.3: l'arrotondamento del limite d'ingresso si sposta
PRIMA di SL/TP e del sizing). Il guard resta l'autorita' finale; il sizing sullo stesso snapshot non deve mai produrre
un ordine che il guard rifiuta (property test T4).

### Q2 - Bracket e fill parziali

DECISIONE: in v2.4 il bracket e' SEMPRE standalone, piazzato DOPO lo stato terminale dell'ingresso
("percorso a due passi" promosso a percorso unico). `attached_order_configuration` non si usa e viene tolto dalla
whitelist del body.

Motivo: la semantica dell'attached bracket su fill parziale non e' verificabile (PARTE D, H2). Nelle ipotesi
"bracket per la size intera" la gamba potrebbe vendere base non di Chronos (es. SOL dell'utente sullo stesso conto),
violando il vincolo 3. Il bracket standalone ha `base_size` esplicita = quantita' posseduta: l'invariante
`bracket_base_size <= filled_base_size` vale per costruzione, in ogni ipotesi.

Ordine delle operazioni (obbligatorio, `LiveExecutionGateway` e `ShadowExecutionGateway`):
```
E1  authorize(BUY limit) -> ticket ; create_order(ticket)
E2  attesa dello stato terminale dell'ingresso:
      - FILLED                                  -> E4
      - timeout (maker 1800 s, taker 20 s)      -> E3
    backup al WS user: get_order ogni execution.entry_poll_s (5 s) finche' l'ingresso non e' terminale
    dal PRIMO fill: positions[book,product] = LONG, base_qty = filled cumulato, bracket_order_id = NULL
E3  cancel(residuo) -> attesa conferma terminale (CANCELLED/FILLED) con filled_size definitivo
      timeout conferma execution.cancel_confirm_timeout_s (10 s) -> get_order -> ripeti finche' terminale (allarme dopo 3)
E4  release(riserva residua dell'ingresso)
    filled_total == 0                              -> fine, nessuna posizione
    s = floor_to(min(filled_total, sellable), base_increment)
    s < min_sell_size(product, bid)                -> DUST (sotto)
    altrimenti authorize(SELL bracket, s) -> trigger_bracket_gtc standalone ; positions.bracket_order_id = id
min_sell_size soddisfatto se: s >= base_min_size  E  s * best_bid >= quote_min_size
```
Posizione LONG senza bracket (finestra fra primo fill e E4, oppure bracket rifiutato): il watchdog usa come trigger
lo **SL stesso** (stop virtuale), non `SL - 1 x ATR_1h`. Azione: cancel residuo ingresso (con conferma) ->
`market_market_ioc` SELL di `floor_to(min(base_qty, sellable))`. Perdita massima comunque <= capitale allocato
(Q6). Con barriere a 2 ATR giornalieri il rischio della finestra di 30 min e' marginale.

DUST (fill totale sotto il minimo vendibile): la riga `positions` resta `side = 'FLAT'` con nuova colonna
`dust_qty TEXT NOT NULL DEFAULT '0'` e `dust_cost TEXT NOT NULL DEFAULT '0'` (quote spesa, fee incluse). Il watchdog la
ignora, lo `StateBuilder` vede FLAT. Al fill del prossimo ingresso sullo stesso (book, prodotto) la dust si fonde:
`base_qty = filled + dust_qty`, `avg_entry` pesato, dust azzerata, flag `dust_merged=true` nel `VirtualTrade` / trade
live. Perdita massima della dust = suo nozionale < `quote_min_size` (vincolo 3 rispettato).

`available_base` dell'exchange: il campo `available_balance` di `get_accounts` esclude gli `hold` (PARTE D, H4).
Comunque il guard non usa il valore grezzo: usa `free(base)` del `BalanceBook`, che sottrae le riserve locali di tutti
gli ordini SELL ancora non terminali. Un nuovo SELL prima della conferma di cancellazione del bracket precedente viene
quindi rifiutato per costruzione.

`submit_exit` e `emergency_exit`: NESSUN rilascio ottimistico.
```
X1  cancel(bracket) -> attesa conferma terminale (come E3)
X2  se il bracket e' terminale FILLED (TP/SL eseguito durante la cancel) -> posizione gia' chiusa, uscita annullata
    se parzialmente fillato -> si vende solo il residuo del ledger
X3  release(riserva del bracket)   # solo ora la base torna free
X4  s = floor_to(min(position.base_qty, sellable), base_increment) ; authorize(SELL limit|market, s)
X5  limit marketable a best_bid ; timeout exit_timeout_s ; cancel con conferma ; release ;
    authorize(SELL market, residuo) ; market_market_ioc
```
Il rilascio ottimistico e' vietato perche' la corsa "cancel vs fill del bracket" produrrebbe una doppia vendita.
Il watchdog non puo' essere bloccato dall'eta' del saldo (P4): `authorize` per SELL non applica la regola di staleness.

### Q3 - BalanceBook: stato in memoria, aggiornamento, staleness

DECISIONE: un `BalanceBook` per book, in memoria, mutato solo dal thread dell'event loop con metodi sincroni (nessun
`await` fra check e reserve = atomicita' senza lock). Nessuna chiamata di rete nel percorso d'invio.

Stato per valuta `c`:
```
free[c]              Decimal >= 0     # spendibile ora
reservations[cid]    {currency, product_id, side, amount >= 0}   # residuo prenotato per ordine non terminale
reserved[c]          = somma amount delle reservations su c      # mantenuto incrementale, O(1)
total[c]             = free[c] + reserved[c]
epoch                int, +1 a ogni mutazione
snapshot_at          datetime | None (solo sorgente "exchange")
```

Formule (a), con `R0 = required_quote` (BUY) o `base_size` (SELL):
```
reserve(cid, c, R0):          require free[c] >= R0 ; free[c] -= R0 ; reservations[cid] = R0
BUY  fill (p <= limit, q, fee in quote):
    consumed = p*q + fee
    reservations[cid] -= consumed
    if reservations[cid] < 0:                         # impossibile se H1/H3 valgono
        over = -reservations[cid] ; reservations[cid] = 0 ; free[quote] = max(free[quote] - over, 0)
        allarme reservation_overdraw -> KillSwitch coverage_breach(book)
    free[base] += q
SELL fill (q, p, fee in quote):
    reservations[cid] -= q        (mai < 0: il fill non supera la size dell'ordine; se succede -> stesso trattamento)
    free[quote] += p*q - fee
terminale (FILLED | CANCELLED | EXPIRED | REJECTED | FAILED):
    free[c] += reservations[cid] ; del reservations[cid]          # idempotente
```
Invarianti: `free >= 0`, `amount >= 0`; reserve/release lasciano `total` invariato; un fill BUY cambia
`total[quote]` di esattamente `-consumed` e `total[base]` di `+q`; un fill SELL di `-q` base e `+(p*q - fee)` quote.

Refresh (b) - solo sorgente "exchange" (live):
```
task in background ogni execution.balance_refresh_s (default 30 s):
    e0 = book.epoch ; snap = await exchange.get_accounts() ; open = await exchange.list_open_orders()
    if book.epoch != e0: scarta, riprova dopo 1 s           # evento locale durante la lettura
    per ogni valuta c:
        A = available_balance, H = hold, T = A + H
        H_credit = min(H, reserved[c])   se nessun ordine aperto non-Chronos tocca c
                 = 0                     altrimenti          # ordini estranei: non si accredita l'hold
        free[c]  = max(A - (reserved[c] - H_credit), 0)
        allarme hold_exceeds_reservation se H_credit == min(H, reserved) e H > reserved[c] + quote_increment
                                         e nessun ordine estraneo -> KillSwitch coverage_breach(book)
        drift = |T - total_locale_prima| ; drift > max(2 * increment(c), 1e-4 * T) -> allarme balance_drift
    snapshot_at = now
"non-Chronos" = client_order_id assente dalla tabella orders del book live.
```
La formula e' prudente in ogni corsa fra snapshot ed eventi locali (sottostima `free`, mai sovrastima): riserve
appena create non ancora in hold vengono sottratte; hold di ordini appena chiusi localmente non vengono riaccreditati.

Staleness (b, c):
```
BALANCE_MAX_AGE_S = 120        # costante in balance_guard.py, NON in config
BUY : se now - snapshot_at > 120 s (o snapshot_at is None) -> BalanceStale, Skip(reason="balance_stale")
SELL: mai bloccata dalla staleness (P4)
```
Nessun refresh sincrono nel percorso caldo. Lo stato "degradato" con saldo locale > reale e' NON accettabile per i BUY
(vincolo 2 e' assoluto): fail-closed obbligatorio. Costo: con orizzonte di giorni, saltare un ingresso costa poco.
Vincolo di config: `balance_refresh_s <= BALANCE_MAX_AGE_S / 3` (almeno 3 tentativi prima del fail-closed).

KillSwitch `coverage_breach` (ambito book): blocca i BUY del book fino a reset manuale (cancellazione della riga in
`kill_switch_state`); le uscite restano attive. Scatta su: `reservation_overdraw`, `hold_exceeds_reservation`,
>= 3 `balance_drift` in 24 h.

(d) Confermato: il canale WS `user` porta ordini e fill, non saldi spot. Il `BalanceBook` e' tenuto aggiornato dagli
eventi d'ordine/fill (tempestivi) e corretto dallo snapshot REST. Se in futuro esistesse un canale saldi spot, sarebbe
solo un'altra sorgente di snapshot con la stessa regola `epoch`.

Diagramma degli stati di una riserva (per `client_order_id`):
```
             authorize ok
   (none) ---------------> RESERVED --create_order ok--> OPEN --fill--> PARTIAL --fill--> ...
                              |                           |               |
                              | rifiuto definitivo        | terminale     | terminale
                              v                           v               v
                           RELEASED <---------------- RELEASED <------ RELEASED
                              ^
                              | non trovato dopo 2 cicli di riconciliazione
   RESERVED --errore di rete ambiguo--> UNKNOWN --trovato aperto--> OPEN
                                            (retry create_order con lo stesso client_order_id, max 3)
```
Stato del book (per il solo BUY): `FRESH` (snapshot <= 120 s) | `STALE` (> 120 s, BUY rifiutati) |
`BREACHED` (KillSwitch coverage_breach attivo, BUY rifiutati). Sorgente "virtual": sempre `FRESH`.

### Q4 - Un solo guard per live e dry

DECISIONE: si', con una variante: non due implementazioni di `BalanceSource`, ma **un'unica classe `BalanceBook`** con
`source: Literal["exchange", "virtual"]`. Differiscono solo l'alimentazione (snapshot REST vs capitale iniziale) e la
regola di staleness (non applicata a "virtual", dove il libro locale e' la verita'). `VirtualAccount` COMPONE un
`BalanceBook` (nessuna logica di riserva duplicata).

- `BalanceGuard` e' lo stesso codice nei due modi. Non e' disattivabile: nessuna chiave di config lo controlla e i
  costruttori dei gateway lo richiedono come argomento non opzionale.
- Punto di chiamata: SOLO dentro `ExecutionGateway` (live e shadow), come ultimo passo prima dell'invio. Il
  `RiskManager` legge il `BalanceBook` in sola lettura per il sizing; non prenota.
- Impossibile aggirarlo: `CoinbaseExchange.create_order(ticket: CoverageTicket)` e lo shadow submit accettano solo un
  ticket; il body e' costruito da `build_order_body(ticket.spec)`.
- `InsufficientCoverage`, `BalanceStale`: non fatali, non ritentate; per un ingresso -> `Skip(reason=...)` nel
  `DecisionRecord`; per un'uscita -> log `exit_not_covered` + riconciliazione immediata (ledger e saldo divergono).
- `buy_and_hold` (dry) salta il `RiskManager` ma NON il guard: la sua size e'
  `max_base_for_quote(free(quote), ...)`.
- `training/backtest.py` e il `SimulatedExchange` usano `VirtualAccount` -> guard attivo anche in backtest e in
  `tune_policy` (stessi vincoli del live).

### Q5 - Divieto di leva, hard-coded

DECISIONE: proposta confermata con queste precisazioni.

1. Registro spot all'avvio (`SpotRegistry.load()`), per ogni prodotto della config, da `get_product`:
   ```
   product_type == "SPOT"
   future_product_details assente o null
   product_venue in SPOT_VENUES (se il campo e' presente; SPOT_VENUES = valori registrati dal probe per i 3 prodotti, atteso "CBE")
   product_id == f"{base_currency_id}-{quote_currency_id}" e corrisponde a ^[A-Z0-9]+-[A-Z]+$
   trading_disabled == false, cancel_only == false, view_only == false (se presenti)
   ```
   Un prodotto che fallisce -> `SpotViolation` -> exit code 4. Allowlist, mai denylist (niente controlli su "-PERP").
2. Permessi della key in live (`get_api_key_permissions`): `can_transfer == false`, `can_trade == true`,
   `portfolio_type` in allowlist registrata dal probe (attesi "DEFAULT"/"CONSUMER", da confermare); altrimenti rifiuto
   di partire in live. Raccomandazione operativa all'utente: portafoglio Coinbase dedicato a Chronos.
3. Matrice ammessa (unica, in `build_order_body`):
   | side | order_configuration | chiavi interne ammesse |
   |------|---------------------|------------------------|
   | BUY  | `limit_limit_gtc` | `base_size`, `limit_price`, `post_only` |
   | SELL | `limit_limit_gtc` | `base_size`, `limit_price`, `post_only` (=false) |
   | SELL | `trigger_bracket_gtc` | `base_size`, `limit_price`, `stop_trigger_price` |
   | SELL | `market_market_ioc` | `base_size` |
   Chiavi top-level ammesse: esattamente `client_order_id`, `product_id`, `side`, `order_configuration`.
   Qualunque altra (`attached_order_configuration`, `leverage`, `margin_type`, `retail_portfolio_id`, `preview_id`,
   `quote_size`, ...) o combinazione fuori matrice (es. BUY market) -> `SpotViolation` (fatale, exit 4) prima della rete.
4. `CoinbaseExchange` tiene il `RESTClient` privato (`_client`). Allowlist dei metodi del client usati:
   `get_product`, `get_transaction_summary`, `get_best_bid_ask`, `create_order`, `cancel_orders`, `list_orders`,
   `get_order`, `get_fills`, `get_accounts`, `get_api_key_permissions`, `preview_order`, `get_public_candles`,
   `get_candles`. Il fallback `client.post(...)` per l'attached bracket e' rimosso.
5. SELL solo con `base_size <= sellable` (Q1): mai short, mai base non di Chronos.
6. Test: vedi PARTE B, T9-T12. Le chiavi View-only in dry non rendono superfluo nulla: guard, registro spot e
   whitelist sono attivi anche in dry (stesso codice, C9).

### Q6 - Micro-ordini e rischio massimo

DECISIONE:
- Rischio massimo di un ordine = sua riserva `R0 = required_quote`. Prova: spesa d'ingresso `<= R0` (fill a prezzo
  `<= limit`, fee `<= f_cov`); valore d'uscita `>= 0` (prezzo spot >= 0, fee di vendita < ricavo). Quindi
  `perdita <= R0` in qualunque gap, anche se lo stop non si esegue. Il vincolo 3 e' soddisfatto per costruzione dallo
  spot + guard; nessuna logica aggiuntiva.
- Min-size bump: resta, con condizioni
  `min_size * loss_per_unit <= equity * risk_frac` E `min_size <= size_cap` (il nuovo `size_cap` include gia' la
  copertura con `f_cov` e il cap assoluto). La condizione proposta `min_size*entry*(1+taker) <= available_quote` e'
  quindi gia' implicita e piu' debole di quella reale: non va aggiunta.
- Cap assoluto: SI', `risk.max_order_quote` (Decimal, default `"50.00"`, `> 0`), in config perche' sale con la rampa
  R0/R1/R2. E' un parametro di rischio applicato nel sizing (`notional_cap_base`), NON nel guard (il guard copre solo
  la copertura, hard-coded). Difende da stime di equity errate.

---

## PARTE B - File, funzioni, eccezioni, test

### B.1 File e firme

`modules/risk/costs.py` (aggiunte; scrivere per prime)
```python
COVERAGE_FEE_FLOOR_BPS: Final = Decimal(120)
def coverage_fee_rate(fees: FeeRates, fallback: FallbackFees) -> Decimal
def required_quote(limit_price: Decimal, base_size: Decimal, fee_rate: Decimal, quote_increment: Decimal) -> Decimal
def max_base_for_quote(quote: Decimal, limit_price: Decimal, fee_rate: Decimal,
                       quote_increment: Decimal, base_increment: Decimal) -> Decimal
```
`floor_to`/`ceil_to` importati da `modules/execution/rounding.py` (sez. 4.3 punto 3; modulo senza dipendenze).

`modules/execution/balance.py`
```python
BalanceSource = Literal["exchange", "virtual"]
class Reservation(BaseModel, frozen): client_order_id: str; product_id: str; side: Literal["BUY","SELL"]; currency: str; amount: Decimal
class AccountSnapshot(BaseModel, frozen): balances: dict[str, tuple[Decimal, Decimal]]  # currency -> (available, hold)
                                          foreign_order_currencies: frozenset[str]; taken_at: datetime
class SnapshotOutcome(StrEnum): ACCEPTED, DISCARDED_EPOCH
class BalanceBook:
    def __init__(self, book: str, source: BalanceSource, clock: Clock, initial: Mapping[str, Decimal] | None = None)
    epoch: int ; snapshot_at: datetime | None
    def free(self, currency: str) -> Decimal
    def reserved(self, currency: str) -> Decimal
    def total(self, currency: str) -> Decimal
    def reserved_sell_base(self, product_id: str) -> Decimal
    def snapshot_age_s(self) -> float | None
    def _reserve(self, r: Reservation) -> None                      # solo BalanceGuard; ValueError se free < amount
    def apply_fill(self, client_order_id: str, *, base_currency: str, quote_currency: str,
                   price: Decimal, size: Decimal, fee: Decimal) -> list[CoverageAlarm]
    def release(self, client_order_id: str) -> Decimal              # idempotente
    def restore_reservation(self, r: Reservation) -> None           # solo Reconciler all'avvio (ordini aperti importati)
    def apply_snapshot(self, snap: AccountSnapshot, epoch_at_request: int,
                       increments: Mapping[str, Decimal]) -> tuple[SnapshotOutcome, list[CoverageAlarm]]
```
Tutti i metodi sono sincroni. Nessun `await` dentro `BalanceBook`.

`modules/execution/balance_guard.py`
```python
BALANCE_MAX_AGE_S: Final = 120
class OrderSpec(BaseModel, frozen): client_order_id: str; book: str; product_id: str; side: Literal["BUY","SELL"]
    kind: Literal["limit","bracket","market"]; base_size: Decimal; limit_price: Decimal | None
    stop_trigger_price: Decimal | None; post_only: bool
class CoverageTicket: spec: OrderSpec; reserved: Decimal; currency: str   # costruttore protetto da sentinella privata del modulo
                      def consume(self) -> OrderSpec                       # uso singolo; secondo uso -> SpotViolation
class CoverageRefused(Exception): reason: SkipReason
class InsufficientCoverage(CoverageRefused): currency; required: Decimal; available: Decimal   # reason="insufficient_balance"
class BalanceStale(CoverageRefused): age_s: float | None                                     # reason="balance_stale"
class CoverageBreached(CoverageRefused)                                                      # reason="kill_switch"
class BalanceGuard:
    def __init__(self, balances: BalanceBook, positions: PositionCache, products: SpotRegistry,
                 fees: FeeProvider, fallback: FallbackFees, kill_switch: KillSwitch)
    def authorize(self, spec: OrderSpec) -> CoverageTicket       # sincrono; ordine dei controlli sotto
```
Ordine dei controlli in `authorize`: (1) prodotto nel `SpotRegistry` e combinazione side/kind nella matrice Q5.3,
altrimenti `SpotViolation`; (2) `base_size > 0`, multiplo di `base_increment`, prezzi multipli di `quote_increment`,
altrimenti `SpotViolation`; (3) BUY: `coverage_breach` attivo -> `CoverageBreached`; staleness -> `BalanceStale`;
`required_quote` vs `free(quote)` -> `InsufficientCoverage`; (4) SELL: `sellable` vs `base_size` ->
`InsufficientCoverage`; (5) `_reserve`, emissione ticket.

`modules/execution/spot.py`
```python
class SpotViolation(Exception)                     # fatale: exit code 4
SPOT_EXIT_CODE: Final = 4
class SpotRegistry:  async def load(exchange, products, allowed_venues) -> SpotRegistry ; def meta(product_id) -> ProductMeta
def build_order_body(spec: OrderSpec) -> dict[str, Any]      # unica costruzione del body, matrice Q5.3, stringhe Decimal
def assert_live_key_permissions(perms: KeyPermissions, allowed_portfolio_types: frozenset[str]) -> None
```

`modules/execution/exchange.py` (modifiche): `create_order(ticket: CoverageTicket) -> CreateOrderResult` (chiama
`ticket.consume()` e `build_order_body`); `get_accounts() -> AccountSnapshot`; `get_api_key_permissions()`.
Nessun altro metodo accetta un body d'ordine.

`modules/execution/balance_feed.py`: `LiveBalanceFeed(exchange, book, ledger, interval_s).run()` - task del
`TaskGroup`, regola `epoch` di Q3.

`modules/ledger/positions.py` (modifiche): colonne `dust_qty`, `dust_cost`; `PositionCache` in memoria
(sincrona, `view(product_id)`), aggiornata nella stessa transazione delle mutazioni.

`modules/dryrun/virtual_account.py`: `VirtualAccount(book, starting_quote, clock)` compone
`BalanceBook(book, "virtual", clock, {quote: starting_quote})`; `equity(mark_prices)`, `fees_paid`, `realized_pnl`;
nessun `reserve()/release()` proprio (delegati al `BalanceBook`, chiamati da guard e shadow gateway).

`modules/config.py` (aggiunte): `RiskConfig.max_order_quote: Decimal = Decimal("50.00")` (`gt=0`);
`ExecutionConfig.balance_refresh_s: float = 30` (`gt=0`, validator `<= 40`), `entry_poll_s: float = 5`,
`cancel_confirm_timeout_s: float = 10`. Nessuna chiave che disattivi guard, whitelist o registro spot.

`SkipReason` (enum): aggiungere `insufficient_balance`, `balance_stale`. Il report giornaliero (sez. 5.2) le elenca
in `skip_reasons`.

### B.2 Test obbligatori

Property-based (`hypothesis`), `tests/test_coverage_properties.py`:
- T1 `test_no_order_with_required_above_available`: sequenze casuali di authorize BUY/SELL, fill parziali (prezzo
  `<= limit`, fee `<= f_cov * p*q`), cancel, snapshot coerenti; ogni ticket emesso soddisfa
  `required <= free_prima`; ogni rifiuto ha `required > free` (o motivo staleness/breach).
- T2 `test_reservations_and_free_never_negative`: dopo ogni operazione `free[c] >= 0`, ogni `amount >= 0`.
- T3 `test_conservation`: reserve/release/terminale lasciano `free + reserved` invariato per valuta; i fill lo
  cambiano esattamente dei delta di Q3.
- T4 `test_sizing_never_refused_by_guard`: per stati casuali, `compute_size` produce una size che `authorize`
  accetta sullo stesso `BalanceBook`.
- T5 `test_max_base_for_quote_inverse`: `required_quote(max_base_for_quote(q)) <= q`, risultato multiplo di
  `base_increment`, `>= 0`.
- T6 `test_max_loss_bounded_by_reservation`: per fill `<= limit`, fee `<= f_cov`, qualunque prezzo d'uscita `>= 0`:
  `perdita netta <= R0`.
- T7 `test_snapshot_is_conservative`: con insiemi casuali di ordini in volo fra lettura ed eventi, `free` dopo
  `apply_snapshot` non supera mai il free "vero" del modello di riferimento del test.

Unitari:
- T8 staleness: BUY rifiutato con snapshot > 120 s o assente; SELL accettata con snapshot vecchio; sorgente
  "virtual" mai stale; scarto per `epoch` cambiato.
- T9 `build_order_body`: ogni chiave fuori whitelist, BUY market, `quote_size`, `attached_order_configuration`
  -> `SpotViolation`; snapshot JSON dei 4 casi ammessi.
- T10 registro spot: `product_type != "SPOT"`, `future_product_details` presente, venue fuori allowlist,
  `trading_disabled` -> exit code 4.
- T11 introspezione: metodi pubblici di `CoinbaseExchange` = allowlist; scansione AST di `modules/` che fallisce su
  attributi del client fuori dalla lista Q5.4 e su nomi contenenti `futures`, `perp`, `intx`, `cfm`, `portfolio_margin`,
  `move_portfolio_funds`.
- T12 ticket: `create_order` senza ticket o con ticket gia' consumato o costruito fuori dal guard -> `SpotViolation`.
- T13 flusso ingresso: fill parziale + timeout -> cancel confermata PRIMA del bracket; `bracket.base_size ==
  floor_to(filled_total)`; nessun SELL inviato mentre l'ingresso non e' terminale.
- T14 dust: fill sotto `min_sell_size` -> FLAT con `dust_qty`; ingresso successivo fonde la dust nel bracket.
- T15 uscita: nessuna SELL prima della conferma di cancellazione del bracket; bracket FILLED durante la cancel ->
  nessuna vendita; watchdog su posizione senza bracket scatta allo SL.
- T16 ordini estranei: `H_credit = 0`; `hold > reserved` senza ordini estranei -> `coverage_breach`.
- T17 parita' live/dry: stessa sequenza di intent e fill su `BalanceBook("exchange")` con snapshot coerenti e su
  `VirtualAccount` -> stessa sequenza di accettazioni e rifiuti.
- T18 config: nessun campo di `AppConfig` disattiva guard o whitelist; i costruttori dei gateway richiedono
  `BalanceGuard` (argomento non `Optional`).

### B.3 Ordine di implementazione
costs.py (aggiunte + T5, T6) -> rounding.py -> balance.py (T2, T3, T7) -> spot.py (T9, T10) -> balance_guard.py
(T1, T8, T12, T16) -> sizing.py (T4) -> risk_manager.py -> virtual_account.py (T17) -> exchange.py (T11) ->
gateway.py / shadow_gateway.py (T13, T14, T15) -> balance_feed.py -> reconciler.py -> config (T18).

---

## PARTE C - Testo da inserire nella spec (`docs/specs/chronos-system1-spec.md`)

### C.0 Intestazione
Sostituire `Versione: 2.3-draft` con `Versione: 2.4-draft`. Inserire dopo il blocco "Changelog 2.3":
```
Changelog 2.4 (vincoli finanziari assoluti):
- nuovo vincolo C11 (solo spot, copertura totale, nessun debito) e nuova sez. 4.1.6 "Copertura spot" (`BalanceBook`, `BalanceGuard`, `CoverageTicket`);
- bracket sempre standalone dopo lo stato terminale dell'ingresso; `attached_order_configuration` non piu' usato;
- `size_cap` (4.1.2) calcolato con la formula di copertura invece del cuscinetto 0.98; nuovo cap `risk.max_order_quote`;
- arrotondamento del limite d'ingresso prima di SL/TP e del sizing (4.1.3);
- riconciliazione dei saldi con regola `epoch` e staleness fail-closed per i BUY (4.1.5);
- `VirtualAccount` compone lo stesso `BalanceBook` del live (5.3).
```

### C.1 Sez. 0 - nuova riga dopo C10 nella tabella dei vincoli
```
| C11 | **Solo spot, copertura totale, nessun debito** (vincoli finanziari assoluti di CLAUDE.md). Vietati futures, margin, opzioni, leva. Ogni ordine e' coperto al 100% dal saldo reale; nessuna logica puo' rendere negativo un saldo. | Registro spot all'avvio (`product_type == "SPOT"`), whitelist del body d'ordine, nessun endpoint futures/perpetuals nel wrapper (`SpotViolation`, exit code 4). Ogni ordine passa da `BalanceGuard.authorize()` (sez. 4.1.6), che verifica in memoria `saldo disponibile >= importo + fee` e prenota l'importo; senza `CoverageTicket` nessun ordine puo' essere inviato, in live come in dry. Guard non disattivabile da config. Rischio massimo di un ordine = sua riserva. |
```

### C.2 Sez. 4.1 - diagramma
Nel blocco del diagramma sostituire le righe da `3. A = ATR_14...` fino alla riga `-> Watchdog: ...` con:
```
        3. A = ATR_14_<atr_tf>(t) dal label_config; entry = floor_to(best_bid, quote_increment) (maker)
                                                       | ceil_to(best_ask, quote_increment) (taker)
        4. SL = entry - k_sl * A ; TP = entry + k_tp * A                  (k da label_config; arrotondati come 4.1.3)
        5. costs = CostModel(entry_mode, fees, spread)                    -> cost_tp, cost_sl (sez. 0.3)
        6. fee floor: tp_bps - cost_tp >= min_net_edge_bps                altrimenti SKIP(fee_floor)
        7. EV gate:   EV(p_edge) > ev_min_bps                              altrimenti SKIP(ev_negative)
        8. sizing:    vedi 4.1.2 (cap di copertura, sola lettura del BalanceBook)  eventuale SKIP(below_min_size)
        9. KillSwitch.check(book)                                          SKIP(kill_switch) se attivo
   -> OrderIntent (Decimal, gia' arrotondati agli increment)
   -> ExecutionGateway[book].submit_entry_with_bracket()
        BalanceGuard.authorize(BUY limit)                  SKIP(insufficient_balance | balance_stale | kill_switch)
        maker: limit_limit_gtc post_only=true al best_bid ; taker: limit_limit_gtc al best_ask (marketable)
        attesa dello stato terminale: FILLED, oppure timeout (maker 30 min, taker 20 s) -> cancel residuo -> conferma
        filled > 0: LONG dal primo fill; al terminale BalanceGuard.authorize(SELL bracket, base_size = filled posseduto)
                    -> trigger_bracket_gtc standalone (sez. 4.1.6)
   -> fills -> PositionLedger[book]
   -> Watchdog: posizione con bracket: prezzo oltre lo SL di 1 x ATR_1h -> uscita d'emergenza
                posizione senza bracket (finestra d'ingresso): prezzo <= SL -> uscita d'emergenza
```
Sostituire il paragrafo "Decision(SELL) su posizione LONG: ..." con:
```
Decision(SELL) su posizione LONG: cancella il bracket attivo e attende la conferma terminale (nessun rilascio ottimistico della base), poi chiude con limit marketable `sell` a `best_bid` con timeout e fallback a `market_market_ioc`. Ogni ordine della sequenza passa dal `BalanceGuard` (sez. 4.1.6).
```

### C.3 Sez. 4.1.2 - modifiche al blocco di formule
Sostituire la riga `equity = ...` con:
```
equity          = BalanceBook.total(quote) + sum(mark_to_market posizioni del book)   # in memoria, live e dry (sez. 4.1.6)
```
Sostituire la riga `size_cap = ...` con:
```
f_cov           = coverage_fee_rate(fees, fallback_fees)                      # sez. 4.1.6, non e' un costo
notional_cap    = min(equity * max_position_frac, max_order_quote) / entry
cover_cap       = max_base_for_quote(BalanceBook.free(quote), entry, f_cov, quote_increment, base_increment)
size_cap        = floor_to(min(notional_cap, cover_cap), base_increment)
```
Aggiungere sotto il blocco, prima di "Esempio":
```
`entry` e' il prezzo limite gia' arrotondato (sez. 4.1.3). Il sizing usa le formule di copertura della sez. 4.1.6 solo per non produrre ordini che il guard rifiuterebbe; l'autorita' finale resta `BalanceGuard.authorize()`. `max_order_quote` (config `risk`, default 50 EUR) e' un cap assoluto per ordine che sale con la rampa (sez. 5.1.7).
```
Nella config YAML della sez. 4.2, sotto `risk:` aggiungere `max_order_quote: "50.00"`; sotto `execution:` aggiungere
`balance_refresh_s: 30`, `entry_poll_s: 5`, `cancel_confirm_timeout_s: 10`.

### C.4 Sez. 4.1.3 - riga introduttiva sopra la tabella
Inserire prima della tabella:
```
Ordine: il prezzo limite d'ingresso si arrotonda per primo; SL, TP, costi, sizing e copertura si calcolano sul prezzo arrotondato.
```

### C.5 Sez. 4.1.4 - riga Watchdog SL
Sostituire la cella "Condizione" con:
```
Posizione con bracket: `last_price <= SL - 1 x ATR_1h` e posizione ancora aperta dopo 10 s. Posizione senza bracket (finestra d'ingresso o bracket rifiutato): `last_price <= SL`.
```
Aggiungere una riga alla tabella:
```
| Coverage breach | Book | `reservation_overdraw`, `hold_exceeds_reservation`, oppure >= 3 `balance_drift` in 24 h (sez. 4.1.6) | Blocca i BUY del book fino a reset manuale; le uscite restano attive. |
```

### C.6 Sez. 4.1.5 - sostituire i tre punti con
```
- Avvio: legge account (`get_accounts`: `available_balance`, `hold`), ordini aperti (`list_orders` con `order_status=OPEN`) e fill recenti; ricostruisce il ledger e il `BalanceBook`: per ogni ordine Chronos aperto ripristina la riserva (`BUY`: `required_quote(limit, leaves_size)`; `SELL`: `leaves_size`), poi applica lo snapshot. Un ordine con `client_order_id` di Chronos senza record locale viene importato; una posizione senza bracket attivo riceve un bracket standalone `trigger_bracket_gtc` sell con `base_size = floor_to(min(base_qty, sellable))`, oppure diventa dust se sotto il minimo vendibile.
- Runtime ordini: canale WS `user` per gli aggiornamenti degli ordini e dei fill (aggiornano subito `BalanceBook`); `get_order` ogni `entry_poll_s` per l'ingresso non terminale; polling REST di sicurezza ogni `reconcile_interval_s`.
- Runtime saldi: `LiveBalanceFeed` ogni `balance_refresh_s` (default 30 s) legge saldi e ordini aperti; lo snapshot viene scartato se il `BalanceBook` ha cambiato `epoch` durante la lettura; se accettato, `free = max(available - (reserved - H_credit), 0)` per valuta, con `H_credit = min(hold, reserved)` senza ordini estranei sulla valuta e `0` altrimenti. Drift fra totale locale e totale dell'exchange oltre `max(2 x increment, 1e-4 x totale)`: allarme `balance_drift`, il valore dell'exchange prevale. Il canale WS `user` non porta saldi spot.
- Idempotenza: `client_order_id = uuid5(NAMESPACE_CHRONOS, f"{decision_id}:{leg}")`. Un retry di rete non crea mai un secondo ordine; un errore ambiguo lascia la riserva in stato UNKNOWN finche' la riconciliazione non trova o esclude l'ordine (rilascio dopo 2 cicli senza riscontro).
```

### C.7 Nuova sez. 4.1.6 - inserire dopo 4.1.5, prima di "### 4.2"
```
#### 4.1.6 Copertura spot (vincolo C11)

Componenti (`modules/execution/`): `BalanceBook` (saldi in memoria per book), `BalanceGuard` (unica autorita' di copertura), `CoverageTicket` (capacita' d'invio), `SpotRegistry` e `build_order_body` (divieto di leva). Formule in `modules/risk/costs.py` (sez. 0.3).

Formule (Decimal):
    f_cov          = max(f_t corrente, f_t fallback, 120 bps)            # tetto di prenotazione, non un costo
    required_quote = ceil_to(limit_price * base_size * (1 + f_cov), quote_increment) + quote_increment     # BUY
    required_base  = base_size                                                                           # SELL
    sellable       = min(position.base_qty - reserved_sell_base(book, product), free(base))
    BUY ammesso  se  free(quote) >= required_quote ; SELL ammessa se sellable >= base_size

Regole:
1. Ogni ordine, live o shadow, e' inviabile solo con un `CoverageTicket` emesso da `BalanceGuard.authorize()`, che verifica e prenota nello stesso passo sincrono (nessuna chiamata di rete, nessun `await`). Il body JSON e' costruito dal ticket con `build_order_body`.
2. BUY: solo `limit_limit_gtc`. Rifiutato se il saldo e' insufficiente (`insufficient_balance`), se l'ultimo snapshot dei saldi ha piu' di 120 s (`balance_stale`, solo live) o se `coverage_breach` e' attivo. SELL: `limit_limit_gtc`, `trigger_bracket_gtc`, `market_market_ioc`, solo su base posseduta da Chronos; mai bloccata dall'eta' dello snapshot.
3. Riserve: al fill BUY si consuma `p*q + fee`, al terminale si rilascia il residuo; al fill SELL si consuma `q`. Nessun rilascio ottimistico: una cancellazione libera la riserva solo alla conferma terminale dell'exchange.
4. Bracket: sempre standalone, piazzato dopo lo stato terminale dell'ingresso con `base_size = floor_to(min(filled, sellable), base_increment)`. Sotto il minimo vendibile (`base_min_size`, `quote_min_size`) la quantita' resta come dust e si fonde nel bracket dell'ingresso successivo sullo stesso prodotto.
5. Rischio massimo di un ordine = sua riserva: la spesa d'ingresso non supera `required_quote` e il valore d'uscita non e' mai negativo.
6. Non disattivabile: nessuna chiave di config; stesso codice in live, dry, backtest (`VirtualAccount` compone un `BalanceBook`). Violazioni di whitelist o prodotto non spot: `SpotViolation`, exit code 4.
7. Verifiche continue delle ipotesi sull'exchange: `hold > reserved` senza ordini estranei, riserva consumata oltre il prenotato, drift ripetuti -> `coverage_breach` (sez. 4.1.4).
```

### C.8 Sez. 4.2
- Sostituire il blocco "Payload Coinbase - ingresso con TP/SL allegato" con lo stesso JSON SENZA
  `attached_order_configuration`, titolo "Payload Coinbase - ingresso".
- Sostituire il paragrafo "Note dalla documentazione Coinbase: nell'ordine allegato..." con:
```
L'attached bracket (`attached_order_configuration`) non e' usato in v2.4: la sua semantica su fill parziali non e' verificata e un bracket piu' grande della quantita' fillata potrebbe vendere base non di Chronos (C11). Si potra' reintrodurre solo con una nuova versione della spec, dopo verifica documentata del comportamento su fill parziale.
```
- Titolo del payload standalone: "Payload Coinbase - bracket standalone (percorso unico e riconciliazione)".
- DDL `positions`: aggiungere `dust_qty TEXT NOT NULL DEFAULT '0', dust_cost TEXT NOT NULL DEFAULT '0',`.

### C.9 Sez. 4.3 (prompt)
- Punto 2: sostituire `create_order(body: dict) -> CreateOrderResult` con `create_order(ticket: CoverageTicket) -> CreateOrderResult`; eliminare la frase sul test di introspezione per `attached_order_configuration` e sul fallback `client.post`; aggiungere `get_api_key_permissions()` e "metodi del `RESTClient` limitati all'allowlist della sez. 4.1.6".
- Punto 7, sostituire il primo e il secondo trattino con:
```
>    - `submit_entry_with_bracket(intent)`: `BalanceGuard.authorize` (BUY), invio, registrazione in `orders`; attesa dello stato terminale (WS `user` + `get_order` ogni `entry_poll_s`); timeout `entry_timeout_s` -> cancel del residuo con conferma; al terminale, se `filled > 0`, `BalanceGuard.authorize` (SELL bracket) e bracket standalone `trigger_bracket_gtc` con `base_size` = quantita' fillata posseduta, oppure dust (sez. 4.1.6);
```
- Punto 7, terzo trattino: dopo "attende la conferma della cancellazione (o lo stato terminale)" aggiungere
  "; se il bracket risulta FILLED non vende; ogni ordine della sequenza passa da `BalanceGuard`".
- Aggiungere punto 14: "`modules/execution/balance.py`, `balance_guard.py`, `spot.py`, `balance_feed.py` secondo la sez. 4.1.6, con i test property-based: nessun ordine con `required > available`, riserve e saldi mai negativi, conservazione di `free + reserved`."
- Test: eliminare il secondo caso del test `@pytest.mark.preview` (market + attached).

### C.10 Sez. 5.1.2 - riga `fill` del ciclo di vita
Sostituire con:
```
fill    VirtualPosition(LONG) dal primo fill; VirtualBracket(TP, SL) creato allo stato terminale dell'ingresso con
        size = quantita' fillata (stessa regola del live, sez. 4.1.6); prima del bracket vale lo stop virtuale allo SL
```

### C.11 Sez. 5.3 punto 5 - sostituire con
```
> 5. `modules/dryrun/virtual_account.py` — `VirtualAccount(book, starting_quote, clock)`: compone un `BalanceBook(book, source="virtual")` (sez. 4.1.6), stessa classe del live; riserve, fill e rilasci passano dal `BalanceGuard` e dal `BalanceBook`, nessuna logica di riserva propria. Espone `equity(mark_prices)`, `fees_paid`, `realized_pnl`. Invarianti testati con `hypothesis`: `quote + sum(base_i * price_i) + fees_pagate_cumulative == starting_quote + pnl_realizzato + pnl_non_realizzato` (tolleranza 1e-12); riserve e saldi mai negativi; `free + reserved` invariato da reserve/release.
```
Sez. 5.3 punto 6, primo trattino: sostituire "prenota il capitale" con "ottiene un `CoverageTicket` da
`BalanceGuard.authorize` (stessa prenotazione del live)". Sez. 5.3 punto 8: aggiungere "`buy_and_hold` salta il
`RiskManager` ma non il `BalanceGuard`: size = `max_base_for_quote(free(quote), ...)`".

### C.12 Sez. 5.2 - report
In `skip_reasons` aggiungere `"insufficient_balance": 0, "balance_stale": 0`.

---

## PARTE D - Ipotesi su Coinbase non verificabili dalla documentazione disponibile

| ID | Ipotesi | Verificabile ora? | Regola prudente (valida in ogni caso) | Controllo in esercizio |
|----|---------|-------------------|---------------------------------------|------------------------|
| H1 | Il `hold` di un limit BUY e' il nozionale oppure nozionale + fee | NO | Riserva `required_quote` con `f_cov >= ogni fee` + 1 increment: copre entrambi i casi | `hold > reserved` senza ordini estranei -> `coverage_breach` |
| H2 | L'attached bracket su fill parziale ha size = fillato, size intera, o si attiva solo al fill completo | NO | Attached non usato; bracket standalone dopo il terminale con size = posseduto | n/a |
| H3 | Le fee (BUY e SELL) sono addebitate in quote | Parziale (dato comune, non verificato nella doc in repo) | `sellable` usa `min(ledger, free(base))`: se la fee fosse in base la vendita si riduce, mai oltre il posseduto | delta di base al fill != `size` -> `balance_drift` (3 in 24 h -> `coverage_breach`) |
| H4 | `available_balance` di `get_accounts` esclude gli `hold` | Parziale (campi separati `available_balance`/`hold`) | Formula `free = A - (reserved - H_credit)` e' prudente anche se fosse falso (sottostima) | drift |
| H5 | Valori di `product_venue` e `portfolio_type` per spot | NO | Allowlist con i valori registrati dal probe; ogni altro valore rifiuta l'avvio | probe con key View prima di P4 |
| H6 | Il canale WS `user` non porta saldi spot | Si' per quanto noto | Saldi da eventi locali + snapshot REST | n/a |

Azione aggiuntiva per lo Sviluppatore: estendere `script/probe_coinbase.py` (in sola lettura) per registrare in
`data/probe/coinbase_probe.json` i campi `product_type`, `product_venue`, `future_product_details` dei prodotti e
`portfolio_type`, `can_transfer` della key. Sono gli input delle allowlist H5.

---

Fine della risoluzione. Dopo l'implementazione: applicare i testi della PARTE C alla spec, poi cancellare questo file
(protocollo CLAUDE.md, punto 5).
