"""SPY opening-range breakout monitor. Read-only: no order execution methods."""
import argparse
from datetime import datetime, timedelta
import json
import logging
from math import floor, isfinite
import os
from pathlib import Path
import time
from zoneinfo import ZoneInfo

NY = ZoneInfo('America/New_York')
ROOT = Path(__file__).resolve().parents[1]


def finite_positive(value):
    return isfinite(float(value)) and float(value) > 0


def session_time(value):
    return value.replace(tzinfo=NY) if value.tzinfo is None else value.astimezone(NY)


def daily_state(path, day, baseline, account_id):
    """Broker previous-close equity anchors the day, even on a mid-day restart."""
    if not finite_positive(baseline):
        raise ValueError('Invalid previous-close equity')
    if path.exists():
        state = json.loads(path.read_text(encoding='utf-8'))
        if state['account_id'] != account_id:
            raise ValueError('State belongs to another account')
        if state['day'] > str(day):
            raise ValueError('State date is ahead of broker clock')
        if state['day'] == str(day):
            if not finite_positive(state['baseline']) or not isinstance(state['halted'], bool):
                raise ValueError('Invalid saved state')
            if not isinstance(state['entry_candidates'], int) or state['entry_candidates'] < 0:
                raise ValueError('Invalid candidate counter')
            if state['last_entry_at']:
                stamp = datetime.fromisoformat(state['last_entry_at'])
                if stamp.tzinfo is None:
                    raise ValueError('Invalid cooldown timestamp')
            return state
    return dict(day=str(day), account_id=account_id, baseline=float(baseline), halted=False,
                last_candidate=None, entry_candidates=0, last_entry_at=None)


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(state), encoding='utf-8')
    temp.replace(path)


def evaluate(now, opened, closed, account, orders, bars, state, allow_short=False):
    """Pure decisions; risk exits precede pending-order and stale-data guards."""
    equity, cash, qty, value, entry = (float(account[k]) for k in ('equity', 'cash', 'qty', 'value', 'entry'))
    if not all(isfinite(v) for v in (equity, cash, qty, value, entry)) or equity <= 0:
        raise ValueError('Invalid account snapshot')
    result = dict(action='WAIT', reason='Outside regular session', candidate_qty=0,
                  daily_return=equity / state['baseline'] - 1, pending_orders=len(orders))
    if result['daily_return'] <= -0.20:
        state['halted'] = True
    if (qty == 0) != (value == 0) or qty * value < 0 or (qty < 0 and not allow_short):
        return result | dict(action='REVIEW', reason='Unexpected SPY position')
    if state['halted'] or now >= closed - timedelta(minutes=10):
        reason = 'Daily loss limit latched' if state['halted'] else 'Exit ten minutes before close'
        action = 'CANCEL THEN EXIT CANDIDATE' if orders else ('EXIT CANDIDATE' if qty else 'HALT')
        return result | dict(action=action, reason=reason, candidate_qty=abs(qty))
    if account['blocked']:
        return result | dict(action='STOP', reason='Account blocked or inactive')
    if now < opened or now >= closed:
        return result
    if orders and not qty:
        return result | dict(reason='Existing SPY order; no new entry')
    if now < opened + timedelta(minutes=20, seconds=15):
        return result | dict(reason='Building 15-minute opening range and first 5-minute signal')
    # Five-minute bars are timestamped at interval start. Never use the current bar.
    completed = sorted([b for b in bars if opened <= b['time'] and
                        b['time'] + timedelta(minutes=5, seconds=15) <= now and
                        b['time'] < closed], key=lambda b: b['time'])
    if not completed or now - (completed[-1]['time'] + timedelta(minutes=5)) > timedelta(seconds=375):
        return result | dict(action='STOP', reason='Missing or stale completed bars; manually review any position')
    times = [b['time'] for b in completed]
    if len(set(times)) != len(times):
        raise ValueError('Duplicate bars')
    for b in completed:
        if not all(finite_positive(b[k]) for k in ('open', 'high', 'low', 'close')) or not b['low'] <= min(b['open'], b['close']) <= max(b['open'], b['close']) <= b['high']:
            raise ValueError('Invalid OHLC data')
    opening = [next((b for b in completed if b['time'] == opened + timedelta(minutes=m)), None) for m in (0, 5, 10)]
    if any(b is None for b in opening):
        return result | dict(action='STOP', reason='Incomplete opening range')
    high, low = max(b['high'] for b in opening), min(b['low'] for b in opening)
    latest = completed[-1]
    price = latest['close']
    result.update(range_high=high, range_low=low, reference_price=price,
                  signal_bar=latest['time'].isoformat(), position_limit=equity * .05)
    if qty:
        if entry <= 0:
            raise ValueError('Invalid entry price')
        exit_long = qty > 0 and (price <= entry * .99 or price < low or price >= entry * 1.02)
        exit_short = qty < 0 and (price >= entry * 1.01 or price > high or price <= entry * .98)
        if exit_long or exit_short:
            return result | dict(action='EXIT CANDIDATE', reason='1% stop, range failure or 2% take-profit', candidate_qty=abs(qty))
        if abs(value) > equity * .05:
            return result | dict(action='REVIEW', reason='Position exceeds 5% cap')
        return result | dict(action='HOLD', reason='Existing SPY position')
    if now >= closed - timedelta(minutes=30):
        return result | dict(reason='No entries in final 30 minutes')
    if state['entry_candidates'] >= 3:
        return result | dict(reason='Three entry candidates already emitted today')
    if state['last_entry_at'] and now - datetime.fromisoformat(state['last_entry_at']) < timedelta(minutes=15):
        return result | dict(reason='15-minute entry cooldown')
    previous = next((b for b in completed if b['time'] == latest['time'] - timedelta(minutes=5)), None)
    if previous is None or latest['time'] < opened + timedelta(minutes=15):
        return result | dict(reason='Need consecutive completed signal bars')
    if previous['close'] <= high < price:
        quantity = floor(min(max(cash, 0), equity * .05) / (price * 1.02))
        if quantity:
            return result | dict(action='BUY CANDIDATE', reason='5-minute close crosses opening-range high', candidate_qty=quantity)
    if allow_short and previous['close'] >= low > price:
        quantity = floor(min(max(cash, 0), equity * .05) / (price * 1.05))
        if quantity:
            return result | dict(action='SHORT CANDIDATE', reason='5-minute close crosses opening-range low', candidate_qty=quantity)
    return result | dict(reason='No new opening-range breakout')


