"""Durable, public-safe typed response cache and bounded pilot accounting.

No article bodies, prompts, headers or keys are stored in repository state.
"""
from datetime import timedelta
import hashlib
import json
from pathlib import Path
from generate import instant
from jev_client import JevClient, PRICE_PER_INPUT_TOKEN, RESERVE_PER_ATTEMPT

RUN_LIMIT = 0.02
DAY_LIMIT = 0.02
PILOT_LIMIT = 0.10
# Local trial + first successful Action, before this durable ledger existed.
PREVIOUS_ESTIMATED_COST = 0.00168496 + 0.00029992
BACKOFF_HOURS = 6


class DeferredRequest(RuntimeError):
    """A known retry/budget gate; never a negative news judgment."""


def digest(payload):
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def clean_response(payload, response):
    """Persist only validated primitive values, never arbitrary provider fields."""
    answers = {}
    for key, question in payload['questions'].items():
        answer = response['answers'].get(key, {})
        kind = question['type']
        if answer.get('type') != kind:
            raise ValueError('Invalid answer type')
        if kind == 'noul':
            value = answer.get('noul')
            if type(value) not in (float, int) or not 0 <= value <= 1:
                raise ValueError('Invalid probability')
        elif kind == 'choice':
            value = answer.get('choice')
            if value not in question['criteria']:
                raise ValueError('Unknown choice')
        else:
            raise ValueError('Unsupported primitive')
        answers[key] = {'type':kind, kind:value}
    usage = response['usage']
    if any(type(usage.get(k)) is not int or usage[k] < 0 for k in ('input_tokens','output_tokens')):
        raise ValueError('Invalid usage')
    model = response.get('model')
    if not isinstance(model, str) or len(model)>100:
        raise ValueError('Invalid model name')
    return {'model':model,'answers':answers,'usage':{k:usage[k] for k in ('input_tokens','output_tokens')}}


class PersistentJevClient:
    def __init__(self, state_path, private, now, client=None):
        self.path, self.now = Path(state_path), now
        if self.path.exists():
            self.state = json.loads(self.path.read_text())
            if self.state.get('schema_version') != 1:
                raise ValueError('Unsupported Jev state schema; do not reset silently')
        else:
            self.state = {'schema_version':1, 'previous_estimated_cost_usd':PREVIOUS_ESTIMATED_COST,
                          'previous_cost_day':'2026-11-05', 'entries':{}, 'attempts':[]}
        self.client = client or JevClient(private,budget=RUN_LIMIT,key_file=Path('/nonexistent'),allow_network=True)
        self.ledger = self.client.ledger
        self.cache_hits = 0
        self.new_requests = 0
        self.run_committed = 0

    def save(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.state,ensure_ascii=False,indent=2)+'\n')
        tmp.replace(self.path)

    def ask(self,payload):
        key=digest(payload)
        entry=self.state['entries'].get(key)
        if entry and entry['status']=='success':
            self.cache_hits+=1
            return clean_response(payload,entry['response']),True
        if entry and instant(entry['retry_after']) > self.now:
            raise DeferredRequest('retry_backoff')
        spent=sum(a['committed_usd'] for a in self.state['attempts'])
        today=self.now.strftime('%Y-%m-%d')
        daily=sum(a['committed_usd'] for a in self.state['attempts'] if a['day']==today)
        if self.state.get('previous_cost_day', '2026-11-05') == today:
            daily += self.state['previous_estimated_cost_usd']
        reserve=RESERVE_PER_ATTEMPT
        if (self.run_committed+reserve>RUN_LIMIT or daily+reserve>DAY_LIMIT or
                self.state['previous_estimated_cost_usd']+spent+reserve>PILOT_LIMIT):
            raise DeferredRequest('budget_limit')
        receipt={'day':today,'committed_usd':reserve,'status':'started','sha256':key}
        self.state['attempts'].append(receipt)
        self.state['entries'][key]={'status':'started','retry_after':(self.now+timedelta(hours=BACKOFF_HOURS)).isoformat()}
        self.run_committed+=reserve
        self.save()  # local receipt exists before the request; workflow persists even on failure
        self.new_requests+=1
        try:
            response,_=self.client.ask(payload)
            cleaned=clean_response(payload,response)
            actual=cleaned['usage']['input_tokens']*PRICE_PER_INPUT_TOKEN
            self.run_committed+=actual-reserve
            receipt.update(committed_usd=actual,status='success')
            self.state['entries'][key]={'status':'success','response':cleaned}
            self.save()
            return cleaned,False
        except Exception:
            receipt['status']='error'
            self.state['entries'][key]['status']='error'
            self.save()
            raise
