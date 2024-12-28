import torch
import torch.nn as nn
import numpy as np
from modules.data_preprocessing import normalize_data, compute_indicators

"""
Extremely simplified time-series Transformer model for trading logic.
Args:
    input_dim (int): Number of input features. Default is 6.
    d_model (int): Dimension of the model. Default is 32.
    nhead (int): Number of heads in the multiheadattention models. Default is 1.
    num_layers (int): Number of sub-encoder-layers in the encoder. Default is 2.
    output_dim (int): Dimension of the output. Default is 1.
Methods:
    forward(x):
        Forward pass of the transformer model.
        Args:
            x (torch.Tensor): Input tensor of shape (batch_size, seq_len, input_dim).
        Returns:
            torch.Tensor: Output tensor of shape (batch_size, output_dim).
"""
class SimpleTransformer(nn.Module):
    """
    Extremely simplified time-series Transformer
    (In practice, you'd have more layers, heads, etc.)
    """
    def __init__(self, input_dim=6, d_model=32, nhead=1, num_layers=2, output_dim=1):
        super().__init__()
        encoder_layer = nn.TransformerEncoderLayer(d_model=d_model, nhead=nhead)
        self.transformer_encoder = nn.TransformerEncoder(encoder_layer, num_layers=num_layers)
        self.linear = nn.Linear(d_model, output_dim)
        self.embedding = nn.Linear(input_dim, d_model)

    def forward(self, x):
        # x shape: (batch_size, seq_len, input_dim)
        # We need (seq_len, batch_size, d_model) for PyTorch's transformer
        x = self.embedding(x)  # (batch_size, seq_len, d_model)
        x = x.permute(1, 0, 2) # (seq_len, batch_size, d_model)
        transformed = self.transformer_encoder(x)  # (seq_len, batch_size, d_model)
        # take last seq element
        out = transformed[-1, :, :]  # (batch_size, d_model)
        out = self.linear(out)       # (batch_size, output_dim)
        return out

class TradingLogic:
    def __init__(self, config, model):
        self.config = config
        self.model = model
        self.threshold_up = 0.01
        self.threshold_down = -0.01

    def generate_signal(self, data_df):
        """
        data_df: Pandas DataFrame with time-series data.
        Return: buy/sell/hold
        """
        # Preprocess
        data_df, _ = normalize_data(data_df)
        data_df = compute_indicators(data_df)
        # Create input tensor
        seq_data = torch.tensor(data_df[['close', 'volume', 'ma_5', 'rsi']].values, dtype=torch.float32).unsqueeze(0)
        # shape = (1, seq_len, input_dim)
        with torch.no_grad():
            prediction = self.model(seq_data).item()
        # Simple logic: if > 0 => price goes up, else down
        if prediction > self.threshold_up:
            return "BUY"
        elif prediction < self.threshold_down:
            return "SELL"
        else:
            return "HOLD"
