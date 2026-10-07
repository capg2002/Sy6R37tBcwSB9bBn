"""Completed-candle features, using the project's existing ATR definition."""
import pandas as pd
from market.indicators import calculate_atr, calculate_sma


def add_features(data, config):
    data = data.copy()
    data['ATR'] = float('nan')
    groups = (~data['observed_ohlc']).cumsum()
    for _, segment in data[data.observed_ohlc].groupby(groups):
        calculated = calculate_atr(segment.copy(), config['atr']['period'])
        data.loc[segment.index, 'ATR'] = calculated['ATR']
    for period in (20, 50):
        name = f'SMA_{period}'
        data[name] = calculate_sma(data, period)
        data['PREV_' + name] = data[name].shift(1)
    return data


def compute_estimate(close_history, config, seed):
    # Imported inside the process so fitting cannot block the quote-monitor loop.
    from market.volatility import forecast_volatility
    c = config['garch']
    return forecast_volatility(close_history, min_observed_returns=c['min_observed_returns'],
                               n_paths=c['n_paths'], seed=seed)
