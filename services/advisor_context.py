"""Build a small, JSON-only view. These functions never modify trading objects."""
# Import hashing, JSON, numeric validation, and candle-time utilities.
import hashlib
import json
import math
from datetime import timedelta


# Convert a value to a finite float, returning None when conversion is unavailable.
def finite(value):

    # Attempt numeric conversion before checking for infinity or NaN.
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    
    # Treat nonnumeric or incompatible values as unavailable data.
    except (TypeError, ValueError):
        return None


# Summarize the latest indicators and valid one-hour and 24-hour price returns.
def market_summary(frame):

    # Select the latest candle and normalize the required indicators to finite values.
    candle = frame.iloc[-1]

    result = {key: finite(candle.get(key)) for key in
              ('Close', 'ATR', 'SMA_20', 'SMA_50', 'PREV_SMA_20', 'PREV_SMA_50')}
    
    # Calculate each return horizon only from a complete sequence of observed hourly candles.
    for hours in (1, 24):

        tail = frame.tail(hours+1)
        # Require the expected row count, observed OHLC values, and exactly one hour between timestamps.
        valid = (len(tail) == hours+1 and bool(tail['observed_ohlc'].all())
                 and all((b-a).total_seconds() == 3600
                         for a, b in zip(tail.index, tail.index[1:])))
        first = finite(tail.iloc[0]['Close']) if valid else None

        # Calculate the fractional price change only when a valid positive starting price exists.
        result[f'return_{hours}h_fraction'] = (
            result['Close']/first-1 if first and first > 0 and result['Close'] else None)
        
    # Count unavailable observations and retain the latest candle observation flag.
    result['missing_or_invalid_hours_in_window'] = int((~frame['observed_ohlc']).sum())
    result['observed_ohlc'] = bool(candle['observed_ohlc'])
    
    return result


# Build a detached advisory snapshot with explicit market and account timing.
def advisory_context(timestamp, market, estimate, portfolio, state, config):
    """Portfolio is the saved decision-time state; price is the completed close.

    Quote monitoring may already have processed a newer quote while GARCH fitted.
    Explicitly distinguish these times; this is not an exact close-time account.
    """
    # Extract forecast volatility and hash the full configuration for audit identification.
    sigma = finite(getattr(estimate, 'sigma_next_hour', None))
    position = state.active_position
    digest = hashlib.sha256(json.dumps(config, sort_keys=True, allow_nan=False,
                                      separators=(',', ':')).encode()).hexdigest()
    # Assemble JSON-compatible market, portfolio, volatility, and rule information.
    return {
        'schema_version': 1,
        'candle_open': timestamp.isoformat(),
        'candle_close': (timestamp+timedelta(hours=1)).isoformat(),
        'config_hash': digest,
        'current_mode': config['strategy_mode'],
        'market': dict(market),
        'garch': {
            'status': getattr(estimate, 'status', 'unavailable'),
            'sigma_next_hour_fraction': sigma,
            'horizon_hours': 1,
            'reference_hourly_sigma': config['risk']['reference_hourly_sigma'],
        },
        'portfolio_at_decision': {
            'cash': finite(portfolio.cash),
            'dca_cash': finite(portfolio.dca_cash),
            'swing_cash': finite(portfolio.swing_cash),
            'reserve_cash': finite(portfolio.reserve_cash),
            'dca_btc': finite(portfolio.dca_btc),
            'swing_btc': finite(portfolio.active_btc),
            'active_stop': finite(position.stop_loss) if position else None,
            'last_quote_time': state.last_quote_time.isoformat() if state.last_quote_time is not None else None,
            'execution_path_uncertain': bool(state.execution_path_uncertain),
            'pending_dca_buy': state.pending_dca is not None,
            'pending_swing_buy': state.pending_swing_buy is not None,
            'pending_swing_sell': state.pending_swing_sell is not None,
        },
        'rule_settings': {
            'swing_crossover': config['swing']['crossover'],
            'dca_drop_fraction': config['dca']['drop_threshold'],
            'garch_sizing_enabled': config['risk']['garch_sizing_enabled'],
            'garch_trailing_enabled': config['exits']['garch_trailing_enabled'],
        },
    }
