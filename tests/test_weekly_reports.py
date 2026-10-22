"""Offline tests: no email or market requests."""

# Import the test dependencies and application components used by this module.
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from services.weekly_report import due_period, report_period, read_report, render_report
from services.report_delivery import DeliveryLedger
from services.gmail_sender import GmailSender, RetryableDeliveryError, UncertainDeliveryError

# Define fixed scheduling, request, or retry settings for reproducible tests.
UTC = timezone.utc
# Define fixed scheduling, request, or retry settings for reproducible tests.
SETTINGS = dict(timezone='UTC',
    weekday=0,
    hour=9,
    minute=0,
    poll_seconds=30,
    retry_seconds=300,
    max_attempts=2,
    max_quote_age_seconds=300,
    max_quote_gap_seconds=300,)


# Test weekly report calculations, valuation coverage, and reporting boundaries.
class ReportsTests(unittest.TestCase):
    # Create a paper reporting database and an opening valuation for a fixed weekly period.
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'paper.sqlite'
        # Set a fixed opening time and a seven-day reporting window.
        self.start = datetime(2026, 10, 5, tzinfo=UTC)
        self.end = self.start + timedelta(days=7)
        self.db = sqlite3.connect(self.path)
        # Create the minimal database tables and fixture rows required by these tests.
        self.db.executescript('''
        CREATE TABLE checkpoint(id INTEGER PRIMARY KEY,mode TEXT);
        INSERT INTO checkpoint VALUES(1,'paper');
        CREATE TABLE snapshots(event_key TEXT PRIMARY KEY,timestamp TEXT,portfolio_value REAL,
            cash REAL,dca_btc REAL,active_btc REAL,payload_json TEXT);
        CREATE TABLE trades(trade_id TEXT PRIMARY KEY,timestamp TEXT,strategy TEXT,side TEXT,
            reason TEXT,fee_usd REAL,realized_pnl REAL,fill_json TEXT);
        ''')
        # Seed a valuation just before the reporting window opens.
        self.snap('quote:opening', self.start - timedelta(seconds=10), 10000, 10000)

    # Close test resources and remove the temporary files created for this test.
    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    # Insert and commit a valuation snapshot, including its execution uncertainty flag.
    def snap(self, key, t, value, cash, btc=0, uncertain=False):
        self.db.execute('INSERT INTO snapshots VALUES(?,?,?,?,?,?,?)',
            (key,
                t.isoformat(),
                value,
                cash,
                btc,
                0,
                json.dumps({'execution_path_uncertain': uncertain}),),)
        self.db.commit()

    # Insert and commit a trade with its fee, realized profit, and net cash amount.
    def trade(self, key, t, strategy, side, net, fee=0, pnl=None):
        self.db.execute('INSERT INTO trades VALUES(?,?,?,?,?,?,?,?)',
            (key, t.isoformat(), strategy, side, 'test', fee, pnl, json.dumps({'net_usd': net})),)
        self.db.commit()

    # Check report totals for value change, realized and unrealized profit, fees, and trade count.
    def test_fee_inclusive_metrics(self):
        # Add DCA and swing trades with known cash flows and fees for report calculations.
        self.trade('dca', self.start + timedelta(hours=1), 'DCA', 'BUY', 500, 0.5)
        self.trade('buy', self.start + timedelta(hours=2), 'SWING', 'BUY', 1000)
        self.trade('sell', self.start + timedelta(hours=3), 'SWING', 'SELL', 1100, pnl=100)
        self.snap('quote:end', self.end - timedelta(seconds=10), 10149.45, 9600, 0.004995)
        # Build the report from the saved fixtures for the specified reporting period.
        r = read_report(self.path, self.start, self.end, SETTINGS)
        # Verify the expected results and safeguards for this scenario.
        self.assertAlmostEqual(r['change_usd'], 149.45)
        self.assertAlmostEqual(r['unrealized_pnl'], 49.45)
        self.assertEqual(r['realized_pnl'], 100)
        self.assertEqual(r['fees'], 0.5)
        self.assertEqual(r['trade_count'], 3)
        self.assertIn('PAPER TRADING', render_report(r))

    # Check that candle-close snapshots and trades exactly at the period end are excluded.
    def test_end_excluded_and_candle_snapshots_ignored(self):
        self.snap('quote:end', self.end - timedelta(seconds=10), 10000, 10000)
        # Insert an exaggerated candle-close valuation that must not determine closing value.
        self.snap('close:misleading', self.end - timedelta(seconds=1), 999999, 999999)
        # Place a trade exactly at the exclusive period-end boundary.
        self.trade('boundary', self.end, 'DCA', 'BUY', 500)
        # Build the report from the saved fixtures for the specified reporting period.
        r = read_report(self.path, self.start, self.end, SETTINGS)
        # Verify the expected results and safeguards for this scenario.
        self.assertEqual(r['closing_value'], 10000)
        self.assertEqual(r['trade_count'], 0)

    # Check that stale or absent valuation data suppresses unsupported report metrics.
    def test_stale_and_missing_valuation(self):
        # Provide a closing quote that is too old for a supported return calculation.
        self.snap('quote:old', self.end - timedelta(hours=2), 9900, 9900)
        # Build the report from the saved fixtures for the specified reporting period.
        r = read_report(self.path, self.start, self.end, SETTINGS)
        # Verify the expected results and safeguards for this scenario.
        self.assertIsNone(r['return_pct'])
        # Remove all valuations to test reporting when no snapshots remain.
        self.db.execute('DELETE FROM snapshots')
        self.db.commit()
        # Build the report from the saved fixtures for the specified reporting period.
        r = read_report(self.path, self.start, self.end, SETTINGS)
        # Verify the expected results and safeguards for this scenario.
        self.assertIsNone(r['closing_value'])
        self.assertIsNone(r['change_usd'])
        self.assertFalse(r['has_period_activity'])

    # Check that the report warns about uncertain execution and gaps in valuation coverage.
    def test_uncertainty_and_coverage_disclosed(self):
        self.snap('quote:end', self.end - timedelta(seconds=10), 10000, 10000, uncertain=True)
        # Build the report from the saved fixtures for the specified reporting period.
        r = read_report(self.path, self.start, self.end, SETTINGS)
        # Verify the expected results and safeguards for this scenario.
        self.assertTrue(any('execution_path_uncertain' in w for w in r['warnings']))
        self.assertTrue(any('gaps' in w for w in r['warnings']))

    # Check that weekly reporting rejects a database marked as a backtest account.
    def test_backtest_rejected(self):
        self.db.execute("UPDATE checkpoint SET mode='backtest'")
        self.db.commit()
        # Require ValueError from this rejected operation; a normal return fails the test.
        with self.assertRaises(ValueError):
            read_report(self.path, self.start, self.end, SETTINGS)

    # Check Monday 9 AM scheduling in Toronto and the 167-hour and 169-hour daylight-saving weeks.
    def test_monday_nine_and_dst(self):
        # Use Toronto local time to exercise the weekly boundary across daylight-saving changes.
        settings = dict(SETTINGS, timezone='America/Toronto')
        _, old_end = due_period(datetime(2026, 3, 9, 12, 59, tzinfo=UTC), settings)
        start, end = due_period(datetime(2026, 3, 9, 13, 0, tzinfo=UTC), settings)
        # Verify the expected results and safeguards for this scenario.
        self.assertLess(old_end, end)
        self.assertEqual((end - start).total_seconds() / 3600, 167)
        # Check the autumn clock change, which adds an hour to the reporting week.
        start, end = due_period(datetime(2026, 11, 2, 14, tzinfo=UTC), settings)
        # Verify the expected results and safeguards for this scenario.
        self.assertEqual((end - start).total_seconds() / 3600, 169)

    # Check that a preview ends at the supplied current time rather than the next weekly boundary.
    def test_week_to_date_preview(self):
        now = self.start + timedelta(days=3)
        # Verify the expected results and safeguards for this scenario.
        self.assertEqual(report_period(now, SETTINGS, True), (self.start, now))


