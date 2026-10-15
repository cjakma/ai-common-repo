"""Vector index abstraction + interchangeable implementations.

``VectorIndex`` is the interface the search service depends on. Implementations:

* ``QdrantVectorIndex`` — the **canonical** backend (operator directive). Stores
  embeddings in a Qdrant collection (Cosine distance, dimension parameterised by
  the embedding backend). Production points at a Qdrant server; tests/offline use
  ``QdrantClient(location=":memory:")`` so the same code path runs without a
  server or network.
* ``BruteForceVectorIndex`` — pure-Python cosine over vectors loaded from the
  :class:`EmbeddingRepository`. Offline fallback only; no native dependency.
* ``SqliteVecVectorIndex`` — pushes the cosine scan into SQL via the optional
  ``sqlite-vec`` extension (when installed). Offline fallback only.

``build_vector_index`` selects one from ``storage.vector_backend`` config. All
SQL lives in the storage layer (EmbeddingRepository); these classes only own the
similarity *selection* policy. Qdrant payloads carry ``object_type``/``object_id``
so the search service can hydrate + apply scope/project visibility against the DB
(the authoritative source), with ``object_type`` pushed into the Qdrant filter.
"""
from __future__ import annotations

import math
import uuid
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from ..core import constants as C
from ..storage.repositories import EmbeddingRepository

if TYPE_CHECKING:
    from ..core.config import StorageConfig

# Stable namespace so a (object_type, object_id) pair always maps to the same
# Qdrant point id (Qdrant requires int/UUID ids; ours are opaque strings).
_POINT_NAMESPACE = uuid.UUID("b9c1e0e2-0d4a-4e1b-9b3a-7c2f5e6a8d10")


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

    def status(self) -> tuple[bool, dict]:
        """(ok, info) for /v1/health. SQLite-backed fallbacks share the DB, so
        they are healthy whenever the database is."""
        return True, {"backend": type(self).__name__}


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


class QdrantVectorIndex(VectorIndex):
    """Canonical backend. One collection, ``object_type``/``object_id`` in payload.

    The collection is created lazily on first write using the *actual* vector
    length, so the dimension always matches the live embedding backend (the
    ``dim`` hint is only used to create the collection up front for query-before-
    write). Distance is Cosine to match the L2-normalised embeddings.
    """

    def __init__(self, client, collection: str, *, dim: int):
        self._client = client
        self._collection = collection
        self._dim = dim
        self._ready = False

    def index(self, object_type: str, object_id: str, vector: list[float], *,
              model: str) -> None:
        from qdrant_client import models
        self._ensure_collection(len(vector))
        self._client.upsert(
            self._collection,
            points=[models.PointStruct(
                id=_point_id(object_type, object_id),
                vector=list(vector),
                payload={"object_type": object_type, "object_id": object_id,
                         "model": model})],
        )

    def query(self, vector: list[float], object_types: list[str],
              limit: int) -> list[tuple[str, str, float]]:
        from qdrant_client import models
        if not self._collection_exists():
            return []
        flt = models.Filter(must=[models.FieldCondition(
            key="object_type", match=models.MatchAny(any=list(object_types)))])
        res = self._client.query_points(
            self._collection, query=list(vector), limit=limit,
            query_filter=flt, with_payload=True)
        out: list[tuple[str, str, float]] = []
        for point in res.points:
            payload = point.payload or {}
            otype, oid = payload.get("object_type"), payload.get("object_id")
            if otype and oid:
                out.append((otype, oid, float(point.score)))
        return out

    def delete(self, object_type: str, object_id: str) -> None:
        if not self._collection_exists():
            return
        self._client.delete(
            self._collection,
            points_selector=[_point_id(object_type, object_id)])

    def status(self) -> tuple[bool, dict]:
        info: dict = {"backend": "qdrant", "collection": self._collection}
        try:
            if not self._client.collection_exists(self._collection):
                return True, {**info, "points": 0, "detail": "collection not created yet"}
            col = self._client.get_collection(self._collection)
        except Exception as exc:  # noqa: BLE001 — report, never raise from health
            return False, {**info, "detail": f"unreachable: {type(exc).__name__}"}
        vectors = col.config.params.vectors
        return str(col.status).endswith("green"), {
            **info, "points": col.points_count, "status": str(col.status).split(".")[-1].lower(),
            "vector_size": getattr(vectors, "size", None)}

    # -- collection lifecycle ------------------------------------------------
    def _ensure_collection(self, size: int) -> None:
        if self._ready:
            return
        from qdrant_client import models
        if not self._client.collection_exists(self._collection):
            self._client.create_collection(
                self._collection,
                vectors_config=models.VectorParams(
                    size=size, distance=models.Distance.COSINE))
        self._ready = True

    def _collection_exists(self) -> bool:
        if self._ready:
            return True
        if self._client.collection_exists(self._collection):
            self._ready = True
        return self._ready


def _point_id(object_type: str, object_id: str) -> str:
    return str(uuid.uuid5(_POINT_NAMESPACE, f"{object_type}:{object_id}"))


def build_vector_index(storage: "StorageConfig",
                       embeddings: EmbeddingRepository, *,
                       dim: int) -> VectorIndex:
    """Factory. ``qdrant`` (default) is canonical; ``auto`` prefers sqlite-vec
    when importable, else brute-force (both offline fallbacks only)."""
    backend = storage.vector_backend
    if backend == C.VECTOR_BACKEND_QDRANT:
        return QdrantVectorIndex(
            _build_qdrant_client(storage), storage.qdrant_collection, dim=dim)
    if backend == C.VECTOR_BACKEND_SQLITE_VEC:
        return SqliteVecVectorIndex(embeddings)
    if backend == C.VECTOR_BACKEND_AUTO and _sqlite_vec_available():
        return SqliteVecVectorIndex(embeddings)
    return BruteForceVectorIndex(embeddings)


def _build_qdrant_client(storage: "StorageConfig"):
    """Construct a QdrantClient from config. No url/location => in-memory
    (offline tests run the same code path as a real server)."""
    from qdrant_client import QdrantClient
    if storage.qdrant_url:
        return QdrantClient(
            url=storage.qdrant_url, prefer_grpc=storage.qdrant_prefer_grpc,
            timeout=storage.qdrant_timeout_s)
    return QdrantClient(location=storage.qdrant_location or ":memory:")


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
