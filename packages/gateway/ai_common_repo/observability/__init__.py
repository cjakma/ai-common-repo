"""Observability: health, metrics, structured logging and read-only ops status.

All operational surfaces report *hints, not values* — every string that could
carry a secret passes through the redactor before it is exposed or logged.
"""
from __future__ import annotations

from .logging import (
    LOGGER_ROOT, configure_logging, current_job_id, current_request_id,
    job_context, request_context,
)
from .metrics import MetricsCollector
from .models import (
    ConnectorStatusResponse, HealthResponse, JobStatusResponse, MetricsResponse,
)
from .reports import (
    build_connector_status, build_health, build_job_status, build_metrics,
    render_prometheus,
)

__all__ = [
    "MetricsCollector",
    "configure_logging",
    "current_job_id",
    "current_request_id",
    "job_context",
    "request_context",
    "LOGGER_ROOT",
    "HealthResponse",
    "MetricsResponse",
    "JobStatusResponse",
    "ConnectorStatusResponse",
    "build_health",
    "build_metrics",
    "build_job_status",
    "build_connector_status",
    "render_prometheus",
]
