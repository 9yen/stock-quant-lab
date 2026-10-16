"""0DTE capability and reference quotes only. No order execution methods."""

import os
from datetime import datetime
from math import floor, isfinite
from zoneinfo import ZoneInfo

from quant_lab.strategies.intraday import NY, ROOT, save_state, timeout_request

POLICY = dict(
    underlyings=["SPY", "QQQ"],
    buy_only=True,
    max_contracts_per_entry=1,
    max_open_positions=2,
    per_entry_premium_fraction=0.005,
    total_premium_fraction=0.01,
    daily_loss_fraction=0.01,
    stop_loss_fraction=0.25,
    take_profit_fraction=0.40,
    last_entry_et="14:00",
    exit_by_et="14:30",
    feed_required="opra",
    automatic_enabled=False,
)


def premium_quantity(equity, ask, multiplier=100):
    if multiplier != 100 or not all(isfinite(float(v)) and v > 0 for v in (equity, ask)):
        return 0
    return min(1, floor(equity * POLICY["per_entry_premium_fraction"] / (ask * multiplier)))


def quote_ready(feed, quote, now):
    if feed != "opra" or not quote:
        return False
    bid, ask = float(quote.bid_price), float(quote.ask_price)
    return (
        all(isfinite(v) and v > 0 for v in (bid, ask))
        and bid <= ask
        and (ask - bid) / ask <= 0.10
        and -3 <= (now - quote.timestamp).total_seconds() <= 10
    )


def collect_options(root=ROOT):
    from alpaca.data.enums import DataFeed, OptionsFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.historical.option import OptionHistoricalDataClient
    from alpaca.data.requests import OptionLatestQuoteRequest, StockLatestQuoteRequest
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetOptionContractsRequest
    from dotenv import load_dotenv

    load_dotenv(root / ".env", override=False)
    key, secret = os.environ["ALPACA_KEY"], os.environ["ALPACA_SECRET"]
    client = TradingClient(key, secret, paper=True)
    stock = StockHistoricalDataClient(key, secret)
    options = OptionHistoricalDataClient(key, secret)
    for sdk in (client, stock, options):
        sdk._retry = 0
        sdk._session.request = timeout_request(sdk._session.request)
    now = client.get_clock().timestamp.astimezone(NY)
    account = client.get_account()
    approved = int(account.options_approved_level or 0)
    level = int(account.options_trading_level or 0)
    report = dict(
        recorded_at=now.isoformat(),
        expiry=str(now.date()),
        approved_level=approved,
        trading_level=level,
        options_buying_power=float(account.options_buying_power or 0),
        equity=float(account.equity),
        policy=POLICY,
        status="OBSERVE_ONLY",
        automatic_enabled=False,
        rows=[],
    )
    if min(approved, level) < 2:
        return report | dict(status="NO_OPTIONS_PERMISSION")
    quotes = stock.get_stock_latest_quote(
        StockLatestQuoteRequest(symbol_or_symbols=POLICY["underlyings"], feed=DataFeed.IEX)
    )
    contracts = []
    for underlying in POLICY["underlyings"]:
        q = quotes[underlying]
        spot = (q.bid_price + q.ask_price) / 2
        response = client.get_option_contracts(
            GetOptionContractsRequest(
                underlying_symbols=[underlying],
                expiration_date=now.date(),
                strike_price_gte=str(round(spot * 0.985, 2)),
                strike_price_lte=str(round(spot * 1.015, 2)),
                limit=1000,
            )
        )
        eligible = [
            c
            for c in response.option_contracts
            if c.tradable and c.expiration_date == now.date() and int(c.size) == 100
        ]
        for kind in ("call", "put"):
            rows = [c for c in eligible if getattr(c.type, "value", c.type) == kind]
            if rows:
                contracts.append(min(rows, key=lambda c: abs(float(c.strike_price) - spot)))
    if not contracts:
        return report | dict(status="NO_0DTE_CONTRACTS")
    symbols = [c.symbol for c in contracts]
    try:
        data = options.get_option_latest_quote(
            OptionLatestQuoteRequest(symbol_or_symbols=symbols, feed=OptionsFeed.OPRA)
        )
        feed = "opra"
        report["opra_available"] = True
    except Exception as exc:
        code = getattr(exc, "status_code", None)
        report.update(
            opra_available=False, opra_http_status=code, opra_error_type=type(exc).__name__
        )
        data = options.get_option_latest_quote(
            OptionLatestQuoteRequest(symbol_or_symbols=symbols, feed=OptionsFeed.INDICATIVE)
        )
        feed = "indicative"
    report["status"] = "BLOCKED_OPRA" if feed != "opra" else "OBSERVE_ONLY_OPRA_AVAILABLE"
    for contract in contracts:
        quote = data.get(contract.symbol)
        report["rows"].append(
            dict(
                symbol=contract.symbol,
                underlying=contract.underlying_symbol,
                type=getattr(contract.type, "value", contract.type),
                strike=float(contract.strike_price),
                expiry=str(contract.expiration_date),
                multiplier=100,
                feed=feed,
                bid=quote.bid_price if quote else None,
                ask=quote.ask_price if quote else None,
                quote_time=quote.timestamp.isoformat() if quote else None,
                one_contract_premium=float(quote.ask_price) * 100 if quote else None,
                budget_qty=premium_quantity(float(account.equity), float(quote.ask_price))
                if quote
                else 0,
                quote_ready=quote_ready(feed, quote, now),
            )
        )
    return report


def main():
    import json
    import logging
    import time

    logging.getLogger("alpaca").setLevel(logging.CRITICAL)
    while True:
        try:
            report = collect_options()
        except Exception as exc:
            report = dict(
                status="ERROR",
                automatic_enabled=False,
                error_type=type(exc).__name__,
                recorded_at=datetime.now(ZoneInfo("UTC")).isoformat(),
                rows=[],
                policy=POLICY,
            )
        save_state(ROOT / "state" / "options_preview.json", report)
        print(
            json.dumps(
                dict(status=report["status"], automatic_enabled=False, rows=len(report["rows"]))
            ),
            flush=True,
        )
        time.sleep(60)


if __name__ == "__main__":
    main()
