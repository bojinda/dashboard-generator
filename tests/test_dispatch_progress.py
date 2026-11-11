"""Desired outcomes using the actual client SDK/store and synthetic GET replies.

Temporary files are real; no GPU observations, backend work or production calls.
Run with the installed SDK, or PYTHONPATH=compatibility/client-sdk for the pinned
client compatibility lane. The application module is imported from app/.
"""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
from media_dispatch import DispatchBlocked, DispatchOutputSlot
from aihub_gpu_runner.core import encode


class SyntheticGET:
    def __init__(self):
        self.calls, self.jobs, self.owners = [], {}, {}
    def request(self, method, route):
        assert method == 'GET'
        self.calls.append(route)
        if route == '/resources':
            code, value = 200, {'ready': True, 'owners': self.owners}
        else:
            value = self.jobs.get(route.removeprefix('/jobs/'))
            code = 200 if value is not None else 404
        return code, 'application/json', json.dumps(value or {}).encode()


class DispatchProgress(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.http = SyntheticGET()
        self.slot = self.restart()

    def restart(self):
        return DispatchOutputSlot(Path(self.tmp.name) / 'state', [Path(self.tmp.name) / 'output'],
                                  transport=self.http, clock=lambda: 10000)

    def add(self, number, *, settled=False, old_scope=False):
        job, operation, request_hash = 'stage-' + f'{number:048x}', f'{number:032x}', f'{number:064x}'
        record = {'request_id': operation, 'scope': 'other-scope' if old_scope else self.slot.scope,
                  'status': 'error', 'started': 1, 'finished': 2,
                  'runner_jobs': {job: {'target': 'ollama', 'request_hash': request_hash, 'settled': settled}}}
        path = self.slot.store.path('operations/' + operation + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encode(record))
        self.http.jobs[job] = {'request_id': job, 'request_hash': request_hash, 'target': 'ollama',
                               'state': 'failed', 'resources_released': True, 'supervision_active': False}
        return job, path

    def receipts(self):
        return list(self.slot.store.path('dispatch-reconciliations').glob('*.json'))

    def test_nine_plus_references_make_durable_progress_without_history_rewrite(self):
        originals = {}
        for n in range(1, 26):
            _, path = self.add(n)
            originals[path] = path.read_bytes()
        for expected in (8, 16, 24):
            with self.assertRaises(DispatchBlocked) as caught:
                self.slot.reconcile_runner_jobs()
            self.assertEqual(caught.exception.reason, 'output_reconciliation_bound_exceeded')
            self.assertFalse(caught.exception.operator_required)
            self.assertEqual(len(self.receipts()), expected)
            self.slot = self.restart()
        self.slot.reconcile_runner_jobs()
        self.assertEqual(len(self.receipts()), 25)
        self.assertEqual(sum(p.startswith('/jobs/') for p in self.http.calls), 25)
        self.http.calls.clear()
        self.slot = self.restart()
        self.slot.reconcile_runner_jobs()
        self.assertEqual(self.http.calls, ['/resources', '/resources'])
        self.assertTrue(all(path.read_bytes() == value for path, value in originals.items()))

    def test_more_than_five_thousand_records_and_old_scopes_do_not_size_block(self):
        for n in range(1, 5102):
            self.add(n, settled=True)
        for n in range(10000, 10101):
            self.add(n, settled=True, old_scope=True)
        self.slot.reconcile_runner_jobs()
        self.slot = self.restart()
        self.slot.reconcile_runner_jobs()
        self.assertEqual(self.http.calls, ['/resources', '/resources'] * 2)

    def test_tampered_receipt_is_refused(self):
        self.add(1)
        self.slot.reconcile_runner_jobs()
        receipt = self.receipts()[0]
        value = json.loads(receipt.read_bytes())
        value['proof']['resources_released'] = False
        receipt.write_bytes(encode(value))
        with self.assertRaises(DispatchBlocked) as caught:
            self.restart().reconcile_runner_jobs()
        self.assertEqual(caught.exception.reason, 'runner_job_identity_mismatch')

    def test_changed_original_is_not_silently_covered_by_old_receipt(self):
        _, path = self.add(1)
        self.slot.reconcile_runner_jobs()
        value = json.loads(path.read_bytes())
        value['finished'] += 1
        path.write_bytes(encode(value))
        with self.assertRaises(DispatchBlocked) as caught:
            self.restart().reconcile_runner_jobs()
        self.assertEqual(caught.exception.reason, 'runner_job_identity_mismatch')

    def test_identity_mismatch_and_unresolved_native_state_still_block(self):
        job, _ = self.add(1)
        for change, reason in [({'request_hash': 'f' * 64}, 'runner_job_identity_mismatch'),
                               ({'state': 'reconciling', 'resources_released': False}, 'previous_runner_job_unresolved')]:
            old = dict(self.http.jobs[job])
            self.http.jobs[job].update(change)
            with self.assertRaises(DispatchBlocked) as caught:
                self.slot.reconcile_runner_jobs()
            self.assertEqual(caught.exception.reason, reason)
            self.http.jobs[job] = old

    def test_current_owner_is_checked_even_when_historical_receipt_is_valid(self):
        job, _ = self.add(1)
        self.slot.reconcile_runner_jobs()
        self.http.owners = {'gpu1': {'job_id': job}}
        self.http.jobs[job].update(state='reconciling', resources_released=False)
        with self.assertRaises(DispatchBlocked) as caught:
            self.restart().reconcile_runner_jobs()
        self.assertEqual(caught.exception.reason, 'runner_owner_reconciling')

    def test_receipt_cannot_skip_same_job_currently_reported_as_active_owner(self):
        job, _ = self.add(1)
        self.slot.reconcile_runner_jobs()
        self.http.owners = {'gpu1': {'job_id': job}}
        self.http.jobs[job].update(state='running', resources_released=False, supervision_active=True)
        with self.assertRaises(DispatchBlocked) as caught:
            self.restart().reconcile_runner_jobs()
        self.assertEqual(caught.exception.reason, 'previous_runner_job_unresolved')

    def test_receipt_survives_interruption_before_health_write(self):
        self.add(1)
        write = self.slot.store.write
        def fail_health(name, value):
            if name == 'dispatch-health.json':
                raise OSError('synthetic interrupted health persistence')
            return write(name, value)
        with patch.object(self.slot.store, 'write', side_effect=fail_health):
            with self.assertRaises(DispatchBlocked):
                self.slot.reconcile_runner_jobs()
        self.assertEqual(len(self.receipts()), 1)
        self.http.calls.clear()
        self.restart().reconcile_runner_jobs()
        self.assertEqual(self.http.calls, ['/resources', '/resources'])

    def test_failed_receipt_write_cannot_create_false_progress(self):
        self.add(1)
        write = self.slot.store.write
        def fail_receipt(name, value):
            if name.startswith('dispatch-reconciliations/'):
                raise OSError('synthetic interrupted receipt persistence')
            return write(name, value)
        with patch.object(self.slot.store, 'write', side_effect=fail_receipt):
            with self.assertRaises(DispatchBlocked):
                self.slot.reconcile_runner_jobs()
        self.assertEqual(self.receipts(), [])
        self.restart().reconcile_runner_jobs()
        self.assertEqual(len(self.receipts()), 1)
        self.assertEqual(sum(route.startswith('/jobs/') for route in self.http.calls), 2)

    def test_absence_proof_stays_two_gets_and_never_replays(self):
        job, path = self.add(1)
        self.http.jobs.pop(job)
        original = path.read_bytes()
        self.slot.reconcile_runner_jobs()
        self.assertEqual(self.http.calls.count('/jobs/' + job), 2)
        self.http.calls.clear()
        self.restart().reconcile_runner_jobs()
        self.assertEqual(self.http.calls.count('/jobs/' + job), 2)
        self.assertEqual(path.read_bytes(), original)

    def test_historical_absence_cannot_hide_later_waiting_job_without_owner(self):
        job, _ = self.add(1)
        status = self.http.jobs.pop(job)
        self.slot.reconcile_runner_jobs()
        self.http.jobs[job] = dict(status, state='waiting', resources_released=False, supervision_active=True)
        self.assertEqual(self.http.owners, {})
        with self.assertRaises(DispatchBlocked) as caught:
            self.restart().reconcile_runner_jobs()
        self.assertEqual(caught.exception.reason, 'previous_runner_job_unresolved')

    def test_historical_absence_cannot_hide_later_identity_mismatch(self):
        job, _ = self.add(1)
        status = self.http.jobs.pop(job)
        self.slot.reconcile_runner_jobs()
        self.http.jobs[job] = dict(status, request_hash='f' * 64)
        with self.assertRaises(DispatchBlocked) as caught:
            self.restart().reconcile_runner_jobs()
        self.assertEqual(caught.exception.reason, 'runner_job_identity_mismatch')

    def test_later_exact_terminal_job_adds_positive_receipt_without_replay(self):
        job, path = self.add(1)
        status = self.http.jobs.pop(job)
        original = path.read_bytes()
        self.slot.reconcile_runner_jobs()
        self.http.jobs[job] = status
        self.restart().reconcile_runner_jobs()
        self.assertEqual(len(self.receipts()), 2)
        self.http.calls.clear()
        self.restart().reconcile_runner_jobs()
        self.assertEqual(self.http.calls, ['/resources', '/resources'])
        self.assertEqual(path.read_bytes(), original)

    def test_absence_proof_rejects_recent_or_owned_request(self):
        job, path = self.add(1)
        self.http.jobs.pop(job)
        value = json.loads(path.read_bytes()); value['finished'] = 9999
        path.write_bytes(encode(value))
        with self.assertRaises(DispatchBlocked) as caught:
            self.slot.reconcile_runner_jobs()
        self.assertEqual(caught.exception.reason, 'runner_absence_not_yet_stable')


if __name__ == '__main__':
    unittest.main()
