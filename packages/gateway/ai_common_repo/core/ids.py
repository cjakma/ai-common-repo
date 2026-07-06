"""Sortable, collision-resistant ids without external deps.

ULID-like: 48-bit ms timestamp + 80 bits randomness, Crockford base32. Lexical
order matches creation order, which keeps FTS/scan locality reasonable.
"""
from __future__ import annotations

import os
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32 (no I L O U)


def _b32(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        value, rem = divmod(value, 32)
        chars.append(_ALPHABET[rem])
    return "".join(reversed(chars))


def ulid() -> str:
    ts = int(time.time() * 1000) & ((1 << 48) - 1)
    rand = int.from_bytes(os.urandom(10), "big")
    return _b32(ts, 10) + _b32(rand, 16)


def prefixed(prefix: str) -> str:
    return f"{prefix}_{ulid()}"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def shift_iso(iso_ts: str, seconds: float) -> str:
    """Add ``seconds`` to an ISO ``YYYY-MM-DDTHH:MM:SSZ`` timestamp (>=0 only)."""
    if seconds <= 0:
        return iso_ts
    import calendar
    epoch = calendar.timegm(time.strptime(iso_ts, "%Y-%m-%dT%H:%M:%SZ")) + seconds
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))
