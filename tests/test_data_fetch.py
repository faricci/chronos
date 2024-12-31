
# tests/test_data_fetch.py
import unittest
import os
import logging
import pandas as pd
from unittest.mock import patch
# Make sure to import *all* you need from datetime
from datetime import datetime, timezone, timedelta

# If you're patching coinbase's RESTClient:
from coinbase.rest import RESTClient

from modules.utils import load_config
from modules.data_fetch import DataFetcher, MINIMUM_DATE

"""
This module contains unit tests for the DataFetcher class in the data_fetch module.

Classes:
    TestDataFetcher: A unittest.TestCase subclass that contains tests for the DataFetcher class.

Methods:
    setUpClass(cls): Initializes shared resources before running any tests in this class, 
                     pointing to the Sandbox credentials & base URL.
    test_fetch_realtime_data(self): Tests fetching real-time data for a product 
                                    and verifies the returned DataFrame structure.
    test_fetch_historical_data(self): Tests fetching historical candle data for the last hour 
                                      with 1-minute granularity and verifies the returned 
                                      DataFrame structure and ordering.
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
        Typically used to initialize shared resources, such as the DataFetcher.
        Here, we specifically override the 'coinbase' config with 'coinbase_sandbox' settings.
        """

        logging.basicConfig(level=logging.DEBUG)

        # Load your entire config from the YAML file
        config = load_config()

        # Overwrite the 'coinbase' section with the sandbox credentials
        # so that DataFetcher will use the sandbox environment.
        config['coinbase']['base_url'] = config['coinbase_sandbox']['base_url']
        config['coinbase']['name'] = config['coinbase'].get('name', '')
        config['coinbase']['privateKey'] = config['coinbase'].get('privateKey', '')

        cls.config = config
        cls.data_fetcher = DataFetcher(config=cls.config)

    #TODO @unittest.skipIf(CONDITION, "Sandbox not guaranteed to have data.")
    def test_fetch_realtime_data(self):
        """
        Tests fetching real-time data for a product (e.g., XLM-EUR).
        Verifies the returned DataFrame structure.
        NOTE: The sandbox might have limited data, so if this test fails due 
              to empty results, you may need to skip or handle it.
        """
        product_id = "XLM-EUR"
        df = self.data_fetcher.fetch_realtime_data(product_id)

        self.assertIsInstance(df, pd.DataFrame, "Should return a Pandas DataFrame")

        if not df.empty:
            self.assertIn("price", df.columns, "DataFrame should have 'price' column")
            self.assertIn("volume", df.columns, "DataFrame should have 'volume' column")
            self.assertIn("time", df.columns, "DataFrame should have 'time' column")
        else:
            # It's possible the sandbox may return empty data
            self.fail("Realtime data fetch returned an empty DataFrame (sandbox might have no data?)")

    def test_fetch_historical_data(self):
        """
        Tests fetching historical candle data for the last hour with 1-minute granularity.
        Verifies the returned DataFrame structure and ordering.
        NOTE: The sandbox might have limited or no historical data.
        """
        product_id = "XLM-EUR"
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
            self.assertTrue(
                expected_cols.issubset(df.columns),
                "DataFrame should have columns: time, open, high, low, close, volume"
            )
            self.assertTrue(
                df["time"].is_monotonic_increasing,
                "Time column should be sorted in ascending order"
            )
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

class TestDataFetcherPastDates(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.config = {
            'coinbase': {
                'base_url': 'https://api.coinbase.com',
                'name': '',
                'privateKey': ''
            },
            'historical': {
                'past_interval_hours': 8
            }
        }
        cls.fetcher = DataFetcher(cls.config)

        cls.test_file_path = "data/historical_BTC-USD.csv"
        # Ensure the file is removed before testing
        if os.path.exists(cls.test_file_path):
            os.remove(cls.test_file_path)

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(cls.test_file_path):
            os.remove(cls.test_file_path)

    @patch.object(RESTClient, 'get_candles')
    def test_get_historical_data_past_dates(self, mock_get_candles):
        """
        Tests get_historical_data_past_dates by patching RESTClient.get_candles,
        letting the real fetch_historical_data method run so CSV is written.
        """

        # We'll simulate multiple calls to get_candles as DataFetcher loops backwards in time
        # Each call can return some 'candles' or an empty set if we crossed 2020

        def side_effect_get_candles(product_id, start, end, granularity):
            # Convert string times to int, back to datetime, etc.
            start_dt = datetime.fromtimestamp(int(start), tz=timezone.utc)
            end_dt   = datetime.fromtimestamp(int(end), tz=timezone.utc)

            # If end_dt <= MINIMUM_DATE => return empty
            if end_dt <= MINIMUM_DATE:
                return {
                    "candles": []
                }

            # Otherwise, produce a small subset of candles
            # We'll produce 3 candle entries, 1-minute apart near the 'end_dt'
            times = [end_dt - timedelta(minutes=2),
                     end_dt - timedelta(minutes=1),
                     end_dt]
            # Filter out times < MINIMUM_DATE
            times = [t for t in times if t > MINIMUM_DATE]

            # Build the 'candles' list
            # Per Coinbase doc: each entry is {start, open, high, low, close, volume}
            # We'll fill them with dummy data
            candles_list = []
            for t in times:
                candles_list.append({
                    "start": str(int(t.timestamp())),
                    "low": "100.0",
                    "high": "102.0",
                    "open": "101.0",
                    "close": "101.5",
                    "volume": "10.0"
                })

            return {
                "candles": candles_list
            }

        mock_get_candles.side_effect = side_effect_get_candles

        # Now call the incremental fetch
        self.fetcher.get_historical_data_past_dates(product_id="BTC-USD")

        # The real fetch_historical_data runs, calls _append_data_to_file, etc.
        # Check the CSV now exists
        self.assertTrue(
            os.path.exists(self.test_file_path), 
            "CSV file should be created by the real fetch_historical_data code."
        )

        # Optionally read and validate the CSV
        df = pd.read_csv(self.test_file_path, parse_dates=["time"])
        self.assertFalse(df.empty, "DataFrame in CSV should not be empty if we returned candles.")
        self.assertIn("time", df.columns)
        self.assertIn("open", df.columns)
        # and so on...
        # You can also confirm no row's time < 2020 if that's part of your logic

        # Confirm that get_candles was indeed called multiple times
        self.assertGreater(
            mock_get_candles.call_count, 
            1,
            "Should call get_candles multiple times while looping backwards in 8-hour increments."
        )

# If you want to run ONLY this file’s tests directly:
# if __name__ == "__main__":
#     runner = unittest.TextTestRunner(verbosity=2)
#     runner.run(TestDataFetcher.suite())
