"""Read trading events read-only; write results ONLY to a separate advisor DB."""
# Import database, timestamp, serialization, and advice-validation dependencies.
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time
from services.llm_advisor import PROMPT, PROMPT_VERSION, validate_advice


# Serialize audit data deterministically and reject non-finite JSON numbers.
def encode(value):
    return json.dumps(value, sort_keys=True, allow_nan=False)


# Read the latest eligible candle-close context from a paper database without modifying it.
def read_latest_context(path):
    path = Path(path).resolve()

    # Reject a missing source file before opening a database connection.
    if not path.is_file():
        raise ValueError('Paper database does not exist; check --db.')
    
    # Open the source database in read-only mode with a short lock timeout.
    db = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=2)

    # Read account mode and the latest event within one consistent SQLite transaction.
    try:
        db.execute('BEGIN')
        row = db.execute('SELECT mode FROM checkpoint WHERE id=1').fetchone()

        # Require an initialized paper account before exposing a decision context.
        if row is None or row[0] != 'paper':
            raise ValueError('The advisor requires a PAPER database.')
        
        # Select the most recently stored runtime event whose key identifies a candle close.
        row = db.execute("""SELECT event_key,payload_json FROM events
            WHERE kind='runtime' AND event_key LIKE 'close:%'
            ORDER BY id DESC LIMIT 1""").fetchone()
        
        # Return no context when no matching close event exists.
        if not row:
            return None
        
        event = json.loads(row[1])
        # Ignore events lacking a close decision or its saved advisor input.
        if event.get('kind') != 'close_decision' or 'advisor_input' not in event:
            return None
        
        context = event['advisor_input']

        # Attach the actual decision time to the selected advisor input.
        # Do not pass arbitrary event fields or credentials to the model.
        context['decision_time'] = event['actual_decision_time']

        return row[0], context
    
    # Close the read-only connection even when validation or decoding fails.
    finally:
        db.close()


# Measure the age of the completed candle in seconds relative to the supplied time.
def input_age(context, now):

    stamp = datetime.fromisoformat(context['candle_close'].replace('Z', '+00:00'))

    # Reject timezone-free candle times so age comparisons remain unambiguous.
    if stamp.tzinfo is None:
        raise ValueError('Candle time must include timezone.')
    
    return (now - stamp).total_seconds()


# Require observed OHLC data and positive finite values for every core market indicator.
def input_usable(context):

    import math
    market = context.get('market', {})
    keys = ('Close', 'ATR', 'SMA_20', 'SMA_50', 'PREV_SMA_20', 'PREV_SMA_50')

    # Check each required indicator without accepting Boolean values as numbers.
    return bool(market.get('observed_ohlc')) and all(
        type(market.get(k)) in (int, float) and math.isfinite(market[k]) and market[k] > 0
        for k in keys)


