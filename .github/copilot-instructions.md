# GitHub Copilot Instructions for Chronos

## Project Overview

**Chronos** is an AI-powered cryptocurrency trading agent built in Python. It integrates real-time market data, sentiment analysis, and machine learning models to generate trading signals and execute orders via the Coinbase Advanced Trade API.

### Core Stack
- **Language:** Python 3.x
- **ML/AI:** PyTorch, Transformers, Sentence-Transformers
- **Vector Storage:** FAISS
- **Data:** Pandas, NumPy, Scikit-learn
- **Scheduling:** Schedule library
- **Exchange:** Coinbase Advanced Trade SDK
- **Configuration:** YAML-based config files

---

## Architectural Commandments

### Layer Integrity (Separation of Concerns)

This project follows strict separation of concerns. **Cross-layer pollution is a critical failure.**

| Layer | Location | Responsibilities | Restrictions |
|-------|----------|------------------|--------------|
| **Orchestration** | `main.py` | Task scheduling, module coordination | NO business logic, NO direct API calls |
| **Business/Service** | `modules/trading_logic.py` | Signal generation, risk analysis, AI decisions | NO direct exchange API calls, NO raw data fetching |
| **Data Access** | `modules/data_fetch.py`, `modules/order_execution.py` | Coinbase API calls, data retrieval | NO business rules, NO signal decisions |
| **Data Processing** | `modules/data_preprocessing.py` | Cleaning, normalization, feature engineering | NO API calls, NO trading decisions |
| **AI/ML** | `modules/sentiment_analysis.py`, `modules/vector_storage.py` | NLP, embeddings, FAISS operations | NO order execution |
| **Monitoring** | `modules/monitoring.py` | Logging, performance tracking | Read-only, NO mutations |
| **Utilities** | `modules/utils.py` | Config loading, logger setup | Stateless helpers only |

### Security Integrity (CRITICAL - Financial Application)

This is a **financial trading application** handling real money. Security is paramount.

#### Zero Secrets Policy
- **NEVER** hardcode API keys, private keys, or credentials
- All secrets MUST come from `config/config.yml` (gitignored) or environment variables
- Template file `config/config.yml_template` shows structure without real values
- When generating code, use `config['coinbase']['privateKey']` pattern

#### Input Validation
- Validate ALL external data (API responses, scraped content)
- Sanitize price/quantity values before order execution
- Never trust raw market data without validation

#### Financial Safety Guards
- Always respect `risk_management.stop_loss_percent` and `take_profit_percent` from config
- Never execute orders without proper size/price validation
- Log ALL trade decisions and executions

---

## Docker-Only Execution Policy (MANDATORY)

**ALL Python code execution MUST happen inside Docker containers.** This is non-negotiable.

### Rules for AI Agents & Developers

1. **NEVER run Python directly on the host machine**
   - ❌ `python main.py`
   - ❌ `python -m unittest discover`
   - ❌ `pip install -r requirements.txt`

2. **ALWAYS use Docker commands**
   ```bash
   # Build the image
   docker build -t chronos .
   
   # Run the trading agent
   docker run --rm -v ${PWD}/config:/app/config -v ${PWD}/data:/app/data chronos
   
   # Run tests
   docker run --rm chronos python -m unittest discover -s tests
   
   # Run a specific script
   docker run --rm -v ${PWD}/config:/app/config chronos python script/train_timeseries_model.py
   
   # Interactive shell for debugging
   docker run --rm -it -v ${PWD}:/app chronos bash
   ```

3. **Docker Compose for development** (if available)
   ```bash
   docker-compose up
   docker-compose run --rm chronos python -m unittest discover
   ```

### Why Docker-Only?
- **Reproducibility:** Same environment everywhere
- **Security:** Isolated execution, no host system pollution
- **Dependency Management:** No Python version conflicts
- **Production Parity:** Dev environment matches deployment

### Volume Mounts
- `/app/config` - Configuration files (secrets)
- `/app/data` - Historical and real-time data persistence
- `/app/model_checkpoints` - Trained model weights

---

## Module-Specific Guidelines

