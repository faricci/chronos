# tests/test_order_execution.py

import unittest
from unittest.mock import MagicMock, patch
from modules.order_execution import OrderExecutor

"""
Unit tests for the OrderExecutor class.
Uses mocking to avoid real Coinbase API calls.
If you want to test real orders (e.g., DOGE-EUR),
you can adapt a separate integration test with a real client and a test account.
"""

class TestOrderExecutor(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # Minimal config for risk management
        cls.config = {
            "risk_management": {
                "stop_loss_percent": 0.02,
                "take_profit_percent": 0.05
            }
        }
        # Mock coinbase_client
        cls.mock_client = MagicMock()

        # Create OrderExecutor instance
        cls.executor = OrderExecutor(config=cls.config, coinbase_client=cls.mock_client)

    def setUp(self):
        # Just reset the mock in setUp, so each test sees a fresh call count
        self.mock_client.reset_mock()

    def test_get_fills_for_order(self):
        order_id = "mock_order_id"
        expected_fills = [
            {"trade_id": "1", "product_id": "DOGE-EUR", "price": "0.10", "size": "5"},
            {"trade_id": "2", "product_id": "DOGE-EUR", "price": "0.11", "size": "5"}
        ]

        self.mock_client.get_fills.return_value = expected_fills

        fills = self.mock_client.get_fills(order_id=order_id)
        self.assertEqual(fills, expected_fills)

        self.mock_client.get_fills.assert_called_once_with(order_id=order_id)

    def test_execute_market_order(self):
        # Simulate placing a market order
        product_id = "DOGE-EUR"
        side = "BUY"
        size = 10

        # Mock the return value from place_order
        self.mock_client.place_order.return_value = "mock_order_id"
        
        order_id = self.executor.execute_market_order(product_id, side, size)
        self.assertEqual(order_id, "mock_order_id")

        self.mock_client.place_order.assert_called_once_with(
            product_id=product_id,
            side=side,
            order_type="market_market_ioc",
            base_size=size
        )

    def test_execute_limit_order(self):
        product_id = "DOGE-EUR"
        side = "SELL"
        limit_price = 0.12
        size = 5

        self.mock_client.place_order.return_value = "limit_order_id"

        order_id = self.executor.execute_limit_order(product_id, side, limit_price, size)
        self.assertEqual(order_id, "limit_order_id")

        self.mock_client.place_order.assert_called_once_with(
            product_id=product_id,
            side=side,
            order_type="limit_limit_gtc",
            base_size=size,
            limit_price=limit_price
        )

    def test_execute_stop_limit_order(self):
        product_id = "DOGE-EUR"
        side = "SELL"
        stop_price = 0.10
        limit_price = 0.09
        size = 100

        self.mock_client.place_order.return_value = "stop_limit_id"

        order_id = self.executor.execute_stop_limit_order(
            product_id, side, stop_price, limit_price, size
        )
        self.assertEqual(order_id, "stop_limit_id")

        self.mock_client.place_order.assert_called_once_with(
            product_id=product_id,
            side=side,
            order_type="stop_limit_stop_limit_gtc",
            base_size=size,
            limit_price=limit_price,
            stop_price=stop_price
        )

    def test_execute_bracket_order(self):
        product_id = "DOGE-EUR"
        side = "SELL"
        entry_price = 0.15
        take_profit_price = 0.20
        stop_loss_price = 0.12
        size = 50

        self.mock_client.place_order.return_value = "bracket_order_id"

        order_id = self.executor.execute_bracket_order(
            product_id=product_id,
            side=side,
            entry_price=entry_price,
            take_profit_price=take_profit_price,
            stop_loss_price=stop_loss_price,
            size=size
        )
        self.assertEqual(order_id, "bracket_order_id")

        self.mock_client.place_order.assert_called_once_with(
            product_id=product_id,
            side=side,
            order_type="trigger_bracket_gtc",
            base_size=size,
            entry_price=entry_price,
            take_profit_price=take_profit_price,
            stop_price=stop_loss_price
        )

    def test_execute_order_with_risk_management(self):
        product_id = "DOGE-EUR"
        side = "BUY"
        current_price = 0.08
        size = 100

        self.mock_client.place_order.return_value = "risk_managed_id"

        # Expected stop_loss = current_price * (1 - 0.02) = 0.0784
        # Expected take_profit = current_price * (1 + 0.05) = 0.084
        order_id = self.executor.execute_order_with_risk_management(
            product_id=product_id,
            side=side,
            current_price=current_price,
            size=size
        )
        self.assertEqual(order_id, "risk_managed_id")

        # Check the bracket order call
        # bracket_order => entry_price = 0.08, take_profit_price=0.084, stop_price=0.0784
        self.mock_client.place_order.assert_called_once()
        called_kwargs = self.mock_client.place_order.call_args[1]  # get the second param: {key: val}
        self.assertAlmostEqual(called_kwargs['take_profit_price'], 0.084, places=4)
        self.assertAlmostEqual(called_kwargs['stop_price'], 0.0784, places=4)

        
    @staticmethod
    def suite():
        """
        Optional static method to create a test suite for this class alone.
        You can call this if you want to run only these tests in isolation.
        """
        suite = unittest.TestSuite()
        suite.addTest(TestOrderExecutor("test_get_fills_for_order"))
        suite.addTest(TestOrderExecutor("test_execute_market_order"))
        suite.addTest(TestOrderExecutor("test_execute_limit_order"))
        suite.addTest(TestOrderExecutor("test_execute_stop_limit_order"))
        suite.addTest(TestOrderExecutor("test_execute_bracket_order"))
        suite.addTest(TestOrderExecutor("test_execute_order_with_risk_management"))

        return suite


# If you want to run ONLY this file’s tests directly:
# if __name__ == "__main__":
#     runner = unittest.TextTestRunner(verbosity=2)
#     runner.run(TestOrderExecutor.suite())
