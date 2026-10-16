import unittest
from datetime import date
from unittest.mock import patch

import pandas as pd

from quant_lab.brokers.alpaca.preview import main
from quant_lab.strategies.core import Snapshot, decision, signal


class CoreTests(unittest.TestCase):
    def test_pending_blocks_buy_and_sell(self):
        for invested in (True, False):
            result = decision(Snapshot(100000, 95000, 5, 4000, True), 800, invested)
            self.assertEqual(result["action"], "WAIT")
            self.assertEqual(result["candidate_qty"], 0)

    def test_budget(self):
        result = decision(Snapshot(100000, 100000, 0, 0), 771.79, True)
        self.assertEqual(result["candidate_qty"], 6)
        self.assertLessEqual(result["candidate_qty"] * 771.79 * 1.02, 5000)

    def test_cash_and_existing_value(self):
        self.assertEqual(decision(Snapshot(100000, 100, 0, 0), 800, True)["candidate_qty"], 0)
        self.assertEqual(decision(Snapshot(100000, 90000, 5, 4000), 800, True)["candidate_qty"], 1)

    def test_cash_target(self):
        self.assertEqual(
            decision(Snapshot(100000, 95000, 5.5, 4000), 800, False)["candidate_qty"], 5.5
        )

    def test_abnormal_states(self):
        for qty, value in [(-1, -800), (0, 800), (1, 0), (8, 6400)]:
            self.assertEqual(
                decision(Snapshot(100000, 90000, qty, value), 800, True)["action"], "REVIEW"
            )
        self.assertEqual(
            decision(Snapshot(100000, 90000, 0, 0, blocked=True), 800, True)["action"], "STOP"
        )

    def test_invalid_numbers(self):
        for price in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                decision(Snapshot(100000, 100000, 0, 0), price, True)

    def frame(self):
        return pd.DataFrame(
            {"Close": range(1, 221)}, index=pd.bdate_range(end="2026-10-15", periods=220)
        )

    def test_fresh_signal(self):
        result = signal(self.frame(), date(2026, 10, 15))
        self.assertTrue(result["invested"])

    def test_stale_short_duplicate_and_nan(self):
        frame = self.frame()
        bad_nan = frame.copy()
        bad_nan.iloc[-1, 0] = float("nan")
        for bad in (frame.iloc[:-1], frame.tail(199), pd.concat([frame, frame.tail(1)]), bad_nan):
            with self.assertRaises(ValueError):
                signal(bad, date(2026, 10, 15))

    def test_excludes_uncompleted_bar(self):
        frame = self.frame()
        frame.loc[pd.Timestamp("2026-10-16")] = 99999
        self.assertEqual(signal(frame, date(2026, 10, 15))["price"], 220)


if __name__ == "__main__":
    unittest.main()
