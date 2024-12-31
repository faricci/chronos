import schedule
import time
from datetime import datetime, timedelta

from modules.utils import load_config, get_logger
from modules.data_fetch import DataFetcher
from modules.sentiment_analysis import get_sentiment_data
from modules.vector_storage import FaissVectorStore
from modules.trading_logic import SimpleTransformer, TradingLogic
from modules.order_execution import OrderExecutor
from modules.monitoring import log_trade, evaluate_performance

import torch

logger = get_logger("MAIN")

def main():
    config = load_config()
    
    # Initialize data fetcher
    fetcher = DataFetcher(config)    

    # Initialize FAISS store
    faiss_store = FaissVectorStore(
        index_path=config['faiss']['index_path'],
        embedding_dim=384  # must match your embedding model
    )

    # Initialize model
    # For demonstration, we are not loading pretrained weights
    model = SimpleTransformer()
    model.eval()  # set to eval mode

    # Initialize trading logic
    trading_logic = TradingLogic(config, model)

    # Initialize coinbase client for OrderExecutor
    coinbase_client = fetcher.client
    executor = OrderExecutor(config, coinbase_client)

    # Backfill older data once on startup
    for product in config['trading']['products']:
        fetcher.get_historical_data_past_dates(product_id=product)

    # Task 1: Real-Time Trading (runs every minute)
    def trading_task():
        for product in config['trading']['products']:
            # 1) Fetch real-time data
            df = fetcher.fetch_realtime_data(product)

            # 2) Generate signal
            signal = trading_logic.generate_signal(df)
            logger.info(f"Signal for {product}: {signal}")

            # 3) Check current market price
            current_price = df['price'].iloc[-1]

            if signal == "BUY":
                size = 5
                order_id = executor.execute_order_with_risk_management(
                    product_id=product,
                    side="BUY",
                    current_price=current_price,
                    size=size
                )
                log_trade("BUY with bracket", product, current_price, size)

            elif signal == "SELL":
                # NEW SELL LOGIC
                size = 5
                order_id = executor.execute_order_with_risk_management(
                    product_id=product,
                    side="SELL",
                    current_price=current_price,
                    size=size
                )
                log_trade("SELL with bracket", product, current_price, size)

            # else: HOLD => do nothing


    # Task 2: Fetch Sentiment Data and Store in FAISS (runs every 8 hours)
    def sentiment_task():
        sentiment_df = get_sentiment_data()
        # Add to FAISS
        vectors = [emb for emb in sentiment_df['embedding']]
        meta = [{'text': txt} for txt in sentiment_df['text']]
        faiss_store.add_vectors(vectors, meta)
        faiss_store.save_index()
        logger.info("Sentiment data updated in FAISS.")

    # Task 3: Fetch Historical Data (runs every 8 hours)
    def historical_task():
        # Example: last 24 hours
        end = datetime.utcnow()
        start = end - timedelta(hours=8)
        for product in config['trading']['products']:
            df = fetcher.fetch_historical_data(product, start, end)
            # Save or do something with df
            path = f"data/{product}_historical_data.csv"
            df.to_csv(path, index=False)
        logger.info("Historical data fetch completed.")

    # Task 4: Evaluate Performance (daily, for instance)
    def performance_task():
        evaluate_performance()

    # Schedule tasks
    schedule.every(config['schedule']['real_time_interval_minutes']).minutes.do(trading_task)
    schedule.every(config['schedule']['sentiment_interval_hours']).hours.do(sentiment_task)
    #FIXME obsolete
    #schedule.every(config['schedule']['sentiment_interval_hours']).hours.do(historical_task)
    #to schedule get historical data every 8 hours
    #schedule.every(config['schedule']['historical_interval_hours']).hours.do(
    #    lambda: [fetcher.get_historical_data_past_dates(p) for p in config['trading']['products']]
    #)

    schedule.every().day.at("23:59").do(performance_task)    

    # Main loop
    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    main()
