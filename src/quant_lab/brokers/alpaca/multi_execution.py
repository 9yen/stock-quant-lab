"""Multi-symbol paper executor: parallel reads, serialized portfolio-reserved orders."""

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta
from math import isfinite
from zoneinfo import ZoneInfo

from quant_lab.brokers.alpaca.execution import (
    PREFIX,
    TERMINAL,
    audit,
    entry_request,
    enum_value,
    flatten,
    gross_exposure,
    occupied_symbols,
    open_orders,
    position_limits,
    require_paper,
    require_shortable,
    slot_limit,
    submit_once,
)
from quant_lab.strategies.intraday import NY, daily_state, save_state, session_time, timeout_request
from quant_lab.strategies.scanner import TECH_UNIVERSE, UNIVERSE, collect


def read_symbol(client, data, symbol):
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockLatestQuoteRequest

    asset = client.get_asset(symbol)
    quote = data.get_stock_latest_quote(
        StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX)
    )[symbol]
    return dict(asset=asset, quote=quote)


class ParallelReads:
    """Independent SDK sessions per thread, at most one outstanding job per symbol."""

    def __init__(self, workers=6):
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="market-read")
        self.local = threading.local()
        self.pending = {}

    def work(self, symbol):
        if not hasattr(self.local, "clients"):
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.trading.client import TradingClient

            key, secret = os.environ["ALPACA_KEY"], os.environ["ALPACA_SECRET"]
            client = TradingClient(key, secret, paper=True)
            data = StockHistoricalDataClient(key, secret)
            for sdk in (client, data):
                sdk._session.request = timeout_request(sdk._session.request)
                sdk._retry = 0
            self.local.clients = (client, data)
        return read_symbol(*self.local.clients, symbol)

    def snapshot(self, symbols, timeout=3):
        for symbol in symbols:
            if symbol not in self.pending:
                self.pending[symbol] = self.pool.submit(self.work, symbol)
        wait([self.pending[s] for s in symbols], timeout=timeout)
        result = {}
        for symbol in symbols:
            future = self.pending[symbol]
            if future.done():
                try:
                    result[symbol] = future.result()
                except Exception as exc:
                    result[symbol] = dict(error=type(exc).__name__)
                del self.pending[symbol]
            else:
                result[symbol] = dict(error="ReadPending")
        return result

    def close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)


def prepare_multi(client, root, session):
    require_paper(client)
    if str(client.get_clock().timestamp.astimezone(NY).date()) != session:
        raise ValueError("Session mismatch")
    old = json.loads((root / "state" / "paper_authorization.json").read_text(encoding="utf-8"))
    account = client.get_account()
    if old["account_id"] != str(account.id) or old["session"] != session or not old["authorized"]:
        raise ValueError("Account/session mismatch")
    if client.get_all_positions() or open_orders(client, UNIVERSE):
        raise ValueError("Upgrade requires flat reconciled portfolio")
    eligibility = {}
    for symbol in UNIVERSE:
        asset = client.get_asset(symbol)
        if not asset.tradable or enum_value(asset.status) != "active":
            raise ValueError("Inactive asset")
        eligibility[symbol] = dict(
            long=True,
            short=bool(
                account.shorting_enabled
                and asset.shortable
                and asset.easy_to_borrow
                and asset.marginable
            ),
        )
    slots = slot_limit(old) if old.get("strategy") == "multi_1m" else 10
    auth = dict(
        old,
        strategy="multi_1m",
        universe=list(UNIVERSE),
        allow_short=True,
        max_positions=slots,
        max_position=0.05,
        gross_limit=0.30,
        daily_loss=0.20,
        timeframe="1Min",
        per_symbol_limit=19,
        global_entry_limit=100,
        cooldown_minutes=15,
        read_workers=6,
    )
    save_state(root / "state" / "paper_authorization.json", auth)
    audit(
        root,
        dict(
            event="MULTI_MINUTE_AUTHORIZED",
            session=session,
            universe=list(UNIVERSE),
            max_positions=slots,
            per_symbol_limit=19,
            global_entry_limit=100,
        ),
    )
    return dict(
        status="READY",
        strategy="multi_1m",
        universe=list(UNIVERSE),
        eligibility=eligibility,
        max_positions=slots,
        gross_limit=0.30,
        per_symbol_limit=19,
        global_entry_limit=100,
    )


