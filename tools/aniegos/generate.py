#!/usr/bin/env python3
"""Local pilot. Discovery != incident verification. Python 3.9+, stdlib only."""
import argparse
import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

PERU = timezone(timedelta(hours=-5))
MAX_BYTES = 3_000_000


def normalized(value):
    return ''.join(c for c in unicodedata.normalize('NFD', value.lower())
                   if unicodedata.category(c) != 'Mn')


def instant(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return result if result.tzinfo else None


def recent_publication(value, now):
    stamp = instant(value)
    return stamp is not None and now - timedelta(hours=48) <= stamp <= now


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta, self.documents, self.links = {}, [], []
        self.in_json = False
        self.buffer = ''

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta':
            self.meta[attrs.get('property', attrs.get('name', ''))] = attrs.get('content', '')
        if tag == 'a' and attrs.get('href'):
            self.links.append(attrs['href'])
        if tag == 'script' and attrs.get('type') == 'application/ld+json':
            self.in_json, self.buffer = True, ''

    def handle_data(self, data):
        if self.in_json:
            self.buffer += data

    def handle_endtag(self, tag):
        if tag == 'script' and self.in_json:
            try:
                self.documents.append(json.loads(self.buffer))
            except (ValueError, TypeError):
                pass
            self.in_json = False


def nodes(value):
    if isinstance(value, list):
        for child in value:
            yield from nodes(child)
    elif isinstance(value, dict):
        yield value
        # Do not harvest unrelated recommended articles or organization addresses.
        yield from nodes(value.get('@graph', []))


def article(data, url, feed_entry=None):
    page = Page()
    page.feed(data)
    news = []
    for node in nodes(page.documents):
        types = node.get('@type', [])
        if isinstance(types, str):
            types = [types]
        if any(t in ('NewsArticle', 'Article', 'BlogPosting') for t in types):
            news.append(node)
    if not news:
        # A publisher's own RSS can supply the timestamp when its page has no JSON-LD.
        # Search-engine dates must never enter this path.
        if (feed_entry and feed_entry.get('kind') == 'official'
                and feed_entry.get('url') == url and page.meta.get('og:type') == 'article'):
            return {'url': url, 'title': page.meta.get('og:title', ''),
                    'published_at': feed_entry['published_at'], 'modified_at': None,
                    '_body': feed_entry.get('_body', '')}
        return None
    node = news[0]
    return {
        'url': url,
        'title': html.unescape(node.get('headline') or page.meta.get('og:title', '')),
        'published_at': node.get('datePublished'),
        'modified_at': node.get('dateModified'),
        '_body': html.unescape(node.get('articleBody') or node.get('description') or ''),
    }


def locations(text):
    """Hints only: a mention does not establish an incident, date or point."""
    text = normalized(text)
    names = ['Villa El Salvador', 'Villa María del Triunfo', 'Chorrillos',
             'San Juan de Miraflores', 'Sector 1, Grupo 1',
             'Hospital de la Solidaridad', 'Avenida 26 de Noviembre',
             'Óvalo de Nueva Esperanza', 'Avenida Los Héroes']
    return [name for name in names if normalized(name) in text]


def candidate(data, url, now, provenance, feed_entry=None):
    parsed = article(data, url, feed_entry)
    if not parsed:
        return {'url': url, 'decision': 'rejected', 'reason': 'no_article_metadata'}
    body = parsed.pop('_body')
    relevant = any(word in normalized(parsed['title']) for word in ('aniego', 'inund', 'alcantarillado', 'desague'))
    if not relevant:
        return {'url': url, 'decision': 'rejected', 'reason': 'not_incident_candidate'}
    if not recent_publication(parsed['published_at'], now):
        return {**parsed, 'decision': 'rejected', 'reason': 'old_future_or_unknown_publication_date'}
    return {
        **parsed,
        'id': hashlib.sha256(url.encode()).hexdigest()[:20],
        'decision': 'needs_review',
        'discovered_via': provenance,
        'source_kind': 'official' if urllib.parse.urlsplit(url).hostname == 'www.defensoria.gob.pe' else 'media',
        'fetched_at': now.isoformat(),
        'content_sha256': hashlib.sha256(data.encode()).hexdigest(),
        'location_hints': locations(parsed['title'] + ' ' + body),
        'incident_at': None,
        'current_status': 'unconfirmed',
        'geometry': None,
        'review_reasons': ['publication_date_is_not_incident_date',
                           'one_article_can_describe_multiple_incidents',
                           'location_and_current_status_require_verification'],
    }


class Fetcher:
    def __init__(self, allowed):
        self.allowed = set(allowed)
        self.last = {}

    def check(self, url):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != 'https' or parts.hostname not in self.allowed or parts.username or parts.password or parts.port not in (None, 443):
            raise ValueError('URL outside HTTPS allowlist')

    def get(self, url):
        self.check(url)
        host = urllib.parse.urlsplit(url).hostname
        time.sleep(max(0, 1 - (time.monotonic() - self.last.get(host, 0))))
        self.last[host] = time.monotonic()
        owner = self

        class Redirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                owner.check(newurl)
                return super().redirect_request(req, fp, code, msg, headers, newurl)

        request = urllib.request.Request(url, headers={'User-Agent': 'AguaCerca-ReportPilot/0.1 (local research)'})
        with urllib.request.build_opener(Redirect).open(request, timeout=20) as response:
            raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError('response too large')
            return raw.decode('utf-8', errors='replace')


def discover(xml, kind, now):
    output = []
    for item in ET.fromstring(xml).findall('.//item')[:60]:
        title = item.findtext('title', '')
        if not any(word in normalized(title) for word in ('aniego', 'inund', 'desague', 'alcantarillado')):
            continue
        try:
            date = parsedate_to_datetime(item.findtext('pubDate', '')).isoformat()
        except (TypeError, ValueError):
            date = None
        if not recent_publication(date, now):
            continue
        source = item.find('source')
        output.append({'title': title, 'url': item.findtext('link'), 'published_at': date,
                       'publisher_url': source.get('url') if source is not None else None,
                       'kind': kind,
                       '_body': html.unescape(re.sub('<[^>]+>', ' ', item.findtext(
                           '{http://purl.org/rss/1.0/modules/content/}encoded', '')))})
    return output


def approved_features(reviews, candidates, now):
    """Explicit reviewed facts only. Never use publication/fetch time to renew a case."""
    by_url = {c['url']: c for c in candidates if c.get('decision') == 'needs_review'}
    features, rejected = [], []
    seen = set()
    for entry in reviews:
        source = by_url.get(entry.get('source_url'))
        stamp = instant(entry.get('incident_at'))
        reviewed = instant(entry.get('reviewed_at'))
        coords = entry.get('coordinates')
        valid = (
            source and entry.get('reviewed_by') and reviewed and reviewed <= now
            and stamp and now - timedelta(hours=48) <= stamp <= now
            and entry.get('event_id') and entry.get('location_label')
            and entry.get('status') in ('reported', 'in_attention', 'resolved', 'unconfirmed')
            and entry.get('location_precision') in ('approximate', 'verified_point')
            and isinstance(coords, list) and len(coords) == 2
            and all(type(v) in (int, float) for v in coords)
            and -78 <= coords[0] <= -75 and -14 <= coords[1] <= -10
        )
        if not valid or entry.get('event_id') in seen:
            rejected.append({'event_id': entry.get('event_id'), 'reason': 'invalid_expired_duplicate_or_unverified_review'})
            continue
        seen.add(entry['event_id'])
        features.append({'type': 'Feature', 'id': entry['event_id'],
                         'geometry': {'type': 'Point', 'coordinates': coords},
                         'properties': {key: entry[key] for key in (
                             'source_url', 'incident_at', 'reviewed_at', 'reviewed_by',
                             'location_label', 'location_precision', 'status')}})
    return features, rejected


def run(config, output, now, reviews):
    fetch = Fetcher(config['allowed_hosts'])
    discovery, errors, candidates = [], [], []
    urls = {url: 'seed' for url in config['seed_urls']}
    feed_entries = {}
    for feed in config['feeds']:
        try:
            entries = discover(fetch.get(feed['url']), feed['kind'], now)
            discovery.extend({k: v for k, v in item.items() if k != '_body'} for item in entries)
            if feed['kind'] != 'discovery_only':
                for item in entries:
                    urls.setdefault(item['url'], 'publisher_feed')
                    feed_entries[item['url']] = item
        except (OSError, ValueError, ET.ParseError) as error:
            errors.append({'url': feed['url'], 'error': str(error), 'stage': 'discovery'})
    for url, provenance in list(urls.items())[:15]:
        try:
            candidates.append(candidate(fetch.get(url), url, now, provenance, feed_entries.get(url)))
        except (OSError, ValueError) as error:
            errors.append({'url': url, 'error': str(error), 'stage': 'article'})
    features, rejected_reviews = approved_features(reviews, candidates, now)
    counts = {
        'discovered_recent_links': len(discovery), 'articles_attempted': min(len(urls), 15),
        'article_candidates': sum(c.get('decision') == 'needs_review' for c in candidates),
        'automatically_discovered_candidates': sum(c.get('discovered_via') == 'publisher_feed' for c in candidates),
        'rejected_articles': sum(c.get('decision') == 'rejected' for c in candidates),
        'fetch_errors': len(errors), 'reviewed_map_points': len(features),
    }
    report = {'schema_version': 1, 'generated_at': now.isoformat(), 'window_hours': 48,
              'mode': 'local_pilot_not_app_feed', 'counts': counts, 'discovery': discovery,
              'candidates': candidates, 'errors': errors, 'rejected_reviews': rejected_reviews}
    output.mkdir(parents=True, exist_ok=True)
    for name, value in [('report.json', report), ('aniegos.geojson', {
        'type': 'FeatureCollection', 'generated_at': now.isoformat(), 'window_hours': 48,
        'coverage': 'partial', 'features': features})]:
        (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    lines = ['# Resultado del piloto local', '', 'Consulta: ' + now.isoformat(), '',
             'No equivale a un inventario exhaustivo ni a confirmación de actividad actual.', '',
             '| Métrica | Resultado |', '| --- | --- |']
    lines += [f'| {key} | {value} |' for key, value in counts.items()]
    lines += ['', '## Candidatos para revisión', '']
    for c in candidates:
        if c['decision'] == 'needs_review':
            # Own summaries only; no article bodies or lengthy copyrighted extracts.
            lines += [f"- {urllib.parse.urlsplit(c['url']).hostname}: publicación {c['published_at']}; "
                      f"lugares mencionados: {', '.join(c['location_hints']) or 'sin identificar'}. "
                      f"[Fuente]({c['url']}). Fecha de incidente y punto pendientes."]
    lines += ['', '## Fallos de descarga', '']
    lines += [f"- {e['url']}: {e['error']}" for e in errors]
    (output / 'RESULTADO.md').write_text('\n'.join(lines) + '\n')
    print(json.dumps(counts, ensure_ascii=False, indent=2))
    # Partial success is explicit. A totally failed run must fail CI rather than publish empty success.
    return 0 if any(c.get('decision') == 'needs_review' for c in candidates) else 2


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path(__file__).with_name('config.json'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--reviews', type=Path)
    parser.add_argument('--now', help='ISO8601 with timezone; reproducible tests only')
    args = parser.parse_args()
    now = instant(args.now) if args.now else datetime.now(PERU)
    if now is None:
        parser.error('--now requires a timezone')
    raise SystemExit(run(json.loads(args.config.read_text()), args.output, now,
                         json.loads(args.reviews.read_text()) if args.reviews else []))
