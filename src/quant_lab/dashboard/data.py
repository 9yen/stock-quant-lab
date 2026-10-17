"""Read-only broker snapshots and local status; no GUI toolkit dependency."""

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from quant_lab.strategies.intraday import NY, ROOT, fetch_bars, session_time, timeout_request

UTC = ZoneInfo("UTC")
NZ = ZoneInfo("Pacific/Auckland")
STATUS = {
    "WAIT_OPEN": "等待开盘",
    "WAIT": "等待交易信号",
    "HOLD": "持仓观察",
    "ENTRY_ACCEPTED": "买单已受理",
    "ENTRY_NEW": "买单等待成交",
    "ENTRY_FILLED": "买入已成交",
    "ENTRY_PARTIALLY_FILLED": "买单部分成交",
    "ENTRY_PENDING_NEW": "买单正在受理",
    "EXIT_ACCEPTED": "退出订单已受理",
    "EXIT_PENDING": "退出订单等待成交",
    "EXIT_FILLED": "退出已成交",
    "FLAT": "已确认空仓",
    "CANCEL_PENDING": "等待撤单确认",
    "ENTRY_DISABLED": "已禁用新入场",
    "ACCOUNT_BLOCKED": "账户限制",
    "SESSION_FINISHED": "本场已结束",
    "SESSION_EXPIRED": "本场授权已到期",
    "STOPPED_FLAT": "已停止并空仓",
    "ERROR": "连接或数据异常",
    "MARKET_CLOSED_WITH_POSITION": "收盘仍有持仓，需检查",
}
ORDER_STATUS = {
    "new": "待成交",
    "accepted": "已受理",
    "pending_new": "受理中",
    "filled": "已成交",
    "partially_filled": "部分成交",
    "canceled": "已撤销",
    "expired": "已过期",
    "rejected": "被拒绝",
    "held": "保护单待激活",
    "pending_cancel": "撤单中",
    "replaced": "已替换",
}


def value(v):
    return getattr(v, "value", v)


def read_json(path, fallback=None):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return fallback


def tail_events(path, count=100):
    """Bounded log read; tolerate a concurrent incomplete final line."""
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - 131072))
            raw = stream.read().decode("utf-8", errors="replace")
        rows = []
        for line in raw.splitlines():
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    rows.append(row)
            except ValueError:
                continue
        return rows[-count:]
    except OSError:
        return []


def process_alive(pid):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
    import ctypes
    from ctypes import wintypes

    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    dll.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    dll.OpenProcess.restype = wintypes.HANDLE
    dll.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    dll.GetExitCodeProcess.restype = wintypes.BOOL
    dll.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = dll.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        return (
            bool(dll.GetExitCodeProcess(handle, __import__("ctypes").byref(code)))
            and code.value == 259
        )
    finally:
        dll.CloseHandle(handle)


def local_status(root, now=None):
    now = now or datetime.now(UTC)
    events = tail_events(root / "logs" / "paper_execution.jsonl")
    cycles = [e for e in events if e.get("event") == "CYCLE"]
    last = cycles[-1] if cycles else {}
    stamp = last.get("recorded_at")
    try:
        age = max(0, (now - datetime.fromisoformat(stamp)).total_seconds())
    except (ValueError, TypeError):
        age = None
    record = read_json(root / "state" / "paper_process.json", {})
    alive = process_alive(record.get("pid"))
    return dict(
        last=last,
        age=age,
        alive=alive,
        healthy=alive and age is not None and age < 100,
        stopped=(root / "STOP_PAPER").exists(),
        events=events,
    )


def session_completed(local, account, account_error=None, now=None):
    """A stopped process is expected only with terminal audit and fresh broker confirmation."""
    last = local["last"]
    if local["alive"] or account_error or not account:
        return False
    if not (
        last.get("done") is True
        and last.get("flat") is True
        and last.get("status") in {"FLAT", "STOPPED_FLAT", "SESSION_FINISHED"}
    ):
        return False
    if local["events"] and local["events"][-1] != last:
        return False
    try:
        age = (
            (now or datetime.now(UTC)) - datetime.fromisoformat(account["fetched"])
        ).total_seconds()
    except (KeyError, ValueError, TypeError):
        return False
    return (
        0 <= age <= 45
        and "holdings" in account
        and "open_symbols" in account
        and not account["holdings"]
        and not account["open_symbols"]
    )


