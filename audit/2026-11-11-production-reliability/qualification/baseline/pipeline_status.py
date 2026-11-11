"""Bounded public pipeline diagnostics. Never persist exception/prompt text."""
import errno
from aihub_gpu_runner.client import CURRENT_OPERATION, CoordinationError, KnownJobFailure, PendingJob

STAGES = {'weather', 'season', 'todo', 'quote', 'wallpaper', 'composition', 'conversion', 'delivery'}
STATES = {'running', 'success', 'cached', 'fallback', 'failed', 'uncertain', 'skipped'}
REASONS = {'invalid_model_output', 'runner_failed', 'runner_uncertain', 'service_unavailable',
           'quote_in_progress', 'quote_day_rolled_over', 'storage_uncertain', 'pipeline_aborted',
           'conversion_failed', 'image_composition_failed', 'epd_delivery_failed', 'epd_route_unavailable',
           'epd_network_unreachable', 'epd_connection_refused', 'epd_timeout', 'delivery_disabled'}
CODES = {'resource_acquisition_timeout', 'comfy_execution_failed', 'ollama_execution_failed',
         'operator_recovered_no_automatic_resubmission', 'cancelled_before_submission'}
RUNNER_STATES = {'running', 'waiting', 'cleanup_pending', 'reconciling', 'transport_uncertain',
                 'coordinator_unavailable', 'invalid_coordinator_response', 'client_wait_timeout'}


def failure_reason(exc, default='service_unavailable'):
    if isinstance(exc, CoordinationError): return 'runner_uncertain'
    if isinstance(exc, KnownJobFailure): return 'runner_failed'
    return default


def stage_event(name, status, reason=None, exc=None):
    operation = CURRENT_OPERATION.get()
    if operation is None: return
    if name not in STAGES or status not in STATES: raise ValueError('invalid_pipeline_stage')
    pipeline = dict(operation.record.get('details', {}).get('pipeline', {}))
    stages = dict(pipeline.get('stages', {}))
    value = {'status': status}
    if reason in REASONS: value['reason'] = reason
    if isinstance(exc, KnownJobFailure):
        for code in CODES:
            if str(exc) == 'runner_job_failed_' + code: value['runner_error_code'] = code
    if isinstance(exc, PendingJob):
        import re
        if re.fullmatch(r'stage-[0-9a-f]{48}', exc.request_id): value['runner_request_id'] = exc.request_id
        if exc.state in RUNNER_STATES: value['runner_state'] = exc.state
    stages[name] = value
    operation.update_details(pipeline={'schema': 1, 'stages': stages})


def pipeline_summary(record, failed=False):
    raw = record.get('details', {}).get('pipeline', {}).get('stages', {})
    stages = {}
    for name in STAGES:
        entry = raw.get(name)
        if not isinstance(entry, dict) or entry.get('status') not in STATES: continue
        clean = {'status': entry['status']}
        if entry.get('reason') in REASONS: clean['reason'] = entry['reason']
        if entry.get('runner_error_code') in CODES: clean['runner_error_code'] = entry['runner_error_code']
        if entry.get('runner_state') in RUNNER_STATES: clean['runner_state'] = entry['runner_state']
        import re
        rid = entry.get('runner_request_id')
        if isinstance(rid, str) and re.fullmatch(r'stage-[0-9a-f]{48}', rid): clean['runner_request_id'] = rid
        stages[name] = clean
    warnings = [{'stage': name, **value} for name, value in sorted(stages.items())
                if value['status'] in ('fallback', 'failed', 'uncertain')]
    status = ('uncertain' if any(v['status'] == 'uncertain' for v in stages.values()) else
              'failed' if failed else 'degraded' if warnings else
              'running' if record.get('status') == 'running' else 'success')
    return {'pipeline_status': status, 'fully_successful': status == 'success',
            'stages': stages, 'warnings': warnings,
            'wallpaper_generated': stages.get('wallpaper', {}).get('status') == 'success'}


def abort_pipeline(operation, exc):
    stages = operation.record.get('details', {}).get('pipeline', {}).get('stages', {})
    # Preserve the specific failed/uncertain stage already recorded by a caller.
    if not any(v.get('status') in ('failed', 'uncertain') for v in stages.values()):
        name = next((n for n in ('delivery', 'conversion', 'composition') if stages.get(n, {}).get('status') == 'running'), 'composition')
        default = {'delivery': 'epd_delivery_failed', 'conversion': 'conversion_failed',
                   'composition': 'image_composition_failed'}[name]
        if isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EIO, errno.EROFS):
            default = 'storage_uncertain'
        stage_event(name, 'uncertain' if isinstance(exc, CoordinationError) else 'failed',
                    failure_reason(exc, default), exc)
    return 'runner_uncertain' if isinstance(exc, CoordinationError) else 'pipeline_failed'


def delivery_reason(exc):
    current = exc
    for _ in range(4):
        code = getattr(current, 'errno', None)
        if code in (errno.EHOSTUNREACH, errno.ENETUNREACH, errno.ECONNREFUSED, errno.ETIMEDOUT):
            return {errno.EHOSTUNREACH: 'epd_route_unavailable', errno.ENETUNREACH: 'epd_network_unreachable',
                    errno.ECONNREFUSED: 'epd_connection_refused', errno.ETIMEDOUT: 'epd_timeout'}[code]
        current = getattr(current, '__cause__', None) or getattr(current, '__context__', None)
        if current is None: break
    # Requests sometimes retains OS detail only in its wrapped text. Classify a
    # bounded fixed marker, but never copy any part of that message into storage.
    if 'No route to host' in str(exc)[:2048]: return 'epd_route_unavailable'
    return 'epd_delivery_failed'
