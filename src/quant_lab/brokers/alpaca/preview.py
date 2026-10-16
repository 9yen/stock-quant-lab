import argparse
import json
import logging
import os
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from quant_lab.strategies.core import Snapshot, decision, signal


def collect(env_file):
    import yfinance as yf
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetCalendarRequest, GetOrdersRequest
    from dotenv import load_dotenv

    load_dotenv(env_file, override=False)
    key, secret = os.getenv("ALPACA_KEY"), os.getenv("ALPACA_SECRET")
    if not key or not secret:
        raise ValueError("Missing ALPACA_KEY or ALPACA_SECRET")
    client = TradingClient(key, secret, paper=True)
    # Use broker time and session closes, including holidays and early closes.
    clock = client.get_clock()
    now = clock.timestamp.astimezone(ZoneInfo("America/New_York"))
    sessions = client.get_calendar(
        GetCalendarRequest(start=now.date() - timedelta(days=30), end=now.date())
    )
    completed = []
    for session in sessions:
        close = session.close
        if close.tzinfo is None:
            close = close.replace(tzinfo=ZoneInfo("America/New_York"))
        if close + timedelta(minutes=30) <= now:
            completed.append(session.date)
    if not completed:
        raise ValueError("Cannot establish last completed market session")
    expected = max(completed)
    # Filter on the server so unrelated orders cannot hide SPY orders behind the default limit.
    orders = client.get_orders(
        filter=GetOrdersRequest(
            status=QueryOrderStatus.OPEN, symbols=["SPY"], limit=500, nested=True
        )
    )
    account = client.get_account()
    positions = [p for p in client.get_all_positions() if p.symbol == "SPY"]
    if len(positions) > 1:
        raise ValueError("Ambiguous SPY position")
    position = positions[0] if positions else None
    snapshot = Snapshot(
        float(account.equity),
        float(account.cash),
        float(position.qty) if position else 0,
        float(position.market_value) if position else 0,
        bool(orders),
        bool(
            account.trading_blocked
            or account.account_blocked
            or getattr(account.status, "value", account.status) != "ACTIVE"
        ),
    )
    frame = yf.download(
        "SPY",
        period="2y",
        auto_adjust=True,
        multi_level_index=False,
        progress=False,
        timeout=20,
        threads=False,
    )
    market = signal(frame, expected)
    return dict(
        mode="DRY RUN",
        symbol="SPY",
        broker_time=now.isoformat(),
        market=market,
        account=asdict(snapshot),
        decision=decision(snapshot, market["price"], market["invested"]),
        note="Historical adjusted close is a sizing estimate, not an executable quote. No orders submitted.",
    )


def main():
    parser = argparse.ArgumentParser(description="Read-only Alpaca paper preview")
    parser.add_argument(
        "--env-file", type=Path, default=Path(__file__).resolve().parents[1] / ".env"
    )
    parser.add_argument("--log-file", type=Path, default=Path("logs/preview.jsonl"))
    args = parser.parse_args()
    # Do not emit third-party request logs or exception strings containing credentials.
    logging.getLogger("alpaca").setLevel(logging.CRITICAL)
    try:
        report = collect(args.env_file)
        code = 0
    except Exception as exc:
        report = dict(
            mode="DRY RUN",
            action="STOP",
            error_type=type(exc).__name__,
            reason="Preview failed; check credentials, connectivity and data availability locally. No orders submitted.",
        )
        code = 1
    report["recorded_at"] = datetime.now(ZoneInfo("UTC")).isoformat()
    rendered = json.dumps(report, ensure_ascii=False)
    try:
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
        with args.log_file.open("a", encoding="utf-8") as stream:
            stream.write(rendered + "\n")
    except OSError:
        print("STOP: Unable to write audit log. No orders submitted.")
        return 1
    print(rendered)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
