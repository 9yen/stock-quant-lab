import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from quant_lab.strategies.capital import assess, public_status, record


class CapitalTests(unittest.TestCase):
    def check_entry(
        self, capital=10000, qty=9, price=100, short=False, spread=0.02, gross=0, **updates
    ):
        budget = dict(equity=capital, baseline=capital, exit_fee_reserve=0)
        budget.update(updates)
        return assess(budget, qty, price, short, spread, gross, 0.01)

    def test_500_whole_share_trade_rejected_after_fees(self):
        result = self.check_entry(capital=500, qty=4, price=10)
        self.assertEqual(result["reason"], "COST_EXCEEDS_EDGE")
        self.assertLess(result["target_net"], 0)

    def test_even_40_percent_ticket_is_not_forced_to_trade(self):
        self.assertEqual(self.check_entry(capital=500, qty=2)["reason"], "COST_EXCEEDS_EDGE")

    def test_10000_ticket_can_pass(self):
        self.assertTrue(self.check_entry()["allowed"])

    def test_whole_share_short_and_invalid_inputs(self):
        self.assertEqual(self.check_entry(capital=500, qty=0)["reason"], "WHOLE_SHARE_BUDGET")
        self.assertEqual(
            self.check_entry(capital=500, short=True)["reason"], "SMALL_BUDGET_LONG_ONLY"
        )
        for value in (float("nan"), float("inf"), -1):
            self.assertFalse(self.check_entry(price=value)["allowed"])

    def test_wide_spread_reduces_economics(self):
        self.assertEqual(self.check_entry(spread=1)["reason"], "COST_EXCEEDS_EDGE")

    def test_pending_gross_reserves_daily_risk(self):
        self.assertEqual(self.check_entry(gross=9500)["reason"], "DAILY_RISK_RESERVED")

    def test_pending_orders_reserve_additional_costs(self):
        budget = dict(equity=10000, baseline=10000)
        result = assess(budget, 9, 100, False, 0.02, 8500, 0.01, pending=2)
        self.assertEqual(result["reason"], "DAILY_RISK_RESERVED")

    def test_realized_loss_limits_new_entries(self):
        self.assertEqual(self.check_entry(equity=9910)["reason"], "DAILY_RISK_RESERVED")

    def test_risk_per_trade_includes_costs(self):
        self.assertEqual(self.check_entry(qty=60)["reason"], "TRADE_RISK_LIMIT")

    def test_stats_survive_restart_and_count_checks(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            now = datetime(2026, 10, 19, 15, tzinfo=timezone.utc)
            result = self.check_entry(capital=500, qty=0)
            record(root, "moomoo", "US.AAPL", result, now)
            record(root, "moomoo", "US.AAPL", result, now)
            self.assertEqual(public_status(root, "moomoo")["checks"], 2)
            self.assertEqual(public_status(root, "moomoo")["reasons"], {"WHOLE_SHARE_BUDGET": 2})
