"""Continuous PAPER execution using the same models, engine and Portfolio as main.py."""

# Import background workers, logging, feeds, configuration services, and shared trading components.
import argparse
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import time
import uuid
import pandas as pd
from filelock import FileLock
from dotenv import load_dotenv
from services.sheet_config import SheetPoller, config_hash, require_live_compatible
from services.config_store import ConfigStore, refresh_cache
from services.config_validation import validate_config
from services.advisor_context import market_summary, advisory_context
from trading.models import EngineState
from trading.execution import ExecutionModel
from trading.engine import CandleResult, process_close, process_quote
from services.storage import SQLiteStore, new_portfolio
from market.feeds import CoinbaseFeed, FeedError
from market.features import add_features, compute_estimate
from market.missing_data import prepare_hourly_data

# Operational defaults only. Trading thresholds remain in your unchanged config.json.
# Define operational polling, freshness, decision-delay, and outage thresholds in seconds.
POLL_SECONDS = 10
REFRESH_SECONDS = 60
MAX_QUOTE_AGE = 30
MAX_DECISION_DELAY = 300
OUTAGE_SECONDS = 60


# Return the current timezone-aware UTC timestamp.
def now():
    return pd.Timestamp.now(tz='UTC')


# Fetch completed hourly candles ending at the start of the current UTC hour.
def fetch_history(hours):
    # Fetches already recorded history
    end = now().floor('h')
    return CoinbaseFeed().candles(end - pd.Timedelta(hours=hours), end)


# Process a fresh quote after clearing stale buy intents and identifying quote-gap uncertainty.
def monitor_quote(timestamp, bid, ask, portfolio, state, execution, config):
    # Checks for gap in present call
    gap = (state.last_quote_time is not None
        and (timestamp - state.last_quote_time).total_seconds() > OUTAGE_SECONDS)
    # Cancel pending entry buys when quote monitoring resumes after an excessive gap.
    if gap:
        state.pending_dca = state.pending_swing_buy = None
        # Mark execution uncertain when a swing position existed during the quote gap.
        if state.active_position is not None:
            state.execution_path_uncertain = True
    # Pass current bid and ask prices to the shared quote-processing engine.
    result = process_quote(timestamp,
        bid,
        ask,
        portfolio,
        state,
        execution,
        config,)
    return result, gap


# Log the execution details of fills already supplied by the engine.
def print_fills(result):
    # Print what may need to be presently traded from previous
    # Write one structured log entry for each fill in the processing result.
    for fill in result.fills:
        logging.info('PAPER %s %s %.8f BTC at %.2f | %s | %s',
            fill.strategy,
            fill.side,
            fill.btc_amount,
            fill.fill_price,
            fill.reason,
            fill.trade_id,)


