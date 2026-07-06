"""Phase 1 — worker foundation. Exercises the SQLite job queue, the in-process
worker (success / retry / unknown handler / graceful shutdown), the interval
scheduler, the async ingest path, and the auto-forget job. All offline."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "gateway"))

from ai_common_repo.core import constants as C  # noqa: E402
from ai_common_repo.core.config import load_config  # noqa: E402
from ai_common_repo.core.models import SessionIngestRequest, StoreMemoryRequest  # noqa: E402
from ai_common_repo.service import MemoryService  # noqa: E402
from ai_common_repo.storage.database import Database  # noqa: E402
from ai_common_repo.workers import Scheduler, SqliteJobQueue, Worker  # noqa: E402


def _db() -> Database:
    tmp = tempfile.mkdtemp()
    return Database(os.path.join(tmp, "memory.db"))


def _service(**memory_overrides) -> MemoryService:
    tmp = tempfile.mkdtemp()
    os.environ["AICR_STORAGE__DB_PATH"] = os.path.join(tmp, "memory.db")
    os.environ["AICR_MODELS__DEFAULT_BACKEND"] = "none"  # offline heuristic
    cfg = load_config()
    for k, v in memory_overrides.items():
        setattr(cfg.memory, k, v)
    return MemoryService(cfg)


# -- queue -------------------------------------------------------------------
def test_enqueue_claim_complete():
    q = SqliteJobQueue(_db())
    job = q.enqueue("noop", {"x": 1})
    assert job is not None and q.counts()["queued"] == 1

    claimed = q.claim()
    assert claimed is not None and claimed.id == job.id
    assert claimed.status == "running" and claimed.attempts == 1
    assert q.claim() is None  # nothing else runnable

    q.complete(claimed.id)
    assert q.get(job.id).status == "done"
    assert q.counts()["done"] == 1


def test_idempotency_key_dedup():
    q = SqliteJobQueue(_db())
    first = q.enqueue("noop", idempotency_key="k1")
    second = q.enqueue("noop", idempotency_key="k1")
    assert first is not None and second is None
    assert q.counts()["queued"] == 1


def test_fail_retries_then_gives_up():
    q = SqliteJobQueue(_db(), backoff_base_s=0.0)  # no delay -> immediately runnable
    q.enqueue("noop", max_attempts=2)

    j = q.claim()
    q.fail(j.id, "boom")
    assert q.get(j.id).status == "queued"  # retry scheduled

    j2 = q.claim()
    assert j2.attempts == 2
    q.fail(j2.id, "boom again")
    assert q.get(j2.id).status == "failed"  # attempts exhausted
    assert q.claim() is None


def test_backoff_delays_next_run():
    q = SqliteJobQueue(_db(), backoff_base_s=60.0)
    q.enqueue("noop", max_attempts=3)
    j = q.claim()
    q.fail(j.id, "err")
    # run_at pushed into the future -> not immediately claimable
    assert q.claim() is None
    assert q.get(j.id).status == "queued"


# -- worker ------------------------------------------------------------------
def test_worker_runs_handler_and_isolates_failure():
    q = SqliteJobQueue(_db(), backoff_base_s=0.0)
    seen: list[dict] = []

    def ok(payload):
        seen.append(payload)

    def boom(payload):
        raise ValueError("nope")

    w = Worker(q, {"ok": ok, "boom": boom})
    ok_job = q.enqueue("ok", {"a": 1})
    boom_job = q.enqueue("boom", max_attempts=1)

    w.drain()
    assert seen == [{"a": 1}]
    assert q.get(ok_job.id).status == "done"
    assert q.get(boom_job.id).status == "failed"  # exception isolated


def test_worker_unknown_handler_fails_job():
    q = SqliteJobQueue(_db())
    job = q.enqueue("does_not_exist", max_attempts=1)
    Worker(q, {}).run_once()
    assert q.get(job.id).status == "failed"


def test_worker_graceful_shutdown_no_residue():
    q = SqliteJobQueue(_db())
    w = Worker(q, {"noop": lambda p: None}, poll_interval_s=0.01)
    before = threading.active_count()
    w.start()
    time.sleep(0.05)
    w.stop(timeout=2.0)
    assert w._thread is None
    # give the daemon a moment to fully unwind, then assert no leak
    time.sleep(0.05)
    assert threading.active_count() == before


# -- scheduler ---------------------------------------------------------------
def test_scheduler_fires_due_tasks_with_injected_clock():
    q = SqliteJobQueue(_db())
    fake = {"t": 1000.0}
    sched = Scheduler(q, clock=lambda: fake["t"])
    sched.every(10.0, "tick", {"n": 1}, name="ticker")

    assert sched.tick() == 0  # next_run = 1010, not yet due
    fake["t"] = 1011.0
    assert sched.tick() == 1
    assert q.counts()["queued"] == 1
    # same slot must not double-enqueue if tick runs again before advancing time
    assert sched.tick() == 0
    fake["t"] = 1025.0
    assert sched.tick() == 1


# -- async ingest path -------------------------------------------------------
def test_async_ingest_defers_to_worker():
    svc = _service(async_jobs=True)
    out = svc.ingest_session(SessionIngestRequest.model_validate({
        "schema": "ai-common-repo.session.v1", "tool": "codex", "project_id": "repo:async",
        "messages": [{"role": "user", "content": "We must always run tests before pushing."}],
    }))
    assert out["memories_created"] == 0  # nothing inline
    assert C.JOB_EXTRACT_SESSION in out["queued_jobs"]

    worker = Worker(svc.jobs, svc.handlers)
    worker.drain()  # extract_session, then the chained rebuild_profile

    found = svc.memories.active_for_scope("painmaster", "repo:async")
    assert any("tests" in m.content for m in found), found
    prof = svc.get_profile(None, "repo:async")
    assert prof.stable or prof.dynamic


# -- forget job --------------------------------------------------------------
def test_forget_job_expires_only_due_memories():
    svc = _service()
    keep = svc.store_memory(StoreMemoryRequest(
        content="Keep this active fact about the build.", type="project_fact",
        scope="project", project_id="repo:forget"))
    drop = svc.store_memory(StoreMemoryRequest(
        content="This decision is time-limited and should expire.", type="decision",
        scope="project", project_id="repo:forget"))
    # backdate drop's validity window
    svc.memories._db.conn.execute(
        "UPDATE memories SET valid_until=? WHERE id=?",
        ("2000-01-01T00:00:00Z", drop.id))
    svc.memories._db.conn.commit()

    svc._job_forget_expired({})

    statuses = {r["id"]: r["status"] for r in svc.memories._db.conn.execute(
        "SELECT id, status FROM memories").fetchall()}
    assert statuses[keep.id] == C.MEMORY_STATUS_ACTIVE
    assert statuses[drop.id] == C.MEMORY_STATUS_EXPIRED


def test_forget_confidence_floor():
    svc = _service(forget_confidence_floor=0.5)
    low = svc.store_memory(StoreMemoryRequest(
        content="Low confidence guess about the API.", type="project_fact",
        scope="project", project_id="repo:floor", confidence=0.2))
    high = svc.store_memory(StoreMemoryRequest(
        content="High confidence fact about the API.", type="project_fact",
        scope="project", project_id="repo:floor", confidence=0.95))
    svc._job_forget_expired({})
    statuses = {r["id"]: r["status"] for r in svc.memories._db.conn.execute(
        "SELECT id, status FROM memories").fetchall()}
    assert statuses[low.id] == C.MEMORY_STATUS_EXPIRED
    assert statuses[high.id] == C.MEMORY_STATUS_ACTIVE


# allow direct execution like the smoke test
if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print("OK", _name)
