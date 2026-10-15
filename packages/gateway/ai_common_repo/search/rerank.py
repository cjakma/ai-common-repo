"""Reranking — a second-pass scorer over fused retrieval candidates.

Hybrid fusion (RRF) orders results from two scorers that never *read* the query
against a candidate's full text. Reranking adds that read:

* ``FeatureReranker`` — deterministic, dependency-free. Scores each candidate on
  query-term coverage, exact-phrase presence and a length normalization, blended
  with a small prior on the fused rank so it refines rather than discards the
  upstream order. Needs no model and runs offline.
* ``CrossEncoderReranker`` — optional. Delegates to an external backend that
  implements the narrow :class:`RerankBackend` protocol. Disabled unless such a
  backend is wired (config ``search.rerank.use_cross_encoder``); the gateway
  falls back to the feature scorer, so tests/offline never need a model.

``build_reranker`` selects one (or ``None`` when reranking is off) from config —
the search service depends only on the :class:`Reranker` interface.
"""
from __future__ import annotations

import math
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ..core.models import SearchHit

if TYPE_CHECKING:
    from ..core.config import RerankConfig

# Feature weights (sum to 1 so a feature score lands in 0..1).
_W_COVERAGE = 0.6
_W_PHRASE = 0.3
_W_LENGTH = 0.1


class Reranker(ABC):
    """Reorders retrieval hits for a query. Implementations may mutate ``score``."""

    @abstractmethod
    def rerank(self, query: str, hits: list[SearchHit]) -> list[SearchHit]: ...


@runtime_checkable
class RerankBackend(Protocol):
    """Optional external cross-encoder. ``rerank`` returns one relevance score per
    document, aligned with the input order."""

    def rerank(self, query: str, documents: list[str]) -> list[float]: ...


class FeatureReranker(Reranker):
    """Deterministic feature-based scorer. Only the top ``pool`` candidates are
    rescored (the tail keeps its fused order), so cost stays bounded."""

    def __init__(self, *, pool: int = 50, prior_weight: float = 0.15):
        self._pool = max(1, pool)
        self._prior_weight = min(max(prior_weight, 0.0), 1.0)

    def rerank(self, query: str, hits: list[SearchHit]) -> list[SearchHit]:
        if not hits:
            return hits
        pool, tail = hits[:self._pool], hits[self._pool:]
        q_lower = query.lower()
        q_tokens = set(_tokens(query))
        scored: list[tuple[float, int, SearchHit]] = []
        for rank, hit in enumerate(pool):
            feat = _feature_score(q_tokens, q_lower, hit.content)
            prior = 1.0 / (rank + 1)
            blended = (1.0 - self._prior_weight) * feat + self._prior_weight * prior
            scored.append((blended, rank, hit))
        # Sort by score desc, breaking ties on the original fused rank (stable).
        scored.sort(key=lambda t: (-t[0], t[1]))
        out: list[SearchHit] = []
        for blended, _, hit in scored:
            hit.score = round(blended, 6)
            out.append(hit)
        return out + tail


class CrossEncoderReranker(Reranker):
    """Delegates scoring to an external :class:`RerankBackend` (e.g. a hosted
    cross-encoder). Used only when explicitly configured and wired."""

    def __init__(self, backend: RerankBackend, *, pool: int = 50):
        self._backend = backend
        self._pool = max(1, pool)

    def rerank(self, query: str, hits: list[SearchHit]) -> list[SearchHit]:
        if not hits:
            return hits
        pool, tail = hits[:self._pool], hits[self._pool:]
        scores = self._backend.rerank(query, [h.content for h in pool])
        ranked = sorted(zip(scores, range(len(pool)), pool),
                        key=lambda t: (-t[0], t[1]))
        out: list[SearchHit] = []
        for score, _, hit in ranked:
            hit.score = round(float(score), 6)
            out.append(hit)
        return out + tail


def build_reranker(cfg: "RerankConfig",
                   backend: object | None = None) -> Reranker | None:
    """Factory. Returns ``None`` when reranking is disabled. Prefers the external
    cross-encoder only when enabled *and* a compatible backend is wired."""
    if not cfg.enabled:
        return None
    if cfg.use_cross_encoder and isinstance(backend, RerankBackend):
        return CrossEncoderReranker(backend, pool=cfg.pool)
    return FeatureReranker(pool=cfg.pool)


# -- feature helpers ---------------------------------------------------------
def _feature_score(q_tokens: set[str], q_lower: str, content: str) -> float:
    if not q_tokens:
        return 0.0
    c_tokens = _tokens(content)
    coverage = len(q_tokens & set(c_tokens)) / len(q_tokens)
    phrase = 1.0 if q_lower and q_lower in content.lower() else 0.0
    # Mild penalty for verbose candidates so a tight match outranks a long one.
    excess = max(len(c_tokens) - len(q_tokens), 0)
    length_norm = 1.0 / (1.0 + math.log1p(excess / 10.0))
    return _W_COVERAGE * coverage + _W_PHRASE * phrase + _W_LENGTH * length_norm


def _tokens(text: str) -> list[str]:
    return ["".join(ch for ch in tok if ch.isalnum())
            for tok in text.lower().split() if any(ch.isalnum() for ch in tok)]
