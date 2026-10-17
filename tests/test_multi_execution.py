import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from quant_lab.brokers.alpaca.execution import entry_request, flatten, submit_once
from quant_lab.brokers.alpaca.multi_execution import (
    ParallelReads,
    candidate_rows,
    entry_counts,
    multi_cycle,
)
from quant_lab.strategies.scanner import UNIVERSE


class MultiTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parents[1] / "state" / "test-scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        self.tmp = TemporaryDirectory(dir=scratch)
        self.root = Path(self.tmp.name)
        self.now = datetime(2026, 10, 16, 10, 0, 45, tzinfo=ZoneInfo("America/New_York"))
        self.open = self.now.replace(hour=9, minute=30, second=0)
        self.client = MagicMock()
        self.client._base_url = "https://paper-api.alpaca.markets"
        self.account = NS(
            id="paper",
            equity="100000",
            last_equity="100000",
            cash="100000",
            buying_power="400000",
            status="ACTIVE",
            trading_blocked=False,
            account_blocked=False,
            shorting_enabled=True,
        )
        self.client.get_account.return_value = self.account
        self.client.get_clock.return_value = NS(timestamp=self.now, is_open=True)
        self.client.get_calendar.return_value = [
            NS(open=self.open, close=self.open.replace(hour=16, minute=0))
        ]
        self.client.get_all_positions.return_value = []
        self.client.get_orders.return_value = []
        self.asset = NS(
            tradable=True, status="active", shortable=True, easy_to_borrow=True, marginable=True
        )
        self.client.get_asset.return_value = self.asset
        self.client.submit_order.return_value = NS(id="order", status="accepted")
        self.client.get_order_by_client_id.return_value = NS(id="order", status="filled")
        self.quote = NS(timestamp=self.now, bid_price=101, ask_price=101.02)
        self.readers = MagicMock()
        self.readers.snapshot.return_value = {
            s: dict(asset=self.asset, quote=self.quote) for s in UNIVERSE
        }
        self.data = MagicMock()
        self.auth = dict(
            account_id="paper",
            session="2026-10-16",
            strategy="multi_1m",
            universe=list(UNIVERSE),
            allow_short=True,
            max_positions=6,
            per_symbol_limit=19,
            global_entry_limit=100,
        )
        self.j = dict(self.auth, entry_disabled=False, orders={})
        self.jp = self.root / "state" / "journal.json"

    def tearDown(self):
        self.tmp.cleanup()

    def row(self, symbol="QQQ", direction="LONG"):
        return dict(
            symbol=symbol,
            minute_signal=direction,
            last_bar=(self.now - timedelta(minutes=1, seconds=45)).isoformat(),
            range_high=100,
            range_low=99,
            price=101 if direction == "LONG" else 98,
            tradable=True,
            shortable=True,
        )

    def scan(self, rows):
        return dict(mode="OBSERVE ONLY", recorded_at=self.now.isoformat(), rows=rows)

    def run_cycle(self, rows):
        with patch(
            "quant_lab.brokers.alpaca.multi_execution.collect", return_value=self.scan(rows)
        ):
            return multi_cycle(
                self.client, self.data, self.root, self.auth, self.j, self.jp, readers=self.readers
            )

    def request(self, symbol="QQQ", stamp=None):
        report = self.row(symbol) | dict(
            action="BUY CANDIDATE",
            candidate_qty=100000,
            reference_price=101,
            signal_bar=stamp or self.row()["last_bar"],
        )
        return entry_request(report, self.account, self.quote, self.now, "2026-10-16")

    def holding(self, symbol="QQQ", qty=10, value=1010):
        return NS(symbol=symbol, qty=str(qty), market_value=str(value), avg_entry_price="100")

    def test_multiple_symbols_can_enter_same_cycle(self):
        r = self.run_cycle([self.row("QQQ"), self.row("NVDA")])
        self.assertEqual({x["symbol"] for x in r["submitted"]}, {"QQQ", "NVDA"})
        self.assertEqual(self.client.submit_order.call_count, 2)

    def test_strategy_budget_limits_real_entry_and_final_submission(self):
        from quant_lab.runtime.storage import write_json

        config = dict(
            schema_version=1,
            currency="USD",
            initial_capital=5000,
            account_id="paper",
            activated_at=(self.now - timedelta(minutes=1)).isoformat(),
        )
        write_json(
            self.root / "state/strategy_budget/config.json",
            {"schema_version": 1, "brokers": {"alpaca": config}},
        )
        self.auth.update(max_position=0.4, target_position=0.1, gross_limit=1)
        self.j.update(max_position=0.4, target_position=0.1, gross_limit=1)
        with patch("quant_lab.analytics.realized.read_fills", return_value=[]):
            result = self.run_cycle([self.row("QQQ")])
            self.assertEqual(len(result["submitted"]), 1)
            request = self.client.submit_order.call_args.kwargs["order_data"]
            self.assertLessEqual(request.qty * request.limit_price * 1.02, 500)
            self.client.submit_order.reset_mock()
            self.j["orders"] = {}
            self.j.update(max_position=0.4, target_position=0.1, gross_limit=1)
            oversized = self.request("NVDA")
            oversized.qty = 20
            with self.assertRaises(ValueError):
                submit_once(self.client, oversized, self.j, self.jp, self.root)
            self.client.submit_order.assert_not_called()

    def test_existing_holding_does_not_block_other_symbol(self):
        self.client.get_all_positions.return_value = [self.holding()]
        r = self.run_cycle([self.row("QQQ"), self.row("NVDA")])
        self.assertEqual([x["symbol"] for x in r["submitted"]], ["NVDA"])

    def test_other_pending_does_not_block_but_same_symbol_does(self):
        order = NS(
            symbol="QQQ",
            client_order_id="sqlab-20261016-B-old-QQQ",
            submitted_at=self.now,
            qty="10",
            filled_qty="0",
            limit_price="100",
        )
        self.client.get_orders.return_value = [order]
        r = self.run_cycle([self.row("QQQ"), self.row("NVDA")])
        self.assertEqual([x["symbol"] for x in r["submitted"]], ["NVDA"])

    def test_five_percent_cap_applies_per_symbol(self):
        r = self.request()
        self.assertLessEqual(r.qty * r.limit_price * 1.02, 5000)
        r.qty = 100
        with self.assertRaises(ValueError):
            submit_once(self.client, r, self.j, self.jp, self.root)

    def test_pending_notional_counts_toward_thirty_percent(self):
        self.client.get_all_positions.return_value = [self.holding("SPY", qty=250, value=25000)]
        self.client.get_orders.return_value = [
            NS(
                symbol="AAPL",
                client_order_id="sqlab-20261016-B-pending-AAPL",
                qty="20",
                filled_qty="0",
                limit_price="100",
            )
        ]
        with self.assertRaises(ValueError):
            submit_once(self.client, self.request(), self.j, self.jp, self.root)
        self.client.submit_order.assert_not_called()

    def test_symbol_19_cap(self):
        self.j["orders"] = {
            f"sqlab-20261016-B-{i}-QQQ": dict(symbol="QQQ", status="filled") for i in range(19)
        }
        self.assertEqual(self.run_cycle([self.row()])["submitted"], [])
        with self.assertRaises(ValueError):
            submit_once(self.client, self.request(), self.j, self.jp, self.root)

    def test_global_100_cap(self):
        self.j["orders"] = {
            f"sqlab-20261016-B-{i}": dict(symbol=UNIVERSE[i % 6], status="filled")
            for i in range(100)
        }
        self.assertEqual(self.run_cycle([self.row()])["status"], "ENTRY_DISABLED")
        self.client.submit_order.assert_not_called()

    def test_cooldown_is_per_symbol(self):
        from quant_lab.strategies.intraday import daily_state, save_state

        p = self.root / "state" / "intraday.json"
        state = daily_state(p, self.now.date(), 100000, "paper")
        state["symbols"] = {
            "QQQ": dict(last_entry_at=(self.now - timedelta(minutes=2)).isoformat())
        }
        save_state(p, state)
        r = self.run_cycle([self.row("QQQ"), self.row("NVDA")])
        self.assertEqual([x["symbol"] for x in r["submitted"]], ["NVDA"])

    def test_slow_symbol_does_not_block_others(self):
        self.readers.snapshot.return_value["QQQ"] = dict(error="ReadPending")
        r = self.run_cycle([self.row("QQQ"), self.row("NVDA")])
        self.assertEqual([x["symbol"] for x in r["submitted"]], ["NVDA"])

    def test_daily_loss_liquidates_all_pool_symbols_without_scan(self):
        self.account.equity = "79000"
        with patch("quant_lab.brokers.alpaca.multi_execution.collect") as scan:
            r = multi_cycle(
                self.client, self.data, self.root, self.auth, self.j, self.jp, self.readers
            )
        self.assertTrue(r["done"])
        scan.assert_not_called()
        self.assertEqual(set(r["exits"]), set(UNIVERSE))

    def test_preclose_bypasses_quote_workers(self):
        self.client.get_clock.return_value = NS(
            timestamp=self.now.replace(hour=15, minute=51), is_open=True
        )
        r = self.run_cycle([])
        self.assertTrue(r["done"])
        self.readers.snapshot.assert_not_called()

    def test_short_cover_targets_exact_symbol(self):
        self.client.get_all_positions.return_value = [self.holding("NVDA", qty=-2, value=-200)]
        flatten(self.client, self.j, self.jp, self.root, "2026-10-16", "NVDA")
        r = self.client.submit_order.call_args.kwargs["order_data"]
        self.assertEqual((r.symbol, r.side.value, r.qty), ("NVDA", "buy", 2))

    def test_historical_or_future_signal_rejected(self):
        old = self.scan([self.row()])
        old["recorded_at"] = (self.now - timedelta(minutes=5)).isoformat()
        self.assertEqual(candidate_rows(old, self.now, self.open), [])
        bad = self.row()
        bad["last_bar"] = self.now.isoformat()
        self.assertEqual(candidate_rows(self.scan([bad]), self.now, self.open), [])

    def test_reservation_blocks_duplicate_when_broker_snapshot_lags(self):
        submit_once(self.client, self.request(), self.j, self.jp, self.root)
        with self.assertRaises(ValueError):
            submit_once(
                self.client,
                self.request(stamp=(self.now - timedelta(minutes=2)).isoformat()),
                self.j,
                self.jp,
                self.root,
            )
        self.client.submit_order.assert_called_once()

    def test_concurrent_submission_serialized(self):
        active = 0
        maximum = 0
        lock = threading.Lock()

        def send(**kwargs):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            return NS(id="order", status="accepted")

        self.client.submit_order.side_effect = send
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    submit_once, self.client, self.request(symbol), self.j, self.jp, self.root
                )
                for symbol in ("QQQ", "NVDA")
            ]
            for f in futures:
                f.result()
        self.assertEqual(maximum, 1)
        self.assertEqual(len(self.j["orders"]), 2)


class PoolTests(unittest.TestCase):
    def test_pending_read_does_not_queue_duplicates_or_block_fast_symbol(self):
        gate = threading.Event()
        pool = ParallelReads(workers=2)
        called = []

        def worker(symbol):
            called.append(symbol)
            if symbol == "SLOW":
                gate.wait(2)
            return dict(quote=symbol)

        pool.work = worker
        try:
            first = pool.snapshot(["SLOW", "FAST"], timeout=0.05)
            self.assertEqual(first["SLOW"]["error"], "ReadPending")
            self.assertEqual(first["FAST"]["quote"], "FAST")
            pool.snapshot(["SLOW"], timeout=0.01)
            self.assertEqual(called.count("SLOW"), 1)
        finally:
            gate.set()
            pool.close()
