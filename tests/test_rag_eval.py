"""Phase 8 — RAG evaluation harness regression test. Deterministic/offline, so
the recall@k and MRR gains from expansion and reranking are reproducible."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "gateway"))

from scripts.eval_rag import run_eval  # noqa: E402


def test_expansion_lifts_recall_and_rerank_lifts_mrr():
    r = run_eval()
    # query expansion strictly improves lexical recall@k over the baseline
    assert r["exp_fts"].recall_at_k > r["base_fts"].recall_at_k
    # reranking strictly improves MRR over the (already-expanded) hybrid baseline
    assert r["rr_hybrid"].mrr > r["base_hybrid"].mrr
    # reranking never hurts recall (it only reorders within the candidate set)
    assert r["rr_hybrid"].recall_at_k >= r["base_hybrid"].recall_at_k


def test_eval_metrics_are_deterministic():
    assert run_eval()["exp_fts"].recall_at_k == run_eval()["exp_fts"].recall_at_k


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print("OK", _name)
