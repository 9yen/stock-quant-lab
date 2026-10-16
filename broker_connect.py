"""Read-only paper account discovery. No order or trade-unlock methods."""
import argparse
import json
import socket
from pathlib import Path


def paper_moomoo_rows(rows):
    return [r for r in rows if str(r.get('trd_env')) == 'SIMULATE'
            and 'US' in str(r.get('trdmarket_auth', []))
            and str(r.get('sim_acc_type')) in ('STOCK', 'STOCK_AND_OPTION')]


def paper_tiger_rows(rows):
    return [r for r in rows if str(r.get('account_type')) == 'PAPER']


def masked(value):
    return '***' + str(value)[-4:]


def moomoo_probe():
    with socket.socket() as sock:
        sock.settimeout(2)
        if sock.connect_ex(('127.0.0.1', 11111)):
            return dict(status='needs_login', detail='Install/start moomoo OpenD and log in; localhost:11111 is unavailable.')
    from moomoo import OpenSecTradeContext, TrdMarket, RET_OK
    ctx=OpenSecTradeContext(filter_trdmarket=TrdMarket.US, host='127.0.0.1', port=11111)
    try:
        ret, data=ctx.get_acc_list()
        if ret != RET_OK:
            return dict(status='query_failed', detail='Check OpenD login and API agreements locally.')
        accounts=paper_moomoo_rows(data.to_dict('records'))
        return dict(status='paper_accounts_found' if accounts else 'no_us_paper_account',
                    accounts=[dict(account=masked(r['acc_id']), environment='SIMULATE', type=str(r['sim_acc_type'])) for r in accounts])
    finally:
        ctx.close()


def tiger_probe(config_path):
    if not config_path or not Path(config_path).is_file():
        return dict(status='needs_setup', detail='Activate Tiger OpenAPI and supply a local properties file via --tiger-config.')
    from tigeropen.tiger_open_config import TigerOpenClientConfig
    from tigeropen.trade.trade_client import TradeClient
    config=TigerOpenClientConfig(props_path=str(Path(config_path).resolve()))
    client=TradeClient(config)
    accounts=client.get_managed_accounts()
    rows=[dict(account=a.account, account_type=getattr(a,'account_type',None)) for a in accounts]
    papers=paper_tiger_rows(rows)
    # No account selection by number format; fail closed if broker type is unavailable.
    return dict(status='paper_accounts_found' if papers else 'paper_type_not_verified',
                accounts=[dict(account=masked(r['account']), environment='PAPER') for r in papers])


def main():
    parser=argparse.ArgumentParser(description='Read-only paper broker discovery; never submits orders.')
    parser.add_argument('--broker',choices=['all','moomoo','tiger'],default='all')
    parser.add_argument('--tiger-config')
    args=parser.parse_args()
    result=dict(mode='READ_ONLY', automatic_trading=False)
    for name, probe in [('moomoo',moomoo_probe),('tiger',lambda:tiger_probe(args.tiger_config))]:
        if args.broker not in ('all',name):continue
        try:result[name]=probe()
        except Exception as exc:
            # SDK error strings can contain credentials, paths or account data.
            result[name]=dict(status='connection_failed', error_type=type(exc).__name__, detail='Check local SDK/login/configuration; raw errors suppressed.')
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