# Maintain a separate audit ledger for advice requests, outcomes, and skipped events.
class AdvisorLog:
    """One worker owns this file under FileLock; jobs are never auto-retried."""
    # Create the advisor ledger, bind it to one source account, and recover interrupted requests.
    def __init__(self, path, source):

        path = Path(path).resolve()
        source = Path(source).resolve()

        # Prevent advisor audit writes from targeting the trading database itself.
        if path == source:
            raise ValueError('Advisor log must be separate from the trading database.')
        
        # Create the ledger directory and configure SQLite for WAL journaling and full synchronization.
        path.parent.mkdir(parents=True, exist_ok=True)

        self.db = sqlite3.connect(path, timeout=2)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')

        # Create source-identity and advice-history tables if they do not already exist.
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS advisor_source(id INTEGER PRIMARY KEY CHECK(id=1),path TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS advice(
            event_key TEXT PRIMARY KEY, created_at REAL NOT NULL, finished_at REAL,
            status TEXT NOT NULL, model TEXT NOT NULL, prompt_version TEXT NOT NULL,
            prompt TEXT NOT NULL, settings_json TEXT NOT NULL, input_json TEXT NOT NULL,
            advice_json TEXT, metadata_json TEXT, elapsed_seconds REAL,
            request_started REAL, error_type TEXT);
        ''')

        # Save identity checks and interrupted-request recovery in a single transaction.
        with self.db:
            existing = self.db.execute('SELECT path FROM advisor_source WHERE id=1').fetchone()

            # Reject reuse of this ledger for a different source database path.
            if existing and existing[0] != str(source):
                raise ValueError('Advisor log belongs to a different database path.')
            
            # Register the source and mark previously running jobs as interrupted without retrying them.
            self.db.execute('INSERT OR IGNORE INTO advisor_source VALUES(1,?)', (str(source),))
            self.db.execute("UPDATE advice SET status='interrupted', error_type = 'Worker stopped before result was saved' WHERE status='running'")

    # Record a new decision once, enforce input and quota checks, and save the provider outcome.
    def process(self, event_key, context, settings, provider, now = None):

        now = now or datetime.now(timezone.utc)
        created = now.timestamp()

        # Skip every event key already recorded, including failed or skipped requests.
        if self.db.execute('SELECT 1 FROM advice WHERE event_key=?', (event_key,)).fetchone():
            return 'already_recorded'
        
        # Calculate input age and count requests started since midnight UTC.
        age = input_age(context, now)
        midnight = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        used = self.db.execute('SELECT count(*) FROM advice WHERE request_started>=?', 
                               (midnight,)).fetchone()[0]
        
        # Skip future-dated or overly old candles before making a provider request.
        if not 0 <= age <= settings['max_input_age_seconds']:
            status = 'skipped_stale'

        # Skip contexts with missing, unobserved, non-finite, or nonpositive core indicators.
        elif not input_usable(context):
            status = 'skipped_data'

        # Skip additional requests once the daily UTC allowance has been used.
        elif used >= settings['max_requests_per_utc_day']:
            status = 'skipped_daily_limit'

        # Mark an eligible request as running before contacting the provider.
        else:
            status = 'running'

        # Detach via JSON before calling external code: no trading objects exist
        # in this process, and the audit input is saved before any request.
        saved_input = encode(context)
        
        # Persist the detached input, request settings, prompt, and starting status before external work.
        with self.db:
            self.db.execute('''INSERT INTO advice
                (event_key,created_at,status,model,prompt_version,prompt,settings_json,input_json,request_started)
                VALUES(?,?,?,?,?,?,?,?,?)''',
                (event_key, created, status, settings['model'], PROMPT_VERSION, PROMPT,
                 encode(settings), saved_input, created if status == 'running' else None))
            
        # Return a skip status immediately without calling the provider.
        if status != 'running':
            return status
        
        # Start an elapsed-time measurement and initialize empty result fields.
        begun = time.monotonic()
        advice = metadata = error_type = None

        # Call the provider with the saved JSON snapshot and validate its reported outcome.
        try:
            status, advice, metadata = provider.advise(json.loads(saved_input))

            # Validate successful advice locally before accepting its contents.
            if status == 'ok':
                advice = validate_advice(advice)

                # Mark otherwise valid advice as late if the input expired while the request was running.
                if input_age(context, datetime.now(timezone.utc)) > settings['max_input_age_seconds']:
                    status = 'late'

            # Reject provider statuses outside the explicitly supported response outcomes.
            elif status not in ('refused', 'incomplete', 'invalid_response'):
                raise ValueError('Unexpected provider status.')
            
        # Record the exception class without retaining potentially sensitive exception text.
        except Exception as error:
            status, advice, error_type = 'error', None, type(error).__name__

        elapsed = time.monotonic()-begun

        # Save the final status, advice, metadata, elapsed time, and error category atomically.
        with self.db:
            self.db.execute('''UPDATE advice SET finished_at=?,status=?,advice_json=?,metadata_json=?,
                elapsed_seconds=?,error_type=? WHERE event_key=?''',
                (time.time(), status, encode(advice) if advice else None,
                 encode(metadata) if metadata else None, elapsed, error_type, event_key))
            
        return status

    # Close the advisor audit database connection.
    def close(self):
        self.db.close()
