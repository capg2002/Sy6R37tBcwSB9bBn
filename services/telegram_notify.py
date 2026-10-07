"""Notifications are independent of execution. Never log a token-bearing URL."""
import os
import time
import requests


def format_trade(fill):
    text = (f"PAPER | {fill['strategy']} {fill['side']}\n"
            f"Time: {fill['timestamp']}\n"
            f"BTC: {fill['btc_amount']:.8f} at ${fill['btc_price']:,.2f}\n"
            f"Cash change: ${fill['cash_change']:,.2f}\n"
            f"Fee: ${fill['fee_usd']:.2f}\n"
            f"Reason: {fill['reason']}\n"
            f"Trade ID: {fill['trade_id']}")
    if 'realized_pnl' in fill:
        text += f"\nRealized P/L: ${fill['realized_pnl']:.2f}"
    return text


class DeliveryError(Exception):
    def __init__(self, message, retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


class Telegram:
    def __init__(self):
        self.token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
        self.chat = os.getenv('TELEGRAM_CHAT_ID', '').strip()
        if not self.token or not self.chat:
            raise ValueError('Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.')

    def send(self, text):
        try:
            response = requests.post(f'https://api.telegram.org/bot{self.token}/sendMessage',
                json={'chat_id': self.chat, 'text': text}, timeout=(5, 15))
            body = response.json()
        except (requests.RequestException, ValueError):
            # Requests exceptions may include the secret token in their URL.
            raise DeliveryError('Telegram transport/response failure') from None
        if not response.ok or not body.get('ok'):
            retry = body.get('parameters', {}).get('retry_after', 0)
            raise DeliveryError(f'Telegram rejected request (HTTP {response.status_code})', retry)
        return str(body['result']['message_id'])


def deliver_one(store, sender, now=None):
    now = time.time() if now is None else now
    row = store.db.execute('SELECT * FROM outbox WHERE sent_at IS NULL AND next_attempt<=? ORDER BY rowid LIMIT 1', (now,)).fetchone()
    if row is None:
        return False
    try:
        message_id = sender.send(row['message'])
    except DeliveryError as error:
        delay = max(float(error.retry_after), min(3600, 2 ** min(row['attempts'] + 1, 12)))
        with store.db:
            store.db.execute('UPDATE outbox SET attempts=attempts+1,next_attempt=?,last_error=? WHERE trade_id=?',
                             (now + delay, str(error), row['trade_id']))
    else:
        with store.db:
            store.db.execute('UPDATE outbox SET sent_at=?,message_id=?,last_error=NULL WHERE trade_id=?', (now, message_id, row['trade_id']))
    return True
