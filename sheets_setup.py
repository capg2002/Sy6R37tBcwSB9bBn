"""Print the sheet template or check access. Never changes the trading database."""
import argparse
import json
from pathlib import Path
from dotenv import load_dotenv
from services.sheet_config import flatten, checked_config, read_sheet, config_hash


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--template', action='store_true')
    args = parser.parse_args()
    load_dotenv(Path(__file__).with_name('.env'))
    config = json.loads(Path(args.config).read_text(encoding='utf-8'))
    checked_config(config, config)
    if args.template:
        print('key\tvalue')
        for key, value in flatten(config).items():
            print(key + '\t' + (value if isinstance(value, str) else json.dumps(value)))
        return
    try:
        candidate = read_sheet(config)
    except Exception as error:
        print(f'Sheet check failed ({type(error).__name__}). Check the credentials path, '
              'API enablement, sheet sharing, tab name, and complete key/value rows.')
        raise SystemExit(1) from None
    print('Sheet read and validated successfully. No trades or database changes made.')
    print('Configuration hash:', config_hash(candidate)[:12])
    print('Live-account compatibility is also checked by paper_loop.py before applying changes.')


if __name__ == '__main__':
    main()
