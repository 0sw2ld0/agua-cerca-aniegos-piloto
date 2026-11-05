import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from generate import instant
from jev_client import JevClient
from jev_questions import address_spans, batch_payload, date_spans, route, verdict

PAYLOAD = {'model': 'jev-latest', 'state': 'Synthetic test', 'questions': {}}


class JevTests(unittest.TestCase):
    def test_no_network_without_explicit_live_flag(self):
        with tempfile.TemporaryDirectory() as directory, patch('urllib.request.build_opener') as opener:
            with self.assertRaises(RuntimeError):
                JevClient(directory).ask(PAYLOAD)
            opener.assert_not_called()

    def test_cache_avoids_second_call_and_keeps_auth_out_of_files(self):
        result = {'model': 'test-model', 'answers': {}, 'usage': {'input_tokens': 10, 'output_tokens': 2}}
        with tempfile.TemporaryDirectory() as directory, patch('jev_client.api_key', return_value='test-secret-only'), patch('urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = io.BytesIO(json.dumps(result).encode())
            client = JevClient(directory, allow_network=True)
            self.assertFalse(client.ask(PAYLOAD)[1])
            self.assertTrue(client.ask(PAYLOAD)[1])
            self.assertEqual(opener.return_value.open.call_count, 1)
            self.assertEqual(client.ledger['input_tokens'], 10)
            for file in Path(directory).rglob('*.json'):
                self.assertNotIn('test-secret-only', file.read_text())

    def test_budget_refuses_call_before_reading_key(self):
        with tempfile.TemporaryDirectory() as directory, patch('jev_client.api_key') as key:
            with self.assertRaises(RuntimeError):
                JevClient(directory, budget=0.003, allow_network=True).ask(PAYLOAD)
            key.assert_not_called()

    def test_success_releases_reservation_but_unknown_failure_keeps_it(self):
        result = {'model': 'test', 'answers': {}, 'usage': {'input_tokens': 10, 'output_tokens': 0}}
        with tempfile.TemporaryDirectory() as directory, patch('jev_client.api_key', return_value='test'), patch('urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = [io.BytesIO(json.dumps(result).encode()), io.BytesIO(json.dumps(result).encode())]
            client = JevClient(directory, budget=0.0041, allow_network=True)
            client.ask(PAYLOAD)
            client.ask(dict(PAYLOAD, state='Second synthetic state'))
            self.assertEqual(opener.return_value.open.call_count, 2)
        with tempfile.TemporaryDirectory() as directory, patch('jev_client.api_key', return_value='test'), patch('urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = TimeoutError()
            client = JevClient(directory, budget=0.0041, allow_network=True)
            with self.assertRaises(RuntimeError):
                client.ask(PAYLOAD)
            with self.assertRaises(RuntimeError):
                client.ask(dict(PAYLOAD, state='Second synthetic state'))
            self.assertEqual(opener.return_value.open.call_count, 1)

    def test_http_failure_is_error_not_no_incident(self):
        with tempfile.TemporaryDirectory() as directory, patch('jev_client.api_key', return_value='test'), patch('urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = urllib.error.HTTPError('https://api.typesafe.ai', 429, 'rate limit', {}, None)
            client = JevClient(directory, allow_network=True)
            with self.assertRaises(RuntimeError):
                client.ask(PAYLOAD)
            self.assertEqual(client.ledger['attempts'][0]['status'], 'http_error')
            self.assertEqual(client.ledger['input_tokens'], 0)

    def test_answer_labels_not_visible_in_state_or_questions(self):
        payload = batch_payload({'title': 'Document'}, {'EXPECTED_ATTENDED': {'text': 'Synthetic fragment'}})
        self.assertNotIn('EXPECTED_ATTENDED', json.dumps(payload))
        self.assertIn('passage_0', payload['state']['fragments'])

    def test_invalid_probabilities_do_not_become_false(self):
        with self.assertRaises(ValueError):
            verdict({}, 'passage_0')

    def test_uncertain_or_expired_is_not_a_live_alert(self):
        values = {name: {'value': False} for name in ['historical', 'observed', 'attended', 'water_present', 'removal_work']}
        values['historical']['value'] = None
        self.assertEqual(route(values, '2026-11-05', instant('2026-11-05T17:00:00Z')), 'review')
        self.assertEqual(route(values, '2023-11-05', instant('2026-11-05T17:00:00Z')), 'exclude_expired_or_future')
        values['historical']['value'] = False
        values['attended']['value'] = True
        self.assertEqual(route(values, '2026-11-05', instant('2026-11-05T17:00:00Z')), 'attended_according_to_source')

    def test_extracted_values_are_literal_and_dates_use_publication_reference(self):
        text = 'Se reportó un aniego en el Sector 1, Grupo 1.'
        spans = address_spans(text)
        self.assertTrue(spans)
        self.assertTrue(all(span in text for span in spans))
        self.assertEqual(address_spans('Hay un aniego en una calle sin identificar.'), [])
        self.assertEqual(date_spans('Ocurrió ayer.', '2026-11-05')[0]['date'], '2026-11-04')
        self.assertEqual(date_spans('32 de febrero de 2026', '2026-11-05'), [])


if __name__ == '__main__':
    unittest.main()
