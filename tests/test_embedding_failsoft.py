"""Embedding backend failures degrade instead of failing requests: search falls
back to FTS, stores still succeed (the vector is filled later by
scripts/reembed.py), and /v1/health reports the vector store + embedding
backend. Also covers the OpenAI-compatible client's prompts and truncation."""
from __future__ import annotations

import os
import sys
import tempfile

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "gateway"))

from ai_common_repo.core import constants as C  # noqa: E402
from ai_common_repo.core.config import BackendConfig, load_config  # noqa: E402
from ai_common_repo.core.models import (  # noqa: E402
    RecallRequest, SearchRequest, StoreMemoryRequest,
)
from ai_common_repo.model_backends.openai_compatible import (  # noqa: E402
    OpenAICompatibleBackend,
)
from ai_common_repo.service import MemoryService  # noqa: E402


class _DownEmbedder:
    name = "down"
    model = "down-model"

    def embed(self, texts):
        raise httpx.ConnectError("embedding backend unreachable")

    embed_query = embed


def _service() -> MemoryService:
    tmp = tempfile.mkdtemp()
    os.environ["AICR_STORAGE__DB_PATH"] = os.path.join(tmp, "memory.db")
    os.environ["AICR_MODELS__DEFAULT_BACKEND"] = "none"
    return MemoryService(load_config())


def _break_embedder(svc: MemoryService) -> None:
    down = _DownEmbedder()
    svc.embedder = down
    svc.search_svc._embedder = down


def test_store_succeeds_and_search_falls_back_to_fts_when_embedding_is_down():
    svc = _service()
    _break_embedder(svc)
    mem = svc.store_memory(StoreMemoryRequest(
        content="Rotate the staging certificate every month.",
        scope="project", project_id="repo:failsoft"))
    assert mem is not None  # stored although the vector could not be made
    for mode in (C.SEARCH_MODE_HYBRID, C.SEARCH_MODE_FTS):
        hits = svc.search(SearchRequest(q="staging certificate",
                                        project_id="repo:failsoft", search_mode=mode))
        assert any(h["id"] == mem.id for h in hits)
    assert svc.search(SearchRequest(q="staging certificate", project_id="repo:failsoft",
                                    search_mode=C.SEARCH_MODE_VECTOR)) == []
    recall = svc.recall(RecallRequest(query="staging certificate", project_id="repo:failsoft"))
    assert "staging certificate" in recall.context_block


def test_health_reports_vector_store_and_embedding():
    svc = _service()
    svc.store_memory(StoreMemoryRequest(content="health probe memory",
                                        scope="project", project_id="repo:h"))
    health = svc.health()
    comps = {c.name: c for c in health.components}
    assert comps["vector_store"].status == "ok"
    assert comps["vector_store"].info["backend"] == "qdrant"
    assert comps["vector_store"].info["points"] == 1
    assert comps["embedding"].status == "ok"
    assert health.status == "ok" and health.ready

    _break_embedder(svc)

    def _down_health():
        from ai_common_repo.model_backends.base import BackendHealth
        return BackendHealth("down", "openai_compatible", "http://x", "unreachable", [], "boom")
    svc.embedder.health = _down_health
    health = svc.health()
    comps = {c.name: c for c in health.components}
    assert comps["embedding"].status == "down"
    assert health.status == "degraded" and health.ready  # FTS still answers


def test_openai_backend_applies_prompts_and_truncation(monkeypatch):
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        body = json.loads(request.content)
        sent.append(body)
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2]}
                                                  for _ in body["input"]]})

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(
        transport=httpx.MockTransport(handler), **kw))
    backend = OpenAICompatibleBackend("emb", BackendConfig(
        base_url="http://emb/v1", embedding_model="m",
        embedding_query_prefix="Q: ", embedding_document_prefix="D: ",
        embedding_max_chars=5))
    backend.embed(["abcdefgh"])
    backend.embed_query(["xyz"])
    assert sent[0]["input"] == ["D: abcde"]
    assert sent[1]["input"] == ["Q: xyz"]
    assert sent[0]["model"] == "m" and backend.model == "m"
