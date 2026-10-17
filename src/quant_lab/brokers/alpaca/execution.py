"""Explicitly authorized, one-session SPY paper execution. No live endpoint."""

import argparse
import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from math import floor, isfinite
from pathlib import Path
from zoneinfo import ZoneInfo

from quant_lab.strategies.intraday import (
    NY,
    ROOT,
    daily_state,
    save_state,
    session_time,
    tick,
    timeout_request,
)

PREFIX = "sqlab-"
TERMINAL = {"filled", "canceled", "expired", "rejected", "replaced"}
EXECUTION_LOCK = threading.RLock()


def enum_value(value):
    return getattr(value, "value", value)


def require_paper(client):
    if str(enum_value(client._base_url)).rstrip("/") != "https://paper-api.alpaca.markets":
        raise ValueError("Execution requires the exact Alpaca paper endpoint")


def audit(root, event):
    path = root / "logs" / "paper_execution.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    event = dict(event, recorded_at=datetime.now(ZoneInfo("UTC")).isoformat())
    with path.open("a", encoding="utf-8") as out:
        out.write(json.dumps(event, ensure_ascii=False) + "\n")
        out.flush()
        os.fsync(out.fileno())


def open_orders(client, symbols=None):
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    return client.get_orders(
        filter=GetOrdersRequest(
            status=QueryOrderStatus.OPEN, symbols=list(symbols or ["SPY"]), nested=True, limit=500
        )
    )


def position(client, symbol="SPY"):
    rows = [p for p in client.get_all_positions() if p.symbol == symbol]
    if len(rows) > 1:
        raise ValueError("Ambiguous SPY position")
    if rows and not isfinite(float(rows[0].qty)):
        raise ValueError("Invalid position: manual review required")
    return rows[0] if rows else None


def cancel_spy(client, root, symbol="SPY"):
    require_paper(client)
    orders = open_orders(client, [symbol])
    for order in orders:
        if order.symbol != symbol:
            raise ValueError("Unexpected symbol")
        if enum_value(getattr(order, "status", None)) == "pending_cancel":
            continue
        audit(root, dict(event="CANCEL_INTENT", order_id=str(order.id), symbol=symbol))
        try:
            client.cancel_order_by_id(order.id)
        except Exception:
            # A concurrent fill/cancel is only accepted if the broker confirms terminal state.
            confirmed = client.get_order_by_id(order.id)
            if enum_value(confirmed.status) not in TERMINAL:
                raise
    # A cancel acknowledgement is not a cancellation confirmation.
    if open_orders(client, [symbol]):
        return False
    return all(enum_value(client.get_order_by_id(o.id).status) in TERMINAL for o in orders)


def load_journal(path, account_id, session):
    if path.exists():
        journal = json.loads(path.read_text(encoding="utf-8"))
        if journal["account_id"] != account_id or journal["session"] != session:
            raise ValueError("Journal account/session mismatch")
        return journal
    return dict(account_id=account_id, session=session, entry_disabled=False, orders={})


def position_limits(settings):
    per, gross = float(settings.get("max_position", 0.05)), float(settings.get("gross_limit", 0.30))
    if not all(isfinite(v) for v in (per, gross)) or not 0 < per <= 0.40 or not per <= gross <= 1:
        raise ValueError("Invalid authorized exposure limits")
    return per, gross


def slot_limit(settings):
    count = settings.get("max_positions", 6)
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 10:
        raise ValueError("Invalid simultaneous position limit")
    return count


def occupied_symbols(portfolio, orders, journal=None):
    symbols = {p.symbol for p in portfolio if float(p.qty)}
    symbols.update(o.symbol for o in orders)
    for cid, r in (journal or {}).get("orders", {}).items():
        if any(tag in cid for tag in ("-B-", "-S-")) and r.get("status") not in TERMINAL:
            symbols.add(r.get("symbol", "SPY"))
    return symbols


