"""Multi-symbol paper-only ORB executor. One writer, durable intents, reconciliation before entries."""

import json
import math
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from quant_lab.brokers.discovery import masked, paper_moomoo_rows
from quant_lab.brokers.moomoo.market import CODES, analyze, exit_decision
from quant_lab.brokers.moomoo.policy import (
    COOLDOWN_MINUTES,
    DAILY_ENTRIES,
    DAILY_LOSS,
    GROSS,
    MAX_POSITIONS,
    OPTION_CONCURRENT,
    OPTION_DAILY,
    PER_SYMBOL_ENTRIES,
    STOCK_CAP,
    TARGET,
)
from quant_lab.runtime.gateway import opend_kwargs
from quant_lab.runtime.paths import project_home

ROOT = project_home()
DIR = ROOT / "state" / "brokers"
NY = ZoneInfo("America/New_York")

TERMINAL = {
    "FILLED_ALL",
    "CANCELLED_ALL",
    "CANCELLED_PART",
    "FAILED",
    "SUBMIT_FAILED",
    "DISABLED",
    "DELETED",
}


def write(path, value):
    from quant_lab.runtime.storage import write_json

    write_json(path, value, durable=True)


def read(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def fresh_quote(row, now, max_spread=0.003):
    try:
        bid, ask = float(row["bid_price"]), float(row["ask_price"])
        stamp = datetime.fromisoformat(row["update_time"]).replace(tzinfo=NY)
        return (
            all(math.isfinite(v) and v > 0 for v in (bid, ask))
            and bid <= ask
            and (ask - bid) / bid <= max_spread
            and -5 <= (now - stamp).total_seconds() <= 60
        )
    except (ValueError, KeyError, TypeError):
        return False


def position_qty(p):
    qty = float(p["qty"])
    side = str(p.get("position_side", ""))
    if side not in ("LONG", "SHORT") or not math.isfinite(qty):
        raise ValueError("Unknown position direction")
    return abs(qty) * (1 if side == "LONG" else -1)


def owned_qty(orders, code):
    return sum(
        float(o.get("filled", 0)) * (1 if o["side"] == "BUY" else -1)
        for o in orders
        if o["symbol"] == code
    )


def active(orders, code=None):
    return [
        o for o in orders if o["status"] not in TERMINAL and (code is None or o["symbol"] == code)
    ]


def reserve(positions, orders):
    gross = sum(abs(float(p["market_val"])) for p in positions)
    gross += sum(
        max(0, o["qty"] - o.get("filled", 0)) * o["price"] * o.get("multiplier", 1)
        for o in active(orders)
        if o["kind"] == "ENTRY"
    )
    return gross


def entry_qty(equity, price, gross, cash, short=False):
    if not all(math.isfinite(v) for v in (equity, price, gross, cash)) or equity <= 0 or price <= 0:
        return 0
    budget = min(equity * TARGET, max(0, equity * GROSS - gross), max(0, cash))
    return max(0, math.floor(budget / (price * 1.005)))


class Executor:
    def __init__(self, trade, quote, sdk):
        self.t, self.q, self.s = trade, quote, sdk
        self.path = DIR / "moomoo_auto_state.json"
        self.state = read(self.path, dict(orders=[], days={}))
        self.aid = None

    def persist(self):
        write(self.path, self.state)

    def checked(self, result):
        ret, data = result
        if ret != self.s.RET_OK:
            raise RuntimeError("Broker API rejected request")
        return data

    def positions(self):
        rows = [
            r
            for r in self.checked(
                self.t.position_list_query(
                    trd_env=self.s.TrdEnv.SIMULATE, acc_id=self.aid, refresh_cache=True
                )
            ).to_dict("records")
            if float(r["qty"])
        ]
        return [dict(r, qty=position_qty(r)) for r in rows]

    def submit(self, code, qty, side, kind, price, bar, reason, now, contract=None):
        if contract and contract.get("instrument") == "OPTION":
            if contract.get("multiplier") != 100 or (
                kind == "ENTRY" and (side != "BUY" or qty != 1)
            ):
                raise ValueError("Only one-contract long option entries allowed")
        key = f"{code}:{bar}:{side}:{kind}"
        if any(o["key"] == key for o in self.state["orders"]):
            return
        if active(self.state["orders"], code):
            return
        intent = dict(
            key=key,
            remark="ql-" + uuid.uuid4().hex[:24],
            symbol=code,
            qty=qty,
            side=side,
            kind=kind,
            price=price,
            bar=bar,
            reason=reason,
            created=now.isoformat(),
            day=str(now.date()),
            status="SUBMITTING",
            filled=0,
            average=0,
        )
        if contract:
            intent.update(
                {
                    k: v
                    for k, v in contract.items()
                    if k
                    in ("multiplier", "instrument", "underlying", "option_type", "expiry", "strike")
                }
            )
        self.state["orders"].append(intent)
        self.persist()  # durable before broker call
        try:
            ret, data = self.t.place_order(
                price=price,
                qty=qty,
                code=code,
                trd_side=self.s.TrdSide.BUY if side == "BUY" else self.s.TrdSide.SELL,
                order_type=self.s.OrderType.MARKET if kind == "EXIT" else self.s.OrderType.NORMAL,
                trd_env=self.s.TrdEnv.SIMULATE,
                acc_id=self.aid,
                time_in_force=self.s.TimeInForce.DAY,
                session=self.s.Session.RTH,
                remark=intent["remark"],
            )
            if ret != self.s.RET_OK:
                intent["status"] = "SUBMIT_FAILED"
            else:
                row = data.iloc[0]
                self.apply_fill(intent, row)
        except Exception:
            intent["status"] = "UNKNOWN"  # no blind retries; reconcile by remark
        self.persist()

    def apply_fill(self, o, row):
        filled, average = float(row["dealt_qty"]), float(row["dealt_avg_price"])
        old = float(o.get("filled", 0))
        delta = filled - old
        if not old <= filled <= o["qty"] or (
            filled and (not math.isfinite(average) or average <= 0)
        ):
            raise RuntimeError("Invalid fill")
        if delta:
            price = (filled * average - old * o.get("average", 0)) / delta
            if not math.isfinite(price) or price <= 0:
                raise RuntimeError("Invalid cumulative average")
            stamp = str(row.get("updated_time", o["created"]))
            if "T" not in stamp:
                stamp = datetime.fromisoformat(stamp).replace(tzinfo=NY).isoformat()
            self.state.setdefault("fills", []).append(
                dict(
                    id=o["remark"] + ":" + str(filled),
                    symbol=o["symbol"],
                    side=o["side"],
                    qty=delta,
                    price=price,
                    time=stamp,
                    kind=o["kind"],
                    instrument=o.get("instrument", "STOCK"),
                    multiplier=o.get("multiplier", 1),
                    underlying=o.get("underlying", o["symbol"]),
                )
            )
        o.update(
            order_id=str(row["order_id"]),
            status=str(row["order_status"]),
            filled=filled,
            average=average,
        )

    def reconcile(self, broker):
        for o in self.state["orders"]:
            if o["status"] in TERMINAL:
                continue
            matches = [
                r
                for r in broker
                if str(r["order_id"]) == o.get("order_id")
                or str(r.get("remark", "")) == o["remark"]
            ]
            if len(matches) != 1:
                o["status"] = "UNKNOWN"
                continue
            row = matches[0]
            filled = float(row["dealt_qty"])
            if not o.get("filled", 0) <= filled <= o["qty"]:
                raise RuntimeError("Invalid fill quantity")
            self.apply_fill(o, row)
        self.persist()

    def cycle(self, now=None):
        now = now or datetime.now(NY)
        from quant_lab.analytics.optimizer import active_profile, entry_filter

        profile = active_profile(ROOT, now.date())
        auth = read(DIR / "moomoo_auto_config.json", {})
        rows = paper_moomoo_rows(self.checked(self.t.get_acc_list()).to_dict("records"))
        if len(rows) != 1 or str(rows[0]["acc_id"]) != str(auth.get("account_id")):
            raise RuntimeError("Paper account mismatch")
        self.aid = int(rows[0]["acc_id"])
        if self.state.get("account_id", str(self.aid)) != str(self.aid):
            raise RuntimeError("Journal account mismatch")
        self.state["account_id"] = str(self.aid)
        broker = self.checked(
            self.t.order_list_query(
                trd_env=self.s.TrdEnv.SIMULATE, acc_id=self.aid, refresh_cache=True
            )
        ).to_dict("records")
        self.reconcile(broker)
        positions = self.positions()
        funds = self.checked(
            self.t.accinfo_query(
                trd_env=self.s.TrdEnv.SIMULATE, acc_id=self.aid, currency=self.s.Currency.USD
            )
        ).iloc[0]
        equity, cash = float(funds["total_assets"]), float(funds["cash"])
        if not math.isfinite(equity) or equity <= 0:
            raise RuntimeError("Invalid equity")
        day = self.state["days"].setdefault(str(now.date()), dict(baseline=equity, halted=False))
        if equity <= day["baseline"] * (1 - DAILY_LOSS):
            day["halted"] = True
        self.persist()
        calendar = self.checked(
            self.q.request_trading_days(
                market=self.s.TrdMarket.US, start=str(now.date()), end=str(now.date())
            )
        )
        today = [r for r in calendar if r["time"] == str(now.date())]
        common = dict(
            enabled=bool(auth.get("enabled")),
            automatic_trading=bool(auth.get("enabled")),
            account=masked(self.aid),
            options_enabled=bool(auth.get("options_enabled")),
            tuning_profile=profile,
            updated=now.isoformat(),
            positions=len(positions),
            daily_entries_used=sum(
                o["kind"] == "ENTRY" and o.get("day") == str(now.date())
                for o in self.state["orders"]
            ),
            daily_halted=day["halted"],
            baseline=day["baseline"],
            equity=equity,
            risk=dict(
                per_stock=TARGET,
                stock_cap=STOCK_CAP,
                gross=GROSS,
                max_positions=MAX_POSITIONS,
                per_symbol_entries=PER_SYMBOL_ENTRIES,
                daily_entries=DAILY_ENTRIES,
                cooldown=COOLDOWN_MINUTES,
                stop=0.01,
                take=0.02,
                daily_loss=DAILY_LOSS,
                option_concurrent=OPTION_CONCURRENT,
                option_daily=OPTION_DAILY,
                universe=CODES,
            ),
            orders=[
                {
                    k: o.get(k)
                    for k in (
                        "symbol",
                        "kind",
                        "side",
                        "qty",
                        "filled",
                        "average",
                        "status",
                        "reason",
                        "created",
                        "instrument",
                        "underlying",
                        "option_type",
                        "expiry",
                    )
                }
                for o in self.state["orders"][-250:]
            ],
        )
        if not today:
            return common | dict(status="WAIT_TRADING_DAY")
        kind = str(today[0]["trade_date_type"])
        if kind not in ("WHOLE", "HALF"):
            return common | dict(status="UNKNOWN_CALENDAR")
        closed = now.replace(hour=13 if kind == "HALF" else 16, minute=0, second=0, microsecond=0)
        states = self.checked(self.q.get_market_state(["US.AAPL"])).to_dict("records")
        regular = all(str(r["market_state"]) in ("MORNING", "AFTERNOON") for r in states)
        if len(states) != 1 or not regular:
            return common | dict(status="WAIT_REGULAR_SESSION")
        if not auth.get("enabled"):
            return common | dict(status="DISABLED")
        if (ROOT / "STOP_MOOMOO").exists():
            day["halted"] = True
            self.persist()
        # Cancel stale entries before any replacement/exit. Unconfirmed cancellation keeps reservations.
        for o in active(self.state["orders"]):
            age = (now - datetime.fromisoformat(o["created"])).total_seconds()
            if o.get("order_id") and (
                (
                    o["kind"] == "ENTRY"
                    and (age > 60 or day["halted"] or now >= closed - timedelta(minutes=30))
                )
                or (o["kind"] == "EXIT" and age > 30)
            ):
                self.checked(
                    self.t.modify_order(
                        self.s.ModifyOrderOp.CANCEL,
                        o["order_id"],
                        0,
                        0,
                        trd_env=self.s.TrdEnv.SIMULATE,
                        acc_id=self.aid,
                    )
                )
                return common | dict(status="WAIT_CANCEL_CONFIRMATION")
        tracked_options = {
            o["symbol"] for o in self.state["orders"] if o.get("instrument") == "OPTION"
        }
        snaps = self.checked(
            self.q.get_market_snapshot(
                list(
                    dict.fromkeys(
                        CODES + [p["code"] for p in positions if p["code"] in tracked_options]
                    )
                )
            )
        ).to_dict("records")
        quotes = {r["code"]: r for r in snaps}
        actual = {p["code"]: float(p["qty"]) for p in positions}
        mismatched = [
            code
            for code in set(actual) | set(CODES) | tracked_options
            if abs(actual.get(code, 0) - owned_qty(self.state["orders"], code)) > 1e-7
        ]
        # Only exactly reconciled script positions can be sold/bought back.
        for p in positions:
            code = p["code"]
            qty = float(p["qty"])
            if (
                code not in set(CODES) | tracked_options
                or code in mismatched
                or active(self.state["orders"], code)
            ):
                continue
            quote = quotes.get(code, {})
            price = float(quote.get("bid_price" if qty > 0 else "ask_price", 0))
            metadata = next(
                (
                    o
                    for o in reversed(self.state["orders"])
                    if o["symbol"] == code and o["kind"] == "ENTRY"
                ),
                {},
            )
            is_option = metadata.get("instrument") == "OPTION"
            if is_option:
                from quant_lab.brokers.moomoo.options import option_exit

                elapsed = (now - datetime.fromisoformat(metadata["created"])).total_seconds() / 60
                stop = (
                    option_exit(float(p["cost_price"]), price, elapsed)
                    if fresh_quote(quote, now, 0.15)
                    else "DATA_HOLD"
                )
            else:
                stop = exit_decision(
                    "LONG" if qty > 0 else "SHORT",
                    float(p["cost_price"]),
                    price if price > 0 else 1,
                    fresh_quote(quote, now),
                    True,
                )
            reason = (
                "DAILY_RISK"
                if day["halted"]
                else "PRECLOSE"
                if now >= closed - timedelta(minutes=10)
                else "POSITION_CAP"
                if not is_option and abs(float(p["market_val"])) > equity * STOCK_CAP
                else "OPTION_TIME_30M"
                if is_option and elapsed >= 30
                else stop
            )
            if reason not in ("HOLD", "DATA_HOLD"):
                self.submit(
                    code,
                    abs(qty),
                    "SELL" if qty > 0 else "BUY",
                    "EXIT",
                    0,
                    now.isoformat(),
                    reason,
                    now,
                    metadata if is_option else None,
                )
                return common | dict(status="EXIT_SUBMITTED")
        if day["halted"] or now >= closed - timedelta(minutes=30):
            return common | dict(status="ENTRY_WINDOW_CLOSED")
        if mismatched or any(o["status"] == "UNKNOWN" for o in self.state["orders"]):
            return common | dict(status="RECONCILIATION_HOLD")
        # Block new entries if any unrecognized broker order is active, including manual orders.
        known = {o.get("order_id") for o in self.state["orders"]}
        if any(
            str(o["order_id"]) not in known and str(o["order_status"]) not in TERMINAL
            for o in broker
        ):
            return common | dict(status="EXTERNAL_ORDER_HOLD")
        entries = [
            o
            for o in self.state["orders"]
            if o["kind"] == "ENTRY" and o.get("day") == str(now.date())
        ]
        if len(entries) >= DAILY_ENTRIES:
            return common | dict(status="DAILY_ENTRY_LIMIT")
        scanner = read(DIR / "quant_lab.brokers.moomoo.market.json", {})
        candidates = {
            r["symbol"]: r
            for r in scanner.get("rows", [])
            if r.get("symbol") in CODES
            and r.get("signal") in ("LONG", "SHORT")
            and r.get("updated")
            and -5 <= (now - datetime.fromisoformat(r["updated"])).total_seconds() <= 45
        }
        occupied = {p["code"] for p in positions} | {
            o["symbol"] for o in active(self.state["orders"])
        }
        if len(occupied) >= MAX_POSITIONS:
            return common | dict(status="POSITION_LIMIT")
        for code in CODES:
            if any(p["code"] == code for p in positions) or active(self.state["orders"], code):
                continue
            if any(
                owned_qty(self.state["orders"], o["symbol"])
                or active(self.state["orders"], o["symbol"])
                for o in self.state["orders"]
                if o.get("instrument") == "OPTION" and o.get("underlying") == code
            ):
                continue
            own = [o for o in entries if o.get("underlying", o["symbol"]) == code]
            if len(own) >= PER_SYMBOL_ENTRIES or (
                own
                and now - datetime.fromisoformat(own[-1]["created"])
                < timedelta(minutes=COOLDOWN_MINUTES)
            ):
                continue
            row = candidates.get(code)
            if not row:
                continue
            if (
                not row.get("last_bar")
                or not 30
                <= (
                    now - (datetime.fromisoformat(row["last_bar"]) + timedelta(minutes=1))
                ).total_seconds()
                <= 150
            ):
                continue
            if row["signal"] not in ("LONG", "SHORT"):
                continue
            high, low, atr = row["range_high"], row["range_low"], row["atr14"]
            if not high or not low or not atr or (high - low) / low > 0.03:
                continue
            if abs(row["price"] - (high if row["signal"] == "LONG" else low)) > 1.5 * atr:
                continue
            quote = quotes.get(code, {})
            if not fresh_quote(quote, datetime.now(NY)):
                continue
            mid = (float(quote["bid_price"]) + float(quote["ask_price"])) / 2
            if (
                abs(mid / row["price"] - 1) > 0.005
                or (row["signal"] == "LONG" and mid <= high)
                or (row["signal"] == "SHORT" and mid >= low)
            ):
                continue
            option = None
            if (
                auth.get("options_enabled")
                and len(
                    {p["code"] for p in positions if p["code"] in tracked_options}
                    | {
                        o["symbol"]
                        for o in active(self.state["orders"])
                        if o.get("instrument") == "OPTION"
                    }
                )
                < OPTION_CONCURRENT
                and sum(o.get("instrument") == "OPTION" for o in entries) < OPTION_DAILY
            ):
                from quant_lab.brokers.moomoo.options import select_contract

                try:
                    option = select_contract(
                        self.q, self.s, code, row["signal"], row["price"], equity, cash, now
                    )
                except Exception:
                    option = None
            if option:
                if (
                    reserve(positions, self.state["orders"]) + option["price"] * 100
                    <= equity * GROSS
                ):
                    limits = self.checked(
                        self.t.acctradinginfo_query(
                            self.s.OrderType.NORMAL,
                            option["code"],
                            option["price"],
                            trd_env=self.s.TrdEnv.SIMULATE,
                            acc_id=self.aid,
                            session=self.s.Session.RTH,
                        )
                    ).iloc[0]
                    if float(limits["max_cash_buy"]) >= 1:
                        if {p["code"]: float(p["qty"]) for p in self.positions()} != {
                            p["code"]: float(p["qty"]) for p in positions
                        }:
                            return common | dict(status="POSITION_CHANGED")
                        self.submit(
                            option["code"],
                            1,
                            "BUY",
                            "ENTRY",
                            option["price"],
                            row["last_bar"],
                            "OPTION_" + row["signal"],
                            now,
                            option,
                        )
                        return common | dict(status="OPTION_ENTRY_SUBMITTED")
            if not entry_filter(row, profile):
                continue
            short = row["signal"] == "SHORT"
            price = round(
                float(quote["bid_price" if short else "ask_price"]) * (0.999 if short else 1.001), 2
            )
            qty = entry_qty(equity, price, reserve(positions, self.state["orders"]), cash, short)
            limits = self.checked(
                self.t.acctradinginfo_query(
                    self.s.OrderType.NORMAL,
                    code,
                    price,
                    trd_env=self.s.TrdEnv.SIMULATE,
                    acc_id=self.aid,
                    session=self.s.Session.RTH,
                )
            ).iloc[0]
            qty = min(qty, int(float(limits["max_sell_short" if short else "max_cash_buy"])))
            if qty <= 0:
                continue
            # Fresh holdings must match the start-of-cycle snapshot; otherwise restart reconciliation.
            if {p["code"]: float(p["qty"]) for p in self.positions()} != {
                p["code"]: float(p["qty"]) for p in positions
            }:
                return common | dict(status="POSITION_CHANGED")
            self.submit(
                code,
                qty,
                "SELL" if short else "BUY",
                "ENTRY",
                price,
                row["last_bar"],
                "ORB_MA20_VWAP:" + profile,
                now,
            )
            return common | dict(status="ENTRY_SUBMITTED")
        return common | dict(status="WATCHING")


def main():
    import argparse

    from quant_lab.runtime.locking import acquire

    parser = argparse.ArgumentParser()
    parser.add_argument("--execute-paper", action="store_true")
    args = parser.parse_args()
    if not args.execute_paper:
        raise SystemExit("Explicit --execute-paper is required")
    DIR.mkdir(parents=True, exist_ok=True)
    lock = open(DIR / "quant_lab.brokers.moomoo.execution.lock", "a+b")
    lock.seek(0)
    lock.write(b"0")
    lock.flush()
    lock.seek(0)
    try:
        acquire(lock)
    except OSError:
        raise SystemExit("Executor already running")
    import moomoo as sdk

    while True:
        t = q = None
        try:
            t = sdk.OpenSecTradeContext(filter_trdmarket=sdk.TrdMarket.US, **opend_kwargs())
            q = sdk.OpenQuoteContext(**opend_kwargs())
            # Scanner owns all 100 minute subscriptions; this executor only reads cached candidates and fresh quote snapshots.
            executor = Executor(t, q, sdk)
            while True:
                report = executor.cycle()
                write(DIR / "moomoo_auto_status.json", report)
                time.sleep(5)
        except Exception as exc:
            write(
                DIR / "moomoo_auto_status.json",
                dict(
                    enabled=True,
                    automatic_trading=True,
                    status="CONNECTION_OR_RECONCILIATION_HOLD",
                    error_type=type(exc).__name__,
                    updated=datetime.now(NY).isoformat(),
                ),
            )
        finally:
            if q:
                q.close()
            if t:
                t.close()
        time.sleep(10)


if __name__ == "__main__":
    main()
