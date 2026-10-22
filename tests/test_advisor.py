"""Offline tests; no API calls or messages."""
# Import the test dependencies and application components used by this module.
import ast
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import pandas as pd
from services.advisor_context import advisory_context, market_summary
from services.advisor_log import AdvisorLog, read_latest_context
from services.llm_advisor import validate_advice, GeminiAdvisor, public_context, GeminiRateLimitError

# Locate the project root relative to this test file.
ROOT = Path(__file__).resolve().parents[1]
# Define fixed scheduling, request, or retry settings for reproducible tests.
SETTINGS = dict(model='test',
                poll_seconds=10,
                timeout_seconds=30,
                max_output_tokens=500,
                max_input_age_seconds=900,
                max_requests_per_utc_day=24)

# Build a recent candle context with fixed market indicators for offline advisor tests.
def context():
    return {'candle_close': (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(),
            'current_mode': 'hybrid',
            'market': dict(Close=100,
                           ATR=2,
                           SMA_20=101,
                           SMA_50=100,
                           PREV_SMA_20=100,
                           PREV_SMA_50=101,
                           observed_ohlc=True)}
# Return a mock advisor that recommends pausing entries without contacting an API.
def provider():

    p = Mock(); p.advise.return_value = ('ok',dict(recommended_mode='pause_entries',
                                                   reason='Offline test',
                                                   limitations=[]),
                                                   {});return p

# Test advisor validation, request limits, persistence, and protection of account data.
class AdvisorTests(unittest.TestCase):
    # Create a temporary paper account, a recent decision event, and a separate advisor ledger.
    def setUp(self):

        self.tmp = tempfile.TemporaryDirectory()
        self.source = Path(self.tmp.name)/'paper.sqlite'
        self.log = Path(self.tmp.name)/'advisor.sqlite'

        self.db = sqlite3.connect(self.source)

        # Create the minimal database tables and fixture rows required by these tests.
        self.db.executescript('''CREATE TABLE checkpoint(id INTEGER PRIMARY KEY,mode TEXT,config_json TEXT,portfolio_json TEXT,engine_json TEXT);
        INSERT INTO checkpoint VALUES(1,'paper','{"strategy_mode":"hybrid"}','{"cash":10000}','{"pending":"keep"}');
        CREATE TABLE events(id INTEGER PRIMARY KEY,event_key TEXT,kind TEXT,payload_json TEXT);''')
        
        # Create a recent advisor input and store it inside a candle-close decision event.
        self.ctx = context()
        event = {'kind':'close_decision',
                 'actual_decision_time':datetime.now(timezone.utc).isoformat(),
                 'advisor_input':self.ctx}
        self.db.execute('INSERT INTO events VALUES(1,?,?,?)',
                        ('close:test','runtime',json.dumps(event)))
        self.db.commit()

        # Open the advisor ledger associated with the source paper account.
        self.ledger = AdvisorLog(self.log,self.source)

    # Close test resources and remove the temporary files created for this test.
    def tearDown(self):
        self.ledger.close()
        self.db.close()
        self.tmp.cleanup()

    # Check that advice is saved without changing the source database or input context.
    def test_no_account_or_input_changes(self):

        # Capture the database and input context before processing advice.
        before = list(self.db.iterdump())
        original = deepcopy(self.ctx)
        event, ctx = read_latest_context(self.source)
        # Verify the expected results and safeguards for this scenario.
        self.assertEqual(self.ledger.process(event,ctx,SETTINGS,provider()),'ok')
        self.assertEqual(list(self.db.iterdump()),before)
        self.assertEqual(self.ctx,original)
        self.assertEqual(json.loads(self.ledger.db.execute('SELECT advice_json FROM advice').fetchone()[0])['recommended_mode'],'pause_entries')
    
    # Check that reopening the advisor ledger does not repeat a recorded request.
    def test_restart_deduplication(self):

        p = provider()
        self.ledger.process('one', self.ctx, SETTINGS, p)
        self.ledger.close()
        # Open the advisor ledger associated with the source paper account.
        self.ledger = AdvisorLog(self.log,self.source)
        # Verify the expected results and safeguards for this scenario.
        self.assertEqual(self.ledger.process('one', self.ctx, SETTINGS, p),'already_recorded')
        self.assertEqual(p.advise.call_count, 1)

    # Check that a timeout records only its error type and prevents a repeated request.
    def test_timeout_no_retry(self):

        p = provider()
        # Simulate a provider timeout with a message that must not enter the error log.
        p.advise.side_effect = TimeoutError('secret should not be logged')
        # Verify the expected results and safeguards for this scenario.
        self.assertEqual(self.ledger.process('one',self.ctx,SETTINGS,p),'error')
        self.assertEqual(self.ledger.db.execute('SELECT error_type FROM advice').fetchone()[0],'TimeoutError')
        self.ledger.process('one',self.ctx,SETTINGS,p)
        # Verify the expected results and safeguards for this scenario.
        self.assertEqual(p.advise.call_count,1)

    # Check that stale candles and missing ATR values skip the advisor entirely.
    def test_stale_and_missing_data_skip(self):

        p = provider()
        c = deepcopy(self.ctx)
        # Age the candle by two hours to exercise the stale-input safeguard.
        c['candle_close'] = (datetime.now(timezone.utc) - timedelta(hours = 2)).isoformat()
        # Verify the expected results and safeguards for this scenario.
        self.assertEqual(self.ledger.process('old', c, SETTINGS, p), 'skipped_stale')
        c = deepcopy(self.ctx)
        # Remove the ATR value to exercise the missing-market-data safeguard.
        c['market']['ATR'] = None
        # Verify the expected results and safeguards for this scenario.
        self.assertEqual(self.ledger.process('bad', c, SETTINGS, p),'skipped_data')
        p.advise.assert_not_called()

    # Check that a second event is skipped after the configured daily request limit.
    def test_daily_cap(self):

        p = provider()
        # Limit the provider to one request for the UTC day.
        s = dict(SETTINGS, max_requests_per_utc_day = 1)
        self.ledger.process('one', self.ctx, s, p)
        # Verify the expected results and safeguards for this scenario.
        self.assertEqual(self.ledger.process('two', self.ctx, s, p),' skipped_daily_limit')
        self.assertEqual(p.advise.call_count, 1)

    # Check that unsupported modes, extra fields, and empty reasons fail validation.
    def test_invalid_output(self):

        # Try each invalid recommendation and require validation to reject it.
        for advice in ({'recommended_mode':'sell','reason':'x','limitations':[]},
            {'recommended_mode':'hybrid','reason':'x','limitations':[],'order':'BUY'},
            {'recommended_mode':'hybrid','reason':'','limitations':[]}):
            # Require ValueError from this rejected operation; a normal return fails the test.
            with self.assertRaises(ValueError):
                validate_advice(advice)

    # Check that a request left running becomes interrupted and is not submitted again.
    def test_interrupted_job_preserved(self):

        self.ledger.process('one', self.ctx, SETTINGS, provider())
        # Commit the simulated status change as a database transaction before testing restart recovery.
        with self.ledger.db:
            self.ledger.db.execute("UPDATE advice SET status='running'")
        self.ledger.close()
        # Open the advisor ledger associated with the source paper account.
        self.ledger = AdvisorLog(self.log,self.source)
        # Verify the expected results and safeguards for this scenario.
        self.assertEqual(self.ledger.db.execute('SELECT status FROM advice').fetchone()[0],'interrupted')
        p = provider()
        self.ledger.process('one',self.ctx,SETTINGS,p)
        p.advise.assert_not_called()

    # Check that advisor storage rejects the source file itself and backtest accounts.
    def test_backtest_and_same_file_rejected(self):
        # Require ValueError from this rejected operation; a normal return fails the test.
        with self.assertRaises(ValueError):
            AdvisorLog(self.source,self.source)
        self.db.execute("UPDATE checkpoint SET mode='backtest'")
        self.db.commit()
        # Require ValueError from this rejected operation; a normal return fails the test.
        with self.assertRaises(ValueError):
            read_latest_context(self.source)

    # Test the Gemini request schema and mocked success, refusal, truncation, and rate-limit responses.
    def test_api_adapter_schema_and_refusal(self):
        # Construct the adapter without normal initialization and replace HTTP access with mocks.
        a = GeminiAdvisor.__new__(GeminiAdvisor)
        a.settings = dict(SETTINGS, model = 'gemini-2.5-flash-lite')
        a.key = 'fake'
        a.client = Mock()
        response = Mock(status_code = 200)
        # Prepare a Gemini-style response containing a valid JSON recommendation.
        data = {
            'candidates':[{
                'finishReason':'STOP',
                'content':{
                    'parts':[{
                        'text':json.dumps(dict(recommended_mode = 'hybrid',
                                               reason = 'Test',
                                               limitations=[]))
                            }]
                           }
                           }]
              }
        
        # Connect the fake JSON response to the mocked HTTP request.
        response.json.return_value = data
        a.client.post.return_value = response

        # Verify the expected results and safeguards for this scenario.
        self.assertEqual(a.advise(self.ctx)[0], 'ok')

        # Inspect the outgoing request options captured by the HTTP mock.
        kwargs = a.client.post.call_args.kwargs

        # Verify the expected results and safeguards for this scenario.
        self.assertNotIn('tools',kwargs['json'])
        self.assertFalse(kwargs['allow_redirects'])
        self.assertIn('responseJsonSchema',kwargs['json']['generationConfig'])

        # Simulate a safety refusal from the provider.
        data['candidates'][0]['finishReason'] = 'SAFETY'
        # Verify the expected results and safeguards for this scenario.
        self.assertEqual(a.advise(self.ctx)[0],'refused')

        # Simulate a response cut short by the output-token limit.
        data['candidates'][0]['finishReason'] = 'MAX_TOKENS'
        # Verify the expected results and safeguards for this scenario.
        self.assertEqual(a.advise(self.ctx)[0], 'incomplete')

        # Simulate an HTTP rate-limit response.
        response.status_code = 429
        # Require GeminiRateLimitError from this rejected operation; a normal return fails the test.
        with self.assertRaises(GeminiRateLimitError):
            a.advise(self.ctx)

    # Check that public advisor context includes approved fields and excludes private values.
    def test_only_allowlisted_data_sent(self):

        c = deepcopy(self.ctx)
        # Add an approved volatility estimate alongside private values to test field filtering.
        c['garch'] = {'sigma_next_hour_fraction': .01}
        c['portfolio'] = {'cash': 12345}
        c['secret'] = 'secret'
        c['market']['secret'] = 'secret'
        result = public_context(c)
        # Verify the expected results and safeguards for this scenario.
        self.assertEqual(result['sigma_next_hour'], .01)
        self.assertNotIn('secret',json.dumps(result))
        self.assertNotIn('12345',json.dumps(result))

    # Check market summary values and ensure the generated context is detached from its inputs.
    def test_context_detached(self):
        # Build two hourly observations with fixed prices and indicators.
        f = pd.DataFrame({'Close':[100.,101.],
                          'ATR':[2.,2.],
                          'SMA_20':[100.,100.],
                          'SMA_50':[99.,99.],
                          'PREV_SMA_20':[100.,100.],
                          'PREV_SMA_50':[99.,99.],
                          'observed_ohlc':[True,True]}, index = pd.date_range('2026-01-01',
                                                                              periods = 2,
                                                                              freq = 'h',
                                                                              tz = 'UTC'))
        
        # Calculate the one-hour return from the fixture; a 24-hour history is unavailable.
        m = market_summary(f)
        # Verify the expected results and safeguards for this scenario.
        self.assertAlmostEqual(m['return_1h_fraction'], .01)
        self.assertIsNone(m['return_24h_fraction'])

        # Prepare configuration values containing both public settings and a private marker.
        c = dict(strategy_mode = 'hybrid',
                 risk = {'reference_hourly_sigma': .004,
                         'garch_sizing_enabled': True},
                         exits = {'garch_trailing_enabled': True},
                         swing = {'crossover':'bear'},
                         dca = {'drop_threshold': .03},
                         secret = 'never send')
        
        # Represent the portfolio balances without constructing a full trading account.
        p = SimpleNamespace(cash = 10000,
                            dca_cash = 6000,
                            swing_cash = 3000,
                            reserve_cash = 1000,
                            dca_btc = 0,
                            active_btc = 0)
        
        # Represent engine state with no active position or pending orders.
        s = SimpleNamespace(active_position=None,
                            last_quote_time=None,
                            execution_path_uncertain=False,
                            pending_dca=None,
                            pending_swing_buy=None,
                            pending_swing_sell=None)
        
        # Snapshot the input objects before building and modifying the advisor payload.
        before = (deepcopy(vars(p)),deepcopy(vars(s)),deepcopy(c))
        payload = advisory_context(f.index[-1],
                                   m,
                                   None,
                                   p,s,c)
        # Verify the expected results and safeguards for this scenario.
        self.assertNotIn('never send', json.dumps(payload))
        # Modify the returned payload to test that the original portfolio remains detached.
        payload['portfolio_at_decision']['cash'] = 0
        
        # Verify the expected results and safeguards for this scenario.
        self.assertEqual((vars(p), vars(s), c), before)
        
# Run this file's unittest suite only when the file is executed directly.
if __name__=='__main__':unittest.main()
