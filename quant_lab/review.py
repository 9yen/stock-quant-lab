"""Moomoo NZ estimated charges and persistent, read-only paper performance reviews."""
from collections import defaultdict, deque
from datetime import datetime
from decimal import Decimal, ROUND_CEILING
from .intraday import NY, save_state

PROFILE = dict(name='moomoo NZ 美股整股 · 2026-10-17 核验', currency='USD',
               source='https://www.moomoo.com/nz/support/topic13_19',
               rounding='估算假设：每个订单每天每项费用向上取整至美分',
               exclusions='未含借券/融资利息、汇兑、税款及特殊服务费；不是券商账单')
D = Decimal


def cents(v):
    return v.quantize(D('.01'), rounding=ROUND_CEILING)


def order_fees(qty, amount, side):
    qty, amount = D(str(qty)), D(str(amount))
    if qty <= 0 or amount <= 0:
        raise ValueError('Invalid fee inputs')
    if qty < 1:
        raise ValueError('This review supports whole-share orders only')
    if side not in ('buy','sell','sell_short'):
        raise ValueError('Invalid fee side')
    return dict(platform=D('.99'), settlement=cents(min(qty*D('.003'),amount*D('.01'))),
                sec=cents(max(D('.01'),amount*D('.0000206'))) if side!='buy' else D(0),
                taf=D(0),cat=cents(qty*D('.000003')))


def build_reviews(fills, universe):
    unique={r['id']:r for r in fills if r['symbol'] in universe}
    rows=sorted(unique.values(),key=lambda r:(r['transaction_time'],r['id']))
    groups=defaultdict(list)
    for r in rows:
        day=str(datetime.fromisoformat(r['transaction_time'].replace('Z','+00:00')).astimezone(NY).date())
        # Same order split over several fills is charged once per trading day.
        groups[(day,r['order_id'])].append(r)
    allocated, daily={}, {}
    def day_record(day):
        return daily.setdefault(day,dict(day=day,profile=PROFILE,stocks={},trades=[],fees=0.,gross_realized=0.,closed_net=0.,closed_entry_notional=0.,orders=0))
    def stock_record(day,symbol):
        return day_record(day)['stocks'].setdefault(symbol,dict(symbol=symbol,fees=0.,gross_realized=0.,closed_net=0.,closed_entry_notional=0.,closed_batches=0,orders=0))
    for (day,order),group in groups.items():
        qty=sum((D(r['qty']) for r in group),D(0))
        amount=sum((D(r['qty'])*D(r['price']) for r in group),D(0))
        if len({(r['symbol'],r['side']) for r in group})!=1:
            raise ValueError('Inconsistent order fills')
        components=order_fees(qty,amount,group[0]['side'])
        fee=sum(components.values(),D(0))
        for r in group:allocated[r['id']]=fee/qty
        for record in (day_record(day),stock_record(day,group[0]['symbol'])):
            record['fees']+=float(fee);record['orders']+=1
            parts=record.setdefault('fee_breakdown',{})
            for k,v in components.items():parts[k]=parts.get(k,0.)+float(v)
    books=defaultdict(deque)
    for r in rows:
        stamp=datetime.fromisoformat(r['transaction_time'].replace('Z','+00:00')).astimezone(NY)
        day=str(stamp.date());symbol=r['symbol'];qty=D(r['qty']);price=D(r['price'])
        direction=1 if r['side']=='buy' else -1
        fee=allocated[r['id']];book=books[symbol]
        while qty and book and book[0][0]!=direction:
            old_direction,remaining,entry,entry_fee,entry_time=book[0]
            matched=min(qty,remaining);gross=(price-entry)*matched*old_direction
            costs=matched*(entry_fee+fee);net=gross-costs
            trade=dict(symbol=symbol,direction='多头' if old_direction==1 else '空头',qty=float(matched),
                       entry=float(entry),exit=float(price),opened=entry_time,time=stamp.isoformat(),
                       gross=float(gross),fees=float(costs),net=float(net),
                       closed_entry_notional=float(entry*matched),net_return_pct=float(net/(entry*matched)*100))
            day_record(day)['trades'].append(trade)
            for record in (day_record(day),stock_record(day,symbol)):
                record['gross_realized']+=float(gross);record['closed_net']+=float(net)
                record['closed_entry_notional']+=float(entry*matched)
            stock_record(day,symbol)['closed_batches']+=1
            qty-=matched;book[0][1]-=matched
            if not book[0][1]:book.popleft()
        if qty:book.append([direction,qty,price,fee,stamp.isoformat()])
        for s,b in books.items():stock_record(day,s)['end_qty']=float(sum((d*q for d,q,p,f,t in b),D(0)))
    for day,record in daily.items():
        stocks=list(record['stocks'].values())
        for s in stocks:
            s['realized_less_day_fees']=s['gross_realized']-s['fees']
        for item in [record,*stocks]:
            basis=item['closed_entry_notional']
            item['net_return_pct']=item['closed_net']/basis*100 if basis else None
        record['stocks']=sorted(stocks,key=lambda s:s['closed_net'],reverse=True)
        record['realized_less_day_fees']=record['gross_realized']-record['fees']
        eligible=[s for s in record['stocks'] if s['closed_batches']]
        record['best']=eligible[0]['symbol'] if eligible else None
        record['worst']=eligible[-1]['symbol'] if eligible else None
        record['trades'].sort(key=lambda t:t['net'],reverse=True)
        record['open_positions']=sum(bool(s.get('end_qty')) for s in stocks)
    return daily


