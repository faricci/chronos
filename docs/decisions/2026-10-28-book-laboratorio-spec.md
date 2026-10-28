# SPECIFICA SOFTWARE: book di laboratorio nel dry run (A-trend, B-rotazione, C-revert, C0-controllo, D-squeeze, HODL)

Autore: architetto. Data: 2026-10-28. Stato: proposta di implementazione. Fonte delle regole: `docs/decisions/2026-10-28-ipotesi-strategie-laboratorio.md` (quant, BOZZA). Il codice si puo' scrivere e testare subito; i book si ABILITANO in `config/config.yml` solo dopo l'approvazione dell'utente dei punti 1 e 3 della sez. 9 del documento del quant. Nessuna modifica a `BalanceGuard`, `ShadowExecutionGateway`, `RiskManager`, `KillSwitch`, `exchange.py`, rifiuto del live.

## 0. Decisioni in sintesi

1. Sei book SOLO simulati nel processo del dry run esistente, uno per ipotesi, ciascuno con il proprio `VirtualAccount`, `BalanceGuard`, `ShadowExecutionGateway` (costruiti da `books._infrastructure`, l'unico costruttore di gateway dei book). Nessun nuovo processo, nessun nuovo feed, nessun ordine reale.
2. HODL non e' codice nuovo: e' un secondo book con `decider: buy_and_hold` (`BuyAndHoldDriver` esistente), nome `lab_hodl`, avviato il giorno di avvio del laboratorio.
3. A, B, D: **driver a stato obiettivo** (`TargetDriver`). Le regole sono funzioni pure che, dalle barre chiuse, danno la posizione obiettivo per moneta; il driver riconcilia ad ogni barra 1m posizione reale e obiettivo. Robusto a riavvii, barre mancate e ingressi bloccati per costruzione.
4. C, C0: **driver a eventi** (`EventDriver`): l'esito dipende dai fill (limite maker, bracket, uscita a tempo dal fill), quindi lo stato e' quello del gateway e del ledger.
5. Una sola definizione delle regole: `modules/lab/rules.py`. Il driver live e lo script di backtest del quant importano le STESSE funzioni (come `fill_model.py` per i fill). La parita' segnale-backtest e' per costruzione; i test verificano l'impianto incrementale.
6. Una sola fonte di barre: le barre 1m `BarClosed` gia' distribuite dal `ShadowBookManager` e lo storico 1m gia' letto da `app.warm()` per il warm-up. Aggregazione con `modules/market/timeframes.py` (stesso codice di indicatori e training).
7. Parametri congelati nel codice (`modules/lab/params.py`); la config sceglie il tipo, non puo' cambiarli (validatore); l'hash dei parametri (`rules_version`) e' fissato nel ledger al primo avvio e un cambio rifiuta l'avvio.
8. Gli ingressi dei book di laboratorio rispettano lo shutdown e le guardie di SISTEMA (dati/barre stale, salute, task falliti) ma non le guardie di CONTO (daily loss, loss streak, esposizione 50%), come oggi `buy_and_hold` (sez. 5.3 punto 8 della spec: benchmark fuori dal RiskManager, mai fuori dal BalanceGuard). Motivo: l'esposizione massima 0,50 bloccherebbe la terza sleeve di A e D e falserebbe l'ipotesi. Da confermare dal quant (domanda Q1).
9. Il report giornaliero aggiunge un blocco `lab` (scarto di implementazione, confronto con HODL) e `gate_econ_by_book`; le statistiche primarie dell'ipotesi e il confronto C - C0 si calcolano SOLO con il comando di sguardo unico `python -m modules.lab.forward --look`.

## 1. Perimetro e invarianti

- Solo spot, solo long o cassa, nessuna leva, nessuno short: ogni ordine nasce da `OrderIntent`/`ExitIntent` verso lo shadow gateway, che passa da `BalanceGuard.authorize`. SELL solo di base posseduta (regola esistente del gateway).
- Copertura 100% con la cache saldo simulata: il `BalanceBook(source="virtual")` del `VirtualAccount` e' la cache in memoria; nessuna chiamata di rete per il saldo.
- Nessun oggetto in `modules/lab/` costruisce o importa un exchange o un gateway: riceve dal builder il gateway gia' costruito (avvolto da `_ObservedGateway` dell'app).
- I book di laboratorio non scrivono `DecisionRecord` in `data/decisions/` (il `LabelResolver`, la parita' delle decisioni e G-DRY-OPS restano del champion); scrivono un proprio journal (sez. 3.8).
- Vietati in `modules/` gli identificatori di `FORBIDDEN_TOKENS` di `tests/test_c11_wiring.py` (`margin`, `leverage`, `futures`, `perp`, ...) e la parola `margine` anche nei commenti: usare `headroom`, `buffer`.

## 2. Configurazione

Nessuna sezione top-level nuova: `dry_run.books` e' gia' "la sezione dei book" letta da `_plan_books`, `build_books`, restore, kill switch e report. Una seconda lista duplicherebbe tutto il wiring. Cambia la grammatica di `decider` e si aggiunge `params`.

```yaml
dry_run:
  books:
    - {name: champion, decider: registry:champion, starting_quote: "100.00"}      # invariati
    - {name: challenger, decider: registry:challenger, starting_quote: "100.00"}
    - {name: random_entry, decider: random, match_frequency_of: champion, seed: 42, starting_quote: "100.00"}
    - {name: buy_and_hold, decider: buy_and_hold, starting_quote: "100.00"}
    # laboratorio (abilitare solo dopo l'approvazione dell'utente)
    - {name: lab_hodl, decider: buy_and_hold, starting_quote: "100.00"}
    - {name: lab_a_trend, decider: "lab:a_trend", starting_quote: "100.00"}
    - {name: lab_b_rotation, decider: "lab:b_rotation", starting_quote: "100.00"}
    - {name: lab_c_revert, decider: "lab:c_revert", starting_quote: "100.00"}
    - {name: lab_c0_control, decider: "lab:c0_control", starting_quote: "100.00"}
    - {name: lab_d_squeeze, decider: "lab:d_squeeze", starting_quote: "100.00"}
```

`BookConfig` (modifica additiva):
- `params: dict[str, Any] | None = None`;
- validatore: `decider` `lab:<tipo>` con `<tipo>` in `LAB_TYPES`; `params` assente oppure UGUALE a `FROZEN[tipo].as_config()` (altrimenti errore "parametri pre-registrati, non modificabili: un'altra variante e' un altro book con un altro nome"); `params` vietato per gli altri decider; `forced_*` resta solo di `random`.
- `AppConfig` validatore: l'universo congelato di ogni book `lab:` e' contenuto in `market.products` e i prodotti hanno quote EUR.
- `_default_books()` NON cambia; `config/config.example.yml` riporta i sei book commentati.
- `starting_quote`: 100.00 come gli altri book (confrontabilita'; i rendimenti sono in percentuale). Domanda Q8.

`modules/lab/params.py` (dataclass frozen, nessun import da `modules.config`, per evitare cicli; `config.py` lo importa come gia' fa con `modules.probe.limits`):

```python
UNIVERSE: Final = ("BTC-EUR", "ETH-EUR", "SOL-EUR")
LabType = Literal["a_trend", "b_rotation", "c_revert", "c0_control", "d_squeeze"]

@dataclass(frozen=True)
class TrendParams:      # A
    universe: tuple[str, ...] = UNIVERSE
    entry_lookback_d: int = 50          # C_t > max(C_(t-50) .. C_(t-1))
    exit_lookback_d: int = 20           # C_t < min(C_(t-20) .. C_(t-1))
    maker_window_s: int = 3600          # post-only al best bid per 60 minuti, poi taker

@dataclass(frozen=True)
class RotationParams:   # B
    universe: tuple[str, ...] = UNIVERSE
    lookback_d: int = 28                # R28 = ln(C_t / C_(t-28)), t = ultimo giorno del mese
    hysteresis: float = 0.05

@dataclass(frozen=True)
class RevertParams:     # C e C0
    universe: tuple[str, ...] = UNIVERSE
    sigma_window_h: int = 720           # sd ddof 1 dei rendimenti log 1h
    drop_h: int = 4                     # ln(C_t / C_(t-4h)) <= -z_entry * sigma4h, sigma4h = 2 * sigma1h
    z_entry: float = 3.0
    limit_sigma1h: float = 1.0          # L = C_t * (1 - sigma1h)
    entry_valid_h: int = 4
    tp_sigma4h: float = 2.0             # TP = L * (1 + 2 sigma4h)
    sl_sigma4h: float = 3.0             # SL = L * (1 - 3 sigma4h)
    time_exit_h: int = 24
    dedup_h: int = 24
    fixed_hour_utc: int | None = None   # None per C; 12 per C0 (nessun evento)

@dataclass(frozen=True)
class SqueezeParams:    # D
    universe: tuple[str, ...] = UNIVERSE
    q_short: int = 30
    q_long: int = 180
    q_pct: float = 0.20                 # numpy.quantile, method="linear"
    q_window: tuple[date, date] | None = None   # finestra di training per q20: domanda Q4 (None = book non avviabile)
    breakout_lookback: int = 30         # C_t > max(H_(t-30) .. H_(t-1))
    exit_lookback: int = 15             # C_t < min(L_(t-15) .. L_(t-1))
    max_hold_bars: int = 60

FROZEN: Final[Mapping[LabType, object]]   # c0_control = RevertParams(fixed_hour_utc=12)
RULES_REVISION: Final = 1                 # cambia solo con una nuova pre-registrazione (= nuovi nomi di book)
def rules_version(t: LabType) -> str: ... # "lab:<tipo>@" + sha256(json canonico di params + RULES_REVISION)[:12]
```

## 3. Architettura

Nuovo package `modules/lab/`: `params.py`, `rules.py`, `history.py`, `drivers.py`, `state.py`, `journal.py`, `forward.py`.

### 3.1 Barre (fonte unica)

- Warm-up: `app.warm(product)` gia' legge l'INTERO storico 1m (`deps.history(root_1m, product)`). Se almeno un book `lab:` e' configurato, nello stesso thread costruisce `LabHistory.from_1m(history, last_ts)`: frame `aggregate_bars(history, tf)` per `tf` in `{"1h", "4h", "1d"}` e i `TimeframeAggregator` gia' innescati. Nessuna seconda lettura dello store, nessuna REST.
- Innesco: per ogni `tf` l'aggregatore riceve le righe 1m da `bucket_start(last_ts, tf)` a `last_ts` incluso (il bucket in corso); se `last_ts` e' l'ultimo minuto del bucket non riceve nulla. Senza innesco il primo bucket dopo l'avvio andrebbe perso (l'aggregatore scarta il bucket parziale): A perderebbe la chiusura giornaliera del giorno del riavvio.
- Live: ogni `BarClosed` del batch del manager entra in `LabHistory.update(product, bar)`; un bucket completato si appende al frame del suo `tf` e produce un `BucketClosed(product, tf, ts_open, ts_close)`.
- `BarGapError` (non dovrebbe accadere: il `BarFinalizer` pubblica barre contigue, piatte incluse): il prodotto passa a `degraded` per quel book (nessun nuovo ingresso, uscite gia' decise eseguite), evento `lab_bars_gap`, journal; si risolve con un riavvio controllato (warm-up dallo store).
- `modules/market/timeframes.py`: aggiungere `"4h": 14400` a `TIMEFRAME_SECONDS` e `"4h": "4h"` a `_PANDAS_FREQ` (bucket allineati all'epoch: 00, 04, ..., 20 UTC). Prima di farlo verificare con una ricerca che nessun consumatore iteri su tutte le chiavi di `TIMEFRAME_SECONDS`; se uno lo fa, NON toccare il dizionario e aggiungere invece in `timeframes.py` un parametro esplicito `seconds` al costruttore di `TimeframeAggregator` e una funzione `aggregate_bars_seconds`, lasciando invariate le API esistenti. In entrambi i casi i test di parita' degli indicatori restano verdi senza modifiche.

### 3.2 Regole pure (`modules/lab/rules.py`)

Funzioni senza stato, input = frame di barre COMPLETE con indice `ts` UTC di apertura, nessun accesso a dati oltre l'ultima riga. Convenzione: il valore all'indice `t` e' la decisione presa alla chiusura della barra `t`, eseguita dalla prima barra 1m successiva.

```python
def trend_targets(close_1d: pd.Series, p: TrendParams, start: pd.Timestamp) -> pd.Series      # bool: long dopo la chiusura di t
def rotation_targets(close_1d: pd.DataFrame, p: RotationParams, start: pd.Timestamp) -> pd.Series  # str | None (moneta o cassa), valori solo sugli ultimi giorni del mese
def revert_events(bars_1h: pd.DataFrame, p: RevertParams) -> pd.DataFrame   # event, z, sigma1h, sigma4h, limit, tp, sl (prezzi float non arrotondati)
def control_slots(bars_1h: pd.DataFrame, p: RevertParams) -> pd.DataFrame   # stesse colonne, una riga per giorno alle 12:00 UTC
def squeeze_q(bars_4h: pd.DataFrame, p: SqueezeParams) -> pd.Series         # Q(t)
def squeeze_threshold(bars_4h_training: pd.DataFrame, p: SqueezeParams) -> float
def squeeze_targets(bars_4h: pd.DataFrame, p: SqueezeParams, q20: float, start: pd.Timestamp) -> pd.Series  # bool
```

- Macchine a stati di A, B, D: partono in cassa a `start` (data di avvio del laboratorio, sez. 3.7) e si percorrono fino all'ultima barra. A e D per moneta (sleeve indipendenti), B per l'intero book (isteresi rispetto alla moneta OBIETTIVO detenuta, non a quella reale).
- B: decide solo alla chiusura dell'ultimo giorno del mese e solo quando TUTTE le monete hanno quella barra; fuori da quelle date l'obiettivo e' quello precedente. A `start` B resta in cassa fino alla prima fine mese.
- D, uscita a tempo: barra d'ingresso decisa a `t`; se nessuna uscita di canale, l'uscita e' decisa alla chiusura di `t + max_hold_bars` (posizione tenuta per le barre `t+1 .. t+60`) ed eseguita all'apertura successiva (conferma in Q5).
- C, deduplica: un evento e' rilevato se la condizione vale e non c'e' stato un evento RILEVATO della stessa moneta nelle 24 h precedenti, indipendentemente dal fatto che sia stato operato (conferma in Q3).
- Il driver chiama le stesse funzioni sul proprio frame a ogni `BucketClosed` del `tf` del tipo e legge l'ultimo valore (costo trascurabile: al massimo qualche migliaio di righe, una volta all'ora per C e C0, ogni 4 h per D, al giorno per A e B). Nessuna seconda implementazione incrementale.
- Lo script di backtest del quant (proposta: `script/lab_backtest.py`) importa queste funzioni e `modules.dryrun.fill_model` per i prezzi di fill; non ne definisce copie (test T10). `modules/lab/rules.py` va hashato nella sua pre-registrazione.

### 3.3 Interfaccia dei driver

Un driver implementa il protocollo `BookEngine` di `books.py` (`on_bars`, `drain`) e restituisce sempre `[]` (nessun `Processed`, nessun `DecisionRecord`).

```python
class LabDriver(BookEngine, Protocol):
    async def on_bars(self, events: Iterable[BarClosed]) -> list[Processed]: ...
    async def drain(self, timeout: float | None = None) -> None: ...
    @property
    def busy(self) -> frozenset[str]: ...            # prodotti con una chiamata al gateway in corso (replay)
    def stats(self) -> dict[str, int]: ...           # signals, submitted, filled, blocked, refused, missed, degraded

@dataclass(frozen=True)
class LabContext:          # cio' che il builder passa, nient'altro
    name: str
    type: LabType
    params: object
    rules_version: str
    account: VirtualAccount
    gateway: ExecutionGateway          # il wrapper osservato dell'app; mai costruito dal driver
    deps: BookDeps                     # ledger, clock, registry, fees, quotes, fallback_fees, entry_timeout, new_id
    gate: EntryGate                    # sez. 3.6
    history: Mapping[str, LabHistory]
    state: LabState                    # sez. 3.7
    journal: LabJournal                # sez. 3.8
```

Ordine per batch (garantito dal manager): prima il gateway riceve la barra (fill, bracket, uscite), poi `driver.on_bars`: (1) aggiorna `LabHistory`; (2) per i bucket chiusi ricalcola obiettivi o eventi e scrive il journal; (3) riconcilia; gli ordini partono come task (`asyncio.create_task`, come `BuyAndHoldDriver`), al massimo una chiamata in corso per (book, prodotto) (`busy`).

`TargetDriver.reconcile(product)` a ogni barra 1m del prodotto, se non `busy` e non `degraded`:
- obiettivo long, reale flat: ingresso (sez. 3.4) se `gate.entry_blocked(name)` e' `None`, altrimenti journal `blocked:<motivo>` e nuovo tentativo alla barra successiva;
- obiettivo flat (o altra moneta per B), reale long: `submit_exit` (mai bloccata);
- B: l'ingresso nella nuova moneta parte solo quando nessuna moneta e' detenuta (dopo l'`ExitResult` `closed`);
- un rifiuto di copertura o una taglia sotto il minimo si ritenta solo al `BucketClosed` successivo del `tf` (niente tentativi a ogni minuto); `no_fill`/`expired` taker si ritenta alla barra successiva.

`EventDriver` (C, C0) a ogni `BucketClosed("1h")`:
- book con posizione o ingresso pendente: evento ignorato (journal `ignored_busy`); la copertura al 100% lo impone comunque (la riserva dell'ingresso pendente occupa tutta la quota);
- eventi su piu' monete nella stessa ora: domanda Q3 (proposta: z piu' negativo, poi ordine dell'universo);
- C0: una "occasione" alle 12:00 UTC, stesse colonne di C; regole di universo e frequenza: domanda Q2 (C0 NON si implementa finche' non c'e' risposta);
- uscita a tempo: domanda Q5 (proposta: decisa alla chiusura della 24a barra 1h completa dopo la barra 1h del fill, eseguita all'apertura successiva); a ogni barra 1m il driver controlla `opened_at` della posizione nel ledger;
- protezione: posizione long senza bracket (`bracket_order_id` nullo dopo un riavvio fallito) e `low <= sl` del ledger: `submit_exit` motivo `SL_BACKUP`.

### 3.4 Intent coerenti con il gateway shadow e con le regole di fill della sez. 3

| Book | Ingresso (`OrderIntent.entry`) | Bracket | Uscita |
|---|---|---|---|
| A | fase 1 `limit_post_only`, `limit = best_bid` del `QuoteSource` all'invio, `time_in_force_s = 3600 - (now - maker_started_at)`; fase 2 solo se la fase 1 finisce SENZA alcun fill (`no_fill`, `expired`, `rejected` post_only_would_cross): `limit_marketable`, `limit = ceil(best_ask x 1.01)`, `time_in_force_s = entry_timeout.taker`, ritentata a ogni barra | irraggiungibile: `stop_loss = quote_increment`, `take_profit = ceil(limit x 1000)`, come `BuyAndHoldDriver` | `submit_exit`, motivo `SIGNAL` (market alla prima barra 1m utile) |
| B | `limit_marketable` come A fase 2 (domanda Q6) | irraggiungibile | `submit_exit`, motivo `ROTATION` |
| C | `limit_post_only`, `L = floor(C_t x (1 - sigma1h))`, `time_in_force_s = 14400` | `tp = ceil(L x (1 + 2 sigma4h))`, `sl = floor(L x (1 - 3 sigma4h))`; `k_tp = 2.0`, `k_sl = 3.0`, `atr_tf = "lab_sigma4h"`, `atr = sigma4h x L` (informativi) | TP/SL dal gateway; tempo: `submit_exit` motivo `TIME` |
| C0 | come C, alle 12:00 UTC | come C | come C |
| D | `limit_marketable` come A fase 2 | irraggiungibile | `submit_exit`, motivo `SIGNAL` o `TIME` |
| HODL | `BuyAndHoldDriver` esistente | irraggiungibile | nessuna |

- Arrotondamenti a `quote_increment`: `L` per difetto, `tp` per eccesso, `sl` per difetto (barriere lontane dall'ingresso: lato prudente sul PnL).
- `decision_id` = `deps.new_id(now)` per OGNI tentativo (il `client_order_id` deriva dal `decision_id`: maker e taker dello stesso segnale non devono collidere); il journal lega `signal_id` e `decision_id`.
- Blocco `risk` dell'intent: zeri come `BuyAndHoldDriver`, `equity_eur` = equity del book.
- Fill parziale di un ingresso maker (A fase 1, C): il driver chiama `gateway.cancel(product)` alla prima barra in cui la posizione e' long con ingresso ancora pendente; il residuo e' cancellato e il bracket si piazza sul preso. Chiude la finestra "posizione senza stop" del bar mode (docstring dello shadow) e non richiede modifiche al gateway. Il residuo resta in cassa (il gateway rifiuta un secondo ingresso con `position_open`), nessuna fase 2; journal `residual_unfilled` (Q11). Con taglie di ~30-100 EUR contro il tetto `queue_haircut x volume` del minuto il caso e' raro.
- Confronto con le regole di fill della sez. 3 del quant (il gateway e' sempre uguale o piu' prudente):

| Regola del quant | Gateway shadow | Esito |
|---|---|---|
| maker riempito solo se il minimo trapassa L di un tick | `low < limit` con L sulla griglia di `quote_increment` (equivalente), piu' latenza e tetto di volume | piu' prudente |
| TP solo se il massimo supera di un tick, dalla barra dopo il fill | `high > tp` dalla barra dopo il fill | uguale |
| stop a `min(stop, open) x (1 - 5 bps)` | `min(stop, open) x (1 - (s/2 + 5 + 10) bps)` | piu' prudente di s/2 + 10 bps (incoerenza I4) |
| tempo / taker a `open x (1 - 5 bps)` | `open x (1 - (s/2 + 5) bps)` | piu' prudente di s/2 |
| stop e target nella stessa barra: stop | idem | uguale |

### 3.5 Sizing (capitale simulato per book e per moneta)

- Taglia = `max_base_for_quote(budget, limit, coverage_fee_rate(fees.current, fallback_fees), quote_increment, base_increment)`, come `BuyAndHoldDriver`: il `BalanceGuard` non rifiuta mai cio' che il driver chiede. Sotto `max(base_min_size, quote_min_size / limit)`: nessun ordine, journal `below_min_size`.
- A, D: sleeve 1/N mai trasferite. `sleeve_cash(p) = starting_quote / N + somma(SELL: prezzo x size - fee) - somma(BUY: prezzo x size + fee)` sui fill del prodotto `p` nel book. Ricostruita al riavvio dai `BookRestore.fills` gia' caricati (nessuna lettura extra), aggiornata in memoria dagli `EntryResult`/`ExitResult`. `budget = min(sleeve_cash(p), free(EUR))`.
- B, C, C0: `budget = free(EUR)` (100% del book, una posizione alla volta).
- HODL: `starting_quote / N` per moneta (esistente).
- `risk.max_order_quote` (50 EUR) non si applica: e' un tetto del `RiskManager` (sez. 4.1.2), che i book di laboratorio non usano, come `buy_and_hold`. Se un'ipotesi arrivera' al reale passera' dal percorso `RiskManager` del live: fuori ambito qui.

### 3.6 Gate degli ingressi

`_LabGate` in `app.py` (implementa `EntryGate`): `"shutting_down"` se `life.stopping`; altrimenti `system.blocked_reason(clock())` (dati e barre stale, salute del decider, task falliti, drift); altrimenti `None`. La `coverage_breach` del book resta applicata dal `BalanceGuard` (KillSwitchView), senza eccezioni. Le uscite non passano mai dal gate. `gates.no_entry` (policy `untunable` del champion) non riguarda i book di laboratorio (`ShadowBook.model` e' `None`). `status.entries_blocked[<lab>]` mostra la ragione di `_LabGate`, non quella del kill switch. `BookDeps.lab_gate: EntryGate | None = None`; `build_lab_book` senza gate solleva `BookConfigError` (fail closed).

### 3.7 Stato e riavvio (`modules/lab/state.py`)

Verita' = ledger dry. Il ledger ha gia' posizioni, ordini, fill, bracket (`BookRestore`, invariato). Lo stato proprio del laboratorio e' minimo, in `meta` del DB dry:

| Chiave | Contenuto | Regola |
|---|---|---|
| `lab_started_at:<book>` | istante del primo avvio del book (ISO UTC) | scritto una volta, come `starting_quote:<book>`; ancora delle macchine a stati |
| `lab_rules_version:<book>` | `rules_version` del tipo | scritto una volta; diverso al riavvio = `StartupError` ("parametri cambiati: nuovo book, nuovo nome") |
| `lab_state:<book>` | JSON `chronos.lab.state.v1`: `maker_started_at` per prodotto (A); `pending_entry` `{product, signal_id, signal_bucket, limit, tp, sl, valid_until}` o `null` (C, C0); `updated_at` | scritto PRIMA di inviare l'ordine (write-ahead) e dopo ogni esito terminale |

- Scrittura: dentro `async with ledger.exclusive():` seguito da `meta_set` (stesso schema del salvataggio del kill switch, 2.15). Verificare prima che `meta_set` non prenda a sua volta il lock (deadlock); se lo prende, chiamarlo senza `exclusive()`.
- Caricamento: in `Runtime._build`, dopo `restores` e prima di `build_books`, `lab_states = {b.name: await load_lab_state(ledger, b, clock)}` per i book `lab:`; passati in `BookDeps.lab_states`.
- Al primo batch dopo il riavvio:
  - A, B, D: l'obiettivo si ricalcola dalla storia da `lab_started_at` (include i bucket chiusi durante il fermo); se reale e obiettivo differiscono si esegue in ritardo e il journal registra `late_min`. A: se `now - maker_started_at >= 3600` si va diretti alla fase taker.
  - C, C0: `pending_entry` con `valid_until > now`, book flat e nessun ordine aperto (il restore lo ha cancellato): si ripiazza con gli STESSI `L`, `tp`, `sl` e `time_in_force_s = valid_until - now`; altrimenti si azzera (journal `expired_during_downtime`). Eventi rilevati durante il fermo con validita' residua: ingresso con validita' residua; senza: `missed_downtime`. Posizioni oltre le 24 h: uscita alla prima barra.
  - Bracket di C: restore e `resolve_gap` esistenti (TP/SL toccati nel fermo si risolvono alla barra che li ha toccati). Bracket irraggiungibili di A, B, D, HODL: ripristinati allo stesso modo.
- Invariante "perdita di stato zero": nessuna informazione necessaria al driver vive solo in memoria; tutto e' nel ledger o nelle tre chiavi.

### 3.8 Journal (`modules/lab/journal.py`)

`data/lab/journal/YYYY-MM-DD.jsonl`, append con flush e fsync per riga (frequenza bassa), riga troncata in coda tagliata all'apertura (come `label_resolver._cut_torn_tail`). Schema `chronos.lab.journal.v1`:

```json
{"v": "chronos.lab.journal.v1", "at": "...Z", "book": "lab_a_trend", "type": "a_trend", "rules_version": "lab:a_trend@...",
 "product": "BTC-EUR", "event": "signal|order|result|blocked|refused|missed|ignored_busy|residual_unfilled|degraded|state",
 "tf": "1d", "bucket_open": "...Z", "signal_id": "...", "decision_id": "...", "target": true, "actual": false,
 "inputs": {"close": 0.0, "ref_max": 0.0, "ref_min": 0.0, "z": null, "sigma1h": null, "q": null},
 "order": {"type": "limit_post_only", "limit": "...", "tif_s": 3600, "tp": "...", "sl": "..."},
 "result": {"status": "filled", "reason": "", "size": "...", "avg_price": "...", "fees": "...", "liquidity": "maker"},
 "ref_price": "...", "late_min": 0}
```

`ref_price` = apertura della prima barra 1m dopo la chiusura del bucket (il prezzo O_(t+1) del backtest), per misurare lo scarto di implementazione. I driver NON chiamano `deps.on_execution` (le statistiche ops di fill maker restano del champion); gli eventi `position_closed` arrivano dal listener del ledger come per ogni book.

## 4. Wiring

`modules/dryrun/books.py`:
- `BookKind` += `"lab"`; `ShadowBook.rules_version: str | None = None`;
- `BookDeps` += `lab_gate`, `lab_states: Mapping[str, LabState]`, `lab_history: Mapping[str, LabHistory]`, `lab_journal: LabJournal | None` (default vuoti: i test esistenti non cambiano);
- `build_lab_book(cfg, deps, spread) -> ShadowBook`: `_infrastructure(...)` (unico costruttore), gateway avvolto con `deps.wrap_gateway(name, gateway)` se presente (morte del processo su `SpotViolation`/`DryRunViolation`, motivo di uscita annunciato al ledger), driver per tipo; `ShadowBook(name, "lab", account, guard, gateway, spread, engine=driver, rules_version=...)`;
- `build_books`: ramo `cfg.decider.startswith("lab:")` nel secondo giro (come `buy_and_hold`).

`modules/app.py`:
- `warm()` restituisce anche `LabHistory` (solo se ci sono book `lab:`); aggiornare i tre punti che spacchettano `warmed` (oggi tuple di 5);
- `_LabGate`, `lab_states`, journal, campi nuovi di `BookDeps`;
- `ledger.set_trade_info(..., model_version=sb.model_version or sb.rules_version, ...)`: i `closed_trades` dei book di laboratorio portano la loro `rules_version` (mai attribuiti al champion da `rollback_advice`);
- `status.entries_blocked` per i book `lab` da `_LabGate`;
- nient'altro: `_plan_books` lascia passare i decider non `registry:` (nessun cambio), nessun `Watchdog` (non in `MODEL_BOOK_KINDS`), `evaluate_decider_health` e `_sample_ops` li ignorano (`Book.engine` e' `None` per un driver non `DecisionEngine`), `reload_models` e `book_needs_restart` riguardano solo `registry:` (un book di laboratorio aggiunto parte solo al riavvio), `forced_test_books` invariato.

Registry: i book di laboratorio non hanno artefatti nel `ModelRegistry`, L5 non li vede, nessun `promote`/`rollback` li tocca; la loro versione e' `rules_version`.

## 5. Report

`modules/dryrun/report.py` (additivo, nessun criterio esistente cambia):
- `books` gia' itera `cfg.books`: i sei book compaiono con il blocco standard (equity da `data/equity`, PnL, DD).
- nuovo blocco giornaliero `lab` (presente solo se esiste almeno un book `lab:` o `lab_hodl`), letto dal journal, dai `closed_trades` e dall'equity:
  - per book: `rules_version`, `started_at`, `signals`, `submitted`, `filled`, `blocked` per motivo, `missed`, `late_min_p50`, `maker_fill_rate` (A fase 1, C, C0), `realized_cost_bps_mean` per giro con il costo assunto dal quant accanto (A, B, D 110/77; C, C0 50 a target, 80 a stop/tempo), `slippage_bps_mean` (fill contro `ref_price`), `ret_since_start`, `max_dd_frac`;
  - confronto con HODL: `ret_since_start - ret_since_start(lab_hodl)` e `max_dd_frac / max_dd_frac(lab_hodl)` (la seconda e' la condizione (e) di A), sulle equity dalla stessa `started_at`;
  - C e C0: solo i conteggi di trade (`trades_c`, `trades_c0`); la differenza si calcola solo nello sguardo unico.
- `generate()` aggiunge `gate_econ_by_book`: `evaluate_gate_econ` INVARIATA per ogni book di laboratorio (con il suo `starting_quote`, `random_pnl=None`, `ece=None`, equity del book). `gate_econ` (champion) resta identico. Per costruzione questi gate restano al massimo `INCOMPLETE` (incoerenza I2).
- `modules/lab/forward.py`, CLI `python -m modules.lab.forward [--book B] [--look]`: senza `--look` stampa solo idoneita' (data >= 2027-10-28 oppure minimo della tabella della sez. 5 del quant: A 10 trade, B 10 cambi, C 40, D 40) e conteggi; con `--look`, se idoneo, scrive UNA volta (apertura esclusiva, seconda corsa rifiutata) `data/reports/lab/forward_look_<book>.json` con le statistiche primarie della sez. 3, C - C0 e B - B0, calcolate con le funzioni statistiche dello script di backtest del quant (importate, non riscritte). Implementazione delle statistiche dopo lo script del quant (passo 13).

## 6. Retrocompatibilita'

- YAML esistenti validi senza modifiche; `_default_books()` invariato; nessun campo rimosso.
- Book esistenti: stesso codice, stesse decisioni, stesso report (`gate_ops`, `gate_econ`, `decisions_by_book`, parita' delle decisioni, `rollback_advice` invariati: il laboratorio non scrive `data/decisions` e le sue `closed_trades` hanno un'altra `model_version`).
- `test_books.py`, `test_app*.py`, `test_report*.py`, `test_c11_wiring.py`, `test_obligations.py`, test di replay e degli indicatori: verdi senza modifiche ai test esistenti (l'unica modifica ammessa e' l'aggiornamento della tupla di `warm()` nei fake che la costruiscono, se ce ne sono).

## 7. Riavvio controllato del dry run

Non esiste un comando `graceful_stop` nel codice: l'arresto ordinato e' `Runtime._shutdown` su SIGTERM/SIGINT (ingressi rifiutati, ingressi pendenti cancellati, ordini in volo attesi, barre scritte, kill switch salvato, ops chiuse; bracket mai toccati). Procedura:

1. Codice in `main` con tutti i test verdi; config con i sei book pronta ma non ancora attiva.
2. Scegliere un'ora fuori da 23:50-00:40 UTC (chiusura giornaliera di A/B, learner 00:15, reload 00:30) e lontano da xx:00 per non perdere decisioni orarie gia' dovute (comunque recuperate dal riavvio).
3. Backup del DB dry (`sqlite3 .backup` o `Connection.backup()`) in `data/backup/`.
4. `docker compose stop chronos` (SIGTERM, grazia 10 s); verificare nel log `agent_stopping` con `exit_code` 0 e nessun `shutdown_step_failed`.
5. Attivare i book in `config/config.yml` (prima solo `lab_hodl`, `lab_a_trend`, `lab_b_rotation`); `docker compose up -d chronos`.
6. Verifiche: eventi `book_restored` per i book esistenti con `unprotected` vuoto; `status.json` con i nuovi book; `meta` con `starting_quote`, `lab_started_at`, `lab_rules_version` dei nuovi; `lab_hodl` investito entro pochi minuti; heartbeat `ok`; equity dei book esistenti uguale a prima dello stop salvo il mercato.
7. Ritorno indietro: togliere i book dalla config e riavviare; le loro righe nel ledger (colonna `book`) restano e non influenzano gli altri book.
8. Il fermo pesa sull'uptime di G-DRY-OPS (0,99 su 7 giorni = ~100 minuti): un riavvio di pochi minuti non lo compromette; non e' un crash.

## 8. Test

| ID | File | Cosa prova |
|---|---|---|
| T1 | `tests/test_lab_config.py` | `lab:<tipo>` accettato; tipo ignoto, `params` diversi dai congelati, `params` su un decider non `lab:`, universo fuori da `market.products` rifiutati; `_default_books()` invariato; `rules_version` stabile (valore atteso fissato nel test) |
| T2 | `tests/test_lab_safety.py` | NESSUN book nuovo puo' istanziare il gateway live: (a) scansione AST di `modules/lab/**`: nessun import di `modules.execution.exchange`, `modules.probe`, SDK `coinbase`, nessun identificatore `LiveExecutionGateway`, `build_exchange`, `CoinbaseExchange`, `read_only`; (b) ogni book `lab:` costruito da `build_books` ha `type(book.gateway) is ShadowExecutionGateway`; (c) `BookDeps` con ledger `live` -> `LedgerModeMismatch`; (d) i driver non tengono riferimenti ad altri gateway (ispezione degli attributi: l'unico gateway e' il wrapper il cui interno e' lo shadow del book); (e) senza `lab_gate` -> `BookConfigError`; (f) `FORBIDDEN_TOKENS` e `margine` assenti nei file nuovi |
| T3 | `tests/test_lab_rules.py` | serie sintetiche con date attese: ingressi e uscite di A; isteresi e cassa di B; evento, deduplica, livelli L/TP/SL di C; slot delle 12:00 di C0; Q, soglia, breakout e uscita a 60 barre di D; invarianza al prefisso (aggiungere barre future non cambia i valori passati: nessun lookahead) |
| T4 | `tests/test_lab_parity.py` | coerenza incrementale-batch: N giorni di barre 1m (fixture dallo store o sintetiche) passati come `BarClosed` al driver con gateway finto danno nel journal gli stessi segnali delle funzioni batch su `aggregate_bars` delle stesse barre; con un riavvio a meta' (stato persistito, nuovo driver dal warm-up) il risultato e' identico; innesco del bucket in corso verificato |
| T5 | `tests/test_lab_execution.py` | con `ShadowExecutionGateway` vero e barre sintetiche: A fase maker -> taker, fill parziale cancellato e `residual_unfilled`; B uscita poi ingresso; C livelli arrotondati, scadenza 4 h, bracket, uscita a 24 h, `SL_BACKUP`; D taker in e out; sleeve indipendenti; `below_min_size`; il `BalanceGuard` non rifiuta mai la taglia del driver; il bracket irraggiungibile non diventa dust (pinna `_protect`, sez. 10) |
| T6 | `tests/test_lab_gate.py` | `shutting_down` e guardie di sistema bloccano gli ingressi; `daily_loss`, `loss_streak`, `exposure` del book NON li bloccano; `coverage_breach` li blocca (guard); le uscite non sono mai bloccate |
| T7 | `tests/test_lab_restart.py` | stop ordinato durante un ingresso maker -> riavvio: ordine cancellato dal restore, reinviato con TIF residuo, nessun doppio ingresso; posizioni e sleeve ricostruite dai fill; `lab_rules_version` diverso -> `StartupError`; `lab_started_at` e `starting_quote` conservati |
| T8 | `tests/test_report_lab.py` | blocco `lab` e `gate_econ_by_book` su fixture; documento del giorno identico byte per byte al precedente su una fixture SENZA book di laboratorio |
| T9 | `tests/test_app_lab.py` | l'app parte con i sei book; nessun watchdog per i `lab`; `set_trade_info` con `rules_version`; nessuna riga dei book di laboratorio in `data/decisions`; `ops.observe_entry` non chiamato per loro |
| T10 | `tests/test_lab_backtest_coherence.py` | coerenza segnale-backtest, con `skip_until` di `tests/wiring_helpers.py` finche' `script/lab_backtest.py` non esiste: lo script importa le funzioni da `modules.lab.rules` e `modules.dryrun.fill_model`, non definisce funzioni con gli stessi nomi; su una finestra fissa dello store le date dei segnali dello script e del driver coincidono |
| T11 | test di replay esistente | lo stesso replay con i book di laboratorio, eseguito due volte, da' ledger e journal identici |
| T12 | `tests/test_timeframes.py` | bucket 4h di `aggregate_bars` e `TimeframeAggregator` uguali; allineamento 00/04/.../20 UTC |

## 9. Piano a passi

Fase 1 (HODL, A, B):

1. `timeframes.py`: 4h con la verifica della sez. 3.1; T12; test degli indicatori verdi.
2. `modules/lab/params.py` + modifiche a `BookConfig`/`AppConfig`; T1.
3. `modules/lab/rules.py`: `trend_targets`, `rotation_targets`; T3 (parte A, B).
4. `modules/lab/history.py` (frame, innesco, update, `degraded`); parte di T4.
5. `modules/lab/state.py` e `journal.py`; test unitari di scrittura, rilettura, riga troncata.
6. `modules/lab/drivers.py`: `TargetDriver` con A e B, sizing, intent della sez. 3.4; T5 (A, B).
7. `books.py`: `build_lab_book`, ramo in `build_books`, campi di `BookDeps`/`ShadowBook`; T2.
8. `app.py`: `warm()`, `_LabGate`, stati, journal, `set_trade_info`, `status`; T6, T7, T9; tutti i test esistenti verdi.
9. `report.py`: blocco `lab`, `gate_econ_by_book`; T8. `forward.py` con la sola idoneita' e il rifiuto della seconda corsa.
10. T4 completo, T11; `config.example.yml` con i book commentati; changelog 2.16 della spec (sez. 0.6, 5.1.4, 5.2, 5.3) e questa decisione citata.
11. Dopo l'approvazione dell'utente (sez. 9 punti 1 e 3 del quant): riavvio controllato (sez. 7) con `lab_hodl`, `lab_a_trend`, `lab_b_rotation`.

Fase 2 (C, C0, D), dopo le risposte del quant:

12. `revert_events`, `control_slots`, `EventDriver`, persistenza `pending_entry`; T3, T5, T7 per C/C0. D: `squeeze_q`, `squeeze_threshold`, `squeeze_targets` nel `TargetDriver`; il book D non si costruisce (`BookConfigError`, evento `book_not_started`) finche' `q_window` e' `None`.
13. Script di backtest del quant (`script/lab_backtest.py`, del quant, pre-registrato con hash di `modules/lab/rules.py`): T10 attivo; statistiche dello sguardo unico in `forward.py` importate dallo script.
14. Riavvio controllato con `lab_c_revert`, `lab_c0_control`, `lab_d_squeeze`.

Definizione di fatto di ogni passo: test nuovi e tutti gli esistenti verdi, `mypy --strict` e `ruff` puliti.

## 10. Verifica dei vincoli

- Spot e nessuna leva: nessuna API nuova verso l'exchange; ordini solo `BUY` limit (post-only o marketable) e `SELL` di base posseduta, attraverso il gateway shadow e il `BalanceGuard` invariati. Nessun book puo' avere saldo negativo: la taglia e' calcolata sul saldo libero e il guard prenota prima dell'"invio".
- Copertura 100% con cache: `BalanceBook` virtuale in memoria, nessuna lettura sincrona del saldo.
- Tripwire C11 intoccati: `test_c11_wiring.py` e le scansioni AST di `test_app.py` invariati e verdi; `app.py` continua a non nominare `LiveExecutionGateway`; `exchange` resta `read_only=True`; nessun file nuovo nelle liste di chi puo' importare l'SDK. In piu' T2 pinna il perimetro di `modules/lab/`.
- `margin`/`margine`: assenti (T2 f, oltre a `FORBIDDEN_TOKENS`).
- Il `KillSwitch` non cambia: le guardie di conto dei book di laboratorio esistono e osservano equity e chiusure, ma i driver consultano solo `_LabGate`, come oggi `buy_and_hold` (decisione 8, Q1).
- Rischio residuo da tenere d'occhio: i bracket irraggiungibili (stop a un `quote_increment`) funzionano perche' `ShadowExecutionGateway._protect` valuta la vendibilita' al solo `bid_estimate`. La spec 2.15 dice che la regola `is_sellable(size, min(best_bid, stop_loss))` e' "condivisa con lo shadow", ma il codice dello shadow non la applica: se la si applicasse, ogni posizione HODL, A, B, D (e l'attuale `buy_and_hold`) diventerebbe dust. T5 pinna il comportamento attuale; la discrepanza spec/codice va aperta in un CONFLITTO separato prima di toccare `_protect`.

## 11. Incoerenze del documento del quant e correzione minima

- I1. C0 (sez. 5): 40-70 trade/mese incompatibili con "stesse regole di C" (una posizione alla volta, 100% del capitale, uscita a 24 h): al massimo ~30 occasioni/mese, ~15-21 fill con fill 50-70%. Correzione minima: dichiarare l'universo e il vincolo di posizione di C0 (Q2). Finche' manca, C0 non si implementa.
- I2. Secondo gate (sez. 4, "G-DRY-ECON esistente superato senza modifiche"): G-DRY-ECON e' definito per il champion; per un book a regole `beats_random_entry` e `edge_ece` non sono misurabili (nessun random gemello, nessun `p_edge`) e restano `unmeasured`, quindi il gate e' al massimo `INCOMPLETE`; `max_drawdown <= 5%` e' strutturalmente irraggiungibile per A e HODL (drawdown crypto di decine di punti; la condizione propria di A e' DD <= 0,6 x HODL); `min_trades` 30 contro i 10 della sez. 4 per A e B. Correzione minima: G-DRY-ECON resta calcolato invariato e riportato (`gate_econ_by_book`), ma il secondo gate dei book di laboratorio va scritto dal quant con criteri applicabili (Q7).
- I3. B: (a) l'esecuzione nel dry run non e' specificata (A e D si'); (b) la statistica di B e' B - B0, ma B0 non e' tra i book della sez. 5, quindi in forward non e' misurabile con fill realistici. Correzione minima: B taker in ingresso e uscita (coerente con i 110 bps assunti) e un settimo book `lab_b0_benchmark` (stessa cadenza e filtro, 1/N per moneta con R28 > 0), oppure B - B0 solo offline (Q6).
- I4. Costi di uscita: la sez. 3 definisce prezzi di stop e di uscita taker propri (5 bps), mentre la spec (5.3 punto 4) impone che ogni backtest importi `BarFillModel` (stop: `s/2 + 5 + 10` bps; taker: `s/2 + 5`). Effetto: il criterio forward "costo realizzato <= costo assunto + 10 bps" fallirebbe per costruzione su C quando prevalgono gli stop (~91,5 contro 80 bps). Correzione minima: lo script di backtest usa i kernel di `fill_model.py` per ogni prezzo di fill e i costi assunti si ricalcolano con essi (Q9).
- I5. D (sez. 3): "soglia q20 sulle barre di TRAINING della moneta" non e' definita per il dry run: il braccio U non ha SOL e usa USD; il braccio E e' EUR 2023-10-01 .. 2025-08-31. Correzione minima: fissare la finestra (proposta: braccio E, calcolata dal dry run sullo store 1m con `squeeze_threshold`, stessa funzione dello script) (Q4).
- I6. Uscite a tempo (C a 24 h dal fill, D a 60 barre) e deduplica di C non definiscono la barra esatta e il caso di evento ignorato: proposte nelle sez. 3.2 e 3.3, da confermare (Q3, Q5), perche' entrano in `rules.py` e quindi nel backtest.
- I7. Sguardo unico (sez. 2 e 5): il report giornaliero mostra gia' equity e PnL di ogni book; e' uno sguardo ripetuto se lo si usa per decidere. Correzione minima: le statistiche delle ipotesi e C - C0 solo in `forward --look` (una volta); il quant conferma che equity e scarto di implementazione giornalieri non violano la pre-registrazione (Q10).

## 12. Domande al trader-quant

- Q1. Guardie di conto (daily loss 2%, loss streak 4 stop -> 48 h, esposizione <= 50% dell'equity) sui book di laboratorio: confermi che NON si applicano (regole identiche alla sez. 3, come `buy_and_hold`)? Con l'esposizione al 50% A e D non potrebbero mai investire la terza sleeve.
- Q2. C0: universo (tutte e tre le monete alle 12:00 o una sola, quale), una posizione alla volta per il book o per moneta, quota di capitale per occasione; e quindi la frequenza attesa corretta.
- Q3. C: la finestra di 24 h della deduplica parte da ogni evento rilevato anche se non operato (book occupato)? Con eventi su piu' monete nella stessa ora e book libero, quale si opera (proposta: z piu' negativo)?
- Q4. D: finestra di training per q20 nel dry run (proposta: braccio E 2023-10-01 .. 2025-08-31 sui dati EUR dello store, per ogni moneta, SOL compresa).
- Q5. Barra esatta delle uscite a tempo: C = chiusura della 24a barra 1h completa dopo la barra del fill, vendita all'apertura successiva; D = chiusura della barra `t + 60` (barra di decisione `t`), vendita all'apertura successiva. Confermi?
- Q6. B: esecuzione taker in ingresso e uscita? Aggiungere il book `lab_b0_benchmark` o B - B0 solo offline?
- Q7. Secondo gate dei book di laboratorio: quali criteri sostituiscono `beats_random_entry` (proposta: contro HODL per A, B, D; contro C0 per C), `edge_ece` (non applicabile) e `max_drawdown <= 5%` (proposta per A: la condizione (e) DD <= 0,6 x HODL), e quale minimo di trade?
- Q8. Capitale simulato: 100 EUR per book come gli altri book del dry run, oppure il capitale reale (~28 EUR) per misurare anche gli effetti di taglia minima?
- Q9. Accetti che lo script di backtest usi i kernel di `modules/dryrun/fill_model.py` (stop con `s/2 + 5 + 10` bps, uscite taker con `s/2 + 5`) e che i costi assunti della sez. 3 si ricalcolino di conseguenza?
- Q10. Il report giornaliero con equity, PnL e scarto di implementazione dei book di laboratorio e' compatibile con lo "sguardo unico", purche' le statistiche delle ipotesi si calcolino solo con `forward --look`?
- Q11. A: con fill maker parziale il residuo resta in cassa (il sistema non consente un secondo ingresso sulla stessa posizione, quindi niente taker sul residuo, diversamente dalla sez. 3 "poi taker sul residuo"). Va bene? Il caso "nessun fill maker -> taker sull'intera sleeve" resta come scritto.

## 13. Risposta trader-quant

Autore: trader-quant. Data: 2026-10-28. Le regole normative sono nell'errata corrige della sez. 10 di `docs/decisions/2026-10-28-ipotesi-strategie-laboratorio.md` (E1-E10); qui le risposte e cio' che cambia per questa specifica. Nessuna correzione tocca la corsa offline di A e B (braccio L, gia' eseguita: INCONCLUSO/INCONCLUSO); cio' che riguarda A e B e' solo forward ed e' una nuova pre-registrazione della parte forward, da far prendere atto all'utente con l'approvazione dei book.

### 13.1 Incoerenze

| ID | Reale? | Esito |
|---|---|---|
| I1 | si' | C0 a sleeve 1/3 per moneta, una posizione per sleeve; 35-45 trade/mese [D/S] (E1) |
| I2 | si' | secondo gate riscritto F1-F6; G-DRY-ECON calcolato e riportato, non decisionale per i book a regole (E2) |
| I3 | si' | B taker in e out; settimo book `lab_b0_benchmark` (E3) |
| I4 | si' | backtest di C, C0, D con i kernel di `fill_model.py`; costi assunti ricalcolati; A e B invariati (E4) |
| I5 | si' | q20 dal training del braccio che decide D: BTC/ETH braccio U, SOL braccio E (E5) |
| I6 | si' (ambiguita') | barre esatte e deduplica fissate (E6) |
| I7 | in parte (procedurale) | report giornaliero ammesso con limiti; confronti con i riferimenti solo allo sguardo (E7) |

### 13.2 Risposte

- Q1. Confermato: guardie di conto (perdita giornaliera, serie di stop, esposizione 50%) NON applicate ai book di laboratorio; shutdown, guardie di sistema, `BalanceGuard` e `coverage_breach` sempre. Decisione 8 confermata. Nota per il futuro (scelta dell'utente, solo dopo un GO e il secondo gate): A e D al reale investono fino al 100%; eseguirli al 50% sarebbe un'altra strategia, non testata.
- Q2. C0 (E1): universo BTC/ETH/SOL-EUR; tre sleeve 1/3 mai trasferite (sizing come A e D: `budget = min(sleeve_cash(p), free(EUR))`); una posizione per sleeve; occasione per ogni moneta con sleeve flat e senza ingresso pendente alla chiusura della barra 1h che chiude alle 12:00 UTC; tutto il resto come C. Frequenza: f / (1 + f q) con f = 0,50-0,62, q ~0,45 -> 35-45 trade/mese [D/S]. In `params.py` serve un campo che distingua C0 a sleeve per moneta da C a posizione unica (struttura a cura dell'architetto).
- Q3. Si': ogni evento RILEVATO apre la finestra di 24 h (chiusura in (t - 24 h, t)), operato o no; "book occupato" = posizione aperta O ingresso pendente. Piu' eventi nella stessa ora con book libero: z piu' negativo, z = ln(C_t / C_(t-4h)) / sigma4h(t); parita': ordine dell'universo (E6).
- Q4. Proposta non accettata nella forma: la soglia del forward deve essere quella del training del braccio che decide D (il forward e' un secondo holdout della stessa regola). BTC, ETH: braccio U, 2016-01-01 .. 2020-12-31; SOL: braccio E, 2023-10-01 .. 2025-08-31; senza braccio U, braccio E per tutte. Conseguenza software: al posto di `q_window` un `q20` numerico congelato per prodotto (con fonte e finestra), copiato dal file dei risultati di training dello script C/D; il book D non parte finche' manca. Il valore di SOL e' ricalcolabile dallo store con `squeeze_threshold` (test di coerenza possibile). Q e' adimensionale: la soglia USD vale su EUR [D] (E5).
- Q5. Confermato, con due precisazioni (E6). C e C0: f = barra 1h del fill; stop attivo da f inclusa, target da f+1; senza barriere entro la chiusura di f+24, vendita all'apertura di f+25. D: uscita di canale valutata alle chiusure t+1 .. t+60, altrimenti decisa alla chiusura di t+60 ed eseguita a O_(t+61); una sola azione per barra per sleeve, nuovo ingresso valutabile dalla chiusura successiva a quella dell'uscita.
- Q6. B taker in ingresso e in uscita (`limit_marketable` come A fase 2), alla prima barra 1m dopo la chiusura dell'ultimo giorno UTC del mese; cambio = vendita completa poi acquisto. Aggiungere `lab_b0_benchmark` (settimo book, fase 1 insieme a B, stesso giorno di avvio): tre sleeve 1/3 mai trasferite e mai ribilanciate (convenzione `combine_sleeves` dello script congelato), ogni sleeve al 100% se R28 della sua moneta a fine mese > 0, altrimenti cassa; esecuzione taker; si implementa come `TargetDriver` a sleeve (come A) con `RotationParams`. B - B0 sulle equity giornaliere 00:00 UTC (E3).
- Q7. Secondo gate = F1-F6 di E2, valutati solo allo sguardo, solo per ipotesi con GO offline (per A e B, oggi INCONCLUSO, lo sguardo forward e' descrittivo). Sintesi: F1 >= 6 mesi e trade chiusi >= A 10, B 10 (periodi di detenzione), C 40, D 40; F2 segno (A netto >= 0; B - B0 >= 0; C netto >= 0 e C - C0 > 0; D netto >= 0); F3 costo realizzato <= assunto + 10 bps; F4 fill di C entro 15 punti dal training; F5 solo A: DD forward <= DD di HODL (non 0,6: su 12 mesi non stimabile); F6 nessuna violazione spot/copertura, nessun `degraded` pendente. Sostituzioni: `beats_random_entry` -> F2 (B0, C0) e F5 (HODL per A); battere HODL in rendimento NON e' un criterio (nessuna potenza); `edge_ece` non applicabile; `max_drawdown <= 5%` non applicabile. `gate_econ_by_book` resta come riportato e non decisionale.
- Q8. 100 EUR per book. A 33 EUR per sleeve gli arrotondamenti a `base_increment` pesano < 0,01% [D]; le taglie minime dell'exchange non legano. Gli effetti di taglia minima si misureranno, se mai, al passaggio al reale.
- Q9. Si', con s = lo stesso spread per prodotto usato dal gateway shadow, congelato nel `00_preregistration.json` dello script C/D (anche per il braccio U: prudente). Costi assunti per giro (per F3 e per `realized_cost_bps_mean` del report): C e C0 target 50, tempo 80 + s/2, stop 90 + s/2; D 110 + s; A e B 110 (invariati, braccio L gia' corso; differenza < 1 bps per lato sugli USD [S], nessuna nuova corsa). Nessun tetto di quantita' nei backtest (E4).
- Q10. Si', alle condizioni di E7: ogni giorno equity e PnL per book (sezione esistente) e metriche di implementazione (conteggi, ritardi, tasso di fill, costo realizzato, slippage); nessuna azione sui book puo' dipendere dalla performance interim, salvo arresto per errore operativo o per futilita' (conservativi). Una correzione che cambia i fill = nuovo nome di book. Conseguenza sul blocco `lab` della sez. 5: togliere dal giornaliero `ret_since_start - ret_since_start(lab_hodl)` e `max_dd_frac / max_dd_frac(lab_hodl)` (sono il criterio F5 e il confronto della statistica): si calcolano solo con `forward --look`. `ret_since_start` e `max_dd_frac` del singolo book restano (sono gia' nell'equity).
- Q11. Si'. Residuo maker parziale di A in cassa fino all'uscita, frazione non investita registrata (`residual_unfilled` con la frazione) come scarto di implementazione; nessun fill maker -> taker sull'intera sleeve. Il backtest di A (O_(t+1), 100% della sleeve) non cambia (E8).

### 13.3 Modifiche a questa specifica che ne derivano (struttura all'architetto)

- Book: sette book di laboratorio (aggiunto `lab_b0_benchmark`, tipo `b0_benchmark`); fase 1 = `lab_hodl`, `lab_a_trend`, `lab_b_rotation`, `lab_b0_benchmark`.
- C0 a sleeve per moneta; C a posizione unica (invariato).
- D: `q20` numerico congelato per prodotto al posto di `q_window`.
- `forward.py` idoneita': A 10 trade, B 10 trade (periodi di detenzione chiusi), C 40, D 40, al primo fra questi e 2027-10-28; C0 sguardo descrittivo separato a 120 trade o 2027-10-28; B0 nessuno sguardo proprio (entra in B - B0). Statistiche: A, B come lo script del braccio L (bootstrap stazionario 20 g e HAC 20 lag); C, D, C - C0 come lo script C/D (Welch a una coda e bootstrap a cluster, unita' di E1); verdetto F1-F6.
- Report giornaliero: costi assunti di E4 accanto a `realized_cost_bps_mean`; via i due confronti con HODL (Q10). `ref_price` per gamba come F3 (O_(t+1) per gli ordini a mercato e l'uscita a tempo; L, tp, livello di stop per C e C0).

### 13.4 Scelte dell'utente

1. Presa d'atto dell'errata corrige E1-E10 (in particolare: struttura di C0, settimo book B0, secondo gate F1-F6, prezzi di stop e taker di C e D da `fill_model.py`) prima di abilitare i book.
2. Dopo lo sguardo descrittivo di C0: eventuale arresto di C per futilita' (mai il contrario).
3. Solo dopo un GO e il secondo gate di A o D: come conciliare l'investimento al 100% con il limite di esposizione 50% del `RiskManager`.
4. A e B sono INCONCLUSO offline (A per 27 trade di training contro 30): il forward non autorizza capitale; un riesame richiede una nuova pre-registrazione su dati nuovi.
