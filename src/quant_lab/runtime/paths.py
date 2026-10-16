"""Separate writable account state from installed application resources."""

import os
from pathlib import Path


def project_home() -> Path:
    """Use an explicit home, the source checkout, or a user-writable installed home."""
    override = os.environ.get("QUANT_LAB_HOME")
    if override:
        return Path(override).expanduser().resolve()
    checkout = Path(__file__).resolve().parents[3]
    if (checkout / "pyproject.toml").is_file() and (checkout / "src" / "quant_lab").is_dir():
        return checkout
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share")))
    return base / "StockQuantLab"


def web_assets(home: Path | None = None) -> Path:
    """A temporary demo/test may override resources; normal assets ship inside the wheel."""
    if home is not None and (home / "web").is_dir():
        return home / "web"
    return Path(__file__).resolve().parents[1] / "dashboard" / "assets"
