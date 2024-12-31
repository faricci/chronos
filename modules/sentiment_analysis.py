# modules/sentiment_analysis.py

import requests
from bs4 import BeautifulSoup
from langchain.embeddings import HuggingFaceEmbeddings
from langchain.docstore.document import Document
import pandas as pd
import numpy as np

"""
SentimentAnalysis performs sentiment analysis on text data.

Methods:
    scrape_news(config):
        Scrapes example news data and returns a list of text snippets.

    get_text_embeddings(texts, model_name="sentence-transformers/all-MiniLM-L6-v2"):
        Converts a list of text snippets into their corresponding embeddings.

    analyze_sentiment(embeddings):
        Analyzes sentiment based on the provided embeddings.

    get_sentiment_data(config):
        Retrieves sentiment data by scraping news, generating embeddings, and analyzing sentiment.
"""

class SentimentAnalysis:
    
    @staticmethod
    def scrape_news(config):
        """
        Placeholder function that simulates scraping a news site or forum
        and returns a list of text snippets.
        """
        # Example for a more 'real' scenario (commented out):
        try:
            response = requests.get(config['sentiment'].get('base_url', 'https://www.coindesk.com'))
            soup = BeautifulSoup(response.text, "html.parser")
            #articles = soup.find_all("div", class_="article-content")
            #texts = [article.get_text(strip=True) for article in articles]

            # Extract all text from the page, excluding script and style tags
            for script in soup(["script", "style"]):
                script.extract()    # rip it out

            text = soup.get_text()
            # Remove extra whitespace and newlines
            lines = (line.strip() for line in text.splitlines())
            chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
            text = '\n'.join(chunk for chunk in chunks if chunk)

            return text
        except Exception as e:
            print("Error scraping site:", e)
            return []

    @staticmethod
    def get_text_embeddings(texts, model_name="sentence-transformers/all-MiniLM-L6-v2"):
        """
        Converts a list of text snippets into their embeddings using LangChain's HuggingFaceEmbeddings.
        """
        if not texts:
            return []
        embedder = HuggingFaceEmbeddings(model_name=model_name)
        docs = [Document(page_content=txt) for txt in texts]
        embeddings = embedder.embed_documents(docs)
        return embeddings

    @staticmethod
    def analyze_sentiment(embeddings):
        """
        Simple placeholder: average embedding magnitude as a 'sentiment score'
        (A real scenario could use a classification or regression model.)

        # Example approach for a real scenario (commented):
        # Suppose we had a logistic regression that outputs a sentiment label (pos/neg) based on embeddings.
        # clf = joblib.load("sentiment_model.pkl")
        # predictions = clf.predict(embeddings)
        # return predictions

        """
        if not embeddings:
            return []
        scores = [np.linalg.norm(e) for e in embeddings]
        return scores

    @staticmethod
    def get_sentiment_data(config):
        """
        Retrieves sentiment data by:
          1) Scraping text snippets (scrape_news).
          2) Generating embeddings (get_text_embeddings).
          3) Calculating a basic sentiment score (analyze_sentiment).
          4) Returning a DataFrame with columns ['text', 'embedding', 'score'].
        """
        texts = SentimentAnalysis.scrape_news(config)
        embeddings = SentimentAnalysis.get_text_embeddings(texts)
        scores = SentimentAnalysis.analyze_sentiment(embeddings)
        sentiment_df = pd.DataFrame({
            'text': texts,
            'embedding': embeddings,
            'score': scores
        })
        return sentiment_df
