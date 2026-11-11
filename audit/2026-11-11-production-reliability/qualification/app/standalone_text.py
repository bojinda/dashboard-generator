"""Durable Media text intents. Only the creating call may submit an intent.

Later calls reconcile with GET only; absence is not permission to resubmit.
Terminal receipts and the legacy runner journal are never reopened or erased.
"""
import hashlib
import os
import re
import uuid

from aihub_gpu_runner.client import CoordinationError, JobClient, KnownJobFailure
from aihub_gpu_runner.core import AtomicStore, NativeLock, encode
from daily_quotes import toronto_day


class InvalidModelOutput(ValueError):
    pass


class StandaloneText:
    def __init__(self, store, client, day=toronto_day):
        self.store, self.client, self.day = store, client, day

    @staticmethod
    def expected(target, payload):
        return hashlib.sha256(encode(dict(target=target, operation='generate', payload=payload))).hexdigest()

    def reconcile(self, record):
        status = self.client.status(record['request_id'])
        if (status.get('request_hash') != record['request_hash'] or
                status.get('target') != record['target']):
            raise CoordinationError('standalone_identity_mismatch')
        if (status.get('state') not in ('success', 'failed', 'cancelled') or
                status.get('resources_released') is not True or
                status.get('supervision_active') is not False):
            raise CoordinationError('standalone_runner_uncertain')
        if status['state'] != 'success':
            raise KnownJobFailure('runner_job_failed_' + str(status.get('error_code') or status['state']))
        return self.client.result(record['request_id'])['value']

    def run(self, step, target, payload, validator=None):
        key = hashlib.sha256(encode(step)).hexdigest()
        pointer = 'standalone-text/' + key + '.json'
        lock = NativeLock(self.store.path('standalone-text-' + key + '.lock'))
        if not lock.acquire(0):
            raise CoordinationError('standalone_text_in_progress')
        try:
            return self._locked(pointer, step, target, payload, validator)
        finally:
            lock.close()

    def _locked(self, pointer, step, target, payload, validator):
        expected = self.expected(target, payload)
        today = self.day()
        previous = self.store.read(pointer)
        if previous is not None:
            if (not isinstance(previous, dict) or previous.get('schema') != 1 or
                    previous.get('step') != step or previous.get('state') not in ('pending', 'success', 'failed', 'invalid', 'resolved') or
                    not isinstance(previous.get('request_id'), str) or
                    not re.fullmatch(r'stage-[0-9a-f]{48}', previous['request_id']) or
                    not isinstance(previous.get('request_hash'), str) or
                    not re.fullmatch(r'[0-9a-f]{64}', previous['request_hash']) or
                    not isinstance(previous.get('target'), str) or not previous['target'] or
                    not isinstance(previous.get('day'), str)):
                raise CoordinationError('standalone_state_unverified')
            if previous['state'] == 'pending':
                if (not isinstance(previous.get('payload'), dict) or
                        self.expected(previous['target'], previous['payload']) != previous['request_hash'] or
                        JobClient.stage_id(previous.get('operation_id', ''), step) != previous['request_id']):
                    raise CoordinationError('standalone_state_unverified')
                # Even changed inputs/days cannot bypass an unresolved intent.
                try:
                    data = self.reconcile(previous)
                except KnownJobFailure as exc:
                    self.finish(pointer, previous, 'failed')
                    raise exc
                except Exception:
                    raise CoordinationError('standalone_runner_uncertain') from None
                # Validate old data only against its exact original input.
                if previous['request_hash'] != expected:
                    self.finish(pointer, previous, 'resolved')
                    raise CoordinationError('standalone_previous_input_settled')
                return self.accept(pointer, previous, data, validator)
            if previous['state'] == 'success' and previous['request_hash'] == expected:
                data = previous['result']
                if validator is not None:
                    validator(data)
                return data

        # Migration from the old payload-derived ID is GET-only. A terminal
        # retired job authorizes no replay; this invocation creates a new intent.
        legacy_id = JobClient.stage_id('standalone-' + hashlib.sha256(encode([step, payload])).hexdigest()[:40], step)
        if previous is None:
            legacy = dict(request_id=legacy_id, request_hash=expected, target=target)
            try:
                data = self.reconcile(legacy)
            except KnownJobFailure:
                pass
            except CoordinationError as exc:
                if str(exc) != 'runner_request_rejected_404':
                    raise
            else:
                # Preserve ordinary successful legacy content caching.
                record = dict(schema=1, step=step, day=today, request_id=legacy_id,
                              request_hash=expected, target=target, state='pending', legacy=True)
                return self.accept(pointer, record, data, validator)

        operation_id = 'standalone-' + uuid.uuid4().hex
        record = dict(schema=1, step=step, day=today, operation_id=operation_id,
                      request_id=JobClient.stage_id(operation_id, step), request_hash=expected,
                      target=target, payload=payload, state='pending', legacy_request_id=legacy_id)
        # AtomicStore fsyncs file and directory. Failure here prevents submission.
        self.store.write(pointer, record)
        try:
            data = self.client.run(record['request_id'], target, 'generate', payload)
        except KnownJobFailure:
            self.finish(pointer, record, 'failed')
            raise
        except Exception:
            raise CoordinationError('standalone_runner_uncertain') from None
        return self.accept(pointer, record, data, validator)

    def accept(self, pointer, record, data, validator):
        try:
            if validator is not None:
                validator(data)
        except InvalidModelOutput:
            self.finish(pointer, record, 'invalid')
            raise
        self.finish(pointer, record, 'success', data)
        return data

    def finish(self, pointer, record, state, result=None):
        receipt = dict(record, state=state)
        if result is not None:
            receipt['result'] = result
        # Retain a receipt before moving the active pointer. Failed writes leave
        # the pending identity in place, so the next call can only reconcile it.
        name = 'standalone-text/receipts/' + record['request_id'] + '.json'
        old = self.store.read(name)
        if old is not None and old != receipt:
            raise CoordinationError('standalone_receipt_conflict')
        self.store.write(name, receipt)
        self.store.write(pointer, receipt)


def generate_standalone(payload, step, target, validator=None):
    try:
        store = AtomicStore(os.environ.get('GPU_RUNNER_CLIENT_STATE', '/var/lib/dashboard-gpu-client'))
        return StandaloneText(store, JobClient.from_environment()).run(step, target, payload, validator)
    except (KnownJobFailure, InvalidModelOutput, CoordinationError):
        raise
    except Exception:
        raise CoordinationError('standalone_storage_or_state_uncertain') from None
