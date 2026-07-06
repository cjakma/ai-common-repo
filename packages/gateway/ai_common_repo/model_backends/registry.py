"""Builds backends from config and routes per-task. The rest of the app asks the
registry for a backend by task name and never touches provider classes."""
from __future__ import annotations

from ..core import constants as C
from ..core.config import ModelsConfig
from .base import BackendHealth, ChatBackend, EmbeddingBackend
from .hash_embedding import HashEmbeddingBackend
from .openai_compatible import OpenAICompatibleBackend

_PROVIDERS = {
    "openai_compatible": OpenAICompatibleBackend,
    # ollama /v1 is OpenAI-compatible; native embedding handled inside the client.
    "ollama": OpenAICompatibleBackend,
}


class BackendRegistry:
    def __init__(self, cfg: ModelsConfig):
        self._cfg = cfg
        self._backends: dict[str, ChatBackend] = {}
        for name, bcfg in cfg.backends.items():
            provider_cls = _PROVIDERS.get(bcfg.provider, OpenAICompatibleBackend)
            self._backends[name] = provider_cls(name, bcfg)

    def get(self, name: str) -> ChatBackend | None:
        return self._backends.get(name)

    def for_task(self, task: str) -> ChatBackend | None:
        return self.get(self._cfg.backend_for(task))

    def embedder(self, dim: int = 256) -> EmbeddingBackend:
        """Embedding backend for the embedding task. Falls back to the offline
        deterministic hash embedder when no backend that can ``embed`` is wired
        (unset task route, ``none`` backend, or a chat-only provider)."""
        backend = self.get(self._cfg.backend_for(C.TASK_EMBEDDING))
        if backend is not None and callable(getattr(backend, "embed", None)):
            return backend  # type: ignore[return-value]
        return HashEmbeddingBackend(dim)

    def health_all(self) -> list[BackendHealth]:
        return [b.health() for b in self._backends.values()]
