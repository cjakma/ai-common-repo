"""Phase 9 — observability / operability. Exercises the metrics collector,
health/metrics/status builders, the Prometheus renderer, redacted structured
logging with request/job correlation, and the read-only ops endpoints over a
FastAPI TestClient. All offline (in-memory service over a temp DB, no sockets)."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "gateway"))

from fastapi.testclient import TestClient  # noqa: E402

from ai_common_repo.core import constants as C  # noqa: E402
from ai_common_repo.core.config import load_config  # noqa: E402
from ai_common_repo.core.models import SearchRequest, StoreMemoryRequest  # noqa: E402
from ai_common_repo.observability import (  # noqa: E402
    LOGGER_ROOT, MetricsCollector, build_connector_status, build_health,
    build_job_status, build_metrics, configure_logging, job_context,
    render_prometheus, request_context,
)
from ai_common_repo.privacy.redactor import Redactor  # noqa: E402
from ai_common_repo.server.app import create_app  # noqa: E402
from ai_common_repo.service import MemoryService  # noqa: E402
from ai_common_repo.storage.job_repository import Job  # noqa: E402
from ai_common_repo.workers import Worker  # noqa: E402


def _cfg():
    tmp = tempfile.mkdtemp()
    os.environ["AICR_STORAGE__DB_PATH"] = os.path.join(tmp, "memory.db")
    os.environ["AICR_MODELS__DEFAULT_BACKEND"] = "none"  # offline => hash embedder
    return load_config()


def _service() -> MemoryService:
    return MemoryService(_cfg())


# -- metrics collector -------------------------------------------------------
def test_metrics_collector_records_latency_and_errors():
    m = MetricsCollector()
    with m.timer("search"):
        pass
    try:
        with m.timer("search"):
            raise ValueError("boom")
    except ValueError:
        pass
    snap = m.snapshot()
    assert snap["search"]["count"] == 2
    assert snap["search"]["errors"] == 1
    assert snap["search"]["max_ms"] >= snap["search"]["avg_ms"] >= 0.0
    assert m.uptime_s() >= 0.0


# -- health builder ----------------------------------------------------------
def test_build_health_ready_when_db_and_queue_ok():
    h = build_health(db_ok=True, queue_ok=True, worker_enabled=False,
                     worker_running=False)
    assert h.live and h.ready and h.status == "ok"
    assert {c.name for c in h.components} == {"database", "job_queue"}


def test_build_health_degraded_when_db_down():
    h = build_health(db_ok=False, queue_ok=True, worker_enabled=True,
                     worker_running=False)
    assert h.live and not h.ready and h.status == "degraded"
    worker = next(c for c in h.components if c.name == "worker")
    assert worker.status == "down"  # enabled but not running


# -- metrics builder + prometheus -------------------------------------------
def test_build_metrics_and_prometheus():
    counts = {"queued": 2, "running": 1, "done": 8, "failed": 2}
    report = build_metrics(uptime_s=12.34, counts=counts, embeddings=5,
                           operations={"search": {"count": 3, "errors": 0,
                                                  "avg_ms": 1.5, "max_ms": 4.0}})
    assert report.jobs.queued == 2 and report.jobs.failed == 2
    assert report.jobs.failure_rate == 0.2  # 2 / (8 + 2)
    assert report.embeddings == 5
    text = render_prometheus(report)
    assert 'acr_jobs{status="queued"} 2' in text
    assert "acr_job_failure_rate 0.2" in text
    assert 'acr_operation_count{op="search"} 3' in text
    assert 'acr_operation_latency_ms{op="search",stat="max"} 4.0' in text


# -- job status redaction ----------------------------------------------------
def test_build_job_status_redacts_last_error():
    redactor = Redactor(_cfg().privacy)
    failures = [Job(id="job_1", handler="connector_poll", attempts=3,
                    last_error="auth failed token=sk-ABCDEFGH12345 retry",
                    updated_at="2026-06-30T00:00:00Z")]
    status = build_job_status(counts={"failed": 1}, failures=failures,
                              redactor=redactor)
    assert status.queue.failed == 1
    err = status.recent_failures[0].last_error
    assert "sk-ABCDEFGH12345" not in err and "[REDACTED]" in err


# -- connector status --------------------------------------------------------
def test_build_connector_status_merges_and_redacts():
    redactor = Redactor(_cfg().privacy)
    states = [{"connector": "github", "resource": "default",
               "cursor": "watermark sk-SECRET99 tail",
               "updated_at": "2026-06-30T00:00:00Z"}]
    out = build_connector_status(
        enabled={"github": True, "notion": False}, states=states, redactor=redactor)
    by_name = {c.connector: c for c in out.connectors}
    assert by_name["github"].enabled is True
    assert "sk-SECRET99" not in by_name["github"].cursor
    # notion is configured but never polled => present, enabled, no cursor.
    assert by_name["notion"].enabled is False and by_name["notion"].cursor is None


# -- structured logging ------------------------------------------------------
def test_logging_redacts_and_correlates():
    redactor = Redactor(_cfg().privacy)
    logger = configure_logging(redactor, fmt="json")
    stream = io.StringIO()
    logger.handlers[0].stream = stream
    with request_context("req-123"), job_context("job-abc"):
        logger.info("leaking sk-ABCDEFGH12345 in a log line")
    data = json.loads(stream.getvalue().strip())
    assert data["request_id"] == "req-123" and data["job_id"] == "job-abc"
    assert "sk-ABCDEFGH12345" not in stream.getvalue()
    assert "[REDACTED]" in data["msg"]
    assert data["logger"] == LOGGER_ROOT


# -- service-level wiring ----------------------------------------------------
def test_service_metrics_count_search():
    svc = _service()
    svc.store_memory(StoreMemoryRequest(content="postgres is the database"))
    svc.search(SearchRequest(q="database", search_mode=C.SEARCH_MODE_FTS))
    report = svc.metrics_report()
    assert report.operations["search"].count == 1
    assert report.uptime_s >= 0.0


def test_service_job_status_reports_failure():
    svc = _service()
    # A job whose handler is missing fails on its single allowed attempt.
    svc.jobs.enqueue("does_not_exist", {"x": 1}, max_attempts=1)
    Worker(svc.jobs, svc.handlers).run_once()
    status = svc.job_status()
    assert status.queue.failed == 1
    assert status.recent_failures[0].handler == "does_not_exist"


# -- HTTP endpoints ----------------------------------------------------------
def test_ops_endpoints_over_http():
    app = create_app(_cfg())
    with TestClient(app) as client:
        r = client.get("/v1/health")
        assert r.status_code == 200
        body = r.json()
        assert body["live"] is True and body["ready"] is True
        assert r.headers.get("X-Request-ID")  # correlation id echoed back

        # caller-supplied request id is honored end-to-end.
        r2 = client.get("/v1/health", headers={"X-Request-ID": "trace-xyz"})
        assert r2.headers["X-Request-ID"] == "trace-xyz"

        m = client.get("/v1/metrics").json()
        assert "jobs" in m and "operations" in m and m["uptime_s"] >= 0.0

        prom = client.get("/v1/metrics", params={"format": "prometheus"})
        assert prom.headers["content-type"].startswith("text/plain")
        assert "acr_jobs{status=" in prom.text

        jobs = client.get("/v1/jobs").json()
        assert "queue" in jobs and "recent_failures" in jobs

        conns = client.get("/v1/connectors").json()
        names = {c["connector"] for c in conns["connectors"]}
        assert {"github", "notion"} <= names
