"""Read-only installation diagnostics; no broker requests or credential output."""

from importlib.util import find_spec
from pathlib import Path

from .paths import web_assets
from .settings import load_settings


def diagnose(home: Path) -> dict:
    checks = {
        "packaged_web_assets": (web_assets() / "index.html").is_file(),
        "english_catalog": (web_assets() / "locales" / "en.json").is_file(),
        "chinese_catalog": (web_assets() / "locales" / "zh-CN.json").is_file(),
        "alpaca_sdk_installed": find_spec("alpaca") is not None,
        "moomoo_sdk_installed": find_spec("moomoo") is not None,
        "local_credentials_present": (home / ".env").is_file(),
        "local_automation_config_present": (home / "state" / "automation_config.json").is_file(),
    }
    try:
        load_settings(home)
        checks["runtime_settings_valid"] = True
    except (ValueError, OSError):
        checks["runtime_settings_valid"] = False
    return {
        "mode": "PAPER_ONLY",
        "home": str(home),
        "checks": checks,
        "note": "Optional SDKs and account authorization are separate; no broker connection was made.",
    }
