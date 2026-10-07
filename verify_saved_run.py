"""Frozen-input regression check INCLUDING a SQLite commit after every candle."""

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


class SavedEstimate:

    def __init__(self, row):
        self.__dict__.update(row.to_dict())

    def to_dict(self):
        return self.__dict__


def main():

    # ------------------------------------------------------------------
    # Command-line arguments
    # ------------------------------------------------------------------

    p = argparse.ArgumentParser()

    p.add_argument(
        '--data',
        required=True
    )

    p.add_argument(
        '--forecasts',
        required=True
    )

    p.add_argument(
        '--expected-trades',
        required=True
    )

    p.add_argument(
        '--config',
        default='config.json'
    )

    p.add_argument(
        '--db',
        default='runtime/regression.sqlite'
    )

    a = p.parse_args()


    # ------------------------------------------------------------------
    # Load and validate configuration
    # ------------------------------------------------------------------

    c = json.loads(
        Path(a.config).read_text()
    )

    validate_config(c)


    # ------------------------------------------------------------------
    # Load market data and forecasts
    # ------------------------------------------------------------------

    raw = pd.read_csv(
        a.data,
        index_col=0,
        parse_dates=True
    )

    frame = add_features(
        prepare_hourly_data(raw),
        c
    )

    forecasts = pd.read_csv(
        a.forecasts,
        index_col='candle_open',
        parse_dates=True
    )

    if not forecasts.index.is_unique:
        raise ValueError('Duplicate forecast origins.')


    # ------------------------------------------------------------------
    # Replace live forecasting with frozen forecasts
    # ------------------------------------------------------------------

    def frozen_forecast(history, **kwargs):
        return SavedEstimate(
            forecasts.loc[history.index[-1]]
        )

    engine.forecast_volatility = frozen_forecast


    # ------------------------------------------------------------------
    # Record hashes of regression inputs
    # ------------------------------------------------------------------

    context = {
        key: hashlib.sha256(
            Path(path).read_bytes()
        ).hexdigest()
        for key, path in [
            ('data', a.data),
            ('forecasts', a.forecasts)
        ]
    }


    # ------------------------------------------------------------------
    # Initialize SQLite storage
    # ------------------------------------------------------------------

    store = SQLiteStore(a.db)

    store.initialize(
        c,
        new_portfolio(c),
        EngineState(),
        'backtest',
        context
    )

    portfolio, state, revision = store.restore()

    execution = ExecutionModel(
        **c['execution']
    )


    # ------------------------------------------------------------------
    # Process candles
    # ------------------------------------------------------------------

    for i, (timestamp, candle) in enumerate(frame.iterrows()):

        key = 'candle:' + timestamp.isoformat()

        if store.contains(key):
            continue

        result = engine.process_candle(
            timestamp,
            candle,
            frame.iloc[:i + 1],
            i,
            portfolio,
            state,
            execution,
            c
        )

        revision = store.commit(
            key,
            portfolio,
            state,
            result,
            revision
        )

        # Exercise restart round-trips
        if i % 500 == 0:
            portfolio, state, revision = store.restore()


    # ------------------------------------------------------------------
    # Compare actual trades against expected trades
    # ------------------------------------------------------------------

    actual = pd.DataFrame(portfolio.trades)

    expected = pd.read_csv(
        a.expected_trades
    )

    if len(actual) != len(expected):
        raise AssertionError(
            f'Trade count differs: {len(actual)} vs {len(expected)}'
        )


    # ------------------------------------------------------------------
    # Compare individual trade fields
    # ------------------------------------------------------------------

    if len(actual):

        for key in ['strategy', 'side']:

            if not actual[key].equals(expected[key]):
                raise AssertionError(
                    key + ' differs'
                )

        if not pd.to_datetime(
            actual.timestamp,
            utc=True
        ).equals(
            pd.to_datetime(
                expected.timestamp,
                utc=True
            )
        ):
            raise AssertionError(
                'Trade timestamps differ'
            )

        for key in [
            'btc_price',
            'btc_amount',
            'fee_usd',
            'usd_amount'
        ]:

            np.testing.assert_allclose(
                actual[key],
                expected[key],
                rtol=1e-12,
                atol=1e-10,
                err_msg=key
            )


    # ------------------------------------------------------------------
    # Success
    # ------------------------------------------------------------------

    print(
        'PASS:',
        len(actual),
        'trades match; SQLite state round-trips preserve results.'
    )

    store.close()


if __name__ == '__main__':
    main()