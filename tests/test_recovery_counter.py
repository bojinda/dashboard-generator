"""Offline cache, real PIL renderer, synthetic tasks and EPD palette regression."""
import ast
import contextlib
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw, ImageFont
from aihub_gpu_runner.core import AtomicStore, encode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
from recovery_status import RecoveryStatus, RecoveryStatusReader, environment_transport

NOW = datetime(2026, 10, 24, 16, tzinfo=timezone.utc)


def aggregate(count=3, attention=False, at=NOW):
    return {'schema_version': 1, 'incidents_7d': count, 'operator_required': attention,
            'snapshot_at': at.isoformat()}


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = AtomicStore(self.tmp.name)
        self.clock, self.monotonic = [NOW], [0]
        self.transport = Mock()
        self.transport.request.return_value = (200, 'application/json', encode(aggregate()))
    def reader(self, store=None):
        return RecoveryStatusReader(lambda: store or self.store, lambda: self.transport,
            clock=lambda: self.clock[0], monotonic=lambda: self.monotonic[0])
    def settle(self, reader):
        result = reader.read()
        reader.thread.join(1)
        return reader.read(0)
    def test_valid_summary_persists_only_aggregate_and_polling_is_cached(self):
        reader = self.reader()
        self.assertEqual(self.settle(reader), RecoveryStatus(3, False, False))
        self.assertEqual(self.store.read('recovery-summary.json'), aggregate())
        for _ in range(5):
            reader.read(0)
        self.transport.request.assert_called_once_with('GET', '/recovery-summary')
    def test_last_aggregate_survives_restart_and_failure_marks_it_stale(self):
        self.settle(self.reader())
        self.transport.request.side_effect = OSError('offline')
        self.assertEqual(self.settle(self.reader()), RecoveryStatus(3, False, True))
        self.assertEqual(self.store.read('recovery-summary.json'), aggregate())
    def test_no_successful_summary_displays_unknown_instead_of_zero(self):
        self.transport.request.side_effect = OSError('offline')
        self.assertEqual(self.settle(self.reader()), RecoveryStatus())
        self.assertIsNone(self.store.read('recovery-summary.json'))
    def test_expired_snapshot_and_failed_refresh_are_stale(self):
        reader = self.reader()
        self.settle(reader)
        self.clock[0] += timedelta(minutes=16)
        self.monotonic[0] += 61
        self.transport.request.side_effect = OSError('offline')
        self.assertEqual(self.settle(reader), RecoveryStatus(3, False, True))
    def test_invalid_private_extra_fields_bad_dates_and_auth_failure_do_not_replace_cache(self):
        self.store.write('recovery-summary.json', aggregate())
        for payload in (dict(aggregate(), token='must-not-persist'), dict(aggregate(), incidents_7d=True),
                        dict(aggregate(), snapshot_at='2026-10-24'),
                        dict(aggregate(), snapshot_at=(NOW + timedelta(days=1)).isoformat())):
            self.transport.request.return_value = (200, 'application/json', encode(payload))
            self.assertEqual(self.settle(self.reader()), RecoveryStatus(3, False, True))
            self.assertEqual(self.store.read('recovery-summary.json'), aggregate())
        self.transport.request.return_value = (401, 'application/json', b'{}')
        self.assertEqual(self.settle(self.reader()), RecoveryStatus(3, False, True))
    def test_slow_lookup_is_bounded_and_has_only_one_inflight_worker(self):
        entered, finish = threading.Event(), threading.Event()
        self.addCleanup(finish.set)
        def slow(*args):
            entered.set()
            if not finish.wait(3):
                raise OSError('test not released')
            return 200, 'application/json', encode(aggregate())
        self.transport.request.side_effect = slow
        reader = self.reader()
        start = time.monotonic()
        self.assertEqual(reader.read(.02), RecoveryStatus())
        self.assertLess(time.monotonic() - start, .25)
        self.assertTrue(entered.wait(1))
        self.monotonic[0] = 100
        for _ in range(5):
            reader.read(0)
        self.assertEqual(self.transport.request.call_count, 1)
        finish.set()
        reader.thread.join(1)
    def test_failed_storage_never_fails_a_valid_remote_result(self):
        store = Mock()
        store.read.side_effect = OSError('disk offline')
        store.write.side_effect = OSError('disk offline')
        self.assertEqual(self.settle(self.reader(store)), RecoveryStatus(3, False, False))
    def test_existing_connection_credentials_and_short_timeout_are_reused(self):
        with patch.dict('os.environ', {'GPU_RUNNER_URL': 'http://offline.invalid', 'GPU_RUNNER_TOKEN': 'x' * 40}), \
                patch('recovery_status.APITransport') as transport:
            environment_transport()
            transport.assert_called_once_with('http://offline.invalid', 'x' * 40, timeout=.4)


