"""Run production text/cache functions offline without web/rendering dependencies."""
import ast
import contextlib
from datetime import date
import hashlib
import io
import json
from pathlib import Path
import re
import threading
import time
import unittest
import tempfile
import sys
from types import SimpleNamespace
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
from daily_quotes import DailyQuotes, toronto_day
from aihub_gpu_runner.core import AtomicStore


class SyntheticCoordinationError(Exception):
    pass


def text_functions():
    names = {
        '_norm_ws', 'is_effectively_blank_line', '_stable_todo_hash',
        'clean_quote_text', 'quote_is_usable', 'ollama_daily_quote', 'todo_display_rows',
        'ollama_format_todo', 'todo_ai_refresh', 'ollama_generate_overlay',
        'ollama_generate_season_block', 'scrub_precip_words',
        'overlay_is_valid', 'season_is_valid',
    }
    source = ast.parse((ROOT / 'app/main.py').read_text(encoding='utf-8'))
    functions = [node for node in source.body if
                 (isinstance(node, ast.FunctionDef) and node.name in names) or
                 (isinstance(node, ast.Assign) and any(
                     isinstance(t, ast.Name) and t.id in {'_OVERLAY_RE', '_SEASON_RE'} for t in node.targets))]
    for node in functions:
        if isinstance(node, ast.FunctionDef):
            node.decorator_list = []  # Exercise the endpoint body without starting FastAPI.
    namespace = {
        'date': date, 'hashlib': hashlib, 'json': json, 're': re, 'time': time,
        'OLLAMA_MODEL': 'qwen3.8:27b', 'OLLAMA_URL': 'http://offline.invalid',
        'TODO_ENTITY': 'todo.synthetic', 'CoordinationError': SyntheticCoordinationError,
        'QUOTE_LOCK': threading.Lock(), 'TODO_AI_LOCK': threading.Lock(),
        'QUOTE_CACHE': {'day': None, 'quote': '', 'recent': []},
        'TODO_AI_CACHE': {'hash': None, 'day': None, 'lines': [], 'summary_line': '', 'ts': 0},
        'runner_generate': Mock(return_value={}),
        '_todo_items_open_sorted': Mock(side_effect=AssertionError('live HA forbidden')),
    }
    for name in ('quote_system', 'quote_user', 'todo_system', 'todo_user',
                 'overlay_system', 'overlay_user', 'season_system', 'season_user'):
        namespace['build_' + name + '_prompt'] = Mock(return_value='synthetic prompt')
    temporary = tempfile.TemporaryDirectory()
    namespace['_temporary'] = temporary
    client = SimpleNamespace(run=lambda rid, target, op, payload: namespace['runner_generate'](payload, 'daily-quote'), status=Mock(side_effect=OSError('uncertain')))
    ledger = DailyQuotes(lambda: AtomicStore(temporary.name), lambda: client)
    namespace.update(_daily_quotes=lambda: ledger, toronto_day=toronto_day)
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(ROOT / 'app/main.py'), 'exec'), namespace)
    return namespace


