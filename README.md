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
faiss-cpu
requests
beautifulsoup4
schedule
pyyaml
coinbase-advanced-trade
```
Then run:
```
pip install -r requirements.txt
```

### Set up config:
Update `config/config.yml` with your actual Coinbase API credentials. Customize your product list (BTC-USD, ETH-USD, etc.) and intervals.

### Testing:
```
python -m unittest discover -s tests
```

### Run:
```
python main.py
```

The script will:
- **Every minute**: Fetch real-time data, generate trading signals, possibly place orders.
- **Every 8 hours**: Scrape sentiment, store embeddings in FAISS, fetch historical data.
- **Every day at 23:59**: Evaluate performance.
