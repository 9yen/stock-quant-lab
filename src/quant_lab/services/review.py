"""Periodic bounded review. Auto-promotion requires explicit existing local policy."""

import time
from datetime import datetime, timezone

from quant_lab.runtime.paths import project_home
from quant_lab.runtime.storage import write_json
from quant_lab.services.automation import InstanceLock


def main():
    from quant_lab.analytics.daily import run
    from quant_lab.dashboard.data import read_json

    root = project_home()
    with InstanceLock(root / "state/review.lock"):
        while True:
            config = read_json(root / "state/automation_config.json", {})
            try:
                report = run(root, auto_apply=config.get("optimizer_auto_apply") is True)
                status = {"status": report.get("status", "REVIEWED")}
            except Exception as exc:
                status = {"status": "REVIEW_HOLD", "error_type": type(exc).__name__}
            write_json(
                root / "state/review_status.json",
                status | {"updated": datetime.now(timezone.utc).isoformat()},
            )
            time.sleep(1800)
