import json
from pathlib import Path
import sys
import unittest
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from generate import instant
from osm_places import Locator, crossings, distance_m, inside, parse_location
from publish import build

NOW = instant('2026-11-06T11:30:00-05:00')
FIXTURE = json.loads((Path(__file__).parent / 'fixtures' / 'osm_ovalo_26_noviembre.json').read_text())
BYPASS = {'key': 'vmt-bypass', 'district': 'Villa María del Triunfo',
          'location': 'Bypass de Av. Pachacútec con Av. 26 de Noviembre, debajo de la línea férrea',
          'summary': 'Prueba', 'day': '2026-11-05', 'coordinates': None, 'precision': 'Sin punto verificado',
          'sources': [{'name': 'Defensoría', 'url': 'https://www.defensoria.gob.pe/test'}]}


class FakeOSM:
    """Serves the real OSM extract; counts calls; can fail on demand."""
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __call__(self, url, data=None, timeout=45):
        self.calls.append(url)
        if self.fail:
            raise urllib.error.URLError('busy')
        if 'nominatim' in url:
            return json.dumps([FIXTURE['district']]).encode()
        query = data['data']
        if 'around:' in query:
            return json.dumps({'elements': FIXTURE['structures']}).encode()
        return json.dumps({'elements': FIXTURE['roads']}).encode()


def locator(fetch, cache=None):
    return Locator(cache if cache is not None else {}, NOW, fetch=fetch, sleep=lambda s: None)


class ParseTests(unittest.TestCase):
    def test_intersection_with_structure(self):
        self.assertEqual(parse_location(BYPASS['location']),
                         {'kind': 'intersection', 'roads': ['pachacutec', '26 de noviembre'], 'structure': True})
        self.assertEqual(parse_location('Av. El Sol con Av. Separadora Industrial · zona del bypass')['roads'],
                         ['el sol', 'separadora industrial'])

    def test_imprecise_places_get_no_rule(self):
        for text in ('Av. Pachacútec, cuadra 34 · frente a la Red Integrada de Salud', 'Sector 1',
                     'Avenida 26 de Noviembre', 'Pasaje Los Gorriones, primera cuadra de avenida Guardia Civil'):
            self.assertIsNone(parse_location(text), text)

    def test_neighbourhood(self):
        self.assertEqual(parse_location('Sector 1, Grupo 1'), {'kind': 'neighbourhood', 'name': 'sector 1 grupo 1'})
        self.assertEqual(parse_location('Sector 2 Grupo 15 · pasaje interior')['name'], 'sector 2 grupo 15')

    def test_landmark(self):
        self.assertEqual(parse_location('Exteriores del Hospital de la Solidaridad'),
                         {'kind': 'landmark', 'landmark': 'hospital', 'tokens': ['solidaridad']})


class CrossingTests(unittest.TestCase):
    def test_grade_separated_roads_cross_without_shared_node(self):
        a = [[(-12.10, -77.03), (-12.10, -77.01)]]
        b = [[(-12.11, -77.02), (-12.09, -77.02)]]
        (lat, lon), = crossings(a, b)
        self.assertAlmostEqual(lat, -12.10)
        self.assertAlmostEqual(lon, -77.02)

    def test_parallel_roads_do_not_cross(self):
        self.assertEqual(crossings([[(-12.10, -77.03), (-12.10, -77.01)]], [[(-12.11, -77.03), (-12.11, -77.01)]]), [])


