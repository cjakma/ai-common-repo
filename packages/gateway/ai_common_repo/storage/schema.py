"""SQLite DDL kept in one place. Schema version lets migrate-db evolve it."""
from __future__ import annotations

SCHEMA_VERSION = 6

DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_sessions (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  project_id TEXT,
  tool TEXT NOT NULL,
  wrapper INTEGER DEFAULT 0,
  cwd TEXT,
  git_remote TEXT,
  git_branch TEXT,
  git_commit_before TEXT,
  git_commit_after TEXT,
  started_at TEXT,
  ended_at TEXT,
  exit_code INTEGER,
  raw_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  project_id TEXT,
  scope TEXT NOT NULL,
  type TEXT NOT NULL,
  content TEXT NOT NULL,
  normalized_content TEXT,
  source_session_id TEXT,
  confidence REAL DEFAULT 0.8,
  tags_json TEXT,
  valid_from TEXT,
  valid_until TEXT,
  status TEXT DEFAULT 'active',
  supersedes_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_scope
  ON memories(user_id, project_id, scope, status);

CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  project_id TEXT,
  source_type TEXT NOT NULL,
  source_uri TEXT,
  title TEXT,
  content TEXT NOT NULL,
  metadata_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_chunks (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL,
  chunk_index INTEGER NOT NULL,
  content TEXT NOT NULL,
  metadata_json TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profiles (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  project_id TEXT,
  profile_type TEXT NOT NULL,
  stable_json TEXT NOT NULL,
  dynamic_json TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(user_id, project_id, profile_type)
);

CREATE TABLE IF NOT EXISTS embeddings (
  id TEXT PRIMARY KEY,
  object_type TEXT NOT NULL,
  object_id TEXT NOT NULL,
  model TEXT NOT NULL,
  vector BLOB NOT NULL,
  created_at TEXT NOT NULL
);
-- One current vector per object (re-embedding replaces in place).
CREATE UNIQUE INDEX IF NOT EXISTS idx_embeddings_object
  ON embeddings(object_type, object_id);

CREATE TABLE IF NOT EXISTS audit_log (
  id TEXT PRIMARY KEY,
  action TEXT NOT NULL,
  object_type TEXT,
  object_id TEXT,
  detail_json TEXT,
  created_at TEXT NOT NULL
);

-- Full-text search over memory content (Phase 1 retrieval).
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
  USING fts5(content, tokenize='unicode61', content='');

-- Full-text search over document chunks (Phase 2 RAG retrieval). Mirrors the
-- memories_fts pattern: external-content FTS keyed by document_chunks.rowid.
CREATE VIRTUAL TABLE IF NOT EXISTS document_chunks_fts
  USING fts5(content, tokenize='unicode61', content='');

-- Background job queue (Phase 1 worker foundation). Local-first: no external
-- broker. A row is claimed atomically by flipping queued->running with a guard.
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  handler TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'queued',   -- queued|running|done|failed
  idempotency_key TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  run_at TEXT NOT NULL,                     -- earliest time the job may run
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(status, run_at);
-- Idempotency: at most one live (non-failed) job per key. Failed/done keys may
-- be re-enqueued, so the uniqueness only covers in-flight work.
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idem
  ON jobs(idempotency_key)
  WHERE idempotency_key IS NOT NULL AND status IN ('queued', 'running');

-- Inter-harness contract inbox (schema v5, HARNESS_COMMON_SPEC §4). Requesters
-- (Hermes/peers) enqueue tasks; each harness runs a pull worker that claims
-- tasks for its executor name atomically. The queue is the doorbell — contract
-- bodies live in handoff files + memory keys, and results are summarized into
-- result_json. No automatic retries; stale claims requeue at most max_requeues.
CREATE TABLE IF NOT EXISTS harness_tasks (
  id TEXT PRIMARY KEY,
  requester TEXT NOT NULL,
  executor TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  priority INTEGER NOT NULL DEFAULT 0,
  task_slug TEXT,
  prompt TEXT NOT NULL,
  cwd TEXT,
  permission_profile TEXT NOT NULL DEFAULT 'accept-edits',
  review TEXT,
  project_id TEXT,
  user_id TEXT,
  timeout_s INTEGER NOT NULL DEFAULT 1800,
  result_json TEXT,
  error TEXT,
  claimed_by TEXT,
  claimed_at TEXT,
  requeues INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_harness_claim
  ON harness_tasks(executor, status, priority DESC, created_at);

-- Event outbox (schema v6, event subscription/webhook layer). The gateway
-- publishes domain events (e.g. harness.task.status_changed) here inside the
-- same process right after a successful state change; an async delivery worker
-- fans them out to configured HTTP webhook subscriptions. One row per
-- (event × subscription) so each destination gets its own attempt/retry state
-- and its own X-AICR-Delivery-ID. Never stores secrets — only payloads.
CREATE TABLE IF NOT EXISTS event_outbox (
  id TEXT PRIMARY KEY,                       -- delivery id (dlv_...)
  event_id TEXT NOT NULL,                    -- envelope id (evt_...), shared per event
  event_type TEXT NOT NULL,
  aggregate_type TEXT NOT NULL,
  aggregate_id TEXT NOT NULL,
  subscription_id TEXT NOT NULL,
  payload_json TEXT NOT NULL,                -- full envelope JSON = the webhook body
  status TEXT NOT NULL DEFAULT 'pending',    -- pending|delivering|delivered|failed|dead_letter
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 8,
  next_attempt_at TEXT NOT NULL,             -- earliest time a (re)delivery may run
  last_error TEXT,
  http_status INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(event_id, subscription_id)
);
CREATE INDEX IF NOT EXISTS idx_event_outbox_status_next
  ON event_outbox(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_event_outbox_event ON event_outbox(event_id);
CREATE INDEX IF NOT EXISTS idx_event_outbox_type ON event_outbox(event_type);

-- Incremental polling cursor per connector (Phase 4). One opaque cursor string
-- per (connector, resource); the connector encodes whatever it needs into it.
CREATE TABLE IF NOT EXISTS connector_state (
  connector TEXT NOT NULL,
  resource TEXT NOT NULL DEFAULT 'default',
  cursor TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (connector, resource)
);
"""
