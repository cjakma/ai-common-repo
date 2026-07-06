"""WebhookDispatcher — async, best-effort HTTP delivery of outbox events.

Responsibilities:
  * claim due outbox rows (pending -> delivering) and POST the envelope body;
  * add signature/delivery headers (events.signing);
  * classify the response: 2xx = delivered; 408/429/5xx/network = retry with
    exponential backoff + jitter; other 4xx = permanent failure; retries exhausted
    = dead_letter;
  * never raise into the caller — a failing webhook must not affect the API.

The HTTP call is injected as a ``transport`` callable so tests are deterministic
and never touch the network. The signing secret is read from an env var only at
delivery time via ``secret_provider`` and is never logged.

SSRF guard (design §15): unless ``allow_external_urls`` is set, delivery is
restricted to loopback/private hosts.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import random
import socket
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

from ..core import constants as C
from ..core.config import NotificationsConfig, WebhookSubscriptionConfig
from ..core.ids import now_iso, shift_iso
from ..core.models import OutboxEvent
from ..storage.repositories import EventOutboxRepository
from .signing import signature_headers

# transport(url, headers, body, timeout_s) -> DeliveryResult; may raise on
# network error / timeout (treated as retryable).
Transport = Callable[[str, dict, bytes, float], "DeliveryResult"]
SecretProvider = Callable[[str], "str | None"]


@dataclass
class DeliveryResult:
    status_code: int


# 4xx codes that are NOT permanent — the destination is asking us to back off.
_RETRYABLE_4XX = {408, 429}


def _is_retryable(status: int) -> bool:
    return status in _RETRYABLE_4XX or status >= 500


def httpx_transport(url: str, headers: dict, body: bytes,
                    timeout_s: float) -> DeliveryResult:
    """Default production transport. Imported lazily so the events package has no
    hard httpx dependency at import time."""
    import httpx

    resp = httpx.post(url, content=body, headers=headers, timeout=timeout_s)
    return DeliveryResult(status_code=resp.status_code)


def _host_is_private(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr.split("%", 1)[0])
        except ValueError:
            return False
        if not (ip.is_loopback or ip.is_private or ip.is_link_local):
            return False
    return bool(infos)


class WebhookDispatcher:
    def __init__(self, outbox: EventOutboxRepository, cfg: NotificationsConfig,
                 subscriptions: list[WebhookSubscriptionConfig] | None = None, *,
                 transport: Transport | None = None,
                 secret_provider: SecretProvider | None = None,
                 rng: random.Random | None = None,
                 logger: logging.Logger | None = None):
        self._outbox = outbox
        self._cfg = cfg
        self._subs = {s.id: s for s in (subscriptions if subscriptions is not None
                                        else cfg.subscriptions)}
        self._transport = transport or httpx_transport
        self._secret = secret_provider or os.environ.get
        self._rng = rng or random.Random()
        self._log = logger or logging.getLogger("ai_common_repo.webhook")

    # -- batch loop ----------------------------------------------------------
    def run_pending(self, *, now: str | None = None, limit: int = 50) -> int:
        """Claim and attempt all currently-due deliveries. Returns the number of
        rows attempted. Safe to call from a worker thread or directly in tests."""
        now = now or now_iso()
        rows = self._outbox.claim_due(now, limit=limit)
        for row in rows:
            self._deliver(row, now)
        return len(rows)

    # -- single delivery -----------------------------------------------------
    def _deliver(self, row: OutboxEvent, now: str) -> None:
        sub = self._subs.get(row.subscription_id)
        if sub is None or not sub.enabled:
            # Subscription removed/disabled after enqueue: nothing can deliver it.
            self._outbox.mark_terminal(
                row.id, C.OUTBOX_STATUS_FAILED,
                f"subscription {row.subscription_id!r} not available", None)
            return
        if not self._url_allowed(sub.url):
            self._fail_permanent(row, "webhook url host not allowed (SSRF guard)",
                                  None)
            return

        import json
        body = json.dumps(row.payload, ensure_ascii=False).encode("utf-8")
        headers = self._headers(sub, row, body)
        timeout = sub.timeout_s or self._cfg.default_timeout_s
        try:
            result = self._transport(sub.url, headers, body, timeout)
        except Exception as exc:  # noqa: BLE001 - network/timeout => retryable
            self._retry_or_dead(row, now, f"transport error: {exc!r}", None)
            return

        status = result.status_code
        if 200 <= status < 300:
            self._outbox.mark_delivered(row.id, status)
            self._log.info("webhook delivered dlv=%s sub=%s type=%s http=%s",
                           row.id, sub.id, row.event_type, status)
        elif _is_retryable(status):
            self._retry_or_dead(row, now, f"http {status}", status)
        else:
            self._fail_permanent(row, f"http {status}", status)

    def _headers(self, sub: WebhookSubscriptionConfig, row: OutboxEvent,
                 body: bytes) -> dict[str, str]:
        timestamp = row.payload.get("occurred_at") or now_iso()
        secret = self._secret(sub.secret_env) if sub.secret_env else None
        headers = {"Content-Type": "application/json"}
        headers.update(sub.headers or {})
        headers.update(signature_headers(
            event_id=row.event_id, event_type=row.event_type, delivery_id=row.id,
            timestamp=timestamp, body=body, secret=secret))
        return headers

    def _retry_or_dead(self, row: OutboxEvent, now: str, error: str,
                       http_status: int | None) -> None:
        if row.attempts >= row.max_attempts:
            self._outbox.mark_terminal(row.id, C.OUTBOX_STATUS_DEAD_LETTER,
                                       error, http_status)
            self._log.warning("webhook dead-letter dlv=%s sub=%s after %d attempts: %s",
                              row.id, row.subscription_id, row.attempts, error)
            return
        delay = self._backoff(row)
        self._outbox.reschedule(row.id, shift_iso(now, delay), error, http_status)

    def _fail_permanent(self, row: OutboxEvent, error: str,
                        http_status: int | None) -> None:
        self._outbox.mark_terminal(row.id, C.OUTBOX_STATUS_FAILED, error, http_status)
        self._log.warning("webhook permanent-fail dlv=%s sub=%s: %s",
                          row.id, row.subscription_id, error)

    def _backoff(self, row: OutboxEvent) -> float:
        sub = self._subs.get(row.subscription_id)
        base = (sub.backoff_base_s if sub and sub.backoff_base_s is not None
                else self._cfg.backoff_base_s)
        cap = (sub.backoff_max_s if sub and sub.backoff_max_s is not None
               else self._cfg.backoff_max_s)
        delay = min(base * (2 ** max(row.attempts - 1, 0)), cap)
        ratio = self._cfg.jitter_ratio
        if ratio:
            delay += delay * ratio * (self._rng.random() * 2 - 1)
        return max(delay, 0.0)

    def _url_allowed(self, url: str) -> bool:
        if self._cfg.allow_external_urls:
            return True
        host = urlparse(url).hostname
        return bool(host) and _host_is_private(host)
