"""Inter-harness contract-inbox queue (HARNESS_COMMON_SPEC §4).

The gateway only stores and hands out tasks — it never executes anything.
Each harness (claudecode/openclaw/opencode/…) runs its own pull worker that
claims tasks for its executor name, re-validates cwd/permissions against its
own policy, executes, and reports status back. Distrust is mutual: runners
don't trust task payloads, and this service doesn't trust runners beyond the
worker_id they claimed with.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from ..core import constants as C
from ..core.config import HarnessConfig
from ..core.ids import now_iso, prefixed
from ..core.models import (
    HarnessClaimRequest, HarnessStatusUpdate, HarnessTask, HarnessTaskCreate,
)
from ..storage.repositories import AuditRepository, HarnessTaskRepository

if TYPE_CHECKING:  # avoid importing the events package into core queue logic
    from ..events.service import EventService

_log = logging.getLogger("ai_common_repo.harness")


class HarnessQueueService:
    def __init__(self, repo: HarnessTaskRepository, audit: AuditRepository,
                 cfg: HarnessConfig, events: "EventService | None" = None):
        self._repo = repo
        self._audit = audit
        self._cfg = cfg
        # Optional event publisher. Publishing is best-effort: a webhook/outbox
        # failure must never fail a harness status update (design §12).
        self._events = events

    def create(self, req: HarnessTaskCreate) -> HarnessTask:
        self._check_executor(req.executor)
        task = HarnessTask(
            id=prefixed("task"), requester=req.requester, executor=req.executor,
            priority=req.priority, task_slug=req.task_slug, prompt=req.prompt,
            cwd=req.cwd, permission_profile=req.permission_profile,
            review=req.review, project_id=req.project_id, user_id=req.user_id,
            timeout_s=req.timeout_s or self._cfg.default_timeout_s,
            created_at=now_iso(), updated_at=now_iso(),
        )
        self._repo.add(task)
        self._audit.log("harness_task_create", "harness_task", task.id,
                        {"executor": task.executor, "requester": task.requester,
                         "task_slug": task.task_slug})
        return task

    def claim(self, req: HarnessClaimRequest) -> HarnessTask | None:
        self._check_executor(req.executor)
        # Lazy stale recovery: a dead runner's task returns to pending right
        # before the next claim attempt, so no background sweeper is needed.
        requeued = self._repo.requeue_stale(
            req.executor, now_iso(), self._cfg.stale_multiplier,
            self._cfg.max_requeues)
        if requeued:
            self._audit.log("harness_task_requeue", "harness_task", None,
                            {"executor": req.executor, "ids": requeued})
        task = self._repo.claim(req.executor, req.worker_id)
        if task is not None:
            self._audit.log("harness_task_claim", "harness_task", task.id,
                            {"worker_id": req.worker_id})
        return task

    def update_status(self, task_id: str, req: HarnessStatusUpdate) -> HarnessTask:
        current = self._repo.get(task_id)
        if current is None:
            raise KeyError(task_id)
        allowed_from = tuple(
            s for s, targets in C.HARNESS_STATUS_TRANSITIONS.items()
            if req.status in targets)
        if current.status not in allowed_from:
            raise ValueError(
                f"invalid transition {current.status!r} -> {req.status!r}")
        # Only the claiming worker may report on a claimed task; a requester
        # cancelling a pending task carries no worker_id.
        if current.claimed_by and req.worker_id and req.worker_id != current.claimed_by:
            raise ValueError(
                f"task is claimed by {current.claimed_by!r}, not {req.worker_id!r}")
        updated = self._repo.set_status(
            task_id, req.status, allowed_from=allowed_from,
            result_json=(json.dumps(req.result, ensure_ascii=False)
                         if req.result is not None else None),
            error=req.error)
        if updated is None:  # lost a race with another transition
            raise ValueError(f"transition to {req.status!r} no longer applies")
        self._audit.log("harness_task_status", "harness_task", task_id,
                        {"status": req.status, "worker_id": req.worker_id})
        # Best-effort event publication AFTER the status transition committed. A
        # publish failure is swallowed (logged + audited) so the API still
        # succeeds — harness completion must not roll back on a notification bug.
        if self._events is not None:
            try:
                self._events.publish_status_changed(current, updated)
            except Exception as exc:  # noqa: BLE001 - notifications are best-effort
                _log.warning("event publish failed for %s: %r", task_id, exc)
                self._audit.log("event_enqueue_failed", "harness_task", task_id,
                                {"status": req.status, "error": repr(exc)})
        return updated

    def get(self, task_id: str) -> HarnessTask | None:
        return self._repo.get(task_id)

    def list(self, status: str | None = None, executor: str | None = None,
             limit: int = 100) -> list[HarnessTask]:
        if status is not None and status not in C.HARNESS_STATUSES:
            raise ValueError(f"unknown status {status!r}")
        return self._repo.list(status=status, executor=executor, limit=limit)

    def _check_executor(self, executor: str) -> None:
        if executor not in self._cfg.executors:
            raise ValueError(
                f"unknown executor {executor!r}; allowed: {self._cfg.executors}")
