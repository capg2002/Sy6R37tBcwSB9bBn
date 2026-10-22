"""Frozen-input regression check INCLUDING a SQLite commit after every candle."""

# Import frozen-input loading, numerical comparisons, and the shared backtest storage and engine.
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

import trading.engine as engine

from trading.execution import ExecutionModel
from trading.models import EngineState
from services.storage import SQLiteStore, new_portfolio
from market.features import add_features
from market.missing_data import prepare_hourly_data
from services.config_validation import validate_config


# Expose a saved forecast row through the interface expected by the trading engine.
class SavedEstimate:

    # Copy the saved row fields into estimate attributes.
    def __init__(self, row):
        self.__dict__.update(row.to_dict())

    # Return the estimate attribute dictionary for serialization by the engine.
    def to_dict(self):
        return self.__dict__


# Replay saved data and forecasts with database commits, then compare the resulting trades.
def main():

    # ------------------------------------------------------------------
    # Command-line arguments
    # ------------------------------------------------------------------

    # Define the input data, saved forecasts, expected trades, configuration, and regression database paths.
    p = argparse.ArgumentParser()

    p.add_argument('--data', required=True)

    p.add_argument('--forecasts', required=True)

    p.add_argument('--expected-trades', required=True)

    p.add_argument('--config', default='config.json')

    p.add_argument('--db', default='runtime/regression.sqlite')

    a = p.parse_args()

    # ------------------------------------------------------------------
    # Load and validate configuration
    # ------------------------------------------------------------------

    # Load and validate the configuration before reconstructing the regression run.
    c = json.loads(Path(a.config).read_text())

    validate_config(c)

    # ------------------------------------------------------------------
    # Load market data and forecasts
    # ------------------------------------------------------------------

    # Load saved market data, recompute features, and read timestamp-indexed forecasts.
    raw = pd.read_csv(a.data, index_col=0, parse_dates=True)

    frame = add_features(prepare_hourly_data(raw), c)

    forecasts = pd.read_csv(a.forecasts, index_col='candle_open', parse_dates=True)

    # Reject duplicate forecast timestamps so each candle maps to exactly one saved estimate.
    if not forecasts.index.is_unique:
        raise ValueError('Duplicate forecast origins.')

    # ------------------------------------------------------------------
    # Replace live forecasting with frozen forecasts
    # ------------------------------------------------------------------

    # Return the saved estimate corresponding to the latest candle in the supplied history.
    def frozen_forecast(history, **kwargs):
        return SavedEstimate(forecasts.loc[history.index[-1]])

    # Replace the engine forecast function with the frozen-input adapter for this regression process.
    engine.forecast_volatility = frozen_forecast

    # ------------------------------------------------------------------
    # Record hashes of regression inputs
    # ------------------------------------------------------------------

    # Hash both input files to bind the regression checkpoint to the exact data and forecasts.
    context = {
        key: hashlib.sha256(Path(path).read_bytes()).hexdigest()
        for key, path in [('data', a.data), ('forecasts', a.forecasts)]
    }

    # ------------------------------------------------------------------
    # Initialize SQLite storage
    # ------------------------------------------------------------------

    # Initialize or validate regression storage and restore its committed account state.
    store = SQLiteStore(a.db)

    store.initialize(c, new_portfolio(c), EngineState(), 'backtest', context)

    portfolio, state, revision = store.restore()

    execution = ExecutionModel(**c['execution'])

    # ------------------------------------------------------------------
    # Process candles
    # ------------------------------------------------------------------

    # Replay each candle chronologically with history limited to that candle and its predecessors.
    for i, (timestamp, candle) in enumerate(frame.iterrows()):

        key = 'candle:' + timestamp.isoformat()

        # Skip candles already committed to this regression database.
        if store.contains(key):
            continue

        # Process the next candle with frozen forecasts and commit its resulting state.
        result = engine.process_candle(timestamp, candle, frame.iloc[: i + 1], i, portfolio, state, execution, c)

        revision = store.commit(key, portfolio, state, result, revision)

        # Exercise restart round-trips
        # Restore the checkpoint every 500 rows to exercise persistence round-trips during the replay.
        if i % 500 == 0:
            portfolio, state, revision = store.restore()

    # ------------------------------------------------------------------
    # Compare actual trades against expected trades
    # ------------------------------------------------------------------

    # Build the actual trade table and load the expected reference trades.
    actual = pd.DataFrame(portfolio.trades)

    expected = pd.read_csv(a.expected_trades)

    # Fail immediately if the actual and expected trade counts differ.
    if len(actual) != len(expected):
        raise AssertionError(f'Trade count differs: {len(actual)} vs {len(expected)}')

    # ------------------------------------------------------------------
    # Compare individual trade fields
    # ------------------------------------------------------------------

    # Compare individual trade fields only when the run produced trades.
    if len(actual):

        # Compare strategy and side columns exactly.
        for key in ['strategy', 'side']:

            # Reject any mismatch in the current categorical trade field.
            if not actual[key].equals(expected[key]):
                raise AssertionError(key + ' differs')

        # Normalize trade timestamps to UTC and require an exact match.
        if not pd.to_datetime(actual.timestamp, utc=True).equals(pd.to_datetime(expected.timestamp, utc=True)):
            raise AssertionError('Trade timestamps differ')

        # Compare prices, BTC amounts, fees, and USD amounts within strict numerical tolerances.
        for key in ['btc_price', 'btc_amount', 'fee_usd', 'usd_amount']:

            np.testing.assert_allclose(actual[key], expected[key], rtol=1e-12, atol=1e-10, err_msg=key)

    # ------------------------------------------------------------------
    # Success
    # ------------------------------------------------------------------

    # Print success only after every comparison passes, then close the regression store.
    print('PASS:', len(actual), 'trades match; SQLite state round-trips preserve results.')

    store.close()


# Run the frozen-input regression check only when executed directly.
if __name__ == '__main__':
    main()
