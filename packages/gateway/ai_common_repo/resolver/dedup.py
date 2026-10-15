"""Near-duplicate clustering for memory consolidation.

Pure, dependency-free cosine clustering used by the periodic dedup job. Given a
list of vectors it groups indices whose pairwise cosine meets a (high) threshold.
The grouping is greedy and anchored: each still-unassigned vector starts a
cluster and absorbs the remaining vectors similar to it. Conservative by design —
a high threshold keeps unrelated memories apart (no false merges)."""
from __future__ import annotations

import math


def cluster_by_cosine(vectors: list[list[float]],
                      threshold: float) -> list[list[int]]:
    """Return clusters of indices into ``vectors`` (singletons included). Only
    vectors with cosine >= ``threshold`` to a cluster anchor are merged in."""
    n = len(vectors)
    norms = [math.sqrt(sum(v * v for v in vec)) for vec in vectors]
    assigned = [False] * n
    clusters: list[list[int]] = []
    for i in range(n):
        if assigned[i]:
            continue
        assigned[i] = True
        cluster = [i]
        for j in range(i + 1, n):
            if assigned[j]:
                continue
            if _cosine(vectors[i], vectors[j], norms[i], norms[j]) >= threshold:
                assigned[j] = True
                cluster.append(j)
        clusters.append(cluster)
    return clusters


def _cosine(a: list[float], b: list[float], a_norm: float, b_norm: float) -> float:
    if a_norm == 0.0 or b_norm == 0.0 or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (a_norm * b_norm)
