import datetime
from modules.utils import get_logger

logger = get_logger(__name__)

"""
A class to handle the execution of different types of orders on a trading platform.
Attributes:
    config (dict): Configuration settings for the order executor.
    client (object): An instance of the Coinbase client to interact with the trading platform.
Methods:
    execute_market_order(product_id, side, size):
    execute_limit_order(product_id, side, limit_price, size):
    execute_stop_limit_order(product_id, side, stop_price, limit_price, size):
        Places a STOP LIMIT order that triggers once the last trade price hits stop_price,
    execute_bracket_order(product_id, side, entry_price, take_profit_price, stop_loss_price, size):
        Places a 'Bracket Order' with two triggers: take-profit and stop-loss.
"""
class OrderExecutor:
    def __init__(self, config, coinbase_client):
        self.config = config
        self.client = coinbase_client

    def execute_market_order(self, product_id, side, size):
        """
        Places a simple MARKET order (immediate or cancel).
        This might not handle risk management automatically.
        """
        logger.info(f"Placing MARKET {side} order on {product_id}, size={size}")
        # Example of the required parameters (pseudo-code):
        order_type = "market_market_ioc"  # immediate or cancel
        order_id = self.client.place_order(
            product_id=product_id,
            side=side,           # "BUY" or "SELL"
            order_type=order_type,
            base_size=size       # for SELL or BUY in base currency
        )
        return order_id

    def execute_limit_order(self, product_id, side, limit_price, size):
        """
        Places a LIMIT order (good-till-cancel by default).
        Still doesn't handle stop-loss or take-profit automatically.
        """
        logger.info(f"Placing LIMIT {side} order on {product_id}, price={limit_price}, size={size}")
        order_type = "limit_limit_gtc"
        order_id = self.client.place_order(
            product_id=product_id,
            side=side,
            order_type=order_type,
            base_size=size,
            limit_price=limit_price
        )
        return order_id

    def execute_stop_limit_order(self, product_id, side, stop_price, limit_price, size):
        """
        Places a STOP LIMIT order that triggers once last trade price hits stop_price,
        then places a limit order at limit_price.
        """
        logger.info(f"Placing STOP LIMIT {side} order on {product_id}, stop={stop_price}, limit={limit_price}, size={size}")
        order_type = "stop_limit_stop_limit_gtc"
        order_id = self.client.place_order(
            product_id=product_id,
            side=side,
            order_type=order_type,
            base_size=size,
            limit_price=limit_price,
            stop_price=stop_price
        )
        return order_id

    def execute_bracket_order(
        self, product_id, side, entry_price, take_profit_price, stop_loss_price, size
    ):
        """
        Places a 'Bracket Order' (trigger_bracket_*).
        This single order has two triggers: 
          - If price moves >= take_profit_price => SELL triggers (take-profit)
          - If price moves <= stop_loss_price   => SELL triggers (stop-loss)
        Whichever triggers first, the other side is canceled automatically.

        entry_price is relevant if you do a bracket for an entry as well, 
        but typically bracket orders are placed for an existing position. 
        (Implementation can vary based on how Coinbase structures bracket orders.)
        """
        logger.info(
            f"Placing BRACKET {side} order on {product_id}, "
            f"entry={entry_price}, TP={take_profit_price}, SL={stop_loss_price}, size={size}"
        )

        # GTC = Good 'Till Cancel
        # GTD = Good 'Till Date (requires specifying an expiration date)
        order_type = "trigger_bracket_gtc"

        order_id = self.client.place_order(
            product_id=product_id,
            side=side,
            order_type=order_type,
            base_size=size,
            # Pseudo-fields for bracket order
            entry_price=entry_price,         # (some bracket orders might not require entry price if you already hold a position)
            take_profit_price=take_profit_price,
            stop_price=stop_loss_price
            # The exact field names may differ in the real SDK
        )

        return order_id
