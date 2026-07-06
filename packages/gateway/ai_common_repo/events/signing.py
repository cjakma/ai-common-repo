"""HMAC-SHA256 request signing for webhook delivery.

Signature scheme (see design §7.2):

    signed input = "<timestamp>.<raw_body_bytes>"
    X-AICR-Signature: v1=hex(hmac_sha256(secret, signed_input))

A receiver recomputes the HMAC over the exact bytes it received and the
``X-AICR-Timestamp`` header, then compares in constant time. The secret is only
ever read from an env var at delivery time — it is never persisted or logged.
"""
from __future__ import annotations

import hashlib
import hmac

from ..core import constants as C


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """Return ``v1=<hexdigest>`` for the given secret/timestamp/body."""
    mac = hmac.new(secret.encode("utf-8"),
                   timestamp.encode("utf-8") + b"." + body, hashlib.sha256)
    return f"v1={mac.hexdigest()}"


def verify(secret: str, timestamp: str, body: bytes, signature: str) -> bool:
    """Constant-time check — provided for receivers/tests."""
    return hmac.compare_digest(sign(secret, timestamp, body), signature)


def signature_headers(*, event_id: str, event_type: str, delivery_id: str,
                      timestamp: str, body: bytes,
                      secret: str | None) -> dict[str, str]:
    """Standard delivery headers, adding the HMAC signature only when a secret
    is available (unsigned delivery is allowed but discouraged in production)."""
    headers = {
        C.HDR_EVENT_ID: event_id,
        C.HDR_EVENT_TYPE: event_type,
        C.HDR_DELIVERY_ID: delivery_id,
        C.HDR_TIMESTAMP: timestamp,
    }
    if secret:
        headers[C.HDR_SIGNATURE] = sign(secret, timestamp, body)
    return headers
