"""Synthetic local UI demo. Never loads .env, contacts a broker or starts an executor."""

import argparse
import json
import math
import os
import shutil
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit

from quant_lab.dashboard.web import LiveData, make_handler
from quant_lab.services.automation import write
from quant_lab.strategies.scanner import TECH_UNIVERSE, UNIVERSE


def synthetic_chart(symbol):
    start = datetime(2026, 1, 15, 14, 30, tzinfo=timezone.utc)
    base = 90 + sum(map(ord, symbol)) % 240
    bars = []
    for i in range(180):
        opening = base + math.sin(i / 12) * 1.2 + i * 0.008
        close = opening + math.sin(i / 3) * 0.18
        bars.append(
            dict(
                time=(start + timedelta(minutes=i)).isoformat(),
                open=opening,
                high=max(opening, close) + 0.22,
                low=min(opening, close) - 0.22,
                close=close,
                volume=1000 + (i * 137) % 5000,
            )
        )
    price = bars[-1]["close"]
    return dict(
        symbol=symbol,
        bars=bars,
        bar_minutes=1,
        chart_date="2026-01-15",
        quote=dict(symbol=symbol, bid=price - 0.02, ask=price + 0.02, timestamp=bars[-1]["time"]),
        fetched=datetime.now(timezone.utc).isoformat(),
    )


def build_store(root):
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        dict(
            symbol=s,
            minute_signal="WAIT",
            signal="WAIT",
            price=synthetic_chart(s)["bars"][-1]["close"],
            last_bar="2026-01-15T17:29:00+00:00",
            ma20=100,
            volume=2000,
            opening_minutes=15,
            five_signal="WAIT",
            shortable=False,
            reason="DEMO: synthetic data; no broker connection",
        )
        for s in TECH_UNIVERSE
    ]
    write(
        root / "state" / "paper_authorization.json",
        dict(
            session="2026-01-15",
            strategy="multi_1m",
            universe=list(UNIVERSE),
            entry_universe=list(TECH_UNIVERSE),
            allow_short=True,
            max_positions=10,
            max_position=0.4,
            target_position=0.1,
            gross_limit=1,
            per_symbol_limit=19,
            global_entry_limit=100,
        ),
    )
    write(root / "state" / "scanner.json", dict(rows=rows, recorded_at=now, mode="DEMO"))

    class DemoStore(LiveData):
        def state(self):
            self.account["fetched"] = datetime.now(timezone.utc).isoformat()
            data = super().state()
            data["mode"] = "DEMO"
            data["backend"].update(demo=True, healthy=True, label="DEMO · Synthetic data only")
            return data

    store = DemoStore(root)
    store.account = dict(
        equity=10000,
        cash=10000,
        baseline=10000,
        pnl=0,
        daily_pct=0,
        qty=0,
        position_value=0,
        position_pnl=0,
        holdings=[],
        open_symbols=[],
        rows=[],
        bars=[],
        fetched=now,
        market_open=False,
        next_open=None,
        symbol="SPY",
        realized_complete=True,
        realized_pnl=0,
        realized_by_symbol={},
        closed_rows=[],
        realized_session="2026-01-15",
        unrealized_pnl=0,
        review=None,
    )
    write(
        root / "state" / "brokers" / "moomoo_snapshot.json",
        dict(
            ok=True,
            updated=now,
            account="DEMO",
            assets=10000,
            cash=10000,
            positions=[],
            auto=dict(
                enabled=False,
                status="DISABLED",
                updated=now,
                baseline=10000,
                equity=10000,
                orders=[],
                daily_entries_used=0,
                options_enabled=False,
                risk=dict(universe=["US." + s for s in TECH_UNIVERSE]),
            ),
            market=dict(
                universe=["US." + s for s in TECH_UNIVERSE],
                rows=[dict(r, symbol="US." + r["symbol"]) for r in rows],
            ),
            performance=dict(closed=[], by_symbol=[], days=[], realized_gross=0),
            equity_curve=[],
        ),
    )
    for symbol in TECH_UNIVERSE:
        chart = synthetic_chart(symbol)
        write(
            root / "state" / "brokers" / "moomoo_charts" / f"US.{symbol}.json",
            dict(symbol="US." + symbol, candles=chart["bars"], updated=now),
        )
    return store


def handler(store, port):
    parent = make_handler(store, "demo-only-no-broker", port)

    class DemoHandler(parent):
        def do_GET(self):
            if not self.valid_host():
                return self.reply(403, b'{"error":"forbidden host"}')
            if urlsplit(self.path).path == "/api/chart":
                symbol = parse_qs(urlsplit(self.path).query).get("symbol", ["SPY"])[0]
                if symbol not in UNIVERSE:
                    return self.reply(400, b'{"error":"unknown demo symbol"}')
                return self.reply(200, json.dumps(synthetic_chart(symbol)).encode())
            if urlsplit(self.path).path == "/api/automation":
                disabled = dict(enabled=False, stopped=False, fresh=True, status="DEMO")
                return self.reply(
                    200,
                    json.dumps(
                        dict(status="DEMO", fresh=True, alpaca=disabled, moomoo=disabled)
                    ).encode(),
                )
            if urlsplit(self.path).path == "/api/moomoo":
                data = json.loads((store.root / "state/brokers/moomoo_snapshot.json").read_text())
                data["updated"] = datetime.now(timezone.utc).isoformat()
                data["auto"].update(updated=data["updated"], fresh=True)
                return self.reply(200, json.dumps(data).encode())
            return super().do_GET()

    return DemoHandler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args()
    with TemporaryDirectory(prefix="quant-lab-demo-") as folder:
        root = Path(folder)
        from quant_lab.runtime.paths import web_assets

        shutil.copytree(web_assets(), root / "web")
        for name in ("index.html", "moomoo.html"):
            page = root / "web" / name
            page.write_text(
                page.read_text(encoding="utf-8").replace(
                    "<main>",
                    '<main><div class="notice-static">DEMO · Synthetic data only · No broker connection or trading</div>',
                    1,
                ),
                encoding="utf-8",
            )
        store = build_store(root)
        # Compose publishes this namespace only on the host's loopback interface.
        host = "0.0.0.0" if os.environ.get("QUANT_LAB_CONTAINER") == "1" else "127.0.0.1"
        server = ThreadingHTTPServer((host, args.port), handler(store, args.port))
        server.daemon_threads = True
        server.block_on_close = False
        print(f"DEMO (synthetic data): http://127.0.0.1:{server.server_port}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            store.closing.set()
            server.server_close()


if __name__ == "__main__":
    main()
