"""Run the advisory-only observer in its own terminal. No orders can be submitted."""

# Import command-line, logging, locking, database, and advisory service dependencies.
import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sqlite3
import time
from dotenv import load_dotenv
from filelock import FileLock
from services.advisor_log import AdvisorLog, read_latest_context
from services.llm_advisor import GeminiAdvisor

# Resolve default paths relative to this script rather than the current working directory.
BASE = Path(__file__).resolve().parent


# Load advisor settings and validate the model name and bounded integer options.
def load_settings(path):
    s = json.loads(Path(path).read_text(encoding='utf-8'))
    # Reject an absent, non-text, or blank model identifier.
    if not isinstance(s.get('model'), str) or not s['model'].strip():
        raise ValueError('A model identifier is required.')
    # Define permitted polling, request, token, input-age, and daily-quota ranges.
    bounds = {
        'poll_seconds': (1, 300),
        'timeout_seconds': (1, 120),
        'max_output_tokens': (100, 2000),
        'max_input_age_seconds': (1, 3600),
        'max_requests_per_utc_day': (1, 100),
    }
    # Check each bounded setting against its minimum and maximum.
    for key, (low, high) in bounds.items():
        # Reject missing settings, non-integers, Booleans, and values outside the allowed range.
        if type(s.get(key)) is not int or not low <= s[key] <= high:
            raise ValueError(f'{key} must be an integer between {low} and {high}.')
    return s


# Run the selected preview, history display, single-request, or continuous advisor mode.
def main():
    # Define the account and settings paths and require exactly one operating mode.
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db', default=str(BASE / 'runtime/paper.sqlite'))
    p.add_argument('--settings', default=str(BASE / 'advisor.json'))
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--input', action='store_true', help='Preview latest saved input; no API request')
    mode.add_argument('--show', action='store_true', help='Show latest logged recommendations; no API request')
    mode.add_argument('--once', action='store_true', help='At most one Gemini API request for latest fresh close')
    mode.add_argument('--watch', action='store_true', help='Watch new saved closes and call the API')
    a = p.parse_args()
    path = Path(a.db).resolve()
    # Reject a missing paper database before creating or reading advisor storage.
    if not path.is_file():
        p.error('Paper database does not exist; check --db.')
    log_path = Path(str(path) + '.advisor.sqlite')
    # Display recorded recommendations without contacting Gemini.
    if a.show:
        # Report an absent advisor ledger and stop without creating one.
        if not log_path.exists():
            print('No advisory log yet.')
            return
        # Open the advisor ledger in read-only mode with named-column access.
        db = sqlite3.connect(log_path.as_uri() + '?mode=ro', uri=True)
        db.row_factory = sqlite3.Row
        # Read recent records while ensuring the database connection is closed afterward.
        try:
            # Print up to ten recommendations in descending creation-time order.
            for row in db.execute('''SELECT event_key,status,model,prompt_version,advice_json,
                                     elapsed_seconds,error_type FROM advice ORDER BY created_at DESC LIMIT 10'''):
                print(json.dumps(dict(row), indent=2))
        # Always close the recommendation-history connection.
        finally:
            db.close()
        return
    # Preview the latest saved advisor input without making a provider request.
    if a.input:
        latest = read_latest_context(path)
        print(json.dumps(latest[1], indent=2)
            if latest
            else 'No advisory input yet. Wait for a NEW completed-candle decision from the updated paper runner.')
        return
    # Load environment credentials, validate settings, and configure console and rotating-file logging.
    load_dotenv(BASE / '.env')
    settings = load_settings(a.settings)
    logging.basicConfig(level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(str(path) + '.advisor.log', maxBytes=2_000_000, backupCount=3),
        ],)
    # Serializes API attempts across restarts and accidental duplicate workers.
    # Hold the advisor process lock to prevent overlapping request workers.
    with FileLock(str(log_path) + '.lock', timeout=0):
        ledger = AdvisorLog(log_path, path)
        provider = None
        # Create the provider and run the observer with guaranteed resource cleanup.
        try:
            provider = GeminiAdvisor(settings)
            last_notice = None
            # Poll saved candle decisions until a single-run completion or an interruption.
            while True:
                # Read the latest context and let the ledger enforce deduplication, input checks, and quotas.
                try:
                    latest = read_latest_context(path)
                    status = (ledger.process(*latest, settings, provider)
                        if latest
                        else 'waiting_for_new_close')
                    notice = (latest[0] if latest else None, status)
                    # Log only when the event or its processing status changes.
                    if notice != last_notice:
                        logging.info('ADVISORY ONLY | %s | %s', *notice)
                        last_notice = notice
                # Report temporary SQLite access failures independently of the trading process.
                except sqlite3.OperationalError:
                    logging.warning('Advisor database unavailable; trading process is independent.')
                    # Propagate database failure in single-run mode instead of silently treating it as success.
                    if a.once:
                        raise
                # Stop after one polling attempt when single-run mode is selected.
                if a.once:
                    break
                # Wait for the configured interval before checking for another saved decision.
                time.sleep(settings['poll_seconds'])
        # Handle a keyboard interruption with an advisory-only shutdown message.
        except KeyboardInterrupt:
            logging.info('Advisor stopped. No trading mode was changed.')
        # Release provider and ledger resources when the observer exits.
        finally:
            # Close the provider only if initialization succeeded.
            if provider:
                provider.close()
            ledger.close()


# Invoke the command-line entry point only when this file is executed directly.
if __name__ == '__main__':
    main()
