# tests/test_sentiment_analysis.py

import unittest
import pandas as pd
import logging

from modules.sentiment_analysis import SentimentAnalysis
from modules.utils import load_config  # or wherever your load_config function is defined

class TestSentimentAnalysis(unittest.TestCase):

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

        cls.config = config

    def test_scrape_news(self):
        texts = SentimentAnalysis.scrape_news(self.config)
        self.assertIsInstance(texts, str, "scrape_news should return a string.")

    def test_get_text_embeddings(self):
        texts = ["Hello World", "Testing embeddings"]
        embeddings = SentimentAnalysis.get_text_embeddings(texts)
        self.assertEqual(len(embeddings), len(texts))

        # Test empty list
        embeddings_empty = SentimentAnalysis.get_text_embeddings([])
        self.assertEqual(len(embeddings_empty), 0, "Should return empty list for empty input.")

    def test_analyze_sentiment(self):
        # 2D dummy embeddings: let's say 2 embeddings with dimension=3
        dummy_embeddings = [
            [1.0, 2.0, 3.0],
            [0.5, 0.2, 0.1]
        ]
        scores = SentimentAnalysis.analyze_sentiment(dummy_embeddings)
        self.assertEqual(len(scores), len(dummy_embeddings), 
                         "Number of sentiment scores should match number of embeddings.")
        for s in scores:
            self.assertIsInstance(s, float, "Sentiment score should be a float.")

        # Test empty embeddings
        empty_scores = SentimentAnalysis.analyze_sentiment([])
        self.assertEqual(len(empty_scores), 0, "Should return empty list for empty embeddings.")

    def test_get_sentiment_data(self):
        # End-to-end test
        df = SentimentAnalysis.get_sentiment_data(self.config)
        self.assertIsInstance(df, pd.DataFrame, "Should return a DataFrame.")
        required_cols = {"text", "embedding", "score"}
        self.assertTrue(required_cols.issubset(df.columns), 
                        f"DataFrame should contain columns {required_cols}.")
        self.assertGreater(len(df), 0, "Expected some rows from get_sentiment_data.")
        # Check types
        for emb in df['embedding']:
            self.assertIsInstance(emb, list, "Embedding should be a list (or np.array).")
        for sc in df['score']:
            self.assertIsInstance(sc, float, "Sentiment score should be a float.")

    @staticmethod
    def suite():
        """
        Optional static method to create a test suite for this class alone.
        You can call this if you want to run only these tests in isolation.
        """
        suite = unittest.TestSuite()
        suite.addTest(TestSentimentAnalysis("test_scrape_news"))
        suite.addTest(TestSentimentAnalysis("test_get_text_embeddings"))
        suite.addTest(TestSentimentAnalysis("test_analyze_sentiment"))
        suite.addTest(TestSentimentAnalysis("test_get_sentiment_data"))
        return suite
# If you want to run ONLY this file’s tests directly:
# if __name__ == "__main__":
#     runner = unittest.TextTestRunner(verbosity=2)
#     runner.run(TestSentimentAnalysis.suite())
