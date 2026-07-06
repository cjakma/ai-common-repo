"""Connector toolkit: the `Connector` interface plus the safe HTTP fetcher all
connectors share.

A connector polls an external source on an interval (driven by the Phase 1
scheduler) and returns *normalized documents* plus a next cursor. The gateway
turns those documents into the same `documents`/`document_chunks` records the
RAG ingest path produces, so chunking/embedding/search are reused unchanged.

Security posture (see PLAN §1):
- Outbound HTTP is locked to an allow-list of hosts + an https-only scheme + a
  timeout. User/config input never becomes an arbitrary fetch URL.
- Auth tokens are read from the environment at sync time and only ever placed in
  an Authorization header — never persisted, logged, or returned in a document.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx


@dataclass
class ConnectorDoc:
    """A source-agnostic document a connector emits. ``source_uri`` is the stable
    external identity used to detect changed/unchanged documents across polls."""
    source_type: str
    source_uri: str
    title: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SyncResult:
    documents: list[ConnectorDoc]
    next_cursor: str | None


class HttpFetcher(Protocol):
    """Narrow seam so tests can inject canned responses without real network."""

    def get_json(self, url: str, *, headers: dict[str, str] | None = None,
                 params: dict[str, Any] | None = None) -> Any: ...

    def post_json(self, url: str, *, headers: dict[str, str] | None = None,
                  json: dict[str, Any] | None = None) -> Any: ...


class AllowlistHttpFetcher:
    """The only HttpFetcher used in production. Enforces https + host allow-list
    + timeout on every request, so an SSRF via a mistyped config host fails fast."""

    def __init__(self, allowed_hosts: list[str], timeout_s: float = 20.0):
        self._allowed = {h.lower() for h in allowed_hosts if h}
        self._timeout_s = timeout_s

    def get_json(self, url: str, *, headers: dict[str, str] | None = None,
                 params: dict[str, Any] | None = None) -> Any:
        self._check(url)
        with httpx.Client(timeout=self._timeout_s) as client:
            resp = client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            return resp.json()

    def post_json(self, url: str, *, headers: dict[str, str] | None = None,
                  json: dict[str, Any] | None = None) -> Any:
        self._check(url)
        with httpx.Client(timeout=self._timeout_s) as client:
            resp = client.post(url, headers=headers, json=json)
            resp.raise_for_status()
            return resp.json()

    def _check(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https":
            raise ValueError(f"refusing non-https connector URL: {parsed.scheme!r}")
        host = (parsed.hostname or "").lower()
        if host not in self._allowed:
            raise ValueError(f"connector host {host!r} not in allow-list")


class Connector(ABC):
    """Pull-based source. ``sync`` takes the opaque cursor from the last run and
    returns new/changed documents plus the cursor to persist for the next run."""

    name: str

    @abstractmethod
    def sync(self, cursor: str | None) -> SyncResult:
        ...
