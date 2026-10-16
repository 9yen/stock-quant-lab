"""Daily closed-session evaluation; optional evidence-gated paper stock parameter promotion."""

import argparse
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from quant_lab.analytics.optimizer import (
    MIN_DAYS,
    MIN_LIVE_STOCK_CLOSES,
    MODEL_VERSION,
    PROFILES,
    active_profile,
    archive_chart,
    choose,
    deployment_safe,
    load_days,
    promote,
    read,
    replay,
    write,
)
from quant_lab.runtime.paths import project_home

ROOT = project_home()
NY = ZoneInfo("America/New_York")


def calendar_sessions(root, now):
    """Only quote/calendar reads. A connection failure does not invent a calendar."""
    from moomoo import RET_OK, OpenQuoteContext, TrdMarket

    q = OpenQuoteContext(host="127.0.0.1", port=11111)
    try:
        ret, days = q.request_trading_days(
            market=TrdMarket.US,
            start=str(now.date() - timedelta(days=7)),
            end=str(now.date() + timedelta(days=14)),
        )
        if ret != RET_OK:
            raise RuntimeError("Official trading calendar unavailable")
        for item in days:
            write(root / "state" / "optimizer" / "calendars" / (item["time"] + ".json"), item)
        return days
    finally:
        q.close()


def summarize_brokers(root):
    journal = read(root / "state" / "brokers" / "moomoo_auto_state.json", {})
    exits = [
        o
        for o in journal.get("orders", [])
        if o.get("kind") == "EXIT"
        and o.get("instrument", "STOCK") == "STOCK"
        and o.get("filled", 0) > 0
    ]
    from quant_lab.brokers.moomoo.performance import review

    moo = review(journal.get("fills", []))
    alpaca = []
    for p in sorted((root / "state" / "reviews").glob("????-??-??.json")):
        r = read(p, {})
        if r:
            alpaca.append(
                {
                    k: r.get(k)
                    for k in (
                        "day",
                        "closed_net",
                        "fees",
                        "model_return_pct",
                        "reconciled",
                        "strategy_observations",
                    )
                }
            )
    return dict(
        moomoo_stock_exit_orders=len(exits),
        moomoo_gross=moo["gross_realized"],
        moomoo_closed_pairs=len(moo["closed"]),
        alpaca_days=len(alpaca),
        alpaca_latest=alpaca[-1] if alpaca else None,
        note="券商、策略版本及股票/期权分开核对；旧仓清理排除。moomoo实际成交毛利未扣费用，模型股票回放已估算费用。",
    )


