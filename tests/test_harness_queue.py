"""Harness contract-inbox queue (HARNESS_COMMON_SPEC §4) — all offline over a
temp DB: lifecycle transitions, atomic claiming, stale-claim requeue, the REST
surface, the two MCP tools, and tag-addressed memory search (§3.4)."""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "gateway"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from ai_common_repo.core.config import load_config  # noqa: E402
from ai_common_repo.core.models import (  # noqa: E402
    HarnessClaimRequest, HarnessStatusUpdate, HarnessTaskCreate, SearchRequest,
    StoreMemoryRequest,
)
from ai_common_repo.server.app import create_app  # noqa: E402
from ai_common_repo.service import MemoryService  # noqa: E402


def _service() -> MemoryService:
    tmp = tempfile.mkdtemp()
    os.environ["AICR_STORAGE__DB_PATH"] = os.path.join(tmp, "memory.db")
    os.environ["AICR_MODELS__DEFAULT_BACKEND"] = "none"  # offline => hash embedder
    return MemoryService(load_config())


def _create(svc, **kw) -> str:
    defaults = dict(requester="hermes", executor="claudecode",
                    prompt="Read request.md and process it.")
    defaults.update(kw)
    return svc.harness.create(HarnessTaskCreate(**defaults)).id


def _claim(svc, executor="claudecode", worker="w1"):
    return svc.harness.claim(HarnessClaimRequest(executor=executor, worker_id=worker))


# -- lifecycle ----------------------------------------------------------------
def test_full_lifecycle_pending_claimed_running_done():
    svc = _service()
    task_id = _create(svc, task_slug="t-life")
    assert svc.harness.get(task_id).status == "pending"

    claimed = _claim(svc)
    assert claimed.id == task_id and claimed.status == "claimed"
    assert claimed.claimed_by == "w1" and claimed.claimed_at

    running = svc.harness.update_status(
        task_id, HarnessStatusUpdate(status="running", worker_id="w1"))
    assert running.status == "running"

    done = svc.harness.update_status(task_id, HarnessStatusUpdate(
        status="done", worker_id="w1", result={"summary": "ok", "exit_code": 0}))
    assert done.status == "done"
    assert done.result == {"summary": "ok", "exit_code": 0}


def test_claim_is_exclusive_and_respects_priority_and_executor():
    svc = _service()
    low = _create(svc, priority=0)
    high = _create(svc, priority=5)
    other = _create(svc, executor="openclaw")

    first, second = _claim(svc, worker="w1"), _claim(svc, worker="w2")
    assert first.id == high and second.id == low  # priority order, no double-claim
    assert _claim(svc, worker="w3") is None       # claudecode queue drained

    assert _claim(svc, executor="openclaw").id == other


def test_invalid_transitions_and_wrong_worker_are_rejected():
    svc = _service()
    task_id = _create(svc)
    with pytest.raises(ValueError):  # pending cannot jump straight to done
        svc.harness.update_status(task_id, HarnessStatusUpdate(status="done"))
    _claim(svc, worker="w1")
    with pytest.raises(ValueError):  # only the claiming worker may report
        svc.harness.update_status(
            task_id, HarnessStatusUpdate(status="running", worker_id="intruder"))
    svc.harness.update_status(task_id, HarnessStatusUpdate(status="cancelled",
                                                           worker_id="w1"))
    with pytest.raises(ValueError):  # terminal states are final
        svc.harness.update_status(
            task_id, HarnessStatusUpdate(status="running", worker_id="w1"))


def test_unknown_executor_rejected_on_create_and_claim():
    svc = _service()
    with pytest.raises(ValueError):
        _create(svc, executor="nonsense")
    with pytest.raises(ValueError):
        _claim(svc, executor="nonsense")


def test_stale_claim_requeues_once_then_stays_failed_out():
    svc = _service()
    task_id = _create(svc, timeout_s=60)
    _claim(svc, worker="w-dead")

    # Backdate the claim beyond stale_multiplier x timeout_s.
    repo = svc.harness._repo
    repo._db.conn.execute(
        "UPDATE harness_tasks SET claimed_at='2020-01-01T00:00:00Z' WHERE id=?",
        (task_id,))
    repo._db.conn.commit()

    reclaimed = _claim(svc, worker="w-new")  # requeue happens lazily inside claim
    assert reclaimed.id == task_id and reclaimed.claimed_by == "w-new"
    assert svc.harness.get(task_id).requeues == 1

    # A second stale death is NOT requeued (max_requeues=1) — no auto-retries.
    repo._db.conn.execute(
        "UPDATE harness_tasks SET claimed_at='2020-01-01T00:00:00Z' WHERE id=?",
        (task_id,))
    repo._db.conn.commit()
    assert _claim(svc, worker="w3") is None
    assert svc.harness.get(task_id).status == "claimed"  # stranded, requester decides


