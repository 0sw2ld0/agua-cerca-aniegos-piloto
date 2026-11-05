#!/usr/bin/env python3
"""Build the app feed: reviewed incidents plus clearly distinguished publications."""
import argparse
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit
from generate import PERU, instant, recent_publication, Fetcher, normalized


def stable_id(key):
    return -int(hashlib.sha256(key.encode()).hexdigest()[:13], 16)


def canonical(url):
    parts = urlsplit(url)
    return parts.scheme + '://' + parts.netloc.lower() + parts.path.rstrip('/')


def build(report, reviews, now, allowed):
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
        coords = review.get('coordinates')
        if coords is not None:
            if (len(coords) != 2 or not all(type(c) in (float, int) for c in coords)
                    or not -78 <= coords[0] <= -75 or not -14 <= coords[1] <= -10
                    or not review.get('location_evidence')):
                raise ValueError('Unverified geometry')
        identifier = stable_id(review['key'])
        if identifier in ids:
            raise ValueError('Duplicate reviewed incident')
        ids.add(identifier)
        rows.append({'id': identifier, 'kind': 'incident', 'district': review['district'],
                     'location': review['location'], 'summary': review['summary'],
                     'date': review['day'], 'expires_at': expires.isoformat(),
                     'status': 'Situación actual sin confirmar', 'coordinates': coords,
                     'precision': review['precision'], 'sources': review['sources']})
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
    args = parser.parse_args()
    config = json.loads(Path(__file__).with_name('config.json').read_text())
    result = build(json.loads(args.report.read_text()), json.loads(args.reviews.read_text()),
                   datetime.now(PERU), config['allowed_hosts'])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'app_reports': len(result['reports']),
                      'map_points': sum(r['coordinates'] is not None for r in result['reports'])}))