def rendering_functions(status=RecoveryStatus(3, False, False), ai=True):
    """Execute unchanged production function bodies, without app startup or I/O."""
    names = {'load_font', 'recovery_indicator_layout', 'todo_text_width', 'draw_recovery_indicator',
             'render_wallpaper', 'draw_text_bold', 'draw_multiline', 'wrap_text', 'draw_wrapped_lines',
             'clamp_to_width', 'is_effectively_blank_line', 'looks_like_address_line', 'parse_task_key_and_text'}
    tree = ast.parse((ROOT / 'app/main.py').read_text(encoding='utf-8'))
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    ns = {'Image': Image, 'ImageDraw': ImageDraw, 'ImageFont': ImageFont, 'datetime': datetime,
          'timedelta': timedelta, 're': re, 'TODO_AI_LOCK': threading.Lock(), 'BASE_IMAGE_PROMPT': 'synthetic',
          'WEATHER_ENTITY': 'weather.synthetic', 'CALENDAR_ENTITY': 'calendar.synthetic', 'TODO_ENTITY': 'todo.synthetic',
          'CoordinationError': RuntimeError, '_RECOVERY_STATUS': SimpleNamespace(read=Mock(return_value=status)),
          'TODO_AI_CACHE': {'lines': ['Synthetic task ' + str(i) for i in range(6)] if ai else [],
                            'summary_line': '+9 more — ' + 'A long task summary continues here ' * 12},
          'ha_get_state': Mock(return_value={}), 'format_weather_for_display': Mock(return_value='Clear'),
          'infer_day_or_night_from_weather': Mock(return_value='day'), 'infer_season': Mock(return_value='autumn'),
          'ollama_generate_overlay': Mock(return_value='synthetic overlay'),
          'ollama_generate_season_block': Mock(return_value='synthetic season'),
          'extract_clothing_weather_inputs': Mock(return_value=(10, 'none', 'none', False)),
          'choose_clothing_profile': Mock(return_value={}), 'build_clothing_block': Mock(return_value='synthetic'),
          'build_final_prompt': Mock(return_value='synthetic'), 'clean_for_json': lambda x: x,
          'comfy_render_image': lambda prompt, width, height: Image.new('RGB', (width, height), (220, 220, 220)),
          'ha_calendar_get_events_range': Mock(return_value={}),
          'ha_todo_get_items': Mock(return_value=[{'summary': 'A' * 500} for _ in range(15)])}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(ROOT / 'app/main.py'), 'exec'), ns)
    return ns


class RenderingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
    def render(self, ns):
        path = Path(self.tmp.name) / 'synthetic.png'
        with contextlib.redirect_stdout(io.StringIO()):
            ns['render_wallpaper'](str(path))
        return Image.open(path).copy()
    def test_states_font_arrow_support_padding_and_muted_grayscale(self):
        ns = rendering_functions()
        panel = (15, 870, 1585, 1185)
        draw = ImageDraw.Draw(Image.new('RGB', (1600, 1200)))
        for status, expected in ((RecoveryStatus(3, False, False), '↻ 3'),
                                 (RecoveryStatus(3, True, False), '↻ 3 !'),
                                 (RecoveryStatus(3, True, True), '↻ 3 ?'), (RecoveryStatus(), '↻ - ?')):
            item = ns['recovery_indicator_layout'](draw, panel, status)
            self.assertEqual(item['text'], expected)  # DejaVuSans really supports ↻.
            self.assertEqual(item['font'].size, 19)
            self.assertEqual(item['bounds'][2:], (1559, 1159))
            self.assertGreater(item['bounds'][0], 1400)
        image = self.render(ns)
        item = ns['recovery_indicator_layout'](ImageDraw.Draw(image), panel, RecoveryStatus(3, False, False))
        colors = image.crop(tuple(int(v) for v in item['bounds'])).getdata()
        self.assertTrue(any(96 <= min(c) < 160 for c in colors))
        self.assertTrue(all(r == g == b for r, g, b in colors))
    def test_missing_arrow_uses_unobtrusive_ascii_fallback(self):
        ns = rendering_functions()
        original = ns['load_font'](19)
        class MissingArrowFont:
            def __getattr__(self, name):
                return getattr(original, name)
            def getmask(self, text):
                return original.getmask('\u0378' if text == '↻' else text)
        ns['load_font'] = lambda size: MissingArrowFont()
        item = ns['recovery_indicator_layout'](ImageDraw.Draw(Image.new('RGB', (1600, 1200))),
                                               (15, 870, 1585, 1185), RecoveryStatus())
        self.assertEqual(item['text'], '~ - ?')
    def test_ai_long_summary_and_raw_tasks_do_not_collide_with_indicator(self):
        original = ImageDraw.ImageDraw.text
        for ai in (True, False):
            ns, rendered = rendering_functions(ai=ai), []
            def record(draw, xy, text, *args, **kwargs):
                font = kwargs.get('font')
                if xy[1] >= 900:
                    rendered.append((text, draw.textbbox(xy, text, font=font,
                                                       stroke_width=kwargs.get('stroke_width', 0))))
                return original(draw, xy, text, *args, **kwargs)
            with patch.object(ImageDraw.ImageDraw, 'text', new=record):
                image = self.render(ns)
            indicator = ns['recovery_indicator_layout'](ImageDraw.Draw(image), (15, 870, 1585, 1185),
                                                        RecoveryStatus(3, False, False))
            left, top, right, bottom = indicator['bounds']
            for text, bbox in rendered:
                if text != indicator['text'] and bbox[3] > top and bbox[1] < bottom:
                    self.assertLessEqual(bbox[2], left - 16)
            self.assertTrue(any(text.startswith('+') for text, _ in rendered))
            self.assertEqual(image.size, (1600, 1200))
    def test_indicator_retains_ink_after_repository_epd_palette_conversion(self):
        spec = importlib.util.spec_from_file_location('epd_palette', ROOT / 'frame-app/mk_bin_and_upload.py')
        converter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(converter)
        for status in (RecoveryStatus(3, False, False), RecoveryStatus(3, True, False),
                       RecoveryStatus(3, False, True), RecoveryStatus()):
            ns = rendering_functions(status)
            image = self.render(ns)
            item = ns['recovery_indicator_layout'](ImageDraw.Draw(image), (15, 870, 1585, 1185), status)
            bounds = tuple(int(v) for v in item['bounds'])
            converted = converter.to_indices(image, dither=True).convert('RGB').crop(bounds)
            ink = [max(p) < 160 for p in image.crop(bounds).getdata()]
            retained = sum(is_ink and max(p) < 160 for is_ink, p in zip(ink, converted.getdata()))
            # Retain the glyph strokes themselves, not background dither noise.
            self.assertGreater(sum(ink), 80)
            self.assertGreater(retained / sum(ink), .4)


if __name__ == '__main__':
    unittest.main()
