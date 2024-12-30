# test_data_preprocessing.py
import unittest
import pandas as pd
import numpy as np

from modules.data_preprocessing import normalize_data, compute_indicators

"""
This module contains unit tests for data_preprocessing.py, specifically testing:
    1) normalize_data
    2) compute_indicators

Class-based tests for the data_preprocessing module.
This class contains unit tests for the following functions:
Methods
-------
    setUpClass(cls):
        Class-based tests for data_preprocessing module.
    test_normalize_data(self):
        Tests if normalize_data properly scales the desired columns to [0,1].
    test_compute_indicators(self):
        Tests if compute_indicators properly adds 'ma_5' and 'rsi' columns.
    suite():
        Optional static method to create a test suite for this class alone.
"""

class TestDataPreprocessing(unittest.TestCase):
    """
    Class-based tests for data_preprocessing module.
    """

    @classmethod
    def setUpClass(cls):
        """
        Creates a dummy DataFrame to use across all tests.
        """
        # Example dummy data for 10 rows
        data = {
            "price":  [10, 12, 13, 15, 14, 20, 18, 22, 25, 30],
            "volume": [100, 120, 80, 90, 110, 150, 130, 160, 200, 250],
            "open":   [10, 11, 12, 14, 14, 19, 17, 21, 24, 28],
            "high":   [11, 13, 14, 16, 15, 21, 19, 23, 26, 31],
            "low":    [9,  10, 11, 13, 13, 18, 16, 20, 23, 27],
            "close":  [10, 12, 13, 15, 14, 20, 18, 22, 25, 30],
        }
        cls.df = pd.DataFrame(data)

    def test_normalize_data(self):
        """
        Tests if normalize_data properly scales the desired columns to [0,1].
        """
        # Make a copy so we can check original vs scaled
        df_copy = self.df.copy()

        scaled_df, scaler = normalize_data(df_copy)
        self.assertIsNotNone(scaler, "Scaler should be returned.")

        # We expect columns: [price, volume, open, high, low, close] to be in [0,1] range
        for col in ['price', 'volume', 'open', 'high', 'low', 'close']:
            min_val = scaled_df[col].min()
            max_val = scaled_df[col].max()
            self.assertGreaterEqual(min_val, 0.0, f"{col} min should be >= 0")
            self.assertLessEqual(max_val, 1.0, f"{col} max should be <= 1")

        # Check if the shape is the same
        self.assertEqual(scaled_df.shape, self.df.shape, "DataFrame shape should remain the same after scaling.")

    def test_compute_indicators(self):
        """
        Tests if compute_indicators properly adds 'ma_5' and 'rsi' columns.
        """
        df_copy = self.df.copy()

        result_df = compute_indicators(df_copy, ma_window=5, rsi_period=14)
        self.assertIn('ma_5', result_df.columns, "Expected 'ma_5' column in result.")
        self.assertIn('rsi', result_df.columns, "Expected 'rsi' column in result.")

        # For a 5-day rolling average, first few rows may be NaN but we do fillna(bfill)
        # So let's see if it's effectively filled.
        self.assertFalse(result_df['ma_5'].isna().any(), "ma_5 should have been backfilled.")
        self.assertFalse(result_df['rsi'].isna().any(),  "rsi should have been backfilled.")

        # Basic check: 'ma_5' should be between min and max of 'close'
        # (though it might be slightly beyond if there's a short initial window).
        ma_min = result_df['ma_5'].min()
        ma_max = result_df['ma_5'].max()
        self.assertGreaterEqual(ma_min, result_df['close'].min(), "ma_5 min shouldn't be below close min.")
        self.assertLessEqual(ma_max, result_df['close'].max(), "ma_5 max shouldn't be above close max.")

    @staticmethod
    def suite():
        """
        Optional static method to create a test suite for this class alone.
        """
        suite = unittest.TestSuite()
        suite.addTest(TestDataPreprocessing("test_normalize_data"))
        suite.addTest(TestDataPreprocessing("test_compute_indicators"))
        return suite


# If you want to run ONLY this file’s tests directly:
# if __name__ == "__main__":
#     runner = unittest.TextTestRunner(verbosity=2)
#     runner.run(TestDataPreprocessing.suite())
