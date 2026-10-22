"""Coinbase Exchange PUBLIC market data, no credentials and no order submission."""
from math import isfinite
import time
import pandas as pd
import requests


class FeedError(Exception):
    pass


class CoinbaseFeed:

    def __init__(self, product = 'BTC-USD'):

        if product != 'BTC-USD':
            raise ValueError('Only BTC-USD supported.')
        
        self.base = f'https://api.exchange.coinbase.com/products/{product}'
        self.session = requests.Session()
        self.session.headers.update({'User-Agent':'BTC-paper-research/1.0'})

    def get(self, endpoint, params = None):

        try:
            response = self.session.get(self.base + endpoint, params=params, timeout=(3, 8))
            response.raise_for_status()
            return response.json()
        
        except (requests.RequestException, ValueError):
            raise FeedError('Market-data request failed; retry later.') from None

    def quote(self, now, max_age):

        data = self.get('/ticker')

        try:
            bid, ask = float(data['bid']), float(data['ask'])
            provider_time = pd.Timestamp(data['time'])
            age = (pd.Timestamp.now(tz='UTC') - provider_time).total_seconds()

            if not all(isfinite(v) and v > 0 for v in (bid,ask)) or bid > ask or not -5 <= age <= max_age:
                raise ValueError()
            
        except (KeyError, TypeError, ValueError):
            raise FeedError('Malformed, future, or stale quote; no fill allowed.') from None
        
        return {'bid': bid, 
                'ask': ask, 
                'provider_time': str(provider_time), 
                'received_at': str(pd.Timestamp.now(tz = 'UTC'))}

    def candles(self, start, end):
        """Paginate <= 299 hourly buckets/request, reject conflicting duplicates."""
        start, end = pd.Timestamp(start), pd.Timestamp(end)

        cursor, records = start, {}

        while cursor < end:
            finish = min(end, cursor + pd.Timedelta(hours=299))
            raw = self.get('/candles', {'start': cursor.isoformat(), 
                                        'end': finish.isoformat(), 
                                        'granularity': 3600})
            
            if not isinstance(raw, list):
                raise FeedError('Malformed candle response.')
            
            for row in raw:
                if not isinstance(row, list) or len(row) != 6:
                    raise FeedError('Malformed candle row.')
                
                try:
                    timestamp = pd.to_datetime(row[0], unit='s', utc=True)
                    values = [float(x) for x in row[1:]]

                except (TypeError, ValueError):
                    raise FeedError('Malformed candle values.') from None
                
                if not start <= timestamp < end:
                    continue

                if timestamp in records and records[timestamp] != values:
                    raise FeedError('Conflicting duplicated candle.')
                
                records[timestamp] = values

            cursor = finish

            if cursor < end:
                time.sleep(.4)

        if not records:
            raise FeedError('No candles returned.')
        
        frame = pd.DataFrame.from_dict(records, 
                                       orient = 'index', 
                                       columns=['Low','High','Open','Close','Volume']).sort_index()
        
        frame.index = pd.DatetimeIndex(frame.index)
        
        return frame
