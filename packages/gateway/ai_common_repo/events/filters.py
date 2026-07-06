"""Subscription matching: event-type gating + simple dict filters (design §13).

Filters are intentionally minimal — no CEL/JQ DSL. A subscription matches an
event when:

  * its ``event_types`` is empty, or contains the envelope's event_type; and
  * every key in ``filters`` matches. A filter value may be a scalar (equality)
    or a list (membership). The special key ``event_type`` matches the envelope's
    type; every other key is looked up in the event ``payload``.

Booleans compare by value so ``terminal: true`` works as expected.
"""
from __future__ import annotations

from typing import Any

from ..core.config import WebhookSubscriptionConfig


def _value_matches(expected: Any, actual: Any) -> bool:
    if isinstance(expected, list):
        return actual in expected
    return actual == expected


def subscription_matches(sub: WebhookSubscriptionConfig,
                         envelope: dict[str, Any]) -> bool:
    if not sub.enabled:
        return False
    event_type = envelope.get("event_type")
    if sub.event_types and event_type not in sub.event_types:
        return False
    payload = envelope.get("payload") or {}
    for key, expected in (sub.filters or {}).items():
        actual = event_type if key == "event_type" else payload.get(key)
        if not _value_matches(expected, actual):
            return False
    return True
