import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

from quant_lab.services.automation import (
    AlpacaService,
    Supervisor,
    fresh,
    public_status,
    renew_authorization,
    session_plan,
    write,
)
from quant_lab.strategies.intraday import NY
from quant_lab.strategies.scanner import UNIVERSE


class AutomationTests(unittest.TestCase):
    def test_supervisor_registry_reaches_the_real_packaged_launcher(self):
        from unittest.mock import patch

        from quant_lab.services.automation import launch

        for name in (".venv", ".venv-brokers"):
            interpreter = self.root / name / "Scripts" / "python.exe"
            interpreter.parent.mkdir(parents=True)
            interpreter.touch()
        with patch(
            "quant_lab.services.automation.subprocess.Popen", return_value=self.start.return_value
        ) as popen:
            report = Supervisor(self.root, launch, self.processes).step()
        self.assertTrue(all(row["state"] == "STARTING" for row in report["workers"]))
        self.assertEqual(popen.call_count, 6)
        commands = [call.args[0] for call in popen.call_args_list]
        self.assertTrue(all(command[2:4] == ["-m", "quant_lab"] for command in commands))
        self.assertTrue(
            any("run-moomoo" in command and "--execute-paper" in command for command in commands)
        )

    def setUp(self):
        from unittest.mock import patch

        # Tests below describe native supervision, independent of the test runner's container.
        environment = patch.dict("os.environ", {"QUANT_LAB_CONTAINER": "0"})
        environment.start()
        self.addCleanup(environment.stop)
        scratch = Path(__file__).resolve().parents[1] / "state" / "test-scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = TemporaryDirectory(dir=scratch)
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 10, 19, 10, tzinfo=NY)
        self.opened = self.now.replace(hour=9, minute=30)
        self.closed = self.now.replace(hour=16, minute=0)
        self.config = dict(
            alpaca_enabled=True,
            moomoo_enabled=True,
            recurring_paper_authorized=True,
            alpaca_account_id="paper-private",
        )
        self.auth = dict(
            account_id="paper-private",
            authorized=True,
            paper_only=True,
            session="2026-10-16",
            strategy="multi_1m",
            universe=list(UNIVERSE),
            max_position=0.4,
            gross_limit=1.0,
            max_positions=10,
            target_position=0.1,
            strategy_daily_loss=0.01,
            allow_short=True,
        )
        write(self.root / "state" / "automation_config.json", self.config)
        write(self.root / "state" / "paper_authorization.json", self.auth)
        self.client = MagicMock()
        self.client._base_url = "https://paper-api.alpaca.markets"
        self.client.get_account.return_value = NS(id="paper-private")
        self.client.get_clock.return_value = NS(timestamp=self.now, next_open=self.opened)
        self.client.get_all_positions.return_value = []
        self.client.get_orders.return_value = []
        self.client.get_calendar.return_value = [NS(open=self.opened, close=self.closed)]
        self.start = MagicMock(return_value=NS(pid=123, poll=lambda: None))
        self.processes = MagicMock(return_value={})
        self.service = AlpacaService(self.root, self.client, self.start, self.processes)

    def tearDown(self):
        self.temp.cleanup()

    def test_new_session_reuses_policy_without_resetting_risk_files(self):
        write(self.root / "state" / "intraday.json", {"halted": True, "day": "2026-10-16"})
        before = (self.root / "state" / "intraday.json").read_bytes()
        renewed = renew_authorization(self.root, self.client, self.config, self.auth, "2026-10-19")
        self.assertEqual(renewed, self.auth | {"session": "2026-10-19"})
        self.assertEqual((self.root / "state" / "intraday.json").read_bytes(), before)
        self.assertTrue(
            (self.root / "state" / "paper_authorization_history" / "2026-10-16.json").exists()
        )
        self.client.submit_order.assert_not_called()

    def test_same_day_restart_does_not_overwrite_authorization_or_halts(self):
        same = self.auth | {"session": "2026-10-19"}
        before = (self.root / "state" / "paper_authorization.json").read_bytes()
        self.assertEqual(
            renew_authorization(self.root, self.client, self.config, same, "2026-10-19"), same
        )
        self.assertEqual((self.root / "state" / "paper_authorization.json").read_bytes(), before)

    def test_no_live_account_switch_or_unapproved_recurring_authorization(self):
        for setting in ("alpaca_enabled", "recurring_paper_authorized", "alpaca_account_id"):
            with self.assertRaises(ValueError):
                renew_authorization(
                    self.root, self.client, self.config | {setting: False}, self.auth, "2026-10-19"
                )
        self.client._base_url = "https://api.alpaca.markets"
        with self.assertRaises(ValueError):
            self.service.step()
        self.start.assert_not_called()

    def test_previous_holdings_orders_and_unknown_journal_block_rollover(self):
        for method in ("get_all_positions", "get_orders"):
            getattr(self.client, method).return_value = [NS()]
            with self.assertRaises(ValueError):
                renew_authorization(self.root, self.client, self.config, self.auth, "2026-10-19")
            getattr(self.client, method).return_value = []
        write(
            self.root / "state" / "paper_intents_2026-10-16.json",
            dict(
                account_id="paper-private",
                session="2026-10-16",
                orders={"cid": {"status": "unknown"}},
            ),
        )
        self.client.get_order_by_client_id.return_value = NS(status="pending_new")
        with self.assertRaises(ValueError):
            renew_authorization(self.root, self.client, self.config, self.auth, "2026-10-19")
        self.client.get_order_by_client_id.return_value = NS(status="filled")
        self.assertEqual(
            renew_authorization(self.root, self.client, self.config, self.auth, "2026-10-19")[
                "session"
            ],
            "2026-10-19",
        )

    def test_holiday_and_weekend_wait_without_renewing(self):
        self.client.get_calendar.return_value = []
        self.assertEqual(self.service.step()["status"], "WAIT_TRADING_DAY")
        self.start.assert_not_called()
        self.client.get_all_positions.return_value = [NS()]
        self.assertEqual(self.service.step()["status"], "OVERNIGHT_POSITION_HOLD")

    def test_open_launches_once_with_current_broker_date(self):
        self.service.step()
        self.start.assert_called_once_with(
            self.root, "paper_intraday.py", ("--execute-paper", "--session", "2026-10-19")
        )
        self.processes.return_value = {"paper_intraday.py": [123]}
        self.service.step()
        self.assertEqual(self.start.call_count, 1)

    def test_crash_retries_have_backoff(self):
        self.service.step()
        self.service.step()
        self.assertEqual(self.start.call_count, 1)

    def test_stop_and_completed_day_are_not_overridden(self):
        (self.root / "STOP_PAPER").touch()
        self.assertEqual(self.service.step()["status"], "MANUAL_STOP")
        self.assertTrue((self.root / "STOP_PAPER").exists())
        self.start.assert_not_called()
        (self.root / "STOP_PAPER").unlink()
        self.root.joinpath("logs").mkdir()
        self.root.joinpath("logs/paper_execution.jsonl").write_text(
            '{"event":"CYCLE","session":"2026-10-19","status":"FLAT","flat":true,"done":true}\n'
        )
        self.assertEqual(self.service.step()["status"], "SESSION_COMPLETE")
        self.start.assert_not_called()

    def test_running_manual_executor_prevents_auth_rollover(self):
        self.processes.return_value = {"paper_intraday.py": [444]}
        self.assertEqual(self.service.step()["status"], "EXECUTOR_STARTING_OR_STALE")
        self.start.assert_not_called()
        self.assertIn("2026-10-16", (self.root / "state" / "paper_authorization.json").read_text())

    def test_half_day_entry_cutoff_and_overnight_hold(self):
        early_close = self.closed.replace(hour=13)
        self.assertEqual(
            session_plan(
                early_close - timedelta(minutes=9),
                self.opened,
                early_close,
                True,
                False,
                False,
                False,
            ),
            "SESSION_COMPLETE",
        )
        self.assertEqual(
            session_plan(early_close, self.opened, early_close, False, False, False, True),
            "OVERNIGHT_POSITION_HOLD",
        )
        self.assertEqual(
            session_plan(
                self.opened - timedelta(minutes=16),
                self.opened,
                early_close,
                True,
                False,
                False,
                False,
            ),
            "WAIT_REGULAR_SESSION",
        )

    def test_supervisor_adopts_workers_and_restarts_only_absent_workers(self):
        names = [
            "web_dashboard.py",
            "alpaca_auto.py",
            "moomoo_market.py",
            "moomoo_observer.py",
            "moomoo_options_watch.py",
            "moomoo_auto.py",
        ]
        self.processes.return_value = {n: [100, 101] for n in names}
        supervisor = Supervisor(self.root, self.start, self.processes)
        self.assertTrue(all(r["state"] == "RUNNING" for r in supervisor.step()["workers"]))
        self.start.assert_not_called()
        self.processes.return_value.pop("moomoo_auto.py")
        supervisor.step()
        self.start.assert_called_once_with(self.root, "moomoo_auto.py", ("--execute-paper",), True)
        supervisor.step()
        self.assertEqual(self.start.call_count, 1)

    def test_failed_inventory_cannot_spawn_duplicate_workers(self):
        self.processes.side_effect = RuntimeError("Unavailable")
        with self.assertRaises(RuntimeError):
            Supervisor(self.root, self.start, self.processes).step()
        self.start.assert_not_called()

    def test_public_endpoint_has_no_account_identifiers(self):
        now = datetime.now(timezone.utc)
        write(
            self.root / "state" / "alpaca_auto_status.json",
            dict(
                updated=now.isoformat(),
                status="WAIT_TRADING_DAY",
                secret="private",
                account_id="private",
            ),
        )
        public = str(public_status(self.root))
        self.assertNotIn("paper-private", public)
        self.assertNotIn("secret", public)
        self.assertTrue(public_status(self.root)["alpaca"]["fresh"])
        self.assertFalse(fresh({"updated": (now - timedelta(seconds=100)).isoformat()}, now))
        self.assertFalse(fresh({"updated": (now + timedelta(seconds=100)).isoformat()}, now))