def test_list_filters_by_status_and_executor():
    svc = _service()
    _create(svc)
    done_id = _create(svc)
    _create(svc, executor="opencode")
    claimed = _claim(svc)
    assert claimed.id  # first created claudecode task (fifo at equal priority)
    svc.harness.update_status(claimed.id, HarnessStatusUpdate(status="running"))
    svc.harness.update_status(claimed.id, HarnessStatusUpdate(status="done"))
    del done_id

    assert {t.status for t in svc.harness.list(executor="claudecode")} == {
        "pending", "done"}
    assert len(svc.harness.list(status="pending")) == 2
    with pytest.raises(ValueError):
        svc.harness.list(status="bogus")


# -- REST surface --------------------------------------------------------------
def test_rest_endpoints_roundtrip():
    app = create_app(_service().cfg)
    with TestClient(app) as client:
        r = client.post("/v1/harness/tasks", json={
            "requester": "hermes", "executor": "claudecode",
            "prompt": "Read handoff request", "task_slug": "t-rest"})
        assert r.status_code == 200
        task_id = r.json()["task_id"]

        assert client.post("/v1/harness/tasks", json={
            "requester": "x", "executor": "bogus", "prompt": "p"}).status_code == 422

        r = client.post("/v1/harness/tasks/claim",
                        json={"executor": "claudecode", "worker_id": "w1"})
        assert r.json()["task"]["id"] == task_id
        assert client.post("/v1/harness/tasks/claim",
                           json={"executor": "claudecode",
                                 "worker_id": "w2"}).json()["task"] is None

        r = client.post(f"/v1/harness/tasks/{task_id}/status",
                        json={"status": "running", "worker_id": "w1"})
        assert r.status_code == 200
        r = client.post(f"/v1/harness/tasks/{task_id}/status",
                        json={"status": "done", "worker_id": "w1",
                              "result": {"summary": "ok"}})
        assert r.json()["task"]["result"] == {"summary": "ok"}

        assert client.post(f"/v1/harness/tasks/{task_id}/status",
                           json={"status": "running"}).status_code == 409
        assert client.get("/v1/harness/tasks/task_missing").status_code == 404

        r = client.get("/v1/harness/tasks", params={"status": "done"})
        assert [t["id"] for t in r.json()["tasks"]] == [task_id]


# -- MCP tools ------------------------------------------------------------------
def test_mcp_harness_request_and_result():
    app = create_app(_service().cfg)
    with TestClient(app) as client:
        def call(name, arguments):
            return client.post("/mcp", json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": name, "arguments": arguments}}).json()["result"]

        res = call("harness_request", {"executor": "claudecode",
                                       "prompt": "do the thing"})
        assert res["isError"] is False
        task_id = res["structuredContent"]["task_id"]

        res = call("harness_result", {"task_id": task_id})
        assert res["structuredContent"]["status"] == "pending"

        res = call("harness_result", {"task_id": "task_missing"})
        assert res["isError"] is True  # domain error, not protocol error

        res = call("harness_request", {"executor": "bogus", "prompt": "p"})
        assert res["isError"] is True


# -- tag-addressed search (spec §3.4) -------------------------------------------
def test_search_tags_require_all_and_exclude_chunks():
    svc = _service()
    svc.store_memory(StoreMemoryRequest(
        content="t-slug:result — queue implemented", project_id="p1",
        tags=["harness", "t-slug", "result"]))
    svc.store_memory(StoreMemoryRequest(
        content="t-slug:risk — none found", project_id="p1",
        tags=["harness", "t-slug", "risk"]))

    hits = svc.search(SearchRequest(q="t-slug", project_id="p1",
                                    tags=["harness", "t-slug", "result"]))
    assert len(hits) == 1 and "queue implemented" in hits[0]["content"]

    hits = svc.search(SearchRequest(q="t-slug", project_id="p1",
                                    tags=["harness", "t-slug"]))
    assert len(hits) == 2

    # hybrid mode must honor the same tag filter on the vector path
    hits = svc.search(SearchRequest(q="t-slug", project_id="p1",
                                    search_mode="hybrid",
                                    tags=["harness", "t-slug", "risk"]))
    assert len(hits) == 1 and "risk" in hits[0]["content"]


if __name__ == "__main__":
    test_full_lifecycle_pending_claimed_running_done()
    print("HARNESS QUEUE OK")
