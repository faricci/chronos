# tests/test_data_preprocessing.py
import unittest
import pandas as pd
import numpy as np

from modules.data_preprocessing import normalize_data, compute_indicators

'''
Unit tests for the data_preprocessing module.
This module contains class-based tests for the functions `normalize_data` and `compute_indicators`
from the `data_preprocessing` module. The tests ensure that the data normalization and indicator
computation functions work as expected.
Classes:
    TestDataPreprocessing: Contains unit tests for the data_preprocessing module.
Methods:
    setUpClass: Sets up a dummy DataFrame to be used across tests.
    test_normalize_data: Tests that `normalize_data` scales the specified columns between [0,1].
    test_compute_indicators: Tests that `compute_indicators` adds a moving average column and an RSI column.
    suite: Creates a test suite for these tests alone.
'''
class TestDataPreprocessing(unittest.TestCase):
    """
    Class-based tests for the data_preprocessing module.
    """

    @classmethod
    def setUpClass(cls):
        """
        Create a dummy DataFrame to use across tests.
        We'll have more than 6 rows, so RSI(6) can compute.
        """
        data = {
            "price":  [10, 12, 13, 15, 14, 20, 18, 22, 25],
            "volume": [100,120, 80, 90, 110,150,130,160,200],
            "open":   [10, 11, 12, 14, 14, 19,17,21,24],
            "high":   [11, 13, 14, 16, 15, 21,19,23,26],
            "low":    [9,  10, 11, 13, 13, 18,16,20,23],
            "close":  [10, 12, 13, 15, 14, 20,18,22,25]
        }
        cls.df = pd.DataFrame(data)

    def test_normalize_data(self):
        """
        Tests that normalize_data scales the specified columns between [0,1].
        """
        df_copy = self.df.copy()
        scaled_df, scaler = normalize_data(df_copy)
        self.assertIsNotNone(scaler, "Expected a scaler instance to be returned.")

        for col in ['price','volume','open','high','low','close']:
            self.assertIn(col, scaled_df.columns, f"{col} column should exist.")
            col_min = scaled_df[col].min()
            col_max = scaled_df[col].max()
            self.assertGreaterEqual(col_min, 0.0, f"{col} min should be >= 0 after scaling")
            self.assertLessEqual(col_max, 1.0, f"{col} max should be <= 1 after scaling")

        # The shape should remain the same
        self.assertEqual(scaled_df.shape, self.df.shape, "Data shape should not change.")

    def test_compute_indicators(self):
        """
        Tests that compute_indicators adds a moving average column and an RSI column.
        We use a smaller rsi_period so we don't need a large DataFrame to avoid all NaNs.
        """
        df_copy = self.df.copy()

        result_df = compute_indicators(df_copy, ma_window=3, rsi_period=6)  
        self.assertIn('ma_3', result_df.columns, "Expected moving average column in result.")
        self.assertIn('rsi', result_df.columns, "Expected 'rsi' column in result.")

        # Verify we have no NaNs left in those columns after the fill
        self.assertFalse(result_df['ma_3'].isna().any(), "All ma_3 values should be filled.")
        self.assertFalse(result_df['rsi'].isna().any(), "All RSI values should be filled.")

        # Quick check: 'ma_3' should be an average of 'close' over 3 rows
        # Just spot check last row (index=8) => average of close at [6,7,8] => (18+22+25)/3 = 21.666...
        expected_ma = (18 + 22 + 25) / 3
        self.assertAlmostEqual(result_df['ma_3'].iloc[-1], expected_ma, places=2,
                               msg="Moving average calculation mismatch.")

        # RSI is trickier to verify exactly, but we can ensure it's between 0 and 100
        rsi_min = result_df['rsi'].min()
        rsi_max = result_df['rsi'].max()
        self.assertGreaterEqual(rsi_min, 0, "RSI should not go below 0.")
        self.assertLessEqual(rsi_max, 100, "RSI should not exceed 100.")

    @staticmethod
    def suite():
        """
        Creates a test suite for these tests alone.
        """
        suite = unittest.TestSuite()
        suite.addTest(TestDataPreprocessing("test_normalize_data"))
        suite.addTest(TestDataPreprocessing("test_compute_indicators"))
        return suite

# If you want to run ONLY this file’s tests directly:
# if __name__ == "__main__":
#     runner = unittest.TextTestRunner(verbosity=2)
#     runner.run(TestDataPreprocessing.suite())
