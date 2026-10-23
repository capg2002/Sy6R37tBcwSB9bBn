"""Quote freshness healthcheck, not a guarantee that forecasts or delivery work."""
# Import timestamp parsing, read-only database access, JSON decoding, and process exit support.
from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3
import sys

# Check the saved quote timestamp and report failure if reading or interpreting it raises an error.
try:
    # Locate the paper-account database at the fixed container runtime path.
    p = Path('/app/runtime/paper.sqlite')
    
    # Read the checkpoint engine state through a read-only connection with a two-second lock timeout.
    with sqlite3.connect(p.as_uri() + '?mode=ro', uri = True, timeout = 2) as db:
        row = db.execute('SELECT engine_json FROM checkpoint WHERE id=1').fetchone()

    # Decode the last quote timestamp and measure its age relative to the current UTC time.
    stamp = datetime.fromisoformat(json.loads(row[0])['last_quote_time'])
    age = (datetime.now(timezone.utc) - stamp).total_seconds()

    # Exit successfully only for quotes aged zero to 120 seconds; stale or future quotes fail the check.
    sys.exit(0 if 0 <= age <= 120 else 1)
    
# Return a failing exit status for missing state, invalid timestamps, or other ordinary exceptions.
except Exception:
    sys.exit(1)