def gross_exposure(portfolio, orders, journal=None):
    gross = sum(abs(float(p.market_value)) for p in portfolio)
    for order in orders:
        cid = str(order.client_order_id)
        if cid.startswith(PREFIX) and any(tag in cid for tag in ("-B-", "-S-")):
            gross += max(0, float(order.qty) - float(order.filled_qty)) * float(order.limit_price)
    visible = {str(o.client_order_id) for o in orders}
    for cid, r in (journal or {}).get("orders", {}).items():
        if (
            cid not in visible
            and any(tag in cid for tag in ("-B-", "-S-"))
            and r.get("status") not in TERMINAL
        ):
            if r.get("limit_price") is None:
                raise ValueError("Unpriced unresolved entry reservation")
            price = float(r.get("limit_price", 0))
            qty = float(r["qty"])
            if not all(isfinite(v) and v > 0 for v in (price, qty)):
                raise ValueError("Invalid unresolved entry reservation")
            gross += qty * price
    if not isfinite(gross) or gross < 0:
        raise ValueError("Invalid gross exposure")
    return gross


def submit_once(client, request, journal, journal_path, root):
    with EXECUTION_LOCK:
        return _submit_once(client, request, journal, journal_path, root)


def _submit_once(client, request, journal, journal_path, root):
    require_paper(client)
    universe = journal.get("universe", ["SPY"])
    if request.symbol not in universe or not request.client_order_id.startswith(PREFIX):
        raise ValueError("Unexpected order")
    cid = request.client_order_id
    if cid in journal["orders"]:
        # Never re-submit an ambiguous intent, even when a lookup returns 404.
        return client.get_order_by_client_id(cid)
    clock = client.get_clock()
    if not clock.is_open or str(clock.timestamp.astimezone(NY).date()) != journal["session"]:
        raise ValueError("Orders are restricted to the authorized regular session")
    account = client.get_account()
    if str(account.id) != journal["account_id"]:
        raise ValueError("Order account mismatch")
    if not request.qty or not isfinite(float(request.qty)) or request.qty <= 0:
        raise ValueError("Invalid order quantity")
    is_short = "-S-" in cid
    is_entry = "-B-" in cid or is_short
    if is_entry:
        from quant_lab.runtime.budget import BudgetAccount, alpaca_budget

        budget = alpaca_budget(client, root, account, clock.timestamp)
        if budget is not None:
            if not budget.get("valid"):
                raise ValueError("Strategy budget unavailable before submission")
            account = BudgetAccount(account, budget)
            journal["daily_baseline"] = budget["baseline"]
        if journal["entry_disabled"] or (root / "STOP_PAPER").exists():
            raise ValueError("Entry disabled")
        if enum_value(request.order_class) != "bracket" or enum_value(request.type) != "limit":
            raise ValueError("Entries must use protected limit brackets")
        if enum_value(request.side) != ("sell" if is_short else "buy"):
            raise ValueError("Entry side/intent mismatch")
        if is_short:
            if not journal.get("allow_short", False):
                raise ValueError("Short entries are not authorized")
            require_shortable(client, account, request.symbol)
        multi = journal.get("strategy") == "multi_1m"
        per_limit, gross_limit = position_limits(journal) if multi else (0.05, 0.05)
        if multi:
            from quant_lab.strategies.exits import daily_loss_budget

            baseline = journal.get("daily_baseline")
            if baseline is not None:
                if not isfinite(float(baseline)) or float(baseline) <= 0:
                    raise ValueError("Invalid daily baseline")
                if float(account.equity) <= float(baseline) * (1 - daily_loss_budget(journal)):
                    journal["entry_disabled"] = True
                    save_state(journal_path, journal)
                    raise ValueError("Daily budget reached before submission")
            target = float(journal.get("target_position", per_limit))
            if not isfinite(target) or not 0 < target <= per_limit:
                raise ValueError("Invalid entry target")
            per_limit = min(per_limit, target)
        cap = min(
            float(account.equity) * per_limit, float(account.cash), float(account.buying_power)
        )
        buffer = 1.05 if is_short else 1.02
        portfolio = client.get_all_positions()
        pool_orders = open_orders(client, universe)
        multi = journal.get("strategy") == "multi_1m"
        own_position = any(p.symbol == request.symbol and float(p.qty) != 0 for p in portfolio)
        own_order = any(o.symbol == request.symbol for o in pool_orders)
        gross = gross_exposure(portfolio, pool_orders, journal) if multi else 0
        if (
            not isfinite(cap)
            or request.qty * request.limit_price * buffer > cap
            or (own_position or own_order if multi else bool(portfolio) or bool(pool_orders))
        ):
            raise ValueError("Exposure or cash check failed")
        if (
            multi
            and gross + request.qty * request.limit_price * buffer
            > float(account.equity) * gross_limit
        ):
            raise ValueError("Combined authorized gross exposure cap")
        if multi:
            occupied = occupied_symbols(portfolio, pool_orders, journal)
            if request.symbol not in journal.get("entry_universe", universe):
                raise ValueError("Symbol is exit-only")
            if request.symbol not in occupied and len(occupied) >= slot_limit(journal):
                raise ValueError("Concurrent symbol limit reached")
        if multi:
            entries = [
                record
                for key, record in journal["orders"].items()
                if any(tag in key for tag in ("-B-", "-S-"))
            ]
            if len(entries) >= journal.get("global_entry_limit", 100) or sum(
                record.get("symbol", "SPY") == request.symbol for record in entries
            ) >= journal.get("per_symbol_limit", 19):
                raise ValueError("Daily entry submission cap")
            if any(
                record.get("symbol", "SPY") == request.symbol
                and record.get("status") not in TERMINAL
                for record in entries
            ):
                raise ValueError("Unresolved or reserved entry for symbol")
        if (
            account.trading_blocked
            or account.account_blocked
            or enum_value(account.status) != "ACTIVE"
        ):
            raise ValueError("Account blocked")
    elif "-X-" in cid:
        p = position(client, request.symbol)
        if not p or request.qty > abs(float(p.qty)) or open_orders(client, [request.symbol]):
            raise ValueError("Exit would exceed available SPY holdings")
        if enum_value(request.side) != ("buy" if float(p.qty) < 0 else "sell"):
            raise ValueError("Exit direction mismatch")
        if enum_value(request.type) != "market":
            raise ValueError("Exit requires market order")
    else:
        raise ValueError("Unsupported side")
    journal["orders"][cid] = dict(
        status="unknown",
        side=enum_value(request.side),
        qty=request.qty,
        symbol=request.symbol,
        strategy_version=journal.get("strategy_version", "legacy"),
        limit_price=getattr(request, "limit_price", None),
    )
    save_state(journal_path, journal)
    audit(
        root,
        dict(
            event="SUBMIT_INTENT",
            client_order_id=cid,
            symbol=request.symbol,
            side=enum_value(request.side),
            qty=request.qty,
            type=enum_value(request.type),
        ),
    )
    try:
        order = client.submit_order(order_data=request)
    except Exception:
        journal["entry_disabled"] = True
        save_state(journal_path, journal)
        order = client.get_order_by_client_id(cid)
    journal["orders"][cid].update(status=str(enum_value(order.status)), order_id=str(order.id))
    save_state(journal_path, journal)
    audit(root, dict(event="SUBMIT_ACK", client_order_id=cid, status=str(enum_value(order.status))))
    return order


