"""Data-access objects. All SQL lives here; services depend on these interfaces,
never on sqlite directly."""
from __future__ import annotations

import json
import struct
from typing import Any

from ..core import constants as C
from ..core.ids import now_iso, prefixed
from ..core.models import HarnessTask, Memory, OutboxEvent, Profile
from .database import Database, database_locked_methods


def _row_to_memory(row: Any) -> Memory:
    return Memory(
        id=row["id"],
        user_id=row["user_id"],
        project_id=row["project_id"],
        scope=row["scope"],
        type=row["type"],
        content=row["content"],
        normalized_content=row["normalized_content"],
        source_session_id=row["source_session_id"],
        confidence=row["confidence"],
        tags=json.loads(row["tags_json"] or "[]"),
        valid_from=row["valid_from"],
        valid_until=row["valid_until"],
        status=row["status"],
        supersedes_id=row["supersedes_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@database_locked_methods
class MemoryRepository:
    def __init__(self, db: Database):
        self._db = db

    def add(self, mem: Memory) -> Memory:
        c = self._db.conn
        c.execute(
            """INSERT INTO memories
               (id,user_id,project_id,scope,type,content,normalized_content,
                source_session_id,confidence,tags_json,valid_from,valid_until,
                status,supersedes_id,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mem.id, mem.user_id, mem.project_id, mem.scope, mem.type, mem.content,
             mem.normalized_content, mem.source_session_id, mem.confidence,
             json.dumps(mem.tags), mem.valid_from, mem.valid_until, mem.status,
             mem.supersedes_id, mem.created_at, mem.updated_at),
        )
        c.execute("INSERT INTO memories_fts(rowid, content) VALUES "
                  "((SELECT rowid FROM memories WHERE id=?), ?)", (mem.id, mem.content))
        c.commit()
        return mem

    def set_status(self, memory_id: str, status: str) -> None:
        c = self._db.conn
        c.execute("UPDATE memories SET status=?, updated_at=? WHERE id=?",
                  (status, now_iso(), memory_id))
        c.commit()

    def active_for_scope(self, user_id: str, project_id: str | None,
                         limit: int = 200) -> list[Memory]:
        rows = self._db.conn.execute(
            """SELECT * FROM memories
               WHERE user_id=? AND status=?
                 AND (project_id IS ? OR project_id=? OR scope IN ('user','global'))
               ORDER BY updated_at DESC LIMIT ?""",
            (user_id, C.MEMORY_STATUS_ACTIVE, project_id, project_id, limit),
        ).fetchall()
        return [_row_to_memory(r) for r in rows]

    def active_all(self, limit: int = 5000) -> list[Memory]:
        """All active memories (across users/projects). Used by the consolidation
        job, which groups them by visibility scope before comparing."""
        rows = self._db.conn.execute(
            "SELECT * FROM memories WHERE status=? ORDER BY updated_at DESC LIMIT ?",
            (C.MEMORY_STATUS_ACTIVE, limit),
        ).fetchall()
        return [_row_to_memory(r) for r in rows]

    def expire_due(self, now: str, confidence_floor: float = 0.0) -> list[str]:
        """Mark active memories as expired when past valid_until, or (when
        confidence_floor > 0) when their confidence has decayed below it.
        Returns the ids that were expired so callers can audit them."""
        c = self._db.conn
        rows = c.execute(
            """SELECT id FROM memories
               WHERE status=?
                 AND ((valid_until IS NOT NULL AND valid_until<=?)
                      OR (?>0 AND confidence<?))""",
            (C.MEMORY_STATUS_ACTIVE, now, confidence_floor, confidence_floor),
        ).fetchall()
        ids = [r["id"] for r in rows]
        for mem_id in ids:
            c.execute("UPDATE memories SET status=?, updated_at=? WHERE id=?",
                      (C.MEMORY_STATUS_EXPIRED, now_iso(), mem_id))
        c.commit()
        return ids

    def by_ids(self, ids: list[str]) -> dict[str, Memory]:
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        rows = self._db.conn.execute(
            f"SELECT * FROM memories WHERE id IN ({placeholders})", ids).fetchall()
        return {r["id"]: _row_to_memory(r) for r in rows}

    def search_fts(self, query: str, user_id: str, project_id: str | None,
                   limit: int = 20,
                   tags: list[str] | None = None) -> list[tuple[Memory, float]]:
        # bm25 lower == better; convert to a 0..1-ish score.
        # When a project_id is given, scope results to that project PLUS
        # cross-project user/global memories (so other projects don't leak in).
        sql = ["""SELECT m.*, bm25(memories_fts) AS rank
                  FROM memories_fts
                  JOIN memories m ON m.rowid = memories_fts.rowid
                  WHERE memories_fts MATCH ? AND m.user_id=? AND m.status=?"""]
        params: list = [_fts_query(query), user_id, C.MEMORY_STATUS_ACTIVE]
        if project_id is not None:
            sql.append("AND (m.project_id=? OR m.scope IN ('user','global'))")
            params.append(project_id)
        for tag in tags or []:
            # Every requested tag must be present (AND semantics) — harness key
            # addressing relies on exact tag matches, not fuzzy text.
            sql.append("AND EXISTS (SELECT 1 FROM json_each(m.tags_json) WHERE value=?)")
            params.append(tag)
        sql.append("ORDER BY rank LIMIT ?")
        params.append(limit)
        rows = self._db.conn.execute(" ".join(sql), params).fetchall()
        out = []
        for r in rows:
            score = 1.0 / (1.0 + max(r["rank"], 0.0))
            out.append((_row_to_memory(r), score))
        return out


@database_locked_methods
class ProfileRepository:
    def __init__(self, db: Database):
        self._db = db

    def get(self, user_id: str, project_id: str | None,
            profile_type: str = "combined") -> Profile:
        row = self._db.conn.execute(
            "SELECT * FROM profiles WHERE user_id=? AND project_id IS ? AND profile_type=?",
            (user_id, project_id, profile_type),
        ).fetchone()
        if not row:
            return Profile(user_id=user_id, project_id=project_id)
        return Profile(
            user_id=user_id, project_id=project_id,
            stable=json.loads(row["stable_json"]),
            dynamic=json.loads(row["dynamic_json"]),
            updated_at=row["updated_at"],
        )

    def upsert(self, profile: Profile, profile_type: str = "combined") -> None:
        c = self._db.conn
        c.execute(
            """INSERT INTO profiles(id,user_id,project_id,profile_type,stable_json,
                                    dynamic_json,updated_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(user_id,project_id,profile_type) DO UPDATE SET
                 stable_json=excluded.stable_json,
                 dynamic_json=excluded.dynamic_json,
                 updated_at=excluded.updated_at""",
            (prefixed("prof"), profile.user_id, profile.project_id, profile_type,
             json.dumps(profile.stable), json.dumps(profile.dynamic), now_iso()),
        )
        c.commit()


@database_locked_methods
class SessionRepository:
    def __init__(self, db: Database):
        self._db = db

    def add(self, *, session_id: str, user_id: str, project_id: str | None,
            tool: str, wrapper: bool, cwd: str | None, git: dict[str, Any],
            exit_code: int | None, raw_json: str) -> str:
        c = self._db.conn
        c.execute(
            """INSERT OR REPLACE INTO raw_sessions
               (id,user_id,project_id,tool,wrapper,cwd,git_remote,git_branch,
                git_commit_before,git_commit_after,started_at,ended_at,exit_code,
                raw_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (session_id, user_id, project_id, tool, int(wrapper), cwd,
             git.get("remote"), git.get("branch"), git.get("commit_before"),
             git.get("commit_after"), now_iso(), now_iso(), exit_code,
             raw_json, now_iso()),
        )
        c.commit()
        return session_id

    def get(self, session_id: str) -> dict[str, Any] | None:
        row = self._db.conn.execute(
            "SELECT id, user_id, project_id, tool, raw_json FROM raw_sessions WHERE id=?",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None


@database_locked_methods
class AuditRepository:
    def __init__(self, db: Database):
        self._db = db

    def log(self, action: str, object_type: str | None = None,
            object_id: str | None = None, detail: dict[str, Any] | None = None) -> None:
        c = self._db.conn
        c.execute(
            "INSERT INTO audit_log(id,action,object_type,object_id,detail_json,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (prefixed("aud"), action, object_type, object_id,
             json.dumps(detail or {}), now_iso()),
        )
        c.commit()


@database_locked_methods
class EmbeddingRepository:
    """Persistence for vectors. Stores math-free: similarity lives in the search
    layer (BruteForceVectorIndex) or, when available, in SQL via ``vec_search``.
    Vectors are packed as little-endian float32 BLOBs."""

    def __init__(self, db: Database):
        self._db = db

    def upsert(self, object_type: str, object_id: str, model: str,
               vector: list[float]) -> None:
        c = self._db.conn
        c.execute(
            """INSERT INTO embeddings(id,object_type,object_id,model,vector,created_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(object_type,object_id) DO UPDATE SET
                 model=excluded.model, vector=excluded.vector,
                 created_at=excluded.created_at""",
            (prefixed("emb"), object_type, object_id, model,
             _pack_vector(vector), now_iso()),
        )
        c.commit()

    def vectors_for(self, object_types: list[str]) -> list[tuple[str, str, list[float]]]:
        if not object_types:
            return []
        placeholders = ",".join("?" * len(object_types))
        rows = self._db.conn.execute(
            f"SELECT object_type,object_id,vector FROM embeddings "
            f"WHERE object_type IN ({placeholders})",
            list(object_types),
        ).fetchall()
        return [(r["object_type"], r["object_id"], _unpack_vector(r["vector"]))
                for r in rows]

    def delete(self, object_type: str, object_id: str) -> None:
        c = self._db.conn
        c.execute("DELETE FROM embeddings WHERE object_type=? AND object_id=?",
                  (object_type, object_id))
        c.commit()

    def count(self) -> int:
        return self._db.conn.execute(
            "SELECT COUNT(*) AS n FROM embeddings").fetchone()["n"]

    def vec_search(self, query: list[float], object_types: list[str],
                   limit: int) -> list[tuple[str, str, float]]:
        """ANN-ish search pushed into SQL via the sqlite-vec extension. Only
        usable when the extension loaded at connection time (see Database).
        Returns (object_type, object_id, similarity) with similarity in 0..1."""
        if not object_types:
            return []
        import sqlite_vec  # noqa: F401 - import guards availability

        placeholders = ",".join("?" * len(object_types))
        rows = self._db.conn.execute(
            f"SELECT object_type,object_id,"
            f"       vec_distance_cosine(vector, ?) AS dist "
            f"FROM embeddings WHERE object_type IN ({placeholders}) "
            f"ORDER BY dist LIMIT ?",
            [_pack_vector(query), *object_types, limit],
        ).fetchall()
        return [(r["object_type"], r["object_id"], 1.0 - float(r["dist"]))
                for r in rows]


def _chunk_view(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "document_id": row["document_id"],
        "chunk_index": row["chunk_index"],
        "content": row["content"],
        "metadata": json.loads(row["metadata_json"] or "{}"),
        "user_id": row["user_id"],
        "project_id": row["project_id"],
        "title": row["title"],
        "source_uri": row["source_uri"],
        "source_type": row["source_type"],
    }


_CHUNK_COLS = """ch.id, ch.document_id, ch.chunk_index, ch.content, ch.metadata_json,
           d.user_id, d.project_id, d.title, d.source_uri, d.source_type"""

_CHUNK_SELECT = f"""
    SELECT {_CHUNK_COLS}
    FROM document_chunks ch JOIN documents d ON d.id = ch.document_id
"""

# FTS variant: drive off the FTS virtual table and expose bm25 as ``rank``.
_CHUNK_FTS_SELECT = f"""
    SELECT {_CHUNK_COLS}, bm25(document_chunks_fts) AS rank
    FROM document_chunks_fts
    JOIN document_chunks ch ON ch.rowid = document_chunks_fts.rowid
    JOIN documents d ON d.id = ch.document_id
"""


@database_locked_methods
class DocumentRepository:
    """Documents + their chunks. Chunk content is mirrored into an FTS table so
    document retrieval works the same way memory retrieval does."""

    def __init__(self, db: Database):
        self._db = db

    def add_document(self, *, user_id: str, project_id: str | None,
                     source_type: str, source_uri: str | None, title: str | None,
                     content: str, metadata: dict[str, Any] | None = None) -> str:
        doc_id = prefixed("doc")
        c = self._db.conn
        c.execute(
            """INSERT INTO documents(id,user_id,project_id,source_type,source_uri,
                                     title,content,metadata_json,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (doc_id, user_id, project_id, source_type, source_uri, title, content,
             json.dumps(metadata or {}), now_iso(), now_iso()),
        )
        c.commit()
        return doc_id

    def add_chunk(self, *, document_id: str, chunk_index: int, content: str,
                  metadata: dict[str, Any] | None = None) -> str:
        chunk_id = prefixed("chk")
        c = self._db.conn
        c.execute(
            """INSERT INTO document_chunks(id,document_id,chunk_index,content,
                                           metadata_json,created_at)
               VALUES(?,?,?,?,?,?)""",
            (chunk_id, document_id, chunk_index, content,
             json.dumps(metadata or {}), now_iso()),
        )
        c.execute("INSERT INTO document_chunks_fts(rowid, content) VALUES "
                  "((SELECT rowid FROM document_chunks WHERE id=?), ?)",
                  (chunk_id, content))
        c.commit()
        return chunk_id

    def get_document(self, document_id: str) -> dict[str, Any] | None:
        row = self._db.conn.execute(
            "SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
        return dict(row) if row else None

    def get_chunk(self, chunk_id: str) -> dict[str, Any] | None:
        row = self._db.conn.execute(
            _CHUNK_SELECT + " WHERE ch.id=?", (chunk_id,)).fetchone()
        return _chunk_view(row) if row else None

    def chunks_by_ids(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        rows = self._db.conn.execute(
            _CHUNK_SELECT + f" WHERE ch.id IN ({placeholders})", ids).fetchall()
        return {r["id"]: _chunk_view(r) for r in rows}

    def find_by_source(self, user_id: str, project_id: str | None,
                       source_uri: str) -> dict[str, Any] | None:
        """Look up an existing document by its external source URI, so a polling
        connector can detect new/changed/unchanged documents (idempotent sync)."""
        row = self._db.conn.execute(
            """SELECT id, content FROM documents
               WHERE user_id=? AND project_id IS ? AND source_uri=?
               ORDER BY updated_at DESC LIMIT 1""",
            (user_id, project_id, source_uri),
        ).fetchone()
        return dict(row) if row else None

    def chunk_ids(self, document_id: str) -> list[str]:
        rows = self._db.conn.execute(
            "SELECT id FROM document_chunks WHERE document_id=?", (document_id,)
        ).fetchall()
        return [r["id"] for r in rows]

    def purge(self, document_id: str) -> None:
        """Delete a document and its chunks, keeping the contentless FTS index in
        sync via the special fts5 'delete' command (it needs the old content)."""
        c = self._db.conn
        rows = c.execute(
            "SELECT rowid, content FROM document_chunks WHERE document_id=?",
            (document_id,),
        ).fetchall()
        for r in rows:
            c.execute(
                "INSERT INTO document_chunks_fts(document_chunks_fts, rowid, content) "
                "VALUES('delete', ?, ?)", (r["rowid"], r["content"]))
        c.execute("DELETE FROM document_chunks WHERE document_id=?", (document_id,))
        c.execute("DELETE FROM documents WHERE id=?", (document_id,))
        c.commit()

    def search_chunks_fts(self, query: str, user_id: str, project_id: str | None,
                          limit: int = 20) -> list[tuple[dict[str, Any], float]]:
        clauses = [_CHUNK_FTS_SELECT,
                   "WHERE document_chunks_fts MATCH ? AND d.user_id=?"]
        params: list = [_fts_query(query), user_id]
        if project_id is not None:
            clauses.append("AND d.project_id=?")
            params.append(project_id)
        clauses.append("ORDER BY rank LIMIT ?")
        params.append(limit)
        rows = self._db.conn.execute(" ".join(clauses), params).fetchall()
        out = []
        for r in rows:
            score = 1.0 / (1.0 + max(r["rank"], 0.0))
            out.append((_chunk_view(r), score))
        return out


def _row_to_task(row: Any) -> HarnessTask:
    return HarnessTask(
        id=row["id"], requester=row["requester"], executor=row["executor"],
        status=row["status"], priority=row["priority"], task_slug=row["task_slug"],
        prompt=row["prompt"], cwd=row["cwd"],
        permission_profile=row["permission_profile"], review=row["review"],
        project_id=row["project_id"], user_id=row["user_id"],
        timeout_s=row["timeout_s"],
        result=json.loads(row["result_json"]) if row["result_json"] else None,
        error=row["error"], claimed_by=row["claimed_by"],
        claimed_at=row["claimed_at"], requeues=row["requeues"],
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


@database_locked_methods
class HarnessTaskRepository:
    """Contract-inbox queue rows. Claiming is a single guarded UPDATE...RETURNING
    so two concurrent runners can never take the same task."""

    def __init__(self, db: Database):
        self._db = db

    def add(self, task: HarnessTask) -> HarnessTask:
        c = self._db.conn
        c.execute(
            """INSERT INTO harness_tasks
               (id,requester,executor,status,priority,task_slug,prompt,cwd,
                permission_profile,review,project_id,user_id,timeout_s,
                result_json,error,claimed_by,claimed_at,requeues,
                created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (task.id, task.requester, task.executor, task.status, task.priority,
             task.task_slug, task.prompt, task.cwd, task.permission_profile,
             task.review, task.project_id, task.user_id, task.timeout_s,
             json.dumps(task.result) if task.result is not None else None,
             task.error, task.claimed_by, task.claimed_at, task.requeues,
             task.created_at, task.updated_at),
        )
        c.commit()
        return task

    def get(self, task_id: str) -> HarnessTask | None:
        row = self._db.conn.execute(
            "SELECT * FROM harness_tasks WHERE id=?", (task_id,)).fetchone()
        return _row_to_task(row) if row else None

    def claim(self, executor: str, worker_id: str) -> HarnessTask | None:
        """Atomically flip the best pending task to claimed. The nested SELECT
        picks the candidate and the status guard re-checks it inside the same
        statement, so concurrent claimers get distinct tasks (or None)."""
        c = self._db.conn
        row = c.execute(
            """UPDATE harness_tasks
               SET status=?, claimed_by=?, claimed_at=?, updated_at=?
               WHERE id = (SELECT id FROM harness_tasks
                           WHERE status=? AND executor=?
                           ORDER BY priority DESC, created_at ASC LIMIT 1)
                 AND status=?
               RETURNING *""",
            (C.HARNESS_STATUS_CLAIMED, worker_id, now_iso(), now_iso(),
             C.HARNESS_STATUS_PENDING, executor, C.HARNESS_STATUS_PENDING),
        ).fetchone()
        c.commit()
        return _row_to_task(row) if row else None

    def set_status(self, task_id: str, status: str, *, allowed_from: tuple[str, ...],
                   result_json: str | None = None,
                   error: str | None = None) -> HarnessTask | None:
        """Guarded transition: only applies when the current status is in
        allowed_from. Returns the updated task, or None when the guard failed."""
        placeholders = ",".join("?" * len(allowed_from))
        c = self._db.conn
        row = c.execute(
            f"""UPDATE harness_tasks
                SET status=?,
                    result_json=COALESCE(?, result_json),
                    error=COALESCE(?, error),
                    updated_at=?
                WHERE id=? AND status IN ({placeholders})
                RETURNING *""",
            (status, result_json, error, now_iso(), task_id, *allowed_from),
        ).fetchone()
        c.commit()
        return _row_to_task(row) if row else None

    def requeue_stale(self, executor: str, now: str, stale_multiplier: int,
                      max_requeues: int) -> list[str]:
        """Return stranded claimed/running tasks to pending (bounded). Uses the
        claim timestamp + the task's own timeout to define staleness."""
        c = self._db.conn
        rows = c.execute(
            """UPDATE harness_tasks
               SET status=?, claimed_by=NULL, claimed_at=NULL,
                   requeues=requeues+1, updated_at=?
               WHERE executor=? AND status IN (?,?) AND requeues<?
                 AND claimed_at IS NOT NULL
                 AND unixepoch(?) - unixepoch(claimed_at) > timeout_s*?
               RETURNING id""",
            (C.HARNESS_STATUS_PENDING, now_iso(), executor,
             C.HARNESS_STATUS_CLAIMED, C.HARNESS_STATUS_RUNNING,
             max_requeues, now, stale_multiplier),
        ).fetchall()
        c.commit()
        return [r["id"] for r in rows]

    def list(self, status: str | None = None, executor: str | None = None,
             limit: int = 100) -> list[HarnessTask]:
        sql = ["SELECT * FROM harness_tasks WHERE 1=1"]
        params: list = []
        if status is not None:
            sql.append("AND status=?")
            params.append(status)
        if executor is not None:
            sql.append("AND executor=?")
            params.append(executor)
        sql.append("ORDER BY created_at DESC LIMIT ?")
        params.append(limit)
        rows = self._db.conn.execute(" ".join(sql), params).fetchall()
        return [_row_to_task(r) for r in rows]


def _row_to_outbox(row: Any) -> OutboxEvent:
    return OutboxEvent(
        id=row["id"], event_id=row["event_id"], event_type=row["event_type"],
        aggregate_type=row["aggregate_type"], aggregate_id=row["aggregate_id"],
        subscription_id=row["subscription_id"],
        payload=json.loads(row["payload_json"]), status=row["status"],
        attempts=row["attempts"], max_attempts=row["max_attempts"],
        next_attempt_at=row["next_attempt_at"], last_error=row["last_error"],
        http_status=row["http_status"], created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@database_locked_methods
class EventOutboxRepository:
    """Durable event-delivery outbox. One row per (event × subscription); the
    delivery worker claims due rows (pending->delivering), sends them, and marks
    the outcome. All state transitions are guarded UPDATEs so a second worker (or
    a crash mid-delivery) cannot double-count attempts."""

    def __init__(self, db: Database):
        self._db = db

    def add(self, ev: OutboxEvent) -> OutboxEvent:
        c = self._db.conn
        c.execute(
            """INSERT INTO event_outbox
               (id,event_id,event_type,aggregate_type,aggregate_id,subscription_id,
                payload_json,status,attempts,max_attempts,next_attempt_at,last_error,
                http_status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ev.id, ev.event_id, ev.event_type, ev.aggregate_type, ev.aggregate_id,
             ev.subscription_id, json.dumps(ev.payload, ensure_ascii=False),
             ev.status, ev.attempts, ev.max_attempts, ev.next_attempt_at,
             ev.last_error, ev.http_status, ev.created_at, ev.updated_at),
        )
        c.commit()
        return ev

    def get(self, delivery_id: str) -> OutboxEvent | None:
        row = self._db.conn.execute(
            "SELECT * FROM event_outbox WHERE id=?", (delivery_id,)).fetchone()
        return _row_to_outbox(row) if row else None

    def by_event(self, event_id: str) -> list[OutboxEvent]:
        rows = self._db.conn.execute(
            "SELECT * FROM event_outbox WHERE event_id=? ORDER BY created_at",
            (event_id,)).fetchall()
        return [_row_to_outbox(r) for r in rows]

    def list(self, *, status: str | None = None, event_type: str | None = None,
             aggregate_id: str | None = None, limit: int = 100) -> list[OutboxEvent]:
        sql = ["SELECT * FROM event_outbox WHERE 1=1"]
        params: list = []
        if status is not None:
            sql.append("AND status=?")
            params.append(status)
        if event_type is not None:
            sql.append("AND event_type=?")
            params.append(event_type)
        if aggregate_id is not None:
            sql.append("AND aggregate_id=?")
            params.append(aggregate_id)
        sql.append("ORDER BY created_at DESC LIMIT ?")
        params.append(limit)
        rows = self._db.conn.execute(" ".join(sql), params).fetchall()
        return [_row_to_outbox(r) for r in rows]

    def claim_due(self, now: str, limit: int = 50) -> list[OutboxEvent]:
        """Atomically move up to ``limit`` due pending rows to ``delivering``
        (bumping attempts) and return them. The guarded per-row UPDATE means two
        workers never take the same delivery."""
        c = self._db.conn
        rows = c.execute(
            """SELECT id FROM event_outbox
               WHERE status=? AND next_attempt_at<=?
               ORDER BY next_attempt_at LIMIT ?""",
            (C.OUTBOX_STATUS_PENDING, now, limit),
        ).fetchall()
        claimed: list[OutboxEvent] = []
        for r in rows:
            got = c.execute(
                """UPDATE event_outbox
                   SET status=?, attempts=attempts+1, updated_at=?
                   WHERE id=? AND status=?
                   RETURNING *""",
                (C.OUTBOX_STATUS_DELIVERING, now_iso(), r["id"],
                 C.OUTBOX_STATUS_PENDING),
            ).fetchone()
            if got is not None:
                claimed.append(_row_to_outbox(got))
        c.commit()
        return claimed

    def mark_delivered(self, delivery_id: str, http_status: int | None) -> None:
        c = self._db.conn
        c.execute(
            "UPDATE event_outbox SET status=?, http_status=?, last_error=NULL, "
            "updated_at=? WHERE id=?",
            (C.OUTBOX_STATUS_DELIVERED, http_status, now_iso(), delivery_id))
        c.commit()

    def reschedule(self, delivery_id: str, next_attempt_at: str, error: str,
                   http_status: int | None) -> None:
        """Return a failed delivery to ``pending`` for a later retry."""
        c = self._db.conn
        c.execute(
            "UPDATE event_outbox SET status=?, next_attempt_at=?, last_error=?, "
            "http_status=?, updated_at=? WHERE id=?",
            (C.OUTBOX_STATUS_PENDING, next_attempt_at, error[:2000], http_status,
             now_iso(), delivery_id))
        c.commit()

    def mark_terminal(self, delivery_id: str, status: str, error: str | None,
                      http_status: int | None) -> None:
        """Final resting state: ``dead_letter`` (retries exhausted) or ``failed``
        (permanent non-retryable response)."""
        c = self._db.conn
        c.execute(
            "UPDATE event_outbox SET status=?, last_error=?, http_status=?, "
            "updated_at=? WHERE id=?",
            (status, (error or "")[:2000], http_status, now_iso(), delivery_id))
        c.commit()

    def requeue(self, delivery_id: str, now: str) -> OutboxEvent | None:
        """Manual retry: force a non-pending row back to pending, due now."""
        c = self._db.conn
        row = c.execute(
            "UPDATE event_outbox SET status=?, next_attempt_at=?, updated_at=? "
            "WHERE id=? RETURNING *",
            (C.OUTBOX_STATUS_PENDING, now, now_iso(), delivery_id)).fetchone()
        c.commit()
        return _row_to_outbox(row) if row else None

    def counts(self) -> dict[str, int]:
        rows = self._db.conn.execute(
            "SELECT status, COUNT(*) AS n FROM event_outbox GROUP BY status"
        ).fetchall()
        out = {s: 0 for s in C.OUTBOX_STATUSES}
        for r in rows:
            out[r["status"]] = r["n"]
        return out


@database_locked_methods
class ConnectorStateRepository:
    """Incremental polling cursors. Each connector stores an opaque cursor string
    per resource (default a single 'default' slot); only the connector interprets
    it. No tokens or secrets are ever stored here — just sync watermarks."""

    def __init__(self, db: Database):
        self._db = db

    def get(self, connector: str, resource: str = "default") -> str | None:
        row = self._db.conn.execute(
            "SELECT cursor FROM connector_state WHERE connector=? AND resource=?",
            (connector, resource),
        ).fetchone()
        return row["cursor"] if row else None

    def all(self) -> list[dict[str, Any]]:
        """Every stored cursor row (connector, resource, cursor, updated_at).
        Used by the read-only ops status view to surface sync watermarks."""
        rows = self._db.conn.execute(
            "SELECT connector, resource, cursor, updated_at FROM connector_state "
            "ORDER BY connector, resource").fetchall()
        return [dict(r) for r in rows]

    def set(self, connector: str, cursor: str | None,
            resource: str = "default") -> None:
        c = self._db.conn
        c.execute(
            """INSERT INTO connector_state(connector,resource,cursor,updated_at)
               VALUES(?,?,?,?)
               ON CONFLICT(connector,resource) DO UPDATE SET
                 cursor=excluded.cursor, updated_at=excluded.updated_at""",
            (connector, resource, cursor, now_iso()),
        )
        c.commit()


def _pack_vector(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack_vector(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def _fts_query(raw: str) -> str:
    # Sanitize user text into a safe FTS5 OR query. Each term is quoted so that
    # words such as AND/OR/NOT/NEAR are matched as text, not parsed as operators.
    terms = [t for t in "".join(ch if ch.isalnum() else " " for ch in raw).split() if t]
    return " OR ".join(f'"{t}"' for t in terms) if terms else '""'
