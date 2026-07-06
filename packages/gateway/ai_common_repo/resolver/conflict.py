"""Conflict resolver. Decides how a new candidate relates to existing active
memories: duplicate / update / contradiction / supersedes / independent.

Phase 1 uses cheap lexical heuristics; a model backend can be slotted in later
behind the same `classify` interface."""
from __future__ import annotations

import re

from ..core import constants as C
from ..core.models import Memory, MemoryCandidate

_NEGATION = re.compile(r"\b(not|never|no longer|don't|do not|avoid|stop)\b", re.I)


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 2}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class ConflictResolver:
    def __init__(self, dup_threshold: float = 0.85, related_threshold: float = 0.5):
        self._dup = dup_threshold
        self._related = related_threshold

    def classify(self, candidate: MemoryCandidate,
                 existing: list[Memory]) -> tuple[str, Memory | None]:
        cand_tokens = _tokens(candidate.content)
        best: Memory | None = None
        best_sim = 0.0
        for mem in existing:
            if mem.scope != candidate.scope or mem.type != candidate.type:
                continue
            sim = _jaccard(cand_tokens, _tokens(mem.content))
            if sim > best_sim:
                best_sim, best = sim, mem

        if best is None or best_sim < self._related:
            return C.RESOLUTION_INDEPENDENT, None
        if best_sim >= self._dup:
            return C.RESOLUTION_DUPLICATE, best
        # related but not identical: contradiction if negation polarity differs
        if self._polarity(candidate.content) != self._polarity(best.content):
            return C.RESOLUTION_SUPERSEDES, best
        return C.RESOLUTION_UPDATE, best

    @staticmethod
    def _polarity(text: str) -> bool:
        return _NEGATION.search(text) is None  # True = positive statement
