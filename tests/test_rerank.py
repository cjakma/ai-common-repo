"""Phase 8 — reranking. Deterministic feature scorer + the optional external
cross-encoder seam. All offline (no model)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "gateway"))

from ai_common_repo.core import constants as C  # noqa: E402
from ai_common_repo.core.config import RerankConfig  # noqa: E402
from ai_common_repo.core.models import SearchHit  # noqa: E402
from ai_common_repo.search.rerank import (  # noqa: E402
    CrossEncoderReranker, FeatureReranker, build_reranker,
)


def _hit(oid: str, content: str, score: float) -> SearchHit:
    return SearchHit(object_type=C.OBJECT_TYPE_CHUNK, id=oid, content=content,
                     score=score, document_id=oid)


def test_feature_reranker_promotes_higher_coverage():
    # Fused order puts a single-term repeater first; the true full-query match is
    # second. The feature reranker (full-query coverage) flips them.
    hits = [
        _hit("distract", "manifests manifests manifests manifests", 0.9),
        _hit("gold", "kubernetes deployment manifests cluster", 0.8),
    ]
    out = FeatureReranker().rerank("kubernetes manifests", hits)
    assert [h.id for h in out] == ["gold", "distract"]
    assert out[0].score >= out[1].score


def test_feature_reranker_exact_phrase_bonus():
    hits = [
        _hit("a", "the deploy step runs many other unrelated things here", 0.9),
        _hit("b", "run make deploy to ship", 0.8),
    ]
    out = FeatureReranker().rerank("make deploy", hits)
    assert out[0].id == "b"  # contains the exact phrase "make deploy"


def test_feature_reranker_empty_is_noop():
    assert FeatureReranker().rerank("anything", []) == []


def test_feature_reranker_pool_leaves_tail_in_place():
    hits = [_hit(f"h{i}", "filler text", 0.5) for i in range(5)]
    hits.append(_hit("tail", "deploy release pipeline", 0.1))
    out = FeatureReranker(pool=3).rerank("deploy release", hits)
    # The strong match sits beyond the pool, so it is NOT rescued into the top.
    assert out[-1].id == "tail"
    assert {h.id for h in out} == {h.id for h in hits}  # no drops


class _StubBackend:
    """Implements the RerankBackend protocol: scores by reversed input order."""

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        return [float(i) for i in range(len(documents))]


def test_cross_encoder_reranker_uses_backend_scores():
    hits = [_hit("a", "alpha", 0.9), _hit("b", "beta", 0.8), _hit("c", "c", 0.7)]
    out = CrossEncoderReranker(_StubBackend()).rerank("q", hits)
    # backend gives the last document the highest score -> it ranks first
    assert [h.id for h in out] == ["c", "b", "a"]


def test_build_reranker_selection():
    assert build_reranker(RerankConfig(enabled=False)) is None
    assert isinstance(build_reranker(RerankConfig()), FeatureReranker)
    # cross-encoder requested + compatible backend wired
    cfg = RerankConfig(use_cross_encoder=True)
    assert isinstance(build_reranker(cfg, _StubBackend()), CrossEncoderReranker)
    # cross-encoder requested but backend is incompatible -> feature fallback
    assert isinstance(build_reranker(cfg, object()), FeatureReranker)


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print("OK", _name)
