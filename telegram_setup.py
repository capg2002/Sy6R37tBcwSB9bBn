"""Local token check/chat-ID discovery; never prints token-bearing request URLs."""

# Import argument parsing, environment credentials, HTTP access, and the Telegram sender.
import argparse
import os
from pathlib import Path
import requests
from dotenv import load_dotenv
from services.telegram_notify import Telegram


# Send an explicitly requested test message or discover chat IDs from bot updates.
def main():
    # Load bot settings and parse the optional test-message flag.
    load_dotenv(Path(__file__).with_name('.env'))
    p = argparse.ArgumentParser()
    p.add_argument('--test', action='store_true', help='Send a test message to TELEGRAM_CHAT_ID')
    a = p.parse_args()

    # Send the fixed notification test to the configured chat, then stop.
    if a.test:
        print('Sent test message ID:',
            Telegram().send('PAPER trading notification test. No trade was made.'),)
        return

    # Read the bot token without printing it.
    token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()

    # Reject chat discovery when no bot token is configured.
    if not token:
        raise ValueError('Set TELEGRAM_BOT_TOKEN in .env first.')

    # Request recent bot updates and parse the response using bounded network timeouts.
    try:
        response = requests.get(f'https://api.telegram.org/bot{token}/getUpdates', timeout=(5, 15))
        body = response.json()
        # Report a rejected Telegram response without exposing the token-bearing URL.
        if not response.ok or not body.get('ok'):
            print('Telegram rejected the request. Check the token and that this bot has no webhook.')
            return
        # Track chat IDs already printed during this discovery run.
        seen = set()
        # Inspect the message chat attached to each returned bot update.
        for update in body['result']:
            chat = update.get('message', {}).get('chat', {})
            # Print each available chat ID once along with its type and display name.
            if chat.get('id') not in seen and 'id' in chat:
                print('Chat ID:',
                    chat['id'],
                    'Type:',
                    chat.get('type'),
                    'Name:',
                    chat.get('first_name', chat.get('title', '')),)
                seen.add(chat['id'])

        # Print setup guidance when no message chat IDs were found.
        if not seen:
            print('Send /start to your bot in Telegram, then run this command again.')
    # Report transport or response-decoding failure without logging request credentials.
    except (requests.RequestException, ValueError):
        print('Telegram connection/response failed; token not logged.')


# Run the Telegram setup utility only when executed directly.
if __name__ == '__main__':
    main()
