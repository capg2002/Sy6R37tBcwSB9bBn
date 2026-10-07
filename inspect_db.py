"""Read-only inspection"""
import argparse
import json
from pathlib import Path
import sqlite3

# Ensures database has all required fields and structure
def main():
    p=argparse.ArgumentParser();p.add_argument('database');a=p.parse_args()
    db=sqlite3.connect(Path(a.database).resolve().as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
    row=db.execute('SELECT mode,portfolio_json,engine_json,revision FROM checkpoint WHERE id=1').fetchone()
    print(json.dumps(dict(mode=row['mode'],revision=row['revision'],portfolio=json.loads(row['portfolio_json']),engine=json.loads(row['engine_json'])),indent=2))
    print('Trade count:',db.execute('SELECT COUNT(*) FROM trades').fetchone()[0])
    print('Unsent notifications:',db.execute('SELECT COUNT(*) FROM outbox WHERE sent_at IS NULL').fetchone()[0])
    for trade in db.execute('SELECT timestamp,strategy,side,btc_amount,fill_price,reason FROM trades ORDER BY rowid DESC LIMIT 5'):
        print(dict(trade))
    db.close()


if __name__=='__main__':main()
