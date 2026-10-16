import json,unittest
from datetime import datetime,timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo
from quant_lab.optimizer import (archive_chart,verify_archive,features,replay,entry_filter,active_profile,PROFILES,choose,promote,write)
from daily_optimize import run
NY=ZoneInfo('America/New_York')
class OptimizerTests(unittest.TestCase):
    def setUp(self):
        scratch=Path(__file__).resolve().parents[1]/'state'/'test-scratch';scratch.mkdir(parents=True,exist_ok=True)
        self.temp=TemporaryDirectory(dir=scratch);self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.now=datetime(2026,10,16,17,tzinfo=NY)
    def candles(self,half=False):
        start=self.now.replace(hour=9,minute=31,second=0,microsecond=0)
        return [dict(time=(start+timedelta(minutes=i)).isoformat(),open=100.,high=100.1,low=99.9,close=100.,volume=1000.) for i in range(209 if half else 389)]
    def test_only_closed_complete_official_session_archived(self):
        chart=dict(symbol='US.AAPL',candles=self.candles())
        self.assertFalse(archive_chart(self.root,chart,'WHOLE',self.now.replace(hour=15)))
        self.assertFalse(archive_chart(self.root,dict(chart,candles=chart['candles'][1:]),'WHOLE',self.now))
        self.assertTrue(archive_chart(self.root,chart,'WHOLE',self.now))
        p=self.root/'state/optimizer/archive/moomoo/2026-10-16/US.AAPL.json';record=json.loads(p.read_text())
        self.assertTrue(verify_archive(record));record['candles'][0]['close']=101;self.assertFalse(verify_archive(record))
    def test_half_day_requires_calendar_half(self):
        chart=dict(symbol='US.AAPL',candles=self.candles(True))
        self.assertFalse(archive_chart(self.root,chart,'WHOLE',self.now))
        self.assertTrue(archive_chart(self.root,chart,'HALF',self.now.replace(hour=13,minute=20)))
    def test_profiles_can_only_tighten_original_filter(self):
        for p in PROFILES.values():
            self.assertGreaterEqual(p['min_rvol'],1);self.assertLessEqual(p['max_range'],.03);self.assertLessEqual(p['max_breakout_atr'],1.5)
        row=dict(signal='LONG',range_high=101,range_low=100,price=101.1,atr14=1,rvol=1.1)
        self.assertTrue(entry_filter(row));self.assertFalse(entry_filter(row,'volume_125'))
    def test_effective_next_session_and_invalid_parameters_fail_closed(self):
        record=dict(schema=1,model='native-orb-stock-v1',profile='volume_125',parameters=PROFILES['volume_125'],effective_from='2026-10-19')
        p=self.root/'state/optimizer/active.json';write(p,record)
        self.assertEqual(active_profile(self.root,'2026-10-16'),'baseline');self.assertEqual(active_profile(self.root,'2026-10-19'),'volume_125')
        record['parameters']=dict(min_rvol=0);write(p,record);self.assertEqual(active_profile(self.root,'2026-10-19'),'baseline')
    def metric(self,net,days=5):
        return dict(valid=True,net=net,drawdown=.001,trades=30,initial=1000000.,days=[dict(day=str(i),net=net/days) for i in range(days)])
    def results(self):
        return {p:{k:self.metric(5000) for k in ('train','validation','holdout','stress')} for p in PROFILES}
    def test_holdout_veto_does_not_pick_second_candidate(self):
        results=self.results();results['volume_125']={k:self.metric(10000) for k in ('train','validation','holdout','stress')};results['range_2']={k:self.metric(9000) for k in ('train','validation','holdout','stress')}
        results['volume_125']['holdout']=self.metric(-1)
        winner,_=choose({}, {}, {},results);self.assertIsNone(winner)
    def test_strong_candidate_must_pass_stress_and_all_periods(self):
        results=self.results();results['volume_125']={k:self.metric(10000) for k in ('train','validation','holdout','stress')}
        winner,_=choose({}, {}, {},results);self.assertEqual(winner,'volume_125')
        results['volume_125']['stress']=self.metric(4000);winner,_=choose({}, {}, {},results);self.assertIsNone(winner)
    def test_promotion_refuses_open_positions_and_unknown_orders(self):
        p=self.root/'state/brokers/moomoo_auto_status.json';write(p,dict(enabled=True,updated=self.now.isoformat(),positions=1,status='WAIT_REGULAR_SESSION'))
        self.assertFalse(promote(self.root,'volume_125','2026-10-19',self.now,{}))
        write(p,dict(enabled=True,updated=self.now.isoformat(),positions=0,status='WAIT_REGULAR_SESSION'))
        write(self.root/'state/brokers/moomoo_auto_state.json',dict(orders=[dict(status='UNKNOWN')]))
        self.assertFalse(promote(self.root,'volume_125','2026-10-19',self.now,{}))
    def test_safe_promotion_retains_risk_config_bytes(self):
        write(self.root/'state/brokers/moomoo_auto_status.json',dict(enabled=True,updated=self.now.isoformat(),positions=0,status='WAIT_REGULAR_SESSION'))
        write(self.root/'state/brokers/moomoo_auto_state.json',dict(orders=[]))
        risk=self.root/'state/brokers/moomoo_auto_config.json';write(risk,dict(enabled=True,gross=1.,daily_loss=.02));before=risk.read_bytes()
        self.assertTrue(promote(self.root,'volume_125','2026-10-19',self.now,{}));self.assertEqual(before,risk.read_bytes())
        self.assertEqual(active_profile(self.root,'2026-10-19'),'volume_125')
    def test_insufficient_samples_never_change_strategy(self):
        report=run(self.root,True,self.now,calendar=[])
        self.assertEqual(report['status'],'ACCUMULATING');self.assertFalse((self.root/'state/optimizer/active.json').exists())
    def test_future_data_cannot_change_earlier_features(self):
        bars=self.candles()
        for i in range(22,30):bars[i]=dict(bars[i],open=100,high=101.3,low=99.9,close=101.2,volume=3000)
        a=features(dict(candles=bars[:31],session_type='WHOLE'))['signals']
        b=[r for r in features(dict(candles=bars,session_type='WHOLE'))['signals'] if r['minute']<=30]
        self.assertEqual(a,b)
    def test_replay_stop_before_target_and_charges_costs(self):
        bars={str(i):{k:v for k,v in b.items() if k!='time'} for i,b in enumerate(self.candles())}
        bars['22'].update(high=103,low=98)
        signal=dict(minute=20,signal='LONG',range_high=99.9,range_low=99,price=100,atr14=1,rvol=2)
        days={'2026-10-16':{'US.AAPL':dict(bars=bars,signals=[signal],session_type='WHOLE')}}
        result=replay(days,'baseline');self.assertTrue(result['valid']);self.assertEqual(result['trades'],1);self.assertLess(result['net'],0)
