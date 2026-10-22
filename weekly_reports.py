"""Preview, send a test, or run the scheduled Gmail reporter independently."""

# Import scheduling, logging, credentials, reporting, and persistent delivery dependencies.
import argparse
from datetime import datetime, timezone
import json
import logging
import sqlite3
from logging.handlers import RotatingFileHandler
from pathlib import Path
import time
import uuid
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from filelock import FileLock
from services.weekly_report import (load_settings,
    report_period,
    due_period,
    read_report,
    render_report,)
from services.gmail_sender import GmailSender
from services.report_delivery import DeliveryLedger

# Resolve default files relative to the project script directory.
BASE = Path(__file__).resolve().parent


# Return the current timezone-aware UTC time for scheduling and report boundaries.
def utcnow():
    return datetime.now(timezone.utc)


# Run a report preview, test delivery, status inspection, manual resolution, or scheduled sender.
def main():
    # Define the mutually exclusive report actions and their supporting command-line options.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default=str(BASE / 'runtime/paper.sqlite'))
    parser.add_argument('--settings', default=str(BASE / 'reporting.json'))
    parser.add_argument('--out', default=str(BASE / 'runtime/reports'))
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument('--preview', action='store_true', help='Save text/JSON only; no email')
    actions.add_argument('--send-test', action='store_true', help='SEND one current-week test email')
    actions.add_argument('--send-due', action='store_true', help='SEND/retry scheduled reports once')
    actions.add_argument('--watch', action='store_true', help='Keep the scheduled reporter running')
    actions.add_argument('--status', action='store_true', help='Show delivery statuses')
    actions.add_argument('--resolve', metavar='REPORT_ID')
    parser.add_argument('--resolution', choices=['sent', 'retry'])
    parser.add_argument('--current-week', action='store_true', help='With --preview: week to now')
    args = parser.parse_args()
    # Allow the current-week override only for preview mode.
    if args.current_week and not args.preview:
        parser.error('--current-week is only used with --preview')
    # Require a report ID and resolution choice together for manual reconciliation.
    if bool(args.resolve) != bool(args.resolution):
        parser.error('--resolve and --resolution must be used together')
    # Load environment settings and validate the report schedule.
    load_dotenv(BASE / '.env')
    settings = load_settings(args.settings)
    database = Path(args.db).resolve()
    # Reject a missing paper-account database.
    if not database.is_file():
        parser.error('Paper database does not exist; check --db.')
    # Create the report output directory and configure console and rotating-file logs.
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(out / 'reports.log', maxBytes=2_000_000, backupCount=3),
        ],)
    # Generate preview files for a preview request or a separate current-week test email.
    if args.preview or args.send_test:
        # Choose reporting boundaries and read the paper-account metrics.
        start, end = report_period(utcnow(), settings, current=args.current_week or args.send_test)
        report = read_report(database, start, end, settings)
        title = ('TEST — current-week paper report'
            if args.send_test
            else 'Paper-trading report preview')
        # Render the report and save timestamped text and JSON preview files.
        body = render_report(report, title)
        tag = utcnow().strftime('%Y%m%dT%H%M%S_%fZ')
        text_path = out / f'preview_{tag}.txt'
        text_path.write_text(body, encoding='utf-8')
        (out / f'preview_{tag}.json').write_text(json.dumps(report, indent=2, allow_nan=False), encoding='utf-8')
        print(body)
        print('Saved preview:', text_path)
        # Send a separate test email without marking the scheduled weekly report as delivered.
        if args.send_test:
            # Test mail never marks the scheduled weekly report as sent.
            GmailSender().send(title, body, f'<test-{uuid.uuid4()}@bitcoin-paper-report.invalid>')
            print('Test email accepted by Gmail. Check the destination inbox/spam.')
        return
    # Locate the independent delivery ledger associated with the trading database.
    ledger_path = Path(str(database) + '.reports.sqlite')
    # Display existing delivery statuses without attempting to send a report.
    if args.status:
        # Report an absent ledger without creating delivery records.
        if not ledger_path.exists():
            print('No delivery ledger yet. Preview/test emails do not create weekly delivery records.')
            return
        # Open the delivery ledger read-only for status inspection.
        db = sqlite3.connect(ledger_path.as_uri() + '?mode=ro', uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        # Read delivery status rows while ensuring connection cleanup afterward.
        try:
            # Print each report's status, attempts, and last error in period-end order.
            for row in db.execute('SELECT report_id,period_end,status,attempts,last_error FROM deliveries ORDER BY period_end'):
                print(dict(row))
        # Close the status-inspection database connection.
        finally:
            db.close()
        return
    # Hold the report worker lock before modifying delivery records or sending scheduled reports.
    with FileLock(str(ledger_path) + '.lock', timeout=0):
        ledger = DeliveryLedger(ledger_path, database)
        # Manage report delivery with interruption handling and guaranteed ledger cleanup.
        try:
            # Apply an explicit manual resolution and stop before any automatic send attempt.
            if args.resolve:
                ledger.resolve(args.resolve, args.resolution)
                print('Updated report status. Use --send-due to attempt an authorized retry.')
                return
            # Create the Gmail sender and initialize the period-inspection marker.
            sender = GmailSender()
            last_period = None
            # Check due periods and attempt eligible queued deliveries until single-run completion or interruption.
            while True:
                start, end = due_period(utcnow(), settings)
                # Inspect a due period once per process, rather than rescanning
                # a week's quotes every 30 seconds. Ledger handles restart dedup.
                # Read the latest due period only when it differs from the last period inspected by this process.
                if end != last_period:
                    report = read_report(database, start, end, settings)
                    # Queue a deduplicated weekly report only when the period contains recorded account activity.
                    if report['has_period_activity']:
                        local_end = end.astimezone(ZoneInfo(settings['timezone']))
                        subject = f'BTC PAPER — week ending {local_end:%Y-%m-%d}'
                        report_id = ledger.enqueue(report, subject, render_report(report), sender.recipient)
                        logging.info('Scheduled report recorded: %s', report_id)
                    # Log inactivity instead of queuing an empty-period report.
                    else:
                        logging.info('No paper activity in the latest due period; waiting for the next schedule.')
                    last_period = end
                # Attempt one pending delivery according to the saved retry schedule.
                result = ledger.deliver_one(sender, settings)
                # Log the report identity and resulting delivery status when an attempt occurred.
                if result:
                    logging.info('Report %s: %s', *result)
                # Return after one iteration unless continuous watch mode is enabled.
                if not args.watch:
                    return
                # Wait for the configured polling interval before the next scheduling check.
                time.sleep(settings['poll_seconds'])
        # Handle keyboard interruption while retaining persistent delivery statuses.
        except KeyboardInterrupt:
            logging.info('Reporter stopped; delivery statuses retained.')
        # Close the delivery ledger whenever the reporting worker exits.
        finally:
            ledger.close()


# Run the report command-line entry point only when executed directly.
if __name__ == '__main__':
    main()
