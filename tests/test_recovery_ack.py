"""Exact incident sets, private durable acknowledgment and protected HA endpoint."""
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from recovery_ack import RecoveryAcknowledgment, validate_inventory
from recovery_status import RecoveryStatusReader
from aihub_gpu_runner.core import AtomicStore, encode
from fastapi.testclient import TestClient
import test_runner_adapter as fixture

NOW = datetime(2026, 10, 26, 16, tzinfo=timezone.utc)
def token(name): return hashlib.sha256(name.encode()).hexdigest()
def inventory(names=('old', 'recent'), attention=True, at=NOW):
    return {'schema_version': 1, 'incidents_7d': len(names), 'operator_required': attention,
            'snapshot_at': at.isoformat(), 'incidents': [
                {'id': token(n), 'aliases': [token(n), token(n + '-lease')],
                 'first_seen': (NOW - timedelta(days=6 if n == 'old' else 1 if n == 'recent' else 0)).isoformat()}
                for n in names]}


class AcknowledgmentTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.store = AtomicStore(tmp.name); self.now = [NOW]
        self.transport = Mock()
        self.transport.request.return_value = (200, 'application/json', encode(inventory()))
        self.ack = RecoveryAcknowledgment(lambda: self.store, lambda: self.transport, lambda: self.now[0])

    def test_ack_expiry_new_incidents_and_restart_use_sets_not_subtraction(self):
        self.assertEqual(self.ack.acknowledge()['acknowledged'], 2)
        self.assertEqual(self.ack.displayed(inventory(), 2), 0)
        self.now[0] += timedelta(days=2)
        later = inventory(('recent', 'new'), at=self.now[0])
        restarted = RecoveryAcknowledgment(lambda: self.store, lambda: self.transport, lambda: self.now[0])
        restarted.reload()
        self.assertEqual(restarted.displayed(later, 2), 1)
        self.assertIsNone(restarted.displayed(None, 2))  # Never approximate on old runner.

    def test_window_expiry_works_even_before_next_remote_poll(self):
        self.ack.reload(); self.now[0] += timedelta(days=2)
        self.assertEqual(self.ack.displayed(inventory(), 2), 1)

    def test_reclassification_alias_is_not_counted_as_new(self):
        self.ack.acknowledge()
        later = inventory(('recent',))
        later['incidents'][0]['id'] = token('boot')
        later['incidents'][0]['aliases'].append(token('boot'))
        self.assertEqual(self.ack.displayed(later, 1), 0)

    def test_repeated_ack_preserves_previous_aliases(self):
        self.ack.acknowledge()
        self.transport.request.return_value = 200, 'application/json', encode(inventory(('new',)))
        self.ack.acknowledge()
        self.assertEqual(self.ack.displayed(inventory(('old', 'new')), 2), 0)

    def test_unavailable_old_or_stale_inventory_cannot_be_acknowledged(self):
        for response in ((404, 'application/json', b'{}'),
                         (200, 'application/json', encode(inventory(at=NOW - timedelta(minutes=5))))):
            self.transport.request.return_value = response
            with self.assertRaises(ValueError): self.ack.acknowledge()
            self.assertIsNone(self.store.read('recovery-ack.json'))

    def test_invalid_partial_and_private_inventory_is_rejected(self):
        for bad in (dict(inventory(), incidents=[]), dict(inventory(), token='PRIVATE'),
                    dict(inventory(), incidents_7d=True)):
            with self.assertRaises(ValueError): validate_inventory(bad, NOW)

    def test_reader_preserves_attention_after_ack_and_cached_restart(self):
        self.ack.acknowledge()
        reader = RecoveryStatusReader(lambda: self.store, lambda: self.transport,
            clock=lambda: NOW, acknowledgment=self.ack)
        reader.read(); reader.thread.join(1)
        status = reader.read(); reader.thread.join(1)
        self.assertEqual(status.count, 0)
        self.assertTrue(status.operator_required)
        self.assertFalse(status.stale)
        self.assertEqual(self.store.read('recovery-summary.json')['incidents_7d'], 2)
        self.assertTrue(all(c.args == ('GET', '/recovery-incidents') for c in self.transport.request.call_args_list))

    def test_corrupt_or_uncertain_storage_never_becomes_a_zero_count(self):
        self.store.write('recovery-ack.json', {'broken': True})
        self.ack.reload()
        self.assertIsNone(self.ack.displayed(inventory(), 2))
        with self.assertRaises(ValueError): self.ack.acknowledge()


class ProtectedEndpointTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.DashboardInterfaceTests('runTest'); self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.main = self.fixture.main
        self.client = TestClient(self.main.app)
        self.secret = 'synthetic-test-only-acknowledgment-token-0123456789_ABC'
        self.env = patch.dict('os.environ', {'RECOVERY_ACK_TOKEN': self.secret})
        self.env.start(); self.addCleanup(self.env.stop)

    def test_endpoint_rejects_unauthenticated_without_fetching_or_gpu_work(self):
        with patch.object(self.main._RECOVERY_ACK, 'acknowledge') as ack:
            for headers in ({}, {'Authorization': 'Bearer wrong'}):
                response = self.client.post('/recovery-counter/acknowledge', headers=headers)
                self.assertEqual(response.status_code, 401)
            ack.assert_not_called()

    def test_success_is_ack_only_and_token_is_never_in_response(self):
        with patch.object(self.main._RECOVERY_ACK, 'acknowledge', return_value={'ok': True, 'acknowledged': 2, 'operator_required': True}) as ack, \
                patch.object(self.main, 'runner_generate', side_effect=AssertionError('GPU')), \
                patch.object(self.main, 'render_wallpaper', side_effect=AssertionError('render')):
            response = self.client.post('/recovery-counter/acknowledge', headers={'Authorization': 'Bearer ' + self.secret})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()['operator_required'])
            self.assertNotIn(self.secret, response.text)
            ack.assert_called_once()

    def test_missing_dedicated_secret_and_missing_runner_dependency_are_explicit(self):
        with patch.dict('os.environ', {'RECOVERY_ACK_TOKEN': ''}):
            self.assertEqual(self.client.post('/recovery-counter/acknowledge').status_code, 503)
        with patch.object(self.main._RECOVERY_ACK, 'acknowledge', side_effect=RuntimeError('PRIVATE')):
            response = self.client.post('/recovery-counter/acknowledge', headers={'Authorization': 'Bearer ' + self.secret})
            self.assertEqual(response.status_code, 503)
            self.assertNotIn('PRIVATE', response.text)
