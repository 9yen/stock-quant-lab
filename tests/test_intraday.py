from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zoneinfo import ZoneInfo
from quant_lab.intraday import evaluate, daily_state, save_state, timeout_request


class IntradayTests(unittest.TestCase):
    def setUp(self):
        self.open = datetime(2026, 10, 16, 9, 30, tzinfo=ZoneInfo('America/New_York'))
        self.close = self.open.replace(hour=16, minute=0)
        self.now = self.open + timedelta(minutes=20, seconds=30)
        self.account = dict(equity=100000, cash=100000, qty=0, value=0, entry=0, blocked=False)
        self.state = dict(baseline=100000, halted=False, entry_candidates=0, last_entry_at=None)
        self.bars = [dict(time=self.open+timedelta(minutes=i*5), open=100, high=101, low=99, close=100) for i in range(4)]
        self.bars[-1].update(high=103, close=102)

    def run_decision(self, orders=None):
        return evaluate(self.now, self.open, self.close, self.account, orders or [], self.bars, self.state)

    def test_breakout_and_budget(self):
        r = self.run_decision()
        self.assertEqual(r['action'], 'BUY CANDIDATE')
        self.assertLessEqual(r['candidate_qty']*102*1.02, 5000)

    def test_pending_blocks_entry(self):
        self.assertEqual(self.run_decision([object()])['action'], 'WAIT')

    def test_incomplete_candle_excluded(self):
        self.now = self.open+timedelta(minutes=19)
        self.assertEqual(self.run_decision()['action'], 'WAIT')

    def test_stale_data(self):
        self.now += timedelta(minutes=10)
        self.assertEqual(self.run_decision()['action'], 'STOP')

    def test_missing_opening_bar(self):
        self.bars.pop(1)
        self.assertEqual(self.run_decision()['action'], 'STOP')

    def test_latched_daily_loss_survives_recovery(self):
        self.account['equity'] = 79000
        self.assertEqual(self.run_decision()['action'], 'HALT')
        self.account['equity'] = 100000
        self.assertEqual(self.run_decision()['action'], 'HALT')

    def test_exit_before_pending_and_data_checks(self):
        self.account.update(qty=5, value=500, entry=100)
        self.account['equity'] = 79000
        self.bars = []
        self.assertEqual(self.run_decision([object()])['action'], 'CANCEL THEN EXIT CANDIDATE')

    def test_preclose_exit_early_close(self):
        self.close = self.open.replace(hour=13, minute=0)
        self.now = self.close-timedelta(minutes=10)
        self.account.update(qty=5, value=500, entry=100)
        self.assertEqual(self.run_decision()['action'], 'EXIT CANDIDATE')

    def test_stop_and_profit(self):
        self.account.update(qty=5, value=500, entry=100)
        self.assertEqual(self.run_decision()['action'], 'EXIT CANDIDATE')
        self.bars[-1].update(close=98, low=97)
        self.assertEqual(self.run_decision()['action'], 'EXIT CANDIDATE')

    def test_no_repeated_above_range_entry(self):
        self.bars.append(dict(time=self.open+timedelta(minutes=20), open=102, high=104, low=101, close=103))
        self.now += timedelta(minutes=5)
        self.assertEqual(self.run_decision()['action'], 'WAIT')

    def test_candidate_cap_and_cooldown(self):
        self.state['entry_candidates'] = 3
        self.assertEqual(self.run_decision()['action'], 'WAIT')
        self.state.update(entry_candidates=1, last_entry_at=(self.now-timedelta(minutes=5)).isoformat())
        self.assertEqual(self.run_decision()['action'], 'WAIT')

    def test_blocked(self):
        self.account['blocked'] = True
        self.assertEqual(self.run_decision()['action'], 'STOP')

    def test_restart_and_rollover(self):
        scratch = Path(__file__).resolve().parents[1] / 'state' / 'test-scratch'
        scratch.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=scratch) as d:
            p = Path(d)/'state.json'
            s = daily_state(p, self.open.date(), 100000, 'test')
            s['halted'] = True
            save_state(p, s)
            self.assertTrue(daily_state(p, self.open.date(), 80000, 'test')['halted'])
            self.assertEqual(daily_state(p, self.open.date(), 80000, 'test')['baseline'], 100000)
            self.assertFalse(daily_state(p, (self.open+timedelta(days=1)).date(), 80000, 'test')['halted'])
            with self.assertRaises(ValueError):
                daily_state(p, self.open.date(), 100000, 'different-account')
            p.write_text('broken')
            with self.assertRaises(ValueError):
                daily_state(p, self.open.date(), 100000, 'test')

    def test_timeout(self):
        seen = {}
        timeout_request(lambda **kw: seen.update(kw))(timeout=None)
        self.assertEqual(seen['timeout'], 15)


if __name__ == '__main__':
    unittest.main()
