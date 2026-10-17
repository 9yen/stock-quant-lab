"""Three-symbol read-only minute feed and existing ORB signal validation."""

import time
from datetime import datetime
from datetime import time as walltime
from zoneinfo import ZoneInfo

from quant_lab.brokers.moomoo.observer import ROOT, number, save
from quant_lab.runtime.gateway import opend_kwargs
from quant_lab.strategies.scanner import compare, signal_context
from quant_lab.strategies.universe import WATCH_UNIVERSE

NY = ZoneInfo("America/New_York")

CODES = ["US." + symbol for symbol in WATCH_UNIVERSE]


def analyze(code, rows, market, now):
    bars = []
    for r in rows:
        stamp = datetime.fromisoformat(str(r["time_key"])).replace(tzinfo=NY)
        if stamp.date() == now.date() and walltime(9, 30) <= stamp.time() < walltime(16):
            bars.append(
                dict(
                    time=stamp,
                    **{k: float(r[k]) for k in ("open", "high", "low", "close", "volume")},
                )
            )
    opened = now.replace(hour=9, minute=31, second=0, microsecond=0)
    signal = compare(code, bars, now, opened)
    context = signal_context(bars, now)
    regular = market in ("MORNING", "AFTERNOON")
    candidate = signal["minute_signal"]
    filtered = (candidate == "LONG" and context.get("filter_long")) or (
        candidate == "SHORT" and context.get("filter_short")
    )
    return dict(
        symbol=code,
        market=str(market),
        bars=len(bars),
        last_bar=signal["last_bar"],
        range_high=signal.get("range_high"),
        range_low=signal.get("range_low"),
        atr14=context.get("atr14"),
        rvol=context.get("rvol"),
        price=signal["price"],
        ma20=context.get("ma20"),
        volume=bars[-1]["volume"] if bars else None,
        opening_minutes=signal["opening_minutes"],
        signal=candidate if regular and filtered else "WAIT",
        reason=(
            "常规时段之外，禁止入场"
            if not regular
            else signal["reason"]
            if candidate not in ("LONG", "SHORT")
            else context["filter_reason"]
        ),
        candles=[
            dict(
                time=b["time"].isoformat(),
                **{k: b[k] for k in ("open", "high", "low", "close", "volume")},
            )
            for b in bars
            if b["time"].timestamp() + 90 <= now.timestamp()
        ],
        observation_only=True,
    )


def exit_decision(direction, entry, price, fresh, connected):
    if direction not in ("LONG", "SHORT") or not entry > 0 or not price > 0:
        raise ValueError("Invalid position")
    if not fresh or not connected:
        return "DATA_HOLD"
    change = (price / entry - 1) * (1 if direction == "LONG" else -1)
    if change <= -0.01 + 1e-12:
        return "STOP"
    if change >= 0.02 - 1e-12:
        return "TAKE_PROFIT"
    return "HOLD"


def main():
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from moomoo import RET_OK, AuType, KLType, OpenQuoteContext, SubType

    # Independent contexts prevent one slow symbol from blocking the entire pool.
    lock = threading.Lock()
    latest = {}
    stop = threading.Event()

    def worker(codes):
        while not stop.is_set():
            ctx = None
            try:
                ctx = OpenQuoteContext(**opend_kwargs())
                ret, _ = ctx.subscribe(codes, [SubType.K_1M], subscribe_push=False)
                if ret != RET_OK:
                    raise RuntimeError("Minute subscription failed")
                calendar_cache = {}
                while not stop.is_set():
                    ret, states = ctx.get_market_state(codes)
                    state = (
                        {r["code"]: str(r["market_state"]) for r in states.to_dict("records")}
                        if ret == RET_OK
                        else {}
                    )
                    session_day = str(datetime.now(NY).date())
                    if session_day not in calendar_cache:
                        ret, dates = ctx.request_trading_days(
                            market="US", start=session_day, end=session_day
                        )
                        calendar_cache[session_day] = (
                            next((d for d in dates if d["time"] == session_day), {})
                            if ret == RET_OK
                            else {}
                        )
                        if calendar_cache[session_day]:
                            from quant_lab.analytics.optimizer import write as optimizer_write

                            optimizer_write(
                                ROOT
                                / "state"
                                / "optimizer"
                                / "calendars"
                                / (session_day + ".json"),
                                calendar_cache[session_day],
                            )
                    for code in codes:
                        now = datetime.now(NY)
                        try:
                            ret, data = ctx.get_cur_kline(code, 1000, KLType.K_1M, AuType.QFQ)
                            if ret != RET_OK:
                                raise RuntimeError("Minute bars unavailable")
                            row = analyze(
                                code, data.to_dict("records"), state.get(code, "UNKNOWN"), now
                            )
                            candles = row.pop("candles")
                            save(
                                ROOT / "state" / "brokers" / "moomoo_charts" / f"{code}.json",
                                dict(symbol=code, updated=now.isoformat(), candles=candles),
                            )
                            row["updated"] = now.isoformat()
                            session = calendar_cache.get(session_day, {})
                            if session and state.get(code) == "AFTER_HOURS_BEGIN":
                                from quant_lab.analytics.optimizer import archive_chart

                                archive_chart(
                                    ROOT,
                                    dict(symbol=code, candles=candles),
                                    session.get("trade_date_type"),
                                    now,
                                )
                        except Exception as exc:
                            row = dict(
                                symbol=code,
                                signal="WAIT",
                                reason="行情暂不可用",
                                updated=now.isoformat(),
                                error_type=type(exc).__name__,
                            )
                        with lock:
                            latest[code] = row
                    stop.wait(20)
            except Exception as exc:
                with lock:
                    for code in codes:
                        latest[code] = dict(
                            symbol=code,
                            signal="WAIT",
                            reason="分钟订阅待恢复",
                            updated=datetime.now(NY).isoformat(),
                            error_type=type(exc).__name__,
                        )
            finally:
                if ctx:
                    ctx.close()
            stop.wait(15)

    with ThreadPoolExecutor(max_workers=4) as pool:
        for i in range(4):
            pool.submit(worker, CODES[i::4])
        try:
            while True:
                with lock:
                    rows = [
                        latest.get(c, dict(symbol=c, signal="WAIT", reason="正在加载分钟线"))
                        for c in CODES
                    ]
                available = sum(not r.get("error_type") and bool(r.get("updated")) for r in rows)
                save(
                    ROOT / "state" / "brokers" / "quant_lab.brokers.moomoo.market.json",
                    dict(
                        ok=bool(available),
                        updated=datetime.now(NY).isoformat(),
                        rows=rows,
                        universe=CODES,
                        available=available,
                        workers=4,
                        automatic_trading=False,
                    ),
                )
                stop.wait(2)
        finally:
            stop.set()


if __name__ == "__main__":
    main()
