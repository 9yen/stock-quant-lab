import json
import re
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from quant_lab.dashboard.desks import moomoo_chart, moomoo_state, review_home, stock_report
from quant_lab.runtime.storage import write_json


class SharedDeskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = datetime.now(timezone.utc).isoformat()

    def test_stock_report_matches_gross_and_deducts_paired_fees(self):
        fills = [
            dict(id="entry:2", symbol="US.AAPL", side="BUY", qty=2, price=100, time=self.now),
            dict(id="exit:2", symbol="US.AAPL", side="SELL", qty=2, price=102, time=self.now),
        ]
        result = stock_report(self.root, fills, [], dict(baseline=10000), {}, self.now)
        self.assertTrue(result["realized_complete"])
        self.assertEqual(result["realized_pnl"], 4)
        self.assertGreater(result["review"]["current"]["fees"], 0)
        self.assertLess(result["review"]["current"]["closed_net"], 4)
        self.assertTrue((review_home(self.root) / "state/reviews/fills.json").exists())
        self.assertFalse((self.root / "state/reviews/fills.json").exists())

    def test_adapter_never_exposes_private_account_or_order_ids(self):
        write_json(
            self.root / "state/brokers/moomoo_snapshot.json",
            dict(
                ok=True,
                updated=self.now,
                account="DO_NOT_EXPOSE_ACCOUNT",
                assets=10000,
                cash=9000,
                holdings=[],
                auto=dict(
                    baseline=10000,
                    orders=[
                        dict(
                            symbol="US.AAPL",
                            kind="ENTRY",
                            side="BUY",
                            qty=1,
                            status="FILLED_ALL",
                            order_id="DO_NOT_EXPOSE_ORDER",
                            remark="DO_NOT_EXPOSE_REMARK",
                        )
                    ],
                ),
                market={},
            ),
        )
        result = moomoo_state(self.root)
        self.assertEqual(result["account"]["equity"], 10000)
        self.assertEqual(result["account"]["rows"][0]["raw_status"], "filled")
        self.assertNotIn("DO_NOT_EXPOSE", json.dumps(result))

    def test_chart_preserves_volume_and_rejects_non_universe_paths(self):
        bar = dict(time=self.now, open=100, high=101, low=99, close=100, volume=1234)
        write_json(
            self.root / "state/brokers/moomoo_charts/US.AAPL.json",
            dict(candles=[bar], updated=self.now),
        )
        result = moomoo_chart(self.root, "AAPL")
        self.assertEqual(result["bars"][0]["volume"], 1234)
        self.assertEqual(result["symbol"], "AAPL")
        self.assertEqual(result["bar_minutes"], 1)
        with self.assertRaises(ValueError):
            moomoo_chart(self.root, "../../.env")

    def test_missing_observer_is_not_reported_as_connected(self):
        result = moomoo_state(self.root)
        self.assertIsNone(result["account"])
        self.assertIsNotNone(result["account_error"])

    def test_both_pages_share_template_and_have_unique_controls(self):
        base = Path(__file__).resolve().parents[1] / "src/quant_lab/dashboard/assets"
        if not base.exists():
            from quant_lab.runtime.paths import web_assets

            base = web_assets(self.root)
        html = (base / "index.html").read_text(encoding="utf-8")
        self.assertEqual(html, (base / "moomoo.html").read_text(encoding="utf-8"))
        ids = re.findall(r'\bid="([^"]+)"', html)
        self.assertEqual(len(ids), len(set(ids)))
        for control in (
            "chart-type",
            "older-bars",
            "newer-bars",
            "pause",
            "full",
            "history-sort",
            "moomoo-net",
        ):
            self.assertIn(control, ids)
        self.assertNotIn("/moomoo.js", html)
