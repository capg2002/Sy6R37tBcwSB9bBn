"""Offline tests: no Google connection, market feed, or messages."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import Future

import pandas as pd
from services.config_store import ConfigStore
from services.sheet_config import (SheetPoller, flatten, parse_rows,
    require_live_compatible, write_cache)
from services.storage import new_portfolio
from trading.models import EngineState, OrderIntent
from trading.execution import ExecutionModel
from trading.engine import process_quote

ROOT = Path(__file__).resolve().parents[1]


class SheetConfigTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT/'config.json').read_text())
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'paper.sqlite'
        self.cache = Path(self.tmp.name)/'cache.json'
        self.store = ConfigStore(self.path)
        self.portfolio = new_portfolio(self.config)
        self.state = EngineState()
        self.store.initialize(self.config, self.portfolio, self.state, 'paper')
        self.store.record_initial_config(self.config, 0)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def rows(self):
        return [['key', 'value']] + [[k, v] for k, v in flatten(self.config).items()]

    def test_template_round_trip_and_decimal_purchase(self):
        rows = self.rows()
        for row in rows:
            if row[0] == 'dca.amount': row[1] = '250.50'
        candidate = parse_rows(rows, self.config)
        self.assertEqual(candidate['dca']['amount'], 250.50)

    def test_reject_incomplete_duplicate_unknown_invalid(self):
        cases = [self.rows()[:-1], self.rows()+[self.rows()[1]],
                 self.rows()+[['secret_token', 'no']], self.rows()+[['exits', '']]]
        for value in (-1, float('nan'), True, 'nonsense'):
            rows = self.rows()
            for row in rows:
                if row[0] == 'dca.amount': row[1] = value
            cases.append(rows)
        for rows in cases:
            with self.subTest(rows=rows):
                with self.assertRaises(ValueError): parse_rows(rows, self.config)

    def test_locked_settings_and_full_validation(self):
        for group, key, value in [('execution', 'fee_rate', .002),
                                  ('garch', 'n_paths', 6), ('atr', 'period', 20)]:
            c = deepcopy(self.config); c[group][key] = value
            with self.assertRaises(ValueError): require_live_compatible(c, self.config)
        c = deepcopy(self.config); c['reserve'] += 1
        with self.assertRaises(ValueError): require_live_compatible(c, self.config)

    def test_live_change_preserves_filled_account_and_stop(self):
        stamp = pd.Timestamp('2026-01-01T00:00Z')
        self.state.pending_swing_buy = OrderIntent('buy', 'SWING', 'BUY', 'test', stamp,
            amount_usd=1000, atr=10, atr_multiplier=2)
        result = process_quote(stamp+pd.Timedelta(seconds=10), 100, 100,
            self.portfolio, self.state, ExecutionModel(**self.config['execution']))
        revision = self.store.commit('quote', self.portfolio, self.state, result, 0)
        self.state.pending_dca = OrderIntent('dca', 'DCA', 'BUY', 'test', stamp, amount_usd=500)
        self.state.pending_swing_sell = OrderIntent('sell', 'SWING', 'SELL', 'test', stamp)
        before = self.store.portfolio_state(self.portfolio)
        stop = deepcopy(self.state.active_position)
        c = deepcopy(self.config); c['dca']['amount'] = 250
        c, state, revision = self.store.apply_config(c, self.config, self.state, revision)
        p, saved, rev = self.store.restore()
        self.assertEqual(self.store.portfolio_state(p), before)
        self.assertEqual(saved.active_position, stop)
        self.assertIsNone(saved.pending_dca)
        self.assertEqual(saved.pending_swing_sell.order_id, 'sell')
        self.assertIsNotNone(self.state.pending_dca)  # input is not mutated
        self.assertEqual(len(p.trades), 1)
        self.assertEqual(rev, revision)
        self.assertEqual(self.store.startup_config(self.config, self.cache), c)

    def test_stale_revision_rolls_back_everything(self):
        c = deepcopy(self.config); c['dca']['amount'] = 250
        with self.assertRaises(RuntimeError): self.store.apply_config(c, self.config, self.state, 9)
        self.assertEqual(self.store.restore()[2], 0)
        self.assertEqual(self.store.startup_config(self.config, self.cache), self.config)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM config_versions').fetchone()[0], 1)

    def test_audit_failure_rolls_back_checkpoint(self):
        self.store.db.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON config_versions BEGIN SELECT RAISE(ABORT, 'test'); END")
        c = deepcopy(self.config); c['dca']['amount'] = 250
        with self.assertRaises(Exception): self.store.apply_config(c, self.config, self.state, 0)
        self.assertEqual(self.store.restore()[2], 0)
        self.assertEqual(self.store.startup_config(self.config, self.cache), self.config)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM processed_events').fetchone()[0], 0)

    def test_restart_database_wins_over_cache(self):
        c = deepcopy(self.config); c['dca']['amount'] = 250
        self.store.apply_config(c, self.config, self.state, 0)
        write_cache(self.cache, self.config)  # stale export after a simulated crash
        self.store.close(); self.store = ConfigStore(self.path)
        self.assertEqual(self.store.startup_config(self.config, self.cache), c)

    def test_fresh_database_cache_fallback_and_bad_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            fresh = ConfigStore(Path(directory)/'new.sqlite')
            try:
                c = deepcopy(self.config); c['dca']['amount'] = 250
                write_cache(self.cache, c)
                self.assertEqual(fresh.startup_config(self.config, self.cache), c)
                self.cache.write_text('{broken')
                self.assertEqual(fresh.startup_config(self.config, self.cache), self.config)
            finally: fresh.close()

    def test_forecast_defers_application_and_network_failure_is_nonfatal(self):
        poller = SheetPoller(self.config)
        try:
            poller.next_fetch = float('inf')
            poller.job = Future(); poller.job.set_result(self.config)
            self.assertIsNone(poller.tick(can_apply=False))
            self.assertIsNotNone(poller.job)
            self.assertEqual(poller.tick(can_apply=True), self.config)
            poller.job = Future(); poller.job.set_exception(TimeoutError())
            self.assertIsNone(poller.tick(can_apply=True))
        finally: poller.close()


if __name__ == '__main__': unittest.main()
