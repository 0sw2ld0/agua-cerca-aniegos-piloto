from datetime import datetime
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from generate import PERU
import gpt_shadow
from gpt_shadow import run

NOW = datetime(2026, 11, 6, 13, 0, tzinfo=PERU)
URL = 'https://rpp.pe/nota-1'
TEXT = ('Este jueves, en el Sector uno, grupo uno de Villa El Salvador el desagüe colapsó y el agua ingresó '
        'a las viviendas. ' * 3)
PAGE = '<script type="application/ld+json">%s</script>' % json.dumps(
    {'@type': 'NewsArticle', 'headline': 'Aniego en VES', 'datePublished': '2026-11-06T08:00:00-05:00',
     'articleBody': TEXT})
REPORT = {'candidates': [{'decision': 'needs_review', 'url': URL, 'published_at': '2026-11-06T08:00:00-05:00',
                          'title': 'Aniego en VES'}]}
EXTRACTION = {'incidents': [{
    'district': 'Villa El Salvador', 'district_quote': 'Villa El Salvador', 'location': 'Sector 1, Grupo 1',
    'location_quote': 'Sector uno, grupo uno', 'timing': 'current', 'water_reported': 'yes',
    'date_relation': 'same_day_as_publication', 'explicit_date': '', 'date_quote': 'Este jueves',
    'summary': 'Colapso del desagüe con ingreso de agua a viviendas.'}]}


class FakeFetch:
    def get(self, url):
        return PAGE


class FakeClient:
    model = 'gpt-6-luna'

    def __init__(self, fail=False):
        self.calls, self.fail = 0, fail
        self.ledger = {'attempts': []}

    def ask(self, payload):
        self.calls += 1
        if self.fail:
            self.ledger['attempts'].append({'status': 'http_error', 'reserved_usd': 0.003})
            raise RuntimeError('OpenAI returned HTTP 500; not treated as an answer.')
        self.ledger['attempts'].append({'status': 'success', 'cost_usd': 0.0007, 'model': 'gpt-6-luna',
                                        'input_tokens': 1800, 'output_tokens': 1000})
        return {'status': 'completed', 'output': [{'type': 'message', 'content': [
            {'type': 'output_text', 'text': json.dumps(EXTRACTION)}]}]}, False


class ShadowTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.paths = dict(state_path=self.dir / 'state.json', shadow_path=self.dir / 'sombra.json',
                          places_path=None, private=self.dir / 'private', output=self.dir / 'out')

    def go(self, client):
        return run(REPORT, [], NOW, fetch=FakeFetch(), client=client, **self.paths)

    def test_detects_and_reuses_without_second_call(self):
        client = FakeClient()
        self.go(client)
        self.go(client)
        self.assertEqual(client.calls, 1)
        shadow = json.loads(self.paths['shadow_path'].read_text())
        self.assertEqual(shadow['mode'], 'shadow_not_published')
        self.assertEqual(shadow['current'][0]['location'], 'Sector 1, Grupo 1')
        state = json.loads(self.paths['state_path'].read_text())
        self.assertNotIn(TEXT[:40], json.dumps(state))  # no article bodies in the repository

    def test_failure_is_recorded_and_backed_off(self):
        client = FakeClient(fail=True)
        self.go(client)
        self.go(client)
        self.assertEqual(client.calls, 1)  # second run waits for the backoff
        state = json.loads(self.paths['state_path'].read_text())
        self.assertEqual(state['attempts'][0]['status'], 'error')
        self.assertEqual(state['attempts'][0]['cost_usd'], 0.003)
        self.assertEqual(json.loads(self.paths['shadow_path'].read_text())['current'], [])

    def test_budget_limits_stop_calls(self):
        state = {'schema_version': 1, 'previous_estimated_cost_usd': 0.499, 'attempts': [], 'articles': {}}
        self.paths['state_path'].write_text(json.dumps(state))
        client = FakeClient()
        self.go(client)
        self.assertEqual(client.calls, 0)
        detail = json.loads((self.paths['output'] / 'detalle.json').read_text())
        self.assertEqual(detail['deferred'][0]['reason'], 'total_limit')

    def test_without_key_nothing_happens(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(run(REPORT, [], NOW, **self.paths), 0)
        self.assertFalse(self.paths['state_path'].exists())

    def test_invalid_model_fields_are_not_stored(self):
        with self.assertRaises(ValueError):
            gpt_shadow.clean({'incidents': [dict(EXTRACTION['incidents'][0], timing='maybe')]})


if __name__ == '__main__':
    unittest.main()
