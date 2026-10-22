"""Notifications are independent of execution. Never log a token-bearing URL."""
# Import environment access, retry timing, and HTTP request support.
import os
import time
import requests


# Format a paper-trade accounting record as a readable Telegram message.
def format_trade(fill):

    # Include trade identity, execution details, cash change, fees, and exit or entry reason.
    text = (f"PAPER | {fill['strategy']} {fill['side']}\n"
            f"Time: {fill['timestamp']}\n"
            f"BTC: {fill['btc_amount']:.8f} at ${fill['btc_price']:,.2f}\n"
            f"Cash change: ${fill['cash_change']:,.2f}\n"
            f"Fee: ${fill['fee_usd']:.2f}\n"
            f"Reason: {fill['reason']}\n"
            f"Trade ID: {fill['trade_id']}")
    
    # Append realized profit or loss when the record represents an accounted sale.
    if 'realized_pnl' in fill:
        text += f"\nRealized P/L: ${fill['realized_pnl']:.2f}"

    return text


# Represent a Telegram delivery failure with an optional provider retry delay.
class DeliveryError(Exception):

    # Store the error description and retry-after interval for outbox scheduling.
    def __init__(self, message, retry_after=0):

        super().__init__(message)
        self.retry_after = retry_after


# Send Telegram messages using bot credentials stored in environment variables.
class Telegram:

    # Load the bot token and destination chat ID.
    def __init__(self):

        self.token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
        self.chat = os.getenv('TELEGRAM_CHAT_ID', '').strip()

        # Reject initialization when either required credential setting is missing.
        if not self.token or not self.chat:
            raise ValueError('Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.')

    # Submit a message and return its Telegram message ID on confirmed success.
    def send(self, text):

        # Send the JSON request with timeouts and decode the response body.
        try:
            response = requests.post(f'https://api.telegram.org/bot{self.token}/sendMessage',
                json = {'chat_id': self.chat, 
                        'text': text}, 
                        timeout = (5, 15))
            body = response.json()

        # Replace transport and JSON errors with a generic message that omits token-bearing URLs.
        except (requests.RequestException, ValueError):
            # Requests exceptions may include the secret token in their URL.
            raise DeliveryError('Telegram transport/response failure') from None
        # Reject unsuccessful API responses and retain any provider retry-after interval.
        if not response.ok or not body.get('ok'):
            retry = body.get('parameters', {}).get('retry_after', 0)
            raise DeliveryError(f'Telegram rejected request (HTTP {response.status_code})', retry)
        # Return the confirmed message ID as text for persistence in the outbox.
        return str(body['result']['message_id'])


# Attempt the oldest eligible unsent outbox message and persist its delivery result.
def deliver_one(store, sender, now=None):
    # Select a due message using the supplied clock or the current time.
    now = time.time() if now is None else now
    row = store.db.execute('SELECT * FROM outbox WHERE sent_at IS NULL AND next_attempt<=? ORDER BY rowid LIMIT 1', (now,)).fetchone()
    
    # Return False when no message is currently eligible for delivery.
    if row is None:
        return False
    
    # Send the saved message through the supplied Telegram adapter.
    try:
        message_id = sender.send(row['message'])

    # Use the greater of provider retry-after and capped exponential backoff after a delivery failure.
    except DeliveryError as error:
        delay = max(float(error.retry_after), min(3600, 2 ** min(row['attempts'] + 1, 12)))

        # Save the attempt count, next attempt time, and error without changing account balances.
        with store.db:
            store.db.execute('UPDATE outbox SET attempts=attempts+1,next_attempt=?,last_error=? WHERE trade_id=?',
                             (now + delay, str(error), row['trade_id']))
            
    # Mark success only after the sender has returned a message ID.
    else:
        # Store the sent timestamp and message ID, then clear the previous error.
        with store.db:
            store.db.execute('UPDATE outbox SET sent_at=?,message_id=?,last_error=NULL WHERE trade_id=?', (now, message_id, row['trade_id']))
    
    # Return True when an eligible message was attempted, even if delivery failed.
    return True
