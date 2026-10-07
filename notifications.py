"""Run in a second terminal so Telegram failures never block stop monitoring."""
import argparse
from pathlib import Path
import time
from dotenv import load_dotenv
from filelock import FileLock
from services.storage import SQLiteStore as Store
from services.telegram_notify import Telegram, deliver_one

# Sends notifications when a trade occurs, pulling from SQLite
def main():
    p=argparse.ArgumentParser();p.add_argument('--db',default='runtime/paper.sqlite')

    p.add_argument('--once',action='store_true');a=p.parse_args()

    load_dotenv(Path(__file__).with_name('.env'))

    sender = Telegram()

    path = Path(a.db).resolve()

    if not path.exists(): raise ValueError('Start paper.py first so the account exists.')

    with FileLock(str(path)+'.notifier.lock',timeout=0):
        store=Store(path)
        try:
            while True:
                worked = deliver_one(store,sender)
                if a.once:break
                time.sleep(1 if worked else 3)
        except KeyboardInterrupt:pass
        finally:store.close()

if __name__=='__main__':main()
