<!-- Risoluzione di architetto-opus del 2026-10-17 (definizione esatta degli indicatori). La sez. 2 e' ora nella spec come 2.2.1.b; le firme (sez. 4) e i test T1-T10 (sez. 5) restano il riferimento per indicators_batch.py, indicators.py, labeling.py e state_builder.py. -->

# RISOLUZIONE ARCHITETTONICA - Definizione esatta degli indicatori

Da: architetto-opus. Per: Sviluppatore (Sonnet).
Stato: RISOLTO. Implementabile senza altre domande. Dopo aver applicato le modifiche alla spec (sez. 6) e scritto
il codice, cancella questo file (protocollo CLAUDE.md).

File letti: `docs/specs/chronos-system1-spec.md` (0.3, 0.4, 0.5, 2.2.1, 2.3 A, 3.1.3, 3.5, 4.1),
`modules/market/timeframes.py`, `modules/market/backfill.py`.

## 0. Principio guida (Decision Tokenization)

Lo stato serializzato e' il prompt: ogni token dello stato deve essere prodotto dalla **stessa funzione
deterministica** dello **stesso insieme informativo F_t** in training (batch) e in serving (incrementale).
Ne discendono quattro regole, applicate a tutte le risposte sotto:
1. nessuna barra in formazione in nessuna serie su timeframe alto (C7, nessun valore provvisorio);
2. nessun campo che lo storico OHLCV 1m non puo' ricostruire identico al live (niente train/serve skew);
3. un'unica ancora dello storico per batch e runtime (le ricorrenze Wilder/EMA dipendono dal punto di partenza);
4. arrotondamento e clamp in un solo punto (`StateBuilder`), mai negli indicatori.

## 1. Decisioni Q1-Q6

### Q1 - Point-in-time su HTF: **opzione A, confermata**
- Ogni serie su 1h/6h/1d usa solo barre HTF **chiuse e complete** (quelle emesse da `aggregate_bars` /
  `TimeframeAggregator`). La barra in formazione non entra mai in ATR, RSI, MACD, Bollinger, RV, z-score, percentile, seq.