def run(root, auto_apply=False, now=None, calendar=None):
    now = now or datetime.now(NY)
    base = root / "state" / "optimizer"
    errors = []
    try:
        sessions = calendar if calendar is not None else calendar_sessions(root, now)
    except Exception as exc:
        sessions = [read(p, {}) for p in (base / "calendars").glob("????-??-??.json")]
        errors.append(type(exc).__name__)
    today = next((r for r in sessions if r.get("time") == str(now.date())), None)
    if today:
        for p in (root / "state" / "brokers" / "moomoo_charts").glob("US.*.json"):
            chart = read(p, {})
            try:
                archive_chart(root, chart, today["trade_date_type"], now)
            except (ValueError, KeyError, TypeError):
                errors.append("Invalid chart")
    days, skipped = load_days(root)
    broker = summarize_brokers(root)
    current = active_profile(root, now.date())
    active = read(base / "active.json", {})
    next_day = next(
        (
            r["time"]
            for r in sorted(sessions, key=lambda x: x.get("time", ""))
            if r.get("time", "") > str(now.date())
        ),
        None,
    )
    report = dict(
        updated=now.isoformat(),
        enabled=True,
        auto_apply=auto_apply,
        status="ACCUMULATING",
        message="样本不足，保持当前参数",
        model=MODEL_VERSION,
        valid_days=len(days),
        minimum_days=MIN_DAYS,
        live_stock_closes=broker["moomoo_stock_exit_orders"],
        minimum_live_closes=MIN_LIVE_STOCK_CLOSES,
        profile=current,
        pending_profile=active.get("profile")
        if active.get("effective_from", "") > str(now.date())
        else None,
        effective_from=active.get("effective_from"),
        broker_review=broker,
        skipped=skipped,
        errors=errors,
        profiles=PROFILES,
        risk_changes=False,
        options_tuned=False,
        alpaca_tuned=False,
        limitations="仅优化moomoo股票入场筛选；OHLC回放不是逐笔撮合，未模拟借券限制、精确买卖价差或期权成交。滚动分段评估仍可能过拟合。仓位、止损、杠杆及权限不自动放宽。",
    )
    if report["pending_profile"]:
        report.update(status="PENDING_NEXT_SESSION", message="已排定下个交易日生效，盘中不改参数")
    elif len(days) >= MIN_DAYS and broker["moomoo_stock_exit_orders"] >= MIN_LIVE_STOCK_CLOSES:
        selected = dict(list(days.items())[-MIN_DAYS:])
        keys = list(selected)
        train = {k: selected[k] for k in keys[:20]}
        validation = {k: selected[k] for k in keys[20:25]}
        holdout = {k: selected[k] for k in keys[25:]}
        results = {
            p: dict(
                train=replay(train, p),
                validation=replay(validation, p),
                holdout=replay(holdout, p),
                stress=replay(holdout, p, slippage=0.001),
            )
            for p in PROFILES
        }
        winner, reason = choose(train, validation, holdout, results, current)
        report.update(
            status="KEEP_CURRENT",
            message=reason,
            candidate=winner,
            splits=dict(train=list(train), validation=list(validation), holdout=list(holdout)),
            results=results,
        )
        # Canary proxy rollback: three paired days worse, total deficit >=0.5% normalized equity.
        rollout = {k: v for k, v in days.items() if active.get("effective_from", "9999") <= k}
        if current != "baseline" and len(rollout) >= 3:
            last = dict(list(rollout.items())[-3:])
            a = replay(last, current)
            b = replay(last, "baseline")
            if (
                a.get("valid")
                and b.get("valid")
                and all(x["net"] < y["net"] for x, y in zip(a["days"], b["days"]))
                and a["net"] < b["net"] - a["initial"] * 0.005
            ):
                winner = "baseline"
                report.update(
                    candidate=winner, message="三日配对回放明显落后，恢复基准筛选", rollback=True
                )
        if winner:
            cooldown = current != "baseline" and len(rollout) < 5 and not report.get("rollback")
            evidence = dict(
                data_through=keys[-1],
                split=report["splits"],
                model=MODEL_VERSION,
                reason=report["message"],
            )
            if auto_apply and next_day and not cooldown and deployment_safe(root, now):
                if promote(root, winner, next_day, now, evidence):
                    report.update(
                        status="ROLLED_BACK" if report.get("rollback") else "PROMOTED",
                        pending_profile=winner,
                        effective_from=next_day,
                    )
            else:
                report.update(
                    status="CANDIDATE_DEFERRED",
                    message=report["message"] + "；等待空仓、交易日或版本冷却确认",
                )
    signature = hashlib.sha256(
        json.dumps(
            dict(days=list(days), profile=current, closes=broker["moomoo_stock_exit_orders"]),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    report["data_signature"] = signature
    write(base / "latest.json", report)
    write(base / "reports" / (str(now.date()) + ".json"), report)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--auto-apply", action="store_true")
    args = parser.parse_args()
    result = run(ROOT, args.auto_apply)
    print(
        json.dumps(
            {
                k: result.get(k)
                for k in (
                    "status",
                    "message",
                    "valid_days",
                    "minimum_days",
                    "live_stock_closes",
                    "minimum_live_closes",
                    "profile",
                    "pending_profile",
                    "effective_from",
                )
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
