"""Read-only isolated observer. Contains no order submission or cancellation API."""

import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from quant_lab.brokers.discovery import masked, paper_moomoo_rows
from quant_lab.runtime.gateway import opend_kwargs
from quant_lab.runtime.paths import project_home

ROOT = project_home()


def save(path, value):
    from quant_lab.runtime.storage import write_json

    write_json(path, value, durable=False)


def number(value):
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except (ValueError, TypeError):
        return None


def snapshot():
    from moomoo import RET_OK, Currency, OpenSecTradeContext, TrdEnv, TrdMarket

    ctx = OpenSecTradeContext(filter_trdmarket=TrdMarket.US, **opend_kwargs())
    try:
        ret, data = ctx.get_acc_list()
        if ret != RET_OK:
            raise RuntimeError("account query")
        accounts = paper_moomoo_rows(data.to_dict("records"))
        if len(accounts) != 1:
            raise RuntimeError("ambiguous paper account")
        account = int(accounts[0]["acc_id"])
        ret, funds = ctx.accinfo_query(
            trd_env=TrdEnv.SIMULATE, acc_id=account, currency=Currency.USD
        )
        if ret != RET_OK:
            raise RuntimeError("funds query")
        ret, positions = ctx.position_list_query(
            trd_env=TrdEnv.SIMULATE, acc_id=account, currency=Currency.USD
        )
        if ret != RET_OK:
            raise RuntimeError("position query")
        ret, orders = ctx.order_list_query(trd_env=TrdEnv.SIMULATE, acc_id=account)
        if ret != RET_OK:
            raise RuntimeError("order query")
        holdings = [
            dict(
                symbol=str(p["code"]),
                qty=number(p.get("qty")),
                cost=number(p.get("cost_price")),
                value=number(p.get("market_val")),
                pnl=number(p.get("pl_val")),
                managed=False,
            )
            for p in positions.to_dict("records")
            if number(p.get("qty"))
        ]
        boundary = ROOT / "state" / "brokers" / "moomoo_isolation.json"
        identity = masked(account)
        if not boundary.exists():
            save(
                boundary,
                dict(
                    account=identity,
                    excluded_symbols=sorted(p["symbol"] for p in holdings),
                    created=datetime.now(timezone.utc).isoformat(),
                ),
            )
        isolation = json.loads(boundary.read_text(encoding="utf-8"))
        if isolation["account"] != identity:
            raise RuntimeError("paper account changed")
        f = funds.to_dict("records")[0]
        market_path = ROOT / "state" / "brokers" / "quant_lab.brokers.moomoo.market.json"
        market = json.loads(market_path.read_text(encoding="utf-8")) if market_path.exists() else {}
        cleanup_path = ROOT / "state" / "brokers" / "moomoo_legacy_cleanup.json"
        cleanup = (
            json.loads(cleanup_path.read_text(encoding="utf-8")) if cleanup_path.exists() else {}
        )
        auto_path = ROOT / "state" / "brokers" / "moomoo_auto_status.json"
        auto = json.loads(auto_path.read_text(encoding="utf-8")) if auto_path.exists() else {}
        auto_fresh = (
            bool(auto.get("updated"))
            and (
                datetime.now(timezone.utc) - datetime.fromisoformat(auto["updated"])
            ).total_seconds()
            < 45
        )
        auto["fresh"] = auto_fresh
        journal_path = ROOT / "state" / "brokers" / "moomoo_auto_state.json"
        journal = (
            json.loads(journal_path.read_text(encoding="utf-8")) if journal_path.exists() else {}
        )
        from quant_lab.brokers.moomoo.execution import owned_qty, position_qty
        from quant_lab.brokers.moomoo.performance import review

        performance = review(journal.get("fills", []))
        from quant_lab.runtime.budget import folder, public_snapshot, stamp

        budget = public_snapshot(ROOT, "moomoo")
        if budget is not None:
            if budget.get("activated_at"):
                performance = review(
                    [
                        row
                        for row in journal.get("fills", [])
                        if stamp(row["time"]) >= stamp(budget["activated_at"])
                    ]
                )
            f = dict(
                f,
                total_assets=budget.get("equity") if budget.get("valid") else None,
                cash=budget.get("available_cash", 0),
            )
        options_path = ROOT / "state" / "brokers" / "quant_lab.brokers.moomoo.options_watch.json"
        options_watch = (
            json.loads(options_path.read_text(encoding="utf-8")) if options_path.exists() else {}
        )
        curve_path = ROOT / "state" / "brokers" / "moomoo_equity_curve.json"
        if budget is not None:
            curve_path = folder(ROOT, "moomoo") / "equity_curve.json"
        curve = json.loads(curve_path.read_text(encoding="utf-8")) if curve_path.exists() else []
        curve.append(
            dict(time=datetime.now(timezone.utc).isoformat(), equity=number(f.get("total_assets")))
        )
        curve = curve[-1800:]
        save(curve_path, curve)
        for item in holdings:
            match = next(p for p in positions.to_dict("records") if p["code"] == item["symbol"])
            item["managed"] = (
                abs(owned_qty(journal.get("orders", []), item["symbol"]) - position_qty(match))
                < 1e-7
            )
            item["qty"] = position_qty(match)
        auto_on = bool(auto.get("enabled")) and auto_fresh
        clean_rows = [
            {k: o.get(k) for k in ("symbol", "qty", "filled", "average", "status")}
            for o in cleanup.get("orders", {}).values()
        ]
        return dict(
            ok=True,
            mode="SIMULATE / READ ONLY",
            account=identity,
            currency="USD",
            updated=datetime.now(timezone.utc).isoformat(),
            options_watch=options_watch,
            performance=performance,
            strategy_budget=budget,
            equity_curve=curve,
            auto=auto,
            market=market,
            cleanup=clean_rows,
            assets=number(f.get("total_assets")),
            cash=number(f.get("cash")),
            holdings=holdings,
            order_count=len(orders),
            excluded_symbols=isolation["excluded_symbols"],
            automatic_trading=auto_on,
            checks=[
                dict(name="美股模拟账户", ok=True),
                dict(name="资金、持仓、订单读取", ok=True),
                dict(name="原有持仓隔离清单", ok=True),
                dict(
                    name=f"分钟行情读取（{market.get('available', 0)}/{len(market.get('universe', []))}）",
                    ok=bool(market.get("available"))
                    and market.get("available") == len(market.get("universe", [])),
                ),
                dict(name="原生09:31–09:45区间 / 延迟确认", ok=auto_on),
                dict(name="自动执行与订单核对进程", ok=auto_on),
            ],
            note="100股自动模拟已开启；止盈止损依赖本机与OpenD在线，断线期间不能保证即时退出。旧仓清理不计策略收益。"
            if auto_on
            else "自动执行未就绪或心跳过期；请检查OpenD及本机进程。",
        )
    finally:
        ctx.close()


def main():
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--watch", action="store_true")
    args = p.parse_args()
    while True:
        try:
            data = snapshot()
        except Exception as exc:
            data = dict(
                ok=False,
                updated=datetime.now(timezone.utc).isoformat(),
                automatic_trading=False,
                error_type=type(exc).__name__,
                note="连接未通过；停止刷新账户值，请检查OpenD登录。",
            )
        save(ROOT / "state" / "brokers" / "moomoo_snapshot.json", data)
        if not args.watch:
            print(json.dumps(data, ensure_ascii=False))
            return
        time.sleep(15)


if __name__ == "__main__":
    main()
