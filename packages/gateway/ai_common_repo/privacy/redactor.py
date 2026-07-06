"""Secret redaction. Patterns are config-driven so new token shapes need no code
change. Used before persisting raw text and before any external model call."""
from __future__ import annotations

import re

from ..core.config import PrivacyConfig

_PLACEHOLDER = "[REDACTED]"


class Redactor:
    def __init__(self, cfg: PrivacyConfig):
        self._enabled = cfg.redact_secrets
        self._patterns = [re.compile(p) for p in cfg.secret_patterns]

    def redact(self, text: str | None) -> str:
        if not text or not self._enabled:
            return text or ""
        out = text
        for pat in self._patterns:
            out = pat.sub(_PLACEHOLDER, out)
        return out

    def contains_secret(self, text: str | None) -> bool:
        if not text:
            return False
        return any(p.search(text) for p in self._patterns)
