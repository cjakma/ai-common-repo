"""Polling connectors (Phase 4). Each connector turns an external source into
normalized documents that flow through the existing RAG ingest path.

``build_connectors`` is the factory the service uses: it instantiates only the
*enabled* connectors, each wired to an allow-listed HTTP fetcher derived from its
own config. Tokens are never resolved here — connectors read them from the
environment at sync time."""
from __future__ import annotations

from ..core import constants as C
from ..core.config import ConnectorsConfig
from .base import (
    AllowlistHttpFetcher, Connector, ConnectorDoc, HttpFetcher, SyncResult,
)
from .github import GithubConnector
from .notion import NotionConnector

__all__ = [
    "AllowlistHttpFetcher", "Connector", "ConnectorDoc", "HttpFetcher",
    "SyncResult", "GithubConnector", "NotionConnector", "build_connectors",
]


def build_connectors(cfg: ConnectorsConfig) -> dict[str, Connector]:
    out: dict[str, Connector] = {}
    if cfg.github.enabled:
        fetcher = AllowlistHttpFetcher(cfg.github.allowed_hosts, cfg.github.timeout_s)
        out[C.CONNECTOR_GITHUB] = GithubConnector(cfg.github, fetcher)
    if cfg.notion.enabled:
        fetcher = AllowlistHttpFetcher(cfg.notion.allowed_hosts, cfg.notion.timeout_s)
        out[C.CONNECTOR_NOTION] = NotionConnector(cfg.notion, fetcher)
    return out