class LocatorTests(unittest.TestCase):
    def test_bypass_point_is_under_the_grade_separation(self):
        fetch = FakeOSM()
        found = locator(fetch).locate(BYPASS)
        lon, lat = found['point']
        # Midway between the two OSM bridge decks of Óvalo 26 de Noviembre.
        self.assertLess(distance_m((lat, lon), (-12.18042, -76.94357)), 15)
        self.assertIn('paso a desnivel', found['precision'])
        self.assertIn('OpenStreetMap', found['precision'])
        self.assertTrue(inside(FIXTURE['district']['geojson'], lat, lon))

    def test_plain_intersection_uses_the_junction(self):
        review = dict(BYPASS, location='Av. Pachacútec con Av. 26 de Noviembre')
        lon, lat = locator(FakeOSM()).locate(review)['point']
        self.assertLess(distance_m((lat, lon), (-12.18040, -76.94352)), 40)

    def test_cache_avoids_repeated_queries(self):
        cache, fetch = {}, FakeOSM()
        first = locator(fetch, cache).locate(BYPASS)
        calls = len(fetch.calls)
        self.assertEqual(locator(fetch, json.loads(json.dumps(cache))).locate(BYPASS), first)
        self.assertEqual(len(fetch.calls), calls)

    def test_provider_failure_is_not_cached_as_not_found(self):
        cache = {}
        loc = locator(FakeOSM(fail=True), cache)
        self.assertIsNone(loc.locate(BYPASS))
        self.assertEqual(cache['places'], {})
        self.assertEqual(loc.log[-1]['reason'], 'provider_error')
        self.assertIsNotNone(locator(FakeOSM(), cache).locate(BYPASS))

    def test_unknown_district_or_outside_polygon_gets_no_point(self):
        fetch = FakeOSM()
        self.assertIsNone(locator(fetch).locate(dict(BYPASS, district='Lima Sur · distrito por precisar')))
        self.assertFalse(any('overpass' in c or 'mail.ru' in c for c in fetch.calls))
        shifted = json.loads(json.dumps(FIXTURE['district']))
        shifted['geojson'] = {'type': 'Polygon', 'coordinates': [[[-76.90, -12.10], [-76.89, -12.10],
                                                                  [-76.89, -12.09], [-76.90, -12.10]]]}
        original = FIXTURE['district']
        FIXTURE['district'] = shifted
        try:
            loc = locator(FakeOSM())
            self.assertIsNone(loc.locate(BYPASS))
            self.assertEqual(loc.log[-1]['reason'], 'no_junction_in_district')
        finally:
            FIXTURE['district'] = original

    def test_two_separate_junctions_are_ambiguous(self):
        far = json.loads(json.dumps(FIXTURE['roads']))
        for way in far:
            way['id'] += 10 ** 12
            way['nodes'] = [n + 10 ** 12 for n in way['nodes']]
            way['geometry'] = [{'lat': p['lat'] - 0.01, 'lon': p['lon'] + 0.01} for p in way['geometry']]
        fetch = FakeOSM()
        original = FIXTURE['roads']
        FIXTURE['roads'] = original + far
        try:
            loc = locator(fetch)
            self.assertIsNone(loc.locate(dict(BYPASS, location='Av. Pachacútec con Av. 26 de Noviembre')))
            self.assertEqual(loc.log[-1]['reason'], 'ambiguous_junctions')
        finally:
            FIXTURE['roads'] = original


class NeighbourhoodTests(unittest.TestCase):
    def fetch_with(self, elements):
        fake = FakeOSM()

        def fetch(url, data=None, timeout=45):
            if 'nominatim' in url:
                return fake(url)
            return json.dumps({'elements': elements}).encode()
        return fetch

    def test_single_named_group_inside_district(self):
        group = {'type': 'way', 'id': 1, 'tags': {'place': 'neighbourhood', 'name': 'Sector 1 Grupo 1'},
                 'center': {'lat': -12.1800, 'lon': -76.9430}}
        other = {'type': 'way', 'id': 2, 'tags': {'place': 'neighbourhood', 'name': 'Sector 1 Grupo 11'},
                 'center': {'lat': -12.1810, 'lon': -76.9440}}
        review = dict(BYPASS, location='Sector 1, Grupo 1')
        found = locator(self.fetch_with([group, other])).locate(review)
        self.assertEqual(found['point'], [-76.943, -12.18])
        self.assertIn('zona referencial', found['precision'])

    def test_duplicate_group_names_are_ambiguous(self):
        a = {'type': 'node', 'id': 1, 'tags': {'place': 'neighbourhood', 'name': 'Sector 1 Grupo 1'},
             'lat': -12.1800, 'lon': -76.9430}
        b = dict(a, id=2, lat=-12.1700)
        loc = locator(self.fetch_with([a, b]))
        self.assertIsNone(loc.locate(dict(BYPASS, location='Sector 1, Grupo 1')))
        self.assertEqual(loc.log[-1]['reason'], 'ambiguous_neighbourhood')


class PublishIntegrationTests(unittest.TestCase):
    def test_reviewed_incident_gets_automatic_point_and_evidence(self):
        feed = build({'candidates': [], 'errors': []}, [BYPASS], NOW, ['www.defensoria.gob.pe'],
                     locator(FakeOSM()).locate)
        row = feed['reports'][0]
        self.assertIsNotNone(row['coordinates'])
        self.assertTrue(-78 <= row['coordinates'][0] <= -75 and -14 <= row['coordinates'][1] <= -10)
        self.assertIn('OpenStreetMap', row['precision'])

    def test_without_locator_behaviour_is_unchanged(self):
        feed = build({'candidates': [], 'errors': []}, [BYPASS], NOW, ['www.defensoria.gob.pe'])
        self.assertIsNone(feed['reports'][0]['coordinates'])
        self.assertEqual(feed['reports'][0]['precision'], 'Sin punto verificado')


if __name__ == '__main__':
    unittest.main()
