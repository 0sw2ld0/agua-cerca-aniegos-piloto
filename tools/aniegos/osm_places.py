"""Referential points for reviewed incidents, from OpenStreetMap (ODbL).

Deterministic rules, never free-text guesses:
- Intersection: the review names two roads ("Av. X con Av. Y"). Both must exist in OSM
  with the same name and share nodes forming ONE junction inside the explicit district.
- Grade separation: if the text mentions a bypass/paso a desnivel/puente/túnel, the point
  moves to the short road bridges/tunnels next to that junction (where water collects).
- Residential group: "Sector N, Grupo M" (Villa El Salvador) with exactly one OSM
  place=neighbourhood of that exact name inside the district; the point is its centre.
- Landmark: a hospital, clinic, market or school named in the review, with exactly one
  match inside the district.
Anything else (a single avenue, "cuadra 34", a sector) keeps no point. A provider failure
is never cached as "not found": the next run retries.
"""
import json
import math
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from place_evidence import validate_geocoding

ATTRIBUTION = '© OpenStreetMap contributors (ODbL)'
USER_AGENT = 'AguaCerca-aniegos/1.0 (+https://github.com/0sw2ld0/agua-cerca-aniegos-piloto)'
NOMINATIM = 'https://nominatim.openstreetmap.org/search'
OVERPASS = ('https://overpass-api.de/api/interpreter',
            'https://maps.mail.ru/osm/tools/overpass/api/interpreter')
FOUND_TTL = timedelta(days=30)
NOT_FOUND_TTL = timedelta(hours=24)
DISTRICT_TTL = timedelta(days=90)
JUNCTION_RADIUS_M = 150
STRUCTURE_RADIUS_M = 150
STRUCTURE_MAX_LENGTH_M = 400  # a long viaduct is not "the bypass at this junction"

ROAD = r'(?:av(?:enida)?\.?|jr\.?|jir[oó]n|calle|pasaje|psje\.?|prolongaci[oó]n|carretera|alameda|malec[oó]n|v[ií]a\s+expresa)'
ROAD_PREFIX = re.compile(r'^(?:avenida|av|jiron|jr|calle|pasaje|psje|prolongacion|carretera|alameda|malecon|'
                         r'via expresa|ovalo|plaza|paseo)\s+')
STRUCTURE = re.compile(r'\b(?:by[\s-]?pass|baip[aá]s|paso\s+a\s+desnivel|puente|t[uú]nel|intercambio\s+vial)\b', re.I)
INTERSECTION = re.compile(
    r'^(?:(?:by[\s-]?pass|baip[aá]s|paso\s+a\s+desnivel|puente|t[uú]nel|[oó]valo|intercambio\s+vial|cruce)'
    r'\s+(?:de\s+(?:la\s+)?|entre\s+)?)?(?:cruce\s+de\s+)?'
    r'(?P<a>' + ROAD + r'\s+.+?)\s+(?:con|y|esquina\s+con)\s+(?:la\s+)?(?P<b>' + ROAD + r'\s+.+)$', re.I)
LANDMARKS = {
    'hospital': '["amenity"="hospital"]', 'clinica': '["amenity"~"^(clinic|hospital)$"]',
    'mercado': '["amenity"="marketplace"]', 'colegio': '["amenity"="school"]',
}
LANDMARK = re.compile(r'\b((?i:hospital|cl[ií]nica|mercado|colegio))\s+((?:de\s+(?:la\s+|los\s+|las\s+)?)?[A-ZÁÉÍÓÚÑ0-9][^,·]*)')
SECTOR = re.compile(r'(?i)^\s*sector\s+(\d{1,2})\s*,?\s*grupo\s+(\d{1,2})\s*(?:$|[,·])')
STOPWORDS = {'de', 'del', 'la', 'las', 'los', 'el', 'y'}


def norm(value):
    value = ''.join(c for c in unicodedata.normalize('NFD', value.lower()) if unicodedata.category(c) != 'Mn')
    return re.sub(r'[^a-z0-9]+', ' ', value).strip()


def road_core(name):
    value = norm(name)
    while True:
        stripped = ROAD_PREFIX.sub('', value)
        if stripped == value:
            return value
        value = stripped


