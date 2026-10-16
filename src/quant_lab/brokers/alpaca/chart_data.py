"""Read-only, explicitly selected equity chart; independent of the active position."""

import os
from datetime import timedelta

from quant_lab.strategies.intraday import NY, session_time, timeout_request
from quant_lab.strategies.scanner import UNIVERSE


def chart_snapshot(root, symbol):
    if symbol not in UNIVERSE:
        raise ValueError("Unsupported chart symbol")
    from alpaca.data.enums import DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetCalendarRequest
    from dotenv import load_dotenv

    load_dotenv(root / ".env", override=False)
    key, secret = os.environ["ALPACA_KEY"], os.environ["ALPACA_SECRET"]
    client = TradingClient(key, secret, paper=True)
    data = StockHistoricalDataClient(key, secret)
    for sdk in (client, data):
        sdk._retry = 0
        sdk._session.request = timeout_request(sdk._session.request)
    now = client.get_clock().timestamp.astimezone(NY)
    sessions = client.get_calendar(
        GetCalendarRequest(start=now.date() - timedelta(days=10), end=now.date())
    )
    available = [s for s in sessions if session_time(s.open) <= now]
    if not available:
        raise ValueError("No chart session")
    selected = available[-1]
    end = min(now, session_time(selected.close))
    response = data.get_stock_bars(
        StockBarsRequest(
            symbol_or_symbols=symbol,
            start=session_time(selected.open),
            end=end,
            timeframe=TimeFrame.Minute,
            feed=DataFeed.IEX,
        )
    )
    bars = [
        dict(
            time=b.timestamp.isoformat(),
            open=b.open,
            high=b.high,
            low=b.low,
            close=b.close,
            volume=float(b.volume),
        )
        for b in response.data.get(symbol, [])
        if b.timestamp + timedelta(minutes=1, seconds=30) <= now
    ]
    q = data.get_stock_latest_quote(
        StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX)
    )[symbol]
    return dict(
        symbol=symbol,
        bar_minutes=1,
        chart_date=str(selected.date),
        bars=bars,
        fetched=now.isoformat(),
        quote=dict(
            symbol=symbol, bid=q.bid_price, ask=q.ask_price, timestamp=q.timestamp.isoformat()
        ),
    )
