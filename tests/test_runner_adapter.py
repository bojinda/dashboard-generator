"""Dashboard integration: mock services, real Pillow/ASGI and native temp locks."""
import asyncio
import concurrent.futures
import importlib.util
import io
import json
import multiprocessing
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image, ImageChops, ImageDraw, ImageFont
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient

from aihub_gpu_runner.api import Application
from aihub_gpu_runner.backends import Target, OllamaBackend, ComfyBackend, Uncertain
from aihub_gpu_runner.client import JobClient, OutputSlot, OutputBusy, PendingJob, CoordinationError, KnownJobFailure, CURRENT_OPERATION
from aihub_gpu_runner.core import Admission, AtomicStore, NativeLock, Runner, encode

ROOT = Path(__file__).resolve().parents[1]
TOKEN = 'offline-synthetic-runner-token-000000000000'


class InProcessAPI:
    def __init__(self, application):
        self.application, self.calls, self.drop_once = application, [], False
    def request(self, method, route, payload=None):
        self.calls.append((method, route))
        response = self.application.handle(method, route, {'Authorization': 'Bearer ' + TOKEN},
                                          b'' if payload is None else encode(payload))
        if method == 'POST' and route == '/jobs' and self.drop_once:
            self.drop_once = False
            raise OSError('synthetic accepted reply lost')
        return response


class OllamaTransport:
    def __init__(self, reply='synthetic', cleanup_event=None, fail=False):
        self.reply, self.cleanup_event, self.fail = reply, cleanup_event, fail
        self.generations, self.unloads = 0, 0
        self.payloads = []
    def json(self, method, route, payload=None, timeout=5):
        if route == '/api/ps':
            return {'models': [] if self.cleanup_event is None or self.cleanup_event.is_set() else ['synthetic']}
        if payload.get('keep_alive') == 0:
            self.unloads += 1
            return {'done': True}
        self.generations += 1
        self.payloads.append(payload)
        if self.fail:
            raise Uncertain('synthetic_lost_backend_reply')
        return {'done': True, 'response': self.reply}


class ComfyTransport:
    def __init__(self, cleanup_event=None, error=False, partial=False):
        image = Image.new('RGB', (32, 32), (90, 140, 190))
        buffer = io.BytesIO()
        image.save(buffer, format='PNG')
        self.png = buffer.getvalue()
        self.cleanup_event, self.error, self.partial = cleanup_event, error, partial
        self.submissions, self.frees, self.history_reads, self.views = 0, 0, 0, []
    def json(self, method, route, payload=None, timeout=5):
        if route == '/queue':
            return {'queue_running': [], 'queue_pending': []}
        if route == '/prompt':
            self.submissions += 1
            self.payload = payload['prompt']
            return {'prompt_id': 'owned-prompt'}
        if route == '/free':
            self.frees += 1
            if self.cleanup_event is not None and not self.cleanup_event.wait(3):
                raise OSError('synthetic cleanup not confirmed')
            return {}
        if route == '/history/owned-prompt':
            self.history_reads += 1
            outputs = {'36': {'images': [{'filename': 'synthetic.png', 'subfolder': '', 'type': 'output'}]},
                       'unrelated': {'images': [{'filename': 'wrong.png', 'type': 'output'}]}}
            if self.error:
                return {'owned-prompt': {'status': {'status_str': 'error', 'completed': False}, 'outputs': outputs}}
            if self.partial and self.history_reads == 1:
                return {'owned-prompt': {'status': {'status_str': 'running', 'completed': False}, 'outputs': outputs}}
            return {'owned-prompt': {'status': {'status_str': 'success', 'completed': True}, 'outputs': outputs}}
        raise AssertionError('unexpected synthetic route')
    def binary(self, route, params, timeout=5):
        self.views.append(params)
        return self.png, 'image/png'
    def ack(self, method, route, payload=None, timeout=5):
        # Match the runner's existing HTTP acknowledgement cleanup contract.
        self.json(method, route, payload, timeout)


