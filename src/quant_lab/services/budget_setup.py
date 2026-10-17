"""Activate independent stock budgets only while all workers and accounts are flat."""

import argparse
import contextlib
import io
import json
import os
from datetime import datetime, timezone
from math import isfinite

from quant_lab.runtime.budget import calculate, folder, settings
from quant_lab.runtime.paths import project_home
from quant_lab.runtime.storage import write_json
from quant_lab.services.automation import InstanceLock, inventory
from quant_lab.strategies.intraday import NY, timeout_request


def check_accounts(root):
    from dotenv import load_dotenv

    load_dotenv(root / ".env", override=False)
    config = json.loads((root / "state/automation_config.json").read_text(encoding="utf-8-sig"))
    result = {}
    if config.get("alpaca_enabled"):
        from alpaca.trading.client import TradingClient
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        from quant_lab.brokers.alpaca.execution import TERMINAL, require_paper

        client = TradingClient(os.environ["ALPACA_KEY"], os.environ["ALPACA_SECRET"], paper=True)
        client._session.request = timeout_request(client._session.request)
        client._retry = 0
        require_paper(client)
        account = client.get_account()
        auth = json.loads((root / "state/paper_authorization.json").read_text())
        if (
            not auth.get("authorized")
            or not auth.get("paper_only")
            or str(account.id) != config.get("alpaca_account_id")
            or str(account.id) != auth.get("account_id")
        ):
            raise ValueError("Alpaca paper binding mismatch")
        if client.get_all_positions() or client.get_orders(
            filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500)
        ):
            raise ValueError("Alpaca must be flat with no pending orders")
        path = root / "state" / f"paper_intents_{auth['session']}.json"
        if path.exists():
            for cid, row in json.loads(path.read_text()).get("orders", {}).items():
                if row.get("status") not in TERMINAL:
                    status = client.get_order_by_client_id(cid).status
                    if getattr(status, "value", status) not in TERMINAL:
                        raise ValueError("Unresolved Alpaca intent")
        result["alpaca"] = (str(account.id), float(account.equity), float(account.cash))
    if config.get("moomoo_enabled"):
        import moomoo as sdk

        from quant_lab.brokers.discovery import paper_moomoo_rows
        from quant_lab.brokers.moomoo.execution import TERMINAL
        from quant_lab.runtime.gateway import opend_kwargs

        ctx = sdk.OpenSecTradeContext(filter_trdmarket=sdk.TrdMarket.US, **opend_kwargs())
        try:

            def checked(value):
                code, data = value
                if code != sdk.RET_OK:
                    raise RuntimeError("OpenD read failed")
                return data

            rows = paper_moomoo_rows(checked(ctx.get_acc_list()).to_dict("records"))
            auth = json.loads((root / "state/brokers/moomoo_auto_config.json").read_text())
            if len(rows) != 1 or str(rows[0]["acc_id"]) != str(auth.get("account_id")):
                raise ValueError("Moomoo paper binding mismatch")
            aid = int(rows[0]["acc_id"])
            pos = checked(
                ctx.position_list_query(trd_env=sdk.TrdEnv.SIMULATE, acc_id=aid, refresh_cache=True)
            )
            orders = checked(
                ctx.order_list_query(trd_env=sdk.TrdEnv.SIMULATE, acc_id=aid, refresh_cache=True)
            ).to_dict("records")
            if any(float(r["qty"]) for r in pos.to_dict("records")) or any(
                str(r["order_status"]) not in TERMINAL for r in orders
            ):
                raise ValueError("Moomoo must be flat with no pending orders")
            ledger = json.loads((root / "state/brokers/moomoo_auto_state.json").read_text())
            for row in ledger.get("orders", []):
                if row.get("status") not in TERMINAL:
                    matches = [
                        r
                        for r in orders
                        if str(r["order_id"]) == str(row.get("order_id"))
                        or str(r.get("remark", "")) == row.get("remark")
                    ]
                    if len(matches) != 1 or str(matches[0]["order_status"]) not in TERMINAL:
                        raise ValueError("Unresolved moomoo intent")
            funds = checked(
                ctx.accinfo_query(
                    trd_env=sdk.TrdEnv.SIMULATE, acc_id=aid, currency=sdk.Currency.USD
                )
            ).iloc[0]
            result["moomoo"] = (str(aid), float(funds["total_assets"]), float(funds["cash"]))
        finally:
            ctx.close()
    if not result:
        raise ValueError("No enabled paper broker")
    return result


def activate(root, capital, checks=check_accounts):
    target = root / "state/strategy_budget/config.json"
    if target.exists():
        raise ValueError("An existing budget cannot be reset or overwritten")
    if inventory(root):
        raise RuntimeError("Stop workers before budget activation")
    capital = float(capital)
    if not 0 < capital <= 1000000:
        raise ValueError("Invalid capital")
    accounts = checks(root)
    now = datetime.now(timezone.utc)
    configs = {}
    for broker, (identity, equity, cash) in accounts.items():
        if not all(isfinite(float(v)) for v in (equity, cash)) or min(equity, cash) < capital:
            raise ValueError("Broker cash cannot fund the strategy capital")
        config = {
            "schema_version": 1,
            "currency": "USD",
            "initial_capital": capital,
            "account_id": identity,
            "activated_at": now.isoformat(),
        }
        configs[broker] = config
    if inventory(root):
        raise RuntimeError("Workers restarted during activation")
    for broker, config in configs.items():
        equity, cash = accounts[broker][1:]
        snapshot = calculate(config, [], [])
        snapshot.update(
            valid=True,
            baseline=capital,
            available_cash=capital - 2.10,
            broker_equity=equity,
            broker_cash=cash,
            updated=now.isoformat(),
            activated_at=now.isoformat(),
            days={str(now.astimezone(NY).date()): {"baseline": capital, "halted": False}},
        )
        write_json(folder(root, broker) / "snapshot.json", snapshot)
    write_json(target, {"schema_version": 1, "brokers": configs})
    return {
        "mode": "PAPER_ONLY",
        "initial_capital_each": capital,
        "brokers": list(configs),
        "stock_only": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capital", type=float, required=True)
    args = parser.parse_args()
    root = project_home()
    with InstanceLock(root / "state/strategy_budget_setup.lock"):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = activate(root, args.capital)
    print(json.dumps(result))
