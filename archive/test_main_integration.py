"""Run the actual main loop with tiny offline fixtures, not fitted GARCH."""
import contextlib
import io
import json
from pathlib import Path
import runpy
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import pandas as pd


class MainIntegrationTests(unittest.TestCase):
    def test_exit_reasons_and_fill_prices(self):
        source = Path(__file__).resolve().parent
        for expected in ['atr_stop', 'garch_trailing_stop', 'bearish_crossover']:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                shutil.copy(source / 'main.py', root / 'main.py')
                config = json.loads((source / 'config.json').read_text())
                config['exits']['garch_trailing_enabled'] = expected == 'garch_trailing_stop'
                (root / 'config.json').write_text(json.dumps(config))
                index = pd.date_range('2025-01-01', periods=8, freq='h', tz='UTC')
                data = pd.DataFrame({'Open':100., 'High':101., 'Low':99.9,
                                     'Close':100., 'Volume':1., 'observed_ohlc':True}, index=index)
                if expected == 'atr_stop':
                    data.loc[index[4], 'Low'] = 95.
                if expected == 'garch_trailing_stop':
                    data.loc[index[4], 'Low'] = 99.
                class Estimate:
                    status = 'ok'
                    sigma_next_hour = .001
                    def to_dict(self):
                        return {'status': self.status, 'sigma_next_hour': self.sigma_next_hour}
                def module(name, **attributes):
                    obj = types.ModuleType(name)
                    obj.__dict__.update(attributes)
                    return obj
                def atr(frame, period):
                    frame['ATR'] = 2.
                    return frame
                def sma(frame, period):
                    return pd.Series(([99,99,101,101,101,99,99,99] if period == 20
                                      else [100]*8), index=index)
                stubs = {
                    'market_data': module('market_data', get_btc_data=lambda: data.copy()),
                    'missing_data': module('missing_data', prepare_hourly_data=lambda df: df),
                    'indicators': module('indicators', calculate_atr=atr, calculate_sma=sma),
                    'volatility': module('volatility', forecast_volatility=lambda *a, **kw: Estimate()),
                    'risk': module('risk', size_swing_purchase=lambda **kw: (1000., 'fixture')),
                    'dca_strategy': module('dca_strategy', should_dca_buy=lambda price, last, threshold: last is None),
                }
                with patch.dict(sys.modules, stubs), contextlib.redirect_stdout(io.StringIO()):
                    runpy.run_path(str(root / 'main.py'), run_name='__main__')
                folders = list((root / 'data').iterdir())
                self.assertEqual(len(folders), 1)
                trades = pd.read_csv(folders[0] / 'trades.csv')
                sales = trades[trades.side == 'SELL']
                self.assertEqual(sales.exit_reason.tolist(), [expected])
                buys = trades[trades.side == 'BUY']
                self.assertTrue((abs(buys.btc_price - 100.05) < 1e-9).all())
                self.assertTrue((sales.btc_price < sales.reference_price).all())
                # main.py itself also asserts both P/L accounting identities.


if __name__ == '__main__':
    unittest.main()
