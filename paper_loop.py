"""Continuous PAPER execution using the same models, engine and Portfolio as main.py."""
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
from trading.models import EngineState
from trading.execution import ExecutionModel
from trading.engine import CandleResult, process_close, process_quote
from services.storage import SQLiteStore, new_portfolio
from market.feeds import CoinbaseFeed, FeedError
from market.features import add_features, compute_estimate
from market.missing_data import prepare_hourly_data

# Operational defaults only. Trading thresholds remain in your unchanged config.json.
POLL_SECONDS = 10
REFRESH_SECONDS = 60
MAX_QUOTE_AGE = 30
MAX_DECISION_DELAY = 300
OUTAGE_SECONDS = 60


def now():return pd.Timestamp.now(tz='UTC')

def fetch_history(hours):
    # Fetches already recorded history
    end=now().floor('h')
    return CoinbaseFeed().candles(end-pd.Timedelta(hours=hours),end)


def monitor_quote(timestamp,bid,ask,portfolio,state,execution):
    # Checks for gap in present call
    gap=state.last_quote_time is not None and (timestamp-state.last_quote_time).total_seconds()>OUTAGE_SECONDS
    if gap:
        state.pending_dca=state.pending_swing_buy=None
        if state.active_position is not None:state.execution_path_uncertain=True
    result=process_quote(timestamp,bid,ask,portfolio,state,execution)
    return result,gap


def print_fills(result):
    # Print what may need to be presently traded from previous
    for fill in result.fills:
        logging.info('PAPER %s %s %.8f BTC at %.2f | %s | %s',
                     fill.strategy,
                     fill.side,fill.btc_amount,
                     fill.fill_price,
                     fill.reason,
                     fill.trade_id)