def fetch_bars(data, start, end):
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from alpaca.data.enums import DataFeed
    request = StockBarsRequest(symbol_or_symbols='SPY', start=start, end=end,
                               timeframe=TimeFrame(5, TimeFrameUnit.Minute), feed=DataFeed.IEX)
    response = data.get_stock_bars(request)
    return [dict(time=b.timestamp.astimezone(NY), open=b.open, high=b.high,
                 low=b.low, close=b.close) for b in response.data.get('SPY', [])]


def tick(client, data, state_path, preflight=False, allow_short=False):
    from alpaca.trading.requests import GetCalendarRequest, GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus
    now = client.get_clock().timestamp.astimezone(NY)
    sessions = client.get_calendar(GetCalendarRequest(start=now.date()-timedelta(days=10), end=now.date()+timedelta(days=7)))
    today = next((s for s in sessions if s.date == now.date()), None)
    upcoming = next((s for s in sessions if session_time(s.open) > now), None)
    account = client.get_account()
    orders = client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=['SPY'], limit=500, nested=True))
    positions = [p for p in client.get_all_positions() if p.symbol == 'SPY']
    if len(positions) > 1:
        raise ValueError('Ambiguous position')
    p = positions[0] if positions else None
    snapshot = dict(equity=float(account.equity), cash=float(account.cash), qty=float(p.qty) if p else 0,
                    value=float(p.market_value) if p else 0, entry=float(p.avg_entry_price) if p else 0,
                    blocked=bool(account.trading_blocked or account.account_blocked or
                                 getattr(account.status, 'value', account.status) != 'ACTIVE'))
    state = daily_state(state_path, now.date(), float(account.last_equity), str(account.id))
    if not finite_positive(snapshot['equity']):
        raise ValueError('Invalid equity')
    if snapshot['equity'] / state['baseline'] <= .80:
        state['halted'] = True
    # Persist the circuit breaker before a later market-data request can fail.
    save_state(state_path, state)
    if today:
        opened, closed = session_time(today.open), session_time(today.close)
        bars = fetch_bars(data, opened, now) if opened < now < closed else []
        report = evaluate(now, opened, closed, snapshot, orders, bars, state, allow_short=allow_short)
    else:
        report = dict(action='WAIT', reason='No regular session today', candidate_qty=0)
    if report['action'] in ('BUY CANDIDATE', 'SHORT CANDIDATE'):
        if state['last_candidate'] == report['signal_bar']:
            report.update(action='WAIT', candidate_qty=0, reason='Candidate already logged for this bar')
        else:
            state.update(last_candidate=report['signal_bar'], entry_candidates=state['entry_candidates']+1,
                         last_entry_at=now.isoformat())
    if preflight:
        previous = [s for s in sessions if session_time(s.close) < now]
        if not previous:
            raise ValueError('No previous session for data test')
        session = previous[-1]
        test = fetch_bars(data, session_time(session.open), session_time(session.close))
        if len(test) < 4:
            raise ValueError('IEX intraday feed unavailable')
        report['feed_test'] = dict(feed='IEX', session=str(session.date), bars=len(test))
    save_state(state_path, state)
    report.update(mode='DRY RUN', feed='IEX', symbol='SPY', broker_time=now.isoformat(),
                  regular_session_open=bool(today and session_time(today.open) <= now < session_time(today.close)),
                  account=snapshot, baseline_equity=state['baseline'], loss_limit=0.20,
                  next_open_nz=session_time(upcoming.open).astimezone(ZoneInfo('Pacific/Auckland')).isoformat() if upcoming else None,
                  note='Candidates only. No orders submitted/cancelled. No simulated fills or guaranteed exits.')
    return report


