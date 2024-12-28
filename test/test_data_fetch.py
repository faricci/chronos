# tests/test_data_fetch.py

import unittest
from datetime import datetime, timedelta
import pandas as pd

# Optional: If you want to patch calls or use a mock
# from unittest.mock import patch

from modules.data_fetch import DataFetcher

"""
This module contains unit tests for the DataFetcher class in the data_fetch module.
Classes:
    TestDataFetcher: A unittest.TestCase subclass that contains tests for the DataFetcher class.
Methods:
    setUpClass(cls): Initializes shared resources before running any tests in this class.
    test_fetch_realtime_data(self): Tests fetching real-time data for a product and verifies the returned DataFrame structure.
    test_fetch_historical_data(self): Tests fetching historical candle data for the last hour with 1-minute granularity and verifies the returned DataFrame structure and ordering.
    suite(): Creates a test suite for this class alone, allowing these tests to be run in isolation.

"""
class TestDataFetcher(unittest.TestCase):
    """
    Class-based tests for the DataFetcher in data_fetch.py
    """

    @classmethod
    def setUpClass(cls):
        """
        setUpClass is called once before running any tests in this class.
        Typically used to initialize shared resources.
        """
        cls.config = {
            "coinbase": {
                # Replace with your Coinbase sandbox credentials
                "api_key": "YOUR_SANDBOX_KEY",
                "api_secret": "YOUR_SANDBOX_SECRET"
            }
        }
        cls.data_fetcher = DataFetcher(config=cls.config)

    def test_fetch_realtime_data(self):
        """
        Tests fetching real-time data for a product (e.g., BTC-USD).
        Verifies the returned DataFrame structure.
        """
        product_id = "BTC-USD"

        df = self.data_fetcher.fetch_realtime_data(product_id)
        self.assertIsInstance(df, pd.DataFrame, "Should return a Pandas DataFrame")

        if not df.empty:
            # Check columns
            self.assertIn("price", df.columns, "DataFrame should have 'price' column")
            self.assertIn("volume", df.columns, "DataFrame should have 'volume' column")
            self.assertIn("time", df.columns, "DataFrame should have 'time' column")
        else:
            # It's possible the sandbox may return empty data
            self.fail("Realtime data fetch returned an empty DataFrame (sandbox issue?)")

    def test_fetch_historical_data(self):
        """
        Tests fetching historical candle data for the last hour with 1-minute granularity.
        Verifies the returned DataFrame structure and ordering.
        """
        product_id = "BTC-USD"
        end_time = datetime.utcnow()
        start_time = end_time - timedelta(hours=1)
        granularity = 60  # 1 minute

        df = self.data_fetcher.fetch_historical_data(
            product_id=product_id,
            start=start_time,
            end=end_time,
            granularity=granularity
        )

        self.assertIsInstance(df, pd.DataFrame, "Should return a Pandas DataFrame")

        if not df.empty:
            expected_cols = {"time", "open", "high", "low", "close", "volume"}
            self.assertTrue(expected_cols.issubset(df.columns), 
                            "DataFrame should have candle columns: time, open, high, low, close, volume")
            self.assertTrue(df["time"].is_monotonic_increasing, 
                            "Time column should be sorted in ascending order")
        else:
            self.fail("Historical data fetch returned an empty DataFrame (sandbox might have no data?)")

    @staticmethod
    def suite():
        """
        Optional static method to create a test suite for this class alone.
        You can call this if you want to run only these tests in isolation.
        """
        suite = unittest.TestSuite()
        suite.addTest(TestDataFetcher("test_fetch_realtime_data"))
        suite.addTest(TestDataFetcher("test_fetch_historical_data"))
        return suite


# If you want to run ONLY this file’s tests directly:
# if __name__ == "__main__":
#     runner = unittest.TextTestRunner()
#     runner.run(TestDataFetcher.suite())
