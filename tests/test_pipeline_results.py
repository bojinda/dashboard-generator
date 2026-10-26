"""Actual compositor + durable output records with only mocked I/O/backends."""
import asyncio
from contextlib import ExitStack
import errno
import io
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from fastapi import BackgroundTasks
from fastapi.testclient import TestClient
from PIL import Image
from aihub_gpu_runner.client import KnownJobFailure, PendingJob, CURRENT_OPERATION
from recovery_status import RecoveryStatus
import test_runner_adapter as fixture

WEATHER = 'WEATHER OVERLAY:\nSky: clear\nAir: dry air\nGround: gravel\nLanternLight: warm glow'
SEASON = 'SEASONAL ENVIRONMENT:\nTrees: amber leaves\nGround: fallen leaves\nAir: cool\nDistantDetails: hills'


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.DashboardInterfaceTests('runTest'); self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.main = self.fixture.main
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.generate = self.stack.enter_context(patch.object(self.main, 'runner_generate', side_effect=self.valid))
        self.stack.enter_context(patch.object(self.main, 'ha_get_state', return_value={'state': 'sunny', 'attributes': {'temperature': 15}}))
        self.stack.enter_context(patch.object(self.main, 'ha_calendar_get_events_range', return_value={}))
        self.stack.enter_context(patch.object(self.main, 'ha_todo_get_items', return_value=[]))
        self.stack.enter_context(patch.object(self.main._RECOVERY_STATUS, 'read', return_value=RecoveryStatus(0, False, False)))
        self.ledger = self.stack.enter_context(patch.object(self.main, '_daily_quotes'))
        self.ledger.return_value.get.return_value = {'quote': 'A quiet day still counts as progress.', 'status': 'cached', 'reason': None}
        png = io.BytesIO(); Image.new('RGB', (64, 64), (100, 150, 190)).save(png, 'PNG')
        self.wallpaper = self.stack.enter_context(patch.object(self.main, 'runner_wallpaper', return_value=(png.getvalue(), 'image/png')))
        self.convert = self.stack.enter_context(patch.object(self.main, 'generate_epaper_bin_epdoptimize', return_value=960000))
        self.push = self.stack.enter_context(patch.object(self.main, 'push_bin_to_epd'))

    def valid(self, payload, step):
        return {'done': True, 'done_reason': 'stop', 'response': WEATHER if step.startswith('weather-') else SEASON}

    def async_run(self, request_id='pipeline'):
        tasks = BackgroundTasks()
        self.assertTrue(self.main.render_async(tasks, request_id)['queued'])
        asyncio.run(tasks())
        record = self.main._output_slot().operation_status(request_id)
        return record, self.main.render_status(request_id)['last']

    def test_normal_success_and_durable_stage_results_survive_process_restart(self):
        record, status = self.async_run()
        self.assertEqual(status['pipeline_status'], 'success')
        self.assertTrue(status['fully_successful']); self.assertTrue(status['wallpaper_generated'])
        self.assertEqual(status['stages']['weather']['status'], 'success')
        self.assertEqual(status['stages']['conversion']['status'], 'success')
        self.assertEqual(status['stages']['delivery']['status'], 'skipped')
        self.main._OUTPUT_SLOT = None
        self.assertEqual(self.main.render_status('pipeline')['last'], status)
        self.assertEqual(record['response']['pipeline_status'], 'success')

    def test_invalid_ai_output_is_degraded_and_no_private_output_is_exposed(self):
        self.generate.side_effect = None
        self.generate.return_value = {'done': True, 'done_reason': 'length', 'response': 'PRIVATE reasoning prompt'}
        record, status = self.async_run()
        self.assertEqual(status['pipeline_status'], 'degraded')
        self.assertFalse(status['fully_successful'])
        self.assertEqual(status['stages']['weather']['reason'], 'invalid_model_output')
        self.assertTrue(status['wallpaper_generated'])
        self.assertNotIn('PRIVATE', json.dumps(record['response']) + json.dumps(status))

    def test_october25_known_runner_failures_render_explicit_error_imagery_as_degraded(self):
        error = KnownJobFailure('runner_job_failed_resource_acquisition_timeout')
        self.generate.side_effect = error; self.wallpaper.side_effect = error
        record, status = self.async_run()
        self.assertEqual(status['pipeline_status'], 'degraded')
        self.assertFalse(status['wallpaper_generated'])
        for stage in ('weather', 'season', 'wallpaper'):
            self.assertEqual(status['stages'][stage]['reason'], 'runner_failed')
            self.assertEqual(status['stages'][stage]['runner_error_code'], 'resource_acquisition_timeout')
        self.assertEqual(self.generate.call_count, 2)  # No replay after definitive stage failure.
        self.wallpaper.assert_called_once(); self.convert.assert_called_once()

    def test_uncertain_weather_aborts_without_any_wallpaper_retry(self):
        self.generate.side_effect = PendingJob('stage-' + 'a' * 48, 'cleanup_pending')
        record, status = self.async_run()
        self.assertEqual(record['status'], 'error')
        self.assertEqual(status['pipeline_status'], 'uncertain')
        self.assertEqual(status['stages']['weather']['runner_state'], 'cleanup_pending')
        self.wallpaper.assert_not_called(); self.convert.assert_not_called()
        with self.assertRaises(Exception): self.main.render_async(BackgroundTasks(), 'pipeline')
        self.generate.assert_called_once()

    def test_comfy_failure_and_invalid_png_are_distinguished_from_success(self):
        self.wallpaper.return_value = b'not PNG PRIVATE', 'image/png'
        record, status = self.async_run()
        self.assertFalse(status['wallpaper_generated'])
        self.assertEqual(status['stages']['wallpaper']['reason'], 'invalid_model_output')
        self.assertEqual(status['pipeline_status'], 'degraded')
        self.assertNotIn('PRIVATE', json.dumps(status))

    def test_uncertain_comfy_ownership_stops_before_conversion_or_delivery(self):
        self.wallpaper.side_effect = PendingJob('stage-' + 'b' * 48, 'reconciling')
        _, status = self.async_run()
        self.assertEqual(status['pipeline_status'], 'uncertain')
        self.convert.assert_not_called(); self.push.assert_not_called()
        self.wallpaper.assert_called_once()

    def test_october26_epd_soft_failure_is_structured_durable_and_inspectable(self):
        self.main.EPD_PUSH_ENABLED = True
        self.push.side_effect = OSError(errno.EHOSTUNREACH, 'PRIVATE No route to host http://secret/token')
        record, status = self.async_run()
        self.assertEqual(record['status'], 'ok')  # Output-slot completed-ID compatibility.
        self.assertEqual(record['response']['epd_push'], 'failed_soft')
        self.assertEqual(status['pipeline_status'], 'degraded')
        self.assertTrue(status['wallpaper_generated'])
        self.assertEqual(status['stages']['conversion']['status'], 'success')
        self.assertEqual(status['stages']['delivery']['reason'], 'epd_route_unavailable')
        self.assertNotIn('PRIVATE', json.dumps(record['response']) + json.dumps(status))
        self.main._OUTPUT_SLOT = None
        repeated = self.main.render_async(BackgroundTasks(), 'pipeline')
        self.assertEqual(repeated, record['response'])
        self.wallpaper.assert_called_once(); self.push.assert_called_once()

    def test_delivery_hard_failure_and_binary_failure_have_separate_stages(self):
        self.convert.side_effect = RuntimeError('PRIVATE stderr')
        _, status = self.async_run()
        self.assertEqual(status['pipeline_status'], 'failed')
        self.assertEqual(status['stages']['conversion']['status'], 'failed')
        self.push.assert_not_called()
        self.assertNotIn('PRIVATE', json.dumps(status))

    def test_epd_hard_failure_keeps_successful_composition_and_conversion_evidence(self):
        self.main.EPD_PUSH_ENABLED = self.main.EPD_PUSH_FAIL_HARD = True
        self.push.side_effect = OSError(errno.EHOSTUNREACH, 'PRIVATE')
        record, status = self.async_run()
        self.assertEqual(record['status'], 'error')
        self.assertEqual(status['pipeline_status'], 'failed')
        self.assertEqual(status['stages']['composition']['status'], 'success')
        self.assertEqual(status['stages']['conversion']['status'], 'success')
        self.assertEqual(status['stages']['delivery']['reason'], 'epd_route_unavailable')

    def test_gpu_free_existing_image_paths_share_soft_delivery_reporting(self):
        self.main.EPD_PUSH_ENABLED = True
        self.push.side_effect = OSError(errno.EHOSTUNREACH, 'PRIVATE')
        Image.new('RGB', (32, 32)).save(self.main.OUT_PATH, 'PNG')
        response = self.main.render_test('existing-image')
        self.assertEqual(response['pipeline_status'], 'degraded')
        self.assertEqual(response['epd_push'], 'failed_soft')
        self.wallpaper.assert_not_called(); self.generate.assert_not_called()
        response = self.main.render_test_path(self.main.OUT_PATH, 'existing-path')
        self.assertEqual(response['pipeline_status'], 'degraded')
        self.assertEqual(response['source'], self.main.OUT_PATH)

    def test_quote_fallback_uncertainty_and_layout_do_not_displace_tasks(self):
        self.ledger.return_value.get.return_value = {'quote': 'A quiet day still counts as progress.', 'status': 'fallback', 'reason': 'runner_uncertain'}
        _, status = self.async_run()
        self.assertEqual(status['pipeline_status'], 'uncertain')
        self.assertEqual(status['stages']['quote']['reason'], 'runner_uncertain')
        for count in (0, 1, 3, 4, 6):
            rows = ['Valid task ' + str(i) for i in range(count)]
            display, index = self.main.todo_display_rows(rows)
            self.assertEqual(display[:count], rows)
            self.assertEqual(index, 4 if count <= 3 else None)

    def test_quote_validator_rejects_fragments_overlong_and_non_ascii_without_laundering(self):
        self.assertTrue(self.main.quote_is_usable('A quiet day still counts as progress.'))
        for text in ('A quiet day still counts as progress', 'A' * 100 + '.',
                     'A quiet day still counts as progress…', 'Sorry, let me generate another quote.'):
            self.assertFalse(self.main.quote_is_usable(text))

    def test_daily_quote_is_shared_by_refresh_and_render_without_task_formatting_on_render(self):
        self.main.TODO_AI_CACHE.update(hash='successful-task-format', day=self.main.toronto_day(),
            lines=['One task'], summary_line='')
        _, status = self.async_run()
        self.assertEqual(status['stages']['quote']['status'], 'cached')
        self.ledger.return_value.get.assert_called_once()
        self.assertTrue(all(c.args[1] != 'todo-format' for c in self.generate.call_args_list))

    def test_status_lookup_is_bounded_and_does_not_expose_legacy_exception(self):
        self.async_run()
        client = TestClient(self.main.app)
        self.assertEqual(client.get('/render_status?request_id=missing').status_code, 404)
        self.assertEqual(client.get('/render_status?request_id=../secret').status_code, 400)
        record = self.main._output_slot().status()
        record['error'] = 'PRIVATE legacy error'
        self.assertNotIn('PRIVATE', json.dumps(self.main._operation_status(record)))