def run(config,database,notify=False,once=False,sheets=False):

    # Sets up SQL database. Grants initial portfolio and engine state
    store=ConfigStore(database) if sheets else SQLiteStore(database)
    cache_path=Path(str(database)+'.config.json')
    if sheets:
        config=store.startup_config(config,cache_path)
    store.initialize(config,new_portfolio(config),EngineState(),
                     'paper',{'provider':'Coinbase Exchange',
                              'product':'BTC-USD','schema':1})
    
    # Bot restores previous portfolio and trading state
    portfolio,state,revision=store.restore()
    execution=ExecutionModel(**config['execution'])
    sheet_poller=None
    if sheets:
        store.record_initial_config(config,revision)
        refresh_cache(cache_path,config)
        if not once:
            sheet_poller=SheetPoller(config)

    # Retrieves BTC market data
    feed=CoinbaseFeed()

    # Fetch history handling
    threads=ThreadPoolExecutor(max_workers=1)

    # Fitting and forecast handling
    processes=ProcessPoolExecutor(max_workers=1)

    # Initialize asynchronous jobs
    candle_job=forecast_job=None
    forecast_context=None
    history=None
    next_quote=next_refresh=0.
    failures=0
    try:
        while True:
            # Apply only between forecasts. Never mix a forecast fitted under old
            # settings with a decision evaluated under new settings.
            if sheet_poller is not None:
                candidate=sheet_poller.tick(can_apply=forecast_job is None)
                if candidate is not None:
                    try:
                        candidate=require_live_compatible(candidate,config)
                    except ValueError as error:
                        logging.warning('Sheet configuration rejected: %s',error)
                    else:
                        if candidate != config:
                            # Database errors deliberately propagate: never keep
                            # trading with state that failed to commit.
                            config,state,revision=store.apply_config(candidate,config,state,revision)
                            refresh_cache(cache_path,config)
                            logging.info('Sheet configuration applied: %s',config_hash(config)[:12])
                        else:
                            logging.info('Sheet checked: configuration unchanged.')
            # Existing quote monitoring and trading logic follows.
            # Check if new quote must be pulled
            if time.monotonic()>=next_quote:
                next_quote=time.monotonic()+POLL_SECONDS
                try:

                    # Retreives current bid and ask

                    quote = feed.quote(now(),MAX_QUOTE_AGE)
                    stamp = now()
                    result,gap = monitor_quote(stamp,
                                               quote['bid'],
                                               quote['ask'],
                                               portfolio,
                                               state,
                                               execution)
                    
                    # Every quote result state gets committed and stored in SQL
                    revision = store.commit('quote:' + str(uuid.uuid4()),
                                          portfolio,
                                          state,
                                          result,
                                          revision,
                                          notify,
                                          [{'kind':'quote_gap_recovery','time':stamp}] if gap else [])
                    print_fills(result)
                    logging.info('Quote %.2f/%.2f | portfolio %.2f',
                                 quote['bid'],
                                 quote['ask'],
                                 result.portfolio_snapshot['portfolio_value'])
                    
                    failures = 0

                except FeedError as error:

                    failures += 1; next_quote = time.monotonic() + min(60, POLL_SECONDS * 2 ** min(failures-1,3))
                    logging.warning('%s No fills; saved state retained.',error)

            if once:
                break

            if candle_job is None and time.monotonic()>=next_refresh:
                candle_job=threads.submit(fetch_history,config['garch']['window_hours'] if history is None else 72)
                next_refresh=time.monotonic()+REFRESH_SECONDS

            if candle_job is not None and candle_job.done():
                job, candle_job=candle_job, None
                try:
                    raw = job.result()
                    if history is not None:
                        raw = pd.concat([history,raw]);raw=raw[~raw.index.duplicated(keep='last')].sort_index()
                    raw = raw.loc[raw.index >= now().floor('h') -pd.Timedelta(hours=config['garch']['window_hours'])]
                    history = prepare_hourly_data(raw, now = now())

                except (FeedError,ValueError,TypeError):
                    logging.warning('History unavailable; quote monitoring continues.')

            if forecast_job is not None and forecast_job.done():
                job,forecast_job=forecast_job,None
                timestamp,candle=forecast_context

                try:
                    estimate=job.result()
                except Exception as error:
                    estimate = None
                    logging.warning('Forecast failed (%s); swing entries blocked.',type(error).__name__)

                delay = (now() - (timestamp + pd.Timedelta(hours=1))).total_seconds()

                if 0 <= delay <= MAX_DECISION_DELAY:
                    exited = state.last_exit_hour is not None and state.last_exit_hour >= timestamp
                    # estimate=None explicitly means unavailable; it does not fit synchronously.

                    result = process_close(timestamp,candle,None,0,portfolio,state,config,exited,estimate)
                    event = {'kind' : 'close_decision',
                             'candle' : timestamp,
                             'actual_decision_time' : now(),
                             'config_hash' : config_hash(config)}
                else:
                    state.last_processed_candle = timestamp
                    state.pending_dca=state.pending_swing_buy = None
                    result = CandleResult()
                    event = {'kind' : 'late_forecast_skipped',
                           'candle' : timestamp,
                           'delay_seconds' : delay}
                revision = store.commit('close:' + timestamp.isoformat(),
                                        portfolio,
                                        state,
                                        result,
                                        revision,
                                        notify,
                                        [event])
                logging.info('Close processed: %s (%s)',timestamp,event['kind'])

            if history is not None and forecast_job is None:
                latest = history.index[-1]; expected = now().floor('h')-pd.Timedelta(hours=1)

                if latest == expected:
                    if state.last_processed_candle is None:
                        # First startup is warm-up, not a replay of historical purchases.
                        state.last_processed_candle=latest
                        revision=store.commit('bootstrap',portfolio,state,CandleResult(),revision,extra_events=[{'kind':'bootstrap','last_close':latest}])
                        logging.info('Warm-up loaded. Waiting for NEXT completed hourly candle.')

                    elif latest>state.last_processed_candle:
                        if latest-state.last_processed_candle > pd.Timedelta(hours=1):
                            state.pending_dca=state.pending_swing_buy = None

                            if state.active_position is not None:
                                state.execution_path_uncertain = True

                        frame = add_features(history,config); candle = frame.iloc[-1].copy()
                        age=(now() - (latest+pd.Timedelta(hours=1))).total_seconds()

                        if not candle.observed_ohlc or age>MAX_DECISION_DELAY:
                            state.pending_dca = state.pending_swing_buy = None

                            if not candle.observed_ohlc and state.active_position is not None:
                                state.execution_path_uncertain = True

                            state.last_processed_candle = latest
                            revision=store.commit('close:' + latest.isoformat(),
                                                  portfolio,
                                                  state,
                                                  CandleResult(),
                                                  revision,
                                                  extra_events=[{'kind':'invalid_or_late_candle','candle':latest}])
                        else:
                            store.save_forecast_input(latest,history)
                            forecast_context = (latest,candle)

                            # Time-based seed survives restarts; backtests keep original row-based seeds
                            seed = config['garch']['seed'] + int(latest.timestamp()//3600)
                            forecast_job = processes.submit(compute_estimate,frame.Close,config,seed)
            time.sleep(.2)
    except KeyboardInterrupt:
        logging.info('Stopped. Last committed state is saved.')
    finally:
        if sheet_poller is not None:
            sheet_poller.close()
        threads.shutdown(wait=False,cancel_futures=True)
        processes.shutdown(wait=False,cancel_futures=True)
        store.close()


def main():
    p = argparse.ArgumentParser();p.add_argument('--config',default='config.json')
    p.add_argument('--db',default='runtime/paper.sqlite');p.add_argument('--once',action='store_true')
    p.add_argument('--telegram',action='store_true',help='Queue notifications for NEW paper fills')
    p.add_argument('--sheets',action='store_true',help='Read validated Google Sheets settings hourly')
    a = p.parse_args()
    load_dotenv(Path(__file__).with_name('.env'))
    config=json.loads(Path(a.config).read_text());validate_config(config)
    path=Path(a.db).resolve();path.parent.mkdir(parents=True,exist_ok=True)
    log = RotatingFileHandler(path.parent/'paper.log',maxBytes=5_000_000,backupCount=3)
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s',handlers=[logging.StreamHandler(),log])
    with FileLock(str(path)+'.runner.lock',timeout=0):run(config,path,a.telegram,a.once,a.sheets)


if __name__=='__main__':main()
