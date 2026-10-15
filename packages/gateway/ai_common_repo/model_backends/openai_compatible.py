"""OpenAI-compatible chat/embedding client (vLLM, Ollama /v1, LM Studio, ...).

Deliberately uses `max_completion_tokens` with a `max_tokens` fallback so it
works across both legacy and GPT-5-era endpoints — the exact incompatibility
that broke the old supermemory memory-agent."""
from __future__ import annotations

import httpx

from ..core.config import BackendConfig
from .base import BackendHealth, ChatMessage


class OpenAICompatibleBackend:
    def __init__(self, name: str, cfg: BackendConfig):
        self.name = name
        self._cfg = cfg
        self._base = cfg.base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {cfg.api_key}",
                         "Content-Type": "application/json"}

    # -- chat ----------------------------------------------------------------
    def chat(self, messages: list[ChatMessage], *, model: str | None = None,
             temperature: float = 0.2, max_tokens: int = 1024) -> str:
        payload = {
            "model": model or self._cfg.chat_model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
        }
        with httpx.Client(timeout=self._cfg.timeout_s) as client:
            data = self._post_chat(client, payload, max_tokens)
        return data["choices"][0]["message"]["content"]

    def _post_chat(self, client: httpx.Client, payload: dict, max_tokens: int) -> dict:
        body = {**payload, "max_completion_tokens": max_tokens}
        resp = client.post(f"{self._base}/chat/completions",
                           json=body, headers=self._headers)
        if resp.status_code == 400 and "max_tokens" in resp.text:
            body = {**payload, "max_tokens": max_tokens}
            resp = client.post(f"{self._base}/chat/completions",
                               json=body, headers=self._headers)
        resp.raise_for_status()
        return resp.json()

    # -- embedding (OpenAI /v1/embeddings shape) -----------------------------
    @property
    def model(self) -> str:
        return self._cfg.embedding_model or self._cfg.chat_model or "external"

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed stored content (memories, document chunks)."""
        return self._embed(texts, self._cfg.embedding_document_prefix)

    def embed_query(self, texts: list[str]) -> list[list[float]]:
        """Embed search queries (models with asymmetric prompts differ here)."""
        return self._embed(texts, self._cfg.embedding_query_prefix)

    def _embed(self, texts: list[str], prefix: str) -> list[list[float]]:
        limit = self._cfg.embedding_max_chars
        inputs = [prefix + (t[:limit] if limit else t) for t in texts]
        with httpx.Client(timeout=self._cfg.timeout_s) as client:
            resp = client.post(f"{self._base}/embeddings",
                               json={"model": self.model, "input": inputs},
                               headers=self._headers)
            resp.raise_for_status()
            data = resp.json()
        return [item["embedding"] for item in data["data"]]

    # -- health --------------------------------------------------------------
    def health(self) -> BackendHealth:
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(f"{self._base}/models", headers=self._headers)
            if resp.status_code < 400:
                models = [m.get("id", "?") for m in resp.json().get("data", [])]
                return BackendHealth(self.name, "openai_compatible", self._base,
                                     "ok", models)
            return BackendHealth(self.name, "openai_compatible", self._base,
                                 "error", [], f"HTTP {resp.status_code}")
        except httpx.HTTPError as exc:
            return BackendHealth(self.name, "openai_compatible", self._base,
                                 "unreachable", [], str(exc))
