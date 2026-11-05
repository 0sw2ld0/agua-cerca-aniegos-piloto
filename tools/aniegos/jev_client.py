"""Small local TypeSafe client. Auth never enters payload files or console output."""
import hashlib
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.request

ENDPOINT = 'https://api.typesafe.ai/v1/systemone'
PRICE_PER_INPUT_TOKEN = 0.042 / 1_000_000
# With the documented default 64k context, a request costs <= $0.002753.
# Reserve $0.004 per network attempt, including errors; no long-context header.
RESERVE_PER_ATTEMPT = 0.004


def api_key(env_file=Path('.env.local')):
    key = os.environ.get('TYPESAFE_API_KEY')
    if not key and env_file.exists():
        for line in env_file.read_text().splitlines():
            match = re.match(r'^\s*(?:export\s+)?TYPESAFE_API_KEY\s*=\s*(.*?)\s*$', line)
            if match:
                key = match[1].strip().strip('\"\x27')
                break
    if not key or '\n' in key or '\r' in key:
        raise ValueError('Missing or invalid TYPESAFE_API_KEY; value not logged.')
    return key


def private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


class JevClient:
    def __init__(self, output, budget=0.10, key_file=Path('.env.local'), allow_network=False):
        if not 0 < budget <= 0.10:
            raise ValueError('This local pilot is authorized for at most US$0.10.')
        self.output, self.budget = Path(output), budget
        self.key_file = key_file
        self.allow_network = allow_network
        self.ledger_path = self.output / 'usage.json'
        self.ledger = json.loads(self.ledger_path.read_text()) if self.ledger_path.exists() else {
            'budget_usd': budget, 'reserved_per_attempt_usd': RESERVE_PER_ATTEMPT,
            'attempts': [], 'input_tokens': 0, 'output_tokens': 0,
            'estimated_cost_usd': 0.0,
        }

    def ask(self, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
        if len(body) > 40_000:
            raise ValueError('Pilot request too large; split state/questions first.')
        digest = hashlib.sha256(body).hexdigest()
        response_path = self.output / 'responses' / (digest + '.json')
        if response_path.exists():
            return json.loads(response_path.read_text()), True
        if not self.allow_network:
            raise RuntimeError('No cached result. Live API requires explicit authorization and --live.')
        committed = sum(
            a['input_tokens'] * PRICE_PER_INPUT_TOKEN if a.get('status') == 'success'
            else a.get('reserved_usd', RESERVE_PER_ATTEMPT)
            for a in self.ledger['attempts'])
        if committed + RESERVE_PER_ATTEMPT > min(self.budget, self.ledger['budget_usd']) + 1e-12:
            raise RuntimeError('Authorized request budget reached; no network request sent.')
        private_json(self.output / 'requests' / (digest + '.json'), payload)
        attempt = {'sha256': digest, 'reserved_usd': RESERVE_PER_ATTEMPT, 'status': 'started'}
        self.ledger['attempts'].append(attempt)
        private_json(self.ledger_path, self.ledger)

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                raise ValueError('Redirect refused for authenticated request.')

        request = urllib.request.Request(ENDPOINT, data=body, method='POST', headers={
            'Authorization': 'Bearer ' + api_key(self.key_file),
            'Content-Type': 'application/json', 'Accept': 'application/json',
        })
        started = time.monotonic()
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=90) as response:
                raw = response.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise ValueError('TypeSafe response too large.')
            result = json.loads(raw)
            if not isinstance(result.get('answers'), dict) or not isinstance(result.get('usage'), dict):
                raise ValueError('Unexpected TypeSafe response shape.')
            usage = result['usage']
            for key in ('input_tokens', 'output_tokens'):
                if type(usage.get(key)) is not int or usage[key] < 0:
                    raise ValueError('Missing token accounting.')
            attempt.update(status='success', duration_seconds=round(time.monotonic() - started, 3),
                           model=result.get('model'), **usage)
            self.ledger['input_tokens'] += usage['input_tokens']
            self.ledger['output_tokens'] += usage['output_tokens']
            self.ledger['estimated_cost_usd'] = round(self.ledger['input_tokens'] * PRICE_PER_INPUT_TOKEN, 8)
            private_json(response_path, result)
            return result, False
        except urllib.error.HTTPError as error:
            attempt.update(status='http_error', http_status=error.code)
            raise RuntimeError('TypeSafe returned HTTP ' + str(error.code) + '; not treated as an answer.') from None
        except Exception as error:
            attempt.update(status='error', error_type=type(error).__name__)
            raise RuntimeError('TypeSafe request failed; inspect status metadata. Auth is not logged.') from None
        finally:
            private_json(self.ledger_path, self.ledger)
