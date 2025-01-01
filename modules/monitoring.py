import os
import logging
import pandas as pd
import numpy as np
from datetime import datetime
from modules.utils import get_logger

logger = get_logger(__name__)

"""
Monitoring & Retraining Module

Logs trades to CSV, calculates performance metrics (PnL, ROI, drawdown),
and optionally triggers model retraining if performance deteriorates.
"""

# FIXME add to config
# Path to the trade log CSV
TRADE_LOG_PATH = "data/trade_log.csv"

# Path to store performance metrics
PERF_LOG_PATH = "data/performance_log.csv"

# Example thresholds for deciding to retrain or alert
LOSS_THRESHOLD = -0.1    # e.g. -10% cumulative ROI
DRAW_THRESHOLD = -0.2    # e.g. -20% maximum drawdown

# A placeholder function you might implement to retrain the model
# or call a separate script (train_timeseries_model.py).
def retrain_model():
    logger.info("Retraining the model with new data...")
    os.system("python script/train_timeseries_model.py")
    # or import train_informer_model(...) from your train script
    # and call it with fresh CSV data
    pass

def log_trade(signal, product_id, price, size, realized_pnl=None):
    """
    Appends a row to 'trade_log.csv' or another persistent store.

    Parameters:
    - signal (str): "BUY" / "SELL" / "CLOSE" / etc.
    - product_id (str): e.g. "BTC-USD"
    - price (float): Fill or execution price
    - size (float): Number of units traded (e.g., 5 tokens, or 0.01 BTC)
    - realized_pnl (float, optional): Profit/Loss realized by this trade.
      Could be 0 if it's an opening trade, or if you compute PnL only upon closing.
    """
    row = {
        "timestamp": pd.Timestamp.now(),
        "signal": signal,
        "product_id": product_id,
        "price": price,
        "size": size,
        "realized_pnl": realized_pnl if realized_pnl is not None else np.nan
    }

    if not os.path.exists(TRADE_LOG_PATH):
        pd.DataFrame([row]).to_csv(TRADE_LOG_PATH, index=False)
    else:
        df = pd.read_csv(TRADE_LOG_PATH)
        df = df.append(row, ignore_index=True)
        df.to_csv(TRADE_LOG_PATH, index=False)

    logger.info(f"Trade logged: {row}")

def _compute_basic_metrics(df):
    """
    Compute basic performance metrics from a trade DataFrame:
      - cumulative_pnl: sum of realized PnL
      - roi: ratio of cumulative_pnl to total capital used (toy approach)
      - max_drawdown: the largest peak-to-trough decline in cumulative PnL
    """
    # We assume 'realized_pnl' is only recorded when a position is closed
    # or partial realized. If your logic logs PnL differently, adjust accordingly.

    if "realized_pnl" not in df.columns:
        return {}

    # If the strategy logs realized PnL at each trade close, we can just do:
    df["cumulative_pnl"] = df["realized_pnl"].cumsum()

    # ROI calculation is simplistic; in reality, you’d have a known capital base
    # or track margin usage. For demonstration, assume an initial capital of e.g. 10000
    initial_capital = 10000.0
    final_pnl = df["cumulative_pnl"].iloc[-1] if not df.empty else 0.0
    roi = final_pnl / initial_capital

    # Max Drawdown calculation:
    # 1) get the running maximum of cumulative pnl
    # 2) compute drawdown = running_max - current_pnl
    # 3) the maximum of that difference is the max drawdown
    running_max = df["cumulative_pnl"].cummax()
    drawdown_series = df["cumulative_pnl"] - running_max
    max_drawdown = drawdown_series.min()  # negative value => e.g. -200 => -$200 from the peak

    metrics = {
        "final_pnl": final_pnl,
        "roi": roi,
        "max_drawdown": max_drawdown
    }
    return metrics

def evaluate_performance(trigger_retrain=False):
    """
    Reads the trade_log and calculates performance metrics (PnL, ROI, drawdown).
    Optionally triggers model retraining if performance is below thresholds.
    Logs results to 'performance_log.csv' for historical record.

    Parameters:
    - trigger_retrain (bool): if True, will call retrain_model() if thresholds are exceeded.
    """
    if not os.path.exists(TRADE_LOG_PATH):
        logger.info("No trades to evaluate yet.")
        return

    df = pd.read_csv(TRADE_LOG_PATH, parse_dates=["timestamp"])
    if df.empty:
        logger.info("Trade log is empty. No performance to evaluate.")
        return

    metrics = _compute_basic_metrics(df)
    if not metrics:
        logger.info("No realized PnL column found. Performance evaluation incomplete.")
        return

    # Summarize performance
    final_pnl = metrics["final_pnl"]
    roi = metrics["roi"]
    max_dd = metrics["max_drawdown"]

    logger.info(f"--- Performance Summary ---")
    logger.info(f"Total trades so far: {len(df)}")
    logger.info(f"Final PnL: {final_pnl:.2f}")
    logger.info(f"ROI (assuming 10k initial capital): {roi:.2%}")
    logger.info(f"Max Drawdown: {max_dd:.2f}")

    # Log the metrics to a separate CSV
    perf_row = {
        "timestamp": datetime.utcnow().isoformat(),
        "num_trades": len(df),
        "final_pnl": final_pnl,
        "roi": roi,
        "max_drawdown": max_dd
    }
    if not os.path.exists(PERF_LOG_PATH):
        pd.DataFrame([perf_row]).to_csv(PERF_LOG_PATH, index=False)
    else:
        perf_df = pd.read_csv(PERF_LOG_PATH)
        perf_df = perf_df.append(perf_row, ignore_index=True)
        perf_df.to_csv(PERF_LOG_PATH, index=False)

    # Optional: if performance is bad, we can retrain or alert.
    if trigger_retrain:
        # FIXME If your strategy hits a large drawdown, you might want instant alerts (Slack, email, etc.).
        # Check if ROI < LOSS_THRESHOLD or max drawdown below DRAW_THRESHOLD
        # e.g. if ROI = -0.12 => -12% => trigger
        if roi < LOSS_THRESHOLD or max_dd < DRAW_THRESHOLD:
            logger.warning("Performance threshold exceeded! Initiating retrain workflow.")
            retrain_model()
        else:
            logger.info("Performance within acceptable range. No retraining triggered.")