def output_contender(root, paths, output):
    try:
        operation = OutputSlot(root, paths).begin('render', 'second-process')
    except OutputBusy:
        output.put(False)
    else:
        output.put(True)
        operation.finish({'ok': True})


class RunnerAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='dashboard-adapter-')
        self.root = Path(self.temp.name)
        self.paths = {name: self.root / (name + '.lock') for name in ('gpu0', 'gpu1')}
        self.store = AtomicStore(self.root / 'runner-state')
        self.admission = Admission(self.store, self.paths, 'synthetic-dashboard-scope')
        self.admission.bootstrap({'approval_ref': 'SYNTHETIC ONLY', 'bootstrap_id': self.admission.snapshot()['bootstrap_id'],
                                  'backend_quiescent': True, 'cleanup_verified': True})
        self.workflow = {'7': {'class_type': 'Seed', 'inputs': {'seed': 1}},
                         '36': {'class_type': 'SaveImage', 'inputs': {'filename_prefix': 'synthetic'}}}
        self.targets = {'ollama': Target('ollama', 'ollama', ('gpu1',), 'http://127.0.0.1:1',
                                         models=('synthetic',), acquisition_timeout=.5, cleanup_timeout=2),
                        'wallpaper': Target('wallpaper', 'comfy', ('gpu0',), 'http://127.0.0.1:2',
                                            workflow=self.workflow, mutations={'7.seed': {'type': 'integer'}},
                                            output_node='36', idle_memory_mb=0, cleanup_samples=2,
                                            acquisition_timeout=.5, cleanup_timeout=.2)}
        self.runners, self.processes, self.events = [], [], []
    def tearDown(self):
        for event in self.events:
            event.set()
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=8)
        for runner in self.runners:
            runner.close()
        self.temp.cleanup()
    def make(self, ollama=None, comfy=None, probe=None):
        ollama, comfy = ollama or OllamaTransport(), comfy or ComfyTransport()
        runner = Runner(self.admission, self.targets,
                        {'ollama': OllamaBackend(self.targets['ollama'], ollama, poll_interval=.005),
                         'wallpaper': ComfyBackend(self.targets['wallpaper'], comfy,
                                                  probe=probe or (lambda: 0), poll_interval=.005)}, poll_interval=.005)
        self.runners.append(runner)
        transport = InProcessAPI(Application(runner, TOKEN))
        return runner, JobClient(transport, wait_timeout=1, poll_interval=.005), ollama, comfy, transport
    def generate(self, client, request_id='dashboard-generation'):
        return client.run(request_id, 'ollama', 'generate', {'model': 'synthetic', 'prompt': 'neutral'})
    def test_lost_accept_reply_reconnects_without_duplicate_generation(self):
        runner, client, ollama, _, transport = self.make()
        transport.drop_once = True
        with self.assertRaises(PendingJob):
            self.generate(client)
        self.assertEqual(self.generate(client)['response'], 'synthetic')
        self.assertEqual(ollama.generations, 1)
        self.assertEqual(ollama.unloads, 1)
    def test_same_id_different_payload_never_submits_a_new_job(self):
        _, client, ollama, *_ = self.make()
        self.generate(client)
        with self.assertRaises(CoordinationError):
            client.run('dashboard-generation', 'ollama', 'generate', {'model': 'synthetic', 'prompt': 'changed'})
        self.assertEqual(ollama.generations, 1)
    def test_comfy_partial_output_waits_for_own_terminal_success(self):
        _, client, _, comfy, _ = self.make(comfy=ComfyTransport(partial=True))
        data, media = client.run('wallpaper-success', 'wallpaper', 'workflow', self.workflow, artifact=True)
        self.assertEqual(media, 'image/png')
        self.assertEqual(Image.open(io.BytesIO(data)).mode, 'RGB')
        self.assertEqual(comfy.history_reads, 2)
        self.assertEqual(comfy.views[0]['filename'], 'synthetic.png')
        self.assertEqual(comfy.frees, 1)
        self.assertEqual(self.admission.snapshot()['owners'], {})
    def test_comfy_terminal_error_completed_false_cleans_before_failure(self):
        _, client, _, comfy, _ = self.make(comfy=ComfyTransport(error=True))
        with self.assertRaises(KnownJobFailure):
            client.run('wallpaper-error', 'wallpaper', 'workflow', self.workflow, artifact=True)
        self.assertEqual(comfy.views, [])
        self.assertEqual(comfy.submissions, 1)
        self.assertEqual(comfy.frees, 1)
        self.assertEqual(self.admission.snapshot()['owners'], {})
    def test_cleanup_pending_keeps_gpu0_reserved(self):
        observations = iter([0] + [100] * 100)
        _, client, _, comfy, _ = self.make(probe=lambda: next(observations))
        with self.assertRaises(PendingJob) as caught:
            client.run('pending-cleanup', 'wallpaper', 'workflow', self.workflow, artifact=True)
        self.assertEqual(caught.exception.state, 'cleanup_pending')
        self.assertIn('gpu0', self.admission.snapshot()['owners'])
        self.assertEqual(comfy.submissions, 1)
        self.assertIsNone(self.admission.try_acquire('meeting-after-uncertain', 'hash', ('gpu0',), 'host-whisperx'))
    def test_gpu1_cleanup_and_descriptor_release_precede_gpu0_submission(self):
        event = threading.Event()
        self.events.append(event)
        runner, client, _, comfy, _ = self.make(ollama=OllamaTransport(cleanup_event=event))
        finished = threading.Event()
        errors = []
        def work():
            try:
                self.generate(client)
                physical = NativeLock(self.paths['gpu1'])
                if not physical.acquire(0):
                    raise AssertionError('GPU1 descriptor still held')
                physical.close()
                client.run('wallpaper-after-summary', 'wallpaper', 'workflow', self.workflow, artifact=True)
            except BaseException as exc:
                errors.append(exc)
            finally:
                finished.set()
        thread = threading.Thread(target=work)
        thread.start()
        deadline = time.monotonic() + 1
        while 'gpu1' not in self.admission.snapshot()['owners'] and time.monotonic() < deadline:
            time.sleep(.005)
        time.sleep(.03)
        self.assertEqual(comfy.submissions, 0)
        event.set()
        self.assertTrue(finished.wait(2))
        thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(comfy.submissions, 1)
    def test_client_wait_expiry_does_not_cancel_supervision(self):
        event = threading.Event()
        self.events.append(event)
        runner, client, _, comfy, _ = self.make(comfy=ComfyTransport(cleanup_event=event))
        client.wait_timeout = .03
        with self.assertRaises(PendingJob):
            client.run('disconnect-wait', 'wallpaper', 'workflow', self.workflow, artifact=True)
        self.assertIn('gpu0', self.admission.snapshot()['owners'])
        event.set()
        client.wait_timeout = 1
        client.run('disconnect-wait', 'wallpaper', 'workflow', self.workflow, artifact=True)
        self.assertEqual(comfy.submissions, 1)
    @unittest.skipUnless(sys.platform.startswith('linux'), 'Native Linux host-stage contention')
    def test_dashboard_gpu0_waits_for_meeting_transcription_helper(self):
        self._meeting_contention('gpu0')
    @unittest.skipUnless(sys.platform.startswith('linux'), 'Native Linux host-stage contention')
    def test_dashboard_gpu1_waits_for_meeting_summary_helper(self):
        self._meeting_contention('gpu1')
    def _meeting_contention(self, resource):
        # Start a synthetic meeting helper against a real shared native journal.
        from aihub_gpu_runner.config import admission_from_config
        import hashlib
        for path in self.paths.values():
            path.touch(exist_ok=True)
        config = self.root / 'host.synthetic.json'
        raw_workflow = encode(self.workflow)
        template = self.root / 'workflow.synthetic.json'
        template.write_bytes(raw_workflow)
        config.write_text(json.dumps({'state_dir': str(self.root / 'host-state'),
            'lock_paths': {key: str(path) for key, path in self.paths.items()},
            'host_stages': {'whisperx': {'idle_memory_mb': 0, 'cleanup_samples': 2, 'cleanup_timeout': 1}},
            'targets': {'ollama': {'kind': 'ollama', 'resources': ['gpu1'], 'base_url': 'http://127.0.0.1:1',
                                   'models': ['synthetic']}}}))
        _, _, self.admission = admission_from_config(config)
        self.store = self.admission.store
        self.admission.bootstrap({'approval_ref': 'SYNTHETIC ONLY',
            'bootstrap_id': self.admission.snapshot()['bootstrap_id'], 'backend_quiescent': True, 'cleanup_verified': True})
        binary = self.root / 'probe'
        binary.mkdir()
        probe = binary / 'nvidia-smi'
        probe.write_text('#!' + sys.executable + '\nprint(0)\n')
        probe.chmod(0o755)
        ready, release = self.root / 'meeting-ready', self.root / 'meeting-release'
        code = f'import pathlib,time;pathlib.Path({str(ready)!r}).touch();\nwhile not pathlib.Path({str(release)!r}).exists():time.sleep(.005)'
        environment = dict(os.environ, AIHUB_GPU_RUNNER_CONFIG=str(config), AIHUB_GPU_RUNNER_PYTHON=sys.executable,
                           AIHUB_GPU_STAGE_ID='synthetic-meeting-stage', PATH=str(binary) + ':' + os.environ['PATH'])
        helper = Path(os.environ['MEETING_PIPELINE_SOURCE']) / 'bin/with-gpu-lock.sh'
        process = subprocess.Popen(['bash', str(helper), resource, 'synthetic meeting', sys.executable, '-c', code],
                                   env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.processes.append(process)
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail(str(process.communicate()))
            time.sleep(.005)
        self.assertTrue(ready.exists())
        _, client, ollama, comfy, _ = self.make()
        done, errors = threading.Event(), []
        def dashboard_work():
            try:
                if resource == 'gpu0':
                    client.run('dashboard-contention', 'wallpaper', 'workflow', self.workflow, artifact=True)
                else:
                    self.generate(client, 'dashboard-contention')
            except BaseException as exc:
                errors.append(exc)
            finally:
                done.set()
        thread = threading.Thread(target=dashboard_work)
        thread.start()
        time.sleep(.08)
        self.assertEqual(comfy.submissions if resource == 'gpu0' else ollama.generations, 0)
        release.touch()
        process.communicate(timeout=3)
        self.assertEqual(process.returncode, 0)
        self.assertTrue(done.wait(2))
        thread.join()
        self.assertEqual(errors, [])
    @unittest.skipUnless(sys.platform.startswith('linux'), 'Native process exclusion')
    def test_reusable_output_slot_excludes_another_process(self):
        paths = [str(self.root / 'wallpaper.png'), str(self.root / 'image_data.bin')]
        operation = OutputSlot(self.root / 'client-state', paths).begin('render', 'first')
        context = multiprocessing.get_context('spawn')
        output = context.Queue()
        child = context.Process(target=output_contender, args=(str(self.root / 'client-state'), paths, output))
        child.start()
        try:
            self.assertFalse(output.get(timeout=5))
            child.join(5)
            self.assertEqual(child.exitcode, 0)
        finally:
            if child.is_alive():
                child.terminate()
                child.join(5)
            operation.finish({'ok': True})
            output.close()
            output.join_thread()
    def test_interrupted_output_operation_never_replays_old_id(self):
        slot = OutputSlot(self.root / 'client-state', [self.root / 'wallpaper.png'])
        operation = slot.begin('render', 'interrupted')
        operation.lock.close()  # Synthetic abrupt app exit; no backend cleanup claim.
        operation.lock = None
        self.assertEqual(slot.status()['status'], 'error')
        with self.assertRaises(CoordinationError):
            slot.begin('render', 'interrupted')
    def test_invalid_client_deadlines_fail_closed(self):
        for value in (float('inf'), float('nan'), -1, 0):
            with self.subTest(value=value), self.assertRaises(CoordinationError):
                JobClient(None, wait_timeout=value)


class DashboardInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='dashboard-interface-')
        self.root = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {'HA_URL': 'http://synthetic.invalid', 'HA_TOKEN': 'synthetic-only',
            'CALENDAR_ENTITY': 'calendar.synthetic', 'OLLAMA_URL': 'http://synthetic.invalid', 'COMFY_URL': 'http://synthetic.invalid',
            'GPU_RUNNER_CLIENT_STATE': str(self.root / 'client-state'), 'OLLAMA_MODEL': 'synthetic'})
        self.environment.start()
        sys.path.insert(0, str(ROOT / 'app'))
        for name in ('config', 'gpu_runner', 'main'):
            sys.modules.pop(name, None)
        import main
        self.main = main
        main.OUT_PATH = str(self.root / 'wallpaper.png')
        main.COMFY_WORKFLOW_FILE = str(ROOT / 'workflows/picture_api.json')
        self.blocks = [patch('requests.get', side_effect=AssertionError('live HA/HTTP forbidden')),
                       patch('requests.post', side_effect=AssertionError('live HTTP/EPD forbidden')),
                       patch('subprocess.run', side_effect=AssertionError('live Node/EPD forbidden'))]
        for block in self.blocks:
            block.start()
    def tearDown(self):
        for block in self.blocks:
            block.stop()
        self.environment.stop()
        sys.path.pop(0)
        self.temp.cleanup()
    def test_async_trigger_reserves_before_background_task_is_started(self):
        tasks = BackgroundTasks()
        self.assertEqual(self.main.render_async(tasks, 'first-async'), {'ok': True, 'queued': True})
        self.assertEqual(self.main.render_async(BackgroundTasks(), 'second-async'),
                         {'ok': True, 'queued': False, 'status': 'busy'})
        self.assertEqual(self.main.render('sync-while-async')['status'], 'busy')
        self.assertEqual(self.main.render_test('epd-while-async')['status'], 'busy')
        operation = tasks.tasks[0].args[0]
        self.main._finish_output(operation, {'ok': True})
    def test_simultaneous_dashboard_triggers_schedule_exactly_one_writer(self):
        barrier = threading.Barrier(8)
        def trigger(number):
            tasks = BackgroundTasks()
            barrier.wait(2)
            return self.main.render_async(tasks, 'parallel-' + str(number)), tasks
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(trigger, range(8)))
        winners = [tasks for response, tasks in results if response.get('queued') is True]
        self.assertEqual(len(winners), 1)
        self.assertEqual(sum(response.get('status') == 'busy' for response, _ in results), 7)
        self.main._finish_output(winners[0].tasks[0].args[0], {'ok': True})
    def test_disconnect_then_reconnect_reuses_completed_async_response(self):
        tasks = BackgroundTasks()
        self.main.render_async(tasks, 'stable-async-id')  # Caller discards this accepted response.
        with (patch.object(self.main, 'render_wallpaper') as compose,
             patch.object(self.main, 'generate_epaper_bin_epdoptimize', return_value=960000),
             patch.object(self.main, 'push_bin_to_epd')):
            asyncio.run(tasks())
            repeat = BackgroundTasks()
            response = self.main.render_async(repeat, 'stable-async-id')
            self.assertEqual(response, {'ok': True, 'queued': False, 'status': 'ok'})
            self.assertEqual(repeat.tasks, [])
            self.assertEqual(compose.call_count, 1)
    def test_ha_routes_and_normal_sync_response_preserved(self):
        client = TestClient(self.main.app)
        self.assertEqual(client.get('/health').json(), {'ok': True})
        with (patch.object(self.main, 'render_wallpaper') as compose,
             patch.object(self.main, 'generate_epaper_bin_epdoptimize', return_value=960000)):
            response = client.post('/render', headers={'X-Request-ID': 'ha-render'})
            self.assertEqual(response.status_code, 200)
            value = response.json()
            self.assertEqual(value, {'ok': True, 'out': self.main.OUT_PATH,
                'bin': str(self.root / 'image_data.bin'), 'bin_bytes': 960000,
                'epd_push': 'disabled', 'weather_entity': self.main.WEATHER_ENTITY, 'ollama_model': 'synthetic'})
            self.assertEqual(client.post('/render', headers={'X-Request-ID': 'ha-render'}).json(), value)
            self.assertEqual(compose.call_count, 1)
        routes = {route.path for route in self.main.app.routes}
        self.assertTrue({'/render', '/render_async', '/render_status', '/render_test', '/render_test_path',
                         '/todo_ai_refresh', '/todo_generate_today', '/todo_debug', '/health'} <= routes)
        self.assertFalse(client.get('/render_status').json()['running'])
    def test_dashboard_thin_adapter_end_to_end_uses_named_runner_targets(self):
        import copy
        import main as dashboard
        store = AtomicStore(self.root / 'wire-runner-state')
        paths = {key: self.root / (key + '.wire.lock') for key in ('gpu0', 'gpu1')}
        admission = Admission(store, paths, 'synthetic-wire-scope')
        admission.bootstrap({'approval_ref': 'SYNTHETIC', 'bootstrap_id': admission.snapshot()['bootstrap_id'],
                             'backend_quiescent': True, 'cleanup_verified': True})
        workflow = json.loads((ROOT / 'workflows/picture_api.json').read_text())
        settings = json.loads(Path(os.environ['DASHBOARD_POLICY_EXAMPLE']).read_text())
        mutations = copy.deepcopy(settings['mutations'])
        mutations['9.image']['values'] = [dashboard.COMFY_INPUT_IMAGE]
        targets = {'ollama': Target('ollama', 'ollama', ('gpu1',), 'http://127.0.0.1:1', models=('synthetic',)),
                   'wallpaper': Target('wallpaper', 'comfy', ('gpu0',), 'http://127.0.0.1:2',
                                       workflow=workflow, mutations=mutations, output_node='36',
                                       idle_memory_mb=0, cleanup_samples=2)}
        ollama, comfy = OllamaTransport(reply='synthetic accepted text'), ComfyTransport()
        runner = Runner(admission, targets,
                        {'ollama': OllamaBackend(targets['ollama'], ollama),
                         'wallpaper': ComfyBackend(targets['wallpaper'], comfy, probe=lambda: 0, poll_interval=.005)},
                        poll_interval=.005)
        job_client = JobClient(InProcessAPI(Application(runner, TOKEN)), wait_timeout=2, poll_interval=.005)
        try:
            with (patch.object(JobClient, 'from_environment', return_value=job_client),
                  patch.object(dashboard, 'ha_get_state', return_value={'state': 'sunny', 'attributes': {'temperature': 15}}),
                  patch.object(dashboard, 'ha_calendar_get_events_range', return_value={}),
                  patch.object(dashboard, 'ha_todo_get_items', return_value=[]),
                  patch.object(dashboard, 'overlay_is_valid', return_value=True),
                  patch.object(dashboard, 'season_is_valid', return_value=True),
                  patch.object(dashboard, 'generate_epaper_bin_epdoptimize', return_value=960000)):
                result = dashboard.render('full-adapter-operation')
                self.assertTrue(result['ok'])
                self.assertEqual(dashboard.render('full-adapter-operation'), result)
            self.assertEqual((ollama.generations, ollama.unloads, comfy.submissions, comfy.frees), (3, 3, 1, 1))  # Weather, season and independently owned daily quote.
            self.assertEqual(admission.snapshot()['owners'], {})
            image = Image.open(dashboard.OUT_PATH)
            self.assertEqual((image.format, image.mode, image.size), ('PNG', 'RGB', (1600, 1200)))
        finally:
            runner.close()
    def test_standalone_todo_and_quote_use_runner_payloads_and_keep_fallbacks(self):
        values = []
        def generate(payload, step):
            values.append((payload, step))
            if step == 'todo-format':
                return {'response': 'one\ntwo\n\n\n\n\n+0 more — done'}
            return {'done': True, 'response': 'A quiet day still counts as progress.'}
        from daily_quotes import DailyQuotes
        from types import SimpleNamespace
        ledger = DailyQuotes(lambda: self.main._output_slot().store,
            lambda: SimpleNamespace(run=lambda rid, target, op, payload: generate(payload, 'daily-quote')))
        with patch.object(self.main, 'runner_generate', side_effect=generate), patch.object(self.main, '_daily_quotes', return_value=ledger):
            self.main.ollama_format_todo([{'summary': 'synthetic task'}])
            self.assertEqual(self.main.ollama_daily_quote(), 'A quiet day still counts as progress.')
        self.assertEqual([step for _, step in values][0], 'todo-format')
        self.assertTrue(all(payload['model'] == 'synthetic' and payload['stream'] is False for payload, _ in values))
        with patch.object(self.main, 'runner_generate', side_effect=KnownJobFailure('known-failure')):
            fallback = self.main.ollama_format_todo([{'summary': 'preserved fallback'}])
            self.assertEqual(fallback['lines'][0], 'preserved fallback')
    def test_pending_ollama_does_not_start_wallpaper_or_unload_outside_runner(self):
        with (patch.object(self.main, 'runner_generate', side_effect=PendingJob('pending', 'cleanup_pending')),
             patch.object(self.main, 'runner_wallpaper') as wallpaper,
             patch.object(self.main, 'ha_get_state', return_value={'state': 'sunny', 'attributes': {}})):
            with self.assertRaises(PendingJob):
                self.main.render_wallpaper(self.main.OUT_PATH)
            wallpaper.assert_not_called()
        self.assertFalse(hasattr(self.main, 'ollama_unload_model'))
    def test_workflow_mutations_and_png_decode_preserved(self):
        buffer = io.BytesIO()
        Image.new('RGB', (1056, 808), 'navy').save(buffer, 'PNG')
        captured = []
        with patch.object(self.main, 'runner_wallpaper', side_effect=lambda payload: (captured.append(payload) or buffer.getvalue(), 'image/png')):
            image = self.main.comfy_render_image('unchanged prompt', 1056, 808)
        self.assertEqual((image.mode, image.size), ('RGB', (1056, 808)))
        workflow = captured[0]
        self.assertEqual(workflow['88']['inputs']['value'], 'unchanged prompt')
        self.assertEqual(workflow['9']['inputs']['image'], self.main.COMFY_INPUT_IMAGE)
        self.assertEqual(workflow['40']['inputs']['proportional_width'], 1056)
        self.assertEqual(workflow['40']['inputs']['proportional_height'], 808)
        self.assertEqual(workflow['35']['inputs']['value'], 1056)
        self.assertEqual(workflow['40']['inputs']['background_color'], '#F5F5F5')
    def test_real_wallpaper_graph_satisfies_reviewed_server_mutation_policy(self):
        import copy
        template = json.loads((ROOT / 'workflows/picture_api.json').read_text())
        policy = json.loads(Path(os.environ['DASHBOARD_POLICY_EXAMPLE']).read_text())
        mutations = copy.deepcopy(policy['mutations'])
        mutations['9.image']['values'] = [self.main.COMFY_INPUT_IMAGE]
        target = Target('wallpaper', 'comfy', ('gpu0',), 'http://127.0.0.1:1', workflow=template,
                        mutations=mutations, output_node='36', idle_memory_mb=0)
        captured = []
        buffer = io.BytesIO()
        Image.new('RGB', (1056, 808)).save(buffer, 'PNG')
        with patch.object(self.main, 'runner_wallpaper', side_effect=lambda payload: (captured.append(payload) or buffer.getvalue(), 'image/png')):
            self.main.comfy_render_image('synthetic prompt', 1056, 808)
        ComfyBackend(target, None, probe=lambda: 0).validate('workflow', captured[0])
        captured[0]['36']['inputs']['filename_prefix'] = 'unapproved-output'
        with self.assertRaises(ValueError):
            ComfyBackend(target, None, probe=lambda: 0).validate('workflow', captured[0])
    def test_original_composition_pixels_and_format_unchanged(self):
        spec = importlib.util.spec_from_file_location('dashboard_original', os.environ['DASHBOARD_ORIGINAL_MAIN'])
        original = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(original)
        image = Image.new('RGB', (1056, 808), (80, 100, 160))
        weather = {'state': 'sunny', 'attributes': {'temperature': 15, 'temperature_unit': '°C'}}
        for module, output in ((original, self.root / 'original.png'), (self.main, self.root / 'wallpaper.png')):
            with (patch.object(module, 'ha_get_state', return_value=weather),
                 patch.object(module, 'ollama_generate_overlay', return_value='synthetic overlay'),
                 patch.object(module, 'ollama_generate_season_block', return_value='synthetic season'),
                 patch.object(module, 'comfy_render_image', return_value=image),
                 patch.object(module, 'ha_calendar_get_events_range', return_value={}),
                 patch.object(module, 'ha_todo_get_items', return_value=[{'summary': 'Task ' + str(i)} for i in range(6)])):
                module.render_wallpaper(str(output))
        a, b = Image.open(self.root / 'original.png'), Image.open(self.root / 'wallpaper.png')
        self.assertEqual((b.format, b.mode, b.size), ('PNG', 'RGB', (1600, 1200)))
        # The intentional new footer indicator is the only changed area when
        # no tasks require shortening. Calendar/picture/title/task pixels match.
        from recovery_status import RecoveryStatus
        indicator = self.main.recovery_indicator_layout(ImageDraw.Draw(b), (15, 870, 1585, 1185), RecoveryStatus())
        changed = ImageChops.difference(a, b).getbbox()
        left, top, right, bottom = indicator['bounds']
        self.assertIsNotNone(changed)
        self.assertTrue(left <= changed[0] <= changed[2] <= right)
        self.assertTrue(top <= changed[1] <= changed[3] <= bottom)
    def test_epd_soft_failure_and_existing_paths_preserved(self):
        self.main.EPD_PUSH_ENABLED = True
        with (patch.object(self.main, 'render_wallpaper'),
             patch.object(self.main, 'generate_epaper_bin_epdoptimize', return_value=960000),
             patch.object(self.main, 'push_bin_to_epd', side_effect=RuntimeError('synthetic EPD unavailable'))):
            response = self.main.render('soft-epd')
        self.assertEqual(response['epd_push'], 'failed_soft')
        self.assertEqual(response['out'], self.main.OUT_PATH)
        self.assertEqual(response['bin'], str(self.root / 'image_data.bin'))
    def test_epd_hard_failure_retains_existing_http_error_contract(self):
        self.main.EPD_PUSH_ENABLED = self.main.EPD_PUSH_FAIL_HARD = True
        with (patch.object(self.main, 'render_wallpaper'),
              patch.object(self.main, 'generate_epaper_bin_epdoptimize', return_value=960000),
              patch.object(self.main, 'push_bin_to_epd', side_effect=RuntimeError('synthetic EPD unavailable'))):
            response = TestClient(self.main.app).post('/render', headers={'X-Request-ID': 'hard-epd'})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()['detail'], 'EPD push failed: synthetic EPD unavailable')
        self.assertEqual(self.main.render_status()['last']['status'], 'error')

if __name__ == '__main__':
    unittest.main()
