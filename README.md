# Chronos 🕐

**An AI-powered cryptocurrency trading agent that thinks before it trades.**

---

## What is Chronos?

Chronos is an autonomous trading bot that:
1. **Watches** the crypto market in real-time
2. **Analyzes** price patterns using AI (Transformer models)
3. **Reads** market sentiment from news
4. **Decides** when to buy or sell
5. **Executes** trades automatically with built-in risk management

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        CHRONOS AGENT                            │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│   ┌─────────────┐    ┌─────────────┐    ┌─────────────┐        │
│   │  COINBASE   │    │  NEWS/WEB   │    │   FAISS     │        │
│   │    API      │    │  SCRAPING   │    │  VECTORS    │        │
│   └──────┬──────┘    └──────┬──────┘    └──────┬──────┘        │
│          │                  │                  │               │
│          v                  v                  v               │
│   ┌─────────────────────────────────────────────────────────┐  │
│   │              DATA LAYER                                 │  │
│   │  data_fetch.py | sentiment_analysis.py | vector_storage │  │
│   └─────────────────────────┬───────────────────────────────┘  │
│                             │                                  │
│                             v                                  │
│   ┌─────────────────────────────────────────────────────────┐  │
│   │           PROCESSING LAYER                              │  │
│   │  data_preprocessing.py (clean, normalize, features)     │  │
│   └─────────────────────────┬───────────────────────────────┘  │
│                             │                                  │
│                             v                                  │
│   ┌─────────────────────────────────────────────────────────┐  │
│   │            AI/ML LAYER                                  │  │
│   │  trading_logic.py (Transformer model -> BUY/SELL/HOLD)  │  │
│   └─────────────────────────┬───────────────────────────────┘  │
│                             │                                  │
│                             v                                  │
│   ┌─────────────────────────────────────────────────────────┐  │
│   │          EXECUTION LAYER                                │  │
│   │  order_execution.py (bracket orders, stop-loss)         │  │
│   └─────────────────────────┬───────────────────────────────┘  │
│                             │                                  │
│                             v                                  │
│                      COINBASE API                              │
│                    (Real trades! 💰)                           │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## APIs Used

| API | Purpose | Module |
|-----|---------|--------|
| **Coinbase Advanced Trade API** | Fetch prices, execute trades, manage orders | `data_fetch.py`, `order_execution.py` |
| **Hugging Face Transformers** | Time-series forecasting, sentiment analysis | `trading_logic.py`, `sentiment_analysis.py` |
| **Sentence-Transformers** | Text embeddings for sentiment | `sentiment_analysis.py` |
| **FAISS (Facebook AI)** | Vector similarity search | `vector_storage.py` |
| **Web Scraping (BeautifulSoup)** | News/sentiment data collection | `sentiment_analysis.py` |

---

## How to Run (Docker Only! 🐳)

> ⚠️ **IMPORTANT:** All code execution happens inside Docker. Never run Python directly on your machine.

### 1. Setup Configuration

```bash
# Copy the template
cp config/config.yml_template config/config.yml

# Edit with your Coinbase API credentials
# NEVER commit config.yml to git!
```

### 2. Using Docker Compose (Recommended)

```bash
# Build the image
docker compose build

# Run the trading agent
docker compose up chronos

# Run in background (detached)
docker compose up -d chronos

# View logs
docker compose logs -f chronos

# Stop the agent
docker compose down
```

### 3. Run Tests

```bash
# Run all tests
docker compose run --rm test

# Run tests with verbose output
docker compose run --rm test python -m unittest discover -s tests -v

# Run a single test file
docker compose run --rm test python -m unittest tests.test_data_fetch -v
```

### 4. Interactive Development Shell

```bash
# Start a bash shell inside the container
docker compose run --rm dev

# Or with docker directly
docker compose run --rm chronos bash
```

### 5. Alternative: Direct Docker Commands

```bash
# Build
docker build -t chronos .

# Run agent
docker run --rm -v ${PWD}/config:/app/config -v ${PWD}/data:/app/data chronos

# Run tests
docker run --rm chronos python -m unittest discover -s tests -v

# Interactive shell
docker run --rm -it -v ${PWD}:/app chronos bash
```

---

## Scheduled Tasks

| Interval | Task | Description |
|----------|------|-------------|
| **Every 1 min** | Real-time trading | Fetch prices, generate signal, execute trade |
| **Every 8 hours** | Sentiment update | Scrape news, generate embeddings, store in FAISS |
| **Every 8 hours** | Historical backfill | Fetch older price data for model training |
| **Daily at 23:59** | Performance review | Calculate P&L, log metrics |

---

## Project Structure

```
chronos/
├── main.py                 # Orchestration (scheduling, coordination)
├── Dockerfile              # Container definition
├── config/
│   └── config.yml_template # Config template (copy to config.yml)
├── data/
│   ├── historical_*.csv    # Price history
│   └── realtime_*.csv      # Live data cache
├── modules/
│   ├── data_fetch.py       # Coinbase API integration
│   ├── data_preprocessing.py
│   ├── trading_logic.py    # AI brain (Transformer model)
│   ├── order_execution.py  # Trade execution
│   ├── sentiment_analysis.py
│   ├── vector_storage.py   # FAISS operations
│   ├── monitoring.py       # Logging & metrics
│   └── utils.py
├── tests/                  # Unit tests
└── script/                 # Training scripts
```

---

## The Trading Loop

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐     ┌──────────────┐
│  Fetch Data │ --> │  Preprocess  │ --> │ AI Predict  │ --> │   Execute    │
│  (Coinbase) │     │  (Normalize) │     │ (BUY/SELL)  │     │   (Order)    │
└─────────────┘     └──────────────┘     └─────────────┘     └──────────────┘
                                                                     │
                                                                     v
                                                            ┌──────────────┐
                                                            │  Monitor &   │
                                                            │    Log       │
                                                            └──────────────┘
```

---

## Risk Management

Every trade includes automatic protection:
- **Stop-Loss:** Limits downside (default: 2%)
- **Take-Profit:** Locks in gains (default: 5%)
- All parameters configurable in `config.yml`

---

## License

MIT