def persist_reviews(root, fills, universe, snapshot):
    import json
    directory=root/'state'/'reviews'
    ledger=directory/'fills.json'
    stored=json.loads(ledger.read_text(encoding='utf-8')) if ledger.exists() else []
    fields=('id','order_id','symbol','side','qty','price','transaction_time')
    merged={r['id']:r for r in stored}
    merged.update({r['id']:{k:r[k] for k in fields} for r in fills if r['symbol'] in universe})
    fills=list(merged.values())
    save_state(ledger,fills)
    records=build_reviews(fills,universe)
    current=str(datetime.fromisoformat(snapshot['clock']).astimezone(NY).date())
    for day,record in records.items():
        record['updated']=snapshot['fetched']
        record['reconciled']=snapshot['realized_complete']
        record['status']='交易中/待收盘核对' if day==current and snapshot['market_open'] else ('存在跨日持仓/待核对' if record['open_positions'] else '成交历史已汇总')
        path=directory/f'{day}.json'
        # Persist the latest observed valuation; a restart must not erase previous days.
        old={}
        if path.exists():
            import json
            old=json.loads(path.read_text(encoding='utf-8'))
        if day==current:
            observations=old.get('strategy_observations',[])
            version=snapshot.get('strategy_version','legacy')
            if not observations or observations[-1]['version']!=version:
                observations.append(dict(version=version,time=snapshot['fetched']))
            record['strategy_observations']=observations
            record['unrealized']=snapshot['unrealized_pnl']
            record['valuation_time']=snapshot['fetched']
            record['net_with_unrealized']=record['realized_less_day_fees']+snapshot['unrealized_pnl']
            baseline=snapshot.get('baseline')
            if baseline and baseline>0:
                record['baseline']=baseline
                record['model_return_pct']=record['net_with_unrealized']/baseline*100
        else:
            for k in ('unrealized','valuation_time','net_with_unrealized','baseline','model_return_pct','strategy_observations'):
                if k in old:record[k]=old[k]
        save_state(path,record)
    # Never delete earlier archives if the broker returns a shorter history.
    import json
    summaries=[]
    for path in sorted(directory.glob('????-??-??.json'),reverse=True):
        r=json.loads(path.read_text(encoding='utf-8'))
        summaries.append({k:r.get(k) for k in ('day','fees','gross_realized','closed_net','realized_less_day_fees','net_with_unrealized','orders','best','worst','status','updated','reconciled')})
    return dict(profile=PROFILE,days=summaries,current=records.get(current))
