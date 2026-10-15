"""MemoryService — the application core. Orchestrates storage, extraction,
conflict resolution, profiles, search and context. The HTTP/CLI/MCP layers are
thin shells over this single object."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .core import constants as C
from .core.config import AppConfig
from .core.context import ContextBuilder
from .core.ids import now_iso, prefixed
from .core.models import (
    DocumentIngestRequest, Memory, MemoryCandidate, Message, Profile,
    RecallRequest, RecallResponse, SearchRequest, SessionIngestRequest,
    StoreMemoryRequest,
)
from .connectors import ConnectorDoc, build_connectors
from .events.dispatcher import WebhookDispatcher
from .events.service import EventService
from .extraction.chunker import chunk_text
from .extraction.pipeline import ExtractionPipeline
from .harness import HarnessQueueService
from .model_backends.registry import BackendRegistry
from .observability import (
    MetricsCollector, build_connector_status, build_health, build_job_status,
    build_metrics,
)
from .observability.models import (
    ComponentHealth, ConnectorStatusResponse, HealthResponse, JobStatusResponse,
    MetricsResponse,
)
from .privacy.redactor import Redactor
from .profiles.builder import ProfileBuilder
from .resolver.conflict import ConflictResolver
from .resolver.dedup import cluster_by_cosine
from .search.expansion import build_expander
from .search.rerank import build_reranker
from .search.service import SearchService
from .search.vector import build_vector_index
from .storage.database import Database
from .storage.repositories import (
    AuditRepository, ConnectorStateRepository, DocumentRepository,
    EmbeddingRepository, EventOutboxRepository, HarnessTaskRepository,
    MemoryRepository, ProfileRepository, SessionRepository,
)
from .workers.queue import SqliteJobQueue

_log = logging.getLogger("ai_common_repo.service")


class MemoryService:
    """Composition root for the gateway domain."""

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self._db = Database(cfg.storage.db_file)
        self.memories = MemoryRepository(self._db)
        self.profiles_repo = ProfileRepository(self._db)
        self.sessions = SessionRepository(self._db)
        self.documents = DocumentRepository(self._db)
        self.embeddings = EmbeddingRepository(self._db)
        self.connector_state = ConnectorStateRepository(self._db)
        self.audit = AuditRepository(self._db)
        self.jobs = SqliteJobQueue(
            self._db, default_max_attempts=cfg.workers.max_attempts,
            backoff_base_s=cfg.workers.backoff_base_s,
            backoff_max_s=cfg.workers.backoff_max_s)

        self.backends = BackendRegistry(cfg.models)
        self.redactor = Redactor(cfg.privacy)
        self.extractor = ExtractionPipeline(
            cfg.memory, self.backends.for_task(C.TASK_EXTRACTION), self.redactor)
        self.resolver = ConflictResolver()
        self.profile_builder = ProfileBuilder(cfg.memory)
        self.embedder = self.backends.embedder(cfg.embedding.dim)
        self.vector_index = build_vector_index(
            cfg.storage, self.embeddings, dim=cfg.embedding.dim)
        self.reranker = build_reranker(
            cfg.search.rerank, self.backends.for_task(C.TASK_RERANKING))
        self.expander = build_expander(cfg.search.query_expansion)
        self.search_svc = SearchService(
            self.memories, self.documents, self.vector_index, self.embedder,
            rrf_k=cfg.embedding.rrf_k,
            include_documents=cfg.memory.include_documents,
            reranker=self.reranker, expander=self.expander)
        self.context_builder = ContextBuilder(cfg.memory)
        self.connectors = build_connectors(cfg.connectors)
        # Event subscription / webhook layer. The outbox tables always exist; the
        # EventService only publishes when notifications are enabled, and delivery
        # is done by the (optional) EventDeliveryWorker via this dispatcher.
        self.event_outbox = EventOutboxRepository(self._db)
        self.events = EventService(self.event_outbox, cfg.notifications)
        self.event_dispatcher = WebhookDispatcher(self.event_outbox, cfg.notifications)
        self.harness = HarnessQueueService(
            HarnessTaskRepository(self._db), self.audit, cfg.harness,
            events=self.events)
        self.metrics = MetricsCollector()

    # -- identity helpers ----------------------------------------------------
    def _user(self, user_id: str | None) -> str:
        return user_id or self.cfg.identity.default_user_id

    # -- store ---------------------------------------------------------------
    def store_memory(self, req: StoreMemoryRequest) -> Memory | None:
        cand = MemoryCandidate(type=req.type, scope=req.scope, content=req.content,
                               confidence=req.confidence)
        mem = self._persist_candidate(cand, self._user(req.user_id), req.project_id,
                                      source_session_id=None, tags=req.tags)
        if mem is not None:
            self._rebuild_profile(mem.user_id, mem.project_id)
        return mem

    def forget(self, memory_id: str) -> None:
        self.memories.set_status(memory_id, C.MEMORY_STATUS_FORGOTTEN)
        self.vector_index.delete(C.OBJECT_TYPE_MEMORY, memory_id)
        self.audit.log("forget", "memory", memory_id)

    # -- ingest --------------------------------------------------------------
    def ingest_session(self, req: SessionIngestRequest) -> dict:
        user_id = self._user(req.user_id)
        session_id = req.session_id or prefixed("sess")
        git = req.git.model_dump() if req.git else {}
        raw = req.model_dump(by_alias=True)
        if not self.cfg.privacy.store_raw_outputs:
            raw["messages"] = [m for m in raw["messages"] if m["role"] != "assistant"]
        self.sessions.add(session_id=session_id, user_id=user_id,
                          project_id=req.project_id, tool=req.tool,
                          wrapper=req.wrapper, cwd=req.cwd, git=git,
                          exit_code=req.artifacts.get("exit_code"),
                          raw_json=json.dumps(raw, ensure_ascii=False))

        created = 0
        queued: list[str] = []
        if self.cfg.memory.auto_extract:
            if self.cfg.memory.async_jobs:
                # Defer extraction + profile rebuild to the worker. The raw
                # session is already persisted, so the job reloads it by id.
                self.jobs.enqueue(C.JOB_EXTRACT_SESSION, {"session_id": session_id},
                                  idempotency_key=f"extract:{session_id}")
                queued = [C.JOB_EXTRACT_SESSION, C.JOB_REBUILD_PROFILE]
            else:
                created = self._extract_and_store(session_id, user_id,
                                                  req.project_id, req.messages)
                self._rebuild_profile(user_id, req.project_id)
        self.audit.log("ingest_session", "session", session_id,
                       {"memories_created": created, "queued": queued})
        return {"accepted": True, "session_id": session_id,
                "memories_created": created, "queued_jobs": queued}

    # -- recall --------------------------------------------------------------
    def recall(self, req: RecallRequest) -> RecallResponse:
        with self.metrics.timer("recall"):
            return self._recall(req)

    def _recall(self, req: RecallRequest) -> RecallResponse:
        user_id = self._user(req.user_id)
        limit = req.limit or self.cfg.memory.max_context_items
        # Hybrid fuses lexical + vector across memories and document chunks.
        hits = self.search_svc.search(SearchRequest(
            q=req.query, user_id=user_id, project_id=req.project_id, limit=limit,
            search_mode=C.SEARCH_MODE_HYBRID), self.cfg.identity.default_user_id)

        mem_ids = [h.id for h in hits if h.object_type == C.OBJECT_TYPE_MEMORY]
        mem_map = self.memories.by_ids(mem_ids)
        memories = [mem_map[i] for i in mem_ids if i in mem_map]
        if not memories and req.include_recent:
            memories = self.memories.active_for_scope(user_id, req.project_id, limit)
        doc_snippets = [h.content for h in hits
                        if h.object_type == C.OBJECT_TYPE_CHUNK]

        profile = (self.profiles_repo.get(user_id, req.project_id)
                   if req.include_profile else Profile(user_id=user_id))
        block = self.context_builder.build(profile, memories, documents=doc_snippets)
        return RecallResponse(
            profile={"stable": profile.stable, "dynamic": profile.dynamic},
            memories=[h.model_dump(exclude_none=True) for h in hits],
            context_block=block,
        )

    # -- search --------------------------------------------------------------
    def search(self, req: SearchRequest) -> list[dict]:
        with self.metrics.timer("search"):
            results = self.search_svc.search(req, self.cfg.identity.default_user_id)
        return [h.model_dump(exclude_none=True) for h in results]

    # -- observability (read-only ops surfaces) ------------------------------
    def health(self, worker_running: bool | None = None) -> HealthResponse:
        return build_health(
            db_ok=self._db.ping(), queue_ok=self._queue_ok(),
            worker_enabled=self.cfg.workers.enabled,
            worker_running=bool(worker_running),
            extra=[self._vector_store_health(), self._embedding_health()])

    def _vector_store_health(self) -> ComponentHealth:
        ok, info = self.vector_index.status()
        return ComponentHealth(name="vector_store", status="ok" if ok else "down",
                               detail=info.pop("detail", None), info=info)

    def _embedding_health(self) -> ComponentHealth:
        info = {"model": getattr(self.embedder, "model", "external"),
                "backend": getattr(self.embedder, "name", type(self.embedder).__name__)}
        probe = getattr(self.embedder, "health", None)
        if probe is None:  # offline hash embedder: always available
            return ComponentHealth(name="embedding", status="ok", info=info)
        h = probe()
        return ComponentHealth(name="embedding", status="ok" if h.status == "ok" else "down",
                               detail=h.detail, info={**info, "base_url": h.base_url})

    def metrics_report(self) -> MetricsResponse:
        return build_metrics(
            uptime_s=self.metrics.uptime_s(), counts=self.jobs.counts(),
            embeddings=self.embeddings.count(),
            operations=self.metrics.snapshot())

    def job_status(self) -> JobStatusResponse:
        return build_job_status(
            counts=self.jobs.counts(),
            failures=self.jobs.recent_failed(
                self.cfg.observability.recent_failures_limit),
            redactor=self.redactor)

    def connector_status(self) -> ConnectorStatusResponse:
        enabled = {C.CONNECTOR_GITHUB: self.cfg.connectors.github.enabled,
                   C.CONNECTOR_NOTION: self.cfg.connectors.notion.enabled}
        return build_connector_status(
            enabled=enabled, states=self.connector_state.all(),
            redactor=self.redactor)

    def _queue_ok(self) -> bool:
        try:
            self.jobs.counts()
            return True
        except Exception:  # noqa: BLE001 - readiness probe must not raise
            return False

    # -- documents (RAG ingest) ----------------------------------------------
    def ingest_document(self, req: DocumentIngestRequest) -> dict:
        user_id = self._user(req.user_id)
        content = self.redactor.redact(self._load_document_content(req))
        dcfg = self.cfg.documents
        if len(content) > dcfg.max_content_chars:
            raise ValueError(
                f"document too large ({len(content)} > {dcfg.max_content_chars} chars)")
        if not content.strip():
            raise ValueError("document content is empty")
        chunks = chunk_text(content, max_chars=dcfg.chunk_max_chars,
                            overlap=dcfg.chunk_overlap_chars,
                            source_type=req.source_type)
        if len(chunks) > dcfg.max_chunks:
            raise ValueError(
                f"document produced too many chunks ({len(chunks)} > {dcfg.max_chunks})")
        doc_id = self.documents.add_document(
            user_id=user_id, project_id=req.project_id, source_type=req.source_type,
            source_uri=req.source_uri, title=req.title, content=content,
            metadata=req.metadata)
        for index, chunk in enumerate(chunks):
            chunk_id = self.documents.add_chunk(
                document_id=doc_id, chunk_index=index, content=chunk)
            self._embed_object(C.OBJECT_TYPE_CHUNK, chunk_id, chunk)
        self.audit.log("ingest_document", "document", doc_id, {"chunks": len(chunks)})
        return {"document_id": doc_id, "chunks": len(chunks)}

    def get_profile(self, user_id: str | None, project_id: str | None) -> Profile:
        return self.profiles_repo.get(self._user(user_id), project_id)

    def backend_health(self) -> list[dict]:
        return [h.__dict__ for h in self.backends.health_all()]

    # -- events / webhook subscriptions --------------------------------------
    def list_events(self, *, status: str | None = None,
                    event_type: str | None = None, aggregate_id: str | None = None,
                    limit: int = 100) -> list[dict]:
        return [e.model_dump() for e in self.event_outbox.list(
            status=status, event_type=event_type, aggregate_id=aggregate_id,
            limit=limit)]

    def get_event(self, event_id: str) -> list[dict]:
        """All (per-subscription) deliveries for one event_id."""
        return [e.model_dump() for e in self.event_outbox.by_event(event_id)]

    def event_outbox_counts(self) -> dict[str, int]:
        return self.event_outbox.counts()

    def list_webhook_subscriptions(self) -> list[dict]:
        """Redacted subscription views. The signing secret is never returned —
        only the env var name and whether it resolves are exposed."""
        out = []
        for sub in self.cfg.notifications.subscriptions:
            out.append({
                "id": sub.id, "name": sub.name, "enabled": sub.enabled,
                "type": sub.type, "url": sub.url,
                "event_types": sub.event_types, "filters": sub.filters,
                "secret_env": sub.secret_env,
                "signed": bool(sub.secret_env and os.environ.get(sub.secret_env)),
            })
        return out

    def test_webhook_subscription(self, subscription_id: str) -> dict | None:
        """Enqueue a synthetic ``webhook.test`` event for one subscription and
        attempt delivery immediately. Returns the resulting delivery row, or None
        if the subscription id is unknown."""
        sub = next((s for s in self.cfg.notifications.subscriptions
                    if s.id == subscription_id), None)
        if sub is None:
            return None
        row = self.events.enqueue_for_subscription(
            sub, C.EVENT_WEBHOOK_TEST, "webhook_subscription", sub.id,
            {"subscription_id": sub.id, "message": "ai-common-repo webhook test",
             "sent_at": now_iso()})
        self.event_dispatcher.run_pending()
        delivered = self.event_outbox.get(row.id)
        return delivered.model_dump() if delivered else row.model_dump()

    # -- job handlers --------------------------------------------------------
    @property
    def handlers(self) -> dict[str, object]:
        """Registry the worker uses: handler name -> bound callable(payload)."""
        return {
            C.JOB_EXTRACT_SESSION: self._job_extract_session,
            C.JOB_REBUILD_PROFILE: self._job_rebuild_profile,
            C.JOB_FORGET_EXPIRED: self._job_forget_expired,
            C.JOB_EMBED: self._job_embed,
            C.JOB_CONNECTOR_POLL: self._job_poll_connector,
            C.JOB_DEDUP_MEMORIES: self._job_dedup_memories,
        }

    def _job_extract_session(self, payload: dict) -> None:
        rec = self.sessions.get(payload["session_id"])
        if rec is None:
            return
        raw = json.loads(rec["raw_json"])
        messages = [Message.model_validate(m) for m in raw.get("messages", [])]
        self._extract_and_store(rec["id"], rec["user_id"], rec["project_id"], messages)
        # Chain the profile rebuild as its own job so a heavy rebuild cannot
        # block (or fail) the extraction that produced it.
        self.jobs.enqueue(C.JOB_REBUILD_PROFILE,
                          {"user_id": rec["user_id"], "project_id": rec["project_id"]})

    def _job_rebuild_profile(self, payload: dict) -> None:
        self._rebuild_profile(payload["user_id"], payload.get("project_id"))

    def _job_forget_expired(self, payload: dict | None = None) -> None:
        if not self.cfg.memory.forget_enabled:
            return
        ids = self.memories.expire_due(now_iso(), self.cfg.memory.forget_confidence_floor)
        if ids:
            for mem_id in ids:
                self.vector_index.delete(C.OBJECT_TYPE_MEMORY, mem_id)
            self.audit.log("forget_expired", "memory", None,
                           {"count": len(ids), "ids": ids[:50]})

    def _job_embed(self, payload: dict) -> None:
        otype, oid = payload["object_type"], payload["object_id"]
        text = self._object_text(otype, oid)
        if text:
            self._embed_now(otype, oid, text)

    def _job_dedup_memories(self, payload: dict | None = None) -> None:
        """Consolidate near-duplicate active memories. Within each visibility
        group (user/project/scope) memories are re-embedded and clustered by
        cosine; the highest-confidence / most-recent member of a cluster is kept
        active while the rest are superseded (and their vectors dropped)."""
        if not self.cfg.memory.dedup_enabled:
            return
        threshold = self.cfg.memory.dedup_similarity
        groups: dict[tuple, list[Memory]] = {}
        for mem in self.memories.active_all():
            groups.setdefault((mem.user_id, mem.project_id, mem.scope), []).append(mem)

        merged = 0
        for group in groups.values():
            if len(group) < 2:
                continue
            vectors = self.embedder.embed([m.content for m in group])
            for cluster in cluster_by_cosine(vectors, threshold):
                if len(cluster) < 2:
                    continue
                members = [group[i] for i in cluster]
                winner = max(members, key=lambda m: (m.confidence, m.updated_at or ""))
                for mem in members:
                    if mem.id == winner.id:
                        continue
                    self.memories.set_status(mem.id, C.MEMORY_STATUS_SUPERSEDED)
                    self.vector_index.delete(C.OBJECT_TYPE_MEMORY, mem.id)
                    merged += 1
        if merged:
            self.audit.log("dedup_memories", "memory", None, {"merged": merged})

    def _job_poll_connector(self, payload: dict) -> None:
        name = payload["connector"]
        connector = self.connectors.get(name)
        if connector is None:
            return
        sub = getattr(self.cfg.connectors, name)
        user_id = self._user(sub.user_id)
        result = connector.sync(self.connector_state.get(name))
        ingested = 0
        for doc in result.documents:
            if self._ingest_connector_doc(doc, user_id, sub.project_id):
                ingested += 1
        self.connector_state.set(name, result.next_cursor)
        self.audit.log("connector_poll", "connector", name,
                       {"fetched": len(result.documents), "ingested": ingested})

    # -- internals -----------------------------------------------------------
    def _extract_and_store(self, session_id: str, user_id: str,
                           project_id: str | None, messages: list[Message]) -> int:
        created = 0
        for cand in self.extractor.extract(messages):
            if self._persist_candidate(cand, user_id, project_id, session_id):
                created += 1
        return created

    def _persist_candidate(self, cand: MemoryCandidate, user_id: str,
                           project_id: str | None, source_session_id: str | None,
                           tags: list[str] | None = None) -> Memory | None:
        if self.cfg.memory.enable_conflict_resolution:
            existing = self.memories.active_for_scope(user_id, project_id)
            verdict, target = self.resolver.classify(cand, existing)
            if verdict == C.RESOLUTION_DUPLICATE:
                self.audit.log("dedupe", "memory", target.id if target else None)
                return None
            if verdict == C.RESOLUTION_SUPERSEDES and target:
                self.memories.set_status(target.id, C.MEMORY_STATUS_SUPERSEDED)

        mem = Memory(
            id=prefixed("mem"), user_id=user_id, project_id=project_id,
            scope=cand.scope, type=cand.type, content=cand.content,
            source_session_id=source_session_id, confidence=cand.confidence,
            tags=tags or [], valid_from=now_iso(),
            created_at=now_iso(), updated_at=now_iso(),
        )
        self.memories.add(mem)
        self._embed_object(C.OBJECT_TYPE_MEMORY, mem.id, mem.content)
        self.audit.log("memory_write", "memory", mem.id, {"type": mem.type})
        return mem

    def _rebuild_profile(self, user_id: str, project_id: str | None) -> None:
        active = self.memories.active_for_scope(user_id, project_id)
        profile = self.profile_builder.build(user_id, project_id, active)
        self.profiles_repo.upsert(profile)

    # -- embedding helpers ---------------------------------------------------
    def _embed_object(self, object_type: str, object_id: str, text: str) -> None:
        """Index an object's vector. Deferred to the worker when async_jobs is on,
        else embedded inline so vector/hybrid search works without a worker."""
        if self.cfg.memory.async_jobs:
            self.jobs.enqueue(
                C.JOB_EMBED, {"object_type": object_type, "object_id": object_id},
                idempotency_key=f"embed:{object_type}:{object_id}")
        else:
            self._embed_now(object_type, object_id, text)

    def _embed_now(self, object_type: str, object_id: str, text: str) -> None:
        # Fail soft: the object is already stored; a missing vector only drops
        # it from vector search until scripts/reembed.py fills it in.
        try:
            vector = self.embedder.embed([text])[0]
            model = getattr(self.embedder, "model", "external")
            self.vector_index.index(object_type, object_id, vector, model=model)
        except Exception as exc:  # noqa: BLE001 — any backend failure degrades
            _log.warning("embedding skipped for %s %s (filled later): %s",
                         object_type, object_id, exc)

    def _object_text(self, object_type: str, object_id: str) -> str | None:
        if object_type == C.OBJECT_TYPE_MEMORY:
            mem = self.memories.by_ids([object_id]).get(object_id)
            return mem.content if mem else None
        if object_type == C.OBJECT_TYPE_CHUNK:
            chunk = self.documents.get_chunk(object_id)
            return chunk["content"] if chunk else None
        return None

    # -- connector ingest ----------------------------------------------------
    def _ingest_connector_doc(self, doc: ConnectorDoc, user_id: str,
                              project_id: str | None) -> bool:
        """Idempotent ingest of one polled document. Skips when an identically
        sourced document is unchanged; replaces it (chunks + vectors) when the
        content has changed. Redaction is applied before any persistence."""
        redacted = self.redactor.redact(doc.content)
        if not redacted.strip():
            return False
        existing = self.documents.find_by_source(user_id, project_id, doc.source_uri)
        if existing is not None:
            if existing["content"] == redacted:
                return False  # unchanged since the last poll
            self._purge_document(existing["id"])
        try:
            self.ingest_document(DocumentIngestRequest(
                user_id=user_id, project_id=project_id, source_type=doc.source_type,
                source_uri=doc.source_uri, title=doc.title, content=doc.content,
                metadata=doc.metadata))
        except ValueError as exc:
            # One oversized/empty doc must not poison the whole poll job.
            self.audit.log("connector_doc_skipped", "document", doc.source_uri,
                           {"reason": str(exc)})
            return False
        return True

    def _purge_document(self, document_id: str) -> None:
        for chunk_id in self.documents.chunk_ids(document_id):
            self.vector_index.delete(C.OBJECT_TYPE_CHUNK, chunk_id)
        self.documents.purge(document_id)

    def _load_document_content(self, req: DocumentIngestRequest) -> str:
        if req.content is not None:
            return req.content
        if not req.path:
            raise ValueError("document ingest requires 'content' or 'path'")
        return self._read_allowed_path(req.path)

    def _read_allowed_path(self, raw_path: str) -> str:
        roots = [Path(r).expanduser().resolve()
                 for r in self.cfg.documents.allowed_roots]
        if not roots:
            raise ValueError("path ingest is disabled (documents.allowed_roots is empty)")
        target = Path(raw_path).expanduser().resolve()
        if not any(_is_within(target, root) for root in roots):
            raise ValueError("path is outside the allowed document roots")
        if not target.is_file():
            raise ValueError("path is not a readable file")
        return target.read_text(encoding="utf-8", errors="replace")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
