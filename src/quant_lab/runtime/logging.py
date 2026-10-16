"""Bounded structured service logs without raw broker exception details."""

import json
import logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path


class ServiceFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "level": record.levelname,
                "event": record.getMessage(),
                "command": getattr(record, "command", None),
                "error_type": getattr(record, "error_type", None),
            }
        )


def service_logger(home: Path) -> logging.Logger:
    logger = logging.getLogger("quant_lab.service")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    folder = home / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        folder / "services.jsonl", maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(ServiceFormatter())
    logger.addHandler(handler)
    return logger
