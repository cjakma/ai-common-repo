#!/usr/bin/env python
"""One-shot backfill: copy vectors from the legacy sqlite ``embeddings`` table
into the canonical Qdrant index.

The vector store moved from sqlite-vec/brute-force (vectors in the sqlite
``embeddings`` table) to Qdrant. Deployments that already accumulated embeddings
need them re-indexed into Qdrant once; fresh installs have nothing to copy and
this is a no-op.

Idempotent: Qdrant point ids are derived deterministically from
(object_type, object_id) (see ``vector._point_id``), so re-running upserts in
place rather than duplicating.

Usage::

    python -m scripts.backfill_qdrant                 # uses default config
    AICR_STORAGE__QDRANT_URL=http://127.0.0.1:6333 python scripts/backfill_qdrant.py

Reads connection details from the same config/env the gateway uses.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "gateway"))

from ai_common_repo.core import constants as C  # noqa: E402
from ai_common_repo.core.config import AppConfig, load_config  # noqa: E402
from ai_common_repo.search.vector import QdrantVectorIndex, build_vector_index  # noqa: E402
from ai_common_repo.storage.database import Database  # noqa: E402
from ai_common_repo.storage.repositories import EmbeddingRepository  # noqa: E402

_OBJECT_TYPES = [C.OBJECT_TYPE_MEMORY, C.OBJECT_TYPE_CHUNK]


def backfill(cfg: AppConfig) -> int:
    """Copy every legacy sqlite embedding into Qdrant. Returns rows copied."""
    db = Database(cfg.storage.db_file)
    embeddings = EmbeddingRepository(db)
    index = build_vector_index(cfg.storage, embeddings, dim=cfg.embedding.dim)
    if not isinstance(index, QdrantVectorIndex):
        raise SystemExit(
            f"vector_backend is {cfg.storage.vector_backend!r}, not qdrant — "
            "nothing to backfill into. Set storage.vector_backend=qdrant.")

    copied = 0
    for object_type, object_id, vector in embeddings.vectors_for(_OBJECT_TYPES):
        if not vector:
            continue
        index.index(object_type, object_id, vector, model="backfill")
        copied += 1
    return copied


def main() -> None:
    cfg = load_config()
    copied = backfill(cfg)
    where = cfg.storage.qdrant_url or cfg.storage.qdrant_location or ":memory:"
    print(f"backfilled {copied} vector(s) into qdrant "
          f"collection={cfg.storage.qdrant_collection!r} target={where}")


if __name__ == "__main__":
    main()
