"""Local token check/chat-ID discovery; never prints token-bearing request URLs."""
import argparse
import os
from pathlib import Path
import requests
from dotenv import load_dotenv
from services.telegram_notify import Telegram


def main():
    load_dotenv(Path(__file__).with_name('.env'))
    p = argparse.ArgumentParser(); p.add_argument('--test',
                                                  action='store_true',
                                                  help='Send a test message to TELEGRAM_CHAT_ID')
    a = p.parse_args()

    if a.test:
        print('Sent test message ID:',Telegram().send('PAPER trading notification test. No trade was made.'))
        return
    
    token = os.getenv('TELEGRAM_BOT_TOKEN','').strip()

    if not token:
        raise ValueError('Set TELEGRAM_BOT_TOKEN in .env first.')
    
    try:
        response = requests.get(f'https://api.telegram.org/bot{token}/getUpdates',timeout=(5,15))
        body = response.json()
        if not response.ok or not body.get('ok'):
            print('Telegram rejected the request. Check the token and that this bot has no webhook.')
            return
        seen = set()
        for update in body['result']:
            chat = update.get('message',{}).get('chat',{})
            if chat.get('id') not in seen and 'id' in chat:
                print('Chat ID:',
                      chat['id'],
                      'Type:',
                      chat.get('type'),
                      'Name:',
                      chat.get('first_name',chat.get('title','')))
                seen.add(chat['id'])

        if not seen:
            print('Send /start to your bot in Telegram, then run this command again.')
    except (requests.RequestException,ValueError):
        print('Telegram connection/response failed; token not logged.')


if __name__=='__main__':main()
