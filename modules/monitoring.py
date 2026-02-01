import os
import logging
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from modules.utils import get_logger

logger = get_logger(__name__)

"""
Enhanced Monitoring & Retraining Module

- Logs trades to CSV (rotates logs if needed).
- Tracks open positions for unrealized PnL.
- Calculates realized PnL, ROI, drawdown, etc.
- Optionally triggers model retraining if performance is poor.
"""

# Paths (could move to config)
TRADE_LOG_DIR = "data/trade_logs"  # We'll store multiple log files if rotating
PERFORMANCE_LOG_PATH = "data/performance_log.csv"

# Thresholds for triggering retrain
LOSS_THRESHOLD = -0.1
DRAW_THRESHOLD = -0.2

# Example: If logs exceed this many trades in a file, we rotate.
MAX_TRADES_PER_FILE = 1000

#####################
# Rotating CSV Logic
#####################
def _get_current_trade_log_path():
    """
    Returns a log file path based on date or size-based rotation.
    We'll do date-based: trade_log_YYYY-MM-DD.csv
    """
    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    filename = f"trade_log_{today_str}.csv"
    if not os.path.exists(TRADE_LOG_DIR):
        os.makedirs(TRADE_LOG_DIR, exist_ok=True)
    return os.path.join(TRADE_LOG_DIR, filename)

def _rotate_if_needed(current_path):
    """
    If the current file exceeds MAX_TRADES_PER_FILE, 
    we rename it with a suffix and start a new file.
    """
    if not os.path.exists(current_path):
        return  # no file yet, no rotation

    df = pd.read_csv(current_path)
    if len(df) >= MAX_TRADES_PER_FILE:
        suffix = datetime.utcnow().strftime("%H%M%S")
        rotated_name = current_path.replace(".csv", f"_{suffix}.csv")
        os.rename(current_path, rotated_name)
        logger.info(f"Rotated log file: {current_path} => {rotated_name}")

###############################
# Model Retraining Placeholder
###############################
def retrain_model():
    logger.info("Retraining the model with new data (coinbase advanced trade usage).")
    os.system("python script/train_timeseries_model.py")
    # or import train_informer_model(...) from your train script
    # and call it with fresh CSV data
    pass

####################################
# Log Trades, Realized/Unrealized
####################################
def log_trade(
    signal, product_id, price, size, 
    realized_pnl=None, 
    unrealized_pnl=None, 
    capital_used=None
):
    """
    Log a trade to CSV with optional realized/unrealized PnL.

    signal: e.g. "BUY", "SELL", "CLOSE"
    product_id: e.g. "DOGE-EUR"
    price: float fill price
    size: float quantity
    realized_pnl: profit/loss if closing a position
    unrealized_pnl: optional field to track open position gain/loss
    capital_used: optional notional cost of this trade (for ROI calc)
    """
    row = {
        "timestamp": pd.Timestamp.now(),
        "signal": signal,
        "product_id": product_id,
        "price": price,
        "size": size,
        "realized_pnl": realized_pnl if realized_pnl is not None else np.nan,
        "unrealized_pnl": unrealized_pnl if unrealized_pnl is not None else np.nan,
        "capital_used": capital_used if capital_used is not None else np.nan
    }

    current_path = _get_current_trade_log_path()
    # Possibly rotate if needed
    _rotate_if_needed(current_path)

    if not os.path.exists(current_path):
        pd.DataFrame([row]).to_csv(current_path, index=False)
    else:
        df = pd.read_csv(current_path)
        # Pandas 2.x compatible - use pd.concat instead of df.append
        new_row_df = pd.DataFrame([row])
        df = pd.concat([df, new_row_df], ignore_index=True)
        df.to_csv(current_path, index=False)

    logger.info(f"Trade logged: {row}")

###############################
# Compute Performance Metrics
###############################
def _compute_metrics_from_df(df):
    """
    Given a DF of trades (with realized_pnl, unrealized_pnl, capital_used),
    compute cumulative PnL, ROI, drawdown, etc.
    """

    # Realized PnL (sum of all closed trade PnL)
    if "realized_pnl" not in df.columns:
        df["realized_pnl"] = 0.0

    df["cumulative_realized"] = df["realized_pnl"].cumsum()

    # ROI calculation:
    #  - If you log capital_used on each trade, sum that up or use the largest open capital
    total_capital_used = df["capital_used"].fillna(0).sum()
    # fallback if no capital_used => assume 10k
    if total_capital_used <= 0:
        total_capital_used = 10000.0
    final_realized = df["cumulative_realized"].iloc[-1]
    roi = final_realized / total_capital_used

    # Max drawdown on realized PnL
    running_max = df["cumulative_realized"].cummax()
    drawdown = df["cumulative_realized"] - running_max
    max_drawdown = drawdown.min()  # negative number

    return {
        "final_realized_pnl": final_realized,
        "roi": roi,
        "max_drawdown": max_drawdown,
        "number_of_trades": len(df)
    }

def evaluate_performance(trigger_retrain=False):
    """
    Goes through *all* trade logs in TRADE_LOG_DIR, merges them, 
    calculates PnL, ROI, drawdown, etc.
    Optionally triggers retraining if thresholds are exceeded.
    """
    if not os.path.exists(TRADE_LOG_DIR):
        logger.info("No trade logs directory found. No trades to evaluate.")
        return

    # Gather all CSV files
    files = [f for f in os.listdir(TRADE_LOG_DIR) if f.endswith(".csv")]
    if not files:
        logger.info("No trade log files found.")
        return

    # Merge all logs
    dfs = []
    for f in files:
        path = os.path.join(TRADE_LOG_DIR, f)
        df_temp = pd.read_csv(path, parse_dates=["timestamp"])
        dfs.append(df_temp)
    all_trades_df = pd.concat(dfs, ignore_index=True)
    all_trades_df.sort_values("timestamp", inplace=True)

    if all_trades_df.empty:
        logger.info("Trade logs are empty. No performance to evaluate.")
        return

    metrics = _compute_metrics_from_df(all_trades_df)

    final_pnl = metrics["final_realized_pnl"]
    roi = metrics["roi"]
    max_dd = metrics["max_drawdown"]
    n_trades = metrics["number_of_trades"]

    logger.info("--- Performance Summary ---")
    logger.info(f"Total trades so far: {n_trades}")
    logger.info(f"Final Realized PnL: {final_pnl:.2f}")
    logger.info(f"ROI: {roi:.2%} (relative to sum of 'capital_used' or 10k fallback)")
    logger.info(f"Max Drawdown (realized): {max_dd:.2f}")

    # Log metrics (timestamp-based) in performance CSV
    perf_data = {
        "timestamp": datetime.utcnow().isoformat(),
        "num_trades": n_trades,
        "final_realized_pnl": final_pnl,
        "roi": roi,
        "max_drawdown": max_dd
    }
    if not os.path.exists(PERFORMANCE_LOG_PATH):
        pd.DataFrame([perf_data]).to_csv(PERFORMANCE_LOG_PATH, index=False)
    else:
        df_perf = pd.read_csv(PERFORMANCE_LOG_PATH)
        df_perf = df_perf.append(perf_data, ignore_index=True)
        df_perf.to_csv(PERFORMANCE_LOG_PATH, index=False)

    # Optionally retrain if below thresholds
    if trigger_retrain:
        if roi < LOSS_THRESHOLD or max_dd < DRAW_THRESHOLD:
            logger.warning("Performance threshold exceeded! Retraining model.")
            retrain_model()
        else:
            logger.info("Performance within acceptable range. No retraining triggered.")
