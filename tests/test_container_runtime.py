import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from quant_lab.runtime.gateway import opend_address
from quant_lab.runtime.locking import acquire, release
from quant_lab.runtime.ownership import require_execution_owner
from quant_lab.runtime.settings import load_settings
from quant_lab.services.container import require_activation
from quant_lab.services.migration import prepare


class ContainerRuntimeTests(unittest.TestCase):
    def test_migration_preserves_risk_and_stops_without_copying_activation(self):
        with TemporaryDirectory() as folder:
            source, target = Path(folder) / "native", Path(folder) / "private"
            (source / "state").mkdir(parents=True)
            (source / "state/automation_config.json").write_text("{}")
            ledger = b'{"entry_disabled":true,"loss_budget_used":42}'
            (source / "state/ledger.json").write_bytes(ledger)
            (source / "STOP_PAPER").write_text("STOP")
            (source / "state/container_authorization.json").write_text("untrusted old marker")
            with patch("quant_lab.services.migration.inventory", return_value={}):
                prepare(source, target)
            self.assertEqual((target / "state/ledger.json").read_bytes(), ledger)
            self.assertTrue((target / "STOP_PAPER").is_file())
            require_activation(target)
            self.assertEqual(
                json.loads((target / "state/automation_config.json").read_text())["startup"],
                "DOCKER_COMPOSE",
            )
            with patch.dict(os.environ, {"QUANT_LAB_CONTAINER": "0"}):
                with self.assertRaises(RuntimeError):
                    require_execution_owner(source)
            self.assertEqual(
                (source / "state/container_authorization.json").read_text(), "untrusted old marker"
            )

    def test_migration_blocks_running_native_workers_and_existing_destination(self):
        with TemporaryDirectory() as folder:
            source, target = Path(folder) / "native", Path(folder) / "private"
            (source / "state").mkdir(parents=True)
            (source / "state/automation_config.json").write_text("{}")
            with patch("quant_lab.services.migration.inventory", return_value={"worker": [1]}):
                with self.assertRaises(RuntimeError):
                    prepare(source, target)
            self.assertFalse(target.exists())
            with self.assertRaises(ValueError):
                prepare(source, source / "private")

    def test_lock_excludes_another_process_and_releases_after_exit(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "worker.lock"
            child = (
                "from pathlib import Path; from quant_lab.runtime.locking import acquire; "
                f"h=Path({str(path)!r}).open('a+b'); acquire(h)"
            )
            with path.open("a+b") as handle:
                acquire(handle)
                self.assertNotEqual(
                    subprocess.run([sys.executable, "-c", child], capture_output=True).returncode, 0
                )
                release(handle)
                self.assertEqual(
                    subprocess.run([sys.executable, "-c", child], capture_output=True).returncode, 0
                )

    def test_gateway_defaults_and_explicit_override(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(opend_address(), ("127.0.0.1", 11111))
        with patch.dict(os.environ, {"QUANT_LAB_OPEND_HOST": "host.docker.internal"}):
            self.assertEqual(opend_address()[0], "host.docker.internal")
        for port in ("0", "65536", "invalid"):
            with patch.dict(os.environ, {"QUANT_LAB_OPEND_PORT": port}):
                with self.assertRaises(ValueError):
                    opend_address()

    def test_migrated_native_cli_cannot_restart_execution(self):
        from contextlib import redirect_stderr
        from io import StringIO

        from quant_lab.cli import main

        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "state").mkdir()
            (root / "state/container_migrated.json").write_text("{}")
            with patch.dict(os.environ, {"QUANT_LAB_CONTAINER": "0"}):
                with patch("quant_lab.cli.invoke") as invoke:
                    with redirect_stderr(StringIO()):
                        self.assertEqual(
                            main(["--home", str(root), "run-moomoo", "--execute-paper"]), 1
                        )
                    invoke.assert_not_called()

    def test_container_activation_never_infers_permission_from_broker_config(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "state").mkdir()
            with self.assertRaises(FileNotFoundError):
                require_activation(root)
            path = root / "state/container_authorization.json"
            path.write_text(json.dumps({"mode": "LIVE"}))
            with self.assertRaises(ValueError):
                require_activation(root)
            path.write_text(
                json.dumps(
                    {"schema_version": 1, "mode": "PAPER_ONLY", "native_workers_stopped": True}
                )
            )
            require_activation(root)

    def test_container_bind_is_explicit_and_does_not_change_native_default(self):
        with TemporaryDirectory() as folder:
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(load_settings(Path(folder)).web_host, "127.0.0.1")
            with patch.dict(os.environ, {"QUANT_LAB_CONTAINER": "1"}):
                self.assertEqual(load_settings(Path(folder)).web_host, "0.0.0.0")
            with patch.dict(
                os.environ, {"QUANT_LAB_CONTAINER": "1", "QUANT_LAB_HOST_NETWORK": "1"}
            ):
                self.assertEqual(load_settings(Path(folder)).web_host, "127.0.0.1")

    @unittest.skipIf(os.name == "nt", "Linux /proc integration")
    def test_inventory_adopts_only_the_selected_runtime_home(self):
        from quant_lab.services.automation import posix_inventory

        with TemporaryDirectory() as folder:
            root = Path(folder)
            child = subprocess.Popen(
                [sys.executable, "-m", "quant_lab", "--home", str(root), "dashboard"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                # cmdline exists as soon as exec completes; retry only the startup transition.
                import time

                found = {}
                for _ in range(30):
                    found = posix_inventory(root)
                    if child.pid in found.get("web_dashboard.py", []):
                        break
                    time.sleep(0.01)
                self.assertIn(child.pid, found.get("web_dashboard.py", []))
                self.assertEqual(posix_inventory(root / "other"), {})
            finally:
                child.terminate()
                child.wait(timeout=10)
