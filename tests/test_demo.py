from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from demo_dashboard import build_store, synthetic_chart


class DemoTests(unittest.TestCase):
    def test_synthetic_demo_never_starts_account_workers(self):
        scratch=Path(__file__).resolve().parents[1]/'state'/'test-scratch'
        scratch.mkdir(parents=True,exist_ok=True)
        with TemporaryDirectory(dir=scratch) as folder:
            with patch('quant_lab.web_dashboard.account_snapshot', side_effect=AssertionError('No broker reads')):
                store=build_store(Path(folder))
                data=store.state()
                self.assertEqual(data['mode'],'DEMO')
                self.assertEqual(data['account']['equity'],10000)
                self.assertFalse((Path(folder)/'.env').exists())
                self.assertFalse((Path(folder)/'state/automation_config.json').exists())

    def test_demo_bars_are_repeatable_and_identified_as_historical(self):
        a,b=synthetic_chart('AAPL'),synthetic_chart('AAPL')
        self.assertEqual(a['bars'],b['bars'])
        self.assertEqual(a['chart_date'],'2026-01-15')
        self.assertEqual(len(a['bars']),180)
        self.assertTrue(all(row['low']<=row['close']<=row['high'] for row in a['bars']))
