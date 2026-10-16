"""Fixed, volatility-scaled and time-based paper exit policies."""
from math import isfinite

POLICIES=('fixed','atr','fixed_timeout','atr_timeout')


def daily_loss_budget(settings):
    hard=float(settings.get('daily_loss',.20))
    trial=float(settings.get('strategy_daily_loss',hard))
    if not all(isfinite(v) for v in (hard,trial)) or not 0<trial<=hard<=.20:
        raise ValueError('Invalid daily risk budget')
    return trial


def exit_plan(policy,reference,atr=None):
    if policy not in POLICIES:raise ValueError('Unsupported exit policy')
    if not isfinite(reference) or reference<=0:raise ValueError('Invalid reference price')
    stop=.01
    if policy.startswith('atr'):
        if atr is None or not isfinite(atr) or atr<=0:raise ValueError('ATR unavailable')
        stop=min(.01,max(.003,1.5*atr/reference))
    return dict(policy=policy,stop_fraction=stop,take_fraction=2*stop,
                timeout_minutes=45 if policy.endswith('timeout') else None,min_progress_r=.5)


def timed_exit(plan,elapsed_minutes,profit_per_share,risk_per_share):
    limit=plan.get('timeout_minutes')
    return bool(limit is not None and elapsed_minutes>=limit and risk_per_share>0
                and profit_per_share<plan.get('min_progress_r',.5)*risk_per_share)
