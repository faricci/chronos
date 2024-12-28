import requests
from bs4 import BeautifulSoup
from langchain.embeddings import HuggingFaceEmbeddings
from langchain.docstore.document import Document
import pandas as pd
import numpy as np

"""
SentimentAnalysis is used to perform sentiment analysis on text data.
It includes a website scraper method 

Methods:
    scrape_example_news():
        Scrapes example news data and returns a list of text snippets.

    get_text_embeddings(texts, model_name="sentence-transformers/all-MiniLM-L6-v2"):
        Converts a list of text snippets into their corresponding embeddings.

    analyze_sentiment(embeddings):
        Analyzes sentiment based on the provided embeddings.

    get_sentiment_data():
        Retrieves sentiment data by scraping news, generating embeddings, and analyzing sentiment.
"""
class SentimentAnalysis:
    
    @staticmethod
    def scrape_example_news():
        """
        Placeholder function that scrapes a news site or forum
        and returns a list of text snippets.
        """
        texts = [
            "Bitcoin price is rising, bullish momentum expected.",
            "ETH developers share major update."
        ]
        return texts

    @staticmethod
    def get_text_embeddings(texts, model_name="sentence-transformers/all-MiniLM-L6-v2"):
        embedder = HuggingFaceEmbeddings(model_name=model_name)
        docs = [Document(page_content=txt) for txt in texts]
        embeddings = embedder.embed_documents(docs)
        return embeddings

    @staticmethod
    def analyze_sentiment(embeddings):
        """
        Simple placeholder: average embedding magnitude as a 'sentiment score'
        (Real scenario: you'd use a classification model or a regression model.)
        """
        scores = [np.linalg.norm(e) for e in embeddings]
        return scores

    @staticmethod
    def get_sentiment_data():
        texts = SentimentAnalysis.scrape_example_news()
        embeddings = SentimentAnalysis.get_text_embeddings(texts)
        scores = SentimentAnalysis.analyze_sentiment(embeddings)
        sentiment_df = pd.DataFrame({
            'text': texts,
            'embedding': embeddings,
            'score': scores
        })
        return sentiment_df