def parse_location(location):
    """Return {'kind': 'intersection'|'landmark', ...} or None when the text is not precise enough."""
    main = re.split(r'\s+·\s+|,', location, maxsplit=1)[0].strip()
    structure = bool(STRUCTURE.search(location))
    match = INTERSECTION.match(main)
    if match:
        a, b = road_core(match['a']), road_core(match['b'])
        if a and b and a != b and not re.search(r'\bcuadra\b', a + ' ' + b):
            return {'kind': 'intersection', 'roads': [a, b], 'structure': structure}
    sector = SECTOR.match(location)
    if sector:
        return {'kind': 'neighbourhood', 'name': 'sector %s grupo %s' % (sector[1], sector[2])}
    found = LANDMARK.search(location)
    if found:
        kind = norm(found[1])
        tokens = [t for t in norm(found[2]).split() if t not in STOPWORDS]
        if tokens:
            return {'kind': 'landmark', 'landmark': kind, 'tokens': tokens}
    return None


def distance_m(a, b):
    """a, b = (lat, lon)."""
    dy = (a[0] - b[0]) * 111320
    dx = (a[1] - b[1]) * 111320 * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot(dx, dy)


def centroid(points):
    return (sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points))


def clusters(points, radius):
    groups = []
    for point in points:
        for group in groups:
            if distance_m(point, centroid(group)) <= radius:
                group.append(point)
                break
        else:
            groups.append([point])
    return groups


def inside(polygon_geojson, lat, lon):
    """Ray casting over (Multi)Polygon GeoJSON; holes excluded."""
    kind, coords = polygon_geojson.get('type'), polygon_geojson.get('coordinates') or []
    polygons = [coords] if kind == 'Polygon' else coords if kind == 'MultiPolygon' else []

    def ring_has(ring):
        hit = False
        for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
            if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
                hit = not hit
        return hit
    return any(rings and ring_has(rings[0]) and not any(ring_has(h) for h in rings[1:]) for rings in polygons)


def token_regex(cores):
    accents = {'a': '[aá]', 'e': '[eé]', 'i': '[ií]', 'o': '[oó]', 'u': '[uúü]', 'n': '[nñ]'}
    parts = []
    for core in cores:
        words = sorted(core.split(), key=len, reverse=True)
        word = words[0] if len(words[0]) >= 4 else core
        parts.append(''.join(accents.get(c, re.escape(c)) for c in word))
    return '|'.join(parts)


class ProviderError(Exception):
    pass


