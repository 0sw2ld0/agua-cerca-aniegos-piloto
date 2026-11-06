"""Small OpenAI Responses API client for the local pilot. The key never enters payload files or logs."""
import hashlib
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.request

from jev_client import private_json

ENDPOINT = 'https://api.openai.com/v1/responses'
# Official standard tier, checked 2026-11-06 at developers.openai.com/api/docs/pricing.
PRICES = {'gpt-6-luna': (0.10 / 1_000_000, 0.50 / 1_000_000)}
MAX_OUTPUT_TOKENS = 4000
MAX_REQUEST_BYTES = 60_000


def api_key(env_file=Path('.env.local')):
    key = os.environ.get('OPENAI_API_KEY')
    if not key and env_file.exists():
        for line in env_file.read_text().splitlines():
            match = re.match(r'^\s*(?:export\s+)?OPENAI_API_KEY\s*=\s*(.*?)\s*$', line)
            if match:
                key = match[1].strip().strip('"\'')
                break
    if not key or '\n' in key or '\r' in key:
        raise ValueError('Missing or invalid OPENAI_API_KEY; value not logged.')
    return key


def reserve_for(model, body_bytes):
    price_in, price_out = PRICES[model]
    # Upper bound: ~1 token per 3 bytes of request plus the full output allowance.
    return body_bytes / 3 * price_in + MAX_OUTPUT_TOKENS * price_out


class OpenAIClient:
    def __init__(self, output, model='gpt-6-luna', budget=0.10, key_file=Path('.env.local'), allow_network=False):
        if model not in PRICES:
            raise ValueError('Unknown price for model; refusing to estimate cost.')
        if not 0 < budget <= 0.10:
            raise ValueError('This local pilot is authorized for at most US$0.10.')
        self.output, self.model, self.budget = Path(output), model, budget
        self.key_file, self.allow_network = key_file, allow_network
        self.ledger_path = self.output / 'usage.json'
        self.ledger = json.loads(self.ledger_path.read_text()) if self.ledger_path.exists() else {
            'model': model, 'budget_usd': budget, 'attempts': [],
            'input_tokens': 0, 'output_tokens': 0, 'estimated_cost_usd': 0.0}

    def committed(self):
        return sum(a.get('cost_usd', 0) if a.get('status') == 'success' else a.get('reserved_usd', 0)
                   for a in self.ledger['attempts'])

    def ask(self, payload):
        payload = {**payload, 'model': self.model, 'max_output_tokens': MAX_OUTPUT_TOKENS, 'store': False}
        body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
        if len(body) > MAX_REQUEST_BYTES:
            raise ValueError('Request too large; shorten the article first.')
        digest = hashlib.sha256(body).hexdigest()
        response_path = self.output / 'responses' / (digest + '.json')
        if response_path.exists():
            return json.loads(response_path.read_text()), True
        if not self.allow_network:
            raise RuntimeError('No cached result. Live API requires --live.')
        reserve = reserve_for(self.model, len(body))
        if self.committed() + reserve > min(self.budget, self.ledger['budget_usd']) + 1e-12:
            raise RuntimeError('Authorized budget reached; no network request sent.')
        private_json(self.output / 'requests' / (digest + '.json'), payload)
        attempt = {'sha256': digest, 'reserved_usd': round(reserve, 8), 'status': 'started'}
        self.ledger['attempts'].append(attempt)
        private_json(self.ledger_path, self.ledger)

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                raise ValueError('Redirect refused for authenticated request.')

        request = urllib.request.Request(ENDPOINT, data=body, method='POST', headers={
            'Authorization': 'Bearer ' + api_key(self.key_file),
            'Content-Type': 'application/json', 'Accept': 'application/json'})
        started = time.monotonic()
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=120) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise ValueError('Response too large.')
            result = json.loads(raw)
            usage = result.get('usage') or {}
            if type(usage.get('input_tokens')) is not int or type(usage.get('output_tokens')) is not int:
                raise ValueError('Missing token accounting.')
            price_in, price_out = PRICES[self.model]
            cost = usage['input_tokens'] * price_in + usage['output_tokens'] * price_out
            attempt.update(status='success', duration_seconds=round(time.monotonic() - started, 3),
                           model=result.get('model'), response_status=result.get('status'),
                           input_tokens=usage['input_tokens'], output_tokens=usage['output_tokens'],
                           cost_usd=round(cost, 8))
            self.ledger['input_tokens'] += usage['input_tokens']
            self.ledger['output_tokens'] += usage['output_tokens']
            self.ledger['estimated_cost_usd'] = round(self.ledger['estimated_cost_usd'] + cost, 8)
            private_json(response_path, result)
            return result, False
        except urllib.error.HTTPError as error:
            detail = error.read(2000).decode('utf-8', 'replace')
            attempt.update(status='http_error', http_status=error.code, detail=detail[:500])
            raise RuntimeError('OpenAI returned HTTP %d; not treated as an answer.' % error.code) from None
        except Exception as error:
            attempt.update(status='error', error_type=type(error).__name__)
            raise RuntimeError('OpenAI request failed; key is not logged.') from None
        finally:
            private_json(self.ledger_path, self.ledger)


def output_json(result):
    """Parsed structured output, or None if refused/incomplete."""
    if result.get('status') != 'completed':
        return None
    for item in result.get('output', []):
        if item.get('type') == 'message':
            for part in item.get('content', []):
                if part.get('type') == 'output_text':
                    return json.loads(part['text'])
                if part.get('type') == 'refusal':
                    return None
    return None
