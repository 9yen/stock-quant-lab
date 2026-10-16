from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo
from quant_lab.intraday import evaluate
from quant_lab.paper_execution import entry_request, submit_once, flatten, require_shortable, cycle


class ShortTests(unittest.TestCase):
    def setUp(self):
        scratch=Path(__file__).resolve().parents[1]/'state'/'test-scratch'
        scratch.mkdir(parents=True,exist_ok=True)
        self.tmp=TemporaryDirectory(dir=scratch)
        self.root=Path(self.tmp.name)
        self.open=datetime(2026,10,16,9,30,tzinfo=ZoneInfo('America/New_York'))
        self.now=self.open+timedelta(minutes=20,seconds=30)
        self.close=self.open.replace(hour=16,minute=0)
        self.a=dict(equity=100000,cash=100000,qty=0,value=0,entry=0,blocked=False)
        self.state=dict(baseline=100000,halted=False,entry_candidates=0,last_entry_at=None)
        self.bars=[dict(time=self.open+timedelta(minutes=i*5),open=100,high=101,low=99,close=100) for i in range(4)]
        self.bars[-1].update(close=98,low=97)
        self.client=MagicMock()
        self.client._base_url='https://paper-api.alpaca.markets'
        self.client.get_clock.return_value=NS(is_open=True,timestamp=self.now)
        self.account=NS(id='paper',equity='100000',cash='100000',buying_power='400000',last_equity='100000',shorting_enabled=True,
                        status='ACTIVE',trading_blocked=False,account_blocked=False)
        self.client.get_account.return_value=self.account
        self.asset=NS(tradable=True,shortable=True,easy_to_borrow=True,marginable=True,status='active')
        self.client.get_asset.return_value=self.asset
        self.client.get_all_positions.return_value=[]
        self.client.get_orders.return_value=[]
        self.client.submit_order.return_value=NS(id='order',status='accepted')
        self.client.get_calendar.return_value=[NS(open=self.open,close=self.close)]
        self.j=dict(account_id='paper',session='2026-10-16',entry_disabled=False,orders={},allow_short=True)
        self.jp=self.root/'state'/'journal.json'
        self.report=dict(action='SHORT CANDIDATE',reference_price=98,range_low=99,range_high=101,candidate_qty=48,signal_bar=self.open.replace(minute=45).isoformat())
        self.quote=NS(timestamp=self.now,bid_price=98,ask_price=98.02)

    def tearDown(self): self.tmp.cleanup()

    def decision(self,allow=True):
        return evaluate(self.now,self.open,self.close,self.a,[],self.bars,self.state,allow_short=allow)

    def request(self):return entry_request(self.report,self.account,self.quote,self.now,'2026-10-16')

    def test_lower_breakout_requires_enabled_mode(self):
        self.assertEqual(self.decision()['action'],'SHORT CANDIDATE')
        self.assertEqual(self.decision(False)['action'],'WAIT')

    def test_short_stop_take_profit_and_range_failure(self):
        self.a.update(qty=-3,value=-300,entry=100)
        self.assertEqual(self.decision()['action'],'EXIT CANDIDATE') # 2% profit
        self.bars[-1].update(close=102,high=103)
        self.assertEqual(self.decision()['action'],'EXIT CANDIDATE') # stop/range
        self.bars[-1].update(close=100,high=101)
        self.assertEqual(self.decision()['action'],'HOLD')

    def test_short_daily_and_preclose_exit_quantities_positive(self):
        self.a.update(qty=-6,value=-600,entry=100,equity=79000)
        self.assertEqual(self.decision()['candidate_qty'],6)

    def test_signed_inconsistency_and_exposure(self):
        self.a.update(qty=-6,value=600,entry=100)
        self.assertEqual(self.decision()['action'],'REVIEW')
        self.a.update(qty=-60,value=-6000,entry=100)
        self.bars[-1].update(close=100,high=101)
        self.assertEqual(self.decision()['action'],'REVIEW')

    def test_short_sell_bracket_prices_and_gross_cap(self):
        r=self.request()
        self.assertEqual(r.side.value,'sell')
        self.assertIn('-S-',r.client_order_id)
        self.assertGreater(r.stop_loss.stop_price,r.limit_price)
        self.assertLess(r.take_profit.limit_price,r.limit_price)
        self.assertLessEqual(r.qty*max(r.limit_price,self.quote.ask_price)*1.05,5000)

    def test_permission_and_borrow_each_submission(self):
        for flag in ('shortable','easy_to_borrow','marginable','tradable'):
            setattr(self.asset,flag,False)
            with self.assertRaises(ValueError):
                submit_once(self.client,self.request(),self.j,self.jp,self.root)
            setattr(self.asset,flag,True)
        self.account.shorting_enabled=False
        with self.assertRaises(ValueError):require_shortable(self.client,self.account)
        self.client.submit_order.assert_not_called()

    def test_short_not_authorized(self):
        self.j['allow_short']=False
        with self.assertRaises(ValueError):submit_once(self.client,self.request(),self.j,self.jp,self.root)
        self.client.submit_order.assert_not_called()

    def test_short_entry_from_flat_and_no_add(self):
        submit_once(self.client,self.request(),self.j,self.jp,self.root)
        self.client.submit_order.assert_called_once()
        self.j['orders']={}
        self.client.get_all_positions.return_value=[NS(symbol='SPY',qty='-1')]
        with self.assertRaises(ValueError):submit_once(self.client,self.request(),self.j,self.jp,self.root)
        self.assertEqual(self.client.submit_order.call_count,1)

    def test_buy_to_cover_partial_short(self):
        self.client.get_all_positions.return_value=[NS(symbol='SPY',qty='-1.5')]
        flatten(self.client,self.j,self.jp,self.root,'2026-10-16')
        r=self.client.submit_order.call_args.kwargs['order_data']
        self.assertEqual(r.side.value,'buy')
        self.assertEqual(r.qty,1.5)

    def test_wrong_cover_side_rejected(self):
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide,TimeInForce
        self.client.get_all_positions.return_value=[NS(symbol='SPY',qty='-2')]
        r=MarketOrderRequest(symbol='SPY',qty=2,side=OrderSide.SELL,time_in_force=TimeInForce.DAY,client_order_id='sqlab-20261016-X-001')
        with self.assertRaises(ValueError):submit_once(self.client,r,self.j,self.jp,self.root)
        self.client.submit_order.assert_not_called()

    def test_short_cycle_and_combined_entry_cap(self):
        data=MagicMock()
        data.get_stock_latest_quote.return_value={'SPY':self.quote}
        auth=dict(account_id='paper',session='2026-10-16',allow_short=True)
        with patch('quant_lab.paper_execution.tick',return_value=self.report):
            self.assertEqual(cycle(self.client,data,self.root,auth,self.j,self.jp)['status'],'ENTRY_ACCEPTED')
        self.j['orders']={f'sqlab-20261016-{side}-{i}':{} for i,side in enumerate(['B','S','B'])}
        with patch('quant_lab.paper_execution.tick',return_value=self.report):
            self.assertEqual(cycle(self.client,data,self.root,auth,self.j,self.jp)['status'],'ENTRY_DISABLED')

    def test_no_cover_permission_requirement(self):
        self.account.shorting_enabled=False
        self.asset.shortable=False
        self.j['allow_short']=False
        self.client.get_all_positions.return_value=[NS(symbol='SPY',qty='-2')]
        self.assertEqual(flatten(self.client,self.j,self.jp,self.root,'2026-10-16'),'EXIT_ACCEPTED')
