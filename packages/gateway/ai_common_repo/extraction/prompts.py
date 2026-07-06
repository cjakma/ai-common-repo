"""Extraction prompt text isolated from logic so it can be tuned/reviewed alone."""
from __future__ import annotations

from ..core import constants as C

SYSTEM = (
    "You extract durable, reusable memories from a coding-agent session. "
    "Return only long-lived facts that help future sessions."
)

INSTRUCTIONS = f"""Extract only durable memories useful in future coding-agent sessions.

Do not extract:
- temporary progress
- exact command output unless reusable
- secrets or token values
- one-time task completion
- unstable file counts or commit hashes
- PR/issue numbers or commit SHAs unless durable context

Return a JSON array (no prose):
[
  {{
    "type": "{'|'.join(C.MEMORY_TYPES)}",
    "scope": "{'|'.join(C.MEMORY_SCOPES)}",
    "content": "...",
    "confidence": 0.0,
    "reason": "..."
  }}
]
"""


def build_user_prompt(transcript: str) -> str:
    return f"{INSTRUCTIONS}\n\n--- SESSION ---\n{transcript}\n--- END ---"
