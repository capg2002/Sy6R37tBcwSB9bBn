"""Atomic SQLite persistence for Portfolio, EngineState, fills and audit events."""
# Import dataclass serialization, database support, and trading state models.
from dataclasses import asdict
import json
import math
from pathlib import Path
import sqlite3
import time
import pandas as pd
from trading.models import EngineState, OrderIntent, ActivePosition
from trading.portfolio import Portfolio

# List the portfolio fields stored in account checkpoints.
PORTFOLIO_FIELDS = ('starting_cash','dca_cash','swing_cash','reserve_cash','dca_cost',
                    'active_cost','realized_pnl','dca_btc','active_btc','fee_rate','slippage_rate')


# Recursively convert stored values into portable JSON-compatible data.
def json_value(value):
    """Convert dataclass payloads and pandas/numpy scalars to portable JSON."""
    # Convert each dictionary value recursively while retaining its key.
    if isinstance(value,dict):
        return {k: json_value(v) for k,v in value.items()}
    # Convert list and tuple elements recursively into a JSON array.
    if isinstance(value,(list,tuple)):
        return [json_value(v) for v in value]
    # Serialize pandas timestamps using ISO text.
    if isinstance(value,pd.Timestamp):
        return value.isoformat()
    # Unwrap scalar objects exposing item() and normalize the resulting value recursively.
    if hasattr(value,'item'):
        return json_value(value.item())
    # Represent non-finite diagnostic floats as JSON null.
    if isinstance(value,float) and not math.isfinite(value):
        return None  # diagnostic warm-up values
    return value


# Produce deterministic JSON after converting timestamps and scalar values.
def dumps(value):
    return json.dumps(json_value(value), sort_keys = True, allow_nan = False)


# Create a portfolio using the configured budget pools, reserve, fees, and slippage.
def new_portfolio(config):

    return Portfolio(config['starting_budget'], config['dca']['allocation'],
        config['swing']['allocation'], config['reserve'], **config['execution'])


# Rebuild typed engine state from its saved dictionary representation.
def state_from_dict(data):

    data = dict(data)
    # Restore each pending order slot from its serialized dictionary.
    for key in ('pending_dca','pending_swing_buy','pending_swing_sell'):
        
        # Recreate an OrderIntent only when the pending slot contains an order.
        if data[key] is not None:
            order = dict(data[key]); order['signal_time'] = pd.Timestamp(order['signal_time'])
            data[key] = OrderIntent(**order)

    # Recreate the active-position object when a position was saved.
    if data['active_position'] is not None:
        data['active_position'] = ActivePosition(**data['active_position'])

    # Restore the engine timestamps used for candle, quote, and exit tracking.
    for key in ('last_processed_candle','last_quote_time','last_exit_hour'):
        # Convert a timestamp only when its saved value is present.
        if data.get(key) is not None:
            data[key] = pd.Timestamp(data[key])

    return EngineState(**data)


