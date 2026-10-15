"""Pydantic response shapes for the operational endpoints (health / metrics /
status). Read-only views — they never echo secrets (values are redacted upstream)."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ComponentHealth(BaseModel):
    name: str
    status: str  # "ok" | "down"
    detail: str | None = None
    info: dict[str, Any] | None = None


class HealthResponse(BaseModel):
    status: str  # "ok" | "degraded"
    live: bool
    ready: bool
    components: list[ComponentHealth] = Field(default_factory=list)


class JobMetrics(BaseModel):
    queued: int = 0
    running: int = 0
    done: int = 0
    failed: int = 0
    failure_rate: float = 0.0  # failed / (done + failed), 0..1


class OperationMetrics(BaseModel):
    count: int = 0
    errors: int = 0
    avg_ms: float = 0.0
    max_ms: float = 0.0


class MetricsResponse(BaseModel):
    uptime_s: float
    jobs: JobMetrics
    embeddings: int = 0
    operations: dict[str, OperationMetrics] = Field(default_factory=dict)


class FailedJob(BaseModel):
    id: str
    handler: str
    attempts: int
    last_error: str | None = None
    updated_at: str | None = None


class JobStatusResponse(BaseModel):
    queue: JobMetrics
    recent_failures: list[FailedJob] = Field(default_factory=list)


class ConnectorState(BaseModel):
    connector: str
    enabled: bool
    resource: str = "default"
    cursor: str | None = None  # opaque sync watermark (redacted, never a secret)
    updated_at: str | None = None


class ConnectorStatusResponse(BaseModel):
    connectors: list[ConnectorState] = Field(default_factory=list)
