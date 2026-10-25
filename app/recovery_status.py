"""Best-effort aggregate-only status cache; never part of GPU coordination."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
import threading
import time

from aihub_gpu_runner.client import APITransport
from aihub_gpu_runner.core import strict_json

FIELDS = {'schema_version', 'incidents_7d', 'operator_required', 'snapshot_at'}


@dataclass(frozen=True)
class RecoveryStatus:
    count: int | None = None
    operator_required: bool = False
    stale: bool = True


def validate_summary(value, now):
    if (not isinstance(value, dict) or set(value) != FIELDS or
            type(value['schema_version']) is not int or value['schema_version'] != 1 or
            type(value['incidents_7d']) is not int or not 0 <= value['incidents_7d'] <= 999999999 or
            type(value['operator_required']) is not bool or not isinstance(value['snapshot_at'], str)):
        raise ValueError('invalid_recovery_aggregate')
    stamp = datetime.fromisoformat(value['snapshot_at'].replace('Z', '+00:00'))
    if stamp.tzinfo is None or stamp > now + timedelta(seconds=30):
        raise ValueError('untrusted_recovery_snapshot_time')
    return dict(value)


def environment_transport():
    token = os.environ.get('GPU_RUNNER_TOKEN', '')
    if len(token) < 32:
        raise ValueError('private_runner_token_required')
    return APITransport(os.environ.get('GPU_RUNNER_URL', ''), token, timeout=.4)


class RecoveryStatusReader:
    """One in-flight fetch, <=150ms render wait, 60s polling, 15min freshness.

    A stuck DNS/socket/storage operation stays in the one daemon worker. It
    cannot hold the renderer or create unbounded replacement threads. The
    optional AtomicStore is the existing dashboard output-operation store.
    """
    def __init__(self, store_factory, transport_factory=environment_transport, *,
                 clock=lambda: datetime.now(timezone.utc), monotonic=time.monotonic):
        self.store_factory, self.transport_factory = store_factory, transport_factory
        self.clock, self.monotonic = clock, monotonic
        self.lock = threading.Lock()
        self.cached, self.thread, self.store = None, None, None
        self.loaded, self.failed, self.next_check = False, False, 0

    def _refresh(self):
        try:
            if not self.loaded:
                try:
                    self.store = self.store_factory()
                    cached = self.store.read('recovery-summary.json')
                    if cached is not None:
                        cached = validate_summary(cached, self.clock())
                        with self.lock:
                            self.cached = cached
                except Exception:
                    pass
                finally:
                    self.loaded = True
            status, media, data = self.transport_factory().request('GET', '/recovery-summary')
            if status != 200 or media != 'application/json' or len(data) > 2048:
                raise ValueError('recovery_summary_unavailable')
            summary = validate_summary(strict_json(data), self.clock())
            with self.lock:
                self.cached, self.failed = summary, False
            try:
                if self.store is not None:
                    self.store.write('recovery-summary.json', summary)
            except Exception:
                pass  # A cache write failure never turns generation into failure.
        except Exception:
            with self.lock:
                self.failed = True

    def read(self, wait_budget=.15):
        """Return truthful cached/unknown status even if refresh never returns."""
        with self.lock:
            if self.monotonic() >= self.next_check and not (self.thread and self.thread.is_alive()):
                self.next_check = self.monotonic() + 60
                self.thread = threading.Thread(target=self._refresh, name='recovery-summary', daemon=True)
                try:
                    self.thread.start()
                except RuntimeError:
                    self.thread, self.failed = None, True
            thread = self.thread
        if thread is not None:
            thread.join(max(0, min(.15, wait_budget)))
        with self.lock:
            if self.cached is None:
                return RecoveryStatus()
            age = self.clock() - datetime.fromisoformat(self.cached['snapshot_at'].replace('Z', '+00:00'))
            return RecoveryStatus(self.cached['incidents_7d'], self.cached['operator_required'],
                                  self.failed or age > timedelta(minutes=15))
