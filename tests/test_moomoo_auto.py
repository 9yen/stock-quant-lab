import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as N
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pandas as pd

import quant_lab.brokers.moomoo.execution as a

NY = ZoneInfo("America/New_York")


class AutoTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parents[1] / "state" / "test-scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.patch = patch.object(a, "DIR", self.directory)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        rootpatch = patch.object(a, "ROOT", self.directory)
        rootpatch.start()
        self.addCleanup(rootpatch.stop)
        self.sdk = N(
            RET_OK=0,
            TrdEnv=N(SIMULATE="SIMULATE"),
            TrdMarket=N(US="US"),
            Currency=N(USD="USD"),
            TrdSide=N(BUY="BUY", SELL="SELL"),
            OrderType=N(MARKET="MARKET", NORMAL="NORMAL"),
            TimeInForce=N(DAY="DAY"),
            Session=N(RTH="RTH"),
            ModifyOrderOp=N(CANCEL="CANCEL"),
        )
        self.t = MagicMock()
        self.q = MagicMock()
        self.e = a.Executor(self.t, self.q, self.sdk)
        self.e.aid = 123

    def test_budget_reserves_and_no_leverage(self):
        self.assertEqual(a.entry_qty(100000, 100, 99000, 100000), 9)
        self.assertEqual(a.entry_qty(100000, 100, 100000, 100000), 0)
        self.assertEqual(a.entry_qty(100000, 100, 0, 0), 0)
        self.assertEqual(a.entry_qty(100000, 100, 0, 100000), 99)

    def test_short_position_sign_from_broker_side(self):
        self.assertEqual(a.position_qty(dict(qty=10, position_side="SHORT")), -10)
        self.assertEqual(a.position_qty(dict(qty=-10, position_side="SHORT")), -10)
        with self.assertRaises(ValueError):
            a.position_qty(dict(qty=10, position_side="UNKNOWN"))

    def test_stale_and_crossed_quotes_block(self):
        now = datetime(2026, 10, 19, 10, tzinfo=NY)
        row = dict(bid_price=100, ask_price=100.1, update_time="2026-10-19 10:00:00")
        self.assertTrue(a.fresh_quote(row, now))
        self.assertFalse(a.fresh_quote(row, now + timedelta(minutes=2)))
        self.assertFalse(a.fresh_quote(dict(row, ask_price=99), now))

    def test_timeout_persists_unknown_and_does_not_retry(self):
        self.t.place_order.side_effect = TimeoutError()
        now = datetime(2026, 10, 19, 10, tzinfo=NY)
        self.e.submit("US.AAPL", 10, "BUY", "ENTRY", 100, "bar", "test", now)
        self.e.submit("US.AAPL", 10, "BUY", "ENTRY", 100, "bar", "test", now)
        self.assertEqual(self.t.place_order.call_count, 1)
        self.assertEqual(self.e.state["orders"][0]["status"], "UNKNOWN")
        self.assertEqual(self.t.place_order.call_args.kwargs["trd_env"], "SIMULATE")
        self.assertEqual(self.t.place_order.call_args.kwargs["acc_id"], 123)

    def test_restart_recovers_unknown_by_remark(self):
        self.t.place_order.side_effect = TimeoutError()
        now = datetime(2026, 10, 19, 10, tzinfo=NY)
        self.e.submit("US.AAPL", 10, "BUY", "ENTRY", 100, "bar", "test", now)
        order = self.e.state["orders"][0]
        restarted = a.Executor(self.t, self.q, self.sdk)
        restarted.reconcile(
            [
                dict(
                    order_id="o",
                    remark=order["remark"],
                    order_status="FILLED_ALL",
                    dealt_qty=10,
                    dealt_avg_price=100,
                )
            ]
        )
        self.assertEqual(a.owned_qty(restarted.state["orders"], "US.AAPL"), 10)
        self.assertEqual(a.active(restarted.state["orders"]), [])

    def test_immediate_fill_is_recorded(self):
        self.t.place_order.return_value = (
            0,
            pd.DataFrame(
                [dict(order_id="o", order_status="FILLED_ALL", dealt_qty=10, dealt_avg_price=100)]
            ),
        )
        self.e.submit(
            "US.AAPL",
            10,
            "SELL",
            "ENTRY",
            100,
            "bar",
            "test",
            datetime(2026, 10, 19, 10, tzinfo=NY),
        )
        self.assertEqual(a.owned_qty(self.e.state["orders"], "US.AAPL"), -10)

    def test_partial_order_reserves_only_unfilled(self):
        orders = [
            dict(
                symbol="US.AAPL",
                side="BUY",
                kind="ENTRY",
                status="FILLED_PART",
                qty=10,
                filled=4,
                price=100,
            )
        ]
        self.assertEqual(a.reserve([dict(market_val=400)], orders), 1000)

    def base_cycle(self, real=False):
        a.write(self.directory / "moomoo_auto_config.json", dict(account_id="123", enabled=True))
        self.t.get_acc_list.return_value = (
            0,
            pd.DataFrame(
                [
                    dict(
                        acc_id=123,
                        trd_env="REAL" if real else "SIMULATE",
                        sim_acc_type="STOCK_AND_OPTION",
                        trdmarket_auth=["US"],
                    )
                ]
            ),
        )
        self.t.order_list_query.return_value = (0, pd.DataFrame([]))
        self.t.position_list_query.return_value = (0, pd.DataFrame([]))
        self.t.accinfo_query.return_value = (
            0,
            pd.DataFrame([dict(total_assets=100000, cash=100000)]),
        )

    def test_real_account_rejected(self):
        self.base_cycle(real=True)
        with self.assertRaises(RuntimeError):
            self.e.cycle(datetime(2026, 10, 19, 10, tzinfo=NY))
        self.t.place_order.assert_not_called()

    def test_weekend_enabled_but_no_order(self):
        self.base_cycle()
        self.q.request_trading_days.return_value = (0, [])
        report = self.e.cycle(datetime(2026, 10, 17, 10, tzinfo=NY))
        self.assertEqual(report["status"], "WAIT_TRADING_DAY")
        self.assertTrue(report["enabled"])
        self.t.place_order.assert_not_called()

    def test_afterhours_no_order(self):
        self.base_cycle()
        self.q.request_trading_days.return_value = (
            0,
            [dict(time="2026-10-19", trade_date_type="WHOLE")],
        )
        self.q.get_market_state.return_value = (
            0,
            pd.DataFrame([dict(code=c, market_state="AFTER_HOURS_BEGIN") for c in ["US.AAPL"]]),
        )
        self.assertEqual(
            self.e.cycle(datetime(2026, 10, 19, 17, tzinfo=NY))["status"], "WAIT_REGULAR_SESSION"
        )
        self.t.place_order.assert_not_called()

    def test_halfday_entry_window_closed(self):
        self.base_cycle()
        self.q.request_trading_days.return_value = (
            0,
            [dict(time="2026-11-27", trade_date_type="HALF")],
        )
        self.q.get_market_state.return_value = (
            0,
            pd.DataFrame([dict(code=c, market_state="MORNING") for c in ["US.AAPL"]]),
        )
        self.q.get_market_snapshot.return_value = (0, pd.DataFrame([]))
        self.assertEqual(
            self.e.cycle(datetime(2026, 11, 27, 12, 40, tzinfo=NY))["status"], "ENTRY_WINDOW_CLOSED"
        )
        self.t.place_order.assert_not_called()

    def regular(self, now, positions):
        self.base_cycle()
        self.t.position_list_query.return_value = (0, pd.DataFrame(positions))
        self.q.request_trading_days.return_value = (
            0,
            [dict(time=str(now.date()), trade_date_type="WHOLE")],
        )
        self.q.get_market_state.return_value = (
            0,
            pd.DataFrame([dict(code=c, market_state="MORNING") for c in ["US.AAPL"]]),
        )
        self.q.get_market_snapshot.return_value = (
            0,
            pd.DataFrame(
                [
                    dict(
                        code="US.AAPL",
                        bid_price=98,
                        ask_price=98.05,
                        update_time=now.strftime("%Y-%m-%d %H:%M:%S"),
                    )
                ]
            ),
        )
        self.t.place_order.return_value = (
            0,
            pd.DataFrame(
                [
                    dict(
                        order_id="exit1",
                        order_status="FILLED_ALL",
                        dealt_qty=10,
                        dealt_avg_price=98,
                    )
                ]
            ),
        )

    def test_managed_long_stop_submits_paper_market_exit(self):
        now = datetime(2026, 10, 19, 10, tzinfo=NY)
        self.regular(
            now,
            [dict(code="US.AAPL", qty=10, position_side="LONG", cost_price=100, market_val=980)],
        )
        self.e.state["orders"] = [
            dict(
                key="entry",
                remark="e",
                symbol="US.AAPL",
                side="BUY",
                kind="ENTRY",
                qty=10,
                filled=10,
                status="FILLED_ALL",
            )
        ]
        self.assertEqual(self.e.cycle(now)["status"], "EXIT_SUBMITTED")
        args = self.t.place_order.call_args.kwargs
        self.assertEqual(
            (args["trd_env"], args["trd_side"], args["qty"], args["order_type"]),
            ("SIMULATE", "SELL", 10, "MARKET"),
        )

    def test_external_position_never_sold(self):
        now = datetime(2026, 10, 19, 10, tzinfo=NY)
        self.regular(
            now,
            [dict(code="US.AAPL", qty=10, position_side="LONG", cost_price=100, market_val=980)],
        )
        self.assertEqual(self.e.cycle(now)["status"], "RECONCILIATION_HOLD")
        self.t.place_order.assert_not_called()

    def test_managed_short_profit_buys_back(self):
        now = datetime(2026, 10, 19, 10, tzinfo=NY)
        self.regular(
            now,
            [dict(code="US.AAPL", qty=10, position_side="SHORT", cost_price=101, market_val=-980)],
        )
        self.e.state["orders"] = [
            dict(
                key="entry",
                remark="e",
                symbol="US.AAPL",
                side="SELL",
                kind="ENTRY",
                qty=10,
                filled=10,
                status="FILLED_ALL",
            )
        ]
        self.assertEqual(self.e.cycle(now)["status"], "EXIT_SUBMITTED")
        self.assertEqual(self.t.place_order.call_args.kwargs["trd_side"], "BUY")

    def test_daily_budget_liquidates_even_without_fresh_quote(self):
        now = datetime(2026, 10, 19, 10, tzinfo=NY)
        self.regular(
            now,
            [dict(code="US.AAPL", qty=10, position_side="LONG", cost_price=100, market_val=980)],
        )
        self.e.state["orders"] = [
            dict(
                key="entry",
                remark="e",
                symbol="US.AAPL",
                side="BUY",
                kind="ENTRY",
                qty=10,
                filled=10,
                status="FILLED_ALL",
            )
        ]
        self.e.state["days"][str(now.date())] = dict(baseline=104000, halted=False)
        self.q.get_market_snapshot.return_value = (0, pd.DataFrame([]))
        self.assertEqual(self.e.cycle(now)["status"], "EXIT_SUBMITTED")
        self.assertEqual(self.e.state["orders"][-1]["reason"], "DAILY_RISK")

    def candidate_cycle(self, now):
        self.regular(now, [])
        self.sdk.KLType = N(K_1M="K_1M")
        self.sdk.AuType = N(QFQ="QFQ")
        self.q.get_cur_kline.return_value = (0, pd.DataFrame([]))
        self.q.get_market_snapshot.return_value = (
            0,
            pd.DataFrame(
                [
                    dict(
                        code="US.AAPL",
                        bid_price=100.19,
                        ask_price=100.21,
                        update_time=now.strftime("%Y-%m-%d %H:%M:%S"),
                    )
                ]
            ),
        )
        self.t.acctradinginfo_query.return_value = (
            0,
            pd.DataFrame([dict(max_cash_buy=1000, max_sell_short=1000)]),
        )
        row = dict(
            symbol="US.AAPL",
            updated=now.isoformat(),
            rvol=1.2,
            signal="LONG",
            range_high=100,
            range_low=99,
            price=100.2,
            atr14=1,
            last_bar="2026-10-19T09:58:00-04:00",
        )
        a.write(self.directory / "quant_lab.brokers.moomoo.market.json", dict(rows=[row]))
        return row

    def test_option_entry_buys_one_contract_only(self):
        now = datetime(2026, 10, 19, 10, tzinfo=NY)
        row = self.candidate_cycle(now)
        a.write(
            self.directory / "moomoo_auto_config.json",
            dict(account_id="123", enabled=True, options_enabled=True),
        )
        option = dict(
            code="US.AAPL261030C100000",
            price=4,
            qty=1,
            multiplier=100,
            instrument="OPTION",
            underlying="US.AAPL",
            option_type="CALL",
            expiry="2026-10-30",
            strike=100,
        )
        self.t.place_order.return_value = (
            0,
            pd.DataFrame(
                [dict(order_id="op1", order_status="FILLED_ALL", dealt_qty=1, dealt_avg_price=4)]
            ),
        )
        with (
            patch("quant_lab.brokers.moomoo.execution.analyze", return_value=row),
            patch("quant_lab.brokers.moomoo.execution.fresh_quote", return_value=True),
            patch("quant_lab.brokers.moomoo.options.select_contract", return_value=option),
        ):
            self.assertEqual(self.e.cycle(now)["status"], "OPTION_ENTRY_SUBMITTED")
        args = self.t.place_order.call_args.kwargs
        self.assertEqual((args["trd_env"], args["trd_side"], args["qty"]), ("SIMULATE", "BUY", 1))
        self.assertEqual(self.e.state["fills"][0]["multiplier"], 100)

    def test_stock_entry_if_options_not_enabled(self):
        now = datetime(2026, 10, 19, 10, tzinfo=NY)
        row = self.candidate_cycle(now)
        self.t.place_order.return_value = (
            0,
            pd.DataFrame(
                [dict(order_id="s1", order_status="SUBMITTED", dealt_qty=0, dealt_avg_price=0)]
            ),
        )
        with (
            patch("quant_lab.brokers.moomoo.execution.analyze", return_value=row),
            patch("quant_lab.brokers.moomoo.execution.fresh_quote", return_value=True),
        ):
            self.assertEqual(self.e.cycle(now)["status"], "ENTRY_SUBMITTED")
        args = self.t.place_order.call_args.kwargs
        self.assertEqual(args["code"], "US.AAPL")
        self.assertGreater(args["qty"], 0)
        self.assertLessEqual(args["qty"] * args["price"], 10000)

    def test_cumulative_average_generates_incremental_fill_price(self):
        o = dict(
            symbol="US.AAPL",
            qty=10,
            side="BUY",
            kind="ENTRY",
            remark="p",
            created="2026-10-19T10:00:00-04:00",
            filled=0,
            average=0,
        )
        self.e.apply_fill(
            o, dict(order_id="1", order_status="FILLED_PART", dealt_qty=4, dealt_avg_price=100)
        )
        self.e.apply_fill(
            o, dict(order_id="1", order_status="FILLED_ALL", dealt_qty=10, dealt_avg_price=103)
        )
        self.assertEqual([f["qty"] for f in self.e.state["fills"]], [4, 6])
        self.assertEqual([f["price"] for f in self.e.state["fills"]], [100, 105])
        self.e.apply_fill(
            o, dict(order_id="1", order_status="FILLED_ALL", dealt_qty=10, dealt_avg_price=103)
        )
        self.assertEqual(len(self.e.state["fills"]), 2)

    def test_option_reservation_uses_multiplier(self):
        orders = [
            dict(
                symbol="OPT",
                kind="ENTRY",
                status="SUBMITTED",
                qty=1,
                filled=0,
                price=8,
                multiplier=100,
            )
        ]
        self.assertEqual(a.reserve([], orders), 800)

    def test_option_entry_rejects_naked_sale_or_multiple_contracts(self):
        now = datetime(2026, 10, 19, 10, tzinfo=NY)
        contract = dict(instrument="OPTION", multiplier=100)
        with self.assertRaises(ValueError):
            self.e.submit("OPT", 1, "SELL", "ENTRY", 5, "bar", "test", now, contract)
        with self.assertRaises(ValueError):
            self.e.submit("OPT", 2, "BUY", "ENTRY", 5, "bar", "test", now, contract)
        self.t.place_order.assert_not_called()

    def test_expanded_pool_and_ten_position_limit(self):
        self.assertEqual(len(a.CODES), 100)
        self.assertEqual(len(set(a.CODES)), 100)
        now = datetime(2026, 10, 19, 10, tzinfo=NY)
        positions = [
            dict(code=code, qty=1, position_side="LONG", cost_price=100, market_val=100)
            for code in a.CODES[:10]
        ]
        self.regular(now, positions)
        self.e.state["orders"] = [
            dict(
                key=code,
                remark=code,
                symbol=code,
                side="BUY",
                kind="ENTRY",
                qty=1,
                filled=1,
                status="FILLED_ALL",
                day=str(now.date()),
                created=now.isoformat(),
            )
            for code in a.CODES[:10]
        ]
        self.q.get_market_snapshot.return_value = (0, pd.DataFrame([]))
        self.assertEqual(self.e.cycle(now)["status"], "POSITION_LIMIT")
        self.t.place_order.assert_not_called()

    def test_old_scanner_row_never_opens_position(self):
        now = datetime(2026, 10, 19, 10, tzinfo=NY)
        row = self.candidate_cycle(now)
        row["updated"] = (now - timedelta(minutes=2)).isoformat()
        a.write(self.directory / "quant_lab.brokers.moomoo.market.json", dict(rows=[row]))
        with patch("quant_lab.brokers.moomoo.execution.fresh_quote", return_value=True):
            self.assertEqual(self.e.cycle(now)["status"], "WATCHING")
        self.t.place_order.assert_not_called()

    def test_daily_counter_not_truncated_with_recent_order_payload(self):
        now = datetime(2026, 10, 19, 10, tzinfo=NY)
        self.regular(
            now,
            [dict(code="US.AAPL", qty=300, position_side="LONG", cost_price=100, market_val=30000)],
        )
        self.q.get_market_snapshot.return_value = (
            0,
            pd.DataFrame(
                [
                    dict(
                        code="US.AAPL",
                        bid_price=100,
                        ask_price=100.05,
                        update_time=now.strftime("%Y-%m-%d %H:%M:%S"),
                    )
                ]
            ),
        )
        self.e.state["orders"] = [
            dict(
                key=str(i),
                remark=str(i),
                symbol="US.AAPL",
                side="BUY",
                kind="ENTRY",
                qty=1,
                filled=1,
                status="FILLED_ALL",
                day=str(now.date()) if i >= 200 else "2026-10-16",
                created=now.isoformat(),
            )
            for i in range(300)
        ]
        report = self.e.cycle(now)
        self.assertEqual(report["status"], "DAILY_ENTRY_LIMIT")
        self.assertEqual(report["daily_entries_used"], 100)
        self.assertEqual(len(report["orders"]), 250)
