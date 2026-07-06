"""Renders the persistent-context markdown block injected into agents. Format and
budget are config-driven (memory.max_context_*)."""
from __future__ import annotations

from .config import MemoryConfig
from .models import Memory, Profile

_HEADER = "# Persistent Context"
_RULES = (
    "Rules:\n"
    "- Use this context as silent background only; do not mention or restate it.\n"
    "- Do not expose secrets.\n"
    "- If context conflicts with explicit user instruction, follow the latest user instruction."
)


class ContextBuilder:
    def __init__(self, cfg: MemoryConfig):
        self._cfg = cfg

    def build(self, profile: Profile, memories: list[Memory],
              documents: list[str] | None = None) -> str:
        sections: list[str] = [_HEADER]
        if profile.stable:
            sections.append(self._section("User Stable Preferences", profile.stable))
        if profile.dynamic:
            sections.append(self._section("Recent Relevant Decisions",
                                          profile.dynamic[: self._cfg.recent_max_items]))
        items = [m.content for m in memories[: self._cfg.max_context_items]]
        if items:
            sections.append(self._section("Current Task Related Memories", items))
        if documents:
            sections.append(self._section("Relevant Document Excerpts",
                                          documents[: self._cfg.max_context_items]))
        sections.append(_RULES)
        block = "\n\n".join(sections)
        return block[: self._cfg.max_context_chars]

    @staticmethod
    def _section(title: str, items: list[str]) -> str:
        bullets = "\n".join(f"- {i}" for i in items)
        return f"## {title}\n{bullets}"
