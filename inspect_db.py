"""Read-only inspection"""

# Import argument parsing, JSON formatting, path handling, and SQLite access.
import argparse
import json
from pathlib import Path
import sqlite3


# Ensures database has all required fields and structure
# Display the saved checkpoint, trade counts, pending notifications, and recent fills.
def main():
    # Read the database path from the command line.
    p = argparse.ArgumentParser()
    p.add_argument('database')
    a = p.parse_args()
    # Open the existing database read-only and fetch the account checkpoint.
    db = sqlite3.connect(Path(a.database).resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    row = db.execute('SELECT mode,portfolio_json,engine_json,revision FROM checkpoint WHERE id=1').fetchone()
    # Decode the saved portfolio and engine state for an indented JSON display.
    print(json.dumps(dict(mode=row['mode'],
                revision=row['revision'],
                portfolio=json.loads(row['portfolio_json']),
                engine=json.loads(row['engine_json']),),
            indent=2,))
    # Print the total fill count and the number of notifications still awaiting delivery.
    print('Trade count:', db.execute('SELECT COUNT(*) FROM trades').fetchone()[0])
    print('Unsent notifications:',
        db.execute('SELECT COUNT(*) FROM outbox WHERE sent_at IS NULL').fetchone()[0],)
    # Print the five most recently inserted trade records with execution details.
    for trade in db.execute('SELECT timestamp,strategy,side,btc_amount,fill_price,reason FROM trades ORDER BY rowid DESC LIMIT 5'):
        print(dict(trade))
    # Close the inspection connection after printing the selected records.
    db.close()


# Run the inspection entry point only when this file is executed directly.
if __name__ == '__main__':
    main()