def http_fetch(url, data=None, timeout=45):
    body = urllib.parse.urlencode(data).encode() if data else None
    request = urllib.request.Request(url, data=body, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(5_000_000)


class Locator:
    def __init__(self, cache, now, fetch=http_fetch, sleep=time.sleep, max_lookups=6, deadline_s=150):
        if cache.get('schema_version') != 1:
            cache.clear()  # unknown format: start over in place
        self.cache = cache
        self.cache['schema_version'] = 1
        self.cache['attribution'] = ATTRIBUTION
        self.cache.setdefault('districts', {})
        self.cache.setdefault('places', {})
        self.now, self.fetch, self.sleep = now, fetch, sleep
        self.lookups_left = max_lookups
        self.deadline = time.monotonic() + deadline_s
        self.log = []

    # ---- providers -------------------------------------------------------
    def overpass(self, query):
        last = None
        for attempt in range(2):
            for endpoint in OVERPASS:
                if time.monotonic() > self.deadline:
                    raise ProviderError('deadline')
                try:
                    data = json.loads(self.fetch(endpoint, {'data': query}))
                    if 'elements' not in data:
                        raise ValueError('no elements')
                    return data['elements']
                except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
                    last = error
            self.sleep(10)
        raise ProviderError('overpass: %s' % last)

    def district(self, name):
        key = norm(name)
        if not key or '·' in name or 'precisar' in key or 'lima metropolitana' in key:
            return None
        cached = self.cache['districts'].get(key)
        if cached and self.now - datetime.fromisoformat(cached['checked_at']) < DISTRICT_TTL:
            return cached if cached.get('polygon') else None
        query = {'q': name + ', Lima, Perú', 'format': 'jsonv2', 'polygon_geojson': 1,
                 'polygon_threshold': 0.0003, 'limit': 5, 'countrycodes': 'pe'}
        try:
            results = json.loads(self.fetch(NOMINATIM + '?' + urllib.parse.urlencode(query)))
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
            raise ProviderError('nominatim: %s' % error)
        finally:
            self.sleep(1.1)  # Nominatim usage policy: at most one request per second.
        match = next((r for r in results if r.get('category') == 'boundary' and r.get('type') == 'administrative'
                      and norm(r.get('name', '')) == key and r.get('geojson', {}).get('type') in ('Polygon', 'MultiPolygon')), None)
        entry = {'checked_at': self.now.isoformat()}
        if match:
            south, north, west, east = (float(v) for v in match['boundingbox'])
            entry.update({'name': match['name'], 'osm': '%s/%s' % (match['osm_type'], match['osm_id']),
                          'bbox': [south, west, north, east], 'polygon': match['geojson']})
        self.cache['districts'][key] = entry
        return entry if match else None

    # ---- rules -----------------------------------------------------------
    def intersection(self, place, district):
        s, w, n, e = district['bbox']
        elements = self.overpass('[out:json][timeout:25][bbox:%f,%f,%f,%f];way["highway"]["name"~"%s",i];out body geom;'
                                 % (s, w, n, e, token_regex(place['roads'])))
        coords, members = {}, ({}, {})
        for way in elements:
            core = road_core(way.get('tags', {}).get('name', ''))
            for index, road in enumerate(place['roads']):
                if core == road:
                    for node, point in zip(way.get('nodes', []), way.get('geometry', [])):
                        coords[node] = (point['lat'], point['lon'])
                        members[index].setdefault(node, set()).add(way['id'])
        shared = [coords[n] for n in set(members[0]) & set(members[1])
                  if inside(district['polygon'], *coords[n])]
        if not shared:
            return None, 'no_junction_in_district' if members[0] and members[1] else 'road_not_found'
        groups = clusters(shared, JUNCTION_RADIUS_M)
        if len(groups) != 1:
            return None, 'ambiguous_junctions'
        junction = centroid(groups[0])
        precision = 'Cruce de vías según OpenStreetMap · punto referencial (±50 m)'
        evidence = 'OSM: cruce de %s y %s' % tuple(place['roads'])
        if place['structure']:
            point = self.structure(junction)
            if point:
                junction = point
                precision = 'Debajo del paso a desnivel según OpenStreetMap · punto referencial (±50 m)'
                evidence += '; estructura vial a desnivel junto al cruce'
            else:
                precision += ' · no se ubicó la estructura del bypass'
        if not inside(district['polygon'], *junction):
            return None, 'outside_district'
        candidate = {'district': district['name'], 'roads': place['roads'], 'precision': 'intersection',
                     'point': [round(junction[1], 6), round(junction[0], 6)]}
        gate = validate_geocoding({'district': district['name'], 'roads': place['roads']}, [candidate])
        if gate['point'] is None:
            return None, gate['reason']
        return {'point': gate['point'], 'precision': precision, 'evidence': evidence}, 'verified'

    def structure(self, junction):
        lat, lon = junction
        elements = self.overpass('[out:json][timeout:25];(way["highway"]["bridge"](around:%d,%f,%f);'
                                 'way["highway"]["tunnel"](around:%d,%f,%f););out body geom;'
                                 % (STRUCTURE_RADIUS_M, lat, lon, STRUCTURE_RADIUS_M, lat, lon))
        mids = []
        for way in elements:
            tags = way.get('tags', {})
            if tags.get('highway') in ('footway', 'steps', 'path', 'cycleway', 'pedestrian'):
                continue
            if tags.get('bridge', 'no') == 'no' and tags.get('tunnel', 'no') == 'no':
                continue
            points = [(p['lat'], p['lon']) for p in way.get('geometry', [])]
            if len(points) < 2:
                continue
            length = sum(distance_m(a, b) for a, b in zip(points, points[1:]))
            mid = centroid(points)
            if length <= STRUCTURE_MAX_LENGTH_M and distance_m(mid, junction) <= STRUCTURE_RADIUS_M:
                mids.append(mid)
        groups = clusters(mids, STRUCTURE_RADIUS_M)
        return centroid(groups[0]) if len(groups) == 1 else None

    def landmark(self, place, district):
        s, w, n, e = district['bbox']
        elements = self.overpass('[out:json][timeout:25][bbox:%f,%f,%f,%f];nwr%s["name"~"%s",i];out center tags;'
                                 % (s, w, n, e, LANDMARKS[place['landmark']], token_regex(place['tokens'][:1])))
        points = []
        for element in elements:
            name_tokens = set(norm(element.get('tags', {}).get('name', '')).split())
            if not set(place['tokens']) <= name_tokens:
                continue
            where = element.get('center') or {'lat': element.get('lat'), 'lon': element.get('lon')}
            if where.get('lat') is not None and inside(district['polygon'], where['lat'], where['lon']):
                points.append((where['lat'], where['lon']))
        groups = clusters(points, JUNCTION_RADIUS_M)
        if len(groups) != 1:
            return None, 'ambiguous_landmark' if groups else 'landmark_not_found'
        lat, lon = centroid(groups[0])
        return {'point': [round(lon, 6), round(lat, 6)],
                'precision': 'Referencia: %s según OpenStreetMap, no el lugar exacto del agua' % place['landmark'],
                'evidence': 'OSM: %s %s en %s' % (place['landmark'], ' '.join(place['tokens']), district['name'])}, 'verified'

    def neighbourhood(self, place, district):
        s, w, n, e = district['bbox']
        elements = self.overpass('[out:json][timeout:25][bbox:%f,%f,%f,%f];nwr["place"~"^(neighbourhood|quarter)$"]'
                                 '["name"~"grupo",i];out center tags;' % (s, w, n, e))
        points = []
        for element in elements:
            if norm(element.get('tags', {}).get('name', '')) != place['name']:
                continue
            where = element.get('center') or {'lat': element.get('lat'), 'lon': element.get('lon')}
            if where.get('lat') is not None and inside(district['polygon'], where['lat'], where['lon']):
                points.append((where['lat'], where['lon']))
        if len(points) != 1:
            return None, 'ambiguous_neighbourhood' if points else 'neighbourhood_not_found'
        lat, lon = points[0]
        return {'point': [round(lon, 6), round(lat, 6)],
                'precision': 'Centro del grupo residencial según OpenStreetMap · zona referencial (±150 m), no la calle exacta',
                'evidence': 'OSM: %s en %s' % (place['name'], district['name'])}, 'verified'

    # ---- entry point -----------------------------------------------------
    def locate(self, review):
        """Return {'point': [lon, lat], 'precision', 'evidence'} or None."""
        place = parse_location(review.get('location', ''))
        if not place:
            return None
        key = norm(review.get('district', '')) + '|' + norm(review['location'])
        cached = self.cache['places'].get(key)
        if cached:
            age = self.now - datetime.fromisoformat(cached['checked_at'])
            if age < (FOUND_TTL if cached.get('point') else NOT_FOUND_TTL):
                return {k: cached[k] for k in ('point', 'precision', 'evidence')} if cached.get('point') else None
        if self.lookups_left <= 0:
            self.log.append({'key': key, 'reason': 'lookup_budget'})
            return None
        self.lookups_left -= 1
        try:
            district = self.district(review.get('district', ''))
            if not district:
                result, reason = None, 'district_unknown'
            elif place['kind'] == 'intersection':
                result, reason = self.intersection(place, district)
            elif place['kind'] == 'neighbourhood':
                result, reason = self.neighbourhood(place, district)
            else:
                result, reason = self.landmark(place, district)
        except ProviderError as error:
            self.log.append({'key': key, 'reason': 'provider_error', 'detail': str(error)[:200]})
            return None  # Not cached: a failure is not evidence that the place does not exist.
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as error:
            # Malformed provider data must not block the feed; the review keeps no point.
            self.log.append({'key': key, 'reason': 'locator_error', 'detail': repr(error)[:200]})
            return None
        entry = {'checked_at': self.now.isoformat(), 'reason': reason}
        if result:
            entry.update(result)
        self.cache['places'][key] = entry
        self.log.append({'key': key, 'reason': reason})
        return result
