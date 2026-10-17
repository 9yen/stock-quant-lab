"""Explicit container activation gate; missing state never authorizes trading."""

import argparse
import json

from quant_lab.runtime.paths import project_home


def require_activation(root):
    marker = json.loads((root / "state/container_authorization.json").read_text())
    if marker != {"schema_version": 1, "mode": "PAPER_ONLY", "native_workers_stopped": True}:
        raise ValueError("Container migration is not authorized")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-paper", action="store_true", required=True)
    parser.parse_args()
    root = project_home()
    require_activation(root)
    from quant_lab.services.automation import run_supervisor

    run_supervisor(root)
