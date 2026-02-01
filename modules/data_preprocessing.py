# modules/data_preprocessing.py

import pandas as pd
import numpy as np
from sklearn.preprocessing import MinMaxScaler

"""
This module provides functions for data preprocessing, including normalization and computation of financial indicators.
Functions:
    normalize_data(df, columns=None, scaler=None):
        Applies MinMax scaling to the selected columns of a DataFrame.
    compute_indicators(df, ma_window=5, rsi_period=14):
        Computes simple financial indicators like moving averages and RSI for a DataFrame.
"""

def normalize_data(df, columns=None, scaler=None):
    """
    Applies MinMax scaling to the selected columns.
    
    Args:
        df (pd.DataFrame): Input DataFrame with numerical columns to be scaled.
        columns (list): List of columns to scale. 
            Default = ['price', 'volume', 'open', 'high', 'low', 'close'].
        scaler (object): A scikit-learn scaler instance. If None, a new MinMaxScaler is fit.
        
    Returns:
        (pd.DataFrame, scaler): The scaled DataFrame and the scaler used.
    """
    if columns is None:
        columns = ['price', 'volume', 'open', 'high', 'low', 'close']

    # Filter out any columns not present in df
    columns_to_scale = [col for col in columns if col in df.columns]
    if not columns_to_scale:
        # No valid columns to scale
        return df, None

    if scaler is None:
        scaler = MinMaxScaler()

    df[columns_to_scale] = scaler.fit_transform(df[columns_to_scale])

    return df, scaler


def compute_indicators(df, ma_window=5, rsi_period=14):
    """
    Computes simple indicators like moving averages (MA) and RSI using a classical approach.

    Args:
        df (pd.DataFrame): Input DataFrame (must contain 'close').
        ma_window (int): Window size for the moving average. Default = 5.
        rsi_period (int): Period for the classical RSI calculation. Default = 14 (production).

    Returns:
        pd.DataFrame: DataFrame with new columns [f"ma_{ma_window}", 'rsi'].
    """
    if 'close' not in df.columns:
        # If there's no 'close' column, nothing to compute
        return df

    # 1) Moving Average
    ma_col = f"ma_{ma_window}"
    df[ma_col] = df['close'].rolling(window=ma_window).mean()

    # 2) Classical RSI Calculation (simple moving average approach)
    #    RSI = 100 - (100 / (1 + RS)), RS = avg_gain / avg_loss
    #    avg_gain/avg_loss = simple rolling average of up/down moves
    delta = df['close'].diff()

    # Gains (positive deltas) and losses (negative deltas)
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)

    # Simple rolling average of gains/losses
    avg_gain = gain.rolling(window=rsi_period).mean()
    avg_loss = loss.rolling(window=rsi_period).mean()

    # Avoid division by zero
    rs = avg_gain / avg_loss.replace(0, np.nan)

    df['rsi'] = 100 - (100 / (1 + rs))

    # Fill NaNs by backward fill (Pandas 2.x compatible)
    df.bfill(inplace=True)

    return df