def money(value, up=False):
    return float(
        Decimal(str(value)).quantize(Decimal(".01"), rounding=ROUND_CEILING if up else ROUND_FLOOR)
    )


def require_shortable(client, account, symbol="SPY"):
    asset = client.get_asset(symbol)
    if not getattr(account, "shorting_enabled", False) or float(account.equity) < 2000:
        raise ValueError("Shorting unavailable for this account")
    if (
        not asset.tradable
        or not asset.shortable
        or not asset.easy_to_borrow
        or not asset.marginable
    ):
        raise ValueError("SPY is not currently eligible for easy-to-borrow shorts")
    return asset


def entry_request(report, account, quote, now, session, max_position=0.05):
    from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
    from alpaca.trading.requests import LimitOrderRequest, StopLossRequest, TakeProfitRequest

    age = (now - quote.timestamp).total_seconds()
    bid, ask = float(quote.bid_price), float(quote.ask_price)
    if age < -5 or age > 60 or not all(isfinite(v) and v > 0 for v in (bid, ask)) or bid > ask:
        raise ValueError("Invalid or stale quote")
    if (ask - bid) / ask > 0.0025:
        raise ValueError("Spread too wide")
    is_short = report.get("action") == "SHORT CANDIDATE"
    price = bid if is_short else ask
    breakout = bid < report["range_low"] if is_short else ask > report["range_high"]
    if not breakout or abs(price / report["reference_price"] - 1) > 0.01:
        raise ValueError("Breakout no longer valid or price moved too far")
    equity, cash, buying_power = (
        float(account.equity),
        float(account.cash),
        float(account.buying_power),
    )
    if not all(isfinite(v) for v in (equity, cash, buying_power)) or equity <= 0:
        raise ValueError("Invalid account amounts")
    limit = money(bid * 0.999) if is_short else money(ask * 1.001, up=True)
    reference = max(ask, limit) if is_short else limit
    buffer = 1.05 if is_short else 1.02
    if not isfinite(max_position) or not 0 < max_position <= 0.40:
        raise ValueError("Invalid sizing limit")
    qty = min(
        int(report["candidate_qty"]),
        floor(
            min(equity * max_position, max(cash, 0), max(buying_power, 0)) / (reference * buffer)
        ),
    )
    if qty <= 0:
        raise ValueError("Insufficient budget")
    stamp = datetime.fromisoformat(report["signal_bar"])
    direction = "S" if is_short else "B"
    symbol = report.get("symbol", "SPY")
    suffix = f"-{symbol}" if "symbol" in report else ""
    cid = f"{PREFIX}{session.replace('-', '')}-{direction}-{stamp.strftime('%H%M')}{suffix}"
    return LimitOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.SELL if is_short else OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        limit_price=limit,
        client_order_id=cid,
        order_class=OrderClass.BRACKET,
        stop_loss=StopLossRequest(
            stop_price=money(limit * (1.01 if is_short else 0.99), up=is_short)
        ),
        take_profit=TakeProfitRequest(
            limit_price=money(limit * (0.98 if is_short else 1.02), up=not is_short)
        ),
    )


