"""Interval-based periodic scheduler (no cron dependency).

Each registered task fires every ``interval_s`` seconds by *enqueuing* a job onto
the shared queue, so the worker — not the scheduler — does the actual work. Each
fire carries an idempotency key derived from its scheduled slot, so overlapping
ticks (or a second scheduler) cannot double-enqueue the same run.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from .queue import JobQueue

Clock = Callable[[], float]


@dataclass
class PeriodicTask:
    name: str
    handler: str
    payload: dict[str, Any]
    interval_s: float
    next_run: float


class Scheduler:
    def __init__(self, queue: JobQueue, *, clock: Clock = time.time,
                 poll_interval_s: float = 1.0, logger: logging.Logger | None = None):
        self._queue = queue
        self._clock = clock
        self._poll_interval_s = poll_interval_s
        self._log = logger or logging.getLogger("ai_common_repo.scheduler")
        self._tasks: list[PeriodicTask] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def every(self, interval_s: float, handler: str, payload: dict[str, Any] | None = None,
              *, name: str | None = None, run_immediately: bool = False) -> None:
        now = self._clock()
        self._tasks.append(PeriodicTask(
            name=name or handler, handler=handler, payload=payload or {},
            interval_s=interval_s,
            next_run=now if run_immediately else now + interval_s,
        ))

    def tick(self) -> int:
        """Enqueue all tasks whose time has come. Returns how many fired."""
        now = self._clock()
        fired = 0
        for task in self._tasks:
            if now < task.next_run:
                continue
            slot = int(task.next_run)
            self._queue.enqueue(task.handler, task.payload,
                                idempotency_key=f"sched:{task.name}:{slot}")
            # advance to the next slot strictly in the future
            while task.next_run <= now:
                task.next_run += task.interval_s
            fired += 1
        return fired

    # -- background loop -----------------------------------------------------
    def start(self) -> None:
        if not self._tasks:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="acr-scheduler", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - never let the loop die
                self._log.exception("scheduler tick error")
            self._stop.wait(self._poll_interval_s)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
