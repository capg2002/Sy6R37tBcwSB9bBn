# Import the test dependencies and application components used by this module.
import copy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
import pandas as pd
from trading.models import EngineState, OrderIntent
from trading.execution import ExecutionModel
from trading.engine import CandleResult, process_quote, process_close
from services.storage import SQLiteStore, new_portfolio
from paper_loop import monitor_quote
from services.telegram_notify import deliver_one, DeliveryError

# Locate the project root relative to this test file.
ROOT = Path(__file__).resolve().parents[1]


# Test saved trading state, transaction rollback, and duplicate execution safeguards.
class PersistenceTests(unittest.TestCase):
    # Create a temporary paper account using the project configuration and execution settings.
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'account.sqlite'
        # Load the project settings used to initialize the test account.
        self.config = json.loads((ROOT / 'config.json').read_text())
        self.store = SQLiteStore(self.path)
        self.portfolio = new_portfolio(self.config)
        self.state = EngineState()
        self.store.initialize(self.config, self.portfolio, self.state, 'paper')
        # Use the configured fee and slippage settings for simulated fills.
        self.execution = ExecutionModel(**self.config['execution'])

    # Close test resources and remove the temporary files created for this test.
    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    # Queue fixed DCA and swing buy orders for the simulated execution tests.
    def queue(self):
        # Stage a DCA buy order without executing it yet.
        self.state.pending_dca = OrderIntent('dca-order', 'DCA', 'BUY', 'test', pd.Timestamp('2025-01-01T01:00Z'), amount_usd=500)
        # Stage a swing buy order with the specified ATR-based stop parameters.
        self.state.pending_swing_buy = OrderIntent('swing-order',
            'SWING',
            'BUY',
            'test',
            pd.Timestamp('2025-01-01T01:00Z'),
            amount_usd=1000,
            atr=2,
            atr_multiplier=2,)

    # Queue both buy orders and process a quote ten seconds after the signal time.
    def buy(self):
        self.queue()
        return process_quote(pd.Timestamp('2025-01-01T01:00:10Z'),
            100,
            100,
            self.portfolio,
            self.state,
            self.execution,)

    # Check that holdings, stops, and executed order identities survive a database restart.
    def test_restore_holdings_stops_and_ids(self):
        result = self.buy()
        # Attempt to persist the event, portfolio, and engine state at the supplied revision.
        self.store.commit('quote1', self.portfolio, self.state, result, 0, True)
        self.store.close()
        self.store = SQLiteStore(self.path)
        # Restore the latest committed portfolio, engine state, and revision from storage.
        p, s, r = self.store.restore()
        # Verify the expected results and safeguards for this scenario.
        self.assertEqual(p.active_btc, self.portfolio.active_btc)
        self.assertEqual(s.active_position, self.state.active_position)
        self.assertIsNone(s.pending_swing_buy)
        self.assertEqual(len(p.trades), 2)
        self.assertFalse(p.apply_fill(result.fills[0]))
        self.assertFalse(p.apply_fill(replace(result.fills[0], trade_id='different-id-same-order')))

    # Check that saved pending orders can be restored, filled, and committed.
    def test_restore_pending_before_fill(self):
        self.queue()
        # Attempt to persist the event, portfolio, and engine state at the supplied revision.
        self.store.commit('signal', self.portfolio, self.state, CandleResult(), 0)
        # Restore the latest committed portfolio, engine state, and revision from storage.
        p, s, r = self.store.restore()
        result = process_quote(pd.Timestamp('2025-01-01T01:00:10Z'), 100, 100, p, s, self.execution)
        # Verify the expected results and safeguards for this scenario.
        self.assertEqual(len(result.fills), 2)
        # Attempt to persist the event, portfolio, and engine state at the supplied revision.
        self.store.commit('fill', p, s, result, r)

    # Check that reusing an event key rolls back changes to the saved cash balance.
    def test_duplicate_event_rolls_back(self):
        result = self.buy()
        # Attempt to persist the event, portfolio, and engine state at the supplied revision.
        self.store.commit('q', self.portfolio, self.state, result, 0)
        before = self.store.restore()[0].cash
        # Alter the in-memory cash balance to expose any accidental save during a rejected commit.
        self.portfolio.dca_cash = 1
        # Require Exception from this rejected operation; a normal return fails the test.
        with self.assertRaises(Exception):
            # Attempt to persist the event, portfolio, and engine state at the supplied revision.
            self.store.commit('q', self.portfolio, self.state, CandleResult(), 1)
        # Verify the expected results and safeguards for this scenario.
        self.assertEqual(self.store.restore()[0].cash, before)

    # Check that a duplicate fill leaves the checkpoint, revision, and event registry unchanged.
    def test_fill_conflict_rolls_back_checkpoint(self):
        result = self.buy()
        # Attempt to persist the event, portfolio, and engine state at the supplied revision.
        self.store.commit('q', self.portfolio, self.state, result, 0)
        # Alter the in-memory cash balance to expose any accidental save during a rejected commit.
        self.portfolio.dca_cash = 1
        # Require Exception from this rejected operation; a normal return fails the test.
        with self.assertRaises(Exception):
            # Attempt to persist the event, portfolio, and engine state at the supplied revision.
            self.store.commit('q2', self.portfolio, self.state, result, 1)
        # Restore the latest committed portfolio, engine state, and revision from storage.
        p, s, r = self.store.restore()
        # Verify the expected results and safeguards for this scenario.
        self.assertEqual(p.dca_cash, 5500)
        self.assertEqual(r, 1)
        self.assertFalse(self.store.contains('q2'))

    # Check that an in-memory purchase is absent from the last committed account state.
    def test_uncommitted_action_not_restored(self):
        self.buy()
        # Restore the latest committed portfolio, engine state, and revision from storage.
        p, s, r = self.store.restore()
        # Verify the expected results and safeguards for this scenario.
        self.assertEqual(p.cash, 10000)
        self.assertEqual(p.active_btc, 0)

    # Check that a commit using an outdated revision raises an error.
    def test_stale_writer_rejected(self):
        result = self.buy()
        # Attempt to persist the event, portfolio, and engine state at the supplied revision.
        self.store.commit('q', self.portfolio, self.state, result, 0)
        # Require RuntimeError from this rejected operation; a normal return fails the test.
        with self.assertRaises(RuntimeError):
            # Attempt to persist the event, portfolio, and engine state at the supplied revision.
            self.store.commit('q2', self.portfolio, self.state, CandleResult(), 0)

    # Check that a quote arriving after order expiry produces no fills.
    def test_expired_buy_does_not_execute(self):
        self.queue()
        result = process_quote(pd.Timestamp('2025-01-01T03:00Z'), 100, 100, self.portfolio, self.state, self.execution)
        # Verify the expected results and safeguards for this scenario.
        self.assertEqual(result.fills, [])

    # Check that a quote gap marks execution uncertain and uses the current bid for the stop exit.
    def test_outage_uses_current_quote_for_stop(self):
        self.buy()
        result, gap = monitor_quote(pd.Timestamp('2025-01-01T02:00Z'), 90, 91, self.portfolio, self.state, self.execution)
        # Verify the expected results and safeguards for this scenario.
        self.assertTrue(gap)
        self.assertTrue(self.state.execution_path_uncertain)
        self.assertEqual(result.fills[-1].reference_price, 90)

    # Check that negative BTC fills and partial sales of the active swing position are rejected.
    def test_invalid_fill_and_partial_sale_rejected(self):
        result = self.buy()
        # Require ValueError from this rejected operation; a normal return fails the test.
        with self.assertRaises(ValueError):
            self.portfolio.apply_fill(replace(result.fills[0], trade_id='bad', order_id='bad', btc_amount=-1))
        # Construct a sale for half the active position to test rejection of partial exits.
        order = OrderIntent('sale', 'SWING', 'SELL', 'test', pd.Timestamp('2025-01-01T02:00Z'))
        fill = self.execution.fill_sell(order, 100, order.signal_time, self.portfolio.active_btc / 2)
        # Require ValueError from this rejected operation; a normal return fails the test.
        with self.assertRaises(ValueError):
            self.portfolio.apply_fill(fill)

    # Check that an existing account cannot be reinitialized with different strategy settings.
    def test_settings_change_rejected(self):
        c = copy.deepcopy(self.config)
        # Change the saved strategy choice to test configuration mismatch protection.
        c['swing']['crossover'] = 'bull'
        # Require ValueError from this rejected operation; a normal return fails the test.
        with self.assertRaises(ValueError):
            self.store.initialize(c, self.portfolio, self.state, 'paper')

    # Check that requesting notifications for a backtest commit is rejected without advancing its revision.
    def test_backtest_cannot_send(self):
        self.store.close()
        self.store = SQLiteStore(Path(self.tmp.name) / 'backtest.sqlite')
        self.store.initialize(self.config, self.portfolio, self.state, 'backtest')
        result = self.buy()
        # Require ValueError from this rejected operation; a normal return fails the test.
        with self.assertRaises(ValueError):
            # Attempt to persist the event, portfolio, and engine state at the supplied revision.
            self.store.commit('q', self.portfolio, self.state, result, 0, True)
        # Verify the expected results and safeguards for this scenario.
        self.assertEqual(self.store.restore()[2], 0)

    # Check that Telegram failures leave account cash intact and successful delivery is not repeated.
    def test_telegram_failure_does_not_change_account(self):
        result = self.buy()
        # Attempt to persist the event, portfolio, and engine state at the supplied revision.
        self.store.commit('q', self.portfolio, self.state, result, 0, True)

        # Provide a sender stub that raises a controlled delivery error without network access.
        class Broken:
            # Raise the simulated delivery failure expected by the surrounding test.
            def send(self, text):
                raise DeliveryError('network failure')

        # Attempt delivery through the stub sender at the supplied test time.
        deliver_one(self.store, Broken(), 100)
        # Verify the expected results and safeguards for this scenario.
        self.assertEqual(self.store.restore()[0].cash, 8500)

        # Provide a successful Telegram sender stub that returns a fixed message ID.
        class Good:
            # Return a fixed message ID to represent successful Telegram delivery.
            def send(self, text):
                return '123'

        # Attempt delivery through the stub sender at the supplied test time.
        deliver_one(self.store, Good(), 200)
        # Attempt delivery through the stub sender at the supplied test time.
        deliver_one(self.store, Good(), 200)
        # Verify the expected results and safeguards for this scenario.
        self.assertFalse(deliver_one(self.store, Good(), 200))

    # Check that processing an already handled candle close creates no additional fills.
    def test_duplicate_close_is_noop(self):
        # Mark the candle as already processed before passing the same timestamp to the engine.
        self.state.last_processed_candle = pd.Timestamp('2025-01-01T01:00Z')
        result = process_close(self.state.last_processed_candle,
            None,
            None,
            0,
            self.portfolio,
            self.state,
            self.config,)
        # Verify the expected results and safeguards for this scenario.
        self.assertEqual(result.fills, [])


# Run this file's unittest suite only when the file is executed directly.
if __name__ == '__main__':
    unittest.main()
