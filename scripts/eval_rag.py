#!/usr/bin/env python
"""Offline RAG retrieval evaluation harness (Phase 8).

Measures recall@k and MRR over a fixed query set against a fixed corpus, and
compares the search-quality stages on vs off:

* query expansion (identifier decomposition + dev synonyms), and
* reranking (deterministic feature scorer).

Everything is deterministic and offline: the hash-embedding fallback stands in
for a real embedding backend and the brute-force vector index needs no native
extension or server. The same numbers come out on every machine, so this doubles
as a regression test (see ``tests/test_rag_eval.py``).

Usage::

    python -m scripts.eval_rag
"""
from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "gateway"))

from ai_common_repo.core import constants as C  # noqa: E402
from ai_common_repo.core.config import load_config  # noqa: E402
from ai_common_repo.core.models import (  # noqa: E402
    DocumentIngestRequest, SearchRequest,
)
from ai_common_repo.search.expansion import QueryExpander  # noqa: E402
from ai_common_repo.search.rerank import FeatureReranker  # noqa: E402
from ai_common_repo.search.service import SearchService  # noqa: E402
from ai_common_repo.service import MemoryService  # noqa: E402

_PROJECT = "repo:eval"

# Corpus: (key, title, content). The key is how queries name their gold doc.
# The ``distract_*`` docs repeat a single query term so lexical fusion (bm25)
# over-ranks them above the true answer — reranking's full-query coverage is what
# pushes the real gold back to the top.
_CORPUS = [
    ("auth", "Auth", "The authentication flow validates the user session token "
                      "before serving any request."),
    ("migrate", "Migrations", "Database migrations run via make migrate. Always "
                              "back up first."),
    ("username", "Names", "The get user name helper returns the current display "
                          "name for a user."),
    ("k8s", "Deploy", "Kubernetes deployment manifests live under the cluster "
                      "directory of the repository."),
    ("config", "Loader", "The configuration loader merges yaml files and "
                         "environment overrides at startup."),
    ("noise", "Notes", "Assorted notes about coffee, weather, and lunch plans "
                       "unrelated to engineering."),
    ("distract_manifest", "ManifestList",
     "Manifests manifests manifests. A list of manifests and yet more manifests."),
    ("distract_token", "TokenList",
     "Token token session token. Session token token session token session."),
]

# Query set: (query text, gold key). Several use code-identifier / abbreviation
# phrasings that only match the corpus after expansion.
_QUERIES = [
    ("getUserName", "username"),
    ("db migration", "migrate"),
    ("config overrides", "config"),
    ("auth session token", "auth"),
    ("k8s manifests", "k8s"),
]


@dataclass
class Metrics:
    recall_at_k: float
    mrr: float
    k: int


def _offline_service() -> MemoryService:
    tmp = tempfile.mkdtemp()
    os.environ["AICR_STORAGE__DB_PATH"] = os.path.join(tmp, "memory.db")
    os.environ["AICR_MODELS__DEFAULT_BACKEND"] = "none"  # hash embedder
    cfg = load_config()
    cfg.storage.vector_backend = C.VECTOR_BACKEND_BRUTEFORCE  # offline, no server
    return MemoryService(cfg)


def _ingest_corpus(svc: MemoryService) -> dict[str, str]:
    """Ingest the fixed corpus; return gold key -> document_id."""
    gold: dict[str, str] = {}
    for key, title, content in _CORPUS:
        out = svc.ingest_document(DocumentIngestRequest(
            project_id=_PROJECT, title=title, source_type="text", content=content))
        gold[key] = out["document_id"]
    return gold


def _variant(svc: MemoryService, *, rerank: bool, expansion: bool) -> SearchService:
    return SearchService(
        svc.memories, svc.documents, svc.vector_index, svc.embedder,
        rrf_k=svc.cfg.embedding.rrf_k,
        include_documents=svc.cfg.memory.include_documents,
        reranker=FeatureReranker() if rerank else None,
        expander=QueryExpander() if expansion else None)


def _score(search_svc: SearchService, gold: dict[str, str], *, k: int,
           mode: str, default_user: str) -> Metrics:
    recall_sum = 0.0
    mrr_sum = 0.0
    for query, gold_key in _QUERIES:
        gold_id = gold[gold_key]
        hits = search_svc.search(SearchRequest(
            q=query, project_id=_PROJECT, search_mode=mode, limit=k), default_user)
        ranks = [i for i, h in enumerate(hits[:k])
                 if h.object_type == C.OBJECT_TYPE_CHUNK and h.document_id == gold_id]
        if ranks:
            recall_sum += 1.0
            mrr_sum += 1.0 / (ranks[0] + 1)
    n = len(_QUERIES)
    return Metrics(recall_at_k=recall_sum / n, mrr=mrr_sum / n, k=k)


def run_eval(k: int = 5) -> dict[str, Metrics]:
    """Build the offline corpus and score four stage combinations.

    Returns a dict keyed by ``"<expansion>+<rerank>"`` (e.g. ``"exp+rr"``)::

        base    expansion off, rerank off   (FTS recall baseline uses fts mode)
        exp     expansion on,  rerank off
        rr      expansion off, rerank on
        exp+rr  expansion on,  rerank on
    """
    svc = _offline_service()
    gold = _ingest_corpus(svc)
    user = svc.cfg.identity.default_user_id
    results: dict[str, Metrics] = {}
    # Expansion's effect is clearest on lexical retrieval, so measure it in FTS
    # mode; reranking only reorders fused candidates, so measure it in hybrid.
    results["base_fts"] = _score(_variant(svc, rerank=False, expansion=False),
                                 gold, k=k, mode=C.SEARCH_MODE_FTS, default_user=user)
    results["exp_fts"] = _score(_variant(svc, rerank=False, expansion=True),
                                gold, k=k, mode=C.SEARCH_MODE_FTS, default_user=user)
    results["base_hybrid"] = _score(_variant(svc, rerank=False, expansion=True),
                                    gold, k=k, mode=C.SEARCH_MODE_HYBRID, default_user=user)
    results["rr_hybrid"] = _score(_variant(svc, rerank=True, expansion=True),
                                  gold, k=k, mode=C.SEARCH_MODE_HYBRID, default_user=user)
    return results


def main() -> None:
    results = run_eval()
    print(f"RAG eval — {len(_QUERIES)} queries, {len(_CORPUS)} docs, k={5}\n")
    print(f"{'config':<14}{'recall@k':>10}{'mrr':>8}")
    for name, m in results.items():
        print(f"{name:<14}{m.recall_at_k:>10.3f}{m.mrr:>8.3f}")
    print("\nexpansion recall gain (fts): "
          f"{results['exp_fts'].recall_at_k - results['base_fts'].recall_at_k:+.3f}")
    print("rerank mrr gain (hybrid):   "
          f"{results['rr_hybrid'].mrr - results['base_hybrid'].mrr:+.3f}")


if __name__ == "__main__":
    main()
