"""In-process worker: polls the job queue and runs registered handlers.

Each handler is a ``callable(payload: dict) -> None`` keyed by name. Handler
exceptions are isolated (one bad job never kills the loop) and routed through the
queue's retry/backoff logic. ``start()`` runs the loop in a background thread;
``stop()`` requests a graceful shutdown that lets the in-flight job finish.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

from ..observability.logging import job_context
from .queue import JobQueue

Handler = Callable[[dict], None]


class Worker:
    def __init__(self, queue: JobQueue, handlers: dict[str, Handler] | None = None, *,
                 poll_interval_s: float = 0.5, logger: logging.Logger | None = None):
        self._queue = queue
        self._handlers: dict[str, Handler] = dict(handlers or {})
        self._poll_interval_s = poll_interval_s
        self._log = logger or logging.getLogger("ai_common_repo.worker")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def register(self, name: str, handler: Handler) -> None:
        self._handlers[name] = handler

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- single-step (used by tests and the loop) ----------------------------
    def run_once(self) -> bool:
        """Claim and execute at most one job. Returns True if work was done."""
        job = self._queue.claim()
        if job is None:
            return False
        handler = self._handlers.get(job.handler)
        if handler is None:
            self._queue.fail(job.id, f"no handler registered for {job.handler!r}")
            self._log.warning("dropping job %s: unknown handler %s", job.id, job.handler)
            return True
        # Bind the job-id so every line a handler logs is correlated to it.
        with job_context(job.id):
            try:
                handler(job.payload)
            except Exception as exc:  # noqa: BLE001 - isolate handler failures
                self._queue.fail(job.id, repr(exc))
                self._log.warning("job %s (%s) failed: %r", job.id, job.handler, exc)
            else:
                self._queue.complete(job.id)
        return True

    def drain(self, max_jobs: int = 1000) -> int:
        """Run queued jobs until none remain (or the cap is hit). For tests."""
        done = 0
        while done < max_jobs and self.run_once():
            done += 1
        return done

    # -- background loop -----------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="acr-worker", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                did_work = self.run_once()
            except Exception:  # noqa: BLE001 - never let the loop die
                self._log.exception("worker loop error")
                did_work = False
            if not did_work:
                self._stop.wait(self._poll_interval_s)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
