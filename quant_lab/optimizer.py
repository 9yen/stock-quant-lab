"""Closed-session stock-only parameter evaluation. No trading clients or risk-limit writes."""
import hashlib,json,math,os,uuid
from collections import defaultdict
from datetime import datetime,timedelta,time
from pathlib import Path
from zoneinfo import ZoneInfo
from .scanner import signal_context
from .review import order_fees
NY=ZoneInfo('America/New_York')
PROFILES={
 'baseline':dict(min_rvol=1.,max_range=.03,max_breakout_atr=1.5),
 'volume_125':dict(min_rvol=1.25,max_range=.03,max_breakout_atr=1.5),
 'volume_150':dict(min_rvol=1.5,max_range=.03,max_breakout_atr=1.5),
 'range_2':dict(min_rvol=1.,max_range=.02,max_breakout_atr=1.5),
 'breakout_1':dict(min_rvol=1.,max_range=.03,max_breakout_atr=1.)}
MIN_DAYS=30
MIN_LIVE_STOCK_CLOSES=100
MODEL_VERSION='native-orb-stock-v1'

def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    tmp.write_text(json.dumps(value,ensure_ascii=False,allow_nan=False),encoding='utf-8');os.replace(tmp,path)

def read(path,default):
    try:return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default
    except (ValueError,OSError):return default

def entry_filter(row,profile='baseline'):
    if profile not in PROFILES:return False
    p=PROFILES[profile]
    try:
        high,low,atr,price,rvol=(float(row[k]) for k in ('range_high','range_low','atr14','price','rvol'))
        direction=row['signal'];distance=abs(price-(high if direction=='LONG' else low))
        return (direction in ('LONG','SHORT') and all(math.isfinite(v) and v>0 for v in (high,low,atr,price))
                and math.isfinite(rvol) and high>=low and rvol>=p['min_rvol'] and (high-low)/low<=p['max_range']
                and distance<=p['max_breakout_atr']*atr)
    except (KeyError,ValueError,TypeError,ZeroDivisionError):return False

def active_profile(root,day):
    record=read(root/'state'/'optimizer'/'active.json',{})
    if record.get('schema')!=1 or record.get('model')!=MODEL_VERSION or record.get('profile') not in PROFILES:return 'baseline'
    if record.get('parameters')!=PROFILES[record['profile']]:return 'baseline'
    if not record.get('effective_from') or record['effective_from']>str(day):return 'baseline'
    return record['profile']

def archive_chart(root,chart,session_type,now):
    """Archive only calendar-confirmed, complete closed sessions; never overwrite a valid archive."""
    if session_type not in ('WHOLE','HALF') or not chart.get('candles'):return False
    bars=chart['candles'];parsed=[datetime.fromisoformat(b['time']).astimezone(NY) for b in bars]
    day=parsed[0].date()
    if day!=now.astimezone(NY).date() or any(t.date()!=day for t in parsed):return False
    opened=datetime.combine(day,time(9,31),NY);closed=datetime.combine(day,time(13 if session_type=='HALF' else 16),NY)
    if now.astimezone(NY)<closed+timedelta(minutes=10):return False
    times=set(parsed);expected=int((closed-opened).total_seconds()/60)
    if len(times)!=len(parsed) or len(times)<math.ceil(expected*.95) or closed-timedelta(minutes=1) not in times:return False
    if any(opened+timedelta(minutes=i) not in times for i in range(15)):return False
    for b,t in zip(bars,parsed):
        if not opened<=t<closed:return False
        try:
            if not all(math.isfinite(float(b[k])) and float(b[k])>0 for k in ('open','high','low','close')):return False
            if not b['low']<=min(b['open'],b['close'])<=max(b['open'],b['close'])<=b['high']:return False
            if not math.isfinite(float(b['volume'])) or b['volume']<0:return False
        except (KeyError,TypeError,ValueError):return False
    payload=dict(symbol=chart['symbol'],day=str(day),session_type=session_type,source='moomoo',model=MODEL_VERSION,candles=bars)
    payload['sha256']=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
    file=root/'state'/'optimizer'/'archive'/'moomoo'/str(day)/(chart['symbol']+'.json')
    if file.exists():return True
    write(file,payload);return True

