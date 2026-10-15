"""Phase 8 — deterministic query expansion. Identifier decomposition + a small
conservative synonym map, and the recall lift it gives lexical search."""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "gateway"))

from ai_common_repo.core.config import QueryExpansionConfig, load_config  # noqa: E402
from ai_common_repo.core.models import (  # noqa: E402
    DocumentIngestRequest, SearchRequest,
)
from ai_common_repo.search.expansion import (  # noqa: E402
    QueryExpander, build_expander,
)
from ai_common_repo.search.service import SearchService  # noqa: E402
from ai_common_repo.service import MemoryService  # noqa: E402


def _terms(expanded: str) -> set[str]:
    return set(expanded.lower().split())


def test_decomposes_camel_and_snake_and_kebab():
    e = QueryExpander()
    assert {"get", "user", "name"} <= _terms(e.expand("getUserName"))
    assert {"user", "id"} <= _terms(e.expand("user_id"))
    assert {"max", "items"} <= _terms(e.expand("max-items"))


def test_conservative_synonyms_both_directions():
    e = QueryExpander()
    assert "kubernetes" in _terms(e.expand("k8s rollout"))
    assert "database" in _terms(e.expand("db migration"))
    assert "configuration" in _terms(e.expand("config loader"))


def test_plain_words_are_unchanged():
    e = QueryExpander()
    # No identifier boundary, no synonym => query returned verbatim.
    assert e.expand("the deployment pipeline") == "the deployment pipeline"


def test_original_query_is_preserved():
    e = QueryExpander()
    out = e.expand("getUserName")
    assert out.startswith("getUserName")  # never replaces, only appends


def test_build_expander_disabled_returns_none():
    assert build_expander(QueryExpansionConfig(enabled=False)) is None
    assert isinstance(build_expander(QueryExpansionConfig()), QueryExpander)


def _service() -> MemoryService:
    tmp = tempfile.mkdtemp()
    os.environ["AICR_STORAGE__DB_PATH"] = os.path.join(tmp, "memory.db")
    os.environ["AICR_MODELS__DEFAULT_BACKEND"] = "none"
    cfg = load_config()
    cfg.storage.vector_backend = "bruteforce"
    return MemoryService(cfg)


def test_expansion_recovers_identifier_query_in_fts():
    """An identifier query that FTS misses verbatim is recovered once expansion
    decomposes it into word parts."""
    svc = _service()
    svc.ingest_document(DocumentIngestRequest(
        project_id="repo:qe", title="Names", source_type="text",
        content="The get user name helper returns the current display name."))
    user = svc.cfg.identity.default_user_id

    no_exp = SearchService(svc.memories, svc.documents, svc.vector_index,
                           svc.embedder, expander=None)
    with_exp = SearchService(svc.memories, svc.documents, svc.vector_index,
                             svc.embedder, expander=QueryExpander())
    req = SearchRequest(q="getUserName", project_id="repo:qe", search_mode="fts")
    assert no_exp.search(req, user) == []           # whole-word FTS finds nothing
    assert with_exp.search(req, user)               # expansion -> get/user/name hit


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print("OK", _name)