def flatten(client, journal, journal_path, root, session, symbol="SPY"):
    """Do not sell until all SPY orders have cancelled and quantity has been refreshed."""
    require_paper(client)
    orders = open_orders(client, [symbol])
    # Existing bot market exit owns the shares until terminal; never cancel/recreate it each tick.
    if any(
        "-X-" in str(o.client_order_id) and str(o.client_order_id).startswith(PREFIX)
        for o in orders
    ):
        return "EXIT_PENDING"
    if not cancel_spy(client, root, symbol):
        return "CANCEL_PENDING"
    p = position(client, symbol)
    if not p or float(p.qty) == 0:
        return "FLAT"
    if open_orders(client, [symbol]):
        return "CANCEL_PENDING"
    # Resolve prior uncertain exits before allowing a new sell request.
    exits = [
        cid
        for cid, record in journal["orders"].items()
        if "-X-" in cid and record.get("symbol", "SPY") == symbol
    ]
    for cid in exits:
        if journal["orders"][cid].get("status") in TERMINAL:
            continue
        order = client.get_order_by_client_id(cid)
        journal["orders"][cid]["status"] = str(enum_value(order.status))
        if enum_value(order.status) not in TERMINAL:
            return "EXIT_PENDING"
    # Refresh again after the order reconciliation to avoid selling a just-filled exit.
    p = position(client, symbol)
    if not p or float(p.qty) == 0:
        return "FLAT"
    if len(exits) >= max(10, journal.get("per_symbol_limit", 3) * 3):
        raise ValueError("Exit retry cap reached: manual review required")
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    suffix = f"-{symbol}" if journal.get("strategy") == "multi_1m" else ""
    request = MarketOrderRequest(
        symbol=symbol,
        qty=abs(float(p.qty)),
        side=OrderSide.BUY if float(p.qty) < 0 else OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
        client_order_id=f"{PREFIX}{session.replace('-', '')}-X-{len(exits) + 1:03}{suffix}",
    )
    # Do not queue an exit into the next session if the market just closed.
    if not client.get_clock().is_open:
        return "MARKET_CLOSED_WITH_POSITION"
    order = submit_once(client, request, journal, journal_path, root)
    return "EXIT_" + str(enum_value(order.status)).upper()


