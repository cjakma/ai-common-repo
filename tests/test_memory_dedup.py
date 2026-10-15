"""Phase 8 — near-duplicate memory consolidation. The periodic dedup job merges
embedding-similar active memories under the strongest one; distinct memories are
left untouched (no false merges). All offline (hash embedder)."""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "gateway"))

from ai_common_repo.core import constants as C  # noqa: E402
from ai_common_repo.core.config import load_config  # noqa: E402
from ai_common_repo.core.models import SearchRequest, StoreMemoryRequest  # noqa: E402
from ai_common_repo.resolver.dedup import cluster_by_cosine  # noqa: E402
from ai_common_repo.service import MemoryService  # noqa: E402


def _service(**memory_overrides) -> MemoryService:
    tmp = tempfile.mkdtemp()
    os.environ["AICR_STORAGE__DB_PATH"] = os.path.join(tmp, "memory.db")
    os.environ["AICR_MODELS__DEFAULT_BACKEND"] = "none"
    cfg = load_config()
    cfg.storage.vector_backend = "bruteforce"
    # Disable insert-time conflict resolution so the duplicates actually persist
    # and the *job* is what consolidates them.
    cfg.memory.enable_conflict_resolution = False
    for k, v in memory_overrides.items():
        setattr(cfg.memory, k, v)
    return MemoryService(cfg)


def _status(svc: MemoryService, mem_id: str) -> str:
    return svc.memories.by_ids([mem_id])[mem_id].status


# -- clustering primitive ----------------------------------------------------
def test_cluster_by_cosine_groups_only_similar():
    a = [1.0, 0.0, 0.0]
    b = [0.99, 0.01, 0.0]   # ~identical direction
    c = [0.0, 1.0, 0.0]     # orthogonal
    clusters = cluster_by_cosine([a, b, c], threshold=0.95)
    sizes = sorted(len(c) for c in clusters)
    assert sizes == [1, 2]  # {a,b} merged, {c} alone


# -- job behaviour -----------------------------------------------------------
def test_dedup_merges_duplicates_keeps_strongest():
    svc = _service(dedup_enabled=True)
    keep = svc.store_memory(StoreMemoryRequest(
        content="We deploy on Fridays", type="workflow", scope="project",
        project_id="repo:dd", confidence=0.9))
    drop = svc.store_memory(StoreMemoryRequest(
        content="We deploy on Fridays.", type="workflow", scope="project",
        project_id="repo:dd", confidence=0.6))
    distinct = svc.store_memory(StoreMemoryRequest(
        content="The database uses connection pooling", type="project_fact",
        scope="project", project_id="repo:dd", confidence=0.8))

    svc._job_dedup_memories()

    assert _status(svc, keep.id) == C.MEMORY_STATUS_ACTIVE        # highest conf wins
    assert _status(svc, drop.id) == C.MEMORY_STATUS_SUPERSEDED    # duplicate merged
    assert _status(svc, distinct.id) == C.MEMORY_STATUS_ACTIVE    # no false merge
    # the superseded memory's vector is gone from the index
    hits = svc.search(SearchRequest(q="deploy on fridays", project_id="repo:dd",
                                    search_mode="vector"))
    assert all(h["id"] != drop.id for h in hits)


def test_dedup_disabled_is_noop():
    svc = _service(dedup_enabled=False)
    a = svc.store_memory(StoreMemoryRequest(
        content="Identical fact here", scope="project", project_id="repo:dd2"))
    b = svc.store_memory(StoreMemoryRequest(
        content="Identical fact here", scope="project", project_id="repo:dd2"))
    svc._job_dedup_memories()
    assert _status(svc, a.id) == C.MEMORY_STATUS_ACTIVE
    assert _status(svc, b.id) == C.MEMORY_STATUS_ACTIVE


def test_dedup_does_not_cross_scope_boundaries():
    # Same content but different visibility scope => never merged.
    svc = _service(dedup_enabled=True)
    u = svc.store_memory(StoreMemoryRequest(
        content="Shared platform note", scope="user", project_id="repo:dd3"))
    p = svc.store_memory(StoreMemoryRequest(
        content="Shared platform note", scope="project", project_id="repo:dd3"))
    svc._job_dedup_memories()
    assert _status(svc, u.id) == C.MEMORY_STATUS_ACTIVE
    assert _status(svc, p.id) == C.MEMORY_STATUS_ACTIVE


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print("OK", _name)