def exposure(client):
    rows = client.get_all_positions()
    for p in rows:
        if not all(
            isfinite(float(getattr(p, k))) for k in ("qty", "market_value", "avg_entry_price")
        ):
            raise ValueError("Invalid portfolio")
    return rows, [p for p in rows if p.symbol in UNIVERSE and float(p.qty) != 0]


def liquidate_pool(client, journal, path, root, session):
    results = {}
    for symbol in UNIVERSE:
        try:
            results[symbol] = flatten(client, journal, path, root, session, symbol)
        except Exception as exc:
            results[symbol] = "ERROR_" + type(exc).__name__
    _, holdings = exposure(client)
    flat = (
        not holdings
        and not open_orders(client, UNIVERSE)
        and not any(v.startswith("ERROR") for v in results.values())
    )
    return dict(status="FLAT" if flat else "PORTFOLIO_EXIT_PENDING", exits=results, flat=flat)


def candidate_rows(scan, now, opened):
    if scan.get("mode") != "OBSERVE ONLY" or scan.get("error_type"):
        return []
    stamp = datetime.fromisoformat(scan["recorded_at"])
    if not -5 <= (now - stamp).total_seconds() <= 60 or stamp.astimezone(NY).date() != now.date():
        return []
    eligible = []
    for row in scan.get("rows", []):
        direction = row.get("minute_signal")
        if (
            row.get("symbol") not in UNIVERSE
            or direction not in ("LONG", "SHORT")
            or row.get("tradable") is False
        ):
            continue
        if direction == "SHORT" and row.get("shortable") is False:
            continue
        bar = datetime.fromisoformat(row["last_bar"])
        if (
            bar < opened + timedelta(minutes=15)
            or not 30 <= (now - (bar + timedelta(minutes=1))).total_seconds() <= 150
        ):
            continue
        high, low, price = (float(row[k]) for k in ("range_high", "range_low", "price"))
        if not all(isfinite(v) and v > 0 for v in (high, low, price)) or high < low:
            continue
        score = (price - high if direction == "LONG" else low - price) / max(high - low, 0.01)
        if score > 0:
            eligible.append((score, row))
    return [row for _, row in sorted(eligible, key=lambda item: (-item[0], item[1]["symbol"]))]


def entry_counts(journal):
    per = {s: 0 for s in UNIVERSE}
    for cid, record in journal["orders"].items():
        if any(tag in cid for tag in ("-B-", "-S-")):
            symbol = record.get("symbol", "SPY")
            if symbol in per:
                per[symbol] += 1
    return per, sum(per.values())


def entry_allowed(row, auth):
    direction = row.get("minute_signal")
    if direction not in ("LONG", "SHORT"):
        return False
    if auth.get("trend_filter") and not row.get(
        "filter_long" if direction == "LONG" else "filter_short", False
    ):
        return False
    if auth.get("entry_variant") == "bounded_orb":
        try:
            price, high, low, atr = (
                float(row[k]) for k in ("price", "range_high", "range_low", "atr14")
            )
            distance = price - high if direction == "LONG" else low - price
            return (
                all(isfinite(v) and v > 0 for v in (price, high, low, atr))
                and high >= low
                and (high - low) / price <= 0.03
                and 0 < distance <= 1.5 * atr
            )
        except (TypeError, ValueError, KeyError):
            return False
    return True


def safe_flatten(client, journal, path, root, session, symbol):
    try:
        return flatten(client, journal, path, root, session, symbol)
    except Exception as exc:
        journal["entry_disabled"] = True
        save_state(path, journal)
        audit(root, dict(event="EXIT_ERROR", symbol=symbol, error_type=type(exc).__name__))
        return "ERROR_" + type(exc).__name__