# Run persistent paper execution with asynchronous history, forecasting, and optional sheet polling.
def run(config, database, notify=False, once=False, sheets=False):

    # Sets up SQL database. Grants initial portfolio and engine state
    # Choose account storage and the JSON fallback cache path.
    store = ConfigStore(database) if sheets else SQLiteStore(database)
    cache_path = Path(str(database) + '.config.json')
    # Restore authoritative spreadsheet-managed settings before account initialization.
    if sheets:
        config = store.startup_config(config, cache_path)
    # Initialize or validate the paper checkpoint against the configured Coinbase account context.
    store.initialize(config,
        new_portfolio(config),
        EngineState(),
        'paper',
        {'provider': 'Coinbase Exchange', 'product': 'BTC-USD', 'schema': 1},)

    # Bot restores previous portfolio and trading state
    # Restore saved balances and engine state, then configure execution fees and slippage.
    portfolio, state, revision = store.restore()
    execution = ExecutionModel(**config['execution'])
    sheet_poller = None
    # Record initial configuration history and refresh the local cache when sheet integration is enabled.
    if sheets:
        store.record_initial_config(config, revision)
        refresh_cache(cache_path, config)
        # Start background sheet polling only for continuous execution.
        if not once:
            sheet_poller = SheetPoller(config)

    # Retrieves BTC market data
    # Create the quote feed, a history-fetch thread, and a separate forecast process.
    feed = CoinbaseFeed()

    # Fetch history handling
    threads = ThreadPoolExecutor(max_workers=1)

    # Fitting and forecast handling
    processes = ProcessPoolExecutor(max_workers=1)

    # Initialize asynchronous jobs
    # Initialize asynchronous jobs, history, polling clocks, and quote failure tracking.
    candle_job = forecast_job = None
    forecast_context = None
    history = None
    next_quote = next_refresh = 0.0
    failures = 0
    # Run the trading loop with interruption handling and guaranteed worker/storage cleanup.
    try:
        # Service configuration updates, live quotes, completed candles, and forecast results repeatedly.
        while True:
            # Apply only between forecasts. Never mix a forecast fitted under old
            # settings with a decision evaluated under new settings.
            # Poll for configuration changes when spreadsheet integration is active.
            if sheet_poller is not None:
                candidate = sheet_poller.tick(can_apply=forecast_job is None)
                # Validate a completed configuration candidate before attempting to apply it.
                if candidate is not None:
                    # Check the full configuration and locked settings against the currently active settings.
                    try:
                        candidate = require_live_compatible(candidate, config)
                    # Log an invalid spreadsheet candidate and retain the current configuration.
                    except ValueError as error:
                        logging.warning('Sheet configuration rejected: %s', error)
                    # Apply only candidates that passed compatibility validation.
                    else:
                        # Atomically save changed settings and canceled entry intents, then refresh the fallback cache.
                        if candidate != config:
                            # Database errors deliberately propagate: never keep
                            # trading with state that failed to commit.
                            config, state, revision = store.apply_config(candidate, config, state, revision)
                            refresh_cache(cache_path, config)
                            logging.info('Sheet configuration applied: %s', config_hash(config)[:12])
                        # Log successful validation when the sheet matches the current configuration.
                        else:
                            logging.info('Sheet checked: configuration unchanged.')
            # Existing quote monitoring and trading logic follows.
            # Check if new quote must be pulled
            # Fetch another quote only after its scheduled polling time.
            if time.monotonic() >= next_quote:
                next_quote = time.monotonic() + POLL_SECONDS
                # Process a fresh quote and persist the resulting account state.
                try:

                    # Retreives current bid and ask

                    quote = feed.quote(now(), MAX_QUOTE_AGE)
                    stamp = now()
                    result, gap = monitor_quote(stamp,
                        quote["bid"],
                        quote["ask"],
                        portfolio,
                        state,
                        execution,
                        config,)

                    # Every quote result state gets committed and stored in SQL
                    # Commit the quote result with a unique event key and an optional gap-recovery audit event.
                    revision = store.commit('quote:' + str(uuid.uuid4()),
                        portfolio,
                        state,
                        result,
                        revision,
                        notify,
                        [{'kind': 'quote_gap_recovery', 'time': stamp}] if gap else [],)
                    print_fills(result)
                    # Log committed fills and the current quote valuation.
                    logging.info('Quote %.2f/%.2f | portfolio %.2f',
                        quote['bid'],
                        quote['ask'],
                        result.portfolio_snapshot['portfolio_value'],)

                    failures = 0

                # Back off after feed errors while keeping the last saved account state.
                except FeedError as error:

                    failures += 1
                    next_quote = time.monotonic() + min(60, POLL_SECONDS * 2 ** min(failures - 1, 3))
                    logging.warning('%s No fills; saved state retained.', error)

            # End after the quote attempt in single-run mode.
            if once:
                break

            # Schedule a background history request when none is active and refresh is due.
            if candle_job is None and time.monotonic() >= next_refresh:
                candle_job = threads.submit(fetch_history, config['garch']['window_hours'] if history is None else 72)
                next_refresh = time.monotonic() + REFRESH_SECONDS

            # Consume a completed history fetch without waiting on the trading thread.
            if candle_job is not None and candle_job.done():
                job, candle_job = candle_job, None
                # Merge and normalize the returned history, catching supported feed or data errors.
                try:
                    raw = job.result()
                    # Merge recent fetched candles with existing history and retain the latest copy of duplicate timestamps.
                    if history is not None:
                        raw = pd.concat([history, raw])
                        raw = raw[~raw.index.duplicated(keep='last')].sort_index()
                    # Trim history to the configured GARCH window before preparing hourly data.
                    raw = raw.loc[
                        raw.index
                        >= now().floor('h') - pd.Timedelta(hours=config['garch']['window_hours'])
                    ]
                    history = prepare_hourly_data(raw, now=now())

                # Log unavailable history while allowing quote monitoring to continue.
                except (FeedError, ValueError, TypeError):
                    logging.warning('History unavailable; quote monitoring continues.')

            # Consume a completed forecast together with the candle context captured when it started.
            if forecast_job is not None and forecast_job.done():
                job, forecast_job = forecast_job, None
                timestamp, candle, advisor_market = forecast_context

                # Retrieve the background forecast result.
                try:
                    estimate = job.result()
                # Treat forecast failures as unavailable estimates and log only the exception class.
                except Exception as error:
                    estimate = None
                    logging.warning('Forecast failed (%s); swing entries blocked.', type(error).__name__)

                # Measure decision delay from the completed candle's closing time.
                delay = (now() - (timestamp + pd.Timedelta(hours=1))).total_seconds()

                # Evaluate the close decision only while the forecast remains within the allowed delay.
                if 0 <= delay <= MAX_DECISION_DELAY:
                    exited = state.last_exit_hour is not None and state.last_exit_hour >= timestamp
                    # estimate=None explicitly means unavailable; it does not fit synchronously.

                    # Process the candle close with the current account state and completed forecast.
                    result = process_close(timestamp, candle, None, 0, portfolio, state, config, exited, estimate)
                    # Record actual decision time and the configuration hash for the close audit event.
                    event = {
                        'kind': 'close_decision',
                        'candle': timestamp,
                        'actual_decision_time': now(),
                        'config_hash': config_hash(config),
                    }
                    # Optional diagnostics must not prevent committing a trade decision.
                    # Attach optional advisor diagnostics without making them a prerequisite for committing the decision.
                    try:
                        event['advisor_input'] = advisory_context(timestamp, advisor_market, estimate, portfolio, state, config)
                    # Log unavailable advisor input without discarding the trading decision.
                    except Exception as error:
                        logging.warning('Advisor input unavailable (%s).', type(error).__name__)
                # Mark an out-of-window forecast as skipped and clear pending entry buys.
                else:
                    state.last_processed_candle = timestamp
                    state.pending_dca = state.pending_swing_buy = None
                    result = CandleResult()
                    event = {
                        'kind': 'late_forecast_skipped',
                        'candle': timestamp,
                        'delay_seconds': delay,
                    }
                # Commit either the evaluated close decision or the skipped-forecast event.
                revision = store.commit('close:' + timestamp.isoformat(),
                    portfolio,
                    state,
                    result,
                    revision,
                    notify,
                    [event],)
                logging.info('Close processed: %s (%s)', timestamp, event['kind'])

            # Consider a new forecast only when prepared history exists and no forecast is active.
            if history is not None and forecast_job is None:
                latest = history.index[-1]
                expected = now().floor('h') - pd.Timedelta(hours=1)

                # Proceed only when history ends at the most recently completed hourly candle.
                if latest == expected:
                    # Treat the first available close as warm-up rather than replaying historical entries.
                    if state.last_processed_candle is None:
                        # First startup is warm-up, not a replay of historical purchases.
                        state.last_processed_candle = latest
                        revision = store.commit('bootstrap',
                            portfolio,
                            state,
                            CandleResult(),
                            revision,
                            extra_events=[{'kind': 'bootstrap', 'last_close': latest}],)
                        logging.info('Warm-up loaded. Waiting for NEXT completed hourly candle.')

                    # Handle only candles later than the last processed close.
                    elif latest > state.last_processed_candle:
                        # Clear pending buys when one or more hourly decisions were missed.
                        if latest - state.last_processed_candle > pd.Timedelta(hours=1):
                            state.pending_dca = state.pending_swing_buy = None

                            # Flag execution uncertainty when a position spans the missing decision interval.
                            if state.active_position is not None:
                                state.execution_path_uncertain = True

                        # Calculate indicators and measure the age of the latest completed candle.
                        frame = add_features(history, config)
                        candle = frame.iloc[-1].copy()
                        age = (now() - (latest + pd.Timedelta(hours=1))).total_seconds()

                        # Skip entry processing for unobserved OHLC data or a candle beyond the decision delay.
                        if not candle.observed_ohlc or age > MAX_DECISION_DELAY:
                            state.pending_dca = state.pending_swing_buy = None

                            # Flag position uncertainty when the skipped candle has unobserved market data.
                            if not candle.observed_ohlc and state.active_position is not None:
                                state.execution_path_uncertain = True

                            # Commit the skipped close and its invalid-or-late audit event.
                            state.last_processed_candle = latest
                            revision = store.commit('close:' + latest.isoformat(),
                                portfolio,
                                state,
                                CandleResult(),
                                revision,
                                extra_events=[
                                    {'kind': 'invalid_or_late_candle', 'candle': latest}
                                ],)
                        # Persist forecast input and schedule forecasting only for an eligible observed candle.
                        else:
                            store.save_forecast_input(latest, history)
                            # Prepare the market summary used by optional advisor diagnostics.
                            try:
                                advisor_market = market_summary(frame)
                            # Use an empty advisor summary if diagnostics cannot be calculated.
                            except Exception:
                                advisor_market = {}
                            forecast_context = (latest, candle, advisor_market)

                            # Time-based seed survives restarts; backtests keep original row-based seeds
                            # Derive a restart-stable seed from the candle hour and submit the forecast to the process worker.
                            seed = config['garch']['seed'] + int(latest.timestamp() // 3600)
                            forecast_job = processes.submit(compute_estimate, frame.Close, config, seed)
            # Pause briefly between loop iterations to avoid continuous busy polling.
            time.sleep(0.2)
    # Log a keyboard interruption while retaining the last committed account state.
    except KeyboardInterrupt:
        logging.info('Stopped. Last committed state is saved.')
    # Release optional polling resources, worker pools, and the account database on exit.
    finally:
        # Close the sheet poller only if it was started.
        if sheet_poller is not None:
            sheet_poller.close()
        threads.shutdown(wait=False, cancel_futures=True)
        processes.shutdown(wait=False, cancel_futures=True)
        store.close()


# Parse runner options, validate configuration, configure logs, and acquire account ownership.
def main():
    # Define database, configuration, single-run, notification, and spreadsheet options.
    p = argparse.ArgumentParser()
    p.add_argument('--config', default='config.json')
    p.add_argument('--db', default='runtime/paper.sqlite')
    p.add_argument('--once', action='store_true')
    p.add_argument('--telegram', action='store_true', help='Queue notifications for NEW paper fills')
    p.add_argument('--sheets', action='store_true', help='Read validated Google Sheets settings hourly')
    a = p.parse_args()
    # Load credentials and validate the configuration before initializing the runner.
    load_dotenv(Path(__file__).with_name('.env'))
    config = json.loads(Path(a.config).read_text())
    validate_config(config)
    # Create the runtime directory and rotating paper-execution log.
    path = Path(a.db).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    log = RotatingFileHandler(path.parent / 'paper.log', maxBytes=5_000_000, backupCount=3)
    logging.basicConfig(level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
        handlers=[logging.StreamHandler(), log],)
    # Hold the runner lock so only one cooperating trader writes this account.
    with FileLock(str(path) + '.runner.lock', timeout=0):
        run(config, path, a.telegram, a.once, a.sheets)


# Start the paper runner only when this file is executed directly.
if __name__ == '__main__':
    main()
