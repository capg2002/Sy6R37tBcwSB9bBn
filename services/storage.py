"""Atomic SQLite persistence for Portfolio, EngineState, fills and audit events."""
from dataclasses import asdict
import json
import math
from pathlib import Path
import sqlite3
import time
import pandas as pd
from trading.models import EngineState, OrderIntent, ActivePosition
from trading.portfolio import Portfolio

PORTFOLIO_FIELDS = ('starting_cash','dca_cash','swing_cash','reserve_cash','dca_cost',
                    'active_cost','realized_pnl','dca_btc','active_btc','fee_rate','slippage_rate')


def json_value(value):
    """Convert dataclass payloads and pandas/numpy scalars to portable JSON."""
    if isinstance(value,dict):return {k:json_value(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [json_value(v) for v in value]
    if isinstance(value,pd.Timestamp):return value.isoformat()
    if hasattr(value,'item'):return json_value(value.item())
    if isinstance(value,float) and not math.isfinite(value):return None  # diagnostic warm-up values
    return value


def dumps(value):return json.dumps(json_value(value),sort_keys=True,allow_nan=False)


def new_portfolio(config):
    return Portfolio(config['starting_budget'],config['dca']['allocation'],
        config['swing']['allocation'],config['reserve'],**config['execution'])


def state_from_dict(data):
    data=dict(data)
    for key in ('pending_dca','pending_swing_buy','pending_swing_sell'):
        if data[key] is not None:
            order=dict(data[key]);order['signal_time']=pd.Timestamp(order['signal_time'])
            data[key]=OrderIntent(**order)
    if data['active_position'] is not None:
        data['active_position']=ActivePosition(**data['active_position'])
    for key in ('last_processed_candle','last_quote_time','last_exit_hour'):
        if data.get(key) is not None:data[key]=pd.Timestamp(data[key])
    return EngineState(**data)


class SQLiteStore:
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(self.path,timeout=10)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
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

    def initialize(self,config,portfolio,state,mode,context=None):
        if mode not in ('backtest','paper'):raise ValueError('Mode must be backtest or paper.')
        context={} if context is None else context
        with self.db:
            row=self.db.execute('SELECT * FROM checkpoint WHERE id=1').fetchone()
            if row:
                if row['mode']!=mode or row['config_json']!=dumps(config) or row['context_json']!=dumps(context):
                    raise ValueError('Existing database has different settings/mode/input. Use its original settings or an explicitly NEW database.')
            else:
                self.db.execute('INSERT INTO checkpoint VALUES(1,?,?,?,?,?,0,?)',
                    (mode,dumps(config),dumps(context),dumps(self.portfolio_state(portfolio)),dumps(asdict(state)),time.time()))

    @staticmethod
    def portfolio_state(portfolio):
        result={key:getattr(portfolio,key) for key in PORTFOLIO_FIELDS}
        if any(not math.isfinite(v) for v in result.values()):
            raise ValueError('Nonfinite portfolio values cannot be checkpointed.')
        return result

    def restore(self):
        row=self.db.execute('SELECT * FROM checkpoint WHERE id=1').fetchone()
        if row is None:raise ValueError('Database has not been initialized.')
        config=json.loads(row['config_json']);portfolio=new_portfolio(config)
        for key,value in json.loads(row['portfolio_json']).items():setattr(portfolio,key,value)
        # Restore audit history/ID guards without reapplying fills to balances.
        for trade in self.db.execute('SELECT trade_id,order_id,accounting_json FROM trades ORDER BY rowid'):
            record=json.loads(trade['accounting_json']);record['timestamp']=pd.Timestamp(record['timestamp'])
            portfolio.trades.append(record)
            portfolio.applied_trade_ids.add(trade['trade_id']);portfolio.applied_order_ids.add(trade['order_id'])
        state=state_from_dict(json.loads(row['engine_json']))
        if (state.active_position is None) != (portfolio.active_btc == 0):
            raise ValueError('Saved position/holdings disagree; refusing to run.')
        return portfolio,state,row['revision']

    def contains(self,event_key):
        return self.db.execute('SELECT 1 FROM processed_events WHERE event_key=?',(event_key,)).fetchone() is not None

    def commit(self,event_key,portfolio,state,result,expected_revision,notify=False,extra_events=()):
        """On failure stop/reload; do not continue using uncommitted in-memory objects."""
        records={trade.get('trade_id'):trade for trade in portfolio.trades[-len(result.fills):]} if result.fills else {}
        p=dumps(self.portfolio_state(portfolio));s=dumps(asdict(state))
        with self.db:
            self.db.execute('INSERT INTO processed_events VALUES(?,?)',(event_key,time.time()))
            cursor=self.db.execute('UPDATE checkpoint SET portfolio_json=?,engine_json=?,revision=revision+1,updated_at=? WHERE id=1 AND revision=?',
                                  (p,s,time.time(),expected_revision))
            if cursor.rowcount!=1:raise RuntimeError('Stale/concurrent writer rejected.')
            mode=self.db.execute('SELECT mode FROM checkpoint WHERE id=1').fetchone()[0]
            if notify and mode!='paper':raise ValueError('Backtests must not queue Telegram notifications.')
            for fill in result.fills:
                record=records[fill.trade_id]
                self.db.execute('INSERT INTO trades VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (fill.trade_id,fill.order_id,str(fill.timestamp),fill.strategy,fill.side,fill.reason,
                     fill.btc_amount,fill.fill_price,fill.fee_usd,record.get('realized_pnl'),dumps(asdict(fill)),dumps(record)))
                if notify:
                    message=(f'PAPER | {fill.strategy} {fill.side}\nTime: {fill.timestamp}\n'
                             f'BTC: {fill.btc_amount:.8f} at ${fill.fill_price:,.2f}\nFee: ${fill.fee_usd:.2f}\n'
                             f'Reason: {fill.reason}\nTrade ID: {fill.trade_id}')
                    if 'realized_pnl' in record:message+=f"\nP/L: ${record['realized_pnl']:.2f}"
                    self.db.execute('INSERT INTO outbox(trade_id,message) VALUES(?,?)',(fill.trade_id,message))
            snapshot=result.portfolio_snapshot
            if snapshot:
                self.db.execute('INSERT INTO snapshots VALUES(?,?,?,?,?,?,?)',
                    (event_key,str(snapshot['timestamp']),snapshot['portfolio_value'],snapshot['cash'],snapshot['dca_btc'],snapshot['active_btc'],dumps(snapshot)))
            for kind,events in [('stop',result.stop_events),('risk',result.risk_events),('volatility',result.volatility_events),('runtime',extra_events)]:
                for event in events:
                    self.db.execute('INSERT INTO events(event_key,kind,payload_json) VALUES(?,?,?)',(event_key,kind,dumps(event)))
        return expected_revision+1

    def save_forecast_input(self,timestamp,history):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO forecast_inputs VALUES(?,?)',(str(timestamp),history.to_csv()))

    def close(self):self.db.close()
