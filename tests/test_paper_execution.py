import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from quant_lab.brokers.alpaca.execution import (
    cancel_spy,
    cycle,
    entry_request,
    flatten,
    require_paper,
    submit_once,
)


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parents[1] / "state" / "test-scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = TemporaryDirectory(dir=scratch)
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 10, 16, 10, 0, tzinfo=ZoneInfo("America/New_York"))
        self.account = NS(
            id="paper-id",
            equity="100000",
            cash="100000",
            buying_power="100000",
            last_equity="100000",
            trading_blocked=False,
            account_blocked=False,
            status="ACTIVE",
        )
        self.client = MagicMock()
        self.client._base_url = "https://paper-api.alpaca.markets"
        self.client.get_account.return_value = self.account
        self.client.get_clock.return_value = NS(timestamp=self.now, is_open=True)
        self.client.get_all_positions.return_value = []
        self.client.get_orders.return_value = []
        self.client.get_calendar.return_value = [
            NS(open=self.now.replace(hour=9, minute=30), close=self.now.replace(hour=16))
        ]
        self.client.submit_order.return_value = NS(id="order-id", status="accepted")
        self.report = dict(
            range_high=100,
            reference_price=101,
            candidate_qty=48,
            signal_bar=self.now.replace(minute=0).isoformat(),
        )
        self.quote = NS(timestamp=self.now, bid_price=100.99, ask_price=101)
        self.journal = dict(
            account_id="paper-id", session="2026-10-16", entry_disabled=False, orders={}
        )
        self.jpath = self.root / "state" / "journal.json"
        self.auth = dict(account_id="paper-id", session="2026-10-16")

    def tearDown(self):
        self.temp.cleanup()

    def request(self):
        return entry_request(self.report, self.account, self.quote, self.now, "2026-10-16")

    def test_live_endpoint_rejected(self):
        self.client._base_url = "https://api.alpaca.markets"
        with self.assertRaises(ValueError):
            require_paper(self.client)
        with self.assertRaises(ValueError):
            submit_once(self.client, self.request(), self.journal, self.jpath, self.root)
        self.client.submit_order.assert_not_called()

    def test_entry_bracket_limit_and_cap(self):
        r = self.request()
        self.assertEqual(r.symbol, "SPY")
        self.assertEqual(r.order_class.value, "bracket")
        self.assertLessEqual(r.qty * r.limit_price * 1.02, 5000)
        self.assertLess(r.stop_loss.stop_price, r.limit_price)
        self.assertGreater(r.take_profit.limit_price, r.limit_price)
        self.account.cash = "10"
        with self.assertRaises(ValueError):
            self.request()

    def test_stale_invalid_spread_and_chase_quotes(self):
        for quote in [
            NS(timestamp=self.now - timedelta(seconds=61), bid_price=100.99, ask_price=101),
            NS(timestamp=self.now, bid_price=100, ask_price=102),
            NS(timestamp=self.now, bid_price=105, ask_price=105.01),
            NS(timestamp=self.now, bid_price=float("nan"), ask_price=101),
            NS(timestamp=self.now, bid_price=99.99, ask_price=100),
        ]:
            with self.assertRaises(ValueError):
                entry_request(self.report, self.account, quote, self.now, "2026-10-16")

    def test_idempotency(self):
        r = self.request()
        submit_once(self.client, r, self.journal, self.jpath, self.root)
        submit_once(self.client, r, self.journal, self.jpath, self.root)
        self.client.submit_order.assert_called_once()
        self.client.get_order_by_client_id.assert_called_once_with(r.client_order_id)

    def test_ambiguous_timeout_never_resubmits(self):
        r = self.request()
        self.client.submit_order.side_effect = TimeoutError()
        self.client.get_order_by_client_id.side_effect = RuntimeError("not found")
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                submit_once(self.client, r, self.journal, self.jpath, self.root)
        self.client.submit_order.assert_called_once()
        self.assertTrue(self.journal["entry_disabled"])
        self.assertTrue(self.jpath.exists())

    def test_ack_lost_reconciles(self):
        self.client.submit_order.side_effect = TimeoutError()
        self.client.get_order_by_client_id.return_value = NS(id="accepted-id", status="accepted")
        self.assertEqual(
            submit_once(self.client, self.request(), self.journal, self.jpath, self.root).id,
            "accepted-id",
        )
        self.assertTrue(self.journal["entry_disabled"])

    def test_no_submit_if_audit_fails(self):
        with patch("quant_lab.brokers.alpaca.execution.audit", side_effect=OSError()):
            with self.assertRaises(OSError):
                submit_once(self.client, self.request(), self.journal, self.jpath, self.root)
        self.client.submit_order.assert_not_called()

    def test_closed_or_wrong_day_no_submit(self):
        for clock in [
            NS(timestamp=self.now, is_open=False),
            NS(timestamp=self.now + timedelta(days=1), is_open=True),
        ]:
            self.client.get_clock.return_value = clock
            with self.assertRaises(ValueError):
                submit_once(self.client, self.request(), self.journal, self.jpath, self.root)
        self.client.submit_order.assert_not_called()

    def test_account_mismatch(self):
        self.account.id = "another"
        with self.assertRaises(ValueError):
            submit_once(self.client, self.request(), self.journal, self.jpath, self.root)
        self.client.submit_order.assert_not_called()

    def test_exposure_blocks_entry(self):
        self.client.get_all_positions.return_value = [NS(symbol="SPY", qty="1")]
        with self.assertRaises(ValueError):
            submit_once(self.client, self.request(), self.journal, self.jpath, self.root)
        self.client.submit_order.assert_not_called()

    def test_cancellation_requires_confirmation(self):
        order = NS(id="old", symbol="SPY")
        self.client.get_orders.return_value = [order]
        self.assertFalse(cancel_spy(self.client, self.root))
        self.client.get_orders.side_effect = [[order], []]
        self.client.get_order_by_id.return_value = NS(status="pending_cancel")
        self.assertFalse(cancel_spy(self.client, self.root))

    def test_no_sell_while_cancel_pending(self):
        self.client.get_orders.return_value = [NS(id="old", symbol="SPY", client_order_id="legacy")]
        self.assertEqual(
            flatten(self.client, self.journal, self.jpath, self.root, "2026-10-16"),
            "CANCEL_PENDING",
        )
        self.client.submit_order.assert_not_called()

    def test_partial_holding_exit_and_no_short(self):
        self.client.get_all_positions.return_value = [NS(symbol="SPY", qty="1.5")]
        status = flatten(self.client, self.journal, self.jpath, self.root, "2026-10-16")
        self.assertEqual(status, "EXIT_ACCEPTED")
        self.assertEqual(self.client.submit_order.call_args.kwargs["order_data"].qty, 1.5)

    def test_existing_exit_not_duplicated(self):
        self.client.get_orders.return_value = [NS(client_order_id="sqlab-20261016-X-001")]
        self.assertEqual(
            flatten(self.client, self.journal, self.jpath, self.root, "2026-10-16"), "EXIT_PENDING"
        )
        self.client.cancel_order_by_id.assert_not_called()
        self.client.submit_order.assert_not_called()

    def test_risk_exit_does_not_require_market_data(self):
        self.account.equity = "79000"
        data = MagicMock()
        result = cycle(self.client, data, self.root, self.auth, self.journal, self.jpath)
        self.assertTrue(result["done"])
        data.get_stock_bars.assert_not_called()
        self.assertIn('"halted": true', (self.root / "state" / "intraday.json").read_text())

    def test_session_expires(self):
        self.client.get_clock.return_value = NS(
            timestamp=self.now + timedelta(days=1), is_open=False
        )
        self.assertTrue(
            cycle(self.client, MagicMock(), self.root, self.auth, self.journal, self.jpath)["done"]
        )
        self.client.submit_order.assert_not_called()

    def test_stop_file_flattens_before_signals(self):
        (self.root / "STOP_PAPER").touch()
        with patch("quant_lab.brokers.alpaca.execution.tick") as strategy:
            r = cycle(self.client, MagicMock(), self.root, self.auth, self.journal, self.jpath)
        self.assertEqual(r["status"], "FLAT")
        strategy.assert_not_called()

    def test_full_entry_cycle(self):
        self.client.get_asset.return_value = NS(tradable=True, status="active")
        data = MagicMock()
        data.get_stock_latest_quote.return_value = {"SPY": self.quote}
        report = self.report | dict(action="BUY CANDIDATE")
        with patch("quant_lab.brokers.alpaca.execution.tick", return_value=report):
            result = cycle(self.client, data, self.root, self.auth, self.journal, self.jpath)
        self.assertEqual(result["status"], "ENTRY_ACCEPTED")
        self.client.submit_order.assert_called_once()

    def test_preclose_exit_ignores_failed_signal_service(self):
        self.client.get_clock.return_value = NS(
            timestamp=self.now.replace(hour=15, minute=51), is_open=True
        )
        with patch(
            "quant_lab.brokers.alpaca.execution.tick", side_effect=RuntimeError()
        ) as strategy:
            result = cycle(self.client, MagicMock(), self.root, self.auth, self.journal, self.jpath)
        self.assertEqual(result["status"], "FLAT")
        strategy.assert_not_called()

    def test_stale_signal_with_holdings_exits(self):
        self.client.get_all_positions.return_value = [NS(symbol="SPY", qty="2")]
        with patch(
            "quant_lab.brokers.alpaca.execution.tick",
            return_value=dict(action="STOP", reason="stale bars"),
        ):
            result = cycle(self.client, MagicMock(), self.root, self.auth, self.journal, self.jpath)
        self.assertEqual(result["status"], "EXIT_ACCEPTED")
        self.assertTrue(self.journal["entry_disabled"])
