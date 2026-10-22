"""One real Gemini request with synthetic data; no database or orders."""

# Import environment loading, validated advisor settings, and the Gemini client.
import json
from pathlib import Path
from dotenv import load_dotenv
from advisor import load_settings
from services.llm_advisor import GeminiAdvisor


# Perform one real Gemini request using synthetic market data without accessing the trading database.
def main():
    # Load credentials and advisor settings relative to this script.
    root = Path(__file__).resolve().parent
    load_dotenv(root / '.env')
    settings = load_settings(root / 'advisor.json')
    # Build fixed market indicators, returns, and volatility for a reproducible connection-test input.
    context = {
        'current_mode': 'hybrid',
        'market': {
            'Close': 60000,
            'ATR': 900,
            'SMA_20': 60100,
            'SMA_50': 60500,
            'PREV_SMA_20': 60090,
            'PREV_SMA_50': 60510,
            'observed_ohlc': True,
            'return_1h_fraction': -0.005,
            'return_24h_fraction': -0.02,
            'missing_or_invalid_hours_in_window': 0,
        },
        'garch': {'sigma_next_hour_fraction': 0.012},
    }
    # Create the provider using the validated settings.
    provider = GeminiAdvisor(settings)
    # Submit the synthetic context and print the returned status, advice, and metadata.
    try:
        status, advice, metadata = provider.advise(context)
        print(json.dumps({'synthetic_test': True, 'status': status, 'advice': advice, 'metadata': metadata},
                indent=2,))
        # Exit unsuccessfully if the provider did not return a validated ok result.
        if status != 'ok':
            raise SystemExit(1)
    # Print the exception category and exit unsuccessfully after a request failure.
    except Exception as error:
        print('Gemini test failed:', type(error).__name__)

        # Our adapter creates this message using only the HTTP status code.
        # Print the adapter's sanitized HTTP-status message only for its dedicated HTTP error type.
        if type(error).__name__ == "GeminiHTTPError":
            print(str(error))
        raise SystemExit(1) from None
    # Always close the provider session after the connection test.
    finally:
        provider.close()


# Run the connection test only when executed directly.
if __name__ == '__main__':
    main()
