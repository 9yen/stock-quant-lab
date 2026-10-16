from datetime import datetime,timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import patch
from zoneinfo import ZoneInfo
from quant_lab.options_preview import premium_quantity,quote_ready,collect_options,POLICY


class OptionRiskTests(TestCase):
    def test_multiplier_and_budget(self):
        self.assertEqual(premium_quantity(100000,5),1)
        self.assertEqual(premium_quantity(100000,5.01),0)
        self.assertEqual(premium_quantity(100000,.1),1)
        self.assertEqual(premium_quantity(100000,1,multiplier=10),0)
        self.assertEqual(premium_quantity(100000,float('nan')),0)
        self.assertFalse(POLICY['automatic_enabled'])

    def test_indicative_never_executable(self):
        now=datetime.now(ZoneInfo('UTC'));q=NS(bid_price=1,ask_price=1.01,timestamp=now)
        self.assertFalse(quote_ready('indicative',q,now))
        self.assertTrue(quote_ready('opra',q,now))
        q.timestamp-=timedelta(seconds=11)
        self.assertFalse(quote_ready('opra',q,now))

    @patch.dict('os.environ',{'ALPACA_KEY':'test','ALPACA_SECRET':'test'})
    @patch('alpaca.data.historical.option.OptionHistoricalDataClient')
    @patch('alpaca.data.historical.StockHistoricalDataClient')
    @patch('alpaca.trading.client.TradingClient')
    def test_opra_403_is_observation_only(self,tc,sc,oc):
        now=datetime(2026,10,16,11,tzinfo=ZoneInfo('America/New_York'))
        tc.return_value.get_clock.return_value=NS(timestamp=now)
        tc.return_value.get_account.return_value=NS(options_approved_level=3,options_trading_level=3,options_buying_power='100000',equity='100000')
        sc.return_value.get_stock_latest_quote.return_value={s:NS(bid_price=100.99,ask_price=101.01) for s in ('SPY','QQQ')}
        def contracts(request):
            s=request.underlying_symbols[0]
            return NS(option_contracts=[NS(symbol=s+kind,underlying_symbol=s,type=kind,tradable=True,expiration_date=now.date(),size='100',strike_price=101) for kind in ('call','put')])
        tc.return_value.get_option_contracts.side_effect=contracts
        class Denied(Exception):status_code=403
        def quotes(request):
            if request.feed.value=='opra':raise Denied()
            return {s:NS(bid_price=1,ask_price=1.01,timestamp=now) for s in request.symbol_or_symbols}
        oc.return_value.get_option_latest_quote.side_effect=quotes
        result=collect_options(Path(__file__).resolve().parents[1])
        self.assertEqual(result['status'],'BLOCKED_OPRA')
        self.assertEqual(result['opra_http_status'],403)
        self.assertFalse(result['automatic_enabled'])
        self.assertEqual(len(result['rows']),4)
        self.assertTrue(all(not row['quote_ready'] for row in result['rows']))
        tc.return_value.submit_order.assert_not_called()
