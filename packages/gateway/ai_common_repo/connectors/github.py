"""GitHub polling connector.

Polls each configured ``owner/name`` repo for issues and commits updated since the
last cursor, plus the README on the first sync. The cursor is an opaque JSON map
of ``repo -> last-seen ISO timestamp`` so each repo advances independently.

No realtime webhooks, no OAuth: the token is read from the environment at sync
time only and used solely as a bearer header. Outbound calls go through an
allow-listed HTTP fetcher (host + https + timeout)."""
from __future__ import annotations

import base64
import json
import os

from ..core.config import GithubConnectorConfig
from .base import Connector, ConnectorDoc, HttpFetcher, SyncResult

_ACCEPT = "application/vnd.github+json"
_API_VERSION = "2022-06-28"


class GithubConnector(Connector):
    name = "github"

    def __init__(self, cfg: GithubConnectorConfig, fetcher: HttpFetcher):
        self._cfg = cfg
        self._http = fetcher
        self._base = cfg.base_url.rstrip("/")

    # -- sync ----------------------------------------------------------------
    def sync(self, cursor: str | None) -> SyncResult:
        since_by_repo = _parse_cursor(cursor)
        docs: list[ConnectorDoc] = []
        next_cursor = dict(since_by_repo)
        for repo in self._cfg.repos:
            since = since_by_repo.get(repo)
            high = since
            for doc, ts in self._poll_repo(repo, since):
                docs.append(doc)
                if ts and (high is None or ts > high):
                    high = ts
            if high:
                next_cursor[repo] = high
        return SyncResult(documents=docs, next_cursor=json.dumps(next_cursor))

    def _poll_repo(self, repo: str, since: str | None):
        yield from self._issues(repo, since)
        yield from self._commits(repo, since)
        if since is None:  # README only on the first sync of a repo
            readme = self._readme(repo)
            if readme is not None:
                yield readme, None

    # -- resources -----------------------------------------------------------
    def _issues(self, repo: str, since: str | None):
        params = {"state": "all", "sort": "updated", "direction": "asc",
                  "per_page": self._cfg.max_items_per_sync}
        if since:
            params["since"] = since
        for item in self._get(f"/repos/{repo}/issues", params) or []:
            if "pull_request" in item:  # the issues endpoint also lists PRs; skip
                continue
            number, updated = item.get("number"), item.get("updated_at")
            body = item.get("body") or ""
            yield ConnectorDoc(
                source_type="github_issue",
                source_uri=item.get("html_url", f"{repo}#{number}"),
                title=f"{repo}#{number}: {item.get('title', '')}".strip(),
                content=f"{item.get('title', '')}\n\n{body}".strip(),
                metadata={"repo": repo, "number": number, "state": item.get("state"),
                          "author": (item.get("user") or {}).get("login"),
                          "updated_at": updated},
            ), updated

    def _commits(self, repo: str, since: str | None):
        params = {"per_page": self._cfg.max_items_per_sync}
        if since:
            params["since"] = since
        for item in self._get(f"/repos/{repo}/commits", params) or []:
            commit = item.get("commit") or {}
            message = commit.get("message") or ""
            date = (commit.get("author") or {}).get("date")
            sha = item.get("sha", "")
            yield ConnectorDoc(
                source_type="github_commit",
                source_uri=item.get("html_url", f"{repo}@{sha}"),
                title=f"{repo}@{sha[:7]}: {message.splitlines()[0] if message else ''}",
                content=message,
                metadata={"repo": repo, "sha": sha, "date": date},
            ), date

    def _readme(self, repo: str) -> ConnectorDoc | None:
        data = self._get(f"/repos/{repo}/readme", None, optional=True)
        if not data:
            return None
        content = data.get("content") or ""
        if data.get("encoding") == "base64":
            content = base64.b64decode(content).decode("utf-8", errors="replace")
        return ConnectorDoc(
            source_type="github_readme",
            source_uri=data.get("html_url", f"{repo}/README"),
            title=f"{repo} README",
            content=content,
            metadata={"repo": repo, "path": data.get("path")},
        )

    # -- http ----------------------------------------------------------------
    def _get(self, path: str, params: dict | None, *, optional: bool = False):
        try:
            return self._http.get_json(f"{self._base}{path}", headers=self._headers(),
                                       params=params)
        except Exception:  # noqa: BLE001 - missing README / 404 is non-fatal
            if optional:
                return None
            raise

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": _ACCEPT, "X-GitHub-Api-Version": _API_VERSION}
        token = os.environ.get(self._cfg.token_env)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers


def _parse_cursor(cursor: str | None) -> dict[str, str]:
    if not cursor:
        return {}
    try:
        data = json.loads(cursor)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}
