"""Profile builder. Derives a compact stable/dynamic profile from active
memories. Pure function of the memory set -> deterministic and cheap to rebuild."""
from __future__ import annotations

from ..core.config import MemoryConfig
from ..core.models import Memory, Profile

_STABLE_TYPES = {"preference", "constraint"}
_DYNAMIC_TYPES = {"decision", "workflow", "project_fact", "handoff", "todo_context"}


class ProfileBuilder:
    def __init__(self, cfg: MemoryConfig):
        self._cfg = cfg

    def build(self, user_id: str, project_id: str | None,
              memories: list[Memory]) -> Profile:
        ranked = sorted(memories, key=lambda m: (m.confidence, m.updated_at), reverse=True)
        stable, dynamic = [], []
        for mem in ranked:
            bucket = stable if mem.type in _STABLE_TYPES else (
                dynamic if mem.type in _DYNAMIC_TYPES else None)
            if bucket is None:
                continue
            if mem.content not in bucket:
                bucket.append(mem.content)
        return Profile(
            user_id=user_id,
            project_id=project_id,
            stable=self._cap(stable),
            dynamic=self._cap(dynamic),
        )

    def _cap(self, items: list[str]) -> list[str]:
        out, budget = [], self._cfg.profile_max_chars
        for item in items:
            if budget - len(item) < 0:
                break
            out.append(item)
            budget -= len(item)
        return out
