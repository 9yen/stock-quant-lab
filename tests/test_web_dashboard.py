import http.client
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from quant_lab.web_dashboard import LiveData, make_handler


class WebTests(unittest.TestCase):
    def setUp(self):
        scratch=Path(__file__).resolve().parents[1]/'state'/'test-scratch'
        scratch.mkdir(parents=True,exist_ok=True)
        self.temp=TemporaryDirectory(dir=scratch)
        self.root=Path(self.temp.name)
        (self.root/'web').mkdir()
        (self.root/'web'/'index.html').write_text('<meta content="__CSRF_TOKEN__">')
        self.store=LiveData(self.root)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.store,'test-token',0))
        self.server.daemon_threads=True
        self.port=self.server.server_address[1]
        self.server.RequestHandlerClass=make_handler(self.store,'test-token',self.port)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()

    def tearDown(self):
        self.store.closing.set()
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def request(self,path,method='GET',headers=None):
        c=http.client.HTTPConnection('127.0.0.1',self.port,timeout=3)
        c.request(method,path,headers=headers or {})
        response=c.getresponse()
        status,body=response.status,response.read()
        c.close()
        return status,body

    def test_static_injection_and_file_boundary(self):
        code,body=self.request('/')
        self.assertEqual(code,200)
        self.assertIn(b'test-token',body)
        self.assertNotIn(b'__CSRF_TOKEN__',body)
        self.assertEqual(self.request('/.env')[0],404)
        self.assertEqual(self.request('/state/paper_authorization.json')[0],404)
        self.assertEqual(self.request('/../.env')[0],404)

    def test_rebinding_host_blocked(self):
        self.assertEqual(self.request('/',headers={'Host':'evil.example'})[0],403)

    def test_stop_requires_origin_and_token(self):
        self.assertEqual(self.request('/api/stop','POST')[0],403)
        headers={'Origin':'https://evil.example','X-Dashboard-Token':'test-token'}
        self.assertEqual(self.request('/api/stop','POST',headers)[0],403)
        self.assertFalse((self.root/'STOP_PAPER').exists())
        headers={'Origin':f'http://127.0.0.1:{self.port}','X-Dashboard-Token':'test-token'}
        self.assertEqual(self.request('/api/stop','POST',headers)[0],200)
        self.assertTrue((self.root/'STOP_PAPER').exists())

    def test_status_payload_whitelists_authorization(self):
        (self.root/'state').mkdir()
        (self.root/'state'/'paper_authorization.json').write_text(json.dumps(dict(session='2026-10-16',account_id='private-id',secret='hidden')))
        code,body=self.request('/api/status')
        self.assertEqual(code,200)
        self.assertNotIn(b'private-id',body)
        self.assertNotIn(b'hidden',body)
        self.assertEqual(json.loads(body)['mode'],'PAPER ONLY')

    def test_review_date_validation_and_archive(self):
        p=self.root/'state'/'reviews';p.mkdir(parents=True)
        (p/'2026-10-16.json').write_text('{"day":"2026-10-16","fees":1.02}')
        self.assertEqual(self.request('/api/review?day=2026-10-16')[0],200)
        self.assertEqual(self.request('/api/review?day=2026-10-15')[0],404)
        self.assertEqual(self.request('/api/review?day=..%2F.env')[0],400)

    def test_live_stream(self):
        c=http.client.HTTPConnection('127.0.0.1',self.port,timeout=3)
        c.request('GET','/events')
        response=c.getresponse()
        self.assertEqual(response.getheader('Content-Type'),'text/event-stream')
        line=response.readline().decode('utf-8')
        self.assertTrue(line.startswith('data: '))
        self.assertEqual(json.loads(line[6:])['mode'],'PAPER ONLY')
        c.close()

    @patch('quant_lab.chart_data.chart_snapshot')
    def test_chart_symbol_validation_and_cache(self,fetch):
        fetch.return_value=dict(symbol='QQQ',bars=[],bar_minutes=1)
        self.assertEqual(self.request('/api/chart?symbol=QQQ')[0],200)
        code,body=self.request('/api/chart?symbol=QQQ')
        self.assertEqual(json.loads(body)['symbol'],'QQQ')
        fetch.assert_called_once_with(self.root,'QQQ')
        self.assertEqual(self.request('/api/chart?symbol=INVALID')[0],400)
        self.assertEqual(self.request('/api/chart?symbol=..%2F.env')[0],400)

    def test_moomoo_stop_is_authenticated_and_separate(self):
        self.assertEqual(self.request('/api/moomoo/stop','POST')[0],403)
        headers={'Origin':f'http://127.0.0.1:{self.port}','X-Dashboard-Token':'test-token'}
        self.assertEqual(self.request('/api/moomoo/stop','POST',headers)[0],200)
        self.assertTrue((self.root/'STOP_MOOMOO').exists())
        self.assertFalse((self.root/'STOP_PAPER').exists())

    def test_moomoo_assets_and_private_config_boundary(self):
        (self.root/'web'/'moomoo.html').write_text('<meta content="__CSRF_TOKEN__">')
        (self.root/'web'/'moomoo.css').write_text('body{}')
        code,body=self.request('/moomoo.html')
        self.assertEqual(code,200);self.assertIn(b'test-token',body)
        self.assertEqual(self.request('/moomoo.css')[0],200)
        self.assertEqual(self.request('/state/brokers/moomoo_auto_config.json')[0],404)

    def test_moomoo_chart_whitelist_and_separate_payload(self):
        folder=self.root/'state'/'brokers'/'moomoo_charts';folder.mkdir(parents=True)
        (folder/'US.AMD.json').write_text('{"symbol":"US.AMD","candles":[]}')
        code,body=self.request('/api/moomoo/chart?symbol=US.AMD')
        self.assertEqual(code,200);self.assertEqual(json.loads(body)['symbol'],'US.AMD')
        self.assertEqual(self.request('/api/moomoo/chart?symbol=..%2F.env')[0],400)
        self.assertEqual(self.request('/api/moomoo/chart?symbol=US.NOT_REAL')[0],400)

    def test_optimizer_serves_only_review_report(self):
        folder=self.root/'state'/'optimizer';folder.mkdir(parents=True)
        (folder/'latest.json').write_text('{"status":"ACCUMULATING","valid_days":1}')
        code,body=self.request('/api/optimizer');self.assertEqual(code,200);self.assertEqual(json.loads(body)['valid_days'],1)
        self.assertEqual(self.request('/state/optimizer/active.json')[0],404)

    def test_automation_status_whitelists_private_config_and_shared_assets(self):
        folder=self.root/'state';folder.mkdir(parents=True)
        (folder/'automation_config.json').write_text('{"alpaca_enabled":true,"alpaca_account_id":"private-account","secret":"hidden"}')
        code,body=self.request('/api/automation')
        self.assertEqual(code,200)
        self.assertTrue(json.loads(body)['alpaca']['enabled'])
        self.assertNotIn(b'private-account',body);self.assertNotIn(b'hidden',body)
        self.assertEqual(self.request('/state/automation_config.json')[0],404)
        for name in ('automation.js','automation.css'):
            (self.root/'web'/name).write_text('/* shared status */')
            self.assertEqual(self.request('/'+name)[0],200)

    def test_locale_routes_are_explicit_and_do_not_serve_arbitrary_json(self):
        folder=self.root/'web'/'locales';folder.mkdir()
        for name in ('en','zh-CN'):
            (folder/(name+'.json')).write_text('{"key":"translation"}')
            self.assertEqual(self.request('/locales/'+name+'.json')[0],200)
        self.assertEqual(self.request('/locales/../../.env')[0],404)
        self.assertEqual(self.request('/locales/private.json')[0],404)
