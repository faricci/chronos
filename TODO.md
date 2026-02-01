# Chronos TODO - 2026 Simplified Vision

## 🎯 Project Goal (Simplified)

Build a **minimal, working trading agent** that:
1. Learns from historical price patterns
2. Calculates ALL costs (fees, spread) BEFORE trading
3. Only trades when profit > costs
4. Works with small orders (€5-€50 range)

---

## ✅ What's Already Working (Keep As-Is)

- [x] `modules/data_fetch.py` - Coinbase API integration for prices
- [x] `modules/data_preprocessing.py` - Normalization & indicators (RSI, MA)
- [x] `modules/utils.py` - Config loading, logging
- [x] `modules/monitoring.py` - Trade logging, performance tracking
- [x] Docker setup documented in README
- [x] Basic project structure

---

## ❌ What to REMOVE or SIMPLIFY

### Remove (Over-Engineered for Our Goal)
- [ ] **Sentiment Analysis** (`sentiment_analysis.py`) - Remove for now
  - Web scraping is fragile
  - Adds complexity without proven value for small trades
  - Can add back later if needed

- [ ] **FAISS Vector Storage** (`vector_storage.py`) - Remove for now
  - Overkill for price-based trading
  - No clear use case without sentiment

- [ ] **Hugging Face Informer model** - Too complex
  - Requires GPU/large resources
  - Hard to train and maintain
  - Replace with simpler approach

### Simplify
- [ ] **Trading Logic** - Replace Transformer with simpler strategy
  - Use proven technical indicators (RSI, MA crossover)
  - Add rule-based entry/exit conditions
  - Easier to debug and understand

---

## 🔧 Must Build (Critical for Working System)

### 1. Fee Calculator Module (NEW - CRITICAL)
**File:** `modules/fee_calculator.py`

```python
# Coinbase Advanced Trade Fees (as of 2025):
# - Maker: 0.40% (limit orders that add liquidity)
# - Taker: 0.60% (market orders that take liquidity)
# - Spread: ~0.5% typical for small caps

class FeeCalculator:
    def __init__(self, config):
        self.maker_fee = config.get('fees', {}).get('maker_percent', 0.004)  # 0.4%
        self.taker_fee = config.get('fees', {}).get('taker_percent', 0.006)  # 0.6%
        self.spread_estimate = config.get('fees', {}).get('spread_percent', 0.005)  # 0.5%

    def calculate_round_trip_cost(self, amount_eur):
        """Total cost to buy AND sell (round trip)"""
        buy_fee = amount_eur * self.taker_fee  # Usually market buy
        sell_fee = amount_eur * self.maker_fee  # Usually limit sell
        spread_cost = amount_eur * self.spread_estimate
        return buy_fee + sell_fee + spread_cost

    def minimum_profit_percent(self):
        """Minimum price increase needed to break even"""
        return self.taker_fee + self.maker_fee + self.spread_estimate

    def is_trade_profitable(self, entry_price, target_price, amount):
        """Check if expected profit exceeds all costs"""
        expected_profit = (target_price - entry_price) / entry_price
        min_required = self.minimum_profit_percent()
        return expected_profit > min_required
```

**Why Critical:** With small orders, fees eat your profit. A €10 trade with 1.5% round-trip cost needs the price to move 1.5%+ just to break even!

---

### 2. Simple Signal Strategy (Replace Complex ML)
**File:** `modules/simple_strategy.py`

Replace the Transformer model with a rule-based strategy:

```python
class SimpleStrategy:
    """
    Entry conditions (ALL must be true for BUY):
    1. RSI < 30 (oversold)
    2. Price below MA20 (discount)
    3. Expected move > fee threshold

    Exit conditions:
    1. Take profit at +3% (configurable)
    2. Stop loss at -1.5% (configurable)
    3. RSI > 70 (overbought) -> consider selling
    """
```

**Why:** 
- Transparent logic (you know why it trades)
- No training required
- Proven indicators
- Easy to backtest

---

### 3. Position Manager (NEW)
**File:** `modules/position_manager.py`

Track open positions properly:

```python
class PositionManager:
    def __init__(self, db_path="data/positions.json"):
        # Persist positions to file (survive restarts)
        pass

    def open_position(self, product_id, entry_price, size, fees_paid):
        pass

    def close_position(self, product_id, exit_price, fees_paid):
        # Calculate actual P&L including all fees
        pass

    def get_open_positions(self):
        pass

    def get_break_even_price(self, product_id):
        # entry_price + all fees = minimum exit price to not lose
        pass
```

---

### 4. Order Size Calculator (NEW)
**File:** `modules/order_sizer.py`

Smart position sizing for small accounts:

