"""Read-only multi-symbol 1m/5m comparison. Never executes orders."""

import argparse
import json
import logging
import os
import time
from datetime import datetime, timedelta
from math import isfinite
from zoneinfo import ZoneInfo

from quant_lab.strategies.intraday import NY, ROOT, save_state, session_time, timeout_request
from quant_lab.strategies.universe import WATCH_UNIVERSE

TECH_UNIVERSE = (
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "GOOGL",
    "META",
    "AVGO",
    "AMD",
    "TSLA",
    "NFLX",
    "MU",
    "ORCL",
    "CRM",
    "ADBE",
    "NOW",
    "PLTR",
    "INTC",
    "QCOM",
    "AMAT",
    "LRCX",
)
# Existing ETF positions remain managed to exit; no new ETF entries.

UNIVERSE = ("SPY", "QQQ", "IWM") + WATCH_UNIVERSE
_asset_cache = (0, {})


def compare(symbol, bars, now, opened):
    out = dict(
        symbol=symbol,
        price=None,
        minute_signal="WAIT",
        five_signal="WAIT",
        reason="等待开盘区间",
        last_bar=None,
        range_high=None,
        range_low=None,
        bars=len(bars),
        opening_minutes=0,
    )
    done = sorted(
        [
            b
            for b in bars
            if opened <= b["time"] and b["time"] + timedelta(minutes=1, seconds=30) <= now
        ],
        key=lambda b: b["time"],
    )
    if len({b["time"] for b in done}) != len(done):
        return out | dict(reason="重复行情", minute_signal="INVALID", five_signal="INVALID")
    for b in done:
        if (
            not all(isfinite(float(b[k])) and b[k] > 0 for k in ("open", "high", "low", "close"))
            or not b["low"] <= min(b["open"], b["close"]) <= max(b["open"], b["close"]) <= b["high"]
        ):
            return out | dict(reason="非法行情", minute_signal="INVALID", five_signal="INVALID")
    by_time = {b["time"]: b for b in done}
    opening = [by_time.get(opened + timedelta(minutes=i)) for i in range(15)]
    out["opening_minutes"] = sum(b is not None for b in opening)
    if done:
        out.update(price=done[-1]["close"], last_bar=done[-1]["time"].isoformat())
    if now < opened + timedelta(minutes=16, seconds=30):
        return out
    if not done or now - (done[-1]["time"] + timedelta(minutes=1)) > timedelta(seconds=150):
        return out | dict(reason="一分钟行情缺失或过期", minute_signal="STALE", five_signal="STALE")
    if any(b is None for b in opening):
        return out | dict(
            reason=f"开盘区间仅 {out['opening_minutes']}/15 分钟，不补造数据",
            minute_signal="INCOMPLETE",
            five_signal="INCOMPLETE",
        )
    high, low = max(b["high"] for b in opening), min(b["low"] for b in opening)
    out.update(range_high=high, range_low=low)
    latest = done[-1]
    previous = by_time.get(latest["time"] - timedelta(minutes=1))

    def cross(prev, price):
        if prev <= high < price:
            return "LONG"
        if prev >= low > price:
            return "SHORT"
        return "WAIT"

    if previous and latest["time"] >= opened + timedelta(minutes=15):
        out["minute_signal"] = cross(previous["close"], latest["close"])
    else:
        out["minute_signal"] = "INCOMPLETE"
    # Compare only consecutive, complete five-minute intervals, using the same 1m feed.
    groups = {}
    for b in done:
        key = int((b["time"] - opened).total_seconds() // 300)
        groups.setdefault(key, []).append(b)
    complete = {}
    for key, rows in groups.items():
        expected = [opened + timedelta(minutes=key * 5 + i) for i in range(5)]
        if [b["time"] for b in rows] == expected:
            complete[key] = rows[-1]["close"]
    if complete:
        key = max(complete)
        if key >= 3 and key - 1 in complete:
            end = opened + timedelta(minutes=(key + 1) * 5)
            if now - end <= timedelta(minutes=6, seconds=30):
                out["five_signal"] = cross(complete[key - 1], complete[key])
            else:
                out["five_signal"] = "STALE"
        elif now >= opened + timedelta(minutes=20, seconds=30):
            out["five_signal"] = "INCOMPLETE"
    out["reason"] = "仅观察，不下单；1m 与 5m 是独立确认信号"
    return out


def signal_context(bars, now):
    done = sorted(
        [b for b in bars if b["time"] + timedelta(minutes=1, seconds=30) <= now],
        key=lambda b: b["time"],
    )
    if len(done) < 21:
        return dict(filter_long=False, filter_short=False, filter_reason="MA20 预热不足")
    last = done[-21:]
    if any(b["time"] - a["time"] != timedelta(minutes=1) for a, b in zip(last, last[1:])):
        return dict(filter_long=False, filter_short=False, filter_reason="最近21分钟存在缺失")
    volumes = [float(b.get("volume", 0)) for b in done]
    if any(not isfinite(v) or v < 0 for v in volumes) or sum(volumes) <= 0:
        return dict(filter_long=False, filter_short=False, filter_reason="成交量不可用")
    ma = sum(b["close"] for b in last[1:]) / 20
    prev = sum(b["close"] for b in last[:-1]) / 20
    vwap = sum((b["high"] + b["low"] + b["close"]) / 3 * v for b, v in zip(done, volumes)) / sum(
        volumes
    )
    avg = sum(volumes[-21:-1]) / 20
    rvol = volumes[-1] / avg if avg else 0
    price = last[-1]["close"]
    tr = [
        max(b["high"] - b["low"], abs(b["high"] - a["close"]), abs(b["low"] - a["close"]))
        for a, b in zip(last[-15:-1], last[-14:])
    ]
    atr = sum(tr) / len(tr)
    return dict(
        ma20=ma,
        vwap=vwap,
        rvol=rvol,
        filter_long=price > ma and ma > prev and price > vwap and rvol >= 1,
        filter_short=price < ma and ma < prev and price < vwap and rvol >= 1,
        atr14=atr,
        pullback_signal=(
            "LONG"
            if last[-2]["close"] <= prev and price > ma and ma > prev and price > vwap and rvol >= 1
            else "SHORT"
            if last[-2]["close"] >= prev and price < ma and ma < prev and price < vwap and rvol >= 1
            else "WAIT"
        ),
        filter_reason="MA20斜率 + VWAP + 最近20根均量对比",
    )


def collect(client, data, include_assets=True):
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.trading.requests import GetCalendarRequest

    now = client.get_clock().timestamp.astimezone(NY)
    sessions = client.get_calendar(GetCalendarRequest(start=now.date(), end=now.date()))
    result = dict(
        mode="OBSERVE ONLY",
        universe=list(UNIVERSE),
        recorded_at=now.isoformat(),
        rows=[],
        note="Read-only 1m/5m radar; orders require the separately authorized executor.",
    )
    if not sessions:
        return result | dict(reason="今天没有正常交易时段")
    opened, closed = session_time(sessions[0].open), session_time(sessions[0].close)
    if not opened <= now < closed:
        return result | dict(reason="正常交易时段之外")
    response = data.get_stock_bars(
        StockBarsRequest(
            symbol_or_symbols=list(UNIVERSE),
            start=opened,
            end=now,
            timeframe=TimeFrame.Minute,
            feed=DataFeed.IEX,
        )
    )
    assets = {}
    if include_assets:
        global _asset_cache
        try:
            if time.monotonic() - _asset_cache[0] > 600 or not _asset_cache[1]:
                _asset_cache = (time.monotonic(), {a.symbol: a for a in client.get_all_assets()})
            assets = _asset_cache[1]
        except Exception:
            pass
    for symbol in UNIVERSE:
        bars = [
            dict(
                time=b.timestamp.astimezone(NY),
                open=b.open,
                high=b.high,
                low=b.low,
                close=b.close,
                volume=float(b.volume),
            )
            for b in response.data.get(symbol, [])
        ]
        row = compare(symbol, bars, now, opened)
        row.update(signal_context(bars, now))
        if include_assets:
            try:
                asset = assets.get(symbol)
                if asset is None:
                    raise ValueError("Asset absent")
                row.update(
                    name=asset.name,
                    tradable=bool(asset.tradable),
                    shortable=bool(asset.shortable and asset.easy_to_borrow and asset.marginable),
                )
            except Exception:
                row.update(tradable=None, shortable=None)
        result["rows"].append(row)
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Observe 1m and 5m signals across ETFs and large-cap stocks"
    )
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    logging.getLogger("alpaca").setLevel(logging.CRITICAL)
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.trading.client import TradingClient
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=False)
    client = TradingClient(os.environ["ALPACA_KEY"], os.environ["ALPACA_SECRET"], paper=True)
    data = StockHistoricalDataClient(os.environ["ALPACA_KEY"], os.environ["ALPACA_SECRET"])
    for sdk in (client, data):
        sdk._session.request = timeout_request(sdk._session.request)
        sdk._retry = 0
    while True:
        try:
            report = collect(client, data)
        except Exception as exc:
            report = dict(
                mode="OBSERVE ONLY",
                recorded_at=datetime.now(ZoneInfo("UTC")).isoformat(),
                error_type=type(exc).__name__,
                rows=[],
                reason="扫描连接失败，不提供新信号",
            )
        save_state(ROOT / "state" / "scanner.json", report)
        log = ROOT / "logs" / "scanner.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as out:
            out.write(json.dumps(report, ensure_ascii=False) + "\n")
        print(
            json.dumps(
                dict(mode=report["mode"], rows=len(report["rows"]), error=report.get("error_type"))
            ),
            flush=True,
        )
        if not args.watch:
            return
        time.sleep(20)


if __name__ == "__main__":
    main()
