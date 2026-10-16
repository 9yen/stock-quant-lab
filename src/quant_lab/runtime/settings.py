"""Validated non-secret runtime settings. Broker authorization remains local state."""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .paths import project_home


@dataclass(frozen=True)
class RuntimeSettings:
    home: Path
    web_host: str = "127.0.0.1"
    web_port: int = 8766


def load_settings(home: Path | None = None) -> RuntimeSettings:
    selected = home or project_home()
    path = selected / "config" / "local.toml"
    data = tomllib.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
    unknown = set(data) - {"schema_version", "web"}
    if unknown or data.get("schema_version", 1) != 1:
        raise ValueError("Unsupported runtime settings schema")
    web = data.get("web", {})
    if not isinstance(web, dict) or set(web) - {"host", "port"}:
        raise ValueError("Unsupported web settings")
    host = web.get("host", "127.0.0.1")
    if host != "127.0.0.1":
        raise ValueError("The dashboard must bind to loopback")
    raw_port = os.environ.get("QUANT_LAB_WEB_PORT", web.get("port", 8766))
    if isinstance(raw_port, (bool, float)):
        raise ValueError("Invalid dashboard port type")
    port = int(raw_port)
    if not 1 <= port <= 65535:
        raise ValueError("Invalid dashboard port")
    return RuntimeSettings(home=selected, web_host=host, web_port=port)
