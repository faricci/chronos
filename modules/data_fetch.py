import pandas as pd
from coinbase.rest import RESTClient
from modules.utils import get_logger
from datetime import datetime, timedelta, timezone

logger = get_logger(__name__)

"""
DataFetcher is used to fetch data from data sources.

Attributes:
    config (dict): Configuration dictionary containing API keys and other settings.
    client (RESTClient): Initialized Coinbase REST client for API interactions.

Methods:
    fetch_realtime_data(product_id):
        Fetches current price and volume for the given product_id.

    fetch_historical_data(product_id, start, end, granularity=3600):
        Fetches historical candle data for the given product_id within the specified time range
        and granularity in seconds (default = 3600 = 1 hour).
"""

# ---------------------------------
# Helper to map integer "seconds" granularity to Coinbase strings
# This mapping may vary based on Coinbase docs for the RESTClient you’re using.
# Adjust as needed.
# ---------------------------------
def convert_granularity_to_coinbase_str(granularity_seconds: int) -> str:
    """
    Convert a numeric granularity (in seconds) to one of the official
    Coinbase strings, e.g. "ONE_MINUTE", "FIVE_MINUTE", "FIFTEEN_MINUTE",
    "ONE_HOUR", "SIX_HOUR", "ONE_DAY", etc.
    """
    if granularity_seconds <= 60:
        return "ONE_MINUTE"
    elif granularity_seconds <= 300:
        return "FIVE_MINUTE"
    elif granularity_seconds <= 900:
        return "FIFTEEN_MINUTE"
    elif granularity_seconds <= 3600:
        return "ONE_HOUR"
    elif granularity_seconds <= 21600:
        return "SIX_HOUR"
    else:
        return "ONE_DAY"  # default fallback


class DataFetcher:
    def __init__(self, config):
        self.config = config
        # Initialize Coinbase client
        self.client = RESTClient(
            api_key=self.config['coinbase']['api_key'],
            api_secret=self.config['coinbase']['api_secret'],
            timeout=10
        )
        # Potentially retrieve account info or limits:
        # logger.info(f"Account info: {self.client.get_accounts().to_dict()}")

    def fetch_realtime_data(self, product_id: str) -> pd.DataFrame:
        """
        Fetches current price and volume for the given product_id.
        Returns: DataFrame with columns [time, price, volume]
        """
        try:
            response = self.client.get_best_bid_ask(product_id)
            logger.info(f"Fetched realtime data for {product_id}. Response: {response}")

            data = {
                "time": datetime.utcnow(),
                "price": float(response['price']),
                "volume": float(response['volume_24h'])
            }
            df = pd.DataFrame([data])
            return df

        except Exception as e:
            logger.error(f"Error fetching bid/ask prices for {product_id}: {e}", exc_info=True)
            return pd.DataFrame()  # return empty DataFrame or None

    def fetch_historical_data(
        self, 
        product_id: str, 
        start: datetime, 
        end: datetime, 
        granularity: int = 3600
    ) -> pd.DataFrame:
        """
        Fetch historical candle data for the given product_id within [start, end].
        
        - start, end: Python datetime objects (UTC recommended).
        - granularity: in seconds (defaults to 3600 = 1 hour). Mapped to a Coinbase string.

        Returns: DataFrame with columns [time, open, high, low, close, volume]
                 sorted by time ascending.
        """
        try:
            # Convert Python datetimes to timestamps (ints in seconds)
            start_ts = int(start.timestamp())
            end_ts = int(end.timestamp())

            # Convert to a Coinbase-recognized granularity string
            gran_str = convert_granularity_to_coinbase_str(granularity)

            logger.info(f"Fetching historical data for {product_id} from {start} to {end}, "
                        f"granularity={granularity} ({gran_str})")

            candles = self.client.get_candles(
                product_id=product_id,
                start=str(start_ts),
                end=str(end_ts),
                granularity=gran_str
            )

            # Check response validity
            if (not candles) or ('candles' not in candles) or (not candles['candles']):
                raise ValueError(f"No candles returned for {product_id}")

            # Each element is typically [time, low, high, open, close, volume].
            # Adjust indexing as needed for your client library.
            rows = []
            for c in candles['candles']:
                # c[0] = epoch timestamp
                # c[1] = low
                # c[2] = high
                # c[3] = open
                # c[4] = close
                # c[5] = volume
                rows.append({
                    "time": datetime.fromtimestamp(c[0], tz=timezone.utc),
                    "low": float(c[1]),
                    "high": float(c[2]),
                    "open": float(c[3]),
                    "close": float(c[4]),
                    "volume": float(c[5])
                })

            df = pd.DataFrame(rows)
            # Sort by ascending time just in case the API returns descending
            df.sort_values("time", inplace=True, ignore_index=True)

            logger.info(f"Fetched {len(df)} candle records for {product_id}.")
            return df

        except Exception as e:
            logger.error(f"Error fetching historical data for {product_id}: {e}", exc_info=True)
            return pd.DataFrame()  # or None
