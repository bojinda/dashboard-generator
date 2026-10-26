"""Exact display acknowledgment. Runner access is authenticated GET only."""
from datetime import datetime, timedelta, timezone
import re
import threading

from aihub_gpu_runner.core import NativeLock, strict_json

TOKEN = re.compile(r'^[0-9a-f]{64}$')


def validate_inventory(value, now):
    from recovery_status import validate_summary, FIELDS
    if not isinstance(value, dict) or set(value) != FIELDS | {'incidents'}:
        raise ValueError('invalid_incident_inventory')
    validate_summary({k: value[k] for k in FIELDS}, now)
    incidents = value['incidents']
    if not isinstance(incidents, list) or len(incidents) > 4096 or len(incidents) != value['incidents_7d']:
        raise ValueError('invalid_incident_inventory')
    seen, total = set(), 0
    snapshot = datetime.fromisoformat(value['snapshot_at'])
    for entry in incidents:
        if (not isinstance(entry, dict) or set(entry) != {'id', 'first_seen', 'aliases'} or
                not isinstance(entry['id'], str) or not TOKEN.fullmatch(entry['id']) or entry['id'] in seen or
                not isinstance(entry['aliases'], list) or not entry['aliases'] or
                any(not isinstance(a, str) or not TOKEN.fullmatch(a) for a in entry['aliases']) or
                len(entry['aliases']) != len(set(entry['aliases'])) or entry['id'] not in entry['aliases']):
            raise ValueError('invalid_incident_inventory')
        date = datetime.fromisoformat(entry['first_seen'])
        if date.tzinfo is None or not snapshot - timedelta(days=7) <= date <= snapshot:
            raise ValueError('invalid_incident_time')
        seen.add(entry['id']); total += len(entry['aliases'])
    if total > 8192:
        raise ValueError('invalid_incident_inventory')
    return value


class RecoveryAcknowledgment:
    def __init__(self, store_factory, transport_factory, clock=lambda: datetime.now(timezone.utc)):
        self.store_factory, self.transport_factory, self.clock = store_factory, transport_factory, clock
        self.mutex = threading.Lock()
        self.tokens, self.available = None, True

    @staticmethod
    def tokens_from(state):
        if state is None: return None
        if (not isinstance(state, dict) or set(state) != {'schema', 'acknowledged_at', 'aliases'} or
                state['schema'] != 1 or not isinstance(state['aliases'], list) or len(state['aliases']) > 65536 or
                any(not isinstance(a, str) or not TOKEN.fullmatch(a) for a in state['aliases'])):
            raise ValueError('invalid_acknowledgment_state')
        stamp = datetime.fromisoformat(state['acknowledged_at'])
        if stamp.tzinfo is None: raise ValueError('invalid_acknowledgment_state')
        return set(state['aliases'])

    def reload(self):
        try:
            store = self.store_factory()
            with NativeLock(store.path('recovery-ack.lock')).held(timeout=.1):
                tokens = self.tokens_from(store.read('recovery-ack.json'))
                with self.mutex: self.tokens, self.available = tokens, True
        except Exception:
            with self.mutex: self.available = False

    def fetch(self):
        status, media, data = self.transport_factory().request('GET', '/recovery-incidents')
        if status != 200 or media != 'application/json' or len(data) > 1024 * 1024:
            raise ValueError('runner_incident_inventory_required')
        return validate_inventory(strict_json(data), self.clock())

    def acknowledge(self):
        inventory = self.fetch()  # Always fresh; never acknowledge stale cache.
        now = self.clock()
        if now - datetime.fromisoformat(inventory['snapshot_at']) > timedelta(seconds=60):
            raise ValueError('fresh_incident_inventory_required')
        store = self.store_factory()
        with NativeLock(store.path('recovery-ack.lock')).held(timeout=1):
            tokens = self.tokens_from(store.read('recovery-ack.json')) or set()
            tokens.update(a for entry in inventory['incidents'] for a in entry['aliases'])
            if len(tokens) > 65536: raise ValueError('acknowledgment_capacity_exceeded')
            store.write('recovery-ack.json', {'schema': 1, 'acknowledged_at': now.isoformat(), 'aliases': sorted(tokens)})
            with self.mutex: self.tokens, self.available = tokens, True
        return {'ok': True, 'acknowledged': inventory['incidents_7d'],
                'operator_required': inventory['operator_required'], 'snapshot_at': inventory['snapshot_at']}

    def displayed(self, inventory, aggregate_count):
        with self.mutex:
            if not self.available: return None
            if inventory is None: return aggregate_count if self.tokens is None else None
            cutoff = self.clock() - timedelta(days=7)
            return sum(datetime.fromisoformat(i['first_seen']) >= cutoff and
                       not (self.tokens or set()).intersection(i['aliases']) for i in inventory['incidents'])
