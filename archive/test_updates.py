"""Offline checks: run with python -m unittest test_updates -v."""
import copy
import contextlib
import io
import json
import unittest
from pathlib import Path
from trading.portfolio import Portfolio
from services.config_validation import validate_config
from trading.swing_strategy import should_open_swing_trade_bullish, should_open_swing_trade_bearish


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads(Path(__file__).with_name('config.json').read_text())
        self.p = Portfolio(10000, 6000, 3000, 1000, .001, .0005)

    def test_current_config(self):
        validate_config(self.config)

    def test_bad_config(self):
        for group, key, value in [('dca', 'allocation', 6001), ('dca', 'amount', -1),
                                  ('execution', 'fee_rate', float('nan')),
                                  ('execution', 'slippage_rate', 1),
                                  ('atr', 'period', 1.5), ('swing', 'crossover', 'typo'),
                                  ('risk', 'garch_sizing_enabled', 'false')]:
            with self.subTest(key=key):
                c = copy.deepcopy(self.config)
                c[group][key] = value
                with self.assertRaises(ValueError):
                    validate_config(c)

    def test_invalid_orders_do_not_mutate_balances(self):
        for buy in (self.p.buy_btc, self.p.buy_active):
            for amount, price in [(-1, 100), (0, 100), (1, 0), (1, -100),
                                  (float('nan'), 100), (100, float('inf'))]:
                before = copy.deepcopy(self.p.__dict__)
                with self.assertRaises(ValueError):
                    buy(amount, price, 'test')
                self.assertEqual(before, self.p.__dict__)

    def test_budget_and_reserve(self):
        self.assertFalse(self.p.buy_btc(6001, 100, 'test'))
        self.assertFalse(self.p.buy_active(3001, 100, 'test'))
        self.assertEqual(self.p.reserve_cash, 1000)
        self.assertEqual(self.p.cash, 10000)

    def test_fees_slippage_and_accounting(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(self.p.buy_active(1000, 100, 'buy'))
            qty = 1000 / 1.001 / 100.05
            self.assertAlmostEqual(self.p.active_btc, qty)
            self.assertFalse(self.p.buy_active(50, 100, 'second'))
            self.assertTrue(self.p.sell_active(100, 'sell', reason='atr_stop'))
        proceeds = qty * 99.95 * .999
        self.assertAlmostEqual(self.p.realized_pnl, proceeds - 1000)
        self.assertAlmostEqual(self.p.get_value(100), 10000 + proceeds - 1000)
        self.assertEqual(self.p.trades[-1]['exit_reason'], 'atr_stop')
        self.assertAlmostEqual(self.p.trades[0]['btc_price'], 100.05)
        self.assertAlmostEqual(self.p.trades[-1]['btc_price'], 99.95)
        self.assertEqual(self.p.reserve_cash, 1000)
        self.assertFalse(self.p.sell_active(100, 'repeat'))

    def test_zero_slippage_matches_previous_model(self):
        p = Portfolio(10000, 6000, 3000, 1000, .001)
        with contextlib.redirect_stdout(io.StringIO()):
            p.buy_active(1000, 100, 'buy')
            p.sell_active(100, 'sell')
        self.assertAlmostEqual(p.realized_pnl, -1.998001998001996)

    def test_crossover_direction(self):
        self.assertTrue(should_open_swing_trade_bullish(99, 100, 101, 100))
        self.assertFalse(should_open_swing_trade_bearish(99, 100, 101, 100))
        self.assertTrue(should_open_swing_trade_bearish(101, 100, 99, 100))
        self.assertFalse(should_open_swing_trade_bullish(101, 100, 99, 100))


if __name__ == '__main__':
    unittest.main()
