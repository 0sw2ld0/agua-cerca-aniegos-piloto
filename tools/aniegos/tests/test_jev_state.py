from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from generate import instant
from jev_state import PersistentJevClient, DeferredRequest, DAY_LIMIT

NOW=instant('2026-11-05T17:00:00Z')
PAYLOAD={'model':'jev-latest','state':{'text':'Un aniego público.'},'questions':{'observed':{'type':'noul','instructions':'¿Se observó agua?'}}}

class Model:
    def __init__(self,fail=False):
        self.calls=0
        self.fail=fail
        self.ledger={'attempts':[], 'input_tokens':0,'output_tokens':0,'estimated_cost_usd':0}
    def ask(self,payload):
        self.calls+=1
        if self.fail: raise RuntimeError('never log secret')
        return {'model':'test','answers':{'observed':{'type':'noul','noul':0.5}},'usage':{'input_tokens':100,'output_tokens':4},'unwanted_echo':'secret'},False

class StateTests(unittest.TestCase):
    def test_relaunch_reuses_uncertain_answer_without_network(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'state.json'
            first=Model()
            PersistentJevClient(p,Path(d)/'private',NOW,first).ask(PAYLOAD)
            second=Model()
            client=PersistentJevClient(p,Path(d)/'other',NOW+timedelta(minutes=30),second)
            result,cached=client.ask(PAYLOAD)
            self.assertTrue(cached)
            self.assertEqual(second.calls,0)
            self.assertEqual(result['answers']['observed']['noul'],0.5)
            self.assertEqual(first.calls,1)
            self.assertNotIn('secret',p.read_text())
            self.assertNotIn('Un aniego público',p.read_text())

    def test_changed_evidence_is_new_but_old_version_keeps_cache(self):
        with tempfile.TemporaryDirectory() as d:
            m=Model();c=PersistentJevClient(Path(d)/'state',Path(d)/'private',NOW,m)
            c.ask(PAYLOAD)
            changed=deepcopy(PAYLOAD);changed['state']['text']='El aniego ya fue atendido.'
            c.ask(changed)
            c.ask(PAYLOAD)
            self.assertEqual(m.calls,2)

    def test_failure_waits_six_hours_and_is_never_a_negative(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'state';m=Model(fail=True)
            with self.assertRaises(RuntimeError): PersistentJevClient(p,Path(d)/'private',NOW,m).ask(PAYLOAD)
            client=PersistentJevClient(p,Path(d)/'other',NOW+timedelta(minutes=30),m)
            with self.assertRaises(DeferredRequest):client.ask(PAYLOAD)
            self.assertEqual(m.calls,1)
            later=PersistentJevClient(p,Path(d)/'later',NOW+timedelta(hours=7),m)
            with self.assertRaises(RuntimeError):later.ask(PAYLOAD)
            self.assertEqual(m.calls,2)

    def test_daily_and_total_limits_allow_cache_but_prevent_new_spending(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'state';m=Model();c=PersistentJevClient(p,Path(d)/'private',NOW,m)
            c.ask(PAYLOAD)
            c.state['attempts'].append({'day':'2026-11-05','committed_usd':DAY_LIMIT,'status':'success'})
            c.save()
            blocked=deepcopy(PAYLOAD);blocked['state']['text']='Un reporte nuevo'
            with self.assertRaises(DeferredRequest):c.ask(blocked)
            self.assertTrue(c.ask(PAYLOAD)[1])
            self.assertEqual(m.calls,1)
            c.state['previous_estimated_cost_usd']=0.10
            c.state['attempts']=[]
            with self.assertRaises(DeferredRequest):c.ask(blocked)

    def test_prior_trials_also_count_toward_their_day_limit(self):
        from jev_client import RESERVE_PER_ATTEMPT
        with tempfile.TemporaryDirectory() as d:
            m=Model();c=PersistentJevClient(Path(d)/'state',Path(d)/'private',NOW,m)
            c.state['attempts']=[{'day':'2026-11-05','committed_usd':DAY_LIMIT-RESERVE_PER_ATTEMPT,'status':'success'}]
            with self.assertRaises(DeferredRequest): c.ask(PAYLOAD)
            self.assertEqual(m.calls,0)

    def test_invalid_response_is_not_persisted_as_success(self):
        class Bad(Model):
            def ask(self,payload):
                result,cached=super().ask(payload)
                result['answers']['observed']['noul']=True
                return result,cached
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'state';c=PersistentJevClient(p,Path(d)/'private',NOW,Bad())
            with self.assertRaises(ValueError):c.ask(PAYLOAD)
            self.assertEqual(next(iter(c.state['entries'].values()))['status'],'error')

class FlowTests(unittest.TestCase):
    def test_two_independent_runs_make_zero_calls_second_time_even_with_tracking_url(self):
        from jev_pilot import run
        class CompleteModel(Model):
            def ask(self,payload):
                self.calls+=1
                answers={k:{'type':q['type'],q['type']:0.5 if q['type']=='noul' else next(iter(q['criteria']))} for k,q in payload['questions'].items()}
                return {'model':'test','answers':answers,'usage':{'input_tokens':100,'output_tokens':4}},False
        class Fetch:
            def check(self,url): pass
            def get(self,url):return '<script type="application/ld+json">'+json.dumps({'@type':'NewsArticle','headline':'Aniego en Lima','datePublished':'2026-11-05T10:00:00-05:00','articleBody':'Hay un aniego en la calle Las Flores con agua acumulada.'})+'</script>'
        def report(url):return {'candidates':[{'decision':'needs_review','url':url,'published_at':'2026-11-05T10:00:00-05:00'}],'errors':[]}
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);m=CompleteModel();state=root/'state.json'
            first=PersistentJevClient(state,root/'private1',NOW,m)
            run(report('https://rpp.pe/ejemplo/?utm_source=uno'),{},root/'out1',root/'private1',NOW,client=first,fetch=Fetch())
            self.assertEqual(m.calls,1)
            second=PersistentJevClient(state,root/'private2',NOW+timedelta(minutes=30),m)
            run(report('https://rpp.pe/ejemplo?utm_source=dos'),{},root/'out2',root/'private2',NOW+timedelta(minutes=30),client=second,fetch=Fetch())
            self.assertEqual(m.calls,1)
            self.assertEqual(json.loads((root/'out2/report.json').read_text())['reused_requests'],1)
            self.assertEqual(second.new_requests,0)

    def test_no_new_articles_is_success_not_absence_of_aniegos(self):
        from jev_pilot import run
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);m=Model();client=PersistentJevClient(root/'state',root/'private',NOW,m)
            code=run({'candidates':[],'errors':[]},{},root/'out',root/'private',NOW,client=client,fetch=object())
            self.assertEqual(code,0)
            self.assertEqual(m.calls,0)
            self.assertEqual(json.loads((root/'out/report.json').read_text())['coverage'],'partial')
