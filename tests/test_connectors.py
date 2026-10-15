"""Phase 4 — GitHub/Notion polling connectors. All offline: a fake HTTP fetcher
stands in for the real network, so polling -> normalize -> document -> hybrid
search is exercised end to end without tokens or sockets."""
from __future__ import annotations

import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "gateway"))

from ai_common_repo.connectors import (  # noqa: E402
    AllowlistHttpFetcher, ConnectorDoc, SyncResult,
)
from ai_common_repo.connectors.base import Connector  # noqa: E402
from ai_common_repo.connectors.github import GithubConnector  # noqa: E402
from ai_common_repo.connectors.notion import NotionConnector  # noqa: E402
from ai_common_repo.core import constants as C  # noqa: E402
from ai_common_repo.core.config import load_config  # noqa: E402
from ai_common_repo.core.models import SearchRequest  # noqa: E402
from ai_common_repo.service import MemoryService  # noqa: E402
from ai_common_repo.workers import Worker  # noqa: E402


# -- fakes -------------------------------------------------------------------
class FakeFetcher:
    """Routes URLs to canned JSON and records what was sent (headers/params)."""

    def __init__(self, routes: dict):
        self._routes = routes
        self.calls: list[tuple[str, dict | None, dict | None]] = []
        self.headers_seen: list[dict] = []

    def get_json(self, url, *, headers=None, params=None):
        self.calls.append((url, params, None))
        self.headers_seen.append(headers or {})
        return self._match(url)

    def post_json(self, url, *, headers=None, json=None):
        self.calls.append((url, None, json))
        self.headers_seen.append(headers or {})
        return self._match(url)

    def _match(self, url):
        for suffix, payload in self._routes.items():
            if url.endswith(suffix):
                return payload
        return None


class StubConnector(Connector):
    """Returns pre-baked SyncResults so the service-side poll path can be tested
    without any HTTP at all."""

    def __init__(self, name: str, results: list[SyncResult]):
        self.name = name
        self._results = list(results)
        self.cursors_seen: list[str | None] = []

    def sync(self, cursor):
        self.cursors_seen.append(cursor)
        if self._results:
            return self._results.pop(0)
        return SyncResult([], cursor)


def _service(tmp: str, **github):
    os.environ["AICR_STORAGE__DB_PATH"] = os.path.join(tmp, "memory.db")
    os.environ["AICR_MODELS__DEFAULT_BACKEND"] = "none"  # offline hash embedder
    cfg = load_config()
    cfg.connectors.github.enabled = True
    for k, v in github.items():
        setattr(cfg.connectors.github, k, v)
    return MemoryService(cfg)


def _drain(service):
    Worker(service.jobs, service.handlers).drain()


# -- SSRF / transport guard --------------------------------------------------
def test_fetcher_rejects_non_https_and_unlisted_host():
    f = AllowlistHttpFetcher(["api.github.com"], 5.0)
    with pytest.raises(ValueError):
        f.get_json("http://api.github.com/repos")   # non-https
    with pytest.raises(ValueError):
        f.get_json("https://evil.example/repos")     # host not allow-listed
    with pytest.raises(ValueError):
        f.post_json("https://evil.example/x", json={})


# -- GitHub connector --------------------------------------------------------
def _github_cfg(**over):
    cfg = load_config().connectors.github
    cfg.repos = ["octo/repo"]
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def test_github_normalizes_issue_commit_readme_and_skips_prs():
    import base64
    routes = {
        "/repos/octo/repo/issues": [
            {"number": 7, "title": "Adopt ruff", "body": "We standardize on ruff.",
             "updated_at": "2026-06-20T10:00:00Z", "state": "open",
             "user": {"login": "alice"},
             "html_url": "https://github.com/octo/repo/issues/7"},
            {"number": 8, "title": "a pull request", "pull_request": {},
             "updated_at": "2026-06-20T12:00:00Z",
             "html_url": "https://github.com/octo/repo/pull/8"},
        ],
        "/repos/octo/repo/commits": [
            {"sha": "abcdef1234567", "html_url": "https://github.com/octo/repo/commit/a",
             "commit": {"message": "Fix flaky test\n\ndetails",
                        "author": {"date": "2026-06-21T11:00:00Z"}}},
        ],
        "/repos/octo/repo/readme": {
            "encoding": "base64", "path": "README.md",
            "content": base64.b64encode(b"# Project\nUsage docs here.").decode(),
            "html_url": "https://github.com/octo/repo/blob/main/README.md"},
    }
    conn = GithubConnector(_github_cfg(), FakeFetcher(routes))
    result = conn.sync(None)

    types = sorted(d.source_type for d in result.documents)
    assert types == ["github_commit", "github_issue", "github_readme"]
    assert all("pull/8" not in d.source_uri for d in result.documents)  # PR skipped
    readme = next(d for d in result.documents if d.source_type == "github_readme")
    assert "Usage docs here." in readme.content
    # cursor advances to the latest timestamp seen for the repo.
    assert json.loads(result.next_cursor)["octo/repo"] == "2026-06-21T11:00:00Z"


