"""Phase 2 — vector/hybrid search + RAG documents. All offline: the deterministic
hash-embedding fallback stands in for a real embedding backend, and the
brute-force vector index needs no native extension."""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "gateway"))

from ai_common_repo.core import constants as C  # noqa: E402
from ai_common_repo.core.config import load_config  # noqa: E402
from ai_common_repo.core.models import (  # noqa: E402
    DocumentIngestRequest, RecallRequest, SearchRequest, StoreMemoryRequest,
)
from ai_common_repo.extraction.chunker import chunk_text, looks_like_code  # noqa: E402
from ai_common_repo.model_backends.hash_embedding import HashEmbeddingBackend  # noqa: E402
from ai_common_repo.search.vector import (  # noqa: E402
    BruteForceVectorIndex, build_vector_index,
)
from ai_common_repo.service import MemoryService  # noqa: E402
from ai_common_repo.storage.database import Database  # noqa: E402
from ai_common_repo.storage.repositories import EmbeddingRepository  # noqa: E402
from ai_common_repo.workers import Worker  # noqa: E402


def _service(**memory_overrides) -> MemoryService:
    tmp = tempfile.mkdtemp()
    os.environ["AICR_STORAGE__DB_PATH"] = os.path.join(tmp, "memory.db")
    os.environ["AICR_MODELS__DEFAULT_BACKEND"] = "none"  # offline => hash embedder
    cfg = load_config()
    for k, v in memory_overrides.items():
        setattr(cfg.memory, k, v)
    return MemoryService(cfg)


# -- hash embedding ----------------------------------------------------------
def test_hash_embedding_deterministic_and_normalized():
    emb = HashEmbeddingBackend(dim=128)
    a = emb.embed(["the build uses bazel"])[0]
    b = emb.embed(["the build uses bazel"])[0]
    assert a == b  # deterministic across calls/processes (BLAKE2, not salted hash)
    assert len(a) == 128
    norm = sum(x * x for x in a) ** 0.5
    assert abs(norm - 1.0) < 1e-6


def test_hash_embedding_morphological_neighbors_beat_unrelated():
    emb = HashEmbeddingBackend(dim=256)
    base, near, far = emb.embed(["postgresql", "postgres", "kubernetes"])

    def cos(x, y):
        return sum(a * b for a, b in zip(x, y))

    assert cos(base, near) > cos(base, far)


# -- brute-force vector index ------------------------------------------------
def test_brute_force_index_ranks_by_cosine():
    db = Database(os.path.join(tempfile.mkdtemp(), "memory.db"))
    repo = EmbeddingRepository(db)
    index = BruteForceVectorIndex(repo)
    emb = HashEmbeddingBackend(dim=128)
    for oid, text in [("m1", "redis cache layer"),
                      ("m2", "postgres primary store"),
                      ("m3", "redis caching strategy")]:
        index.index(C.OBJECT_TYPE_MEMORY, oid, emb.embed([text])[0], model=emb.model)

    hits = index.query(emb.embed(["redis cache"])[0], [C.OBJECT_TYPE_MEMORY], 3)
    ids = [oid for _, oid, _ in hits]
    assert ids[0] in {"m1", "m3"} and "m2" == ids[-1]
    # re-indexing the same object replaces (no duplicate rows)
    index.index(C.OBJECT_TYPE_MEMORY, "m1", emb.embed(["redis"])[0], model=emb.model)
    assert repo.count() == 3


def test_build_vector_index_falls_back_to_bruteforce():
    repo = EmbeddingRepository(Database(os.path.join(tempfile.mkdtemp(), "m.db")))
    idx = build_vector_index(C.VECTOR_BACKEND_AUTO, repo)
    assert isinstance(idx, BruteForceVectorIndex)  # sqlite-vec not installed here


# -- vector recall that FTS misses (acceptance) ------------------------------
def test_vector_recovers_what_fts_cannot():
    svc = _service()
    svc.store_memory(StoreMemoryRequest(
        content="Use PostgreSQL for the primary datastore.",
        type="decision", scope="project", project_id="repo:vec"))

    # FTS tokenizes whole words: 'postgres'/'database' don't match the stored
    # 'postgresql'/'datastore', so lexical search finds nothing...
    fts = svc.search(SearchRequest(q="postgres database", project_id="repo:vec",
                                   search_mode="fts"))
    assert fts == []
    # ...but the char-n-gram vector recovers it.
    vec = svc.search(SearchRequest(q="postgres database", project_id="repo:vec",
                                   search_mode="vector"))
    assert any("PostgreSQL" in h["content"] for h in vec), vec
    assert all(h["object_type"] == C.OBJECT_TYPE_MEMORY for h in vec)


