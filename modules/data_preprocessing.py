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

    # Filter out columns that do not exist
    columns_to_scale = [col for col in columns if col in df.columns]

    if not columns_to_scale:
        # No valid columns to scale
        return df, None

    if scaler is None:
        scaler = MinMaxScaler()

    df[columns_to_scale] = scaler.fit_transform(df[columns_to_scale])

    return df, scaler

def compute_indicators(df, ma_window=5, rsi_period=10):
    """
    Computes simple indicators like moving averages, RSI, etc.
    
    Args:
        df (pd.DataFrame): Input DataFrame with a 'close' column.
        ma_window (int): The window size for the moving average. Default = 5.
        rsi_period (int): The period for RSI calculation. Default = 10.
        
    Returns:
        pd.DataFrame: DataFrame with new columns ['ma_5', 'rsi'].
    """
    # 1) Moving average (e.g., 5-day)
    if 'close' in df.columns:
        ma_col = f'ma_{ma_window}'
        df[ma_col] = df['close'].rolling(window=ma_window).mean()

        # 2) RSI Calculation
        # Using exponential moving average (ema) for smoothed RSI
        delta = df['close'].diff()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)
        
        # Adjusting min_periods
        min_periods_adjusted = min(rsi_period, 10)

        avg_gain = gain.ewm(span=rsi_period, min_periods=min_periods_adjusted, adjust=False).mean()
        avg_loss = loss.ewm(span=rsi_period, min_periods=min_periods_adjusted, adjust=False).mean()

        #avg_gain = gain.ewm(span=rsi_period, min_periods=rsi_period).mean()
        #avg_loss = loss.ewm(span=rsi_period, min_periods=rsi_period).mean()

        rs = avg_gain / avg_loss
        df['rsi'] = 100 - (100 / (1 + rs))

        # Fill NaNs
        df.fillna(method='bfill', inplace=True)

    return df
