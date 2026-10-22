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
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]


class SyntheticCoordinationError(Exception):
    pass


def text_functions():
    names = {
        '_norm_ws', 'is_effectively_blank_line', '_stable_todo_hash',
        'clean_quote_text', 'quote_is_usable', 'ollama_daily_quote',
        'ollama_format_todo', 'todo_ai_refresh', 'ollama_generate_overlay',
        'ollama_generate_season_block', 'scrub_precip_words',
    }
    source = ast.parse((ROOT / 'app/main.py').read_text(encoding='utf-8'))
    functions = [node for node in source.body if isinstance(node, ast.FunctionDef) and node.name in names]
    for node in functions:
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
        'overlay_is_valid': Mock(return_value=True), 'season_is_valid': Mock(return_value=True),
    }
    for name in ('quote_system', 'quote_user', 'todo_system', 'todo_user',
                 'overlay_system', 'overlay_user', 'season_system', 'season_user'):
        namespace['build_' + name + '_prompt'] = Mock(return_value='synthetic prompt')
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(ROOT / 'app/main.py'), 'exec'), namespace)
    return namespace


class DailyTextTests(unittest.TestCase):
    def setUp(self):
        self.app = text_functions()
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
        self.assertEqual(payload['options'], {'temperature': .2, 'top_p': .9, 'num_predict': 1024, 'num_ctx': 4096})
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
        self.assertEqual(payload['options'], {'temperature': 1.0, 'top_p': .95, 'num_predict': 512, 'num_ctx': 4096})
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

    def test_empty_quote_keeps_same_fallback_as_exception_and_caches_it(self):
        self.app['runner_generate'].return_value = {'done': True, 'done_reason': 'length', 'response': ''}
        fallback = self.app['ollama_daily_quote']()
        self.assertTrue(fallback.strip())
        self.assertEqual(self.app['ollama_daily_quote'](), fallback)
        self.app['runner_generate'].assert_called_once()  # No added retries on an empty answer.
        self.app['QUOTE_CACHE'].update(day=None, quote='', recent=[])
        self.app['runner_generate'].side_effect = RuntimeError('synthetic unavailable')
        self.assertEqual(self.app['ollama_daily_quote'](), fallback)

    def test_unusable_nonempty_quotes_keep_existing_three_attempt_fallback(self):
        self.app['runner_generate'].return_value = {'response': 'sorry'}
        self.assertTrue(self.app['ollama_daily_quote']().strip())
        self.assertEqual(self.app['runner_generate'].call_count, 3)

    def test_coordination_failures_still_propagate(self):
        self.app['runner_generate'].side_effect = SyntheticCoordinationError('held')
        for name, args in [('ollama_format_todo', (self.items,)), ('ollama_daily_quote', ())]:
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

    def test_other_ollama_requests_keep_their_budgets_and_options(self):
        self.app['runner_generate'].return_value = {'response': 'synthetic output'}
        self.app['ollama_generate_overlay']('clear', 'day')
        self.app['ollama_generate_season_block']('autumn', 'day', 'clear')
        self.assertEqual(self.app['runner_generate'].call_count, 2)
        for call in self.app['runner_generate'].call_args_list:
            payload, _ = call.args
            self.assertEqual(payload['options'], {'temperature': .3, 'top_p': .9, 'num_predict': 200, 'num_ctx': 4096})
            self.assertEqual(payload['model'], 'qwen3.8:27b')
            self.assertFalse(payload['stream'])
            self.assertNotIn('think', payload)


if __name__ == '__main__':
    unittest.main()
