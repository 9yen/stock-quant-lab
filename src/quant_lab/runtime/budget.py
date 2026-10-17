"""Separate paper capital from broker balances using post-activation stock fills."""

import json
from collections import defaultdict, deque
from datetime import datetime, timezone
from decimal import Decimal

from quant_lab.runtime.storage import write_json
from quant_lab.strategies.intraday import NY

D = Decimal


def folder(root, broker):
    if broker not in {"alpaca", "moomoo"}:
        raise ValueError("Unknown budget broker")
    return root / "state/strategy_budget" / broker


def settings(root, broker):
    path = root / "state/strategy_budget/config.json"
    if not path.exists():
        return None
    config = json.loads(path.read_text(encoding="utf-8-sig"))["brokers"][broker]
    if config["schema_version"] != 1 or config["currency"] != "USD":
        raise ValueError("Unsupported strategy budget")
    if (
        not D(str(config["initial_capital"])).is_finite()
        or not 0 < D(str(config["initial_capital"])) <= 1000000
    ):
        raise ValueError("Invalid strategy capital")
    stamp(config["activated_at"])
    return config


def stamp(value):
    value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("Budget timestamps require a timezone")
    return value


def charges(qty, amount, side):
    from quant_lab.analytics.review import cents, order_fees

    fees = order_fees(qty, amount, side)
    # Budget uses current NZ stock TAF, even where historical reviews used an older profile.
    fees["taf"] = (
        cents(min(D("9.79"), max(D(".01"), qty * D(".000195")))) if side != "buy" else D(0)
    )
    return sum(fees.values(), D(0))


def calculate(config, fills, positions):
    """Stock-only FIFO. Deposits and pre-activation profits never fund the strategy."""
    rows, seen = [], {}
    activated = stamp(config["activated_at"])
    for original in fills:
        when = stamp(original.get("transaction_time", original.get("time")))
        if when < activated:
            continue
        row = dict(original)
        if row.get("instrument", "STOCK") != "STOCK" or row.get("multiplier", 1) != 1:
            raise ValueError("Options are outside the stock strategy budget")
        side = str(row["side"]).lower()
        if side not in {"buy", "sell", "sell_short"}:
            raise ValueError("Unknown fill side")
        qty, price = D(str(row["qty"])), D(str(row["price"]))
        if not all(x.is_finite() and x > 0 for x in (qty, price)) or qty != qty.to_integral_value():
            raise ValueError("Budget requires valid whole-share fills")
        identity = str(row["id"])
        normalized = (str(row["symbol"]), side, qty, price, when)
        if identity in seen:
            if seen[identity] != normalized:
                raise ValueError("Conflicting duplicate fill")
            continue
        seen[identity] = normalized
        order = str(row.get("order_id") or identity.rsplit(":", 1)[0])
        rows.append((when, identity, str(row["symbol"]), side, qty, price, order))
    groups = defaultdict(list)
    books = defaultdict(deque)
    realized = D(0)
    for when, identity, symbol, side, qty, price, order in sorted(rows):
        groups[(str(when.astimezone(NY).date()), order)].append((qty, price, side, symbol))
        direction = 1 if side == "buy" else -1
        book = books[symbol]
        while qty and book and book[0][0] != direction:
            old_direction, remaining, entry = book[0]
            matched = min(qty, remaining)
            realized += matched * (price - entry) * old_direction
            qty -= matched
            book[0][1] -= matched
            if not book[0][1]:
                book.popleft()
        if qty:
            book.append([direction, qty, price])
    fees = D(0)
    for group in groups.values():
        if len({(x[2], x[3]) for x in group}) != 1:
            raise ValueError("Inconsistent order group")
        fees += charges(
            sum((x[0] for x in group), D(0)), sum((x[0] * x[1] for x in group), D(0)), group[0][2]
        )
    actual = {}
    for p in positions:
        qty, value = D(str(p["qty"])), D(str(p["value"]))
        if not qty.is_finite() or not value.is_finite() or p["symbol"] in actual:
            raise ValueError("Invalid budget position")
        if qty:
            actual[p["symbol"]] = (qty, value)
    expected = {s: sum((d * q for d, q, p in book), D(0)) for s, book in books.items() if book}
    if set(actual) != set(expected) or any(
        abs(actual[s][0] - q) > D(".0000001") for s, q in expected.items()
    ):
        raise ValueError("Budget fills and broker positions require reconciliation")
    unrealized = sum(
        (actual[s][1] - sum((d * q * p for d, q, p in books[s]), D(0)) for s in actual), D(0)
    )
    exit_reserve = sum(
        (charges(abs(q), abs(value), "sell" if q > 0 else "buy") for q, value in actual.values()),
        D(0),
    )
    net = realized + unrealized - fees
    capital = D(str(config["initial_capital"]))
    return {
        "initial_capital": float(capital),
        "equity": float(capital + net),
        "net_pnl": float(net),
        "realized_gross": float(realized),
        "unrealized": float(unrealized),
        "estimated_fees": float(fees),
        "exit_fee_reserve": float(exit_reserve),
        "return_pct": float(net / capital * 100),
        "fills": len(rows),
    }