def account_snapshot(root):
    """Broker reads only; keys stay in this worker and never reach the display."""
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetCalendarRequest, GetOrdersRequest
    from dotenv import load_dotenv

    load_dotenv(root / ".env", override=False)
    key, secret = os.getenv("ALPACA_KEY"), os.getenv("ALPACA_SECRET")
    if not key or not secret:
        raise ValueError("Missing paper credentials")
    client = TradingClient(key, secret, paper=True)
    data = StockHistoricalDataClient(key, secret)
    for sdk in (client, data):
        sdk._session.request = timeout_request(sdk._session.request)
        sdk._retry = 0
    clock = client.get_clock()
    now = clock.timestamp.astimezone(NY)
    account = client.get_account()
    auth = read_json(root / "state" / "paper_authorization.json", {})
    universe = auth.get("universe", ["SPY"])
    holdings = [p for p in client.get_all_positions() if p.symbol in universe and float(p.qty) != 0]
    pos = holdings[0] if holdings else None
    symbol = pos.symbol if pos else "SPY"
    orders = client.get_orders(
        filter=GetOrdersRequest(
            status=QueryOrderStatus.ALL, symbols=universe, nested=True, limit=30
        )
    )
    state = read_json(root / "state" / "intraday.json", {})
    baseline = state.get("baseline", float(account.last_equity))
    equity = float(account.equity)
    result = dict(
        equity=equity,
        cash=float(account.cash),
        baseline=baseline,
        symbol=symbol,
        holdings=[
            dict(
                symbol=p.symbol,
                qty=float(p.qty),
                value=float(p.market_value),
                entry=float(p.avg_entry_price),
                pnl=float(p.unrealized_pl),
            )
            for p in holdings
        ],
        bar_minutes=1 if auth.get("strategy") == "multi_1m" else 5,
        pnl=equity - baseline,
        daily_pct=(equity / baseline - 1) * 100 if baseline else None,
        qty=float(pos.qty) if pos else 0,
        position_value=float(pos.market_value) if pos else 0,
        position_pnl=float(pos.unrealized_pl) if pos else 0,
        rows=[],
        bars=[],
        clock=now.isoformat(),
        market_open=clock.is_open,
        fetched=datetime.now(UTC).isoformat(),
    )
    result["strategy_version"] = auth.get("strategy_version", "legacy")
    result["open_symbols"] = sorted(
        {
            o.symbol
            for o in client.get_orders(
                filter=GetOrdersRequest(
                    status=QueryOrderStatus.OPEN, symbols=universe, nested=False, limit=500
                )
            )
        }
    )
    result["unrealized_pnl"] = sum(p["pnl"] for p in result["holdings"])
    try:
        from quant_lab.analytics.realized import read_fills, summarize

        fills = read_fills(client)
        result.update(
            summarize(fills, universe, auth.get("session", str(now.date())), result["holdings"])
        )
    except Exception as exc:
        result.update(
            realized_pnl=None,
            realized_complete=False,
            closed_rows=[],
            realized_error=type(exc).__name__,
        )
    if result.get("realized_complete"):
        try:
            from quant_lab.analytics.review import persist_reviews

            result["review"] = persist_reviews(root, fills, universe, result)
        except Exception as exc:
            result["review_error"] = type(exc).__name__
    seen = set()

    def append_order(o, parent_short=None):
        if str(o.id) in seen:
            return
        seen.add(str(o.id))
        t = o.submitted_at or o.created_at
        cid = str(o.client_order_id)
        short_entry = "-S-" in cid
        long_entry = "-B-" in cid
        side = str(value(o.side))
        intent = (
            ("开空" if short_entry else "开多")
            if short_entry or long_entry
            else (
                ("买入回补" if side == "buy" else "卖出平多")
                if "-X-" in cid or parent_short is not None
                else ("买入" if side == "buy" else "卖出")
            )
        )
        result["rows"].append(
            dict(
                time=t.astimezone(NY).strftime("%m-%d %H:%M:%S") if t else "—",
                symbol=o.symbol,
                side="买入" if value(o.side) == "buy" else "卖出",
                qty=o.qty,
                intent_label=intent,
                filled=o.filled_qty,
                price=o.filled_avg_price,
                status=ORDER_STATUS.get(str(value(o.status)), str(value(o.status))),
                type=str(value(o.type)),
                raw_status=str(value(o.status)),
            )
        )
        for child in o.legs or []:
            append_order(child, short_entry if short_entry or long_entry else parent_short)

    for order in orders:
        append_order(order)
    result["rows"] = result["rows"][:24]
    # Price chart failures must not hide successfully read account/position values.
    try:
        sessions = client.get_calendar(
            GetCalendarRequest(
                start=now.date() - timedelta(days=10), end=now.date() + timedelta(days=7)
            )
        )
        current = next((s for s in sessions if s.date == now.date()), None)
        previous = [s for s in sessions if session_time(s.close) < now]
        selected = (
            current
            if current and now >= session_time(current.open)
            else (previous[-1] if previous else None)
        )
        upcoming = next((s for s in sessions if session_time(s.open) > now), None)
        result["next_open"] = session_time(upcoming.open).isoformat() if upcoming else None
        result["close"] = session_time(current.close).isoformat() if current else None
        if selected:
            from alpaca.data.enums import DataFeed
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

            minutes = result["bar_minutes"]
            response = data.get_stock_bars(
                StockBarsRequest(
                    symbol_or_symbols=symbol,
                    start=session_time(selected.open),
                    end=min(now, session_time(selected.close)),
                    timeframe=TimeFrame(minutes, TimeFrameUnit.Minute),
                    feed=DataFeed.IEX,
                )
            )
            result["bars"] = [
                dict(
                    time=b.timestamp.astimezone(NY).isoformat(),
                    open=b.open,
                    high=b.high,
                    low=b.low,
                    close=b.close,
                )
                for b in response.data.get(symbol, [])
                if b.timestamp + timedelta(minutes=minutes, seconds=30) <= now
            ]
            result["chart_date"] = str(selected.date)
    except Exception as exc:
        result["chart_error"] = type(exc).__name__
    from quant_lab.runtime.budget import alpaca_budget

    budget = alpaca_budget(client, root, account, now, commit=False)
    if budget is not None:
        result["strategy_budget"] = {k: v for k, v in budget.items() if k != "days"}
        if budget.get("valid"):
            result.update(
                equity=budget["equity"],
                cash=budget["available_cash"],
                baseline=budget["baseline"],
                pnl=budget["equity"] - budget["baseline"],
                daily_pct=(budget["equity"] / budget["baseline"] - 1) * 100,
            )
            if result.get("realized_complete"):
                from quant_lab.runtime.budget import folder, stamp

                budget_fills = [
                    f
                    for f in fills
                    if stamp(f["transaction_time"]) >= stamp(budget["activated_at"])
                ]
                result.update(
                    summarize(
                        budget_fills,
                        universe,
                        auth.get("session", str(now.date())),
                        result["holdings"],
                    )
                )
                result["review"] = persist_reviews(
                    folder(root, "alpaca"), budget_fills, universe, result
                )
    return result
