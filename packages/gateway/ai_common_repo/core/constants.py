"""Central registry of non-configurable enums and identifiers.

Everything that *could* vary at deploy time belongs in config, not here. This
module only holds closed vocabularies the schema/business-logic agree on.
"""
from __future__ import annotations

# Environment variable that overrides the config file location.
ENV_CONFIG_PATH = "AICR_CONFIG"
# Prefix for scalar config overrides, e.g. AICR_SERVER__PORT=29000
ENV_OVERRIDE_PREFIX = "AICR_"
# Explicit project id override consumed by project_scope.
ENV_PROJECT_ID = "AI_COMMON_REPO_PROJECT_ID"

DEFAULT_CONFIG_FILENAME = "config.yaml"
PACKAGED_DEFAULT_CONFIG = "default.yaml"  # shipped under configs/

SCHEMA_SESSION = "ai-common-repo.session.v1"

# memories.scope
MEMORY_SCOPES = ("user", "project", "repo", "global")

# memories.type
MEMORY_TYPES = (
    "preference",
    "constraint",
    "decision",
    "workflow",
    "project_fact",
    "warning",
    "credential_hint",
    "handoff",
    "todo_context",
)

# memories.status
MEMORY_STATUS_ACTIVE = "active"
MEMORY_STATUS_SUPERSEDED = "superseded"
MEMORY_STATUS_FORGOTTEN = "forgotten"
MEMORY_STATUS_EXPIRED = "expired"  # auto-forget job: past valid_until / decayed
MEMORY_STATUSES = (MEMORY_STATUS_ACTIVE, MEMORY_STATUS_SUPERSEDED,
                   MEMORY_STATUS_FORGOTTEN, MEMORY_STATUS_EXPIRED)

# jobs.status (background worker)
JOB_STATUS_QUEUED = "queued"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_DONE = "done"
JOB_STATUS_FAILED = "failed"
JOB_STATUSES = (JOB_STATUS_QUEUED, JOB_STATUS_RUNNING, JOB_STATUS_DONE, JOB_STATUS_FAILED)

# job handler names (registry keys)
JOB_EXTRACT_SESSION = "extract_session"
JOB_REBUILD_PROFILE = "rebuild_profile"
JOB_FORGET_EXPIRED = "forget_expired"
JOB_EMBED = "embed"
JOB_CONNECTOR_POLL = "connector_poll"
JOB_DEDUP_MEMORIES = "dedup_memories"

# connector names (registry keys / connector_state.connector)
CONNECTOR_GITHUB = "github"
CONNECTOR_NOTION = "notion"

# harness_tasks.status — the inter-harness contract-inbox queue. A task is
# claimed atomically by one executor runner; there are no automatic retries
# (code-executing tasks are not idempotent), only a bounded stale-claim requeue.
HARNESS_STATUS_PENDING = "pending"
HARNESS_STATUS_CLAIMED = "claimed"
HARNESS_STATUS_RUNNING = "running"
HARNESS_STATUS_DONE = "done"
HARNESS_STATUS_FAILED = "failed"
HARNESS_STATUS_CANCELLED = "cancelled"
HARNESS_STATUSES = (HARNESS_STATUS_PENDING, HARNESS_STATUS_CLAIMED,
                    HARNESS_STATUS_RUNNING, HARNESS_STATUS_DONE,
                    HARNESS_STATUS_FAILED, HARNESS_STATUS_CANCELLED)
# Transitions a runner/requester may request via the status endpoint. Claiming
# (pending->claimed) only happens through the atomic claim call, and the
# stale-claim requeue (claimed/running->pending) only through the queue service.
HARNESS_STATUS_TRANSITIONS = {
    HARNESS_STATUS_PENDING: (HARNESS_STATUS_CANCELLED,),
    HARNESS_STATUS_CLAIMED: (HARNESS_STATUS_RUNNING, HARNESS_STATUS_FAILED,
                             HARNESS_STATUS_CANCELLED),
    HARNESS_STATUS_RUNNING: (HARNESS_STATUS_DONE, HARNESS_STATUS_FAILED,
                             HARNESS_STATUS_CANCELLED),
}

# event bus / webhook outbox (event subscription layer). The gateway publishes
# domain events into a durable outbox and an async worker fans them out to
# configured generic HTTP webhook subscriptions — core stays platform-agnostic
# (no Slack/Discord/Hermes coupling).
EVENT_SOURCE = "ai-common-repo"
EVENT_ENVELOPE_VERSION = "1"
EVENT_HARNESS_TASK_STATUS_CHANGED = "harness.task.status_changed"
EVENT_WEBHOOK_TEST = "webhook.test"
EVENT_AGGREGATE_HARNESS_TASK = "harness_task"

# harness statuses that are terminal (easy subscription filtering / notify-once).
HARNESS_TERMINAL_STATUSES = ("done", "failed", "cancelled")

# event_outbox.status — one row per (event × subscription); delivery is
# at-least-once with bounded retry, then dead-letter.
OUTBOX_STATUS_PENDING = "pending"
OUTBOX_STATUS_DELIVERING = "delivering"
OUTBOX_STATUS_DELIVERED = "delivered"
OUTBOX_STATUS_FAILED = "failed"
OUTBOX_STATUS_DEAD_LETTER = "dead_letter"
OUTBOX_STATUSES = (OUTBOX_STATUS_PENDING, OUTBOX_STATUS_DELIVERING,
                   OUTBOX_STATUS_DELIVERED, OUTBOX_STATUS_FAILED,
                   OUTBOX_STATUS_DEAD_LETTER)

# Webhook signature / delivery headers (see events.signing).
HDR_EVENT_ID = "X-AICR-Event-ID"
HDR_EVENT_TYPE = "X-AICR-Event-Type"
HDR_DELIVERY_ID = "X-AICR-Delivery-ID"
HDR_TIMESTAMP = "X-AICR-Timestamp"
HDR_SIGNATURE = "X-AICR-Signature"

# embeddings.object_type — what a stored vector points back to.
OBJECT_TYPE_MEMORY = "memory"
OBJECT_TYPE_CHUNK = "document_chunk"

# search modes (SearchRequest.search_mode)
SEARCH_MODE_FTS = "fts"
SEARCH_MODE_VECTOR = "vector"
SEARCH_MODE_HYBRID = "hybrid"

# vector index backend selection (storage.vector_backend)
VECTOR_BACKEND_QDRANT = "qdrant"        # canonical backend (operator directive)
VECTOR_BACKEND_AUTO = "auto"            # sqlite-vec if importable, else bruteforce
VECTOR_BACKEND_BRUTEFORCE = "bruteforce"
VECTOR_BACKEND_SQLITE_VEC = "sqlite-vec"

# conflict resolver verdicts
RESOLUTION_DUPLICATE = "duplicate"
RESOLUTION_UPDATE = "update"
RESOLUTION_CONTRADICTION = "contradiction"
RESOLUTION_SUPERSEDES = "supersedes"
RESOLUTION_INDEPENDENT = "independent"

# model backend task names (routing keys)
TASK_EXTRACTION = "extraction"
TASK_SUMMARIZATION = "summarization"
TASK_CONFLICT = "conflict_resolution"
TASK_RERANKING = "reranking"
TASK_EMBEDDING = "embedding"