# Test persistent email delivery status, duplicate prevention, and retry handling.
class DeliveryTests(unittest.TestCase):
    # Create a temporary delivery ledger and queue a report with a fixed reporting period.
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'reports.sqlite'
        self.source = Path(self.tmp.name) / 'paper.sqlite'
        self.ledger = DeliveryLedger(self.path, self.source)
        # Define the reporting period used as the delivery deduplication identity.
        self.report = {'start': '2026-10-05T00:00:00+00:00', 'end': '2026-10-12T00:00:00+00:00'}
        # Queue the fixture email and retain its report ID for later status checks.
        self.id = self.ledger.enqueue(self.report, 'Weekly', 'Body', 'test@example.com')

    # Close test resources and remove the temporary files created for this test.
    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    # Read the saved delivery status for the report queued by the test setup.
    def status(self):
        return self.ledger.db.execute('SELECT status FROM deliveries WHERE report_id=?', (self.id,)).fetchone()[0]

    # Check that a sent report remains deduplicated after restart and changes to recipient or content.
    def test_success_restart_and_recipient_change_dedup(self):
        # Provide an email sender stub that counts delivery attempts without sending mail.
        class Sender:
            calls = 0

            # Count the attempted email delivery without sending a message.
            def send(self, *args):
                self.calls += 1

        sender = Sender()
        # Attempt delivery through the stub sender at the supplied test time.
        self.ledger.deliver_one(sender, SETTINGS, 100)
        self.ledger.close()
        self.ledger = DeliveryLedger(self.path, self.source)
        # Verify the expected results and safeguards for this scenario.
        self.assertEqual(self.ledger.enqueue(self.report, 'Changed', 'Changed', 'other@example.com'), self.id)
        self.assertIsNone(self.ledger.deliver_one(sender, SETTINGS, 1000))
        self.assertEqual(sender.calls, 1)

    # Check that a retryable failure waits for the backoff interval and stops at the attempt limit.
    def test_safe_retry_backoff_and_limit(self):
        # Provide a sender stub that raises a controlled delivery error without network access.
        class Broken:
            # Raise the simulated delivery failure expected by the surrounding test.
            def send(self, *args):
                raise RetryableDeliveryError('Login failed')

        # Attempt delivery through the stub sender at the supplied test time.
        self.ledger.deliver_one(Broken(), SETTINGS, 100)
        # Verify the expected results and safeguards for this scenario.
        self.assertEqual(self.status(), 'pending')
        self.assertIsNone(self.ledger.deliver_one(Broken(), SETTINGS, 101))
        # Attempt delivery through the stub sender at the supplied test time.
        self.ledger.deliver_one(Broken(), SETTINGS, 401)
        # Verify the expected results and safeguards for this scenario.
        self.assertEqual(self.status(), 'failed')

    # Check that uncertain delivery blocks automatic retries until its status is explicitly resolved.
    def test_ambiguous_outcome_needs_resolution(self):
        # Simulate an email timeout after submission, when delivery success is unknown.
        class Unknown:
            # Raise an uncertain-delivery error to simulate a timeout after email submission.
            def send(self, *args):
                raise UncertainDeliveryError('Timeout after data')

        # Attempt delivery through the stub sender at the supplied test time.
        self.ledger.deliver_one(Unknown(), SETTINGS, 100)
        # Verify the expected results and safeguards for this scenario.
        self.assertEqual(self.status(), 'uncertain')
        self.assertIsNone(self.ledger.deliver_one(Unknown(), SETTINGS, 1000))
        # Resolve the ambiguous delivery as sent without attempting another transmission.
        self.ledger.resolve(self.id, 'sent')
        # Verify the expected results and safeguards for this scenario.
        self.assertEqual(self.status(), 'sent')

    # Check that a send interrupted by restart becomes uncertain and can be marked for retry.
    def test_interrupted_send_becomes_uncertain(self):
        # Commit the simulated status change as a database transaction before testing restart recovery.
        with self.ledger.db:
            self.ledger.db.execute("UPDATE deliveries SET status='sending'")
        self.ledger.close()
        self.ledger = DeliveryLedger(self.path, self.source)
        # Verify the expected results and safeguards for this scenario.
        self.assertEqual(self.status(), 'uncertain')
        # Explicitly authorize a new attempt for the interrupted delivery.
        self.ledger.resolve(self.id, 'retry')
        # Verify the expected results and safeguards for this scenario.
        self.assertEqual(self.status(), 'pending')

    # Check that login failures are retryable while timeouts during submission are treated as uncertain.
    def test_login_failure_vs_unknown_send(self):
        # Import the SMTP exception type used to simulate an authentication failure.
        import smtplib

        # Provide dummy email credentials and a recipient for the mocked SMTP adapter.
        env = dict(GMAIL_SENDER='sender@gmail.com',
            REPORT_RECIPIENT='recipient@example.com',
            GMAIL_APP_PASSWORD='test',)
        # Temporarily replace email settings and SMTP connections so no real email is sent.
        with patch.dict('os.environ', env), patch('services.gmail_sender.smtplib.SMTP_SSL') as smtp:
            sender = GmailSender()
            # Simulate authentication failure before any message can be submitted.
            smtp.return_value.login.side_effect = smtplib.SMTPAuthenticationError(535, b'bad')
            # Require RetryableDeliveryError from this rejected operation; a normal return fails the test.
            with self.assertRaises(RetryableDeliveryError):
                sender.send('Test', 'Body', '<test@example.com>')
            # Allow login for the separate submission-timeout scenario.
            smtp.return_value.login.side_effect = None
            # Simulate a timeout during message submission, when delivery may already have occurred.
            smtp.return_value.send_message.side_effect = TimeoutError()
            # Require UncertainDeliveryError from this rejected operation; a normal return fails the test.
            with self.assertRaises(UncertainDeliveryError):
                sender.send('Test', 'Body', '<test@example.com>')


# Run this file's unittest suite only when the file is executed directly.
if __name__ == '__main__':
    unittest.main()