def main():
    parser = argparse.ArgumentParser(description='SPY intraday DRY RUN monitor; no execution support')
    parser.add_argument('--watch', action='store_true')
    parser.add_argument('--preflight', action='store_true')
    parser.add_argument('--interval', type=int, default=30)
    parser.add_argument('--env-file', type=Path, default=ROOT / '.env')
    args = parser.parse_args()
    if args.interval < 15:
        parser.error('Minimum interval is 15 seconds')
    logging.disable(logging.CRITICAL)
    from dotenv import load_dotenv
    from alpaca.trading.client import TradingClient
    from alpaca.data.historical import StockHistoricalDataClient
    load_dotenv(args.env_file, override=False)
    key, secret = os.getenv('ALPACA_KEY'), os.getenv('ALPACA_SECRET')
    if not key or not secret:
        print('STOP: Missing paper API credentials; no orders submitted.')
        return 1
    client = TradingClient(key, secret, paper=True)
    data = StockHistoricalDataClient(key, secret)
    # Bound network calls so an unavailable service cannot silently hang the watcher.
    for sdk in (client, data):
        sdk._session.request = timeout_request(sdk._session.request)
    log = ROOT / 'logs' / 'intraday.jsonl'
    state_path = ROOT / 'state' / 'intraday.json'
    lock = ROOT / 'state' / 'intraday.lock'
    lock.parent.mkdir(parents=True, exist_ok=True)
    # OS lock releases on process exit, including crashes. The file itself may remain.
    import msvcrt
    with lock.open('a+b') as handle:
        handle.seek(0)
        handle.write(b'0')
        handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            print('STOP: Another intraday monitor is running.')
            return 1
        try:
            failures = 0
            while True:
                try:
                    report = tick(client, data, state_path, args.preflight)
                    failures = 0
                except Exception as exc:
                    failures += 1
                    report = dict(mode='DRY RUN', action='STOP', error_type=type(exc).__name__,
                                  reason='Data/account/state check failed; no orders submitted. Review existing positions manually.')
                report['recorded_at'] = datetime.now(ZoneInfo('UTC')).isoformat()
                text = json.dumps(report, ensure_ascii=False)
                log.parent.mkdir(parents=True, exist_ok=True)
                with log.open('a', encoding='utf-8') as stream:
                    stream.write(text + '\n')
                label = {'WAIT': '等待', 'STOP': '停止：需要检查', 'HALT': '当日停止入场',
                         'BUY CANDIDATE': '买入候选（未下单）', 'EXIT CANDIDATE': '退出候选（未执行）',
                         'CANCEL THEN EXIT CANDIDATE': '需要先撤单再退出（未执行）',
                         'HOLD': '持仓观察', 'REVIEW': '需要人工复核'}.get(report['action'], report['action'])
                print(f"[{report.get('broker_time', report['recorded_at'])}] DRY RUN | {label} | "
                      f"{report['reason']} | shares={report.get('candidate_qty', 0)}", flush=True)
                if report.get('next_open_nz'):
                    print(f"Next open (Auckland): {report['next_open_nz']} | "
                          f"Pending SPY orders: {report.get('pending_orders', 0)}", flush=True)
                if report.get('feed_test'):
                    print('Feed preflight: ' + json.dumps(report['feed_test']), flush=True)
                if not args.watch or failures >= 3:
                    return 1 if failures else 0
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print('Monitor stopped. No orders submitted.')
            return 0
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def timeout_request(request):
    def bounded(*args, **kwargs):
        kwargs['timeout'] = 15
        return request(*args, **kwargs)
    return bounded


if __name__ == '__main__':
    raise SystemExit(main())
