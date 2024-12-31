import os
import pandas as pd
from coinbase.rest import RESTClient
from modules.utils import get_logger
from datetime import datetime, timezone, timedelta

logger = get_logger(__name__)

MINIMUM_DATE = datetime(2024, 12, 1, tzinfo=timezone.utc)  # Stop before 2020
DEFAULT_INTERVAL_HOURS = 8  # if not in config, use 8
DEFAULT_GRANULARITY_SECONDS = 60  # 1 minute

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

        # Initialize Coinbase client with the correct base URL
        base_url = self.config['coinbase'].get('base_url', 'https://api.coinbase.com')

        self.client = RESTClient(
            base_url=base_url,
            api_key=self.config['coinbase']['name'],
            api_secret=self.config['coinbase']['privateKey'],
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

            logger.debug(f"BID/ASK response for {product_id}: {response}")

            pricebooks = response['pricebooks']
            if not pricebooks:
                logger.error("No pricebooks in best_bid_ask response")
                return pd.DataFrame()

            first_pb = pricebooks[0]
            bid = first_pb["bids"][0]
            ask = first_pb["asks"][0]

            mid_price = (float(bid["price"]) + float(ask["price"])) / 2.0
            volume = float(bid["size"]) + float(ask["size"])

            data = {
                "time": datetime.utcnow(),
                "price": mid_price,
                "volume": volume
            }
            df = pd.DataFrame([data])
            
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
        - granularity: in seconds (defaults = 3600 = 1 hour). Mapped to a Coinbase string.

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

            # Pagination might be needed if the endpoint limits the # of results
            # For Coinbase Advanced Trade, you can chunk the range into smaller slices or
            # rely on a built-in pagination if available. This is a placeholder.
            
            candles = self.client.get_candles(
                product_id=product_id,
                start=str(start_ts),
                end=str(end_ts),
                granularity=gran_str
            )

            logger.debug(f"Candles response for {product_id}: {candles}")

            if (not candles) or (not candles['candles']):
                logger.debug("No candles returned, returning empty DataFrame.")
                return pd.DataFrame()

            rows = []
            for c in candles['candles']:
                rows.append({
                    "time": datetime.fromtimestamp(int(c['start']), tz=timezone.utc),
                    "low": float(c['low']),
                    "high": float(c['high']),
                    "open": float(c['open']),
                    "close": float(c['close']),
                    "volume": float(c['volume'])
                })

            df = pd.DataFrame(rows)
            df.sort_values("time", inplace=True, ignore_index=True)

            logger.info(f"Fetched {len(df)} candle records for {product_id}.")            

            file_path = f"data/historical_{product_id}.csv"
            self._append_data_to_file(df, file_path=file_path, time_col="time")

            return df

        except Exception as e:
            logger.exception(f"Error fetching historical data for {product_id}", exc_info=True)
            return pd.DataFrame()

    # -------------------------------------------------------------------------
    # NEW METHOD: get_historical_data_past_dates
    # -------------------------------------------------------------------------
    def get_historical_data_past_dates(self, product_id: str):
        """
        Incrementally fetch older historical data (in 8-hour chunks, 1-minute granularity)
        until reaching or crossing the date boundary or no more data is found.

        Flow:
          1) Determine the earliest 'time' we already have in data/historical_<product_id>.csv
             If file doesn't exist or is empty, assume 'now' as earliest.
          2) Start a loop that fetches data from (earliest - 8 hours) to earliest,
             with 1-minute granularity.
          3) Append results to the CSV, update 'earliest' to the new start,
             keep going until we cross boundary or an empty response is returned.

        The interval is read from config['historical']['past_interval_hours'] if present,
        otherwise defaults to 8 hours.

        In a real scenario, you might schedule this once at startup, or as needed
        to backfill older data.
        """
        file_path = f"data/historical_{product_id}.csv"
        interval_hours = self.config.get('historical', {}).get('past_interval_hours', DEFAULT_INTERVAL_HOURS)

        # 1) Determine earliest time in CSV or use now
        if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
            try:
                existing_df = pd.read_csv(file_path, parse_dates=['time'])
                if existing_df.empty:
                    # If CSV has headers but no rows
                    earliest_time = datetime.utcnow().replace(tzinfo=timezone.utc)
                else:
                    # earliest_time is the earliest row in ascending order
                    earliest_time = existing_df['time'].min()
            except Exception as e:
                logger.error(f"Could not read {file_path}: {e}, defaulting to now.")
                earliest_time = datetime.utcnow().replace(tzinfo=timezone.utc)
        else:
            earliest_time = datetime.utcnow().replace(tzinfo=timezone.utc)

        # 2) Start loop going backwards in time
        granularity_seconds = DEFAULT_GRANULARITY_SECONDS  # 1 min
        while True:
            if earliest_time <= MINIMUM_DATE:
                logger.info(f"Earliest time {earliest_time} <= {MINIMUM_DATE}. Stopping backfill.")
                break

            # compute next time window [next_start, earliest_time]
            next_start = earliest_time - timedelta(hours=interval_hours)
            if next_start < MINIMUM_DATE:
                # clamp to boundary
                next_start = MINIMUM_DATE

            logger.info(f"Backfilling older data for {product_id}: {next_start} -> {earliest_time} "
                        f"(granularity=ONE_MINUTE)")

            df = self.fetch_historical_data(
                product_id=product_id,
                start=next_start,
                end=earliest_time,
                granularity=granularity_seconds
            )

            # If no new data, break out
            if df.empty:
                logger.info("No (or empty) candle data returned. Stopping backfill.")
                break

            # Update earliest_time
            earliest_time = next_start

            # If we've reached or crossed 2020, break
            if earliest_time <= MINIMUM_DATE:
                logger.info(f"Reached the boundary ({MINIMUM_DATE}). Stopping.")
                break

        logger.info("Backfill of past dates completed.")
