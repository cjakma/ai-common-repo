"""Offline smoke test: exercises the full service path with a temp DB and no
model backend (heuristic extraction). Run: PYTHONPATH=packages/gateway pytest -q
or `python tests/test_smoke.py`."""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "gateway"))

from ai_common_repo.core.config import load_config  # noqa: E402
from ai_common_repo.core.models import (  # noqa: E402
    RecallRequest, SearchRequest, SessionIngestRequest, StoreMemoryRequest,
)
from ai_common_repo.service import MemoryService  # noqa: E402


def _service() -> MemoryService:
    tmp = tempfile.mkdtemp()
    os.environ["AICR_STORAGE__DB_PATH"] = os.path.join(tmp, "memory.db")
    # force offline heuristic extraction
    os.environ["AICR_MODELS__DEFAULT_BACKEND"] = "none"
    return MemoryService(load_config())


def test_store_recall_search_profile_ingest():
    svc = _service()

    mem = svc.store_memory(StoreMemoryRequest(
        content="This project must use uv for dependency management.",
        type="constraint", scope="project", project_id="repo:test"))
    assert mem.id.startswith("mem_")

    # dedupe: storing a near-identical memory should not create a second one
    dup = svc.store_memory(StoreMemoryRequest(
        content="This project must use uv for dependency management.",
        type="constraint", scope="project", project_id="repo:test"))
    assert dup is None  # conflict resolver recognized the duplicate

    res = svc.search(SearchRequest(q="uv dependency", project_id="repo:test"))
    assert any("uv" in r["content"] for r in res), res

    recall = svc.recall(RecallRequest(query="dependency rules", project_id="repo:test"))
    assert "Persistent Context" in recall.context_block

    out = svc.ingest_session(SessionIngestRequest.model_validate({
        "schema": "ai-common-repo.session.v1", "tool": "codex", "project_id": "repo:test",
        "messages": [{"role": "user", "content": "We must never commit secrets to git."}],
    }))
    assert out["accepted"] is True

    prof = svc.get_profile(None, "repo:test")
    assert prof.stable or prof.dynamic
    print("SMOKE OK:", {"stored": mem.id, "ingest": out, "profile_stable": prof.stable})


if __name__ == "__main__":
    test_store_recall_search_profile_ingest()


def test_fts_operator_words_are_plain_terms():
    # Regression: uppercase AND/OR/NOT/NEAR in a query used to reach FTS5 as
    # operators and fail with "fts5: syntax error" (500 on /v1/recall).
    svc = _service()
    svc.store_memory(StoreMemoryRequest(
        content="Deploy is NOT allowed on Fridays.",
        type="constraint", scope="project", project_id="repo:fts"))
    for q in ["deploy NOT working", "use AND or", "NEAR the gateway", "NOT", "OR"]:
        svc.search(SearchRequest(q=q, project_id="repo:fts"))
        svc.recall(RecallRequest(query=q, project_id="repo:fts"))
    hits = svc.search(SearchRequest(q="NOT allowed", project_id="repo:fts"))
    assert any("Fridays" in h["content"] for h in hits)
