import unittest
from datetime import date,datetime
from zoneinfo import ZoneInfo
from moomoo_options import valid_contract,premium_limit,option_exit
from moomoo_performance import review
class OptionTests(unittest.TestCase):
    def test_standard_contract_only_not_zero_dte(self):
        row=dict(stock_owner='US.AAPL',option_type='CALL',option_standard_type='STANDARD',lot_size=100,strike_time='2026-10-30',suspension=False)
        self.assertTrue(valid_contract(row,'US.AAPL','LONG',date(2026,10,19)))
        self.assertFalse(valid_contract(dict(row,strike_time='2026-10-19'),'US.AAPL','LONG',date(2026,10,19)))
        self.assertFalse(valid_contract(dict(row,lot_size=10),'US.AAPL','LONG',date(2026,10,19)))
        self.assertFalse(valid_contract(row,'US.AAPL','SHORT',date(2026,10,19)))
    def test_premium_includes_100_multiplier_and_freshness(self):
        now=datetime(2026,10,19,10,tzinfo=ZoneInfo('America/New_York'))
        row=dict(bid_price=4.9,ask_price=5,update_time='2026-10-19 10:00:00',option_contract_size=100,option_open_interest=500,volume=100)
        self.assertEqual(premium_limit(row,1000000,1000000,now),5.05)
        self.assertIsNone(premium_limit(row,100000,100000,now))
        self.assertIsNone(premium_limit(dict(row,ask_price=8),1000000,1000000,now))
        self.assertIsNone(premium_limit(dict(row,update_time='2026-10-19 09:50:00'),1000000,1000000,now))
    def test_option_exit_is_long_only_policy(self):
        self.assertEqual(option_exit(4,3,5),'OPTION_STOP_25')
        self.assertEqual(option_exit(4,5.6,5),'OPTION_TAKE_40')
        self.assertEqual(option_exit(4,4.1,30),'OPTION_TIME_30M')
    def test_fifo_option_profit_uses_contract_size(self):
        fills=[dict(id='1',symbol='US.AAPL261030C340000',side='BUY',kind='ENTRY',qty=1,price=4,time='2026-10-19T10:00:00-04:00',instrument='OPTION',multiplier=100),dict(id='2',symbol='US.AAPL261030C340000',side='SELL',kind='EXIT',qty=1,price=5,time='2026-10-19T10:15:00-04:00',instrument='OPTION',multiplier=100)]
        result=review(fills+[fills[0]])
        self.assertEqual(result['gross_realized'],100)
        self.assertEqual(result['closed'][0]['return_pct'],25)
    def test_fifo_short_partial_and_day_archive(self):
        base=dict(symbol='US.NVDA',instrument='STOCK',multiplier=1)
        fills=[dict(base,id='1',side='SELL',kind='ENTRY',qty=10,price=100,time='2026-10-19T10:00:00-04:00'),dict(base,id='2',side='BUY',kind='EXIT',qty=4,price=95,time='2026-10-19T10:10:00-04:00')]
        result=review(fills)
        self.assertEqual(result['gross_realized'],20)
        self.assertEqual(result['days'][0]['day'],'2026-10-19')
        self.assertEqual(result['closed'][0]['return_pct'],5)
