import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from quant_lab.runtime.budget import BudgetAccount, calculate, evaluate, folder, settings
from quant_lab.runtime.storage import write_json
from quant_lab.services.budget_setup import activate


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = datetime(2026, 10, 19, 14, tzinfo=timezone.utc)
        self.config = dict(
            schema_version=1,
            currency="USD",
            initial_capital=5000,
            account_id="paper",
            activated_at=self.now.isoformat(),
        )

    def configure(self):
        write_json(
            self.root / "state/strategy_budget/config.json",
            {"schema_version": 1, "brokers": {"alpaca": self.config, "moomoo": self.config}},
        )

    def fill(self, side="buy", qty=5, price=100, identity="order-1", seconds=1):
        return dict(
            id=identity,
            symbol="AAPL",
            side=side,
            qty=str(qty),
            price=str(price),
            order_id=identity.split(":")[0],
            transaction_time=(self.now + timedelta(seconds=seconds)).isoformat(),
        )

    def test_broker_balance_does_not_change_virtual_capital(self):
        self.configure()
        for equity in (100000, 1000000):
            result = evaluate(self.root, "alpaca", "paper", [], [], equity, equity, 0, self.now)
            self.assertEqual(result["equity"], 5000)
            self.assertLess(result["available_cash"], 5000)

    def test_long_roundtrip_tracks_realized_and_all_order_fees(self):
        result = calculate(self.config, [self.fill(), self.fill("sell", 5, 102, "order-2", 2)], [])
        self.assertEqual(result["realized_gross"], 10)
        self.assertGreater(result["estimated_fees"], 1.98)
        self.assertAlmostEqual(result["equity"], 5010 - result["estimated_fees"])

    def test_short_proceeds_are_not_equity_or_spendable_leverage(self):
        self.configure()
        result = evaluate(
            self.root,
            "alpaca",
            "paper",
            [self.fill("sell", 5, 100)],
            [dict(symbol="AAPL", qty=-5, value=-490)],
            100010,
            100500,
            490,
            self.now,
        )
        self.assertAlmostEqual(result["equity"], 5010 - result["estimated_fees"])
        self.assertLess(result["available_cash"], 4520)
        self.assertEqual(result["unrealized"], 10)

    def test_short_roundtrip_and_partial_cover(self):
        rows = [self.fill("sell", 5, 100), self.fill("buy", 2, 95, "order-2", 2)]
        result = calculate(self.config, rows, [dict(symbol="AAPL", qty=-3, value=-285)])
        self.assertEqual(result["realized_gross"], 10)
        self.assertEqual(result["unrealized"], 15)

    def test_pre_activation_history_is_excluded(self):
        rows = [self.fill(seconds=-20), self.fill("sell", 5, 110, "old", -10)]
        self.assertEqual(calculate(self.config, rows, [])["equity"], 5000)

    def test_duplicate_fill_is_idempotent_but_conflicting_copy_blocks(self):
        row = self.fill()
        positions = [dict(symbol="AAPL", qty=5, value=500)]
        one = calculate(self.config, [row], positions)
        self.assertEqual(one, calculate(self.config, [row, row], positions))
        with self.assertRaises(ValueError):
            calculate(self.config, [row, dict(row, price="101")], positions)

    def test_split_fills_charge_platform_once_per_order_day(self):
        rows = [
            self.fill(qty=2, identity="order:1"),
            self.fill(qty=3, identity="order:2", seconds=2),
        ]
        partial = calculate(self.config, rows, [dict(symbol="AAPL", qty=5, value=500)])
        whole = calculate(self.config, [self.fill()], [dict(symbol="AAPL", qty=5, value=500)])
        self.assertEqual(partial["estimated_fees"], whole["estimated_fees"])

    def test_missing_fills_or_external_positions_pause_entries(self):
        self.configure()
        result = evaluate(
            self.root,
            "alpaca",
            "paper",
            [],
            [dict(symbol="AAPL", qty=5, value=500)],
            100000,
            100000,
            500,
            self.now,
        )
        self.assertFalse(result["valid"])
        self.assertEqual(result["available_cash"], 0)

    def test_pending_reservations_consume_budget(self):
        self.configure()
        result = evaluate(self.root, "alpaca", "paper", [], [], 100000, 100000, 4900, self.now)
        self.assertLess(result["available_cash"], 100)

    def test_restart_preserves_daily_baseline_after_loss(self):
        self.configure()
        evaluate(self.root, "alpaca", "paper", [], [], 100000, 100000, 0, self.now)
        fills = [self.fill(), self.fill("sell", 5, 90, "exit", 2)]
        result = evaluate(
            self.root,
            "alpaca",
            "paper",
            fills,
            [],
            99950,
            99950,
            0,
            self.now + timedelta(seconds=4),
        )
        self.assertEqual(result["baseline"], 5000)
        self.assertLess(result["equity"], 4950)
        again = evaluate(
            self.root,
            "alpaca",
            "paper",
            fills,
            [],
            99950,
            99950,
            0,
            self.now + timedelta(minutes=1),
        )
        self.assertEqual(result["equity"], again["equity"])
        self.assertEqual(again["baseline"], 5000)

    def test_account_binding_and_read_only_preview(self):
        self.configure()
        result = evaluate(
            self.root, "alpaca", "wrong", [], [], 100000, 100000, 0, self.now, commit=False
        )
        self.assertFalse(result["valid"])
        self.assertFalse((folder(self.root, "alpaca") / "snapshot.json").exists())

    def test_proxy_limits_cash_and_buying_power_without_changing_sdk_account(self):
        original = SimpleNamespace(equity=100000, cash=100000, buying_power=400000, id="paper")
        proxy = BudgetAccount(original, dict(equity=5000, available_cash=800, baseline=5000))
        self.assertEqual((proxy.equity, proxy.cash, proxy.buying_power), (5000, 800, 800))
        self.assertEqual(original.equity, 100000)

    def test_options_are_not_silently_funded_by_stock_budget(self):
        with self.assertRaises(ValueError):
            calculate(
                self.config, [dict(self.fill(qty=1), instrument="OPTION", multiplier=100)], []
            )

    def test_activate_both_only_after_stopped_flat_checks_and_never_reset(self):
        def checks(root):
            return {"alpaca": ("paper", 100000, 100000), "moomoo": ("paper", 1000000, 1000000)}

        with patch("quant_lab.services.budget_setup.inventory", return_value={}):
            result = activate(self.root, 5000, checks)
            self.assertEqual(set(result["brokers"]), {"alpaca", "moomoo"})
            self.assertEqual(settings(self.root, "alpaca")["initial_capital"], 5000)
            with self.assertRaises(ValueError):
                activate(self.root, 5000, checks)

    def test_running_workers_or_insufficient_real_cash_block_activation(self):
        with patch("quant_lab.services.budget_setup.inventory", return_value={"worker": [1]}):
            with self.assertRaises(RuntimeError):
                activate(self.root, 5000, lambda root: {})
        with patch("quant_lab.services.budget_setup.inventory", return_value={}):
            with self.assertRaises(ValueError):
                activate(self.root, 5000, lambda root: {"alpaca": ("paper", 100000, 100)})
        self.assertIsNone(settings(self.root, "alpaca"))
