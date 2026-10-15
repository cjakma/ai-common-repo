"""Phase 7 — fallback vector backends smoke test.

Phase 6 demoted brute-force / sqlite-vec to *offline fallback only* (Qdrant is
canonical). These tests prove the fallbacks still work end-to-end when Qdrant is
not the selected backend, and stay environment-agnostic: the sqlite-vec path is
skipped (not failed) when the optional native extension is not installed."""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "gateway"))

from ai_common_repo.core import constants as C  # noqa: E402
from ai_common_repo.core.config import StorageConfig  # noqa: E402
from ai_common_repo.model_backends.hash_embedding import HashEmbeddingBackend  # noqa: E402
from ai_common_repo.search.vector import (  # noqa: E402
    BruteForceVectorIndex, SqliteVecVectorIndex, _sqlite_vec_available,
    build_vector_index,
)
from ai_common_repo.storage.database import Database  # noqa: E402
from ai_common_repo.storage.repositories import EmbeddingRepository  # noqa: E402


def _repo() -> EmbeddingRepository:
    return EmbeddingRepository(Database(os.path.join(tempfile.mkdtemp(), "m.db")))


def _index(backend: str, repo: EmbeddingRepository, dim: int = 64):
    return build_vector_index(StorageConfig(vector_backend=backend), repo, dim=dim)


# -- factory selection -------------------------------------------------------
def test_bruteforce_backend_selected():
    idx = _index(C.VECTOR_BACKEND_BRUTEFORCE, _repo())
    assert isinstance(idx, BruteForceVectorIndex)


def test_auto_backend_picks_available_fallback():
    """``auto`` => sqlite-vec when importable, else brute-force — never Qdrant."""
    idx = _index(C.VECTOR_BACKEND_AUTO, _repo())
    if _sqlite_vec_available():
        assert isinstance(idx, SqliteVecVectorIndex)
    else:
        assert isinstance(idx, BruteForceVectorIndex)


# -- brute-force end-to-end --------------------------------------------------
def test_bruteforce_index_query_ranks_by_cosine():
    idx = _index(C.VECTOR_BACKEND_BRUTEFORCE, _repo(), dim=128)
    emb = HashEmbeddingBackend(dim=128)
    for oid, text in [("m1", "redis cache layer"),
                      ("m2", "postgres primary store"),
                      ("m3", "redis caching strategy")]:
        idx.index(C.OBJECT_TYPE_MEMORY, oid, emb.embed([text])[0], model=emb.model)

    hits = idx.query(emb.embed(["redis cache"])[0], [C.OBJECT_TYPE_MEMORY], 3)
    ids = [oid for _, oid, _ in hits]
    assert ids[0] in {"m1", "m3"} and ids[-1] == "m2"


def test_bruteforce_reindex_replaces_and_delete_removes():
    idx = _index(C.VECTOR_BACKEND_BRUTEFORCE, _repo())
    emb = HashEmbeddingBackend(dim=64)
    idx.index(C.OBJECT_TYPE_MEMORY, "m1", emb.embed(["first"])[0], model=emb.model)
    idx.index(C.OBJECT_TYPE_MEMORY, "m1", emb.embed(["second"])[0], model=emb.model)
    hits = idx.query(emb.embed(["second"])[0], [C.OBJECT_TYPE_MEMORY], 5)
    assert [oid for _, oid, _ in hits] == ["m1"]  # in-place replace, not a dup

    idx.delete(C.OBJECT_TYPE_MEMORY, "m1")
    assert idx.query(emb.embed(["second"])[0], [C.OBJECT_TYPE_MEMORY], 5) == []


def test_bruteforce_object_type_filter():
    idx = _index(C.VECTOR_BACKEND_BRUTEFORCE, _repo())
    emb = HashEmbeddingBackend(dim=64)
    idx.index(C.OBJECT_TYPE_MEMORY, "m1", emb.embed(["alpha"])[0], model=emb.model)
    idx.index(C.OBJECT_TYPE_CHUNK, "c1", emb.embed(["alpha"])[0], model=emb.model)
    hits = idx.query(emb.embed(["alpha"])[0], [C.OBJECT_TYPE_MEMORY], 5)
    assert {ot for ot, _, _ in hits} == {C.OBJECT_TYPE_MEMORY}


# -- sqlite-vec end-to-end (skipped when the extension is absent) -------------
def test_sqlite_vec_index_query_when_available():
    if not _sqlite_vec_available():
        pytest.skip("sqlite-vec extension not installed (offline fallback path)")
    idx = _index(C.VECTOR_BACKEND_SQLITE_VEC, _repo(), dim=64)
    assert isinstance(idx, SqliteVecVectorIndex)
    emb = HashEmbeddingBackend(dim=64)
    for oid, text in [("m1", "redis cache layer"), ("m2", "postgres primary store")]:
        idx.index(C.OBJECT_TYPE_MEMORY, oid, emb.embed([text])[0], model=emb.model)
    hits = idx.query(emb.embed(["redis cache"])[0], [C.OBJECT_TYPE_MEMORY], 2)
    assert hits and hits[0][1] == "m1"


# allow direct execution like the other suites
if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print("OK", _name)
            except Exception as exc:  # noqa: BLE001
                print("FAIL", _name, exc)
