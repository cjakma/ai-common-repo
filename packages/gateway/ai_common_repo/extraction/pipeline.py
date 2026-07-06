"""Memory-candidate extraction. Tries the configured model backend; if it is
unreachable or returns garbage, falls back to a transparent heuristic so the
gateway is useful even fully offline."""
from __future__ import annotations

import json
import re

from ..core import constants as C
from ..core.config import MemoryConfig
from ..core.models import MemoryCandidate, Message
from ..model_backends.base import ChatBackend, ChatMessage
from ..privacy.redactor import Redactor
from . import prompts

_HEURISTIC_MARKERS = (
    ("constraint", re.compile(r"\b(must|never|always|do not|don't|required)\b", re.I)),
    ("decision", re.compile(r"\b(decided|chose|use|adopt|switch to|going with)\b", re.I)),
    ("preference", re.compile(r"\b(prefer|like|favou?r|rather)\b", re.I)),
    ("workflow", re.compile(r"\b(run|build|test|deploy|command|script)\b", re.I)),
)

# Wrappers often fold a chat into a single "Human: ... Assistant: ..." prompt.
# Strip those role labels so they don't leak into stored memory content.
_ROLE_MARKER = re.compile(r"^\s*(?:human|assistant|system|user|ai)\s*[:>-]\s*", re.I)


def _strip_role_markers(text: str) -> str:
    out = text.strip()
    prev = None
    while prev != out:  # handle repeated/stacked labels
        prev = out
        out = _ROLE_MARKER.sub("", out).strip()
    return out


class ExtractionPipeline:
    def __init__(self, cfg: MemoryConfig, backend: ChatBackend | None, redactor: Redactor):
        self._cfg = cfg
        self._backend = backend
        self._redactor = redactor

    def extract(self, messages: list[Message]) -> list[MemoryCandidate]:
        transcript = self._redactor.redact(self._render(messages))
        if not transcript.strip():
            return []
        if self._backend is not None:
            try:
                return self._extract_with_model(transcript)
            except Exception:  # noqa: BLE001 - any backend failure -> fallback
                if not self._cfg.allow_heuristic_fallback:
                    raise
        return self._extract_heuristic(messages)

    # -- model path ----------------------------------------------------------
    def _extract_with_model(self, transcript: str) -> list[MemoryCandidate]:
        reply = self._backend.chat(
            [ChatMessage("system", prompts.SYSTEM),
             ChatMessage("user", prompts.build_user_prompt(transcript))],
            temperature=0.1, max_tokens=1200,
        )
        return self._parse(reply)

    def _parse(self, reply: str) -> list[MemoryCandidate]:
        match = re.search(r"\[.*\]", reply, re.S)
        if not match:
            return []
        out = []
        for item in json.loads(match.group(0)):
            t = item.get("type", "project_fact")
            s = item.get("scope", "project")
            content = _strip_role_markers(item.get("content") or "")
            if not content:
                continue
            out.append(MemoryCandidate(
                type=t if t in C.MEMORY_TYPES else "project_fact",
                scope=s if s in C.MEMORY_SCOPES else "project",
                content=self._redactor.redact(content),
                confidence=float(item.get("confidence", 0.7)),
                reason=item.get("reason"),
            ))
        return out

    # -- heuristic fallback --------------------------------------------------
    def _extract_heuristic(self, messages: list[Message]) -> list[MemoryCandidate]:
        out: list[MemoryCandidate] = []
        for msg in messages:
            if msg.role != "user":
                continue
            for sentence in re.split(r"(?<=[.!?])\s+|\n", msg.content):
                s = _strip_role_markers(sentence)
                if len(s) < 15 or self._redactor.contains_secret(s):
                    continue
                for mtype, pat in _HEURISTIC_MARKERS:
                    if pat.search(s):
                        out.append(MemoryCandidate(
                            type=mtype, scope="project",
                            content=self._redactor.redact(s),
                            confidence=0.45, reason="heuristic"))
                        break
        return out[:10]

    @staticmethod
    def _render(messages: list[Message]) -> str:
        return "\n".join(f"{m.role}: {m.content}" for m in messages)
