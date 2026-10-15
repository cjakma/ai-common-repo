"""Phase 6 — Qdrant as the canonical vector backend. All offline: an in-memory
QdrantClient(location=":memory:") runs the exact production code path with no
server or network."""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "gateway"))

from ai_common_repo.core import constants as C  # noqa: E402
from ai_common_repo.core.config import StorageConfig, load_config  # noqa: E402
from ai_common_repo.core.models import (  # noqa: E402
    DocumentIngestRequest, SearchRequest, StoreMemoryRequest,
)
from ai_common_repo.model_backends.hash_embedding import HashEmbeddingBackend  # noqa: E402
from ai_common_repo.search.vector import (  # noqa: E402
    QdrantVectorIndex, build_vector_index,
)
from ai_common_repo.service import MemoryService  # noqa: E402
from ai_common_repo.storage.database import Database  # noqa: E402
from ai_common_repo.storage.repositories import EmbeddingRepository  # noqa: E402


def _memory_index(dim: int = 64) -> QdrantVectorIndex:
    storage = StorageConfig(vector_backend=C.VECTOR_BACKEND_QDRANT)  # url unset => :memory:
    repo = EmbeddingRepository(Database(os.path.join(tempfile.mkdtemp(), "m.db")))
    idx = build_vector_index(storage, repo, dim=dim)
    assert isinstance(idx, QdrantVectorIndex)
    return idx


def _service(**memory_overrides) -> MemoryService:
    tmp = tempfile.mkdtemp()
    os.environ["AICR_STORAGE__DB_PATH"] = os.path.join(tmp, "memory.db")
    os.environ["AICR_MODELS__DEFAULT_BACKEND"] = "none"  # offline => hash embedder
    cfg = load_config()
    for k, v in memory_overrides.items():
        setattr(cfg.memory, k, v)
    return MemoryService(cfg)


# -- factory default ---------------------------------------------------------
def test_default_backend_is_qdrant():
    assert StorageConfig().vector_backend == C.VECTOR_BACKEND_QDRANT
    idx = _service().vector_index
    assert isinstance(idx, QdrantVectorIndex)


# -- index lifecycle ---------------------------------------------------------
def test_query_before_any_write_is_empty():
    idx = _memory_index()
    # No collection yet — must not raise, just return nothing.
    assert idx.query([0.1] * 64, [C.OBJECT_TYPE_MEMORY], 5) == []


def test_index_query_ranks_by_cosine():
    idx = _memory_index(dim=128)
    emb = HashEmbeddingBackend(dim=128)
    for oid, text in [("m1", "redis cache layer"),
                      ("m2", "postgres primary store"),
                      ("m3", "redis caching strategy")]:
        idx.index(C.OBJECT_TYPE_MEMORY, oid, emb.embed([text])[0], model=emb.model)

    hits = idx.query(emb.embed(["redis cache"])[0], [C.OBJECT_TYPE_MEMORY], 3)
    ids = [oid for _, oid, _ in hits]
    assert ids[0] in {"m1", "m3"} and ids[-1] == "m2"


def test_object_type_filter_excludes_other_types():
    idx = _memory_index(dim=64)
    emb = HashEmbeddingBackend(dim=64)
    idx.index(C.OBJECT_TYPE_MEMORY, "m1", emb.embed(["alpha"])[0], model=emb.model)
    idx.index(C.OBJECT_TYPE_CHUNK, "c1", emb.embed(["alpha"])[0], model=emb.model)

    only_mem = idx.query(emb.embed(["alpha"])[0], [C.OBJECT_TYPE_MEMORY], 5)
    assert {ot for ot, _, _ in only_mem} == {C.OBJECT_TYPE_MEMORY}
    both = idx.query(emb.embed(["alpha"])[0],
                     [C.OBJECT_TYPE_MEMORY, C.OBJECT_TYPE_CHUNK], 5)
    assert {ot for ot, _, _ in both} == {C.OBJECT_TYPE_MEMORY, C.OBJECT_TYPE_CHUNK}