- Regola di visibilita' (unica, da implementare come funzione): una barra HTF con inizio `B` e' visibile dalla riga 1m `i`
  se e solo se `B + tf <= t_i`, con `t_i = ts_i + 1 min` (istante di chiusura della barra 1m = istante di decisione).
  Quindi diventa visibile esattamente nella riga del suo ultimo minuto (es. barra 1d del giorno D visibile dalla riga
  `D 23:59`, cioe' `t = D+1 00:00`). E' lo stesso istante in cui `TimeframeAggregator.update` la restituisce.
- Le feature di prezzo a 1m (`ret_bps`, `dist_vwap_1d_bps`, `hh_ll_pos_30d`, numeratore di `bb_pos_1d`) usano `c_i`,
  close della barra 1m della riga `i` (chiusa a `t_i`).
- Compatibilita' label/RiskManager: la label usa `A = atr_14_<atr_tf>` della riga `t` e la scansione parte dalla riga
  `t+1` (sez. 3.1.3): nessun lookahead. Il RiskManager usa l'ATR **dello snapshot della barra della decisione**
  (trasportato con la `Decision`), non un valore riletto al momento dell'invio: anche se la barra 1d chiude tra la
  decisione e il fill, TP/SL restano calcolati con `A(t)`, come nella label. Stesso numero in label, RiskManager,
  OrderIntent (`bracket.atr`), watchdog (`atr_14_1h`) e CUSUM (`atr_14_1h_bps`).
- Costo accettato: l'ATR 1d ha fino a 24 h di ritardo. E' coerente con barriere e orizzonte di giorni (sez. 0.4) e il
  ritardo e' gia' incluso nello studio di fattibilita', che usa gli stessi valori batch.

### Q2 - Semina e warm-up: **semina SMA (stile TA-Lib), vietato `ewm` di pandas**
- Wilder(n), usato da ATR e RSI: primo valore = media aritmetica dei primi `n` input; poi
  `x_k = (x_{k-1} * (n - 1) + u_k) / n` (esattamente questa forma in batch e incrementale).
- TR: `TR_0 = H_0 - L_0`; `TR_k = max(H_k - L_k, |H_k - C_{k-1}|, |L_k - C_{k-1}|)` per `k >= 1`.
- EMA(N): primo valore all'indice `N-1` = media dei primi `N` input; poi `e_k = alpha * x_k + (1 - alpha) * e_{k-1}`,
  `alpha = 2 / (N + 1)` (esattamente questa forma).
- Output `NaN` sotto il minimo storico (tabella sez. 2); lo StateBuilder lo trasforma in `null`.
- **Ancora e warm-up (decisione aggiuntiva, bloccante per la parita'):** le ricorrenze dipendono dal punto di partenza.
  Con il warm-up attuale della spec (30 giorni di 1m) le barre 6h sono 120: scarto relativo `(13/14)^106 ~ 4e-4` rispetto
  al batch, cioe' parita' violata e skew visibile sugli interi in bps. Quindi:
  - ancora = prima riga di `load_bars(root, product)` chiamato **senza** `start`; batch e runtime partono da li';
  - `build_dataset` calcola gli indicatori sull'intero storico del prodotto e **solo dopo** seleziona i timestamp;
    vietato calcolare indicatori su uno slice;
  - `IndicatorEngine.warm_up` = replay di **tutte** le barre 1m dello storico nello stesso codice incrementale
    (`_ingest`, senza costruire snapshot). Nessun percorso di warm-up alternativo (niente HTF da `aggregate_bars`,
    niente priming parziale). Budget: <= 120 s per prodotto su 3 anni, misurato e loggato (`indicator_warmup_s`).
    Se il budget e' superato, apri un nuovo CONFLITTO: non introdurre ottimizzazioni senza parere.
  - il model card registra `data.indicator_anchor: {product: ts}`; il runtime logga un warning se la propria ancora
    differisce (non fatale: dopo ~311 barre del timeframe piu' lento la differenza e' < 1e-10 relativo).

### Q3 - Volatilita' realizzata: **rendimenti log su barre 1h chiuse, un'unica funzione `RVd` per stato e label**
- `r_k = ln(C_k / C_{k-1})` sulle barre 1h chiuse, `k >= 1`.
- `RVd(m, k) = 1e4 * sqrt( (24 / m) * fsum_{j=k-m+1..k} r_j^2 )` = volatilita' giornaliera in bps stimata sulle ultime
  `m` ore che terminano alla fine della barra 1h `k`. Definita per `k >= m`.
  - `rv_1d_bps = RVd(24, K)` (coincide con `sqrt(sum 24 r^2)` proposto);
  - `rv_30d_bps = RVd(720, K)` (coincide con `sqrt(sum 720 r^2 / 30)` proposto);
  - `rv_ratio_1d_30d = rv_1d_bps / rv_30d_bps`.
- **Label `vol_regime` (sez. 3.1.3): si', stessa funzione e stesso campionamento 1h.** Definizione esatta:
  - sia `R(E) = RVd(m_H, k)` con `m_H = H / 60` (H multiplo di 60, assert) e `E = B_k + 1h` la fine della barra `k`;
  - `E_t = ceil_to_hour(t)`, `F_t = floor_to_hour(t)` (con `t = t_i`; se `t` e' allineato all'ora, `E_t = F_t = t`);
  - valore futuro: `fwd(t) = R(E_t + H)` (rendimenti nell'intervallo `[E_t, E_t + H]`); NaN se lo storico non arriva a
    `E_t + H` -> nessuna label `vol_regime` per quel sample;
  - riferimento: `ref(t) = { R(E) : F_t - 365 d < E <= F_t }` (8760 valori, tutti su finestre **gia' concluse** a `t`:
    e' qui che C7 vale anche per la label); se uno dei 8760 valori e' NaN -> nessuna label `vol_regime`;
  - `q = np.quantile(ref, [0.2, 0.4, 0.6, 0.8], method="linear")`; `bucket = int(np.searchsorted(q, fwd, side="right"))`
    in 0..4.
  - Conseguenza: le label `vol_regime` esistono solo da ~375 giorni dopo l'ancora; le altre domande no (accettato).

### Q4 - Volume e numero di trade
- `vol_z_1d` (modificata rispetto alla proposta):
  - volume in **quote (EUR)** con prezzo tipico, stessa grandezza del numeratore del VWAP: `pv_j = (h_j + l_j + c_j)/3 * v_j`;
  - `QV_D = fsum(pv_j)` sui 1440 minuti del giorno UTC completo `D`;
  - `y_D = ln(1 + QV_D)` (log: la distribuzione del volume e' a code pesanti; lo z-score del grezzo e' dominato dai picchi);
  - `vol_z_1d = (y_D - m) / s`, con `D` = ultimo giorno chiuso visibile, `m` e `s` calcolati sui 30 giorni
    `D-30..D-1` (corrente escluso), a due passate: `m = fsum(y)/30`, `s = sqrt(fsum((y - m)^2) / 29)` (ddof=1);
    `NaN` se `s == 0`.
- `trades_z_1d`: **opzione (i), campo rimosso** da `chronos.state.v1`. Non si adotta (ii) perche' BTC-EUR ed ETH-EUR hanno
  ~98% di minuti con scambi (sez. 0.4): `active_min` e' saturo vicino a 1440, la deviazione standard e' di pochi minuti e
  lo z-score diventa rumore clampato a +-6 su 2 prodotti su 3, con semantica diversa per prodotto e informazione
  ridondante con `vol_z_1d`. Un token che e' rumore per la maggioranza dei prodotti insegna associazioni spurie.
  (iii) e' escluso per C7/skew.

### Q5 - Finestre mobili a 1m e altri campi: **proposte confermate con le correzioni sotto**
- `dist_vwap_1d_bps`: confermata; la nullita' si decide con un **contatore intero** dei minuti con `v_j > 0` nella
  finestra, mai confrontando una somma float con 0 (dopo sottrazioni incrementali resta un residuo ~1e-17).
- `hh_ll_pos_30d`: confermata (finestra 43200 minuti incluso il corrente), ma **`NaN` se `HH == LL`** (non 0.5).
- `bb_pos_1d`: confermata (SMA20 +- 2 std ddof=0 sulle chiusure 1d chiuse, numeratore con `c_i`, non clampata
  nell'indicatore); `NaN` se `std == 0`. Clamp in `[-1.0, 2.0]` nello StateBuilder.
- Regola unica dei casi degeneri: **denominatore nullo -> NaN -> null**. Nessun valore convenzionale (0.5, 50, ...),
  tranne RSI (definito sotto, e' una convenzione standard dell'indicatore e non una divisione per zero).
- `macd_hist_1d_bps`, `atr_*_bps`, `ret_bps`, `seq_*`: confermati (formule in tabella).
- `atr_1d_pctile_365d`: confermato, valore corrente **escluso** dalla finestra, `<=` per i pari merito, NaN con meno di
  90 valori precedenti.
- `seq_1h_bps` / `seq_1d_bps`: se mancano barre, l'intera lista e' `null` (mai liste parziali).

### Q6 - Tolleranza e deriva numerica
- Criterio di parita' su ogni colonna grezza (float, prima di arrotondamenti):
  `abs(a - b) <= 1e-9 * max(1.0, abs(b))` **e** maschere NaN identiche.
- Somme mobili incrementali (VWAP: `sum pv`, `sum v`): add/subtract O(1) **piu'** ricalcolo esatto con `math.fsum` sul
  deque a ogni chiusura della barra 1d (riga `23:59`). Costo O(1) ammortizzato; elimina la deriva in un processo live che
  gira per mesi, che il test su 5.000 barre non puo' osservare.
- Somme su finestre piccole (24/720 r^2, 20 chiusure, 30 y, minuti del giorno): ricalcolo completo con `math.fsum` alla
  chiusura della barra HTF che le aggiorna (O(finestra) una volta per barra HTF = O(1) ammortizzato per minuto).
- Batch: `pandas.Series.rolling(w, min_periods=w)` per le finestre a 1m; `numpy.lib.stride_tricks.sliding_window_view`
  con la stessa formula a due passate per le finestre piccole su HTF. Vietato `cumsum` globale con differenze.

## 2. Testo della nuova sez. 2.2.1.b (da inserire nella spec)

Punto di inserimento: in `docs/specs/chronos-system1-spec.md`, dopo l'ultimo punto elenco di "Note sui campi" della
sez. 2.2.1 (quello che inizia con "`sentiment: null` quando il servizio sentiment non ha dati") e prima di
"#### 2.2.2 Richiesta al decider". Inserire verbatim il blocco tra le righe `---8<---`.

---8<---
#### 2.2.1.b Definizione esatta delle feature di mercato

Unica definizione delle feature prodotte dall'`IndicatorEngine`. Costanti e nomi delle colonne in
`modules/market/indicator_defs.py`; due implementazioni: `indicators_batch.compute_indicators` (training) e
`indicators.IndicatorEngine` (runtime), verificate dal test di parita' della sez. 2.3 A.3.

Convenzioni:
- Riga `i` = barra 1m con inizio `ts_i` sulla griglia contigua di `load_bars` (barre piatte incluse). Istante di decisione
  `t_i = ts_i + 1 min`. La riga `i` usa solo barre 1m con indice `<= i`. `c_i, h_i, l_i, v_i` = OHLCV della barra 1m.
- Barre HTF (`tf` in 1h, 6h, 1d; bucket UTC di `modules/market/timeframes.py`): una barra con inizio `B` e' visibile dalla
  riga `i` se e solo se `B + tf <= t_i`. `K = K(i, tf)` = ultima barra visibile; `C_k, H_k, L_k` = OHLC della barra HTF `k`.
  La barra in formazione non entra in nessuna serie.
- `tp_j = (h_j + l_j + c_j) / 3`; `pv_j = tp_j * v_j` (quote, EUR).
- Ancora: tutte le serie partono dalla prima riga di `load_bars(root, product)` senza `start`, in batch e a runtime.
- Valore non disponibile = `NaN` nell'indicatore, `null` nello stato. Storico sotto il minimo o denominatore nullo -> `NaN`.
- Gli indicatori restituiscono float gia' in bps (non frazioni); arrotondamento e clamp solo nello `StateBuilder`.

Ricorrenze (identiche su ogni timeframe):
- `Wilder(n)`: primo valore = media dei primi `n` input; poi `x_k = (x_{k-1} * (n-1) + u_k) / n`.
- `EMA(N)`: valore all'indice `N-1` = media dei primi `N` input; poi `e_k = alpha * x_k + (1 - alpha) * e_{k-1}`, `alpha = 2/(N+1)`.
- `TR_0 = H_0 - L_0`; `TR_k = max(H_k - L_k, |H_k - C_{k-1}|, |L_k - C_{k-1}|)`; `ATR = Wilder(14)` su `TR_0..` (primo valore a `k = 13`).
- `d_k = C_k - C_{k-1}` (`k >= 1`), `G = max(d, 0)`, `P = max(-d, 0)`; `avgG = Wilder(14)` su `G_1..`, `avgP = Wilder(14)` su
  `P_1..` (primo valore a `k = 14`); `RSI = 100 - 100 / (1 + avgG/avgP)`; se `avgP == 0`: `RSI = 100` se `avgG > 0`, altrimenti `50`.
- `MACD_k = EMA12_k - EMA26_k` (da `k = 25`); `SIG = EMA(9)` sulla serie `MACD_25..` (primo valore a `k = 33`); `HIST_k = MACD_k - SIG_k`.
- `r_k = ln(C_k / C_{k-1})` su barre 1h; `RVd(m, k) = 1e4 * sqrt((24/m) * fsum(r_{k-m+1..k}^2))`, definita per `k >= m`.
  `RVd` e' l'unica funzione di volatilita' realizzata del sistema (stato e label `vol_regime`, sez. 3.1.3).

| Campo stato | Formula (riga `i`) | Minimo storico | Unita' |
|-------------|--------------------|----------------|--------|
| `clock.hour_utc` | `t_i.hour` | - | ora 0..23 |
| `clock.dow` | `t_i.weekday()` (lunedi' = 0) | - | 0..6 |
| `price.ret_bps[w]` | `1e4 * (c_i / c_{i-w} - 1)`, `w` = 15, 60, 360, 1440, 4320, 10080 min per 15m, 1h, 6h, 1d, 3d, 7d | `w + 1` barre 1m | bps |
| `price.seq_1h_bps` | `[1e4 * (C_k / C_{k-1} - 1) for k in K-11..K]`, 1h, dal piu' vecchio | 13 barre 1h chiuse | bps x 12 |
| `price.seq_1d_bps` | come sopra su 1d, `k in K-6..K` | 8 barre 1d chiuse | bps x 7 |
| `price.dist_vwap_1d_bps` | `VWAP = fsum(pv_j) / fsum(v_j)`, `j = i-1439..i`; `1e4 * (c_i / VWAP - 1)`; NaN se nessun `v_j > 0` (contatore intero) | 1440 barre 1m | bps |
| `price.bb_pos_1d` | su `C_{K-19..K}` 1d: `m = fsum/20`, `sd = sqrt(fsum((C - m)^2)/20)`; `(c_i - (m - 2 sd)) / (4 sd)`; NaN se `sd == 0`; non clampato | 20 barre 1d | rapporto |
| `price.hh_ll_pos_30d` | `HH = max h_j`, `LL = min l_j`, `j = i-43199..i`; `(c_i - LL) / (HH - LL)`; NaN se `HH == LL` | 43200 barre 1m | rapporto in [0, 1] |
| `momentum.rsi_14_<tf>` (1h, 6h, 1d) | `RSI_K` | 15 barre `tf` | 0..100 |
| `momentum.macd_hist_1d_bps` | `1e4 * HIST_K / C_K` su 1d | 34 barre 1d | bps |
| `volatility.atr_14_<tf>_bps` (1h, 6h, 1d) | `1e4 * ATR_K / C_K` | 14 barre `tf` | bps |
| `volatility.rv_1d_bps` | `RVd(24, K)` su 1h | 25 barre 1h | bps/giorno |
| `volatility.rv_ratio_1d_30d` | `RVd(24, K) / RVd(720, K)`; NaN se il denominatore e' 0 | 721 barre 1h | rapporto |
| `volatility.atr_1d_pctile_365d` | `a_k = atr_14_1d_bps` della barra 1d `k`; `Prev = {a_{K-365..K-1}}` disponibili, `N = len(Prev)`; `100 * #{p in Prev : p <= a_K} / N`; NaN se `N < 90` | 104 barre 1d | 0..100 |
| `liquidity.vol_z_1d` | `QV_D = fsum(pv_j)` sui minuti del giorno 1d `D`; `y_D = ln(1 + QV_D)`; `(y_K - m) / s`, `m`, `s` (ddof=1, due passate) su `y_{K-30..K-1}`; NaN se `s == 0` | 31 barre 1d | z-score |
| `liquidity.spread_bps` | non prodotto dall'`IndicatorEngine` (LocalOrderBook live) | - | bps |
| `btc_context.ret_1d_bps`, `btc_context.rv_ratio_1d_30d` | stesse formule sulla serie BTC-EUR alla stessa riga `ts_i`; blocco `null` per `BTC-*` o se la riga BTC manca | come sopra | come sopra |

Colonne esportate non presenti nello stato (consumatori: label, RiskManager, watchdog, CUSUM, feasibility):
`atr_14_1h`, `atr_14_6h`, `atr_14_1d` = `ATR_K` in unita' di prezzo (quote); `rv_30d_bps = RVd(720, K)`.
Label e RiskManager usano sempre l'ATR in prezzo della riga della decisione: `upper = entry + k_tp * atr_14_<atr_tf>`,
mai `bps * prezzo`.

Visibilita' (C7): ogni valore della riga `i` e' funzione delle sole barre 1m `<= i`. Test di troncamento obbligatorio
(sez. 2.3 A.3): `compute_indicators(bars[:i+1])` all'ultima riga coincide con `compute_indicators(bars)` alla riga `i`.
---8<---

## 3. Modifiche a `DecisionState` (`modules/features/schemas.py`)

| Blocco | Modifica |
|--------|----------|
| `liquidity` | **rimosso** `trades_z_1d`. Il blocco diventa `{"spread_bps": int, "vol_z_1d": float}`. |
| tutti gli altri | nessun campo rinominato o aggiunto; semantica fissata dalla sez. 2.2.1.b. |

- Identificatore di schema invariato: `"v": "chronos.state.v1"` (nessun modello e nessun dataset prodotti finora). Se
  esistono gia' `tests/golden/state_*.json` o fixture con `trades_z_1d`, rigenerali; aggiorna `flatten_state()`.
- StateBuilder (sez. 2.3 B.2): gli indicatori sono gia' in bps, quindi conversione `int(round(x))` (non `x * 10_000`);
  `NaN -> None`. Clamp: `ret_bps`, `seq_1h_bps`, `seq_1d_bps`, `dist_vwap_1d_bps`, `macd_hist_1d_bps` in `[-5000, 5000]`;
  `vol_z_1d` in `[-6, 6]`; `bb_pos_1d` in `[-1.0, 2.0]`; `hh_ll_pos_30d` e rapporti con `round(x, 2)`.

## 4. Funzioni e firme

### 4.1 `modules/market/indicator_defs.py` (nuovo, condiviso, nessuna logica oltre alle costanti)
```python
RET_WINDOWS_MIN: Final[dict[str, int]] = {"15m": 15, "1h": 60, "6h": 360, "1d": 1440, "3d": 4320, "7d": 10080}
ATR_N: Final = 14; RSI_N: Final = 14; MACD_FAST: Final = 12; MACD_SLOW: Final = 26; MACD_SIGNAL: Final = 9
BB_N: Final = 20; BB_K: Final = 2.0
VWAP_WIN_MIN: Final = 1440; HHLL_WIN_MIN: Final = 43200
RV_1D_BARS: Final = 24; RV_30D_BARS: Final = 720
VOLZ_DAYS: Final = 30; PCTILE_DAYS: Final = 365; PCTILE_MIN_N: Final = 90
SEQ_1H_LEN: Final = 12; SEQ_1D_LEN: Final = 7
FEATURE_COLUMNS: Final[tuple[str, ...]]   # ordine fisso, vedi sotto
```
`FEATURE_COLUMNS` = `ret_15m_bps, ret_1h_bps, ret_6h_bps, ret_1d_bps, ret_3d_bps, ret_7d_bps, seq_1h_bps_00..seq_1h_bps_11,
seq_1d_bps_0..seq_1d_bps_6, dist_vwap_1d_bps, bb_pos_1d, hh_ll_pos_30d, rsi_14_1h, rsi_14_6h, rsi_14_1d, macd_hist_1d_bps,
atr_14_1h, atr_14_6h, atr_14_1d, atr_14_1h_bps, atr_14_6h_bps, atr_14_1d_bps, rv_1d_bps, rv_30d_bps, rv_ratio_1d_30d,
atr_1d_pctile_365d, vol_z_1d`.

### 4.2 `modules/market/indicators_batch.py`
```python
def compute_indicators(bars_1m: pd.DataFrame) -> pd.DataFrame:
    """Input: frame contiguo di load_bars (indice ts UTC, OHLCV). Output: stesso indice, colonne FEATURE_COLUMNS,
    float64, NaN = non disponibile. Solleva BarGapError se l'input non e' contiguo."""
def htf_asof(htf: pd.DataFrame, tf: str, index_1m: pd.DatetimeIndex) -> pd.DataFrame:
    """Join point-in-time: il valore della barra B e' assegnato dalla riga ts = B + tf - 1min in avanti (ffill)."""
def wilder(u: np.ndarray, n: int) -> np.ndarray
def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray
def wilder_atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int = ATR_N) -> np.ndarray
def wilder_rsi(close: np.ndarray, n: int = RSI_N) -> np.ndarray
def ema_sma_seed(x: np.ndarray, n: int) -> np.ndarray          # NaN iniziali in x ammessi: seme sui primi n non-NaN
def macd_hist(close: np.ndarray, fast: int = MACD_FAST, slow: int = MACD_SLOW, signal: int = MACD_SIGNAL) -> np.ndarray
def rv_per_day_bps(close_1h: np.ndarray, m: int) -> np.ndarray  # RVd(m, k) per ogni barra 1h k
def pctile_prev(x: np.ndarray, window: int = PCTILE_DAYS, min_n: int = PCTILE_MIN_N) -> np.ndarray
def zscore_prev(y: np.ndarray, window: int = VOLZ_DAYS) -> np.ndarray
```
Le ricorrenze (`wilder`, `ema_sma_seed`) possono usare `numba.njit`; nessuna dipendenza da `pandas.ewm`.
`rv_per_day_bps` e' importata da `training/labeling.py` per `forward_rv_bucket` (nessuna reimplementazione).

### 4.3 `modules/market/indicators.py`
```python
@dataclass(frozen=True, slots=True)
class IndicatorSnapshot:
    product_id: str
    ts: datetime                        # inizio della barra 1m
    t: datetime                         # ts + 1 min, istante di decisione
    # un campo float per ogni colonna scalare di FEATURE_COLUMNS (NaN = non disponibile)
    seq_1h_bps: tuple[float, ...] | None
    seq_1d_bps: tuple[float, ...] | None
    def as_row(self) -> dict[str, float]: ...                  # chiavi = FEATURE_COLUMNS, seq appiattite
    @classmethod
    def from_row(cls, product_id: str, ts: datetime, row: Mapping[str, float]) -> IndicatorSnapshot: ...

class WilderState:      def __init__(self, n: int) -> None; def update(self, u: float) -> float
class EmaState:         def __init__(self, n: int) -> None; def update(self, x: float) -> float
class RollingSum:       def __init__(self, window: int) -> None; def update(self, x: float) -> float; def resum(self) -> None
class RollingExtreme:   def __init__(self, window: int, mode: Literal["max", "min"]) -> None; def update(self, i: int, x: float) -> float
class PrevPercentile:   def __init__(self, window: int, min_n: int) -> None; def update(self, x: float) -> float  # SortedList
class PrevZScore:       def __init__(self, window: int) -> None; def update(self, y: float) -> float

class IndicatorEngine:
    def __init__(self, product_id: str) -> None
    def warm_up(self, bars_1m: pd.DataFrame) -> None           # replay completo via _ingest, nessuno snapshot
    def update(self, bar: Bar) -> IndicatorSnapshot            # _ingest(bar) + snapshot()
    def snapshot(self) -> IndicatorSnapshot
    def _ingest(self, bar: Bar) -> None
```
- Le barre HTF arrivano solo da `TimeframeAggregator` (`timeframes.py`, invariato). `QV_D` si accumula nell'engine
  (lista dei `pv` del giorno aperto, `fsum` quando il `TimeframeAggregator("1d")` restituisce la barra).
- `PrevPercentile.update(x)`: calcola `bisect_right(prev, x)` sulla finestra **prima** di inserire `x`, poi inserisce e
  sfratta il piu' vecchio oltre 365. Stessa logica (esclusione del corrente) in `PrevZScore`.
- Lo `StateBuilder` riceve sempre un `IndicatorSnapshot` (runtime: da `update`; training: da `from_row`), cosi' la
  conversione verso lo stato ha un solo percorso.

### 4.4 A valle (contratti, non riscrivere ora)
- `training/labeling.py`: `triple_barrier(..., atr=frame["atr_14_<atr_tf>"].to_numpy(), ...)`;
  `forward_rv_bucket(close_1h: pd.Series, t: pd.DatetimeIndex, horizon_min: int) -> np.ndarray` (int8, `-1` = nessuna
  label) con la definizione della Q3; sample con `atr_14_<atr_tf>` NaN esclusi da tutte le domande.
- `modules/risk/risk_manager.py`: `A` = `snapshot.atr_14_<atr_tf>` della `Decision`.

## 5. Test di parita' obbligatori (`tests/test_indicators_parity.py` salvo diversa indicazione)

| ID | Test | Dati | Criterio |
|----|------|------|----------|
| T1 | `test_parity_synthetic` | serie sintetica deterministica (seed fisso) di 200 giorni: random walk, tratti di barre piatte, un giorno intero a volume 0, un tratto a prezzo costante | `IndicatorEngine.update` dalla riga 0 vs `compute_indicators`: tutte le righe, tutte le colonne, tolleranza Q6, maschere NaN identiche. Sempre in CI. |
| T2 | `test_parity_history[product]` per BTC-EUR, ETH-EUR, SOL-EUR | Parquet reale (`@pytest.mark.data`, skip se assente) | `warm_up(bars[:-N])` + `update` sulle ultime `N = 20_000` righe vs `compute_indicators(bars)` sulle stesse righe; tolleranza Q6; assert che almeno 5.000 righe abbiano **tutte** le colonne non-NaN (criterio P0). Logga `indicator_warmup_s`. |
| T3 | `test_no_lookahead_truncation` | sintetica T1 | per 50 righe `i` (incluse 23:58, 23:59, 00:00, 05:59, 06:00, xx:59): `compute_indicators(bars[:i+1]).iloc[-1]` == `compute_indicators(bars).iloc[i]` (tolleranza Q6). |
| T4 | `test_htf_visibility_boundary` | sintetica | `atr_14_1d` alla riga `D 23:58` = valore alla riga `D-1 23:59`; cambia alla riga `D 23:59`. Idem per 1h alla riga `xx:59` e 6h alla riga `05:59`. |
| T5 | `test_reference_values` | serie scritte a mano | ATR primo valore a `k = 13` = media dei 14 TR con `TR_0 = H-L`; RSI a `k = 14`; EMA seme SMA; primo HIST a `k = 33`; percentile con corrente escluso e `<=`; z con ddof=1; `RVd(24)` = `sqrt(sum r^2)`. |
| T6 | `test_degenerate_cases` | sintetica | finestra 1440 a volume 0 -> `dist_vwap_1d_bps` NaN; prezzo costante -> RSI 50, `bb_pos_1d` NaN, `hh_ll_pos_30d` NaN, `rv_ratio_1d_30d` NaN; `s == 0` -> `vol_z_1d` NaN. |
| T7 | `test_warmup_equals_stream` | sintetica | `warm_up(bars[:P])` + `update(bars[P:])` == `update` di tutte le righe da 0 (stesse righe finali, tolleranza Q6). |
| T8 | `tests/test_labeling.py::test_label_atr_is_snapshot_atr` | sintetica | l'`A` usato da `triple_barrier` alla riga `t` == `IndicatorEngine` snapshot `atr_14_<atr_tf>` alla stessa riga. |
| T9 | `tests/test_labeling.py::test_vol_regime_point_in_time` | sintetica | quantili `ref(t)` invariati troncando lo storico a `F_t`; `fwd(t)` == `rv_per_day_bps(close_1h, H/60)` alla barra che termina in `E_t + H`. |
| T10 | `tests/test_state_parity.py` | come T2 | `serialize_state(StateBuilder(from_row(batch)))` vs `serialize_state(StateBuilder(snapshot))`: identici su >= 99.99% delle righe; nelle restanti, solo campi interi con differenza di 1 (bordo di arrotondamento). |

## 6. Modifiche alla spec da applicare (a cura di Sonnet, insieme al codice)

1. Intestazione: `Versione: 2.5-draft`. Dopo il blocco "Changelog 2.4" aggiungi:
   "Changelog 2.5 (definizione esatta delle feature): nuova sez. 2.2.1.b (formule, minimi storici, unita'; serie HTF solo
   su barre chiuse; ancora unica dello storico); rimosso `liquidity.trades_z_1d` (non ricostruibile dallo storico OHLCV);
   `vol_z_1d` su `ln(1 + quote volume)`; warm-up del runtime = replay completo (2.3 A.4); label `vol_regime` con la stessa
   `RVd` dello stato e quantili su finestre concluse (3.1.3)."
2. Sez. 2.2.1, JSON "pretty": `"liquidity": {"spread_bps": 3, "vol_z_1d": 0.6},`.
3. Sez. 2.2.1.b: inserire il blocco della sez. 2 di questo file nel punto indicato.
4. Sez. 2.3 A.3: sostituisci "realized vol = `sqrt(sum(r^2))` su finestra, z-score rolling di volume e numero di trade
   (finestra 30 giorni sulle barre 1d), percentile dell'ATR 1d su 365 giorni (`sortedcontainers.SortedList` con finestra
   scorrevole) e i campi di prezzo dello stato della sez. 2.2.1" con "volatilita' realizzata `RVd`, z-score di
   `ln(1 + quote volume)` giornaliero (30 giorni), percentile dell'ATR 1d su 365 giorni (`sortedcontainers.SortedList`) e
   i campi di prezzo, con le definizioni esatte della sez. 2.2.1.b"; e "(tolleranza 1e-9)" con "(tolleranza
   `|a-b| <= 1e-9 * max(1, |b|)`, maschere NaN identiche; test T1-T10 della risoluzione indicatori)".
5. Sez. 2.3 A.4: sostituisci l'intero punto con "All'avvio, warm-up: `load_bars` sull'intero storico del prodotto
   (ancora della sez. 2.2.1.b) e replay di tutte le barre 1m nello stesso `IndicatorEngine` (`warm_up`, senza snapshot).
   Un warm-up parziale non e' ammesso: le ricorrenze di Wilder ed EMA dipendono dal punto di partenza. Budget <= 120 s per
   prodotto. Se il Parquet non copre il periodo, il processo esegue prima il backfill incrementale (sez. 0.5) e solo dopo
   apre gli stream."
6. Sez. 2.3 B.2: sostituisci "Conversione in bps: `int(round(x * 10_000))`. Clamp dei valori estremi (`ret_bps` in
   [-5000, 5000], z-score in [-6, 6])" con il testo dello StateBuilder della sez. 3 di questo file.
7. Sez. 3.1.3, riga `vol_regime` della tabella delle label: "Bucket 0..4 di `fwd(t) = R(E_t + H)` rispetto ai quantili
   20/40/60/80 (`np.quantile`, `method="linear"`) di `ref(t) = {R(E) : F_t - 365 d < E <= F_t}`, con `R = RVd(H/60, .)`
   della sez. 2.2.1.b, `E_t = ceil_to_hour(t)`, `F_t = floor_to_hour(t)`; `bucket = searchsorted(q, fwd, side="right")`.
   Nessuna label se `fwd` o uno dei valori di `ref` e' NaN."
8. Sez. 3.5 punto 1: dopo `forward_rv_bucket(...)` aggiungi "con la definizione della sez. 3.1.3, usando
   `indicators_batch.rv_per_day_bps`".
9. Sez. 3.5 punto 4: sostituisci "calcola indicatori con `modules.market.indicators_batch`" con "calcola indicatori con
   `modules.market.indicators_batch.compute_indicators` sull'intero storico del prodotto (ancora sez. 2.2.1.b), poi
   seleziona i timestamp; sample con `atr_14_<atr_tf>` NaN esclusi".
10. Sez. 4.1, passo 3: "A = `atr_14_<atr_tf>` (in prezzo) dello snapshot della barra della decisione, lo stesso valore
    usato dalla label; mai riletto al momento dell'invio".
11. Sez. 3.2.4 model card: aggiungi in `data` il campo `"indicator_anchor": {"BTC-EUR": "...", ...}`.

## 7. Punti fuori perimetro (non bloccano `indicators_batch.py` / `indicators.py`)

- **O1 - `liquidity.spread_bps` e blocco `costs` in training.** Lo storico non ha spread: in training lo stato
  riceverebbe la mediana per prodotto e ora (sez. 3.1.3), a runtime lo spread istantaneo del `LocalOrderBook`. E' lo
  stesso tipo di skew di `trades_z_1d`. Non decido ora perche' tocca StateBuilder, CostModel e recorder: aprire un
  CONFLITTO dedicato prima di implementare `state_builder.py`.
- **O2 - ATR del probe.** Come per i costi (changelog 2.3), `script/probe_coinbase.py` dovra' importare
  `indicators_batch.wilder_atr` invece di una formula propria, cosi' la tabella della sez. 0.4 usa la stessa definizione.
  Da fare dopo che `indicators_batch.py` e' verde; non blocca P0.
