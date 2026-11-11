import ast
import concurrent.futures
import hashlib
import importlib
import json
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
from aihub_gpu_runner.client import CURRENT_OPERATION, CoordinationError, JobClient, KnownJobFailure
from aihub_gpu_runner.core import AtomicStore, encode
from standalone_text import StandaloneText, InvalidModelOutput
import gpu_runner
import pipeline_status
from daily_quotes import DailyQuotes

PAYLOAD = {'model': 'synthetic', 'prompt': 'private task', 'stream': False}
ANSWER = {'done': True, 'done_reason': 'stop', 'response': 'Formatted task\n\n\n\n\n\n+0 more'}
RECOVERED = 'operator_recovered_no_automatic_resubmission'


class Transport:
    """Actual deployed JobClient protocol, entirely synthetic transport."""
    def __init__(self):
        self.jobs, self.calls = {}, []
        self.state, self.error, self.answer = 'success', None, ANSWER
        self.lost_reply, self.lookup_error, self.before_submit = False, False, None

    def record(self, rid, payload=PAYLOAD, state='success', error=None):
        value = dict(request_id=rid, target='ollama', request_hash=StandaloneText.expected('ollama', payload),
                     state=state, resources_released=state in ('success', 'failed', 'cancelled'),
                     supervision_active=state not in ('success', 'failed', 'cancelled'), error_code=error)
        self.jobs[rid] = value
        return value

    def request(self, method, route, payload=None):
        self.calls.append((method, route, payload))
        if method == 'POST':
            if self.before_submit: self.before_submit()
            rid = payload['request_id']
            if rid not in self.jobs: self.record(rid, payload['payload'], self.state, self.error)
            if self.lost_reply:
                self.lost_reply = False
                raise OSError('private transport content')
            return 200, 'application/json', encode(self.jobs[rid])
        if self.lookup_error: raise OSError('unavailable')
        rid = route.split('/')[2]
        if rid not in self.jobs: return 404, 'application/json', b'{}'
        if route.endswith('/result'):
            return 200, 'application/json', encode({'value': self.answer, 'error_code': self.jobs[rid]['error_code']})
        return 200, 'application/json', encode(self.jobs[rid])

    @property
    def submissions(self): return [p['request_id'] for m, r, p in self.calls if m == 'POST']


