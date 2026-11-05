import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from generate import (Fetcher, approved_features, article, candidate, discover,
                      instant, recent_publication)

NOW = instant('2026-11-05T09:45:00-05:00')
URL = 'https://rpp.pe/noticia-prueba'


def page(**overrides):
    data = {'@type': 'NewsArticle', 'headline': 'Aniego en Villa El Salvador',
            'datePublished': '2026-11-04T23:52:07-05:00',
            'articleBody': 'Sector 1, Grupo 1. Antecedente del 16 de agosto de 2026.'}
    data.update(overrides)
    return '<script type="application/ld+json">' + json.dumps(data) + '</script>'


class PilotTests(unittest.TestCase):
    def test_48h_boundary_and_future(self):
        self.assertTrue(recent_publication('2026-11-03T09:45:00-05:00', NOW))
        self.assertFalse(recent_publication('2026-11-03T09:44:59-05:00', NOW))
        self.assertFalse(recent_publication('2026-11-05T09:45:01-05:00', NOW))

    def test_dates_require_timezone(self):
        self.assertFalse(recent_publication('2026-11-04', NOW))
        self.assertFalse(recent_publication('not a date', NOW))

    def test_updated_old_article_stays_old(self):
        result = candidate(page(datePublished='2023-11-04T12:00:00-05:00',
                                dateModified=NOW.isoformat()), URL, NOW, 'seed')
        self.assertEqual(result['decision'], 'rejected')

    def test_never_infers_event_date_status_or_geometry(self):
        result = candidate(page(), URL, NOW, 'seed')
        self.assertIsNone(result['incident_at'])
        self.assertIsNone(result['geometry'])
        self.assertEqual(result['current_status'], 'unconfirmed')
        self.assertNotIn('_body', result)
        self.assertIn('Sector 1, Grupo 1', result['location_hints'])

    def test_unrelated_article_rejected(self):
        self.assertEqual(candidate(page(headline='Resultados de fútbol'), URL, NOW, 'seed')['decision'], 'rejected')

    def test_graph_and_organization_not_incident(self):
        document = {'@graph': [{'@type': 'Organization', 'address': {'latitude': -12}},
                               {'@type': ['Article', 'NewsArticle'], 'headline': 'Aniego',
                                'datePublished': NOW.isoformat()}]}
        result = article('<script type="application/ld+json">' + json.dumps(document) + '</script>', URL)
        self.assertEqual(result['title'], 'Aniego')
        self.assertNotIn('geometry', result)

    def test_only_matching_publisher_feed_can_supply_missing_date(self):
        html = '<meta property="og:type" content="article"><meta property="og:title" content="Aniegos en Lima">'
        entry = {'kind': 'official', 'url': URL, 'published_at': NOW.isoformat()}
        self.assertEqual(article(html, URL, entry)['published_at'], NOW.isoformat())
        self.assertIsNone(article(html, URL, {**entry, 'kind': 'discovery_only'}))
        self.assertIsNone(article(html, URL, {**entry, 'url': 'https://other.pe'}))

    def test_feed_is_discovery_not_an_event(self):
        xml = '''<rss><channel><item><title>Aniegos en Lima</title>
        <link>https://news.google.com/article/123</link>
        <pubDate>Wed, 04 Nov 2026 22:00:00 GMT</pubDate></item>
        <item><title>Aniego antiguo</title><pubDate>Tue, 04 Nov 2025 22:00:00 GMT</pubDate></item>
        </channel></rss>'''
        found = discover(xml, 'discovery_only', NOW)
        self.assertEqual(len(found), 1)
        self.assertNotIn('incident_at', found[0])

    def test_ssrf_redirect_allowlist(self):
        fetcher = Fetcher(['rpp.pe'])
        for url in ('http://rpp.pe/', 'https://127.0.0.1/', 'https://rpp.pe.evil.com/',
                    'file:///etc/passwd', 'https://rpp.pe:123/', 'https://user@rpp.pe/'):
            with self.assertRaises(ValueError):
                fetcher.check(url)
        fetcher.check(URL)

    def review(self, **changes):
        row = {'event_id': 'synthetic-case-not-a-real-report', 'source_url': URL,
               'incident_at': '2026-11-04T12:00:00-05:00', 'reviewed_at': NOW.isoformat(),
               'reviewed_by': 'unit-test', 'location_label': 'Synthetic test location',
               'location_precision': 'approximate', 'coordinates': [-77.0, -12.0],
               'status': 'unconfirmed'}
        row.update(changes)
        return row

    def test_unreviewed_candidates_never_publish(self):
        candidates = [candidate(page(), URL, NOW, 'seed')]
        self.assertEqual(approved_features([], candidates, NOW), ([], []))

    def test_review_expired_even_when_article_fresh(self):
        candidates = [candidate(page(), URL, NOW, 'seed')]
        features, rejected = approved_features([self.review(incident_at='2023-11-04T12:00:00-05:00')], candidates, NOW)
        self.assertEqual(features, [])
        self.assertEqual(len(rejected), 1)

    def test_review_coordinates_and_source_validation(self):
        candidates = [candidate(page(), URL, NOW, 'seed')]
        for changes in ({'coordinates': [0, 0]}, {'coordinates': [True, -12]},
                        {'source_url': 'https://unknown.pe'}, {'reviewed_by': ''},
                        {'status': 'active'}, {'incident_at': NOW.replace(year=2027).isoformat()}):
            features, _ = approved_features([self.review(**changes)], candidates, NOW)
            self.assertEqual(features, [])

    def test_review_deduplicates_and_exports_valid_geojson(self):
        candidates = [candidate(page(), URL, NOW, 'seed')]
        features, rejected = approved_features([self.review(), self.review()], candidates, NOW)
        self.assertEqual(len(features), 1)
        self.assertEqual(len(rejected), 1)
        self.assertEqual(features[0]['geometry']['type'], 'Point')
        self.assertEqual(features[0]['properties']['status'], 'unconfirmed')


if __name__ == '__main__':
    unittest.main()
