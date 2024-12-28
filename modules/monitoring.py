import pandas as pd
import os
from modules.utils import get_logger

logger = get_logger(__name__)

def log_trade(signal, product_id, price, size):
    """
    Append a row to CSV or database.
    """
    row = {
        "timestamp": pd.Timestamp.now(),
        "signal": signal,
        "product_id": product_id,
        "price": price,
        "size": size
    }
    log_path = "data/trade_log.csv"
    if not os.path.exists(log_path):
        pd.DataFrame([row]).to_csv(log_path, index=False)
    else:
        df = pd.read_csv(log_path)
        df = df.append(row, ignore_index=True)
        df.to_csv(log_path, index=False)
    logger.info(f"Trade logged: {row}")

def evaluate_performance():
    """
    Reads the trade_log and calculates PnL or other metrics.
    """
    log_path = "data/trade_log.csv"
    if not os.path.exists(log_path):
        logger.info("No trades to evaluate yet.")
        return
    df = pd.read_csv(log_path)
    # Example: just print number of trades
    logger.info(f"Total trades so far: {len(df)}")