def validate(data):
    if data.get('done') is not True or not data.get('response'):
        raise InvalidModelOutput('invalid')


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = AtomicStore(self.tmp.name)
        self.t = Transport()
        self.client = JobClient(self.t, wait_timeout=.05, poll_interval=.001)
        self.day = '2026-11-10'
        self.manager = StandaloneText(self.store, self.client, lambda: self.day)
        self.legacy = JobClient.stage_id('standalone-' + hashlib.sha256(encode(['todo-format', PAYLOAD])).hexdigest()[:40], 'todo-format')

    def run_job(self, payload=PAYLOAD):
        return self.manager.run('todo-format', 'ollama', payload, validate)

    def pointer(self):
        return next((Path(self.tmp.name) / 'standalone-text').glob('*.json'))

    def test_identical_payloads_separate_schedules_after_failure_receive_distinct_ids(self):
        self.t.state, self.t.error = 'failed', RECOVERED
        with self.assertRaises(KnownJobFailure): self.run_job()
        self.day = '2026-11-11'; self.t.state, self.t.error = 'success', None
        self.run_job()
        self.assertEqual(len(set(self.t.submissions)), 2)

    def test_terminal_failure_then_new_refresh_receives_new_identity(self):
        self.t.state, self.t.error = 'failed', RECOVERED
        with self.assertRaises(KnownJobFailure): self.run_job()
        old = self.pointer().read_bytes()
        receipts = list((Path(self.tmp.name) / 'standalone-text/receipts').glob('*'))
        original = receipts[0].read_bytes()
        self.t.state, self.t.error = 'success', None
        self.run_job()
        self.assertEqual(len(set(self.t.submissions)), 2)
        self.assertEqual(receipts[0].read_bytes(), original)
        self.assertNotEqual(self.pointer().read_bytes(), old)

    def test_cached_success_survives_restart_without_inference(self):
        self.assertEqual(self.run_job(), ANSWER)
        self.day = '2026-11-11'  # Unchanged content does not need new inference.
        self.manager = StandaloneText(AtomicStore(self.tmp.name), self.client, lambda: self.day)
        self.assertEqual(self.run_job(), ANSWER)
        self.assertEqual(len(self.t.submissions), 1)

    def test_concurrent_refresh_does_not_submit_second_job(self):
        entered, release = threading.Event(), threading.Event()
        self.t.before_submit = lambda: (entered.set(), release.wait(5))
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            future = pool.submit(self.run_job)
            self.assertTrue(entered.wait(2))
            try:
                with self.assertRaisesRegex(CoordinationError, 'in_progress'): self.run_job()
            finally: release.set()
            self.assertEqual(future.result(), ANSWER)
        self.assertEqual(len(self.t.submissions), 1)

    def test_identity_and_payload_are_durable_before_post(self):
        def inspect():
            record = json.loads(self.pointer().read_bytes())
            self.assertEqual(record['state'], 'pending')
            self.assertEqual(record['payload'], PAYLOAD)
            self.assertEqual(record['request_id'], self.t.submissions[0])
        self.t.before_submit = inspect
        self.run_job()

    def test_interruption_before_submission_never_posts_on_resume_even_if_absent(self):
        with patch.object(self.client, 'run', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): self.run_job()
        original = self.pointer().read_bytes()
        for payload in (PAYLOAD, dict(PAYLOAD, prompt='changed')):
            with self.assertRaises(CoordinationError): self.run_job(payload)
        self.assertEqual(self.pointer().read_bytes(), original)
        self.assertFalse(self.t.submissions)

    def test_process_death_before_submission_retains_identity(self):
        def child():
            self.client.run = lambda *a, **k: os._exit(23)
            self.run_job()
        process = multiprocessing.get_context('fork').Process(target=child)
        process.start(); process.join(5)
        self.assertEqual(process.exitcode, 23)
        original = self.pointer().read_bytes()
        with self.assertRaises(CoordinationError): self.run_job()
        self.assertEqual(self.pointer().read_bytes(), original)
        self.assertFalse(self.t.submissions)

    def test_intent_storage_failure_prevents_post(self):
        with patch.object(self.store, 'write', side_effect=OSError('disk')):
            with self.assertRaises(OSError): self.run_job()
        self.assertFalse(self.t.submissions)

    def test_uncertain_accepted_reply_reconciles_get_only(self):
        self.t.lost_reply = True
        with self.assertRaises(CoordinationError): self.run_job()
        rid = self.t.submissions[0]
        self.assertEqual(self.run_job(), ANSWER)
        self.assertEqual(self.t.submissions, [rid])

    def test_timeout_and_changed_day_cannot_bypass_unresolved_job(self):
        self.t.state = 'running'
        with self.assertRaises(CoordinationError): self.run_job()
        self.day = '2026-11-11'
        with self.assertRaises(CoordinationError): self.run_job(dict(PAYLOAD, prompt='changed'))
        self.assertEqual(len(self.t.submissions), 1)

    def test_lookup_transport_failure_preserves_pending(self):
        self.t.lost_reply = True
        with self.assertRaises(CoordinationError): self.run_job()
        old = self.pointer().read_bytes(); self.t.lookup_error = True
        with self.assertRaises(CoordinationError): self.run_job()
        self.assertEqual(old, self.pointer().read_bytes())
        self.assertEqual(len(self.t.submissions), 1)

    def test_corrupt_pending_identity_cannot_create_a_replacement(self):
        self.t.lost_reply = True
        with self.assertRaises(CoordinationError): self.run_job()
        record = json.loads(self.pointer().read_bytes())
        record['payload']['prompt'] = 'corrupted'
        self.pointer().write_text(json.dumps(record))
        with self.assertRaises(CoordinationError): self.run_job()
        self.assertEqual(len(self.t.submissions), 1)

    def test_recovered_pending_is_reported_once_then_new_schedule_can_submit(self):
        self.t.lost_reply = True
        with self.assertRaises(CoordinationError): self.run_job()
        self.t.record(self.t.submissions[0], state='failed', error=RECOVERED)
        with self.assertRaisesRegex(KnownJobFailure, RECOVERED): self.run_job()
        self.assertEqual(len(self.t.submissions), 1)
        self.run_job(); self.assertEqual(len(set(self.t.submissions)), 2)

    def test_historical_recovery_is_never_reposted_or_changed(self):
        self.t.record(self.legacy, state='failed', error=RECOVERED)
        original = encode(self.t.jobs[self.legacy])
        self.run_job()
        self.assertNotIn(self.legacy, self.t.submissions)
        self.assertEqual(encode(self.t.jobs[self.legacy]), original)

    def test_historical_success_is_cached_without_submission(self):
        self.t.record(self.legacy)
        self.assertEqual(self.run_job(), ANSWER)
        self.assertEqual(self.run_job(), ANSWER)
        self.assertFalse(self.t.submissions)

    def test_historical_unresolved_or_retained_or_mismatched_is_not_bypassed(self):
        for changes in ({'state': 'running'}, {'resources_released': False},
                        {'supervision_active': True}, {'request_hash': 'bad'}, {'target': 'wrong'}):
            with self.subTest(changes=changes):
                self.t.record(self.legacy).update(changes)
                with self.assertRaises(CoordinationError): self.run_job()
                self.assertFalse(self.t.submissions)

    def test_validation_failure_is_terminal_but_not_successfully_cached(self):
        self.t.answer = {'done': True, 'response': ''}
        with self.assertRaises(InvalidModelOutput): self.run_job()
        self.t.answer = ANSWER
        self.run_job()
        self.assertEqual(len(set(self.t.submissions)), 2)

    def test_changed_payload_cannot_consume_old_result_as_new(self):
        self.t.lost_reply = True
        with self.assertRaises(CoordinationError): self.run_job()
        changed = dict(PAYLOAD, prompt='different task')
        with self.assertRaisesRegex(CoordinationError, 'previous_input_settled'): self.run_job(changed)
        self.assertEqual(len(self.t.submissions), 1)
        self.run_job(changed)
        self.assertEqual(len(self.t.submissions), 2)

    def test_failed_terminal_storage_does_not_rotate_identity(self):
        write = self.store.write
        def fail_receipt(name, value):
            if '/receipts/' in name: raise OSError('disk')
            write(name, value)
        with patch.object(self.store, 'write', side_effect=fail_receipt):
            with self.assertRaises(OSError): self.run_job()
        rid = self.t.submissions[0]
        self.assertEqual(self.run_job(), ANSWER)
        self.assertEqual(self.t.submissions, [rid])

    def test_adapter_current_operation_identity_is_unchanged(self):
        class Operation:
            request_id = 'existing-operation'
            def snapshot(self, step, payload): return payload
        token = CURRENT_OPERATION.set(Operation())
        try:
            with patch.object(JobClient, 'from_environment') as factory:
                factory.return_value.run.return_value = ANSWER
                gpu_runner.generate(PAYLOAD, 'todo-format', validate)
                self.assertEqual(factory.return_value.run.call_args.args[0], JobClient.stage_id('existing-operation', 'todo-format'))
        finally: CURRENT_OPERATION.reset(token)


class ReportingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for name in ('HA_URL', 'OLLAMA_URL', 'COMFY_URL'): os.environ[name] = 'http://offline.invalid'
        for name in ('HA_TOKEN', 'CALENDAR_ENTITY'): os.environ[name] = 'synthetic'
        cls.main = importlib.import_module('main')

    def setUp(self):
        self.main.TODO_AI_CACHE.update(hash=None, day=None, lines=[], reason=None, runner_error_code=None)
        self.items = [{'summary': 'Original task', 'description': 'Original details'}]

    def test_known_failure_endpoint_retains_reason_and_safe_code_in_display_cache(self):
        with patch.object(self.main, '_todo_items_open_sorted', return_value=self.items), \
             patch.object(self.main, 'todo_display_rows', side_effect=lambda lines: (lines, None)), \
             patch.object(self.main, 'runner_generate', side_effect=KnownJobFailure('runner_job_failed_' + RECOVERED)):
            result = self.main.todo_ai_refresh()
        self.assertTrue(result['fallback'])
        self.assertEqual(result['reason'], 'runner_failed')
        self.assertEqual(result['runner_error_code'], RECOVERED)
        self.assertIn('Original details', result['lines'][0])
        self.assertIsNone(self.main.TODO_AI_CACHE['hash'])
        self.assertEqual(self.main.TODO_AI_CACHE['reason'], 'runner_failed')

    def test_cached_fallback_render_pipeline_preserves_safe_classification(self):
        # Execute the exact production cached-ToDo status branch without GPU/render IO.
        tree = ast.parse((ROOT / 'app/main.py').read_text())
        render = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'render_wallpaper')
        branch = next(n for n in ast.walk(render) if isinstance(n, ast.If) and ast.unparse(n.test) == 'ai_lines')
        class Operation:
            record = {}
            def update_details(self, **fields): self.record['details'] = fields
        op = Operation(); token = CURRENT_OPERATION.set(op)
        try:
            exec(compile(ast.Module(body=[branch.body[0]], type_ignores=[]), '<render-status>', 'exec'),
                 dict(stage_event=pipeline_status.stage_event, todo_cached=False, todo_reason='runner_failed', todo_error_code=RECOVERED))
        finally: CURRENT_OPERATION.reset(token)
        summary = pipeline_status.pipeline_summary(op.record)
        self.assertEqual(summary['pipeline_status'], 'degraded')
        self.assertEqual(summary['stages']['todo'], dict(status='fallback', reason='runner_failed', runner_error_code=RECOVERED))

    def test_only_validation_failures_get_invalid_model_output(self):
        cases = [(InvalidModelOutput('bad'), 'invalid_model_output'),
                 (RuntimeError('private detail'), 'service_unavailable'),
                 (KnownJobFailure('runner_job_failed_private detail'), 'runner_failed')]
        for exc, reason in cases:
            with patch.object(self.main, 'runner_generate', side_effect=exc):
                result = self.main.ollama_format_todo(self.items)
            self.assertEqual(result['reason'], reason)
            self.assertIsNone(result['runner_error_code'])
            self.assertNotIn('private detail', json.dumps(result))

    def test_actual_response_validation_failure_is_classified(self):
        with patch.object(self.main, 'runner_generate', return_value={'done': True, 'response': ''}):
            self.assertEqual(self.main.ollama_format_todo(self.items)['reason'], 'invalid_model_output')

    def test_full_endpoint_uses_new_id_after_legacy_retirement_then_caches_success(self):
        with tempfile.TemporaryDirectory() as root:
            t = Transport(); client = JobClient(t)
            # Capture the actual formatter payload without calling any backend.
            with patch.object(self.main, 'runner_generate', return_value=ANSWER) as mocked:
                self.main.ollama_format_todo(self.items)
            payload = mocked.call_args.args[0]
            legacy = JobClient.stage_id('standalone-' + hashlib.sha256(encode(['todo-format', payload])).hexdigest()[:40], 'todo-format')
            t.record(legacy, payload, 'failed', RECOVERED)
            with patch.dict(os.environ, {'GPU_RUNNER_CLIENT_STATE': root}), \
                 patch.object(JobClient, 'from_environment', return_value=client), \
                 patch.object(self.main, '_todo_items_open_sorted', return_value=self.items), \
                 patch.object(self.main, 'todo_display_rows', side_effect=lambda lines: (lines, None)):
                first = self.main.todo_ai_refresh()
                second = self.main.todo_ai_refresh()
            self.assertFalse(first['fallback']); self.assertTrue(second['cached'])
            self.assertEqual(len(t.submissions), 1); self.assertNotEqual(t.submissions[0], legacy)

    def test_concurrent_endpoint_refuses_second_refresh_until_cache_commit(self):
        entered, release = threading.Event(), threading.Event()
        def formatting(items):
            entered.set(); release.wait(5)
            return dict(lines=['task'], summary_line='+0 more')
        with patch.object(self.main, '_todo_items_open_sorted', return_value=self.items), \
             patch.object(self.main, 'todo_display_rows', side_effect=lambda lines: (lines, None)), \
             patch.object(self.main, 'ollama_format_todo', side_effect=formatting) as formatter, \
             concurrent.futures.ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.main.todo_ai_refresh)
            self.assertTrue(entered.wait(2))
            try:
                with self.assertRaises(self.main.HTTPException) as error: self.main.todo_ai_refresh()
                self.assertEqual(error.exception.status_code, 409)
            finally: release.set()
            first.result()
            self.assertTrue(self.main.todo_ai_refresh()['cached'])
            self.assertEqual(formatter.call_count, 1)

    def test_successful_endpoint_cache_skips_formatting_and_preserves_quote_path(self):
        with patch.object(self.main, '_todo_items_open_sorted', return_value=self.items), \
             patch.object(self.main, 'ollama_daily_quote', return_value='Cached quote') as quote, \
             patch.object(self.main, 'runner_generate', return_value=ANSWER) as generate:
            first = self.main.todo_ai_refresh(); second = self.main.todo_ai_refresh()
        self.assertFalse(first['fallback']); self.assertTrue(second['cached'])
        self.assertEqual(generate.call_count, 1)
        self.assertIn('Cached quote', second['lines'])
        self.assertEqual(quote.call_count, 2)

    def test_existing_daily_quote_persistent_cache_unchanged(self):
        with tempfile.TemporaryDirectory() as root:
            store = AtomicStore(root); t = Transport(); t.answer = dict(ANSWER, response='A quiet day still counts as progress.')
            client = JobClient(t)
            quote = DailyQuotes(lambda: store, lambda: client, lambda: '2026-11-10')
            first = quote.get(lambda recent: PAYLOAD, lambda text: len(text) > 10)
            second = quote.get(lambda recent: PAYLOAD, lambda text: len(text) > 10)
            self.assertEqual(first['status'], 'success'); self.assertEqual(second['status'], 'cached')
            self.assertEqual(len(t.submissions), 1)


if __name__ == '__main__': unittest.main()
