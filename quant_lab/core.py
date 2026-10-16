from dataclasses import dataclass
from math import floor, isfinite


@dataclass(frozen=True)
class Snapshot:
    equity: float
    cash: float
    qty: float
    value: float
    pending: bool = False
    blocked: bool = False


def decision(account, price, invested, fraction=0.05, buffer=0.02):
    values = (account.equity, account.cash, account.qty, account.value, price, fraction, buffer)
    if not all(isfinite(v) for v in values) or account.equity <= 0 or price <= 0:
        raise ValueError('Invalid account or price data')
    if not 0 < fraction <= 0.05 or buffer < 0:
        raise ValueError('Invalid risk settings')
    target = account.equity * fraction if invested else 0.0
    result = dict(target_value=target, target_qty=floor(target / (price * (1 + buffer))),
                  candidate_qty=0, action='HOLD', reason='Target reached')
    if account.blocked:
        result.update(action='STOP', reason='Account is blocked or inactive')
    elif account.pending:
        result.update(action='WAIT', reason='Existing SPY order; no additional candidate')
    elif account.qty < 0 or account.value < 0 or (account.qty == 0) != (account.value == 0):
        result.update(action='REVIEW', reason='Unexpected position state')
    elif not invested and account.qty > 0:
        result.update(action='SELL CANDIDATE', candidate_qty=account.qty, reason='Target is cash')
    elif account.value > target:
        result.update(action='REVIEW', reason='Position exceeds target; manual review required')
    elif invested:
        budget = min(max(0, target - account.value), max(0, account.cash))
        qty = floor(budget / (price * (1 + buffer)))
        if qty:
            result.update(action='BUY CANDIDATE', candidate_qty=qty, reason='Within cash and 5% budget')
    return result


def signal(frame, expected_date):
    import pandas as pd
    if frame.empty or 'Close' not in frame:
        raise ValueError('Market data unavailable')
    frame = frame.sort_index()
    if frame.index.has_duplicates:
        raise ValueError('Duplicate market dates')
    frame = frame.loc[frame.index.date <= expected_date]
    if len(frame) < 200 or frame.index[-1].date() != expected_date:
        raise ValueError('Insufficient or stale market data')
    close = pd.to_numeric(frame['Close'].tail(200), errors='coerce')
    if not all(isfinite(float(v)) and v > 0 for v in close):
        raise ValueError('Invalid closing prices')
    return dict(data_date=str(expected_date), price=float(close.iloc[-1]),
                ma50=float(close.tail(50).mean()), ma200=float(close.mean()),
                invested=bool(close.tail(50).mean() > close.mean()))
