"""Budget-only whole-share entry economics; targets are scenarios, not forecasts."""

import json
from math import isfinite

from quant_lab.runtime.budget import D, charges
from quant_lab.runtime.storage import write_json
from quant_lab.strategies.intraday import NY


def assess(budget, qty, price, short, spread, gross, daily_loss, stop=0.01, take=0.02, pending=0):
    values = (
        qty,
        price,
        spread,
        gross,
        daily_loss,
        stop,
        take,
        budget["equity"],
        budget["baseline"],
        pending,
    )
    if (
        not all(isfinite(float(v)) for v in values)
        or min(price, stop, take) <= 0
        or min(spread, gross) < 0
        or not 0 < daily_loss <= 0.20
        or min(budget["equity"], budget["baseline"]) <= 0
        or pending < 0
        or not budget.get("valid", True)
    ):
        return dict(allowed=False, reason="INVALID_INPUT")
    if qty <= 0 or qty != int(qty):
        return dict(allowed=False, reason="WHOLE_SHARE_BUDGET")
    if short and budget["equity"] < 2000:
        return dict(allowed=False, reason="SMALL_BUDGET_LONG_ONLY")
    amount = qty * price
    fees = float(
        charges(D(str(qty)), D(str(amount)), "sell" if short else "buy")
        + charges(D(str(qty)), D(str(amount)), "buy" if short else "sell")
    )
    # One spread plus 10 bps round-trip slippage; neither is a guaranteed bound.
    friction = fees + qty * spread + amount * 0.001
    reward, loss = amount * take - friction, amount * stop + friction
    remaining = max(0, budget["equity"] - budget["baseline"] * (1 - daily_loss))
    reserved = gross * stop + budget.get("exit_fee_reserve", 0) + pending * 2.10
    reason = "ELIGIBLE"
    if friction > amount * take * 0.35 or reward < loss:
        reason = "COST_EXCEEDS_EDGE"
    elif loss > budget["equity"] * 0.005:
        reason = "TRADE_RISK_LIMIT"
    elif loss + reserved > remaining:
        reason = "DAILY_RISK_RESERVED"
    return dict(
        allowed=reason == "ELIGIBLE",
        reason=reason,
        notional=amount,
        estimated_round_trip=friction,
        target_net=reward,
        stop_scenario_loss=loss,
        remaining_daily_risk=remaining,
        reserved_risk=reserved,
    )


def record(root, broker, symbol, result, now):
    path = root / "state/capital_guard" / (broker + ".json")
    old = json.loads(path.read_text()) if path.exists() else {}
    day = str(now.astimezone(NY).date())
    if old.get("day") != day:
        old = dict(day=day, checks=0, reasons={})
    old["checks"] += 1
    reason = result["reason"]
    old["reasons"][reason] = old["reasons"].get(reason, 0) + 1
    old.update(updated=now.isoformat(), latest=dict(symbol=symbol, **result))
    write_json(path.parent / broker / (day + ".json"), old)
    write_json(path, old)


def public_status(root, broker):
    path = root / "state/capital_guard" / (broker + ".json")
    if not path.exists():
        return dict(checks=0, reasons={})
    return json.loads(path.read_text())
