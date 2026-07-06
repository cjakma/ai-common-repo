"""Search service. Fuses full-text (FTS5) and vector retrieval over two corpora
— memories and document chunks — behind a single ``search`` call.

* ``fts``    — lexical only (Phase 1 behaviour, now also covering doc chunks).
* ``vector`` — embedding similarity (offline hash fallback when no backend).
* ``hybrid`` — Reciprocal Rank Fusion (RRF) of the FTS and vector rankings,
  which is robust to the two scorers living on different scales.

Project/scope filtering matches the memory rules: a project query also sees
cross-project ``user``/``global`` memories, but only its own project's docs."""
from __future__ import annotations

from ..core import constants as C
from ..core.models import Memory, SearchHit, SearchRequest
from ..model_backends.base import EmbeddingBackend
from ..storage.repositories import DocumentRepository, MemoryRepository
from .vector import VectorIndex

# Over-fetch before scope filtering so post-filter results still fill the limit.
_CANDIDATE_FACTOR = 5
_MIN_CANDIDATES = 50


class SearchService:
    def __init__(self, memories: MemoryRepository, documents: DocumentRepository,
                 vector_index: VectorIndex, embedder: EmbeddingBackend, *,
                 rrf_k: int = 60, include_documents: bool = True):
        self._memories = memories
        self._documents = documents
        self._index = vector_index
        self._embedder = embedder
        self._rrf_k = rrf_k
        self._include_documents = include_documents

    def search(self, req: SearchRequest, default_user: str) -> list[SearchHit]:
        user_id = req.user_id or default_user
        include_docs = (self._include_documents if req.include_documents is None
                        else req.include_documents)
        if req.tags:
            include_docs = False  # chunks carry no tags — memory-only addressing
        if req.search_mode == C.SEARCH_MODE_VECTOR:
            hits = self._vector(req, user_id, include_docs)
        elif req.search_mode == C.SEARCH_MODE_HYBRID:
            hits = self._hybrid(req, user_id, include_docs)
        else:
            hits = self._fts(req, user_id, include_docs)
        if req.scope:
            hits = [h for h in hits
                    if h.object_type != C.OBJECT_TYPE_MEMORY or h.scope in req.scope]
        return hits[:req.limit]

    # -- fts -----------------------------------------------------------------
    def _fts(self, req: SearchRequest, user_id: str,
             include_docs: bool) -> list[SearchHit]:
        hits = [_mem_hit(m, s) for m, s in
                self._memories.search_fts(req.q, user_id, req.project_id, req.limit,
                                          tags=req.tags)]
        if include_docs:
            hits += [_chunk_hit(c, s) for c, s in
                     self._documents.search_chunks_fts(
                         req.q, user_id, req.project_id, req.limit)]
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits

    # -- vector --------------------------------------------------------------
    def _vector(self, req: SearchRequest, user_id: str,
                include_docs: bool) -> list[SearchHit]:
        qvec = self._embedder.embed([req.q])[0]
        object_types = [C.OBJECT_TYPE_MEMORY]
        if include_docs:
            object_types.append(C.OBJECT_TYPE_CHUNK)
        cand = max(req.limit * _CANDIDATE_FACTOR, _MIN_CANDIDATES)
        raw = self._index.query(qvec, object_types, cand)

        mem_ids = [oid for ot, oid, _ in raw if ot == C.OBJECT_TYPE_MEMORY]
        chunk_ids = [oid for ot, oid, _ in raw if ot == C.OBJECT_TYPE_CHUNK]
        mems = self._memories.by_ids(mem_ids) if mem_ids else {}
        chunks = self._documents.chunks_by_ids(chunk_ids) if chunk_ids else {}

        hits: list[SearchHit] = []
        for otype, oid, score in raw:
            if otype == C.OBJECT_TYPE_MEMORY:
                mem = mems.get(oid)
                if (mem and _memory_visible(mem, user_id, req.project_id)
                        and _tags_match(mem, req.tags)):
                    hits.append(_mem_hit(mem, score))
            else:
                ch = chunks.get(oid)
                if ch and _chunk_visible(ch, user_id, req.project_id):
                    hits.append(_chunk_hit(ch, score))
        return hits

    # -- hybrid (RRF) --------------------------------------------------------
    def _hybrid(self, req: SearchRequest, user_id: str,
                include_docs: bool) -> list[SearchHit]:
        rankings = [self._fts(req, user_id, include_docs),
                    self._vector(req, user_id, include_docs)]
        fused: dict[tuple[str, str], SearchHit] = {}
        scores: dict[tuple[str, str], float] = {}
        for ranking in rankings:
            for rank, hit in enumerate(ranking):
                key = (hit.object_type, hit.id)
                scores[key] = scores.get(key, 0.0) + 1.0 / (self._rrf_k + rank + 1)
                fused.setdefault(key, hit)
        out = []
        for key, hit in fused.items():
            hit.score = round(scores[key], 6)
            out.append(hit)
        out.sort(key=lambda h: h.score, reverse=True)
        return out


# -- visibility + mapping helpers -------------------------------------------
def _tags_match(mem: Memory, tags: list[str] | None) -> bool:
    return not tags or set(tags).issubset(mem.tags)


def _memory_visible(mem: Memory, user_id: str, project_id: str | None) -> bool:
    if mem.user_id != user_id or mem.status != C.MEMORY_STATUS_ACTIVE:
        return False
    if project_id is None:
        return True
    return mem.project_id == project_id or mem.scope in ("user", "global")


def _chunk_visible(chunk: dict, user_id: str, project_id: str | None) -> bool:
    if chunk["user_id"] != user_id:
        return False
    return project_id is None or chunk["project_id"] == project_id


def _mem_hit(mem: Memory, score: float) -> SearchHit:
    return SearchHit(
        object_type=C.OBJECT_TYPE_MEMORY, id=mem.id, content=mem.content,
        score=round(score, 4), type=mem.type, scope=mem.scope, status=mem.status,
        source_session_id=mem.source_session_id)


def _chunk_hit(chunk: dict, score: float) -> SearchHit:
    return SearchHit(
        object_type=C.OBJECT_TYPE_CHUNK, id=chunk["id"], content=chunk["content"],
        score=round(score, 4), document_id=chunk["document_id"],
        document_title=chunk["title"], chunk_index=chunk["chunk_index"])
