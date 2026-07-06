"""Backend abstractions. Services depend only on these; concrete providers
(OpenAI-compatible, Ollama, vLLM) are interchangeable and config-selected."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class ChatMessage:
    role: str
    content: str


@dataclass
class BackendHealth:
    name: str
    provider: str
    base_url: str
    status: str          # ok | unreachable | error
    models: list[str]
    detail: str | None = None


@runtime_checkable
class ChatBackend(Protocol):
    name: str

    def chat(self, messages: list[ChatMessage], *, model: str | None = None,
             temperature: float = 0.2, max_tokens: int = 1024) -> str: ...

    def health(self) -> BackendHealth: ...


@runtime_checkable
class EmbeddingBackend(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class BackendError(RuntimeError):
    pass
