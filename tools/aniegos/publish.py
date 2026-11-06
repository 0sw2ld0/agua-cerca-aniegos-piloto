#!/usr/bin/env python3
"""Build the app feed: reviewed incidents plus clearly distinguished publications."""
import argparse
from datetime import datetime, timedelta
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit
from generate import PERU, instant, recent_publication, Fetcher, normalized


def stable_id(key):
    return -int(hashlib.sha256(key.encode()).hexdigest()[:13], 16)


def canonical(url):
    parts = urlsplit(url)
    return parts.scheme + '://' + parts.netloc.lower() + parts.path.rstrip('/')


def place_key(district, location):
    """Same place, written differently: 'Av. El Sol con Av. Separadora Industrial' == 'Av. Sol con Av. ...'."""
    head = re.split(r'\s+·\s+|,', location, maxsplit=1)[0]
    words = [w for w in normalized(head).replace('.', ' ').split() if w not in PLACE_FILLER]
    return normalized(district) + '|' + ' '.join(sorted(words))


PLACE_FILLER = {'av', 'avenida', 'jr', 'jiron', 'calle', 'bypass', 'baipas', 'de', 'del', 'la', 'el', 'con', 'y', 'frente', 'al'}


def merge_official(reviews, official):
    """Automatic COEN-INDECI incidents, skipped when a manual review covers the same place and day."""
    def marks(r):
        out = {(r['day'], place_key(r['district'], r['location']))}
        if r.get('seccion') == 'vias' or 'otras vías' in r['location']:
            out.add((r['day'], normalized(r['district']) + '|#vias'))  # one list of roads per district and day
        return out
    taken = set().union(*(marks(r) for r in reviews)) if reviews else set()
    merged, keys = list(reviews), {r['key'] for r in reviews}
    for item in official:
        mine = marks(item)
        if mine & taken or item['key'] in keys:
            continue
        taken |= mine; keys.add(item['key'])
        merged.append(item)
    return merged


def build(report, reviews, now, allowed, locate=None):
    """locate(review) -> {'point', 'precision', 'evidence'} | None, for reviews without a point."""
    guard = Fetcher(allowed)
    rows, consumed, ids = [], set(), set()
    for review in reviews:
        for source in review['sources']:
            guard.check(source['url'])
            consumed.add(canonical(source['url']))  # Never renew an expired reviewed event.
        start = datetime.strptime(review['day'], '%Y-%m-%d').replace(tzinfo=PERU)
        expires = start + timedelta(hours=48)
        if not start <= now < expires:
            continue
        coords, precision = review.get('coordinates'), review['precision']
        evidence = review.get('location_evidence')
        if coords is None and locate:
            found = locate(review)
            if found:
                coords, precision, evidence = found['point'], found['precision'], found['evidence']
        if coords is not None:
            if (len(coords) != 2 or not all(type(c) in (float, int) for c in coords)
                    or not -78 <= coords[0] <= -75 or not -14 <= coords[1] <= -10
                    or not evidence):
                raise ValueError('Unverified geometry')
        identifier = stable_id(review['key'])
        if identifier in ids:
            raise ValueError('Duplicate reviewed incident')
        ids.add(identifier)
        rows.append({'id': identifier, 'kind': 'incident', 'district': review['district'],
                     'location': review['location'], 'summary': review['summary'],
                     'date': review['day'], 'expires_at': expires.isoformat(),
                     'status': 'Situación actual sin confirmar', 'coordinates': coords,
                     'precision': precision, 'sources': review['sources']})
    for item in report['candidates']:
        if item.get('decision') != 'needs_review' or canonical(item['url']) in consumed:
            continue
        guard.check(item['url'])
        stamp = instant(item.get('published_at'))
        if not stamp or not recent_publication(item['published_at'], now):
            continue
        title = normalized(item['title'])
        # Only title geography, not mentions of historical events buried in article bodies.
        districts = [name for name in ('Villa El Salvador', 'Villa María del Triunfo', 'Chorrillos',
                                      'San Juan de Miraflores') if normalized(name) in title]
        if not districts and 'lima' not in title:
            continue
        identifier = stable_id(canonical(item['url']))
        if identifier in ids:
            continue
        ids.add(identifier)
        rows.append({'id': identifier, 'kind': 'publication',
                     'district': ' / '.join(districts) or 'Lima Metropolitana',
                     'location': 'Publicación reciente · por verificar',
                     'summary': item['title'][:240],
                     'date': stamp.astimezone(PERU).strftime('%Y-%m-%d'),
                     'expires_at': (stamp + timedelta(hours=48)).isoformat(),
                     'status': 'Fecha del hecho por verificar', 'coordinates': None,
                     'precision': 'Sin punto verificado',
                     'sources': [{'name': urlsplit(item['url']).hostname, 'url': item['url']}]})
    return {'schema_version': 1, 'generated_at': now.isoformat(), 'coverage': 'partial',
            'source_errors': len(report['errors']), 'reports': rows}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--reviews', type=Path, default=Path(__file__).with_name('reviewed.json'))
    parser.add_argument('--official', type=Path, help='Automatic official incidents (data/indeci.json)')
    parser.add_argument('--places-cache', type=Path,
                        help='OpenStreetMap lookups (JSON). Without it, no automatic points are added.')
    args = parser.parse_args()
    config = json.loads(Path(__file__).with_name('config.json').read_text())
    now = datetime.now(PERU)
    locator = None
    if args.places_cache:
        from osm_places import Locator
        cache = json.loads(args.places_cache.read_text()) if args.places_cache.exists() else {}
        locator = Locator(cache, now, max_lookups=10)
    reviews = json.loads(args.reviews.read_text())
    if args.official and args.official.exists():
        reviews = merge_official(reviews, json.loads(args.official.read_text()).get('incidentes', []))
    result = build(json.loads(args.report.read_text()), reviews,
                   now, config['allowed_hosts'], locator.locate if locator else None)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    if locator:
        args.places_cache.parent.mkdir(parents=True, exist_ok=True)
        args.places_cache.write_text(json.dumps(locator.cache, ensure_ascii=False, indent=1, sort_keys=True) + '\n')
    print(json.dumps({'app_reports': len(result['reports']),
                      'map_points': sum(r['coordinates'] is not None for r in result['reports']),
                      'osm_lookups': locator.log if locator else []}, ensure_ascii=False))
