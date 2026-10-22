"""Separate delivery ledger: never changes balances, fills, or trading state."""
# Import ledger persistence, account identifiers, hashing, and delivery failure categories.
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sqlite3
import time
import uuid

from services.gmail_sender import RetryableDeliveryError, UncertainDeliveryError


# Track report delivery independently from trading balances and execution state.
class DeliveryLedger:
    # Create the delivery ledger, bind its account identity, and recover interrupted sends.
    def __init__(self, path, source):

        # Create the ledger directory and configure durable SQLite storage.
        Path(path).parent.mkdir(parents = True, exist_ok = True)

        self.db = sqlite3.connect(path, timeout = 10)
        self.db.row_factory = sqlite3.Row

        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')

        # Create account identity and report delivery tables with persistent status and retry fields.
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS identity(id INTEGER PRIMARY KEY CHECK(id=1),
                source TEXT NOT NULL, account_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS deliveries(
                report_id TEXT PRIMARY KEY, period_end TEXT NOT NULL,
                recipient TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL,
                message_id TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0, last_error TEXT, sent_at TEXT);
        ''')

        source = str(Path(source).resolve())

        # Bind the ledger and recover interrupted deliveries in a single database transaction.
        with self.db:
            row = self.db.execute('SELECT * FROM identity WHERE id=1').fetchone()

            # Assign a persistent account ID when the ledger is first initialized.
            if row is None:
                self.db.execute('INSERT INTO identity VALUES(1,?,?)', (source, str(uuid.uuid4())))

            # Reject reuse of the ledger with a different source account path.
            elif row['source'] != source:
                raise ValueError('This report ledger belongs to a different database path.')
            
            self.account_id = self.db.execute('SELECT account_id FROM identity WHERE id=1').fetchone()[0]
            # Caller holds the process lock. A previous sender died before saving
            # its outcome: it may have delivered, so manual reconciliation is needed.
            self.db.execute("UPDATE deliveries SET status='uncertain',last_error='Interrupted send; inspect Gmail Sent before resolving.' WHERE status='sending'")

    # Queue a report once per account and reporting period, regardless of recipient changes.
    def enqueue(self, report, subject, body, recipient):

        # UTC bounds identify the actual week, independent of recipient changes.
        # Hash the account and UTC reporting bounds to form stable report and email message IDs.
        digest = hashlib.sha256((self.account_id+'|'+report['start']+'|'+report['end']).encode()).hexdigest()[:24]
        report_id = 'weekly-'+digest
        message_id = f'<{report_id}@bitcoin-paper-report.invalid>'

        # Insert a pending delivery only if the report ID has not already been recorded.
        with self.db:
            self.db.execute('''INSERT OR IGNORE INTO deliveries
                (report_id,period_end,recipient,subject,body,message_id,status)
                VALUES(?,?,?,?,?,?,'pending')''',
                (report_id, report['end'], recipient, subject+' ['+report_id+']',
                 body+'\nReport ID: '+report_id+'\n', message_id))
            
        return report_id

    # Attempt the oldest pending report whose retry delay has elapsed.
    def deliver_one(self, sender, settings, clock=None):

        # Use the supplied test clock or current time to select an eligible delivery.
        clock = time.time() if clock is None else clock
        row = self.db.execute("""SELECT * FROM deliveries WHERE status='pending'
            AND next_attempt<=? ORDER BY period_end LIMIT 1""", (clock,)).fetchone()
        
        # Return without sending when no pending report is due.
        if row is None:
            return None
        
        # Claim the pending report and increment its attempt count before contacting the sender.
        with self.db:
            cursor = self.db.execute("""UPDATE deliveries SET status='sending',attempts=attempts+1
                WHERE report_id=? AND status='pending'""", (row['report_id'],))
            
            # Reject a report claim lost to another worker.
            if cursor.rowcount != 1:
                raise RuntimeError('Concurrent reporter detected.')
            
        # Send the saved subject, body, message ID, and recipient through the supplied adapter.
        try:
            sender.send(row['subject'], row['body'], row['message_id'], row['recipient'])

        # Schedule another attempt for retryable failures unless the attempt limit has been reached.
        except RetryableDeliveryError as error:
            status = 'failed' if row['attempts']+1 >= settings['max_attempts'] else 'pending'

            # Persist the retry or failed status and the next permitted attempt time.
            with self.db:
                self.db.execute('UPDATE deliveries SET status=?,next_attempt=?,last_error=? WHERE report_id=?',
                    (status, clock+settings['retry_seconds'], str(error), row['report_id']))
                
        # Treat unexpected failures as uncertain and retain only the exception class.
        except Exception as error:
            # Unexpected failures also have an unknown delivery outcome. Do not
            # include raw exception text, which could expose connection details.
            status = 'uncertain'

            # Persist an uncertain result so automatic delivery attempts stop.
            with self.db:
                self.db.execute("UPDATE deliveries SET status='uncertain',last_error=? WHERE report_id=?",
                                (type(error).__name__, row['report_id']))
                
        # Mark delivery successful only when the sender returns without an exception.
        else:
            status = 'sent'

            # Save the sent timestamp and clear any previous delivery error.
            with self.db:
                self.db.execute("UPDATE deliveries SET status='sent',sent_at=?,last_error=NULL WHERE report_id=?",
                    (datetime.now(timezone.utc).isoformat(), row['report_id']))
                
        return row['report_id'], status

    # Resolve a failed or uncertain report through an explicit sent or retry decision.
    def resolve(self, report_id, outcome):

        row = self.db.execute('SELECT status FROM deliveries WHERE report_id=?', (report_id,)).fetchone()
        
        # Allow manual resolution only for an existing failed or uncertain report.
        if row is None or row['status'] not in ('uncertain', 'failed'):
            raise ValueError('Only uncertain or failed reports can be resolved.')
        
        # Apply the selected resolution atomically.
        with self.db:
            # Record manual confirmation that the report was already sent.
            if outcome == 'sent':
                self.db.execute("UPDATE deliveries SET status='sent',last_error='Manually confirmed sent',sent_at=? WHERE report_id=?",
                    (datetime.now(timezone.utc).isoformat(), report_id))
                
            # Reset retry counters and make the report pending after explicit retry authorization.
            elif outcome == 'retry':
                self.db.execute("UPDATE deliveries SET status='pending',attempts=0,next_attempt=0,last_error='Manual retry authorized' WHERE report_id=?", (report_id,))
            
            # Reject unsupported resolution commands.
            else:
                raise ValueError('Resolution must be sent or retry.')

    # Close the report delivery database.
    def close(self):
        self.db.close()
