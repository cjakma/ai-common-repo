"""In-process metrics collector.

Thread-safe; records operation counts, error counts and latency so ``/v1/metrics``
can report throughput and search latency. Job-queue depth/throughput come from the
queue itself (DB-backed) — this collector only owns the in-memory latency view that
the database cannot reconstruct after the fact.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass
class _Stat:
    count: int = 0
    errors: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0

    def observe(self, elapsed_ms: float, ok: bool) -> None:
        self.count += 1
        if not ok:
            self.errors += 1
        self.total_ms += elapsed_ms
        self.max_ms = max(self.max_ms, elapsed_ms)

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.count if self.count else 0.0


class MetricsCollector:
    """Records per-operation latency. No secrets pass through here — operation
    names are fixed labels (``search``/``recall``/...), never user input."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ops: dict[str, _Stat] = {}
        self._started = time.time()

    @contextmanager
    def timer(self, op: str):
        """Time a block, recording success/failure. Re-raises any exception."""
        start = time.perf_counter()
        ok = True
        try:
            yield
        except Exception:
            ok = False
            raise
        finally:
            self.observe(op, (time.perf_counter() - start) * 1000.0, ok)

    def observe(self, op: str, elapsed_ms: float, ok: bool = True) -> None:
        with self._lock:
            self._ops.setdefault(op, _Stat()).observe(elapsed_ms, ok)

    def uptime_s(self) -> float:
        return time.time() - self._started

    def snapshot(self) -> dict[str, dict[str, float]]:
        """{op -> {count, errors, avg_ms, max_ms}} — a copy safe to serialize."""
        with self._lock:
            return {
                name: {
                    "count": s.count,
                    "errors": s.errors,
                    "avg_ms": round(s.avg_ms, 3),
                    "max_ms": round(s.max_ms, 3),
                }
                for name, s in self._ops.items()
            }
