"""Read-only adapters for the shared broker desk. Never exposes private IDs."""

import re
from datetime import datetime, timezone

from quant_lab.dashboard.data import read_json
from quant_lab.strategies.intraday import NY


def review_home(root):
    return root / "state/brokers/moomoo_desk"


def stock_fills(fills):
    return [
        dict(
            id=f["id"],
            order_id=str(f["id"]).rsplit(":", 1)[0],
            symbol=f["symbol"].removeprefix("US."),
            side=f["side"].lower(),
            qty=str(f["qty"]),
            price=str(f["price"]),
            transaction_time=f["time"],
        )
        for f in fills
        if f.get("instrument", "STOCK") == "STOCK" and f.get("multiplier", 1) == 1
    ]


def stock_holdings(holdings):
    return [
        dict(
            symbol=p["symbol"].removeprefix("US."),
            qty=p["qty"],
            value=p["value"],
            entry=p["cost"],
            pnl=p["pnl"],
            managed=p.get("managed"),
        )
        for p in holdings
        if p.get("instrument", "STOCK") == "STOCK" and not re.search(r"\d{6}[CP]\d+$", p["symbol"])
    ]


def stock_report(root, fills, holdings, auto, market, updated):
    from quant_lab.analytics.realized import summarize
    from quant_lab.analytics.review import persist_reviews
    from quant_lab.strategies.universe import WATCH_UNIVERSE

    day = str(datetime.fromisoformat(updated).astimezone(NY).date())
    normalized = stock_fills(fills)
    held = stock_holdings(holdings)
    result = summarize(normalized, WATCH_UNIVERSE, day, held)
    floating = sum(p["pnl"] for p in held)
    opened = any(r.get("market") in ("MORNING", "AFTERNOON") for r in market.get("rows", []))
    result["review"] = persist_reviews(
        review_home(root),
        normalized,
        WATCH_UNIVERSE,
        dict(
            clock=updated,
            fetched=updated,
            realized_complete=result["realized_complete"],
            market_open=opened,
            unrealized_pnl=floating,
            baseline=auto.get("baseline"),
            strategy_version=auto.get("tuning_profile", "baseline"),
        ),
    )
    return result


