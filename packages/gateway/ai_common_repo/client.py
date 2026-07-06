"""Tiny HTTP client so adapters/hooks stay decoupled from the daemon process and
share one transport. Fails soft: adapters must never break the wrapped tool."""
from __future__ import annotations

from typing import Any

import httpx


class GatewayClient:
    def __init__(self, base_url: str, timeout: float = 5.0):
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        try:
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.post(f"{self._base}{path}", json=payload)
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPError:
            return None

    def recall(self, **payload: Any) -> dict[str, Any] | None:
        return self._post("/v1/recall", payload)

    def ingest_session(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        return self._post("/v1/sessions/ingest", payload)

    def store(self, **payload: Any) -> dict[str, Any] | None:
        return self._post("/v1/memory", payload)

    def context_block(self, **recall_payload: Any) -> str:
        data = self.recall(**recall_payload)
        return data.get("context_block", "") if data else ""