```python
class OrderSizer:
    def __init__(self, config):
        self.max_position_eur = config.get('trading', {}).get('max_position_eur', 20)
        self.max_portfolio_percent = config.get('trading', {}).get('max_portfolio_percent', 0.1)

    def calculate_order_size(self, available_balance, current_price, product_id):
        """
        Returns optimal order size considering:
        - Maximum position limit
        - Minimum order size (Coinbase minimums)
        - Available balance
        """
        pass
```

---

### 5. Update Config Template
**File:** `config/config.yml_template`

Add new sections:

```yaml
coinbase:
  base_url: "https://api.coinbase.com"
  name: ""
  privateKey: ""

trading:
  products:
    - "XLM-EUR"  # Low-price coins good for small trades
  max_position_eur: 20  # Max €20 per position
  max_open_positions: 3
  
fees:
  maker_percent: 0.004  # 0.4%
  taker_percent: 0.006  # 0.6%
  spread_percent: 0.005  # 0.5% estimated

strategy:
  rsi_oversold: 30
  rsi_overbought: 70
  take_profit_percent: 0.03  # 3%
  stop_loss_percent: 0.015   # 1.5%
  min_profit_after_fees: 0.01  # Only trade if expected profit > 1% after fees

schedule:
  check_interval_minutes: 5  # Don't need every minute
```

---

## 📋 Implementation Priority

### Phase 1: Foundation (Week 1)
1. [ ] Create `fee_calculator.py` with Coinbase fee structure
2. [ ] Create `position_manager.py` with JSON persistence
3. [ ] Update `config.yml_template` with new sections
4. [ ] Create simple test to verify fee calculations

### Phase 2: Strategy (Week 2)
5. [ ] Create `simple_strategy.py` with RSI + MA rules
6. [ ] Create `order_sizer.py` for position sizing
7. [ ] Update `trading_logic.py` to use simple strategy
8. [ ] Remove/archive sentiment and vector storage modules

### Phase 3: Integration (Week 3)
9. [ ] Update `main.py` to use new modules
10. [ ] Add backtest script (`script/backtest.py`)
11. [ ] Test with Coinbase sandbox
12. [ ] Update all unit tests

### Phase 4: Go Live (Week 4)
13. [ ] Paper trade for 1 week (log signals, don't execute)
14. [ ] Review paper trade results
15. [ ] Enable real trading with small amounts (€5-€10)
16. [ ] Monitor and tune parameters

---

## 🧮 Quick Math: Why Fees Matter

**Example: €10 XLM Trade**

| Item | Cost |
|------|------|
| Buy fee (0.6%) | €0.06 |
| Sell fee (0.4%) | €0.04 |
| Spread (~0.5%) | €0.05 |
| **Total round-trip** | **€0.15 (1.5%)** |

**To make €0.10 profit, price must increase by:**
- €0.15 (fees) + €0.10 (profit) = €0.25
- That's a **2.5% price movement** on a €10 trade

**Conclusion:** Only trade when:
- Expected price move > 2.5%
- OR increase position size to reduce fee impact %
- OR use limit orders exclusively (lower fees)

---

## 🚫 Out of Scope (For Now)

- Multi-exchange arbitrage
- Short selling
- Margin trading
- Complex ML models
- High-frequency trading
- Social sentiment
- News scraping

Keep it simple. Make it work. Iterate.

---

## 📁 Simplified Project Structure (Target)

```
chronos/
├── main.py                     # Simplified orchestration
├── Dockerfile
├── config/
│   └── config.yml_template
├── data/
│   ├── historical_*.csv
│   ├── positions.json          # NEW: Persistent positions
│   └── trade_logs/
├── modules/
│   ├── data_fetch.py           # KEEP
│   ├── data_preprocessing.py   # KEEP
│   ├── fee_calculator.py       # NEW
│   ├── simple_strategy.py      # NEW (replaces trading_logic.py)
│   ├── position_manager.py     # NEW
│   ├── order_sizer.py          # NEW
│   ├── order_execution.py      # KEEP (minor updates)
│   ├── monitoring.py           # KEEP
│   └── utils.py                # KEEP
├── script/
│   ├── backtest.py             # NEW
│   └── train_timeseries_model.py  # ARCHIVE (not needed now)
└── tests/
```

---

## ✨ Success Criteria

The system is "done" when:
1. ✅ Can calculate exact costs before any trade
2. ✅ Only opens positions when profit potential > costs
3. ✅ Tracks all positions with actual P&L
4. ✅ Works reliably with €10-€20 trades
5. ✅ Runs unattended in Docker
6. ✅ Logs everything for review
7. ✅ Can be backtested on historical data
