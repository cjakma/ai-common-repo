"""Vector index abstraction + two interchangeable implementations.

``VectorIndex`` is the interface the search service depends on. Implementations:

* ``BruteForceVectorIndex`` — pure-Python cosine over vectors loaded from the
  :class:`EmbeddingRepository`. No native dependency; always available. Fine for
  the local single-user corpus sizes this gateway targets.
* ``SqliteVecVectorIndex`` — pushes the cosine scan into SQL via the optional
  ``sqlite-vec`` extension (when installed). Same persistence, faster query.

``build_vector_index`` selects one from ``storage.vector_backend`` config. All
SQL lives in the storage layer (EmbeddingRepository); these classes only own the
similarity *selection* policy.
"""
from __future__ import annotations

import math
from abc import ABC, abstractmethod

from ..core import constants as C
from ..storage.repositories import EmbeddingRepository


class VectorIndex(ABC):
    """Write + query surface for object embeddings."""

    @abstractmethod
    def index(self, object_type: str, object_id: str, vector: list[float], *,
              model: str) -> None: ...

    @abstractmethod
    def query(self, vector: list[float], object_types: list[str],
              limit: int) -> list[tuple[str, str, float]]:
        """Return up to ``limit`` (object_type, object_id, score) by descending
        cosine similarity (score in roughly 0..1)."""

    @abstractmethod
    def delete(self, object_type: str, object_id: str) -> None: ...


class BruteForceVectorIndex(VectorIndex):
    def __init__(self, embeddings: EmbeddingRepository):
        self._embeddings = embeddings

    def index(self, object_type: str, object_id: str, vector: list[float], *,
              model: str) -> None:
        self._embeddings.upsert(object_type, object_id, model, vector)

    def query(self, vector: list[float], object_types: list[str],
              limit: int) -> list[tuple[str, str, float]]:
        q_norm = math.sqrt(sum(v * v for v in vector))
        if q_norm == 0.0:
            return []
        scored: list[tuple[str, str, float]] = []
        for otype, oid, vec in self._embeddings.vectors_for(object_types):
            if len(vec) != len(vector):
                continue  # different embedder/dim — not comparable
            sim = _cosine(vector, vec, q_norm)
            scored.append((otype, oid, sim))
        scored.sort(key=lambda r: r[2], reverse=True)
        return scored[:limit]

    def delete(self, object_type: str, object_id: str) -> None:
        self._embeddings.delete(object_type, object_id)


class SqliteVecVectorIndex(VectorIndex):
    """Same store as brute-force; cosine pushed into SQL via sqlite-vec."""

    def __init__(self, embeddings: EmbeddingRepository):
        self._embeddings = embeddings

    def index(self, object_type: str, object_id: str, vector: list[float], *,
              model: str) -> None:
        self._embeddings.upsert(object_type, object_id, model, vector)

    def query(self, vector: list[float], object_types: list[str],
              limit: int) -> list[tuple[str, str, float]]:
        return self._embeddings.vec_search(vector, object_types, limit)

    def delete(self, object_type: str, object_id: str) -> None:
        self._embeddings.delete(object_type, object_id)


def build_vector_index(backend: str,
                       embeddings: EmbeddingRepository) -> VectorIndex:
    """Factory. ``auto`` prefers sqlite-vec when importable, else brute-force."""
    if backend == C.VECTOR_BACKEND_SQLITE_VEC:
        return SqliteVecVectorIndex(embeddings)
    if backend == C.VECTOR_BACKEND_AUTO and _sqlite_vec_available():
        return SqliteVecVectorIndex(embeddings)
    return BruteForceVectorIndex(embeddings)


def _sqlite_vec_available() -> bool:
    try:
        import sqlite_vec  # noqa: F401
        return True
    except ImportError:
        return False


def _cosine(a: list[float], b: list[float], a_norm: float) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    b_norm = math.sqrt(sum(y * y for y in b))
    if b_norm == 0.0:
        return 0.0
    return dot / (a_norm * b_norm)
