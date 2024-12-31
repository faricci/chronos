# modules/sentiment_analysis.py

import requests
from bs4 import BeautifulSoup
from langchain_community.embeddings import HuggingFaceEmbeddings
import pandas as pd
import numpy as np

"""
SentimentAnalysis performs sentiment analysis on text data.

Methods:
    scrape_news(config):
        Scrapes a news site or forum specified in config['sentiment']['base_url']
        and returns the entire page text as a single string.

    get_text_embeddings(texts, model_name="sentence-transformers/all-MiniLM-L6-v2"):
        Converts a list of text snippets into their embeddings.

    analyze_sentiment(embeddings):
        Simple placeholder: returns norm-based 'sentiment score'.

    get_sentiment_data(config):
        1) scrape_news -> single string of page text
        2) convert to one-element list for embeddings
        3) generate embeddings, analyze sentiment
        4) build a DataFrame
"""

class SentimentAnalysis:
    
    @staticmethod
    def scrape_news(config):
        """
        Attempts to scrape the webpage specified by config['sentiment']['base_url'].
        Returns the entire page text as a single string.

        If any error occurs, returns an empty string.
        """
        try:
            url = config['sentiment'].get('base_url', 'https://www.coindesk.com')
            response = requests.get(url)
            soup = BeautifulSoup(response.text, "html.parser")

            # Remove script/style tags
            for script in soup(["script", "style"]):
                script.extract()

            text = soup.get_text()
            # Clean whitespace
            lines = (line.strip() for line in text.splitlines())
            chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
            text = '\n'.join(chunk for chunk in chunks if chunk)
            return text

        except Exception as e:
            print("Error scraping site:", e)
            return ""

    @staticmethod
    def get_text_embeddings(texts, model_name="sentence-transformers/all-MiniLM-L6-v2"):
        """
        Converts a list of text strings into embeddings using HuggingFaceEmbeddings.
        (No longer passing Document objects.)
        """
        if not texts:
            return []
        embedder = HuggingFaceEmbeddings(model_name=model_name)
        # Directly pass a list of strings:
        embeddings = embedder.embed_documents(texts)
        return embeddings

    @staticmethod
    def analyze_sentiment(embeddings):
        """
        Placeholder sentiment analysis:
        calculates the L2 norm of each embedding as a 'score'.
        """
        if not embeddings:
            return []
        scores = [np.linalg.norm(e) for e in embeddings]
        return scores

    @staticmethod
    def get_sentiment_data(config):
        """
        Full pipeline:
          1) Scrape webpage text (scrape_news) -> single string
          2) Convert to a one-element list for embedding
          3) Generate embeddings and sentiment scores
          4) Return a DataFrame with ['text', 'embedding', 'score']
        """
        page_text = SentimentAnalysis.scrape_news(config)  # single string
        if not page_text:
            # If scraping failed or returned nothing, return an empty DataFrame
            return pd.DataFrame(columns=['text', 'embedding', 'score'])

        # We'll treat the entire page as ONE "document"
        texts = [page_text]  # single-element list
        embeddings = SentimentAnalysis.get_text_embeddings(texts)
        scores = SentimentAnalysis.analyze_sentiment(embeddings)

        sentiment_df = pd.DataFrame({
            'text': texts,
            'embedding': embeddings,
            'score': scores
        })
        return sentiment_df
