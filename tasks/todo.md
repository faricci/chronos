# Modulo 1 - P0 Fondamenta (parte 1)

Spec: `docs/specs/chronos-system1-spec.md` v2.3, sez. 0.3 (CostModel), 0.5 (P0), 4.3 punti 1 e 5.
Ordine richiesto dall'utente: `modules/config.py` -> `modules/risk/costs.py` -> `modules/market/backfill.py`.
Branch: `reachitect/v2-opus55-spec` (resta questo; il rename a `v2` della sez. 0.5 e' una decisione dell'utente).

## Decisioni di scope

- Docker daemon spento e Python locale 3.14: il codice resta compatibile 3.12 (target del container), i test girano in un venv locale `.venv`. La verifica nel container 3.12 e' un passo aperto finche' Docker non e' attivo.
- I moduli v1 NON vengono cancellati in questo modulo: `utils.py` e' ancora importato da `main.py`, `data_fetch.py`, `monitoring.py`, `order_execution.py`, che hanno il loro sostituto v2 piu' avanti (sez. 0.5: "cancellato nella stessa PR che introduce il suo sostituto").
- Discrepanze trovate vanno in `.claude/CONFLITTO.md` -> `architetto-opus` (CLAUDE.md).

## Piano

- [x] 0. Tooling: `pyproject.toml` (pytest, mypy strict, ruff), dipendenze P0 in `requirements.txt` + `requirements-dev.txt`, Dockerfile su `python:3.12-slim`, venv locale
- [x] 1. `modules/config.py`
  - [x] modelli pydantic v2 (`extra="forbid"`, frozen): Paths, Market, Sentiment, Decision, Risk, Execution, DryRun, LiveRamp, Coinbase (segreti solo da env)
  - [x] `load_config(path)`, `ConfigError`, `load_config_or_exit` con exit code 2 e messaggio leggibile
  - [x] `resolve_mode(cfg, env)` + test della tabella di verita' completa
  - [x] `config/config.example.yml` con i valori della spec; un test verifica che coincida con i default
  - [x] test: YAML valido/vuoto/malformato, chiavi sconosciute (anche v1) rifiutate, sezione `coinbase` nel YAML rifiutata, 16 validazioni
- [x] 2. `modules/risk/costs.py`
  - [x] `FeeRates` (flag `assumed`), `FeeProvider` (cache 1 h, fallback config, retry a 60 s, clock iniettato)
  - [x] `CostModel` (gambe maker/taker, cost_tp, cost_sl, cost_mkt, fee floor, EV, p_breakeven, required_lift), `round_bps` half-up
  - [x] `net_return` / `net_pnl_quote` esatti in Decimal
  - [x] test: esempio di riferimento sez. 0.3 (cost_tp 120, cost_sl 196.5 -> 197, p_be 0.39, EV +358) e sez. 4.1.2 (loss_per_unit 10.27, size 0.024); tabella lift 0.4 completa (18 righe); proprieta' hypothesis (EV nullo a p_breakeven, invariante VERTICAL)
- [x] 3. `modules/market/backfill.py` + `load_bars()`
  - [x] finestre da 300 minuti (end esclusivo, minuto aperto mai richiesto), ripresa idempotente, rate limit `aiolimiter` condiviso, retry `tenacity` solo su 429/5xx/rete
  - [x] Parquet `product=/year=/month=`, dedup su `ts`, scrittura atomica, flush periodico + `finally` per le interruzioni
  - [x] `load_bars()` / `fill_flat_bars()`: barre piatte (OHLC = close precedente, volume 0), chiusura precedente anche da un mese prima
  - [x] report per prodotto (righe, intervallo, copertura, buchi > 60 min) e CLI
  - [x] test con sorgente finta (26) + test `live` manuale; prova reale su BTC-EUR e SOL-EUR (6 h): 352/347 righe, seconda esecuzione +0 righe
- [x] 4. Verifica: `pytest` 124 passati (1 `live` escluso di default, passato a mano), `mypy --strict` e `ruff` puliti sui file del modulo
- [x] 5. Conflitto CostModel risolto da architetto-opus e applicato (vedi sotto)
- [x] 6. `docker-compose.yml` con named volume `chronos-state` (chronos, test, dev)
- [ ] 7. Aperti: verifica nel container Python 3.12 (Docker spento); esito del backfill completo a 3 anni (in corso)

## Vincoli per P1 (dalla risoluzione architetto-opus, spec v2.3)

- [ ] `training/tune_policy.py`: ogni combinazione di soglie si valuta con `training/backtest.py` (stesso RiskManager, fee floor ed EV gate attivi). Test: `p_edge` sopra `p_edge_min` ma sotto `p_breakeven` deve dare `Skip(ev_negative)` anche nel tuning.
- [ ] `training/feasibility.py`: colonne solo diagnostiche `p_vertical` e `vert_mean_net_bps` (da `net_return`) in `data/reports/feasibility.md`; non in `label_config.json`, non nella regola di scelta.
- [ ] `CostModel.ev_bps` resta a due esiti: vietato aggiungere `p_vertical` (richiederebbe una nuova domanda e una nuova versione di QUESTIONS).

