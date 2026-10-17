"""Configurable OpenD endpoint. No login, unlock or network configuration changes."""

import os


def opend_address():
    host = os.environ.get("QUANT_LAB_OPEND_HOST", "127.0.0.1").strip()
    port = int(os.environ.get("QUANT_LAB_OPEND_PORT", "11111"))
    if not host or "/" in host or "://" in host or not 1 <= port <= 65535:
        raise ValueError("Invalid OpenD endpoint")
    return host, port


def opend_kwargs():
    host, port = opend_address()
    return {"host": host, "port": port}