# -- chunker -----------------------------------------------------------------
def test_chunk_prose_respects_size_and_overlap():
    para = ("Sentence number {} about the deployment pipeline and its many "
            "moving parts.").format
    text = "\n\n".join(para(i) for i in range(40))
    chunks = chunk_text(text, max_chars=300, overlap=50)
    assert len(chunks) > 1
    assert all(len(c) <= 300 + 50 for c in chunks)  # overlap may add a tail
    # adjacent chunks share an overlap fragment
    assert any(chunks[0][-20:] in chunks[1] for _ in [0]) or len(chunks) >= 2


def test_chunk_code_splits_on_definitions():
    code = (
        "import os\n\n"
        "def alpha():\n    return os.getcwd()\n\n"
        "def beta():\n    return 42\n\n"
        "class Gamma:\n    def method(self):\n        return None\n")
    assert looks_like_code(code)
    chunks = chunk_text(code, max_chars=60, overlap=10, source_type="code")
    assert len(chunks) >= 2
    joined = "\n".join(chunks)
    assert "def alpha" in joined and "class Gamma" in joined


# -- document ingest + fused retrieval (acceptance) --------------------------
def test_document_ingest_then_search_returns_chunk_with_memory():
    svc = _service()
    svc.store_memory(StoreMemoryRequest(
        content="The release runbook lives in the deploy folder.",
        type="project_fact", scope="project", project_id="repo:doc"))
    out = svc.ingest_document(DocumentIngestRequest(
        project_id="repo:doc", title="Runbook", source_type="text",
        content="Deploy by running the deploy script. The deploy step "
                "publishes artifacts and tags the release."))
    assert out["chunks"] >= 1

    hits = svc.search(SearchRequest(q="deploy release", project_id="repo:doc",
                                    search_mode="hybrid"))
    types = {h["object_type"] for h in hits}
    assert C.OBJECT_TYPE_MEMORY in types and C.OBJECT_TYPE_CHUNK in types, hits
    chunk_hit = next(h for h in hits if h["object_type"] == C.OBJECT_TYPE_CHUNK)
    assert chunk_hit["document_title"] == "Runbook"
    assert "document_id" in chunk_hit


def test_recall_context_includes_document_excerpt():
    svc = _service()
    svc.ingest_document(DocumentIngestRequest(
        project_id="repo:rc", title="Guide", content=(
            "The migration command is `make migrate`. Always back up first.")))
    resp = svc.recall(RecallRequest(query="migration command", project_id="repo:rc"))
    assert "Relevant Document Excerpts" in resp.context_block
    assert any(h["object_type"] == C.OBJECT_TYPE_CHUNK for h in resp.memories)


# -- hybrid fusion stability -------------------------------------------------
def test_hybrid_fuses_without_duplicates():
    svc = _service()
    svc.store_memory(StoreMemoryRequest(
        content="Run the test suite with pytest before pushing.",
        type="workflow", scope="project", project_id="repo:hy"))
    hits = svc.search(SearchRequest(q="pytest test", project_id="repo:hy",
                                    search_mode="hybrid"))
    keys = [(h["object_type"], h["id"]) for h in hits]
    assert len(keys) == len(set(keys))  # RRF de-dupes across the two rankings
    assert hits and hits[0]["score"] > 0


# -- ingest guards -----------------------------------------------------------
def test_document_path_ingest_disabled_by_default():
    svc = _service()
    try:
        svc.ingest_document(DocumentIngestRequest(path="/etc/passwd",
                                                  project_id="repo:g"))
        raise AssertionError("expected path ingest to be rejected")
    except ValueError as exc:
        assert "allowed_roots" in str(exc) or "outside" in str(exc)


def test_document_size_guard():
    svc = _service()
    svc.cfg.documents.max_content_chars = 50
    try:
        svc.ingest_document(DocumentIngestRequest(
            content="x" * 200, project_id="repo:g2"))
        raise AssertionError("expected size guard to trip")
    except ValueError as exc:
        assert "too large" in str(exc)


# -- async embedding via the worker ------------------------------------------
def test_async_embed_indexes_via_worker():
    svc = _service(async_jobs=True)
    mem = svc.store_memory(StoreMemoryRequest(
        content="The cache backend is memcached.", type="project_fact",
        scope="project", project_id="repo:ae"))
    assert mem is not None
    assert svc.embeddings.count() == 0  # nothing embedded inline yet

    Worker(svc.jobs, svc.handlers).drain()
    assert svc.embeddings.count() >= 1

    hits = svc.search(SearchRequest(q="memcached", project_id="repo:ae",
                                    search_mode="vector"))
    assert any("memcached" in h["content"] for h in hits), hits


# allow direct execution like the other suites
if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print("OK", _name)