def test_github_incremental_passes_since_and_skips_readme():
    fetcher = FakeFetcher({"/repos/octo/repo/issues": [],
                           "/repos/octo/repo/commits": []})
    conn = GithubConnector(_github_cfg(), fetcher)
    cursor = json.dumps({"octo/repo": "2026-06-21T11:00:00Z"})
    conn.sync(cursor)

    called = [url for url, _, _ in fetcher.calls]
    assert any(u.endswith("/issues") for u in called)
    assert not any(u.endswith("/readme") for u in called)  # only on first sync
    issue_params = next(p for u, p, _ in fetcher.calls if u.endswith("/issues"))
    assert issue_params["since"] == "2026-06-21T11:00:00Z"


# -- Notion connector --------------------------------------------------------
def test_notion_builds_page_document_with_title_and_blocks():
    routes = {
        "/v1/databases/db1/query": {"results": [
            {"id": "page1", "url": "https://notion.so/page1",
             "last_edited_time": "2026-06-22T09:00:00Z",
             "properties": {"Name": {"type": "title",
                                     "title": [{"plain_text": "Deploy runbook"}]}}},
        ]},
        "/v1/blocks/page1/children": {"results": [
            {"type": "paragraph",
             "paragraph": {"rich_text": [{"plain_text": "Run terraform apply first."}]}},
        ]},
    }
    cfg = load_config().connectors.notion
    cfg.databases = ["db1"]
    conn = NotionConnector(cfg, FakeFetcher(routes))
    result = conn.sync(None)

    assert len(result.documents) == 1
    doc = result.documents[0]
    assert doc.title == "Deploy runbook"
    assert "Run terraform apply first." in doc.content
    assert json.loads(result.next_cursor)["db1"] == "2026-06-22T09:00:00Z"


# -- service poll -> document -> hybrid search --------------------------------
def test_poll_ingests_documents_retrievable_by_hybrid_search():
    """Full chain over mock HTTP: poll -> normalize -> document -> hybrid search."""
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp, repos=["octo/repo"], project_id="proj-a")
        fetcher = FakeFetcher({
            "/repos/octo/repo/issues": [
                {"number": 7, "title": "Adopt ruff",
                 "body": "We standardize on ruff for linting across repositories.",
                 "updated_at": "2026-06-20T10:00:00Z", "state": "open",
                 "user": {"login": "alice"},
                 "html_url": "https://github.com/octo/repo/issues/7"}],
            "/repos/octo/repo/commits": [],
            "/repos/octo/repo/readme": None,
        })
        svc.connectors[C.CONNECTOR_GITHUB] = GithubConnector(_github_cfg(), fetcher)

        svc.jobs.enqueue(C.JOB_CONNECTOR_POLL, {"connector": "github"})
        _drain(svc)

        hits = svc.search(SearchRequest(q="ruff linting", project_id="proj-a",
                                        search_mode=C.SEARCH_MODE_HYBRID))
        assert any(h["object_type"] == C.OBJECT_TYPE_CHUNK
                   and "ruff" in h["content"] for h in hits)
        # cursor persisted for the next poll.
        assert json.loads(svc.connector_state.get("github"))["octo/repo"] \
            == "2026-06-20T10:00:00Z"


