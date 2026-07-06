"""Event subscription / webhook layer.

The gateway publishes domain events (starting with harness task status changes)
into a durable SQLite outbox; an async delivery worker fans them out to config-
driven generic HTTP webhook subscriptions with HMAC signing, retry/backoff and
dead-lettering. ai-common-repo core stays platform-agnostic: there is no Slack /
Discord / Hermes specific code here — only signed HTTP POSTs to configured URLs.
"""
from __future__ import annotations

from .dispatcher import DeliveryResult, WebhookDispatcher, httpx_transport
from .filters import subscription_matches
from .service import EventService
from .signing import signature_headers, sign
from .worker import EventDeliveryWorker

__all__ = [
    "DeliveryResult",
    "EventDeliveryWorker",
    "EventService",
    "WebhookDispatcher",
    "httpx_transport",
    "sign",
    "signature_headers",
    "subscription_matches",
]
