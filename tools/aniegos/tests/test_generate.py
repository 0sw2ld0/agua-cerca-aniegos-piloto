import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gzip
import tempfile
from generate import (MAX_BYTES, Fetcher, approved_features, article, candidate, decoded_body, discover,
                      instant, recent_publication, remember_feed_entries, run)

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



class EncodingAndFeedMemoryTests(unittest.TestCase):
    def test_unrequested_gzip_is_decoded(self):
        html = '<meta property="og:type" content="article">'.encode()
        self.assertEqual(decoded_body(gzip.compress(html), 'gzip'), html)
        self.assertEqual(decoded_body(html, None), html)
        with self.assertRaises(ValueError):
            decoded_body(html, 'br')

    def test_gzip_bomb_is_rejected(self):
        with self.assertRaises(ValueError):
            decoded_body(gzip.compress(b'a' * (MAX_BYTES + 10)), 'gzip')

    def test_publisher_timestamp_is_remembered_and_expires(self):
        cache = {}
        entries = [{'url': URL, 'title': 'Aniego', 'published_at': '2026-11-05T08:00:00-05:00', 'kind': 'publisher'},
                   {'url': 'https://news.google.com/x', 'title': 'Aniego',
                    'published_at': '2026-11-05T08:00:00-05:00', 'kind': 'discovery_only'}]
        remember_feed_entries(cache, entries, NOW)
        self.assertEqual(list(cache), [URL])
        self.assertNotIn('_body', cache[URL])
        remember_feed_entries(cache, [], instant('2026-11-07T09:00:00-05:00'))
        self.assertEqual(cache, {})

    def test_article_that_left_the_rss_is_still_dated_by_the_remembered_entry(self):
        html = '<meta property="og:type" content="article"><meta property="og:title" content="Aniego en VES">'
        remembered = {URL: {'url': URL, 'title': 'Aniego en VES', 'published_at': '2026-11-05T08:00:00-05:00',
                            'kind': 'publisher', 'first_seen': '2026-11-05T08:10:00-05:00'}}

        class Offline(Fetcher):
            def get(self, url):
                if url.endswith('/feed/'):
                    return '<rss><channel></channel></rss>'
                return html
        config = {'allowed_hosts': ['rpp.pe'], 'feeds': [{'url': 'https://rpp.pe/feed/', 'kind': 'publisher'}],
                  'seed_urls': []}
        import generate
        original = generate.Fetcher
        generate.Fetcher = Offline
        try:
            with tempfile.TemporaryDirectory() as folder:
                run(config, Path(folder), NOW, [], remembered)
                report = json.loads((Path(folder) / 'report.json').read_text())
        finally:
            generate.Fetcher = original
        found = [c for c in report['candidates'] if c['url'] == URL][0]
        self.assertEqual(found['decision'], 'needs_review')
        self.assertEqual(found['discovered_via'], 'publisher_feed_cache')


if __name__ == '__main__':
    unittest.main()
