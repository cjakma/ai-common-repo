#!/usr/bin/env python
"""Fill a Qdrant collection with vectors for every active memory and document
chunk that is missing from it.

Two uses, one code path:

* **Model switch** — point ``--collection`` at a new collection and
  ``--backend`` at the new embedding backend; the first run embeds everything
  while the gateway keeps serving from the old collection.
* **Catch-up** — stores made while the embedding backend was down are saved
  without a vector (fail-soft); re-running against the live collection embeds
  just those.

Idempotent: only objects without a point are embedded (point ids are derived
from (object_type, object_id)). ``--prune`` also drops points whose object is
no longer active. Runs inside the gateway container::

    sudo docker exec -i -w /app ai-common-repo python - \\
        --collection ai_common_repo_embgemma768 --backend embed-gemma < scripts/reembed.py
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "..",
                                "packages", "gateway"))

from ai_common_repo.core import constants as C  # noqa: E402
from ai_common_repo.core.config import load_config  # noqa: E402
from ai_common_repo.model_backends.registry import BackendRegistry  # noqa: E402
from ai_common_repo.search.vector import (  # noqa: E402
    QdrantVectorIndex, _point_id, build_vector_index,
)
from ai_common_repo.storage.database import Database  # noqa: E402
from ai_common_repo.storage.repositories import (  # noqa: E402
    DocumentRepository, EmbeddingRepository, MemoryRepository,
)

_ALL = 10**9  # active_all() takes a limit; re-embedding wants every row


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--collection", help="target collection (default: configured one)")
    ap.add_argument("--backend", help="models.backends entry to embed with "
                                      "(default: the configured embedding backend)")
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--prune", action="store_true",
                    help="also delete points whose object is no longer active")
    ap.add_argument("--dry-run", action="store_true", help="only count what is missing")
    ap.add_argument("--limit", type=int, default=0,
                    help="embed at most N missing objects (smoke tests)")
    args = ap.parse_args()

    cfg = load_config()
    if args.collection:
        cfg.storage.qdrant_collection = args.collection
    db = Database(cfg.storage.db_file)
    index = build_vector_index(cfg.storage, EmbeddingRepository(db), dim=cfg.embedding.dim)
    if not isinstance(index, QdrantVectorIndex):
        raise SystemExit(f"vector_backend is {cfg.storage.vector_backend!r}, not qdrant")
    registry = BackendRegistry(cfg.models)
    embedder = registry.get(args.backend) if args.backend else registry.embedder(cfg.embedding.dim)
    if embedder is None or not callable(getattr(embedder, "embed", None)):
        raise SystemExit(f"no embedding backend named {args.backend!r} in config")
    model = getattr(embedder, "model", "external")

    objects = [(C.OBJECT_TYPE_MEMORY, m.id, m.content)
               for m in MemoryRepository(db).active_all(limit=_ALL)]
    objects += [(C.OBJECT_TYPE_CHUNK, cid, text)
                for cid, text in DocumentRepository(db).all_chunks()]
    client, collection = index._client, cfg.storage.qdrant_collection
    exists = client.collection_exists(collection)
    missing = objects
    if exists:
        have = set()
        for i in range(0, len(objects), 500):
            ids = [_point_id(t, o) for t, o, _ in objects[i:i + 500]]
            have.update(str(p.id) for p in client.retrieve(collection, ids=ids,
                                                            with_payload=False))
        missing = [ob for ob in objects if _point_id(ob[0], ob[1]) not in have]
    print(f"[reembed] collection={collection} model={model} objects={len(objects)} "
          f"missing={len(missing)}", flush=True)

    if args.prune and exists and not args.dry_run:
        keep = {_point_id(t, o) for t, o, _ in objects}
        stale, offset = [], None
        while True:
            points, offset = client.scroll(collection, limit=2000, offset=offset,
                                           with_payload=False, with_vectors=False)
            stale += [p.id for p in points if str(p.id) not in keep]
            if offset is None:
                break
        if stale:
            client.delete(collection, points_selector=stale)
        print(f"[reembed] pruned {len(stale)} stale point(s)", flush=True)

    if args.limit:
        missing = missing[:args.limit]
    if args.dry_run or not missing:
        return 0
    started, done = time.monotonic(), 0
    for i in range(0, len(missing), args.batch):
        batch = missing[i:i + args.batch]
        vectors = embedder.embed([text for _, _, text in batch])
        for (otype, oid, _), vec in zip(batch, vectors):
            index.index(otype, oid, vec, model=model)
        done += len(batch)
        if done % 500 < args.batch or done == len(missing):
            rate = done / max(time.monotonic() - started, 1e-6)
            print(f"[reembed] {done}/{len(missing)} ({rate:.1f}/s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
