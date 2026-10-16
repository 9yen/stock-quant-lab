from datetime import datetime,timedelta
import unittest
from zoneinfo import ZoneInfo
from quant_lab.scanner import compare


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.open=datetime(2026,10,16,9,30,tzinfo=ZoneInfo('America/New_York'))
        self.bars=[dict(time=self.open+timedelta(minutes=i),open=100,high=101,low=99,close=100) for i in range(20)]
        self.now=self.open+timedelta(minutes=20,seconds=31)

    def scan(self):return compare('TEST',self.bars,self.now,self.open)

    def test_minute_breakout_before_five(self):
        self.now=self.open+timedelta(minutes=16,seconds=31)
        self.bars[15].update(high=103,close=102)
        r=self.scan()
        self.assertEqual(r['minute_signal'],'LONG')
        self.assertEqual(r['five_signal'],'WAIT')

    def test_five_and_minute_short(self):
        self.bars[19].update(low=97,close=98)
        r=self.scan()
        self.assertEqual(r['minute_signal'],'SHORT')
        self.assertEqual(r['five_signal'],'SHORT')

    def test_uncompleted_and_late_update_buffer(self):
        self.now=self.open+timedelta(minutes=16,seconds=5)
        self.bars[15].update(high=103,close=102)
        self.assertEqual(self.scan()['minute_signal'],'WAIT')

    def test_opening_gap_not_filled(self):
        self.bars.pop(5)
        self.assertEqual(self.scan()['minute_signal'],'INCOMPLETE')
        self.assertEqual(self.scan()['opening_minutes'],14)

    def test_stale_and_duplicate(self):
        self.now+=timedelta(minutes=5)
        self.assertEqual(self.scan()['minute_signal'],'STALE')
        self.now-=timedelta(minutes=5)
        self.bars.append(self.bars[-1])
        self.assertEqual(self.scan()['minute_signal'],'INVALID')

    def test_invalid_close(self):
        self.bars[3]['close']=float('nan')
        self.assertEqual(self.scan()['minute_signal'],'INVALID')

    def test_missing_signal_predecessor(self):
        self.bars.pop(18)
        self.assertEqual(self.scan()['minute_signal'],'INCOMPLETE')
