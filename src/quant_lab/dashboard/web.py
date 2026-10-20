"""Loopback-only live display. Reuses broker reads; does not launch a trading bot."""

import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from quant_lab.dashboard.data import (
    ROOT,
    STATUS,
    account_snapshot,
    local_status,
    read_json,
    session_completed,
)
from quant_lab.strategies.intraday import timeout_request

UTC = ZoneInfo("UTC")


class LiveData:
    def __init__(self, root):
        from quant_lab.runtime.paths import web_assets

        self.assets = web_assets(root)
        self.root = root
        self.lock = threading.Lock()
        self.account = None
        self.quote = None
        self.error = None
        self.quote_error = None
        self.closing = threading.Event()
        self.chart_lock = threading.Lock()
        self.charts = {}
        self.equity_curve = read_json(root / "state/desk_equity_alpaca.json", [])

    def state(self):
        local = local_status(self.root)
        with self.lock:
            data = dict(
                account=self.account,
                quote=self.quote,
                account_error=self.error,
                quote_error=self.quote_error,
                extensions=dict(equity_curve=list(self.equity_curve)),
            )
        last = local["last"]
        auth = read_json(self.root / "state" / "paper_authorization.json", {})
        risk = read_json(self.root / "state" / "intraday.json", {})
        from quant_lab.runtime.budget import settings as budget_settings

        if budget_settings(self.root, "alpaca") is not None:
            risk = read_json(self.root / "state/intraday_strategy_budget.json", {})
        journal = read_json(
            self.root / "state" / f"paper_intents_{auth.get('session', '')}.json", {}
        )
        per = {s: 0 for s in auth.get("universe", ["SPY"])}
        for cid, record in journal.get("orders", {}).items():
            if any(tag in cid for tag in ("-B-", "-S-")):
                symbol = record.get("symbol", "SPY")
                if symbol in per:
                    per[symbol] += 1
        from quant_lab.services.automation import public_status

        automatic = public_status(self.root)["alpaca"]
        managed_wait = (
            automatic["enabled"]
            and automatic["fresh"]
            and automatic.get("status")
            in {"WAIT_TRADING_DAY", "WAIT_REGULAR_SESSION", "SESSION_COMPLETE", "MANUAL_STOP"}
        )
        current = data.get("account") or {}
        completed = session_completed(local, current, data.get("account_error")) and last.get(
            "session"
        ) == auth.get("session")
        occupied = {p["symbol"] for p in current.get("holdings", [])} | set(
            current.get("open_symbols", [])
        )
        for cid, r in journal.get("orders", {}).items():
            if any(tag in cid for tag in ("-B-", "-S-")) and r.get("status") not in {
                "filled",
                "canceled",
                "expired",
                "rejected",
                "replaced",
            }:
                occupied.add(r.get("symbol", "SPY"))
        # Deliberately whitelist fields; never serve account IDs, environment files or key material.
        data.update(
            server_time=datetime.now(UTC).isoformat(),
            mode="PAPER ONLY",
            scanner=read_json(self.root / "state" / "scanner.json", {}),
            backend=dict(
                alive=local["alive"],
                healthy=local["healthy"],
                age=local["age"],
                completed=completed,
                managed_wait=managed_wait,
                auto_status=automatic.get("status"),
                status=last.get("status", "UNKNOWN"),
                label="本场已结束 · 已空仓"
                if completed
                else STATUS.get(last.get("status"), last.get("status", "尚无记录")),
                stopped=local["stopped"],
                next_open=last.get("next_open_nz"),
            ),
            session=auth.get("session"),
            risk=dict(
                halted=risk.get("halted", False),
                allow_short=bool(auth.get("allow_short", False)),
                strategy=auth.get("strategy", "spy_5m"),
                universe=auth.get("universe", ["SPY"]),
                trend_filter=auth.get("trend_filter", False),
                profit_trailing=auth.get("profit_trailing", False),
                target_position=auth.get("target_position", auth.get("max_position", 0.05)),
                entry_variant=auth.get("entry_variant", "orb"),
                strategy_version=auth.get("strategy_version", "legacy"),
                strategy_daily_loss=auth.get("strategy_daily_loss", auth.get("daily_loss", 0.20)),
                exit_policy=auth.get("exit_policy", "fixed"),
                candidates=sum(per.values())
                if auth.get("strategy") == "multi_1m"
                else risk.get("entry_candidates", 0),
                per_symbol_entries=per,
                per_symbol_limit=auth.get("per_symbol_limit", 3),
                max_candidates=auth.get("global_entry_limit", 3),
                max_positions=auth.get("max_positions", 1),
                slots_used=len(occupied),
                reserved_symbols=sorted(occupied),
                gross_limit=auth.get("gross_limit", 0.05),
                workers=auth.get("read_workers", 1),
                cap=auth.get("max_position", 0.05),
                loss_limit=auth.get("daily_loss", 0.20),
            ),
            events=[
                {
                    k: e[k]
                    for k in (
                        "recorded_at",
                        "event",
                        "status",
                        "reason",
                        "symbol",
                        "side",
                        "qty",
                        "type",
                        "error_type",
                    )
                    if k in e
                }
                for e in local["events"][-40:]
            ],
        )
        return data

    def start(self):
        threading.Thread(target=self.account_worker, daemon=True).start()
        threading.Thread(target=self.quote_worker, daemon=True).start()

    def account_worker(self):
        while not self.closing.is_set():
            try:
                account = account_snapshot(self.root)
                from quant_lab.runtime.storage import write_json

                self.equity_curve = (
                    self.equity_curve + [dict(time=account["fetched"], equity=account["equity"])]
                )[-1800:]
                write_json(self.root / "state/desk_equity_alpaca.json", self.equity_curve)
                with self.lock:
                    self.account, self.error = account, None
            except Exception as exc:
                with self.lock:
                    self.error = type(exc).__name__
            self.closing.wait(15)

    def quote_worker(self):
        from alpaca.data.enums import DataFeed
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestQuoteRequest
        from dotenv import load_dotenv

        load_dotenv(self.root / ".env", override=False)
        key, secret = os.getenv("ALPACA_KEY"), os.getenv("ALPACA_SECRET")
        if not key or not secret:
            with self.lock:
                self.quote_error = "MissingCredentials"
            return
        client = StockHistoricalDataClient(key, secret)
        client._session.request = timeout_request(client._session.request)
        client._retry = 0
        while not self.closing.is_set():
            try:
                with self.lock:
                    symbol = (self.account or {}).get("symbol", "SPY")
                quote = client.get_stock_latest_quote(
                    StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX)
                )[symbol]
                with self.lock:
                    self.quote = dict(
                        symbol=symbol,
                        bid=quote.bid_price,
                        ask=quote.ask_price,
                        timestamp=quote.timestamp.isoformat(),
                        fetched=datetime.now(UTC).isoformat(),
                    )
                    self.quote_error = None
            except Exception as exc:
                with self.lock:
                    self.quote_error = type(exc).__name__
            self.closing.wait(5)


