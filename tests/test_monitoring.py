# tests/test_monitoring.py

import unittest
import os
import pandas as pd
import shutil
from datetime import datetime
from modules.monitoring import (
    log_trade,
    evaluate_performance,
    TRADE_LOG_DIR,
    PERFORMANCE_LOG_PATH,
    _compute_metrics_from_df
)

class TestMonitoring(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # Clean up any old logs
        if os.path.exists(TRADE_LOG_DIR):
            shutil.rmtree(TRADE_LOG_DIR)
        if os.path.exists(PERFORMANCE_LOG_PATH):
            os.remove(PERFORMANCE_LOG_PATH)
        os.makedirs(TRADE_LOG_DIR, exist_ok=True)

    def test_log_and_evaluate(self):
        # 1) Log some trades
        # A: Opening a position (unrealized for now)
        log_trade(
            signal="BUY", 
            product_id="DOGE-EUR",
            price=0.1, 
            size=100, 
            realized_pnl=0, 
            unrealized_pnl=5,    # 5 euros in unrealized profit
            capital_used=10      # e.g. 0.1 * 100
        )
        # B: Another trade, partially closing => realized some profit
        log_trade(
            signal="SELL",
            product_id="DOGE-EUR",
            price=0.12,
            size=50,
            realized_pnl=1.0,    # e.g. realized +1 euro
            unrealized_pnl=2.0,  # remaining 50 doge open
            capital_used=6       # e.g. 0.12 * 50
        )
        # C: Final close
        log_trade(
            signal="SELL",
            product_id="DOGE-EUR",
            price=0.15,
            size=50,
            realized_pnl=3.0,    # final realized profit
            unrealized_pnl=0,    # fully closed
            capital_used=7.5     # 0.15 * 50
        )

        # 2) Evaluate Performance
        # We'll capture logs
        evaluate_performance(trigger_retrain=False)

        # 3) Verify results
        files = os.listdir(TRADE_LOG_DIR)
        self.assertTrue(len(files) > 0, "Trade log file should be created.")
        
        # Combine them
        dfs = []
        for f in files:
            if f.endswith(".csv"):
                path = os.path.join(TRADE_LOG_DIR, f)
                df_temp = pd.read_csv(path)
                dfs.append(df_temp)
        all_trades_df = pd.concat(dfs, ignore_index=True)
        self.assertEqual(len(all_trades_df), 3, "We should have 3 trades logged.")
        
        # Check performance log
        self.assertTrue(os.path.exists(PERFORMANCE_LOG_PATH),
                        "Performance log should be created or updated.")
        perf_df = pd.read_csv(PERFORMANCE_LOG_PATH)
        self.assertFalse(perf_df.empty, "Performance log shouldn't be empty.")
        
        # Let's do a local test of metrics
        metrics = _compute_metrics_from_df(all_trades_df)
        self.assertIn("final_realized_pnl", metrics)
        self.assertEqual(metrics["final_realized_pnl"], 4.0, 
                         "We realized 1 + 3 = 4.0 in total profits.")
        
        # ROI = 4 / sum_of_capital_used => 4 / (10 + 6 + 7.5) = 4/23.5 ~ 0.1702 => 17%
        self.assertAlmostEqual(metrics["roi"], 4.0 / 23.5, places=4)

    @classmethod
    def tearDownClass(cls):
        # Clean up after test
        if os.path.exists(TRADE_LOG_DIR):
            shutil.rmtree(TRADE_LOG_DIR)
        if os.path.exists(PERFORMANCE_LOG_PATH):
            os.remove(PERFORMANCE_LOG_PATH)

    
    @staticmethod
    def suite():
        """
        Optional static method to create a test suite for this class alone.
        You can call this if you want to run only these tests in isolation.
        """
        suite = unittest.TestSuite()
        suite.addTest(TestMonitoring("test_log_and_evaluate"))

        return suite

# If you want to run ONLY this file’s tests directly:
# if __name__ == "__main__":
#     runner = unittest.TextTestRunner(verbosity=2)
#     runner.run(TestMonitoring.suite())
