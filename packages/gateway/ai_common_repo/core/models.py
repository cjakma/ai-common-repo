"""Domain + API DTOs (pydantic). Shared by storage, services and the HTTP layer
so there is exactly one definition of each shape."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from . import constants as C


# ---- domain ---------------------------------------------------------------
class Memory(BaseModel):
    id: str
    user_id: str
    project_id: str | None = None
    scope: str = "project"
    type: str = "project_fact"
    content: str
    normalized_content: str | None = None
    source_session_id: str | None = None
    confidence: float = 0.8
    valid_from: str | None = None
    valid_until: str | None = None
    status: str = C.MEMORY_STATUS_ACTIVE
    supersedes_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str


class MemoryCandidate(BaseModel):
    type: str = "project_fact"
    scope: str = "project"
    content: str
    confidence: float = 0.8
    reason: str | None = None


class Profile(BaseModel):
    user_id: str
    project_id: str | None = None
    stable: list[str] = Field(default_factory=list)
    dynamic: list[str] = Field(default_factory=list)
    updated_at: str | None = None


# ---- API requests ---------------------------------------------------------
class RecallRequest(BaseModel):
    query: str
    user_id: str | None = None
    project_id: str | None = None
    tool: str | None = None
    cwd: str | None = None
    limit: int | None = None
    include_profile: bool = True
    include_recent: bool = True


class RecallResponse(BaseModel):
    profile: dict[str, list[str]]
    memories: list[dict[str, Any]]
    context_block: str


class StoreMemoryRequest(BaseModel):
    scope: str = "project"
    user_id: str | None = None
    project_id: str | None = None
    type: str = "decision"
    content: str
    tags: list[str] = Field(default_factory=list)
    confidence: float = 0.9


class GitInfo(BaseModel):
    remote: str | None = None
    branch: str | None = None
    commit_before: str | None = None
    commit_after: str | None = None


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str


class SessionIngestRequest(BaseModel):
    schema_: str = Field(default=C.SCHEMA_SESSION, alias="schema")
    session_id: str | None = None
    tool: str
    wrapper: bool = False
    user_id: str | None = None
    project_id: str | None = None
    cwd: str | None = None
    git: GitInfo | None = None
    messages: list[Message] = Field(default_factory=list)
    artifacts: dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


class SearchRequest(BaseModel):
    q: str
    scope: list[str] | None = None
    user_id: str | None = None
    project_id: str | None = None
    limit: int = 20
    search_mode: Literal["fts", "vector", "hybrid"] = "fts"
    # Whether to fuse document chunks into results. None => config default.
    include_documents: bool | None = None
    # Require every listed tag on a memory (harness key addressing). Tag-filtered
    # search is memory-only: chunks carry no tags, so they are excluded.
    tags: list[str] | None = None


class SearchHit(BaseModel):
    """Unified retrieval result spanning memories and document chunks. The
    ``object_type`` field labels the source so callers can distinguish them."""
    object_type: str = C.OBJECT_TYPE_MEMORY
    id: str
    content: str
    score: float
    # memory-only
    type: str | None = None
    scope: str | None = None
    status: str | None = None
    source_session_id: str | None = None
    # document-chunk-only
    document_id: str | None = None
    document_title: str | None = None
    chunk_index: int | None = None


class HarnessTask(BaseModel):
    """One inter-harness contract-inbox item (HARNESS_COMMON_SPEC §4)."""
    id: str
    requester: str
    executor: str
    status: str = C.HARNESS_STATUS_PENDING
    priority: int = 0
    task_slug: str | None = None
    prompt: str
    cwd: str | None = None
    permission_profile: str = "accept-edits"
    review: str | None = None
    project_id: str | None = None
    user_id: str | None = None
    timeout_s: int = 1800
    result: dict[str, Any] | None = None
    error: str | None = None
    claimed_by: str | None = None
    claimed_at: str | None = None
    requeues: int = 0
    created_at: str
    updated_at: str


class HarnessTaskCreate(BaseModel):
    requester: str = Field(..., min_length=1)
    executor: str = Field(..., min_length=1)
    prompt: str = Field(..., min_length=1)
    priority: int = 0
    task_slug: str | None = None
    cwd: str | None = None
    permission_profile: str = "accept-edits"
    review: str | None = None
    project_id: str | None = None
    user_id: str | None = None
    timeout_s: int | None = None  # None => config default


class HarnessClaimRequest(BaseModel):
    executor: str = Field(..., min_length=1)
    worker_id: str = Field(..., min_length=1)


class HarnessStatusUpdate(BaseModel):
    status: Literal["running", "done", "failed", "cancelled"]
    worker_id: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


class EventAggregate(BaseModel):
    type: str
    id: str


class EventEnvelope(BaseModel):
    """Stable outbound event envelope (event subscription layer). The webhook
    body is exactly this shape; ``payload`` carries the event-type-specific data."""
    event_id: str
    event_type: str
    occurred_at: str
    source: str = C.EVENT_SOURCE
    version: str = C.EVENT_ENVELOPE_VERSION
    aggregate: EventAggregate
    payload: dict[str, Any] = Field(default_factory=dict)


class OutboxEvent(BaseModel):
    """One durable (event × subscription) delivery record from event_outbox."""
    id: str                    # delivery id (dlv_...)
    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    subscription_id: str
    payload: dict[str, Any]    # the full envelope
    status: str = C.OUTBOX_STATUS_PENDING
    attempts: int = 0
    max_attempts: int = 8
    next_attempt_at: str
    last_error: str | None = None
    http_status: int | None = None
    created_at: str
    updated_at: str


class DocumentIngestRequest(BaseModel):
    user_id: str | None = None
    project_id: str | None = None
    source_type: str = "text"
    source_uri: str | None = None
    title: str | None = None
    # Provide inline content, or a server-readable path (gated by allowed_roots).
    content: str | None = None
    path: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