### `modules/data_fetch.py`
- Handles Coinbase API interactions for market data
- Returns Pandas DataFrames with standardized columns
- Must handle API rate limits and connection errors gracefully

### `modules/trading_logic.py`
- Contains `SimpleTransformer` model and `TradingLogic` class
- Signal output: `"BUY"`, `"SELL"`, or `"HOLD"`
- Must never directly call exchange APIs

### `modules/order_execution.py`
- `OrderExecutor` class wraps Coinbase client
- Implements: market, limit, stop-limit, and bracket orders
- Must apply risk management parameters from config

### `modules/sentiment_analysis.py`
- Web scraping and NLP for market sentiment
- Uses Hugging Face models for classification/embeddings
- Returns sentiment scores or embeddings

### `modules/vector_storage.py`
- `FaissVectorStore` class for similarity search
- Embedding dimension: 384 (sentence-transformers default)
- Index path from `config['faiss']['index_path']`

### `modules/monitoring.py`
- `log_trade()` for trade logging
- `evaluate_performance()` for daily performance metrics
- Must be side-effect free (logging only)

---

## Code Generation Rules

### When Adding New Features

1. **Identify the target layer** before writing code
2. **Check for existing utilities** in `modules/utils.py`
3. **Follow existing patterns** in similar modules
4. **Add corresponding tests** in `tests/` directory

### Naming Conventions
- Classes: `PascalCase` (e.g., `OrderExecutor`, `FaissVectorStore`)
- Functions/methods: `snake_case` (e.g., `fetch_realtime_data`, `generate_signal`)
- Config keys: `snake_case` (e.g., `stop_loss_percent`)
- Constants: `UPPER_SNAKE_CASE`

### Error Handling Pattern
```python
from modules.utils import get_logger
logger = get_logger(__name__)

try:
    # risky operation
except SpecificException as e:
    logger.error(f"Context about failure: {e}")
    # graceful degradation or re-raise
```

### Configuration Access Pattern
```python
from modules.utils import load_config
config = load_config()

# Access nested config
api_key = config['coinbase']['privateKey']
products = config['trading']['products']
```

---

## Testing Requirements

- Every module in `modules/` must have a corresponding test in `tests/`
- Test file naming: `test_<module_name>.py`
- Use `unittest` framework (project standard)
- Mock external API calls (Coinbase, web scrapers)
- Test edge cases: empty data, API failures, invalid signals

### Running Tests
```bash
python -m unittest discover -s tests
```

---

## Prohibited Patterns

❌ **NEVER** run Python code outside of Docker containers  
❌ **NEVER** hardcode API credentials or secrets  
❌ **NEVER** place business logic in `main.py`  
❌ **NEVER** make API calls from `trading_logic.py`  
❌ **NEVER** skip input validation for financial operations  
❌ **NEVER** execute trades without risk management checks  
❌ **NEVER** leave debug print statements in production code  
❌ **NEVER** commit `config/config.yml` to version control  

---

## VAP Protocol Checklist

Before completing any task, verify:

- [ ] **Blueprint:** Identified target layer and security implications
- [ ] **Layer Integrity:** No cross-layer violations
- [ ] **Security:** No hardcoded secrets, inputs validated
- [ ] **Deduplication:** Reused existing utilities where possible
- [ ] **Testing:** Added/updated tests for new functionality
- [ ] **Purge:** Removed debug logs and dead code

---

## File Structure Reference

```
chronos/
├── main.py                    # Orchestration layer
├── config/
│   └── config.yml_template    # Config template (never commit real config)
├── data/
│   ├── historical_*.csv       # Historical price data
│   └── realtime_*.csv         # Real-time data cache
├── modules/
│   ├── data_fetch.py          # Data access layer
│   ├── data_preprocessing.py  # Data processing layer
│   ├── order_execution.py     # Exchange integration layer
│   ├── trading_logic.py       # Business logic layer
│   ├── sentiment_analysis.py  # AI/ML layer
│   ├── vector_storage.py      # AI/ML layer
│   ├── monitoring.py          # Observability layer
│   └── utils.py               # Shared utilities
├── tests/
│   └── test_*.py              # Unit tests
└── requirements.txt           # Dependencies
```