def test_repolling_is_idempotent_and_replaces_changed_docs():
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp, project_id="proj-a")
        uri = "https://github.com/octo/repo/issues/7"

        def poll(content):
            doc = ConnectorDoc(source_type="github_issue", source_uri=uri,
                               title="issue 7", content=content, metadata={})
            svc.connectors[C.CONNECTOR_GITHUB] = StubConnector(
                "github", [SyncResult([doc], "{}")])
            svc.jobs.enqueue(C.JOB_CONNECTOR_POLL, {"connector": "github"})
            _drain(svc)

        def doc_count():
            return svc._db.conn.execute(
                "SELECT COUNT(*) AS n FROM documents").fetchone()["n"]

        poll("Original body about kafka pipelines.")
        assert doc_count() == 1
        poll("Original body about kafka pipelines.")  # unchanged -> no new doc
        assert doc_count() == 1

        poll("Rewritten body about airflow scheduling instead.")  # changed -> replace
        assert doc_count() == 1
        hits = svc.search(SearchRequest(q="airflow scheduling", project_id="proj-a",
                                        search_mode=C.SEARCH_MODE_HYBRID))
        assert any("airflow" in h["content"] for h in hits)
        # the superseded content's chunks are gone (purged with their vectors).
        stale = svc._db.conn.execute(
            "SELECT COUNT(*) AS n FROM document_chunks WHERE content LIKE '%kafka%'"
        ).fetchone()["n"]
        assert stale == 0


# -- SSRF / transport guard: additional edge cases ---------------------------
def test_fetcher_rejects_more_ssrf_shapes():
    f = AllowlistHttpFetcher(["api.github.com"], 5.0)
    for bad in ["ftp://api.github.com/x",                 # non-https scheme
                "file:///etc/passwd",                     # local file scheme
                "https://evil.api.github.com/x",          # unlisted subdomain
                "https://api.github.com.evil.com/x",      # suffix-spoof host
                "https:///just/a/path"]:                  # missing host
        with pytest.raises(ValueError):
            f.get_json(bad)
        with pytest.raises(ValueError):
            f.post_json(bad, json={})


def test_fetcher_accepts_case_insensitive_host_and_applies_timeout(monkeypatch):
    """A mixed-case host still matches the allow-list, and the configured timeout
    reaches the httpx client — verified without touching the network."""
    import ai_common_repo.connectors.base as base

    seen = {}

    class _Resp:
        def raise_for_status(self): ...
        def json(self): return {"ok": True}

    class _Client:
        def __init__(self, *, timeout): seen["timeout"] = timeout
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, headers=None, params=None):
            seen["url"] = url
            return _Resp()
        def post(self, url, headers=None, json=None): return _Resp()

    monkeypatch.setattr(base.httpx, "Client", _Client)
    f = base.AllowlistHttpFetcher(["api.github.com"], 7.5)
    assert f.get_json("https://API.GitHub.COM/repos") == {"ok": True}
    assert seen["timeout"] == 7.5 and seen["url"] == "https://API.GitHub.COM/repos"


# -- GitHub cursor boundaries ------------------------------------------------
def _github_cfg_repos(repos, **over):
    cfg = load_config().connectors.github
    cfg.repos = repos
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def test_github_multi_repo_cursors_advance_independently():
    routes = {
        "/repos/octo/a/issues": [
            {"number": 1, "title": "A issue", "body": "x",
             "updated_at": "2026-06-25T10:00:00Z", "state": "open",
             "user": {"login": "a"},
             "html_url": "https://github.com/octo/a/issues/1"}],
        "/repos/octo/a/commits": [],
        "/repos/octo/b/issues": [],   # b has nothing new this poll
        "/repos/octo/b/commits": [],
    }
    conn = GithubConnector(_github_cfg_repos(["octo/a", "octo/b"]), FakeFetcher(routes))
    nxt = json.loads(conn.sync(json.dumps({"octo/b": "2026-06-01T00:00:00Z"})).next_cursor)
    assert nxt["octo/a"] == "2026-06-25T10:00:00Z"   # advanced from its item
    assert nxt["octo/b"] == "2026-06-01T00:00:00Z"   # preserved untouched


def test_github_empty_first_sync_adds_no_watermark():
    routes = {"/repos/octo/repo/issues": [], "/repos/octo/repo/commits": [],
              "/repos/octo/repo/readme": None}
    result = GithubConnector(_github_cfg(), FakeFetcher(routes)).sync(None)
    assert result.documents == []
    assert json.loads(result.next_cursor) == {}   # nothing seen => no cursor key


def test_github_malformed_cursor_is_treated_as_first_sync():
    import base64
    routes = {"/repos/octo/repo/issues": [], "/repos/octo/repo/commits": [],
              "/repos/octo/repo/readme": {
                  "encoding": "base64", "path": "README.md",
                  "content": base64.b64encode(b"# hi").decode(),
                  "html_url": "https://github.com/octo/repo/blob/main/README.md"}}
    fetcher = FakeFetcher(routes)
    result = GithubConnector(_github_cfg(), fetcher).sync("not-json{{")
    # garbage cursor => since None => README fetched (first-sync behavior)
    assert any(d.source_type == "github_readme" for d in result.documents)
    assert any(u.endswith("/readme") for u, _, _ in fetcher.calls)


