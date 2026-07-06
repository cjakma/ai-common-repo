"""Text/markdown/code chunking — dependency-free.

Splits a document into retrieval-sized chunks while trying to keep semantic
units intact: paragraphs for prose/markdown, and function/class boundaries for
code (regex-level, no parser). Chunks carry a small char overlap so a fact that
straddles a boundary is still recoverable from at least one chunk. Multi-modal
input is out of scope (PLAN): callers pass already-textual content."""
from __future__ import annotations

import re

# Lines that typically begin a new top-level code unit across common languages.
_CODE_BOUNDARY = re.compile(
    r"(?m)^(?=\s*(?:async\s+def\s|def\s|class\s|func\s|function\s|"
    r"public\s|private\s|protected\s|export\s|module\s|package\s|impl\s|fn\s))")
_CODE_HINT = re.compile(
    r"(?m)^\s*(?:def\s|class\s|function\s|func\s|import\s|#include|package\s|"
    r"public\s+(?:class|static)|const\s|let\s|var\s)")

# Source types that are unambiguously code (skip the heuristic sniff).
_CODE_SOURCE_TYPES = frozenset({"code", "source", "repo"})


def looks_like_code(text: str, source_type: str | None = None) -> bool:
    if source_type and source_type.lower() in _CODE_SOURCE_TYPES:
        return True
    hits = len(_CODE_HINT.findall(text))
    return hits >= 2


def chunk_text(text: str, *, max_chars: int = 1200, overlap: int = 150,
               source_type: str | None = None) -> list[str]:
    """Split ``text`` into chunks of at most ~``max_chars`` characters."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    overlap = max(0, min(overlap, max_chars // 2))
    atoms = (_code_atoms(text) if looks_like_code(text, source_type)
             else _prose_atoms(text))

    chunks: list[str] = []
    cur = ""
    for atom in atoms:
        for piece in _hard_split(atom, max_chars):
            if not cur:
                cur = piece
            elif len(cur) + 1 + len(piece) <= max_chars:
                cur = f"{cur}\n{piece}"
            else:
                chunks.append(cur)
                tail = cur[-overlap:] if overlap else ""
                cur = f"{tail}\n{piece}" if tail else piece
    if cur:
        chunks.append(cur)
    return [c.strip() for c in chunks if c.strip()]


def _prose_atoms(text: str) -> list[str]:
    # Paragraphs separated by blank lines; markdown headings start a new atom.
    parts = re.split(r"\n\s*\n", text)
    return [p.strip() for p in parts if p.strip()]


def _code_atoms(text: str) -> list[str]:
    parts = _CODE_BOUNDARY.split(text)
    atoms = [p.strip("\n") for p in parts if p.strip()]
    return atoms or [text]


def _hard_split(atom: str, max_chars: int) -> list[str]:
    """Window an over-long atom, preferring to break at a newline/space."""
    if len(atom) <= max_chars:
        return [atom]
    out: list[str] = []
    start = 0
    n = len(atom)
    while start < n:
        end = min(start + max_chars, n)
        if end < n:
            window = atom[start:end]
            cut = max(window.rfind("\n"), window.rfind(" "))
            if cut > max_chars // 2:
                end = start + cut
        out.append(atom[start:end])
        start = end
    return [p for p in out if p.strip()]