def verify_archive(record):
    if record.get('source')!='moomoo' or record.get('model')!=MODEL_VERSION:return False
    copy={k:v for k,v in record.items() if k!='sha256'}
    return hashlib.sha256(json.dumps(copy,sort_keys=True).encode()).hexdigest()==record.get('sha256')

def features(record):
    bars=sorted([dict(b,time=datetime.fromisoformat(b['time']).astimezone(NY)) for b in record['candles']],key=lambda b:b['time'])
    opened=bars[0]['time'].replace(hour=9,minute=31,second=0,microsecond=0)
    by={int((b['time']-opened).total_seconds()/60):b for b in bars}
    high=max(by[i]['high'] for i in range(15));low=min(by[i]['low'] for i in range(15));past=[];signals=[]
    for minute,b in sorted(by.items()):
        past.append(b)
        if minute<20 or minute-1 not in by:continue
        context=signal_context(past,b['time']+timedelta(seconds=90))
        direction='LONG' if by[minute-1]['close']<=high<b['close'] else 'SHORT' if by[minute-1]['close']>=low>b['close'] else 'WAIT'
        if direction=='WAIT' or not context.get('filter_long' if direction=='LONG' else 'filter_short'):continue
        signals.append(dict(minute=minute,signal=direction,range_high=high,range_low=low,price=b['close'],atr14=context.get('atr14'),rvol=context.get('rvol')))
    return dict(bars={str(k):{key:(value.isoformat() if key=='time' else value) for key,value in b.items()} for k,b in by.items()},signals=signals,session_type=record['session_type'])

def load_days(root):
    output={};skipped=[];base=root/'state'/'optimizer'
    for folder in sorted((base/'archive'/'moomoo').glob('????-??-??')):
        records=[r for p in folder.glob('*.json') if verify_archive(r:=read(p,{}))]
        if len(records)<80:skipped.append(dict(day=folder.name,valid_symbols=len(records)));continue
        items={}
        for record in records:
            cache=base/'features'/folder.name/(record['symbol']+'.json');item=read(cache,{})
            if item.get('source_hash')!=record['sha256']:
                item=dict(source_hash=record['sha256'],data=features(record));write(cache,item)
            items[record['symbol']]=item['data']
        output[folder.name]=items
    return output,skipped

def fee(qty,price,side):return float(sum(order_fees(qty,qty*price,side).values()))

def replay(days,profile,slippage=.0005,initial=1000000.):
    """Conservative OHLC proxy: entry t+2, stop first if both touched, estimated stock fees."""
    balance=initial;peak=balance;drawdown=0.;daily=[];trades=[]
    for day,stocks in sorted(days.items()):
        baseline=balance;positions={};cooldown={};counts=defaultdict(int);total=0;halted=False;pending=defaultdict(list)
        end=209 if all(s['session_type']=='HALF' for s in stocks.values()) else 389
        for symbol,data in stocks.items():
            for row in data['signals']:
                if entry_filter(row,profile):pending[row['minute']+2].append((symbol,row))
        def close(code,price,minute,reason):
            nonlocal balance
            p=positions.pop(code);price*=1-slippage*p['d'];cost=fee(p['qty'],price,'sell' if p['d']>0 else 'buy')
            net=(price-p['entry'])*p['qty']*p['d']-cost-p['fee'];balance+=net+p['fee']
            trades.append(dict(day=day,symbol=code,net=net,reason=reason,minute=minute))
        for minute in range(end):
            for code,p in list(positions.items()):
                bar=stocks[code]['bars'].get(str(minute))
                if not bar:continue
                if halted or minute>=end-10:close(code,bar['open'],minute,'DAILY_OR_PRECLOSE')
            for code,row in sorted(pending.get(minute,[])):
                if halted or minute>=end-30 or code in positions or len(positions)>=10 or counts[code]>=19 or total>=100 or minute-cooldown.get(code,-1000)<5:continue
                bar=stocks[code]['bars'].get(str(minute))
                if not bar:continue
                equity=balance+sum((p['mark']-p['entry'])*p['qty']*p['d'] for p in positions.values());gross=sum(p['mark']*p['qty'] for p in positions.values())
                direction=1 if row['signal']=='LONG' else -1;price=bar['open']*(1+slippage*direction)
                if abs(price/row['price']-1)>.005 or (direction>0 and price<=row['range_high']) or (direction<0 and price>=row['range_low']):continue
                qty=math.floor(min(equity*.1,max(0,equity-gross))/(price*1.005))
                if qty<1:continue
                cost=fee(qty,price,'buy' if direction>0 else 'sell');balance-=cost
                positions[code]=dict(qty=qty,d=direction,entry=price,mark=price,fee=cost);cooldown[code]=minute;counts[code]+=1;total+=1
            for code,p in list(positions.items()):
                bar=stocks[code]['bars'].get(str(minute))
                if not bar:continue
                stop=p['entry']*(.99 if p['d']>0 else 1.01);target=p['entry']*(1.02 if p['d']>0 else .98)
                if (bar['low']<=stop if p['d']>0 else bar['high']>=stop):
                    close(code,min(bar['open'],stop) if p['d']>0 else max(bar['open'],stop),minute,'STOP');continue
                if (bar['high']>=target if p['d']>0 else bar['low']<=target):close(code,target,minute,'TARGET');continue
                p['mark']=bar['close']
            equity=balance+sum((p['mark']-p['entry'])*p['qty']*p['d'] for p in positions.values());peak=max(peak,equity);drawdown=max(drawdown,1-equity/peak)
            if equity<=baseline*.98:halted=True
        if positions:return dict(valid=False,reason='Unresolved missing-data positions')
        daily.append(dict(day=day,net=balance-baseline,trades=total))
    return dict(valid=True,net=balance-initial,drawdown=drawdown,trades=len(trades),days=daily,initial=initial,slippage=slippage)

