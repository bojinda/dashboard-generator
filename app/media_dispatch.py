"""Media output admission/reconciliation only. Never submits or retries GPU jobs."""
import hashlib
import json
import os
import re
import stat
import time
from pathlib import Path

from aihub_gpu_runner.client import APITransport, CoordinationError, OutputBusy, OutputSlot
from aihub_gpu_runner.core import NativeLock, check_id, encode, strict_json, timestamp

# Render admission/status callers allow 10s. Include initial/final readiness,
# historical lookups and 404 revalidation in ONE budget, with 1s caller margin.
RUNNER_REQUEST_TIMEOUT = 2.5
RECONCILIATION_TIMEOUT = 9.0

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

    @staticmethod
    def _read_child(directory, name):
        """Hash the real bytes without resolving the same ancestors per record.

        Both directories come from AtomicStore.path, resolved once per scan.
        Reject links and replacement during the read; this is not an mtime
        cache. Every selected record and receipt is parsed again on each pass.
        """
        if Path(name).name != name:
            raise ValueError('state_path_escape')
        path = directory / name
        try:
            before = path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('non_regular_reconciliation_record')
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
        with os.fdopen(fd, 'rb') as stream:
            opened = os.fstat(stream.fileno())
            # Windows stat/fstat expose different ctime semantics on some
            # Python versions. Compare the same API before/after for changes.
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise ValueError('reconciliation_record_changed')
            data = stream.read()
            if identity(os.fstat(stream.fileno())) != identity(opened):
                raise ValueError('reconciliation_record_changed')
        if identity(path.lstat()) != identity(before):
            raise ValueError('reconciliation_record_changed')
        return strict_json(data)

    def _get(self, route, deadline):
        import os
        if self.transport is None:
            self.transport = APITransport(os.environ.get('GPU_RUNNER_URL', ''),
                                          os.environ.get('GPU_RUNNER_TOKEN', ''), timeout=RUNNER_REQUEST_TIMEOUT)
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
            if self.monotonic() >= deadline:
                raise DispatchBlocked('output_reconciliation_bound_exceeded') from None
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

    def _settled_receipt(self, record, job_id, pending):
        """Read a bound proof, never infer settlement from a receipt's existence.

        The output lock excludes writers for this scope. Original records remain
        immutable; every pass hashes them again rather than trusting a timestamp
        or a mutable scan cursor. Native current ownership is checked separately.
        """
        for outcome in ('terminal_released', 'not_submitted'):
            key = hashlib.sha256(encode([self.scope, record['request_id'], job_id, pending, outcome])).hexdigest()
            receipt = self._read_child(self._receipt_directory, key + '.json')
            if receipt is None:
                continue
            valid = (isinstance(receipt, dict) and receipt.get('schema') == 1 and
                receipt.get('scope') == self.scope and receipt.get('operation_id') == record['request_id'] and
                receipt.get('operation_sha256') == hashlib.sha256(encode(record)).hexdigest() and
                receipt.get('runner_request_id') == job_id and receipt.get('runner_binding') == pending and
                receipt.get('outcome') == outcome and receipt.get('replayed') is False and
                isinstance(receipt.get('reconciled_at'), str) and isinstance(receipt.get('proof'), dict))
            proof = receipt.get('proof', {}) if isinstance(receipt, dict) else {}
            if outcome == 'terminal_released':
                valid = valid and (proof.get('request_id') == job_id and
                    proof.get('request_hash') == pending['request_hash'] and proof.get('target') == pending['target'] and
                    proof.get('state') in ('success', 'failed', 'cancelled') and
                    proof.get('resources_released') is True and proof.get('supervision_active') is False)
            else:
                ended = record.get('finished') or record.get('started')
                owners = proof.get('unrelated_owners')
                valid = valid and (proof.get('exact_request_absent_twice') is True and
                    proof.get('runner_ready') is True and proof.get('minimum_completed_client_age_seconds') == 60 and
                    type(ended) in (int, float) and self.clock() - ended >= 60 and isinstance(owners, dict) and
                    all(isinstance(o, dict) and o.get('job_id') != job_id for o in owners.values()))
            if not valid:
                raise DispatchBlocked('runner_job_identity_mismatch', operator_required=True)
            return outcome
        return None

    def reconcile_runner_jobs(self):
        """Existing output lock is held by begin. Only bounded authenticated GETs."""
        deadline = self.monotonic() + RECONCILIATION_TIMEOUT
        try:
            ready = self._ready(deadline)
            current_owned_jobs = {owner['job_id'] for owner in ready['owners'].values()}
            # Archive size is not a safety predicate. The existing total deadline
            # still bounds disk/GET work; positive terminal receipts let later
            # passes spend the query budget on references lacking final proof.
            operation_directory = self.store.path('operations')
            self._receipt_directory = self.store.path('dispatch-reconciliations')
            paths = operation_directory.glob('*.json')
            count = 0
            reconciled = set()
            historical_jobs = set()
            for path in paths:
                if self.monotonic() >= deadline:raise DispatchBlocked('output_reconciliation_bound_exceeded')
                record = self._read_child(operation_directory, path.name)
                if not isinstance(record, dict):
                    raise DispatchBlocked('reconciliation_storage_unavailable', operator_required=True)
                if record.get('scope') != self.scope:continue
                check_id(record['request_id'])
                if path.name != record['request_id'] + '.json':
                    raise DispatchBlocked('runner_job_identity_mismatch', operator_required=True)
                if record.get('status') == 'running' and not record.get('runner_jobs'):
                    self._receipt(record, None, {}, 'abandoned_before_submission', {'output_lock_exclusive': True})
                for job_id, pending in record.get('runner_jobs', {}).items():
                    historical_jobs.add(job_id)
                    if job_id in current_owned_jobs:
                        # A historical receipt can never authorize a job which
                        # the native authority currently says still owns a GPU.
                        raise DispatchBlocked('previous_runner_job_unresolved', operator_required=True)
                    if pending.get('settled') is True:continue
                    check_id(job_id)
                    if pending.get('target') not in ('ollama', 'wallpaper') or not isinstance(pending.get('request_hash'), str) or re.fullmatch('[a-f0-9]{64}', pending['request_hash']) is None:
                        raise DispatchBlocked('runner_job_identity_mismatch', operator_required=True)
                    receipt_outcome = self._settled_receipt(record, job_id, pending)
                    if receipt_outcome == 'terminal_released':
                        reconciled.add(record['request_id'])
                        continue
                    # A historical 404 is not an immutable native disposition.
                    # Revalidate absence (or a later exact native job) on every
                    # pass using the original bounded GET/age/ownership proof.
                    if count >= 8:raise DispatchBlocked('output_reconciliation_bound_exceeded')
                    count += 1
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
            # Admission is based on current ownership, even if all referenced
            # historical jobs were already verified by earlier passes.
            final = self._ready(deadline)
            if any(owner['job_id'] in historical_jobs for owner in final['owners'].values()):
                raise DispatchBlocked('previous_runner_job_unresolved', operator_required=True)
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
