#!/usr/bin/env python3
"""Shadow mode: GPT drafts incidents from recent news; NOTHING reaches the app feed.

Persists in the repository only validated extraction fields (short literal quotes, own-words
summaries), costs and outcomes, never article bodies, prompts, headers or keys. Without
OPENAI_API_KEY it exits cleanly. A failed or skipped call is never "no flooding".
"""
import argparse
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from generate import PERU, Fetcher  # noqa: E402
from evaluate_gpt import article_text  # noqa: E402
from gpt_incidents import SCHEMA, core, gate, merge, request, same_place  # noqa: E402
from openai_client import MAX_OUTPUT_TOKENS, OpenAIClient, output_json, reserve_for  # noqa: E402
from osm_places import Locator  # noqa: E402

RUN_LIMIT, DAY_LIMIT, TOTAL_LIMIT = 0.03, 0.10, 0.50
# Local evaluation of 6/11 (26 calls) counts toward the total.
PREVIOUS_ESTIMATED_COST = 0.0183851
BACKOFF = timedelta(hours=6)
MAX_ARTICLES = 30
QUOTE_LIMIT = 200
FIELDS = SCHEMA['properties']['incidents']['items']['properties']


class Deferred(Exception):
    pass


def clean(extraction):
    """Keep only schema fields with valid values; truncate quotes."""
    incidents = []
    for item in extraction.get('incidents', [])[:40]:
        row = {}
        for key, spec in FIELDS.items():
            value = item.get(key, '')
            if not isinstance(value, str) or ('enum' in spec and value not in spec['enum']):
                raise ValueError('Invalid field ' + key)
            row[key] = value[:QUOTE_LIMIT] if key.endswith('_quote') else value[:300]
        incidents.append(row)
    return {'incidents': incidents}


class ShadowState:
    def __init__(self, path, now):
        self.path, self.now = Path(path), now
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {}
        if self.state.get('schema_version') != 1:
            self.state = {'schema_version': 1, 'previous_estimated_cost_usd': PREVIOUS_ESTIMATED_COST,
                          'attempts': [], 'articles': {}}

    def spent(self, day=None):
        return sum(a['cost_usd'] for a in self.state['attempts'] if day is None or a['day'] == day)

    def allow(self, reserve, run_spent):
        day = self.now.date().isoformat()
        if run_spent + reserve > RUN_LIMIT:
            raise Deferred('run_limit')
        if self.spent(day) + reserve > DAY_LIMIT:
            raise Deferred('day_limit')
        if self.state['previous_estimated_cost_usd'] + self.spent() + reserve > TOTAL_LIMIT:
            raise Deferred('total_limit')

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.state, ensure_ascii=False, indent=1, sort_keys=True) + '\n')


def extract(article, shadow, client, run_spent):
    """Return (extraction, cost). Reuses a stored answer for the same text."""
    entry = shadow.state['articles'].get(article['url'], {})
    text_sha = hashlib.sha256(article['text'].encode()).hexdigest()
    if entry.get('text_sha') == text_sha and entry.get('extraction') is not None:
        return entry['extraction'], 0.0
    failed = entry.get('failed_at')
    if failed and shadow.now - datetime.fromisoformat(failed) < BACKOFF and entry.get('text_sha') == text_sha:
        raise Deferred('backoff')
    payload = request(article)
    size = len(json.dumps({**payload, 'model': client.model, 'max_output_tokens': MAX_OUTPUT_TOKENS},
                          ensure_ascii=False).encode())
    shadow.allow(reserve_for(client.model, size), run_spent)
    day = shadow.now.date().isoformat()
    try:
        result, _ = client.ask(payload)
        attempt = client.ledger['attempts'][-1]
        cost = attempt.get('cost_usd', 0.0)
        extraction = output_json(result)
        if extraction is None:
            raise ValueError('incomplete_or_refused')
        extraction = clean(extraction)
    except Exception as error:
        last = client.ledger['attempts'][-1] if client.ledger['attempts'] else {}
        cost = last.get('cost_usd', last.get('reserved_usd', 0.0))  # a failed attempt keeps its reservation
        shadow.state['attempts'].append({'day': day, 'cost_usd': round(cost, 8), 'status': 'error'})
        shadow.state['articles'][article['url']] = {**entry, 'text_sha': text_sha, 'failed_at': shadow.now.isoformat(),
                                                    'error': type(error).__name__}
        shadow.save()
        raise
    shadow.state['attempts'].append({'day': day, 'cost_usd': round(cost, 8), 'status': 'success',
                                     'input_tokens': attempt.get('input_tokens'),
                                     'output_tokens': attempt.get('output_tokens')})
    shadow.state['articles'][article['url']] = {
        'text_sha': text_sha, 'published_at': article['published_at'], 'title': article['title'][:200],
        'checked_at': shadow.now.isoformat(), 'model': attempt.get('model'), 'extraction': extraction}
    shadow.save()
    return extraction, cost


def compare_with_reviews(incidents, reviews):
    out = []
    for inc in incidents:
        hit = next((r['key'] for r in reviews if r['day'] == inc['day'] and same_place(
            {'district': r['district'], 'location': r['location'], 'point': r.get('coordinates')}, inc)), None)
        out.append(hit)
    return out


