"""Shared atomic JSON persistence, with an explicit durability policy."""

import json
import os
import uuid
from pathlib import Path
from typing import Any


def write_json(path: Path, value: Any, *, durable: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            if durable:
                os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
