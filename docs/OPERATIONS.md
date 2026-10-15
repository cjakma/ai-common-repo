# Operations (Phase 9 — Observability)

Read-only operational surfaces for running the gateway. Everything here is for a
**local single-user** deployment: there is no auth layer, so these endpoints are
expected to live behind `127.0.0.1` (or the docker compose internal network), not
on a public interface. Every value that could carry a secret is passed through the
redactor before it is exposed or logged — the surfaces report *hints, not values*.

## Endpoints

All are `GET` and side-effect free.

| Endpoint | Purpose | Response |
| --- | --- | --- |
| `/healthz` | Legacy liveness ping (unchanged). | `{"status":"ok"}` |
| `/v1/health` | Liveness + readiness with per-component status. | `HealthResponse` |
| `/v1/metrics` | Job-queue depth/throughput/failure-rate + operation latency. | JSON (default) or Prometheus text |
| `/v1/jobs` | Queue depth + most-recent failed jobs (redacted errors). | `JobStatusResponse` |
| `/v1/connectors` | Connector enable state + last sync watermark (cursor). | `ConnectorStatusResponse` |

### `/v1/health`

```json
{
  "status": "ok",            // "ok" when ready, else "degraded"
  "live": true,              // process is up and serving
  "ready": true,             // DB + job queue both answer
  "components": [
    {"name": "database",  "status": "ok"},
    {"name": "job_queue", "status": "ok"},
    {"name": "worker",    "status": "ok", "detail": "running"}  // only when workers.enabled
  ]
}
```

- **Liveness** (`live`) is true whenever the process can answer the request.
- **Readiness** (`ready`) requires the dependencies a *read* needs: the SQLite DB
  (`SELECT 1` ping) and the job queue (`counts()`). The background **worker is
  reported but not required** — the gateway serves recall/search without one.
- The `worker` component appears only when `workers.enabled` is true.

### `/v1/metrics`

JSON by default. For Prometheus scraping use `?format=prometheus`
(`text/plain; version=0.0.4`). Metric families (`acr_` prefix):

| Metric | Type | Meaning |
| --- | --- | --- |
| `acr_uptime_seconds` | gauge | Seconds since the service object was built. |
| `acr_embeddings_total` | gauge | Rows in the `embeddings` table (vectors indexed). |
| `acr_jobs{status="queued\|running\|done\|failed"}` | gauge | Job-queue depth by status. |
| `acr_job_failure_rate` | gauge | `failed / (done + failed)`, `0..1`. |
| `acr_operation_count{op}` | gauge | Calls to an instrumented op (`search`, `recall`). |
| `acr_operation_errors{op}` | gauge | Calls that raised. |
| `acr_operation_latency_ms{op,stat="avg\|max"}` | gauge | In-process latency. |

> Operation latency is **in-process and resets on restart** (it is the one signal
> the DB cannot reconstruct after the fact). Job counts are durable (DB-backed).

### `/v1/jobs`

Queue depth plus the most recent failed jobs (capped by
`observability.recent_failures_limit`, default 20). `last_error` is redacted.

### `/v1/connectors`

One row per configured connector (`github`, `notion`) merged with any stored sync
cursor. Connectors that have never polled still appear (`enabled`, `cursor: null`).
The cursor is an opaque sync watermark (e.g. `repo -> last-seen ISO`), redacted
defensively even though it is never a secret.

## Structured logging

`configure_logging` installs a redacting, correlated handler on the
`ai_common_repo` logger tree (called by the server factory and the `worker` CLI).

- **Format**: JSON by default (`observability.log_format: json|text`),
  `observability.log_level` sets the level.
- **Correlation**: every record carries `request_id` and `job_id`.
  - HTTP: middleware honors an inbound `X-Request-ID` (trace continuity) or mints
    one, binds it for the request, and echoes it back in the response header.
  - Worker: each handler invocation runs inside a `job_id` context, so every line
    a job logs is attributable to that job.
- **Redaction**: each record's rendered message passes through the redactor before
  emit, so a stray secret in a log call never reaches the sink.

## Recommended alarm thresholds

Single-user / local, so these are guidelines, not SLOs:

| Signal | Warn | Page |
| --- | --- | --- |
| `/v1/health` `ready=false` | — | immediate (DB or queue down) |
| `acr_jobs{status="failed"}` | > 0 sustained | growing across scrapes |
| `acr_job_failure_rate` | > 0.1 | > 0.5 |
| `acr_jobs{status="queued"}` (backlog) | > 100 and rising | not draining for 15 min |
| `acr_operation_latency_ms{op="search",stat="avg"}` | > 250 | > 1000 |
| connector cursor `updated_at` stale | > 2× poll interval | > 6× poll interval |

## Rollback

Phase 9 is **additive** — new read-only endpoints, a logging configurator, and an
in-process metrics collector. It changes no storage schema (no `SCHEMA_VERSION`
bump) and no write path. To roll back, revert the Phase 9 commit; existing data and
the on-disk DB are unaffected. If only the new endpoints are unwanted, they can be
left unrouted by removing their registrations in `api/routes.py` — the rest of the
gateway is unchanged.
