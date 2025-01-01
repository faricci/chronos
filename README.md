# Project Structure

### `config/`
Store all configuration details (API keys, trading parameters, etc.) in a `config.yml` or `.env` file.

### `data/`
This is where you keep any local CSV files for historical data, or scrapes for sentiment data. Over time, you can push these to a more robust data storage system (e.g., S3, database).

### `models/`
Python files containing your model definitions (e.g., custom PyTorch Transformer for time series or a fine-tuned sentiment classifier).

### `modules/`
Each Python file in `modules/` corresponds to a specific step in your pipeline:
- `data_fetch.py`: real-time / historical data fetching
- `data_preprocessing.py`: data cleaning, normalization, feature engineering
- `sentiment_analysis.py`: scraping forums/news, using a Hugging Face model to classify or embed text
- `vector_storage.py`: FAISS vector database interactions
- `trading_logic.py`: AI analysis, signal generation, risk management
- `order_execution.py`: placing orders via Coinbase Advanced Trade SDK
- `monitoring.py`: performance tracking, logs
- `utils.py`: any helper functions, shared code

### `tests/`
Contains unit tests for your modules. Each module should have a corresponding test file (e.g., `test_data_fetch.py`, `test_data_preprocessing.py`). Use a framework like `unittest` or `pytest` to write and run your tests.

### `main.py`
The central file that ties all modules together. Schedules tasks, runs main loops, and orchestrates the end-to-end process.

### `requirements.txt`
Python packages needed (e.g., `pandas`, `torch`, `transformers`, `faiss-cpu`, `langchain`, `coinbase-advanced-trade`, etc.).

### `Dockerfile`
For containerizing your project (optional but recommended for deployment).

### `README.md`
Explains how to set up and run your project.

# How to Run

### Install dependencies:
In your `requirements.txt`, you might have:
```
pandas
numpy
scikit-learn
torch
transformers
langchain
langchain-community
faiss-cpu
requests
beautifulsoup4
schedule
pyyaml
coinbase-advanced-trade
sentence-transformers
transformers
accelerate 
datasets
```
Then run:
```
pip install -r requirements.txt
```

### Set up config:
Rename the `config/config.yml_template` to `config/config.yml`
Update `config/config.yml` with your actual Coinbase API credentials. Customize your product list (BTC-USD, ETH-USD, etc.) and intervals.

### Testing:
```
python -m unittest discover -s tests
```
or for single class you can use:
```
python -m unittest tests.test_data_fetch
python -m unittest tests.test_data_preprocessing
python -m unittest tests.test_sentiment_analysis.py
python -m unittest tests.test_vector_storage.py
python -m unittest tests.test_order_execution.py
python -m unittest tests.test_monitoring.py
python -m unittest tests/test_trading_logic.py
```

### Run:
```
python main.py
```

The script will:
- **Every minute**: Fetch real-time data, generate trading signals, possibly place orders.
- **Every 8 hours**: Scrape sentiment, store embeddings in FAISS, fetch historical data.
- **Every day at 23:59**: Evaluate performance.


# Workflow

Below is a **high-level** overview of how your updated AI trading agent works, from **data collection** through to **trade execution** and **continuous improvement**. This summary is intended to be **clear and straightforward**, avoiding deep technical jargon.

---

## 1. Data Collection and Storage
- **Real-Time Data**: The agent pulls up-to-the-minute prices and volumes (e.g., from Coinbase).  
- **Historical Data**: The agent fetches past market data at regular intervals and appends it to local files (CSV). It also “backfills” older data so you have a continuous record from past to present.

## 2. Sentiment Analysis (Optional Enhancement)
- Periodically, the agent scrapes news and social media (or any relevant text sources).  
- It converts this text into numerical “embeddings” (think of them as “topics in numbers”) and optionally stores them for quick retrieval in a **Vector Database**.  
- Sentiment scores can also be included as an extra factor in the trading decision.

## 3. Time-Series Forecasting (Hugging Face Transformer)
- The agent uses a **Transformer** model (e.g., “Informer” or “Autoformer”) from Hugging Face, which is specifically designed for time-series forecasting.  
- This model looks at a “window” of the most recent historical data (e.g., last 64 timesteps) and predicts the next few values.  
- The model is **fine-tuned periodically** with new data to keep it accurate, saving these updates locally or to a Hugging Face model repository.

## 4. Trading Logic (Generate BUY/SELL Signals)
- **Sliding Window**: The latest chunk of historical + real-time data (e.g., last 64 data points) is fed into the time-series model.  
- **Forecast Output**: The model predicts the next 1 or more future steps.  
- **Signal Decision**: If the predicted future price is sufficiently **above** a threshold, the agent signals **BUY**; if it’s sufficiently **below** a threshold, it signals **SELL**; otherwise, it signals **HOLD**.

## 5. Order Execution with Risk Management
- When a BUY or SELL signal is triggered, the agent creates **bracket orders**:  
  1. **Stop-Loss** to limit losses if the price drops.  
  2. **Take-Profit** to automatically close a trade once it hits a target profit.  
- These orders are placed through the trading platform API (Coinbase, etc.) using an **OrderExecutor** class.

## 6. Scheduling & Automation
- A simple scheduler (e.g., `schedule` or cron) runs tasks at set intervals:  
  - **Real-Time Trading**: Every minute, fetch data, get a forecast, place trades if needed.  
  - **Historical Updates**: Every 8 hours, backfill older data so the agent has a complete price history.  
  - **Sentiment Data Updates**: Every 8 hours, gather new text data for embeddings (optional).  
  - **Performance Evaluation**: Daily or weekly, check profit/loss, logs, and metrics like ROI or drawdown.

## 7. Performance Tracking and Continuous Improvement
- Each trade is logged, including price, size, profit/loss.  
- Periodically, the agent:
  1. Evaluates overall performance metrics (win/loss, total profit, etc.).  
  2. **Retrains** the Transformer model with the newest market data if performance declines or enough new data arrives.  
  3. Resaves the updated model so the next real-time forecast uses the latest knowledge.

---

### In Short
1. **Collect** data (real-time + historical).  
2. **Preprocess** (optionally add sentiment).  
3. **Forecast** future prices with a **Transformer** model (updated regularly).  
4. **Decide** on trades (BUY/SELL/HOLD).  
5. **Execute** orders with built-in risk management.  
6. **Monitor & Retrain** to keep the model aligned with market changes.

The key idea: **Use the most recent data to forecast short-term price moves, then automatically place trades (with risk controls) based on those forecasts.** This workflow runs automatically, logs performance, and periodically improves the model to adapt to changing market conditions.