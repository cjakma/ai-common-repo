"""Hermes memory-provider adapter (thin).

Hermes drives its own tool/provider registry, so rather than ship an MCP client
this is a small HTTP shim over the gateway's REST surface. It deliberately holds
**no logic** beyond shaping calls — all behaviour lives in the gateway's
MemoryService. Register `HermesMemoryProvider` with your Hermes runtime (see
APPLY.md).

The gateway exposes the identical six capabilities over MCP at POST /mcp; use
this adapter only if you prefer plain REST inside Hermes.
"""
from __future__ import annotations

from typing import Any

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:28787"


class HermesMemoryProvider:
    """REST shim mapping Hermes tool calls to the gateway. Fails soft: a down
    gateway must never break the host agent (returns None / empty context)."""

    name = "ai-common-repo"

    def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout: float = 5.0):
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    # -- transport -----------------------------------------------------------
    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        try:
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.post(f"{self._base}{path}", json=payload)
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPError:
            return None

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any] | None:
        try:
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.get(f"{self._base}{path}", params=params)
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPError:
            return None

    # -- capabilities (mirror the six MCP tools) -----------------------------
    def store(self, content: str, **kw: Any) -> dict[str, Any] | None:
        return self._post("/v1/memory", {"content": content, **kw})

    def search(self, q: str, **kw: Any) -> list[dict[str, Any]]:
        data = self._post("/v1/search", {"q": q, **kw})
        return data.get("results", []) if data else []

    def recall(self, query: str, **kw: Any) -> dict[str, Any] | None:
        return self._post("/v1/recall", {"query": query, **kw})

    def context_block(self, query: str, **kw: Any) -> str:
        data = self.recall(query, **kw)
        return data.get("context_block", "") if data else ""

    def profile(self, user_id: str | None = None,
                project_id: str | None = None) -> dict[str, Any] | None:
        return self._get("/v1/profile", {"user_id": user_id, "project_id": project_id})

    def forget(self, memory_id: str) -> dict[str, Any] | None:
        return self._post(f"/v1/memory/{memory_id}/forget", {})

    def ingest_session(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        return self._post("/v1/sessions/ingest", payload)
