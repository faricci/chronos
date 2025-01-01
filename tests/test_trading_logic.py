# tests/test_trading_logic.py

import unittest
import torch
import pandas as pd
from unittest.mock import patch, MagicMock

from modules.trading_logic import SimpleTransformer, TradingLogic
from modules.data_preprocessing import normalize_data, compute_indicators

class TestSimpleTransformer(unittest.TestCase):
    """
    Tests for the SimpleTransformer class.
    """

    def test_forward_pass(self):
        # Create a SimpleTransformer with known dimensions
        model = SimpleTransformer(input_dim=4, d_model=16, nhead=1, num_layers=1, output_dim=2)

        # Synthetic input: batch_size=2, seq_len=5, input_dim=4
        x = torch.randn(2, 5, 4)  # shape = (batch_size, seq_len, input_dim)

        # Forward pass
        with torch.no_grad():
            out = model(x)

        # Expected shape = (batch_size=2, output_dim=2)
        self.assertEqual(out.shape, (2, 2), "Output shape should match (batch_size, output_dim).")

        # No actual numeric check, just ensuring it runs and shapes are correct
        self.assertFalse(torch.isnan(out).any(), "Output shouldn't contain NaNs.")

        
    @staticmethod
    def suite():
        """
        Optional static method to create a test suite for this class alone.
        You can call this if you want to run only these tests in isolation.
        """
        suite = unittest.TestSuite()
        suite.addTest(TestSimpleTransformer("test_forward_pass"))

        return suite


@patch("modules.trading_logic.InformerForPrediction")
class TestTradingLogic(unittest.TestCase):
    """
    Tests for TradingLogic class that loads an InformerForPrediction (mocked).
    """

    def setUp(self):
        # Minimal config
        self.config = {
            "model": {
                "context_length": 3,
                "prediction_length": 1
            },
            "trading_logic": {
                "threshold_up": 0.01,
                "threshold_down": -0.01
            }
        }

    def test_generate_signal_hold_due_to_insufficient_data(self, mock_informer_class):
        """
        If the DataFrame is too short, TradingLogic should return HOLD.
        """
        # Mock the loaded HF model so it doesn't need a real checkpoint
        mock_informer = MagicMock()
        # If forward is called, we'll ensure we don't crash
        mock_informer.forward.return_value = None
        # The from_pretrained constructor returns this mock
        mock_informer_class.from_pretrained.return_value = mock_informer

        # Initialize TradingLogic
        logic = TradingLogic(self.config, local_model_path="./fake_model_dir")

        # Provide a DataFrame shorter than context_length=3
        df_too_short = pd.DataFrame({
            "close": [100.0, 101.0],
            "volume": [10, 12],
            "open": [99.0, 101.5],   # not strictly needed, but any columns
            "high": [102.5, 103.0],
            "low": [98.5, 100.0]
        })

        result = logic.generate_signal(df_too_short)
        self.assertEqual(result, "HOLD", "Should return HOLD if data is insufficient.")

    def test_generate_signal_buy(self, mock_informer_class):
        """
        If the model output is > threshold_up => BUY
        """
        # Mock the HF model
        mock_informer = MagicMock()
        # Suppose the model predictions => [0.02], which is above threshold_up=0.01
        mock_informer.return_value = None
        # We fake the output as if .predictions[0].numpy() => [0.02]
        mock_outputs = MagicMock()
        mock_outputs.predictions = torch.tensor([[0.02]])  # shape=(1,1)
        # We'll mock .__call__ to return this
        mock_informer.__call__.return_value = mock_outputs

        mock_informer_class.from_pretrained.return_value = mock_informer

        logic = TradingLogic(self.config, local_model_path="./fake_model_dir")

        # Provide a DataFrame with at least 3 rows
        df_ok = pd.DataFrame({
            "close": [100.0, 101.0, 102.0, 103.0],
            "volume": [10, 12, 11, 9],
            "open": [99.0, 101.5, 101.0, 102.5],
            "high": [102.5, 103.0, 103.5, 104.0],
            "low": [98.5, 100.0, 100.5, 101.5]
        })
        # Ensure it's at least as long as context_length=3
        result = logic.generate_signal(df_ok)
        self.assertEqual(result, "BUY", "Model output above threshold => BUY signal")

    def test_generate_signal_sell(self, mock_informer_class):
        """
        If the model output is < threshold_down => SELL
        """
        mock_informer = MagicMock()
        # We'll return a negative forecast => e.g. -0.05 => below threshold_down=-0.01
        mock_outputs = MagicMock()
        mock_outputs.predictions = torch.tensor([[-0.05]])  # shape=(1,1)
        mock_informer.__call__.return_value = mock_outputs
        mock_informer_class.from_pretrained.return_value = mock_informer

        logic = TradingLogic(self.config, local_model_path="./fake_model_dir")

        df_ok = pd.DataFrame({
            "close": [100.0, 101.0, 102.0, 103.0],
            "volume": [10, 12, 11, 9],
            "open": [99.0, 101.5, 101.0, 102.5],
            "high": [102.5, 103.0, 103.5, 104.0],
            "low": [98.5, 100.0, 100.5, 101.5]
        })
        result = logic.generate_signal(df_ok)
        self.assertEqual(result, "SELL", "Model output below threshold => SELL signal")

    def test_generate_signal_hold_in_between(self, mock_informer_class):
        """
        If the model output is between threshold_down and threshold_up => HOLD
        """
        mock_informer = MagicMock()
        # Suppose the forecast is 0.0 => within [-0.01, 0.01]
        mock_outputs = MagicMock()
        mock_outputs.predictions = torch.tensor([[0.0]])  # shape=(1,1)
        mock_informer.__call__.return_value = mock_outputs
        mock_informer_class.from_pretrained.return_value = mock_informer

        logic = TradingLogic(self.config, local_model_path="./fake_model_dir")

        df_ok = pd.DataFrame({
            "close": [99.0, 99.5, 100.0, 100.5],
            "volume": [5, 6, 7, 8],
            "open": [98.0, 99.0, 99.5, 100.0],
            "high": [100.0, 100.5, 101.0, 101.5],
            "low": [97.5, 98.5, 99.0, 99.5]
        })
        result = logic.generate_signal(df_ok)
        self.assertEqual(result, "HOLD", "Model output within threshold => HOLD")

    @staticmethod
    def suite():
        """
        Optional static method to create a test suite for this class alone.
        You can call this if you want to run only these tests in isolation.
        """
        suite = unittest.TestSuite()
        suite.addTest(TestTradingLogic("test_generate_signal_hold_due_to_insufficient_data"))
        suite.addTest(TestTradingLogic("test_generate_signal_buy"))
        suite.addTest(TestTradingLogic("test_generate_signal_sell"))
        suite.addTest(TestTradingLogic("test_generate_signal_hold_in_between"))

        return suite

# If you want to run ONLY this file’s tests directly:
# if __name__ == "__main__":
#     runner = unittest.TextTestRunner(verbosity=2)
#     runner.run(TestTradingLogic.suite())
#     runner.run(TestSimpleTransformer.suite())