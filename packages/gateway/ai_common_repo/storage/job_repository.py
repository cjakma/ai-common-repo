"""Data-access object for the background-job queue.

All ``jobs``-table SQL lives here so the queue stays a policy-only wrapper
(``workers/queue.py``) and the project keeps its "no SQL outside the storage
layer" invariant. The ``Job`` row model lives here too since it mirrors a table
row; the queue re-exports it so its public surface is unchanged."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..core import constants as C
from ..core.ids import now_iso
from .database import Database, database_locked_methods


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


@database_locked_methods
class JobRepository:
    """Every ``jobs``-table statement. No queueing policy lives here."""

    def __init__(self, db: Database):
        self._db = db

    def insert(self, job: Job) -> None:
        c = self._db.conn
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

    def live_key_exists(self, key: str) -> bool:
        row = self._db.conn.execute(
            "SELECT 1 FROM jobs WHERE idempotency_key=? AND status IN (?,?) LIMIT 1",
            (key, C.JOB_STATUS_QUEUED, C.JOB_STATUS_RUNNING),
        ).fetchone()
        return row is not None

    def next_runnable(self, now: str) -> Job | None:
        row = self._db.conn.execute(
            "SELECT * FROM jobs WHERE status=? AND run_at<=? "
            "ORDER BY run_at, created_at LIMIT 1",
            (C.JOB_STATUS_QUEUED, now),
        ).fetchone()
        return _row_to_job(row) if row else None

    def mark_running(self, job_id: str) -> bool:
        """Compare-and-swap ``queued -> running``. True iff this caller won it."""
        c = self._db.conn
        cur = c.execute(
            "UPDATE jobs SET status=?, attempts=attempts+1, updated_at=? "
            "WHERE id=? AND status=?",
            (C.JOB_STATUS_RUNNING, now_iso(), job_id, C.JOB_STATUS_QUEUED),
        )
        c.commit()
        return cur.rowcount == 1

    def mark_done(self, job_id: str) -> None:
        c = self._db.conn
        c.execute("UPDATE jobs SET status=?, last_error=NULL, updated_at=? WHERE id=?",
                  (C.JOB_STATUS_DONE, now_iso(), job_id))
        c.commit()

    def mark_failed(self, job_id: str, error: str) -> None:
        c = self._db.conn
        c.execute("UPDATE jobs SET status=?, last_error=?, updated_at=? WHERE id=?",
                  (C.JOB_STATUS_FAILED, error[:2000], now_iso(), job_id))
        c.commit()

    def requeue(self, job_id: str, error: str, run_at: str) -> None:
        c = self._db.conn
        c.execute(
            "UPDATE jobs SET status=?, last_error=?, run_at=?, updated_at=? WHERE id=?",
            (C.JOB_STATUS_QUEUED, error[:2000], run_at, now_iso(), job_id),
        )
        c.commit()

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

    def recent_failed(self, limit: int = 20) -> list[Job]:
        rows = self._db.conn.execute(
            "SELECT * FROM jobs WHERE status=? ORDER BY updated_at DESC LIMIT ?",
            (C.JOB_STATUS_FAILED, limit),
        ).fetchall()
        return [_row_to_job(r) for r in rows]
