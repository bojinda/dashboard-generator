"""Media output admission/reconciliation only. Never submits or retries GPU jobs."""
import hashlib
import json
import re
import time
from pathlib import Path

from aihub_gpu_runner.client import APITransport, CoordinationError, OutputBusy, OutputSlot
from aihub_gpu_runner.core import NativeLock, check_id, encode, timestamp

REASONS = {'output_operation_busy', 'runner_unavailable', 'runner_maintenance',
           'runner_owner_reconciling', 'runner_owner_unverified',
           'previous_runner_job_unresolved', 'runner_job_identity_mismatch',
           'runner_absence_not_yet_stable', 'output_reconciliation_bound_exceeded',
           'reconciliation_storage_unavailable', 'output_supervisor_unavailable_no_replay'}


class DispatchBlocked(OutputBusy):
    def __init__(self, reason, *, operator_required=False, blocking_job=None):
        self.reason = reason if reason in REASONS else 'previous_runner_job_unresolved'
        self.operator_required = operator_required
        self.blocking_job = blocking_job
        super().__init__(self.reason)


def public_operation(record):
    status = record.get('status', 'idle')
    return 'running' if status == 'running' else 'completed' if status == 'ok' else 'failed' if status in ('failed', 'error') else 'idle'


class DispatchOutputSlot(OutputSlot):
    def __init__(self, *args, transport=None, clock=time.time, monotonic=time.monotonic, **kwargs):
        super().__init__(*args, **kwargs)
        self.transport = transport
        self.clock, self.monotonic = clock, monotonic

    def _get(self, route, deadline):
        import os
        if self.transport is None:
            self.transport = APITransport(os.environ.get('GPU_RUNNER_URL', ''),
                                          os.environ.get('GPU_RUNNER_TOKEN', ''), timeout=.6)
        if self.monotonic() >= deadline:
            raise DispatchBlocked('output_reconciliation_bound_exceeded')
        try:
            if isinstance(self.transport, APITransport):
                status, media, raw = self.transport.request('GET', route, deadline=deadline)
            else:
                status, media, raw = self.transport.request('GET', route)
            if self.monotonic() >= deadline or media != 'application/json' or len(raw) > 65536:
                raise ValueError()
            value = json.loads(raw)
            if not isinstance(value, dict):raise ValueError()
            return status, value
        except DispatchBlocked:
            raise
        except Exception:
            raise DispatchBlocked('runner_unavailable') from None

    def _ready(self, deadline):
        code, value = self._get('/resources', deadline)
        if code != 200 or type(value.get('ready')) is not bool or not isinstance(value.get('owners'), dict):
            raise DispatchBlocked('runner_unavailable')
        if value['ready'] is not True:raise DispatchBlocked('runner_maintenance')
        # Healthy unrelated work is serialized by the existing runner. Retained
        # or unverifiable ownership must name the ACTUAL blocker, never the
        # already disproved historical Media request.
        seen=set()
        for owner in value['owners'].values():
            job_id=owner.get('job_id') if isinstance(owner,dict) else None
            if not isinstance(job_id,str) or len(seen)>=4:
                raise DispatchBlocked('runner_owner_unverified',operator_required=True)
            check_id(job_id)
            if job_id in seen:continue
            seen.add(job_id);code,job=self._get('/jobs/'+job_id,deadline)
            if code!=200 or job.get('request_id')!=job_id:
                raise DispatchBlocked('runner_owner_unverified',operator_required=True)
            blocker={k:job.get(k) for k in ('request_id','target','state','error_code','resources_released','supervision_active')}
            if job.get('state')=='reconciling' or job.get('supervision_active') is not True:
                raise DispatchBlocked('runner_owner_reconciling',operator_required=True,blocking_job=blocker)
        return value

    def _receipt(self, record, job_id, pending, outcome, proof):
        # Append reconciliation evidence; original operations/active.json stay exact.
        key = hashlib.sha256(encode([self.scope, record['request_id'], job_id, pending, outcome])).hexdigest()
        name = 'dispatch-reconciliations/' + key + '.json'
        if self.store.read(name) is None:
            self.store.write(name, dict(schema=1, scope=self.scope, operation_id=record['request_id'],
                operation_sha256=hashlib.sha256(encode(record)).hexdigest(), runner_request_id=job_id,
                runner_binding=pending, outcome=outcome, proof=proof, reconciled_at=timestamp(),
                replayed=False))

    def reconcile_runner_jobs(self):
        """Existing output lock is held by begin. Only bounded authenticated GETs."""
        deadline = self.monotonic() + 3
        try:
            self._ready(deadline)
            paths = list(self.store.path('operations').glob('*.json'))
            if len(paths) > 5000:raise DispatchBlocked('output_reconciliation_bound_exceeded', operator_required=True)
            count = 0
            reconciled = set()
            for path in paths:
                if self.monotonic() >= deadline:raise DispatchBlocked('output_reconciliation_bound_exceeded')
                record = self.store.read(str(path.relative_to(self.store.root)))
                if record.get('scope') != self.scope:continue
                check_id(record['request_id'])
                if record.get('status') == 'running' and not record.get('runner_jobs'):
                    self._receipt(record, None, {}, 'abandoned_before_submission', {'output_lock_exclusive': True})
                for job_id, pending in record.get('runner_jobs', {}).items():
                    if pending.get('settled') is True:continue
                    count += 1
                    if count > 8:raise DispatchBlocked('output_reconciliation_bound_exceeded', operator_required=True)
                    check_id(job_id)
                    if pending.get('target') not in ('ollama', 'wallpaper') or not isinstance(pending.get('request_hash'), str) or re.fullmatch('[a-f0-9]{64}', pending['request_hash']) is None:
                        raise DispatchBlocked('runner_job_identity_mismatch', operator_required=True)
                    code, status = self._get('/jobs/' + job_id, deadline)
                    if code == 404:
                        # Never replay the old operation. Only admit a DIFFERENT later
                        # scheduled request after its completed client call is old,
                        # runner is ready, this request owns nothing, and its
                        # exact journal stays absent. Healthy unrelated owners
                        # remain under normal runner admission/serialization.
                        ended = record.get('finished') or record.get('started')
                        if type(ended) not in (int, float) or not 60 <= self.clock() - ended:
                            raise DispatchBlocked('runner_absence_not_yet_stable')
                        ready = self._ready(deadline)
                        if any(o.get('job_id')==job_id for o in ready['owners'].values()):
                            raise DispatchBlocked('runner_job_identity_mismatch',operator_required=True)
                        second, _ = self._get('/jobs/' + job_id, deadline)
                        if second != 404:raise DispatchBlocked('previous_runner_job_unresolved', operator_required=True)
                        self._receipt(record, job_id, pending, 'not_submitted',
                                      {'exact_request_absent_twice': True, 'runner_ready': True,
                                       'unrelated_owners': ready['owners'], 'minimum_completed_client_age_seconds': 60})
                    elif code == 200:
                        if status.get('request_id') != job_id or status.get('request_hash') != pending['request_hash'] or status.get('target') != pending['target']:
                            raise DispatchBlocked('runner_job_identity_mismatch', operator_required=True)
                        if status.get('state') not in ('success', 'failed', 'cancelled') or status.get('resources_released') is not True or status.get('supervision_active') is not False:
                            raise DispatchBlocked('previous_runner_job_unresolved', operator_required=status.get('state') in ('reconciling', 'cleanup_pending'))
                        self._receipt(record, job_id, pending, 'terminal_released', status)
                    else:raise DispatchBlocked('runner_unavailable')
                    reconciled.add(record['request_id'])
            self.store.write('dispatch-health.json', dict(schema=1, status='ready', reason=None,
                operator_required=False, checked_at=timestamp(), checked_at_unix=self.clock(),
                pending_references_checked=count, reconciled_operations=sorted(reconciled)))
        except DispatchBlocked as exc:
            self.store.write('dispatch-health.json', dict(schema=1, status='blocked', reason=exc.reason,
                operator_required=exc.operator_required, blocking_job=exc.blocking_job,
                checked_at=timestamp(), checked_at_unix=self.clock()))
            raise
        except Exception:
            raise DispatchBlocked('reconciliation_storage_unavailable', operator_required=True) from None

    def health(self, refresh=False):
        if refresh:
            cached = self.store.read('dispatch-health.json', {})
            active = self.status()
            stale = self.clock() - cached.get('checked_at_unix', 0) >= 15
            new_failure = (active.get('finished') or 0) > cached.get('checked_at_unix', 0)
            if stale or new_failure:
                # Same CPU output lock; a live background writer is never touched.
                lock = NativeLock(self.lock_path, create=False)
                try:
                    if lock.acquire(0):
                        try:self.reconcile_runner_jobs()
                        except DispatchBlocked:pass
                finally:lock.close()
        record = self.status()
        health = self.store.read('dispatch-health.json', {'status': 'unknown', 'reason': None, 'operator_required': False})
        failed = record.get('status') in ('error', 'failed')
        pending = any(not p.get('settled') for p in record.get('runner_jobs', {}).values())
        unresolved = failed and pending and record.get('request_id') not in health.get('reconciled_operations', [])
        error = record.get('error') if record.get('error') in ('runner_uncertain', 'pipeline_failed', 'output_supervisor_unavailable_no_replay') else 'operation_failed' if failed else None
        return dict(operation_status=public_operation(record), request_id=record.get('request_id'),
                    dispatch_status='reconciliation_pending' if unresolved and health['status']=='ready' else health['status'],
                    reason=health.get('reason'), last_operation_error=error,
                    blocking_job=health.get('blocking_job'),
                    operator_required=health.get('operator_required', False) or unresolved,
                    checked_at=health.get('checked_at'), last_operation_failed=failed)
