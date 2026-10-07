import copy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
import pandas as pd
from trading.models import EngineState, OrderIntent
from trading.execution import ExecutionModel
from trading.engine import CandleResult, process_quote, process_close
from services.storage import SQLiteStore,new_portfolio
from paper_loop import monitor_quote
from services.telegram_notify import deliver_one,DeliveryError
ROOT=Path(__file__).resolve().parents[1]

class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'account.sqlite'
        self.config=json.loads((ROOT/'config.json').read_text());self.store=SQLiteStore(self.path)
        self.portfolio=new_portfolio(self.config);self.state=EngineState()
        self.store.initialize(self.config,self.portfolio,self.state,'paper')
        self.execution=ExecutionModel(**self.config['execution'])
    def tearDown(self):self.store.close();self.tmp.cleanup()
    def queue(self):
        self.state.pending_dca=OrderIntent('dca-order','DCA','BUY','test',pd.Timestamp('2025-01-01T01:00Z'),amount_usd=500)
        self.state.pending_swing_buy=OrderIntent('swing-order','SWING','BUY','test',pd.Timestamp('2025-01-01T01:00Z'),amount_usd=1000,atr=2,atr_multiplier=2)
    def buy(self):
        self.queue();return process_quote(pd.Timestamp('2025-01-01T01:00:10Z'),100,100,self.portfolio,self.state,self.execution)
    def test_restore_holdings_stops_and_ids(self):
        result=self.buy();self.store.commit('quote1',self.portfolio,self.state,result,0,True)
        self.store.close();self.store=SQLiteStore(self.path);p,s,r=self.store.restore()
        self.assertEqual(p.active_btc,self.portfolio.active_btc);self.assertEqual(s.active_position,self.state.active_position)
        self.assertIsNone(s.pending_swing_buy);self.assertEqual(len(p.trades),2)
        self.assertFalse(p.apply_fill(result.fills[0]))
        self.assertFalse(p.apply_fill(replace(result.fills[0],trade_id='different-id-same-order')))
    def test_restore_pending_before_fill(self):
        self.queue();self.store.commit('signal',self.portfolio,self.state,CandleResult(),0)
        p,s,r=self.store.restore();result=process_quote(pd.Timestamp('2025-01-01T01:00:10Z'),100,100,p,s,self.execution)
        self.assertEqual(len(result.fills),2);self.store.commit('fill',p,s,result,r)
    def test_duplicate_event_rolls_back(self):
        result=self.buy();self.store.commit('q',self.portfolio,self.state,result,0);before=self.store.restore()[0].cash
        self.portfolio.dca_cash=1
        with self.assertRaises(Exception):self.store.commit('q',self.portfolio,self.state,CandleResult(),1)
        self.assertEqual(self.store.restore()[0].cash,before)
    def test_fill_conflict_rolls_back_checkpoint(self):
        result=self.buy();self.store.commit('q',self.portfolio,self.state,result,0);self.portfolio.dca_cash=1
        with self.assertRaises(Exception):self.store.commit('q2',self.portfolio,self.state,result,1)
        p,s,r=self.store.restore();self.assertEqual(p.dca_cash,5500);self.assertEqual(r,1);self.assertFalse(self.store.contains('q2'))
    def test_uncommitted_action_not_restored(self):
        self.buy();p,s,r=self.store.restore();self.assertEqual(p.cash,10000);self.assertEqual(p.active_btc,0)
    def test_stale_writer_rejected(self):
        result=self.buy();self.store.commit('q',self.portfolio,self.state,result,0)
        with self.assertRaises(RuntimeError):self.store.commit('q2',self.portfolio,self.state,CandleResult(),0)
    def test_expired_buy_does_not_execute(self):
        self.queue();result=process_quote(pd.Timestamp('2025-01-01T03:00Z'),100,100,self.portfolio,self.state,self.execution)
        self.assertEqual(result.fills,[])
    def test_outage_uses_current_quote_for_stop(self):
        self.buy();result,gap=monitor_quote(pd.Timestamp('2025-01-01T02:00Z'),90,91,self.portfolio,self.state,self.execution)
        self.assertTrue(gap);self.assertTrue(self.state.execution_path_uncertain);self.assertEqual(result.fills[-1].reference_price,90)
    def test_invalid_fill_and_partial_sale_rejected(self):
        result=self.buy()
        with self.assertRaises(ValueError):self.portfolio.apply_fill(replace(result.fills[0],trade_id='bad',order_id='bad',btc_amount=-1))
        order=OrderIntent('sale','SWING','SELL','test',pd.Timestamp('2025-01-01T02:00Z'))
        fill=self.execution.fill_sell(order,100,order.signal_time,self.portfolio.active_btc/2)
        with self.assertRaises(ValueError):self.portfolio.apply_fill(fill)
    def test_settings_change_rejected(self):
        c=copy.deepcopy(self.config);c['swing']['crossover']='bull'
        with self.assertRaises(ValueError):self.store.initialize(c,self.portfolio,self.state,'paper')
    def test_backtest_cannot_send(self):
        self.store.close();self.store=SQLiteStore(Path(self.tmp.name)/'backtest.sqlite')
        self.store.initialize(self.config,self.portfolio,self.state,'backtest');result=self.buy()
        with self.assertRaises(ValueError):self.store.commit('q',self.portfolio,self.state,result,0,True)
        self.assertEqual(self.store.restore()[2],0)
    def test_telegram_failure_does_not_change_account(self):
        result=self.buy();self.store.commit('q',self.portfolio,self.state,result,0,True)
        class Broken:
            def send(self,text):raise DeliveryError('network failure')
        deliver_one(self.store,Broken(),100);self.assertEqual(self.store.restore()[0].cash,8500)
        class Good:
            def send(self,text):return '123'
        deliver_one(self.store,Good(),200);deliver_one(self.store,Good(),200)
        self.assertFalse(deliver_one(self.store,Good(),200))
    def test_duplicate_close_is_noop(self):
        self.state.last_processed_candle=pd.Timestamp('2025-01-01T01:00Z')
        result=process_close(self.state.last_processed_candle,None,None,0,self.portfolio,self.state,self.config)
        self.assertEqual(result.fills,[])

if __name__=='__main__':unittest.main()
