"""Private atomic daily quote ledger. Never replay an uncertain runner operation."""
from datetime import datetime
from zoneinfo import ZoneInfo
import uuid

from aihub_gpu_runner.client import CoordinationError, JobClient, KnownJobFailure
from aihub_gpu_runner.core import NativeLock


def toronto_day():
    return datetime.now(ZoneInfo('America/Toronto')).date().isoformat()


FALLBACK = 'A quiet day still counts as progress.'


class DailyQuotes:
    def __init__(self, store_factory, client_factory=None, day=toronto_day):
        self.store_factory, self.client_factory, self.day = store_factory, client_factory or JobClient.from_environment, day

    def get(self, payload_factory, validator):
        store = self.store_factory()
        lock = NativeLock(store.path('daily-quotes.lock'))
        if not lock.acquire(0):
            return self.fallback('quote_in_progress')
        try:
            state = store.read('daily-quotes.json', {'schema': 1, 'successful': [], 'pending': None})
            if (not isinstance(state, dict) or state.get('schema') != 1 or
                    not isinstance(state.get('successful'), list) or 'pending' not in state):
                raise CoordinationError('quote_state_unverified')
            for entry in state['successful']:
                if (not isinstance(entry, dict) or not isinstance(entry.get('day'), str) or
                        not isinstance(entry.get('quote'), str) or not validator(entry['quote'])):
                    raise CoordinationError('quote_state_unverified')
            today = self.day()
            for entry in reversed(state['successful']):
                if entry['day'] == today:
                    return {'quote': entry['quote'], 'status': 'cached', 'reason': None}
            pending = state['pending']
            client = self.client_factory()
            if pending is not None:
                # Read-only reconciliation of an interrupted operation. Never
                # POST again, even if a status lookup cannot find the old ID.
                if (not isinstance(pending, dict) or not isinstance(pending.get('request_id'), str)
                        or not isinstance(pending.get('payload'), dict) or not isinstance(pending.get('day'), str)):
                    raise CoordinationError('quote_state_unverified')
                try:
                    status = client.status(pending['request_id'])
                    from aihub_gpu_runner.core import encode
                    import hashlib
                    expected = hashlib.sha256(encode(dict(target=pending['target'], operation='generate',
                                                          payload=pending['payload']))).hexdigest()
                    if status.get('request_hash') != expected or status.get('target') != pending['target']:
                        return self.fallback('runner_uncertain')
                    if status.get('resources_released') is not True or status.get('state') not in ('success', 'failed', 'cancelled'):
                        return self.fallback('runner_uncertain')
                    if status['state'] != 'success':
                        state['pending'] = None
                        store.write('daily-quotes.json', state)
                        return self.fallback('runner_failed')
                    data = client.result(pending['request_id'])['value']
                except Exception:
                    return self.fallback('runner_uncertain')
            else:
                import os
                pending = {'day': today, 'request_id': JobClient.stage_id(uuid.uuid4().hex, 'daily-quote'),
                           'payload': payload_factory([e['quote'] for e in state['successful']][-10:]),
                           'target': os.environ.get('GPU_RUNNER_OLLAMA_TARGET', 'ollama')}
                state['pending'] = pending
                # Identity and exact payload must be durable BEFORE submission.
                store.write('daily-quotes.json', state)
                try:
                    data = client.run(pending['request_id'], pending['target'], 'generate', pending['payload'])
                except KnownJobFailure:
                    state['pending'] = None
                    store.write('daily-quotes.json', state)
                    return self.fallback('runner_failed')
                except Exception:
                    # Keep the intent forever until read-only evidence resolves
                    # it or an operator reviews it. A new day cannot bypass it.
                    return self.fallback('runner_uncertain')
            raw = data.get('response') if isinstance(data, dict) else None
            valid = (isinstance(raw, str) and '\n' not in raw.strip() and '\r' not in raw.strip() and
                     data.get('done') is True and data.get('done_reason') in (None, 'stop') and validator(raw.strip()))
            state['pending'] = None
            if valid:
                state['successful'].append({'day': pending['day'], 'quote': raw.strip()})
                state['successful'] = state['successful'][-10:]
            store.write('daily-quotes.json', state)
            if valid and pending['day'] == today:
                return {'quote': raw.strip(), 'status': 'success', 'reason': None}
            return self.fallback('invalid_model_output' if not valid else 'quote_day_rolled_over')
        finally:
            lock.close()

    @staticmethod
    def fallback(reason):
        return {'quote': FALLBACK, 'status': 'fallback', 'reason': reason}