def prepare(client, root, session, allow_short=False):
    require_paper(client)
    clock = client.get_clock()
    if str(clock.timestamp.astimezone(NY).date()) != session:
        raise ValueError("Authorization must be for the current New York date")
    account = client.get_account()
    position(client)
    if allow_short:
        require_shortable(client, account)
    # Re-running prepare cannot silently overwrite a different account/session authorization.
    auth_path = root / "state" / "paper_authorization.json"
    if auth_path.exists():
        old = json.loads(auth_path.read_text(encoding="utf-8"))
        if old["account_id"] != str(account.id) or old["session"] != session:
            raise ValueError("Existing authorization mismatch")
    for attempt in range(10):
        if cancel_spy(client, root):
            auth = dict(
                account_id=str(account.id),
                session=session,
                paper_only=True,
                symbol="SPY",
                max_position=0.05,
                daily_loss=0.20,
                authorized=True,
                allow_short=allow_short,
            )
            save_state(auth_path, auth)
            audit(root, dict(event="AUTHORIZED_SESSION_READY", session=session, open_spy_orders=0))
            p = position(client)
            return dict(
                status="READY", session=session, open_spy_orders=0, spy_qty=float(p.qty) if p else 0
            )
        time.sleep(2)
    raise ValueError("Cancellation not confirmed; paper execution not enabled")


