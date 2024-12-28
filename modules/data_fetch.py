import datetime
import pandas as pd
from coinbase.rest import RESTClient
from modules.utils import get_logger
from datetime import datetime, timedelta, timezone

logger = get_logger(__name__)

"""
DataFetcher is used to fetch data from data sources
Attributes:
    config (dict): Configuration dictionary containing API keys and other settings.
    client (RESTClient): Initialized Coinbase REST client for API interactions.
Methods:
    fetch_realtime_data(product_id):
        Fetches current price and volume for the given product_id.
    fetch_historical_data(product_id, start, end, granularity=3600):
        Fetches historical candle data for the given product_id within the specified time range.
"""
class DataFetcher:
    def __init__(self, config):
        self.config = config
        # Initialize Coinbase client
        self.client = RESTClient(
            api_key=self.config['coinbase']['api_key'],
            api_secret=self.config['coinbase']['api_secret'],
            timeout=10
        )

        #TODO account limits can be retrieved directly via API
        #json.dump(self.client.get_accounts().to_dict(), f, indent=2)
                                 

    def fetch_realtime_data(self, product_id):
        """
        Fetches current price and volume for the given product_id
        """
        try:
            response = self.client.get_best_bid_ask(product_id)
            logger.info(f"Fetched realtime data. Received response: {response}")

            data = {
                "time": datetime.datetime.utcnow(),
                "price": response['price'],
                "volume": response['volume_24h']
            }
            df = pd.DataFrame([data])  # Create DataFrame from the response
            return df

        except Exception as e:
            print(f"Error fetching bid/ask prices: {e}")
            return None

    def fetch_historical_data(self, product_id, start, end, granularity=3600):
        """
        Fetch historical candle data
        """
        try:
            end_time = datetime.now()
            start_time = end_time - timedelta(minutes=1)
            print(f"Fetching data for {product_id} from {start_time} to {end_time}")
            candles = self.client.get_candles(
                product_id=product_id,
                start=str(int(start_time.timestamp())),  # Fixed Unix timestamp
                end=str(int(end_time.timestamp())),      # Fixed Unix timestamp
                granularity="ONE_MINUTE"  # 1  minute granularity
            )
            print("Candles:", candles)
            # Validate response
            if not candles or 'candles' not in candles or not candles['candles']:
                raise ValueError(f"No data returned for {product_id}")
        
            latest = candles['candles'][0]
            data = {
                "time": [end_time.astimezone(timezone.utc)],
                'open': float(latest[3]),
                'high': float(latest[2]),
                'low': float(latest[1]),
                'close': float(latest[4]),
                'volume': float(latest[5])
            }

            df = pd.DataFrame(data)
            return df
        
        except Exception as e:
            print(f"Error fetching data for {product_id}: {e}")
            return None
