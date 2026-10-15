"""Local-first background job queue.

`JobQueue` is the interface the rest of the gateway depends on; `SqliteJobQueue`
is the only implementation today and keeps every job in the shared SQLite DB so
there is no external broker (Redis/Celery) to run. All SQL lives in the storage
layer (`storage/job_repository.py`); this module owns only the queueing *policy*
— idempotency, atomic claim, and retry backoff. A job is claimed atomically by
flipping its row ``queued -> running`` with a status guard, which is safe both
across threads (a process-local lock) and across processes (the conditional
UPDATE acts as a compare-and-swap under SQLite's write lock).
"""
from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from typing import Any

from ..core.ids import now_iso, prefixed
from ..storage.database import Database
from ..storage.job_repository import Job, JobRepository

__all__ = ["Job", "JobQueue", "SqliteJobQueue"]


class JobQueue(ABC):
    """Minimal job-queue surface the worker/service code talks to."""

    @abstractmethod
    def enqueue(self, handler: str, payload: dict[str, Any] | None = None, *,
                idempotency_key: str | None = None, max_attempts: int | None = None,
                delay_s: float = 0.0) -> Job | None:
        """Insert a job. Returns None when an idempotency key is already live."""

    @abstractmethod
    def claim(self) -> Job | None:
        """Atomically take the next runnable job, marking it ``running``."""

    @abstractmethod
    def complete(self, job_id: str) -> None: ...

    @abstractmethod
    def fail(self, job_id: str, error: str) -> None:
        """Reschedule with backoff, or mark ``failed`` once attempts run out."""

    @abstractmethod
    def get(self, job_id: str) -> Job | None: ...

    @abstractmethod
    def counts(self) -> dict[str, int]: ...

    @abstractmethod
    def recent_failed(self, limit: int = 20) -> list[Job]:
        """Most recently failed jobs (read-only ops introspection)."""


class SqliteJobQueue(JobQueue):
    def __init__(self, db: Database, *, default_max_attempts: int = 3,
                 backoff_base_s: float = 1.0, backoff_max_s: float = 300.0):
        self._repo = JobRepository(db)
        self._default_max_attempts = default_max_attempts
        self._backoff_base_s = backoff_base_s
        self._backoff_max_s = backoff_max_s
        self._lock = threading.Lock()

    # -- producer ------------------------------------------------------------
    def enqueue(self, handler: str, payload: dict[str, Any] | None = None, *,
                idempotency_key: str | None = None, max_attempts: int | None = None,
                delay_s: float = 0.0) -> Job | None:
        job = Job(
            id=prefixed("job"), handler=handler, payload=payload or {},
            idempotency_key=idempotency_key,
            max_attempts=max_attempts or self._default_max_attempts,
            run_at=_shift(now_iso(), delay_s), created_at=now_iso(), updated_at=now_iso(),
        )
        with self._lock:
            if idempotency_key and self._repo.live_key_exists(idempotency_key):
                return None
            self._repo.insert(job)
        return job

    # -- consumer ------------------------------------------------------------
    def claim(self) -> Job | None:
        with self._lock:
            for _ in range(5):  # retry a few times if another claimer beats us
                job = self._repo.next_runnable(now_iso())
                if job is None:
                    return None
                if self._repo.mark_running(job.id):
                    return self._repo.get(job.id)  # re-read the now-running row
            return None

    def complete(self, job_id: str) -> None:
        with self._lock:
            self._repo.mark_done(job_id)

    def fail(self, job_id: str, error: str) -> None:
        with self._lock:
            job = self._repo.get(job_id)
            if job is None:
                return
            if job.attempts >= job.max_attempts:
                self._repo.mark_failed(job_id, error)
            else:
                self._repo.requeue(job_id, error, _shift(now_iso(), self._backoff(job.attempts)))

    def _backoff(self, attempts: int) -> float:
        return min(self._backoff_base_s * (2 ** max(attempts - 1, 0)), self._backoff_max_s)

    # -- introspection -------------------------------------------------------
    def get(self, job_id: str) -> Job | None:
        return self._repo.get(job_id)

    def counts(self) -> dict[str, int]:
        return self._repo.counts()

    def recent_failed(self, limit: int = 20) -> list[Job]:
        return self._repo.recent_failed(limit)


def _shift(iso_ts: str, seconds: float) -> str:
    """Add ``seconds`` to an ISO ``YYYY-MM-DDTHH:MM:SSZ`` timestamp."""
    if seconds <= 0:
        return iso_ts
    import calendar
    import time
    epoch = calendar.timegm(time.strptime(iso_ts, "%Y-%m-%dT%H:%M:%SZ")) + seconds
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))
