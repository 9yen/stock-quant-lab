"""One-off user-authorized legacy long liquidation, SIMULATE only."""
import json,time
from datetime import datetime,timezone
from decimal import Decimal,ROUND_DOWN
from pathlib import Path
from broker_connect import paper_moomoo_rows
from moomoo_observer import save
ROOT=Path(__file__).resolve().parent
LIMITS={'US.VOO':279,'US.FUTU':501}

def run(execute=False):
    from moomoo import OpenQuoteContext,OpenSecTradeContext,TrdMarket,TrdEnv,TrdSide,OrderType,TimeInForce,Session,RET_OK
    t=OpenSecTradeContext(filter_trdmarket=TrdMarket.US,host='127.0.0.1',port=11111)
    q=OpenQuoteContext(host='127.0.0.1',port=11111)
    path=ROOT/'state'/'brokers'/'moomoo_legacy_cleanup.json'
    ledger=json.loads(path.read_text(encoding='utf-8')) if path.exists() else dict(purpose='legacy_cleanup_not_strategy',orders={})
    def checked(result):
        ret,data=result
        if ret!=RET_OK:raise RuntimeError('Broker query failed')
        return data
    try:
        rows=paper_moomoo_rows(checked(t.get_acc_list()).to_dict('records'))
        if len(rows)!=1:raise RuntimeError('Ambiguous paper account')
        aid=int(rows[0]['acc_id'])
        for code,maxqty in LIMITS.items():
            if code in ledger['orders']:continue # uncertain/accepted submission never retried blindly
            positions=checked(t.position_list_query(code=code,trd_env=TrdEnv.SIMULATE,acc_id=aid,refresh_cache=True))
            matches=[r for r in positions.to_dict('records') if r['code']==code and float(r['qty'])!=0]
            if not matches:continue
            if len(matches)!=1:raise RuntimeError('Ambiguous position')
            p=matches[0];qty=float(p['qty'])
            if str(p['position_side'])!='LONG' or qty<=0 or qty>maxqty or float(p['can_sell_qty'])!=qty:raise RuntimeError('Position changed')
            orders=checked(t.order_list_query(code=code,trd_env=TrdEnv.SIMULATE,acc_id=aid,refresh_cache=True))
            terminal={'FILLED_ALL','CANCELLED_ALL','CANCELLED_PART','FAILED','DISABLED','DELETED'}
            if any(str(r['order_status']) not in terminal for r in orders.to_dict('records')):raise RuntimeError('Existing order')
            state=checked(q.get_market_state([code])).iloc[0]['market_state']
            if state not in ('MORNING','AFTERNOON','AFTER_HOURS_BEGIN'):raise RuntimeError('Unsupported trading session')
            quote=checked(q.get_market_snapshot([code])).iloc[0]
            bid=float(quote['bid_price']);ask=float(quote['ask_price'])
            if not 0<bid<=ask:raise RuntimeError('Invalid quote')
            # Limit bounds price deterioration to 0.5% below observed bid; never unrestricted market order after hours.
            price=float((Decimal(str(bid))*Decimal('.995')).quantize(Decimal('.01'),rounding=ROUND_DOWN))
            intent=dict(symbol=code,qty=qty,limit=price,status='preview',created=datetime.now(timezone.utc).isoformat())
            print(json.dumps(intent))
            if not execute:continue
            ledger['orders'][code]={**intent,'status':'submitting'};save(path,ledger)
            ret,data=t.place_order(price=price,qty=qty,code=code,trd_side=TrdSide.SELL,order_type=OrderType.NORMAL,
                trd_env=TrdEnv.SIMULATE,acc_id=aid,time_in_force=TimeInForce.DAY,session=Session.ETH if state=='AFTER_HOURS_BEGIN' else Session.RTH,
                remark='legacy-cleanup-20261017')
            if ret!=RET_OK:
                ledger['orders'][code]['status']='rejected';save(path,ledger)
                print('REJECTED',code,str(data)[:240]);continue
            order=data.iloc[0]
            ledger['orders'][code].update(status=str(order['order_status']),order_id=str(order['order_id']));save(path,ledger)
        for code,order in ledger['orders'].items():
            if order.get('order_id'):
                data=checked(t.order_list_query(order_id=order['order_id'],trd_env=TrdEnv.SIMULATE,acc_id=aid,refresh_cache=True))
                if len(data):
                    row=data.iloc[0];order.update(status=str(row['order_status']),filled=float(row['dealt_qty']),average=float(row['dealt_avg_price']))
        save(path,ledger)
        print(json.dumps(ledger,ensure_ascii=False))
    finally:q.close();t.close()
if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--execute-paper-cleanup',action='store_true');a=p.parse_args();run(a.execute_paper_cleanup)
