# CONFLITTO P1b - spread/costi nello stato (O1), cadenza del backtest, baseline LightGBM

Autore: Sviluppatore (Sonnet). Data: 2026-10-18. Il `label_config` definitivo esiste (`data/datasets/label_config.json`, spec 2.7: 6h, 3/1.5, maker, H = 15 giorni, F = 3, spread di label 3 bps, holdout dal 2026-06-20, GO con lift_0 +0.074). Risoluzione precedente: `docs/decisions/2026-10-18-fattibilita.md` (gia' applicata). Leggi anche D (backtest/evaluate) di quel file: le decisioni D1-D6 valgono, qui si chiedono solo i punti aperti sotto.

## O1 - spread e costi: valori di training (mediana) contro valori live (obbligo di `tests/test_obligations.py`)

`StateBuilder` deve scrivere nello stato `liquidity.spread_bps` e il blocco `costs` (`cost_tp_bps`, `cost_sl_bps`, `p_breakeven`).
Nello storico non esiste uno spread (nessun L2 storico): in training il valore e' costante per prodotto (`label_config.spread_bps`,
mediana dal probe, poi dal recorder per ora del giorno), a runtime e' lo spread vivo del `LocalOrderBook`. Il modello vedrebbe in
training una feature quasi costante e a runtime una feature variabile: train/serve skew. Impatto numerico: con ingresso maker lo spread
entra solo in `c_sl_out` come `s/2` (0.5-1.5 bps su costi di 50-90 bps).

