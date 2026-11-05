import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from generate import instant
from publish import build

NOW = instant('2026-11-05T11:30:00-05:00')
ROOT = Path(__file__).resolve().parents[1]


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.reviews = json.loads((ROOT / 'reviewed.json').read_text())
        self.hosts = json.loads((ROOT / 'config.json').read_text())['allowed_hosts']

    def test_reviewed_incidents_include_one_referential_point_and_district_notices(self):
        feed = build({'candidates': [], 'errors': []}, self.reviews, NOW, self.hosts)
        self.assertEqual(len(feed['reports']), 4)
        self.assertEqual(sum(r['coordinates'] is not None for r in feed['reports']), 1)
        self.assertTrue(all(r['status'] == 'Situación actual sin confirmar' for r in feed['reports']))

    def test_expired_review_does_not_reappear_as_new_publication(self):
        candidate = {'decision': 'needs_review', 'url': self.reviews[0]['sources'][0]['url'],
                     'published_at': '2026-11-06T08:00:00-05:00', 'title': 'Aniegos en Villa El Salvador'}
        feed = build({'candidates': [candidate], 'errors': []}, self.reviews,
                     instant('2026-11-06T10:00:00-05:00'), self.hosts)
        self.assertEqual(feed['reports'], [])

    def test_unreviewed_news_has_no_coordinates_or_inferred_incident_date(self):
        candidate = {'decision': 'needs_review', 'url': 'https://rpp.pe/new-report',
                     'published_at': '2026-11-05T08:00:00-05:00', 'title': 'Aniegos en Villa El Salvador'}
        feed = build({'candidates': [candidate], 'errors': []}, [], NOW, self.hosts)
        self.assertEqual(feed['reports'][0]['kind'], 'publication')
        self.assertIsNone(feed['reports'][0]['coordinates'])
        self.assertEqual(feed['reports'][0]['status'], 'Fecha del hecho por verificar')

    def test_foreign_news_and_untrusted_source_excluded(self):
        candidate = {'decision': 'needs_review', 'url': 'https://rpp.pe/report',
                     'published_at': '2026-11-05T08:00:00-05:00', 'title': 'Inundaciones en Buenos Aires'}
        self.assertEqual(build({'candidates': [candidate], 'errors': []}, [], NOW, self.hosts)['reports'], [])
        self.reviews[0]['sources'][0]['url'] = 'http://127.0.0.1/data'
        with self.assertRaises(ValueError):
            build({'candidates': [], 'errors': []}, self.reviews, NOW, self.hosts)


if __name__ == '__main__':
    unittest.main()
