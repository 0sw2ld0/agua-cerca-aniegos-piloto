import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from generate import instant
from jev_pilot import analyze, passages, run, selected
from jev_questions import confirmed_date, date_extraction, date_spans, questions_for

NOW=instant('2026-11-05T17:00:00Z')
DOC={'url':'https://rpp.pe/example','title':'Aniego','published':'2026-11-05T10:00:00-05:00'}

class Client:
    def ask(self, payload):
        answers={ 'passage_0__'+name:{'type':'noul','noul':0.99 if name in ('observed','water_present') else 0.01} for name in questions_for('passage_0')}
        answers.update(has_place={'type':'noul','noul':0.99},place={'type':'choice','choice':'place_0'},has_explicit_date={'type':'noul','noul':0.01})
        return {'model':'mock','answers':answers},False

class PilotTests(unittest.TestCase):
    def test_unknown_event_day_never_uses_publication_day(self):
        r=analyze(DOC,'Se reporta un aniego en la calle Los Cedros con agua acumulada.',Client(),NOW)
        self.assertIsNone(r['incident_date'])
        self.assertEqual(r['decision'],'review_date')
        self.assertIsNone(r['coordinates'])
        self.assertFalse(r['eligible_for_publication'])

    def test_invalid_choice_or_probability_fails_closed(self):
        for answers in [{'gate':{'type':'noul','noul':True}}, {'gate':{'type':'noul','noul':1},'place':{'type':'choice','choice':'invented'}}]:
            with self.assertRaises(ValueError): selected(answers,'gate','place',{'place_0':'Known place'})

    def test_paragraph_bounds_and_abbreviation_preserved(self):
        p=passages('<p>Se reporta un aniego en Av. Los Cedros con agua acumulada frente al mercado.</p><p>También hay un aniego en la calle Las Flores con bombas trabajando.</p>')
        self.assertEqual(len(p),2)
        self.assertIn('Av.',p[0])
        self.assertEqual(passages('Un aniego '+ 'x'*2000),[])

    def test_dated_passage_is_considered_after_two_undated_leads(self):
        lead = 'Vecinos reportan aniegos en varios sectores de Lima Sur.'
        other = 'En una de las calles anegadas se interrumpió el tránsito por la acumulación de agua.'
        date_context = 'El miércoles hubo lluvias en Villa El Salvador.'
        heroes = 'El COEN informó de una inundación el miércoles en la avenida Los Héroes.'
        selected_passages = passages('\n'.join([lead, other, date_context, heroes]), '2026-11-05')
        self.assertEqual([text for text, _ in selected_passages], [lead, heroes])
        self.assertEqual(dict(selected_passages[1][1])['before'], date_context)

    def test_association_requires_typed_support_for_same_incident(self):
        target = 'El COEN informó de una inundación en la avenida Los Héroes.'
        nearby = (('before', 'El miércoles hubo lluvias en Villa El Salvador.'),
                  ('after', 'El 16 de agosto de 2026 hubo otro aniego en el sector.'))
        payload, dates = date_extraction(DOC, target, nearby)
        self.assertEqual(payload['state']['fragment'], target)
        self.assertEqual(dates['date_0']['segment'], 'before')
        def answers(first, second):
            return {f'date_{i}': {'type': 'noul', 'noul': p} for i, p in enumerate((first, second))}
        self.assertEqual(confirmed_date(answers(.95, .05), dates)['date'], '2026-11-04')
        self.assertIsNone(confirmed_date(answers(.50, .05), dates))
        self.assertIsNone(confirmed_date(answers(.95, .95), dates))

    def test_date_candidates_do_not_use_unanchored_or_future_weekdays(self):
        self.assertEqual(date_spans('El día anterior. El próximo miércoles.', '2026-11-05'), [])
        self.assertEqual(date_spans('jueves 5 de noviembre de 2026', '2026-11-04'),
                         [{'span': '5 de noviembre de 2026', 'date': '2026-11-05', 'year_inferred': False}])

    def test_api_failure_produces_error_artifact_not_empty_success(self):
        class Broken(Client):
            ledger={'attempts':[], 'input_tokens':0,'output_tokens':0,'estimated_cost_usd':0}
            def ask(self,payload): raise RuntimeError('secret must never appear')
        class Fetch:
            def check(self,url): pass
            def get(self,url): return '<script type="application/ld+json">'+json.dumps({'@type':'NewsArticle','headline':'Aniego en Lima','datePublished':DOC['published'],'articleBody':'Hay un aniego en la calle Las Flores con agua acumulada.'})+'</script>'
        report={'candidates':[{'decision':'needs_review','url':DOC['url'],'published_at':DOC['published']}], 'errors':[]}
        with tempfile.TemporaryDirectory() as d:
            code=run(report,{},Path(d)/'out',Path(d)/'private',NOW,client=Broken(),fetch=Fetch())
            self.assertEqual(code,2)
            content=(Path(d)/'out/report.json').read_text()
            self.assertNotIn('secret must never appear',content)
            self.assertTrue(json.loads(content)['errors'])

class PublisherFallbackTests(unittest.TestCase):
    def test_official_rss_without_jsonld_reaches_model(self):
        import html
        url=DOC['url']
        class PublicClient(Client):
            ledger={'attempts':[], 'input_tokens':0,'output_tokens':0,'estimated_cost_usd':0}
        class Fetch:
            def check(self,url): pass
            def get(self,location):
                if location.endswith('/feed'):
                    return '<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item><title>Aniego en Lima</title><link>'+url+'</link><pubDate>Thu, 05 Nov 2026 10:00:00 -0500</pubDate><content:encoded>'+html.escape('<p>Hay un aniego en la calle Las Flores con agua acumulada frente al mercado.</p>')+'</content:encoded></item></channel></rss>'
                return '<meta property="og:type" content="article"><meta property="og:title" content="Aniego en Lima">'
        report={'candidates':[{'decision':'needs_review','url':url,'published_at':DOC['published']}], 'errors':[]}
        config={'feeds':[{'url':'https://rpp.pe/feed','kind':'publisher'}]}
        with tempfile.TemporaryDirectory() as d:
            code=run(report,config,Path(d)/'out',Path(d)/'private',NOW,client=PublicClient(),fetch=Fetch())
            self.assertEqual(code,0)
            result=json.loads((Path(d)/'out/report.json').read_text())
            self.assertEqual(result['draft_count'],1)
            self.assertEqual(result['drafts'][0]['place'],'calle Las Flores con agua acumulada frente al mercado')
            self.assertNotIn('frente al mercado',(Path(d)/'out/RESUMEN.md').read_text())
