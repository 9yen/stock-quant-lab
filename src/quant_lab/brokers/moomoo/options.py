"""Long premium only: standard 100-share US calls/puts, one contract."""

import math
from datetime import date, datetime

from quant_lab.brokers.moomoo.policy import OPTION_PREMIUM_CAP, OPTION_PREMIUM_FRACTION


def option_budget(equity):
    return min(OPTION_PREMIUM_CAP, equity * OPTION_PREMIUM_FRACTION)


def valid_contract(row, underlying, direction, today):
    try:
        dte = (date.fromisoformat(str(row["strike_time"])[:10]) - today).days
        return (
            row["stock_owner"] == underlying
            and row["option_type"] == ("CALL" if direction == "LONG" else "PUT")
            and row.get("option_standard_type") == "STANDARD"
            and float(row["lot_size"]) == 100
            and not row.get("suspension", False)
            and 7 <= dte <= 21
        )
    except (KeyError, ValueError, TypeError):
        return False


def premium_limit(row, equity, cash, now):
    try:
        bid, ask = float(row["bid_price"]), float(row["ask_price"])
        stamp = datetime.fromisoformat(row["update_time"]).replace(tzinfo=now.tzinfo)
        limit = round(math.ceil(ask * 1.01 / 0.05) * 0.05, 2)
        if (
            not all(math.isfinite(v) and v > 0 for v in (bid, ask))
            or ask < bid
            or (ask - bid) / bid > 0.08
            or not -5 <= (now - stamp).total_seconds() <= 60
            or float(row.get("option_contract_size", 0)) != 100
            or float(row.get("option_open_interest", 0)) < 100
            or float(row.get("volume", 0)) < 10
            or limit * 100 > min(option_budget(equity), cash)
        ):
            return None
        return limit
    except (KeyError, ValueError, TypeError):
        return None


def select_contract(q, sdk, underlying, direction, price, equity, cash, now):
    from datetime import timedelta

    ret, expiries = q.get_option_expiration_date(underlying)
    if ret != sdk.RET_OK:
        return None
    dates = sorted(
        str(r["strike_time"])[:10]
        for r in expiries.to_dict("records")
        if 7 <= (date.fromisoformat(str(r["strike_time"])[:10]) - now.date()).days <= 21
    )
    if not dates:
        return None
    expiry = dates[0]
    ret, chain = q.get_option_chain(
        underlying,
        start=expiry,
        end=expiry,
        option_type=sdk.OptionType.CALL if direction == "LONG" else sdk.OptionType.PUT,
    )
    if ret != sdk.RET_OK:
        return None
    rows = [
        r for r in chain.to_dict("records") if valid_contract(r, underlying, direction, now.date())
    ]
    rows.sort(key=lambda r: abs(float(r["strike_price"]) - price))
    rows = rows[:3]
    if not rows:
        return None
    ret, quotes = q.get_market_snapshot([r["code"] for r in rows])
    if ret != sdk.RET_OK:
        return None
    by = {r["code"]: r for r in quotes.to_dict("records")}
    for r in rows:
        limit = premium_limit(by.get(r["code"], {}), equity, cash, now)
        if limit:
            return dict(
                code=r["code"],
                price=limit,
                qty=1,
                multiplier=100,
                instrument="OPTION",
                underlying=underlying,
                option_type=r["option_type"],
                expiry=expiry,
                strike=float(r["strike_price"]),
            )
    return None


def option_exit(entry, bid, elapsed):
    if not all(math.isfinite(x) and x > 0 for x in (entry, bid)):
        return "DATA_HOLD"
    if bid <= entry * 0.75:
        return "OPTION_STOP_25"
    if bid >= entry * 1.4:
        return "OPTION_TAKE_40"
    if elapsed >= 30:
        return "OPTION_TIME_30M"
    return "HOLD"