def test_github_cursor_takes_max_across_resources():
    routes = {
        "/repos/octo/repo/issues": [
            {"number": 1, "title": "old", "body": "b",
             "updated_at": "2026-06-10T00:00:00Z", "state": "open",
             "user": {"login": "a"},
             "html_url": "https://github.com/octo/repo/issues/1"}],
        "/repos/octo/repo/commits": [
            {"sha": "deadbeef0", "html_url": "https://github.com/octo/repo/commit/d",
             "commit": {"message": "newer", "author": {"date": "2026-06-15T00:00:00Z"}}}],
    }
    cursor = json.dumps({"octo/repo": "2026-06-01T00:00:00Z"})  # set => README skipped
    result = GithubConnector(_github_cfg(), FakeFetcher(routes)).sync(cursor)
    assert json.loads(result.next_cursor)["octo/repo"] == "2026-06-15T00:00:00Z"


# -- Notion cursor boundaries ------------------------------------------------
def test_notion_incremental_filters_after_and_advances_cursor():
    routes = {
        "/v1/databases/db1/query": {"results": [
            {"id": "page1", "url": "https://notion.so/page1",
             "last_edited_time": "2026-06-26T09:00:00Z",
             "properties": {"Name": {"type": "title",
                                     "title": [{"plain_text": "Runbook"}]}}}]},
        "/v1/blocks/page1/children": {"results": [
            {"type": "paragraph",
             "paragraph": {"rich_text": [{"plain_text": "step one"}]}}]},
    }
    fetcher = FakeFetcher(routes)
    cfg = load_config().connectors.notion
    cfg.databases = ["db1"]
    result = NotionConnector(cfg, fetcher).sync(json.dumps({"db1": "2026-06-01T00:00:00Z"}))

    # incremental query carries the "after" filter derived from the prior cursor ...
    query_body = next(b for u, _, b in fetcher.calls if u.endswith("/query"))
    assert query_body["filter"]["last_edited_time"]["after"] == "2026-06-01T00:00:00Z"
    # ... and the watermark advances to the newest page seen.
    assert len(result.documents) == 1
    assert json.loads(result.next_cursor)["db1"] == "2026-06-26T09:00:00Z"


def test_notion_content_less_page_emits_no_doc_but_advances_cursor():
    """The ``if not content`` branch: a page with no title and no block text still
    advances the watermark so it is not re-fetched forever."""
    routes = {
        "/v1/databases/db1/query": {"results": [
            {"id": "", "url": "https://notion.so/blank",
             "last_edited_time": "2026-06-27T09:00:00Z",
             "properties": {}}]},      # empty id + no title => title ""
        "/v1/blocks//children": {"results": []},   # no block text
    }
    cfg = load_config().connectors.notion
    cfg.databases = ["db1"]
    result = NotionConnector(cfg, FakeFetcher(routes)).sync(None)
    assert result.documents == []
    assert json.loads(result.next_cursor)["db1"] == "2026-06-27T09:00:00Z"


def test_token_read_from_env_is_sent_but_never_persisted():
    secret = "tok_DO_NOT_PERSIST_3141592653"
    os.environ["GITHUB_TOKEN"] = secret
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "memory.db")
            svc = _service(tmp, repos=["octo/repo"], project_id="proj-a")
            fetcher = FakeFetcher({
                "/repos/octo/repo/issues": [
                    {"number": 1, "title": "Hi", "body": "body text",
                     "updated_at": "2026-06-20T10:00:00Z", "state": "open",
                     "user": {"login": "a"},
                     "html_url": "https://github.com/octo/repo/issues/1"}],
                "/repos/octo/repo/commits": [],
            })
            svc.connectors[C.CONNECTOR_GITHUB] = GithubConnector(
                _github_cfg(), fetcher)

            svc.jobs.enqueue(C.JOB_CONNECTOR_POLL, {"connector": "github"})
            _drain(svc)

            # the token was read from env and sent as a bearer header ...
            auth = [h.get("Authorization") for h in fetcher.headers_seen]
            assert f"Bearer {secret}" in auth
            # ... but it is nowhere in the persisted database.
            with open(db_path, "rb") as fh:
                blob = fh.read()
            assert secret.encode() not in blob
    finally:
        del os.environ["GITHUB_TOKEN"]
