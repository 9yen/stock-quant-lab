"""Chronological intraday portfolio replay. Reads data; never imports order clients."""

from collections import defaultdict
from datetime import datetime, time, timedelta
from math import floor

from quant_lab.analytics.review import order_fees
from quant_lab.strategies.exits import exit_plan, timed_exit
from quant_lab.strategies.intraday import NY
from quant_lab.strategies.scanner import signal_context

MODES = ("current_v2", "bounded_orb", "trend_pullback")


def prepare(raw):
    days = defaultdict(dict)
    skipped = []
    for symbol, rows in raw.items():
        grouped = defaultdict(list)
        for row in rows:
            b = dict(row, time=datetime.fromisoformat(row["time"]).astimezone(NY))
            if time(9, 30) <= b["time"].time() < time(16):
                grouped[str(b["time"].date())].append(b)
        for day, bars in grouped.items():
            bars.sort(key=lambda b: b["time"])
            start = bars[0]["time"].replace(hour=9, minute=30, second=0, microsecond=0)
            by = {int((b["time"] - start).total_seconds() / 60): b for b in bars}
            if (
                len(by) != len(bars)
                or len(by) < 300
                or any(i not in by for i in range(15))
                or 389 not in by
            ):
                skipped.append(dict(day=day, symbol=symbol, bars=len(by)))
                continue
            high = max(by[i]["high"] for i in range(15))
            low = min(by[i]["low"] for i in range(15))
            signals = {}
            past = []
            for minute, b in sorted(by.items()):
                past.append(b)
                if minute < 20:
                    continue
                context = signal_context(past, b["time"] + timedelta(minutes=1, seconds=30))
                previous = by.get(minute - 1)
                cross = (
                    "LONG"
                    if previous and previous["close"] <= high < b["close"]
                    else "SHORT"
                    if previous and previous["close"] >= low > b["close"]
                    else "WAIT"
                )
                filtered = (
                    cross
                    if context.get("filter_long" if cross == "LONG" else "filter_short", False)
                    else "WAIT"
                )
                atr = context.get("atr14", 0)
                distance = b["close"] - high if cross == "LONG" else low - b["close"]
                bounded = (
                    filtered
                    if atr > 0 and distance <= 1.5 * atr and (high - low) / b["close"] <= 0.03
                    else "WAIT"
                )
                signals[minute] = dict(
                    current_v2=filtered,
                    bounded_orb=bounded,
                    trend_pullback=context.get("pullback_signal", "WAIT"),
                    atr14=context.get("atr14"),
                    score=max(0, distance) / max(high - low, 0.01),
                )
            days[day][symbol] = dict(bars=by, signals=signals, high=high, low=low)
    return dict(sorted(days.items())), skipped


def fee(qty, price, direction, entry):
    side = "buy" if (direction > 0) == entry else "sell"
    return float(sum(order_fees(qty, qty * price, side).values()))


