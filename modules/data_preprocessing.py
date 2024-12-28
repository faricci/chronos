import pandas as pd
import numpy as np
from sklearn.preprocessing import MinMaxScaler

"""
Handles normalization, feature engineering, etc.
"""

#TODO Improve with more sofisticated techniques
def normalize_data(df, columns=None):
    """
    Applies MinMax scaling to the selected columns.
    """
    if columns is None:
        columns = ['price', 'volume', 'open', 'high', 'low', 'close']
    
    scaler = MinMaxScaler()
    df[columns] = scaler.fit_transform(df[columns])
    return df, scaler

#TODO Improve with more sofisticated techniques
def compute_indicators(df):
    """
    Computes simple indicators like moving averages, RSI, etc.
    """
    # Moving average example (window of 5)
    df['ma_5'] = df['close'].rolling(window=5).mean()

    # RSI example (14 period)
    # We'll do a naive approach here
    #window_length = 14
    #delta = df['close'].diff()
    #gain = (delta.mask(delta < 0, 0)).rolling(window=window_length).mean()
    #loss = (-delta.mask(delta > 0, 0)).rolling(window=window_length).mean()
    #rs = gain / loss
    #df['rsi'] = 100 - (100 / (1 + rs))

    window_length = 14
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    
    avg_gain = gain.ewm(span=window_length, min_periods=window_length).mean()
    avg_loss = loss.ewm(span=window_length, min_periods=window_length).mean()
    
    rs = avg_gain / avg_loss
    df['rsi'] = 100 - (100 / (1 + rs))

    # Fill NaNs
    df.fillna(method='bfill', inplace=True)
    return df
