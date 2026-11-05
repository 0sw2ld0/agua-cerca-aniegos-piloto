"""Manual CI pilot: public news -> typed drafts, without modifying the app feed."""
import argparse
from datetime import datetime, timedelta
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET
from generate import Fetcher, PERU, article, discover, instant, normalized, recent_publication
from jev_client import JevClient
from jev_state import PersistentJevClient, DeferredRequest
from publish import canonical
from jev_questions import date_spans, extractive_payload, questions_for, route, verdict, YES

MAX_ARTICLES = 3
MAX_PASSAGES = 2


class Paragraphs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
    def handle_data(self, value):
        self.parts.append(value)
    def handle_endtag(self, tag):
        if tag in ('p', 'div', 'br'):
            self.parts.append('\n')


def passages(body):
    if '<' in body:
        parser = Paragraphs()
        parser.feed(body)
        body = ''.join(parser.parts)
    # Preserve abbreviation periods. Each passage is evidence, not an incident yet.
    body = re.sub(r'\b(Av|Jr|Urb)\.', lambda m: m[0][:-1] + '\u2024', body)
    parts = re.split(r'\n+|(?<=[.!?])\s+(?=[A-ZÁÉÍÓÚÑ¿])', body)
    output = []
    for part in parts:
        part = ' '.join(part.replace('\u2024', '.').split())
        if (50 <= len(part) <= 1800 and any(word in normalized(part) for word in
                ('aniego', 'agua acumulada', 'inund', 'hidrojet', 'bomba', 'limpieza'))):
            if part not in output:
                output.append(part)
    return output[:MAX_PASSAGES]


def selected(answers, gate, choice, options):
    answer = answers.get(gate, {})
    p = answer.get('noul')
    if answer.get('type') != 'noul' or type(p) not in (int, float) or not 0 <= p <= 1:
        raise ValueError('Invalid Noul response')
    if p < YES or not options:
        return None
    answer = answers.get(choice, {})
    key = answer.get('choice')
    if answer.get('type') != 'choice' or key not in options:
        raise ValueError('Invalid extractive Choice response')
    return key


def analyze(document, text, client, now):
    payload = extractive_payload(document, text)
    payload['state']['fragments'] = {'passage_0': {'text': text}}
    payload['questions'].update({'passage_0__' + k: v for k, v in questions_for('passage_0').items()})
    response, cached = client.ask(payload)
    answers = response['answers']
    values = verdict(answers, 'passage_0')
    state = payload['state']
    place_key = selected(answers, 'has_place', 'place', state['places'])
    date_key = selected(answers, 'has_explicit_date', 'date', state['dates'])
    dates = date_spans(text, document['published'][:10])
    date = dates[int(date_key.split('_')[-1])] if date_key else None
    # Publication timestamp is NOT an event date. An inferred year remains provisional.
    day = date['date'] if date and not date['year_inferred'] else None
    decision = route(values, day, now) if day else 'review_date'
    return {'source_url': document['url'], 'published_at': document['published'],
            'incident_date': day, 'date_evidence': date,
            'place': state['places'][place_key] if place_key else None,
            'observations': values, 'decision': decision, 'coordinates': None,
            'current_status': 'not_independently_verified', 'eligible_for_publication': False,
            'model': response.get('model'), 'cached': cached}


def publisher_entries(config, urls, fetch, now):
    """Only the publisher's own RSS can supply missing metadata/body."""
    hosts = {urlsplit(url).hostname for url in urls}
    entries, failures = {}, []
    for feed in config.get('feeds', []):
        if feed['kind'] not in ('official', 'publisher') or urlsplit(feed['url']).hostname not in hosts:
            continue
        try:
            xml = fetch.get(feed['url'])
            valid = {entry['url']:entry for entry in discover(xml, feed['kind'], now)}
            for item in ET.fromstring(xml).findall('.//item'):
                url = item.findtext('link')
                if url not in valid or urlsplit(url).hostname != urlsplit(feed['url']).hostname:
                    continue
                entry = valid[url]
                entry['_body'] = item.findtext('{http://purl.org/rss/1.0/modules/content/}encoded', '') or entry['_body']
                entries[url] = entry
        except Exception as error:
            failures.append({'source_url':feed['url'], 'error_type':type(error).__name__, 'stage':'publisher_rss'})
    return entries, failures


