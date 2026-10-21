"""Read-only IEX minute cache shared by scanners; never broker authorization."""

import re
from datetime import datetime

from quant_lab.runtime.storage import write_json


def publish(root, symbol, bars, recorded_at):
    if not re.fullmatch(r"[A-Z]{1,5}", symbol):
        raise ValueError("Invalid cache symbol")
    write_json(
        root / "state/market_bars" / (symbol + ".json"),
        dict(
            symbol=symbol,
            feed="IEX",
            updated=recorded_at,
            candles=[dict(b, time=b["time"].isoformat()) for b in bars],
        ),
        durable=False,
    )


def read(root, symbol, now, max_age=45):
    from quant_lab.dashboard.data import read_json

    if not re.fullmatch(r"[A-Z]{1,5}", symbol):
        return None
    value = read_json(root / "state/market_bars" / (symbol + ".json"), {})
    try:
        age = (now - datetime.fromisoformat(value["updated"])).total_seconds()
        if value.get("symbol") != symbol or value.get("feed") != "IEX" or not -5 <= age <= max_age:
            return None
        if datetime.fromisoformat(value["updated"]).date() != now.date():
            return None
        return value
    except (KeyError, ValueError, TypeError):
        return None
