"""Deterministic offline embedding fallback.

No model server required: hashes character n-grams into a fixed-width vector
(the "hashing trick") and L2-normalizes. Two texts that share sub-word character
n-grams land near each other, so vector recall keeps working — and stays
deterministic across processes (BLAKE2, not Python's salted ``hash``) — even when
no embedding backend is configured.

This is intentionally a *lexical* embedding, not a semantic one. Its value over
plain FTS is robustness to morphology/typos: ``postgres`` and ``postgresql``
share trigrams and land near each other even though FTS5's whole-word tokenizer
treats them as unrelated terms. It exists so the gateway is useful and testable
fully offline (see PLAN Phase 2)."""
from __future__ import annotations

import hashlib
import math
import re

_TOKEN = re.compile(r"[0-9a-z]+", re.I)
_NGRAM = 3


class HashEmbeddingBackend:
    """An :class:`~.base.EmbeddingBackend` with no external dependency."""

    name = "hash-fallback"

    def __init__(self, dim: int = 256):
        self._dim = max(int(dim), 8)

    @property
    def model(self) -> str:
        return f"hash-{_NGRAM}gram-{self._dim}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        for feat in _features(text.lower()):
            digest = hashlib.blake2b(feat.encode("utf-8"), digest_size=8).digest()
            idx = int.from_bytes(digest[:4], "big") % self._dim
            # Signed hashing: roughly cancels collision bias instead of compounding it.
            vec[idx] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vec))
        return vec if norm == 0.0 else [v / norm for v in vec]


def _features(text: str):
    """Yield space-padded character n-grams for every alphanumeric token."""
    for tok in _TOKEN.findall(text):
        padded = f" {tok} "
        if len(padded) <= _NGRAM:
            yield padded
            continue
        for i in range(len(padded) - _NGRAM + 1):
            yield padded[i:i + _NGRAM]