def make_handler(store, token, port):
    allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, data, content="application/json; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", content)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
            )
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def valid_host(self):
            return self.headers.get("Host") in allowed

        def do_GET(self):
            if not self.valid_host():
                return self.reply(403, b'{"error":"host"}')
            path = urlsplit(self.path).path
            broker = parse_qs(urlsplit(self.path).query).get("broker", ["alpaca"])[0]
            if broker not in ("alpaca", "moomoo"):
                return self.reply(400, b'{"error":"unsupported broker"}')

            def desk_state():
                if broker == "moomoo":
                    from quant_lab.dashboard.desks import moomoo_state

                    value = moomoo_state(store.root)
                    if store.state().get("mode") == "DEMO":
                        value["mode"] = "DEMO"
                        value["account_error"] = None
                        if value.get("account"):
                            value["account"]["fetched"] = value["server_time"]
                        value["backend"].update(
                            demo=True, healthy=True, label="DEMO · Synthetic data only"
                        )
                    return value
                return store.state()

            if path == "/api/review":
                from datetime import date

                day = parse_qs(urlsplit(self.path).query).get("day", [""])[0]
                try:
                    if str(date.fromisoformat(day)) != day:
                        raise ValueError("Invalid date")
                except ValueError:
                    return self.reply(400, b'{"error":"invalid date"}')
                review_root = store.root
                if broker == "moomoo":
                    from quant_lab.dashboard.desks import review_home

                    review_root = review_home(store.root)
                record = read_json(review_root / "state" / "reviews" / f"{day}.json")
                if record is None:
                    return self.reply(404, b'{"error":"not found"}')
                return self.reply(200, json.dumps(record, ensure_ascii=False).encode("utf-8"))
            if path == "/api/chart":
                from quant_lab.brokers.alpaca.chart_data import chart_snapshot
                from quant_lab.strategies.scanner import UNIVERSE

                symbol = parse_qs(urlsplit(self.path).query).get("symbol", ["SPY"])[0]
                if broker == "moomoo":
                    from quant_lab.dashboard.desks import moomoo_chart

                    try:
                        return self.reply(
                            200,
                            json.dumps(moomoo_chart(store.root, symbol), ensure_ascii=False).encode(
                                "utf-8"
                            ),
                        )
                    except ValueError:
                        return self.reply(400, b'{"error":"unsupported symbol"}')
                if symbol not in UNIVERSE:
                    return self.reply(400, b'{"error":"unsupported symbol"}')
                try:
                    with store.chart_lock:
                        cached = store.charts.get(symbol)
                    if not cached or time.monotonic() - cached[0] > 10:
                        chart = chart_snapshot(store.root, symbol)
                        with store.chart_lock:
                            store.charts[symbol] = (time.monotonic(), chart)
                    else:
                        chart = cached[1]
                    return self.reply(200, json.dumps(chart, ensure_ascii=False).encode("utf-8"))
                except Exception as exc:
                    return self.reply(
                        503,
                        json.dumps(dict(error=type(exc).__name__, symbol=symbol)).encode("utf-8"),
                    )
            if path == "/api/automation":
                from quant_lab.services.automation import public_status

                return self.reply(
                    200, json.dumps(public_status(store.root), ensure_ascii=False).encode("utf-8")
                )
            if path == "/api/optimizer":
                record = read_json(store.root / "state" / "optimizer" / "latest.json", {})
                return self.reply(200, json.dumps(record, ensure_ascii=False).encode("utf-8"))
            if path == "/api/moomoo/chart":
                from quant_lab.strategies.universe import WATCH_UNIVERSE

                code = parse_qs(urlsplit(self.path).query).get("symbol", ["US.AAPL"])[0]
                if code not in {"US." + s for s in WATCH_UNIVERSE}:
                    return self.reply(400, b'{"error":"unsupported symbol"}')
                record = read_json(
                    store.root / "state" / "brokers" / "moomoo_charts" / f"{code}.json", {}
                )
                return self.reply(200, json.dumps(record, ensure_ascii=False).encode("utf-8"))
            if path == "/api/moomoo":
                record = read_json(
                    store.root / "state" / "brokers" / "moomoo_snapshot.json",
                    {"ok": False, "note": "观察器尚未启动"},
                )
                return self.reply(200, json.dumps(record, ensure_ascii=False).encode("utf-8"))
            if path == "/api/status":
                return self.reply(200, json.dumps(desk_state(), ensure_ascii=False).encode("utf-8"))
            if path == "/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                try:
                    while not store.closing.is_set():
                        self.wfile.write(
                            (
                                "data: " + json.dumps(desk_state(), ensure_ascii=False) + "\n\n"
                            ).encode("utf-8")
                        )
                        self.wfile.flush()
                        store.closing.wait(2)
                except (ConnectionError, OSError):
                    pass
                return
            assets = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/desk-extras.js": ("desk-extras.js", "text/javascript; charset=utf-8"),
                "/i18n.js": ("i18n.js", "text/javascript; charset=utf-8"),
                "/i18n.css": ("i18n.css", "text/css; charset=utf-8"),
                "/locales/en.json": ("locales/en.json", "application/json; charset=utf-8"),
                "/locales/zh-CN.json": ("locales/zh-CN.json", "application/json; charset=utf-8"),
                "/style.css": ("style.css", "text/css; charset=utf-8"),
                "/moomoo.html": ("index.html", "text/html; charset=utf-8"),
                "/moomoo.js": ("moomoo.js", "text/javascript; charset=utf-8"),
                "/automation.js": ("automation.js", "text/javascript; charset=utf-8"),
                "/automation.css": ("automation.css", "text/css; charset=utf-8"),
                "/moomoo.css": ("moomoo.css", "text/css; charset=utf-8"),
            }
            if path not in assets:
                return self.reply(404, b'{"error":"not found"}')
            file, mime = assets[path]
            raw = (store.assets / file).read_bytes()
            if path in ("/", "/moomoo.html"):
                raw = raw.replace(b"__CSRF_TOKEN__", token.encode("ascii"))
            self.reply(200, raw, mime)

        def do_POST(self):
            if (
                not self.valid_host()
                or self.headers.get("Origin") not in {f"http://{host}" for host in allowed}
                or not secrets.compare_digest(self.headers.get("X-Dashboard-Token", ""), token)
            ):
                return self.reply(403, b'{"error":"forbidden"}')
            if self.path not in ("/api/stop", "/api/moomoo/stop"):
                return self.reply(404, b'{"error":"not found"}')
            if int(self.headers.get("Content-Length", "0")) > 1024:
                return self.reply(413, b'{"error":"too large"}')
            try:
                (
                    store.root
                    / ("STOP_MOOMOO" if self.path == "/api/moomoo/stop" else "STOP_PAPER")
                ).write_text("STOP\n", encoding="utf-8")
                self.reply(200, b'{"requested":true}')
            except OSError:
                self.reply(500, b'{"error":"unable to save stop request"}')

    return Handler


def main():
    logging.getLogger("alpaca").setLevel(logging.CRITICAL)
    from quant_lab.runtime.settings import load_settings

    settings = load_settings(ROOT)
    store = LiveData(ROOT)
    token = secrets.token_urlsafe(32)
    # 8765 may belong to another local tool. Never touch an unrelated listener.
    try:
        server = ThreadingHTTPServer(
            (settings.web_host, settings.web_port), make_handler(store, token, settings.web_port)
        )
    except OSError:
        if os.environ.get("QUANT_LAB_CONTAINER") == "1":
            raise  # A random fallback port would hide a broken container mapping.
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store, token, 0))
    port = server.server_address[1]
    server.RequestHandlerClass = make_handler(store, token, port)
    server.daemon_threads = True
    server.block_on_close = False
    store.start()
    from quant_lab.strategies.intraday import save_state

    save_state(
        ROOT / "state" / "web_process.json",
        dict(pid=os.getpid(), url=f"http://127.0.0.1:{port}/", program="web_dashboard.py"),
    )
    print(f"Local dashboard: http://127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever()
    finally:
        store.closing.set()
        server.server_close()


if __name__ == "__main__":
    main()