class DailyTextTests(unittest.TestCase):
    def setUp(self):
        self.app = text_functions()
        self.addCleanup(self.app['_temporary'].cleanup)
        self.items = [
            {'summary': 'Task A', 'description': 'Original detail A', 'due': '2026-10-22'},
            {'summary': 'Task B', 'description': 'Original detail B'},
            {'item': 'Task C'},
        ]
        self.lines = ['Formatted A', 'Formatted B', 'Formatted C', '', '', '', '+0 more — done']
        self.answer = '\n'.join(self.lines)
        self.logs = io.StringIO()
        self.capture = contextlib.redirect_stdout(self.logs)
        self.capture.__enter__()
        self.addCleanup(self.capture.__exit__, None, None, None)

    def test_todo_budget_and_successful_seven_line_formatting_preserve_thinking(self):
        self.app['runner_generate'].return_value = {'done': True, 'done_reason': 'stop', 'response': self.answer}
        result = self.app['ollama_format_todo'](self.items)
        self.assertEqual(result, {'lines': self.lines[:6], 'summary_line': self.lines[6]})
        payload, step = self.app['runner_generate'].call_args.args
        self.assertEqual(step, 'todo-format')
        self.assertEqual(payload['options'], {'temperature': .2, 'top_p': .9, 'num_predict': 4096, 'num_ctx': 16384})
        self.assertEqual(payload['model'], 'qwen3.8:27b')
        self.assertNotIn('think', payload)
        self.assertNotIn('think', payload['options'])
        self.assertIn('final_empty=False done=True length_limit=False', self.logs.getvalue())

    def test_quote_budget_and_valid_answer_preserve_existing_daily_cache(self):
        quote = 'A quiet day still counts as progress.'
        self.app['runner_generate'].return_value = {'done': True, 'response': quote}
        self.assertEqual(self.app['ollama_daily_quote'](), quote)
        self.assertEqual(self.app['ollama_daily_quote'](), quote)
        self.app['runner_generate'].assert_called_once()
        payload, _ = self.app['runner_generate'].call_args.args
        self.assertEqual(payload['options'], {'temperature': 1.0, 'top_p': .95, 'num_predict': 2048, 'num_ctx': 16384})
        self.assertEqual(payload['model'], 'qwen3.8:27b')
        self.assertFalse(payload['stream'])
        self.assertNotIn('think', payload)

    def test_empty_todo_answers_preserve_real_task_descriptions_and_seven_lines(self):
        for response in ('', ' \n\t ', None, '-\n—\n_\n.\n\n\n+0 more — done'):
            with self.subTest(response_type=type(response).__name__):
                self.app['runner_generate'].return_value = {
                    'done': True, 'done_reason': 'length', 'response': response,
                    'thinking': 'synthetic-private-reasoning'}
                result = self.app['ollama_format_todo'](self.items)
                self.assertTrue(result['fallback'])
                self.assertEqual(result['lines'][:3], [
                    '2026-10-22 — Task A — Original detail A', 'Task B — Original detail B', 'Task C'])
                self.assertEqual(len(result['lines']) + 1, 7)
                self.assertEqual(result['lines'][3:], ['', '', ''])

    def test_fallback_is_displayed_but_not_cached_as_successful_ai_then_success_caches(self):
        self.app['_todo_items_open_sorted'].side_effect = None
        self.app['_todo_items_open_sorted'].return_value = self.items
        self.app['ollama_daily_quote'] = Mock(return_value='A quiet day still counts as progress.')
        self.app['runner_generate'].side_effect = [
            {'done': True, 'done_reason': 'length', 'response': '', 'thinking': 'synthetic reasoning'},
            {'done': True, 'response': self.answer},
        ]
        first = self.app['todo_ai_refresh']()
        self.assertIn('Original detail A', first['lines'][0])
        self.assertIn('Original detail B', first['lines'][1])
        self.assertEqual(first['lines'][2], 'Task C')
        self.assertIsNone(self.app['TODO_AI_CACHE']['hash'])
        second = self.app['todo_ai_refresh']()
        self.assertEqual(second['lines'][:3], self.lines[:3])
        self.assertEqual(self.app['TODO_AI_CACHE']['hash'], second['hash'])
        self.assertTrue(self.app['todo_ai_refresh']()['cached'])
        self.assertEqual(self.app['runner_generate'].call_count, 2)

    def test_length_limited_partial_todo_uses_original_tasks_without_ai_cache(self):
        self.app['_todo_items_open_sorted'].side_effect = None
        self.app['_todo_items_open_sorted'].return_value = self.items
        self.app['ollama_daily_quote'] = Mock(return_value='A quiet day still counts as progress.')
        self.app['runner_generate'].return_value = {
            'done': True, 'done_reason': 'length',
            'response': 'Partial formatted A\nPartial formatted B',
            'thinking': 'synthetic private reasoning',
        }
        result = self.app['todo_ai_refresh']()
        self.assertEqual(result['lines'][:3], [
            '2026-10-22 — Task A — Original detail A', 'Task B — Original detail B', 'Task C'])
        self.assertIsNone(self.app['TODO_AI_CACHE']['hash'])
        self.app['runner_generate'].assert_called_once()
        self.assertIn('final_empty=False done=True length_limit=True', self.logs.getvalue())

    def test_empty_quote_fallback_is_not_cached_and_next_intent_is_allowed(self):
        self.app['runner_generate'].return_value = {'done': True, 'done_reason': 'length', 'response': ''}
        fallback = self.app['ollama_daily_quote']()
        self.assertTrue(fallback.strip())
        self.assertEqual(self.app['ollama_daily_quote'](), fallback)
        self.assertEqual(self.app['runner_generate'].call_count, 2)
        self.app['runner_generate'].side_effect = RuntimeError('synthetic unavailable')
        self.assertEqual(self.app['ollama_daily_quote'](), fallback)

    def test_unusable_nonempty_quote_does_not_automatically_retry(self):
        self.app['runner_generate'].return_value = {'response': 'sorry'}
        self.assertTrue(self.app['ollama_daily_quote']().strip())
        self.assertEqual(self.app['runner_generate'].call_count, 1)

    def test_coordination_failures_still_propagate(self):
        self.app['runner_generate'].side_effect = SyntheticCoordinationError('held')
        for name, args in [('ollama_format_todo', (self.items,)),
                           ('ollama_generate_overlay', ('clear', 'day')),
                           ('ollama_generate_season_block', ('autumn', 'day', 'clear'))]:
            with self.subTest(function=name), self.assertRaises(SyntheticCoordinationError):
                self.app[name](*args)

    def test_diagnostics_never_include_tasks_reasoning_or_exception_text(self):
        secret = 'synthetic-private-reasoning'
        self.app['runner_generate'].return_value = {
            'done': True, 'done_reason': 'length', 'response': '', 'thinking': secret}
        self.app['ollama_format_todo'](self.items)
        self.app['ollama_daily_quote']()
        self.app['runner_generate'].side_effect = RuntimeError('synthetic-private-error-payload')
        self.app['ollama_format_todo'](self.items)
        logs = self.logs.getvalue()
        self.assertIn('final_empty=True done=True length_limit=True thinking_present=True', logs)
        for private in (secret, 'Original detail A', 'Task A', 'synthetic-private-error-payload'):
            self.assertNotIn(private, logs)

    def structured_cases(self):
        return [
            ('ollama_generate_overlay', ('clear', 'day'), 'weather-overlay',
             'WEATHER OVERLAY:\nSky: clear\nAir: dry air\nGround: gravel\nLanternLight: warm glow'),
            ('ollama_generate_season_block', ('autumn', 'day', 'clear'), 'season-block',
             'SEASONAL ENVIRONMENT:\nTrees: amber leaves\nGround: fallen leaves\nAir: cool\nDistantDetails: hills'),
        ]

    def test_weather_and_season_budgets_preserve_options_prompts_and_valid_answers(self):
        for name, args, step, answer in self.structured_cases():
            self.app['runner_generate'].return_value = {'done': True, 'done_reason': 'stop', 'response': answer}
            self.assertEqual(self.app[name](*args), answer)
            self.assertEqual(self.app['runner_generate'].call_args.args[1], step + '-0')
        self.assertEqual(self.app['runner_generate'].call_count, 2)
        for call in self.app['runner_generate'].call_args_list:
            payload, _ = call.args
            self.assertEqual(payload['options'], {'temperature': .3, 'top_p': .9, 'num_predict': 4096, 'num_ctx': 16384})
            self.assertEqual(payload['model'], 'qwen3.8:27b')
            self.assertEqual(payload['prompt'], 'SYSTEM:\nsynthetic prompt\n\nUSER:\nsynthetic prompt\n')
            self.assertFalse(payload['stream'])
            self.assertNotIn('think', payload)
            self.assertNotIn('think', payload['options'])

    def test_structured_answers_reject_length_limit_even_when_schema_is_valid(self):
        for name, args, _, answer in self.structured_cases():
            for metadata in ({'done': True, 'done_reason': 'length'}, {'done': False, 'done_reason': 'stop'}):
                with self.subTest(function=name, metadata=metadata):
                    self.app['runner_generate'].reset_mock()
                    self.app['runner_generate'].return_value = dict(metadata, response=answer)
                    self.assertEqual(self.app[name](*args), '')
                    self.assertEqual(self.app['runner_generate'].call_count, 3)

    def test_structured_invalid_or_empty_answers_return_empty_for_deterministic_fallback(self):
        for name, args, _, answer in self.structured_cases():
            for response in ('', None, '\n'.join(answer.splitlines()[:2]), 'invalid structure'):
                with self.subTest(function=name, response=response):
                    self.app['runner_generate'].return_value = {'done': True, 'done_reason': 'stop', 'response': response}
                    self.assertEqual(self.app[name](*args, retries=0), '')

    def test_structured_retry_accepts_complete_answer_after_truncation(self):
        for name, args, step, answer in self.structured_cases():
            self.app['runner_generate'].reset_mock()
            self.app['runner_generate'].side_effect = [
                {'done': True, 'done_reason': 'length', 'response': answer},
                {'done': True, 'done_reason': 'stop', 'response': answer},
            ]
            self.assertEqual(self.app[name](*args), answer)
            self.assertEqual(self.app['runner_generate'].call_count, 2)
            self.assertEqual(self.app['runner_generate'].call_args.args[1], step + '-1')

    def test_structured_diagnostics_only_log_completion_metadata(self):
        for name, args, step, _ in self.structured_cases():
            self.app['runner_generate'].return_value = {
                'done': True, 'done_reason': 'length', 'response': 'private final content',
                'thinking': 'private reasoning content',
            }
            self.assertEqual(self.app[name](*args, retries=0), '')
            self.assertIn(step + ': final_empty=False done=True length_limit=True thinking_present=True',
                          self.logs.getvalue())
        self.assertNotIn('private final content', self.logs.getvalue())
        self.assertNotIn('private reasoning content', self.logs.getvalue())

    def test_existing_deterministic_overlay_fallbacks_are_used_without_rendering(self):
        source = ast.parse((ROOT / 'app/main.py').read_text(encoding='utf-8'))
        render = next(n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == 'render_wallpaper')
        # Execute only the existing fallback branches, never rendering or submitting GPU work.
        branches = [n for n in render.body if isinstance(n, ast.If) and
                    ast.unparse(n.test) in {'not overlay.strip()', 'not season_block.strip()'}]
        self.assertEqual(len(branches), 2)
        namespace = {'overlay': '', 'season_block': '', 'tod': 'day', 'weather_line': 'clear'}
        exec(compile(ast.Module(body=branches, type_ignores=[]), str(ROOT / 'app/main.py'), 'exec'), namespace)
        self.assertIn('Sky: day sky matching clear', namespace['overlay'])
        self.assertIn('Trees: summer woods with full leafy branches around the scene', namespace['season_block'])


if __name__ == '__main__':
    unittest.main()