def cycle(client, data, root, auth, journal, journal_path):
    require_paper(client)
    from alpaca.trading.requests import GetCalendarRequest

    now_clock = client.get_clock()
    now = now_clock.timestamp.astimezone(NY)
    session = auth["session"]
    account = client.get_account()
    if str(account.id) != auth["account_id"]:
        raise ValueError("Authorized account mismatch")
    if str(now.date()) != session:
        return dict(status="SESSION_EXPIRED", done=True)
    calendar = client.get_calendar(GetCalendarRequest(start=now.date(), end=now.date()))
    if len(calendar) != 1:
        return dict(status="NO_SESSION", done=True)
    opened, closed = session_time(calendar[0].open), session_time(calendar[0].close)
    state_path = root / "state" / "intraday.json"
    state = daily_state(state_path, now.date(), float(account.last_equity), str(account.id))
    equity = float(account.equity)
    if not isfinite(equity) or equity <= 0:
        raise ValueError("Invalid equity")
    if equity / state["baseline"] <= 0.8:
        state["halted"] = True
    save_state(state_path, state)
    stopping = (root / "STOP_PAPER").exists()
    exiting = state["halted"] or stopping or now >= closed - timedelta(minutes=10)
    if now >= closed:
        p, pending = position(client), open_orders(client)
        if pending:
            cancel_spy(client, root)
        return dict(
            status="SESSION_FINISHED" if not p else "MARKET_CLOSED_WITH_POSITION",
            done=True,
            remaining_qty=float(p.qty) if p else 0,
        )
    if not now_clock.is_open or now < opened:
        if stopping and cancel_spy(client, root) and not position(client):
            return dict(status="STOPPED_FLAT", done=True)
        return dict(
            status="WAIT_OPEN",
            broker_time=now.isoformat(),
            next_open_nz=opened.astimezone(ZoneInfo("Pacific/Auckland")).isoformat(),
        )
    if exiting:
        status = flatten(client, journal, journal_path, root, session)
        return dict(status=status, done=status == "FLAT", reason="stop/daily-loss/session-close")
    # Cancel entry orders unfilled for two minutes, including partially filled parents.
    for o in open_orders(client):
        if str(o.client_order_id).startswith(PREFIX) and any(
            tag in str(o.client_order_id) for tag in ("-B-", "-S-")
        ):
            if o.submitted_at and now - o.submitted_at > timedelta(minutes=2):
                return dict(
                    status=flatten(client, journal, journal_path, root, session),
                    reason="Entry timeout",
                )
    report = tick(client, data, state_path, allow_short=auth.get("allow_short", False))
    action = report["action"]
    if action in ("EXIT CANDIDATE", "CANCEL THEN EXIT CANDIDATE"):
        return dict(
            status=flatten(client, journal, journal_path, root, session), reason=report["reason"]
        )
    if action in ("STOP", "REVIEW"):
        journal["entry_disabled"] = True
        save_state(journal_path, journal)
        return dict(
            status=flatten(client, journal, journal_path, root, session), reason=report["reason"]
        )
    if action not in ("BUY CANDIDATE", "SHORT CANDIDATE"):
        return dict(status=action, reason=report["reason"])
    if action == "SHORT CANDIDATE" and not auth.get("allow_short", False):
        return dict(status="SHORT_NOT_AUTHORIZED")
    if (
        journal["entry_disabled"]
        or sum(any(tag in k for tag in ("-B-", "-S-")) for k in journal["orders"]) >= 3
    ):
        return dict(status="ENTRY_DISABLED")
    # Repeat account, position, session, order and risk checks immediately before submission.
    if open_orders(client) or position(client):
        return dict(status="WAIT_EXISTING_EXPOSURE")
    fresh_clock = client.get_clock()
    fresh_now = fresh_clock.timestamp.astimezone(NY)
    if not fresh_clock.is_open or not opened <= fresh_now < closed - timedelta(minutes=30):
        return dict(status="ENTRY_WINDOW_CLOSED")
    account = client.get_account()
    if (
        str(account.id) != auth["account_id"]
        or account.trading_blocked
        or account.account_blocked
        or enum_value(account.status) != "ACTIVE"
    ):
        return dict(status="ACCOUNT_BLOCKED")
    if float(account.equity) <= state["baseline"] * 0.8:
        state["halted"] = True
        save_state(state_path, state)
        return dict(status="DAILY_LOSS_HALTED")
    asset = client.get_asset("SPY")
    if not asset.tradable or enum_value(asset.status) != "active":
        return dict(status="ASSET_NOT_TRADABLE")
    if action == "SHORT CANDIDATE":
        require_shortable(client, account)
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockLatestQuoteRequest

    quote = data.get_stock_latest_quote(
        StockLatestQuoteRequest(symbol_or_symbols="SPY", feed=DataFeed.IEX)
    )["SPY"]
    request = entry_request(report, account, quote, fresh_now, session)
    # Final read immediately before sending a limit order.
    if open_orders(client) or position(client) or (root / "STOP_PAPER").exists():
        return dict(status="ENTRY_ABORTED")
    order = submit_once(client, request, journal, journal_path, root)
    return dict(
        status="ENTRY_" + str(enum_value(order.status)).upper(),
        qty=request.qty,
        limit_price=request.limit_price,
        client_order_id=request.client_order_id,
    )


