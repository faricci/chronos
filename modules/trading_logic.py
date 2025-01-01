# modules/trading_logic.py

import torch
import torch.nn as nn
import pandas as pd
from transformers import InformerForPrediction

from modules.data_preprocessing import normalize_data, compute_indicators

class SimpleTransformer(nn.Module):
    """
    A slightly improved time-series Transformer that can forecast multiple future steps.

    Args:
        input_dim (int): Number of input features per timestep (e.g. close, volume, RSI, etc.).
        d_model (int): Dimension used by the transformer encoder.
        nhead (int): Number of heads in the multi-head attention mechanism.
        num_layers (int): Number of encoder layers stacked in the Transformer.
        output_dim (int): How many future steps to predict at once (1 = next step).
    """
    def __init__(self, input_dim=6, d_model=32, nhead=1, num_layers=2, output_dim=1):
        super().__init__()
        # An embedding layer to project input_dim -> d_model
        self.embedding = nn.Linear(input_dim, d_model)

        # A TransformerEncoder with the specified number of layers
        encoder_layer = nn.TransformerEncoderLayer(d_model=d_model, nhead=nhead)
        self.transformer_encoder = nn.TransformerEncoder(encoder_layer, num_layers=num_layers)

        # A final linear layer to map from d_model -> output_dim
        # e.g., output_dim=1 => single-step forecast, output_dim=N => multi-step forecast
        self.linear = nn.Linear(d_model, output_dim)

    def forward(self, x):
        """
        Args:
            x (torch.Tensor): Shape (batch_size, seq_len, input_dim)
        Returns:
            torch.Tensor: Shape (batch_size, output_dim) or (batch_size, seq_len, output_dim) 
                          depending on your design.
        """
        # x => (batch_size, seq_len, input_dim)
        # Step 1: Project input to d_model
        x = self.embedding(x)  # (batch_size, seq_len, d_model)

        # Step 2: Transformers usually want shape (seq_len, batch_size, d_model)
        x = x.permute(1, 0, 2)  # => (seq_len, batch_size, d_model)

        # Step 3: Pass through the transformer encoder
        transformed = self.transformer_encoder(x)  # => (seq_len, batch_size, d_model)

        # Step 4: We can either take the last timestep or entire sequence
        # If we only want the last hidden state for a 1-step forecast:
        last_hidden = transformed[-1, :, :]  # shape => (batch_size, d_model)

        # Step 5: Map to output_dim
        out = self.linear(last_hidden)  # => (batch_size, output_dim)
        return out

class TradingLogic:
    def __init__(self, config, local_model_path="./model_checkpoints"):
        """
        Load the fine-tuned Informer (or Autoformer, etc.) from local path or HF Hub.
        """
        self.config = config
        self.context_length = config.get("model", {}).get("context_length", 64)
        self.prediction_length = config.get("model", {}).get("prediction_length", 1)
        self.threshold_up = config.get("trading_logic", {}).get("threshold_up", 0.01)
        self.threshold_down = config.get("trading_logic", {}).get("threshold_down", -0.01)

        print(f"Loading time-series model from {local_model_path}...")
        # Either from local dir or HF name
        self.model = InformerForPrediction.from_pretrained(local_model_path)
        self.model.eval()

    def generate_signal(self, data_df: pd.DataFrame):
        """
        data_df: Must contain enough columns for the model's input, e.g. [open, high, low, close, volume].
        We'll do minimal example for demonstration.
        """
        if len(data_df) < self.context_length:
            return "HOLD"

        # 1) Preprocess (normalize, compute indicators, etc.)
        data_df, _ = normalize_data(data_df)
        data_df = compute_indicators(data_df)

        # 2) Extract the last `context_length` rows
        window_df = data_df.iloc[-self.context_length:].copy()

        # Typically, you'd build the time-series features the model expects,
        # e.g. 'past_values'. For demonstration, let's assume 'close' is your main target.
        past_values = window_df["close"].values  # shape=(context_length,)

        # Convert to torch. Expect shape (batch_size=1, context_length, 1)
        past_tensor = torch.tensor(past_values, dtype=torch.float).unsqueeze(0).unsqueeze(-1)

        with torch.no_grad():
            # The InformerForPrediction forward signature might differ;
            # Typically you pass "past_values=past_tensor" and get "predictions".
            # We'll show a conceptual approach:
            outputs = self.model(
                past_values=past_tensor, 
                future_values=None  # we don't have them at inference
            )
            # Usually outputs.predictions => shape (batch_size=1, prediction_length)
            preds = outputs.predictions[0].numpy()  # => shape (prediction_length,)

        # Suppose preds is next 1 or next N future steps
        # If prediction_length=1 => single-step
        final_value = float(preds.mean())  # if multiple steps, we do average

        # 3) Trading logic
        if final_value > self.threshold_up:
            return "BUY"
        elif final_value < self.threshold_down:
            return "SELL"
        else:
            return "HOLD"
