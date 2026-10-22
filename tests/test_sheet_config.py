"""Offline tests: no Google connection, market feed, or messages."""

# Import the test dependencies and application components used by this module.
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import Future

import pandas as pd
from services.config_store import ConfigStore
from services.sheet_config import (SheetPoller,
    flatten,
    parse_rows,
    require_live_compatible,
    write_cache,)
from services.storage import new_portfolio
from trading.models import EngineState, OrderIntent
from trading.execution import ExecutionModel
from trading.engine import process_quote

# Locate the project root relative to this test file.
ROOT = Path(__file__).resolve().parents[1]


# Test spreadsheet configuration parsing and safe application to a running account.
class SheetConfigTests(unittest.TestCase):
    # Initialize temporary account storage and record the starting configuration at revision zero.
    def setUp(self):
        # Load the project settings used to initialize the test account.
        self.config = json.loads((ROOT / 'config.json').read_text())
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'paper.sqlite'
        self.cache = Path(self.tmp.name) / 'cache.json'
        self.store = ConfigStore(self.path)
        self.portfolio = new_portfolio(self.config)
        self.state = EngineState()
        self.store.initialize(self.config, self.portfolio, self.state, 'paper')
        self.store.record_initial_config(self.config, 0)

    # Close test resources and remove the temporary files created for this test.
    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    # Flatten configuration keys into spreadsheet rows with a key/value header.
    def rows(self):
        return [['key', 'value']] + [[k, v] for k, v in flatten(self.config).items()]

    # Check that spreadsheet text for a decimal DCA amount parses back to a numeric value.
    def test_template_round_trip_and_decimal_purchase(self):
        rows = self.rows()
        # Scan the spreadsheet rows to locate the DCA amount setting.
        for row in rows:
            # Replace only the row whose key is dca.amount; leave all other settings intact.
            if row[0] == 'dca.amount':
                row[1] = '250.50'
        # Parse the edited spreadsheet rows back into a validated configuration.
        candidate = parse_rows(rows, self.config)
        # Verify the expected results and safeguards for this scenario.
        self.assertEqual(candidate['dca']['amount'], 250.50)

    # Check that missing, duplicate, unknown, malformed, and invalid spreadsheet settings are rejected.
    def test_reject_incomplete_duplicate_unknown_invalid(self):
        # Build spreadsheet examples with a missing row, duplicate key, unknown key, and malformed group.
        cases = [
            self.rows()[:-1],
            self.rows() + [self.rows()[1]],
            self.rows() + [['secret_token', 'no']],
            self.rows() + [['exits', '']],
        ]
        # Try negative, non-finite, Boolean, and nonnumeric DCA purchase amounts.
        for value in (-1, float('nan'), True, 'nonsense'):
            rows = self.rows()
            # Scan the spreadsheet rows to locate the DCA amount setting.
            for row in rows:
                # Replace only the row whose key is dca.amount; leave all other settings intact.
                if row[0] == 'dca.amount':
                    row[1] = value
            cases.append(rows)
        # Validate each malformed spreadsheet example independently.
        for rows in cases:
            # Report this spreadsheet example as a separate subtest so failures identify the input.
            with self.subTest(rows=rows):
                # Require ValueError from this rejected operation; a normal return fails the test.
                with self.assertRaises(ValueError):
                    parse_rows(rows, self.config)

    # Check that locked settings and an invalid reserve adjustment cannot be applied live.
    def test_locked_settings_and_full_validation(self):
        # Try changing each locked setting independently against the original configuration.
        for group, key, value in [
            ('execution', 'fee_rate', 0.002),
            ('garch', 'n_paths', 6),
            ('atr', 'period', 20),
        ]:
            c = deepcopy(self.config)
            c[group][key] = value
            # Require ValueError from this rejected operation; a normal return fails the test.
            with self.assertRaises(ValueError):
                require_live_compatible(c, self.config)
        c = deepcopy(self.config)
        # Change the reserve alone to exercise full configuration validation.
        c['reserve'] += 1
        # Require ValueError from this rejected operation; a normal return fails the test.
        with self.assertRaises(ValueError):
            require_live_compatible(c, self.config)

    # Check that a live configuration update preserves filled holdings and stops while clearing a pending DCA buy.
    def test_live_change_preserves_filled_account_and_stop(self):
        stamp = pd.Timestamp('2026-01-01T00:00Z')
        # Stage a swing buy order with the specified ATR-based stop parameters.
        self.state.pending_swing_buy = OrderIntent('buy', 'SWING', 'BUY', 'test', stamp, amount_usd=1000, atr=10, atr_multiplier=2)
        result = process_quote(stamp + pd.Timedelta(seconds=10),
            100,
            100,
            self.portfolio,
            self.state,
            ExecutionModel(**self.config['execution']),)
        revision = self.store.commit('quote', self.portfolio, self.state, result, 0)
        # Stage a DCA buy order without executing it yet.
        self.state.pending_dca = OrderIntent('dca', 'DCA', 'BUY', 'test', stamp, amount_usd=500)
        # Stage a swing exit that should remain pending through the configuration change.
        self.state.pending_swing_sell = OrderIntent('sell', 'SWING', 'SELL', 'test', stamp)
        # Record the filled portfolio and active stop before applying the configuration change.
        before = self.store.portfolio_state(self.portfolio)
        stop = deepcopy(self.state.active_position)
        c = deepcopy(self.config)
        # Prepare a changed DCA amount for the configuration update under test.
        c['dca']['amount'] = 250
        # Apply the revised configuration using the latest saved revision.
        c, state, revision = self.store.apply_config(c, self.config, self.state, revision)
        # Reload the persisted portfolio, engine state, and revision for verification.
        p, saved, rev = self.store.restore()
        # Verify the expected results and safeguards for this scenario.
        self.assertEqual(self.store.portfolio_state(p), before)
        self.assertEqual(saved.active_position, stop)
        self.assertIsNone(saved.pending_dca)
        self.assertEqual(saved.pending_swing_sell.order_id, 'sell')
        self.assertIsNotNone(self.state.pending_dca)  # input is not mutated
        self.assertEqual(len(p.trades), 1)
        self.assertEqual(rev, revision)
        self.assertEqual(self.store.startup_config(self.config, self.cache), c)

    # Check that a stale configuration revision leaves the checkpoint and configuration history unchanged.
    def test_stale_revision_rolls_back_everything(self):
        c = deepcopy(self.config)
        # Prepare a changed DCA amount for the configuration update under test.
        c['dca']['amount'] = 250
        # Require RuntimeError from this rejected operation; a normal return fails the test.
        with self.assertRaises(RuntimeError):
            self.store.apply_config(c, self.config, self.state, 9)
        # Verify the expected results and safeguards for this scenario.
        self.assertEqual(self.store.restore()[2], 0)
        self.assertEqual(self.store.startup_config(self.config, self.cache), self.config)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM config_versions').fetchone()[0], 1)

    # Check that an audit insert failure rolls back the configuration update and processed event.
    def test_audit_failure_rolls_back_checkpoint(self):
        # Force configuration-history inserts to fail so transaction rollback can be checked.
        self.store.db.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON config_versions BEGIN SELECT RAISE(ABORT, 'test'); END")
        c = deepcopy(self.config)
        # Prepare a changed DCA amount for the configuration update under test.
        c['dca']['amount'] = 250
        # Require Exception from this rejected operation; a normal return fails the test.
        with self.assertRaises(Exception):
            self.store.apply_config(c, self.config, self.state, 0)
        # Verify the expected results and safeguards for this scenario.
        self.assertEqual(self.store.restore()[2], 0)
        self.assertEqual(self.store.startup_config(self.config, self.cache), self.config)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM processed_events').fetchone()[0], 0)

    # Check that the database configuration takes precedence over a stale cache after restart.
    def test_restart_database_wins_over_cache(self):
        c = deepcopy(self.config)
        # Prepare a changed DCA amount for the configuration update under test.
        c['dca']['amount'] = 250
        self.store.apply_config(c, self.config, self.state, 0)
        # Write the original settings as a deliberately stale cache before reopening storage.
        write_cache(self.cache, self.config)  # stale export after a simulated crash
        self.store.close()
        self.store = ConfigStore(self.path)
        # Verify the expected results and safeguards for this scenario.
        self.assertEqual(self.store.startup_config(self.config, self.cache), c)

    # Check that a fresh database accepts a valid cache and falls back to supplied settings for malformed JSON.
    def test_fresh_database_cache_fallback_and_bad_cache(self):
        # Isolate the fresh database in a temporary directory removed after the block.
        with tempfile.TemporaryDirectory() as directory:
            fresh = ConfigStore(Path(directory) / 'new.sqlite')
            # Exercise the resource-dependent scenario while ensuring cleanup in the finally block.
            try:
                c = deepcopy(self.config)
                # Prepare a changed DCA amount for the configuration update under test.
                c['dca']['amount'] = 250
                write_cache(self.cache, c)
                # Verify the expected results and safeguards for this scenario.
                self.assertEqual(fresh.startup_config(self.config, self.cache), c)
                # Corrupt the cache JSON to exercise fallback to the supplied configuration.
                self.cache.write_text('{broken')
                # Verify the expected results and safeguards for this scenario.
                self.assertEqual(fresh.startup_config(self.config, self.cache), self.config)
            # Always close the resource, including when an assertion or operation raises an error.
            finally:
                fresh.close()

    # Check that polling defers configuration changes when blocked and tolerates a failed fetch.
    def test_forecast_defers_application_and_network_failure_is_nonfatal(self):
        # Create a poller whose background result can be controlled by a Future.
        poller = SheetPoller(self.config)
        # Exercise the resource-dependent scenario while ensuring cleanup in the finally block.
        try:
            # Prevent a real fetch from starting while testing a prepared background result.
            poller.next_fetch = float('inf')
            poller.job = Future()
            # Complete the background job with a valid configuration ready for later application.
            poller.job.set_result(self.config)
            # Verify the expected results and safeguards for this scenario.
            self.assertIsNone(poller.tick(can_apply=False))
            self.assertIsNotNone(poller.job)
            self.assertEqual(poller.tick(can_apply=True), self.config)
            poller.job = Future()
            # Complete the background job with a timeout to test nonfatal fetch failure handling.
            poller.job.set_exception(TimeoutError())
            # Verify the expected results and safeguards for this scenario.
            self.assertIsNone(poller.tick(can_apply=True))
        # Always close the resource, including when an assertion or operation raises an error.
        finally:
            poller.close()


# Run this file's unittest suite only when the file is executed directly.
if __name__ == '__main__':
    unittest.main()