Opzioni: (A) spread vivo nello stato (skew); (B) a runtime si usa lo stesso valore di training (la mediana per prodotto e ora del
giorno del `label_config`) nello stato, e lo spread vivo si usa solo nel `RiskManager` (fee floor / EV gate / sizing / prezzo limite);
(C) come (B) ma `liquidity.spread_bps` esce dallo schema. Raccomandazione dello sviluppatore: (B), cosi' lo stato e' funzione delle sole
barre (punto-in-tempo, identico in training e in serving), `costs` nello stato e' quello del `label_config` e il costo vivo resta
una decisione del RiskManager. Chiedo all'architetto di decidere, di indicare dove va scritta la mediana per ora del giorno (file
del registry?) e se `liquidity.spread_bps` resta nello schema `chronos.state.v1` (nessun modello e' ancora addestrato).

## O2 - backtest (`training/backtest.py`): cadenza e semantica

La spec lo vuole "barra per barra" con la stessa `DecisionPolicy` e lo stesso `RiskManager` del runtime. 3 prodotti x 1.6 M barre x
inferenza LightGBM a ogni minuto sono ~4.8 M righe per valutazione di una combinazione di soglie in `tune_policy` (centinaia di
combinazioni): non praticabile. Proposta: le probabilita' del decider si calcolano UNA volta sui campioni a cadenza fissa
`decision_every_min` (default 15, giusto per barriere di giorni: un'ingresso a +-15 min non cambia le barriere in modo
significativo) e il backtest scorre le barre 1m solo per la gestione delle posizioni (fill, TP/SL con `BarFillModel`), mentre le
decisioni cadono sui campioni; soglie diverse riusano le stesse probabilita' (cache) e rieseguono solo policy + RiskManager +
simulazione. Chiedo: (a) cadenza accettabile? (b) un ingresso pendente per (prodotto) e una sola posizione per prodotto come nel
runtime; esposizione e daily loss sull'equity comune ai tre prodotti (book unico `backtest`)? (c) fill dell'ingresso maker: la regola
bar-level di 5.1.3 con `queue_haircut` sul volume della barra (quantita' massima `0.25 * volume`): con size di pochi euro il vincolo
non morde mai, ma il fill parziale va modellato o basta il tutto-o-niente quando `qty <= haircut*volume`? (d) il `BalanceBook` del
backtest e' lo stesso del live (`VirtualAccount`), quindi la copertura e' verificata dal `BalanceGuard` anche nel backtest (C11): lo
confermo, e non compilo un backtest che aggira il guard.

## O3 - baseline LightGBM (`training/baseline_lgbm.py`, `modules/decision/baseline.py`)

La spec descrive tre modelli (multiclass `action`, binary `edge`, multiclass `vol_regime` a 5 classi), calibrazione (Platt per `edge`,
temperature per `action` e `vol_regime`) e dati con pesi di unicita', HOLD sottocampionato 3:1 solo nel train. Non fissa
iperparametri. Con ~200 finestre indipendenti l'overfitting e' il rischio principale. Propongo default prudenti: `num_leaves=7`,
`max_depth=3`, `learning_rate=0.03`, `min_child_samples=500` (oltre a `min_sum_hessian`), `feature_fraction=0.7`,
`bagging_fraction=0.7`, `lambda_l2=10`, early stopping sul logloss del validation del fold (max 600 alberi), nessuna ricerca di
iperparametri nei fold di test. Domande: (a) accetti? (b) il target `action` per i campioni LONG sintetici (sez. 3.1.3) richiede il
valore di uscita immediata a mercato: va calcolato nel labeler con `BarFillModel`/`CostModel` (stesso codice); confermi che il
campione LONG usa `unreal_bps`, `dist_sl_atr`, `dist_tp_atr` coerenti con l'ingresso fittizio a `t - U` (e `hours_held = U/60`)? (c)
la calibrazione Platt per `edge` si adatta sul validation del fold; per il modello finale sul "fold di validation piu' recente"
(sez. 3.1.6): confermi?

## Risoluzione architetto

Autore: architetto-opus. Data: 2026-10-18. Ambito: O1, O2, O3 e un difetto trovato in `labeling.py` (F0). Vincoli invariati: solo
spot, long-only, copertura 100% (C1, C11). Nessun tripwire va modificato (`tests/test_c11_wiring.py`, `tests/test_obligations.py`):
le regole sotto li soddisfano cosi' come sono. Nessuna domanda all'utente. L'unica scelta di natura statistico-finanziaria (O3-K, fold
senza label `vol_regime`) ha un default prudente esplicito e va solo confermata dal `trader-quant`, senza bloccare il lavoro.

### 0. Principio (Decision Tokenization), applicato a tutti e tre i punti

Un token dello stato e' utile solo se e' la STESSA funzione dei dati punto-in-tempo in training e in serving. Le risposte del
decider sono probabilita' calibrate contro label che descrivono l'ordine che il runtime invierebbe davvero. Ne seguono tre regole:
- T1. Nello stato entra solo cio' che il training puo' ricostruire in modo identico. Una grandezza che esiste solo dal vivo (spread
  del book, fee lette dall'account) non entra nello stato: la usa il `RiskManager`, che e' la parte economica e non viene appresa.
- T2. L'impossibilita' di passare un valore vivo allo stato deve essere strutturale, cioe' garantita dalla firma. Non deve
  dipendere dalla disciplina di chi chiama.
- T3. Il backtest valuta cio' che fara' il runtime: stessa cadenza (1 minuto), stessa policy, stesso RiskManager, stesso guard.
  Una cadenza piu' rada e' ammessa solo come approssimazione di ricerca dentro `tune_policy`, mai per i gate.

### F0. Difetto trovato in `training/labeling.py` (da correggere per primo)

In `_scan`, ramo UPPER, la riga 166 `f_out = f_taker` e' sbagliata: deve essere `f_out = f_maker`. Lo dicono la spec 3.1.3
(`f_out = f_m`), `CostModel.c_tp_out` e la tabella di `fill_model.py`: il TP e' una vendita limit che resta nel book. Il codice
attuale sottostima di 25 bps il `net_ret` di ogni UPPER.
- Cosa NON cambia: esiti, `p_hist`, `p_breakeven` (calcolato da `CostModel`), `lift_0`, scelta della combinazione, GO, `sha256`.
  Cambiano solo il `net_ret` delle label UPPER e le diagnostiche basate sul netto.
- NON rieseguire `feasibility.py`: ricalcolerebbe `holdout_start` su uno storico che nel frattempo e' cresciuto, quindi aprirebbe di
  fatto un nuovo studio (C1 della risoluzione precedente). I campi `sigma_r_bps` e `wl_bps` del `label_config.json` restano come sono:
  sono solo informativi.
- Test nuovo in `tests/test_labeling.py`: `test_upper_exit_pays_maker_fee`, che verifica
  `net_ret == upper * (1 - f_m/1e4) / (entry * (1 + f_in/1e4)) - 1`. Se un test esistente fissa la fee taker sul TP, va corretto.
- Nello stesso commit si aggiorna la docstring di `modules/dryrun/fill_model.py`, che descrive ancora F = 1. Le righe nuove:
  maker = `close[t+F-1] * (1 - s/2)`, finestra `[t+F, t+F+T_in-1]`; taker = `open[t+F] * (1 + s/2 + slip)`.

### O1. Decisione: opzione C (lo stato non contiene lo spread; `costs` e' calcolato dal `label_config`)

**O1.1 Perche' C e non B.** Con B, `liquidity.spread_bps` sarebbe una funzione deterministica di `(prodotto, ora)`, informazioni che
lo stato contiene gia': il campo e' ridondante (oggi vale 3 per tutti e tre i prodotti, quindi non porta informazione). In piu' resta
una tentazione permanente: prima o poi qualcuno ci scrive lo spread vivo e crea lo skew. Lo spread vero non e' ricostruibile dallo
storico (nessun L2), quindi in qualsiasi forma futura il campo sarebbe o ridondante o skew (T1). L'informazione economica dello
spread di label resta comunque nello stato, dentro `costs.cost_sl_bps`, che contiene `s/2`.

**O1.2 Lo schema dello stato CAMBIA (lo dichiaro esplicitamente).**
- `modules/features/schemas.py`: `Liquidity` contiene il solo `vol_z_1d`. Nella forma canonica: `"liquidity":{"vol_z_1d":0.6}`.
- `modules/features/serializer.py`: si toglie `"spread_bps"` da `FLAT_COLUMNS` e da `flatten_state`.
- La versione resta `"chronos.state.v1"`. Motivo: non esistono ancora dataset, modelli o `DecisionRecord`, quindi v1 non e' stato
  rilasciato. Lo schema v1 si considera congelato dal primo `build_dataset` eseguito: da li' ogni modifica e' v2.
- I tre golden `tests/golden/state_{sol_flat,btc_long,cold_start}.json` si rigenerano togliendo soltanto `"spread_bps":3,` e
  nient'altro cambia (va verificato con un diff). `intent_sol_reference.json` resta com'e': `risk.spread_bps` dell'`OrderIntent` e'
  lo spread vivo del RiskManager e deve restare.
- Il tripwire `test_trades_z_1d_does_not_come_back` resta verde, e `"vol_z_1d"` resta in `schemas.py`, come richiede l'obbligo.

**O1.3 Dove sta lo spread di label (anche quello per ora del giorno): solo in `label_config.json`.**
- Non va in un file del registry. Per R1 (risoluzione precedente) ogni parametro che cambia una label sta nel `label_config` e nel
  suo `sha256`. Il registry (`models/registry/<version>/`) ne conterra' una copia identica byte per byte.
- Oggi il campo e' `spread_bps: {prodotto: float}` (gia' presente e gia' nello `sha256`).
- Formato futuro, da NON implementare finche' L4 non produce la tabella: un campo aggiuntivo
  `spread_bps_by_hour: {prodotto: [24 float]}` (ora UTC 0..23), incluso nello `sha256`. Il `fill_model.json` di L4 e' solo una
  misura: la tabella entra in un nuovo `label_config` scritto da un nuovo studio, quindi diventa una nuova versione del modello.
- Accesso unico, da implementare ora in `modules/risk/label_config.py`:

      LoadedLabelConfig.spread_for(product: str, ts_bar_close: datetime) -> float
          # oggi: self.spread_bps[product]; prodotto assente -> ValueError
          # (ts e' gia' nella firma, cosi' l'arrivo della tabella oraria non cambia i chiamanti)

  `build_dataset` (label e stato), `StateBuilder` e backtest usano solo `spread_for`, mai `spread_bps[...]` diretto.

**O1.4 Contenuto del blocco `costs` (identico in training e a runtime).**

    s        = label.spread_for(product, snapshot.t)
    atr_bps  = snapshot.atr_14_<label.cfg.atr_tf>_bps                   # campo gia' presente nello snapshot
    model    = CostModel(fees=label.fees, entry_mode=label.cfg.entry_mode, spread_bps=s, params=label.costs)
    costs    = {entry_mode: label.cfg.entry_mode,
                cost_tp_bps: round_bps(model.cost_tp), cost_sl_bps: round_bps(model.cost_sl),
                p_breakeven: None se atr_bps e' NaN, altrimenti model.p_breakeven(k_tp * atr_bps, k_sl * atr_bps)}   # Dec2

- Fee e spread vengono dal `label_config`, MAI da `FeeProvider` o dal `LocalOrderBook`. Con il `label_config` attuale:
  `cost_tp_bps` = 50, `cost_sl_bps` = round_bps(25 + 50 + 1.5 + 5 + 10 = 91.5) = 92; `p_breakeven` varia con l'ATR.
- `tp_bps` e `sl_bps` usano `atr_14_<tf>_bps` dello snapshot (ATR / chiusura HTF), come nell'esempio della sez. 0.3. Servono solo
  come feature (sez. 0.3: "solo per gate e feature"). Il RiskManager continua a usare l'ATR in prezzo e i livelli arrotondati.
- A runtime il `RiskManager` resta com'e': spread vivo (`MarketQuote.spread_bps`) e fee vive in fee floor, EV gate, sizing e prezzo
  limite, e `OrderIntent.risk.spread_bps` = spread vivo. Gia' implementato, non si tocca.
- Diagnostica di runtime (da fare con `engine.py`, non ora): se `FeeProvider.current` non e' presunto (`assumed=False`) e differisce
  dalle fee del `label_config`, si logga il warning `label_fee_mismatch` e lo si porta nel report. Non e' bloccante: la sez. 0.4 dice
  gia' di rieseguire lo studio.

**O1.5 `StateBuilder` (`modules/features/state_builder.py`): firma definitiva.**

    @dataclass(frozen=True)
    class PositionInput:
        side: Literal["FLAT", "LONG"]
        entry_px: float | None; stop_loss: float | None; take_profit: float | None
        atr_entry: float | None; opened_at: datetime | None
        @classmethod
        def flat(cls) -> PositionInput
        @classmethod
        def from_view(cls, v: PositionView) -> PositionInput       # avg_entry, sl, tp, atr_at_entry, opened_at; Decimal -> float(str(x))

    class StateBuilder:
        def __init__(self, label: LoadedLabelConfig) -> None        # risolve una volta fee, CostParams, k, atr_tf
        def build(self, *, snapshot: IndicatorSnapshot, btc: IndicatorSnapshot | None,
                  sentiment: SentimentSnapshot | None, position: PositionInput, close_t: float) -> DecisionState

- `product = snapshot.product_id`; istante logico = `snapshot.t` (= `ts_bar_close`); `clock` = `(t.hour, t.weekday())`.
- Nessun parametro per spread, fee, quote o costi (T2). `close_t` = close della barra 1m che chiude in `t` (runtime: `bar.close`
  di `BarClosed`; training: `close[t_idx]`). Deve essere `> 0`, altrimenti `ValueError`. Serve solo al blocco `position`.
- `btc`: per `BTC-*` deve essere `None` (altrimenti `ValueError`) e il blocco e' `null`. Per gli altri prodotti e' lo snapshot BTC
  alla stessa `ts`; se manca, `btc_context = null`; se c'e' con valori NaN, il blocco contiene dei `null`.
- `position`, unica eccezione "dal vivo" ammessa (lo stato reale del conto):
  FLAT -> `{FLAT, 0, null, null, null}`. LONG -> tutti i campi di `PositionInput` obbligatori (altrimenti `ValueError`), e:

      hours_held  = floor((snapshot.t - opened_at) / 1h), minimo 0
      unreal_bps  = clamp(int(round(1e4 * (close_t / entry_px - 1))), -5000, 5000)
      dist_sl_atr = clamp((close_t - stop_loss) / atr_entry, -20, 20)        # Dec2
      dist_tp_atr = clamp((take_profit - close_t) / atr_entry, -20, 20)      # Dec2

  Il prezzo di riferimento e' `close_t`, MAI il mid vivo del book (T1).
- Arrotondamenti e clamp delle feature di mercato: come la sez. 2.3 B.2 (invariati).

**O1.6 Test richiesti.**
1. `test_state_serialization_parity` (T10, obbligo di `test_obligations.py`, in `tests/test_state_builder.py`):
   - Barre: la stessa sorgente del test di parita' degli indicatori gia' esistente, almeno 5 000 barre 1m per SOL-EUR e BTC-EUR.
   - Percorso training: `compute_indicators` -> `IndicatorSnapshot.from_row` -> `StateBuilder.build` -> `serialize_state`.
   - Percorso serving: `IndicatorEngine` incrementale (`update` barra per barra) -> snapshot -> stesso `build` -> `serialize_state`.
   - Righe confrontate: una ogni 7 a partire dalla prima con `atr_14_6h` finito, piu' l'ultima.
   - Casi: posizione FLAT e una LONG fissa (`entry_px` = close di 30 righe prima, `stop_loss`/`take_profit` = entry -/+ 1.5/3 x ATR,
     `opened_at` = `t` di 30 righe prima); sentiment `None` e uno `SentimentSnapshot` fisso; BTC context per SOL.
   - Asserzione: stringhe identiche byte per byte. Unica eccezione: una differenza in cui i due valori non arrotondati stanno entro
     la tolleranza di parita' degli indicatori (`1e-9 * max(1, |b|)`) ma cadono ai due lati di un confine di arrotondamento
     (`|frac(x) - 0.5| < 1e-6` per gli interi, analogo per Dec2). Queste eccezioni devono essere <= 1 ogni 10 000 righe confrontate,
     e il test le stampa.
2. `test_state_costs_and_spread_come_from_label_config` (O1, stesso file):
   (a) `set(Liquidity.model_fields) == {"vol_z_1d"}` e `"spread_bps" not in FLAT_COLUMNS`;
   (b) i parametri di `inspect.signature(StateBuilder.build)` sono esattamente `{snapshot, btc, sentiment, position, close_t}`;
   (c) con il vero `data/datasets/label_config.json` e uno snapshot con `atr_14_6h_bps = 164.0`: `cost_tp_bps == 50`,
       `cost_sl_bps == 92`, `p_breakeven == 0.43` (= 337.5 / 779.5 arrotondato);
   (d) due `LoadedLabelConfig` che differiscono solo per lo spread (3 contro 9 bps) danno `cost_sl_bps` 92 contro 95, cioe' il valore
       dipende dal `label_config` e non dal mercato.
3. `test_flatten_roundtrip_through_canonical_string` (serve a O3):
   `flatten_state(s) == flatten_state(DecisionState.model_validate_json(serialize_state(s)))` per i 3 golden e per gli stati del T10.
4. Il tripwire `test_spread_skew_is_decided_before_the_state_builder` chiede un file `docs/decisions/*spread*.md`: va creato
   `docs/decisions/2026-10-18-spread.md` copiando parola per parola questa sezione O1 (con O1.1-O1.7). E' il registro della
   decisione, non un report.

**O1.7 Modifiche alla spec (versione 2.8-draft, changelog "2.8: spread e costi dello stato dal label_config; backtest a 1 minuto;
baseline LightGBM").**
- 2.1, paragrafo "Istante logico": "Fanno eccezione ... `position` e `costs`/`spread_bps`, letti al momento del calcolo" diventa:
  "Fa eccezione solo `position` (lato, ingresso, SL/TP e ATR d'ingresso dal ledger al momento del calcolo), con `unreal_bps` e
  distanze calcolate su `close[t]`. `costs` e' funzione del `label_config` e dello snapshot: fee e spread vivi li usa solo il
  RiskManager (sez. 4.1)."
- 2.2.1: si toglie `spread_bps` dall'esempio; la nota su `costs` diventa: "calcolati dal `CostModel` con fee, `slip`, `x` e spread
  del `label_config` (`spread_for`) e con `atr_14_<atr_tf>_bps` dello snapshot; identici in training e serving".
- 2.2.1.b: si toglie la riga `liquidity.spread_bps`.
- 2.3 B.2: firma di O1.5.
- 3.1.3: dopo "Lo spread fa parte della label ed entra nello `sha256`" si aggiunge: "Lo stesso valore (`spread_for`) e' l'unico
  spread usato nel blocco `costs` dello stato; lo stato non contiene lo spread vivo."

### O2. Backtest (`training/backtest.py`)

**O2.1 Cadenza: 1 minuto per il backtest di riferimento, 15 minuti solo per la ricerca in `tune_policy`.**
- `EVAL_DECISION_EVERY_MIN = 1`. Lo usano GE (E1-E5), G3, il backtest dell'holdout, il report e il confronto G-DRY-OPS sulla
  frequenza dei BUY. Il runtime decide ogni minuto (C2, sez. 2.1) e i gate devono misurare quel sistema (T3). A 1 minuto ingressi
  e uscite del modello cadono al primo minuto in cui la policy li emette, come dal vivo. Lo stesso vale per i BUY nati da picchi
  brevi di probabilita', che una cadenza da 15 minuti non vedrebbe.
- `TUNE_DECISION_EVERY_MIN = 15`, cioe' le barre con `ts_bar_close.minute % 15 == 0` (contengono la griglia oraria del
  training). E' un'approssimazione di ricerca, accettata perche': con una sola posizione per prodotto e durate mediane di 48 h un
  ritardo d'ingresso di 14 minuti al massimo sposta di poco l'esito; con barriere di piu' ATR a 6h, 15 minuti valgono circa 1/24
  dell'ATR (una deriva trascurabile); il numero di trade e' limitato dalla durata delle posizioni, non dalla cadenza.
- Misura obbligatoria: per la policy scelta `evaluate.py` esegue i fold di test a 1 e a 15 minuti e riporta `cadence_sensitivity`
  (numero di trade e media di `net_ret_bps` per entrambe). E' solo diagnostica: il gate usa la cadenza di 1 minuto.
- Costo stimato a 1 minuto: circa 3.5 M stati FLAT sui fold di test (StateBuilder + flatten, ~5 min, una sola volta per modello,
  in cache), piu' gli stati LONG dei soli trade (~300-500 trade x ~4 500 minuti, ~3 min). E' praticabile.

**O2.2 Struttura di `training/backtest.py`.**

    @dataclass(frozen=True)
    class BacktestConfig:
        starting_quote: Decimal                 # train.yml backtest.starting_quote, default "100.00"
        decision_every_min: int                 # 1 (gate) o 15 (tune)
        book: str = "backtest"
        risk: RiskConfig; fallback_fees: FallbackFees; entry_timeout: EntryTimeout; fill: FillModelParams   # dall'AppConfig

    @dataclass(frozen=True)
    class ProductMarket:                        # uno per prodotto, barre GREZZE (mai detrend, C2 della risoluzione precedente)
        product: str; ts: np.ndarray            # datetime64[ns] inizio barra; ts_bar_close = ts + 1 min
        o; h; lo; c; v: np.ndarray              # float64, griglia contigua di load_bars
        atr: np.ndarray                         # atr_14_<atr_tf> in PREZZO
        snapshots: Callable[[int], IndicatorSnapshot]   # IndicatorSnapshot.from_row sulla riga i di compute_indicators

    @dataclass(frozen=True)
    class Span:
        entry_start: pd.Timestamp; entry_end: pd.Timestamp     # ts_bar_close delle decisioni d'ingresso ammesse: [start, end)
        data_limit: pd.Timestamp | None                        # ingresso solo se label_window_end(t) < data_limit
        fold: int                                              # -1 = holdout

    class FlatProbCache:                        # risposte FLAT, una volta per (modello, prodotto, cadenza, span)
        path = data/backtest_cache/<model_key>/<product>_<span_tag>_D<d>.parquet
        colonne: t_idx, p_buy, p_sell, p_hold, p_edge, p_reg_0..p_reg_4   # gia' calibrate, ordine ACTIONS / REGIME_LEGEND
        model_key = sha256(file del modello + calibration.json + label sha + FLAT_COLUMNS)
    class LongProbCache:                        # in memoria, condivisa da tutte le combinazioni di una run di tune_policy
        chiave: (product, decision_idx, fill_idx, str(entry), str(sl), str(tp), str(atr_entry))
        valore: probabilita' action [BUY, SELL, HOLD] calibrate per ogni barra di decisione da fill a vertical,
                calcolate a blocchi di 1440 barre solo quando servono

    async def run_backtest(markets: Mapping[str, ProductMarket], decider: BaselineDecider, policy: PolicyConfig,
                           label: LoadedLabelConfig, span: Span, cfg: BacktestConfig, registry: SpotRegistry,
                           flat_cache: FlatProbCache, long_cache: LongProbCache) -> BacktestResult

    @dataclass(frozen=True)
    class BacktestResult:
        trades: pd.DataFrame        # colonne D1 + entry_px, exit_px, fees_eur, label_outcome, label_net_bps, cadence_min
        equity: pd.Series           # equity a ogni chiusura d'ora (quote + somma di base x close), indice UTC
        skips: dict[str, int]       # per motivo: Skip del RM, CoverageRefused, entry_pending, kill_switch
        metrics: dict[str, float]   # sharpe (rendimenti giornalieri UTC dell'equity, ddof=1, x sqrt(365)), max_dd_frac,
                                    # profit_factor (somma pnl>0 / |somma pnl<0|), turnover_per_year
                                    # ((notional d'ingresso + d'uscita) / equity media / anni), fees_eur, haircut_blocked_bars

- Le probabilita' FLAT e LONG si calcolano SOLO tramite `BaselineDecider.answers_batch` (O3.8) su
  `flatten_state(StateBuilder.build(...))`, cioe' lo stesso percorso di `decide` (lo garantisce il test O1.6.3). In
  `tune_policy` cambiano solo le soglie: le cache si riusano e a ogni combinazione si rieseguono policy, RiskManager, guard e
  simulazione. Nessuna scorciatoia che salti uno di questi passi (sez. 3.5 punto 10).
- Il calcolo a blocchi degli stati LONG futuri di un trade aperto non e' lookahead: ogni stato usa solo barre `<= t'`, e la decisione
  in `t'` agisce solo da `t'+F`.

**O2.3 Ciclo di simulazione (un solo book `backtest`, equity comune ai tre prodotti): confermato (b).**
1. Si percorrono le barre di decisione della cadenza, in ordine `(ts_bar_close, prodotto)`. Prima di ogni barra di decisione `t`
   si chiama `SimulatedExchange.advance(t)`: ogni ordine o posizione aperta cerca in modo vettoriale il primo evento nelle barre
   `(ultima elaborata, t]`, e gli eventi di tutti i prodotti si applicano in ordine di barra e poi di prodotto. Senza ordini ne'
   posizioni aperte non c'e' nulla da scorrere.
2. Mark: `equity = VirtualAccount.equity({p: close[t]})`, `exposure = somma(base_p x close_p[t])`. Poi si chiamano
   `AccountGuards.observe_equity(now, equity, exposure)` e, alla chiusura di ogni trade, `observe_trade_closed(now, pnl_quote,
   exit_reason)`. Sono le stesse chiamate che fara' l'orchestratore live (O2.6). `now = ts_bar_close(t) + S` (clock simulato).
3. Prodotto FLAT senza ingresso pendente, e solo se `t` e' in `[entry_start, entry_end)` e (`data_limit` assente oppure
   `label_window_end(t) < data_limit`): `DecisionPolicy.resolve(answers_FLAT[t], "FLAT")`. Se BUY: `RiskManager.plan_entry` con
   `quote = MarketQuote(best_bid = Decimal(str(c[t+F-1] * (1 - s/2/1e4))), best_ask = Decimal(str(c[t+F-1] * (1 + s/2/1e4))))`,
   `s = spread_for(product, t)`, `atr = Decimal(str(atr[t]))`, fee del `label_config`, `equity`, `free_quote = BalanceBook.free("EUR")`,
   `p_edge_min` della policy, `position = ledger.view(book, product)`, `gate = KillSwitch`. Poi `BarExecutionGateway.submit_entry`.
4. Prodotto LONG: `resolve(answers_LONG[t], "LONG")`. Se SELL: `plan_exit` -> `submit_exit` (O2.4).
5. Prodotto con ingresso pendente: la decisione si conta come `entry_pending` e non si esegue (sez. 0.6).
6. Regole d'ingresso successive (stesse del runtime). Dopo un `NO_FILL` deciso in `t`, il prossimo ingresso si puo' decidere solo
   da `t + T_in`, cioe' dopo la cancellazione confermata. Dopo un'uscita alla barra `e`, il prossimo ingresso si puo' decidere da `e`.
7. Gli `id` sono deterministici: `decision_id = f"bt-{product}-{t_idx}"`, `client_order_id = uuid5(NAMESPACE_CHRONOS,
   f"{decision_id}:{leg}")`. Due esecuzioni identiche devono produrre trade list identiche byte per byte.

**O2.4 Esecuzione bar-level: chi e' `SimulatedExchange` e dove sta.**
- `modules/dryrun/sim_exchange.py`, classe `SimulatedExchange(registry, fill: BarFillModel, fees: FeeRates, spread_for)`. E' una
  sede d'esecuzione a barre: tiene gli ordini a riposo, consuma le barre e restituisce eventi `SimFill(client_order_id, product,
  side, price: Decimal, size: Decimal, fee: Decimal, liquidity, bar_idx, trade_id = "sim-...")` e `SimTerminal(client_order_id,
  status, bar_idx)`. Non ha saldi, non prenota e non conosce il guard. Tutti i prezzi vengono dai kernel di `fill_model.py`.
  La condividono il backtest e i test e2e della sez. 4.3.
- `modules/dryrun/bar_gateway.py`, classe `BarExecutionGateway(book, guard: BalanceGuard, account: VirtualAccount,
  exchange: SimulatedExchange, ledger: PositionLedger, registry: SpotRegistry, clock)`. `guard` e' obbligatorio e non Optional.
  Ha il ciclo di vita degli ordini del live: `guard.authorize(spec)` -> `ticket.consume()` -> `exchange.submit(spec, ...)`; gli
  eventi passano a `account` (che chiama `BalanceBook.apply_fill` e `release`) e al `PositionLedger`
  (`PositionLedger.open(":memory:", "dry")`, la stessa classe del live).
  Vincolo C11 sui nomi: `CoverageTicket` NON va scritto come identificatore in questi file (tripwire
  `test_only_the_guard_makes_tickets_and_reserves`); il ticket resta una variabile locale senza annotazione.
- Regole (sez. 5.1.3, colonna bar-level):
  - Ingresso maker, limite `L` = `entry` gia' arrotondata dal RiskManager (floor del best_bid), quindi `<=` al limite della label
    (vale l'invariante D1). Riposa sulle barre `[t+F, t+F+T_in-1]`. Fill TUTTO-O-NIENTE alla prima barra `k` della finestra con
    `low[k] < L` E `qty <= queue_haircut * volume[k]` (volume in base). Una barra con `low < L` ma volume insufficiente NON
    riempie: si conta in `haircut_blocked_bars` e si prova la barra seguente (R4, conservativo). Risposta a (c): niente fill
    parziali nel backtest. Restano della sola faccia tick (dry run), e l'eventuale differenza la misura il dry run.
  - Ingresso taker: fill a `t+F` a `taker_entry_px(open[t+F], s, slip)` se `<= L`, altrimenti `EXPIRED`.
  - Al terminale dell'ingresso: si rilascia la riserva residua, poi `authorize(SELL bracket, base_size = floor_to(min(filled,
    sellable)))`. Lo stop vale dalla barra di fill compresa (watchdog senza bracket, B4), il TP dalla barra `fill+1` con
    `high > TP` (senza haircut, come la colonna bar-level). Entrambi nella stessa barra: SL. Prezzo SL =
    `sl_exit_px(SL, open[j], s, slip, x)` con fee taker; TP = `TP` con fee maker. `opened_at = ts[fill]` (inizio barra, stessa
    convenzione del training, O3.3).
  - SELL del modello decisa in `t'`: il bracket resta attivo sulle barre `t'+1 .. t'+F-1`. Se si riempie li', la SELL decade (sez.
    4.1: "se il bracket risulta FILLED non vende"). Altrimenti a `t'+F` si cancella il bracket, si rilascia la sua riserva, si
    chiama `authorize(SELL market)` e si esegue a `market_exit_px(open[t'+F], s, slip)` con fee taker. `exit_reason = "SELL"`.
  - Orizzonte: alla barra `vertical = fill + H`, senza TP ne' SL prima, si cancella il bracket, si chiama `authorize(SELL market)`
    e si esegue a `market_exit_px(close[vertical], s, slip)`, che e' la formula della label. `exit_reason = "HORIZON"`. Buco di spec
    da chiudere in P2: la sez. 5.1.2 elenca "orizzonte" fra le uscite, ma il runtime non lo implementa ancora. Va aggiunto al
    watchdog con trigger a `ts_bar_close(vertical)` (l'ordine arriva a `+S`, uno scarto di 2 minuti accettato e misurato dal dry run).
  - Fee in quote: `price * size * f / 1e4`, in Decimal esatto, con `f` del `label_config` (maker o taker secondo la liquidita').
- Test AST nuovo, `tests/test_bar_gateway.py::test_bar_gateway_requires_guard`: stessa logica del T18 applicata a
  `modules/dryrun/bar_gateway.py` (`guard` obbligatorio, annotato `BalanceGuard`, nessun `None`, `authorize` chiamato). E' un file
  di test nuovo: non tocca i tripwire.

**O2.5 Copertura nel backtest (C11): confermato (d).**
- Un `VirtualAccount("backtest", starting_quote, clock)` nuovo per ogni fold e per l'holdout. Compone `BalanceBook(source=
  "virtual")`, la stessa classe del live. Saldo iniziale `{"EUR": starting_quote}`.
- `BalanceGuard(balances, positions = ledger.book_view("backtest"), products = registry, fees = FeeSource fissa con le fee del
  label_config, fallback = AppConfig.dry_run.fill_model.fallback_fees, kill_switch = KillSwitch)`. La prenotazione usa `f_cov` =
  120 bps come dal vivo.
- Ogni ordine del backtest (ingresso, bracket, SELL di mercato) passa da `authorize`. Un `CoverageRefused` sull'ingresso diventa
  uno skip `insufficient_balance` (o `kill_switch`). Un `CoverageRefused` su un'uscita, o un `CoverageAlarm` di `apply_fill`, nel
  backtest e' un bug: si solleva `BacktestInvariantError` e il processo esce con 1.
- `SpotRegistry.from_raw` riceve i payload di prodotto salvati da `data/probe/coinbase_probe.json`. Se il probe non contiene i
  campi grezzi richiesti da `_meta_from_raw` (`product_type`, increment, min size, valute), si estende il probe in modo che salvi
  `get_product_raw` (lettura pubblica) e lo si riesegue. E' VIETATO scrivere a mano increment o min size.
- Prerequisito: `modules/dryrun/virtual_account.py` secondo la sez. 5.3 punto 5, con il test `test_live_and_dry_guard_parity`
  richiesto dal tripwire di `test_c11_wiring.py` (stessa sequenza di `OrderSpec` autorizzata da un `BalanceBook` "exchange" con
  snapshot fresco e da uno "virtual" con gli stessi saldi: stessi esiti e stesse riserve).

**O2.6 Esposizione e kill switch nel backtest.**
- Prerequisito: `modules/risk/kill_switch.py` completo (sez. 4.1.4, 4.3 punto 6), con i tre test del tripwire
  (`test_no_stale_trip_with_a_90s_bar_lag`, `test_stale_bar_trips_beyond_180s`, `test_coverage_breach_blocks_buys`). Lo stato e'
  persistito in `kill_switch_state` sulla connessione del `PositionLedger` (`ledger.connection`, `:memory:` nel backtest).
- API fissata qui, la stessa per live, dry e backtest:
  `AccountGuards.observe_equity(now, equity: Decimal, exposure_quote: Decimal)`,
  `AccountGuards.observe_trade_closed(now, pnl_quote: Decimal, exit_reason: Literal["TP","SL","SELL","HORIZON","WATCHDOG"])`,
  `KillSwitch.entry_blocked(book) -> str | None` (e' l'`EntryGate` del RiskManager), `KillSwitch.coverage_breach_active(book)`.
- Guardie di conto, attive nel backtest con i valori della `RiskConfig`:
  - daily loss: `equity - equity_apertura_giorno_UTC <= -max_daily_loss_frac x equity_apertura`. L'apertura e' l'equity alla prima
    barra del giorno UTC. Blocca gli ingressi fino alle 00:00 UTC.
  - loss streak: `max_consecutive_losses` chiusure consecutive con `exit_reason` in {SL, WATCHDOG}; una chiusura di altro tipo
    azzera il conteggio. Pausa di `loss_streak_pause_h`.
  - exposure: `exposure_quote / equity > max_total_exposure_frac` blocca i nuovi ingressi. `max_position_frac` resta nel sizing.
- Guardie di sistema: e' la stessa `SystemGuards`, alimentata con le barre pubblicate a `ts_bar_close + S` sul clock simulato, quindi
  non scatta mai per costruzione. Nessun parametro "disabilita".
- A cadenza 15 le guardie vedono l'equity solo ogni 15 minuti (approssimazione di ricerca, O2.1). A cadenza 1 la vedono come il
  runtime.

**O2.7 Span (riprende D1 della risoluzione precedente).**
- Test del fold `k`: `[test_start_k, test_end_k)`. `data_limit` = `holdout_start - EMBARGO` solo per l'ultimo fold; per gli altri
  `None` (un trade puo' chiudersi oltre `test_end`).
- Validation del fold `k` (usata da `tune_policy`): `[val_start_k, test_start_k)` con `data_limit = test_start_k - EMBARGO`.
- Validation finale: `[val_start_final, holdout_start)` con `data_limit = holdout_start - EMBARGO` (O3.6).
- Holdout: `[holdout_start, fine)` con `data_limit = ultima ts_bar_close + 1 ns`.

**O2.8 Test (`tests/test_backtest.py`, oltre a quelli di D6).**
`test_backtest_every_order_goes_through_the_guard` (spy su `authorize`: ordini simulati == ticket consumati);
`test_coverage_refusal_becomes_skip`; `test_one_pending_entry_and_one_position_per_product`; `test_next_entry_after_no_fill_waits_t_in`;
`test_haircut_blocks_fill_on_thin_bar`; `test_horizon_exit_matches_label`;
`test_single_trade_matches_label_net` (una sola BUY: `net_ret_bps` del backtest uguale a quello della label di `decision_idx`,
salvo l'effetto degli arrotondamenti agli increment, con tolleranza calcolata dagli increment);
`test_sell_void_if_bracket_fills_before_t_plus_f`; `test_daily_loss_blocks_entries_until_midnight`;
`test_exposure_guard_blocks_third_entry`; `test_cached_and_uncached_runs_are_identical`; `test_backtest_is_deterministic`.

### O3. Baseline LightGBM e `build_dataset`

**O3.1 Iperparametri: accettati, con tre aggiunte vincolanti (a).** Costante `LGBM_PARAMS` in `training/baseline_lgbm.py`,
riportata nel model card:

    boosting_type="gbdt", num_leaves=7, max_depth=3, learning_rate=0.03, num_boost_round=600,
    min_data_in_leaf=500, min_sum_hessian_in_leaf=1.0, feature_fraction=0.7, bagging_fraction=0.7, bagging_freq=1,
    lambda_l1=0.0, lambda_l2=10.0, max_bin=63, min_data_in_bin=50, seed=1234, deterministic=True, force_row_wise=True,
    num_threads=train.yml lgbm.num_threads (default 4), verbosity=-1
    early_stopping_rounds=50 sul logloss PESATO del validation del fold (first_metric_only=True)

- Aggiunta 1, normalizzazione dei pesi (obbligatoria). I pesi di unicita' valgono in media circa 0.01 (N_eff 665 su ~63 000 fill).
  Con pesi cosi' piccoli `lambda_l2 = 10` schiaccerebbe i valori delle foglie (`-G / (H + lambda)`) e il modello non imparerebbe.
  Per ogni modello e ogni insieme (train, val) si usa `w_norm = w / mean(w)` calcolato su quell'insieme.
- Aggiunta 2, `max_bin = 63`, contro l'overfitting su feature continue con pochi campioni indipendenti.
- Aggiunta 3, nessuna ricerca di iperparametri in nessun fold (ne' test ne' validation) in v1: i parametri sono fissi.
- Objective: `action` = `multiclass`, `num_class = 3`, ordine `ACTIONS` (BUY, SELL, HOLD); `edge` = `binary`;
  `vol_regime` = `multiclass`, `num_class = 5`, ordine `REGIME_LEGEND`.
- Feature: `action` usa tutte le `FLAT_COLUMNS`; `edge` e `vol_regime` usano le `FLAT_COLUMNS` senza le `pos_*` (le loro label non
  dipendono dalla posizione). Le liste e il loro sha256 si scrivono in `features.json`.

**O3.2 Target, righe e pesi.**

| Modello | Righe di train/val/test | Target | Esclusi | Span del peso di unicita' (per prodotto, fra le righe di quel modello) |
|---|---|---|---|---|
| `action` | FLAT + LONG sintetici | FLAT: BUY se UPPER e `net_ret > 0`, altrimenti HOLD (anche NO_FILL). LONG: O3.3 | UNRESOLVED | FLAT con fill `[t, exit_idx]`; NO_FILL `[t, t+F+T_in-1]`; LONG `[t, exit_idx_br]` |
| `edge` | solo FLAT | 1 se UPPER; 0 se LOWER o VERTICAL | NO_FILL, UNRESOLVED | `[t, exit_idx]` |
| `vol_regime` | solo FLAT | `forward_rv_bucket` 0..4 | bucket -1 | `[t, label_window_end(t)]` in indici di barra |

- I pesi FLAT e LONG di `action` si calcolano separatamente (con `uniqueness_weights`) e si normalizzano insieme (O3.1).
- Il modello `edge`, addestrato solo su FLAT, risponde anche negli stati LONG: senza le `pos_*` la risposta e' quella del mercato
  in `t`, che e' esattamente la semantica della domanda ("se si aprisse ora un long").
- Sottocampionamento di HOLD, solo nel train di `action`: `minority = min(n_BUY, n_SELL)`; se `n_HOLD > 3 x minority`, si estraggono
  `3 x minority` righe HOLD in modo uniforme (`numpy.random.default_rng([seed, fold])`), poi si normalizzano i pesi delle righe
  rimaste. Correzione del prior, obbligatoria perche' la temperatura da sola non corregge uno spostamento additivo di classe:
  `keep_rate = n_HOLD_tenute / n_HOLD`, e nell'inferenza `raw[:, HOLD] += ln(keep_rate)` prima della temperatura. Si salva in
  `calibration.json` come `action_hold_log_prior`. Senza sottocampionamento vale 0.0.

**O3.3 Campioni LONG sintetici: risposta a (b). Si usa lo stesso kernel delle label; `hours_held = U/60` NO.**
- Per ogni sample FLAT `t` (prodotto p): `U = rng.integers(60, H + 1)` (minuti), con `rng = numpy.random.default_rng([seed,
  product_index])` estratto nell'ordine dei sample; `t_e = t - U`.
- Ingresso fittizio = l'ordine che il runtime avrebbe davvero inviato decidendo in `t_e`: `r = triple_barrier(..., t_idx=[t_e],
  spread = spread_for(p, t_e))`. Il campione LONG si scarta se `r` e' NO_FILL o UNRESOLVED, se `fill_idx >= t` (in `t` non si e'
  ancora LONG), oppure se `exit_idx_br < t + F` (il bracket chiude prima che una SELL decisa in `t` possa arrivare sul mercato:
  la decisione non avrebbe effetto).
- `entry_px = r.entry_px`, `A_e = atr[t_e]`, `stop_loss = entry - k_sl * A_e`, `take_profit = entry + k_tp * A_e`,
  `atr_entry = A_e`, `opened_at = ts[fill_idx]` (inizio della barra di fill, la stessa convenzione del backtest). Lo stato si
  costruisce con `StateBuilder.build(position = PositionInput("LONG", ...), close_t = close[t])`. `unreal_bps`, `dist_*` e
  `hours_held` vengono quindi dalle formule di O1.5 e coincidono per costruzione con quelle del runtime.
- Target `action` LONG (nuova funzione `long_action_targets` in `labeling.py`, che usa solo i kernel di `fill_model.py` e le fee
  del `label_config`):

      V_now = market_exit_px(open[t+F], s_t, slip) * (1 - f_t/1e4)              # SELL decisa in t, eseguita a open[t+F]
      V_br  = exit_px_br * (1 - f_out_br/1e4)                                    # esito del bracket fittizio (f_m se UPPER, f_t altrimenti)
      target = SELL se V_now > V_br, altrimenti HOLD

  Generalizza la regola della 3.1.3 ("LOWER, oppure VERTICAL peggiore dell'uscita immediata") e coincide con essa salvo i casi
  limite di gap. E' la regola che il backtest premia davvero.
- Nel JSONL (LLM), le righe LONG portano anche `edge` e `vol_regime`, con i target del sample FLAT nello stesso `t` (sez. 3.1.3:
  "FLAT e LONG").

**O3.4 Calibrazione: risposta a (c), confermato, con una precisazione sul modello finale.**
- `edge`: Platt sul punteggio grezzo (logit) di LightGBM, `p = sigmoid(a * z + b)`, con `(a, b)` dal minimo della NLL pesata sul
  validation (`scipy.optimize.minimize`, BFGS, partenza (1, 0)).
- `action` e `vol_regime`: temperatura sui punteggi grezzi multiclasse (per `action` dopo la correzione del prior),
  `p = softmax(raw / T)`, con `T` dal minimo della NLL pesata (`minimize_scalar`, limiti (0.05, 10)).
- Modelli di fold: calibrati sul validation del proprio fold. Modello finale: calibrato su `val_final` (O3.6), che e' il "fold di
  validation piu' recente" della 3.1.6 e di L1. Il modello finale NON si riaddestra su train + val, perche' la calibrazione
  richiede dati non visti dal fit.
- Si accetta che lo stesso validation serva a early stopping (un solo parametro), calibrazione e soglie. Il test del fold resta
  intatto, ed e' l'unico dato usato dai gate.
- In `calibration.json`: `platt_edge {a, b}`, `T_action`, `T_vol_regime`, `action_hold_log_prior`, e per ogni modello ECE (15 bin),
  Brier e logloss pesati prima e dopo, sul validation.

**O3.5 Soglie per fold, perche' altrimenti c'e' lookahead.** I trade OOS del fold `k` (GE, E1-E5) usano il modello `k`, la
calibrazione `k` e una `policy_k.json` tarata da `tune_policy` sul validation `k`. Una sola policy tarata su `val_final` vedrebbe il
test dei fold piu' recenti. Il `policy.json` di produzione e' quello tarato su `val_final` col modello finale; l'holdout (E5, G0,
G4) si valuta con modello finale e policy finale. Se in un fold nessuna combinazione raggiunge `min_trades = 30` sul validation,
quel fold non opera (zero trade, motivo `untunable` nel report). E' la scelta conservativa, e il quant puo' rivedere `min_trades`
per i validation corti.

**O3.6 Split finale (nuova funzione in `training/splits.py`).**

    def final_fit_split(timestamps, t1, holdout_start, val_frac=0.15, embargo=DEFAULT_EMBARGO) -> tuple[np.ndarray, np.ndarray]
        # dev = sample con t < holdout_start e t1 < holdout_start - embargo
        # val_start = t[dev[0]] + (holdout_start - t[dev[0]]) * (1 - val_frac)
        # val   = dev con t >= val_start ; train = dev con t < val_start e t1 < val_start - embargo

Test: nessuna sovrapposizione fra `[t, t1]` del train e del val; il val finisce prima di `holdout_start - embargo`.

**O3.7 Cosa si salva.**
- `models/baseline/<version>/`, con `version = "baseline-lgbm@<YYYY-MM-DD>.<n>"`:
  - `fold_<k>/{action,edge,vol_regime}.txt` (`Booster.model_to_string()`), `fold_<k>/calibration.json`, `fold_<k>/policy.json`;
  - `final/` con lo stesso contenuto;
  - `features.json`, `label_config.json` (copia identica byte per byte), `model_card.json`.
- `model_card.json` (sez. 3.2.4 adattata al baseline): `model`, `backend: "baseline"`, `state_schema: "chronos.state.v1"`,
  `flat_columns_sha256`, `questions_version` (`build_questions(label).version_tag`), `label_config` (campi di G9 + `sha256`),
  `data {products, indicator_anchor{prodotto: ISO Z}, dataset_run_id, dataset_manifest_sha256, train_range, val_range,
  holdout: [holdout_start, ultima t], n_rows per modello e split}`, `sentiment_available: false`, `lgbm {params, best_iteration per
  modello e per fold/final}`, `calibration` (copia di `final/calibration.json`), `degenerate` (O3-K) per fold e per modello,
  `metrics_folds` (logloss/ECE/Brier di val e test per fold), `metrics_holdout` (lo compila `evaluate.py`), `git_commit`,
  `created_at`.
- La promozione nel `ModelRegistry` (`models/registry/<version>/`) copia `final/`, il model card e il `label_config` (P3).

**O3.8 `BaselineDecider` (`modules/decision/baseline.py`): firma.**

    class BaselineDecider:
        @classmethod
        def load(cls, model_dir: Path, *, label: LoadedLabelConfig, questions: QuestionSet,
                 allow_degenerate: bool = False) -> BaselineDecider
            # rifiuta (ValueError): label sha diverso da quello del card, questions_version diversa, flat_columns_sha256
            # diverso da quello del codice, modello marcato degenerate senza allow_degenerate (che vale True solo per i fold)
        async def decide(self, state: str, questions: Mapping[str, ChoiceQuestion | NoulQuestion | ScoreQuestion]) -> DecisionAnswers
            # questions != quelle caricate -> DeciderBadResponse ; DecisionState.model_validate_json(state) -> flatten_state
            # -> answers_batch su una riga -> DecisionAnswers (choice = argmax, score = somma i * p_i, legend, confidence 0.0:
            # la ricalcola il client). Nessun await interno, nessun thread: la predizione su una riga sta nel budget.
        def answers_batch(self, X: np.ndarray) -> AnswerArrays     # X: righe nell'ordine FLAT_COLUMNS
        # AnswerArrays: p_action (n, 3) in ordine ACTIONS, p_edge (n,), p_regime (n, 5); tutte gia' calibrate (O3.4)

- `decide` accetta la stringa canonica, come il decider LLM: il baseline vede esattamente i token arrotondati dello stato.
- Test `test_baseline_decide_matches_batch`: `decide(serialize_state(s))` coincide con `answers_batch(flatten_state(s))` per i 3
  golden.

**O3-K. Fold senza label `vol_regime` (scelta statistica: default prudente da confermare col `trader-quant`, non blocca).**
- Il dato: le label `vol_regime` esistono solo da circa il 2024-10-15 (servono 365 giorni di `RVd(360)` dopo l'ancora del
  2023-10-01). Con 5 fold su dev 2023-10..2026-06 (blocchi di circa 5.4 mesi) i train dei fold 0 e 1 non hanno label di regime, e
  quello del fold 2 ne ha poche settimane. Escludere quei fold renderebbe GE strutturalmente INCONCLUSO (al massimo 2 fold attivi).
- Default: un modello `vol_regime` di fold e' `degenerate` se il suo train ha meno di 5 000 righe o il suo val meno di 1 000. In quel
  fold la risposta di regime e' la costante `(0, 0, 1, 0, 0)` ("normal"): nessun veto e `m_reg = 1.0`. Il fold valuta solo `edge` e
  `action`, ed e' marcato nel model card e nel report.
- E' prudente perche': il veto puo' solo togliere trade; il sizing resta entro `risk_per_trade` e `max_position_frac`; il modello
  finale non puo' essere degenerato (`build` fallisce, exit 1) e `BaselineDecider.load` rifiuta un modello degenerato senza
  `allow_degenerate`.
- Il quant puo' scegliere un'alternativa (ad esempio un E4 calcolato sui soli fold con regime attivo) con una nuova risoluzione.

**O3.9 `training/build_dataset.py`.**
- CLI: `python -m training.build_dataset --config config/train.yml [--tokenizer <id|path>]`. `config/train.yml` e
  `training/config.py` (`TrainConfig`, pydantic) sono da creare: `data_root`, `products`, `label_config` (path), `app_config` (path
  dell'AppConfig per risk/fill/timeout), `datasets_dir` (`data/datasets`), `models_dir` (`models/baseline`), `seed` (1234),
  `n_folds` (5), `val_frac` (0.15), `embargo_days` (2), `sentiment_history_db` (`data/news_history.db`),
  `backtest {starting_quote "100.00", tune_decision_every_min 15, eval_decision_every_min 1}`, `lgbm {num_threads 4}`.
  Config o `label_config` non validi (anche lo `sha256`): exit 2.
- Passi, per prodotto:
  1. `load_bars(root, product)` senza `start`. `indicator_anchor` = prima `ts`, scritta in manifest e model card (token richiesti
     dal tripwire: `compute_indicators`, `indicator_anchor`).
  2. `compute_indicators` sull'intero storico. BTC per primo, perche' serve al `btc_context` degli altri prodotti (join su `ts`).
  3. Sample: `sample_indices(index, log_ret_1m, h)` con `h_i = atr_14_1h_bps[i] / 1e4`. Si tengono quelli con `atr_14_<atr_tf>`
     finito e `label_window_end(t) <= ultima ts_bar_close` del prodotto (gli UNRESOLVED escono).
  4. Label: `triple_barrier` (spread `spread_for`), `forward_rv_bucket`, LONG sintetici (O3.3).
  5. Stati: `StateBuilder.build` -> `serialize_state` -> `state_sha256`, e `flatten_state`. `sentiment`: se
     `sentiment_history_db` non esiste, `sentiment=None` in tutte le righe, `meta.sentiment_dropped = false` e
     `manifest.sentiment_available = false`. Il feature dropout del 25% (solo train, `rng([seed, 7])`) e' implementato ma agisce
     solo su sentiment non nullo, quindi oggi non fa nulla. G4 viene riportato come `passed (trivial: sentiment_available=false)`.
- Split: `purged_walk_forward(timestamps = ts_bar_close dei sample FLAT di tutti i prodotti, ordinati per (t, prodotto),
  t1 = label_window_end, holdout_start = label.holdout_start, n_folds, embargo, val_frac)` e `final_fit_split` (O3.6). Le righe LONG
  ereditano l'appartenenza del loro `t`. E' corretto perche' `exit_idx_br < label_window_end(t)` (U >= 60).
- Output in `data/datasets/<run_id>/`, con `run_id = <UTC YYYYMMDDTHHMMSSZ>-<label_sha[:8]>`:
  - `manifest.json`: run_id, `label_config_sha256`, `questions_version`, `state_schema`, `flat_columns_sha256`, products,
    `indicator_anchor`, ultima barra per prodotto, `holdout_start`, numero di righe per (modello, lato, split), `seed`,
    `sentiment_available`, `label_tokens_checked`, `git_commit`;
  - `label_config.json`: copia identica byte per byte;
  - `samples.parquet`: una riga per (prodotto, t, lato). Colonne: product, t, t_idx, position_side, state, state_sha256,
    `FLAT_COLUMNS`, action_target, edge_target, vol_target, outcome, fill_idx, exit_idx, net_ret_bps, label_window_end, w_action,
    w_edge, w_vol, fold_test (indice del fold il cui test contiene t; -1 se t e' nel blocco 0 o purgato da tutti i test; -2 se e'
    holdout), final_split (train | val | holdout | purged), e per le righe LONG t_e, entry_px, stop_loss, take_profit, atr_entry,
    opened_at, outcome_br, exit_idx_br. E' l'input di `baseline_lgbm.py`, che per il fold `k` ricava train/val/test da
    `purged_walk_forward` (deterministico) applicato a `t`.
  - `train.jsonl`, `val.jsonl`, `test.jsonl`: record della sez. 3.2.1 per lo split finale (test = holdout), un record per
    (stato, domanda). `fold` = `fold_test`; `split` = `final_split`; `options`: action `["BUY","SELL","HOLD"]`, edge
    `["true","false"]`, vol_regime `["0".."4"]`; `weight` = peso normalizzato del modello corrispondente; `meta` con
    `barrier_outcome` (per action LONG = `outcome_br`), `net_ret_bps` (int, null per NO_FILL), `fill_minute = fill_idx - t_idx`,
    `exit_minute = exit_idx - t_idx`, `atr_tf`, `atr_bps = round_bps(1e4 * A / close[t])`, `label_config_sha256`,
    `questions_version`, `sentiment_dropped`.
- Controllo dei token a singolo id: solo con `--tokenizer` (richiede il download del tokenizer HF, gratuito). Senza il flag il
  controllo si salta e si scrive `label_tokens_checked: false`; `train_lora.py` deve rifiutare un dataset con `false`.

### P. Ordine di implementazione (Sonnet)

1. F0 (`labeling.py` + docstring di `fill_model.py`) e `test_upper_exit_pays_maker_fee`.
2. O1: `spread_for`; `schemas.py`, `serializer.py` e i golden; `state_builder.py` con i test O1.6.1-3;
   `docs/decisions/2026-10-18-spread.md`; testo della spec (O1.7).
3. `splits.final_fit_split`; `labeling.long_action_targets`; `training/config.py` + `config/train.yml`; `build_dataset.py`.
4. `baseline_lgbm.py` + `modules/decision/baseline.py` (O3) e `test_baseline_decide_matches_batch`.
5. Prerequisiti del backtest: `virtual_account.py` (tripwire C11), `kill_switch.py` (tripwire), `sim_exchange.py`, `bar_gateway.py`.
6. `backtest.py` (O2) con i test O2.8; poi `tune_policy.py` (soglie per fold, O3.5); poi `evaluate.py` (D2-D6 della risoluzione
   precedente, piu' `cadence_sensitivity`).
7. Spec 2.8: oltre a O1.7, la sez. 3.5 punti 8-10 rimandano a O2/O3 di questo file; la 5.1.2 e il watchdog della 4.3 aggiungono
   l'uscita per orizzonte (O2.4); la 3.1.6 aggiunge soglie e calibrazione per fold e lo split finale (O3.4-O3.6).
8. Svuotare questo file a lavoro concluso (protocollo CLAUDE.md, punto 6).
