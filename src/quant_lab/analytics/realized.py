"""Read-only stock fill accounting. FIFO, gross of fees, with position reconciliation."""

from collections import defaultdict, deque
from datetime import datetime
from decimal import Decimal

from quant_lab.strategies.intraday import NY


def read_fills(client):
    rows, seen, token = [], set(), None
    for _ in range(100):
        params = dict(activity_types="FILL", direction="asc", page_size=100)
        if token:
            params["page_token"] = token
        page = client.get("/account/activities", data=params)
        if not isinstance(page, list):
            raise ValueError("Invalid fill response")
        for row in page:
            if row["id"] not in seen:
                seen.add(row["id"])
                rows.append(row)
        if len(page) < 100:
            return rows
        next_token = page[-1]["id"]
        if token == next_token:
            raise ValueError("Repeated fill cursor")
        token = next_token
    raise ValueError("Fill history limit exceeded")


def summarize(fills, universe, session, holdings):
    lots, closed, seen, markers = defaultdict(deque), [], set(), []
    total = Decimal(0)
    per_symbol = {s: Decimal(0) for s in universe}
    for fill in sorted(fills, key=lambda r: (r["transaction_time"], r["id"])):
        symbol = fill["symbol"]
        if symbol not in universe or fill["id"] in seen:
            continue
        seen.add(fill["id"])
        side = fill["side"]
        if side not in ("buy", "sell", "sell_short"):
            raise ValueError("Unknown fill side")
        qty, price = Decimal(fill["qty"]), Decimal(fill["price"])
        if qty <= 0 or price <= 0:
            raise ValueError("Invalid fill quantity or price")
        direction = 1 if side == "buy" else -1
        time = datetime.fromisoformat(fill["transaction_time"].replace("Z", "+00:00")).astimezone(
            NY
        )
        book = lots[symbol]
        closing_qty = Decimal(0)
        closing_direction = book[0][0] if book else None
        while qty and book and book[0][0] != direction:
            old_direction, remaining, entry = book[0]
            matched = min(qty, remaining)
            closing_qty += matched
            pnl = (price - entry) * matched * old_direction
            if str(time.date()) == str(session):
                total += pnl
                per_symbol[symbol] += pnl
                closed.append(
                    dict(
                        symbol=symbol,
                        direction="多头" if old_direction == 1 else "空头",
                        qty=float(matched),
                        entry=float(entry),
                        exit=float(price),
                        pnl=float(pnl),
                        time=time.strftime("%m-%d %H:%M:%S"),
                    )
                )
            qty -= matched
            book[0][1] -= matched
            if not book[0][1]:
                book.popleft()
        if qty:
            book.append([direction, qty, price])
        if str(time.date()) == str(session):
            for amount, label in (
                (closing_qty, "平多" if closing_direction == 1 else "回补"),
                (qty, "开多" if direction == 1 else "开空"),
            ):
                if amount:
                    markers.append(
                        dict(
                            symbol=symbol,
                            time=time.isoformat(),
                            price=float(price),
                            qty=float(amount),
                            side="buy" if direction == 1 else "sell",
                            label=label,
                        )
                    )
    expected = {s: sum((d * q for d, q, p in lots[s]), Decimal(0)) for s in universe}
    actual = {p["symbol"]: Decimal(str(p["qty"])) for p in holdings}
    complete = all(
        abs(expected[s] - actual.get(s, Decimal(0))) < Decimal("0.000001") for s in universe
    )
    return dict(
        realized_pnl=float(total) if complete else None,
        realized_complete=complete,
        closed_rows=list(reversed(closed))[:100],
        closed_count=len(closed),
        realized_session=str(session),
        realized_method="FIFO · 本股票池 · 未扣费用",
        trade_markers=markers,
        realized_by_symbol={s: float(v) if complete else None for s, v in per_symbol.items()},
    )