## Log conflitti

- 2026-10-17 `.claude/CONFLITTO.md` (Q1 probe vs CostModel, Q2 EV a due esiti): risolto da architetto-opus. Applicato: probe riscritto su `CostModel` (`python -m script.probe_coinbase`, credenziali solo da env), tabella 0.4 ricalcolata, docstring `ev_bps`/`p_breakeven`, test invariante VERTICAL, spec portata a v2.3. File cancellato.

## Review

Consegnato: `modules/config.py`, `modules/risk/costs.py`, `modules/market/backfill.py` con 124 test, piu' tooling (`pyproject.toml`, `requirements*.txt`, Dockerfile 3.12) e la riscrittura del probe sul `CostModel`.

Note per chi riprende:
- Il test del `CostModel` usa gli input arrotondati della tabella 0.4; il probe sui dati live da' gli stessi lift a +-0.01.
- `round_bps` e' half-up (196.5 -> 197). `CostModel` tiene i float esatti.
- Il backfill parte dall'ultimo minuto salvato: se un'interruzione cade in un tratto senza scambi e nulla e' ancora salvato, la ripartenza rifa' quelle finestre vuote (costo solo di tempo).
- `ruff --fix` lanciato per errore su `modules/` ha toccato 8 file v1; ripristinati con `git checkout` (lezione in `tasks/lessons.md`). `git status` conferma che nessun file v1 e' modificato.
- Non toccati: `docker-compose.yml`, `main.py`, moduli v1, `config/config.yml_template`.


# Vincoli finanziari assoluti (CLAUDE.md) - copertura spot, 2026-10-17

Risoluzione di architetto-opus in `docs/decisions/2026-10-17-copertura-spot.md`; spec portata a v2.4 (C11, sez. 4.1.6).
Attenzione: la risoluzione ELIMINA l'attached bracket. Il bracket e' sempre standalone, dopo lo stato terminale dell'ingresso.

## Fatto

- [x] `modules/execution/rounding.py` (floor_to, ceil_to, is_multiple_of) + test hypothesis
- [x] `modules/risk/costs.py`: `coverage_fee_rate`, `required_quote`, `max_base_for_quote` (T5, T6)
- [x] `modules/config.py`: `risk.max_order_quote`, `execution.balance_refresh_s/entry_poll_s/cancel_confirm_timeout_s`; nessuna chiave disattiva il guard
- [x] `modules/execution/orders.py` (ProductMeta, OrderSpec), `balance.py` (BalanceBook), `spot.py` (SpotRegistry, build_order_body, validate_body, permessi chiave), `balance_guard.py` (BalanceGuard, CoverageTicket)
- [x] Test: unitari + property-based T1, T2, T3, T5, T6, T7, T8, T9, T10, T12, T16; mutation check: 6 bug introdotti a mano sono tutti intercettati
- [x] `script/probe_coinbase.py` registra i campi per le allowlist (H5). Dati reali: `product_type=SPOT`, `product_venue=CBE`
- [x] Spec v2.4: tutta la PARTE C applicata

## Da fare (ordine B.3 dell'architetto)

- [ ] `modules/risk/sizing.py` (T4: il sizing non produce mai un ordine che il guard rifiuta) -> `risk_manager.py`
- [ ] `modules/dryrun/virtual_account.py` compone `BalanceBook("virtual")` (T17: parita' live/dry)
- [ ] `modules/execution/exchange.py`: `create_order(ticket)`, allowlist dei metodi del client, `get_accounts() -> AccountSnapshot` (T11)
- [ ] `gateway.py` / `shadow_gateway.py`: ordine E1-E4 e X1-X5 (T13, T14, T15), dust, stop virtuale
- [ ] `balance_feed.py`, `reconciler.py`, `PositionCache` e colonne `dust_*` nel ledger, KillSwitch `coverage_breach`
- [ ] T18: i costruttori dei gateway richiedono `BalanceGuard`
- [ ] Prima di P4: eseguire il probe con la key View per confermare `portfolio_type` e `can_transfer` (H5)

## Note di implementazione (deviazioni minime dalla risoluzione)

- `OrderSpec` e `ProductMeta` stanno in `execution/orders.py`, non in `balance_guard.py`, per evitare un import circolare con `spot.py`.
- `BalanceBook.apply_fill` accetta `trade_id` opzionale: lo stesso fill arriva da WS e da polling e va applicato una volta sola.
- Un fill per un ordine senza riserva non modifica i saldi (non se ne conosce il lato): scatta solo l'allarme, il prossimo snapshot corregge.
- Il drift non viene valutato sul primo snapshot (non c'e' un valore locale con cui confrontarlo).
- `DEFAULT_SPOT_VENUES={"CBE"}` e `DEFAULT_PORTFOLIO_TYPES={"DEFAULT","CONSUMER"}`: il primo e' confermato dal probe, il secondo no.