def run(report, reviews, now, state_path, shadow_path, places_path, private, output, fetch=None, client=None):
    output.mkdir(parents=True, exist_ok=True)
    if client is None and not os.environ.get('OPENAI_API_KEY'):
        (output / 'RESUMEN.md').write_text('# Modo sombra GPT\n\nSin OPENAI_API_KEY: no se hicieron consultas.\n')
        print('shadow: OPENAI_API_KEY not configured; skipped')
        return 0
    shadow = ShadowState(state_path, now)
    client = client or OpenAIClient(private, budget=RUN_LIMIT, key_file=Path('/nonexistent'), allow_network=True)
    locator = None
    if places_path:  # without a cache path there are no OSM lookups (tests, dry runs)
        cache = json.loads(places_path.read_text()) if places_path.exists() else {}
        locator = Locator(cache, now, max_lookups=8, deadline_s=150)
    fetch = fetch or Fetcher(json.loads((Path(__file__).with_name('config.json')).read_text())['allowed_hosts'])
    candidates = [c for c in report['candidates'] if c.get('decision') == 'needs_review'][:MAX_ARTICLES]
    accepted, rejected, deferred, errors, run_spent, calls = [], [], [], [], 0.0, 0
    for candidate in candidates:
        url = candidate['url']
        try:
            # Always re-read the article: quotes are verified against the full current text.
            text, published, title = article_text(fetch.get(url))
            if len(text) < 200:
                deferred.append({'url': url, 'reason': 'no_text'})
                continue
            article = {'url': url, 'source': url.split('/')[2].removeprefix('www.'),
                       'title': title or candidate.get('title', ''),
                       'published_at': candidate['published_at'] or published, 'text': text}
            extraction, cost = extract(article, shadow, client, run_spent)
            calls += cost > 0
            run_spent += cost
        except Deferred as reason:
            deferred.append({'url': url, 'reason': str(reason)})
            continue
        except Exception as error:  # network, provider, malformed output: logged, never "no incident"
            errors.append({'url': url, 'error': type(error).__name__})
            continue
        ok, bad = gate(article, extraction, now, locator)
        accepted += ok
        rejected += bad
    merged = merge(accepted)
    if locator:
        places_path.write_text(json.dumps(locator.cache, ensure_ascii=False, indent=1, sort_keys=True) + '\n')
    matches = compare_with_reviews(merged, reviews)
    snapshot = json.loads(shadow_path.read_text()) if shadow_path.exists() else {'schema_version': 1, 'seen': {}}
    for inc in merged:
        key = '%s|%s|%s' % (inc['day'], inc['district'], core(inc['location']))
        seen = snapshot['seen'].setdefault(key, {'first_seen': now.isoformat(), 'urls': []})
        seen.update({'last_seen': now.isoformat(), 'day': inc['day'], 'date_status': inc['date_status'],
                     'district': inc['district'], 'district_stated': inc.get('district_stated'),
                     'location': inc['location'], 'point': inc['point'], 'rule': inc.get('rule'),
                     'summary': inc['summary']})
        seen['urls'] = sorted(set(seen['urls']) | set(inc['urls']))
    snapshot.update({'generated_at': now.isoformat(), 'model': client.model, 'mode': 'shadow_not_published',
                     'current': [{'day': i['day'], 'date_status': i['date_status'], 'district': i['district'],
                                  'location': i['location'], 'point': i['point'], 'urls': i['urls'],
                                  'matches_review': m} for i, m in zip(merged, matches)]})
    shadow_path.parent.mkdir(parents=True, exist_ok=True)
    shadow_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1, sort_keys=True) + '\n')
    total = shadow.state['previous_estimated_cost_usd'] + shadow.spent()
    lines = ['# Modo sombra GPT (no publica)', '',
             '| Medición | Valor |', '| --- | ---: |',
             '| Notas revisadas | %d |' % len(candidates), '| Consultas nuevas | %d |' % calls,
             '| Costo de esta corrida | US$%.5f |' % run_spent,
             '| Costo de hoy | US$%.5f de %.2f |' % (shadow.spent(now.date().isoformat()), DAY_LIMIT),
             '| Costo acumulado | US$%.5f de %.2f |' % (total, TOTAL_LIMIT),
             '| Incidentes detectados | %d |' % len(merged),
             '| … con punto | %d |' % sum(i['point'] is not None for i in merged),
             '| … que coinciden con una revisión manual | %d |' % sum(m is not None for m in matches),
             '| Pospuestas / errores | %d / %d |' % (len(deferred), len(errors)), '',
             '## Incidentes en sombra', '']
    for inc, match in zip(merged, matches):
        lines.append('- %s · %s · %s%s%s — %s' % (
            inc['day'], inc['district'], inc['location'], ' · punto' if inc['point'] else '',
            ' · fecha por verificar' if inc['date_status'] != 'stated' else '',
            ('= revisión ' + match) if match else 'nuevo'))
    (output / 'RESUMEN.md').write_text('\n'.join(lines) + '\n')
    (output / 'detalle.json').write_text(json.dumps({'deferred': deferred, 'errors': errors, 'rejected': [
        {k: r.get(k) for k in ('reason', 'district', 'location', 'url')} for r in rejected]},
        ensure_ascii=False, indent=1) + '\n')
    print(json.dumps({'shadow_incidents': len(merged), 'calls': calls, 'run_cost_usd': round(run_spent, 6),
                      'errors': len(errors), 'deferred': len(deferred)}))
    return 1 if errors and not merged and calls == 0 and not deferred else 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--shadow', type=Path, required=True)
    parser.add_argument('--places-cache', type=Path)
    parser.add_argument('--private', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--reviews', type=Path, default=Path(__file__).with_name('reviewed.json'))
    args = parser.parse_args()
    raise SystemExit(run(json.loads(args.report.read_text()), json.loads(args.reviews.read_text()),
                         datetime.now(PERU), args.state, args.shadow, args.places_cache, args.private, args.output))
