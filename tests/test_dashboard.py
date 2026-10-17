import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zoneinfo import ZoneInfo

from quant_lab.dashboard.data import local_status, tail_events


class DashboardTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parents[1] / "state" / "test-scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = TemporaryDirectory(dir=scratch)
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_partial_log_tail(self):
        p = self.root / "log.jsonl"
        p.write_text('{"event":"CYCLE","status":"WAIT_OPEN"}\n{"partial":', encoding="utf-8")
        self.assertEqual(len(tail_events(p)), 1)
        self.assertEqual(tail_events(self.root / "absent"), [])

    @patch("quant_lab.dashboard.data.process_alive", return_value=True)
    def test_heartbeat_staleness(self, alive):
        now = datetime.now(ZoneInfo("UTC"))
        (self.root / "logs").mkdir()
        log = self.root / "logs" / "paper_execution.jsonl"
        log.write_text(
            json.dumps(dict(event="CYCLE", recorded_at=now.isoformat(), status="WAIT_OPEN")) + "\n"
        )
        self.assertTrue(local_status(self.root, now)["healthy"])
        self.assertFalse(local_status(self.root, now + timedelta(seconds=120))["healthy"])

    def test_native_window_and_chart(self):
        try:
            import tkinter as tk

            from quant_lab.dashboard.native import Dashboard
        except ImportError:
            self.skipTest("Optional Tk native window is absent from the web container")
        window = tk.Tk()
        try:
            dashboard = Dashboard(window, root=self.root, offline=True)
            now = datetime.now(ZoneInfo("UTC"))
            start = datetime(2026, 10, 16, 9, 30, tzinfo=ZoneInfo("America/New_York"))
            bars = [
                dict(
                    time=(start + timedelta(minutes=5 * i)).isoformat(),
                    open=100 + i * 0.1,
                    high=101 + i * 0.1,
                    low=99 + i * 0.1,
                    close=100.5 + i * 0.1,
                )
                for i in range(20)
            ]
            dashboard.render(
                dict(
                    equity=100000,
                    cash=100000,
                    baseline=100000,
                    pnl=0,
                    daily_pct=0,
                    qty=0,
                    position_value=0,
                    position_pnl=0,
                    fetched=now.isoformat(),
                    bars=bars,
                    chart_date="2026-10-16",
                    rows=[],
                )
            )
            window.update()
            dashboard.draw_chart()
            self.assertGreater(len(dashboard.chart.find_all()), 40)
            self.assertEqual(dashboard.metrics["equity"][0].cget("text"), "$100,000.00")
            self.assertEqual(len(dashboard.table.get_children()), 1)
            self.assertGreater(dashboard.chart.winfo_width(), 200)
            self.assertGreater(dashboard.chart.winfo_height(), 100)
            self.assertLessEqual(
                dashboard.stop_button.winfo_rooty() + dashboard.stop_button.winfo_height(),
                window.winfo_rooty() + window.winfo_height(),
            )
            self.assertFalse((self.root / "STOP_PAPER").exists())
        finally:
            window.destroy()