def choose(train,validation,holdout,profile_results,current='baseline'):
    """Select on train only. Validation/holdout may veto, never pick another winner."""
    scores={p:r['train']['net']-r['train']['drawdown']*r['train']['initial'] for p,r in profile_results.items() if r['train'].get('valid')}
    if not scores or current not in scores:return None,'回放数据无效'
    winner=max(scores,key=scores.get)
    if winner==current:return None,'现行参数仍优'
    r=profile_results[winner];base=profile_results[current]
    for stage in ('train','validation','holdout','stress'):
        a,b=r[stage],base[stage]
        if not a.get('valid') or not b.get('valid') or a['net']<=0 or a['net']<=b['net']+a['initial']*.0005:return None,'候选未通过扣费及压力比较'
        if a['drawdown']>b['drawdown']+1e-9:return None,'候选回撤更差'
    if r['validation']['trades']<10 or r['holdout']['trades']<10:return None,'样本外成交不足'
    for stage in ('validation','holdout'):
        a=r[stage]['days'];b={x['day']:x['net'] for x in base[stage]['days']}
        if sum(x['net']>b.get(x['day'],0) for x in a)<math.ceil(len(a)*.6):return None,'改善集中于少数交易日'
    return winner,'训练、验证、留出与双倍滑点检查通过'

def deployment_safe(root,now):
    status=read(root/'state'/'brokers'/'moomoo_auto_status.json',{});journal=read(root/'state'/'brokers'/'moomoo_auto_state.json',{})
    try:fresh=-5<=(now-datetime.fromisoformat(status['updated'])).total_seconds()<=30
    except (KeyError,ValueError,TypeError):return False
    terminal={'FILLED_ALL','CANCELLED_ALL','CANCELLED_PART','FAILED','SUBMIT_FAILED','DISABLED','DELETED'}
    qty=defaultdict(float)
    for o in journal.get('orders',[]):
        if o.get('status') not in terminal:return False
        qty[o['symbol']]+=float(o.get('filled',0))*(1 if o['side']=='BUY' else -1)
    return (fresh and status.get('enabled') and status.get('positions')==0 and status.get('status') in ('WAIT_REGULAR_SESSION','WAIT_TRADING_DAY') and not any(abs(v)>1e-7 for v in qty.values()))

def promote(root,profile,effective_from,now,evidence):
    if profile not in PROFILES or not deployment_safe(root,now):return False
    base=root/'state'/'optimizer';old=read(base/'active.json',{})
    version=now.strftime('%Y%m%dT%H%M%S')+'-'+profile
    if old:write(base/'versions'/(version+'-previous.json'),old)
    record=dict(schema=1,model=MODEL_VERSION,profile=profile,parameters=PROFILES[profile],effective_from=effective_from,activated_at=now.isoformat(),version=version,evidence=evidence)
    write(base/'versions'/(version+'.json'),record);write(base/'active.json',record);return True
