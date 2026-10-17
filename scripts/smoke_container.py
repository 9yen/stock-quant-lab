"""Exercise the real container supervisor with synthetic state and no network."""

import argparse
import json
import subprocess
import time
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory


def docker(*args, check=True):
    return subprocess.run(
        ["docker", *args], check=check, capture_output=True, text=True, timeout=30
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="stock-quant-lab:paper")
    args = parser.parse_args()
    name = "quant-lab-smoke-" + uuid.uuid4().hex[:10]
    with TemporaryDirectory(prefix="quant-lab-synthetic-") as folder:
        root = Path(folder)
        # Only this account-free, disconnected synthetic fixture is shared across UIDs.
        # Real runtime directories must remain private to the operator/container UID.
        root.chmod(0o777)
        (root / "state").mkdir()
        (root / "state").chmod(0o777)
        (root / ".env").touch()
        config = {"alpaca_enabled": False, "moomoo_enabled": False, "review_enabled": False}
        (root / "state/automation_config.json").write_text(json.dumps(config))
        (root / "state/container_authorization.json").write_text(
            json.dumps({"schema_version": 1, "mode": "PAPER_ONLY", "native_workers_stopped": True})
        )
        ledger = b'{"entry_disabled":true,"synthetic_risk_lock":42}'
        (root / "state/synthetic_ledger.json").write_bytes(ledger)
        try:
            docker(
                "run",
                "-d",
                "--name",
                name,
                "--network",
                "none",
                "--read-only",
                "--tmpfs",
                "/tmp",
                "--mount",
                f"type=bind,src={root},dst=/data",
                args.image,
            )
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if (
                    docker(
                        "exec", name, "python", "-m", "quant_lab.services.healthcheck", check=False
                    ).returncode
                    == 0
                ):
                    break
                time.sleep(1)
            else:
                raise RuntimeError("Synthetic runtime did not become healthy")
            duplicate = docker(
                "exec", name, "quant-lab", "container", "--execute-paper", check=False
            )
            assert duplicate.returncode == 1 and "RuntimeError" in duplicate.stderr
            kill = "import os,signal; from pathlib import Path; from quant_lab.services.automation import inventory; pid=inventory(Path('/data'))['web_dashboard.py'][0]; os.kill(pid,signal.SIGTERM); print(pid)"
            old = int(docker("exec", name, "python", "-c", kill).stdout.strip())
            query = "from pathlib import Path; from quant_lab.services.automation import inventory; print(inventory(Path('/data')).get('web_dashboard.py',[0])[0])"
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                new = int(docker("exec", name, "python", "-c", query).stdout.strip())
                if (
                    new
                    and new != old
                    and docker(
                        "exec", name, "python", "-m", "quant_lab.services.healthcheck", check=False
                    ).returncode
                    == 0
                ):
                    break
                time.sleep(1)
            else:
                raise RuntimeError("Supervisor did not recover the exited synthetic worker")
            assert (root / "state/synthetic_ledger.json").read_bytes() == ledger
            print(
                "PASS: full runtime health, duplicate lock, exited-worker recovery, unchanged risk ledger; no network or accounts"
            )
        finally:
            docker("rm", "-f", name, check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
