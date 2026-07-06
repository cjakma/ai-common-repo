"""Notion polling connector.

Polls each configured database for pages edited since the last cursor, pulls each
page's title + top-level block text, and normalizes them into documents. The
cursor is an opaque JSON map of ``database_id -> last-seen last_edited_time``.

Same security rules as the GitHub connector: token from the environment at sync
time only, allow-listed https fetcher, no realtime webhooks."""
from __future__ import annotations

import json
import os

from ..core.config import NotionConnectorConfig
from .base import Connector, ConnectorDoc, HttpFetcher, SyncResult

# Block types whose rich_text we fold into page content.
_TEXT_BLOCKS = (
    "paragraph", "heading_1", "heading_2", "heading_3",
    "bulleted_list_item", "numbered_list_item", "quote", "callout", "to_do",
)


class NotionConnector(Connector):
    name = "notion"

    def __init__(self, cfg: NotionConnectorConfig, fetcher: HttpFetcher):
        self._cfg = cfg
        self._http = fetcher
        self._base = cfg.base_url.rstrip("/")

    def sync(self, cursor: str | None) -> SyncResult:
        since_by_db = _parse_cursor(cursor)
        docs: list[ConnectorDoc] = []
        next_cursor = dict(since_by_db)
        for db_id in self._cfg.databases:
            high = since_by_db.get(db_id)
            for page in self._query_pages(db_id, since_by_db.get(db_id)):
                doc, edited = self._page_doc(db_id, page)
                if doc is not None:
                    docs.append(doc)
                if edited and (high is None or edited > high):
                    high = edited
            if high:
                next_cursor[db_id] = high
        return SyncResult(documents=docs, next_cursor=json.dumps(next_cursor))

    def _query_pages(self, db_id: str, since: str | None) -> list[dict]:
        body: dict = {"page_size": self._cfg.max_items_per_sync,
                      "sorts": [{"timestamp": "last_edited_time",
                                 "direction": "ascending"}]}
        if since:
            body["filter"] = {"timestamp": "last_edited_time",
                              "last_edited_time": {"after": since}}
        data = self._http.post_json(f"{self._base}/v1/databases/{db_id}/query",
                                    headers=self._headers(), json=body)
        return (data or {}).get("results", [])

    def _page_doc(self, db_id: str, page: dict) -> tuple[ConnectorDoc | None, str | None]:
        page_id = page.get("id", "")
        edited = page.get("last_edited_time")
        title = _title_of(page) or page_id
        text = self._page_text(page_id)
        content = f"{title}\n\n{text}".strip()
        if not content:
            return None, edited
        return ConnectorDoc(
            source_type="notion_page",
            source_uri=page.get("url", f"notion:{page_id}"),
            title=title,
            content=content,
            metadata={"database_id": db_id, "page_id": page_id,
                      "last_edited_time": edited},
        ), edited

    def _page_text(self, page_id: str) -> str:
        data = self._http.get_json(f"{self._base}/v1/blocks/{page_id}/children",
                                   headers=self._headers(),
                                   params={"page_size": self._cfg.max_items_per_sync})
        lines: list[str] = []
        for block in (data or {}).get("results", []):
            btype = block.get("type")
            if btype not in _TEXT_BLOCKS:
                continue
            rich = (block.get(btype) or {}).get("rich_text", [])
            line = "".join(r.get("plain_text", "") for r in rich)
            if line:
                lines.append(line)
        return "\n".join(lines)

    def _headers(self) -> dict[str, str]:
        headers = {"Notion-Version": self._cfg.api_version,
                   "Content-Type": "application/json"}
        token = os.environ.get(self._cfg.token_env)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers


def _title_of(page: dict) -> str | None:
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in prop.get("title", []))
    return None


def _parse_cursor(cursor: str | None) -> dict[str, str]:
    if not cursor:
        return {}
    try:
        data = json.loads(cursor)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}
