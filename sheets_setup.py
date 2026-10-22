"""Print the sheet template or check access. Never changes the trading database."""

# Import configuration parsing, environment loading, and spreadsheet validation helpers.
import argparse
import json
from pathlib import Path
from dotenv import load_dotenv
from services.sheet_config import flatten, checked_config, read_sheet, config_hash


# Print a complete sheet template or check access to the configured Google Sheet.
def main():
    # Parse the local configuration path and optional template-output mode.
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--template', action='store_true')
    args = parser.parse_args()
    # Load environment settings and validate the local configuration used as the sheet schema.
    load_dotenv(Path(__file__).with_name('.env'))
    config = json.loads(Path(args.config).read_text(encoding='utf-8'))
    checked_config(config, config)
    # Print template rows and stop without reading the remote spreadsheet.
    if args.template:
        print('key\tvalue')
        # Emit each flattened key with a tab-separated text or JSON-encoded value.
        for key, value in flatten(config).items():
            print(key + '\t' + (value if isinstance(value, str) else json.dumps(value)))
        return
    # Fetch and validate the configured sheet without applying it to a trading account.
    try:
        candidate = read_sheet(config)
    # Report the failure category without exposing credentials or response contents, then exit unsuccessfully.
    except Exception as error:
        print(f'Sheet check failed ({type(error).__name__}). Check the credentials path, '
            'API enablement, sheet sharing, tab name, and complete key/value rows.')
        raise SystemExit(1) from None
    # Print validation success and a shortened configuration hash for identification.
    print('Sheet read and validated successfully. No trades or database changes made.')
    print('Configuration hash:', config_hash(candidate)[:12])
    print('Live-account compatibility is also checked by paper_loop.py before applying changes.')


# Run the setup entry point only when executed directly.
if __name__ == '__main__':
    main()