def multi_cycle(client, data, root, auth, journal, path, readers=None):
    from quant_lab.strategies.exits import daily_loss_budget

    day_budget = daily_loss_budget(auth)
    require_paper(client)
    per_limit, gross_limit = position_limits(auth)
    target_limit = float(auth.get("target_position", per_limit))
    if not isfinite(target_limit) or not 0 < target_limit <= per_limit:
        raise ValueError("Invalid target position")
    slots = slot_limit(auth)
    if tuple(auth.get("universe", [])) != UNIVERSE or not auth.get("allow_short"):
        raise ValueError("Invalid pool authorization")
    clock = client.get_clock()
    now = clock.timestamp.astimezone(NY)
    session = auth["session"]
    account = client.get_account()
    if str(account.id) != auth["account_id"]:
        raise ValueError("Account mismatch")
    if str(now.date()) != session:
        return dict(status="SESSION_EXPIRED", done=True)
    from alpaca.trading.requests import GetCalendarRequest

    calendar = client.get_calendar(GetCalendarRequest(start=now.date(), end=now.date()))
    if len(calendar) != 1:
        return dict(status="NO_SESSION", done=True)
    opened, closed = session_time(calendar[0].open), session_time(calendar[0].close)
    state_path = root / "state" / "intraday.json"
    from quant_lab.runtime.budget import BudgetAccount, alpaca_budget

    budget = alpaca_budget(client, root, account, now)
    if budget is not None:
        state_path = root / "state" / "intraday_strategy_budget.json"
        account = BudgetAccount(account, budget)
    state = daily_state(state_path, now.date(), float(account.last_equity), str(account.id))
    if budget is not None:
        legacy_path = root / "state/intraday.json"
        legacy = json.loads(legacy_path.read_text()) if legacy_path.exists() else {}
        if legacy.get("day") == str(now.date()) and legacy.get("halted"):
            state["halted"] = True
        journal["daily_baseline"] = state["baseline"]
    equity = float(account.equity)
    if not isfinite(equity) or equity <= 0 and budget is None:
        raise ValueError("Invalid equity")
    if equity <= state["baseline"] * (1 - day_budget):
        state.update(halted=True, halt_reason=f"DAILY_BUDGET_{day_budget:.2%}")
    state.setdefault("symbols", {})
    save_state(state_path, state)
    all_positions, holdings = exposure(client)
    pending = open_orders(client, UNIVERSE)
    if now >= closed:
        return dict(
            status="MARKET_CLOSED_WITH_POSITION" if holdings or pending else "SESSION_FINISHED",
            done=True,
            remaining=[dict(symbol=p.symbol, qty=p.qty) for p in holdings],
        )
    if not clock.is_open or now < opened:
        return dict(status="WAIT_OPEN", broker_time=now.isoformat())
    if (root / "STOP_PAPER").exists() or state["halted"] or now >= closed - timedelta(minutes=10):
        result = liquidate_pool(client, journal, path, root, session)
        return result | dict(done=result["flat"], reason="stop/daily-loss/preclose")
    for cid, record in journal["orders"].items():
        if record.get("status") not in TERMINAL:
            try:
                record["status"] = str(enum_value(client.get_order_by_client_id(cid).status))
            except Exception:
                journal["entry_disabled"] = True
    save_state(path, journal)
    scan = collect(client, data, include_assets=False)
    candidates = candidate_rows(scan, now, opened)
    eligible_candidates = [
        r
        for r in candidates
        if r["symbol"] in auth.get("entry_universe", UNIVERSE) and entry_allowed(r, auth)
    ]
    needed = list(
        dict.fromkeys(
            [p.symbol for p in holdings] + [r["symbol"] for r in eligible_candidates[:12]]
        )
    )
    if readers:
        snapshots = readers.snapshot(needed, timeout=3)
    else:
        snapshots = {}
        for symbol in needed:
            try:
                snapshots[symbol] = read_symbol(client, data, symbol)
            except Exception as exc:
                snapshots[symbol] = dict(error=type(exc).__name__)
    now = client.get_clock().timestamp.astimezone(NY)
    actions = []
    exiting = set()
    for order in pending:
        cid = str(order.client_order_id)
        if cid.startswith(PREFIX) and "-X-" in cid:
            exiting.add(order.symbol)
        elif (
            cid.startswith(PREFIX)
            and any(t in cid for t in ("-B-", "-S-"))
            and order.submitted_at
            and now - order.submitted_at > timedelta(minutes=2)
        ):
            actions.append(
                dict(
                    symbol=order.symbol,
                    status=safe_flatten(client, journal, path, root, session, order.symbol),
                    reason="Entry timeout",
                )
            )
            exiting.add(order.symbol)
    for p in holdings:
        if p.symbol in exiting:
            continue
        qty, entry, value = float(p.qty), float(p.avg_entry_price), float(p.market_value)
        if entry <= 0 or qty * value < 0:
            raise ValueError("Invalid holding")
        snap = snapshots.get(p.symbol, {})
        if snap.get("error") == "ReadPending":
            continue
        quote = snap.get("quote")
        price = float(quote.bid_price if qty > 0 else quote.ask_price) if quote else 0
        valid = (
            quote
            and isfinite(price)
            and price > 0
            and -5 <= (now - quote.timestamp).total_seconds() <= 60
        )
        ranges = journal.get("ranges", {}).get(p.symbol, {})
        # Missing/stale market data is not an economic sell signal. Broker bracket
        # orders remain active; stop, daily-loss and preclose checks precede this.
        if not valid:
            actions.append(
                dict(
                    symbol=p.symbol,
                    status="DATA_HOLD",
                    reason="Quote missing/stale; broker protection retained",
                )
            )
            continue
        reason = (
            "POSITION_CAP"
            if abs(value) > equity * per_limit
            else "STOP_LOSS"
            if (price <= entry * 0.99 if qty > 0 else price >= entry * 1.01)
            else "TAKE_PROFIT"
            if (price >= entry * 1.02 if qty > 0 else price <= entry * 0.98)
            else "RANGE_INVALIDATED"
            if (
                price < ranges.get("low", 0)
                if qty > 0
                else price > ranges.get("high", float("inf"))
            )
            else None
        )
        profit = (price / entry - 1) * (1 if qty > 0 else -1)
        symbol_state = state["symbols"].setdefault(p.symbol, {})
        key = f"{qty}:{entry}:{symbol_state.get('last_entry_at', 'legacy')}"
        if symbol_state.get("position_key") != key:
            symbol_state.update(position_key=key, peak_profit=profit)
        symbol_state["peak_profit"] = max(symbol_state.get("peak_profit", profit), profit)
        if (
            auth.get("profit_trailing")
            and symbol_state["peak_profit"] >= 0.006
            and profit <= symbol_state["peak_profit"] - 0.0025
        ):
            reason = reason or "PROFIT_TRAIL_0.25_AFTER_0.60"
        save_state(state_path, state)
        leave = reason is not None
        if leave:
            audit(
                root,
                dict(
                    event="EXIT_SIGNAL",
                    symbol=p.symbol,
                    reason=reason,
                    price=price,
                    entry=entry,
                    profit_pct=profit * 100,
                    peak_pct=symbol_state["peak_profit"] * 100,
                ),
            )
            actions.append(
                dict(
                    symbol=p.symbol,
                    status=safe_flatten(client, journal, path, root, session, p.symbol),
                    reason=reason,
                )
            )
            exiting.add(p.symbol)
    if budget is not None and not budget.get("valid"):
        return dict(status="STRATEGY_BUDGET_HOLD", actions=actions)
    if journal["entry_disabled"]:
        return dict(status="ENTRY_DISABLED", actions=actions)
    if now >= closed - timedelta(minutes=30):
        return dict(status="ENTRY_WINDOW_CLOSED", actions=actions)
    if any(p.symbol not in UNIVERSE for p in all_positions):
        return dict(status="WAIT_EXTERNAL_PORTFOLIO", actions=actions)
    counts, total = entry_counts(journal)
    if total >= auth["global_entry_limit"]:
        return dict(status="ENTRY_DISABLED", reason="100 total entries reached", actions=actions)
    submitted = []
    for row in candidates:
        symbol = row["symbol"]
        if symbol not in auth.get("entry_universe", UNIVERSE):
            continue
        if not entry_allowed(row, auth):
            continue
        if symbol in exiting or counts[symbol] >= auth["per_symbol_limit"]:
            continue
        symbol_state = state["symbols"].get(symbol, {})
        last = symbol_state.get("last_entry_at")
        if last and now - datetime.fromisoformat(last) < timedelta(minutes=15):
            continue
        if (
            symbol == "SPY"
            and not last
            and state.get("last_entry_at")
            and now - datetime.fromisoformat(state["last_entry_at"]) < timedelta(minutes=15)
        ):
            continue
        snap = snapshots.get(symbol, {})
        if snap.get("error") or not snap.get("asset") or not snap.get("quote"):
            continue
        asset, quote = snap["asset"], snap["quote"]
        if not asset.tradable or enum_value(asset.status) != "active":
            continue
        short = row["minute_signal"] == "SHORT"
        if short and not (
            account.shorting_enabled
            and asset.shortable
            and asset.easy_to_borrow
            and asset.marginable
        ):
            continue
        report = dict(
            symbol=symbol,
            action="SHORT CANDIDATE" if short else "BUY CANDIDATE",
            range_high=row["range_high"],
            range_low=row["range_low"],
            reference_price=row["price"],
            signal_bar=row["last_bar"],
            candidate_qty=10**9,
        )
        try:
            fresh_clock = client.get_clock()
            fresh_now = fresh_clock.timestamp.astimezone(NY)
            fresh_account = client.get_account()
            if budget is not None:
                fresh_budget = alpaca_budget(client, root, fresh_account, fresh_now)
                if not fresh_budget.get("valid"):
                    break
                fresh_account = BudgetAccount(fresh_account, fresh_budget)
            if not fresh_clock.is_open or not opened <= fresh_now < closed - timedelta(minutes=30):
                break
            if (
                str(fresh_account.id) != auth["account_id"]
                or fresh_account.trading_blocked
                or fresh_account.account_blocked
                or enum_value(fresh_account.status) != "ACTIVE"
            ):
                break
            if float(fresh_account.equity) <= state["baseline"] * (1 - day_budget):
                state["halted"] = True
                save_state(state_path, state)
                break
            if (
                not 30
                <= (
                    fresh_now - (datetime.fromisoformat(row["last_bar"]) + timedelta(minutes=1))
                ).total_seconds()
                <= 150
            ):
                continue
            positions = client.get_all_positions()
            orders = open_orders(client, UNIVERSE)
            if len(occupied_symbols(positions, orders, journal)) >= slots:
                continue
            available = gross_limit - gross_exposure(positions, orders, journal) / float(
                fresh_account.equity
            )
            if available <= 0:
                continue
            if budget is not None:
                from quant_lab.strategies.capital import assess, record

                sizing_price = float(quote.ask_price) * 1.001 * (1.05 if short else 1.02)
                estimated_qty = int(
                    min(
                        float(fresh_account.equity) * min(target_limit, available),
                        float(fresh_account.cash),
                    )
                    / sizing_price
                )
                economics = assess(
                    fresh_budget,
                    estimated_qty,
                    float(quote.ask_price),
                    short,
                    max(0, float(quote.ask_price) - float(quote.bid_price)),
                    gross_exposure(positions, orders, journal),
                    day_budget,
                    pending=sum(
                        str(o.client_order_id).startswith(PREFIX)
                        and any(t in str(o.client_order_id) for t in ("-B-", "-S-"))
                        for o in orders
                    ),
                )
                record(root, "alpaca", symbol, economics, fresh_now)
                if not economics["allowed"]:
                    continue
            request = entry_request(
                report,
                fresh_account,
                quote,
                fresh_now,
                session,
                max_position=min(target_limit, available),
            )
            if request.client_order_id in journal["orders"]:
                continue
            positions = client.get_all_positions()
            orders = open_orders(client, UNIVERSE)
            if any(p.symbol == symbol and float(p.qty) for p in positions) or any(
                o.symbol == symbol for o in orders
            ):
                continue
            if (root / "STOP_PAPER").exists():
                break
            if short:
                require_shortable(client, fresh_account, symbol)
            state["symbols"][symbol] = dict(last_entry_at=fresh_now.isoformat())
            state.update(
                entry_candidates=state["entry_candidates"] + 1,
                last_candidate=request.client_order_id,
            )
            save_state(state_path, state)
            journal.setdefault("ranges", {})[symbol] = dict(
                high=row["range_high"], low=row["range_low"]
            )
            save_state(path, journal)
            order = submit_once(client, request, journal, path, root)
            submitted.append(
                dict(
                    symbol=symbol,
                    status=str(enum_value(order.status)),
                    side=enum_value(request.side),
                    qty=request.qty,
                    limit_price=request.limit_price,
                )
            )
            counts, total = entry_counts(journal)
            if journal["entry_disabled"] or total >= auth["global_entry_limit"]:
                break
        except ValueError as exc:
            audit(
                root, dict(event="CANDIDATE_SKIPPED", symbol=symbol, error_type=type(exc).__name__)
            )
            continue
    return dict(
        status="ENTRY_SUBMITTED" if submitted else ("HOLD" if holdings else "WAIT"),
        submitted=submitted,
        actions=actions,
        counts=counts,
        total_entries=total,
        workers=auth.get("read_workers", 6),
        reason=f"{len(auth.get('entry_universe', []))} stocks 1m; quote-safe exits; {per_limit:.0%} each, {gross_limit:.0%} gross",
    )
