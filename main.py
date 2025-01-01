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
import math

logger = get_logger("MAIN")

def main():
    config = load_config()
    
    # Initialize data fetcher
    fetcher = DataFetcher(config)

    # Initialize FAISS store
    faiss_store = FaissVectorStore(
        index_path=config['faiss']['index_path'],
        embedding_dim=384
    )

    # Initialize model (example: not loading from checkpoint, just a placeholder)
    # FIXME use the same parameters as in the training script (retrieve it from config)
    model = SimpleTransformer(input_dim=6, output_dim=1)
    #model.eval()

    # Initialize trading logic
    trading_logic = TradingLogic(config, local_model_path="./model_checkpoints")

    # Coinbase Advanced Trade client for OrderExecutor
    coinbase_client = fetcher.client
    executor = OrderExecutor(config, coinbase_client)

    # Positions dictionary in-memory:
    # Example structure:
    # positions = {
    #   "DOGE-EUR": {
    #       "quantity": 0.0,
    #       "cost_basis": 0.0
    #   },
    #   ...
    # }
    # FIXME load/write from a persistent storage 
    positions = {}
    for product in config['trading']['products']:
        positions[product] = {
            "quantity": 0.0,
            "cost_basis": 0.0  # average cost basis if partial
        }

    # Backfill older data once on startup
    for product in config['trading']['products']:
        fetcher.get_historical_data_past_dates(product_id=product)

    # --------------------------
    # TASK 1: Real-Time Trading
    # --------------------------
    def trading_task():
        for product in config['trading']['products']:
            # 1) Fetch real-time data
            df = fetcher.fetch_realtime_data(product)
            signal = trading_logic.generate_signal(df)
            logger.info(f"Signal for {product}: {signal}")

            if signal not in ["BUY", "SELL"]:
                # HOLD or other => do nothing
                continue

            # 2) Check current price from df
            current_price = df['price'].iloc[-1]
            size = 5  # e.g. number of DOGE or portion of coin

            # 3) Place the bracket order
            order_id = executor.execute_order_with_risk_management(
                product_id=product,
                side=signal,
                current_price=current_price,
                size=size
            )

            # 4) Retrieve fills from Coinbase
            fills = executor.get_fills_for_order(order_id)
            if not fills:
                # Possibly the order hasn't filled yet or partial fill
                # We'll assume no fill => no PnL or capital used
                log_trade(
                    signal=signal,
                    product_id=product,
                    price=current_price,
                    size=size,
                    realized_pnl=0.0,      # not filled => 0
                    unrealized_pnl=0.0,
                    capital_used=current_price * size
                )
                continue

            # 5) Process each fill
            total_capital = 0.0
            total_pnl = 0.0
            fill_size_sum = 0.0

            for fill in fills:
                fill_price = float(fill["price"])
                fill_size  = float(fill["size"])

                # capital = price * size
                capital = fill_price * fill_size
                total_capital += capital
                fill_size_sum += fill_size

                if signal == "BUY":
                    # -- Opening / Adding a position --
                    # if we had 0 quantity, cost_basis = fill_price
                    # else compute a new cost basis if partial adding
                    old_qty = positions[product]["quantity"]
                    old_basis = positions[product]["cost_basis"]

                    new_qty = old_qty + fill_size
                    if math.isclose(new_qty, 0.0):
                        # means no position => do nothing
                        positions[product]["quantity"] = 0.0
                        positions[product]["cost_basis"] = 0.0
                    else:
                        # Weighted average cost basis
                        old_value = old_qty * old_basis
                        added_value = fill_price * fill_size
                        new_basis = (old_value + added_value) / new_qty
                        positions[product]["quantity"] = new_qty
                        positions[product]["cost_basis"] = new_basis

                    # For opening trades => realized_pnl=0
                    # You might log "unrealized" if you want
                    # This example logs no unrealized right now.
                    log_trade(
                        signal=signal,
                        product_id=product,
                        price=fill_price,
                        size=fill_size,
                        realized_pnl=0.0,
                        unrealized_pnl=0.0,
                        capital_used=capital
                    )

                elif signal == "SELL":
                    # -- Closing or partially reducing a position --
                    old_qty = positions[product]["quantity"]
                    old_basis = positions[product]["cost_basis"]
                    if old_qty <= 0.0:
                        # no open position => treat as separate logic
                        log_trade(
                            signal=signal,
                            product_id=product,
                            price=fill_price,
                            size=fill_size,
                            realized_pnl=0.0,
                            unrealized_pnl=0.0,
                            capital_used=capital
                        )
                        continue

                    # If SELL is partial or full close => realized PnL = (fill_price - cost_basis)* fill_size
                    realized_pnl = (fill_price - old_basis) * fill_size

                    # update position
                    new_qty = old_qty - fill_size
                    if new_qty < 0:
                        # Over-close => handle partial or treat as you prefer
                        # We'll treat the extra as separate logic => for now just set to 0
                        logger.warning("Sell size is bigger than open quantity. Adjusting to 0.")
                        realized_pnl = (fill_price - old_basis) * old_qty
                        new_qty = 0.0

                    positions[product]["quantity"] = new_qty
                    if math.isclose(new_qty, 0.0):
                        positions[product]["cost_basis"] = 0.0

                    log_trade(
                        signal=signal,
                        product_id=product,
                        price=fill_price,
                        size=fill_size,
                        realized_pnl=realized_pnl,
                        unrealized_pnl=0.0,   # after close => no unrealized for that portion
                        capital_used=capital
                    )

    # ---------------------------
    # TASK 2: Sentiment & FAISS
    # ---------------------------
    def sentiment_task():
        sentiment_df = get_sentiment_data()
        vectors = [emb for emb in sentiment_df['embedding']]
        meta = [{'text': txt} for txt in sentiment_df['text']]
        faiss_store.add_vectors(vectors, meta)
        faiss_store.save_index()
        logger.info("Sentiment data updated in FAISS.")

    # ---------------------------
    # TASK 3: Performance Eval
    # ---------------------------
    def performance_task():
        evaluate_performance(trigger_retrain=True)

    # Scheduling
    schedule.every(config['schedule']['real_time_interval_minutes']).minutes.do(trading_task)
    schedule.every(config['schedule']['sentiment_interval_hours']).hours.do(sentiment_task)
    schedule.every().day.at("23:59").do(performance_task)

    # Main loop
    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    main()
