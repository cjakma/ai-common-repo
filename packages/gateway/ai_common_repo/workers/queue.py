"""Local-first background job queue.

`JobQueue` is the interface the rest of the gateway depends on; `SqliteJobQueue`
is the only implementation today and keeps every job in the shared SQLite DB so
there is no external broker (Redis/Celery) to run. A job is claimed atomically
by flipping its row ``queued -> running`` with a status guard, which is safe both
across threads (a process-local lock) and across processes (the conditional
UPDATE acts as a compare-and-swap under SQLite's write lock).
"""
from __future__ import annotations

import json
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..core import constants as C
from ..core.ids import now_iso, prefixed
from ..storage.database import Database, database_locked_methods


@dataclass
class Job:
    id: str
    handler: str
    payload: dict[str, Any] = field(default_factory=dict)
    status: str = C.JOB_STATUS_QUEUED
    idempotency_key: str | None = None
    attempts: int = 0
    max_attempts: int = 3
    run_at: str = ""
    last_error: str | None = None
    created_at: str = ""
    updated_at: str = ""


def _row_to_job(row: Any) -> Job:
    return Job(
        id=row["id"],
        handler=row["handler"],
        payload=json.loads(row["payload_json"] or "{}"),
        status=row["status"],
        idempotency_key=row["idempotency_key"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        run_at=row["run_at"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


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


@database_locked_methods
class SqliteJobQueue(JobQueue):
    def __init__(self, db: Database, *, default_max_attempts: int = 3,
                 backoff_base_s: float = 1.0, backoff_max_s: float = 300.0):
        self._db = db
        self._default_max_attempts = default_max_attempts
        self._backoff_base_s = backoff_base_s
        self._backoff_max_s = backoff_max_s
        self._lock = threading.Lock()

    # -- producer ------------------------------------------------------------
    def enqueue(self, handler: str, payload: dict[str, Any] | None = None, *,
                idempotency_key: str | None = None, max_attempts: int | None = None,
                delay_s: float = 0.0) -> Job | None:
        run_at = _shift(now_iso(), delay_s)
        job = Job(
            id=prefixed("job"), handler=handler, payload=payload or {},
            idempotency_key=idempotency_key,
            max_attempts=max_attempts or self._default_max_attempts,
            run_at=run_at, created_at=now_iso(), updated_at=now_iso(),
        )
        c = self._db.conn
        with self._lock:
            if idempotency_key and self._live_key_exists(idempotency_key):
                return None
            c.execute(
                """INSERT INTO jobs
                   (id,handler,payload_json,status,idempotency_key,attempts,
                    max_attempts,run_at,last_error,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (job.id, job.handler, json.dumps(job.payload, ensure_ascii=False),
                 job.status, job.idempotency_key, job.attempts, job.max_attempts,
                 job.run_at, job.last_error, job.created_at, job.updated_at),
            )
            c.commit()
        return job

    def _live_key_exists(self, key: str) -> bool:
        row = self._db.conn.execute(
            "SELECT 1 FROM jobs WHERE idempotency_key=? AND status IN (?,?) LIMIT 1",
            (key, C.JOB_STATUS_QUEUED, C.JOB_STATUS_RUNNING),
        ).fetchone()
        return row is not None

    # -- consumer ------------------------------------------------------------
    def claim(self) -> Job | None:
        now = now_iso()
        c = self._db.conn
        with self._lock:
            for _ in range(5):  # retry a few times if another claimer beats us
                row = c.execute(
                    "SELECT * FROM jobs WHERE status=? AND run_at<=? "
                    "ORDER BY run_at, created_at LIMIT 1",
                    (C.JOB_STATUS_QUEUED, now),
                ).fetchone()
                if row is None:
                    return None
                cur = c.execute(
                    "UPDATE jobs SET status=?, attempts=attempts+1, updated_at=? "
                    "WHERE id=? AND status=?",
                    (C.JOB_STATUS_RUNNING, now_iso(), row["id"], C.JOB_STATUS_QUEUED),
                )
                c.commit()
                if cur.rowcount == 1:
                    job = _row_to_job(row)
                    job.status = C.JOB_STATUS_RUNNING
                    job.attempts += 1
                    return job
            return None

    def complete(self, job_id: str) -> None:
        c = self._db.conn
        with self._lock:
            c.execute("UPDATE jobs SET status=?, last_error=NULL, updated_at=? WHERE id=?",
                      (C.JOB_STATUS_DONE, now_iso(), job_id))
            c.commit()

    def fail(self, job_id: str, error: str) -> None:
        with self._lock:
            job = self.get(job_id)
            if job is None:
                return
            c = self._db.conn
            if job.attempts >= job.max_attempts:
                c.execute("UPDATE jobs SET status=?, last_error=?, updated_at=? WHERE id=?",
                          (C.JOB_STATUS_FAILED, error[:2000], now_iso(), job_id))
            else:
                run_at = _shift(now_iso(), self._backoff(job.attempts))
                c.execute(
                    "UPDATE jobs SET status=?, last_error=?, run_at=?, updated_at=? WHERE id=?",
                    (C.JOB_STATUS_QUEUED, error[:2000], run_at, now_iso(), job_id),
                )
            c.commit()

    def _backoff(self, attempts: int) -> float:
        return min(self._backoff_base_s * (2 ** max(attempts - 1, 0)), self._backoff_max_s)

    # -- introspection -------------------------------------------------------
    def get(self, job_id: str) -> Job | None:
        row = self._db.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _row_to_job(row) if row else None

    def counts(self) -> dict[str, int]:
        rows = self._db.conn.execute(
            "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
        out = {s: 0 for s in C.JOB_STATUSES}
        for r in rows:
            out[r["status"]] = r["n"]
        return out


def _shift(iso_ts: str, seconds: float) -> str:
    """Add ``seconds`` to an ISO ``YYYY-MM-DDTHH:MM:SSZ`` timestamp."""
    if seconds <= 0:
        return iso_ts
    import calendar
    import time
    epoch = calendar.timegm(time.strptime(iso_ts, "%Y-%m-%dT%H:%M:%SZ")) + seconds
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))