def run(report, config, output, private, now, budget=0.02, client=None, fetch=None):
    output.mkdir(parents=True, exist_ok=True)
    client = client or JevClient(private, budget=budget, key_file=Path('/nonexistent'), allow_network=True)
    fetch = fetch or Fetcher(config['allowed_hosts'])
    drafts, failures, skipped = [], [], []
    candidates, seen = [], set()
    for item in report['candidates']:
        if item.get('decision') != 'needs_review' or not recent_publication(item.get('published_at'), now):
            continue
        if canonical(item['url']) in seen:
            continue
        seen.add(canonical(item['url']))
        candidates.append(item)
    entries, rss_failures = publisher_entries(config, [item['url'] for item in candidates], fetch, now)
    failures.extend(rss_failures)
    processed_articles = 0
    for item in candidates:
        try:
            fetch.check(item['url'])
            source = article(fetch.get(item['url']), item['url'], entries.get(item['url']))
            if not source or not recent_publication(source['published_at'], now):
                skipped.append({'source_url':item['url'], 'reason':'missing_or_expired_source_metadata'})
                continue
            body = source['_body'] or entries.get(item['url'], {}).get('_body', '')
            text_parts = passages(body)
            if not text_parts:
                skipped.append({'source_url':item['url'], 'reason':'no_suitable_article_passages'})
                continue
            stamp = instant(source['published_at']).astimezone(PERU)
            document = {'url':canonical(item['url']), 'title':source['title'], 'published':stamp.isoformat()}
            before = getattr(client, 'new_requests', 0)
            if processed_articles >= MAX_ARTICLES:
                skipped.append({'source_url':item['url'], 'reason':'run_article_limit'})
                continue
            for text in text_parts:
                try:
                    drafts.append(analyze(document, text, client, now))
                except DeferredRequest as error:
                    skipped.append({'source_url':item['url'], 'reason':str(error)})
            if getattr(client, 'new_requests', before + 1) > before:
                processed_articles += 1
        except Exception as error:
            # No error text: providers/auth must never leak into public logs/artifacts.
            failures.append({'source_url':item['url'], 'error_type':type(error).__name__})
    usage = client.ledger
    summary = {'mode':'manual_jev_pilot_no_publication', 'generated_at':now.isoformat(),
               'coverage':'partial', 'selected_articles':len(candidates), 'draft_count':len(drafts),
               'verified_map_points':0, 'api_attempts':len(usage['attempts']),
               'input_tokens':usage['input_tokens'], 'output_tokens':usage['output_tokens'],
               'estimated_cost_usd':usage['estimated_cost_usd'], 'max_budget_usd':budget,
               'reused_requests':getattr(client,'cache_hits',0),
               'new_requests':getattr(client,'new_requests',len(usage['attempts'])),
               'collection_errors':len(report.get('errors',[])),
               'drafts':drafts, 'errors':failures, 'skipped':skipped}
    (output/'report.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    lines = ['# Prueba de Jev en Actions', '',
             'Borradores experimentales. No modifica el feed ni confirma ausencia de aniegos.', '',
             f"Artículos seleccionados: {len(candidates)}; borradores: {len(drafts)}; puntos verificados: 0.",
             f"Consultas reutilizadas: {summary['reused_requests']}; llamadas: {summary['api_attempts']}; costo estimado: US${summary['estimated_cost_usd']:.8f}; límite: US${budget:.2f}.",
             f"Fallos del análisis: {len(failures)}; fallos del recolector: {summary['collection_errors']}.", '',
             'Las fechas faltantes, las ubicaciones y los estados inciertos requieren comprobación.']
    (output/'RESUMEN.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps({k:summary[k] for k in ('draft_count','api_attempts','estimated_cost_usd','verified_map_points')}))
    return 2 if failures else 0


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--private',type=Path,required=True)
    parser.add_argument('--state',type=Path,required=True)
    args=parser.parse_args()
    config=json.loads(Path(__file__).with_name('config.json').read_text())
    now=datetime.now(PERU)
    client=PersistentJevClient(args.state,args.private,now)
    raise SystemExit(run(json.loads(args.report.read_text()),config,args.output,args.private,now,client=client))