def evaluate(
    root, broker, account_id, fills, positions, broker_equity, broker_cash, gross, now, commit=True
):
    config = settings(root, broker)
    if config is None:
        return None
    path = folder(root, broker) / "snapshot.json"
    old = json.loads(path.read_text()) if path.exists() else {}
    try:
        if str(config["account_id"]) != str(account_id):
            raise ValueError("Budget account mismatch")
        result = calculate(config, fills, positions)
        numbers = [D(str(x)) for x in (broker_equity, broker_cash, gross)]
        if not all(x.is_finite() for x in numbers) or numbers[0] <= 0 or numbers[2] < 0:
            raise ValueError("Invalid broker funds")
        equity = min(result["equity"], float(broker_equity))
        result["equity"] = equity
        result["available_cash"] = max(
            0, min(float(broker_cash), equity - gross - result["exit_fee_reserve"] - 2.10)
        )
        days = old.get("days", {})
        day = str(now.astimezone(NY).date())
        days.setdefault(
            day, {"baseline": equity if equity > 0 else config["initial_capital"], "halted": False}
        )
        days[day].update(
            equity=equity, net_pnl=result["net_pnl"], estimated_fees=result["estimated_fees"]
        )
        result.update(
            valid=True,
            currency="USD",
            baseline=days[day]["baseline"],
            days=days,
            activated_at=config["activated_at"],
            broker_equity=float(broker_equity),
            broker_cash=float(broker_cash),
        )
    except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
        result = dict(
            old,
            valid=False,
            equity=old.get("equity", config["initial_capital"]),
            baseline=old.get("baseline", config["initial_capital"]),
            available_cash=0,
            error_type=type(exc).__name__,
        )
    result["updated"] = now.isoformat()
    if commit:
        write_json(path, result)
    return result


def public_snapshot(root, broker):
    path = folder(root, broker) / "snapshot.json"
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"valid": False, "error_type": "SnapshotUnavailable"}
    allowed = (
        "valid",
        "initial_capital",
        "equity",
        "net_pnl",
        "realized_gross",
        "unrealized",
        "estimated_fees",
        "return_pct",
        "available_cash",
        "baseline",
        "updated",
        "activated_at",
        "broker_equity",
        "error_type",
    )
    return {k: value[k] for k in allowed if k in value}


class BudgetAccount:
    """Read-only proxy; the real SDK account and broker permissions remain untouched."""

    def __init__(self, account, budget):
        self.original, self.budget = account, budget

    def __getattr__(self, key):
        if key == "equity":
            return self.budget["equity"]
        if key == "last_equity":
            return self.budget["baseline"]
        if key in {"cash", "buying_power"}:
            return min(float(getattr(self.original, key)), self.budget["available_cash"])
        return getattr(self.original, key)


def alpaca_budget(client, root, account, now, commit=True):
    config = settings(root, "alpaca")
    if config is None:
        return None
    from quant_lab.analytics.realized import read_fills
    from quant_lab.brokers.alpaca.execution import gross_exposure, open_orders
    from quant_lab.strategies.scanner import UNIVERSE

    try:
        positions = client.get_all_positions()
        return evaluate(
            root,
            "alpaca",
            account.id,
            read_fills(client),
            [
                dict(symbol=p.symbol, qty=float(p.qty), value=float(p.market_value))
                for p in positions
            ],
            float(account.equity),
            float(account.cash),
            gross_exposure(positions, open_orders(client, UNIVERSE), {"orders": {}}),
            now,
            commit,
        )
    except Exception as exc:
        old = public_snapshot(root, "alpaca") or {
            "equity": config["initial_capital"],
            "baseline": config["initial_capital"],
        }
        return dict(old, valid=False, available_cash=0, error_type=type(exc).__name__)