# Persist account state, fills, snapshots, audit events, and notification tasks atomically.
class SQLiteStore:
    # Open the account database and create the required tables with durable SQLite settings.
    def __init__(self,path):
        self.path = Path(path); self.path.parent.mkdir(parents = True, exist_ok = True)
        self.db=sqlite3.connect(self.path, timeout = 10)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')

        # Create checkpoint, event, trade, snapshot, outbox, and forecast-input tables if absent.
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS checkpoint (
            id INTEGER PRIMARY KEY CHECK(id=1), mode TEXT NOT NULL, config_json TEXT NOT NULL,
            context_json TEXT NOT NULL, portfolio_json TEXT NOT NULL, engine_json TEXT NOT NULL,
            revision INTEGER NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS processed_events (
            event_key TEXT PRIMARY KEY, committed_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS trades (
            trade_id TEXT PRIMARY KEY, order_id TEXT UNIQUE NOT NULL, timestamp TEXT NOT NULL,
            strategy TEXT NOT NULL, side TEXT NOT NULL, reason TEXT NOT NULL,
            btc_amount REAL NOT NULL, fill_price REAL NOT NULL, fee_usd REAL NOT NULL,
            realized_pnl REAL, fill_json TEXT NOT NULL, accounting_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS snapshots (
            event_key TEXT PRIMARY KEY, timestamp TEXT NOT NULL, portfolio_value REAL NOT NULL,
            cash REAL NOT NULL, dca_btc REAL NOT NULL, active_btc REAL NOT NULL,
            payload_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY, event_key TEXT NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS outbox (
            trade_id TEXT PRIMARY KEY, message TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt REAL NOT NULL DEFAULT 0, sent_at REAL, message_id TEXT, last_error TEXT);
        CREATE TABLE IF NOT EXISTS forecast_inputs (
            candle_open TEXT PRIMARY KEY, history_csv TEXT NOT NULL);
        ''')

    # Initialize a new checkpoint or verify that an existing account matches the requested run.
    def initialize(self,config,portfolio,state,mode,context=None):
        
        # Reject unsupported run modes before changing account storage.
        if mode not in ('backtest','paper'):
            raise ValueError('Mode must be backtest or paper.')
        context={} if context is None else context

        # Check or create the initial account checkpoint within one transaction.
        with self.db:
            row = self.db.execute('SELECT * FROM checkpoint WHERE id=1').fetchone()

            # Compare an existing checkpoint with the requested mode, configuration, and input context.
            if row:
                # Reject an attempt to reuse the database with different account settings or input context.
                if row['mode'] != mode or row['config_json'] != dumps(config) or row['context_json'] != dumps(context):
                    raise ValueError('Existing database has different settings/mode/input. Use its original settings or an explicitly NEW database.')
            # Create revision zero from the supplied portfolio and engine state for a new account.
            else:
                self.db.execute('INSERT INTO checkpoint VALUES(1,?,?,?,?,?,0,?)',
                    (mode,
                     dumps(config),
                     dumps(context),
                     dumps(self.portfolio_state(portfolio)),
                     dumps(asdict(state)),
                     time.time()))

    # Extract the checkpointed portfolio values without altering the portfolio object.
    @staticmethod
    def portfolio_state(portfolio):
        result = {key:getattr(portfolio,key) for key in PORTFOLIO_FIELDS}

        # Reject non-finite balances or accounting values before saving a checkpoint.
        if any(not math.isfinite(v) for v in result.values()):
            raise ValueError('Nonfinite portfolio values cannot be checkpointed.')
        return result

    # Restore the committed portfolio, order guards, engine state, and revision.
    def restore(self):
        row = self.db.execute('SELECT * FROM checkpoint WHERE id=1').fetchone()

        # Reject restoration when the database has no initialized account checkpoint.
        if row is None:
            raise ValueError('Database has not been initialized.')
        
        # Recreate a portfolio using the saved configuration before restoring its recorded balances.
        config = json.loads(row['config_json']);portfolio=new_portfolio(config)

        # Assign each saved portfolio field to the reconstructed portfolio.
        for key,value in json.loads(row['portfolio_json']).items():
            setattr(portfolio,key,value)

        # Restore audit history/ID guards without reapplying fills to balances.
        # Restore trade history and duplicate-order guards without applying fills to balances again.
        for trade in self.db.execute('SELECT trade_id,order_id,accounting_json FROM trades ORDER BY rowid'):
            record = json.loads(trade['accounting_json']); record['timestamp'] = pd.Timestamp(record['timestamp'])
            portfolio.trades.append(record)
            portfolio.applied_trade_ids.add(trade['trade_id']); portfolio.applied_order_ids.add(trade['order_id'])

        # Rebuild the typed engine state from its checkpoint JSON.
        state = state_from_dict(json.loads(row['engine_json']))

        # Reject a mismatch between active swing holdings and the presence of an active position.
        if (state.active_position is None) != (portfolio.active_btc == 0):
            raise ValueError('Saved position/holdings disagree; refusing to run.')
        
        return portfolio,state,row['revision']

    # Check whether an event key has already been committed.
    def contains(self, event_key):
        return self.db.execute('SELECT 1 FROM processed_events WHERE event_key=?',(event_key,)).fetchone() is not None

    # Commit an event and all related state, fills, snapshots, and audit records together.
    def commit(self, event_key, portfolio,state, result, expected_revision, notify = False, extra_events = ()):
        """On failure stop/reload; do not continue using uncommitted in-memory objects."""
        # Associate new fills with their accounting records and serialize the proposed checkpoint.
        records = {trade.get('trade_id'): trade for trade in portfolio.trades[-len(result.fills):]} if result.fills else {}
        p = dumps(self.portfolio_state(portfolio)); s = dumps(asdict(state))
        # Use one transaction so every database change rolls back if any step fails.
        with self.db:
            self.db.execute('INSERT INTO processed_events VALUES(?,?)', (event_key,time.time()))

            # Advance the checkpoint only when its current revision matches the expected revision.
            cursor=self.db.execute('UPDATE checkpoint SET portfolio_json=?,engine_json=?,revision=revision+1,updated_at=? WHERE id=1 AND revision=?',
                                  (p,s,time.time(),expected_revision))
            
            # Reject a stale or concurrent writer when the checkpoint update affects no matching row.
            if cursor.rowcount != 1:
                raise RuntimeError('Stale/concurrent writer rejected.')
            
            mode = self.db.execute('SELECT mode FROM checkpoint WHERE id=1').fetchone()[0]

            # Prevent backtest events from queuing Telegram messages.
            if notify and mode != 'paper':
                raise ValueError('Backtests must not queue Telegram notifications.') 
            
            # Store each fill with its order ID, prices, fees, and matching accounting record.
            for fill in result.fills:
                record = records[fill.trade_id]
                self.db.execute('INSERT INTO trades VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (fill.trade_id,fill.order_id,
                     str(fill.timestamp),
                     fill.strategy,
                     fill.side,
                     fill.reason,
                     fill.btc_amount,
                     fill.fill_price,
                     fill.fee_usd,
                     record.get('realized_pnl'),
                     dumps(asdict(fill)),
                     dumps(record)))
                
                # Create a paper-trade notification only when notification queuing is enabled.
                if notify:
                    message = (f'PAPER | {fill.strategy} {fill.side}\nTime: {fill.timestamp}\n'
                             f'BTC: {fill.btc_amount:.8f} at ${fill.fill_price:,.2f}\nFee: ${fill.fee_usd:.2f}\n'
                             f'Reason: {fill.reason}\nTrade ID: {fill.trade_id}')
                    
                    # Append realized profit or loss only when the accounting record contains it.
                    if 'realized_pnl' in record:
                        message += f"\nP/L: ${record['realized_pnl']:.2f}"

                    # Queue the notification in the same transaction as the recorded fill.
                    self.db.execute('INSERT INTO outbox(trade_id,message) VALUES(?,?)',
                                    (fill.trade_id,message))
            snapshot = result.portfolio_snapshot

            # Store a valuation snapshot when the engine result includes one.
            if snapshot:
                self.db.execute('INSERT INTO snapshots VALUES(?,?,?,?,?,?,?)',
                    (event_key,
                     str(snapshot['timestamp']),
                     snapshot['portfolio_value'],
                     snapshot['cash'],
                     snapshot['dca_btc'],
                     snapshot['active_btc'],
                     dumps(snapshot)))
            # Visit each audit category, including extra runtime events supplied by the caller.
            for kind,events in [('stop',result.stop_events),
                                ('risk',result.risk_events),
                                ('volatility',result.volatility_events),
                                ('runtime',extra_events)]:
                # Store every event in the current category under the committed event key.
                for event in events:
                    self.db.execute('INSERT INTO events(event_key,kind,payload_json) VALUES(?,?,?)',
                                    (event_key, kind,dumps(event)))
        # Return the advanced revision after the transaction succeeds.
        return expected_revision + 1

    # Save the exact historical data used for a forecast under its candle timestamp.
    def save_forecast_input(self,timestamp,history):

        # Commit the forecast input CSV as a single database transaction.
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO forecast_inputs VALUES(?,?)',
                            (str(timestamp),history.to_csv()))

    # Close the account database connection.
    def close(self):self.db.close()
