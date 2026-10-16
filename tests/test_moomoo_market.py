import unittest
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from moomoo_market import exit_decision,analyze
class MoomooRiskTests(unittest.TestCase):
    def test_long_stop_and_take(self):
        self.assertEqual(exit_decision('LONG',100,99,True,True),'STOP')
        self.assertEqual(exit_decision('LONG',100,102,True,True),'TAKE_PROFIT')
    def test_short_stop_and_take(self):
        self.assertEqual(exit_decision('SHORT',100,101,True,True),'STOP')
        self.assertEqual(exit_decision('SHORT',100,98,True,True),'TAKE_PROFIT')
    def test_no_execution_on_stale_or_disconnect(self):
        self.assertEqual(exit_decision('LONG',100,90,False,True),'DATA_HOLD')
        self.assertEqual(exit_decision('SHORT',100,90,True,False),'DATA_HOLD')
    def test_after_hours_never_entry(self):
        now=datetime(2026,10,16,17,10,tzinfo=ZoneInfo('America/New_York'))
        row=analyze('US.AAPL',[],'AFTER_HOURS_BEGIN',now)
        self.assertEqual(row['signal'],'WAIT')
    def test_missing_opening_never_entry(self):
        now=datetime(2026,10,16,10,10,tzinfo=ZoneInfo('America/New_York'))
        row=analyze('US.AAPL',[],'MORNING',now)
        self.assertEqual(row['signal'],'WAIT')

    def test_native_0931_opening_window_has_15_real_bars(self):
        now=datetime(2026,10,19,10,10,tzinfo=ZoneInfo('America/New_York'))
        start=now.replace(hour=9,minute=31)
        rows=[dict(time_key=(start+timedelta(minutes=i)).strftime('%Y-%m-%d %H:%M:%S'),open=100,high=101,low=99,close=100,volume=1000) for i in range(30)]
        row=analyze('US.AAPL',rows,'MORNING',now)
        self.assertEqual(row['opening_minutes'],15)