def replay(days, mode, allocation=0.40, slippage=0.0005, exit_policy="fixed", daily_loss=0.20):
    if mode not in MODES:
        raise ValueError("Unsupported research mode")
    balance = 100000.0
    results = []
    trades = []
    peak = balance
    drawdown = 0
    for day, stocks in days.items():
        start_balance = balance
        positions = {}
        pending = {}
        cooldown = {}
        entries = defaultdict(int)
        halted = False

        def close(symbol, price, reason, minute):
            nonlocal balance
            p = positions.pop(symbol)
            price *= 1 - slippage * p["direction"]
            costs = fee(p["qty"], price, p["direction"], False)
            pnl = (price - p["entry"]) * p["qty"] * p["direction"]
            balance += pnl - costs
            trades.append(
                dict(
                    day=day,
                    symbol=symbol,
                    net=pnl - costs - p["fee"],
                    gross=pnl,
                    fees=costs + p["fee"],
                    reason=reason,
                    minute=minute,
                    notional=p["qty"] * p["entry"],
                    hold_minutes=minute - p["entered"],
                    mae_pct=p["mae"] * 100,
                    mfe_pct=p["mfe"] * 100,
                    exit_policy=exit_policy,
                )
            )

        for minute in range(390):
            for symbol, p in list(positions.items()):
                b = stocks[symbol]["bars"].get(minute)
                if not b:
                    continue
                if (
                    minute >= 380
                    or halted
                    or (p.get("exit_at") is not None and minute >= p["exit_at"])
                ):
                    close(
                        symbol,
                        b["open"],
                        "PRE_CLOSE" if minute >= 380 else p.get("exit_reason", "RANGE_OR_RISK"),
                        minute,
                    )
            # Signal ranking uses only the signal minute, never next-bar returns.
            for symbol, item in sorted(
                list(pending.items()),
                key=lambda pair: (pair[1]["at"], -pair[1].get("score", 0), pair[0]),
            ):
                if item["at"] != minute:
                    continue
                del pending[symbol]
                b = stocks[symbol]["bars"].get(minute)
                if not b or halted or len(positions) >= 6 or symbol in positions:
                    continue
                equity = balance + sum(
                    (p["mark"] - p["entry"]) * p["qty"] * p["direction"] for p in positions.values()
                )
                gross = sum(p["mark"] * p["qty"] for p in positions.values())
                budget = min(equity * allocation, max(0, equity - gross))
                d = item["direction"]
                price = b["open"] * (1 + slippage * d)
                qty = floor(budget / (price * (1.05 if d < 0 else 1.02)))
                try:
                    plan = exit_plan(exit_policy, price, item.get("atr14"))
                except ValueError:
                    continue
                if qty < 1:
                    continue
                entry_fee = fee(qty, price, d, True)
                balance -= entry_fee
                positions[symbol] = dict(
                    qty=qty,
                    direction=d,
                    entry=price,
                    mark=price,
                    fee=entry_fee,
                    plan=plan,
                    entered=minute,
                    mae=0.0,
                    mfe=0.0,
                )
                cooldown[symbol] = minute
                entries[symbol] += 1
            for symbol, p in list(positions.items()):
                b = stocks[symbol]["bars"].get(minute)
                if not b:
                    continue
                d = p["direction"]
                plan = p["plan"]
                stop = p["entry"] * (1 - plan["stop_fraction"] * d)
                target = p["entry"] * (1 + plan["take_fraction"] * d)
                # Excursion uses observed OHLC; bar extrema order remains unknown.
                p["mae"] = min(
                    p["mae"], (b["low"] / p["entry"] - 1) if d > 0 else (1 - b["high"] / p["entry"])
                )
                p["mfe"] = max(
                    p["mfe"], (b["high"] / p["entry"] - 1) if d > 0 else (1 - b["low"] / p["entry"])
                )
                if b["low"] <= stop if d > 0 else b["high"] >= stop:
                    close(
                        symbol,
                        min(b["open"], stop) if d > 0 else max(b["open"], stop),
                        "STOP",
                        minute,
                    )
                    continue
                if b["high"] >= target if d > 0 else b["low"] <= target:
                    close(symbol, target, "TARGET", minute)
                    continue
                p["mark"] = b["close"]
                if (
                    b["close"] < stocks[symbol]["low"]
                    if d > 0
                    else b["close"] > stocks[symbol]["high"]
                ):
                    p.setdefault("exit_at", minute + 2)
                    p.setdefault("exit_reason", "RANGE_INVALIDATED")
                if timed_exit(
                    plan,
                    minute - p["entered"],
                    (b["close"] - p["entry"]) * d,
                    p["entry"] * plan["stop_fraction"],
                ):
                    p.setdefault("exit_at", minute + 2)
                    p.setdefault("exit_reason", "TIMEOUT")
            equity = balance + sum(
                (p["mark"] - p["entry"]) * p["qty"] * p["direction"] for p in positions.values()
            )
            peak = max(peak, equity)
            drawdown = max(drawdown, 1 - equity / peak)
            if equity <= start_balance * (1 - daily_loss):
                halted = True
            if minute >= 358 or halted:
                continue
            for symbol, stock in stocks.items():
                if (
                    symbol == "SPY"
                    or symbol in positions
                    or symbol in pending
                    or entries[symbol] >= 19
                    or sum(entries.values()) >= 100
                ):
                    continue
                if minute - cooldown.get(symbol, -1000) < 15:
                    continue
                signal = stock["signals"].get(minute, {}).get(mode, "WAIT")
                if signal in ("LONG", "SHORT"):
                    pending[symbol] = dict(
                        at=minute + 2,
                        direction=1 if signal == "LONG" else -1,
                        score=stock["signals"][minute].get("score", 0),
                        atr14=stock["signals"][minute].get("atr14"),
                    )
        if positions:
            raise ValueError("Unresolved end-of-day positions")
        day_trades = [t for t in trades if t["day"] == day]
        results.append(
            dict(
                day=day,
                pnl=balance - start_balance,
                return_pct=(balance / start_balance - 1) * 100,
                fees=sum(t["fees"] for t in day_trades),
                trades=len(day_trades),
                equity=balance,
            )
        )
    return dict(
        mode=mode,
        allocation=allocation,
        exit_policy=exit_policy,
        net=balance - 100000,
        days=results,
        trades=trades,
        max_drawdown_pct=drawdown * 100,
    )


def metrics(result, day_filter=None):
    rows = [r for r in result["days"] if day_filter is None or day_filter(r["day"])]
    return dict(
        days=len(rows),
        net=round(sum(r["pnl"] for r in rows), 2),
        mean_day_pct=round(sum(r["return_pct"] for r in rows) / len(rows), 4) if rows else 0,
        worst_day_pct=round(min((r["return_pct"] for r in rows), default=0), 4),
        positive_days=sum(r["pnl"] > 0 for r in rows),
        trades=sum(r["trades"] for r in rows),
        fees=round(sum(r["fees"] for r in rows), 2),
    )
