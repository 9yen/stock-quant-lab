"""Copy a stopped private runtime without publishing credentials or resetting risk."""

import argparse
import shutil
from pathlib import Path

from quant_lab.runtime.storage import write_json
from quant_lab.services.automation import inventory


def prepare(source, destination):
    source, destination = source.resolve(), destination.resolve()
    if (
        destination.exists()
        or destination.is_relative_to(source)
        or source.is_relative_to(destination)
    ):
        raise ValueError("Use a new private runtime outside the source directory")
    if any((parent / ".git").exists() for parent in (destination, *destination.parents)):
        raise ValueError("Private runtime must not be inside a Git repository")
    if not (source / "state/automation_config.json").is_file():
        raise ValueError("Existing local automation configuration required")
    if inventory(source):
        raise RuntimeError("Native workers must be stopped before migration")
    names = (".env", "state", "logs", "config", "STOP_PAPER", "STOP_MOOMOO")
    selected = [source / name for name in names if (source / name).exists()]
    for path in selected:
        if path.is_symlink() or path.is_dir() and any(p.is_symlink() for p in path.rglob("*")):
            raise ValueError("Runtime symlinks require manual review")
    destination.mkdir(parents=True, mode=0o700)
    for path in selected:
        target = destination / path.name
        if path.is_dir():
            shutil.copytree(
                path, target, ignore=shutil.ignore_patterns("container_authorization.json")
            )
        else:
            shutil.copy2(path, target)
    # Never trust activation copied from an earlier migration.
    marker = destination / "state/container_authorization.json"
    marker.unlink(missing_ok=True)
    if inventory(source):
        raise RuntimeError("Native workers restarted; copied runtime remains unactivated")
    write_json(marker, {"schema_version": 1, "mode": "PAPER_ONLY", "native_workers_stopped": True})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--activate-paper", action="store_true", required=True)
    args = parser.parse_args()
    try:
        prepare(args.source, args.destination)
    except Exception as exc:
        print(f"Migration blocked: {type(exc).__name__}; no sensitive values printed")
        return 1
    print("Private runtime copied and activated for PAPER only; risk records preserved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