def main():
    parser = argparse.ArgumentParser(description="One-session SPY PAPER execution only")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--prepare-paper", action="store_true")
    modes.add_argument("--execute-paper", action="store_true")
    parser.add_argument("--session", required=True, type=date.fromisoformat)
    parser.add_argument(
        "--allow-shorts",
        action="store_true",
        help="Enable explicitly authorized two-way PAPER strategy when preparing",
    )
    parser.add_argument(
        "--multi-minute",
        action="store_true",
        help="Prepare the explicitly authorized six-symbol one-minute strategy",
    )
    args = parser.parse_args()
    session = str(args.session)
    logging.getLogger("alpaca").setLevel(logging.CRITICAL)
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.trading.client import TradingClient
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=False)
    key, secret = os.getenv("ALPACA_KEY"), os.getenv("ALPACA_SECRET")
    if not key or not secret:
        print("STOP: Missing credentials")
        return 1
    client = TradingClient(key, secret, paper=True)
    data = StockHistoricalDataClient(key, secret)
    for sdk in (client, data):
        sdk._session.request = timeout_request(sdk._session.request)
        sdk._retry = 0  # No automatic POST retry after ambiguous network failures.
    require_paper(client)
    from quant_lab.runtime.locking import acquire, release

    lock = ROOT / "state" / "intraday.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+b") as handle:
        if handle.seek(0, 2) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            acquire(handle)
        except OSError:
            print("STOP: Another intraday process is running")
            return 1
        try:
            if args.prepare_paper:
                if args.multi_minute:
                    from quant_lab.brokers.alpaca.multi_execution import prepare_multi

                    print(json.dumps(prepare_multi(client, ROOT, session)), flush=True)
                    return 0
                print(
                    json.dumps(prepare(client, ROOT, session, allow_short=args.allow_shorts)),
                    flush=True,
                )
                return 0
            auth = json.loads(
                (ROOT / "state" / "paper_authorization.json").read_text(encoding="utf-8")
            )
            if (
                not auth["authorized"]
                or auth["session"] != session
                or auth["symbol"] != "SPY"
                or not auth["paper_only"]
            ):
                raise ValueError("Missing session authorization")
            journal_path = ROOT / "state" / f"paper_intents_{session}.json"
            journal = load_journal(journal_path, auth["account_id"], session)
            journal["allow_short"] = bool(auth.get("allow_short", False))
            journal["universe"] = auth.get("universe", ["SPY"])
            journal["entry_universe"] = auth.get("entry_universe", journal["universe"])
            journal["max_position"] = auth.get("max_position", 0.05)
            journal["gross_limit"] = auth.get("gross_limit", 0.30)
            journal["target_position"] = auth.get("target_position", journal["max_position"])
            journal["strategy_version"] = auth.get("strategy_version", "legacy")
            journal["daily_loss"] = auth.get("daily_loss", 0.20)
            journal["strategy_daily_loss"] = auth.get("strategy_daily_loss", journal["daily_loss"])
            current_risk_path = ROOT / "state" / "intraday.json"
            current_risk = (
                json.loads(current_risk_path.read_text(encoding="utf-8-sig"))
                if current_risk_path.exists()
                else {}
            )
            journal["daily_baseline"] = (
                current_risk["baseline"]
                if current_risk.get("day") == session
                and current_risk.get("account_id") == auth["account_id"]
                else float(client.get_account().last_equity)
            )
            journal["strategy"] = auth.get("strategy", "spy_5m")
            journal["per_symbol_limit"] = auth.get("per_symbol_limit", 3)
            journal["global_entry_limit"] = auth.get("global_entry_limit", 3)
            journal["max_positions"] = auth.get("max_positions", 6)
            save_state(journal_path, journal)
            failures = 0
            readers = None
            if auth.get("strategy") == "multi_1m":
                from quant_lab.brokers.alpaca.multi_execution import ParallelReads

                readers = ParallelReads(workers=auth.get("read_workers", 6))
            while True:
                try:
                    if auth.get("strategy") == "multi_1m":
                        from quant_lab.brokers.alpaca.multi_execution import multi_cycle

                        result = multi_cycle(
                            client, data, ROOT, auth, journal, journal_path, readers=readers
                        )
                    else:
                        result = cycle(client, data, ROOT, auth, journal, journal_path)
                    failures = 0
                except Exception as exc:
                    failures += 1
                    if failures >= 3:
                        journal["entry_disabled"] = True
                        save_state(journal_path, journal)
                    result = dict(
                        status="ERROR", error_type=type(exc).__name__, consecutive_failures=failures
                    )
                    # Service failures do not abandon the pre-close liquidation loop.
                result.update(mode="PAPER ONLY", session=session)
                audit(ROOT, dict(event="CYCLE", **result))
                print(json.dumps(result, ensure_ascii=False), flush=True)
                if result.get("done"):
                    return 0 if result["status"] != "MARKET_CLOSED_WITH_POSITION" else 2
                time.sleep(15 if auth.get("strategy") == "multi_1m" else 30)
        finally:
            if "readers" in locals() and readers is not None:
                readers.close()
            handle.seek(0)
            release(handle)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        # Never print request headers, API keys, or raw SDK exception strings.
        print(json.dumps(dict(status="STOP", error_type=type(error).__name__)), flush=True)
        raise SystemExit(1)
