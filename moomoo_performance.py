"""FIFO review from observed cumulative order-fill increments; gross USD only."""
from collections import defaultdict,deque
from datetime import datetime
from zoneinfo import ZoneInfo
NY=ZoneInfo('America/New_York')

def review(fills):
    books=defaultdict(deque);closed=[];markers=[];byday=defaultdict(lambda:dict(gross=0.,basis=0.,trades=0))
    seen=set()
    for f in sorted(fills,key=lambda f:(f['time'],f['id'])):
        if f['id'] in seen:continue
        seen.add(f['id']);qty=float(f['qty']);price=float(f['price']);multiple=float(f.get('multiplier',1));direction=1 if f['side']=='BUY' else -1
        code=f['symbol'];book=books[code];stamp=datetime.fromisoformat(f['time']).astimezone(NY);day=str(stamp.date())
        label=('买入·开多' if direction==1 else '卖出·开空') if f['kind']=='ENTRY' else ('买入·回补' if direction==1 else '卖出·平多')
        markers.append(dict(symbol=code,underlying=f.get('underlying',code),time=f['time'],price=price,qty=qty,label=label,instrument=f.get('instrument','STOCK')))
        while qty and book and book[0]['direction']!=direction:
            old=book[0];matched=min(qty,old['qty']);basis=old['price']*matched*multiple
            gross=(price-old['price'])*matched*multiple*old['direction']
            trade=dict(symbol=code,instrument=f.get('instrument','STOCK'),direction='多头' if old['direction']>0 else '空头',qty=matched,entry=old['price'],exit=price,time=f['time'],opened=old['time'],gross=gross,return_pct=gross/basis*100,basis=basis,day=day)
            closed.append(trade);d=byday[day];d['gross']+=gross;d['basis']+=basis;d['trades']+=1
            qty-=matched;old['qty']-=matched
            if not old['qty']:book.popleft()
        if qty:book.append(dict(direction=direction,qty=qty,price=price,time=f['time']))
    days=[]
    for day,d in sorted(byday.items(),reverse=True):days.append(dict(day=day,**d,return_pct=d['gross']/d['basis']*100 if d['basis'] else None))
    return dict(gross_realized=sum(t['gross'] for t in closed),closed=list(reversed(closed)),days=days,markers=markers,
                fee_note='已实现毛利未扣费用；模拟接口不提供订单费用查询。收益率=平仓毛利/配对开仓金额。成交按订单累计数量增量归档，时间以券商订单更新时间为准。')
