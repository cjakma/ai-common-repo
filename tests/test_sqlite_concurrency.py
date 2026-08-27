"""Concurrency regression tests for the shared SQLite connection.

The production gateway uses one SQLite connection shared by FastAPI's sync
thread pool and the background workers. These tests exercise the HTTP surface,
not just repository methods, so the same app/service instance is hit by
multiple request threads.
"""
from __future__ import annotations

import concurrent.futures
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "gateway"))

from fastapi.testclient import TestClient  # noqa: E402

from ai_common_repo.core.config import load_config  # noqa: E402
from ai_common_repo.server.app import create_app  # noqa: E402


def _app():
    tmp = tempfile.mkdtemp()
    os.environ["AICR_STORAGE__DB_PATH"] = os.path.join(tmp, "memory.db")
    os.environ["AICR_MODELS__DEFAULT_BACKEND"] = "none"
    cfg = load_config()
    # Keep the test focused on concurrent HTTP requests rather than lifecycle
    # worker timing. The same service instance still owns the shared Database.
    cfg.workers.enabled = False
    cfg.notifications.enabled = False
    return create_app(cfg)


def _parallel_post(client: TestClient, path: str, payloads: list[dict],
                   max_workers: int = 16):
    def post(payload: dict):
        response = client.post(path, json=payload)
        return response.status_code, response.json()

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        return list(pool.map(post, payloads))


def test_concurrent_http_memory_posts_have_no_sqlite_errors():
    app = _app()
    payloads = [
        {
            # Use disjoint lexical tokens so conflict resolution cannot classify
            # a successful concurrent write as a duplicate.
            "content": f"sqlitepayload{i:03d} uniquefact{i:03d} distinctvalue{i:03d}",
            "type": "project_fact",
            "scope": "project",
            "project_id": "repo:sqlite-concurrency",
            "tags": ["concurrency", f"item-{i}"],
        }
        for i in range(48)
    ]

    with TestClient(app) as client:
        results = _parallel_post(client, "/v1/memory", payloads)

    assert [status for status, _ in results] == [200] * len(payloads)
    bodies = [body for _, body in results]
    assert all(body["stored"] is True for body in bodies), bodies
    assert len({body["id"] for body in bodies}) == len(payloads)

    memories = app.state.service.memories.active_for_scope(
        "painmaster", "repo:sqlite-concurrency", limit=100)
    assert len(memories) == len(payloads)


def test_concurrent_http_harness_claims_are_exclusive_and_audited():
    app = _app()
    task_count = 48
    task_payload = {
        "requester": "hermes",
        "executor": "claudecode",
        "prompt": "concurrency regression task",
    }

    with TestClient(app) as client:
        task_ids = [
            client.post(
                "/v1/harness/tasks",
                json={**task_payload, "task_slug": f"sqlite-claim-{i}"},
            ).json()["task_id"]
            for i in range(task_count)
        ]
        results = _parallel_post(
            client,
            "/v1/harness/tasks/claim",
            [
                {"executor": "claudecode", "worker_id": f"worker-{i}"}
                for i in range(task_count)
            ],
        )

    assert [status for status, _ in results] == [200] * task_count
    claimed_ids = [body["task"]["id"] for _, body in results]
    assert len(claimed_ids) == task_count
    assert set(claimed_ids) == set(task_ids)
    assert len(set(claimed_ids)) == task_count

    remaining = app.state.service.harness.list(
        status="pending", executor="claudecode")
    assert remaining == []
