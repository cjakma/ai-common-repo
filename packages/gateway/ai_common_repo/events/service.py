"""EventService — publishes domain events into the durable outbox.

Called from inside the request path *after* a state change has committed (e.g.
HarnessQueueService.update_status). Publishing is fan-out by subscription: one
outbox row per matching subscription, each with its own delivery id and retry
state. Delivery itself is done asynchronously by WebhookDispatcher, so this stays
fast and never performs network I/O.

Design constraints honoured here:
  * ai-common-repo core is not coupled to Slack/Hermes — only generic events.
  * The default webhook payload is compact (no giant ``result`` blob); full data
    stays retrievable via GET /v1/harness/tasks/{id}.
"""
from __future__ import annotations

import logging
from typing import Any

from ..core import constants as C
from ..core.config import NotificationsConfig, WebhookSubscriptionConfig
from ..core.ids import now_iso, prefixed
from ..core.models import EventEnvelope, HarnessTask, OutboxEvent
from ..storage.repositories import EventOutboxRepository
from .filters import subscription_matches


class EventService:
    def __init__(self, outbox: EventOutboxRepository,
                 cfg: NotificationsConfig,
                 subscriptions: list[WebhookSubscriptionConfig] | None = None,
                 *, logger: logging.Logger | None = None):
        self._outbox = outbox
        self._cfg = cfg
        self._subs = list(subscriptions if subscriptions is not None
                          else cfg.subscriptions)
        self._log = logger or logging.getLogger("ai_common_repo.events")

    # -- generic publish -----------------------------------------------------
    def publish(self, event_type: str, aggregate_type: str, aggregate_id: str,
                payload: dict[str, Any]) -> str | None:
        """Build an envelope and enqueue one outbox row per matching subscription.
        Returns the event_id (even when no subscription matched, so callers can
        correlate), or None when publishing is disabled."""
        if not (self._cfg.enabled and self._cfg.publish_enabled):
            return None
        event_id = prefixed("evt")
        envelope = EventEnvelope(
            event_id=event_id, event_type=event_type, occurred_at=now_iso(),
            aggregate={"type": aggregate_type, "id": aggregate_id},
            payload=payload,
        )
        env_dict = envelope.model_dump()
        for sub in self._subs:
            if subscription_matches(sub, env_dict):
                self._enqueue(sub, envelope, aggregate_type, aggregate_id)
        return event_id

    def enqueue_for_subscription(self, sub: WebhookSubscriptionConfig,
                                 event_type: str, aggregate_type: str,
                                 aggregate_id: str,
                                 payload: dict[str, Any]) -> OutboxEvent:
        """Enqueue a delivery to one specific subscription, bypassing filters.
        Used by the ``/test`` endpoint so an operator can probe a destination."""
        envelope = EventEnvelope(
            event_id=prefixed("evt"), event_type=event_type, occurred_at=now_iso(),
            aggregate={"type": aggregate_type, "id": aggregate_id}, payload=payload,
        )
        return self._enqueue(sub, envelope, aggregate_type, aggregate_id)

    def _enqueue(self, sub: WebhookSubscriptionConfig, envelope: EventEnvelope,
                 aggregate_type: str, aggregate_id: str) -> OutboxEvent:
        row = OutboxEvent(
            id=prefixed("dlv"), event_id=envelope.event_id,
            event_type=envelope.event_type, aggregate_type=aggregate_type,
            aggregate_id=aggregate_id, subscription_id=sub.id,
            payload=envelope.model_dump(),
            status=C.OUTBOX_STATUS_PENDING, attempts=0,
            max_attempts=(sub.max_attempts or self._cfg.default_max_attempts),
            next_attempt_at=now_iso(), created_at=now_iso(), updated_at=now_iso(),
        )
        return self._outbox.add(row)

    # -- harness task status change -----------------------------------------
    def publish_status_changed(self, old: HarnessTask,
                               new: HarnessTask) -> str | None:
        payload = harness_status_payload(old, new)
        return self.publish(C.EVENT_HARNESS_TASK_STATUS_CHANGED,
                            C.EVENT_AGGREGATE_HARNESS_TASK, new.id, payload)


def _first(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return None


def harness_status_payload(old: HarnessTask, new: HarnessTask) -> dict[str, Any]:
    """Compact, notification-friendly payload (design §5.2). Pulls a few well-
    known fields out of the task ``result`` blob but never embeds the whole
    thing — consumers fetch full data via GET /v1/harness/tasks/{id}."""
    result = new.result or {}
    tests = result.get("tests") if isinstance(result.get("tests"), dict) else {}
    payload: dict[str, Any] = {
        "task_id": new.id,
        "task_slug": new.task_slug,
        "requester": new.requester,
        "executor": new.executor,
        "project_id": new.project_id,
        "user_id": new.user_id,
        "old_status": old.status,
        "new_status": new.status,
        "terminal": new.status in C.HARNESS_TERMINAL_STATUSES,
        "claimed_by": new.claimed_by,
        "claimed_at": new.claimed_at,
        "created_at": new.created_at,
        "updated_at": new.updated_at,
        "result_summary": _first(result, "summary", "result_summary"),
        "response_md": result.get("response_md"),
        "log": result.get("log"),
        "error": new.error,
    }
    exit_code = _first(tests, "exit_code") if tests else result.get("exit_code")
    if exit_code is not None:
        payload["tests"] = {"exit_code": exit_code}
        if isinstance(tests, dict) and tests.get("tail") is not None:
            payload["tests"]["tail"] = tests["tail"]
    return payload
