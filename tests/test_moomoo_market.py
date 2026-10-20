import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pandas as pd

from quant_lab.brokers.moomoo.market import MarketStates, analyze, exit_decision


class MoomooRiskTests(unittest.TestCase):
    def test_shared_market_states_use_one_request_across_workers(self):
        clock = MagicMock(return_value=0)
        ctx = MagicMock()
        ctx.get_market_state.return_value = (
            0,
            pd.DataFrame([dict(code="US.AAPL", market_state="AFTERNOON")]),
        )
        cache = MarketStates(["US.AAPL", "US.MSFT"], clock)
        for _ in range(4):
            self.assertEqual(cache.get(ctx)["US.AAPL"], "AFTERNOON")
        ctx.get_market_state.assert_called_once_with(["US.AAPL", "US.MSFT"])
        clock.return_value = 21
        cache.get(ctx)
        self.assertEqual(ctx.get_market_state.call_count, 2)

    def test_market_state_failure_does_not_reuse_old_regular_status(self):
        clock = MagicMock(return_value=0)
        ctx = MagicMock()
        ctx.get_market_state.return_value = (
            0,
            pd.DataFrame([dict(code="US.AAPL", market_state="AFTERNOON")]),
        )
        cache = MarketStates(["US.AAPL"], clock)
        cache.get(ctx)
        clock.return_value = 21
        ctx.get_market_state.return_value = (-1, "unavailable")
        self.assertEqual(cache.get(ctx), {})
        self.assertEqual(cache.get(ctx), {})
        self.assertEqual(ctx.get_market_state.call_count, 2)

    def test_busy_expired_cache_is_nonblocking_and_fail_closed(self):
        clock = MagicMock(return_value=40)
        cache = MarketStates(["US.AAPL"], clock)
        cache.updated = 0
        cache.states = {"US.AAPL": "AFTERNOON"}
        cache.lock.acquire()
        try:
            self.assertEqual(cache.get(MagicMock()), {})
        finally:
            cache.lock.release()

    def test_unknown_market_is_unavailable_not_confirmed_closed(self):
        now = datetime(2026, 10, 20, 10, tzinfo=ZoneInfo("America/New_York"))
        row = analyze("US.AAPL", [], "UNKNOWN", now)
        self.assertEqual(row["signal"], "WAIT")
        self.assertIn("暂不可用", row["reason"])

    def test_long_stop_and_take(self):
        self.assertEqual(exit_decision("LONG", 100, 99, True, True), "STOP")
        self.assertEqual(exit_decision("LONG", 100, 102, True, True), "TAKE_PROFIT")

    def test_short_stop_and_take(self):
        self.assertEqual(exit_decision("SHORT", 100, 101, True, True), "STOP")
        self.assertEqual(exit_decision("SHORT", 100, 98, True, True), "TAKE_PROFIT")

    def test_no_execution_on_stale_or_disconnect(self):
        self.assertEqual(exit_decision("LONG", 100, 90, False, True), "DATA_HOLD")
        self.assertEqual(exit_decision("SHORT", 100, 90, True, False), "DATA_HOLD")

    def test_after_hours_never_entry(self):
        now = datetime(2026, 10, 16, 17, 10, tzinfo=ZoneInfo("America/New_York"))
        row = analyze("US.AAPL", [], "AFTER_HOURS_BEGIN", now)
        self.assertEqual(row["signal"], "WAIT")

    def test_missing_opening_never_entry(self):
        now = datetime(2026, 10, 16, 10, 10, tzinfo=ZoneInfo("America/New_York"))
        row = analyze("US.AAPL", [], "MORNING", now)
        self.assertEqual(row["signal"], "WAIT")

    def test_native_0931_opening_window_has_15_real_bars(self):
        now = datetime(2026, 10, 19, 10, 10, tzinfo=ZoneInfo("America/New_York"))
        start = now.replace(hour=9, minute=31)
        rows = [
            dict(
                time_key=(start + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S"),
                open=100,
                high=101,
                low=99,
                close=100,
                volume=1000,
            )
            for i in range(30)
        ]
        row = analyze("US.AAPL", rows, "MORNING", now)
        self.assertEqual(row["opening_minutes"], 15)
