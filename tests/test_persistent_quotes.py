"""Durable quote identity, validation and concurrency without GPU requests."""
import hashlib
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from daily_quotes import DailyQuotes, toronto_day
from aihub_gpu_runner.client import KnownJobFailure, PendingJob, CoordinationError
from aihub_gpu_runner.core import AtomicStore, encode

QUOTE = 'Small steps still carry you toward the horizon.'


class QuoteTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.store = AtomicStore(tmp.name)
        self.day = ['2026-10-26']
        self.client = Mock()
        self.client.run.return_value = {'done': True, 'response': QUOTE}
        self.payload = Mock(return_value={'model': 'synthetic', 'prompt': 'PRIVATE'})
        self.validator = lambda text: 20 <= len(text) <= 80 and text.isascii()

    def ledger(self):
        return DailyQuotes(lambda: self.store, lambda: self.client, lambda: self.day[0])

    def get(self): return self.ledger().get(self.payload, self.validator)

    def test_success_restores_after_recreation_and_history_is_success_only(self):
        self.assertEqual(self.get()['status'], 'success')
        self.assertEqual(self.get(), {'quote': QUOTE, 'status': 'cached', 'reason': None})
        self.client.run.assert_called_once()
        state = self.store.read('daily-quotes.json')
        self.assertEqual(state['successful'], [{'day': self.day[0], 'quote': QUOTE}])
        self.assertIsNone(state['pending'])

    def test_invalid_results_fallback_then_next_intent_uses_new_identity(self):
        for data in ({'done': True, 'response': ''}, {'done': False, 'response': QUOTE},
                     {'done': True, 'done_reason': 'length', 'response': QUOTE},
                     {'done': True, 'done_reason': 'error', 'response': QUOTE},
                     {'done': True, 'response': QUOTE + '\nextra'}, {'response': QUOTE}):
            with self.subTest(data=data):
                self.client.run.return_value = data
                self.assertEqual(self.get()['reason'], 'invalid_model_output')
                self.assertEqual(self.store.read('daily-quotes.json')['successful'], [])
        ids = [c.args[0] for c in self.client.run.call_args_list]
        self.assertEqual(len(ids), len(set(ids)))
        self.client.run.return_value = {'done': True, 'response': QUOTE}
        self.assertEqual(self.get()['status'], 'success')

    def test_released_failure_permits_next_render_but_uncertainty_never_posts_again(self):
        self.client.run.side_effect = KnownJobFailure('PRIVATE')
        self.assertEqual(self.get()['reason'], 'runner_failed')
        self.client.run.side_effect = PendingJob('synthetic', 'cleanup_pending')
        self.assertEqual(self.get()['reason'], 'runner_uncertain')
        count = self.client.run.call_count
        self.client.status.side_effect = OSError('PRIVATE')
        self.day[0] = '2026-10-27'
        self.assertEqual(self.get()['reason'], 'runner_uncertain')
        self.assertEqual(self.client.run.call_count, count)
        self.assertIsNotNone(self.store.read('daily-quotes.json')['pending'])

    def test_interrupted_success_is_read_only_reconciled_with_exact_identity(self):
        self.client.run.side_effect = PendingJob('synthetic', 'transport_uncertain')
        self.get()
        pending = self.store.read('daily-quotes.json')['pending']
        request_hash = hashlib.sha256(encode(dict(target=pending['target'], operation='generate', payload=pending['payload']))).hexdigest()
        self.client.status.return_value = dict(state='success', resources_released=True,
            target=pending['target'], request_hash=request_hash)
        self.client.result.return_value = {'value': {'done': True, 'response': QUOTE}}
        self.assertEqual(self.get()['status'], 'success')
        self.client.run.assert_called_once()
        self.client.status.assert_called_with(pending['request_id'])

    def test_unreleased_success_wrong_binding_and_missing_job_remain_blocked(self):
        self.client.run.side_effect = PendingJob('synthetic', 'reconciling'); self.get()
        for response in ({'state': 'success', 'resources_released': False},
                         {'state': 'success', 'resources_released': True, 'target': 'wrong', 'request_hash': 'wrong'}):
            self.client.status.return_value = response
            self.assertEqual(self.get()['reason'], 'runner_uncertain')
        self.client.status.side_effect = CoordinationError('runner_request_rejected_404')
        self.assertEqual(self.get()['reason'], 'runner_uncertain')
        self.client.run.assert_called_once()

    def test_concurrent_process_style_ledger_instances_generate_one_success(self):
        entered, release = threading.Event(), threading.Event()
        def run(*args):
            entered.set(); self.assertTrue(release.wait(2))
            return {'done': True, 'response': QUOTE}
        self.client.run.side_effect = run
        result = []
        thread = threading.Thread(target=lambda: result.append(self.get()))
        thread.start(); self.assertTrue(entered.wait(1))
        try:
            self.assertEqual(self.get()['reason'], 'quote_in_progress')
        finally:
            release.set(); thread.join(2)
        self.assertEqual(self.get()['status'], 'cached')
        self.client.run.assert_called_once()

    def test_day_rollover_and_recent_success_history(self):
        self.get(); self.day[0] = '2026-10-27'; self.get()
        self.assertEqual(self.client.run.call_count, 2)
        self.payload.assert_called_with([QUOTE])
        self.assertEqual(len(self.store.read('daily-quotes.json')['successful']), 2)

    def test_intent_is_persisted_before_submission_and_storage_failure_blocks_gpu(self):
        def run(request_id, *args):
            self.assertEqual(self.store.read('daily-quotes.json')['pending']['request_id'], request_id)
            return {'done': True, 'response': QUOTE}
        self.client.run.side_effect = run
        with patch.object(self.store, 'write', side_effect=OSError('disk uncertain')):
            with self.assertRaises(OSError): self.get()
        self.client.run.assert_not_called()
        self.get()

    def test_corrupt_cache_fails_closed_and_toronto_day_is_explicit(self):
        self.store.write('daily-quotes.json', {'unexpected': 'state'})
        with self.assertRaises(CoordinationError): self.get()
        self.client.run.assert_not_called()
        with patch('daily_quotes.datetime') as clock:
            clock.now.side_effect = lambda tz: datetime(2026, 10, 27, 2, tzinfo=timezone.utc).astimezone(tz)
            self.assertEqual(toronto_day(), '2026-10-26')
