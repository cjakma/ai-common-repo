"""Pure builders that turn raw probe results into the operational response models,
plus a Prometheus text renderer. Kept free of I/O so they are trivially testable;
the service supplies the probed primitives (DB ping, job counts, cursors)."""
from __future__ import annotations

from ..privacy.redactor import Redactor
from ..storage.job_repository import Job
from .models import (
    ComponentHealth, ConnectorState, ConnectorStatusResponse, FailedJob,
    HealthResponse, JobMetrics, JobStatusResponse, MetricsResponse,
    OperationMetrics,
)


def build_health(*, db_ok: bool, queue_ok: bool, worker_enabled: bool,
                 worker_running: bool,
                 extra: list[ComponentHealth] | None = None) -> HealthResponse:
    """Liveness is always true while we are serving; readiness requires the
    dependencies a read needs (DB + queue). The worker is reported but not
    required — the gateway answers reads without one. ``extra`` components
    (vector store, embedding backend) degrade the status but not readiness:
    search falls back to FTS without them."""
    components = [
        ComponentHealth(name="database", status="ok" if db_ok else "down"),
        ComponentHealth(name="job_queue", status="ok" if queue_ok else "down"),
    ]
    if worker_enabled:
        components.append(ComponentHealth(
            name="worker", status="ok" if worker_running else "down",
            detail="running" if worker_running else "not running"))
    components += extra or []
    ready = db_ok and queue_ok
    all_ok = all(c.status == "ok" for c in components)
    return HealthResponse(status="ok" if ready and all_ok else "degraded",
                          live=True, ready=ready, components=components)


def job_metrics(counts: dict[str, int]) -> JobMetrics:
    done, failed = counts.get("done", 0), counts.get("failed", 0)
    denom = done + failed
    return JobMetrics(
        queued=counts.get("queued", 0), running=counts.get("running", 0),
        done=done, failed=failed,
        failure_rate=round(failed / denom, 4) if denom else 0.0)


def build_metrics(*, uptime_s: float, counts: dict[str, int], embeddings: int,
                  operations: dict[str, dict[str, float]]) -> MetricsResponse:
    return MetricsResponse(
        uptime_s=round(uptime_s, 1),
        jobs=job_metrics(counts),
        embeddings=embeddings,
        operations={name: OperationMetrics(**vals)
                    for name, vals in operations.items()})


def build_job_status(*, counts: dict[str, int], failures: list[Job],
                     redactor: Redactor) -> JobStatusResponse:
    return JobStatusResponse(
        queue=job_metrics(counts),
        recent_failures=[
            FailedJob(id=j.id, handler=j.handler, attempts=j.attempts,
                      last_error=redactor.redact(j.last_error) if j.last_error else None,
                      updated_at=j.updated_at)
            for j in failures])


def build_connector_status(*, enabled: dict[str, bool],
                           states: list[dict], redactor: Redactor
                           ) -> ConnectorStatusResponse:
    """Merge configured connectors with their stored sync watermarks. Connectors
    with no state row yet still appear (enabled, no cursor). Cursors are opaque
    watermarks, but pass through the redactor defensively (never a secret)."""
    by_connector = {s["connector"]: s for s in states}
    rows: list[ConnectorState] = []
    for name in sorted(set(enabled) | set(by_connector)):
        state = by_connector.get(name)
        rows.append(ConnectorState(
            connector=name,
            enabled=enabled.get(name, False),
            resource=state["resource"] if state else "default",
            cursor=redactor.redact(state["cursor"]) if state and state["cursor"] else None,
            updated_at=state["updated_at"] if state else None))
    return ConnectorStatusResponse(connectors=rows)


def render_prometheus(metrics: MetricsResponse) -> str:
    """Render a MetricsResponse as Prometheus text exposition format."""
    lines: list[str] = []

    def gauge(name: str, value, labels: str = "") -> None:
        lines.append(f"acr_{name}{labels} {value}")

    gauge("uptime_seconds", metrics.uptime_s)
    gauge("embeddings_total", metrics.embeddings)
    j = metrics.jobs
    for status in ("queued", "running", "done", "failed"):
        gauge("jobs", getattr(j, status), f'{{status="{status}"}}')
    gauge("job_failure_rate", j.failure_rate)
    for op, m in sorted(metrics.operations.items()):
        gauge("operation_count", m.count, f'{{op="{op}"}}')
        gauge("operation_errors", m.errors, f'{{op="{op}"}}')
        gauge("operation_latency_ms", m.avg_ms, f'{{op="{op}",stat="avg"}}')
        gauge("operation_latency_ms", m.max_ms, f'{{op="{op}",stat="max"}}')
    return "\n".join(lines) + "\n"
