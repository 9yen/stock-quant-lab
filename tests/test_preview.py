from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pandas as pd

from quant_lab.brokers.alpaca.preview import collect


class PreviewTests(TestCase):
    @patch("yfinance.download")
    @patch("alpaca.trading.client.TradingClient")
    @patch.dict("os.environ", {"ALPACA_KEY": "test", "ALPACA_SECRET": "test"})
    def test_weekend_early_close_and_symbol_filter(self, factory, download):
        client = factory.return_value
        client.get_clock.return_value = NS(
            timestamp=datetime(2026, 11, 28, 12, tzinfo=ZoneInfo("America/New_York"))
        )
        client.get_calendar.return_value = [
            NS(date=date(2026, 11, 27), close=datetime(2026, 11, 27, 13))
        ]
        client.get_account.return_value = NS(
            equity="100000",
            cash="100000",
            trading_blocked=False,
            account_blocked=False,
            status="ACTIVE",
        )
        client.get_all_positions.return_value = []
        client.get_orders.return_value = [NS(symbol="SPY")]
        download.return_value = pd.DataFrame(
            {"Close": range(1, 221)}, index=pd.bdate_range(end="2026-11-27", periods=220)
        )
        result = collect(Path("missing-test-env"))
        self.assertEqual(result["decision"]["action"], "WAIT")
        self.assertEqual(result["market"]["data_date"], "2026-11-27")
        self.assertTrue(factory.call_args.kwargs["paper"])
        request = client.get_orders.call_args.kwargs["filter"]
        self.assertEqual(request.symbols, ["SPY"])
        client.submit_order.assert_not_called()
        client.cancel_orders.assert_not_called()

    @patch("alpaca.trading.client.TradingClient")
    @patch.dict("os.environ", {"ALPACA_KEY": "test", "ALPACA_SECRET": "test"})
    def test_calendar_failure_stops(self, factory):
        factory.return_value.get_clock.return_value = NS(
            timestamp=datetime(2026, 10, 16, 12, tzinfo=ZoneInfo("UTC"))
        )
        factory.return_value.get_calendar.return_value = []
        with self.assertRaises(ValueError):
            collect(Path("missing-test-env"))
        factory.return_value.get_orders.assert_not_called()
