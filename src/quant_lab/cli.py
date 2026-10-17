"""One public command surface; imports broker code only for the chosen command."""

import argparse
import json
import os
import sys
from importlib import import_module
from pathlib import Path

from quant_lab import __version__

COMMANDS = {
    "review": ("quant_lab.services.review", "Periodic bounded local strategy review"),
    "migrate-container": ("quant_lab.services.migration", "Copy a stopped private PAPER runtime"),
    "container": ("quant_lab.services.container", "Activated PAPER-only container runtime"),
    "demo": ("quant_lab.dashboard.demo", "Synthetic UI; no broker connection"),
    "dashboard": ("quant_lab.dashboard.web", "Local broker-connected dashboard; reads only"),
    "desktop": ("quant_lab.dashboard.native", "Optional Tk desktop window"),
    "probe": ("quant_lab.brokers.discovery", "Read-only broker discovery"),
    "preview": ("quant_lab.brokers.alpaca.preview", "Read-only Alpaca preview"),
    "alpaca-session": (
        "quant_lab.brokers.alpaca.execution",
        "Explicitly authorized single paper session",
    ),
    "run-moomoo": (
        "quant_lab.brokers.moomoo.execution",
        "Explicitly authorized SIMULATE execution",
    ),
    "scan-alpaca": ("quant_lab.strategies.scanner", "Read-only stock scanner"),
    "scan-moomoo": ("quant_lab.brokers.moomoo.market", "Read-only minute scanner"),
    "observe-moomoo": ("quant_lab.brokers.moomoo.observer", "Read-only account observer"),
    "watch-options": ("quant_lab.brokers.moomoo.options_watch", "Read-only option snapshots"),
    "cleanup-moomoo": ("quant_lab.brokers.moomoo.cleanup", "Separately authorized paper cleanup"),
    "optimize": ("quant_lab.analytics.daily", "Closed-session review and bounded evaluation"),
}


def invoke(module: str, arguments: list[str]) -> int:
    """Preserve each tested worker parser while providing one stable top-level CLI."""
    previous = sys.argv
    sys.argv = ["quant-lab " + module.rsplit(".", 1)[-1], *arguments]
    try:
        return import_module(module).main() or 0
    finally:
        sys.argv = previous


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stock Quant Lab — educational paper trading only")
    parser.add_argument(
        "--home", type=Path, help="Writable local runtime home; never a broker endpoint"
    )
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    sub = parser.add_subparsers(dest="command")
    status = sub.add_parser("status", help="Read local service status; no broker requests")
    status.add_argument("--json", action="store_true", help="Print structured status")
    doctor = sub.add_parser("doctor", help="Read-only installation and configuration diagnostics")
    doctor.add_argument("--json", action="store_true")
    sub.add_parser("supervise", help="Worker supervision; respects local authorization")
    alpaca = sub.add_parser(
        "run-alpaca", help="Continuous, explicitly authorized Alpaca PAPER manager"
    )
    alpaca.add_argument("--execute-paper", required=True, action="store_true")
    for name, (_, description) in COMMANDS.items():
        # The worker owns its detailed flags and --help; no flag is silently dropped.
        sub.add_parser(name, help=description, add_help=False)
    args, remainder = parser.parse_known_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    if args.home is not None:
        os.environ["QUANT_LAB_HOME"] = str(args.home.expanduser().resolve())
    from quant_lab.runtime.paths import project_home

    home = project_home()
    if args.command == "doctor":
        if remainder:
            parser.error("Unrecognized diagnostic arguments")
        from quant_lab.runtime.doctor import diagnose

        result = diagnose(home)
        if args.json:
            print(json.dumps(result))
        else:
            print("PAPER ONLY · Installation diagnostics")
            for name, passed in result["checks"].items():
                print(f"{'OK' if passed else 'ABSENT/INVALID'} {name}")
        return 0 if result["checks"]["runtime_settings_valid"] else 1
    if args.command == "status":
        if remainder:
            parser.error("Unrecognized status arguments")
        from quant_lab.services.automation import public_status

        result = public_status(home)
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print("PAPER ONLY · " + result["status"])
            for broker in ("alpaca", "moomoo"):
                print(f"{broker}: {result[broker].get('status', 'NOT_RUNNING')}")
        return 0
    from quant_lab.runtime.logging import service_logger

    logger = service_logger(home, args.command)
    logger.info("command_started", extra={"command": args.command})
    try:
        if args.command in {
            "supervise",
            "run-alpaca",
            "alpaca-session",
            "run-moomoo",
            "cleanup-moomoo",
        }:
            from quant_lab.runtime.ownership import require_execution_owner

            require_execution_owner(home)
        if args.command in {"supervise", "run-alpaca"}:
            if remainder:
                parser.error("Unrecognized service arguments")
            from quant_lab.services.automation import run_alpaca, run_supervisor

            (run_supervisor if args.command == "supervise" else run_alpaca)(home)
            return 0
        return invoke(COMMANDS[args.command][0], remainder)
    except KeyboardInterrupt:
        logger.info("command_interrupted", extra={"command": args.command})
        return 130
    except Exception as exc:
        logger.error(
            "command_failed", extra={"command": args.command, "error_type": type(exc).__name__}
        )
        print(json.dumps({"status": "STOP", "error_type": type(exc).__name__}), file=sys.stderr)
        return 1
    finally:
        logger.info("command_finished", extra={"command": args.command})
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
