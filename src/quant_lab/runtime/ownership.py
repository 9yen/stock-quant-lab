"""Prevent reactivating a stale native runtime after container cutover."""

import os


def require_execution_owner(root):
    if (
        os.environ.get("QUANT_LAB_CONTAINER") != "1"
        and (root / "state/container_migrated.json").exists()
    ):
        raise RuntimeError("Native execution is disabled after Docker migration")
