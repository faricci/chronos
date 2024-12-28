import os
import pandas as pd
from coinbase.rest import RESTClient
from modules.utils import get_logger
from datetime import datetime, timezone

logger = get_logger(__name__)

"""
DataFetcher is used to fetch data from data sources.

Attributes:
    config (dict): Configuration dictionary containing API keys and other settings.
    client (RESTClient): Initialized Coinbase REST client for API interactions.

Methods:
    fetch_realtime_data(product_id):
        Fetches current price and volume for the given product_id. 
        Appends results to data/realtime_<product_id>.csv

    fetch_historical_data(product_id, start, end, granularity=3600):
        Fetches historical candle data for the given product_id within the specified time range
        and granularity in seconds (default=3600=1 hour).
        Appends results to data/historical_<product_id>.csv
"""

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

        # Ensure the data folder exists
        os.makedirs("data", exist_ok=True)

    def _append_data_to_file(self, df: pd.DataFrame, file_path: str, time_col: str = "time"):
        """
        Internal helper: appends new data to an existing CSV or creates a new CSV if none exists.
        Duplicates (by time_col) are dropped.
        """
        if df.empty:
            logger.info("Received an empty DataFrame. Nothing to append.")
            return

        if os.path.exists(file_path):
            try:
                existing_df = pd.read_csv(file_path, parse_dates=[time_col])
                combined_df = pd.concat([existing_df, df], ignore_index=True)
                # Remove duplicates based on time column (if it exists in both)
                combined_df.drop_duplicates(subset=[time_col], inplace=True)
                # Sort by time (ascending) for consistency
                combined_df.sort_values(by=time_col, inplace=True)
                combined_df.to_csv(file_path, index=False)
                logger.info(f"Appended {len(df)} rows to {file_path}. Total rows now: {len(combined_df)}.")
            except Exception as e:
                logger.error(f"Error reading or writing to {file_path}: {e}", exc_info=True)
        else:
            # No existing file, just create it
            df.to_csv(file_path, index=False)
            logger.info(f"Created new dataset file {file_path} with {len(df)} rows.")

    def fetch_realtime_data(self, product_id: str) -> pd.DataFrame:
        """
        Fetches current price and volume for the given product_id.
        Returns: DataFrame with columns [time, price, volume]
        Also appends/creates a CSV file at data/realtime_<product_id>.csv
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

            # Append to local CSV file
            file_path = f"data/realtime_{product_id}.csv"
            self._append_data_to_file(df, file_path=file_path, time_col="time")

            return df

        except Exception as e:
            logger.error(f"Error fetching bid/ask prices for {product_id}: {e}", exc_info=True)
            return pd.DataFrame()

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

        Also appends/creates a CSV file at data/historical_<product_id>.csv
        """
        try:
            start_ts = int(start.timestamp())
            end_ts = int(end.timestamp())
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

            rows = []
            for c in candles['candles']:
                rows.append({
                    "time": datetime.fromtimestamp(c[0], tz=timezone.utc),
                    "low": float(c[1]),
                    "high": float(c[2]),
                    "open": float(c[3]),
                    "close": float(c[4]),
                    "volume": float(c[5])
                })

            df = pd.DataFrame(rows)
            df.sort_values("time", inplace=True, ignore_index=True)

            logger.info(f"Fetched {len(df)} candle records for {product_id}.")

            # Append to local CSV
            file_path = f"data/historical_{product_id}.csv"
            self._append_data_to_file(df, file_path=file_path, time_col="time")

            return df

        except Exception as e:
            logger.error(f"Error fetching historical data for {product_id}: {e}", exc_info=True)
            return pd.DataFrame()
