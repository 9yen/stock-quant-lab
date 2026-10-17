"""Container health is worker liveness + supervisor heartbeat, not trading profit."""

import json
import urllib.request

from quant_lab.runtime.paths import project_home
from quant_lab.services.automation import fresh, inventory


def healthy(root):
    try:
        report = json.loads((root / "state/automation_status.json").read_text())
        if report.get("status") != "RUNNING" or not fresh(report, seconds=90):
            return False
        rows = report.get("workers", [])
        running = inventory(root)
        if not rows or any(not running.get(row["name"]) for row in rows):
            return False
        with urllib.request.urlopen("http://127.0.0.1:8766/api/automation", timeout=3) as r:
            return r.status == 200
    except (OSError, ValueError, KeyError):
        return False


if __name__ == "__main__":
    raise SystemExit(0 if healthy(project_home()) else 1)
