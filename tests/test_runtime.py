"""Behavior at the new package/configuration/storage boundaries."""

import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from quant_lab.cli import main
from quant_lab.runtime.paths import project_home, web_assets
from quant_lab.runtime.settings import load_settings
from quant_lab.runtime.storage import write_json


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def config(self, text):
        folder = self.root / "config"
        folder.mkdir(exist_ok=True)
        (folder / "local.toml").write_text(text, encoding="utf-8")

    def test_explicit_home_does_not_depend_on_working_directory(self):
        with patch.dict(os.environ, {"QUANT_LAB_HOME": str(self.root)}):
            self.assertEqual(project_home(), self.root.resolve())
        self.assertTrue((web_assets() / "locales/en.json").is_file())

    def test_loopback_settings_and_environment_port_override(self):
        self.config('schema_version=1\n[web]\nhost="127.0.0.1"\nport=8877\n')
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(load_settings(self.root).web_port, 8877)
        with patch.dict(os.environ, {"QUANT_LAB_WEB_PORT": "8878"}):
            self.assertEqual(load_settings(self.root).web_port, 8878)

    def test_public_host_unknown_fields_and_invalid_port_fail_closed(self):
        for text in (
            '[web]\nhost="0.0.0.0"',
            "[web]\nport=0",
            "[web]\nport=true",
            "[web]\nport=1.5",
            "schema_version=2",
            "allow_live=true",
        ):
            self.config(text)
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(ValueError):
                    load_settings(self.root)

    def test_failed_replace_keeps_old_ledger_and_removes_temporary_file(self):
        target = self.root / "ledger.json"
        write_json(target, {"orders": ["original"]})
        with patch("quant_lab.runtime.storage.os.replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                write_json(target, {"orders": ["new"]})
        self.assertEqual(json.loads(target.read_text()), {"orders": ["original"]})
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_nan_is_rejected_without_destroying_previous_state(self):
        target = self.root / "ledger.json"
        write_json(target, {"cash": 100})
        with self.assertRaises(ValueError):
            write_json(target, {"cash": float("nan")})
        self.assertEqual(json.loads(target.read_text()), {"cash": 100})

    def test_read_only_status_works_outside_checkout_without_creating_state(self):
        result = subprocess.run(
            [sys.executable, "-m", "quant_lab", "--home", str(self.root), "status", "--json"],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(json.loads(result.stdout)["status"], "NOT_RUNNING")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_web_backend_has_no_tk_or_broker_sdk_import_side_effect(self):
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import quant_lab.dashboard.web; "
                "assert 'tkinter' not in sys.modules; assert 'alpaca' not in sys.modules; "
                "assert 'moomoo' not in sys.modules",
            ],
            cwd=self.root,
            check=True,
        )

    def test_cli_preserves_explicit_paper_execution_flag(self):
        output = StringIO()
        with redirect_stderr(output):
            with self.assertRaises(SystemExit) as error:
                main(["run-alpaca"])
        self.assertEqual(error.exception.code, 2)

    def test_service_errors_never_print_raw_broker_exception_or_secret(self):
        stderr = StringIO()
        with patch.dict(os.environ, {"QUANT_LAB_HOME": str(self.root)}):
            with patch("quant_lab.cli.invoke", side_effect=RuntimeError("SECRET-DO-NOT-LOG")):
                with redirect_stderr(stderr):
                    self.assertEqual(main(["dashboard"]), 1)
        self.assertNotIn("SECRET-DO-NOT-LOG", stderr.getvalue())
        log = (self.root / "logs/services.dashboard.jsonl").read_text()
        self.assertIn("RuntimeError", log)
        self.assertNotIn("SECRET-DO-NOT-LOG", log)

    def test_doctor_discloses_presence_only_not_credential_contents(self):
        (self.root / ".env").write_text("ALPACA_SECRET=SECRET-DO-NOT-LOG")
        output = StringIO()
        with patch.dict(os.environ, {"QUANT_LAB_HOME": str(self.root)}):
            with redirect_stdout(output):
                self.assertEqual(main(["doctor", "--json"]), 0)
        self.assertNotIn("SECRET-DO-NOT-LOG", output.getvalue())
        self.assertTrue(json.loads(output.getvalue())["checks"]["local_credentials_present"])
