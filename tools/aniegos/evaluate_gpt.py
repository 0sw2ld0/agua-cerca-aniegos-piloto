#!/usr/bin/env python3
"""Local test: GPT extraction + code gates vs. the manual reviews. Publishes nothing.

  python3 tools/aniegos/evaluate_gpt.py --report RESULTADO/report.json [--rss-cache rss.json] [--live]

Without --live only cached responses are used. Corpus, requests and responses stay in .gpt-eval/ (git-ignored).
"""
import argparse
from datetime import datetime
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from generate import PERU, Fetcher, Page, nodes  # noqa: E402
from gpt_incidents import gate, merge, request, same_place  # noqa: E402
from jev_client import private_json  # noqa: E402
from openai_client import OpenAIClient, output_json  # noqa: E402
from osm_places import Locator  # noqa: E402

ROOT = Path(__file__).resolve().parent


class Paragraphs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth, self.parts, self.skip = 0, [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'nav', 'footer', 'aside'):
            self.skip += 1
        if tag == 'p':
            self.depth += 1
            self.parts.append('')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'nav', 'footer', 'aside') and self.skip:
            self.skip -= 1
        if tag == 'p' and self.depth:
            self.depth -= 1

    def handle_data(self, data):
        if self.depth and not self.skip:
            self.parts[-1] += data


def article_text(raw):
    page = Page()
    page.feed(raw)
    for node in nodes(page.documents):
        types = node.get('@type', [])
        types = [types] if isinstance(types, str) else types
        if any(t in ('NewsArticle', 'Article', 'BlogPosting', 'ReportageNewsArticle') for t in types) \
                and node.get('articleBody'):
            return html.unescape(node['articleBody']), node.get('datePublished'), node.get('headline')
    parser = Paragraphs()
    parser.feed(raw)
    text = '\n'.join(p.strip() for p in parser.parts if len(p.strip()) > 40)
    return html.unescape(text), None, page.meta.get('og:title')


def corpus(report, rss, folder, allowed):
    path = folder / 'corpus.json'
    if path.exists():
        return json.loads(path.read_text())
    reviews = json.loads((ROOT / 'reviewed.json').read_text())
    config = json.loads((ROOT / 'config.json').read_text())
    dates = {c['url']: (c.get('published_at'), c.get('title')) for c in report['candidates']
             if c.get('decision') == 'needs_review'}
    urls = list(dates) + [s['url'] for r in reviews for s in r['sources']] + config['seed_urls']
    fetch, items, seen = Fetcher(allowed), [], set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        try:
            text, published, title = article_text(fetch.get(url))
        except (OSError, ValueError) as error:
            print('skip', url, type(error).__name__)
            continue
        published = published or dates.get(url, (None,))[0] or rss.get(url, {}).get('published_at')
        title = title or dates.get(url, (None, ''))[1] or rss.get(url, {}).get('title', '')
        if not published or len(text) < 200:
            print('skip', url, 'no date' if not published else 'no text')
            continue
        source = re.sub(r'^www\.', '', url.split('/')[2])
        items.append({'url': url, 'source': source, 'title': title, 'published_at': published, 'text': text})
    private_json(path, items)
    return items


def matches(manual, auto):
    probe = {'district': manual['district'], 'location': manual['location'],
             'point': manual.get('coordinates') or manual.get('auto_point')}
    return [a for a in auto if a['day'] == manual['day'] and same_place(probe, a)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--rss-cache', type=Path)
    parser.add_argument('--places-cache', type=Path)
    parser.add_argument('--private', type=Path, default=Path('.gpt-eval/2026-11-06'))
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--now', default='2026-11-06T13:00:00-05:00')
    args = parser.parse_args()
    now = datetime.fromisoformat(args.now)
    config = json.loads((ROOT / 'config.json').read_text())
    rss = json.loads(args.rss_cache.read_text()) if args.rss_cache and args.rss_cache.exists() else {}
    articles = corpus(json.loads(args.report.read_text()), rss, args.private, config['allowed_hosts'])
    client = OpenAIClient(args.private, allow_network=args.live)
    cache = json.loads(args.places_cache.read_text()) if args.places_cache and args.places_cache.exists() else {}
    locator = Locator(cache, now, max_lookups=200, deadline_s=1500)

    accepted, rejected, failures = [], [], []
    for article in articles:
        try:
            result, cached = client.ask(request(article))
            extraction = output_json(result)
            if extraction is None:
                failures.append({'url': article['url'], 'reason': 'incomplete_or_refused'})
                continue
        except (RuntimeError, ValueError) as error:
            failures.append({'url': article['url'], 'reason': str(error)})
            continue
        ok, bad = gate(article, extraction, now, locator, check_window=False)
        accepted += ok
        rejected += bad
    if args.places_cache:
        args.places_cache.write_text(json.dumps(locator.cache, ensure_ascii=False, indent=1, sort_keys=True) + '\n')
    merged = merge(accepted)

    reviews = json.loads((ROOT / 'reviewed.json').read_text())
    for review in reviews:
        if not review.get('coordinates'):
            found = locator.locate(review)
            review['auto_point'] = found['point'] if found else None
    comparison = []
    for review in reviews:
        hit = matches(review, merged)
        comparison.append({'manual': review['key'], 'day': review['day'], 'district': review['district'],
                           'location': review['location'], 'found': bool(hit),
                           'auto_location': hit[0]['location'] if hit else None,
                           'auto_point': hit[0]['point'] if hit else None,
                           'auto_sources': hit[0]['sources'] if hit else []})
    extra = [m for m in merged if not any(matches(r, [m]) for r in reviews)]
    summary = {
        'articles': len(articles), 'failures': failures, 'usage': client.ledger,
        'auto_incidents': len(merged), 'auto_with_point': sum(m['point'] is not None for m in merged),
        'auto_stated_date': sum(m['date_status'] == 'stated' for m in merged),
        'manual_found': sum(c['found'] for c in comparison), 'manual_total': len(comparison),
        'rejections': {}, 'comparison': comparison,
        'extra': [{k: m[k] for k in ('day', 'date_status', 'district', 'location', 'location_quote', 'point', 'sources',
                                     'summary')}
                  for m in extra],
        'rejected': [{k: r.get(k) for k in ('reason', 'district', 'location', 'location_quote', 'url')} for r in rejected],
    }
    for r in rejected:
        summary['rejections'][r['reason']] = summary['rejections'].get(r['reason'], 0) + 1
    private_json(args.private / 'resultado.json', summary)
    print(json.dumps({k: summary[k] for k in ('articles', 'auto_incidents', 'auto_with_point', 'auto_stated_date',
                                              'manual_found',
                                              'manual_total', 'rejections')}, ensure_ascii=False))
    print('failures', len(failures), 'cost_usd', client.ledger['estimated_cost_usd'],
          'tokens', client.ledger['input_tokens'], client.ledger['output_tokens'])


if __name__ == '__main__':
    main()
