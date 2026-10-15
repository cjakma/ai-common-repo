"""Query expansion — deterministic, dependency-free recall booster.

Code-heavy queries lose recall because identifiers fuse several words into one
token: ``getUserName`` never matches a doc that wrote ``get user name`` or
``user_name``. The expander normalizes the query and adds:

* identifier decomposition — camelCase / snake_case / kebab-case / dotted names
  split into their parts (``getUserName`` -> ``get user name``);
* a small, conservative synonym map for common dev/code abbreviations.

The result is appended to the original query (never replaces it), so both the
FTS term set and the embedding text gain the extra surface without losing the
original phrasing. An optional model-based expansion seam exists but is off by
default, keeping expansion fully offline and deterministic.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core.config import QueryExpansionConfig

# Split on case boundaries, digit boundaries, and non-alphanumeric separators.
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_SEP = re.compile(r"[^A-Za-z0-9]+")

# Conservative dev/code synonyms. Kept deliberately small and unambiguous so
# expansion never injects misleading terms. Bidirectional pairs are expanded
# both ways at build time.
_SYNONYM_PAIRS = [
    ("db", "database"),
    ("config", "configuration"),
    ("auth", "authentication"),
    ("k8s", "kubernetes"),
    ("repo", "repository"),
    ("env", "environment"),
    ("dir", "directory"),
    ("doc", "document"),
    ("fn", "function"),
    ("impl", "implementation"),
]


def _build_synonyms() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for a, b in _SYNONYM_PAIRS:
        out.setdefault(a, []).append(b)
        out.setdefault(b, []).append(a)
    return out


_SYNONYMS = _build_synonyms()


class QueryExpander:
    """Deterministic expander. ``expand`` returns the original query enriched with
    decomposed identifier parts and conservative synonyms (deduped, order-stable).
    """

    def __init__(self, *, enabled: bool = True, max_terms: int = 32,
                 use_synonyms: bool = True):
        self._enabled = enabled
        self._max_terms = max(1, max_terms)
        self._use_synonyms = use_synonyms

    def expand(self, query: str) -> str:
        if not self._enabled or not query.strip():
            return query
        added: list[str] = []
        seen = {t.lower() for t in query.split()}
        for token in _SEP.split(query):
            if not token:
                continue
            # Separator-split tokens (snake/kebab/dotted) plus camelCase parts.
            parts = _split_identifier(token) or [token]
            for part in parts:
                low = part.lower()
                if low and low not in seen:
                    seen.add(low)
                    added.append(part)
            # Synonyms for the original token *and* each decomposed part.
            if self._use_synonyms:
                for base in (token, *parts):
                    for syn in _SYNONYMS.get(base.lower(), []):
                        if syn not in seen:
                            seen.add(syn)
                            added.append(syn)
        if not added:
            return query
        extra = " ".join(added[:self._max_terms])
        return f"{query} {extra}"


def _split_identifier(token: str) -> list[str]:
    """Decompose an identifier into its word parts. Returns the parts only when
    the token actually splits (so plain words add nothing)."""
    pieces = [p for p in _CAMEL.sub(" ", token).split() if p]
    if len(pieces) <= 1:
        return []
    return pieces


def build_expander(cfg: "QueryExpansionConfig") -> QueryExpander | None:
    """Factory. Returns ``None`` when expansion is disabled."""
    if not cfg.enabled:
        return None
    return QueryExpander(enabled=True, max_terms=cfg.max_terms,
                         use_synonyms=cfg.use_synonyms)
