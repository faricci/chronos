# tests/test_sentiment_analysis.py

import unittest
import pandas as pd
from modules.sentiment_analysis import SentimentAnalysis

class TestSentimentAnalysis(unittest.TestCase):

    def test_scrape_example_news(self):
        texts = SentimentAnalysis.scrape_example_news()
        self.assertIsInstance(texts, list, "scrape_example_news should return a list.")
        self.assertTrue(len(texts) > 0, "scrape_example_news returned empty list unexpectedly.")

    def test_get_text_embeddings(self):
        # Test normal input
        texts = ["Hello World", "Testing embeddings"]
        embeddings = SentimentAnalysis.get_text_embeddings(texts)
        self.assertEqual(len(embeddings), len(texts), "Should return one embedding per text snippet.")
        self.assertGreater(len(embeddings[0]), 0, "Embedding vector should not be empty.")

        # Test empty input
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
        df = SentimentAnalysis.get_sentiment_data()
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

# If you want to run ONLY this file’s tests directly:
# if __name__ == "__main__":
#     runner = unittest.TextTestRunner(verbosity=2)
#     runner.run(TestSentimentAnalysis.suite())
