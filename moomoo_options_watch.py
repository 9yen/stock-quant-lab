"""Read-only near-ATM option board; snapshot timestamps shown explicitly."""
import time
from datetime import datetime,date
from zoneinfo import ZoneInfo
from moomoo_auto import DIR,write
from moomoo_options import valid_contract
from moomoo_market import CODES as ALL_CODES
CODES=ALL_CODES[:6] # Display sample only; automatic selection considers the full stock candidate pool.
NY=ZoneInfo('America/New_York')
def main():
    import moomoo as s
    while True:
        q=None
        try:
            q=s.OpenQuoteContext(host='127.0.0.1',port=11111);now=datetime.now(NY)
            ret,stocks=q.get_market_snapshot(CODES)
            if ret!=s.RET_OK:raise RuntimeError('underlying unavailable')
            prices={r['code']:float(r['last_price']) for r in stocks.to_dict('records')};contracts=[]
            for code in CODES:
                ret,dates=q.get_option_expiration_date(code)
                if ret!=s.RET_OK:continue
                days=sorted(str(r['strike_time'])[:10] for r in dates.to_dict('records') if 7<=(date.fromisoformat(str(r['strike_time'])[:10])-now.date()).days<=21)
                if not days:continue
                ret,chain=q.get_option_chain(code,start=days[0],end=days[0])
                if ret!=s.RET_OK:continue
                for direction in ('LONG','SHORT'):
                    rows=[r for r in chain.to_dict('records') if valid_contract(r,code,direction,now.date())]
                    rows.sort(key=lambda r:abs(float(r['strike_price'])-prices[code]))
                    if rows:contracts.append(rows[0])
            out=[]
            if contracts:
                ret,quotes=q.get_market_snapshot([r['code'] for r in contracts])
                if ret!=s.RET_OK:raise RuntimeError('options unavailable')
                lookup={r['code']:r for r in quotes.to_dict('records')}
                for r in contracts:
                    v=lookup.get(r['code'],{});bid,ask=float(v.get('bid_price',0)),float(v.get('ask_price',0))
                    out.append(dict(code=r['code'],underlying=r['stock_owner'],type=r['option_type'],expiry=r['strike_time'],strike=float(r['strike_price']),bid=bid,ask=ask,
                        contract_cost=ask*100,open_interest=float(v.get('option_open_interest',0)),volume=float(v.get('volume',0)),time=str(v.get('update_time','')),spread_pct=(ask-bid)/bid*100 if bid else None))
            write(DIR/'moomoo_options_watch.json',dict(ok=True,updated=now.isoformat(),rows=out))
        except Exception as exc:write(DIR/'moomoo_options_watch.json',dict(ok=False,updated=datetime.now(NY).isoformat(),rows=[],error_type=type(exc).__name__))
        finally:
            if q:q.close()
        time.sleep(120)
if __name__=='__main__':main()
