"""Wire a Worker + Scheduler from a service and config.

Kept separate from ``service.py`` so the application core does not import the
worker loop machinery; both the server lifespan and the standalone CLI entrypoint
build their runtime through here. The ``service`` argument is duck-typed: it only
needs ``.jobs`` (a JobQueue) and ``.handlers`` (name -> callable).
"""
from __future__ import annotations

from typing import Protocol

from ..core import constants as C
from ..core.config import AppConfig
from .queue import JobQueue
from .scheduler import Scheduler
from .worker import Worker


class _Runnable(Protocol):
    jobs: JobQueue
    handlers: dict


def build_runtime(service: _Runnable, cfg: AppConfig) -> tuple[Worker, Scheduler]:
    worker = Worker(service.jobs, service.handlers,
                    poll_interval_s=cfg.workers.poll_interval_s)
    scheduler = Scheduler(service.jobs)
    if cfg.memory.forget_enabled:
        scheduler.every(cfg.memory.forget_interval_s, C.JOB_FORGET_EXPIRED,
                        name="forget_expired")
    for name, sub in (("github", cfg.connectors.github),
                      ("notion", cfg.connectors.notion)):
        if sub.enabled:
            scheduler.every(sub.poll_interval_s, C.JOB_CONNECTOR_POLL,
                            {"connector": name}, name=f"connector:{name}")
    return worker, scheduler