def moomoo_state(root):
    from quant_lab.services.automation import public_status
    from quant_lab.strategies.universe import WATCH_UNIVERSE

    value = read_json(root / "state/brokers/moomoo_snapshot.json", {})
    auto = value.get("auto", {})
    status = public_status(root)["moomoo"]
    market = value.get("market", {})
    updated = value.get("updated")
    healthy = (
        bool(updated)
        and (datetime.now(timezone.utc) - datetime.fromisoformat(updated)).total_seconds() < 45
        and value.get("ok", False)
    )
    holdings = stock_holdings(value.get("holdings", []))
    economics = value.get("stock_report", {})
    baseline = auto.get("baseline")
    equity = value.get("assets")
    pnl = equity - baseline if equity is not None and baseline else None
    limits = auto.get("risk", {})
    source_orders = auto.get("orders", [])
    mapped = {
        "FILLED_ALL": "filled",
        "CANCELLED_ALL": "canceled",
        "CANCELLED_PART": "canceled",
        "FAILED": "rejected",
        "DELETED": "canceled",
        "DISABLED": "expired",
    }
    orders = []
    for o in source_orders:
        side = o.get("side", "")
        orders.append(
            dict(
                symbol=o.get("symbol", "").removeprefix("US."),
                time=o.get("created", ""),
                side="买入" if side == "BUY" else "卖出",
                intent_label=("买入·开多" if side == "BUY" else "卖出·开空")
                if o.get("kind") == "ENTRY"
                else ("买入·回补" if side == "BUY" else "卖出·平多"),
                qty=o.get("qty"),
                filled=o.get("filled", 0),
                price=o.get("average"),
                type="限价" if o.get("kind") == "ENTRY" else "市价",
                instrument=o.get("instrument", "STOCK"),
                raw_status=mapped.get(o.get("status"), "accepted"),
                status=o.get("status", "UNKNOWN"),
            )
        )
    scanner = []
    for r in market.get("rows", []):
        scanner.append(
            dict(
                symbol=r["symbol"].removeprefix("US."),
                price=r.get("price"),
                minute_signal=r.get("signal", "WAIT"),
                five_signal=r.get("five_signal", "WAIT"),
                shortable=None,
                opening_minutes=r.get("opening_minutes", 0),
                reason=r.get("reason"),
                filter_long=r.get("signal") == "LONG",
                filter_short=r.get("signal") == "SHORT",
            )
        )
    current_orders = [
        o
        for o in source_orders
        if o.get("kind") == "ENTRY" and o.get("day") == str(datetime.now(NY).date())
    ]
    per = {s: sum(o.get("symbol") == "US." + s for o in current_orders) for s in WATCH_UNIVERSE}
    account = None
    if updated:
        account = dict(
            equity=equity,
            cash=value.get("cash"),
            baseline=baseline,
            pnl=pnl,
            daily_pct=pnl / baseline * 100 if pnl is not None and baseline else None,
            fetched=updated,
            clock=updated,
            qty=sum(p["qty"] for p in holdings),
            position_value=sum(abs(p["value"]) for p in holdings),
            position_pnl=sum(p["pnl"] for p in holdings),
            holdings=holdings,
            rows=orders,
            unrealized_pnl=sum(p["pnl"] for p in holdings),
            market_open=any(
                r.get("market") in ("MORNING", "AFTERNOON") for r in market.get("rows", [])
            ),
            next_open=status.get("next_open"),
            close=auto.get("close"),
            **economics,
        )
    return dict(
        account=account,
        quote=None,
        account_error=None if healthy else "MoomooSnapshotStale",
        quote_error=None,
        server_time=datetime.now(timezone.utc).isoformat(),
        mode="PAPER ONLY",
        session=str(datetime.fromisoformat(updated).astimezone(NY).date()) if updated else None,
        scanner=dict(mode="OBSERVE ONLY", recorded_at=market.get("updated"), rows=scanner),
        backend=dict(
            alive=status.get("fresh", False),
            healthy=status.get("fresh", False),
            stopped=status.get("stopped", False),
            managed_wait=status.get("status")
            in ("WAIT_REGULAR_SESSION", "WAIT_TRADING_DAY", "SESSION_COMPLETE"),
            completed=status.get("status") == "SESSION_COMPLETE",
            auto_status=status.get("status"),
            status=status.get("status"),
            label=status.get("status", "UNKNOWN"),
            next_open=status.get("next_open"),
        ),
        risk=dict(
            strategy="multi_1m",
            universe=list(WATCH_UNIVERSE),
            allow_short=True,
            cap=limits.get("stock_cap", 0.4),
            target_position=limits.get("per_stock", 0.1),
            gross_limit=limits.get("gross", 1),
            max_positions=limits.get("max_positions", 10),
            slots_used=len(holdings),
            candidates=auto.get("daily_entries_used", 0),
            max_candidates=limits.get("daily_entries", 100),
            per_symbol_entries=per,
            per_symbol_limit=limits.get("per_symbol_entries", 19),
            strategy_daily_loss=limits.get("daily_loss", 0.02),
            loss_limit=limits.get("daily_loss", 0.02),
            halted=auto.get("daily_halted", False),
            trend_filter=True,
            entry_variant="bounded_orb",
            strategy_version=auto.get("tuning_profile", "baseline"),
            exit_policy="fixed",
            profit_trailing=False,
        ),
        events=[
            dict(
                recorded_at=o.get("created"),
                status=o.get("status"),
                reason=o.get("reason"),
                symbol=o.get("symbol", "").removeprefix("US."),
            )
            for o in source_orders[-30:]
        ][::-1],
        broker="moomoo",
        capabilities=dict(
            protective_orders=False, options_enabled=bool(auto.get("options_enabled"))
        ),
        extensions=dict(
            options_watch=value.get("options_watch", {}), equity_curve=value.get("equity_curve", [])
        ),
    )


def moomoo_chart(root, symbol):
    from quant_lab.strategies.universe import WATCH_UNIVERSE

    if symbol not in WATCH_UNIVERSE:
        raise ValueError("Unsupported symbol")
    raw = read_json(root / "state/brokers/moomoo_charts" / ("US." + symbol + ".json"), {})
    return dict(
        symbol=symbol,
        bars=raw.get("candles", []),
        bar_minutes=1,
        chart_date=str(datetime.fromisoformat(raw["candles"][-1]["time"]).astimezone(NY).date())
        if raw.get("candles")
        else None,
        feed="moomoo",
        updated=raw.get("updated"),
    )
