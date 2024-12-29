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
    execute_bracket_order(product_id, side, entry_price, take_profit_price, stop_loss_price, size):
    execute_order_with_risk_management(product_id, side, current_price, size):
        Automatically applies risk management parameters (stop-loss, take-profit).
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
        order_type = "market_market_ioc"
        order_id = self.client.place_order(
            product_id=product_id,
            side=side,           
            order_type=order_type,
            base_size=size       
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

        order_type = "trigger_bracket_gtc"  # or "trigger_bracket_gtd" if you prefer an expiration date

        order_id = self.client.place_order(
            product_id=product_id,
            side=side,
            order_type=order_type,
            base_size=size,
            # Pseudo-fields for bracket order
            entry_price=entry_price,
            take_profit_price=take_profit_price,
            stop_price=stop_loss_price
        )

        return order_id

    # -----------------------------------------------------------------
    # NEW METHOD: Use stop_loss_percent & take_profit_percent from config
    # -----------------------------------------------------------------
    def execute_order_with_risk_management(self, product_id, side, current_price, size):
        """
        Places a bracket order (take-profit & stop-loss) automatically, 
        using risk_management config parameters.

        Example:
          - If stop_loss_percent = 0.02 => stop_loss_price = current_price * (1 - 0.02)
          - If take_profit_percent = 0.05 => take_profit_price = current_price * (1 + 0.05)
        """
        stop_loss_pct = self.config["risk_management"]["stop_loss_percent"]
        take_profit_pct = self.config["risk_management"]["take_profit_percent"]

        take_profit_price = current_price * (1 + take_profit_pct)
        stop_loss_price = current_price * (1 - stop_loss_pct)

        logger.info(f"Placing bracket order with risk management: side={side}, "
                    f"entry_price={current_price}, TP={take_profit_price}, SL={stop_loss_price}")

        return self.execute_bracket_order(
            product_id=product_id,
            side=side,
            entry_price=current_price,
            take_profit_price=take_profit_price,
            stop_loss_price=stop_loss_price,
            size=size
        )