def test_reindex_replaces_and_delete_removes():
    idx = _memory_index(dim=64)
    emb = HashEmbeddingBackend(dim=64)
    idx.index(C.OBJECT_TYPE_MEMORY, "m1", emb.embed(["first"])[0], model=emb.model)
    # Same (type,id) => same point id => in-place replace, not a duplicate.
    idx.index(C.OBJECT_TYPE_MEMORY, "m1", emb.embed(["second"])[0], model=emb.model)
    hits = idx.query(emb.embed(["second"])[0], [C.OBJECT_TYPE_MEMORY], 5)
    assert [oid for _, oid, _ in hits] == ["m1"]

    idx.delete(C.OBJECT_TYPE_MEMORY, "m1")
    assert idx.query(emb.embed(["second"])[0], [C.OBJECT_TYPE_MEMORY], 5) == []


# -- end-to-end through the service (acceptance) -----------------------------
def test_service_vector_search_via_qdrant():
    svc = _service()
    svc.store_memory(StoreMemoryRequest(
        content="Use PostgreSQL for the primary datastore.",
        type="project_fact", scope="project", project_id="repo:x"))
    svc.ingest_document(DocumentIngestRequest(
        content="The deployment runs on Kubernetes with autoscaling.",
        title="ops", project_id="repo:x"))

    hits = svc.search(SearchRequest(q="postgres database", project_id="repo:x",
                                    search_mode="vector"))
    assert any("PostgreSQL" in h["content"] for h in hits), hits

    hybrid = svc.search(SearchRequest(q="kubernetes", project_id="repo:x",
                                      search_mode="hybrid"))
    types = {h["object_type"] for h in hybrid}
    assert C.OBJECT_TYPE_CHUNK in types, hybrid


# -- backfill ----------------------------------------------------------------
def test_backfill_copies_legacy_embeddings():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from scripts.backfill_qdrant import backfill

    tmp = tempfile.mkdtemp()
    os.environ["AICR_STORAGE__DB_PATH"] = os.path.join(tmp, "memory.db")
    os.environ["AICR_MODELS__DEFAULT_BACKEND"] = "none"
    cfg = load_config()

    # Seed the legacy sqlite embeddings table directly.
    db = Database(cfg.storage.db_file)
    repo = EmbeddingRepository(db)
    emb = HashEmbeddingBackend(dim=cfg.embedding.dim)
    repo.upsert(C.OBJECT_TYPE_MEMORY, "m1", emb.model, emb.embed(["redis"])[0])
    repo.upsert(C.OBJECT_TYPE_CHUNK, "c1", emb.model, emb.embed(["postgres"])[0])

    # Backfill builds its own (in-memory) Qdrant index; assert it copied both.
    copied = backfill(cfg)
    assert copied == 2


def test_backfill_noop_on_empty():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from scripts.backfill_qdrant import backfill

    tmp = tempfile.mkdtemp()
    os.environ["AICR_STORAGE__DB_PATH"] = os.path.join(tmp, "memory.db")
    os.environ["AICR_MODELS__DEFAULT_BACKEND"] = "none"
    cfg = load_config()
    assert backfill(cfg) == 0


# allow direct execution like the other suites
if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print("OK", _name)


def test_forget_drops_the_vector_point():
    svc = _service()
    mem = svc.store_memory(StoreMemoryRequest(
        content="qdrant forget check: rotate the staging certificate monthly",
        scope="project", project_id="repo:qforget"))
    qvec = svc.embedder.embed([mem.content])[0]
    ids = [oid for _, oid, _ in svc.vector_index.query(qvec, [C.OBJECT_TYPE_MEMORY], 5)]
    assert mem.id in ids
    svc.forget(mem.id)
    ids = [oid for _, oid, _ in svc.vector_index.query(qvec, [C.OBJECT_TYPE_MEMORY], 5)]
    assert mem.id not in ids
