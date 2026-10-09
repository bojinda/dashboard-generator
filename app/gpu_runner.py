"""Thin dashboard transport adapter; coordination stays in aihub-gpu-runner."""
import hashlib
import os

from aihub_gpu_runner.client import CURRENT_OPERATION, JobClient
from aihub_gpu_runner.core import encode


def _run(step, target, operation, payload, artifact=False):
    context = CURRENT_OPERATION.get()
    if context is not None:
        payload = context.snapshot(step, payload)
        operation_id = context.request_id
    else:
        # Independently invoked quote/to-do calls also reconnect by payload.
        operation_id = 'standalone-' + hashlib.sha256(encode([step, payload])).hexdigest()[:40]
    request_id = JobClient.stage_id(operation_id, step)
    return JobClient.from_environment().run(request_id, target, operation, payload, artifact=artifact)


def generate(payload, step):
    return _run(step, os.environ.get('GPU_RUNNER_OLLAMA_TARGET', 'ollama'), 'generate', payload)


def wallpaper(workflow):
    return _run('wallpaper', os.environ.get('GPU_RUNNER_WALLPAPER_TARGET', 'wallpaper'),
                'workflow', workflow, artifact=True)
