"""Run in a second terminal so Telegram failures never block stop monitoring."""

# Import notification polling, credentials, locking, and outbox storage dependencies.
import argparse
from pathlib import Path
import time
from dotenv import load_dotenv
from filelock import FileLock
from services.storage import SQLiteStore as Store
from services.telegram_notify import Telegram, deliver_one


# Sends notifications when a trade occurs, pulling from SQLite
# Deliver queued Telegram notifications independently of the trading loop.
def main():
    # Parse the account path and optional single-attempt mode.
    p = argparse.ArgumentParser()
    p.add_argument('--db', default='runtime/paper.sqlite')

    p.add_argument('--once', action='store_true')
    a = p.parse_args()

    # Load credentials and construct the Telegram sender.
    load_dotenv(Path(__file__).with_name('.env'))

    sender = Telegram()

    path = Path(a.db).resolve()

    # Require an existing account database before starting the notifier.
    if not path.exists():
        raise ValueError('Start paper.py first so the account exists.')

    # Hold the notifier lock to prevent simultaneous workers for this account.
    with FileLock(str(path) + '.notifier.lock', timeout=0):
        store = Store(path)
        # Poll the outbox with guaranteed database cleanup.
        try:
            # Attempt eligible notifications repeatedly until stopped or single-run mode completes.
            while True:
                worked = deliver_one(store, sender)
                # Exit after one outbox attempt when --once is selected.
                if a.once:
                    break
                # Poll more quickly after an attempted message and more slowly when the outbox has no due work.
                time.sleep(1 if worked else 3)
        # Allow a keyboard interruption to stop the worker without printing a traceback.
        except KeyboardInterrupt:
            pass
        # Close the account store on exit.
        finally:
            store.close()


# Run the notifier entry point only when this file is executed directly.
if __name__ == '__main__':
    main()
